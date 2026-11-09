# Run of show: the five-minute demo

Audience: mid-to-advanced in observability. They know RED, burn rates, sampling and MCP. What they
have not seen is a threshold crossing being *asked about* before it pages, and a posture audit on
the same graph that explains latency. Slide 12 explained MCP; do not explain it again. Show it.

Every output below is pasted from a real run of this repo (`mesh rehearse`), so you know what the
screen will say before it says it. Values are synthetic and drift a little between runs.

## The two ways to drive it

**Director mode (recommended):** one command, one Enter per beat, every wait handled.

```bash
mesh demo --approved-by <your name>       # add --extended for the Q&A spares at the end
```

It runs the exact commands listed in each beat, waits for the right events on the bus before
moving on (so you never type into a half-ready estate), and prints the alert decisions in pane A as
well as pane B. Nothing is faked: it calls the same CLI functions you would.

**Manual mode:** type the commands yourself. They are listed per beat. Same outputs.

## Before the talk (T-10 min, backstage)

```bash
cd context-mesh-demo
make up                                    # docker: redis + estate + 5 nodes + graph (~60 s to GO). Or: make local
python3 -m venv .venv && source .venv/bin/activate   # Homebrew Python refuses a global pip install
pip install -e .                           # the `mesh` CLI on the host
export BUS_URL=redis://localhost:6379/0    # optional: without it the CLI finds the docker redis by itself
make reset                                 # clean chaos/freeze/deploys, empty the telemetry windows AND restart the nodes
sleep 30 && mesh preflight                 # six checks, ends in GO or NO-GO
```

Or, once the venv exists, all of it in one go: `make stage` (starts Docker Desktop if needed, brings
the stack up, resets, waits 30 s, runs preflight).

`mesh preflight` must print:

```
preflight
  pass  nodes up                           6/6 answering
  pass  estate writing                     orders 22.0 req/s
  pass  graph loaded                       10 nodes · 7 edges · RED 0s ago
  pass  bus round-trip                     redis bus round-trip ok
  pass  baseline healthy                   orders->payments p99 64.7 ms, errors 0.0%
  pass  clean state                        no chaos, no freeze, no deploys, no alerts

  GO: walk on
```

A FAIL on `clean state` means a rehearsal is still in the graph's memory: `make reset`, wait 30 s,
preflight again. A FAIL on `bus round-trip` means the CLI and the graph are on different buses
(docker without `BUS_URL`, or local mode with it set).

Terminal: **two panes**, 18 pt or larger, dark background. Pane **B** (right, ~45 %): `mesh watch --all`
and leave it; it is the bus, and the audience watches facts arrive there before you ask anything.
Pane **A** (left): `mesh demo --approved-by <you>`, which stops at `[…: Enter]` prompts.

Let the estate run at least two minutes before beat 1 (the traces node needs a healthy baseline;
preflight's "baseline healthy" line checks it). Keep slide 24 up; switch to the terminal on
"three things to watch for".

## Beat 1 · Correlation across layers, live (0:00 – 2:00)

Say: *"Quiet estate. Four services, a database, a cache, a topic. Let me ask the graph about orders."*
Enter.

```
mesh explain orders
```
```
orders  owner orders · wave 4
cause   no explaining context in the last 15 min; here is what changed  (confidence low)
action  investigate from the evidence below
evidence (oldest first)
    (nothing in the last 15 min)
one request, three signals
  metric   error_rate 0.0% over 1140 req / 5m   [metrics-node]
  trace    e0ef532dc21440d5 96 ms   [traces-node]
           web:GET /checkout 96ms → orders:POST /orders 85ms → payments:POST /charge 68ms → payments:SELECT balance 8ms
sources  metrics-node 0s ago · traces-node 0s ago
```
Point at the bottom block: *"one request, already joined from the metric, the trace's critical path
and the log lines on that trace id. That join is the thing we paid five vendors for."*

Say: *"Now I deploy payments. A deploy is a write, so a human has to approve it."* Enter.

```
mesh deploy payments v2.3.1 --and-break                     # refused
mesh deploy payments v2.3.1 --approved-by <you> --and-break # recorded; v2.3.1 is slow and flaky
```
```
refused: record_deploy is a write tool: pass approved_by=<human>
deploy recorded: payments v2.3.1 (approved by prerit)
```

**Stop talking and point at pane B.** Within ~10 s, in this order:

```
19:49:54 context.topology.deploy      {"service": "payments", "version": "v2.3.1", ...}   [topology-node]
19:49:58 context.logs.error_burst     {"service": "orders", "count_1m": 8, "sample": "POST /orders 502..."}   [logs-node]
19:49:58 context.logs.error_burst     {"service": "payments", ... "charge failed: upstream timeout..."}   [logs-node]
19:50:01 context.traces.slow_edge     {"from": "orders", "to": "payments", "p99_ms": 1972, "baseline_ms": 67, ...}   [traces-node]
19:50:03 context.metrics.latency      {"service": "payments", "p99_ms": 2132}   [metrics-node]
```
Say: *"Four nodes that do not know each other, each publishing one fact. Nobody correlated anything
yet. Those are facts on a bus. Now I ask."* The director waits for all four before it lets you
continue (`four nodes have spoken`). Enter.

```
mesh explain orders
```
```
cause   deploy v2.3.1 of payments 6s ago; symptoms started 2 s after it  (confidence high)
action  roll back payments to the previous version (write: needs a human approver)

hot edges
  web -> orders                  6.67 req/s  err   1.0%  p99    2041 ms  slow p99 1747 ms vs baseline 76 ms
  orders -> payments             6.67 req/s  err   1.0%  p99    2031 ms  slow p99 1737 ms vs baseline 66 ms

evidence (oldest first)
  deploy v2.3.1 of payments by prerit  [topology-node, 6s ago]
  web -> orders p99 1747 ms (baseline 76 ms)  [traces-node, 3s ago]
  orders -> payments p99 1737 ms (baseline 66 ms)  [traces-node, 3s ago]
  7 error lines/min: "POST /orders 502: payments unavailable trace=f7b824611d11448d"  [logs-node, 1s ago]
  7 error lines/min: "charge failed: upstream timeout after 2279ms (gateway=stripe-sim, retry=0) trace"  [logs-node, 1s ago]
  p99 2012 ms on web  [metrics-node, 1s ago]
  p99 2003 ms on orders  [metrics-node, 1s ago]
  p99 1979 ms on payments  [metrics-node, 1s ago]

one request, three signals
  metric   error_rate 1.0% over 800 req / 5m   [metrics-node]
  trace    9e6f973dee5642e5 2355 ms   [traces-node]
           web:GET /checkout 2355ms ✗ → orders:POST /orders 2346ms ✗ → payments:POST /charge 2329ms ✗ → payments:SELECT balance 8ms
  log      payments ERROR charge failed: upstream timeout after 2329ms (gateway=stripe-sim, retry=0) trace=9e6f973dee5642e5   [logs-node]
  log      orders ERROR POST /orders 502: payments unavailable trace=9e6f973dee5642e5   [logs-node]

sources  topology-node 6s ago · traces-node 0s ago · logs-node 1s ago · metrics-node 0s ago
```
Read it top to bottom, slowly: cause with the delta in seconds; the action flagged as a write;
the RED on `orders -> payments` against its own baseline; the evidence timeline, every line
tagged with its node and age; one request as metric + critical path ending in `POST /charge ✗` +
the two log lines **with the same trace id**; sources with ages.
Say: *"No provenance, no verdict."* (The three `p99 … on web/orders/payments` lines
are the metrics node; the order of the evidence lines varies by a second or two between runs.)

## Beat 2 · An alert that fires on context (2:00 – 3:45)

Say: *"Same threshold, crossed twice. Watch what the bus does with it."* Enter.

```
mesh chaos failover db-prod-17
```
Within ~6 s pane B shows `logs.keyword failover`, `metrics.cpu_high`, `metrics.replica_lag`, then:

```
00:55:25 SUPPRESSED cpu 94% on db-prod-17
         reason   replica catch-up after failover (4s ago): lag 80 s, falling
         recheck  in 120 s · rule: replica catch-up in progress on the same host after a failover in the last 15 min
         sources  metrics-node 0s ago · logs-node 4s ago · topology-node now · policy-node now
```
Say: *"CPU 94 percent. In my old inbox that is a page. The graph asked the policy node, the policy
node asked for the host's facts: expected, re-check in two minutes, sleep. The rule is a sentence
in a YAML file, not a regex."* (If the lag has only just
appeared, the engine looks again for up to 4 s so the trend it reports is the drain, not the jump.)

The lag drains in ~45 s and the CPU clears by itself; the director waits for
`context.metrics.cpu_high.cleared` (pane B shows it) and then prompts. Meanwhile: the suppression
is a delay with a reason, not a mute; the graph re-evaluates it after `recheck_s`. Enter.

```
mesh chaos cpu db-prod-17                 # same threshold, no story behind it
```
Within ~5 s:

```
19:50:53 PAGE cpu 94% on db-prod-17 → db platform
         reason    a failover 50s ago is on record but catch-up is done (lag 0 s); this CPU has no explanation
         hosts     ['orders-db'] · dependents ['payments', 'orders', 'web']
         host facts failover: failover complete: db-prod-17r promoted to primary ... [logs-node, 50s ago]; cpu 94% on db-prod-17  [metrics-node, 0s ago]
         runbook   RB-DB-07 (hot host without catch-up)
         action    check connections and top queries on the host; page the owner if p99 on dependents moves
         sources   metrics-node 0s ago · logs-node 50s ago · topology-node now · policy-node now
```
Say: *"Same number. One decision was sleep, one was wake up, and both told you why, and what it
looked for and did not find."*

Manual mode only: `mesh chaos cpu` checks the graph's event log first; if the previous `cpu_high`
has not cleared yet it clears the host chaos and waits for the falling edge, so the second alert
can never be swallowed. You will see it wait; that is correct. A second crossing on the same host within
5 minutes of a PAGE does not page again: pane B shows `RE-CHECK … already paged 10s ago for this host;
not paging twice`.

## Beat 3 · SLOs and a posture audit on the same graph (3:45 – 5:00)

Payments v2.3.1 is still live (nothing was reset), so the budget has been burning for ~3 minutes. Enter.

```
mesh audit slo
```
```
SLO status · multi-window burn rate · windows 2m / 10m · fast-burn line 14.4, elevated 6
  service     target   err 2m  err 10m  burn 2m burn 10m  status         p99 latency  burning edge
  web          0.995    6.61%    5.62%     13.2     11.2  ELEVATED    2378ms BREACH   web -> orders (6.7% errors)
  orders       0.995    6.52%    5.59%     13.0     11.2  ELEVATED    2369ms BREACH   orders -> payments (6.7% errors)
  payments     0.999    6.53%    5.59%     65.3     55.9  BURNING     2350ms BREACH   -
```
(Measured 3 min 30 s after the deploy on an estate reset ~1 min before it. On an estate that has run >10 min the
10 m column is lower, about 1.8% / burn ~18 for payments at 3 min: still BURNING. The director will not run
this audit before 150 s after the deploy, so it cannot show FAST from being early.)
Say: *"Payments is burning its 0.1 percent budget at sixty times the rate that would spend it evenly
over the month; orders and web are over the slow-burn line. The burn-rate maths is the policy node's, the
ratios are the metrics node's, the burning edge is the traces node's. The graph only did the join.
Windows are two and ten minutes because this estate is minutes old; the thresholds are the standard
ones, and shorter windows make the signal noisier, not stricter."* Enter.

```
mesh audit posture
```
```
posture audit · 5 findings · rules sql_tls, no_public_db, owner_required, incidental_approved, freeze_deploys, write_tools_human
  high   sql_tls              payments -> orders-db              SQL over port 5432 without TLS
         graph: carrying 18.5 req/s right now; 3 services behind it
  medium incidental_approved  billing-records -> orders-cache    incidental read found in traces, never approved by the owner
  high   no_public_db         orders-db on db-prod-17            database host has a public IP
         graph: failover: ... [logs-node, 51s ago]; cpu 94% on db-prod-17  [metrics-node, 1s ago]
  medium owner_required       search-indexer                     no owner recorded
  info   write_tools_human    topology-node.record_deploy        write tool exposed; requires approved_by (verified by its own refusal path)
```
Walk two: the SQL edge without TLS, annotated with the live rate and the three services behind it
(*"the policy node asked the topology node; nothing new was built"*), and `write_tools_human`
(*"the audit reads the mesh's own tool annotations; the only write tool is the one that refused me
in beat one"*).

Close: *"Everything you saw was read-only tools, a bus and one graph. The model is optional."*
The director resets chaos on exit. Back to the deck.

## If there is time, or in Q&A (`--extended` adds these to the director)

```
mesh ask "is orders safe to cut over right now?"
```
```
  topology   orders is in wave 4 · 1 dependents                                             ok
  database   orders-db on db-prod-17 · replica lag 0 s                                      ok
  kafka      orders.events · producers ['orders'] + consumers ['payments', 'search-indexer', 'notifier'] co-waved ok
  cache      orders-cache · auth PR merged · endpoint flipped                               ok
  policy     freeze: none                                                                   ok
  note       billing-records reads orders-cache incidentally; it is in wave 5
   GO, with notes   5 nodes · 0 tabs
```
The slide-19 console, for real. Then `mesh chaos freeze on && mesh deploy web v9 --approved-by <you>`
and `mesh audit posture` gains a sixth finding: *web v9 deployed during freeze*. Other spares:
`mesh ask "who depends on orders-db"` (blast radius with live error rates), `mesh events -n 10`
(the bus as a timeline), `mesh ask --llm "why is orders slow"` only with a key and wifi, and say
it only sees the read-only tools.

## Timing, risks and recovery

| risk | what you see | what to do |
|---|---|---|
| rehearsal leaked into the graph | preflight FAIL `clean state`; beat 1 opens with a cause | `make reset`, wait 30 s, `mesh preflight` |
| CLI on the wrong bus | preflight FAIL `bus round-trip`; pane B silent | docker: `export BUS_URL=redis://localhost:6379/0`; local: unset it |
| beat 1 evidence thin after 10 s | three nodes, not four | the director waits for all four; manually, give it 15 s more |
| second alert does not appear | no PAGE after `chaos cpu` | manual mode: `mesh chaos cpu` now waits for the `cleared` edge itself; watch pane B |
| a node died (`mesh status` red dot) | one source missing from `sources` | `docker compose restart <node>`; facts come back within 10 s |
| the alert engine cannot reach a node | `PAGE … the graph could not decide … failing open` | say it out loud: failing open is the designed behaviour |
| laptop dies | nothing | `asciinema play demo.cast` recorded with `mesh rehearse` |
| conference wifi | nothing needs the network; images are pre-pulled; `--llm` is never on the critical path |

Record the fallback the evening before: `asciinema rec --overwrite -i 2 --window-size 160x45 -c "mesh rehearse" demo.cast`;
play it with `asciinema play -s 2 demo.cast`.
`mesh rehearse` is `mesh demo --auto --extended`: the whole flow unattended in about 2.5 min of
machine time (it waits for the events, plus until 150 s after the deploy before the SLO audit).

## Between rehearsals

`make reset` clears the control files, has the estate empty its telemetry stores (so no rehearsal sits in
any 2/5/10-minute window), and restarts the nodes and the graph, so facts, slow-edge markers, baselines
and cooldowns start clean. `mesh preflight` exits non-zero on NO-GO. Wait 30 s, run `mesh preflight`.
Run the demo twice the day before; the second run is your timing reference.
