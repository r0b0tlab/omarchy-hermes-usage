#!/usr/bin/python3
"""Bounded launcher for the Hermes usage collector.

Service.qml never executes anything but a fixed absolute interpreter; this
launcher runs under it, validates the interpreter override
(HERMES_USAGE_PYTHON), drops to a closed environment, puts itself in its own
session (so the service can terminate the whole process group), and
re-executes the collector with `-I`.
"""

from __future__ import annotations

import errno
import ctypes
import time
import signal
import os
import stat
import sys

DEFAULT_INTERPRETER = "/usr/bin/python3"
MAX_PATH_LENGTH = 1024
CHILD_ENV_ALLOWLIST = ("HOME", "XDG_STATE_HOME", "HERMES_HOME", "TZ")


def validate_interpreter(candidate: str) -> str | None:
    """Resolve `candidate` to a trusted interpreter path, or None.

    Trusted means: absolute, length-bounded, every resolved path component
    root-owned and not group/other writable, and the final component a
    regular file with at least one execute bit. Symlinks are resolved first,
    so a chain into a user-writable directory fails closed.
    """
    if not candidate or not candidate.startswith("/") or len(candidate) > MAX_PATH_LENGTH:
        return None
    if "\x00" in candidate:
        return None
    try:
        real = os.path.realpath(candidate)
    except OSError:
        return None
    info = None
    current = "/"
    for part in real.split("/")[1:]:
        current = os.path.join(current, part)
        try:
            info = os.lstat(current)
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode):  # realpath() already resolved these
            return None
        if info.st_uid != 0:
            return None
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None
    if info is None or not stat.S_ISREG(info.st_mode):
        return None
    if not info.st_mode & 0o111:
        return None
    return real


def child_environment(environ) -> dict[str, str]:
    """The exact environment the collector is allowed to see."""
    return {name: environ[name] for name in CHILD_ENV_ALLOWLIST if name in environ}


def select_interpreter(environ) -> tuple:
    """(interpreter, warning). An invalid override is refused, never run."""
    override = environ.get("HERMES_USAGE_PYTHON") or ""
    warning = None
    if override:
        validated = validate_interpreter(override)
        if validated:
            return validated, None
        warning = (
            f"hermes-usage: refused HERMES_USAGE_PYTHON={override!r} (must be an "
            "absolute root-owned, non-group/world-writable, regular executable); "
            f"using {DEFAULT_INTERPRETER}"
        )
    validated = validate_interpreter(DEFAULT_INTERPRETER)
    if not validated:
        return None, (warning + "; " if warning else "") + (
            f"hermes-usage: {DEFAULT_INTERPRETER} failed trust validation"
        )
    return validated, warning


TIMEOUT = 60.0
GRACE = 3.0
OUT_CAP = 262144
ERR_CAP = 8192


def subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), 'cannot enable child subreaper')


def direct_children():
    # This supervisor is single-threaded. Do not generalize to another PID.
    with open('/proc/self/task/%d/children' % os.getpid(), encoding='ascii') as f:
        return [int(x) for x in f.read().split()]


def signal_owned_child(pid, sig):
    # Caller owns this direct child and has NOT reaped it. No SIGCHLD handler
    # or Popen.poll()/wait() may independently reap children in this process.
    fd = os.pidfd_open(pid)
    try:
        signal.pidfd_send_signal(fd, sig)
    except ProcessLookupError:
        pass
    finally:
        os.close(fd)


def signal_reserved_group(pid, sig):
    # Valid ONLY while the group's leader is our unreaped child. ESRCH means
    # no signalable members remain; it does not excuse skipping wait/reaping.
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass


def drain_adopted():
    reaped = []
    while True:
        children = direct_children()
        if not children:
            try:
                os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return reaped
            time.sleep(0.02)
            continue
        for pid in children:
            # Still our child: no concurrent reaper, even if already a zombie.
            signal_owned_child(pid, signal.SIGKILL)
        for pid in children:
            done, _ = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped.append(done)
        time.sleep(0.02)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    interpreter, warning = select_interpreter(os.environ)
    if warning:
        print(warning, file=sys.stderr)
    if interpreter is None:
        return 1
    collector = os.path.join(os.path.dirname(os.path.realpath(__file__)), "hermes-usage.py")
    # Private session/process group: pid == pgid == sid afterwards, so the
    # service can signal this whole tree and nothing else.
    try:
        os.setsid()
    except OSError as error:
        if error.errno != errno.EPERM or os.getpgrp() != os.getpid():
            print(f"hermes-usage: cannot create a private process group: {error}", file=sys.stderr)
            return 1
    try:
        os.execve(interpreter, [interpreter, "-I", collector, *args], child_environment(os.environ))
    except OSError as error:
        print(f"hermes-usage: cannot execute {interpreter}: {error}", file=sys.stderr)
        return 1
    return 1  # unreachable: execve replaces the process


if __name__ == "__main__":
    raise SystemExit(main())
