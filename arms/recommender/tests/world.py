"""A tiny in-memory taco world built from contracts models (same shapes as data/), for deterministic tests."""

from datetime import date, timedelta

from contracts.catalog import DealTerms, Lot, Product
from contracts.common import Point3, Pose2D, Rect2D, utcnow
from contracts.planogram import DemoUser, Fixture, Planogram, Slot
from contracts.profile import PurchaseRecord, UserProfile
from contracts.shopping import ListItem, ShoppingList
from contracts.trust import IntentMandate

TODAY = date(2026, 10, 17)
STORE = "store_ave"
TRIP = "trip_test1"

_BARCODE = iter(range(4000000000000, 4000000001000))


def prod(sku: str, name: str, category: str, price: int, tags=(), subcategory=None, spice=None, **kw) -> Product:
    return Product(
        sku=sku,
        name=name,
        category=category,
        subcategory=subcategory,
        price_cents=price,
        tags=list(tags),
        spice_level=spice,
        barcode=str(next(_BARCODE)),
        **kw,
    )


PRODUCTS = [
    prod("tortilla_corn", "Corn Tortillas", "bakery", 349, ["taco", "mexican"], "tortillas"),
    prod("beef_ground", "Ground Beef 1 lb", "meat", 699, ["taco", "protein"], "beef"),
    prod("beans_black", "Black Beans", "pantry", 149, ["taco", "vegetarian", "mexican"], "beans", dietary=["vegan"]),
    prod("salsa_mild", "Mild Salsa", "pantry", 399, ["taco", "mexican", "salsa"], "salsa", spice=1),
    prod("salsa_hot", "Habanero Salsa", "pantry", 449, ["taco", "mexican", "salsa"], "salsa", spice=4),
    prod("hot_sauce_cholula", "Cholula Hot Sauce", "pantry", 499, ["mexican", "hot_sauce"], "hot_sauce", spice=3),
    prod("hot_sauce_ghost", "Ghost Pepper Sauce", "pantry", 699, ["hot_sauce"], "hot_sauce_xhot", spice=5),
    prod("guac_kit", "Guacamole Kit", "produce", 649, ["taco", "mexican", "avocado"], "guacamole", perishable=True),
    prod("chips_tortilla", "Tortilla Chips", "snacks", 399, ["mexican", "snack"], "chips"),
    prod("cookies_choc", "Chocolate Cookies", "snacks", 449, ["sweet", "snack"], "cookies"),
    prod("mushroom_pack", "Mushroom Pack", "produce", 299, ["mushroom", "vegetarian"], "mushrooms"),
    prod("chorizo", "Pork Chorizo", "meat", 599, ["taco", "mexican", "spicy"], "sausage", spice=3),
    prod("lime_bag", "Limes", "produce", 299, ["taco", "citrus"], "limes"),
]
BY_SKU = {p.sku: p for p in PRODUCTS}


def planogram() -> Planogram:
    fixtures = [Fixture(fixture_id="f1", rect=Rect2D(x=2, y=2, w=10, h=1), levels=3)]
    slots = []
    for i, p in enumerate(PRODUCTS):
        lots: list[Lot] = []
        deal = None
        qty = 10
        if p.sku == "guac_kit":
            lots = [
                Lot(lot_id="guac_a", qty=4, expiry=TODAY + timedelta(days=1), freshness=0.6),
                Lot(lot_id="guac_b", qty=6, expiry=TODAY + timedelta(days=9), freshness=0.95),
            ]
            deal = DealTerms(
                kind="near_expiry", price_cents=389, original_cents=649, percent_off=40, reason="", lot_id="guac_a"
            )
        if p.sku == "chips_tortilla":
            deal = DealTerms(kind="percent_off", price_cents=299, original_cents=399, percent_off=25)
        if p.sku == "lime_bag":
            qty = 0  # out of stock
        slots.append(
            Slot(
                slot_id=f"s{i:02d}",
                fixture_id="f1",
                sku=p.sku,
                level=i % 3,
                index=i,
                item_position=Point3(x=2.5 + 0.7 * i, y=2.0),
                approach_pose=Pose2D(x=2.5 + 0.7 * i, y=1.0, theta=1.5708),
                qty=qty,
                lots=lots,
                deal=deal,
            )
        )
    return Planogram(
        store_id=STORE,
        name="Ave Market (test)",
        bounds=Rect2D(x=0, y=0, w=14, h=22),
        entrance=Pose2D(x=1, y=1),
        checkout=Pose2D(x=12, y=1),
        fixtures=fixtures,
        slots=slots,
    )


def _mandate(uid: str) -> IntentMandate:
    return IntentMandate(
        mandate_id=f"m_{uid}",
        user_id=uid,
        cap_cents=10000,
        ask_above_cents=2000,
        expires_at=utcnow() + timedelta(days=30),
    )


def users() -> list[DemoUser]:
    maya = UserProfile(
        user_id="maya",
        display_name="Maya",
        phone="+15555550100",
        email="Maya@example.com",
        prefs={"mexican": 0.6, "spicy": 0.2, "deals": 0.4, "snack": 0.1},
        dislikes=["mushroom"],
        dietary=["vegetarian"],
    )
    sam = UserProfile(user_id="sam", display_name="Sam", prefs={"sweet": 0.7}, dislikes=["spicy"])
    return [
        DemoUser(
            user_id="maya",
            profile=maya,
            mandate=_mandate("maya"),
            purchases=[
                PurchaseRecord(
                    purchase_id="p1",
                    user_id="maya",
                    sku="hot_sauce_cholula",
                    unit_price_cents=499,
                    store_id=STORE,
                    rating=5,
                ),
                PurchaseRecord(
                    purchase_id="p2", user_id="maya", sku="salsa_mild", unit_price_cents=399, store_id=STORE, rating=4
                ),
            ],
        ),
        DemoUser(user_id="sam", profile=sam, mandate=_mandate("sam")),
    ]


def taco_list(trip_id: str = TRIP) -> ShoppingList:
    items = [
        ("tortillas", "tortilla_corn"),
        ("black beans", "beans_black"),
        ("salsa", "salsa_mild"),
        ("limes", "lime_bag"),
    ]
    return ShoppingList(
        list_id="list_1",
        user_id="maya",
        trip_id=trip_id,
        store_id=STORE,
        title="Tacos for 6",
        servings=6,
        budget_cents=4000,
        items=[ListItem(item_id=f"i{n}", query=q, sku=s, sourcing="in_store") for n, (q, s) in enumerate(items)],
    )
