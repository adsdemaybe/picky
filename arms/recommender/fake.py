"""Deterministic fake RecommenderService (base deps only).

Profiles, purchases and ratings come from ``data/fixtures/users.yaml`` via ``utils.data`` (loaded lazily on first
call). Scoring = tag/category overlap between ``UserProfile.prefs`` and products, minus dislikes, plus deal and
near-expiry boosts (see ``scoring.py``). ``record_feedback`` mutates the in-memory profile, so trip 2 ranks
differently. Store stock/prices/deals come from ``services.catalog`` at call time when wired, else straight from the
planogram files.
"""

from datetime import date

from arms.recommender.scoring import (
    Candidate,
    Scored,
    UserHistory,
    feedback_changes,
    is_excluded,
    near_expiry_days,
    rank,
    rec_id_for,
    score_candidate,
    words,
)
from contracts.catalog import Deal, NearExpiryItem, Product
from contracts.common import ONLINE_STORE_ID, Sku, StoreId, TripId, UserId, utcnow
from contracts.messaging import normalize_handle
from contracts.planogram import DemoUser, Planogram
from contracts.profile import Feedback, ProfileUpdate, PurchaseRecord, UserProfile
from contracts.services import Services
from contracts.shopping import Cart, RecKind, Recommendation, ShoppingList
from utils import data as data_mod
from utils.log import get_logger

log = get_logger(__name__)

NEAR_EXPIRY_WITHIN_DAYS = 3
CHECKOUT_BATCH_MAX = 3


class FakeRecommenderService:
    """In-memory recommender. Inject ``products`` / ``planograms`` / ``users`` in tests; otherwise ``data/`` is used."""

    def __init__(
        self,
        services: Services | None = None,
        *,
        data_root: str | None = None,
        products: dict[Sku, Product] | None = None,
        planograms: dict[StoreId, Planogram] | None = None,
        users: list[DemoUser] | None = None,
        today: date | None = None,
    ) -> None:
        self._services = services or Services()
        self._data_root = data_root
        self._products = products
        self._planograms = planograms
        self._today = today
        self._profiles: dict[UserId, UserProfile] | None = None
        self._history: dict[UserId, UserHistory] = {}
        self._purchases: dict[UserId, list[PurchaseRecord]] = {}
        self._seed_users = users

    # ---------------------------------------------------------------------------------------------------- loading
    def _ensure_loaded(self) -> dict[UserId, UserProfile]:
        if self._profiles is None:
            users = self._seed_users
            if users is None:
                try:
                    users = data_mod.load_demo_fixtures(self._data_root).users
                except data_mod.DataError as exc:
                    log.warning("no demo users loaded (%s); starting with an empty profile store", exc)
                    users = []
            self._profiles = {}
            for u in users:
                self._profiles[u.user_id] = u.profile.model_copy(deep=True)
                hist = self._history.setdefault(u.user_id, UserHistory())
                self._purchases[u.user_id] = [p.model_copy() for p in u.purchases]
                for pr in u.purchases:
                    hist.purchase_counts[pr.sku] = hist.purchase_counts.get(pr.sku, 0) + pr.qty
                    if pr.rating is not None:
                        hist.ratings[pr.sku] = pr.rating
                for fb in u.feedback:
                    if fb.sku and fb.rating is not None:
                        hist.ratings[fb.sku] = fb.rating
        return self._profiles

    def _all_products(self) -> dict[Sku, Product]:
        if self._products is None:
            try:
                self._products = data_mod.load_products(self._data_root)
            except data_mod.DataError as exc:
                log.warning("no products loaded (%s)", exc)
                return {}
        return self._products

    def _today_date(self) -> date:
        return self._today or utcnow().date()

    def _profile(self, user_id: UserId) -> UserProfile:
        profiles = self._ensure_loaded()
        if user_id not in profiles:
            raise KeyError(f"unknown user {user_id!r}")
        return profiles[user_id]

    async def _planogram(self, store_id: StoreId) -> Planogram | None:
        if store_id == ONLINE_STORE_ID:
            return None
        catalog = self._services.catalog
        if catalog is not None:
            try:
                return await catalog.get_planogram(store_id)
            except KeyError:
                return None
            except Exception as exc:  # degrade to the files
                log.warning("catalog.get_planogram(%s) failed (%s); using data/", store_id, exc)
        if self._planograms is not None:
            return self._planograms.get(store_id)
        try:
            return data_mod.load_planogram(store_id, self._data_root)
        except data_mod.DataError as exc:
            log.warning("no planogram for %s (%s)", store_id, exc)
            return None

    async def _deals_and_near(
        self, store_id: StoreId, plan: Planogram
    ) -> tuple[dict[Sku, Deal], dict[Sku, NearExpiryItem]]:
        catalog = self._services.catalog
        if catalog is not None:
            try:
                deals = await catalog.get_deals(store_id)
                near = await catalog.get_near_expiry(store_id, NEAR_EXPIRY_WITHIN_DAYS)
                return (
                    {d.sku: d for d in sorted(deals, key=lambda d: d.price_cents, reverse=True)},
                    {n.sku: n for n in sorted(near, key=lambda n: n.days_left, reverse=True)},
                )
            except Exception as exc:
                log.warning("catalog deals/near-expiry failed for %s (%s); deriving from planogram", store_id, exc)
        return self._derive_deals(store_id, plan)

    def _derive_deals(self, store_id: StoreId, plan: Planogram) -> tuple[dict[Sku, Deal], dict[Sku, NearExpiryItem]]:
        products = self._all_products()
        today = self._today_date()
        deals: dict[Sku, Deal] = {}
        near: dict[Sku, NearExpiryItem] = {}
        for slot in plan.slots:
            prod = products.get(slot.sku)
            if prod is None:
                continue
            list_price = slot.price_cents if slot.price_cents is not None else prod.price_cents
            deal = None
            if slot.deal is not None:
                deal = Deal(
                    deal_id=f"{store_id}:{slot.slot_id}", sku=slot.sku, store_id=store_id, **slot.deal.model_dump()
                )
                deals.setdefault(slot.sku, deal)
            for lot in slot.lots:
                days = near_expiry_days(lot.expiry, today)
                if days is None or days < 0 or days > NEAR_EXPIRY_WITHIN_DAYS or lot.qty <= 0:
                    continue
                cur = near.get(slot.sku)
                if cur is None or days < cur.days_left:
                    price = deal.price_cents if deal is not None else list_price
                    near[slot.sku] = NearExpiryItem(
                        store_id=store_id,
                        sku=slot.sku,
                        slot_id=slot.slot_id,
                        lot=lot,
                        days_left=days,
                        price_cents=price,
                        original_cents=list_price,
                        deal=deal,
                    )
        return deals, near

    async def _candidates(self, store_id: StoreId) -> list[Candidate]:
        plan = await self._planogram(store_id)
        if plan is None:
            return []
        products = self._all_products()
        deals, near = await self._deals_and_near(store_id, plan)
        out: dict[Sku, Candidate] = {}
        for slot in plan.slots:
            prod = products.get(slot.sku)
            if prod is None and self._services.catalog is not None:
                try:
                    prod = await self._services.catalog.get_product(slot.sku)
                except Exception:
                    prod = None
            if prod is None or slot.qty <= 0:
                continue
            list_price = slot.price_cents if slot.price_cents is not None else prod.price_cents
            deal = deals.get(slot.sku)
            price = deal.price_cents if deal is not None else list_price
            if deal is not None and deal.original_cents:
                list_price = deal.original_cents
            prev = out.get(slot.sku)
            if prev is not None and prev.qty >= slot.qty:
                continue
            out[slot.sku] = Candidate(
                product=prod,
                store_id=store_id,
                price_cents=price,
                list_price_cents=list_price,
                qty=slot.qty,
                slot_id=slot.slot_id,
                deal=deal,
                near_expiry=near.get(slot.sku),
            )
        return [out[k] for k in sorted(out)]

    # ---------------------------------------------------------------------------------------------------- scoring
    def _score_all(
        self,
        profile: UserProfile,
        candidates: list[Candidate],
        exclude: set[Sku],
        context_words: set[str],
        context_label: str | None,
        focus: str | None,
    ) -> list[Scored]:
        products = self._all_products()
        history = self._history.setdefault(profile.user_id, UserHistory())
        focus_words = words(focus) if focus else None
        eligible = [c for c in candidates if c.sku not in exclude and not is_excluded(c.product, profile)]
        scored = [
            score_candidate(c, profile, history, products, context_words, context_label, focus_words) for c in eligible
        ]
        if focus_words and not any(s.signals.get("focus_match") for s in scored):
            scored = [
                score_candidate(c, profile, history, products, context_words, context_label, None) for c in eligible
            ]
        return scored

    def _rank(
        self, scored: list[Scored], limit: int, profile: UserProfile, context: str, mix: bool = False
    ) -> list[Scored]:
        """Hook: the real service overrides this (embeddings + LLM rerank)."""
        return rank(scored, limit, mix=mix)

    async def _rank_async(
        self, scored: list[Scored], limit: int, profile: UserProfile, context: str, mix: bool = False
    ) -> list[Scored]:
        return self._rank(scored, limit, profile, context, mix=mix)

    def _to_rec(self, s: Scored, user_id: UserId, trip_id: TripId | None, kind: RecKind) -> Recommendation:
        c = s.candidate
        return Recommendation(
            rec_id=rec_id_for(user_id, trip_id, c.sku, kind),
            user_id=user_id,
            trip_id=trip_id,
            sku=c.sku,
            store_id=c.store_id,
            product_name=c.product.name,
            interest=s.interest,
            reason=s.reason,
            signals=dict(s.signals),
            kind=kind,
            sponsored=False,
            slot_id=c.slot_id,
            price_cents=c.price_cents,
            deal=c.deal,
        )

    # ---------------------------------------------------------------------------------------------------- Protocol
    async def get_profile(self, user_id: UserId) -> UserProfile:
        return self._profile(user_id).model_copy(deep=True)

    async def find_user_by_handle(self, handle: str) -> UserProfile | None:
        target = normalize_handle(handle)
        for uid in sorted(self._ensure_loaded()):
            prof = self._ensure_loaded()[uid]
            for h in (prof.phone, prof.email):
                if h and normalize_handle(h) == target:
                    return prof.model_copy(deep=True)
        return None

    async def save_profile(self, profile: UserProfile) -> UserProfile:
        profiles = self._ensure_loaded()
        old = profiles.get(profile.user_id)
        new = profile.model_copy(deep=True)
        new.version = (old.version + 1) if old is not None else max(profile.version, 1)
        new.updated_at = utcnow()
        profiles[profile.user_id] = new
        self._history.setdefault(profile.user_id, UserHistory())
        return new.model_copy(deep=True)

    async def recommend(
        self,
        user_id: UserId,
        store_id: StoreId,
        shopping_list: ShoppingList,
        limit: int = 3,
        focus: str | None = None,
        exclude_skus: list[Sku] | None = None,
    ) -> list[Recommendation]:
        profile = self._profile(user_id)
        exclude = set(exclude_skus or [])
        for item in shopping_list.items:
            if item.sku:
                exclude.add(item.sku)
            exclude.update(item.substitutes)
        ctx = words(
            shopping_list.title, *(i.query for i in shopping_list.items), *(i.category for i in shopping_list.items)
        )
        label = shopping_list.title.lower() if shopping_list.title else None
        scored = self._score_all(profile, await self._candidates(store_id), exclude, ctx, label, focus)
        top = await self._rank_async(scored, max(limit, 0), profile, shopping_list.title or "your list", mix=True)
        trip_id = shopping_list.trip_id
        return [
            self._to_rec(s, user_id, trip_id, "near_expiry_offer" if s.near_expiry_driven else "detour") for s in top
        ]

    async def checkout_batch(
        self,
        user_id: UserId,
        store_id: StoreId,
        cart: Cart,
        limit: int = 3,
        focus: str | None = None,
        exclude_skus: list[Sku] | None = None,
    ) -> list[Recommendation]:
        profile = self._profile(user_id)
        exclude = set(exclude_skus or []) | {line.sku for line in cart.lines}
        ctx = words(*(line.name for line in cart.lines), *(line.category for line in cart.lines))
        scored = self._score_all(profile, await self._candidates(store_id), exclude, ctx, "cart", focus)
        top = await self._rank_async(scored, max(0, min(limit, CHECKOUT_BATCH_MAX)), profile, "checkout")
        return [self._to_rec(s, user_id, cart.trip_id, "checkout_batch") for s in top]

    async def near_expiry_offers(
        self,
        user_id: UserId,
        store_id: StoreId,
        limit: int = 3,
        focus: str | None = None,
        exclude_skus: list[Sku] | None = None,
        trip_id: TripId | None = None,
    ) -> list[Recommendation]:
        profile = self._profile(user_id)
        cands = [c for c in await self._candidates(store_id) if c.near_expiry is not None]
        scored = self._score_all(profile, cands, set(exclude_skus or []), set(), None, focus)
        # only users likely to want them: the near-expiry boost must have applied (deal-friendly or affinity)
        scored = [s for s in scored if s.near_expiry_driven or s.signals.get("score", 0) >= 0.5]
        top = rank(scored, max(limit, 0), diversify=False)
        return [self._to_rec(s, user_id, trip_id, "near_expiry_offer") for s in top]

    async def record_feedback(self, feedback: Feedback) -> ProfileUpdate:
        profile = self._profile(feedback.user_id)
        before = profile.version
        product = self._all_products().get(feedback.sku) if feedback.sku else None
        if product is None and feedback.sku and self._services.catalog is not None:
            try:
                product = await self._services.catalog.get_product(feedback.sku)
            except Exception:
                product = None
        hist = self._history.setdefault(feedback.user_id, UserHistory())
        if feedback.sku and feedback.rating is not None:
            hist.ratings[feedback.sku] = feedback.rating
        changes, summary = feedback_changes(profile, feedback, product)
        if changes:
            profile.version = before + 1
            profile.updated_at = utcnow()
        return ProfileUpdate(
            user_id=feedback.user_id,
            trip_id=feedback.trip_id,
            source="feedback",
            changes=changes,
            summary=summary,
            version_before=before,
            version_after=profile.version,
        )

    async def record_purchase(self, records: list[PurchaseRecord]) -> None:
        for user_id in {r.user_id for r in records}:  # a new trip's basket replaces the last one
            self._history.setdefault(user_id, UserHistory()).just_bought = {
                r.sku for r in records if r.user_id == user_id
            }
        for r in records:
            hist = self._history.setdefault(r.user_id, UserHistory())
            hist.purchase_counts[r.sku] = hist.purchase_counts.get(r.sku, 0) + r.qty
            if r.rating is not None:
                hist.ratings[r.sku] = r.rating
            self._purchases.setdefault(r.user_id, []).append(r.model_copy())

    # ---------------------------------------------------------------------------------------------------- extras
    def purchases(self, user_id: UserId) -> list[PurchaseRecord]:
        """Purchase history (debug/tests)."""
        self._ensure_loaded()
        return list(self._purchases.get(user_id, []))


def create_fake(services: Services | None = None) -> FakeRecommenderService:
    return FakeRecommenderService(services)
