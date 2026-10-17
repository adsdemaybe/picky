"""routing arm: planogram -> occupancy grid -> distance matrix -> orienteering route (mandatory list stops +
optional recommendation detours). Public API: ``create(services)`` (real) and ``create_fake(services)``."""

from contracts.services import RoutingService, Services


def create(services: Services) -> RoutingService:
    """Real implementation (grid + MCP + OR-Tools; degrades to greedy/straight-line without the 'routing' extra)."""
    from arms.routing.service import GridRoutingService

    return GridRoutingService(services)


def create_fake(services: Services) -> RoutingService:
    """Deterministic fake (straight lines, nearest-neighbor + cheapest insertion). Base deps only."""
    from arms.routing.fake import FakeRoutingService

    return FakeRoutingService(services)


__all__ = ["create", "create_fake"]
