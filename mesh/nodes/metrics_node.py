"""metrics-node: one MCP face in front of the metrics store (think Prometheus/Mimir).

Reads the `metrics` table only. Exposes a handful of question-shaped tools and, in the
background, publishes context events to the bus when a value crosses a line.
"""
from __future__ import annotations

import json
import threading
import time

from mcp.server.mcpserver import MCPServer

from .. import telemetry
from ..bus import Bus
from ..common import READ_ONLY, now, run_server

server = MCPServer("metrics-node", instructions="Rate, errors, duration and gauges for the estate. Read-only.")
conn = telemetry.reader()
bus = Bus()
SERVICES = ("web", "orders", "payments")


def _window(window_s: int) -> float:
    return now() - window_s


def _sum(name: str, labels: dict, window_s: int) -> float:
    row = conn.execute("SELECT COALESCE(SUM(value),0) FROM metrics WHERE name=? AND labels=? AND ts>?",
                       (name, json.dumps(labels), _window(window_s))).fetchone()
    return float(row[0])


def _values(name: str, labels: dict, window_s: int) -> list[float]:
    rows = conn.execute("SELECT value FROM metrics WHERE name=? AND labels=? AND ts>?",
                        (name, json.dumps(labels), _window(window_s))).fetchall()
    return [r[0] for r in rows]


@server.tool(annotations=READ_ONLY)
def rate(service: str, window_s: int = 300) -> dict:
    """Requests per second for a service over the window (the R in RED)."""
    return {"service": service, "window_s": window_s,
            "rate_per_s": round(_sum("http_requests_total", {"service": service}, window_s) / window_s, 2),
            "source": "metrics-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def error_rate(service: str, window_s: int = 300) -> dict:
    """errors / requests for a service over the window (the E in RED), as a ratio."""
    req = _sum("http_requests_total", {"service": service}, window_s)
    err = _sum("http_errors_total", {"service": service}, window_s)
    return {"service": service, "window_s": window_s, "requests": int(req), "errors": int(err),
            "error_rate": round(err / req, 4) if req else 0.0, "source": "metrics-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def p99(service: str, window_s: int = 300) -> dict:
    """p99 request duration in ms for a service over the window (the D in RED)."""
    vals = _values("http_request_duration_ms", {"service": service}, window_s)
    return {"service": service, "window_s": window_s, "samples": len(vals),
            "p50_ms": round(telemetry.percentile(vals, 0.5), 1), "p99_ms": round(telemetry.percentile(vals, 0.99), 1),
            "source": "metrics-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def gauge(name: str, label: str, value: str, window_s: int = 30) -> dict:
    """Latest value and short trend of a gauge, e.g. gauge('node_cpu_ratio','instance','db-prod-17')."""
    rows = conn.execute("SELECT ts, value FROM metrics WHERE name=? AND labels=? AND ts>? ORDER BY ts",
                        (name, json.dumps({label: value}), _window(window_s))).fetchall()
    if not rows:
        return {"name": name, label: value, "value": None, "trend": "unknown", "source": "metrics-node", "ts": now()}
    tail = [v for _, v in rows[-4:]]  # trend from the last few samples, not the whole window
    first, last = tail[0], tail[-1]
    trend = "rising" if last > first * 1.05 + 0.5 else "falling" if last < first * 0.95 - 0.5 else "flat"
    return {"name": name, label: value, "value": round(last, 3), "trend": trend, "samples": len(rows),
            "source": "metrics-node", "ts": rows[-1][0]}


@server.tool(annotations=READ_ONLY)
def series(name: str, label: str, value: str, window_s: int = 600, step_s: int = 30) -> dict:
    """A downsampled series (mean per step) for sparklines and burn-rate windows."""
    rows = conn.execute("SELECT ts, value FROM metrics WHERE name=? AND labels=? AND ts>? ORDER BY ts",
                        (name, json.dumps({label: value}), _window(window_s))).fetchall()
    buckets: dict[int, list[float]] = {}
    for ts, v in rows:
        buckets.setdefault(int(ts // step_s), []).append(v)
    return {"name": name, "points": [[k * step_s, round(sum(v) / len(v), 3)] for k, v in sorted(buckets.items())],
            "source": "metrics-node", "ts": now()}


# -- the part that makes it a mesh, not a chatbot -------------------------------
_state: dict[str, bool] = {}


def _edge(key: str, firing: bool, topic: str, **payload) -> None:
    """Publish on the rising edge only; publish a 'cleared' event on the falling edge."""
    was = _state.get(key, False)
    if firing and not was:
        bus.publish(topic, source="metrics-node", **payload)
        print(f"[metrics-node] -> {topic} {payload}", flush=True)
    elif was and not firing:
        bus.publish(topic + ".cleared", source="metrics-node", **payload)
    _state[key] = firing


def watch() -> None:
    while True:
        try:
            for svc in SERVICES:
                er = error_rate(svc, 120)
                _edge(f"er:{svc}", er["error_rate"] > 0.02 and er["requests"] > 50, "context.metrics.error_rate",
                      service=svc, value=er["error_rate"], window="2m")
                lat = p99(svc, 120)
                _edge(f"p99:{svc}", lat["p99_ms"] > 800, "context.metrics.latency", service=svc, p99_ms=lat["p99_ms"])
            cpu = gauge("node_cpu_ratio", "instance", "db-prod-17", 20)
            _edge("cpu:db-prod-17", (cpu["value"] or 0) > 0.85, "context.metrics.cpu_high",
                  instance="db-prod-17", value=cpu["value"])
            lag = gauge("pg_replica_lag_seconds", "host", "db-prod-17", 20)
            _edge("lag:db-prod-17", (lag["value"] or 0) > 30, "context.metrics.replica_lag",
                  host="db-prod-17", value=lag["value"], trend=lag["trend"])
        except Exception as e:
            print(f"[metrics-node] watch error: {e}", flush=True)
        time.sleep(2)  # 2 s poll: rising edges reach the bus sooner on stage


if __name__ == "__main__":
    threading.Thread(target=watch, daemon=True).start()
    run_server(server, "metrics")
