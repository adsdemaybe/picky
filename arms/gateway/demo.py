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
(d) planner -> VLA -> verifier loop (contracts 0.2.1; on unless ``--exec-mode direct``): shop goals run as typed step
    plans (navigate / align / ground / grasp / place / verify / handoff), each step checked by the verifier. The demo
    injects ONE bad grasp (``--faults drop_on_first_grasp``: the first grasp "succeeds" but nothing is held): the
    verifier must catch it and the retry must succeed. The summary prints that moment.

Exits non-zero unless every goal completed, every maya trip passed every pipeline state to ``done`` with no failed
stops and within budget (unless an over-budget ask was approved), stock alerts exist for store_ave, trip 2 changed,
in plan mode every shop trip ran a step plan to completion and the injected bad grasp was caught by the verifier and
retried successfully, and (without ``--allow-errors``) no ``error`` event was published. With fakes it finishes in
well under 30 s.

Approvals are answered by the scripted shopper in the messaging fake (Photon path); a web-shopper fallback answers
anything still pending after ``WEB_FALLBACK_S`` (approve / first option / echoes the checkout cart_hash; says no to
asks flagged over budget), so it also runs with real messaging and no phone.

Options: ``--quiet``, ``--text``, ``--user``, ``--staff-text``, ``--no-staff``, ``--timeout 30``, ``--allow-errors``,
``--exec-mode auto|plan|direct|tools`` (default PICKY_EXEC_MODE; blank = plan), ``--faults <scenario>|none``
(default PICKY_FAULTS, else drop_on_first_grasp). ``--no-staff`` also skips the store_hill scan.
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
from contracts.faults import DEMO_SCENARIOS
from contracts.fleet import FleetRequest, Goal, GoalStatus, RequestPlan
from contracts.shopping import TripState
from contracts.trip import Trip
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse
from utils.config import get_settings, set_settings
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
DEFAULT_FAULTS = "drop_on_first_grasp"
"""THE demo moment: the first grasp claims success with nothing held; the verifier catches it; the retry succeeds."""
NO_FAULTS = ("", "none", "clean", "off")

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
class VerifierCatch:
    """One 'the VLA said success, the verifier said no' moment and what happened next."""

    plan_id: str
    step_id: str
    robot_id: str | None
    title: str
    attempt: int
    code: str
    decision: str
    verdict_reason: str = ""
    verified_by: str = ""
    fault_ids: list[str] = field(default_factory=list)
    retry_status: str | None = None
    retry_attempts: int | None = None
    retry_verified_by: str = ""

    @property
    def retried_ok(self) -> bool:
        return self.retry_status == "succeeded" and (self.retry_attempts or 0) > self.attempt


@dataclass
class LoopStats:
    """What the planner -> VLA -> verifier loop did (filled from the event stream)."""

    plans: dict[str, str] = field(default_factory=dict)
    """plan_id -> robot_id of every plan.created / plan.revised."""
    revised_from: dict[str, str] = field(default_factory=dict)
    titles: dict[tuple[str, str], str] = field(default_factory=dict)
    completed: list[Any] = field(default_factory=list)
    """plan.completed events."""
    firings: list[Any] = field(default_factory=list)
    catches: list[VerifierCatch] = field(default_factory=list)
    verdicts: int = 0
    failed_verdicts: int = 0
    dropoffs: int = 0
    """Mid-trip basket drop-offs at the checkout (a full basket before a grasp)."""
    unload: dict[str, dict[str, int]] = field(default_factory=dict)
    """0.2.4 checkout unload by the arm: plan_id -> {'total', 'verified', 'skipped', 'done'} (checkout.unload)."""
    unload_lines: list[str] = field(default_factory=list)
    _pending_verdict: dict[tuple[str, str, int], Any] = field(default_factory=dict)

    def observe(self, ev: Any) -> None:
        t = ev.type
        if t in ("plan.created", "plan.revised"):
            self.plans[ev.plan.plan_id] = ev.plan.robot_id
            for st in ev.plan.steps:
                self.titles[(ev.plan.plan_id, st.step_id)] = st.title
            if t == "plan.revised":
                self.revised_from[ev.plan.plan_id] = ev.previous_plan_id
        elif t == "step.started":
            self.titles[(ev.plan_id, ev.step.step_id)] = ev.step.title
            if ev.step.kind == "handoff" and ev.step.group_id == "dropoff" and ev.attempt == 1:
                self.dropoffs += 1
        elif t == "step.verified":
            self.verdicts += 1
            if ev.verdict.outcome != "pass":
                self.failed_verdicts += 1
            if ev.caught_vla_false_success:
                self._pending_verdict[(ev.plan_id, ev.step_id, ev.attempt)] = ev.verdict
            for c in self.catches:
                if self._same_step(c, ev.plan_id, ev.step_id) and ev.attempt > c.attempt and ev.verdict.passed:
                    c.retry_verified_by = verified_by(ev.verdict)
        elif t == "step.failed":
            f = ev.failure
            if not f.caught_by_verifier:
                return
            verdict = f.verdict or self._pending_verdict.get((ev.plan_id, ev.step_id, f.attempt))
            self.catches.append(
                VerifierCatch(
                    plan_id=ev.plan_id,
                    step_id=ev.step_id,
                    robot_id=ev.robot_id,
                    title=self.titles.get((ev.plan_id, ev.step_id), ev.step_id),
                    attempt=f.attempt,
                    code=f.code,
                    decision=ev.decision.reason,
                    verdict_reason=verdict.reason if verdict is not None else f.message,
                    verified_by=verified_by(verdict) if verdict is not None else "",
                    fault_ids=[x.fault_id for x in self.firings if x.context.step_id == ev.step_id],
                )
            )
        elif t == "step.completed":
            for c in self.catches:
                if self._same_step(c, ev.plan_id, ev.step_id) and c.retry_status != "succeeded":
                    c.retry_status, c.retry_attempts = str(ev.status), ev.attempts
        elif t == "demo.fault":
            self.firings.append(ev.firing)
        elif t == "checkout.unload":
            u = self.unload.setdefault(ev.plan_id, {"total": 0, "verified": 0, "skipped": 0, "done": 0})
            u["total"] = max(u["total"], ev.total)
            if ev.status in ("verified", "skipped", "done"):
                u[ev.status] += 1
            if ev.status in ("verified", "done", "skipped"):
                self.unload_lines.append(ev.text)
        elif t == "plan.completed":
            self.completed.append(ev)

    def _same_step(self, c: VerifierCatch, plan_id: str, step_id: str) -> bool:
        if step_id != c.step_id:
            return False
        pid: str | None = plan_id
        while pid is not None:  # a re-plan keeps closed steps' ids: follow the revision chain back
            if pid == c.plan_id:
                return True
            pid = self.revised_from.get(pid)
        return False


def verified_by(verdict: Any) -> str:
    """'Perceptron perceptron-mk1.5' / 'Sim ground truth' / 'Robot state' (the UI's attribution badge)."""
    if verdict is None:
        return ""
    if verdict.provider is not None and verdict.decided_by == "provider":
        return verdict.provider.attribution
    return {"ground_truth": "Sim ground truth", "state": "Robot state", "human": "a person"}.get(
        verdict.decided_by, verdict.provider.attribution if verdict.provider is not None else verdict.decided_by
    ) + (" (degraded)" if verdict.degraded else "")


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
    exec_mode: str = "direct"
    faults: str | None = None
    loop: LoopStats = field(default_factory=LoopStats)

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
    return describe_loop(ev)


def describe_loop(ev: Any) -> str | None:
    """Planner -> VLA -> verifier loop events: the plan, failures, catches and summaries (routine progress is told by
    the mirrored brain.step lines, so step.started / passing checks / vla progress are not printed)."""
    t = ev.type
    if t in ("plan.created", "plan.revised"):
        p = ev.plan
        pl = p.planner
        how = (
            pl.origin
            + (f" {pl.model}" if pl.model else "")
            + (f", fallback: {pl.fallback_reason}" if pl.fallback_reason else "")
        )
        head = f"rev {p.revision} ({ev.reason}): " if t == "plan.revised" else ""
        return f"{head}{p.title or p.plan_id} | {len(p.steps)} steps for {len(p.items)} items, planner {how}"
    if t == "step.failed":
        f, d = ev.failure, ev.decision
        caught = " CAUGHT BY THE VERIFIER" if f.caught_by_verifier else ""
        return f"{ev.step_id} attempt {f.attempt} {f.code}{caught} -> {d.action}: {d.reason}"
    if t == "step.verified":
        v = ev.verdict
        if v.outcome == "pass" and not v.degraded and not ev.caught_vla_false_success:
            return None
        flag = " VLA said success" if ev.caught_vla_false_success else ""
        return f"{ev.step_id} {ev.phase} {v.outcome.upper()}{flag}: {v.reason} [{verified_by(v)}]"
    if t == "step.completed":
        if str(ev.status) in ("succeeded", "provisional"):
            return None
        return f"{ev.step_id} {ev.status} after {ev.attempts} attempt(s)" + (f": {ev.note}" if ev.note else "")
    if t == "vla.status":
        st = ev.status
        if st.phase != "failed":
            return None
        return f"{ev.step_id} {st.tier or ''} failed: {st.error.code if st.error else st.message}"
    if t == "vla.tier_changed":
        return f"{ev.from_tier or '-'} -> {ev.to_tier}: {ev.reason}"
    if t == "plan.completed":
        m = ev.stats
        return (
            f"{ev.status}: {ev.summary} | {m.steps_total} steps, {m.verifier_catches} verifier catch(es), "
            f"{m.retries} retr{'y' if m.retries == 1 else 'ies'}, {m.replans} replan(s), {m.duration_s:.1f}s"
        )
    if t == "demo.fault":
        c = ev.firing.context
        what = " ".join(x for x in (c.step_kind or "", c.sku or "", f"attempt {c.attempt}") if x)
        return f"INJECTED {ev.firing.effect} ({ev.firing.fault_id}, scenario {ev.scenario or '-'}) on {what}"
    if t == "checkout.unload":
        return ev.text if ev.status in ("verified", "skipped", "done") else None
    if t in ("step.started", "target.grounded"):
        return None
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
            stats["loop"].observe(ev)
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
    exec_mode: str | None = None,
    faults: str | None = None,
) -> DemoResult:
    """``exec_mode`` overrides PICKY_EXEC_MODE for this run ('auto' = blank); ``faults`` = a DEMO_SCENARIOS name
    (None = PICKY_FAULTS, else ``DEFAULT_FAULTS``; 'none' = no faults), injected only in plan / tools mode."""
    seed_ids(int(os.environ.get("PICKY_ID_SEED", "42")))
    previous_settings = get_settings()
    if exec_mode is not None:
        set_settings(previous_settings, picky_exec_mode="" if exec_mode == "auto" else exec_mode)
    try:
        return await _run_demo(
            user=user,
            text=text,
            staff_text=staff_text,
            timeout_s=timeout_s,
            quiet=quiet,
            allow_errors=allow_errors,
            faults=faults,
        )
    finally:
        set_settings(previous_settings)


async def _run_demo(
    *,
    user: str,
    text: str,
    staff_text: str | None,
    timeout_s: float,
    quiet: bool,
    allow_errors: bool,
    faults: str | None,
) -> DemoResult:
    reg = build_registry(
        fake_opts=FAKE_OPTS,
        real_opts=REAL_OPTS,
        orchestrator_config=OrchestratorConfig(
            approval_timeout_s=10.0, checkout_timeout_s=10.0, followup_timeout_s=None
        ),
    )
    real = sorted(a for a, m in reg.modes.items() if m == "real")
    mode = reg.exec_mode
    scenario = await _load_faults(reg, faults, mode)
    print(f"picky demo | services: {', '.join(f'{a}={m}' for a, m in reg.modes.items())}")
    if reg.notes:
        print("notes: " + "; ".join(f"{a}: {n}" for a, n in reg.notes.items()))
    print(
        f'real: {real or "none (all fakes)"} | {user}: "{text}"' + (f' | staff: "{staff_text}"' if staff_text else "")
    )
    loop_line = f"exec_mode: {mode}"
    if mode != "direct":
        loop_line += f" (planner -> VLA -> verifier loop) | faults: {scenario or 'none'}"
        if scenario in DEMO_SCENARIOS:
            loop_line += f" ({DEMO_SCENARIOS[scenario].description})"
    print(loop_line)
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
    stats: dict[str, Any] = {"types": {}, "errors": [], "loop": LoopStats()}
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
        exec_mode=mode,
        faults=scenario,
        loop=stats["loop"],
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


async def _load_faults(reg: Registry, faults: str | None, mode: str) -> str | None:
    """Load the demo fault scenario into ``services.faults`` (plan / tools mode only). Returns its name or None."""
    injector = reg.services.faults
    if mode == "direct" or injector is None:
        return None
    name = faults if faults is not None else (get_settings().picky_faults or DEFAULT_FAULTS)
    name = name.strip()
    if name.lower() in NO_FAULTS:
        await injector.clear_faults()
        return None
    try:
        loaded = await injector.load_scenario(name)
    except KeyError:
        raise DemoFailure(f"unknown fault scenario {name!r}; pick one of {sorted(DEMO_SCENARIOS)}") from None
    return loaded.name


GRASP_FAULT_EFFECTS = frozenset({"drop_after_lift", "wrong_item"})
"""Faults whose catch-and-retry the demo asserts (the verifier must catch them; the retry must succeed)."""


def _expects_catch(scenario: str | None) -> bool:
    sc = DEMO_SCENARIOS.get(scenario or "")
    return sc is not None and any(r.effect in GRASP_FAULT_EFFECTS for r in sc.rules)


def check_loop(res: DemoResult) -> list[str]:
    """Plan-mode assertions: every shop trip ran a step plan to completion; an injected bad grasp was caught by the
    verifier and the retry succeeded."""
    if res.exec_mode == "direct":
        return []
    problems: list[str] = []
    lp = res.loop
    trip_robots = {t.robot_id for t in (*res.trips1, *res.trips2)}
    planned = set(lp.plans.values())
    if not trip_robots <= planned:
        problems.append(f"plan mode, but no step plan ran on {sorted(r or '-' for r in trip_robots - planned)}")
    bad = [f"{e.plan_id} {e.status}: {e.summary}" for e in lp.completed if e.status != "completed"]
    if bad:
        problems.append("plans not completed: " + "; ".join(bad))
    if get_settings().picky_unload_by_arm:
        for pid in {e.plan_id for e in lp.completed if e.status == "completed"}:
            u = lp.unload.get(pid)
            if u is None or not u["total"]:
                problems.append(f"{pid}: the arm unloaded nothing at the checkout counter (checkout.unload)")
            elif u["verified"] + u["skipped"] < u["total"] or not u["done"]:
                problems.append(f"{pid}: checkout unload incomplete ({u})")
    if len(lp.completed) < len(res.trips1) + len(res.trips2):
        problems.append(f"only {len(lp.completed)} plan.completed for {len(res.trips1) + len(res.trips2)} shop trips")
    if _expects_catch(res.faults):
        if not lp.firings:
            problems.append(f"fault scenario {res.faults} loaded but no fault fired")
        if not lp.catches:
            problems.append(f"the injected bad grasp ({res.faults}) was not caught by the verifier")
        elif not any(c.retried_ok for c in lp.catches):
            c = lp.catches[0]
            problems.append(
                f"the verifier caught {c.title!r} but the retry did not succeed "
                f"(status {c.retry_status}, attempts {c.retry_attempts})"
            )
    return problems


def check_result(res: DemoResult, staff: bool = True) -> list[str]:
    problems: list[str] = check_loop(res)
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
    _loop_summary(res, names)
    print(f"elapsed {res.elapsed_s:.1f}s")


def _loop_summary(res: DemoResult, names: dict[str, str]) -> None:
    lp = res.loop
    if res.exec_mode == "direct":
        print("loop: exec_mode=direct (v0.2 brain path; no step plans)")
        return
    for e in lp.completed:
        m = e.stats
        robot = names.get(e.robot_id or "", e.robot_id)
        label = names.get(e.trip_id or "", e.trip_id or e.plan_id)
        print(
            f"plan {label} on {robot}: {e.status}, {m.steps_total} steps, {m.verifier_catches} verifier catch(es), "
            f"{m.retries} retr{'y' if m.retries == 1 else 'ies'}, {m.replans} replan(s), {m.duration_s:.1f}s"
        )
    print(
        f"verifier: {lp.verdicts} checks, {lp.failed_verdicts} not passed | basket drop-offs: {lp.dropoffs} | "
        f"faults: {res.faults or 'none'}"
    )
    for pid, u in lp.unload.items():
        print(
            f"checkout unload {pid}: {u['verified']}/{u['total']} units placed on the counter by the arm and verified"
            + (f", {u['skipped']} left for the handoff" if u["skipped"] else "")
        )
    fired: dict[tuple[str, str], list[Any]] = {}
    for f in lp.firings:
        fired.setdefault((f.fault_id, f.effect), []).append(f)
    for (fault_id, effect), group in fired.items():
        if len(group) > 1:
            print(f"  INJECTED {effect} ({fault_id}) x{len(group)}")
            continue
        c = group[0].context
        print(f"  INJECTED {effect} ({fault_id}) on {c.step_kind} {c.sku or ''} attempt {c.attempt}".rstrip())
    for c in lp.catches:
        robot = names.get(c.robot_id or "", c.robot_id)
        print(f"  CAUGHT {robot}: '{c.title}' attempt {c.attempt} claimed success; verifier: {c.verdict_reason}")
        print(f"     verified by {c.verified_by or '?'} -> {c.decision}")
        if c.retried_ok:
            by = f", verified by {c.retry_verified_by}" if c.retry_verified_by else ""
            print(f"     RETRY OK: attempt {c.retry_attempts} succeeded{by}")
        else:
            print(f"     retry: {c.retry_status or 'none'} (attempts {c.retry_attempts})")


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
    parser.add_argument(
        "--exec-mode",
        choices=["auto", "plan", "direct", "tools"],
        default=None,
        help="override PICKY_EXEC_MODE (auto = plan when the VLA + verifier are wired; direct = the v0.2 brain path)",
    )
    parser.add_argument(
        "--faults",
        default=None,
        help=f"demo fault scenario ({', '.join(sorted(DEMO_SCENARIOS))}, or none); default PICKY_FAULTS, else "
        f"{DEFAULT_FAULTS}",
    )
    args = parser.parse_args(argv)
    get_settings()  # fail early on a broken .env
    _quiet_logs()
    try:
        result = asyncio.run(
            run_demo(
                user=args.user,
                text=args.text,
                staff_text=None if args.no_staff else args.staff_text,
                timeout_s=args.timeout,
                quiet=args.quiet,
                allow_errors=args.allow_errors,
                exec_mode=args.exec_mode,
                faults=args.faults,
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
    if result.exec_mode != "direct":
        caught = any(c.retried_ok for c in result.loop.catches)
        print(
            f"LOOP OK: {len(result.loop.completed)} step plans completed ({result.exec_mode} mode)"
            + ("; the verifier caught the injected bad grasp and the retry succeeded." if caught else ".")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
