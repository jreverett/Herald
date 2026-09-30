"""Isolated real-Herald transport + MCP/webhook acceptance tests; no real accounts."""
import base64
from contextlib import contextmanager
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
if ROOT.name == "tests":
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))
import herald_mcp as mcp


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def environment(values):
    old = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class MCPIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="herald-mcp-test-")
        self.addCleanup(self.temp.cleanup)
        self.tokens = {"jamie": "fixture-jamie-" + "x" * 32, "simon": "fixture-simon-" + "y" * 32}
        self.secret = "whsec_" + base64.b64encode(b"fixture-signing-key-32-bytes!!!!!!!").decode()
        self.envs, self.bridges, self.servers, self.configs = {}, {}, {}, {}
        self.processes = []
        self.callbacks, self.seen = [], set()
        self.challenge_mode = "ok"
        self.statuses = []
        test = self

        class Receiver(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                identifier = self.headers["webhook-id"]
                stamp = self.headers["webhook-timestamp"]
                # Independently verify the exact Standard Webhooks message format.
                signing_input = identifier.encode() + b"." + stamp.encode() + b"." + body
                expected = base64.b64encode(hmac.new(base64.b64decode(test.secret[6:]),
                                                    signing_input, hashlib.sha256).digest()).decode()
                if abs(time.time() - int(stamp)) > 300 or "v1," + expected not in self.headers["webhook-signature"].split():
                    self.send_response(401)
                    self.end_headers()
                    return
                event = json.loads(body)
                test.callbacks.append((event, dict(self.headers), body))
                if event.get("type") == "verification":
                    response = {"challenge": event["challenge"] if test.challenge_mode == "ok" else "wrong"}
                    status = 200
                else:
                    if identifier != event["eventId"]:
                        status = 400
                    else:
                        status = test.statuses.pop(0) if test.statuses else 200
                    if status == 200:
                        test.seen.add(identifier)  # receiver deduplication
                    response = {}
                encoded = json.dumps(response).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        self.receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        self.start_server(self.receiver)
        self.callback_url = f"http://127.0.0.1:{self.receiver.server_port}/callback"
        ports = {name: free_port() for name in self.tokens}
        for owner, peer, agent in (("jamie", "simon", "cody"), ("simon", "jamie", "simon-dot")):
            directory = Path(self.temp.name) / owner
            directory.mkdir()
            peer_agent = "simon-dot" if peer == "simon" else "cody"
            config = {"me": owner, "listen": {"host": "127.0.0.1", "port": ports[owner]},
                      "default_mailbox": "main", "mailboxes": ["main", "dot"],
                      "peers": {peer: {"url": f"http://127.0.0.1:{ports[peer]}",
                                       "token": self.tokens[owner], "issued_token": self.tokens[peer]}}}
            (directory / "config.json").write_text(json.dumps(config))
            values = {"HERALD_DIR": str(directory), "HERALD_AGENT": agent, "HERALD_MAILBOX": "dot"}
            self.envs[owner] = values
            policy = {"owner": owner, "enabled": True, "agent": agent, "mailbox": "dot",
                      "peers": {peer: {"agent": peer_agent, "mailbox": "dot"}}, "callback_hosts": ["127.0.0.1"]}
            policy_path = directory / "bridge.json"
            policy_path.write_text(json.dumps(policy))
            self.configs[owner] = policy_path
            process = subprocess.Popen([sys.executable, str(ROOT / "herald.py"), "daemon"],
                                       env={**os.environ, **values}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.processes.append(process)
            self.addCleanup(self.stop_process, process)
            self.wait_daemon(ports[owner], process)
            with environment(values):
                spec = importlib.util.spec_from_file_location("herald_" + owner, ROOT / "herald.py")
                backend = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(backend)
                bridge = mcp.Bridge(policy_path, backend, mcp.CallbackTransport(test_loopback=True))
            self.bridges[owner] = bridge
            self.addCleanup(lambda b=bridge: b.db.close())
            server = mcp.make_server(bridge, self.tokens[owner])
            self.servers[owner] = server
            self.start_server(server)

    def start_server(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.addCleanup(stop)

    @staticmethod
    def stop_process(process):
        process.terminate()
        try:
            process.wait(3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(3)

    @staticmethod
    def wait_daemon(port, process):
        for _ in range(100):
            if process.poll() is not None:
                raise AssertionError("Isolated daemon failed to start")
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=0.1).close()
                return
            except (OSError, urllib.error.URLError):
                time.sleep(0.02)
        raise AssertionError("Daemon startup timeout")

    def rpc(self, owner, method, params=None, token=None, meta=True, headers=None):
        params = dict(params or {})
        if meta:
            params["_meta"] = {mcp.META_VERSION: mcp.VERSION,
                               "io.modelcontextprotocol/clientInfo": {"name": "fixture", "version": "1"},
                               "io.modelcontextprotocol/clientCapabilities": {}}
        metadata_headers = {"MCP-Protocol-Version": mcp.VERSION, "Mcp-Method": method,
                            "Accept": "application/json, text/event-stream"}
        if method == "tools/call":
            metadata_headers["Mcp-Name"] = params["name"]
        metadata_headers.update(headers or {})
        request = urllib.request.Request(f"http://127.0.0.1:{self.servers[owner].server_port}/mcp",
            data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + (token or self.tokens[owner]), **metadata_headers})
        with environment(self.envs[owner]):
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code in (400, 404):
                    return json.load(error)
                raise

    def tool(self, owner, name, args=None):
        result = self.rpc(owner, "tools/call", {"name": name, "arguments": args or {}})
        if "error" in result:
            return result
        return result["result"]["structuredContent"]

    def subscribe(self, owner="simon", **updates):
        params = {"name": mcp.EVENT, "arguments": {"mailbox": "dot", "peer": "jamie" if owner == "simon" else "simon"},
                  "delivery": {"mode": "webhook", "url": self.callback_url, "secret": self.secret}, "cursor": None}
        params.update(updates)
        return self.rpc(owner, "events/subscribe", params)

    def send(self, key="greeting"):
        return self.tool("jamie", "send_message", {"peer": "simon", "text": "Hi from Cody", "request_id": key})

    def tick(self, owner="simon"):
        with environment(self.envs[owner]):
            self.bridges[owner].tick()

    def test_greeting_signed_event_and_threaded_reply(self):
        discovery = self.rpc("jamie", "server/discover")["result"]
        self.assertEqual(discovery["supportedVersions"], ["2026-07-28"])
        self.assertIn("events", discovery["capabilities"])
        self.assertEqual(len(self.rpc("jamie", "tools/list")["result"]["tools"]), 5)
        self.assertEqual(self.rpc("simon", "events/list")["result"]["events"][0]["name"], mcp.EVENT)
        subscription = self.subscribe()["result"]
        greeting = self.send()
        self.tick()
        event = self.callbacks[-1][0]
        self.assertEqual(event["data"]["id"], greeting["id"])
        self.assertEqual(self.callbacks[-1][1]["X-MCP-Subscription-Id"], subscription["id"])
        self.assertNotIn("text", event["data"])
        read = self.tool("simon", "read_message", {"id": greeting["id"]})
        self.assertEqual(read["text"], "Hi from Cody")
        reply = self.tool("simon", "reply", {"id": greeting["id"], "text": "Hi Cody", "request_id": "reply-1"})
        self.assertEqual(reply["thread"], greeting["thread"])
        self.assertEqual(self.tool("jamie", "read_message", {"id": reply["id"]})["text"], "Hi Cody")
        with environment(self.envs["simon"]):
            self.assertEqual(self.bridges["simon"].item(greeting["id"])["state"], "handled")

    def test_idempotent_send_reply_and_key_conflict(self):
        sent = self.send()
        self.assertEqual(self.send(), sent)
        self.assertEqual(len(self.tool("simon", "list_messages")["messages"]), 1)
        conflict = self.tool("jamie", "send_message", {"peer": "simon", "text": "changed", "request_id": "greeting"})
        self.assertEqual(conflict["error"]["code"], -32602)
        args = {"id": sent["id"], "text": "Hi", "request_id": "reply"}
        self.assertEqual(self.tool("simon", "reply", args), self.tool("simon", "reply", args))
        self.assertEqual(len(self.tool("jamie", "list_messages")["messages"]), 1)

    def test_auth_identity_mailbox_and_peer_boundaries(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.rpc("simon", "tools/list", token=self.tokens["jamie"])
        self.assertEqual(error.exception.code, 401)
        self.assertEqual(self.tool("jamie", "send_message", {"peer": "eve", "text": "Hi", "request_id": "x"})["error"]["code"], -32012)
        self.assertEqual(self.subscribe(arguments={"mailbox": "main"})["error"]["code"], -32012)
        self.assertEqual(self.tool("jamie", "send_message", {"peer": "simon", "text": "Hi", "request_id": "x", "from": "simon"})["error"]["code"], -32602)
        sent = self.send()
        self.assertEqual(self.tool("jamie", "read_message", {"id": sent["id"]})["error"]["code"], -32011)
        self.assertEqual(self.tool("simon", "read_message", {"id": "../../config"})["error"]["code"], -32602)

    def test_read_does_not_claim_or_expose_other_mailbox(self):
        sent = self.send()
        self.tool("simon", "read_message", {"id": sent["id"]})
        bridge = self.bridges["simon"]
        with environment(self.envs["simon"]):
            item = bridge.item(sent["id"])
            self.assertEqual(item["state"], "pending")
            self.assertEqual(item["claimed_by"], "")
            item.update(id="private-fixture", to_mailbox="main", text="private")
            bridge.herald.atomic_write_json(bridge.herald.INBOX_DIR / "private-fixture.json", item)
        self.assertEqual(len(self.tool("simon", "list_messages")["messages"]), 1)
        self.assertEqual(self.tool("simon", "read_message", {"id": "private-fixture"})["error"]["code"], -32011)

    def test_challenge_failure_never_activates_subscription(self):
        self.challenge_mode = "wrong"
        result = self.subscribe()
        self.assertEqual(result["error"]["code"], -32015)
        self.send()
        self.tick()
        self.assertTrue(all(c[0].get("type") == "verification" for c in self.callbacks))

    def test_subscription_refresh_cache_secret_rotation_and_unsubscribe(self):
        first = self.subscribe()["result"]
        self.assertEqual(self.subscribe()["result"]["id"], first["id"])
        self.assertEqual(len(self.callbacks), 1)
        self.secret = "whsec_" + base64.b64encode(b"rotated-signing-key-32-bytes!!!!!").decode()
        self.assertEqual(self.subscribe()["result"]["id"], first["id"])
        self.send()
        self.tick()
        self.assertEqual(len(self.callbacks[-1][1]["webhook-signature"].split()), 2)
        params = {"name": mcp.EVENT, "arguments": {"peer": "jamie", "mailbox": "dot"},
                  "delivery": {"mode": "webhook", "url": self.callback_url}}
        for _ in range(2):
            self.assertEqual(self.rpc("simon", "events/unsubscribe", params)["result"], {})
        count = len(self.callbacks)
        self.send("later")
        self.tick()
        self.assertEqual(len(self.callbacks), count)

    def test_restart_persists_retry_identity_and_write_idempotence(self):
        self.subscribe()
        self.statuses = [503, 200]
        sent = self.send()
        self.tick()
        first_event = self.callbacks[-1][0]
        bridge = self.bridges["simon"]
        bridge.db.close()
        with environment(self.envs["simon"]):
            bridge.__init__(self.configs["simon"], bridge.herald, mcp.CallbackTransport(test_loopback=True))
        row = bridge.db.execute("SELECT sub,event,record FROM deliveries").fetchone()
        rec = json.loads(row[2])
        rec["next"] = 0
        bridge.db.execute("UPDATE deliveries SET record=?", (json.dumps(rec),))
        bridge.db.commit()
        self.tick()
        self.assertEqual(first_event, self.callbacks[-1][0])
        self.assertEqual(len(self.seen), 1)
        before = len(self.callbacks)
        self.tick()
        self.assertEqual(len(self.callbacks), before)
        jamie = self.bridges["jamie"]
        jamie.db.close()
        with environment(self.envs["jamie"]):
            jamie.__init__(self.configs["jamie"], jamie.herald, mcp.CallbackTransport(test_loopback=True))
        self.assertEqual(sent, self.send())

    def test_expiration_revocation_and_no_backlog_replay(self):
        self.send("before-subscription")
        self.subscribe()
        self.tick()
        self.assertEqual(len(self.callbacks), 1)
        self.send("after-subscription")
        policy = json.loads(self.configs["simon"].read_text())
        policy["enabled"] = False
        self.configs["simon"].write_text(json.dumps(policy))
        self.tick()
        self.assertEqual(len(self.callbacks), 1)
        self.assertEqual(self.rpc("simon", "events/list")["error"]["code"], -32012)
        policy["enabled"] = True
        self.configs["simon"].write_text(json.dumps(policy))
        bridge = self.bridges["simon"]
        row = bridge.db.execute("SELECT id,record FROM subscriptions").fetchone()
        rec = json.loads(row[1]); rec["expires"] = 0
        bridge.db.execute("UPDATE subscriptions SET record=? WHERE id=?", (json.dumps(rec), row[0]))
        bridge.db.commit()
        self.tick()
        self.assertEqual(len(self.callbacks), 1)

    def test_410_and_413_are_not_retried(self):
        for status in (410, 413):
            self.subscribe()
            self.statuses = [status]
            self.send(str(status))
            self.tick()
            before = len(self.callbacks)
            self.tick()
            self.assertEqual(len(self.callbacks), before)

    def test_secret_cursor_ttl_and_protocol_validation(self):
        bad = {"mode": "webhook", "url": self.callback_url, "secret": "whsec_" + base64.b64encode(b"short").decode()}
        self.assertEqual(self.subscribe(delivery=bad)["error"]["code"], -32602)
        self.assertEqual(self.subscribe(cursor="invented")["error"]["code"], -32602)
        self.assertEqual(self.subscribe(ttlMs=-1)["error"]["code"], -32602)
        self.assertEqual(self.rpc("jamie", "server/discover", meta=False)["error"]["code"], -32020)

    def test_mcp2_http_header_validation(self):
        wrong = self.rpc("jamie", "tools/list", headers={"Mcp-Method": "events/list"})
        self.assertEqual(wrong["error"]["code"], -32020)
        wrong_name = self.rpc("jamie", "tools/call", {"name": "list_messages", "arguments": {}},
                              headers={"Mcp-Name": "read_message"})
        self.assertEqual(wrong_name["error"]["code"], -32020)
        encoded = "=?base64?" + base64.b64encode(b"list_messages").decode() + "?="
        correct = self.rpc("jamie", "tools/call", {"name": "list_messages", "arguments": {}}, headers={"Mcp-Name": encoded})
        self.assertIn("result", correct)

    def test_uncertain_send_response_reuses_herald_delivery_id(self):
        sent = self.send()
        bridge = self.bridges["jamie"]
        bridge.db.execute("UPDATE writes SET result=NULL")
        bridge.db.commit()
        retried = self.send()
        self.assertEqual(retried["id"], sent["id"])
        self.assertTrue(retried["duplicate"])
        self.assertEqual(len(self.tool("simon", "list_messages")["messages"]), 1)

    def test_lost_webhook_ack_retries_without_duplicate_receiver_effect(self):
        self.subscribe()
        self.send()
        self.tick()
        bridge = self.bridges["simon"]
        row = bridge.db.execute("SELECT sub,event,record FROM deliveries").fetchone()
        rec = json.loads(row[2])
        rec.update(state="pending", next=0)
        bridge.db.execute("UPDATE deliveries SET record=?", (json.dumps(rec),))
        bridge.db.commit()
        self.tick()
        self.assertEqual(self.callbacks[-1][0], self.callbacks[-2][0])
        self.assertEqual(len(self.seen), 1)

    def test_peer_revocation_stops_delivery_and_read_access(self):
        self.subscribe()
        sent = self.send()
        policy = json.loads(self.configs["simon"].read_text())
        policy["peers"] = {}
        self.configs["simon"].write_text(json.dumps(policy))
        self.tick()
        self.assertEqual(len(self.callbacks), 1)
        self.assertEqual(self.tool("simon", "read_message", {"id": sent["id"]})["error"]["code"], -32011)

    def test_expired_refresh_starts_new_no_replay_window(self):
        self.subscribe()
        bridge = self.bridges["simon"]
        row = bridge.db.execute("SELECT id,record FROM subscriptions").fetchone()
        rec = json.loads(row[1]); rec["expires"] = 0
        bridge.db.execute("UPDATE subscriptions SET record=? WHERE id=?", (json.dumps(rec), row[0]))
        bridge.db.commit()
        self.send("during-expiration")
        self.subscribe()
        self.tick()
        self.assertEqual(len(self.callbacks), 1)
        self.send("after-refresh")
        self.tick()
        self.assertEqual(len(self.seen), 1)

    def test_herald_peer_removal_also_blocks_pending_retry(self):
        self.subscribe()
        self.statuses = [503]
        self.send()
        self.tick()
        bridge = self.bridges["simon"]
        config_path = bridge.herald.CONFIG_PATH
        config = json.loads(config_path.read_text())
        config["peers"] = {}
        config_path.write_text(json.dumps(config))
        row = bridge.db.execute("SELECT record FROM deliveries").fetchone()
        rec = json.loads(row[0]); rec["next"] = 0
        bridge.db.execute("UPDATE deliveries SET record=?", (json.dumps(rec),))
        bridge.db.commit()
        before = len(self.callbacks)
        self.tick()
        self.assertEqual(len(self.callbacks), before)
        self.assertEqual(self.rpc("simon", "events/list")["result"]["events"][0]["inputSchema"]["properties"]["peer"]["enum"], [])


class SigningAndSSRF(unittest.TestCase):
    def test_standard_webhooks_known_vector(self):
        secret = "whsec_" + base64.b64encode(bytes(range(32))).decode()
        headers = mcp.signed_headers(secret, "evt_1", b'{"data":{}}', timestamp=123)
        expected = base64.b64encode(hmac.new(bytes(range(32)), b'evt_1.123.{"data":{}}', hashlib.sha256).digest()).decode()
        self.assertEqual(headers["webhook-signature"], "v1," + expected)

    def test_production_rejects_http_credentials_fragments_and_private_dns(self):
        transport = mcp.CallbackTransport()
        for url in ("http://example.com/cb", "https://user:pass@example.com/cb", "https://example.com/cb#fragment"):
            with self.assertRaises(mcp.RPCError):
                transport.post(url, b"{}", {}, ["example.com"])
        with patch("herald_mcp.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaises(mcp.RPCError):
                transport.post("https://example.com/cb", b"{}", {}, ["example.com"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
