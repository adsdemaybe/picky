"""messaging arm: Photon 2.0 (iMessage) channel for intents, approvals and post-purchase follow-ups.

Public API (the only things the gateway may import): ``create(services)`` (real, Photon) and
``create_fake(services)`` (in-process console channel with a scripted shopper).
"""

import importlib.util

from contracts.services import MessagingService, Services
from utils.config import get_settings


def create(services: Services) -> MessagingService:
    """Real Photon implementation. Needs ``uv sync --extra messaging`` + PHOTON_API_KEY/PHOTON_PROJECT_ID.

    Raises ``MessagingNotConfigured`` when either is missing (cheap preflight: no I/O, no other services), so the
    gateway registry falls back to the fake and ``/health`` says why.
    """
    from arms.messaging.service import MessagingNotConfigured, PhotonMessaging

    if importlib.util.find_spec("photon_api") is None:
        raise MessagingNotConfigured("photonhq-api not installed: uv sync --extra messaging")
    s = get_settings()
    if not (s.photon_api_key and s.photon_project_id):
        raise MessagingNotConfigured("set PHOTON_API_KEY and PHOTON_PROJECT_ID in .env")
    return PhotonMessaging(services)


def create_fake(services: Services) -> MessagingService:
    """Deterministic console channel (base deps only)."""
    from arms.messaging.fake import FakeMessaging

    return FakeMessaging(services)


__all__ = ["create", "create_fake"]
