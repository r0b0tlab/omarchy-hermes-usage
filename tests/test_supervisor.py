import json
import subprocess
from pathlib import Path
from unittest import TestCase

RUNNER = Path(__file__).with_name('supervisor_runner.py')

class SupervisorTest(TestCase):
    def run_worker(self, code, mode=""):
        p = subprocess.run(['/usr/bin/python3', '-B', str(RUNNER), code, mode],
                           capture_output=True, text=True, timeout=12)
        self.assertEqual(p.returncode, 0, p.stderr)
        result = json.loads(p.stdout)
        self.assertEqual(result['fd_delta'], 0, result)
        self.assertEqual(result['children'], [], result)
        self.assertTrue(result['echild'], result)
        self.assertLess(result['elapsed'], 4, result)
        return result

    def test_normal(self):
        r = self.run_worker("print('ok')")
        self.assertEqual(r['code'], 0)
        self.assertEqual(r['stdout'], 'ok\n')

    def test_term_ignoring_worker(self):
        r = self.run_worker('import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(5)')
        self.assertEqual(r['code'], 124)

    def test_raw_byte_cap_without_newline(self):
        r = self.run_worker("import os; os.write(2, b'\\xff'*50000)")
        self.assertEqual(r['code'], 125)
        # Retained bytes are capped before replacement-character decoding.
        self.assertLessEqual(len(r['stderr']), 8192)

    def test_leader_exits_before_detached_descendant(self):
        r = self.run_worker('import os,time\npid=os.fork()\nif pid==0:\n os.setsid(); time.sleep(5); os._exit(0)\nprint(pid,flush=True)\nos._exit(0)')
        self.assertEqual(r['code'], 0)
        self.assertIn(int(r['stdout'].strip()), r['reaped'])

    def test_exit_status_and_final_output(self):
        r = self.run_worker("import os; os.write(1,b'final'*1000); os._exit(17)")
        self.assertEqual(r['code'], 17)
        self.assertEqual(r['stdout'], 'final'*1000)

    def test_stdout_raw_flood(self):
        r = self.run_worker("import os; os.write(1,b'\\xff'*400000)")
        self.assertEqual(r['code'], 125)
        self.assertEqual(len(r['stdout']), 262144)
        self.assertEqual(r['counts'][0], 262145)

    def test_double_fork(self):
        r = self.run_worker("import os,time\np=os.fork()\nif p==0:\n os.setsid(); q=os.fork()\n if q==0:\n  print(os.getpid(),flush=True); time.sleep(3); os._exit(0)\n print(os.getpid(),flush=True); os._exit(0)\ntime.sleep(0.1); os._exit(0)")
        self.assertEqual(r['code'], 0)
        self.assertEqual(len(r['stdout'].split()), 2)
        self.assertTrue(set(map(int,r['stdout'].split())) <= set(r['reaped']))

    def test_startup_failures_leave_no_children_or_fds(self):
        for mode in ('pidfd', 'selector', 'register', 'fork', 'pipe', 'subreaper'):
            with self.subTest(mode=mode):
                r = self.run_worker('import time; time.sleep(2)', mode)
                self.assertIn('error', r)

    def test_pre_handshake_exit(self):
        r = self.run_worker('pass', 'early')
        self.assertEqual(r['code'], 23)
        self.assertFalse(any(e[0]=='group' for e in r['events']))

    def test_exec_failure(self):
        self.assertEqual(self.run_worker('pass', 'exec')['code'], 126)

    def test_cancellation_and_unrelated_sentinel(self):
        import signal, time
        sentinel = subprocess.Popen(['/usr/bin/python3','-I','-c','import time; time.sleep(6)'])
        p = subprocess.Popen(['/usr/bin/python3','-B',str(RUNNER),
            'import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); os.kill(os.getppid(),signal.SIGTERM); time.sleep(3)'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            out, err = p.communicate(timeout=12)
            self.assertEqual(p.returncode, 0, err)
            r = json.loads(out)
            self.assertEqual(r['code'],130)
            self.assertEqual(r['children'],[])
            self.assertTrue(r['echild'])
            self.assertLess(r['elapsed'],4)
            self.assertIsNone(sentinel.poll())
        finally:
            if p.poll() is None:
                p.terminate(); p.wait(timeout=8)
            sentinel.terminate(); sentinel.wait(timeout=8)

    def test_persistent_pidfd_failure_with_detached_descendant(self):
        r = self.run_worker('import os,time\np=os.fork()\nif p==0:\n os.setsid(); time.sleep(3); os._exit(0)\ntime.sleep(3)', 'pidfd-descendants')
        self.assertIn('error', r)
        self.assertEqual(len([e for e in r['events'] if e[0]=='wait']), 2)

    def test_group_absent_still_reaps_children(self):
        r = self.run_worker('import time; time.sleep(3)', 'group-esrch')
        self.assertEqual(r['code'],124)
        self.assertEqual(len(r['reaped']),1)

    def test_no_privilege_gaining_exec(self):
        r = self.run_worker("print(next(x for x in open('/proc/self/status') if x.startswith('NoNewPrivs:')),end='')")
        self.assertEqual(r['stdout'].split()[-1], '1')
