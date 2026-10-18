"""Real RecommenderService: bge-small embeddings (+ pgvector) -> similarity blend -> LLM rerank + "why".

Builds on the fake's profile store, candidate building and deterministic scoring (``FakeRecommenderService``), and
replaces the ranking step:

1. Heuristic shortlist (``scoring.score_candidate``) of the eligible in-stock candidates (~4x limit).
2. Embedding similarity: cosine(profile taste vector, product vector) with ``BAAI/bge-small-en-v1.5``
   (``EMBED_DIM`` = 384, extra ``recommender``). Without sentence-transformers it degrades to the heuristic only.
3. LLM rerank of the shortlist via ``utils.llm.complete_json`` (kind='brain', Gemini by default): new order + a
   one-line "why". Without an LLM key (``LLMNotConfigured``) or on malformed output it keeps the heuristic
   order/reasons.
The LLM never sees prices it could change and never decides money; ``sponsored`` is always False.

TODO (numbered, maps to CLAUDE.md §3 recommender scope):
  1. [P0] Persist profiles / feedback / purchases in Postgres (tables profiles, profile_updates, purchases, feedback in
     contracts/db/schema.sql) instead of memory -> ``_pg_load_profiles`` / ``_pg_save_profile``.
  2. [P1] Store product vectors in ``product_embeddings`` (pgvector, HNSW cosine) and query with ``<=>`` instead of
     the in-process numpy scan -> ``_pg_similar``.
  3. [P1] Persist ``UserProfile.taste_vector`` after each feedback/purchase (``_refresh_taste_vector`` does the math).
  4. [P2] Voyage ``voyage-4-lite`` upgrade (separate column; never mix models in one column).
"""

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from arms.recommender.fake import FakeRecommenderService
from arms.recommender.scoring import Scored, interest_from_raw, rank
from contracts.common import EMBED_DIM, Sku
from contracts.profile import Feedback, ProfileUpdate, PurchaseRecord, UserProfile
from contracts.services import Services
from utils.log import get_logger

log = get_logger(__name__)

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
SIM_WEIGHT = 1.2
"""How much cosine similarity (in [-1, 1]) adds to the heuristic raw score."""
SHORTLIST_FACTOR = 4
RERANK_MAX = 8


class _RerankItem(BaseModel):
    sku: str
    interest: float = Field(ge=0.0, le=1.0)
    why: str = Field(min_length=1, max_length=160)


class _Rerank(BaseModel):
    items: list[_RerankItem]


def product_text(p: Any) -> str:
    """Text embedded per product (research.md §9.7): name | brand | category | tags | description."""
    return " | ".join(x for x in [p.name, p.brand or "", p.category, " ".join(p.tags), p.description] if x)


class Embedder:
    """Lazy sentence-transformers wrapper; ``available`` is False when the extra isn't installed."""

    def __init__(self, model_name: str = EMBED_MODEL) -> None:
        self.model_name = model_name
        self._model: Any = None
        self.available: bool | None = None

    def _load(self) -> Any:
        if self.available is False:
            return None
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer  # extra: recommender
            except ImportError:
                log.warning("sentence-transformers not installed (uv sync --extra recommender); heuristic only")
                self.available = False
                return None
            self._model = SentenceTransformer(self.model_name)
            self.available = True
        return self._model

    def encode(self, texts: list[str]) -> Any:
        """(n, EMBED_DIM) L2-normalized numpy array, or None when unavailable."""
        model = self._load()
        if model is None or not texts:
            return None
        vecs = model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)
        if vecs.shape[1] != EMBED_DIM:
            raise ValueError(f"{self.model_name} produced dim {vecs.shape[1]}, contracts EMBED_DIM={EMBED_DIM}")
        return vecs


class RecommenderService(FakeRecommenderService):
    def __init__(self, services: Services | None = None, **kwargs: Any) -> None:
        super().__init__(services, **kwargs)
        self._embedder = Embedder()
        self._vec_cache: dict[Sku, Any] = {}

    # ------------------------------------------------------------------------------------------------ embeddings
    async def _product_vectors(self, skus: list[Sku]) -> dict[Sku, Any]:
        missing = [s for s in skus if s not in self._vec_cache]
        products = self._all_products()
        texts = [product_text(products[s]) for s in missing if s in products]
        keys = [s for s in missing if s in products]
        if texts:
            vecs = await asyncio.to_thread(self._embedder.encode, texts)
            if vecs is None:
                return {}
            for k, v in zip(keys, vecs, strict=True):
                self._vec_cache[k] = v
        return {s: self._vec_cache[s] for s in skus if s in self._vec_cache}

    async def _taste_vector(self, profile: UserProfile) -> Any:
        """Weighted mean of liked/purchased/highly-rated product vectors minus disliked ones (normalized)."""
        import numpy as np

        hist = self._history.get(profile.user_id)
        weights: dict[Sku, float] = {s: 1.0 for s in profile.favorite_skus}
        if hist is not None:
            for sku, r in hist.ratings.items():
                weights[sku] = weights.get(sku, 0.0) + (r - 3) * 0.5
            for sku, n in hist.purchase_counts.items():
                weights[sku] = weights.get(sku, 0.0) + 0.2 * min(n, 3)
        for sku in profile.disliked_skus:
            weights[sku] = weights.get(sku, 0.0) - 1.0
        vecs = await self._product_vectors(sorted(weights))
        if not vecs:
            return None
        acc = np.zeros(EMBED_DIM, dtype="float32")
        for sku, v in vecs.items():
            acc += weights[sku] * v
        norm = float(np.linalg.norm(acc))
        return acc / norm if norm > 0 else None

    async def _refresh_taste_vector(self, profile: UserProfile) -> None:
        vec = await self._taste_vector(profile)
        if vec is not None:
            profile.taste_vector = [float(x) for x in vec]

    # ------------------------------------------------------------------------------------------------ ranking
    async def _rank_async(
        self, scored: list[Scored], limit: int, profile: UserProfile, context: str, mix: bool = False
    ) -> list[Scored]:
        shortlist = rank(scored, max(limit * SHORTLIST_FACTOR, limit), diversify=False)
        if not shortlist:
            return []
        try:
            taste = await self._taste_vector(profile)
            if taste is not None:
                vecs = await self._product_vectors([s.candidate.sku for s in shortlist])
                for s in shortlist:
                    v = vecs.get(s.candidate.sku)
                    if v is not None:
                        sim = float(v @ taste)
                        s.signals["embedding_similarity"] = round(sim, 3)
                        s.raw += SIM_WEIGHT * sim
                        s.interest = interest_from_raw(s.raw)
        except Exception as exc:  # embeddings are an enhancement, never fatal
            log.warning("embedding similarity failed (%s); heuristic only", exc)
        ordered = rank(shortlist, RERANK_MAX)
        ordered = await self._llm_rerank(ordered, profile, context)
        if mix:  # trip detours: keep one taste pick, one markdown and one deal when available
            return rank(ordered, limit, min_raw=float("-inf"), mix=True)
        return ordered[:limit]

    async def _llm_rerank(self, items: list[Scored], profile: UserProfile, context: str) -> list[Scored]:
        from utils import llm

        if len(items) <= 1 or not llm.is_configured():
            return items
        lines = [
            f"- sku={s.candidate.sku} name={s.candidate.product.name!r} category={s.candidate.product.category} "
            f"tags={','.join(s.candidate.product.tags)} signals={s.signals}"
            for s in items
        ]
        prompt = (
            f"Shopper prefs (weights -1..1): {profile.prefs}. Dislikes: {profile.dislikes}. Diet: {profile.dietary}.\n"
            f"Context: {context}.\nCandidates (already filtered and scored):\n" + "\n".join(lines) + "\n\n"
            "Rerank ALL candidates best-first for this shopper. For each give interest 0..1 and a short, specific, "
            "honest 'why you'd like this' (<= 12 words) grounded ONLY in the listed signals. Use only the given skus."
        )
        try:
            out = await llm.complete_json(prompt, _Rerank, kind="brain", max_tokens=1024)
        except Exception as exc:  # LLMNotConfigured / LLMOutputError / network: keep the heuristic
            log.warning("LLM rerank skipped (%s)", exc)
            return items
        by_sku = {s.candidate.sku: s for s in items}
        result: list[Scored] = []
        for it in out.items:
            s = by_sku.pop(it.sku, None)
            if s is None:
                continue
            s.interest = round(it.interest, 3)
            s.reason = it.why.strip()
            s.signals["llm_rerank"] = True
            result.append(s)
        result.extend(by_sku.values())  # anything the LLM dropped keeps its heuristic place at the end
        return result

    # ------------------------------------------------------------------------------------------------ profile writes
    async def record_feedback(self, feedback: Feedback) -> ProfileUpdate:
        update = await super().record_feedback(feedback)
        try:
            await self._refresh_taste_vector(self._profile(feedback.user_id))
        except Exception as exc:
            log.warning("taste vector refresh failed (%s)", exc)
        return update

    async def record_purchase(self, records: list[PurchaseRecord]) -> None:
        await super().record_purchase(records)

    # ------------------------------------------------------------------------------------------------ TODO: Postgres
    async def _pg_load_profiles(self) -> dict[str, UserProfile]:
        raise NotImplementedError("TODO 1: load profiles/purchases/feedback from Postgres (extra: catalog deps)")

    async def _pg_save_profile(self, profile: UserProfile) -> None:
        raise NotImplementedError("TODO 1/3: upsert profiles row incl. taste_vector + profile_updates")

    async def _pg_similar(self, vector: list[float], store_id: str, k: int) -> list[Sku]:
        raise NotImplementedError("TODO 2: SELECT sku FROM product_embeddings ORDER BY embedding <=> %s LIMIT k")


def create(services: Services | None = None) -> RecommenderService:
    return RecommenderService(services)
