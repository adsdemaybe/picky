"""Deterministic fault injection for demos and tests ("drop the item on attempt 1").

Faults perturb the WORLD (or a layer's behaviour), never a verdict: the fake VLA, the fake camera and the fake verifier
all consult the same ``FaultInjector`` (``Services.faults``, robot arm) with a ``FaultContext``, and the 2D world /
fault overlay they share stays consistent, so the verifier catches an injected failure for real.

Matching is data-agnostic where it matters: ``occurrence=1, attempt=1`` means "the first grasp step of the run, first
try" whatever the planogram holds. Every firing is recorded (``FaultFiring``) and published as ``demo.fault`` (staff /
presenter view only, never shown to shoppers as a real failure without the label).

Enabled only when PICKY_FAULTS names a scenario or via the dev endpoints (PICKY_DEV=1).
"""

import hashlib
from typing import Literal

from pydantic import Field

from contracts.cameras import RobotCameraKind
from contracts.common import ItemId, JsonScalar, Model, RobotId, Score01, Sku, SlotId, Timestamp, utcnow
from contracts.evidence import FaultId
from contracts.plan import ExecId, PlanStepId, StepKind, VlaTier

FaultEffect = Literal[
    "drop_after_lift",
    "grasp_miss",
    "wrong_item",
    "place_miss",
    "nav_blocked",
    "vla_hang",
    "policy_unavailable",
    "disconnect",
    "camera_occluded",
    "camera_offline",
    "stale_frame",
    "provider_down",
    "verifier_unavailable",
    "low_confidence",
    "mislabel",
    "out_of_stock",
]
"""drop_after_lift: grasp REPORTS success but the unit is not held (T0 fake: never attached, it stays on the shelf;
Isaac later: a detach after lift) -> the wrist check catches it (THE demo moment) · grasp_miss: grasp fails honestly
(grasp_missed) · wrong_item: grasps the neighbouring SKU (2D fake overlay only; label check catches it, put-back via
the overlay) · place_miss: place reports success, the unit is still in the gripper -> place_not_released · nav_blocked:
navigate fails path_blocked · vla_hang: the exec stops emitting statuses and never finishes (stall -> cancel ->
deadline_exceeded) · policy_unavailable: T1 refuses at accept (-> T0 inside the same exec) · disconnect: Isaac drops
mid-step (failed disconnected, free retry on the 2D fake) · camera_occluded: frames of the camera are blank (camera
atoms uncertain -> reverify on the next camera) · camera_offline: capture fails (the camera is left out) ·
stale_frame: capture re-serves the previous frame (rejected by not_before) · provider_down: the vision provider errors
inside the verifier (falls back to ground truth / state, verdict.degraded) · verifier_unavailable: the verifier
service raises / hangs (runner degrade policy: state-only checks) · low_confidence: camera answers below threshold
(uncertain) · mislabel: the held unit's label reads ``params.text`` · out_of_stock: the slot looks empty at scan."""

FaultLayer = Literal["vla", "camera", "verifier", "world"]
FAULT_LAYERS: dict[str, FaultLayer] = {
    "drop_after_lift": "vla",
    "grasp_miss": "vla",
    "wrong_item": "vla",
    "place_miss": "vla",
    "nav_blocked": "vla",
    "vla_hang": "vla",
    "policy_unavailable": "vla",
    "disconnect": "vla",
    "camera_occluded": "camera",
    "camera_offline": "camera",
    "stale_frame": "camera",
    "provider_down": "verifier",
    "verifier_unavailable": "verifier",
    "low_confidence": "verifier",
    "mislabel": "world",
    "out_of_stock": "world",
}
"""Which fake applies each effect (the robot arm's fake VLA / truth, perception's fake camera / verifier)."""


class FaultMatch(Model):
    """When a fault fires: every set field must match (None = any)."""

    robot_id: RobotId | None = None
    step_kind: StepKind | None = None
    item_id: ItemId | None = None
    sku: Sku | None = None
    slot_id: SlotId | None = None
    tier: VlaTier | None = None
    camera: RobotCameraKind | None = None
    attempt: int | None = Field(None, ge=1, description="Attempt number of the step (1 = first try).")
    occurrence: int | None = Field(
        None, ge=1, description="n-th DISTINCT step matching the other fields in this run (data-agnostic)."
    )


class FaultRule(Model):
    fault_id: FaultId
    effect: FaultEffect
    match: FaultMatch = Field(default_factory=FaultMatch)
    times: int = Field(1, ge=0, description="How many matching events it fires on; 0 = every time.")
    probability: Score01 = Field(1.0, description="Seeded per exec/step key, so a given run is reproducible.")
    seed: int = 0
    params: dict[str, JsonScalar] = Field(
        default_factory=dict, description="{'text': 'Medium Salsa', 'confidence': .3}"
    )
    note: str = ""


class FaultContext(Model):
    """What a fake passes to ``FaultInjector.consult``. ``step_key`` = 'plan:step' identifies a step across retries."""

    robot_id: RobotId
    step_kind: StepKind | None = None
    step_id: PlanStepId | None = None
    step_key: str | None = None
    item_id: ItemId | None = None
    sku: Sku | None = None
    slot_id: SlotId | None = None
    tier: VlaTier | None = None
    camera: RobotCameraKind | None = None
    attempt: int = 1
    exec_id: ExecId | None = None
    occurrence: int | None = Field(None, description="Filled by the injector per rule (n-th distinct step_key).")


def fault_matches(rule: FaultRule, ctx: FaultContext) -> bool:
    """Static fields only (everything but times / occurrence / probability)."""
    m = rule.match
    for name in ("robot_id", "step_kind", "item_id", "sku", "slot_id", "tier", "camera", "attempt"):
        want = getattr(m, name)
        if want is not None and getattr(ctx, name) != want:
            return False
    return True


def fault_fires(rule: FaultRule, ctx: FaultContext, fired_so_far: int) -> bool:
    """THE deterministic fault decision (every fake calls it through the injector). ``ctx.occurrence`` must already
    be the n-th distinct step_key that matched this rule's static fields."""
    if rule.times and fired_so_far >= rule.times:
        return False
    if not fault_matches(rule, ctx):
        return False
    if rule.match.occurrence is not None and ctx.occurrence != rule.match.occurrence:
        return False
    if rule.probability >= 1.0:
        return True
    key = ctx.exec_id or ctx.step_key or ""
    h = hashlib.sha256(f"{rule.seed}:{rule.fault_id}:{key}".encode()).digest()
    return int.from_bytes(h[:4], "big") / 0xFFFFFFFF < rule.probability


class FaultFiring(Model):
    fault_id: FaultId
    effect: FaultEffect
    context: FaultContext
    scenario: str | None = None
    note: str = ""
    ts: Timestamp = Field(default_factory=utcnow)


class FaultScenario(Model):
    name: str
    description: str
    rules: list[FaultRule] = Field(default_factory=list)
    two_d_fake_only: bool = Field(False, description="Needs the 2D fake's overlay (no Isaac primitive yet).")


def _rule(fid: str, effect: FaultEffect, note: str = "", times: int = 1, **match: object) -> FaultRule:
    return FaultRule(fault_id=fid, effect=effect, match=FaultMatch(**match), times=times, note=note)  # type: ignore[arg-type]


DEMO_SCENARIOS: dict[str, FaultScenario] = {
    s.name: s
    for s in (
        FaultScenario(name="clean", description="No faults."),
        FaultScenario(
            name="drop_on_first_grasp",
            description="The first grasp 'succeeds' but the jar is not in the gripper; the wrist check catches it, the "
            "robot re-grounds the jar and grabs it again (attempt 2).",
            rules=[
                _rule(
                    "f-drop-1",
                    "drop_after_lift",
                    "drop the item on attempt 1",
                    step_kind="grasp",
                    occurrence=1,
                    attempt=1,
                )
            ],
        ),
        FaultScenario(
            name="slow_vla",
            description="The first grasp hangs (no heartbeats); the runner detects the stall, cancels it and retries.",
            rules=[_rule("f-hang-1", "vla_hang", step_kind="grasp", occurrence=1, attempt=1)],
        ),
        FaultScenario(
            name="verifier_down",
            description="The verifier service is down for the whole run; the runner checks robot state only "
            "(amber 'degraded' checks) and the trip still completes.",
            rules=[_rule("f-vdown", "verifier_unavailable", times=0)],
        ),
        FaultScenario(
            name="flaky_t1",
            description="The pretrained VLA misses twice; the breaker opens and the robot finishes on the scripted "
            "grasp.",
            rules=[_rule("f-t1-miss", "grasp_miss", times=2, step_kind="grasp", tier="t1_pretrained")],
        ),
        FaultScenario(
            name="isaac_drop",
            description="Isaac disconnects mid-navigation; the robot continues on the 2D fake from its last pose.",
            rules=[_rule("f-dc", "disconnect", step_kind="navigate_to", occurrence=2, attempt=1)],
        ),
        FaultScenario(
            name="wrong_salsa",
            description="The robot grabs the jar next to the mild salsa; the label check catches it, it puts the jar "
            "back and takes the right one.",
            rules=[_rule("f-wrong", "wrong_item", sku="SALSA-MILD", step_kind="grasp", attempt=1)],
            two_d_fake_only=True,
        ),
        FaultScenario(
            name="foggy_basket",
            description="The basket camera is blocked once: the place check is uncertain, re-verified, then accepted "
            "provisionally until the pre-checkout basket audit.",
            rules=[_rule("f-fog", "camera_occluded", times=2, camera="basket", step_kind="place", occurrence=1)],
        ),
    )
}
