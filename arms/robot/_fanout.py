"""Tiny broadcast helper: every ``stream()`` subscriber gets its own bounded queue (drop-oldest when slow).

The subscription is registered EAGERLY when ``stream()`` is called (not on the first ``__anext__``), so nothing
pushed between creating the iterator and starting to iterate is lost. A subscription may carry a ``predicate``
(only matching items are queued) and an ``on_close`` hook (used to ref-count camera streams).
"""

import asyncio
import contextlib
from collections.abc import Callable, Iterable
from typing import Generic, TypeVar

T = TypeVar("T")


class Subscription(Generic[T]):
    def __init__(
        self,
        owner: "Fanout[T]",
        maxsize: int,
        initial: Iterable[T] = (),
        predicate: Callable[[T], bool] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._owner = owner
        self.queue: asyncio.Queue[T] = asyncio.Queue(maxsize)
        self.predicate = predicate
        self._on_close = on_close
        self.closed = False
        for item in initial:
            self.offer(item)

    def offer(self, item: T) -> None:
        if self.closed or (self.predicate is not None and not self.predicate(item)):
            return
        q = self.queue
        if q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
        q.put_nowait(item)

    def __aiter__(self) -> "Subscription[T]":
        return self

    async def __anext__(self) -> T:
        if self.closed:
            raise StopAsyncIteration
        try:
            return await self.queue.get()
        except asyncio.CancelledError:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._owner._remove(self)
            if self._on_close is not None:
                with contextlib.suppress(Exception):
                    self._on_close()

    def __del__(self) -> None:  # an abandoned iterator must not keep a camera streaming forever
        with contextlib.suppress(Exception):
            self.close()


class Fanout(Generic[T]):
    def __init__(self, maxsize: int = 64) -> None:
        self._maxsize = maxsize
        self._subs: list[Subscription[T]] = []

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def push(self, item: T) -> None:
        for sub in list(self._subs):
            sub.offer(item)

    def stream(
        self,
        initial: T | Iterable[T] | None = None,
        predicate: Callable[[T], bool] | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> Subscription[T]:
        if initial is None:
            items: Iterable[T] = ()
        elif isinstance(initial, list | tuple):
            items = initial
        else:
            items = (initial,)  # type: ignore[assignment]
        sub = Subscription(self, self._maxsize, items, predicate, on_close)
        self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription[T]) -> None:
        if sub in self._subs:
            self._subs.remove(sub)
