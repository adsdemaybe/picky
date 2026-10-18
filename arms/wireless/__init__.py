"""wireless arm: deterministic plans, trade-in, financing, promos, compatibility and cart quotes for store_tmobile."""

from contracts.services import Services


def create(services: Services):
    """Real WirelessService: data/wireless.yaml + live catalog prices and stock (``services.catalog``)."""
    from arms.wireless.service import WirelessService

    return WirelessService(services)


def create_fake(services: Services):
    """Deterministic fake: same engine and data, prices from data/products.yaml, stock as in the planogram."""
    from arms.wireless.fake import FakeWirelessService

    return FakeWirelessService(services)


__all__ = ["create", "create_fake"]
