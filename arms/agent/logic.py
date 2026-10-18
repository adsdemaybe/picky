"""Deterministic agent logic shared by the fake and the real (Claude) agent.

Everything here is plain code (no LLM): intent parsing for the demo phrases, catalog-backed SKU guessing, split
sourcing, reply classification by regex, and follow-up composition. The real agent uses these as its fallback and for
the parts that must stay deterministic (budget parsing, sourcing).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from contracts.catalog import Product, SearchHit, SearchQuery, StoreOffer
from contracts.common import ONLINE_STORE_ID, Sku, StoreId
from contracts.messaging import InboundMessage, OutboundMessage, ReplyInterpretation
from contracts.profile import UserProfile
from contracts.services import CatalogService
from contracts.shopping import CartLine, Intent, ListItem, ShoppingList, TripState
from contracts.trip import Trip
from contracts.trust import ApprovalRequest
from utils.ids import IdFactory
from utils.log import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------------------------------------------------
# Intent parsing
# ---------------------------------------------------------------------------------------------------------------------
WORD_NUMBERS: dict[str, int] = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "dozen": 12,
}
_NUM = r"(\d+|" + "|".join(sorted(WORD_NUMBERS, key=len, reverse=True)) + r")"
_COUNT = r"(\d+|" + "|".join(sorted((w for w in WORD_NUMBERS if w not in ("a", "an")), key=len, reverse=True)) + r")"

_SERVINGS_RE = re.compile(rf"\bfor\s+{_COUNT}\b(?!\s*(?:dollars|bucks|\$))", re.I)
_PEOPLE_RE = re.compile(rf"\b{_COUNT}\s+(?:people|persons|guests|friends|of us|adults|kids)\b", re.I)
_BUDGET_RE = re.compile(
    r"(?:under|below|less than|max(?:imum)?|budget(?: of| is)?|no more than|up to|within|<)\s*"
    r"\$?\s*(\d+(?:\.\d{1,2})?)\s*(?:dollars|bucks|usd)?",
    re.I,
)
_BUDGET_SUFFIX_RE = re.compile(r"\$\s*(\d+(?:\.\d{1,2})?)\s*(?:budget|max|or less|limit)", re.I)
_VEG_COUNT_RE = re.compile(rf"\b{_COUNT}\s+(?:of them\s+)?(?:is\s+|are\s+)?(?:vegetarians?|veggies?|veg)\b", re.I)
_VEG_ANY_RE = re.compile(r"\b(?:vegetarian|veggie|meatless|no meat)\b", re.I)
_VEGAN_RE = re.compile(r"\bvegan\b", re.I)


def parse_number(token: str | None) -> int | None:
    if token is None:
        return None
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return WORD_NUMBERS.get(token)


def parse_budget_cents(text: str) -> int | None:
    """'under $40' / 'less than 40 dollars' / '$40 budget' -> 4000. Deterministic (money never comes from the LLM)."""
    m = _BUDGET_RE.search(text) or _BUDGET_SUFFIX_RE.search(text)
    if not m:
        return None
    try:
        return int((Decimal(m.group(1)) * 100).to_integral_value())
    except InvalidOperation:
        return None


@dataclass
class ParsedIntent:
    text: str
    dish: str | None = None
    servings: int | None = None
    budget_cents: int | None = None
    vegetarian_count: int = 0
    all_vegetarian: bool = False
    vegan: bool = False
    free_items: list[tuple[str, int]] = field(default_factory=list)

    @property
    def dietary_notes(self) -> list[str]:
        notes: list[str] = []
        if self.all_vegetarian:
            notes.append("all vegetarian")
        elif self.vegetarian_count:
            s = "" if self.vegetarian_count == 1 else "s"
            notes.append(f"{self.vegetarian_count} vegetarian guest{s}")
        if self.vegan:
            notes.append("vegan")
        return notes


@dataclass(frozen=True)
class RecipeLine:
    query: str
    category: str
    qty: Callable[[int, int, int], int]
    """(servings, meat_eaters, vegetarians) -> qty (0 = skip)."""
    dietary: tuple[str, ...] = ()
    required: bool = True
    note: Callable[[int, int, int], str] = lambda n, m, v: ""


def _per(k: int) -> Callable[[int, int, int], int]:
    return lambda n, m, v: max(1, math.ceil(n / k))


RECIPES: dict[str, list[RecipeLine]] = {
    "tacos": [
        RecipeLine(
            "tortillas", "bakery", lambda n, m, v: max(1, math.ceil(n * 3 / 12)), note=lambda n, m, v: "3/person"
        ),
        RecipeLine(
            "ground beef",
            "meat",
            lambda n, m, v: math.ceil(m / 4) if m else 0,
            note=lambda n, m, v: f"for {m} meat eaters",
        ),
        RecipeLine(
            "black beans",
            "pantry",
            lambda n, m, v: max(1, math.ceil(v / 2)) if v else 0,
            dietary=("vegetarian",),
            note=lambda n, m, v: f"vegetarian protein for {v}",
        ),
        RecipeLine("shredded cheese", "dairy", _per(6)),
        RecipeLine("salsa", "pantry", _per(8)),
        RecipeLine("avocado", "produce", _per(2)),
        RecipeLine("limes", "produce", _per(3)),
        RecipeLine("onion", "produce", _per(8)),
        RecipeLine("cilantro", "produce", _per(10)),
        RecipeLine("sour cream", "dairy", _per(8), required=False),
    ],
}
DISH_ALIASES: dict[str, str] = {"taco": "tacos", "tacos": "tacos", "taco night": "tacos", "taco tuesday": "tacos"}
DEFAULT_SERVINGS = 4

_FILLER_RE = re.compile(
    r"\b(?:i need|i want|need|want|get me|get|buy|pick up|grab|some|please|also|and also|for tonight|shopping list)\b",
    re.I,
)


def parse_intent_text(text: str, profile: UserProfile | None = None) -> ParsedIntent:
    """Rule-based parse of the demo phrases: 'tacos for 6, under $40, one vegetarian'."""
    p = ParsedIntent(text=text)
    low = text.lower()

    for alias, dish in sorted(DISH_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", low):
            p.dish = dish
            break

    m = _SERVINGS_RE.search(text) or _PEOPLE_RE.search(text)
    p.servings = parse_number(m.group(1)) if m else None
    p.budget_cents = parse_budget_cents(text)

    vm = _VEG_COUNT_RE.search(text)
    if vm:
        p.vegetarian_count = parse_number(vm.group(1)) or 0
    elif _VEG_ANY_RE.search(text):
        p.all_vegetarian = True
    p.vegan = bool(_VEGAN_RE.search(text))
    if p.vegan:
        p.all_vegetarian = True
    if profile is not None and "vegetarian" in [d.lower() for d in profile.dietary] and not vm:
        p.all_vegetarian = True

    if p.dish is None:
        p.free_items = _parse_free_items(text)
    return p


def _parse_free_items(text: str) -> list[tuple[str, int]]:
    """'milk, 2 eggs and bread under $20' -> [('milk', 1), ('eggs', 2), ('bread', 1)]."""
    cleaned = _BUDGET_RE.sub(" ", text)
    cleaned = _BUDGET_SUFFIX_RE.sub(" ", cleaned)
    cleaned = _SERVINGS_RE.sub(" ", cleaned)
    cleaned = _FILLER_RE.sub(" ", cleaned)
    out: list[tuple[str, int]] = []
    for frag in re.split(r",|;|\n|\band\b|&|\+", cleaned, flags=re.I):
        frag = re.sub(r"[^\w\s'-]", " ", frag).strip().lower()
        frag = re.sub(r"\s+", " ", frag)
        if not frag:
            continue
        qty = 1
        qm = re.match(rf"^{_NUM}\s+(.+)$", frag)
        if qm and parse_number(qm.group(1)):
            qty, frag = parse_number(qm.group(1)) or 1, qm.group(2).strip()
        if frag and len(frag) > 1:
            out.append((frag, max(1, qty)))
    return out


@dataclass
class DraftItem:
    query: str
    qty: int = 1
    category: str | None = None
    dietary: list[str] = field(default_factory=list)
    required: bool = True
    notes: str = ""


def draft_items(p: ParsedIntent) -> list[DraftItem]:
    if p.dish and p.dish in RECIPES:
        n = p.servings or DEFAULT_SERVINGS
        v = n if p.all_vegetarian else min(p.vegetarian_count, n)
        m = n - v
        items: list[DraftItem] = []
        for line in RECIPES[p.dish]:
            q = line.qty(n, m, v)
            if q <= 0:
                continue
            dietary = list(line.dietary)
            if p.vegan and "vegan" not in dietary and line.category in ("dairy",):
                continue  # vegan: drop dairy rather than guess a substitute
            items.append(DraftItem(line.query, q, line.category, dietary, line.required, line.note(n, m, v)))
        return items
    return [DraftItem(q, qty) for q, qty in p.free_items]


# ---------------------------------------------------------------------------------------------------------------------
# Catalog-backed SKU guessing (personalized ranking)
# ---------------------------------------------------------------------------------------------------------------------
def _tokens(s: str) -> set[str]:
    toks = {t for t in re.findall(r"[a-z]+", s.lower()) if len(t) > 2}
    return toks | {t[:-1] for t in toks if t.endswith("s") and len(t) > 3}


def _match_count(hit: SearchHit, query: str) -> int:
    """How many query words the product's name/category/subcategory/tags contain ('ground beef' vs 'Ground Coffee')."""
    q = _tokens(query)
    hay = _tokens(" ".join([hit.product.name, hit.product.category, hit.product.subcategory or "", *hit.product.tags]))
    return len(q & hay)


def _relevant(hit: SearchHit, query: str) -> bool:
    return _match_count(hit, query) > 0


def pref_score(product: Product, profile: UserProfile | None) -> float:
    if profile is None:
        return 0.0
    keys = [*product.tags, product.category, *(product.dietary or [])]
    if product.subcategory:
        keys.append(product.subcategory)
    score = sum(profile.prefs.get(k, 0.0) for k in keys)
    if product.spice_level is not None and "spicy" in profile.prefs:
        score += profile.prefs["spicy"] * (product.spice_level - 2) / 3
    return score


def _blocked(product: Product, dietary: list[str], profile: UserProfile | None) -> bool:
    if any(d not in product.dietary for d in dietary):
        return True
    if profile is not None:
        if set(a.lower() for a in profile.allergies) & set(product.allergens):
            return True
    return False


def rank_hits(
    hits: list[SearchHit],
    query: str,
    dietary: list[str],
    profile: UserProfile | None,
    category: str | None = None,
) -> list[SearchHit]:
    """Best candidate first: relevant -> not disliked -> expected category ('limes' is produce, not chips) -> most
    query words matched -> favorite -> profile prefs -> catalog order -> sku."""
    usable = [h for h in hits if not _blocked(h.product, dietary, profile)]
    relevant = [h for h in usable if _relevant(h, query)] or usable
    order = {id(h): i for i, h in enumerate(hits)}
    fav = set(profile.favorite_skus) if profile else set()
    dis = set(profile.disliked_skus) if profile else set()
    dis_tags = set(t.lower() for t in profile.dislikes) if profile else set()

    def key(h: SearchHit) -> tuple:
        p = h.product
        disliked = p.sku in dis or bool(dis_tags & (set(p.tags) | {p.category}))
        off_category = bool(category) and p.category.lower() != (category or "").lower()
        return (
            disliked,
            off_category,
            -_match_count(h, query),
            p.sku not in fav,
            -round(pref_score(p, profile), 6),
            order[id(h)],
            p.sku,
        )

    return sorted(relevant, key=key)


async def search_candidates(
    catalog: CatalogService | None,
    query: str,
    dietary: list[str] | None = None,
    profile: UserProfile | None = None,
    store_ids: list[StoreId] | None = None,
    limit: int = 8,
    category: str | None = None,
) -> list[SearchHit]:
    if catalog is None:
        return []
    try:
        hits = await catalog.search(
            SearchQuery(text=query, dietary=list(dietary or []), store_ids=store_ids, limit=limit)
        )
    except Exception as e:  # catalog down / bad query: degrade to unresolved
        log.warning("catalog search %r failed: %s", query, e)
        return []
    return rank_hits(hits, query, list(dietary or []), profile, category)


async def guess_skus(
    catalog: CatalogService | None, items: list[ListItem], profile: UserProfile | None
) -> list[ListItem]:
    """Attach a SKU guess + substitutes + category to each item (sourcing stays 'unresolved')."""
    out: list[ListItem] = []
    for item in items:
        ranked = await search_candidates(catalog, item.query, item.dietary, profile, category=item.category)
        if not ranked:
            out.append(item)
            continue
        best = ranked[0].product
        same_kind = [h for h in ranked[1:] if not item.category or h.product.category == item.category]
        subs = [h.product.sku for h in same_kind[:2]]
        out.append(item.model_copy(update={"sku": best.sku, "substitutes": subs, "category": best.category}))
    return out


def build_shopping_list(
    intent: Intent,
    items: list[DraftItem],
    *,
    title: str | None,
    servings: int | None,
    budget_cents: int | None,
    dietary_notes: list[str],
) -> ShoppingList:
    ids = IdFactory(intent.intent_id)
    list_id = ids("list")
    list_items = [
        ListItem(
            item_id=f"{list_id}_i{n + 1}",
            query=d.query,
            qty=max(1, int(d.qty)),
            category=d.category,
            dietary=list(d.dietary),
            required=d.required,
            notes=d.notes,
        )
        for n, d in enumerate(items)
    ]
    return ShoppingList(
        list_id=list_id,
        user_id=intent.user_id,
        trip_id=intent.trip_id,
        title=title,
        servings=servings,
        budget_cents=budget_cents,
        dietary_notes=dietary_notes,
        items=list_items,
        intent_id=intent.intent_id,
    )


def title_for(p: ParsedIntent) -> str | None:
    if p.dish:
        return f"{p.dish.capitalize()} for {p.servings}" if p.servings else p.dish.capitalize()
    return None


async def expand_intent_rules(
    catalog: CatalogService | None, intent: Intent, profile: UserProfile | None
) -> ShoppingList:
    p = parse_intent_text(intent.text, profile)
    budget = p.budget_cents if p.budget_cents is not None else (profile.default_budget_cents if profile else None)
    sl = build_shopping_list(
        intent,
        draft_items(p),
        title=title_for(p),
        servings=p.servings or (DEFAULT_SERVINGS if p.dish else None),
        budget_cents=budget,
        dietary_notes=p.dietary_notes,
    )
    items = await guess_skus(catalog, sl.items, profile)
    return sl.model_copy(update={"items": items})


# ---------------------------------------------------------------------------------------------------------------------
# Split sourcing (deterministic)
# ---------------------------------------------------------------------------------------------------------------------
async def _offers(catalog: CatalogService, sku: Sku) -> list[StoreOffer]:
    try:
        return await catalog.get_offers(sku)
    except Exception as e:
        log.warning("get_offers(%s) failed: %s", sku, e)
        return []


def _pick(offers: list[StoreOffer], store_id: StoreId, qty: int) -> StoreOffer | None:
    o = next((o for o in offers if o.store_id == store_id and o.qty >= qty), None)
    return o or next((o for o in offers if o.store_id == store_id and o.in_stock), None)


async def source_items(catalog: CatalogService | None, shopping_list: ShoppingList, store_id: StoreId) -> ShoppingList:
    """in stock at store_id -> 'in_store'; OOS / online-only -> 'online' (store_id='online'); else 'unresolved'.

    Order tried: the SKU guess in store, the SKU guess online, substitutes in store, substitutes online, then a fresh
    search (same order). Keeping the exact SKU online before switching brands keeps the shopper's pick.
    """
    items: list[ListItem] = []
    for item in shopping_list.items:
        items.append(await _source_one(catalog, item, store_id))
    return shopping_list.model_copy(update={"items": items, "store_id": store_id})


async def _source_one(catalog: CatalogService | None, item: ListItem, store_id: StoreId) -> ListItem:
    if catalog is None:
        return item.model_copy(update={"sourcing": "unresolved", "notes": _join(item.notes, "catalog unavailable")})

    candidates: list[Sku] = [s for s in [item.sku, *item.substitutes] if s]
    if not candidates:
        hits = await search_candidates(catalog, item.query, item.dietary, category=item.category)
        candidates = [h.product.sku for h in hits[:3]]

    offers_by_sku: dict[Sku, list[StoreOffer]] = {}
    for sku in candidates:
        offers_by_sku[sku] = await _offers(catalog, sku)

    def done(sku: Sku, offer: StoreOffer, note: str) -> ListItem:
        return item.model_copy(
            update={
                "sku": sku,
                "sourcing": "online" if offer.store_id == ONLINE_STORE_ID else "in_store",
                "store_id": offer.store_id,
                "est_line_cents": item.qty * offer.price_cents,
                "notes": _join(item.notes, note),
            }
        )

    if store_id != ONLINE_STORE_ID:
        first = candidates[0] if candidates else None
        if first:
            o = _pick(offers_by_sku[first], store_id, item.qty)
            if o:
                aisle = o.location.aisle_label if o.location and o.location.aisle_label else None
                return done(first, o, f"in stock at {store_id}" + (f", {aisle}" if aisle else ""))
            o = _pick(offers_by_sku[first], ONLINE_STORE_ID, item.qty)
            if o:
                at_store = any(x.store_id == store_id for x in offers_by_sku[first])
                why = f"OOS at {store_id} -> online" if at_store else f"not carried at {store_id} -> online"
                return done(first, o, why)
        for sku in candidates[1:]:
            o = _pick(offers_by_sku[sku], store_id, item.qty)
            if o:
                return done(sku, o, f"substitute in stock at {store_id}")
    for sku in candidates:
        o = _pick(offers_by_sku[sku], ONLINE_STORE_ID, item.qty)
        if o:
            return done(sku, o, "online")

    return item.model_copy(
        update={"sourcing": "unresolved", "store_id": None, "notes": _join(item.notes, "not found in any store")}
    )


def _join(a: str, b: str) -> str:
    return f"{a}; {b}" if a else b


# ---------------------------------------------------------------------------------------------------------------------
# Reply interpretation (regex)
# ---------------------------------------------------------------------------------------------------------------------
_YES_RE = re.compile(
    r"^\s*(?:y|ya|yes+|yeah|yea|yep|yup|sure|ok|okay|k|approved?|confirm(?:ed)?|do it|go ahead|add (?:it|them)|"
    r"sounds good|please|absolutely|definitely|why not|of course|let'?s do it)\b",
    re.I,
)
_NO_RE = re.compile(
    r"^\s*(?:n|no+|nope|nah|skip|pass|reject|decline|don'?t|not now|no thanks|cancel|never ?mind)\b", re.I
)
_YES_EMOJI = ("👍", "✅", "👌", "🙌", "💯")
_NO_EMOJI = ("👎", "❌", "🚫", "🙅")
_ALL_RE = re.compile(r"\b(?:all|all of them|everything|both)\b", re.I)
_NONE_RE = re.compile(r"\b(?:none|neither|nothing)\b", re.I)
_INT_RE = re.compile(r"\b(\d{1,2})\b")
_RATING_RE = re.compile(r"\b([1-5])\s*(?:/\s*5|out of 5|of 5|stars?|⭐)", re.I)
_BARE_RATING_RE = re.compile(r"^\s*([1-5])\s*[.!]*\s*$")
_INTENT_RE = re.compile(
    r"\b(?:for \d+|for (?:two|three|four|five|six|seven|eight|nine|ten)|under \$|need|buy|get me|pick up|"
    r"shopping|groceries|tacos?|recipe|dinner|party)\b",
    re.I,
)
_SENTIMENT: list[tuple[re.Pattern[str], int, float]] = [
    (re.compile(r"\b(?:loved|love|amazing|perfect|incredible|the best|fantastic)\b", re.I), 5, 0.9),
    (re.compile(r"\b(?:great|good|tasty|delicious|nice|liked|yum)\b", re.I), 4, 0.6),
    (re.compile(r"\b(?:ok|okay|fine|meh|alright|so-so)\b", re.I), 3, 0.0),
    (re.compile(r"\b(?:bland|disappointing|not great|boring|too mild|weak)\b", re.I), 2, -0.5),
    (re.compile(r"\b(?:terrible|awful|gross|hated|disgusting|bad)\b", re.I), 1, -0.9),
]
_TAGS: list[tuple[re.Pattern[str], list[str]]] = [
    (
        re.compile(
            r"\b(?:too mild|not (?:spicy|hot) enough|bland|needs? more (?:heat|spice|kick)|could be (?:spicier|hotter)|"
            r"spicier|more heat)\b",
            re.I,
        ),
        ["too_mild", "want_spicier"],
    ),
    (re.compile(r"\btoo (?:spicy|hot)\b", re.I), ["too_spicy"]),
    (re.compile(r"\btoo (?:salty)\b", re.I), ["too_salty"]),
    (re.compile(r"\btoo (?:sweet)\b", re.I), ["too_sweet"]),
    (re.compile(r"\b(?:stale|not fresh|overripe|went bad|moldy)\b", re.I), ["freshness_issue"]),
]


def _rating_and_sentiment(text: str) -> tuple[int | None, float | None, bool]:
    """(rating, sentiment, explicit) from '4/5', '5 stars', '⭐⭐⭐', 'loved it'..."""
    m = _RATING_RE.search(text) or _BARE_RATING_RE.match(text)
    stars = text.count("⭐")
    rating: int | None = int(m.group(1)) if m else (min(stars, 5) if stars else None)
    explicit = rating is not None
    sentiment: float | None = None
    for pat, r, s in _SENTIMENT:
        if pat.search(text):
            sentiment = s
            if rating is None:
                rating = r
            break
    if sentiment is None and rating is not None:
        sentiment = round((rating - 3) / 2, 2)
    return rating, sentiment, explicit


def _feedback_tags(text: str) -> list[str]:
    tags: list[str] = []
    for pat, ts in _TAGS:
        if pat.search(text):
            tags += [t for t in ts if t not in tags]
    return tags


def _feedback_sku(text: str, trip: Trip | None, taste_tags: bool = False) -> Sku | None:
    """Which bought item the reply is about: the follow-up's item when the reply names it, names nothing else, or
    complains about taste ('too mild' answers 'how was the salsa?'); otherwise the first item it names."""
    if trip is None:
        return None
    lines: list[CartLine] = list(trip.receipt.lines) if trip.receipt else list(trip.cart.active_lines())
    about = list(trip.followup.about_skus) if trip.followup else []
    words = _tokens(text)
    mentioned = [line.sku for line in lines if line.name and words & _tokens(line.name)]
    if about and (about[0] in mentioned or not mentioned or taste_tags):
        return about[0]
    if mentioned:
        return mentioned[0]
    return None


_YES_REPLY = {
    "checkout": "Approved. Paying now.",
    "online_order": "Done, added to your online order.",
    "substitution": "OK, swapping it.",
}


def _target_approval(pending: list[ApprovalRequest], want_options: bool = False) -> ApprovalRequest | None:
    if not pending:
        return None
    ordered = sorted(pending, key=lambda a: a.created_at)
    if want_options:
        with_opts = [a for a in ordered if a.options]
        if with_opts:
            return with_opts[-1]
    return ordered[-1]


def _option_ids(req: ApprovalRequest, numbers: list[int]) -> list[str]:
    ids = [o.option_id for o in req.options]
    out: list[str] = []
    for n in numbers:
        s = str(n)
        if s in ids:
            pick = s
        elif 1 <= n <= len(ids):
            pick = ids[n - 1]
        else:
            continue
        if pick not in out:
            out.append(pick)
    return out


def interpret_reply_rules(
    message: InboundMessage, pending: list[ApprovalRequest], trip: Trip | None = None
) -> ReplyInterpretation:
    text = message.text.strip()
    yes = bool(_YES_RE.search(text)) or any(e in text for e in _YES_EMOJI)
    no = bool(_NO_RE.search(text)) or any(e in text for e in _NO_EMOJI)
    numbers = [int(n) for n in _INT_RE.findall(text)]

    # 1) answers to pending approvals
    if pending:
        opt_req = _target_approval(pending, want_options=True)
        if opt_req is not None and opt_req.options:
            if _NONE_RE.search(text) or (no and not numbers):
                return _approval(opt_req, "reject", reply="OK, none of those.")
            if _ALL_RE.search(text) and opt_req.multi:
                ids = [o.option_id for o in opt_req.options]
                return _approval(opt_req, "approve", choices=ids, reply="Adding all of them.")
            picked = _option_ids(opt_req, numbers)
            if picked:
                if opt_req.multi:
                    return _approval(opt_req, "approve", choices=picked, reply=f"Got it: {', '.join(picked)}.")
                return _approval(opt_req, "approve", choice=picked[0], reply=f"Going with option {picked[0]}.")
        req = _target_approval(pending)
        assert req is not None
        if no and not yes:
            return _approval(req, "reject", reply="No problem, skipping it.")
        if yes:
            return _approval(req, "approve", reply=_YES_REPLY.get(req.kind.value, "Done, adding it."))

    # 2) post-purchase feedback
    followup_open = trip is not None and (
        trip.followup is not None or trip.state in (TripState.FOLLOWUP, TripState.PAID, TripState.DONE)
    )
    rating, sentiment, explicit = _rating_and_sentiment(text)
    tags = _feedback_tags(text)
    if followup_open and (rating is not None or tags):
        if not explicit and {"too_mild", "too_spicy"} & set(tags):
            rating, sentiment = 2, -0.5  # 'too mild' is about the item we asked about, even if they 'loved' another
        reply = "Thanks! I'll look for spicier picks next time." if "want_spicier" in tags else "Thanks, noted!"
        return ReplyInterpretation(
            kind="feedback",
            rating=rating,
            sentiment=sentiment,
            feedback_tags=tags,
            sku=_feedback_sku(text, trip, taste_tags=bool({"too_mild", "too_spicy"} & set(tags))),
            reply_text=reply,
            confidence=0.9 if explicit else 0.6,
        )

    # 3) a new shopping intent
    if _INTENT_RE.search(text) and len(text.split()) >= 2:
        return ReplyInterpretation(
            kind="intent", intent_text=text, reply_text="On it, building your list.", confidence=0.7
        )

    # 4) plain chat
    return ReplyInterpretation(kind="chat", sentiment=sentiment, confidence=0.5)


def _approval(
    req: ApprovalRequest,
    decision: str,
    *,
    choice: str | None = None,
    choices: list[str] | None = None,
    reply: str | None = None,
) -> ReplyInterpretation:
    return ReplyInterpretation(
        kind="approval",
        decision=decision,  # type: ignore[arg-type]
        approval_id=req.approval_id,
        choice=choice,
        choices=list(choices or []),
        reply_text=reply,
        confidence=0.95 if choice is None and not choices else 0.9,
    )


# ---------------------------------------------------------------------------------------------------------------------
# Follow-up
# ---------------------------------------------------------------------------------------------------------------------
FOLLOWUP_KEYWORDS = ("salsa", "hot sauce", "guacamole", "sauce", "dip", "cheese")


def followup_line(trip: Trip) -> CartLine | None:
    """The line worth asking about: a flavor item (salsa first), else a recommended add-on, else the priciest line."""
    lines = list(trip.receipt.lines) if trip.receipt else list(trip.cart.active_lines())
    lines = [line for line in lines if line.status != "declined"]
    if not lines:
        return None
    for kw in FOLLOWUP_KEYWORDS:
        for line in lines:
            if kw in (line.name or line.sku).lower():
                return line
    rec_lines = [line for line in lines if line.rec_id]
    if rec_lines:
        return rec_lines[0]
    return max(lines, key=lambda line: (line.unit_price_cents, line.sku))


def compose_followup_rules(trip: Trip, message_id: str | None = None) -> OutboundMessage:
    line = followup_line(trip)
    name = (line.name or "groceries") if line else "trip"
    text = (
        f"Hi! How was the {name}? Reply 1-5 (5 = loved it), or tell me what to change next time."
        if line
        else "Hi! How was your Picky trip? Reply 1-5."
    )
    return OutboundMessage(
        message_id=message_id,
        channel="photon",
        user_id=trip.user_id,
        trip_id=trip.trip_id,
        text=text,
        about_skus=[line.sku] if line else [],
        quick_replies=["1", "2", "3", "4", "5"],
    )
