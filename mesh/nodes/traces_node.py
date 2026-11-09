"""traces-node: one MCP face in front of the trace store (think Tempo/Jaeger). Reads `spans` only."""
from __future__ import annotations

import threading
import time

from mcp.server.mcpserver import MCPServer

from .. import telemetry
from ..bus import Bus
from ..common import READ_ONLY, now, run_server

server = MCPServer("traces-node", instructions="Slow traces, critical paths and per-edge latency. Read-only.")
conn = telemetry.connect()
bus = Bus()


def _rows(sql: str, args: tuple) -> list[dict]:
    cur = conn.execute(sql, args)
    return [dict(zip([c[0] for c in cur.description], r)) for r in cur.fetchall()]


@server.tool(annotations=READ_ONLY)
def trace(trace_id: str) -> dict:
    """All spans of one trace, in start order, plus the critical path (longest child chain)."""
    spans = _rows("SELECT ts, span_id, parent_id, service, op, peer, duration_ms, status FROM spans WHERE trace_id=? ORDER BY ts",
                  (trace_id,))
    by_parent: dict[str | None, list[dict]] = {}
    for s in spans:
        by_parent.setdefault(s["parent_id"], []).append(s)

    def path(node: dict) -> list[dict]:
        kids = by_parent.get(node["span_id"], [])
        if not kids:
            return [node]
        return [node] + path(max(kids, key=lambda k: k["duration_ms"]))

    root = by_parent.get(None, [None])[0]
    cp = [{"service": s["service"], "op": s["op"], "ms": round(s["duration_ms"], 1), "status": s["status"]} for s in path(root)] if root else []
    return {"trace_id": trace_id, "spans": spans, "critical_path": cp,
            "duration_ms": round(root["duration_ms"], 1) if root else None, "source": "traces-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def slowest(service: str, window_s: int = 300, limit: int = 3) -> dict:
    """The slowest traces that pass through a service, each with its critical path."""
    rows = _rows("SELECT trace_id, MAX(duration_ms) AS d FROM spans WHERE service=? AND parent_id IS NOT NULL AND ts>? "
                 "GROUP BY trace_id ORDER BY d DESC LIMIT ?", (service, now() - window_s, limit))
    if service == "web":
        rows = _rows("SELECT trace_id, MAX(duration_ms) AS d FROM spans WHERE service='web' AND ts>? GROUP BY trace_id ORDER BY d DESC LIMIT ?",
                     (now() - window_s, limit))
    return {"service": service, "traces": [trace(r["trace_id"]) for r in rows], "source": "traces-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def edge_latency(window_s: int = 300) -> dict:
    """RED per call edge (caller -> callee), measured from spans: rate, error ratio, p50, p99."""
    rows = _rows("SELECT service, peer, duration_ms, status FROM spans WHERE ts>? AND peer IS NOT NULL", (now() - window_s,))
    acc: dict[tuple, list] = {}
    for r in rows:
        acc.setdefault((r["service"], r["peer"]), []).append(r)
    edges = []
    for (a, b), rs in acc.items():
        d = [x["duration_ms"] for x in rs]
        edges.append({"from": a, "to": b, "rate_per_s": round(len(rs) / window_s, 2),
                      "error_ratio": round(sum(1 for x in rs if x["status"] == "error") / len(rs), 4),
                      "p50_ms": round(telemetry.percentile(d, 0.5), 1), "p99_ms": round(telemetry.percentile(d, 0.99), 1)})
    return {"window_s": window_s, "edges": edges, "source": "traces-node", "ts": now()}


# -- background: an edge whose p99 leaves its baseline is context, not an alert ------
_baseline: dict[tuple, float] = {}
_fired: dict[tuple, float] = {}


def watch() -> None:
    time.sleep(8)  # let the estate warm up so the baseline is a healthy one (the tools answer meanwhile)
    while True:
        try:
            for e in edge_latency(60)["edges"]:
                key = (e["from"], e["to"])
                if key not in _baseline and e["error_ratio"] > 0:
                    continue  # a baseline is learned from a healthy minute only, never from a broken one
                base = _baseline.setdefault(key, e["p99_ms"])
                if e["p99_ms"] > max(2.5 * base, base + 100) and now() - _fired.get(key, 0) > 300:
                    sample = slowest(e["to"] if e["to"] in ("orders", "payments") else e["from"], 60, 1)["traces"]
                    bus.publish("context.traces.slow_edge", source="traces-node", **{"from": key[0], "to": key[1]},
                                p99_ms=e["p99_ms"], baseline_ms=round(base, 1),
                                sample_trace=sample[0]["trace_id"] if sample else None)
                    _fired[key] = now()
                    print(f"[traces-node] -> context.traces.slow_edge {key} p99={e['p99_ms']} base={base:.0f}", flush=True)
                elif e["p99_ms"] < 1.5 * base:
                    _baseline[key] = 0.9 * base + 0.1 * e["p99_ms"]  # slow EWMA while healthy
                    _fired.pop(key, None)  # healthy again: the next excursion is news, cooldown or not
        except Exception as ex:
            print(f"[traces-node] watch error: {ex}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=watch, daemon=True).start()
    run_server(server, "traces")
