# Ephemeral collection supervision

This documents the security implementation for the 1.2.0 development branch,
not publication approval. Independent review remains required. See the README
and security-boundary.md for the companion and Details feature boundary.

## QML lifetime is not supervisor lifetime

Quickshell 0.3.1 destroys a `Process` by immediately SIGKILLing its QProcess.
A destruction-time `signal(15)` cannot postpone that; during startup its numeric
PID can be zero. `Service.qml` therefore never calls `signal(int)`.

The production chain is:

1. QML runs `/usr/bin/python3 -I collector/bootstrap.py --write`, with its
   inherited environment cleared and only HOME, XDG_STATE_HOME, HERMES_HOME, TZ
   and HERMES_USAGE_PYTHON passed through. `stdinEnabled: true` keeps a host-owned
   input pipe open. Bootstrap refuses an already-closed or non-pipe stdin.
2. Bootstrap creates a private CLOEXEC pipe and forks one ephemeral supervisor.
   Only bootstrap retains the write end. It monitors host stdin EOF and closes
   its private lease on cancellation, host loss or an exception. QML destruction
   can SIGKILL bootstrap without SIGKILLing the supervisor.
3. The forked child establishes a separate session, closes every unneeded FD
   (including bootstrap's lease writer and host stdin), loads sibling launch.py
   by absolute file location under `-I`, and acquires exclusion. Fork clears
   Linux PDEATHSIG. It is not a permanently installed daemon or a worker launched
   without a cleanup owner.
4. The supervisor checks lease readiness before and after lock acquisition and
   immediately before worker fork. Cancellation racing the fork is detected by
   the supervision loop. Parent loss **before entry** is covered by host-pipe EOF,
   not guessed parent PIDs. The worker inherits neither lease nor lock.
5. The supervisor runs the same direct-parent collection loop used by the CLI.
   It enforces the 60-second collection deadline, a three-second TERM grace,
   final KILL and subreaper drainage to ECHILD. Bootstrap normally waitpids the
   supervisor; if bootstrap was killed, the kernel adopter reaps the supervisor
   after cleanup. There is no detached fire-and-forget worker.

## Exclusion and filesystem boundary

Both the QML supervisor and direct launch.py CLI take nonblocking exclusive
`flock` on `$HOME/.hermes-usage-supervision/lock`. A contender returns zero without
spawning a worker. A skipped refresh retries on the next timer/manual refresh;
it does not queue work or imply a new usage record was collected.

The supervisor holds the lock through all owned-child cleanup and final output.
The lock is never unlinked. HOME must be absolute, canonical in components,
not symlinked, and owned by the invoking UID without group/other write access.
Ancestors are opened from `/` with descriptor-relative O_DIRECTORY/O_NOFOLLOW;
only root or invoking-UID owners are accepted, and writable ancestors are
rejected except root-owned sticky directories such as `/tmp`. The private
directory must be exactly mode 0700; the lock must be a single-link, UID-owned,
regular mode-0600 file, opened nofollow/nonblocking relative to that directory.
Invalid existing modes are refused, not repaired. This adds one private directory
and one persistent empty lock inode. No runtime PID file is created.

HOME deliberately defines exclusion independently of XDG_STATE_HOME or
XDG_RUNTIME_DIR: changing runtime/state paths cannot bypass it within the same
HOME. Production supervision does not read or trust XDG_RUNTIME_DIR. Changing
HOME intentionally creates another scope. An adversary with the same UID can
replace plugin files/owned directories; this is not a same-UID sandbox.

## Identity, permissions and errors

- The supervisor is single-threaded and exclusively owns its children. It
  refuses root, unequal real/effective IDs and permitted/effective/ambient
  capabilities. Setuid/setgid interpreter modes are rejected. The worker sets
  PR_SET_NO_NEW_PRIVS before exec; its forked descendants inherit it.
- Worker setsid readiness is established by a private pipe. `waitid(WNOWAIT)`
  observes exit without reaping. The final group signal is **before any waitpid**;
  there is never a group signal after releasing the unreaped leader's PID.
- Direct/adopted children are signalled through pidfds opened while exclusively
  owned and unreaped. The existing EMFILE/ENFILE/ENOMEM fallback is a direct
  signal to an exclusively owned unreaped child, never an arbitrary PID lookup.
- The proc children descriptor is reserved before fork. Cleanup retains the
  known leader independently of proc results, tolerates incomplete/erroring
  enumeration, and counts global waitpid results every round. Only global
  ECHILD establishes completion. Permission and wait errors do not release
  ownership or report success while children remain.
- Partial setup/cleanup failures attempt all remaining descriptor closes and
  signal-handler restorations. Failed Linux close calls are not retried against
  potentially reused FD numbers. Exceptional group/enumeration/wait operations
  cannot skip owned-child drainage. No global process scanning or PID sweeps.
- After ECHILD, final draining reads only immediately available bounded chunks.
  EAGAIN stops the drain instead of spinning forever if a pipe FD was transferred
  to an external process. Counters remain saturating raw-byte counters.

Worker stdout/stderr retain at most 262144/8192 bytes before decoding. Final
stderr forwarding retains at most 2048 bytes plus static diagnostics. QML uses
chunk parsers, saturating per-run counters and a cumulative 2100-unit logging
budget even for bootstrap/interpreter pre-start errors. Its decoded UTF-16
ceilings are supplementary UI limits, **not** claims of raw-byte accounting.
It requests direct bootstrap termination on stream overflow, closing the lease.

## Limits and verification

Uninterruptible tasks or persistent permission/proc failures can keep cleanup
pending. The supervisor emits one bounded pending diagnostic and retains
exclusion; it cannot promise a finite kernel reaping deadline. External SIGKILL
of the **supervisor itself**, unlike destruction of bootstrap, prevents cleanup.
Arbitrary malicious executable code, fork bombs and same-UID file replacement
are not sandboxed. No production shell restart, provider call or installation is
part of these tests.

Run `/usr/bin/python3 -B -m unittest discover -s tests -v` for all unit and actual
Quickshell lanes. `tests/lifecycle_runner.py` also runs them separately. These
integration tests require installed Quickshell and Linux x86_64 ptrace access;
unavailable prerequisites fail, rather than skip or manufacture a pass.

The isolated shell copies **byte-identical** Service.qml, bootstrap.py and
launch.py; only sibling hermes-usage.py is replaced with a local fixture. No
production command selector or fixture environment knob is added. Test-only
Unix seqpacket events gate fixture startup; an ancestor ptrace observer records
actual wait4 syscall returns and group-signal ordering without modifying code,
syscalls or signal targets. The supervisor must actually wait every reported
leader/intermediate/detached child and observe ECHILD. A same-group, unrelated
Popen-owned sentinel must remain alive. An outer test subreaper reaps orphaned
bootstrap/supervisors and verifies ECHILD for the whole fixture.

Lanes include immediate start/destruction, active unload, hard reload, shell
exit, shell SIGKILL, unload/re-enable during old cancellation, later successful
refresh, normal completion and leader-first exit with detached/double-fork,
TERM-ignoring pipe-retaining children. Traces establish nonoverlapping worker
intervals and actual waits. FD snapshots verify lease/lock noninheritance and
real `/proc` NoNewPrivs checks include descendants. Unit subprocess lanes cover
output floods, retained external writer, partial/empty proc listings, transient
wait/enumeration failures, signalling denial with pending reporting, descriptor
exhaustion, failing closes and partial bootstrap setup.

Scratch/evidence roots for this development run:

- `/home/r0b0tmagic/hermes-workspace/scratch/hermes-usage-lifecycle/`
- `/home/r0b0tmagic/hermes-workspace/evidence/2026-09-12-hermes-usage-1.2.0/lifecycle/`

Each real-runtime receipt records production-file SHA256 values, actual worker
and wait events, interval bounds, IPC responses, sentinel survival and outer
ECHILD. Scratch `qs.log` files retain raw Quickshell logs. A private `/tmp/hul-*`
alias only shortens the scratch runtime path below Unix socket length limits;
its contents point into scratch and the alias is removed after the owned shell
exits. `QS_NO_RELOAD_POPUP=1` is fixture-only, since offscreen rendering has no
PanelWindow backend. Failed probes/RED receipts remain in evidence and are not
rewritten as passes.
