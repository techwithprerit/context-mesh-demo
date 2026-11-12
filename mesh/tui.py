"""`mesh tui` - the whole run of show in one full-screen terminal: director, live vitals, bus, nodes and beats.

It drives the very same director as `mesh demo` (cli.cmd_demo). The TUI only swaps the Enter prompt for a
key and captures what the CLI prints, so the stage flow can never drift from the CLI. Everything else it
shows comes from the same read-only tools any client would call.
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import contextlib
import io
import json
import os
import re
import shlex
import subprocess
import threading
import time

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, RichLog, Static

from . import cli
from .bus import Bus
from .common import CHAOS_FILE, FREEZE_FILE, PORTS, call, read_json

ANSI = {"cy": "\033[96m", "am": "\033[93m", "rd": "\033[91m", "gr": "\033[92m", "dim": "\033[2m", "b": "\033[1m", "x": "\033[0m"}
BEATS = [("BEAT 1", "correlation across layers"), ("BEAT 2", "an alert that fires on context"),
         ("BEAT 3", "SLOs and a posture audit"), ("done", "reset chaos")]
LAYER = {"metrics": "cyan", "logs": "magenta", "traces": "yellow", "topology": "green", "policy": "blue", "alert": "red"}
FILTERS = ["all", "alert", "topology", "metrics", "logs", "traces"]
SERVICES = ["web", "orders", "payments"]
NOT_HERE = {"watch", "demo", "rehearse", "tui"}  # long-running or interactive: they have keys of their own
SPARK = "▁▂▃▄▅▆▇█"

# Speaker notes per director step (matched on the step's prompt), with the target time on the demo clock.
NOTES = [
    ("quiet estate", 0, "Quiet estate. Ask the graph about orders: there is nothing to explain, and it says so instead of guessing."),
    ("deploy payments", 30, "A deploy is a write. Without a human approver it is refused; with one it is recorded. Point at the bus: four nodes speak, nobody has correlated anything yet."),
    ("explain orders again", 60, "Now ask. Cause, action, hot edges, evidence, and one request joined across metric, trace and logs by trace id. Every fact carries a source and an age."),
    ("failover", 120, "Same CPU threshold, crossed twice. First right after a failover: the graph knows catch-up is running, so it suppresses, says why, and re-checks."),
    ("cpu 94%", 180, "The same number with no story behind it. This time it pages, with the dependents and a runbook. Both decisions explain what they looked for."),
    ("audit slo", 225, "Burn rates off the same graph: payments burning, orders and web elevated. Policy does the maths, metrics give ratios, traces name the edge."),
    ("audit posture", 260, "Posture rules over topology, deploys and the mesh's own tools. Five findings, one annotated with live traffic from the graph."),
    ("the slide-19", 300, "The cutover question in plain words. A deterministic router picks the tools; no model needed."),
    ("freeze on", 330, "Freeze on, an approved deploy anyway, audit again: a sixth finding appears."),
]

PALETTE = [  # (what you type in ctrl+p, mesh argv, help)
    ("explain orders", ["explain", "orders"], "cause, evidence and one request across three signals"),
    ("explain payments", ["explain", "payments"], "the same story from the payments side"),
    ("audit slo", ["audit", "slo"], "multi-window burn rate per SLO"),
    ("audit posture", ["audit", "posture"], "policy rules over topology, deploys and tools"),
    ("ask: is orders safe to cut over right now?", ["ask", "is orders safe to cut over right now?"], "the cutover check"),
    ("ask: who depends on orders-db", ["ask", "who depends on orders-db"], "blast radius"),
    ("alerts so far", ["alerts"], "decisions the alert engine has taken"),
    ("recent events", ["events", "-n", "15"], "the graph's event log"),
    ("status", ["status"], "every node, port and tool"),
    ("chaos: slow payments", ["chaos", "slow", "payments"], "make payments slow and flaky (no deploy)"),
    ("chaos: cpu db-prod-17", ["chaos", "cpu", "db-prod-17"], "94% cpu with no story behind it"),
    ("chaos: failover db-prod-17", ["chaos", "failover", "db-prod-17"], "a failover: expected cpu while the replica catches up"),
    ("chaos: freeze on", ["chaos", "freeze", "on"], "deploy freeze on"),
    ("chaos: freeze off", ["chaos", "freeze", "off"], "deploy freeze off"),
    ("chaos: reset", ["chaos", "reset"], "clear all chaos and the freeze"),
    ("deploy payments v2.3.1 (no approver)", ["deploy", "payments", "v2.3.1"], "shows the refusal: writes need a human"),
]

KEYS = [
    ("p", "preflight: six go/no-go checks; the top bar shows GO or NO-GO"),
    ("r", "make reset, wait 30 s, preflight"),
    ("d", "the demo, one key per beat (the amber bar shows the next step)"),
    ("⏎ / space", "run the next step"),
    ("R", "rehearse: the whole flow unattended"),
    ("a", "switch a running demo to auto (and back)"),
    ("esc", "abort the demo at its next pause"),
    (":", "type any mesh command, e.g. explain payments"),
    ("ctrl+p", "command palette: every mesh command and chaos knob"),
    ("e u o s", "explain orders · audit slo · audit posture · status"),
    ("f", "filter the bus: all → alerts → topology → metrics → logs → traces"),
    ("z", "focus: the director alone, full width (for big projector fonts)"),
    ("v / b", "hide the vitals panel / the whole right column"),
    ("n", "speaker notes and pace (visible on screen: mirror carefully)"),
    ("w", "save the session (director + bus) under ./data"),
    ("c / q / ?", "clear the director / quit / this help"),
]


def _banner() -> str:
    font = {"c": ("┏━╸", "┃  ", "┗━╸"), "o": ("┏━┓", "┃ ┃", "┗━┛"), "n": ("┏┓╻", "┃┗┫", "╹ ╹"), "t": ("╺┳╸", " ┃ ", " ╹ "),
            "e": ("┏━╸", "┣╸ ", "┗━╸"), "x": ("╻ ╻", "┏╋┛", "╹ ╹"), "m": ("┏┳┓", "┃┃┃", "╹ ╹"), "s": ("┏━┓", "┗━┓", "┗━┛"),
            "h": ("╻ ╻", "┣━┫", "╹ ╹"), " ": ("  ", "  ", "  ")}
    return "\n".join("".join(font[ch][row] for ch in "context mesh") for row in range(3))


def spark(vals: list[float], lo: float = 0.0, hi: float | None = None) -> str:
    if not vals:
        return ""
    hi = max(hi or 0, max(vals), lo + 1e-9)
    return "".join(SPARK[min(len(SPARK) - 1, int((v - lo) / (hi - lo) * (len(SPARK) - 1)))] for v in vals)


def mmss(s: float) -> str:
    sign, s = ("-" if s < 0 else ""), abs(int(s))
    return f"{sign}{s // 60}:{s % 60:02d}"


class _Abort(Exception):
    pass


class _Sink(io.TextIOBase):
    """stdout for the director thread: every write lands in the director pane, colours intact."""

    def __init__(self, app: "MeshTUI"):
        self.app = app

    def write(self, s: str) -> int:
        if threading.current_thread() is threading.main_thread():
            self.app.append(s)
        else:
            self.app.call_from_thread(self.app.append, s)
        return len(s)

    def isatty(self) -> bool:
        return True


def bus_line(ev: dict) -> Text:
    t, ts = ev.get("topic", "?"), cli.hhmmss(ev.get("ts", time.time()))
    tag = " (re-check)" if ev.get("recheck") else ""
    if t == "context.alert.page":
        return Text.assemble((f"{ts} ", "dim"), (f" PAGE{tag} ", "bold white on red"),
                             f" cpu {(ev.get('value') or 0)*100:.0f}% on {ev.get('instance')} → {ev.get('owner')}\n",
                             (f"         {ev.get('reason')} · {ev.get('runbook')}", "red"))
    if t == "context.alert.suppressed":
        return Text.assemble((f"{ts} ", "dim"), (f" SUPPRESSED{tag} ", "bold black on green"),
                             f" cpu {(ev.get('value') or 0)*100:.0f}% on {ev.get('instance')}\n",
                             (f"         {ev.get('reason')} · recheck in {ev.get('recheck_s')} s", "green"))
    if t == "context.alert.recheck":
        return Text.assemble((f"{ts} ", "dim"), (" RE-CHECK ", "bold reverse"), f" {ev.get('alert')} on {ev.get('instance')}: {ev.get('outcome')}")
    layer = _layer(t)
    style = "dim" if t.endswith(".cleared") or layer == "preflight" else LAYER.get(layer, "white")
    body = " ".join(f"{k}={v}" for k, v in ev.items() if k not in ("topic", "ts", "source"))
    return Text.assemble((f"{ts} ", "dim"), (t.removeprefix("context."), f"bold {style}"), f" {body[:140]} ", (f"[{ev.get('source')}]", "dim"))


def _layer(topic: str) -> str:
    return topic.split(".")[1] if topic.count(".") >= 2 else ""


async def _vitals_once() -> dict:
    """One sweep of the estate's vitals: RED per service (last 10 s), RED per edge (last 15 s), the db host."""
    jobs = {}
    for s in SERVICES:
        jobs[(s, "rate")] = call("metrics", "rate", service=s, window_s=10)
        jobs[(s, "err")] = call("metrics", "error_rate", service=s, window_s=10)
        jobs[(s, "p99")] = call("metrics", "p99", service=s, window_s=10)
    jobs["edges"] = call("traces", "edge_latency", window_s=15)
    jobs["cpu"] = call("metrics", "gauge", name="node_cpu_ratio", label="instance", value="db-prod-17", window_s=60)
    jobs["lag"] = call("metrics", "gauge", name="pg_replica_lag_seconds", label="host", value="db-prod-17", window_s=60)
    res = await asyncio.gather(*jobs.values(), return_exceptions=True)
    return {k: (None if isinstance(v, BaseException) else v) for k, v in zip(jobs, res)}


class HelpScreen(ModalScreen):
    BINDINGS = [Binding("escape,question_mark,q", "dismiss", "close")]
    DEFAULT_CSS = """
    HelpScreen { align: center middle; }
    #help { width: 92; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    """

    def compose(self) -> ComposeResult:
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold yellow", no_wrap=True)
        t.add_column()
        for k, what in KEYS:
            t.add_row(k, what)
        t.add_row("", "")
        t.add_row("", Text("Same director as `mesh demo`. Every number comes from a read-only tool, with its source and age.", style="dim"))
        yield Static(t, id="help")


class MeshCommands(Provider):
    """ctrl+p: every mesh command and chaos knob, fuzzy-matched."""

    def _hits(self):
        app: MeshTUI = self.app  # type: ignore[assignment]
        for name, argv, help_ in PALETTE:
            yield name, (lambda a=argv: app.job(" ".join(a), lambda: cli.main(a))), help_
        for name, action, help_ in [("demo (one key per beat)", app.action_demo, "the run of show"),
                                    ("rehearse (unattended)", app.action_rehearse, "the whole flow, ~2.5 min"),
                                    ("preflight", app.action_preflight, "go / no-go"),
                                    ("reset + preflight", app.action_reset, "make reset, wait 30 s, preflight"),
                                    ("save session", app.action_save, "director + bus under ./data")]:
            yield name, action, help_

    async def discover(self):
        for name, cb, help_ in self._hits():
            yield DiscoveryHit(name, cb, help=help_)

    async def search(self, query: str):
        m = self.matcher(query)
        for name, cb, help_ in self._hits():
            score = m.match(name)
            if score > 0:
                yield Hit(score, m.highlight(name), cb, help=help_)


class MeshTUI(App):
    TITLE = "context mesh"
    AUTO_FOCUS = None  # keys belong to the app until `:` opens the command bar
    COMMANDS = App.COMMANDS | {MeshCommands}
    CSS = """
    #top { height: 1; background: $primary-background; color: $text; padding: 0 1; }
    #top.page { background: $error; }
    #top.ok { background: $success-darken-2; }
    #main { height: 1fr; }
    #side { width: 44; border: round $primary; padding: 0 1; }
    #director { width: 3fr; border: round $accent; padding: 0 1; }
    #right { width: 2fr; }
    #vitals { height: auto; border: round $success; padding: 0 1; }
    #bus { height: 1fr; border: round $secondary; padding: 0 1; overflow-x: hidden; }
    #bus.page { border: heavy $error; }
    #bus.ok { border: heavy $success; }
    #prompt { height: 3; border: round $warning; padding: 0 1; content-align: left middle; }
    #prompt.idle { border: round $panel-lighten-2; color: $text-muted; }
    #cmd { display: none; }
    #cmd.open { display: block; }
    .hidden { display: none; }
    """
    BINDINGS = [
        Binding("enter,space", "next", "next beat", priority=False),
        Binding("d", "demo", "demo"), Binding("R", "rehearse", "rehearse"), Binding("a", "auto", "auto"),
        Binding("escape", "abort", "abort"), Binding("p", "preflight", "preflight"), Binding("r", "reset", "reset"),
        Binding("e", "explain", "explain orders", show=False), Binding("u", "slo", "audit slo", show=False),
        Binding("o", "posture", "audit posture", show=False), Binding("s", "status", "status", show=False),
        Binding("colon", "command", ": mesh"), Binding("f", "filter", "filter bus"),
        Binding("z", "focus_director", "focus"), Binding("v", "toggle_vitals", "vitals", show=False), Binding("b", "toggle_right", "right pane", show=False),
        Binding("n", "notes", "notes"), Binding("w", "save", "save", show=False), Binding("c", "clear", "clear", show=False),
        Binding("question_mark", "help", "help"), Binding("q", "quit", "quit"),
    ]

    def __init__(self, who: str, extended: bool):
        super().__init__()
        self.who, self.extended = who, extended
        self.buf, self.busy, self.auto, self.abort, self.go = "", None, False, False, threading.Event()
        self.verdict, self.t0, self.beat, self.steps, self.waiting, self.wait_t = "not run", None, -1, [], None, 0.0
        self.nodes: dict[str, bool] = {}
        self.events, self.bus_kind, self._polling, self._vpolling = 0, "?", False, False
        self.bus_log: collections.deque[dict] = collections.deque(maxlen=1500)
        self.rate_hist: collections.deque[int] = collections.deque([0] * 24, maxlen=24)
        self._tick_events, self.filter, self.show_notes, self.last_decision = 0, "all", False, None
        self.hist: dict[str, collections.deque] = collections.defaultdict(lambda: collections.deque(maxlen=30))
        self.vitals: dict = {}
        self.sink = _Sink(self)

    def compose(self) -> ComposeResult:
        yield Static(id="top")
        with Horizontal(id="main"):
            yield Static(id="side")
            with VerticalScroll(id="director"):
                yield Static(id="out")
            with Vertical(id="right"):
                yield Static("waiting for the first sweep…", id="vitals")
                yield RichLog(id="bus", wrap=True, markup=False, max_lines=2000)
        yield Static(id="prompt")
        yield Input(id="cmd", disabled=True, placeholder="mesh …   e.g. explain payments · ask who depends on orders-db · chaos slow payments")
        yield Footer()

    # -- life cycle ---------------------------------------------------------------
    def on_mount(self) -> None:
        cli.C.update(ANSI)  # the CLI blanks its colours when stdout is not a tty; the sink renders them
        orig_say = cli._say

        def say(txt: str) -> None:
            orig_say(txt)
            self.call_from_thread(self._on_beat, txt)

        cli._say, cli._pause = say, self._pause
        self.query_one("#director").border_title = "director · what the audience sees"
        self.query_one("#side").border_title = "mesh"
        self.query_one("#vitals").border_title = "vitals · live, last 10 s"
        self._start_bus()
        self.set_interval(1, self._tick)
        self.set_interval(3, self._poll)
        self.set_interval(3, self._poll_vitals)
        self._poll()
        self._poll_vitals()
        self._tick()
        self.append(f"{ANSI['b']}{ANSI['cy']}{_banner()}{ANSI['x']}\n"
                    f"{ANSI['dim']}MCP nodes · one bus · one context graph · every fact with a source and an age{ANSI['x']}\n\n"
                    f"{ANSI['dim']}p preflight · r reset · d demo (one key per beat) · R rehearse · ctrl+p every command · ? help{ANSI['x']}\n")

    def on_resize(self, ev) -> None:
        """Narrow window (big projector font): drop the right column under 150 cols, the sidebar under 110."""
        size = "wide" if ev.size.width >= 150 else "mid" if ev.size.width >= 110 else "narrow"
        if size != getattr(self, "_size", None):
            self._size = size
            self.query_one("#right").set_class(size != "wide", "hidden")
            self.query_one("#side").set_class(size == "narrow", "hidden")

    def _start_bus(self) -> None:
        log = self.query_one("#bus", RichLog)
        try:
            b = Bus()
            self.bus_kind = b.kind
            if not b.ping():
                raise RuntimeError(f"unreachable at {b.url}")
        except Exception as e:
            log.write(Text(f"bus: {e}. Docker: export BUS_URL=redis://localhost:6379/0", style="bold red"))
            return

        def on_event(ev: dict) -> None:
            self.call_from_thread(self._bus_event, ev)

        b.subscribe("context.*", on_event)

    def _shown(self, ev: dict) -> bool:
        return self.filter == "all" or _layer(ev.get("topic", "")) == self.filter

    def _bus_event(self, ev: dict) -> None:
        self.events += 1
        self._tick_events += 1
        self.bus_log.append(ev)
        if self._shown(ev):
            self.query_one("#bus", RichLog).write(bus_line(ev))
        t = ev.get("topic", "")
        if t in ("context.alert.page", "context.alert.suppressed") and not ev.get("recheck"):
            page = t.endswith("page")
            self.last_decision = (("PAGE" if page else "SUPPRESSED"), ev.get("ts", time.time()))
            self._flash("page" if page else "ok")
            if page:
                self.bell()
            self.notify(f"{ev.get('instance')}: {ev.get('reason')}", title="PAGE" if page else "SUPPRESSED",
                        severity="error" if page else "information", timeout=6)
        elif t == "context.topology.deploy":
            self.notify(f"{ev.get('service')} {ev.get('version')} · approved by {ev.get('approved_by')}", title="deploy recorded", timeout=4)
        self._tick()

    def _flash(self, cls: str) -> None:
        for w in (self.query_one("#bus"), self.query_one("#top")):
            w.add_class(cls)
        self.set_timer(3, lambda: [w.remove_class(cls) for w in (self.query_one("#bus"), self.query_one("#top"))])

    # -- rendering ----------------------------------------------------------------
    def append(self, s: str) -> None:
        self.buf = (self.buf + s)[-300_000:]
        self.query_one("#out", Static).update(Text.from_ansi(self.buf))
        scroll = self.query_one("#director", VerticalScroll)
        self.call_after_refresh(scroll.scroll_end, animate=False)

    def _tick(self) -> None:
        col = {"GO": "bold black on green", "NO-GO": "bold white on red"}.get(self.verdict, "bold reverse")
        clock = f"T+{mmss(time.time() - self.t0)}" if self.t0 else "T+-:--"
        last = Text()
        if self.last_decision:
            kind, ts = self.last_decision
            last = Text.assemble("  ·  last ", (f" {kind} ", "bold white on red" if kind == "PAGE" else "bold black on green"), f" {cli.hhmmss(ts)}")
        self.query_one("#top", Static).update(Text.assemble(
            ("◆ context mesh", "bold cyan"), "  ·  preflight ", (f" {self.verdict} ", col),
            f"  ·  {clock}  ·  bus {self.bus_kind}  ·  approver {self.who}", last, f"  ·  {time.strftime('%H:%M:%S')}"))
        filt = "" if self.filter == "all" else f" · filter {self.filter}"
        self.query_one("#bus").border_title = f"bus · context.* · {self.events} events {spark(list(self.rate_hist))}{filt}"
        self._render_side()
        p = self.query_one("#prompt", Static)
        if self.waiting:
            p.set_class(False, "idle")
            p.update(Text.assemble(("▶ next  ", "bold yellow"), (self.waiting, "bold"),
                                   (f"    waiting {mmss(time.time() - self.wait_t)} · ⏎ / space · a auto · esc abort", "dim")))
        else:
            p.set_class(True, "idle")
            p.update(f"running: {self.busy}{'  (auto)' if self.auto else ''} …" if self.busy
                     else "idle · d demo · R rehearse · p preflight · r reset · : command · ctrl+p palette · ? help")

    def _render_side(self) -> None:
        t = Text()
        t.append("NODES\n", "bold")
        for n, port in PORTS.items():
            up = self.nodes.get(n)
            t.append("● ", "green" if up else "red" if up is False else "dim")
            t.append(f"{n:<9} :{port}\n")
        ch, fz = read_json(CHAOS_FILE, {}), read_json(FREEZE_FILE, {"active": False})
        t.append("\nESTATE\n", "bold")
        t.append("chaos   "), t.append(f"{', '.join(ch) or 'none'}\n", "yellow" if ch else "green")
        t.append("freeze  "), t.append(f"{'ON' if fz.get('active') else 'off'}\n", "yellow" if fz.get("active") else "green")
        t.append("\nRUN OF SHOW\n", "bold")
        beats = self._beats()
        for i, (name, what) in enumerate(beats):
            finished = i < self.beat or (self.beat == len(beats) - 1 and not self.busy)
            mark, style = ("✓ ", "green") if finished else ("▶ ", "bold yellow") if i == self.beat else ("· ", "dim")
            t.append(mark + f"{name} · {what}\n", style)
        if self.steps:
            t.append("\nSTEPS\n", "bold")
            for s in self.steps[-9:]:
                live = s == self.waiting
                t.append(("▶ " if live else "✓ ") + s + "\n", "bold yellow" if live else "dim")
        if self.show_notes:
            note = self._note()
            t.append("\nNOTES\n", "bold magenta")
            if note:
                target, txt = note
                t.append(txt + "\n", "italic")
                if self.t0:
                    drift = (time.time() - self.t0) - target
                    t.append(f"target T+{mmss(target)} · ", "dim")
                    t.append(f"{'behind' if drift > 15 else 'ahead' if drift < -15 else 'on pace'} {mmss(abs(drift))}\n",
                             "red" if drift > 30 else "yellow" if drift > 15 else "green")
            else:
                t.append("start the demo (d) to see notes for each step\n", "dim")
        self.query_one("#side", Static).update(t)

    def _note(self) -> tuple[int, str] | None:
        current = self.waiting or (self.steps[-1] if self.steps else "")
        return next(((tgt, txt) for key, tgt, txt in NOTES if current.startswith(key)), None)

    def _render_vitals(self) -> None:
        """Fixed-width lines, not a table: the sparklines must never squeeze the numbers."""
        v, out = self.vitals, Text()
        last = lambda key, n: list(self.hist[key])[-n:]  # noqa: E731
        out.append(f"{'service':<9}{'req/s':>6} {'err':>6} {'p99':>8}   {'p99 · last 48 s':<18}errors\n", "bold dim")
        for s in SERVICES:
            r, e, p = v.get((s, "rate")), v.get((s, "err")), v.get((s, "p99"))
            if not (r and e and p):
                out.append(f"{s:<9}", "bold"), out.append("  no answer\n", "red"); continue
            p99, err = p["p99_ms"], e["error_rate"]
            pc = "red" if p99 > 500 else "yellow" if p99 > 200 else "green"
            ec = "red" if err > 0.01 else "yellow" if err > 0.001 else "green"
            out.append(f"{s:<9}", "bold"), out.append(f"{r['rate_per_s']:>6.1f} ")
            out.append(f"{err*100:>5.1f}% ", ec), out.append(f"{p99:>6.0f}ms   ", pc)
            out.append(f"{spark(last(f'{s}:p99', 16), hi=300):<18}", pc), out.append(spark(last(f"{s}:err", 16), hi=0.02) + "\n", ec)
        edges = (v.get("edges") or {}).get("edges", [])
        if edges:
            out.append("\nedges (traces, last 15 s)\n", "bold dim")
            for ed in sorted(edges, key=lambda x: (x["from"], x["to"])):
                p99, er = ed["p99_ms"], ed["error_ratio"]
                c = "red" if p99 > 500 or er > 0.01 else "yellow" if p99 > 200 else "green"
                out.append(f"{ed['from']:>8} → {ed['to']:<14}", c)
                out.append(f" {p99:>6.0f}ms {er*100:>5.1f}%  ")
                out.append(spark(last(f"{ed['from']}>{ed['to']}", 12), hi=300) + "\n", c)
        cpu, lag = (v.get("cpu") or {}).get("value"), (v.get("lag") or {}).get("value")
        out.append("\ndb-prod-17 ", "bold dim")
        if cpu is not None:
            n = round(cpu * 16)
            c = "red" if cpu > 0.85 else "yellow" if cpu > 0.6 else "green"
            out.append("cpu "), out.append("█" * n, c), out.append("░" * (16 - n), "dim"), out.append(f" {cpu*100:>3.0f}%  ", c)
        if lag is not None:
            out.append(f"lag {lag:.0f}s {(v.get('lag') or {}).get('trend', '')}", "yellow" if lag > 5 else "dim")
        self.query_one("#vitals", Static).update(out)

    def _beats(self) -> list[tuple[str, str]]:
        return BEATS[:3] + ([("extra", "cutover check, freeze")] if self.extended else []) + BEATS[3:]

    def _on_beat(self, txt: str) -> None:
        names = [b[0] for b in self._beats()]
        key = "done" if txt.startswith("done") else " ".join(txt.split(" ")[:2])
        if key in names:
            self.beat = names.index(key)
        self._tick()

    # -- background work ----------------------------------------------------------
    def _poll(self) -> None:
        self.rate_hist.append(self._tick_events)
        self._tick_events = 0
        if self._polling:
            return
        self._polling = True

        def body():
            res = {n: cli._up(n) for n in PORTS}
            self.call_from_thread(self._polled, res)

        threading.Thread(target=body, daemon=True).start()

    def _polled(self, res: dict) -> None:
        self.nodes, self._polling = res, False
        self._render_side()

    def _poll_vitals(self) -> None:
        if self._vpolling or self.query_one("#right").has_class("hidden"):
            return
        self._vpolling = True

        def body():
            try:
                res = asyncio.run(_vitals_once())
            except Exception:
                res = {}
            self.call_from_thread(self._vitals_in, res)

        threading.Thread(target=body, daemon=True).start()

    def _vitals_in(self, v: dict) -> None:
        self._vpolling, self.vitals = False, v
        for s in SERVICES:
            if v.get((s, "p99")):
                self.hist[f"{s}:p99"].append(v[(s, "p99")]["p99_ms"])
            if v.get((s, "err")):
                self.hist[f"{s}:err"].append(v[(s, "err")]["error_rate"])
        for ed in (v.get("edges") or {}).get("edges", []):
            self.hist[f"{ed['from']}>{ed['to']}"].append(ed["p99_ms"])
        self._render_vitals()

    def _pause(self, auto: bool, prompt: str) -> None:
        """Replaces the director's `input()`: show the next beat and wait for a key (or don't, in auto)."""
        if self.abort:
            raise _Abort()
        if auto or self.auto:
            self.call_from_thread(self._wait_on, prompt)
            self.call_from_thread(self._wait_on, None)
            return
        self.go.clear()
        self.call_from_thread(self._wait_on, prompt)
        self.go.wait()
        self.call_from_thread(self._wait_on, None)
        if self.abort:
            raise _Abort()

    def _wait_on(self, prompt: str | None) -> None:
        if prompt:
            self.steps.append(prompt)
            self.wait_t = time.time()
            if prompt.startswith("the slide-19") and self.extended:  # the extended beat has no banner of its own
                self.beat = 3
        self.waiting = prompt
        self._tick()

    def job(self, label: str, fn) -> None:
        if self.busy:
            self.notify(f"busy with {self.busy}", severity="warning")
            return
        self.busy, self.abort = label, False

        def body():
            with contextlib.redirect_stdout(self.sink), contextlib.redirect_stderr(self.sink):
                print(f"\n{ANSI['dim']}$ mesh {label}{ANSI['x']}")
                try:
                    fn()
                except _Abort:
                    print(f"\n{ANSI['am']}aborted. chaos may still be set: press r to reset{ANSI['x']}")
                except SystemExit as e:
                    if e.code not in (0, None) and label != "preflight":
                        print(f"{ANSI['rd']}exit {e.code}{ANSI['x']}")
                except Exception as e:
                    print(f"{ANSI['rd']}{type(e).__name__}: {e}{ANSI['x']}")
            self.call_from_thread(self._done)

        threading.Thread(target=body, daemon=True).start()
        self._tick()

    def _done(self) -> None:
        self.busy, self.waiting, self.auto = None, None, False
        self._tick()

    def _preflight(self) -> None:
        try:
            cli.cmd_preflight(None)
            verdict = "GO"
        except SystemExit:
            verdict = "NO-GO"
        self.call_from_thread(setattr, self, "verdict", verdict)

    # -- actions ------------------------------------------------------------------
    def _run_demo(self, auto: bool, extended: bool) -> None:
        if self.busy:
            self.notify(f"busy with {self.busy}", severity="warning"); return
        self.auto, self.steps, self.beat, self.t0, self.extended = auto, [], -1, time.time(), extended
        self.job("rehearse" if auto else f"demo --approved-by {self.who}" + (" --extended" if extended else ""),
                 lambda: cli.cmd_demo(argparse.Namespace(auto=False, extended=extended, approved_by=self.who)))

    def action_demo(self) -> None:
        self._run_demo(False, self.extended)

    def action_rehearse(self) -> None:
        self._run_demo(True, True)

    def action_next(self) -> None:
        if self.waiting:
            self.go.set()

    def action_auto(self) -> None:
        if self.busy:
            self.auto = not self.auto
            if self.auto:
                self.go.set()
            self._tick()

    def action_abort(self) -> None:
        if self.query_one("#cmd").has_class("open"):
            self._close_cmd(); return
        if self.busy:
            self.abort = True
            self.go.set()
            self.notify("aborting at the next pause")

    def action_preflight(self) -> None:
        self.job("preflight", self._preflight)

    def action_reset(self) -> None:
        def body():
            p = subprocess.Popen(["make", "reset"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in p.stdout:
                print(line, end="")
            p.wait()
            print("waiting 30 s for the nodes to come back", end="")
            for _ in range(30):
                if self.abort:
                    raise _Abort()
                time.sleep(1); print(".", end="")
            print()
            self._preflight()
        if self.busy:
            self.notify(f"busy with {self.busy}", severity="warning"); return
        self.verdict, self.steps, self.beat, self.t0, self.last_decision = "…", [], -1, None, None
        self.job("reset && sleep 30 && mesh preflight", body)

    def action_explain(self) -> None:
        self.job("explain orders", lambda: cli.cmd_explain(argparse.Namespace(service="orders")))

    def action_slo(self) -> None:
        self.job("audit slo", lambda: cli.cmd_audit(argparse.Namespace(what="slo")))

    def action_posture(self) -> None:
        self.job("audit posture", lambda: cli.cmd_audit(argparse.Namespace(what="posture")))

    def action_status(self) -> None:
        self.job("status", lambda: cli.cmd_status(None))

    def action_filter(self) -> None:
        self.filter = FILTERS[(FILTERS.index(self.filter) + 1) % len(FILTERS)]
        log = self.query_one("#bus", RichLog)
        log.clear()
        for ev in self.bus_log:
            if self._shown(ev):
                log.write(bus_line(ev))
        self._tick()

    def action_focus_director(self) -> None:
        """Projector mode: the director alone, full width, for big fonts on a small screen."""
        hide = not self.query_one("#side").has_class("hidden")
        for w in (self.query_one("#side"), self.query_one("#right")):
            w.set_class(hide, "hidden")

    def action_toggle_vitals(self) -> None:
        self.query_one("#vitals").toggle_class("hidden")

    def action_toggle_right(self) -> None:
        self.query_one("#right").toggle_class("hidden")

    def action_notes(self) -> None:
        self.show_notes = not self.show_notes
        self._render_side()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_save(self) -> None:
        os.makedirs("data", exist_ok=True)
        path = os.path.join("data", f"tui-session-{time.strftime('%Y%m%d-%H%M%S')}.log")
        with open(path, "w") as f:
            f.write(re.sub(r"\x1b\[[0-9;]*m", "", self.buf) + "\n\n# bus\n")
            f.writelines(json.dumps(ev) + "\n" for ev in self.bus_log)
        self.notify(path, title="session saved")

    def action_clear(self) -> None:
        self.buf = ""
        self.append("")

    def action_command(self) -> None:
        cmd = self.query_one("#cmd", Input)
        cmd.disabled = False
        cmd.add_class("open")
        cmd.focus()

    def _close_cmd(self) -> None:
        cmd = self.query_one("#cmd", Input)
        cmd.value = ""
        cmd.remove_class("open")
        cmd.disabled = True
        self.set_focus(None)

    def on_input_submitted(self, ev: Input.Submitted) -> None:
        args = shlex.split(ev.value.strip().removeprefix("mesh "))
        self._close_cmd()
        if not args:
            return
        if args[0] in NOT_HERE:
            self.notify(f"`mesh {args[0]}` runs from its own key here (d, R, the bus pane)", severity="warning"); return
        self.job(" ".join(args), lambda: cli.main(args))

    def action_quit(self) -> None:
        self.abort = True
        self.go.set()
        self.exit()


def run(who: str, extended: bool) -> None:
    if not os.path.isdir("data"):
        print("run `mesh tui` from the repo root, so it sees ./data like the stack does")
    MeshTUI(who, extended).run()
