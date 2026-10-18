"""Purchase-history database + smart suggestions (judges' demo): seeded months of history, appended checkouts with
photos, replenish / complement / substitute / deal cards (only in-stock items), LLM re-rank with grounding checks."""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from arms.catalog import create_fake as catalog_fake
from arms.recommender.fake import FakeRecommenderService
from arms.recommender.purchases import PurchaseStore, seed_days
from arms.recommender.suggest import grounded, llm_rerank
from contracts.common import utcnow
from contracts.profile import PurchaseRecord
from contracts.services import Services


def _rec(**kw: Any) -> FakeRecommenderService:
    s = Services()
    s.catalog = catalog_fake(s)
    return FakeRecommenderService(s, purchases_db=":memory:", suggestions_llm=False, **kw)


def test_seed_is_relative_to_now_and_persists_checkouts(tmp_path: Any) -> None:
    store = PurchaseStore(str(tmp_path / "p.sqlite"))

    async def go() -> None:
        rows = await store.history("maya")
        salsa = [r for r in rows if r.sku == "SALSA-MILD"]
        assert len(salsa) >= 10 and all(r.source == "seed" for r in salsa)  # months of history
        last = max(salsa, key=lambda r: r.ts)
        assert (utcnow() - last.ts).days == 12  # 'You bought this 12 days ago' stays true
        assert {"maya", "sam", "jordan"} <= set(await store.users())
        rec = PurchaseRecord(
            purchase_id="pur-x",
            user_id="maya",
            sku="LIMES",
            qty=4,
            unit_price_cents=45,
            trip_id="trip-x",
            name="Limes",
            store_id="store_ave",
        )
        await store.add([rec])
        assert await store.add_photos("trip-x", "LIMES", ["/api/evidence/f1.jpg"]) == 1
        newest = (await store.history("maya", limit=1, newest_first=True))[0]
        assert newest.purchase_id == "pur-x" and newest.photo_urls == ["/api/evidence/f1.jpg"]
        assert newest.source == "checkout"

    asyncio.run(go())
    again = PurchaseStore(str(tmp_path / "p.sqlite"))  # a second process sees the same rows, no re-seed
    rows = asyncio.run(again.history("maya"))
    assert any(r.purchase_id == "pur-x" for r in rows)


def test_seed_days_with_anchor() -> None:
    from arms.recommender.purchases import SeedUser

    u = SeedUser.model_validate(
        {"months": 2, "items": {"A": {"every": 10, "last": 2}, "B": {"with": "A", "with_every": 2, "with_skip": 1}}}
    )
    days = seed_days(u)
    assert days["A"] == [2, 12, 22, 32, 42, 52] and days["B"] == [12, 32, 52]


def test_maya_cards_cover_replenish_and_complement() -> None:
    cards = asyncio.run(_rec().replenish_suggestions("maya", "store_ave", 10))
    by = {c.sku: c for c in cards}
    salsa = by["SALSA-MILD"]
    assert cards[0].sku == "SALSA-MILD" and salsa.kind == "replenish"
    assert salsa.reason == "You bought this 12 days ago — you usually buy it every 10 days."
    assert salsa.last_bought_days == 12 and salsa.cadence_days == 10 and salsa.overdue_days == 2
    assert salsa.in_stock and salsa.aisle_label and salsa.image_url == "/api/products/SALSA-MILD/image.jpg"
    chips = by["CHIPS-TORTILLA"]
    assert chips.kind == "complement" and chips.related_to == "SALSA-MILD" and "Mild Chunky Salsa" in chips.reason
    assert all(c.in_stock for c in cards) and all(c.ranked_by == "rules" for c in cards)
    assert "HOTSAUCE-RED" not in by  # not due
    assert [c.score for c in cards] == sorted((c.score for c in cards), reverse=True)


def test_out_of_stock_due_item_becomes_a_substitute() -> None:
    cards = asyncio.run(_rec().replenish_suggestions("sam", "store_ave", 10))
    sub = next(c for c in cards if c.kind == "substitute")
    assert sub.substitute_for == "PRETZELS" and sub.in_stock and "out of stock" in sub.reason
    assert all(c.sku != "PRETZELS" for c in cards)


def test_checkout_moves_the_clock() -> None:
    rec = _rec()
    rec_now = utcnow()
    asyncio.run(
        rec.record_purchase(
            [
                PurchaseRecord(
                    purchase_id="p1",
                    user_id="maya",
                    sku="SALSA-MILD",
                    qty=1,
                    unit_price_cents=349,
                    ts=rec_now,
                    store_id="store_ave",
                )
            ]
        )
    )
    cards = asyncio.run(rec.replenish_suggestions("maya", "store_ave", 10))
    assert "SALSA-MILD" not in {c.sku for c in cards}  # just bought: not due any more
    rows = asyncio.run(rec.purchase_history("maya", limit=1))
    assert rows[0].purchase_id == "p1" and rows[0].name  # the name was filled from the catalog


def test_unknown_user() -> None:
    with pytest.raises(KeyError):
        asyncio.run(_rec().replenish_suggestions("nobody", "store_ave"))


class FakeLlm:
    provider = "gemini"

    def __init__(self, payload: Any = None, exc: Exception | None = None) -> None:
        self.payload, self.exc = payload, exc
        self.messages = self

    async def create(self, **kw: Any) -> Any:
        if self.exc is not None:
            raise self.exc
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


def test_llm_rerank_is_grounded_and_falls_back() -> None:
    cards = asyncio.run(_rec().replenish_suggestions("maya", "store_ave", 4))
    a, b = cards[0], cards[1]
    payload = {
        "ranked": [
            {"suggestion_id": b.suggestion_id, "reason": "Pairs with your salsa — grab it while it's here."},
            {"suggestion_id": a.suggestion_id, "reason": "You're 9 days late on this!"},  # 9 is not in the signals
            {"suggestion_id": "made-up", "reason": "x"},
        ]
    }
    out = asyncio.run(llm_rerank(cards, client=FakeLlm(payload)))
    assert [c.suggestion_id for c in out[:2]] == [b.suggestion_id, a.suggestion_id]
    assert out[0].ranked_by == "llm" and out[0].reason.startswith("Pairs with")
    assert out[1].ranked_by == "llm" and out[1].reason == a.reason  # ungrounded number -> template kept
    assert len(out) == len(cards) and {c.suggestion_id for c in out} == {c.suggestion_id for c in cards}
    same = asyncio.run(llm_rerank(cards, client=FakeLlm(exc=RuntimeError("503 overloaded"))))
    assert same == cards  # LLM down: rule order, templated reasons
    assert grounded("You bought this 12 days ago.", a) == ("12" in str(a.signals.values()))


def test_llm_rerank_is_cached_per_candidate_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Home page asks on every visit; one Gemini call per candidate set (TTL) keeps it under the app's timeout."""
    from arms.recommender import suggest
    from utils import llm

    cards = asyncio.run(_rec().replenish_suggestions("maya", "store_ave", 3))
    calls: list[str] = []

    async def complete_json(prompt: str, schema: Any, **kw: Any) -> Any:
        calls.append(prompt)
        ranked = [{"suggestion_id": c.suggestion_id, "reason": "Time to restock."} for c in reversed(cards)]
        return schema.model_validate({"ranked": ranked})

    monkeypatch.setattr(llm, "is_configured", lambda: True)
    monkeypatch.setattr(llm, "complete_json", complete_json)
    monkeypatch.setattr(suggest, "_RERANK_CACHE", {})
    one = asyncio.run(llm_rerank(cards))
    two = asyncio.run(llm_rerank(cards))
    assert len(calls) == 1 and one == two and one[0].suggestion_id == cards[-1].suggestion_id
    assert all(c.ranked_by == "llm" and c.reason == "Time to restock." for c in two)
    monkeypatch.setattr(suggest, "RERANK_TTL_S", 0.0)
    asyncio.run(llm_rerank(cards))
    assert len(calls) == 2  # expired -> asked again


def test_old_purchase_is_not_due(tmp_path: Any) -> None:
    rec = FakeRecommenderService(purchases_db=str(tmp_path / "q.sqlite"), suggestions_llm=False)
    asyncio.run(rec.purchase_store().ensure(utcnow() - timedelta(days=0)))
    assert asyncio.run(rec.purchase_history("jordan"))
