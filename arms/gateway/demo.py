"""Headless P0 demo: trip 1 -> follow-up feedback -> trip 2, through the real gateway pipeline.

``python -m arms.gateway.demo`` (= ``make demo``) uses whatever arms ``PICKY_REAL_ARMS`` selects (default: all fakes),
prints a compact event timeline, and exits non-zero unless both trips pass through every pipeline state to ``done``
with no ``error`` events and no failed stops, and trip 2 (after the "too mild" follow-up) picks a different salsa and
different recommendations. With fakes it finishes in well under 20 s (the robot fake runs at 120x).

Approvals are answered by the scripted shopper in the messaging fake (Photon path: approval text -> inbound reply ->
agent.interpret_reply -> broker). A web-shopper fallback answers anything still pending after ``WEB_FALLBACK_S``
(approve / first option / echoes the checkout cart_hash; says no to asks flagged over budget), so the demo also runs
with real messaging and no phone. Each trip must also stay within its stated budget unless the shopper explicitly
approved an over-budget item.

Options: ``--quiet`` (summary only), ``--text "..."``, ``--user maya``, ``--timeout 20``, ``--allow-errors`` (error
events become warnings, e.g. ``PICKY_REAL_ARMS=robot`` without a sim worker falling back to the 2D fake).
"""

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Any

from arms.gateway.orchestrator import OrchestratorConfig
from arms.gateway.registry import Registry, build_registry
from contracts.api import CreateTripRequest, RobotLoginRequest
from contracts.common import money
from contracts.shopping import TripState
from contracts.trip import Trip
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse
from utils.config import get_settings
from utils.ids import seed_ids

DEMO_TEXT = "tacos for 6, under $40, one vegetarian"
DEMO_USER = "maya"
WEB_FALLBACK_S = 1.5

FAKE_OPTS: dict[str, dict[str, Any]] = {
    "robot": {"realtime_factor": 120.0},
    "messaging": {"reply_delay_s": 0.05},
}


class DemoFailure(AssertionError):
    pass


# ----------------------------------------------------------------------------------------------------------------------
# timeline printing
# ----------------------------------------------------------------------------------------------------------------------
def describe(ev: Any) -> str | None:
    """One compact line per event (None = don't print)."""
    t = ev.type
    if t == "robot.state":
        return None
    if t == "trip.created":
        return f"store={ev.trip.store_id} user={ev.trip.user_id}"
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
        return f"{ev.stop_id} at ({ev.pose.x:.1f}, {ev.pose.y:.1f})"
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
        return f"[{r.kind.value}] {r.title}" + (f" ({len(r.options)} options)" if r.options else "")
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
        who = ev.role
        return f"{who}/{ev.channel or '-'}: {ev.text[:90]}"
    if t == "error":
        return f"{ev.arm}: {ev.message}" + (f" [{ev.code}]" if ev.code else "")
    return ""


async def _printer(sub: Any, t0: float, quiet: bool, trip_names: dict[str, str], errors: list[str]) -> None:
    async with sub:
        async for ev in sub:
            if ev.type == "error":
                errors.append(f"{trip_names.get(ev.trip_id or '', ev.trip_id or '-')}: {ev.arm}: {ev.message}")
            if quiet and ev.type != "error":
                continue
            line = describe(ev)
            if line is None:
                continue
            name = trip_names.get(ev.trip_id or "", ev.trip_id or "-")
            line = " ".join(line.split())
            print(f"{time.monotonic() - t0:6.2f}s #{ev.seq:<4} {name:<7} {ev.type:<19} {line}", flush=True)


async def _web_fallback(reg: Registry, sub: Any) -> None:
    """Answer approvals nobody answered within WEB_FALLBACK_S, like a shopper tapping the web UI."""
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
async def _wait_state(reg: Registry, trip_id: str, states: set[TripState], deadline: float) -> Trip:
    while True:
        trip = await reg.orchestrator.get_trip(trip_id)
        if trip.state in states:
            return trip
        if trip.state == TripState.FAILED:
            raise DemoFailure(f"{trip_id} failed: {trip.error}")
        if time.monotonic() > deadline:
            raise DemoFailure(f"{trip_id} stuck in {trip.state.value} (wanted {sorted(s.value for s in states)})")
        await asyncio.sleep(0.05)


async def run_trip(reg: Registry, user: str, text: str, deadline: float, names: dict[str, str], label: str) -> Trip:
    trip = await reg.orchestrator.create_trip(CreateTripRequest(user_id=user, text=text, channel="photon"))
    names[trip.trip_id] = label
    await _wait_state(reg, trip.trip_id, {TripState.AWAITING_LOGIN, TripState.EXECUTING}, deadline)
    await reg.orchestrator.login(trip.trip_id, RobotLoginRequest(user_id=user))
    return await _wait_state(reg, trip.trip_id, {TripState.DONE}, deadline)


def _rec_key(trip: Trip) -> list[str]:
    return [r.sku for r in trip.recs]


PIPELINE_STATES = [
    TripState.INTENT,
    TripState.LIST_READY,
    TripState.RECS_READY,
    TripState.ROUTE_PLANNED,
    TripState.AWAITING_LOGIN,
    TripState.EXECUTING,
    TripState.AWAITING_APPROVAL,
    TripState.CHECKOUT,
    TripState.PAID,
    TripState.FOLLOWUP,
    TripState.DONE,
]


def _salsa(trip: Trip) -> str | None:
    items = trip.shopping_list.items if trip.shopping_list else []
    return next((i.sku for i in items if i.query == "salsa"), None)


def check_trip(label: str, trip: Trip) -> list[str]:
    """Pipeline-level assertions for one finished trip (empty list = OK)."""
    problems: list[str] = []
    seen = {h.to_state for h in trip.history}
    missing = [s.value for s in PIPELINE_STATES if s not in seen]
    if missing:
        problems.append(f"{label} skipped pipeline states {missing}")
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


def _is_over_budget_ask(req: ApprovalRequest) -> bool:
    """Approval the gateway flagged as busting the trip budget (detour heads-up or over_budget mandate reason)."""
    return bool(req.payload.get("over_budget")) or "over_budget" in req.payload.get("reasons", [])


def _overage_approved(trip: Trip) -> bool:
    approved = {r.approval_id for r in trip.approval_responses if r.approved}
    return any(_is_over_budget_ask(a) and a.approval_id in approved for a in trip.approvals)


async def run_demo(
    *,
    user: str = DEMO_USER,
    text: str = DEMO_TEXT,
    timeout_s: float = 20.0,
    quiet: bool = False,
    allow_errors: bool = False,
) -> tuple[Trip, Trip]:
    seed_ids(int(os.environ.get("PICKY_ID_SEED", "42")))
    reg = build_registry(
        fake_opts=FAKE_OPTS,
        orchestrator_config=OrchestratorConfig(
            approval_timeout_s=10.0, checkout_timeout_s=10.0, followup_timeout_s=None
        ),
    )
    real = sorted(a for a, m in reg.modes.items() if m == "real")
    print(f"picky demo | arms: {', '.join(f'{a}={m}' for a, m in reg.modes.items())}")
    if reg.notes:
        print("notes: " + "; ".join(f"{a}: {n}" for a, n in reg.notes.items()))
    print(f'real arms: {real or "none (all fakes)"} | user={user} | "{text}"\n')
    t0 = time.monotonic()
    names: dict[str, str] = {}
    errors: list[str] = []
    helpers = [
        asyncio.create_task(_printer(reg.bus.subscribe(None, name="demo-printer"), t0, quiet, names, errors)),
        asyncio.create_task(_web_fallback(reg, reg.bus.subscribe("approval.requested", name="demo-web-shopper"))),
    ]
    await reg.orchestrator.start()
    try:
        deadline = t0 + timeout_s
        trip1 = await run_trip(reg, user, text, deadline, names, "trip1")
        print("\n---- trip 1 done; trip 2 (same request, after feedback) ----\n", flush=True)
        trip2 = await run_trip(reg, user, text, deadline, names, "trip2")
        await asyncio.sleep(0.05)  # let the printer drain
    finally:
        await reg.orchestrator.stop()
        for h in helpers:
            h.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        reg.bus.close()

    elapsed = time.monotonic() - t0
    r1, r2 = _rec_key(trip1), _rec_key(trip2)
    print("\n==== summary ====")
    for label, trip in (("trip 1", trip1), ("trip 2", trip2)):
        receipt = trip.receipt
        paid = f"{money(receipt.amount_cents)} via {receipt.provider}" if receipt else "not paid"
        if trip.budget_cents is not None:
            paid += f" (budget {money(trip.budget_cents)})"
        print(f"{label}: {trip.trip_id} state={trip.state.value} paid={paid} recs={[r.sku for r in trip.recs]}")
        print(f"        stops: {stop_summary(trip)} | cart {len(trip.cart.active_lines())} lines")
    print(f"salsa: trip 1 {_salsa(trip1)} -> trip 2 {_salsa(trip2)}")
    print(f"elapsed {elapsed:.1f}s")
    if trip1.state != TripState.DONE or trip2.state != TripState.DONE:
        raise DemoFailure("both trips must reach done")
    problems = check_trip("trip 1", trip1) + check_trip("trip 2", trip2)
    if allow_errors:
        for e in errors:
            print(f"WARNING (allowed): error event: {e}")
    else:
        problems += [f"error event: {e}" for e in errors]
    if r1 == r2:
        problems.append(f"trip 2 recommendations did not change after feedback: {r2}")
    if _salsa(trip1) == _salsa(trip2):
        problems.append(f"trip 2 still picks the same salsa ({_salsa(trip2)}) after 'too mild' feedback")
    if problems:
        raise DemoFailure("; ".join(problems))
    return trip1, trip2


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
    parser.add_argument("--timeout", type=float, default=20.0)
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
    print("DEMO OK: both trips ran every pipeline stage; trip 2 changed its salsa and recommendations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
