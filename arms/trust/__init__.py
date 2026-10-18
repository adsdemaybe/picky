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


async def run_payment_agent(trust, request, on_step=None, **kw):  # noqa: ANN001, ANN201
    """The Visa payment agent (judges' demo): checkout as a ``payment.agent_step`` timeline (``arms.trust.agent``)."""
    from arms.trust.agent import run_payment_agent as _run

    return await _run(trust, request, on_step, **kw)


__all__ = ["create", "create_fake", "run_payment_agent"]
