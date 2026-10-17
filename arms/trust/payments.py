"""Payment adapters. Only the trust engine calls these, after an APPROVE decision bound to the cart hash.

- ``MockPaymentAdapter``: deterministic, always succeeds (provider 'mock'); ``decline_tokens`` simulates declines.
- ``VisaIntelligentCommerceAdapter``: the VIC-shaped surface (enroll_card, request_agent_token,
  create_payment_instruction, send_commerce_signal), labelled "VIC-ready, sandbox access pending". The charge itself
  runs on **Stripe test mode** (PaymentIntent, integer cents, ``pm_card_visa``). Live keys are refused.

``stripe`` is an optional dep (``uv sync --extra trust``) and is imported lazily.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol

from contracts.trust import PaymentProvider, PaymentStatus
from utils.log import get_logger

log = get_logger(__name__)


@dataclass
class ChargeResult:
    provider: PaymentProvider
    status: PaymentStatus
    provider_ref: str | None = None
    error: str | None = None


class PaymentAdapter(Protocol):
    provider: PaymentProvider

    async def charge(
        self,
        amount_cents: int,
        *,
        payment_token: str,
        idempotency_key: str,
        description: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ChargeResult: ...


@dataclass
class MockPaymentAdapter:
    provider: PaymentProvider = "mock"
    decline_tokens: set[str] = field(default_factory=lambda: {"pm_card_chargeDeclined", "tok_decline"})

    async def charge(
        self,
        amount_cents: int,
        *,
        payment_token: str,
        idempotency_key: str,
        description: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ChargeResult:
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            return ChargeResult(self.provider, "error", error=f"invalid amount {amount_cents!r}")
        if payment_token in self.decline_tokens:
            return ChargeResult(self.provider, "declined", error="card_declined (mock)")
        return ChargeResult(self.provider, "succeeded", provider_ref=f"mock_pi_{idempotency_key[-16:]}")


class StripeNotConfigured(RuntimeError):
    """Stripe SDK missing, no key, or a non-test key."""


class VisaIntelligentCommerceAdapter:
    """VIC-shaped adapter backed by Stripe test mode (Visa sandbox access pending; never claims certification)."""

    provider: PaymentProvider = "stripe_test"

    def __init__(self, stripe_secret_key: str, *, visa_api_key: str = "") -> None:
        if not stripe_secret_key:
            raise StripeNotConfigured("STRIPE_SECRET_KEY is empty")
        if not stripe_secret_key.startswith(("sk_test_", "rk_test_")):
            raise StripeNotConfigured("refusing a non-test Stripe key (test mode only)")
        try:
            import stripe  # noqa: F401  (optional extra: trust)
        except ImportError as exc:  # pragma: no cover - depends on extras
            raise StripeNotConfigured("stripe not installed: uv sync --extra trust") from exc
        self._key = stripe_secret_key
        self._visa_api_key = visa_api_key
        self.sandbox_status = "VIC-ready, sandbox access pending"

    # --- VIC surface (stubs until Visa sandbox onboarding) ----------------------------------------------------------
    async def enroll_card(self, user_id: str, payment_token: str) -> dict[str, Any]:
        """VIC: enroll a tokenized card for agent use. Stub: the Stripe pm_... token IS the enrolled instrument."""
        return {"user_id": user_id, "instrument": payment_token, "status": "enrolled", "sandbox": True}

    async def request_agent_token(self, user_id: str, mandate_id: str | None, amount_cents: int) -> dict[str, Any]:
        """VIC: scoped agentic token bound to the mandate. Stub: echoes the scope (amount cap + mandate)."""
        return {"user_id": user_id, "mandate_id": mandate_id, "max_amount_cents": amount_cents, "sandbox": True}

    async def create_payment_instruction(
        self,
        amount_cents: int,
        *,
        payment_token: str,
        idempotency_key: str,
        description: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ChargeResult:
        """VIC: payment instruction -> Stripe test-mode PaymentIntent (confirmed immediately, no redirects)."""
        return await asyncio.to_thread(
            self._stripe_charge, amount_cents, payment_token, idempotency_key, description, metadata or {}
        )

    async def send_commerce_signal(self, receipt_id: str, event: str = "purchase.completed") -> dict[str, Any]:
        """VIC: post-purchase commerce signal. Stub: recorded locally (the audit chain carries it)."""
        return {"receipt_id": receipt_id, "event": event, "sandbox": True}

    # --- PaymentAdapter --------------------------------------------------------------------------------------------
    async def charge(
        self,
        amount_cents: int,
        *,
        payment_token: str,
        idempotency_key: str,
        description: str = "",
        metadata: dict[str, str] | None = None,
    ) -> ChargeResult:
        return await self.create_payment_instruction(
            amount_cents,
            payment_token=payment_token,
            idempotency_key=idempotency_key,
            description=description,
            metadata=metadata,
        )

    def _stripe_charge(
        self, amount_cents: int, payment_token: str, idempotency_key: str, description: str, metadata: dict[str, str]
    ) -> ChargeResult:
        import stripe

        if not isinstance(amount_cents, int) or amount_cents < 50:  # Stripe minimum is $0.50
            return ChargeResult(self.provider, "error", error=f"invalid amount {amount_cents!r} (min 50 cents)")
        try:
            pi = stripe.PaymentIntent.create(
                api_key=self._key,
                amount=amount_cents,
                currency="usd",
                payment_method=payment_token,
                confirm=True,
                automatic_payment_methods={"enabled": True, "allow_redirects": "never"},
                description=description or None,
                metadata=metadata,
                idempotency_key=idempotency_key,
            )
        except stripe.CardError as exc:  # type: ignore[attr-defined]
            return ChargeResult(self.provider, "declined", error=f"card_declined: {exc.user_message or exc}")
        except Exception as exc:  # network, auth, invalid request
            log.warning("stripe charge failed: %s", exc)
            return ChargeResult(self.provider, "error", error=f"stripe_error: {type(exc).__name__}")
        status = "succeeded" if pi.status == "succeeded" else "declined"
        error = None if status == "succeeded" else f"payment_intent_status: {pi.status}"
        return ChargeResult(self.provider, status, provider_ref=pi.id, error=error)
