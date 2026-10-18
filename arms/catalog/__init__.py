"""catalog arm: multi-store product catalog (physical planograms + online), stock, deals, scans, merchant view.

``create(services)`` -> Postgres-backed ``CatalogService`` (extra: ``catalog``); ``create_fake(services)`` ->
in-memory catalog over ``data/`` (base deps only). Neither touches other services or the disk at construction.
``create()`` raises when the ``catalog`` extra is missing, so the gateway registry falls back to the fake (and
``/health`` says why) instead of failing the trip later. It also raises while the real service still has
NotImplementedError demo paths (search, get_offers, apply_scan, restock, ...: TODOs 1-4, 7, 8 in service.py), so
``PICKY_REAL_ARMS=catalog`` / ``all`` boots the fake instead of crashing the first trip on search. Developers working
on it construct ``PostgresCatalogService`` directly (or ``create(services, allow_incomplete=True)``).
"""

import importlib.util
from typing import Any

from contracts.services import CatalogService, Services

REAL_CATALOG_COMPLETE = False
"""Flip to True once service.py implements search / get_offers / apply_scan / merchant_dashboard / category_products
/ restock / adjust_stock (no NotImplementedError left on demo paths, CLAUDE.md §6)."""


def create(services: Services, *, allow_incomplete: bool = False) -> CatalogService:
    if importlib.util.find_spec("sqlalchemy") is None:  # cheap preflight; no I/O, no other services
        raise RuntimeError("real catalog needs the extra: uv sync --extra catalog && make db-up && make seed")
    if not (REAL_CATALOG_COMPLETE or allow_incomplete):
        raise RuntimeError(
            "real catalog incomplete: search/get_offers/apply_scan/restock are TODO (arms/catalog/service.py); "
            "using the fake"
        )
    from arms.catalog.service import PostgresCatalogService

    return PostgresCatalogService(services)


def create_fake(services: Services, **kwargs: Any) -> CatalogService:
    from arms.catalog.fake import FakeCatalogService

    return FakeCatalogService(services, **kwargs)


__all__ = ["create", "create_fake"]
