"""In-process pub/sub used to drive the live progress UI over SSE."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any


class Broker:
    def __init__(self, history: int = 400) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict] = deque(maxlen=history)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def replay(self) -> list[dict]:
        return list(self._history)

    def publish(self, kind: str, **payload: Any) -> None:
        """Safe to call from any thread."""
        event = {"kind": kind, "ts": time.time(), **payload}
        if kind in {"log", "job"}:
            self._history.append(event)
        loop = self._loop
        if loop is None:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._fanout(event)
        else:
            loop.call_soon_threadsafe(self._fanout, event)

    def _fanout(self, event: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop rather than stall the producer.
                pass

    def log(self, message: str, level: str = "info", **extra: Any) -> None:
        self.publish("log", level=level, message=message, **extra)


broker = Broker()


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"
