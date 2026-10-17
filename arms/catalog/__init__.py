"""catalog arm: multi-store product catalog (physical planograms + online), stock, deals, scans, merchant view.

``create(services)`` -> Postgres-backed ``CatalogService`` (extra: ``catalog``); ``create_fake(services)`` ->
in-memory catalog over ``data/`` (base deps only). Neither touches other services or the disk at construction.
``create()`` raises when the ``catalog`` extra is missing, so the gateway registry falls back to the fake (and
``/health`` says why) instead of failing the trip later.
"""

import importlib.util
from typing import Any

from contracts.services import CatalogService, Services


def create(services: Services) -> CatalogService:
    if importlib.util.find_spec("sqlalchemy") is None:  # cheap preflight; no I/O, no other services
        raise RuntimeError("real catalog needs the extra: uv sync --extra catalog && make db-up && make seed")
    from arms.catalog.service import PostgresCatalogService

    return PostgresCatalogService(services)


def create_fake(services: Services, **kwargs: Any) -> CatalogService:
    from arms.catalog.fake import FakeCatalogService

    return FakeCatalogService(services, **kwargs)


__all__ = ["create", "create_fake"]
