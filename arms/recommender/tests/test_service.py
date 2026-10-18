"""Real service: must degrade to the heuristic without extras/keys; embeddings tested only when the extra exists."""

import pytest

from arms.recommender.service import RecommenderService, product_text
from arms.recommender.tests import world as w
from contracts.profile import Feedback
from contracts.services import RecommenderService as RecommenderProtocol
from utils.config import reset_settings, set_settings


@pytest.fixture(autouse=True)
def _no_llm():
    set_settings(anthropic_api_key="")
    yield
    reset_settings()


def make() -> RecommenderService:
    return RecommenderService(
        None, products=w.BY_SKU, planograms={w.STORE: w.planogram()}, users=w.users(), today=w.TODAY
    )


async def test_real_service_degrades_without_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    svc = make()
    assert isinstance(svc, RecommenderProtocol)
    svc._embedder.available = False  # force the no-sentence-transformers path
    recs = await svc.recommend("maya", w.STORE, w.taco_list(), limit=3)
    assert len(recs) == 3 and all(r.reason and r.sponsored is False for r in recs)
    up = await svc.record_feedback(
        Feedback(feedback_id="f", user_id="maya", sku="salsa_mild", rating=3, text="too mild")
    )
    assert up.version_after > up.version_before


def test_product_text() -> None:
    assert product_text(w.BY_SKU["salsa_hot"]).startswith("Habanero Salsa | pantry | taco mexican salsa")


async def test_embeddings_when_installed() -> None:
    pytest.importorskip("sentence_transformers")
    svc = make()
    recs = await svc.recommend("maya", w.STORE, w.taco_list(), limit=3)
    assert any("embedding_similarity" in r.signals for r in recs)
