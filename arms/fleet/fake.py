"""Deterministic fake FleetService: keyword interpretation (``rules.py``) + the shared dispatcher (``core.py``).

Shop lists come from the agent arm (``interpret_request`` when it returns a list, else ``expand_intent``), so the taco
demo list is identical to the trip flow's. Store choice, assignment, queueing and reassignment are the same
deterministic code the real fleet runs.
"""

from typing import Any

from arms.fleet.core import FleetManager


class FakeFleet(FleetManager):
    def __init__(self, services: Any, **opts: Any) -> None:
        super().__init__(services, mode="fake", **opts)


__all__ = ["FakeFleet"]
