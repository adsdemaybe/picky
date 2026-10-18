"""FakeVerifier: the deterministic ``VerifierService``. Exact verdicts from the ground-truth sidecar / TruthSource
(``verify.evaluate_on_truth``), real evidence frames from ``EvidenceCameras``, and the verifier-layer faults:

- verifier_unavailable -> raises ``VerifierUnavailableError`` (the runner's degrade policy takes over)
- provider_down        -> the (simulated) Perceptron call fails; ground truth decides; ``degraded=True``
- low_confidence       -> camera atoms answer at ``params.confidence`` (default 0.4) -> uncertain
- stale frames         -> a frame captured before ``not_before`` is re-captured once, else its atoms are uncertain

``decided_by`` is always 'ground_truth' here (honest labelling); the real verifier keeps the same contract with a
``VisionProvider`` (Perceptron) deciding in mode 'decide'.
"""

import time
from typing import Any

from contracts.cameras import robot_camera_id
from contracts.evidence import FrameRef
from contracts.faults import FaultContext
from contracts.grounding import Evidence, GroundedTarget, NormBox, ProviderCall
from contracts.plan import ATOM_SOURCES, Condition, atom_cameras, cameras_needed, iter_atoms, slot_ids_in
from contracts.verify import (
    GroundingResult,
    GroundRequest,
    Verdict,
    VerifierCapabilities,
    VerifierMode,
    VerifierUnavailableError,
    VerifyBaseline,
    VerifyRequest,
    evaluate_on_truth,
    verdict_reason,
)


class FakeVerifier:
    def __init__(self, services: Any, *, mode: VerifierMode = "shadow") -> None:
        self.s = services
        self.mode: VerifierMode = mode
        self.calls = 0
        self._cache: dict[str, Verdict] = {}

    def _truth_source(self) -> Any:
        truth = getattr(self.s, "truth", None)
        if truth is None:
            raise VerifierUnavailableError("no TruthSource wired: the fake verifier has nothing to judge from")
        return truth

    async def _faults(self, req_robot: str, context: Any, effects: list[str]) -> dict[str, Any]:
        faults = getattr(self.s, "faults", None)
        if faults is None:
            return {}
        ctx = FaultContext(
            robot_id=req_robot,
            step_kind=getattr(context, "step_kind", None),
            step_id=getattr(context, "step_id", None),
            step_key=context.step_key if context is not None else None,
            attempt=getattr(context, "attempt", 1) or 1,
            item_id=getattr(context, "item_id", None),
        )
        out: dict[str, Any] = {}
        for f in await faults.consult(ctx, effects):
            rule = faults.rule(f.fault_id) if hasattr(faults, "rule") else None
            out[f.effect] = rule.params if rule is not None else {}
        return out

    async def _frames(
        self, robot_id: str, cameras: list[str], given: list[FrameRef], not_before: Any, context: dict[str, Any]
    ) -> tuple[list[FrameRef], set[str]]:
        """Frames per needed camera (given first, fresh captures for the rest) -> (frames, stale camera ids)."""
        evidence = getattr(self.s, "evidence", None)
        aim = (context.get("slot_ids") or [None])[0]
        by_cam: dict[str, FrameRef] = {f.camera_id: f for f in given}
        missing = [c for c in cameras if c not in by_cam]
        if missing and evidence is not None:
            for ref in await evidence.capture_refs(
                robot_id, [c.rsplit(".", 1)[1] for c in missing], context=context, aim_slot_id=aim
            ):
                by_cam[ref.camera_id] = ref
        stale: set[str] = set()
        if not_before is not None and evidence is not None:
            old = [c for c, f in by_cam.items() if f.ts < not_before]
            if old:
                for ref in await evidence.capture_refs(
                    robot_id, [c.rsplit(".", 1)[1] for c in old], context=context, aim_slot_id=aim
                ):
                    by_cam[ref.camera_id] = ref
                stale = {c for c, f in by_cam.items() if f.ts < not_before}
        return [by_cam[c] for c in cameras if c in by_cam], stale

    async def verify(self, request: VerifyRequest) -> Verdict:
        if request.verify_id in self._cache:
            return self._cache[request.verify_id]
        self.calls += 1
        t0 = time.monotonic()
        effects = await self._faults(
            request.robot_id, request.context, ["verifier_unavailable", "provider_down", "low_confidence"]
        )
        if "verifier_unavailable" in effects:
            raise VerifierUnavailableError("verifier service unavailable (injected fault)")
        truth_src = self._truth_source()
        cond = request.condition
        cams = cameras_needed(cond, request.robot_id, alternate=request.alternate_cameras)
        ctxd: dict[str, Any] = {"slot_ids": slot_ids_in(cond)}
        if request.context is not None:
            ctxd.update(
                step_id=request.context.step_id,
                step_key=request.context.step_key,
                attempt=request.context.attempt,
                item_id=request.context.item_id,
            )
        ctxd["step_kind"] = request.context.step_kind if request.context is not None else None
        frames, stale = await self._frames(request.robot_id, cams, list(request.frames), request.not_before, ctxd)
        occluded: set[str] = set(stale)
        seen: dict[str, Any] = {}
        for f in frames:
            try:
                sc = await truth_src.ground_truth(f.frame_id)
            except KeyError:
                continue
            seen[f.camera_id.rsplit(".", 1)[1]] = sc.truth
            if sc.occluded:
                occluded.add(f.camera_id)
        truth = await truth_src.truth(request.robot_id, slot_ids_in(cond))
        # judge each camera's atoms from what THAT frame showed (a stale frame shows the past: not_before matters)
        upd: dict[str, Any] = {"occluded": sorted(occluded | set(truth.occluded))}
        if "wrist" in seen:
            upd["holding"] = seen["wrist"].holding
        if "basket" in seen:
            upd["basket"] = seen["basket"].basket
        if "front" in seen:
            upd["slots"] = {**truth.slots, **{k: v for k, v in seen["front"].slots.items() if k in truth.slots}}
        upd["labels"] = {**truth.labels, **{k: v for t in seen.values() for k, v in t.labels.items()}}
        truth = truth.model_copy(update=upd)
        low = effects.get("low_confidence")
        outcome, conf, atoms = evaluate_on_truth(
            cond,
            truth,
            baseline=request.baseline,
            bindings=request.bindings,
            camera_confidence=float(low.get("confidence", 0.4)) if low is not None else None,
            approvals=request.approvals,
        )
        by_cam = {f.camera_id: f for f in frames}
        for i, av in enumerate(atoms):
            atom = dict(iter_atoms(cond)).get(av.path)
            if atom is None or "camera" not in ATOM_SOURCES[atom.op]:
                continue
            kinds = atom_cameras(atom)
            frame = by_cam.get(
                robot_camera_id(request.robot_id, kinds[1 if request.alternate_cameras and len(kinds) > 1 else 0])
            )
            if frame is None:
                continue
            ev = [e.model_copy(update={"frame": frame}) for e in av.evidence] or [
                Evidence(
                    kind="state", role="state", frame=frame, text=av.reason, matches_expectation=av.outcome == "pass"
                )
            ]
            atoms[i] = av.model_copy(update={"evidence": ev})
        degraded = "provider_down" in effects
        reason = verdict_reason(outcome, atoms)
        if stale and outcome != "pass":
            reason = f"stale frame (captured before the action finished): {reason}"
        latency = (time.monotonic() - t0) * 1000
        verdict = Verdict(
            verify_id=request.verify_id,
            robot_id=request.robot_id,
            phase=request.phase,
            outcome=outcome,
            confidence=conf,
            reason=reason,
            atoms=atoms,
            frames=frames,
            decided_by="ground_truth",
            provider=ProviderCall(
                provider="fake",
                call="sidecar",
                latency_ms=latency,
                error_code=None,
            ),
            ground_truth_agrees=True,
            degraded=degraded,
            methods_tried=["perceptron", "ground_truth"] if degraded else ["fake"],
            latency_ms=latency,
        )
        self._cache[request.verify_id] = verdict
        return verdict

    async def ground(self, request: GroundRequest) -> GroundingResult:
        t0 = time.monotonic()
        truth_src = self._truth_source()
        target = request.target
        truth = await truth_src.truth(request.robot_id, [target.slot_id] if target.slot_id else [])
        slot = truth.slots.get(target.slot_id or "")
        if target.slot_id is None:  # 0.2.4 checkout unload: the unit is in the robot's basket (basket camera)
            units = [u for u in truth.basket if u.sku == target.sku]
        else:
            units = [u for u in (slot.instances if slot else []) if u.sku == target.sku]
        unit = next((u for u in units if u.instance_id == target.instance_id), None) if target.instance_id else None
        unit = unit or (units[0] if units else None)
        if unit is None:
            return GroundingResult(
                grounding_id=request.grounding_id,
                found=False,
                reason=f"no {target.sku} on {target.slot_id}" if target.slot_id else f"no {target.sku} in the basket",
                methods_tried=["fake"],
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        frames = list(request.frames)
        evidence = getattr(self.s, "evidence", None)
        if not frames and evidence is not None:
            ctxd = {"slot_ids": [target.slot_id] if target.slot_id else []}
            if request.context is not None:
                ctxd.update(
                    step_id=request.context.step_id,
                    step_key=request.context.step_key,
                    attempt=request.context.attempt,
                    step_kind=request.context.step_kind,
                )
            frames = await evidence.capture_refs(
                request.robot_id, [request.camera], context=ctxd, aim_slot_id=target.slot_id
            )
        frame = frames[0] if frames else None
        box = NormBox(x1=400, y1=300, x2=600, y2=700)
        if frame is not None:
            try:
                sc = await truth_src.ground_truth(frame.frame_id)
                px = sc.in_view.get(unit.instance_id)
                if px is not None:
                    box = NormBox.from_px(px, frame.w, frame.h)
            except KeyError:
                pass
        grounded = GroundedTarget(
            grounding_id=request.grounding_id,
            target=target.model_copy(update={"instance_id": unit.instance_id}),
            frame=frame,
            box=box,
            grasp_point=box.center(),
            confidence=0.95,
            decided_by="ground_truth",
            provider=ProviderCall(provider="fake", call="sidecar", prompt=f"Point to the {target.description}"),
            instance_id=unit.instance_id,
            robot_id=request.robot_id,
            robot_pose=truth.pose,
        )
        return GroundingResult(
            grounding_id=request.grounding_id,
            found=True,
            target=grounded,
            reason=f"found {unit.instance_id}",
            methods_tried=["fake"],
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    async def baseline(self, robot_id: str, condition: Condition | None, *, baseline_id: str) -> VerifyBaseline:
        if "verifier_unavailable" in await self._faults(robot_id, None, ["verifier_unavailable"]):
            raise VerifierUnavailableError("verifier service unavailable (injected fault)")
        truth = await self._truth_source().truth(robot_id, slot_ids_in(condition))
        return VerifyBaseline(
            baseline_id=baseline_id,
            robot_id=robot_id,
            basket_counts=truth.basket_counts(),
            slot_counts={sid: len(s.instances) for sid, s in truth.slots.items()},
            holding_sku=truth.holding.sku if truth.holding else None,
            holding_instance_id=truth.holding.instance_id if truth.holding else None,
        )

    async def capabilities(self) -> VerifierCapabilities:
        return VerifierCapabilities(mode=self.mode, methods=["fake", "ground_truth", "state"], provider="fake")

    async def set_mode(self, mode: VerifierMode) -> VerifierCapabilities:
        self.mode = mode
        return await self.capabilities()
