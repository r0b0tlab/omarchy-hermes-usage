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
- **Hero line** — the provider paying for the usage, taken from the dominant
  billing provider in the last 30 days (`ChatGPT subscription`, `Nous Portal`,
  `DeepSeek`, …), or `Local session totals` when nothing recent is billed.

There are no rate-limit meters. Hermes is not queried against a provider usage
endpoint, so there are no session/weekly windows to draw and the limits section
stays empty rather than showing invented numbers.

## How it works

```
Service.qml                       # entry point, kind: service
└── runs on a 15-minute timer
    └── collector/hermes-usage.py --write
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
| Hero provider | Dominant `billing_provider` by tokens in the last 30 days |

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
  only counts, token counters, model names, providers, and timestamps.
- **Writes** exactly one file:
  `$XDG_STATE_HOME/omarchy/agents/usage/hermes.json`, written to a temp file in
  the same directory and renamed into place.
- **Runs** one command: `python3 <plugin dir>/collector/hermes-usage.py --write`,
  every 15 minutes, plus once when the shell starts.
- **No network access. No sudo. No other commands. No telemetry.** Nothing
  leaves the machine.

## Configuration

`HERMES_USAGE_REFRESH_SEC` overrides the refresh interval (minimum 60).
`HERMES_HOME` is honored if you keep Hermes outside `~/.hermes`, and profile
stores under it are picked up automatically.

A refresh can be forced without waiting for the timer — this is the exact
command the service runs:

```sh
python3 ~/.config/omarchy/plugins/io.github.r0b0tlab.hermes-usage/collector/hermes-usage.py --write
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
python3 collector/hermes-usage.py            # print the record
python3 collector/hermes-usage.py --write    # write it, then look at the panel
omarchy plugin validate .                    # manifest and layout
qmllint -I "$OMARCHY_PATH/shell" Service.qml # entry point against the shell imports
```

`--force` and `--limits-only` are accepted and ignored, so the script can also
be dropped into an `omarchy-agent-usage-<agent>` slot unchanged. It exits `1`
and writes nothing when no Hermes store exists, so a machine without Hermes
never grows an empty tab.

## License

MIT — see [LICENSE](LICENSE).
