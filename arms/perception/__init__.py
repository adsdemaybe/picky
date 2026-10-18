"""Perception arm: face blur, product identification (scan reports), produce freshness top-3, basket verification,
and the Cameras API.

``create(services)`` -> real CV cascade (extras: perception); ``create_fake(services)`` -> ground-truth bypass.
Both satisfy ``contracts.services.PerceptionService``.

``create_cameras(services)`` -> cameras with claude-haiku-5-5 ``observe`` (raises without ANTHROPIC_API_KEY so the
gateway falls back); ``create_cameras_fake(services)`` -> ground-truth ``observe``. Both satisfy
``contracts.services.CameraService``. ``jpeg_bytes`` / ``mjpeg_part`` / ``MJPEG_CONTENT_TYPE`` help the gateway serve
snapshot.jpg and stream.mjpg.
"""

from contracts.services import CameraService, PerceptionService, Services

from .cameras import MJPEG_BOUNDARY, MJPEG_CONTENT_TYPE, jpeg_bytes, mjpeg_part


def create(services: Services) -> PerceptionService:
    from arms.perception.service import RealPerception

    return RealPerception(services)


def create_fake(services: Services) -> PerceptionService:
    from arms.perception.fake import FakePerception

    return FakePerception(services)


def create_cameras(services: Services) -> CameraService:
    from arms.perception.cameras import create_cameras as _create

    return _create(services)


def create_cameras_fake(services: Services) -> CameraService:
    from arms.perception.cameras import FakeCameras

    return FakeCameras(services)


__all__ = [
    "MJPEG_BOUNDARY",
    "MJPEG_CONTENT_TYPE",
    "create",
    "create_cameras",
    "create_cameras_fake",
    "create_fake",
    "jpeg_bytes",
    "mjpeg_part",
]
