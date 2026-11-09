"""topology-node: who calls whom, on what, owned by whom, moving in which wave.

The only node with a WRITE tool (record_deploy). It is annotated readOnlyHint=false and
refuses to run without an approver - the "a write is a proposal until a human approves it" rule.
"""
from __future__ import annotations

import os
import threading
import time

import yaml
from mcp.server.mcpserver import MCPServer

from ..bus import Bus
from ..common import DEPLOYS_FILE, READ_ONLY, TOPOLOGY_FILE, WRITE, now, read_json, run_server, write_json

server = MCPServer("topology-node", instructions="Service map, owners, waves, dependents, deploys.")
bus = Bus()
TOPO = yaml.safe_load(open(TOPOLOGY_FILE))


def _kind(name: str) -> str:
    for k in ("services", "databases", "caches", "topics", "hosts"):
        if name in TOPO.get(k, {}):
            return k[:-1]
    return "unknown"


@server.tool(annotations=READ_ONLY)
def map() -> dict:  # noqa: A001 - 'map' is the obvious name for the whole service map
    """The whole service map: services, databases, caches, topics, hosts and typed edges."""
    return {**TOPO, "source": "topology-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def neighbours(service: str) -> dict:
    """Upstream callers and downstream callees of one service, with the edge type."""
    up = [e for e in TOPO["edges"] if e["to"] == service]
    down = [e for e in TOPO["edges"] if e["from"] == service]
    return {"service": service, "kind": _kind(service), "upstream": up, "downstream": down, "source": "topology-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def dependents(node: str, depth: int = 3) -> dict:
    """Everything that (transitively) depends on a node: services, with their waves and owners."""
    seen: list[str] = []
    frontier = [node]
    for _ in range(depth):
        nxt = [e["from"] for e in TOPO["edges"] if e["to"] in frontier and e["from"] not in seen]
        for t, meta in TOPO.get("topics", {}).items():
            if t in frontier:
                nxt += [c for c in meta["consumers"] if c not in seen]
        seen += [n for n in dict.fromkeys(nxt)]
        frontier = nxt
        if not frontier:
            break
    svc = TOPO["services"]
    return {"node": node, "dependents": [{"service": s, "wave": svc.get(s, {}).get("wave"), "owner": svc.get(s, {}).get("owner")} for s in seen],
            "source": "topology-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def owner(name: str) -> dict:
    """Owner, wave and tier of a service (or the host/engine of a database)."""
    meta = TOPO["services"].get(name) or TOPO["databases"].get(name) or TOPO["caches"].get(name) or {}
    return {"name": name, "kind": _kind(name), **meta, "source": "topology-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def host_of(database: str) -> dict:
    """Which host a database lives on, and that host's replica."""
    db = TOPO["databases"].get(database, {})
    host = TOPO["hosts"].get(db.get("host"), {})
    return {"database": database, "host": db.get("host"), **host, "source": "topology-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def deploys(service: str | None = None, window_s: int = 3600) -> dict:
    """Recent deploys, newest first."""
    ds = [d for d in read_json(DEPLOYS_FILE, []) if d["ts"] > now() - window_s and (service is None or d["service"] == service)]
    return {"service": service, "deploys": sorted(ds, key=lambda d: -d["ts"]), "source": "topology-node", "ts": now()}


@server.tool(annotations=WRITE)
def record_deploy(service: str, version: str, approved_by: str = "") -> dict:
    """WRITE: record a deploy of a service. Refuses without approved_by - a human stays in the loop."""
    if not approved_by:
        return {"ok": False, "error": "record_deploy is a write tool: pass approved_by=<human>", "source": "topology-node"}
    ds = read_json(DEPLOYS_FILE, [])
    d = {"service": service, "version": version, "ts": now(), "approved_by": approved_by}
    write_json(DEPLOYS_FILE, ds + [d])
    return {"ok": True, **d, "source": "topology-node"}


@server.resource("service-map://{service}")
def service_map(service: str) -> str:
    """A one-service slice of the map, as text."""
    n = neighbours(service)
    return (f"{service} ({_kind(service)}): owner={TOPO['services'].get(service, {}).get('owner')} "
            f"wave={TOPO['services'].get(service, {}).get('wave')}\n"
            f"upstream: {[e['from'] for e in n['upstream']]}\ndownstream: {[e['to'] for e in n['downstream']]}")


# -- background: a new deploy is context --------------------------------------------
def watch() -> None:
    seen = {f"{d['service']}@{d['ts']}" for d in read_json(DEPLOYS_FILE, [])}
    while True:
        try:
            for d in read_json(DEPLOYS_FILE, []):
                key = f"{d['service']}@{d['ts']}"
                if key not in seen:
                    seen.add(key)
                    bus.publish("context.topology.deploy", source="topology-node", **d)
                    print(f"[topology-node] -> context.topology.deploy {d['service']} {d['version']}", flush=True)
        except Exception as e:
            print(f"[topology-node] watch error: {e}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(DEPLOYS_FILE) or ".", exist_ok=True)
    threading.Thread(target=watch, daemon=True).start()
    run_server(server, "topology")
