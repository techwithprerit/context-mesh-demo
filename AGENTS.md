# AGENTS.md

Instructions for coding agents (Claude Code, Cursor, Codex, Copilot, Aider and others) working in this
repository. Humans should start with `README.md`.

## What this repo is

A runnable proof of concept of a **context-driven telemetry mesh**: a synthetic service estate writes
metrics, logs and spans to SQLite; five MCP servers ("nodes") each read one store and publish context
events to a bus; one context graph joins everything and answers `explain`, alert-decision, SLO and posture
questions with sources and ages. The `mesh` CLI drives it. It's the live demo for a conference talk, and
`DEMO.md` holds the expected output of every step.

## Pick the task the user asked for

| User says something like… | Do |
|---|---|
| "run this", "set it up", "run the POC", "is it working?" | **Task A: run the POC** |
| "rehearse the demo", "dress rehearsal", "verify the demo" | **Task A, then Task B** |
| "point it at Prometheus/Loki/…", "add a node", "change a rule" | **Task C: extend**, within the rules below |

If it's unclear, do Task A and report.

---

## Task A: run the POC (≈ 5 min wall clock)

1. **Check prerequisites** and report versions. Stop and tell the user if any is missing; don't install Docker yourself.
   ```bash
   python3 --version            # need 3.11+
   docker info >/dev/null && docker compose version   # Docker running, Compose v2
   ```
   On macOS, if Docker isn't running: `open -a Docker`, then poll `docker info` for up to 60 s.

2. **Install the CLI into a repo venv** (system/Homebrew Python refuses a global install, PEP 668):
   ```bash
   python3 -m venv .venv && . .venv/bin/activate && pip install -e .
   pip show mcp | grep Version   # must be 2.x; if it's 1.x, report it and do NOT edit the code to fit
   ```
   Every later command needs `. .venv/bin/activate` in the same shell, or call `.venv/bin/mesh` directly.

3. **Start the stack and get to GO:**
   ```bash
   make up                                  # redis + estate + 5 nodes + graph, ~60 s first time
   export BUS_URL=redis://localhost:6379/0
   make reset && sleep 30
   mesh preflight                           # exit code 0 = GO, 1 = NO-GO
   ```
   Expected: six `pass` lines (`nodes up`, `estate writing`, `graph loaded`, `bus round-trip`,
   `baseline healthy`, `clean state`) and `GO: walk on`.
   On NO-GO, use the fix table at the end of this file, then retry up to 3 times.

4. **Run the whole demo unattended** and capture both views:
   ```bash
   mesh watch --all > /tmp/mesh-paneB.log 2>&1 &   # the bus
   WATCH_PID=$!
   mesh rehearse 2>&1 | tee /tmp/mesh-run.log       # ~2 min; demo --auto --extended
   kill $WATCH_PID
   ```
   `mesh rehearse` takes 2 to 2.5 minutes because it really waits for bus events and burn windows.
   Use a long timeout (≥ 300 s); don't kill it early.

5. **Check the run** against these markers (grep the logs). Each one must be present:

   | Beat | Must appear in `/tmp/mesh-run.log` | and/or in `/tmp/mesh-paneB.log` |
   |---|---|---|
   | 1 | `no explaining context` (first explain) | |
   | 1 | `refused: record_deploy is a write tool`, then `deploy recorded` | `context.topology.deploy` |
   | 1 | `four nodes have spoken` | `context.logs.error_burst`, `context.traces.slow_edge`, `context.metrics.latency` |
   | 1 | `cause   deploy v2.3.1 of payments` … `(confidence high)` and `needs a human approver` | |
   | 2 | `SUPPRESSED cpu` with `recheck  in 120 s` | `context.metrics.cpu_high.cleared` after it |
   | 2 | `PAGE cpu` with `RB-DB-07` | |
   | 3 | `audit slo`: payments `BURNING`; orders and web `ELEVATED`; `BREACH` | |
   | 3 | `posture audit · 5 findings` with `sql_tls`, `incidental_approved`, `no_public_db`, `owner_required`, `write_tools_human` | |
   | extra | `GO, with notes`, then `6 findings` including `freeze_deploys` | |

   Numbers (req/s, ms, %) are synthetic and drift between runs. Compare **structure and labels, not values**.

6. **Report** to the user: environment versions, time to GO, a PASS/FAIL table for the markers above with
   the evidence line for each, and any NO-GO you hit and how you fixed it.

7. **Leave it ready:** `make reset`, wait 30 s, `mesh preflight` → GO. Leave the stack running unless the user
   asked you to stop it (`make down`). Tell the user how to drive it themselves:
   `mesh watch --all` in one terminal, `mesh demo --approved-by <name>` in another.

## Task B: full rehearsal and hardening

`TRIAL-PROMPT.md` is the full brief: two rehearsal runs, six deliberate failure tests (stop a node, restart
redis, restart the graph, double `chaos cpu`, unapproved deploys, estate outage), allowed fixes, and the
format of `REHEARSAL-REPORT.md`. Follow it after Task A. `REHEARSAL-REPORT.md` holds the last report; compare against it.

Local mode is the backup for Docker; check it too if asked:
```bash
make down && unset BUS_URL
scripts/run_local.sh up && sleep 45 && mesh preflight   # must also be GO
scripts/run_local.sh down
```
(`run_local.sh` calls `python3`, so the venv must be active.)

## Task C: extend

Read `skills/context-mesh/SKILL.md` first. It explains the method and the build order.
`skills/context-mesh/scripts/node_template.py` is a complete node to copy, and `skills/context-mesh/references/backends.md`
has Prometheus / Loki / Tempo / Datadog / catalog / Kubernetes snippets.

---

## Hard rules

**Safety, always:**
- **Never pass `--approved-by` on your own initiative** to `mesh deploy` or any write tool, except in the
  scripted `mesh rehearse` / `mesh demo` flow the user asked for. Approving writes is the human's job;
  that's the point of the demo.
- Never commit, push, publish images, or open ports beyond `127.0.0.1` unless the user asks.
- Don't point nodes at real production backends unless the user explicitly asks and provides the endpoints.
- `mesh ask --llm` needs network access and `ANTHROPIC_API_KEY`. Don't use it in Tasks A and B.

**Don't change the story.** The talk's slides print these commands, so they and their flags must keep working
exactly as written: `mesh demo`, `mesh rehearse`, `mesh preflight`, `mesh watch --all`, `mesh explain orders`,
`mesh deploy payments v2.3.1 --approved-by <me> --and-break`, `mesh chaos failover db-prod-17`,
`mesh chaos cpu db-prod-17`, `mesh audit slo`, `mesh audit posture`, `mesh ask "..."`.
New flags are fine if their defaults keep these valid.

**Keep the architecture rules** (each one is enforced in the code and shown on stage):
1. One node reads one store. Nodes never call each other.
2. Tools are named as questions with typed arguments, not query strings.
3. Every tool is read-only (`readOnlyHint`) except `topology.record_deploy`, which must keep refusing without `approved_by`.
4. Every tool result carries `source` and `ts`. No provenance, no verdict.
5. Facts on the graph expire (15 min). Humans never edit the graph.
6. Nodes publish on the rising edge, with a `.cleared` twin on the falling edge.
7. `ask` is a deterministic router; a model is only ever the `--llm` side door.
8. A threshold crossing is a question to the policy node (`decide`) before it is a page; if the graph can't decide, it fails open (pages).

**Code style:** this code is shown on stage. Keep every node under ~150 lines, make the smallest diff that fixes
the cause, and add a one-line comment only when the reason isn't obvious. If a fix changes printed output,
update the matching pasted block in `DEMO.md` with **real** output from a run; never hand-edit numbers.

**Don't** move the Docker telemetry DB back to a bind mount (SQLite WAL breaks on macOS bind mounts), and
don't delete `demo.cast` or `fallback/` (they're the presenter's fallback).

## Map

| Path | What |
|---|---|
| `mesh/estate.py` | synthetic services + chaos knobs (`data/chaos.json`); single writer of `telemetry.db`; honours `data/wipe` |
| `mesh/telemetry.py` | SQLite store, one table per signal |
| `mesh/nodes/{metrics,logs,traces,topology,policy}_node.py` | the five MCP nodes, ports 8101–8105 |
| `mesh/graph.py` | the context graph, alert engine, audits; MCP + HTTP on 8100 |
| `mesh/bus.py` | redis pub/sub (`BUS_URL=redis://…`) or JSONL file (`file://…`) |
| `mesh/cli.py` | the `mesh` CLI, `preflight`, and the demo director (`cmd_demo`) |
| `mesh/topology.yaml`, `mesh/policy.yaml` | the estate and the rules (SLOs, posture, alert suppress rules in prose) |
| `DEMO.md` | the expected output of every step (source of truth for checks) |
| `docker-compose.yml`, `Makefile`, `scripts/run_local.sh` | the two run modes |

## Fixes for NO-GO and common failures

| Symptom | Fix |
|---|---|
| `failed to connect to the docker API` | Docker isn't running: `open -a Docker` (macOS), wait, retry |
| `mesh: command not found` | `. .venv/bin/activate` |
| `FAIL clean state` | `make reset`, `sleep 30`, preflight |
| `FAIL bus round-trip` / `mesh watch` silent | Docker: `export BUS_URL=redis://localhost:6379/0`. Local: `unset BUS_URL` |
| `FAIL baseline healthy` | estate too young or carrying a broken run: `make reset`, `sleep 60`, preflight |
| `FAIL nodes up` | `docker compose ps`; `docker compose logs <svc> --tail 50`; `docker compose restart <svc>` |
| `timed out waiting for four nodes have spoken` | telemetry from an earlier run is still in the windows: `make reset`, `sleep 30`, rerun |
| `audit slo` shows `FAST`, not `BURNING` | ran under ~2.5 min after the deploy; the director normally waits for this. Report it rather than "fixing" thresholds |
| ports 6379 / 8100–8105 in use | report which process holds them (`lsof -i :8100`); don't kill unknown processes |
