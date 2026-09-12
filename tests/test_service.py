from pathlib import Path
from unittest import TestCase

class ServiceBoundary(TestCase):
    def test_only_supervisor_owns_escalation(self):
        source = (Path(__file__).resolve().parents[1] / 'Service.qml').read_text()
        self.assertNotIn('reap.py', source)
        self.assertNotIn('processId', source)
        self.assertNotIn('killGrace', source)
        self.assertIn('clearEnvironment: true', source)
        self.assertIn('"/usr/bin/python3", "-I", root.launcherPath', source)
