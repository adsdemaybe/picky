"""gateway arm: the composition root. FastAPI app + trip orchestrator (the pipeline state machine) + arm registry.

Public API:
- ``create(services)`` -> ``TripOrchestrator`` (the gateway's ``TripController``) over an already-wired container.
- ``create_fake(services)`` -> the same (the gateway has no fake; everything it drives can be faked instead).
- ``build_registry(...)`` -> wire every arm (real vs fake from ``PICKY_REAL_ARMS``) + gateway services.
- ``create_app(...)`` -> the FastAPI app (``arms.gateway.app:app`` for uvicorn).
"""

from typing import Any

from contracts.services import Services


def create(services: Services, **kwargs: Any) -> Any:
    from arms.gateway.orchestrator import TripOrchestrator

    return TripOrchestrator(services, **kwargs)


def create_fake(services: Services, **kwargs: Any) -> Any:
    return create(services, **kwargs)


def build_registry(*args: Any, **kwargs: Any) -> Any:
    from arms.gateway.registry import build_registry as _build

    return _build(*args, **kwargs)


def create_app(*args: Any, **kwargs: Any) -> Any:
    from arms.gateway.app import create_app as _create_app

    return _create_app(*args, **kwargs)


__all__ = ["build_registry", "create", "create_app", "create_fake"]
