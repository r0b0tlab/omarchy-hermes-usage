"""Fixture-only installed Hermes CLI entrypoint with fail-closed audit guards."""
import os
import sys
from pathlib import Path

root = Path(os.environ['HERMES_FEATURE_FIXTURE']).resolve()

def audit(event, args):
    if event in ('socket.connect', 'socket.connect_ex', 'socket.getaddrinfo', 'subprocess.Popen', 'os.system'):
        raise RuntimeError('fixture forbids network and child processes: ' + event)
    if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod') and isinstance(args[0], (str, bytes)):
        if not Path(os.fsdecode(args[0])).absolute().is_relative_to(root):
            raise RuntimeError('fixture forbids external filesystem changes')
    if event in ('os.rename', 'os.link', 'os.symlink'):
        for item in args[:2]:
            if isinstance(item, (str, bytes)) and not Path(os.fsdecode(item)).absolute().is_relative_to(root):
                raise RuntimeError('fixture forbids external filesystem changes')
    if event == 'open' and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).absolute()
        private = path.is_relative_to(root)
        if not private and (path.name in ('.env', '.op.env', 'auth.json') or path.suffix == '.key'):
            raise RuntimeError('fixture forbids external credential files')
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        if not private and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
            raise RuntimeError('fixture forbids external writes')

sys.addaudithook(audit)
from hermes_cli.main import main
raise SystemExit(main())
