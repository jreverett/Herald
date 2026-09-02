#!/usr/bin/env python3
"""herald - peer-to-peer agent-to-agent messaging.

Agent sessions (Claude Code, Codex, Copilot) on different machines hold threaded
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
  "default_mailbox": "main",
  "mailboxes": ["main"],
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
import fcntl
import json
import os
import secrets
import select
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__version__ = "0.9.6"

HERALD_DIR = Path(os.environ.get("HERALD_DIR", Path.home() / ".herald"))
CONFIG_PATH = HERALD_DIR / "config.json"
INBOX_DIR = HERALD_DIR / "inbox"
OUTBOX_DIR = HERALD_DIR / "outbox"
FILES_DIR = HERALD_DIR / "files"
QUEUE_DIR = HERALD_DIR / "queue"
ACTIVITY_DIR = HERALD_DIR / "activity"
WORKING_DIR = HERALD_DIR / "working"
SESSIONS_DIR = HERALD_DIR / "sessions"
CONSUMERS_DIR = HERALD_DIR / "consumers"
FAILED_DIR = HERALD_DIR / "failed"
STATUS_PATH = HERALD_DIR / "status.json"
STATE_LOCK_PATH = HERALD_DIR / "state.lock"
MAX_FILE_BYTES = 100 * 1024 * 1024
KINDS = ("message", "task", "result")
STATUSES = ("accepted", "working", "done", "failed")
FALLBACKS = ("broadcast", "hold", "bounce")
RETRY_INTERVAL = 45
HEARTBEAT_INTERVAL = 5
SESSION_LEASE = 75          # a session with no heartbeat in this long is treated as gone
WORKING_LEASE = 90          # a working marker with no live session behind it goes stale this fast
WORKING_ALIVE_LEASE = 600   # its session is alive, so a long stretch between tool calls is not staleness
HARNESS_COMMS_SKIP = ("sh", "bash", "dash", "zsh", "fish", "env", "python", "python3", "herald")
TARGET_GIVEUP = 300         # release a targeted item whose target never reappears after this
SUSPEND_GAP = HEARTBEAT_INTERVAL * 6   # a maintenance tick later than this means the host slept
TTY_SEARCH_DEPTH = 12
PTS_MAJOR = 136


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"No config at {CONFIG_PATH}. Run: herald init --me NAME")
    return json.loads(CONFIG_PATH.read_text())


def ensure_dirs():
    for d in (INBOX_DIR, OUTBOX_DIR, FILES_DIR, QUEUE_DIR, ACTIVITY_DIR,
              WORKING_DIR, SESSIONS_DIR, CONSUMERS_DIR, FAILED_DIR):
        d.mkdir(parents=True, exist_ok=True)


@contextmanager
def state_lock():
    HERALD_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_LOCK_PATH.open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def atomic_write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    tmp.write_text(json.dumps(value, indent=2))
    os.replace(tmp, path)


def mailbox_name(cfg=None):
    if os.environ.get("HERALD_MAILBOX"):
        return os.environ["HERALD_MAILBOX"]
    if cfg:
        return str(cfg.get("default_mailbox") or "main")
    return "main"


def valid_mailbox_name(name):
    return (bool(name) and len(name) <= 64
            and all(char.isalnum() or char in "-_." for char in name)
            and name not in (".", ".."))


def configured_default_mailbox(cfg):
    name = str(cfg.get("default_mailbox") or "main")
    return name if valid_mailbox_name(name) else "main"


def registered_mailboxes(cfg):
    default = configured_default_mailbox(cfg)
    mailboxes = cfg.get("mailboxes") or [default]
    if isinstance(mailboxes, dict):
        mailboxes = list(mailboxes)
    names = [str(name) for name in mailboxes if valid_mailbox_name(str(name))]
    return list(dict.fromkeys([default, *names]))


def touch_activity(kind):
    """Record an outgoing ('send') or incoming ('recv') event for the tray."""
    try:
        ensure_dirs()
        (ACTIVITY_DIR / kind).write_text(str(time.time()))
    except OSError:
        pass


def harness_pid():
    """Pid of the agent process that ran this hook, so a marker can be dropped
    when that session dies rather than only when its lease expires. The hook's
    own ancestors are an interpreter and the shell that ran it; the first thing
    above those is the harness."""
    pid = os.getppid()
    for _ in range(TTY_SEARCH_DEPTH):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            comm = stat[stat.index("(") + 1:stat.rindex(")")]
            ppid = int(stat[stat.rindex(")") + 2:].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        if comm not in HARNESS_COMMS_SKIP:
            return pid
        if ppid <= 1:
            return None
        pid = ppid
    return None


def _start_ticks_from_stat(line):
    """Field 22 of a /proc/<pid>/stat line. Field 2 is the executable name in
    parentheses, unescaped, and may itself contain spaces and closing parens, so
    the fields after it can only be found from the last one."""
    return int(line[line.rindex(")") + 2:].split()[19])


def process_start_ticks(pid):
    """The process's start time in clock ticks since boot, or None. Pids are
    reused, so this is what makes a recorded pid a stable identity."""
    try:
        return _start_ticks_from_stat(Path(f"/proc/{pid}/stat").read_text())
    except (OSError, ValueError, IndexError):
        return None


def boot_id():
    """This boot's identifier. Start times are counted from boot, so they only
    compare within one."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def mark_working(key, label="", pid=None):
    """Record that an agent turn is running right now."""
    try:
        ensure_dirs()
        atomic_write_json(WORKING_DIR / f"{sanitize_filename(key)}.json",
                          {"key": key, "label": label, "pid": pid,
                           "pid_started": process_start_ticks(pid) if pid else None,
                           "boot": boot_id(), "heartbeat": time.time()})
    except OSError:
        pass


def clear_working(key):
    try:
        (WORKING_DIR / f"{sanitize_filename(key)}.json").unlink()
    except OSError:
        pass


def working_alive(rec, now=None):
    """Whether a marker still represents a running turn.

    A dead session's clear will never arrive, so its marker goes immediately.
    A live one gets the long lease: only a tool call refreshes the marker, and a
    turn can think for minutes without making one. The lease is still bounded so
    that a clear lost to a broken hook cannot pin the signal on for a session's
    whole life. The session is identified by pid and start time together, and a
    pid whose start time has moved is a different process on a reused number."""
    now = time.time() if now is None else now
    pid, started = rec.get("pid"), rec.get("pid_started")
    lease = WORKING_LEASE
    if isinstance(pid, int):
        if not is_pid_alive(pid):
            return False
        if isinstance(started, int) and rec.get("boot") == boot_id():
            if process_start_ticks(pid) != started:
                return False        # a reused number, running something else
            lease = WORKING_ALIVE_LEASE
    return now - rec.get("heartbeat", 0) <= lease


def working_labels(only_pids=None):
    """Labels of the agent turns still running, most recent first.

    A marker is cleared when its turn ends, so a surviving one means the agent
    is spending tokens now - not that it holds a claimed item and is waiting on
    its human. Spent markers are pruned here because a harness that crashes
    mid-turn never sends its clear."""
    now = time.time()
    live = []
    for path in WORKING_DIR.glob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not working_alive(rec, now):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        if only_pids is not None and rec.get("pid") not in only_pids:
            continue
        live.append((rec.get("heartbeat", 0), rec.get("label") or rec.get("key") or "?"))
    return [label for _, label in sorted(live, reverse=True)]


def herald_working_pids():
    """Harnesses holding a claimed inbox item they have not answered.

    A tab is reused for anything, so a turn is only herald's work if that tab
    also owes herald a reply. Neither half is enough: a marker alone reports any
    session that is busy, and a claim alone stays lit while the agent waits on
    its human."""
    pids = set()
    for path in INBOX_DIR.glob("*.json"):
        try:
            item = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if item_state(item) == "active" and isinstance(item.get("claimed_pid"), int):
            pids.add(item["claimed_pid"])
    return pids


def awaiting_human():
    """Herald work that needs the human, for the tray's red state.

    Two cases, both about herald itself rather than about whatever else the
    session is doing: a task this side answered 'accepted', which promises an
    answer once the human decides, and an item on a mailbox nothing is listening
    to, which will sit unread until someone looks."""
    listening = {s.get("mailbox", "main") for s in live_sessions()}
    accepted, unread = [], 0
    for path in INBOX_DIR.glob("*.json"):
        try:
            item = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        state = item_state(item)
        if item.get("acked_status") == "accepted" and state == "active":
            accepted.append(f"{item.get('from') or '?'}'s task")
        elif state == "pending" and item.get("to_mailbox", "main") not in listening:
            unread += 1
    return accepted + ([f"{unread} unread"] if unread else [])


def working_summary(labels):
    """Collapse repeated labels for a status line: several tabs open on one repo
    share a working directory, and would otherwise read as the same name twice."""
    counts = {}
    for label in labels:
        counts[label] = counts.get(label, 0) + 1
    return [name if n == 1 else f"{name} x{n}" for name, n in counts.items()]


def write_status(fields):
    try:
        ensure_dirs()
        tmp = STATUS_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(fields))
        tmp.replace(STATUS_PATH)
    except OSError:
        pass


def bell_enabled(cfg=None):
    override = os.environ.get("HERALD_BELL", "").strip().lower()
    if override:
        return override not in ("0", "off", "false", "no")
    return cfg.get("bell", True) is not False if cfg else True


def owning_tty():
    """Terminal device that owns this process tree, or None. An agent harness
    runs herald with pipes for stdio and no controlling terminal, so the tty
    the human is watching belongs to an ancestor process."""
    pid = os.getpid()
    for _ in range(TTY_SEARCH_DEPTH):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat[stat.rindex(")") + 2:].split()
            ppid, tty_nr = int(fields[1]), int(fields[4])
        except (OSError, ValueError, IndexError):
            return None
        if tty_nr:
            major = (tty_nr >> 8) & 0xfff
            minor = (tty_nr & 0xff) | ((tty_nr >> 12) & 0xfff00)
            return f"/dev/pts/{minor}" if major == PTS_MAJOR else None
        if ppid <= 1:
            return None
        pid = ppid
    return None


def ring_bell(cfg=None):
    """Emit BEL when the human is blocking the turn. What the terminal does
    with it (beep, flash, tab badge) is the terminal's choice."""
    if not bell_enabled(cfg):
        return False
    for target in (os.environ.get("HERALD_BELL_TTY"), "/dev/tty", owning_tty()):
        if not target:
            continue
        try:
            with open(target, "wb", buffering=0) as tty:
                tty.write(b"\a")
            return True
        except OSError:
            continue
    try:
        if sys.stderr.isatty():
            sys.stderr.write("\a")
            sys.stderr.flush()
            return True
    except (OSError, ValueError):
        pass
    return False


def new_id():
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(3)


def new_delivery_id():
    return secrets.token_hex(16)


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
        session_id = s.get("session_id") or p.stem
        s.setdefault("session_id", session_id)
        s.setdefault("mailbox", "main")
        s.setdefault("mode", "general")
        out[session_id] = s
    return out


def _session_record_alive(session):
    if session.get("host") == socket.gethostname() and isinstance(session.get("pid"), int):
        if not is_pid_alive(session["pid"]):
            return False
    return (time.time() - session.get("heartbeat", 0)) <= SESSION_LEASE


def session_alive(identifier, sessions=None):
    """Return whether a listener instance or legacy agent label is live."""
    if sessions is None:
        sessions = read_sessions()
    if identifier in sessions:
        return _session_record_alive(sessions[identifier])
    return any(s.get("agent") == identifier and _session_record_alive(s)
               for s in sessions.values())


def live_sessions(sessions=None, mailbox=None, mode=None, agent=None):
    sessions = sessions or read_sessions()
    return [s for s in sessions.values()
            if _session_record_alive(s)
            and (mailbox is None or s.get("mailbox", "main") == mailbox)
            and (mode is None or s.get("mode", "general") == mode)
            and (agent is None or s.get("agent") == agent)]


def write_session(listener, waiting_on="inbox"):
    try:
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        record = {
            "session_id": listener["session_id"],
            "agent": listener["agent"],
            "mailbox": listener["mailbox"],
            "mode": listener["mode"],
            "generation": listener.get("generation", 0),
            "request_id": listener.get("request_id", ""),
            "pid": os.getpid(),
            "harness_pid": listener.get("harness_pid"),
            "host": socket.gethostname(),
            "started": listener["started"],
            "heartbeat": time.time(),
            "waiting_on": waiting_on,
        }
        atomic_write_json(SESSIONS_DIR / f"{sanitize_filename(listener['session_id'])}.json", record)
    except OSError:
        pass


def clear_session(session_id):
    try:
        (SESSIONS_DIR / f"{sanitize_filename(session_id)}.json").unlink()
    except OSError:
        pass


def consumer_path(mailbox):
    return CONSUMERS_DIR / f"{sanitize_filename(mailbox)}.json"


def register_listener(cfg, mode="general", takeover=False):
    ensure_dirs()
    listener = {
        "session_id": f"listener-{new_id()}",
        "agent": agent_name(),
        "mailbox": mailbox_name(cfg),
        "mode": mode,
        # The listener process is short-lived; the harness above it is the thing
        # that stays and does the work, so that is what a claim is attributed to.
        "harness_pid": harness_pid(),
        "started": time.strftime("%Y-%m-%d %H:%M:%S"),
        "request_id": "",
        # Only a mailbox owner is given a generation, counting from 1. Zero means
        # this listener holds none, so a later owner re-presents what it left open.
        "generation": 0,
    }
    if mode == "general":
        # Two tabs working different topics share a mailbox routinely. Only take
        # ownership from a live listener when it is the same agent name coming
        # back (a tab restart) or the caller asked to take over (herald resume).
        # Otherwise register as a co-listener: still live, still eligible for
        # items addressed to this agent, but the incumbent keeps the mailbox and
        # the untargeted work that goes with it.
        # Decide and claim inside one lock. Reading the owner first and writing
        # after leaves a window where two tabs starting together both see no
        # owner and both claim the mailbox.
        with state_lock():
            owner = current_consumer(listener["mailbox"])
            coexist = bool(owner) and owner.get("agent") != listener["agent"] and not takeover
            listener["owns_mailbox"] = not coexist
            displaced = owner if (owner and not coexist
                                  and owner.get("agent") != listener["agent"]) else None
            if not coexist:
                path = consumer_path(listener["mailbox"])
                try:
                    current = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    current = {}
                generation = int(current.get("generation", 0))
                if takeover or current.get("agent") != listener["agent"]:
                    generation += 1
                listener["generation"] = max(1, generation)
                atomic_write_json(path, {
                    "mailbox": listener["mailbox"],
                    "agent": listener["agent"],
                    "session_id": listener["session_id"],
                    "generation": listener["generation"],
                    "updated_at": time.time(),
                })
        if coexist:
            print(f"Listening alongside '{owner.get('agent')}' on mailbox "
                  f"'{listener['mailbox']}'. Items addressed to '{listener['agent']}' "
                  f"come here; untargeted items stay with the mailbox owner. Use "
                  f"herald resume to take the mailbox instead.",
                  file=sys.stderr, flush=True)
        elif displaced:
            age = time.time() - displaced.get("heartbeat", 0)
            print(f"Displaced a live listener on mailbox '{listener['mailbox']}': agent "
                  f"'{displaced.get('agent')}' ({displaced.get('session_id')}), heartbeat "
                  f"{age:.0f}s ago. Items meant for that session will now arrive here - "
                  f"check an item is your work before acting on it. To listen without "
                  f"displacing it, use your own mailbox (herald mailbox add <name>, then "
                  f"HERALD_MAILBOX=<name>).", file=sys.stderr, flush=True)
    write_session(listener)
    return listener


def warn_if_owner_was_live(mailbox, agent):
    """Say so when this listener displaces another session that was still alive.

    Taking the mailbox is deliberate and supported - it is how a provider handoff
    works. What causes trouble is doing it silently: the new listener then
    receives work addressed to the displaced session and cannot tell it apart
    from its own, which invites answering for a session it is not.
    """
    owner = current_consumer(mailbox)
    if not owner or owner.get("agent") == agent:
        return
    age = time.time() - owner.get("heartbeat", 0)
    print(f"Displaced a live listener on mailbox '{mailbox}': agent "
          f"'{owner.get('agent')}' ({owner.get('session_id')}), heartbeat {age:.0f}s ago. "
          f"Items meant for that session will now arrive here - check an item is your "
          f"work before acting on it. To listen without displacing it, use your own "
          f"mailbox (herald mailbox add <name>, then HERALD_MAILBOX=<name>).",
          file=sys.stderr, flush=True)


def consumer_is_current(listener):
    if listener["mode"] != "general":
        return True
    if not listener.get("owns_mailbox", True):
        return True          # co-listener: never owned the mailbox, so cannot lose it
    try:
        current = json.loads(consumer_path(listener["mailbox"]).read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return current.get("session_id") == listener["session_id"]


def current_consumer(mailbox, sessions=None):
    try:
        current = json.loads(consumer_path(mailbox).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    sessions = sessions or read_sessions()
    session = sessions.get(current.get("session_id", ""))
    return session if session and _session_record_alive(session) else None


def active_assignment(item, sessions=None):
    sessions = sessions or read_sessions()
    session_id = item.get("assigned_session", "")
    session = sessions.get(session_id)
    if not session or not _session_record_alive(session):
        return None
    if session.get("mode", "general") == "general":
        consumer = current_consumer(item.get("to_mailbox") or "main", sessions)
        if not consumer or consumer.get("session_id") != session_id:
            # A co-listener does not own the mailbox but is still the right home
            # for items addressed to its agent name. This escape is only for a
            # session that never owned the mailbox - an owner that has since been
            # superseded must release its items so a handoff can pick them up.
            if session.get("owns_mailbox", True) or item.get("to_agent") != session.get("agent"):
                return None
    return session


def sanitize_filename(name):
    name = os.path.basename(name.replace("\\", "/"))
    return name.replace("..", "_") or "unnamed"


def item_state(item):
    if item.get("state"):
        return item["state"]
    if ((item.get("kind") == "result" and item.get("status") in ("accepted", "working"))
            or item.get("meta", {}).get("herald_intent") == "ack"):
        return "handled"
    return "handled" if item.get("claimed_by") else "pending"


def update_inbox_item(item_id, **changes):
    path = INBOX_DIR / f"{item_id}.json"
    with state_lock():
        if not path.exists():
            return None
        item = json.loads(path.read_text())
        item.update(changes)
        atomic_write_json(path, item)
        return item


def _apply_source_delivery(payload, delivery_state, error=""):
    item_id = payload.get("_source_item_id")
    effect = payload.get("_source_effect")
    if not item_id or not effect:
        return
    changes = {"response_delivery_id": payload.get("delivery_id", "")}
    # A later progress reply carries no status of its own, and must not erase the
    # 'accepted' that says a human is holding this item.
    if effect != "ack":
        changes["acked_status"] = ""
    elif payload.get("status"):
        changes["acked_status"] = payload["status"]
    if delivery_state == "delivered":
        if effect == "ack":
            changes.update(state="active", acknowledged_at=time.time(), delivery_error="")
        else:
            changes.update(state="handled", handled_at=time.time(), delivery_error="")
    elif delivery_state == "queued":
        changes.update(state="active" if effect == "ack" else "responded_pending_delivery")
        if effect == "ack":
            changes["acknowledged_at"] = time.time()
    else:
        changes.update(state="delivery_failed", delivery_error=error, presented_generation=0)
    update_inbox_item(item_id, **changes)


def _update_outstanding_request(item):
    reply_to = item.get("reply_to", "")
    if not reply_to:
        return
    with state_lock():
        path = OUTBOX_DIR / f"{reply_to}.json"
        if not path.exists():
            path = None
            for candidate in OUTBOX_DIR.glob("*.json"):
                try:
                    request = json.loads(candidate.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if reply_to in request.get("remote_ids", []):
                    path = candidate
                    break
            if path is None:
                return
        try:
            request = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        is_progress = ((item.get("kind") == "result"
                        and item.get("status") in ("accepted", "working"))
                       or item.get("meta", {}).get("herald_intent") == "ack")
        if is_progress:
            waiting = request.get("awaiting_reply_ids")
            if request.get("state") == "handled" or (waiting is not None and reply_to not in waiting):
                return
            request["state"] = "awaiting_terminal"
            request["last_progress_at"] = item.get("received_ts", time.time())
        else:
            waiting = request.get("awaiting_reply_ids", [request.get("id", "")])
            request["awaiting_reply_ids"] = [item_id for item_id in waiting
                                              if item_id != reply_to]
            if request["awaiting_reply_ids"]:
                request["state"] = "awaiting_terminal"
            else:
                request["state"] = "handled"
                request["handled_at"] = item.get("received_ts", time.time())
        atomic_write_json(path, request)


# ---------------- daemon (receiver) ----------------

class Handler(BaseHTTPRequestHandler):
    notify_command = None
    me = None
    default_mailbox = "main"
    mailboxes = ["main"]
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
            self._json(200, {"ok": True, "version": __version__, "me": self.me,
                             "capabilities": ["mailboxes-v1", "delivery-id-v1"]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/send":
            self._json(404, {"error": "not found"})
            return
        try:
            runtime_cfg = load_config()
        except (SystemExit, OSError, json.JSONDecodeError):
            runtime_cfg = None
        peer_by_token = ({p["issued_token"]: name
                          for name, p in runtime_cfg.get("peers", {}).items()
                          if p.get("issued_token")}
                         if runtime_cfg else self.peer_by_token)
        default_mailbox = configured_default_mailbox(runtime_cfg) if runtime_cfg else self.default_mailbox
        mailboxes = registered_mailboxes(runtime_cfg) if runtime_cfg else self.mailboxes
        auth = self.headers.get("Authorization", "")
        tok = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        sender = peer_by_token.get(tok)   # each peer has its own inbound token; the token is the identity
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

        if item["kind"] == "result" and item.get("status") not in STATUSES:
            self._json(400, {"error": f"result status must be one of {STATUSES}"})
            return
        decoded_files = []
        for f in item.get("files", []):
            raw = base64.b64decode(f.get("data_b64", ""))
            if len(raw) > MAX_FILE_BYTES:
                self._json(413, {"error": "file too large"})
                return
            decoded_files.append((sanitize_filename(f.get("filename", "unnamed")), raw))

        requested_mailbox = str(item.get("to_mailbox", ""))
        if requested_mailbox and not valid_mailbox_name(requested_mailbox):
            self._json(400, {"error": "invalid mailbox name"})
            return
        if requested_mailbox and not item.get("broadcast") and requested_mailbox not in mailboxes:
            self._json(400, {"error": f"unknown mailbox '{requested_mailbox}'"})
            return

        delivery_id = str(item.get("delivery_id", ""))[:64]
        stored_items = []
        with state_lock():
            if delivery_id:
                for path in INBOX_DIR.glob("*.json"):
                    try:
                        existing = json.loads(path.read_text())
                    except (OSError, json.JSONDecodeError):
                        continue
                    if existing.get("from") == sender and existing.get("delivery_id") == delivery_id:
                        self._json(200, {"ok": True, "id": existing["id"],
                                         "thread": existing["thread"], "duplicate": True})
                        return

            destinations = mailboxes if item.get("broadcast") else [
                str(item.get("to_mailbox") or default_mailbox)[:64]
            ]
            thread = str(item.get("thread", ""))[:64]
            for destination in destinations:
                item_id = new_id()
                thread = thread or item_id
                sessions = read_sessions()
                preferred_session = str(item.get("to_session", ""))[:64]
                selected = (sessions.get(preferred_session)
                            if preferred_session and session_alive(preferred_session, sessions)
                            else None)
                target_agent = str(item.get("to_agent", ""))[:64]
                # An agent name is an address. A live listener under that name gets
                # the item wherever it is listening, even when another session owns
                # the mailbox - otherwise two tabs sharing a mailbox each receive the
                # other's work and neither can tell it apart from its own.
                target_listener = (_pick_live(sessions, mailbox=destination, agent=target_agent)
                                   or _pick_live(sessions, agent=target_agent)) if target_agent else None
                selected = selected or target_listener
                if not selected and (not item.get("targeted") or item.get("to_mailbox")):
                    selected = current_consumer(destination, sessions) or _pick_live(
                        sessions, mailbox=destination)
                stored = {
                    "id": item_id,
                    "delivery_id": delivery_id,
                    "thread": thread,
                    "reply_to": str(item.get("reply_to", ""))[:64],
                    "from": sender,
                    "from_agent": str(item.get("from_agent", ""))[:64],
                    "from_mailbox": str(item.get("from_mailbox") or "main")[:64],
                    "from_session": str(item.get("from_session", ""))[:64],
                    "to_agent": str(target_agent or (
                        selected.get("agent", "") if selected else ""))[:64],
                    "to_mailbox": destination,
                    "preferred_session": preferred_session,
                    "assigned_session": selected.get("session_id", "") if selected else "",
                    "broadcast": bool(item.get("broadcast")),
                    "targeted": bool(item.get("targeted")),
                    "mailbox_targeted": bool(item.get("to_mailbox")),
                    "fallback": item["fallback"] if item.get("fallback") in FALLBACKS else "hold",
                    "kind": item["kind"],
                    "status": item.get("status", ""),
                    "text": str(item.get("text", ""))[:200_000],
                    "meta": item.get("meta") if isinstance(item.get("meta"), dict) else {},
                    "files": [],
                    "received": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "received_ts": time.time(),
                    "state": "pending",
                    "claimed_by": "",
                    "claimed_mailbox": "",
                    "claimed_at": 0,
                    "presented_generation": 0,
                }
                for fname, raw in decoded_files:
                    fpath = FILES_DIR / f"{item_id}_{fname}"
                    fpath.write_bytes(raw)
                    stored["files"].append(
                        {"filename": fname, "size": len(raw), "stored_path": str(fpath)})
                atomic_write_json(INBOX_DIR / f"{item_id}.json", stored)
                stored_items.append(stored)

        for stored in stored_items:
            _update_outstanding_request(stored)
        touch_activity("recv")
        first = stored_items[0]
        self._json(200, {"ok": True, "id": first["id"], "thread": first["thread"],
                         "ids": [stored["id"] for stored in stored_items]})
        if self.notify_command:
            summary = f"{first['from']}: {first['kind']} - {first['text'][:120]}"
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
    Handler.default_mailbox = configured_default_mailbox(cfg)
    Handler.mailboxes = registered_mailboxes(cfg)
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
        working_labels()                      # prune spent markers before filtering
        working = working_labels(herald_working_pids())
        blocked = awaiting_human()
        write_status({"pid": os.getpid(), "version": __version__, "me": me,
                      "listen": listen, "started": started, "heartbeat": now,
                      "queued": sum(1 for _ in QUEUE_DIR.glob("*/*.json")),
                      "working": len(working),
                      "working_agents": working_summary(working)[:4],
                      "blocked": len(blocked),
                      "blocked_agents": working_summary(blocked)[:4]})
        try:
            cfg = load_config()
        except (SystemExit, OSError, json.JSONDecodeError):
            cfg = None
        if cfg:
            # pick up newly issued/removed peer tokens without needing a restart
            Handler.peer_by_token = {p["issued_token"]: name
                                     for name, p in cfg.get("peers", {}).items() if p.get("issued_token")}
            Handler.default_mailbox = configured_default_mailbox(cfg)
            Handler.mailboxes = registered_mailboxes(cfg)
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


def _pick_live(sessions=None, mailbox=None, agent=None, mode="general"):
    sessions = sessions or read_sessions()
    live = live_sessions(sessions, mailbox=mailbox, mode=mode, agent=agent)
    return max(live, key=lambda session: session.get("heartbeat", 0)) if live else None


def _route(cfg):
    """Assign each open item to one eligible listener instance."""
    sessions = read_sessions()
    with state_lock():
        for path in INBOX_DIR.glob("*.json"):
            try:
                item = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if item_state(item) == "handled":
                continue
            preferred = item.get("preferred_session", "")
            if preferred and session_alive(preferred, sessions):
                if item.get("assigned_session") != preferred:
                    item["assigned_session"] = preferred
                    atomic_write_json(path, item)
                continue
            if item.get("targeted") and item.get("to_agent"):
                destination = item.get("to_mailbox") or "main"
                chosen = (_pick_live(sessions, mailbox=destination, agent=item["to_agent"])
                          or _pick_live(sessions, agent=item["to_agent"]))
                if chosen:
                    next_session = chosen["session_id"]
                    if item.get("assigned_session") != next_session:
                        item["assigned_session"] = next_session
                        atomic_write_json(path, item)
                    continue
                if item.get("targeted") and not item.get("mailbox_targeted"):
                    if item.get("assigned_session"):
                        item["assigned_session"] = ""
                        atomic_write_json(path, item)
                    continue
            assigned = item.get("assigned_session", "")
            if active_assignment(item, sessions):
                continue
            destination = item.get("to_mailbox") or configured_default_mailbox(cfg)
            chosen = current_consumer(destination, sessions) or _pick_live(
                sessions, mailbox=destination)
            next_session = chosen.get("session_id", "") if chosen else ""
            if next_session != assigned:
                item["assigned_session"] = next_session
                atomic_write_json(path, item)


def _reap(cfg):
    """Prune dead listeners and apply explicit target fallback policies."""
    now = time.time()
    sessions = read_sessions()
    for session_id in list(sessions):
        if not session_alive(session_id, sessions):
            try:
                (SESSIONS_DIR / f"{sanitize_filename(session_id)}.json").unlink()
            except OSError:
                pass
            sessions.pop(session_id, None)
    notices = []
    with state_lock():
        for path in INBOX_DIR.glob("*.json"):
            try:
                item = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            assigned = item.get("assigned_session", "")
            if assigned and not session_alive(assigned, sessions):
                item["assigned_session"] = ""
            if (item_state(item) == "handled" or item.get("claimed_by")
                    or item.get("unpinned") or item.get("bounced")):
                atomic_write_json(path, item)
                continue
            targeted = item.get("targeted") or item.get("mailbox_targeted")
            if not targeted or now - item.get("received_ts", 0) < TARGET_GIVEUP:
                atomic_write_json(path, item)
                continue
            target = item.get("to_mailbox") if item.get("mailbox_targeted") else item.get("to_agent")
            target_live = (current_consumer(target, sessions) if item.get("mailbox_targeted")
                           else _pick_live(sessions, agent=target))
            if target_live or item.get("fallback", "hold") == "hold":
                atomic_write_json(path, item)
                continue
            target_type = "mailbox" if item.get("mailbox_targeted") else "agent"
            if item.get("fallback") == "bounce":
                item.update(bounced=True, state="handled", handled_at=now)
                notices.append((item, target,
                                f"Undeliverable: {target_type} '{target}' was unavailable for your "
                                f"{item.get('kind')} in thread {item.get('thread')}; not reassigned.",
                                "undeliverable"))
            else:
                item.update(unpinned=True, targeted=False, mailbox_targeted=False,
                            to_agent="", to_mailbox=configured_default_mailbox(cfg), assigned_session="")
                notices.append((item, target,
                                f"Reassigned: {target_type} '{target}' was unavailable, so your "
                                f"{item.get('kind')} in thread {item.get('thread')} went to "
                                f"the default mailbox for {cfg['me']}.", "reassigned"))
            atomic_write_json(path, item)
    for item, target, text_value, intent in notices:
        _notify_origin(cfg, item, target, text_value, intent)


def _notify_origin(cfg, item, target, text, intent):
    peer = item.get("from", "")
    if peer not in cfg.get("peers", {}):
        return
    payload = {"kind": "message", "text": text,
               "thread": item.get("thread", ""), "reply_to": item.get("id", ""),
               "to_agent": item.get("from_agent", ""),
               "to_mailbox": item.get("from_mailbox") or "main",
               "to_session": item.get("from_session", ""),
               "fallback": "hold",
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
    wire_payload = {k: v for k, v in payload.items() if not k.startswith("_")}
    wire_payload["from"] = cfg["me"]
    req = urllib.request.Request(
        peer["url"].rstrip("/") + "/send",
        data=json.dumps(wire_payload).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {peer['token']}"},
    )
    touch_activity("send")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _record_outbox(payload, result, peer_name):
    ensure_dirs()
    record = {k: v for k, v in payload.items() if not k.startswith("_")}
    record["files"] = [{k: v for k, v in f.items() if k != "data_b64"}
                       for f in payload.get("files", [])]
    record.update(id=result["id"], thread=result["thread"], to=peer_name,
                  remote_ids=result.get("ids") or [result["id"]],
                  sent=time.strftime("%Y-%m-%d %H:%M:%S"))
    with state_lock():
        if payload.get("_expects_terminal"):
            record["state"] = "awaiting_terminal"
            record["awaiting_reply_ids"] = list(record["remote_ids"])
            for path in INBOX_DIR.glob("*.json"):
                try:
                    response = json.loads(path.read_text())
                except (OSError, json.JSONDecodeError):
                    continue
                if response.get("reply_to") not in record["remote_ids"]:
                    continue
                if not ((response.get("kind") == "result"
                         and response.get("status") in ("accepted", "working"))
                        or response.get("meta", {}).get("herald_intent") == "ack"):
                    record["awaiting_reply_ids"] = [item_id
                                                     for item_id in record["awaiting_reply_ids"]
                                                     if item_id != response.get("reply_to")]
            if not record["awaiting_reply_ids"]:
                record["state"] = "handled"
                record["handled_at"] = time.time()
        atomic_write_json(OUTBOX_DIR / f"{result['id']}.json", record)


def enqueue(peer_name, payload):
    d = QUEUE_DIR / sanitize_filename(peer_name)
    d.mkdir(parents=True, exist_ok=True)
    payload.setdefault("_qid", new_id())
    atomic_write_json(d / f"{payload['_qid']}.json", payload)
    return len(list(d.glob("*.json")))


def record_failed_delivery(peer_name, payload, error):
    failed = dict(payload)
    failed["_peer"] = peer_name
    failed["_delivery_error"] = error
    failed["_failed_at"] = time.time()
    item_id = payload.get("delivery_id") or payload.get("_qid") or new_id()
    atomic_write_json(FAILED_DIR / f"{sanitize_filename(item_id)}.json", failed)


def flush_queue(cfg, peer_name, verbose=False):
    """Retry queued items for a peer, oldest first. Stops at the first
    unreachable error (peer still down); records items the peer rejects."""
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
            error = f"rejected ({e.code} {e.reason})"
            _apply_source_delivery(payload, "failed", error)
            record_failed_delivery(peer_name, payload, error)
            f.unlink()
            if verbose:
                print(f"  failed queued item for {peer_name}: rejected ({e.code})")
            continue
        except urllib.error.URLError:
            break
        _record_outbox(payload, result, peer_name)
        _apply_source_delivery(payload, "delivered")
        f.unlink()
        sent += 1
    if sent and verbose:
        print(f"Flushed {sent} queued item(s) to {peer_name}")
    return sent


def deliver(cfg, peer_name, payload, queue_on_fail=True):
    peer = cfg.get("peers", {}).get(peer_name)
    if not peer:
        sys.exit(f"Unknown peer '{peer_name}'. Known: {', '.join(cfg.get('peers', {}))}")
    payload.setdefault("delivery_id", new_delivery_id())
    payload.setdefault("from_agent", sender_agent())
    payload.setdefault("from_mailbox", mailbox_name(cfg))
    flush_queue(cfg, peer_name)
    try:
        result = _post(cfg, peer, payload)
    except urllib.error.HTTPError as e:
        error = f"rejected ({e.code} {e.reason})"
        _apply_source_delivery(payload, "failed", error)
        record_failed_delivery(peer_name, payload, error)
        sys.exit(f"Send to {peer_name} rejected ({e.code} {e.reason}) - "
                 f"check the peer token/URL; not queued.")
    except urllib.error.URLError as e:
        if not queue_on_fail:
            sys.exit(f"Send to {peer_name} failed: {e.reason}")
        depth = enqueue(peer_name, payload)
        reason = getattr(e, "reason", e)
        print(f"Peer '{peer_name}' is unreachable ({reason}) - queued for retry "
              f"({depth} pending). Delivers on next contact, or run: herald flush {peer_name}")
        _apply_source_delivery(payload, "queued")
        return {"delivery_state": "queued", "delivery_id": payload["delivery_id"]}

    _record_outbox(payload, result, peer_name)
    _apply_source_delivery(payload, "delivered")
    result["delivery_state"] = "delivered"
    result["delivery_id"] = payload["delivery_id"]
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
        payload["_expects_terminal"] = True
    if args.thread:
        payload["thread"] = args.thread
    if args.all:
        payload["broadcast"] = True
    else:
        if args.mailbox:
            payload["to_mailbox"] = args.mailbox
            payload["fallback"] = args.fallback
        if args.agent:
            payload["to_agent"] = args.agent
            payload["targeted"] = True
            payload["fallback"] = args.fallback
    attach_files(payload, args.file)
    result = deliver(cfg, args.peer, payload)
    if result["delivery_state"] == "queued":
        return
    print(f"Delivered to {args.peer}: {payload['kind']} id {result['id']}, thread {result['thread']}")


def find_inbox_item(item_id):
    path = INBOX_DIR / f"{item_id}.json"
    if not path.exists():
        sys.exit(f"No inbox item {item_id}")
    return json.loads(path.read_text())


def cmd_reply(cfg, args):
    orig = find_inbox_item(args.id)
    meta = parse_meta(args.meta)
    payload = {
        "kind": "message",
        "text": args.message or "",
        "thread": orig["thread"],
        "reply_to": orig["id"],
        "meta": meta,
        "_source_item_id": orig["id"],
        "_source_effect": "ack" if meta.get("herald_intent") == "ack" else "final",
    }
    if args.all:
        payload["broadcast"] = True
    else:
        target = args.agent or orig.get("from_agent") or ""
        target_mailbox = args.mailbox or orig.get("from_mailbox") or "main"
        payload["to_mailbox"] = target_mailbox
        payload["fallback"] = args.fallback
        if target:
            payload["to_agent"] = target
            payload["targeted"] = True
        if orig.get("from_session") and not args.agent and not args.mailbox:
            payload["to_session"] = orig["from_session"]
    attach_files(payload, args.file)
    result = deliver(cfg, orig["from"], payload)
    if result["delivery_state"] == "queued":
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
        "_source_item_id": orig["id"],
        "_source_effect": "ack" if args.status in ("accepted", "working") else "final",
    }
    if args.all:
        payload["broadcast"] = True
    else:
        target = args.agent or orig.get("from_agent") or ""
        target_mailbox = args.mailbox or orig.get("from_mailbox") or "main"
        payload["to_mailbox"] = target_mailbox
        payload["fallback"] = args.fallback
        if target:
            payload["to_agent"] = target
            payload["targeted"] = True
        if orig.get("from_session") and not args.agent and not args.mailbox:
            payload["to_session"] = orig["from_session"]
    attach_files(payload, args.file)
    result = deliver(cfg, orig["from"], payload)
    if args.status == "accepted":   # the protocol's "waiting for my human to decide"
        ring_bell(cfg)
    if result["delivery_state"] == "queued":
        return
    print(f"Sent {args.status} result to {orig['from']} in thread {orig['thread']} (id {result['id']})")


def _hook_payload(timeout=0.5):
    """An editor hook's JSON payload from stdin, or {} when there is none.

    A hook writes its JSON and closes, so anything still open and silent is not
    a hook - waiting on it would hang the caller instead of stamping."""
    if sys.stdin.isatty():
        return {}
    try:
        if not select.select([sys.stdin], [], [], timeout)[0]:
            return {}
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _repo_label(cwd):
    """Name the repository rather than the directory the hook happened to fire
    in - an agent's cwd is often a folder deep inside the tree, whose basename
    says nothing about which work it is.

    The walk is deliberate rather than a call to git: `git rev-parse
    --show-toplevel` refuses to cross a filesystem boundary without
    GIT_DISCOVERY_ACROSS_FILESYSTEM, so it fails on a repo under /mnt/c and
    would leave every WSL turn unlabelled."""
    path = Path(cwd)
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists():
            return candidate.name
    return path.name


def _hook_identity(args):
    """Marker identity for one agent turn, preferring an editor hook's JSON
    payload on stdin over the environment - a hook runs with neither
    HERALD_AGENT nor a herald session of its own."""
    payload = {} if (args.key and args.label) else _hook_payload()
    key = args.key or payload.get("session_id") or os.environ.get("HERALD_AGENT") or "default"
    label = args.label or os.environ.get("HERALD_AGENT") or ""
    if not label:
        cwd = str(payload.get("cwd") or "")
        label = _repo_label(cwd) if cwd else str(key)[:8]
    return str(key), str(label)


def cmd_activity(cfg, args):
    """Record that this agent turn is running, or that it has handed back.

    Prints nothing when it sets a state: Claude Code feeds hook stdout back to
    the model on some events, so anything written here would cost tokens on
    every tool call."""
    if not args.state:
        for name, labels in (("turns running", working_labels()),
                             ("on herald work", working_labels(herald_working_pids())),
                             ("waiting on you", awaiting_human())):
            summary = ", ".join(working_summary(labels))
            print(f"{len(labels)} {name}" + (f": {summary}" if labels else ""))
        return
    key, label = _hook_identity(args)
    if args.state == "working":
        mark_working(key, label, harness_pid())
    else:
        clear_working(key)


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
    working = s.get("working_agents") or []
    blocked = s.get("blocked_agents") or []
    busy = f", working: {', '.join(working)}" if working else ""
    busy += f", waiting on you: {', '.join(blocked)}" if blocked else ""
    print(f"herald v{s.get('version', '?')} daemon: running - {s.get('me')} on {s.get('listen')} "
          f"(pid {s.get('pid')}, up since {s.get('started')}, {s.get('queued', 0)} queued{busy})")


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
    state = f" [{item_state(i)}]" if direction == "in" else f" [{i.get('state')}]" if i.get("state") else ""
    target = (f" ->{i['to_agent']} mailbox {i.get('to_mailbox', 'main')}"
              if i.get("to_agent") else f" ->{i.get('to_mailbox', 'main')}")
    files = f" ({len(i['files'])} file{'s' if len(i['files']) != 1 else ''})" if i.get("files") else ""
    claimed = f" (claimed: {i['claimed_by']})" if i.get("claimed_by") else ""
    preview = i["text"][:80].replace("\n", " ")
    return f"{i['id']}  {i['kind']:<7}{status}{state}{target} {who}{files}{claimed}  {preview}"


def _item_matches_mailbox(item, mailbox, agent):
    if (item.get("to_mailbox") or "main") != mailbox:
        return False
    if (item.get("targeted") and not item.get("mailbox_targeted")
            and item.get("to_agent") and item.get("to_agent") != agent
            and not item.get("unpinned")):
        return False
    return True


def _is_progress_item(item):
    return ((item.get("kind") == "result" and item.get("status") in ("accepted", "working"))
            or item.get("meta", {}).get("herald_intent") == "ack")


def orphaned(item, mailbox, sessions=None):
    """The item is pinned to, or claimed by, an agent with no live session. The
    reaper skips items that were already claimed, so an explicit close or reopen
    from the same mailbox is the only way one of these leaves the open list."""
    if (item.get("to_mailbox") or "main") != mailbox:
        return False
    owners = {item.get("to_agent", ""), item.get("claimed_by", "")} - {""}
    return bool(owners) and not any(_pick_live(sessions, agent=owner) for owner in owners)


def _claim_item(item_id, agent, mailbox, listener=None, force=False, allow_orphan=False):
    path = INBOX_DIR / f"{item_id}.json"
    with state_lock():
        if not path.exists():
            sys.exit(f"No inbox item {item_id}")
        item = json.loads(path.read_text())
        orphan = allow_orphan and orphaned(item, mailbox)
        if not force and not orphan and not _item_matches_mailbox(item, mailbox, agent):
            target = item.get("to_agent") or item.get("to_mailbox") or "main"
            sys.exit(f"Item {item_id} is addressed to '{target}', not this agent mailbox.")
        assignment = active_assignment(item)
        if (not force and assignment and assignment.get("agent") != agent):
            sys.exit(f"Item {item_id} is assigned to live agent '{assignment.get('agent')}'.")
        owner_mailbox = item.get("claimed_mailbox", "")
        if owner_mailbox and owner_mailbox != mailbox and not force:
            sys.exit(f"Item {item_id} is active in mailbox '{owner_mailbox}'.")
        if (not force and not orphan and item.get("claimed_by") not in ("", agent)
                and item_state(item) != "pending"):
            sys.exit(f"Item {item_id} is active under agent '{item.get('claimed_by')}'. "
                     "Use `herald resume` to take over the mailbox.")
        if not force:
            item["claimed_by"] = agent
            item["claimed_mailbox"] = mailbox
            item["claimed_pid"] = harness_pid()
            item["claimed_at"] = item.get("claimed_at") or time.time()
            if item_state(item) == "pending":
                item["state"] = "handled" if _is_progress_item(item) else "active"
                if item["state"] == "handled":
                    item["handled_at"] = time.time()
            if listener:
                item["assigned_session"] = listener["session_id"]
                item["presented_generation"] = listener.get("generation", 0)
            atomic_write_json(path, item)
        return item


def _claim_next(listener):
    sessions = read_sessions()
    with state_lock():
        candidates = []
        for path in INBOX_DIR.glob("*.json"):
            try:
                item = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            state = item_state(item)
            if state == "handled":
                continue
            if not _item_matches_mailbox(item, listener["mailbox"], listener["agent"]):
                continue
            if listener["mode"] == "ask":
                if not listener.get("request_id") or item.get("reply_to") != listener["request_id"]:
                    continue
            elif state != "pending" and item.get("presented_generation", 0) >= listener["generation"]:
                continue
            preferred = item.get("preferred_session", "")
            if preferred and preferred != listener["session_id"] and session_alive(preferred, sessions):
                continue
            assigned = item.get("assigned_session", "")
            assignment = active_assignment(item, sessions)
            if assignment and assigned != listener["session_id"]:
                continue
            candidates.append((item.get("received_ts", 0), item["id"], path, item))
        if not candidates:
            return None
        _, _, path, item = min(candidates)
        item["claimed_by"] = listener["agent"]
        item["claimed_mailbox"] = listener["mailbox"]
        item["claimed_pid"] = listener.get("harness_pid")
        item["claimed_at"] = item.get("claimed_at") or time.time()
        item["assigned_session"] = listener["session_id"]
        item["presented_generation"] = listener.get("generation", 0)
        item["state"] = "handled" if listener["mode"] == "ask" or _is_progress_item(item) else (
            "active" if item_state(item) == "pending" else item_state(item))
        if item["state"] == "handled":
            item["handled_at"] = time.time()
        atomic_write_json(path, item)
        return item


def _write_files(item, out_dir=None):
    for attached in item.get("files", []):
        out = Path(out_dir or ".") / attached["filename"]
        out.write_bytes(Path(attached["stored_path"]).read_bytes())
        print(f"File written to {out.resolve()}", flush=True)


def _show_item(item, out_dir=None):
    shown = {k: v for k, v in item.items() if k != "files"}
    shown["files"] = [f["filename"] for f in item.get("files", [])]
    print(json.dumps(shown, indent=2))
    _write_files(item, out_dir)


def inbox_summary(item):
    """The fields a listing needs, without the text body or file contents - so a
    caller rendering a menu does not have to reimplement item_state or carry
    megabytes of attachment through a pipe."""
    return {
        "id": item["id"],
        "kind": item.get("kind", ""),
        "state": item_state(item),
        "status": item.get("status", ""),
        "from": item.get("from", ""),
        "from_agent": item.get("from_agent", ""),
        "to_agent": item.get("to_agent", ""),
        "to_mailbox": item.get("to_mailbox") or "main",
        "thread": item.get("thread", ""),
        "received": item.get("received", ""),
        "claimed_by": item.get("claimed_by", ""),
        "files": len(item.get("files", [])),
        "preview": item.get("text", "")[:120].replace("\n", " "),
    }


def cmd_inbox(cfg, args):
    ensure_dirs()
    items = [json.loads(p.read_text()) for p in sorted(INBOX_DIR.glob("*.json"))]
    if args.history:
        items = [i for i in items if item_state(i) == "handled"]
    elif args.unclaimed:
        items = [i for i in items if item_state(i) == "pending"]
    else:
        items = [i for i in items if item_state(i) != "handled"]
    if args.mine:
        items = [i for i in items if _item_matches_mailbox(
            i, mailbox_name(cfg), agent_name())]
    if args.json:
        print(json.dumps([inbox_summary(i) for i in items]))
        return
    if not items:
        print("No matching inbox items")
        return
    for i in items:
        flag = " " if i.get("claimed_by") else "*"
        print(f"{flag} {summarise(i, 'in')}")


def cmd_read(cfg, args):
    item = _claim_item(args.id, agent_name(), mailbox_name(cfg), force=args.force)
    _show_item(item, args.out)


def cmd_close(cfg, args):
    item = _claim_item(args.id, agent_name(), mailbox_name(cfg), allow_orphan=True)
    update_inbox_item(item["id"], state="handled", handled_at=time.time())
    print(f"Closed inbox item {item['id']}")


def cmd_rm(cfg, args):
    """Delete an inbox record outright, for clearing debris while debugging.

    Unlike close this keeps no history, so the item leaves `herald thread` and
    `herald reply <id>` can no longer answer it. The delivery-id record goes with
    it, so a delivery the sender is still retrying can arrive again as a new item.
    """
    with state_lock():
        item = find_inbox_item(args.id)
        assignment = active_assignment(item)
        if assignment and not args.force and assignment.get("agent") != agent_name():
            sys.exit(f"Item {args.id} is assigned to live agent "
                     f"'{assignment.get('agent')}'. Use --force to delete it anyway.")
        files = 0
        for attached in item.get("files", []):
            stored = Path(attached.get("stored_path", ""))
            if stored.parent == FILES_DIR and stored.exists():
                stored.unlink()
                files += 1
        (INBOX_DIR / f"{item['id']}.json").unlink()
    note = f" and {files} file{'s' if files != 1 else ''}" if files else ""
    print(f"Deleted inbox item {item['id']}{note}")


def cmd_reopen(cfg, args):
    mailbox = mailbox_name(cfg)
    item = find_inbox_item(args.id)
    orphan = orphaned(item, mailbox)
    if not _item_matches_mailbox(item, mailbox, agent_name()) and not orphan:
        sys.exit(f"Item {args.id} belongs to another mailbox")
    # a pin to a session that no longer exists would make the item invisible again
    released = ({"unpinned": True, "targeted": False, "to_agent": ""}
                if orphan and item.get("to_agent") and item["to_agent"] != agent_name() else {})
    update_inbox_item(item["id"], state="pending", claimed_by="", claimed_mailbox="",
                      claimed_pid=None, claimed_at=0, handled_at=0, assigned_session="",
                      presented_generation=0, **released)
    print(f"Reopened inbox item {item['id']}"
          + (f", released from the gone session '{item['to_agent']}'" if released else ""))


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


def _open_outstanding_requests(mailbox):
    records = []
    for path in OUTBOX_DIR.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if record.get("state") == "awaiting_terminal" and record.get("from_mailbox", "main") == mailbox:
            records.append(record)
    return records


def cmd_wait(cfg, args):
    listener = register_listener(cfg, mode="general", takeover=getattr(args, "resume", False))
    atexit.register(clear_session, listener["session_id"])
    signal.signal(signal.SIGTERM,
                  lambda *a: (clear_session(listener["session_id"]), sys.exit(0)))
    if getattr(args, "resume", False):
        outstanding = _open_outstanding_requests(listener["mailbox"])
        if outstanding:
            print(f"{len(outstanding)} outgoing request(s) still await a final reply", flush=True)
    deadline = time.time() + args.timeout if args.timeout else None
    while True:
        if not consumer_is_current(listener):
            clear_session(listener["session_id"])
            print(f"Mailbox '{listener['mailbox']}' moved to another agent listener")
            return
        write_session(listener)
        item = _claim_next(listener)
        if item:
            if args.read or getattr(args, "resume", False):
                _show_item(item, args.out)
            else:
                status = f" [{item['status']}]" if item.get("status") else ""
                print(f"NEW {item['kind']}{status} from {item['from']}: "
                      f"id {item['id']}, thread {item['thread']}")
            clear_session(listener["session_id"])
            return
        if deadline and time.time() > deadline:
            print("Timed out with no new items")
            clear_session(listener["session_id"])
            sys.exit(2)
        time.sleep(1)


def cmd_resume(cfg, args):
    args.resume = True
    args.read = True
    cmd_wait(cfg, args)


def cmd_ask(cfg, args):
    """Send a task/message and block for the reply, returning it in one command
    - so a synchronous request/reply is a single turn, not send + wait + read.
    Only for reachable peers; an offline peer falls back to the async queue."""
    if args.task and args.message:
        sys.exit("Use --task or --message, not both")
    if not (args.task or args.message):
        sys.exit("Nothing to ask: use --task or --message")
    listener = register_listener(cfg, mode="ask")
    atexit.register(clear_session, listener["session_id"])
    signal.signal(signal.SIGTERM,
                  lambda *a: (clear_session(listener["session_id"]), sys.exit(0)))
    payload = {
        "kind": "task" if args.task else "message",
        "text": args.task or args.message,
        "meta": parse_meta(args.meta),
        "from_session": listener["session_id"],
        "_expects_terminal": True,
    }
    if args.task:
        payload["status"] = "pending"
    if args.mailbox:
        payload["to_mailbox"] = args.mailbox
        payload["fallback"] = args.fallback
    if args.agent:
        payload["to_agent"] = args.agent
        payload["targeted"] = True
        payload["fallback"] = args.fallback
    attach_files(payload, args.file)
    result = deliver(cfg, args.peer, payload)
    if result["delivery_state"] == "queued":
        clear_session(listener["session_id"])
        print("The request is queued. Run `herald resume` to receive the reply.")
        return
    thread = result["thread"]
    listener["request_id"] = result["id"]
    write_session(listener, waiting_on=f"reply:{result['id']}")
    print(f"Sent {payload['kind']} to {args.peer} (thread {thread}); waiting for reply...")
    idle_timeout = args.timeout or 300
    deadline = time.time() + idle_timeout
    progress = None
    while time.time() < deadline:
        write_session(listener, waiting_on=f"reply:{result['id']}")
        item = _claim_next(listener)
        if item:
            if _is_progress_item(item):
                # The peer has committed to a later reply, so the wait restarts from here
                # rather than expiring on the original deadline.
                progress = item
                label = item.get("status") or "ack"
                print(f"[{label}] {item['from']}: {item['text']}", flush=True)
                # A progress item can carry finished work, so write its files here too -
                # otherwise the text names an attachment the caller never receives.
                _write_files(item, args.out)
                print(f"-- a final reply is expected; waiting up to {idle_timeout}s "
                      "from now for it", flush=True)
                deadline = time.time() + idle_timeout
                continue
            _show_item(item, args.out)
            clear_session(listener["session_id"])
            return
        time.sleep(1)
    clear_session(listener["session_id"])
    if progress:
        print(f"{args.peer} acknowledged but sent no final reply within {idle_timeout}s "
              f"of that acknowledgement (still open in thread {thread}).")
    else:
        print(f"No reply from {args.peer} within {idle_timeout}s (still open in thread {thread}).")
    print("Nothing is listening now - run `herald resume` to receive the reply.")
    sys.exit(2)


def cmd_bell(cfg, args):
    """Ring the terminal because the turn cannot continue without the human."""
    if ring_bell(cfg):
        print("Rang the terminal bell")
    else:
        print("No terminal to ring (bell disabled, or no terminal owns this session)")


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
    live = sorted((session_id, session) for session_id, session in sessions.items()
                  if session_alive(session_id, sessions))
    if not live:
        print("No live agent sessions")
        return
    for session_id, s in live:
        age = now - s.get("heartbeat", 0)
        print(f"{s.get('agent', '?')}  mailbox {s.get('mailbox', 'main')}  "
              f"mode {s.get('mode', 'general')}  listener {session_id}  "
              f"host {s.get('host', '?')} pid {s.get('pid', '?')}  "
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
    atomic_write_json(CONFIG_PATH, cfg)
    payload = {
        "kind": "message",
        "text": f"{cfg['me']} would like to connect - accept with: herald accept <item-id>",
        "meta": {"herald_intent": "introduce", "name": cfg["me"],
                 "url": url, "token": issued},
    }
    result = deliver(cfg, args.peer, payload)
    if result["delivery_state"] == "queued":
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
    atomic_write_json(CONFIG_PATH, cfg)
    payload = {"kind": "message",
               "text": f"{cfg['me']} accepted your introduction - connected.",
               "thread": item["thread"], "reply_to": item["id"],
               "meta": {"herald_intent": "accepted", "name": cfg["me"]},
               "to_mailbox": item.get("from_mailbox") or "main",
               "to_agent": item.get("from_agent", ""),
               "to_session": item.get("from_session", ""),
               "fallback": "hold",
               "_source_item_id": item["id"], "_source_effect": "final"}
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
        atomic_write_json(CONFIG_PATH, cfg)
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
    atomic_write_json(CONFIG_PATH, cfg)
    print(f"Peer '{args.name}' {'added' if args.action == 'add' else 'removed'}")


def cmd_mailbox(cfg, args):
    names = registered_mailboxes(cfg)
    if args.action == "list":
        for name in names:
            marker = " (default)" if name == configured_default_mailbox(cfg) else ""
            print(f"{name}{marker}")
        return
    if not args.name:
        sys.exit("mailbox add/remove/default needs a name")
    if not valid_mailbox_name(args.name):
        sys.exit("Mailbox names can contain letters, numbers, '-', '_', and '.' (maximum 64)")
    if args.action == "add":
        if args.name not in names:
            names.append(args.name)
    elif args.action == "remove":
        if args.name == configured_default_mailbox(cfg):
            sys.exit("Cannot remove the default mailbox")
        if live_sessions(mailbox=args.name):
            sys.exit(f"Cannot remove mailbox '{args.name}' while it has a live listener")
        open_items = []
        for path in INBOX_DIR.glob("*.json"):
            try:
                item = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            if item.get("to_mailbox") == args.name and item_state(item) != "handled":
                open_items.append(item)
        if open_items:
            sys.exit(f"Cannot remove mailbox '{args.name}': {len(open_items)} open item(s) remain")
        names = [name for name in names if name != args.name]
    else:
        if args.name not in names:
            names.append(args.name)
        cfg["default_mailbox"] = args.name
    cfg["mailboxes"] = names
    atomic_write_json(CONFIG_PATH, cfg)
    print(f"Mailbox '{args.name}' updated")


def cmd_init(cfg_unused, args):
    HERALD_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not args.force:
        sys.exit(f"{CONFIG_PATH} already exists (use --force to overwrite)")
    cfg = {
        "me": args.me,
        "listen": {"host": "auto", "port": args.port},
        "default_mailbox": "main",
        "mailboxes": ["main"],
        "peers": {},
    }
    atomic_write_json(CONFIG_PATH, cfg)
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
    sp.add_argument("--mailbox", help="address a durable mailbox at the peer")
    sp.add_argument("--fallback", choices=FALLBACKS, default="hold",
                    help="if the target session never appears: broadcast (release to any), "
                         "hold (keep pinned), bounce (return undeliverable to you)")
    sp.add_argument("--all", action="store_true",
                    help="deliver to all registered recipient mailboxes")

    sp = sub.add_parser("reply", help="reply to an inbox item (peer/thread inferred)")
    sp.add_argument("id")
    sp.add_argument("--message", "-m", required=True)
    sp.add_argument("--file", "-f", action="append")
    sp.add_argument("--meta", action="append")
    sp.add_argument("--agent", help="override the target session (defaults to the sender's)")
    sp.add_argument("--mailbox", help="override the target mailbox")
    sp.add_argument("--fallback", choices=FALLBACKS, default="hold")
    sp.add_argument("--all", action="store_true", help="deliver to all recipient mailboxes")

    sp = sub.add_parser("result", help="send a task status/result back to its sender")
    sp.add_argument("id", help="the inbox task item id")
    sp.add_argument("--status", required=True, choices=STATUSES)
    sp.add_argument("--message", "-m")
    sp.add_argument("--file", "-f", action="append")
    sp.add_argument("--meta", action="append")
    sp.add_argument("--agent", help="override the target session (defaults to the sender's)")
    sp.add_argument("--mailbox", help="override the target mailbox")
    sp.add_argument("--fallback", choices=FALLBACKS, default="hold")
    sp.add_argument("--all", action="store_true", help="deliver to all recipient mailboxes")

    sp = sub.add_parser("inbox", help="list received items")
    sp.add_argument("--unclaimed", action="store_true")
    sp.add_argument("--history", action="store_true", help="show handled items instead of open work")
    sp.add_argument("--mine", action="store_true",
                    help="only items for this mailbox and exact agent targets")
    sp.add_argument("--json", action="store_true",
                    help="machine-readable listing, one object per item, [] when empty")

    sp = sub.add_parser("read", help="show an item (writes files to cwd), claim it for this agent")
    sp.add_argument("id")
    sp.add_argument("--out")
    sp.add_argument("--force", action="store_true", help="read without claiming, even if claimed")

    sp = sub.add_parser("close", help="mark an inbox item handled")
    sp.add_argument("id")

    sp = sub.add_parser("reopen", help="return a handled inbox item to pending")
    sp.add_argument("id")

    sp = sub.add_parser("rm", help="delete an inbox item and its files, keeping no history")
    sp.add_argument("id")
    sp.add_argument("--force", action="store_true",
                    help="delete even when a live session other than this one holds it")

    sp = sub.add_parser("introduce", help="send a peer my address+token so they can add me")
    sp.add_argument("peer")

    sp = sub.add_parser("accept", help="accept an introduction: add them as a peer, confirm back")
    sp.add_argument("id")

    sp = sub.add_parser("peer", help="manage peers")
    sp.add_argument("action", choices=["add", "list", "remove", "issue"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("url", nargs="?")
    sp.add_argument("token", nargs="?")

    sp = sub.add_parser("mailbox", help="manage durable mailboxes")
    sp.add_argument("action", choices=["list", "add", "remove", "default"])
    sp.add_argument("name", nargs="?")

    sub.add_parser("access", help="audit who can reach whom and who is authenticated")

    sp = sub.add_parser("thread", help="show a whole conversation, both directions")
    sp.add_argument("id")

    sp = sub.add_parser("wait", help="block until eligible open work is available")
    sp.add_argument("--timeout", type=int, default=0)
    sp.add_argument("--read", action="store_true",
                    help="show and claim each new item on wake (folds in the read)")
    sp.add_argument("--out", help="directory for attached files (with --read)")

    sp = sub.add_parser("resume", help="take over a mailbox, surface open work, and wait")
    sp.add_argument("--timeout", type=int, default=0)
    sp.add_argument("--out", help="directory for attached files")

    sp = sub.add_parser("ask", help="send and block for the reply in one turn (reachable peers only)")
    sp.add_argument("peer")
    sp.add_argument("--message", "-m")
    sp.add_argument("--task", "-t", help="a task request (text is the request)")
    sp.add_argument("--file", "-f", action="append")
    sp.add_argument("--meta", action="append", help="key=value, repeatable")
    sp.add_argument("--agent", help="address a specific session of the peer")
    sp.add_argument("--mailbox", help="address a durable mailbox at the peer")
    sp.add_argument("--fallback", choices=FALLBACKS, default="hold")
    sp.add_argument("--timeout", type=int, default=300,
                    help="seconds to wait without progress; an acknowledgement restarts it")
    sp.add_argument("--out", help="directory for attached files in the reply")

    sub.add_parser("bell", help="ring the terminal: this turn cannot continue without the human")

    sp = sub.add_parser("ping", help="check a peer's daemon is up and its version (no agent woken)")
    sp.add_argument("peer", nargs="?", help="a single peer (default: all)")

    sp = sub.add_parser("flush", help="retry items queued for unreachable peers")
    sp.add_argument("peer", nargs="?", help="a single peer (default: all)")

    sub.add_parser("status", help="report whether the daemon is running")

    sub.add_parser("sessions", help="list agent sessions currently listening")

    sp = sub.add_parser("activity", help="record whether this agent turn is running (editor hook)")
    sp.add_argument("state", nargs="?", choices=("working", "idle"),
                    help="omit to report which agent turns are running now")
    sp.add_argument("--key", default="", help="marker identity (default: the hook's session id)")
    sp.add_argument("--label", default="", help="name shown in the tray tooltip")

    args = p.parse_args()
    identity_commands = {"send", "reply", "result", "read", "close", "reopen",
                         "rm", "wait", "resume", "ask"}
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
    selected_mailbox = os.environ.get("HERALD_MAILBOX")
    if (cfg and selected_mailbox and args.cmd != "mailbox"
            and selected_mailbox not in registered_mailboxes(cfg)):
        sys.exit(f"Unknown local mailbox '{selected_mailbox}'. Add it with: "
                 f"herald mailbox add {selected_mailbox}")
    {"init": cmd_init, "daemon": cmd_daemon, "send": cmd_send, "reply": cmd_reply,
     "result": cmd_result, "inbox": cmd_inbox, "read": cmd_read,
     "close": cmd_close, "reopen": cmd_reopen, "rm": cmd_rm, "peer": cmd_peer,
     "mailbox": cmd_mailbox,
     "introduce": cmd_introduce, "accept": cmd_accept, "ask": cmd_ask, "ping": cmd_ping,
     "bell": cmd_bell,
     "access": cmd_access, "thread": cmd_thread, "wait": cmd_wait,
     "resume": cmd_resume, "flush": cmd_flush,
     "status": cmd_status, "sessions": cmd_sessions,
     "activity": cmd_activity}[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
