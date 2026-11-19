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


class AutolistenHandover(unittest.TestCase):
    DESK, OTHER = 4242, 5353

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="herald-handover-"))
        self.inbox, self.working = root / "inbox", root / "working"
        self.inbox.mkdir()
        self.working.mkdir()
        for name, value in (("INBOX_DIR", self.inbox), ("WORKING_DIR", self.working)):
            p = patch.object(herald, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.desk_pids = {self.DESK, self.DESK + 1}

    def sessions(self, *records):
        return {r["session_id"]: {"heartbeat": herald.time.time(), "mode": "general", "mailbox": "main",
                                 "host": "elsewhere", **r} for r in records}

    def desk_and_other(self, other_mailbox="main"):
        return self.sessions(
            {"session_id": "desk", "agent": "alice", "pid": self.DESK + 1, "harness_pid": self.DESK},
            {"session_id": "orch", "agent": "alice", "pid": 77, "harness_pid": self.OTHER,
             "mailbox": other_mailbox})

    def test_idle_desk_hands_over_to_another_listener_with_the_same_agent_name(self):
        desk = herald.autolisten_handover(self.desk_pids, self.desk_and_other())

        self.assertEqual([s["session_id"] for s in desk], ["desk"])

    def test_desk_alone_keeps_listening(self):
        sessions = self.sessions({"session_id": "desk", "agent": "alice", "pid": self.DESK + 1,
                                  "harness_pid": self.DESK})

        self.assertIsNone(herald.autolisten_handover(self.desk_pids, sessions))

    def test_listener_on_another_mailbox_does_not_take_over(self):
        self.assertIsNone(herald.autolisten_handover(self.desk_pids, self.desk_and_other("ops")))

    def test_desk_mid_turn_is_kept(self):
        with patch.object(herald, "working_labels", return_value=["desk turn"]) as labels:
            self.assertIsNone(herald.autolisten_handover(self.desk_pids, self.desk_and_other()))
        self.assertEqual(labels.call_args.kwargs["only_pids"], self.desk_pids)

    def test_desk_waiting_on_its_human_is_kept(self):
        (self.inbox / "q1.json").write_text(json.dumps(
            {"id": "q1", "state": "active", "claimed_pid": self.DESK}))

        self.assertIsNone(herald.autolisten_handover(self.desk_pids, self.desk_and_other()))

    def test_stop_desk_ends_listener_record_and_tmux_session(self):
        record = {"session_id": "desk", "pid": self.DESK + 1, "host": herald.socket.gethostname()}

        with patch.object(herald.os, "kill") as kill, \
             patch.object(herald, "clear_session") as clear, \
             patch.object(herald.subprocess, "run") as run:
            herald.autolisten_stop_desk([record])

        kill.assert_called_once_with(self.DESK + 1, herald.signal.SIGTERM)
        clear.assert_called_once_with("desk")
        self.assertEqual(run.call_args.args[0][:2], ["tmux", "kill-session"])


class AutolistenWake(unittest.TestCase):
    SETTINGS = {"wake_cmd": "wake", "wake_alive_cmd": "alive"}

    def runner(self, alive=True, wakes=True):
        calls = []
        def run(command):
            calls.append(command)
            return alive if command == "alive" else wakes
        return run, calls

    def test_no_wake_command_starts_the_desk(self):
        run, calls = self.runner()

        action, woken = herald.autolisten_wake({"wake_cmd": ""}, {"a1"}, set(), run)

        self.assertEqual(action, "desk")
        self.assertEqual(calls, [])

    def test_live_target_is_woken_once_per_item(self):
        run, calls = self.runner()

        action, woken = herald.autolisten_wake(self.SETTINGS, {"a1"}, set(), run)
        again, woken = herald.autolisten_wake(self.SETTINGS, {"a1"}, woken, run)

        self.assertEqual((action, again), ("woken", "already-woken"))
        self.assertEqual(calls.count("wake"), 1)

    def test_new_item_wakes_again_and_handled_items_are_forgotten(self):
        run, calls = self.runner()

        action, woken = herald.autolisten_wake(self.SETTINGS, {"a2"}, {"a1"}, run)

        self.assertEqual(action, "woken")
        self.assertEqual(woken, {"a2"})

    def test_dead_target_falls_back_to_desk_even_for_woken_items(self):
        run, calls = self.runner(alive=False)

        action, woken = herald.autolisten_wake(self.SETTINGS, {"a1"}, {"a1"}, run)

        self.assertEqual(action, "desk")
        self.assertNotIn("wake", calls)

    def test_failed_wake_falls_back_to_desk(self):
        run, calls = self.runner(wakes=False)

        action, woken = herald.autolisten_wake(self.SETTINGS, {"a1"}, set(), run)

        self.assertEqual((action, woken), ("desk", set()))

    def test_without_alive_command_the_wake_result_decides(self):
        run, calls = self.runner()

        action, woken = herald.autolisten_wake({"wake_cmd": "wake"}, {"a1"}, set(), run)

        self.assertEqual((action, calls), ("woken", ["wake"]))

    def test_commands_report_their_exit_status(self):
        self.assertTrue(herald.autolisten_run("true"))
        self.assertFalse(herald.autolisten_run("false"))

    def test_timed_out_command_and_its_children_are_killed(self):
        proc = SimpleNamespace(pid=999, wait=lambda timeout=None: (_ for _ in ()).throw(
            herald.subprocess.TimeoutExpired("wake", 30)) if timeout else 0)

        with patch.object(herald.subprocess, "Popen", return_value=proc), \
             patch.object(herald.os, "killpg") as killpg:
            self.assertFalse(herald.autolisten_run("wake"))

        killpg.assert_called_once_with(999, herald.signal.SIGKILL)

    def test_settings_read_wake_commands_from_env(self):
        settings = herald.autolisten_settings({"me": "alice"}, {"HERALD_AUTOLISTEN_WAKE_CMD": "w",
                                                                "HERALD_AUTOLISTEN_WAKE_ALIVE_CMD": "a"})

        self.assertEqual((settings["wake_cmd"], settings["wake_alive_cmd"]), ("w", "a"))


class AutolistenLoop(unittest.TestCase):
    def run_loop(self, env, polls=4, pending=None):
        prompt = Path(tempfile.mkdtemp()) / "prompt.md"
        prompt.write_text("desk")
        herald_dir = Path(tempfile.mkdtemp(prefix="herald-loop-"))
        sleeps = []
        def sleep(_):
            sleeps.append(1)
            if len(sleeps) >= polls:
                raise KeyboardInterrupt
        env = {"HERALD_AUTOLISTEN_PROMPT": str(prompt), "HERALD_AUTOLISTEN_GRACE": "1", **env}
        with patch.dict(os.environ, env), \
             patch.object(herald, "HERALD_DIR", herald_dir), \
             patch.object(herald, "autolisten_desk_pids", return_value=set()), \
             patch.object(herald, "autolisten_pending_ids", side_effect=pending or [{"a1"}] * polls), \
             patch.object(herald, "live_sessions", return_value=[]), \
             patch.object(herald, "autolisten_desk_running", return_value=False), \
             patch.object(herald, "autolisten_run", return_value=True) as run, \
             patch.object(herald, "autolisten_start_desk", return_value=True) as start, \
             patch.object(herald.time, "sleep", side_effect=sleep), \
             contextlib.redirect_stdout(io.StringIO()), \
             self.assertRaises(KeyboardInterrupt):
            herald.cmd_autolisten({"me": "alice"}, SimpleNamespace(once=False, install_service=False))
        return run, start

    def test_wake_target_is_woken_once_and_no_desk_starts(self):
        run, start = self.run_loop({"HERALD_AUTOLISTEN_WAKE_CMD": "wake"})

        self.assertEqual([c.args[0] for c in run.call_args_list], ["wake"])
        start.assert_not_called()

    def test_item_that_was_handled_and_came_back_wakes_again(self):
        run, start = self.run_loop({"HERALD_AUTOLISTEN_WAKE_CMD": "wake"},
                                   pending=[{"a1"}, {"a1"}, set(), {"a1"}])

        self.assertEqual(run.call_count, 2)
        start.assert_not_called()

    def test_without_wake_settings_the_desk_starts_as_before(self):
        run, start = self.run_loop({})

        run.assert_not_called()
        self.assertTrue(start.called)


if __name__ == "__main__":
    unittest.main()
