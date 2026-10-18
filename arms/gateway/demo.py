"""Headless P0 demo through the real gateway pipeline: NL request -> fleet -> robot brains -> checkout -> follow-up ->
a smarter second request, with a store-staff shelf scan running concurrently on another robot.

``python -m arms.gateway.demo`` (= ``make demo``) uses whatever services ``PICKY_REAL_ARMS`` selects (default: all
fakes) and prints a compact timeline (requests, goals, brain steps, trip flow, stock alerts). Scenario:

(a) maya texts "tacos for 6, under $40, one vegetarian" -> ``fleet.submit_request`` -> store choice (an item only
    store_hill / online carries may split the list) -> one auto-executing trip + shop goal per store -> the robots'
    brains run them (brain.step) -> checkout -> follow-up "too mild" -> profile update.
(b) staff: "check stock in the snacks aisle at store_ave" -> a shelf_scan goal on the other store_ave rover (Rhea)
    while Pico shops -> scans update stock -> stock alerts.
(b2) staff at the second store: "Hazel, scan the dairy aisle at the Hill store" -> a shelf_scan goal on rover-hill-1
    (Hazel) at store_hill, at the same time: three robots in two stores work in parallel.
(c) maya sends the same request again: trip 2 must pick a different salsa and different recommendations.

Exits non-zero unless every goal completed, every maya trip passed every pipeline state to ``done`` with no failed
stops and within budget (unless an over-budget ask was approved), stock alerts exist for store_ave, trip 2 changed,
and (without ``--allow-errors``) no ``error`` event was published. With fakes it finishes in well under 30 s.

Approvals are answered by the scripted shopper in the messaging fake (Photon path); a web-shopper fallback answers
anything still pending after ``WEB_FALLBACK_S`` (approve / first option / echoes the checkout cart_hash; says no to
asks flagged over budget), so it also runs with real messaging and no phone.

Options: ``--quiet``, ``--text``, ``--user``, ``--staff-text``, ``--no-staff``, ``--timeout 30``, ``--allow-errors``.
``--no-staff`` also skips the store_hill scan.
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from arms.gateway.orchestrator import OrchestratorConfig
from arms.gateway.registry import Registry, build_registry
from contracts.common import STAFF_USER_ID, money
from contracts.fleet import FleetRequest, Goal, GoalStatus, RequestPlan
from contracts.shopping import TripState
from contracts.trip import Trip
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse
from utils.config import get_settings
from utils.ids import seed_ids

DEMO_TEXT = "tacos for 6, under $40, one vegetarian"
DEMO_USER = "maya"
STAFF_TEXT = "check stock in the snacks aisle at store_ave"
STAFF_STORE = "store_ave"
STAFF_ROBOT = "rover-ave-2"
HILL_TEXT = "Hazel, scan the dairy aisle at the Hill store"
HILL_STORE = "store_hill"
HILL_ROBOT = "rover-hill-1"
WEB_FALLBACK_S = 1.5
DEFAULT_TIMEOUT_S = 30.0

FAKE_OPTS: dict[str, dict[str, Any]] = {
    "robot": {"realtime_factor": 120.0},
    "messaging": {"reply_delay_s": 0.05, "reply_mode": "auto"},  # the scripted shopper (the dev server: off)
}

REAL_OPTS: dict[str, dict[str, Any]] = {
    # the real robot arm drives its built-in 2D fake when no Isaac worker hosts a store: same demo speed
    "robot": {"realtime_factor": 120.0},
}

PIPELINE_STATES = [
    TripState.INTENT,
    TripState.LIST_READY,
    TripState.RECS_READY,
    TripState.ROUTE_PLANNED,
    TripState.EXECUTING,
    TripState.AWAITING_APPROVAL,
    TripState.CHECKOUT,
    TripState.PAID,
    TripState.FOLLOWUP,
    TripState.DONE,
]
"""Request trips are auto_execute: no AWAITING_LOGIN."""

TERMINAL_REQUEST = ("completed", "failed", "rejected", "needs_input")


class DemoFailure(AssertionError):
    pass


@dataclass
class DemoResult:
    plan1: RequestPlan
    plan2: RequestPlan
    trips1: list[Trip]
    trips2: list[Trip]
    staff_plan: RequestPlan | None = None
    staff_goals: list[Goal] = field(default_factory=list)
    hill_goals: list[Goal] = field(default_factory=list)
    goals: list[Goal] = field(default_factory=list)
    stock_alerts: int = 0
    brain_steps: int = 0
    elapsed_s: float = 0.0

    @property
    def trip1(self) -> Trip:
        return _primary(self.trips1)

    @property
    def trip2(self) -> Trip:
        return _primary(self.trips2)


# ----------------------------------------------------------------------------------------------------------------------
# timeline printing
# ----------------------------------------------------------------------------------------------------------------------
def describe(ev: Any) -> str | None:
    """One compact line per event (None = don't print)."""
    t = ev.type
    if t in ("robot.state", "fleet.robot_updated", "stock.updated"):
        return None
    if t == "request.received":
        return f'"{ev.request.text}" from {ev.request.user_id} ({ev.request.role})'
    if t == "request.planned":
        p = ev.plan
        chosen = ", ".join(f"{c.store_id} ({c.reason})" for c in p.store_choices if c.chosen) or "-"
        return f"{p.status}: {p.interpretation.intent if p.interpretation else '?'} -> {chosen} | {p.reply}"
    if t in ("goal.created", "goal.updated", "goal.completed", "goal.failed"):
        g = ev.goal
        extra = f" {g.progress:.0%}" + (f" {g.progress_note}" if g.progress_note else "")
        if t == "goal.failed":
            extra += f" ({g.failure_code or ev.reason})"
        if t == "goal.created":
            extra = f" {g.title}"
        return f"{g.kind} {g.goal_id} [{g.status.value}]{extra}"
    if t == "goal.assigned":
        a = ev.assignment
        return f"{ev.goal.goal_id} -> {a.robot_id} (attempt {a.attempt}, score {a.score:.2f}): {a.reason}"
    if t == "brain.step":
        s = ev.step
        tool = f" {s.tool}" if s.tool else ""
        ok = "" if s.ok else " FAILED"
        return f"#{s.step_no} {s.kind}{tool}{ok}: {s.text[:110]}"
    if t == "stock.alert":
        a = ev.alert
        return f"{'ALERT' if a.active else 'cleared'} {a.status} {a.sku} {a.name or ''} (qty {a.qty})"
    if t == "trip.created":
        return f"store={ev.trip.store_id} user={ev.trip.user_id} robot={ev.trip.robot_id}"
    if t == "trip.state_changed":
        return f"{ev.from_state.value if ev.from_state else '-'} -> {ev.to_state.value}  ({ev.reason})"
    if t == "intent.received":
        return f'"{ev.intent.text}" via {ev.intent.channel}'
    if t == "list.ready":
        sl = ev.shopping_list
        budget = money(sl.budget_cents) if sl.budget_cents is not None else "-"
        return f"{len(sl.items)} items, budget {budget}: " + ", ".join(f"{i.qty}x {i.query}" for i in sl.items)
    if t == "sourcing.split":
        online = ", ".join(i.query for i in ev.online) or "-"
        return f"in-store {len(ev.in_store)}, online {len(ev.online)} ({online}), unresolved {len(ev.unresolved)}"
    if t == "recs.ready":
        recs = "; ".join(
            f"{r.product_name or r.sku} [{r.kind}, {r.interest:.2f}"
            + (f", +{r.extra_distance_m:.0f} m" if r.extra_distance_m is not None else "")
            + f"] {r.reason}"
            for r in ev.recs
        )
        return f"{len(ev.recs)} detours: {recs or '-'} | checkout batch: {len(ev.checkout_recs)}"
    if t == "route.planned":
        r = ev.route
        base = f" (list-only {r.baseline_distance_m:.1f} m)" if r.baseline_distance_m is not None else ""
        return (
            f"{'RE-PLAN ' if ev.replan else ''}{len(r.order)} stops, {r.total_distance_m:.1f} m{base}, "
            f"detours {len(r.visited_optional)} visited / {len(r.skipped_optional)} skipped, solver={r.solver}"
        )
    if t == "stop.updated":
        return f"{ev.stop_id}: {ev.status}" + (f" ({ev.reason})" if ev.reason else "")
    if t == "robot.arrived":
        return f"{ev.stop_id or ev.slot_id} at ({ev.pose.x:.1f}, {ev.pose.y:.1f})"
    if t == "scan.result":
        rep = ev.report
        return f"{rep.slot_id}: {rep.expected_sku} x{rep.observed_count}" + (" MISMATCH" if rep.mismatch else "")
    if t == "pick.choices":
        c = ev.choice
        return f"{c.sku}: " + ", ".join(f"#{x.rank} {x.instance_id} ({x.score:.2f})" for x in c.candidates)
    if t == "item.picked":
        return f"{ev.sku} ({ev.instance_id})"
    if t == "basket.verified":
        return "ok" if ev.check.ok else f"MISMATCH missing={ev.check.missing} extra={ev.check.unexpected}"
    if t == "cart.updated":
        rem = f", remaining {money(ev.remaining_cents)}" if ev.remaining_cents is not None else ""
        return f"{len(ev.cart.active_lines())} lines, total {money(ev.total_cents)}{rem}"
    if t == "approval.requested":
        r = ev.request
        who = f"{r.robot_id} asks " if r.robot_id else ""
        return f"{who}[{r.kind.value}] {r.title}" + (f" ({len(r.options)} options)" if r.options else "")
    if t == "approval.resolved":
        r = ev.response
        return f"[{ev.kind.value if ev.kind else '?'}] {r.decision} via {r.channel}" + (
            f" -> {','.join(r.chosen())}" if r.chosen() else ""
        )
    if t == "mandate.decision":
        d = ev.decision
        if d.scope == "line" and d.verdict == "approve":
            return None  # too chatty: one per pick
        reasons = ",".join(r.value for r in d.reasons) or "ok"
        return f"{d.scope} {d.verdict.value} ({reasons})" + (
            f" total {money(d.total_cents)}" if d.total_cents is not None else ""
        )
    if t == "checkout.completed":
        r = ev.receipt
        return f"{r.status} {money(r.amount_cents)} via {r.provider}" + (
            f", online order {r.online_order_id}" if r.online_order_id else ""
        )
    if t == "followup.sent":
        return f'"{ev.message.text}" about {ev.message.about_skus}'
    if t == "feedback.received":
        f = ev.feedback
        return f'{f.sku} rating={f.rating} tags={f.tags} "{f.text}"'
    if t == "profile.updated":
        u = ev.update
        return f"v{u.version_before}->v{u.version_after}: {u.summary or len(u.changes)}"
    if t == "agent.message":
        return f"{ev.role}/{ev.channel or '-'}: {ev.text[:90]}"
    if t == "error":
        return f"{ev.arm}: {ev.message}" + (f" [{ev.code}]" if ev.code else "")
    return ""


def _label(ev: Any, names: dict[str, str]) -> str:
    if ev.trip_id and ev.trip_id in names:
        return names[ev.trip_id]
    if ev.robot_id:
        return names.get(ev.robot_id, ev.robot_id)
    if ev.request_id and ev.request_id in names:
        return names[ev.request_id]
    return ev.store_id or ev.trip_id or "-"


async def _printer(sub: Any, t0: float, quiet: bool, names: dict[str, str], stats: dict[str, Any]) -> None:
    async with sub:
        async for ev in sub:
            stats["types"][ev.type] = stats["types"].get(ev.type, 0) + 1
            if ev.type == "error":
                stats["errors"].append(f"{_label(ev, names)}: {ev.arm}: {ev.message}")
            if quiet and ev.type != "error":
                continue
            line = describe(ev)
            if line is None:
                continue
            line = " ".join(line.split())
            print(
                f"{time.monotonic() - t0:6.2f}s #{ev.seq:<5} {_label(ev, names):<12} {ev.type:<19} {line}", flush=True
            )


async def _web_fallback(reg: Registry, sub: Any) -> None:
    """Answer approvals nobody answered within WEB_FALLBACK_S, like a person tapping the web UI."""
    async with sub:
        async for ev in sub:
            asyncio.get_running_loop().create_task(_answer_later(reg, ev.request))


async def _answer_later(reg: Registry, req: ApprovalRequest) -> None:
    await asyncio.sleep(WEB_FALLBACK_S)
    if reg.approvals.get(req.approval_id) is None:
        return
    if req.options:
        resp = ApprovalResponse(
            approval_id=req.approval_id,
            decision="choice",
            choice=req.options[0].option_id,
            choices=[req.options[0].option_id] if req.multi else [],
            channel="web",
        )
    else:
        resp = ApprovalResponse(
            approval_id=req.approval_id,
            decision="reject" if _is_over_budget_ask(req) else "approve",  # same budget rule as the scripted shopper
            channel="web",
            cart_hash=req.cart_hash if req.kind == ApprovalKind.CHECKOUT else None,
        )
    await reg.approvals.resolve(resp)


# ----------------------------------------------------------------------------------------------------------------------
# scenario
# ----------------------------------------------------------------------------------------------------------------------
def _is_over_budget_ask(req: ApprovalRequest) -> bool:
    """Approval the gateway flagged as busting the trip budget (detour heads-up or over_budget mandate reason)."""
    return bool(req.payload.get("over_budget")) or "over_budget" in req.payload.get("reasons", [])


def _overage_approved(trip: Trip) -> bool:
    approved = {r.approval_id for r in trip.approval_responses if r.approved}
    return any(_is_over_budget_ask(a) and a.approval_id in approved for a in trip.approvals)


def _primary(trips: list[Trip]) -> Trip:
    """The trip holding the salsa (else the biggest list)."""
    with_salsa = [t for t in trips if _salsa(t)]
    pool = with_salsa or trips
    return max(pool, key=lambda t: len(t.shopping_list.items) if t.shopping_list else 0)


def _salsa(trip: Trip) -> str | None:
    items = trip.shopping_list.items if trip.shopping_list else []
    return next((i.sku for i in items if "salsa" in i.query.lower()), None)


def _request_salsa(trips: list[Trip]) -> str | None:
    return next((s for s in (_salsa(t) for t in trips) if s), None)


def _rec_key(trips: list[Trip]) -> list[str]:
    return sorted(r.sku for t in trips for r in t.recs)


def check_trip(label: str, trip: Trip) -> list[str]:
    """Pipeline-level assertions for one finished request trip (empty list = OK)."""
    problems: list[str] = []
    seen = {h.to_state for h in trip.history}
    missing = [s.value for s in PIPELINE_STATES if s not in seen]
    if missing:
        problems.append(f"{label} skipped pipeline states {missing}")
    if TripState.AWAITING_LOGIN in seen:
        problems.append(f"{label} waited for a login although the fleet created it (auto_execute)")
    if trip.robot_id is None or trip.goal_id is None:
        problems.append(f"{label} has no robot / goal assigned")
    failed = sorted(k for k, v in trip.stop_status.items() if v == "failed")
    if failed:
        problems.append(f"{label} has failed stops {failed}")
    if trip.receipt is None or trip.receipt.status != "succeeded":
        problems.append(f"{label} was not paid")
    if trip.followup is None:
        problems.append(f"{label} sent no follow-up")
    budget = trip.budget_cents
    if trip.receipt is not None and budget is not None and trip.receipt.amount_cents > budget:
        if not _overage_approved(trip):
            problems.append(
                f"{label} paid {money(trip.receipt.amount_cents)}, over the {money(budget)} budget, "
                "without the shopper approving an over-budget item"
            )
    return problems


async def _wait_plan(reg: Registry, plan: RequestPlan, deadline: float) -> list[Trip]:
    """Every trip of a shop request reaches done (any trip failing / timing out -> DemoFailure)."""
    if plan.status in ("failed", "rejected", "needs_input"):
        raise DemoFailure(f"request {plan.request_id} came back {plan.status}: {plan.error or plan.reply}")
    if not plan.trip_ids:
        raise DemoFailure(f"request {plan.request_id} created no trip ({plan.status}: {plan.reply})")
    trips: list[Trip] = []
    for trip_id in plan.trip_ids:
        while True:
            trip = await reg.orchestrator.get_trip(trip_id)
            if trip.state == TripState.DONE:
                trips.append(trip)
                break
            if trip.state == TripState.FAILED:
                raise DemoFailure(f"{trip_id} failed: {trip.error}")
            if time.monotonic() > deadline:
                raise DemoFailure(f"{trip_id} stuck in {trip.state.value}")
            await asyncio.sleep(0.05)
    return trips


async def _wait_goals(reg: Registry, request_id: str, deadline: float) -> list[Goal]:
    fleet = reg.services.require("fleet")
    while True:
        goals = await fleet.list_goals(request_id=request_id, limit=100)
        if goals and all(g.terminal for g in goals):
            return goals
        if time.monotonic() > deadline:
            states = ", ".join(f"{g.goal_id}={g.status.value}" for g in goals) or "no goals"
            raise DemoFailure(f"goals of {request_id} not finished: {states}")
        await asyncio.sleep(0.05)


def _name_trips(names: dict[str, str], plan: RequestPlan, trips_label: str, reg: Registry) -> None:
    names[plan.request_id] = trips_label
    for n, trip_id in enumerate(plan.trip_ids):
        try:
            store = reg.orchestrator.peek(trip_id).store_id
        except KeyError:
            store = str(n)
        short = store.removeprefix("store_")
        names[trip_id] = f"{trips_label}-{short}" if len(plan.trip_ids) > 1 else trips_label


async def run_demo(
    *,
    user: str = DEMO_USER,
    text: str = DEMO_TEXT,
    staff_text: str | None = STAFF_TEXT,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    quiet: bool = False,
    allow_errors: bool = False,
) -> DemoResult:
    seed_ids(int(os.environ.get("PICKY_ID_SEED", "42")))
    reg = build_registry(
        fake_opts=FAKE_OPTS,
        real_opts=REAL_OPTS,
        orchestrator_config=OrchestratorConfig(
            approval_timeout_s=10.0, checkout_timeout_s=10.0, followup_timeout_s=None
        ),
    )
    real = sorted(a for a, m in reg.modes.items() if m == "real")
    print(f"picky demo | services: {', '.join(f'{a}={m}' for a, m in reg.modes.items())}")
    if reg.notes:
        print("notes: " + "; ".join(f"{a}: {n}" for a, n in reg.notes.items()))
    print(
        f'real: {real or "none (all fakes)"} | {user}: "{text}"' + (f' | staff: "{staff_text}"' if staff_text else "")
    )
    print()
    fleet = reg.services.fleet
    if fleet is None:
        raise DemoFailure(f"no fleet service ({reg.notes.get('fleet', 'missing')})")
    t0 = time.monotonic()
    names: dict[str, str] = {}
    try:
        from utils.data import load_robots

        names.update({rid: spec.name for rid, spec in load_robots().items()})
    except Exception:  # noqa: BLE001 - labels only
        pass
    stats: dict[str, Any] = {"types": {}, "errors": []}
    helpers = [
        asyncio.create_task(_printer(reg.bus.subscribe(None, name="demo-printer"), t0, quiet, names, stats)),
        asyncio.create_task(_web_fallback(reg, reg.bus.subscribe("approval.requested", name="demo-web-shopper"))),
    ]
    await reg.orchestrator.start()
    staff_plan: RequestPlan | None = None
    hill_plan: RequestPlan | None = None
    staff_goals: list[Goal] = []
    hill_goals: list[Goal] = []
    try:
        deadline = t0 + timeout_s
        plan1 = await fleet.submit_request(FleetRequest(text=text, user_id=user, channel="photon"))
        _name_trips(names, plan1, "trip1", reg)
        if staff_text:
            staff_plan = await fleet.submit_request(
                FleetRequest(text=staff_text, user_id=STAFF_USER_ID, role="staff", store_id=STAFF_STORE)
            )
            names[staff_plan.request_id] = "staff"
            if staff_plan.status in ("failed", "rejected", "needs_input") or not staff_plan.goals:
                raise DemoFailure(f"staff request came back {staff_plan.status} with no goal: {staff_plan.reply}")
            hill_plan = await fleet.submit_request(FleetRequest(text=HILL_TEXT, user_id=STAFF_USER_ID, role="staff"))
            names[hill_plan.request_id] = "staff-hill"
            if hill_plan.status in ("failed", "rejected", "needs_input") or not hill_plan.goals:
                raise DemoFailure(f"store_hill request came back {hill_plan.status} with no goal: {hill_plan.reply}")
        trips1 = await _wait_plan(reg, plan1, deadline)
        goals = await _wait_goals(reg, plan1.request_id, deadline)
        if staff_plan is not None:
            staff_goals = await _wait_goals(reg, staff_plan.request_id, deadline)
        if hill_plan is not None:
            hill_goals = await _wait_goals(reg, hill_plan.request_id, deadline)
        print("\n---- request 1 done; request 2 (same text, after the follow-up feedback) ----\n", flush=True)
        plan2 = await fleet.submit_request(FleetRequest(text=text, user_id=user, channel="photon"))
        _name_trips(names, plan2, "trip2", reg)
        trips2 = await _wait_plan(reg, plan2, deadline)
        goals += await _wait_goals(reg, plan2.request_id, deadline)
        plan1 = await fleet.get_request(plan1.request_id)
        plan2 = await fleet.get_request(plan2.request_id)
        alerts = []
        catalog = reg.services.catalog
        if catalog is not None and hasattr(catalog, "stock_alerts"):
            try:
                alerts = await catalog.stock_alerts(STAFF_STORE)
            except Exception as exc:  # noqa: BLE001
                stats["errors"].append(f"catalog: stock_alerts failed: {exc}")
        await asyncio.sleep(0.05)  # let the printer drain
    finally:
        await reg.orchestrator.stop()
        for h in helpers:
            h.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        reg.bus.close()

    result = DemoResult(
        plan1=plan1,
        plan2=plan2,
        trips1=trips1,
        trips2=trips2,
        staff_plan=staff_plan,
        staff_goals=staff_goals,
        hill_goals=hill_goals,
        goals=goals + staff_goals + hill_goals,
        stock_alerts=max(len(alerts), stats["types"].get("stock.alert", 0)),
        brain_steps=stats["types"].get("brain.step", 0),
        elapsed_s=time.monotonic() - t0,
    )
    _summary(result, names)
    problems = check_result(result, staff=staff_text is not None)
    if allow_errors:
        for e in stats["errors"]:
            print(f"WARNING (allowed): error event: {e}")
    else:
        problems += [f"error event: {e}" for e in stats["errors"]]
    if problems:
        raise DemoFailure("; ".join(problems))
    return result


def check_result(res: DemoResult, staff: bool = True) -> list[str]:
    problems: list[str] = []
    for label, trips in (("trip 1", res.trips1), ("trip 2", res.trips2)):
        for trip in trips:
            problems += check_trip(f"{label} ({trip.store_id})", trip)
    bad = [
        f"{g.goal_id} ({g.kind}) {g.status.value}: {g.error or g.failure_code or ''}"
        for g in res.goals
        if g.status != GoalStatus.COMPLETED
    ]
    if bad:
        problems.append("goals not completed: " + "; ".join(bad))
    if res.brain_steps == 0:
        problems.append("no brain.step events (robot brains did not report what they were doing)")
    if staff:
        scans = [g for g in res.staff_goals if g.kind == "shelf_scan"]
        if not scans:
            problems.append("the staff request created no shelf_scan goal")
        elif scans[0].robot_id != STAFF_ROBOT:
            problems.append(f"the shelf scan ran on {scans[0].robot_id}, expected {STAFF_ROBOT} (Pico is shopping)")
        if res.stock_alerts == 0:
            problems.append(f"no stock alerts for {STAFF_STORE} after the shelf scan")
        hill = [g for g in res.hill_goals if g.kind == "shelf_scan"]
        if not hill:
            problems.append("the store_hill request created no shelf_scan goal")
        elif (hill[0].robot_id, hill[0].store_id) != (HILL_ROBOT, HILL_STORE):
            problems.append(f"the store_hill scan ran on {hill[0].robot_id}@{hill[0].store_id}, expected {HILL_ROBOT}")
    if _rec_key(res.trips1) == _rec_key(res.trips2):
        problems.append(f"trip 2 recommendations did not change after feedback: {_rec_key(res.trips2)}")
    s1, s2 = _request_salsa(res.trips1), _request_salsa(res.trips2)
    if s1 == s2:
        problems.append(f"trip 2 still picks the same salsa ({s2}) after 'too mild' feedback")
    return problems


def _summary(res: DemoResult, names: dict[str, str]) -> None:
    print("\n==== summary ====")
    for label, plan, trips in (("request 1", res.plan1, res.trips1), ("request 2", res.plan2, res.trips2)):
        chosen = ", ".join(c.store_id for c in plan.store_choices if c.chosen) or "-"
        split = f" split {plan.budget_split}" if plan.split else ""
        print(f"{label}: {plan.request_id} {plan.status} stores={chosen}{split}")
        for trip in trips:
            receipt = trip.receipt
            paid = f"{money(receipt.amount_cents)} via {receipt.provider}" if receipt else "not paid"
            if trip.budget_cents is not None:
                paid += f" (budget {money(trip.budget_cents)})"
            robot = names.get(trip.robot_id or "", trip.robot_id)
            print(f"  {trip.trip_id} @{trip.store_id} robot={robot} state={trip.state.value} paid={paid}")
            print(
                f"     stops: {stop_summary(trip)} | cart {len(trip.cart.active_lines())} lines | "
                f"recs {[r.sku for r in trip.recs]}"
            )
    for g in res.staff_goals + res.hill_goals:
        robot = names.get(g.robot_id or "", g.robot_id)
        print(
            f"staff: {g.kind} {g.goal_id} on {robot} @{g.store_id} {g.status.value}: "
            f"{len(g.scanned_slot_ids)}/{len(g.slot_ids)} slots scanned"
        )
    print(f"stock alerts at {STAFF_STORE}: {res.stock_alerts} | brain steps: {res.brain_steps}")
    print(f"salsa: trip 1 {_request_salsa(res.trips1)} -> trip 2 {_request_salsa(res.trips2)}")
    print(f"elapsed {res.elapsed_s:.1f}s")


def _quiet_logs() -> None:
    """Timeline only: picky loggers at WARNING unless PICKY_LOG_LEVEL says otherwise."""
    if os.environ.get("PICKY_LOG_LEVEL"):
        return
    os.environ["PICKY_LOG_LEVEL"] = "WARNING"
    for name in list(logging.Logger.manager.loggerDict):
        if name.split(".")[0] in ("arms", "utils", "contracts"):
            logging.getLogger(name).setLevel(logging.WARNING)


def stop_summary(trip: Trip) -> str:
    counts: dict[str, int] = {}
    for status in trip.stop_status.values():
        counts[status] = counts.get(status, 0) + 1
    return ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no stops"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--user", default=DEMO_USER)
    parser.add_argument("--text", default=DEMO_TEXT)
    parser.add_argument("--staff-text", default=STAFF_TEXT)
    parser.add_argument("--no-staff", action="store_true", help="skip the concurrent staff shelf scan")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--allow-errors",
        action="store_true",
        help="report error events as warnings (e.g. a real arm degrading to its fallback) instead of failing",
    )
    args = parser.parse_args(argv)
    get_settings()  # fail early on a broken .env
    _quiet_logs()
    try:
        asyncio.run(
            run_demo(
                user=args.user,
                text=args.text,
                staff_text=None if args.no_staff else args.staff_text,
                timeout_s=args.timeout,
                quiet=args.quiet,
                allow_errors=args.allow_errors,
            )
        )
    except DemoFailure as exc:
        print(f"\nDEMO FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\nDEMO CRASHED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(
        "DEMO OK: fleet -> robot brains -> checkout -> follow-up for both requests, staff shelf scans in two stores "
        "(Rhea @store_ave raised stock alerts, Hazel @store_hill), and trip 2 changed its salsa and recommendations."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
