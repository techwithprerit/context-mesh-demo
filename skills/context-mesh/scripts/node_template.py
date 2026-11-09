"""A complete context-mesh node, ready to copy.

One store, three question-shaped tools, one resource, a background loop that publishes context
events on the rising edge, and read-only annotations on everything. Replace the three `fetch_*`
helpers with calls to your backend (see references/backends.md); leave the rest.

Run:  python node_template.py            (streamable HTTP on PORT, default 8101)
Test: python -c "import asyncio; from mcp.client.client import Client
      async def m():
        async with Client('http://127.0.0.1:8101/mcp') as c: print(await c.call_tool('error_rate', {'service':'orders'}))
      asyncio.run(m())"

Requires: mcp>=2 (for mcp<2: `from mcp.server.fastmcp import FastMCP as MCPServer` and camelCase annotation names).
"""
from __future__ import annotations

import json
import os
import threading
import time

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

NODE = os.environ.get("NODE_NAME", "metrics-node")
PORT = int(os.environ.get("PORT", "8101"))
BUS_FILE = os.environ.get("BUS_URL", "file://./data/bus.jsonl").replace("file://", "", 1)
SERVICES = os.environ.get("WATCH_SERVICES", "web,orders,payments").split(",")

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
server = MCPServer(NODE, instructions="Rate, errors and duration per service. Read-only. Publishes context events.")


def now() -> float:
    return time.time()


# -- 1. the three helpers that touch the store: replace these ------------------------------------
def fetch_counts(service: str, window_s: int) -> tuple[int, int]:
    """(requests, errors) in the window. Prometheus: sum(increase(http_requests_total{service=$s}[$w]))."""
    return 1000, 3


def fetch_durations(service: str, window_s: int) -> list[float]:
    """Request durations in ms. Prometheus: histogram_quantile over the bucket rate; return [p50, p99] then."""
    return [42.0] * 99 + [180.0]


def fetch_gauge(name: str, label: str, value: str, window_s: int) -> list[tuple[float, float]]:
    """(ts, value) samples for a gauge in the window, oldest first."""
    t = now()
    return [(t - window_s, 0.31), (t, 0.33)]


def percentile(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, round(p * (len(s) - 1))))]


# -- 2. tools: questions, with a result envelope (source, ts) -------------------------------------
@server.tool(annotations=READ_ONLY)
def error_rate(service: str, window_s: int = 300) -> dict:
    """errors / requests for a service over the window (the E in RED), as a ratio."""
    req, err = fetch_counts(service, window_s)
    return {"service": service, "window_s": window_s, "requests": req, "errors": err,
            "error_rate": round(err / req, 4) if req else 0.0, "source": NODE, "ts": now()}


@server.tool(annotations=READ_ONLY)
def p99(service: str, window_s: int = 300) -> dict:
    """p50 and p99 request duration in ms for a service over the window (the D in RED)."""
    d = fetch_durations(service, window_s)
    return {"service": service, "window_s": window_s, "samples": len(d),
            "p50_ms": round(percentile(d, 0.5), 1), "p99_ms": round(percentile(d, 0.99), 1), "source": NODE, "ts": now()}


@server.tool(annotations=READ_ONLY)
def gauge(name: str, label: str, value: str, window_s: int = 30) -> dict:
    """Latest value and short trend (rising|falling|flat) of a gauge, e.g. gauge('node_cpu_ratio','instance','db-17')."""
    pts = fetch_gauge(name, label, value, window_s)
    if not pts:
        return {"name": name, label: value, "value": None, "trend": "unknown", "source": NODE, "ts": now()}
    first, last = pts[0][1], pts[-1][1]
    trend = "rising" if last > first * 1.05 + 0.5 else "falling" if last < first * 0.95 - 0.5 else "flat"
    return {"name": name, label: value, "value": round(last, 3), "trend": trend, "samples": len(pts), "source": NODE, "ts": pts[-1][0]}


@server.resource("dashboard://{service}")
def dashboard(service: str) -> str:
    """Where a human would look next, as text."""
    return f"{service}: RED dashboard -> https://grafana.example/d/red?var-service={service}"


# -- 3. the bus and the rising-edge rule ------------------------------------------------------------
_lock = threading.Lock()
_state: dict[str, bool] = {}


def publish(topic: str, **payload) -> None:
    ev = {"topic": topic, "source": NODE, "ts": now(), **payload}
    os.makedirs(os.path.dirname(BUS_FILE) or ".", exist_ok=True)
    with _lock, open(BUS_FILE, "a") as f:          # swap for redis.publish / nats.publish; keep the signature
        f.write(json.dumps(ev) + "\n")
    print(f"[{NODE}] -> {topic} {payload}", flush=True)


def edge(key: str, firing: bool, topic: str, **payload) -> None:
    was = _state.get(key, False)
    if firing and not was:
        publish(topic, **payload)
    elif was and not firing:
        publish(topic + ".cleared", **payload)
    _state[key] = firing


# -- 4. the loop that makes it a signal processor, not a query proxy ------------------------------
ERROR_LINE, P99_LINE_MS, MIN_REQUESTS = 0.02, 800, 50


def watch() -> None:
    while True:
        try:
            for svc in SERVICES:
                er = error_rate(svc, 120)
                edge(f"er:{svc}", er["error_rate"] > ERROR_LINE and er["requests"] >= MIN_REQUESTS,
                     "context.metrics.error_rate", service=svc, value=er["error_rate"], window="2m")
                lat = p99(svc, 120)
                edge(f"p99:{svc}", lat["p99_ms"] > P99_LINE_MS, "context.metrics.latency", service=svc, p99_ms=lat["p99_ms"])
        except Exception as e:  # the loop must outlive a bad poll
            print(f"[{NODE}] watch error: {e}", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    threading.Thread(target=watch, daemon=True).start()
    server.run(transport="streamable-http", host="0.0.0.0", port=PORT,
               transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
