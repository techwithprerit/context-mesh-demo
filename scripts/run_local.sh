#!/usr/bin/env bash
# Run the whole mesh on one machine without Docker: file bus, SQLite stores, six processes.
# Usage: scripts/run_local.sh [up|down|restart-nodes|logs]
set -euo pipefail
cd "$(dirname "$0")/.."
export MESH_DATA="${MESH_DATA:-./data}"
export TELEMETRY_DB="${TELEMETRY_DB:-$MESH_DATA/telemetry.db}"
export BUS_URL="${BUS_URL:-file://$MESH_DATA/bus.jsonl}"
mkdir -p "$MESH_DATA/logs"

PROCS=(mesh.estate mesh.nodes.metrics_node mesh.nodes.logs_node mesh.nodes.traces_node mesh.nodes.topology_node mesh.nodes.policy_node mesh.graph)

case "${1:-up}" in
  up)
    [ -f "$MESH_DATA/pids" ] && { echo "already running (pids file exists); run: $0 down"; exit 1; }
    rm -f "$MESH_DATA/telemetry.db"* "$MESH_DATA/bus.jsonl" "$MESH_DATA/chaos.json" "$MESH_DATA/deploys.json" "$MESH_DATA/freeze.json"
    : > "$MESH_DATA/pids"
    for m in "${PROCS[@]}"; do
      nohup python3 -m "$m" > "$MESH_DATA/logs/${m##*.}.log" 2>&1 &
      echo $! >> "$MESH_DATA/pids"
    done
    echo "mesh starting (7 processes). logs in $MESH_DATA/logs. give it ~20 s, then: python3 -m mesh.cli status"
    ;;
  down)
    [ -f "$MESH_DATA/pids" ] && xargs kill < "$MESH_DATA/pids" 2>/dev/null || true
    rm -f "$MESH_DATA/pids"; echo "mesh stopped"
    ;;
  restart-nodes)   # keep the estate (and its history) running; restart the five nodes and the graph
    for m in "${PROCS[@]:1}"; do pkill -f "python3 -m $m" || true; done
    sleep 1
    grep -v "^$" "$MESH_DATA/pids" | head -1 > "$MESH_DATA/pids.new"; mv "$MESH_DATA/pids.new" "$MESH_DATA/pids"
    for m in "${PROCS[@]:1}"; do
      nohup python3 -m "$m" >> "$MESH_DATA/logs/${m##*.}.log" 2>&1 &
      echo $! >> "$MESH_DATA/pids"
    done
    echo "nodes and graph restarted; give it ~30 s"
    ;;
  logs)
    tail -n 20 "$MESH_DATA"/logs/*.log
    ;;
esac
