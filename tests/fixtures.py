"""Shared fixtures for the plugin test suite."""

import sqlite3
import time
from pathlib import Path


def make_store(path: Path, sessions=3, usage_per_session=2, messages_per_session=5) -> Path:
    db = path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, ended_at REAL,
            last_activity_at REAL, billing_provider TEXT, billing_mode TEXT,
            estimated_cost_usd REAL);
        CREATE TABLE session_model_usage (session_id TEXT, model TEXT,
            billing_provider TEXT NOT NULL DEFAULT '', billing_mode TEXT NOT NULL DEFAULT '',
            task TEXT NOT NULL DEFAULT '', api_call_count INTEGER NOT NULL DEFAULT 0,
            input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0, actual_cost_usd REAL NOT NULL DEFAULT 0,
            cost_status TEXT, cost_source TEXT, first_seen REAL, last_seen REAL);
        CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
            role TEXT NOT NULL, content TEXT, timestamp REAL NOT NULL);
        """
    )
    now = time.time()
    for s in range(sessions):
        sid = f"sess-{s}"
        conn.execute(
            "INSERT INTO sessions (id, started_at, ended_at, last_activity_at) VALUES (?,?,?,?)",
            (sid, now - 86400, now, now),
        )
        for u in range(usage_per_session):
            conn.execute(
                """INSERT INTO session_model_usage
                (session_id, model, billing_provider, billing_mode, input_tokens, output_tokens,
                 estimated_cost_usd, first_seen, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (sid, "test-model", "openai-codex", "subscription_included", 100, 50, 0.01, now - 3600, now),
            )
        for m in range(messages_per_session):
            role = "user" if m % 2 == 0 else "assistant"
            conn.execute(
                "INSERT INTO messages (session_id, role, timestamp) VALUES (?,?,?)",
                (sid, role, now - 100 + m),
            )
    conn.commit()
    conn.close()
    return db
