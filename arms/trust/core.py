"""Shared TrustService engine (fake + real differ only in the payment adapter and the mandate fallback).

In-memory ledger: mandates per user, decisions, transparency (upsert on rec_id), receipts, hash-chained audit.
Trust calls no other service; the gateway publishes ``mandate.decision`` / ``checkout.completed``.
"""

from collections.abc import Callable
from datetime import datetime

from arms.trust.mandates import AuditChain, check_checkout, evaluate_mandate, sign_model
from arms.trust.payments import ChargeResult, PaymentAdapter
from contracts.common import ONLINE_STORE_ID, utcnow
from contracts.services import Services
from contracts.shopping import Cart, CartLine
from contracts.trust import (
    CheckoutRequest,
    IntentMandate,
    MandateDecision,
    MandateVerdict,
    Receipt,
    TransparencyEntry,
    TrustLedger,
)
from utils.ids import IdFactory
from utils.log import get_logger

log = get_logger(__name__)
ACTOR = "trust"


def load_fixture_mandates() -> dict[str, IntentMandate]:
    """Mandates from data/fixtures/users.yaml ({} if the file is missing/invalid)."""
    try:
        from utils.data import DataError, load_demo_fixtures

        try:
            return {u.user_id: u.mandate for u in load_demo_fixtures().users}
        except DataError as exc:
            log.warning("no demo mandates loaded: %s", exc)
            return {}
    except Exception as exc:  # never break construction-time-free calls on bad data
        log.warning("demo mandates unavailable: %s", exc)
        return {}


class TrustCore:
    def __init__(
        self,
        services: Services | None,
        payments: PaymentAdapter,
        *,
        signing_key: str | bytes | None = None,
        id_seed: int | str = "trust",
        clock: Callable[[], datetime] = utcnow,
        default_mandate: Callable[[str], IntentMandate | None] | None = None,
    ) -> None:
        self.services = services  # unused: trust is a leaf (kept for the factory signature)
        self.payments = payments
        self._key = signing_key
        self._ids = IdFactory(id_seed)
        self._clock = clock
        self._default_mandate = default_mandate
        self._mandates: dict[str, IntentMandate] = {}
        self._fixtures_loaded = False
        self._decisions: list[MandateDecision] = []
        self._transparency: dict[str, TransparencyEntry] = {}
        self._receipts: list[Receipt] = []
        self._by_idempotency: dict[str, Receipt] = {}
        self._used_approvals: set[str] = set()
        self.audit = AuditChain(signing_key, clock)

    # ---------------------------------------------------------------------------------------------------- mandates
    def _ensure_fixtures(self) -> None:
        if self._fixtures_loaded:
            return
        self._fixtures_loaded = True
        for uid, m in load_fixture_mandates().items():
            self._mandates.setdefault(uid, m if m.signature else sign_model(m, self._key))

    async def get_mandate(self, user_id: str) -> IntentMandate | None:
        self._ensure_fixtures()
        m = self._mandates.get(user_id)
        if m is None and self._default_mandate is not None:
            m = self._default_mandate(user_id)
            if m is not None:
                m = sign_model(m, self._key)
                self._mandates[user_id] = m
        return m

    async def set_mandate(self, mandate: IntentMandate) -> IntentMandate:
        self._ensure_fixtures()
        signed = sign_model(mandate, self._key)
        self._mandates[mandate.user_id] = signed
        self.audit.append(
            f"user:{mandate.user_id}",
            "mandate.set",
            user_id=mandate.user_id,
            data={
                "mandate_id": mandate.mandate_id,
                "cap_cents": mandate.cap_cents,
                "ask_above_cents": mandate.ask_above_cents,
                "revoked": mandate.revoked,
                "signature": signed.signature,
            },
        )
        return signed

    # ---------------------------------------------------------------------------------------------------- evaluate
    async def evaluate(
        self,
        user_id: str,
        cart: Cart,
        line: CartLine | None = None,
        budget_cents: int | None = None,
        trip_id: str | None = None,
    ) -> MandateDecision:
        mandate = await self.get_mandate(user_id)
        decision = evaluate_mandate(
            mandate,
            cart,
            line,
            user_id=user_id,
            budget_cents=budget_cents,
            trip_id=trip_id,
            now=self._clock(),
            decision_id=self._ids("md"),
        )
        self._record_decision(decision)
        return decision

    def _record_decision(self, decision: MandateDecision) -> None:
        self._decisions.append(decision)
        self.audit.append(
            ACTOR,
            "mandate.evaluate",
            trip_id=decision.trip_id,
            user_id=decision.user_id,
            data={
                "decision_id": decision.decision_id,
                "scope": decision.scope,
                "sku": decision.sku,
                "verdict": decision.verdict.value,
                "reasons": [r.value for r in decision.reasons],
                "total_cents": decision.total_cents,
                "cart_hash": decision.cart_hash,
            },
        )

    # ---------------------------------------------------------------------------------------------------- checkout
    async def checkout(self, request: CheckoutRequest) -> Receipt:
        if request.idempotency_key and request.idempotency_key in self._by_idempotency:
            return self._by_idempotency[request.idempotency_key]

        mandate = await self.get_mandate(request.user_id)
        decision = check_checkout(
            request,
            mandate,
            used_approval_ids=self._used_approvals,
            now=self._clock(),
            decision_id=self._ids("md"),
        )
        self._record_decision(decision)
        cart = request.cart
        base = {
            "receipt_id": self._ids("rcpt"),
            "trip_id": request.trip_id,
            "user_id": request.user_id,
            "amount_cents": cart.total_cents,
            "lines": [ln.model_copy() for ln in cart.active_lines()],
            "in_store_cents": cart.in_store_cents,
            "online_cents": cart.online_cents,
            "cart_hash": request.cart_hash,
            "mandate_id": mandate.mandate_id if mandate else None,
            "approval_id": request.approval.approval_id,
            "created_at": self._clock(),
        }

        if decision.verdict == MandateVerdict.DENY:
            result = ChargeResult(self.payments.provider, "declined", error="; ".join(decision.messages))
        elif cart.total_cents <= 0:
            result = ChargeResult(self.payments.provider, "error", error="empty cart")
        else:
            key = request.idempotency_key or f"{request.trip_id}:{request.cart_hash[:24]}"
            try:
                result = await self.payments.charge(
                    cart.total_cents,
                    payment_token=request.payment_token,
                    idempotency_key=key,
                    description=f"picky trip {request.trip_id}",
                    metadata={"trip_id": request.trip_id, "cart_hash": request.cart_hash},
                )
            except Exception as exc:  # adapters should not raise, but never kill the trip
                log.warning("payment adapter raised: %s", exc)
                result = ChargeResult(self.payments.provider, "error", error=f"payment_error: {type(exc).__name__}")

        online_order_id = None
        if result.status == "succeeded":
            self._used_approvals.add(request.approval.approval_id)
            if any(ln.store_id == ONLINE_STORE_ID for ln in cart.active_lines()):
                online_order_id = self._ids("ord")

        receipt = Receipt(
            **base,
            provider=result.provider,
            status=result.status,
            provider_ref=result.provider_ref,
            error=result.error,
            online_order_id=online_order_id,
        )
        receipt = sign_model(receipt, self._key)
        self._receipts.append(receipt)
        if request.idempotency_key and result.status == "succeeded":
            self._by_idempotency[request.idempotency_key] = receipt
        self.audit.append(
            ACTOR,
            "checkout.charge",
            trip_id=request.trip_id,
            user_id=request.user_id,
            data={
                "receipt_id": receipt.receipt_id,
                "provider": receipt.provider,
                "status": receipt.status,
                "amount_cents": receipt.amount_cents,
                "cart_hash": receipt.cart_hash,
                "approval_id": receipt.approval_id,
                "provider_ref": receipt.provider_ref,
                "online_order_id": online_order_id,
                "receipt_signature": receipt.signature,
            },
        )
        return receipt

    # ---------------------------------------------------------------------------------------------------- ledger
    async def record_transparency(self, entry: TransparencyEntry) -> None:
        prev = self._transparency.get(entry.rec_id)
        self._transparency[entry.rec_id] = entry
        if prev is None or prev.outcome != entry.outcome:
            self.audit.append(
                ACTOR,
                "transparency.record",
                trip_id=entry.trip_id,
                user_id=entry.user_id,
                data={
                    "rec_id": entry.rec_id,
                    "sku": entry.sku,
                    "sponsored": entry.sponsored,
                    "outcome": entry.outcome,
                    "reason": entry.reason,
                },
            )

    async def ledger(self, user_id: str | None = None, trip_id: str | None = None) -> TrustLedger:
        def keep(uid: str | None, tid: str | None) -> bool:
            return (user_id is None or uid == user_id) and (trip_id is None or tid == trip_id)

        return TrustLedger(
            user_id=user_id,
            trip_id=trip_id,
            mandate=await self.get_mandate(user_id) if user_id else None,
            decisions=[d for d in self._decisions if keep(d.user_id, d.trip_id)],
            transparency=[t for t in self._transparency.values() if keep(t.user_id, t.trip_id)],
            receipts=[r for r in self._receipts if keep(r.user_id, r.trip_id)],
            audit=[a for a in self.audit.entries if keep(a.user_id, a.trip_id)],
        )
