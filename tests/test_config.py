from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autofollow.config import DEFAULT_FOLLOWUP_TEMPLATE, initialize_project, load_config


class ConfigTests(unittest.TestCase):
    def test_reset_resolves_new_template_path(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / 'config.toml'
            path.write_text('[followup]\ndefault_template_path = "custom/message.txt"\n')
            cfg = initialize_project(path, overwrite=True)
            self.assertEqual(cfg.default_template_path, (base / 'templates/followup.txt').resolve())
            self.assertEqual(cfg.default_template_path.read_text(), DEFAULT_FOLLOWUP_TEMPLATE)
            self.assertFalse((base / 'custom').exists())

    def test_reset_repairs_invalid_config_in_nested_directory(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'nested/config.toml'
            cfg = initialize_project(path)
            self.assertTrue(cfg.default_template_path.exists())
            path.write_text('not valid toml')
            self.assertEqual(initialize_project(path, overwrite=True).timezone, 'UTC')

    def test_existing_custom_template_is_preserved(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / 'config.toml'
            path.write_text('[followup]\ndefault_template_path = "custom.txt"\n')
            (base / 'custom.txt').write_text('Custom text')
            self.assertEqual(initialize_project(path).default_template_path.read_text(), 'Custom text')

    def test_defaults_and_overrides(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            cfg = load_config(path)
            self.assertEqual(cfg.initial_lookback_days, 30)
            self.assertEqual(cfg.timezone, 'UTC')
            path.write_text('[inbox]\ntimezone = "America/New_York"\ninitial_lookback_days = 60\n[llm]\nmodel = "custom"\nhost = "http://localhost:1234/"\n[email]\nown_addresses = [" ME@EXAMPLE.COM "]\n')
            cfg = load_config(path)
            self.assertEqual(cfg.initial_lookback_days, 60)
            self.assertEqual(cfg.timezone, 'America/New_York')
            self.assertEqual(cfg.llm_model, 'custom')
            self.assertEqual(cfg.llm_host, 'http://localhost:1234')
            self.assertEqual(cfg.own_addresses, ['me@example.com'])

    def test_invalid_values_have_configuration_errors(self):
        cases = [
            'google = "bad"',
            '[followup]\ndefault_delay_days = true',
            '[followup]\ndefault_max_followups = 0',
            '[followup]\ndefault_delay_days = "5"',
            '[inbox]\ninitial_lookback_days = -1',
            '[inbox]\ntimezone = "MadeUp/Zone"',
            '[google]\nscopes = "scope"',
            '[google]\nscopes = []',
            '[google]\ncredentials_path = 42',
            '[email]\nown_addresses = [42]',
            '[email]\nown_addresses = "me@example.com"',
            '[llm]\nmodel = ""',
            '[llm]\nhost = "file:///tmp/model"',
            '[llm]\nhost = "http://localhost:bad"',
        ]
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            for value in cases:
                with self.subTest(config=value):
                    path.write_text(value)
                    with self.assertRaises(ValueError):
                        load_config(path)


if __name__ == '__main__':
    unittest.main()
