import json
import unittest
from unittest.mock import Mock, patch

from autofollow.local_llm import (
    CLASSIFICATION_VERSION, OllamaError, classify_email_with_ollama,
    enforce_priority_policy, render_daily_digest, validate_classification,
)


def decision(**overrides):
    result = dict(needs_action=False, important=False, very_important=False,
                  category='other', deadline=None, reason='Routine update',
                  priority_basis='none', important_evidence=None,
                  direct_contact=False, personal_evidence=None, bulk_message=False)
    result.update(overrides)
    return result


class PriorityTests(unittest.TestCase):
    def classify(self, response, body, **kwargs):
        http = Mock()
        http.__enter__ = Mock(return_value=http)
        http.__exit__ = Mock(return_value=False)
        http.read.return_value = json.dumps({'response': json.dumps(response)}).encode()
        with patch('autofollow.local_llm.urllib.request.urlopen', return_value=http) as urlopen:
            classified = classify_email_with_ollama('Test subject', 'sender@example.com', body, **kwargs)
        prompt = json.loads(urlopen.call_args.args[0].data)['prompt']
        self.assertIn('Most inbox messages are NOT important', prompt)
        return classified

    def test_newsletter_overclassification_is_downgraded(self):
        body = 'Weekly opportunities: apply by Friday'
        result = self.classify(decision(important=True, very_important=True,
            category='newsletter', priority_basis='personal_opportunity',
            important_evidence=body, personal_evidence=body, direct_contact=True,
            bulk_message=True), body)
        self.assertFalse(result['important'])
        self.assertFalse(result['very_important'])

    def test_professor_and_personal_offer_are_very_important(self):
        examples = [
            ('professor', 'Following our meeting, please send me your thesis revision', True),
            ('recruiting', 'Your application impressed me; I would like to offer you the role', False),
        ]
        for category, body, needs_action in examples:
            with self.subTest(category=category):
                result = self.classify(decision(important=True, very_important=True,
                    needs_action=needs_action, category=category, priority_basis='personal_opportunity',
                    important_evidence=body, personal_evidence=body, direct_contact=True), body)
                self.assertTrue(result['very_important'])
                self.assertTrue(result['important'])

    def test_automated_required_bill_important_without_personal_priority(self):
        body = 'Pay your overdue balance by Friday to avoid a $25 late fee'
        result = self.classify(decision(needs_action=True, important=True, very_important=True,
            category='finance', priority_basis='required_action', important_evidence=body,
            bulk_message=True), body)
        self.assertTrue(result['important'])
        self.assertFalse(result['very_important'])

    def test_transport_bulk_signal_overrules_claimed_personal_contact(self):
        body = 'You have been selected for an offer'
        result = self.classify(decision(important=True, very_important=True,
            priority_basis='personal_opportunity', direct_contact=True,
            personal_evidence=body, important_evidence=body), body, is_bulk=True)
        self.assertFalse(result['very_important'])
        self.assertFalse(result['important'])

    def test_fabricated_or_empty_evidence_rejects_priority(self):
        for evidence in (None, '', 'You will lose enrollment unless you respond'):
            with self.subTest(evidence=evidence):
                result = self.classify(decision(important=True, needs_action=True,
                    priority_basis='required_action', important_evidence=evidence), 'Hello there')
                self.assertFalse(result['important'])
                self.assertTrue(result['needs_action'])

    def test_optional_action_does_not_imply_important(self):
        result = self.classify(decision(needs_action=True, category='school'), 'RSVP to our optional webinar')
        self.assertTrue(result['needs_action'])
        self.assertFalse(result['important'])

    def test_strict_schema_for_new_fields(self):
        for updates in ({'very_important': 'false'}, {'direct_contact': 1},
                        {'bulk_message': 'false'}, {'priority_basis': []},
                        {'important_evidence': 5}, {'personal_evidence': False}):
            with self.subTest(updates=updates), self.assertRaises(OllamaError):
                enforce_priority_policy(decision(**updates), subject='', body='')
        missing = decision()
        del missing['very_important']
        with self.assertRaises(OllamaError):
            validate_classification(missing)

    def test_very_important_implies_important(self):
        self.assertTrue(validate_classification(decision(very_important=True))['important'])
        self.assertEqual(CLASSIFICATION_VERSION, 2)

    def test_digest_priorities_are_disjoint_and_completed_contact_remains_visible(self):
        base = dict(sender='sender', subject='Routine', reason='Reason', needs_action=False)
        emails = [dict(base, important=False), dict(base, important=True, subject='Bill'),
                  dict(base, important=True, very_important=True, needs_action=True,
                       task_state='done', subject='Professor invitation')]
        digest = render_daily_digest(emails)
        self.assertIn('Very important emails: 1', digest)
        self.assertIn('Important emails: 1', digest)
        self.assertIn('Other emails: 1', digest)
        self.assertIn('Professor invitation', digest)
        self.assertIn('None today.', digest)
        self.assertLess(digest.index('Professor invitation'), digest.index('- Bill'))
