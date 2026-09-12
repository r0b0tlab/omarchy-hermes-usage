#!/usr/bin/python3
"""Disposable QML child; a private pipe leases one ephemeral supervisor.

QML must keep stdinEnabled true. EOF on that upstream pipe also covers shell
loss before Python entry. Only this process holds the private lease writer.
No fixture command, module or lease path is configurable through the CLI/env.
"""
import fcntl
import importlib.util
import os
from pathlib import Path
import select
import signal
import stat
import sys
import time


def close_all(fds):
    """Attempt every close even if an individual descriptor fails."""
    for fd in fds:
        try:
            os.close(fd)
        except OSError:
            pass


def acquire_lock(environment):
    """Descriptor-relative, nofollow, owned persistent inode; never unlink it.

    HOME is the stable per-user exclusion scope, independent of state/runtime
    overrides. A same-UID adversary can replace our files and is not sandboxed.
    """
    home = environment.get('HOME', '')
    if not home.startswith('/') or '\0' in home or len(home) > 4096:
        raise OSError('unsafe HOME')
    parts = home.split('/')[1:]
    if not parts or any(p in ('', '.', '..') for p in parts):
        raise OSError('unsafe HOME components')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    lock = None
    try:
        for part in parts:
            next_fd = os.open(part, flags, dir_fd=fd)
            close_all([fd])
            fd = next_fd
            info = os.fstat(fd)
            sticky_root = info.st_uid == 0 and info.st_mode & stat.S_ISVTX
            if info.st_uid not in (0, os.getuid()) or (info.st_mode & 0o022 and not sticky_root):
                raise OSError('unsafe HOME ancestor')
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OSError('unsafe HOME ownership')
        try:
            os.mkdir('.hermes-usage-supervision', mode=0o700, dir_fd=fd)
        except FileExistsError:
            pass
        private = os.open('.hermes-usage-supervision', flags, dir_fd=fd)
        close_all([fd])
        fd = private
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise OSError('unsafe supervision directory')
        lock = os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600, dir_fd=fd)
        info = os.fstat(lock)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise OSError('unsafe supervision lock')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            close_all([lock])
            lock = None
        result, lock = lock, None
        return result
    finally:
        close_all([fd] + ([] if lock is None else [lock]))


def lease_closed(fd):
    # No byte protocol: any data or EOF cancels. Never block on an empty lease.
    return bool(select.select([fd], [], [], 0)[0])


def supervisor(lease, argv):
    lock = None
    try:
        os.setsid()
        # Fork clears Linux PDEATHSIG. Close inherited upstream stdin and every
        # unneeded descriptor, including the lease writer, before any worker.
        close_all(int(name) for name in os.listdir('/proc/self/fd')
                  if int(name) not in (1, 2, lease))
        null = os.open('/dev/null', os.O_RDONLY)
        if null != 0:
            os.dup2(null, 0)
            close_all([null])
        if lease_closed(lease):
            return 130
        spec = importlib.util.spec_from_file_location('usage_supervisor', Path(__file__).resolve().with_name('launch.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.require_unprivileged()
        lock = acquire_lock(os.environ)
        if lock is None:
            return 0  # old cleanup owns exclusion; skip, never overlap
        if lease_closed(lease):
            return 130
        result = module.main(argv, lease_fd=lease, lock_fd=lock)
        sys.stdout.flush()
        sys.stderr.flush()
        return result
    except Exception:
        try:
            os.write(2, b'hermes-usage: bootstrap refused or failed\n')
        except OSError:
            pass
        return 126
    finally:
        close_all([lease] + ([] if lock is None else [lock]))


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    descriptors = set()
    handlers = {}
    cancelled = [False]
    child = None
    try:
        # A real QProcess stdin pipe, not an interactive terminal or guessed PID.
        if not stat.S_ISFIFO(os.fstat(0).st_mode) or lease_closed(0):
            return 130
        handlers[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            handlers[sig] = signal.signal(sig, lambda *_: cancelled.__setitem__(0, True))
        read_fd, write_fd = os.pipe2(os.O_CLOEXEC)
        descriptors.update((read_fd, write_fd))
        child = os.fork()
        if child == 0:
            # Never run bootstrap's finally in the forked process.
            for sig in handlers:
                signal.signal(sig, signal.SIG_DFL)
            close_all([write_fd])
            os._exit(supervisor(read_fd, args))
        close_all([read_fd])
        descriptors.remove(read_fd)
        while True:
            if cancelled[0] or lease_closed(0):
                close_all(descriptors)
                descriptors.clear()
            done, status = os.waitpid(child, os.WNOHANG)
            if done:
                code = os.waitstatus_to_exitcode(status)
                return code if code >= 0 else 128 - code
            time.sleep(0.02)
    finally:
        close_all(descriptors)
        # Closing the lease is the ONLY cancellation action. On exceptional
        # setup after fork keep ownership and wait, never kill the supervisor.
        if child is not None and child > 0:
            while True:
                try:
                    os.waitpid(child, 0)
                    break
                except InterruptedError:
                    continue
                except ChildProcessError:
                    break
        for sig, handler in handlers.items():
            try:
                signal.signal(sig, handler)
            except OSError:
                pass


if __name__ == '__main__':
    try:
        result = main()
    except Exception:
        try:
            os.write(2, b'hermes-usage: bootstrap failed\n')
        except OSError:
            pass
        result = 126
    os._exit(result)
