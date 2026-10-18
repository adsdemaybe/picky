"""The Visa payment agent (judges' demo, Visa "Reimagine Shopping" track): checkout run the way Visa Intelligent
Commerce describes agentic payments, as a timeline of ``PaymentAgentStep``s. SANDBOX / SIMULATED — no real money.

    cart_mandate      the verified basket -> a CartMandate (lines, total, cart hash), signed by code
    intent_check      the shopper's IntentMandate (cap, per-item cap, categories, ask-above-$X) checked BY CODE
    passkey_requested the checkout approval the shopper saw in the app (passkey-style, bound to the cart hash)
    passkey_verified  their explicit approval (who, channel, when) — nothing pays without it
    token_issued      a MOCK Visa Intelligent Commerce agentic token: tokenized credential, no PAN, amount-capped,
                      expires in 10 min, bound to the cart mandate
    authorized        authorization against the token (mock VIC; Stripe test mode when STRIPE_SECRET_KEY is set)
    captured          capture
    receipt_signed    the trust core's signed receipt
    ledger            the hash-chained audit entries it appended
    declined / error  a mandate DENY / payment decline stops the timeline there

Money math, mandate checks, the approval binding and the charge are deterministic code: this module only wraps
``TrustCore.checkout`` (which re-checks everything) and never decides an amount. The LLM (``utils.llm``, kind 'fast')
may only write the one-line ``narration`` of each step from the step's facts; a narration quoting a number that is
not in those facts is replaced by the template. No key / disabled / timeout -> templates (``narrated_by='template'``).
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field

from contracts.common import money, utcnow
from contracts.trust import (
    PAYMENT_SANDBOX_LABEL,
    CartMandate,
    CheckoutRequest,
    MandateVerdict,
    PaymentAgentStep,
    Receipt,
)
from utils.log import get_logger

log = get_logger(__name__)

StepSink = Callable[[PaymentAgentStep], Awaitable[None]]
TOKEN_TTL_S = 600
NARRATION_TIMEOUT_S = 4.0
STEP_PAUSE_S = 0.35
"""A short beat between steps so the web app's timeline visibly fills in (the work itself is instant)."""

TITLES: dict[str, str] = {
    "cart_mandate": "Cart mandate built",
    "intent_check": "Your spending rules checked",
    "passkey_requested": "Passkey approval requested",
    "passkey_verified": "Passkey approval verified",
    "token_issued": "Visa agentic token issued",
    "authorized": "Payment authorized",
    "captured": "Payment captured",
    "receipt_signed": "Receipt signed",
    "ledger": "Audit ledger updated",
    "declined": "Payment declined",
    "error": "Payment error",
}


class _Narration(BaseModel):
    step: str = Field(description="The step name copied exactly.")
    line: str = Field(description="One short first-person sentence (<= 110 chars) using ONLY the given facts.")


class _Narrations(BaseModel):
    lines: list[_Narration]


def _template(name: str, facts: dict[str, Any]) -> str:
    t = {
        "cart_mandate": "I packaged your verified basket into a cart mandate: {lines} lines, {total}.",
        "intent_check": "Your rules allow it: {total} is within your {cap} cap.",
        "passkey_requested": "I asked you to approve {total} with your passkey in the app.",
        "passkey_verified": "You approved it via {channel}; the approval is bound to this exact cart.",
        "token_issued": "Visa issued a one-time agentic token capped at {total}; your card number never leaves Visa.",
        "authorized": "Authorized {total} on the token ({provider}).",
        "captured": "Captured {total}. No real money moved: this is a sandbox.",
        "receipt_signed": "Receipt {receipt} signed so it can't be altered.",
        "ledger": "Logged {entries} audit entries in your tamper-evident ledger.",
        "declined": "I stopped: {reason}",
        "error": "Payment failed: {reason}",
    }[name]
    try:
        return t.format(**facts)
    except (KeyError, IndexError):
        return TITLES.get(name, name)


_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def _grounded(line: str, facts: dict[str, Any]) -> bool:
    allowed = set(_NUM.findall(" ".join(str(v) for v in facts.values())))
    return 0 < len(line) <= 140 and all(n in allowed for n in _NUM.findall(line))


async def _llm_narrations(facts: dict[str, dict[str, Any]], *, client: Any = None) -> dict[str, str]:
    from utils import llm

    if client is None and not llm.is_configured():
        return {}
    prompt = (
        "You are Picky's payment agent narrating a SANDBOX Visa agentic checkout to the shopper. For each step write "
        "one short, plain first-person sentence using ONLY the facts given for that step (no other numbers, no "
        "promises). Steps and facts:\n" + "\n".join(f"- {k}: {v}" for k, v in facts.items())
    )
    try:
        out = await asyncio.wait_for(
            llm.complete_json(prompt, _Narrations, kind="fast", max_tokens=800, client=client), NARRATION_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 - narration is cosmetic: templates
        log.info("payment agent narration unavailable (%s): templates", type(exc).__name__)
        return {}
    return {n.step: n.line.strip() for n in out.lines if n.step in facts and _grounded(n.line.strip(), facts[n.step])}


def mock_agentic_token(cart_mandate: CartMandate, user_id: str) -> dict[str, Any]:
    """A MOCK Visa Intelligent Commerce agentic token (tokenized credential, no PAN): deterministic per cart."""
    digest = hashlib.sha256(f"{user_id}:{cart_mandate.cart_hash}".encode()).hexdigest()
    return {
        "token": f"vic_tok_{digest[:20]}",
        "token_ref": f"…{digest[16:20]}",
        "credential": "Visa network token (no PAN shared)",
        "max_amount_cents": cart_mandate.total_cents,
        "expires_at": (utcnow() + timedelta(seconds=TOKEN_TTL_S)).isoformat(timespec="seconds"),
        "bound_to": cart_mandate.cart_mandate_id,
    }


class VisaPaymentAgent:
    """Runs one checkout through ``trust`` (a TrustCore: mandates, adapter, signed receipt, audit chain)."""

    def __init__(self, trust: Any, *, narrate: bool = True, llm_client: Any = None, pause_s: float = STEP_PAUSE_S):
        self.trust = trust
        self.narrate = narrate
        self.llm_client = llm_client
        self.pause_s = pause_s

    async def run(self, request: CheckoutRequest, on_step: StepSink | None = None) -> Receipt:
        trust = self.trust
        cart = request.cart
        mandate = await trust.get_mandate(request.user_id)
        provider = "stripe_test" if getattr(trust.payments, "provider", "") == "stripe_test" else "mock_vic"
        cm = CartMandate.from_cart(
            cart,
            trip_id=request.trip_id,
            user_id=request.user_id,
            intent_mandate_id=mandate.mandate_id if mandate else None,
        )
        token = mock_agentic_token(cm, request.user_id)
        total = money(cart.total_cents)
        appr, areq = request.approval, request.approval_request
        facts: dict[str, dict[str, Any]] = {
            "cart_mandate": {"lines": len(cart.active_lines()), "total": total, "hash": cm.cart_hash[:10]},
            "intent_check": {
                "total": total,
                "cap": money(mandate.cap_cents) if mandate else "no",
                "ask_above": money(mandate.ask_above_cents) if mandate else "-",
            },
            "passkey_requested": {"total": total},
            "passkey_verified": {"channel": appr.channel, "user": appr.user_id or request.user_id},
            "token_issued": {"total": total, "token": token["token_ref"], "minutes": TOKEN_TTL_S // 60},
            "authorized": {"total": total, "provider": "Stripe test mode" if provider == "stripe_test" else "mock VIC"},
            "captured": {"total": total},
        }
        lines = await _llm_narrations(facts, client=self.llm_client) if self.narrate else {}
        n = 0
        steps: list[PaymentAgentStep] = []

        async def emit(name: str, status: str = "ok", detail: str = "", data: dict[str, Any] | None = None, **f: Any):
            nonlocal n
            n += 1
            f2 = {**facts.get(name, {}), **f}
            narr = lines.get(name)
            step = PaymentAgentStep(
                step_id=f"pay-{request.trip_id}-{n:02d}",
                trip_id=request.trip_id,
                user_id=request.user_id,
                n=n,
                name=name,  # type: ignore[arg-type]
                status=status,  # type: ignore[arg-type]
                title=TITLES.get(name, name),
                detail=detail,
                narration=narr or _template(name, f2),
                narrated_by="llm" if narr else "template",
                provider=provider,
                label=PAYMENT_SANDBOX_LABEL,
                amount_cents=cart.total_cents,
                data={k: v for k, v in (data or {}).items() if isinstance(v, str | int | float | bool) or v is None},
            )
            steps.append(step)
            if on_step is not None:
                try:
                    await on_step(step)
                except Exception as exc:  # noqa: BLE001 - the timeline is a view; the payment goes on
                    log.warning("payment step publish failed: %s", exc)
            if self.pause_s:
                await asyncio.sleep(self.pause_s)

        await emit(
            "cart_mandate",
            detail=f"{len(cart.active_lines())} lines · {total} · cart hash {cm.cart_hash[:12]}…",
            data={"cart_mandate_id": cm.cart_mandate_id, "cart_hash": cm.cart_hash},
        )
        decision = await trust.evaluate(request.user_id, cart, None, request.budget_cents, request.trip_id)
        verdict = getattr(decision.verdict, "value", decision.verdict)
        if decision.verdict == MandateVerdict.DENY:
            reason = "; ".join(decision.messages) or "your spending rules say no"
            await emit("intent_check", "failed", detail=reason, data={"verdict": verdict})
            await emit("declined", "failed", detail=reason, reason=reason)
            return await trust.checkout(request)  # the core records the DENY (declined receipt + audit)
        await emit(
            "intent_check",
            detail=(
                f"verdict {verdict} · cap {money(mandate.cap_cents)} · ask above {money(mandate.ask_above_cents)}"
                if mandate
                else f"verdict {verdict} (no mandate on file)"
            ),
            data={"verdict": verdict, "mandate_id": mandate.mandate_id if mandate else None},
        )
        await emit(
            "passkey_requested",
            detail=f"'{areq.title}' · bound to cart hash {(areq.cart_hash or request.cart_hash)[:12]}…",
            data={"approval_id": areq.approval_id},
        )
        await emit(
            "passkey_verified",
            detail=f"{appr.decision} by {appr.user_id or request.user_id} via {appr.channel} at {appr.ts:%H:%M:%S}",
            data={"approval_id": appr.approval_id, "channel": appr.channel},
        )
        await emit(
            "token_issued",
            detail=f"{token['token'][:12]}…{token['token'][-4:]} · max {total} · expires in {TOKEN_TTL_S // 60} min",
            data={k: v for k, v in token.items() if k != "token"} | {"token_tail": token["token"][-4:]},
        )
        audit_before = len(trust.audit.entries)
        pay_token = token["token"] if provider == "mock_vic" else request.payment_token  # Stripe: its test card
        receipt: Receipt = await trust.checkout(request.model_copy(update={"payment_token": pay_token}))
        if receipt.status != "succeeded":
            await emit(
                "declined" if receipt.status == "declined" else "error",
                "failed",
                detail=receipt.error or receipt.status,
                reason=receipt.error or receipt.status,
            )
            return receipt
        ref = receipt.provider_ref or f"auth_{hashlib.sha256(receipt.receipt_id.encode()).hexdigest()[:10]}"
        await emit("authorized", detail=f"{total} on {token['token_ref']} · auth {ref}", data={"provider_ref": ref})
        await emit("captured", detail=f"{total} captured · {receipt.provider} sandbox", data={"provider_ref": ref})
        await emit(
            "receipt_signed",
            detail=f"{receipt.receipt_id} · signature {(receipt.signature or '')[:16]}…",
            data={"receipt_id": receipt.receipt_id},
            receipt=receipt.receipt_id,
        )
        added = trust.audit.entries[audit_before:]
        head = trust.audit.entries[-1].hash if trust.audit.entries else ""
        await emit(
            "ledger",
            detail=f"{len(added)} entr{'y' if len(added) == 1 else 'ies'} · chain head {head[:12]}…",
            data={"entries": len(added), "head": head},
            entries=len(added),
        )
        return receipt


async def run_payment_agent(
    trust: Any,
    request: CheckoutRequest,
    on_step: StepSink | None = None,
    *,
    narrate: bool | None = None,
    llm_client: Any = None,
    pause_s: float = STEP_PAUSE_S,
) -> Receipt:
    """Checkout through the Visa payment agent (``trust`` must be a TrustCore: the fake or the real service)."""
    if narrate is None:
        try:
            from utils.config import get_settings

            narrate = bool(get_settings().picky_payment_agent_llm)
        except Exception:  # noqa: BLE001
            narrate = False
    agent = VisaPaymentAgent(trust, narrate=narrate, llm_client=llm_client, pause_s=pause_s)
    return await agent.run(request, on_step)


__all__ = ["VisaPaymentAgent", "mock_agentic_token", "run_payment_agent"]
