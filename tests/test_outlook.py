"""Graph transport, account isolation, and crash recovery without live email."""
import base64
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

import requests

from autofollow import sending
from autofollow.app import main
from autofollow.config import initialize_project, load_config, OUTLOOK_CONFIG_TEXT
from autofollow.db import connect, init_db, get_thread
from autofollow.followup import find_external_reply, latest_outgoing_at
from autofollow.mail import build_email
from autofollow.mail_client import create_mail_client, extract_plain_body, MailError
from autofollow.outlook_client import OutlookClient, normalize_message, GRAPH, SCOPES, safe_web_link
from autofollow.inbox import sync_inbox, classify_pending
from autofollow_ui import App, HTML

CLIENT_ID = '11111111-2222-4333-8444-555555555555'

def recipient(address):
    return {'emailAddress': {'address': address}}

def message(**changes):
    result = {'id': 'immutable/1', 'conversationId': 'conversation1', 'internetMessageId': '<actual@outlook>',
              'subject': 'Hello', 'from': recipient('me@outlook.com'), 'sender': recipient('me@outlook.com'),
              'toRecipients': [recipient('you@example.com')], 'ccRecipients': [], 'bccRecipients': [],
              'isDraft': False, 'sentDateTime': '2026-09-01T12:00:00Z',
              'receivedDateTime': '2026-09-01T12:00:01Z', 'bodyPreview': 'Hello',
              'body': {'contentType': 'text', 'content': 'Hello'},
              'webLink': 'https://outlook.live.com/mail/0/id/safe', 'internetMessageHeaders': []}
    result.update(changes)
    return result

class Base(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config.toml'
        self.cfg = replace(initialize_project(self.path, provider='outlook'), microsoft_client_id=CLIENT_ID)
        self.client = OutlookClient(self.cfg)
        self.client._profile = {'id': 'user1', 'mail': 'me@outlook.com'}
        self.client._account_id = 'home:tenant'

    def database(self):
        con = connect(self.cfg.database_path)
        init_db(con)
        self.addCleanup(con.close)
        return con

class ConfigAndUITests(Base):
    def test_new_outlook_config_and_legacy_gmail_default(self):
        cfg = load_config(self.path)
        self.assertEqual(cfg.mail_provider, 'outlook')
        self.assertEqual(cfg.token_path.name, 'outlook-token-cache.json')
        self.assertFalse(cfg.mail_configured)
        self.assertIsInstance(create_mail_client(cfg), OutlookClient)
        self.path.write_text('[email]\nown_addresses=[]\n')
        self.assertEqual(load_config(self.path).mail_provider, 'gmail')

    def test_invalid_microsoft_settings(self):
        for text in ['[email]\nprovider="exchange"', '[microsoft]\nclient_id="secret"',
                     '[microsoft]\ntenant_id="../bad"', '[email]\nsignature=42']:
            self.path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                load_config(self.path)

    def test_init_and_preview_do_not_need_google_or_microsoft_credentials(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--config', str(self.path), 'init', '--provider', 'outlook']), 0)
            self.assertEqual(main(['--config', str(self.path), 'send', '--to', 'you@example.com',
                '--subject', 'Hello', '--body', 'Hello', '--followup-plan', '[]', '--dry-run']), 0)
        self.assertFalse(self.cfg.token_path.exists())
        self.assertFalse(self.cfg.credentials_path.exists())

    def test_doctor_recognizes_microsoft_client_id_without_google_json(self):
        self.path.write_text(OUTLOOK_CONFIG_TEXT.replace('client_id = ""', f'client_id = "{CLIENT_ID}"'))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main(['--config', str(self.path), 'doctor']), 0)
        self.assertIn('Outlook', output.getvalue())

    def test_snapshot_has_provider_signature_and_message_link(self):
        from autofollow.db import create_labeled_email
        self.path.write_text(OUTLOOK_CONFIG_TEXT.replace('signature = ""', 'signature = "Best, Alex"'))
        con = self.database()
        sending.initialize(con)
        create_labeled_email(con, gmail_message_id='remote', thread_id='conversation', subject='Hi',
            sender='them@example.com', received_at_utc='2026-09-01T12:00:00+00:00', snippet='hi',
            needs_action=False, important=False, category='other', deadline=None, reason='test')
        con.execute('INSERT INTO mail_links VALUES(?,?)', ('remote', 'https://outlook.live.com/mail/0/id/safe'))
        con.commit()
        app = App(self.path)
        self.addCleanup(app.close)
        state = app.snapshot()
        self.assertEqual(state['config']['mail_provider'], 'outlook')
        self.assertEqual(state['config']['signature'], 'Best, Alex')
        self.assertEqual(state['emails'][0]['web_link'], 'https://outlook.live.com/mail/0/id/safe')
        self.assertNotIn('Kai (Gunwoo)', HTML)

    def test_browser_mail_link_rejects_unsafe_urls(self):
        import shutil
        if not shutil.which('node'):
            self.skipTest('Node unavailable')
        code = next(line for line in HTML.splitlines() if line.startswith('function gmail('))
        script = "const state={config:{mail_provider:'outlook'}},esc=s=>s;" + code + '''
const assert=require('node:assert/strict');
assert.equal(gmail('x','https://outlook.live.com/mail/id/abc'),'https://outlook.live.com/mail/id/abc');
assert.equal(gmail('x','javascript:alert(1)'),'https://outlook.office.com/mail/');
state.config.mail_provider='gmail';
assert.equal(gmail('a/b'),'https://mail.google.com/mail/u/0/#all/a%2Fb');
'''
        subprocess.run(['node', '-e', script], check=True, capture_output=True)

class ReadTests(Base):
    def test_pagination_and_timestamp_filter(self):
        next_link = GRAPH + '/me/mailFolders/inbox/messages?$skiptoken=opaque'
        with patch.object(self.client, '_request', side_effect=[
            {'value': [{'id': 'one', 'conversationId': 't'}], '@odata.nextLink': next_link},
            {'value': [{'id': 'two', 'conversationId': 't'}]}]) as request:
            pages = list(self.client.iter_inbox_pages_since('0'))
        self.assertEqual([m['id'] for p in pages for m in p], ['one', 'two'])
        self.assertEqual(request.call_args_list[1].args, ('GET', next_link))
        self.assertEqual(request.call_args_list[0].kwargs['params']['$filter'], 'receivedDateTime ge 1970-01-01T00:00:00Z')

    def test_normalization_keeps_headers_html_aliases_and_drafts(self):
        m = message(**{'from': recipient('alias@example.com'), 'isDraft': True,
            'body': {'contentType': 'html', 'content': '<p>Hello</p><script>hidden</script>'},
            'internetMessageHeaders': [{'name': 'List-Id', 'value': 'newsletter'}]})
        result = normalize_message(m, {'me@outlook.com'})
        self.assertEqual(result['labelIds'], ['DRAFT'])
        self.assertEqual(extract_plain_body(result['payload']), 'Hello')
        self.assertIn({'name': 'list-id', 'value': 'newsletter'}, result['payload']['headers'])
        m['isDraft'] = False
        self.assertEqual(normalize_message(m, {'me@outlook.com'})['labelIds'], ['SENT'])

    def test_reply_detection_ignores_drafts_and_self_and_finds_archived_reply(self):
        own = {'me@outlook.com'}
        incoming = message(id='reply', **{'from': recipient('you@example.com'), 'sender': recipient('you@example.com'),
            'receivedDateTime': '2026-09-02T12:00:00Z'})
        draft = message(id='draft', isDraft=True, receivedDateTime='2026-09-03T12:00:00Z')
        with patch.object(self.client, '_request', return_value={'value': [draft, incoming, message()]}) as request:
            thread = self.client.get_thread_metadata('conversation1')
        self.assertEqual(request.call_args.args, ('GET', '/me/messages'))
        reply = find_external_reply(thread, own_addresses=own, after_utc='2026-09-01T12:00:00+00:00', initial_message_id='immutable/1')
        self.assertEqual(reply.gmail_message_id, 'reply')
        self.assertEqual(latest_outgoing_at(thread, own), '2026-09-01T12:00:00+00:00')

    def test_missing_conversation_fails_closed(self):
        with patch.object(self.client, '_request', return_value={'value': []}), self.assertRaises(MailError):
            self.client.get_thread_metadata('missing')

    def test_missing_draft_flag_rejected(self):
        with self.assertRaises(ValueError):
            normalize_message(message(isDraft=None), {'me@outlook.com'})

    def test_sync_classify_persists_web_link_and_graph_404_is_unavailable(self):
        con = self.database()
        sending.bind_account(con, self.client)
        with patch.object(self.client, 'iter_inbox_pages_since', return_value=iter([[{'id': 'one', 'threadId': 't'}, {'id': 'two', 'threadId': 't'}]])):
            sync_inbox(con, self.client, self.cfg)
        classification = dict(needs_action=False, important=False, very_important=False, category='other', deadline=None, reason='test')
        with patch.object(self.client, 'get_message_full', side_effect=[normalize_message(message(id='one'), {'me@outlook.com'}), MailError(404)]), \
             patch('autofollow.inbox.classify_email_with_ollama', return_value=classification):
            result = classify_pending(con, self.client, replace(self.cfg, llm_provider='ollama'), model='test', host='http://localhost', report=lambda _: None)
        self.assertEqual((result.classified, result.pending, result.errors, result.skipped), (1, 0, 0, 1))
        self.assertEqual(con.execute('SELECT message_id FROM mail_links').fetchone()[0], 'one')

class TransportTests(Base):
    def response(self, status=200, data=None, headers=None):
        result = Mock(status_code=status, headers=headers or {}, content=b'json' if data is not None else b'')
        result.json.return_value = data
        return result

    def test_no_bearer_token_sent_to_untrusted_nextlink(self):
        with patch.object(self.client, '_token') as token:
            for url in ['https://evil.example/v1.0/me', 'http://graph.microsoft.com/v1.0/me',
                        'https://graph.microsoft.com@evil.example/v1.0/me', 'https://graph.microsoft.com/beta/me']:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    self.client._request('GET', url)
            token.assert_not_called()

    def test_read_retry_honors_throttle_and_immutable_id(self):
        with patch.object(self.client, '_token', return_value='private'), \
             patch('autofollow.outlook_client.requests.request', side_effect=[self.response(429, {}, {'Retry-After': '2'}), self.response(200, {'id': 'ok'})]) as request, \
             patch('autofollow.outlook_client.time.sleep') as sleep:
            self.assertEqual(self.client._request('GET', '/me')['id'], 'ok')
        sleep.assert_called_once_with(2)
        self.assertIn('IdType="ImmutableId"', request.call_args.kwargs['headers']['Prefer'])
        self.assertFalse(request.call_args.kwargs['allow_redirects'])

    def test_mutations_never_retry_and_errors_do_not_expose_body(self):
        for method in ['POST', 'PATCH']:
            with self.subTest(method=method), patch.object(self.client, '_token', return_value='private'), \
                 patch('autofollow.outlook_client.requests.request', return_value=self.response(503, {'error': {'code': 'Unavailable', 'message': 'PRIVATE MAIL'}})) as request:
                with self.assertRaises(MailError) as error:
                    self.client._request(method, '/me/messages')
                self.assertNotIn('PRIVATE', str(error.exception))
                self.assertEqual(request.call_count, 1)

    def test_timeout_after_send_is_not_retried(self):
        with patch.object(self.client, '_token', return_value='private'), patch('autofollow.outlook_client.requests.request', side_effect=requests.Timeout('PRIVATE')) as request:
            with self.assertRaisesRegex(RuntimeError, 'reconciliation'):
                self.client._request('POST', '/me/messages/id/send')
        self.assertEqual(request.call_count, 1)

    def test_401_reads_refresh_once(self):
        with patch.object(self.client, '_token', return_value='private') as token, \
             patch('autofollow.outlook_client.requests.request', side_effect=[self.response(401, {}), self.response(200, {'ok': True})]):
            self.assertTrue(self.client._request('GET', '/me')['ok'])
        self.assertEqual([c.kwargs['force_refresh'] for c in token.call_args_list], [False, True])

class AuthTests(Base):
    def test_background_operation_never_opens_login(self):
        app = Mock()
        app.get_accounts.return_value = []
        with patch('autofollow.outlook_client.msal.PublicClientApplication', return_value=app):
            with self.assertRaisesRegex(RuntimeError, 'run auth'):
                self.client._token()
        app.acquire_token_interactive.assert_not_called()
        self.assertFalse(self.cfg.token_path.exists())

    def test_explicit_login_persists_private_cache_and_uses_no_secret(self):
        self.client._account_id = None
        app = Mock()
        app.get_accounts.side_effect = [[], [{'home_account_id': 'home', 'realm': 'tenant'}]]
        app.acquire_token_interactive.return_value = {'access_token': 'private'}
        cache = Mock(has_state_changed=True)
        cache.serialize.return_value = '{}'
        with patch('autofollow.outlook_client.msal.SerializableTokenCache', return_value=cache), \
             patch('autofollow.outlook_client.msal.PublicClientApplication', return_value=app) as factory:
            self.assertEqual(self.client._token(interactive=True), 'private')
        self.assertEqual(self.cfg.token_path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('client_credential', factory.call_args.kwargs)
        self.assertEqual(app.acquire_token_interactive.call_args.kwargs['scopes'], SCOPES)
        self.assertNotIn('private', self.cfg.token_path.read_text())

    def test_changed_client_id_requires_explicit_reauth(self):
        self.cfg.token_path.write_text(json.dumps({'client_id': 'wrong', 'tenant_id': 'common', 'cache': '{}'}))
        with self.assertRaisesRegex(RuntimeError, 'reauth'):
            self.client._token()

class JournalTests(Base):
    def setUp(self):
        super().setUp()
        self.con = self.database()
        sending.bind_account(self.con, self.client)
        self.built = build_email(to='you@example.com', subject='Hello', body='Hello', sender_email='me@outlook.com')
        self.metadata = dict(recipient='you@example.com', subject='Hello', followup_delay_days=1,
                             max_followups=1, followup_template='Hello again')

    def test_send_checkpoints_and_uses_actual_remote_ids_and_timestamp(self):
        stages = []
        def graph(method, path, **kwargs):
            if method == 'POST' and path == '/me/messages':
                self.assertEqual(self.con.execute('SELECT state FROM send_jobs').fetchone()[0], 'uncertain')
                return message(isDraft=True)
            if method == 'POST':
                row = self.con.execute('SELECT remote_draft_id,submission_stage FROM send_jobs').fetchone()
                stages.append(tuple(row))
                return {}
            row = self.con.execute('SELECT submission_stage FROM send_jobs').fetchone()[0]
            return message(isDraft=row != 'submitting')
        with patch.object(self.client, '_request', side_effect=graph):
            tracked_id = sending.deliver(self.con, self.client, self.built, metadata=self.metadata)
        self.assertEqual(stages, [('immutable/1', 'submitting')])
        tracked = get_thread(self.con, tracked_id)
        self.assertEqual(tracked.initial_rfc_message_id, '<actual@outlook>')
        self.assertEqual(tracked.initial_sent_at_utc, '2026-09-01T12:00:00+00:00')
        self.assertEqual(tracked.thread_id, 'conversation1')
        self.assertEqual(self.con.execute('SELECT web_link FROM mail_links').fetchone()[0], message()['webLink'])

    def uncertain(self, responses):
        with patch.object(self.client, '_request', side_effect=responses), patch('autofollow.outlook_client.time.sleep'):
            with self.assertRaisesRegex(RuntimeError, 'Delivery uncertain'):
                sending.deliver(self.con, self.client, self.built, metadata=self.metadata)
        return self.con.execute('SELECT * FROM send_jobs').fetchone()

    def test_lost_send_response_recovers_once_without_resending(self):
        self.uncertain([message(isDraft=True), message(isDraft=True), RuntimeError('timeout')])
        with patch.object(self.client, '_request', return_value=message()) as request:
            self.assertEqual(sending.reconcile(self.con, self.client), [])
            self.assertEqual(sending.reconcile(self.con, self.client), [])
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], 'GET')
        self.assertEqual(self.con.execute('SELECT count(*) FROM events').fetchone()[0], 1)

    def test_202_delayed_copy_blocks_new_send_and_reconciles_later(self):
        job = self.uncertain([message(isDraft=True), message(isDraft=True), {}, MailError(404), MailError(404), MailError(404), MailError(404)])
        self.assertEqual(job['submission_stage'], 'submitting')
        with patch.object(self.client, '_request') as request:
            with self.assertRaisesRegex(RuntimeError, 'initial delivery is still uncertain'):
                sending.deliver(self.con, self.client, self.built, metadata=self.metadata)
        request.assert_not_called()
        with patch.object(self.client, '_request', return_value=message()):
            self.assertEqual(sending.reconcile(self.con, self.client), [])

    def test_lost_create_response_does_not_send_or_guess_a_sent_message(self):
        job = self.uncertain([RuntimeError('lost create response')])
        self.assertIsNone(job['remote_draft_id'])
        with patch.object(self.client, '_request') as request:
            self.assertEqual(sending.reconcile(self.con, self.client), [job['operation_id']])
        request.assert_not_called()

    def test_wrong_draft_recipients_never_send(self):
        job = self.uncertain([message(isDraft=True), message(isDraft=True, toRecipients=[recipient('wrong@example.com')])])
        self.assertEqual(job['submission_stage'], 'draft_created')

    def test_wrong_sender_sent_copy_is_not_finalized(self):
        self.uncertain([message(isDraft=True), message(isDraft=True), RuntimeError('timeout')])
        wrong = message(**{'from': recipient('wrong@example.com'), 'sender': recipient('wrong@example.com')})
        with patch.object(self.client, '_request', return_value=wrong), self.assertRaisesRegex(RuntimeError, 'identity'):
            sending.reconcile(self.con, self.client)
        self.assertEqual(self.con.execute('SELECT count(*) FROM events').fetchone()[0], 0)

    def test_followup_reply_resets_to_cc_and_removes_bcc(self):
        built = build_email(to='you@example.com', cc='cc@example.com', subject='Hello', body='Following up', reply=True)
        draft = message(id='followup', isDraft=True, toRecipients=[recipient('me@outlook.com')])
        verified = message(id='followup', isDraft=True, ccRecipients=[recipient('cc@example.com')])
        with patch.object(self.client, '_request', side_effect=[draft, {}, verified, {}, message(id='followup')]) as request:
            self.client.submit_message(built, thread_id='conversation1', anchor_id='original/id', checkpoint=lambda *args: None)
        self.assertEqual(request.call_args_list[0].args, ('POST', '/me/messages/original%2Fid/createReply'))
        payload = request.call_args_list[1].kwargs['json_body']
        self.assertEqual(payload['toRecipients'][0]['emailAddress']['address'], 'you@example.com')
        self.assertEqual(payload['ccRecipients'][0]['emailAddress']['address'], 'cc@example.com')
        self.assertEqual(payload['bccRecipients'], [])

    def test_wrong_reply_conversation_never_sends(self):
        with patch.object(self.client, '_request', return_value=message(isDraft=True, conversationId='wrong')) as request:
            with self.assertRaisesRegex(RuntimeError, 'conversation'):
                self.client.submit_message(self.built, thread_id='conversation1', anchor_id='original', checkpoint=lambda *args: None)
        self.assertEqual(request.call_count, 1)

    def test_different_account_and_provider_cannot_reuse_database(self):
        self.client._account_id = 'other:tenant'
        with self.assertRaisesRegex(RuntimeError, 'different Outlook account'):
            sending.bind_account(self.con, self.client)
        gmail = SimpleNamespace(provider='gmail', get_profile_email=lambda: 'me@outlook.com')
        with self.assertRaisesRegex(RuntimeError, 'different mail provider'):
            sending.bind_account(self.con, gmail)

    def test_dry_run_identity_check_does_not_write(self):
        before = list(self.con.iterdump())
        sending.bind_account(self.con, self.client, persist=False)
        self.assertEqual(list(self.con.iterdump()), before)


class EndToEndTests(Base):
    def test_cli_outreach_followup_reply_and_statistics_with_graph(self):
        from urllib.parse import unquote
        from autofollow.statistics import reply_statistics, set_statistics_excluded
        from autofollow.timeutil import utc_now
        from datetime import timedelta
        baseline = utc_now() - timedelta(days=10)
        messages = {}
        send_count = 0
        def graph(method, path, **kwargs):
            nonlocal send_count
            if method == 'POST' and (path == '/me/messages' or path.endswith('/createReply')):
                identifier = 'draft/' + str(len(messages) + 1)
                data = message(id=identifier, isDraft=True)
                data.update(kwargs.get('json_body') or {})
                messages[identifier] = data
                return dict(data)
            if method == 'GET' and path == '/me/messages':
                return {'value': list(messages.values())}
            resource = unquote(path.removeprefix('/me/messages/').removesuffix('/send'))
            if method == 'GET':
                return dict(messages[resource])
            if method == 'PATCH':
                messages[resource].update(kwargs['json_body'])
                return dict(messages[resource])
            if method == 'POST' and path.endswith('/send'):
                send_count += 1
                timestamp = (baseline + timedelta(days=send_count - 1)).isoformat()
                messages[resource].update(isDraft=False, sentDateTime=timestamp, receivedDateTime=timestamp,
                                           internetMessageId=f'<actual{send_count}@outlook>')
                return {}
            raise AssertionError((method, path))
        output = io.StringIO()
        with patch('autofollow.mail_client.create_mail_client', return_value=self.client), \
             patch.object(self.client, '_request', side_effect=graph), redirect_stdout(output):
            self.assertEqual(main(['--config', str(self.path), 'send', '--to', 'you@example.com',
                '--subject', 'Hello', '--body', 'Hello', '--sender-name', 'Alex',
                '--followup-plan', '[{"day":1,"body":"Hello again"}]']), 0)
            self.assertEqual(main(['--config', str(self.path), 'check']), 0)
            self.assertEqual(send_count, 2)
            messages['incoming'] = message(id='incoming', **{'from': recipient('you@example.com'),
                'sender': recipient('you@example.com'), 'receivedDateTime': (baseline + timedelta(days=2)).isoformat()})
            self.assertEqual(main(['--config', str(self.path), 'check']), 0)
            self.assertEqual(send_count, 2)
        con = self.database()
        thread = get_thread(con, 1)
        self.assertEqual(thread.status, 'replied')
        self.assertEqual(thread.followup_count, 1)
        stats = reply_statistics(con)
        self.assertEqual(stats['totals']['sent'], 1)
        self.assertEqual(stats['totals']['replied'], 1)
        set_statistics_excluded(con, tracked_id=1, initial_message_id=thread.initial_message_id, excluded=True)
        self.assertEqual(reply_statistics(con)['totals']['sent'], 0)
        set_statistics_excluded(con, tracked_id=1, initial_message_id=thread.initial_message_id, excluded=False)
        self.assertEqual(reply_statistics(con)['totals']['replied'], 1)

    def test_provider_factory_rejects_gmail_in_outlook_bound_database(self):
        con = self.database()
        sending.bind_account(con, self.client)
        with self.assertRaisesRegex(RuntimeError, 'different mail provider'):
            create_mail_client(replace(self.cfg, mail_provider='gmail'))

    def test_doctor_checks_actual_mailbox_without_opening_browser(self):
        self.path.write_text(OUTLOOK_CONFIG_TEXT.replace('client_id = ""', f'client_id = "{CLIENT_ID}"'))
        with patch('autofollow.mail_client.create_mail_client', return_value=self.client), \
             patch.object(self.client, '_request', return_value={'id': 'inbox'}) as request, \
             patch.object(self.client, 'ensure_authenticated') as auth, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--config', str(self.path), 'doctor', '--auth']), 0)
        auth.assert_not_called()
        request.assert_called_once_with('GET', '/me/mailFolders/inbox', params={'$select': 'id'})

    def test_outlook_message_id_longer_than_gmail_limit_can_be_excluded(self):
        from autofollow.statistics import set_statistics_excluded
        con = self.database()
        sending.bind_account(con, self.client)
        built = build_email(to='you@example.com', subject='Hello', body='Hello', sender_email='me@outlook.com')
        from autofollow.mail_client import SentMessage
        with patch.object(self.client, 'submit_message', return_value=SentMessage('x' * 300, 'conversation', '<actual@outlook>')):
            tracked_id = sending.deliver(con, self.client, built, metadata=dict(recipient='you@example.com',
                subject='Hello', followup_delay_days=1, max_followups=0, followup_template=''))
        set_statistics_excluded(con, tracked_id=tracked_id, initial_message_id='x' * 300, excluded=True)


if __name__ == '__main__':
    unittest.main()
