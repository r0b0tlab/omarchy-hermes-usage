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
dropped into `omarchy-agent-usage-<agent>` slots unchanged; Hermes has no
provider limits endpoint to query, so there is nothing to force or limit.

Exit codes: 0 with a record printed/written, 1 when no Hermes store was found
(writes nothing, so the panel does not grow an empty tab).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Iterable

AGENT_ID = "hermes"
AGENT_NAME = "Hermes Agent"
WEEK_DAYS = 7
# Only usage rows touched inside this window get day-by-day attribution from
# message timestamps; older rows are attributed to the day they were last
# recorded. Their tokens still count toward all-time totals either way.
DAY_MAP_HORIZON_DAYS = 120
# Hermes bills across whatever providers are configured, often several at once,
# so the panel's single plan line cannot name one of them truthfully. It says
# what the panel is showing instead.
HERO_LABEL = "Usage breakdown"


def expand(value: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(value)))


def hermes_home() -> Path:
    return expand(os.environ.get("HERMES_HOME") or "~/.hermes")


def state_home() -> Path:
    return expand(os.environ.get("XDG_STATE_HOME") or "~/.local/state")


def usage_dir() -> Path:
    return state_home() / "omarchy" / "agents" / "usage"


def store_paths() -> list[Path]:
    """Every Hermes session store on this machine, primary profile first."""
    home = hermes_home()
    candidates = [home / "state.db"]
    profiles = home / "profiles"
    if profiles.is_dir():
        try:
            candidates.extend(sorted(p / "state.db" for p in profiles.iterdir() if p.is_dir()))
        except OSError:
            pass
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
    conn.row_factory = sqlite3.Row
    return conn


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def has_table(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
    except sqlite3.Error:
        return False
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


class Accumulator:
    """Everything the panel can be shown, gathered across every store."""

    def __init__(self) -> None:
        self.tokens_by_day: dict[str, float] = {}
        self.tokens_by_model: dict[str, dict[str, int]] = {}
        self.today_tokens_by_model: dict[str, float] = {}
        self.session_days: set[str] = set()
        self.total_sessions = 0
        self.total_prompts = 0
        self.today_sessions = 0
        self.today_prompts = 0
        self.today_total_tokens = 0.0
        self.usage_rows = 0
        self.days_scanned = 0

    def add_tokens(self, model: str, bucket: dict[str, int]) -> None:
        target = self.tokens_by_model.setdefault(model, empty_bucket())
        for key, value in bucket.items():
            target[key] += int(value)

    def add_day(self, day: dt.date, tokens: float) -> None:
        if tokens <= 0:
            return
        key = day_string(day)
        self.tokens_by_day[key] = self.tokens_by_day.get(key, 0.0) + tokens

    def add_today_model(self, model: str, tokens: float) -> None:
        if tokens <= 0:
            return
        self.today_tokens_by_model[model] = self.today_tokens_by_model.get(model, 0.0) + tokens
        self.today_total_tokens += tokens


def message_day_weights(conn: sqlite3.Connection, session_ids: Iterable[str]) -> dict[str, dict[str, int]]:
    """Assistant messages per session per local day — the activity shape used
    to place a session's token counters on the calendar."""
    ids = [str(value) for value in session_ids]
    if not ids or not has_table(conn, "messages"):
        return {}
    weights: dict[str, dict[str, int]] = {}
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
                " GROUP BY session_id, day",
                chunk,
            )
        except sqlite3.Error:
            return weights
        for row in rows:
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
        activity = "COALESCE(last_activity_at, ended_at, started_at)"
        if "last_activity_at" not in session_columns:
            activity = "COALESCE(ended_at, started_at)"
        try:
            acc.total_sessions += int(
                conn.execute("SELECT COUNT(*) AS n FROM sessions").fetchone()["n"] or 0
            )
            rows = conn.execute(
                f"SELECT date(started_at, 'unixepoch', 'localtime') AS day,"
                f" COUNT(*) AS n FROM sessions GROUP BY day"
            )
            for row in rows:
                if row["day"]:
                    acc.session_days.add(str(row["day"]))
            active_ids = [
                str(row["id"])
                for row in conn.execute(
                    f"SELECT id FROM sessions WHERE {activity} >= ?", (today_start,)
                )
            ]
            acc.today_sessions += len(active_ids)
        except sqlite3.Error:
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
            pass

    if not has_table(conn, "session_model_usage"):
        return

    rows = list(
        conn.execute(
            "SELECT session_id, model,"
            " input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
            " reasoning_tokens, first_seen, last_seen"
            " FROM session_model_usage"
        )
    )
    acc.usage_rows += len(rows)
    acc.days_scanned += 1

    recent_sessions = [
        str(row["session_id"])
        for row in rows
        if float(row["last_seen"] or 0) >= horizon_start
    ]
    weights_by_session = message_day_weights(conn, recent_sessions)

    for row in rows:
        model = str(row["model"] or "unknown")
        input_tokens = int(row["input_tokens"] or 0)
        output_tokens = int(row["output_tokens"] or 0) + int(row["reasoning_tokens"] or 0)
        cache_read = int(row["cache_read_tokens"] or 0)
        cache_write = int(row["cache_write_tokens"] or 0)
        bucket = {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "cacheReadInputTokens": cache_read,
            "cacheCreationInputTokens": cache_write,
        }
        total = float(sum(bucket.values()))
        acc.add_tokens(model, bucket)

        session = str(row["session_id"] or "")
        weights = weights_by_session.get(session) or {}
        weight_total = sum(weights.values())

        if weight_total > 0:
            attribution = [
                (dt.date.fromtimestamp(day_start_epoch(dt.datetime.strptime(day, "%Y-%m-%d").date())), count / weight_total)
                for day, count in sorted(weights.items())
            ]
        else:
            first_seen = row["first_seen"]
            last_seen = row["last_seen"]
            attribution = [
                (day, weight)
                for day, weight in split_by_day(
                    float(first_seen) if first_seen else None,
                    float(last_seen) if last_seen else None,
                )
            ]

        for day, weight in attribution:
            share = total * weight
            acc.add_day(day, share)
            if day == today:
                acc.add_today_model(model, share)



def build_record() -> dict[str, Any] | None:
    stores = store_paths()
    if not stores:
        return None

    acc = Accumulator()
    scanned = 0
    for path in stores:
        try:
            conn = connect(path)
        except sqlite3.Error as error:
            print(f"hermes-usage: cannot read {path}: {error}", file=sys.stderr)
            continue
        try:
            scan_store(conn, acc)
            scanned += 1
        except sqlite3.Error as error:
            print(f"hermes-usage: skipping {path}: {error}", file=sys.stderr)
        finally:
            conn.close()

    if scanned == 0:
        return None

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
        "activeDates": active_dates[-365:],
        "modelUsage": acc.tokens_by_model,
    }
    return record


def write_record(record: dict[str, Any]) -> Path:
    """Atomic replace, mirroring what omarchy-agent-usage-update does."""
    target = usage_dir()
    target.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, separators=(",", ":")) + "\n"
    handle, temporary = tempfile.mkstemp(prefix=f".{AGENT_ID}.", dir=str(target))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(payload)
        os.replace(temporary, target / f"{AGENT_ID}.json")
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return target / f"{AGENT_ID}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Print or write the Hermes Agent usage record as JSON."
    )
    parser.add_argument("--write", action="store_true", help="Write the record into the agents usage directory")
    parser.add_argument("--force", action="store_true", help="Accepted for omarchy-agent-usage-* compatibility")
    parser.add_argument("--limits-only", action="store_true", help="Accepted for omarchy-agent-usage-* compatibility")
    args = parser.parse_args(argv)

    record = build_record()
    if record is None:
        print(
            "hermes-usage: no Hermes Agent session store found "
            f"(looked in {hermes_home()})",
            file=sys.stderr,
        )
        return 1

    if args.write:
        path = write_record(record)
        print(f"hermes-usage: wrote {path}", file=sys.stderr)
        return 0

    json.dump(record, sys.stdout, separators=(",", ":"))
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
