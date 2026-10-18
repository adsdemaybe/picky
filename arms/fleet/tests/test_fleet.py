"""Fleet manager tests: store choice + split, assignment, queueing, reassignment, cancel/stop, NL routing."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

import arms.fleet as fleet_arm
from arms.fleet.core import FleetManager
from arms.fleet.fake import FakeFleet
from arms.fleet.tests.stubs import (
    ESCALATED,
    SPECS,
    EventLog,
    StubAgent,
    StubApprovals,
    StubBrain,
    StubCatalog,
    StubRobot,
    StubTrips,
)
from contracts.api import CreateTripRequest
from contracts.common import STAFF_USER_ID
from contracts.fleet import (
    MAX_GOAL_ATTEMPTS,
    FleetRequest,
    GoalItem,
    GoalSpec,
    GoalStatus,
    GoalUpdate,
)
from contracts.services import FleetService, Services
from contracts.shopping import Intent
from contracts.sim import RobotStatus
from contracts.trust import ApprovalKind, ApprovalRequest

TACO = [("tortillas", "TORT", 2), ("ground beef", "BEEF", 2), ("salsa", "SALSA", 1), ("cilantro", "CILANTRO", 1)]
SPLIT = [*TACO, ("queso fresco", "QUESO", 1), ("crema", "CREMA", 1)]


class World:
    def __init__(self, items: list[tuple[str, str, int]] | None = None, auto: bool = True) -> None:
        self.services = Services()
        self.catalog = StubCatalog()
        self.robot = StubRobot(SPECS)
        self.brain = StubBrain(self.services)
        self.trips = StubTrips(self.services, self.catalog, auto=auto)
        self.approvals = StubApprovals()
        self.events = EventLog()
        self.agent = StubAgent(items or TACO)
        s = self.services
        s.catalog, s.robot, s.brain, s.trips = self.catalog, self.robot, self.brain, self.trips  # type: ignore[assignment]
        s.approvals, s.events, s.agent = self.approvals, self.events, self.agent  # type: ignore[assignment]
        self.fleet: FleetManager = FakeFleet(s, robots=SPECS, robot_poll_s=0.01, robot_wait_s=0.3)
        s.fleet = self.fleet  # type: ignore[assignment]


async def until(pred: Callable[[], Any], limit_s: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + limit_s
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.005)


@pytest.fixture
async def world():
    w = World()
    yield w
    await w.fleet.shutdown()
    for t in w.trips.tasks:
        t.cancel()


def test_factories_satisfy_protocol_without_calling_services():
    s = Services()
    assert isinstance(fleet_arm.create_fake(s), FleetService)
    assert isinstance(fleet_arm.create(s), FleetService)


# ---------------------------------------------------------------------------------------------------------------------
# shop requests: store choice, split, pre-assignment, execution
# ---------------------------------------------------------------------------------------------------------------------
async def test_single_store_request_runs_to_completion(world: World):
    plan = await world.fleet.submit_request(FleetRequest(text="tacos for 6, under $40", user_id="maya"))
    assert plan.status == "executing"
    assert plan.interpretation is not None and plan.interpretation.intent == "shop"
    chosen = [c for c in plan.store_choices if c.chosen]
    assert [c.store_id for c in chosen] == ["store_ave"]  # 3/4 vs 3/4 coverage, but only cilantro (online) missing
    assert not plan.split and len(plan.trip_ids) == 1 and plan.budget_split == {"store_ave": 4000}
    goal = plan.goals[0]
    assert goal.kind == "shop" and goal.status == GoalStatus.PENDING and goal.robot_id == "rover-ave-1"
    assert goal.trip_id == plan.trip_ids[0] and goal.items == []
    req = world.trips.requests[0]
    assert req.auto_execute and req.robot_id == "rover-ave-1" and req.request_id == plan.request_id
    assert [i.query for i in req.shopping_list.items] == [q for q, _, _ in TACO]  # untouched list

    await until(lambda: plan.trip_ids[0] in world.trips.results)
    final = world.trips.results[plan.trip_ids[0]]
    assert final.status == GoalStatus.COMPLETED and final.robot_id == "rover-ave-1"
    assert final.items and all(i.status == "in_basket" for i in final.items)
    assert final.attempts == 1 and final.assignment.outcome == "completed"
    assert (await world.fleet.get_request(plan.request_id)).status == "completed"

    types = world.events.types()
    for t in ("request.received", "request.planned", "goal.created", "goal.assigned", "goal.updated", "goal.completed"):
        assert t in types, t
    for e in world.events.events:
        if e.type.startswith("goal."):
            assert e.trip_id == goal.trip_id and e.request_id == plan.request_id and e.user_id == "maya"
        if e.type.startswith("request."):
            assert e.request_id == plan.request_id and e.trip_id is None
    assert world.trips.assigned[0] == (goal.trip_id, "rover-ave-1", goal.goal_id)


async def test_request_preassigns_robot_and_marks_it_unavailable():
    w = World(auto=False)
    plan = await w.fleet.submit_request(FleetRequest(text="tacos for 6", user_id="maya"))
    pico = await w.fleet.get_robot("rover-ave-1")
    assert plan.goals[0].goal_id in pico.queued_goal_ids and not pico.available
    rhea = await w.fleet.get_robot("rover-ave-2")
    assert rhea.available and rhea.queued_goal_ids == []
    # a second request in the same store goes to the free robot
    plan2 = await w.fleet.submit_request(FleetRequest(text="tacos again", user_id="ben"))
    assert plan2.goals[0].robot_id == "rover-ave-2"
    await w.fleet.shutdown()


async def test_split_across_stores_with_budget_split():
    w = World(items=SPLIT)
    plan = await w.fleet.submit_request(FleetRequest(text="tacos with queso and crema, under $40", user_id="maya"))
    assert plan.split
    chosen = [c.store_id for c in plan.store_choices if c.chosen]
    assert chosen == ["store_ave", "store_hill"]
    assert sum(plan.budget_split.values()) == 4000 and set(plan.budget_split) == {"store_ave", "store_hill"}
    by_store = {r.store_id: r for r in w.trips.requests}
    hill = by_store["store_hill"].shopping_list
    assert sorted(i.sku for i in hill.items) == ["CREMA", "QUESO"]
    assert hill.budget_cents == plan.budget_split["store_hill"]
    ave = by_store["store_ave"].shopping_list
    assert "CILANTRO" in [i.sku for i in ave.items]  # nobody has it in stock: stays on the primary (-> online)
    assert {g.store_id: g.robot_id for g in plan.goals} == {"store_ave": "rover-ave-1", "store_hill": "rover-hill-1"}
    hill_choice = next(c for c in plan.store_choices if c.store_id == "store_hill")
    assert "queso fresco" in hill_choice.reason
    for g in plan.goals:  # the store is named once; counts are pluralized
        assert g.title.count(w.fleet._store_name(g.store_id)) == 1, g.title
    assert "(2 items)" in plan.reply and "1 items" not in plan.reply
    await until(lambda: len(w.trips.results) == 2)
    assert all(g.status == GoalStatus.COMPLETED for g in w.trips.results.values())
    await until(lambda: w.fleet._requests[plan.request_id].status == "completed")
    await w.fleet.shutdown()


async def test_store_hint_disables_split():
    w = World(items=SPLIT, auto=False)
    plan = await w.fleet.submit_request(FleetRequest(text="tacos", user_id="maya", store_id="store_ave"))
    assert [c.store_id for c in plan.store_choices if c.chosen] == ["store_ave"]
    assert len(plan.trip_ids) == 1
    plan2 = await w.fleet.submit_request(FleetRequest(text="tacos at capitol hill", user_id="maya"))
    assert [c.store_id for c in plan2.store_choices if c.chosen] == ["store_hill"]
    await w.fleet.shutdown()


# ---------------------------------------------------------------------------------------------------------------------
# assignment, queueing, reassignment
# ---------------------------------------------------------------------------------------------------------------------
async def test_busy_robot_loses_to_idle_robot(world: World):
    world.brain.behavior["rover-ave-2"] = "block"
    g1 = await world.fleet.create_goal(GoalSpec(kind="fetch", store_id="store_ave", items=[GoalItem(query="milk")]))
    assert g1.robot_id == "rover-ave-2"  # both idle: Rhea's dock is closer to the milk
    assert g1.items[0].sku == "MILK" and g1.items[0].slot_id == "AVE-MILK"  # resolved against the catalog
    await until(lambda: world.brain.runs)
    g2 = await world.fleet.create_goal(GoalSpec(kind="fetch", store_id="store_ave", items=[GoalItem(query="milk")]))
    assert g2.robot_id == "rover-ave-1"
    a = g2.assignment
    assert a is not None and a.breakdown["idle"] == 1.0 and len(a.candidates) == 2
    rhea = next(c for c in a.candidates if c.robot_id == "rover-ave-2")
    assert rhea.eligible and rhea.breakdown["idle"] == 0.25 and rhea.score < a.score


async def test_pinned_goals_queue_on_one_robot(world: World):
    world.brain.behavior["rover-ave-1"] = "block"
    g1 = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", robot_id="rover-ave-1"))
    await until(lambda: world.brain.runs)
    g2 = await world.fleet.create_goal(GoalSpec(kind="return_to_dock", robot_id="rover-ave-1"))
    assert g2.robot_id == "rover-ave-1" and g2.assignment.pinned
    info = await world.fleet.get_robot("rover-ave-1")
    assert info.current_goal_id == g1.goal_id and info.queued_goal_ids == [g2.goal_id]
    assert (await world.fleet.get_goal(g2.goal_id)).status == GoalStatus.ASSIGNED
    world.brain.behavior["rover-ave-1"] = "complete"
    await world.fleet.cancel_goal(g1.goal_id, "test")
    await until(lambda: world.fleet._goals[g2.goal_id].status == GoalStatus.COMPLETED)
    assert [r for r, _ in world.brain.runs] == ["rover-ave-1", "rover-ave-1"]


async def test_reassign_on_robot_failure(world: World):
    world.brain.behavior["rover-ave-1"] = "raise"
    plan = await world.fleet.submit_request(FleetRequest(text="tacos for 6", user_id="maya"))
    tid = plan.trip_ids[0]
    await until(lambda: tid in world.trips.results)
    goal = world.trips.results[tid]
    assert goal.status == GoalStatus.COMPLETED and goal.robot_id == "rover-ave-2"
    assert [a.robot_id for a in goal.assignments] == ["rover-ave-1", "rover-ave-2"]
    assert [a.outcome for a in goal.assignments] == ["reassigned", "completed"]
    assert goal.assignments[0].note.startswith("battery_low")
    assigned = [e for e in world.events.of("goal.assigned") if e.goal.goal_id == goal.goal_id]
    assert [e.assignment.attempt for e in assigned] == [1, 2]
    assert "rover-ave-1" in world.robot.stopped
    assert world.trips.assigned[-1] == (tid, "rover-ave-2", goal.goal_id)
    assert goal.items and all(i.status == "in_basket" for i in goal.items)


async def test_failure_without_spare_robot_fails_goal(world: World):
    world.brain.behavior["rover-hill-1"] = "raise"
    g = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", store_id="store_hill", categories=["dairy"]))
    await until(lambda: world.fleet._goals[g.goal_id].terminal)
    final = await world.fleet.get_goal(g.goal_id)
    assert final.status == GoalStatus.FAILED and final.failure_code == "battery_low"
    assert world.events.of("goal.failed")[-1].goal.goal_id == g.goal_id
    assert MAX_GOAL_ATTEMPTS == 2


async def test_max_attempts_stops_reassigning(world: World):
    world.brain.behavior["rover-ave-1"] = "raise"
    world.brain.behavior["rover-ave-2"] = "raise"
    g = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", store_id="store_ave", aisle_ids=["a1"]))
    await until(lambda: world.fleet._goals[g.goal_id].terminal)
    final = await world.fleet.get_goal(g.goal_id)
    assert final.status == GoalStatus.FAILED and final.attempts == MAX_GOAL_ATTEMPTS
    assert final.assignments[-1].outcome == "failed"


async def test_escalated_goal_moves_to_the_free_robot_with_the_step_code(world: World):
    """The agent hands a goal back with RobotError('unknown_robot', '<step code>: <why>'): the fleet records the
    step's code, stops the failed robot and runs the goal on the other robot in the store."""
    world.brain.behavior["rover-ave-1"] = "escalate"
    plan = await world.fleet.submit_request(FleetRequest(text="tacos for 6", user_id="maya"))
    tid = plan.trip_ids[0]
    await until(lambda: tid in world.trips.results)
    goal = world.trips.results[tid]
    assert goal.status == GoalStatus.COMPLETED and goal.robot_id == "rover-ave-2"
    assert [a.robot_id for a in goal.assignments] == ["rover-ave-1", "rover-ave-2"]
    assert goal.assignments[0].note == "unsupported_step: " + ESCALATED.message.split(": ", 1)[1]
    assert "rover-ave-1" in world.robot.stopped
    pico = await world.fleet.get_robot("rover-ave-1")
    assert pico.current_goal_id is None and pico.queued_goal_ids == [] and pico.available


async def test_escalation_with_the_only_other_robot_stopped_fails_readably(world: World):
    """Live trip B (goal_xrytx8eq, 12:03 PT): Pico stopped from the web app, Rhea escalates the put-back. The goal
    must fail at once with a reason the shopper can read (not 'unknown_robot'), and Rhea must be free again."""
    await world.fleet.stop_robot("rover-ave-1")
    world.brain.behavior["rover-ave-2"] = "escalate"
    plan = await world.fleet.submit_request(FleetRequest(text="tacos for 6", user_id="maya"))
    tid = plan.trip_ids[0]
    await until(lambda: tid in world.trips.results)
    goal = world.trips.results[tid]
    assert goal.status == GoalStatus.FAILED and goal.failure_code == "unsupported_step"
    assert "unknown_robot" not in (goal.error or "")
    assert goal.error == (
        "Rhea could not finish: no place-back primitive on this backend (T0 over the 0.2 sim wire); "
        "no other robot at Picky Market - The Ave can take over (Pico is stopped)"
    )
    assert world.events.of("goal.failed")[-1].reason == goal.error
    rhea = await world.fleet.get_robot("rover-ave-2")
    assert rhea.current_goal_id is None and rhea.queued_goal_ids == [] and rhea.available


async def test_stopping_the_queued_robot_never_hands_the_goal_back_to_the_failed_one(world: World):
    """Live trip A (goal_bu7gy5gm, 11:57 PT): Rhea escalated, the goal queued behind busy Pico, then Pico was stopped
    from the web app. stop_robot used to move the goal straight back to Rhea (the robot that had just failed it, still
    holding the salsa) and its driver then waited for Pico's resume forever: 'assigned' for good. Now the goal ends
    failed with the reason and neither robot keeps it."""
    world.brain.behavior["rover-ave-1"] = "block"
    other = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", robot_id="rover-ave-1"))
    await until(lambda: world.brain.runs)
    world.brain.behavior["rover-ave-2"] = "escalate"
    plan = await world.fleet.submit_request(FleetRequest(text="tacos for 6", user_id="maya"))
    gid, tid = plan.goals[0].goal_id, plan.trip_ids[0]
    assert plan.goals[0].robot_id == "rover-ave-2"  # Pico is busy
    await until(lambda: world.fleet._goals[gid].robot_id == "rover-ave-1")  # Rhea escalated: queued behind Pico
    assert world.fleet._goals[gid].status == GoalStatus.ASSIGNED
    assert (await world.fleet.get_robot("rover-ave-1")).queued_goal_ids == [gid]

    await world.fleet.stop_robot("rover-ave-1", "stopped from the web app")
    assert (await world.fleet.get_goal(other.goal_id)).status == GoalStatus.CANCELLED
    await until(lambda: tid in world.trips.results)
    goal = world.trips.results[tid]
    assert goal.status == GoalStatus.FAILED and goal.failure_code == "unsupported_step"
    assert [a.robot_id for a in goal.assignments] == ["rover-ave-2", "rover-ave-1"]  # never back to Rhea
    assert goal.error == (
        "Rhea could not finish: no place-back primitive on this backend (T0 over the 0.2 sim wire); "
        "no other robot at Picky Market - The Ave can take over (Pico is stopped)"
    )
    assert [r for r, g in world.brain.runs if g == gid] == ["rover-ave-2"]
    for rid in ("rover-ave-1", "rover-ave-2"):
        info = await world.fleet.get_robot(rid)
        assert info.current_goal_id is None and info.queued_goal_ids == [], rid
    assert (await world.fleet.get_robot("rover-ave-2")).available


async def test_execute_trip_without_robot_fails_no_robot(world: World):
    world.robot.set("rover-hill-1", status=RobotStatus.DISCONNECTED)
    w_list = await StubAgent([("milk", "MILK", 1)]).expand_intent(Intent(intent_id="i", user_id="maya", text="milk"))
    trip = await world.trips.create_trip(
        CreateTripRequest(user_id="maya", text="tacos", store_id="store_hill", shopping_list=w_list)
    )
    goal = await world.fleet.execute_trip(trip.trip_id)
    assert goal.status == GoalStatus.FAILED and goal.failure_code == "no_robot"
    assert goal.trip_id == trip.trip_id and goal.items and world.brain.runs == []


# ---------------------------------------------------------------------------------------------------------------------
# cancel / stop / messages
# ---------------------------------------------------------------------------------------------------------------------
async def test_cancel_running_goal_withdraws_questions(world: World):
    world.brain.behavior["rover-ave-1"] = "block"
    g = await world.fleet.create_goal(
        GoalSpec(kind="fetch", robot_id="rover-ave-1", items=[GoalItem(query="milk")], user_id="staff-1")
    )
    await until(lambda: world.brain.runs)
    world.approvals.items.append(
        ApprovalRequest(
            approval_id="ap1", kind=ApprovalKind.QUESTION, title="Which milk?", goal_id=g.goal_id, user_id="staff-1"
        )
    )
    cancelled = await world.fleet.cancel_goal(g.goal_id, "changed my mind")
    assert cancelled.status == GoalStatus.CANCELLED and cancelled.error == "changed my mind"
    await until(lambda: world.brain.cancelled == [g.goal_id])
    assert world.approvals.cancelled == ["ap1"]
    assert "rover-ave-1" in world.robot.stopped
    ev = world.events.of("goal.failed")[-1]
    assert ev.goal.status == GoalStatus.CANCELLED and ev.reason == "changed my mind"
    await until(lambda: world.fleet._running.get("rover-ave-1") is None)
    assert (await world.fleet.get_robot("rover-ave-1")).available
    # cancelling again is a no-op
    assert (await world.fleet.cancel_goal(g.goal_id)).status == GoalStatus.CANCELLED


async def test_cancel_pending_shop_goal_then_execute_returns_cancelled():
    w = World(auto=False)
    plan = await w.fleet.submit_request(FleetRequest(text="tacos", user_id="maya"))
    gid = plan.goals[0].goal_id
    await w.fleet.cancel_goal(gid, "nevermind")
    goal = await w.fleet.execute_trip(plan.trip_ids[0])
    assert goal.goal_id == gid and goal.status == GoalStatus.CANCELLED
    assert w.brain.runs == []
    assert (await w.fleet.get_request(plan.request_id)).status == "failed"
    await w.fleet.shutdown()


async def test_stop_robot_cancels_active_goal(world: World):
    world.brain.behavior["rover-ave-2"] = "block"
    g = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", robot_id="rover-ave-2"))
    await until(lambda: world.brain.runs)
    info = await world.fleet.stop_robot("rover-ave-2", "emergency")
    assert info.robot_id == "rover-ave-2" and info.current_goal_id is None
    assert (await world.fleet.get_goal(g.goal_id)).status == GoalStatus.CANCELLED
    reply = await world.fleet.message_robot("rover-ave-2", "status?")
    assert reply.text == "ack: status?"
    with pytest.raises(KeyError):
        await world.fleet.get_robot("rover-nope")


# ---------------------------------------------------------------------------------------------------------------------
# NL routing for staff / status / clarifications
# ---------------------------------------------------------------------------------------------------------------------
async def test_shelf_scan_request_staff_and_shopper(world: World):
    world.brain.behavior["rover-hill-1"] = "block"
    plan = await world.fleet.submit_request(
        FleetRequest(text="scan the dairy aisle at Hill", user_id="sam", role="staff")
    )
    assert plan.status == "executing" and plan.interpretation.intent == "shelf_scan"
    goal = plan.goals[0]
    assert goal.kind == "shelf_scan" and goal.store_id == "store_hill" and goal.robot_id == "rover-hill-1"
    assert sorted(goal.slot_ids) == ["HILL-CREMA", "HILL-MILK", "HILL-QUESO"]
    assert goal.trip_id is None and goal.end_at == "stay"
    rejected = await world.fleet.submit_request(FleetRequest(text="scan the dairy aisle", user_id="maya"))
    assert rejected.status == "rejected" and rejected.goals == []


async def test_dock_and_fetch_requests(world: World):
    world.brain.behavior["rover-hill-1"] = "block"
    world.brain.behavior["rover-ave-2"] = "block"
    dock = await world.fleet.submit_request(FleetRequest(text="Hazel, go charge", user_id="sam", role="staff"))
    g = dock.goals[0]
    assert g.kind == "return_to_dock" and g.robot_id == "rover-hill-1" and g.end_at == "dock" and g.priority == "high"
    fetch = await world.fleet.submit_request(
        FleetRequest(text="Rhea, bring 2 whole milk to checkout", user_id="sam", role="staff")
    )
    fg = fetch.goals[0]
    assert fg.kind == "fetch" and fg.robot_id == "rover-ave-2" and fg.items[0].qty == 2
    assert fg.items[0].sku == "MILK" and fg.user_id == "sam"
    which = await world.fleet.submit_request(FleetRequest(text="go charge", user_id="sam", role="staff"))
    assert which.status == "needs_input" and "Hazel" in which.reply


async def test_needs_input_then_reply_updates_same_plan():
    w = World(auto=False)
    plan = await w.fleet.submit_request(FleetRequest(text="get me some stuff", user_id="maya"))
    assert plan.status == "needs_input" and plan.reply.startswith("What should I get")
    again = await w.fleet.submit_request(
        FleetRequest(text="tacos for 6 under $40", user_id="maya", in_reply_to=plan.request_id)
    )
    assert again.request_id == plan.request_id and again.replies == ["tacos for 6 under $40"]
    assert again.status == "executing" and len(again.goals) == 1
    assert len(await w.fleet.list_requests("maya")) == 1
    await w.fleet.shutdown()


async def test_status_and_chat_reply_only(world: World):
    s = await world.fleet.submit_request(FleetRequest(text="where is my order?", user_id="maya"))
    assert s.status == "completed" and s.goals == [] and s.reply
    c = await world.fleet.submit_request(FleetRequest(text="hello", user_id="maya"))
    assert c.status == "completed" and c.interpretation.intent == "chat"
    reqs = await world.fleet.list_requests("maya")
    assert [r.request_id for r in reqs] == [c.request_id, s.request_id]
    with pytest.raises(KeyError):
        await world.fleet.get_request("req_nope")


async def test_product_question_is_answered_by_the_agent(world: World):
    """'where is the salsa?' is not about the asker's goals: the agent's catalog answer wins over the goal status."""
    from contracts.fleet import RequestDraft

    async def interpret_request(text, user_id, store_id=None, **kw):  # noqa: ANN001, ANN003, ANN202
        return RequestDraft(intent="status", summary="Where is salsa", reply="Salsa is in Aisle 3 at store_ave.")

    world.agent.interpret_request = interpret_request  # type: ignore[attr-defined]
    q = await world.fleet.submit_request(FleetRequest(text="where is the salsa?", user_id="maya"))
    assert q.status == "completed" and q.reply == "Salsa is in Aisle 3 at store_ave."
    mine = await world.fleet.submit_request(FleetRequest(text="where is my order?", user_id="maya"))
    assert mine.status == "completed" and "Aisle 3" not in (mine.reply or "")


# ---------------------------------------------------------------------------------------------------------------------
# update_goal, list_goals, monitor
# ---------------------------------------------------------------------------------------------------------------------
async def test_update_goal_rules(world: World):
    world.brain.behavior["rover-ave-1"] = "block"
    g = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", robot_id="rover-ave-1", aisle_ids=["a1"]))
    await until(lambda: world.brain.runs)
    assert g.user_id == STAFF_USER_ID and g.slot_ids
    upd = await world.fleet.update_goal(g.goal_id, GoalUpdate(scanned_slot_ids=[g.slot_ids[0]], note="scanning"))
    assert upd.progress == pytest.approx(1 / len(g.slot_ids), abs=1e-3) and upd.progress_note == "scanning"
    same = await world.fleet.update_goal(g.goal_id, GoalUpdate(status=GoalStatus.ACTIVE))
    assert same.status == GoalStatus.ACTIVE
    waiting = await world.fleet.update_goal(g.goal_id, GoalUpdate(status=GoalStatus.WAITING))
    assert waiting.status == GoalStatus.WAITING
    added = await world.fleet.update_goal(g.goal_id, GoalUpdate(add_items=[GoalItem(item_id="x1", query="limes")]))
    assert added.item("x1") is not None
    await world.fleet.cancel_goal(g.goal_id)
    with pytest.raises(ValueError):
        await world.fleet.update_goal(g.goal_id, GoalUpdate(status=GoalStatus.ACTIVE))
    with pytest.raises(KeyError):
        await world.fleet.update_goal("goal_nope", GoalUpdate())


async def test_list_goals_filters(world: World):
    world.brain.behavior["rover-ave-1"] = "block"
    a = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", robot_id="rover-ave-1"))
    b = await world.fleet.create_goal(GoalSpec(kind="return_to_dock", robot_id="rover-hill-1"))
    await until(lambda: world.fleet._goals[b.goal_id].terminal)
    assert [g.goal_id for g in await world.fleet.list_goals()] == [b.goal_id, a.goal_id]
    assert [g.goal_id for g in await world.fleet.list_goals(store_id="store_hill")] == [b.goal_id]
    assert [g.goal_id for g in await world.fleet.list_goals(active=True)] == [a.goal_id]
    assert [g.goal_id for g in await world.fleet.list_goals(status=[GoalStatus.COMPLETED])] == [b.goal_id]
    assert [g.goal_id for g in await world.fleet.list_goals(kind="shelf_scan", robot_id="rover-ave-1")] == [a.goal_id]
    assert await world.fleet.list_goals(limit=1) and len(await world.fleet.list_goals(limit=1)) == 1


async def test_monitor_publishes_robot_updates_and_docks_low_battery(world: World):
    await world.fleet.start()
    await world.fleet.start()  # idempotent
    st = world.robot.states_["rover-ave-2"].model_copy(update={"battery_pct": 12.0})
    world.robot.states_["rover-ave-2"] = st.model_copy(update={"pose": st.pose.model_copy(update={"x": 6.0})})
    await world.robot.queue.put(world.robot.states_["rover-ave-2"])
    await until(lambda: any(g.kind == "return_to_dock" for g in world.fleet._goals.values()))
    dock = next(g for g in world.fleet._goals.values() if g.kind == "return_to_dock")
    assert dock.robot_id == "rover-ave-2"
    upd = world.events.of("fleet.robot_updated")
    assert upd and upd[0].robot.robot_id == "rover-ave-2" and upd[0].store_id == "store_ave"
    robots = await world.fleet.list_robots("store_ave")
    assert [r.robot_id for r in robots] == ["rover-ave-1", "rover-ave-2"]
    assert robots[0].cameras == ["rover-ave-1.front", "rover-ave-1.wrist", "rover-ave-1.basket"]


async def test_stop_holds_the_robot_queue_until_a_manual_goal_or_resume(world: World):
    world.brain.behavior["rover-hill-1"] = "block"
    first = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", robot_id="rover-hill-1"))
    await until(lambda: world.brain.runs)
    queued = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", store_id="store_hill"))
    info = await world.fleet.get_robot("rover-hill-1")
    assert info.current_goal_id == first.goal_id and info.queued_goal_ids == [queued.goal_id]

    info = await world.fleet.stop_robot("rover-hill-1", "spill in aisle 2")
    assert (await world.fleet.get_goal(first.goal_id)).status == GoalStatus.CANCELLED
    assert not info.available and "spill in aisle 2" in (info.error or "")
    await asyncio.sleep(0.4)  # > robot_wait_s: Hazel is store_hill's only robot; the queued goal must NOT start or fail
    assert (await world.fleet.get_goal(queued.goal_id)).status in (GoalStatus.PENDING, GoalStatus.ASSIGNED)
    assert [r for r in world.brain.runs if r[1] == queued.goal_id] == []
    assert (await world.fleet.get_robot("rover-hill-1")).queued_goal_ids == [queued.goal_id]

    world.brain.behavior["rover-hill-1"] = "complete"
    info = await world.fleet.resume_robot("rover-hill-1")
    assert info.error is None
    await until(lambda: any(r[1] == queued.goal_id for r in world.brain.runs))
    await until(lambda: world.fleet._goals[queued.goal_id].status == GoalStatus.COMPLETED)


async def test_stop_moves_queued_goals_to_another_robot_and_a_manual_goal_resumes(world: World):
    world.brain.behavior["rover-ave-1"] = "block"
    world.brain.behavior["rover-ave-2"] = "block"
    first = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", robot_id="rover-ave-1"))
    busy = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", robot_id="rover-ave-2"))
    await until(lambda: len(world.brain.runs) == 2)
    queued = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", store_id="store_ave"))
    assert queued.robot_id in ("rover-ave-1", "rover-ave-2")
    stopped = queued.robot_id
    other = "rover-ave-2" if stopped == "rover-ave-1" else "rover-ave-1"
    await world.fleet.stop_robot(stopped, "maintenance")
    moved = await world.fleet.get_goal(queued.goal_id)
    assert moved.robot_id == other and moved.status in (GoalStatus.PENDING, GoalStatus.ASSIGNED)
    assert (await world.fleet.get_robot(stopped)).queued_goal_ids == []
    # a new automatic goal never lands on the stopped robot
    auto = await world.fleet.create_goal(GoalSpec(kind="shelf_scan", store_id="store_ave"))
    assert auto.robot_id == other
    # a manual goal for the stopped robot resumes it
    world.brain.behavior[stopped] = "complete"
    manual = await world.fleet.create_goal(GoalSpec(kind="return_to_dock", robot_id=stopped))
    assert (await world.fleet.get_robot(stopped)).error is None
    await until(lambda: world.fleet._goals[manual.goal_id].status == GoalStatus.COMPLETED)
    for g in (first, busy):
        await world.fleet.cancel_goal(g.goal_id)


def test_shop_goal_titles_name_the_store_once_and_pluralize():
    from arms.fleet.core import shop_goal_title

    ave = "Picky Market - The Ave"
    assert shop_goal_title("Tacos for 6", 10, ave) == f"Shop Tacos for 6 (10 items) at {ave}"
    assert shop_goal_title(f"Shopping ({ave})", 3, ave, "store_ave") == f"Shop 3 items at {ave}"
    assert shop_goal_title("Tacos (store_ave)", 1, ave, "store_ave") == f"Shop Tacos (1 item) at {ave}"
    assert shop_goal_title(None, 1, ave) == f"Shop 1 item at {ave}"


def test_shop_goal_title_says_the_count_once():
    """Live Isaac trip goal_nznwackk: 'Shop Your order (3 items) (3 items) at ...' (the order title has a count)."""
    from arms.fleet.core import shop_goal_title

    ave = "Picky Market - The Ave"
    assert shop_goal_title("Your order (3 items)", 3, ave) == f"Shop Your order (3 items) at {ave}"
    assert shop_goal_title("Your order (1 item)", 1, ave) == f"Shop Your order (1 item) at {ave}"
    # a split sub-list keeps the parent's title: the count is the sub-list's own
    assert (
        shop_goal_title("Your order (3 items) (store_ave)", 2, ave, "store_ave")
        == f"Shop Your order (2 items) at {ave}"
    )
