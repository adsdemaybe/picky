"""In-memory stubs for fleet tests: a two-store world with three robots, a tiny catalog, a trip controller that
auto-executes like the gateway, and a scriptable robot brain. Base deps only."""

from __future__ import annotations

import asyncio
from typing import Any

from contracts.api import CreateTripRequest
from contracts.catalog import CategoryInfo, Product, ProductLocation, SearchHit, StoreInfo, StoreOffer
from contracts.common import Point3, Pose2D, Rect2D
from contracts.fleet import BrainReply, BrainStatus, Goal, GoalItemUpdate, GoalStatus, GoalUpdate
from contracts.planogram import Aisle, Fixture, Planogram, RobotSpec, Slot
from contracts.routing import Route, Stop
from contracts.services import Services
from contracts.shopping import Cart, Intent, ListItem, ShoppingList
from contracts.sim import RobotError, RobotState, RobotStatus
from contracts.trip import Trip

# sku -> (name, category, subcategory, price ave, qty ave, price hill, qty hill, online?)
PRODUCTS: dict[str, tuple[str, str, str | None, int | None, int, int | None, int, bool]] = {
    "TORT": ("Corn Tortillas", "bakery", "tortillas", 279, 10, None, 0, True),
    "BEEF": ("Ground Beef", "meat", None, 549, 10, None, 0, False),
    "SALSA": ("Mild Salsa", "international", "salsa", 349, 5, None, 0, True),
    "CILANTRO": ("Cilantro", "produce", "herbs", 99, 0, None, 0, True),
    "QUESO": ("Queso Fresco", "dairy", "cheese", None, 0, 449, 4, False),
    "CREMA": ("Mexican Crema", "dairy", None, None, 0, 379, 4, False),
    "MILK": ("Whole Milk", "dairy", "milk", 389, 12, 399, 12, True),
}


def _slots(store: str, prefix: str, col: int) -> list[Slot]:
    out = []
    for n, (sku, row) in enumerate(PRODUCTS.items()):
        price, qty = (row[3], row[4]) if store == "store_ave" else (row[5], row[6])
        if price is None:
            continue
        aisle = "dairy" if row[1] == "dairy" else "a1"
        out.append(
            Slot(
                slot_id=f"{prefix}-{sku}",
                fixture_id="F1",
                aisle_id=aisle,
                sku=sku,
                index=n,
                item_position=Point3(x=2.0 + n * 0.5, y=3.0, z=0.8),
                approach_pose=Pose2D(x=2.0 + n * 0.5, y=2.62, theta=1.571),
                capacity=12,
                qty=qty,
                price_cents=price,
            )
        )
    return out


def planogram(store: str) -> Planogram:
    prefix = "AVE" if store == "store_ave" else "HILL"
    return Planogram(
        store_id=store,
        name="Picky Market - The Ave" if store == "store_ave" else "Picky Express - Capitol Hill",
        bounds=Rect2D(x=0, y=0, w=12, h=8),
        entrance=Pose2D(x=1.0, y=1.0),
        checkout=Pose2D(x=10.0, y=1.0),
        fixtures=[Fixture(fixture_id="F1", aisle_id="a1", rect=Rect2D(x=1.5, y=3.0, w=6, h=0.5))],
        aisles=[
            Aisle(aisle_id="a1", label="Aisle 1", rect=Rect2D(x=1.5, y=2.0, w=6, h=1.0)),
            Aisle(aisle_id="dairy", label="Dairy wall", rect=Rect2D(x=1.5, y=5.0, w=6, h=1.0)),
        ],
        slots=_slots(store, prefix, 0),
    )


class StubCatalog:
    def __init__(self) -> None:
        self.plans = {s: planogram(s) for s in ("store_ave", "store_hill")}

    async def list_stores(self) -> list[StoreInfo]:
        return [
            StoreInfo(store_id="store_ave", name="Picky Market - The Ave"),
            StoreInfo(store_id="store_hill", name="Picky Express - Capitol Hill"),
            StoreInfo(store_id="online", name="Picky Online", kind="online", has_planogram=False),
        ]

    async def get_planogram(self, store_id: str) -> Planogram:
        if store_id not in self.plans:
            raise KeyError(store_id)
        return self.plans[store_id]

    def product(self, sku: str) -> Product:
        row = PRODUCTS[sku]
        return Product(
            sku=sku,
            name=row[0],
            category=row[1],
            subcategory=row[2],
            price_cents=row[3] or 100,
            barcode="0000000000000",
        )

    async def get_product(self, sku: str) -> Product | None:
        return self.product(sku) if sku in PRODUCTS else None

    async def get_offers(self, sku: str, store_ids: list[str] | None = None) -> list[StoreOffer]:
        row = PRODUCTS.get(sku)
        if row is None:
            return []
        out = []
        if row[3] is not None:
            out.append(StoreOffer(store_id="store_ave", sku=sku, price_cents=row[3], qty=row[4]))
        if row[5] is not None:
            out.append(StoreOffer(store_id="store_hill", sku=sku, price_cents=row[5], qty=row[6]))
        if row[7]:
            out.append(StoreOffer(store_id="online", sku=sku, price_cents=(row[3] or row[5] or 100) + 30, qty=50))
        return [o for o in out if store_ids is None or o.store_id in store_ids]

    async def search(self, query: Any) -> list[SearchHit]:
        text = query.text.lower()
        hits = []
        for sku, row in PRODUCTS.items():
            if any(w in row[0].lower() for w in text.split() if len(w) > 2):
                hits.append(SearchHit(product=self.product(sku), offers=await self.get_offers(sku), score=1.0))
        return hits[: query.limit]

    async def get_locations(self, sku: str, store_id: str | None = None) -> list[ProductLocation]:
        out = []
        for sid, plan in self.plans.items():
            if store_id is not None and sid != store_id:
                continue
            for s in plan.slots_for(sku):
                out.append(
                    ProductLocation(
                        store_id=sid,
                        sku=sku,
                        slot_id=s.slot_id,
                        fixture_id=s.fixture_id,
                        level=s.level,
                        index=s.index,
                        item_position=s.item_position,
                        approach_pose=s.approach_pose,
                        qty=s.qty,
                    )
                )
        return out

    async def list_categories(self, store_id: str | None = None) -> list[CategoryInfo]:
        names = {"dairy": "Dairy & Eggs", "produce": "Produce", "meat": "Meat & Seafood", "bakery": "Bakery"}
        names |= {"international": "International & Mexican"}
        return [CategoryInfo(category_id=c, name=n) for c, n in names.items()]


class StubAgent:
    """No interpret_request: the fleet must fall back to expand_intent for the list."""

    def __init__(self, items: list[tuple[str, str, int]], budget: int | None = 4000) -> None:
        self.items = items
        self.budget = budget
        self.calls: list[str] = []

    async def expand_intent(self, intent: Intent, profile: Any = None) -> ShoppingList:
        self.calls.append(intent.text)
        return ShoppingList(
            list_id="list_t",
            user_id=intent.user_id,
            title="Taco night",
            budget_cents=self.budget,
            items=[ListItem(item_id=f"i{n}", query=q, sku=s, qty=qty) for n, (q, s, qty) in enumerate(self.items)],
        )


class StubRobot:
    def __init__(self, specs: dict[str, RobotSpec]) -> None:
        self.states_: dict[str, RobotState] = {
            rid: RobotState(
                robot_id=rid, store_id=s.store_id, pose=s.dock, status=RobotStatus.IDLE, battery_pct=90, backend="fake"
            )
            for rid, s in specs.items()
        }
        self.stopped: list[str] = []
        self.queue: asyncio.Queue[RobotState] = asyncio.Queue()

    def set(self, robot_id: str, **fields: Any) -> None:
        self.states_[robot_id] = self.states_[robot_id].model_copy(update=fields)

    async def state(self, robot_id: str) -> RobotState:
        if robot_id not in self.states_:
            raise RobotError("unknown_robot", robot_id)
        return self.states_[robot_id]

    async def list_robots(self) -> list[RobotState]:
        return list(self.states_.values())

    async def stop(self, robot_id: str) -> RobotState:
        self.stopped.append(robot_id)
        return self.states_[robot_id]

    async def states(self, robot_id: str | None = None):
        while True:
            yield await self.queue.get()


class StubBrain:
    """``behavior[robot_id]``: 'complete' (default), 'raise' (RobotError battery_low), 'block' (until cancelled)."""

    def __init__(self, services: Services) -> None:
        self.services = services
        self.behavior: dict[str, str] = {}
        self.runs: list[tuple[str, str]] = []
        self.cancelled: list[str] = []
        self.started = asyncio.Event()

    async def run_goal(self, robot_id: str, goal: Goal) -> Goal:
        self.runs.append((robot_id, goal.goal_id))
        self.started.set()
        fleet = self.services.fleet
        mode = self.behavior.get(robot_id, "complete")
        if mode == "raise":
            await asyncio.sleep(0)
            raise RobotError("battery_low", f"{robot_id} battery low")
        if mode == "block":
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled.append(goal.goal_id)
                raise
        patches = [GoalItemUpdate(item_id=i.item_id, status="in_basket", picked_qty=i.qty) for i in goal.items]
        await fleet.update_goal(goal.goal_id, GoalUpdate(items=patches, note="picking"))
        return await fleet.update_goal(
            goal.goal_id, GoalUpdate(status=GoalStatus.COMPLETED, summary=f"done by {robot_id}")
        )

    async def handle_message(self, robot_id: str, text: str, user_id: str | None = None) -> BrainReply:
        return BrainReply(robot_id=robot_id, text=f"ack: {text}")

    async def status(self, robot_id: str) -> BrainStatus:
        return BrainStatus(robot_id=robot_id)

    async def recent_steps(self, robot_id=None, goal_id=None, limit=50, before=None):
        return []


class StubTrips:
    """Creates trips, 'plans' a route from the list's in-store items and auto-executes like the gateway."""

    def __init__(self, services: Services, catalog: StubCatalog, auto: bool = True) -> None:
        self.services = services
        self.catalog = catalog
        self.auto = auto
        self.trips: dict[str, Trip] = {}
        self.requests: list[CreateTripRequest] = []
        self.assigned: list[tuple[str, str, str | None]] = []
        self.results: dict[str, Goal] = {}
        self.tasks: list[asyncio.Task[Any]] = []

    async def create_trip(self, req: CreateTripRequest) -> Trip:
        self.requests.append(req)
        tid = f"trip_{len(self.trips) + 1}"
        store = req.store_id or "store_ave"
        plan = self.catalog.plans[store]
        stops = [Stop(stop_id="_start", kind="start", pose=plan.entrance)]
        for item in req.shopping_list.items if req.shopping_list else []:
            slot = next((s for s in plan.slots_for(item.sku or "") if s.qty > 0), None)
            if slot is not None:
                stops.append(
                    Stop(
                        stop_id=f"s_{item.item_id}",
                        kind="mandatory",
                        pose=slot.approach_pose,
                        slot_id=slot.slot_id,
                        skus=[slot.sku],
                        qty=item.qty,
                        item_ids=[item.item_id],
                    )
                )
        stops.append(Stop(stop_id="_end", kind="end", pose=plan.checkout))
        route = Route(route_id=f"r_{tid}", store_id=store, stops=stops, order=[s.stop_id for s in stops])
        trip = Trip(
            trip_id=tid,
            user_id=req.user_id,
            store_id=store,
            shopping_list=req.shopping_list,
            route=route,
            cart=Cart(cart_id=f"c_{tid}", trip_id=tid),
            robot_id=req.robot_id,
            request_id=req.request_id,
            auto_execute=req.auto_execute,
        )
        self.trips[tid] = trip
        if self.auto and req.auto_execute:
            self.tasks.append(asyncio.create_task(self._execute(tid, req.robot_id)))
        return trip

    async def _execute(self, tid: str, robot_id: str | None) -> None:
        await asyncio.sleep(0.01)
        self.results[tid] = await self.services.fleet.execute_trip(tid, robot_id)

    async def get_trip(self, trip_id: str) -> Trip:
        return self.trips[trip_id].model_copy(deep=True)

    async def assign_robot(self, trip_id: str, robot_id: str, goal_id: str | None = None) -> Trip:
        self.assigned.append((trip_id, robot_id, goal_id))
        t = self.trips[trip_id]
        t.robot_id, t.goal_id = robot_id, goal_id
        return t


class StubApprovals:
    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.items: list[Any] = []

    def pending(self, trip_id=None, *, goal_id=None, robot_id=None, store_id=None, user_id=None):
        return [a for a in self.items if goal_id is None or a.goal_id == goal_id]

    async def cancel(self, approval_id: str, reason: str = "") -> bool:
        self.cancelled.append(approval_id)
        return True


class EventLog:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> Any:
        self.events.append(event)
        return event

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, type_: str) -> list[Any]:
        return [e for e in self.events if e.type == type_]


SPECS = {
    "rover-ave-1": RobotSpec(robot_id="rover-ave-1", name="Pico", store_id="store_ave", dock=Pose2D(x=1.0, y=1.5)),
    "rover-ave-2": RobotSpec(robot_id="rover-ave-2", name="Rhea", store_id="store_ave", dock=Pose2D(x=1.8, y=1.5)),
    "rover-hill-1": RobotSpec(robot_id="rover-hill-1", name="Hazel", store_id="store_hill", dock=Pose2D(x=1.0, y=1.5)),
}
