"""fleet arm: the fleet manager for every robot across every store (NL request -> store choice -> goals -> robot
assignment -> robot brain -> monitor / reassign).

Public API (used only by the gateway composition root):
    create(services)      -> real FleetService (agent.interpret_request first, keyword rules as fallback)
    create_fake(services) -> deterministic FleetService (keyword rules; the agent arm only expands shop lists)
Neither calls another service at construction time.
"""

from typing import Any

from contracts.services import FleetService, Services


def create(services: Services, **opts: Any) -> FleetService:
    from arms.fleet.service import RealFleet

    return RealFleet(services, **opts)


def create_fake(services: Services, **opts: Any) -> FleetService:
    from arms.fleet.fake import FakeFleet

    return FakeFleet(services, **opts)


__all__ = ["create", "create_fake"]
