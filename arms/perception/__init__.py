"""Perception arm: face blur, product identification (scan reports), produce freshness top-3, basket verification.

``create(services)`` -> real CV cascade (extras: perception); ``create_fake(services)`` -> ground-truth bypass.
Both satisfy ``contracts.services.PerceptionService``.
"""

from contracts.services import PerceptionService, Services


def create(services: Services) -> PerceptionService:
    from arms.perception.service import RealPerception

    return RealPerception(services)


def create_fake(services: Services) -> PerceptionService:
    from arms.perception.fake import FakePerception

    return FakePerception(services)


__all__ = ["create", "create_fake"]
