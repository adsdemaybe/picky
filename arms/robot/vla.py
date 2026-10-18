"""T0Vla: the robot arm's ``contracts.services.VlaService`` (``Services.vla``): VLA steps as streamed executions over
the RobotService the gateway wired, so the SAME code drives the 2D fake and Isaac (``SimRobot`` routes each store).

    execute(VlaRequest) ═▶ accepted ▸ running (stages + heartbeats every heartbeat_s) ▸ ONE terminal
                           succeeded (a CLAIM + VlaOutcome) | failed (typed VlaError) | cancelled

T0 ("scripted / oracle") mapping, one RobotService motion per step:

    navigate_to  routed path (services.routing leg from the current pose; step.waypoints win) -> robot.navigate;
                 progress = arc length along that path from robot.state; a no-op when already at the goal pose
    align        small robot.navigate to the slot's approach pose (routed when far); 'lift_preposition' and
                 'aiming_camera' are UI stages (v0.2 moves the lift inside pick; the head is aimed by evidence scans)
    grasp        reach pre-check (contracts.common.slot_reachable on the live planogram: not_at_pose / out_of_reach
                 BEFORE any motion) -> robot.pick(slot, sku, grounding.instance_id or target.instance_id) (magic grasp)
    place        basket: robot.place_in_basket (empty gripper -> place_failed) · shelf (put back): FakeRobot /
                 SimRobot-on-the-fake ``put_back`` or a virtual unit from the overlay; Isaac -> unsupported_step
    handoff      robot.unload_basket (0.2.4: after the per-unit unload it only bags what is on the counter)
    grasp source='basket' (0.2.4)      robot.pick_from_basket(sku, grounding.instance_id): the arm reaches back into the
                 basket at the checkout counter (T0 always: the learned T1 policies are trained on shelf grasps)
    place destination='counter' (0.2.4) robot.place_on_counter(spot.position): reach check against the spot's
                 approach pose (slot_reachable), then release on the counter (``on_counter``)

Contract rules kept here: ``exec_id`` is idempotent (same body = re-attach + replay after ``after_seq``, never a
second motion; another body = one failed ``idempotency_conflict``); one running exec per robot (else failed ``busy`` at
once); no hidden retries; ``deadline_s`` enforced (``deadline_exceeded`` + robot.stop); ``cancel`` = robot.stop and
the exec's terminal status, idempotent; RobotError -> ``VlaError.from_sim``; ``VlaOutcome.ended_at`` = gateway clock
right after the RobotService call returned; ``capture_after`` frames come from ``services.evidence`` with the front
camera aimed at the step's slot; execs are kept >= 1 h. Publishes nothing (the runner owns events).

Faults (``services.faults``, layer 'vla' + 'mislabel'): drop_after_lift (grasp CLAIMS success, nothing attached),
grasp_miss (honest grasp_missed), wrong_item (2D fake: really grabs the neighbouring SKU; Isaac: overlay), place_miss
(claims a place, still holding), nav_blocked, vla_hang (silence until stalled / deadline), policy_unavailable (T1
refuses at accept -> T0 in the same exec), disconnect, mislabel (the held unit's label reads ``params.text``).

Pictures while moving (every tier, every backend): while ``navigate_to`` / ``align`` drive, ``_nav_camera`` takes a
front-camera picture every 1 / PICKY_VLA_NAV_CAPTURE_HZ s (default 2 Hz) through ``services.evidence`` (face-blurred
+ retained: each one is a ``FrameRef`` at /api/evidence/{id}.jpg; ``ex.nav_frames``) and reports it in a running
status ('driving', ``message`` names the frame). On the approach (<= PICKY_VLA_NAV_LOOK_M of path left) and once
more on arrival with the head aimed at the slot (unless the first saw it; Isaac stores by default: PICKY_VLA_NAV_LOOK =
isaac | always | off) one blurred picture goes to Perceptron Mk1.5 ("is <product> visible?",
``arms.robot.vla_mk15.look``, per-trip budget PICKY_PERCEPTRON_CALL_BUDGET) and the answer is a running status
with stage 'observing' (the runner shows it; a brain.step mirror of 'observing' statuses belongs to the agent arm).

T1: ``arms.robot.t1.T1Adapter`` (unavailable unless configured, then T0 takes over). With a learned policy
(PICKY_VLA_T1=perceptron|smolvla|scripted) a grasp resolved to T1 runs ``arms.robot.vla_t1.run_policy_grasp``: the
policy drives the SO-101 joint by joint on the Isaac worker (contracts 0.2.2 ``ArmJointsCmd``) until the sticky grasp
attaches a unit, ``PICKY_VLA_T1_TIMEOUT_S`` / ``_MAX_CHUNKS`` run out (``policy_timeout`` / ``grasp_missed``) or the
policy fails (``policy_unavailable`` / ``policy_diverged``). One running status per action chunk (``chunk_no``). No
fallback mid-run: the runner's RetryPolicy downgrades to T0 (``policy`` category -> downgrade_tier).
"""

import asyncio
import base64
import contextlib
import math
import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

from arms.robot.faults import FaultOverlay
from arms.robot.t1 import T1Adapter
from arms.robot.world import live_planogram
from contracts.common import Pose2D, RobotId, angle_diff, slot_reachable, utcnow
from contracts.evidence import FrameRef
from contracts.faults import FaultContext, FaultEffect
from contracts.plan import ExecId, StepKind, VlaTier
from contracts.planogram import Slot
from contracts.routing import RouteRequest
from contracts.services import Services
from contracts.sim import ItemInstance, RobotError, RobotState
from contracts.vla import (
    ExecRecord,
    VlaCapabilities,
    VlaError,
    VlaErrorCode,
    VlaOutcome,
    VlaRequest,
    VlaStatus,
    resolve_tier,
)
from utils.log import get_logger

log = get_logger(__name__)

T0: VlaTier = "t0_scripted"
T0_KINDS: list[StepKind] = ["navigate_to", "align", "grasp", "place", "handoff"]
VLA_EFFECTS: dict[str, list[FaultEffect]] = {
    "navigate_to": ["nav_blocked", "disconnect", "vla_hang"],
    "align": ["disconnect", "vla_hang"],
    "grasp": ["drop_after_lift", "grasp_miss", "wrong_item", "mislabel", "disconnect", "vla_hang"],
    "place": ["place_miss", "disconnect", "vla_hang"],
    "handoff": ["vla_hang"],
}
EXEC_RETENTION_S = 3600.0
"""Exec records are kept at least this long (idempotent re-attach + audit), pruned on insert."""
EVIDENCE_TIMEOUT_S = 5.0
"""capture_after is best effort: the verifier captures by itself when the VLA brought no frames."""
ALIGN_TOLERANCE_M = 0.02
ALIGN_YAW_TOLERANCE_RAD = 0.05
ROUTED_ALIGN_M = 0.5
"""An align further than this from the approach pose drives a routed path (a straight line may cross a shelf)."""
NOT_AT_POSE_M = 0.5
"""grasp: further than this from the slot's approach pose = not_at_pose (else out_of_reach)."""
DEFAULT_SPEED_MPS = 0.8
DEFAULT_NAV_CAPTURE_HZ = 2.0
"""Front-camera pictures per second while driving (PICKY_VLA_NAV_CAPTURE_HZ; 0 = off)."""
NAV_LOOKS_PER_EXEC = 2
"""Perceptron look-outs per drive: one while approaching, one on arrival (head aimed at the slot) unless the first
already saw the product."""
ARRIVAL_LOOK_TIMEOUT_S = 6.0
DEFAULT_NAV_LOOK_M = 2.5
NAV_FRAMES_KEPT = 240
CAPTURE_TIMEOUT_S = 3.0


def _setting(name: str, default: Any) -> Any:
    try:
        from utils.config import get_settings

        value = getattr(get_settings(), name, None)
    except Exception:  # noqa: BLE001 - config errors must not take the robot down
        return default
    return default if value is None or value == "" else value


class _Fail(Exception):
    def __init__(self, code: VlaErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: VlaErrorCode = code
        self.message = message


class _Exec:
    def __init__(self, request: VlaRequest) -> None:
        self.request = request
        self.record = ExecRecord(exec_id=request.exec_id, body_hash=request.body_hash, request=request)
        self.cond = asyncio.Condition()
        self.task: asyncio.Task[None] | None = None
        self.tier: VlaTier | None = None
        self.backend: Any = None
        self.stage = "starting"
        self.progress = 0.0
        self.chunk = 0
        self.silent = False
        self.cancelling = False
        self.cancel_reason = ""
        self.ended_at: Any = None
        self.path: list[Pose2D] = []
        self.path_len = 0.0
        self.fault_ids: list[str] = []
        self.accepted_mono = 0.0
        self.self_report: Any = "success"
        self.nav_frames: list[FrameRef] = []
        """Front-camera pictures taken while driving (blurred, retained evidence)."""
        self.observations: list[str] = []
        """Perceptron look-out answers while driving."""
        self.nav_target: str | None = None
        self.nav_looks = 0


def _swallow(task: "asyncio.Task[Any]") -> None:
    if not task.cancelled() and task.exception() is not None:
        log.debug("background task %s ended: %s", task.get_name(), task.exception())


def path_progress(path: list[Pose2D], pose: Pose2D) -> tuple[float, float]:
    """(fraction travelled, remaining meters) of ``pose`` projected on the polyline ``path``."""
    segs = list(zip(path, path[1:], strict=False))
    total = sum(a.distance_to(b) for a, b in segs)
    if total <= 1e-9:
        return 1.0, 0.0
    best_d, best_s, done = math.inf, 0.0, 0.0
    for a, b in segs:
        dx, dy = b.x - a.x, b.y - a.y
        length = math.hypot(dx, dy)
        t = 0.0 if length <= 1e-9 else max(0.0, min(1.0, ((pose.x - a.x) * dx + (pose.y - a.y) * dy) / length**2))
        d = math.hypot(a.x + t * dx - pose.x, a.y + t * dy - pose.y)
        if d < best_d:
            best_d, best_s = d, done + t * length
        done += length
    return max(0.0, min(1.0, best_s / total)), max(0.0, total - best_s)


class T0Vla:
    """VlaService. Construct via ``create_vla(services)`` / ``create_vla_fake(services)``; no I/O until a call.

    ``t1``: the T1 adapter (default: not enabled). Extras: ``execs()`` (records, newest last)."""

    def __init__(self, services: Services | None = None, *, t1: T1Adapter | None = None) -> None:
        self.services = services or Services()
        self.t1 = t1 or T1Adapter()
        self._execs: dict[ExecId, _Exec] = {}

    # ==================================================================================================================
    # VlaService
    # ==================================================================================================================
    async def capabilities(self, robot_id: RobotId) -> list[VlaCapabilities]:
        robot = self.services.require("robot")
        try:
            st = await robot.state(robot_id)
        except RobotError as exc:
            raise KeyError(robot_id) from exc
        return [
            VlaCapabilities(
                robot_id=robot_id,
                tier=T0,
                available=True,
                backend=st.backend,
                step_kinds=list(T0_KINDS),
                model_id="scripted",
                place_back=self._can_put_back(robot, robot_id, st),
                basket_unload=self._can_unload(robot, robot_id),
                cancel_latency_ms=200.0 if st.backend == "isaac" else 50.0,
            ),
            self.t1.capability(robot_id, st.backend, robot=robot),
            VlaCapabilities(
                robot_id=robot_id,
                tier="t2_finetuned",
                available=False,
                reason="fine-tuned SO-101 policy: later (physical robot)",
                backend=st.backend,
                step_kinds=["grasp", "place"],
            ),
        ]

    async def execute(self, request: VlaRequest, *, after_seq: int = 0) -> AsyncIterator[VlaStatus]:
        ex = self._execs.get(request.exec_id)
        if ex is None:
            ex = _Exec(request)
            self._prune()
            self._execs[request.exec_id] = ex
            other = self._running(request.robot_id, exclude=request.exec_id)
            if other is not None:
                await self._emit(
                    ex,
                    phase="failed",
                    error=VlaError(code="busy", message=f"{request.robot_id} is running {other.request.exec_id}"),
                )
            else:
                ex.task = asyncio.create_task(self._run(ex), name=f"vla:{request.exec_id}")
        elif ex.record.body_hash != request.body_hash:
            yield VlaStatus(
                exec_id=request.exec_id,
                robot_id=request.robot_id,
                seq=1,
                phase="failed",
                error=VlaError(code="idempotency_conflict", message="exec_id reused with a different request body"),
            )
            return
        seen = after_seq
        while True:
            async with ex.cond:
                await ex.cond.wait_for(lambda n=seen: len(ex.record.statuses) > n)  # type: ignore[misc]
            for status in ex.record.statuses[seen:]:
                seen = status.seq
                yield status
                if status.terminal:
                    return

    async def get_exec(self, exec_id: ExecId) -> ExecRecord:
        return self._execs[exec_id].record.model_copy(deep=True)

    async def cancel(self, exec_id: ExecId, reason: str = "") -> VlaStatus:
        ex = self._execs[exec_id]
        if ex.record.terminal is None and ex.task is not None and not ex.task.done():
            if not ex.cancelling:
                ex.cancelling = True
                ex.cancel_reason = reason
                ex.task.cancel(reason or "cancel")
            await asyncio.wait({ex.task})
        terminal = ex.record.terminal
        if terminal is None:  # the task died without a terminal (cannot happen; belt and braces)
            terminal = await self._emit(ex, phase="cancelled", error=VlaError(code="cancelled", message=reason))
        return terminal

    async def cancel_robot(self, robot_id: RobotId, reason: str = "") -> list[VlaStatus]:
        out = []
        for ex in list(self._execs.values()):
            if ex.request.robot_id == robot_id and ex.record.terminal is None:
                out.append(await self.cancel(ex.request.exec_id, reason))
        return out

    # ---- extras ------------------------------------------------------------------------------------------------------
    def execs(self) -> list[ExecRecord]:
        return [ex.record for ex in self._execs.values()]

    # ==================================================================================================================
    # stream internals
    # ==================================================================================================================
    def _running(self, robot_id: RobotId, *, exclude: ExecId) -> _Exec | None:
        return next(
            (
                e
                for e in self._execs.values()
                if e.request.robot_id == robot_id and e.request.exec_id != exclude and e.record.terminal is None
            ),
            None,
        )

    def _prune(self) -> None:
        cutoff = utcnow() - timedelta(seconds=EXEC_RETENTION_S)
        for eid in [
            e for e, x in self._execs.items() if x.record.terminal is not None and x.record.created_at < cutoff
        ]:
            del self._execs[eid]

    async def _emit(self, ex: _Exec, **fields: Any) -> VlaStatus:
        async with ex.cond:
            if ex.record.terminal is not None:  # exactly one terminal, nothing after it
                return ex.record.terminal
            status = VlaStatus(
                exec_id=ex.request.exec_id,
                robot_id=ex.request.robot_id,
                seq=len(ex.record.statuses) + 1,
                tier=fields.pop("tier", ex.tier),
                backend=fields.pop("backend", ex.backend),
                **fields,
            )
            ex.record.statuses.append(status)
            ex.cond.notify_all()
        return status

    async def _stage(self, ex: _Exec, stage: str, progress: float, **extra: Any) -> None:
        ex.stage, ex.progress = stage, progress
        learned = ex.tier not in (None, T0)
        if learned:
            ex.chunk += 1
        await self._emit(
            ex, phase="running", stage=stage, progress=progress, chunk_no=ex.chunk if learned else None, **extra
        )
        await asyncio.sleep(0)

    async def _heartbeat(self, ex: _Exec) -> None:
        robot = self.services.require("robot")
        while True:
            await asyncio.sleep(ex.request.heartbeat_s)
            if ex.silent:
                continue
            pose: Pose2D | None = None
            eta: float | None = None
            try:
                pose = (await robot.state(ex.request.robot_id)).pose
            except Exception:  # noqa: BLE001 - a heartbeat must never fail the exec
                pose = None
            if pose is not None and ex.stage == "driving" and len(ex.path) > 1:
                frac, remaining = path_progress(ex.path, pose)
                ex.progress = max(ex.progress, round(0.1 + 0.85 * frac, 3))
                eta = remaining / DEFAULT_SPEED_MPS
            await self._emit(
                ex, phase="running", stage=ex.stage, progress=ex.progress, pose=pose, eta_s=eta, message="heartbeat"
            )

    async def _consult(self, ex: _Exec, tier: VlaTier, effects: list[FaultEffect]) -> dict[str, str]:
        faults = self.services.faults
        if faults is None or not effects:
            return {}
        req, step, ctx = ex.request, ex.request.step, ex.request.context
        target = getattr(step, "target", None)
        fctx = FaultContext(
            robot_id=req.robot_id,
            step_kind=step.kind,
            step_id=ctx.step_id,
            step_key=ctx.step_key if ctx.step_id else req.exec_id,
            item_id=ctx.item_id or step.item_id,
            sku=getattr(target, "sku", None) or getattr(step, "sku", None),
            slot_id=getattr(target, "slot_id", None) or getattr(step, "slot_id", None),
            tier=tier,
            attempt=ctx.attempt,
            exec_id=req.exec_id,
        )
        try:
            fired = await faults.consult(fctx, effects)
        except Exception as exc:  # noqa: BLE001 - a broken injector must not break the robot
            log.warning("fault consult failed: %s", exc)
            return {}
        out = {f.effect: f.fault_id for f in fired}
        ex.fault_ids.extend(out.values())
        return out

    def _accept_tier(self, req: VlaRequest, caps: list[VlaCapabilities]) -> tuple[VlaTier, VlaTier | None]:
        """THE tier at accept: resolve_tier, then the adapter's executability; fallback only if allowed."""
        kind = req.step.kind
        tier = resolve_tier(req.tier, caps, kind)
        fell_back: VlaTier | None = None
        if req.tier != "auto" and tier != req.tier:
            cap = next((c for c in caps if c.tier == req.tier), None)
            why = (cap.reason if cap is not None and cap.reason else None) or f"{req.tier} cannot run {kind} now"
            if not req.allow_fallback:
                raise _Fail("policy_unavailable", why)
            fell_back = req.tier  # type: ignore[assignment]
        if tier != T0 and not self.t1.can_execute():
            if not req.allow_fallback:
                raise _Fail("policy_unavailable", f"{tier} cannot execute on this wire")
            fell_back, tier = tier, T0
        unload = getattr(req.step, "source", "shelf") == "basket" or getattr(req.step, "destination", "") == "counter"
        if tier != T0 and unload:  # the learned policies are trained on shelf grasps: the checkout unload is T0
            fell_back, tier = tier, T0
        return tier, fell_back

    async def _run(self, ex: _Exec) -> None:
        req, step = ex.request, ex.request.step
        t0 = time.monotonic()
        hb: asyncio.Task[None] | None = None
        try:
            try:
                robot = self.services.require("robot")
            except RuntimeError as exc:
                await self._emit(ex, phase="failed", error=VlaError(code="not_ready", message=str(exc)))
                return
            try:
                caps = await self.capabilities(req.robot_id)
            except KeyError:
                await self._emit(ex, phase="failed", error=VlaError(code="unknown_robot", message=req.robot_id))
                return
            st = await robot.state(req.robot_id)
            ex.backend = st.backend
            self._bind_faults(req, st)
            if req.store_id and st.store_id and req.store_id != st.store_id:
                raise _Fail("precondition_failed", f"{req.robot_id} works in {st.store_id}, not {req.store_id}")
            if step.kind not in T0_KINDS:
                raise _Fail("unsupported_step", f"{step.kind} is not a VLA step")
            try:
                tier, fell_back = self._accept_tier(req, caps)
            except _Fail:
                ex.tier = req.tier if req.tier != "auto" else None  # type: ignore[assignment]
                raise
            if tier != T0 and await self._consult(ex, tier, ["policy_unavailable"]):
                if not req.allow_fallback:
                    ex.tier = tier
                    raise _Fail("policy_unavailable", "policy server refused the skill (injected)")
                fell_back, tier = tier, T0
            ex.tier = tier
            ex.accepted_mono = time.monotonic()
            await self._emit(
                ex,
                phase="accepted",
                fell_back_from=fell_back,
                pose=st.pose,
                message=f"{step.kind} on {tier}" + (f" (fell back from {fell_back})" if fell_back else ""),
            )
            effects = await self._consult(ex, tier, VLA_EFFECTS.get(step.kind, []))
            hb = asyncio.create_task(self._heartbeat(ex), name=f"vla-hb:{req.exec_id}")
            async with asyncio.timeout(req.deadline_s):
                if "vla_hang" in effects:
                    ex.silent = True
                    await asyncio.Event().wait()
                claim = await self._act(ex, robot, st, effects)
            ended = ex.ended_at or utcnow()
            final = await robot.state(req.robot_id)
            overlay = self._overlay()
            virtual = overlay.virtual_holding.get(req.robot_id) if overlay is not None else None
            # The heartbeat keeps running through the evidence capture: on a loaded GPU host one render can take
            # seconds, and a silent capture used to trip the runner's stall detector ("the VLA stopped reporting
            # progress") after a motion that had already succeeded (measured on vhagar 2026-10-18).
            frames = await self._capture_after(ex)
            hb.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await hb
            outcome = VlaOutcome(
                self_report=ex.self_report,
                self_confidence=0.9 if tier != T0 else None,
                final_state=final,
                holding_instance_id=virtual.instance_id if virtual else final.holding,
                frames=frames,
                ended_at=ended,
                duration_ms=(time.monotonic() - t0) * 1000,
                chunks_executed=ex.chunk or None,
            )
            await self._emit(
                ex, phase="succeeded", progress=1.0, stage="done", pose=final.pose, message=claim, outcome=outcome
            )
        except TimeoutError:
            await self._safe_stop(req.robot_id)
            await self._emit(
                ex,
                phase="failed",
                error=VlaError(
                    code="deadline_exceeded", tier=ex.tier, message=f"deadline {req.deadline_s:.1f}s exceeded"
                ),
                outcome=await self._final(ex, t0),
            )
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None:
                task.uncancel()  # we own this task: finish the stop + terminal status, then end normally
            await self._safe_stop(req.robot_id)
            await self._emit(
                ex,
                phase="cancelled",
                error=VlaError(code="cancelled", tier=ex.tier, message=ex.cancel_reason or "cancelled"),
                outcome=await self._final(ex, t0),
            )
        except RobotError as exc:
            await self._emit(
                ex,
                phase="failed",
                error=VlaError.from_sim(exc.code, exc.message, ex.tier),
                outcome=await self._final(ex, t0),
            )
        except _Fail as exc:
            await self._emit(
                ex,
                phase="failed",
                error=VlaError(code=exc.code, message=exc.message, tier=ex.tier),
                outcome=await self._final(ex, t0),
            )
        except Exception as exc:  # noqa: BLE001 - the stream must always end with exactly one terminal
            log.exception("vla exec %s crashed", req.exec_id)
            await self._emit(ex, phase="failed", error=VlaError(code="internal", message=repr(exc), tier=ex.tier))
        finally:
            if hb is not None:
                hb.cancel()

    async def _safe_stop(self, robot_id: RobotId) -> None:
        try:
            await self.services.require("robot").stop(robot_id)
        except Exception:  # noqa: BLE001
            pass

    async def _final(self, ex: _Exec, t0: float) -> VlaOutcome | None:
        try:
            st = await self.services.require("robot").state(ex.request.robot_id)
        except Exception:  # noqa: BLE001
            return None
        return VlaOutcome(
            self_report="failure",
            final_state=st,
            holding_instance_id=st.holding,
            ended_at=ex.ended_at,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    async def _capture_after(self, ex: _Exec) -> list[FrameRef]:
        evidence = self.services.evidence
        req = ex.request
        if evidence is None or not req.capture_after:
            return []
        step, ctx = req.step, req.context
        slot = getattr(getattr(step, "target", None), "slot_id", None) or getattr(step, "slot_id", None)
        context = {
            "plan_id": ctx.plan_id,
            "goal_id": ctx.goal_id,
            "step_id": ctx.step_id,
            "step_key": ctx.step_key if ctx.step_id else req.exec_id,
            "attempt": ctx.attempt,
            "step_kind": step.kind,
            "item_id": ctx.item_id or step.item_id,
            "exec_id": req.exec_id,
            "slot_ids": [slot] if slot else [],
        }
        try:
            aim = slot if "front" in req.capture_after else None
            return await asyncio.wait_for(
                evidence.capture_refs(req.robot_id, list(req.capture_after), context=context, aim_slot_id=aim),
                EVIDENCE_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 - evidence is best effort; the verifier captures by itself
            log.warning("capture_after for %s failed: %s", req.exec_id, exc)
            return []

    def _overlay(self) -> FaultOverlay | None:
        faults = self.services.faults
        return getattr(faults, "overlay", None) if faults is not None else None

    def _bind_faults(self, req: VlaRequest, st: RobotState) -> None:
        bind = getattr(self.services.faults, "bind_robot", None) if self.services.faults is not None else None
        if bind is not None:
            try:
                bind(req.robot_id, trip_id=req.context.trip_id, store_id=st.store_id)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _can_unload(robot: Any, robot_id: RobotId) -> bool:
        """0.2.4: basket -> counter primitives on this robot's backend (fake: yes; Isaac: worker capability)."""
        probe = getattr(robot, "can_unload_to_counter", None)
        try:
            return bool(probe(robot_id)) if probe is not None else False
        except Exception:  # noqa: BLE001
            return False

    def _can_put_back(self, robot: Any, robot_id: RobotId, st: RobotState) -> bool:
        overlay = self._overlay()
        if overlay is not None and robot_id in overlay.virtual_holding:
            return True
        probe = getattr(robot, "can_put_back", None)
        try:
            return bool(probe(robot_id)) if probe is not None else False
        except Exception:  # noqa: BLE001
            return False

    # ==================================================================================================================
    # T0 motions
    # ==================================================================================================================
    async def _slot(self, store_id: str, slot_id: str) -> Slot:
        plan = await live_planogram(self.services, store_id)
        slot = plan.slot(slot_id)
        if slot is None:
            raise RobotError("unknown_slot", f"unknown slot {slot_id!r} in {store_id}")
        return slot

    async def _path(self, st: RobotState, goal: Pose2D) -> list[Pose2D]:
        """Classical planner inside the VLA (every tier): a RoutingService leg from the current pose, else straight."""
        routing = self.services.routing
        if routing is None:
            return [goal]
        try:
            route = await routing.plan_route(
                RouteRequest(store_id=st.store_id or "", start=st.pose, end=goal, stops=[])
            )
        except Exception as exc:  # noqa: BLE001 - no path service: drive straight (the robot reports path_blocked)
            log.warning("routing failed (%s): driving straight", exc)
            return [goal]
        pts = [wp for leg in route.legs for wp in leg.waypoints]
        out = [p for i, p in enumerate(pts) if i == 0 or (p.x, p.y) != (pts[i - 1].x, pts[i - 1].y)]
        out = out[1:] if len(out) > 1 else out
        if not out or out[-1].distance_to(goal) > 1e-3 or abs(angle_diff(out[-1].theta, goal.theta)) > 1e-3:
            out.append(goal)
        return out

    async def _drive(self, ex: _Exec, robot: Any, st: RobotState, waypoints: list[Pose2D]) -> RobotState:
        ex.path = [st.pose, *waypoints]
        ex.path_len = sum(a.distance_to(b) for a, b in zip(ex.path, ex.path[1:], strict=False))
        await self._stage(ex, "driving", 0.1, eta_s=ex.path_len / DEFAULT_SPEED_MPS)
        cam = asyncio.create_task(self._nav_camera(ex, robot, st), name=f"vla-cam:{ex.request.exec_id}")
        try:
            final = await robot.navigate(ex.request.robot_id, waypoints)
        finally:
            cam.cancel()  # never awaited here: a cancel of this exec must reach the caller untouched
            cam.add_done_callback(_swallow)
        ex.ended_at = utcnow()
        await self._arrival_look(ex)
        return final

    async def _arrival_look(self, ex: _Exec) -> None:
        """On arrival: one more picture with the head aimed at the slot + a look-out, unless one already saw it."""
        evidence = self.services.evidence
        slot = getattr(ex.request.step, "slot_id", None)
        seen = any(o.lower().startswith("yes") for o in ex.observations)
        if evidence is None or not ex.nav_target or not slot or seen or ex.nav_looks >= NAV_LOOKS_PER_EXEC:
            return
        ex.nav_looks += 1
        try:
            async with asyncio.timeout(ARRIVAL_LOOK_TIMEOUT_S):
                refs = await evidence.capture_refs(
                    ex.request.robot_id,
                    ["front"],
                    context={"exec_id": ex.request.exec_id, "step_kind": ex.request.step.kind, "purpose": "arrival"},
                    aim_slot_id=slot,
                )
                if refs:
                    ex.nav_frames.append(refs[0])
                    await self._look(ex, evidence, refs[0], ex.nav_target, when="on arrival")
        except TimeoutError:
            log.info("arrival look-out for %s timed out", ex.request.exec_id)
        except Exception as exc:  # noqa: BLE001 - advisory
            log.debug("arrival look-out for %s failed: %s", ex.request.exec_id, exc)

    async def _nav_camera(self, ex: _Exec, robot: Any, st: RobotState) -> None:
        """Pictures while driving: front camera every 1/hz s -> blurred, retained evidence (FrameRef) + a running
        status; on the approach a rate-limited Perceptron look-out ('is <product> visible?') -> stage 'observing'."""
        try:
            hz = float(_setting("picky_vla_nav_capture_hz", DEFAULT_NAV_CAPTURE_HZ))
        except (TypeError, ValueError):
            hz = DEFAULT_NAV_CAPTURE_HZ
        evidence = self.services.evidence
        if hz <= 0 or evidence is None:
            return
        period = 1.0 / hz
        req = ex.request
        look_mode = str(_setting("picky_vla_nav_look", "isaac")).lower()
        try:
            look_m = float(_setting("picky_vla_nav_look_m", DEFAULT_NAV_LOOK_M))
        except (TypeError, ValueError):
            look_m = DEFAULT_NAV_LOOK_M
        want_look = req.step.kind == "navigate_to" and (
            look_mode == "always" or (look_mode == "isaac" and st.backend == "isaac")
        )
        target = await self._nav_target_name(ex, st) if want_look else None
        ex.nav_target = target
        context = {
            "plan_id": req.context.plan_id,
            "goal_id": req.context.goal_id,
            "step_id": req.context.step_id,
            "step_kind": req.step.kind,
            "attempt": req.context.attempt,
            "exec_id": req.exec_id,
            "purpose": "nav_picture",
        }
        while True:
            t0 = time.monotonic()
            try:
                refs = await asyncio.wait_for(
                    evidence.capture_refs(req.robot_id, ["front"], context=context), CAPTURE_TIMEOUT_S
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a missed picture never fails the drive
                log.debug("nav picture for %s failed: %s", req.exec_id, exc)
                refs = []
            if refs:
                ref = refs[0]
                ex.nav_frames.append(ref)
                del ex.nav_frames[:-NAV_FRAMES_KEPT]
                await self._emit(
                    ex,
                    phase="running",
                    stage=ex.stage,
                    progress=ex.progress,
                    pose=ref.pose,
                    message=f"front camera picture {len(ex.nav_frames)} while driving ({ref.frame_id})",
                )
                remaining = path_progress(ex.path, ref.pose)[1] if ref.pose is not None and len(ex.path) > 1 else 0.0
                if target and ex.nav_looks < NAV_LOOKS_PER_EXEC - 1 and remaining <= look_m:
                    ex.nav_looks += 1  # one while approaching; the last one is kept for the arrival
                    await self._look(ex, evidence, ref, target, when="while driving")
            await asyncio.sleep(max(0.0, period - (time.monotonic() - t0)))

    async def _look(self, ex: _Exec, evidence: Any, ref: FrameRef, target: str, when: str = "") -> None:
        """One Perceptron Mk1.5 look-out on a retained (blurred) picture -> a running 'observing' status."""
        from arms.robot.vla_mk15 import look

        try:
            frame = await evidence.get_frame(ref.frame_id)
            if not frame.blurred:
                return  # section 8: only blurred frames leave for a vision model
            ctx = ex.request.context
            answer = await look(
                base64.b64decode(frame.jpeg_b64),
                target,
                budget_key=ctx.trip_id or ctx.goal_id or ex.request.robot_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the look-out is advisory
            log.debug("look-out on %s failed: %s", ref.frame_id, exc)
            return
        if not answer:
            return
        ex.observations.append(answer)
        await self._emit(
            ex,
            phase="running",
            stage="observing",
            progress=ex.progress,
            pose=ref.pose,
            message=f"Perceptron Mk1.5 {when} ({ref.frame_id}): is {target} visible? {answer}",
        )

    async def _nav_target_name(self, ex: _Exec, st: RobotState) -> str | None:
        slot_id = getattr(ex.request.step, "slot_id", None)
        if not slot_id:
            return None
        try:
            slot = await self._slot(st.store_id or "", slot_id)
        except Exception:  # noqa: BLE001
            return None
        return (await self._product_name(slot.sku)).lower()

    @staticmethod
    def _at(pose: Pose2D, goal: Pose2D, tol_m: float, tol_rad: float) -> bool:
        return pose.distance_to(goal) <= tol_m and abs(angle_diff(pose.theta, goal.theta)) <= tol_rad

    async def _act(self, ex: _Exec, robot: Any, st: RobotState, effects: dict[str, str]) -> str:
        step = ex.request.step
        if "disconnect" in effects:
            raise RobotError("disconnected", "Isaac link lost (injected); the robot continues on the 2D fake")
        if step.kind == "navigate_to":
            return await self._navigate(ex, robot, st, effects)
        if step.kind == "align":
            return await self._align(ex, robot, st)
        if step.kind == "grasp" and getattr(step, "source", "shelf") == "basket":
            return await self._grasp_basket(ex, robot, st, effects)
        if step.kind == "place" and getattr(step, "destination", "basket") == "counter":
            return await self._place_counter(ex, robot, st, effects)
        if step.kind == "grasp":
            return await self._grasp(ex, robot, st, effects)
        if step.kind == "place":
            return await self._place(ex, robot, st, effects)
        if step.kind == "handoff":
            await self._stage(ex, "unloading", 0.5)
            units = await robot.unload_basket(ex.request.robot_id)
            ex.ended_at = utcnow()
            overlay = self._overlay()
            if overlay is not None:
                overlay.virtual_basket.pop(ex.request.robot_id, None)
            return f"unloaded {len(units)} units at checkout"
        raise _Fail("unsupported_step", f"{step.kind} is not a VLA step")

    async def _navigate(self, ex: _Exec, robot: Any, st: RobotState, effects: dict[str, str]) -> str:
        step = ex.request.step
        await self._stage(ex, "planning_path", 0.05)
        if "nav_blocked" in effects:
            raise RobotError("path_blocked", "another rover blocks the aisle (injected)")
        goal: Pose2D = step.goal_pose  # type: ignore[union-attr]
        if self._at(st.pose, goal, step.tolerance_m, step.yaw_tolerance_rad):  # type: ignore[union-attr]
            ex.ended_at = utcnow()
            return f"already at {step.target}"  # type: ignore[union-attr]
        waypoints = list(step.waypoints) or await self._path(st, goal)  # type: ignore[union-attr]
        if waypoints[-1].distance_to(goal) > 1e-3 or abs(angle_diff(waypoints[-1].theta, goal.theta)) > 1e-3:
            waypoints.append(goal)
        await self._drive(ex, robot, st, waypoints)
        return f"arrived at {step.target}" + (f" {step.slot_id}" if step.slot_id else "")  # type: ignore[union-attr]

    async def _align(self, ex: _Exec, robot: Any, st: RobotState) -> str:
        step = ex.request.step
        await self._stage(ex, "aligning", 0.2)
        loc = step.location  # type: ignore[union-attr]
        approach = (
            loc.approach_pose if loc is not None else (await self._slot(st.store_id or "", step.slot_id)).approach_pose
        )  # type: ignore[union-attr]
        if not self._at(st.pose, approach, ALIGN_TOLERANCE_M, ALIGN_YAW_TOLERANCE_RAD):
            far = st.pose.distance_to(approach) > ROUTED_ALIGN_M
            waypoints = await self._path(st, approach) if far else [approach]
            await self._drive(ex, robot, st, waypoints)
        else:
            ex.ended_at = utcnow()
        await self._stage(ex, "lift_preposition", 0.6)
        await self._stage(ex, "aiming_camera", 0.9)
        return f"aligned to {step.slot_id}"  # type: ignore[union-attr]

    async def _grasp(self, ex: _Exec, robot: Any, st: RobotState, effects: dict[str, str]) -> str:
        req, step = ex.request, ex.request.step
        rid = req.robot_id
        target = step.target  # type: ignore[union-attr]
        if target.slot_id is None:
            raise _Fail("target_not_grounded", "T0 grasp needs the target's slot")
        grounding = req.grounding
        inst = (grounding.instance_id if grounding else None) or target.instance_id
        if ex.tier != T0 and self.t1.needs_grounding and not (grounding and (grounding.instance_id or grounding.box)):
            raise _Fail("target_not_grounded", f"{ex.tier} needs a grounded target (run ground_target first)")
        await self._stage(ex, "reaching", 0.2)
        slot = await self._slot(st.store_id or "", target.slot_id)
        if not slot_reachable(st.pose, slot.approach_pose, slot.item_position):
            dist = st.pose.distance_to(slot.approach_pose)
            code: VlaErrorCode = "not_at_pose" if dist > NOT_AT_POSE_M else "out_of_reach"
            raise _Fail(code, f"{target.slot_id} not reachable from here ({dist:.2f} m from its approach pose)")
        if ex.tier != T0 and self.t1.backend == "policy":
            return await self._grasp_policy(ex, robot, slot, target.sku, inst)
        if "grasp_miss" in effects:
            await self._stage(ex, "closing", 0.5)
            raise RobotError("grasp_failed", "gripper closed on air (injected)")
        await self._stage(ex, "closing", 0.5)
        if "drop_after_lift" in effects:
            await self._stage(ex, "lifting", 0.85)
            ex.ended_at = utcnow()
            return f"grasped {target.sku}"  # the CLAIM: nothing is attached; the wrist check must catch it
        if "wrong_item" in effects and await self._grab_wrong(ex, robot, st, slot, target.sku):
            await self._stage(ex, "lifting", 0.85)
            return f"grasped {target.sku}"  # the CLAIM: it holds the neighbouring SKU
        held: ItemInstance = await robot.pick(rid, target.slot_id, target.sku, inst)
        ex.ended_at = utcnow()
        if "mislabel" in effects:
            self._mislabel(held, effects["mislabel"])
        await self._stage(ex, "lifting", 0.85)
        return f"grasped {target.sku} ({held.instance_id})"

    async def _grasp_basket(self, ex: _Exec, robot: Any, st: RobotState, effects: dict[str, str]) -> str:
        """0.2.4 checkout unload: take one unit of the target SKU out of the robot's own basket."""
        req, step = ex.request, ex.request.step
        target = step.target  # type: ignore[union-attr]
        grounding = req.grounding
        inst = (grounding.instance_id if grounding else None) or target.instance_id
        if not self._can_unload(robot, req.robot_id):
            raise _Fail("unsupported_step", "no basket -> counter primitive on this backend (Isaac worker < 0.2.4)")
        await self._stage(ex, "reaching_into_basket", 0.25)
        if "grasp_miss" in effects:
            raise RobotError("grasp_failed", "gripper closed on air in the basket (injected)")
        try:
            held: ItemInstance = await robot.pick_from_basket(req.robot_id, target.sku, inst)
        except RobotError as exc:
            if exc.code == "no_instance" and inst is not None:  # the grounded unit moved: any unit of the SKU
                held = await robot.pick_from_basket(req.robot_id, target.sku, None)
            else:
                raise
        ex.ended_at = utcnow()
        await self._stage(ex, "lifting", 0.85)
        return f"took {target.sku} out of the basket ({held.instance_id})"

    async def _place_counter(self, ex: _Exec, robot: Any, st: RobotState, effects: dict[str, str]) -> str:
        """0.2.4 checkout unload: put the held unit on the counter spot (reach-checked like a shelf slot)."""
        req, step = ex.request, ex.request.step
        spot = step.counter  # type: ignore[union-attr]
        if spot is None:
            raise _Fail("precondition_failed", "place on the counter needs a counter spot")
        if st.holding is None:
            raise _Fail("place_failed", "nothing to put on the counter: the gripper is empty")
        if not slot_reachable(st.pose, spot.approach_pose, spot.position):
            dist = st.pose.distance_to(spot.approach_pose)
            code: VlaErrorCode = "not_at_pose" if dist > NOT_AT_POSE_M else "out_of_reach"
            raise _Fail(code, f"counter spot {spot.index + 1} not reachable ({dist:.2f} m from its approach pose)")
        await self._stage(ex, "moving_to_counter", 0.4)
        if "place_miss" in effects:
            ex.ended_at = utcnow()
            await self._stage(ex, "releasing", 0.9)
            return f"placed {step.sku} on the counter"  # type: ignore[union-attr]  # the CLAIM: still holding
        await robot.place_on_counter(req.robot_id, spot.position, spot.yaw, spot.index)
        ex.ended_at = utcnow()
        await self._stage(ex, "releasing", 0.9)
        return f"placed {step.sku} on counter spot {spot.index + 1}"  # type: ignore[union-attr]

    async def _grasp_policy(self, ex: _Exec, robot: Any, slot: Slot, sku: str, inst: str | None) -> str:
        """T1 learned grasp (arms.robot.vla_t1): the policy drives the joints on Isaac; a CLAIM like every tier."""
        from arms.robot.vla_t1 import STEP_RESERVE_S, run_policy_grasp

        req = ex.request
        cfg = self.t1.grasp_config
        backend = await self.t1.policy_backend(req.robot_id)
        name = await self._product_name(sku)
        budget = max(1.0, req.deadline_s - (time.monotonic() - ex.accepted_mono) - STEP_RESERVE_S)

        async def on_stage(stage: str, progress: float) -> None:
            await self._policy_status(ex, stage, progress)

        async def on_chunk(n: int, res: Any, chunk: Any) -> None:
            arm = res.arm
            ex.chunk = n
            note = f"chunk {n}: {arm.steps_applied if arm else '?'} rows"
            if arm is not None and arm.events:
                note += f" ({', '.join(arm.events)})"
            await self._policy_status(ex, "policy", min(0.85, 0.2 + 0.65 * n / max(1, cfg.max_chunks)), note)

        result = await run_policy_grasp(
            robot,
            req.robot_id,
            backend=backend,
            slot=slot,
            sku=sku,
            product_name=name,
            instance_id=inst,
            cfg=cfg,
            perception=self.services.perception,
            on_stage=on_stage,
            on_chunk=on_chunk,
            budget_s=budget,
            exec_id=req.exec_id,
            budget_key=req.context.trip_id or req.context.goal_id or req.robot_id,
        )
        ex.ended_at = result.ended_at or utcnow()
        if not result.ok:
            raise _Fail(result.code or "internal", result.message)
        if result.on_target is False:
            ex.self_report = "unsure"
        await self._policy_status(ex, "lifting", 0.9, result.message)
        return result.message

    async def _policy_status(self, ex: _Exec, stage: str, progress: float, message: str = "") -> None:
        """A running status of a learned grasp: ``chunk_no`` = action chunks executed so far (liveness)."""
        ex.stage, ex.progress = stage, progress
        await self._emit(ex, phase="running", stage=stage, progress=progress, chunk_no=ex.chunk, message=message)
        await asyncio.sleep(0)

    async def _product_name(self, sku: str) -> str:
        catalog = self.services.catalog
        if catalog is not None:
            try:
                product = await catalog.get_product(sku)
                if product is not None and product.name:
                    return product.name
            except Exception as exc:  # noqa: BLE001 - the sku reads fine in an instruction too
                log.warning("product lookup for %s failed: %s", sku, exc)
        return sku.replace("_", " ").replace("-", " ")

    async def _grab_wrong(self, ex: _Exec, robot: Any, st: RobotState, slot: Slot, sku: str) -> bool:
        """'wrong_item': grab a unit of the nearest other-SKU slot on the same shelf level (2D fake: for real)."""
        plan = await live_planogram(self.services, st.store_id or "")
        rid = ex.request.robot_id
        near = sorted(
            (s for s in plan.slots if s.fixture_id == slot.fixture_id and s.level == slot.level and s.sku != sku),
            key=lambda s: (  # the next slot to the right first, then left, then by distance
                abs(s.index - slot.index),
                s.index < slot.index,
                math.dist((s.item_position.x, s.item_position.y), (slot.item_position.x, slot.item_position.y)),
            ),
        )
        for other in near:
            units = await robot.list_instances(rid, other.slot_id)
            if not units:
                continue
            grab = getattr(robot, "grab_unit", None)
            if grab is not None and st.backend == "fake":
                await grab(rid, other.slot_id, units[0].instance_id)
            else:
                overlay = self._overlay()
                if overlay is None:
                    return False
                overlay.virtual_holding[rid] = units[0]
            ex.ended_at = utcnow()
            return True
        return False

    def _mislabel(self, held: ItemInstance, fault_id: str) -> None:
        faults, overlay = self.services.faults, self._overlay()
        rule = faults.rule(fault_id) if faults is not None and hasattr(faults, "rule") else None
        text = str((rule.params.get("text") if rule is not None else None) or "UNKNOWN LABEL")
        if overlay is not None:
            overlay.mislabels[held.instance_id] = text

    async def _place(self, ex: _Exec, robot: Any, st: RobotState, effects: dict[str, str]) -> str:
        req, step = ex.request, ex.request.step
        rid = req.robot_id
        overlay = self._overlay()
        virtual = overlay.virtual_holding.get(rid) if overlay is not None else None
        if step.destination == "shelf":  # type: ignore[union-attr]
            await self._stage(ex, "moving_to_shelf", 0.4)
            if overlay is not None and virtual is not None:
                del overlay.virtual_holding[rid]
                ex.ended_at = utcnow()
                await self._stage(ex, "releasing", 0.9)
                return f"put {virtual.sku} back on {virtual.slot_id}"
            if st.holding is None:
                raise _Fail("place_failed", "nothing to put back: the gripper is empty")
            if not self._can_put_back(robot, rid, st):
                raise _Fail("unsupported_step", "no place-back primitive on this backend (T0 over the 0.2 sim wire)")
            await robot.put_back(rid, step.slot_id)  # type: ignore[union-attr]
            ex.ended_at = utcnow()
            await self._stage(ex, "releasing", 0.9)
            return f"put {st.holding} back"
        await self._stage(ex, "moving_to_basket", 0.4)
        if overlay is not None and virtual is not None:  # a virtual (Isaac overlay) unit lands in the basket
            del overlay.virtual_holding[rid]
            overlay.virtual_basket.setdefault(rid, []).append(virtual.model_copy(update={"in_basket": True}))
            ex.ended_at = utcnow()
            await self._stage(ex, "releasing", 0.9)
            return f"placed {step.sku}"  # type: ignore[union-attr]
        if st.holding is None:
            raise _Fail("place_failed", "nothing to place: the gripper is empty")
        if "place_miss" in effects:
            ex.ended_at = utcnow()
            await self._stage(ex, "releasing", 0.9)
            return f"placed {step.sku}"  # type: ignore[union-attr]  # the CLAIM: still in the gripper
        await robot.place_in_basket(rid)
        ex.ended_at = utcnow()
        await self._stage(ex, "releasing", 0.9)
        return f"placed {step.sku}"  # type: ignore[union-attr]


__all__ = ["EXEC_RETENTION_S", "T0_KINDS", "VLA_EFFECTS", "T0Vla", "path_progress"]
