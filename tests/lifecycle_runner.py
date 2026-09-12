"""Real offscreen Quickshell, exact production files, syscall wait receipts.

Run as a standalone subprocess, never import into the unittest process. All
fixture state is retained in scratch; a private /tmp alias only shortens Unix
socket names. No production shell/plugin or network operations.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import select
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback

from lifecycle_trace import WaitTrace

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = Path('/home/r0b0tmagic/hermes-workspace/scratch/hermes-usage-lifecycle')
EVIDENCE = Path('/home/r0b0tmagic/hermes-workspace/evidence/2026-09-12-hermes-usage-1.2.0/lifecycle')


def wait_until(check, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.01)
    raise AssertionError('bounded fixture condition timed out')


class Shell:
    def __init__(self, name):
        self.root = Path(tempfile.mkdtemp(prefix=name+'-', dir=SCRATCH))
        (self.root/'collector').mkdir()
        for relative in ('Service.qml', 'collector/bootstrap.py', 'collector/launch.py'):
            shutil.copyfile(ROOT/relative, self.root/relative)
            assert (ROOT/relative).read_bytes() == (self.root/relative).read_bytes()
        shutil.copyfile(ROOT/'tests/lifecycle-shell.qml', self.root/'shell.qml')
        shutil.copyfile(ROOT/'tests/lifecycle_worker.py', self.root/'collector/hermes-usage.py')
        (self.root/'mode').write_text('tree')
        (self.root/'runtime').mkdir(mode=0o700)
        # TMPDIR is intentionally long in the outer verification sandbox.
        # This private alias must actually live in /tmp for sockaddr_un.
        self.alias = Path(tempfile.mkdtemp(prefix='hul-', dir='/tmp'))
        (self.alias/'r').symlink_to(self.root/'runtime', target_is_directory=True)
        self.env = dict(HOME=str(self.root), HERMES_HOME=str(self.root), PATH='/usr/bin',
            XDG_CONFIG_HOME=str(self.root/'config'), XDG_STATE_HOME=str(self.root/'state'),
            XDG_RUNTIME_DIR=str(self.alias/'r'), QT_QPA_PLATFORM='offscreen', QS_NO_RELOAD_POPUP='1')
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        old = os.getcwd()
        os.chdir(self.root)
        try:
            self.listener.bind('events.sock')
        finally:
            os.chdir(old)
        self.listener.listen(10)
        self.listener.settimeout(5)
        self.log = (self.root/'qs.log').open('wb')
        self.process = subprocess.Popen(['/usr/bin/qs', '-p', str(self.root), '--no-color'],
            env=self.env, stdout=self.log, stderr=subprocess.STDOUT)
        self.calls = []
        self.channels = []
        try:
            wait_until(lambda: self.call('ping', check=False).stdout.strip() == 'ready')
        except BaseException:
            # Constructor failures must not lose the only owned Popen handle.
            self.close()
            raise

    def call(self, function, check=True):
        result = subprocess.run(['/usr/bin/qs','ipc','--pid',str(self.process.pid),
            'call','fixture',function], env=self.env, capture_output=True, text=True, timeout=3)
        self.calls.append(dict(function=function, code=result.returncode, out=result.stdout,
                               err=result.stderr, at=time.monotonic()))
        if check:
            assert result.returncode == 0, result.stderr
        return result

    def worker(self, mode='tree'):
        (self.root/'mode').write_text(mode)
        self.call('start')
        channel, _ = self.listener.accept()
        self.channels.append(channel)
        channel.settimeout(5)
        leader = json.loads(channel.recv(4096))
        assert leader['kind'] == 'leader'
        supervisor = leader['supervisor']
        status = dict(line.split(':', 1) for line in Path('/proc/%d/status' % supervisor).read_text().splitlines() if ':' in line)
        bootstrap = int(status['PPid'])
        lease_links = []
        for fd in Path('/proc/%d/fd' % bootstrap).iterdir():
            if int(fd.name) >= 3:
                lease_links.append(os.readlink(fd))
        assert len(lease_links) == 1, lease_links
        assert lease_links[0] not in leader['fds'].values(), 'worker inherited lease'
        assert not any(link.endswith('/lock') for link in leader['fds'].values())
        assert leader['sid'] == leader['pid'] and os.getsid(supervisor) == supervisor
        leader['bootstrap'] = bootstrap
        leader['lease'] = lease_links[0]
        trace = WaitTrace(leader['supervisor'])
        channel.send(b'go')
        events = [leader]
        if mode != 'normal':
            while not any(e['kind'] == 'ready' for e in events):
                events.append(json.loads(channel.recv(4096)))
            assert {e['kind'] for e in events} == {'leader','intermediate','detached','ready'}
        assert all(e['nnp'] == '1' for e in events)
        assert all(lease_links[0] not in e['fds'].values() for e in events), 'descendant inherited lease'
        return events, trace

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        self.process.wait(timeout=5)
        self.log.close()
        for channel in self.channels:
            channel.close()
        self.listener.close()
        shutil.rmtree(self.alias)


def verify_worker(events, trace):
    calls = trace.finish()
    waited = [e['result'] for e in calls if e['kind'] == 'wait4' and e['result'] > 0]
    owned = {e['pid'] for e in events}
    assert set(waited) == owned, (waited, owned, calls)
    assert len(waited) == len(owned), 'duplicate wait'
    assert any(e['kind'] == 'wait4' and e['result'] == -10 for e in calls), 'missing ECHILD'
    leader = events[0]['pid']
    leader_wait = next(e['at'] for e in calls if e['kind'] == 'wait4' and e['result'] == leader)
    assert any(e['kind'] == 'kill' and e['target'] == -leader for e in calls), 'missing private group signal'
    assert all(e['at'] < leader_wait for e in calls if e['kind'] == 'kill' and e['target'] == -leader)
    return dict(worker_events=events, syscalls=calls, waited=waited,
                interval=[events[0]['at'], max(e['at'] for e in calls if e['kind']=='wait4' and e['result']>0)])


def run_case(name, sentinel):
    shell = Shell(name)
    result = dict(case=name, root=str(shell.root), runs=[])
    assert os.getpgid(shell.process.pid) == os.getpgid(sentinel.pid), 'sentinel not in shell group'
    try:
        if name == 'immediate':
            for _ in range(30):
                shell.call('immediate')
            # Event-loop IPC barrier and a normal completed run prove the
            # instance remains healthy after immediate start/destroy requests.
            shell.call('ping')
            assert not select.select([shell.listener], [], [], .2)[0], 'worker escaped immediate destruction'
            events, trace = shell.worker('normal')
            result['runs'].append(verify_worker(events, trace))
        else:
            mode = name if name in ('normal', 'leader-first') else 'tree'
            events, trace = shell.worker(mode)
            if name in ('active-unload', 'reenable'):
                shell.call('stop')
            elif name == 'reload':
                shell.call('reload')
                wait_until(lambda: shell.call('ping', check=False).stdout.strip() == 'ready')
            elif name == 'shell-exit':
                shell.call('quit', check=False)
            elif name == 'shell-kill':
                shell.process.kill()  # Only our owned disposable QShell
            if name == 'reenable':
                shell.call('start')
                assert not select.select([shell.listener], [], [], .3)[0], 'overlapping worker'
            first = verify_worker(events, trace)
            result['runs'].append(first)
            if name in ('reenable', 'reload', 'active-unload'):
                shell.call('stop')
                events, trace = shell.worker('normal')
                second = verify_worker(events, trace)
                assert first['interval'][1] < second['interval'][0], 'overlapping worker intervals'
                result['runs'].append(second)
        assert sentinel.poll() is None, 'unrelated same-group sentinel died'
        result['sentinel_alive'] = True
        result['same_group'] = os.getpgid(sentinel.pid) == os.getpgrp()
        result['pass'] = True
    finally:
        result['ipc'] = shell.calls
        shell.close()
        (shell.root/'result.json').write_text(json.dumps(result, indent=2))
    return result


def main():
    assert platform.machine() == 'x86_64', 'syscall observer requires x86_64'
    assert ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) == 0
    SCRATCH.mkdir(parents=True, exist_ok=True)
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    receipt = EVIDENCE/('quickshell-'+str(time.time_ns())+'.json')
    results = dict(files={name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest()
        for name in ('Service.qml','collector/bootstrap.py','collector/launch.py')}, cases=[])
    sentinel = subprocess.Popen(['/usr/bin/python3','-I','-c','import time; time.sleep(120)'])
    try:
        cases = sys.argv[1:] or ['normal','active-unload','reload','shell-exit','shell-kill','reenable','leader-first','immediate']
        for name in cases:
            results['cases'].append(run_case(name, sentinel))
            receipt.write_text(json.dumps(results, indent=2))
            print(name, 'PASS', flush=True)
    except BaseException:
        results['failure'] = traceback.format_exc()
        raise
    finally:
        sentinel.terminate()
        sentinel.wait(timeout=5)
        # Reap only our adopted bootstrap/supervisors. No global PID scans.
        adopted = []
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline:
            try:
                done, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                results['outer_echild'] = True
                break
            if done:
                adopted.append([done, status])
            else:
                time.sleep(.02)
        results['adopted'] = adopted
        receipt.write_text(json.dumps(results, indent=2))
        print('receipt', receipt, flush=True)
    assert results.get('outer_echild'), 'fixture children remain'


if __name__ == '__main__':
    main()
