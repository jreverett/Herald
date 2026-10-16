"""Route one public Herald MCP address to per-owner bridges by the token subject (stdlib only).

Each owner keeps their own bridge process and store. The router only reads the
unverified `sub` claim to choose a bridge; that bridge still verifies the token
fully, so a forged token sent to the wrong owner is refused there.
"""
import argparse
import base64
import http.client
import json
import os
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_ROUTES = "/etc/herald/routes.json"
MAX_BODY = 300 * 1024
FORWARDED_HEADERS = ("Authorization", "Content-Type", "Accept", "Mcp-Method", "Mcp-Name", "MCP-Protocol-Version")
HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authenticate",
              "proxy-authorization", "content-length", "server", "date"}


class Routes:
    def __init__(self, path):
        self.path, self.stamp, self.value = Path(path), None, None
        self.lock = threading.Lock()

    def table(self):
        with self.lock:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
            if stamp != self.stamp:
                value = json.loads(self.path.read_text())
                owners = value.get("owners")
                if not isinstance(owners, dict) or value.get("default") not in owners:
                    raise ValueError("routes need owners and a default owner")
                for name, owner in owners.items():
                    if not isinstance(owner.get("port"), int) or not isinstance(owner.get("subjects", []), list):
                        raise ValueError(f"route for {name} needs a port and a subject list")
                self.value, self.stamp = value, stamp
            return self.value

    def target(self, authorization):
        table = self.table()
        subject = token_subject(authorization)
        for name, owner in table["owners"].items():
            if subject and subject in owner.get("subjects", []):
                return name, owner["port"], True
        return table["default"], table["owners"][table["default"]]["port"], False


def token_subject(authorization):
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        return None
    parts = authorization[7:].split(".")
    if len(parts) != 3 or len(parts[1]) > 16384:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    subject = claims.get("sub") if isinstance(claims, dict) else None
    return subject if isinstance(subject, str) else None


def add_subject(routes_path, owner, subject):
    path = Path(routes_path)
    value = json.loads(path.read_text())
    if owner not in value.get("owners", {}):
        raise ValueError(f"No route for owner {owner}")
    for other in value["owners"].values():
        other["subjects"] = [s for s in other.get("subjects", []) if s != subject]
    value["owners"][owner]["subjects"] = sorted(set(value["owners"][owner]["subjects"]) | {subject})
    stat = path.stat()
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    os.chmod(temporary, stat.st_mode & 0o777)
    os.replace(temporary, path)
    return value["owners"][owner]


def make_server(routes, port=8770):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass  # Never log authorization headers, paths or bodies.

        def label(self):
            if self.path == "/mcp":
                return "mcp"
            return "metadata" if self.path.startswith("/.well-known/oauth-protected-resource") else "other"

        def finish_with(self, status, headers, body, owner, matched):
            self.send_response(status)
            for key, value in headers:
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
            print(f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')} route owner={owner} "
                  f"matched={'yes' if matched else 'default'} http={self.command} path={self.label()} status={status}",
                  flush=True)

        def forward(self):
            owner, matched = "-", False
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > MAX_BODY:
                    self.finish_with(413, [("Content-Type", "application/json")], b'{"error":"Request size invalid"}', owner, matched)
                    return
                body = self.rfile.read(length) if length else None
                owner, port, matched = routes.target(self.headers.get("Authorization"))
                headers = {name: self.headers[name] for name in FORWARDED_HEADERS if self.headers.get(name) is not None}
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
                try:
                    connection.request(self.command, self.path, body=body, headers=headers)
                    response = connection.getresponse()
                    data = response.read()
                    kept = [(k, v) for k, v in response.getheaders() if k.lower() not in HOP_BY_HOP]
                    self.finish_with(response.status, kept, data, owner, matched)
                finally:
                    connection.close()
            except (OSError, http.client.HTTPException, ValueError):
                self.finish_with(502, [("Content-Type", "application/json")], b'{"error":"Bridge unavailable"}', owner, matched)

        do_GET = do_POST = do_DELETE = do_OPTIONS = do_HEAD = forward

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes", default=DEFAULT_ROUTES)
    parser.add_argument("--port", type=int, default=8770)
    args = parser.parse_args()
    routes = Routes(args.routes)
    routes.table()
    server = make_server(routes, args.port)
    print(f"Herald MCP router listening at http://127.0.0.1:{server.server_port}/mcp", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
