"""Shared fixtures: a tiny self-contained store (no dependency on data/, which is written concurrently) and a stub
catalog serving it."""

import math

import pytest

from contracts.catalog import Lot, Product, SimAsset
from contracts.common import Point3, Pose2D, Rect2D
from contracts.planogram import Fixture, Planogram, Slot
from contracts.services import Services

STORE = "store_test"


def make_planogram() -> Planogram:
    up = math.pi / 2
    shelf = Fixture(
        fixture_id="F1",
        kind="shelf",
        rect=Rect2D(x=2.0, y=4.0, w=6.0, h=1.0),
        yaw=-up,
        levels=1,
        level_z_m=[0.4],
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
                approach_pose=Pose2D(x=3.0, y=3.2, theta=up),
                facings=2,
                qty=4,
            ),
            Slot(
                slot_id="S-avocado",
                fixture_id="F1",
                sku="avocado",
                item_position=Point3(x=5.0, y=4.0, z=0.4),
                approach_pose=Pose2D(x=5.0, y=3.2, theta=up),
                facings=3,
                qty=5,
                lots=[Lot(lot_id="L-avo-a", qty=3, freshness=0.9), Lot(lot_id="L-avo-b", qty=2, freshness=0.55)],
            ),
            Slot(
                slot_id="S-limes",
                fixture_id="F1",
                sku="limes",
                item_position=Point3(x=7.0, y=4.0, z=0.4),
                approach_pose=Pose2D(x=7.0, y=3.2, theta=up),
                qty=0,
            ),
        ],
    )


def make_products() -> dict[str, Product]:
    return {
        "salsa-mild": Product(
            sku="salsa-mild",
            name="Mild Salsa",
            category="pantry",
            price_cents=399,
            barcode="0000000000017",
            asset=SimAsset(shape="cylinder", dims_m=(0.08, 0.08, 0.12)),
        ),
        "avocado": Product(sku="avocado", name="Avocado", category="produce", price_cents=129, barcode="0000000000024"),
        "limes": Product(sku="limes", name="Limes", category="produce", price_cents=99, barcode="0000000000031"),
    }


class StubCatalog:
    """Only what the robot calls: get_planogram + get_product."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def get_planogram(self, store_id: str) -> Planogram:
        self.calls.append(f"get_planogram:{store_id}")
        if store_id != STORE:
            raise KeyError(store_id)
        return make_planogram()

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
