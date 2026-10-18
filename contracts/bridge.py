"""v0.2 bridge: how the robot brain's LLM tools become typed plan steps (tools mode), how verified outcomes map back
to the v0.2 tool results the LLM already understands, and how a plan run shows up in the v0.2 brain view (the
``brain.step`` mirror, plan mode). Full table in docs/vla-loop/tools.md.

Tools mode = PICKY_EXEC_MODE=tools: the v0.2 LLM tool loop is unchanged; its MOTION tools expand into verified steps
(``BRAIN_TOOL_STEPS``) appended to the goal's ONE live StepPlan; the runner applies only INNER_RECOVERIES inside the
call; anything else comes back as ``BrainToolError`` so the LLM decides (re-plan / ask / skip) exactly as today. The
executor keeps its v0.2 side effects (record_pick, item.picked, add_line, basket.verified, update_goal), applied ONLY
after a verified outcome. ``pick_item``'s mandate check (trips.check_line before the first unit) stays in the executor
BEFORE the micro-plan runs: no motion happens for a denied line.
"""

from typing import Literal

from pydantic import Field

from contracts.common import ItemId, Model, Sku
from contracts.fleet import BrainStep, GoalStatus, GoalUpdate
from contracts.plan import PlanStep, StepKind, StepPlan
from contracts.policy import PlanOutcome, RecoveryDecision, StepFailure
from contracts.routing import StopStatus
from contracts.sim import ItemInstance
from contracts.tools import BRAIN_RETRYABLE_CODES, BrainToolError, PickResult, PlaceResult
from contracts.verify import Verdict
from contracts.vla import ERROR_TRAITS, VLA_TO_BRAIN_CODE

BRAIN_TOOL_STEPS: dict[str, tuple[StepKind, ...]] = {
    "navigate_to": ("navigate_to",),
    "scan_shelf": ("align", "scan_shelf"),
    "pick_item": ("align", "ground_target", "grasp"),
    "place_in_basket": ("place",),
    "finish_goal": ("verify", "navigate_to", "handoff"),
}
"""Motion tools -> steps (finish_goal expands only when end_at='checkout'; otherwise it has no motion)."""
UNCHANGED_BRAIN_TOOLS: tuple[str, ...] = (
    "locate_items",
    "plan_route",
    "look",
    "ask_user",
    "add_item",
    "update_progress",
)
"""Keep their v0.2 executors (look = CameraService.observe; ask_user = trips.ask / approvals.request)."""

STEP_TO_BRAIN_TOOL: dict[str, str] = {
    "navigate_to": "navigate_to",
    "align": "navigate_to",
    "ground_target": "look",
    "grasp": "pick_item",
    "place": "place_in_basket",
    "verify": "look",
    "scan_shelf": "scan_shelf",
    "ask_user": "ask_user",
    "handoff": "finish_goal",
}
"""For the brain.step mirror (plan mode): the v0.2 tool a step corresponds to, so the ThinkingLog stays readable."""

MIRROR_MODEL = "loop"
"""``BrainStep.model`` of mirrored steps (the brain view shows 'loop' instead of an LLM / 'scripted')."""


def mirror_brain_step(plan: StepPlan, step: PlanStep, text: str, *, step_no: int, ok: bool = True) -> BrainStep:
    """THE brain.step mirror of a plan step transition (plan mode; the runner publishes it with
    ``make_event("brain.step", **events.plan_envelope(plan), step=..., source="agent")``): kind 'status', text
    '<step title>: <what happened>', ``tool`` = the v0.2 tool the step stands for. ``step_no`` is the runner's
    per-plan counter (1-based); ids are unique per plan / step / attempt / counter. Goal-less dev plans
    ('adhoc-...') carry no goal_id."""
    return BrainStep(
        step_id=f"{plan.plan_id}:{step.step_id}:{step.attempts}:{step_no}",
        robot_id=plan.robot_id,
        goal_id=None if plan.goal_id.startswith("adhoc-") else plan.goal_id,
        trip_id=plan.trip_id,
        store_id=plan.store_id,
        step_no=step_no,
        kind="status",
        text=f"{step.title}: {text}",
        tool=STEP_TO_BRAIN_TOOL.get(step.kind),
        ok=ok,
        model=MIRROR_MODEL,
    )


class ToolStepOutcome(Model):
    """What ``LoopRunner.run_tool_step`` returns to the v0.2 brain executor (tools mode)."""

    tool: str
    ok: bool
    steps: list[PlanStep] = Field(default_factory=list, description="The steps appended to the goal's live plan.")
    failure: StepFailure | None = None
    decision: RecoveryDecision | None = Field(None, description="The OUTER decision handed back to the LLM.")
    verdicts: list[Verdict] = Field(default_factory=list, description="Final post verdicts of the steps.")
    holding: ItemInstance | None = Field(None, description="Verified unit in the gripper after a pick.")
    verified: bool | None = Field(None, description="True = postcondition passed; None = accepted provisionally.")
    error: BrainToolError | None = None


def brain_tool_error(failure: StepFailure, decision: RecoveryDecision | None = None) -> BrainToolError:
    """Map a final step failure to the v0.2 ``BrainToolError`` vocabulary (error_code in SimErrorCode terms)."""
    code = VLA_TO_BRAIN_CODE.get(failure.code, failure.code.removeprefix("atom:"))
    traits = ERROR_TRAITS.get(failure.code)
    retryable = (
        traits.retryable
        if traits is not None
        else (code in BRAIN_RETRYABLE_CODES or failure.code.startswith(("atom:", "place_", "wrong_")))
    )
    hint = f" Suggested next: {decision.action}." if decision is not None else ""
    return BrainToolError(
        error_code=code,
        error=f"{failure.step_kind} failed after {failure.attempt} attempt(s): {failure.message or failure.code}."
        + hint,
        retryable=retryable,
    )


def pick_result_from(outcome: ToolStepOutcome) -> PickResult:
    """``PickResult.picked`` is the VERIFIED holding, never the VLA's claim."""
    if outcome.ok and outcome.holding is not None:
        return PickResult(picked=True, instance=outcome.holding, note="verified in the gripper")
    code = outcome.error.error_code if outcome.error else (outcome.failure.code if outcome.failure else "grasp_failed")
    return PickResult(picked=False, error_code=code, note=outcome.error.error if outcome.error else "")


def place_result_from(
    outcome: ToolStepOutcome,
    *,
    sku: Sku | None = None,
    item_id: ItemId | None = None,
    picked_qty: int = 0,
    basket_count: int = 0,
    line_id: str | None = None,
    cart_total_cents: int | None = None,
) -> PlaceResult:
    """``basket_ok``: True = verified, None = accepted provisionally (audit before checkout), False = failed."""
    basket_ok: bool | None = outcome.verified if outcome.ok else False
    return PlaceResult(
        sku=sku,
        item_id=item_id,
        picked_qty=picked_qty,
        basket_count=basket_count,
        line_id=line_id,
        cart_total_cents=cart_total_cents,
        basket_ok=basket_ok,
        note="" if outcome.ok else (outcome.error.error if outcome.error else "place failed"),
    )


ITEM_TO_STOP_STATUS: dict[str, StopStatus] = {
    "in_basket": "done",
    "substituted": "done",
    "declined": "declined",
    "unavailable": "failed",
    "online": "failed",
    "skipped": "skipped",
    "failed": "failed",
}
"""v0.2 rule (contracts.fleet GoalItemStatus docs): shop goals mirror each item's terminal status onto its trip route
stop with ``trips.set_stop_status`` (navigate steps set 'navigating' -> 'arrived'). In plan mode THE RUNNER does it
(once per stop + status), so the v0.2 store map keeps showing done / skipped / failed stops."""


def goal_update_from_outcome(outcome: PlanOutcome) -> GoalUpdate:
    """Plan mode: the terminal ``fleet.update_goal`` that ``RobotBrainService.run_goal`` sends after
    ``LoopRunner.run_goal`` returns (v0.2 finish_goal semantics). The RUNNER never sets goal status (it patches items /
    progress only); the BRAIN finishes the goal with this and returns the fleet's terminal Goal. 'cancelled' carries no
    status: the fleet's cancel_goal owns CANCELLED. A RobotError raised by the runner (escalate) is re-raised as is."""
    if outcome.status == "cancelled":
        return GoalUpdate(note=outcome.summary or "cancelled")
    failure = next(
        (a.failure for a in reversed(outcome.run.attempts) if a.failure is not None and outcome.status == "failed"),
        None,
    )
    return GoalUpdate(
        status=GoalStatus.COMPLETED if outcome.status == "completed" else GoalStatus.FAILED,
        progress=1.0 if outcome.status == "completed" else None,
        summary=outcome.summary,
        failure_code=VLA_TO_BRAIN_CODE.get(failure.code, failure.code) if failure is not None else None,
        error=failure.message if failure is not None else None,
    )


ExecMode = Literal["direct", "plan", "tools"]
"""direct = exact v0.2 path (RobotService called by the brain's tools) · plan = the runner runs a full StepPlan per goal
· tools = the v0.2 LLM tool loop with verified motion tools."""

__all__ = [
    "BRAIN_TOOL_STEPS",
    "ITEM_TO_STOP_STATUS",
    "MIRROR_MODEL",
    "STEP_TO_BRAIN_TOOL",
    "UNCHANGED_BRAIN_TOOLS",
    "ExecMode",
    "ToolStepOutcome",
    "brain_tool_error",
    "goal_update_from_outcome",
    "mirror_brain_step",
    "pick_result_from",
    "place_result_from",
]
