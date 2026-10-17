"""Tiny broadcast helper: every ``stream()`` subscriber gets its own bounded queue (drop-oldest when slow).

The subscription is registered EAGERLY when ``stream()`` is called (not on the first ``__anext__``), so nothing
pushed between creating the iterator and starting to iterate is lost.
"""

import asyncio
import contextlib
from typing import Generic, TypeVar

T = TypeVar("T")


class Subscription(Generic[T]):
    def __init__(self, owner: "Fanout[T]", maxsize: int, initial: T | None) -> None:
        self._owner = owner
        self.queue: asyncio.Queue[T] = asyncio.Queue(maxsize)
        if initial is not None:
            self.queue.put_nowait(initial)
        self.closed = False

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
        if not self.closed:
            self.closed = True
            self._owner._remove(self)


class Fanout(Generic[T]):
    def __init__(self, maxsize: int = 64) -> None:
        self._maxsize = maxsize
        self._subs: list[Subscription[T]] = []

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def push(self, item: T) -> None:
        for sub in list(self._subs):
            q = sub.queue
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(item)

    def stream(self, initial: T | None = None) -> Subscription[T]:
        sub = Subscription(self, self._maxsize, initial)
        self._subs.append(sub)
        return sub

    def _remove(self, sub: Subscription[T]) -> None:
        if sub in self._subs:
            self._subs.remove(sub)
