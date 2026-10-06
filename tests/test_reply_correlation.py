"""Sanitized concurrent-request replay and adversarial correlation checks."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import herald


class ReplyCorrelation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for name, path in {
            'HERALD_DIR': root, 'OUTBOX_DIR': root / 'outbox',
            'STATE_LOCK_PATH': root / 'state.lock',
            **{name + '_DIR': root / name.lower() for name in (
                'INBOX', 'FILES', 'QUEUE', 'ACTIVITY', 'WORKING',
                'SESSIONS', 'CONSUMERS', 'FAILED')},
        }.items():
            p = patch.object(herald, name, path)
            p.start()
            self.addCleanup(p.stop)

    def request(self, name, acknowledged=True, **extra):
        record = dict(id=name, thread='shared-thread', to='peer', kind='task',
                      state='awaiting_terminal', remote_ids=[name],
                      awaiting_reply_ids=[name], sent_ts=10,
                      acknowledged_ids=[name] if acknowledged else [])
        record.update(extra)
        herald.atomic_write_json(herald.OUTBOX_DIR / (name + '.json'), record)

    def response(self, reply_to='', **extra):
        item = dict(id='response', thread='shared-thread', **{'from': 'peer'},
                    reply_to=reply_to, kind='result', status='done',
                    meta={}, received_ts=20)
        item.update(extra)
        herald._update_outstanding_request(item)

    def state(self, name):
        return json.loads((herald.OUTBOX_DIR / (name + '.json')).read_text())['state']

    def test_terminal_reply_settles_only_its_request(self):
        self.request('setup')
        self.request('access')
        self.response('access')
        self.assertEqual(self.state('access'), 'handled')
        self.assertEqual(self.state('setup'), 'awaiting_terminal')

    def test_unknown_explicit_reply_does_not_answer_thread_requests(self):
        self.request('setup')
        self.response('unknown-request')
        self.assertEqual(self.state('setup'), 'awaiting_terminal')

    def test_wrong_authenticated_peer_cannot_settle_matching_id(self):
        self.request('setup')
        self.response('setup', **{'from': 'other-peer'})
        self.assertEqual(self.state('setup'), 'awaiting_terminal')

    def test_wrong_thread_cannot_settle_matching_id(self):
        self.request('setup')
        self.response('setup', thread='other-thread')
        self.assertEqual(self.state('setup'), 'awaiting_terminal')

    def test_delayed_duplicate_does_not_settle_new_request(self):
        self.request('old')
        self.response('old')
        self.request('new')
        self.response('old', id='delayed-duplicate')
        self.assertEqual(self.state('new'), 'awaiting_terminal')

    def test_reply_using_remote_delivery_id_is_correlated(self):
        self.request('local', remote_ids=['remote'], awaiting_reply_ids=['remote'],
                     acknowledged_ids=['remote'])
        self.request('other')
        self.response('remote')
        self.assertEqual(self.state('local'), 'handled')
        self.assertEqual(self.state('other'), 'awaiting_terminal')

    def test_progress_preserves_both_requests_and_approval_wait(self):
        for status in ('accepted', 'working'):
            with self.subTest(status=status):
                self.request('setup')
                self.request('access')
                self.response('access', status=status)
                self.assertEqual(self.state('access'), 'awaiting_terminal')
                self.assertEqual(self.state('setup'), 'awaiting_terminal')

    def test_failed_result_is_terminal_only_for_its_request(self):
        self.request('setup')
        self.request('access')
        self.response('access', status='failed')
        self.assertEqual(self.state('access'), 'handled')
        self.assertEqual(self.state('setup'), 'awaiting_terminal')

    def test_uncorrelated_thread_answer_keeps_legacy_behavior(self):
        self.request('setup')
        self.response()
        self.assertEqual(self.state('setup'), 'handled')

    def test_completion_words_do_not_override_explicit_progress(self):
        # Terminal-sounding text tagged ack was observed in local history.
        # The daemon must not infer an answer or permission from its prose.
        self.request('setup')
        self.response('setup', meta={'herald_intent': 'ack'},
                      text='Cleanup complete. No further action needed.')
        self.assertEqual(self.state('setup'), 'awaiting_terminal')

    def test_unacknowledged_setup_survives_unrelated_fyi(self):
        # Shape observed in local history: setup has no acknowledgement.
        self.request('setup', acknowledged=False)
        self.request('access', acknowledged=False)
        self.response('access', kind='message', status='')
        self.assertEqual(self.state('access'), 'handled')
        self.assertEqual(self.state('setup'), 'awaiting_terminal')

    def test_multirecipient_result_preserves_other_recipient(self):
        self.request('setup', remote_ids=['one', 'two'],
                     awaiting_reply_ids=['one', 'two'], acknowledged_ids=['one', 'two'])
        self.response('one')
        record = json.loads((herald.OUTBOX_DIR / 'setup.json').read_text())
        self.assertEqual(record['awaiting_reply_ids'], ['two'])
        self.assertEqual(record['state'], 'awaiting_terminal')

    def cached_reply(self, **extra):
        response = dict(id='answer', reply_to='request', thread='shared-thread',
                        kind='result', status='done', meta={}, **{'from': 'peer'})
        response.update(extra)
        herald.atomic_write_json(herald.INBOX_DIR / 'answer.json', response)
        herald._record_outbox(dict(kind='task', text='synthetic', _expects_terminal=True),
                              dict(id='request', thread='shared-thread'), 'peer')
        return json.loads((herald.OUTBOX_DIR / 'request.json').read_text())

    def test_cached_valid_terminal_before_outbox_record_completes(self):
        self.assertEqual(self.cached_reply()['state'], 'handled')

    def test_cached_wrong_peer_terminal_does_not_complete(self):
        self.assertEqual(self.cached_reply(**{'from': 'other-peer'})['state'], 'awaiting_terminal')

    def test_cached_wrong_thread_terminal_does_not_complete(self):
        self.assertEqual(self.cached_reply(thread='other-thread')['state'], 'awaiting_terminal')

    def test_cached_missing_peer_or_thread_fails_closed(self):
        for field in ('from', 'thread'):
            with self.subTest(field=field):
                self.assertEqual(self.cached_reply(**{field: ''})['state'], 'awaiting_terminal')

    def test_cached_progress_does_not_complete(self):
        for status in ('accepted', 'working'):
            with self.subTest(status=status):
                self.assertEqual(self.cached_reply(status=status)['state'], 'awaiting_terminal')


if __name__ == '__main__':
    unittest.main()
