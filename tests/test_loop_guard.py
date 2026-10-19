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

    def test_out_closes_the_conversation_on_both_sides(self):
        self.f.subscribe()
        hello = self.f.send()
        self.f.tick()
        out = self.f.tool("simon", "reply", {"id": hello["id"], "text": "Done", "request_id": "s-out", "final": True})

        again = self.f.tool("simon", "reply", {"id": hello["id"], "text": "One more", "request_id": "s-again"})
        answer = self.f.tool("jamie", "reply", {"id": out["id"], "text": "Thanks", "request_id": "j-thanks"})
        fresh = self.f.send("new-conversation")
        self.f.tick()

        self.assertEqual((again["error"]["code"], answer["error"]["code"]), (-32014, -32014))
        self.assertNotEqual(fresh["thread"], hello["thread"])
        self.assertEqual([e["data"]["id"] for e in self.events_for(fresh["thread"])], [fresh["id"]])

    def test_messages_after_out_are_stored_but_do_not_wake_the_dot(self):
        self.f.subscribe()
        out = self.f.tool("jamie", "send_message", {"peer": "simon", "text": "FYI, done", "request_id": "j-out",
                                                    "final": True})
        self.f.tick()
        with fixtures.environment(self.f.envs["jamie"]):
            late = self.f.bridges["jamie"].herald.deliver(self.f.bridges["jamie"].herald_config(), "simon", {
                "kind": "message", "text": "ignoring the close", "thread": out["thread"], "to_mailbox": "dot",
                "to_agent": "", "targeted": True, "fallback": "hold", "delivery_id": "late-1"})
        self.f.tick()

        self.assertEqual([e["data"]["id"] for e in self.events_for(out["thread"])], [out["id"]])
        read = self.f.tool("simon", "read_message", {"id": late["id"]})
        listed = {m["id"]: m for m in self.f.tool("simon", "list_messages")["messages"]}
        self.assertEqual((read["text"], read["conversation_closed"], read["reply_expected"]), ("ignoring the close", True, False))
        self.assertEqual((listed[late["id"]]["conversation_closed"], listed[late["id"]]["reply_expected"]), (True, False))

    def test_notification_limit_is_a_rate_not_a_lifetime_cap(self):
        policy = json.loads(self.f.configs["simon"].read_text())
        policy.update(event_limit=1, event_window=60)
        self.f.configs["simon"].write_text(json.dumps(policy))
        self.f.subscribe()
        first = self.f.send("rate-1")
        self.f.tick()
        over = self.f.send("rate-2")
        self.f.tick()
        bridge = self.f.bridges["simon"]
        with fixtures.environment(self.f.envs["simon"]):
            bridge.db.execute("UPDATE deliveries SET record=json_set(record,'$.created',json_extract(record,'$.created')-120)")
            bridge.db.commit()
        later = self.f.send("rate-3")
        self.f.tick()

        woken = [c[0]["data"]["id"] for c in self.f.callbacks if "data" in c[0]]
        self.assertEqual(woken, [first["id"], later["id"]])
        self.assertNotIn(over["id"], woken)

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
        policy["thread_event_window"] = 600
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
