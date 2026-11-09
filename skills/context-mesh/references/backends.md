# Wrapping real backends

The node's tools and events do not change when the store does. Only the three or four helper
functions that fetch data do. Replace the SQLite helpers in the template with the calls below.

## Metrics

**Prometheus / Mimir / Thanos / VictoriaMetrics** - `GET /api/v1/query` with `query=` and `time=`.

```
rate:        sum(rate(http_requests_total{service="$s"}[$w]))
error_rate:  sum(rate(http_requests_total{service="$s",code=~"5.."}[$w])) / sum(rate(http_requests_total{service="$s"}[$w]))
p99:         histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket{service="$s"}[$w]))) * 1000
gauge:       last value of node_cpu_seconds_total-derived usage, pg_replication_lag_seconds, etc.; trend from two points $w apart
series:      /api/v1/query_range with step=$step for sparklines and burn windows
```

Use the service label your estate actually has (`service`, `app`, `job`); put it in one place.

**Datadog** - `/api/v1/query` with `from`, `to`, `query=sum:trace.http.request.errors{service:$s}.as_rate()`;
same tool names, different query strings.

**Google Cloud Monitoring** - MQL or PromQL via `projects.timeSeries.query`; replica lag for Cloud SQL is
`cloudsql.googleapis.com/database/replication/replica_lag` (seconds; `database/postgresql/replication/replica_byte_lag` for bytes).

Thresholds for the loop: error ratio > 2 % with at least 50 requests in the window; p99 above the
SLO's latency target; cpu > 85 %; replica lag > 30 s.

## Logs

**Loki** - `GET /loki/api/v1/query_range` with `{service="$s"} |= "error"` (or `| json | level="error"`),
`limit`, `direction=backward`. `for_trace`: `{service=~".+"} |= "$trace_id"`. Bursts:
`sum(count_over_time({service="$s"} |= "error" [1m]))` now vs one minute ago.

**Elasticsearch / OpenSearch** - `_search` with a `range` on `@timestamp`, `term` on `service`
and `level`, sorted desc. `for_trace`: `term` on `trace_id` (or `trace.id` in ECS).

**Cloud Logging** - `entries.list` with a filter `resource.labels.container_name="$s" severity>=ERROR`.

Watch-words are a list in the node, not a dashboard: `failover`, `promoted`, `deadlock`, `OOMKilled`,
`rollout complete`. Publish the line itself with the keyword.

## Traces

**Tempo** - TraceQL via `/api/search?q={resource.service.name="$s" && duration > 1s}` (raise the floor
until a handful come back, then take the longest); `/api/traces/{id}` for one trace. Per-edge RED: `{span.kind="client" && resource.service.name="$a"}`
grouped by `span.peer.service` (or compute from the service graph metrics Tempo's metrics-generator
emits: `traces_service_graph_request_total`, `_failed_total`, `_server_seconds_bucket`).

**Jaeger** - `/api/traces?service=$s&limit=&minDuration=`; dependencies from `/api/dependencies`.

**OpenTelemetry collector** - if you own the pipeline, add the `servicegraph` connector and read its
metrics from the metrics store; that gives topology and RED per edge with no extra node code.

Critical path: walk parent -> child picking the longest child at each level; that is what humans
read first and what the correlated trio shows.

## Topology

This is the hard node to source and the one that multiplies the others. In order of preference:

1. A service catalog / portal (Backstage, OpsLevel, Cortex, an internal portal): owners, tiers,
   dependencies declared by teams.
2. A CMDB or cloud inventory (GCP Asset Inventory, AWS Config): hosts, databases, networks, public IPs.
3. Derived from traces: `servicegraph` metrics or `edge_latency` give you edges nobody declared -
   mark those `incidental` and let the posture audit ask for approval.
4. Kubernetes: labels (`app.kubernetes.io/name`, team annotations), Services, NetworkPolicies.
5. A migration tracker or cutover ledger, during a migration: waves, CNAME state, readiness.

Merge them in the node, keep the merge rules in the node's README, refresh on a schedule (a cron
that rewrites `topology.yaml` or a cache is fine), and tag every field with where it came from.
Deploys: CI webhooks, Argo/Flux events, or the `rollout complete` watch-word from logs.

## Policy

A git repo of YAML: SLOs per service (availability, latency target, budget window), burn windows,
posture rules as sentences, alert decision rules as sentences, freeze windows. The node reads the
file; `decide` and `evaluate` are pure functions over facts handed in. Keep the policy node free of
any telemetry client so it can be reviewed by people who never touch Grafana.

## Profiles (optional sixth node)

**Pyroscope / Parca** - `hottest(service, window_s)` from the top of the flame graph; `diff(service, before, after)`
to publish `context.profiles.regression` when a deploy changes the top frames. Only worth wrapping
once the first five nodes answer the 2 AM question.

## Grafana itself

Grafana is a client, not a store. Point its alert rules at the graph's HTTP API (`/api/explain/...`)
in the alert annotations, or run the alert engine on the mesh and let Grafana render
`/api/alerts`. Do not wrap Grafana as a node; wrap the datasources behind it.
