"""Bridge supervision with stand-in child processes; no network or credentials."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
import herald_mcp as mcp


class BridgeSupervisor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        policy = self.dir / "bridge.json"
        policy.write_text(json.dumps({"agent": "cody", "mailbox": "dot"}))
        self.block = {"config": str(policy), "port": 1}
        self.stop = threading.Event()

    def start(self, command):
        thread = threading.Thread(target=mcp.supervise_bridge, args=(self.block, self.dir, self.stop, command))
        thread.start()
        self.addCleanup(thread.join, 15)
        self.addCleanup(self.stop.set)
        return thread

    def wait_for(self, predicate):
        deadline = time.time() + 10
        while time.time() < deadline:
            state = mcp.read_bridge_state(self.dir)
            if predicate(state):
                return state
            time.sleep(0.05)
        self.fail(f"bridge state never matched: {mcp.read_bridge_state(self.dir)}")

    def test_restarts_a_killed_bridge_and_stops_it_with_the_daemon(self):
        command = [sys.executable, "-c", "import os,time; print(os.environ['HERALD_AGENT'], flush=True); time.sleep(60)"]
        with patch.object(mcp, "RESTART_DELAY", 0.05):
            thread = self.start(command)
            first = self.wait_for(lambda s: s.get("state") == "running")
            os.kill(first["pid"], 9)
            second = self.wait_for(lambda s: s.get("state") == "running" and s.get("pid") != first["pid"])
            self.stop.set()
            thread.join(15)

        self.assertEqual(second["restarts"], 1)
        self.assertEqual(mcp.read_bridge_state(self.dir)["state"], "stopped")
        with self.assertRaises(OSError):
            os.kill(second["pid"], 0)
        self.assertIn("cody", (self.dir / mcp.BRIDGE_LOG).read_text())

    def test_gives_up_after_repeated_fast_exits_and_status_says_so(self):
        with patch.object(mcp, "RESTART_DELAY", 0.01), patch.object(mcp, "MAX_RESTART_DELAY", 0.01):
            self.start([sys.executable, "-c", "raise SystemExit(3)"]).join(15)

        state = mcp.read_bridge_state(self.dir)
        self.assertEqual((state["state"], state["last_exit"]), ("gave_up", 3))
        self.assertIn("STOPPED - gave up", mcp.bridge_status_lines(self.block, self.dir)[0])

    def test_unreadable_bridge_policy_is_reported_not_retried(self):
        self.block["config"] = str(self.dir / "missing.json")

        self.start([sys.executable, "-c", "pass"]).join(15)

        self.assertEqual(mcp.read_bridge_state(self.dir)["state"], "gave_up")


if __name__ == "__main__":
    unittest.main()
