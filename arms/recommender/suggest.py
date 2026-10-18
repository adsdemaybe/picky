"""Smart suggestions from the purchase-history database (judges' demo, the Cart Whisperer generators).

Deterministic candidates (code decides WHAT can be suggested; only items in stock at the chosen store):

    replenish   per-SKU cadence from the shopper's own history: due (overdue >= -window) and in stock here
                "You bought this 12 days ago — you usually buy it every 10 days."
    substitute  a due SKU that is OUT of stock here -> an in-stock product of the same subcategory (bought before
                first, then the closest price)  "Mild Chunky Salsa is out here — Medium Salsa is in stock (aisle 3)."
    complement  bought in the same trip as a due SKU in >= 2 trips, not due itself
                "You usually buy this with Corn Tortillas (4 of your last trips)."
    deal        on deal in THIS store and bought before (or matching the profile's likes)
                "25% off here today — you've bought it 6 times."

Score (deterministic, the order without an LLM): replenish 1 + overdue / cadence (clipped), substitute = the due
item's score - 0.05, complement 0.55 + 0.05 x co-purchases, deal 0.5 + percent_off / 100 (+0.1 bought before).

Optional LLM re-rank (``utils.llm``, kind 'fast'; model fallback on 503 / 429 is inside utils.llm): it gets ONLY the
candidates and their ``signals`` and returns an order + one-line reasons. Code validates it: unknown ids dropped,
missing ones appended in rule order, a reason quoting a number that is not in its signals is replaced by the
template (grounding check). No key / disabled / any error -> rule order + templated reasons (``ranked_by='rules'``).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from arms.recommender.replenish import Cadence, _days, cadence_of, reason_for, shelf_label
from contracts.catalog import Deal, Product, StoreOffer
from contracts.common import Sku, StoreId, UserId, money
from contracts.profile import PurchaseRecord, ReplenishSuggestion, SuggestionKind, UserProfile
from utils.log import get_logger

log = get_logger(__name__)

DUE_WINDOW_DAYS = 3
MIN_CO_PURCHASES = 2
WITH_SHARE = 0.8
"""A due SKU bought on an anchor's trips at least this share of the time is shown as the anchor's complement."""
GENERIC_TAGS = frozenset({"vegetarian", "vegan", "gluten_free", "kid_friendly", "fresh", "organic", "dairy_free"})
"""Diet / generic tags never justify a deal card on their own (every shopper 'likes' half the store)."""
LLM_TIMEOUT_S = 8.0
RERANK_TTL_S = 3600.0
"""The LLM re-rank of one candidate set is reused this long: the Home page asks on every visit and a Gemini call takes
5-8 s, longer than the web app waits. A new purchase changes the candidates (new key) -> a fresh call."""
_RERANK_CACHE: dict[str, tuple[float, list[tuple[str, str | None]]]] = {}
KIND_PREFIX: dict[str, str] = {"replenish": "rep", "substitute": "sub", "complement": "cmp", "deal": "deal"}

ProductFn = Callable[[Sku], Awaitable[Product | None]]
OfferFn = Callable[[Sku], Awaitable[StoreOffer | None]]


class _Ranked(BaseModel):
    suggestion_id: str = Field(description="Copied exactly from the candidate list.")
    reason: str = Field(description="One short sentence for the shopper, using ONLY that candidate's signals.")


class _Rerank(BaseModel):
    ranked: list[_Ranked] = Field(description="Most useful first; every candidate at most once.")


def _deal_text(deal: Deal | None) -> str | None:
    if deal is None:
        return None
    if deal.percent_off:
        return f"{deal.percent_off}% off"
    if deal.original_cents:
        return f"was {money(deal.original_cents)}"
    return deal.reason or deal.kind


def co_purchases(history: Sequence[PurchaseRecord]) -> dict[Sku, Counter[Sku]]:
    """sku -> Counter(other sku -> trips where both were bought)."""
    trips: dict[str, set[Sku]] = defaultdict(set)
    for r in history:
        trips[r.trip_id or f"{r.user_id}:{r.ts.date().isoformat()}"].add(r.sku)
    out: dict[Sku, Counter[Sku]] = defaultdict(Counter)
    for skus in trips.values():
        for a in skus:
            for b in skus:
                if a != b:
                    out[a][b] += 1
    return out


def cadences(history: Sequence[PurchaseRecord], now: datetime) -> dict[Sku, Cadence]:
    buys: dict[Sku, list[tuple[datetime, int]]] = defaultdict(list)
    for r in history:
        buys[r.sku].append((min(r.ts, now), r.qty))
    return {sku: c for sku, rows in buys.items() if (c := cadence_of(sku, rows, now)) is not None}


def last_bought(history: Sequence[PurchaseRecord], now: datetime) -> dict[Sku, tuple[datetime, int, int]]:
    """sku -> (last ts, whole days ago, times bought)."""
    out: dict[Sku, tuple[datetime, int, int]] = {}
    for r in sorted(history, key=lambda x: x.ts):
        times = out[r.sku][2] + 1 if r.sku in out else 1
        out[r.sku] = (r.ts, max(0, int((now - r.ts).total_seconds() // 86400)), times)
    return out


class SuggestionEngine:
    """Builds the cards for one request. ``alternatives(sku)`` lists in-store candidates for a substitute."""

    def __init__(
        self,
        *,
        product: ProductFn,
        offer: OfferFn,
        deals: Callable[[], Awaitable[list[Deal]]],
        alternatives: Callable[[Product], Awaitable[list[Product]]],
    ) -> None:
        self.product, self.offer, self.deals, self.alternatives = product, offer, deals, alternatives

    async def candidates(
        self,
        user_id: UserId,
        history: Sequence[PurchaseRecord],
        *,
        store_id: StoreId,
        now: datetime,
        profile: UserProfile | None = None,
        include_all: bool = False,
    ) -> list[ReplenishSuggestion]:
        cads = cadences(history, now)
        seen = last_bought(history, now)
        co = co_purchases(history)
        cards: dict[Sku, ReplenishSuggestion] = {}

        due = sorted(
            (c for c in cads.values() if include_all or c.overdue_days >= -DUE_WINDOW_DAYS),
            key=lambda c: (-c.overdue_days, -c.urgency, c.sku),
        )
        due_skus = {c.sku for c in due}
        scores = {c.sku: 1.0 + max(-0.5, min(1.0, c.urgency)) for c in due}
        anchor_of: dict[Sku, Sku] = {}
        for c in due:  # bought WITH another due item (>= 80% of its trips): shown as that item's complement
            for a, n in co.get(c.sku, Counter()).most_common():
                if a in due_skus and a != c.sku and scores[a] >= scores[c.sku] and n >= MIN_CO_PURCHASES:
                    if n / max(1, c.times) >= WITH_SHARE and a not in anchor_of:
                        anchor_of[c.sku] = a
                        break
        for c in due:
            product = await self.product(c.sku)
            offer = await self.offer(c.sku)
            score = scores[c.sku]
            if offer is not None and offer.qty > 0 and c.sku in anchor_of:
                a = anchor_of[c.sku]
                anchor = await self.product(a)
                n = co[c.sku][a]
                card = self._card(user_id, "complement", c.sku, product, offer, store_id, scores[a] - 0.01, c, seen)
                card.related_to = a
                card.signals.update({"bought_together_trips": n, "with": anchor.name if anchor else a})
                card.reason = f"You usually buy this with {anchor.name if anchor else a} ({n} of your trips)."
                if card.deal_text:
                    card.reason = card.reason[:-1] + f" — {card.deal_text} here today."
                cards[c.sku] = card
                continue
            if offer is not None and offer.qty > 0:
                cards[c.sku] = self._card(user_id, "replenish", c.sku, product, offer, store_id, score, c, seen)
                cards[c.sku].reason = reason_for(c)
                if cards[c.sku].deal_text:
                    cards[c.sku].reason = cards[c.sku].reason[:-1] + f" — {cards[c.sku].deal_text} here today."
                continue
            if product is None:
                continue
            alt = await self._substitute(product, seen)
            if alt is None or alt[0].sku in cards:
                continue
            p2, o2 = alt
            card = self._card(user_id, "substitute", p2.sku, p2, o2, store_id, score - 0.05, cads.get(p2.sku), seen)
            card.substitute_for = c.sku
            card.signals.update(
                {
                    "substitute_for": product.name,
                    "out_of_stock_here": True,
                    "due_last_bought_days": c.last_days,
                    "due_cadence_days": c.cadence_days,
                }
            )
            card.reason = f"{product.name} is out of stock here — {p2.name} is the closest match" + (
                f" ({card.aisle_label})." if card.aisle_label else "."
            )
            cards[p2.sku] = card

        for c in due:  # frequently bought together with a due item
            for other, n in co.get(c.sku, Counter()).most_common():
                if n < MIN_CO_PURCHASES or other in cards or other in due_skus:
                    continue
                recent = seen.get(other)
                oc = cads.get(other)
                if recent is not None and (recent[1] < 3 or (oc is not None and recent[1] < oc.cadence_days / 2)):
                    continue  # bought recently: not needed again yet
                offer = await self.offer(other)
                if offer is None or offer.qty <= 0:
                    continue
                product = await self.product(other)
                anchor = await self.product(c.sku)
                card = self._card(user_id, "complement", other, product, offer, store_id, 0.55 + 0.05 * n, None, seen)
                card.related_to = c.sku
                card.signals.update({"bought_together_trips": n, "with": anchor.name if anchor else c.sku})
                card.reason = f"You usually buy this with {anchor.name if anchor else c.sku} ({n} of your trips)."
                if card.deal_text:
                    card.reason = card.reason[:-1] + f" — {card.deal_text} here today."
                cards[other] = card

        prefs: dict[str, float] = dict(profile.prefs) if profile is not None else {}
        likes = {str(k).lower() for k, w in prefs.items() if w > 0}
        try:
            deals = await self.deals()
        except Exception as exc:  # noqa: BLE001 - no deals: the other generators still answer
            log.debug("deals for %s failed: %s", store_id, exc)
            deals = []
        for d in sorted(deals, key=lambda x: (-(x.percent_off or 0), x.sku)):
            if d.sku in cards:
                continue
            product = await self.product(d.sku)
            if product is None:
                continue
            bought = seen.get(d.sku)
            if bought is not None and bought[1] < 3:
                continue  # bought recently (same rule as the complement loop): a deal on it is not news yet
            tags = {t.lower() for t in product.tags} | {product.category.lower(), (product.subcategory or "").lower()}
            liked = sorted((likes & tags) - GENERIC_TAGS)
            if bought is None and not liked:
                continue
            offer = await self.offer(d.sku)
            if offer is None or offer.qty <= 0:
                continue
            score = 0.5 + (d.percent_off or 0) / 100 + (0.1 if bought else 0.0)
            card = self._card(user_id, "deal", d.sku, product, offer, store_id, score, cads.get(d.sku), seen)
            card.signals.update({"deal": d.reason or d.kind, "percent_off": d.percent_off or 0})
            if liked:
                card.signals["matches_likes"] = ", ".join(liked)
            why = f"you've bought it {_days(bought[2]).replace('day', 'time')}" if bought else f"you like {liked[0]}"
            card.reason = f"{card.deal_text or 'On deal'} here today — {why}."
            cards[d.sku] = card
        return sorted(cards.values(), key=lambda c: (-c.score, c.sku))

    async def _substitute(
        self, product: Product, seen: dict[Sku, tuple[datetime, int, int]]
    ) -> tuple[Product, StoreOffer] | None:
        best: tuple[tuple[int, int, str], Product, StoreOffer] | None = None
        for alt in await self.alternatives(product):
            if alt.sku == product.sku:
                continue
            offer = await self.offer(alt.sku)
            if offer is None or offer.qty <= 0:
                continue
            key = (0 if alt.sku in seen else 1, abs(offer.price_cents - product.price_cents), alt.sku)
            if best is None or key < best[0]:
                best = (key, alt, offer)
        return (best[1], best[2]) if best else None

    @staticmethod
    def _card(
        user_id: UserId,
        kind: SuggestionKind,
        sku: Sku,
        product: Product | None,
        offer: StoreOffer | None,
        store_id: StoreId,
        score: float,
        cad: Cadence | None,
        seen: dict[Sku, tuple[datetime, int, int]],
    ) -> ReplenishSuggestion:
        aisle, slot_id, level = shelf_label(offer)
        price = offer.price_cents if offer is not None else (product.price_cents if product is not None else 0)
        bought = seen.get(sku)
        signals: dict[str, Any] = {"in_stock_here": bool(offer is not None and offer.qty > 0), "price": money(price)}
        if bought is not None:
            signals.update({"last_bought_days": bought[1], "times_bought": bought[2]})
        if cad is not None:
            signals.update({"cadence_days": cad.cadence_days, "overdue_days": cad.overdue_days})
        deal = offer.deal if offer is not None else None
        if deal is not None:
            signals["deal_text"] = _deal_text(deal) or ""
        return ReplenishSuggestion(
            suggestion_id=f"{KIND_PREFIX[kind]}-{user_id}-{sku}",
            kind=kind,
            user_id=user_id,
            sku=sku,
            name=product.name if product is not None else sku,
            brand=product.brand if product is not None else None,
            size=product.size if product is not None else None,
            category=product.category if product is not None else None,
            image_url=f"/api/products/{sku}/image.jpg",
            label_color=product.asset.label_color if product is not None else None,
            price_cents=price,
            store_id=store_id,
            in_stock=bool(offer is not None and offer.qty > 0),
            stock_qty=offer.qty if offer is not None else 0,
            aisle_label=aisle,
            slot_id=slot_id,
            shelf_level=level,
            last_bought_at=bought[0] if bought else None,
            last_bought_days=bought[1] if bought else None,
            cadence_days=cad.cadence_days if cad else None,
            overdue_days=cad.overdue_days if cad else None,
            times_bought=bought[2] if bought else 0,
            suggested_qty=cad.usual_qty if cad else 1,
            reason="",
            signals=signals,
            score=round(score, 4),
            deal_text=_deal_text(deal),
        )


_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def grounded(reason: str, card: ReplenishSuggestion) -> bool:
    """Every number in an LLM reason must appear in the card's signals / name / price (no invented facts)."""
    allowed = (
        " ".join(str(v) for v in card.signals.values()) + f" {card.name} {card.size or ''} {money(card.price_cents)}"
    )
    have = set(_NUM_RE.findall(allowed))
    return all(n in have for n in _NUM_RE.findall(reason)) and 0 < len(reason) <= 160


async def llm_rerank(cards: list[ReplenishSuggestion], *, client: Any = None) -> list[ReplenishSuggestion]:
    """Re-rank + rewrite reasons with the LLM (grounded in the signals only); any failure -> ``cards`` unchanged."""
    if len(cards) < 2:
        return cards
    from utils import llm

    if client is None and not llm.is_configured():
        return cards
    lines = [{"suggestion_id": c.suggestion_id, "kind": c.kind, "name": c.name, "signals": c.signals} for c in cards]
    key = json.dumps(lines, sort_keys=True, default=str) if client is None else None
    hit = _RERANK_CACHE.get(key) if key is not None else None
    if hit is not None and time.monotonic() - hit[0] < RERANK_TTL_S:
        return _apply_rerank(cards, hit[1])
    prompt = (
        "You rank grocery suggestion cards for one shopper. Use ONLY the facts in each card's signals; never invent "
        "numbers, dates, prices or reasons. Put the most useful first (overdue staples, then substitutes for "
        "out-of-stock staples, then things bought together, then deals). For each card write one short, warm "
        "sentence (<= 120 characters) the shopper sees.\nCandidates:\n" + "\n".join(map(str, lines))
    )
    try:
        out = await asyncio.wait_for(
            llm.complete_json(prompt, _Rerank, kind="fast", max_tokens=1200, client=client), LLM_TIMEOUT_S
        )
    except Exception as exc:  # noqa: BLE001 - LLM down / quota / malformed: the rule order stands
        log.info("suggestion re-rank unavailable (%s): rule order", type(exc).__name__)
        return cards
    order = [(r.suggestion_id, r.reason.strip()) for r in out.ranked]
    if key is not None:
        if len(_RERANK_CACHE) > 64:
            _RERANK_CACHE.clear()
        _RERANK_CACHE[key] = (time.monotonic(), order)
    return _apply_rerank(cards, order)


def _apply_rerank(cards: list[ReplenishSuggestion], order: list[tuple[str, str | None]]) -> list[ReplenishSuggestion]:
    """Apply the LLM's order + reasons; a reason quoting a number not in the card's facts keeps its template."""
    by_id = {c.suggestion_id: c for c in cards}
    ranked: list[ReplenishSuggestion] = []
    for sid, reason in order:
        card = by_id.pop(sid, None)
        if card is None:
            continue
        update: dict[str, Any] = {"ranked_by": "llm"}
        if reason and grounded(reason, card):
            update["reason"] = reason
        ranked.append(card.model_copy(update=update))
    ranked += [c for c in cards if c.suggestion_id in by_id]  # the ones the LLM forgot keep their rule order
    return ranked


__all__ = ["SuggestionEngine", "cadences", "co_purchases", "grounded", "last_bought", "llm_rerank"]
