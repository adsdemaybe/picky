"""Deterministic keyword interpretation of fleet requests (the fake fleet's NLU, and the real fleet's fallback when
``agent.interpret_request`` fails). Pure functions: no services, no I/O.

What it recognizes (in this order):
- return_to_dock: "Hazel, go charge", "send Pico back to the dock", "return to dock"
- shelf_scan:     "scan the dairy aisle at Hill", "audit snacks", "check stock in aisle 3", "inventory count"
- fetch (staff):  "Rhea, bring a gallon of milk to checkout" (shoppers asking to bring/grab things = shop)
- status:         questions ("where is my order?", "how's it going?", "status")
- chat:           greetings / thanks
- shop:           everything else with content ("tacos for 6 under $40", "milk, eggs and bread")
- needs_input:    shop requests too vague to plan ("get me some stuff")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from contracts.common import AisleId, CategoryId, RobotId, StoreId
from contracts.fleet import RequestDraft, RequesterRole

_DOCK_RE = re.compile(
    r"\b(go (and )?charge|charge (up|yourself)?|recharge|charging|go (back )?(home|to (the |your )?dock)"
    r"|back to (the |your )?dock|return(ing)? to (the |your )?dock|dock yourself|park (at|on) (the |your )?dock)\b"
)
_SCAN_RE = re.compile(
    r"\b(scan|rescan|audit|inventory|stock ?check|shelf ?check|count (the )?(stock|shelves|items)"
    r"|check (the )?(stock|shelves|inventory|shelf)|check (on )?(what'?s|what is) (in stock|on the shelves))\b"
)
_FETCH_RE = re.compile(r"\b(bring|fetch|go get|grab|pick up|get me)\b")
_QUESTION_RE = re.compile(
    r"^(where|what|how|when|why|who|is|are|did|does|do|has|have|can you tell|status)\b|\bstatus\b|\beta\b"
    r"|\bhow('?s| is) (it|my|the)\b|\bwhere('?s| is| are)\b"
)
_CHAT_RE = re.compile(r"^(hi|hello|hey|yo|thanks|thank you|thx|good (morning|evening|night)|ok|okay|cool)\b[\s!.]*$")
_SHOP_CUE_RE = re.compile(
    r"\b(buy|shop|shopping|get|need|pick up|grab|order|for \d+|for (two|three|four|five|six|eight|ten)"
    r"|under \$|\$\d+|budget|list)\b"
)
_STRONG_SHOP_RE = re.compile(r"\b(buy|shop for|get me|for \d+|under \$|\$\d+|budget)\b")
_FETCH_TAIL_RE = re.compile(
    r"\b(?:to|from|at|in|for)\b (?:the )?(?:checkout|front|counter|desk|register|customer|aisle|store)"
)
_AISLE_RE = re.compile(r"\baisle\s*#?\s*([0-9]+|[a-z])\b")
_VAGUE = frozenset({"stuff", "things", "something", "anything", "groceries", "grocery", "food", "some", "items"})
_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "me",
        "my",
        "i",
        "we",
        "us",
        "please",
        "can",
        "could",
        "you",
        "would",
        "want",
        "like",
        "to",
        "for",
        "of",
        "and",
        "or",
        "some",
        "get",
        "buy",
        "need",
        "shop",
        "go",
        "pick",
        "up",
        "grab",
        "order",
        "at",
        "from",
        "in",
        "on",
        "store",
        "it",
        "just",
        "few",
        "couple",
    }
)


@dataclass
class RuleContext:
    """What the rules can name: robots, stores and categories (built by the fleet from data + catalog)."""

    robots: dict[RobotId, str] = field(default_factory=dict)
    """robot_id -> shopper-facing name ('rover-hill-1' -> 'Hazel')."""
    robot_stores: dict[RobotId, StoreId] = field(default_factory=dict)
    stores: dict[StoreId, list[str]] = field(default_factory=dict)
    """store_id -> keywords ('store_hill' -> ['store_hill', 'hill', 'capitol hill'])."""
    categories: dict[CategoryId, list[str]] = field(default_factory=dict)
    """category_id -> keywords ('dairy' -> ['dairy', 'dairy & eggs', 'eggs'])."""
    aisles: dict[StoreId, dict[AisleId, str]] = field(default_factory=dict)
    """store_id -> {aisle_id: label} ('Aisle 3')."""


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower().replace("’", "'")).strip()


def _has(text: str, phrase: str) -> bool:
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def store_keywords(store_id: StoreId, name: str | None) -> list[str]:
    """Words that name a store: its id, the id without 'store_', and the part of its name after a dash
    ('Picky Express - Capitol Hill' -> 'capitol hill', 'hill'; 'Picky Market - The Ave' -> 'the ave', 'ave')."""
    words = [store_id.lower()]
    short = store_id.lower().removeprefix("store_")
    if short and short != store_id.lower():
        words.append(short)
    if name:
        n = _norm(name)
        words.append(n)
        tail = re.split(r"\s[-–—]\s|:\s", n)[-1].strip()
        if tail and tail != n:
            words.append(tail)
            core = tail.removeprefix("the ").strip()
            if core:
                words.append(core)
                last = core.split()[-1]
                if len(last) >= 3:
                    words.append(last)
    out: list[str] = []
    for w in words:
        if w and w not in out:
            out.append(w)
    return out


def category_keywords(category_id: CategoryId, name: str | None, aliases: list[str] | None = None) -> list[str]:
    words = [category_id.lower(), category_id.lower().replace("_", " ")]
    if name:
        n = _norm(name)
        words.append(n)
        for part in re.split(r"\s*(?:&|and|,|/)\s*", n):
            part = part.strip()
            if len(part) >= 4 and part not in _STOP:
                words.append(part)
    words += [_norm(a) for a in aliases or []]
    out: list[str] = []
    for w in words:
        if w and w not in out:
            out.append(w)
    return out


def find_robot(text: str, ctx: RuleContext) -> RobotId | None:
    t = _norm(text)
    for rid, name in ctx.robots.items():
        if _has(t, rid) or (name and _has(t, name)):
            return rid
    return None


def find_store(text: str, ctx: RuleContext) -> StoreId | None:
    t = _norm(text)
    best: tuple[int, StoreId] | None = None
    for sid, words in ctx.stores.items():
        for w in words:
            if _has(t, w) and (best is None or len(w) > best[0]):
                best = (len(w), sid)
    return best[1] if best else None


def find_categories(text: str, ctx: RuleContext) -> list[CategoryId]:
    t = _norm(text)
    out: list[CategoryId] = []
    for cid, words in ctx.categories.items():
        if any(_has(t, w) for w in words) and cid not in out:
            out.append(cid)
    return out


def find_aisles(text: str, ctx: RuleContext, store_id: StoreId | None) -> list[AisleId]:
    t = _norm(text)
    wanted = [m.group(1) for m in _AISLE_RE.finditer(t)]
    if not wanted:
        return []
    out: list[AisleId] = []
    stores = [store_id] if store_id else list(ctx.aisles)
    for sid in stores:
        for aid, label in ctx.aisles.get(sid, {}).items():
            lab = _norm(label)
            for w in wanted:
                if lab in (f"aisle {w}", f"aisle{w}") or aid.lower() in (w, f"aisle-{w}", f"aisle_{w}", f"a{w}"):
                    if aid not in out:
                        out.append(aid)
    return out


def _content_words(text: str, ctx: RuleContext) -> list[str]:
    t = _norm(text)
    for words in ctx.stores.values():
        for w in words:
            t = re.sub(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", " ", t)
    for rid, name in ctx.robots.items():
        t = t.replace(rid, " ")
        if name:
            t = re.sub(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])", " ", t)
    return [w for w in re.findall(r"[a-z][a-z']+", t) if w not in _STOP]


def fetch_query(text: str, ctx: RuleContext) -> tuple[str, int]:
    """'Rhea, bring 2 gallons of milk to checkout' -> ('gallons of milk', 2)."""
    t = _norm(text)
    m = _FETCH_RE.search(t)
    rest = t[m.end() :] if m else t
    rest = _FETCH_TAIL_RE.split(rest)[0]
    for rid, name in ctx.robots.items():
        rest = rest.replace(rid, " ")
        if name:
            rest = re.sub(r"(?<![a-z0-9])" + re.escape(name.lower()) + r"(?![a-z0-9])", " ", rest)
    rest = re.sub(r"[,.!?]", " ", rest)
    qty = 1
    q = re.match(r"\s*(?:me |us )?(\d+)\s*(?:x\s*)?", rest)
    if q:
        qty = max(1, min(int(q.group(1)), 24))
        rest = rest[q.end() :]
    rest = re.sub(r"^\s*(me|us)\b", " ", rest)
    rest = re.sub(r"^\s*(a|an|the|some|one)\b", " ", rest.strip())
    return re.sub(r"\s+", " ", rest).strip() or "item", qty


def interpret(
    text: str,
    *,
    role: RequesterRole = "shopper",
    store_id: StoreId | None = None,
    ctx: RuleContext | None = None,
) -> RequestDraft:
    """Keyword interpretation -> RequestDraft (shop / fetch drafts carry NO shopping list: the fleet fills it via
    the agent arm; fetch puts the item query in ``summary`` after 'fetch: ')."""
    ctx = ctx or RuleContext()
    t = _norm(text)
    robot = find_robot(t, ctx)
    store = find_store(t, ctx) or (ctx.robot_stores.get(robot) if robot else None) or store_id
    base = {"robot_hint": robot, "store_hint": store}

    if _DOCK_RE.search(t):
        name = ctx.robots.get(robot or "", None)
        if robot is None:
            return RequestDraft(
                intent="return_to_dock",
                summary="Send a robot to its dock",
                clarifying_question="Which robot should go charge? " + ", ".join(sorted(ctx.robots.values())),
                confidence=0.5,
                **base,
            )
        return RequestDraft(
            intent="return_to_dock",
            summary=f"{name} returns to its dock to charge",
            priority="high",
            reply=f"Sending {name} back to the dock to charge.",
            **base,
        )

    if _SCAN_RE.search(t):
        cats = find_categories(t, ctx)
        aisles = find_aisles(t, ctx, store)
        what = ", ".join(cats + aisles) if (cats or aisles) else "the whole store"
        return RequestDraft(
            intent="shelf_scan",
            summary=f"Scan {what}",
            categories=cats,
            aisle_ids=aisles,
            reply=f"Starting a shelf scan of {what}.",
            **base,
        )

    if role == "staff" and _FETCH_RE.search(t) and not _QUESTION_RE.search(t):
        query, qty = fetch_query(t, ctx)
        return RequestDraft(
            intent="fetch",
            summary=f"fetch: {query}" + (f" x{qty}" if qty > 1 else ""),
            reply=f"Fetching {query}.",
            **base,
        )

    if _CHAT_RE.match(t):
        return RequestDraft(
            intent="chat",
            summary="Small talk",
            reply="Hi! Tell me what you need, e.g. 'tacos for 6, under $40, one vegetarian'.",
            **base,
        )

    question = _QUESTION_RE.search(t) is not None
    if (question and not _STRONG_SHOP_RE.search(t)) or (t.endswith("?") and not _SHOP_CUE_RE.search(t)):
        return RequestDraft(intent="status", summary="Status question", **base)

    words = _content_words(t, ctx)
    if not words or all(w in _VAGUE for w in words):
        return RequestDraft(
            intent="shop",
            summary="Shopping request (unclear)",
            clarifying_question="What should I get? For example: 'tacos for 6, under $40, one vegetarian'.",
            confidence=0.3,
            **base,
        )
    return RequestDraft(intent="shop", summary=text.strip()[:120], **base)


__all__ = [
    "RuleContext",
    "category_keywords",
    "fetch_query",
    "find_aisles",
    "find_categories",
    "find_robot",
    "find_store",
    "interpret",
    "store_keywords",
]
