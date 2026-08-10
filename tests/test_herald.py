"""Tests for herald. Stdlib unittest only (no third-party deps), matching the
tool's zero-dependency design.

Pure helpers are tested in-process; the protocol is tested end to end by running
real daemons on loopback (two identities, alice + bob) as subprocesses and
driving them through the CLI - the same setup used by hand during development,
made repeatable.

    python -m unittest discover -s tests
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERALD_PY = os.path.join(REPO, "herald.py")

# Isolate any accidental HERALD_DIR access during import of the module under test.
os.environ["HERALD_DIR"] = tempfile.mkdtemp(prefix="herald-import-")
sys.path.insert(0, REPO)
import herald  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class PureFunctions(unittest.TestCase):
    def test_sanitize_filename_strips_paths_and_dotdot(self):
        self.assertEqual(herald.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(herald.sanitize_filename("a\\b\\c.txt"), "c.txt")
        self.assertEqual(herald.sanitize_filename(".."), "_")
        self.assertEqual(herald.sanitize_filename(""), "unnamed")

    def test_parse_meta(self):
        self.assertEqual(herald.parse_meta(["a=1", "b=two"]), {"a": "1", "b": "two"})
        self.assertEqual(herald.parse_meta(None), {})
        with self.assertRaises(SystemExit):
            herald.parse_meta(["no-equals"])

    def test_new_id_format_and_uniqueness(self):
        import re
        a, b = herald.new_id(), herald.new_id()
        self.assertRegex(a, r"^\d{8}-\d{6}-[0-9a-f]{6}$")
        self.assertNotEqual(a, b)

    def test_agent_name_vs_sender_agent(self):
        saved = os.environ.get("HERALD_AGENT")
        try:
            os.environ.pop("HERALD_AGENT", None)
            self.assertEqual(herald.sender_agent(), "")          # unset -> blank (broadcast)
            self.assertNotEqual(herald.agent_name(), "")         # unset -> hostname fallback
            os.environ["HERALD_AGENT"] = "laptop-1"
            self.assertEqual(herald.sender_agent(), "laptop-1")
            self.assertEqual(herald.agent_name(), "laptop-1")
        finally:
            if saved is None:
                os.environ.pop("HERALD_AGENT", None)
            else:
                os.environ["HERALD_AGENT"] = saved

    def test_summarise_includes_key_fields(self):
        item = {"id": "X", "kind": "task", "from": "bob", "status": "pending",
                "to_agent": "laptop-1", "files": [], "claimed_by": "", "text": "do the thing"}
        line = herald.summarise(item, "in")
        for token in ("X", "task", "pending", "bob", "->laptop-1", "do the thing"):
            self.assertIn(token, line)


class Protocol(unittest.TestCase):
    """Two loopback daemons (alice + bob) driven through the CLI."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="herald-test-")
        self.homes = {"alice": os.path.join(self.root, "alice"),
                      "bob": os.path.join(self.root, "bob")}
        self.ports = {"alice": free_port(), "bob": free_port()}
        # per-peer inbound tokens: TA is what bob presents to reach alice; TB what alice presents to reach bob
        self.TA, self.TB = "tok-alice-issues-bob", "tok-bob-issues-alice"
        self._write_config("alice", "bob", issued=self.TA, token=self.TB)
        self._write_config("bob", "alice", issued=self.TB, token=self.TA)
        self.daemons = {}
        self.start_daemon("alice")
        self.start_daemon("bob")
        for name in ("alice", "bob"):
            self.assertTrue(self._wait_port(self.ports[name]), f"{name} daemon did not come up")

    def tearDown(self):
        for p in self.daemons.values():
            p.terminate()
        for p in self.daemons.values():
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- harness helpers ----

    def _write_config(self, me, peer, issued, token):
        os.makedirs(self.homes[me], exist_ok=True)
        cfg = {"me": me,
               "listen": {"host": "127.0.0.1", "port": self.ports[me]},
               "peers": {peer: {"url": f"http://127.0.0.1:{self.ports[peer]}",
                                "token": token, "issued_token": issued}}}
        with open(os.path.join(self.homes[me], "config.json"), "w") as f:
            json.dump(cfg, f)

    def _env(self, name, agent=None):
        env = dict(os.environ, HERALD_DIR=self.homes[name])
        env.pop("HERALD_AGENT", None)
        if agent:
            env["HERALD_AGENT"] = agent
        return env

    def start_daemon(self, name):
        p = subprocess.Popen([sys.executable, HERALD_PY, "daemon"], env=self._env(name),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.daemons[name] = p

    def stop_daemon(self, name):
        p = self.daemons.pop(name, None)
        if p:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

    def cli(self, name, *args, agent=None, timeout=20):
        return subprocess.run([sys.executable, HERALD_PY, *args], env=self._env(name, agent),
                              cwd=self.root, capture_output=True, text=True, timeout=timeout)

    def _wait_port(self, port, timeout=8):
        end = time.time() + timeout
        while time.time() < end:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/ping", timeout=1)
                return True
            except (urllib.error.URLError, OSError):
                time.sleep(0.1)
        return False

    @staticmethod
    def _load(path):
        with open(path) as f:
            return json.load(f)

    def inbox(self, name):
        d = os.path.join(self.homes[name], "inbox")
        if not os.path.isdir(d):
            return []
        return [self._load(os.path.join(d, f)) for f in sorted(os.listdir(d)) if f.endswith(".json")]

    def wait_for_inbox(self, name, predicate, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            for item in self.inbox(name):
                if predicate(item):
                    return item
            time.sleep(0.1)
        return None

    # ---- protocol tests ----

    def test_ping_reports_version_and_name(self):
        r = self.cli("alice", "ping", "bob")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(herald.__version__, r.stdout)
        self.assertIn("bob", r.stdout)

    def test_send_requires_agent_identity(self):
        r = self.cli("alice", "send", "bob", "-m", "hello bob")

        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HERALD_AGENT is required", r.stderr)

    def test_bad_token_rejected(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.ports['bob']}/send",
            data=json.dumps({"kind": "message", "text": "x"}).encode(),
            headers={"Authorization": "Bearer WRONG", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 401)

    def test_bad_kind_rejected(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.ports['bob']}/send",
            data=json.dumps({"kind": "nonsense", "text": "x"}).encode(),
            headers={"Authorization": f"Bearer {self.TB}", "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req)
        self.assertEqual(cm.exception.code, 400)

    def test_from_is_authenticated_not_spoofable(self):
        # POST to bob using alice's token but a forged 'from'; the stored sender must be 'alice'
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.ports['bob']}/send",
            data=json.dumps({"kind": "message", "text": "spoof attempt", "from": "mallory"}).encode(),
            headers={"Authorization": f"Bearer {self.TB}", "Content-Type": "application/json"})
        urllib.request.urlopen(req)
        item = self.wait_for_inbox("bob", lambda i: i["text"] == "spoof attempt")
        self.assertIsNotNone(item)
        self.assertEqual(item["from"], "alice")   # authenticated by token, not the forged 'mallory'

    def test_send_lands_in_peer_inbox(self):
        r = self.cli("alice", "send", "bob", "-m", "hello bob", agent="alice-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        item = self.wait_for_inbox("bob", lambda i: i["text"] == "hello bob")
        self.assertIsNotNone(item)
        self.assertEqual(item["from"], "alice")
        self.assertEqual(item["kind"], "message")

    def test_read_claims_item(self):
        self.cli("alice", "send", "bob", "-m", "claim me", agent="alice-1")
        item = self.wait_for_inbox("bob", lambda i: i["text"] == "claim me")
        r = self.cli("bob", "read", item["id"], agent="bob-1")
        self.assertEqual(r.returncode, 0, r.stderr)
        claimed = self.wait_for_inbox("bob", lambda i: i["id"] == item["id"] and i.get("claimed_by") == "bob-1")
        self.assertIsNotNone(claimed)

    def test_result_auto_targets_sending_session(self):
        # regression: a reply must route back to the exact session that sent the task.
        self.cli("alice", "send", "bob", "-t", "do X", agent="alice-1")
        task = self.wait_for_inbox("bob", lambda i: i["kind"] == "task")
        self.assertIsNotNone(task)
        self.assertEqual(task["from_agent"], "alice-1")
        r = self.cli("bob", "result", task["id"], "--status", "done", "-m", "did X", agent="bob-9")
        self.assertEqual(r.returncode, 0, r.stderr)
        result = self.wait_for_inbox("alice", lambda i: i["kind"] == "result")
        self.assertIsNotNone(result)
        self.assertEqual(result["to_agent"], "alice-1")   # not stranded on a ghost name

    def test_targeted_item_refuses_wrong_session(self):
        self.cli("alice", "send", "bob", "-t", "for target", "--agent", "bob-target",
                 agent="alice-1")
        task = self.wait_for_inbox("bob", lambda i: i["text"] == "for target")
        wrong = self.cli("bob", "read", task["id"], agent="bob-other")
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("addressed to", (wrong.stderr + wrong.stdout).lower())
        right = self.cli("bob", "read", task["id"], agent="bob-target")
        self.assertEqual(right.returncode, 0, right.stderr)

    def test_claim_stolen_from_dead_session(self):
        self.cli("alice", "send", "bob", "-m", "orphaned", agent="alice-1")
        item = self.wait_for_inbox("bob", lambda i: i["text"] == "orphaned")
        path = os.path.join(self.homes["bob"], "inbox", f"{item['id']}.json")
        item["claimed_by"] = "ghost-session"     # a session that never heartbeats
        item["claimed_at"] = time.time()
        with open(path, "w") as f:
            json.dump(item, f)
        r = self.cli("bob", "read", item["id"], agent="bob-live")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self._load(path)["claimed_by"], "bob-live")

    def test_queue_on_offline_then_flush(self):
        self.stop_daemon("bob")
        r = self.cli("alice", "send", "bob", "-m", "while offline", agent="alice-1")
        self.assertIn("queued", (r.stdout + r.stderr).lower())
        qdir = os.path.join(self.homes["alice"], "queue", "bob")
        self.assertTrue(os.path.isdir(qdir) and os.listdir(qdir))
        self.start_daemon("bob")
        self.assertTrue(self._wait_port(self.ports["bob"]))
        self.cli("alice", "flush", "bob")
        item = self.wait_for_inbox("bob", lambda i: i["text"] == "while offline")
        self.assertIsNotNone(item)

    def test_ask_returns_terminal_result_in_one_command(self):
        p = subprocess.Popen(
            [sys.executable, HERALD_PY, "ask", "bob", "-t", "compute please", "--timeout", "20"],
            env=self._env("alice", "alice-ask"), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            task = self.wait_for_inbox("bob", lambda i: i["kind"] == "task")
            self.assertIsNotNone(task)
            # intermediate 'working' should be shown but not end the ask
            self.cli("bob", "result", task["id"], "--status", "working", "-m", "on it", agent="bob-w")
            self.cli("bob", "result", task["id"], "--status", "done", "-m", "ANSWER-42", agent="bob-w")
            out, err = p.communicate(timeout=25)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertIn("ANSWER-42", out, err)

    def test_ask_keeps_waiting_after_message_ack(self):
        p = subprocess.Popen(
            [sys.executable, HERALD_PY, "ask", "bob", "-m", "ask Jamie", "--timeout", "20"],
            env=self._env("alice", "alice-ask"), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            message = self.wait_for_inbox("bob", lambda i: i["text"] == "ask Jamie")
            self.assertIsNotNone(message)
            self.cli("bob", "reply", message["id"], "-m", "Received; I will ask Jamie.",
                     "--meta", "herald_intent=ack", agent="bob-w")
            self.cli("bob", "reply", message["id"], "-m", "Jamie approved it.", agent="bob-w")
            out, err = p.communicate(timeout=25)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertIn("[ack] bob: Received; I will ask Jamie.", out, err)
        self.assertIn("Jamie approved it.", out, err)

    def test_wait_read_folds_in_content_and_claims(self):
        p = subprocess.Popen(
            [sys.executable, HERALD_PY, "wait", "--read", "--timeout", "15"],
            env=self._env("bob", "bob-wr"), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1)   # let wait snapshot the inbox before we send
            self.cli("alice", "send", "bob", "-m", "HELLO-WR", agent="alice-1")
            out, err = p.communicate(timeout=18)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertIn("HELLO-WR", out, err)
        item = self.wait_for_inbox("bob", lambda i: i.get("text") == "HELLO-WR")
        self.assertEqual(item.get("claimed_by"), "bob-wr")


    def test_all_flag_broadcasts(self):
        self.cli("alice", "send", "bob", "-m", "announce", "--all", agent="alice-1")
        item = self.wait_for_inbox("bob", lambda i: i["text"] == "announce")
        self.assertIsNotNone(item)
        self.assertTrue(item.get("broadcast"))
        self.assertEqual(item.get("to_agent", ""), "")   # every session wakes; not pinned to one

    def test_anycast_delivered_to_one_live_session(self):
        # inject a live listening session for bob (this test process's pid is alive)
        sdir = os.path.join(self.homes["bob"], "sessions")
        os.makedirs(sdir, exist_ok=True)
        with open(os.path.join(sdir, "bob-tab.json"), "w") as f:
            json.dump({"agent": "bob-tab", "pid": os.getpid(),
                       "host": socket.gethostname(), "heartbeat": time.time()}, f)
        self.cli("alice", "send", "bob", "-m", "single delivery",
                 agent="alice-1")   # default = anycast
        item = self.wait_for_inbox("bob", lambda i: i["text"] == "single delivery")
        self.assertIsNotNone(item)
        self.assertFalse(item.get("broadcast"))
        self.assertEqual(item.get("to_agent"), "bob-tab")   # handed to the one live session


if __name__ == "__main__":
    unittest.main()
