"""Isolated nested-parent death fixture; outer subreaper waits every fixture."""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location('launch', Path(__file__).resolve().parents[1] / 'collector/launch.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.subreaper()
with tempfile.TemporaryDirectory() as tmp:
    ready = str(Path(tmp) / 'ready')
    parent = os.fork()
    if parent == 0:
        supervisor = os.fork()
        if supervisor == 0:
            worker = ('import signal,time; signal.alarm(5); '
                      'signal.signal(signal.SIGTERM,signal.SIG_IGN); '
                      f'open({ready!r},"w").close(); time.sleep(4)')
            start = time.monotonic()
            r = m.supervise(['/usr/bin/python3','-I','-c',worker], {}, timeout=2, grace=0.2)
            r['stdout'] = r['stdout'].decode()
            r['stderr'] = r['stderr'].decode()
            r['elapsed'] = time.monotonic() - start
            r['children'] = m.direct_children()
            print(json.dumps(r), flush=True)
            os._exit(0)
        deadline = time.monotonic() + 3
        while not os.path.exists(ready) and time.monotonic() < deadline:
            time.sleep(0.01)
        os._exit(0)
    # The supervisor is adopted by this outer fixture, not init or unittest.
    statuses = []
    while True:
        try:
            pid, status = os.waitpid(-1, 0)
            statuses.append(os.waitstatus_to_exitcode(status))
        except ChildProcessError:
            break
    if statuses != [0, 0]:
        raise SystemExit('unexpected fixture exit statuses: ' + repr(statuses))
