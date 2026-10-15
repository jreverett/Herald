"""Optional OAuth resource-server boundary. No authorization server or login UI.

Python uses only its standard library. RS256 verification delegates to installed
OpenSSL, not a handwritten cryptographic primitive. One issuer and owner-approved subjects per owner.
"""
import base64
import hashlib
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

SCOPES = ["herald:read", "herald:write", "herald:events"]
DIAGNOSTIC_METHODS = ("tools/call", "events/list", "events/subscribe", "events/unsubscribe", "other")
DENIAL_REASONS = ("invalid_token", "connection_disabled", "missing_authorization", "malformed_token",
                 "unsupported_header", "unknown_signing_key", "invalid_signature", "issuer_mismatch",
                 "audience_mismatch", "subject_mismatch", "invalid_time_claims", "token_expired",
                 "token_not_yet_valid", "insufficient_scope", "verifier_or_provider_error")


MAX_SUBJECTS = 8
MAX_CANDIDATES = 5
CANDIDATE_WRITE_INTERVAL = 60


def valid_text(value):
    return isinstance(value, str) and bool(value) and not any(ord(c) < 32 for c in value)


def approved_subjects(config):
    subjects = set(config.get("subjects", []))
    if config.get("subject"):
        subjects.add(config["subject"])
    return subjects


def pending_path(config_path):
    return Path(config_path).with_suffix(".pending.json")


def write_json(path, value, mode):
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def load_candidates(config_path):
    try:
        value = json.loads(pending_path(config_path).read_text())
    except (OSError, ValueError):
        return []
    candidates = value.get("candidates", []) if isinstance(value, dict) else []
    return [c for c in candidates if isinstance(c, dict) and valid_text(c.get("subject"))]


def list_accounts(config_path):
    return sorted(approved_subjects(json.loads(Path(config_path).read_text()))), load_candidates(config_path)


def save_subjects(path, config, subjects):
    config.pop("subject", None)
    config["subjects"] = sorted(subjects)
    write_json(path, config, path.stat().st_mode & 0o777)


def approve_candidate(config_path, number):
    path = Path(config_path)
    candidates = load_candidates(path)
    if not 1 <= number <= len(candidates):
        raise ValueError(f"No pending sign-in number {number}")
    chosen = candidates[number - 1]
    config = json.loads(path.read_text())
    subjects = approved_subjects(config) | {chosen["subject"]}
    if len(subjects) > MAX_SUBJECTS:
        raise ValueError(f"At most {MAX_SUBJECTS} approved accounts")
    save_subjects(path, config, subjects)
    write_json(pending_path(path), {"candidates": [c for c in candidates if c is not chosen]}, 0o600)
    return chosen


def revoke_subject(config_path, number):
    path = Path(config_path)
    config = json.loads(path.read_text())
    subjects = sorted(approved_subjects(config))
    if not 1 <= number <= len(subjects):
        raise ValueError(f"No approved account number {number}")
    save_subjects(path, config, set(subjects) - {subjects[number - 1]})
    return subjects[number - 1]


class AuthError(Exception):
    def __init__(self, code="invalid_token", scope="", reason="invalid_token"):
        self.code, self.scope = code, scope
        self.reason = reason if reason in DENIAL_REASONS else "invalid_token"


def required_scope(method, params):
    if method == "tools/call":
        if params.get("name") == "check_event_access":
            return "herald:events"
        return "herald:write" if params.get("name") in ("send_message", "reply") else "herald:read"
    return "herald:events" if method.startswith("events/") else "herald:read"


def decode(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid base64url")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def der(tag, payload):
    length = len(payload)
    encoded = bytes([length]) if length < 128 else length.to_bytes((length.bit_length() + 7) // 8, "big")
    if length >= 128:
        encoded = bytes([128 + len(encoded)]) + encoded
    return bytes([tag]) + encoded + payload


def public_pem(key):
    """Encode a provider's public RSA numbers as SPKI for OpenSSL."""
    if key.get("kty") != "RSA" or key.get("alg", "RS256") != "RS256" or key.get("use", "sig") != "sig":
        raise ValueError("Unsupported signing key")
    if "key_ops" in key and "verify" not in key["key_ops"]:
        raise ValueError("Key cannot verify")
    modulus, exponent = int.from_bytes(decode(key["n"]), "big"), int.from_bytes(decode(key["e"]), "big")
    if not 2048 <= modulus.bit_length() <= 8192 or not 3 <= exponent <= 2**32 or exponent % 2 == 0:
        raise ValueError("Invalid RSA key")
    def integer(number):
        data = number.to_bytes((number.bit_length() + 7) // 8, "big")
        return der(2, b"\0" + data if data[0] & 128 else data)
    rsa = der(48, integer(modulus) + integer(exponent))
    algorithm = bytes.fromhex("300d06092a864886f70d0101010500")
    spki = der(48, algorithm + der(3, b"\0" + rsa))
    encoded = base64.b64encode(spki).decode()
    return ("-----BEGIN PUBLIC KEY-----\n" + "\n".join(encoded[i:i+64] for i in range(0, len(encoded), 64))
            + "\n-----END PUBLIC KEY-----\n").encode()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_keys(url):
    with urllib.request.build_opener(NoRedirect()).open(url, timeout=5) as response:
        body = response.read(65537)
    if len(body) > 65536:
        raise ValueError("JWKS too large")
    return json.loads(body)


class ResourceAuth:
    def __init__(self, config_path, owner, fetcher=None):
        self.path, self.owner = Path(config_path), owner
        self.fetcher = fetcher or fetch_keys
        self.openssl = shutil.which("openssl")
        if not self.openssl:
            raise ValueError("OAuth mode requires installed OpenSSL")
        self.lock = threading.RLock()
        self.counts = {name: 0 for name in ("config_reads", "authentication_calls", "jwks_fetches",
                                           "signature_verifications", "token_cache_hits", "authentication_denials")}
        self.denial_reasons = {method: {reason: 0 for reason in DENIAL_REASONS} for method in DIAGNOSTIC_METHODS}
        self.config_stamp, self.config = None, None
        self.keys, self.keys_until, self.last_fetch = {}, 0, -float("inf")
        self.cache = {}
        self.settings()

    def settings(self):
        with self.lock:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            if stamp != self.config_stamp:
                value = json.loads(self.path.read_text())
                self.counts["config_reads"] += 1
                if value.get("owner") != self.owner or not isinstance(value.get("enabled"), bool):
                    raise ValueError("OAuth owner/enabled mismatch")
                for field in ("issuer", "resource", "jwks_uri"):
                    if not valid_text(value.get(field)):
                        raise ValueError("OAuth configuration incomplete")
                subjects = value.get("subjects", [])
                if ("subject" in value and not valid_text(value["subject"]) or not isinstance(subjects, list)
                        or len(subjects) > MAX_SUBJECTS or not all(valid_text(s) for s in subjects)):
                    raise ValueError("OAuth subjects must be non-empty strings")
                for field in ("issuer", "resource", "jwks_uri"):
                    p = urlsplit(value[field])
                    if p.scheme != "https" or not p.hostname or p.username or p.password or p.fragment or p.query:
                        raise ValueError("OAuth URLs require HTTPS without credentials/query/fragment")
                if urlsplit(value["issuer"]).netloc != urlsplit(value["jwks_uri"]).netloc:
                    raise ValueError("JWKS must belong to the configured issuer origin")
                self.config, self.config_stamp = value, stamp
                self.keys, self.cache, self.keys_until, self.last_fetch = {}, {}, 0, -float("inf")
            return dict(self.config)

    def record_candidate(self, claims, now):
        subject = claims.get("sub")
        if not valid_text(subject) or len(subject) > 256:
            return
        candidates = load_candidates(self.path)
        existing = next((c for c in candidates if c["subject"] == subject), None)
        if existing and now - existing.get("last_seen", 0) < CANDIDATE_WRITE_INTERVAL:
            return
        entry = existing or {"subject": subject, "first_seen": now, "count": 0}
        entry.update({"last_seen": now, "count": entry.get("count", 0) + 1})
        for claim in ("email", "name"):
            if valid_text(claims.get(claim)) and len(claims[claim]) <= 256:
                entry[claim] = claims[claim]
        others = [c for c in candidates if c is not existing]
        try:
            write_json(pending_path(self.path), {"candidates": ([entry] + others)[:MAX_CANDIDATES]}, 0o600)
        except OSError:
            pass

    def metadata(self):
        config = self.settings()
        return {"resource": config["resource"], "authorization_servers": [config["issuer"]], "scopes_supported": SCOPES}

    def stats(self):
        with self.lock:
            return {**self.counts, "authentication_denial_reasons_by_method": {
                method: dict(reasons) for method, reasons in self.denial_reasons.items()}}

    def metadata_path(self):
        return "/.well-known/oauth-protected-resource" + urlsplit(self.settings()["resource"]).path.rstrip("/")

    def challenge(self, error):
        p = urlsplit(self.settings()["resource"])
        url = p.scheme + "://" + p.netloc + self.metadata_path()
        scope = ', scope="' + error.scope + '"' if error.scope else ""
        return ('Bearer resource_metadata="' + url + '", error="' + error.code
                + '", error_description="Connect the approved owner account with the required Herald scope"' + scope)

    def active_grant(self, grant):
        try:
            c = self.settings()
            return (c["enabled"] and isinstance(grant, dict) and grant.get("issuer") == c["issuer"]
                    and grant.get("subject") in approved_subjects(c) and grant.get("expires", 0) > time.time())
        except (OSError, ValueError):
            return False

    def authenticate(self, authorization, scope, rpc_method="other"):
        method = rpc_method if isinstance(rpc_method, str) and rpc_method in DIAGNOSTIC_METHODS else "other"
        try:
            with self.lock:
                self.counts["authentication_calls"] += 1
                c, now = self.settings(), time.time()
                if not c["enabled"]:
                    raise AuthError(reason="connection_disabled")
                if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
                    raise AuthError(reason="missing_authorization")
                token = authorization[7:]
                if len(token) > 16384:
                    raise AuthError(reason="malformed_token")
                digest = hashlib.sha256(token.encode()).hexdigest()
                cached = self.cache.get(digest)
                if cached and cached[0] > now:
                    self.counts["token_cache_hits"] += 1
                    claims = cached[1]
                else:
                    parts = token.split(".")
                    if len(parts) != 3:
                        raise AuthError(reason="malformed_token")
                    header, claims = json.loads(decode(parts[0])), json.loads(decode(parts[1]))
                    if (not isinstance(header, dict) or not isinstance(claims, dict) or header.get("alg") != "RS256"
                            or not isinstance(header.get("kid"), str) or not header["kid"] or "crit" in header):
                        raise AuthError(reason="unsupported_header")
                    # Never follow token-supplied jku/jwk/x5u: only trusted configured JWKS.
                    kid = header["kid"]
                    if now >= self.keys_until or kid not in self.keys:
                        if now - self.last_fetch < 10:
                            raise AuthError(reason="unknown_signing_key")
                        self.last_fetch = now
                        self.counts["jwks_fetches"] += 1
                        values = self.fetcher(c["jwks_uri"])["keys"]
                        if not isinstance(values, list) or not 1 <= len(values) <= 32:
                            raise AuthError(reason="verifier_or_provider_error")
                        keys = {}
                        for key in values:
                            if not isinstance(key, dict) or not isinstance(key.get("kid"), str) or key["kid"] in keys:
                                raise AuthError(reason="verifier_or_provider_error")
                            if key.get("kty") == "RSA" and key.get("use", "sig") == "sig":
                                keys[key["kid"]] = public_pem(key)
                        self.keys, self.keys_until = keys, now + 300
                    if kid not in self.keys:
                        raise AuthError(reason="unknown_signing_key")
                    with tempfile.TemporaryDirectory(prefix="herald-public-verify-") as directory:
                        pub, signature = Path(directory)/"public.pem", Path(directory)/"signature.bin"
                        pub.write_bytes(self.keys[kid]); signature.write_bytes(decode(parts[2]))
                        self.counts["signature_verifications"] += 1
                        verified = subprocess.run([self.openssl, "dgst", "-sha256", "-verify", str(pub), "-signature", str(signature)],
                            input=(parts[0]+"."+parts[1]).encode(), stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=5, check=False)
                        if verified.returncode != 0:
                            raise AuthError(reason="invalid_signature")
                    audience = claims.get("aud")
                    if claims.get("iss") != c["issuer"]:
                        raise AuthError(reason="issuer_mismatch")
                    if not (audience == c["resource"] or isinstance(audience, list) and c["resource"] in audience):
                        raise AuthError(reason="audience_mismatch")
                    for field in ("exp", "iat"):
                        v = claims.get(field)
                        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                            raise AuthError(reason="invalid_time_claims")
                    if claims["iat"] > now + 30 or claims["exp"] <= claims["iat"]:
                        raise AuthError(reason="invalid_time_claims")
                    if claims["exp"] <= now:
                        raise AuthError(reason="token_expired")
                    nbf = claims.get("nbf", 0)
                    if isinstance(nbf, bool) or not isinstance(nbf, (int,float)) or not math.isfinite(nbf):
                        raise AuthError(reason="invalid_time_claims")
                    if nbf > now:
                        raise AuthError(reason="token_not_yet_valid")
                    # Subject is checked last so only an otherwise valid sign-in becomes a candidate.
                    if claims.get("sub") not in approved_subjects(c):
                        self.record_candidate(claims, now)
                        raise AuthError(reason="subject_mismatch")
                    self.cache = {k:v for k,v in self.cache.items() if v[0] > now}
                    if len(self.cache) >= 128:
                        self.cache.pop(next(iter(self.cache)))
                    self.cache[digest] = (min(claims["exp"], now+60, self.keys_until), claims)
                if not isinstance(claims.get("scope"), str) or scope not in claims["scope"].split():
                    raise AuthError("insufficient_scope", scope, reason="insufficient_scope")
                return {"issuer": c["issuer"], "subject": claims["sub"], "expires": claims["exp"]}
        except AuthError as error:
            with self.lock:
                self.counts["authentication_denials"] += 1
                self.denial_reasons[method][error.reason] += 1
            raise
        except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
            with self.lock:
                self.counts["authentication_denials"] += 1
                self.denial_reasons[method]["verifier_or_provider_error"] += 1
            raise AuthError() from None
