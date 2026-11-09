# Context events and the bus

Nodes never call each other. They publish facts; the graph subscribes. This file is the contract.

## The event

One JSON object per event, flat, small (under 1 KB), with these keys always present:

```json
{
  "topic":  "context.traces.slow_edge",
  "source": "traces-node",
  "ts":     1760000000.0,
  "from":   "orders",  "to": "payments",
  "p99_ms": 1817, "baseline_ms": 65,
  "sample_trace": "03fc6fb024d744f2"
}
```

- `topic` is `context.<node>.<kind>`; the `.cleared` suffix marks the falling edge of the same kind.
- `source` is the node name; `ts` is when the fact was true.
- The subject is explicit: `service`, `instance`, `host`, `from`/`to` for an edge, `topic` for a
  Kafka topic. Use the same subject names the topology node uses, or the graph cannot attach the
  fact to a node.
- The value is explicit and numeric where it is a number (`value`, `p99_ms`, `count_1m`).
- A pointer to evidence when one exists: `sample_trace`, `sample` (one log line), `trace_ids`.

Never put the whole payload of the store in an event. An event is a pointer plus a verdict; the
graph asks the node for detail when it assembles an answer.

## Topic catalogue (starting set)

```
context.metrics.error_rate     service, value, window
context.metrics.latency        service, p99_ms
context.metrics.cpu_high       instance, value
context.metrics.replica_lag    host, value, trend
context.logs.error_burst       service, count_1m, previous_1m, sample, trace_ids
context.logs.keyword           keyword, service, msg
context.traces.slow_edge       from, to, p99_ms, baseline_ms, sample_trace
context.topology.deploy        service, version, approved_by
context.policy.freeze          active, reason, since
context.alert.suppressed       alert, instance, reason, recheck_s, rule, sources
context.alert.page             alert, instance, reason, owner, runbook, action, dependents, sources
```

Every `context.metrics.*` and `context.traces.*` kind has a `.cleared` twin with the same subject keys.

## Publish on the rising edge

```
state = {}
def edge(key, firing, topic, **payload):
    was = state.get(key, False)
    if firing and not was:   publish(topic, **payload)
    elif was and not firing: publish(topic + ".cleared", **payload)
    state[key] = firing
```

This is the single most important discipline on the bus. Without it the bus is a second
metrics pipeline and the graph drowns. With it, a quiet estate publishes nothing, and every
event is news. Add a per-key cooldown (5 minutes) for kinds that can flap (bursts, slow edges).

Consequence to design around: a subscriber that starts late has missed the rising edge. The
graph therefore (a) bootstraps state by asking nodes directly at startup, and (b) when it must
decide something, actively asks the relevant node for facts the bus may not have delivered yet
(the alert engine asks the logs node for `failover` before deciding on a hot host).

## Bus choices

| bus | when | notes |
|---|---|---|
| JSONL file, tailed | laptop, demo, one host | zero infra; subscribers seek to end then tail; good enough for a conference |
| Redis pub/sub | small team, docker compose | `psubscribe("context.*")`; no replay, which matches the rising-edge model |
| NATS / JetStream | several teams, need replay | subjects map 1:1 to topics; JetStream gives late subscribers the last N |
| Kafka / Pub/Sub | the estate already runs it | one topic `context`, the event's `topic` as a header or key; retention gives you an audit log for free |

Put the backend behind one `Bus` class with `publish(topic, **payload)` and
`subscribe(pattern, handler)`; nodes and the graph import that and nothing else. Switching from
the file bus to Redis should be an environment variable.

## Replay and audit

Keep every event the graph sees in a bounded ring (500 is plenty) and expose it as
`events(limit)` on the graph and `GET /api/events`. In a post-mortem, the sequence
`deploy -> slow_edge -> latency -> error_burst -> alert.page` with timestamps is the timeline,
already written. If the bus has retention (Kafka, JetStream), that is the audit trail auditors
ask for; if it does not, append the ring to a file hourly.
