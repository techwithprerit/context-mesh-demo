# Rehearsal report · 2026-11-10

Rehearsed end to end on the presenting laptop (docker mode, plus local mode as backup). Logs:
`/tmp/run1.log`, `/tmp/run2.log` (before fixes), `/tmp/run2b.log`, `/tmp/run3.log` (after fixes),
`/tmp/paneB.log` (bus pane, every run and test, separated by `=== …` markers), `/tmp/paneB-run1-run2.log`.

## Environment

| | |
|---|---|
| OS | macOS 27.2 (26B123), Apple Silicon, 10 CPUs given to Docker, 8 GB |
| Python | 3.15.2 (Homebrew), in a repo venv `.venv/`: Homebrew Python refuses `pip install -e .` (PEP 668) |
| mcp | 2.3.0 (2.x, imports fine) |
| Docker | Docker 29.9.3 client / 29.9.2 Desktop engine, Compose v5.6.1. Docker Desktop was **not running** at first: start it before `make up` |
| Time to GO | cold `make up` (with first image build) to preflight GO: **67 s**. `make reset` to GO: 30 s. Local mode: GO at 45 s |

## Checklist

Part 1
- PASS · docker preflight: six `pass` lines, `GO: walk on` (first try, and after `make reset`).
- PASS · local mode preflight: `pass bus round-trip  file bus round-trip ok` … `GO: walk on`.

Part 2, run 1 (fresh estate)
- PASS · first explain: `no explaining context`; trace ends `payments:SELECT balance 7ms`; no log lines.
- PASS · `refused: record_deploy is a write tool` then `deploy recorded: payments v2.3.1 (approved by prerit)`.
- PASS · four topics in pane B 8 s after the deploy (`00:07:44 deploy` → `00:07:52 context.metrics.latency`).
- PASS · second explain: `cause deploy v2.3.1 of payments 10s ago … (confidence high)`; action `needs a human approver`; `slow p99 1667 ms vs baseline 76 ms` / `vs baseline 66 ms`; evidence from all four nodes; trace `36c5315eb6704a68` = both log lines.
  - Cosmetic, fixed: hot edges showed `p99 81 ms` next to `slow p99 1667 ms` (RED snapshot up to 10 s old).
- PASS · `SUPPRESSED cpu 92%` 3 s after failover, `recheck in 120 s`.
- PASS · `cpu_high.cleared` 48 s after failover, before the cpu step.
- PASS · `PAGE cpu 94% on db-prod-17` 4 s after `chaos cpu`, `catch-up is done (lag 0 s)`, `dependents ['payments', 'orders', 'web']`, `RB-DB-07`, four sources.
- PASS · `00:09:57 RE-CHECK … a newer decision already paged for this host` (120 s after SUPPRESSED), no second PAGE.
  - Cosmetic, fixed: the line ended `(cpu now 0%)` although no reading was taken.
- **FAIL** · audit slo: payments `BURNING`, all `BREACH`, burning edges named, but web/orders `OK` (burn 2m 5.4). In `--auto` the audit ran 65 s after the deploy, before the 2 m window was half broken. Fixed (see changes).
- PASS · posture: exactly the five findings; `graph: carrying 19.5 req/s right now; 3 services behind it`.
- PASS · extended: `GO, with notes` + one billing-records note; then `6 findings` incl. `freeze_deploys web v9`.

Part 2, run 2 (after `make reset`, 30 s)
- **FAIL** · preflight: `FAIL baseline healthy orders->payments p99 1724.3 ms` → NO-GO, yet the next command ran: `mesh preflight` exited 0.
- **FAIL** · first explain was not quiet: `error_rate 1.4%` and a ✗ trace with log lines from run 1.
- **FAIL** · `timed out waiting for four nodes have spoken` (90 s): the metrics node raised `latency` at boot from run 1's data, so no new rising edge came at the deploy.
- PASS · the rest (SUPPRESSED 4 s, PAGE 4 s, ELEVATED at 146 s, posture, extended).
- Root cause of all three: `make reset` cleared the nodes' memory but not the telemetry, so the last run stayed inside every 2/5/10-minute window. Fixed.

Part 2, run 2b and run 3 (after fixes; each straight after `make reset` + 30 s)
- PASS on every Beat 1–3 item. Run 3 evidence:
  - `four nodes have spoken (6 s)`.
  - Trace `9e6f973dee5642e5` = both log lines.
  - `ELEVATED` web 11.5 / orders 11.7; `BURNING` payments 58.5 / 45.5.
  - `5 findings`, `GO, with notes`, `6 findings`.
  - Run 2b RE-CHECK: `a newer decision already paged for this host; nothing to add`.

Part 3
1. PASS · logs stopped + failover: `PAGE … the graph could not decide … failing open` 5 s after the failover.
   - Reason was `ExceptionGroup: unhandled errors in a TaskGroup`; fixed, now `logs-node did not answer search: ConnectError`.
   - `mesh status` green 1 s after `start logs`.
2. PASS · `docker compose restart redis`: `[bus] redis subscribe lost (Error 111 …); retrying in 1 s` in graph logs (the host watcher too). Next `chaos cpu` → PAGE in 6 s. Nothing lost: the metrics node only flips its edge state after a successful publish.
3. PASS · `restart graph`, preflight at +13 s: all six pass, GO.
4. AS DESIGNED, NOT AS WORDED · two `chaos cpu` 5 s apart:
   - Second call printed `cpu_high is still raised … waiting … cleared (2 s)`, then re-raised.
   - Pane B: cpu_high → PAGE, cleared, cpu_high → PAGE. Exactly one PAGE per rising edge, so **two PAGEs for two invocations**, which is what DEMO.md says ("the second alert can never be swallowed").
   - If you meant "one PAGE in total", that needs a design change; I did not make it.
5. PASS · three unapproved deploys: three `refused:` lines; no `data/deploys.json`; chaos `{}`; `context.topology.deploy` count 0.
6. PASS (after a check fix) · estate stopped 20 s: `mesh status` 6/6 at every 3 s sample; nothing paged or suppressed; no log noise.
   - Preflight `estate writing` still **passed** 20 s in (`6.0 req/s` over a 30 s window). Fixed to a 10 s window: FAIL 12 s into an outage (`0.0/s over 10 s`).

Part 4 extras
- PASS · malformed events on the bus (`not json`, `[1,2]`, missing keys, bad `ts`): one `[bus] …` line each, no traceback, watcher and graph alive.
  - Before: a non-JSON message on redis dropped the subscription for 1 s.
- PASS · startup: `red poll failed` once at boot (bar: more than twice); traces down < 10 s. Left alone.
- PASS · node logs quiet when the estate is quiet: only uvicorn access lines from the 10 s RED poll.
- PASS · second explain had all four nodes in `sources` in every run.
- Size: every node < 150 lines (largest policy 131, metrics 129). Not nodes, but shown on stage: `graph.py` 467, `cli.py` 439, `estate.py` 160.
- Tool names: all question-shaped. Borderline: `metrics.gauge(name, label, value)` and `series(…)` take raw metric and label names (`node_cpu_ratio`, `instance`), so the caller knows the backend's vocabulary. Not refactored.

## Timings

| | run 1 | run 2 (pre-fix) | run 2b | run 3 |
|---|---|---|---|---|
| `mesh rehearse` machine time | 69 s | 150 s | 125 s | 125 s |
| approved deploy → "four nodes have spoken" | 10 s (bus: 8 s) | **timed out (90 s)** | 8 s | 6 s |
| `chaos failover` → SUPPRESSED | 4 s (bus: 3 s) | 4 s | 6 s (bus: 5 s) | 4 s |
| failover → `cpu_high.cleared` | 48 s | 46 s | 44 s | 44 s |
| `chaos cpu` → PAGE | 6 s (bus: 4 s) | 4 s | 6 s (bus: 4 s) | 6 s |

Run 2b/3 are longer on purpose: the director now waits until 120 s after the deploy before the SLO
audit (in `--auto` only; live you are ~3 min in by then, so it is a no-op).

## Changes made

| file | what | why | proven by |
|---|---|---|---|
| `mesh/estate.py` | if `data/wipe` exists, `telemetry.prune(keep_s=0)` and remove it | the store's single writer empties it, so a reset leaves no rehearsal in any window | run 2b/3 first explain quiet; reset straight after a broken minute → GO |
| `Makefile` | `reset` touches `data/wipe`, sleeps 2 s, then restarts nodes; `PY` uses `.venv/bin/python` if present | root cause of the three run-2 FAILs; `make reset` with system python3 silently skipped `chaos reset` | same; `make reset` works without activating the venv |
| `mesh/cli.py` | `preflight` exits 1 on NO-GO | run 2 walked into a NO-GO | `exit=1` path; chained commands stop |
| `mesh/cli.py` | `estate writing` over 10 s, not 30 s | passed with the estate 20 s dead | Part 3.6 re-run: FAIL at 12 s |
| `mesh/cli.py` | director waits until 120 s after the deploy before `audit slo` | `--auto` audited at 65 s → web/orders `OK`; the fallback recording must match the story | run 2b/3 `ELEVATED` |
| `mesh/cli.py` | preflight no longer prints ` graph saw the ping (0 s)` mid-list; RE-CHECK omits `cpu now` when no reading was taken | screen matches DEMO.md; `cpu now 0%` was false | preflight output; run 2b RE-CHECK line |
| `mesh/graph.py` | `explain` refreshes RED before listing hot edges | `p99 81 ms` next to `slow p99 1667 ms` | run 2b/3 `p99 2015 ms … slow p99 1954 ms` |
| `mesh/graph.py`, `mesh/common.py` | `call()` names the node and tool that did not answer; the fail-open reason uses it | the stage line read `ExceptionGroup … TaskGroup` | Part 3.1 re-run |
| `mesh/nodes/traces_node.py` | adopt a baseline only from an edge window with no errors | the baseline must come from a healthy minute, even if the node restarts mid-incident | runs 2b/3 baselines 66–78 ms |
| `mesh/bus.py` | decode errors and non-dict events: one line, continue (redis and file) | a bad message dropped the redis subscription for 1 s | malformed-event test |

## Doc updates

- `DEMO.md`:
  - T-10 block: the venv line; ~60 s to GO; `make reset` now empties the telemetry windows.
  - preflight block: replaced with today's GO output (the ping line is gone).
  - Beat 1 second explain: replaced with run 3's block (hot edges now show current RED).
  - Beat 1 note: the metrics node's three p99 lines now appear routinely.
  - Beat 2 SUPPRESSED: replaced with run 3's block (`lag 84 s, rising`), plus a note that `rising`/`falling` depends on whether the engine decided in the first ~3 s. Both happened today.
  - Beat 3 SLO table: replaced with the one measured exactly 3:30 after a fresh `deploy --and-break` on a just-reset estate (10 m burn 55.9 vs 38.7 before).
  - Beat 3 note on a mature estate: the ~1.8% / burn ~18 figure is **computed** (6% × 180 s / 600 s), not measured.
  - Rehearse duration ~70 s → ~2 min; "Between rehearsals" explains the wipe and the exit code.
- `README.md`: venv line in Run it; rehearse ~2 min.
- `DEMO-CARD.md`: unchanged; every line still holds.

## Remaining risks

| risk | what you would see | what to do |
|---|---|---|
| Docker Desktop not started | `failed to connect to the docker API` | `open -a Docker`, wait ~30 s, `make up` |
| venv not active in a new terminal pane | `mesh: command not found` | `source .venv/bin/activate` in **both** panes (the Makefile finds the venv itself) |
| Beat 3 earlier than ~2.5 min after the deploy, on an estate that has run >10 min | payments `FAST` instead of `BURNING` (10 m window not yet over 14.4) | keep the pace in DEMO-CARD; if FAST shows, say "the long window has not caught up yet; that is the point of two windows" |
| Rehearsing right before going on, without `make reset` | first explain shows errors; preflight `clean state` FAIL | `make reset`, 30 s, `mesh preflight` (now always GO from a clean reset) |
| You press Enter again before `four nodes have spoken` | thin evidence | the director blocks until all four; manually give it 10 s |
| `rising` vs `falling` in SUPPRESSED | varies run to run | both are true; don't read the trend aloud |
| A node dies mid-demo | red dot, missing source | `docker compose restart <node>`; failing open is designed, say so |
| Fallback recorded at 80×24 without a TTY | cast plays fine but was recorded off-stage size | re-record from your real terminal at stage size the evening before (command below) |
| `asciinema play` waits ~2 min | real waits (dots every 2 s defeat the idle cap) | `asciinema play -s 2 demo.cast` to play at double speed |

## Pre-stage checklist (T-10 min)

```bash
open -a Docker                                   # if not already running; wait for the whale
cd ~/Desktop/context-mesh-demo
source .venv/bin/activate                        # in BOTH terminal panes
export BUS_URL=redis://localhost:6379/0          # in BOTH panes
docker compose ps                                # 8 containers Up; if not: make up  (~60 s)
make reset                                       # chaos, freeze, deploys, telemetry windows, node memory
sleep 30 && mesh preflight                       # six pass, GO; exits non-zero on NO-GO
# pane B:
mesh watch --all
# pane A, when you switch to the terminal:
mesh demo --approved-by prerit
```

Evening before: `make reset && sleep 30 && mesh preflight && asciinema rec --overwrite -i 2 -c "mesh rehearse" demo.cast`
from the stage terminal, then `asciinema play -s 2 demo.cast` to check it. Today's recording is
`demo.cast` (114 events, all three beats); a text copy is in `fallback/` (`run3.log`, `paneB-run3.log`).
