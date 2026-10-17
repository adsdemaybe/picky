"""Deterministic scoring + explanation + feedback rules shared by the fake and the real recommender.

Pure functions over contracts models (no I/O, no services). The real service layers embeddings + an LLM rerank on
top of ``score_candidate``; the fake uses it as-is.

Score model (raw, roughly in [-2, 2]; ``interest`` squashes it into [0, 1]):
    pref match      sum of profile.prefs weights over the product's features (tags, category, subcategory, dietary)
    spice           SPICE_GAIN (3.0) x prefs['spicy'] x spice_level / 5 (penalty when the user dislikes spicy)
    history         past rating of this sku (5 -> +0.6 ... 1 -> -0.6), favorite +0.4, bought often +0.2
    similar         shares a subcategory with a sku the user rated >= 4 (+0.3)
    list affinity   product tags overlap the list's words/categories ('taco' tag on a tacos trip) (+0.25)
    deal            + discount_pct / 100 (capped at 0.4)
    near expiry     +0.25 when the user is deal-friendly or already likes the product
    dislikes        product feature in profile.dislikes -> -0.8 each
"""

import hashlib
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

from contracts.catalog import Deal, NearExpiryItem, Product
from contracts.common import JsonScalar, Sku, SlotId, StoreId
from contracts.profile import Feedback, ProfileChange, UserProfile

SPICE_GAIN = 3.0
"""Spice term gain: strong enough that 'too mild' feedback visibly reorders trip 2."""

JUST_BOUGHT_PENALTY = 0.75
"""Bought on the last trip: she has it at home, so trip 2 surfaces something new (e.g. a spicier hot sauce)."""

MIN_RAW_SCORE = 0.15
"""Candidates below this raw score are never recommended (keeps irrelevant items out)."""

DEAL_PREF_KEYS = ("deals", "deal", "markdown", "near_expiry", "bargain", "value")
MEAT_CATEGORIES = {"meat", "seafood", "poultry"}
_WORD = re.compile(r"[a-z]+")

SPICIER_TAGS = {"too_mild", "want_spicier", "spicier", "hotter", "more_heat", "not_spicy_enough"}
MILDER_TAGS = {"too_spicy", "too_hot", "want_milder", "milder", "less_heat"}
SPICIER_WORDS = ("too mild", "spicier", "hotter", "more heat", "not spicy enough", "bland", "more kick")
MILDER_WORDS = ("too spicy", "too hot", "milder", "less heat", "burned")
SPICE_FEATURES = {"mild", "spicy", "extra_hot", "hot"}


# --------------------------------------------------------------------------------------------------------------------
# Candidate = a product as offered at one store right now
# --------------------------------------------------------------------------------------------------------------------
@dataclass
class Candidate:
    product: Product
    store_id: StoreId
    price_cents: int
    list_price_cents: int
    qty: int = 0
    slot_id: SlotId | None = None
    deal: Deal | None = None
    near_expiry: NearExpiryItem | None = None

    @property
    def sku(self) -> Sku:
        return self.product.sku

    @property
    def discount_pct(self) -> int:
        if self.deal is not None and self.deal.percent_off is not None:
            return int(self.deal.percent_off)
        if self.list_price_cents > 0 and self.price_cents < self.list_price_cents:
            return round(100 * (self.list_price_cents - self.price_cents) / self.list_price_cents)
        return 0


@dataclass
class UserHistory:
    """What the recommender remembers beyond the profile: ratings and purchase counts per sku."""

    ratings: dict[Sku, int] = field(default_factory=dict)
    purchase_counts: dict[Sku, int] = field(default_factory=dict)
    just_bought: set[Sku] = field(default_factory=set)
    """SKUs bought on the shopper's last trip (record_purchase): don't push the same detour again right away."""


@dataclass
class Scored:
    candidate: Candidate
    raw: float
    interest: float
    reason: str
    signals: dict[str, JsonScalar]
    near_expiry_driven: bool = False


# --------------------------------------------------------------------------------------------------------------------
# Features
# --------------------------------------------------------------------------------------------------------------------
def product_features(product: Product) -> set[str]:
    feats = {t.lower() for t in product.tags} | {d.lower() for d in product.dietary}
    feats.add(product.category.lower())
    if product.subcategory:
        feats.add(product.subcategory.lower())
    if product.spice_level is not None and product.spice_level >= 3:
        feats.add("spicy")
    return feats


def words(*texts: str | None) -> set[str]:
    out: set[str] = set()
    for t in texts:
        if t:
            for w in _WORD.findall(t.lower()):
                out.add(w)
                if w.endswith("s") and len(w) > 3:  # 'tacos' -> 'taco'
                    out.add(w[:-1])
    return out


def likes_deals(profile: UserProfile) -> bool:
    return any(profile.prefs.get(k, 0.0) > 0 for k in DEAL_PREF_KEYS)


def is_excluded(product: Product, profile: UserProfile) -> bool:
    """Hard filters: disliked sku, allergens, diet conflicts, online-only (not on a shelf)."""
    if product.sku in profile.disliked_skus or product.online_only:
        return True
    if {a.lower() for a in profile.allergies} & {a.lower() for a in product.allergens}:
        return True
    diet = {d.lower() for d in profile.dietary}
    if diet & {"vegetarian", "vegan"} and product.category.lower() in MEAT_CATEGORIES:
        return True
    return bool("vegan" in diet and product.category.lower() == "dairy" and "vegan" not in product.dietary)


def interest_from_raw(raw: float) -> float:
    """Logistic squash into (0, 1), centered so MIN_RAW_SCORE ~ 0.35 and raw 1.0 ~ 0.8."""
    return round(1.0 / (1.0 + math.exp(-2.2 * (raw - 0.4))), 3)


def days_phrase(days_left: int) -> str:
    if days_left <= 0:
        return "expires today"
    if days_left == 1:
        return "expires tomorrow"
    return f"expires in {days_left} days"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


# --------------------------------------------------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------------------------------------------------
def score_candidate(
    cand: Candidate,
    profile: UserProfile,
    history: UserHistory,
    products: dict[Sku, Product],
    context_words: set[str],
    context_label: str | None = None,
    focus_words: set[str] | None = None,
) -> Scored:
    p = cand.product
    feats = product_features(p)
    prefs = profile.prefs
    reasons: list[tuple[float, str]] = []  # (strength, phrase)
    signals: dict[str, JsonScalar] = {}

    # pref match
    matched = sorted(((prefs[f], f) for f in feats if f in prefs and f != "spicy"), reverse=True)
    pref_score = sum(w for w, _ in matched)
    likes = [f for w, f in matched if w > 0]
    if likes:
        reasons.append((min(pref_score, 1.0), f"matches your taste for {' & '.join(likes[:2])}"))
    signals["similarity"] = round(pref_score, 3)
    if likes:
        signals["matched_prefs"] = ",".join(likes[:4])

    # spice
    spice = 0.0
    spicy_w = prefs.get("spicy", 0.0)
    if p.spice_level is not None:
        signals["spice_level"] = p.spice_level
        if spicy_w > 0 and p.spice_level >= 2:
            spice = SPICE_GAIN * spicy_w * p.spice_level / 5.0
            reasons.append((spice + 0.2, "spicy like your favorites"))
        elif spicy_w < 0 and p.spice_level >= 3:
            spice = SPICE_GAIN * spicy_w * p.spice_level / 5.0

    # dislikes
    disliked = sorted(feats & {d.lower() for d in profile.dislikes})
    dislike_pen = -0.8 * len(disliked)
    if disliked:
        signals["disliked"] = ",".join(disliked)

    # history
    hist = 0.0
    rating = history.ratings.get(p.sku)
    if rating is not None:
        signals["past_rating"] = rating
        hist += (rating - 3) * 0.3
        if rating >= 4:
            reasons.append((0.9 + rating / 10, f"you rated {p.name} {rating}★"))
    if p.sku in profile.favorite_skus:
        hist += 0.4
        signals["favorite"] = True
        if rating is None:
            reasons.append((0.8, "one of your favorites"))
    bought = history.purchase_counts.get(p.sku, 0)
    if bought:
        signals["times_bought"] = bought
        if bought >= 2:
            hist += 0.2
            reasons.append((0.5, "you buy this often"))
    if p.sku in history.just_bought:
        hist -= JUST_BOUGHT_PENALTY
        signals["just_bought"] = True

    # similar to something rated highly (same subcategory, different sku)
    similar = 0.0
    if rating is None and p.subcategory:
        for sku, r in sorted(history.ratings.items()):
            other = products.get(sku)
            if r >= 4 and other is not None and other.sku != p.sku and other.subcategory == p.subcategory:
                similar = 0.3
                signals["similar_to"] = other.sku
                reasons.append((0.7, f"similar to {other.name}, which you rated {r}★"))
                break

    # list affinity
    affinity = 0.0
    overlap = sorted(({t.lower() for t in p.tags} | {p.category.lower()}) & context_words)
    tag_overlap = sorted({t.lower() for t in p.tags} & context_words)
    if tag_overlap:
        affinity = 0.25
        signals["list_match"] = ",".join(tag_overlap[:3])
        reasons.append((0.45, f"goes with your {context_label or 'list'}"))
    elif overlap:
        affinity = 0.05

    # deal / near expiry
    deal_score = 0.0
    pct = cand.discount_pct
    if pct > 0:
        signals["discount_pct"] = pct
        deal_score = min(pct / 100.0, 0.4)
    near = 0.0
    phrase_deal = f"{pct}% off" if pct > 0 else ""
    if cand.near_expiry is not None:
        days = cand.near_expiry.days_left
        signals["days_left"] = days
        base_affinity = pref_score + spice + hist + similar + affinity
        if likes_deals(profile) or base_affinity > 0.2:
            near = 0.25
        phrase_deal = f"{phrase_deal}, {days_phrase(days)}" if phrase_deal else f"markdown, {days_phrase(days)}"
    if phrase_deal and (deal_score or near):
        reasons.append((0.4 + deal_score + near, phrase_deal))
    if cand.deal is not None:
        signals["deal_id"] = cand.deal.deal_id

    # focus ('something spicy', 'snacks', 'deals')
    focus_bonus = 0.0
    if focus_words:
        if focus_words & (feats | words(p.name, p.description)):
            focus_bonus = 0.5
            signals["focus_match"] = True
        elif focus_words & {"deal", "deals", "sale", "cheap"} and (pct > 0 or cand.near_expiry is not None):
            focus_bonus = 0.5
            signals["focus_match"] = True

    raw = pref_score + spice + dislike_pen + hist + similar + affinity + deal_score + near + focus_bonus
    signals["score"] = round(raw, 3)
    signals["sponsored"] = False

    reasons.sort(key=lambda r: -r[0])
    phrases: list[str] = []
    for _, phrase in reasons:
        if phrase not in phrases:
            phrases.append(phrase)
    reason = _cap("; ".join(phrases[:2])) if phrases else f"Popular pick in {p.category}"
    # a near-expiry *offer* is a markdown; produce that merely expires soon is a normal (taste) rec
    near_driven = cand.near_expiry is not None and near > 0 and pct > 0
    return Scored(cand, raw, interest_from_raw(raw), reason, signals, near_driven)


def is_markdown_offer(s: Scored) -> bool:
    return s.near_expiry_driven


def is_deal(s: Scored) -> bool:
    return s.candidate.discount_pct > 0 and not s.near_expiry_driven


def rank(
    scored: Iterable[Scored],
    limit: int,
    min_raw: float = MIN_RAW_SCORE,
    diversify: bool = True,
    mix: bool = False,
) -> list[Scored]:
    """Best first, ties broken by sku (deterministic); at most one per subcategory when ``diversify``.

    ``mix`` (trip detours): reserve one slot each for the best taste pick, the best near-expiry markdown and the best
    deal (when they clear ``min_raw``), then fill by score. Same candidates, same scores, just a varied trip."""
    ordered = sorted((s for s in scored if s.raw >= min_raw), key=lambda s: (-s.raw, s.candidate.sku))
    out: list[Scored] = []
    seen_sub: set[str] = set()

    def sub_of(s: Scored) -> str:
        return s.candidate.product.subcategory or s.candidate.sku

    def take(s: Scored) -> None:
        seen_sub.add(sub_of(s))
        out.append(s)

    if mix:
        for pred in (lambda s: not s.near_expiry_driven, is_markdown_offer, is_deal):
            if len(out) >= limit:
                break
            fits = [s for s in ordered if pred(s) and s not in out and not (diversify and sub_of(s) in seen_sub)]
            fresh = [s for s in fits if not s.signals.get("just_bought")]  # something new beats last trip's buy
            pick = (fresh or fits or [None])[0]
            if pick is not None:
                take(pick)
    for s in ordered:
        if len(out) >= limit:
            break
        if s in out or (diversify and sub_of(s) in seen_sub):
            continue
        take(s)
    return sorted(out, key=lambda s: (-s.raw, s.candidate.sku))


def rec_id_for(user_id: str, trip_id: str | None, sku: Sku, kind: str) -> str:
    """Stable rec id (same inputs -> same id) so the transparency log can upsert on it."""
    digest = hashlib.sha1(f"{user_id}|{trip_id or '-'}|{sku}|{kind}".encode()).hexdigest()[:10]
    return f"rec_{digest}"


# --------------------------------------------------------------------------------------------------------------------
# Feedback -> profile changes
# --------------------------------------------------------------------------------------------------------------------
def _clamp(w: float) -> float:
    return round(max(-1.0, min(1.0, w)), 3)


def feedback_changes(
    profile: UserProfile, feedback: Feedback, product: Product | None
) -> tuple[list[ProfileChange], str]:
    """Mutates ``profile`` in place (prefs, favorites, disliked_skus) and returns (changes, shopper-facing summary)."""
    changes: list[ProfileChange] = []
    summary_bits: list[str] = []

    def bump(key: str, delta: float, why: str) -> None:
        old = profile.prefs.get(key, 0.0)
        new = _clamp(old + delta)
        if new != old:
            profile.prefs[key] = new
            changes.append(ProfileChange(field="prefs", key=key, old=old, new=new, reason=why))

    rating = feedback.rating
    name = product.name if product else (feedback.sku or "that")
    tags = {t.lower() for t in feedback.tags}
    text = feedback.text.lower()
    spicier = bool(tags & SPICIER_TAGS) or any(w in text for w in SPICIER_WORDS)
    milder = not spicier and (bool(tags & MILDER_TAGS) or any(w in text for w in MILDER_WORDS))
    if product is not None and rating is not None:
        delta = (rating - 3) * 0.15
        if delta:
            feats = sorted(
                {t.lower() for t in product.tags} | ({product.subcategory} if product.subcategory else set())
            )
            if spicier or milder:  # the complaint is the heat level, not 'salsa' / 'vegan' / 'taco'
                feats = [f for f in feats if f in SPICE_FEATURES]
            for f in feats:
                bump(f, delta, f"rated {product.name} {rating}/5")
        if rating >= 4 and product.sku not in profile.favorite_skus:
            profile.favorite_skus.append(product.sku)
            changes.append(ProfileChange(field="favorite_skus", key=product.sku, new=product.sku, reason="rated >= 4"))
            summary_bits.append(f"{name} is now a favorite")
        if rating <= 2:
            if product.sku in profile.favorite_skus:
                profile.favorite_skus.remove(product.sku)
                changes.append(
                    ProfileChange(field="favorite_skus", key=product.sku, old=product.sku, reason="rated <= 2")
                )
            if product.sku not in profile.disliked_skus:
                profile.disliked_skus.append(product.sku)
                changes.append(
                    ProfileChange(field="disliked_skus", key=product.sku, new=product.sku, reason="rated <= 2")
                )
            summary_bits.append(f"we'll skip {name} next time")

    if spicier:
        bump("spicy", 0.4, "wanted it spicier")
        summary_bits.insert(0, "You like it hotter: spicier picks next")
    elif milder:
        bump("spicy", -0.4, "wanted it milder")
        summary_bits.insert(0, "Got it, milder picks next")
    if tags & {"too_expensive", "pricey"} or "expensive" in text:
        bump("deals", 0.3, "price-sensitive")
        summary_bits.append("we'll favor deals")

    if not summary_bits and rating is not None:
        summary_bits.append(f"Thanks! Noted {rating}/5 for {name}")
    summary = _cap(", ".join(summary_bits)) + "." if summary_bits else "Thanks for the feedback."
    return changes, summary


def near_expiry_days(expiry: date | None, today: date) -> int | None:
    return None if expiry is None else (expiry - today).days
