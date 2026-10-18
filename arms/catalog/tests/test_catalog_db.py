"""Real catalog against a live Postgres: parity with the fake on the same data/ + every write path.

Skipped unless PICKY_TEST_DATABASE_URL points at a disposable database (the test RE-SEEDS it) and the catalog extra
is installed. No Docker needed: the pip ``pgserver`` package ships Postgres 16 + pgvector binaries that run as a
normal user (arms/catalog/README.md "How to run"), e.g.
``PICKY_TEST_DATABASE_URL=postgresql+psycopg://picky@localhost:5433/picky uv run pytest arms/catalog``.
"""

import os
import subprocess
import sys

import pytest

from arms.catalog.fake import FakeCatalogService
from contracts.catalog import RestockRequest, SearchQuery, StockAdjustment
from contracts.perception import ScanReport
from contracts.services import Services

URL = os.environ.get("PICKY_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="PICKY_TEST_DATABASE_URL not set (needs a disposable Postgres)")


@pytest.fixture(scope="module")
def seeded():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("psycopg")
    env = {**os.environ, "DATABASE_URL": URL}
    subprocess.run([sys.executable, "-m", "arms.catalog.seed", "--schema", "--apply"], check=True, env=env)
    return URL


async def test_real_catalog_matches_the_fake_and_writes(seeded):
    from arms.catalog.service import PostgresCatalogService

    real, fake = PostgresCatalogService(Services(), database_url=seeded), FakeCatalogService(Services())
    try:
        pr, pf = await real.get_planogram("store_ave"), await fake.get_planogram("store_ave")
        assert [s.model_dump() for s in pr.slots] == [s.model_dump() for s in pf.slots]
        for q in ("salsa", "tortillas", "ground beef", "avocado", "cilantro"):
            hr, hf = await real.search(SearchQuery(text=q, limit=5)), await fake.search(SearchQuery(text=q, limit=5))
            assert [h.model_dump() for h in hr] == [h.model_dump() for h in hf], q
        for sku in ("SALSA-MILD", "CILANTRO", "GUAC-KIT"):
            assert [o.model_dump() for o in await real.get_offers(sku)] == [
                o.model_dump() for o in await fake.get_offers(sku)
            ]
            assert [x.model_dump() for x in await real.get_locations(sku)] == [
                x.model_dump() for x in await fake.get_locations(sku)
            ]
        slot = "AVE-G2-E1-04"
        assert (await real.record_pick("store_ave", slot, "SALSA-MILD", 1, robot_id="r1")).qty_after == 11
        scan = ScanReport(
            scan_id="scan_db_1",
            store_id="store_ave",
            slot_id=slot,
            expected_sku="SALSA-MILD",
            observed_count=9,
            count_is_exact=True,
            robot_id="r1",
        )
        assert (await real.apply_scan(scan)).qty_after == 9
        assert (
            await real.restock("store_ave", RestockRequest(sku="SALSA-MILD", qty=2, actor="staff:t"))
        ).level.qty == 11
        adj = StockAdjustment(sku="SALSA-MILD", delta=-1, reason="damage", actor="staff:t")
        assert (await real.adjust_stock("store_ave", adj)).level.qty == 10
        hist = await real.stock_history("store_ave", sku="SALSA-MILD")
        assert [m.reason for m in hist] == ["damage", "restock", "scan", "pick"]
        assert (await real.get_planogram("store_ave")).slot(slot).qty == 10
        dash = await real.merchant_dashboard("store_ave")
        assert dash.scans_total == 1 and dash.kpis["picks_total"] == 1.0
        assert await real.category_products("pantry", "store_ave")
    finally:
        await real.close()
