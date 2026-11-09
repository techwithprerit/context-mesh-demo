"""policy-node: SLOs, posture rules, freeze windows and alert decision rules.

It never reads telemetry. It is handed facts (a service map, an alert, node tool lists) and
returns judgements. That is how "the policy node asks the topology node" works: via the graph.
"""
from __future__ import annotations

import os
import threading
import time

import yaml
from mcp.server.mcpserver import MCPServer

from ..bus import Bus
from ..common import FREEZE_FILE, POLICY_FILE, READ_ONLY, now, read_json, run_server

server = MCPServer("policy-node", instructions="SLO definitions, burn-rate maths, posture rules, freeze state.")
bus = Bus()
POLICY = yaml.safe_load(open(POLICY_FILE))


@server.tool(annotations=READ_ONLY)
def freeze_active() -> dict:
    """Is a change freeze active right now, and why."""
    f = read_json(FREEZE_FILE, {"active": False})
    return {**f, "source": "policy-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def slo(service: str) -> dict:
    """The SLO for a service and the burn-rate windows that apply to it."""
    return {"service": service, "slo": POLICY["slos"].get(service), "burn": POLICY["burn"], "source": "policy-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def burn(service: str, error_rate_short: float, error_rate_long: float) -> dict:
    """Multi-window burn rate from two error ratios (short and long window). Pure maths, no data access."""
    s = POLICY["slos"].get(service)
    if not s:
        return {"service": service, "status": "no-slo", "source": "policy-node", "ts": now()}
    budget = 1 - s["availability"]
    bs, bl = error_rate_short / budget, error_rate_long / budget
    fast = POLICY["burn"]["fast"]["threshold"]
    elevated = POLICY["burn"].get("elevated_threshold", 6.0)
    if bs >= fast and bl >= fast:
        status = "BURNING"      # both windows over the fast-burn line: page
    elif bs >= fast:
        status = "FAST"         # short window over the line, long window not yet: watch
    elif bs >= elevated:
        status = "ELEVATED"     # over the slow-burn line
    else:
        status = "OK"
    return {"service": service, "budget": round(budget, 4), "burn_short": round(bs, 1), "burn_long": round(bl, 1),
            "threshold": fast, "elevated_threshold": elevated, "status": status, "source": "policy-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def rules() -> dict:
    """The posture rules and alert decision rules, by id."""
    return {"posture_rules": POLICY["posture_rules"], "alert_rules": POLICY["alert_rules"], "source": "policy-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def evaluate(service_map: dict, deploys: list | None = None, node_tools: dict | None = None) -> dict:
    """Run every posture rule over a service map (and optionally recent deploys and the mesh's own tool lists)."""
    findings = []
    for e in service_map.get("edges", []):
        if e["kind"] == "sql" and not e.get("tls"):
            findings.append({"rule": "sql_tls", "severity": "high", "subject": f"{e['from']} -> {e['to']}",
                             "detail": f"SQL over port {e['port']} without TLS"})
        if e.get("incidental") and not e.get("approved"):
            findings.append({"rule": "incidental_approved", "severity": "medium", "subject": f"{e['from']} -> {e['to']}",
                             "detail": "incidental read found in traces, never approved by the owner"})
    for name, db in service_map.get("databases", {}).items():
        if db.get("public_ip"):
            findings.append({"rule": "no_public_db", "severity": "high", "subject": f"{name} on {db.get('host')}",
                             "detail": "database host has a public IP"})
    for name, svc in service_map.get("services", {}).items():
        if not svc.get("owner"):
            findings.append({"rule": "owner_required", "severity": "medium", "subject": name, "detail": "no owner recorded"})
    fz = read_json(FREEZE_FILE, {"active": False})
    if fz.get("active"):
        for d in deploys or []:
            if d["ts"] > fz.get("since", 0):
                findings.append({"rule": "freeze_deploys", "severity": "high", "subject": f"{d['service']} {d['version']}",
                                 "detail": f"deployed during freeze ({fz.get('reason', 'no reason given')})"})
    for node, tools in (node_tools or {}).items():
        for t in tools:
            if not t.get("read_only"):
                findings.append({"rule": "write_tools_human", "severity": "info", "subject": f"{node}.{t['name']}",
                                 "detail": "write tool exposed; requires approved_by (verified by its own refusal path)"})
    return {"findings": findings, "rules_evaluated": list(POLICY["posture_rules"]), "source": "policy-node", "ts": now()}


@server.tool(annotations=READ_ONLY)
def decide(alert: str, facts: dict) -> dict:
    """Decide what an alert becomes given the graph's facts: suppress (with reason and recheck) or page."""
    rule = POLICY["alert_rules"].get(alert, {})
    if alert == "cpu_high":
        lag = facts.get("replica_lag") or {}
        fo = facts.get("failover")
        if fo and (lag.get("value") or 0) > 0:
            return {"verdict": "suppress", "reason": f"replica catch-up after failover ({facts.get('failover_age', '?')}): "
                                                      f"lag {lag.get('value', 0):.0f} s, {lag.get('trend')}",
                    "recheck_s": rule.get("recheck_s", 300), "rule": rule.get("suppress_if"), "source": "policy-node", "ts": now()}
        if fo:
            return {"verdict": "page", "reason": f"a failover {facts.get('failover_age')} is on record but catch-up is done (lag 0 s); "
                                                 "this CPU has no explanation", "source": "policy-node", "ts": now()}
        return {"verdict": "page", "reason": "no explaining context on this host", "source": "policy-node", "ts": now()}
    return {"verdict": "page", "reason": "no rule for this alert", "source": "policy-node", "ts": now()}


def watch() -> None:
    last = None
    while True:
        try:
            f = read_json(FREEZE_FILE, {"active": False})
            if f.get("active") != last and last is not None:
                bus.publish("context.policy.freeze", source="policy-node", **f)
                print(f"[policy-node] -> context.policy.freeze active={f.get('active')}", flush=True)
            last = f.get("active")
        except Exception as e:
            print(f"[policy-node] watch error: {e}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    os.makedirs(os.path.dirname(FREEZE_FILE) or ".", exist_ok=True)
    threading.Thread(target=watch, daemon=True).start()
    run_server(server, "policy")
