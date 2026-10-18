"""Policy-server adapter for the learned VLA tiers (T1 / T2), WORKER side (Isaac on vhagar). PROPOSED with 0.3.0;
inert in 0.2.1 (plain classes + helpers, no wire union references them; P2: T1 is not on tonight's critical path).

    gateway: arms/robot VlaService ── RunSkillCmd ─▶ /ws/sim ─▶ Isaac worker (arms/sim) ◀─ SkillProgress / CommandResult
    worker:  _RunSkill action (stepped by the physics loop, never blocks)
               ├─ every action tick: pop the next action from the chunk queue -> SO-101 joint position targets
               └─ when the queue is <= refill_fraction full: PolicyThread.request(observation)   (non-blocking)
             PolicyThread (one per worker, owns the PolicyClient) ── openpi websocket ──▶ policy server (GPU, same host)

Action chunks never cross /ws/sim (30 Hz x 6 joints stays on vhagar); the gateway only sees SkillProgress (>= 1 Hz)
and one CommandResult.

openpi wire ('openpi_ws'; checked against Physical-Intelligence/openpi ``serving/websocket_policy_server.py`` and
``openpi_client/websocket_client_policy.py``):
- client connects to ``ws://host:port`` (compression off, max_size unlimited); the server FIRST sends its metadata
  dict (msgpack). ``GET /healthz`` answers 200 'OK' (use it for PolicyServerStatus.ready before connecting).
- each request: one binary frame = msgpack-numpy packed observation dict; reply: one binary frame = dict with
  ``"actions"`` (ndarray [chunk, action_dim]) and ``"server_timing"`` ({'infer_ms': ...}).
- a server-side exception arrives as a TEXT frame (the traceback) followed by close code 1011: map it to
  ``policy_diverged`` if it mentions shapes / keys, else ``policy_unavailable``; reconnect before the next exec.
- msgpack-numpy needs ``msgpack`` in the worker's Python (add it to the ``sim`` extra) or the ``openpi-client`` package.
LeRobot async inference ('lerobot_grpc': ``python -m lerobot.async_inference.policy_server --port 8080``, gRPC,
TimedObservation / TimedAction, ``actions_per_chunk``, ``chunk_size_threshold``) is the alternative, but its
RobotClient needs a LeRobot ``Robot``; in Isaac that means our own gRPC client. Not tonight: run a small openpi-wire
server that wraps the LeRobot policy (``SmolVLAPolicy.from_pretrained(model_id)``) instead.

Leaf module: v0.2 ``contracts`` + stdlib + pydantic only (no numpy: observations are typed ``Any``).
"""

import math
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import Field

from contracts.cameras import RobotCameraKind
from contracts.common import Model, WireModel

PolicyProtocol = Literal["openpi_ws", "lerobot_grpc"]
JointUnits = Literal["range_m100_100", "degrees", "radians"]

SO101_JOINT_NAMES: tuple[str, ...] = (
    "shoulder_pan",
    "shoulder_lift",
    "elbow_flex",
    "wrist_flex",
    "wrist_roll",
    "gripper",
)
"""SO-101 joint order of the policy state / action vectors. Same names and order as LeRobot's ``so101_follower``
motors and ``arms.robot.isaac.rover_so101.STOW_JOINTS``; the imported URDF's joint names must be mapped to these."""

HELD_LIFT_MIN_M = 0.02
"""success_check 'held_geometry': the target unit's centre rose at least this much above its shelf rest height ..."""
HELD_JAW_BOX_M: tuple[float, float, float] = (0.08, 0.10, 0.10)
"""... and lies inside this box (x along the jaws, y across, z) centred on the gripper TCP (gripper_frame_link)."""


class PolicyServerSpec(Model):
    """Worker config for one policy server (from env: PICKY_VLA_POLICY_URL / _PROTOCOL / PICKY_VLA_T1_MODEL). The
    keys MUST match what the policy was trained with: read them from ``PolicyServerMetadata`` at connect (the server
    knows its ``policy.config.input_features``) and fail fast with ``policy_unavailable`` on a mismatch."""

    url: str = Field("ws://127.0.0.1:8000", description="openpi default port 8000; LeRobot gRPC default 8080.")
    protocol: PolicyProtocol = "openpi_ws"
    model_id: str = "lerobot/smolvla_base"
    tier: Literal["t1_pretrained", "t2_finetuned"] = "t1_pretrained"
    image_keys: dict[RobotCameraKind, str] = Field(
        default_factory=lambda: {"front": "observation.images.front", "wrist": "observation.images.wrist"},
        description="Robot camera -> observation key (LeRobot style 'observation.images.<name>'; openpi DROID style "
        "'observation/exterior_image_1_left' / 'observation/wrist_image_left').",
    )
    image_size: tuple[int, int] = Field((256, 256), description="(w, h) the worker resizes blurred frames to.")
    state_key: str = "observation.state"
    prompt_key: str = Field("task", description="LeRobot 'task'; openpi 'prompt'.")
    action_dim: int = Field(6, ge=1)
    chunk_size: int = Field(50, ge=1, description="Actions per inference (SmolVLA default 50).")
    action_hz: float = Field(30.0, gt=0, description="Rate the worker applies actions (SO-101 datasets: 30 fps).")
    refill_fraction: float = Field(
        0.5, ge=0, le=1, description="Request the next chunk when the queue is at or below this share (LeRobot g=0.5)."
    )
    joint_units: JointUnits = Field(
        "range_m100_100",
        description="Units of the state / action vectors: LeRobot SO-101 default = body joints in [-100, 100] of the "
        "calibrated range, gripper in [0, 100]; use_degrees=True datasets = degrees. The worker converts to the "
        "articulation's radians with the URDF joint limits.",
    )
    action_abs_max: float = Field(200.0, gt=0, description="|value| above this in a chunk = policy_diverged.")
    connect_timeout_s: float = Field(5.0, gt=0)
    infer_timeout_s: float = Field(2.0, gt=0, description="One inference; exceeded -> policy_timeout.")


class PolicyServerMetadata(WireModel):
    """The first frame an openpi-wire server sends (our wrapper server fills these; unknown keys ignored)."""

    model_id: str | None = None
    policy_type: str | None = Field(None, description="'smolvla', 'pi0', 'act', 'gr00t'.")
    input_features: dict[str, list[int]] = Field(
        default_factory=dict, description="Observation key -> shape, e.g. {'observation.state': [6], ...}."
    )
    action_dim: int | None = None
    chunk_size: int | None = None
    action_hz: float | None = None
    joint_names: list[str] = Field(default_factory=list)
    joint_units: JointUnits | None = None
    prompt_key: str | None = None


class PolicyError(Exception):
    """Raised by a PolicyClient; ``code`` is the VlaErrorCode the worker reports (``vla.skill_error_text``)."""

    def __init__(
        self,
        code: Literal["policy_unavailable", "policy_timeout", "policy_diverged", "internal"],
        message: str = "",
    ) -> None:
        self.code = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


@runtime_checkable
class PolicyClient(Protocol):
    """SYNCHRONOUS client used from the worker's PolicyThread (the Isaac main loop never waits on it). Implementations:
    ``OpenPiWsClient`` (websockets.sync.client + msgpack-numpy, ~60 lines, arms/sim/policy_client.py) and
    a scripted client (deterministic chunks for the worker's unit tests, no GPU; draft: ``ScriptedPolicyClient``)."""

    @property
    def ready(self) -> bool:
        """Connected and the metadata matched the spec."""
        ...

    def connect(self) -> PolicyServerMetadata:
        """Open the connection, read + check the metadata. PolicyError('policy_unavailable') on refusal / mismatch."""
        ...

    def infer(self, observation: Mapping[str, Any]) -> list[list[float]]:
        """One chunk [chunk_size][action_dim] in ``joint_units``. PolicyError on timeout / server error / bad shape."""
        ...

    def reset(self) -> None:
        """New episode (between execs): clear any server-side history."""
        ...

    def close(self) -> None: ...


def build_observation(
    spec: PolicyServerSpec,
    images: Mapping[RobotCameraKind, Any],
    joints: Sequence[float],
    instruction: str,
) -> dict[str, Any]:
    """The observation dict for one inference. ``images``: BLURRED frames (H x W x 3 uint8 arrays, already resized to
    ``spec.image_size``) per robot camera; ``joints``: the 6 SO-101 joint positions in ``spec.joint_units``, in
    SO101_JOINT_NAMES order. KeyError for a camera the spec does not map; ValueError for a wrong joint count."""
    if len(joints) != spec.action_dim:
        raise ValueError(f"expected {spec.action_dim} joint values ({', '.join(SO101_JOINT_NAMES)}), got {len(joints)}")
    obs: dict[str, Any] = {spec.image_keys[cam]: img for cam, img in images.items()}
    obs[spec.state_key] = list(joints)
    obs[spec.prompt_key] = instruction
    return obs


def check_action_chunk(spec: PolicyServerSpec, actions: Sequence[Sequence[float]]) -> str | None:
    """None when the chunk is usable, else the reason (-> PolicyError('policy_diverged')): empty, wrong action_dim,
    NaN / inf, or |value| > action_abs_max."""
    if len(actions) == 0:
        return "empty action chunk"
    for i, row in enumerate(actions):
        if len(row) != spec.action_dim:
            return f"action {i} has {len(row)} values, expected {spec.action_dim}"
        for v in row:
            if not math.isfinite(float(v)) or abs(float(v)) > spec.action_abs_max:
                return f"action {i} out of range: {v}"
    return None
