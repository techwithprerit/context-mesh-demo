"""The bus: nodes publish context events here and never call each other.

Two backends behind one interface:
  redis://host:6379/0      - Redis pub/sub (docker compose default)
  file:///path/bus.jsonl   - append-only JSONL, tailed by subscribers (zero-infra local mode)
"""
from __future__ import annotations

import fnmatch
import json
import os
import threading
import time
from typing import Callable

from .common import now

Handler = Callable[[dict], None]


class Bus:
    def __init__(self, url: str | None = None):
        self.url = url or os.environ.get("BUS_URL", "file://./data/bus.jsonl")
        if self.url.startswith("redis://"):
            import redis  # lazy, so local mode needs no redis client

            self._r = redis.Redis.from_url(self.url)
            self.kind = "redis"
        else:
            self.path = self.url.replace("file://", "", 1)
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            open(self.path, "a").close()
            self._lock = threading.Lock()
            self.kind = "file"

    def ping(self) -> bool:
        """True if the bus is reachable (redis PING; a file bus is always reachable)."""
        if self.kind == "redis":
            try:
                return bool(self._r.ping())
            except Exception:
                return False
        return True

    # -- publish -----------------------------------------------------------
    def publish(self, topic: str, **payload) -> dict:
        event = {"topic": topic, "ts": payload.pop("ts", now()), **payload}
        line = json.dumps(event)
        if self.kind == "redis":
            self._r.publish(topic, line)
        else:
            with self._lock, open(self.path, "a") as f:
                f.write(line + "\n")
        return event

    # -- subscribe ---------------------------------------------------------
    def subscribe(self, pattern: str, handler: Handler, replay: bool = False) -> threading.Thread:
        """Run handler(event) for every event whose topic matches the glob pattern, in a thread."""
        t = threading.Thread(target=self._loop, args=(pattern, handler, replay), daemon=True)
        t.start()
        return t

    def _loop(self, pattern: str, handler: Handler, replay: bool) -> None:
        if self.kind == "redis":
            while True:  # reconnect forever: a bus blip must not silence the graph
                try:
                    ps = self._r.pubsub()
                    ps.psubscribe(pattern)
                    for msg in ps.listen():
                        if msg.get("type") != "pmessage":
                            continue
                        try:
                            ev = json.loads(msg["data"])
                        except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
                            print(f"[bus] skipped a malformed event on {msg.get('channel')!r}", flush=True)
                            continue  # one bad message must not drop the subscription
                        if isinstance(ev, dict):
                            self._safe(handler, ev)
                except Exception as e:
                    print(f"[bus] redis subscribe lost ({e}); retrying in 1 s", flush=True)
                    time.sleep(1)
        with open(self.path) as f:
            if not replay:
                f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if not line:
                    time.sleep(0.25)
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict) and fnmatch.fnmatch(str(ev.get("topic", "")), pattern):
                    self._safe(handler, ev)

    @staticmethod
    def _safe(handler: Handler, ev: dict) -> None:
        try:
            handler(ev)
        except Exception as e:  # a bad handler must never kill the subscriber
            print(f"[bus] handler error on {ev.get('topic')}: {e}", flush=True)
