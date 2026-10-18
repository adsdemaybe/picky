"""FakeVla: the T0 'scripted / oracle' tier of the VLA API over the v0.2 RobotService, with the full stream contract
(accepted ▸ running heartbeats ▸ one terminal), idempotent exec ids, cancel, deadlines and VLA-layer fault injection.

T0 mapping (the same as the real robot arm's T0): navigate_to -> robot.navigate(waypoints or [goal_pose]) · align ->
small navigate to the approach pose (+ lift / pan-tilt stages) · grasp -> robot.pick(slot, sku, grounded instance) ·
place -> robot.place_in_basket · handoff -> robot.unload_basket. RobotError codes map through ``VlaError.from_sim``.

``simulate_t1=True`` advertises a simulated T1 tier (same motions, policy-style chunk counters) so tier selection, the
policy_unavailable fallback and the breaker can be demoed without vhagar. Faults (layer 'vla'): drop_after_lift,
grasp_miss, wrong_item (2D overlay), place_miss, nav_blocked, vla_hang, policy_unavailable, disconnect.
"""

import asyncio
import time
from typing import Any

from contracts.common import angle_diff, utcnow
from contracts.faults import FaultContext, FaultEffect
from contracts.plan import ExecId, VlaTier
from contracts.routing import RouteRequest
from contracts.sim import RobotError
from contracts.vla import ExecRecord, VlaCapabilities, VlaError, VlaOutcome, VlaRequest, VlaStatus, resolve_tier

T0_KINDS = ["navigate_to", "align", "grasp", "place", "handoff"]
VLA_EFFECTS: dict[str, list[FaultEffect]] = {
    "navigate_to": ["nav_blocked", "disconnect", "vla_hang"],
    "align": ["disconnect", "vla_hang"],
    "grasp": ["drop_after_lift", "grasp_miss", "wrong_item", "disconnect", "vla_hang"],
    "place": ["place_miss", "disconnect", "vla_hang"],
    "handoff": ["vla_hang"],
}


class _Fail(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
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
        self.fault_ids: list[str] = []


class FakeVla:
    def __init__(self, services: Any, *, simulate_t1: bool = False) -> None:
        self.s = services
        self.simulate_t1 = simulate_t1
        self._execs: dict[str, _Exec] = {}

    # ---- capabilities ------------------------------------------------------------------------------------------------
    async def capabilities(self, robot_id: str) -> list[VlaCapabilities]:
        try:
            st = await self.s.require("robot").state(robot_id)
        except RobotError as exc:
            raise KeyError(robot_id) from exc
        overlay = getattr(getattr(self.s, "faults", None), "overlay", None)
        return [
            VlaCapabilities(
                robot_id=robot_id,
                tier="t0_scripted",
                available=True,
                backend=st.backend,
                step_kinds=list(T0_KINDS),
                model_id="scripted",
                place_back=overlay is not None,
                cancel_latency_ms=50.0,
            ),
            VlaCapabilities(
                robot_id=robot_id,
                tier="t1_pretrained",
                available=self.simulate_t1,
                reason="" if self.simulate_t1 else "no policy server on vhagar configured",
                backend=st.backend,
                step_kinds=["grasp", "place"],
                model_id="lerobot/smolvla_base (simulated)" if self.simulate_t1 else None,
                policy_host="vhagar",
                action_hz=30.0,
                chunk_size=50,
                cameras=["front", "wrist"],
            ),
            VlaCapabilities(
                robot_id=robot_id,
                tier="t2_finetuned",
                available=False,
                reason="physical SO-101 fine-tune: later",
                step_kinds=["grasp", "place"],
            ),
        ]

    # ---- stream ------------------------------------------------------------------------------------------------------
    async def execute(self, request: VlaRequest, *, after_seq: int = 0):  # type: ignore[no-untyped-def]
        ex = self._execs.get(request.exec_id)
        if ex is None:
            ex = _Exec(request)
            self._execs[request.exec_id] = ex
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
                await ex.cond.wait_for(lambda n=seen: len(ex.record.statuses) > n)
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
            ex.task.cancel(reason or "cancel")
            await asyncio.wait({ex.task})
        terminal = ex.record.terminal
        if terminal is None:  # never started a task (cannot happen) or it died without a terminal
            terminal = await self._emit(ex, phase="cancelled", error=VlaError(code="cancelled", message=reason))
        return terminal

    async def cancel_robot(self, robot_id: str, reason: str = "") -> list[VlaStatus]:
        out = []
        for ex in list(self._execs.values()):
            if ex.request.robot_id == robot_id and ex.record.terminal is None:
                out.append(await self.cancel(ex.request.exec_id, reason))
        return out

    # ---- internals ---------------------------------------------------------------------------------------------------
    async def _emit(self, ex: _Exec, **fields: Any) -> VlaStatus:
        async with ex.cond:
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

    async def _stage(self, ex: _Exec, stage: str, progress: float) -> None:
        ex.stage, ex.progress = stage, progress
        if ex.tier != "t0_scripted":
            ex.chunk += 1
        await self._emit(
            ex, phase="running", stage=stage, progress=progress, chunk_no=ex.chunk if ex.tier != "t0_scripted" else None
        )
        await asyncio.sleep(0)

    async def _heartbeat(self, ex: _Exec) -> None:
        while True:
            await asyncio.sleep(ex.request.heartbeat_s)
            if not ex.silent:
                await self._emit(ex, phase="running", stage=ex.stage, progress=ex.progress, message="heartbeat")

    async def _consult(self, ex: _Exec, tier: VlaTier, effects: list[FaultEffect]) -> dict[str, str]:
        faults = getattr(self.s, "faults", None)
        if faults is None or not effects:
            return {}
        req, step, ctx = ex.request, ex.request.step, ex.request.context
        sku = getattr(getattr(step, "target", None), "sku", None) or getattr(step, "sku", None)
        slot_id = getattr(getattr(step, "target", None), "slot_id", None) or getattr(step, "slot_id", None)
        fctx = FaultContext(
            robot_id=req.robot_id,
            step_kind=step.kind,
            step_id=ctx.step_id,
            step_key=ctx.step_key if ctx.step_id else req.exec_id,
            item_id=ctx.item_id or step.item_id,
            sku=sku,
            slot_id=slot_id,
            tier=tier,
            attempt=ctx.attempt,
            exec_id=req.exec_id,
        )
        out = {f.effect: f.fault_id for f in await faults.consult(fctx, effects)}
        ex.fault_ids.extend(out.values())
        return out

    async def _run(self, ex: _Exec) -> None:
        req, step = ex.request, ex.request.step
        robot = self.s.require("robot")
        t0 = time.monotonic()
        hb: asyncio.Task[None] | None = None
        try:
            try:
                caps = await self.capabilities(req.robot_id)
            except KeyError:
                await self._emit(ex, phase="failed", error=VlaError(code="unknown_robot", message=req.robot_id))
                return
            st = await robot.state(req.robot_id)
            ex.backend = st.backend
            tier = resolve_tier(req.tier, caps, step.kind)
            fell_back: VlaTier | None = None
            if tier != "t0_scripted" and await self._consult(ex, tier, ["policy_unavailable"]):
                if not req.allow_fallback:
                    ex.tier = tier
                    await self._emit(
                        ex,
                        phase="failed",
                        error=VlaError(code="policy_unavailable", tier=tier, message="policy server refused"),
                    )
                    return
                fell_back, tier = tier, "t0_scripted"
            ex.tier = tier
            await self._emit(
                ex,
                phase="accepted",
                fell_back_from=fell_back,
                pose=st.pose,
                message=f"{step.kind} on {tier}" + (f" (fell back from {fell_back})" if fell_back else ""),
            )
            effects = await self._consult(ex, tier, VLA_EFFECTS.get(step.kind, []))
            hb = asyncio.create_task(self._heartbeat(ex))
            async with asyncio.timeout(req.deadline_s):
                if "vla_hang" in effects:
                    ex.silent = True
                    await asyncio.Event().wait()
                claim = await self._act(ex, effects)
            ended = utcnow()
            hb.cancel()
            final = await robot.state(req.robot_id)
            overlay = getattr(getattr(self.s, "faults", None), "overlay", None)
            virtual = overlay.virtual_holding.get(req.robot_id) if overlay is not None else None
            outcome = VlaOutcome(
                self_report="success",
                self_confidence=0.9 if tier != "t0_scripted" else None,
                final_state=final,
                holding_instance_id=virtual.instance_id if virtual else final.holding,
                frames=await self._capture_after(ex),
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
            await self._safe_stop(req.robot_id)
            await self._emit(
                ex,
                phase="cancelled",
                error=VlaError(code="cancelled", tier=ex.tier, message="cancelled"),
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
                error=VlaError(code=exc.code, message=exc.message, tier=ex.tier),  # type: ignore[arg-type]
                outcome=await self._final(ex, t0),
            )
        except Exception as exc:  # noqa: BLE001 - a fake must always end the stream
            await self._emit(ex, phase="failed", error=VlaError(code="internal", message=repr(exc), tier=ex.tier))
        finally:
            if hb is not None:
                hb.cancel()

    async def _safe_stop(self, robot_id: str) -> None:
        try:
            await self.s.require("robot").stop(robot_id)
        except Exception:  # noqa: BLE001
            pass

    async def _final(self, ex: _Exec, t0: float) -> VlaOutcome | None:
        try:
            st = await self.s.require("robot").state(ex.request.robot_id)
        except Exception:  # noqa: BLE001
            return None
        return VlaOutcome(
            self_report="failure",
            final_state=st,
            holding_instance_id=st.holding,
            duration_ms=(time.monotonic() - t0) * 1000,
        )

    async def _capture_after(self, ex: _Exec) -> list[Any]:
        evidence = getattr(self.s, "evidence", None)
        req = ex.request
        if evidence is None or not req.capture_after:
            return []
        step, ctx = req.step, req.context
        slot = getattr(getattr(step, "target", None), "slot_id", None) or getattr(step, "slot_id", None)
        context = {
            "step_id": ctx.step_id,
            "step_key": ctx.step_key,
            "attempt": ctx.attempt,
            "step_kind": step.kind,
            "item_id": ctx.item_id or step.item_id,
            "slot_ids": [slot] if slot else [],
        }
        try:
            aim = slot if "front" in req.capture_after else None
            return await evidence.capture_refs(req.robot_id, req.capture_after, context=context, aim_slot_id=aim)
        except Exception:  # noqa: BLE001 - evidence is best effort; the verifier captures itself
            return []

    async def _neighbour(self, robot_id: str, slot_id: str | None, sku: str) -> str | None:
        """The slot next to ``slot_id`` on the same fixture level holding another SKU (the 'wrong item')."""
        if slot_id is None:
            return None
        store = (await self.s.require("robot").state(robot_id)).store_id or ""
        plan = await self.s.require("catalog").get_planogram(store)
        slot = plan.slot(slot_id)
        if slot is None:
            return None
        for d in (1, -1):
            n = next(
                (
                    s
                    for s in plan.slots
                    if s.fixture_id == slot.fixture_id
                    and s.level == slot.level
                    and s.index == slot.index + d
                    and s.sku != sku
                ),
                None,
            )
            if n is not None:
                return n.slot_id
        return None

    async def _path(self, robot_id: str, goal: Any) -> list[Any]:
        """Classical planner inside the VLA (any tier): a v0.2 RoutingService leg from the robot pose, else straight."""
        routing = getattr(self.s, "routing", None)
        if routing is None:
            return [goal]
        st = await self.s.require("robot").state(robot_id)
        try:
            route = await routing.plan_route(
                RouteRequest(store_id=st.store_id or "", start=st.pose, end=goal, stops=[])
            )
        except Exception:  # noqa: BLE001 - no path service: drive straight (the robot reports path_blocked if it can't)
            return [goal]
        pts = [wp for leg in route.legs for wp in leg.waypoints]
        out = [p for i, p in enumerate(pts) if i == 0 or (p.x, p.y) != (pts[i - 1].x, pts[i - 1].y)]
        return (out[1:] if len(out) > 1 else out) or [goal]

    async def _act(self, ex: _Exec, effects: dict[str, str]) -> str:
        req, step = ex.request, ex.request.step
        robot = self.s.require("robot")
        rid = req.robot_id
        if "disconnect" in effects:
            raise RobotError("disconnected", "Isaac link lost (injected); continuing on the 2D fake")
        if step.kind == "navigate_to":
            if "nav_blocked" in effects:
                await self._stage(ex, "planning_path", 0.1)
                raise RobotError("path_blocked", "another rover blocks the aisle (injected)")
            await self._stage(ex, "planning_path", 0.1)
            waypoints = list(step.waypoints) or await self._path(rid, step.goal_pose)
            await self._stage(ex, "driving", 0.2)
            await robot.navigate(rid, waypoints)
            return f"arrived at {step.target}"
        if step.kind == "align":
            pose = (await robot.state(rid)).pose
            await self._stage(ex, "aligning", 0.3)
            if step.location is not None:
                ap = step.location.approach_pose
                if pose.distance_to(ap) > 0.02 or abs(angle_diff(pose.theta, ap.theta)) > 0.05:
                    await robot.navigate(rid, [ap])
            await self._stage(ex, "lift_preposition", 0.6)
            await self._stage(ex, "aiming_camera", 0.9)
            return f"aligned to {step.slot_id}"
        if step.kind == "grasp":
            target = step.target
            inst = (req.grounding.instance_id if req.grounding else None) or target.instance_id
            await self._stage(ex, "reaching", 0.3)
            if "grasp_miss" in effects:
                await self._stage(ex, "closing", 0.6)
                raise RobotError("grasp_failed", "gripper closed on air (injected)")
            await self._stage(ex, "closing", 0.6)
            if "drop_after_lift" in effects:
                await self._stage(ex, "lifting", 0.9)
                return f"grasped {target.sku}"  # the CLAIM: nothing is attached (the verifier must catch it)
            if "wrong_item" in effects:
                overlay = getattr(getattr(self.s, "faults", None), "overlay", None)
                nslot = await self._neighbour(rid, target.slot_id, target.sku)
                if overlay is not None and nslot is not None:
                    units = await robot.list_instances(rid, nslot)
                    if units:
                        overlay.virtual_holding[rid] = units[0]
                        await self._stage(ex, "lifting", 0.9)
                        return f"grasped {target.sku}"  # the CLAIM; it actually holds the neighbour
            if target.slot_id is None:
                raise _Fail("unsupported_step", "T0 grasp needs a slot")
            await robot.pick(rid, target.slot_id, target.sku, inst)
            await self._stage(ex, "lifting", 0.9)
            return f"grasped {target.sku}"
        if step.kind == "place":
            overlay = getattr(getattr(self.s, "faults", None), "overlay", None)
            await self._stage(ex, "moving_to_basket" if step.destination == "basket" else "moving_to_shelf", 0.4)
            if step.destination == "shelf":
                if overlay is not None and rid in overlay.virtual_holding:
                    del overlay.virtual_holding[rid]
                    await self._stage(ex, "releasing", 0.9)
                    return "put the unit back on the shelf"
                raise _Fail("unsupported_step", "T0 over v0.2 RobotService has no place-back primitive")
            if overlay is not None and rid in overlay.virtual_holding:
                del overlay.virtual_holding[rid]  # a wrong unit 'placed': it never reaches the basket
                return "placed"
            if "place_miss" in effects:
                await self._stage(ex, "releasing", 0.9)
                return f"placed {step.sku}"  # the CLAIM: the unit is still in the gripper
            await robot.place_in_basket(rid)
            await self._stage(ex, "releasing", 0.9)
            return f"placed {step.sku}"
        if step.kind == "handoff":
            await self._stage(ex, "unloading", 0.5)
            units = await robot.unload_basket(rid)
            return f"unloaded {len(units)} units at checkout"
        raise _Fail("unsupported_step", f"{step.kind} is not a VLA step")
