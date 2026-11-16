"""Auto-listener decisions: when to start the Herald desk session."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("HERALD_DIR", tempfile.mkdtemp(prefix="herald-import-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import herald  # noqa: E402


class AutolistenState(unittest.TestCase):
    def test_no_pending_work_is_idle(self):
        self.assertEqual(herald.autolisten_state(0, 0, False), "idle")

    def test_any_live_listener_counts(self):
        self.assertEqual(herald.autolisten_state(3, 1, False), "listened")

    def test_desk_session_already_running(self):
        self.assertEqual(herald.autolisten_state(1, 0, True), "desk-starting")

    def test_pending_work_and_nobody_listening(self):
        self.assertEqual(herald.autolisten_state(1, 0, False), "unattended")


class AutolistenGrace(unittest.TestCase):
    def test_desk_starts_only_after_grace_unattended_checks(self):
        misses, start = herald.autolisten_step("unattended", 0, 2)
        self.assertEqual((misses, start), (1, False))

        misses, start = herald.autolisten_step("unattended", misses, 2)

        self.assertEqual((misses, start), (2, True))

    def test_attended_check_resets_misses(self):
        for state in ("idle", "listened", "desk-starting"):
            with self.subTest(state=state):
                self.assertEqual(herald.autolisten_step(state, 1, 2), (0, False))


class AutolistenCheck(unittest.TestCase):
    def setUp(self):
        self.inbox = Path(tempfile.mkdtemp(prefix="herald-autolisten-"))
        for name, patcher in (("INBOX_DIR", self.inbox), ("ensure_dirs", lambda: None)):
            p = patch.object(herald, name, patcher)
            p.start()
            self.addCleanup(p.stop)

    def write(self, item_id, **fields):
        (self.inbox / f"{item_id}.json").write_text(json.dumps({"id": item_id, **fields}))

    def test_listener_under_another_agent_name_counts(self):
        self.write("a1")
        other = {"agent": "cloud-orchestrator", "heartbeat": 0}

        with patch.object(herald, "live_sessions", return_value=[other]), \
             patch.object(herald, "autolisten_desk_running", return_value=False):
            state = herald.autolisten_check()

        self.assertEqual(state, "listened")

    def test_claimed_work_is_not_pending(self):
        self.write("a1", claimed_by="someone")

        with patch.object(herald, "live_sessions", return_value=[]), \
             patch.object(herald, "autolisten_desk_running", return_value=False):
            state = herald.autolisten_check()

        self.assertEqual(state, "idle")

    def test_once_prints_state_and_never_starts_desk(self):
        self.write("a1")
        out = io.StringIO()

        with patch.object(herald, "live_sessions", return_value=[]), \
             patch.object(herald, "autolisten_desk_running", return_value=False), \
             patch.object(herald, "autolisten_start_desk") as start, \
             contextlib.redirect_stdout(out):
            herald.cmd_autolisten({"me": "alice"}, SimpleNamespace(once=True, install_service=False))

        self.assertEqual(out.getvalue().strip(), "unattended")
        start.assert_not_called()


class AutolistenSettings(unittest.TestCase):
    def test_env_overrides_config_and_agent_defaults_to_me(self):
        cfg = {"me": "alice", "autolisten": {"interval": 30, "workdir": "/srv"}}

        settings = herald.autolisten_settings(cfg, {"HERALD_AUTOLISTEN_INTERVAL": "5"})

        self.assertEqual(settings["agent"], "alice")
        self.assertEqual(settings["interval"], 5)
        self.assertEqual(settings["workdir"], "/srv")
        self.assertEqual(settings["grace"], 2)

    def test_desk_gets_agent_name_and_prompt_placeholder(self):
        prompt = Path(tempfile.mkdtemp()) / "prompt.md"
        prompt.write_text("Use HERALD_AGENT={agent}.")
        settings = {"agent": "alice-vm", "workdir": "/tmp", "prompt": str(prompt)}

        with patch.object(herald.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as run:
            herald.autolisten_start_desk(settings)

        argv = run.call_args.args[0]
        self.assertIn("HERALD_AGENT=alice-vm", argv)
        self.assertEqual(argv[-1], "Use HERALD_AGENT=alice-vm.")


if __name__ == "__main__":
    unittest.main()
