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
    picky_vla_t1: str = ""
    """T1 grasp policy driven joint by joint on the Isaac worker (contracts 0.2.2 ArmJointsCmd): perceptron (= the
    Perceptron Mk1.5 tool policy, PERCEPTRON_API_KEY) | perceptron_vla (robotics VLA service, team key) | smolvla |
    scripted | off. Blank = the PICKY_VLA_TIERS / PICKY_VLA_POLICY_PROTOCOL rules (T1 off by default)."""
    picky_vla_t1_timeout_s: float = 25.0
    """Policy-loop budget of one T1 grasp (also capped by the step's deadline); exceeded -> policy_timeout."""
    picky_vla_t1_max_chunks: int = 12
    """Most action chunks one T1 grasp predicts before giving up (grasp_missed / policy_timeout)."""
    picky_vla_t1_exec_steps: int = 25
    """Rows of each predicted chunk executed (at the chunk's action_hz) before the next observation (<= 50)."""
    picky_vla_t1_home_deg: str = "0,-100,90,50,45,7"
    """6 USD joint degrees the arm moves to before the policy starts (the recorded demos' start pose: LeIsaac rest,
    wrist roll + gripper at the recorder's mean); blank = start from the scripted stow pose."""
    picky_vla_t1_instruction: str = "pick up the {name} and put it in the basket"
    """T1 policy instruction; {name} = the product name (lower case), {sku}. Default = the recorded dataset's task."""
    picky_vla_t1_capture_hz: float = 4.0
    """Side + wrist pictures per second while a T1 chunk plays (live feed; the last pair is the next observation)."""
    picky_vla_nav_capture_hz: float = 2.0
    """Front-camera pictures per second while a VLA step drives (blurred, retained evidence); 0 = off."""
    picky_vla_nav_look: str = "isaac"
    """Perceptron look-out ("is <product> visible?") on the approach: isaac (Isaac stores only) | always | off."""
    picky_vla_nav_look_m: float = 2.5
    """Path left (m) below which the look-out runs (<= 2 per drive)."""
    picky_perceptron_call_budget: int = 80
    """Perceptron Mk1.5 calls per trip for the robot (T1 tool policy + navigation look-outs); spent -> T0."""
    picky_vla_mk15_timeout_s: float = 12.0
    """Per-call timeout of the Mk1.5 tool policy / look-out."""
    picky_perceived_streams: bool = True
    """List the annotated onboard cameras '<robot>.front.perceived' / '<robot>.wrist.perceived' (contracts 0.2.3)."""
    picky_perceived_provider: str = "perceptron"
    """Boxes on the perceived streams: perceptron (Mk1.5, needs PERCEPTRON_API_KEY + the real cameras arm; ground
    truth whenever it is unavailable / quota'd / over budget) | ground_truth (never calls Perceptron)."""
    picky_perceived_detect_hz: float = 1.0
    """Detections per second per perceived stream (boxes are reused between detections)."""
    picky_perceived_call_budget: int = 240
    """Perceptron calls per trip for the perceived streams (separate from PICKY_PERCEPTRON_CALL_BUDGET); spent ->
    ground-truth boxes for the rest of the trip."""
    picky_perceived_timeout_s: float = 6.0
    """Per-call timeout of one perceived-stream detection."""
    picky_director_camera: bool = True
    """List '<robot>.director' (third-person chase view) for robots on the 2D fake; Isaac robots get it when the
    worker runs with PICKY_SIM_DIRECTOR."""
    picky_product_image_dir: str = ""
    """Optional folder of product photos / renders ('<SKU>.jpg|png') served by GET /api/products/{sku}/image.jpg;
    blank or missing file = a label-card render of the sim prop."""
    picky_unload_by_arm: bool = True
    """0.2.4 judges' demo: at checkout the arm takes every unit out of the basket and puts it on the counter (planned
    per unit, verified); False = the handoff unloads the basket at once (the 0.2.3 behaviour)."""
    picky_verify_items: bool = True
    """Send the shopper a photo of every purchased unit (verification card: correct / wrong) after each pick."""
    picky_verify_auto_accept_s: float = 20.0
    """No answer to a verification card within this many seconds -> 'auto-accepted' (labelled as such)."""
    picky_verify_max_rejections: int = 1
    """'wrong' answers per unit before the robot skips that unit (each 'wrong' = put back + pick another)."""
    picky_produce_review_timeout_s: float = 30.0
    """Produce review ('Which avocados?') with photos: no answer within this -> the robot's best guess (labelled)."""
    picky_purchases_db: str = "var/purchases.sqlite"
    """Purchase-history database (SQLite file, relative to the repo root; ':memory:' = per process). Seeded with
    months of demo history on first use; every completed checkout appends its lines."""
    picky_suggestions_llm: bool = True
    """Rank suggestion candidates + write their one-line reasons with the LLM (utils.llm, grounded in the signals
    only); False / no key / any LLM error = deterministic order + templated reasons."""
    picky_payment_agent: bool = True
    """Run checkout through the Visa payment agent (cart mandate -> intent check -> passkey -> agentic token -> auth
    + capture -> signed receipt, as payment.agent_step events). Always a sandbox: mock Visa Intelligent Commerce, or
    Stripe test mode when STRIPE_SECRET_KEY is set."""
    picky_payment_agent_llm: bool = True
    """LLM narration of the payment agent's steps (one line each, grounded in the step facts); else templates."""
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
