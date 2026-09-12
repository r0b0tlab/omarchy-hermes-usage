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
if mode == 'pidfd':
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
except OSError as error:
    result = {'error': error.errno}
finally:
    if patcher:
        patcher.stop()
result['elapsed'] = time.monotonic() - started
result['events'] = events
result['fd_delta'] = len(os.listdir('/proc/self/fd')) - before
result['children'] = m.direct_children()
try:
    os.waitpid(-1, os.WNOHANG)
    result['echild'] = False
except ChildProcessError:
    result['echild'] = True
print(json.dumps(result))
