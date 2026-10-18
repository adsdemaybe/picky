"""Robot brain: scripted executor (fake) + Claude tool loop with a mocked client (base deps only, no network)."""

from __future__ import annotations

import asyncio
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

from arms.agent import create_brain, create_brain_fake
from arms.agent.brain import MAX_ITERS, ClaudeBrain, ScriptedBrain
from arms.agent.tests.brain_stubs import ROBOT, STORE, answer, demo_trip, shop_goal, slot_id, world
from contracts.events import EVENT_PRODUCERS
from contracts.fleet import Goal, GoalItem, GoalStatus
from contracts.services import RobotBrainService, Services
from contracts.sim import RobotError
from contracts.tools import (
    BRAIN_STRICT_TOOLS,
    STRICT_TOOL_LIMITS,
    brain_tool_definitions,
    strict_schema_budget,
)
from contracts.trust import MandateVerdict
from utils.config import reset_settings, set_settings


@pytest.fixture(autouse=True)
def _no_llm_key():
    set_settings(anthropic_api_key="")
    yield
    reset_settings()


def run(coro):
    return asyncio.run(coro)


def events_of(parts, type_: str) -> list[Any]:
    return [e for e in parts["events"].events if e.type == type_]


# --- conformance ---------------------------------------------------------------------------------------------------
def test_brain_factories_satisfy_protocol_without_calling_services():
    empty = Services()
    assert isinstance(create_brain_fake(empty), RobotBrainService)
    assert isinstance(create_brain(empty), RobotBrainService)


def test_brain_tool_request_fits_strict_limits():
    tools = brain_tool_definitions(strict=True)
    budget = strict_schema_budget(tools)
    assert all(budget[k] <= STRICT_TOOL_LIMITS[k] for k in STRICT_TOOL_LIMITS)
    assert {t["name"] for t in tools if t.get("strict")} == set(BRAIN_STRICT_TOOLS)


# --- scripted shop goal ----------------------------------------------------------------------------------------------
def test_scripted_shop_goal_reproduces_the_trip_story():
    services, parts = world()
    trip = parts["trips"].trip
    goal = parts["fleet"].add(shop_goal(trip))
    brain = ScriptedBrain(services)
    final = run(brain.run_goal(ROBOT, goal))

    assert final.status == GoalStatus.COMPLETED
    status = {i.item_id: i.status for i in final.items}
    assert status == {"s_tort": "in_basket", "s_hot": "in_basket", "s_avo": "in_basket", "s_cil": "online"}

    trips = parts["trips"]
    lines = trips.lines()
    in_store = {ln.sku: ln.qty for ln in lines if ln.source == "in_store"}
    assert in_store == {"TORT-01": 2, "SALSA-HOT": 1, "AVO-01": 2}
    assert [ln.sku for ln in lines if ln.source == "online"] == ["CIL-01"]
    # add_line ONCE per goal item, check_line on the FULL line before the first unit
    checks = [c[1] for c in trips.calls if c[0] == "check_line"]
    assert {c.sku: c.qty for c in checks} == {"TORT-01": 2, "SALSA-HOT": 1, "AVO-01": 2}
    # stop statuses mirrored onto the trip
    assert trips.trip.stop_status == {"s_tort": "done", "s_hot": "done", "s_avo": "done", "s_cil": "failed"}
    # questions: detour first (route order), then the produce pick choice
    kinds = [a.kind.value for a in trips.asked]
    assert kinds == ["recommendation", "pick_choice"]
    rec_ask = trips.asked[0]
    assert rec_ask.rec_id == "rec_hot" and rec_ask.amount_cents == 379 and rec_ask.robot_id == ROBOT
    assert rec_ask.payload["sponsored"] is False and "interest" in rec_ask.payload
    assert "Detour: +4 m" in rec_ask.body
    # the shopper picked option #2: that unit is picked first
    robot = parts["robot"]
    avo_picks = [c[1][1] for c in robot.calls if c[0] == "pick" and c[1][0] == slot_id("AVO-01")]
    assert avo_picks[0] == trips.asked[1].options[1].option_id
    # basket unloaded at the checkout at the end
    assert robot.calls[-1] == ("unload", 5)
    assert robot.pose == parts["catalog"].plan.checkout
    # stock: a scan per visited slot (mispricing / OOS go through apply_scan), a pick per unit
    assert len(parts["catalog"].picks) == 5
    visited = {slot_id(s) for s in ("TORT-01", "SALSA-HOT", "AVO-01", "CIL-01")}
    assert {s.slot_id for s in parts["catalog"].scans} == visited


def test_scripted_shop_events_carry_the_goal_envelope():
    services, parts = world()
    goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
    run(ScriptedBrain(services).run_goal(ROBOT, goal))
    evs = parts["events"].events
    agent_types = {t for t, producer in EVENT_PRODUCERS.items() if producer == "agent"}
    seen = {e.type for e in evs}
    for t in ("brain.step", "robot.arrived", "scan.result", "pick.choices", "item.picked", "basket.verified"):
        assert t in seen, t
    for e in evs:
        if e.type in agent_types:
            assert (e.trip_id, e.store_id, e.robot_id, e.user_id, e.request_id) == (
                "trip_1",
                STORE,
                ROBOT,
                "maya",
                "req_1",
            ), e.type
    steps = [e.step for e in events_of(parts, "brain.step")]
    assert [s.step_no for s in steps] == list(range(1, len(steps) + 1))
    assert {s.kind for s in steps} >= {"thought", "tool_call", "tool_result", "observation", "message"}
    assert all(s.model == "scripted" for s in steps)
    picked = events_of(parts, "item.picked")
    assert len(picked) == 5 and all(p.line_id for p in picked)
    checks = events_of(parts, "basket.verified")
    assert checks and all(c.check.ok for c in checks)


def test_recent_steps_and_status():
    services, parts = world()
    goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
    brain = ScriptedBrain(services)
    run(brain.run_goal(ROBOT, goal))
    steps = run(brain.recent_steps(ROBOT, limit=10))
    assert len(steps) == 10
    assert [s.step_no for s in steps] == sorted((s.step_no for s in steps), reverse=True)
    assert run(brain.recent_steps(goal_id="goal_1", limit=1000))[0].goal_id == "goal_1"
    assert run(brain.recent_steps(goal_id="nope")) == []
    older = run(brain.recent_steps(ROBOT, before=steps[-1].ts, limit=1000))
    assert all(s.ts < steps[-1].ts for s in older)
    st = run(brain.status(ROBOT))
    assert st.state == "idle" and st.goal_id is None and st.mode == "scripted" and st.last_step is not None


def test_declined_detour_is_skipped_and_replanned():
    def policy(req):
        return answer(req, "reject") if req.kind.value == "recommendation" else answer(req)

    services, parts = world(policy=policy)
    goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert final.item("s_hot").status == "declined"
    assert parts["trips"].trip.stop_status["s_hot"] == "declined"
    assert "SALSA-HOT" not in {ln.sku for ln in parts["trips"].lines()}
    assert parts["trips"].replans >= 2  # initial plan_route + the re-plan after 'no'
    assert slot_id("SALSA-HOT") not in [c[1] for c in parts["robot"].calls if c[0] == "scan"]


def test_over_threshold_asks_then_adds_with_the_approval():
    services, parts = world()
    trips = parts["trips"]
    trips.verdicts["TORT-01"] = MandateVerdict.NEEDS_APPROVAL
    goal = parts["fleet"].add(shop_goal(trips.trip))
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    asks = [a for a in trips.asked if a.kind.value == "over_threshold"]
    assert len(asks) == 1
    assert asks[0].sku == "TORT-01" and asks[0].amount_cents == 2 * 299
    assert asks[0].payload["reasons"] == ["above_ask_threshold"]
    assert final.item("s_tort").status == "in_basket"
    idx = [n for n, c in enumerate(trips.calls) if c[0] == "add_line" and c[1].sku == "TORT-01"][0]
    assert trips.calls[idx + 1] == ("approval_id", asks[0].approval_id)


def test_over_threshold_declined_and_mandate_deny():
    def policy(req):
        return answer(req, "reject") if req.kind.value == "over_threshold" else answer(req)

    services, parts = world(policy=policy)
    trips = parts["trips"]
    trips.verdicts["TORT-01"] = MandateVerdict.NEEDS_APPROVAL
    trips.verdicts["AVO-01"] = MandateVerdict.DENY
    goal = parts["fleet"].add(shop_goal(trips.trip))
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert final.item("s_tort").status == "declined"
    assert final.item("s_avo").status == "failed"
    assert trips.trip.stop_status["s_avo"] == "failed"
    assert not {"TORT-01", "AVO-01"} & {ln.sku for ln in trips.lines()}
    assert ("pick", (slot_id("TORT-01"), None)) not in parts["robot"].calls


def test_grasp_failed_is_retried_once():
    services, parts = world()
    parts["robot"].fail["pick"] = ["grasp_failed"]
    goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert final.item("s_tort").status == "in_basket"
    assert final.status == GoalStatus.COMPLETED


def test_navigation_blocked_twice_fails_the_item_but_not_the_goal():
    services, parts = world()
    parts["robot"].fail["navigate"] = ["path_blocked", "path_blocked"]
    goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert final.item("s_tort").status == "failed"
    assert final.status == GoalStatus.COMPLETED
    assert any(e.type == "error" for e in parts["events"].events)


def test_fatal_robot_error_raises_without_failing_the_goal():
    services, parts = world()
    parts["robot"].fail["navigate"] = ["battery_low"]
    goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
    with pytest.raises(RobotError) as exc:
        run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert exc.value.code == "battery_low"
    assert all(u.status not in (GoalStatus.FAILED, GoalStatus.COMPLETED) for u in parts["fleet"].updates)


def test_cancellation_withdraws_the_pending_question():
    gate = asyncio.Event()

    async def policy(req):
        await gate.wait()  # never answered
        return answer(req)

    async def scenario():
        services, parts = world(policy=policy)
        goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
        brain = ScriptedBrain(services)
        task = asyncio.create_task(brain.run_goal(ROBOT, goal))
        for _ in range(200):
            await asyncio.sleep(0)
            if parts["trips"].asked:
                break
        assert parts["trips"].asked, "brain should be waiting on the detour question"
        assert parts["fleet"].goals["goal_1"].status == GoalStatus.WAITING
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return parts

    parts = run(scenario())
    assert parts["approvals"].cancelled == [parts["trips"].asked[0].approval_id]


def test_message_mid_goal_adds_an_item_and_skips_another():
    gate = asyncio.Event()

    async def policy(req):
        if req.kind.value == "recommendation":
            await gate.wait()
        return answer(req, "choice", req.options[0].option_id) if req.options else answer(req)

    async def scenario():
        services, parts = world(policy=policy)
        goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
        brain = ScriptedBrain(services)
        task = asyncio.create_task(brain.run_goal(ROBOT, goal))
        for _ in range(200):
            await asyncio.sleep(0)
            if parts["trips"].asked:
                break
        added = await brain.handle_message(ROBOT, "also grab limes", user_id="maya")
        skipped = await brain.handle_message(ROBOT, "skip the cilantro", user_id="maya")
        status = await brain.handle_message(ROBOT, "status?")
        gate.set()
        final = await task
        return parts, added, skipped, status, final

    parts, added, skipped, status, final = run(scenario())
    assert "Limes" in added.text and added.goal_id == "goal_1"
    assert any(s.role == "user" for s in added.steps)
    assert "skipping" in skipped.text.lower()
    assert "Working on" in status.text
    lime = next(i for i in final.items if i.sku == "LIME-01")
    assert lime.status == "in_basket" and lime.stop_id is not None and lime.list_item_id
    assert final.item("s_cil").status == "skipped"
    assert parts["trips"].trip.stop_status["s_cil"] == "skipped"
    assert "LIME-01" in {ln.sku for ln in parts["trips"].lines()}
    assert any(c[0] == "update_list" for c in parts["trips"].calls)


def test_idle_message_reports_status_and_what_it_sees():
    services, parts = world()
    brain = ScriptedBrain(services)
    idle = run(brain.handle_message(ROBOT, "how are you doing?"))
    assert idle.goal_id is None and "free" in idle.text and "80%" in idle.text
    seen = run(brain.handle_message(ROBOT, "what do you see?"))
    assert seen.text == "Shelf with 6 units in view."
    assert any(s.kind == "observation" for s in seen.steps)
    both = run(brain.handle_message(ROBOT, "what are you doing and what do you see?"))
    assert "free" in both.text and both.text.endswith("Shelf with 6 units in view."), both.text


# --- other goal kinds -------------------------------------------------------------------------------------------------
def _goal(**kw: Any) -> Goal:
    base: dict[str, Any] = {"goal_id": "goal_x", "store_id": STORE, "robot_id": ROBOT, "status": "active"}
    base.update(kw)
    return Goal(**base)


def test_shelf_scan_goal_updates_stock_and_progress():
    services, parts = world()
    slots = [slot_id(s) for s in ("CIL-01", "BEAN-01", "TORT-01")]
    goal = parts["fleet"].add(
        _goal(kind="shelf_scan", title="Scan", end_at="stay", user_id="staff", categories=["produce"], slot_ids=slots)
    )
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert final.status == GoalStatus.COMPLETED
    assert sorted(final.scanned_slot_ids) == sorted(slots)
    assert final.progress == 1.0
    assert {s.slot_id for s in parts["catalog"].scans} == set(slots)
    assert all(s.robot_id == ROBOT and s.goal_id == "goal_x" for s in parts["catalog"].scans)
    assert "out of stock: CIL-01" in (final.summary or "")
    assert "mispriced: BEAN-01" in (final.summary or "")
    arrived = events_of(parts, "robot.arrived")
    assert len(arrived) == 3 and all(e.trip_id is None and e.stop_id is None for e in arrived)


def test_shelf_scan_resumes_from_scanned_slots():
    services, parts = world()
    slots = [slot_id(s) for s in ("CIL-01", "BEAN-01")]
    goal = parts["fleet"].add(
        _goal(kind="shelf_scan", title="Scan", end_at="stay", slot_ids=slots, scanned_slot_ids=slots[:1])
    )
    run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert [s.slot_id for s in parts["catalog"].scans] == slots[1:]


def test_fetch_goal_without_trip_picks_and_unloads():
    services, parts = world()
    goal = parts["fleet"].add(
        _goal(kind="fetch", title="Fetch salsa", user_id="staff", items=[GoalItem(item_id="i1", query="salsa")])
    )
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert final.status == GoalStatus.COMPLETED, final.summary
    item = final.item("i1")
    assert item.status == "in_basket" and item.sku and item.sku.startswith("SALSA")
    assert parts["trips"].lines() == []  # no trip -> no cart, no mandate
    assert parts["robot"].calls[-1][0] == "unload"


def test_return_to_dock_goal():
    services, parts = world()
    goal = parts["fleet"].add(_goal(kind="return_to_dock", title="Charge", end_at="dock"))
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert final.status == GoalStatus.COMPLETED
    assert parts["robot"].calls[0][0] == "navigate"


def test_runs_without_fleet_or_trips_wired():
    services, parts = world()
    services.fleet = None
    goal = _goal(kind="return_to_dock", title="Charge", end_at="dock")
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert final.status == GoalStatus.COMPLETED


# --- Claude brain with a mocked client ----------------------------------------------------------------------------
class Block(SimpleNamespace):
    def model_dump(self, mode: str = "python", exclude_none: bool = False) -> dict[str, Any]:
        return {k: v for k, v in vars(self).items() if not (exclude_none and v is None)}


def tool(name: str, n: int, **inp: Any) -> Block:
    return Block(type="tool_use", id=f"tu_{n}", name=name, input=inp)


class FakeClient:
    """Returns scripted responses; records every request."""

    def __init__(self, turns: list[Any]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kw: Any) -> Any:
        self.requests.append({**kw, "messages": list(kw["messages"])})
        await asyncio.sleep(0)
        if not self.turns:
            raise RuntimeError("no more scripted turns")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        blocks = turn if isinstance(turn, list) else [turn]
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleNamespace(content=blocks, stop_reason=stop)


def _dock_goal() -> Goal:
    return _goal(kind="return_to_dock", title="Charge", end_at="dock")


def test_claude_brain_runs_the_tool_loop():
    client = FakeClient(
        [
            [Block(type="text", text="Heading to my dock."), tool("navigate_to", 1, target="dock")],
            tool("update_progress", 2, note="Almost there", progress=0.9),
            tool("finish_goal", 3, status="completed", summary="Docked."),
        ]
    )
    services, parts = world()
    goal = parts["fleet"].add(_dock_goal())
    brain = ClaudeBrain(services, client=client)
    final = run(brain.run_goal(ROBOT, goal))
    assert final.status == GoalStatus.COMPLETED and final.summary == "Docked."
    assert len(client.requests) == 3
    req = client.requests[0]
    assert {t["name"] for t in req["tools"] if t.get("strict")} == set(BRAIN_STRICT_TOOLS)
    assert "SO-101" in req["system"][0]["text"]
    assert "return_to_dock" in req["messages"][0]["content"]
    # tool results flow back with the right ids
    second = client.requests[1]["messages"][-1]["content"]
    assert second[0]["type"] == "tool_result" and second[0]["tool_use_id"] == "tu_1"
    steps = [e.step for e in events_of(parts, "brain.step")]
    assert steps[0].kind == "thought" and steps[0].text == "Heading to my dock."
    assert all(s.model != "scripted" for s in steps)
    st = run(brain.status(ROBOT))
    assert st.mode == "llm" and st.iterations == 3


def test_claude_brain_tool_errors_are_returned_to_the_model():
    client = FakeClient(
        [
            tool("navigate_to", 1, target="slot"),  # invalid: slot needs slot_id
            tool("pick_item", 2, slot_id="AVE-NOPE", sku="X"),
            tool("finish_goal", 3, status="failed", summary="Gave up.", failure_code="unknown_slot"),
        ]
    )
    services, parts = world()
    goal = parts["fleet"].add(_dock_goal())
    final = run(ClaudeBrain(services, client=client).run_goal(ROBOT, goal))
    assert final.status == GoalStatus.FAILED and final.failure_code == "unknown_slot"
    r1 = client.requests[1]["messages"][-1]["content"][0]
    r2 = client.requests[2]["messages"][-1]["content"][0]
    assert r1["is_error"] and "invalid_input" in r1["content"]
    assert r2["is_error"] and "unknown_slot" in r2["content"]


def test_claude_brain_falls_back_to_scripted_on_llm_failure():
    client = FakeClient([tool("plan_route", 1), RuntimeError("overloaded")])
    services, parts = world()
    goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
    brain = ClaudeBrain(services, client=client)
    final = run(brain.run_goal(ROBOT, goal))
    assert final.status == GoalStatus.COMPLETED
    assert Counter(i.status for i in final.items)["in_basket"] == 3
    errors = events_of(parts, "error")
    assert any(e.code == "llm_fallback" for e in errors)
    steps = [e.step for e in events_of(parts, "brain.step")]
    assert any(s.kind == "error" for s in steps)
    assert run(brain.status(ROBOT)).mode == "scripted"


def test_claude_brain_injects_user_messages_and_checkpoints():
    turns = [tool("update_progress", n, note=f"step {n}") for n in range(1, 7)]
    turns.append(tool("finish_goal", 7, status="completed", summary="done"))
    client = FakeClient(turns)

    async def scenario():
        services, parts = world()
        goal = parts["fleet"].add(_dock_goal())
        brain = ClaudeBrain(services, client=client)
        task = asyncio.create_task(brain.run_goal(ROBOT, goal))
        for _ in range(100):
            if client.requests:
                break
            await asyncio.sleep(0)
        reply = await brain.handle_message(ROBOT, "please hurry")
        await task
        return reply

    reply = run(scenario())
    assert reply.text == "Got it."
    texts = [
        b["text"]
        for r in client.requests
        for m in r["messages"]
        if m["role"] == "user" and isinstance(m["content"], list)
        for b in m["content"]
        if b.get("type") == "text"
    ]
    assert any("please hurry" in t for t in texts)
    assert any(t.startswith("Checkpoint: turn 6") and "Front camera now" in t for t in texts)


def test_claude_brain_without_key_is_scripted():
    services, parts = world()
    goal = parts["fleet"].add(_dock_goal())
    brain = ClaudeBrain(services)
    final = run(brain.run_goal(ROBOT, goal))
    assert final.status == GoalStatus.COMPLETED
    assert run(brain.status(ROBOT)).mode == "scripted"


def test_claude_brain_iteration_cap_falls_back():
    client = FakeClient([tool("update_progress", n, note="spinning") for n in range(MAX_ITERS + 2)])
    services, parts = world()
    goal = parts["fleet"].add(_dock_goal())
    final = run(ClaudeBrain(services, client=client).run_goal(ROBOT, goal))
    assert len(client.requests) == MAX_ITERS
    assert final.status == GoalStatus.COMPLETED  # scripted runner docked it


def test_demo_trip_fixture_is_consistent():
    trip = demo_trip()
    assert [s for s in trip.route.order if s not in ("start", "end")] == ["s_tort", "s_hot", "s_avo", "s_cil"]


def test_full_basket_is_dropped_off_at_checkout_mid_goal(monkeypatch):
    from arms.agent.brain_exec import BrainExecutor

    monkeypatch.setattr(BrainExecutor, "basket_capacity", property(lambda self: 3))
    services, parts = world()
    goal = parts["fleet"].add(shop_goal(parts["trips"].trip))
    final = run(ScriptedBrain(services).run_goal(ROBOT, goal))
    assert final.status == GoalStatus.COMPLETED
    assert Counter(i.status for i in final.items)["in_basket"] == 3
    unloads = [c[1] for c in parts["robot"].calls if c[0] == "unload"]
    assert len(unloads) == 2 and sum(unloads) == 5
    assert all(c.check.ok for c in events_of(parts, "basket.verified"))
