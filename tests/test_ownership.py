import contextlib
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

import herald


class Ownership(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="herald-ownership-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, HERALD_DIR=str(self.root),
                              HERALD_AGENT="other", HERALD_MAILBOX="main")
        self.env.start()
        self.addCleanup(self.env.stop)
        for name, value in vars(herald).copy().items():
            if isinstance(value, Path) and value.is_relative_to(herald.HERALD_DIR):
                replacement = self.root / value.relative_to(herald.HERALD_DIR)
                if name != "HERALD_DIR":
                    p = patch.object(herald, name, replacement)
                    p.start()
                    self.addCleanup(p.stop)
        p = patch.object(herald, "HERALD_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)
        herald.ensure_dirs()
        self.cfg = {"me": "local", "mailboxes": ["main", "work"], "peers": {}}
        herald.atomic_write_json(herald.CONFIG_PATH, self.cfg)
        self.item = {"id": "test-item", "kind": "task", "from": "peer",
                     "text": "Private task", "thread": "thread", "files": [],
                     "targeted": True, "mailbox_targeted": True,
                     "to_agent": "intended", "to_mailbox": "main", "fallback": "hold",
                     "state": "pending", "claimed_by": "", "received_ts": time.time()}
        self.path = herald.INBOX_DIR / "test-item.json"
        self.save()

    def save(self):
        herald.atomic_write_json(self.path, self.item)

    def test_slow_queue_and_fallback_delivery_do_not_stop_heartbeat(self):
        launch = """
import sys, time, urllib.error
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import herald
herald.HEARTBEAT_INTERVAL = 0.05
herald.RETRY_INTERVAL = 0.1
def blocked_post(*args):
    with (herald.HERALD_DIR / 'attempts').open('a') as log:
        log.write('attempt\\n')
    while not (herald.HERALD_DIR / 'release').exists():
        time.sleep(0.01)
    raise urllib.error.URLError('timed out')
herald._post = blocked_post
herald.cmd_daemon(herald.load_config(), None)
"""
        for source in ("queue", "fallback"):
            with self.subTest(source=source):
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", 0))
                    port = probe.getsockname()[1]
                self.cfg.update(listen={"host": "127.0.0.1", "port": port},
                                peers={"peer": {"url": "http://127.0.0.1:1", "token": "test"}})
                herald.atomic_write_json(herald.CONFIG_PATH, self.cfg)
                self.item.update(fallback="bounce" if source == "fallback" else "hold",
                                 received_ts=0, state="pending", bounced=False)
                self.save()
                queued = herald.QUEUE_DIR / "peer" / "queued.json"
                if source == "queue":
                    herald.atomic_write_json(queued, {"kind": "message", "text": "queued",
                                                      "delivery_id": "slow-delivery"})
                attempts = self.root / "attempts"
                release = self.root / "release"
                for path in (attempts, release, herald.STATUS_PATH):
                    path.unlink(missing_ok=True)
                process = subprocess.Popen([sys.executable, "-c", launch,
                                            str(Path(herald.__file__).parent)],
                                           env=os.environ.copy(), stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, text=True)
                try:
                    deadline = time.monotonic() + 5
                    while (not attempts.exists() or not herald.STATUS_PATH.exists()) and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(attempts.exists(), "Daemon did not start delivery")
                    first = json.loads(herald.STATUS_PATH.read_text())
                    time.sleep(0.3)
                    latest = json.loads(herald.STATUS_PATH.read_text())

                    self.assertGreater(latest["heartbeat"], first["heartbeat"])
                    self.assertLess(time.time() - latest["heartbeat"], 0.15)
                    with urllib.request.urlopen("http://" + latest["listen"] + "/ping", timeout=2) as response:
                        self.assertEqual(response.status, 200)
                    self.assertEqual(attempts.read_text().splitlines(), ["attempt"])
                finally:
                    release.touch()
                    process.terminate()
                    process.communicate(timeout=5)
                    queued.unlink(missing_ok=True)

    def test_heartbeat_stops_if_maintenance_exits(self):
        maintenance = Mock()
        maintenance.is_alive.return_value = False

        with patch.object(herald, "write_status") as write:
            herald._heartbeat_loop("local", "127.0.0.1:1", "today", threading.Event(), maintenance)

        write.assert_not_called()

    def test_daemon_workers_stop_when_server_exits(self):
        self.cfg["listen"] = {"host": "127.0.0.1", "port": 0}
        workers = []
        real_thread = threading.Thread

        def create_thread(*args, **kwargs):
            worker = real_thread(*args, **kwargs)
            workers.append(worker)
            return worker

        with patch.object(herald, "ReceiverServer") as server, \
                patch.object(herald.threading, "Thread", side_effect=create_thread), \
                contextlib.redirect_stdout(io.StringIO()):
            server.return_value.serve_forever.return_value = None
            herald.cmd_daemon(self.cfg, None)

        self.assertEqual(len(workers), 2)
        self.assertTrue(all(not worker.is_alive() for worker in workers))

    def cli(self, *args, agent="other", mailbox="main"):
        return subprocess.run([sys.executable, str(Path(herald.__file__)), *args],
                              env=dict(os.environ, HERALD_AGENT=agent, HERALD_MAILBOX=mailbox),
                              cwd=self.root, capture_output=True, text=True, timeout=10)

    def test_named_mailbox_item_is_not_routed_or_claimed_by_other_listener(self):
        listener = herald.register_listener(self.cfg)

        herald._route(self.cfg)
        claimed = herald._claim_next(listener)

        self.assertIsNone(claimed)
        self.assertFalse(json.loads(self.path.read_text()).get("assigned_session"))

    def test_named_recipient_can_receive_from_another_mailbox(self):
        with patch.dict(os.environ, HERALD_AGENT="intended", HERALD_MAILBOX="work"):
            listener = herald.register_listener(self.cfg)

        herald._route(self.cfg)
        claimed = herald._claim_next(listener)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["claimed_by"], "intended")

    def test_foreign_read_close_reply_result_cannot_mutate_or_send(self):
        for args in [("read",), ("close",), ("reply", "-m", "wrong"),
                     ("result", "--status", "done", "-m", "wrong"), ("rm",)]:
            with self.subTest(command=args[0]):
                before = self.path.read_bytes()
                result = self.cli(args[0], "test-item", *args[1:])

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("intended", result.stderr)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(list(herald.OUTBOX_DIR.glob("*.json")))

    def test_reopen_by_wrong_claimant_preserves_recipient_and_returns_to_target(self):
        self.item.update(state="active", claimed_by="other")
        self.save()

        result = self.cli("reopen", "test-item")

        self.assertEqual(result.returncode, 0, result.stderr)
        reopened = json.loads(self.path.read_text())
        self.assertEqual(reopened["to_agent"], "intended")
        self.assertTrue(reopened["targeted"])
        self.assertEqual(reopened["state"], "pending")
        self.assertEqual(reopened["claimed_by"], "")
        listener = herald.register_listener(self.cfg)
        self.assertIsNone(herald._claim_next(listener))
        with patch.dict(os.environ, HERALD_AGENT="intended"):
            target = herald.register_listener(self.cfg)
        self.assertEqual(herald._claim_next(target)["claimed_by"], "intended")

    def test_foreign_reopen_cannot_release_unclaimed_private_work(self):
        before = self.path.read_bytes()

        result = self.cli("reopen", "test-item")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), before)

    def test_peek_shows_full_body_without_claim_or_attachment_write(self):
        attached = herald.FILES_DIR / "attachment.txt"
        attached.write_text("attachment")
        self.item["files"] = [{"filename": "attachment.txt", "stored_path": str(attached)}]
        self.item["text"] = "x" * 400
        self.save()
        before = self.path.read_bytes()

        result = self.cli("peek", "test-item", agent="")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["text"], self.item["text"])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.root / "attachment.txt").exists())

    def test_takeover_is_explicit_and_keeps_original_recipient(self):
        result = self.cli("takeover", "test-item")

        self.assertEqual(result.returncode, 0, result.stderr)
        item = json.loads(self.path.read_text())
        self.assertEqual(item["to_agent"], "intended")
        self.assertEqual(item["taken_over_by"], "other")
        self.assertEqual(item["claimed_by"], "other")
        self.assertEqual(self.cli("close", "test-item").returncode, 0)
        self.assertNotEqual(self.cli("read", "test-item", agent="intended").returncode, 0)

    def test_takeover_cannot_cross_mailboxes(self):
        before = self.path.read_bytes()

        result = self.cli("takeover", "test-item", mailbox="work")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("another mailbox", result.stderr)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unread_warning_uses_named_listener_not_mailbox_presence(self):
        herald.register_listener(self.cfg)

        summary = herald.inbox_summary(self.item)

        self.assertTrue(summary["blocked"])
        with patch.dict(os.environ, HERALD_AGENT="intended", HERALD_MAILBOX="work"):
            herald.register_listener(self.cfg)
        self.assertFalse(herald.inbox_summary(self.item)["blocked"])

    def test_listing_distinguishes_legacy_assignment_from_intended_recipient(self):
        self.item.update(targeted=False, to_agent="old-consumer")
        self.save()

        result = self.cli("inbox", "--json")

        summary = json.loads(result.stdout)[0]
        self.assertEqual(summary["intended_agent"], "")
        self.assertFalse(summary["targeted"])
        self.assertIn("shared", self.cli("inbox").stdout)

    def test_shared_work_still_goes_to_mailbox_owner(self):
        self.item.update(targeted=False, to_agent="")
        self.save()
        owner = herald.register_listener(self.cfg)
        with patch.dict(os.environ, HERALD_AGENT="alongside"):
            other = herald.register_listener(self.cfg)

        herald._route(self.cfg)

        self.assertIsNone(herald._claim_next(other))
        self.assertEqual(herald._claim_next(owner)["claimed_by"], "other")

    def test_hold_survives_expired_listener_and_reaper(self):
        self.item["received_ts"] = time.time() - herald.TARGET_GIVEUP - 1
        self.save()
        herald.register_listener(self.cfg)

        herald._reap(self.cfg)
        herald._route(self.cfg)

        item = json.loads(self.path.read_text())
        self.assertTrue(item["targeted"])
        self.assertEqual(item["to_agent"], "intended")
        self.assertFalse(item.get("assigned_session"))

    def test_outgoing_lists_queue_errors_without_payload_or_network(self):
        payload = {"kind": "task", "text": "Queued task", "to_agent": "remote-agent",
                   "targeted": True, "meta": {"token": "secret"},
                   "files": [{"filename": "proof.txt", "data_b64": "secret"}]}
        self.cfg["peers"] = {"peer": {"url": "http://example.invalid", "token": "secret"}}
        with patch.object(herald, "_post", side_effect=urllib.error.URLError("offline")):
            with contextlib.redirect_stdout(io.StringIO()):
                herald.deliver(self.cfg, "peer", payload)
            herald.flush_queue(self.cfg, "peer")

        result = self.cli("outgoing", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        rows = json.loads(result.stdout)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["state"], "queued")
        self.assertEqual(row["to"], "peer")
        self.assertEqual(row["recipient_agent"], "remote-agent")
        self.assertEqual(row["attempts"], 2)
        self.assertIn("offline", row["last_error"])
        self.assertGreater(row["last_attempt_at"], 0)
        self.assertGreaterEqual(row["age_seconds"], 0)
        self.assertEqual(row["retry_status"], "automatic retry pending")
        self.assertNotIn("secret", result.stdout)

    def test_outgoing_distinguishes_failed_from_awaiting_reply(self):
        herald.atomic_write_json(herald.FAILED_DIR / "failed.json", {
            "kind": "message", "text": "Rejected", "_peer": "peer",
            "_delivery_error": "rejected (403)", "_failed_at": time.time()})
        herald.atomic_write_json(herald.OUTBOX_DIR / "waiting.json", {
            "id": "waiting", "kind": "task", "text": "Awaiting reply", "to": "peer",
            "state": "awaiting_terminal", "from_mailbox": "main"})
        herald.atomic_write_json(herald.OUTBOX_DIR / "done.json", {
            "id": "done", "text": "Completed", "state": "handled"})

        result = self.cli("outgoing", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({r["state"] for r in json.loads(result.stdout)},
                         {"delivery_failed", "awaiting_reply"})
        self.assertNotIn("Completed", result.stdout)

    def test_old_queued_response_cannot_close_reopened_item(self):
        self.item.update(state="responded_pending_delivery", claimed_by="intended")
        self.save()
        payload = {"_source_item_id": "test-item", "_source_effect": "final",
                   "_source_revision": 0}
        self.assertEqual(self.cli("reopen", "test-item", agent="intended").returncode, 0)

        herald._apply_source_delivery(payload, "delivered")

        self.assertEqual(json.loads(self.path.read_text())["state"], "pending")

    def test_preferred_listener_cannot_override_named_recipient(self):
        listener = herald.register_listener(self.cfg)
        self.item["preferred_session"] = listener["session_id"]
        self.save()

        herald._route(self.cfg)

        self.assertFalse(json.loads(self.path.read_text()).get("assigned_session"))
        self.assertIsNone(herald._claim_next(listener))

    def test_explicit_broadcast_uses_agent_liveness_and_keeps_original_address(self):
        self.item.update(fallback="broadcast", received_ts=time.time() - herald.TARGET_GIVEUP - 1)
        self.save()
        herald.register_listener(self.cfg)

        with patch.object(herald, "_notify_origin"):
            herald._reap(self.cfg)

        item = json.loads(self.path.read_text())
        self.assertTrue(item["unpinned"])
        self.assertEqual(item["to_agent"], "intended")
        self.assertEqual(herald.recipient_agent(item), "")

    def test_stale_response_cannot_change_takeover_or_closed_item(self):
        payload = {"_source_item_id": "test-item", "_source_effect": "ack", "_source_revision": 0}
        for command, agent in (("takeover", "other"), ("close", "intended")):
            with self.subTest(command=command):
                self.save()
                result = self.cli(command, "test-item", agent=agent)
                self.assertEqual(result.returncode, 0, result.stderr)
                before = self.path.read_bytes()

                herald._apply_source_delivery(payload, "delivered")

                self.assertEqual(self.path.read_bytes(), before)

    def test_ask_for_another_request_cannot_hide_unread_work(self):
        with patch.dict(os.environ, HERALD_AGENT="intended"):
            listener = herald.register_listener(self.cfg, mode="ask")
            listener["request_id"] = "different-request"
            herald.write_session(listener)
        self.item.update(preferred_session=listener["session_id"], reply_to="our-request")

        self.assertTrue(herald.inbox_summary(self.item)["blocked"])
        self.item["reply_to"] = "different-request"
        self.assertFalse(herald.inbox_summary(self.item)["blocked"])

    def test_shared_work_is_unread_when_only_a_request_listener_exists(self):
        herald.register_listener(self.cfg, mode="ask")
        self.item.update(targeted=False, to_agent="")

        self.assertTrue(herald.inbox_summary(self.item)["blocked"])

    def test_intended_recipient_recovers_previously_misclaimed_active_item(self):
        self.item.update(state="active", claimed_by="other", presented_generation=9)
        self.save()
        herald.register_listener(self.cfg)
        with patch.dict(os.environ, HERALD_AGENT="intended"):
            listener = herald.register_listener(self.cfg)

        claimed = herald._claim_next(listener)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["claimed_by"], "intended")

    def test_explicit_bounce_uses_named_agent_not_shared_mailbox_listener(self):
        self.item.update(fallback="bounce", received_ts=time.time() - herald.TARGET_GIVEUP - 1)
        self.save()
        herald.register_listener(self.cfg)

        with patch.object(herald, "_notify_origin") as notify:
            herald._reap(self.cfg)

        item = json.loads(self.path.read_text())
        self.assertTrue(item["bounced"])
        self.assertEqual(item["state"], "handled")
        self.assertEqual(notify.call_args.args[2], "intended")

    def test_shared_takeover_stays_reserved_after_reopen(self):
        self.item.update(targeted=False, to_agent="")
        self.save()
        self.assertEqual(self.cli("takeover", "test-item").returncode, 0)
        self.assertEqual(self.cli("reopen", "test-item").returncode, 0)

        result = self.cli("read", "test-item", agent="third")

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(json.loads(self.path.read_text())["taken_over_by"], "other")

    def test_wrong_recipient_cannot_accept_introduction_or_change_config(self):
        self.item.update(kind="message", meta={"herald_intent": "introduce", "name": "peer",
                                             "url": "http://example.invalid", "token": "secret"})
        self.save()
        config = herald.CONFIG_PATH.read_bytes()
        before = self.path.read_bytes()

        result = self.cli("accept", "test-item")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("intended", result.stderr)
        self.assertEqual(herald.CONFIG_PATH.read_bytes(), config)
        self.assertEqual(self.path.read_bytes(), before)

    def test_old_queue_without_retry_metadata_remains_visible_without_changes(self):
        path = herald.QUEUE_DIR / "peer" / "old.json"
        herald.atomic_write_json(path, {"kind": "message", "text": "Old queued message"})
        before = path.read_bytes()

        result = self.cli("outgoing", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        row = json.loads(result.stdout)[0]
        self.assertEqual(row["preview"], "Old queued message")
        self.assertEqual(row["last_attempt_at"], 0)
        self.assertEqual(path.read_bytes(), before)

    def test_rejected_queue_item_moves_to_failed_listing(self):
        self.cfg["peers"] = {"peer": {"url": "http://example.invalid", "token": "secret"}}
        herald.enqueue("peer", {"kind": "task", "text": "Rejected task", "delivery_id": "rejected"})
        error = urllib.error.HTTPError("http://example.invalid", 403, "Forbidden", {}, None)

        with patch.object(herald, "_post", side_effect=error):
            herald.flush_queue(self.cfg, "peer")

        rows = json.loads(self.cli("outgoing", "--json").stdout)
        self.assertEqual([r["state"] for r in rows], ["delivery_failed"])
        self.assertIn("403", rows[0]["last_error"])

    def test_fast_reply_is_reserved_while_ask_records_its_request(self):
        with patch.dict(os.environ, HERALD_AGENT="intended"):
            general = herald.register_listener(self.cfg)
            request = herald.register_listener(self.cfg, mode="ask")
        self.item.update(preferred_session=request["session_id"], reply_to="remote-request")
        self.save()

        herald._route(self.cfg)

        self.assertEqual(json.loads(self.path.read_text())["assigned_session"], request["session_id"])
        self.assertIsNone(herald._claim_next(general))
        self.assertIsNone(herald._claim_next(request))
        request["request_id"] = "remote-request"
        herald.write_session(request)
        self.assertEqual(herald._claim_next(request)["id"], "test-item")

    def test_live_request_listener_prevents_fallback_and_unread_warning(self):
        for targeted in (True, False):
            with self.subTest(targeted=targeted):
                with patch.dict(os.environ, HERALD_AGENT="intended"):
                    request = herald.register_listener(self.cfg, mode="ask")
                    request["request_id"] = "remote-request"
                    herald.write_session(request)
                self.item.update(targeted=targeted, preferred_session=request["session_id"],
                                 reply_to="remote-request", fallback="bounce",
                                 received_ts=time.time() - herald.TARGET_GIVEUP - 1)
                self.save()

                with patch.object(herald, "_notify_origin"):
                    herald._reap(self.cfg)

                item = json.loads(self.path.read_text())
                self.assertEqual(item["state"], "pending")
                self.assertFalse(herald.inbox_summary(item)["blocked"])
