"""Planner output: the typed ``StepPlan`` (ordered steps, each with machine-checkable pre/post Conditions).

    NL text / Goal ─▶ PlannerService.plan ─▶ StepPlan
      LLM (real planner) proposes a small ``PlanDraft`` (WHAT, in which order: fetch_item / scan_shelf / ask_user /
      navigate_to / checkout / verify_basket); deterministic code compiles it (HOW): poses + lift heights from the
      planogram, ``TargetSpec`` descriptions from the catalog, ``default_conditions`` per step, timeouts, one
      ground/grasp/place triple per remaining unit (qty - picked_qty), ask_user first for optional (recommended)
      items, ask_user pick_choice after the scan for produce, a basket audit + checkout handoff at the end.
      The LLM never writes conditions, poses, budgets or money.

Step kinds (``PlanStep``, discriminated on ``kind``):

    kind           executor  what                                                   v0.2 brain tool it replaces
    navigate_to    vla       drive to a pose (classical planner inside the VLA)     navigate_to
    align          vla       look_at: fine base align + lift pre-position + aim     (part of pick_item / scan_shelf)
                             the pan-tilt head at the slot
    ground_target  verifier  box + grasp point of the exact unit (Perceptron)       (part of pick_item)
    grasp          vla       lift + SO-101 grasp (T0 magic grasp / T1 / T2 policy)   pick_item
    place          vla       put the held unit in the basket (or back on a shelf)   place_in_basket
    verify         verifier  a standalone check (basket audit before checkout)      (new)
    scan_shelf     runner    v0.2 scan path (robot.scan -> identify -> apply_scan)   scan_shelf
    ask_user       runner    v0.2 ask path (trips.ask / approvals.request)          ask_user
    handoff        vla       unload the basket at checkout                          finish_goal (end_at checkout)

Conditions are a tiny three-valued language (atoms + all / any / not, discriminated on ``op``), evaluated by the
verifier (``contracts.verify``): state atoms from RobotState (free), camera atoms from blurred frames (Perceptron
in the real verifier, ground truth in the fake). ``advisory`` atoms (label_matches) fail on a confident mismatch but
never block when they cannot be checked.
"""

from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import Field, computed_field, model_validator

from contracts.cameras import RobotCameraKind, robot_camera_id
from contracts.common import (
    LIFT_MAX_M,
    LIFT_MIN_M,
    REACH_NAV_TOLERANCE_M,
    ApprovalId,
    Cents,
    GoalId,
    ItemId,
    Model,
    Pose2D,
    RecId,
    RequestId,
    RobotId,
    Score01,
    Sku,
    SlotId,
    StopId,
    StoreId,
    Timestamp,
    TripId,
    UserId,
    utcnow,
)
from contracts.evidence import FrameRef
from contracts.fleet import Goal, GoalEndAt, GoalItem
from contracts.grounding import GroundedTarget, SlotLocation, TargetSpec
from contracts.tools import AskKind, NavTarget

# ====================================================================================================================
# Ids (deterministic, so retries / re-attach / replans are idempotent)
# ====================================================================================================================
PlanId = str
PlanStepId = str
ExecId = str
"""Idempotency key of ONE VLA execution attempt: ``make_exec_id(plan_id, step_id, attempt)``."""
VerifyId = str
EffectKey = str
"""``make_effect_key(root_plan_id, scope, kind)``: a side effect (stock pick, cart line, ...) commits at most once."""
BindingKey = str
"""Plan blackboard key, e.g. 'target:item_03:1' -> GroundedTarget."""


def goal_attempt(goal: Goal | None) -> int:
    """Which fleet attempt of the goal this is (``Goal.assignments``: one per attempt, >= 1); 2 after a reassign."""
    if goal is None:
        return 1
    return max(1, len(goal.assignments), *(a.attempt for a in goal.assignments))


def make_plan_id(goal_id: GoalId, attempt: int = 1) -> PlanId:
    """Root plan id: 'plan-g1' (first assignment), 'plan-g1-a2' (the fleet reassigned the goal, ``goal_attempt``).
    MUST differ per attempt: exec / verify / effect ids derive from it, and a reused id would re-attach to the
    previous robot's exec (idempotency_conflict) or return a verdict cached for the previous robot."""
    return f"plan-{goal_id}" if attempt <= 1 else f"plan-{goal_id}-a{attempt}"


def make_exec_id(plan_id: PlanId, step_id: PlanStepId, attempt: int) -> ExecId:
    """'plan-g1:s07:a2'. Re-sending VlaService.execute with the same id re-attaches (never a second motion)."""
    return f"{plan_id}:{step_id}:a{attempt}"


def make_verify_id(plan_id: PlanId, step_id: PlanStepId, attempt: int, phase: str) -> VerifyId:
    """'plan-g1:s07:a2:post' (the verifier caches verdicts by id)."""
    return f"{plan_id}:{step_id}:a{attempt}:{phase}"


def make_effect_key(root_plan_id: PlanId, scope: str, kind: str) -> EffectKey:
    """Effects key on the ROOT plan (re-plans keep the ids of carried-over steps) + a step id or item id + the effect
    kind, never on the attempt: 'plan-g1:s07:record_pick', 'plan-g1:item_03:add_cart_line'."""
    return f"{root_plan_id}:{scope}:{kind}"


def binding_key(item_id: ItemId | None, unit: int | None) -> BindingKey:
    return f"target:{item_id or 'adhoc'}:{unit or 1}"


def recovery_step_id(failed_step_id: PlanStepId, n: int, k: int) -> PlanStepId:
    """Id of the k-th step inserted by the n-th recovery of a failed step: 's07.r1.1'."""
    return f"{failed_step_id}.r{n}.{k}"


class ExecContext(Model):
    """Where a VLA / verifier call comes from (logs, events, fault matching). Never changes semantics."""

    goal_id: GoalId | None = None
    plan_id: PlanId | None = None
    step_id: PlanStepId | None = None
    step_kind: "StepKind | None" = None
    attempt: int = Field(1, ge=1)
    item_id: ItemId | None = None
    trip_id: TripId | None = None

    @property
    def step_key(self) -> str:
        return f"{self.plan_id}:{self.step_id}"


# ====================================================================================================================
# The condition language
# ====================================================================================================================
class _Atom(Model):
    min_confidence: Score01 = Field(0.75, description="Below this the atom is 'uncertain', never pass/fail.")
    advisory: bool = Field(
        False,
        description="A confident FAIL still fails the condition; UNCERTAIN (could not check: label unreadable, "
        "camera down) is ignored instead of making the whole condition uncertain.",
    )
    cameras: list[RobotCameraKind] = Field(default_factory=list, description="Override ATOM_DEFAULT_CAMERAS.")
    note: str = Field("", description="Human-readable form for the UI: 'holding the mild salsa'.")


class RobotAt(_Atom):
    op: Literal["robot_at"] = "robot_at"
    pose: Pose2D
    tolerance_m: float = REACH_NAV_TOLERANCE_M
    yaw_tolerance_rad: float = 0.2


class SlotReachable(_Atom):
    """``contracts.common.slot_reachable(robot pose, slot.approach_pose, slot.item_position)``: THE reach predicate."""

    op: Literal["slot_reachable"] = "slot_reachable"
    slot_id: SlotId


class BatteryAbove(_Atom):
    """battery_pct >= pct (v0.2 BATTERY_MOTION_MIN_PCT semantics). Unknown battery -> uncertain."""

    op: Literal["battery_above"] = "battery_above"
    pct: float = Field(ge=0, le=100)


class SlotVisible(_Atom):
    op: Literal["slot_visible"] = "slot_visible"
    slot_id: SlotId


class ItemOnShelf(_Atom):
    op: Literal["item_on_shelf"] = "item_on_shelf"
    slot_id: SlotId
    sku: Sku
    min_count: int = Field(1, ge=0)


class TargetGrounded(_Atom):
    op: Literal["target_grounded"] = "target_grounded"
    binding: BindingKey
    max_age_s: float = 60.0
    max_motion_m: float = 0.05


class GripperHolding(_Atom):
    op: Literal["gripper_holding"] = "gripper_holding"
    sku: Sku | None = Field(None, description="None = holding anything.")
    instance_id: str | None = Field(
        None, description="A specific unit: state / sim ground truth only (a camera cannot tell units apart)."
    )


class GripperEmpty(_Atom):
    op: Literal["gripper_empty"] = "gripper_empty"


class BasketDelta(_Atom):
    """The basket holds exactly ``delta`` more units of ``sku`` than at the step's baseline (VerifyBaseline)."""

    op: Literal["basket_delta"] = "basket_delta"
    sku: Sku
    delta: int = 1


class BasketContains(_Atom):
    op: Literal["basket_contains"] = "basket_contains"
    counts: dict[Sku, int] = Field(default_factory=dict)
    exact: bool = Field(False, description="True = nothing else in the basket.")


class LabelMatches(_Atom):
    """The held (or ``binding``-grounded) unit's label contains ``expect_text`` (case-insensitive)."""

    op: Literal["label_matches"] = "label_matches"
    sku: Sku
    expect_text: str
    binding: BindingKey | None = None


class ShelfTagReads(_Atom):
    op: Literal["shelf_tag_reads"] = "shelf_tag_reads"
    slot_id: SlotId
    cents: Cents


class ApprovalGranted(_Atom):
    op: Literal["approval_granted"] = "approval_granted"
    approval_id: ApprovalId


class AllOf(Model):
    op: Literal["all"] = "all"
    of: list["Condition"] = Field(min_length=1)


class AnyOf(Model):
    op: Literal["any"] = "any"
    of: list["Condition"] = Field(min_length=1)


class NotCond(Model):
    op: Literal["not"] = "not"
    cond: "Condition"


Atom = (
    RobotAt
    | SlotReachable
    | BatteryAbove
    | SlotVisible
    | ItemOnShelf
    | TargetGrounded
    | GripperHolding
    | GripperEmpty
    | BasketDelta
    | BasketContains
    | LabelMatches
    | ShelfTagReads
    | ApprovalGranted
)
Condition = Annotated[Atom | AllOf | AnyOf | NotCond, Field(discriminator="op")]
AllOf.model_rebuild()
AnyOf.model_rebuild()
NotCond.model_rebuild()

AtomSource = Literal["state", "camera"]
ATOM_SOURCES: dict[str, tuple[AtomSource, ...]] = {
    "robot_at": ("state",),
    "slot_reachable": ("state",),
    "battery_above": ("state",),
    "approval_granted": ("state",),
    "target_grounded": ("state",),
    "gripper_holding": ("camera", "state"),
    "gripper_empty": ("camera", "state"),
    "basket_delta": ("camera", "state"),
    "basket_contains": ("camera", "state"),
    "slot_visible": ("camera",),
    "item_on_shelf": ("camera",),
    "label_matches": ("camera",),
    "shelf_tag_reads": ("camera",),
}
"""Evidence sources per atom. State-only atoms cost nothing (no frame, no model call): the verifier evaluates them
first and short-circuits an ``all`` on a state failure. Dual-source atoms are fused with ``verify.combine_verdicts``
(camera wins a disagreement only at >= VISION_OVERRIDE_CONFIDENCE). Camera-only atoms are 'uncertain' when no camera
answer is available (e.g. the verifier is down and the loop degrades to state-only checks)."""

ATOM_DEFAULT_CAMERAS: dict[str, tuple[RobotCameraKind, ...]] = {
    "slot_visible": ("front",),
    "item_on_shelf": ("front",),
    "gripper_holding": ("wrist", "front"),
    "gripper_empty": ("wrist", "front"),
    "basket_delta": ("basket",),
    "basket_contains": ("basket",),
    "label_matches": ("wrist",),
    "shelf_tag_reads": ("front",),
}
"""Primary camera first; the second is the re-verify view when the first is occluded / uncertain."""


def iter_atoms(cond: Condition | None, path: str = "0") -> Iterator[tuple[str, Atom]]:
    """Depth-first (path, atom) pairs; path '0.1.0' addresses an atom in AtomVerdict / the UI."""
    if cond is None:
        return
    if isinstance(cond, AllOf | AnyOf):
        for i, sub in enumerate(cond.of):
            yield from iter_atoms(sub, f"{path}.{i}")
    elif isinstance(cond, NotCond):
        yield from iter_atoms(cond.cond, f"{path}.0")
    else:
        yield path, cond


def atom_cameras(atom: Atom) -> list[RobotCameraKind]:
    if "camera" not in ATOM_SOURCES[atom.op]:
        return []
    return list(atom.cameras) or list(ATOM_DEFAULT_CAMERAS.get(atom.op, ("front",)))


def cameras_needed(cond: Condition | None, robot_id: RobotId, *, alternate: bool = False) -> list[str]:
    """Robot camera ids the verifier captures for ``cond`` (primary camera per camera atom; ``alternate`` = the second
    camera, for re-verification)."""
    out: list[str] = []
    for _, atom in iter_atoms(cond):
        kinds = atom_cameras(atom)
        if not kinds:
            continue
        kind = kinds[1] if alternate and len(kinds) > 1 else kinds[0]
        cid = robot_camera_id(robot_id, kind)
        if cid not in out:
            out.append(cid)
    return out


def slot_ids_in(cond: Condition | None) -> list[SlotId]:
    """Slots a condition talks about (the truth / verifier loads only these)."""
    out: list[SlotId] = []
    for _, atom in iter_atoms(cond):
        sid = getattr(atom, "slot_id", None)
        if sid and sid not in out:
            out.append(sid)
    return out


def describe(cond: Condition | None) -> str:
    """Compact human form: 'all(at the shelf, gripper free, within reach)'."""
    if cond is None:
        return "true"
    if isinstance(cond, AllOf | AnyOf):
        return f"{cond.op}({', '.join(describe(c) for c in cond.of)})"
    if isinstance(cond, NotCond):
        return f"not({describe(cond.cond)})"
    return cond.note or cond.op


# ====================================================================================================================
# Steps
# ====================================================================================================================
StepKind = Literal[
    "navigate_to", "align", "ground_target", "grasp", "place", "verify", "scan_shelf", "ask_user", "handoff"
]
STEP_KIND_ALIASES: dict[str, StepKind] = {
    "look_at": "align",
    "move_to": "navigate_to",
    "drive": "navigate_to",
    "pick": "grasp",
    "grab": "grasp",
    "locate_target": "ground_target",
    "checkout": "handoff",
}
"""Names a planner / UI may use for a kind ('look_at' IS 'align')."""

StepExecutor = Literal["vla", "verifier", "runner"]
STEP_EXECUTORS: dict[str, StepExecutor] = {
    "navigate_to": "vla",
    "align": "vla",
    "grasp": "vla",
    "place": "vla",
    "handoff": "vla",
    "ground_target": "verifier",
    "verify": "verifier",
    "scan_shelf": "runner",
    "ask_user": "runner",
}
VLA_STEP_KINDS: frozenset[str] = frozenset(k for k, v in STEP_EXECUTORS.items() if v == "vla")

DEFAULT_STEP_TIMEOUTS_S: dict[str, float] = {
    "navigate_to": 90.0,  # = v0.2 SIM_DEFAULT_TIMEOUTS_S['navigate']
    "align": 10.0,
    "ground_target": 8.0,
    "grasp": 30.0,  # T1 budget; T0 typically 4-6 s
    "place": 15.0,
    "verify": 8.0,
    "scan_shelf": 15.0,
    "ask_user": 300.0,
    "handoff": 30.0,
}
DEFAULT_HEARTBEAT_S = 1.0
"""A running VLA execution emits a VlaStatus at least this often; silence > STALL_FACTOR x heartbeat = stalled."""
STALL_FACTOR = 3.0

ExecContext.model_rebuild()

VlaTier = Literal["t0_scripted", "t1_pretrained", "t2_finetuned"]
"""t0_scripted = oracle: classical drive + scripted 'magic grasp' over v0.2 RobotService (always available, Isaac
and 2D fake) · t1_pretrained = pretrained VLA for grasp/place (SmolVLA / GR00T on SO-101) next to Isaac on vhagar ·
t2_finetuned = fine-tuned on the physical SO-101 (later). Movement is classical on every tier."""
VlaTierPref = Literal["auto", "t0_scripted", "t1_pretrained", "t2_finetuned"]
TIER_PREFERENCE: tuple[VlaTier, ...] = ("t2_finetuned", "t1_pretrained", "t0_scripted")


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    SUCCEEDED = "succeeded"
    PROVISIONAL = "provisional"
    """Succeeded on an 'uncertain' postcondition the policy accepted; the pre-checkout basket audit confirms it."""
    FAILED = "failed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


DONE_STEP_STATUSES: frozenset[StepStatus] = frozenset(
    {StepStatus.SUCCEEDED, StepStatus.PROVISIONAL, StepStatus.SKIPPED}
)
CLOSED_STEP_STATUSES: frozenset[StepStatus] = DONE_STEP_STATUSES | {StepStatus.FAILED, StepStatus.CANCELLED}

StepOrigin = Literal["plan", "tool", "recovery", "replan"]


class _StepBase(Model):
    step_id: PlanStepId
    title: str = Field(description="UI line: 'Grab Sol de Casa mild salsa (1/1)'.")
    item_id: ItemId | None = Field(None, description="GoalItem served (shop goals: = route stop_id).")
    unit: int | None = Field(None, ge=1, description="Which unit of a multi-qty item (1-based).")
    group_id: str | None = Field(None, description="Collapsible group in the plan list: 'item:<item_id>', 'checkout'.")
    group_label: str | None = Field(None, description="'Mild salsa x1'.")
    preconditions: Condition | None = None
    postconditions: Condition | None = None
    timeout_s: float | None = Field(None, gt=0, description="None = DEFAULT_STEP_TIMEOUTS_S[kind] (per attempt).")
    max_attempts: int | None = Field(None, ge=1, description="None = the RetryPolicy budget.")
    optional: bool = Field(False, description="Exhausted recovery -> skipped (plan continues) instead of failed.")
    depends_on: list[PlanStepId] = Field(default_factory=list, description="Reserved (plans run sequentially).")
    origin: StepOrigin = "plan"
    inserted_by: PlanStepId | None = Field(None, description="Recovery steps: the failed step they repair.")
    status: StepStatus = StepStatus.PENDING
    attempts: int = Field(0, ge=0, description="Attempts started (ids use it; budgets use charged attempts).")

    def effective_timeout_s(self) -> float:
        return self.timeout_s or DEFAULT_STEP_TIMEOUTS_S[self.kind]  # type: ignore[attr-defined]


class NavigateToStep(_StepBase):
    """Drive to a pose. A classical planner (routing A* + pure pursuit) backs it inside the VLA service, every tier."""

    kind: Literal["navigate_to"] = "navigate_to"
    target: NavTarget
    slot_id: SlotId | None = None
    stop_id: StopId | None = None
    goal_pose: Pose2D = Field(description="Approach pose / checkout / dock, resolved by the planner.")
    waypoints: list[Pose2D] = Field(default_factory=list, description="Optional path hint; empty = the VLA plans.")
    tolerance_m: float = 0.05
    yaw_tolerance_rad: float = 0.05


class AlignStep(_StepBase):
    """look_at / align: fine base alignment to the approach pose, lift pre-position, pan-tilt head aimed at the slot."""

    kind: Literal["align"] = "align"
    slot_id: SlotId
    location: SlotLocation | None = None
    camera: RobotCameraKind = "front"
    lift_height_m: float | None = None


class GroundTargetStep(_StepBase):
    kind: Literal["ground_target"] = "ground_target"
    target: TargetSpec
    camera: RobotCameraKind = "front"
    binding: BindingKey = Field(description="Blackboard key the GroundedTarget is stored under.")


class GraspStep(_StepBase):
    kind: Literal["grasp"] = "grasp"
    target: TargetSpec
    binding: BindingKey | None = Field(None, description="Grounding to aim with (T1 needs it; T0 uses the slot).")
    instruction: str = Field(description="Language instruction for T1/T2: 'pick up the mild salsa jar'.")
    approval_id: ApprovalId | None = Field(None, description="over_threshold approval that unlocked this item.")
    tier: VlaTierPref = "auto"


class PlaceStep(_StepBase):
    kind: Literal["place"] = "place"
    sku: Sku
    destination: Literal["basket", "shelf"] = Field(
        "basket",
        description="'shelf' = put a wrong unit back (needs VlaCapabilities.place_back; v0.2 RobotService has no "
        "place-back primitive, so only the 2D fake's fault overlay supports it tonight).",
    )
    slot_id: SlotId | None = Field(None, description="destination='shelf': where to put it back.")
    instruction: str = "put the item in the basket"
    tier: VlaTierPref = "auto"


class VerifyStep(_StepBase):
    """A standalone check. Its ``condition`` is the step (pre/post unused). purpose='audit' = the pre-checkout basket
    audit that confirms provisional places before the handoff."""

    kind: Literal["verify"] = "verify"
    condition: Condition
    purpose: Literal["audit", "check"] = "check"


class ScanShelfStep(_StepBase):
    kind: Literal["scan_shelf"] = "scan_shelf"
    slot_id: SlotId
    expected_sku: Sku | None = None
    location: SlotLocation | None = None
    min_count: int = Field(1, ge=0, description="Fewer units seen -> failure 'atom:item_on_shelf' (re-source).")


class AskUserStep(_StepBase):
    kind: Literal["ask_user"] = "ask_user"
    ask_kind: AskKind = "question"
    question: str
    options: list[str] = Field(default_factory=list)
    sku: Sku | None = None
    rec_id: RecId | None = None
    evidence: list[FrameRef] = Field(default_factory=list, description="Blurred photo shown with the question.")
    on_decline: Literal["skip_item", "continue", "fail_goal"] = "skip_item"


class HandoffStep(_StepBase):
    """At checkout: unload the basket (robot.unload_basket). The trip flow then runs mandate + approval + payment."""

    kind: Literal["handoff"] = "handoff"
    at: Literal["checkout"] = "checkout"


PlanStep = Annotated[
    NavigateToStep
    | AlignStep
    | GroundTargetStep
    | GraspStep
    | PlaceStep
    | VerifyStep
    | ScanShelfStep
    | AskUserStep
    | HandoffStep,
    Field(discriminator="kind"),
]
VlaStep = Annotated[NavigateToStep | AlignStep | GraspStep | PlaceStep | HandoffStep, Field(discriminator="kind")]


def lift_height_for(item_z_m: float) -> float:
    """Lift pre-position (SO-101 shoulder height) for an item on a board at ``item_z_m``."""
    return round(min(LIFT_MAX_M, max(LIFT_MIN_M, item_z_m + 0.05)), 3)


def default_conditions(
    kind: StepKind,
    *,
    sku: Sku | None = None,
    slot_id: SlotId | None = None,
    approach_pose: Pose2D | None = None,
    binding: BindingKey | None = None,
    expect_text: str | None = None,
    goal_pose: Pose2D | None = None,
    basket_counts: dict[Sku, int] | None = None,
) -> tuple[Condition | None, Condition | None]:
    """THE default (pre, post) per step kind. The planner's compiler attaches these; the LLM never writes conditions.
    Camera-only sanity checks (shelf in view after align, item on the shelf before grounding) are ADVISORY: a confident
    camera 'no' fails the step, but a check that cannot be made (verifier down, camera blocked) never blocks it.

    Place posts list ``gripper_empty`` BEFORE ``basket_delta``: a place that never released is diagnosed
    'place_not_released' (retry the place), not 'dropped' (which would re-pick while still holding)."""
    at: list[Any] = [RobotAt(pose=approach_pose, note="at the shelf")] if approach_pose is not None else []
    if kind == "navigate_to":
        post = RobotAt(pose=goal_pose, note="arrived") if goal_pose is not None else None
        return BatteryAbove(pct=5.0, note="battery ok"), post
    if kind == "align" and slot_id:
        return (AllOf(of=at) if at else None), SlotVisible(slot_id=slot_id, advisory=True, note="shelf in view")
    if kind == "scan_shelf":
        return (AllOf(of=at) if at else None), None
    if kind == "ground_target" and binding:
        pre = (
            ItemOnShelf(slot_id=slot_id, sku=sku, advisory=True, note="item on the shelf") if slot_id and sku else None
        )
        return pre, TargetGrounded(binding=binding, note="target located")
    if kind == "grasp" and sku:
        pre_parts: list[Any] = [*at, GripperEmpty(note="gripper free")]
        if slot_id:
            pre_parts.append(SlotReachable(slot_id=slot_id, note="within reach"))
        post_parts: list[Any] = [GripperHolding(sku=sku, note=f"holding {sku}")]
        if expect_text:
            post_parts.append(
                LabelMatches(sku=sku, expect_text=expect_text, min_confidence=0.6, advisory=True, note="right label")
            )
        post: Any = AllOf(of=post_parts) if len(post_parts) > 1 else post_parts[0]
        return AllOf(of=pre_parts), post
    if kind == "place" and sku:
        return GripperHolding(sku=sku, note=f"holding {sku}"), AllOf(
            of=[GripperEmpty(note="released"), BasketDelta(sku=sku, delta=1, note=f"+1 {sku} in the basket")]
        )
    if kind == "verify" and basket_counts is not None:
        return None, BasketContains(counts=dict(basket_counts), note="basket matches the cart")
    if kind == "handoff":
        return RobotAt(pose=goal_pose, note="at checkout") if goal_pose else None, BasketContains(
            counts={}, exact=True, note="basket unloaded"
        )
    return None, None


# ====================================================================================================================
# StepPlan
# ====================================================================================================================
class PlanStatus(StrEnum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


PlanOrigin = Literal["llm", "scripted", "replan", "tools"]
"""tools = the goal's single live plan in tools mode: the v0.2 LLM brain's motion tool calls append steps to it."""


class PlannerInfo(Model):
    """How the plan was made (shown under the plan title; makes 'LLM proposes, code decides' visible)."""

    origin: PlanOrigin = "scripted"
    mode: Literal["llm", "scripted"] = "scripted"
    model: str | None = Field(
        None, description="'gemini-3.8-flash' (the utils.llm 'brain' model) / None for the scripted planner."
    )
    latency_ms: float = 0.0
    rationale: str = Field("", description="The LLM's one-paragraph reasoning ('Dairy last so it stays cold').")
    fixes: list[str] = Field(
        default_factory=list, description="What CODE changed in the draft: 'dropped unknown item x', 'added audit'."
    )
    fallback_reason: str | None = Field(
        None,
        description="Why the scripted draft ran instead of the LLM's: 'no GEMINI_API_KEY', 'LLM timeout after "
        "20 s', 'invalid draft: ...', 'refusal'. None when origin='llm' or nobody tried the LLM.",
    )


class StepPlan(Model):
    plan_id: PlanId
    root_plan_id: PlanId = Field(description="First revision's id (= plan_id for revision 1); effect keys use it.")
    revision: int = Field(1, ge=1)
    supersedes: PlanId | None = None
    goal_id: GoalId = Field(description="Fleet goal (NL-only plans get an 'adhoc-<hash>' id).")
    robot_id: RobotId
    store_id: StoreId
    trip_id: TripId | None = None
    user_id: UserId | None = None
    request_id: RequestId | None = None
    mode: Literal["plan", "tools"] = "plan"
    title: str = ""
    planner: PlannerInfo = Field(default_factory=PlannerInfo)
    items: list[GoalItem] = Field(default_factory=list, description="Goal items served (snapshot, incl. online).")
    budget_cents: Cents | None = None
    end_at: GoalEndAt = "checkout"
    steps: list[PlanStep] = Field(default_factory=list)
    bindings: dict[BindingKey, GroundedTarget] = Field(default_factory=dict, description="Blackboard.")
    policy_id: str = "default"
    status: PlanStatus = PlanStatus.DRAFT
    reason: str = Field("", description="Why this revision exists ('mild salsa out: taking medium').")
    created_at: Timestamp = Field(default_factory=utcnow)
    updated_at: Timestamp = Field(default_factory=utcnow)

    def step(self, step_id: PlanStepId) -> PlanStep | None:
        return next((s for s in self.steps if s.step_id == step_id), None)

    def item(self, item_id: ItemId | None) -> GoalItem | None:
        return next((i for i in self.items if i.item_id == item_id), None) if item_id else None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def progress(self) -> float:
        if not self.steps:
            return 0.0
        return round(sum(1 for s in self.steps if s.status in DONE_STEP_STATUSES) / len(self.steps), 3)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def current_step_id(self) -> PlanStepId | None:
        return next((s.step_id for s in self.steps if s.status not in CLOSED_STEP_STATUSES), None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def est_total_cents(self) -> int:
        """In-store estimate: sum of price x qty of the items the plan picks."""
        return sum((i.price_cents or 0) * i.qty for i in self.items if i.status not in ("online", "unavailable"))


# ====================================================================================================================
# LLM-facing draft + planner input
# ====================================================================================================================
DraftKind = Literal["fetch_item", "scan_shelf", "ask_user", "navigate_to", "checkout", "verify_basket"]


class DraftStep(Model):
    """LLM-facing step (structured-output friendly: 5 optional fields): WHAT, not HOW. There is deliberately no
    'add_item': only handle_message (a user saying 'also grab limes') may add goal items; drafts naming unknown items
    are fixed. The field descriptions are part of the JSON schema the LLM sees (``utils.llm.complete_json``)."""

    kind: DraftKind = Field(
        description="fetch_item = drive to the item, look, pick every remaining unit, put them in the basket (needs "
        "item_id) · scan_shelf = look at a shelf without picking (needs slot_id) · ask_user = ask the shopper (needs "
        "question; item_id when it is about an item) · navigate_to = drive somewhere (target checkout/dock/entrance) "
        "· checkout = basket audit + drive to checkout + hand off (always last) · verify_basket = basket audit only."
    )
    item_id: ItemId | None = Field(None, description="A goal item id copied EXACTLY from the item list.")
    slot_id: SlotId | None = Field(None, description="scan_shelf only: a slot id from the item list.")
    target: NavTarget | None = Field(None, description="navigate_to only.")
    question: str | None = Field(None, description="ask_user only: one short sentence the shopper answers yes/no.")
    note: str = Field("", description="Optional reason for this step, shown in the plan list.")


class PlanDraft(Model):
    summary: str = Field(description="Plan title for the UI, <= 60 characters: 'Taco night for 6'.")
    steps: list[DraftStep] = Field(min_length=1, description="In execution order. One fetch_item per item.")
    rationale: str = Field("", description="One or two sentences: why this order ('dairy last so it stays cold').")


DRAFT_MACROS: dict[str, tuple[StepKind, ...]] = {
    "fetch_item": ("navigate_to", "align", "scan_shelf", "ground_target", "grasp", "place"),
    "scan_shelf": ("navigate_to", "align", "scan_shelf"),
    "ask_user": ("ask_user",),
    "navigate_to": ("navigate_to",),
    "checkout": ("verify", "navigate_to", "handoff"),
    "verify_basket": ("verify",),
}
"""Compile templates. fetch_item: optional item -> ask_user(recommendation) first; produce -> ask_user(pick_choice)
after scan_shelf; units = qty - picked_qty, each extra unit repeats EXTRA_UNIT_MACRO."""
EXTRA_UNIT_MACRO: tuple[StepKind, ...] = ("ground_target", "grasp", "place")

OPEN_ITEM_STATUSES: tuple[str, ...] = ("pending", "located", "picked")
"""Goal item statuses that still need steps ('picked' = some units placed, picked_qty < qty)."""
DEFAULT_PLAN_TIMEOUT_S = 20.0
"""Wall budget for ONE LLM draft call (PICKY_PLAN_TIMEOUT_S). The real planner wraps its utils.llm call in
asyncio.wait_for(this) and falls back to the scripted draft (PlannerInfo.fallback_reason) on timeout."""


def normalize_draft(
    draft: PlanDraft,
    items: Sequence[GoalItem],
    *,
    end_at: GoalEndAt = "checkout",
    lock_order: bool = False,
) -> tuple[PlanDraft, list[str]]:
    """Deterministic repair of an LLM draft BEFORE compile (the LLM proposes, code decides). Returns (draft, fixes).

    - drops steps naming unknown item ids, fetch_item without item_id, duplicate fetch_item of one item (a second fetch
      would pick every unit twice), duplicate item asks, navigate_to slot/stop (fetch / scan carry their own driving)
    - moves an item's ask_user right before its fetch_item (never after the pick)
    - ``lock_order`` (shop goals: the trip route is the order the shopper sees on the map): fetches are re-sequenced
      into goal item order in the slots where the draft put fetches
    - appends a fetch_item for every open in-store item the draft forgot (with its asks)
    - checkout / verify_basket only at the end: exactly one checkout when end_at='checkout' (added if missing, dropped
      when end_at is dock / none); a lone verify_basket is kept last
    - an empty result becomes the scripted fallback step (navigate_to checkout / dock)."""
    fixes: list[str] = []
    known = {i.item_id: i for i in items}

    def label(it_id: str | None) -> str:
        it = known.get(it_id or "")
        return f"{it_id} ({it.name or it.query})" if it is not None else str(it_id)

    def needs_steps(it: GoalItem) -> bool:
        return it.status in OPEN_ITEM_STATUSES and bool(it.slot_id) and bool(it.sku) and it.picked_qty < it.qty

    body: list[DraftStep] = []
    asks: dict[str, list[DraftStep]] = {}
    fetched: list[str] = []
    saw_checkout = saw_verify = False
    for d in draft.steps:
        if d.item_id is not None and d.item_id not in known:
            fixes.append(f"dropped {d.kind} for unknown item {d.item_id}")
            continue
        if d.kind == "fetch_item":
            if d.item_id is None:
                fixes.append("dropped fetch_item without an item_id")
            elif d.item_id in fetched:
                fixes.append(f"dropped duplicate fetch of {label(d.item_id)}")
            else:
                fetched.append(d.item_id)
                body.append(d)
        elif d.kind == "ask_user" and d.item_id is not None:
            if d.item_id in asks:
                fixes.append(f"dropped duplicate question about {label(d.item_id)}")
                continue
            if d.item_id in fetched:
                fixes.append(f"moved the question about {label(d.item_id)} before its pick")
            asks[d.item_id] = [d]
        elif d.kind == "checkout":
            saw_checkout = True
        elif d.kind == "verify_basket":
            saw_verify = True
            if end_at == "checkout":
                fixes.append("dropped verify_basket (the checkout step audits the basket)")
        elif d.kind == "navigate_to" and d.target in ("slot", "stop", None):
            fixes.append(f"dropped navigate_to {d.target or '?'} (fetch_item / scan_shelf drive there themselves)")
        else:
            body.append(d)

    if lock_order:
        order = [i.item_id for i in items if i.item_id in fetched]
        if order != fetched:
            fixes.append("kept the trip route order for the picks")
            slots = iter(order)
            by_item = {d.item_id: d for d in body if d.kind == "fetch_item"}
            body = [by_item[next(slots)] if d.kind == "fetch_item" else d for d in body]

    for it in items:
        if needs_steps(it) and it.item_id not in fetched:
            fixes.append(f"added the missing pick of {label(it.item_id)}")
            body.append(DraftStep(kind="fetch_item", item_id=it.item_id, note="added by the compiler"))
            fetched.append(it.item_id)

    steps: list[DraftStep] = []
    for d in body:
        if d.kind == "fetch_item" and d.item_id in asks:
            steps.extend(asks.pop(d.item_id))
        steps.append(d)
    for leftover in asks.values():  # questions about items with no pick (e.g. already in the basket): keep them
        steps.extend(leftover)

    last = [s.kind for s in draft.steps if s.kind in ("checkout", "verify_basket")]
    if end_at == "checkout":
        if not saw_checkout:
            fixes.append("added the basket audit + checkout handoff")
        elif draft.steps[-1].kind != "checkout" or last.count("checkout") > 1:
            fixes.append("moved checkout to the end")
        steps.append(DraftStep(kind="checkout"))
    else:
        if saw_checkout:
            fixes.append(f"dropped checkout (this goal ends at the {end_at})")
        if saw_verify:
            steps.append(DraftStep(kind="verify_basket"))
    if not steps:
        steps.append(DraftStep(kind="navigate_to", target="dock" if end_at == "dock" else "checkout"))
        fixes.append("empty draft: drive to the " + ("dock" if end_at == "dock" else "checkout"))
    return draft.model_copy(update={"steps": steps}), fixes


class PlanRequest(Model):
    """PlannerService.plan input: a fleet Goal (normal pipeline) or raw NL text (standalone / demo)."""

    robot_id: RobotId
    goal: Goal | None = None
    text: str | None = Field(None, description="'tacos for 6, under $40, one vegetarian' (used when goal is None).")
    store_id: StoreId | None = None
    user_id: UserId | None = None
    request_id: RequestId | None = None
    budget_cents: Cents | None = None
    end_at: GoalEndAt | None = None
    instructions: str | None = Field(None, description="Extra NL guidance for the LLM planner.")
    mode: Literal["plan", "tools"] = "plan"

    @model_validator(mode="after")
    def _shape(self) -> "PlanRequest":
        if self.goal is None and not (self.text and self.text.strip()):
            raise ValueError("PlanRequest needs a goal or text")
        if self.goal is not None and self.store_id is None:
            self.store_id = self.goal.store_id
        if self.store_id is None:
            raise ValueError("PlanRequest needs store_id when there is no goal")
        return self


# ====================================================================================================================
# Validation (deterministic; run on every plan / revision before it runs)
# ====================================================================================================================
class PlanIssue(Model):
    severity: Literal["error", "warning"]
    step_id: PlanStepId | None = None
    code: str
    message: str


def validate_plan(plan: StepPlan, items: Sequence[GoalItem] | None = None) -> list[PlanIssue]:
    """Structural checks; errors block execution. Rules: unique step ids · depends_on earlier · no grasp while holding
    · no place with an empty gripper · grasp bindings are grounded somewhere · every item_id is a known goal item (no
    exemptions: drafts cannot add items) · an optional item is asked about (ask_user recommendation) before its first
    grasp · a 'plan'-mode plan that places into the basket and hands off has a basket audit before the handoff
    (provisional places are confirmed there) · warnings: driving while holding, VLA step without postcondition,
    plan ends holding."""
    issues: list[PlanIssue] = []
    known = {i.item_id: i for i in (items if items is not None else plan.items)}

    def err(code: str, msg: str, step_id: str | None = None, severity: str = "error") -> None:
        issues.append(PlanIssue(severity=severity, step_id=step_id, code=code, message=msg))  # type: ignore[arg-type]

    seen: set[str] = set()
    asked: set[str] = set()
    holding = False
    audit_seen = False
    places = 0
    for s in plan.steps:
        if s.step_id in seen:
            err("dup_step", "duplicate step_id", s.step_id)
        for dep in s.depends_on:
            if dep not in seen:
                err("bad_dep", f"depends_on {dep!r} is not an earlier step", s.step_id)
        seen.add(s.step_id)
        if s.item_id is not None and known and s.item_id not in known:
            err("unknown_item", f"item_id {s.item_id!r} is not a goal item", s.step_id)
        if isinstance(s, AskUserStep) and s.ask_kind == "recommendation" and s.item_id:
            asked.add(s.item_id)
        if isinstance(s, VerifyStep) and s.purpose == "audit":
            audit_seen = True
        closed = s.status in CLOSED_STEP_STATUSES
        if isinstance(s, GraspStep):
            item = known.get(s.item_id or "")
            if item is not None and item.optional and s.item_id not in asked and not closed:
                err("optional_not_asked", f"optional item {s.item_id} grasped before asking the shopper", s.step_id)
            if holding and not closed:
                err("double_grasp", "grasp while already holding (place first)", s.step_id)
            if (
                s.binding
                and s.binding not in plan.bindings
                and not any(isinstance(p, GroundTargetStep) and p.binding == s.binding for p in plan.steps)
            ):
                err("unbound_target", f"binding {s.binding!r} is never grounded", s.step_id)
            if not closed or s.status in DONE_STEP_STATUSES - {StepStatus.SKIPPED}:
                holding = True
        elif isinstance(s, PlaceStep):
            if not holding and not closed:
                err("place_empty", "place without a preceding grasp", s.step_id)
            if s.status not in (StepStatus.SKIPPED, StepStatus.CANCELLED):
                holding = False
            places += 1 if s.destination == "basket" else 0
        elif isinstance(s, NavigateToStep) and holding and not closed:
            err("drive_holding", "driving while holding an item", s.step_id, "warning")
        elif isinstance(s, HandoffStep) and plan.mode == "plan" and places and not audit_seen:
            err("no_audit", "basket audit (verify purpose='audit') must come before the handoff", s.step_id)
        if s.kind in VLA_STEP_KINDS and s.postconditions is None and not isinstance(s, AlignStep):
            err("unverified", "VLA step without postcondition: success is unverifiable", s.step_id, "warning")
    if holding:
        err("ends_holding", "plan ends with an item in the gripper", None, "warning")
    return issues


def plan_errors(plan: StepPlan, items: Sequence[GoalItem] | None = None) -> list[PlanIssue]:
    return [i for i in validate_plan(plan, items) if i.severity == "error"]
