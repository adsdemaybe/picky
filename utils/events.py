"""Async in-process event bus (no contracts import; works with any pydantic model or dict that has a ``type``).

- ``publish(event)`` stamps a bus-wide, strictly increasing ``seq`` (and ``ts`` if the event has an empty one),
  then fans out to every matching subscriber without ever blocking the publisher.
- Each subscriber owns a bounded ``asyncio.Queue``; when full, the OLDEST queued event is dropped
  (``Subscription.dropped`` counts them) so a slow websocket can't stall the trip.
- Subscribe to everything or to dotted type prefixes: ``"route"`` matches ``route`` and ``route.planned``
  (not ``routes.x``). ``"*"`` / ``None`` = all. Optional ``trip_id`` filter.
- A small ring buffer (``recent()``) lets a late websocket client replay what it missed.

Usage::

    bus = EventBus()
    async with bus.subscribe(["route", "robot"]) as sub:
        async for ev in sub:
            ...
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import itertools
from collections import deque
from collections.abc import AsyncIterator, Iterable
from typing import Any

from utils.log import get_logger

log = get_logger(__name__)

DEFAULT_QUEUE_SIZE = 256
DEFAULT_HISTORY = 1000


def event_type(event: Any) -> str:
    t = event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    t = getattr(t, "value", t)  # tolerate Enum types
    if not isinstance(t, str) or not t:
        raise ValueError(f"event has no 'type': {event!r}")
    return t


def event_field(event: Any, name: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(name, default)
    return getattr(event, name, default)


def event_to_dict(event: Any) -> dict[str, Any]:
    """JSON-ready dict (for websockets / logs)."""
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json")
    return dict(event)


def type_matches(etype: str, prefixes: frozenset[str] | None) -> bool:
    if prefixes is None:
        return True
    for p in prefixes:
        if etype == p or etype.startswith(p + "."):
            return True
    return False


def _norm_prefixes(prefix: str | Iterable[str] | None) -> frozenset[str] | None:
    if prefix is None:
        return None
    items = [prefix] if isinstance(prefix, str) else list(prefix)
    cleaned = {p.strip().rstrip(".*").rstrip(".") for p in items}
    if "" in cleaned or not items:
        return None  # "*" or "" -> everything
    return frozenset(cleaned)


def _stamp(event: Any, seq: int) -> Any:
    now = _dt.datetime.now(_dt.UTC)
    if isinstance(event, dict):
        out = dict(event)
        out["seq"] = seq
        if not out.get("ts"):
            out["ts"] = now.isoformat()
        return out
    fields = getattr(type(event), "model_fields", None)
    if fields is not None:
        update: dict[str, Any] = {}
        if "seq" in fields:
            update["seq"] = seq
        if "ts" in fields and not getattr(event, "ts", None):
            update["ts"] = now
        return event.model_copy(update=update) if update else event
    return event


class Subscription:
    """Async-iterable handle. Use as ``async with`` (auto-unsubscribe) or call :meth:`close`."""

    def __init__(
        self, bus: EventBus, prefixes: frozenset[str] | None, trip_id: str | None, maxsize: int, name: str
    ) -> None:
        self._bus = bus
        self.prefixes = prefixes
        self.trip_id = trip_id
        self.name = name
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)
        self.dropped = 0
        self.closed = False

    def matches(self, event: Any, etype: str) -> bool:
        if self.trip_id is not None and event_field(event, "trip_id") != self.trip_id:
            return False
        return type_matches(etype, self.prefixes)

    def _offer(self, event: Any) -> None:
        if self.closed:
            return
        while True:
            try:
                self.queue.put_nowait(event)
                return
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()
                    self.dropped += 1
                    if self.dropped in (1, 10, 100) or self.dropped % 1000 == 0:
                        log.warning("subscriber %s slow: dropped %d events", self.name, self.dropped)

    async def get(self, timeout: float | None = None) -> Any:  # noqa: ASYNC109
        """Next event; raises ``TimeoutError`` after ``timeout`` seconds."""
        if timeout is None:
            return await self.queue.get()
        return await asyncio.wait_for(self.queue.get(), timeout)

    def get_nowait(self) -> Any:
        return self.queue.get_nowait()

    def drain(self) -> list[Any]:
        out = []
        while True:
            try:
                out.append(self.queue.get_nowait())
            except asyncio.QueueEmpty:
                return out

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._bus._remove(self)
            if self.queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()
            self.queue.put_nowait(_CLOSED)

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        if self.closed and self.queue.empty():
            raise StopAsyncIteration
        item = await self.queue.get()
        if item is _CLOSED:
            raise StopAsyncIteration
        return item

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()


_CLOSED = object()


class EventBus:
    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE, history: int = DEFAULT_HISTORY) -> None:
        self._subs: list[Subscription] = []
        self._seq = itertools.count(1)
        self._last_seq = 0
        self._queue_size = queue_size
        self._history: deque[Any] = deque(maxlen=history)
        self._names = itertools.count(1)

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def subscriber_count(self) -> int:
        return len(self._subs)

    def publish_nowait(self, event: Any) -> Any:
        """Stamp ``seq``/``ts``, record, fan out. Never blocks. Returns the stamped event."""
        etype = event_type(event)
        seq = next(self._seq)
        self._last_seq = seq
        stamped = _stamp(event, seq)
        self._history.append(stamped)
        for sub in list(self._subs):
            if sub.matches(stamped, etype):
                sub._offer(stamped)
        return stamped

    async def publish(self, event: Any) -> Any:
        return self.publish_nowait(event)

    def subscribe(
        self,
        prefix: str | Iterable[str] | None = None,
        *,
        trip_id: str | None = None,
        maxsize: int | None = None,
        replay: bool = False,
        name: str | None = None,
    ) -> Subscription:
        """New subscription. ``replay=True`` pre-loads matching events from ``recent()``."""
        sub = Subscription(
            self,
            _norm_prefixes(prefix),
            trip_id,
            maxsize or self._queue_size,
            name or f"sub{next(self._names)}",
        )
        if replay:
            for ev in self._history:
                if sub.matches(ev, event_type(ev)):
                    sub._offer(ev)
        self._subs.append(sub)
        return sub

    def recent(
        self, prefix: str | Iterable[str] | None = None, *, trip_id: str | None = None, after_seq: int = 0
    ) -> list[Any]:
        prefixes = _norm_prefixes(prefix)
        out = []
        for ev in self._history:
            if (event_field(ev, "seq") or 0) <= after_seq:
                continue
            if trip_id is not None and event_field(ev, "trip_id") != trip_id:
                continue
            if type_matches(event_type(ev), prefixes):
                out.append(ev)
        return out

    def _remove(self, sub: Subscription) -> None:
        with contextlib.suppress(ValueError):
            self._subs.remove(sub)

    def close(self) -> None:
        for sub in list(self._subs):
            sub.close()
