"""Dot-to-dot loop protection: final messages and the per-thread wake budget."""
import json
import sys
import unittest
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / "tests"))
import test_mcp as fixtures


class LoopGuard(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MCPIntegration("test_greeting_signed_event_and_threaded_reply")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def events_for(self, thread):
        return [c[0] for c in self.f.callbacks if c[0].get("data", {}).get("thread") == thread]

    def test_final_message_is_marked_and_cannot_be_replied_to(self):
        self.f.subscribe()
        sent = self.f.tool("jamie", "send_message", {"peer": "simon", "text": "Done, thanks", "request_id": "done-1",
                                                     "final": True})
        self.f.tick()

        listed = self.f.tool("simon", "list_messages")["messages"]
        read = self.f.tool("simon", "read_message", {"id": sent["id"]})
        refused = self.f.tool("simon", "reply", {"id": sent["id"], "text": "You're welcome", "request_id": "r-1"})
        self.assertEqual([m["final"] for m in listed], [True])
        self.assertEqual((read["final"], read["reply_expected"]), (True, False))
        self.assertEqual(refused["error"]["code"], -32014)
        self.assertTrue(self.events_for(sent["thread"])[0]["data"]["final"])
        self.assertEqual(self.f.tool("simon", "usage_stats")["counters"]["final_reply_refusals"], 1)

    def test_ordinary_message_still_expects_a_reply(self):
        sent = self.f.send()

        read = self.f.tool("simon", "read_message", {"id": sent["id"]})
        reply = self.f.tool("simon", "reply", {"id": sent["id"], "text": "Hi", "request_id": "r-1"})
        self.assertEqual((read["final"], read["reply_expected"]), (False, True))
        self.assertEqual(reply["thread"], sent["thread"])

    def test_final_flag_must_be_boolean_and_is_part_of_the_retry_key(self):
        bad = self.f.tool("jamie", "send_message", {"peer": "simon", "text": "x", "request_id": "k", "final": "yes"})
        self.f.tool("jamie", "send_message", {"peer": "simon", "text": "x", "request_id": "k2"})
        conflict = self.f.tool("jamie", "send_message", {"peer": "simon", "text": "x", "request_id": "k2", "final": True})

        self.assertIn("error", bad)
        self.assertIn("error", conflict)

    def test_busy_thread_stops_waking_the_dot_but_messages_stay_readable(self):
        policy = json.loads(self.f.configs["simon"].read_text())
        policy["thread_event_limit"] = 2
        self.f.configs["simon"].write_text(json.dumps(policy))
        self.f.subscribe()
        to_simon = self.f.send()
        self.f.tick()
        for turn in range(2):
            back = self.f.tool("simon", "reply", {"id": to_simon["id"], "text": f"ack {turn}", "request_id": f"s{turn}"})
            to_simon = self.f.tool("jamie", "reply", {"id": back["id"], "text": f"ack {turn}", "request_id": f"j{turn}"})
            self.f.tick()

        self.assertEqual(len(self.events_for(to_simon["thread"])), 2)
        self.assertEqual(self.f.tool("simon", "read_message", {"id": to_simon["id"]})["text"], "ack 1")
        self.assertEqual(self.f.tool("simon", "usage_stats")["counters"]["events_suppressed"], 1)


if __name__ == "__main__":
    unittest.main()
