"""Deterministic AP2-style mandate engine (pure functions, no I/O, no LLM).

- ``evaluate_mandate``: cart (or cart + proposed line) vs the standing ``IntentMandate`` and the trip budget
  -> ``MandateDecision`` (approve | needs_approval | deny) with reason codes and per-line violations.
- ``build_cart_mandate``: signed ``CartMandate`` bound to ``compute_cart_hash(cart, trip_id)``.
- ``check_checkout``: checkout preconditions (approval kind/decision/expiry/replay + cart hash) and the mandate.
- ``AuditChain``: append-only, hash-chained, HMAC-signed audit entries (``utils.signing``).

Severity rules (contracts/trust.py docstring):
- DENY: no-longer-valid mandate (expired/revoked), total over ``cap_cents``, line over ``per_item_cap_cents``,
  blocked / not-allowed category, merchant not allowed, online not allowed.
- NEEDS_APPROVAL: added line total strictly above ``ask_above_cents`` (line scope only), total over the trip budget,
  no standing mandate at all.
"""

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from contracts.common import utcnow
from contracts.shopping import Cart, CartLine
from contracts.trust import (
    ApprovalKind,
    AuditEntry,
    CartMandate,
    CheckoutRequest,
    IntentMandate,
    MandateDecision,
    MandateReason,
    MandateVerdict,
    MandateViolation,
    compute_cart_hash,
)
from utils.money import fmt_cents
from utils.signing import sha256_hex, sign, verify

R = MandateReason
DENY_REASONS: frozenset[MandateReason] = frozenset(
    {
        R.MANDATE_EXPIRED,
        R.MANDATE_REVOKED,
        R.OVER_CAP,
        R.OVER_ITEM_CAP,
        R.CATEGORY_BLOCKED,
        R.CATEGORY_NOT_ALLOWED,
        R.MERCHANT_NOT_ALLOWED,
        R.ONLINE_NOT_ALLOWED,
        R.CART_HASH_MISMATCH,
        R.APPROVAL_MISSING,
        R.APPROVAL_REJECTED,
        R.APPROVAL_EXPIRED,
        R.APPROVAL_REPLAYED,
    }
)
ASK_REASONS: frozenset[MandateReason] = frozenset(
    {R.ABOVE_ASK_THRESHOLD, R.OVER_BUDGET, R.NO_MANDATE, R.CHECKOUT_REQUIRES_APPROVAL}
)
SIGNATURE_EXCLUDE = frozenset({"signature"})


# --------------------------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------------------------
def _norm(cat: str | None) -> str:
    return (cat or "").strip().lower()


def verdict_for(reasons: Iterable[MandateReason]) -> MandateVerdict:
    rs = set(reasons)
    if rs & DENY_REASONS:
        return MandateVerdict.DENY
    if rs & ASK_REASONS:
        return MandateVerdict.NEEDS_APPROVAL
    return MandateVerdict.APPROVE


def project_cart(cart: Cart, line: CartLine | None) -> Cart:
    """Cart as it would be with ``line`` added (replacing a line with the same ``line_id``)."""
    if line is None:
        return cart
    lines = [ln for ln in cart.lines if ln.line_id != line.line_id] + [line]
    return cart.model_copy(update={"lines": lines})


def _safe_hash(cart: Cart, trip_id: str | None) -> str | None:
    tid = trip_id or cart.trip_id
    if not tid:
        return None
    try:
        return compute_cart_hash(cart, tid)
    except ValueError:
        return None


def _line_violations(mandate: IntentMandate, line: CartLine, *, check_ask: bool) -> list[MandateViolation]:
    out: list[MandateViolation] = []
    cat = _norm(line.category)
    label = line.name or line.sku

    def v(reason: MandateReason, msg: str) -> None:
        out.append(MandateViolation(reason=reason, sku=line.sku, line_id=line.line_id, message=msg))

    blocked = {_norm(c) for c in mandate.blocked_categories}
    allowed = {_norm(c) for c in mandate.allowed_categories}
    if cat in blocked:
        v(R.CATEGORY_BLOCKED, f"{label}: category '{line.category}' is blocked by your mandate")
    elif allowed and cat not in allowed:
        v(R.CATEGORY_NOT_ALLOWED, f"{label}: category '{line.category}' is not in your allowed categories")
    if line.source == "online" and not mandate.allow_online:
        v(R.ONLINE_NOT_ALLOWED, f"{label}: online orders are not allowed by your mandate")
    if mandate.allowed_merchants and line.store_id not in mandate.allowed_merchants:
        v(R.MERCHANT_NOT_ALLOWED, f"{label}: store '{line.store_id}' is not an allowed merchant")
    if mandate.per_item_cap_cents is not None and line.total_cents > mandate.per_item_cap_cents:
        v(
            R.OVER_ITEM_CAP,
            f"{label}: {fmt_cents(line.total_cents)} is over your per-item cap {fmt_cents(mandate.per_item_cap_cents)}",
        )
    if check_ask and line.total_cents > mandate.ask_above_cents:
        v(
            R.ABOVE_ASK_THRESHOLD,
            f"{label}: {fmt_cents(line.total_cents)} is above your 'ask me above "
            f"{fmt_cents(mandate.ask_above_cents)}' rule",
        )
    return out


def _dedupe(reasons: Iterable[MandateReason]) -> list[MandateReason]:
    seen: list[MandateReason] = []
    for r in reasons:
        if r not in seen:
            seen.append(r)
    return seen


# --------------------------------------------------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------------------------------------------------
def evaluate_mandate(
    mandate: IntentMandate | None,
    cart: Cart,
    line: CartLine | None = None,
    *,
    user_id: str,
    budget_cents: int | None = None,
    trip_id: str | None = None,
    now: datetime | None = None,
    decision_id: str | None = None,
) -> MandateDecision:
    """Deterministic check of ``cart`` (scope='cart') or ``cart + line`` (scope='line')."""
    now = now or utcnow()
    trip_id = trip_id or cart.trip_id
    projected = project_cart(cart, line)
    total = projected.total_cents
    violations: list[MandateViolation] = []
    cart_level: list[tuple[MandateReason, str]] = []

    if mandate is None:
        cart_level.append((R.NO_MANDATE, "No spending mandate is set, so every purchase needs your approval"))
    else:
        if mandate.revoked:
            cart_level.append((R.MANDATE_REVOKED, "Your spending mandate was revoked"))
        if mandate.expires_at <= now:
            cart_level.append((R.MANDATE_EXPIRED, f"Your spending mandate expired {mandate.expires_at:%Y-%m-%d}"))
        if total > mandate.cap_cents:
            cart_level.append(
                (R.OVER_CAP, f"Cart {fmt_cents(total)} is over your trip cap {fmt_cents(mandate.cap_cents)}")
            )
        lines_to_check = [line] if line is not None else projected.active_lines()
        for ln in lines_to_check:
            violations += _line_violations(mandate, ln, check_ask=line is not None)

    if budget_cents is not None and total > budget_cents:
        cart_level.append(
            (R.OVER_BUDGET, f"Cart {fmt_cents(total)} is over this trip's budget {fmt_cents(budget_cents)}")
        )

    cart_violations = [MandateViolation(reason=r, message=m) for r, m in cart_level]
    violations = cart_violations + violations

    reasons = _dedupe(v.reason for v in violations) or [R.OK]
    messages = [v.message for v in violations] or ["Within your mandate"]
    limits = [x for x in (mandate.cap_cents if mandate else None, budget_cents) if x is not None]
    remaining = (min(limits) - total) if limits else None
    cart_hash = _safe_hash(projected, trip_id)
    if decision_id is None:
        decision_id = (
            "md_"
            + sha256_hex(
                {
                    "m": mandate.mandate_id if mandate else None,
                    "h": cart_hash,
                    "t": total,
                    "l": line.line_id if line else None,
                    "now": now.isoformat(),
                }
            )[:16]
        )
    return MandateDecision(
        decision_id=decision_id,
        user_id=user_id,
        trip_id=trip_id,
        mandate_id=mandate.mandate_id if mandate else None,
        verdict=verdict_for(reasons),
        reasons=reasons,
        messages=messages,
        violations=violations,
        scope="line" if line is not None else "cart",
        sku=line.sku if line is not None else None,
        total_cents=total,
        budget_cents=budget_cents,
        remaining_cents=remaining,
        cart_hash=cart_hash,
        evaluated_at=now,
    )


# --------------------------------------------------------------------------------------------------------------------
# Signing
# --------------------------------------------------------------------------------------------------------------------
def sign_model(model: Any, key: str | bytes | None = None) -> Any:
    """Copy of a contract model with ``signature`` = HMAC over its canonical JSON (signature excluded)."""
    unsigned = model.model_copy(update={"signature": None})
    return unsigned.model_copy(update={"signature": sign(unsigned, key, exclude=SIGNATURE_EXCLUDE)})


def verify_model(model: Any, key: str | bytes | None = None) -> bool:
    if not model.signature:
        return False
    return verify(model.model_copy(update={"signature": None}), model.signature, key, exclude=SIGNATURE_EXCLUDE)


def build_cart_mandate(
    cart: Cart,
    *,
    trip_id: str,
    user_id: str,
    mandate: IntentMandate | None = None,
    key: str | bytes | None = None,
) -> CartMandate:
    """Signed CartMandate whose ``cart_hash`` = ``compute_cart_hash(cart, trip_id)``."""
    cm = CartMandate.from_cart(
        cart, trip_id=trip_id, user_id=user_id, intent_mandate_id=mandate.mandate_id if mandate else None
    )
    return sign_model(cm, key)


# --------------------------------------------------------------------------------------------------------------------
# Checkout preconditions
# --------------------------------------------------------------------------------------------------------------------
def approval_violations(request: CheckoutRequest, *, used_approval_ids: Iterable[str] = ()) -> list[MandateViolation]:
    """Approval must be: kind=checkout, same approval_id, decision 'approve', unexpired, unused, and bound to the
    hash of the cart being paid (request, approval request, approval and recomputed hash all equal)."""
    areq, appr = request.approval_request, request.approval
    out: list[MandateViolation] = []

    def v(reason: MandateReason, msg: str) -> None:
        out.append(MandateViolation(reason=reason, message=msg))

    if areq.kind != ApprovalKind.CHECKOUT:
        v(R.APPROVAL_MISSING, f"Approval {areq.approval_id} is a '{areq.kind}' approval, not a checkout approval")
    if appr.approval_id != areq.approval_id:
        v(R.APPROVAL_MISSING, "The approval answer does not belong to this checkout request")
    if areq.trip_id != request.trip_id or areq.user_id != request.user_id:
        v(R.APPROVAL_MISSING, "The approval was raised for a different trip or shopper")
    if appr.user_id is not None and appr.user_id != request.user_id:
        v(R.APPROVAL_MISSING, "The approval was answered by a different shopper")
    if appr.decision == "timeout":
        v(R.APPROVAL_EXPIRED, "The checkout approval timed out")
    elif appr.decision != "approve":
        v(R.APPROVAL_REJECTED, "You did not approve this checkout")
    if appr.decision != "timeout" and appr.ts > areq.created_at + timedelta(seconds=areq.expires_s):
        v(R.APPROVAL_EXPIRED, "The checkout approval was answered after it expired")
    if appr.approval_id in set(used_approval_ids):
        v(R.APPROVAL_REPLAYED, "This approval was already used for a payment")

    try:
        actual = compute_cart_hash(request.cart, request.trip_id)
    except ValueError:
        actual = None
    hashes = {request.cart_hash, areq.cart_hash, appr.cart_hash}
    if actual is None or hashes != {actual}:
        v(R.CART_HASH_MISMATCH, "The cart changed after you approved it; please approve the new total")
    return out


def check_checkout(
    request: CheckoutRequest,
    mandate: IntentMandate | None,
    *,
    used_approval_ids: Iterable[str] = (),
    now: datetime | None = None,
    decision_id: str | None = None,
) -> MandateDecision:
    """Final gate before charging. APPROVE iff the approval checks pass and the mandate does not DENY.

    Soft mandate reasons (over budget, no mandate) are satisfied by the explicit, hash-bound checkout approval; they
    stay listed in ``reasons`` for the ledger.
    """
    now = now or utcnow()
    base = evaluate_mandate(
        mandate,
        request.cart,
        user_id=request.user_id,
        budget_cents=request.budget_cents,
        trip_id=request.trip_id,
        now=now,
        decision_id=decision_id,
    )
    approval = approval_violations(request, used_approval_ids=used_approval_ids)
    violations = approval + list(base.violations)
    reasons = _dedupe(v.reason for v in violations) or [R.OK]
    if verdict_for(reasons) == MandateVerdict.DENY:
        verdict = MandateVerdict.DENY
    else:
        verdict = MandateVerdict.APPROVE
    messages = [v.message for v in violations] or ["Within your mandate"]
    if verdict == MandateVerdict.APPROVE:
        messages.append(f"Approved by you ({request.approval.approval_id}) for {fmt_cents(request.cart.total_cents)}")
    return base.model_copy(
        update={"verdict": verdict, "reasons": reasons, "messages": messages, "violations": violations}
    )


# --------------------------------------------------------------------------------------------------------------------
# Audit chain
# --------------------------------------------------------------------------------------------------------------------
class AuditChain:
    """Append-only audit log: ``hash`` = sha256 over the entry (minus hash/signature) incl. ``prev_hash``;
    ``signature`` = HMAC over the entry incl. its hash."""

    def __init__(self, key: str | bytes | None = None, clock: Callable[[], datetime] = utcnow) -> None:
        self._key = key
        self._clock = clock
        self.entries: list[AuditEntry] = []

    def append(
        self,
        actor: str,
        action: str,
        *,
        trip_id: str | None = None,
        user_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AuditEntry:
        seq = len(self.entries)
        prev = self.entries[-1].hash if self.entries else None
        entry = AuditEntry(
            entry_id=f"aud_{seq:06d}",
            seq=seq,
            actor=actor,
            action=action,
            trip_id=trip_id,
            user_id=user_id,
            data=data or {},
            prev_hash=prev,
            ts=self._clock(),
        )
        entry = entry.model_copy(update={"hash": sha256_hex(entry, exclude={"hash", "signature"})})
        entry = entry.model_copy(update={"signature": sign(entry, self._key, exclude=SIGNATURE_EXCLUDE)})
        self.entries.append(entry)
        return entry

    def verify(self) -> bool:
        prev: str | None = None
        for i, e in enumerate(self.entries):
            if e.seq != i or e.prev_hash != prev:
                return False
            if e.hash != sha256_hex(e, exclude={"hash", "signature"}):
                return False
            if not e.signature or not verify(e, e.signature, self._key, exclude=SIGNATURE_EXCLUDE):
                return False
            prev = e.hash
        return True
