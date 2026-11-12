"""The synthetic estate: web -> orders -> payments -> orders-db, plus a cache and a topic.

Every second it serves a batch of requests and writes what real services would emit:
spans (traces), log lines (with trace ids) and metric samples. Chaos is read from
data/chaos.json, which the `mesh chaos` command edits. Nothing here knows about the mesh.
"""
from __future__ import annotations

import json
import os
import random
import time
import uuid

import yaml

from . import telemetry
from .common import CHAOS_FILE, DATA, DEPLOYS_FILE, TOPOLOGY_FILE, read_json

WIPE_FILE = os.path.join(DATA, "wipe")  # `make reset` touches it: empty the stores so no rehearsal sits in any window

TICK_S = 1.0
REQ_PER_TICK = 20
BASE_MS = {"web": 6, "orders": 12, "payments": 28, "orders-db": 8, "orders-cache": 2}


def rid() -> str:
    return uuid.uuid4().hex[:16]


class Estate:
    def __init__(self):
        self.conn = telemetry.connect()
        self.topo = yaml.safe_load(open(TOPOLOGY_FILE))
        self.seen_deploys: set[str] = set()
        self.seen_failovers: set[float] = set()
        self.last_prune = 0.0

    # -- chaos -------------------------------------------------------------
    def chaos(self) -> dict:
        return read_json(CHAOS_FILE, {})

    def lag(self, ch: dict, ts: float) -> float:
        fo = ch.get("db-prod-17", {}).get("failover_at")
        if not fo:
            return 0.0
        return max(0.0, 90.0 - 3.0 * (ts - fo))  # catch-up: 90 s of lag draining at 3 s/s (clears in 30 s)

    # -- one request through the estate -------------------------------------
    def request(self, ts: float, ch: dict) -> None:
        trace = rid()
        pay = ch.get("payments", {})
        slow = random.random() < 0.12 and pay.get("p99_ms")
        failed = random.random() < pay.get("error_rate", 0.0)
        pay_ms = (pay["p99_ms"] * random.uniform(0.85, 1.1)) if slow else random.lognormvariate(3.3, 0.25)
        if failed:
            pay_ms = random.uniform(2100, 2400)  # a failed charge is a timed-out charge: always the slowest
        db_ms = random.lognormvariate(2.1, 0.3)
        if self.lag(ch, ts) > 0 or ch.get("db-prod-17", {}).get("cpu"):
            db_ms *= 2.2  # a hot host answers slower
        cache_ms = random.lognormvariate(0.7, 0.2)
        orders_ms = BASE_MS["orders"] + pay_ms + cache_ms + random.lognormvariate(1.5, 0.3)
        web_ms = BASE_MS["web"] + orders_ms + random.lognormvariate(1.2, 0.3)
        st_pay = "error" if failed else "ok"
        st = "error" if failed else "ok"
        web_id, ord_id, pay_id = rid(), rid(), rid()
        spans = [
            (ts, trace, web_id, None, "web", "GET /checkout", "orders", web_ms, st),
            (ts + 0.004, trace, ord_id, web_id, "orders", "POST /orders", "payments", orders_ms, st),
            (ts + 0.006, trace, rid(), ord_id, "orders", "GET cache", "orders-cache", cache_ms, "ok"),
            (ts + 0.009, trace, pay_id, ord_id, "payments", "POST /charge", None, pay_ms, st_pay),
            (ts + 0.012, trace, rid(), pay_id, "payments", "SELECT balance", "orders-db", db_ms, "ok"),
            (ts + 0.015, trace, rid(), ord_id, "orders", "PUBLISH orders.events", "orders.events", 1.5, "ok"),
        ]
        self.conn.executemany("INSERT INTO spans VALUES (?,?,?,?,?,?,?,?,?)", spans)
        metrics = [
            (ts, "http_request_duration_ms", json.dumps({"service": "web"}), web_ms),
            (ts, "http_request_duration_ms", json.dumps({"service": "orders"}), orders_ms),
            (ts, "http_request_duration_ms", json.dumps({"service": "payments"}), pay_ms),
            (ts, "http_requests_total", json.dumps({"service": "web"}), 1),
            (ts, "http_requests_total", json.dumps({"service": "orders"}), 1),
            (ts, "http_requests_total", json.dumps({"service": "payments"}), 1),
        ]
        if failed:
            metrics += [
                (ts, "http_errors_total", json.dumps({"service": "web"}), 1),
                (ts, "http_errors_total", json.dumps({"service": "orders"}), 1),
                (ts, "http_errors_total", json.dumps({"service": "payments"}), 1),
            ]
            self.conn.executemany(
                "INSERT INTO logs VALUES (?,?,?,?,?)",
                [
                    (ts + 0.03, "payments", "ERROR", f"charge failed: upstream timeout after {int(pay_ms)}ms "
                                                       f"(gateway=stripe-sim, retry=0) trace={trace}", trace),
                    (ts + 0.031, "orders", "ERROR", f"POST /orders 502: payments unavailable trace={trace}", trace),
                ],
            )
        elif random.random() < 0.05:
            self.conn.execute("INSERT INTO logs VALUES (?,?,?,?,?)",
                              (ts, "orders", "INFO", f"order created id=o-{rid()[:6]} trace={trace}", trace))
        self.conn.executemany("INSERT INTO metrics VALUES (?,?,?,?)", metrics)

    # -- host-level signals ----------------------------------------------------
    def host_signals(self, ts: float, ch: dict) -> None:
        host = ch.get("db-prod-17", {})
        lag = self.lag(ch, ts)
        cpu = host.get("cpu") or (0.93 + random.uniform(-0.02, 0.02) if lag > 0 else 0.34 + random.uniform(-0.04, 0.04))
        rows = [
            (ts, "node_cpu_ratio", json.dumps({"instance": "db-prod-17"}), cpu),
            (ts, "pg_replica_lag_seconds", json.dumps({"host": "db-prod-17"}), lag),
            (ts, "cache_hit_ratio", json.dumps({"instance": "orders-cache"}), 0.97 + random.uniform(-0.01, 0.01)),
            (ts, "kafka_consumer_lag", json.dumps({"topic": "orders.events"}), 0),
        ]
        self.conn.executemany("INSERT INTO metrics VALUES (?,?,?,?)", rows)
        fo = host.get("failover_at")
        if fo and fo not in self.seen_failovers:
            self.seen_failovers.add(fo)
            self.conn.execute("INSERT INTO logs VALUES (?,?,?,?,?)",
                              (ts, "orders-db", "WARN", "failover complete: db-prod-17r promoted to primary on db-prod-17, "
                                                        "replica catch-up starting", None))
        if lag > 0 and int(ts) % 10 == 0:
            self.conn.execute("INSERT INTO logs VALUES (?,?,?,?,?)",
                              (ts, "orders-db", "INFO", f"replica catch-up running lag={lag:.0f}s", None))

    def deploy_logs(self, ts: float) -> None:
        for d in read_json(DEPLOYS_FILE, []):
            key = f"{d['service']}@{d['version']}@{d['ts']}"
            if key not in self.seen_deploys:
                self.seen_deploys.add(key)
                if ts - d["ts"] < 120:
                    self.conn.execute("INSERT INTO logs VALUES (?,?,?,?,?)",
                                      (ts, d["service"], "INFO", f"deploy {d['version']} rollout complete (3/3 pods)", None))

    def run(self) -> None:
        print(f"[estate] serving {REQ_PER_TICK} req/s into {telemetry.DB}", flush=True)
        while True:
            ts = time.time()
            try:
                ch = self.chaos()
                for _ in range(REQ_PER_TICK):
                    self.request(ts + random.random() * 0.8, ch)
                self.host_signals(ts, ch)
                self.deploy_logs(ts)
                self.conn.commit()
                if os.path.exists(WIPE_FILE):
                    telemetry.prune(self.conn, keep_s=0)
                    os.remove(WIPE_FILE)
                    print("[estate] stores wiped for a clean rehearsal", flush=True)
                if ts - self.last_prune > 60:
                    telemetry.prune(self.conn)
                    self.last_prune = ts
            except Exception as e:  # the single writer must never die
                print(f"[estate] tick error: {e}", flush=True)
                time.sleep(1)
            time.sleep(max(0.0, TICK_S - (time.time() - ts)))


if __name__ == "__main__":
    os.makedirs(os.path.dirname(CHAOS_FILE) or ".", exist_ok=True)
    Estate().run()
