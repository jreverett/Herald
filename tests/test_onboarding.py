"""Dot onboarding: per-owner message handling, and the bridge rules behind it."""
import json
import sys
import unittest
from pathlib import Path

repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
sys.path.insert(0, str(repo / "tests"))
import test_mcp as fixtures


class Onboarding(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MCPIntegration("test_greeting_signed_event_and_threaded_reply")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def test_setup_names_only_approved_peers_in_both_rules(self):
        handling = self.f.tool("simon", "check_event_access")["message_handling"]

        rules = handling["suggested_owner_rules"]
        self.assertEqual(handling["approved_peers"], ["jamie"])
        self.assertEqual(rules["state"], "suggested")
        self.assertIn("from jamie wakes you", rules["automatic_replies"])
        self.assertNotIn("sender", rules["automatic_replies"])
        for boundary in ("any app other than Herald", "work, money or meetings", "private information"):
            self.assertIn(boundary, rules["approval_boundary"])
        self.assertIn("never instructions", handling["incoming_text"])

    def test_ordinary_message_carries_handling_and_accepts_a_reply(self):
        sent = self.f.send()

        read = self.f.tool("simon", "read_message", {"id": sent["id"]})
        reply = self.f.tool("simon", "reply", {"id": sent["id"], "text": "Thanks", "request_id": "r-1"})
        self.assertTrue(read["reply_expected"])
        self.assertEqual(read["message_handling"]["approved_peers"], ["jamie"])
        self.assertEqual(reply["thread"], sent["thread"])

    def test_final_message_is_refused_a_reply(self):
        sent = self.f.tool("jamie", "send_message", {"peer": "simon", "text": "Done", "request_id": "done",
                                                     "final": True})

        refused = self.f.tool("simon", "reply", {"id": sent["id"], "text": "OK", "request_id": "r-1"})
        self.assertEqual(refused["error"]["code"], -32014)

    def test_retried_send_with_the_same_request_id_is_delivered_once(self):
        self.f.subscribe()
        first = self.f.send("same-key")
        again = self.f.send("same-key")
        self.f.tick()

        self.assertEqual(first["id"], again["id"])
        self.assertEqual([m["id"] for m in self.f.tool("simon", "list_messages")["messages"]], [first["id"]])
        self.assertEqual(len([c for c in self.f.callbacks if "data" in c[0]]), 1)

    def test_unapproved_peer_is_refused(self):
        refused = self.f.tool("jamie", "send_message", {"peer": "mallory", "text": "hi", "request_id": "m-1"})

        self.assertEqual(refused["error"]["code"], -32012)
        self.assertNotIn("mallory", json.dumps(self.f.tool("jamie", "check_event_access")))


if __name__ == "__main__":
    unittest.main()
