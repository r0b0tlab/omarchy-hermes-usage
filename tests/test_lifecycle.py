"""Required real-runtime acceptance lane; no mocks and no skipped fallback."""
import json
from pathlib import Path
import subprocess
from unittest import TestCase


class QuickshellLifecycle(TestCase):
    def test_isolated_production_chain(self):
        runner = Path(__file__).with_name('lifecycle_runner.py')
        result = subprocess.run(['/usr/bin/python3', '-B', str(runner)],
            capture_output=True, text=True, timeout=100, start_new_session=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        receipt = Path(result.stdout.split('receipt ')[-1].strip())
        data = json.loads(receipt.read_text())
        self.assertEqual({case['case'] for case in data['cases']},
            {'normal','active-unload','reload','shell-exit','shell-kill','reenable','leader-first','immediate'})
        self.assertTrue(data['outer_echild'])
        self.assertTrue(all(case['pass'] and case['same_group'] for case in data['cases']))
