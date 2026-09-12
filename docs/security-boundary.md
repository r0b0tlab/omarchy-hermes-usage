# Security boundary: local activity and optional account observations

This plugin is **not a sandbox**. The Omarchy service is local-only; the optional
Hermes companion is a separate, explicitly invoked trusted-network component.
Review and production installation/provider validation remain separate gates.

## Supervision

The fixed QML child is a disposable bootstrap, not the collector. Its upstream
host-input pipe and private pipe lease cancel an ephemeral supervisor if QML is
unloaded, reloaded or killed. The supervisor survives bootstrap destruction and
retains `$HOME/.hermes-usage-supervision/lock` until owned-child cleanup ends.
The lock inode is persistent and never unlinked by running code.

The supervisor exclusively owns an unreaped direct worker. `waitid(WNOWAIT)`
observes exit without releasing that identity; the **last group signal happens
before leader reap**. Direct/adopted-child signals use pidfds while owned and
unreaped (the documented resource-exhaustion fallback still targets only an
exclusively owned unreaped direct child). A Linux subreaper drains detached and
double-fork descendants until global ECHILD. No numeric-PID reaper reopens
arbitrary process identities; no group signals occur after leader wait.

Worker streams are counted as raw bytes before decoding: 256 KiB stdout, 8 KiB
stderr. Collection has a 60-second deadline and three-second TERM grace. Cleanup
pending retains exclusion; D-state or persistent permission/proc failures can
prevent a finite reap deadline. An external SIGKILL of the supervisor itself
prevents cleanup. See [security-lifecycle.md](security-lifecycle.md) for exact
process, privilege, FD-acquisition and exceptional-path invariants and tests.

## Local record boundary

The worker reads bounded SQLite accounting, not message contents or credentials.
Rows, VM operations, attribution groups, profile enumeration and dictionary sizes
are bounded. Missing schema fields and failed scans are not complete observations.
Reasoning is an output subset. Estimated and actual cumulative amounts are
independent; latest status is not cost coverage. Serialization is checked on both
stdout and file paths; failure never emits an oversized final payload.

Collector output and account exports use the same stdlib filesystem module,
shipped inside the self-contained companion subdirectory. The collector loads it
by explicit absolute local path under Python `-I`, without importing exporter or
Hermes APIs. All path components are opened nofollow and held as directory FDs;
ownership/modes and retained parent-child associations are checked. Exclusive
relative mode-0600 temporaries, relative replacement/unlink and file/directory
fsync avoid following replaced pathnames. Rejected writes do not repair unsafe
permissions. Same-UID malicious code can still replace files/code: this is not an
account-signature or hostile-user sandbox.

## Optional network boundary

Only `hermes usage-export --provider … --allow-network` imports optional account
APIs and fetches. No shell daemon fetches, no scheduler is installed, no pool is
enumerated, and no purchases/reset redemption are exposed. Hermes controls its
credential reads and possible OAuth/cache updates. Anthropic may borrow a Claude
credential; Nous resolution may differ from the current conversation.

The exporter retains the exact account-selection caveat in the reader/UI:

> Hermes-resolved credential; may differ from this conversation; not a pool total

Only typed, finite amounts/windows and static/allowlisted strings are exported.
Raw responses, financial prose, errors, URLs, account IDs and credentials are not
copied. Nous requires `logged_in`, `fresh`, `source == account_api`; typed credits
are dollars, and paid-access/member-cap status remains visible. Positive account
credit does not grant the member access. Formatted Codex/OpenRouter balances are
not parsed. Unsupported or absent numeric allowance means unavailable.

The reader checks 16 KiB size, schema integer-not-bool, dict/list/window types,
finite ordered timestamps, ten-minute TTL, control characters, bounded nesting
and integer text length. Fresh and stale observations are filtered independently;
expired window arrays become unavailable after filtering. The UI rechecks expiry
while open. Observations are only from the active profile’s four fixed filenames.

**Transport limitation:** Hermes helpers may use configured bases, follow
redirects and buffer network bodies. The exported-file cap is not a response-size
cap or endpoint pin. No Hermes core networking is patched. The companion is a
trusted-Hermes integration coupled to the inspected API version, not an isolated
network broker. Local fixture tests explicitly avoid production auth/endpoints.

## Validation scope

Unit tests include real in-memory Hermes mixed-status UPSERT, old schemas,
partial scans, bounded aggregation/serialization, unsafe parents, swapped parent
paths, FIFO/symlink/oversized/deep/malformed snapshots and restricted Nous credit.
The CLI discovery fixture guards against network and external credential access.
The standalone Quickshell fixture uses synthetic records, captures actual pixels
and tests lifecycle/refresh/expiry. No fixture is evidence of live account quota.
Independent review and any approved live install/provider receipt remain required
before publication claims.
