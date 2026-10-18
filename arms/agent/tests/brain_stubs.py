"""In-memory store world for robot-brain tests: catalog + planogram, robot, perception, cameras, trips, approvals,
fleet. No other arm is imported; behavior is just enough to exercise the brain's tool executor."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from arms.agent.brain_exec import apply_update_locally
from arms.agent.tests.stubs import PRODUCTS, StubCatalog, StubEvents, StubTrips, make_trip
from contracts.api import AddLineResult
from contracts.cameras import VisionObservation
from contracts.catalog import ProductLocation, ScanApplyResult
from contracts.common import Point3, Pose2D, Rect2D
from contracts.fleet import Goal, GoalUpdate, goal_items_from_trip
from contracts.perception import BasketCheck, FreshnessCandidate, PickChoice, ScanReport
from contracts.planogram import Fixture, Planogram, Slot
from contracts.routing import Route, RouteLeg, Stop
from contracts.services import Services
from contracts.shopping import CartLine, ListItem, Recommendation, ShoppingList
from contracts.sim import CameraFrame, ItemInstance, RobotError, RobotState, RobotStatus
from contracts.trust import ApprovalRequest, ApprovalResponse, MandateDecision, MandateReason, MandateVerdict

STORE = "store_ave"
ROBOT = "rover-ave-1"
SHELF_QTY = {p.sku: 6 for p in PRODUCTS} | {"CIL-01": 0}
SHELF_TAG = {"BEAN-01": 199}  # mispriced: system 129


def slot_id(sku: str) -> str:
    return f"AVE-{sku}"


def build_planogram() -> Planogram:
    slots = []
    for n, p in enumerate(PRODUCTS):
        x = 1.0 + n
        slots.append(
            Slot(
                slot_id=slot_id(p.sku),
                fixture_id="F1",
                sku=p.sku,
                level=1,
                index=n,
                item_position=Point3(x=x, y=5.2, z=0.6),
                approach_pose=Pose2D(x=x, y=4.82, theta=1.571),
                capacity=12,
                qty=SHELF_QTY[p.sku],
                shelf_tag_cents=SHELF_TAG.get(p.sku),
            )
        )
    return Planogram(
        store_id=STORE,
        name="Picky Market - The Ave",
        bounds=Rect2D(x=0, y=0, w=14, h=22),
        entrance=Pose2D(x=1.2, y=1.2, theta=1.571),
        checkout=Pose2D(x=11.25, y=2.75, theta=-1.571),
        robot_home=Pose2D(x=2.6, y=1.6, theta=1.571),
        fixtures=[Fixture(fixture_id="F1", rect=Rect2D(x=0.5, y=5.2, w=12.0, h=0.6), yaw=-1.571, levels=2)],
        slots=slots,
    )


class WorldCatalog(StubCatalog):
    def __init__(self) -> None:
        super().__init__()
        self.plan = build_planogram()
        self.scans: list[ScanReport] = []
        self.picks: list[tuple[str, str, str | None]] = []

    async def search(self, query):
        hits = await super().search(query)
        out = []
        for h in hits:
            offers = []
            for o in h.offers:
                if o.store_id == STORE:
                    locs = await self.get_locations(h.product.sku, STORE)
                    o = o.model_copy(update={"location": locs[0] if locs else None, "qty": locs[0].qty if locs else 0})
                offers.append(o)
            out.append(h.model_copy(update={"offers": offers}))
        return out

    async def get_planogram(self, store_id: str) -> Planogram:
        if store_id != STORE:
            raise KeyError(store_id)
        return self.plan

    async def get_locations(self, sku: str, store_id: str | None = None) -> list[ProductLocation]:
        out = []
        for s in self.plan.slots_for(sku):
            out.append(
                ProductLocation(
                    store_id=STORE,
                    sku=sku,
                    slot_id=s.slot_id,
                    aisle_label="Aisle 3",
                    fixture_id=s.fixture_id,
                    level=s.level,
                    index=s.index,
                    item_position=s.item_position,
                    approach_pose=s.approach_pose,
                    qty=s.qty,
                )
            )
        return out

    async def apply_scan(self, report: ScanReport) -> ScanApplyResult:
        self.scans.append(report)
        slot = self.plan.slot(report.slot_id or "")
        product = self.by_sku.get(report.expected_sku or "")
        mis = bool(report.shelf_tag_cents and product and report.shelf_tag_cents != product.price_cents)
        return ScanApplyResult(
            store_id=STORE, slot_id=report.slot_id, qty_after=slot.qty if slot else None, mispricing=mis
        )

    async def record_pick(self, store_id, slot_id, sku, qty=1, trip_id=None, robot_id=None, lot_id=None):
        self.picks.append((slot_id, sku, robot_id))
        slot = self.plan.slot(slot_id)
        if slot is not None:
            slot.qty = max(0, slot.qty - qty)
        return ScanApplyResult(store_id=store_id, slot_id=slot_id)


class StubRobot:
    def __init__(self, catalog: WorldCatalog) -> None:
        self.catalog = catalog
        self.pose = Pose2D(x=2.6, y=1.6, theta=1.571)
        self.basket: list[ItemInstance] = []
        self.holding: ItemInstance | None = None
        self.taken: set[str] = set()
        self.calls: list[tuple[str, Any]] = []
        self.fail: dict[str, list[str]] = {}  # method -> error codes to raise next (FIFO)
        self.status = RobotStatus.IDLE

    def _maybe_fail(self, method: str) -> None:
        codes = self.fail.get(method)
        if codes:
            raise RobotError(codes.pop(0), f"{method} failed (test)")  # type: ignore[arg-type]

    def _state(self) -> RobotState:
        return RobotState(
            robot_id=ROBOT,
            store_id=STORE,
            pose=self.pose,
            status=self.status,
            holding=self.holding.instance_id if self.holding else None,
            basket=[i.instance_id for i in self.basket],
            basket_skus=[i.sku for i in self.basket],
            battery_pct=80.0,
            backend="fake",
        )

    async def state(self, robot_id: str) -> RobotState:
        return self._state()

    async def navigate(self, robot_id: str, waypoints: list[Pose2D]) -> RobotState:
        self.calls.append(("navigate", waypoints[-1]))
        self._maybe_fail("navigate")
        self.pose = waypoints[-1]
        return self._state()

    async def stop(self, robot_id: str) -> RobotState:
        return self._state()

    def _units(self, sid: str) -> list[ItemInstance]:
        slot = self.catalog.plan.slot(sid)
        if slot is None:
            raise RobotError("unknown_slot", sid)
        units = []
        n = 0
        while len(units) < slot.qty:
            iid = f"{sid}#{n:02d}"
            n += 1
            if iid in self.taken:
                continue
            units.append(
                ItemInstance(
                    instance_id=iid,
                    sku=slot.sku,
                    store_id=STORE,
                    slot_id=sid,
                    position=slot.item_position,
                    freshness=round(0.95 - 0.1 * len(units), 2),
                    lot_id=f"lot_{slot.sku}",
                )
            )
        return units

    async def scan(self, robot_id: str, slot_id: str) -> CameraFrame:
        self.calls.append(("scan", slot_id))
        self._maybe_fail("scan")
        return CameraFrame(
            frame_id=f"f_{slot_id}",
            camera_id=f"{ROBOT}.front",
            robot_id=ROBOT,
            store_id=STORE,
            jpeg_b64="AA==",
            w=64,
            h=48,
            slot_id=slot_id,
            instances=self._units(slot_id),
        )

    async def capture(self, robot_id: str, camera: str = "front") -> CameraFrame:
        return CameraFrame(
            frame_id="f_basket",
            camera=camera,  # type: ignore[arg-type]
            camera_id=f"{ROBOT}.{camera}",
            robot_id=ROBOT,
            jpeg_b64="AA==",
            w=64,
            h=48,
            instances=list(self.basket) if camera == "basket" else [],
        )

    async def pick(self, robot_id: str, slot_id: str, sku: str, instance_id: str | None = None) -> ItemInstance:
        self.calls.append(("pick", (slot_id, instance_id)))
        self._maybe_fail("pick")
        units = self._units(slot_id)
        if instance_id is not None:
            units = [u for u in units if u.instance_id == instance_id]
        if not units:
            raise RobotError("no_instance", slot_id)
        self.holding = units[0]
        self.taken.add(units[0].instance_id)
        return units[0]

    async def place_in_basket(self, robot_id: str) -> RobotState:
        self.calls.append(("place", None))
        assert self.holding is not None
        self.basket.append(self.holding.model_copy(update={"in_basket": True}))
        self.holding = None
        return self._state()

    async def unload_basket(self, robot_id: str) -> list[ItemInstance]:
        self.calls.append(("unload", len(self.basket)))
        out, self.basket = self.basket, []
        return out


class StubPerception:
    async def blur_faces(self, frame: CameraFrame) -> CameraFrame:
        return frame.model_copy(update={"blurred": True})

    async def identify(self, frame, store_id, slot_id=None, expected_sku=None) -> ScanReport:
        units = [i for i in frame.instances if i.sku == expected_sku]
        return ScanReport(
            scan_id=f"scan_{slot_id}",
            store_id=store_id,
            slot_id=slot_id,
            expected_sku=expected_sku,
            observed_count=len(units),
            count_is_exact=True,
            shelf_tag_cents=SHELF_TAG.get(expected_sku or ""),
        )

    async def rank_freshness(self, frame, slot_id, sku, k=3) -> PickChoice:
        units = sorted((i for i in frame.instances if i.sku == sku), key=lambda i: -(i.freshness or 0))[:k]
        return PickChoice(
            choice_id=f"ch_{slot_id}",
            slot_id=slot_id,
            sku=sku,
            candidates=[
                FreshnessCandidate(instance_id=u.instance_id, sku=sku, score=u.freshness or 0, rank=n + 1)
                for n, u in enumerate(units)
            ],
        )

    async def verify_basket(self, frame, expected) -> BasketCheck:
        return BasketCheck.compare(expected, [i.sku for i in frame.instances])


class StubCameras:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    async def observe(self, camera_id, question=None, *, goal_id=None) -> VisionObservation:
        self.calls.append((camera_id, question))
        return VisionObservation(observation_id="obs_1", camera_id=camera_id, answer="Shelf with 6 units in view.")


AskPolicy = Callable[[ApprovalRequest], Awaitable[ApprovalResponse] | ApprovalResponse]


def answer(req: ApprovalRequest, decision: str = "approve", choice: str | None = None) -> ApprovalResponse:
    return ApprovalResponse(approval_id=req.approval_id, decision=decision, choice=choice)  # type: ignore[arg-type]


def default_policy(req: ApprovalRequest) -> ApprovalResponse:
    if req.kind.value == "pick_choice":
        return answer(req, "choice", req.options[1].option_id)  # the shopper picks #2
    return answer(req)


class WorldTrips(StubTrips):
    def __init__(self, trip, policy: AskPolicy = default_policy) -> None:
        super().__init__(trip)
        self.policy = policy
        self.asked: list[ApprovalRequest] = []
        self.verdicts: dict[str, MandateVerdict] = {}
        self.replans = 0

    async def set_stop_status(self, trip_id, stop_id, status, reason=""):
        self.calls.append(("stop", (stop_id, status)))
        self.trip.stop_status[stop_id] = status
        return self.trip

    async def check_line(self, trip_id, line: CartLine) -> MandateDecision:
        self.calls.append(("check_line", line))
        verdict = self.verdicts.get(line.sku, MandateVerdict.APPROVE)
        reasons = [MandateReason.ABOVE_ASK_THRESHOLD] if verdict == MandateVerdict.NEEDS_APPROVAL else []
        return MandateDecision(decision_id="d", user_id="maya", verdict=verdict, reasons=reasons, messages=["rule"])

    async def ask(self, trip_id, request: ApprovalRequest) -> ApprovalResponse:
        self.asked.append(request)
        out = self.policy(request)
        if asyncio.iscoroutine(out):
            out = await out
        return out  # type: ignore[return-value]

    async def replan(self, trip_id, include_recommendations=True, max_optional_stops=None) -> Route:
        self.replans += 1
        return self.trip.route  # type: ignore[return-value]

    async def update_list(self, trip_id, shopping_list: ShoppingList):
        self.calls.append(("update_list", shopping_list))
        new = [i for i in shopping_list.items if i.item_id not in {x.item_id for x in self.trip.shopping_list.items}]
        route = self.trip.route
        assert route is not None
        stops = list(route.stops)
        order = list(route.order)
        for li in new:
            sid = f"stop_{li.item_id}"
            slot = slot_id(li.sku or "")
            stops.append(
                Stop(
                    stop_id=sid,
                    kind="mandatory",
                    pose=Pose2D(x=3, y=4.82, theta=1.571),
                    slot_id=slot,
                    skus=[li.sku],
                    qty=li.qty,
                    item_ids=[li.item_id],
                )
            )
            order.insert(len(order) - 1, sid)
        self.trip = self.trip.model_copy(
            update={"shopping_list": shopping_list, "route": route.model_copy(update={"stops": stops, "order": order})}
        )
        return self.trip

    async def add_line(self, trip_id, line: CartLine, approval_id=None) -> AddLineResult:
        if self.verdicts.get(line.sku) == MandateVerdict.NEEDS_APPROVAL and approval_id is None:
            self.calls.append(("add_line_refused", line))
            return AddLineResult(
                added=False,
                decision=MandateDecision(decision_id="d", user_id="maya", verdict=MandateVerdict.NEEDS_APPROVAL),
                cart=self.trip.cart,
            )
        res = await super().add_line(trip_id, line, approval_id)
        self.calls.append(("approval_id", approval_id))
        return res

    def lines(self) -> list[CartLine]:
        return [c[1] for c in self.calls if c[0] == "add_line"]


class StubApprovals:
    def __init__(self, policy: AskPolicy = default_policy) -> None:
        self.policy = policy
        self.requests: list[ApprovalRequest] = []
        self.cancelled: list[str] = []

    async def request(self, request: ApprovalRequest) -> ApprovalResponse:
        self.requests.append(request)
        out = self.policy(request)
        if asyncio.iscoroutine(out):
            out = await out
        return out  # type: ignore[return-value]

    def pending(self, trip_id=None, *, goal_id=None, robot_id=None, store_id=None, user_id=None):
        return []

    async def cancel(self, approval_id: str, reason: str = "") -> bool:
        self.cancelled.append(approval_id)
        return True


class StubFleet:
    def __init__(self) -> None:
        self.goals: dict[str, Goal] = {}
        self.updates: list[GoalUpdate] = []

    def add(self, goal: Goal) -> Goal:
        self.goals[goal.goal_id] = goal
        return goal

    async def update_goal(self, goal_id: str, update: GoalUpdate) -> Goal:
        self.updates.append(update)
        self.goals[goal_id] = apply_update_locally(self.goals[goal_id], update)
        return self.goals[goal_id]

    async def get_goal(self, goal_id: str) -> Goal:
        return self.goals[goal_id]


def demo_trip(**kw: Any):
    """Taco-ish trip: tortillas x2, avocado x2 (produce), cilantro (shelf empty -> online), hot-salsa detour."""
    plan = build_planogram()
    items = [
        ListItem(item_id="li_tort", query="tortillas", sku="TORT-01", qty=2, sourcing="in_store", category="bakery"),
        ListItem(item_id="li_avo", query="avocado", sku="AVO-01", qty=2, sourcing="in_store", category="produce"),
        ListItem(item_id="li_cil", query="cilantro", sku="CIL-01", qty=1, sourcing="in_store", category="produce"),
    ]
    sl = ShoppingList(list_id="list_1", user_id="maya", trip_id="trip_1", budget_cents=4000, items=items)
    rec = Recommendation(
        rec_id="rec_hot",
        user_id="maya",
        trip_id="trip_1",
        sku="SALSA-HOT",
        store_id=STORE,
        product_name="Casa Salsa Diablo",
        interest=0.8,
        reason="you like spicy",
        price_cents=379,
        slot_id=slot_id("SALSA-HOT"),
    )
    start = Stop(stop_id="start", kind="start", pose=plan.robot_home)  # type: ignore[arg-type]
    end = Stop(stop_id="end", kind="end", pose=plan.checkout)
    stops = [start]
    for sid, sku, li, qty in [
        ("s_tort", "TORT-01", "li_tort", 2),
        ("s_avo", "AVO-01", "li_avo", 2),
        ("s_cil", "CIL-01", "li_cil", 1),
    ]:
        slot = plan.slot(slot_id(sku))
        assert slot is not None
        stops.append(
            Stop(
                stop_id=sid,
                kind="mandatory",
                pose=slot.approach_pose,
                slot_id=slot.slot_id,
                skus=[sku],
                qty=qty,
                item_ids=[li],
            )
        )
    hot = plan.slot(slot_id("SALSA-HOT"))
    assert hot is not None
    stops.append(
        Stop(
            stop_id="s_hot",
            kind="optional",
            pose=hot.approach_pose,
            slot_id=hot.slot_id,
            skus=["SALSA-HOT"],
            rec_id="rec_hot",
            interest=0.8,
            detour_m=4.0,
            detour_s=9.0,
        )
    )
    stops.append(end)
    order = ["start", "s_tort", "s_hot", "s_avo", "s_cil", "end"]
    by = {s.stop_id: s for s in stops}
    legs = [
        RouteLeg(from_stop_id=a, to_stop_id=b, waypoints=[by[a].pose, by[b].pose], distance_m=1.0)
        for a, b in zip(order, order[1:], strict=False)
    ]
    route = Route(route_id="route_1", store_id=STORE, stops=stops, order=order, legs=legs, total_distance_m=5.0)
    return make_trip(shopping_list=sl, recs=[rec], route=route, robot_id=ROBOT, **kw)


def shop_goal(trip) -> Goal:
    return Goal(
        goal_id="goal_1",
        kind="shop",
        status="active",
        store_id=STORE,
        robot_id=ROBOT,
        title="Shop tacos",
        trip_id=trip.trip_id,
        user_id="maya",
        request_id="req_1",
        items=goal_items_from_trip(trip),
    )


def world(trip=None, policy: AskPolicy = default_policy, **kw: Any) -> tuple[Services, dict[str, Any]]:
    catalog = WorldCatalog()
    trips = WorldTrips(trip or demo_trip(), policy)
    parts = {
        "catalog": catalog,
        "robot": StubRobot(catalog),
        "perception": StubPerception(),
        "cameras": StubCameras(),
        "trips": trips,
        "approvals": StubApprovals(policy),
        "fleet": StubFleet(),
        "events": StubEvents(),
    }
    parts.update(kw)
    return Services(**parts), parts
