"""Real FleetService: the same deterministic dispatcher as the fake, but natural-language understanding goes through
``services.agent.interpret_request`` (Claude in the real agent arm; no direct LLM calls here). Any interpretation
failure falls back to the keyword rules and publishes an ``error`` event (code ``interpret_fallback``).

TODO (P1): smarter store choice with routing distances (``routing.plan_route`` per store) and per-robot travel ETA.
"""

from typing import Any

from arms.fleet.core import FleetManager


class RealFleet(FleetManager):
    def __init__(self, services: Any, **opts: Any) -> None:
        super().__init__(services, mode="real", **opts)


__all__ = ["RealFleet"]
