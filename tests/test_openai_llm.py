import io
import json
import os
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from autofollow.config import load_config
from autofollow.db import init_db
from autofollow.inbox import sync_inbox, classify_pending
from autofollow.openai_llm import (
    OpenAIError, classify_email_with_openai, selected_provider, selected_model,
)


RESULT = dict(needs_action=False, important=False, very_important=False,
              category="school", deadline=None, reason="Routine announcement",
              priority_basis="none", important_evidence=None, direct_contact=False,
              personal_evidence=None, bulk_message=True)


def response(result=RESULT, **extra):
    return dict(status="completed", output=[dict(type="message", content=[
        dict(type="output_text", text=json.dumps(result))])], **extra)


class OpenAIProviderTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        sleeper = patch("autofollow.openai_llm.time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        self.addCleanup(self.env.stop)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.toml"

    def config(self, text=""):
        self.path.write_text(text)
        return load_config(self.path)

    def mock_response(self, data):
        mock = Mock()
        mock.__enter__ = Mock(return_value=mock)
        mock.__exit__ = Mock(return_value=False)
        mock.read.return_value = json.dumps(data).encode()
        return mock

    def test_auto_and_forced_provider_selection(self):
        cfg = self.config()
        self.assertEqual(selected_provider(cfg), "ollama")
        self.assertEqual(selected_model(cfg), "qwen3.5:9b")
        os.environ["OPENAI_API_KEY"] = "   "
        self.assertEqual(selected_provider(cfg), "ollama")
        os.environ["OPENAI_API_KEY"] = "sk-test-key"
        self.assertEqual(selected_provider(cfg), "openai")
        self.assertEqual(selected_model(cfg), "gpt-4.1-mini")
        cfg = self.config('[llm]\nprovider="ollama"\nmodel="custom-local"')
        self.assertEqual(selected_provider(cfg), "ollama")
        self.assertEqual(selected_model(cfg), "custom-local")
        cfg = self.config('[llm]\nprovider="openai"\nopenai_model="custom-cloud"')
        os.environ.pop("OPENAI_API_KEY")
        self.assertEqual(selected_provider(cfg), "openai")
        self.assertEqual(selected_model(cfg), "custom-cloud")

    def test_invalid_configuration(self):
        for value in ('provider="other"', 'provider=true', 'openai_model=""'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.config('[llm]\n' + value)

    def test_dashboard_reports_active_provider_without_key(self):
        from autofollow_ui import App
        self.config()
        app = App(self.path)
        self.addCleanup(app.close)
        self.assertEqual(app.snapshot()['config']['provider'], 'ollama')
        os.environ['OPENAI_API_KEY'] = 'sk-secret-value'
        state = app.snapshot()
        self.assertEqual(state['config']['provider'], 'openai')
        self.assertEqual(state['config']['model'], 'gpt-4.1-mini')
        self.assertNotIn('sk-secret-value', json.dumps(state))

    def test_missing_key_fails_before_network(self):
        with patch('autofollow.openai_llm.urllib.request.urlopen') as fetch:
            with self.assertRaisesRegex(OpenAIError, 'OPENAI_API_KEY'):
                classify_email_with_openai('Test', 'sender', 'body')
            fetch.assert_not_called()

    def test_pasted_command_or_key_with_whitespace_fails_before_network(self):
        for value in ('export OPENAI_API_KEY', 'sk-test key', 'OpenAI API key: sk-test'):
            os.environ['OPENAI_API_KEY'] = value
            with self.subTest(value=value), patch('autofollow.openai_llm.urllib.request.urlopen') as fetch:
                with self.assertRaisesRegex(OpenAIError, 'OPENAI_API_KEY is malformed'):
                    classify_email_with_openai('Test', 'sender', 'body')
                fetch.assert_not_called()

    def test_structured_request_and_validated_response(self):
        os.environ['OPENAI_API_KEY'] = 'sk-test-key'
        with patch('autofollow.openai_llm.urllib.request.urlopen', return_value=self.mock_response(response())) as fetch:
            result = classify_email_with_openai('Test', 'sender', 'body', received_at='2026-09-15', timezone='America/New_York')
        self.assertEqual(result['category'], 'school')
        self.assertNotIn('bulk_message', result)
        req = fetch.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, 'https://api.openai.com/v1/responses')
        self.assertEqual(req.get_header('Authorization'), 'Bearer sk-test-key')
        self.assertFalse(payload['store'])
        self.assertTrue(payload['text']['format']['strict'])
        self.assertEqual(set(payload['text']['format']['schema']['required']), set(RESULT))
        self.assertIn('timezone=America/New_York', payload['input'])
        self.assertNotIn('sk-test-key', payload['input'])

    def test_same_evidence_policy_applies_to_cloud(self):
        os.environ['OPENAI_API_KEY'] = 'sk-test-key'
        inflated = dict(RESULT, important=True, very_important=True)
        with patch('autofollow.openai_llm.urllib.request.urlopen', return_value=self.mock_response(response(inflated))):
            result = classify_email_with_openai('Test', 'sender', 'body')
        self.assertFalse(result['important'])
        self.assertFalse(result['very_important'])

    def test_incomplete_refused_and_malformed_outputs_remain_errors(self):
        os.environ['OPENAI_API_KEY'] = 'sk-test-key'
        bad = [[], {'status': 'incomplete'}, {'status': 'completed', 'output': []},
               {'status': 'completed', 'output': [None]},
               {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal'}]}]},
               response(dict(RESULT, needs_action='yes'))]
        for data in bad:
            with self.subTest(data=data), patch('autofollow.openai_llm.urllib.request.urlopen', return_value=self.mock_response(data)):
                with self.assertRaises(OpenAIError):
                    classify_email_with_openai('Test', 'sender', 'body')

    def test_transport_errors_do_not_expose_secrets(self):
        os.environ['OPENAI_API_KEY'] = 'sk-secret-value'
        for code in (401, 403, 429, 500):
            exc = HTTPError('https://api.openai.com/v1/responses', code, 'sk-secret-value', {}, io.BytesIO(b'sk-secret-value'))
            with patch('autofollow.openai_llm.urllib.request.urlopen', side_effect=exc):
                with self.assertRaises(OpenAIError) as caught:
                    classify_email_with_openai('Test', 'sender', 'body')
                self.assertIn(str(code), str(caught.exception))
                self.assertNotIn('sk-secret-value', str(caught.exception))
        with patch('autofollow.openai_llm.urllib.request.urlopen', side_effect=URLError('sk-secret-value')):
            with self.assertRaises(OpenAIError) as caught:
                classify_email_with_openai('Test', 'sender', 'body')
            self.assertNotIn('sk-secret-value', str(caught.exception))

    def test_queue_uses_openai_and_retries_without_local_fallback(self):
        os.environ['OPENAI_API_KEY'] = 'sk-test-key'
        cfg = self.config()
        con = sqlite3.connect(':memory:')
        self.addCleanup(con.close)
        con.row_factory = sqlite3.Row
        init_db(con)
        gmail = Mock()
        gmail.iter_inbox_pages_since.return_value = [[{'id': 'new'}]]
        gmail.get_message_full.return_value = {'internalDate': '1789516800000'}
        sync_inbox(con, gmail, cfg)
        with patch('autofollow.inbox.classify_email_with_ollama') as local, patch(
            'autofollow.openai_llm.urllib.request.urlopen', side_effect=URLError('offline')
        ):
            batch = classify_pending(con, gmail, cfg, model=None, host=cfg.llm_host, report=lambda _: None)
            self.assertEqual((batch.errors, batch.pending), (1, 1))
            local.assert_not_called()
        with patch('autofollow.openai_llm.urllib.request.urlopen', return_value=self.mock_response(response())):
            batch = classify_pending(con, gmail, cfg, model=None, host=cfg.llm_host, report=lambda _: None)
            self.assertEqual((batch.classified, batch.pending), (1, 0))
        self.assertIsNone(con.execute('SELECT error FROM inbox_queue').fetchone()[0])

    def test_rate_limit_retries_same_request_then_succeeds(self):
        os.environ['OPENAI_API_KEY'] = 'sk-test-key'
        errors = [HTTPError('https://api.openai.com', 429, '', {'retry-after': '10'},
                            io.BytesIO(b'{"error":{"code":"rate_limit_exceeded"}}')) for _ in range(2)]
        logs = []
        with patch('autofollow.openai_llm.urllib.request.urlopen',
                   side_effect=[*errors, self.mock_response(response())]) as fetch:
            result = classify_email_with_openai('Test', 'sender', 'body', report=logs.append)
        self.assertEqual(result['category'], 'school')
        self.assertEqual(fetch.call_count, 3)
        self.assertTrue(all(call.args[0] is fetch.call_args.args[0] for call in fetch.call_args_list))
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [10, 10])
        self.assertEqual(len(logs), 2)
        self.assertIn('same email', logs[0])

    def test_rate_limit_exhaustion_is_bounded(self):
        os.environ['OPENAI_API_KEY'] = 'sk-test-key'
        errors = [HTTPError('https://api.openai.com', 429, '', {}, io.BytesIO(b'{}')) for _ in range(5)]
        with patch('autofollow.openai_llm.urllib.request.urlopen', side_effect=errors) as fetch:
            with self.assertRaisesRegex(OpenAIError, 'retries exhausted'):
                classify_email_with_openai('Test', 'sender', 'body')
        self.assertEqual(fetch.call_count, 5)
        self.assertEqual(self.sleep.call_count, 4)
        delays = [c.args[0] for c in self.sleep.call_args_list]
        self.assertEqual(delays, sorted(delays))

    def test_quota_and_long_cooldown_pause_without_immediate_retries(self):
        os.environ['OPENAI_API_KEY'] = 'sk-test-key'
        cases = [({'error': {'code': 'insufficient_quota', 'message': 'private'}}, {}, 'quota exhausted'),
                 ({'error': {'code': 'rate_limit_exceeded'}}, {'retry-after': '120'}, '120s cooldown')]
        for body, headers, message in cases:
            with self.subTest(message=message):
                error = HTTPError('https://api.openai.com', 429, '', headers, io.BytesIO(json.dumps(body).encode()))
                with patch('autofollow.openai_llm.urllib.request.urlopen', side_effect=error) as fetch:
                    with self.assertRaisesRegex(OpenAIError, message):
                        classify_email_with_openai('Test', 'sender', 'body')
                self.assertEqual(fetch.call_count, 1)
        self.sleep.assert_not_called()


if __name__ == '__main__':
    unittest.main()
