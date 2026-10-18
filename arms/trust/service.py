"""Real TrustService: same deterministic engine + Stripe test-mode PaymentIntent behind the
VisaIntelligentCommerceAdapter (provider 'stripe_test').

Degrades to the mock adapter (provider 'mock', honestly labelled) when ``stripe`` is not installed or
STRIPE_SECRET_KEY is empty / not a test key, so the trip never dies on payment setup.

TODO (maps to CLAUDE.md §3 trust + docs/research.md §9.8):
1. [P1] Persist mandates / decisions / receipts / audit to Postgres (contracts/db/schema.sql) instead of memory.
2. [P1] Visa sandbox (Cybersource / VIC) provider 'visa_sandbox' once access exists; keep Stripe as fallback.
3. [P2] Ed25519 (PyNaCl) signatures + RFC 9421 agent request signing (TAP-style agent identity).
4. [P2] Stripe Shared Payment Tokens with usage_limits bound to the IntentMandate.
"""

from arms.trust.core import TrustCore
from arms.trust.payments import MockPaymentAdapter, PaymentAdapter, StripeNotConfigured, VisaIntelligentCommerceAdapter
from contracts.services import Services
from utils.config import get_settings
from utils.log import get_logger

log = get_logger(__name__)


def make_payment_adapter() -> PaymentAdapter:
    s = get_settings()
    try:
        return VisaIntelligentCommerceAdapter(s.stripe_secret_key, visa_api_key=s.visa_api_key)
    except StripeNotConfigured as exc:
        log.warning("trust: Stripe test mode unavailable (%s); using mock payments", exc)
        return MockPaymentAdapter()


class TrustService(TrustCore):
    def __init__(self, services: Services | None = None, *, payments: PaymentAdapter | None = None) -> None:
        super().__init__(services, payments or make_payment_adapter(), id_seed="trust")
