"""Resource/usage acceptance checks; only temporary stores and loopback peers."""
import json
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

import test_mcp as fixtures
import herald_mcp as mcp


def history(bridge, size):
    received = time.time() - 86400
    for index in range(size):
        item_id = f"history-{index:06}"
        item = {"id": item_id, "kind": "message", "received_ts": received,
                "received": mcp.iso(received), "from": "jamie", "to_mailbox": "dot",
                "to_agent": "simon-dot", "thread": item_id, "text": "retained fixture", "state": "handled"}
        path = bridge.herald.INBOX_DIR / (item_id + ".json")
        path.write_text(json.dumps(item))
        os.utime(path, (received, received))


class UsageAcceptance(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.MCPIntegration(methodName="test_greeting_signed_event_and_threaded_reply")
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.bridge = self.f.bridges["simon"]

    def snapshot(self):
        return dict(self.bridge.counts)

    def assert_idle(self, before):
        for key in ("inbox_files_parsed", "inbox_bytes_read", "config_reads", "sqlite_commits",
                    "callback_attempts", "callback_request_bytes", "pending_rows_loaded", "events_created"):
            self.assertEqual(self.bridge.counts[key], before[key], key)

    def test_no_subscriptions_means_no_inbox_configuration_or_write_work(self):
        history(self.bridge, 1000)
        before = self.snapshot()
        changes = self.bridge.db.total_changes
        with patch.object(self.bridge, "scan_new", side_effect=AssertionError("Unexpected inbox scan")), \
                patch.object(self.bridge, "policy", side_effect=AssertionError("Unexpected config read")):
            for _ in range(50):
                self.bridge.tick()
        self.assert_idle(before)
        self.assertEqual(self.bridge.counts["inbox_scans"], 0)
        self.assertEqual(self.bridge.db.total_changes, changes)

    def test_idle_subscription_has_no_body_reads_no_commits_no_network(self):
        self.f.subscribe()
        self.f.tick()
        before = self.snapshot()
        with patch.object(self.bridge.transport, "post", side_effect=AssertionError("Idle network call")), \
                patch.object(Path, "read_bytes", side_effect=AssertionError("Idle content read")):
            for _ in range(50):
                self.bridge.tick()
        self.assert_idle(before)
        self.assertEqual(self.bridge.counts["inbox_scans"], before["inbox_scans"])

    def test_large_history_is_indexed_once_without_parsing_old_bodies(self):
        history(self.bridge, 10000)
        self.f.subscribe()
        self.f.tick()
        self.assertEqual(self.bridge.counts["inbox_files_parsed"], 0)
        self.assertEqual(self.bridge.db.execute("SELECT count(*) FROM inbox_seen").fetchone()[0], 10000)
        before = self.snapshot()
        for _ in range(50):
            self.bridge.tick()
        self.assert_idle(before)
        self.assertEqual(self.bridge.counts["inbox_scans"], 1)

    def test_burst_is_incremental_and_successful_events_are_never_repeated(self):
        self.f.subscribe()
        self.f.tick()
        for index in range(10):
            self.f.send("burst-" + str(index))
        self.f.tick()
        self.assertEqual(self.bridge.counts["inbox_files_parsed"], 10)
        self.assertEqual(self.bridge.counts["events_created"], 10)
        self.assertEqual(self.bridge.counts["callback_accepted"], 10)
        self.assertEqual(len(self.f.seen), 10)
        before = self.snapshot()
        for _ in range(50):
            self.bridge.tick()
        self.assert_idle(before)
        stats = self.f.tool("simon", "usage_stats")
        self.assertEqual(stats["bridge_model_calls"], 0)
        self.assertEqual(stats["autonomous_reply_calls"], 0)
        self.assertEqual(stats["counters"]["send_calls"], 0)
        self.assertEqual(stats["counters"]["reply_calls"], 0)
        self.assertEqual(len(list(self.bridge.herald.OUTBOX_DIR.glob("*.json"))), 0)

    def test_retry_attempts_are_counted_and_duplicate_receiver_effect_is_suppressed(self):
        self.f.subscribe()
        self.f.statuses = [503, 200]
        self.f.send()
        self.f.tick()
        row = self.bridge.db.execute("SELECT record FROM deliveries").fetchone()
        record = json.loads(row[0]); record["next"] = 0
        self.bridge.db.execute("UPDATE deliveries SET record=?", (json.dumps(record),))
        self.bridge.db.commit()
        self.f.tick()
        self.assertEqual(self.bridge.counts["callback_attempts"], 2)
        self.assertEqual(self.bridge.counts["callback_retries"], 1)
        self.assertEqual(self.bridge.counts["callback_failures"], 1)
        self.assertEqual(self.bridge.counts["callback_accepted"], 1)
        self.assertEqual(len(self.f.seen), 1)
        before = self.snapshot()
        for _ in range(20):
            self.bridge.tick()
        self.assert_idle(before)

    def test_slow_callback_does_not_hold_messaging_lock(self):
        self.f.subscribe()
        sent = self.f.send()
        entered, release = threading.Event(), threading.Event()
        post = self.bridge.transport.post

        def slow(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("Test callback was never released")
            return post(*args)

        worker = None
        try:
            with patch.object(self.bridge.transport, "post", side_effect=slow):
                worker = threading.Thread(target=self.bridge.tick)
                worker.start()
                self.assertTrue(entered.wait(3))
                # Reply must finish WHILE the callback remains blocked.
                result = self.f.tool("simon", "reply", {"id": sent["id"], "text": "Approved fixture reply", "request_id": "slow-test"})
                self.assertIn("id", result)
                self.assertTrue(worker.is_alive())
                release.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())
        finally:
            release.set()
            if worker:
                worker.join(6)

    def test_notification_budget_and_refresh_cannot_create_a_reply_loop(self):
        policy = json.loads(self.f.configs["simon"].read_text())
        policy["event_limit"] = 1
        self.f.configs["simon"].write_text(json.dumps(policy))
        self.f.subscribe()
        for index in range(3):
            self.f.send("limit-" + str(index))
        self.f.tick()
        self.assertEqual(len(self.f.seen), 1)
        self.assertEqual(len(self.f.tool("simon", "list_messages")["messages"]), 3)
        self.f.subscribe()  # Active refresh does not reset the event budget.
        self.f.tick()
        self.assertEqual(len(self.f.seen), 1)
        self.assertEqual(self.bridge.counts["reply_calls"], 0)

    def test_pending_index_skips_large_successful_delivery_history(self):
        sub_id = self.f.subscribe()["result"]["id"]
        finished = {"state": "delivered", "next": 0, "attempts": 1, "event": {"data": {"id": "old"}}}
        self.bridge.db.executemany("INSERT INTO deliveries VALUES (?,?,?)",
                                  [(sub_id, f"old-{i}", json.dumps(finished)) for i in range(10000)])
        self.bridge.db.commit()
        self.f.tick()
        self.assertEqual(self.bridge.counts["pending_rows_loaded"], 0)
        plan = self.bridge.db.execute("EXPLAIN QUERY PLAN SELECT event,record FROM deliveries WHERE sub=? "
                                      "AND json_extract(record,'$.state')='pending' AND json_extract(record,'$.next')<=? LIMIT 16",
                                      (sub_id, time.time())).fetchall()
        self.assertTrue(any("deliveries_due" in row[-1] for row in plan))

    def test_daemon_reap_only_writes_when_a_record_changes(self):
        backend = self.bridge.herald
        rows = [
            {"id": "handled", "state": "handled"},
            {"id": "claimed", "state": "active", "claimed_by": "owner"},
            {"id": "untargeted", "state": "pending"},
            {"id": "held", "state": "pending", "targeted": True, "fallback": "hold"},
            {"id": "dead-assignment", "state": "handled", "assigned_session": "missing-listener"},
        ]
        for row in rows:
            row.update(kind="message", text="fixture", received_ts=time.time() - 1000,
                       to_agent="simon-dot", to_mailbox="dot")
            backend.atomic_write_json(backend.INBOX_DIR / (row["id"] + ".json"), row)
        with patch.object(backend, "atomic_write_json", wraps=backend.atomic_write_json) as write:
            backend._reap(backend.load_config())
            self.assertEqual(write.call_count, 1)
        record = json.loads((backend.INBOX_DIR / "dead-assignment.json").read_text())
        self.assertEqual(record["assigned_session"], "")
        with patch.object(backend, "atomic_write_json", wraps=backend.atomic_write_json) as write:
            backend._reap(backend.load_config())
            self.assertEqual(write.call_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
