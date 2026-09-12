import json
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]

class UiContract(TestCase):
    def test_details_are_visible_and_use_injected_service(self):
        path = ROOT / 'Details.qml'
        self.assertTrue(path.exists(), 'details panel missing')
        source = path.read_text()
        for value in ('property var service', 'service.refresh()', 'availableWidth',
                      'Text.PlainText', 'closingFromHost', 'accountRow.modelData',
                      'windowRow.modelData', 'latestStatusRows', 'estimatedUsd', 'actualUsd'):
            self.assertIn(value, source)
        self.assertNotIn('Process {', source)
        manifest = json.loads((ROOT / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], '1.2.0')
        self.assertEqual(manifest['kinds'], ['service', 'panel'])
        self.assertEqual(manifest['entryPoints']['panel'], 'Details.qml')
