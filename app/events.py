"""In-process pub/sub used to drive the live progress UI over SSE."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Callable


class Broker:
    def __init__(self, history: int = 400) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[dict] = deque(maxlen=history)
        self._loop: asyncio.AbstractEventLoop | None = None
        # Set by the app at startup to persist log lines, returning the stored row
        # id. A hook rather than a direct `db` import: this module is deliberately
        # dependency-free so it can be imported from anywhere, including from db.
        self.sink: Callable[[dict], int | None] | None = None

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

    def log(
        self,
        message: str,
        level: str = "info",
        *,
        source: str | None = None,
        context: dict | None = None,
        job_id: str | None = None,
        **extra: Any,
    ) -> None:
        """Stream a log line, and persist it if a sink is registered.

        ``context`` is the structured detail behind the sentence — the path that
        failed, its extension and size, the exception type, a traceback. It rides
        along on the event so the live view can expand a row, and it is what makes
        an error reportable rather than merely visible.
        """
        # Stored first, then streamed, so the event carries the row id of the line
        # it is a copy of. That is what lets a browser merge the live feed with a
        # history query without showing the same line twice.
        log_id = None
        if self.sink is not None:
            try:
                log_id = self.sink(
                    {"level": level, "message": message, "source": source,
                     "context": context, "job_id": job_id}
                )
            except Exception:  # noqa: BLE001 — a log write must never break a job
                log_id = None
        self.publish(
            "log", id=log_id, level=level, message=message, source=source, context=context,
            job_id=job_id, **extra,
        )


broker = Broker()


def sse(event: dict) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"
