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
import pathlib
import signal
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

    def test_bell_enabled_defaults_on_and_env_beats_config(self):
        saved = os.environ.pop("HERALD_BELL", None)
        try:
            self.assertTrue(herald.bell_enabled())
            self.assertTrue(herald.bell_enabled({}))
            self.assertFalse(herald.bell_enabled({"bell": False}))
            os.environ["HERALD_BELL"] = "0"
            self.assertFalse(herald.bell_enabled())
            os.environ["HERALD_BELL"] = "1"
            self.assertTrue(herald.bell_enabled({"bell": False}))
        finally:
            os.environ.pop("HERALD_BELL", None)
            if saved is not None:
                os.environ["HERALD_BELL"] = saved

    def test_ring_bell_writes_bel_to_the_named_device(self):
        target = os.path.join(tempfile.mkdtemp(prefix="herald-bell-"), "tty")
        saved = os.environ.pop("HERALD_BELL", None)
        os.environ["HERALD_BELL_TTY"] = target
        try:
            herald.ring_bell()
            self.assertEqual(pathlib.Path(target).read_bytes(), b"\a")

            os.remove(target)
            herald.ring_bell({"bell": False})
            self.assertFalse(os.path.exists(target))
        finally:
            os.environ.pop("HERALD_BELL_TTY", None)
            if saved is not None:
                os.environ["HERALD_BELL"] = saved

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

    def test_legacy_claimed_and_progress_items_are_history(self):
        claimed = {"claimed_by": "old-agent", "kind": "message", "meta": {}}
        progress = {"claimed_by": "", "kind": "result", "status": "working", "meta": {}}
        pending = {"claimed_by": "", "kind": "message", "meta": {}}

        self.assertEqual(herald.item_state(claimed), "handled")
        self.assertEqual(herald.item_state(progress), "handled")
        self.assertEqual(herald.item_state(pending), "pending")

    def test_configured_default_mailbox_ignores_agent_selection(self):
        saved = os.environ.get("HERALD_MAILBOX")
        try:
            os.environ["HERALD_MAILBOX"] = "personal"

            self.assertEqual(herald.configured_default_mailbox(
                {"default_mailbox": "main", "mailboxes": ["main", "personal"]}), "main")
        finally:
            if saved is None:
                os.environ.pop("HERALD_MAILBOX", None)
            else:
                os.environ["HERALD_MAILBOX"] = saved


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

    def _env(self, name, agent=None, mailbox=None):
        env = dict(os.environ, HERALD_DIR=self.homes[name])
        env.pop("HERALD_AGENT", None)
        env.pop("HERALD_MAILBOX", None)
        if agent:
            env["HERALD_AGENT"] = agent
        if mailbox:
            env["HERALD_MAILBOX"] = mailbox
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

    def cli(self, name, *args, agent=None, mailbox=None, timeout=20):
        return subprocess.run([sys.executable, HERALD_PY, *args], env=self._env(name, agent, mailbox),
                              cwd=self.root, capture_output=True, text=True, timeout=timeout)

    def set_mailboxes(self, name, default, *mailboxes):
        path = os.path.join(self.homes[name], "config.json")
        cfg = self._load(path)
        cfg["default_mailbox"] = default
        cfg["mailboxes"] = list(mailboxes)
        with open(path, "w") as f:
            json.dump(cfg, f)

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

    def outbox(self, name):
        d = os.path.join(self.homes[name], "outbox")
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

        duplicate = self.cli("bob", "read", item["id"], agent="bob-2")
        self.assertNotEqual(duplicate.returncode, 0)
        self.assertIn("herald resume", duplicate.stderr)

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

    def test_exact_target_does_not_wake_another_listener(self):
        other = subprocess.Popen(
            [sys.executable, HERALD_PY, "wait", "--read", "--timeout", "12"],
            env=self._env("bob", "bob-other"), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1)
            self.cli("alice", "send", "bob", "-t", "EXACT-TARGET",
                     "--agent", "bob-target", agent="alice-1")
            time.sleep(1)
            self.assertIsNone(other.poll())

            target = self.cli("bob", "wait", "--read", "--timeout", "5",
                              agent="bob-target")
        finally:
            if other.poll() is None:
                other.kill()
            other_out, other_err = other.communicate()

        self.assertEqual(target.returncode, 0, target.stderr)
        self.assertIn("EXACT-TARGET", target.stdout)
        self.assertNotIn("EXACT-TARGET", other_out, other_err)

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

    def test_ask_reply_goes_to_scoped_listener_not_general_consumer(self):
        general = subprocess.Popen(
            [sys.executable, HERALD_PY, "wait", "--read", "--timeout", "20"],
            env=self._env("alice", "codex-general"), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        ask = None
        try:
            time.sleep(1)
            ask = subprocess.Popen(
                [sys.executable, HERALD_PY, "ask", "bob", "-t", "SCOPED-REQUEST",
                 "--timeout", "15"],
                env=self._env("alice", "copilot-ask"), cwd=self.root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            task = self.wait_for_inbox("bob", lambda i: i.get("text") == "SCOPED-REQUEST")
            self.assertIsNotNone(task)
            self.cli("bob", "result", task["id"], "--status", "done",
                     "-m", "SCOPED-ANSWER", agent="bob-worker")
            ask_out, ask_err = ask.communicate(timeout=18)
        finally:
            if ask and ask.poll() is None:
                ask.kill()
            if general.poll() is None:
                general.kill()
            general_out, general_err = general.communicate()

        self.assertIn("SCOPED-ANSWER", ask_out, ask_err)
        self.assertNotIn("SCOPED-ANSWER", general_out, general_err)
        reply = self.wait_for_inbox("alice", lambda i: i.get("text") == "SCOPED-ANSWER")
        self.assertEqual(reply.get("state"), "handled")

    def test_wait_read_folds_in_content_and_claims(self):
        p = subprocess.Popen(
            [sys.executable, HERALD_PY, "wait", "--read", "--timeout", "15"],
            env=self._env("bob", "bob-wr"), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1)   # let the listener register before the send
            self.cli("alice", "send", "bob", "-m", "HELLO-WR", agent="alice-1")
            out, err = p.communicate(timeout=18)
        finally:
            if p.poll() is None:
                p.kill()
        self.assertIn("HELLO-WR", out, err)
        item = self.wait_for_inbox("bob", lambda i: i.get("text") == "HELLO-WR")
        self.assertEqual(item.get("claimed_by"), "bob-wr")

    def test_bell_rings_only_when_the_human_blocks_the_turn(self):
        bell = os.path.join(self.root, "bell-tty")

        def run(*args, agent="bob-bell"):
            return subprocess.run([sys.executable, HERALD_PY, *args],
                                  env=dict(self._env("bob", agent), HERALD_BELL_TTY=bell),
                                  cwd=self.root, capture_output=True, text=True, timeout=20)

        self.cli("alice", "send", "bob", "-t", "run the tests", agent="alice-1")
        run("wait", "--read", "--timeout", "10")
        self.assertFalse(os.path.exists(bell), "arriving work alone must not ring the bell")

        task = self.wait_for_inbox("bob", lambda i: i["kind"] == "task")
        run("result", task["id"], "--status", "working", "-m", "on it")
        self.assertFalse(os.path.exists(bell), "autonomous work must not ring the bell")

        run("result", task["id"], "--status", "accepted", "-m", "asking my human")
        self.assertEqual(pathlib.Path(bell).read_bytes(), b"\a")

        os.remove(bell)
        r = run("bell", agent=None)
        self.assertEqual(pathlib.Path(bell).read_bytes(), b"\a")
        self.assertIn("Rang the terminal bell", r.stdout)

    def test_wait_reads_pending_item_that_predates_listener(self):
        self.cli("alice", "send", "bob", "-m", "WAITING-BEFORE-START",
                 agent="alice-1")

        r = self.cli("bob", "wait", "--read", "--timeout", "5", agent="bob-later")

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("WAITING-BEFORE-START", r.stdout)

    def test_provider_switch_resumes_acknowledged_work_without_second_ack(self):
        self.cli("alice", "send", "bob", "-t", "needs human approval", agent="alice-1")
        task = self.wait_for_inbox("bob", lambda i: i.get("text") == "needs human approval")
        self.cli("bob", "read", task["id"], agent="claude-work")
        self.cli("bob", "result", task["id"], "--status", "accepted",
                 "-m", "Received. I will ask Simon.", agent="claude-work")
        before = len([i for i in self.inbox("alice") if i.get("reply_to") == task["id"]])

        r = self.cli("bob", "resume", "--timeout", "5", agent="codex-personal")

        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("needs human approval", r.stdout)
        after = len([i for i in self.inbox("alice") if i.get("reply_to") == task["id"]])
        self.assertEqual(after, before)
        resumed = self.wait_for_inbox("bob", lambda i: i.get("id") == task["id"])
        self.assertEqual(resumed.get("claimed_by"), "codex-personal")
        self.assertTrue(resumed.get("acknowledged_at"))

    def test_resume_reoffers_open_work_to_same_agent_name(self):
        self.cli("alice", "send", "bob", "-t", "resume same identity", agent="alice-1")
        task = self.wait_for_inbox("bob", lambda i: i.get("text") == "resume same identity")
        self.cli("bob", "read", task["id"], agent="stable-agent")
        self.cli("bob", "result", task["id"], "--status", "accepted",
                 "-m", "Received. I will ask the human.", agent="stable-agent")

        resumed = self.cli("bob", "resume", "--timeout", "5", agent="stable-agent")

        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn("resume same identity", resumed.stdout)

    def test_new_provider_listener_supersedes_old_consumer(self):
        old = subprocess.Popen(
            [sys.executable, HERALD_PY, "wait", "--read", "--timeout", "15"],
            env=self._env("bob", "claude-work"), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        new = None
        try:
            time.sleep(1)
            new = subprocess.Popen(
                [sys.executable, HERALD_PY, "wait", "--read", "--timeout", "15"],
                env=self._env("bob", "copilot-personal"), cwd=self.root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(1)
            self.cli("alice", "send", "bob", "-m", "ONLY-NEW-CONSUMER", agent="alice-1")
            new_out, new_err = new.communicate(timeout=18)
            old_out, old_err = old.communicate(timeout=18)
        finally:
            for process in (old, new):
                if process and process.poll() is None:
                    process.kill()
        self.assertIn("ONLY-NEW-CONSUMER", new_out, new_err)
        self.assertIn("moved to another agent listener", old_out, old_err)
        self.assertNotIn("ONLY-NEW-CONSUMER", old_out)

    def test_provider_handoff_takes_item_assigned_to_suspended_listener(self):
        old = subprocess.Popen(
            [sys.executable, HERALD_PY, "wait", "--read", "--timeout", "20"],
            env=self._env("bob", "claude-work"), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(1)
            os.kill(old.pid, signal.SIGSTOP)
            self.cli("alice", "send", "bob", "-m", "ASSIGNED-BEFORE-SWITCH",
                     agent="alice-1")
            item = self.wait_for_inbox(
                "bob", lambda i: i.get("text") == "ASSIGNED-BEFORE-SWITCH")
            self.assertTrue(item.get("assigned_session"))

            replacement = self.cli("bob", "resume", "--timeout", "5",
                                   agent="codex-personal")
        finally:
            old.kill()
            old.communicate()

        self.assertEqual(replacement.returncode, 0, replacement.stderr)
        self.assertIn("ASSIGNED-BEFORE-SWITCH", replacement.stdout)

    def test_mailboxes_do_not_cross_deliver(self):
        self.set_mailboxes("bob", "work", "work", "personal")
        time.sleep(1)
        self.cli("alice", "send", "bob", "-m", "WORK-ONLY", "--mailbox", "work",
                 agent="alice-1")

        personal = self.cli("bob", "resume", "--timeout", "2",
                            agent="copilot-personal", mailbox="personal", timeout=5)
        work = self.cli("bob", "resume", "--timeout", "5",
                        agent="codex-work", mailbox="work")

        self.assertEqual(personal.returncode, 2)
        self.assertNotIn("WORK-ONLY", personal.stdout)
        self.assertEqual(work.returncode, 0, work.stderr)
        self.assertIn("WORK-ONLY", work.stdout)

    def test_unknown_mailbox_is_rejected(self):
        rejected = self.cli("alice", "send", "bob", "-m", "NOWHERE",
                            "--mailbox", "missing", agent="alice-1")

        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("rejected", rejected.stderr.lower())
        self.assertFalse(any(i.get("text") == "NOWHERE" for i in self.inbox("bob")))

    def test_duplicate_delivery_id_creates_one_inbox_item(self):
        payload = {"kind": "message", "text": "ONCE", "delivery_id": "stable-delivery"}
        req = lambda: urllib.request.Request(
            f"http://127.0.0.1:{self.ports['bob']}/send",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {self.TB}", "Content-Type": "application/json"})

        first = json.loads(urllib.request.urlopen(req()).read())
        second = json.loads(urllib.request.urlopen(req()).read())

        matches = [i for i in self.inbox("bob") if i.get("delivery_id") == "stable-delivery"]
        self.assertEqual(len(matches), 1)
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second.get("duplicate"))

    def test_broadcast_creates_one_item_per_mailbox(self):
        self.set_mailboxes("bob", "work", "work", "personal")
        time.sleep(1)

        self.cli("alice", "send", "bob", "-m", "ALL-MAILBOXES", "--all", agent="alice-1")

        matches = [i for i in self.inbox("bob") if i.get("text") == "ALL-MAILBOXES"]
        self.assertEqual({i.get("to_mailbox") for i in matches}, {"work", "personal"})
        self.assertEqual(len(matches), 2)

    def test_broadcast_task_waits_for_each_mailbox_result(self):
        self.set_mailboxes("bob", "work", "work", "personal")
        self.cli("alice", "send", "bob", "-t", "ALL-WORK", "--all", agent="alice-1")
        tasks = [i for i in self.inbox("bob") if i.get("text") == "ALL-WORK"]
        self.assertEqual(len(tasks), 2)

        self.cli("bob", "result", tasks[0]["id"], "--status", "done",
                 "-m", "first", agent="bob-1")
        request = next(i for i in self.outbox("alice") if i.get("text") == "ALL-WORK")
        self.assertEqual(request.get("state"), "awaiting_terminal")
        self.assertEqual(len(request.get("awaiting_reply_ids", [])), 1)

        self.cli("bob", "result", tasks[1]["id"], "--status", "done",
                 "-m", "second", agent="bob-2")
        request = next(i for i in self.outbox("alice") if i.get("text") == "ALL-WORK")
        self.assertEqual(request.get("state"), "handled")
        self.assertEqual(request.get("awaiting_reply_ids"), [])

    def test_queued_final_result_closes_source_after_delivery(self):
        self.cli("alice", "send", "bob", "-t", "finish later", agent="alice-1")
        task = self.wait_for_inbox("bob", lambda i: i.get("text") == "finish later")
        self.cli("bob", "read", task["id"], agent="bob-worker")
        self.stop_daemon("alice")

        queued = self.cli("bob", "result", task["id"], "--status", "done",
                          "-m", "finished", agent="bob-worker")

        pending = self.wait_for_inbox("bob", lambda i: i.get("id") == task["id"])
        self.assertIn("queued", (queued.stdout + queued.stderr).lower())
        self.assertEqual(pending.get("state"), "responded_pending_delivery")

        self.start_daemon("alice")
        self.assertTrue(self._wait_port(self.ports["alice"]))
        self.cli("bob", "flush", "alice")
        handled = self.wait_for_inbox("bob", lambda i: i.get("id") == task["id"])
        self.assertEqual(handled.get("state"), "handled")

    def test_queued_ack_is_recorded_before_provider_handoff(self):
        self.cli("alice", "send", "bob", "-t", "ack while offline", agent="alice-1")
        task = self.wait_for_inbox("bob", lambda i: i.get("text") == "ack while offline")
        self.stop_daemon("alice")

        queued = self.cli("bob", "result", task["id"], "--status", "accepted",
                          "-m", "Received. I will ask.", agent="claude-work")
        source = self.wait_for_inbox("bob", lambda i: i.get("id") == task["id"])

        self.assertIn("queued", (queued.stdout + queued.stderr).lower())
        self.assertEqual(source.get("state"), "active")
        self.assertTrue(source.get("acknowledged_at"))

    def test_rejected_final_result_keeps_source_visible(self):
        self.cli("alice", "send", "bob", "-t", "reject final", agent="alice-1")
        task = self.wait_for_inbox("bob", lambda i: i.get("text") == "reject final")
        path = os.path.join(self.homes["bob"], "config.json")
        cfg = self._load(path)
        cfg["peers"]["alice"]["token"] = "wrong-token"
        with open(path, "w") as f:
            json.dump(cfg, f)

        rejected = self.cli("bob", "result", task["id"], "--status", "done",
                            "-m", "cannot deliver", agent="bob-worker")

        source = self.wait_for_inbox("bob", lambda i: i.get("id") == task["id"])
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(source.get("state"), "delivery_failed")
        self.assertIn("rejected", source.get("delivery_error", ""))

    def test_ask_agent_sets_targeted_flag(self):
        process = subprocess.Popen(
            [sys.executable, HERALD_PY, "ask", "bob", "-t", "TARGETED-ASK",
             "--agent", "bob-target", "--timeout", "20"],
            env=self._env("alice", "alice-ask"), cwd=self.root,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            task = self.wait_for_inbox("bob", lambda i: i.get("text") == "TARGETED-ASK")
            self.assertTrue(task.get("targeted"))
            self.assertEqual(task.get("to_agent"), "bob-target")
        finally:
            process.kill()
            process.communicate()


    def test_all_flag_marks_broadcast_delivery(self):
        self.cli("alice", "send", "bob", "-m", "announce", "--all", agent="alice-1")
        item = self.wait_for_inbox("bob", lambda i: i["text"] == "announce")
        self.assertIsNotNone(item)
        self.assertTrue(item.get("broadcast"))
        self.assertEqual(item.get("to_agent", ""), "")

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
