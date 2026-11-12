# AGENTS.md

Instructions for coding agents (Claude Code, Cursor, Codex, Copilot, Aider, Gemini CLI and others)
working in this repository. Humans should start with [`README.md`](README.md).

## What this repo is

A runnable proof of concept of a **context-driven telemetry mesh**. A synthetic service estate writes
metrics, logs and spans to SQLite. Five MCP servers ("nodes") each read one store and publish context
events to a bus. One context graph joins everything and answers `explain`, alert-decision, SLO and
posture questions, citing the source and age of each fact. The `mesh` CLI drives it. Everything is local,
synthetic and bound to `127.0.0.1`. No cloud account or LLM is needed.

## Pick the task the user asked for

| The user says something like… | Do |
|---|---|
| "run this", "set it up", "run the POC", "is it working?", "is it GO?" | **Task A: run the POC** |
| "rehearse the demo", "verify it", "dress rehearsal", "break it on purpose" | **Task A, then Task B** |
| "point it at Prometheus/Loki/…", "add a node", "change a rule" | **Task C: extend**, within the hard rules |
| "explain how it works" | read `README.md` → *How it works*, then `mesh/graph.py`, and answer; run nothing |

If it's unclear, do Task A and report.

---

## Task A: run the POC (about 5 minutes)

**1. Check prerequisites** and report the versions. If something is missing, stop and tell the user.
Don't install Docker yourself.
```bash
python3 --version                                    # need 3.11+
docker info >/dev/null && docker compose version     # Docker running, Compose v2
```
macOS: if Docker isn't running, run `open -a Docker`, then poll `docker info` for up to 60 s.
Linux: ask the user to start the Docker daemon.

**2. Install the CLI into a repo venv.** Homebrew and system Python refuse a global install (PEP 668).
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e .
pip show mcp | grep Version     # must be 2.x; if it is 1.x, report it and do NOT edit the code to fit
```
Every later command needs `. .venv/bin/activate` in the same shell, or call `.venv/bin/mesh` directly.

**3. Start the stack and get to GO:**
```bash
make up                                   # redis + estate + 5 nodes + graph, ~60 s the first time
export BUS_URL=redis://localhost:6379/0
make reset && sleep 30
mesh preflight                            # exit code 0 = GO, 1 = NO-GO
```
Expected output: six `pass` lines (`nodes up`, `estate writing`, `graph loaded`, `bus round-trip`,
`baseline healthy`, `clean state`) and `GO: walk on`.
On NO-GO, use the [fix table](#fixes-for-no-go-and-common-failures) and retry up to 3 times.

**4. Run the whole demo unattended** and capture both views:
```bash
mesh watch --all > /tmp/mesh-paneB.log 2>&1 &    # the bus
WATCH_PID=$!
mesh rehearse 2>&1 | tee /tmp/mesh-run.log        # = mesh demo --auto --extended
kill $WATCH_PID
```
`mesh rehearse` takes 2 to 2.5 minutes because it really waits for bus events and for the SLO burn
windows to fill. Use a timeout of at least 300 s and don't kill it early. It records the scripted
deploy as approved by `$USER`. That approval is part of the scripted flow the user asked for.

**5. Check the run** by grepping the logs for these markers. Every one must be present.

| Beat | Must appear in `/tmp/mesh-run.log` | and/or in `/tmp/mesh-paneB.log` |
|---|---|---|
| 1 | `no explaining context` (the first explain) | |
| 1 | `refused: record_deploy is a write tool`, then `deploy recorded` | `context.topology.deploy` |
| 1 | `four nodes have spoken` | `context.logs.error_burst`, `context.traces.slow_edge`, `context.metrics.latency` |
| 1 | `cause   deploy v2.3.1 of payments` … `(confidence high)` and `needs a human approver` | |
| 1 | in `one request, three signals`, the trace id on the `trace` line equals the one on both `log` lines | |
| 2 | `SUPPRESSED cpu` with `recheck  in 120 s` | `context.metrics.cpu_high.cleared` after it |
| 2 | `PAGE cpu` with `RB-DB-07` and `dependents ['payments', 'orders', 'web']` | |
| 3 | `audit slo`: payments `BURNING`; orders and web `ELEVATED`; `BREACH` | |
| 3 | `posture audit · 5 findings` with `sql_tls`, `incidental_approved`, `no_public_db`, `owner_required`, `write_tools_human` | |
| extra | `GO, with notes`, then `6 findings` including `freeze_deploys` | |

The numbers (req/s, ms, %) are synthetic and drift between runs. Compare **structure and labels, not values**.

**6. Report** to the user in chat:
- the environment (OS, Python, mcp, Docker and Compose versions) and the time from `make up` to GO
- a PASS/FAIL table of the markers above, with the evidence line for each
- any NO-GO you hit, and how you fixed it

**7. Leave it ready:** `make reset`, wait 30 s, `mesh preflight` → GO. Leave the stack running unless the
user asked you to stop it (`make down`). Tell the user how to drive it themselves:
`mesh watch --all` in one terminal and `mesh demo --approved-by <their name>` in another.

## Task B: full rehearsal and failure tests

Do this after Task A, with the stack up and preflight GO.

**B1. Run it twice.** Run `make reset`, wait 30 s, `mesh preflight` (it must be GO; this proves the reset
clears the nodes' memory), then `mesh rehearse` again into `/tmp/mesh-run2.log`. Check the Task A markers
on both runs. Also record:
- the time from the approved deploy to `four nodes have spoken` (expect under 20 s)
- from `chaos failover` to `SUPPRESSED` (under 10 s)
- from `chaos cpu` to `PAGE` (under 10 s)
- the total time of `mesh rehearse`

**B2. Break it on purpose.** Before each test, run `make reset` and wait for preflight GO. Keep
`mesh watch --all` running in the background, logging to a file.

| # | Do | Expected (the designed behaviour) |
|---|---|---|
| 1 | `docker compose stop logs`, then `mesh chaos failover db-prod-17` | within ~15 s: `PAGE … the graph could not decide … failing open`. Then `docker compose start logs`; `mesh status` is green within 20 s |
| 2 | `docker compose restart redis` while the estate is quiet | `docker compose logs graph` shows `[bus] redis subscribe lost … retrying`; a later `mesh chaos cpu db-prod-17` still PAGEs |
| 3 | `docker compose restart graph`, then `mesh preflight` within 30 s | `graph loaded` and `clean state` both pass |
| 4 | `mesh chaos cpu db-prod-17` twice, 5 s apart | exactly one PAGE; the second call waits for the `cleared` edge, and a second crossing within 5 min shows `RE-CHECK … not paging twice` |
| 5 | `mesh deploy payments v2.3.1 --and-break` (no approver) three times | three refusals; `mesh events` shows no `context.topology.deploy`; no chaos applied |
| 6 | `docker compose stop estate`, wait 20 s, `docker compose start estate` | nodes stay up; only preflight's `estate writing` fails during the gap; nothing pages or suppresses |

**B3. Local mode** is the backup when Docker isn't available. Check that it also reaches GO:
```bash
make down && unset BUS_URL
scripts/run_local.sh up && sleep 45 && mesh preflight    # must also be GO
scripts/run_local.sh down
```
`run_local.sh` calls `python3`, so the venv must be active.

**B4. Fix only what fails**, within the hard rules: make the smallest diff that fixes the cause, not
the check, then re-run the check that failed to prove the fix. Don't refactor code nobody asked you to.

**B5. Report** in chat, with these sections: Environment · Checklist (every PASS/FAIL with its evidence
line) · Timings · Changes made (file, what, why, which check proves it) · Remaining risks. Write it
to a file only if the user asks. Finish with Task A step 7, so the stack is reset and GO.

## Task C: extend

Read [`skills/context-mesh/SKILL.md`](skills/context-mesh/SKILL.md) first. It explains the method and the
build order. [`skills/context-mesh/scripts/node_template.py`](skills/context-mesh/scripts/node_template.py)
is a complete node to copy, and [`skills/context-mesh/references/backends.md`](skills/context-mesh/references/backends.md)
has snippets for Prometheus, Loki, Tempo, Datadog, a service catalog and Kubernetes. To swap a backend,
replace the store helpers in one node. The tools, the events and the graph don't change.

---

## Hard rules

**Safety, always:**
- **Never pass `--approved-by` on your own initiative** to `mesh deploy` or any write tool. The only
  exception is the scripted `mesh rehearse` / `mesh demo` flow, when the user asked for it. Approving
  writes is the human's job, and that's the point of the demo.
- Never commit, push, publish images, or bind ports beyond `127.0.0.1` unless the user asks.
- Don't point nodes at real backends unless the user explicitly asks and provides the endpoints.
  Even then, keep every tool read-only.
- `mesh ask --llm` needs network access and `ANTHROPIC_API_KEY`. Don't use it in Tasks A and B.
- Don't kill processes you didn't start. If ports 6379 or 8100–8105 are taken, report which process holds them.

**These commands and flags must keep working exactly as written** (they're printed on the talk's slides):
`mesh demo`, `mesh rehearse`, `mesh preflight`, `mesh watch --all`, `mesh explain orders`,
`mesh deploy payments v2.3.1 --approved-by <me> --and-break`, `mesh chaos failover db-prod-17`,
`mesh chaos cpu db-prod-17`, `mesh audit slo`, `mesh audit posture`, `mesh ask "..."`.
New flags are fine if their defaults keep these valid.

**Architecture rules.** Each one is enforced in the code, so keep it that way:
1. One node reads one store. Nodes never call each other.
2. Tools are named as questions with typed arguments, not query strings.
3. Every tool is read-only (`readOnlyHint`) except `topology.record_deploy`, which must keep refusing without `approved_by`.
4. Every tool result carries `source` and `ts`. No provenance, no verdict.
5. Facts on the graph expire after 15 min. Humans never edit the graph.
6. Nodes publish on the rising edge, with a `.cleared` twin on the falling edge.
7. `ask` is a deterministic router. A model is only ever the `--llm` side door.
8. A threshold crossing is a question to the policy node (`decide`) before it is a page. If the graph can't decide, it fails open and pages.

**Code style:** keep every node under ~150 lines, make the smallest diff that fixes the cause, and add a
one-line comment only when the reason isn't obvious. If a change alters printed output that `README.md`
quotes, update that block with **real** output from a run. Never hand-edit numbers.

**Don't** move the Docker telemetry DB to a bind mount (SQLite WAL breaks on macOS bind mounts).

## Map

| Path | What |
|---|---|
| `mesh/estate.py` | synthetic services and chaos knobs (`data/chaos.json`); the single writer of `telemetry.db`; honours `data/wipe` |
| `mesh/telemetry.py` | the SQLite store, one table per signal |
| `mesh/nodes/{metrics,logs,traces,topology,policy}_node.py` | the five MCP nodes, ports 8101–8105 |
| `mesh/graph.py` | the context graph, alert engine and audits; MCP + HTTP on 8100 |
| `mesh/bus.py` | redis pub/sub (`BUS_URL=redis://…`) or a JSONL file (`file://…`) |
| `mesh/cli.py` | the `mesh` CLI, `preflight`, and the demo director (`cmd_demo`) |
| `mesh/topology.yaml`, `mesh/policy.yaml` | the estate and the rules (SLOs, posture, alert suppress rules in prose) |
| `docker-compose.yml`, `Makefile`, `scripts/run_local.sh` | the two run modes |
| `skills/context-mesh/` | the reusable method as a Claude skill |

## Fixes for NO-GO and common failures

| Symptom | Fix |
|---|---|
| `failed to connect to the docker API` | Docker isn't running: `open -a Docker` (macOS) or start the daemon (Linux), wait, retry |
| `mesh: command not found` | `. .venv/bin/activate` |
| `pip install` refused (PEP 668) | use the repo venv from step 2 |
| `FAIL clean state` | `make reset`, `sleep 30`, preflight |
| `FAIL bus round-trip`, or `mesh watch` is silent | Docker: `export BUS_URL=redis://localhost:6379/0`. Local: `unset BUS_URL` |
| `FAIL baseline healthy` | the estate is too young or still carrying a broken run: `make reset`, `sleep 60`, preflight |
| `FAIL nodes up` | `docker compose ps`; `docker compose logs <svc> --tail 50`; `docker compose restart <svc>` |
| `timed out waiting for four nodes have spoken` | telemetry from an earlier run is still in the windows: `make reset`, `sleep 30`, rerun |
| `audit slo` shows `FAST`, not `BURNING` | it ran less than ~2.5 min after the deploy (the director normally waits for this). Report it; don't change the thresholds |
| `make stage` fails on Linux | it uses macOS `open`; run `make up`, `make reset`, `sleep 30`, `mesh preflight` instead |
| ports 6379 / 8100–8105 in use | report which process holds them (`lsof -i :8100`); don't kill unknown processes |
