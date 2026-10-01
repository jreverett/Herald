"""Dummy signed-token tests: no identity-provider accounts or network access."""
import base64
import json
import shutil
import subprocess
import tempfile
import time
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import patch

import herald_oauth as oauth
import herald_mcp as mcp
import test_mcp as fixtures


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class DummyKeys:
    def __init__(self, directory):
        self.private = Path(directory)/"dummy-private.pem"
        subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(self.private)],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        modulus = subprocess.check_output(["openssl", "rsa", "-in", str(self.private), "-noout", "-modulus"], stderr=subprocess.DEVNULL)
        number = int(modulus.decode().strip().split("=")[1], 16)
        self.key = {"kid": "dummy-1", "kty": "RSA", "alg": "RS256", "use": "sig",
                    "n": encoded(number.to_bytes((number.bit_length()+7)//8, "big")), "e": encoded(b"\1\0\1")}
        self.fetches = 0

    def fetch(self, url):
        self.fetches += 1
        return {"keys": [self.key]}

    def token(self, config, changes=None, header=None):
        now = int(time.time())
        claims = {"iss": config["issuer"], "aud": config["resource"], "sub": config["subject"],
                  "iat": now, "exp": now+600, "scope": " ".join(oauth.SCOPES)}
        claims.update(changes or {})
        parts = [encoded(json.dumps(header or {"alg": "RS256", "kid": "dummy-1"}).encode()), encoded(json.dumps(claims).encode())]
        message = ".".join(parts).encode()
        signature = subprocess.check_output(["openssl", "dgst", "-sha256", "-sign", str(self.private)], input=message)
        return message.decode()+"."+encoded(signature)


def config(owner="jamie"):
    return {"owner": owner, "enabled": True, "issuer": "https://dummy-idp.example/",
            "resource": "https://dummy-mcp.example/mcp/"+owner,
            "jwks_uri": "https://dummy-idp.example/.well-known/jwks.json", "subject": "dummy|"+owner}


class ResourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.keys = DummyKeys(self.tmp.name)
        self.config = config()
        self.path = Path(self.tmp.name)/"oauth.json"
        self.path.write_text(json.dumps(self.config))
        self.auth = oauth.ResourceAuth(self.path, "jamie", self.keys.fetch)

    def verify(self, changes=None, header=None, scope="herald:read"):
        return self.auth.authenticate("Bearer "+self.keys.token(self.config, changes, header), scope)

    def test_valid_signature_and_stable_identity_do_not_use_email(self):
        expires = int(time.time()) + 600
        a = self.verify({"email": "one@example.test", "exp": expires})
        b = self.verify({"email": "other@example.test", "exp": expires})
        self.assertEqual(a,b)
        self.assertEqual(a["subject"], "dummy|jamie")
        self.assertEqual(self.keys.fetches,1)

    def test_invalid_issuer_audience_subject_and_numeric_claims_fail(self):
        for changes in ({"iss":"https://evil.example/"}, {"aud":"wrong"}, {"sub":"dummy|simon","email":"jamie@example.test"},
                        {"exp":0}, {"exp":True}, {"iat":True}, {"iat":time.time()+100}, {"nbf":time.time()+100},
                        {"nbf":True}, {"exp":float("nan")}, {"scope": ["herald:read"]}):
            with self.subTest(changes=changes), self.assertRaises(oauth.AuthError):
                self.verify(changes)

    def test_algorithm_confusion_unknown_key_and_tampering_fail(self):
        for header in ({"alg":"none","kid":"dummy-1"}, {"alg":"HS256","kid":"dummy-1"},
                       {"alg":"RS256","kid":"other","jku":"https://evil.example/jwks"},
                       {"alg":"RS256","kid":"dummy-1","crit":["unknown"]}):
            with self.subTest(header=header), self.assertRaises(oauth.AuthError):
                self.verify(header=header)
        token = self.keys.token(self.config)
        parts = token.split("."); parts[1] = encoded(b'{"sub":"dummy|jamie"}')
        with self.assertRaises(oauth.AuthError):
            self.auth.authenticate("Bearer "+".".join(parts), "herald:read")

    def test_scope_enforced_on_cached_token_and_expiry(self):
        token = "Bearer "+self.keys.token(self.config,{"scope":"herald:read"})
        self.auth.authenticate(token,"herald:read")
        with self.assertRaises(oauth.AuthError) as error:
            self.auth.authenticate(token,"herald:write")
        self.assertEqual(error.exception.code,"insufficient_scope")
        with patch.object(oauth.time,"time",return_value=time.time()+1000), self.assertRaises(oauth.AuthError):
            self.auth.authenticate(token,"herald:read")

    def test_connection_disable_revokes_cached_token_and_grant(self):
        token = "Bearer "+self.keys.token(self.config)
        grant = self.auth.authenticate(token,"herald:events")
        self.assertTrue(self.auth.active_grant(grant))
        self.path.write_text(json.dumps({**self.config,"enabled":False}))
        self.assertFalse(self.auth.active_grant(grant))
        with self.assertRaises(oauth.AuthError): self.auth.authenticate(token,"herald:read")

    def test_token_cache_and_untrusted_key_refresh_are_bounded(self):
        token="Bearer "+self.keys.token(self.config)
        with patch.object(oauth.subprocess,"run",wraps=subprocess.run) as verify:
            for _ in range(20): self.auth.authenticate(token,"herald:read")
            self.assertEqual(verify.call_count,1)
        for _ in range(20):
            with self.assertRaises(oauth.AuthError): self.verify(header={"alg":"RS256","kid":"missing"})
        self.assertEqual(self.keys.fetches,1)
        self.assertLessEqual(len(self.auth.cache),128)

    def test_missing_verifier_and_malformed_provider_response_fail_closed(self):
        with patch.object(oauth.shutil,"which",return_value=None), self.assertRaises(ValueError):
            oauth.ResourceAuth(self.path,"jamie",self.keys.fetch)
        for document in ({"keys":[]},{"keys":[self.keys.key,self.keys.key]}, {"keys":[{**self.keys.key,"n":"bad"}]}):
            auth=oauth.ResourceAuth(self.path,"jamie",lambda url:document)
            with self.assertRaises(oauth.AuthError):
                auth.authenticate("Bearer "+self.keys.token(self.config),"herald:read")

    def test_disabled_or_wrong_owner_and_insecure_configuration(self):
        for changes in ({"owner":"simon"}, {"resource":"http://localhost/mcp"}, {"jwks_uri":"https://other.example/keys"}):
            self.path.write_text(json.dumps({**self.config,**changes}))
            with self.assertRaises(ValueError): oauth.ResourceAuth(self.path,"jamie",self.keys.fetch)


class OAuthHTTPTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.MCPIntegration("test_greeting_signed_event_and_threaded_reply")
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.keys=DummyKeys(self.f.temp.name)
        self.auths,self.configs,self.paths={}, {}, {}
        for owner in ("jamie","simon"):
            c=config(owner); path=Path(self.f.temp.name)/(owner+"-oauth.json")
            path.write_text(json.dumps(c))
            auth=oauth.ResourceAuth(path,owner,self.keys.fetch)
            server=mcp.make_server(self.f.bridges[owner],auth=auth)
            self.f.start_server(server); self.f.servers[owner]=server
            self.f.tokens[owner]=self.keys.token(c)
            self.auths[owner],self.configs[owner],self.paths[owner]=auth,c,path

    def test_public_metadata_discovery_and_tool_authentication_challenge(self):
        server=self.f.servers["jamie"]
        with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}"+self.auths["jamie"].metadata_path()) as response:
            metadata=json.load(response)
        self.assertEqual(metadata["resource"],self.configs["jamie"]["resource"])
        tools=self.f.rpc("jamie","tools/list",token="not-a-token")["result"]["tools"]
        self.assertEqual(len(tools),5)
        self.assertTrue(all(t["securitySchemes"][0]["type"]=="oauth2" for t in tools))
        response=self.f.rpc("jamie","tools/call",{"name":"list_messages","arguments":{}},token="bad")
        self.assertTrue(response["result"]["isError"])
        self.assertIn("resource_metadata",response["result"]["_meta"]["mcp/www_authenticate"][0])
        self.assertEqual(self.f.bridges["jamie"].counts["tool_calls"],0)

    def test_wrong_owner_and_read_only_scope_cannot_send(self):
        args={"name":"send_message","arguments":{"peer":"simon","text":"Hi","request_id":"oauth-reject"}}
        wrong=self.f.rpc("jamie","tools/call",args,token=self.f.tokens["simon"])
        self.assertTrue(wrong["result"]["isError"])
        token=self.keys.token(self.configs["jamie"],{"scope":"herald:read"})
        denied=self.f.rpc("jamie","tools/call",args,token=token)
        self.assertIn("insufficient_scope",denied["result"]["_meta"]["mcp/www_authenticate"][0])
        self.assertEqual(self.f.bridges["jamie"].counts["send_calls"],0)

    def test_two_oauth_owners_greeting_signed_event_and_threaded_reply(self):
        # Reuse the independent signed receiver and real two-owner Herald transport.
        self.f.test_greeting_signed_event_and_threaded_reply()
        self.assertEqual(self.f.bridges["jamie"].counts["send_calls"],1)

    def test_event_scope_connection_disable_and_expiry_stop_callbacks(self):
        params={"name":mcp.EVENT,"arguments":{"mailbox":"dot","peer":"jamie"},
                "delivery":{"mode":"webhook","url":self.f.callback_url,"secret":self.f.secret}}
        token=self.keys.token(self.configs["simon"],{"scope":"herald:read"})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.f.rpc("simon","events/subscribe",params,token=token)
        self.assertEqual(error.exception.code,403)
        reply=self.f.rpc("simon","events/subscribe",params)
        self.assertIn("id",reply["result"])
        with fixtures.environment(self.f.envs["jamie"]):
            self.f.bridges["jamie"].tool("send_message",{"peer":"simon","text":"test","request_id":"disable-grant"})
        self.paths["simon"].write_text(json.dumps({**self.configs["simon"],"enabled":False}))
        before=self.f.bridges["simon"].counts["callback_attempts"]
        self.f.tick("simon")
        self.assertEqual(self.f.bridges["simon"].counts["callback_attempts"],before)
        row=self.f.bridges["simon"].db.execute("SELECT record FROM subscriptions").fetchone()
        record=json.loads(row[0])
        self.assertEqual(record["grant"]["subject"],"dummy|simon")
        self.assertLessEqual(record["expires"],record["grant"]["expires"])

    def test_idle_oauth_subscription_never_fetches_keys_or_verifies_tokens(self):
        self.f.subscribe()
        self.f.tick()
        fetches=self.keys.fetches
        before=self.auths["simon"].stats()
        with patch.object(oauth.subprocess,"run",wraps=subprocess.run) as verify:
            for _ in range(50): self.f.tick()
            self.assertEqual(verify.call_count,0)
        self.assertEqual(self.keys.fetches,fetches)
        self.assertEqual(self.auths["simon"].stats(),before)
        self.assertEqual(self.f.bridges["simon"].counts["callback_attempts"],0)
        stats=self.f.tool("simon","usage_stats")
        self.assertEqual(stats["oauth_counters"]["jwks_fetches"],1)
        self.assertEqual(stats["oauth_counters"]["signature_verifications"],1)

    def test_expired_grant_prevents_delivery_and_refresh_cannot_revive_without_token(self):
        self.f.subscribe()
        bridge=self.f.bridges["simon"]
        row=bridge.db.execute("SELECT id,record FROM subscriptions").fetchone()
        record=json.loads(row[1]); record["grant"]["expires"]=time.time()-1
        # Leave outer TTL future to verify the grant check independently.
        bridge.db.execute("UPDATE subscriptions SET record=? WHERE id=?",(json.dumps(record),row[0])); bridge.db.commit()
        self.f.send("expired-grant")
        self.f.tick()
        self.assertEqual(bridge.counts["callback_attempts"],0)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.f.rpc("simon","events/subscribe",{"name":mcp.EVENT,"arguments":{"mailbox":"dot","peer":"jamie"},
                "delivery":{"mode":"webhook","url":self.f.callback_url,"secret":self.f.secret}},
                token=self.keys.token(self.configs["simon"],{"exp":1}))
        self.assertEqual(error.exception.code,401)

    def test_empty_peer_event_discovery_and_bounded_request_counters(self):
        path=self.f.configs["jamie"]
        policy=json.loads(path.read_text()); policy.update(peers={},event_limit=0)
        path.write_text(json.dumps(policy))
        bridge=self.f.bridges["jamie"]
        discovered=self.f.rpc("jamie","server/discover",token="not-a-token")
        self.assertIn("events",discovered["result"]["capabilities"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.f.rpc("jamie","events/list",token="not-a-token")
        self.assertEqual(error.exception.code,401)
        events=self.f.rpc("jamie","events/list")["result"]["events"]
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]["name"],mcp.EVENT)
        self.assertNotIn("peer",events[0]["inputSchema"]["properties"])
        self.assertEqual(bridge.counts["server_discovery_requests"],1)
        self.assertEqual(bridge.counts["event_discovery_requests"],2)
        self.assertEqual(bridge.counts["event_discovery_auth_denials"],1)
        self.assertEqual(bridge.counts["event_discovery_results"],1)
        self.assertEqual(bridge.counts["callback_attempts"],0)
        with self.assertRaises(mcp.RPCError):
            bridge.subscription_identity({"name":mcp.EVENT,"arguments":{"mailbox":"dot","peer":"simon"},
                "delivery":{"mode":"webhook","url":self.f.callback_url,"secret":self.f.secret}})


if __name__=="__main__": unittest.main()
