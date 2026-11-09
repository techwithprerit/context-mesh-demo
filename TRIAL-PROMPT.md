# Trial prompt

Paste everything below the line into Claude Code (or Cowork with this folder connected), from the
repo root, on the laptop you will present from. It makes Claude run the dress rehearsal itself,
compare what it sees against the script, fix what is flaky, and leave you a report. Budget: one hour
of wall clock, mostly waiting for the estate.

---

You are my stage engineer for a five-minute live conference demo that runs from this repository
(`context-mesh-demo`). The talk is "Logs, Metrics, Traces… Assemble! Building a Context-Driven Mesh";
the audience is mid-to-advanced in observability. Your job is to rehearse the demo end to end on THIS
machine, verify every beat against the written script, make it more robust, and hand me a report.
Burn whatever tokens you need; reliability matters more than speed.

## Read first, in this order

1. `README.md` (what the system is), `DEMO.md` (the run of show, with the expected output of every
   step pasted from a real run), `DEMO-CARD.md` (the one-page stage card).
2. `skills/context-mesh/SKILL.md` (the eight rules the code must keep obeying; the "Rules the code
   enforces" section of the README repeats them).
3. The code: `mesh/estate.py`, `mesh/bus.py`, `mesh/graph.py`, `mesh/cli.py`, `mesh/nodes/*.py`,
   `mesh/policy.yaml`, `mesh/topology.yaml`, `docker-compose.yml`, `Makefile`, `scripts/run_local.sh`.

## Ground rules

- Do not change the story. The three beats, their order, the CLI commands and flags printed in
  `DEMO.md`, `DEMO-CARD.md` and on the Demo slide are fixed: `mesh demo`, `mesh explain orders`,
  `mesh deploy payments v2.3.1 --approved-by <me> --and-break`, `mesh chaos failover db-prod-17`,
  `mesh chaos cpu db-prod-17`, `mesh audit slo`, `mesh audit posture`, `mesh watch --all`,
  `mesh preflight`, `mesh ask "..."`. Add flags only with defaults that keep the printed commands valid.
- Keep the eight rules from the skill: one node reads one store; tools are questions, not query
  strings; every tool read-only except `topology.record_deploy`, which must keep refusing without
  `approved_by`; every result carries `source` and `ts`; facts expire; rising-edge publishing with
  `.cleared` twins; deterministic `ask` route in front, a model only as `--llm`; a threshold is a
  question to the policy node before it is a page.
- Keep every node under ~150 lines and the whole thing readable; this code is shown on stage.
- If an output changes because of a fix, update the pasted block in `DEMO.md` and the slide notes
  text in `DEMO.md` so the script stays truthful. Do not hand-edit numbers; paste what the tool printed.
- Never leave the estate, nodes or redis running when you finish a section unless the next section
  needs them. Never commit or push.

## Part 1: environment and both run modes

1. `python3 --version` (need 3.11+), `pip install -e .`, `pip show mcp` (need 2.x; if the installed
   SDK is 1.x, the imports in `mesh/common.py` and the nodes fail: tell me, do not downgrade the code).
2. Docker mode, the one I will use on stage: `make up`, then `export BUS_URL=redis://localhost:6379/0`,
   `make reset`, wait 30 s, `mesh preflight`. It must print six `pass` lines and `GO: walk on`. If any
   line fails, fix the cause (not the check) and re-run until it passes. Note: on macOS the
   telemetry database lives on a named volume for a reason (SQLite WAL over a bind mount breaks);
   do not move it back.
3. Local mode as the backup: `make down`, `unset BUS_URL`, `scripts/run_local.sh up`, wait 45 s,
   `mesh preflight` must also be GO. Then `scripts/run_local.sh down`.

Record for the report: OS, Python, mcp version, Docker version, how long `make up` took until GO.

## Part 2: the rehearsal, twice, in docker mode

Open the bus pane in the background and capture it: `mesh watch --all > /tmp/paneB.log 2>&1 &`.
Run `mesh rehearse` (that is `mesh demo --auto --extended`) and save its output to `/tmp/run1.log`.
Then `make reset`, wait 30 s, `mesh preflight` (must be GO: this proves the reset really clears the
nodes' memory), and run it again into `/tmp/run2.log`.

Compare each run against the expected blocks in `DEMO.md` and check every item below. For each,
record PASS or FAIL with the evidence line from the logs:

Beat 1
- The first `explain orders` says `no explaining context` and shows the one-request block with a
  trace whose critical path ends in `payments:SELECT balance` and no log lines.
- The unapproved deploy prints `refused: record_deploy is a write tool`; the approved one prints
  `deploy recorded`.
- Within 20 s of the approved deploy, pane B shows all four of: `context.topology.deploy`,
  `context.traces.slow_edge`, `context.metrics.latency`, `context.logs.error_burst`.
- The second `explain orders` says `cause   deploy v2.3.1 of payments … (confidence high)`, the
  action mentions `needs a human approver`, both hot edges show a `slow p99 … vs baseline` of under
  100 ms, the evidence has lines from all four nodes each ending `[node, age]`, and in the
  "one request, three signals" block the trace id on the `trace` line is IDENTICAL to the trace id
  in both `log` lines. (This exact match is the point of the beat; if it ever differs, that is a bug.)

Beat 2
- Within 10 s of `chaos failover`, pane B shows `SUPPRESSED cpu … reason   replica catch-up after
  failover … recheck  in 120 s`.
- `context.metrics.cpu_high.cleared` appears within 60 s of the failover, before the cpu step runs.
- Within 10 s of `chaos cpu`, pane B shows `PAGE cpu 94% on db-prod-17` with `reason    a failover
  … is on record but catch-up is done (lag 0 s)`, `dependents ['payments', 'orders', 'web']`,
  a runbook and four sources.
- About 120 s after the SUPPRESSED line, pane B shows a `RE-CHECK` line, and it does NOT produce a
  second PAGE for the same host (the engine must notice a newer decision already paged).

Beat 3
- `audit slo`: payments `BURNING`, orders and web `ELEVATED`, all three `BREACH` on latency, a
  burning edge named for web and orders. Note how many seconds after the deploy the audit ran; in
  the real demo it is ~3.5 min, in `--auto` mode it is ~1 min, so the long-window burn will be lower
  in your run than in `DEMO.md`. Then run `mesh audit slo` again at exactly 3 min 30 s after a fresh
  `deploy --and-break` on a clean estate and paste THAT table into `DEMO.md` if it differs from what
  is there by more than rounding.
- `audit posture`: exactly these five findings: `sql_tls`, `incidental_approved`, `no_public_db`,
  `owner_required`, `write_tools_human`; the `sql_tls` one carries a `graph:` line with a live rate
  and `3 services behind it`.
- Extended: `is orders safe to cut over right now?` ends `GO, with notes` with one note about
  billing-records; after `freeze on` + `deploy web v9`, the posture audit has a sixth finding
  `freeze_deploys`.

Timing
- Total machine time of `mesh rehearse` (expect 60–80 s). The time from `chaos failover` to
  SUPPRESSED, from `chaos cpu` to PAGE, and from the approved deploy to "four nodes have spoken".

## Part 3: break it on purpose

With the stack up and the estate quiet (`make reset`, preflight GO), do each of these, observe, and
either confirm the designed behaviour or fix it:

1. `docker compose stop logs` then `mesh chaos failover db-prod-17`. Expected: the alert engine
   cannot ask the logs node for the failover line, so it fails open with
   `PAGE … the graph could not decide … failing open` within ~15 s, and pane B shows it. Then
   `docker compose start logs`; `mesh status` goes green within 20 s. Record what actually happened.
2. `docker compose restart redis` during a quiet period. Expected: the graph's subscriber reconnects
   (`[bus] redis subscribe lost … retrying` in `docker compose logs graph`), and a following
   `mesh chaos cpu db-prod-17` still produces a PAGE. If events are lost silently, fix `mesh/bus.py`.
3. `docker compose restart graph` then `mesh preflight` within 30 s. Expected: `graph loaded` passes
   (the graph rebuilds from the topology node) and `clean state` passes (facts are gone).
4. Run `mesh chaos cpu db-prod-17` twice in a row with 5 s between them. Expected: exactly one PAGE
   (rising edge only), and the second invocation waits for the `cleared` edge before re-raising.
5. Run `mesh deploy payments v2.3.1 --and-break` (no approver) three times. Expected: three refusals,
   no deploy recorded (`mesh events` shows no `context.topology.deploy`), no chaos applied.
6. Kill the estate (`docker compose stop estate`) for 20 s and start it again. Expected: nodes
   survive, `mesh status` stays green throughout, preflight's `estate writing` fails only while it is
   down. Note whether anything else paged or suppressed during the gap; it should not.

## Part 4: improve, within the rules

After Parts 2 and 3, pick the highest-value fixes and make them. Candidates I already suspect, in
priority order; verify before touching, and skip any that are not real on this machine:

- Anything that produced a FAIL above.
- Startup ordering: if `graph` logs `red poll failed` more than twice at boot, or `mesh status`
  shows traces down for more than 10 s after `make up`, add compose healthchecks or shorten the
  traces node's warm-up sleep without breaking its baseline (the baseline must come from a healthy
  minute).
- The `docker compose logs` of every node should be quiet when the estate is quiet: no stack traces,
  no repeated warnings. Fix the source of any noise.
- `mesh watch --all` should never print a Python traceback; if a malformed event appears, print one
  line and continue.
- If the second `explain orders` ever lacks a node in `sources`, make the wait in `cmd_demo` include
  that node's event before it continues.
- If any node file is over 150 lines or any tool name reads like a query language, say so in the
  report (do not refactor unasked).

For every change: smallest diff that fixes the cause, a one-line comment saying why if the reason is
not obvious from the code, and a re-run of the affected check from Part 2 or 3 to prove it. Re-run
`mesh rehearse` once more at the end into `/tmp/run3.log` and confirm Beats 1-3 still pass.

## Part 5: the fallback and the report

1. Record the fallback: `asciinema rec demo.cast -c "mesh rehearse"` (install asciinema if missing;
   if you cannot, save `/tmp/run3.log` and `/tmp/paneB.log` side by side as `fallback/` and say so).
   Play it back for 10 s to confirm the file is good.
2. Write `REHEARSAL-REPORT.md` in the repo root with exactly these sections:
   - Environment (OS, Python, mcp version, Docker version, time to GO)
   - Checklist (every PASS/FAIL from Parts 2 and 3 with its evidence line)
   - Timings (the numbers from Part 2, run 1 and run 2 and run 3)
   - Changes made (file, what, why, which check proves it)
   - Doc updates (which pasted blocks in DEMO.md you replaced, and why)
   - Remaining risks (what could still go wrong on stage and what I should do if it does)
   - Pre-stage checklist (the exact commands, in order, for T-10 min; fold in anything you learned)
3. Leave the stack running in docker mode, reset and preflight-green, so I can walk straight into
   `mesh demo --approved-by prerit`.

Tell me when you are done, with the five most important lines of the report first.
