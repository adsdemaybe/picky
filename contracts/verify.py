"""Verifier contract: verify(condition, camera frames) -> Verdict, ground(target) -> GroundedTarget, and the pure
evaluation code both the fake verifier and the runner's degrade path use.

    VerifyRequest(condition, frames | capture, baseline, bindings, not_before, mode)
      1. state atoms from RobotState / blackboard (free; an ``all`` with a failing state atom short-circuits)
      2. camera atoms grouped by camera, one provider round trip per frame (Perceptron Multilook)
      3. dual-source atoms (gripper / basket) fused with ``combine_verdicts`` (camera overrides state only when
         confident >= VISION_OVERRIDE_CONFIDENCE)
      4. an uncertain camera atom gets one re-capture from the next camera (ATOM_DEFAULT_CAMERAS)
      5. Kleene ``combine`` -> Verdict{outcome pass|fail|uncertain, confidence, reason, atoms, evidence, frames,
         decided_by, provider attribution, shadow opinions, ground_truth_agrees, degraded}

Modes (honesty about who decided): ``decide`` = the provider decides (Isaac renders) · ``shadow`` = sim ground truth
decides, the provider's opinion is shown next to it with agreement (default on 2D-fake stores, where Pillow shelves can
fool a VLM) · ``gt_only`` = no provider calls (CI, no key). ``Verdict.decided_by`` always says who decided.

Frames: never judged when captured before ``not_before`` (the VLA terminal ts): no stale-frame false passes.
Provider failures never raise (worst case 'uncertain' + reason, ``degraded``). ``VerifierUnavailableError`` is raised
only when NO verdict can be produced; the runner then applies its degrade policy (state-only checks by code).
"""

import math
import re
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, computed_field, model_validator

from contracts.cameras import RobotCameraKind, robot_camera_id
from contracts.common import (
    ApprovalId,
    InstanceId,
    Model,
    Pose2D,
    RobotId,
    Score01,
    Sku,
    SlotId,
    StoreId,
    Timestamp,
    angle_diff,
    slot_reachable,
    utcnow,
)
from contracts.evidence import FrameRef, SlotTruth, TruthSnapshot
from contracts.grounding import (
    DecisionSource,
    Evidence,
    GroundedTarget,
    GroundingId,
    ProviderCall,
    TargetSpec,
    VerifyMethod,
    VisionProviderName,
)
from contracts.plan import (
    ATOM_SOURCES,
    AllOf,
    AnyOf,
    ApprovalGranted,
    Atom,
    BasketContains,
    BasketDelta,
    BatteryAbove,
    BindingKey,
    Condition,
    ExecContext,
    GripperEmpty,
    GripperHolding,
    ItemOnShelf,
    LabelMatches,
    NotCond,
    RobotAt,
    ShelfTagReads,
    SlotReachable,
    SlotVisible,
    TargetGrounded,
    VerifyId,
    atom_cameras,
)

VerdictOutcome = Literal["pass", "fail", "uncertain"]
VerifierMode = Literal["decide", "shadow", "gt_only"]
VerifyPhase = Literal["pre", "post", "verify", "reverify", "adhoc"]
DEFAULT_VERIFIER_MODE: dict[str, VerifierMode] = {"fake": "shadow", "isaac": "decide", "none": "gt_only"}
"""Default mode by the store's robot backend (RobotState.backend)."""

VERIFY_PASS_CONFIDENCE = 0.6
"""A camera answer below this is 'uncertain' (never pass / fail)."""
VISION_OVERRIDE_CONFIDENCE = 0.85
"""A camera answer that disagrees with robot state wins only at or above this confidence (else 'uncertain').
Perceptron returns no calibrated score: the real verifier should give >= 0.9 only when two cues agree (e.g. a held box
in GRIPPER_ROI_WRIST AND 'no' to 'is the gripper empty?'), 0.7 for a single cue, so one noisy answer never overrides
the exact robot state of a T0 magic grasp."""
VERIFY_DEADLINE_MARGIN_S = 1.0
"""The runner waits ``RetryPolicy.degrade.verify_timeout_s`` per verifier call and sends ``deadline_s`` = that minus
this margin (``verify_deadline``), so a verifier that honours its deadline always answers before the runner gives up
and degrades. Per provider call: min(PICKY_VISION_CALL_TIMEOUT_S, time left in deadline_s)."""
DEFAULT_VISION_CALL_TIMEOUT_S = 4.0


def verify_deadline(runner_timeout_s: float, margin_s: float = VERIFY_DEADLINE_MARGIN_S) -> float:
    """``VerifyRequest.deadline_s`` / ``GroundRequest.deadline_s`` for a runner that waits ``runner_timeout_s``."""
    return max(0.5, runner_timeout_s - margin_s)


class VerifierUnavailableError(RuntimeError):
    """The verifier cannot produce ANY verdict (service down / no frames, no truth, no state). Provider failures alone
    never raise; the runner catches this (and timeouts) and applies ``policy.VerifierDegradePolicy``."""


# ====================================================================================================================
# Results
# ====================================================================================================================
class AtomVerdict(Model):
    path: str
    op: str
    outcome: VerdictOutcome
    confidence: Score01
    reason: str = ""
    advisory: bool = False
    decided_by: DecisionSource = "ground_truth"
    method: VerifyMethod = "fake"
    evidence: list[Evidence] = Field(default_factory=list)
    observed: dict[str, Any] = Field(default_factory=dict, description="{'held_sku': 'SALSA-HOT'} etc.")
    latency_ms: float = 0.0


class ShadowOpinion(Model):
    """A second opinion shown next to the deciding verdict (shadow mode: the provider's view)."""

    provider: ProviderCall
    outcome: VerdictOutcome
    confidence: Score01 = 0.0
    reason: str = ""
    evidence: list[Evidence] = Field(default_factory=list)


class VerifyBaseline(Model):
    """Snapshot taken BEFORE a step so postconditions can be relative ("one MORE salsa in the basket")."""

    baseline_id: str
    robot_id: RobotId
    basket_counts: dict[Sku, int] = Field(default_factory=dict)
    slot_counts: dict[SlotId, int] = Field(default_factory=dict)
    holding_sku: Sku | None = None
    holding_instance_id: InstanceId | None = None
    frames: list[FrameRef] = Field(default_factory=list)
    decided_by: DecisionSource = "ground_truth"
    ts: Timestamp = Field(default_factory=utcnow)


class VerifyRequest(Model):
    verify_id: VerifyId = Field(description="Idempotent: the same id returns the cached Verdict.")
    robot_id: RobotId
    store_id: StoreId | None = None
    phase: VerifyPhase = "post"
    condition: Condition
    frames: list[FrameRef] = Field(default_factory=list, description="Empty = capture fresh (cameras_needed).")
    baseline: VerifyBaseline | None = None
    bindings: dict[BindingKey, GroundedTarget] = Field(default_factory=dict)
    approvals: dict[ApprovalId, bool] = Field(
        default_factory=dict,
        description="Shopper answers the RUNNER knows (approval_granted atoms; the verifier has no broker access, so "
        "without this an approval_granted atom is always 'uncertain').",
    )
    context: ExecContext | None = None
    not_before: Timestamp | None = Field(
        None, description="Only frames captured at/after this (VlaOutcome.ended_at) count: no stale-frame passes."
    )
    mode: VerifierMode | None = Field(None, description="None = the verifier's configured mode.")
    alternate_cameras: bool = Field(False, description="Re-verify: use each atom's second camera.")
    deadline_s: float = Field(
        7.0,
        gt=0,
        description="TOTAL wall budget for this verify (captures + every provider call + re-capture). The runner sets "
        "verify_deadline(verify_timeout_s). When it runs out, answer with what is decided so far (undecided camera "
        "atoms 'uncertain', degraded=True); never overrun it.",
    )


class Verdict(Model):
    """Answer of VerifierService.verify (published inside ``step.verified``)."""

    verify_id: VerifyId
    robot_id: RobotId
    phase: VerifyPhase = "post"
    outcome: VerdictOutcome
    confidence: Score01
    reason: str = Field(description="One line for the UI: 'Wrist camera: gripper is empty (expected mild salsa)'.")
    atoms: list[AtomVerdict] = Field(default_factory=list, description="Leaf results, path order.")
    frames: list[FrameRef] = Field(default_factory=list)
    decided_by: DecisionSource = "ground_truth"
    provider: ProviderCall | None = Field(None, description="Deciding call (attribution badge).")
    shadow: list[ShadowOpinion] = Field(default_factory=list)
    ground_truth_agrees: bool | None = Field(None, description="Sim only: did the deciding source match ground truth?")
    degraded: bool = Field(False, description="A preferred source failed and a fallback decided (amber in the UI).")
    methods_tried: list[VerifyMethod] = Field(default_factory=list)
    latency_ms: float = 0.0
    ts: Timestamp = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def passed(self) -> bool:
        return self.outcome == "pass"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def evidence(self) -> list[Evidence]:
        """Flattened leaf evidence (<= 8, failing atoms first) for overlays."""
        order = {"fail": 0, "uncertain": 1, "pass": 2}
        out: list[Evidence] = []
        for a in sorted(self.atoms, key=lambda a: order[a.outcome]):
            out.extend(a.evidence)
        return out[:8]


class GroundRequest(Model):
    grounding_id: GroundingId
    robot_id: RobotId
    target: TargetSpec
    camera: RobotCameraKind = "front"
    frames: list[FrameRef] = Field(default_factory=list, description="Empty = capture a fresh frame from ``camera``.")
    expects: Literal["point", "box"] = "point"
    read_label: bool = False
    min_confidence: Score01 = 0.6
    context: ExecContext | None = None
    mode: VerifierMode | None = None
    deadline_s: float = Field(7.0, gt=0, description="TOTAL budget (capture + detect + point); see VerifyRequest.")


class GroundingResult(Model):
    grounding_id: GroundingId
    found: bool
    target: GroundedTarget | None = None
    reason: str = ""
    methods_tried: list[VerifyMethod] = Field(default_factory=list)
    degraded: bool = False
    latency_ms: float = 0.0


class VerifierCapabilities(Model):
    mode: VerifierMode = "shadow"
    methods: list[VerifyMethod] = Field(default_factory=list, description="Usable now, in fallback order.")
    provider: VisionProviderName = "fake"
    model: str | None = None
    healthy: bool = True
    reason: str = ""
    atoms: list[str] = Field(default_factory=lambda: list(ATOM_SOURCES))


class ProviderInfo(Model):
    """GET /api/verifier/providers: 'Vision by Perceptron Mk1.5 · 42 calls · p50 610 ms · 97% agree with sim'."""

    provider: VisionProviderName
    model: str | None = None
    roles: list[Literal["grounding", "verification", "label_reading"]] = Field(default_factory=list)
    available: bool = True
    reason: str | None = None
    mode: VerifierMode = "shadow"
    calls: int = 0
    errors: int = 0
    p50_latency_ms: float | None = None
    agreement_with_ground_truth: Score01 | None = None


VerifyPreset = Literal["gripper", "basket", "shelf"]


class VerifyNowRequest(Model):
    """POST /api/verify (P1): run a check now on a robot's cameras (the 'check the basket' judge button)."""

    robot_id: RobotId
    preset: VerifyPreset | None = None
    condition: Condition | None = None
    sku: Sku | None = Field(None, description="preset gripper: the SKU it should hold (None = anything).")
    slot_id: SlotId | None = Field(None, description="preset shelf: the slot to look at.")
    counts: dict[Sku, int] = Field(default_factory=dict, description="preset basket: expected contents.")
    mode: VerifierMode | None = None

    @model_validator(mode="after")
    def _something(self) -> "VerifyNowRequest":
        if self.preset is None and self.condition is None:
            raise ValueError("give preset or condition")
        if self.preset == "shelf" and not self.slot_id and self.condition is None:
            raise ValueError("preset 'shelf' needs slot_id")
        return self

    def resolved_condition(self) -> Condition:
        if self.condition is not None:
            return self.condition
        if self.preset == "gripper":
            return GripperHolding(sku=self.sku, note="holding the item")
        if self.preset == "basket":
            return BasketContains(counts=dict(self.counts), note="basket contents")
        return SlotVisible(slot_id=self.slot_id or "", note="shelf in view")


VisionExpect = Literal["text", "box", "point", "bool"]


class VisionAnswer(Model):
    """Normalized answer of one VisionProvider call (0-1000 coordinates in Evidence.box / point)."""

    text: str = ""
    yes: bool | None = Field(None, description="expects='bool': parsed yes/no.")
    boxes: list[Evidence] = Field(default_factory=list)
    points: list[Evidence] = Field(default_factory=list)
    confidence: Score01 = 0.5
    call: ProviderCall


# ====================================================================================================================
# Pure evaluation code
# ====================================================================================================================
Part = tuple[VerdictOutcome, float, bool]
"""(outcome, confidence, advisory) of one sub-condition."""


def combine(op: Literal["all", "any", "not"], parts: list[Part]) -> tuple[VerdictOutcome, float]:
    """Kleene three-valued combination with confidences. Advisory parts that are 'uncertain' are dropped first (could
    not be checked); if nothing is left the result is a low-confidence pass.

    all: any fail -> fail (max fail conf); else any uncertain -> uncertain (min); else pass (min conf).
    any: any pass -> pass (max pass conf); else any uncertain -> uncertain; else fail (min conf).
    not: swaps pass/fail, keeps uncertain and the confidence."""
    if op == "not":
        outcome, conf, _ = parts[0]
        return {"pass": "fail", "fail": "pass", "uncertain": "uncertain"}[outcome], conf  # type: ignore[return-value]
    kept = [(o, c) for o, c, adv in parts if not (adv and o == "uncertain")]
    if not kept:
        return "pass", 0.5
    win, lose = ("fail", "pass") if op == "all" else ("pass", "fail")
    winners = [c for o, c in kept if o == win]
    if winners:
        return win, max(winners)  # type: ignore[return-value]
    unsure = [c for o, c in kept if o == "uncertain"]
    if unsure:
        return "uncertain", min(unsure)
    return lose, min(c for _, c in kept)  # type: ignore[return-value]


def combine_verdicts(
    state_v: AtomVerdict | None,
    vision_v: AtomVerdict | None,
    *,
    pass_confidence: float = VERIFY_PASS_CONFIDENCE,
    override_confidence: float = VISION_OVERRIDE_CONFIDENCE,
) -> AtomVerdict:
    """THE camera + robot-state fusion rule for dual-source atoms (gripper_holding / gripper_empty / basket_*):
    - one source only -> that one (a camera answer below ``pass_confidence`` becomes uncertain);
    - both agree -> the camera verdict with the higher confidence, 'state agrees';
    - camera uncertain -> state, confidence capped at 0.8;
    - disagree -> camera if confidence >= ``override_confidence`` (catches a policy that closed on air while the
      state says holding), else uncertain."""
    if state_v is None and vision_v is None:
        raise ValueError("combine_verdicts needs at least one verdict")
    if vision_v is None:
        assert state_v is not None
        return state_v
    vis = vision_v
    if vis.outcome != "uncertain" and vis.confidence < pass_confidence:
        vis = vis.model_copy(update={"outcome": "uncertain", "reason": f"low confidence: {vis.reason}"})
    if state_v is None or state_v.outcome == "uncertain":
        return vis
    if vis.outcome == "uncertain":
        return state_v.model_copy(
            update={"confidence": min(state_v.confidence, 0.8), "reason": f"{state_v.reason} (camera inconclusive)"}
        )
    if vis.outcome == state_v.outcome:
        return vis.model_copy(
            update={"confidence": max(vis.confidence, state_v.confidence), "reason": f"{vis.reason}; state agrees"}
        )
    if vis.confidence >= override_confidence:
        return vis.model_copy(update={"reason": f"{vis.reason}; overrides robot state ({state_v.reason})"})
    return vis.model_copy(
        update={
            "outcome": "uncertain",
            "confidence": 0.5,
            "reason": f"camera ({vis.reason}) vs state ({state_v.reason})",
        }
    )


AtomFn = Callable[[str, Atom], AtomVerdict]


def evaluate(cond: Condition, atom_fn: AtomFn, path: str = "0") -> tuple[VerdictOutcome, float, list[AtomVerdict]]:
    """Generic Kleene evaluation of a condition tree with a per-atom evaluator -> (outcome, confidence, leaves).
    Applies each atom's ``min_confidence`` (below it -> uncertain)."""
    if isinstance(cond, AllOf | AnyOf):
        parts: list[Part] = []
        leaves: list[AtomVerdict] = []
        for i, sub in enumerate(cond.of):
            o, c, ls = evaluate(sub, atom_fn, f"{path}.{i}")
            adv = not isinstance(sub, AllOf | AnyOf | NotCond) and sub.advisory
            parts.append((o, c, adv))
            leaves.extend(ls)
        o, c = combine(cond.op, parts)
        return o, c, leaves
    if isinstance(cond, NotCond):
        o, c, ls = evaluate(cond.cond, atom_fn, f"{path}.0")
        o, c = combine("not", [(o, c, False)])
        return o, c, ls
    av = atom_fn(path, cond)
    if av.outcome != "uncertain" and av.confidence < cond.min_confidence:
        av = av.model_copy(update={"outcome": "uncertain", "reason": f"{av.reason} (low confidence)"})
    av = av.model_copy(update={"advisory": cond.advisory})
    if path == "0":  # a bare atom is a one-part 'all': an unverifiable advisory atom does not block
        o, c = combine("all", [(av.outcome, av.confidence, cond.advisory)])
        return o, c, [av]
    return av.outcome, av.confidence, [av]


def _av(
    path: str,
    atom: Atom,
    outcome: VerdictOutcome,
    reason: str,
    *,
    conf: float = 1.0,
    decided_by: DecisionSource = "ground_truth",
    method: VerifyMethod = "ground_truth",
    evidence: list[Evidence] | None = None,
    observed: dict[str, Any] | None = None,
) -> AtomVerdict:
    return AtomVerdict(
        path=path,
        op=atom.op,
        outcome=outcome,
        confidence=conf,
        reason=reason,
        decided_by=decided_by,
        method=method,
        evidence=evidence or [],
        observed=observed or {},
    )


def _state_atom(
    path: str,
    atom: Atom,
    *,
    pose: Pose2D | None,
    battery_pct: float | None,
    approvals: Mapping[str, bool],
    bindings: Mapping[str, GroundedTarget],
    slots: Mapping[SlotId, SlotTruth],
    now: datetime,
) -> AtomVerdict | None:
    """Atoms decided from robot state / blackboard alone (shared by the truth and the state evaluators)."""
    st: dict[str, Any] = {"decided_by": "state", "method": "state"}
    if isinstance(atom, RobotAt):
        if pose is None:
            return _av(path, atom, "uncertain", "pose unknown", conf=0.0, **st)
        d = pose.distance_to(atom.pose)
        dyaw = abs(angle_diff(pose.theta, atom.pose.theta))
        ok = d <= atom.tolerance_m + 1e-9 and dyaw <= atom.yaw_tolerance_rad + 1e-9
        return _av(path, atom, "pass" if ok else "fail", f"{d:.2f} m / {math.degrees(dyaw):.0f} deg from target", **st)
    if isinstance(atom, SlotReachable):
        slot = slots.get(atom.slot_id)
        if slot is None or pose is None:
            return _av(path, atom, "uncertain", "slot geometry unknown", conf=0.0, **st)
        ok = slot_reachable(pose, slot.approach_pose, slot.item_position)
        return _av(path, atom, "pass" if ok else "fail", "within reach" if ok else "out of reach", **st)
    if isinstance(atom, BatteryAbove):
        if battery_pct is None:
            return _av(path, atom, "uncertain", "battery unknown", conf=0.0, **st)
        ok = battery_pct >= atom.pct
        return _av(path, atom, "pass" if ok else "fail", f"battery {battery_pct:.0f}%", **st)
    if isinstance(atom, ApprovalGranted):
        got = approvals.get(atom.approval_id)
        if got is None:
            return _av(path, atom, "uncertain", "no answer yet", conf=0.0, **st)
        return _av(path, atom, "pass" if got else "fail", "approved" if got else "declined", **st)
    if isinstance(atom, TargetGrounded):
        g = bindings.get(atom.binding)
        if g is None:
            return _av(path, atom, "fail", f"{atom.binding} not grounded", **st)
        fresh = g.is_fresh(now, pose, max_age_s=atom.max_age_s, max_motion_m=atom.max_motion_m)
        return _av(path, atom, "pass" if fresh else "fail", "grounded" if fresh else "grounding stale", **st)
    return None


def evaluate_on_truth(
    cond: Condition,
    truth: TruthSnapshot,
    *,
    baseline: VerifyBaseline | None = None,
    bindings: Mapping[str, GroundedTarget] | None = None,
    now: datetime | None = None,
    camera_confidence: float | None = None,
    approvals: Mapping[str, bool] | None = None,
) -> tuple[VerdictOutcome, float, list[AtomVerdict]]:
    """EXACT evaluation on ground truth (the fake verifier; the real verifier's labelled 'ground_truth' fallback in
    sim). Camera atoms whose cameras are ALL occluded -> uncertain (so camera faults exercise the uncertain branch).
    ``camera_confidence`` overrides the confidence of camera-decided atoms (the 'low_confidence' fault).
    ``approvals`` (= VerifyRequest.approvals) override ``truth.approvals``."""
    binds = bindings or {}
    when = now or utcnow()
    answers = {**truth.approvals, **(approvals or {})}

    def occluded(atom: Atom) -> bool:
        kinds = atom_cameras(atom)
        return bool(kinds) and all(robot_camera_id(truth.robot_id, k) in truth.occluded for k in kinds)

    def fn(path: str, atom: Atom) -> AtomVerdict:
        s = _state_atom(
            path,
            atom,
            pose=truth.pose,
            battery_pct=truth.battery_pct,
            approvals=answers,
            bindings=binds,
            slots=truth.slots,
            now=when,
        )
        if s is not None:
            return s
        if occluded(atom):
            return _av(path, atom, "uncertain", "camera view blocked", conf=0.0)
        if isinstance(atom, GripperHolding):
            h = truth.holding
            ok = (
                h is not None
                and (atom.sku is None or h.sku == atom.sku)
                and (atom.instance_id is None or h.instance_id == atom.instance_id)
            )
            if h is None:
                why = "gripper is empty" + (f" (expected {atom.sku})" if atom.sku else "")
            else:
                why = f"holding {h.sku}" if ok else f"holding {h.sku}, not {atom.sku}"
            ev = Evidence(
                kind="state",
                role="held",
                mention="gripper",
                text=why,
                sku=h.sku if h else None,
                instance_id=h.instance_id if h else None,
                matches_expectation=ok,
            )
            return _av(
                path, atom, "pass" if ok else "fail", why, evidence=[ev], observed={"held_sku": h.sku if h else None}
            )
        if isinstance(atom, GripperEmpty):
            ok = truth.holding is None
            held = truth.holding.sku if truth.holding else None
            return _av(
                path,
                atom,
                "pass" if ok else "fail",
                "gripper empty" if ok else f"still holding {held}",
                observed={"held_sku": held},
            )
        if isinstance(atom, SlotVisible):
            ok = any(atom.slot_id in s for c, s in truth.visible_slots.items() if c not in truth.occluded)
            return _av(path, atom, "pass" if ok else "fail", "slot in view" if ok else "slot not in view")
        if isinstance(atom, ItemOnShelf):
            slot = truth.slots.get(atom.slot_id)
            n = sum(1 for i in (slot.instances if slot else []) if i.sku == atom.sku)
            return _av(
                path,
                atom,
                "pass" if n >= atom.min_count else "fail",
                f"{n} x {atom.sku} on the shelf",
                observed={"count": n},
            )
        if isinstance(atom, BasketDelta):
            if baseline is None:
                return _av(path, atom, "uncertain", "no baseline for a relative check", conf=0.0)
            d = truth.basket_counts().get(atom.sku, 0) - baseline.basket_counts.get(atom.sku, 0)
            ok = d == atom.delta
            return _av(
                path,
                atom,
                "pass" if ok else "fail",
                f"basket {d:+d} {atom.sku} (want {atom.delta:+d})",
                observed={"delta": d},
            )
        if isinstance(atom, BasketContains):
            have = truth.basket_counts()
            ok = all(have.get(s, 0) >= n for s, n in atom.counts.items())
            if atom.exact:
                ok = ok and have == {s: n for s, n in atom.counts.items() if n}
            return _av(
                path, atom, "pass" if ok else "fail", f"basket {dict(sorted(have.items()))}", observed={"basket": have}
            )
        if isinstance(atom, LabelMatches):
            unit = truth.holding
            g = binds.get(atom.binding) if atom.binding else None
            if g is not None and g.instance_id:
                unit = next(
                    (i for s in truth.slots.values() for i in s.instances if i.instance_id == g.instance_id), unit
                )
            if unit is None:
                return _av(path, atom, "uncertain", "no unit to read", conf=0.0)
            label = truth.labels.get(unit.instance_id, unit.sku)
            ok = atom.expect_text.casefold() in label.casefold()
            ev = Evidence(
                kind="text",
                role="label",
                text=label,
                sku=unit.sku,
                instance_id=unit.instance_id,
                matches_expectation=ok,
            )
            return _av(
                path,
                atom,
                "pass" if ok else "fail",
                f"label reads {label!r}",
                evidence=[ev],
                observed={"label": label, "held_sku": unit.sku},
            )
        if isinstance(atom, ShelfTagReads):
            slot = truth.slots.get(atom.slot_id)
            tag = slot.shelf_tag_cents if slot else None
            if tag is None:
                return _av(path, atom, "uncertain", "no tag", conf=0.0)
            return _av(path, atom, "pass" if tag == atom.cents else "fail", f"tag reads {tag}c")
        return _av(path, atom, "uncertain", f"unsupported atom {atom.op}", conf=0.0)

    def fn_conf(path: str, atom: Atom) -> AtomVerdict:
        av = fn(path, atom)
        if camera_confidence is not None and av.decided_by != "state" and atom_cameras(atom):
            av = av.model_copy(update={"confidence": camera_confidence})
        return av

    return evaluate(cond, fn_conf)


class StateFacts(Model):
    """What robot state alone tells (v0.2 RobotState + the held unit's SKU): the runner's degrade path input."""

    robot_id: RobotId
    pose: Pose2D | None = None
    battery_pct: float | None = None
    holding_instance_id: InstanceId | None = None
    holding_sku: Sku | None = None
    basket_counts: dict[Sku, int] = Field(default_factory=dict)
    approvals: dict[str, bool] = Field(default_factory=dict)
    slots: dict[SlotId, SlotTruth] = Field(default_factory=dict, description="Slot geometry for slot_reachable.")
    ts: Timestamp = Field(default_factory=utcnow)


def evaluate_on_state(
    cond: Condition,
    facts: StateFacts,
    *,
    baseline: VerifyBaseline | None = None,
    bindings: Mapping[str, GroundedTarget] | None = None,
    now: datetime | None = None,
) -> tuple[VerdictOutcome, float, list[AtomVerdict]]:
    """State-only evaluation (deterministic code, no camera): used by the runner when the verifier is unavailable
    (``VerifierDegradePolicy.on_unavailable='state_only'``). Dual-source atoms are decided from state (confidence 0.8);
    camera-only atoms are uncertain (advisory ones are then ignored by ``combine``)."""
    binds = bindings or {}
    when = now or utcnow()
    st: dict[str, Any] = {"decided_by": "state", "method": "state", "conf": 0.8}

    def fn(path: str, atom: Atom) -> AtomVerdict:
        s = _state_atom(
            path,
            atom,
            pose=facts.pose,
            battery_pct=facts.battery_pct,
            approvals=facts.approvals,
            bindings=binds,
            slots=facts.slots,
            now=when,
        )
        if s is not None:
            return s
        if isinstance(atom, GripperHolding):
            held = facts.holding_sku if facts.holding_instance_id else None
            ok = facts.holding_instance_id is not None and (atom.sku is None or held == atom.sku)
            why = "robot state: gripper empty" if facts.holding_instance_id is None else f"robot state: holding {held}"
            return _av(path, atom, "pass" if ok else "fail", why, observed={"held_sku": held}, **st)
        if isinstance(atom, GripperEmpty):
            ok = facts.holding_instance_id is None
            return _av(
                path,
                atom,
                "pass" if ok else "fail",
                "robot state: gripper empty" if ok else f"robot state: holding {facts.holding_sku or 'something'}",
                observed={"held_sku": facts.holding_sku if not ok else None},
                **st,
            )
        if isinstance(atom, BasketDelta):
            if baseline is None:
                return _av(path, atom, "uncertain", "no baseline", conf=0.0, decided_by="state", method="state")
            d = facts.basket_counts.get(atom.sku, 0) - baseline.basket_counts.get(atom.sku, 0)
            return _av(
                path, atom, "pass" if d == atom.delta else "fail", f"robot state: basket {d:+d} {atom.sku}", **st
            )
        if isinstance(atom, BasketContains):
            have = facts.basket_counts
            ok = all(have.get(s, 0) >= n for s, n in atom.counts.items())
            if atom.exact:
                ok = ok and have == {s: n for s, n in atom.counts.items() if n}
            return _av(path, atom, "pass" if ok else "fail", f"robot state: basket {dict(sorted(have.items()))}", **st)
        return _av(
            path,
            atom,
            "uncertain",
            "needs a camera check (verifier unavailable)",
            conf=0.0,
            decided_by="state",
            method="state",
        )

    return evaluate(cond, fn)


_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = frozenset({"the", "a", "an", "of", "and", "with", "jar", "can", "bag", "box", "pack", "oz", "lb", "ct"})


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall(text.casefold()) if t not in _STOP_WORDS and len(t) > 1}


def match_mention_to_sku(mention: str, names: Mapping[Sku, str], *, min_overlap: float = 0.5) -> Sku | None:
    """Which product a provider box's ``mention`` names. ``names`` = the class strings sent to ``detect`` (sku ->
    '<brand> <name> <size>'). Score = share of the mention's words found in the class string (then the number of shared
    words); best >= min_overlap and unique, else None ('salsa' with mild + hot classes is ambiguous: the verifier
    treats it as unknown, never as a match)."""
    said = _tokens(mention)
    if not said:
        return None
    scored = sorted(
        (((len(said & _tokens(n)) / len(said)), len(said & _tokens(n)), sku) for sku, n in names.items()),
        reverse=True,
    )
    if not scored or scored[0][0] < min_overlap:
        return None
    if len(scored) > 1 and scored[1][:2] == scored[0][:2]:
        return None
    return scored[0][2]


def count_boxes_by_sku(boxes: list[Evidence], names: Mapping[Sku, str]) -> dict[Sku, int]:
    """Per-SKU counts of detected boxes (basket_delta / basket_contains / item_on_shelf); unmatched ones ignored."""
    out: dict[Sku, int] = {}
    for b in boxes:
        sku = b.sku or match_mention_to_sku(b.mention, names)
        if sku is not None:
            out[sku] = out.get(sku, 0) + 1
    return out


_PRICE_RE = re.compile(r"\$?\s*(\d{1,4})(?:[.,](\d{2}))?(?!\d)")
_CENTS_RE = re.compile(r"(\d{1,3})\s*(?:¢|c\b|cents?\b)", re.IGNORECASE)


def parse_price_cents(text: str) -> int | None:
    """Integer cents from a shelf-tag OCR string: '$3.49' -> 349, '3,49' -> 349, '$5' -> 500, '99¢' -> 99. Prefers
    a $-prefixed or two-decimal number; None when nothing parses (-> the shelf_tag_reads atom is 'uncertain')."""
    if not text:
        return None
    m = _CENTS_RE.search(text)
    candidates = list(_PRICE_RE.finditer(text))
    best = next((c for c in candidates if c.group(2) is not None or c.group(0).lstrip().startswith("$")), None)
    if best is not None:
        return int(best.group(1)) * 100 + int(best.group(2) or 0)
    if m is not None:
        return int(m.group(1))
    return None


def verdict_reason(outcome: VerdictOutcome, atoms: list[AtomVerdict]) -> str:
    """One UI line: the first failing (or uncertain) atom's reason; for a pass, the decisive facts."""
    if outcome == "pass":
        facts = [a.reason for a in atoms if a.outcome == "pass" and not a.advisory][:3]
        return "ok: " + "; ".join(facts) if facts else "ok"
    want = "fail" if outcome == "fail" else "uncertain"
    bad = next((a for a in atoms if a.outcome == want and not (a.advisory and want == "uncertain")), None)
    return bad.reason if bad else outcome


# ====================================================================================================================
# Perceptron mapping (documentation as data; perceptron.md renders it). The real verifier's provider slot.
# ====================================================================================================================
class PerceptronCallSpec(Model):
    capability: str
    camera: RobotCameraKind | None = None
    sdk: str = Field(description="Exact SDK call (perceptron>=0.4, AsyncClient per event loop).")
    decides: str = Field(description="How CODE turns the answer into pass / fail / uncertain + evidence.")


PERCEPTRON_CALLS: tuple[PerceptronCallSpec, ...] = (
    PerceptronCallSpec(
        capability="ground_target (box + grasp point of the exact unit)",
        camera="front",
        sdk='detect(image(f), classes=["{brand} {name} {size}"]) then question(image(f), "Point to the best grasp '
        'spot on the {name}", expects="point")',
        decides="best box (slot-position tie-break; sim: instance whose bbox contains the point) -> GroundedTarget."
        "box / grasp_point; no box or confidence < min -> found=False",
    ),
    PerceptronCallSpec(
        capability="gripper_holding / gripper_empty",
        camera="wrist",
        sdk='client.chat.completions.multilook(model="perceptron-mk1.5", context=[image(f)], prompts=["Box the object '
        'held between the gripper jaws", "Is the gripper empty? Answer yes or no."])',
        decides="held box overlapping the gripper ROI (IoU > 0.2) whose mention matches the product -> holding; 'yes' "
        "-> empty; a held box on an expected-empty gripper sets observed.held_sku (-> adopt / put back); fused with "
        "RobotState.holding by combine_verdicts",
    ),
    PerceptronCallSpec(
        capability="label_matches (advisory) / read_label",
        camera="wrist",
        sdk='ocr(image(f), prompt="Read the brand, product name and size on the label of the held item")',
        decides="casefold substring of expect_text (+ label_hints) -> pass; text read but no match -> fail (evidence "
        "= text, -> wrong_item_grasped); nothing read -> uncertain (ignored: advisory)",
    ),
    PerceptronCallSpec(
        capability="basket_delta / basket_contains",
        camera="basket",
        sdk="detect(image(f), classes=[names of the expected SKUs]) on the baseline frame and on the current frame",
        decides="per-class box counts -> perception.BasketCheck.compare(expected, observed); delta = after - before",
    ),
    PerceptronCallSpec(
        capability="item_on_shelf / slot_visible",
        camera="front",
        sdk='detect(image(f), classes=["{brand} {name} {size}"])',
        decides="visible count >= min_count -> pass; fewer -> uncertain, never fail (back rows are hidden: the "
        "scan_shelf step's exact count is THE out-of-stock authority); slot_visible: product or shelf tag in view",
    ),
    PerceptronCallSpec(
        capability="shelf_tag_reads",
        camera="front",
        sdk='ocr(image(f), prompt="Read the price on the shelf tag under the {name}")',
        decides="parsed integer cents == expected cents",
    ),
)
