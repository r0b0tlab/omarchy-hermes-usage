"""Subprocess-only harness: owns subreaper state, never runs in unittest host."""
import errno
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('launch', Path(__file__).resolve().parents[1] / 'collector/launch.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
mode = sys.argv[2] if len(sys.argv) > 2 else ''
original_wait = os.waitpid
original_group = os.killpg
waited = set()
events = []

def wait(pid, flags):
    result = original_wait(pid, flags)
    if result[0]:
        waited.add(result[0])
        events.append(['wait', result[0]])
    return result

def group(pid, sig):
    assert pid not in waited, 'group signal after leader reap'
    events.append(['group', pid, sig])
    return original_group(pid, sig)

before = len(os.listdir('/proc/self/fd'))
started = time.monotonic()
worker = 'import signal; signal.alarm(6)\n' + sys.argv[1]
command = ['/usr/bin/python3', '-I', '-c', worker]
if mode == 'exec':
    command = ['/nonexistent-hermes-fixture-executable']
patcher = None
exhausted = []
limit_before = None
if mode in ('empty-listing', 'partial-listing'):
    original_read = os.read
    def listing(fd, n):
        value = original_read(fd, n)
        if os.readlink('/proc/self/fd/%d' % fd).endswith('/children'):
            return b'' if mode == 'empty-listing' else b' '.join(value.split()[:1])
        return value
    patcher = patch.object(m.os, 'read', side_effect=listing)
elif mode == 'wait-error':
    real_wait = original_wait
    injected = [False]
    def transient_wait(pid, flags):
        if not injected[0]:
            injected[0] = True
            raise OSError(errno.EIO, 'injected wait failure')
        return real_wait(pid, flags)
    original_wait = transient_wait
elif mode == 'permission-pending':
    def denied(*args):
        raise PermissionError(errno.EACCES, 'injected signalling denial')
    original_group = denied
    patcher = patch.object(m.signal, 'pidfd_send_signal', side_effect=denied)
elif mode == 'final-group-error':
    real_group = m.signal_reserved_group
    def group_error(pid, sig):
        if sig == signal.SIGKILL:
            raise RuntimeError('injected final group exception')
        return real_group(pid, sig)
    patcher = patch.object(m, 'signal_reserved_group', side_effect=group_error)
elif mode == 'held-writer':
    original_pipe = os.pipe
    def held_pipe():
        pair = original_pipe()
        if not exhausted:
            exhausted.append(os.dup(pair[1]))
        return pair
    patcher = patch.object(m.os, 'pipe', side_effect=held_pipe)
elif mode == 'enumeration-value-error':
    original_read = os.read
    injected = [False]
    def read(fd, n):
        if (not injected[0] and time.monotonic() - started > .1
                and os.readlink('/proc/self/fd/%d' % fd).endswith('/children')):
            injected[0] = True
            raise ValueError('injected malformed listing')
        return original_read(fd, n)
    patcher = patch.object(m.os, 'read', side_effect=read)
elif mode == 'close-error':
    original_close = os.close
    def close(fd):
        target = os.readlink('/proc/self/fd/%d' % fd)
        original_close(fd)
        if target.endswith('/children'):
            raise OSError(errno.EIO, 'injected close result')
    patcher = patch.object(m.os, 'close', side_effect=close)
elif mode == 'real-emfile':
    import resource
    limit_before = resource.getrlimit(resource.RLIMIT_NOFILE)
    real_pidfd = os.pidfd_open
    def exhaust(*args):
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, limit_before[1]))
        while True:
            try:
                exhausted.append(os.open('/dev/null', os.O_RDONLY))
            except OSError as error:
                assert error.errno == errno.EMFILE
                break
        return real_pidfd(*args)
    patcher = patch.object(m.os, 'pidfd_open', side_effect=exhaust)
elif mode == 'pidfd-descendants':
    def no_pidfds(*args):
        time.sleep(0.1)
        raise OSError(errno.EMFILE, 'injected persistent exhaustion')
    patcher = patch.object(m.os, 'pidfd_open', side_effect=no_pidfds)
elif mode == 'group-esrch':
    patcher = patch.object(m, 'signal_reserved_group', side_effect=lambda *args: None)
elif mode == 'pidfd':
    patcher = patch.object(m.os, 'pidfd_open', side_effect=OSError(errno.EMFILE, 'injected'))
elif mode == 'selector':
    patcher = patch.object(m.selectors, 'DefaultSelector', side_effect=OSError(errno.EMFILE, 'injected'))
elif mode == 'register':
    patcher = patch.object(m.selectors.EpollSelector, 'register', side_effect=OSError(errno.ENOMEM, 'injected'))
elif mode == 'fork':
    patcher = patch.object(m.os, 'fork', side_effect=OSError(errno.EAGAIN, 'injected'))
elif mode == 'pipe':
    original_pipe = os.pipe
    calls = [0]
    def pipe():
        calls[0] += 1
        if calls[0] == 3:
            raise OSError(errno.EMFILE, 'injected')
        return original_pipe()
    patcher = patch.object(m.os, 'pipe', side_effect=pipe)
elif mode == 'early':
    patcher = patch.object(m.os, 'setsid', side_effect=lambda: os._exit(23))
elif mode == 'subreaper':
    patcher = patch.object(m, 'subreaper', side_effect=OSError(errno.EPERM, 'injected'))
if patcher:
    patcher.start()
try:
    with patch.object(m.os, 'waitpid', side_effect=wait), patch.object(m.os, 'killpg', side_effect=group):
        result = m.supervise(command, {}, timeout=0.5, grace=0.2)
    result['stdout'] = result['stdout'].decode('utf-8', 'replace')
    result['stderr'] = result['stderr'].decode('utf-8', 'replace')
except Exception as error:
    result = {'error': getattr(error, 'errno', type(error).__name__)}
finally:
    if patcher:
        patcher.stop()
    for fd in exhausted:
        os.close(fd)
    if limit_before is not None:
        resource.setrlimit(resource.RLIMIT_NOFILE, limit_before)
result['elapsed'] = time.monotonic() - started
result['events'] = events
result['fd_delta'] = len(os.listdir('/proc/self/fd')) - before
result['children'] = m.direct_children()
try:
    os.waitpid(-1, os.WNOHANG)
    result['echild'] = False
except ChildProcessError:
    result['echild'] = True
result['emergency_waited'] = []
if not result['echild']:
    # Preserve the failing production snapshot above, then safely reap only
    # this harness's unreaped direct/adopted fixture children before exiting.
    while True:
        for child in m.direct_children():
            try:
                os.kill(child, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            done, _ = original_wait(-1, os.WNOHANG)
        except ChildProcessError:
            break
        if done:
            result['emergency_waited'].append(done)
        time.sleep(.01)
print(json.dumps(result))
