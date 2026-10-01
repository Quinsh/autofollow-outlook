import sqlite3
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from autofollow.db import init_db, create_labeled_email, list_labeled_emails, set_task_state
from autofollow.inbox import classify_pending, init_inbox, queue_reclassification, reclassification_progress
from autofollow.local_llm import CLASSIFICATION_VERSION
from autofollow.timeutil import utc_now, to_iso

NORMAL = dict(needs_action=False,important=False,very_important=False,category='newsletter',deadline=None,reason='Optional campus update')
VERY = dict(needs_action=True,important=True,very_important=True,category='professor',deadline=None,reason='Direct research offer')


class ReclassificationTests(unittest.TestCase):
    def setUp(self):
        self.con=sqlite3.connect(':memory:')
        self.con.row_factory=sqlite3.Row
        init_db(self.con)
        self.addCleanup(self.con.close)
        self.cfg=SimpleNamespace(timezone='UTC')
        self.gmail=Mock()
        self.gmail.get_message_full.return_value={'threadId':'thread', 'internalDate':str(int(utc_now().timestamp()*1000)),
            'snippet':'A campus update', 'payload':{'headers':[{'name':'Subject','value':'Campus update'},
            {'name':'From','value':'campus@example.com'},{'name':'List-Id','value':'campus.example.com'},
            {'name':'To','value':'student@example.com'}]}}

    def add(self, version=0, message_id='mail', **kwargs):
        return create_labeled_email(self.con,gmail_message_id=message_id,thread_id='thread',subject='Campus update',
            sender='campus@example.com',received_at_utc=to_iso(utc_now()),snippet='Summary',needs_action=True,
            important=True,category='school',deadline=None,reason='Old broad classification',classification_version=version,**kwargs)

    def classify(self, result=NORMAL):
        with patch('autofollow.inbox.classify_email_with_ollama',return_value=result) as model:
            outcome=classify_pending(self.con,self.gmail,self.cfg,model='test',host='test',report=lambda _:None)
            return outcome,model

    def test_old_saved_email_upgraded_without_losing_task_or_identity(self):
        email_id=self.add()
        until=to_iso(utc_now()+timedelta(days=3))
        set_task_state(self.con,email_id,'snoozed',until)
        result,model=self.classify()
        self.assertEqual((result.classified,result.pending),(1,0))
        updated=list_labeled_emails(self.con)[0]
        self.assertEqual((updated.id,updated.task_state,updated.snoozed_until_utc),(email_id,'snoozed',until))
        self.assertFalse(updated.important)
        self.assertEqual(updated.classification_version,CLASSIFICATION_VERSION)
        self.assertTrue(model.call_args.kwargs['is_bulk'])
        self.assertIn('student@example.com',model.call_args.kwargs['recipient_context'])
        result,_=self.classify()
        self.assertEqual(result.classified,0)

    def test_explicit_reclassification_retries_failure_preserving_old_result(self):
        email_id=self.add(CLASSIFICATION_VERSION)
        set_task_state(self.con,email_id,'done')
        self.assertEqual(queue_reclassification(self.con),1)
        with patch('autofollow.inbox.classify_email_with_ollama',side_effect=RuntimeError('offline')):
            failed=classify_pending(self.con,self.gmail,self.cfg,model='test',host='test',report=lambda _:None)
        self.assertEqual((failed.errors,failed.pending),(1,1))
        self.assertEqual(list_labeled_emails(self.con)[0].reason,'Old broad classification')
        result,_=self.classify(VERY)
        self.assertEqual(result.pending,0)
        updated=list_labeled_emails(self.con,very_important=True)[0]
        self.assertEqual((updated.id,updated.task_state),(email_id,'done'))
        self.assertTrue(updated.very_important)
        self.assertEqual(len(list_labeled_emails(self.con,important=True)),1)
        self.assertEqual(self.con.execute('SELECT reclassify FROM inbox_queue').fetchone()[0],0)

    def test_v4_migration_preserves_old_flags_and_marks_policy_outdated(self):
        self.add()
        self.con.execute('DROP INDEX idx_labeled_emails_priority')
        self.con.execute('ALTER TABLE labeled_emails DROP COLUMN very_important')
        self.con.execute('ALTER TABLE labeled_emails DROP COLUMN classification_version')
        self.con.execute('PRAGMA user_version = 4')
        self.con.commit()
        init_db(self.con)
        old=list_labeled_emails(self.con)[0]
        self.assertTrue(old.important)
        self.assertFalse(old.very_important)
        self.assertEqual(old.classification_version,0)
        init_inbox(self.con)
        self.assertEqual(self.con.execute('SELECT message_id FROM inbox_queue').fetchone()[0],'mail')

    def test_rejects_inconsistent_priority_at_storage_boundary(self):
        with self.assertRaises(ValueError):
            create_labeled_email(self.con,gmail_message_id='bad',thread_id='thread',subject='Bad',sender='a@b.com',
                received_at_utc=to_iso(utc_now()),snippet=None,needs_action=False,important=False,
                very_important=True,category='other',deadline=None,reason='Contradictory')

    def test_repeated_reclassify_resumes_completed_and_failed_work(self):
        for mid in ('a', 'b', 'c'):
            self.add(CLASSIFICATION_VERSION, message_id=mid)
        self.assertEqual(queue_reclassification(self.con), 3)
        with patch('autofollow.inbox.classify_email_with_ollama', side_effect=[NORMAL, RuntimeError('offline')]):
            failed = classify_pending(self.con,self.gmail,self.cfg,model='test',host='test',report=lambda _:None)
        self.assertEqual((failed.classified,failed.errors,failed.pending), (1,1,2))
        self.assertEqual(self.gmail.get_message_full.call_count, 2)
        self.assertEqual(queue_reclassification(self.con), 2)
        self.assertEqual(reclassification_progress(self.con), dict(total=3,completed=1,pending=2,unavailable=0))
        self.assertEqual(self.con.execute("SELECT reclassify FROM inbox_queue WHERE message_id='a'").fetchone()[0], 0)
        self.assertEqual(self.con.execute("SELECT error FROM inbox_queue WHERE message_id='b'").fetchone()[0], 'offline')
        self.gmail.get_message_full.reset_mock()
        outcome, classifier = self.classify()
        self.assertEqual(outcome.classified, 2)
        self.assertEqual([c.args[0] for c in self.gmail.get_message_full.call_args_list], ['b', 'c'])
        self.assertEqual(reclassification_progress(self.con)['completed'], 3)
        # A deliberate new run is possible once the previous run is finished.
        self.assertEqual(queue_reclassification(self.con), 3)

    def test_progress_survives_connection_restart(self):
        import tempfile
        from pathlib import Path
        for mid in ('a', 'b'):
            self.add(CLASSIFICATION_VERSION, message_id=mid)
        queue_reclassification(self.con)
        with patch('autofollow.inbox.classify_email_with_ollama', return_value=NORMAL):
            classify_pending(self.con,self.gmail,self.cfg,model='test',host='test',limit=1,report=lambda _:None)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'checkpoint.sqlite3'
            with sqlite3.connect(path) as disk:
                self.con.backup(disk)
            disk.close()
            with sqlite3.connect(path) as reopened:
                self.assertEqual(queue_reclassification(reopened), 1)
                self.assertEqual(reclassification_progress(reopened)['completed'], 1)
            reopened.close()

    def test_legacy_run_is_adopted_without_resetting_successes(self):
        for mid in ('a', 'b'):
            self.add(CLASSIFICATION_VERSION, message_id=mid)
        queue_reclassification(self.con)
        with patch('autofollow.inbox.classify_email_with_ollama', return_value=NORMAL):
            classify_pending(self.con,self.gmail,self.cfg,model='test',host='test',limit=1,report=lambda _:None)
        self.con.execute('DROP TABLE inbox_reclassification')
        self.con.commit()
        self.assertEqual(queue_reclassification(self.con), 1)
        self.assertEqual(reclassification_progress(self.con), dict(total=2,completed=1,pending=1,unavailable=0))

    def test_classification_and_progress_commit_atomically(self):
        self.add(CLASSIFICATION_VERSION)
        queue_reclassification(self.con)
        def interrupted_write(*args, **kwargs):
            create_labeled_email(*args, **kwargs)
            raise RuntimeError('Interrupted before queue checkpoint')
        with patch('autofollow.inbox.create_labeled_email', side_effect=interrupted_write):
            outcome, _ = self.classify()
        self.assertEqual((outcome.classified,outcome.pending), (0,1))
        self.assertEqual(list_labeled_emails(self.con)[0].reason, 'Old broad classification')
        self.assertEqual(reclassification_progress(self.con)['completed'], 0)

    def test_unavailable_email_does_not_block_or_restart_run(self):
        from googleapiclient.errors import HttpError
        self.add(CLASSIFICATION_VERSION)
        queue_reclassification(self.con)
        self.gmail.get_message_full.side_effect = HttpError(SimpleNamespace(status=404,reason='gone'), b'{}')
        result, _ = self.classify()
        self.assertEqual((result.errors,result.pending,result.skipped), (0,0,1))
        self.assertEqual(reclassification_progress(self.con)['unavailable'], 1)
        self.assertEqual(queue_reclassification(self.con), 0)
