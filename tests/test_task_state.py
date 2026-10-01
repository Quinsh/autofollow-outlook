import tempfile
import unittest
from pathlib import Path
from datetime import timedelta

from autofollow.db import connect, init_db, create_labeled_email, list_labeled_emails, set_task_state
from autofollow.timeutil import utc_now, to_iso


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.con = connect(Path(self.tmp.name) / 'test.db')
        init_db(self.con)
        self.id = create_labeled_email(self.con, gmail_message_id='m1', thread_id='t1',
            subject='Action', sender='a@example.com', received_at_utc=to_iso(utc_now()),
            snippet=None, needs_action=True, important=True, category='other', deadline=None, reason='Reply')

    def tearDown(self):
        self.con.close()
        self.tmp.cleanup()

    def test_done_hidden_and_reopen(self):
        self.assertEqual(len(list_labeled_emails(self.con, actionable=True)), 1)
        set_task_state(self.con, self.id, 'done')
        self.assertEqual(list_labeled_emails(self.con, actionable=True), [])
        self.assertEqual(list_labeled_emails(self.con)[0].task_state, 'done')
        set_task_state(self.con, self.id, 'open')
        self.assertEqual(len(list_labeled_emails(self.con, actionable=True)), 1)

    def test_snooze_expiry(self):
        set_task_state(self.con, self.id, 'snoozed', to_iso(utc_now() + timedelta(days=1)))
        self.assertEqual(list_labeled_emails(self.con, actionable=True), [])
        self.con.execute('UPDATE labeled_emails SET snoozed_until_utc = ?', (to_iso(utc_now() - timedelta(days=1)),))
        self.con.commit()
        self.assertEqual(len(list_labeled_emails(self.con, actionable=True)), 1)

    def test_invalid_updates(self):
        for state, until in [('invalid', None), ('snoozed', None), ('done', '2029-01-01')]:
            with self.assertRaises(ValueError):
                set_task_state(self.con, self.id, state, until)
        with self.assertRaises(ValueError):
            set_task_state(self.con, 999, 'done')

    def test_migration_retains_rows(self):
        self.con.execute("ALTER TABLE labeled_emails DROP COLUMN task_state")
        self.con.execute("ALTER TABLE labeled_emails DROP COLUMN snoozed_until_utc")
        self.con.execute("PRAGMA user_version = 2")
        self.con.commit()
        init_db(self.con)
        self.assertEqual(len(list_labeled_emails(self.con)), 1)
        self.con.execute('PRAGMA user_version = 999')
        with self.assertRaises(ValueError):
            init_db(self.con)


if __name__ == '__main__':
    unittest.main()
