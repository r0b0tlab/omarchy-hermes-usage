"""Bootstrap contract tests; subprocess lifecycle lanes are separate."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]

class BootstrapBoundary(unittest.TestCase):
    def test_service_never_signals_a_starting_process(self):
        source = (ROOT / 'Service.qml').read_text()
        self.assertNotIn('.signal(', source)
        self.assertIn('collector/bootstrap.py', source)

    def test_bootstrap_flushes_normal_output(self):
        import shutil
        import subprocess
        with tempfile.TemporaryDirectory() as home:
            folder = Path(home)
            for name in ('bootstrap.py', 'launch.py'):
                shutil.copyfile(ROOT / 'collector' / name, folder / name)
            (folder / 'hermes-usage.py').write_text("print('bootstrap output')\n")
            with subprocess.Popen(['/usr/bin/python3', '-I', str(folder / 'bootstrap.py')],
                    env={'HOME': home}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE) as child:
                self.assertEqual(child.wait(timeout=5), 0)
                self.assertEqual(child.stdout.read(), b'bootstrap output\n')

    def test_bootstrap_reaps_with_inherited_ignored_sigchld(self):
        import shutil
        import signal
        import subprocess
        with tempfile.TemporaryDirectory() as home:
            folder = Path(home)
            for name in ('bootstrap.py', 'launch.py'):
                shutil.copyfile(ROOT / 'collector' / name, folder / name)
            (folder / 'hermes-usage.py').write_text('pass\n')
            with subprocess.Popen(['/usr/bin/python3', '-I', str(folder / 'bootstrap.py')],
                    env={'HOME': home}, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    preexec_fn=lambda: signal.signal(signal.SIGCHLD, signal.SIG_IGN)) as child:
                self.assertEqual(child.wait(timeout=5), 0, child.stderr.read())

    def test_host_eof_before_bootstrap_entry_never_spawns(self):
        import subprocess
        with tempfile.TemporaryDirectory() as home:
            read_fd, write_fd = os.pipe()
            os.close(write_fd)
            try:
                result = subprocess.run(['/usr/bin/python3', '-I', str(ROOT / 'collector/bootstrap.py')],
                    env={'HOME': home}, stdin=read_fd, capture_output=True, timeout=5)
            finally:
                os.close(read_fd)
            self.assertEqual(result.returncode, 130)
            self.assertFalse((Path(home) / '.hermes-usage-supervision').exists())

    def test_cli_respects_supervisor_lock(self):
        import subprocess
        spec = importlib.util.spec_from_file_location('bootstrap', ROOT / 'collector/bootstrap.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as home:
            lock = m.acquire_lock({'HOME': home})
            try:
                result = subprocess.run(['/usr/bin/python3', '-I', str(ROOT / 'collector/launch.py'), '--write'],
                    env={'HOME': home, 'HERMES_HOME': home}, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stderr, b'')
            finally:
                os.close(lock)

    def test_partial_bootstrap_setup_restores_handlers_and_fds(self):
        import signal
        from types import SimpleNamespace
        from unittest.mock import patch
        spec = importlib.util.spec_from_file_location('bootstrap', ROOT / 'collector/bootstrap.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        before = set(os.listdir('/proc/self/fd'))
        handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)}
        with patch.object(m.os, 'fstat', return_value=SimpleNamespace(st_mode=0o010000)), \
                patch.object(m, 'lease_closed', return_value=False), \
                patch.object(m.os, 'fork', side_effect=OSError('injected fork failure')):
            with self.assertRaises(OSError):
                m.main([])
        self.assertEqual(set(os.listdir('/proc/self/fd')), before)
        self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)

    def test_close_all_attempts_every_descriptor(self):
        from unittest.mock import patch, call
        spec = importlib.util.spec_from_file_location('bootstrap', ROOT / 'collector/bootstrap.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with patch.object(m.os, 'close', side_effect=[OSError('injected'), None]) as close:
            m.close_all([71, 72])
        self.assertEqual(close.call_args_list, [call(71), call(72)])

    def test_unsafe_home_and_lock_modes_are_refused(self):
        spec = importlib.util.spec_from_file_location('bootstrap', ROOT / 'collector/bootstrap.py')
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        for home in ('relative', '/', '/tmp/../tmp', '/tmp/\0bad'):
            with self.subTest(home=home), self.assertRaises(OSError):
                m.acquire_lock({'HOME': home})
        with tempfile.TemporaryDirectory() as home:
            lock = m.acquire_lock({'HOME': home})
            os.close(lock)
            folder = Path(home) / '.hermes-usage-supervision'
            folder.chmod(0o755)
            with self.assertRaises(OSError):
                m.acquire_lock({'HOME': home})
            folder.chmod(0o700)
            (folder / 'lock').chmod(0o644)
            with self.assertRaises(OSError):
                m.acquire_lock({'HOME': home})

    def test_private_lock_excludes_and_rejects_symlinks(self):
        path = ROOT / 'collector/bootstrap.py'
        self.assertTrue(path.exists(), 'QML needs a disposable lease bootstrap')
        spec = importlib.util.spec_from_file_location('bootstrap', path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        with tempfile.TemporaryDirectory() as home:
            first = m.acquire_lock({'HOME': home})
            self.assertIsNotNone(first)
            self.assertIsNone(m.acquire_lock({'HOME': home}))
            os.close(first)
            second = m.acquire_lock({'HOME': home})
            os.close(second)
            lock = Path(home) / '.hermes-usage-supervision' / 'lock'
            lock.unlink()
            lock.symlink_to('/dev/null')
            with self.assertRaises(OSError):
                m.acquire_lock({'HOME': home})
