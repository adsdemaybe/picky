"""Deterministic fake TrustService: the real mandate engine + a mock payment adapter (Receipt.provider='mock').

Base deps only. Mandates come from data/fixtures/users.yaml; when a user has none (or the file is missing) the fake
falls back to ``demo_mandate(user_id)`` so the pipeline still runs end to end.
"""

from datetime import timedelta

from arms.trust.core import TrustCore
from arms.trust.payments import MockPaymentAdapter
from contracts.common import utcnow
from contracts.services import Services
from contracts.trust import IntentMandate

DEMO_CAP_CENTS = 6000
DEMO_ASK_ABOVE_CENTS = 1000
DEMO_BLOCKED = ["alcohol", "tobacco"]


def demo_mandate(user_id: str) -> IntentMandate:
    """Fallback standing mandate: $60 cap per trip, ask above $10 per line, no alcohol/tobacco, valid 30 days."""
    return IntentMandate(
        mandate_id=f"mandate_{user_id}_demo",
        user_id=user_id,
        cap_cents=DEMO_CAP_CENTS,
        ask_above_cents=DEMO_ASK_ABOVE_CENTS,
        blocked_categories=list(DEMO_BLOCKED),
        expires_at=utcnow() + timedelta(days=30),
    )


class FakeTrustService(TrustCore):
    def __init__(self, services: Services | None = None, *, signing_key: str | bytes | None = None) -> None:
        super().__init__(
            services,
            MockPaymentAdapter(),
            signing_key=signing_key,
            id_seed="trust-fake",
            default_mandate=demo_mandate,
        )
