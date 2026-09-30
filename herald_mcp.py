"""Owner-scoped Herald MCP 2.0 + webhook events proof of concept (stdlib only).

Run one bridge process per owner/HERALD_DIR. See README for local-only setup.
No received text is executed, and events contain identifiers rather than text.
"""
import argparse
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import re
import secrets
import resource
import socket
import sqlite3
import ssl
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

VERSION = "2026-07-28"
META_VERSION = "io.modelcontextprotocol/protocolVersion"
EVENT = "herald.message.created"
MAX_BODY = 262144


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def iso(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


class RPCError(Exception):
    def __init__(self, code, message, data=None):
        self.code, self.message, self.data = code, message, data


def invalid(message):
    raise RPCError(-32602, message)


def strings(arguments, required, optional=()):
    if not isinstance(arguments, dict) or set(arguments) - set(required) - set(optional):
        invalid("Unexpected arguments")
    if any(not isinstance(arguments.get(k), str) or not arguments[k] for k in required):
        invalid("Required arguments must be nonempty strings")
    if any(k in arguments and not isinstance(arguments[k], str) for k in optional):
        invalid("Optional arguments must be strings")


def signing_key(secret):
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        invalid("Expected a whsec_ signing secret")
    try:
        key = base64.b64decode(secret[6:], validate=True)
    except ValueError:
        invalid("Invalid signing secret encoding")
    if not 24 <= len(key) <= 64:
        invalid("Signing key must contain 24-64 bytes")
    return key


def signed_headers(secret, event_id, body, old_secret=None, timestamp=None):
    stamp = str(int(time.time() if timestamp is None else timestamp))
    message = event_id.encode() + b"." + stamp.encode() + b"." + body
    signatures = []
    for value in (secret, old_secret):
        if value:
            sig = hmac.new(signing_key(value), message, hashlib.sha256).digest()
            signatures.append("v1," + base64.b64encode(sig).decode())
    return {"Content-Type": "application/json", "webhook-id": event_id,
            "webhook-timestamp": stamp, "webhook-signature": " ".join(signatures)}


class CallbackTransport:
    """Resolve on every attempt; pin TCP destination, preserve hostname for TLS."""
    def __init__(self, test_loopback=False):
        self.test_loopback = test_loopback

    def post(self, url, body, headers, allowed_hosts):
        parts = urlsplit(url)
        local = self.test_loopback and parts.scheme == "http" and parts.hostname == "127.0.0.1"
        if (parts.username or parts.password or parts.fragment or not parts.hostname
                or parts.hostname not in allowed_hosts or (parts.scheme != "https" and not local)):
            raise RPCError(-32015, "Callback rejected", {"reason": "invalid_url"})
        port = parts.port or (443 if parts.scheme == "https" else 80)
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global
                                and not (local and a[4][0] == "127.0.0.1") for a in addresses):
            raise RPCError(-32015, "Callback rejected", {"reason": "non_public_address"})
        address = (addresses[0][4][0], port)
        conn = http.client.HTTPConnection(parts.hostname, port, timeout=10)
        try:
            # Using the resolved numeric address prevents a second DNS lookup/rebinding.
            conn.sock = socket.create_connection(address, timeout=10)
            if parts.scheme == "https":
                conn.sock = ssl.create_default_context().wrap_socket(conn.sock, server_hostname=parts.hostname)
            target = (parts.path or "/") + ("?" + parts.query if parts.query else "")
            conn.request("POST", target, body, headers={**headers, "Host": parts.netloc})
            response = conn.getresponse()
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise RPCError(-32015, "Callback response too large", {"reason": "response_size"})
            return response.status, data
        finally:
            conn.close()


def schema(properties, required):
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


def text_property(description):
    return {"type": "string", "minLength": 1, "maxLength": 4000, "description": description}


TOOLS = [
    {"name": "usage_stats", "description": "Read local bridge counters and resource usage. ChatGPT tokens/credits are not observable here.",
     "inputSchema": schema({}, [])},
    {"name": "send_message", "description": "Send owner-approved text to an allowlisted person's dot mailbox.",
     "inputSchema": schema({"peer": text_property("Configured person"), "text": text_property("Message data"),
                            "request_id": text_property("Reuse this key when retrying the same send")},
                           ["peer", "text", "request_id"])},
    {"name": "list_messages", "description": "List text messages in this owner's dedicated mailbox without claiming work.",
     "inputSchema": schema({}, [])},
    {"name": "read_message", "description": "Read one authorized message as external data; does not execute or claim it.",
     "inputSchema": schema({"id": text_property("Inbox message ID")}, ["id"])},
    {"name": "reply", "description": "Send one owner-approved threaded reply. Reuse request_id for retries; never auto-reply to acknowledgements.",
     "inputSchema": schema({"id": text_property("Inbox message ID"), "text": text_property("Reply data"),
                            "request_id": text_property("Retry key")}, ["id", "text", "request_id"])},
]
for tool in TOOLS:
    read_only = tool["name"] in ("usage_stats", "list_messages", "read_message")
    tool["annotations"] = {"readOnlyHint": read_only, "destructiveHint": False,
                           "idempotentHint": True, "openWorldHint": not read_only}


class Bridge:
    def __init__(self, config_path, herald, transport=None):
        self.config_path, self.herald = Path(config_path), herald
        self.owner = json.loads(self.config_path.read_text())["owner"]
        self.transport = transport or CallbackTransport()
        self.lock = threading.RLock()
        self.tick_lock = threading.Lock()
        self.stats_lock = threading.Lock()
        self.cache_lock = threading.RLock()
        self.cache = {}
        self.started = time.time()
        self.cpu_start = time.process_time()
        self.counts = {key: 0 for key in (
            "ticks", "idle_ticks", "inbox_scans", "inbox_files_parsed", "inbox_bytes_read",
            "config_reads", "config_bytes_read", "sqlite_commits", "events_created",
            "pending_rows_loaded", "callback_verifications", "callback_attempts", "callback_accepted",
            "callback_failures", "callback_retries", "callback_request_bytes", "callback_response_bytes",
            "tool_calls", "send_calls", "reply_calls", "idempotent_write_hits")}
        self.inbox_stamp = None
        self.reconcile_at = 0
        self.catalog_dirty = True
        self.verified = {}
        policy = self.policy()
        self.agent, self.mailbox = policy["agent"], policy["mailbox"]
        if herald.load_config()["me"] != self.owner:
            raise ValueError("Bridge owner must match Herald me")
        if os.environ.get("HERALD_AGENT") != policy["agent"] or os.environ.get("HERALD_MAILBOX") != policy["mailbox"]:
            raise ValueError("HERALD_AGENT and HERALD_MAILBOX must match bridge policy")
        herald.ensure_dirs()
        db_path = herald.HERALD_DIR / "mcp.sqlite3"
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        os.chmod(db_path, 0o600)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS writes (key TEXT PRIMARY KEY, args TEXT, payload TEXT, result TEXT);
            CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY, record TEXT);
            CREATE TABLE IF NOT EXISTS deliveries (sub TEXT, event TEXT, record TEXT, PRIMARY KEY(sub,event));
            CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries
                (sub, json_extract(record,'$.state'), json_extract(record,'$.next'));
            CREATE INDEX IF NOT EXISTS deliveries_message ON deliveries
                (sub, json_extract(record,'$.event.data.id'));
            CREATE INDEX IF NOT EXISTS subscriptions_active ON subscriptions
                (json_extract(record,'$.enabled'), json_extract(record,'$.expires'));
            CREATE TABLE IF NOT EXISTS inbox_seen (path TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS message_catalog
                (id TEXT PRIMARY KEY, received REAL, peer TEXT, mailbox TEXT, agent TEXT, thread TEXT);
            CREATE INDEX IF NOT EXISTS catalog_received ON message_catalog(received);
        """)
        self.db.commit()

    def count(self, name, amount=1):
        with self.stats_lock:
            self.counts[name] += amount

    def commit(self):
        self.db.commit()
        self.count("sqlite_commits")

    def cached_json(self, path):
        path = Path(path)
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        with self.cache_lock:
            prior = self.cache.get(str(path))
            if prior and prior[0] == signature:
                return prior[1]
            raw = path.read_bytes()
            value = json.loads(raw)
            self.cache[str(path)] = (signature, value)
            self.count("config_reads")
            self.count("config_bytes_read", len(raw))
            self.catalog_dirty = True
            return value

    def herald_config(self):
        return self.cached_json(self.herald.CONFIG_PATH)

    def policy(self):
        policy = self.cached_json(self.config_path)
        if policy.get("owner") != self.owner or policy.get("enabled") is not True:
            raise RPCError(-32012, "Owner access disabled")
        if not isinstance(policy.get("peers"), dict) or not isinstance(policy.get("callback_hosts"), list):
            raise ValueError("Invalid bridge policy")
        if hasattr(self, "agent") and (policy.get("agent") != self.agent or policy.get("mailbox") != self.mailbox):
            raise RPCError(-32012, "Restart required after identity changes")
        for key, default, low, high in (("event_limit", 10, 0, 1000), ("max_subscriptions", 8, 1, 64),
                                       ("callbacks_per_tick", 16, 1, 64)):
            value = policy.get(key, default)
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise ValueError("Invalid bridge resource limit")
        return policy

    def peer(self, name):
        policy = self.policy()
        if name not in policy["peers"] or name not in self.herald_config().get("peers", {}):
            raise RPCError(-32012, "Peer is not allowlisted")
        return policy["peers"][name]

    def authorized_item(self, item, p=None, config=None):
        p = p or self.policy()
        config = config or self.herald_config()
        return (item.get("kind") == "message" and item.get("from") in p["peers"]
                and item.get("from") in config.get("peers", {})
                and item.get("to_mailbox") == p["mailbox"]
                and item.get("to_agent", "") in ("", p["agent"]))

    def items(self):
        items = []
        policy, config = self.policy(), self.herald_config()
        for path in self.herald.INBOX_DIR.glob("*.json"):
            try:
                item = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if self.authorized_item(item, policy, config):
                items.append(item)
        return sorted(items, key=lambda i: (i.get("received_ts", 0), i["id"]))

    def item(self, item_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item_id):
            invalid("Invalid message ID")
        try:
            item = json.loads((self.herald.INBOX_DIR / (item_id + ".json")).read_text())
            if item.get("id") == item_id and self.authorized_item(item):
                return item
        except (OSError, ValueError):
            pass
        raise RPCError(-32011, "Message not found in this owner's mailbox")

    def tool(self, name, args):
        self.count("tool_calls")
        if name == "usage_stats":
            strings(args, [])
            with self.stats_lock:
                counters = dict(self.counts)
            usage = resource.getrusage(resource.RUSAGE_SELF)
            peak_bytes = int(usage.ru_maxrss * (1 if os.uname().sysname == "Darwin" else 1024))
            with self.lock:
                pending = self.db.execute("SELECT count(*) FROM deliveries WHERE json_extract(record,'$.state')='pending'").fetchone()[0]
            return {"counters": counters, "uptime_seconds": time.time() - self.started,
                    "process_cpu_seconds_since_bridge_start": time.process_time() - self.cpu_start,
                    "process_peak_rss_bytes": peak_bytes, "pending_events": pending,
                    "event_limit_per_subscription": self.policy().get("event_limit", 10),
                    "bridge_model_calls": 0, "autonomous_reply_calls": 0,
                    "platform_tokens_and_credits": "unknown_until_owner_approved_live_test",
                    "scope": "Counters are in-memory since this bridge start; resources cover this process, not the whole host."}
        if name == "list_messages":
            strings(args, [])
            return {"messages": [{"id": i["id"], "from": i["from"], "thread": i["thread"],
                                   "state": self.herald.item_state(i)} for i in self.items()[-100:]]}
        if name == "read_message":
            strings(args, ["id"])
            item = self.item(args["id"])
            return {k: item[k] for k in ("id", "from", "thread", "text", "received")}
        if name not in ("send_message", "reply"):
            raise RPCError(-32602, "Unknown tool")
        strings(args, ["peer" if name == "send_message" else "id", "text", "request_id"])
        if any(len(v) > 4000 for v in args.values()):
            invalid("Text and identifiers are limited to 4000 characters")
        with self.lock:
            key = hashlib.sha256(canonical([self.owner, name, args["request_id"]]).encode()).hexdigest()
            row = self.db.execute("SELECT args,payload,result FROM writes WHERE key=?", (key,)).fetchone()
            if row and row[0] != canonical(args):
                invalid("request_id already used with different arguments")
            if name == "send_message":
                peer = args["peer"]
                address = self.peer(peer)
                payload = {"kind": "message", "text": args["text"], "to_mailbox": address["mailbox"],
                           "to_agent": address["agent"], "targeted": True, "fallback": "hold"}
            else:
                orig = self.item(args["id"])
                peer = orig["from"]
                address = self.peer(peer)
                # Only the configured adapter identity may claim; callers cannot override it.
                if not row:
                    orig = self.herald._claim_item(orig["id"], self.policy()["agent"], self.policy()["mailbox"])
                payload = {"kind": "message", "text": args["text"], "thread": orig["thread"],
                           "reply_to": orig["id"], "to_mailbox": address["mailbox"],
                           "to_agent": address["agent"], "targeted": True, "fallback": "hold",
                           "_source_item_id": orig["id"], "_source_revision": orig.get("ownership_revision", 0),
                           "_source_effect": "final"}
            if row and row[2]:
                self.count("idempotent_write_hits")
                return json.loads(row[2])
            if row:
                payload = json.loads(row[1])
            else:
                payload["delivery_id"] = secrets.token_hex(16)
                self.db.execute("INSERT INTO writes VALUES (?,?,?,NULL)", (key, canonical(args), canonical(payload)))
                self.commit()  # Persist stable delivery ID BEFORE network transmission.
            self.count("send_calls" if name == "send_message" else "reply_calls")
            result = self.herald.deliver(self.herald_config(), peer, payload)
            self.db.execute("UPDATE writes SET result=? WHERE key=?", (canonical(result), key))
            self.commit()
            return result

    def definition(self):
        p = self.policy()
        reachable = sorted(set(p["peers"]) & set(self.herald_config().get("peers", {})))
        return {"name": EVENT, "description": "New text message in the connected owner's dedicated Herald mailbox.",
                "delivery": ["webhook"],
                "inputSchema": schema({"mailbox": {"type": "string", "enum": [p["mailbox"]]},
                                       "peer": {"type": "string", "enum": reachable}}, ["mailbox"]),
                "payloadSchema": schema({k: {"type": "string"} for k in ("id", "peer", "mailbox", "thread")},
                                        ["id", "peer", "mailbox", "thread"])}

    def subscription_identity(self, params, need_secret=True):
        if params.get("name") != EVENT:
            invalid("Unknown event")
        args = params.get("arguments")
        strings(args, ["mailbox"], ["peer"])
        if args["mailbox"] != self.policy()["mailbox"]:
            raise RPCError(-32012, "Mailbox is not authorized")
        if "peer" in args:
            self.peer(args["peer"])
        delivery = params.get("delivery", {})
        if not isinstance(delivery, dict) or delivery.get("mode") != "webhook" or not isinstance(delivery.get("url"), str):
            invalid("Expected webhook delivery URL")
        if need_secret:
            signing_key(delivery.get("secret"))
        identity = [self.owner, delivery["url"], EVENT, args]
        return "sub_" + hashlib.sha256(canonical(identity).encode()).hexdigest(), args, delivery

    def subscribe(self, params):
        sub_id, args, delivery = self.subscription_identity(params)
        if params.get("cursor") is not None:
            invalid("This event does not support protocol replay; use list_messages for backlog")
        ttl = params.get("ttlMs", 3600000)
        if ttl is None:
            ttl = 3600000
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 1000:
            invalid("ttlMs must be at least 1000 milliseconds")
        now = time.time()
        with self.lock:
            existing = self.db.execute("SELECT record FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
            active = self.db.execute("SELECT count(*) FROM subscriptions WHERE json_extract(record,'$.enabled')=1 AND json_extract(record,'$.expires')>?", (now,)).fetchone()[0]
            prior = json.loads(existing[0]) if existing else {}
            if not (prior.get("enabled") and prior.get("expires", 0) > now) and active >= self.policy().get("max_subscriptions", 8):
                raise RPCError(-32012, "Owner subscription limit reached")
        cache_key = (self.owner, delivery["url"], hashlib.sha256(delivery["secret"].encode()).hexdigest())
        self.verified = {key: expires for key, expires in self.verified.items() if expires > now}
        if len(self.verified) >= 128 and cache_key not in self.verified:
            del self.verified[min(self.verified, key=self.verified.get)]
        if self.verified.get(cache_key, 0) < now:
            challenge = secrets.token_urlsafe(32)
            body = canonical({"type": "verification", "challenge": challenge}).encode()
            headers = signed_headers(delivery["secret"], "verify_" + secrets.token_hex(16), body)
            headers["X-MCP-Subscription-Id"] = sub_id
            try:
                self.count("callback_verifications")
                self.count("callback_request_bytes", len(body))
                status, response = self.transport.post(delivery["url"], body, headers, self.policy()["callback_hosts"])
                self.count("callback_response_bytes", len(response))
                echo = json.loads(response).get("challenge", "")
                if not (200 <= status < 300 and isinstance(echo, str) and hmac.compare_digest(challenge, echo)):
                    raise RPCError(-32015, "Callback verification failed", {"reason": "challenge_failed"})
            except RPCError:
                raise
            except (OSError, ValueError, http.client.HTTPException):
                raise RPCError(-32015, "Callback verification failed", {"reason": "timeout_or_response"})
            self.verified[cache_key] = now + 300
        with self.lock:
            row = self.db.execute("SELECT record FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
            old = json.loads(row[0]) if row else {}
            active_refresh = old.get("enabled") and old.get("expires", 0) > now
            active_count = self.db.execute("SELECT count(*) FROM subscriptions WHERE json_extract(record,'$.enabled')=1 "
                                           "AND json_extract(record,'$.expires')>?", (now,)).fetchone()[0]
            if not active_refresh and active_count >= self.policy().get("max_subscriptions", 8):
                raise RPCError(-32012, "Owner subscription limit reached")
            if not active_refresh:
                self.db.execute("DELETE FROM deliveries WHERE sub=?", (sub_id,))
            rec = {"id": sub_id, "owner": self.owner, "arguments": args, "url": delivery["url"],
                   "secret": delivery["secret"], "created": old["created"] if active_refresh else now,
                   "expires": now + min(ttl, 86400000) / 1000, "enabled": True}
            if old.get("secret") and old["secret"] != rec["secret"]:
                rec.update(old_secret=old["secret"], rotation_until=now + 300)
            elif old.get("rotation_until", 0) > now:
                rec.update(old_secret=old.get("old_secret"), rotation_until=old["rotation_until"])
            self.db.execute("INSERT OR REPLACE INTO subscriptions VALUES (?,?)", (sub_id, canonical(rec)))
            self.commit()
            self.catalog_dirty = True
        return {"id": sub_id, "refreshBefore": iso(rec["expires"]), "cursor": None, "truncated": False}

    def scan_new(self, cutoff):
        """Directory metadata detects changes; parse each newly observed record once.

        A 30-second reconciliation also handles filesystems with coarse directory
        timestamps. Existing history is registered without reading message bodies.
        The durable name index survives bridge restarts and lifecycle rewrites.
        """
        now = time.monotonic()
        stat = self.herald.INBOX_DIR.stat()
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
        if stamp == self.inbox_stamp and now < self.reconcile_at:
            return False
        self.count("inbox_scans")
        changed = False
        writes = False
        with os.scandir(self.herald.INBOX_DIR) as entries:
            for entry in entries:
                if not entry.name.endswith(".json") or not entry.is_file():
                    continue
                if self.db.execute("SELECT 1 FROM inbox_seen WHERE path=?", (entry.name,)).fetchone():
                    continue
                try:
                    # Records predating every active subscription cannot generate
                    # an event: no protocol replay is advertised.
                    if entry.stat().st_mtime < cutoff:
                        item = None
                    else:
                        raw = Path(entry.path).read_bytes()
                        self.count("inbox_files_parsed")
                        self.count("inbox_bytes_read", len(raw))
                        item = json.loads(raw)
                except (OSError, ValueError):
                    continue  # Retry incomplete/corrupt observations on reconciliation.
                if isinstance(item, dict) and item.get("kind") == "message" and item.get("received_ts", 0) >= cutoff:
                    self.db.execute("INSERT OR IGNORE INTO message_catalog VALUES (?,?,?,?,?,?)",
                                    (item["id"], item["received_ts"], item["from"], item.get("to_mailbox", "main"),
                                     item.get("to_agent", ""), item["thread"]))
                    changed = True
                self.db.execute("INSERT OR IGNORE INTO inbox_seen VALUES (?)", (entry.name,))
                writes = True
        if writes:
            self.commit()
        self.inbox_stamp = stamp
        self.reconcile_at = now + 30
        return changed

    def enqueue_events(self, sub, policy, config):
        peers = sorted(set(policy["peers"]) & set(config.get("peers", {})))
        if sub["arguments"].get("peer"):
            peers = [p for p in peers if p == sub["arguments"]["peer"]]
        if not peers:
            return
        used = self.db.execute("SELECT count(*) FROM deliveries WHERE sub=?", (sub["id"],)).fetchone()[0]
        remaining = max(0, policy.get("event_limit", 10) - used)
        if not remaining:
            return
        placeholders = ",".join("?" for _ in peers)
        rows = self.db.execute(
            "SELECT id,received,peer,mailbox,thread FROM message_catalog c WHERE received>=? "
            "AND mailbox=? AND agent IN ('',?) AND peer IN (" + placeholders + ") "
            "AND NOT EXISTS (SELECT 1 FROM deliveries d WHERE d.sub=? AND "
            "json_extract(d.record,'$.event.data.id')=c.id) ORDER BY received,id LIMIT ?",
            (sub["created"], policy["mailbox"], policy["agent"], *peers, sub["id"], remaining)).fetchall()
        for item_id, received, peer, mailbox, thread in rows:
            event_id = "evt_" + hashlib.sha256(canonical([self.owner, item_id]).encode()).hexdigest()
            event = {"eventId": event_id, "name": EVENT, "timestamp": iso(received),
                     "data": {"id": item_id, "peer": peer, "mailbox": mailbox, "thread": thread}, "cursor": None}
            record = {"event": event, "attempts": 0, "next": 0, "state": "pending"}
            self.db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?)", (sub["id"], event_id, canonical(record)))
            self.count("events_created")
        if rows:
            self.commit()

    def tick(self):
        if not self.tick_lock.acquire(blocking=False):
            return
        try:
            self._tick()
        finally:
            self.tick_lock.release()

    def _tick(self):
        self.count("ticks")
        now = time.time()
        with self.lock:
            # No inbox or configuration work when nobody can receive events.
            rows = self.db.execute("SELECT record FROM subscriptions WHERE json_extract(record,'$.enabled')=1 "
                                   "AND json_extract(record,'$.expires')>?", (now,)).fetchall()
            if not rows:
                self.count("idle_ticks")
                return
            try:
                policy, config = self.policy(), self.herald_config()
            except (RPCError, ValueError, OSError):
                return
            subscriptions = [json.loads(row[0]) for row in rows]
            changed = self.scan_new(min(s["created"] for s in subscriptions))
            if changed or self.catalog_dirty:
                for sub in subscriptions:
                    self.enqueue_events(sub, policy, config)
                self.catalog_dirty = False
            candidates = []
            budget = policy.get("callbacks_per_tick", 16)
            for sub in subscriptions:
                if sub["arguments"].get("peer") not in (None, *policy["peers"]):
                    continue
                rows = self.db.execute("SELECT event,record FROM deliveries WHERE sub=? "
                                       "AND json_extract(record,'$.state')='pending' "
                                       "AND json_extract(record,'$.next')<=? LIMIT ?", (sub["id"], now, budget - len(candidates))).fetchall()
                self.count("pending_rows_loaded", len(rows))
                for event_id, raw in rows:
                    record = json.loads(raw)
                    peer = record["event"]["data"]["peer"]
                    if peer not in policy["peers"] or peer not in config.get("peers", {}):
                        continue
                    # Persist a short lease before network I/O. A crash retains the
                    # stable event ID; its retry becomes eligible after the lease.
                    record.update(attempt_token=secrets.token_hex(16), next=now + 30)
                    record["attempts"] += 1
                    updated = self.db.execute("UPDATE deliveries SET record=? WHERE sub=? AND event=? "
                                              "AND json_extract(record,'$.state')='pending' AND json_extract(record,'$.next')<=?",
                                              (canonical(record), sub["id"], event_id, now)).rowcount
                    if updated:
                        candidates.append((sub["id"], event_id, record))
                if len(candidates) >= budget:
                    break
            if candidates:
                self.commit()
        # HTTP callbacks never hold the messaging/SQLite lock.
        for sub_id, event_id, record in candidates:
            with self.lock:
                row = self.db.execute("SELECT record FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
                if not row:
                    continue
                sub = json.loads(row[0])
                try:
                    policy, config = self.policy(), self.herald_config()
                except (RPCError, ValueError, OSError):
                    continue
                peer = record["event"]["data"]["peer"]
                if not sub["enabled"] or sub["expires"] <= time.time() or peer not in policy["peers"] or peer not in config.get("peers", {}):
                    continue
            body = canonical(record["event"]).encode()
            old = sub.get("old_secret") if sub.get("rotation_until", 0) > time.time() else None
            headers = signed_headers(sub["secret"], event_id, body, old)
            headers["X-MCP-Subscription-Id"] = sub_id
            self.count("callback_attempts")
            self.count("callback_request_bytes", len(body))
            if record["attempts"] > 1:
                self.count("callback_retries")
            try:
                status, response = self.transport.post(sub["url"], body, headers, policy["callback_hosts"])
                self.count("callback_response_bytes", len(response))
            except (RPCError, OSError, ValueError, http.client.HTTPException):
                status = 503
            if 200 <= status < 300:
                record["state"] = "delivered"
                self.count("callback_accepted")
            else:
                self.count("callback_failures")
                if status in (410, 413) or record["attempts"] >= 5 or (400 <= status < 500 and status != 429):
                    record["state"] = "failed"
                else:
                    record["next"] = time.time() + min(60, 2 ** record["attempts"])
            with self.lock:
                # Never resurrect an unsubscribed or refreshed-away delivery.
                updated = self.db.execute("UPDATE deliveries SET record=? WHERE sub=? AND event=? "
                                          "AND json_extract(record,'$.attempt_token')=?",
                                          (canonical(record), sub_id, event_id, record["attempt_token"])).rowcount
                if updated and status == 410:
                    current = self.db.execute("SELECT record FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
                    if current:
                        value = json.loads(current[0]); value["enabled"] = False
                        self.db.execute("UPDATE subscriptions SET record=? WHERE id=?", (canonical(value), sub_id))
                if updated:
                    self.commit()
                else:
                    self.db.rollback()

    def rpc(self, request):
        self.policy()
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0" or "id" not in request:
            raise RPCError(-32600, "Expected one JSON-RPC request")
        params = request.get("params", {})
        if not isinstance(params, dict):
            invalid("Expected object params")
        meta = params.get("_meta", {})
        if not isinstance(meta, dict) or meta.get(META_VERSION) != VERSION:
            raise RPCError(-32022, "Unsupported protocol version", {"supportedVersions": [VERSION]})
        if not isinstance(meta.get("io.modelcontextprotocol/clientInfo"), dict) or not isinstance(
                meta.get("io.modelcontextprotocol/clientCapabilities"), dict):
            invalid("Required MCP 2.0 client metadata missing")
        method = request.get("method")
        if method == "server/discover":
            return {"resultType": "complete", "supportedVersions": [VERSION], "capabilities": {"tools": {}, "events": {}},
                    "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "herald-mcp-poc", "version": "0.14.1"}},
                    "ttlMs": 0, "cacheScope": "private"}
        if method == "tools/list":
            return {"resultType": "complete", "tools": TOOLS}
        if method == "tools/call":
            try:
                result = self.tool(params.get("name"), params.get("arguments", {}))
                return {"resultType": "complete", "content": [{"type": "text", "text": canonical(result)}],
                        "structuredContent": result, "isError": False}
            except SystemExit:
                raise RPCError(-32012, "Herald ownership or delivery check rejected the operation")
        if method == "events/list":
            return {"events": [self.definition()]}
        if method == "events/subscribe":
            return self.subscribe(params)
        if method == "events/unsubscribe":
            sub_id, _, _ = self.subscription_identity(params, need_secret=False)
            with self.lock:
                self.db.execute("DELETE FROM subscriptions WHERE id=?", (sub_id,))
                self.db.execute("DELETE FROM deliveries WHERE sub=?", (sub_id,))
                self.commit()
                self.catalog_dirty = True
            return {}
        raise RPCError(-32601, "Method not found")


def make_server(bridge, token, port=0):
    if not token or len(token) < 24:
        raise ValueError("HERALD_MCP_TOKEN must contain at least 24 characters")

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, *args):
            pass  # Never log authorization, callback secrets, or message bodies.

        def respond(self, status, value):
            body = canonical(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.respond(405, {"error": "Use POST /mcp"})

        def do_POST(self):
            if self.path != "/mcp":
                self.respond(404, {"error": "Not found"})
                return
            if self.headers.get("Origin"):
                self.respond(403, {"error": "Browser origins disabled"})
                return
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.respond(401, {"error": "Unauthorized"})
                return
            request = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_BODY:
                    self.respond(413, {"error": "Request size invalid"})
                    return
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or not isinstance(request.get("params", {}), dict):
                    raise RPCError(-32600, "Expected object request and params")
                params = request.get("params", {})
                meta = params.get("_meta", {})
                if not isinstance(meta, dict):
                    raise RPCError(-32602, "Expected object metadata")
                expected = {"MCP-Protocol-Version": meta.get(META_VERSION), "Mcp-Method": request.get("method")}
                if request.get("method") == "tools/call":
                    expected["Mcp-Name"] = params.get("name")
                for name, value in expected.items():
                    actual = self.headers.get(name)
                    if name == "Mcp-Name" and actual and actual.startswith("=?base64?") and actual.endswith("?="):
                        try:
                            actual = base64.b64decode(actual[9:-2], validate=True).decode("utf-8")
                        except (ValueError, UnicodeDecodeError):
                            raise RPCError(-32020, "Malformed mirrored header")
                    if not isinstance(value, str) or actual != value:
                        raise RPCError(-32020, "Missing or mismatched mirrored header")
                result = bridge.rpc(request)
                self.respond(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})
            except RPCError as error:
                value = {"code": error.code, "message": error.message}
                if error.data is not None:
                    value["data"] = error.data
                status = 400 if error.code in (-32020, -32022, -32600) else 404 if error.code == -32601 else 200
                self.respond(status, {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": value})
            except (ValueError, TypeError):
                self.respond(400, {"error": "Invalid request"})
            except Exception:
                self.respond(500, {"error": "Bridge operation failed"})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    import herald
    bridge = Bridge(args.config, herald)
    server = make_server(bridge, os.environ.get("HERALD_MCP_TOKEN"), args.port)
    stop = threading.Event()

    def worker():
        while not stop.wait(1):
            bridge.tick()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print(f"Herald MCP POC listening at http://127.0.0.1:{server.server_port}/mcp", flush=True)
    try:
        server.serve_forever()
    finally:
        stop.set()
        thread.join(timeout=12)
        server.server_close()
        bridge.db.close()


if __name__ == "__main__":
    main()
