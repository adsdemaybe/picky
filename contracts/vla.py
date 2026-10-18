"""VLA API: ONE execution API over every tier (T0 scripted / T1 pretrained / T2 fine-tuned).

    VlaService.execute(VlaRequest) ═▶ VlaStatus stream:
        accepted (tier resolved; or failed at once: precondition / busy / unknown_robot / idempotency_conflict)
        ▸ running* (>= 1 per heartbeat_s: progress, stage, chunk_no)
        ▸ exactly ONE terminal: succeeded | failed (typed VlaError) | cancelled

Rules (the runner relies on them):
- ``exec_id = make_exec_id(plan, step, attempt)`` is the idempotency key: the same id + the same ``body_hash``
  re-attaches (statuses with seq > after_seq are replayed, then live) and never starts a second motion; the same id
  with a different body -> one 'failed' status with ``idempotency_conflict``.
- No hidden retries: the only fallback inside one exec is to the next tier when the resolved tier is unavailable AT
  ACCEPT (``allow_fallback``; reported in ``accepted.fell_back_from``). Failures while running come back typed; the
  runner's RetryPolicy decides. Honest attempt counts in the UI.
- 'succeeded' is a CLAIM (``VlaOutcome.self_report``), never a commit: the verifier checks the step's postcondition
  before stock / goal / cart change.
- Deadlines are enforced on both sides: the service fails ``deadline_exceeded`` after ``deadline_s``; the runner's
  ``services.run_vla_to_terminal`` also detects stalls (no status for STALL_FACTOR x heartbeat_s), cancels and
  synthesizes a terminal status.
- Isaac loss mid-step: terminal 'failed' ``disconnected`` (free retry: counts_attempt=False); the robot continues on
  the 2D fake from its last state and capabilities drop to T0.
- Privacy: learned tiers consume camera frames. A tier is usable only when ``input_frames_blurred`` (the worker blurs
  faces with YuNet before frames reach the policy); T0 consumes no frames.
"""

from collections.abc import Collection, Sequence
from typing import Any, Literal

from pydantic import Field, computed_field

from contracts.cameras import RobotCameraKind
from contracts.common import (
    CmdId,
    FrameId,
    InstanceId,
    Model,
    Pose2D,
    RobotId,
    Score01,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    WireModel,
    utcnow,
)
from contracts.evidence import FrameRef, fingerprint
from contracts.grounding import GroundedTarget, NormBox
from contracts.plan import (
    DEFAULT_HEARTBEAT_S,
    TIER_PREFERENCE,
    ExecContext,
    ExecId,
    StepKind,
    VlaStep,
    VlaTier,
    VlaTierPref,
)
from contracts.sim import CommandResult, RobotBackend, RobotState, SimErrorCode

__all__ = [
    "ERROR_TRAITS",
    "SIM_TO_VLA_ERROR",
    "TERMINAL_VLA_PHASES",
    "TIER_PREFERENCE",
    "VLA_TO_BRAIN_CODE",
    "ErrorCategory",
    "ErrorTraits",
    "ExecRecord",
    "PolicyServerStatus",
    "RunSkillCmd",
    "SKILL_ERROR_PREFIX",
    "SkillProgress",
    "skill_error_text",
    "t1_capability_from_status",
    "vla_error_from_skill_result",
    "VlaCapabilities",
    "VlaError",
    "VlaErrorCode",
    "VlaOutcome",
    "VlaPhase",
    "VlaRequest",
    "VlaStatus",
    "VlaTier",
    "VlaTierPref",
    "resolve_tier",
]

ErrorCategory = Literal["precondition", "motion", "manipulation", "policy", "stock", "infra", "safety"]
VlaErrorCode = Literal[
    # precondition (checked at accept; the runner's verifier pre-check normally catches them first)
    "precondition_failed",
    "not_at_pose",
    "out_of_reach",
    "target_not_grounded",
    "target_stale",
    "unsupported_step",
    # motion
    "path_blocked",
    "nav_timeout",
    "collision",
    # manipulation
    "grasp_missed",
    "grasp_slipped",
    "wrong_item_grasped",
    "place_failed",
    "gripper_fault",
    # policy (T1 / T2 only)
    "policy_unavailable",
    "policy_timeout",
    "policy_diverged",
    "policy_low_confidence",
    # stock
    "no_instance",
    "unknown_slot",
    # infra / safety
    "disconnected",
    "busy",
    "not_ready",
    "cancelled",
    "deadline_exceeded",
    "idempotency_conflict",
    "internal",
    "battery_low",
    "unknown_robot",
]


class ErrorTraits(Model):
    category: ErrorCategory
    retryable: bool = Field(description="The same step may run again (maybe after inserted recovery steps).")
    t1_strike: bool = Field(False, description="Counts toward the T1/T2 circuit breaker when that tier was used.")
    counts_attempt: bool = Field(True, description="False = infra hiccup; does not consume the step's attempt budget.")
    fatal: bool = Field(False, description="Ends the goal on this robot: the brain raises RobotError, fleet reassigns.")


_T = ErrorTraits
ERROR_TRAITS: dict[str, ErrorTraits] = {
    "precondition_failed": _T(category="precondition", retryable=True),
    "not_at_pose": _T(category="precondition", retryable=True),
    "out_of_reach": _T(category="precondition", retryable=True),
    "target_not_grounded": _T(category="precondition", retryable=True),
    "target_stale": _T(category="precondition", retryable=True, counts_attempt=False),
    "unsupported_step": _T(category="precondition", retryable=False, counts_attempt=False),
    "path_blocked": _T(category="motion", retryable=True),
    "nav_timeout": _T(category="motion", retryable=True),
    "collision": _T(category="safety", retryable=True),
    "grasp_missed": _T(category="manipulation", retryable=True, t1_strike=True),
    "grasp_slipped": _T(category="manipulation", retryable=True, t1_strike=True),
    "wrong_item_grasped": _T(category="manipulation", retryable=True, t1_strike=True),
    "place_failed": _T(category="manipulation", retryable=True, t1_strike=True),
    "gripper_fault": _T(category="safety", retryable=False, fatal=True),
    "policy_unavailable": _T(category="policy", retryable=True, t1_strike=True, counts_attempt=False),
    "policy_timeout": _T(category="policy", retryable=True, t1_strike=True),
    "policy_diverged": _T(category="policy", retryable=True, t1_strike=True),
    "policy_low_confidence": _T(category="policy", retryable=True, t1_strike=True),
    "no_instance": _T(category="stock", retryable=False),
    "unknown_slot": _T(category="stock", retryable=False),
    "disconnected": _T(category="infra", retryable=True, counts_attempt=False),
    "busy": _T(category="infra", retryable=True, counts_attempt=False),
    "not_ready": _T(category="infra", retryable=True, counts_attempt=False),
    "cancelled": _T(category="infra", retryable=False, counts_attempt=False),
    "deadline_exceeded": _T(category="infra", retryable=True, t1_strike=True),
    "idempotency_conflict": _T(category="infra", retryable=False, counts_attempt=False),
    "internal": _T(category="infra", retryable=True),
    "battery_low": _T(category="safety", retryable=False, fatal=True),
    "unknown_robot": _T(category="infra", retryable=False, fatal=True),
}

SIM_TO_VLA_ERROR: dict[str, VlaErrorCode] = {
    "unknown_slot": "unknown_slot",
    "no_instance": "no_instance",
    "grasp_failed": "grasp_missed",
    "path_blocked": "path_blocked",
    "timeout": "nav_timeout",
    "cancelled": "cancelled",
    "busy": "busy",
    "not_ready": "not_ready",
    "disconnected": "disconnected",
    "unknown_robot": "unknown_robot",
    "battery_low": "battery_low",
    "internal": "internal",
}
"""v0.2 RobotError / CommandResult codes -> VLA codes (T0 wraps RobotService, so every SimErrorCode maps)."""

VLA_TO_BRAIN_CODE: dict[str, str] = {
    **{v: k for k, v in SIM_TO_VLA_ERROR.items()},
    "grasp_slipped": "grasp_failed",
    "wrong_item_grasped": "grasp_failed",
    "place_failed": "grasp_failed",
    "place_not_released": "grasp_failed",
    "out_of_reach": "grasp_failed",
    "not_at_pose": "grasp_failed",
    "nav_timeout": "timeout",
    "policy_timeout": "timeout",
    "deadline_exceeded": "timeout",
    "policy_unavailable": "not_ready",
    "unsupported_step": "internal",
}
"""VLA / diagnosis codes -> the v0.2 ``BrainToolError.error_code`` vocabulary, so the LLM brain's prompts keep working
(tools mode)."""


class VlaError(Model):
    code: VlaErrorCode
    message: str = ""
    sim_code: SimErrorCode | None = Field(None, description="Underlying RobotError code (T0 / sim).")
    tier: VlaTier | None = None
    details: dict[str, Any] = Field(default_factory=dict, description="{'stalled': True} for a runner-detected stall.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def category(self) -> ErrorCategory:
        return ERROR_TRAITS[self.code].category

    @computed_field  # type: ignore[prop-decorator]
    @property
    def retryable(self) -> bool:
        return ERROR_TRAITS[self.code].retryable

    @classmethod
    def from_sim(cls, code: str, message: str = "", tier: VlaTier | None = "t0_scripted") -> "VlaError":
        return cls(
            code=SIM_TO_VLA_ERROR.get(code, "internal"),
            message=message or code,
            sim_code=code if code in SIM_TO_VLA_ERROR else None,  # type: ignore[arg-type]
            tier=tier,
        )


class VlaCapabilities(Model):
    """One tier on one robot right now (``VlaService.capabilities``; GET /api/fleet/robots/{id}/vla)."""

    robot_id: RobotId
    tier: VlaTier
    available: bool
    reason: str = Field("", description="'policy server on vhagar unreachable', 'store on 2D fake: T0 only'.")
    backend: RobotBackend | None = None
    step_kinds: list[StepKind] = Field(default_factory=list)
    model_id: str | None = Field(None, description="'scripted', 'lerobot/smolvla_base', 'nvidia/GR00T-N1.5-3B'.")
    policy_host: str | None = Field(None, description="'vhagar' (co-located with Isaac; action chunks stay local).")
    action_hz: float | None = None
    chunk_size: int | None = None
    needs_grounding: bool = Field(False, description="True = GraspStep.binding required (T0: the slot is enough).")
    cameras: list[RobotCameraKind] = Field(default_factory=list, description="Camera inputs the policy consumes.")
    input_frames_blurred: bool = Field(
        True,
        description="Frames reaching the policy are face-blurred (CLAUDE.md section 8). A learned tier with False is "
        "never selected by resolve_tier. T0 consumes no frames.",
    )
    place_back: bool = Field(False, description="Can put a held unit back on its shelf (PlaceStep destination shelf).")
    supports_progress: bool = True
    cancel_latency_ms: float = Field(200.0, description="Worst-case stop latency after cancel().")
    est_success_rate: dict[str, Score01] = Field(default_factory=dict, description="Per step kind (rolling).")


class VlaRequest(Model):
    exec_id: ExecId = Field(description="Idempotency key (make_exec_id). Same id + same body = re-attach.")
    robot_id: RobotId
    store_id: StoreId | None = None
    step: VlaStep
    tier: VlaTierPref = "auto"
    allow_fallback: bool = Field(
        True,
        description="If the resolved tier is unavailable AT ACCEPT, run the next tier inside this exec (accepted."
        "fell_back_from). Mid-run failures never fall back silently.",
    )
    grounding: GroundedTarget | None = None
    deadline_s: float = Field(gt=0, description="Wall budget from 'accepted'; the service fails deadline_exceeded.")
    heartbeat_s: float = Field(DEFAULT_HEARTBEAT_S, gt=0)
    capture_after: list[RobotCameraKind] = Field(
        default_factory=list, description="Cameras captured right after the action (outcome.frames) for the post check."
    )
    context: ExecContext = Field(default_factory=ExecContext)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def body_hash(self) -> str:
        """Fingerprint of what is executed (step without run state, tier, grounding id); compared on exec_id reuse."""
        return fingerprint(
            {
                "robot": self.robot_id,
                "step": self.step.model_dump(mode="json", exclude={"status", "attempts"}),
                "tier": self.tier,
                "grounding": self.grounding.grounding_id if self.grounding else None,
            }
        )


VlaPhase = Literal["accepted", "running", "succeeded", "failed", "cancelled"]
TERMINAL_VLA_PHASES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})


class VlaOutcome(Model):
    """Terminal payload: the VLA's CLAIM + references for the verifier. Never committed without verification."""

    self_report: Literal["success", "unsure", "failure"] = "success"
    self_confidence: Score01 | None = Field(None, description="T1/T2 policy confidence, if it has one.")
    final_state: RobotState | None = None
    holding_instance_id: InstanceId | None = None
    frames: list[FrameRef] = Field(default_factory=list, description="Captured after the action (capture_after).")
    ended_at: Timestamp | None = Field(
        None,
        description="When the motion finished, GATEWAY clock: the moment the RobotService call returned (T0) or the "
        "worker's CommandResult arrived (T1/T2), never a worker-side timestamp. capture_after frames are taken at / "
        "after it. The runner passes it as VerifyRequest.not_before: a frame older than this never counts.",
    )
    duration_ms: float = 0.0
    chunks_executed: int | None = None


class VlaStatus(Model):
    """One update of an execution stream. ``seq`` is per exec_id, 1-based, gap-free; replays keep original seqs."""

    exec_id: ExecId
    robot_id: RobotId
    seq: int = Field(ge=1)
    phase: VlaPhase
    tier: VlaTier | None = Field(None, description="Resolved tier (from 'accepted' on).")
    backend: RobotBackend | None = None
    progress: Score01 | None = None
    stage: str | None = Field(None, description="'driving', 'aligning', 'reaching', 'closing', 'lifting', ...")
    message: str = ""
    eta_s: float | None = None
    chunk_no: int | None = Field(None, description="T1/T2 action chunk counter (proves liveness).")
    pose: Pose2D | None = None
    fell_back_from: VlaTier | None = Field(None, description="'accepted' only: tier skipped at accept time.")
    error: VlaError | None = Field(None, description="phase failed / cancelled.")
    outcome: VlaOutcome | None = Field(None, description="Terminal phases.")
    ts: Timestamp = Field(default_factory=utcnow)

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_VLA_PHASES


class ExecRecord(Model):
    """Everything known about one exec_id (VlaService.get_exec); kept >= 1 h for idempotent re-attach + audit."""

    exec_id: ExecId
    body_hash: str
    request: VlaRequest
    statuses: list[VlaStatus] = Field(default_factory=list)
    created_at: Timestamp = Field(default_factory=utcnow)

    @property
    def terminal(self) -> VlaStatus | None:
        last = self.statuses[-1] if self.statuses else None
        return last if last is not None and last.terminal else None


def resolve_tier(
    pref: VlaTierPref,
    caps: Sequence[VlaCapabilities],
    step_kind: StepKind,
    open_tiers: Collection[str] = (),
) -> VlaTier:
    """THE tier choice (runner and VLA service use the same rule): the requested tier if usable, else (or for 'auto')
    the first usable tier in TIER_PREFERENCE. Usable = available, supports ``step_kind``, breaker not open, and (for
    learned tiers) ``input_frames_blurred``. T0 is the floor: it wins even when its capability row is missing."""
    by_tier = {c.tier: c for c in caps}
    order: list[VlaTier] = list(TIER_PREFERENCE) if pref == "auto" else [pref, *TIER_PREFERENCE]  # type: ignore[list-item]
    for tier in order:
        cap = by_tier.get(tier)
        if cap is None or not cap.available or step_kind not in cap.step_kinds or tier in open_tiers:
            continue
        if tier != "t0_scripted" and not cap.input_frames_blurred:
            continue
        return tier
    return "t0_scripted"


# ====================================================================================================================
# PROPOSED sim wire additions (ship ONLY with CONTRACTS_VERSION 0.3.0 + an Isaac worker rebuild; not part of the
# 0.2.1 T0 loop). INERT in 0.2.1: none of these is in ``contracts.sim.GatewayToSim`` / ``SimMessage`` /
# ``MOTION_COMMANDS``, so nothing sends or parses them yet and v0.2 workers are unaffected. WireModel = unknown fields
# ignored on both ends.
# ====================================================================================================================
class RunSkillCmd(WireModel):
    """Motion command: run a learned skill ON THE WORKER (policy server on vhagar next to Isaac, so 30 Hz action chunks
    never cross the websocket; worker side: ``contracts.skill_server``). Advertised by the worker as capability
    'vla:t1:<model_id>' + ``PolicyServerStatus``. One CommandResult at the end: ok=True with ``state`` (holding set
    from the worker's held-geometry check) + ``instance_id``; failures error_code 'internal' + error
    ``skill_error_text(code, msg)`` ('vla:grasp_missed: ...'), so v0.2 parsers cope (``vla_error_from_skill_result``).
    The worker MUST blur faces (YuNet) on every frame before it reaches the policy (``blur_faces`` is always True).

    Worker sequence: refuse at once if busy / battery / not hosted / model mismatch / policy not ready
    (policy_unavailable) -> lift to ``lift_height_m`` (pretrained SO-101 policies drive the 6 arm joints only, never the
    lift or the base) -> loop at ``action_hz``: blur + build the observation, PolicyClient.infer in a thread, execute
    the chunk on the SO-101 articulation, SkillProgress >= 1 Hz -> stop on ``success_check``, ``max_chunks`` or
    ``deadline_s`` -> CommandResult."""

    type: Literal["run_skill"] = "run_skill"
    cmd_id: CmdId
    robot_id: RobotId
    skill: Literal["grasp", "place", "align"]
    tier: Literal["t1_pretrained", "t2_finetuned"]
    instruction: str
    model_id: str | None = Field(None, description="Must equal PolicyServerStatus.model_id (else policy_unavailable).")
    slot_id: SlotId | None = None
    sku: Sku | None = None
    instance_id: InstanceId | None = None
    target_box: NormBox | None = Field(None, description="Grounding in the front camera frame (aim / crop hint).")
    target_frame_id: FrameId | None = None
    cameras: list[RobotCameraKind] = Field(
        default_factory=lambda: ["front", "wrist"],
        description="Robot cameras fed to the policy, mapped to its image keys by the worker's PolicyServerSpec.",
    )
    lift_height_m: float | None = Field(None, description="Lift pre-position before the policy starts (None = keep).")
    max_chunks: int | None = Field(None, ge=1, description="Hard cap on action chunks (None = until deadline_s).")
    success_check: Literal["held_geometry", "basket_geometry", "none"] = Field(
        "held_geometry",
        description="How the worker ends a run early with ok=True: grasp = the target unit sits between the jaws and "
        "is lifted >= skill_server.HELD_LIFT_MIN_M off its shelf; place = the unit lies inside the basket volume; "
        "none = run to max_chunks / deadline, then report (self_report 'unsure').",
    )
    assist_attach: bool = Field(
        True,
        description="After a geometrically confirmed grasp, attach the unit kinematically (as the T0 magic grasp does) "
        "so it cannot slip while driving. Reported in the result (error text 'assisted'); never attaches a unit that "
        "is not between the jaws.",
    )
    deadline_s: float = 30.0
    blur_faces: Literal[True] = True
    exec_id: ExecId = Field(description="Echoed on SkillProgress.")


class SkillProgress(WireModel):
    """Unsolicited, >= 1 Hz while a RunSkillCmd runs (feeds VlaStatus 'running' heartbeats)."""

    type: Literal["skill_progress"] = "skill_progress"
    cmd_id: CmdId
    exec_id: ExecId
    robot_id: RobotId
    stage: str
    progress: Score01 | None = None
    chunk_no: int | None = None
    self_confidence: Score01 | None = None
    ts: Timestamp = Field(default_factory=utcnow)


class PolicyServerStatus(WireModel):
    """Worker -> gateway (PROPOSED 0.3.0): readiness of the learned-policy server next to Isaac. Sent right after the
    welcome, on every change (server up / down / model swapped) and every 30 s. ``SimHosted`` carries no capabilities,
    so this is how a policy server that comes up AFTER the hello becomes usable. The robot arm turns it into the T1/T2
    ``VlaCapabilities`` rows (``t1_capability_from_status``)."""

    type: Literal["policy_status"] = "policy_status"
    worker_id: str = "isaac-0"
    tier: Literal["t1_pretrained", "t2_finetuned"] = "t1_pretrained"
    model_id: str = Field(description="'lerobot/smolvla_base'.")
    protocol: Literal["openpi_ws", "lerobot_grpc"] = "openpi_ws"
    ready: bool
    reason: str = Field("", description="'policy server ws://127.0.0.1:8000 refused', 'warming up'.")
    robot_ids: list[RobotId] = Field(
        default_factory=list, description="Robots with an articulated SO-101 it can drive (proxy-arm rovers cannot)."
    )
    skills: list[Literal["grasp", "place", "align"]] = Field(default_factory=lambda: ["grasp", "place"])
    action_hz: float | None = None
    chunk_size: int | None = None
    cameras: list[RobotCameraKind] = Field(default_factory=lambda: ["front", "wrist"])
    input_frames_blurred: bool = Field(True, description="The worker blurs (YuNet) before the policy sees a frame.")
    p50_infer_ms: float | None = None
    ts: Timestamp = Field(default_factory=utcnow)


SKILL_ERROR_PREFIX = "vla:"
_SKILL_TO_STEP: dict[str, StepKind] = {"grasp": "grasp", "place": "place", "align": "align"}


def skill_error_text(code: VlaErrorCode, message: str = "") -> str:
    """Worker side: ``CommandResult.error`` for a failed RunSkillCmd ('vla:grasp_missed: closed on air')."""
    return f"{SKILL_ERROR_PREFIX}{code}: {message}" if message else f"{SKILL_ERROR_PREFIX}{code}"


def vla_error_from_skill_result(result: CommandResult, tier: VlaTier | None = "t1_pretrained") -> VlaError | None:
    """Gateway side: the typed error of a RunSkillCmd result (None when ok). 'vla:<code>' texts map to that VlaErrorCode
    (unknown codes -> 'internal'); plain v0.2 codes (busy / battery_low / disconnected ...) go through from_sim."""
    if result.ok:
        return None
    text = result.error or ""
    if text.startswith(SKILL_ERROR_PREFIX):
        code, _, msg = text[len(SKILL_ERROR_PREFIX) :].partition(":")
        code = code.strip()
        return VlaError(
            code=code if code in ERROR_TRAITS else "internal",  # type: ignore[arg-type]
            message=msg.strip() or code,
            tier=tier,
            details={} if code in ERROR_TRAITS else {"worker_code": code},
        )
    return VlaError.from_sim(result.error_code or "internal", text, tier)


def t1_capability_from_status(
    robot_id: RobotId, status: PolicyServerStatus | None, backend: RobotBackend | None
) -> VlaCapabilities:
    """THE T1/T2 availability rule: store on Isaac AND a ready policy server that drives this robot AND blurred inputs.
    ``status`` None = the worker never reported a policy server."""
    tier: VlaTier = status.tier if status is not None else "t1_pretrained"
    reason = ""
    if backend != "isaac":
        reason = "store on the 2D fake: T0 only"
    elif status is None:
        reason = "no policy server reported by the Isaac worker"
    elif not status.ready:
        reason = status.reason or "policy server not ready"
    elif robot_id not in status.robot_ids:
        reason = "this rover has no articulated SO-101 in the scene"
    elif not status.input_frames_blurred:
        reason = "worker does not blur policy inputs"
    return VlaCapabilities(
        robot_id=robot_id,
        tier=tier,
        available=not reason,
        reason=reason,
        backend=backend,
        step_kinds=[_SKILL_TO_STEP[k] for k in (status.skills if status else ["grasp", "place"])],
        model_id=status.model_id if status else None,
        policy_host="vhagar",
        action_hz=status.action_hz if status else None,
        chunk_size=status.chunk_size if status else None,
        needs_grounding=False,
        cameras=list(status.cameras) if status else ["front", "wrist"],
        input_frames_blurred=status.input_frames_blurred if status else True,
        cancel_latency_ms=1000.0 / (status.action_hz or 30.0) * 2 if status else 200.0,
    )
