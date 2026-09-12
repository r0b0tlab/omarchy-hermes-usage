import importlib.util
import os
import signal
import time
import unittest
from pathlib import Path

REAP = Path(__file__).resolve().parent.parent / "collector" / "reap.py"

spec = importlib.util.spec_from_file_location("reap", REAP)
reap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reap)


class ReapTest(unittest.TestCase):
    def test_pgid_of_self_matches_kernel(self):
        self.assertEqual(reap.process_group(os.getpid()), os.getpgrp())

    def test_group_leader_detection_matches_kernel(self):
        self.assertEqual(reap.is_group_leader(os.getpid()), os.getpgid(os.getpid()) == os.getpid())

    def test_kills_own_session_group(self):
        pid = os.fork()
        if pid == 0:
            try:
                os.setsid()
                time.sleep(30)
            finally:
                os._exit(0)
        try:
            deadline = time.time() + 5
            while time.time() < deadline and reap.process_group(pid) != pid:
                time.sleep(0.05)
            self.assertEqual(reap.process_group(pid), pid)
            self.assertTrue(reap.is_group_leader(pid))
            reap.send(signal.SIGTERM, pid)
            _, status = os.waitpid(pid, 0)
            self.assertTrue(os.WIFSIGNALED(status))
            self.assertEqual(os.WTERMSIG(status), signal.SIGTERM)
        finally:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                os.waitpid(pid, 0)
            except ChildProcessError:
                pass


if __name__ == "__main__":
    unittest.main()
