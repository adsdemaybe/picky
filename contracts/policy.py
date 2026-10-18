"""Orchestration policy: who retries what, budgets, and how verified results update cart / stock / goal.

Recovery is DATA (``RetryPolicy``: ordered rules, each with an escalation ladder) decided by CODE (``decide_recovery``,
pure and deterministic). The LLM only appears for ``replan`` (it proposes a new tail; ``validate_plan`` + the
compiler accept or reject it). Layering:

    layer        retries                                             never
    VLA service  nothing (one exec = one attempt; the only fallback   whole-step retries, events, stock / cart
                 is T1 -> T0 when T1 is unavailable AT ACCEPT)
    verifier     one re-capture on a stale frame                      retries of the step
    runner       retry / reverify / reground / realign / renavigate /  changing the item list by itself
                 rescan / redo_unit / return_item / downgrade_tier /
                 accept_provisional, within budgets; asks the user
    planner LLM  replan only (completed prefix frozen)                conditions, poses, budgets, money
    shopper      answers ask_user (retry / skip / order online)       -
    fleet (v0.2) reassign on fatal codes (battery_low, unknown_robot,  -
                 gripper_fault): run_goal raises RobotError

Tools mode (the v0.2 LLM brain keeps its tool loop): inside ONE brain tool call the runner applies only
``INNER_RECOVERIES``; every other action goes back to the LLM as a v0.2 ``BrainToolError`` and ``PlannerService.replan``
is never called (two planners never fight).

Effects commit only after the postcondition PASSED (or was accepted provisionally), each at most once per
``effect_key`` (``EffectRecord`` ledger), because v0.2 ``catalog.record_pick`` and ``trips.add_line`` are not
idempotent. A provisional line shows as provisional in the cart and the pre-checkout basket audit must pass before
the handoff / ``start_checkout``.
"""

from typing import Any, Literal

from pydantic import Field, computed_field

from contracts.common import Cents, InstanceId, ItemId, Model, RobotId, Score01, Timestamp, utcnow
from contracts.fleet import GoalItemStatus
from contracts.plan import (
    TIER_PREFERENCE,
    ExecId,
    PlanId,
    PlanStatus,
    PlanStep,
    PlanStepId,
    StepKind,
    StepPlan,
    VerifyId,
    VlaTier,
    VlaTierPref,
)
from contracts.shopping import CartLine
from contracts.sim import RobotState
from contracts.verify import Verdict
from contracts.vla import ERROR_TRAITS, ErrorCategory, VlaError, VlaStatus

RecoveryAction = Literal[
    "retry",
    "reverify",
    "reground",
    "realign",
    "renavigate",
    "rescan",
    "redo_unit",
    "return_item",
    "return_held",
    "adopt_held",
    "downgrade_tier",
    "replan",
    "substitute",
    "accept_provisional",
    "ask_user",
    "skip_item",
    "mark_unavailable",
    "fail_goal",
    "escalate",
]
ACTION_INSERTS: dict[str, tuple[StepKind, ...]] = {
    "reground": ("ground_target",),
    "realign": ("align", "ground_target"),
    "renavigate": ("navigate_to", "align", "ground_target"),
    "rescan": ("scan_shelf",),
    "redo_unit": ("ground_target", "grasp"),
    "return_item": ("place", "ground_target"),
    "return_held": ("place",),
}
"""Steps the runner inserts BEFORE re-running the failed step (ids ``recovery_step_id``), built from the failed step's
item / slot / binding with ``default_conditions``. ground_target is inserted only when the failed step is a grasp;
rescan inserts nothing when the failed step IS the scan; return_item's and return_held's place has destination='shelf'
and goes back to the HELD unit's own slot (``ItemInstance.slot_id``). A held unit is NEVER placed into the basket by a
recovery: only a plan place step for that item's unit may, so stock / cart counts stay exact."""
ACTION_RERUNS_STEP: frozenset[str] = frozenset(
    {
        "retry",
        "reground",
        "realign",
        "renavigate",
        "rescan",
        "redo_unit",
        "return_item",
        "return_held",
        "downgrade_tier",
    }
)
INNER_RECOVERIES: frozenset[str] = frozenset(
    {"retry", "reverify", "reground", "realign", "downgrade_tier", "accept_provisional", "adopt_held"}
)
"""Tools mode: what the runner may do inside one brain tool call. accept_provisional maps to v0.2
``PlaceResult.basket_ok=None`` ('not verified'), which the brain already understands."""
ITEM_TERMINAL_ACTIONS: frozenset[str] = frozenset({"skip_item", "mark_unavailable", "fail_goal", "escalate"})
"""skip_item -> GoalItem 'failed' ('skipped' if optional) · mark_unavailable -> v0.2 'unavailable' (online line) ·
fail_goal -> goal failed · escalate -> RobotError raised (the fleet reassigns, MAX_GOAL_ATTEMPTS)."""

ACTION_PHRASES: dict[str, str] = {
    "retry": "retrying",
    "reverify": "checking again with another view",
    "reground": "re-finding the item and retrying",
    "realign": "re-aligning to the shelf and retrying",
    "renavigate": "driving back to the shelf and retrying",
    "rescan": "re-scanning the shelf",
    "redo_unit": "picking another unit",
    "return_item": "putting it back and taking the right one",
    "return_held": "putting the item it is holding back on its shelf first",
    "adopt_held": "it is already holding the right item: counting the grasp as done",
    "downgrade_tier": "switching to the scripted grasp",
    "replan": "re-planning the rest of the trip",
    "substitute": "offering a substitute",
    "accept_provisional": "accepting it for now (basket audit before checkout)",
    "ask_user": "asking the shopper",
    "skip_item": "skipping this item",
    "mark_unavailable": "ordering it online instead",
    "fail_goal": "stopping the goal",
    "escalate": "handing the goal to another robot",
}

FailureKind = Literal["vla_error", "precondition", "postcondition", "verifier_uncertain", "stalled"]

DIAGNOSIS_CATEGORIES: dict[str, ErrorCategory] = {
    "place_not_released": "manipulation",
    "already_holding": "manipulation",
    "atom:gripper_holding": "manipulation",
    "atom:gripper_empty": "manipulation",
    "atom:basket_delta": "manipulation",
    "atom:label_matches": "manipulation",
    "atom:item_on_shelf": "stock",
    "atom:robot_at": "precondition",
    "atom:slot_reachable": "precondition",
    "atom:battery_above": "safety",
}


class StepFailure(Model):
    plan_id: PlanId
    step_id: PlanStepId
    step_kind: StepKind
    attempt: int = Field(ge=1)
    kind: FailureKind
    code: str = Field(
        description="VlaErrorCode, a diagnosis ('wrong_item_grasped', 'place_not_released') or 'atom:<op>'."
    )
    message: str = ""
    tier: VlaTier | None = None
    item_id: ItemId | None = None
    vla_error: VlaError | None = None
    verdict: Verdict | None = None
    vla_claimed_success: bool = Field(False, description="The VLA's terminal status was 'succeeded'.")
    caught_by_verifier: bool = Field(
        False, description="The VLA claimed success and a postcondition FAILED (demo moment)."
    )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def category(self) -> ErrorCategory:
        if self.code in ERROR_TRAITS:
            return ERROR_TRAITS[self.code].category
        if self.code in DIAGNOSIS_CATEGORIES:
            return DIAGNOSIS_CATEGORIES[self.code]
        if self.kind == "precondition":
            return "precondition"
        return "manipulation" if self.step_kind in ("grasp", "place") else "motion"


ATOM_DIAGNOSIS_PRIORITY: tuple[str, ...] = (
    "label_matches",
    "gripper_holding",
    "gripper_empty",
    "basket_delta",
    "item_on_shelf",
    "target_grounded",
    "slot_visible",
    "slot_reachable",
    "robot_at",
    "basket_contains",
    "shelf_tag_reads",
    "battery_above",
    "approval_granted",
)
"""When several atoms fail, the most specific diagnosis names the failure (and so picks the recovery rule)."""


def failure_from_verdict(
    verdict: Verdict,
    *,
    plan_id: PlanId,
    step: PlanStep,
    attempt: int,
    phase: Literal["pre", "post", "verify", "reverify"],
    tier: VlaTier | None = None,
    vla_claimed_success: bool = False,
) -> StepFailure | None:
    """THE verdict -> StepFailure classifier (None when the verdict passed). Fail beats uncertain; among failing atoms
    ATOM_DIAGNOSIS_PRIORITY decides; step-aware diagnoses: a place whose gripper is not empty -> 'place_not_released'
    (retry the place, never re-pick while holding); a grasp holding another SKU -> 'wrong_item_grasped' (put back); a
    grasp whose PRE-check finds the gripper already holding the target SKU -> 'already_holding' (an earlier attempt
    worked after all, or a cancelled goal left it there: adopt it, no second unit)."""
    if verdict.outcome == "pass":
        return None
    rank = {op: i for i, op in enumerate(ATOM_DIAGNOSIS_PRIORITY)}
    want = "fail" if verdict.outcome == "fail" else "uncertain"
    bad = sorted(
        (a for a in verdict.atoms if a.outcome == want and not (a.advisory and want == "uncertain")),
        key=lambda a: rank.get(a.op, len(rank)),
    )
    top = bad[0] if bad else None
    code = f"atom:{top.op}" if top else "atom:unknown"
    if want == "fail":
        held_pre = next((a.observed.get("held_sku") for a in bad if a.op == "gripper_empty"), None)
        if phase == "pre" and step.kind == "grasp" and held_pre:
            if held_pre == getattr(getattr(step, "target", None), "sku", None):
                code = "already_holding"
        elif step.kind == "place" and any(a.op == "gripper_empty" for a in bad):
            code = "place_not_released"
        elif step.kind == "grasp" and top is not None and top.op in ("gripper_holding", "label_matches"):
            expected = getattr(getattr(step, "target", None), "sku", None)
            held = top.observed.get("held_sku")
            if held and expected and held != expected:
                code = "wrong_item_grasped"
    kind: FailureKind
    if want == "uncertain":
        kind = "verifier_uncertain"
    else:
        kind = "precondition" if phase == "pre" else "postcondition"
    return StepFailure(
        plan_id=plan_id,
        step_id=step.step_id,
        step_kind=step.kind,
        attempt=attempt,
        kind=kind,
        code=code,
        message=verdict.reason,
        tier=tier,
        item_id=step.item_id,
        verdict=verdict,
        vla_claimed_success=vla_claimed_success,
        caught_by_verifier=vla_claimed_success and phase in ("post", "reverify") and want == "fail",
    )


def failure_from_vla(status: VlaStatus, *, plan_id: PlanId, step: PlanStep, attempt: int) -> StepFailure | None:
    """Terminal VLA status -> StepFailure (None for 'succeeded': the postcondition still decides). A runner-detected
    stall (details.stalled) is kind 'stalled'."""
    if status.phase == "succeeded":
        return None
    err = status.error or VlaError(code="internal", message="terminal status without error")
    return StepFailure(
        plan_id=plan_id,
        step_id=step.step_id,
        step_kind=step.kind,
        attempt=attempt,
        kind="stalled" if err.details.get("stalled") else "vla_error",
        code=err.code,
        message=err.message,
        tier=status.tier,
        item_id=step.item_id,
        vla_error=err,
    )


# ====================================================================================================================
# Policy as data
# ====================================================================================================================
class FailureMatch(Model):
    """All non-empty fields must match. ``codes`` holds VLA codes, diagnoses and/or 'atom:<op>'."""

    step_kinds: list[StepKind] = Field(default_factory=list)
    kinds: list[FailureKind] = Field(default_factory=list)
    codes: list[str] = Field(default_factory=list)
    categories: list[ErrorCategory] = Field(default_factory=list)
    tiers: list[VlaTier] = Field(default_factory=list)


class RecoveryRule(Model):
    rule_id: str
    match: FailureMatch
    ladder: list[RecoveryAction] = Field(
        min_length=1, description="Action for the n-th failure of the step UNDER THIS RULE = ladder[min(n,len)-1]."
    )
    backoff_s: float = Field(0.0, ge=0)
    note: str = ""


class PlanBudget(Model):
    """THE budgets (one place: the policy; StepPlan only names the policy)."""

    max_step_attempts: int = Field(3, ge=1, description="Charged attempts per step (infra hiccups are free).")
    max_unit_attempts: int = Field(4, ge=1, description="Grasp+place cycles for ONE unit across reground / redo.")
    max_replans: int = Field(3, ge=0)
    max_user_questions: int = Field(4, ge=0, description="Recovery questions (normal ask_user steps excluded).")
    max_wall_s: float = Field(900.0, gt=0)


class BreakerPolicy(Model):
    strikes: int = Field(2, ge=1, description="T1/T2 strikes before the tier is opened for the rest of the goal.")
    cooldown_s: float = Field(600.0, description="Half-open after this (one probe attempt), within the goal.")
    scope: Literal["goal", "robot"] = "goal"


class VerifierDegradePolicy(Model):
    """What the runner does when the verifier raises VerifierUnavailableError / times out / is not wired."""

    on_unavailable: Literal["state_only", "uncertain"] = Field(
        "state_only",
        description="state_only = evaluate the condition from robot state by code (verify.evaluate_on_state; camera-"
        "only atoms uncertain, advisory ones ignored), verdict degraded=True decided_by='state' · uncertain = every "
        "check is 'uncertain' (the policy's uncertain rules decide: reverify, then ask / accept provisionally).",
    )
    verify_timeout_s: float = Field(8.0, gt=0, description="asyncio.wait_for around every verifier call.")


PRECONDITION_REPAIRS: dict[str, RecoveryAction] = {
    "atom:robot_at": "renavigate",
    "atom:slot_reachable": "renavigate",
    "already_holding": "adopt_held",
    "atom:gripper_empty": "return_held",
    "atom:gripper_holding": "redo_unit",
    "atom:battery_above": "escalate",
}
"""The precondition branch (checked before the ladder rules): not at the shelf / out of reach -> drive back (+ align
+ re-ground for grasps); already holding the target SKU before a grasp -> adopt it (the grasp succeeds without
motion; its place step then commits it exactly once); holding another SKU -> put it back on its own shelf first
(place destination='shelf', needs ``VlaCapabilities.place_back``; without it rule 'cannot_return' asks staff, then
escalates); a place whose unit vanished -> pick another; battery below the motion floor -> escalate (RobotError
battery_low: the fleet docks / reassigns). A repair that fails twice for the same step falls through to the rules."""


class RetryPolicy(Model):
    policy_id: str = "default"
    rules: list[RecoveryRule] = Field(default_factory=list, description="First match wins.")
    precondition_repairs: dict[str, RecoveryAction] = Field(default_factory=lambda: dict(PRECONDITION_REPAIRS))
    fallback: list[RecoveryAction] = Field(default_factory=lambda: ["ask_user", "skip_item"])
    budget: PlanBudget = Field(default_factory=PlanBudget)
    breaker: BreakerPolicy = Field(default_factory=BreakerPolicy)
    degrade: VerifierDegradePolicy = Field(default_factory=VerifierDegradePolicy)


class RecoveryContext(Model):
    failure: StepFailure
    rule_failures: dict[str, int] = Field(
        default_factory=dict, description="PRIOR failures of this step per rule_id (the ladder rung is per rule)."
    )
    failures_of_step: int = Field(1, ge=1, description="Charged failures of this step so far, incl. this one.")
    unit_attempts: int = Field(1, ge=1)
    replans_used: int = 0
    questions_used: int = 0
    wall_s: float = 0.0
    tier_strikes: int = Field(0, description="Strikes of failure.tier before this failure.")
    optional_step: bool = False
    available_tiers: list[VlaTier] = Field(default_factory=lambda: ["t0_scripted"])
    mode: Literal["plan", "tools"] = "plan"


class RecoveryDecision(Model):
    action: RecoveryAction
    rule_id: str | None = None
    rung: int = Field(1, ge=1, description="Ladder rung used (1-based).")
    reason: str = Field(description="Human line: 'Verifier caught it: gripper is empty; re-finding ... (attempt 2/3)'.")
    insert: list[StepKind] = Field(default_factory=list)
    rerun_failed_step: bool = False
    next_tier: VlaTierPref | None = None
    open_breaker: VlaTier | None = Field(None, description="Tier the runner opens now (vla.tier_changed).")
    backoff_s: float = 0.0
    budget_exhausted: bool = False
    next_attempt: int | None = Field(None, description="Attempt number of the rerun (UI 'attempt 2/3').")
    max_attempts: int | None = None
    inner: bool = Field(True, description="In INNER_RECOVERIES (tools mode: handled inside the tool call).")


def _rule_matches(rule: RecoveryRule, f: StepFailure) -> bool:
    m = rule.match
    if m.step_kinds and f.step_kind not in m.step_kinds:
        return False
    if m.kinds and f.kind not in m.kinds:
        return False
    if m.codes and f.code not in m.codes:
        return False
    if m.categories and f.category not in m.categories:
        return False
    if m.tiers and (f.tier is None or f.tier not in m.tiers):
        return False
    return True


def _why(f: StepFailure) -> str:
    if f.caught_by_verifier:
        return f"Verifier caught it: {f.message or f.code}"
    if f.kind == "verifier_uncertain":
        return f"Not sure: {f.message or f.code}"
    if f.kind == "stalled":
        return "The VLA stopped reporting progress"
    if f.kind == "precondition":
        return f"Not ready for {f.step_kind}: {f.message or f.code}"
    return f"{f.step_kind} failed: {f.message or f.code}"


def decide_recovery(policy: RetryPolicy, ctx: RecoveryContext) -> RecoveryDecision:
    """THE recovery decision (deterministic, unit-testable, no LLM). Ladder rungs are counted PER MATCHED RULE, so
    mixed failures never skip rungs. Budgets push past rerun actions; replans / questions have their own budgets;
    skip_item on a step without an item becomes fail_goal."""
    f, b = ctx.failure, policy.budget
    traits = ERROR_TRAITS.get(f.code)
    if traits is not None and traits.fatal:
        return RecoveryDecision(action="escalate", reason=f"{_why(f)}; {ACTION_PHRASES['escalate']}", inner=False)
    if ctx.wall_s > b.max_wall_s:
        act: RecoveryAction = "skip_item" if f.item_id else "fail_goal"
        return RecoveryDecision(
            action=act, budget_exhausted=True, inner=False, reason=f"goal time budget {b.max_wall_s:.0f}s spent"
        )

    rule_id: str | None
    ladder: list[RecoveryAction]
    backoff = 0.0
    repair = policy.precondition_repairs.get(f.code) if f.kind == "precondition" else None
    if repair is not None and ctx.rule_failures.get(f"pre:{f.code}", 0) < 2:
        rule_id, ladder = f"pre:{f.code}", [repair]
    else:
        rule = next((r for r in policy.rules if _rule_matches(r, f)), None)
        rule_id = rule.rule_id if rule else None
        ladder = list(rule.ladder) if rule else list(policy.fallback)
        backoff = rule.backoff_s if rule else 0.0
    n = ctx.rule_failures.get(rule_id or "_fallback", 0) + 1
    rung = min(n, len(ladder))
    action: RecoveryAction = ladder[rung - 1]

    open_breaker: VlaTier | None = None
    strike = (traits is not None and traits.t1_strike) or f.kind in ("postcondition", "stalled")
    if f.tier in ("t1_pretrained", "t2_finetuned") and strike and ctx.tier_strikes + 1 >= policy.breaker.strikes:
        open_breaker = f.tier

    counts = traits.counts_attempt if traits is not None else True
    exhausted = counts and (ctx.failures_of_step >= b.max_step_attempts or ctx.unit_attempts >= b.max_unit_attempts)
    if exhausted and action in ACTION_RERUNS_STEP:
        action = next((a for a in ladder[rung:] if a not in ACTION_RERUNS_STEP), policy.fallback[0])
    if action == "downgrade_tier" and f.tier in (None, "t0_scripted"):
        action = "retry" if not exhausted else policy.fallback[0]
    if action == "replan" and ctx.replans_used >= b.max_replans:
        action = "ask_user"
    if action in ("ask_user", "substitute") and ctx.questions_used >= b.max_user_questions:
        action = "skip_item"
    if action == "skip_item" and not f.item_id and not ctx.optional_step:
        action = "fail_goal"

    next_tier: VlaTierPref | None = None
    if action == "downgrade_tier" or open_breaker is not None:
        below = list(TIER_PREFERENCE[TIER_PREFERENCE.index(f.tier) + 1 :]) if f.tier else []
        next_tier = next((t for t in below if t in ctx.available_tiers), "t0_scripted")
    rerun = action in ACTION_RERUNS_STEP
    next_attempt = ctx.failures_of_step + 1 if rerun and counts else None
    suffix = f" (attempt {next_attempt}/{b.max_step_attempts})" if next_attempt else (" (free retry)" if rerun else "")
    phrase = ACTION_PHRASES[action] + suffix
    breaker_txt = f"; {open_breaker} switched off for this goal" if open_breaker else ""
    return RecoveryDecision(
        action=action,
        rule_id=rule_id,
        rung=rung,
        reason=f"{_why(f)}; {phrase}{breaker_txt}",
        insert=list(ACTION_INSERTS.get(action, ())),
        rerun_failed_step=rerun,
        next_tier=next_tier,
        open_breaker=open_breaker,
        backoff_s=backoff if rerun else 0.0,
        budget_exhausted=exhausted,
        next_attempt=next_attempt,
        max_attempts=b.max_step_attempts,
        inner=action in INNER_RECOVERIES,
    )


def _r(rule_id: str, ladder: list[str], note: str, backoff_s: float = 0.0, **match: Any) -> RecoveryRule:
    return RecoveryRule(rule_id=rule_id, match=FailureMatch(**match), ladder=ladder, backoff_s=backoff_s, note=note)  # type: ignore[arg-type]


DEFAULT_RETRY_POLICY = RetryPolicy(
    policy_id="default",
    rules=[
        _r(
            "infra_disconnect",
            ["retry"],
            "Isaac lost / busy: free retry (now on the 2D fake, T0).",
            codes=["disconnected", "busy", "not_ready"],
            backoff_s=0.2,
        ),
        _r(
            "policy_down",
            ["downgrade_tier"],
            "T1/T2 unhealthy or stuck: same step on the next tier.",
            categories=["policy"],
        ),
        _r(
            "deadline",
            ["downgrade_tier", "retry", "skip_item"],
            "Over deadline or stalled (runner cancels, code deadline_exceeded).",
            codes=["deadline_exceeded"],
        ),
        _r(
            "grasp_unsure",
            ["reverify", "ask_user"],
            "Can't tell if it holds the item: other camera, then ask.",
            step_kinds=["grasp"],
            kinds=["verifier_uncertain"],
        ),
        _r(
            "place_unsure",
            ["reverify", "accept_provisional"],
            "Basket view unclear: other view, then provisional (the pre-checkout audit confirms).",
            step_kinds=["place"],
            kinds=["verifier_uncertain"],
        ),
        _r("unsure", ["reverify", "ask_user"], "Any other uncertain check.", kinds=["verifier_uncertain"]),
        _r(
            "wrong_item",
            ["return_item", "return_item", "ask_user"],
            "Put it back, re-ground the right product.",
            codes=["wrong_item_grasped", "atom:label_matches"],
        ),
        _r(
            "grasp_failed",
            ["reground", "realign", "downgrade_tier", "ask_user"],
            "THE demo ladder: re-ground, re-align, scripted grasp, then ask.",
            step_kinds=["grasp"],
            codes=["grasp_missed", "grasp_slipped", "atom:gripper_holding", "out_of_reach"],
        ),
        _r(
            "place_not_released",
            ["retry", "retry", "ask_user"],
            "Still holding after the place: place again.",
            codes=["place_not_released"],
        ),
        _r(
            "place_failed",
            ["redo_unit", "redo_unit", "ask_user"],
            "Unit not in the basket (dropped): pick another.",
            step_kinds=["place"],
            codes=["place_failed", "atom:basket_delta"],
        ),
        _r(
            "not_on_shelf",
            ["rescan", "substitute", "mark_unavailable"],
            "Empty slot: rescan, offer a substitute, else order online (v0.2 'unavailable').",
            codes=["no_instance", "unknown_slot", "atom:item_on_shelf"],
        ),
        _r(
            "nav",
            ["retry", "replan", "skip_item"],
            "Blocked aisle / missed the pose: wait + retry, then re-route.",
            step_kinds=["navigate_to"],
            backoff_s=1.0,
        ),
        _r(
            "not_positioned",
            ["renavigate", "replan", "skip_item"],
            "Not at the shelf / out of reach.",
            codes=["atom:robot_at", "atom:slot_reachable", "not_at_pose", "atom:slot_visible"],
        ),
        _r(
            "grounding",
            ["realign", "renavigate", "ask_user"],
            "Couldn't find the product in view.",
            codes=["atom:target_grounded", "target_not_grounded", "target_stale"],
        ),
        _r("audit", ["reverify", "ask_user"], "Basket audit mismatch before checkout.", step_kinds=["verify"]),
        _r(
            "cannot_return",
            ["ask_user", "escalate"],
            "No place-back primitive (T0 over v0.2 RobotService): ask staff to take the unit, then hand the goal on.",
            step_kinds=["place"],
            codes=["unsupported_step"],
        ),
    ],
)
"""The demo policy. 'drop_on_first_grasp': grasp -> VLA 'succeeded' -> post gripper_holding FAILS on the wrist camera
(caught_by_verifier) -> rule grasp_failed rung 1 = reground -> insert ground_target -> grasp attempt 2 -> holding
verified -> place -> gripper empty + basket +1 verified -> commit record_pick / goal item / cart line (once)."""


# ====================================================================================================================
# Effects + run records
# ====================================================================================================================
EffectKind = Literal[
    "stop_arrived",  # trips.set_stop_status(arrived) + v0.2 robot.arrived
    "apply_scan",  # catalog.apply_scan + v0.2 scan.result (the scan IS the observation)
    "record_pick",  # catalog.record_pick(lot_id, robot_id) + v0.2 item.picked
    "goal_item_progress",  # fleet.update_goal(items=[picked_qty, instance_ids, status])
    "add_cart_line",  # trips.add_line ONCE per GoalItem when picked_qty == qty (v0.2 rule)
    "online_line",  # mark_unavailable: trips.add_line(source='online')
    "return_to_shelf",  # a wrong unit put back: nothing but a log (it never left the store count)
    "unload_basket",  # robot.unload_basket at checkout (the VLA handoff did it)
    "unload_to_counter",  # 0.2.4: one unit taken out of the basket and verified on the checkout counter
]
COMMIT_ON_VERIFIED: dict[str, tuple[EffectKind, ...]] = {
    "navigate_to": ("stop_arrived",),
    "scan_shelf": ("apply_scan",),
    "grasp": (),
    "place": ("record_pick", "goal_item_progress", "add_cart_line"),
    "handoff": ("unload_basket",),
}
"""Per step kind, committed only after the postcondition PASSED (or was accepted provisionally). Holding is not buying:
nothing commits on grasp. place(destination='shelf') commits only 'return_to_shelf'. add_cart_line fires once per
item (when its last unit is verified in the basket). Plan mode only: in tools mode the v0.2 brain executor applies its
own v0.2 side effects after a VERIFIED tool outcome. 0.2.4: place(destination='counter') commits only
'unload_to_counter' (the unit was bought already; nothing touches stock or the cart)."""


class EffectRecord(Model):
    effect_key: str
    kind: EffectKind
    plan_id: PlanId
    step_id: PlanStepId | None = None
    item_id: ItemId | None = None
    exec_id: ExecId | None = None
    verify_id: VerifyId | None = None
    provisional: bool = Field(False, description="Committed on an accepted 'uncertain' verdict; audited later.")
    confidence: Score01 = 1.0
    payload: dict[str, Any] = Field(default_factory=dict, description="{'sku','instance_id','line_id',...}")
    committed_at: Timestamp = Field(default_factory=utcnow)


class BreakerState(Model):
    tier: VlaTier
    strikes: int = 0
    open: bool = False
    opened_at: Timestamp | None = None
    reason: str = ""


class StepAttempt(Model):
    """One attempt of one step: the audit row behind every UI line ('Dropped it. Retrying (2/3)')."""

    plan_id: PlanId
    step_id: PlanStepId
    attempt: int = Field(ge=1)
    exec_id: ExecId | None = None
    tier: VlaTier | None = None
    pre: Verdict | None = None
    vla_terminal: VlaStatus | None = None
    vla_claimed_success: bool = False
    post: Verdict | None = None
    failure: StepFailure | None = None
    decision: RecoveryDecision | None = None
    effects: list[EffectRecord] = Field(default_factory=list)
    fault_ids: list[str] = Field(default_factory=list, description="Injected faults that fired during this attempt.")
    degraded: bool = Field(False, description="A check was decided by the degrade policy (verifier unavailable).")
    started_at: Timestamp = Field(default_factory=utcnow)
    ended_at: Timestamp | None = None


class LoopMetrics(Model):
    """plan.completed stats: 'Done in 2:41 · 82 steps · 1 verifier catch · 1 retry · 0 Perceptron calls'."""

    steps_total: int = 0
    steps_succeeded: int = 0
    steps_provisional: int = 0
    steps_skipped: int = 0
    steps_failed: int = 0
    attempts: int = 0
    retries: int = 0
    replans: int = 0
    asks: int = 0
    verifier_catches: int = 0
    tier_fallbacks: int = 0
    degraded_checks: int = 0
    provider_calls: dict[str, int] = Field(default_factory=dict)
    ground_truth_agreement: Score01 | None = None
    mean_verify_ms: float | None = None
    duration_s: float = 0.0


class PlanRunState(Model):
    """Live run state of a plan (GET /api/plans/{plan_id}/run)."""

    plan_id: PlanId
    root_plan_id: PlanId
    robot_id: RobotId
    status: PlanStatus = PlanStatus.READY
    current_step_id: PlanStepId | None = None
    attempts: list[StepAttempt] = Field(default_factory=list)
    replans: int = 0
    questions: int = 0
    breakers: dict[str, BreakerState] = Field(default_factory=dict, description="Keyed by tier.")
    effects: list[EffectRecord] = Field(default_factory=list, description="The effect ledger (committed once).")
    local_cart: list[CartLine] = Field(
        default_factory=list, description="Only when no TripController is wired (NL-only demo plans)."
    )
    provisional_open: bool = Field(False, description="A provisional effect awaits the basket audit.")
    holding_instance_id: InstanceId | None = Field(
        None,
        description="Set when the plan ENDS (cancelled mid-grasp, escalated) with a unit still in the gripper. Never "
        "committed (holding is not buying); the robot's next grasp pre-check adopts it (same SKU) or puts it back.",
    )
    started_at: Timestamp | None = None
    updated_at: Timestamp = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def local_cart_cents(self) -> int:
        return sum(line.qty * line.unit_price_cents for line in self.local_cart)


class PlanOutcome(Model):
    plan: StepPlan
    run: PlanRunState
    status: Literal["completed", "failed", "cancelled"]
    summary: str = ""
    item_status: dict[ItemId, GoalItemStatus] = Field(default_factory=dict)
    metrics: LoopMetrics = Field(default_factory=LoopMetrics)
    total_cents: Cents = Field(0, description="Verified in-basket value (local cart or trip lines added).")


class ReplanRequest(Model):
    plan: StepPlan
    failure: StepFailure | None = None
    decision: RecoveryDecision | None = None
    robot_state: RobotState | None = None
    user_text: str | None = Field(None, description="'also grab limes' / 'skip the cheese' (handle_message).")
    substitutions: dict[ItemId, str] = Field(default_factory=dict, description="item_id -> substitute SKU (approved).")
    skip_item_ids: list[ItemId] = Field(default_factory=list)
    reason: str = ""
