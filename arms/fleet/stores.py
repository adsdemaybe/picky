"""Store choice for shop requests: deterministic coverage + price, with an optional split across stores.

Rule (fake and real fleet):
1. For every list item and physical store: the first candidate SKU (sku guess, then substitutes, else the top catalog
   search hits) with stock >= qty (else any stock) is "in stock" there; its line = qty x that store's unit price.
2. Primary store = the store hint (UI selection / named in the text) if any, else the best store by
   (items in stock desc, est. total of those items asc, robots that can work there desc, store_id); the shopper's
   home store (UserProfile.home_store_id) wins among the best-coverage stores unless another one is more than
   HOME_STORE_MARGIN_PCT cheaper.
3. Split (only without a store hint): items the primary lacks go to the next-best store that has them in stock when
   that store has a robot that can work AND (it covers >= SPLIT_MIN_ITEMS missing items OR one of them cannot be
   bought online). Single items that the online store carries stay on the primary trip (its sourcing re-sources them
   online), so the taco demo keeps its "cilantro OOS -> online" moment.
4. Items no store covers stay on the primary store's list (trip sourcing: online or unresolved).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from contracts.catalog import SearchQuery, StoreOffer
from contracts.common import ONLINE_STORE_ID, ItemId, Sku, StoreId
from contracts.fleet import StoreChoice
from contracts.shopping import ListItem, ShoppingList
from utils.log import get_logger
from utils.money import fmt_cents

log = get_logger(__name__)

HOME_STORE_MARGIN_PCT = 10
"""The home store keeps the list when it covers as many items as the best store and costs at most this much more."""

SPLIT_MIN_ITEMS = 2
"""A secondary store is added when it covers at least this many items the primary store lacks (or any item that the
online store cannot supply)."""


@dataclass
class ItemOffer:
    sku: Sku
    line_cents: int
    full_qty: bool


@dataclass
class StoreEval:
    store_id: StoreId
    name: str
    offers: dict[ItemId, ItemOffer] = field(default_factory=dict)
    robots: int = 0
    """Robots that can take work here now (eligible for choose_robot)."""

    def total(self, item_ids: list[ItemId] | None = None) -> int:
        ids = self.offers.keys() if item_ids is None else item_ids
        return sum(self.offers[i].line_cents for i in ids if i in self.offers)


@dataclass
class StorePlan:
    choices: list[StoreChoice]
    assignment: dict[StoreId, list[ItemId]]
    """Chosen store -> list item ids it will shop (primary first; uncovered items stay on the primary)."""
    evals: dict[StoreId, StoreEval]
    primary: StoreId

    @property
    def chosen(self) -> list[StoreId]:
        return list(self.assignment)


async def _candidates(catalog: Any, item: ListItem, store_ids: list[StoreId]) -> list[Sku]:
    cands = [s for s in [item.sku, *item.substitutes] if s]
    if cands or catalog is None:
        return cands
    try:
        hits = await catalog.search(SearchQuery(text=item.query, category=None, limit=3))
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("search(%r) failed: %s", item.query, exc)
        return []
    return [h.product.sku for h in hits[:3]]


async def _offers(catalog: Any, sku: Sku, cache: dict[Sku, list[StoreOffer]]) -> list[StoreOffer]:
    if sku not in cache:
        try:
            cache[sku] = list(await catalog.get_offers(sku))
        except Exception as exc:
            log.warning("get_offers(%s) failed: %s", sku, exc)
            cache[sku] = []
    return cache[sku]


def _best_offer(offers_by_sku: dict[Sku, list[StoreOffer]], cands: list[Sku], store_id: StoreId, qty: int):
    for full in (True, False):
        for sku in cands:
            for o in offers_by_sku.get(sku, []):
                if o.store_id == store_id and (o.qty >= qty if full else o.qty > 0):
                    return ItemOffer(sku=sku, line_cents=qty * o.price_cents, full_qty=o.qty >= qty)
    return None


async def evaluate_stores(
    catalog: Any,
    items: list[ListItem],
    stores: dict[StoreId, str],
    robots_per_store: dict[StoreId, int],
) -> tuple[dict[StoreId, StoreEval], dict[ItemId, bool]]:
    """Per store: which items are in stock and at what line price. Also: item_id -> available online."""
    store_ids = list(stores)
    evals = {
        sid: StoreEval(store_id=sid, name=name, robots=robots_per_store.get(sid, 0)) for sid, name in stores.items()
    }
    online: dict[ItemId, bool] = {}
    cache: dict[Sku, list[StoreOffer]] = {}
    for item in items:
        cands = await _candidates(catalog, item, store_ids)
        by_sku = {sku: await _offers(catalog, sku, cache) for sku in cands} if catalog is not None else {}
        online[item.item_id] = any(o.store_id == ONLINE_STORE_ID and o.qty > 0 for os in by_sku.values() for o in os)
        for sid in store_ids:
            off = _best_offer(by_sku, cands, sid, item.qty)
            if off is not None:
                evals[sid].offers[item.item_id] = off
    return evals, online


def choose_stores(
    items: list[ListItem],
    evals: dict[StoreId, StoreEval],
    online: dict[ItemId, bool],
    *,
    hint: StoreId | None = None,
    home: StoreId | None = None,
    allow_split: bool = True,
) -> StorePlan:
    ids = [i.item_id for i in items]
    queries = {i.item_id: i.query for i in items}

    def rank_key(ev: StoreEval) -> tuple:
        return (-len(ev.offers), ev.total(), -min(ev.robots, 1), ev.store_id)

    ranked = sorted(evals.values(), key=rank_key)
    if hint and hint in evals:
        primary = evals[hint]
    else:
        pool = [e for e in ranked if e.robots > 0] or ranked
        primary = pool[0]
        best = [e for e in pool if len(e.offers) == len(primary.offers)]
        home_ev = next((e for e in best if e.store_id == home), None)
        if home_ev is not None and home_ev.total() * 100 <= primary.total() * (100 + HOME_STORE_MARGIN_PCT):
            primary = home_ev
    assignment: dict[StoreId, list[ItemId]] = {primary.store_id: [i for i in ids if i in primary.offers]}
    missing = [i for i in ids if i not in primary.offers]
    split_reason: dict[StoreId, str] = {}
    if allow_split and not hint and missing:
        for ev in ranked:
            if ev.store_id == primary.store_id or ev.robots <= 0 or not missing:
                continue
            covers = [i for i in missing if i in ev.offers]
            if not covers:
                continue
            not_online = [i for i in covers if not online.get(i, False)]
            if len(covers) >= SPLIT_MIN_ITEMS or not_online:
                assignment[ev.store_id] = covers
                missing = [i for i in missing if i not in covers]
                split_reason[ev.store_id] = f"covers {', '.join(queries[i] for i in covers)} missing at {primary.name}"
    # whatever no store covers stays on the primary list (trip sourcing -> online / unresolved)
    assignment[primary.store_id] = [i for i in ids if i in assignment[primary.store_id] or i in missing]

    choices: list[StoreChoice] = []
    for ev in ranked:
        chosen = ev.store_id in assignment
        in_stock = [i for i in ids if i in ev.offers]
        mine = [i for i in assignment.get(ev.store_id, []) if i in ev.offers] if chosen else in_stock
        est = ev.total(mine)
        miss = [queries[i] for i in ids if i not in ev.offers]
        parts = [f"{len(in_stock)}/{len(ids)} items in stock"]
        others = [o for o in evals.values() if o.store_id != ev.store_id]
        if others and in_stock:
            common = [i for i in in_stock if all(i in o.offers for o in others)]
            if common:
                diff = min(o.total(common) for o in others) - ev.total(common)
                if diff > 0:
                    parts.append(f"{fmt_cents(diff)} cheaper on shared items")
                elif diff < 0:
                    parts.append(f"{fmt_cents(-diff)} pricier on shared items")
        parts.append(f"{ev.robots} robot{'s' if ev.robots != 1 else ''} free" if ev.robots else "no robot free")
        if ev.store_id in split_reason:
            parts.append(split_reason[ev.store_id])
        elif chosen and ev.store_id == primary.store_id:
            if hint:
                parts.append("store you picked")
            elif ev.store_id == home:
                parts.append("your home store")
            uncovered = [queries[i] for i in assignment[ev.store_id] if i not in ev.offers]
            if uncovered:
                parts.append(f"{', '.join(uncovered)} -> online")
        coverage = len(in_stock) / len(ids) if ids else 0.0
        choices.append(
            StoreChoice(
                store_id=ev.store_id,
                store_name=ev.name,
                chosen=chosen,
                item_ids=list(assignment.get(ev.store_id, [])) if chosen else in_stock,
                in_stock_count=len(in_stock),
                total_items=len(ids),
                est_total_cents=est,
                missing=miss,
                available_robots=ev.robots,
                score=round(coverage * 100 - ev.total(in_stock) / 1000, 3),
                reason=", ".join(parts),
            )
        )
    order = [primary.store_id] + [s for s in assignment if s != primary.store_id]
    assignment = {s: assignment[s] for s in order}
    choices.sort(key=lambda c: (not c.chosen, order.index(c.store_id) if c.chosen else 0, -c.score, c.store_id))
    return StorePlan(choices=choices, assignment=assignment, evals=evals, primary=primary.store_id)


def sub_list(
    shopping_list: ShoppingList,
    store_id: StoreId,
    item_ids: list[ItemId],
    evals: dict[StoreId, StoreEval],
    *,
    primary: bool,
    split: bool,
    budget_cents: int | None,
    store_name: str | None = None,
) -> ShoppingList:
    """The per-store slice of a request's list. The primary store's items are passed through untouched (identical to
    the unsplit list when there is no split); secondary-store items use the SKU that store has in stock."""
    keep = set(item_ids)
    items: list[ListItem] = []
    for item in shopping_list.items:
        if item.item_id not in keep:
            continue
        off = evals.get(store_id).offers.get(item.item_id) if store_id in evals else None
        if not primary and off is not None and off.sku != item.sku:
            subs = [s for s in [item.sku, *item.substitutes] if s and s != off.sku]
            item = item.model_copy(update={"sku": off.sku, "substitutes": subs})
        if split:
            item = item.model_copy(update={"sourcing": "unresolved", "store_id": None, "est_line_cents": None})
        items.append(item)
    if not split:
        return shopping_list.model_copy(update={"items": items, "budget_cents": budget_cents})
    title = shopping_list.title or "Shopping"
    return shopping_list.model_copy(
        update={
            "list_id": f"{shopping_list.list_id}-{store_id}",
            "items": items,
            "budget_cents": budget_cents,
            "title": f"{title} ({store_name or store_id})",
            "store_id": None,
        }
    )


__all__ = [
    "HOME_STORE_MARGIN_PCT",
    "SPLIT_MIN_ITEMS",
    "ItemOffer",
    "StoreEval",
    "StorePlan",
    "choose_stores",
    "evaluate_stores",
    "sub_list",
]
