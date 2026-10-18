"""T1 VLA adapter: a learned grasp / place tier next to T0. A STUB in contracts 0.2.1: it reports itself unavailable
(with the reason) unless configured, and ``T0Vla`` then runs T0 (``resolve_tier``'s floor, or ``fell_back_from`` at
accept when T1 was asked for explicitly).

Backends (``T1Config.from_settings``; PICKY_VLA_TIERS must name a T1 tier):

- none (default): never available ("T1 not enabled").
- simulated (``t1_sim`` in PICKY_VLA_TIERS): always available; runs the T0 motions with policy-style chunk counters
  (the breaker demo, scenario ``flaky_t1``).
- perceptron_tool (``t1_pretrained`` + PICKY_VLA_POLICY_PROTOCOL=perceptron_tool): available when PERCEPTRON_API_KEY
  is set; the magic grasp AIMED at the unit the verifier's Perceptron grounding chose (``needs_grounding``; no
  grounding -> ``target_not_grounded``).
- openpi_ws (``t1_pretrained``, + PICKY_VLA_POLICY_URL / PICKY_VLA_T1_MODEL): never available in 0.2.1; the worker-side
  policy server needs ``RunSkillCmd`` (contracts 0.3.0 + an Isaac worker rebuild).

The robot arm never sends frames anywhere itself: the perceptron_tool tier only consumes the verifier's
``GroundedTarget`` (computed by perception on blurred frames), so ``input_frames_blurred`` holds for every backend.
``on_policy_status`` is the 0.3.0 hook (``SimRobot`` will call it on ``PolicyServerStatus``); until then it only
changes the reason text.
"""

from dataclasses import dataclass
from typing import Literal

from contracts.common import RobotId
from contracts.sim import RobotBackend
from contracts.vla import PolicyServerStatus, VlaCapabilities, VlaTier, t1_capability_from_status
from utils.config import get_settings

T1Backend = Literal["none", "simulated", "perceptron_tool", "openpi_ws"]
DEFAULT_T1_MODEL = "lerobot/smolvla_base"
DEFAULT_PERCEPTRON_MODEL = "perceptron-mk1.5"
T1_TIER: VlaTier = "t1_pretrained"
NOT_ENABLED = "T1 not enabled (add t1_pretrained or t1_sim to PICKY_VLA_TIERS)"
NEEDS_WIRE = "policy server reported, but RunSkillCmd needs contracts 0.3.0 + an Isaac worker rebuild"


def _setting(name: str) -> str:
    try:
        return str(getattr(get_settings(), name, "") or "").strip()
    except Exception:  # noqa: BLE001 - config errors must not take the robot down
        return ""


def parse_tiers(value: str | None) -> set[str]:
    return {t.strip().lower() for t in (value or "").split(",") if t.strip()}


@dataclass(frozen=True)
class T1Config:
    backend: T1Backend = "none"
    model_id: str | None = None
    policy_url: str | None = None
    api_key_set: bool = False

    @classmethod
    def from_settings(cls) -> "T1Config":
        """PICKY_VLA_TIERS / PICKY_VLA_POLICY_PROTOCOL / PICKY_VLA_POLICY_URL / PICKY_VLA_T1_MODEL /
        PERCEPTRON_API_KEY / PERCEPTRON_MODEL through utils.config (missing Settings fields = unset)."""
        tiers = parse_tiers(_setting("picky_vla_tiers"))
        model = _setting("picky_vla_t1_model") or DEFAULT_T1_MODEL
        if "t1_sim" in tiers:
            return cls(backend="simulated", model_id=f"{model} (simulated)")
        if T1_TIER not in tiers:
            return cls()
        if _setting("picky_vla_policy_protocol").lower() == "perceptron_tool":
            return cls(
                backend="perceptron_tool",
                model_id=_setting("perceptron_model") or DEFAULT_PERCEPTRON_MODEL,
                api_key_set=bool(_setting("perceptron_api_key")),
            )
        return cls(backend="openpi_ws", model_id=model, policy_url=_setting("picky_vla_policy_url") or None)

    @classmethod
    def simulated(cls, model_id: str = f"{DEFAULT_T1_MODEL} (simulated)") -> "T1Config":
        return cls(backend="simulated", model_id=model_id)


class T1Adapter:
    """Capability row + executability of the T1 tier for ``T0Vla``."""

    def __init__(self, config: T1Config | None = None) -> None:
        self.config = config or T1Config()
        self.policy_status: PolicyServerStatus | None = None

    @classmethod
    def from_settings(cls) -> "T1Adapter":
        return cls(T1Config.from_settings())

    @property
    def backend(self) -> T1Backend:
        return self.config.backend

    def on_policy_status(self, status: PolicyServerStatus | None) -> None:
        """0.3.0 hook: the Isaac worker's ``PolicyServerStatus`` (nothing sends it on the 0.2 wire)."""
        self.policy_status = status

    def can_execute(self) -> bool:
        """True when a T1 exec can actually run tonight (simulated / perceptron_tool with a key)."""
        if self.backend == "simulated":
            return True
        if self.backend == "perceptron_tool":
            return self.config.api_key_set
        return False

    @property
    def needs_grounding(self) -> bool:
        return self.backend == "perceptron_tool"

    def capability(self, robot_id: RobotId, backend: RobotBackend | None) -> VlaCapabilities:
        cfg = self.config
        if cfg.backend == "simulated":
            return VlaCapabilities(
                robot_id=robot_id,
                tier=T1_TIER,
                available=True,
                backend=backend,
                step_kinds=["grasp", "place"],
                model_id=cfg.model_id,
                policy_host="simulated (T0 motions)",
                action_hz=30.0,
                chunk_size=50,
                cameras=["front", "wrist"],
                cancel_latency_ms=100.0,
            )
        if cfg.backend == "perceptron_tool":
            return VlaCapabilities(
                robot_id=robot_id,
                tier=T1_TIER,
                available=cfg.api_key_set,
                reason="" if cfg.api_key_set else "perceptron_tool T1 needs PERCEPTRON_API_KEY",
                backend=backend,
                step_kinds=["grasp"],
                model_id=cfg.model_id,
                policy_host="api.perceptron.inc (grounding only; motion = magic grasp)",
                needs_grounding=True,
                cameras=["front"],
                cancel_latency_ms=200.0,
            )
        if cfg.backend == "openpi_ws":
            cap = t1_capability_from_status(robot_id, self.policy_status, backend)
            reason = cap.reason or NEEDS_WIRE
            if cfg.policy_url and not cap.reason:
                reason = f"{NEEDS_WIRE} ({cfg.policy_url})"
            return cap.model_copy(
                update={"available": False, "reason": reason, "model_id": cap.model_id or cfg.model_id}
            )
        return VlaCapabilities(
            robot_id=robot_id,
            tier=T1_TIER,
            available=False,
            reason=NOT_ENABLED,
            backend=backend,
            step_kinds=["grasp", "place"],
        )


__all__ = ["DEFAULT_T1_MODEL", "T1Adapter", "T1Backend", "T1Config", "parse_tiers"]
