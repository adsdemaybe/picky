"""Ground-truth verifier: the deterministic ``VerifierService`` (``create_verifier_fake``) + the real one's base.

``FakeVerifier`` decides EXACTLY from the frame ground truth (``contracts.verify.evaluate_on_truth``): each camera
atom is judged from what THAT evidence frame showed (its ``GroundTruthSidecar``), state atoms from the truth snapshot,
so a stale frame really shows the past and ``VerifyRequest.not_before`` matters. Truth comes from
``services.truth`` (robot arm) or, when that is not wired, the local rebuild over v0.2 Protocols (``truth.py``).

Verifier-layer demo faults (``services.faults``): ``verifier_unavailable`` -> raises ``VerifierUnavailableError``
(the runner's degrade policy takes over) · ``provider_down`` -> the (simulated) provider failed, ground truth decided,
``degraded=True`` · ``low_confidence`` -> camera atoms answer at ``params.confidence`` (default 0.4) -> uncertain.
Stale frames (captured before ``not_before``) are re-captured once, else their atoms are uncertain.

``decided_by`` is always 'ground_truth' here (honest labelling); ``PerceptionVerifier`` (verifier.py) keeps the same
contract with Perceptron deciding in mode 'decide'.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from contracts.cameras import robot_camera_id
from contracts.common import RobotId, SlotId, Timestamp
from contracts.evidence import FrameRef, GroundTruthSidecar, TruthSnapshot
from contracts.faults import FaultContext
from contracts.grounding import Evidence, GroundedTarget, NormBox, ProviderCall
from contracts.plan import ATOM_SOURCES, Condition, ExecContext, atom_cameras, cameras_needed, iter_atoms, slot_ids_in
from contracts.services import Services
from contracts.verify import (
    AtomVerdict,
    GroundingResult,
    GroundRequest,
    ProviderInfo,
    Verdict,
    VerifierCapabilities,
    VerifierMode,
    VerifierUnavailableError,
    VerifyBaseline,
    VerifyRequest,
    evaluate_on_truth,
    verdict_reason,
)
from utils.log import get_logger

from .truth import LocalTruth, truth_for

log = get_logger(__name__)

VERIFIER_FAULTS = ("verifier_unavailable", "provider_down", "low_confidence")
DEFAULT_LOW_CONFIDENCE = 0.4
CACHE_SIZE = 2000


def exec_ctx_dict(context: ExecContext | None, slot_ids: Sequence[SlotId]) -> dict[str, Any]:
    """Context passed to ``EvidenceCameras.capture_refs`` (fault matching + aiming)."""
    out: dict[str, Any] = {"slot_ids": list(slot_ids)}
    if context is not None:
        out.update(
            step_id=context.step_id,
            step_key=context.step_key,
            step_kind=context.step_kind,
            attempt=context.attempt,
            item_id=context.item_id,
        )
    return out


class VerifierBase:
    """Frames, per-frame ground truth, faults, baseline, ground-truth grounding (shared by fake + real verifier)."""

    provider_label = "fake"

    def __init__(self, services: Services, *, mode: VerifierMode = "shadow") -> None:
        self.s = services
        self.mode: VerifierMode = mode
        self.calls = 0
        self.local = LocalTruth(services)
        self._cache: OrderedDict[str, Verdict] = OrderedDict()

    # ---- cache ---------------------------------------------------------------------------------------------------
    def _cached(self, verify_id: str) -> Verdict | None:
        v = self._cache.get(verify_id)
        if v is not None:
            self._cache.move_to_end(verify_id)
        return v

    def _remember(self, verdict: Verdict) -> Verdict:
        self._cache[verdict.verify_id] = verdict
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return verdict

    # ---- faults / truth ------------------------------------------------------------------------------------------
    async def _faults(
        self, robot_id: RobotId, context: ExecContext | None, effects: Sequence[str]
    ) -> dict[str, dict[str, Any]]:
        """Effects that fire now -> the rule params ({'confidence': .3})."""
        faults = getattr(self.s, "faults", None)
        if faults is None:
            return {}
        try:
            ctx = FaultContext(
                robot_id=robot_id,
                step_kind=context.step_kind if context is not None else None,
                step_id=context.step_id if context is not None else None,
                step_key=context.step_key if context is not None else None,
                attempt=context.attempt if context is not None else 1,
                item_id=context.item_id if context is not None else None,
            )
            out: dict[str, dict[str, Any]] = {}
            for f in await faults.consult(ctx, list(effects)):
                rule_fn = getattr(faults, "rule", None)
                rule = rule_fn(f.fault_id) if callable(rule_fn) else None
                out[f.effect] = dict(rule.params) if rule is not None else {}
            return out
        except Exception as exc:  # noqa: BLE001 - a broken fault injector never breaks a verdict
            log.warning("verifier: fault consult failed (%s)", exc)
            return {}

    async def _truth_now(self, robot_id: RobotId, slot_ids: Sequence[SlotId]) -> TruthSnapshot | None:
        try:
            return await truth_for(self.s, self.local, robot_id, slot_ids)
        except Exception as exc:  # noqa: BLE001 - no truth: the caller decides whether that is fatal
            log.debug("verifier: no ground truth for %s (%s)", robot_id, exc)
            return None

    async def _sidecar(self, frame_id: str) -> GroundTruthSidecar | None:
        """The frame's sidecar from this arm's evidence store, else the robot's TruthSource (KeyError -> None)."""
        for src in (getattr(self.s, "evidence", None), getattr(self.s, "truth", None)):
            fn = getattr(src, "ground_truth", None)
            if fn is None:
                continue
            try:
                return await fn(frame_id)
            except KeyError:
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("verifier: sidecar lookup %s failed (%s)", frame_id, exc)
        return None

    # ---- frames --------------------------------------------------------------------------------------------------
    async def _capture(
        self, robot_id: RobotId, cameras: list[str], context: dict[str, Any], aim: SlotId | None
    ) -> list[FrameRef]:
        evidence = getattr(self.s, "evidence", None)
        if evidence is None or not cameras:
            return []
        try:
            return await evidence.capture_refs(
                robot_id, [c.rsplit(".", 1)[1] for c in cameras], context=context, aim_slot_id=aim
            )
        except Exception as exc:  # noqa: BLE001 - no frames: camera atoms fall back / go uncertain
            log.warning("verifier: capture %s failed (%s)", cameras, exc)
            return []

    async def _frames(
        self,
        robot_id: RobotId,
        cameras: list[str],
        given: Sequence[FrameRef],
        not_before: Timestamp | None,
        context: dict[str, Any],
    ) -> tuple[list[FrameRef], set[str]]:
        """Frames per needed camera (given first, fresh captures for the rest; frames older than ``not_before`` are
        re-captured once) -> (frames in ``cameras`` order, camera ids whose frame is still stale)."""
        aim = (context.get("slot_ids") or [None])[0]
        by_cam: dict[str, FrameRef] = {f.camera_id: f for f in given if f.camera_id in cameras}
        missing = [c for c in cameras if c not in by_cam]
        for ref in await self._capture(robot_id, missing, context, aim):
            by_cam[ref.camera_id] = ref
        stale: set[str] = set()
        if not_before is not None:
            old = [c for c, f in by_cam.items() if f.ts < not_before]
            if old:
                for ref in await self._capture(robot_id, old, context, aim):
                    by_cam[ref.camera_id] = ref
                stale = {c for c, f in by_cam.items() if f.ts < not_before}
        return [by_cam[c] for c in cameras if c in by_cam], stale

    @staticmethod
    def stale_block(cond: Condition, stale: set[str], robot_id: RobotId, alternate: bool) -> set[str]:
        """Every camera of each atom whose judging camera only has a stale frame: those atoms are 'uncertain' (a
        stale frame never passes, and no other camera was captured for them)."""
        out: set[str] = set()
        for _, atom in iter_atoms(cond):
            kinds = atom_cameras(atom)
            if not kinds:
                continue
            cid = robot_camera_id(robot_id, kinds[1] if alternate and len(kinds) > 1 else kinds[0])
            if cid in stale:
                out.update(robot_camera_id(robot_id, k) for k in kinds)
        return out

    async def _frame_truth(
        self,
        truth: TruthSnapshot,
        frames: Sequence[FrameRef],
        stale: set[str],
        cond: Condition | None = None,
        alternate: bool = False,
    ) -> tuple[TruthSnapshot, set[str]]:
        """Truth as the evidence frames showed it: gripper from the wrist frame, basket from the basket frame, shelf
        units from the front frame (a stale frame shows the past). Occluded / stale cameras are marked occluded."""
        occluded: set[str] = set(stale)
        if cond is not None and stale:
            occluded |= self.stale_block(cond, stale, truth.robot_id, alternate)
        seen: dict[str, TruthSnapshot] = {}
        for f in frames:
            sc = await self._sidecar(f.frame_id)
            if sc is None:
                continue
            seen[f.camera_id.rsplit(".", 1)[1]] = sc.truth
            if sc.occluded:
                occluded.add(f.camera_id)
        upd: dict[str, Any] = {"occluded": sorted(occluded | set(truth.occluded))}
        if "wrist" in seen:
            upd["holding"] = seen["wrist"].holding
        if "basket" in seen:
            upd["basket"] = seen["basket"].basket
        if "front" in seen:
            upd["slots"] = {**truth.slots, **{k: v for k, v in seen["front"].slots.items() if k in truth.slots}}
        upd["labels"] = {**truth.labels, **{k: v for t in seen.values() for k, v in t.labels.items()}}
        return truth.model_copy(update=upd), occluded

    @staticmethod
    def _attach_frames(
        cond: Condition, atoms: list[AtomVerdict], frames: Sequence[FrameRef], robot_id: RobotId, alternate: bool
    ) -> list[AtomVerdict]:
        """Point every camera atom's evidence at the frame it was judged on (the UI draws overlays on it)."""
        by_path = dict(iter_atoms(cond))
        by_cam = {f.camera_id: f for f in frames}
        out = list(atoms)
        for i, av in enumerate(out):
            atom = by_path.get(av.path)
            if atom is None or "camera" not in ATOM_SOURCES[atom.op] or av.decided_by == "state":
                continue
            kinds = atom_cameras(atom)
            frame = by_cam.get(robot_camera_id(robot_id, kinds[1 if alternate and len(kinds) > 1 else 0]))
            if frame is None:
                continue
            ev = [e if e.frame is not None else e.model_copy(update={"frame": frame}) for e in av.evidence] or [
                Evidence(
                    kind="state", role="state", frame=frame, text=av.reason, matches_expectation=av.outcome == "pass"
                )
            ]
            out[i] = av.model_copy(update={"evidence": ev})
        return out

    # ---- grounding (ground truth) --------------------------------------------------------------------------------
    async def _ground_frames(self, request: GroundRequest) -> list[FrameRef]:
        frames = list(request.frames)
        if frames:
            return frames
        target = request.target
        ctxd = exec_ctx_dict(request.context, [target.slot_id] if target.slot_id else [])
        return await self._capture(
            request.robot_id, [robot_camera_id(request.robot_id, request.camera)], ctxd, target.slot_id
        )

    async def _gt_ground(
        self, request: GroundRequest, truth: TruthSnapshot, frames: Sequence[FrameRef], t0: float
    ) -> GroundingResult:
        target = request.target
        slot = truth.slots.get(target.slot_id or "")
        units = [u for u in (slot.instances if slot else []) if u.sku == target.sku]
        unit = next((u for u in units if u.instance_id == target.instance_id), None) if target.instance_id else None
        unit = unit or (units[0] if units else None)
        if unit is None:
            return GroundingResult(
                grounding_id=request.grounding_id,
                found=False,
                reason=f"no {target.sku} on {target.slot_id or 'the shelf'}",
                methods_tried=["ground_truth"],
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        frame = frames[0] if frames else None
        box = NormBox(x1=400, y1=300, x2=600, y2=700)
        if frame is not None:
            sc = await self._sidecar(frame.frame_id)
            px = sc.in_view.get(unit.instance_id) if sc is not None else None
            if px is not None:
                box = NormBox.from_px(px, frame.w, frame.h)
        grounded = GroundedTarget(
            grounding_id=request.grounding_id,
            target=target.model_copy(update={"instance_id": unit.instance_id}),
            frame=frame,
            box=box,
            grasp_point=box.center(),
            confidence=0.95,
            decided_by="ground_truth",
            provider=ProviderCall(provider="ground_truth", call="sidecar", prompt=f"Point to the {target.description}"),
            instance_id=unit.instance_id,
            robot_id=request.robot_id,
            robot_pose=truth.pose,
        )
        return GroundingResult(
            grounding_id=request.grounding_id,
            found=True,
            target=grounded,
            reason=f"found {unit.instance_id}",
            methods_tried=["ground_truth"],
            latency_ms=(time.monotonic() - t0) * 1000,
        )

    async def ground(self, request: GroundRequest) -> GroundingResult:
        t0 = time.monotonic()
        target = request.target
        truth = await self._truth_now(request.robot_id, [target.slot_id] if target.slot_id else [])
        if truth is None:
            raise VerifierUnavailableError("no ground truth / robot state to ground against")
        frames = await self._ground_frames(request)
        return await self._gt_ground(request, truth, frames, t0)

    # ---- baseline / info -----------------------------------------------------------------------------------------
    async def baseline(self, robot_id: RobotId, condition: Condition | None, *, baseline_id: str) -> VerifyBaseline:
        if "verifier_unavailable" in await self._faults(robot_id, None, ["verifier_unavailable"]):
            raise VerifierUnavailableError("verifier service unavailable (injected fault)")
        truth = await self._truth_now(robot_id, slot_ids_in(condition))
        if truth is None:
            raise VerifierUnavailableError("no ground truth / robot state for a baseline")
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

    async def providers(self) -> list[ProviderInfo]:
        """GET /api/verifier/providers (not on the Protocol; the gateway calls it when present)."""
        return [
            ProviderInfo(
                provider="fake",
                roles=["grounding", "verification", "label_reading"],
                available=True,
                reason="ground-truth verifier (no vision model)",
                mode=self.mode,
                calls=self.calls,
            )
        ]


class FakeVerifier(VerifierBase):
    """Deterministic ``VerifierService``: exact verdicts from frame ground truth (``create_verifier_fake``)."""

    async def verify(self, request: VerifyRequest) -> Verdict:
        cached = self._cached(request.verify_id)
        if cached is not None:
            return cached
        self.calls += 1
        t0 = time.monotonic()
        effects = await self._faults(request.robot_id, request.context, VERIFIER_FAULTS)
        if "verifier_unavailable" in effects:
            raise VerifierUnavailableError("verifier service unavailable (injected fault)")
        cond = request.condition
        slot_ids = slot_ids_in(cond)
        cams = cameras_needed(cond, request.robot_id, alternate=request.alternate_cameras)
        ctxd = exec_ctx_dict(request.context, slot_ids)
        frames, stale = await self._frames(request.robot_id, cams, request.frames, request.not_before, ctxd)
        truth = await self._truth_now(request.robot_id, slot_ids)
        if truth is None:
            raise VerifierUnavailableError("no TruthSource / robot state: the fake verifier has nothing to judge from")
        truth, _ = await self._frame_truth(truth, frames, stale, cond, request.alternate_cameras)
        low = effects.get("low_confidence")
        outcome, conf, atoms = evaluate_on_truth(
            cond,
            truth,
            baseline=request.baseline,
            bindings=request.bindings,
            camera_confidence=float(low.get("confidence", DEFAULT_LOW_CONFIDENCE)) if low is not None else None,
            approvals=request.approvals,
        )
        atoms = self._attach_frames(cond, atoms, frames, request.robot_id, request.alternate_cameras)
        degraded = "provider_down" in effects
        reason = verdict_reason(outcome, atoms)
        if stale and outcome != "pass":
            reason = f"stale frame (captured before the action finished): {reason}"
        latency = (time.monotonic() - t0) * 1000
        return self._remember(
            Verdict(
                verify_id=request.verify_id,
                robot_id=request.robot_id,
                phase=request.phase,
                outcome=outcome,
                confidence=conf,
                reason=reason,
                atoms=atoms,
                frames=frames,
                decided_by="ground_truth",
                provider=ProviderCall(provider="fake", call="sidecar", latency_ms=latency),
                ground_truth_agrees=True,
                degraded=degraded,
                methods_tried=["perceptron", "ground_truth"] if degraded else ["fake"],
                latency_ms=latency,
            )
        )


def create_verifier_fake(services: Services) -> FakeVerifier:
    return FakeVerifier(services)


__all__ = ["VERIFIER_FAULTS", "FakeVerifier", "VerifierBase", "create_verifier_fake", "exec_ctx_dict"]
