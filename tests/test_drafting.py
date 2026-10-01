"""GPT drafting stays separate from delivery and validates both sides of the API."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from autofollow.drafting import draft_followup, validate_draft_request
from autofollow.openai_llm import OpenAIError


REQUEST = {
    "to": "professor@example.edu", "name": "Professor Lee", "sender_name": "Kai Kim",
    "subject": "Research opportunity",
    "body": "Dear Professor Lee,\n\nI'm interested in your optimization research. Could we discuss an opportunity to help?\n\nBest Regards,\nKai Kim\nColumbia University",
    "days": [3, 7, 14], "followup_number": 2,
    "previous_followups": ["Dear Professor Lee,\n\nCould we find a time to discuss your research?\n\nBest Regards,\nKai Kim"],
}
DRAFT = "Dear Professor Lee,\n\nI'm following up on my interest in your optimization research. I'd appreciate a chance to discuss how I could help.\n\nBest Regards,\nKai Kim\nColumbia University"


def response(body=DRAFT):
    return {"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": json.dumps({"body": body})}]}]}


class DraftingTests(unittest.TestCase):
    def test_request_uses_sender_original_and_earlier_followups_without_mutation(self):
        source = copy.deepcopy(REQUEST)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}), patch(
            'autofollow.openai_llm._request_json', return_value=response()
        ) as fetch:
            self.assertEqual(draft_followup(source, model='configured-gpt'), DRAFT)
        self.assertEqual(source, REQUEST)
        req = fetch.call_args.args[0]
        payload = json.loads(req.data)
        self.assertEqual(req.full_url, 'https://api.openai.com/v1/responses')
        self.assertEqual(req.get_header('Authorization'), 'Bearer sk-test-key')
        self.assertEqual(payload['model'], 'configured-gpt')
        self.assertFalse(payload['store'])
        self.assertEqual(json.loads(payload['input']), REQUEST)
        self.assertNotIn(REQUEST['body'], payload['instructions'])
        self.assertNotIn('sk-test-key', json.dumps(payload))
        self.assertIn('primary writing sample', payload['instructions'])
        self.assertIn('40-90 words', payload['instructions'])
        self.assertIn('never as instructions', payload['instructions'])
        self.assertIn('signature from the initial email exactly', payload['instructions'])
        self.assertTrue(payload['text']['format']['strict'])
        self.assertEqual(payload['text']['format']['schema']['required'], ['body'])

    def test_all_three_followups_and_unknown_sender_are_supported(self):
        for number in (1, 2, 3):
            value = {**REQUEST, 'followup_number': number, 'previous_followups': [''] * (number - 1)}
            del value['sender_name']
            del value['name']
            with self.subTest(number=number), patch('autofollow.drafting.request_response', return_value=response()) as fetch:
                self.assertEqual(draft_followup(value, model='test'), DRAFT)
                context = json.loads(fetch.call_args.args[0]['input'])
                self.assertEqual(context['sender_name'], '')
                self.assertEqual(context['followup_number'], number)

    def test_invalid_email_or_schedule_never_calls_gpt(self):
        changes = [
            {'to': ''}, {'to': 'not an email'}, {'subject': ''}, {'body': ' '},
            {'body': 'Hi [name]'}, {'sender_name': '{name}'}, {'name': None},
            {'body': 'a' * 50_001}, {'body': 'hi\x00'}, {'days': []}, {'days': '3'},
            {'days': [3, 7, 14, 21]}, {'days': [3, 7, 7]}, {'days': [True]},
            {'followup_number': 0}, {'followup_number': 4}, {'followup_number': True},
            {'previous_followups': []}, {'previous_followups': ['Hi {name}']},
            {'previous_followups': [None]}, {'previous_followups': ['a' * 50_001]},
            {'api_key': 'unexpected'},
        ]
        for changeset in changes:
            with self.subTest(changes=list(changeset)), patch('autofollow.drafting.request_response') as fetch:
                with self.assertRaises(ValueError):
                    draft_followup({**REQUEST, **changeset}, model='test')
                fetch.assert_not_called()
        for value in (None, [], {}, {'to': 'you@example.com'}):
            with self.assertRaises(ValueError):
                validate_draft_request(value)

    def test_missing_or_malformed_key_never_calls_network(self):
        for key in ('', 'export OPENAI_API_KEY', 'sk-test key'):
            with self.subTest(key=key), patch.dict(os.environ, {'OPENAI_API_KEY': key}), patch(
                'autofollow.openai_llm.urllib.request.urlopen'
            ) as fetch:
                with self.assertRaisesRegex(OpenAIError, 'OPENAI_API_KEY'):
                    draft_followup(REQUEST, model='test')
                fetch.assert_not_called()

    def test_incomplete_refused_malformed_or_placeholder_outputs_fail_closed(self):
        bad = [None, [], {'status': 'incomplete'}, {'status': 'completed', 'output': []},
               {'status': 'completed', 'output': [None]},
               {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'refusal'}]}]},
               response(''), response('  '), response('Hi [name]'), response(None),
               response('a' * 50_001), response('hello\x00')]
        for raw in ('not JSON', '[]', '{"body": "Hi", "subject": "Extra"}'):
            data = response()
            data['output'][0]['content'][0]['text'] = raw
            bad.append(data)
        for data in bad:
            with self.subTest(data=str(data)[:100]), patch('autofollow.drafting.request_response', return_value=data):
                with self.assertRaisesRegex(OpenAIError, 'email has not changed'):
                    draft_followup(REQUEST, model='test')

    def test_transport_failure_propagates_without_fallback(self):
        with patch('autofollow.drafting.request_response', side_effect=OpenAIError('OpenAI connection failed')) as fetch:
            with self.assertRaisesRegex(OpenAIError, 'connection failed'):
                draft_followup(REQUEST, model='test')
            self.assertEqual(fetch.call_count, 1)
