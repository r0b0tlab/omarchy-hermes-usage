import importlib.util
import json
import os
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

def load(name):
    path = ROOT / 'hermes-usage-export' / (name + '.py')
    if not path.exists(): return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

class QuotaTest(TestCase):
    def test_optin_and_sanitized_snapshot(self):
        e = load('exporter')
        self.assertIsNotNone(e, 'explicit quota exporter missing')
        with patch('builtins.__import__', side_effect=AssertionError('import before optin')):
            self.assertEqual(e.command(NS(allow_network=False, provider='nous')), 2)
        now = time.time()
        from datetime import datetime, timezone
        snap = NS(provider='openai-codex', source='secret URL', plan='secret account',
                  fetched_at=datetime.fromtimestamp(now, timezone.utc), unavailable_reason=None,
                  windows=(NS(label='5 hour', used_percent=80, reset_at=None),), details=('SECRET',))
        r = e.normalize(snap, 'openai-codex', now)
        self.assertEqual(r['windows'][0]['remainingPercent'], 20)
        self.assertEqual(r['accountSelection'], 'Hermes-resolved credential; may differ from this conversation; not a pool total')
        self.assertNotIn('secret', json.dumps(r).lower())
        snap.fetched_at = datetime.fromtimestamp(now - 1000, timezone.utc)
        self.assertFalse(e.normalize(snap, 'openai-codex', now)['available'])

    def test_private_snapshot_roundtrip_and_parent_rejection(self):
        q = load('quota_io')
        self.assertIsNotNone(q, 'descriptor-relative snapshot IO missing')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder = root / 'profile' / 'usage-export'
            now = time.time()
            r = q.unavailable('nous', now)
            q.write_snapshot(folder, 'nous', r)
            self.assertEqual(q.read_snapshot(folder, 'nous', now), r)
            os.chmod(root / 'profile', 0o777)
            with self.assertRaises(OSError): q.write_snapshot(folder, 'nous', r)
            self.assertIsNone(q.read_snapshot(folder, 'nous', now))
            os.chmod(root / 'profile', 0o700)
            (root / 'linked').symlink_to(root / 'profile', target_is_directory=True)
            with self.assertRaises(OSError): q.write_snapshot(root / 'linked' / 'usage-export', 'nous', r)

    def test_strict_schema(self):
        q = load('quota_io')
        self.assertIsNotNone(q, 'strict quota schema missing')
        now = time.time(); r = q.unavailable('nous', now)
        for key, value in [('schemaVersion', True), ('windows', {}), ('fetchedAt', now + 1),
                           ('expiresAt', now - 1), ('expiresAt', now + 601),
                           ('available', 0), ('available', True), ('plan', 'bad\nlabel'),
                           ('fetchedAt', 10**10000)]:
            bad = dict(r); bad[key] = value
            self.assertIsNone(q.validate(bad, 'nous', now), key)
