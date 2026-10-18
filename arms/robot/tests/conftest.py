"""Shared fixtures: a tiny self-contained store with two rovers (no dependency on data/, which is written
concurrently) and a stub catalog serving it (mutable, so restock / write-off sync can be tested)."""

import math

import pytest

from contracts.cameras import CameraMount
from contracts.catalog import Lot, Product, SimAsset
from contracts.common import Point3, Pose2D, Rect2D
from contracts.planogram import Fixture, Planogram, RobotSpec, Slot, StoreCameraSpec
from contracts.services import Services

STORE = "store_test"
R1, R2 = "rover-t-1", "rover-t-2"
UP = math.pi / 2


def make_planogram() -> Planogram:
    shelf = Fixture(
        fixture_id="F1",
        kind="shelf",
        label="Test gondola",
        rect=Rect2D(x=2.0, y=4.0, w=6.0, h=1.0),
        yaw=-UP,
        levels=2,
        level_z_m=[0.4, 1.0],
    )
    return Planogram(
        store_id=STORE,
        name="Test Store",
        bounds=Rect2D(x=0.0, y=0.0, w=10.0, h=10.0),
        entrance=Pose2D(x=1.0, y=1.0, theta=0.0),
        checkout=Pose2D(x=9.0, y=1.0, theta=0.0),
        fixtures=[shelf],
        slots=[
            Slot(
                slot_id="S-salsa",
                fixture_id="F1",
                sku="salsa-mild",
                item_position=Point3(x=3.0, y=4.0, z=0.4),
                approach_pose=Pose2D(x=3.0, y=3.62, theta=UP),
                facings=2,
                qty=4,
            ),
            Slot(
                slot_id="S-avocado",
                fixture_id="F1",
                sku="avocado",
                item_position=Point3(x=5.0, y=4.0, z=0.4),
                approach_pose=Pose2D(x=5.0, y=3.62, theta=UP),
                facings=3,
                qty=5,
                lots=[Lot(lot_id="L-avo-a", qty=3, freshness=0.9), Lot(lot_id="L-avo-b", qty=2, freshness=0.55)],
            ),
            Slot(
                slot_id="S-limes",
                fixture_id="F1",
                sku="limes",
                item_position=Point3(x=7.0, y=4.0, z=1.0),
                approach_pose=Pose2D(x=7.0, y=3.62, theta=UP),
                level=1,
                qty=0,
            ),
        ],
    )


def make_products() -> dict[str, Product]:
    return {
        "salsa-mild": Product(
            sku="salsa-mild",
            name="Mild Salsa",
            category="international",
            price_cents=399,
            barcode="0000000000017",
            asset=SimAsset(shape="cylinder", dims_m=(0.08, 0.08, 0.12), label_color="#dc2626"),
        ),
        "avocado": Product(
            sku="avocado",
            name="Avocado",
            category="produce",
            price_cents=129,
            barcode="0000000000024",
            asset=SimAsset(shape="sphere", dims_m=(0.08, 0.08, 0.1), label_color="#166534"),
        ),
        "limes": Product(sku="limes", name="Limes", category="produce", price_cents=99, barcode="0000000000031"),
    }


def make_robots() -> list[RobotSpec]:
    return [
        RobotSpec(robot_id=R1, name="Tess", store_id=STORE, dock=Pose2D(x=1.0, y=1.0, theta=0.0), color="#2563eb"),
        RobotSpec(robot_id=R2, name="Uno", store_id=STORE, dock=Pose2D(x=1.0, y=2.2, theta=0.0), battery_pct=80.0),
    ]


def make_cameras() -> list[StoreCameraSpec]:
    return [
        StoreCameraSpec(
            camera_id=f"{STORE}.ceiling-1",
            store_id=STORE,
            name="Test ceiling 1",
            mount=CameraMount(frame="world", x=5.0, y=5.0, z=3.5, pitch=-1.5708, hfov_deg=100.0),
        )
    ]


class StubCatalog:
    """Only what the robot calls: get_planogram + get_product. ``plan`` may be mutated by tests (restocks)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.plan = make_planogram()

    async def get_planogram(self, store_id: str) -> Planogram:
        self.calls.append(f"get_planogram:{store_id}")
        if store_id != STORE:
            raise KeyError(store_id)
        return self.plan.model_copy(deep=True)

    async def get_product(self, sku: str) -> Product | None:
        return make_products().get(sku)


class RecordingPublisher:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, event):  # noqa: ANN001, ANN201
        self.events.append(event)
        return event


@pytest.fixture
def services() -> Services:
    return Services(catalog=StubCatalog(), events=RecordingPublisher())  # type: ignore[arg-type]


def fleet_opts(**kw):  # noqa: ANN003, ANN201
    """FakeRobot options for the test store: instant sim time, two rovers, one ceiling camera."""
    return {
        "realtime_factor": math.inf,
        "robots": make_robots(),
        "store_cameras": make_cameras(),
        "render_in_thread": False,
        **kw,
    }
