"""0.2.1 loop contracts: Protocols + optional Services fields + kill switch, run_vla_to_terminal safety nets, the
merged event union (one producer per type, envelopes, live-only overlay), the bridge and the version policy."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

import contracts
from contracts.bridge import (
    STEP_TO_BRAIN_TOOL,
    ToolStepOutcome,
    goal_update_from_outcome,
    mirror_brain_step,
    pick_result_from,
    place_result_from,
)
from contracts.common import Point3, Pose2D
from contracts.events import (
    EVENT_CLASSES,
    EVENT_PRODUCERS,
    EVENT_TYPES,
    LIVE_ONLY_EVENT_TYPES,
    LOOP_EVENT_TYPES,
    LOOP_P0_EVENT_TYPES,
    THROTTLED_EVENT_TYPES,
    CameraOverlay,
    PlanCreated,
    StepFailed,
    event_json_schema,
    event_matches,
    goal_envelope,
    make_event,
    parse_event,
    plan_envelope,
)
from contracts.evidence import EvidenceCameras, FrameRef, GroundTruthSidecar, TruthSnapshot, TruthSource
from contracts.faults import FaultFiring, FaultRule, FaultScenario
from contracts.fleet import Goal, GoalStatus
from contracts.plan import GraspStep, PlaceStep, StepPlan
from contracts.policy import (
    DEFAULT_RETRY_POLICY,
    PlanOutcome,
    PlanRunState,
    RecoveryContext,
    StepAttempt,
    StepFailure,
    decide_recovery,
)
from contracts.services import (
    LOOP_CORE_FIELDS,
    LOOP_SERVICE_FACTORIES,
    LOOP_SERVICE_PROTOCOLS,
    SERVICE_FACTORIES,
    FaultInjector,
    LoopRunner,
    PlannerService,
    Services,
    VerifierService,
    VisionProvider,
    VlaService,
    exec_mode,
    run_vla_to_terminal,
)
from contracts.sim import CameraFrame, ItemInstance, versions_compatible
from contracts.verify import Verdict
from contracts.vla import ExecRecord, VlaError, VlaRequest, VlaStatus

ROBOT = "rover-ave-1"
STORE = "store_ave"


def _plan(**kw: Any) -> StepPlan:
    base: dict[str, Any] = {
        "plan_id": "plan-g1",
        "root_plan_id": "plan-g1",
        "goal_id": "g1",
        "robot_id": ROBOT,
        "store_id": STORE,
        "trip_id": "trip_1",
        "user_id": "maya",
        "request_id": "req_1",
    }
    base.update(kw)
    return StepPlan(**base)


# ---- Protocols + Services ----------------------------------------------------------------------------------------
class _Planner:
    async def plan(self, request): ...
    async def compile(self, draft, request, *, items, plan_id=None): ...
    async def replan(self, request): ...
    async def expand_tool_call(self, plan, tool_name, tool_input): ...
    def validate(self, plan): ...


class _Vla:
    async def capabilities(self, robot_id): ...

    async def execute(self, request, *, after_seq=0):
        yield VlaStatus(exec_id=request.exec_id, robot_id=request.robot_id, seq=1, phase="accepted")

    async def get_exec(self, exec_id): ...
    async def cancel(self, exec_id, reason=""): ...
    async def cancel_robot(self, robot_id, reason=""): ...


class _Vision:
    name = "fake"
    model = None

    async def detect(self, frame, classes, *, timeout_s=None): ...
    async def point(self, frame, query, *, timeout_s=None): ...
    async def question(self, frame, question, expects="text", *, timeout_s=None): ...
    async def ocr(self, frame, prompt=None, *, timeout_s=None): ...
    async def compare(self, before, after, question, expects="box", *, timeout_s=None): ...
    async def multilook(self, frame, prompts, *, timeout_s=None): ...


class _Verifier:
    async def verify(self, request): ...
    async def ground(self, request): ...
    async def baseline(self, robot_id, condition, *, baseline_id): ...
    async def capabilities(self): ...
    async def set_mode(self, mode): ...


class _Faults:
    async def load_scenario(self, scenario): ...
    async def inject(self, rule): ...
    async def clear_faults(self): ...
    async def firings(self): ...
    async def consult(self, ctx, effects): ...


class _Runner:
    async def run_goal(self, robot_id, goal): ...
    async def run_plan(self, plan, *, policy=None): ...
    async def run_tool_step(self, robot_id, goal, tool_name, tool_input): ...
    async def cancel(self, plan_id, reason=""): ...
    async def get_plan(self, plan_id): ...
    async def current_plan(self, goal_id): ...
    async def run_state(self, plan_id): ...
    async def attempts(self, plan_id, step_id=None): ...


class _Evidence:
    async def capture_refs(self, robot_id, cameras, *, not_before=None, context=None, aim_slot_id=None): ...
    async def get_frame(self, frame_id): ...


class _Truth:
    async def truth(self, robot_id, slot_ids=()): ...
    async def ground_truth(self, frame_id): ...


def test_loop_protocols_are_runtime_checkable_and_conform() -> None:
    impls = {
        "planner": _Planner(),
        "runner": _Runner(),
        "vla": _Vla(),
        "verifier": _Verifier(),
        "truth": _Truth(),
        "evidence": _Evidence(),
        "faults": _Faults(),
    }
    assert set(impls) == set(LOOP_SERVICE_PROTOCOLS)
    for name, proto in LOOP_SERVICE_PROTOCOLS.items():
        assert getattr(proto, "_is_runtime_protocol", False), name
        assert isinstance(impls[name], proto), name
    assert isinstance(_Vision(), VisionProvider)
    assert not isinstance(_Verifier(), VlaService) and not isinstance(object(), PlannerService)
    assert LOOP_SERVICE_PROTOCOLS["vla"] is VlaService and LOOP_SERVICE_PROTOCOLS["verifier"] is VerifierService
    assert (LOOP_SERVICE_PROTOCOLS["runner"], LOOP_SERVICE_PROTOCOLS["faults"]) == (LoopRunner, FaultInjector)
    assert (LOOP_SERVICE_PROTOCOLS["evidence"], LOOP_SERVICE_PROTOCOLS["truth"]) == (EvidenceCameras, TruthSource)


def test_loop_fields_are_optional_and_registered_apart_from_v02_factories() -> None:
    s = Services()
    for name in LOOP_SERVICE_PROTOCOLS:
        assert name in Services.field_names() and getattr(s, name) is None
    assert set(LOOP_SERVICE_FACTORIES) == set(LOOP_SERVICE_PROTOCOLS)
    assert not set(LOOP_SERVICE_FACTORIES) & set(SERVICE_FACTORIES)  # the gateway registry is unchanged until wired
    assert LOOP_SERVICE_FACTORIES["planner"] == ("agent", "create_planner", "create_planner_fake")
    assert LOOP_SERVICE_FACTORIES["vla"][0] == "robot" and LOOP_SERVICE_FACTORIES["verifier"][0] == "perception"
    assert {arm for arm, _, _ in LOOP_SERVICE_FACTORIES.values()} == {"agent", "robot", "perception"}
    assert set(LOOP_CORE_FIELDS) == {"planner", "runner", "vla", "verifier"}


def test_exec_mode_double_kill_switch() -> None:
    assert exec_mode(Services(), "plan") == "direct"
    wired = Services(planner=_Planner(), runner=_Runner(), vla=_Vla(), verifier=_Verifier())  # type: ignore[arg-type]
    assert exec_mode(wired, None) == "direct"
    assert exec_mode(wired, "garbage") == "direct"
    assert exec_mode(wired, "direct") == "direct"
    assert exec_mode(wired, "plan") == "plan" and exec_mode(wired, "tools") == "tools"
    half = Services(planner=_Planner(), runner=_Runner(), vla=_Vla())  # type: ignore[arg-type]
    assert exec_mode(half, "plan") == "direct"


# ---- run_vla_to_terminal ------------------------------------------------------------------------------------------
def _req(deadline_s: float = 1.0, heartbeat_s: float = 0.05) -> VlaRequest:
    step = PlaceStep(step_id="s04", title="Basket", sku="SALSA-MILD")
    return VlaRequest(exec_id="p:s04:a1", robot_id="r", step=step, deadline_s=deadline_s, heartbeat_s=heartbeat_s)


class _SlowStreamVla:
    """One status, then silence; cancel works."""

    def __init__(self) -> None:
        self.cancels = 0

    async def execute(self, req: VlaRequest, *, after_seq: int = 0) -> AsyncIterator[VlaStatus]:
        yield VlaStatus(exec_id=req.exec_id, robot_id=req.robot_id, seq=1, phase="accepted", tier="t0_scripted")
        await asyncio.sleep(5)

    async def cancel(self, exec_id: str, reason: str = "") -> VlaStatus:
        self.cancels += 1
        return VlaStatus(exec_id=exec_id, robot_id="r", seq=2, phase="cancelled", error=VlaError(code="cancelled"))

    async def get_exec(self, exec_id: str) -> Any:
        raise KeyError(exec_id)


async def test_deadline_cannot_escape_when_on_status_lags() -> None:
    vla = _SlowStreamVla()
    seen: list[str] = []

    async def slow_publish(s: VlaStatus) -> None:
        seen.append(s.phase)
        await asyncio.sleep(0.4)

    terminal = await run_vla_to_terminal(vla, _req(deadline_s=0.1), on_status=slow_publish)  # type: ignore[arg-type]
    assert terminal.phase == "failed" and terminal.error is not None and terminal.error.code == "deadline_exceeded"
    assert vla.cancels == 1 and seen == ["accepted", "failed"]  # observers never see an exec stuck at 'accepted'


async def test_stall_is_cancelled_and_synthesized() -> None:
    vla = _SlowStreamVla()
    terminal = await run_vla_to_terminal(vla, _req(deadline_s=5.0, heartbeat_s=0.02))  # type: ignore[arg-type]
    assert terminal.error is not None and terminal.error.code == "deadline_exceeded"
    assert terminal.error.details == {"stalled": True} and terminal.seq == 3 and vla.cancels == 1


class _DroppingVla:
    """Drops the stream after 'running'; the exec finished meanwhile (re-attach via get_exec, no second motion)."""

    def __init__(self) -> None:
        self.executes = 0

    def _statuses(self, req: VlaRequest) -> list[VlaStatus]:
        return [
            VlaStatus(exec_id=req.exec_id, robot_id="r", seq=1, phase="accepted", tier="t0_scripted"),
            VlaStatus(exec_id=req.exec_id, robot_id="r", seq=2, phase="running", progress=0.5),
            VlaStatus(exec_id=req.exec_id, robot_id="r", seq=3, phase="succeeded"),
        ]

    async def execute(self, req: VlaRequest, *, after_seq: int = 0) -> AsyncIterator[VlaStatus]:
        self.executes += 1
        self.req = req
        for s in self._statuses(req)[:2]:
            if s.seq > after_seq:
                yield s

    async def get_exec(self, exec_id: str) -> ExecRecord:
        req = self.req
        return ExecRecord(exec_id=exec_id, body_hash=req.body_hash, request=req, statuses=self._statuses(req))

    async def cancel(self, exec_id: str, reason: str = "") -> VlaStatus:
        raise AssertionError("must not cancel a finished exec")


async def test_dropped_stream_recovers_the_terminal_without_a_second_motion() -> None:
    vla = _DroppingVla()
    seqs: list[int] = []

    async def record(s: VlaStatus) -> None:
        seqs.append(s.seq)

    terminal = await run_vla_to_terminal(vla, _req(), on_status=record)  # type: ignore[arg-type]
    assert terminal.phase == "succeeded" and seqs == [1, 2, 3] and vla.executes == 1


async def test_caller_cancel_cancels_the_exec() -> None:
    vla = _SlowStreamVla()
    task = asyncio.create_task(run_vla_to_terminal(vla, _req(deadline_s=5.0, heartbeat_s=1.0)))  # type: ignore[arg-type]
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert vla.cancels == 1


# ---- events -----------------------------------------------------------------------------------------------------
def test_loop_events_are_in_the_union_with_one_producer_each() -> None:
    assert len(LOOP_EVENT_TYPES) == 12 and set(LOOP_P0_EVENT_TYPES) <= set(LOOP_EVENT_TYPES)
    assert set(LOOP_EVENT_TYPES) <= set(EVENT_TYPES) and set(EVENT_PRODUCERS) == set(EVENT_TYPES)
    assert {EVENT_PRODUCERS[t] for t in LOOP_EVENT_TYPES if t != "demo.fault"} == {"agent"}
    assert EVENT_PRODUCERS["demo.fault"] == "robot" and EVENT_PRODUCERS["brain.step"] == "agent"
    assert len(EVENT_CLASSES) == len(EVENT_TYPES) == 50
    assert "camera.overlay" in LIVE_ONLY_EVENT_TYPES and THROTTLED_EVENT_TYPES == {"vla.status": 2.0}
    schema = json.dumps(event_json_schema())
    for t in LOOP_EVENT_TYPES:
        assert t in schema, t


def test_plan_envelope_matches_goal_envelope_and_filters() -> None:
    plan = _plan()
    goal = Goal(
        goal_id="g1",
        kind="shop",
        store_id=STORE,
        robot_id=ROBOT,
        title="tacos",
        user_id="maya",
        trip_id="trip_1",
        request_id="req_1",
    )
    assert plan_envelope(plan) == goal_envelope(goal)
    ev = make_event("plan.created", **plan_envelope(plan), plan=plan, source="agent")
    assert isinstance(ev, PlanCreated) and ev.trip_id == "trip_1"
    assert event_matches(ev, {"trip_id": "trip_1"}) and event_matches(ev, {"request_id": ["req_1"]})
    assert not event_matches(ev, {"robot_id": "rover-ave-2"})
    back = parse_event(ev.model_dump_json())
    assert isinstance(back, PlanCreated) and back.plan.plan_id == "plan-g1"
    assert plan_envelope(plan, robot_id="rover-ave-2")["robot_id"] == "rover-ave-2"


def test_step_failed_and_overlay_round_trip() -> None:
    failure = StepFailure(
        plan_id="plan-g1",
        step_id="s05",
        step_kind="grasp",
        attempt=1,
        kind="postcondition",
        code="atom:gripper_holding",
        message="gripper is empty",
        vla_claimed_success=True,
        caught_by_verifier=True,
    )
    decision = decide_recovery(DEFAULT_RETRY_POLICY, RecoveryContext(failure=failure))
    ev = make_event(
        "step.failed", **plan_envelope(_plan()), plan_id="plan-g1", step_id="s05", failure=failure, decision=decision
    )
    back = parse_event(ev.model_dump(mode="json"))
    assert isinstance(back, StepFailed) and back.decision.action == "reground"
    assert back.decision.reason.startswith("Verifier caught it")
    ref = FrameRef(frame_id="rover-ave-1.wrist:000001", camera_id="rover-ave-1.wrist", robot_id=ROBOT)
    ov = make_event("camera.overlay", trip_id=None, camera_id=ref.camera_id, frame=ref, label="grasp check")
    assert isinstance(parse_event(ov.model_dump_json()), CameraOverlay) and ov.type in LIVE_ONLY_EVENT_TYPES
    fault = make_event(
        "demo.fault",
        trip_id=None,
        robot_id=ROBOT,
        firing=FaultFiring(fault_id="f", effect="grasp_miss", context={"robot_id": ROBOT}),
        source="robot",
    )
    assert parse_event(fault.model_dump()).type == "demo.fault"


# ---- bridge -----------------------------------------------------------------------------------------------------
def test_mirror_brain_step_keeps_the_v02_thinking_log() -> None:
    plan = _plan()
    target = {"sku": "S", "description": "salsa"}
    step = GraspStep(step_id="s05", title="Grab salsa (1/1)", target=target, instruction="i")  # type: ignore[arg-type]
    bs = mirror_brain_step(plan, step, "verified", step_no=3)
    assert (bs.kind, bs.tool, bs.goal_id, bs.trip_id, bs.step_no, bs.model) == (
        "status",
        "pick_item",
        "g1",
        "trip_1",
        3,
        "loop",
    )
    assert bs.text == "Grab salsa (1/1): verified" and bs.step_id == "plan-g1:s05:0:3"
    adhoc = mirror_brain_step(_plan(goal_id="adhoc-abc"), step, "x", step_no=1, ok=False)
    assert adhoc.goal_id is None and adhoc.ok is False
    ev = make_event("brain.step", **plan_envelope(plan), step=bs, source="agent")
    assert parse_event(ev.model_dump_json()).type == "brain.step"
    assert set(STEP_TO_BRAIN_TOOL) == {
        "navigate_to",
        "align",
        "ground_target",
        "grasp",
        "place",
        "verify",
        "scan_shelf",
        "ask_user",
        "handoff",
    }


def test_goal_update_from_outcome_never_owns_cancel() -> None:
    plan = _plan()
    run = PlanRunState(plan_id="plan-g1", root_plan_id="plan-g1", robot_id=ROBOT)
    done = goal_update_from_outcome(PlanOutcome(plan=plan, run=run, status="completed", summary="ok"))
    assert done.status == GoalStatus.COMPLETED and done.progress == 1.0
    cancelled = goal_update_from_outcome(PlanOutcome(plan=plan, run=run, status="cancelled"))
    assert cancelled.status is None
    failure = StepFailure(
        plan_id="plan-g1", step_id="s1", step_kind="navigate_to", attempt=3, kind="vla_error", code="nav_timeout"
    )
    attempt = StepAttempt(plan_id="plan-g1", step_id="s1", attempt=3, failure=failure)
    failed_run = run.model_copy(update={"attempts": [attempt]})
    failed = goal_update_from_outcome(PlanOutcome(plan=plan, run=failed_run, status="failed"))
    assert failed.status == GoalStatus.FAILED and failed.failure_code == "timeout"


def test_tool_outcomes_map_to_v02_results() -> None:
    unit = ItemInstance(instance_id="S1#01", sku="SALSA-MILD", position=Point3(x=0, y=0, z=0))
    assert pick_result_from(ToolStepOutcome(tool="pick_item", ok=True, holding=unit, verified=True)).picked
    assert place_result_from(ToolStepOutcome(tool="place_in_basket", ok=True, verified=None)).basket_ok is None
    assert place_result_from(ToolStepOutcome(tool="place_in_basket", ok=False)).basket_ok is False


# ---- evidence / privacy / version ----------------------------------------------------------------------------------
def test_truth_sidecar_and_frames() -> None:
    truth = TruthSnapshot(robot_id=ROBOT, store_id=STORE, pose=Pose2D(x=0, y=0))
    side = GroundTruthSidecar(frame_id="f", camera_id="rover-ave-1.front", truth=truth)
    assert side.synthetic and truth.truth_hash() == truth.model_copy(update={"approvals": {"a": True}}).truth_hash()
    frame = CameraFrame(frame_id="f", jpeg_b64="AAAA", w=640, h=480, blurred=False)
    assert frame.blurred is False  # VisionProvider implementations must reject this frame (ValueError)


def test_version_is_a_patch_bump_the_deployed_isaac_worker_accepts() -> None:
    assert contracts.CONTRACTS_VERSION == "0.2.2"
    assert versions_compatible(contracts.CONTRACTS_VERSION, "0.2.0")  # the worker on vhagar (0.2.0) keeps working
    assert not versions_compatible("0.3.0", "0.2.0")  # T1 wire (RunSkillCmd) waits for 0.3.0 + a worker rebuild
    for name in ("StepPlan", "VlaService", "VerifierService", "PlannerService", "Services", "plan_envelope"):
        assert hasattr(contracts, name) and name in contracts.__all__


def test_fault_scenarios_round_trip() -> None:
    sc = FaultScenario(name="x", description="d", rules=[FaultRule(fault_id="f", effect="drop_after_lift")])
    assert FaultScenario.model_validate_json(sc.model_dump_json()) == sc
    v = Verdict(verify_id="v", robot_id=ROBOT, outcome="pass", confidence=1.0, reason="ok")
    assert v.passed and v.evidence == []
