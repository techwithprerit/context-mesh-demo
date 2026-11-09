"""`mesh` - the human's door into the context mesh, the chaos knobs, and the stage director.

Everything that reads goes through the context-graph MCP server, as any client would.
Everything that changes the estate writes a small JSON file the estate and nodes watch.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.request

from .bus import Bus
from .common import CHAOS_FILE, DEPLOYS_FILE, FREEZE_FILE, PORTS, call, list_tools, node_url, now, read_json, write_json

# -- colours (no deps) ------------------------------------------------------------
C = {"cy": "\033[96m", "am": "\033[93m", "rd": "\033[91m", "gr": "\033[92m", "dim": "\033[2m", "b": "\033[1m", "x": "\033[0m"}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = {k: "" for k in C}


def h(txt: str) -> str:
    return f"{C['b']}{C['cy']}{txt}{C['x']}"


def ok(flag: bool) -> str:
    return f"{C['gr']}ok{C['x']}" if flag else f"{C['rd']}NO{C['x']}"


def src(d: dict) -> str:
    return C["dim"] + " · ".join(f"{k} {v}" for k, v in (d or {}).items()) + C["x"]


def run(coro):
    return asyncio.run(coro)


def hhmmss(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def graph_events(limit: int = 200) -> list[dict]:
    return run(call("graph", "events", limit=limit))["events"]


def wait_for(pred, timeout_s: int, label: str, quiet: bool = False) -> bool:
    """Poll the graph's event log until pred(events) is true; print dots so the stage never looks frozen."""
    t0 = now()
    while now() - t0 < timeout_s:
        try:
            if pred(graph_events()):
                if not quiet:
                    print(f" {C['gr']}{label}{C['x']} ({int(now() - t0)} s)")
                return True
        except Exception:
            pass
        if not quiet:
            print(".", end="", flush=True)
        time.sleep(2)
    if not quiet:
        print(f" {C['am']}timed out waiting for {label}{C['x']}")
    return False


def since(events: list[dict], topic: str, after: float) -> list[dict]:
    return [e for e in events if e["topic"] == topic and e.get("ts", 0) >= after]


# -- read commands ------------------------------------------------------------------
def cmd_status(_):
    print(h("mesh nodes"))
    for n, port in PORTS.items():
        try:
            tools = run(list_tools(n))
            print(f"  {C['gr']}●{C['x']} {n:<9} :{port}  {len(tools)} tools  " + C["dim"] + ", ".join(
                t["name"] + ("" if t["read_only"] else "(write)") for t in tools) + C["x"])
        except Exception as e:
            print(f"  {C['rd']}●{C['x']} {n:<9} :{port}  down ({type(e).__name__})")


def cmd_preflight(_):
    """Everything that must be true before walking on stage, checked in one go."""
    print(h("preflight"))
    good = True

    def check(name, fn):
        nonlocal good
        try:
            detail = fn()
            print(f"  {C['gr']}pass{C['x']}  {name:<34} {C['dim']}{detail}{C['x']}")
        except Exception as e:
            good = False
            print(f"  {C['rd']}FAIL{C['x']}  {name:<34} {e}")

    def nodes_up():
        down = [n for n in PORTS if not _up(n)]
        if down:
            raise RuntimeError(f"down: {down}")
        return "6/6 answering"

    def estate_writing():
        a = run(call("metrics", "rate", service="orders", window_s=10))["rate_per_s"]  # 10 s: "writing now", not "wrote recently"
        if a < 5:
            raise RuntimeError(f"orders rate {a}/s over 10 s: estate not writing?")
        return f"orders {a} req/s"

    def graph_loaded():
        g = run(call("graph", "graph"))
        if len(g["nodes"]) < 8:
            raise RuntimeError("graph has fewer nodes than the topology")
        return f"{len(g['nodes'])} nodes · {len(g['edges'])} edges · RED {g['red_age']}"

    def baseline_warm():
        el = run(call("traces", "edge_latency", window_s=120))
        e = next((x for x in el["edges"] if x["from"] == "orders" and x["to"] == "payments"), None)
        if not e or e["p99_ms"] > 300:
            raise RuntimeError(f"orders->payments p99 {e and e['p99_ms']} ms: estate not quiet or chaos still set")
        return f"orders->payments p99 {e['p99_ms']} ms, errors {e['error_ratio']*100:.1f}%"

    def bus_ok():
        b = Bus()
        if not b.ping():
            raise RuntimeError(f"bus unreachable at {b.url}")
        before = now()
        b.publish("context.preflight.ping", source="mesh-cli")
        if not wait_for(lambda evs: bool(since(evs, "context.preflight.ping", before - 1)), 10, "graph saw the ping", quiet=True):
            raise RuntimeError("published a ping but the graph never saw it: CLI and graph on different buses?")
        return f"{b.kind} bus round-trip ok"

    def clean_state():
        ch = read_json(CHAOS_FILE, {})
        fz = read_json(FREEZE_FILE, {"active": False})
        deps = run(call("topology", "deploys", window_s=900))["deploys"]
        alerts = [e for e in graph_events() if e["topic"].startswith("context.alert.")]
        if ch or fz.get("active") or deps or alerts:
            raise RuntimeError(f"chaos={ch} freeze={fz.get('active')} deploys={len(deps)} alerts={len(alerts)} -> run `make reset`")
        return "no chaos, no freeze, no deploys, no alerts"

    for name, fn in [("nodes up", nodes_up), ("estate writing", estate_writing), ("graph loaded", graph_loaded),
                     ("bus round-trip", bus_ok), ("baseline healthy", baseline_warm), ("clean state", clean_state)]:
        check(name, fn)
    print(f"\n  {'GO' if good else 'NO-GO'}: " + ("walk on" if good else "fix the FAIL lines, then run preflight again"))
    if not good:
        sys.exit(1)  # so `mesh preflight && mesh demo …` can never walk into a NO-GO


def _up(n: str) -> bool:
    try:
        run(list_tools(n))
        return True
    except Exception:
        return False


def cmd_explain(a):
    r = run(call("graph", "explain", service=a.service))
    if "error" in r:
        print(r); return
    print(h(f"{a.service}") + f"  owner {r['owner']} · wave {r['wave']}")
    print(f"\n{C['b']}cause{C['x']}   {C['am']}{r['cause']}{C['x']}  {C['dim']}(confidence {r['confidence']}){C['x']}")
    print(f"{C['b']}action{C['x']}  {r['action']}")
    if r["hot_edges"]:
        print(f"\n{C['b']}hot edges{C['x']}")
        for e in r["hot_edges"]:
            extra = f"  slow p99 {e['slow_p99_ms']:.0f} ms vs baseline {e['baseline_ms']:.0f} ms" if "slow_p99_ms" in e else ""
            print(f"  {e['edge']:<28} {e['rate_per_s']:>6} req/s  err {e['error_ratio']*100:>5.1f}%  p99 {e['p99_ms']:>7.0f} ms{extra}")
    print(f"\n{C['b']}evidence{C['x']} (oldest first)")
    for line in r["evidence"] or ["  (nothing in the last 15 min)"]:
        print(f"  {line}")
    t = r.get("correlated", {})
    if t and "error" not in t:
        print(f"\n{C['b']}one request, three signals{C['x']}")
        m = t["metric"]
        print(f"  metric   error_rate {m['error_rate']*100:.1f}% over {m['requests']} req / 5m   {C['dim']}[metrics-node]{C['x']}")
        if t.get("trace"):
            tr = t["trace"]
            path = " → ".join(f"{s['service']}:{s['op']} {s['ms']:.0f}ms" + (" ✗" if s['status'] == 'error' else "") for s in tr["critical_path"])
            print(f"  trace    {tr['trace_id']} {tr['duration_ms']:.0f} ms   {C['dim']}[traces-node]{C['x']}\n           {path}")
        for l in t["logs"]["lines"]:
            print(f"  log      {l['service']} {l['level']} {l['msg'][:110]}   {C['dim']}[logs-node]{C['x']}")
    print(f"\n{C['b']}sources{C['x']}  {src(r['sources'])}")


def cmd_ask(a):
    q = " ".join(a.question)
    if a.llm:
        from .llm import ask_with_llm
        print(ask_with_llm(q)); return
    r = run(call("graph", "ask", question=q))
    route = r.get("route")
    print(h(f"> {q}") + f"  {C['dim']}route: {route}{C['x']}\n")
    if route == "cutover_check":
        for c in r["checks"]:
            print(f"  {C['cy']}{c['node']:<10}{C['x']} {c['text']:<78} {ok(c['ok'])}")
        for n in r["notes"]:
            print(f"  {C['am']}note      {C['x']} {n}")
        col = C["gr"] if r["verdict"].startswith("GO") else C["rd"]
        print(f"\n  {col}{C['b']} {r['verdict']} {C['x']}  {C['dim']}{r['nodes_consulted']} nodes · 0 tabs{C['x']}")
    elif route == "blast_radius":
        print(f"  {r['count']} dependents of {r['node']}")
        for d in r["dependents"]:
            er = f"{d['error_rate_now']*100:.1f}%" if d.get("error_rate_now") is not None else "n/a"
            print(f"  {d['service']:<18} wave {d['wave']}  owner {str(d['owner'] or '-'):<10} errors now {er:<6} {'; '.join(d['facts'])}")
    elif route == "slo_status":
        cmd_audit(argparse.Namespace(what="slo"))
    elif route == "posture_audit":
        cmd_audit(argparse.Namespace(what="posture"))
    else:
        cmd_explain(argparse.Namespace(service=r["service"]))


def cmd_audit(a):
    if a.what == "slo":
        r = run(call("graph", "slo_status", service=None))
        w = r["slos"][0]["windows"].split(" / ") if r["slos"] else ["short", "long"]
        print(h(f"SLO status · multi-window burn rate · windows {w[0]} / {w[1]} · fast-burn line 14.4, elevated 6"))
        print(f"  {'service':<10} {'target':>7} {'err '+w[0]:>8} {'err '+w[1]:>8} {'burn '+w[0]:>8} {'burn '+w[1]:>8}  {'status':<9} {'p99':>8} {'latency':<8} burning edge")
        for s in r["slos"]:
            col = {"BURNING": C["rd"], "FAST": C["am"], "ELEVATED": C["am"]}.get(s["burn_status"], C["gr"])
            lcol = C["rd"] if s["latency_status"] == "BREACH" else C["gr"]
            print(f"  {s['service']:<10} {s['availability_target']:>7} {s['error_rate_short']*100:>7.2f}% {s['error_rate_long']*100:>7.2f}% "
                  f"{s['burn_short']:>8} {s['burn_long']:>8}  {col}{s['burn_status']:<9}{C['x']} {s['p99_ms']:>6.0f}ms {lcol}{s['latency_status']:<8}{C['x']} "
                  f"{s['burning_edge'] or '-'}")
        print(f"\n  {src(r['sources'])}")
    else:
        r = run(call("graph", "posture_audit"))
        print(h(f"posture audit · {r['count']} findings · rules {', '.join(r['rules_evaluated'])}"))
        for f in r["findings"]:
            col = C["rd"] if f["severity"] == "high" else C["am"] if f["severity"] == "medium" else C["dim"]
            print(f"  {col}{f['severity']:<6}{C['x']} {f['rule']:<20} {f['subject']:<34} {f['detail']}")
            if f.get("context"):
                print(f"         {C['dim']}graph: {f['context']}{C['x']}")
        print(f"\n  {src(r['sources'])}")


def _render_alert(ev):
    t = ev["topic"]
    ts = hhmmss(ev.get("ts", now()))
    tag = " (re-check)" if ev.get("recheck") else ""
    if t == "context.alert.suppressed":
        print(f"\n{ts} {C['gr']}{C['b']}SUPPRESSED{C['x']}{tag} cpu {ev['value']*100:.0f}% on {ev['instance']}\n"
              f"         reason   {ev['reason']}\n         recheck  in {ev['recheck_s']} s · rule: {ev['rule']}\n"
              f"         sources  {src(ev['sources'])}")
    elif t == "context.alert.page":
        print(f"\n{ts} {C['rd']}{C['b']}PAGE{C['x']}{tag} cpu {(ev.get('value') or 0)*100:.0f}% on {ev['instance']} → {ev['owner']}\n"
              f"         reason    {ev['reason']}\n         hosts     {ev['databases']} · dependents {ev['dependents']}\n"
              f"         host facts {'; '.join(ev['host_facts']) or 'none'}\n         runbook   {ev['runbook']}\n"
              f"         action    {ev['action']}\n         sources   {src(ev['sources'])}")
    elif t == "context.alert.recheck":
        cpu = f" (cpu now {ev['value']*100:.0f}%)" if ev.get("value") is not None else ""  # no reading taken: say nothing, not 0%
        print(f"\n{ts} {C['dim']}{C['b']}RE-CHECK{C['x']} {ev['alert']} on {ev['instance']}: {ev['outcome']}{cpu}")


def cmd_watch(a):
    b = Bus()
    if not b.ping():
        print(f"{C['rd']}bus unreachable at {b.url}{C['x']} - with docker: export BUS_URL=redis://localhost:6379/0; local: the file bus under ./data")
        sys.exit(1)
    print(h("watching the bus") + f"  {C['dim']}{b.kind} · threshold events in, decisions out · ctrl-c to stop{C['x']}")

    def show(ev):
        t = ev["topic"]
        if t.startswith("context.alert."):
            _render_alert(ev)
        elif a.all or t.startswith("context.metrics") or t.startswith("context.logs.keyword") or t.startswith("context.topology"):
            short = {k: v for k, v in ev.items() if k not in ("topic", "ts", "source")}
            print(f"{hhmmss(ev.get('ts', now()))} {C['dim']}{t:<32}{C['x']} {json.dumps(short)[:120]}  {C['dim']}[{ev.get('source')}]{C['x']}")

    b.subscribe("context.*", show)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass


def cmd_alerts(_):
    shown = 0
    for ev in graph_events():
        if ev["topic"].startswith("context.alert."):
            _render_alert(ev); shown += 1
    if not shown:
        print("no alert decisions yet")


def cmd_events(a):
    for ev in graph_events(a.n):
        print(hhmmss(ev["ts"]), ev["topic"], json.dumps({k: v for k, v in ev.items() if k not in ("topic", "ts")})[:140])


# -- write commands (the chaos knobs and the one approved write) ------------------------
def _chaos(update):
    ch = read_json(CHAOS_FILE, {})
    update(ch)
    write_json(CHAOS_FILE, ch)
    print(f"{C['dim']}chaos: {json.dumps(ch)}{C['x']}")


def _cpu_firing() -> bool:
    """Is the metrics node's cpu_high edge currently raised for db-prod-17 (per the graph's event log)?"""
    last = None
    for e in graph_events(300):
        if e["topic"] in ("context.metrics.cpu_high", "context.metrics.cpu_high.cleared") and e.get("instance") == "db-prod-17":
            last = e["topic"]
    return last == "context.metrics.cpu_high"


def cmd_chaos(a):
    if a.kind == "reset":
        if a.target:
            _chaos(lambda ch: ch.pop(a.target, None)); return
        write_json(CHAOS_FILE, {}); write_json(FREEZE_FILE, {"active": False}); print("chaos cleared, freeze off"); return
    if a.kind == "slow":
        _chaos(lambda ch: ch.update({"payments": {"p99_ms": a.p99, "error_rate": a.errors}}))
    elif a.kind == "cpu":
        if _cpu_firing():  # a rising edge needs a falling edge first; wait for the metrics node to clear it
            _chaos(lambda ch: ch.pop("db-prod-17", None))
            print("cpu_high is still raised on db-prod-17: cleared the host chaos, waiting for the metrics node to see it", end="")
            wait_for(lambda evs: bool(since(evs, "context.metrics.cpu_high.cleared", now() - 60)) and not _cpu_firing(), 60, "cleared")
        _chaos(lambda ch: ch.setdefault("db-prod-17", {}).update({"cpu": a.value}))
    elif a.kind == "failover":
        _chaos(lambda ch: ch.update({"db-prod-17": {"failover_at": now()}}))
    elif a.kind == "freeze":
        write_json(FREEZE_FILE, {"active": a.target == "on", "since": now(), "reason": "cutover night, no deploys"})
        print(f"freeze {'ON' if a.target == 'on' else 'off'}")


def cmd_deploy(a):
    r = run(call("topology", "record_deploy", service=a.service, version=a.version, approved_by=a.approved_by))
    if not r.get("ok"):
        print(f"{C['rd']}refused:{C['x']} {r.get('error')}"); return
    print(f"deploy recorded: {a.service} {a.version} (approved by {a.approved_by})")
    if a.and_break:
        _chaos(lambda ch: ch.update({a.service: {"p99_ms": 1900, "error_rate": 0.06}}))


# -- the director: the whole run of show, one Enter per beat ---------------------------------
def _say(txt: str) -> None:
    print(f"\n{C['am']}{C['b']}▶ {txt}{C['x']}")


def _pause(auto: bool, prompt: str) -> None:
    if auto:
        return
    try:
        input(f"{C['dim']}   [{prompt}: Enter]{C['x']} ")
    except EOFError:
        pass


def cmd_demo(a):
    auto, who = a.auto, a.approved_by or os.environ.get("USER", "presenter")
    t0 = now()

    def clock():
        return f"{C['dim']}T+{int(now() - t0)//60}:{int(now() - t0)%60:02d}{C['x']}"

    _say("BEAT 1 · correlation across layers, live")
    _pause(auto, "quiet estate: explain orders")
    cmd_explain(argparse.Namespace(service="orders"))
    _pause(auto, "deploy payments v2.3.1 (refused, then approved) and break it")
    cmd_deploy(argparse.Namespace(service="payments", version="v2.3.1", approved_by="", and_break=True))
    t_deploy = now()
    cmd_deploy(argparse.Namespace(service="payments", version="v2.3.1", approved_by=who, and_break=True))
    print(f"   point at pane B: deploy → slow_edge → latency → error_burst, from four nodes {clock()}", end="")
    wait_for(lambda evs: all(since(evs, t, t_deploy) for t in ("context.topology.deploy", "context.traces.slow_edge",
                                                               "context.metrics.latency", "context.logs.error_burst")), 90, "four nodes have spoken")
    _pause(auto, "explain orders again")
    cmd_explain(argparse.Namespace(service="orders"))

    _say("BEAT 2 · an alert that fires on context")
    _pause(auto, "failover db-prod-17 (expected CPU)")
    t_fo = now()
    cmd_chaos(argparse.Namespace(kind="failover", target="db-prod-17", p99=None, errors=None, value=None))
    print(f"   watch pane B for SUPPRESSED {clock()}", end="")
    if wait_for(lambda evs: bool(since(evs, "context.alert.suppressed", t_fo)), 45, "suppressed"):
        _render_alert(since(graph_events(), "context.alert.suppressed", t_fo)[-1])
    print("   the lag drains in ~45 s and cpu clears by itself; talk about the suppress rule meanwhile", end="")
    wait_for(lambda evs: bool(since(evs, "context.metrics.cpu_high.cleared", t_fo)), 90, "cpu_high cleared")
    _pause(auto, "cpu 94% with no story behind it")
    t_cpu = now()
    cmd_chaos(argparse.Namespace(kind="cpu", target="db-prod-17", p99=None, errors=None, value=0.94))
    print(f"   watch pane B for PAGE {clock()}", end="")
    if wait_for(lambda evs: bool(since(evs, "context.alert.page", t_cpu)), 45, "paged"):
        _render_alert(since(graph_events(), "context.alert.page", t_cpu)[-1])

    _say("BEAT 3 · SLOs and a posture audit on the same graph")
    _pause(auto, "audit slo")
    if now() - t_deploy < 150:  # 150 s: 2 m window full, and the 10 m window over 14.4 even on a long-running estate
        print(f"   letting the burn windows fill with v2.3.1 traffic {clock()}", end="", flush=True)
        while now() - t_deploy < 150:
            print(".", end="", flush=True); time.sleep(2)
        print(f" {C['gr']}full{C['x']}")
    cmd_audit(argparse.Namespace(what="slo"))
    _pause(auto, "audit posture")
    cmd_audit(argparse.Namespace(what="posture"))
    if a.extended:
        _pause(auto, "the slide-19 question, for real")
        cmd_ask(argparse.Namespace(question=["is", "orders", "safe", "to", "cut", "over", "right", "now?"], llm=False))
        _pause(auto, "freeze on, deploy web v9, audit again")
        cmd_chaos(argparse.Namespace(kind="freeze", target="on", p99=None, errors=None, value=None))
        cmd_deploy(argparse.Namespace(service="web", version="v9", approved_by=who, and_break=False))
        time.sleep(3)
        cmd_audit(argparse.Namespace(what="posture"))

    _say(f"done {clock()} · resetting chaos (deploys and graph facts age out on their own; `make reset` for a clean rerun)")
    cmd_chaos(argparse.Namespace(kind="reset", target=None, p99=None, errors=None, value=None))


def _default_bus() -> None:
    """No BUS_URL and not in local mode: use the docker redis if it answers, so a fresh pane is never on the wrong bus."""
    if "BUS_URL" in os.environ or os.path.exists(os.path.join(os.environ.get("MESH_DATA", "./data"), "pids")):
        return
    try:
        import redis
        if redis.Redis.from_url("redis://localhost:6379/0", socket_connect_timeout=0.5).ping():
            os.environ["BUS_URL"] = "redis://localhost:6379/0"
    except Exception:
        pass


def main(argv=None):
    _default_bus()
    p = argparse.ArgumentParser(prog="mesh", description="context mesh CLI")
    sp = p.add_subparsers(dest="cmd", required=True)
    sp.add_parser("status", help="every node, port and tool").set_defaults(fn=cmd_status)
    sp.add_parser("preflight", help="the on-stage go/no-go check").set_defaults(fn=cmd_preflight)
    e = sp.add_parser("explain", help="assemble the story of a service"); e.add_argument("service"); e.set_defaults(fn=cmd_explain)
    q = sp.add_parser("ask", help="plain-words question"); q.add_argument("question", nargs="+"); q.add_argument("--llm", action="store_true"); q.set_defaults(fn=cmd_ask)
    au = sp.add_parser("audit", help="slo | posture"); au.add_argument("what", choices=["slo", "posture"]); au.set_defaults(fn=cmd_audit)
    w = sp.add_parser("watch", help="tail the bus"); w.add_argument("--all", action="store_true"); w.set_defaults(fn=cmd_watch)
    sp.add_parser("alerts", help="decisions taken so far").set_defaults(fn=cmd_alerts)
    ev = sp.add_parser("events", help="recent bus events"); ev.add_argument("-n", type=int, default=20); ev.set_defaults(fn=cmd_events)
    c = sp.add_parser("chaos", help="slow | cpu | failover | freeze on|off | reset [key]")
    c.add_argument("kind", choices=["slow", "cpu", "failover", "freeze", "reset"]); c.add_argument("target", nargs="?")
    c.add_argument("--p99", type=float, default=1900); c.add_argument("--errors", type=float, default=0.06); c.add_argument("--value", type=float, default=0.94)
    c.set_defaults(fn=cmd_chaos)
    d = sp.add_parser("deploy", help="the one approved write"); d.add_argument("service"); d.add_argument("version"); d.add_argument("--approved-by", default="")
    d.add_argument("--and-break", action="store_true"); d.set_defaults(fn=cmd_deploy)
    dm = sp.add_parser("demo", help="the run of show, one Enter per beat (--auto for unattended)")
    dm.add_argument("--auto", action="store_true"); dm.add_argument("--extended", action="store_true"); dm.add_argument("--approved-by", default="")
    dm.set_defaults(fn=cmd_demo)
    rh = sp.add_parser("rehearse", help="same as demo --auto --extended"); rh.set_defaults(fn=lambda a: cmd_demo(argparse.Namespace(auto=True, extended=True, approved_by="")))
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
