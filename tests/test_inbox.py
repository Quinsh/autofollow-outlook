import sqlite3
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from autofollow.db import init_db
from autofollow.gmail_client import GmailClient
from autofollow.inbox import sync_inbox, classify_pending
from autofollow.local_llm import validate_classification, OllamaError, generate_daily_digest_with_ollama

VALID = dict(needs_action=False, important=True, very_important=False, category='finance', deadline=None, reason='Payment notice')


class InboxTests(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(':memory:')
        self.con.row_factory = sqlite3.Row
        init_db(self.con)
        self.cfg = SimpleNamespace(initial_lookback_days=30, timezone='America/New_York')

    def tearDown(self):
        self.con.close()

    def test_strict_schema(self):
        for invalid in ([], dict(VALID, needs_action='false'), dict(VALID, important=1),
                        dict(VALID, category=[]), dict(VALID, deadline=123), dict(VALID, reason=None)):
            with self.subTest(invalid=invalid), self.assertRaises(OllamaError):
                validate_classification(invalid)
        self.assertEqual(validate_classification(VALID), VALID)

    def test_gmail_paginates(self):
        gmail = GmailClient(self.cfg)
        messages = Mock()
        gmail._service = Mock()
        gmail._service.users.return_value.messages.return_value = messages
        messages.list.return_value.execute.side_effect = [dict(messages=[{'id':'1'}], nextPageToken='next'), dict(messages=[{'id':'2'}])]
        self.assertEqual([m['id'] for m in gmail.list_inbox_messages_since('123')], ['1', '2'])
        self.assertEqual(messages.list.call_args.kwargs['pageToken'], 'next')

    def test_persistent_backlog_and_fetch_error(self):
        gmail = Mock()
        gmail.iter_inbox_pages_since.return_value = [[{'id': str(i)} for i in range(120)]]
        now = datetime(2026, 9, 15, tzinfo=timezone.utc)
        self.assertEqual(sync_inbox(self.con, gmail, self.cfg, now=now), 120)
        gmail.get_message_full.side_effect = lambda mid: {'internalDate': '1789516800000', 'threadId': mid, 'snippet': 'hello'}
        with patch('autofollow.inbox.classify_email_with_ollama', return_value=VALID) as classifier:
            first = classify_pending(self.con, gmail, self.cfg, model='test', host='test', report=lambda _: None)
            self.assertEqual((first.classified, first.pending), (50,70))
            self.assertEqual(classifier.call_args.kwargs['timezone'], 'America/New_York')
            gmail.iter_inbox_pages_since.return_value = [[]]
            sync_inbox(self.con, gmail, self.cfg, now=now + timedelta(days=8))
            second = classify_pending(self.con, gmail, self.cfg, model='test', host='test', report=lambda _: None)
            self.assertEqual((second.classified, second.pending), (50,20))
            gmail.get_message_full.side_effect = RuntimeError('fetch failed')
            failed = classify_pending(self.con, gmail, self.cfg, model='test', host='test', report=lambda _: None)
            self.assertEqual((failed.errors, failed.pending), (1,20))
            gmail.get_message_full.side_effect = None
            gmail.get_message_full.return_value = {'internalDate': '1789516800000'}
            retried = classify_pending(self.con, gmail, self.cfg, model='test', host='test', report=lambda _: None)
            self.assertEqual((retried.classified, retried.pending), (20,0))

    def test_failed_page_does_not_advance_cursor(self):
        gmail = Mock()
        def pages():
            yield [{'id':'saved'}]
            raise RuntimeError('page failure')
        gmail.iter_inbox_pages_since.return_value = pages()
        with self.assertRaises(RuntimeError):
            sync_inbox(self.con, gmail, self.cfg)
        self.assertEqual(self.con.execute('SELECT count(*) FROM inbox_queue').fetchone()[0], 1)
        self.assertEqual(self.con.execute('SELECT count(*) FROM inbox_sync').fetchone()[0], 0)

    def test_digest_uses_saved_flags_and_task_state(self):
        email = dict(VALID, subject='Bill', sender='noreply@example.com', needs_action=True, task_state='done')
        digest = generate_daily_digest_with_ollama([email])
        self.assertIn('Important emails: 1', digest)
        self.assertIn('None today.', digest)
        self.assertIn('Bill', digest)

    def test_calendar_day_respects_timezone_and_dst(self):
        from autofollow.inbox import daily_classifications
        from autofollow.db import create_labeled_email
        # March 8, 2026 is a 23-hour day in New York, 05:00Z to 04:00Z.
        times = ['2026-03-08T04:59:59+00:00', '2026-03-08T05:00:00+00:00',
                 '2026-03-09T03:59:59+00:00', '2026-03-09T04:00:00+00:00']
        for i, received in enumerate(times):
            create_labeled_email(self.con, gmail_message_id=str(i), thread_id=str(i), subject='x',
                sender='x', received_at_utc=received, snippet='', **VALID)
        rows = daily_classifications(self.con, 'America/New_York', now=datetime(2026,3,8,18,tzinfo=timezone.utc))
        self.assertEqual({row['gmail_message_id'] for row in rows}, {'1', '2'})

    def test_nested_mime_plain_preferred_then_html(self):
        import base64
        from autofollow.gmail_client import extract_plain_body
        def part(mime, text, **extra):
            return dict(mimeType=mime, body={'data':base64.urlsafe_b64encode(text.encode()).decode()}, **extra)
        html = part('text/html', '<style>hidden</style><p>Pay &amp; submit</p><script>bad()</script><div>Friday</div>')
        plain = part('text/plain', 'Preferred plaintext')
        payload = {'parts':[html, {'parts':[plain]}]}
        self.assertEqual(extract_plain_body(payload), 'Preferred plaintext')
        self.assertEqual(extract_plain_body({'parts':[html]}), 'Pay & submit\nFriday')
        self.assertEqual(extract_plain_body({'parts':[part('text/plain', 'Attachment', filename='x.txt'),html]}), 'Pay & submit\nFriday')

    def test_label_and_digest_commands_resolve_gmail_and_config_defaults(self):
        import argparse
        import contextlib
        import io
        import tempfile
        from pathlib import Path
        from autofollow.app import command_label, command_digest
        from autofollow.config import Config
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            cfg = Config(config_path=path/'config.toml', credentials_path=path/'credentials.json',
                token_path=path/'token.json', database_path=path/'test.sqlite3', timezone='America/New_York')
            args = argparse.Namespace(config=str(cfg.config_path), model=None, host=None)
            with patch('autofollow.app.load_config', return_value=cfg), \
                 patch('autofollow.gmail_client.GmailClient') as factory, \
                 patch('autofollow.inbox.classify_email_with_ollama', return_value=VALID) as classifier, \
                 contextlib.redirect_stdout(io.StringIO()):
                factory.return_value.iter_inbox_pages_since.return_value = [[{'id':'1'}]]
                factory.return_value.get_message_full.return_value = {'internalDate': str(int(datetime.now(timezone.utc).timestamp()*1000))}
                self.assertEqual(command_label(args), 0)
                self.assertEqual(classifier.call_args.kwargs['model'], cfg.llm_model)
                self.assertEqual(classifier.call_args.kwargs['host'], cfg.llm_host)
                self.assertEqual(command_digest(args), 0)
                self.assertEqual(classifier.call_count, 1)

    def test_404_is_terminal_but_other_errors_retry(self):
        from googleapiclient.errors import HttpError
        gmail = Mock()
        gmail.iter_inbox_pages_since.return_value = [[{'id':'deleted'}, {'id':'retry'}]]
        sync_inbox(self.con, gmail, self.cfg)
        def fetch(mid):
            raise HttpError(SimpleNamespace(status=404 if mid == 'deleted' else 503, reason='test'), b'{}')
        gmail.get_message_full.side_effect = fetch
        first = classify_pending(self.con, gmail, self.cfg, model='test', host='test', report=lambda _: None)
        self.assertEqual((first.errors, first.pending, first.skipped), (1, 1, 1))
        gmail.get_message_full.reset_mock()
        second = classify_pending(self.con, gmail, self.cfg, model='test', host='test', report=lambda _: None)
        gmail.get_message_full.assert_called_once_with('retry')
        self.assertEqual((second.pending, second.skipped), (1,1))

    def test_rescan_uses_lookback_and_preserves_saved_queue(self):
        from autofollow.inbox import init_inbox
        from autofollow.app import build_parser
        self.assertTrue(build_parser().parse_args(['label', '--rescan']).rescan)
        # Exercise upgrading the queue schema created by the first release.
        self.con.execute('CREATE TABLE inbox_queue(message_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, discovered_at TEXT NOT NULL, attempted_at TEXT, error TEXT)')
        self.con.execute("INSERT INTO inbox_queue VALUES('saved','thread','2026-01-01',NULL,NULL)")
        self.con.commit()
        init_inbox(self.con)
        self.assertEqual(self.con.execute("SELECT status FROM inbox_queue WHERE message_id='saved'").fetchone()[0], 'pending')
        gmail = Mock()
        gmail.iter_inbox_pages_since.return_value = [[]]
        now = datetime(2026,9,15,tzinfo=timezone.utc)
        sync_inbox(self.con, gmail, self.cfg, now=now)
        later = now + timedelta(days=10)
        sync_inbox(self.con, gmail, self.cfg, now=later)
        self.assertEqual(gmail.iter_inbox_pages_since.call_args.args[0], str(int((now-timedelta(days=1)).timestamp())))
        sync_inbox(self.con, gmail, self.cfg, now=later, full_rescan=True)
        self.assertEqual(gmail.iter_inbox_pages_since.call_args.args[0], str(int((later-timedelta(days=30)).timestamp())))
        self.assertEqual(self.con.execute('SELECT count(*) FROM inbox_queue').fetchone()[0], 1)
