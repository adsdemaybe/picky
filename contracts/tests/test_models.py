"""Round-trips, validators and helpers of the contracts models."""

import json
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

import contracts
from contracts.catalog import Product, SearchHit, StoreOffer, ean13_check_digit, is_valid_ean13
from contracts.common import Pose2D, Rect2D, money, normalize_angle
from contracts.perception import BasketCheck
from contracts.planogram import FixturesFile, Planogram
from contracts.profile import UserProfile
from contracts.routing import GridSpec, RouteRequest, Stop
from contracts.shopping import Cart, CartLine, TripState, can_transition
from contracts.trip import Trip
from contracts.trust import (
    ApprovalKind,
    ApprovalRequest,
    CartMandate,
    IntentMandate,
    MandateDecision,
    MandateReason,
    MandateVerdict,
    Receipt,
    compute_cart_hash,
)


def _line(sku: str, qty: int, price: int, **kw) -> CartLine:
    kw.setdefault("store_id", "online" if kw.get("source") == "online" else "store_ave")
    return CartLine(line_id=f"l_{sku}", sku=sku, qty=qty, unit_price_cents=price, **kw)


def _product(**kw) -> Product:
    base = dict(sku="salsa_hot", name="Hot Salsa", category="pantry", price_cents=399, barcode="0123456789012")
    base.update(kw)
    return Product(**base)


def test_version_and_embed_dim():
    assert contracts.CONTRACTS_VERSION == "0.2.2"
    assert contracts.EMBED_DIM == 384


def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        Pose2D(x=1, y=2, theta=0, z=3)


def test_money_formatting_and_angles():
    assert money(1234) == "$12.34"
    assert money(-5) == "-$0.05"
    assert normalize_angle(3 * 3.141592653589793) == pytest.approx(3.141592653589793)


def test_timestamp_coercion():
    prof = UserProfile(user_id="u", display_name="U", updated_at=date(2026, 10, 18))
    assert prof.updated_at == datetime(2026, 10, 18, tzinfo=UTC)
    naive = UserProfile(user_id="u", display_name="U", updated_at=datetime(2026, 10, 18, 9, 0))
    assert naive.updated_at.tzinfo is not None


def test_product_roundtrip_and_tags_lowercased():
    p = _product(tags=["Spicy", " Taco "], spice_level=4)
    assert p.tags == ["spicy", "taco"]
    again = Product.model_validate_json(p.model_dump_json())
    assert again == p
    assert again.asset.dims_m == p.asset.dims_m


def test_product_rejects_bad_barcode_and_negative_price():
    with pytest.raises(ValidationError):
        _product(barcode="abc")
    with pytest.raises(ValidationError):
        _product(price_cents=-1)


def test_ean13():
    code12 = "400638133393"
    full = code12 + str(ean13_check_digit(code12))
    assert is_valid_ean13(full)
    assert not is_valid_ean13(code12 + str((ean13_check_digit(code12) + 1) % 10))


def test_profile_pref_range():
    with pytest.raises(ValidationError):
        UserProfile(user_id="u", display_name="U", prefs={"spicy": 2.0})
    with pytest.raises(ValidationError):
        UserProfile(user_id="u", display_name="U", taste_vector=[0.0] * 3)


def test_cart_totals_exclude_declined_and_add_shipping_only_for_online():
    cart = Cart(
        cart_id="c",
        lines=[
            _line("a", 2, 300),
            _line("b", 1, 500, status="declined"),
            _line("c", 1, 1000, source="online"),
        ],
        shipping_cents=599,
    )
    assert cart.subtotal_cents == 1600
    assert cart.in_store_cents == 600
    assert cart.online_cents == 1000
    assert cart.total_cents == 2199
    in_store_only = Cart(cart_id="c", lines=[_line("a", 1, 100)], shipping_cents=599)
    assert in_store_only.total_cents == 100


def test_cart_hash_deterministic_and_sensitive():
    a = Cart(cart_id="c1", trip_id="t1", lines=[_line("a", 2, 300), _line("b", 1, 500)])
    b = Cart(cart_id="c2", trip_id="t1", lines=[_line("b", 1, 500), _line("a", 2, 300, status="verified")])
    h = compute_cart_hash(a, "t1")
    assert compute_cart_hash(b, "t1") == h  # order/status/cart_id don't matter
    assert len(h) == 64
    changed_qty = Cart(cart_id="c1", trip_id="t1", lines=[_line("a", 3, 300), _line("b", 1, 500)])
    assert compute_cart_hash(changed_qty, "t1") != h
    changed_price = Cart(cart_id="c1", trip_id="t1", lines=[_line("a", 2, 299), _line("b", 1, 500)])
    assert compute_cart_hash(changed_price, "t1") != h
    declined = Cart(cart_id="c1", trip_id="t1", lines=[*a.lines, _line("z", 1, 999, status="declined")])
    assert compute_cart_hash(declined, "t1") == h
    # trip_id is required and must agree with cart.trip_id; a trip-less cart hashes the same as one with the id
    with pytest.raises(ValueError, match="trip_id"):
        compute_cart_hash(a, "other_trip")
    with pytest.raises(ValueError, match="required"):
        compute_cart_hash(a, "")
    unbound = a.model_copy(update={"trip_id": None})
    assert compute_cart_hash(unbound, "t1") == h


def test_cart_mandate_from_cart():
    cart = Cart(cart_id="c", trip_id="t1", lines=[_line("a", 2, 300), _line("b", 1, 500, status="declined")])
    cm = CartMandate.from_cart(cart, trip_id="t1", user_id="maya")
    assert cm.total_cents == 600
    assert [ln.sku for ln in cm.lines] == ["a"]
    assert cm.cart_hash == compute_cart_hash(cart, "t1")
    assert cm.hash_matches()
    tampered = cm.model_copy(update={"total_cents": 1})
    assert not tampered.hash_matches()
    assert CartMandate.model_validate_json(cm.model_dump_json()) == cm


def test_trip_state_machine():
    assert can_transition(TripState.EXECUTING, TripState.AWAITING_APPROVAL)
    assert can_transition(TripState.AWAITING_APPROVAL, TripState.EXECUTING)
    assert can_transition(TripState.CHECKOUT, TripState.PAID)
    assert can_transition(TripState.EXECUTING, TripState.FAILED)
    assert not can_transition(TripState.DONE, TripState.FAILED)
    assert not can_transition(TripState.INTENT, TripState.PAID)


def test_trip_roundtrip_and_pending_approvals():
    from contracts.shopping import Trip as LazyTrip

    assert LazyTrip is Trip
    req = ApprovalRequest(
        approval_id="ap1", trip_id="t1", user_id="maya", kind=ApprovalKind.RECOMMENDATION, title="Add hot sauce?"
    )
    trip = Trip(trip_id="t1", user_id="maya", store_id="store_ave", cart=Cart(cart_id="c"), approvals=[req])
    assert trip.pending_approvals() == [req]
    again = Trip.model_validate_json(trip.model_dump_json())
    assert again == trip
    assert json.loads(trip.model_dump_json())["approvals"][0]["kind"] == "recommendation"


def test_mandate_models_roundtrip():
    m = IntentMandate(mandate_id="m1", user_id="maya", cap_cents=4000, ask_above_cents=1500, expires_at="2026-12-31")
    assert IntentMandate.model_validate_json(m.model_dump_json()) == m
    d = MandateDecision(
        decision_id="d1", user_id="maya", verdict=MandateVerdict.NEEDS_APPROVAL, reasons=[MandateReason.OVER_CAP]
    )
    dumped = json.loads(d.model_dump_json())
    assert dumped["verdict"] == "needs_approval" and dumped["reasons"] == ["over_cap"]
    r = Receipt(
        receipt_id="r", trip_id="t", user_id="maya", provider="mock", status="succeeded", amount_cents=1, cart_hash="x"
    )
    assert Receipt.model_validate(r.model_dump()) == r
    with pytest.raises(ValidationError):
        Receipt(
            receipt_id="r",
            trip_id="t",
            user_id="maya",
            provider="paypal",
            status="succeeded",
            amount_cents=1,
            cart_hash="x",
        )


def test_basket_check_compare():
    chk = BasketCheck.compare(["a", "a", "b"], {"a": 1, "c": 1})
    assert not chk.ok
    assert chk.missing == {"a": 1, "b": 1}
    assert chk.unexpected == {"c": 1}
    assert BasketCheck.compare({"a": 2}, ["a", "a"]).ok


def test_grid_spec_roundtrip_math():
    g = GridSpec(store_id="s", resolution_m=0.05, width=280, height=440)
    col, row = g.world_to_cell(1.0, 2.0)
    assert (col, row) == (20, 40)
    x, y = g.cell_to_world(col, row)
    assert x == pytest.approx(1.025) and y == pytest.approx(2.025)
    assert g.in_bounds(col, row) and not g.in_bounds(-1, 0)


def test_route_request_unique_stop_ids():
    s = Stop(stop_id="s1", kind="mandatory", pose=Pose2D(x=1, y=1))
    with pytest.raises(ValidationError):
        RouteRequest(store_id="store_ave", start=Pose2D(x=0, y=0), stops=[s, s])


def test_search_hit_helpers():
    hit = SearchHit(
        product=_product(),
        offers=[
            StoreOffer(store_id="store_ave", sku="salsa_hot", price_cents=399, qty=0),
            StoreOffer(store_id="online", sku="salsa_hot", price_cents=449, qty=50),
        ],
    )
    assert hit.cheapest_in_stock().store_id == "online"
    assert hit.offer_for("store_ave").qty == 0


def _planogram_dict() -> dict:
    return {
        "store_id": "store_ave",
        "name": "Picky Ave",
        "bounds": {"x": 0, "y": 0, "w": 14, "h": 22},
        "entrance": {"x": 1, "y": 0.5, "theta": 1.57},
        "checkout": {"x": 3, "y": 1.5},
        "fixtures": [{"fixture_id": "f1", "kind": "shelf", "rect": {"x": 2, "y": 4, "w": 0.6, "h": 8}, "levels": 4}],
        "aisles": [{"aisle_id": "a1", "label": "Aisle 1", "rect": {"x": 2.6, "y": 4, "w": 1.6, "h": 8}}],
        "slots": [
            {
                "slot_id": "s1",
                "fixture_id": "f1",
                "aisle_id": "a1",
                "sku": "salsa_hot",
                "level": 1,
                "index": 0,
                "item_position": {"x": 2.55, "y": 5.0, "z": 0.6},
                "approach_pose": {"x": 3.2, "y": 5.0, "theta": 3.14159},
                "qty": 6,
                "lots": [{"lot_id": "L1", "qty": 6, "expiry": "2026-10-20"}],
                "deal": {"kind": "near_expiry", "price_cents": 249, "reason": "Best by Oct 20"},
            }
        ],
    }


def test_planogram_validation():
    plan = Planogram.model_validate(_planogram_dict())
    assert plan.slot("s1").lots[0].expiry == date(2026, 10, 20)
    assert plan.slots_for("salsa_hot")[0].slot_id == "s1"
    assert plan.start_pose == plan.entrance
    assert Planogram.model_validate_json(plan.model_dump_json()) == plan

    dup = _planogram_dict()
    dup["slots"].append(dict(dup["slots"][0]))
    with pytest.raises(ValidationError, match="duplicate"):
        Planogram.model_validate(dup)

    bad_fixture = _planogram_dict()
    bad_fixture["slots"][0]["fixture_id"] = "nope"
    with pytest.raises(ValidationError, match="unknown fixture_id"):
        Planogram.model_validate(bad_fixture)


def test_fixtures_file_user_consistency():
    user = {
        "user_id": "maya",
        "profile": {"user_id": "maya", "display_name": "Maya"},
        "mandate": {
            "mandate_id": "m1",
            "user_id": "maya",
            "cap_cents": 4000,
            "ask_above_cents": 1500,
            "expires_at": "2026-12-31",
        },
    }
    assert FixturesFile.model_validate({"users": [user]}).user("maya") is not None
    bad = {**user, "mandate": {**user["mandate"], "user_id": "alex"}}
    with pytest.raises(ValidationError):
        FixturesFile.model_validate({"users": [bad]})


def test_rect_helpers():
    r = Rect2D(x=0, y=0, w=2, h=1)
    assert r.contains(1, 0.5) and not r.contains(3, 0)
    assert r.intersects(Rect2D(x=1, y=0.5, w=5, h=5))
    assert not r.intersects(Rect2D(x=2, y=0, w=1, h=1))
