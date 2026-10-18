"""Gateway side of the planner -> VLA -> verifier loop (contracts 0.2.1, ``contracts/api.md`` "Planner -> VLA ->
verifier loop"): the REST routes over the OPTIONAL loop services (``runner``, ``planner``, ``vla``, ``verifier``,
``evidence``, ``faults``) and the evidence-annotation index.

- Every route answers 503 when its service is not wired (``exec_mode`` = direct), 404 for unknown / expired ids.
- ``GET /api/evidence/{frame_id}.jpg`` serves ONLY face-blurred frames (``CameraFrame.blurred``), bytes only: the
  frame's ground-truth ``instances`` never leave the gateway.
- Goal-backed plans are cancelled through ``fleet.cancel_goal`` (the fleet owns goal status and the brain task);
  only goal-less dev plans go to ``runner.cancel``.
- Dev routes (``/api/dev/faults*``, ``/api/dev/plans``) exist only with ``PICKY_DEV=1`` (404 otherwise).
- ``EvidenceAnnotations`` is fed by the publisher (``step.verified`` / ``target.grounded`` / ``camera.overlay``) and by
  ``POST /api/verify``, and answers ``GET /api/evidence/{frame_id}/annotations``.
"""

import asyncio
import contextlib
import io
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Response
from pydantic import Field, ValidationError

from contracts.common import Model
from contracts.evidence import FrameAnnotations, FrameRef
from contracts.faults import FaultRule, FaultScenario
from contracts.plan import PlanRequest, StepPlan
from contracts.policy import PlanRunState, StepAttempt
from contracts.verify import (
    ProviderInfo,
    Verdict,
    VerifierCapabilities,
    VerifierMode,
    VerifyNowRequest,
    VerifyRequest,
    verify_deadline,
)
from contracts.vla import ExecRecord, VlaCapabilities, VlaStatus
from utils.config import get_settings
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)
MAX_ANNOTATED_FRAMES = 500
CANCEL_SETTLE_S = 2.0
"""How long POST /api/plans/{id}/cancel waits for the run state to turn terminal after fleet.cancel_goal."""


# ----------------------------------------------------------------------------------------------------------------------
# request bodies (gateway-local shapes of contracts/api.md: `{reason?}`, `{mode}`)
# ----------------------------------------------------------------------------------------------------------------------
class ReasonBody(Model):
    reason: str = ""


class VerifierModeBody(Model):
    mode: VerifierMode


class FaultsState(Model):
    """GET /api/dev/faults."""

    scenario: str | None = None
    rules: list[FaultRule] = Field(default_factory=list)
    firings: list[Any] = Field(default_factory=list)


# ----------------------------------------------------------------------------------------------------------------------
# evidence annotations
# ----------------------------------------------------------------------------------------------------------------------
class EvidenceAnnotations:
    """frame_id -> everything drawn on that evidence frame (boxes / points / texts from verdicts and groundings).
    Bounded to the last ``max_frames`` frames; fed synchronously by ``GatewayPublisher`` (never raises)."""

    def __init__(self, max_frames: int = MAX_ANNOTATED_FRAMES) -> None:
        self.max_frames = max_frames
        self._frames: OrderedDict[str, FrameAnnotations] = OrderedDict()

    def get(self, frame_id: str) -> FrameAnnotations | None:
        return self._frames.get(frame_id)

    def observe(self, event: Any) -> None:
        try:
            t = getattr(event, "type", None)
            if t == "step.verified":
                self.add_verdict(event.verdict)
            elif t == "target.grounded":
                self.add_grounding(event.grounding)
            elif t == "camera.overlay":
                self._entry(event.frame).evidence.extend(e.model_dump(mode="json") for e in event.evidence)
        except Exception as exc:  # noqa: BLE001 - an overlay index must never break publishing
            log.debug("annotation index skipped %s: %s", getattr(event, "type", "?"), exc)

    def add_verdict(self, verdict: Verdict) -> None:
        for ref in verdict.frames:
            entry = self._entry(ref)
            if verdict.verify_id not in entry.verify_ids:
                entry.verify_ids.append(verdict.verify_id)
        for atom in verdict.atoms:
            for ev in atom.evidence:
                if ev.frame is None:
                    continue
                entry = self._entry(ev.frame)
                entry.evidence.append(ev.model_dump(mode="json"))
                if verdict.verify_id not in entry.verify_ids:
                    entry.verify_ids.append(verdict.verify_id)

    def add_grounding(self, grounding: Any) -> None:
        if grounding.frame is None:
            return
        entry = self._entry(grounding.frame)
        if grounding.grounding_id in entry.grounding_ids:
            return
        entry.grounding_ids.append(grounding.grounding_id)
        entry.evidence.append(
            {
                "kind": "box" if grounding.box is not None else "point",
                "role": "target",
                "mention": grounding.target.description,
                "sku": grounding.target.sku,
                "box": grounding.box.model_dump(mode="json") if grounding.box is not None else None,
                "point": grounding.grasp_point.model_dump(mode="json") if grounding.grasp_point is not None else None,
                "instance_id": grounding.instance_id,
                "confidence": grounding.confidence,
                "matches_expectation": True,
            }
        )

    def _entry(self, ref: FrameRef) -> FrameAnnotations:
        entry = self._frames.get(ref.frame_id)
        if entry is None:
            entry = FrameAnnotations(frame=ref)
            self._frames[ref.frame_id] = entry
            while len(self._frames) > self.max_frames:
                self._frames.popitem(last=False)
        else:
            self._frames.move_to_end(ref.frame_id)
        return entry


# ----------------------------------------------------------------------------------------------------------------------
# routes
# ----------------------------------------------------------------------------------------------------------------------
def mount_loop_routes(app: FastAPI, reg: Callable[[], Any]) -> None:
    """Add the loop routes to ``app``; ``reg()`` returns the live ``Registry`` (503 before startup)."""
    dev_tasks: set[asyncio.Task[Any]] = set()

    def loop_svc(name: str) -> Any:
        s = getattr(reg().services, name, None)
        if s is None:
            raise HTTPException(503, f"{name} service not wired (exec_mode={reg().exec_mode})")
        return s

    def require_dev() -> None:
        if not get_settings().picky_dev:
            raise HTTPException(404, "not found (dev routes need PICKY_DEV=1)")

    # ------------------------------------------------------------------------------------------------- plans (P0)
    @app.get("/api/goals/{goal_id}/plan", response_model=StepPlan)
    async def goal_plan(goal_id: str) -> StepPlan:
        runner = loop_svc("runner")
        plan = await runner.current_plan(goal_id)
        if plan is None:
            raise HTTPException(404, f"no plan for goal {goal_id} (direct mode or not planned yet)")
        return plan

    @app.get("/api/plans/{plan_id}", response_model=StepPlan)
    async def get_plan(plan_id: str) -> StepPlan:
        return await loop_svc("runner").get_plan(plan_id)

    @app.get("/api/plans/{plan_id}/run", response_model=PlanRunState)
    async def plan_run(plan_id: str) -> PlanRunState:
        return await loop_svc("runner").run_state(plan_id)

    @app.get("/api/plans/{plan_id}/attempts", response_model=list[StepAttempt])
    async def plan_attempts(plan_id: str, step_id: str | None = None) -> list[StepAttempt]:
        return await loop_svc("runner").attempts(plan_id, step_id)

    @app.post("/api/plans/{plan_id}/cancel", response_model=PlanRunState)
    async def cancel_plan(plan_id: str, req: ReasonBody | None = Body(None)) -> PlanRunState:  # noqa: B008
        runner = loop_svc("runner")
        reason = (req.reason if req is not None else "") or "cancelled from the web app"
        plan = await runner.get_plan(plan_id)
        fleet = reg().services.fleet
        goal = None
        if fleet is not None:
            with contextlib.suppress(KeyError):
                goal = await fleet.get_goal(plan.goal_id)
        if goal is None:  # goal-less dev plan (POST /api/dev/plans)
            return await runner.cancel(plan_id, reason)
        if not goal.terminal:
            await fleet.cancel_goal(plan.goal_id, reason)
        state = await runner.run_state(plan_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CANCEL_SETTLE_S
        while state.status.value in ("running", "ready", "draft") and loop.time() < deadline:
            await asyncio.sleep(0.05)
            state = await runner.run_state(plan_id)
        return state

    # ---------------------------------------------------------------------------------------------------- evidence
    @app.get("/api/evidence/{frame_id}.jpg", response_class=Response)
    async def evidence_jpeg(frame_id: str, w: int | None = None) -> Response:
        evidence = loop_svc("evidence")
        try:
            frame = await evidence.get_frame(frame_id)
        except KeyError:
            raise HTTPException(404, f"evidence frame {frame_id} unknown or expired") from None
        jpeg = _blurred_jpeg(frame)
        if jpeg is None:
            raise HTTPException(503, f"evidence frame {frame_id} is not face-blurred; not served")
        if w:
            jpeg = await asyncio.to_thread(_resize_jpeg, jpeg, max(16, min(w, 4096)))
        return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=600"})

    @app.get("/api/evidence/{frame_id}/annotations", response_model=FrameAnnotations)
    async def evidence_annotations(frame_id: str) -> FrameAnnotations:
        entry = reg().annotations.get(frame_id)
        if entry is None:
            raise HTTPException(404, f"no annotations for evidence frame {frame_id}")
        return entry

    # --------------------------------------------------------------------------------------------------------- VLA
    @app.get("/api/fleet/robots/{robot_id}/vla", response_model=list[VlaCapabilities])
    async def robot_vla(robot_id: str) -> list[VlaCapabilities]:
        return await loop_svc("vla").capabilities(robot_id)

    @app.get("/api/vla/execs/{exec_id}", response_model=ExecRecord)
    async def vla_exec(exec_id: str) -> ExecRecord:
        return await loop_svc("vla").get_exec(exec_id)

    @app.post("/api/vla/execs/{exec_id}/cancel", response_model=VlaStatus)
    async def vla_cancel(exec_id: str, req: ReasonBody | None = Body(None)) -> VlaStatus:  # noqa: B008
        return await loop_svc("vla").cancel(exec_id, (req.reason if req is not None else "") or "cancelled via API")

    # ---------------------------------------------------------------------------------------------------- verifier
    @app.get("/api/verifier", response_model=VerifierCapabilities)
    async def verifier_caps() -> VerifierCapabilities:
        return await loop_svc("verifier").capabilities()

    @app.put("/api/verifier/mode", response_model=VerifierCapabilities)
    async def verifier_mode(req: VerifierModeBody) -> VerifierCapabilities:
        return await loop_svc("verifier").set_mode(req.mode)

    @app.get("/api/verifier/providers", response_model=list[ProviderInfo])
    async def verifier_providers() -> list[ProviderInfo]:
        verifier = loop_svc("verifier")
        providers = getattr(verifier, "providers", None)  # gateway extra (not in the VerifierService Protocol)
        if providers is not None:
            return list(await providers())
        caps = await verifier.capabilities()
        return [
            ProviderInfo(
                provider=caps.provider,
                model=caps.model,
                roles=["verification"],
                available=caps.healthy,
                reason=caps.reason or None,
                mode=caps.mode,
            )
        ]

    @app.post("/api/verify", response_model=Verdict)
    async def verify_now(req: VerifyNowRequest) -> Verdict:
        verifier = loop_svc("verifier")
        store_id = None
        fleet = reg().services.fleet
        if fleet is not None:
            store_id = (await fleet.get_robot(req.robot_id)).store_id  # KeyError / RobotError -> 404
        timeout = float(get_settings().picky_verify_timeout_s or 8.0)
        request = VerifyRequest(
            verify_id=new_id("vfy"),
            robot_id=req.robot_id,
            store_id=store_id,
            phase="verify",
            condition=req.resolved_condition(),
            mode=req.mode,
            deadline_s=verify_deadline(timeout),
        )
        try:
            verdict = await asyncio.wait_for(verifier.verify(request), timeout=timeout)
        except TimeoutError:
            raise HTTPException(504, f"verifier did not answer within {timeout:.0f} s") from None
        reg().annotations.add_verdict(verdict)
        return verdict

    # --------------------------------------------------------------------------------------------- dev (PICKY_DEV)
    @app.get("/api/dev/faults", response_model=FaultsState)
    async def dev_faults() -> FaultsState:
        require_dev()
        faults = loop_svc("faults")
        rules = list(getattr(faults, "rules", []) or [])  # InMemoryFaultInjector extras
        return FaultsState(scenario=getattr(faults, "scenario", None), rules=rules, firings=await faults.firings())

    @app.post("/api/dev/faults", response_model=FaultRule)
    async def dev_inject(rule: FaultRule) -> FaultRule:
        require_dev()
        return await loop_svc("faults").inject(rule)

    @app.delete("/api/dev/faults", status_code=204, response_class=Response)
    async def dev_clear() -> Response:
        require_dev()
        await loop_svc("faults").clear_faults()
        return Response(status_code=204)

    @app.post("/api/dev/faults/scenario", response_model=FaultScenario)
    async def dev_scenario(body: dict[str, Any] = Body(...)) -> FaultScenario:  # noqa: B008
        require_dev()
        faults = loop_svc("faults")
        if "rules" in body or "description" in body:
            try:
                scenario: FaultScenario | str = FaultScenario.model_validate(body)
            except ValidationError as exc:
                raise HTTPException(422, str(exc)) from None
        else:
            name = str(body.get("name") or "").strip()
            if not name:
                raise HTTPException(422, "give {name} (a DEMO_SCENARIOS key) or a full FaultScenario")
            scenario = name
        try:
            return await faults.load_scenario(scenario)
        except KeyError:
            raise HTTPException(404, f"unknown fault scenario {scenario!r}") from None

    @app.post("/api/dev/plans", response_model=StepPlan)
    async def dev_plan(req: PlanRequest) -> StepPlan:
        require_dev()
        planner, runner = loop_svc("planner"), loop_svc("runner")
        plan = await planner.plan(req)

        async def run() -> None:
            try:
                await runner.run_plan(plan)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a dev plan never takes the gateway down
                log.warning("dev plan %s ended with %s", plan.plan_id, exc)
                events = reg().services.events
                if events is not None:
                    from contracts.events import ErrorEvent

                    with contextlib.suppress(Exception):
                        await events.publish(
                            ErrorEvent(
                                trip_id=plan.trip_id,
                                robot_id=plan.robot_id,
                                store_id=plan.store_id,
                                source="gateway",
                                arm="agent",
                                message=f"dev plan {plan.plan_id} failed: {exc}",
                            )
                        )

        task = asyncio.get_running_loop().create_task(run(), name=f"dev-plan-{plan.plan_id}")
        dev_tasks.add(task)
        task.add_done_callback(dev_tasks.discard)
        return plan


def _blurred_jpeg(frame: Any) -> bytes | None:
    """JPEG bytes ONLY for a face-blurred frame (CLAUDE.md §8)."""
    import base64

    if frame is None or not getattr(frame, "blurred", False) or not getattr(frame, "jpeg_b64", ""):
        return None
    try:
        return base64.b64decode(frame.jpeg_b64)
    except (ValueError, TypeError):
        return None


def _resize_jpeg(jpeg: bytes, width: int) -> bytes:
    """Downscale to ``width`` px (never upscale); the original on any decode error."""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(jpeg)) as img:
            if img.width <= width:
                return jpeg
            height = max(1, round(img.height * width / img.width))
            out = io.BytesIO()
            img.convert("RGB").resize((width, height)).save(out, format="JPEG", quality=80)
            return out.getvalue()
    except Exception:  # noqa: BLE001
        return jpeg


__all__ = ["EvidenceAnnotations", "FaultsState", "ReasonBody", "VerifierModeBody", "mount_loop_routes"]
