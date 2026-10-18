"""The Visa payment agent (judges' demo): the payment.agent_step timeline around TrustCore.checkout, deterministic
money/mandate decisions, LLM narration only (mocked client, grounded), sandbox labels."""

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from arms.trust import run_payment_agent
from arms.trust.agent import mock_agentic_token
from arms.trust.fake import FakeTrustService
from arms.trust.tests.helpers import checkout_request, maya_mandate, taco_cart
from contracts.trust import PAYMENT_SANDBOX_LABEL, CartMandate, PaymentAgentStep


class FakeLlm:
    """Messages-shaped client: returns canned narration JSON."""

    provider = "gemini"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls = 0
        self.messages = self

    async def create(self, **kw: Any) -> Any:
        self.calls += 1
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def _run(trust: FakeTrustService, request: Any, **kw: Any) -> tuple[Any, list[PaymentAgentStep]]:
    steps: list[PaymentAgentStep] = []

    async def sink(step: PaymentAgentStep) -> None:
        steps.append(step)

    receipt = asyncio.run(run_payment_agent(trust, request, sink, pause_s=0, **kw))
    return receipt, steps


def test_timeline_on_an_approved_cart() -> None:
    trust = FakeTrustService()
    asyncio.run(trust.set_mandate(maya_mandate()))
    receipt, steps = _run(trust, checkout_request(), narrate=False)
    assert receipt.status == "succeeded"
    assert [s.name for s in steps] == [
        "cart_mandate",
        "intent_check",
        "passkey_requested",
        "passkey_verified",
        "token_issued",
        "authorized",
        "captured",
        "receipt_signed",
        "ledger",
    ]
    assert [s.n for s in steps] == list(range(1, 10)) and all(s.status == "ok" for s in steps)
    assert all(s.simulated and s.label == PAYMENT_SANDBOX_LABEL and s.provider == "mock_vic" for s in steps)
    assert all(s.narrated_by == "template" and s.narration for s in steps)
    assert all(s.amount_cents == taco_cart().total_cents for s in steps)
    tok = next(s for s in steps if s.name == "token_issued")
    assert "vic_tok_" in tok.detail and "4242" not in tok.detail  # tokenized credential, never a PAN
    assert steps[-1].data["entries"] >= 1  # the audit chain got the checkout


def test_mandate_deny_stops_before_any_token() -> None:
    trust = FakeTrustService()
    asyncio.run(trust.set_mandate(maya_mandate(cap_cents=1000)))
    receipt, steps = _run(trust, checkout_request(), narrate=False)
    assert receipt.status == "declined"
    assert [s.name for s in steps] == ["cart_mandate", "intent_check", "declined"]
    assert steps[1].status == "failed" and "token_issued" not in {s.name for s in steps}


def test_llm_narrates_only_grounded_lines() -> None:
    trust = FakeTrustService()
    asyncio.run(trust.set_mandate(maya_mandate()))
    total = f"${taco_cart().total_cents / 100:.2f}"
    llm = FakeLlm(
        {
            "lines": [
                {"step": "cart_mandate", "line": f"Your basket is wrapped up for {total}."},
                {"step": "captured", "line": "Captured $999.99 from your account."},  # invented number: rejected
            ]
        }
    )
    _, steps = _run(trust, checkout_request(), narrate=True, llm_client=llm)
    assert llm.calls == 1
    by = {s.name: s for s in steps}
    assert by["cart_mandate"].narrated_by == "llm" and total in by["cart_mandate"].narration
    assert by["captured"].narrated_by == "template" and "999" not in by["captured"].narration


def test_token_is_deterministic_and_bound_to_the_cart() -> None:
    cm = CartMandate.from_cart(taco_cart(), trip_id="trip_demo1", user_id="maya")
    a, b = mock_agentic_token(cm, "maya"), mock_agentic_token(cm, "maya")
    assert a["token"] == b["token"] and a["bound_to"] == cm.cart_mandate_id
    assert a["max_amount_cents"] == cm.total_cents
