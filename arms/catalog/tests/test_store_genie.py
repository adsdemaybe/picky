"""store_genie (the Genie Sim supermarket as a Picky store): generated data is current, valid and self-consistent."""

import importlib.util
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
import yaml

from arms.catalog.fake import FakeCatalogService
from contracts.catalog import SearchQuery, is_valid_ean13
from contracts.common import slot_reachable
from contracts.services import Services
from utils import data as data_io

ROOT = Path(__file__).resolve().parents[3]
GEN = ROOT / "scripts" / "genie_to_planogram.py"
PRIMS = ROOT / "data" / "sim" / "store_genie_prims.yaml"
STORE = "store_genie"


def _generator():
    spec = importlib.util.spec_from_file_location("genie_to_planogram", GEN)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(spec.name, mod)  # dataclasses look their module up while the script loads
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def plan():
    return data_io.load_planogram(STORE, ROOT / "data")


@pytest.fixture(scope="module")
def prims():
    return yaml.safe_load(PRIMS.read_text(encoding="utf-8"))


def test_generated_files_are_current():
    """data/planograms/store_genie.yaml, data/sim/store_genie_prims.yaml and the products block = generator output."""
    assert _generator().main(["gen", "--check"]) == 0


def test_store_validates_without_problems():
    problems = [p for p in data_io.validate_data(ROOT / "data") if STORE in p or "GEN-" in p or "GN-" in p]
    assert [p for p in problems if not data_io.is_warning(p)] == []


def test_slots_are_reachable_and_skus_known(plan):
    products = data_io.load_products(ROOT / "data")
    assert len(plan.slots) >= 100
    skus = {s.sku for s in plan.slots}
    assert 20 <= len(skus) <= 60
    for s in plan.slots:
        assert s.slot_id.startswith("GEN-")
        assert slot_reachable(s.approach_pose, s.approach_pose, s.item_position), s.slot_id
        p = products[s.sku]
        assert p.sku.startswith("GN-") and is_valid_ean13(p.barcode) and "genie_store" in p.tags
    assert any(f.kind == "checkout" for f in plan.fixtures)
    assert data_io.robots_for_store(STORE, ROOT / "data"), "store_genie needs a robot in data/fixtures/robots.yaml"


def test_prim_map_matches_the_planogram(plan, prims):
    assert prims["store_id"] == STORE
    assert prims["usd"]["path"].endswith(".usda")
    by_slot = prims["slots"]
    assert set(by_slot) == {s.slot_id for s in plan.slots}
    paths = [u["path"] for units in by_slot.values() for u in units]
    assert len(paths) == len(set(paths)), "a product prim is mapped to two units"
    for s in plan.slots:
        units = by_slot[s.slot_id]
        assert len(units) == s.qty  # unit n of the slot = units[n] (instance_id(slot_id, n))
        front = units[0]["c"]
        # the front unit sits right behind the slot's item_position, on its shelf board
        assert abs(front[0] - s.item_position.x) + abs(front[1] - s.item_position.y) < 0.4
        assert front[2] - front[2] / 1000 >= s.item_position.z - 0.05
    in_usd = set(prims["fixtures_in_usd"])
    assert "GEN-COUNTER" not in in_usd  # the sim draws the checkout counter
    assert in_usd <= {f.fixture_id for f in plan.fixtures}


async def test_catalog_finds_genie_products():
    cat = FakeCatalogService(
        Services(), data_root=ROOT / "data", today=date(2026, 10, 17), now=lambda: datetime(2026, 10, 18, tzinfo=UTC)
    )
    hits = await cat.search(SearchQuery(text="chocolate milk", store_ids=[STORE]))
    assert hits and hits[0].product.sku == "GN-CHOCOLATE-MILK"
    assert any(o.store_id == STORE and o.in_stock for o in hits[0].offers)
