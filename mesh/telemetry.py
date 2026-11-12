"""The three signal stores, standing in for Loki / Prometheus / Tempo.

One SQLite file, three tables. Each node reads exactly ONE table - that is the
"one signal store, never two" rule from the talk, enforced by habit rather than
by the database.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

DB = os.environ.get("TELEMETRY_DB", "./data/telemetry.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans(ts REAL, trace_id TEXT, span_id TEXT, parent_id TEXT, service TEXT,
                                 op TEXT, peer TEXT, duration_ms REAL, status TEXT);
CREATE INDEX IF NOT EXISTS spans_ts ON spans(ts);
CREATE INDEX IF NOT EXISTS spans_trace ON spans(trace_id);
CREATE TABLE IF NOT EXISTS logs(ts REAL, service TEXT, level TEXT, msg TEXT, trace_id TEXT);
CREATE INDEX IF NOT EXISTS logs_ts ON logs(ts);
CREATE TABLE IF NOT EXISTS metrics(ts REAL, name TEXT, labels TEXT, value REAL);
CREATE INDEX IF NOT EXISTS metrics_name_ts ON metrics(name, ts);
"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB) or ".", exist_ok=True)
    conn = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


class _PerThread:
    """A connection per thread. MCP runs sync tools in worker threads next to each node's watcher, and one
    shared sqlite3 connection under concurrent calls can hand a query an empty cursor."""

    def __init__(self):
        self._local = threading.local()

    def __getattr__(self, name):
        if not hasattr(self._local, "conn"):
            self._local.conn = connect()
        return getattr(self._local.conn, name)


def reader() -> _PerThread:
    """For the nodes: reads from any thread. The estate, the single writer, keeps one connect()."""
    return _PerThread()


def prune(conn: sqlite3.Connection, keep_s: int = 1800) -> None:
    cutoff = time.time() - keep_s
    for t in ("spans", "logs", "metrics"):
        conn.execute(f"DELETE FROM {t} WHERE ts < ?", (cutoff,))
    conn.commit()


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
    return s[k]
