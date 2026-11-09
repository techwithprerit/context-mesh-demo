"""logs-node: one MCP face in front of the log store (think Loki/Elastic). Reads `logs` only."""
from __future__ import annotations

import threading
import time

from mcp.server.mcpserver import MCPServer

from .. import telemetry
from ..bus import Bus
from ..common import READ_ONLY, now, run_server

server = MCPServer("logs-node", instructions="Search and tail log lines; error bursts are published to the bus.")
conn = telemetry.connect()
bus = Bus()
WATCH_WORDS = ("failover", "deadlock", "OOM", "deploy")


def _rows(sql: str, args: tuple) -> list[dict]:
    cur = conn.execute(sql, args)
    return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]


@server.tool(annotations=READ_ONLY)
def errors(service: str | None = None, window_s: int = 300, limit: int = 10) -> dict:
    """Most recent ERROR lines, optionally for one service, newest first, with their trace ids."""
    q = "SELECT ts, service, level, msg, trace_id FROM logs WHERE level='ERROR' AND ts>?"
    args: tuple = (now() - window_s,)
    if service:
        q, args = q + " AND service=?", args + (service,)
    rows = _rows(q + " ORDER BY ts DESC LIMIT ?", args + (limit,))
    total = conn.execute("SELECT COUNT(*) FROM logs WHERE level='ERROR' AND ts>?" + (" AND service=?" if service else ""),
                         args).fetchone()[0]
    return {"service": service, "window_s": window_s, "total_errors": total, "lines": rows, "source": "logs-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def search(pattern: str, window_s: int = 600, limit: int = 20) -> dict:
    """Case-insensitive substring search over recent log lines."""
    rows = _rows("SELECT ts, service, level, msg, trace_id FROM logs WHERE ts>? AND msg LIKE ? ORDER BY ts DESC LIMIT ?",
                 (now() - window_s, f"%{pattern}%", limit))
    return {"pattern": pattern, "lines": rows, "source": "logs-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def for_trace(trace_id: str) -> dict:
    """Every log line that carries this trace id (the log <-> trace join)."""
    rows = _rows("SELECT ts, service, level, msg FROM logs WHERE trace_id=? ORDER BY ts", (trace_id,))
    return {"trace_id": trace_id, "lines": rows, "source": "logs-node", "ts": now()}


# -- background: bursts and watch-words ------------------------------------------
_last_burst: dict[str, float] = {}
_seen_ts = now()
_seen_lines: set[tuple] = set()


def watch() -> None:
    global _seen_ts
    while True:
        try:
            t = now()
            for svc in ("web", "orders", "payments"):
                cur = conn.execute("SELECT COUNT(*) FROM logs WHERE level='ERROR' AND service=? AND ts>?", (svc, t - 60)).fetchone()[0]
                prev = conn.execute("SELECT COUNT(*) FROM logs WHERE level='ERROR' AND service=? AND ts BETWEEN ? AND ?",
                                    (svc, t - 120, t - 60)).fetchone()[0]
                if cur < 3:
                    _last_burst.pop(svc, None)  # quiet again: the next burst is news
                if cur >= 5 and cur >= 3 * max(prev, 1) and t - _last_burst.get(svc, 0) > 300:
                    sample = errors(svc, 60, 3)["lines"]
                    bus.publish("context.logs.error_burst", source="logs-node", service=svc, count_1m=cur, previous_1m=prev,
                                sample=sample[0]["msg"] if sample else "", trace_ids=[s["trace_id"] for s in sample if s["trace_id"]])
                    _last_burst[svc] = t
                    print(f"[logs-node] -> context.logs.error_burst {svc} {cur}/min", flush=True)
            for word in WATCH_WORDS:
                for r in _rows("SELECT ts, service, msg FROM logs WHERE ts>? AND msg LIKE ? ORDER BY ts", (_seen_ts - 3, f"%{word}%")):
                    key = (round(r["ts"], 3), r["service"], r["msg"])
                    if key in _seen_lines:
                        continue  # the 3 s overlap catches rows committed after the poll; the set stops repeats
                    _seen_lines.add(key)
                    bus.publish("context.logs.keyword", source="logs-node", keyword=word, service=r["service"], msg=r["msg"], ts=r["ts"])
                    print(f"[logs-node] -> context.logs.keyword {word} ({r['service']})", flush=True)
            if len(_seen_lines) > 2000:
                _seen_lines.clear()
            _seen_ts = t
        except Exception as e:
            print(f"[logs-node] watch error: {e}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=watch, daemon=True).start()
    run_server(server, "logs")
