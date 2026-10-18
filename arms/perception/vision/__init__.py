"""Vision providers behind the verifier (``contracts.services.VisionProvider``).

- ``PerceptronVisionProvider``: THE real provider (Perceptron Mk1.5, ``perceptron`` SDK 0.4.0, PERCEPTRON_API_KEY).
- ``SidecarVisionProvider``: deterministic stand-in answering from the frame's ground-truth sidecar (no key, no
  network; ``PICKY_VISION_PROVIDER=fake``).

Both reject frames with ``blurred=False`` (ValueError) and never raise for provider errors (``call.error_code``).
"""

from .common import ProviderStats, provider_method
from .perceptron import DEFAULT_BASE_URL, DEFAULT_MODEL, FALLBACK_MODEL, PerceptronVisionProvider
from .sidecar import SidecarVisionProvider

__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "FALLBACK_MODEL",
    "PerceptronVisionProvider",
    "ProviderStats",
    "SidecarVisionProvider",
    "provider_method",
]
