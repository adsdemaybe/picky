"""Tiny in-memory stand-ins for the services the agent calls (so agent tests don't import other arms)."""

from __future__ import annotations

import re
from typing import Any

from contracts.api import AddLineResult, CheckoutStatus
from contracts.catalog import Product, ProductLocation, SearchHit, SearchQuery, StoreOffer
from contracts.common import Point3, Pose2D
from contracts.shopping import Cart, CartLine, TripState
from contracts.trip import Trip
from contracts.trust import MandateDecision, MandateVerdict

_N = 0


def _p(sku: str, name: str, category: str, price: int, tags=(), dietary=(), spice=None) -> Product:
    global _N
    _N += 1
    return Product(
        sku=sku,
        name=name,
        category=category,
        price_cents=price,
        tags=list(tags),
        dietary=list(dietary),
        spice_level=spice,
        barcode=f"{_N:013d}",
    )


PRODUCTS = [
    _p("TORT-01", "Corn Tortillas 12ct", "bakery", 299, ["taco", "tortillas"], ["vegetarian"]),
    _p("BEEF-01", "Ground Beef 1 lb", "meat", 699, ["taco", "beef"]),
    _p("BEAN-01", "Black Beans 15 oz", "pantry", 129, ["beans"], ["vegetarian", "vegan"]),
    _p("CHS-01", "Mexican Shredded Cheese", "dairy", 449, ["cheese"], ["vegetarian"]),
    _p("SALSA-MILD", "Casa Salsa Mild", "pantry", 349, ["salsa", "mild"], ["vegetarian"], spice=1),
    _p("SALSA-HOT", "Casa Salsa Diablo", "pantry", 379, ["salsa", "spicy"], ["vegetarian"], spice=4),
    _p("AVO-01", "Hass Avocado", "produce", 129, ["avocado"], ["vegetarian"]),
    _p("LIME-01", "Limes", "produce", 50, ["lime"], ["vegetarian"]),
    _p("ONI-01", "White Onion", "produce", 99, ["onion"], ["vegetarian"]),
    _p("CIL-01", "Cilantro Bunch", "produce", 99, ["cilantro", "herbs"], ["vegetarian"]),
    _p("SC-01", "Sour Cream 16 oz", "dairy", 259, ["sour cream"], ["vegetarian"]),
]
STORE_QTY = {p.sku: 10 for p in PRODUCTS} | {"CIL-01": 0}


def _offer(p: Product, store_id: str) -> StoreOffer:
    if store_id == "online":
        return StoreOffer(store_id="online", sku=p.sku, price_cents=p.price_cents + 50, qty=99, eta_hours=24)
    loc = ProductLocation(
        store_id=store_id,
        sku=p.sku,
        slot_id=f"slot_{p.sku}",
        aisle_label="Aisle 3",
        fixture_id="fx_1",
        level=1,
        index=0,
        item_position=Point3(x=1, y=1, z=1),
        approach_pose=Pose2D(x=1, y=0.5),
        qty=STORE_QTY[p.sku],
    )
    return StoreOffer(store_id=store_id, sku=p.sku, price_cents=p.price_cents, qty=STORE_QTY[p.sku], location=loc)


class StubCatalog:
    def __init__(self) -> None:
        self.by_sku = {p.sku: p for p in PRODUCTS}
        self.searches: list[SearchQuery] = []

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        self.searches.append(query)
        words = set(re.findall(r"[a-z]+", query.text.lower()))
        words |= {w[:-1] for w in words if w.endswith("s")}
        hits = []
        for p in PRODUCTS:
            hay = set(re.findall(r"[a-z]+", " ".join([p.name, *p.tags]).lower()))
            hay |= {w[:-1] for w in hay if w.endswith("s")}
            if words and not (words & hay):
                continue
            if any(d not in p.dietary for d in query.dietary):
                continue
            stores = query.store_ids or ["store_ave", "online"]
            hits.append(SearchHit(product=p, offers=[_offer(p, s) for s in stores], score=len(words & hay)))
        hits.sort(key=lambda h: -h.score)  # stable: catalog order on ties
        return hits[: query.limit]

    async def get_product(self, sku: str) -> Product | None:
        return self.by_sku.get(sku)

    async def get_offers(self, sku: str, store_ids: list[str] | None = None) -> list[StoreOffer]:
        p = self.by_sku.get(sku)
        if p is None:
            return []
        return [_offer(p, s) for s in (store_ids or ["store_ave", "online"])]


class StubEvents:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> Any:
        self.events.append(event)
        return event


def _decision(verdict: MandateVerdict = MandateVerdict.APPROVE) -> MandateDecision:
    return MandateDecision(decision_id="dec_1", user_id="maya", verdict=verdict)


class StubTrips:
    def __init__(self, trip: Trip) -> None:
        self.trip = trip
        self.calls: list[tuple[str, Any]] = []

    async def get_trip(self, trip_id: str) -> Trip:
        return self.trip.model_copy(deep=True)

    async def add_line(self, trip_id: str, line: CartLine, approval_id: str | None = None) -> AddLineResult:
        self.calls.append(("add_line", line))
        cart = self.trip.cart.model_copy(update={"lines": [*self.trip.cart.lines, line]})
        self.trip = self.trip.model_copy(update={"cart": cart})
        return AddLineResult(added=True, line_id=line.line_id, decision=_decision(), cart=cart)

    async def start_checkout(self, trip_id: str, note: str | None = None) -> CheckoutStatus:
        self.calls.append(("start_checkout", note))
        return CheckoutStatus(
            trip_id=trip_id,
            state=TripState.CHECKOUT,
            cart_hash="abc",
            total_cents=self.trip.cart.total_cents,
            decision=_decision(),
        )


def make_trip(**kw: Any) -> Trip:
    base: dict[str, Any] = {
        "trip_id": "trip_1",
        "user_id": "maya",
        "store_id": "store_ave",
        "cart": Cart(cart_id="cart_1", trip_id="trip_1", user_id="maya"),
    }
    base.update(kw)
    return Trip(**base)
