from datetime import timedelta
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import sqlite3

from autofollow import sending
from autofollow.app import check_one_thread
from autofollow.db import init_db, get_thread
from autofollow.mail import build_email
from autofollow.followup import next_due_at
from autofollow.timeutil import to_iso, utc_now


class Gmail:
    calls = 0
    fail = False
    recovered = None

    def send_message(self, built, thread_id=None):
        self.calls += 1
        if self.fail:
            raise TimeoutError('lost response')
        return SimpleNamespace(gmail_message_id=f'm{self.calls}', thread_id=thread_id or 't1')

    def find_sent_message(self, rfc_message_id):
        return self.recovered


class SendTests(TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        init_db(self.con)
        self.addCleanup(self.con.close)
        self.gmail = Gmail()
        self.built = build_email(to='you@example.com', subject='Hello', body='Hello')
        self.metadata = dict(recipient='you@example.com',subject='Hello',followup_delay_days=5,
            max_followups=2,followup_template='Hello {name}', sender_name='Pat',cc='cc@example.com')

    def initial(self):
        return sending.deliver(self.con,self.gmail,self.built,metadata=self.metadata)

    def test_confirmed_send_is_finalized_once(self):
        tracked_id = self.initial()
        self.assertEqual(sending.preferences(self.con, tracked_id)['sender_name'], 'Pat')
        followup = build_email(to='you@example.com',subject='Hello',body='Again')
        sending.deliver(self.con,self.gmail,followup,metadata={'followup_number':1},tracked_id=tracked_id,thread_id='t1')
        sending.finalize(self.con, f'followup:{tracked_id}:1')
        self.assertEqual(get_thread(self.con,tracked_id).followup_count,1)
        with self.assertRaises(RuntimeError):
            sending.deliver(self.con,self.gmail,followup,metadata={'followup_number':1},tracked_id=tracked_id,thread_id='t1')
        self.assertEqual(self.gmail.calls,2)

    def test_timeout_is_never_resent_and_can_be_reconciled(self):
        self.gmail.fail = True
        with self.assertRaises(RuntimeError):
            self.initial()
        self.assertEqual(len(sending.reconcile(self.con,self.gmail)),1)
        self.assertEqual(self.gmail.calls,1)
        self.gmail.recovered = SimpleNamespace(gmail_message_id='m1',thread_id='t1')
        self.assertEqual(sending.reconcile(self.con,self.gmail),[])
        self.assertEqual(sending.reconcile(self.con,self.gmail),[])
        self.assertEqual(self.con.execute('SELECT count(*) FROM tracked_threads').fetchone()[0],1)

    def test_crash_after_response_recovers_without_resend(self):
        with patch.object(sending,'finalize',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                self.initial()
        self.assertEqual(sending.reconcile(self.con,self.gmail),[])
        self.assertEqual(self.gmail.calls,1)
        self.assertEqual(self.con.execute('SELECT count(*) FROM tracked_threads').fetchone()[0],1)

    def test_manual_outgoing_resets_schedule(self):
        tracked_id = self.initial()
        old = to_iso(utc_now()-timedelta(days=10))
        self.con.execute('UPDATE tracked_threads SET initial_sent_at_utc=? WHERE id=?',(old,tracked_id))
        self.con.commit()
        now = utc_now()
        self.gmail.get_profile_email = lambda: 'me@example.com'
        self.gmail.get_thread_metadata = lambda _: {'messages':[{'id':'manual', 'internalDate':str(int(now.timestamp()*1000)),
            'payload':{'headers':[{'name':'From','value':'me@example.com'}]}}]}
        result = check_one_thread(cfg=SimpleNamespace(own_addresses=[]),gmail=self.gmail,con=self.con,
            thread=get_thread(self.con,tracked_id),dry_run=False,sender_name=None)
        self.assertIn('next due',result)
        self.assertEqual(self.gmail.calls,1)
        self.assertGreater(next_due_at(get_thread(self.con,tracked_id)),now+timedelta(days=4))

    def test_maxed_never_advertises_due_date(self):
        tracked_id = self.initial()
        self.con.execute("UPDATE tracked_threads SET status='maxed',followup_count=2 WHERE id=?",(tracked_id,))
        self.con.commit()
        self.gmail.get_profile_email = lambda: 'me@example.com'
        self.gmail.get_thread_metadata = lambda _: {'messages':[]}
        result = check_one_thread(cfg=SimpleNamespace(own_addresses=[]),gmail=self.gmail,con=self.con,
            thread=get_thread(self.con,tracked_id),dry_run=True,sender_name=None)
        self.assertIn('limit reached',result)
        self.assertIsNone(next_due_at(get_thread(self.con,tracked_id)))

    def test_invalid_recipient_fails_before_send(self):
        with self.assertRaises(ValueError):
            build_email(to='not-an-email',subject='Hello',body='Hello')

    def test_recovery_does_not_reopen_closed_thread(self):
        tracked_id = self.initial()
        followup = build_email(to='you@example.com',subject='Hello',body='Again')
        with patch.object(sending,'finalize',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                sending.deliver(self.con,self.gmail,followup,metadata={'followup_number':1},tracked_id=tracked_id,thread_id='t1')
        self.con.execute("UPDATE tracked_threads SET status='closed' WHERE id=?", (tracked_id,))
        self.con.commit()
        sending.reconcile(self.con,self.gmail)
        self.assertEqual(get_thread(self.con,tracked_id).status,'closed')
        self.assertEqual(get_thread(self.con,tracked_id).followup_count,1)

    def test_explicit_abandon_allows_retry_and_preserves_journal(self):
        tracked_id = self.initial()
        self.gmail.fail = True
        followup = build_email(to='you@example.com',subject='Hello',body='Again')
        with self.assertRaises(RuntimeError):
            sending.deliver(self.con,self.gmail,followup,metadata={'followup_number':1},tracked_id=tracked_id,thread_id='t1')
        sending.abandon_uncertain(self.con,self.gmail,f'followup:{tracked_id}:1')
        self.gmail.fail = False
        retry = build_email(to='you@example.com',subject='Hello',body='Again')
        sending.deliver(self.con,self.gmail,retry,metadata={'followup_number':1},tracked_id=tracked_id,thread_id='t1')
        self.assertEqual(get_thread(self.con,tracked_id).followup_count,1)
        self.assertEqual(self.con.execute("SELECT count(*) FROM send_jobs WHERE state='abandoned'").fetchone()[0],1)

    def test_grouped_initial_reenrolls_existing_thread(self):
        tracked_id = self.initial()
        another = build_email(to='you@example.com',subject='Hello',body='New initial')
        again = sending.deliver(self.con,self.gmail,another,metadata=self.metadata)
        self.assertEqual(again,tracked_id)
        self.assertEqual(get_thread(self.con,tracked_id).initial_message_id,'m2')
        self.assertEqual(self.con.execute('SELECT count(*) FROM tracked_threads').fetchone()[0],1)

    def test_dry_run_preferences_do_not_create_schema(self):
        self.assertFalse(sending.has_pending(self.con,1))
        self.assertEqual(sending.preferences(self.con,1),{})
        self.assertIsNone(self.con.execute("SELECT 1 FROM sqlite_master WHERE name='send_jobs'").fetchone())

    def test_overlapping_checks_claim_only_one_send(self):
        tracked_id = self.initial()
        followup = build_email(to='you@example.com',subject='Hello',body='Again')
        competing = build_email(to='you@example.com',subject='Hello',body='Again')
        actual_send = self.gmail.send_message
        def send_with_competitor(built, thread_id=None):
            with self.assertRaises(RuntimeError):
                sending.deliver(self.con,self.gmail,competing,metadata={'followup_number':1},tracked_id=tracked_id,thread_id='t1')
            return actual_send(built,thread_id)
        self.gmail.send_message = send_with_competitor
        sending.deliver(self.con,self.gmail,followup,metadata={'followup_number':1},tracked_id=tracked_id,thread_id='t1')
        self.assertEqual(self.gmail.calls,2)
        self.assertEqual(get_thread(self.con,tracked_id).followup_count,1)

    def test_reconcile_while_send_response_pending_is_idempotent(self):
        tracked_id = self.initial()
        followup = build_email(to='you@example.com',subject='Hello',body='Again')
        actual_send = self.gmail.send_message
        def send_with_recovery(built, thread_id=None):
            sent = actual_send(built,thread_id)
            self.gmail.recovered = sent
            sending.reconcile(self.con,self.gmail)
            return sent
        self.gmail.send_message = send_with_recovery
        sending.deliver(self.con,self.gmail,followup,metadata={'followup_number':1},tracked_id=tracked_id,thread_id='t1')
        self.assertEqual(get_thread(self.con,tracked_id).followup_count,1)

    def test_stale_schedule_snapshot_cannot_send(self):
        tracked_id = self.initial()
        snapshot = get_thread(self.con,tracked_id)
        self.con.execute("UPDATE tracked_threads SET last_followup_at_utc=? WHERE id=?", (to_iso(utc_now()),tracked_id))
        self.con.commit()
        followup = build_email(to='you@example.com',subject='Hello',body='Again')
        with self.assertRaisesRegex(RuntimeError,'schedule changed'):
            sending.deliver(self.con,self.gmail,followup,tracked_id=tracked_id,thread_id='t1',
                metadata={'followup_number':1,'observed_initial_message_id':snapshot.initial_message_id,
                    'observed_last_followup_at_utc':snapshot.last_followup_at_utc})
        self.assertEqual(self.gmail.calls,1)

    def test_delayed_initial_recovery_preserves_later_close(self):
        tracked_id = self.initial()
        another = build_email(to='you@example.com',subject='Hello',body='New initial')
        with patch.object(sending,'finalize',side_effect=RuntimeError('crash')):
            with self.assertRaises(RuntimeError):
                sending.deliver(self.con,self.gmail,another,metadata=self.metadata)
        self.con.execute("UPDATE tracked_threads SET status='closed',updated_at_utc=? WHERE id=?", (to_iso(utc_now()),tracked_id))
        self.con.commit()
        sending.reconcile(self.con,self.gmail)
        self.assertEqual(get_thread(self.con,tracked_id).status,'closed')

    def test_reply_during_concurrent_close_preserves_closed_state(self):
        from autofollow.db import close_thread

        tracked_id = self.initial()
        old = to_iso(utc_now() - timedelta(days=10))
        self.con.execute('UPDATE tracked_threads SET initial_sent_at_utc=? WHERE id=?', (old, tracked_id))
        self.con.commit()
        self.gmail.get_profile_email = lambda: 'me@example.com'

        def metadata_with_concurrent_close(_):
            close_thread(self.con, tracked_id, reason='User closed while check fetched Gmail')
            return {'messages': [{
                'id': 'reply',
                'internalDate': str(int(utc_now().timestamp() * 1000)),
                'payload': {'headers': [{'name': 'From', 'value': 'you@example.com'}]},
            }]}

        self.gmail.get_thread_metadata = metadata_with_concurrent_close
        result = check_one_thread(
            cfg=SimpleNamespace(own_addresses=[]), gmail=self.gmail, con=self.con,
            thread=get_thread(self.con, tracked_id), dry_run=False, sender_name=None,
        )
        self.assertEqual(get_thread(self.con, tracked_id).status, 'closed')
        self.assertIn('preserved current status', result)
        self.assertEqual(self.con.execute(
            "SELECT count(*) FROM events WHERE event_type='reply_detected'"
        ).fetchone()[0], 0)
        self.assertEqual(self.gmail.calls, 1)

    def test_send_dry_run_invalid_template_fails_before_auth(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from autofollow.app import command_send
        from autofollow.config import initialize_project

        with TemporaryDirectory() as directory:
            cfg = initialize_project(Path(directory) / 'config.toml')
            cfg.default_template_path.write_text('Hello {unsupported_field}')
            args = SimpleNamespace(
                config=str(cfg.config_path), body='Hello', body_file=None,
                followup_template=None, followup_days=None, max_followups=None,
                to='you@example.com', name=None, subject='Test', cc=None, bcc=None,
                sender_name=None, dry_run=True,
            )
            with patch('autofollow.gmail_client.GmailClient') as gmail_class:
                with self.assertRaisesRegex(ValueError, 'Unknown template field'):
                    command_send(args)
                gmail_class.assert_not_called()
            self.assertFalse(cfg.database_path.exists())

    def test_older_gmail_snapshot_cannot_overwrite_newer_schedule(self):
        tracked_id = self.initial()
        original = to_iso(utc_now() - timedelta(days=20))
        self.con.execute('UPDATE tracked_threads SET initial_sent_at_utc=? WHERE id=?', (original, tracked_id))
        self.con.commit()
        newer = to_iso(utc_now())
        older = utc_now() - timedelta(days=7)
        self.gmail.get_profile_email = lambda: 'me@example.com'

        def metadata_with_newer_check(_):
            self.con.execute('UPDATE tracked_threads SET last_followup_at_utc=? WHERE id=?', (newer, tracked_id))
            self.con.commit()
            return {'messages': [{
                'id': 'older-manual', 'internalDate': str(int(older.timestamp() * 1000)),
                'payload': {'headers': [{'name': 'From', 'value': 'me@example.com'}]},
            }]}

        self.gmail.get_thread_metadata = metadata_with_newer_check
        result = check_one_thread(
            cfg=SimpleNamespace(own_addresses=[]), gmail=self.gmail, con=self.con,
            thread=get_thread(self.con, tracked_id), dry_run=False, sender_name=None,
        )
        self.assertEqual(get_thread(self.con, tracked_id).last_followup_at_utc, newer)
        self.assertIn('next due', result)
        self.assertEqual(self.gmail.calls, 1)

    def test_old_reply_snapshot_cannot_resolve_reenrolled_thread(self):
        tracked_id = self.initial()
        original = to_iso(utc_now() - timedelta(days=20))
        self.con.execute('UPDATE tracked_threads SET initial_sent_at_utc=? WHERE id=?', (original, tracked_id))
        self.con.commit()
        self.gmail.get_profile_email = lambda: 'me@example.com'

        def metadata_with_reenrollment(_):
            self.con.execute('UPDATE tracked_threads SET initial_message_id=?,initial_sent_at_utc=? WHERE id=?',
                             ('new-cycle', to_iso(utc_now()), tracked_id))
            self.con.commit()
            return {'messages': [{
                'id': 'old-reply', 'internalDate': str(int((utc_now() - timedelta(days=1)).timestamp() * 1000)),
                'payload': {'headers': [{'name': 'From', 'value': 'you@example.com'}]},
            }]}

        self.gmail.get_thread_metadata = metadata_with_reenrollment
        result = check_one_thread(
            cfg=SimpleNamespace(own_addresses=[]), gmail=self.gmail, con=self.con,
            thread=get_thread(self.con, tracked_id), dry_run=False, sender_name=None,
        )
        self.assertEqual(get_thread(self.con, tracked_id).status, 'waiting')
        self.assertIn('preserved current status', result)
        self.assertEqual(self.con.execute("SELECT count(*) FROM events WHERE event_type='reply_detected'").fetchone()[0], 0)

    def test_concurrent_initial_send_is_blocked_before_second_delivery(self):
        competing = build_email(to='you@example.com', subject='Hello', body='Another original')
        actual_send = self.gmail.send_message

        def send_with_competitor(built, thread_id=None):
            with self.assertRaisesRegex(RuntimeError, 'initial delivery is still uncertain'):
                sending.deliver(self.con, self.gmail, competing, metadata=self.metadata)
            self.assertFalse(self.con.in_transaction)
            return actual_send(built, thread_id)

        self.gmail.send_message = send_with_competitor
        self.initial()
        self.assertEqual(self.gmail.calls, 1)

    def test_uncertain_original_blocks_old_sequence_followup(self):
        tracked_id = self.initial()
        self.gmail.fail = True
        another = build_email(to='you@example.com', subject='Hello', body='New original')
        with self.assertRaisesRegex(RuntimeError, 'Delivery uncertain'):
            sending.deliver(self.con, self.gmail, another, metadata=self.metadata)
        self.gmail.fail = False
        followup = build_email(to='you@example.com', subject='Hello', body='Old follow-up')
        with self.assertRaisesRegex(RuntimeError, 'initial delivery is still uncertain'):
            sending.deliver(self.con, self.gmail, followup, metadata={'followup_number': 1},
                            tracked_id=tracked_id, thread_id='t1')
        self.assertEqual(self.gmail.calls, 2)

    def test_followup_must_match_saved_recipient_and_thread(self):
        tracked_id = self.initial()
        for recipient, thread_id in [('other@example.com', 't1'), ('you@example.com', 'other-thread')]:
            with self.subTest(recipient=recipient, thread_id=thread_id):
                followup = build_email(to=recipient, subject='Hello', body='Follow-up')
                with self.assertRaisesRegex(RuntimeError, 'tracked conversation'):
                    sending.deliver(self.con, self.gmail, followup, metadata={'followup_number': 1},
                                    tracked_id=tracked_id, thread_id=thread_id)
                self.assertFalse(self.con.in_transaction)
        self.assertEqual(self.gmail.calls, 1)

    def test_account_binding_blocks_token_switch(self):
        self.gmail.get_profile_email = lambda: 'me@example.com'
        self.assertEqual(sending.bind_account(self.con, self.gmail), 'me@example.com')
        self.assertEqual(sending.bind_account(self.con, self.gmail), 'me@example.com')
        self.gmail.get_profile_email = lambda: 'other@example.com'
        with self.assertRaisesRegex(RuntimeError, 'different Gmail account'):
            sending.bind_account(self.con, self.gmail)
        self.assertFalse(self.con.in_transaction)
        self.assertEqual(self.gmail.calls, 0)

    def test_legacy_journal_account_is_verified_before_binding(self):
        self.built = build_email(to='you@example.com', subject='Hello', body='Hello',
                                 sender_email='original@example.com')
        self.initial()
        self.gmail.get_profile_email = lambda: 'wrong@example.com'
        with self.assertRaisesRegex(RuntimeError, 'different Gmail account'):
            sending.bind_account(self.con, self.gmail)
        self.gmail.get_profile_email = lambda: 'original@example.com'
        self.assertEqual(sending.bind_account(self.con, self.gmail), 'original@example.com')

    def test_legacy_threads_without_sender_require_original_sent_evidence(self):
        self.initial()
        self.gmail.get_profile_email = lambda: 'me@example.com'
        self.gmail.get_thread_metadata = lambda _: {'messages': []}
        with self.assertRaisesRegex(RuntimeError, 'Cannot verify'):
            sending.bind_account(self.con, self.gmail)
        self.gmail.get_thread_metadata = lambda _: {'messages': [{
            'id': 'm1', 'labelIds': ['SENT'],
            'payload': {'headers': [{'name': 'From', 'value': 'Me <me@example.com>'}]},
        }]}
        self.assertEqual(sending.bind_account(self.con, self.gmail), 'me@example.com')
        self.assertEqual(self.gmail.calls, 1)

    def test_read_only_account_check_never_creates_tables_or_binding(self):
        self.gmail.get_profile_email = lambda: 'me@example.com'
        before = self.con.iterdump()
        original = list(before)
        self.assertEqual(sending.bind_account(self.con, self.gmail, persist=False), 'me@example.com')
        self.assertEqual(list(self.con.iterdump()), original)
        self.assertFalse(self.con.in_transaction)
