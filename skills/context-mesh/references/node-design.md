# Node design

A node is a thin MCP server in front of one signal store. It answers a few questions well, it
publishes context when a value crosses a line, and it does not know the rest of the mesh exists.

## Contents

1. Tool shape and naming
2. The result envelope
3. Resources
4. Thresholds and the background loop
5. Annotations and the write-tool rule
6. Transport and ports
7. Sizing and performance
8. Per-signal tool sets that have worked

## 1. Tool shape and naming

Name the tool after the question, parameterise by subject and window, return a flat dict.

```
error_rate(service, window_s=300)       -> ratio, requests, errors
p99(service, window_s=300)              -> p50_ms, p99_ms, samples
gauge(name, label, value, window_s=30)  -> value, trend (rising|falling|flat)
slowest(service, window_s=300, limit=3) -> traces with critical paths
for_trace(trace_id)                     -> the log lines on that trace
dependents(node, depth=3)               -> services with wave and owner
```

Avoid: `query(promql)`, `search(lucene)`, `sql(text)`. A raw-query tool couples every client to
one backend and tempts an agent to write queries nobody can review. If someone insists on an
escape hatch, make it a separate node, mark it clearly, and keep it out of the graph's routes.

Keep parameters primitive (str, int, float). Windows are seconds and explicit. Limits default
small (3-10); the graph asks for more when it needs it.

## 2. The result envelope

Every tool returns a dict with at least:

```json
{"...": "the answer fields...", "source": "metrics-node", "ts": 1760000000.0}
```

`source` is the node's name, `ts` is when the fact was true (the newest sample's timestamp, not
"now", when you have it). Add `window_s` whenever the answer depends on a window. Nested
objects are fine for lists (traces, lines, edges) but keep the top level flat so a client can
render it without knowing the node.

Return the structured result from the function; the SDK serialises it. If the SDK you use does
not populate structured content, clients fall back to parsing the text block as JSON - keep the
dict JSON-serialisable (no datetimes, no numpy types).

## 3. Resources

One resource per node is enough, for the thing a client wants to read as a document rather than
query: `service-map://{service}` (topology), `runbook://{alert}` (policy), `dashboard://{service}`
(metrics, a URL plus a one-paragraph description). Resources are for reading; tools are for asking.

## 4. Thresholds and the background loop

The loop is what makes the node a signal processor and not a query proxy. Run it in a daemon
thread (or an asyncio task) started before the server:

```
every 5 s:
  for each subject the node watches:
    value = <the same code path the tool uses>
    edge(key, value > line, topic, subject=..., value=...)
```

`edge()` remembers the last state per key and publishes only on a change: `topic` on the rising
edge, `topic + ".cleared"` on the falling edge. Keep thresholds in one place at the top of the
file; make the window short (1-2 minutes) so events arrive while the symptom is fresh, and
require a minimum sample count so an empty window cannot fire.

What to watch, per node:

- metrics: error ratio per service, p99 per service, host gauges (cpu, replica lag, disk)
- logs: error bursts (count in the last minute vs the previous, with a floor), watch-words
  (`failover`, `deadlock`, `OOM`, `deploy`) published with the line itself
- traces: per-edge p99 against a slowly-moving baseline (EWMA while healthy, frozen while not)
- topology: a new deploy, a changed owner, a new edge discovered in traces
- policy: freeze on/off, a rule changed

## 5. Annotations and the write-tool rule

Annotate every tool. With the mcp Python SDK 2.x:

```python
from mcp.types import ToolAnnotations
READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)
WRITE     = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)

@server.tool(annotations=READ_ONLY)
def error_rate(service: str, window_s: int = 300) -> dict: ...
```

(SDK 1.x spells these `readOnlyHint`, `idempotentHint`, `openWorldHint`, `destructiveHint`.)

A write tool, if one exists at all, takes `approved_by: str = ""` and returns
`{"ok": false, "error": "...pass approved_by=<human>"}` when it is empty. Do not hide the tool;
the posture audit lists every tool whose `read_only_hint` is false and checks that it has an
approver path. An agent calling the write tool without an approver gets a refusal it can show
the human, which is the whole point.

## 6. Transport and ports

Streamable HTTP, one well-known port per node (8100 graph, 8101 metrics, 8102 logs, 8103
traces, 8104 topology, 8105 policy is a fine convention). Bind `0.0.0.0` inside containers.
If the SDK has DNS-rebinding protection, disable it for container-to-container calls or set
the allowed hosts; otherwise `graph -> metrics:8101` is rejected with a confusing 421.

stdio transport is fine for a node a single local agent uses; the mesh needs HTTP so the graph,
the CLI and the alert engine can all be clients at once.

## 7. Sizing and performance

A node is 80-150 lines. If it is longer, it is doing the graph's job. Open one connection to the
store at startup and reuse it; the background loop and the tools share it (SQLite: WAL mode and
`check_same_thread=False`; HTTP backends: one client with a short timeout). Cap every query by
window and limit so a bad client cannot make the node scan a day of data.

## 8. Per-signal tool sets that have worked

| node | tools | resource | publishes |
|---|---|---|---|
| metrics | rate, error_rate, p99, gauge, series | dashboard://{service} | error_rate, latency, cpu_high, replica_lag |
| logs | errors, search, for_trace | - | error_burst, keyword |
| traces | trace, slowest, edge_latency | - | slow_edge |
| topology | map, neighbours, dependents, owner, host_of, deploys, record_deploy (write) | service-map://{service} | deploy |
| policy | freeze_active, slo, burn, rules, evaluate, decide | runbook://{alert} | freeze |
| profiles | hottest(service), diff(service, before, after) | - | regression |

`decide` and `evaluate` on the policy node take facts as arguments and read no telemetry; that
is what lets "the policy node asks the topology node" happen through the graph with nothing new built.
