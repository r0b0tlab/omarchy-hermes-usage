#!/usr/bin/python3
# omarchy:summary=Print the Hermes Agent usage record as JSON
# omarchy:args=[--force] [--limits-only] [--write]
# omarchy:hidden=true
"""Collect Hermes Agent usage into one display-ready JSON record.

Hermes Agent (https://github.com/NousResearch/hermes-agent) keeps its session
history in a local SQLite store: session totals in `sessions`, per-model token
and cost counters in `session_model_usage`, and turn counts in `messages`.
This script reads that store and renders it in the record shape the Omarchy
agents panel already understands, so Hermes shows up next to Claude Code and
Codex with today's tokens, the last week's bars, and the all-time per-model
breakdown.

Everything is local. No provider endpoint is called, nothing is uploaded, and
no network access is required.

Usage:

    hermes-usage.py             # print the record on stdout
    hermes-usage.py --write     # write it into the agents usage directory

`--force` and `--limits-only` are accepted and ignored so this script can be
dropped into `omarchy-agent-usage-<agent>` slots unchanged; this worker has no
authenticated endpoint access here. Fresh sanitized observations are read from
an explicitly invoked companion; local refresh never fetches account quotas.

Exit codes: 0 with a record printed/written, 1 with no usable input or a refused
write. Quota-only records contain no fabricated local activity.
"""

from __future__ import annotations

import importlib.util
import argparse
import datetime as dt
import io
import json
import math
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterable

# Explicit absolute local import: works with system Python -I; no Hermes APIs.
_quota_spec = importlib.util.spec_from_file_location('hermes_usage_quota_io',
    Path(__file__).resolve().parents[1] / 'hermes-usage-export' / 'quota_io.py')
quota_io = importlib.util.module_from_spec(_quota_spec)
_quota_spec.loader.exec_module(quota_io)

AGENT_ID = "hermes"
AGENT_NAME = "Hermes Agent"
WEEK_DAYS = 7
# Only rows touched inside this window get message-day attribution. Other
# rows use their recorded first/last span. Both are estimates, not billing days.
DAY_MAP_HORIZON_DAYS = 120
# Hermes bills across whatever providers are configured, often several at once,
# so the panel's single plan line cannot name one of them truthfully. It says
# what the panel is showing instead.
HERO_LABEL = "Usage breakdown"

PROVIDER_LABELS = {"openai-codex": "Codex", "anthropic": "Anthropic", "nous": "Nous", "deepseek": "DeepSeek", "zai": "Zhipu", "meta-ai": "Meta", "xai": "xAI", "openrouter": "OpenRouter"}

# Hardening budgets. Every query below is LIMITed, every unbounded collection
# is capped, and the serialized record has a ceiling; see each use site.
MAX_STORES = 32
MAX_PROFILE_ENTRIES = 128
MAX_DAY_GROUPS = 20000
MAX_USAGE_ROWS = 20000          # session_model_usage rows scanned per store
MAX_SESSION_IDS = 20000         # session ids held for prompt attribution
MAX_MODELS = 64                 # distinct models kept in modelUsage
MAX_MODEL_NAME_LEN = 128        # model/provider label characters kept
MAX_TOKEN_VALUE = 2**53         # per-counter clamp (exact in float64/JSON)
MAX_ACTIVE_DATES = 365          # dates kept in activeDates (existing cap, now named)
MAX_RECORD_BYTES = 262144       # 256 KiB serialized payload ceiling
SQLITE_OP_BUDGET = 5_000_000   # SQLite VM ops per connection before abort
SQLITE_BUSY_TIMEOUT_MS = 2000  # don't wedge the shell on a locked live store
MAX_STDERR_BYTES = 8192  # max UTF-8 bytes a single run may send to stderr


class CappedStderr(io.TextIOBase):
    """Write-through stderr that stops after MAX_STDERR_BYTES UTF-8 bytes.

    Bounds what one run can ever send to the shell at the source, so no
    consumer can accumulate more than this per run regardless of database
    content. Extra writes are swallowed; errors on a closed pipe are ignored.
    """

    def __init__(self, inner, limit: int) -> None:
        self._inner = inner
        self._limit = limit
        self._written = 0

    def write(self, text: str) -> int:
        if self._written < self._limit:
            room = self._limit - self._written
            chunk = text[:room].encode("utf-8", "replace")[:room].decode("utf-8", "ignore")
            self._written += len(chunk.encode("utf-8"))
            try:
                self._inner.write(chunk)
            except (OSError, ValueError):
                pass
        return len(text)

    def flush(self) -> None:
        try:
            self._inner.flush()
        except (OSError, ValueError):
            pass


def expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def hermes_home() -> Path:
    return expand(os.environ.get("HERMES_HOME") or "~/.hermes")


def state_home() -> Path:
    return expand(os.environ.get("XDG_STATE_HOME") or "~/.local/state")


def usage_dir() -> Path:
    return state_home() / "omarchy" / "agents" / "usage"


def store_paths(acc=None) -> list[Path]:
    """Every Hermes session store on this machine, primary profile first."""
    home = hermes_home()
    candidates = [home / "state.db"]
    profiles = home / "profiles"
    if profiles.is_dir():
        try:
            with os.scandir(profiles) as entries:
                for i, entry in enumerate(entries):
                    if i >= MAX_PROFILE_ENTRIES or len(candidates) >= MAX_STORES:
                        if acc is not None: acc.truncated = True
                        break
                    if entry.is_dir(follow_symlinks=False):
                        candidates.append(Path(entry.path) / 'state.db')
        except OSError:
            if acc is not None: acc.truncated = True
    found: list[Path] = []
    for path in candidates:
        if path.is_file() and path not in found:
            found.append(path)
    return found


def connect(path: Path) -> sqlite3.Connection:
    """Read-only connection, without writing to the agent's live store."""
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        # Some WAL setups refuse a read-only URI; fall back to a normal handle
        # with writes disabled at the SQL level.
        conn = sqlite3.connect(str(path), timeout=5)
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    ops = {"n": 0}

    def _budget() -> int:
        ops["n"] += 10000
        return 1 if ops["n"] > SQLITE_OP_BUDGET else 0

    conn.set_progress_handler(_budget, 10000)
    conn.row_factory = sqlite3.Row
    return conn


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def day_string(value: dt.date) -> str:
    return value.strftime("%Y-%m-%d")


def recent_dates(days: int = WEEK_DAYS) -> list[str]:
    today = dt.date.today()
    return [day_string(today - dt.timedelta(days=offset)) for offset in range(days - 1, -1, -1)]


def day_start_epoch(day: dt.date) -> float:
    return dt.datetime.combine(day, dt.time.min).timestamp()


def split_by_day(start: float | None, end: float | None) -> list[tuple[dt.date, float]]:
    """Spread one usage row across the days it spanned, evenly over time."""
    if start is None:
        start = end
    if start is None:
        return [(dt.date.today(), 1.0)]
    if end is None or end < start:
        end = start
    first = dt.date.fromtimestamp(start)
    last = dt.date.fromtimestamp(end)
    if first == last or end <= start:
        return [(first, 1.0)]
    span = end - start
    out: list[tuple[dt.date, float]] = []
    day = first
    while day <= last:
        low = day_start_epoch(day)
        high = day_start_epoch(day + dt.timedelta(days=1))
        overlap = min(high, end) - max(low, start)
        if overlap > 0:
            out.append((day, overlap / span))
        day += dt.timedelta(days=1)
    return out


def empty_bucket() -> dict[str, int]:
    return {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadInputTokens": 0,
        "cacheCreationInputTokens": 0,
    }


def clamp_token(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(number, MAX_TOKEN_VALUE))


def clean_model_name(value: Any) -> str:
    name = str(value or "unknown").strip() or "unknown"
    return name[:MAX_MODEL_NAME_LEN]


def clean_provider(value: Any) -> str:
    name = str(value or "").strip()[:32]
    return name or "local"


def clean_epoch(value: Any) -> float | None:
    """Seconds-since-epoch or None. Accepts seconds or millis; rejects NaN, negatives, far-future."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number < 0:
        return None
    if number > 4102444800000:  # past year 2100 even in millis: garbage
        return None
    if number > 4102444800:  # past 2100-01-01 in seconds: must be millis
        number /= 1000.0
    if number > dt.datetime.now().timestamp() + 86400:
        return None
    return number


def finite_cost(value):
    if type(value) not in (int, float):
        return None
    try:
        return float(value) if math.isfinite(value) and 0 <= value <= 1e12 else None
    except OverflowError:
        return None


def new_detail_bucket():
    return dict(rows=0, calls=None, unknownCallRows=0, tokens=0, reasoning=0,
                cacheRead=0, estimatedUsd=None, actualUsd=None,
                latestStatusRows={k: 0 for k in ('estimated', 'actual', 'included', 'unknown')})


def add_detail(bucket, row, total):
    bucket['rows'] += 1
    calls = row['api_call_count']
    if type(calls) is int and calls >= 0:
        bucket['calls'] = clamp_token((bucket['calls'] or 0) + calls)
    else:
        bucket['unknownCallRows'] += 1
    for key, value in (('tokens', total), ('reasoning', row['reasoning_tokens']),
                       ('cacheRead', row['cache_read_tokens'])):
        bucket[key] = clamp_token(bucket[key] + clamp_token(value))
    # Hermes UPSERT sums these independently; status only describes latest update.
    for key, column in (('estimatedUsd', 'estimated_cost_usd'), ('actualUsd', 'actual_cost_usd')):
        value = finite_cost(row[column])
        if value is not None and (value > 0 or row['cost_status'] == ('estimated' if key == 'estimatedUsd' else 'actual')):
            bucket[key] = min(1e12, (bucket[key] or 0) + value)
    status = row['cost_status']
    bucket['latestStatusRows'][status if status in ('estimated', 'actual', 'included') else 'unknown'] += 1


def add_group(groups, name, row, total, cap=32):
    name = clean_model_name(name)[:64]
    if name not in groups and len(groups) >= cap - 1:
        name = 'other'
    add_detail(groups.setdefault(name, new_detail_bucket()), row, total)


class Accumulator:
    """Everything the panel can be shown, gathered across every store."""

    def __init__(self) -> None:
        self.tokens_by_day: dict[str, float] = {}
        self.tokens_by_model: dict[str, dict[str, int]] = {}
        self.today_tokens_by_model: dict[str, float] = {}
        self.provider_tokens: dict[str, float] = {}
        self.provider_sub_tokens: dict[str, float] = {}
        self.provider_cost: dict[str, float] = {}
        self.session_days: set[str] = set()
        self.total_sessions = 0
        self.total_prompts = 0
        self.today_sessions = 0
        self.today_prompts = 0
        self.today_total_tokens = 0.0
        self.usage_rows = 0
        self.days_scanned = 0
        self.details = new_detail_bucket()
        self.task_details = {}
        self.provider_details = {}
        self.truncated = False

    def add_tokens(self, model: str, bucket: dict[str, int]) -> None:
        if model not in self.tokens_by_model and len(self.tokens_by_model) >= MAX_MODELS - 1:
            model = "other"
        target = self.tokens_by_model.setdefault(model, empty_bucket())
        for key, value in bucket.items():
            target[key] = clamp_token(target[key] + clamp_token(value))

    def add_day(self, day: dt.date, tokens: float) -> None:
        if tokens <= 0:
            return
        key = day_string(day)
        if key not in self.tokens_by_day and len(self.tokens_by_day) >= 36600:
            self.truncated = True
            return
        self.tokens_by_day[key] = self.tokens_by_day.get(key, 0.0) + tokens

    def add_today_model(self, model: str, tokens: float) -> None:
        if tokens <= 0:
            return
        if model not in self.today_tokens_by_model and len(self.today_tokens_by_model) >= MAX_MODELS - 1:
            model = "other"
        self.today_tokens_by_model[model] = self.today_tokens_by_model.get(model, 0.0) + tokens
        self.today_total_tokens += tokens


def message_day_weights(conn: sqlite3.Connection, session_ids: Iterable[str], acc=None) -> dict[str, dict[str, int]]:
    """Assistant messages per session per local day — the activity shape used
    to place a session's token counters on the calendar."""
    ids = [str(value) for value in session_ids]
    if not ids or not has_table(conn, "messages"):
        return {}
    weights: dict[str, dict[str, int]] = {}
    groups = 0
    # Chunked so a large history cannot blow past SQLite's variable limit.
    for start in range(0, len(ids), 400):
        chunk = ids[start : start + 400]
        placeholders = ",".join("?" for _ in chunk)
        try:
            rows = conn.execute(
                "SELECT session_id AS session_id,"
                " date(timestamp, 'unixepoch', 'localtime') AS day,"
                " COUNT(*) AS messages"
                f" FROM messages WHERE role = 'assistant' AND session_id IN ({placeholders})"
                f" GROUP BY session_id, day LIMIT {MAX_DAY_GROUPS + 1}",
                chunk,
            )
        except sqlite3.Error:
            if acc is not None: acc.truncated = True
            return weights
        for row in rows:
            groups += 1
            if groups > MAX_DAY_GROUPS:
                if acc is not None: acc.truncated = True
                return weights
            session = str(row["session_id"])
            day = str(row["day"] or "")
            if not day:
                continue
            bucket = weights.setdefault(session, {})
            bucket[day] = bucket.get(day, 0) + int(row["messages"] or 0)
    return weights


def scan_store(conn: sqlite3.Connection, acc: Accumulator) -> None:
    today = dt.date.today()
    today_start = day_start_epoch(today)
    horizon_start = day_start_epoch(today - dt.timedelta(days=DAY_MAP_HORIZON_DAYS))

    session_columns = columns(conn, "sessions")
    message_columns = columns(conn, "messages")

    if has_table(conn, "sessions"):
        times = [k for k in ('last_activity_at', 'ended_at', 'started_at') if k in session_columns]
        activity = ('COALESCE(' + ','.join(times) + ')') if len(times) > 1 else (times[0] if times else 'NULL')
        started = 'started_at' if 'started_at' in session_columns else 'NULL'
        if not times: acc.truncated = True
        try:
            acc.total_sessions += int(
                conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] or 0
            )
            rows = conn.execute(
                f"SELECT date({started}, 'unixepoch', 'localtime') AS day,"
                f" COUNT(*) AS n FROM sessions GROUP BY day LIMIT 732"
            )
            for row in rows:
                if row["day"]:
                    if len(acc.session_days) >= 730:
                        acc.truncated = True
                        break
                    acc.session_days.add(str(row["day"]))
            active_ids = [
                str(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM sessions WHERE {activity} >= ? LIMIT {MAX_SESSION_IDS + 1}",
                    (today_start,),
                )
            ]
            if len(active_ids) > MAX_SESSION_IDS:
                acc.truncated = True
            active_ids = active_ids[:MAX_SESSION_IDS]
            acc.today_sessions += int(
                conn.execute(
                    f"SELECT COUNT(*) AS n FROM sessions WHERE {activity} >= ?", (today_start,)
                ).fetchone()["n"]
                or 0
            )
        except sqlite3.Error:
            acc.truncated = True
            active_ids = []
    else:
        active_ids = []

    if has_table(conn, "messages"):
        compressed = "AND COALESCE(_compressed_summary, 0) = 0" if "_compressed_summary" in message_columns else ""
        try:
            acc.total_prompts += int(
                conn.execute(
                    f"SELECT COUNT(*) AS n FROM messages WHERE role = 'user' {compressed}"
                ).fetchone()["n"]
                or 0
            )
            for start in range(0, len(active_ids), 400):
                chunk = active_ids[start : start + 400]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                acc.today_prompts += int(
                    conn.execute(
                        f"SELECT COUNT(*) AS n FROM messages WHERE role = 'user' {compressed}"
                        f" AND timestamp >= ? AND session_id IN ({placeholders})",
                        [today_start, *chunk],
                    ).fetchone()["n"]
                    or 0
                )
        except sqlite3.Error:
            acc.truncated = True

    if not has_table(conn, "session_model_usage"):
        return

    usage_columns = columns(conn, 'session_model_usage')
    fields = ('session_id', 'model', 'billing_provider', 'billing_mode', 'input_tokens',
              'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens',
              'estimated_cost_usd', 'actual_cost_usd', 'cost_status', 'first_seen', 'last_seen',
              'api_call_count', 'task')
    selection = ', '.join(k if k in usage_columns else 'NULL AS ' + k for k in fields)
    try:
        cursor = conn.execute('SELECT ' + selection +
                              f' FROM session_model_usage ORDER BY last_seen DESC LIMIT {MAX_USAGE_ROWS + 1}')
    except sqlite3.Error:
        acc.truncated = True
        return
    rows = []
    seen = 0
    for row in cursor:
        seen += 1
        if seen > MAX_USAGE_ROWS:
            acc.truncated = True
            break
        rows.append(row)
    acc.usage_rows += len(rows)
    acc.days_scanned += 1

    recent_sessions = [
        str(row["session_id"])
        for row in rows
        if (clean_epoch(row["last_seen"]) or 0) >= horizon_start
    ][:MAX_SESSION_IDS]
    weights_by_session = message_day_weights(conn, recent_sessions, acc)

    for row in rows:
        model = clean_model_name(row["model"])
        input_tokens = clamp_token(row["input_tokens"])
        output_tokens = clamp_token(row["output_tokens"])
        cache_read = clamp_token(row["cache_read_tokens"])
        cache_write = clamp_token(row["cache_write_tokens"])
        bucket = {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheReadInputTokens": cache_read,
            "cacheCreationInputTokens": cache_write,
        }
        total = float(sum(bucket.values()))
        acc.add_tokens(model, bucket)
        add_detail(acc.details, row, total)
        add_group(acc.task_details, 'unknown' if row['task'] is None else (row['task'] or 'ordinary'), row, total)
        add_group(acc.provider_details, row['billing_provider'] or 'unknown', row, total)

        provider = clean_provider(row["billing_provider"] if "billing_provider" in row.keys() else "")
        if provider not in acc.provider_tokens and len(acc.provider_tokens) >= MAX_MODELS - 1:
            provider = "other"
        acc.provider_tokens[provider] = acc.provider_tokens.get(provider, 0.0) + total
        try:
            mode = str(row["billing_mode"] or "")
        except (IndexError, KeyError):
            mode = ""
        if mode == "subscription_included":
            acc.provider_sub_tokens[provider] = acc.provider_sub_tokens.get(provider, 0.0) + total
        cost = finite_cost(row["estimated_cost_usd"]) or 0.0
        acc.provider_cost[provider] = min(1e12, acc.provider_cost.get(provider, 0.0) + cost)

        session = str(row["session_id"] or "")
        weights = weights_by_session.get(session) or {}
        weight_total = sum(weights.values())

        if weight_total > 0:
            attribution = [
                (dt.date.fromtimestamp(day_start_epoch(dt.datetime.strptime(day, "%Y-%m-%d").date())), count / weight_total)
                for day, count in sorted(weights.items())
            ]
        else:
            first_seen = clean_epoch(row["first_seen"])
            last_seen = clean_epoch(row["last_seen"])
            attribution = [
                (day, weight)
                for day, weight in split_by_day(
                    first_seen,
                    last_seen,
                )
            ]

        for day, weight in attribution:
            share = total * weight
            acc.add_day(day, share)
            if day == today:
                acc.add_today_model(model, share)



def describe_plan(acc: Accumulator) -> str:
    """Historical activity is not evidence of a current subscription."""
    return "Historical provider mix" if acc.provider_tokens else HERO_LABEL


def account_snapshots(now):
    folder = hermes_home() / 'usage-export'
    return [r for p in quota_io.PROVIDERS
            if (r := quota_io.read_snapshot(folder, p, now)) is not None]


def build_record() -> dict[str, Any] | None:
    acc = Accumulator()
    stores = store_paths(acc)
    accounts = account_snapshots(dt.datetime.now().timestamp())
    if not stores and not accounts and not acc.truncated:
        return None

    scanned = 0
    for path in stores:
        try:
            conn = connect(path)
        except sqlite3.Error as error:
            acc.truncated = True
            print(f"hermes-usage: cannot read {path}: {error}", file=sys.stderr)
            continue
        try:
            scan_store(conn, acc)
            scanned += 1
        except sqlite3.Error as error:
            acc.truncated = True
            print(f"hermes-usage: skipping {path}: {error}", file=sys.stderr)
        finally:
            conn.close()

    days = recent_dates()
    recent = [
        {"date": day, "messageCount": int(round(acc.tokens_by_day.get(day, 0.0)))}
        for day in days
    ]
    active_dates = sorted(acc.session_days | set(acc.tokens_by_day))

    record: dict[str, Any] = {
        "schemaVersion": 1,
        "id": AGENT_ID,
        "name": AGENT_NAME,
        "updatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "ready": True,
        "hasLocalStats": True,
        # Hermes counts its own turns, so the panel may show prompts and
        # sessions rather than hiding them the way billing-API agents do.
        "hasPromptStats": True,
        "tierLabel": HERO_LABEL,
        # No provider usage endpoint is contacted, so there are no rate-limit
        # windows to draw. The panel skips the limits section when this is empty.
        "limits": [],
        # Kept as empty strings for shape parity with the shipped collectors:
        # a non-empty usageStatusText raises the panel's warning card, and
        # nothing here needs warning about.
        "usageStatusText": "",
        "authHelpText": "",
        "todayPrompts": acc.today_prompts,
        "todaySessions": acc.today_sessions,
        "todayTotalTokens": int(round(acc.today_total_tokens)),
        "todayTokensByModel": {
            model: int(round(tokens)) for model, tokens in acc.today_tokens_by_model.items()
        },
        "recentDays": recent,
        "totalPrompts": acc.total_prompts,
        "totalSessions": acc.total_sessions,
        "activeDays": max(len(acc.session_days), len(acc.tokens_by_day)),
        # Capped: the panel counts activeDays, while a synced snapshot uses the
        # date list, and a decade of days would only bloat the file.
        "activeDates": active_dates[-MAX_ACTIVE_DATES:],
        "modelUsage": acc.tokens_by_model,
    }
    provider_usage = {
        provider: {
            "tokens": int(round(tokens)),
            "subscriptionTokens": int(round(acc.provider_sub_tokens.get(provider, 0.0))),
            "estimatedCostUsd": round(acc.provider_cost.get(provider, 0.0), 4),
        }
        for provider, tokens in sorted(acc.provider_tokens.items(), key=lambda item: item[1], reverse=True)[:MAX_MODELS]
    }
    record["providerUsage"] = provider_usage
    record['details'] = {
        'scope': 'device', 'coverage': 'bounded local history',
        'dailyAttribution': 'estimated from assistant-message activity or row times',
        'truncated': acc.truncated, 'totals': acc.details,
        'tasks': acc.task_details, 'providers': acc.provider_details,
    }
    record["scope"] = "device"
    record["tierLabel"] = describe_plan(acc)
    record['accounts'] = accounts
    if scanned == 0:
        record['details']['totals'] = {}
        record['hasLocalStats'] = False
        record['hasPromptStats'] = False
        for key in ('todayPrompts', 'todaySessions', 'todayTotalTokens', 'todayTokensByModel',
                    'totalPrompts', 'totalSessions', 'recentDays', 'activeDays', 'activeDates', 'modelUsage'):
            record.pop(key, None)
    return record


def serialize_record(record: dict[str, Any]) -> str:
    """Serialize under MAX_RECORD_BYTES, degrading gracefully (top models win)."""
    payload = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) <= MAX_RECORD_BYTES:
        return payload
    models = record.get("modelUsage") or {}
    ranked = sorted(models.items(), key=lambda item: sum(item[1].values()), reverse=True)
    record["modelUsage"] = dict(ranked[:32])
    record["activeDates"] = (record.get("activeDates") or [])[-90:]
    record["todayTokensByModel"] = dict(
        sorted((record.get("todayTokensByModel") or {}).items(), key=lambda item: item[1], reverse=True)[:32]
    )
    payload = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) <= MAX_RECORD_BYTES:
        return payload
    record["modelUsage"] = {}
    record["todayTokensByModel"] = {}
    record["activeDates"] = []
    payload = json.dumps(record, separators=(",", ":"), allow_nan=False) + "\n"
    if len(payload.encode("utf-8")) > MAX_RECORD_BYTES:
        raise ValueError("record exceeds payload ceiling")
    return payload


def write_record(record: dict[str, Any], target_dir: Path | None = None) -> Path:
    """Atomically publish under retained, validated nofollow directory handles."""
    target = target_dir if target_dir is not None else usage_dir()
    try:
        payload = serialize_record(record).encode('utf-8')
    except ValueError as error:
        raise OSError('record exceeds payload ceiling') from error
    quota_io.atomic_write(target, AGENT_ID + '.json', payload)
    return target / (AGENT_ID + '.json')


def main(argv: list[str] | None = None) -> int:
    sys.stderr = CappedStderr(sys.stderr, MAX_STDERR_BYTES)
    parser = argparse.ArgumentParser(
        description="Print or write the Hermes Agent usage record as JSON."
    )
    parser.add_argument("--write", action="store_true", help="Write the record into the agents usage directory")
    parser.add_argument("--force", action="store_true", help="Accepted for omarchy-agent-usage-* compatibility")
    parser.add_argument("--limits-only", action="store_true", help="Accepted for omarchy-agent-usage-* compatibility")
    args = parser.parse_args(argv)

    # Cross-instance lifecycle lock is owned by the supervisor, not this worker.
    record = build_record()
    if record is None:
        print(
            "hermes-usage: no Hermes Agent session store found "
            f"(looked in {hermes_home()})",
            file=sys.stderr,
        )
        return 1

    if args.write:
        try:
            path = write_record(record)
        except OSError as error:
            print(f"hermes-usage: {error}", file=sys.stderr)
            return 1
        print(f"hermes-usage: wrote {path}", file=sys.stderr)
        return 0

    try:
        sys.stdout.write(serialize_record(record))
    except ValueError:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
