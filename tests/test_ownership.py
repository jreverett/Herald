import argparse
import contextlib
import io
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
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

    def test_shared_work_goes_to_the_only_eligible_listener(self):
        self.item.update(targeted=False, to_agent="")
        self.save()
        owner = herald.register_listener(self.cfg)

        herald._route(self.cfg)

        self.assertEqual(herald._claim_next(owner)["claimed_by"], "other")

    def test_shared_work_waits_when_two_listeners_could_take_it(self):
        # Owning the mailbox is an accident of who started last, so it must not
        # decide which session a conversation lands in.
        self.item.update(targeted=False, to_agent="")
        self.save()
        owner = herald.register_listener(self.cfg)
        with patch.dict(os.environ, HERALD_AGENT="alongside"):
            other = herald.register_listener(self.cfg)

        herald._route(self.cfg)

        self.assertIsNone(herald._claim_next(other))
        self.assertIsNone(herald._claim_next(owner))
        self.assertTrue(json.loads(self.path.read_text())["unrouted"])

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


class Routing(unittest.TestCase):
    """Topic and thread routing: work reaches the session it belongs to, or waits."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"HERALD_DIR": str(root), "HERALD_AGENT": "solo"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name, sub in (("HERALD_DIR", ""), ("INBOX_DIR", "inbox"), ("OUTBOX_DIR", "outbox"),
                          ("FILES_DIR", "files"), ("QUEUE_DIR", "queue"), ("SESSIONS_DIR", "sessions"),
                          ("CONSUMERS_DIR", "consumers"), ("ACTIVITY_DIR", "activity"),
                          ("WORKING_DIR", "working"), ("FAILED_DIR", "failed")):
            if hasattr(herald, name):
                patcher = patch.object(herald, name, root / sub if sub else root)
                patcher.start()
                self.addCleanup(patcher.stop)
        for name, leaf in (("LOCK_PATH", "state.lock"), ("STATUS_PATH", "status.json"),
                           ("CONFIG_PATH", "config.json")):
            if hasattr(herald, name):
                patcher = patch.object(herald, name, root / leaf)
                patcher.start()
                self.addCleanup(patcher.stop)
        herald.ensure_dirs()
        self.cfg = {"me": "jamie", "peers": {}, "default_mailbox": "main", "mailboxes": ["main"]}

    def _listener(self, agent, subjects=None, mailbox="main"):
        with patch.dict(os.environ, {"HERALD_AGENT": agent, "HERALD_MAILBOX": mailbox}):
            return herald.register_listener(self.cfg, subjects=subjects)

    def _item(self, item_id="i1", thread="t1", subject="", **extra):
        item = {
            "id": item_id, "thread": thread, "subject": subject, "reply_to": "",
            "from": "simon", "from_agent": "si", "kind": "message", "text": "hello",
            "to_agent": "", "to_mailbox": "main", "targeted": False, "mailbox_targeted": False,
            "broadcast": False, "fallback": "hold", "state": "pending",
            "received_ts": time.time(), "presented_generation": 0, "files": [],
        }
        item.update(extra)
        herald.atomic_write_json(herald.INBOX_DIR / f"{item_id}.json", item)
        return item

    def _stored(self, item_id="i1"):
        return json.loads((herald.INBOX_DIR / f"{item_id}.json").read_text())

    # --- the case that must keep working: one listener, undirected message ---

    def test_a_single_generic_listener_receives_undirected_work(self):
        solo = self._listener("solo")
        self._item()

        herald._route(self.cfg)
        claimed = herald._claim_next(solo)

        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["claimed_by"], "solo")
        self.assertFalse(self._stored()["unrouted"])

    def test_a_single_listener_owns_the_thread_after_receiving_it(self):
        solo = self._listener("solo")
        self._item()
        herald._route(self.cfg)
        herald._claim_next(solo)

        self.assertEqual(herald.thread_owner("t1"), "solo")

    # --- ambiguity is never resolved by luck ---

    def test_two_generic_listeners_leave_undirected_work_unrouted(self):
        one = self._listener("one")
        two = self._listener("two")
        self._item()

        herald._route(self.cfg)

        self.assertIsNone(herald._claim_next(one))
        self.assertIsNone(herald._claim_next(two))
        self.assertTrue(self._stored()["unrouted"])

    def test_an_unrouted_item_is_taken_by_an_explicit_claim(self):
        self._listener("one")
        self._listener("two")
        self._item()
        herald._route(self.cfg)

        herald._claim_item("i1", "two", "main")

        stored = self._stored()
        self.assertEqual(stored["claimed_by"], "two")
        self.assertFalse(stored["unrouted"])
        self.assertEqual(herald.thread_owner("t1"), "two")

    def test_a_named_item_is_never_unrouted_even_with_two_listeners(self):
        one = self._listener("one")
        self._listener("two")
        self._item(to_agent="one", targeted=True)

        herald._route(self.cfg)

        self.assertEqual(herald._claim_next(one)["claimed_by"], "one")

    # --- thread affinity ---

    def test_a_later_item_on_a_claimed_thread_goes_to_its_owner(self):
        one = self._listener("one")
        two = self._listener("two")
        self._item(item_id="i1", thread="t1")
        herald._claim_item("i1", "two", "main")

        self._item(item_id="i2", thread="t1")
        herald._route(self.cfg)

        self.assertIsNone(herald._claim_next(one))
        self.assertEqual(herald._claim_next(two)["id"], "i2")

    def test_thread_affinity_beats_being_the_mailbox_owner(self):
        owner = self._listener("owner")
        other = self._listener("other")
        self._item(item_id="i1", thread="t1")
        herald._claim_item("i1", "other", "main")

        self._item(item_id="i2", thread="t1")
        herald._route(self.cfg)

        self.assertEqual(self._stored("i2")["assigned_session"], other["session_id"])
        self.assertIsNone(herald._claim_next(owner))

    def test_a_different_thread_is_not_pulled_in_by_affinity(self):
        one = self._listener("one")
        self._item(item_id="i1", thread="t1")
        herald._claim_item("i1", "one", "main")
        self._listener("two")

        self._item(item_id="i2", thread="t2")
        herald._route(self.cfg)

        self.assertTrue(self._stored("i2")["unrouted"])

    def test_releasing_an_item_gives_up_its_thread(self):
        self._listener("one")
        self._listener("two")
        self._item(item_id="i1", thread="t1")
        herald._claim_item("i1", "one", "main")
        self.assertEqual(herald.thread_owner("t1"), "one")

        with patch.dict(os.environ, {"HERALD_AGENT": "one"}):
            herald.cmd_release(self.cfg, SimpleNamespace(id="i1"))

        self.assertEqual(herald.thread_owner("t1"), "")
        stored = self._stored("i1")
        self.assertEqual(stored["state"], "pending")
        self.assertEqual(stored["claimed_by"], "")

    def test_release_refuses_an_item_held_by_another_agent(self):
        self._listener("one")
        self._item(item_id="i1")
        herald._claim_item("i1", "one", "main")

        with patch.dict(os.environ, {"HERALD_AGENT": "two"}):
            with self.assertRaises(SystemExit):
                herald.cmd_release(self.cfg, SimpleNamespace(id="i1"))

        self.assertEqual(self._stored("i1")["claimed_by"], "one")

    # --- handoff ---

    def test_handoff_moves_the_item_and_the_thread(self):
        self._listener("one")
        two = self._listener("two")
        self._item(item_id="i1", thread="t1")
        herald._claim_item("i1", "one", "main")

        with patch.dict(os.environ, {"HERALD_AGENT": "one"}):
            herald.cmd_handoff(self.cfg, SimpleNamespace(id="i1", to="two"))

        self.assertEqual(herald.thread_owner("t1"), "two")
        self.assertEqual(herald.recipient_agent(self._stored("i1")), "two")
        herald._route(self.cfg)
        self.assertEqual(herald._claim_next(two)["id"], "i1")

    def test_after_handoff_later_thread_items_follow_the_new_owner(self):
        one = self._listener("one")
        two = self._listener("two")
        self._item(item_id="i1", thread="t1")
        herald._claim_item("i1", "one", "main")
        with patch.dict(os.environ, {"HERALD_AGENT": "one"}):
            herald.cmd_handoff(self.cfg, SimpleNamespace(id="i1", to="two"))

        self._item(item_id="i2", thread="t1")
        herald._route(self.cfg)

        self.assertEqual(self._stored("i2")["assigned_session"], two["session_id"])
        self.assertIsNone(herald._claim_next(one))

    def test_handoff_refuses_an_item_held_by_another_agent(self):
        self._listener("one")
        self._item(item_id="i1")
        herald._claim_item("i1", "one", "main")

        with patch.dict(os.environ, {"HERALD_AGENT": "three"}):
            with self.assertRaises(SystemExit):
                herald.cmd_handoff(self.cfg, SimpleNamespace(id="i1", to="two"))

    # --- subjects ---

    def test_a_subject_reaches_the_session_that_declared_it(self):
        general = self._listener("general")
        topic = self._listener("topic", subjects=["pbi-738"])
        self._item(subject="pbi-738")

        herald._route(self.cfg)

        self.assertIsNone(herald._claim_next(general))
        self.assertEqual(herald._claim_next(topic)["claimed_by"], "topic")

    def test_a_subject_nobody_declared_falls_back_to_a_generalist(self):
        general = self._listener("general")
        self._listener("topic", subjects=["something-else"])
        self._item(subject="pbi-738")

        herald._route(self.cfg)

        self.assertEqual(herald._claim_next(general)["claimed_by"], "general")

    def test_undirected_work_does_not_go_to_a_subject_bound_session(self):
        self._listener("topic", subjects=["pbi-738"])
        self._item()

        herald._route(self.cfg)

        stored = self._stored()
        self.assertFalse(stored.get("assigned_session"))

    def test_two_sessions_on_the_same_subject_leave_it_unrouted(self):
        one = self._listener("one", subjects=["pbi-738"])
        two = self._listener("two", subjects=["pbi-738"])
        self._item(subject="pbi-738")

        herald._route(self.cfg)

        self.assertIsNone(herald._claim_next(one))
        self.assertIsNone(herald._claim_next(two))
        self.assertTrue(self._stored()["unrouted"])

    def test_subjects_are_recorded_on_the_session(self):
        listener = self._listener("topic", subjects=["a", "b"])
        herald.write_session(listener)

        record = herald.read_sessions()[listener["session_id"]]
        self.assertEqual(record["subjects"], ["a", "b"])

    # --- attachments never land in the process CWD ---

    def test_attachments_are_written_under_the_herald_directory(self):
        stored_file = herald.FILES_DIR / "i1_note.txt"
        stored_file.parent.mkdir(parents=True, exist_ok=True)
        stored_file.write_bytes(b"payload")
        item = {"id": "i1", "files": [{"filename": "note.txt", "stored_path": str(stored_file)}]}

        with contextlib.redirect_stdout(io.StringIO()):
            herald._write_files(item)

        written = herald.item_files_dir("i1") / "note.txt"
        self.assertTrue(written.exists())
        self.assertEqual(written.read_bytes(), b"payload")
        self.assertFalse((Path.cwd() / "note.txt").exists())


class ContextIsolation(unittest.TestCase):
    """No item's content may reach a session that is not working on it.

    The cost of getting this wrong is not only a confused agent: a delivered item
    is printed into that session's context, so unrelated work is paid for in
    tokens and pollutes the conversation it lands in.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"HERALD_DIR": str(root), "HERALD_AGENT": "solo"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name, sub in (("HERALD_DIR", ""), ("INBOX_DIR", "inbox"), ("OUTBOX_DIR", "outbox"),
                          ("FILES_DIR", "files"), ("QUEUE_DIR", "queue"), ("SESSIONS_DIR", "sessions"),
                          ("CONSUMERS_DIR", "consumers"), ("ACTIVITY_DIR", "activity"),
                          ("WORKING_DIR", "working"), ("FAILED_DIR", "failed")):
            if hasattr(herald, name):
                patcher = patch.object(herald, name, root / sub if sub else root)
                patcher.start()
                self.addCleanup(patcher.stop)
        for name, leaf in (("LOCK_PATH", "state.lock"), ("STATUS_PATH", "status.json"),
                           ("CONFIG_PATH", "config.json")):
            if hasattr(herald, name):
                patcher = patch.object(herald, name, root / leaf)
                patcher.start()
                self.addCleanup(patcher.stop)
        herald.ensure_dirs()
        self.cfg = {"me": "jamie", "peers": {}, "default_mailbox": "main", "mailboxes": ["main"]}

    def _listener(self, agent, subjects=None):
        with patch.dict(os.environ, {"HERALD_AGENT": agent}):
            return herald.register_listener(self.cfg, subjects=subjects)

    def _item(self, item_id, thread, subject="", text="body"):
        herald.atomic_write_json(herald.INBOX_DIR / f"{item_id}.json", {
            "id": item_id, "thread": thread, "subject": subject, "reply_to": "",
            "from": "simon", "from_agent": "si", "kind": "message", "text": text,
            "to_agent": "", "to_mailbox": "main", "targeted": False, "mailbox_targeted": False,
            "broadcast": False, "fallback": "hold", "state": "pending",
            "received_ts": time.time(), "presented_generation": 0, "files": [],
        })

    def _drain(self, listener):
        """Everything this session would actually be shown."""
        seen = []
        while True:
            item = herald._claim_next(listener)
            if not item:
                return seen
            seen.append(item["id"])

    def test_two_topics_two_sessions_never_cross(self):
        deploy = self._listener("deploy-session", subjects=["workflow-deploy"])
        herald_work = self._listener("herald-session", subjects=["herald-routing"])

        self._item("d1", "t-deploy", subject="workflow-deploy", text="pipeline 45 question")
        self._item("h1", "t-herald", subject="herald-routing", text="routing question")
        self._item("d2", "t-deploy", subject="workflow-deploy", text="follow-up on 45")
        herald._route(self.cfg)

        self.assertEqual(sorted(self._drain(deploy)), ["d1", "d2"])
        self.assertEqual(self._drain(herald_work), ["h1"])

    def test_a_reply_on_a_claimed_thread_never_reaches_the_other_session(self):
        one = self._listener("session-one")
        self._item("a1", "t-one")
        herald._claim_item("a1", "session-one", "main")
        two = self._listener("session-two")

        # The follow-up carries no subject and names nobody - only the thread ties it.
        self._item("a2", "t-one", text="follow-up nobody addressed")
        herald._route(self.cfg)

        self.assertEqual(self._drain(two), [])
        self.assertEqual(self._stored("a2")["thread_owner"], "session-one")

    def test_an_ambiguous_item_is_shown_to_nobody_rather_than_to_everybody(self):
        one = self._listener("one")
        two = self._listener("two")
        self._item("x1", "t-x", text="secret content")
        herald._route(self.cfg)

        self.assertEqual(self._drain(one), [])
        self.assertEqual(self._drain(two), [])

    def test_a_departed_owners_thread_is_not_handed_to_a_live_stranger(self):
        gone = self._listener("gone-session")
        self._item("g1", "t-gone")
        herald._claim_item("g1", "gone-session", "main")
        herald.clear_session(gone["session_id"])

        stranger = self._listener("stranger")
        self._item("g2", "t-gone", text="follow-up after the owner left")
        herald._route(self.cfg)

        self.assertEqual(self._drain(stranger), [])

    def test_a_generalist_is_not_given_work_another_session_declared(self):
        general = self._listener("general")
        self._listener("specialist", subjects=["pbi-738"])
        self._item("s1", "t-s", subject="pbi-738", text="738 detail")
        herald._route(self.cfg)

        self.assertEqual(self._drain(general), [])

    def _stored(self, item_id):
        return json.loads((herald.INBOX_DIR / f"{item_id}.json").read_text())


class ClearingFinishedWork(unittest.TestCase):
    """Records must not stay open once nothing is waiting on them."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"HERALD_DIR": str(root), "HERALD_AGENT": "solo"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name, sub in (("HERALD_DIR", ""), ("INBOX_DIR", "inbox"), ("OUTBOX_DIR", "outbox"),
                          ("FILES_DIR", "files"), ("QUEUE_DIR", "queue"), ("SESSIONS_DIR", "sessions"),
                          ("CONSUMERS_DIR", "consumers"), ("ACTIVITY_DIR", "activity"),
                          ("WORKING_DIR", "working"), ("FAILED_DIR", "failed")):
            if hasattr(herald, name):
                patcher = patch.object(herald, name, root / sub if sub else root)
                patcher.start()
                self.addCleanup(patcher.stop)
        for name, leaf in (("LOCK_PATH", "state.lock"), ("STATUS_PATH", "status.json"),
                           ("CONFIG_PATH", "config.json")):
            if hasattr(herald, name):
                patcher = patch.object(herald, name, root / leaf)
                patcher.start()
                self.addCleanup(patcher.stop)
        herald.ensure_dirs()
        self.cfg = {"me": "jamie", "peers": {}, "default_mailbox": "main", "mailboxes": ["main"]}

    def _sent(self, item_id="out1", thread="t1"):
        herald.atomic_write_json(herald.OUTBOX_DIR / f"{item_id}.json", {
            "id": item_id, "thread": thread, "to": "simon", "kind": "message",
            "text": "decisions you are unblocked on", "remote_ids": [item_id],
            "state": "awaiting_terminal", "awaiting_reply_ids": [item_id],
            "sent_ts": time.time(), "files": [],
        })

    def _inbound(self, item_id, thread="t1", reply_to="", meta=None, **extra):
        item = {
            "id": item_id, "thread": thread, "reply_to": reply_to, "from": "simon",
            "kind": "message", "text": "the real answer", "meta": meta or {},
            "received_ts": time.time(), "files": [],
        }
        item.update(extra)
        return item

    def _outgoing(self, item_id="out1"):
        return json.loads((herald.OUTBOX_DIR / f"{item_id}.json").read_text())

    def test_an_acknowledged_request_closes_when_the_answer_lands_on_the_thread(self):
        self._sent()
        herald._update_outstanding_request(
            self._inbound("ack1", reply_to="out1", meta={"herald_intent": "ack"}))
        self.assertEqual(self._outgoing()["state"], "awaiting_terminal")

        herald._update_outstanding_request(self._inbound("answer1", reply_to=""))

        self.assertEqual(self._outgoing()["state"], "handled")

    def test_an_unacknowledged_request_keeps_waiting_for_its_own_reply(self):
        self._sent()

        herald._update_outstanding_request(self._inbound("chatter", reply_to=""))

        self.assertEqual(self._outgoing()["state"], "awaiting_terminal")

    def test_another_thread_does_not_close_an_acknowledged_request(self):
        self._sent()
        herald._update_outstanding_request(
            self._inbound("ack1", reply_to="out1", meta={"herald_intent": "ack"}))

        herald._update_outstanding_request(self._inbound("elsewhere", thread="t2"))

        self.assertEqual(self._outgoing()["state"], "awaiting_terminal")

    def test_tidy_closes_a_stale_claim_whose_session_has_gone(self):
        stale = time.time() - 5 * 86400
        herald.atomic_write_json(herald.INBOX_DIR / "old.json", {
            "id": "old", "thread": "t9", "from": "simon", "kind": "result",
            "status": "done", "text": "row reset", "state": "active",
            "claimed_by": "dead-session", "claimed_at": stale, "received_ts": stale,
            "to_mailbox": "main", "files": [],
        })

        herald.cmd_tidy(self.cfg, argparse.Namespace(older_than=2, dry_run=False))

        self.assertEqual(json.loads((herald.INBOX_DIR / "old.json").read_text())["state"], "handled")

    def test_tidy_never_closes_unread_work(self):
        stale = time.time() - 5 * 86400
        herald.atomic_write_json(herald.INBOX_DIR / "unread.json", {
            "id": "unread", "thread": "t9", "from": "simon", "kind": "task",
            "text": "please run the tests", "state": "pending",
            "received_ts": stale, "to_mailbox": "main", "files": [],
        })

        herald.cmd_tidy(self.cfg, argparse.Namespace(older_than=2, dry_run=False))

        self.assertEqual(json.loads((herald.INBOX_DIR / "unread.json").read_text())["state"], "pending")

    def test_tidy_leaves_recent_work_alone(self):
        herald.atomic_write_json(herald.INBOX_DIR / "fresh.json", {
            "id": "fresh", "thread": "t9", "from": "simon", "kind": "result",
            "status": "done", "text": "just in", "state": "active",
            "claimed_by": "dead-session", "claimed_at": time.time(),
            "received_ts": time.time(), "to_mailbox": "main", "files": [],
        })

        herald.cmd_tidy(self.cfg, argparse.Namespace(older_than=2, dry_run=False))

        self.assertEqual(json.loads((herald.INBOX_DIR / "fresh.json").read_text())["state"], "active")

    def test_tidy_dry_run_changes_nothing(self):
        stale = time.time() - 5 * 86400
        self._sent()
        herald.atomic_write_json(herald.OUTBOX_DIR / "out1.json",
                                 {**self._outgoing(), "sent_ts": stale})

        herald.cmd_tidy(self.cfg, argparse.Namespace(older_than=2, dry_run=True))

        self.assertEqual(self._outgoing()["state"], "awaiting_terminal")
