"""PlanLoop: THE orchestrator (``contracts.services.LoopRunner``, agent arm). Deterministic code, so the real runner
and its "fake" are the same class (``create_runner`` / ``create_runner_fake``); what changes is what it is wired to.

Per step:  pre-check (verifier) -> act (VLA via run_vla_to_terminal | verifier ground / verify | v0.2 scan | ask)
           -> post-check (verifier, frames from the VLA, baseline, not_before = VlaOutcome.ended_at)
           -> commit COMMIT_ON_VERIFIED effects once per effect key -> next
On failure: failure_from_vla / failure_from_verdict -> decide_recovery(RetryPolicy) -> apply (insert recovery steps
and rerun, rerun on the next tier, reverify, accept provisionally, ask, replan, skip / mark unavailable, fail, or
escalate by raising RobotError so the fleet reassigns). Verifier down / slow -> RetryPolicy.degrade (state-only
checks by code).

It talks to the v0.2 services only through their Protocols (robot, catalog, perception, trips, approvals, fleet,
events) and to the 0.2.1 loop services through the same ``Services`` container (planner, vla, verifier, truth,
faults). Everything optional degrades: no trips -> a local cart + budget check; no approvals -> ``answer`` callback
(default: approve / first option); no perception -> ground-truth scan reports; no events -> silent.

Stock (catalog.record_pick / apply_scan) and cart (trips.add_line) effects happen ONLY after a step is verified, and
each effect key at most once (``EffectRecord`` ledger). The runner patches goal ITEMS / progress (fleet.update_goal)
and mirrors item status onto trip route stops; it never sets the goal's status (the brain does, with
``bridge.goal_update_from_outcome``). Ported from the API draft (docs/vla-loop, ``fakes/loop.py``).
"""

import asyncio
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NamedTuple

from contracts.bridge import (
    ITEM_TO_STOP_STATUS,
    MIRROR_MODEL,
    ExecMode,
    ToolStepOutcome,
    brain_tool_error,
    mirror_brain_step,
)
from contracts.common import STAFF_USER_ID, Pose2D, utcnow
from contracts.events import make_event, plan_envelope
from contracts.evidence import SlotTruth
from contracts.fleet import BrainStep, Goal, GoalItemUpdate, GoalUpdate
from contracts.grounding import GroundedTarget, ProviderCall, TargetSpec
from contracts.perception import PICK_CHOICE_MAX, BasketCheck, Detection, FreshnessCandidate, PickChoice, ScanReport
from contracts.plan import (
    CLOSED_STEP_STATUSES,
    DEFAULT_HEARTBEAT_S,
    DEFAULT_PLAN_TIMEOUT_S,
    VLA_STEP_KINDS,
    AlignStep,
    AllOf,
    AnyOf,
    AskUserStep,
    BasketContains,
    Condition,
    ExecContext,
    GraspStep,
    GripperEmpty,
    GripperHolding,
    GroundTargetStep,
    HandoffStep,
    ItemOnCounter,
    NavigateToStep,
    PlaceStep,
    PlannerInfo,
    PlanRequest,
    PlanStatus,
    PlanStep,
    RobotAt,
    ScanShelfStep,
    StepPlan,
    StepStatus,
    TargetGrounded,
    VerifyStep,
    atom_cameras,
    binding_key,
    default_conditions,
    goal_attempt,
    iter_atoms,
    make_effect_key,
    make_exec_id,
    make_plan_id,
    make_verify_id,
    plan_errors,
    recovery_step_id,
)
from contracts.planogram import counter_spots
from contracts.policy import (
    ACTION_INSERTS,
    ACTION_RERUNS_STEP,
    COMMIT_ON_VERIFIED,
    DEFAULT_RETRY_POLICY,
    BreakerState,
    EffectRecord,
    LoopMetrics,
    PlanOutcome,
    PlanRunState,
    RecoveryContext,
    RecoveryDecision,
    ReplanRequest,
    RetryPolicy,
    StepAttempt,
    StepFailure,
    decide_recovery,
    failure_from_verdict,
    failure_from_vla,
)
from contracts.services import exec_mode, run_vla_to_terminal
from contracts.shopping import CartLine
from contracts.sim import ItemInstance, RobotError
from contracts.trust import (
    VERIFY_CORRECT,
    VERIFY_WRONG,
    ApprovalKind,
    ApprovalOption,
    ApprovalRequest,
    ApprovalResponse,
    ItemVerification,
)
from contracts.verify import (
    GroundingResult,
    GroundRequest,
    StateFacts,
    Verdict,
    VerifierUnavailableError,
    VerifyBaseline,
    VerifyRequest,
    evaluate_on_state,
    verdict_reason,
    verify_deadline,
)
from contracts.vla import ERROR_TRAITS, VlaCapabilities, VlaRequest, VlaStatus, resolve_tier
from utils.log import get_logger

log = get_logger(__name__)

AnswerFn = Callable[[ApprovalRequest], Awaitable[ApprovalResponse]]
BrainStepHook = Callable[[BrainStep], None]
DONE_ITEM_OK = ("in_basket", "online", "substituted", "declined", "skipped")
DEFAULT_BASKET_CAPACITY = 12
MAX_DROPOFFS = 3
"""Basket drop-offs per run (a full basket before a grasp: checkout, unload, back), as in the v0.2 brain."""
_SET_ASIDE = "set_aside"  # photo check: wrong unit, no put-back -> basket for staff (uncounted) + grab another
_ASIDE_NOTE = "set aside for staff, not charged"


class _AsideUnit(NamedTuple):
    """A unit set aside for staff (never charged) that is in the basket: unloaded onto the counter like the others."""

    place_step_id: str
    item_id: str | None
    unit: int | None
    sku: str
    instance_id: str | None


def _basket_capacity(robot_id: str) -> int:
    try:
        from utils.data import load_robot

        return int(load_robot(robot_id).basket_capacity)
    except Exception:  # noqa: BLE001 - unknown robot / no fixtures: the default rover basket
        return DEFAULT_BASKET_CAPACITY


def _ripeness(score: float) -> str:
    """Shopper words for a freshness score (produce review labels)."""
    if score >= 0.85:
        return "ripe: eat today or tomorrow"
    if score >= 0.7:
        return "ready in 2-3 days"
    return "firm: ripens in about 4 days"


def _verify_items_on() -> bool:
    """PICKY_VERIFY_ITEMS (default on): photo verification cards for every purchased unit."""
    return bool(setting("picky_verify_items", True))


def _unload_group(step: Any) -> bool:
    """A step of the 0.2.4 checkout unload sequence (group 'unload:<n>')."""
    return bool(step.group_id and step.group_id.startswith("unload:"))


async def _run_to_completion(coro: Awaitable[Any]) -> Any:
    """Run ``coro`` (a short, motion-free commit) to the end even if the caller is cancelled meanwhile, then re-raise
    the cancellation: a goal cancel can never leave record_pick done but the cart line / goal item missing."""
    task = asyncio.ensure_future(coro)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait({task})
        raise


async def auto_answer(request: ApprovalRequest) -> ApprovalResponse:
    """Default answer when no TripController / ApprovalBroker is wired: approve; pick the first option."""
    if request.options:
        return ApprovalResponse(
            approval_id=request.approval_id, decision="choice", choice=request.options[0].option_id, channel="system"
        )
    return ApprovalResponse(approval_id=request.approval_id, decision="approve", channel="system")


class _Run:
    def __init__(self, plan: StepPlan, policy: RetryPolicy) -> None:
        self.plan = plan
        self.policy = policy
        self.mode = plan.mode
        self.state = PlanRunState(
            plan_id=plan.plan_id,
            root_plan_id=plan.root_plan_id,
            robot_id=plan.robot_id,
            status=PlanStatus.RUNNING,
            started_at=utcnow(),
        )
        self.metrics = LoopMetrics()
        self.t0 = time.monotonic()
        self.rule_failures: dict[str, dict[str, int]] = defaultdict(dict)
        self.charged: Counter[str] = Counter()
        self.unit_attempts: Counter[tuple[str | None, int | None]] = Counter()
        self.next_tier: dict[str, Any] = {}
        self.recoveries: Counter[str] = Counter()
        self.held: ItemInstance | None = None
        self.line_checked: set[str] = set()
        self.approvals: dict[str, str] = {}
        self.verify_ms: list[float] = []
        self.caps: list[VlaCapabilities] | None = None
        self.last_vla_emit: dict[str, float] = {}
        self.last_vla_phase: dict[str, str] = {}
        self.ledger: dict[str, EffectRecord] = {}
        self.last_scan: dict[str, Any] = {}
        self.outer: tuple[StepFailure, RecoveryDecision] | None = None
        self.last_verdicts: dict[str, Verdict] = {}
        self.slot_geom: dict[str, SlotTruth] | None = None
        self.brain_no = 0
        self.answers: dict[str, bool] = {}  # approval_id -> approved (approval_granted atoms)
        self.stop_status: dict[str, str] = {}  # stop_id -> last status mirrored onto the trip route
        self.scan_frames: dict[str, Any] = {}  # slot_id -> last BLURRED scan frame (pick_choice ranking)
        self.unloaded: Counter[str] = Counter()  # sku -> units dropped off at the checkout mid-run (basket full)
        self.dropoffs = 0
        # 0.2.4 checkout unload by the arm: unit number per 'unload:<n>' group, total, gate checked once
        self.unload_total = sum(
            1 for st in plan.steps if isinstance(st, GraspStep) and getattr(st, "source", "shelf") == "basket"
        )
        self.unload_gate = False
        self.unload_numbers: dict[str, int] = {}  # 'unload:<n>' group -> unit number after the gate's re-layout
        self.rejections: Counter[tuple[str | None, int | None]] = Counter()  # (item_id, unit) -> 'wrong' answers
        self.set_aside: set[str] = set()  # basket place steps carrying a unit the shopper marked wrong: never counted
        self.aside_units: list[_AsideUnit] = []  # set-aside units in the basket now (a drop-off hands them to staff)
        self.aside_groups: set[str] = set()  # 'unload:<n>' groups unloading a set-aside unit (never charged)


class PlanLoop:
    def __init__(
        self,
        services: Any,
        *,
        policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        answer: AnswerFn | None = None,
        emit_v02_events: bool = True,
        mirror_brain_steps: bool = True,
        vla_status_hz: float = 2.0,
        heartbeat_s: float = DEFAULT_HEARTBEAT_S,
        plan_timeout_s: float = DEFAULT_PLAN_TIMEOUT_S,
        on_brain_step: BrainStepHook | None = None,
        verify_items: bool | None = None,
    ) -> None:
        self.s = services
        self.verify_items = verify_items
        """0.2.4 photo verification cards (None = PICKY_VERIFY_ITEMS)."""
        self.on_brain_step = on_brain_step
        """Called with every mirrored ``brain.step`` (the brain keeps them in its per-robot ring for recent_steps)."""
        self.plan_timeout_s = plan_timeout_s
        self.policy = policy
        self.answer = answer or auto_answer
        self.emit_v02_events = emit_v02_events
        self.mirror_brain_steps = mirror_brain_steps
        self.vla_status_hz = vla_status_hz
        self.heartbeat_s = heartbeat_s
        self._runs: dict[str, _Run] = {}
        self._plans: dict[str, StepPlan] = {}
        self._goal_plan: dict[str, str] = {}
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._tool_runs: dict[str, _Run] = {}

    # =================================================================================================================
    # LoopRunner
    # =================================================================================================================
    async def run_goal(self, robot_id: str, goal: Goal) -> PlanOutcome:
        plan = await self.s.require("planner").plan(PlanRequest(robot_id=robot_id, goal=goal))
        await self._plan_ready(plan)
        return await self.run_plan(plan)

    async def _plan_ready(self, plan: StepPlan) -> None:
        """One ``brain.step(kind='thought')`` the moment the plan arrives ('Plan ready: 73 steps in 16.4 s by
        gemini-3.5-flash'), so the robot view does not sit silent between 'planning' and the first step."""
        events = getattr(self.s, "events", None)
        if events is None or not self.mirror_brain_steps:
            return
        bs = BrainStep(
            step_id=f"{plan.plan_id}:ready",
            robot_id=plan.robot_id,
            goal_id=None if plan.goal_id.startswith("adhoc-") else plan.goal_id,
            trip_id=plan.trip_id,
            store_id=plan.store_id,
            kind="thought",
            text=plan_ready_text(plan),
            model=plan.planner.model or MIRROR_MODEL,
        )
        if self.on_brain_step is not None:
            try:
                self.on_brain_step(bs)
            except Exception as exc:  # noqa: BLE001 - the brain's ring is a view; it never stops a run
                log.warning("on_brain_step hook failed: %s", exc)
        await self._publish(make_event("brain.step", source="agent", **plan_envelope(plan), step=bs))

    async def run_plan(self, plan: StepPlan, *, policy: RetryPolicy | None = None) -> PlanOutcome:
        errors = plan_errors(plan)
        if errors:
            raise ValueError("invalid plan: " + "; ".join(e.message for e in errors))
        plan = plan.model_copy(deep=True)
        plan.status = PlanStatus.RUNNING
        run = _Run(plan, policy or self.policy)
        self._register(run)
        task = asyncio.current_task()
        if task is not None:
            self._tasks[plan.root_plan_id] = task
        await self._pub(run, "plan.created", plan=plan.model_copy(deep=True))
        try:
            status = await self._drive(run)
        except asyncio.CancelledError:
            await self._finish(run, "cancelled", "cancelled")
            raise
        except RobotError as exc:
            await self._finish(run, "failed", f"escalated to the fleet: {exc.code}")
            raise
        return await self._finish(run, status)

    async def run_tool_step(
        self, robot_id: str, goal: Goal, tool_name: str, tool_input: Mapping[str, Any]
    ) -> ToolStepOutcome:
        root = f"{make_plan_id(goal.goal_id, goal_attempt(goal))}-tools"  # a reassigned goal never reuses ids
        run = self._tool_runs.get(root)
        if run is None:
            plan = StepPlan(
                plan_id=root,
                root_plan_id=root,
                goal_id=goal.goal_id,
                robot_id=robot_id,
                store_id=goal.store_id,
                trip_id=goal.trip_id,
                user_id=goal.user_id,
                request_id=goal.request_id,
                mode="tools",
                title=goal.title,
                planner=PlannerInfo(origin="tools"),
                items=list(goal.items),
                end_at=goal.end_at,
                status=PlanStatus.RUNNING,
            )
            run = _Run(plan, self.policy)
            self._tool_runs[root] = run
            self._register(run)
            await self._pub(run, "plan.created", plan=plan.model_copy(deep=True))
        steps = await self.s.require("planner").expand_tool_call(run.plan, tool_name, tool_input)
        start = len(run.plan.steps)
        run.plan.steps.extend(steps)
        run.outer = None
        i = start
        while i < len(run.plan.steps):
            step = run.plan.steps[i]
            if step.status in CLOSED_STEP_STATUSES:
                i += 1
                continue
            res = await self._run_step(run, i)
            if res in ("outer", "fail_goal", "cancelled", "replanned"):
                failure, decision = run.outer if run.outer else (None, None)
                new_steps = [s.model_copy(deep=True) for s in run.plan.steps[start:]]
                if failure is None:
                    failure = StepFailure(
                        plan_id=run.plan.plan_id,
                        step_id=step.step_id,
                        step_kind=step.kind,
                        attempt=max(step.attempts, 1),
                        kind="vla_error",
                        code="cancelled",
                        message=res,
                    )
                return ToolStepOutcome(
                    tool=tool_name,
                    ok=False,
                    steps=new_steps,
                    failure=failure,
                    decision=decision,
                    error=brain_tool_error(failure, decision),
                )
            if res == "next":
                i += 1
        new = run.plan.steps[start:]
        verdicts = [run.last_verdicts[s.step_id] for s in new if s.step_id in run.last_verdicts]
        provisional = any(s.status == StepStatus.PROVISIONAL for s in new)
        holding = await self._held_unit(run) if tool_name == "pick_item" else None
        return ToolStepOutcome(
            tool=tool_name,
            ok=True,
            steps=[s.model_copy(deep=True) for s in new],
            verdicts=verdicts,
            holding=holding,
            verified=None if provisional else True,
        )

    async def cancel(self, plan_id: str, reason: str = "") -> PlanRunState:
        run = self._runs[plan_id]
        task = self._tasks.get(run.plan.root_plan_id)
        vla = getattr(self.s, "vla", None)
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel(reason or "cancel")
            await asyncio.wait({task})
        elif vla is not None:
            await vla.cancel_robot(run.plan.robot_id, reason)
        return run.state.model_copy(deep=True)

    async def get_plan(self, plan_id: str) -> StepPlan:
        return self._plans[plan_id].model_copy(deep=True)

    async def current_plan(self, goal_id: str) -> StepPlan | None:
        pid = self._goal_plan.get(goal_id)
        return self._plans[pid].model_copy(deep=True) if pid else None

    async def run_state(self, plan_id: str) -> PlanRunState:
        return self._runs[plan_id].state.model_copy(deep=True)

    async def attempts(self, plan_id: str, step_id: str | None = None) -> list[StepAttempt]:
        return [a for a in self._runs[plan_id].state.attempts if step_id is None or a.step_id == step_id]

    # =================================================================================================================
    # driver
    # =================================================================================================================
    def _register(self, run: _Run) -> None:
        self._runs[run.plan.plan_id] = run
        self._plans[run.plan.plan_id] = run.plan
        self._goal_plan[run.plan.goal_id] = run.plan.plan_id

    async def _drive(self, run: _Run) -> str:
        i = 0
        while i < len(run.plan.steps):
            step = run.plan.steps[i]
            if step.status in CLOSED_STEP_STATUSES:
                i += 1
                continue
            res = await self._run_step(run, i)
            if res in ("fail_goal", "cancelled"):
                return "failed" if res == "fail_goal" else "cancelled"
            if res == "replanned":
                i = 0
            elif res == "next":
                i += 1
        bad = [it for it in run.plan.items if not it.optional and it.status not in DONE_ITEM_OK]
        return "failed" if any(it.status == "failed" for it in bad) else "completed"

    async def _run_step(self, run: _Run, i: int) -> str:
        plan, step = run.plan, run.plan.steps[i]
        if _unload_group(step) and not run.unload_gate:
            run.unload_gate = True
            await self._unload_gate(run)
            return "again"  # the gate may skip / insert steps at i: re-read the plan
        # mandate / budget gate before the FIRST unit of an item (plan mode; tools mode: the v0.2 executor does it)
        from_shelf = not (isinstance(step, GraspStep) and getattr(step, "source", "shelf") == "basket")
        if (
            isinstance(step, GraspStep)
            and from_shelf
            and run.mode == "plan"
            and step.item_id
            and step.item_id not in run.line_checked
        ):
            gate = await self._check_line(run, step)
            if gate == "deny":
                step.status = StepStatus.FAILED
                await self._close_item(run, step.item_id, "failed", "mandate denied")
                await self._pub(
                    run,
                    "step.completed",
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    status=step.status,
                    attempts=step.attempts,
                    note="mandate denied",
                )
                return "next"
            if gate == "ask":
                plan.steps.insert(i, self._threshold_question(run, step))
                return "again"
        if isinstance(step, GraspStep) and from_shelf and run.mode == "plan" and run.held is None:
            dropoff = await self._dropoff_steps(run, step)
            if dropoff:
                plan.steps[i:i] = dropoff
                return "again"
        step.attempts += 1
        attempt = step.attempts
        step.status = StepStatus.RUNNING
        is_vla = step.kind in VLA_STEP_KINDS
        exec_id = make_exec_id(plan.plan_id, step.step_id, attempt) if is_vla else None
        tier_pref = run.next_tier.get(step.step_id) or getattr(step, "tier", "auto")
        planned = resolve_tier(tier_pref, await self._caps(run), step.kind, self._open_tiers(run)) if is_vla else None
        att = StepAttempt(plan_id=plan.plan_id, step_id=step.step_id, attempt=attempt, exec_id=exec_id, tier=planned)
        run.state.attempts.append(att)
        run.state.current_step_id = step.step_id
        run.metrics.attempts += 1
        await self._pub(
            run,
            "step.started",
            plan_id=plan.plan_id,
            step=step.model_copy(deep=True),
            attempt=attempt,
            exec_id=exec_id,
            tier=planned,
            inserted_by=step.inserted_by,
        )
        if isinstance(step, GraspStep) and not from_shelf and attempt == 1:
            await self._unload_event(run, step, "started")
        ctx = ExecContext(
            goal_id=plan.goal_id,
            plan_id=plan.plan_id,
            step_id=step.step_id,
            step_kind=step.kind,
            attempt=attempt,
            item_id=step.item_id,
            trip_id=plan.trip_id,
        )

        # 1. pre-check
        if step.preconditions is not None:
            pre_v = await self._verify(run, step, attempt, "pre", step.preconditions, ctx)
            att.pre = pre_v
            await self._pub(
                run,
                "step.verified",
                plan_id=plan.plan_id,
                step_id=step.step_id,
                attempt=attempt,
                phase="pre",
                verdict=pre_v,
            )
            if not pre_v.passed:
                f = failure_from_verdict(pre_v, plan_id=plan.plan_id, step=step, attempt=attempt, phase="pre")
                assert f is not None
                return await self._recover(run, i, f, att)

        # 2. act
        if isinstance(step, NavigateToStep) and step.stop_id:
            await self._stop_status(run, step.stop_id, "navigating")
        frames: list[Any] = []
        not_before = None
        baseline: VerifyBaseline | None = None
        claimed = False
        if is_vla:
            if any(a.op in ("basket_delta",) for _, a in iter_atoms(step.postconditions)):
                baseline = await self._baseline(run, step)
            binding = getattr(step, "binding", None)
            req = VlaRequest(
                exec_id=exec_id or "",
                robot_id=plan.robot_id,
                store_id=plan.store_id,
                step=step.model_copy(deep=True),
                tier=planned or tier_pref,  # concrete: the VLA service does not know the runner's breakers
                grounding=plan.bindings.get(binding) if binding else None,
                deadline_s=step.effective_timeout_s(),
                heartbeat_s=self.heartbeat_s,
                capture_after=self._capture_cams(step),
                context=ctx,
            )
            terminal = await run_vla_to_terminal(self.s.require("vla"), req, on_status=self._vla_cb(run, step))
            att.vla_terminal = terminal
            att.tier = terminal.tier or planned
            claimed = terminal.phase == "succeeded"
            att.vla_claimed_success = claimed
            att.fault_ids = await self._fault_ids(run, step, attempt)
            if terminal.phase == "cancelled":
                step.status = StepStatus.CANCELLED
                return "cancelled"
            if not claimed:
                f = failure_from_vla(terminal, plan_id=plan.plan_id, step=step, attempt=attempt)
                assert f is not None
                if f.code == "disconnected":
                    run.caps = None
                return await self._recover(run, i, f, att)
            frames = list(terminal.outcome.frames) if terminal.outcome else []
            not_before = (terminal.outcome.ended_at if terminal.outcome else None) or terminal.ts
        elif isinstance(step, GroundTargetStep):
            res = await self._ground(run, step, ctx)
            if res.found and res.target is not None:
                plan.bindings[step.binding] = res.target
                await self._pub(
                    run, "target.grounded", plan_id=plan.plan_id, step_id=step.step_id, grounding=res.target
                )
            else:
                f = StepFailure(
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    step_kind=step.kind,
                    attempt=attempt,
                    kind="postcondition",
                    code="target_not_grounded",
                    message=res.reason,
                    item_id=step.item_id,
                )
                return await self._recover(run, i, f, att)
        elif isinstance(step, VerifyStep):
            cond = self._audit_condition(run) if step.purpose == "audit" else step.condition
            v = await self._verify(run, step, attempt, "verify", cond, ctx)
            att.post = v
            run.last_verdicts[step.step_id] = v
            await self._pub(
                run,
                "step.verified",
                plan_id=plan.plan_id,
                step_id=step.step_id,
                attempt=attempt,
                phase="verify",
                verdict=v,
            )
            if not v.passed:
                f = failure_from_verdict(v, plan_id=plan.plan_id, step=step, attempt=attempt, phase="verify")
                assert f is not None
                return await self._recover(run, i, f, att)
            if step.purpose == "audit":
                run.state.provisional_open = False
            return await self._succeed(run, step, att)
        elif isinstance(step, ScanShelfStep):
            count = await self._scan(run, step, attempt)
            if count < step.min_count:
                f = StepFailure(
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    step_kind=step.kind,
                    attempt=attempt,
                    kind="postcondition",
                    code="atom:item_on_shelf",
                    item_id=step.item_id,
                    message=f"{count} x {step.expected_sku} on the shelf",
                )
                return await self._recover(run, i, f, att)
            return await self._succeed(run, step, att)
        elif isinstance(step, AskUserStep):
            res2 = await self._ask_step(run, step)
            await self._succeed(run, step, att)
            return res2

        # 3. post-check
        if step.postconditions is not None:
            post_v = await self._verify(
                run,
                step,
                attempt,
                "post",
                step.postconditions,
                ctx,
                frames=frames,
                baseline=baseline,
                not_before=not_before,
            )
            att.post = post_v
            run.last_verdicts[step.step_id] = post_v
            caught = claimed and post_v.outcome == "fail"
            await self._pub(
                run,
                "step.verified",
                plan_id=plan.plan_id,
                step_id=step.step_id,
                attempt=attempt,
                phase="post",
                verdict=post_v,
                caught_vla_false_success=caught,
            )
            if not post_v.passed:
                f = failure_from_verdict(
                    post_v,
                    plan_id=plan.plan_id,
                    step=step,
                    attempt=attempt,
                    phase="post",
                    tier=att.tier,
                    vla_claimed_success=claimed,
                )
                assert f is not None
                return await self._recover(run, i, f, att, baseline=baseline)
        # 3a. the shopper's chosen unit (pick_choice): a grasp (T1) that took ANOTHER unit of the SKU is a wrong target
        if isinstance(step, GraspStep) and from_shelf and run.mode == "plan" and step.target.instance_id:
            held = await self._held_unit(run)
            if held is not None and held.instance_id != step.target.instance_id:
                return await self._not_the_chosen_unit(run, i, att, held, baseline)
        # 3b. 0.2.4: the shopper checks a photo of the unit in the gripper (correct / wrong; auto-accept on silence)
        if isinstance(step, GraspStep) and from_shelf and run.mode == "plan" and self._verify_on():
            answer = await self._verify_item(run, step, att)
            if answer == _SET_ASIDE and self._set_aside(run, i):
                return await self._succeed(run, step, att, note="wrong unit set aside for staff; grabbing another")
            if answer == VERIFY_WRONG:
                f = StepFailure(
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    step_kind=step.kind,
                    attempt=attempt,
                    kind="postcondition",
                    code="wrong_item_grasped",
                    message="the shopper says it is the wrong item (photo check)",  # the recovery says what's next
                    item_id=step.item_id,
                )
                return await self._recover(run, i, f, att, baseline=baseline)
        # 4. commit
        return await self._succeed(run, step, att)

    # =================================================================================================================
    # success / effects
    # =================================================================================================================
    async def _succeed(
        self, run: _Run, step: PlanStep, att: StepAttempt, *, provisional: bool = False, note: str = ""
    ) -> str:
        if isinstance(step, GraspStep):
            run.held = await self._held_unit(run)
        effects = await _run_to_completion(self._commit(run, step, att, provisional)) if run.mode == "plan" else []
        step.status = StepStatus.PROVISIONAL if provisional else StepStatus.SUCCEEDED
        if provisional:
            run.state.provisional_open = True
        await self._complete(run, step, att, effects, note=note)
        if isinstance(step, VerifyStep) and _unload_group(step):
            await self._unload_event(run, step, "verified", verdict=att.post)
            await self._counter_card(run, step, att)
        elif isinstance(step, HandoffStep) and step.group_id == "checkout" and run.unload_gate and run.unload_total:
            await self._unload_event(run, step, "done")
        return "next"

    async def _complete(
        self, run: _Run, step: PlanStep, att: StepAttempt, effects: list[EffectRecord], note: str = ""
    ) -> None:
        att.effects = effects
        att.ended_at = utcnow()
        await self._pub(
            run,
            "step.completed",
            plan_id=run.plan.plan_id,
            step_id=step.step_id,
            status=step.status,
            attempts=step.attempts,
            effects=effects,
            note=note,
        )
        await self._mirror(
            run, step, f"{step.status.value}" + (f" ({note})" if note else ""), ok=step.status != StepStatus.FAILED
        )

    async def _commit(self, run: _Run, step: PlanStep, att: StepAttempt, provisional: bool) -> list[EffectRecord]:
        kinds = COMMIT_ON_VERIFIED.get(step.kind, ())
        if isinstance(step, PlaceStep) and step.destination == "shelf":
            kinds = ("return_to_shelf",)
        elif isinstance(step, PlaceStep) and step.destination == "counter":
            kinds = ("unload_to_counter",)
        out: list[EffectRecord] = []
        for kind in kinds:
            scope = (step.item_id or step.step_id) if kind == "add_cart_line" else step.step_id
            key = make_effect_key(run.plan.root_plan_id, scope, kind)
            if key in run.ledger:
                continue
            payload = await self._apply_effect(run, step, kind, provisional)
            if payload is None:
                continue
            rec = EffectRecord(
                effect_key=key,
                kind=kind,
                plan_id=run.plan.plan_id,
                step_id=step.step_id,
                item_id=step.item_id,  # type: ignore[arg-type]
                exec_id=att.exec_id,
                verify_id=att.post.verify_id if att.post else None,
                provisional=provisional,
                confidence=att.post.confidence if att.post else 1.0,
                payload=payload,
            )
            run.ledger[key] = rec
            run.state.effects.append(rec)
            out.append(rec)
        return out

    async def _apply_effect(
        self, run: _Run, step: PlanStep, kind: str, provisional: bool = False
    ) -> dict[str, Any] | None:
        plan = run.plan
        item = plan.item(step.item_id)
        if kind == "stop_arrived":
            assert isinstance(step, NavigateToStep)
            if step.stop_id:
                await self._stop_status(run, step.stop_id, "arrived")
            if step.slot_id or step.stop_id:
                await self._pub02(
                    run,
                    "robot.arrived",
                    stop_id=step.stop_id,
                    slot_id=step.slot_id,
                    goal_id=plan.goal_id,
                    pose=step.goal_pose,
                )
            return {"slot_id": step.slot_id, "stop_id": step.stop_id}
        if kind == "apply_scan":
            return run.last_scan.get(step.step_id, {})
        if kind == "record_pick":
            assert isinstance(step, PlaceStep)
            unit = run.held
            slot_id = (unit.slot_id if unit else None) or (item.slot_id if item else None)
            catalog = getattr(self.s, "catalog", None)
            if catalog is not None and slot_id:
                await catalog.record_pick(
                    plan.store_id, slot_id, step.sku, 1, plan.trip_id, plan.robot_id, unit.lot_id if unit else None
                )
            await self._pub02(
                run,
                "item.picked",
                sku=step.sku,
                slot_id=slot_id,
                instance_id=unit.instance_id if unit else None,
                stop_id=item.stop_id if item else None,
                goal_id=plan.goal_id,
            )
            payload = {
                "sku": step.sku,
                "slot_id": slot_id,
                "instance_id": unit.instance_id if unit else None,
                "lot_id": unit.lot_id if unit else None,
            }
            if step.step_id in run.set_aside:  # in the basket for staff: the checkout unload puts it on the counter
                run.aside_units.append(
                    _AsideUnit(step.step_id, step.item_id, step.unit, step.sku, unit.instance_id if unit else None)
                )
            run.held = None
            await self._basket_event(run, pending_sku=step.sku)  # this unit's ledger record lands after we return
            return payload
        if kind == "goal_item_progress":
            if item is None:
                return {}
            if step.step_id in run.set_aside:  # the shopper marked this unit wrong: in the basket for staff, never paid
                return {"picked_qty": item.picked_qty, "qty": item.qty, "set_aside": True}
            if item.picked_qty >= item.qty:  # defence in depth: an extra unit never inflates the item / cart line
                return {"picked_qty": item.picked_qty, "qty": item.qty, "extra_unit": True}
            item.picked_qty += 1
            if (
                run.state.effects
                and run.state.effects[-1].kind == "record_pick"
                and run.state.effects[-1].payload.get("instance_id")
            ):
                item.instance_ids.append(run.state.effects[-1].payload["instance_id"])
            item.robot_id = plan.robot_id
            if item.picked_qty >= item.qty:
                item.status = "in_basket"
            await self._goal_update(run, item, note=step.title)
            return {"picked_qty": item.picked_qty, "qty": item.qty}
        if kind == "add_cart_line":
            if item is None or item.picked_qty < item.qty:
                return None
            unsure = provisional or any(r.provisional and r.item_id == item.item_id for r in run.ledger.values())
            return await self._add_line(run, item, item.qty, provisional=unsure)
        if kind == "unload_to_counter":
            assert isinstance(step, PlaceStep)
            unit = run.held
            run.held = None
            spot = step.counter.index if step.counter is not None else None
            await self._unload_event(run, step, "placed")
            return {"sku": step.sku, "instance_id": unit.instance_id if unit else None, "counter_spot": spot}
        if kind in ("unload_basket", "return_to_shelf"):
            return {}
        return {}

    async def _add_line(self, run: _Run, item: Any, qty: int, *, provisional: bool = False) -> dict[str, Any]:
        """Cart line for verified units. A line with any PROVISIONAL unit gets status 'picked' (not 'verified'): the
        web app shows it as provisional and the pre-checkout basket audit must pass before the handoff."""
        plan = run.plan
        catalog = getattr(self.s, "catalog", None)
        product = await catalog.get_product(item.sku) if catalog is not None and item.sku else None
        line = CartLine(
            line_id=f"line-{item.item_id}",
            sku=item.sku or "",
            name=item.name,
            category=item.category or (product.category if product else "uncategorized"),
            qty=qty,
            unit_price_cents=item.price_cents or (product.price_cents if product else 0),
            store_id=plan.store_id,
            slot_id=item.slot_id,
            rec_id=item.rec_id,
            list_item_id=item.list_item_id,
            instance_ids=list(item.instance_ids),
            status="picked" if provisional else "verified",
        )
        trips = getattr(self.s, "trips", None)
        if trips is not None and plan.trip_id:
            res = await trips.add_line(plan.trip_id, line, approval_id=run.approvals.get(item.item_id))
            item.line_id = getattr(getattr(res, "line", None), "line_id", line.line_id)
        else:
            run.state.local_cart.append(line)
            item.line_id = line.line_id
        return {
            "line_id": item.line_id,
            "qty": qty,
            "total_cents": line.qty * line.unit_price_cents,
            "line_status": line.status,
        }

    async def _close_item(self, run: _Run, item_id: str | None, status: str, reason: str) -> None:
        if item_id is None:
            return
        for s in run.plan.steps:
            if s.item_id == item_id and s.status not in CLOSED_STEP_STATUSES and s.status != StepStatus.RUNNING:
                s.status = StepStatus.SKIPPED
        item = run.plan.item(item_id)
        if item is None:
            return
        item.status = status  # type: ignore[assignment]
        item.notes = reason
        if run.mode == "plan" and item.picked_qty > 0:
            key = make_effect_key(run.plan.root_plan_id, item_id, "add_cart_line")
            if key not in run.ledger:
                payload = await self._add_line(run, item, item.picked_qty)
                rec = EffectRecord(
                    effect_key=key, kind="add_cart_line", plan_id=run.plan.plan_id, item_id=item_id, payload=payload
                )
                run.ledger[key] = rec
                run.state.effects.append(rec)
        if status == "unavailable" and run.mode == "plan":
            trips = getattr(self.s, "trips", None)
            if trips is not None and run.plan.trip_id and item.sku:
                online = CartLine(
                    line_id=f"online-{item_id}",
                    sku=item.sku,
                    name=item.name,
                    category=item.category or "uncategorized",
                    qty=max(item.qty - item.picked_qty, 1),
                    unit_price_cents=item.price_cents or 0,
                    source="online",
                    store_id="online",
                )
                await trips.add_line(run.plan.trip_id, online)
            item.status = "online"
            key = make_effect_key(run.plan.root_plan_id, item_id, "online_line")
            rec = EffectRecord(
                effect_key=key, kind="online_line", plan_id=run.plan.plan_id, item_id=item_id, payload={"sku": item.sku}
            )
            run.ledger.setdefault(key, rec)
            run.state.effects.append(rec)
        await self._goal_update(run, item, note=reason)

    async def _stop_status(self, run: _Run, stop_id: str, status: str, reason: str = "") -> None:
        """v0.2 rule: mirror progress onto the trip route stop (trips.set_stop_status), once per stop + status."""
        trips = getattr(self.s, "trips", None)
        if trips is None or not run.plan.trip_id or run.stop_status.get(stop_id) == status:
            return
        run.stop_status[stop_id] = status
        try:
            await trips.set_stop_status(run.plan.trip_id, stop_id, status, reason)  # type: ignore[arg-type]
        except (KeyError, ValueError):
            pass

    async def _goal_update(self, run: _Run, item: Any, note: str = "") -> None:
        stop = ITEM_TO_STOP_STATUS.get(item.status)
        if item.stop_id and stop is not None:
            await self._stop_status(run, item.stop_id, stop, note)
        fleet = getattr(self.s, "fleet", None)
        if fleet is None or run.plan.goal_id.startswith("adhoc-"):
            return
        try:
            await fleet.update_goal(
                run.plan.goal_id,
                GoalUpdate(
                    items=[
                        GoalItemUpdate(
                            item_id=item.item_id,
                            status=item.status,
                            picked_qty=item.picked_qty,
                            instance_ids=list(item.instance_ids),
                            robot_id=item.robot_id,
                            line_id=item.line_id,
                        )
                    ],
                    progress=run.plan.progress,
                    note=note,
                ),
            )
        except (KeyError, ValueError):
            pass

    # =================================================================================================================
    # recovery
    # =================================================================================================================
    async def _recover(
        self, run: _Run, i: int, failure: StepFailure, att: StepAttempt, *, baseline: VerifyBaseline | None = None
    ) -> str:
        plan, step = run.plan, run.plan.steps[i]
        policy = run.policy
        att.failure = failure
        att.ended_at = utcnow()
        if failure.code == "cancelled":
            step.status = StepStatus.CANCELLED
            return "cancelled"
        traits = ERROR_TRAITS.get(failure.code)
        counts = traits.counts_attempt if traits is not None else True
        if counts:
            run.charged[step.step_id] += 1
            if step.kind == "grasp":
                run.unit_attempts[(step.item_id, step.unit)] += 1
        tier = failure.tier
        brk = run.state.breakers.get(tier) if tier else None
        caps = await self._caps(run)
        item = plan.item(step.item_id)
        rctx = RecoveryContext(
            failure=failure,
            rule_failures=dict(run.rule_failures[step.step_id]),
            failures_of_step=max(1, run.charged[step.step_id]),
            unit_attempts=max(1, run.unit_attempts[(step.item_id, step.unit)]),
            replans_used=run.state.replans,
            questions_used=run.state.questions,
            wall_s=time.monotonic() - run.t0,
            tier_strikes=brk.strikes if brk else 0,
            optional_step=step.optional or bool(item and item.optional),
            available_tiers=[c.tier for c in caps if c.available] or ["t0_scripted"],
            mode=run.mode,
        )
        decision = decide_recovery(policy, rctx)
        if _unload_group(step):
            decision = await self._unload_decision(run, step, failure, decision)
        rid = decision.rule_id or "_fallback"
        run.rule_failures[step.step_id][rid] = run.rule_failures[step.step_id].get(rid, 0) + 1
        att.decision = decision
        if failure.caught_by_verifier:
            run.metrics.verifier_catches += 1
        left = max(0, policy.budget.max_step_attempts - run.charged[step.step_id])
        await self._pub(
            run,
            "step.failed",
            plan_id=plan.plan_id,
            step_id=step.step_id,
            failure=failure,
            decision=decision,
            attempts_left=left,
        )
        await self._breaker(run, failure, decision)
        await self._mirror(run, step, decision.reason, ok=False)

        action = decision.action
        if run.mode == "tools" and not decision.inner and action != "escalate":
            step.status = StepStatus.FAILED
            run.outer = (failure, decision)
            return "outer"
        if action == "escalate":
            step.status = StepStatus.FAILED
            code = failure.code if failure.code in ("battery_low", "unknown_robot") else "unknown_robot"
            raise RobotError(code, f"{failure.code}: {failure.message}")  # type: ignore[arg-type]
        if action == "adopt_held":
            return await self._succeed(run, step, att, note="already holding it")
        if action in ACTION_RERUNS_STEP:
            if decision.backoff_s:
                await asyncio.sleep(decision.backoff_s)
            if decision.next_tier:
                run.next_tier[step.step_id] = decision.next_tier
            held_unit = await self._held_unit(run) if action in ("return_item", "return_held") else None
            inserts = self._inserts(run, i, decision.action, failure, held_unit)
            plan.steps[i:i] = inserts
            step.status = StepStatus.PENDING
            run.metrics.retries += 1
            return "again"
        if action == "reverify":
            return await self._reverify(run, i, failure, att, baseline)
        if action == "accept_provisional":
            return await self._succeed(run, step, att, provisional=True)
        if action in ("ask_user", "substitute"):
            return await self._ask_recovery(run, i, failure, decision)
        if action == "replan":
            return await self._replan(run, step, failure, decision)
        if action == "mark_unavailable":
            step.status = StepStatus.FAILED
            await self._close_item(run, step.item_id, "unavailable", decision.reason)
            await self._complete(run, step, att, [], note="ordering online")
            return "next"
        if action == "skip_item" and _unload_group(step):  # the unit stays in the basket: the handoff bags it
            await self._skip_unload_group(run, step, decision.reason)
            await self._complete(run, step, att, [], note="left in the basket for the handoff")
            return "next"
        if action == "skip_item":
            optional = bool(item and item.optional) or step.optional
            step.status = StepStatus.SKIPPED if optional else StepStatus.FAILED
            await self._close_item(run, step.item_id, "skipped" if optional else "failed", decision.reason)
            await self._complete(run, step, att, [], note="item skipped")
            return "next"
        step.status = StepStatus.FAILED
        await self._complete(run, step, att, [], note="goal failed")
        return "fail_goal"

    async def _reverify(
        self, run: _Run, i: int, failure: StepFailure, att: StepAttempt, baseline: VerifyBaseline | None
    ) -> str:
        step = run.plan.steps[i]
        phase = failure.verdict.phase if failure.verdict else "post"
        if phase == "pre":
            step.status = StepStatus.PENDING
            return "again"
        cond: Condition | None = step.condition if isinstance(step, VerifyStep) else step.postconditions
        if isinstance(step, VerifyStep) and step.purpose == "audit":
            cond = self._audit_condition(run)
        assert cond is not None
        ctx = ExecContext(
            goal_id=run.plan.goal_id,
            plan_id=run.plan.plan_id,
            step_id=step.step_id,
            step_kind=step.kind,
            attempt=step.attempts,
            item_id=step.item_id,
            trip_id=run.plan.trip_id,
        )
        n = run.recoveries[step.step_id] = run.recoveries[step.step_id] + 1
        v = await self._verify(run, step, step.attempts, f"reverify{n}", cond, ctx, baseline=baseline, alternate=True)
        await self._pub(
            run,
            "step.verified",
            plan_id=run.plan.plan_id,
            step_id=step.step_id,
            attempt=step.attempts,
            phase="reverify",
            verdict=v,
            caught_vla_false_success=att.vla_claimed_success and v.outcome == "fail",
        )
        run.last_verdicts[step.step_id] = v
        if v.passed:
            att.post = v
            if isinstance(step, VerifyStep) and step.purpose == "audit":
                # the audit passed on a second look: it closes the provisional lines exactly like a first-look pass
                # (before, checkout stayed blocked with 'basket_audit_pending' after a passing reverify; vhagar run4)
                run.state.provisional_open = False
            return await self._succeed(run, step, att)
        f = failure_from_verdict(
            v,
            plan_id=run.plan.plan_id,
            step=step,
            attempt=step.attempts,
            phase="reverify",
            tier=att.tier,
            vla_claimed_success=att.vla_claimed_success,
        )
        assert f is not None
        return await self._recover(run, i, f, att, baseline=baseline)

    async def _ask_recovery(self, run: _Run, i: int, failure: StepFailure, decision: RecoveryDecision) -> str:
        step = run.plan.steps[i]
        item = run.plan.item(step.item_id)
        run.state.questions += 1
        run.metrics.asks += 1
        name = (item.name or item.query) if item else step.title
        picked = item.picked_qty if item else 0
        # a substitute replaces the WHOLE item (sku + price of its single cart line), so it is offered only while no
        # unit of the original is in the basket; otherwise the rest is ordered online (a partial line is kept)
        sub = (
            item.substitutes[0]
            if (decision.action == "substitute" and item and item.substitutes and picked == 0)
            else None
        )
        if decision.action == "substitute" and sub is None and item is not None and picked > 0:
            question, kind = (
                f"I got {picked} of {item.qty} {name}, the shelf is empty now. Order the other "
                f"{item.qty - picked} online?",
                "online_order",
            )
        elif decision.action == "substitute" and sub is None:
            question, kind = (
                f"I can't get {name} ({failure.message or failure.code}). Order it online instead?",
                "online_order",
            )
        elif sub is not None:
            question, kind = f"{name} is not available. Take {sub} instead?", "substitution"
        else:
            question, kind = (
                f"I'm having trouble with '{step.title}' ({failure.message or failure.code}). Try again?",
                "question",
            )
        resp = await self._ask(run, kind, question, [], item, step)
        if resp.approved and sub is not None:
            step.status = StepStatus.FAILED
            return await self._replan(run, step, failure, decision, substitutions={item.item_id: sub} if item else {})
        if resp.approved and decision.action == "substitute":
            step.status = StepStatus.FAILED
            await self._close_item(run, step.item_id, "unavailable", "shopper chose to order online")
            return "next"
        if resp.approved:
            step.status = StepStatus.PENDING
            return "again"
        optional = bool(item and item.optional)
        step.status = StepStatus.SKIPPED if optional else StepStatus.FAILED
        await self._close_item(run, step.item_id, "skipped" if optional else "failed", "shopper said skip it")
        await self._complete(run, step, run.state.attempts[-1], [], note="shopper said skip")
        return "next"

    async def _replan(
        self,
        run: _Run,
        step: PlanStep,
        failure: StepFailure,
        decision: RecoveryDecision,
        substitutions: dict[str, str] | None = None,
    ) -> str:
        planner = getattr(self.s, "planner", None)
        if planner is None or run.mode == "tools":
            step.status = StepStatus.FAILED
            await self._close_item(run, step.item_id, "failed", "no planner to re-plan with")
            return "next"
        run.state.replans += 1
        run.metrics.replans += 1
        if step.status not in CLOSED_STEP_STATUSES:
            step.status = StepStatus.FAILED
        old = run.plan
        robot_state = await self.s.require("robot").state(old.robot_id)
        try:
            new = await asyncio.wait_for(
                planner.replan(
                    ReplanRequest(
                        plan=old,
                        failure=failure,
                        decision=decision,
                        robot_state=robot_state,
                        substitutions=substitutions or {},
                        reason=decision.reason,
                    )
                ),
                timeout=self.plan_timeout_s,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken / slow re-plan ends this item, never the whole plan
            await self._close_item(run, step.item_id, "failed", f"re-plan failed ({type(exc).__name__}: {exc})")
            return "next"
        old.status = PlanStatus.SUPERSEDED
        new.status = PlanStatus.RUNNING
        run.plan = new
        run.state.plan_id = new.plan_id
        self._register(run)
        await self._pub(
            run,
            "plan.revised",
            plan=new.model_copy(deep=True),
            previous_plan_id=old.plan_id,
            reason=decision.reason,
            failure=failure,
        )
        return "replanned"

    def _inserts(
        self, run: _Run, i: int, action: str, failure: StepFailure, held_unit: ItemInstance | None = None
    ) -> list[PlanStep]:
        plan, step = run.plan, run.plan.steps[i]
        kinds = ACTION_INSERTS.get(action, ())
        if not kinds:
            return []
        n = run.recoveries[step.step_id] = run.recoveries[step.step_id] + 1
        grasp = (
            step
            if isinstance(step, GraspStep)
            else next(
                (
                    s
                    for s in reversed(plan.steps[:i])
                    if isinstance(s, GraspStep) and s.item_id == step.item_id and s.unit == step.unit
                ),
                None,
            )
        )
        target = grasp.target if grasp else None
        loc = (target.location if target else None) or getattr(step, "location", None)
        common = {
            "item_id": step.item_id,
            "unit": step.unit,
            "group_id": step.group_id,
            "group_label": step.group_label,
            "origin": "recovery",
            "inserted_by": step.step_id,
        }
        held = held_unit.sku if held_unit is not None else None
        if held is None and failure.verdict is not None:
            held = next((a.observed.get("held_sku") for a in failure.verdict.atoms if a.observed.get("held_sku")), None)
        # the shopper's unit (pick_choice) survives a re-ground of the SAME unit; a new unit is needed after a drop
        keep_unit = action in ("reground", "realign", "renavigate")
        out: list[PlanStep] = []
        for k, kind in enumerate(kinds, start=1):
            sid = recovery_step_id(step.step_id, n, k)
            if kind == "ground_target":
                if target is None:
                    continue
                bkey = grasp.binding if grasp and grasp.binding else binding_key(step.item_id, step.unit)
                pre, post = default_conditions("ground_target", sku=target.sku, slot_id=target.slot_id, binding=bkey)
                out.append(
                    GroundTargetStep(
                        step_id=sid,
                        title=f"Find the {target.description} again",
                        target=target if keep_unit else target.model_copy(update={"instance_id": None}),
                        binding=bkey,
                        camera="front" if n == 1 else "wrist",
                        preconditions=pre,
                        postconditions=post,
                        **common,
                    )
                )
            elif kind == "align" and loc is not None and not isinstance(step, AlignStep):
                pre, post = default_conditions("align", slot_id=loc.slot_id, approach_pose=loc.approach_pose)
                out.append(
                    AlignStep(
                        step_id=sid,
                        title="Re-align to the shelf",
                        slot_id=loc.slot_id,
                        location=loc,
                        lift_height_m=loc.lift_height_m,
                        preconditions=pre,
                        postconditions=post,
                        **common,
                    )
                )
            elif kind == "navigate_to" and loc is not None:
                pre, post = default_conditions("navigate_to", goal_pose=loc.approach_pose)
                out.append(
                    NavigateToStep(
                        step_id=sid,
                        title="Drive back to the shelf",
                        target="slot",
                        slot_id=loc.slot_id,
                        goal_pose=loc.approach_pose,
                        preconditions=pre,
                        postconditions=post,
                        **common,
                    )
                )
            elif kind == "scan_shelf" and loc is not None and not isinstance(step, ScanShelfStep):
                out.append(
                    ScanShelfStep(
                        step_id=sid,
                        title="Re-scan the shelf",
                        slot_id=loc.slot_id,
                        expected_sku=target.sku if target else None,
                        location=loc,
                        min_count=1,
                        **common,
                    )
                )
            elif kind == "grasp" and grasp is not None:
                pre, post = default_conditions(
                    "grasp",
                    sku=grasp.target.sku,
                    slot_id=grasp.target.slot_id,
                    approach_pose=loc.approach_pose if loc else None,
                )
                out.append(
                    GraspStep(
                        step_id=sid,
                        title=f"Grab another {grasp.target.description}",
                        target=grasp.target.model_copy(update={"instance_id": None}),
                        binding=grasp.binding,
                        instruction=grasp.instruction,
                        preconditions=pre,
                        postconditions=post,
                        **common,
                    )
                )
            elif kind == "place":
                sku = held or (target.sku if target else None) or getattr(step, "sku", None)
                if sku is None:
                    continue
                # shopper-visible title: the product's words ('Casa Verde Black Beans 15 oz'), never the raw SKU
                label = (target.description if target is not None and target.sku == sku else None) or next(
                    (it.name for it in plan.items if it.sku == sku and it.name), None
                )
                if action in ("return_item", "return_held"):
                    # back to the HELD unit's own slot (ItemInstance.slot_id), else a planned slot of that SKU
                    slot = (held_unit.slot_id if held_unit is not None else None) or next(
                        (s.target.slot_id for s in plan.steps if isinstance(s, GraspStep) and s.target.sku == sku),
                        None,
                    )
                    out.append(
                        PlaceStep(
                            step_id=sid,
                            title=f"Put the {label or 'wrong item'} back",
                            sku=sku,
                            destination="shelf",
                            slot_id=slot or (target.slot_id if target else None),
                            preconditions=GripperHolding(sku=sku, note="holding the wrong item"),
                            postconditions=GripperEmpty(note="put back"),
                            **common,
                        )
                    )
                else:
                    pre, post = default_conditions("place", sku=sku)
                    out.append(
                        PlaceStep(
                            step_id=sid,
                            title=f"Put the held {label or 'item'} in the basket first",
                            sku=sku,
                            preconditions=pre,
                            postconditions=post,
                            **common,
                        )
                    )
        return out

    async def _breaker(self, run: _Run, failure: StepFailure, decision: RecoveryDecision) -> None:
        tier = failure.tier
        if tier in ("t1_pretrained", "t2_finetuned"):
            traits = ERROR_TRAITS.get(failure.code)
            if (traits is not None and traits.t1_strike) or failure.kind in ("postcondition", "stalled"):
                b = run.state.breakers.setdefault(tier, BreakerState(tier=tier))
                b.strikes += 1
        if decision.open_breaker:
            b = run.state.breakers.setdefault(decision.open_breaker, BreakerState(tier=decision.open_breaker))
            b.open, b.opened_at, b.reason = True, utcnow(), decision.reason
            run.metrics.tier_fallbacks += 1
            await self._pub(
                run,
                "vla.tier_changed",
                from_tier=decision.open_breaker,
                to_tier=decision.next_tier if decision.next_tier not in (None, "auto") else "t0_scripted",
                reason=decision.reason,
                breaker=b.model_copy(),
            )
        elif decision.action == "downgrade_tier" and tier not in (None, "t0_scripted"):
            run.metrics.tier_fallbacks += 1

    def _open_tiers(self, run: _Run) -> set[str]:
        return {t for t, b in run.state.breakers.items() if b.open}

    # =================================================================================================================
    # verification (+ degrade policy)
    # =================================================================================================================
    async def _verify(
        self,
        run: _Run,
        step: PlanStep,
        attempt: int,
        phase: str,
        cond: Condition,
        ctx: ExecContext,
        *,
        frames: list[Any] | None = None,
        baseline: VerifyBaseline | None = None,
        not_before: Any = None,
        alternate: bool = False,
    ) -> Verdict:
        plan = run.plan
        vphase = "reverify" if phase.startswith("reverify") else phase
        req = VerifyRequest(
            verify_id=make_verify_id(plan.plan_id, step.step_id, attempt, phase),
            robot_id=plan.robot_id,
            store_id=plan.store_id,
            phase=vphase,
            condition=cond,
            frames=list(frames or []),
            baseline=baseline,
            bindings=dict(plan.bindings),
            approvals=dict(run.answers),
            context=ctx,
            not_before=not_before,
            alternate_cameras=alternate,
            deadline_s=verify_deadline(run.policy.degrade.verify_timeout_s),
        )  # type: ignore[arg-type]
        t0 = time.monotonic()
        verifier = getattr(self.s, "verifier", None)
        try:
            if verifier is None:
                raise VerifierUnavailableError("no verifier wired")
            v = await asyncio.wait_for(verifier.verify(req), timeout=run.policy.degrade.verify_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - VerifierUnavailableError, TimeoutError, or a broken verifier
            v = await self._degraded_verdict(run, req, f"{type(exc).__name__}: {exc}")
        run.verify_ms.append((time.monotonic() - t0) * 1000)
        if v.degraded:
            run.metrics.degraded_checks += 1
            if run.state.attempts:
                run.state.attempts[-1].degraded = True
        if v.provider is not None:
            run.metrics.provider_calls[v.provider.provider] = run.metrics.provider_calls.get(v.provider.provider, 0) + 1
        return v

    async def _degraded_verdict(self, run: _Run, req: VerifyRequest, why: str) -> Verdict:
        if run.policy.degrade.on_unavailable == "uncertain":
            return Verdict(
                verify_id=req.verify_id,
                robot_id=req.robot_id,
                phase=req.phase,
                outcome="uncertain",
                confidence=0.0,
                reason=f"verifier unavailable ({why})",
                decided_by="state",
                degraded=True,
                methods_tried=["state"],
            )
        facts = await self._facts(run)
        outcome, conf, atoms = evaluate_on_state(req.condition, facts, baseline=req.baseline, bindings=req.bindings)
        return Verdict(
            verify_id=req.verify_id,
            robot_id=req.robot_id,
            phase=req.phase,
            outcome=outcome,
            confidence=conf,
            reason=f"verifier unavailable ({why}); robot state: {verdict_reason(outcome, atoms)}",
            atoms=atoms,
            decided_by="state",
            provider=ProviderCall(provider="state", call="robot_state"),
            degraded=True,
            methods_tried=["state"],
        )

    async def _facts(self, run: _Run) -> StateFacts:
        robot = self.s.require("robot")
        st = await robot.state(run.plan.robot_id)
        held_sku = None
        counter: Counter[str] = Counter()
        if st.holding or run.unload_total:
            units = await robot.list_instances(run.plan.robot_id)
            held_sku = next((u.sku for u in units if u.instance_id == st.holding), None) if st.holding else None
            counter = Counter(u.sku for u in units if getattr(u, "on_counter", False))
        if run.slot_geom is None:
            run.slot_geom = {}
            catalog = getattr(self.s, "catalog", None)
            if catalog is not None:
                try:
                    plano = await catalog.get_planogram(run.plan.store_id)
                    run.slot_geom = {
                        s.slot_id: SlotTruth(
                            slot_id=s.slot_id, sku=s.sku, approach_pose=s.approach_pose, item_position=s.item_position
                        )
                        for s in plano.slots
                    }
                except KeyError:
                    pass
        return StateFacts(
            robot_id=run.plan.robot_id,
            pose=st.pose,
            battery_pct=st.battery_pct,
            holding_instance_id=st.holding,
            holding_sku=held_sku,
            basket_counts=dict(Counter(st.basket_skus)),
            counter_counts=dict(counter),
            approvals=dict(run.answers),
            slots=run.slot_geom,
        )

    async def _baseline(self, run: _Run, step: PlanStep) -> VerifyBaseline:
        bid = f"{run.plan.plan_id}:{step.step_id}:a{step.attempts}:baseline"
        verifier = getattr(self.s, "verifier", None)
        if verifier is not None:
            try:
                return await asyncio.wait_for(
                    verifier.baseline(run.plan.robot_id, step.postconditions, baseline_id=bid),
                    timeout=run.policy.degrade.verify_timeout_s,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - fall back to robot state
                pass
        facts = await self._facts(run)
        return VerifyBaseline(
            baseline_id=bid,
            robot_id=run.plan.robot_id,
            basket_counts=facts.basket_counts,
            holding_sku=facts.holding_sku,
            holding_instance_id=facts.holding_instance_id,
            decided_by="state",
        )

    async def _ground(self, run: _Run, step: GroundTargetStep, ctx: ExecContext) -> GroundingResult:
        req = GroundRequest(
            grounding_id=f"{run.plan.plan_id}:{step.step_id}:a{step.attempts}",
            robot_id=run.plan.robot_id,
            target=step.target,
            camera=step.camera,
            context=ctx,
            deadline_s=verify_deadline(run.policy.degrade.verify_timeout_s),
        )
        verifier = getattr(self.s, "verifier", None)
        try:
            if verifier is None:
                raise VerifierUnavailableError("no verifier wired")
            return await asyncio.wait_for(verifier.ground(req), timeout=run.policy.degrade.verify_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - T0 can grasp from the slot alone: ground from the plan
            st = await self.s.require("robot").state(run.plan.robot_id)
            run.metrics.degraded_checks += 1
            tgt = GroundedTarget(
                grounding_id=req.grounding_id,
                target=step.target,
                confidence=0.5,
                decided_by="state",
                provider=ProviderCall(provider="state", call="planogram", error_code=type(exc).__name__),
                robot_id=run.plan.robot_id,
                robot_pose=st.pose,
            )
            return GroundingResult(
                grounding_id=req.grounding_id,
                found=True,
                target=tgt,
                degraded=True,
                reason="verifier unavailable: grounded from the planogram slot (T0)",
            )

    def _expected_basket(self, run: _Run) -> Counter[str]:
        """Units verified into the basket (record_pick ledger) minus the ones dropped off at the checkout mid-run."""
        counts = Counter(
            r.payload.get("sku") for r in run.ledger.values() if r.kind == "record_pick" and r.payload.get("sku")
        )
        counts.subtract(run.unloaded)
        return Counter({sku: n for sku, n in counts.items() if n > 0})

    async def _dropoff_steps(self, run: _Run, grasp: GraspStep) -> list[PlanStep]:
        """The basket is full before a grasp (tacos for 6 is more units than a 12-unit basket): drive to the checkout,
        unload there (staff bag it) and come back, as real VLA steps (navigate / handoff / navigate, each verified).
        The v0.2 brain's drop-off; the audit then expects only the units placed after it. [] = no drop-off needed."""
        robot, catalog = getattr(self.s, "robot", None), getattr(self.s, "catalog", None)
        if robot is None or catalog is None or run.dropoffs >= MAX_DROPOFFS:
            return []
        try:
            st = await robot.state(run.plan.robot_id)
            if st.holding or len(st.basket) < _basket_capacity(run.plan.robot_id):
                return []
            checkout = (await catalog.get_planogram(run.plan.store_id)).checkout
        except Exception as exc:  # noqa: BLE001 - no drop-off: the grasp then fails honestly ('basket full')
            log.warning("basket drop-off check failed: %s", exc)
            return []
        run.dropoffs += 1
        run.unloaded.update(st.basket_skus)
        run.aside_units.clear()  # the whole basket goes to staff here, set-aside units included
        n = run.dropoffs
        loc = grasp.target.location
        back = loc.approach_pose if loc is not None else st.pose
        common: dict[str, Any] = {
            "group_id": "dropoff",
            "group_label": "Basket drop-off",
            "origin": "recovery",
            "inserted_by": grasp.step_id,
        }
        go_pre, go_post = default_conditions("navigate_to", goal_pose=checkout)
        h_pre, h_post = default_conditions("handoff", goal_pose=checkout)
        back_pre, back_post = default_conditions("navigate_to", goal_pose=back)
        steps: list[PlanStep] = [
            NavigateToStep(
                step_id=f"{grasp.step_id}.d{n}.1",
                title=f"Basket full ({len(st.basket)} items): go to the checkout",
                target="checkout",
                goal_pose=checkout,
                preconditions=go_pre,
                postconditions=go_post,
                **common,
            ),
            HandoffStep(
                step_id=f"{grasp.step_id}.d{n}.2",
                title=f"Drop off {len(st.basket)} items at the checkout",
                preconditions=h_pre,
                postconditions=h_post,
                **common,
            ),
            NavigateToStep(
                step_id=f"{grasp.step_id}.d{n}.3",
                title=f"Back to {loc.aisle_label or 'the shelf'}" if loc is not None else "Back to the shelf",
                target="slot" if grasp.target.slot_id else "checkout",
                slot_id=grasp.target.slot_id,
                goal_pose=back,
                preconditions=back_pre,
                postconditions=back_post,
                **common,
            ),
        ]
        await self._mirror(run, grasp, f"basket full ({len(st.basket)} items): dropping it off at the checkout")
        return steps

    def _audit_condition(self, run: _Run) -> Condition:
        if run.mode == "plan":
            counts = self._expected_basket(run)
        else:
            counts = Counter(
                s.sku
                for s in run.plan.steps
                if isinstance(s, PlaceStep)
                and s.destination == "basket"
                and s.status in (StepStatus.SUCCEEDED, StepStatus.PROVISIONAL)
            )
        return BasketContains(counts=dict(counts), note="basket matches what I picked")

    def _capture_cams(self, step: PlanStep) -> list[Any]:
        cams: list[Any] = []
        for _, atom in iter_atoms(step.postconditions):
            for kind in atom_cameras(atom)[:1]:
                if kind not in cams:
                    cams.append(kind)
        return cams

    # =================================================================================================================
    # v0.2 paths: scan, ask, mandate
    # =================================================================================================================
    async def _scan(self, run: _Run, step: ScanShelfStep, attempt: int) -> int:
        plan = run.plan
        robot = self.s.require("robot")
        frame = await robot.scan(plan.robot_id, step.slot_id)
        perception = getattr(self.s, "perception", None)
        if perception is not None:
            blurred = await perception.blur_faces(frame)
            run.scan_frames[step.slot_id] = blurred
            report = await perception.identify(blurred, plan.store_id, step.slot_id, step.expected_sku)
        else:
            units = [u for u in frame.instances if step.expected_sku in (None, u.sku)]
            report = ScanReport(
                scan_id=f"scan-{plan.plan_id}-{step.step_id}-a{attempt}",
                store_id=plan.store_id,
                slot_id=step.slot_id,
                expected_sku=step.expected_sku,
                observed_count=len(units),
                count_is_exact=True,
                frame_id=frame.frame_id,
                camera_id=frame.camera_id,
                detections=[
                    Detection(
                        sku=u.sku, confidence=1.0, method="ground_truth", instance_id=u.instance_id, lot_id=u.lot_id
                    )
                    for u in units
                ],
                lot_counts=dict(Counter(u.lot_id for u in units if u.lot_id)),
            )
        report = report.model_copy(
            update={
                "robot_id": plan.robot_id,
                "goal_id": plan.goal_id if not plan.goal_id.startswith("adhoc-") else None,
                "trip_id": plan.trip_id,
            }
        )
        catalog = getattr(self.s, "catalog", None)
        applied = await catalog.apply_scan(report) if catalog is not None else None
        await self._pub02(run, "scan.result", report=report)
        run.last_scan[step.step_id] = {
            "observed_count": report.observed_count,
            "qty_after": getattr(applied, "qty_after", None),
        }
        return report.observed_count

    async def _ask(
        self,
        run: _Run,
        kind: str,
        question: str,
        options: list[ApprovalOption],
        item: Any,
        step: PlanStep,
        *,
        multi: bool = False,
        on_request: Callable[[ApprovalRequest], Awaitable[None]] | None = None,
    ) -> ApprovalResponse:
        plan = run.plan
        n = len(run.state.attempts)
        amount = (
            (item.price_cents or 0) * item.qty
            if item is not None and kind in ("over_threshold", "recommendation", "substitution", "online_order")
            else None
        )
        req = ApprovalRequest(
            approval_id=f"apr-{plan.plan_id}-{step.step_id}-{n}",
            trip_id=plan.trip_id,
            goal_id=None if plan.goal_id.startswith("adhoc-") else plan.goal_id,
            robot_id=plan.robot_id,
            store_id=plan.store_id,
            user_id=plan.user_id or STAFF_USER_ID,
            kind=ApprovalKind(kind),
            title=question,
            amount_cents=amount,
            sku=item.sku if item is not None else None,
            rec_id=item.rec_id if item is not None else None,
            options=options,
            multi=multi,
        )
        if on_request is not None:
            await on_request(req)
        trips = getattr(self.s, "trips", None)
        approvals = getattr(self.s, "approvals", None)
        try:
            if trips is not None and plan.trip_id:
                resp = await trips.ask(plan.trip_id, req)
            elif approvals is not None:
                resp = await approvals.request(req)
            else:
                resp = await self.answer(req)
        except asyncio.CancelledError:
            # v0.2 rule: a cancelled run withdraws its pending question (no stale card in the web app / iMessage)
            if approvals is not None:
                try:
                    await asyncio.shield(approvals.cancel(req.approval_id, "plan cancelled"))
                except Exception:  # noqa: BLE001 - best effort while unwinding
                    pass
            raise
        run.answers[req.approval_id] = resp.approved
        return resp

    @staticmethod
    def _with_thumbnails(run: _Run, slot_id: str, choice: PickChoice) -> PickChoice:
        """Produce review photos: candidates without a thumbnail get a crop of the last BLURRED scan frame of the
        slot (the unit's pixel box), so the shopper sees the actual units ('Which avocados?')."""
        frame = run.scan_frames.get(slot_id)
        if frame is None or all(c.thumbnail_b64 for c in choice.candidates):
            return choice
        if not getattr(frame, "blurred", False):
            return choice  # section 8: only blurred pixels leave the backend
        boxes = {i.instance_id: i.bbox for i in getattr(frame, "instances", []) if i.bbox is not None}
        try:
            import base64
            import io

            from PIL import Image

            img = Image.open(io.BytesIO(base64.b64decode(frame.jpeg_b64))).convert("RGB")
        except Exception:  # noqa: BLE001 - no Pillow / bad JPEG: options without photos
            return choice
        out = []
        for c in choice.candidates:
            box = c.bbox or boxes.get(c.instance_id)
            if c.thumbnail_b64 or box is None:
                out.append(c)
                continue
            pad = 0.25
            x0, y0 = max(0, int(box.x - box.w * pad)), max(0, int(box.y - box.h * pad))
            x1, y1 = min(img.width, int(box.x + box.w * (1 + pad))), min(img.height, int(box.y + box.h * (1 + pad)))
            if x1 - x0 < 4 or y1 - y0 < 4:
                out.append(c)
                continue
            crop = img.crop((x0, y0, x1, y1))
            crop.thumbnail((160, 160))
            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=80)
            out.append(c.model_copy(update={"thumbnail_b64": base64.b64encode(buf.getvalue()).decode()}))
        return choice.model_copy(update={"candidates": out})

    async def _pick_choice(self, run: _Run, step: AskUserStep, item: Any) -> PickChoice:
        """Freshness top-k for produce: v0.2 ``perception.rank_freshness`` on the last blurred scan frame (thumbnails +
        reasons), else the sim ground truth ranking (deterministic)."""
        k = min(3, PICK_CHOICE_MAX)
        perception = getattr(self.s, "perception", None)
        frame = run.scan_frames.get(item.slot_id)
        if perception is not None and frame is not None:
            try:
                return await perception.rank_freshness(frame, item.slot_id, item.sku, k)
            except Exception:  # noqa: BLE001 - fall back to ground truth below
                pass
        units = await self.s.require("robot").list_instances(run.plan.robot_id, item.slot_id)
        ranked = sorted((u for u in units if u.sku == item.sku), key=lambda u: (-(u.freshness or 0), u.instance_id))[:k]
        return PickChoice(
            choice_id=f"choice-{run.plan.plan_id}-{step.step_id}",
            slot_id=item.slot_id,
            sku=item.sku,
            candidates=[
                FreshnessCandidate(
                    instance_id=u.instance_id, sku=u.sku, score=u.freshness or 0.0, rank=r, reason="sim ground truth"
                )
                for r, u in enumerate(ranked, 1)
            ],
        )

    async def _ask_step(self, run: _Run, step: AskUserStep) -> str:
        item = run.plan.item(step.item_id)
        options: list[ApprovalOption] = [ApprovalOption(option_id=o, label=o) for o in step.options]
        multi = False
        on_request = None
        open_units: list[int] = []
        choice: PickChoice | None = None
        if step.ask_kind == "pick_choice" and item is not None and item.slot_id and item.sku:
            choice = await self._pick_choice(run, step, item)
            choice = self._with_thumbnails(run, item.slot_id, choice)
            options = [
                ApprovalOption(
                    option_id=c.instance_id,
                    label=f"#{c.rank} {_ripeness(c.score)}",
                    detail=(c.reason if c.reason and c.reason != "sim ground truth" else None)
                    or f"freshness {c.score:.2f} (sim ground truth)",
                    thumbnail_b64=c.thumbnail_b64,
                )
                for c in sorted(choice.candidates, key=lambda c: c.rank)
            ]
            open_units = sorted(
                {
                    s.unit or 1
                    for s in run.plan.steps
                    if s.item_id == step.item_id and isinstance(s, GraspStep) and s.status not in CLOSED_STEP_STATUSES
                }
            )
            multi = len(open_units) > 1  # 3 avocados: the shopper may pick one unit per remaining unit

            async def on_request(req: ApprovalRequest) -> None:  # v0.2 event the web app's produce chooser renders
                await self._pub02(run, "pick.choices", choice=choice, approval_id=req.approval_id)

        run.metrics.asks += 1
        resp = await self._ask(
            run, step.ask_kind, step.question, options, item, step, multi=multi, on_request=on_request
        )
        picks = resp.chosen() if step.ask_kind == "pick_choice" and resp.approved else []
        if step.ask_kind == "pick_choice" and not picks and choice is not None and choice.candidates:
            # 0.2.4 produce review: no answer (timeout) -> the robot's best guess, labelled as such
            best = [c.instance_id for c in sorted(choice.candidates, key=lambda c: c.rank)]
            picks = best[: max(1, len(open_units))]
            why = "no answer in time" if resp.decision == "timeout" else "no choice made"
            await self._mirror(run, step, f"{why}: taking #1 freshness pick (robot's best guess)")
        if step.ask_kind == "pick_choice" and picks:
            # chosen unit k -> the k-th remaining unit, on BOTH its ground_target and its grasp (a re-ground keeps it)
            for unit, choice_id in zip(open_units, picks, strict=False):
                for s in run.plan.steps:
                    if (
                        s.item_id == step.item_id
                        and (s.unit or 1) == unit
                        and isinstance(s, GraspStep | GroundTargetStep)
                        and s.status not in CLOSED_STEP_STATUSES
                    ):
                        s.target = s.target.model_copy(update={"instance_id": choice_id})
        if step.ask_kind == "over_threshold" and resp.approved and step.item_id:
            run.line_checked.add(step.item_id)
            run.approvals[step.item_id] = resp.approval_id
            for s in run.plan.steps:
                if s.item_id == step.item_id and isinstance(s, GraspStep):
                    s.approval_id = resp.approval_id
        if not resp.approved:
            if step.on_decline == "fail_goal":
                return "fail_goal"
            if step.on_decline == "skip_item":
                await self._close_item(
                    run,
                    step.item_id,
                    "declined" if step.ask_kind == "recommendation" else "skipped",
                    f"shopper declined ({step.ask_kind})",
                )
                if step.ask_kind == "recommendation":
                    await self._replan_route(run)
        return "next"

    async def _replan_route(self, run: _Run) -> None:
        """A declined optional stop leaves the route: re-solve the trip route like the v0.2 executor does after a
        yes/no (``trips.replan`` stores Trip.route + emits route.planned), so the web map stops drawing a detour
        the robot never drives. The plan keeps its own steps, so the re-solve may keep at most as many optional stops
        as the plan still drives to (a solver-skipped rec never comes back onto the map). Best effort: the map is a
        view, a routing failure never stops the run."""
        trips = getattr(self.s, "trips", None)
        if trips is None or not run.plan.trip_id:
            return
        ahead = sum(
            1
            for i in run.plan.items
            if i.optional
            and i.status not in DONE_ITEM_OK
            and any(s.item_id == i.item_id and s.status not in CLOSED_STEP_STATUSES for s in run.plan.steps)
        )
        try:
            await trips.replan(run.plan.trip_id, include_recommendations=ahead > 0, max_optional_stops=ahead or None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("route re-plan after a declined stop failed for %s: %s", run.plan.trip_id, exc)

    async def _check_line(self, run: _Run, step: GraspStep) -> str:
        plan = run.plan
        item = plan.item(step.item_id)
        if item is None:
            return "ok"
        if step.approval_id:
            run.line_checked.add(item.item_id)
            return "ok"
        trips = getattr(self.s, "trips", None)
        if trips is not None and plan.trip_id:
            line = CartLine(
                line_id=f"line-{item.item_id}",
                sku=item.sku or step.target.sku,
                name=item.name,
                category=item.category or "uncategorized",
                qty=item.qty,
                unit_price_cents=item.price_cents or 0,
                store_id=plan.store_id,
                slot_id=item.slot_id,
            )
            decision = await trips.check_line(plan.trip_id, line)
            verdict = getattr(decision.verdict, "value", decision.verdict)
            if verdict == "deny":
                return "deny"
            if verdict == "needs_approval":
                return "ask"
        elif plan.budget_cents is not None:
            projected = run.state.local_cart_cents + sum(
                (i.price_cents or 0) * (i.qty - i.picked_qty)
                for i in plan.items
                if i.item_id in run.line_checked and i.status != "in_basket"
            )
            if projected + (item.price_cents or 0) * item.qty > plan.budget_cents:
                return "ask"
        run.line_checked.add(item.item_id)
        return "ok"

    def _threshold_question(self, run: _Run, step: GraspStep) -> AskUserStep:
        item = run.plan.item(step.item_id)
        name = (item.name or item.query) if item else step.target.sku
        total = (item.price_cents or 0) * item.qty if item else 0
        return AskUserStep(
            step_id=f"{step.step_id}.ask",
            title=f"Confirm {name}",
            ask_kind="over_threshold",
            question=f"{name} (${total // 100}.{total % 100:02d}) needs your OK (spending rules / budget). Add it?",
            sku=step.target.sku,
            item_id=step.item_id,
            group_id=step.group_id,
            group_label=step.group_label,
            on_decline="skip_item",
            origin="recovery",
            inserted_by=step.step_id,
        )

    # =================================================================================================================
    # misc
    # =================================================================================================================
    # =================================================================================================================
    # 0.2.4 photo verification of every purchased unit
    # =================================================================================================================
    async def _photo(self, run: _Run, att: StepAttempt | None, cameras: list[str]) -> Any:
        """A blurred, retained photo for a verification card: the step's own evidence frames first (already taken
        for the verifier), else a fresh capture through ``services.evidence`` (face-blurred before it is kept)."""
        frames = list(att.post.frames) if att is not None and att.post is not None else []
        for cam in cameras:
            ref = next((f for f in frames if f.camera_id.endswith(f".{cam}") and f.blurred), None)
            if ref is not None:
                return ref
        evidence = getattr(self.s, "evidence", None)
        if evidence is None:
            return None
        try:
            refs = await asyncio.wait_for(
                evidence.capture_refs(run.plan.robot_id, cameras[:1], context={"purpose": "verification_photo"}), 4.0
            )
        except Exception as exc:  # noqa: BLE001 - a card without a photo still asks
            log.debug("verification photo failed: %s", exc)
            return None
        return next((r for r in refs if r.blurred), None)

    async def _verify_item(self, run: _Run, step: GraspStep, att: StepAttempt) -> str:
        """Send the shopper the wrist photo of the unit just grasped ('Is this the right Mild Chunky Salsa?') and wait
        for correct / wrong; no answer within PICKY_VERIFY_AUTO_ACCEPT_S -> 'auto-accepted' (labelled). Beyond
        PICKY_VERIFY_MAX_REJECTIONS 'wrong' answers for one unit, or on a backend without a put-back primitive, a
        'wrong' is noted and the unit kept (staff swap it at the counter). Returns VERIFY_CORRECT / VERIFY_WRONG, or
        _SET_ASIDE (no put-back: the unit goes in the basket uncounted, never charged, and another one is grabbed)."""
        plan = run.plan
        item = plan.item(step.item_id)
        name = (item.name if item is not None else None) or step.target.description or step.target.sku
        key = (step.item_id, step.unit)
        auto_s = max(1.0, float(setting("picky_verify_auto_accept_s", 20.0) or 20.0))
        ref = await self._photo(run, att, ["wrist", "front"])
        held = run.held
        vid = f"ver-{plan.plan_id}-{step.step_id}-a{step.attempts}"
        ver = ItemVerification(
            verification_id=vid,
            trip_id=plan.trip_id,
            goal_id=None if plan.goal_id.startswith("adhoc-") else plan.goal_id,
            robot_id=plan.robot_id,
            store_id=plan.store_id,
            user_id=plan.user_id or STAFF_USER_ID,
            sku=step.target.sku,
            name=name,
            item_id=step.item_id,
            unit=step.unit,
            question=f"Is this the right {name}?",
            photo_url=f"/api/evidence/{ref.frame_id}.jpg" if ref is not None else None,
            frame_id=ref.frame_id if ref is not None else None,
            camera_id=ref.camera_id if ref is not None else None,
            auto_accept_s=auto_s,
            attempt=run.rejections[key] + 1,
        )
        await self._pub(run, "item.verification", verification=ver)
        req = ApprovalRequest(
            approval_id=vid,
            trip_id=plan.trip_id,
            goal_id=ver.goal_id,
            robot_id=plan.robot_id,
            store_id=plan.store_id,
            user_id=ver.user_id,
            kind=ApprovalKind.VERIFY_ITEM,
            title=ver.question,
            sku=step.target.sku,
            options=[
                ApprovalOption(option_id=VERIFY_CORRECT, label="✓ Correct"),
                ApprovalOption(option_id=VERIFY_WRONG, label="✗ Wrong item"),
            ],
            expires_s=auto_s,
            payload={
                "photo_url": ver.photo_url,
                "frame_id": ver.frame_id,
                "instance_id": held.instance_id if held is not None else None,
                "verification": True,
            },
        )
        resp = await self._ask_raw(run, req)
        if resp.decision == "timeout":
            status, by, answer = "auto_accepted", "auto", VERIFY_CORRECT
        elif resp.approved and VERIFY_WRONG not in resp.chosen():
            status, by, answer = "accepted", "user", VERIFY_CORRECT
        else:
            status, by, answer = "rejected", "user", VERIFY_WRONG
        await self._pub(
            run,
            "item.verification",
            verification=ver.model_copy(update={"status": status, "decided_by": by, "decided_at": utcnow()}),
        )
        if answer == VERIFY_WRONG:
            run.rejections[key] += 1
            caps = await self._caps(run)
            t0 = next((c for c in caps if c.tier == "t0_scripted"), None)
            limit = int(setting("picky_verify_max_rejections", 1) or 1)
            if t0 is not None and not t0.place_back and run.rejections[key] <= limit:
                # no put-back (Isaac): the wrong unit rides in the basket uncounted (never charged), another is grabbed
                await self._mirror(run, step, f"marked wrong: {_ASIDE_NOTE}")
                return _SET_ASIDE
            if run.rejections[key] > limit or (t0 is not None and not t0.place_back):
                await self._mirror(run, step, "marked wrong: keeping it for staff to swap at the counter")
                return VERIFY_CORRECT
        else:
            await self._mirror(run, step, f"photo check {status.replace('_', '-')}")
        return answer

    def _set_aside(self, run: _Run, i: int) -> bool:
        """The shopper marked the held unit wrong and it cannot go back on the shelf: its basket place step is flagged
        (record_pick only: no item progress, no cart line) and a fresh unit (ground + grasp + place) follows it.
        False = no plan place step for this unit (the caller keeps the old behaviour)."""
        plan, grasp = run.plan, run.plan.steps[i]
        j = next(
            (
                k
                for k in range(i + 1, len(plan.steps))
                if isinstance(plan.steps[k], PlaceStep)
                and plan.steps[k].item_id == grasp.item_id
                and plan.steps[k].unit == grasp.unit
                and plan.steps[k].destination == "basket"  # type: ignore[union-attr]
                and plan.steps[k].status == StepStatus.PENDING
            ),
            None,
        )
        if j is None:
            return False
        place = plan.steps[j]
        failure = StepFailure(
            plan_id=plan.plan_id,
            step_id=grasp.step_id,
            step_kind=grasp.kind,
            attempt=grasp.attempts,
            kind="postcondition",
            code="wrong_item_grasped",
            message="the shopper says it is the wrong item (photo check): set aside for staff",
            item_id=grasp.item_id,
        )
        redo = self._inserts(run, i, "redo_unit", failure)
        again = place.model_copy(
            update={
                "step_id": recovery_step_id(grasp.step_id, run.recoveries[grasp.step_id], len(redo) + 1),
                "origin": "recovery",
                "inserted_by": grasp.step_id,
                "status": StepStatus.PENDING,
                "attempts": 0,
            }
        )
        run.set_aside.add(place.step_id)
        plan.steps[j + 1 : j + 1] = [*redo, again]
        return True

    async def _not_the_chosen_unit(
        self, run: _Run, i: int, att: StepAttempt, held: ItemInstance, baseline: VerifyBaseline | None
    ) -> str:
        """The gripper holds a unit of the right SKU but not the one the shopper chose: put it back when the backend
        can (then re-ground + re-grasp THAT unit), else set it aside for staff (never counted / charged) and grab the
        chosen unit with the scripted (T0) grasp, which takes an exact instance."""
        step = run.plan.steps[i]
        assert isinstance(step, GraspStep)
        chosen = step.target.instance_id
        caps = await self._caps(run)
        t0 = next((c for c in caps if c.tier == "t0_scripted"), None)
        item = run.plan.item(step.item_id)
        name = (item.name if item is not None else None) or step.target.description or step.target.sku
        log.info("%s: grabbed %s, not the chosen %s", step.step_id, held.instance_id, chosen)
        await self._mirror(run, step, f"grabbed a different {name} than the one you chose", ok=False)
        if t0 is not None and not t0.place_back and self._set_aside(run, i):
            for s in run.plan.steps:
                if s.inserted_by == step.step_id and s.status == StepStatus.PENDING:
                    if isinstance(s, GraspStep | GroundTargetStep):
                        s.target = s.target.model_copy(update={"instance_id": chosen})
                    if isinstance(s, GraspStep):
                        run.next_tier[s.step_id] = "t0_scripted"
            return await self._succeed(run, step, att, note=f"not the one you chose: {_ASIDE_NOTE}; grabbing yours")
        f = StepFailure(
            plan_id=run.plan.plan_id,
            step_id=step.step_id,
            step_kind=step.kind,
            attempt=step.attempts,
            kind="postcondition",
            code="wrong_item_grasped",
            message=f"grabbed a different {name} than the one you chose: putting it back",
            item_id=step.item_id,
        )
        out = await self._recover(run, i, f, att, baseline=baseline)
        run.next_tier[step.step_id] = "t0_scripted"
        for s in run.plan.steps:  # the re-ground after a put-back keeps the shopper's unit
            if s.inserted_by == step.step_id and s.status == StepStatus.PENDING and isinstance(s, GroundTargetStep):
                s.target = s.target.model_copy(update={"instance_id": chosen})
        return out

    async def _counter_card(self, run: _Run, step: PlanStep, att: StepAttempt) -> None:
        """Unload: the photo of the unit on the counter as a verification card (non-blocking: it is bought already;
        auto-accepted after PICKY_VERIFY_AUTO_ACCEPT_S unless the shopper flags it)."""
        if not self._verify_on():
            return
        sku = next(
            (st.target.sku for st in run.plan.steps if st.group_id == step.group_id and isinstance(st, GraspStep)), ""
        )
        item = run.plan.item(step.item_id)
        name = (item.name if item is not None else None) or sku
        ref = await self._photo(run, att, ["front"])
        auto_s = max(1.0, float(setting("picky_verify_auto_accept_s", 20.0) or 20.0))
        aside = step.group_id in run.aside_groups  # flagged, never bought: its photo stays off the purchase rows
        ver = ItemVerification(
            verification_id=f"ver-{run.plan.plan_id}-{step.step_id}",
            trip_id=run.plan.trip_id,
            robot_id=run.plan.robot_id,
            store_id=run.plan.store_id,
            user_id=run.plan.user_id or STAFF_USER_ID,
            sku=sku,
            name=name,
            item_id=step.item_id,
            unit=step.unit,
            question=f"{name} is on the counter: {_ASIDE_NOTE}"
            if aside
            else f"{name} is on the counter — looks right?",
            photo_url=f"/api/evidence/{ref.frame_id}.jpg" if ref is not None else None,
            frame_id=ref.frame_id if ref is not None else None,
            camera_id=ref.camera_id if ref is not None else None,
            status="rejected" if aside else "auto_accepted",
            decided_by="system" if aside else "auto",
            auto_accept_s=auto_s,
            decided_at=utcnow(),
        )
        await self._pub(run, "item.verification", verification=ver)

    def _verify_on(self) -> bool:
        return _verify_items_on() if self.verify_items is None else self.verify_items

    async def _ask_raw(self, run: _Run, req: ApprovalRequest) -> ApprovalResponse:
        """Ask through the trip flow (keeps ``expires_s`` for verify_item), the broker, or the injected answer."""
        trips = getattr(self.s, "trips", None)
        approvals = getattr(self.s, "approvals", None)
        try:
            if trips is not None and run.plan.trip_id:
                resp = await trips.ask(run.plan.trip_id, req)
            elif approvals is not None:
                resp = await approvals.request(req)
            else:
                resp = await self.answer(req)
        except asyncio.CancelledError:
            if approvals is not None:
                try:
                    await asyncio.shield(approvals.cancel(req.approval_id, "plan cancelled"))
                except Exception:  # noqa: BLE001
                    pass
            raise
        run.answers[req.approval_id] = resp.approved
        return resp

    # =================================================================================================================
    # 0.2.4 checkout unload by the arm
    # =================================================================================================================
    async def _unload_gate(self, run: _Run) -> bool:
        """Before the first unload step (0.2.4 checkout unload):

        1. no basket -> counter primitive on this backend (an Isaac worker older than 0.2.4): skip every unload step,
           the handoff unloads the basket at once;
        2. reconcile with the REAL basket: unload groups of a SKU beyond the units actually in it (dropped off mid-trip
           when the basket was full, item skipped / ordered online after the plan was made) are skipped silently;
        3. re-lay the kept units on the counter spots left -> right from spot 1 (``planogram.counter_spots``), renumber
           them 'Unloading k/n', recompute the per-SKU on-counter counts and re-insert the short drives along the
           counter where the next spot is in another bay.
        Units set aside for staff (no put-back) get an unload group of their own first (``_aside_unload_groups``).
        Returns True when nothing is left to unload by arm."""
        plan = run.plan
        caps = await self._caps(run)
        t0 = next((c for c in caps if c.tier == "t0_scripted"), None)
        aside = 0 if t0 is not None and not t0.basket_unload else self._aside_unload_groups(run)
        order: list[str] = []
        for st in plan.steps:
            if _unload_group(st) and st.status not in CLOSED_STEP_STATUSES and st.group_id not in order:
                order.append(st.group_id or "")
        members = {gid: [st for st in plan.steps if st.group_id == gid] for gid in order}

        def skip(gid: str) -> None:
            for st in members[gid]:
                if st.status not in CLOSED_STEP_STATUSES:
                    st.status = StepStatus.SKIPPED

        if t0 is not None and not t0.basket_unload:
            for gid in order:
                skip(gid)
            run.unload_total = 0
            if order:
                await self._mirror(
                    run, members[order[0]][0], "no basket-unload primitive here: the handoff unloads the basket at once"
                )
            return True
        try:
            basket = Counter((await self.s.require("robot").state(plan.robot_id)).basket_skus)
            spots = counter_spots(await self.s.require("catalog").get_planogram(plan.store_id))
        except Exception as exc:  # noqa: BLE001 - keep the compiled layout; the verifier still checks every unit
            log.warning("unload reconcile failed: %s", exc)
            return False
        seen: Counter[str] = Counter()
        keep: list[str] = []
        for gid in order:
            grasp = next((st for st in members[gid] if isinstance(st, GraspStep)), None)
            sku = grasp.target.sku if grasp is not None else ""
            seen[sku] += 1
            if grasp is None or seen[sku] > basket.get(sku, 0) or len(keep) >= len(spots):
                skip(gid)
            else:
                keep.append(gid)
        total = len(keep)
        run.unload_total = total
        if not total:
            return True
        on_counter: Counter[str] = Counter()
        prev = spots[0].approach_pose
        for k, gid in enumerate(keep):
            spot, n = spots[k], k + 1
            run.unload_numbers[gid] = n
            grasp = next(st for st in members[gid] if isinstance(st, GraspStep))
            sku = grasp.target.sku
            on_counter[sku] += 1
            name = (plan.item(grasp.item_id).name if plan.item(grasp.item_id) else None) or sku
            head = f"Unloading {n}/{total}: {name}" + (f" ({_ASIDE_NOTE})" if gid in run.aside_groups else "")
            first: PlanStep | None = None
            for st in members[gid]:
                if isinstance(st, NavigateToStep):  # re-inserted below where the bay changes
                    st.status = StepStatus.SKIPPED
                    continue
                first = first or st
                st.group_label = head
                if isinstance(st, GroundTargetStep):
                    st.title = f"{head}: find it in the basket"
                elif isinstance(st, GraspStep):
                    st.title = f"{head}: grasp it from the basket"
                elif isinstance(st, PlaceStep):
                    st.counter = spot
                    st.title = f"{head} → counter spot {spot.index + 1}"
                    st.preconditions, st.postconditions = default_conditions(
                        "place", sku=sku, destination="counter", counter_count=on_counter[sku]
                    )
                elif isinstance(st, VerifyStep):
                    st.title = f"{head} → counter ✓"
                    st.condition = AllOf(
                        of=[
                            ItemOnCounter(sku=sku, min_count=on_counter[sku], note=f"{name} on the counter"),
                            GripperEmpty(note="not in the gripper"),
                        ]
                    )
            if first is not None and spot.approach_pose.distance_to(prev) > 1e-6:
                pre, post = default_conditions("navigate_to", goal_pose=spot.approach_pose)
                nav = NavigateToStep(
                    step_id=f"{first.step_id}.bay",
                    title=f"{head}: shift along the counter",
                    target="checkout",
                    goal_pose=spot.approach_pose,
                    preconditions=pre,
                    postconditions=post,
                    item_id=first.item_id,
                    unit=first.unit,
                    group_id=gid,
                    group_label=head,
                    optional=True,
                    origin="replan",
                )
                plan.steps.insert(plan.steps.index(first), nav)
            prev = spot.approach_pose
        if aside:  # more units than compiled: the handoff waits at whichever counter bay the last one used
            bays: list[Pose2D] = []
            for spot in spots[:total]:
                if all(spot.approach_pose.distance_to(b) > 1e-6 for b in bays):
                    bays.append(spot.approach_pose)
            for st in plan.steps:
                if (
                    isinstance(st, HandoffStep)
                    and st.group_id == "checkout"
                    and st.status not in CLOSED_STEP_STATUSES
                    and isinstance(st.preconditions, AnyOf)
                ):
                    st.preconditions = AnyOf(of=[RobotAt(pose=b, note="at the counter") for b in bays])
        return False

    def _aside_unload_groups(self, run: _Run) -> int:
        """Units set aside for staff (photo check 'wrong' / not the chosen unit, on a backend without put-back) ride
        in the basket uncounted. Each gets its own unload group (ground in the basket -> grasp -> place on the counter
        -> verify), placed just before the group of the unit that replaced it (basket order: it went in first) and
        pinned to its instance. Only 'unload_to_counter' commits on those steps: no cart line, no total change.
        Returns how many groups were added."""
        plan = run.plan
        if not run.aside_units:
            return 0

        def open_unload() -> list[int]:
            return [k for k, st in enumerate(plan.steps) if _unload_group(st) and st.status not in CLOSED_STEP_STATUSES]

        if not open_unload():
            return 0
        nums = [
            int(gid.split(":", 1)[1])
            for gid in {st.group_id for st in plan.steps if _unload_group(st)}
            if gid and gid.split(":", 1)[1].isdigit()
        ]
        n = max(nums, default=0)
        for a in run.aside_units:
            n += 1
            gid = f"unload:{n}"
            item = plan.item(a.item_id)
            name = (item.name if item is not None else None) or a.sku
            base = next(
                (
                    st.target
                    for st in plan.steps
                    if isinstance(st, GraspStep) and st.item_id == a.item_id and st.target.sku == a.sku
                ),
                None,
            )
            target = (
                base.model_copy(update={"slot_id": None, "location": None, "instance_id": a.instance_id})
                if base is not None
                else TargetSpec(sku=a.sku, description=name, instance_id=a.instance_id)
            )
            head = f"Unloading: {name} ({_ASIDE_NOTE})"
            group: dict[str, Any] = {
                "item_id": a.item_id,
                "unit": a.unit,
                "group_id": gid,
                "group_label": head,
                "optional": True,
                "origin": "replan",
            }
            bkey = f"basket:aside:{a.place_step_id}"
            g_pre, g_post = default_conditions("grasp", sku=a.sku, source="basket")
            p_pre, p_post = default_conditions("place", sku=a.sku, destination="counter")
            steps: list[PlanStep] = [
                GroundTargetStep(
                    step_id=f"{a.place_step_id}.out1",
                    title=f"{head}: find it in the basket",
                    target=target,
                    camera="basket",
                    binding=bkey,
                    postconditions=TargetGrounded(binding=bkey, note="found in the basket"),
                    **group,
                ),
                GraspStep(
                    step_id=f"{a.place_step_id}.out2",
                    title=f"{head}: grasp it from the basket",
                    source="basket",
                    target=target,
                    binding=bkey,
                    instruction=f"take the {name} out of the basket",
                    preconditions=g_pre,
                    postconditions=g_post,
                    **group,
                ),
                PlaceStep(
                    step_id=f"{a.place_step_id}.out3",
                    title=f"{head} → counter",
                    sku=a.sku,
                    destination="counter",
                    instruction=f"put the {name} on the counter",
                    preconditions=p_pre,
                    postconditions=p_post,
                    **group,
                ),
                VerifyStep(
                    step_id=f"{a.place_step_id}.out4",
                    title=f"{head} → counter ✓",
                    condition=AllOf(
                        of=[
                            ItemOnCounter(sku=a.sku, min_count=1, note=f"{name} on the counter"),
                            GripperEmpty(note="not in the gripper"),
                        ]
                    ),
                    purpose="check",
                    **group,
                ),
            ]
            ks = open_unload()
            at = next(
                (
                    k
                    for k in ks
                    if plan.steps[k].item_id == a.item_id
                    and plan.steps[k].unit == a.unit
                    and plan.steps[k].group_id not in run.aside_groups
                ),
                ks[-1] + 1,  # its unit's own unload is gone (skipped item...): after the last unit
            )
            plan.steps[at:at] = steps
            run.aside_groups.add(gid)
        added = len(run.aside_units)
        run.aside_units.clear()
        return added

    async def _skip_unload_group(self, run: _Run, step: PlanStep, reason: str) -> None:
        gid = step.group_id
        for st in run.plan.steps:
            if st.group_id == gid and st.status not in CLOSED_STEP_STATUSES and st is not step:
                st.status = StepStatus.SKIPPED
        if step.status not in CLOSED_STEP_STATUSES:
            step.status = StepStatus.SKIPPED
        await self._unload_event(run, step, "skipped", note=reason)

    async def _unload_decision(
        self, run: _Run, step: PlanStep, failure: StepFailure, decision: RecoveryDecision
    ) -> RecoveryDecision:
        """Unload steps never re-pick from a shelf or close the (already bought) item: one free re-try of the step,
        adopt a unit already in the gripper, else skip this unit's unload (it stays in the basket for the handoff)."""
        if failure.code == "already_holding":
            return decision.model_copy(update={"action": "adopt_held", "reason": "already holding it"})
        if failure.kind == "verifier_uncertain" and step.kind in ("place", "verify"):
            return decision.model_copy(
                update={"action": "accept_provisional", "insert": [], "reason": "counter view unclear: accepted"}
            )
        if failure.code in ("disconnected", "busy", "not_ready") or run.charged[step.step_id] <= 1:
            return decision.model_copy(
                update={"action": "retry", "rerun_failed_step": True, "insert": [], "reason": f"{decision.reason}"}
            )
        return decision.model_copy(
            update={
                "action": "skip_item",
                "rerun_failed_step": False,
                "insert": [],
                "reason": "leaving it for the handoff",
            }
        )

    async def _unload_event(
        self, run: _Run, step: PlanStep, status: str, *, verdict: Verdict | None = None, note: str = ""
    ) -> None:
        """``checkout.unload``: 'Unloading 3/9: Mild Chunky Salsa → counter ✓' for the live page and the plan list."""
        gid = step.group_id or ""
        try:
            n = int(gid.split(":", 1)[1]) if gid.startswith("unload:") else run.unload_total
        except ValueError:
            n = 1
        n = run.unload_numbers.get(gid, n)
        sku = getattr(getattr(step, "target", None), "sku", None) or getattr(step, "sku", None)
        if sku is None:
            for st in run.plan.steps:
                if st.group_id == gid and isinstance(st, GraspStep):
                    sku = st.target.sku
                    break
        name = (run.plan.item(step.item_id).name if run.plan.item(step.item_id) else None) or sku or "item"
        spot = next(
            (
                st.counter.index
                for st in run.plan.steps
                if st.group_id == gid and isinstance(st, PlaceStep) and st.counter is not None
            ),
            None,
        )
        photo = None
        if verdict is not None:
            photo = next((f"/api/evidence/{f.frame_id}.jpg" for f in verdict.frames if f.blurred), None)
        total = run.unload_total
        shown = f"{name} ({_ASIDE_NOTE})" if gid in run.aside_groups else name
        aside = sum(1 for g in run.aside_groups if g in run.unload_numbers)
        texts = {
            "started": f"Unloading {n}/{total}: {shown}",
            "grasped": f"Unloading {n}/{total}: {shown} out of the basket",
            "placed": f"Unloading {n}/{total}: {shown} → counter",
            "verified": f"Unloading {n}/{total}: {shown} → counter ✓",
            "skipped": f"Unloading {n}/{total}: {shown} left in the basket ({note})",
            "failed": f"Unloading {n}/{total}: {shown} failed",
            "done": f"Basket empty: {total} item{'s' if total != 1 else ''} on the counter"
            + (f" ({aside} {_ASIDE_NOTE})" if aside else ""),
        }
        await self._pub(
            run,
            "checkout.unload",
            plan_id=run.plan.plan_id,
            step_id=step.step_id,
            n=max(1, min(n, max(total, 1))) if status != "done" else max(1, total),
            total=total,
            sku=sku or "",
            name=name,
            status=status,
            counter_spot=spot,
            photo_url=photo,
            text=texts.get(status, status),
        )

    async def _caps(self, run: _Run) -> list[VlaCapabilities]:
        if run.caps is None:
            vla = getattr(self.s, "vla", None)
            try:
                run.caps = await vla.capabilities(run.plan.robot_id) if vla is not None else []
            except KeyError:
                run.caps = []
        return run.caps

    async def _held_unit(self, run: _Run) -> ItemInstance | None:
        truth = getattr(self.s, "truth", None)
        if truth is not None:
            try:
                return (await truth.truth(run.plan.robot_id)).holding
            except Exception:  # noqa: BLE001
                pass
        robot = self.s.require("robot")
        st = await robot.state(run.plan.robot_id)
        if not st.holding:
            return None
        return next((u for u in await robot.list_instances(run.plan.robot_id) if u.instance_id == st.holding), None)

    async def _fault_ids(self, run: _Run, step: PlanStep, attempt: int) -> list[str]:
        faults = getattr(self.s, "faults", None)
        if faults is None:
            return []
        key = f"{run.plan.plan_id}:{step.step_id}"
        return [
            f.fault_id for f in await faults.firings() if f.context.step_key == key and f.context.attempt == attempt
        ]

    def _vla_cb(self, run: _Run, step: PlanStep) -> Callable[[VlaStatus], Awaitable[None]]:
        period = 1.0 / self.vla_status_hz if self.vla_status_hz > 0 else 0.0

        async def cb(status: VlaStatus) -> None:
            now = time.monotonic()
            changed = run.last_vla_phase.get(status.exec_id) != status.phase
            last = run.last_vla_emit.get(status.exec_id)
            if status.phase == "running" and not changed and last is not None and now - last < period:
                return
            run.last_vla_emit[status.exec_id] = now
            run.last_vla_phase[status.exec_id] = status.phase
            await self._pub(run, "vla.status", plan_id=run.plan.plan_id, step_id=step.step_id, status=status)

        return cb

    async def _basket_event(self, run: _Run, pending_sku: str | None = None) -> None:
        if not self.emit_v02_events:
            return
        st = await self.s.require("robot").state(run.plan.robot_id)
        expected = self._expected_basket(run)
        if pending_sku:
            expected[pending_sku] += 1
        await self._pub02(run, "basket.verified", check=BasketCheck.compare(expected, Counter(st.basket_skus)))

    async def _finish(self, run: _Run, status: str, summary: str = "") -> PlanOutcome:
        plan = run.plan
        plan.status = {
            "completed": PlanStatus.COMPLETED,
            "failed": PlanStatus.FAILED,
            "cancelled": PlanStatus.CANCELLED,
        }[status]
        run.state.status = plan.status
        run.state.updated_at = utcnow()
        m = run.metrics
        m.steps_total = len(plan.steps)
        m.steps_succeeded = sum(1 for s in plan.steps if s.status == StepStatus.SUCCEEDED)
        m.steps_provisional = sum(1 for s in plan.steps if s.status == StepStatus.PROVISIONAL)
        m.steps_skipped = sum(1 for s in plan.steps if s.status == StepStatus.SKIPPED)
        m.steps_failed = sum(1 for s in plan.steps if s.status == StepStatus.FAILED)
        m.duration_s = round(time.monotonic() - run.t0, 3)
        m.mean_verify_ms = round(sum(run.verify_ms) / len(run.verify_ms), 2) if run.verify_ms else None
        m.ground_truth_agreement = 1.0 if run.verify_ms else None
        if status != "completed":  # cancelled mid-grasp / escalated: report an uncommitted unit left in the gripper
            try:
                held = await self._held_unit(run)
            except Exception:  # noqa: BLE001 - best effort while unwinding
                held = None
            run.state.holding_instance_id = held.instance_id if held is not None else None
        in_basket = [i for i in plan.items if i.status == "in_basket"]
        total = sum(r.payload.get("total_cents", 0) for r in run.ledger.values() if r.kind == "add_cart_line")
        if not summary:
            online = [i for i in plan.items if i.status == "online"]
            # suggested extras the shopper did not take are not "missing" (same count as brain_script.shop_summary)
            want = [i for i in plan.items if not i.optional or i.status == "in_basket"]
            extras = [i for i in plan.items if i.optional and i.status not in ("in_basket", "online")]
            summary = f"Got {len(in_basket)} of {len(want)} items" + (
                f"; {len(online)} ordered online" if online else ""
            )
            if extras:
                summary += f"; {len(extras)} suggested extra{'' if len(extras) == 1 else 's'} skipped"
            summary += (
                f" · {m.verifier_catches} verifier catch{'' if m.verifier_catches == 1 else 'es'}"
                f" · {m.retries} retr{'y' if m.retries == 1 else 'ies'}"
            )
        await self._pub(
            run, "plan.completed", plan_id=plan.plan_id, status=status, summary=summary, stats=m.model_copy()
        )
        return PlanOutcome(
            plan=plan.model_copy(deep=True),
            run=run.state.model_copy(deep=True),
            status=status,
            summary=summary,  # type: ignore[arg-type]
            item_status={i.item_id: i.status for i in plan.items},
            metrics=m.model_copy(),
            total_cents=total,
        )

    async def _pub(self, run: _Run, type_: str, **payload: Any) -> None:
        if getattr(self.s, "events", None) is None:
            return
        await self._publish(make_event(type_, source="agent", **plan_envelope(run.plan), **payload))

    async def _pub02(self, run: _Run, type_: str, **payload: Any) -> None:
        if getattr(self.s, "events", None) is None or not self.emit_v02_events or run.mode != "plan":
            return
        await self._publish(make_event(type_, source="agent", **plan_envelope(run.plan), **payload))

    async def _publish(self, event: Any) -> None:
        """A broken event bus never kills a run (the effects already happened; the event is only the UI's view)."""
        try:
            await self.s.events.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("publish %s failed: %s", getattr(event, "type", "?"), exc)

    async def _mirror(self, run: _Run, step: PlanStep, text: str, *, ok: bool = True) -> None:
        """One ``brain.step(kind='status')`` per step transition, so the v0.2 brain view keeps telling the story."""
        events = getattr(self.s, "events", None)
        if events is None or not self.mirror_brain_steps:
            return
        run.brain_no += 1
        bs = mirror_brain_step(run.plan, step, text, step_no=run.brain_no, ok=ok)
        if self.on_brain_step is not None:
            try:
                self.on_brain_step(bs)
            except Exception as exc:  # noqa: BLE001 - the brain's ring is a view; it never stops a run
                log.warning("on_brain_step hook failed: %s", exc)
        await self._publish(make_event("brain.step", source="agent", **plan_envelope(run.plan), step=bs))


# =====================================================================================================================
# settings + the kill switch (read at call time; PICKY_* fields land in utils.config.Settings with the utils PR)
# =====================================================================================================================
def setting(name: str, default: Any = None) -> Any:
    """``utils.config.settings.<name>``. Until the utils PR adds the 0.2.1 loop fields to ``Settings``, a missing
    field falls back to the process env var of the same name, so the kill switch (PICKY_EXEC_MODE=direct) always
    works."""
    import os

    from utils.config import get_settings

    s = get_settings()
    if name in type(s).model_fields:
        value = getattr(s, name)
        return default if value is None or value == "" else value
    raw = os.environ.get(name.upper())
    return default if raw is None or raw.strip() == "" else raw.strip()


def _float_setting(name: str, default: float) -> float:
    try:
        value = float(setting(name, default))
    except (TypeError, ValueError):
        log.warning("%s is not a number; using %s", name.upper(), default)
        return default
    return value if value > 0 else default


def plan_ready_text(plan: StepPlan) -> str:
    """'Plan ready: 73 steps in 16.4 s by gemini-3.5-flash' (LLM) or '... by the scripted planner (<fallback>)'."""
    info = plan.planner
    took = f" in {info.latency_ms / 1000:.1f} s" if info.latency_ms > 0 else ""
    if info.origin == "llm" and info.model:
        by = info.model
    else:
        by = "the scripted planner" + (f" ({info.fallback_reason})" if info.fallback_reason else "")
    return f"Plan ready: {len(plan.steps)} steps{took} by {by}."


def plan_timeout_from_settings() -> float:
    """PICKY_PLAN_TIMEOUT_S (default 20 s): the LLM draft call and the runner's replan call."""
    return _float_setting("picky_plan_timeout_s", DEFAULT_PLAN_TIMEOUT_S)


def policy_from_settings(base: RetryPolicy = DEFAULT_RETRY_POLICY) -> RetryPolicy:
    """DEFAULT_RETRY_POLICY with PICKY_VERIFY_TIMEOUT_S applied (the verifier then gets deadline_s = that - 1 s)."""
    timeout = _float_setting("picky_verify_timeout_s", base.degrade.verify_timeout_s)
    if timeout == base.degrade.verify_timeout_s:
        return base
    return base.model_copy(update={"degrade": base.degrade.model_copy(update={"verify_timeout_s": timeout})})


def configured_exec_mode(services: Any) -> str:
    """PICKY_EXEC_MODE as configured: 'direct' | 'plan' | 'tools'. Unset / 'auto' = 'plan' when the VLA and the
    verifier are wired (the lead's default), else 'direct'."""
    raw = str(setting("picky_exec_mode", "auto")).strip().lower()
    if raw in ("", "auto"):
        wired = getattr(services, "vla", None) is not None and getattr(services, "verifier", None) is not None
        return "plan" if wired else "direct"
    return raw


def resolve_exec_mode(services: Any) -> ExecMode:
    """THE switch the brain consults per goal: ``contracts.services.exec_mode`` over ``configured_exec_mode`` (plan /
    tools only when planner, runner, vla and verifier are all wired; anything else is 'direct' = exact v0.2)."""
    return exec_mode(services, configured_exec_mode(services))


def build_runner(services: Any, **kw: Any) -> PlanLoop:
    """The runner with settings applied (policy verify timeout, plan timeout). Same class for real and fake."""
    kw.setdefault("policy", policy_from_settings())
    kw.setdefault("plan_timeout_s", plan_timeout_from_settings())
    return PlanLoop(services, **kw)
