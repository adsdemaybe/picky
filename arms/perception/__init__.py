"""Perception arm: face blur, product identification (scan reports), produce freshness top-3, basket verification,
and the Cameras API.

``create(services)`` -> real CV cascade (extras: perception); ``create_fake(services)`` -> ground-truth bypass.
Both satisfy ``contracts.services.PerceptionService``.

``create_cameras(services)`` -> cameras with LLM-vision ``observe`` (``utils.llm`` fast model, gemini-3.8-flash by
default; raises without GEMINI_API_KEY so the gateway falls back); ``create_cameras_fake(services)`` -> ground-truth
``observe``. Both satisfy ``contracts.services.CameraService``. ``jpeg_bytes`` / ``mjpeg_part`` /
``MJPEG_CONTENT_TYPE`` help the gateway serve snapshot.jpg and stream.mjpg.

Planner -> VLA -> verifier loop (contracts 0.2.1, ``LOOP_SERVICE_FACTORIES``):
``create_verifier(services)`` -> ``PerceptionVerifier`` (Perceptron Mk1.5 vision + robot state + sim ground truth;
raises without PERCEPTRON_API_KEY unless PICKY_VISION_PROVIDER=fake) · ``create_verifier_fake`` -> ``FakeVerifier``
(exact verdicts from frame ground truth). ``create_evidence`` -> ``EvidenceRing`` (real blurred frames, retained) ·
``create_evidence_fake`` -> ``FakeEvidenceCameras`` (synthetic frames + sidecars). Both satisfy
``contracts.services.VerifierService`` / ``contracts.evidence.EvidenceCameras``.
"""

from contracts.evidence import EvidenceCameras
from contracts.services import CameraService, PerceptionService, Services, VerifierService

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


def create_verifier(services: Services) -> VerifierService:
    from arms.perception.verifier import create_verifier as _create

    return _create(services)


def create_verifier_fake(services: Services) -> VerifierService:
    from arms.perception.verifier_fake import FakeVerifier

    return FakeVerifier(services)


def create_evidence(services: Services) -> EvidenceCameras:
    from arms.perception.evidence import EvidenceRing

    return EvidenceRing(services)


def create_evidence_fake(services: Services) -> EvidenceCameras:
    from arms.perception.evidence import FakeEvidenceCameras

    return FakeEvidenceCameras(services)


__all__ = [
    "MJPEG_BOUNDARY",
    "MJPEG_CONTENT_TYPE",
    "create",
    "create_cameras",
    "create_cameras_fake",
    "create_evidence",
    "create_evidence_fake",
    "create_fake",
    "create_verifier",
    "create_verifier_fake",
    "jpeg_bytes",
    "mjpeg_part",
]
