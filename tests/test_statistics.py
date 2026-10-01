"""Statistics count confirmed outreach, independently of current thread status."""
from datetime import timedelta
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from autofollow import sending
from autofollow.db import (add_event, close_thread, create_tracked_thread, get_thread,
                           init_db, mark_replied, record_followup_sent)
from autofollow.mail import build_email
from autofollow.statistics import reply_statistics, set_statistics_excluded
from autofollow.timeutil import to_iso, utc_now


class StatisticsTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        self.addCleanup(self.con.close)
        init_db(self.con)

    def initial(self, key, max_followups=5):
        return create_tracked_thread(self.con, recipient='you@example.com', recipient_name=None,
            subject='Hello', thread_id=key, initial_message_id=key + '-initial',
            initial_rfc_message_id=None, initial_sent_at_utc=to_iso(utc_now()),
            latest_known_message_count=1, followup_delay_days=3,
            max_followups=max_followups, followup_template='Hello')

    def followup(self, tracked_id, number):
        record_followup_sent(self.con, tracked_id, gmail_message_id=f'{tracked_id}-followup-{number}',
                             rfc_message_id=None, new_message_count=None)

    def reply(self, tracked_id):
        return mark_replied(self.con, tracked_id, replied_at_utc=to_iso(utc_now()),
                            gmail_message_id=f'{tracked_id}-reply', from_header='you@example.com',
                            message_count=None)

    def test_empty_denominators_are_not_zero_percent(self):
        for con in (None, self.con):
            stats = reply_statistics(con)
            self.assertIsNone(stats['totals']['reply_rate'])
            self.assertEqual([g['followups'] for g in stats['groups']], [0, 1, 2, 3])
            self.assertTrue(all(g['reply_rate'] is None for g in stats['groups']))

    def test_exclusion_is_reversible_and_preserves_operational_data(self):
        tracked_id = self.initial('test-email')
        self.followup(tracked_id, 1)
        self.reply(tracked_id)
        before = get_thread(self.con, tracked_id)
        events = [tuple(row) for row in self.con.execute('SELECT * FROM events')]
        for _ in range(2):
            set_statistics_excluded(self.con, tracked_id=tracked_id,
                initial_message_id='test-email-initial', excluded=True)
        stats = reply_statistics(self.con)
        self.assertEqual(stats['totals']['sent'], 0)
        self.assertEqual(stats['totals']['replied'], 0)
        self.assertEqual(stats['excluded'], 1)
        self.assertEqual(get_thread(self.con, tracked_id), before)
        self.assertEqual([tuple(row) for row in self.con.execute('SELECT * FROM events')], events)
        for _ in range(2):
            set_statistics_excluded(self.con, tracked_id=tracked_id,
                initial_message_id='test-email-initial', excluded=False)
        self.assertEqual(reply_statistics(self.con)['groups'][1]['reply_rate'], 1)
        self.assertEqual(reply_statistics(self.con)['excluded'], 0)

    def test_exclusion_does_not_affect_later_outreach_in_same_thread(self):
        tracked_id = self.initial('reuse')
        self.followup(tracked_id, 1)
        set_statistics_excluded(self.con, tracked_id=tracked_id,
            initial_message_id='reuse-initial', excluded=True)
        self.con.execute("UPDATE tracked_threads SET initial_message_id='second' WHERE id=?", (tracked_id,))
        add_event(self.con, tracked_id, 'initial_sent', gmail_message_id='second')
        self.con.commit()
        self.reply(tracked_id)
        stats = reply_statistics(self.con)
        self.assertEqual(stats['totals'], dict(sent=1, replied=1, no_reply=0, reply_rate=1))
        self.assertEqual(stats['groups'][0]['sent'], 1)
        self.assertEqual(stats['groups'][1]['sent'], 0)
        self.assertEqual(stats['unattributed_events'], 0)
        # Legacy attribution still sees the excluded initial as a boundary.
        self.con.execute('UPDATE events SET enrollment_message_id=NULL')
        self.con.commit()
        self.assertEqual(reply_statistics(self.con)['groups'], stats['groups'])

    def test_invalid_exclusion_does_not_create_table_or_change_data(self):
        tracked_id = self.initial('valid')
        cases = [(True, 'valid-initial', True), (tracked_id, 'valid-initial', 'true'),
                 (tracked_id, 'missing', True), (999, 'valid-initial', True), (tracked_id, None, True)]
        for thread_id, message_id, excluded in cases:
            with self.subTest(case=(thread_id, message_id, excluded)), self.assertRaises(ValueError):
                set_statistics_excluded(self.con, tracked_id=thread_id,
                    initial_message_id=message_id, excluded=excluded)
        self.assertIsNone(self.con.execute("SELECT 1 FROM sqlite_master WHERE name='statistics_exclusions'").fetchone())
        self.assertEqual(reply_statistics(self.con)['totals']['sent'], 1)

    def test_actual_counts_closed_outreach_and_multiple_replies(self):
        for count in (0, 1, 2, 3, 4):
            for replied in (False, True):
                tracked_id = self.initial(f'{count}-{replied}')
                for number in range(count):
                    self.followup(tracked_id, number)
                if replied:
                    self.assertTrue(self.reply(tracked_id))
                    self.assertFalse(self.reply(tracked_id))
                close_thread(self.con, tracked_id)
        # Planned follow-ups must not move this initial-only outreach to group 5.
        self.initial('pending-plan', max_followups=5)
        stats = reply_statistics(self.con)
        groups = {g['followups']: g for g in stats['groups']}
        self.assertEqual(set(groups), {0, 1, 2, 3, 4})
        self.assertEqual(groups[0]['reply_rate'], 1 / 3)
        for count in (1, 2, 3, 4):
            self.assertEqual(groups[count]['reply_rate'], 0.5)
        self.assertEqual(stats['totals'], dict(sent=11, replied=5, no_reply=6, reply_rate=5/11))

    def test_migration_preserves_old_records_and_can_run_twice(self):
        tracked_id = self.initial('legacy')
        self.followup(tracked_id, 1)
        self.reply(tracked_id)
        before = get_thread(self.con, tracked_id)
        self.con.execute('ALTER TABLE events DROP COLUMN enrollment_message_id')
        self.con.execute('PRAGMA user_version=5')
        self.con.commit()
        old = reply_statistics(self.con)
        self.assertEqual(old['groups'][1]['reply_rate'], 1)
        init_db(self.con)
        init_db(self.con)
        self.assertEqual(get_thread(self.con, tracked_id), before)
        self.assertEqual(reply_statistics(self.con)['groups'], old['groups'])
        self.assertEqual(self.con.execute('SELECT count(*) FROM events').fetchone()[0], 3)

    def test_legacy_reenrollments_disclose_inferred_attribution(self):
        tracked_id = self.initial('reused')
        self.followup(tracked_id, 1)
        self.reply(tracked_id)
        add_event(self.con, tracked_id, 'initial_sent', gmail_message_id='new-initial')
        self.con.execute('UPDATE events SET enrollment_message_id=NULL')
        self.con.commit()
        stats = reply_statistics(self.con)
        self.assertEqual(stats['totals']['sent'], 2)
        self.assertEqual(stats['groups'][0]['replied'], 0)
        self.assertEqual(stats['groups'][1]['replied'], 1)
        self.assertEqual(stats['historically_inferred'], 1)

    def test_delayed_finalization_and_repeated_reconciliation(self):
        sent_count = 0
        def send_message(built, thread_id=None):
            nonlocal sent_count
            sent_count += 1
            return SimpleNamespace(gmail_message_id=f'm{sent_count}', thread_id='same-thread')
        gmail = SimpleNamespace(send_message=send_message)
        metadata = dict(recipient='you@example.com', subject='Hello', followup_delay_days=3,
                        max_followups=2, followup_template='Hello')
        def initial():
            return sending.deliver(self.con, gmail,
                build_email(to='you@example.com', subject='Hello', body='Hello'), metadata=metadata)
        tracked_id = initial()
        with patch.object(sending, 'finalize', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                sending.deliver(self.con, gmail,
                    build_email(to='you@example.com', subject='Hello', body='Following up'),
                    metadata={'followup_number': 1, 'observed_initial_message_id': 'm1',
                              'observed_last_followup_at_utc': None},
                    tracked_id=tracked_id, thread_id='same-thread')
        self.assertEqual(reply_statistics(self.con)['groups'][0]['sent'], 1)
        self.reply(tracked_id)
        # Give the old reply an earlier timestamp than the next enrollment.
        self.con.execute('UPDATE tracked_threads SET updated_at_utc=?', (to_iso(utc_now()-timedelta(days=1)),))
        self.con.commit()
        initial()
        for _ in range(2):
            sending.finalize(self.con, f'followup:{tracked_id}:1')
        stats = reply_statistics(self.con)
        self.assertEqual(stats['groups'][0]['sent'], 1)
        self.assertEqual(stats['groups'][0]['replied'], 0)
        self.assertEqual(stats['groups'][1]['sent'], 1)
        self.assertEqual(stats['groups'][1]['replied'], 1)
        self.assertEqual(stats['historically_inferred'], 0)
        # Previous journals already supplied the original message ID.
        self.con.execute('ALTER TABLE events DROP COLUMN enrollment_message_id')
        self.assertEqual(reply_statistics(self.con)['groups'], stats['groups'])

    def test_full_history_is_not_limited_to_dashboard_rows(self):
        tracked_id = self.initial('many')
        self.con.executemany("""INSERT INTO events(tracked_thread_id,event_type,gmail_message_id,occurred_at_utc)
            VALUES (?,'initial_sent',?,'2026-09-01T00:00:00+00:00')""",
            ((tracked_id, f'message-{i}') for i in range(5001)))
        self.assertEqual(reply_statistics(self.con)['totals']['sent'], 5002)

    def test_unattributed_events_are_reported_without_guessing_explicit_links(self):
        tracked_id = self.initial('orphan')
        self.followup(tracked_id, 1)
        self.con.execute("UPDATE events SET enrollment_message_id='missing' WHERE event_type='followup_sent'")
        stats = reply_statistics(self.con)
        self.assertEqual(stats['unattributed_events'], 1)
        self.assertEqual(stats['groups'][0]['sent'], 1)


if __name__ == '__main__':
    unittest.main()
