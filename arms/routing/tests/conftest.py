"""Synthetic store + demo-scenario request (self-contained: data/ may not exist yet)."""

import math

import pytest

from contracts.common import Point3, Pose2D, Rect2D
from contracts.planogram import Fixture, Planogram, Slot
from contracts.routing import RouteRequest, Stop
from contracts.services import Services

# (x_min of shelf, face) -> aisle center x. Shelves 1 m deep, 1.5 m aisles between them.
SHELVES = {"A": 2.5, "B": 5.0, "C": 7.5, "D": 10.0}
AISLE_X = {("A", "east"): 4.25, ("B", "west"): 4.25, ("B", "east"): 6.75, ("C", "west"): 6.75, ("C", "east"): 9.25}

# slot_id, sku, shelf, face, y
SLOTS = [
    ("s_tortillas", "tortillas", "A", "east", 6.0),
    ("s_beef", "ground_beef", "B", "east", 14.0),
    ("s_beans", "black_beans", "A", "east", 10.0),
    ("s_cheese", "cheese", "C", "east", 6.0),
    ("s_salsa", "salsa_mild", "B", "west", 12.0),
    ("s_avocado", "avocado", "C", "west", 15.0),
    ("s_lime", "lime", "C", "west", 8.0),
    ("s_onion", "onion", "B", "east", 5.0),
    ("s_cilantro", "cilantro", "C", "east", 12.0),
    ("s_sour", "sour_cream", "A", "east", 14.0),
    ("s_hot", "hot_sauce", "B", "west", 7.0),
    ("s_guac", "guac_kit", "C", "east", 9.0),
    ("s_chips", "chips", "B", "east", 10.0),
    ("s_cookies", "cookies", "A", "east", 4.5),
]
MANDATORY = [s[0] for s in SLOTS[:10]]
OPTIONAL_INTEREST = {"s_hot": 0.9, "s_guac": 0.7, "s_chips": 0.6, "s_cookies": 0.3}


def make_store() -> Planogram:
    fixtures = [
        Fixture(fixture_id=f"shelf_{k}", rect=Rect2D(x=x, y=4.0, w=1.0, h=12.0), double_sided=True, levels=3)
        for k, x in SHELVES.items()
    ]
    slots = []
    for slot_id, sku, shelf, face, y in SLOTS:
        x0 = SHELVES[shelf]
        face_x = x0 + 1.0 if face == "east" else x0
        ax = AISLE_X[(shelf, face)]
        theta = math.pi if face == "east" else 0.0
        slots.append(
            Slot(
                slot_id=slot_id,
                fixture_id=f"shelf_{shelf}",
                sku=sku,
                level=1,
                item_position=Point3(x=face_x, y=y, z=0.8),
                approach_pose=Pose2D(x=ax, y=y, theta=theta),
                qty=6,
            )
        )
    return Planogram(
        store_id="store_test",
        name="Test Store",
        bounds=Rect2D(x=0.0, y=0.0, w=14.0, h=22.0),
        entrance=Pose2D(x=2.0, y=1.5, theta=math.pi / 2),
        checkout=Pose2D(x=12.0, y=1.5, theta=-math.pi / 2),
        obstacles=[Rect2D(x=12.5, y=18.0, w=0.5, h=0.5)],
        fixtures=fixtures,
        slots=slots,
    )


def make_request(plan: Planogram, max_optional_stops: int | None = 3, **kw) -> RouteRequest:
    stops = []
    for slot in plan.slots:
        optional = slot.slot_id in OPTIONAL_INTEREST
        stops.append(
            Stop(
                stop_id=f"stop_{slot.slot_id}",
                kind="optional" if optional else "mandatory",
                pose=slot.approach_pose,
                slot_id=slot.slot_id,
                skus=[slot.sku],
                qty=1,
                item_ids=[] if optional else [f"item_{slot.sku}"],
                interest=OPTIONAL_INTEREST.get(slot.slot_id, 1.0),
                rec_id=f"rec_{slot.sku}" if optional else None,
                service_time_s=10.0,
            )
        )
    return RouteRequest(
        store_id=plan.store_id,
        start=plan.entrance,
        stops=stops,
        max_optional_stops=max_optional_stops,
        user_ids=["maya"],
        **kw,
    )


class StubCatalog:
    def __init__(self, plan: Planogram) -> None:
        self.plan = plan
        self.calls = 0

    async def get_planogram(self, store_id: str) -> Planogram:
        self.calls += 1
        if store_id != self.plan.store_id:
            raise KeyError(store_id)
        return self.plan.model_copy(deep=True)


@pytest.fixture
def store() -> Planogram:
    return make_store()


@pytest.fixture
def services(store: Planogram) -> Services:
    return Services(catalog=StubCatalog(store))  # type: ignore[arg-type]
