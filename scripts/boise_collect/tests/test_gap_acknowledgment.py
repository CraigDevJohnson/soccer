"""Operator acknowledgments preserve source failures and only finish job scope."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts.boise_collect.__main__ import main
from scripts.boise_collect.model import parse_response
from scripts.boise_collect.store import Store


ORIGIN = 'http://127.0.0.1:12345'
DAY = '2026-11-09'
HASH = 'a' * 64
REASON = 'confirmed_empty_game_slot'


class GapAcknowledgmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root, ORIGIN)
        self.addCleanup(lambda: self.store.close())
        self.day = date.fromisoformat(DAY)
        self.store.queue_interval(self.day, self.day)
        attempt = self.store.start_attempt('day', DAY, 1)
        self.store.succeed(attempt, parse_response('day', DAY, {'data': {}}), 'c' * 64, 2)
        self.store.begin_refresh('refresh', self.day, self.day, False, 3)

    def fail(self, now, *, outcome='invalid', status=200, error='invalid_source_response', digest=HASH, retry_at=None):
        row = self.store.conn.execute("SELECT status FROM resources WHERE kind='day' AND key=?", (DAY,)).fetchone()
        if row[0] != 'pending':
            self.store.requeue('day', DAY)
        attempt = self.store.start_attempt('day', DAY, now)
        self.store.fail(attempt, outcome, status, error, digest, retry_at, now + 1)
        return attempt

    def stable_failure(self):
        previous = self.fail(4)
        latest = self.fail(6, retry_at=120)
        self.store.set_next_request_at(240)
        self.store.finish_refresh('refresh', 8)
        return previous, latest

    def preserved_state(self):
        tables = ('resources', 'attempts', 'snapshots', 'discoveries', 'games', 'game_observations', 'standings', 'player_leaders', 'meta')
        return {table: [tuple(r) for r in self.store.conn.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}

    def acknowledge(self, **changes):
        args = dict(job_id='refresh', kind='day', key=DAY, body_sha256=HASH, reason=REASON, now=9)
        args.update(changes)
        return self.store.acknowledge_refresh_gap(**args)

    def test_acknowledgment_preserves_source_history_and_deadlines_and_is_idempotent(self):
        previous, latest = self.stable_failure()
        before = self.preserved_state()
        evidence = {p.name: p.read_bytes() for p in self.store.evidence_dir.iterdir()}
        result = self.acknowledge()
        self.assertFalse(result['already_recorded'])
        self.assertEqual(result['refresh_job']['status'], 'complete_with_gaps')
        self.assertEqual((result['refresh_job']['pending'], result['refresh_job']['skipped']), (0, 1))
        ack = result['acknowledgment']
        self.assertEqual((ack['previous_attempt_id'], ack['latest_attempt_id']), (previous, latest))
        self.assertEqual((ack['body_sha256'], ack['reason'], ack['recorded_at']), (HASH, REASON, 9))
        self.assertEqual(self.preserved_state(), before)
        self.assertEqual({p.name: p.read_bytes() for p in self.store.evidence_dir.iterdir()}, evidence)
        repeated = self.acknowledge(now=100)
        self.assertTrue(repeated['already_recorded'])
        self.assertEqual(repeated['acknowledgment'], ack)
        self.assertEqual(repeated['refresh_job']['completed_at'], 9)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM refresh_gap_acknowledgments').fetchone()[0], 1)
        self.assertEqual(self.preserved_state(), before)
        self.assertEqual(list(self.store.conn.execute('PRAGMA foreign_key_check')), [])

    def test_one_attempt_and_stale_or_changing_hashes_are_rejected(self):
        self.fail(4)
        with self.assertRaisesRegex(ValueError, 'two consecutive'):
            self.acknowledge()
        self.fail(6, digest='b' * 64)
        before = self.preserved_state()
        for digest in (HASH, 'b' * 64):
            with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, 'two consecutive'):
                self.acknowledge(body_sha256=digest)
        self.assertEqual(self.preserved_state(), before)
        self.assertEqual(self.store.refresh_job('refresh')['pending'], 1)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM refresh_gap_acknowledgments').fetchone()[0], 0)

    def test_other_statuses_and_errors_cannot_be_acknowledged(self):
        self.fail(4)
        self.fail(6)
        with self.store.conn:
            self.store.conn.execute("UPDATE resources SET status='invalid' WHERE kind='day' AND key=?", (DAY,))
        for status, code, error in [('blocked', 401, 'authentication_refused'), ('blocked', 403, 'authentication_refused'),
                                    ('failed', 503, 'http_503'), ('missing', 404, 'source_not_found'),
                                    ('invalid', 200, 'other_validation_error')]:
            with self.store.conn:
                self.store.conn.execute("UPDATE resources SET status=?,http_status=?,last_error=? WHERE kind='day' AND key=?", (status, code, error, DAY))
            with self.subTest(status=status, code=code), self.assertRaisesRegex(ValueError, 'current HTTP 200'):
                self.acknowledge()
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM refresh_gap_acknowledgments').fetchone()[0], 0)

    def test_two_consecutive_ledger_failures_are_required_even_if_resource_matches(self):
        self.fail(4, outcome='failed', status=503, error='http_503')
        self.fail(6)
        with self.assertRaisesRegex(ValueError, 'two consecutive'):
            self.acknowledge()
        self.assertEqual(self.store.refresh_job('refresh')['pending'], 1)

    def test_membership_kind_reason_and_existing_ack_are_checked(self):
        self.stable_failure()
        with self.assertRaisesRegex(ValueError, 'pending member'):
            self.acknowledge(key='2026-11-10')
        with self.assertRaisesRegex(ValueError, 'diagnosed day'):
            self.acknowledge(kind='division', key='12')
        with self.assertRaisesRegex(ValueError, 'confirmed_empty_game_slot'):
            self.acknowledge(reason='ignore_error')
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            self.acknowledge(body_sha256='bad')
        self.acknowledge()
        with self.assertRaisesRegex(ValueError, 'different immutable'):
            self.acknowledge(body_sha256='b' * 64)
        for command in ('UPDATE refresh_gap_acknowledgments SET reason=reason', 'DELETE FROM refresh_gap_acknowledgments'):
            with self.subTest(command=command), self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
                with self.store.conn:
                    self.store.conn.execute(command)
        self.store.conn.execute('PRAGMA recursive_triggers=OFF')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'immutable'):
            with self.store.conn:
                self.store.conn.execute(
                    'INSERT OR REPLACE INTO refresh_gap_acknowledgments '
                    'SELECT job_id,kind,key,body_sha256,previous_attempt_id,latest_attempt_id,reason,99 '
                    'FROM refresh_gap_acknowledgments'
                )
        self.assertEqual(self.store.conn.execute('SELECT recorded_at FROM refresh_gap_acknowledgments').fetchone()[0], 9)
        self.assertTrue(self.acknowledge(now=100)['already_recorded'])

    def test_ack_audit_member_and_job_changes_roll_back_together(self):
        self.stable_failure()
        before = self.preserved_state()
        self.store.conn.execute("CREATE TRIGGER fail_ack_finish BEFORE UPDATE ON refresh_jobs BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.acknowledge()
        self.assertEqual(self.preserved_state(), before)
        self.assertEqual(self.store.conn.execute('SELECT count(*) FROM refresh_gap_acknowledgments').fetchone()[0], 0)
        self.assertEqual(self.store.refresh_job('refresh')['pending'], 1)
        self.assertEqual(self.store.refresh_job('refresh')['status'], 'needs_attention')

    def test_cli_acknowledgment_requires_no_bruno_and_can_repeat(self):
        self.stable_failure()
        self.store.close()
        args = ['acknowledge-gap', '--data-dir', str(self.root), '--job-id', 'refresh', '--kind', 'day',
                '--key', DAY, '--body-sha256', HASH, '--reason', REASON]
        with patch('scripts.boise_collect.__main__.BrunoClient', side_effect=AssertionError('must not create a network client')):
            for repeated in (False, True):
                out, error = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(error):
                    code = main(args)
                self.assertEqual(code, 0, error.getvalue())
                self.assertEqual(json.loads(out.getvalue())['already_recorded'], repeated)
        self.store = Store(self.root, ORIGIN)
        self.assertEqual(self.store.refresh_job('refresh')['status'], 'complete_with_gaps')
        self.assertEqual(self.store.conn.execute("SELECT status FROM resources WHERE kind='day' AND key=?", (DAY,)).fetchone()[0], 'invalid')


if __name__ == '__main__':
    unittest.main()
