#!/usr/bin/env python3
"""herald - peer-to-peer agent-to-agent messaging.

Agent sessions (Claude Code, Codex CLI) on different machines hold threaded
conversations: messages, task requests with a lifecycle (pending -> working ->
done/failed), results with attached files, and structured metadata. Each
machine runs `herald daemon` (reachable over Tailscale); `herald wait` blocks until
delivery so a session gets woken instead of polling. A person can run many
agent sessions at once: the first session to `herald read` an item claims it.
Session-sensitive commands require HERALD_AGENT. See skill/SKILL.md for the
protocol agents follow.

Config in ~/.herald/config.json:
{
  "me": "alice",
  "listen": {"host": "auto", "port": 8765},   // auto = Tailscale IP only
  "peers": {
    "bob": {
      "url": "http://100.x.y.z:8765",       // how I reach bob
      "token": "<token bob issued me>",      // I present this to reach bob
      "issued_token": "<token I issued bob>" // bob presents this to reach me; it authenticates bob
    }
  }
}
Each peer has its own inbound token, so the sender of every item is authenticated
(the payload's claimed identity is ignored).

Stdlib only. Received tasks are never auto-executed by this tool; the
receiving agent triages them (see AGENTS.md).
"""

import argparse
import atexit
import base64
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.7.4"

HERALD_DIR = Path(os.environ.get("HERALD_DIR", Path.home() / ".herald"))
CONFIG_PATH = HERALD_DIR / "config.json"
INBOX_DIR = HERALD_DIR / "inbox"
OUTBOX_DIR = HERALD_DIR / "outbox"
FILES_DIR = HERALD_DIR / "files"
QUEUE_DIR = HERALD_DIR / "queue"
ACTIVITY_DIR = HERALD_DIR / "activity"
SESSIONS_DIR = HERALD_DIR / "sessions"
STATUS_PATH = HERALD_DIR / "status.json"
MAX_FILE_BYTES = 100 * 1024 * 1024
KINDS = ("message", "task", "result")
STATUSES = ("accepted", "working", "done", "failed")
FALLBACKS = ("broadcast", "hold", "bounce")
RETRY_INTERVAL = 45
HEARTBEAT_INTERVAL = 5
SESSION_LEASE = 75          # a session with no heartbeat in this long is treated as gone
TARGET_GIVEUP = 300         # release a targeted item whose target never reappears after this
SUSPEND_GAP = HEARTBEAT_INTERVAL * 6   # a maintenance tick later than this means the host slept


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"No config at {CONFIG_PATH}. Run: herald init --me NAME")
    return json.loads(CONFIG_PATH.read_text())


def ensure_dirs():
    for d in (INBOX_DIR, OUTBOX_DIR, FILES_DIR, QUEUE_DIR, ACTIVITY_DIR, SESSIONS_DIR):
        d.mkdir(parents=True, exist_ok=True)


def touch_activity(kind):
    """Record an outgoing ('send') or incoming ('recv') event for the tray."""
    try:
        ensure_dirs()
        (ACTIVITY_DIR / kind).write_text(str(time.time()))
    except OSError:
        pass


def write_status(fields):
    try:
        ensure_dirs()
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(fields))
        tmp.replace(STATUS_PATH)
    except OSError:
        pass


def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def agent_name():
    return os.environ.get("HERALD_AGENT", socket.gethostname())


def sender_agent():
    """Agent name stamped on outbound items so replies can target this session.
    Only an explicitly-set HERALD_AGENT is used; unset stays blank so replies
    broadcast, never the hostname default (a hostname is a machine, not a
    listening session, so targeting it is guaranteed-undeliverable)."""
    return os.environ.get("HERALD_AGENT", "")


def is_pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_sessions():
    out = {}
    if not SESSIONS_DIR.exists():
        return out
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            s = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out[s.get("agent", p.stem)] = s
    return out


def session_alive(agent, sessions=None):
    """A session is live if it heartbeated within the lease. Same-machine
    sessions also fail fast if their pid is gone (reboot-safe: pid absent)."""
    if sessions is None:
        sessions = read_sessions()
    s = sessions.get(agent)
    if not s:
        return False
    if s.get("host") == socket.gethostname() and isinstance(s.get("pid"), int):
        if not is_pid_alive(s["pid"]):
            return False
    return (time.time() - s.get("heartbeat", 0)) <= SESSION_LEASE


def write_session(started, waiting_on="inbox"):
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        me = agent_name()
        (SESSIONS_DIR / f"{sanitize_filename(me)}.json").write_text(json.dumps({
            "agent": me, "pid": os.getpid(), "host": socket.gethostname(),
            "started": started, "heartbeat": time.time(), "waiting_on": waiting_on}))
    except OSError:
        pass


def clear_session():
    try:
        (SESSIONS_DIR / f"{sanitize_filename(agent_name())}.json").unlink()
    except OSError:
        pass


def sanitize_filename(name):
    name = os.path.basename(name.replace("\\", "/"))
    return name.replace("..", "_") or "unnamed"


# ---------------- daemon (receiver) ----------------

class Handler(BaseHTTPRequestHandler):
    notify_command = None
    me = None
    peer_by_token = {}   # per-peer inbound token -> peer name, for authenticated identity

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/ping":
            self._json(200, {"ok": True, "version": __version__, "me": self.me})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/send":
            self._json(404, {"error": "not found"})
            return
        auth = self.headers.get("Authorization", "")
        tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        sender = self.peer_by_token.get(tok)   # each peer has its own inbound token; the token is the identity
        if sender is None:
            self._json(401, {"error": "bad token"})
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_FILE_BYTES * 1.4:
            self._json(413, {"error": "too large"})
            return
        try:
            item = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "bad json"})
            return
        if item.get("kind") not in KINDS:
            self._json(400, {"error": f"kind must be one of {KINDS}"})
            return

        item_id = new_id()
        stored = {
            "id": item_id,
            "thread": str(item.get("thread") or item_id)[:64],
            "reply_to": str(item.get("reply_to", ""))[:64],
            "from": sender,   # authoritative: the token proves who sent it, not the payload

            "from_agent": str(item.get("from_agent", ""))[:64],
            "to_agent": str(item.get("to_agent", ""))[:64],
            "broadcast": bool(item.get("broadcast")),   # --all: every session wakes
            "targeted": bool(item.get("targeted")),     # sender chose the session; honour give-up
            "fallback": item["fallback"] if item.get("fallback") in FALLBACKS else "broadcast",
            "kind": item["kind"],
            "status": item.get("status", ""),
            "text": str(item.get("text", ""))[:200_000],
            "meta": item.get("meta") if isinstance(item.get("meta"), dict) else {},
            "files": [],
            "received": time.strftime("%Y-%m-%d %H:%M:%S"),
            "received_ts": time.time(),
            "claimed_by": "",
            "claimed_at": 0,
        }
        if stored["kind"] == "result" and stored["status"] not in STATUSES:
            self._json(400, {"error": f"result status must be one of {STATUSES}"})
            return
        for f in item.get("files", []):
            raw = base64.b64decode(f.get("data_b64", ""))
            if len(raw) > MAX_FILE_BYTES:
                self._json(413, {"error": "file too large"})
                return
            fname = sanitize_filename(f.get("filename", "unnamed"))
            fpath = FILES_DIR / f"{item_id}_{fname}"
            fpath.write_bytes(raw)
            stored["files"].append(
                {"filename": fname, "size": len(raw), "stored_path": str(fpath)})
        if not stored["broadcast"] and not stored["to_agent"]:
            stored["to_agent"] = _pick_live()   # anycast: deliver to one live session ('' -> _route assigns later)
        (INBOX_DIR / f"{item_id}.json").write_text(json.dumps(stored, indent=2))
        touch_activity("recv")
        self._json(200, {"ok": True, "id": item_id, "thread": stored["thread"]})
        if self.notify_command:
            summary = f"{stored['from']}: {stored['kind']} - {stored['text'][:120]}"
            try:
                subprocess.Popen(self.notify_command + [summary],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                sys.stderr.write(f"notify_command failed: {e}\n")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def resolve_listen_host(host):
    if host != "auto":
        return host
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True)
        ip = r.stdout.strip().splitlines()[0] if r.returncode == 0 and r.stdout.strip() else ""
    except FileNotFoundError:
        ip = ""
    if not ip:
        sys.exit("listen.host is 'auto' but no Tailscale IP was found - is tailscale up?\n"
                 "The daemon only binds to the private tailnet interface; it will not\n"
                 "listen on LAN or public interfaces. Set listen.host explicitly to override.")
    return ip


def cmd_daemon(cfg, args):
    ensure_dirs()
    listen = cfg.get("listen", {})
    host = resolve_listen_host(listen.get("host", "auto"))
    port = listen.get("port", 8765)
    Handler.notify_command = cfg.get("notify_command")
    Handler.me = cfg["me"]
    Handler.peer_by_token = {p["issued_token"]: name
                             for name, p in cfg.get("peers", {}).items() if p.get("issued_token")}
    scope = "tailnet-only" if listen.get("host", "auto") == "auto" else "custom bind"
    print(f"herald v{__version__} daemon: {cfg['me']} listening on {host}:{port} ({scope}), inbox {INBOX_DIR}",
          flush=True)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    threading.Thread(target=_maintenance_loop, args=(cfg["me"], f"{host}:{port}", started),
                     daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()


def _maintenance_loop(me, listen, started):
    """Heartbeat the status file every tick, drain queued items to reachable
    peers periodically, and reap dead sessions / stranded targeted items.
    Network is only touched when items queue or a target is unreachable."""
    retry_every = max(1, RETRY_INTERVAL // HEARTBEAT_INTERVAL)
    tick = 0
    last = time.time()
    skip_reap_until = 0
    while True:
        now = time.time()
        if now - last > SUSPEND_GAP:
            skip_reap_until = now + SESSION_LEASE   # host likely slept; let sessions re-check in
        last = now
        write_status({"pid": os.getpid(), "version": __version__, "me": me,
                      "listen": listen, "started": started, "heartbeat": now,
                      "queued": sum(1 for _ in QUEUE_DIR.glob("*/*.json"))})
        try:
            cfg = load_config()
        except (SystemExit, OSError, json.JSONDecodeError):
            cfg = None
        if cfg:
            # pick up newly issued/removed peer tokens without needing a restart
            Handler.peer_by_token = {p["issued_token"]: name
                                     for name, p in cfg.get("peers", {}).items() if p.get("issued_token")}
            try:
                _route(cfg)   # keep single-copy items assigned to one live session
            except (OSError, json.JSONDecodeError):
                pass
        if cfg and tick % retry_every == 0 and any(QUEUE_DIR.glob("*/*.json")):
            for peer_name in cfg.get("peers", {}):
                try:
                    flush_queue(cfg, peer_name)
                except (SystemExit, OSError):
                    pass
        if cfg and now >= skip_reap_until:
            try:
                _reap(cfg)
            except (SystemExit, OSError, json.JSONDecodeError):
                pass
        tick += 1
        time.sleep(HEARTBEAT_INTERVAL)


def _pick_live(sessions=None):
    """One live session to hand a single-copy item to: the most recently
    heartbeated, or '' if nobody is listening."""
    if sessions is None:
        sessions = read_sessions()
    live = [(s.get("heartbeat", 0), n) for n, s in sessions.items() if session_alive(n, sessions)]
    return max(live)[1] if live else ""


def _route(cfg):
    """Single-delivery: keep each anycast item (not broadcast, not sender-
    targeted) assigned to exactly one live session, so only that session's
    `wait` wakes for it. Reassign if its session dies. Sender-targeted items are
    left to _reap's give-up logic."""
    sessions = read_sessions()
    for p in INBOX_DIR.glob("*.json"):
        try:
            item = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if item.get("broadcast") or item.get("claimed_by") or item.get("targeted"):
            continue
        cur = item.get("to_agent", "")
        if cur and session_alive(cur, sessions):
            continue
        chosen = _pick_live(sessions)
        if chosen and chosen != cur:
            item["to_agent"] = chosen
            try:
                p.write_text(json.dumps(item, indent=2))
            except OSError:
                pass


def _reap(cfg):
    """Prune dead session records and release targeted items whose target
    session never showed up (informing the original sender)."""
    now = time.time()
    sessions = read_sessions()
    for name in list(sessions):
        if not session_alive(name, sessions):
            try:
                (SESSIONS_DIR / f"{sanitize_filename(name)}.json").unlink()
            except OSError:
                pass
            sessions.pop(name, None)
    for p in INBOX_DIR.glob("*.json"):
        try:
            item = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        target = item.get("to_agent", "")
        if (not target or not item.get("targeted") or item.get("claimed_by")
                or item.get("unpinned") or item.get("bounced")):
            continue   # anycast items are handled by _route, not the give-up path
        if session_alive(target, sessions):
            continue
        if now - item.get("received_ts", 0) < TARGET_GIVEUP:
            continue
        if item.get("fallback", "broadcast") == "hold":
            continue
        if item.get("fallback") == "bounce":
            _notify_origin(cfg, item, target,
                           f"Undeliverable: agent '{target}' was unreachable for your "
                           f"{item.get('kind')} in thread {item.get('thread')}; not reassigned.",
                           "undeliverable")
            item["bounced"] = True
        else:
            chosen = _pick_live(sessions)
            _notify_origin(cfg, item, target,
                           f"Reassigned: agent '{target}' was unreachable, so your "
                           f"{item.get('kind')} in thread {item.get('thread')} went to "
                           f"another of {cfg['me']}'s sessions.",
                           "reassigned")
            item["unpinned"] = True
            item["targeted"] = False    # now anycast; _route moves it on if that session dies too
            item["to_agent"] = chosen   # one live session, or "" until _route assigns one
        try:
            p.write_text(json.dumps(item, indent=2))
        except OSError:
            pass


def _notify_origin(cfg, item, target, text, intent):
    peer = item.get("from", "")
    if peer not in cfg.get("peers", {}):
        return
    payload = {"kind": "message", "text": text,
               "thread": item.get("thread", ""), "reply_to": item.get("id", ""),
               "to_agent": item.get("from_agent", ""),
               "meta": {"herald_intent": intent, "target": target}}
    try:
        deliver(cfg, peer, payload)
    except SystemExit:
        pass


# ---------------- sending ----------------

def parse_meta(pairs):
    meta = {}
    for pair in pairs or []:
        if "=" not in pair:
            sys.exit(f"--meta must be key=value, got '{pair}'")
        k, v = pair.split("=", 1)
        meta[k] = v
    return meta


def _post(cfg, peer, payload):
    """Send one payload to a peer. Raises URLError if unreachable,
    HTTPError if the peer is reachable but rejects it."""
    payload["from"] = cfg["me"]
    payload["from_agent"] = sender_agent()
    req = urllib.request.Request(
        peer["url"].rstrip("/") + "/send",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {peer['token']}"},
    )
    touch_activity("send")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _record_outbox(payload, result, peer_name):
    ensure_dirs()
    record = dict(payload)
    record.pop("_qid", None)
    for f in record.get("files", []):
        f.pop("data_b64", None)
    record.update(id=result["id"], thread=result["thread"], to=peer_name,
                  sent=time.strftime("%Y-%m-%d %H:%M:%S"))
    (OUTBOX_DIR / f"{result['id']}.json").write_text(json.dumps(record, indent=2))


def enqueue(peer_name, payload):
    d = QUEUE_DIR / sanitize_filename(peer_name)
    d.mkdir(parents=True, exist_ok=True)
    payload.setdefault("_qid", new_id())
    (d / f"{payload['_qid']}.json").write_text(json.dumps(payload))
    return len(list(d.glob("*.json")))


def flush_queue(cfg, peer_name, verbose=False):
    """Retry queued items for a peer, oldest first. Stops at the first
    unreachable error (peer still down); drops items the peer rejects."""
    peer = cfg.get("peers", {}).get(peer_name)
    d = QUEUE_DIR / sanitize_filename(peer_name)
    if not peer or not d.exists():
        return 0
    sent = 0
    for f in sorted(d.glob("*.json")):
        payload = json.loads(f.read_text())
        try:
            result = _post(cfg, peer, payload)
        except urllib.error.HTTPError as e:
            f.unlink()
            if verbose:
                print(f"  dropped queued item for {peer_name}: rejected ({e.code})")
            continue
        except urllib.error.URLError:
            break
        _record_outbox(payload, result, peer_name)
        f.unlink()
        sent += 1
    if sent and verbose:
        print(f"Flushed {sent} queued item(s) to {peer_name}")
    return sent


def deliver(cfg, peer_name, payload, queue_on_fail=True):
    peer = cfg.get("peers", {}).get(peer_name)
    if not peer:
        sys.exit(f"Unknown peer '{peer_name}'. Known: {', '.join(cfg.get('peers', {}))}")
    flush_queue(cfg, peer_name)
    try:
        result = _post(cfg, peer, payload)
    except urllib.error.HTTPError as e:
        sys.exit(f"Send to {peer_name} rejected ({e.code} {e.reason}) - "
                 f"check the peer token/URL; not queued.")
    except urllib.error.URLError as e:
        if not queue_on_fail:
            sys.exit(f"Send to {peer_name} failed: {e.reason}")
        depth = enqueue(peer_name, payload)
        reason = getattr(e, "reason", e)
        print(f"Peer '{peer_name}' is unreachable ({reason}) - queued for retry "
              f"({depth} pending). Delivers on next contact, or run: herald flush {peer_name}")
        return None

    _record_outbox(payload, result, peer_name)
    return result


def attach_files(payload, file_args):
    files = []
    for f in file_args or []:
        fpath = Path(f)
        if not fpath.is_file():
            sys.exit(f"No such file: {fpath}")
        raw = fpath.read_bytes()
        if len(raw) > MAX_FILE_BYTES:
            sys.exit(f"{fpath.name} exceeds {MAX_FILE_BYTES // (1024*1024)}MB limit")
        files.append({"filename": fpath.name, "size": len(raw),
                      "data_b64": base64.b64encode(raw).decode()})
    if files:
        payload["files"] = files


def cmd_send(cfg, args):
    if args.task and args.message:
        sys.exit("Use --task (with the request as its text) or --message, not both")
    if not (args.task or args.message or args.file):
        sys.exit("Nothing to send: use --message, --task, and/or --file")
    payload = {
        "kind": "task" if args.task else "message",
        "text": args.task or args.message or "",
        "meta": parse_meta(args.meta),
    }
    if args.task:
        payload["status"] = "pending"
    if args.thread:
        payload["thread"] = args.thread
    if args.all:
        payload["broadcast"] = True
    elif args.agent:
        payload["to_agent"] = args.agent
        payload["targeted"] = True
        payload["fallback"] = args.fallback
    attach_files(payload, args.file)
    result = deliver(cfg, args.peer, payload)
    if result is None:
        return
    print(f"Delivered to {args.peer}: {payload['kind']} id {result['id']}, thread {result['thread']}")


def find_inbox_item(item_id):
    path = INBOX_DIR / f"{item_id}.json"
    if not path.exists():
        sys.exit(f"No inbox item {item_id}")
    return json.loads(path.read_text())


def cmd_reply(cfg, args):
    orig = find_inbox_item(args.id)
    payload = {
        "kind": "message",
        "text": args.message or "",
        "thread": orig["thread"],
        "reply_to": orig["id"],
        "meta": parse_meta(args.meta),
    }
    if args.all:
        payload["broadcast"] = True
    else:
        target = args.agent or orig.get("from_agent") or ""
        if target:
            payload["to_agent"] = target
            payload["targeted"] = True
            payload["fallback"] = args.fallback
    attach_files(payload, args.file)
    result = deliver(cfg, orig["from"], payload)
    if result is None:
        return
    print(f"Replied to {orig['from']} in thread {orig['thread']} (id {result['id']})")


def cmd_result(cfg, args):
    orig = find_inbox_item(args.id)
    if orig["kind"] != "task":
        sys.exit(f"Item {args.id} is a {orig['kind']}, not a task")
    payload = {
        "kind": "result",
        "status": args.status,
        "text": args.message or "",
        "thread": orig["thread"],
        "reply_to": orig["id"],
        "meta": parse_meta(args.meta),
    }
    if args.all:
        payload["broadcast"] = True
    else:
        target = args.agent or orig.get("from_agent") or ""
        if target:
            payload["to_agent"] = target
            payload["targeted"] = True
            payload["fallback"] = args.fallback
    attach_files(payload, args.file)
    result = deliver(cfg, orig["from"], payload)
    if result is None:
        return
    print(f"Sent {args.status} result to {orig['from']} in thread {orig['thread']} (id {result['id']})")


def cmd_status(cfg, args):
    if not STATUS_PATH.exists():
        print("herald daemon: not running (no status file). Start it with: herald daemon")
        sys.exit(1)
    try:
        s = json.loads(STATUS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        sys.exit("herald daemon: status file unreadable")
    age = time.time() - s.get("heartbeat", 0)
    if age > HEARTBEAT_INTERVAL * 3:
        print(f"herald daemon: STALE - last heartbeat {age:.0f}s ago "
              f"(pid {s.get('pid')} likely dead). Restart with: herald daemon")
        sys.exit(1)
    print(f"herald v{s.get('version', '?')} daemon: running - {s.get('me')} on {s.get('listen')} "
          f"(pid {s.get('pid')}, up since {s.get('started')}, {s.get('queued', 0)} queued)")


def queued_count(peer_name):
    d = QUEUE_DIR / sanitize_filename(peer_name)
    return len(list(d.glob("*.json"))) if d.exists() else 0


def cmd_flush(cfg, args):
    peers = [args.peer] if args.peer else list(cfg.get("peers", {}))
    queued = {name: queued_count(name) for name in peers}
    if not any(queued.values()):
        print(f"Nothing queued for {args.peer}." if args.peer else "Nothing to flush.")
        return
    for name, pending in queued.items():
        if pending:
            flush_queue(cfg, name, verbose=True)
            left = queued_count(name)
            if left:
                print(f"{name}: still unreachable, {left} item(s) remain queued.")


# ---------------- inbox / threads ----------------

def summarise(i, direction):
    who = f"from {i['from']}" if direction == "in" else f"to {i['to']}"
    status = f" [{i['status']}]" if i.get("status") else ""
    target = f" ->{i['to_agent']}" if i.get("to_agent") else ""
    files = f" ({len(i['files'])} file{'s' if len(i['files']) != 1 else ''})" if i.get("files") else ""
    claimed = f" (claimed: {i['claimed_by']})" if i.get("claimed_by") else ""
    preview = i["text"][:80].replace("\n", " ")
    return f"{i['id']}  {i['kind']:<7}{status}{target} {who}{files}{claimed}  {preview}"


def cmd_inbox(cfg, args):
    ensure_dirs()
    items = [json.loads(p.read_text()) for p in sorted(INBOX_DIR.glob("*.json"))]
    if args.unclaimed:
        items = [i for i in items if not i.get("claimed_by")]
    if args.mine:
        me = agent_name()
        items = [i for i in items if not i.get("to_agent") or i.get("to_agent") == me]
    if not items:
        print("No unclaimed items" if args.unclaimed else "Inbox empty")
        return
    for i in items:
        flag = " " if i.get("claimed_by") else "*"
        print(f"{flag} {summarise(i, 'in')}")


def cmd_read(cfg, args):
    path = INBOX_DIR / f"{args.id}.json"
    item = find_inbox_item(args.id)
    me_agent = agent_name()
    target = item.get("to_agent", "")
    if target and target != me_agent and not item.get("unpinned") and not args.force:
        sys.exit(f"Item {args.id} is addressed to agent '{target}', not '{me_agent}'. "
                 f"Leave it for that session, or use --force to handle it anyway.")
    owner = item.get("claimed_by", "")
    if owner and owner != me_agent and not args.force and session_alive(owner):
        sys.exit(f"Already claimed by agent '{owner}' - it is handling this "
                 f"item (use --force to read anyway without claiming)")
    shown = {k: v for k, v in item.items() if k != "files"}
    shown["files"] = [f["filename"] for f in item["files"]]
    print(json.dumps(shown, indent=2))
    for f in item["files"]:
        out = Path(args.out or ".") / f["filename"]
        out.write_bytes(Path(f["stored_path"]).read_bytes())
        print(f"File written to {out.resolve()}")
    if not args.force and (not owner or owner == me_agent or not session_alive(owner)):
        item["claimed_by"] = me_agent
        item["claimed_at"] = time.time()
        path.write_text(json.dumps(item, indent=2))


def cmd_thread(cfg, args):
    ensure_dirs()
    entries = []
    for d, direction in ((INBOX_DIR, "in"), (OUTBOX_DIR, "out")):
        for p in d.glob("*.json"):
            i = json.loads(p.read_text())
            if i.get("thread") == args.id:
                entries.append((i.get("received") or i.get("sent"), direction, i))
    if not entries:
        sys.exit(f"No items in thread {args.id}")
    for ts, direction, i in sorted(entries, key=lambda e: (e[0], e[1], e[2]["id"])):
        arrow = "<-" if direction == "in" else "->"
        print(f"{ts} {arrow} {summarise(i, direction)}")


def _show_and_claim(item, me, out_dir=None):
    """Print an item's full content (writing its files out) and claim it for
    this agent. Used by `read`, and by `wait --read` / `ask` to fold the read
    into the same turn."""
    path = INBOX_DIR / f"{item['id']}.json"
    shown = {k: v for k, v in item.items() if k != "files"}
    shown["files"] = [f["filename"] for f in item["files"]]
    print(json.dumps(shown, indent=2))
    for f in item["files"]:
        out = Path(out_dir or ".") / f["filename"]
        out.write_bytes(Path(f["stored_path"]).read_bytes())
        print(f"File written to {out.resolve()}")
    item["claimed_by"] = me
    item["claimed_at"] = time.time()
    path.write_text(json.dumps(item, indent=2))


def cmd_wait(cfg, args):
    ensure_dirs()
    me = agent_name()
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    atexit.register(clear_session)
    signal.signal(signal.SIGTERM, lambda *a: (clear_session(), sys.exit(0)))
    known = {p.name for p in INBOX_DIR.glob("*.json")}
    deadline = time.time() + args.timeout if args.timeout else None
    while True:
        write_session(started)
        mine = []
        for name in sorted({p.name for p in INBOX_DIR.glob("*.json")} - known):
            item = json.loads((INBOX_DIR / name).read_text())
            if item.get("broadcast"):
                mine.append(item)          # --all: every session wakes
            elif item.get("to_agent", "") == me:
                mine.append(item)          # single-delivery: this copy is for me
            # else: assigned to another session, or not yet assigned - skip silently, no agent woken
        if mine:
            for item in mine:
                if args.read:
                    _show_and_claim(item, me, args.out)   # fold the read into this turn
                else:
                    status = f" [{item['status']}]" if item.get("status") else ""
                    print(f"NEW {item['kind']}{status} from {item['from']}: "
                          f"id {item['id']}, thread {item['thread']}")
            clear_session()
            return
        if deadline and time.time() > deadline:
            print("Timed out with no new items")
            clear_session()
            sys.exit(2)
        time.sleep(1)


def cmd_ask(cfg, args):
    """Send a task/message and block for the reply, returning it in one command
    - so a synchronous request/reply is a single turn, not send + wait + read.
    Only for reachable peers; an offline peer falls back to the async queue."""
    if args.task and args.message:
        sys.exit("Use --task or --message, not both")
    if not (args.task or args.message):
        sys.exit("Nothing to ask: use --task or --message")
    ensure_dirs()
    me = agent_name()
    payload = {
        "kind": "task" if args.task else "message",
        "text": args.task or args.message,
        "meta": parse_meta(args.meta),
    }
    if args.task:
        payload["status"] = "pending"
    if args.agent:
        payload["to_agent"] = args.agent
        payload["fallback"] = args.fallback
    attach_files(payload, args.file)
    known = {p.name for p in INBOX_DIR.glob("*.json")}
    result = deliver(cfg, args.peer, payload)
    if result is None:
        print("Peer is offline - queued; can't wait synchronously. Use `herald wait` for the reply.")
        return
    thread = result["thread"]
    print(f"Sent {payload['kind']} to {args.peer} (thread {thread}); waiting for reply...")
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    atexit.register(clear_session)
    signal.signal(signal.SIGTERM, lambda *a: (clear_session(), sys.exit(0)))
    deadline = time.time() + (args.timeout or 300)
    while time.time() < deadline:
        write_session(started)
        new_names = {p.name for p in INBOX_DIR.glob("*.json")} - known
        known.update(new_names)
        items = [json.loads((INBOX_DIR / name).read_text()) for name in new_names]
        for item in sorted(items, key=lambda value: (value.get("received_ts", 0), value["id"])):
            if item.get("thread") != thread:
                continue
            target = item.get("to_agent", "")
            if target and target != me:
                continue
            if item["kind"] == "result" and item.get("status") in ("accepted", "working"):
                print(f"[{item['status']}] {item['from']}: {item['text'][:200]}")
                continue   # progress; keep waiting for the terminal reply
            if item.get("meta", {}).get("herald_intent") == "ack":
                print(f"[ack] {item['from']}: {item['text'][:200]}")
                continue   # progress; keep waiting for the terminal reply
            _show_and_claim(item, me, args.out)
            clear_session()
            return
        time.sleep(1)
    clear_session()
    print(f"No reply from {args.peer} within {args.timeout or 300}s (still open in thread {thread}).")
    sys.exit(2)


def cmd_ping(cfg, args):
    """Ask a peer's daemon if it's up and what version it runs - answered by the
    daemon itself, no agent woken. Cheap liveness/version check."""
    peers = [args.peer] if args.peer else list(cfg.get("peers", {}))
    if not peers:
        sys.exit("No peers to ping.")
    rc = 0
    for name in peers:
        peer = cfg.get("peers", {}).get(name)
        if not peer:
            sys.exit(f"Unknown peer '{name}'. Known: {', '.join(cfg.get('peers', {}))}")
        try:
            with urllib.request.urlopen(peer["url"].rstrip("/") + "/ping", timeout=5) as resp:
                d = json.loads(resp.read())
            print(f"{name}: up - {d.get('me', '?')} herald v{d.get('version', '?')}")
        except (urllib.error.URLError, OSError) as e:
            print(f"{name}: unreachable ({getattr(e, 'reason', e)})")
            rc = 1
    sys.exit(rc)


def cmd_sessions(cfg, args):
    ensure_dirs()
    sessions = read_sessions()
    now = time.time()
    live = sorted((n, s) for n, s in sessions.items() if session_alive(n, sessions))
    if not live:
        print("No live agent sessions")
        return
    for name, s in live:
        age = now - s.get("heartbeat", 0)
        print(f"{name}  host {s.get('host', '?')} pid {s.get('pid', '?')}  "
              f"heartbeat {age:.0f}s ago  waiting on {s.get('waiting_on', '?')}")


def cmd_access(cfg, args):
    """Read-only audit of who can reach whom, and who is authenticated."""
    peers = cfg.get("peers", {})
    if not peers:
        print("No peers.")
        return
    print(f"{'PEER':<16}{'I CAN REACH':<13}{'THEY REACH ME':<16}URL")
    for name, p in sorted(peers.items()):
        out = "yes" if p.get("token") else "not yet"
        inb = "authenticated" if p.get("issued_token") else "no token issued"
        print(f"{name:<16}{out:<13}{inb:<16}{p.get('url', '-')}")


def cmd_introduce(cfg, args):
    if args.peer not in cfg.get("peers", {}):
        sys.exit(f"Unknown peer '{args.peer}'. Add them first: herald peer add {args.peer} <url> <token>")
    listen = cfg.get("listen", {})
    host = resolve_listen_host(listen.get("host", "auto"))
    url = f"http://{host}:{listen.get('port', 8765)}"
    # issue this peer their own inbound token so we can authenticate them by it
    issued = cfg["peers"][args.peer].get("issued_token") or secrets.token_urlsafe(24)
    cfg["peers"][args.peer]["issued_token"] = issued
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    payload = {
        "kind": "message",
        "text": f"{cfg['me']} would like to connect - accept with: herald accept <item-id>",
        "meta": {"herald_intent": "introduce", "name": cfg["me"],
                 "url": url, "token": issued},
    }
    result = deliver(cfg, args.peer, payload)
    if result is None:
        return   # queued while the peer is offline; deliver already reported it
    print(f"Introduction sent to {args.peer} (id {result['id']}); "
          f"once accepted they can message you back")


def cmd_accept(cfg, args):
    item = find_inbox_item(args.id)
    meta = item.get("meta", {})
    if meta.get("herald_intent") != "introduce":
        sys.exit(f"Item {args.id} is not an introduction")
    name, url, token = meta.get("name"), meta.get("url"), meta.get("token")
    if not (name and url and token):
        sys.exit("Introduction is missing name/url/token")
    peer = cfg.setdefault("peers", {}).setdefault(name, {})   # keep any token we already issued them
    peer["url"] = url
    peer["token"] = token
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    item["claimed_by"] = agent_name()
    item["claimed_at"] = time.time()
    (INBOX_DIR / f"{item['id']}.json").write_text(json.dumps(item, indent=2))
    payload = {"kind": "message",
               "text": f"{cfg['me']} accepted your introduction - connected.",
               "thread": item["thread"], "reply_to": item["id"],
               "meta": {"herald_intent": "accepted", "name": cfg["me"]}}
    deliver(cfg, name, payload)
    print(f"Peer '{name}' added ({url}) and confirmation sent")


def cmd_peer(cfg, args):
    if args.action == "list":
        for name, peer in cfg.get("peers", {}).items():
            print(f"{name}  {peer.get('url', '-')}")
        return
    if not args.name:
        sys.exit("peer add/remove/issue needs a name")
    if args.action == "issue":
        listen = cfg.get("listen", {})
        url = f"http://{resolve_listen_host(listen.get('host', 'auto'))}:{listen.get('port', 8765)}"
        issued = secrets.token_urlsafe(24)
        cfg.setdefault("peers", {}).setdefault(args.name, {})["issued_token"] = issued
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
        print(f"Issued an inbound token for '{args.name}'. Send them these two commands:")
        print(f"  herald peer add {cfg['me']} {url} {issued}")
        print(f"  herald introduce {cfg['me']}")
        return
    if args.action == "add":
        if not (args.url and args.token):
            sys.exit("Usage: herald peer add NAME URL TOKEN")
        peer = cfg.setdefault("peers", {}).setdefault(args.name, {})
        peer["url"] = args.url
        peer["token"] = args.token
    elif args.action == "remove":
        if args.name not in cfg.get("peers", {}):
            sys.exit(f"No peer '{args.name}'")
        del cfg["peers"][args.name]
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"Peer '{args.name}' {'added' if args.action == 'add' else 'removed'}")


def cmd_init(cfg_unused, args):
    HERALD_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not args.force:
        sys.exit(f"{CONFIG_PATH} already exists (use --force to overwrite)")
    cfg = {
        "me": args.me,
        "listen": {"host": "auto", "port": args.port},
        "peers": {},
    }
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    ensure_dirs()
    print(f"Wrote {CONFIG_PATH}")
    print("To connect a peer, issue them a token: herald peer issue <name>")


def main():
    p = argparse.ArgumentParser(prog="herald", description=__doc__.split("\n")[0])
    p.add_argument("--version", action="version", version=f"herald {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init", help="create ~/.herald/config.json")
    sp.add_argument("--me", required=True)
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--force", action="store_true")

    sub.add_parser("daemon", help="run the receiver")

    sp = sub.add_parser("send", help="start or continue a thread with a peer")
    sp.add_argument("peer")
    sp.add_argument("--message", "-m")
    sp.add_argument("--task", "-t", help="a task request (text is the request)")
    sp.add_argument("--file", "-f", action="append")
    sp.add_argument("--meta", action="append", help="key=value, repeatable")
    sp.add_argument("--thread", help="continue an existing thread")
    sp.add_argument("--agent", help="address a specific session of the peer (see: herald sessions)")
    sp.add_argument("--fallback", choices=FALLBACKS, default="broadcast",
                    help="if the target session never appears: broadcast (release to any), "
                         "hold (keep pinned), bounce (return undeliverable to you)")
    sp.add_argument("--all", action="store_true",
                    help="deliver to ALL the recipient's sessions (default: one)")

    sp = sub.add_parser("reply", help="reply to an inbox item (peer/thread inferred)")
    sp.add_argument("id")
    sp.add_argument("--message", "-m", required=True)
    sp.add_argument("--file", "-f", action="append")
    sp.add_argument("--meta", action="append")
    sp.add_argument("--agent", help="override the target session (defaults to the sender's)")
    sp.add_argument("--fallback", choices=FALLBACKS, default="broadcast")
    sp.add_argument("--all", action="store_true", help="deliver to ALL the recipient's sessions")

    sp = sub.add_parser("result", help="send a task status/result back to its sender")
    sp.add_argument("id", help="the inbox task item id")
    sp.add_argument("--status", required=True, choices=STATUSES)
    sp.add_argument("--message", "-m")
    sp.add_argument("--file", "-f", action="append")
    sp.add_argument("--meta", action="append")
    sp.add_argument("--agent", help="override the target session (defaults to the sender's)")
    sp.add_argument("--fallback", choices=FALLBACKS, default="broadcast")
    sp.add_argument("--all", action="store_true", help="deliver to ALL the recipient's sessions")

    sp = sub.add_parser("inbox", help="list received items")
    sp.add_argument("--unclaimed", action="store_true")
    sp.add_argument("--mine", action="store_true",
                    help="only items addressed to this agent or broadcast")

    sp = sub.add_parser("read", help="show an item (writes files to cwd), claim it for this agent")
    sp.add_argument("id")
    sp.add_argument("--out")
    sp.add_argument("--force", action="store_true", help="read without claiming, even if claimed")

    sp = sub.add_parser("introduce", help="send a peer my address+token so they can add me")
    sp.add_argument("peer")

    sp = sub.add_parser("accept", help="accept an introduction: add them as a peer, confirm back")
    sp.add_argument("id")

    sp = sub.add_parser("peer", help="manage peers")
    sp.add_argument("action", choices=["add", "list", "remove", "issue"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("url", nargs="?")
    sp.add_argument("token", nargs="?")

    sub.add_parser("access", help="audit who can reach whom and who is authenticated")

    sp = sub.add_parser("thread", help="show a whole conversation, both directions")
    sp.add_argument("id")

    sp = sub.add_parser("wait", help="block until a new item arrives")
    sp.add_argument("--timeout", type=int, default=0)
    sp.add_argument("--read", action="store_true",
                    help="show and claim each new item on wake (folds in the read)")
    sp.add_argument("--out", help="directory for attached files (with --read)")

    sp = sub.add_parser("ask", help="send and block for the reply in one turn (reachable peers only)")
    sp.add_argument("peer")
    sp.add_argument("--message", "-m")
    sp.add_argument("--task", "-t", help="a task request (text is the request)")
    sp.add_argument("--file", "-f", action="append")
    sp.add_argument("--meta", action="append", help="key=value, repeatable")
    sp.add_argument("--agent", help="address a specific session of the peer")
    sp.add_argument("--fallback", choices=FALLBACKS, default="broadcast")
    sp.add_argument("--timeout", type=int, default=300, help="seconds to wait for the reply")
    sp.add_argument("--out", help="directory for attached files in the reply")

    sp = sub.add_parser("ping", help="check a peer's daemon is up and its version (no agent woken)")
    sp.add_argument("peer", nargs="?", help="a single peer (default: all)")

    sp = sub.add_parser("flush", help="retry items queued for unreachable peers")
    sp.add_argument("peer", nargs="?", help="a single peer (default: all)")

    sub.add_parser("status", help="report whether the daemon is running")

    sub.add_parser("sessions", help="list agent sessions currently listening")

    args = p.parse_args()
    identity_commands = {"send", "reply", "result", "read", "wait", "ask"}
    if args.cmd in identity_commands and not os.environ.get("HERALD_AGENT"):
        sys.exit(
            f"HERALD_AGENT is required for `herald {args.cmd}`. Set one stable session name "
            f"and use it for every related command, for example: "
            f"HERALD_AGENT=codex-ticket123 herald {args.cmd} ..."
        )
    if args.cmd == "inbox" and args.mine and not os.environ.get("HERALD_AGENT"):
        sys.exit(
            "HERALD_AGENT is required for `herald inbox --mine`. Set one stable session name "
            "and use it for every related command."
        )
    cfg = None if args.cmd == "init" else load_config()
    {"init": cmd_init, "daemon": cmd_daemon, "send": cmd_send, "reply": cmd_reply,
     "result": cmd_result, "inbox": cmd_inbox, "read": cmd_read, "peer": cmd_peer,
     "introduce": cmd_introduce, "accept": cmd_accept, "ask": cmd_ask, "ping": cmd_ping,
     "access": cmd_access, "thread": cmd_thread, "wait": cmd_wait, "flush": cmd_flush,
     "status": cmd_status, "sessions": cmd_sessions}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
