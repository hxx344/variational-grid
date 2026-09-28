"""Run the installer's actual preserved-config validation on native Python."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from variational_grid.models import GridError


class InstallerValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='grid-install-config-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.conf = self.root / 'conf'
        self.state = self.root / 'state'
        self.conf.mkdir()
        self.state.mkdir()
        project = Path(__file__).resolve().parents[1]
        self.base = json.loads((project / 'config.example.json').read_text())
        self.base.update(session_file=str(self.state / 'session.json'), state_file=str(self.state / 'single.sqlite3'))
        self.write('config.json', self.base)
        self.qqq = json.loads((project / 'qqq-hedge.example.json').read_text())
        self.qqq.update(base_config=str(self.conf / 'config.json'), output_dir=str(self.state / 'qqq'))
        self.write('qqq-hedge.json', self.qqq)
        self.companion = json.loads((project / 'cl-bz-scalper.example.json').read_text())
        self.companion.update(base_config=str(self.conf / 'config.json'), output_dir=str(self.state / 'cl-bz-scalper'))
        self.write('cl-bz-scalper.json', self.companion)
        installer = (project / 'install.sh').read_text()
        helper = installer.split('python3 - "$mode" "$cl_bz_active" <<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
        helper = helper.replace('/etc/variational-grid', self.conf.as_posix())
        helper = helper.replace('/var/lib/variational-grid', self.state.as_posix())
        self.code = compile(helper, 'install.sh:validate_settings', 'exec')

    def write(self, name, data):
        (self.conf / name).write_text(json.dumps(data))

    def validate(self, enabled=True):
        with patch.object(sys, 'argv', ['install.sh', 'qqq-hedge', '1' if enabled else '0']):
            exec(self.code, {'__name__': 'installer_validation_under_test'})

    def test_independent_experiment_validates_without_touching_configuration_or_data(self):
        original = {path: path.read_bytes() for path in self.conf.iterdir()}
        self.validate()
        self.assertEqual({path: path.read_bytes() for path in self.conf.iterdir()}, original)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_inactive_companion_does_not_validate_or_rewrite_its_settings(self):
        path = self.conf / 'cl-bz-scalper.json'
        path.write_text('{"kind":"invalid-preserved-companion"}')
        self.validate(enabled=False)
        self.assertEqual(path.read_text(), '{"kind":"invalid-preserved-companion"}')

    def test_companion_requires_exact_kind_and_base_config(self):
        alternate = self.conf / 'different-base.json'
        alternate.write_text(json.dumps(self.base))
        for change in ({'kind': 'inventory'}, {'base_config': str(alternate)}):
            with self.subTest(change=change), self.assertRaises(SystemExit):
                self.write('cl-bz-scalper.json', {**self.companion, **change})
                self.validate()

    def test_companion_rejects_equal_nested_parent_and_outside_paths(self):
        outputs = (self.state / 'qqq', self.state / 'qqq/nested', self.state,
                   self.root / 'outside', self.state / 'session.json/nested',
                   self.state / 'single.sqlite3/nested')
        for output in outputs:
            with self.subTest(output=output), self.assertRaises((SystemExit, GridError)):
                self.write('cl-bz-scalper.json', {**self.companion, 'output_dir': str(output)})
                self.validate()
        self.qqq['output_dir'] = str(self.state / 'cl-bz-scalper/nested-qqq')
        self.write('qqq-hedge.json', self.qqq)
        self.write('cl-bz-scalper.json', self.companion)
        with self.assertRaisesRegex(SystemExit, 'separate from all saved'):
            self.validate()

    def test_companion_cannot_reuse_saved_historical_or_previous_qqq_paths(self):
        for name, key in (('experiments.json', 'output_dir'), ('inventory.json', 'output_dir'),
                          ('inventory-base.json', 'state_file'), ('qqq-hedge.json', 'previous_output_dir')):
            with self.subTest(name=name, key=key):
                saved = self.qqq if name == 'qqq-hedge.json' else {}
                self.write(name, {**saved, key: str(self.state / 'cl-bz-scalper')})
                with self.assertRaises((SystemExit, GridError)):
                    self.validate()
                if name == 'qqq-hedge.json':
                    self.write(name, self.qqq)
                else:
                    (self.conf / name).unlink()


if __name__ == '__main__':
    unittest.main()
