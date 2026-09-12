# Hermes Usage 1.2.0 verification receipt

This is a scoped implementation/test receipt, not a security certification or
marketplace approval. Maintainer security re-review remains pending.

## Revisions and scope

- Independently reviewed supervision/security revision:
  `14e9f82f4410e8943b9332d94d2bb7836159c055`.
- Independently reviewed feature integration:
  `9af131477cf9c01f9eee80546dd7aac200484474` (no P0/P1 findings).
- Final tested code: `e6d7288f9787a2f5f4143f4f2d1d3c2fdebf4e43`.
  This adds the RED→GREEN active-session attribution-cap regression/fix to that
  reviewed integration; overflow is reported as partial history.
- The publication commit containing this receipt changes documentation and the
  synthetic preview only. Its SHA is the enclosing Git commit, not the code SHA
  above. Publication gates are rerun at that frozen commit before pushing;
  private execution logs record the exact final SHA and individual exit codes.

## Actual acceptance results before the documentation-only commit

- Full isolated suite: **90 tests passed**, no skips, in **33.945 seconds** at
  the final code SHA. HOME, HERMES_HOME, XDG_CONFIG_HOME, XDG_STATE_HOME and TMPDIR
  explicitly selected private fixture roots, not production account stores.
- Actual Quickshell lifecycle matrix: **8 cases, 11 worker runs, 23 observed
  wait returns, 30 immediate startup/destruction requests**. Ptrace receipts
  verify final group signal before leader reap, actual descendant waits,
  outer ECHILD, sentinel survival and nonoverlapping worker intervals.
- Lanes include startup/destruction, active unload, hard reload, shell exit and
  SIGKILL, unload/re-enable during cleanup, subsequent refresh, normal completion
  and leader-first exit with detached/double-fork, TERM-ignoring descendants.
- Resolved `qs/Commons` QML lint, Omarchy plugin validation and `git diff --check`
  passed. Only the existing framework `QProcess::ExitStatus` lint warning remains.
- Seven synthetic compositor cases cover empty, ordinary, unknown-cost, partial,
  multiple-account, expired and long-label records; native close, Escape,
  reopen, resize, refresh/busy and TTL behavior were exercised.
- Real local installation byte-matched the final code. Service refresh advanced
  the record; Details and the explicitly installed desktop launcher opened;
  native close, Escape, reopen, hide and busy-button behavior were verified.
  Remove/add hot reload required a supported shell restart on the tested host.
- One explicitly approved live Codex export returned an observed account
  allowance, matched the local record, and rendered source, reset and the
  account-selection caveat. **Live export VERIFIED**; personal quantities,
  timestamps and screenshots are deliberately not published. This is a
  point-in-time observation, not a statement of current allowance. No additional
  provider fetch is required for publication/reinstallation.

## Reproducible source and limits

See [tests](../tests/), [lifecycle runner](../tests/lifecycle_runner.py),
[supervisor](../collector/launch.py), [bootstrap](../collector/bootstrap.py),
[Details](../Details.qml), [companion](../hermes-usage-export/),
[security boundary](security-boundary.md), and
[lifecycle invariants](security-lifecycle.md). The
[README](../README.md#development--verification) describes fixture isolation and
lint imports. Failed probes and RED receipts remain in private evidence rather
than being relabeled as passes.

Supervision is exclusive direct-parent ownership, not a PID-leadership check.
WNOWAIT retains the leader until the last group signal; pidfds target owned,
unreaped direct/adopted children, and drainage continues to ECHILD. QML owns a
disposable bootstrap; pipe-lease EOF cancels the surviving ephemeral supervisor.
Its persistent lock excludes replacement workers while cleanup is pending.
External SIGKILL of that supervisor, uninterruptible kernel tasks and persistent
permission failures limit cleanup guarantees. This is not a hostile-code or
same-UID sandbox.

Reasoning remains an output subset; estimated and actual cumulative costs are
separate, and latest row status is not priced-call coverage. Missing/stale
allowances and unobserved costs stay unavailable, never invented zero/full
quotas. The optional companion alone permits explicit authenticated fetching;
it trusts Hermes credential/transport helpers (which may buffer, redirect or use
configured bases). File-size caps are not network-response caps. No purchases,
reset redemption, automatic schedule or Hermes core modification is included.

![Synthetic Details fixture preview](../preview.png)

Details preview uses synthetic fixture data; not an account statement.
