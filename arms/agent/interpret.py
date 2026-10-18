"""Deterministic natural-language request interpretation (``AgentService.interpret_request``, rules path).

The fleet calls ``interpret_request`` for everything a user types into the web app ("tacos for 6, under $40, one
vegetarian", "scan dairy at Hill", "Hazel, go charge", "where is the salsa?"). The rules here are the fake and the
fallback of the real (Claude) path. Shop / fetch lists come from ``logic.expand_intent_rules`` so the taco demo list
is identical to ``expand_intent``'s. Robot / store / category / aisle hints are resolved against the data files and
the catalog at call time; anything unknown is dropped (the LLM proposes, code decides).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from arms.agent import logic
from contracts.catalog import STANDARD_CATEGORY_IDS
from contracts.common import ONLINE_STORE_ID, RobotId, StoreId, UserId, money
from contracts.fleet import GoalPriority, RequestDraft, RequesterRole, RequestIntent
from contracts.profile import UserProfile
from contracts.services import CatalogService
from contracts.shopping import Intent
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)

# Fallback when data/fixtures/robots.yaml is missing (fixed ids, CLAUDE.md).
DEFAULT_ROBOTS: dict[RobotId, tuple[str, StoreId]] = {
    "rover-ave-1": ("Pico", "store_ave"),
    "rover-ave-2": ("Rhea", "store_ave"),
    "rover-hill-1": ("Hazel", "store_hill"),
}
# Words that name a store in text -> store id (extended with planogram / catalog names at call time).
STORE_WORDS: dict[str, StoreId] = {
    "the ave": "store_ave",
    "ave": "store_ave",
    "avenue": "store_ave",
    "u district": "store_ave",
    "hill": "store_hill",
    "capitol hill": "store_hill",
}
CATEGORY_WORDS: dict[str, tuple[str, ...]] = {
    "produce": ("produce", "fruit", "fruits", "vegetable", "vegetables", "veggie aisle", "greens"),
    "dairy": ("dairy", "milk", "cheese", "yogurt", "eggs", "butter"),
    "meat": ("meat", "seafood", "fish", "chicken", "beef", "pork", "butcher"),
    "deli": ("deli",),
    "bakery": ("bakery", "bread", "tortillas"),
    "pantry": ("pantry", "canned", "dry goods", "pasta", "rice", "condiments"),
    "international": ("international", "mexican", "asian", "world foods"),
    "snacks": ("snacks", "snack", "chips", "candy", "cookies"),
    "beverages": ("beverages", "drinks", "soda", "juice", "water", "coffee"),
    "frozen": ("frozen", "freezer", "ice cream"),
    "household": ("household", "cleaning", "paper goods", "paper towels", "detergent"),
    "personal_care": ("personal care", "toiletries", "shampoo", "soap", "toothpaste"),
    "alcohol": ("alcohol", "beer", "wine"),
}

_DOCK_RE = re.compile(
    r"\b(?:go (?:and )?(?:charge|recharge)|charge (?:up|yourself)?|recharge|return to (?:the |your )?(?:dock|base)|"
    r"go (?:back )?(?:to )?(?:the |your )?dock|go home|head home|back to (?:the |your )?dock|dock(?: now)?|"
    r"plug (?:in|yourself in))\b",
    re.I,
)
_SCAN_RE = re.compile(
    r"\b(?:scan|rescan|audit|inventory|stock ?check|check (?:the )?(?:stock|shelves|inventory)|count (?:the )?(?:stock|"
    r"shelves)|shelf scan|cycle count)\b",
    re.I,
)
_FETCH_RE = re.compile(r"^\s*(?:please\s+)?(?:fetch|bring(?: me| up)?|price ?check|go get)\b", re.I)
_QUESTION_RE = re.compile(
    r"^\s*(?:where|what|how|when|why|who|which|is|are|did|does|do|can|could|will|has|have|status)\b|\?\s*$", re.I
)
_CHAT_RE = re.compile(
    r"^\s*(?:hi|hello|hey|yo|thanks|thank you|thx|cool|great|ok|okay|nice|good (?:morning|night|evening)|bye)\b"
    r"[\s!.]*$",
    re.I,
)
_STATUS_RE = re.compile(
    r"\b(?:status|progress|how(?:'s| is) it going|where(?:'s| is| are) (?:my|the) (?:robot|order|stuff|groceries|"
    r"cart)|eta|how long|done yet|what(?:'s| is) (?:\w+ )?(?:doing|thinking)|battery)\b",
    re.I,
)
_URGENT_RE = re.compile(r"\b(?:asap|urgent(?:ly)?|right now|immediately|hurry)\b", re.I)
_AISLE_RE = re.compile(r"\baisle\s*(\d+)\b", re.I)
_VAGUE_RE = re.compile(
    r"^\s*(?:i need |i want |get |buy |pick up |grab )?(?:some )?(?:stuff|things|groceries|food|shopping|"
    r"something|a few things|the usual)\s*[.!]*\s*$",
    re.I,
)


@dataclass
class Hints:
    """Known robots / stores / categories / aisles to resolve names against."""

    robots: dict[RobotId, tuple[str, StoreId]] = field(default_factory=lambda: dict(DEFAULT_ROBOTS))
    store_words: dict[str, StoreId] = field(default_factory=lambda: dict(STORE_WORDS))
    store_names: dict[StoreId, str] = field(default_factory=dict)
    categories: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(CATEGORY_WORDS))
    aisles: dict[StoreId, dict[str, str]] = field(default_factory=dict)
    """store -> {label lower ('aisle 3') -> aisle_id}."""


def load_hints() -> Hints:
    """Hints from data/ (robots.yaml, planograms, categories.yaml); every source is optional."""
    hints = Hints()
    try:
        from utils.data import load_robots

        robots = load_robots()
        if robots:
            hints.robots = {rid: (spec.name, spec.store_id) for rid, spec in robots.items()}
    except Exception as e:  # robots.yaml missing / invalid: keep the fixed ids
        log.debug("robots.yaml unavailable for hints: %s", e)
    try:
        from utils.data import load_planograms

        for sid, plan in load_planograms().items():
            hints.store_names[sid] = plan.name
            for word in _store_name_words(plan.name):
                hints.store_words.setdefault(word, sid)
            hints.aisles[sid] = {a.label.lower(): a.aisle_id for a in plan.aisles}
    except Exception as e:
        log.debug("planograms unavailable for hints: %s", e)
    try:
        from utils.data import load_categories

        for cid, cat in load_categories().items():
            words = {cid.replace("_", " "), cat.name.lower()}
            words |= {w.strip() for w in re.split(r"[&,/]| and ", cat.name.lower()) if len(w.strip()) > 2}
            hints.categories[cid] = tuple(sorted(set(hints.categories.get(cid, ())) | words))
    except Exception as e:
        log.debug("categories unavailable for hints: %s", e)
    return hints


def _store_name_words(name: str) -> list[str]:
    """'Picky Market - The Ave' -> ['picky market - the ave', 'the ave']."""
    low = name.lower().strip()
    out = [low]
    if " - " in low:
        out.append(low.split(" - ", 1)[1].strip())
    return out


def _has(text: str, word: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text, re.I) is not None


def find_robot(text: str, hints: Hints) -> RobotId | None:
    for rid, (name, _store) in hints.robots.items():
        if _has(text, name) or _has(text, rid):
            return rid
    return None


def find_store(text: str, hints: Hints) -> StoreId | None:
    low = text.lower()
    for sid in hints.store_names:
        if _has(low, sid):
            return sid
    for word, sid in sorted(hints.store_words.items(), key=lambda kv: -len(kv[0])):
        if _has(low, word):
            return sid
    return None


def find_categories(text: str, hints: Hints) -> list[str]:
    out: list[str] = []
    for cid, words in hints.categories.items():
        if any(_has(text, w) for w in words) and cid not in out:
            out.append(cid)
    top = [c for c in out if c in STANDARD_CATEGORY_IDS]
    return top + [c for c in out if c not in top]


def find_aisles(text: str, store_id: StoreId | None, hints: Hints) -> list[str]:
    if store_id is None:
        return []
    labels = hints.aisles.get(store_id, {})
    out: list[str] = []
    for m in _AISLE_RE.finditer(text):
        aid = labels.get(f"aisle {m.group(1)}")
        if aid and aid not in out:
            out.append(aid)
    return out


def combined_text(text: str, history: list[str] | None) -> str:
    """Original request + the user's answers (history = [original, question, answer, question, ...] oldest first)."""
    if not history:
        return text
    user_turns = [h for n, h in enumerate(history) if n % 2 == 0 and not _VAGUE_RE.match(h)]
    return ", ".join([*user_turns, text])


def _priority(text: str) -> GoalPriority:
    return "urgent" if _URGENT_RE.search(text) else "normal"


async def answer_question(catalog: CatalogService | None, text: str, store_id: StoreId | None) -> str | None:
    """'where is the salsa?' -> 'Casa Salsa Mild is in Aisle 3 at store_ave, $3.49.' (None = not a product question)."""
    from arms.agent.fake import _WHERE_RE

    m = _WHERE_RE.search(text.strip())
    if not m or catalog is None:
        return None
    query = m.group(1).strip()
    stores = [store_id, ONLINE_STORE_ID] if store_id else None
    hits = await logic.search_candidates(catalog, query, store_ids=stores, limit=5)
    if not hits:
        return f"I couldn't find {query} in our stores or online."
    hit = hits[0]
    offers = [o for o in hit.offers if o.in_stock and o.store_id != ONLINE_STORE_ID]
    if store_id:
        offers = [o for o in offers if o.store_id == store_id] or offers
    if offers:
        o = offers[0]
        where = o.location.aisle_label if o.location and o.location.aisle_label else "on the shelves"
        return f"{hit.product.name} is in {where} at {o.store_id}, {money(o.price_cents)}."
    online = hit.offer_for(ONLINE_STORE_ID)
    if online is not None and online.in_stock:
        return f"{hit.product.name} is out of stock in store; online it's {money(online.price_cents)}."
    return f"{hit.product.name} is out of stock right now."


async def interpret_rules(
    catalog: CatalogService | None,
    text: str,
    user_id: UserId,
    store_id: StoreId | None = None,
    *,
    role: RequesterRole = "shopper",
    profile: UserProfile | None = None,
    history: list[str] | None = None,
    hints: Hints | None = None,
) -> RequestDraft:
    """Keyword rules -> RequestDraft (fleet decides stores, goals and robots)."""
    hints = hints or load_hints()
    full = combined_text(text, history)
    robot = find_robot(full, hints)
    store = find_store(full, hints)
    if store is None and robot is not None:
        store = hints.robots[robot][1]
    store = store or store_id
    robot_name = hints.robots[robot][0] if robot else None
    priority = _priority(full)

    def draft(intent: RequestIntent, summary: str, reply: str, **kw: object) -> RequestDraft:
        return RequestDraft(
            intent=intent,
            summary=summary,
            reply=reply,
            store_hint=store,
            robot_hint=robot,
            priority=priority,
            **kw,  # type: ignore[arg-type]
        )

    # 1) go charge / back to the dock
    if _DOCK_RE.search(full):
        who = robot_name or "The robot"
        return draft("return_to_dock", f"{who} returns to its dock to charge", f"{who} is heading to the dock.")

    # 2) shelf scan (staff): categories / aisles / whole store
    if _SCAN_RE.search(full):
        cats = find_categories(full, hints)
        aisles = find_aisles(full, store, hints)
        what = ", ".join(cats) if cats else ("aisles " + ", ".join(aisles) if aisles else "the whole store")
        where = f" at {hints.store_names.get(store, store)}" if store else ""
        return draft(
            "shelf_scan",
            f"Scan {what}{where}",
            f"Scanning {what}{where}; stock levels update as I go.",
            categories=cats,
            aisle_ids=aisles,
        )

    # 3) plain chat
    if _CHAT_RE.match(full):
        return draft(
            "chat",
            "Small talk",
            "Hi! Tell me what you need, e.g. 'tacos for 6, under $40, one vegetarian'.",
        )

    # 4) status questions / product questions (reply only)
    parsed = logic.parse_intent_text(full, profile)
    is_question = bool(_QUESTION_RE.search(text))
    if _STATUS_RE.search(text) and not parsed.dish:
        who = robot_name or "Your robot"
        return draft("status", "Status question", f"{who} is on it; watch the live view for each step.")
    if is_question and not parsed.dish:
        answer = await answer_question(catalog, text, store)
        if answer is not None:
            return draft("status", f"Question: {text.strip()}", answer)
        hint = "Good question! I can shop for you, find items or scan shelves."
        return draft("chat", f"Question: {text.strip()}", hint)

    # 5) vague: ask what they want
    if _VAGUE_RE.match(text) and not history:
        return draft(
            "shop",
            "Shopping request without items",
            "What should I get?",
            clarifying_question="What would you like me to get? For example 'milk, eggs and bread under $20'.",
            confidence=0.3,
        )

    # 6) fetch (staff only: shopper purchases are always shop goals)
    intent_kind: RequestIntent = "fetch" if role == "staff" and _FETCH_RE.search(full) else "shop"
    sl = await logic.expand_intent_rules(
        catalog, Intent(intent_id=new_id("int"), user_id=user_id, text=full, trip_id=None), profile
    )
    if not sl.items:
        return draft(
            "shop",
            "Shopping request without items",
            "What should I get?",
            clarifying_question="Sorry, I didn't catch the items. What would you like me to get?",
            confidence=0.3,
        )
    if intent_kind == "fetch":
        sl = sl.model_copy(update={"items": sl.items[:1]})
    n = len(sl.items)
    title = sl.title or (sl.items[0].query if n == 1 else f"{n} items")
    budget = f", under {money(sl.budget_cents)}" if sl.budget_cents else ""
    diet = f", {', '.join(sl.dietary_notes)}" if sl.dietary_notes else ""
    summary = f"{title}{budget}{diet}"
    if intent_kind == "fetch":
        reply = f"Fetching {sl.items[0].query}."
    else:
        reply = f"On it: {title} ({n} item{'s' if n != 1 else ''}{budget})."
    return draft(intent_kind, summary, reply, shopping_list=sl)
