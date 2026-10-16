"""Shared-address routing to per-owner bridges, with stand-in bridges and unsigned dummy tokens."""
import base64
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
import herald_oauth as oauth
import herald_route as route


def token(sub):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).rstrip(b"=").decode()
    return f"Bearer e30.{payload}.sig"


def bridge(name):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.dumps({"owner": name, "method": self.command, "path": self.path,
                               "mcp_method": self.headers.get("Mcp-Method"), "origin": self.headers.get("Origin"),
                               "body": self.rfile.read(length).decode()}).encode()
            self.send_response(401 if self.headers.get("Mcp-Method") == "challenge" else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("WWW-Authenticate", f'Bearer realm="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Router(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.bridges = {name: bridge(name) for name in ("jamie", "simon")}
        for server in self.bridges.values():
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.routes = Path(self.temp.name) / "routes.json"
        self.write_routes({"jamie": ["google-oauth2|jamie"], "simon": ["google-oauth2|simon"]})
        self.router = route.make_server(route.Routes(self.routes), port=0)
        threading.Thread(target=self.router.serve_forever, daemon=True).start()
        self.addCleanup(self.router.server_close)
        self.addCleanup(self.router.shutdown)

    def write_routes(self, subjects):
        self.routes.write_text(json.dumps({"default": "jamie", "owners": {
            name: {"port": self.bridges[name].server_port, "subjects": subs} for name, subs in subjects.items()}}))

    def call(self, path="/mcp", auth=None, mcp_method="tools/call", body=b'{"jsonrpc":"2.0"}', method="POST"):
        headers = {"Content-Type": "application/json", "Mcp-Method": mcp_method}
        if auth:
            headers["Authorization"] = auth
        request = urllib.request.Request(f"http://127.0.0.1:{self.router.server_port}{path}",
                                         data=body if method == "POST" else None, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, dict(response.headers), json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), json.loads(error.read())

    def test_each_subject_reaches_its_own_bridge_with_body_and_headers_intact(self):
        with patch("sys.stdout", new_callable=io.StringIO):
            jamie = self.call(auth=token("google-oauth2|jamie"))[2]
            simon = self.call(auth=token("google-oauth2|simon"))[2]

        self.assertEqual((jamie["owner"], simon["owner"]), ("jamie", "simon"))
        self.assertEqual((simon["method"], simon["path"], simon["mcp_method"], simon["body"]),
                         ("POST", "/mcp", "tools/call", '{"jsonrpc":"2.0"}'))
        self.assertIsNone(simon["origin"])

    def test_unknown_missing_or_malformed_tokens_go_to_the_default_bridge(self):
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            owners = [self.call(auth=a)[2]["owner"] for a in (token("google-oauth2|stranger"), None, "Bearer not-a-jwt")]
            metadata = self.call(path="/.well-known/oauth-protected-resource/mcp", method="GET")[2]

        self.assertEqual(owners, ["jamie"] * 3)
        self.assertEqual(metadata["owner"], "jamie")
        self.assertIn("matched=default", out.getvalue())
        self.assertNotIn("stranger", out.getvalue())

    def test_bridge_status_and_auth_challenge_are_returned_unchanged(self):
        with patch("sys.stdout", new_callable=io.StringIO):
            status, headers, body = self.call(auth=token("google-oauth2|simon"), mcp_method="challenge")

        self.assertEqual((status, body["owner"]), (401, "simon"))
        self.assertEqual(headers["WWW-Authenticate"], 'Bearer realm="simon"')

    def test_routes_reload_when_the_file_changes(self):
        with patch("sys.stdout", new_callable=io.StringIO):
            before = self.call(auth=token("google-oauth2|new"))[2]["owner"]
            time.sleep(0.01)
            self.write_routes({"jamie": ["google-oauth2|jamie"], "simon": ["google-oauth2|simon", "google-oauth2|new"]})
            after = self.call(auth=token("google-oauth2|new"))[2]["owner"]

        self.assertEqual((before, after), ("jamie", "simon"))

    def test_unavailable_bridge_gives_502(self):
        self.bridges["simon"].shutdown()
        self.bridges["simon"].server_close()

        with patch("sys.stdout", new_callable=io.StringIO):
            status = self.call(auth=token("google-oauth2|simon"))[0]

        self.assertEqual(status, 502)

    def test_add_subject_moves_an_account_between_owners(self):
        route.add_subject(self.routes, "simon", "google-oauth2|jamie")

        owners = json.loads(self.routes.read_text())["owners"]
        self.assertEqual((owners["jamie"]["subjects"], owners["simon"]["subjects"]),
                         ([], ["google-oauth2|jamie", "google-oauth2|simon"]))
        with self.assertRaises(ValueError):
            route.add_subject(self.routes, "nobody", "x")


class ApproveForOwner(unittest.TestCase):
    def test_candidate_from_the_admin_bridge_is_approved_into_the_target_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            admin, target = Path(directory) / "admin.json", Path(directory) / "simon.json"
            for path, subject in ((admin, "google-oauth2|jamie"), (target, "google-oauth2|old")):
                path.write_text(json.dumps({"owner": path.stem, "enabled": True, "issuer": "https://i.example/",
                                            "resource": "https://r.example/mcp", "jwks_uri": "https://i.example/k",
                                            "subject": subject}))
            oauth.write_json(oauth.pending_path(admin), {"candidates": [
                {"subject": "google-oauth2|simon", "email": "simon@example.test", "last_seen": 1, "count": 1}]}, 0o600)

            oauth.approve_candidate(admin, 1, target_path=target)

            self.assertEqual(oauth.list_accounts(target)[0], ["google-oauth2|old", "google-oauth2|simon"])
            self.assertEqual(oauth.list_accounts(admin), (["google-oauth2|jamie"], []))


if __name__ == "__main__":
    unittest.main()
