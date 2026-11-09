---
name: context-mesh
description: Design and build a context-driven telemetry mesh - MCP nodes in front of logs, metrics, traces, profiles, policies and topology, a bus of context events, and one context graph that explains incidents, decides whether an alert should page, and audits SLOs and security posture. Use this whenever someone wants to wire observability signals together with MCP, build an on-call or SRE agent, correlate logs with metrics and traces, make alerts context-aware instead of threshold-based, put an MCP server in front of Prometheus, Loki, Tempo, Grafana, Datadog, a service catalog or a CMDB, or asks for "AI for observability", "agentic SRE", "AIOps" or "an agent that understands production" - even when they do not say "mesh" or "MCP".
---

# Context mesh

A context mesh turns disconnected telemetry into something that can explain itself. Each signal
store gets a thin MCP node; nodes publish small context events onto a bus; the bus feeds one
graph of who-calls-whom annotated with live facts; and the graph is the only thing a human, an
agent or an alert rule ever asks. The point is not another dashboard that shows. It is a layer
that understands which signal belongs to which, and says so with sources and ages attached.

```
   logs  metrics  traces  profiles  policy  topology      <- the stores you already pay for
     |      |       |        |        |        |
   node   node    node     node     node     node        <- one MCP server each, read-only, 2-3 tools
     \      |       |        |        |       /
      ------------- bus (context.* events) -----------    <- facts, published on the rising edge
                          |
                    context graph                         <- nodes + edges + facts with ages
                   /      |       \
              explain   decide   audit                    <- the only three things anyone asks
```

Read this file whole before building. The deep dives below ship with the skill bundle
(`context-mesh.skill`) and in the demo repo under `skills/context-mesh/references/`; if they are not
beside this file, the rules and build order here are enough to work from.

- `references/node-design.md` - shaping one node: tools named like questions, the result envelope, thresholds, the write-tool rule.
- `references/events-and-bus.md` - the context event schema, topic naming, rising-edge publishing, bus choices.
- `references/graph-assembly.md` - facts with ages, RED per edge, `explain`, the alert decision, SLO and posture audits.
- `references/backends.md` - Prometheus, Loki, Tempo, OpenTelemetry, Datadog, service catalogs, Kubernetes, with query snippets.
- `scripts/node_template.py` - a complete, runnable node (mcp SDK 2.x, streamable HTTP). Copy it rather than rewriting it.

## The rules, and why each one exists

These came out of running a mesh through a one-night cloud migration and an on-call rotation.
Each rule is a scar. Keep them unless you have a better scar.

1. **One node reads one store.** A node that reads two stores is a graph in disguise, and now
   there are two graphs that disagree. The join happens in the graph, nowhere else.
2. **Tools are questions, not query languages.** `error_rate(service, window)` beats
   `promql(query)`. The caller should never need to know which backend answers. If you expose
   a raw query tool, every client becomes coupled to that backend and the node is pointless.
3. **Read-only by default; a write is a proposal until a human approves it.** Annotate every
   tool with `readOnlyHint`. The one write tool you allow takes an `approved_by` argument and
   refuses without it. The first time an agent could flip DNS during a cutover, the tool was
   removed the same day; that is where this rule comes from.
4. **No provenance, no verdict.** Every tool result carries `source` (the node name) and `ts`
   (when the fact was true). The graph renders ages on every line it shows. An answer without
   provenance is a nicer-looking guess, and at 3 AM "trust me" is not a feature.
5. **Probes refresh the graph; humans never edit it.** Hand-filled fields are wrong by Tuesday.
   Facts expire (15 minutes is a good default); if nobody re-asserted it, it is history.
6. **Publish on the rising edge.** A node emits one event when a value crosses a line, and one
   `.cleared` when it comes back. Not one per poll. The bus carries facts, not samples.
7. **Cache the graph, not the answers.** Most questions are a graph lookup that never needs a
   model. Keep the deterministic `ask` route as the front door; a model driving the same
   read-only tools is a side door, never on the critical path.
8. **A threshold is a question before it is a page.** When a metric crosses a line, the graph
   asks the policy node whether the facts explain it (a failover ten seconds ago explains a hot
   database host). Suppress with a reason and a re-check time, or page with host, dependents,
   owner, runbook and sources. Both say why.

## How to build one

Work in this order. Each step is demonstrable on its own, which is how you keep momentum and
how you find out early that a store cannot answer the question you thought it could.

### 1. Inventory the signals and pick the first node

List every signal store and the one question each is best at. Pick the first node by value,
not ease: metrics (RED per service) and topology (who calls whom) multiply every other node;
logs are the easiest to wrap and the least useful alone. A good first weekend is metrics plus
topology; everything else is the second weekend.

Ask the user, if it is not obvious: which stores exist, whether a service catalog or CMDB exists
(topology is the hard node to source), what the painful question at 2 AM actually is, and whether
any write action is in scope at all (default: none).

### 2. Shape each node

Two or three tools and one resource per node. Name tools after the questions people ask
(`slowest(service)`, `for_trace(trace_id)`, `dependents(node)`), keep parameters simple
(service, window_s, limit), return a flat dict with `source` and `ts`. Put the thresholds and
the publishing in a background loop inside the node, so the node is a signal processor and not
only a query proxy. Full guidance and the result envelope: `references/node-design.md`.
Start from `scripts/node_template.py`.

### 3. Define the events and put a bus between nodes

Nodes never call each other. They publish `context.<node>.<kind>` events with the subject
(service, edge, host), the value, the source and the timestamp. Any queue you already run
works (Redis pub/sub, NATS, Kafka, Pub/Sub); for a laptop, an append-only JSONL file tailed by
subscribers is enough and has zero infrastructure. Schema, naming and the rising-edge rule:
`references/events-and-bus.md`.

### 4. Build the graph

The graph bootstraps from the topology node (nodes and typed edges), decorates edges with RED
from the traces or metrics node on a short poll, attaches bus events as facts with a TTL, and
exposes three families of tools: `explain(service)`, `decide(alert, facts)` via the policy
node, and audits (`slo_status`, `posture_audit`, `cutover_check`, `blast_radius`). Expose it
twice: as an MCP server for agents and alert rules, and as a read-only HTTP API for dashboards
and scripts. Assembly rules, the cause-precedence logic and the answer template:
`references/graph-assembly.md`.

### 5. Make alerts ask first

Route the metrics node's threshold events through the graph's alert engine before any pager.
The engine gathers the host or service facts (and actively asks the logs node for things the
bus may not have delivered yet), hands them to `policy.decide`, and publishes
`context.alert.suppressed` or `context.alert.page`, each with a reason and sources. Keep the
decision rules in a policy file as sentences, not regexes; they will be read by humans in a
post-mortem.

### 6. Put SLOs and posture on the same graph

Burn rate is policy-node maths over metrics-node ratios; the burning edge is traces-node data;
the graph only joins them. Posture rules (SQL edges must use TLS, databases have no public IP,
every service has an owner, incidental reads need approval, no deploys during a freeze) run
over the topology map plus recent facts. Add one rule that reads the mesh's own tool
annotations and flags any tool without `readOnlyHint` - the audit should find the write tool
you allowed in step 2.

### 7. Ship it so someone else can run it

A `docker compose up` with one container per node, a bus and the graph; a CLI with `status`,
`explain`, `watch`, `audit`, `ask` and the chaos knobs; and a `rehearse` command that runs the
whole story unattended, which doubles as the smoke test and the demo fallback recording. Keep
every node under ~150 lines so the reader can see it is not a platform project.

## The answer every assembly returns

Whatever the question, the graph answers in this shape. Clients render it; humans learn to
read it fast; a model summarising it cannot drop the provenance.

```
cause        <one sentence, with the triggering fact and its timing>   confidence high|medium|low
action       <one sentence; if it is a write, say "needs a human approver">
hot edges    <edge  rate  error%  p99  vs baseline>
evidence     <oldest first, one fact per line, each ending "[node, age]">
correlated   one request: the metric, the trace's critical path, the log lines on that trace id
sources      <node: age> for every node consulted
```

If there is no explaining context, say so in the cause line and still show what changed. A
confident "ok" from a stale cache is worse than "no explanation in the last 15 minutes".

## Node review checklist

Run through this before calling a node done:

- Reads exactly one store; the backend name does not appear in any tool name or parameter.
- Every tool has a one-line docstring that reads as a question a human would ask.
- Every result carries `source` and `ts`; windows are explicit (`window_s`), never implied.
- Thresholds live in a background loop; events publish on the rising edge with a `.cleared` twin.
- All tools annotated read-only, or the write tool takes `approved_by` and refuses without it.
- Runs with one command, on one port, and `list_tools` from any MCP client shows the annotations.
- Nothing in the node knows the graph exists.

## Anti-patterns to steer away from

- A "universal query" tool that takes PromQL/LogQL/SQL strings. That is the backend with extra steps.
- A node that enriches its own results by calling another node. The join belongs in the graph.
- Polling-interval events ("cpu is 0.31", "cpu is 0.32"). Publish crossings, not samples.
- Letting the model decide whether to page. The policy node decides from facts; the model, if
  present at all, narrates.
- Hand-maintained topology with no refresh job. If it cannot be probed, mark it as asserted-by-
  human with a date, and let the posture audit nag about it.
- Hiding the write tool. Expose it honestly, annotate it, and let the audit find it.

## When the user only wants part of this

A single node in front of one store is a complete, useful deliverable: ship it with the result
envelope and the rising-edge events even if no bus exists yet, so the mesh can grow around it.
A graph with no bus (facts arriving only by polling) is also fine for a first week. What is not
fine is skipping provenance or the read-only rule to move faster; those are the two things that
cannot be retrofitted once people trust the answers.
