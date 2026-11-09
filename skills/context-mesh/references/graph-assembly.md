# The context graph: facts, RED, explain, decide, audit

The graph is the one component that joins. It owns no telemetry; it owns the shape of the estate
and a short memory of what the nodes said.

## Contents

1. Data model
2. Bootstrapping and keeping warm
3. `explain(service)` - assembling a cause
4. The alert engine - `decide`
5. Audits on the same graph
6. Routing plain-words questions
7. Exposure: MCP and HTTP
8. Limits that bit, and the rule each left

## 1. Data model

```
node  = {name, kind: service|database|cache|topic|host, meta: {owner, wave, tier, ...}, facts: [...]}
edge  = {from, to, kind: http|sql|cache|kafka|hosted_on, port, tls, ..., red: {rate_per_s, error_ratio, p50_ms, p99_ms, ts}, slow?: {p99_ms, baseline_ms, ts, sample_trace}}
fact  = {kind, source, ts, ...subject and value keys..., cleared?: true}
```

Facts attach to the subject node (service, host). Edge-shaped facts (`slow_edge`) attach to the
callee node and also decorate the edge. "Latest wins" for gauge-like kinds (`cpu_high`,
`replica_lag`); append for event-like kinds (`deploy`, `error_burst`), capped per node (40).
A fact older than `RECENT_S` (15 minutes) or marked `cleared` is history: never shown as context,
kept only in the event ring.

Dedupe: the same kind, same message, within a few seconds, from the bus and from a direct lookup
is one fact.

## 2. Bootstrapping and keeping warm

- Startup: call `topology.map()` (retry for a minute; the node may be slower to come up).
  Build nodes and typed edges; add `hosted_on` edges from database to host. List every node's
  tools and keep their annotations (`node_tools`) for the posture audit.
- Every 10 s: `traces.edge_latency(window_s=120)` and decorate edges with RED. If traces are
  sampled too hard to trust rates, take rate and error ratio from `metrics` per service and p99
  from traces.
- Subscribe to `context.*`. Route by topic to `fact(...)`; handle `.cleared` by marking.
- Do not persist the graph; it is rebuilt from the nodes in under a minute and that is a feature
  (nothing hand-edited can survive a restart).

## 3. `explain(service)`

Scope: the service, its upstream and downstream neighbours, and the hosts of its downstream
databases. Gather recent facts across the scope, oldest first; gather edges touching the service
that have RED; mark hot edges (`error_ratio > 1%` or a `slow` annotation).

Cause precedence (first match wins; "confidence" follows):

1. A deploy in scope, with the FIRST symptom (`slow_edge`, `error_burst`, `error_rate`,
   `latency`) at or after the deploy minus 30 s -> cause: that deploy, with the delta in seconds.
   Action: roll back (a write: "needs a human approver"). Confidence high.
   Pick the latest deploy that precedes the first symptom, not the latest deploy overall -
   otherwise an unrelated deploy after the incident started steals the blame.
2. A failover on a host in scope and replica lag still above zero -> cause: catch-up after
   failover. Action: none, re-check when lag is under the line. Confidence high.
3. Otherwise: "no explaining context in the last 15 minutes; here is what changed". Action:
   investigate from the evidence. Confidence low. Still show everything.

Correlated trio (the demo's money shot): `metrics.error_rate(service, 300)`,
`traces.slowest(service, 300, 1)` for the critical path, `logs.for_trace(trace_id)` for the lines
on that exact trace. If the slowest trace has no log lines (a slow but successful request), fall
back to the latest error lines in scope so the trio is never empty when the service is unhealthy.

Sources: the max age per node consulted, including the nodes behind the facts. Render as
`node: age`.

Return the answer template from SKILL.md. Every evidence line ends `[source, age]`.

## 4. The alert engine: `decide`

Trigger: a `context.metrics.<kind>` event the policy file has a rule for (`cpu_high`,
`replica_lag`, `error_rate`).

```
on event:
  facts = latest gauge for the subject (ask metrics directly; the event may be 5 s old)
        + recent facts on the subject (failover, deploy, lag)
        + if a needed fact is missing, ask the node that would know (logs.search("failover"))
  verdict = policy.decide(alert_kind, facts)          # the policy node reads no telemetry
  if suppress: publish context.alert.suppressed {reason, recheck_s, rule, sources}
  else:        publish context.alert.page {reason, owner, runbook, action, databases, dependents, host_facts, sources}
  append to the alerts ring; expose GET /api/alerts
```

Policy rules are sentences in a file:

```yaml
alert_rules:
  cpu_high:
    suppress_if: "replica catch-up in progress on the same host after a failover in the last 15 min"
    recheck_s: 300
```

`decide` turns the sentence into a check over the facts it was handed. When it pages, the reason
must say what it looked for and did not find ("a failover 70 s ago is on record but catch-up is
done; this CPU has no explanation"), so the human knows the rule ran.

Re-check: a suppressed alert is re-evaluated after `recheck_s`; if the value is still over the line
and the explaining fact is gone, it pages then (marked as a re-check); if a newer decision already
paged for that subject, the re-check says so and stops. Suppression is a delay with a reason, not
a mute. Fail open: if the engine cannot reach a node, it pages with "could not decide" as the reason
rather than staying silent.

## 5. Audits on the same graph

- `slo_status`: per SLO'd service, error ratio over a short and a long window from metrics,
  `policy.burn(service, short, long)` for the burn rates and status, p99 against the latency
  target, and the edge with the highest error ratio downstream as the "burning edge". Use the
  standard multi-window thresholds (14.4 fast, 6 slow for a 30-day budget); scale the windows
  down only for demos with minutes of history, and say so in the output.
- `posture_audit`: `policy.evaluate(service_map, deploys, node_tools)`. Rules that earn their
  place: SQL edges without TLS, databases with public IPs, services without owners, incidental
  reads never approved, deploys during a freeze, write tools without an approver path. Annotate
  each finding with graph context ("carrying 18 req/s right now; 3 services behind it") so the
  security finding and the latency story are visibly the same graph.
- `cutover_check(service)` for migrations: topology (wave and dependents' waves), database
  (host and lag), kafka (producers and consumers co-waved), cache (auth and endpoint state),
  policy (freeze). Verdict GO / GO with notes / NO-GO; notes carry the incidental dependencies
  nobody's dashboard shows.
- `blast_radius(node)`: transitive dependents with owner, wave, current error ratio and facts.

## 6. Routing plain-words questions

`ask(question)` is deterministic: match node names in the question (longest match wins, so
`orders-db` beats `orders`), then route on keywords - cut over / safe to -> cutover_check;
blast / depend / affected -> blast_radius; slo / budget / burn -> slo_status; posture / audit /
secur -> posture_audit; otherwise explain. This answers every demo question without a model and
is what the alert rules call.

A model, if used, gets the same read-only tools (filter on the annotation) and a system prompt
that requires citing node and age for every fact. It narrates; it does not decide.

## 7. Exposure: MCP and HTTP

Serve the graph as an MCP server (tools above) and add read-only HTTP routes beside it:
`GET /api/graph`, `/api/events`, `/api/alerts`, `/api/explain/{service}`. Dashboards, Slack bots
and scripts use HTTP; agents and alert rules use MCP; both see identical answers.

## 8. Limits that bit, and the rule each left

| what went wrong | rule |
|---|---|
| hand-filled graph fields were wrong by Tuesday | probes refresh; humans never edit; facts expire |
| a node answered "ok" from a cached value during a failover | every answer carries sources and ages; no provenance, no verdict |
| five node calls per question got expensive over an 8-hour night | cache the graph, not the answers; most questions never need a model |
| an agent had a tool that could flip a CNAME | read-only by default; a write is a proposal until a human approves |
| the bus delivered the metric event before the log fact | the decider asks the node directly for anything it needs and does not have |
| a later, unrelated deploy was blamed for an incident | cause = latest deploy before the FIRST symptom |
