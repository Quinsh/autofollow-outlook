import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autofollow.app import main


class CliTests(unittest.TestCase):
    def test_all_commands_build(self):
        from autofollow.app import build_parser
        parser = build_parser()
        args = parser.parse_args(['task', '1', 'done'])
        self.assertTrue(callable(args.func))

    def test_module_propagates_failure(self):
        result = subprocess.run([sys.executable, '-m', 'autofollow', 'auto', '--once', '--interval-minutes', '0'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('at least 1', result.stderr)

    def test_doctor_missing_credentials_is_failure(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            config = str(Path(tmp) / 'config.toml')
            self.assertEqual(main(['--config', config, 'init']), 0)
            self.assertEqual(main(['--config', config, 'doctor']), 1)

    def test_incompatible_task_filters_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(['showemail', '--needs-action', '--task-state', 'done']), 1)

    def test_auto_one_cycle_isolates_failures(self):
        with patch('autofollow.app.command_check', side_effect=RuntimeError('offline')), patch('autofollow.app.command_label', return_value=0) as label, patch('autofollow.app.command_showemail', return_value=0), contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
            # mock function has no __name__; give it a display name as a command
            import autofollow.app as app
            app.command_check.__name__ = 'command_check'
            self.assertEqual(main(['auto', '--once']), 1)
            label.assert_called_once()

    def test_auto_dry_run_does_not_classify(self):
        with patch('autofollow.app.command_check', return_value=0), patch('autofollow.app.command_label') as label, patch('autofollow.app.command_showemail', return_value=0), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['auto', '--once', '--dry-run']), 0)
            label.assert_not_called()

    def test_auto_retries_after_task_display_failure(self):
        with patch('autofollow.app.command_check', return_value=0) as check, patch('autofollow.app.command_label', return_value=0), patch('autofollow.app.command_showemail', side_effect=[RuntimeError('database locked'), 0]) as show, patch('autofollow.app.time.sleep', side_effect=[None, KeyboardInterrupt]) as sleep, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            show.__name__ = 'command_showemail'
            self.assertEqual(main(['auto']), 130)
            self.assertEqual(check.call_count, 2)
            self.assertEqual(sleep.call_count, 2)


if __name__ == '__main__':
    unittest.main()
