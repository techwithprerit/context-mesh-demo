"""Shared plumbing: ports, MCP client helper, server runner, time helpers."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from mcp.client.client import Client
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

PORTS = {"graph": 8100, "metrics": 8101, "logs": 8102, "traces": 8103, "topology": 8104, "policy": 8105}
DATA = os.environ.get("MESH_DATA", "./data")
CHAOS_FILE = os.path.join(DATA, "chaos.json")
DEPLOYS_FILE = os.path.join(DATA, "deploys.json")
FREEZE_FILE = os.path.join(DATA, "freeze.json")
HERE = os.path.dirname(os.path.abspath(__file__))
TOPOLOGY_FILE = os.environ.get("TOPOLOGY_FILE", os.path.join(HERE, "topology.yaml"))
POLICY_FILE = os.environ.get("POLICY_FILE", os.path.join(HERE, "policy.yaml"))

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def now() -> float:
    return time.time()


def age(ts: float | None) -> str:
    if ts is None:
        return "n/a"
    s = int(now() - ts)
    return f"{s}s ago" if s < 120 else f"{s // 60}m ago"


def node_url(name: str) -> str:
    host = os.environ.get(f"{name.upper()}_HOST") or os.environ.get("MESH_HOST", "127.0.0.1")
    return f"http://{host}:{PORTS[name]}/mcp"


def _parse(result) -> Any:
    if getattr(result, "is_error", False):
        raise RuntimeError("tool error: " + "".join(getattr(c, "text", "") for c in result.content)[:300])
    if result.structured_content:
        return result.structured_content
    text = "".join(getattr(c, "text", "") for c in result.content)
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text


async def call(_node: str, _tool: str, **args) -> Any:
    """Call one tool on one MCP node and return its decoded result (positional names are underscored so tools may take node=/tool=)."""
    try:
        async with Client(node_url(_node)) as c:
            result = await c.call_tool(_tool, args)
    except Exception as e:  # name the node that did not answer, not the TaskGroup that wrapped it
        while isinstance(e, BaseExceptionGroup) and e.exceptions:
            e = e.exceptions[0]
        raise RuntimeError(f"{_node}-node did not answer {_tool}: {type(e).__name__}") from e
    return _parse(result)


async def list_tools(node: str) -> list[dict]:
    async with Client(node_url(node)) as c:
        r = await c.list_tools()
        return [
            {
                "name": t.name,
                "description": (t.description or "").strip().split("\n")[0],
                "read_only": bool(t.annotations and t.annotations.read_only_hint),
            }
            for t in r.tools
        ]


def call_sync(_node: str, _tool: str, **args) -> Any:
    return asyncio.run(call(_node, _tool, **args))


def run_server(server, name: str) -> None:
    """Serve an MCP node over streamable HTTP on its well-known port."""
    server.run(
        transport="streamable-http",
        host="0.0.0.0",
        port=PORTS[name],
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def read_json(path: str, default: Any) -> Any:
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
