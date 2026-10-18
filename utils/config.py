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
    perceptron_api_key: str = ""
    """Perceptron Mk1.5 key (the verifier's vision provider, arms/perception); blank = ground-truth fake verifier."""
    perceptron_model: str = "perceptron-mk1.5"
    """perceptron-mk1.5 (default) | perceptron-mk1 (fallback)."""
    perceptron_base_url: str = "https://api.perceptron.inc/v1"
    picky_vision_provider: str = "perceptron"
    """Verifier vision backend: perceptron | fake (deterministic sidecar answers through the real verifier code)."""
    picky_verifier_mode: str = ""
    """decide | shadow | gt_only; blank = per store backend (shadow on the 2D fake, decide on Isaac)."""
    picky_vision_call_timeout_s: float = 4.0
    """Cap per vision-provider call inside the verifier (also bounded by the request's deadline)."""
    picky_exec_mode: str = ""
    """direct | plan | tools. Blank = plan when the gateway wires the VLA + verifier, else direct. direct = the exact
    v0.2 brain path (the loop's kill switch)."""
    picky_faults: str = ""
    """Demo fault scenario loaded at boot (contracts.faults.DEMO_SCENARIOS, e.g. drop_on_first_grasp); blank = none."""
    picky_dev: bool = False
    """True enables the presenter routes /api/dev/faults* and /api/dev/plans on the gateway."""
    picky_plan_timeout_s: float = 20.0
    """Bound on the LLM planner's draft call and the runner's replan call (seconds)."""
    picky_verify_timeout_s: float = 8.0
    """The runner's bound on each verifier call; the verifier gets this minus 1 s as its deadline."""
    picky_vla_tiers: str = ""
    """Extra VLA tiers to offer: t1_pretrained | t1_sim (blank = the scripted T0 tier only)."""
    picky_vla_t1_model: str = ""
    """T1 model id advertised by the Isaac worker, e.g. lerobot/smolvla_base (blank = the robot arm's default)."""
    picky_vla_policy_url: str = ""
    """Isaac worker side: the T1 policy server, e.g. ws://127.0.0.1:8000 (openpi wire)."""
    picky_vla_policy_protocol: str = ""
    """openpi_ws (blank = default) | perceptron_tool (T1 grasps the verifier's grounding) | lerobot_grpc (later)."""
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
