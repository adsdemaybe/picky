"""In-memory v0.2 EventPublisher (stamps bus-wide seq like the gateway's publisher) for tests and standalone runs."""

from typing import Any

from contracts.events import EventBase


class MemoryBus:
    """``contracts.services.EventPublisher``: ``publish`` stamps ``seq`` (strictly increasing) and keeps every event."""

    def __init__(self) -> None:
        self.events: list[Any] = []
        self._seq = 0

    async def publish(self, event: Any) -> Any:
        self._seq += 1
        stamped = event.model_copy(update={"seq": self._seq})
        self.events.append(stamped)
        return stamped

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, *types: str) -> list[Any]:
        return [e for e in self.events if e.type in types]

    def dumps(self) -> list[dict[str, Any]]:
        """JSON-mode dicts (what the websocket would carry)."""
        return [e.model_dump(mode="json") for e in self.events if isinstance(e, EventBase)]
