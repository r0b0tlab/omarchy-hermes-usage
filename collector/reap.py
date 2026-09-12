#!/usr/bin/python3
"""Signal a collector process group — never a group we do not own.

Usage: reap.py <SIGNAL> <pid>

The launcher puts the collector in its own session, so pid == pgid == sid.
This helper re-checks that relationship at signal time and falls back to a
direct kill when the process is not a group leader, so a stale or unexpected
pid can never take down an unrelated process group (such as the shell's).
Prints nothing; exit status only.
"""

from __future__ import annotations

import os
import signal
import sys


def process_group(pid: int) -> int:
    return os.getpgid(pid)


def is_group_leader(pid: int) -> bool:
    return process_group(pid) == pid


def send(signum: int, pid: int) -> None:
    if is_group_leader(pid):
        os.killpg(pid, signum)
    else:
        os.kill(pid, signum)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        return 2
    name, pid_text = argv[1], argv[2]
    if not name.startswith("SIG"):
        name = "SIG" + name
    signum = getattr(signal, name, None)
    if not isinstance(signum, int):
        return 2
    try:
        pid = int(pid_text)
    except ValueError:
        return 2
    if pid <= 1:
        return 2
    try:
        send(signum, pid)
    except (ProcessLookupError, PermissionError):
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
