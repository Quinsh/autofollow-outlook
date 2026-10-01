import unittest
from types import SimpleNamespace
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path

from autofollow.content_safety import validate_content
from autofollow.mail import build_email
from autofollow.sequence import validate_plan
from autofollow.config import initialize_project


class ContentSafetyTests(unittest.TestCase):
    def test_recognizable_unresolved_fields_block_message_and_plan(self):
        for token in ('[name]', '{name}', '{{first_name}}', '[Professor Name]',
                      '<recipient_name>', '[Your Name]', '[insert research topic]',
                      '{sender_email}', '[university]'):
            with self.subTest(token=token):
                with self.assertRaisesRegex(ValueError, 'unresolved placeholder'):
                    build_email(to='prof@example.edu', subject='Research', body='Hi '+token)
                with self.assertRaisesRegex(ValueError, 'unresolved placeholder'):
                    validate_plan([{'day': 3, 'body': 'Hi '+token}])

    def test_subject_and_signature_checked(self):
        with self.assertRaisesRegex(ValueError, 'Subject'):
            build_email(to='prof@example.edu', subject='Research at [university]', body='Hello Professor Lee')
        with self.assertRaisesRegex(ValueError, 'Sender name'):
            build_email(to='prof@example.edu', subject='Research', body='Hello', sender_name='[Your name]')

    def test_ordinary_scientific_and_literal_text_preserved(self):
        body = 'Dear Professor Lee,\nI studied [1] and {x, y}, with {"key": 1} and {literal}.\nBest, Kai\n'
        validate_content(body)
        self.assertEqual(build_email(to='prof@example.edu', subject='Research', body=body).message.get_content(), body)

    def test_bad_followup_prevents_original_send_before_authentication(self):
        from autofollow.app import command_send
        with TemporaryDirectory() as tmp:
            cfg = initialize_project(Path(tmp) / 'config.toml')
            args = SimpleNamespace(config=str(cfg.config_path), body='Hello Professor Lee', body_file=None,
                followup_template=None, followup_days=None, max_followups=None,
                followup_plan='[{"day":3,"body":"Hi [name]"}]', to='prof@example.edu',
                name=None, subject='Research', cc=None, bcc=None, sender_name=None, dry_run=False)
            with patch('autofollow.gmail_client.GmailClient') as gmail:
                with self.assertRaisesRegex(ValueError, 'unresolved placeholder'):
                    command_send(args)
                gmail.assert_not_called()
            self.assertFalse(cfg.database_path.exists())
