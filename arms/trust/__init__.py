"""trust arm: deterministic AP2-style mandates, hash-bound checkout approval, Stripe test payments, ledger."""

from contracts.services import Services


def create(services: Services):
    """Real TrustService (Stripe test mode via the VIC adapter; mock payments if Stripe isn't configured)."""
    from arms.trust.service import TrustService

    return TrustService(services)


def create_fake(services: Services):
    """Deterministic fake (mock receipts, base deps only)."""
    from arms.trust.fake import FakeTrustService

    return FakeTrustService(services)


__all__ = ["create", "create_fake"]
