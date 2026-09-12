# Hermes Agent Usage

Hermes Agent usage in the built-in Omarchy **Agents** panel — today's tokens,
the last week, and the all-time per-model breakdown, next to Claude Code and
Codex.

[Hermes Agent](https://github.com/NousResearch/hermes-agent) is an autonomous
coding and task agent that keeps its session history in a local SQLite store.
This plugin reads that store and hands the numbers to the agents panel in the
shape it already understands, so Hermes gains a tab without the built-in widget
being patched.

## Install

```sh
omarchy plugin add https://github.com/r0b0tlab/omarchy-hermes-usage.git --enable
```

Then look at the **Agents** widget in the bar (it ships in the default Omarchy
bar layout) — Hermes appears as a subscription alongside the others.

Requires Hermes Agent, Python 3 (stdlib only), and the built-in
`omarchy.agents` widget enabled. If the widget is disabled, this plugin still
keeps `~/.local/state/omarchy/agents/usage/hermes.json` up to date — nothing
else consumes it.

## What you get

- **Tokens by day** — the last seven days of Hermes activity, with today
  showing its prompt and session counts on hover.
- **Tokens by model** — all-time totals per model, split into input, output,
  cache read, and cache write.
- **Hero line** — the dominant provider and whether its tokens rode a
  subscription (`Codex subscription`) or metered usage (`DeepSeek usage`);
  with no dominant provider it stays `Usage breakdown`.
- **Plan mix** — the hero line names the dominant provider and whether its tokens rode a subscription (`Codex subscription`) or metered usage (`DeepSeek usage`); with no dominant provider it stays `Usage breakdown`. Per-provider tokens, subscription share, and tracked estimated cost ride in the record's `providerUsage` object.

There are no rate-limit meters. Hermes is not queried against a provider usage
endpoint, so there are no session/weekly windows to draw and the limits section
stays empty rather than showing invented numbers.

## How it works

```
Service.qml                       # entry point, kind: service
└── runs on a 15-minute timer
    └── /usr/bin/python3 -I collector/launch.py --write
        ├── validates the interpreter (HERMES_USAGE_PYTHON or /usr/bin/python3)
        ├── enters its own session, closes the environment to an allowlist
        └── collector/hermes-usage.py
            ├── reads  $HERMES_HOME/state.db            (SQLite, read-only)
            ├── reads  $HERMES_HOME/profiles/*/state.db (every profile)
            └── writes $XDG_STATE_HOME/omarchy/agents/usage/hermes.json  (atomic)
```

The plugin ships no UI. The agents panel discovers `.json` records in the usage
directory and renders whatever it finds, so the record is the whole interface —
the same door `omarchy-agent-usage-update` writes through. The plugin exists
instead of a collector script because `omarchy-agent-usage-update` only globs
collectors out of the package-owned `$OMARCHY_PATH/bin`, which a plugin must
not write to.

### What the numbers mean

| Panel row | Source |
|---|---|
| Tokens by day | `session_model_usage` token counters, placed on the calendar by the session's assistant-message activity per day |
| Tokens by model | `session_model_usage` grouped by model (input, output + reasoning, cache read, cache write) |
| Today's prompts / sessions | `messages` rows with `role = 'user'` today, and sessions with activity today |
| All-time prompts / sessions / active days | `messages`, `sessions` |
| Hero plan line | dominant billing_provider/billing_mode in session_model_usage (subscription_included ⇒ subscription) |
| providerUsage | per-provider tokens + estimated_cost_usd (local estimate, cost_source varies; not a bill) |

No rate-limit windows: Hermes exposes no usage-limits endpoint, so `limits` is `[]`. No balance: Hermes has no prepaid ledger, so no `balance` object is emitted. Cost figures are local estimates (`cost_source` varies by provider), not bills.

Hermes records usage per session and model rather than per message, so a
session that ran across several days has its counters spread over the days it
was actually active (`DAY_MAP_HORIZON_DAYS` = 120 days back, weighted by that
session's assistant messages per day; older rows are placed on the day they
were last recorded). Subagent sessions and background tasks (title generation,
compaction, approvals) are counted too — they cost the same tokens as anything
else.

## Capabilities

Disclosed in full, because plugins run unsandboxed inside `omarchy-shell`:

- **Reads** `$HERMES_HOME/state.db` and `$HERMES_HOME/profiles/*/state.db`
  read-only (`mode=ro`, `PRAGMA query_only`). No message content is read —
  only counts, token counters, model names, and timestamps.
- **Writes** exactly one file:
  `$XDG_STATE_HOME/omarchy/agents/usage/hermes.json`, written to a temp file in
  the same directory and renamed into place.
- **Runs** one command: `/usr/bin/python3 -I <plugin dir>/collector/launch.py --write`, on shell start and every 15 min. The launcher validates the interpreter, closes the environment to an explicit allowlist, and re-executes the collector in a private session; the service terminates that entire process group after 60 s (SIGTERM, then SIGKILL after a 3 s grace). The collector spawns nothing and needs no network.
- **No network access. No sudo. No other commands. No telemetry.** Nothing
  leaves the machine.

### Bounds

Every input from the local store is budgeted: 20k usage rows/store
(recent-first), 20k attribution sessions, 64 models (+`other` bucket),
128-char names, 5M SQLite ops budget, 2 s busy timeout, 256 KiB record
ceiling, owned no-symlink output dir, single-flight lock.

Process boundary: the only executable spawned is the fixed `/usr/bin/python3`;
`HERMES_USAGE_PYTHON`, when set, must be an absolute, root-owned,
non-group/world-writable regular executable or it is refused (with a warning,
falling back to the fixed default). Both stages run `-I` with a closed
environment (`HOME`, `XDG_STATE_HOME`, `HERMES_HOME`, `TZ` only, each passed
through only when set). The service kills the collector's process group after
60 s: SIGTERM, then SIGKILL 3 s later if needed. Stderr is capped at the
source (8 KiB per run) and consumed as a stream — at most 4 KiB retained for
the log, and a run that exceeds 64 KiB on stderr is terminated.

## Configuration

`HERMES_USAGE_REFRESH_SEC` overrides the refresh interval (minimum 60).
`HERMES_USAGE_PYTHON` selects the interpreter that runs the collector; it
must be an absolute path to a root-owned, non-group/world-writable regular
executable — checked before use, and an invalid value is refused with a
warning while `/usr/bin/python3` is used instead. `HERMES_HOME` is
honored if you keep Hermes outside `~/.hermes`, and profile
stores under it are picked up automatically.

A refresh can be forced without waiting for the timer — this is the exact
command the service runs:

```sh
/usr/bin/python3 -I ~/.config/omarchy/plugins/io.github.r0b0tlab.hermes-usage/collector/launch.py --write
```

The plugin registers no IPC target of its own, so `omarchy-shell shell call
io.github.r0b0tlab.hermes-usage …` answers `unknown`; that is expected, not a
failure.

### Icon

The panel resolves a provider's mark from its own `assets/<id>.svg`, and
plugins must not write into the package-owned built-in plugin directory, so
Hermes falls back to the panel's bar glyph (with one harmless
`Cannot open: …/agents/assets/hermes.svg` warning in the shell log the first
time the panel opens). Shipping a `hermes.svg` mark belongs upstream in
Omarchy, not in a plugin.

## Uninstall

```sh
omarchy plugin remove io.github.r0b0tlab.hermes-usage
rm ~/.local/state/omarchy/agents/usage/hermes.json
```

Removing the plugin stops the timer; deleting the record removes the Hermes tab
from the panel. No other state is kept.

## Development

The collector runs standalone — same code path the service uses:

```sh
python3 collector/hermes-usage.py                 # print the record
/usr/bin/python3 -I collector/launch.py --write   # the exact service path
python3 -m unittest discover -s tests -v          # unit tests
omarchy plugin validate .                         # manifest and layout
qmllint -I "$OMARCHY_PATH/shell" Service.qml      # entry point against the shell imports
```

`--force` and `--limits-only` are accepted and ignored, so the script can also
be dropped into an `omarchy-agent-usage-<agent>` slot unchanged. It exits `1`
and writes nothing when no Hermes store exists, so a machine without Hermes
never grows an empty tab.

## License

MIT — see [LICENSE](LICENSE).
