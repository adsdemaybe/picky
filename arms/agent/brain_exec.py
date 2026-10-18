"""Brain tool executor: runs the eleven brain tools (contracts/tools.py) for ONE robot on ONE goal.

Shared by the scripted (fake) brain and the Claude brain, so both produce the same side effects and events. The
executor supplies robot_id / store_id / goal / trip (never the model) and follows the executor mapping in the comments
of ``contracts/tools.py``:

- goals change only through ``services.fleet.update_goal`` (applied locally when no fleet is wired, e.g. tests),
- trips only through ``services.trips`` (set_stop_status, check_line, add_line, ask, replan, update_list),
- questions go through ``trips.ask`` (goals with a trip) or ``approvals.request`` (goals without one),
- every call emits brain.step tool_call + tool_result; robot.arrived / scan.result / pick.choices / item.picked /
  basket.verified carry ``goal_envelope(goal, robot_id)``.

RobotError codes come back as ``BrainToolError`` (``retryable`` from BRAIN_RETRYABLE_CODES); only BRAIN_FATAL_CODES or a
robot left in ERROR re-raise the RobotError (run_goal then raises without failing the goal, so the fleet reassigns).
"""

from __future__ import annotations

import contextlib
import math
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from arms.agent import logic
from contracts.api import AddLineResult
from contracts.cameras import VisionObservation, robot_camera_id
from contracts.catalog import ProductLocation
from contracts.common import ONLINE_STORE_ID, STAFF_USER_ID, Pose2D, money
from contracts.events import goal_envelope, make_event
from contracts.fleet import (
    TERMINAL_GOAL_STATUSES,
    BrainStep,
    BrainStepKind,
    Goal,
    GoalItem,
    GoalItemStatus,
    GoalItemUpdate,
    GoalStatus,
    GoalUpdate,
)
from contracts.planogram import Planogram
from contracts.routing import Route, RouteLeg, RouteRequest, Stop, StopStatus
from contracts.services import Services
from contracts.shopping import Cart, CartLine, ListItem
from contracts.sim import CameraFrame, ItemInstance, RobotError, RobotState, RobotStatus
from contracts.tools import (
    BRAIN_FATAL_CODES,
    BRAIN_RETRYABLE_CODES,
    AddItemInput,
    AskUserInput,
    AskUserResult,
    BrainPlanRouteInput,
    BrainToolError,
    ChoiceOption,
    FinishGoalInput,
    LocatedItem,
    LocateItemsInput,
    LocateItemsResult,
    LookInput,
    NavigateToInput,
    PickItemInput,
    PickResult,
    PlaceInBasketInput,
    PlaceResult,
    RouteSummary,
    ScanShelfInput,
    ScanShelfResult,
    UpdateProgressInput,
    parse_brain_tool_input,
)
from contracts.trip import Trip
from contracts.trust import (
    ApprovalKind,
    ApprovalOption,
    ApprovalRequest,
    ApprovalResponse,
    MandateDecision,
    MandateVerdict,
)
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)

ACTIVE_ITEM_STATUSES: frozenset[str] = frozenset({"pending", "located", "picked"})
"""Item statuses the brain still has to work on."""
LINE_KINDS: frozenset[str] = frozenset({"over_threshold", "recommendation", "substitution", "online_order"})
PICK_CHOICE_EXPIRES_S = 60.0
DEFAULT_BASKET_CAPACITY = 12
_ITEM_TO_STOP: dict[str, StopStatus] = {
    "in_basket": "done",
    "declined": "declined",
    "unavailable": "failed",
    "online": "failed",
    "skipped": "skipped",
    "failed": "failed",
}

StepEmitter = Callable[..., Awaitable[BrainStep]]


class ToolFailure(Exception):
    """A tool could not run (not a RobotError): becomes a BrainToolError result."""

    def __init__(self, code: str, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def _dump(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def apply_update_locally(goal: Goal, update: GoalUpdate) -> Goal:
    """What FleetService.update_goal does, for runs without a fleet (tests / standalone)."""
    g = goal.model_copy(deep=True)
    if update.status is not None and update.status != g.status:
        g.status = update.status
        if update.status in TERMINAL_GOAL_STATUSES:
            g.finished_at = g.updated_at
    if update.progress is not None:
        g.progress = update.progress
    if update.note is not None:
        g.progress_note = update.note
    for patch in update.items:
        item = g.item(patch.item_id)
        if item is None:
            continue
        for key, value in patch.model_dump(exclude={"item_id"}, exclude_none=True).items():
            setattr(item, key, value)
    known = {i.item_id for i in g.items}
    g.items.extend(i for i in update.add_items if i.item_id not in known)
    for sid in update.scanned_slot_ids:
        if sid not in g.scanned_slot_ids:
            g.scanned_slot_ids.append(sid)
    if g.kind == "shelf_scan" and g.slot_ids and update.progress is None:
        g.progress = min(1.0, len(g.scanned_slot_ids) / len(g.slot_ids))
    for key in ("summary", "error", "failure_code"):
        value = getattr(update, key)
        if value is not None:
            setattr(g, key, value)
    return g


class BrainExecutor:
    """Executes brain tools for (robot_id, goal). One instance per run_goal call."""

    def __init__(
        self,
        services: Services,
        robot_id: str,
        goal: Goal,
        *,
        emit: StepEmitter,
        model: str = "scripted",
        on_tool: Callable[[str | None], None] | None = None,
    ) -> None:
        self.services = services
        self.robot_id = robot_id
        self.goal = goal
        self.store_id = goal.store_id
        self.model = model
        self._emit = emit
        self._on_tool = on_tool
        self.route: Route | None = None
        self.at_stop: str | None = None
        self.held: ItemInstance | None = None
        self.held_item_id: str | None = None
        self.last_frames: dict[str, CameraFrame] = {}
        self.decisions: dict[str, MandateDecision] = {}
        self.checked: set[str] = set()
        self.item_approval: dict[str, str] = {}
        self.approvals: dict[str, tuple[ApprovalRequest, ApprovalResponse | None]] = {}
        self.line_ids: dict[str, str] = {}
        self.placed: Counter[str] = Counter()
        self.pending_approval_ids: set[str] = set()
        self.scan_results: dict[str, ScanShelfResult] = {}
        self.finished = False
        self._plan: Planogram | None = None
        self._robot_name: str | None = None
        self._capacity: int | None = None
        self._fresh: dict[str, tuple[Any, list[ApprovalOption]]] = {}
        self.drop_offs = 0

    # ==================================================================================================================
    # basics
    # ==================================================================================================================
    @property
    def trip_id(self) -> str | None:
        return self.goal.trip_id if self.goal.kind == "shop" else None

    @property
    def robot_name(self) -> str:
        if self._robot_name is None:
            self._robot_name = self.robot_id
            with contextlib.suppress(Exception):
                from utils.data import load_robot

                self._robot_name = load_robot(self.robot_id).name
        return self._robot_name

    @property
    def basket_capacity(self) -> int:
        if self._capacity is None:
            self._capacity = DEFAULT_BASKET_CAPACITY
            with contextlib.suppress(Exception):
                from utils.data import load_robot

                self._capacity = load_robot(self.robot_id).basket_capacity
        return self._capacity

    async def emit(self, kind: BrainStepKind, text: str = "", **kw: Any) -> BrainStep:
        return await self._emit(kind, text, goal=self.goal, model=self.model, **kw)

    async def thought(self, text: str) -> BrainStep:
        return await self.emit("thought", text)

    async def publish(self, type_: str, **payload: Any) -> None:
        events = self.services.events
        if events is None:
            return
        try:
            env = goal_envelope(self.goal, self.robot_id)
            await events.publish(make_event(type_, **env, source="agent", **payload))
        except Exception as e:  # events are best effort
            log.warning("publish %s failed: %s", type_, e)

    async def error(self, message: str, code: str | None = None, recoverable: bool = True) -> None:
        log.info("brain %s: %s", self.robot_id, message)
        await self.publish("error", arm="agent", message=message, code=code, recoverable=recoverable)

    async def trip(self) -> Trip | None:
        trips = self.services.trips
        if self.trip_id is None or trips is None:
            return None
        try:
            return await trips.get_trip(self.trip_id)
        except Exception as e:
            log.warning("get_trip(%s) failed: %s", self.trip_id, e)
            return None

    async def planogram(self) -> Planogram | None:
        if self._plan is None and self.services.catalog is not None:
            with contextlib.suppress(Exception):
                self._plan = await self.services.catalog.get_planogram(self.store_id)
        return self._plan

    async def pose(self) -> Pose2D | None:
        robot = self.services.robot
        if robot is None:
            return None
        with contextlib.suppress(Exception):
            return (await robot.state(self.robot_id)).pose
        return None

    async def update(self, update: GoalUpdate) -> Goal:
        fleet = self.services.fleet
        if fleet is None:
            self.goal = apply_update_locally(self.goal, update)
            return self.goal
        try:
            self.goal = await fleet.update_goal(self.goal.goal_id, update)
        except ValueError as e:  # invalid transition (e.g. cancelled meanwhile): keep our view, never crash
            log.warning("update_goal(%s) refused: %s", self.goal.goal_id, e)
            with contextlib.suppress(Exception):
                self.goal = await fleet.get_goal(self.goal.goal_id)
        except KeyError:
            self.goal = apply_update_locally(self.goal, update)
        return self.goal

    async def update_item(self, item_id: str, **fields: Any) -> Goal:
        return await self.update(GoalUpdate(items=[GoalItemUpdate(item_id=item_id, **fields)]))

    async def set_stop(self, stop_id: str | None, status: StopStatus, reason: str = "") -> None:
        trips = self.services.trips
        if self.trip_id is None or trips is None or not stop_id:
            return
        try:
            await trips.set_stop_status(self.trip_id, stop_id, status, reason)
        except Exception as e:
            log.warning("set_stop_status(%s, %s) failed: %s", stop_id, status, e)

    def item(self, item_id: str | None) -> GoalItem | None:
        return self.goal.item(item_id) if item_id else None

    def item_for(self, slot_id: str | None, sku: str | None = None) -> GoalItem | None:
        cands = [i for i in self.goal.items if i.slot_id == slot_id and (sku is None or i.sku == sku)]
        active = [i for i in cands if i.status in ACTIVE_ITEM_STATUSES]
        return (active or cands or [None])[0]

    async def product(self, sku: str | None) -> Any:
        if sku is None or self.services.catalog is None:
            return None
        with contextlib.suppress(Exception):
            return await self.services.catalog.get_product(sku)
        return None

    async def unit_price(self, item: GoalItem) -> int:
        """Rec price for detours, else the store offer, else the goal's hint, else the product base price."""
        if item.rec_id and item.price_cents is not None:
            return item.price_cents
        catalog = self.services.catalog
        if catalog is not None and item.sku:
            with contextlib.suppress(Exception):
                offers = await catalog.get_offers(item.sku, [self.store_id])
                if offers:
                    return offers[0].price_cents
        if item.price_cents is not None:
            return item.price_cents
        product = await self.product(item.sku)
        return product.price_cents if product is not None else 0

    async def is_produce(self, item: GoalItem) -> bool:
        product = await self.product(item.sku)
        if product is not None:
            return product.category == "produce"
        return item.category == "produce"

    async def blur(self, frame: CameraFrame | None) -> CameraFrame | None:
        """Face-blur a RAW frame; never returns an unblurred frame (None instead)."""
        if frame is None:
            return None
        perception = self.services.perception
        if perception is not None:
            try:
                return await perception.blur_faces(frame)
            except Exception as e:
                log.warning("blur_faces failed: %s", e)
        return frame if frame.blurred else None

    async def projected_cart(self, trip: Trip) -> Cart:
        """Cart + in-store list items not in it yet at their estimated prices (budget heads-up for detours)."""
        lines = list(trip.cart.lines)
        have = {ln.list_item_id for ln in lines if ln.list_item_id}
        for li in trip.shopping_list.items if trip.shopping_list else []:
            if li.sourcing != "in_store" or li.sku is None or li.item_id in have:
                continue
            lines.append(
                CartLine(
                    line_id=f"proj_{li.item_id}",
                    sku=li.sku,
                    name=li.query,
                    category=li.category or "uncategorized",
                    qty=li.qty,
                    unit_price_cents=(li.est_line_cents or 0) // max(li.qty, 1),
                    source="in_store",
                    store_id=trip.store_id,
                    list_item_id=li.item_id,
                )
            )
        return trip.cart.model_copy(update={"lines": lines})

    async def budget_overage(self, extra_cents: int | None) -> int:
        trip = await self.trip()
        if trip is None or trip.budget_cents is None or not extra_cents:
            return 0
        return max(0, (await self.projected_cart(trip)).total_cents + extra_cents - trip.budget_cents)

    # ==================================================================================================================
    # the tool entry point
    # ==================================================================================================================
    async def call(self, name: str, args: dict[str, Any] | None = None, *, tool_use_id: str | None = None) -> Any:
        """Run one brain tool: emits tool_call + tool_result steps; returns the result model or a BrainToolError.
        Raises RobotError only for BRAIN_FATAL_CODES / a robot left in ERROR (and CancelledError)."""
        args = dict(args or {})
        tool_use_id = tool_use_id or new_id("tu")
        if self._on_tool:
            self._on_tool(name)
        await self.emit("tool_call", _call_text(name, args), tool=name, tool_use_id=tool_use_id, input=args)
        started = time.perf_counter()
        result: Any = None
        inp: Any = None
        if name not in _TOOL_NAMES:
            result = BrainToolError(error_code="invalid_input", error=f"unknown tool {name!r}", retryable=False)
        else:
            try:
                inp = parse_brain_tool_input(name, args)
            except (ValidationError, ValueError) as e:
                result = BrainToolError(error_code="invalid_input", error=_short(str(e)), retryable=True)
        try:
            if result is None:
                result = await getattr(self, f"_t_{name}")(inp)
        except ToolFailure as e:
            result = BrainToolError(error_code=e.code, error=e.message, retryable=e.retryable)
        except RobotError as e:
            await self._raise_if_fatal(e)
            result = BrainToolError(error_code=e.code, error=e.message, retryable=e.code in BRAIN_RETRYABLE_CODES)
        except KeyError as e:  # unknown slot / sku / trip in another service
            result = BrainToolError(error_code="not_found", error=_short(f"not found: {e}"), retryable=True)
        except ValueError as e:  # a rule refused it (e.g. invalid transition)
            result = BrainToolError(error_code="rejected", error=_short(str(e)), retryable=False)
        except Exception as e:  # never let a tool crash the goal
            log.exception("brain tool %s failed", name)
            await self.error(f"{name} failed: {e}", code="internal")
            result = BrainToolError(error_code="internal", error=_short(f"{type(e).__name__}: {e}"), retryable=False)
        finally:
            if self._on_tool:
                self._on_tool(None)
        ok = not isinstance(result, BrainToolError)
        await self.emit(
            "tool_result",
            _result_text(name, result),
            tool=name,
            tool_use_id=tool_use_id,
            output=_dump(result),
            ok=ok,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return result

    async def _raise_if_fatal(self, exc: RobotError) -> None:
        if exc.code in BRAIN_FATAL_CODES:
            raise exc
        robot = self.services.robot
        if robot is not None:
            state = None
            with contextlib.suppress(Exception):
                state = await robot.state(self.robot_id)
            if state is not None and state.status == RobotStatus.ERROR:
                raise exc

    # ==================================================================================================================
    # tools
    # ==================================================================================================================
    async def _t_locate_items(self, inp: LocateItemsInput) -> LocateItemsResult:
        catalog = self.services.catalog
        if catalog is None:
            raise ToolFailure("not_ready", "catalog unavailable", retryable=True)
        found: list[LocatedItem] = []
        missing: list[str] = []
        for sku in inp.skus:
            located = await self._locate_sku(sku, sku, inp.in_stock_only)
            (found.append(located) if located else missing.append(sku))
        for query in inp.queries:
            hits = await logic.search_candidates(catalog, query, store_ids=[self.store_id], limit=8)
            best: LocatedItem | None = None
            for hit in hits:
                offer = hit.offer_for(self.store_id)
                if offer is None or offer.location is None or (inp.in_stock_only and not offer.in_stock):
                    continue
                cand = _located(query, hit.product, offer.location, offer.price_cents, offer.qty)
                if best is None or (cand.qty > 0 and best.qty == 0):
                    best = cand
                if cand.qty > 0:
                    break
            (found.append(best) if best else missing.append(query))
        # resolve goal items that had no slot yet (fetch goals created from free text)
        patches = []
        for loc in found:
            for item in self.goal.items:
                if item.slot_id is None and item.status in ACTIVE_ITEM_STATUSES and loc.query in (item.sku, item.query):
                    patches.append(
                        GoalItemUpdate(
                            item_id=item.item_id, sku=loc.sku, slot_id=loc.slot_id, status="located", notes=loc.name
                        )
                    )
        if patches:
            await self.update(GoalUpdate(items=patches))
        return LocateItemsResult(found=found, not_found=missing)

    async def _locate_sku(self, query: str, sku: str, in_stock_only: bool) -> LocatedItem | None:
        catalog = self.services.require("catalog")
        locs: list[ProductLocation] = []
        with contextlib.suppress(Exception):
            locs = await catalog.get_locations(sku, self.store_id)
        stocked = [loc for loc in locs if loc.qty > 0]
        pick = (stocked or ([] if in_stock_only else locs) or [None])[0]
        if pick is None:
            return None
        product = await self.product(sku)
        price = None
        with contextlib.suppress(Exception):
            offers = await catalog.get_offers(sku, [self.store_id])
            price = offers[0].price_cents if offers else None
        return _located(query, product, pick, price, pick.qty, sku=sku)

    async def _t_plan_route(self, inp: BrainPlanRouteInput) -> RouteSummary:
        trips = self.services.trips
        if self.trip_id is not None and trips is not None:
            route = await trips.replan(self.trip_id, include_recommendations=inp.include_optional)
        else:
            route = await self._plan_slots(inp.slot_ids or self._pending_slots(), inp.end_at)
        self._set_route(route)
        return RouteSummary.from_route(route)

    def _set_route(self, route: Route) -> None:
        self.route = route
        self.at_stop = route.order[0] if route.order else None

    def _pending_slots(self) -> list[str]:
        if self.goal.kind == "shelf_scan":
            done = set(self.goal.scanned_slot_ids)
            return [s for s in self.goal.slot_ids if s not in done]
        out: list[str] = []
        for item in self.goal.items:
            if item.slot_id and item.status in ACTIVE_ITEM_STATUSES and item.slot_id not in out:
                out.append(item.slot_id)
        return out

    async def _end_pose(self, end_at: str, plan: Planogram | None) -> Pose2D | None:
        if end_at == "dock":
            return await self.dock_pose()
        if plan is not None:
            return plan.checkout
        return None

    async def dock_pose(self) -> Pose2D | None:
        with contextlib.suppress(Exception):
            from utils.data import load_robot

            return load_robot(self.robot_id).dock
        plan = await self.planogram()
        return plan.start_pose if plan is not None else None

    async def _plan_slots(self, slot_ids: list[str], end_at: str) -> Route:
        plan = await self.planogram()
        if plan is None:
            raise ToolFailure("not_ready", "store planogram unavailable", retryable=True)
        start = await self.pose() or plan.start_pose
        stops: list[Stop] = []
        for sid in slot_ids:
            slot = plan.slot(sid)
            if slot is None:
                raise ToolFailure("unknown_slot", f"slot {sid} is not in {self.store_id}", retryable=True)
            if any(s.stop_id == sid for s in stops):
                continue
            stops.append(
                Stop(stop_id=sid, kind="mandatory", pose=slot.approach_pose, label=sid, slot_id=sid, skus=[slot.sku])
            )
        end = await self._end_pose(end_at, plan)
        routing = self.services.routing
        if routing is not None:
            try:
                return await routing.plan_route(
                    RouteRequest(store_id=self.store_id, start=start, end=end, stops=stops, max_optional_stops=0)
                )
            except Exception as e:
                log.warning("routing failed (%s); visiting slots in the given order", e)
        return _straight_route(self.store_id, start, end or start, stops)

    async def _path_to(self, target: Pose2D) -> list[Pose2D]:
        """Waypoints from the current pose to ``target`` (routing A* when wired, else a straight line)."""
        here = await self.pose()
        if here is None:
            return [target]
        routing = self.services.routing
        if routing is not None:
            with contextlib.suppress(Exception):
                route = await routing.plan_route(
                    RouteRequest(store_id=self.store_id, start=here, end=target, stops=[], max_optional_stops=0)
                )
                pts: list[Pose2D] = []
                for leg in route.legs:
                    pts.extend(leg.waypoints if not pts else leg.waypoints[1:])
                if pts:
                    pts[-1] = target
                    return pts
        return [here, target]

    def _route_stop(self, stop_id: str | None = None, slot_id: str | None = None) -> Stop | None:
        if self.route is None:
            return None
        if stop_id:
            return self.route.stop(stop_id)
        for stop in self.route.ordered_stops():
            if slot_id and stop.slot_id == slot_id and stop.kind in ("mandatory", "optional"):
                item = self.item(stop.stop_id)
                if item is None or item.status in ACTIVE_ITEM_STATUSES:
                    return stop
        return None

    def _leg_to(self, stop_id: str) -> RouteLeg | None:
        if self.route is None:
            return None
        return next((leg for leg in self.route.legs if leg.to_stop_id == stop_id), None)

    async def _waypoints_to_stop(self, stop: Stop) -> list[Pose2D]:
        leg = self._leg_to(stop.stop_id)
        if leg is None or self.at_stop not in (None, leg.from_stop_id):
            if self.trip_id is not None and self.services.trips is not None:
                with contextlib.suppress(Exception):
                    self._set_route(await self.services.trips.replan(self.trip_id))
                leg = self._leg_to(stop.stop_id)
                fresh = self.route.stop(stop.stop_id) if self.route else None
                stop = fresh or stop
            else:
                leg = None
        if leg is not None and leg.waypoints:
            return list(leg.waypoints)
        return await self._path_to(stop.pose)

    async def _t_navigate_to(self, inp: NavigateToInput) -> RobotState:
        robot = self.services.require("robot")
        plan = await self.planogram()
        stop: Stop | None = None
        target: Pose2D | None = None
        slot_id: str | None = None
        if inp.target == "stop":
            stop = self._route_stop(stop_id=inp.stop_id)
            if stop is None:
                trip = await self.trip()
                if trip is not None and trip.route is not None and trip.route.stop(inp.stop_id or ""):
                    self._set_route(trip.route)
                    stop = self.route.stop(inp.stop_id or "") if self.route else None
            if stop is None:
                raise ToolFailure("not_found", f"no route stop {inp.stop_id}; call plan_route", retryable=True)
            slot_id = stop.slot_id
        elif inp.target == "slot":
            slot_id = inp.slot_id
            stop = self._route_stop(slot_id=slot_id)
            if stop is None:
                slot = plan.slot(slot_id or "") if plan else None
                if slot is None:
                    raise ToolFailure("unknown_slot", f"slot {slot_id} is not in {self.store_id}", retryable=True)
                target = slot.approach_pose
        elif inp.target == "checkout":
            end = self._route_end()
            if end is not None:
                stop = end
            elif plan is not None:
                target = plan.checkout
        elif inp.target == "dock":
            target = await self.dock_pose()
        elif inp.target == "entrance" and plan is not None:
            target = plan.entrance
        if stop is None and target is None:
            raise ToolFailure("not_ready", f"don't know where {inp.target} is", retryable=False)

        trip_stop = stop is not None and self.trip_id is not None and stop.kind in ("mandatory", "optional")
        if stop is not None:
            waypoints = await self._waypoints_to_stop(stop)
            stop = (self.route.stop(stop.stop_id) if self.route else None) or stop
        else:
            assert target is not None
            waypoints = await self._path_to(target)
        if trip_stop:
            await self.set_stop(stop.stop_id, "navigating")  # type: ignore[union-attr]
        state = await robot.navigate(self.robot_id, waypoints)
        self.at_stop = stop.stop_id if stop is not None else None
        if trip_stop or (slot_id and stop is None) or (stop is not None and stop.slot_id):
            if trip_stop:
                await self.set_stop(stop.stop_id, "arrived")  # type: ignore[union-attr]
            await self.publish(
                "robot.arrived",
                stop_id=stop.stop_id if trip_stop else None,  # type: ignore[union-attr]
                slot_id=slot_id or (stop.slot_id if stop else None),
                goal_id=self.goal.goal_id,
                pose=state.pose,
            )
        return state

    def _route_end(self) -> Stop | None:
        if self.route is None or not self.route.order:
            return None
        end = self.route.stop(self.route.order[-1])
        return end if end is not None and end.kind == "end" else None

    async def _t_look(self, inp: LookInput) -> VisionObservation:
        cameras = self.services.cameras
        if cameras is None:
            raise ToolFailure("not_ready", "camera service unavailable", retryable=False)
        camera_id = robot_camera_id(self.robot_id, inp.camera)
        obs = await cameras.observe(camera_id, inp.question, goal_id=self.goal.goal_id)
        await self.emit("observation", obs.answer, observation=obs, camera_id=camera_id)
        return obs

    async def _t_scan_shelf(self, inp: ScanShelfInput) -> ScanShelfResult:
        robot = self.services.require("robot")
        plan = await self.planogram()
        slot = plan.slot(inp.slot_id) if plan else None
        expected = inp.expected_sku or (slot.sku if slot else None)
        raw = await robot.scan(self.robot_id, inp.slot_id)
        frame = await self.blur(raw)
        if frame is None:
            raise ToolFailure("blur_failed", "could not blur the scan frame; not analysing it", retryable=False)
        self.last_frames[inp.slot_id] = frame
        perception = self.services.perception
        if perception is None:
            raise ToolFailure("not_ready", "perception unavailable", retryable=False)
        report = await perception.identify(frame, self.store_id, inp.slot_id, expected)
        report = report.model_copy(
            update={
                "robot_id": self.robot_id,
                "goal_id": self.goal.goal_id,
                "trip_id": self.goal.trip_id,
                "camera_id": report.camera_id or frame.camera_id or robot_camera_id(self.robot_id, "front"),
            }
        )
        applied = None
        if self.services.catalog is not None:
            try:
                applied = await self.services.catalog.apply_scan(report)
            except Exception as e:
                log.warning("apply_scan failed: %s", e)
        await self.publish("scan.result", report=report)
        if self.goal.kind == "shelf_scan":
            await self.update(GoalUpdate(scanned_slot_ids=[inp.slot_id]))
        system_price = slot.price_cents if slot and slot.price_cents is not None else None
        if system_price is None and expected:
            product = await self.product(expected)
            system_price = product.price_cents if product else None
        mispriced = bool(applied.mispricing) if applied is not None else False
        if not mispriced and report.shelf_tag_cents is not None and system_price is not None:
            mispriced = report.shelf_tag_cents != system_price
        empty = report.observed_count == 0 and (report.count_is_exact or report.mismatch)
        notes = []
        if empty:
            notes.append("slot is empty")
        if mispriced:
            notes.append(f"shelf tag {money(report.shelf_tag_cents or 0)} differs from the system price")
        result = ScanShelfResult(
            slot_id=inp.slot_id,
            expected_sku=expected,
            observed_count=report.observed_count,
            in_stock=not empty,
            mismatch=report.mismatch,
            shelf_tag_cents=report.shelf_tag_cents,
            mispriced=mispriced,
            scan_id=report.scan_id,
            note="; ".join(notes),
        )
        self.scan_results[inp.slot_id] = result
        return result

    async def _full_line(self, item: GoalItem, qty: int | None = None, status: str = "picked") -> CartLine:
        product = await self.product(item.sku)
        trip = await self.trip()
        rec = next((r for r in trip.recs if r.rec_id == item.rec_id), None) if trip and item.rec_id else None
        assert item.sku is not None
        return CartLine(
            line_id=self.line_ids.setdefault(item.item_id, new_id("line")),
            sku=item.sku,
            name=product.name if product else (rec.product_name if rec else item.name or item.query),
            category=product.category if product else (item.category or "uncategorized"),
            qty=max(qty if qty is not None else item.qty, 1),
            unit_price_cents=await self.unit_price(item),
            source="in_store",
            store_id=self.store_id,
            slot_id=item.slot_id,
            status=status,  # type: ignore[arg-type]
            rec_id=item.rec_id,
            deal_id=rec.deal.deal_id if rec and rec.deal else None,
            list_item_id=item.list_item_id,
            user_id=self.goal.user_id,
            instance_ids=list(item.instance_ids),
        )

    def _approval_unlocks(self, approval_id: str | None, item: GoalItem) -> bool:
        if not approval_id or approval_id not in self.approvals:
            return False
        req, resp = self.approvals[approval_id]
        if resp is None or not resp.approved or req.kind.value not in LINE_KINDS:
            return False
        return req.sku == item.sku and req.payload.get("item_id") in (None, item.item_id)

    async def _t_pick_item(self, inp: PickItemInput) -> PickResult:
        robot = self.services.require("robot")
        item = self.item(inp.item_id) or self.item_for(inp.slot_id, inp.sku)
        decision: MandateDecision | None = None
        if item is not None and self.trip_id is not None and self.services.trips is not None:
            if item.item_id not in self.checked:
                if self._approval_unlocks(inp.approval_id, item):
                    self.checked.add(item.item_id)
                    self.item_approval[item.item_id] = inp.approval_id  # type: ignore[assignment]
                else:
                    line = await self._full_line(item, status="planned")
                    decision = await self.services.trips.check_line(self.trip_id, line)
                    self.decisions[item.item_id] = decision
                    if decision.verdict == MandateVerdict.DENY:
                        reason = "blocked by mandate: " + "; ".join(decision.messages)
                        await self.update_item(item.item_id, status="failed", notes=reason)
                        await self.set_stop(item.stop_id, "failed", reason)
                        return PickResult(picked=False, decision=decision, error_code="mandate_denied", note=reason)
                    if decision.verdict == MandateVerdict.NEEDS_APPROVAL:
                        return PickResult(
                            picked=False,
                            needs_approval=True,
                            decision=decision,
                            note=f"ask_user kind over_threshold item_id={item.item_id}, then pick_item with that "
                            "approval_id",
                        )
                    self.checked.add(item.item_id)
            decision = self.decisions.get(item.item_id)
        if self.held is not None:
            raise ToolFailure("busy", f"already holding {self.held.sku}; place_in_basket first", retryable=True)
        if await self._basket_full():
            await self.drop_off()
        inst = await robot.pick(self.robot_id, inp.slot_id, inp.sku, inp.instance_id)
        self.held = inst
        self.held_item_id = item.item_id if item else None
        if item is not None and item.status != "picked":
            await self.update_item(item.item_id, status="picked", slot_id=inp.slot_id)
        return PickResult(picked=True, instance=inst, decision=decision)

    async def _t_place_in_basket(self, inp: PlaceInBasketInput) -> PlaceResult:
        robot = self.services.require("robot")
        if self.held is None:
            raise ToolFailure("nothing_held", "the gripper is empty; pick_item first", retryable=False)
        inst = self.held
        try:
            state = await robot.place_in_basket(self.robot_id)
        except RobotError as e:
            if e.code in BRAIN_FATAL_CODES or not await self._basket_full():
                raise
            await self.drop_off()  # basket filled up while holding a unit: unload at checkout, then place it
            state = await robot.place_in_basket(self.robot_id)
        self.held = None
        item = self.item(inp.item_id or self.held_item_id)
        self.held_item_id = None
        slot_id = inst.slot_id or (item.slot_id if item else None)
        if self.services.catalog is not None and slot_id:
            try:
                await self.services.catalog.record_pick(
                    self.store_id, slot_id, inst.sku, 1, self.goal.trip_id, self.robot_id, inst.lot_id
                )
            except Exception as e:
                log.warning("record_pick failed: %s", e)
        self.placed[inst.sku] += 1
        line_id = self.line_ids.setdefault(item.item_id, new_id("line")) if item else None
        await self.publish(
            "item.picked",
            sku=inst.sku,
            slot_id=slot_id,
            instance_id=inst.instance_id,
            line_id=line_id if self.trip_id else None,
            stop_id=item.stop_id if item else None,
            goal_id=self.goal.goal_id,
        )
        result = PlaceResult(sku=inst.sku, basket_count=len(state.basket), note="")
        if item is not None:
            picked = item.picked_qty + 1
            ids = [*item.instance_ids, inst.instance_id]
            await self.update_item(item.item_id, picked_qty=picked, instance_ids=ids, robot_id=self.robot_id)
            item = self.item(item.item_id) or item
            result.item_id = item.item_id
            result.picked_qty = picked
            if picked >= item.qty:
                added = await self._close_item(item, verify=inp.verify)
                result.line_id = added.line_id if added else None
                result.cart_total_cents = added.cart.total_cents if added else None
                result.basket_ok = self._last_basket_ok
                return result
        if inp.verify:
            result.basket_ok = await self.verify_basket()
        return result

    _last_basket_ok: bool | None = None

    async def _basket_full(self) -> bool:
        robot = self.services.robot
        if robot is None:
            return False
        with contextlib.suppress(Exception):
            return len((await robot.state(self.robot_id)).basket) >= self.basket_capacity
        return False

    async def drop_off(self) -> bool:
        """Basket full mid-goal: drive to the checkout, unload (staff bag it there), come back to where we were."""
        robot = self.services.require("robot")
        plan = await self.planogram()
        here = await self.pose()
        if plan is None or here is None:
            return False
        await self.thought(f"My basket is full ({self.basket_capacity} items); dropping it off at the checkout.")
        try:
            await robot.navigate(self.robot_id, await self._path_to(plan.checkout))
            unloaded = await robot.unload_basket(self.robot_id)
            self.placed.clear()  # the basket check now expects only units placed after the drop-off
            self.drop_offs += 1
            await self.emit("message", f"Dropped {len(unloaded)} items at the checkout; heading back.")
            await robot.navigate(self.robot_id, await self._path_to(here))
            return True
        except RobotError as e:
            await self._raise_if_fatal(e)
            await self.error(f"drop-off at checkout failed: {e}", code=e.code)
            return False

    async def _close_item(self, item: GoalItem, *, verify: bool = True) -> AddLineResult | None:
        """All units of an item are in (or the brain closed it early): add the cart line ONCE, verify, stop done."""
        added: AddLineResult | None = None
        line_id: str | None = None
        if self.trip_id is not None and self.services.trips is not None and item.line_id is None:
            line = await self._full_line(item, qty=item.picked_qty)
            try:
                added = await self.services.trips.add_line(
                    self.trip_id, line, approval_id=self.item_approval.get(item.item_id)
                )
                line_id = added.line_id or line.line_id
                if not added.added:
                    await self.error(f"{line.name} picked but not added: {added.decision.verdict}", code="trust")
            except Exception as e:
                await self.error(f"add_line for {line.name} failed: {e}", code="trust")
        await self.update_item(item.item_id, status="in_basket", line_id=line_id)
        self._last_basket_ok = await self.verify_basket() if verify else None
        await self.set_stop(item.stop_id, "done", f"picked {item.picked_qty}")
        return added

    async def verify_basket(self) -> bool | None:
        """Basket camera vs the units THIS robot placed during this goal."""
        perception = self.services.perception
        robot = self.services.robot
        if perception is None or robot is None:
            return None
        try:
            raw = await robot.capture(self.robot_id, "basket")
            frame = await self.blur(raw)
            if frame is None:
                return None
            check = await perception.verify_basket(frame, dict(self.placed))
            await self.publish("basket.verified", check=check)
            if not check.ok:
                await self.error(f"basket mismatch: missing {check.missing} extra {check.unexpected}", "perception")
            return check.ok
        except RobotError as e:
            await self._raise_if_fatal(e)
            await self.error(f"basket check failed: {e}", code=e.code)
        except Exception as e:
            await self.error(f"basket check failed: {e}", code="perception")
        return None

    async def ask(self, request: ApprovalRequest) -> ApprovalResponse:
        """Ask the goal's user (trips.ask with a trip, else the approval broker); goal WAITING while blocked."""
        trips, approvals = self.services.trips, self.services.approvals
        self.approvals[request.approval_id] = (request, None)
        self.pending_approval_ids.add(request.approval_id)
        await self.update(GoalUpdate(status=GoalStatus.WAITING, note=request.title))
        # kept in pending_approval_ids if this raises (cancellation): run_goal withdraws it with approvals.cancel
        if self.trip_id is not None and trips is not None:
            resp = await trips.ask(self.trip_id, request)
        elif approvals is not None:
            resp = await approvals.request(request)
        else:  # nobody to ask: deterministic default
            resp = ApprovalResponse(approval_id=request.approval_id, decision="timeout", channel="system")
        self.pending_approval_ids.discard(request.approval_id)
        self.approvals[request.approval_id] = (request, resp)
        if self.goal.status == GoalStatus.WAITING:
            await self.update(GoalUpdate(status=GoalStatus.ACTIVE))
        return resp

    async def _t_ask_user(self, inp: AskUserInput) -> AskUserResult:
        item = self.item(inp.item_id)
        kind = ApprovalKind(inp.kind)
        sku, rec_id, amount = inp.sku, inp.rec_id, None
        payload: dict[str, Any] = {}
        options: list[ApprovalOption] = [
            ApprovalOption(option_id=str(n + 1), label=label) for n, label in enumerate(inp.options)
        ]
        expires: float | None = None
        choice = None
        if inp.kind in LINE_KINDS or inp.kind == "pick_choice":
            if item is None and inp.kind in ("over_threshold", "recommendation", "pick_choice"):
                raise ToolFailure("invalid_input", f"{inp.kind} needs the item_id of a goal item", retryable=True)
            if item is not None:
                sku, rec_id = item.sku, item.rec_id
                payload["item_id"] = item.item_id
                if inp.kind in LINE_KINDS:
                    amount = item.qty * await self.unit_price(item)
        if inp.kind == "over_threshold" and item is not None:
            decision = self.decisions.get(item.item_id)
            payload["reasons"] = [str(r) for r in decision.reasons] if decision else []
        elif inp.kind == "recommendation" and item is not None:
            trip = await self.trip()
            rec = next((r for r in trip.recs if r.rec_id == item.rec_id), None) if trip and item.rec_id else None
            stop = trip.route.stop(item.stop_id) if trip and trip.route and item.stop_id else None
            over = await self.budget_overage(rec.price_cents if rec else item.price_cents)
            payload.update(
                {
                    "interest": stop.interest if stop else (rec.interest if rec else 1.0),
                    "sponsored": rec.sponsored if rec else False,
                    "over_budget": over > 0,
                    "over_budget_cents": over,
                }
            )
            if rec is not None and rec.price_cents is not None and item.qty == 1:
                amount = rec.price_cents
        elif inp.kind == "pick_choice" and item is not None:
            choice, options = await self._freshness_options(item)
            expires = PICK_CHOICE_EXPIRES_S
        request = ApprovalRequest(
            approval_id=new_id("appr"),
            trip_id=self.trip_id,
            goal_id=self.goal.goal_id,
            robot_id=self.robot_id,
            store_id=self.store_id,
            user_id=self.goal.user_id or STAFF_USER_ID,
            kind=kind,
            title=inp.question,
            body=inp.body,
            amount_cents=amount,
            sku=sku,
            rec_id=rec_id,
            options=options,
            payload=payload,
            **({"expires_s": expires} if expires else {}),
        )
        if choice is not None:
            await self.publish("pick.choices", choice=choice, approval_id=request.approval_id)
        await self.emit("message", f"{self.robot_name} asks: {inp.question}")
        resp = await self.ask(request)
        return AskUserResult(
            approval_id=request.approval_id,
            decision=resp.decision,
            approved=resp.approved,
            choice=resp.choice,
            choices=list(resp.choices),
            options=[ChoiceOption(option_id=o.option_id, label=o.label, detail=o.detail) for o in options],
        )

    async def freshness_available(self, item: GoalItem) -> bool:
        """True when the last scan of the item's slot has units of noticeably different freshness to choose from."""
        try:
            self._fresh[item.item_id] = await self._freshness_options(item)
            return True
        except ToolFailure:
            return False
        except Exception as e:
            log.warning("rank_freshness failed: %s", e)
            return False

    async def _freshness_options(self, item: GoalItem) -> tuple[Any, list[ApprovalOption]]:
        cached = self._fresh.pop(item.item_id, None)
        if cached is not None:
            return cached
        perception = self.services.perception
        frame = self.last_frames.get(item.slot_id or "")
        if perception is None or frame is None or item.slot_id is None or item.sku is None:
            raise ToolFailure("nothing_to_choose", "scan the slot first (no frame to rank)", retryable=True)
        choice = await perception.rank_freshness(frame, item.slot_id, item.sku, k=3)
        flat = len(choice.candidates) > 1 and len({round(c.score, 3) for c in choice.candidates}) == 1
        if not choice.candidates or flat:
            raise ToolFailure("nothing_to_choose", "no freshness difference between units; pick any", retryable=False)
        ranked = sorted(choice.candidates, key=lambda c: c.rank)
        options = [
            ApprovalOption(
                option_id=c.instance_id,
                label=f"#{c.rank} · freshness {c.score:.0%}",
                detail=c.reason or None,
                thumbnail_b64=c.thumbnail_b64,
            )
            for c in ranked
        ]
        return choice, options

    async def _t_add_item(self, inp: AddItemInput) -> GoalItem:
        catalog = self.services.catalog
        if catalog is None:
            raise ToolFailure("not_ready", "catalog unavailable", retryable=True)
        located = await self._locate_sku(inp.sku, inp.sku, False) if inp.sku else None
        if located is None:
            res = await self._t_locate_items(LocateItemsInput(queries=[inp.query], in_stock_only=False))
            located = res.found[0] if res.found else None
        if located is None or located.sku is None:
            raise ToolFailure("not_found", f"no {inp.query} in {self.store_id}", retryable=False)
        item_id = new_id("item")
        stop_id: str | None = None
        list_item_id: str | None = None
        trips = self.services.trips
        trip = await self.trip()
        if trip is not None and trips is not None and trip.shopping_list is not None:
            list_item_id = new_id("li")
            li = ListItem(
                item_id=list_item_id,
                query=inp.query,
                sku=located.sku,
                qty=inp.qty,
                sourcing="in_store",
                store_id=self.store_id,
                category=located.category,
                est_line_cents=(located.price_cents or 0) * inp.qty,
                notes=f"added by {self.robot_name} on request",
            )
            sl = trip.shopping_list.model_copy(update={"items": [*trip.shopping_list.items, li]})
            try:
                await trips.update_list(trip.trip_id, sl)
                self._set_route(await trips.replan(trip.trip_id))
                stop = next((s for s in self.route.stops if list_item_id in s.item_ids), None) if self.route else None
                stop_id = stop.stop_id if stop else None
            except Exception as e:
                await self.error(f"could not add {inp.query} to the trip list: {e}", code="trips")
        new_item = GoalItem(
            item_id=stop_id or item_id,
            query=inp.query,
            sku=located.sku,
            name=located.name,
            qty=inp.qty,
            slot_id=located.slot_id,
            stop_id=stop_id,
            category=located.category,
            list_item_id=list_item_id,
            price_cents=located.price_cents,
            status="located",
            notes="added on request",
        )
        if self.goal.item(new_item.item_id) is None:
            await self.update(GoalUpdate(add_items=[new_item]))
        return self.goal.item(new_item.item_id) or new_item

    async def _t_update_progress(self, inp: UpdateProgressInput) -> Goal:
        patches: list[GoalItemUpdate] = []
        item = self.item(inp.item_id)
        if inp.item_id and item is None:
            raise ToolFailure("not_found", f"no goal item {inp.item_id}", retryable=True)
        if item is not None and inp.item_status is not None:
            status: GoalItemStatus = inp.item_status
            fields: dict[str, Any] = {"status": status, "notes": inp.note}
            if status == "substituted" and inp.substitute_sku:
                fields["sku"] = inp.substitute_sku
            shop = self.trip_id is not None
            if status == "in_basket" and item.line_id is None and item.picked_qty > 0:
                await self._close_item(item)  # partial pick: add what is in the basket
                fields.pop("status")
            elif status == "unavailable" and shop:
                await self.set_stop(item.stop_id, "failed", "out of stock on the shelf; ordering online")
                if not item.optional and await self._order_online(item):
                    fields["status"] = "online"
            elif shop and status in _ITEM_TO_STOP:
                await self.set_stop(item.stop_id, _ITEM_TO_STOP[status], inp.note)
            patches.append(GoalItemUpdate(item_id=item.item_id, **fields))
        progress = inp.progress
        update = GoalUpdate(note=inp.note, progress=progress, items=patches)
        goal = await self.update(update)
        if progress is None and goal.items and goal.kind in ("shop", "fetch"):
            done = sum(1 for i in goal.items if i.status not in ACTIVE_ITEM_STATUSES)
            goal = await self.update(GoalUpdate(progress=round(done / len(goal.items), 3)))
        if item is not None and inp.item_status == "declined" and self.trip_id and self.services.trips is not None:
            try:
                self._set_route(await self.services.trips.replan(self.trip_id))
            except Exception as e:
                await self.error(f"re-plan failed: {e}", code="routing")
        return goal

    async def _order_online(self, item: GoalItem) -> bool:
        """Out of stock in store: add the list item(s) as an online line (asks first if the mandate wants it)."""
        trips, catalog = self.services.trips, self.services.catalog
        if trips is None or self.trip_id is None or item.sku is None:
            return False
        trip = await self.trip()
        li = None
        if trip is not None and trip.shopping_list is not None and item.list_item_id:
            li = next((x for x in trip.shopping_list.items if x.item_id == item.list_item_id), None)
        sku = (li.sku if li and li.sku else None) or item.sku
        qty = li.qty if li else item.qty
        unit, name, category = None, item.name or item.query, item.category or "uncategorized"
        if catalog is not None:
            with contextlib.suppress(Exception):
                offers = await catalog.get_offers(sku, [ONLINE_STORE_ID])
                stocked = [o for o in offers if o.in_stock]
                if not stocked:
                    await self.error(f"{item.query} is unavailable online too", code="catalog")
                    return False
                unit = stocked[0].price_cents
        product = await self.product(sku)
        if product is not None:
            name, category = product.name, product.category
            unit = unit if unit is not None else product.price_cents
        if unit is None:
            unit = (li.est_line_cents or 0) // max(qty, 1) if li else (item.price_cents or 0)
        line = CartLine(
            line_id=new_id("line"),
            sku=sku,
            name=name,
            category=category,
            qty=qty,
            unit_price_cents=unit,
            source="online",
            store_id=ONLINE_STORE_ID,
            status="planned",
            list_item_id=item.list_item_id,
            user_id=self.goal.user_id,
        )
        try:
            res = await trips.add_line(self.trip_id, line)
            if not res.added and res.decision.verdict == MandateVerdict.NEEDS_APPROVAL:
                req = ApprovalRequest(
                    approval_id=new_id("appr"),
                    trip_id=self.trip_id,
                    goal_id=self.goal.goal_id,
                    robot_id=self.robot_id,
                    store_id=self.store_id,
                    user_id=self.goal.user_id or STAFF_USER_ID,
                    kind=ApprovalKind.ONLINE_ORDER,
                    title=f"{name} is out of stock here. Order it online for {money(line.total_cents)}?",
                    body="; ".join(res.decision.messages),
                    amount_cents=line.total_cents,
                    sku=sku,
                    payload={"item_id": item.item_id, "reasons": [str(r) for r in res.decision.reasons]},
                )
                resp = await self.ask(req)
                if not resp.approved:
                    return False
                res = await trips.add_line(self.trip_id, line, approval_id=req.approval_id)
            if not res.added:
                await self.error(f"online {name} blocked by mandate", code="trust")
            return res.added
        except Exception as e:
            await self.error(f"online order of {name} failed: {e}", code="trips")
            return False

    async def _t_finish_goal(self, inp: FinishGoalInput) -> Goal:
        robot = self.services.robot
        if self.held is not None and robot is not None:  # never leave a unit in the gripper
            with contextlib.suppress(Exception):
                await self._t_place_in_basket(PlaceInBasketInput(verify=False))
        end_at = self.goal.end_at
        if end_at == "checkout" and self.goal.kind in ("shop", "fetch") and robot is not None:
            end = self._route_end()
            if end is None or self.at_stop != end.stop_id:
                try:
                    await self._t_navigate_to(NavigateToInput(target="checkout"))
                except RobotError as e:
                    await self._raise_if_fatal(e)
                    await self.error(f"could not reach checkout: {e}", code=e.code)
                except ToolFailure as e:
                    await self.error(f"could not reach checkout: {e.message}", code=e.code)
            try:
                await robot.unload_basket(self.robot_id)
            except RobotError as e:
                await self._raise_if_fatal(e)
                await self.error(f"unload at checkout failed: {e}", code=e.code)
            except Exception as e:
                await self.error(f"unload at checkout failed: {e}", code="robot")
        elif end_at == "dock" and robot is not None:
            dock = await self.dock_pose()
            here = await self.pose()
            if dock is not None and (here is None or here.distance_to(dock) > 0.15):
                try:
                    await robot.navigate(self.robot_id, await self._path_to(dock))
                except RobotError as e:
                    await self._raise_if_fatal(e)
                    await self.error(f"could not reach the dock: {e}", code=e.code)
        status = GoalStatus.COMPLETED if inp.status == "completed" else GoalStatus.FAILED
        update = GoalUpdate(
            status=status,
            summary=inp.summary,
            note=inp.summary,
            failure_code=inp.failure_code if status == GoalStatus.FAILED else None,
            progress=1.0 if status == GoalStatus.COMPLETED else None,
        )
        goal = await self.update(update)
        self.finished = True
        await self.emit("message", inp.summary)
        return goal


_TOOL_NAMES = {
    "locate_items",
    "plan_route",
    "navigate_to",
    "look",
    "scan_shelf",
    "pick_item",
    "place_in_basket",
    "ask_user",
    "add_item",
    "update_progress",
    "finish_goal",
}


def _located(
    query: str, product: Any, loc: ProductLocation, price: int | None, qty: int, sku: str | None = None
) -> LocatedItem:
    return LocatedItem(
        query=query,
        sku=sku or (product.sku if product else loc.sku),
        name=product.name if product else None,
        category=product.category if product else None,
        slot_id=loc.slot_id,
        aisle_label=loc.aisle_label,
        level=loc.level,
        item_z_m=loc.item_position.z,
        qty=qty,
        price_cents=price,
    )


def _straight_route(store_id: str, start: Pose2D, end: Pose2D, stops: list[Stop]) -> Route:
    """Visit ``stops`` in the given order with straight legs (no routing arm)."""
    s = Stop(stop_id="start", kind="start", pose=start)
    e = Stop(stop_id="end", kind="end", pose=end)
    seq = [s, *stops, e]
    legs = []
    total = 0.0
    for a, b in zip(seq, seq[1:], strict=False):
        d = math.hypot(b.pose.x - a.pose.x, b.pose.y - a.pose.y)
        total += d
        legs.append(RouteLeg(from_stop_id=a.stop_id, to_stop_id=b.stop_id, waypoints=[a.pose, b.pose], distance_m=d))
    return Route(
        route_id=new_id("route"),
        store_id=store_id,
        stops=seq,
        order=[x.stop_id for x in seq],
        legs=legs,
        total_distance_m=round(total, 2),
        solver="fake",
    )


def _short(text: str, n: int = 300) -> str:
    return text if len(text) <= n else text[: n - 3] + "..."


def _call_text(name: str, args: dict[str, Any]) -> str:
    bits = ", ".join(f"{k}={v}" for k, v in args.items() if v not in (None, [], "", {}))
    return _short(f"{name}({bits})", 200)


def _result_text(name: str, result: Any) -> str:
    if isinstance(result, BrainToolError):
        return _short(f"{name} failed: {result.error_code}: {result.error}", 200)
    if isinstance(result, LocateItemsResult):
        found = ", ".join(f"{f.name or f.sku} @ {f.aisle_label or f.slot_id}" for f in result.found)
        miss = f"; not found: {', '.join(result.not_found)}" if result.not_found else ""
        return _short(f"found {found or 'nothing'}{miss}", 200)
    if isinstance(result, RouteSummary):
        n = sum(1 for s in result.stops if s.kind in ("mandatory", "optional"))
        return f"route with {n} stops, {result.total_distance_m:.1f} m"
    if isinstance(result, RobotState):
        return f"arrived at ({result.pose.x:.2f}, {result.pose.y:.2f})"
    if isinstance(result, VisionObservation):
        return _short(result.answer, 200)
    if isinstance(result, ScanShelfResult):
        bits = [f"{result.observed_count} on the shelf"]
        if not result.in_stock:
            bits.append("OUT OF STOCK")
        if result.mispriced:
            bits.append(f"mispriced (tag {money(result.shelf_tag_cents or 0)})")
        return f"scan {result.slot_id}: " + ", ".join(bits)
    if isinstance(result, PickResult):
        if result.picked and result.instance:
            return f"holding {result.instance.sku} ({result.instance.instance_id})"
        if result.needs_approval:
            return "needs the shopper's OK first (spending rules)"
        return f"not picked: {result.error_code or result.note}"
    if isinstance(result, PlaceResult):
        line = f", cart line {result.line_id}" if result.line_id else ""
        return f"{result.sku} in basket ({result.picked_qty} of this item, {result.basket_count} total){line}"
    if isinstance(result, AskUserResult):
        pick = f" -> {result.choice or ', '.join(result.choices)}" if (result.choice or result.choices) else ""
        return f"answer: {result.decision}{pick}"
    if isinstance(result, GoalItem):
        return f"added {result.qty}x {result.name or result.query} ({result.slot_id})"
    if isinstance(result, Goal):
        return f"goal {result.status.value}: {result.progress_note or result.summary or ''}".strip()
    return name
