"""T1 VLA adapter: a learned grasp / place tier next to T0. It reports itself unavailable (with the reason) unless
configured AND runnable, and ``T0Vla`` then runs T0 (``resolve_tier``'s floor, or ``fell_back_from`` at accept when T1
was asked for explicitly).

Backends (``T1Config.from_settings``):

- policy (PICKY_VLA_T1 = perceptron | perceptron_vla | smolvla | scripted; contracts 0.2.2): a grasp POLICY driven
  joint by joint on the Isaac worker (``ArmJointsCmd``) by ``arms.robot.vla_t1.run_policy_grasp``. ``perceptron`` =
  THE T1 backend: Perceptron Mk1.5 calling Cartesian tools (``arms.robot.vla_mk15``; PERCEPTRON_API_KEY; needs the
  worker's ``arm_ik`` too); ``perceptron_vla`` = the Perceptron robotics VLA service (team key + a ready deployment;
  ``arms.robot.policy_backends``); ``smolvla`` = vhagar :8200; ``scripted`` = tests. Available when the robot's store
  runs on an Isaac worker advertising ``arm_joints`` AND the policy is configured. Grasp only.
  PICKY_VLA_T1=off disables T1 whatever the other variables say.
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

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from arms.robot.vla_t1 import PolicyGraspConfig
from contracts.common import RobotId
from contracts.sim import RobotBackend
from contracts.vla import PolicyServerStatus, VlaCapabilities, VlaTier, t1_capability_from_status
from utils.config import get_settings
from utils.log import get_logger

log = get_logger(__name__)

T1Backend = Literal["none", "simulated", "perceptron_tool", "openpi_ws", "policy"]
MK15 = "perceptron_mk15"
POLICY_ALIASES: dict[str, str] = {
    "perceptron": MK15,
    "perceptron_mk15": MK15,
    "mk15": MK15,
    "perceptron_vla": "perceptron_vla",
    "robotics": "perceptron_vla",
    "smolvla": "smolvla",
    "scripted": "scripted",
}
"""PICKY_VLA_T1 value -> backend: ``perceptron`` = THE T1 policy, Perceptron Mk1.5 driving Cartesian tools
(``arms.robot.vla_mk15``, hosted key PERCEPTRON_API_KEY); ``perceptron_vla`` = the Perceptron robotics VLA service
(team key + deployment; ``policy_backends.PerceptronBackend``); ``smolvla`` (vhagar :8200); ``scripted`` (tests)."""
POLICY_NAMES: tuple[str, ...] = tuple(POLICY_ALIASES)
BACKEND_NAMES: dict[str, str] = {"perceptron_vla": "perceptron"}
"""Our canonical policy name -> ``arms.robot.policy_backends`` name (where they differ)."""
POLICY_PROTOCOLS: dict[str, str] = {
    "perceptron_vla": "perceptron_vla",
    "perceptron_mk15": MK15,
    "smolvla": "smolvla",
    "scripted": "scripted",
}
"""PICKY_VLA_POLICY_PROTOCOL aliases of the same backends (with t1_pretrained in PICKY_VLA_TIERS)."""
NO_ARM_IK = "the Isaac worker has no arm_ik (contracts 0.2.2 worker needed for the Mk1.5 policy)"
OFF_VALUES = frozenset({"off", "none", "0", "false", "no", "disabled"})
NO_ARM_JOINTS = "the Isaac worker has no arm_joints (contracts 0.2.2 worker needed)"
ON_FAKE = "store on the 2D fake: learned grasps need the Isaac SO-101 (arm_joints)"
CONFIG_CACHE_S = 30.0
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
    policy: str | None = None
    """backend 'policy': perceptron_mk15 | perceptron_vla (robotics service) | smolvla | scripted."""

    @classmethod
    def from_settings(cls) -> "T1Config":
        """PICKY_VLA_TIERS / PICKY_VLA_POLICY_PROTOCOL / PICKY_VLA_POLICY_URL / PICKY_VLA_T1_MODEL /
        PERCEPTRON_API_KEY / PERCEPTRON_MODEL through utils.config (missing Settings fields = unset)."""
        t1 = _setting("picky_vla_t1").lower()
        if t1 in OFF_VALUES:
            return cls()
        if t1 in POLICY_ALIASES:
            return cls.learned(POLICY_ALIASES[t1])
        if t1:
            log.warning("PICKY_VLA_T1=%r is not one of %s / off: ignored", t1, ", ".join(POLICY_NAMES))
        tiers = parse_tiers(_setting("picky_vla_tiers"))
        model = _setting("picky_vla_t1_model") or DEFAULT_T1_MODEL
        protocol = _setting("picky_vla_policy_protocol").lower()
        if T1_TIER in tiers and protocol in POLICY_PROTOCOLS:
            return cls.learned(POLICY_PROTOCOLS[protocol])
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

    @classmethod
    def learned(cls, policy: str) -> "T1Config":
        """A policy driven joint by joint on Isaac: any PICKY_VLA_T1 value (``perceptron`` = the Mk1.5 tool policy)."""
        policy = POLICY_ALIASES.get(policy, policy)
        return cls(backend="policy", policy=policy, model_id=policy)


class T1Adapter:
    """Capability row + executability of the T1 tier for ``T0Vla``."""

    def __init__(
        self,
        config: T1Config | None = None,
        *,
        backend_factory: Callable[[str], Any] | None = None,
        grasp_config: PolicyGraspConfig | None = None,
    ) -> None:
        self.config = config or T1Config()
        self.policy_status: PolicyServerStatus | None = None
        self._factory = backend_factory
        """backend 'policy': name -> PolicyBackend (tests inject one; default policy_backends.create_policy_backend)."""
        self._grasp_config = grasp_config
        self._backends: dict[RobotId, Any] = {}
        self._config_row: tuple[float, VlaCapabilities] | None = None

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
        """True when a T1 exec can actually run tonight (simulated / perceptron_tool with a key / a learned policy:
        its capability row already says whether this robot can run it)."""
        if self.backend in ("simulated", "policy"):
            return True
        if self.backend == "perceptron_tool":
            return self.config.api_key_set
        return False

    @property
    def needs_grounding(self) -> bool:
        return self.backend == "perceptron_tool"

    # ---- learned policy (backend "policy") ----------------------------------------------------------------------
    @property
    def grasp_config(self) -> PolicyGraspConfig:
        if self._grasp_config is None:
            self._grasp_config = PolicyGraspConfig.from_settings()
        return self._grasp_config

    async def policy_backend(self, robot_id: RobotId) -> Any:
        """The robot's PolicyBackend (one per robot: backends are not safe for concurrent predicts), built lazily."""
        if robot_id not in self._backends:
            name = self.config.policy or "scripted"
            if self._factory is not None:
                self._backends[robot_id] = self._factory(name)
            elif name == MK15:
                from arms.robot.vla_mk15 import Mk15Policy

                self._backends[robot_id] = Mk15Policy()
            else:
                from arms.robot.policy_backends import create_policy_backend

                self._backends[robot_id] = create_policy_backend(BACKEND_NAMES.get(name, name))
        return self._backends[robot_id]

    async def close(self) -> None:
        for backend in list(self._backends.values()):
            close = getattr(backend, "close", None)
            if close is not None:
                try:
                    await close()
                except Exception:  # noqa: BLE001 - shutdown is best effort
                    pass
        self._backends.clear()

    def _policy_row(self, robot_id: RobotId) -> VlaCapabilities:
        """Configuration part of the row (key / deployment / model), cached CONFIG_CACHE_S (the key lookup may hit
        the Keychain)."""
        now = time.monotonic()
        if self._config_row is None or now - self._config_row[0] > CONFIG_CACHE_S:
            name = self.config.policy or "scripted"
            try:
                if name == MK15:
                    from arms.robot.vla_mk15 import mk15_capability

                    row = mk15_capability(robot_id, "isaac")
                else:
                    from arms.robot.policy_backends import policy_capability

                    backend_name = BACKEND_NAMES.get(name, name)
                    row = policy_capability(robot_id, backend_name, "isaac", step_kinds=("grasp",))  # type: ignore[arg-type]
            except Exception as exc:  # noqa: BLE001 - a broken backend module must not break T0
                row = VlaCapabilities(
                    robot_id=robot_id,
                    tier=T1_TIER,
                    available=False,
                    reason=f"policy backend {name!r} unavailable: {exc}",
                    step_kinds=["grasp"],
                    model_id=name,
                )
            self._config_row = (now, row)
        return self._config_row[1].model_copy(update={"robot_id": robot_id})

    def capability(self, robot_id: RobotId, backend: RobotBackend | None, *, robot: Any = None) -> VlaCapabilities:
        cfg = self.config
        if cfg.backend == "policy":
            row = self._policy_row(robot_id)
            reason = row.reason
            if not reason:
                probe = getattr(robot, "supports_arm_joints", None)
                ik_probe = getattr(robot, "supports_arm_ik", None)
                if backend != "isaac":
                    reason = ON_FAKE
                elif probe is None or not probe(robot_id):
                    reason = NO_ARM_JOINTS
                elif cfg.policy == MK15 and (ik_probe is None or not ik_probe(robot_id)):
                    reason = NO_ARM_IK
            gc = self.grasp_config
            return row.model_copy(
                update={
                    "available": not reason,
                    "reason": reason,
                    "backend": backend,
                    "step_kinds": ["grasp"],
                    "needs_grounding": False,
                    "cameras": ["front", "wrist"],
                    "input_frames_blurred": True,
                    "action_hz": row.action_hz or 30.0,
                    "chunk_size": row.chunk_size or gc.exec_steps,
                    "policy_host": row.policy_host or "isaac worker (arm_joints)",
                }
            )
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


__all__ = [
    "DEFAULT_T1_MODEL",
    "MK15",
    "POLICY_ALIASES",
    "POLICY_NAMES",
    "T1Adapter",
    "T1Backend",
    "T1Config",
    "parse_tiers",
]
