"""The fake against the real data/ files (skipped until the data agent has written them)."""

import pytest

from arms.recommender import create_fake
from contracts.common import DEFAULT_STORE_ID
from contracts.services import Services
from contracts.shopping import ListItem, ShoppingList
from utils import data

pytestmark = pytest.mark.skipif(
    not (data.data_dir() / data.USERS_FILE).exists() or not (data.data_dir() / data.PRODUCTS_FILE).exists(),
    reason="data/fixtures/users.yaml or data/products.yaml not written yet",
)


async def test_maya_taco_trip_on_store_ave() -> None:
    svc = create_fake(Services())
    profile = await svc.get_profile("maya")
    queries = ["tortillas", "ground beef", "black beans", "cheese", "salsa", "avocados", "limes", "onion"]
    sl = ShoppingList(
        list_id="list_demo",
        user_id="maya",
        trip_id="trip_demo",
        title="Tacos for 6",
        items=[ListItem(item_id=f"i{n}", query=q) for n, q in enumerate(queries)],
    )
    recs = await svc.recommend(profile.user_id, DEFAULT_STORE_ID, sl, limit=3)
    assert len(recs) <= 3
    for r in recs:
        assert r.reason and r.sponsored is False and r.trip_id == "trip_demo"
