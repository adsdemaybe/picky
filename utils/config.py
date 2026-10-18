"""Central configuration: every env var picky reads lives here (and in ``.env.example``).

Usage::

    from utils.config import settings          # lazily constructed on first access
    settings.picky_default_store

    from utils.config import get_settings, set_settings, reset_settings   # tests
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[1]


def repo_root() -> Path:
    """Absolute path of the monorepo root."""
    return REPO_ROOT


class Settings(BaseSettings):
    """Field names are the env var names lower-cased (env lookup is case-insensitive)."""

    model_config = SettingsConfigDict(
        env_file=str(REPO_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    picky_env: str = "dev"
    picky_real_arms: str = ""
    picky_default_store: str = "store_ave"
    picky_data_dir: str = "data"
    database_url: str = "postgresql+psycopg://picky:picky@localhost:5432/picky"
    picky_llm_provider: str = "gemini"
    """utils.llm provider: gemini (default, google-genai, GEMINI_API_KEY) | anthropic (ANTHROPIC_API_KEY)."""
    gemini_api_key: str = ""
    anthropic_api_key: str = ""
    picky_model_brain: str = ""
    """Blank = the provider default (utils.llm.DEFAULT_MODELS): gemini-3.8-flash / claude-sonnet-5-5."""
    picky_model_fast: str = ""
    """Blank = gemini-3.8-flash / claude-haiku-5-5."""
    picky_model_smart: str = ""
    """Blank = gemini-4-argon / claude-opus-5-5."""
    picky_gemini_thinking: str = "low"
    """Gemini thinking_level for every call (minimal | low | medium | high; blank = model default, medium)."""
    picky_llm_timeout_s: float = 60.0
    """Per-request LLM timeout in seconds (both providers; the SDKs default to minutes)."""
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 8000
    picky_gateway_url: str = "http://localhost:8000"
    picky_sim_token: str = "dev-sim-token"
    picky_signing_key: str = "dev-signing-key"
    stripe_secret_key: str = ""
    visa_api_key: str = ""
    visa_shared_secret: str = ""
    photon_api_key: str = ""
    photon_project_id: str = ""
    deepgram_api_key: str = ""
    picky_rec_max_stops: int = 3
    picky_scripted_shopper: bool = False
    """Gateway server only: True = the fake messaging's scripted shopper answers every texted approval (and the
    follow-up) by itself. False (default) = approvals wait for a person (web app click or a real text)."""

    # --- helpers -------------------------------------------------------------------------------------------------
    def real_arms(self) -> set[str]:
        """Arms that should use their real service (``PICKY_REAL_ARMS=catalog,routing``; ``all`` = every arm)."""
        return {a.strip().lower() for a in self.picky_real_arms.split(",") if a.strip()}

    def is_real(self, arm: str) -> bool:
        arms = self.real_arms()
        return "all" in arms or arm.lower() in arms

    @property
    def repo_root(self) -> Path:
        return REPO_ROOT

    @property
    def data_dir(self) -> Path:
        """``PICKY_DATA_DIR`` resolved against the repo root when relative."""
        p = Path(self.picky_data_dir).expanduser()
        return p if p.is_absolute() else (REPO_ROOT / p).resolve()

    @property
    def is_dev(self) -> bool:
        return self.picky_env.lower() in {"dev", "development", "test"}


ENV_VARS: tuple[str, ...] = tuple(name.upper() for name in Settings.model_fields)

_settings: Settings | None = None


def get_settings() -> Settings:
    """Return the process-wide Settings, constructing it on first use."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def set_settings(new: Settings | None = None, **overrides: Any) -> Settings:
    """Replace the process-wide Settings (tests). ``set_settings(picky_real_arms="routing")`` also works."""
    global _settings
    if new is None:
        new = Settings(**overrides)
    elif overrides:
        new = new.model_copy(update=overrides)
    _settings = new
    return new


def reset_settings() -> None:
    """Drop the cached Settings so the next access re-reads env/.env."""
    global _settings
    _settings = None


def __getattr__(name: str) -> Any:
    # PEP 562: `from utils.config import settings` / `utils.config.settings` construct lazily.
    if name == "settings":
        return get_settings()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
