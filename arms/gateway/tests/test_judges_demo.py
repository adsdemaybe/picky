"""The judges' demo end to end through the gateway on fakes (contracts 0.2.4): suggestion cards from the purchase
database -> structured order -> plan with per-item photo verification -> the arm unloads every unit onto the counter
(checkout.unload) -> the Visa payment agent timeline -> the purchase rows (with photos) in the database."""

import asyncio
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arms.gateway.app import create_app
from arms.gateway.orchestrator import OrchestratorConfig
from arms.gateway.registry import build_registry
from arms.gateway.tests.helpers import FAST_FAKES, needs_data
from utils.config import reset_settings, set_settings

pytestmark = needs_data


@pytest.fixture(autouse=True)
def _settings():
    set_settings(
        picky_exec_mode="",
        picky_faults="",
        picky_dev=False,
        gemini_api_key="",
        anthropic_api_key="",
        perceptron_api_key="",
        picky_purchases_db=":memory:",
        picky_suggestions_llm=False,
        picky_payment_agent_llm=False,
        picky_verify_auto_accept_s=30.0,
    )
    yield
    reset_settings()


def _client() -> TestClient:
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        orchestrator_config=OrchestratorConfig(approval_channels=("web",), payment_step_pause_s=0.0),
    )
    c = TestClient(create_app(registry=reg))
    c.registry = reg  # type: ignore[attr-defined]
    return c


def _answer(c: TestClient, answered: set[str]) -> None:
    """The shopper in the web app: taps ✓ on every photo card, approves everything else (echoing the cart hash)."""
    for v in c.get("/api/verifications", params={"status": "pending"}).json():
        if v["verification_id"] not in answered:
            r = c.post(f"/api/verifications/{v['verification_id']}", json={"decision": "correct"})
            if r.status_code == 200:
                answered.add(v["verification_id"])
    for req in c.get("/api/approvals").json():
        if req["kind"] == "verify_item":
            continue
        body: dict[str, Any] = {"decision": "approve", "channel": "web"}
        if req["options"]:
            body = {"decision": "choice", "choice": req["options"][0]["option_id"], "channel": "web"}
        if req["kind"] == "checkout":
            body["cart_hash"] = req["cart_hash"]
        c.post(f"/api/approvals/{req['approval_id']}", json=body)


def test_suggestions_order_unload_pay_and_history() -> None:
    with _client() as c:
        cards = c.get("/api/users/maya/suggestions", params={"store_id": "store_ave", "limit": 6}).json()
        assert cards and cards[0]["sku"] == "SALSA-MILD" and "12 days ago" in cards[0]["reason"]
        assert {"replenish", "complement"} <= {x["kind"] for x in cards} and all(x["in_stock"] for x in cards)
        history = c.get("/api/users/maya/purchases").json()
        assert len(history) > 50 and history[0]["ts"] >= history[-1]["ts"]  # newest first, months of rows

        order = [{"sku": x["sku"], "qty": 1} for x in cards[:3]]
        plan = c.post("/api/requests", json={"user_id": "maya", "items": order, "store_id": "store_ave"})
        assert plan.status_code == 200, plan.text
        rid = plan.json()["request_id"]

        answered: set[str] = set()
        trip: dict[str, Any] = {}
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            _answer(c, answered)
            req = c.get(f"/api/requests/{rid}").json()
            trip_ids = [t for t in req.get("trip_ids", []) if t]
            if trip_ids:
                trip = c.get(f"/api/trips/{trip_ids[0]}").json()
                if trip["state"] in ("paid", "followup", "done"):
                    break
            time.sleep(0.05)
        assert trip.get("state") in ("paid", "followup", "done"), trip.get("state")
        tid = trip["trip_id"]

        events = c.registry.event_dicts()  # type: ignore[attr-defined]
        mine = [e for e in events if e.get("trip_id") == tid]
        unload = [e for e in mine if e["type"] == "checkout.unload"]
        verified = [e for e in unload if e["status"] == "verified"]
        total = verified[0]["total"]  # the ordered cards + any detour the shopper said yes to
        assert total >= 3 and len(verified) == total
        assert verified[0]["text"] == f"Unloading 1/{total}: {verified[0]['name']} → counter ✓"
        assert [e["counter_spot"] for e in verified] == list(range(total))
        assert any(e["status"] == "done" for e in unload)
        steps = [e for e in mine if e["type"] == "step.started"]
        kinds = [(e["step"]["kind"], e["step"].get("source"), e["step"].get("destination")) for e in steps]
        assert ("grasp", "basket", None) in kinds and ("place", None, "counter") in kinds
        # the payment agent only starts after the basket was verified empty (the handoff)
        handoff = max(e["seq"] for e in steps if e["step"]["kind"] == "handoff")
        pay = [e for e in mine if e["type"] == "payment.agent_step"]
        assert pay and min(e["seq"] for e in pay) > handoff
        assert [e["step"]["name"] for e in pay][-1] == "ledger"
        timeline = c.get(f"/api/trips/{tid}/payment").json()
        assert len(timeline) == 9 and all(s["simulated"] for s in timeline)

        cards_seen = [e for e in mine if e["type"] == "item.verification"]
        shelf = {e["verification"]["verification_id"] for e in cards_seen if e["verification"]["status"] == "accepted"}
        assert len(shelf) >= total  # one per purchased unit, answered ✓ in the app
        assert answered

        rows = c.get("/api/users/maya/purchases", params={"limit": 10}).json()
        bought = [r for r in rows if r["trip_id"] == tid]
        assert {x["sku"] for x in order} <= {r["sku"] for r in bought}
        assert all(r["source"] == "checkout" for r in bought)
        ordered = {x["sku"] for x in order}
        missing = [(r["sku"], r["store_id"]) for r in bought if r["sku"] in ordered and not r["photo_urls"]]
        assert not missing, missing  # every unit the robot picked carries its verification photo
        after = c.get("/api/users/maya/suggestions", params={"store_id": "store_ave", "limit": 6}).json()
        assert cards[0]["sku"] not in {x["sku"] for x in after if x["kind"] == "replenish"}  # just bought


def test_verification_routes() -> None:
    with _client() as c:
        assert c.get("/api/verifications").json() == []
        assert c.post("/api/verifications/nope", json={"decision": "correct"}).status_code == 404
        assert c.post("/api/verifications/nope", json={"decision": "maybe"}).status_code == 422
        assert c.get("/api/trips/none/payment").json() == []
        assert c.get("/api/users/nobody/purchases").status_code == 404


def test_photo_card_per_grasp_and_wrong_answer_puts_it_back() -> None:
    """On the real 2D fakes (FakeRobot can put a unit back): ✗ on the first photo -> put back + re-pick; the counter
    unload then runs for real (checkout.unload verified per unit)."""
    import math

    from arms.agent.loop import PlanLoop
    from contracts.plan import GraspStep, PlanRequest
    from contracts.policy import DEFAULT_RETRY_POLICY
    from contracts.trust import ApprovalRequest, ApprovalResponse

    set_settings(picky_exec_mode="", picky_faults="", gemini_api_key="", anthropic_api_key="", perceptron_api_key="")
    try:
        reg = build_registry(real_arms=set(), fake_opts={"robot": {"realtime_factor": math.inf}})
        s = reg.services
        s.approvals = None  # answers come from the injected shopper below
        asked: list[ApprovalRequest] = []
        wrong_once = {"left": 1}

        async def answer(req: ApprovalRequest) -> ApprovalResponse:
            asked.append(req)
            if req.kind.value == "verify_item":
                pick = "correct"
                if wrong_once["left"]:
                    wrong_once["left"] -= 1
                    pick = "wrong"
                return ApprovalResponse(approval_id=req.approval_id, decision="choice", choice=pick)
            return ApprovalResponse(approval_id=req.approval_id, decision="approve")

        runner = PlanLoop(s, policy=DEFAULT_RETRY_POLICY, answer=answer, verify_items=True)

        async def go() -> Any:
            plan = await s.planner.plan(PlanRequest(robot_id="rover-ave-1", text="2 mild salsa", store_id="store_ave"))
            return await runner.run_plan(plan)

        out = asyncio.run(go())
    finally:
        reset_settings()
    assert out.status == "completed", out.summary
    cards = [r for r in asked if r.kind.value == "verify_item"]
    assert len(cards) >= 3 and all(r.payload.get("verification") for r in cards)  # 2 units + the re-pick
    events = [e for e in reg.bus.recent(after_seq=0)]
    statuses = [e.verification.status for e in events if e.type == "item.verification"]
    assert "rejected" in statuses and statuses.count("accepted") >= 2
    assert any(e.type == "step.failed" and e.failure.code == "wrong_item_grasped" for e in events)
    unload = [e for e in events if e.type == "checkout.unload" and e.status == "verified"]
    assert len(unload) == 2 and unload[-1].text.startswith("Unloading 2/2")
    shelf_grasps = [st for st in out.plan.steps if isinstance(st, GraspStep) and st.source == "shelf"]
    assert shelf_grasps
