"""Recommender arm: profiles + explainable detour / checkout / near-expiry recommendations (RecommenderService)."""

from contracts.services import RecommenderService, Services


def create(services: Services) -> RecommenderService:
    """Real implementation (bge-small embeddings + LLM rerank; degrades to the heuristic without extras/keys)."""
    from arms.recommender.service import create as _create

    return _create(services)


def create_fake(services: Services) -> RecommenderService:
    """Deterministic fake (base deps only)."""
    from arms.recommender.fake import create_fake as _create_fake

    return _create_fake(services)


__all__ = ["create", "create_fake"]
