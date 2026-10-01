"""Per-message follow-up plans: validation, recovery, scheduling and CLI isolation."""
import io
import json
import sqlite3
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from autofollow import sending
from autofollow.app import check_one_thread, command_send
from autofollow.config import initialize_project
from autofollow.db import get_thread, init_db
from autofollow.followup import next_due_at
from autofollow.mail import build_email
from autofollow.sequence import parse_plan, progress, saved_plan, validate_plan
from autofollow.timeutil import from_iso, to_iso, utc_now

PLAN = [{'day': 2, 'body': 'First: literal {variable} and {"key": 1}.'},
        {'day': 7, 'body': 'Second: a different message.\nThanks!'}]
THREE_PLAN = [*PLAN, {'day': 14, 'body': 'Third: a final follow-up.\nThanks again!'}]


class PlanValidationTests(TestCase):
    def test_zero_through_three_messages_preserve_literal_text(self):
        for plan in ([], PLAN[:1], PLAN, THREE_PLAN):
            with self.subTest(plan=plan):
                self.assertEqual(parse_plan(json.dumps(plan)), plan)
                self.assertEqual(validate_plan(plan), plan)
        copied = validate_plan(PLAN)
        self.assertIsNot(copied, PLAN)
        self.assertIsNot(copied[0], PLAN[0])

    def test_invalid_plans_fail_closed(self):
        bad = []
        bad.extend([None, {}, '[]', [*PLAN, PLAN[1]],
                    [*THREE_PLAN, {'day': 21, 'body': 'Fourth'}], [{'day': 1}],
                    [{'day': 1, 'body': 'x', 'extra': True}]])
        bad.extend([[{'day': day, 'body': 'x'}] for day in (0, -1, 3651, True, 1.0, '2', None)])
        bad.extend([[{'day': 1, 'body': body}] for body in ('', ' \n ', None, 123, 'x' * 50001, '\x00')])
        bad.extend([[{'day': 2, 'body': 'a'}, {'day': day, 'body': 'b'}] for day in (1, 2)])
        for plan in bad:
            with self.subTest(plan=str(plan)[:120]):
                with self.assertRaises(ValueError):
                    validate_plan(plan)
        for raw in ('{', 'null', 'true', '1'):
            with self.assertRaises(ValueError):
                parse_plan(raw)

    def test_progress_uses_cycle_baseline_and_rejects_corruption(self):
        thread = SimpleNamespace(followup_plan=json.dumps(PLAN), followup_count=9,
                                 followup_plan_start_count=8, max_followups=10)
        self.assertEqual(progress(thread), (1, 2))
        for count in (7, 11):
            thread.followup_count = count
            with self.assertRaises(ValueError):
                progress(thread)
        self.assertIsNone(saved_plan(SimpleNamespace()))

    def test_custom_days_and_manual_or_delayed_followup_keep_gap(self):
        initial = datetime(2026, 1, 1, tzinfo=timezone.utc)
        thread = SimpleNamespace(status='waiting', followup_count=0, max_followups=2,
            followup_plan=json.dumps(PLAN), followup_plan_start_count=0,
            initial_sent_at_utc=to_iso(initial), last_followup_at_utc=None)
        self.assertEqual(next_due_at(thread), initial + timedelta(days=2))
        thread.last_followup_at_utc = to_iso(initial + timedelta(days=3))
        self.assertEqual(next_due_at(thread), initial + timedelta(days=5))
        thread.followup_count = 1
        thread.last_followup_at_utc = to_iso(initial + timedelta(days=10))
        self.assertEqual(next_due_at(thread), initial + timedelta(days=15))
        thread.followup_count = 2
        self.assertIsNone(next_due_at(thread))


class FakeGmail:
    def __init__(self):
        self.sent = []
        self.fail = False
        self.recovered = None
        self.messages = []

    def send_message(self, built, thread_id=None):
        self.sent.append(built)
        if self.fail:
            raise TimeoutError('response lost')
        return SimpleNamespace(gmail_message_id=f'm{len(self.sent)}', thread_id=thread_id or 'thread')

    def find_sent_message(self, rfc_message_id):
        return self.recovered

    def get_profile_email(self):
        return 'me@example.com'

    def get_thread_metadata(self, thread_id):
        return {'messages': self.messages}


class PlanDeliveryTests(TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        init_db(self.con)
        self.addCleanup(self.con.close)
        self.gmail = FakeGmail()
        self.metadata = dict(recipient='you@example.com', subject='Hello', followup_delay_days=2,
            max_followups=2, followup_template='Invalid legacy {unknown}', followup_plan=PLAN)

    def initial(self):
        return sending.deliver(self.con, self.gmail,
            build_email(to='you@example.com', subject='Hello', body='Original'), metadata=self.metadata)

    def check(self, tracked_id):
        return check_one_thread(cfg=SimpleNamespace(own_addresses=[]), gmail=self.gmail,
            con=self.con, thread=get_thread(self.con, tracked_id), dry_run=False, sender_name=None)

    def make_due(self, tracked_id):
        self.con.execute('UPDATE tracked_threads SET initial_sent_at_utc=? WHERE id=?',
                         (to_iso(utc_now() - timedelta(days=20)), tracked_id))
        self.con.commit()

    def test_distinct_bodies_in_order_and_no_late_catchup_double_send(self):
        tracked_id = self.initial()
        self.make_due(tracked_id)
        self.check(tracked_id)
        self.assertEqual(self.gmail.sent[-1].message.get_content().rstrip('\n'), PLAN[0]['body'])
        self.check(tracked_id)
        self.assertEqual(len(self.gmail.sent), 2)
        self.con.execute('UPDATE tracked_threads SET last_followup_at_utc=? WHERE id=?',
                         (to_iso(utc_now() - timedelta(days=6)), tracked_id))
        self.con.commit()
        self.check(tracked_id)
        self.assertEqual(self.gmail.sent[-1].message.get_content().rstrip('\n'), PLAN[1]['body'])
        self.assertEqual(get_thread(self.con, tracked_id).status, 'maxed')
        self.check(tracked_id)
        self.assertEqual(len(self.gmail.sent), 3)

    def test_stored_placeholder_blocks_automatic_send(self):
        tracked_id = self.initial()
        self.make_due(tracked_id)
        self.con.execute('UPDATE tracked_threads SET followup_plan=? WHERE id=?',
                         (json.dumps([{'day': 3, 'body': 'Hi [name]'}]), tracked_id))
        self.con.commit()
        with self.assertRaisesRegex(ValueError, 'unresolved placeholder'):
            self.check(tracked_id)
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(get_thread(self.con, tracked_id).followup_count, 0)

    def test_three_messages_preserve_delayed_spacing_and_stop_at_limit(self):
        self.metadata.update(followup_plan=THREE_PLAN, max_followups=3)
        tracked_id = self.initial()
        self.make_due(tracked_id)
        original = from_iso(get_thread(self.con, tracked_id).initial_sent_at_utc)
        # All planned dates have elapsed; each delayed send must still leave a gap.
        for index, day in enumerate((20, 25, 32)):
            now = original + timedelta(days=day)
            with patch('autofollow.followup.utc_now', return_value=now), patch('autofollow.sending.utc_now', return_value=now):
                self.check(tracked_id)
                self.check(tracked_id)
            self.assertEqual(len(self.gmail.sent), index + 2)
            self.assertEqual(self.gmail.sent[-1].message.get_content().rstrip('\n'), THREE_PLAN[index]['body'])
            thread = get_thread(self.con, tracked_id)
            self.assertEqual(progress(thread), (index + 1, 3))
            if index < 2:
                gap = THREE_PLAN[index + 1]['day'] - THREE_PLAN[index]['day']
                self.assertEqual(next_due_at(thread), now + timedelta(days=gap))
        self.assertEqual(thread.status, 'maxed')
        self.assertIsNone(next_due_at(thread))
        with patch('autofollow.followup.utc_now', return_value=original + timedelta(days=100)):
            self.check(tracked_id)
        self.assertEqual(len(self.gmail.sent), 4)

    def test_reply_after_second_cancels_third_message(self):
        self.metadata.update(followup_plan=THREE_PLAN, max_followups=3)
        tracked_id = self.initial()
        self.make_due(tracked_id)
        self.check(tracked_id)
        self.con.execute('UPDATE tracked_threads SET last_followup_at_utc=? WHERE id=?',
                         (to_iso(utc_now() - timedelta(days=6)), tracked_id))
        self.con.commit()
        self.check(tracked_id)
        self.gmail.messages = [{'id': 'reply', 'internalDate': str(int((utc_now() + timedelta(seconds=1)).timestamp() * 1000)),
            'payload': {'headers': [{'name': 'From', 'value': 'you@example.com'}]}}]
        self.check(tracked_id)
        self.assertEqual(get_thread(self.con, tracked_id).status, 'replied')
        self.assertEqual(progress(get_thread(self.con, tracked_id)), (2, 3))
        self.assertEqual(len(self.gmail.sent), 3)

    def test_day_three_and_seven_are_measured_from_original(self):
        original = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.metadata['followup_plan'] = [{'day': 3, 'body': 'First message'},
                                          {'day': 7, 'body': 'Second message'}]
        tracked_id = self.initial()
        self.con.execute('UPDATE tracked_threads SET initial_sent_at_utc=? WHERE id=?',
                         (to_iso(original), tracked_id))
        self.con.commit()
        with patch('autofollow.followup.utc_now', return_value=original + timedelta(days=2)):
            self.check(tracked_id)
        self.assertEqual(len(self.gmail.sent), 1)
        with patch('autofollow.followup.utc_now', return_value=original + timedelta(days=3)), patch('autofollow.sending.utc_now', return_value=original + timedelta(days=3)):
            self.check(tracked_id)
        self.assertEqual(self.gmail.sent[-1].message.get_content().strip(), 'First message')
        self.assertEqual(next_due_at(get_thread(self.con, tracked_id)), original + timedelta(days=7))
        with patch('autofollow.followup.utc_now', return_value=original + timedelta(days=6)):
            self.check(tracked_id)
        self.assertEqual(len(self.gmail.sent), 2)
        with patch('autofollow.followup.utc_now', return_value=original + timedelta(days=7)), patch('autofollow.sending.utc_now', return_value=original + timedelta(days=7)):
            self.check(tracked_id)
        self.assertEqual(self.gmail.sent[-1].message.get_content().strip(), 'Second message')
        self.assertEqual(len(self.gmail.sent), 3)

    def test_reply_cancels_second_message(self):
        tracked_id = self.initial()
        self.make_due(tracked_id)
        self.check(tracked_id)
        self.gmail.messages = [{'id': 'reply', 'internalDate': str(int(utc_now().timestamp() * 1000)),
            'payload': {'headers': [{'name': 'From', 'value': 'you@example.com'}]}}]
        self.check(tracked_id)
        self.assertEqual(get_thread(self.con, tracked_id).status, 'replied')
        self.assertEqual(len(self.gmail.sent), 2)

    def test_initial_uncertainty_recovers_plan_snapshot(self):
        self.gmail.fail = True
        with self.assertRaises(RuntimeError):
            self.initial()
        self.gmail.recovered = SimpleNamespace(gmail_message_id='m1', thread_id='thread')
        self.assertEqual(sending.reconcile(self.con, self.gmail), [])
        thread = get_thread(self.con, 1)
        self.assertEqual(saved_plan(thread), PLAN)
        self.assertEqual(progress(thread), (0, 2))
        self.assertEqual(len(self.gmail.sent), 1)

    def test_uncertain_followup_recovers_once_and_advances_to_second(self):
        tracked_id = self.initial()
        self.make_due(tracked_id)
        self.gmail.fail = True
        with self.assertRaises(RuntimeError):
            self.check(tracked_id)
        self.gmail.recovered = SimpleNamespace(gmail_message_id='m2', thread_id='thread')
        self.assertEqual(sending.reconcile(self.con, self.gmail), [])
        self.assertEqual(sending.reconcile(self.con, self.gmail), [])
        self.assertEqual(progress(get_thread(self.con, tracked_id)), (1, 2))
        self.check(tracked_id)
        self.assertEqual(len(self.gmail.sent), 2)

    def test_reenrollment_preserves_lifetime_ids_but_starts_new_plan(self):
        tracked_id = self.initial()
        self.make_due(tracked_id)
        self.check(tracked_id)
        self.metadata['followup_plan'] = [{'day': 3, 'body': 'New cycle message'}]
        self.metadata['max_followups'] = 1
        self.assertEqual(self.initial(), tracked_id)
        thread = get_thread(self.con, tracked_id)
        self.assertEqual(thread.followup_count, 1)
        self.assertEqual(thread.followup_plan_start_count, 1)
        self.assertEqual(thread.max_followups, 2)
        self.assertEqual(progress(thread), (0, 1))
        self.make_due(tracked_id)
        self.check(tracked_id)
        self.assertEqual(self.gmail.sent[-1].message.get_content().strip(), 'New cycle message')
        self.assertEqual(progress(get_thread(self.con, tracked_id)), (1, 1))

    def test_old_cycle_recovery_does_not_consume_new_cycle_message(self):
        tracked_id = self.initial()
        self.make_due(tracked_id)
        self.gmail.fail = True
        with self.assertRaises(RuntimeError):
            self.check(tracked_id)
        self.gmail.fail = False
        self.metadata['followup_plan'] = [{'day': 3, 'body': 'New cycle'}]
        self.metadata['max_followups'] = 1
        self.initial()
        self.gmail.recovered = SimpleNamespace(gmail_message_id='m2', thread_id='thread')
        self.assertEqual(sending.reconcile(self.con, self.gmail), [])
        thread = get_thread(self.con, tracked_id)
        self.assertEqual(progress(thread), (0, 1))
        self.assertEqual(thread.status, 'waiting')
        self.make_due(tracked_id)
        self.check(tracked_id)
        self.assertEqual(self.gmail.sent[-1].message.get_content().strip(), 'New cycle')
        self.assertEqual(progress(get_thread(self.con, tracked_id)), (1, 1))

    def test_changed_plan_prevents_sending_stale_prepared_body(self):
        tracked_id = self.initial()
        thread = get_thread(self.con, tracked_id)
        self.con.execute('UPDATE tracked_threads SET followup_plan=? WHERE id=?',
                         (json.dumps([{'day': 3, 'body': 'Changed message'}]), tracked_id))
        self.con.commit()
        stale = build_email(to='you@example.com', subject='Hello', body=PLAN[0]['body'])
        with self.assertRaisesRegex(RuntimeError, 'plan changed'):
            sending.deliver(self.con, self.gmail, stale, tracked_id=tracked_id, thread_id='thread',
                metadata={'followup_number': 1, 'observed_followup_plan': thread.followup_plan})
        self.assertEqual(len(self.gmail.sent), 1)

    def test_zero_followups_and_legacy_metadata_remain_supported(self):
        self.metadata['followup_plan'] = []
        self.metadata['max_followups'] = 0
        tracked_id = self.initial()
        self.assertEqual(get_thread(self.con, tracked_id).status, 'maxed')
        self.assertIsNone(next_due_at(get_thread(self.con, tracked_id)))
        del self.metadata['followup_plan']
        self.metadata['max_followups'] = 2
        self.initial()
        thread = get_thread(self.con, tracked_id)
        self.assertIsNone(saved_plan(thread))
        self.assertEqual(next_due_at(thread), from_iso(thread.initial_sent_at_utc) + timedelta(days=2))

    def test_v3_database_migrates_without_changing_existing_schedule(self):
        del self.metadata['followup_plan']
        tracked_id = self.initial()
        before = get_thread(self.con, tracked_id)
        self.con.execute('ALTER TABLE tracked_threads DROP COLUMN followup_plan')
        self.con.execute('ALTER TABLE tracked_threads DROP COLUMN followup_plan_start_count')
        self.con.execute('PRAGMA user_version = 3')
        self.con.commit()
        init_db(self.con)
        init_db(self.con)
        after = get_thread(self.con, tracked_id)
        self.assertIsNone(saved_plan(after))
        self.assertEqual(next_due_at(before), next_due_at(after))
        self.assertEqual(after.initial_message_id, before.initial_message_id)


class PlanCliTests(TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.cfg = initialize_project(Path(self.directory.name) / 'config.toml')
        self.args = SimpleNamespace(config=str(self.cfg.config_path), body='Original', body_file=None,
            followup_template=None, followup_days=None, max_followups=None, followup_plan=json.dumps(PLAN),
            to='you@example.com', name=None, subject='Test', cc=None, bcc=None, sender_name=None, dry_run=True)

    def test_custom_preview_bypasses_missing_default_template_and_preserves_bodies(self):
        self.args.followup_plan = json.dumps(THREE_PLAN)
        self.cfg.default_template_path.unlink()
        output = io.StringIO()
        with patch('autofollow.gmail_client.GmailClient') as gmail, redirect_stdout(output):
            self.assertEqual(command_send(self.args), 0)
            gmail.assert_not_called()
        for step in THREE_PLAN:
            self.assertIn(step['body'], output.getvalue())
        self.assertIn('Follow-up 3/3 — day 14', output.getvalue())
        self.assertFalse(self.cfg.database_path.exists())

    def test_legacy_cli_and_config_support_three_but_reject_four(self):
        self.args.followup_plan = None
        for configured in (False, True):
            for maximum in (3, 4):
                with self.subTest(configured=configured, maximum=maximum):
                    self.args.max_followups = None if configured else maximum
                    if configured:
                        self.cfg.config_path.write_text(f'[followup]\ndefault_max_followups = {maximum}\n')
                    output = io.StringIO()
                    with patch('autofollow.gmail_client.GmailClient') as gmail, redirect_stdout(output):
                        if maximum == 3:
                            self.assertEqual(command_send(self.args), 0)
                            self.assertIn('Follow-up 3/3', output.getvalue())
                        else:
                            with self.assertRaisesRegex(ValueError, '0 to 3'):
                                command_send(self.args)
                        gmail.assert_not_called()
        self.assertFalse(self.cfg.database_path.exists())

    def test_preview_then_send_persists_exact_plan_for_scheduler(self):
        literal = 'Hello Alex — café {literal}\n\n  Keep spacing & punctuation.\n'
        plan = [{'day': 1, 'body': literal}, {'day': 4, 'body': 'Second custom email'}]
        self.args.followup_plan = json.dumps(plan)
        self.args.cc = 'team@example.com'
        output = io.StringIO()
        gmail = FakeGmail()
        with redirect_stdout(output), patch('autofollow.gmail_client.GmailClient', return_value=gmail):
            self.assertEqual(command_send(self.args), 0)
            self.assertEqual(len(gmail.sent), 0)
            self.args.dry_run = False
            self.assertEqual(command_send(self.args), 0)
        con = sqlite3.connect(self.cfg.database_path)
        con.row_factory = sqlite3.Row
        self.addCleanup(con.close)
        thread = get_thread(con, 1)
        self.assertEqual(saved_plan(thread), plan)
        self.assertEqual(gmail.sent[0].message.get_content().strip(), self.args.body)
        con.execute('UPDATE tracked_threads SET initial_sent_at_utc=? WHERE id=1',
                    (to_iso(utc_now() - timedelta(days=2)),))
        con.commit()
        check_one_thread(cfg=self.cfg, gmail=gmail, con=con, thread=get_thread(con, 1),
                         dry_run=False, sender_name=None)
        self.assertEqual(gmail.sent[1].message.get_content(), literal)
        self.assertEqual(gmail.sent[1].message['Cc'], 'team@example.com')
        self.assertEqual(gmail.sent[1].message['In-Reply-To'], thread.initial_rfc_message_id)

    def test_invalid_plan_or_conflicting_flags_fail_before_authentication(self):
        for changes in ({'followup_plan': '[{"day": 0, "body": "hello"}]'},
                        {'followup_plan': json.dumps([*THREE_PLAN, {'day': 21, 'body': 'Fourth'}])},
                        {'followup_days': 2}, {'max_followups': 1}, {'followup_template': 'unused.txt'}):
            args = SimpleNamespace(**{**vars(self.args), **changes})
            with self.subTest(changes=changes), patch('autofollow.gmail_client.GmailClient') as gmail:
                with self.assertRaises((ValueError, SystemExit)):
                    command_send(args)
                gmail.assert_not_called()
        self.assertFalse(self.cfg.database_path.exists())
