"""The context graph: the one thing every node, every alert and every human asks.

- Built once from the topology node (who calls whom), then kept warm by:
  - context events from the bus (deploys, bursts, slow edges, cpu, lag, freeze), and
  - a 10-second RED poll of the traces and metrics nodes per edge.
- Exposed twice: as an MCP server (tools below) and as a read-only HTTP API.
- Hosts the alert engine: a threshold crossing is a question for the graph before it is a page.
"""
from __future__ import annotations

import asyncio
import collections
import threading
import time

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import JSONResponse

from .bus import Bus
from .common import READ_ONLY, age, call, list_tools, now, run_server

server = MCPServer("context-graph", instructions="Ask about a service, an alert, a blast radius, an SLO or posture. Answers carry sources and ages.")
bus = Bus()
RECENT_S = 900  # a fact older than 15 minutes is history, not context
PAGE_DEDUP_S = 300  # the same host does not page twice inside 5 minutes


class Graph:
    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []
        self.events = collections.deque(maxlen=500)
        self.alerts = collections.deque(maxlen=100)
        self.red_ts = 0.0
        self.topo: dict = {}
        self.node_tools: dict[str, list] = {}

    # -- build ---------------------------------------------------------------
    async def load(self) -> None:
        for attempt in range(60):
            try:
                self.topo = await call("topology", "map")
                break
            except Exception:
                await asyncio.sleep(2)
        t = self.topo
        for kind in ("services", "databases", "caches", "topics", "hosts"):
            for name, meta in t.get(kind, {}).items():
                self.nodes[name] = {"name": name, "kind": kind[:-1], "meta": meta, "facts": []}
        for e in t.get("edges", []):
            self.edges.append({**e, "red": {}})
        for name, db in t.get("databases", {}).items():
            self.edges.append({"from": name, "to": db["host"], "kind": "hosted_on", "red": {}})
        for svc in ("metrics", "logs", "traces", "topology", "policy"):
            try:
                self.node_tools[f"{svc}-node"] = await list_tools(svc)
            except Exception:
                pass
        print(f"[graph] loaded {len(self.nodes)} nodes, {len(self.edges)} edges", flush=True)

    def edge(self, a: str, b: str) -> dict | None:
        return next((e for e in self.edges if e["from"] == a and e["to"] == b), None)

    # -- facts ---------------------------------------------------------------
    def fact(self, node: str, kind: str, source: str, ts: float, **data) -> None:
        n = self.nodes.setdefault(node, {"name": node, "kind": "unknown", "meta": {}, "facts": []})
        n["facts"] = [f for f in n["facts"] if not (f["kind"] == kind and kind in ("replica_lag", "cpu_high"))]  # latest wins
        if any(f["kind"] == kind and abs(f["ts"] - ts) < 3 and f.get("msg") == data.get("msg") for f in n["facts"]):
            return  # same fact seen twice (bus + direct lookup)
        n["facts"].append({"kind": kind, "source": source, "ts": ts, **data})
        n["facts"] = n["facts"][-40:]

    def recent(self, node: str, within: int = RECENT_S, kinds: tuple | None = None) -> list[dict]:
        fs = self.nodes.get(node, {}).get("facts", [])
        return [f for f in fs if now() - f["ts"] < within and not f.get("cleared") and (kinds is None or f["kind"] in kinds)]

    def host_of(self, db: str) -> str | None:
        return self.topo.get("databases", {}).get(db, {}).get("host")

    def downstream(self, svc: str) -> list[str]:
        return [e["to"] for e in self.edges if e["from"] == svc and e["kind"] != "hosted_on"]

    def upstream(self, svc: str) -> list[str]:
        return [e["from"] for e in self.edges if e["to"] == svc and e["kind"] != "hosted_on"]

    # -- bus -----------------------------------------------------------------
    def on_event(self, ev: dict) -> None:
        self.events.append(ev)
        t, src, ts = ev["topic"], ev.get("source", "?"), ev.get("ts", now())
        if t.endswith(".cleared"):
            key = t.rsplit(".", 1)[0].split(".")[-1]
            subject = {k: ev[k] for k in ("service", "instance", "host", "from", "to") if k in ev}
            for n in self.nodes.values():
                for f in n["facts"]:
                    if f["kind"] == key and all(f.get(k) == v for k, v in subject.items()):
                        f["cleared"] = True
            if key == "latency" and "service" in ev:
                for e in self.edges:  # a cleared latency on the callee retires the slow marker on its edges
                    if e.get("slow") and ev["service"] in (e["from"], e["to"]):
                        e.pop("slow", None)
            return
        if t == "context.metrics.error_rate":
            self.fact(ev["service"], "error_rate", src, ts, value=ev["value"], service=ev["service"])
        elif t == "context.metrics.latency":
            self.fact(ev["service"], "latency", src, ts, p99_ms=ev["p99_ms"], service=ev["service"])
        elif t == "context.metrics.cpu_high":
            self.fact(ev["instance"], "cpu_high", src, ts, value=ev["value"], instance=ev["instance"])
            threading.Thread(target=lambda: asyncio.run(alert_engine("cpu_high", ev)), daemon=True).start()
        elif t == "context.metrics.replica_lag":
            self.fact(ev["host"], "replica_lag", src, ts, value=ev["value"], trend=ev.get("trend"), host=ev["host"])
        elif t == "context.logs.error_burst":
            self.fact(ev["service"], "error_burst", src, ts, count=ev["count_1m"], sample=ev.get("sample"), trace_ids=ev.get("trace_ids", []))
        elif t == "context.logs.keyword":
            if ev["keyword"] == "failover":
                host = self.host_of(ev["service"]) or ev["service"]
                self.fact(host, "failover", src, ts, msg=ev["msg"])
        elif t == "context.traces.slow_edge":
            e = self.edge(ev["from"], ev["to"])
            if e is not None:
                e["slow"] = {"p99_ms": ev["p99_ms"], "baseline_ms": ev["baseline_ms"], "ts": ts, "sample_trace": ev.get("sample_trace")}
            self.fact(ev["to"], "slow_edge", src, ts, **{"from": ev["from"], "to": ev["to"]}, p99_ms=ev["p99_ms"],
                      baseline_ms=ev["baseline_ms"], sample_trace=ev.get("sample_trace"))
        elif t == "context.topology.deploy":
            self.fact(ev["service"], "deploy", src, ts, service=ev["service"], version=ev["version"], approved_by=ev.get("approved_by"))
        elif t == "context.policy.freeze":
            self.fact("_policy", "freeze", src, ts, active=ev.get("active"), reason=ev.get("reason"))

    # -- RED poll ------------------------------------------------------------------
    async def poll_red(self) -> None:
        try:
            el = await call("traces", "edge_latency", window_s=120)
            for e in el["edges"]:
                g = self.edge(e["from"], e["to"])
                if g is not None:
                    g["red"] = {k: e[k] for k in ("rate_per_s", "error_ratio", "p50_ms", "p99_ms")}
                    g["red"]["ts"] = el["ts"]
            self.red_ts = now()
        except Exception as ex:
            print(f"[graph] red poll failed: {ex}", flush=True)


G = Graph()


def _fact_line(f: dict) -> str:
    k = f["kind"]
    if k == "deploy":
        body = f"deploy {f['version']} of {f.get('node') or f.get('service') or ''} by {f.get('approved_by')}".strip()
    elif k == "error_rate":
        body = f"error rate {f['value']*100:.1f}% on {f.get('service')}"
    elif k == "latency":
        body = f"p99 {f['p99_ms']:.0f} ms on {f.get('service')}"
    elif k == "error_burst":
        body = f"{f['count']} error lines/min: \"{(f.get('sample') or '')[:80]}\""
    elif k == "slow_edge":
        body = f"{f['from']} -> {f['to']} p99 {f['p99_ms']:.0f} ms (baseline {f['baseline_ms']:.0f} ms)"
    elif k == "cpu_high":
        body = f"cpu {f['value']*100:.0f}% on {f['instance']}"
    elif k == "replica_lag":
        body = f"replica lag {f['value']:.0f} s and {f.get('trend')} on {f['host']}"
    elif k == "failover":
        body = f"failover: {f.get('msg', '')[:70]}"
    elif k == "freeze":
        body = f"change freeze {'ON' if f.get('active') else 'off'}: {f.get('reason') or ''}"
    else:
        body = k
    return f"{body}  [{f['source']}, {age(f['ts'])}]"


# -- tools ---------------------------------------------------------------------------
@server.tool(annotations=READ_ONLY)
async def explain(service: str) -> dict:
    """Assemble the story of a service right now: cause, evidence (log + trace + metric + deploy), action, sources."""
    if service not in G.nodes:
        return {"error": f"unknown node {service}", "known": sorted(G.nodes)}
    scope = [service] + G.downstream(service) + G.upstream(service)
    hosts = [G.host_of(d) for d in G.downstream(service) if G.host_of(d)]
    facts: list[dict] = []
    for n in scope + hosts:
        for f in G.recent(n):
            facts.append({**f, "node": n})
    facts.sort(key=lambda f: f["ts"])
    await G.poll_red()  # hot edges show RED as of this question, not of the last 10 s poll
    edges = [e for e in G.edges if service in (e["from"], e["to"]) and e.get("red")]
    for e in edges:
        if e.get("slow") and now() - e["slow"]["ts"] > RECENT_S:
            e.pop("slow", None)  # a slow marker older than the context window is history
    hot = [e for e in edges if e["red"].get("error_ratio", 0) > 0.01 or e.get("slow")]

    cause, action, confidence = "no explaining context in the last 15 min; here is what changed", "investigate from the evidence below", "low"
    symptom = next((f for f in facts if f["kind"] in ("slow_edge", "error_burst", "error_rate", "latency")), None)  # the FIRST symptom
    deploy = next((f for f in reversed(facts) if f["kind"] == "deploy" and (symptom is None or f["ts"] <= symptom["ts"] + 30)), None)
    lag = next((f for f in reversed(facts) if f["kind"] == "replica_lag"), None)
    failover = next((f for f in reversed(facts) if f["kind"] == "failover"), None)
    if deploy and symptom and symptom["ts"] >= deploy["ts"] - 30:
        cause = f"deploy {deploy['version']} of {deploy['node']} {age(deploy['ts'])}; symptoms started {max(0, int(symptom['ts'] - deploy['ts']))} s after it"
        action = f"roll back {deploy['node']} to the previous version (write: needs a human approver)"
        confidence = "high"
    elif failover and lag and lag.get("value", 0) > 0:
        cause = f"replica catch-up on {lag['node']} after a failover {age(failover['ts'])}: lag {lag['value']:.0f} s and {lag.get('trend')}"
        action = "none; re-check when lag < 30 s"
        confidence = "high"

    trio = {}
    try:
        er = await call("metrics", "error_rate", service=service, window_s=300)
        slow = await call("traces", "slowest", service=service, window_s=300, limit=3)
        tr, logs = None, {"lines": []}
        for cand in slow["traces"]:  # prefer the slowest trace that also has log lines: one trace id across all three signals
            got = await call("logs", "for_trace", trace_id=cand["trace_id"])
            if any(l["level"] != "INFO" for l in got["lines"]):  # a routine INFO line is not evidence
                tr, logs = cand, got
                break
        if tr is None and slow["traces"]:
            tr = slow["traces"][0]
            errs = await call("logs", "errors", service=None, window_s=300, limit=3)
            tid = next((l["trace_id"] for l in errs["lines"] if l.get("trace_id")), None)
            if tid:  # an error line exists: show ITS trace, so the ids still match
                tr = await call("traces", "trace", trace_id=tid)
                logs = await call("logs", "for_trace", trace_id=tid)
        trio = {"metric": {"error_rate": er["error_rate"], "requests": er["requests"], "window_s": 300, "source": "metrics-node", "ts": er["ts"]},
                "trace": {"trace_id": tr["trace_id"], "duration_ms": tr["duration_ms"], "critical_path": tr["critical_path"],
                          "source": "traces-node", "ts": tr.get("ts", slow["ts"])} if tr else None,
                "logs": {"lines": [{"service": l["service"], "level": l["level"], "msg": l["msg"]} for l in logs["lines"][:3]],
                         "source": "logs-node"}}
    except Exception as ex:
        trio = {"error": str(ex)}

    sources = {}
    for f in facts:
        sources[f["source"]] = max(sources.get(f["source"], 0), f["ts"])
    for k in ("metric", "trace"):
        if trio.get(k):
            sources[trio[k]["source"]] = max(sources.get(trio[k]["source"], 0), trio[k]["ts"])
    return {"service": service, "owner": G.nodes[service]["meta"].get("owner"), "wave": G.nodes[service]["meta"].get("wave"),
            "cause": cause, "confidence": confidence, "action": action,
            "evidence": [_fact_line(f) for f in facts],
            "hot_edges": [{"edge": f"{e['from']} -> {e['to']}", **e["red"], **({"slow_p99_ms": e['slow']['p99_ms'], "baseline_ms": e['slow']['baseline_ms']} if e.get("slow") else {})} for e in hot],
            "correlated": trio, "sources": {k: age(v) for k, v in sources.items()}, "ts": now()}


@server.tool(annotations=READ_ONLY)
async def blast_radius(node: str) -> dict:
    """Who is affected if this node degrades: transitive dependents with owner, wave and current error ratio."""
    deps = await call("topology", "dependents", node=node)
    out = []
    for d in deps["dependents"]:
        try:
            er = (await call("metrics", "error_rate", service=d["service"], window_s=120))["error_rate"]
        except Exception:
            er = None
        out.append({**d, "error_rate_now": er, "facts": [_fact_line(f) for f in G.recent(d["service"])]})
    return {"node": node, "dependents": out, "count": len(out), "source": "context-graph (topology-node, metrics-node)", "ts": now()}


@server.tool(annotations=READ_ONLY)
async def slo_status(service: str | None = None) -> dict:
    """Multi-window burn rate per SLO'd service, with the edge that is burning the budget."""
    svcs = [service] if service else ["web", "orders", "payments"]
    rows = []
    for s in svcs:
        short = await call("metrics", "error_rate", service=s, window_s=120)   # demo-scaled windows, see policy.yaml
        long = await call("metrics", "error_rate", service=s, window_s=600)
        lat = await call("metrics", "p99", service=s, window_s=300)
        b = await call("policy", "burn", service=s, error_rate_short=short["error_rate"], error_rate_long=long["error_rate"])
        pol = await call("policy", "slo", service=s)
        target = (pol["slo"] or {}).get("latency_p99_ms")
        worst = max([e for e in G.edges if e["from"] == s and e.get("red")], key=lambda e: e["red"].get("error_ratio", 0), default=None)
        rows.append({"service": s, "availability_target": (pol["slo"] or {}).get("availability"), "error_rate_short": short["error_rate"],
                     "error_rate_long": long["error_rate"], "burn_short": b.get("burn_short"), "burn_long": b.get("burn_long"),
                     "windows": "2m / 10m",
                     "burn_status": b.get("status"), "p99_ms": lat["p99_ms"], "latency_target_ms": target,
                     "latency_status": "BREACH" if target and lat["p99_ms"] > target else "OK",
                     "burning_edge": f"{worst['from']} -> {worst['to']} ({worst['red']['error_ratio']*100:.1f}% errors)" if worst and worst["red"].get("error_ratio") else None,
                     "owner": G.nodes.get(s, {}).get("meta", {}).get("owner")})
    return {"slos": rows, "sources": {"metrics-node": "now", "policy-node": "now", "traces-node": age(G.red_ts)}, "ts": now()}


@server.tool(annotations=READ_ONLY)
async def posture_audit() -> dict:
    """Security posture on the same graph: policy rules evaluated over the topology, deploys, freeze and the mesh's own tools."""
    for svc in ("metrics", "logs", "traces", "topology", "policy"):
        if f"{svc}-node" not in G.node_tools:
            try:
                G.node_tools[f"{svc}-node"] = await list_tools(svc)  # a node that came up after the graph
            except Exception:
                pass
    smap = await call("topology", "map")
    deps = await call("topology", "deploys", window_s=3600)
    ev = await call("policy", "evaluate", service_map=smap, deploys=deps["deploys"], node_tools=G.node_tools)
    for f in ev["findings"]:
        a, _, b = f["subject"].partition(" -> ")
        e = G.edge(a, b) if b else None
        if e and e.get("red"):
            f["context"] = f"carrying {e['red']['rate_per_s']} req/s right now; " \
                           f"{len((await call('topology', 'dependents', node=b))['dependents'])} services behind it"
        elif f["rule"] == "no_public_db":
            host = f["subject"].split(" on ")[-1]
            f["context"] = "; ".join(_fact_line(x) for x in G.recent(host)) or "host quiet"
    return {"findings": ev["findings"], "rules_evaluated": ev["rules_evaluated"], "count": len(ev["findings"]),
            "sources": {"policy-node": "now", "topology-node": "now", "mesh tool lists": "startup"}, "ts": now()}


@server.tool(annotations=READ_ONLY)
async def cutover_check(service: str) -> dict:
    """Is a service safe to cut over right now? Five nodes answer; the graph assembles GO / NOTE / NO-GO."""
    checks, notes = [], []
    meta = G.nodes.get(service, {}).get("meta", {})
    deps = await call("topology", "dependents", node=service)
    later = [d for d in deps["dependents"] if (d["wave"] or 0) > (meta.get("wave") or 0)]
    checks.append({"node": "topology", "ok": not later, "text": f"{service} is in wave {meta.get('wave')} · {len(deps['dependents'])} dependents"
                   + ("" if not later else f" · {[d['service'] for d in later]} in a later wave")})
    for db in [d for d in G.downstream(service)] + [d for s in G.downstream(service) for d in G.downstream(s)]:
        if G.nodes.get(db, {}).get("kind") == "database":
            host = G.host_of(db)
            lag = await call("metrics", "gauge", name="pg_replica_lag_seconds", label="host", value=host)
            checks.append({"node": "database", "ok": (lag["value"] or 0) < 1, "text": f"{db} on {host} · replica lag {lag['value'] or 0:.0f} s"})
    for t, tm in G.topo.get("topics", {}).items():
        if service in tm["producers"] or service in tm["consumers"]:
            waves = {s: G.nodes.get(s, {}).get("meta", {}).get("wave") for s in tm["producers"] + tm["consumers"]}
            same = len(set(waves.values())) == 1
            checks.append({"node": "kafka", "ok": same, "text": f"{t} · producers {tm['producers']} + consumers {tm['consumers']} "
                           + ("co-waved" if same else f"split across waves {waves}")})
    for c in G.downstream(service):
        cm = G.topo.get("caches", {}).get(c)
        if cm:
            checks.append({"node": "cache", "ok": cm.get("auth_pr_merged", True) and cm.get("endpoint_flipped", True),
                           "text": f"{c} · auth PR {'merged' if cm.get('auth_pr_merged') else 'OPEN'} · endpoint {'flipped' if cm.get('endpoint_flipped') else 'NOT flipped'}"})
            inc = [e for e in G.edges if e["to"] == c and e.get("incidental")]
            for e in inc:
                w = G.nodes.get(e["from"], {}).get("meta", {}).get("wave")
                notes.append(f"{e['from']} reads {c} incidentally; it is in wave {w}")
    fz = await call("policy", "freeze_active")
    checks.append({"node": "policy", "ok": not fz.get("active"), "text": f"freeze: {'ACTIVE (' + str(fz.get('reason')) + ')' if fz.get('active') else 'none'}"})
    verdict = "NO-GO" if any(not c["ok"] for c in checks) else ("GO" if not notes else "GO, with notes")
    return {"service": service, "verdict": verdict, "checks": checks, "notes": notes, "nodes_consulted": 5, "ts": now()}


@server.tool(annotations=READ_ONLY)
async def ask(question: str) -> dict:
    """Plain-words entry point: routes a question to the right assembly (cutover, explain, blast radius, SLO, posture)."""
    q = question.lower()
    named = sorted([n for n in G.nodes if n.lower() in q and n != "_policy"], key=len, reverse=True)  # longest match wins
    svc = next((n for n in named if G.nodes[n]["kind"] == "service"), "orders")
    node = named[0] if named else svc
    if "cut over" in q or "cutover" in q or "safe to" in q:
        return {"route": "cutover_check", **(await cutover_check(svc))}
    if "blast" in q or "depend" in q or "affected" in q:
        return {"route": "blast_radius", **(await blast_radius(node))}
    if "slo" in q or "budget" in q or "burn" in q:
        return {"route": "slo_status", **(await slo_status(None))}
    if "posture" in q or "audit" in q or "secur" in q or "compliance" in q:
        return {"route": "posture_audit", **(await posture_audit())}
    return {"route": "explain", **(await explain(svc))}


@server.tool(annotations=READ_ONLY)
def events(limit: int = 20) -> dict:
    """The most recent context events seen on the bus."""
    return {"events": list(G.events)[-limit:], "ts": now()}


@server.tool(annotations=READ_ONLY)
def graph() -> dict:
    """The graph itself: nodes with live facts, edges with RED."""
    return {"nodes": list(G.nodes.values()), "edges": G.edges, "red_age": age(G.red_ts), "ts": now()}


# -- the alert engine: a threshold is a question --------------------------------------------
async def alert_engine(kind: str, ev: dict, recheck: bool = False) -> None:
    try:
        await _alert_engine(kind, ev, recheck)
    except Exception as e:  # never swallow a threshold crossing: fail open, say why
        out = {"alert": kind, "instance": ev.get("instance"), "value": ev.get("value"), "verdict": "page",
               "reason": f"the graph could not decide ({e if isinstance(e, RuntimeError) else repr(e)}); failing open", "owner": "db platform",
               "runbook": "RB-DB-07", "action": "treat as an unexplained threshold crossing", "databases": [], "dependents": [],
               "host_facts": [], "sources": {"context-graph": "error"}, "failed_open": True, "ts": now()}
        bus.publish("context.alert.page", source="context-graph", **out)
        G.alerts.append(out)
        print(f"[graph] alert engine failed open: {e}", flush=True)


def _schedule_recheck(kind: str, ev: dict, delay_s: int) -> None:
    def later():
        t_suppressed = now()
        time.sleep(delay_s)
        if any(a.get("instance") == ev["instance"] and a.get("verdict") == "page" and a.get("ts", 0) > t_suppressed for a in G.alerts):
            bus.publish("context.alert.recheck", source="context-graph", alert=kind, instance=ev["instance"], value=None,
                        outcome="a newer decision already paged for this host; nothing to add")
            return
        try:
            cur = asyncio.run(call("metrics", "gauge", name="node_cpu_ratio", label="instance", value=ev["instance"], window_s=20))
        except Exception as e:
            print(f"[graph] recheck failed: {e}", flush=True)
            return
        if (cur["value"] or 0) > 0.85:
            asyncio.run(alert_engine(kind, {**ev, "value": cur["value"], "ts": now()}, recheck=True))
        else:
            bus.publish("context.alert.recheck", source="context-graph", alert=kind, instance=ev["instance"], value=cur["value"],
                        outcome="cleared on its own; nothing to do")
    threading.Thread(target=later, daemon=True).start()


async def _alert_engine(kind: str, ev: dict, recheck: bool) -> None:
    inst = ev.get("instance")
    lag = await call("metrics", "gauge", name="pg_replica_lag_seconds", label="host", value=inst, window_s=20)
    for _ in range(2):  # a lag that just jumped from 0 has no direction yet: look again before judging it
        if not (lag["trend"] == "rising" and (lag["value"] or 0) > 0):
            break
        await asyncio.sleep(2)
        lag = await call("metrics", "gauge", name="pg_replica_lag_seconds", label="host", value=inst, window_s=20)
    fo = next((f for f in reversed(G.recent(inst, kinds=("failover",)))), None)
    if fo is None:  # the bus may not have delivered the log fact yet: ask the logs node directly before deciding
        dbs_on_host = [n for n, m in G.topo.get("databases", {}).items() if m.get("host") == inst]
        hits = [l for l in (await call("logs", "search", pattern="failover", window_s=RECENT_S))["lines"] if l["service"] in dbs_on_host]
        if hits:
            G.fact(inst, "failover", "logs-node", hits[0]["ts"], msg=hits[0]["msg"])
            fo = {"ts": hits[0]["ts"]}
    facts = {"replica_lag": {"value": lag["value"], "trend": lag["trend"]}, "failover": bool(fo), "failover_age": age(fo["ts"]) if fo else None}
    d = await call("policy", "decide", alert=kind, facts=facts)
    dbs = [n for n, m in G.topo.get("databases", {}).items() if m.get("host") == inst]
    deps = (await call("topology", "dependents", node=dbs[0]))["dependents"] if dbs else []
    base = {"alert": kind, "instance": inst, "value": ev.get("value"), "recheck": recheck, "host_facts": [_fact_line(f) for f in G.recent(inst)],
            "databases": dbs, "dependents": [d_["service"] for d_ in deps], "verdict": d["verdict"], "reason": d["reason"],
            "sources": {"metrics-node": age(lag["ts"]), "logs-node": age(fo["ts"]) if fo else "no failover seen",
                        "topology-node": "now", "policy-node": "now"}}
    if d["verdict"] == "suppress":
        out = {**base, "recheck_s": d.get("recheck_s"), "rule": d.get("rule")}
        bus.publish("context.alert.suppressed", source="context-graph", **out)
        _schedule_recheck(kind, ev, int(d.get("recheck_s") or 300))
    else:
        prior = next((a for a in reversed(G.alerts) if a.get("instance") == inst and a.get("verdict") == "page"
                      and not a.get("failed_open") and now() - a.get("ts", 0) < PAGE_DEDUP_S), None)
        if prior:  # one page per host per incident: a second crossing adds context, not a second pager buzz
            bus.publish("context.alert.recheck", source="context-graph", alert=kind, instance=inst, value=ev.get("value"),
                        outcome=f"already paged {age(prior['ts'])} for this host; not paging twice")
            return
        owner = G.nodes.get(dbs[0], {}).get("meta", {}) if dbs else {}
        out = {**base, "owner": owner.get("owner") or "db platform", "runbook": "RB-DB-07 (hot host without catch-up)",
               "action": "check connections and top queries on the host; page the owner if p99 on dependents moves"}
        bus.publish("context.alert.page", source="context-graph", **out)
    out["ts"] = now()
    G.alerts.append(out)
    print(f"[graph] alert {kind} on {inst}{' (recheck)' if recheck else ''}: {d['verdict']} - {d['reason']}", flush=True)


# -- HTTP (read-only) ------------------------------------------------------------------------
@server.custom_route("/api/graph", methods=["GET"])
async def api_graph(request: Request):
    return JSONResponse(graph())


@server.custom_route("/api/events", methods=["GET"])
async def api_events(request: Request):
    return JSONResponse(events(50))


@server.custom_route("/api/alerts", methods=["GET"])
async def api_alerts(request: Request):
    return JSONResponse({"alerts": list(G.alerts)})


@server.custom_route("/api/explain/{service}", methods=["GET"])
async def api_explain(request: Request):
    return JSONResponse(await explain(request.path_params["service"]))


def poller() -> None:
    while True:
        asyncio.run(G.poll_red())
        time.sleep(10)


if __name__ == "__main__":
    asyncio.run(G.load())
    bus.subscribe("context.*", G.on_event)
    threading.Thread(target=poller, daemon=True).start()
    run_server(server, "graph")
