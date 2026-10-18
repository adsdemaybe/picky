"""``PerceptionVerifier``: the real ``VerifierService`` (``create_verifier``), Perceptron as its vision provider.

    verify(req), total budget req.deadline_s (never overrun):
      1. state atoms from RobotState (free) -> ``verify.evaluate_on_state`` leaves
      2. frames: the VLA's frames captured at/after ``not_before``; fresh captures for the rest (front aimed)
      3. ground truth (sim): ``evaluate_on_truth`` on what each evidence frame showed (exact; shadow / fallback)
      4. vision (mode decide | shadow): provider calls per camera frame, all in parallel, each bounded by
         min(PICKY_VISION_CALL_TIMEOUT_S, time left) -> ``judges`` turn answers into camera-atom verdicts
      5. fuse dual-source atoms (gripper / basket) with ``combine_verdicts`` (camera overrides state only at >= 0.85)
      6. mode: decide = the fused verdict decides (``decided_by='provider'``, attribution "Perceptron perceptron-mk1.5")
               shadow = ground truth decides, the fused opinion rides along in ``Verdict.shadow`` + agreement
               gt_only = no provider calls

Degrade (never raises for provider problems): a camera atom without a usable answer (no key, provider_down fault,
breaker open, timeout, rate limit, out of budget, stale / missing frame) falls back to sim ground truth, else to robot
state (dual-source atoms) or 'uncertain' (camera-only atoms); the verdict is then ``degraded`` with
``methods_tried=['perceptron', 'ground_truth' | 'state']``. ``VerifierUnavailableError`` only when there is neither
robot state nor ground truth (or the ``verifier_unavailable`` demo fault fires).

Mode: ``VerifyRequest.mode`` > ``set_mode`` / PICKY_VERIFIER_MODE > ``DEFAULT_VERIFIER_MODE[robot backend]`` (shadow on
2D-fake stores, where Pillow shelves can fool a VLM; decide on Isaac).
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter
from collections.abc import Awaitable, Sequence
from typing import Any, TypeVar

from contracts.cameras import robot_camera_id
from contracts.common import RobotId, Sku, SlotId
from contracts.evidence import FrameRef, SlotTruth
from contracts.grounding import Evidence, GroundedTarget, NormBox, ProviderCall, point_in_box
from contracts.plan import ATOM_SOURCES, Atom, Condition, atom_cameras, cameras_needed, iter_atoms, slot_ids_in
from contracts.services import Services, VisionProvider
from contracts.sim import CameraFrame
from contracts.verify import (
    DEFAULT_VERIFIER_MODE,
    DEFAULT_VISION_CALL_TIMEOUT_S,
    AtomVerdict,
    GroundingResult,
    GroundRequest,
    ProviderInfo,
    ShadowOpinion,
    StateFacts,
    Verdict,
    VerifierCapabilities,
    VerifierMode,
    VerifierUnavailableError,
    VerifyBaseline,
    VerifyRequest,
    combine_verdicts,
    evaluate,
    evaluate_on_state,
    evaluate_on_truth,
    verdict_reason,
)
from utils.log import get_logger

from .judges import (
    GENERIC_HELD_CLASS,
    ONE_CUE_CONFIDENCE,
    TWO_CUES_CONFIDENCE,
    CameraAnswers,
    judge_basket_contains,
    judge_basket_delta,
    judge_gripper,
    judge_item_on_counter,
    judge_item_on_shelf,
    judge_label,
    judge_shelf_tag,
    judge_slot_visible,
)
from .truth import product_label
from .verifier_fake import DEFAULT_LOW_CONFIDENCE, VERIFIER_FAULTS, VerifierBase, exec_ctx_dict
from .vision.common import ProviderStats, answer_ok, failed_answer, provider_method

log = get_logger(__name__)
T = TypeVar("T")

EMPTY_QUESTION = "Look at the robot gripper. Is the gripper empty, with nothing held between its jaws?"
STATE_AUTHORITATIVE_GRIPPER_BACKENDS = frozenset({"isaac"})
"""Robot backends whose gripper state is ground truth (the simulator's attach): a disagreeing camera never overrides it
for gripper_holding / gripper_empty (it does on the 2D fake overlay and on a real robot)."""
LABEL_PROMPT = "Read the brand, product name and size on the label of the item held in the gripper."
TAG_PROMPT = "Read the price on the shelf tag under the {name}."
GRASP_POINT_PROMPT = "Point to the best grasp spot on the {description}."
SAFETY_S = 0.15
"""Kept back from every budget for the final (pure-code) verdict assembly."""
MIN_CALL_BUDGET_S = 0.25
NEIGHBOR_RADIUS_M = 1.2
"""Slots whose approach pose is this close to the robot add their product as a detect class (the likely confusers:
the jar next to the mild salsa), from the planogram, never from ground truth."""
MAX_CLASSES = 8
BREAKER_FAILURES = 3
BREAKER_COOLDOWN_S = 30.0
GRIPPER_OPS = frozenset({"gripper_holding", "gripper_empty"})
BASKET_OPS = frozenset({"basket_delta", "basket_contains"})
SHELF_OPS = frozenset({"item_on_shelf", "slot_visible", "item_on_counter"})


async def _bounded(aw: Awaitable[T], end: float, default: T) -> T:
    left = end - time.monotonic()
    if left <= 0:
        if asyncio.iscoroutine(aw):
            aw.close()
        return default
    try:
        return await asyncio.wait_for(aw, timeout=left)
    except TimeoutError:
        return default


class PerceptionVerifier(VerifierBase):
    """Real verifier: robot state + Perceptron (``VisionProvider``) + sim ground truth, deadline-bounded."""

    def __init__(
        self,
        services: Services,
        provider: VisionProvider,
        *,
        mode: VerifierMode | None = None,
        call_timeout_s: float = DEFAULT_VISION_CALL_TIMEOUT_S,
        breaker_failures: int = BREAKER_FAILURES,
        breaker_cooldown_s: float = BREAKER_COOLDOWN_S,
    ) -> None:
        super().__init__(services, mode=mode or "shadow")
        self._mode_override: VerifierMode | None = mode
        self.provider = provider
        self.method = provider_method(provider.name)
        self.call_timeout_s = call_timeout_s
        self.breaker_failures = breaker_failures
        self.breaker_cooldown_s = breaker_cooldown_s
        self.stats = ProviderStats()
        self._fails = 0
        self._breaker_until = 0.0
        self._labels: dict[Sku, str] = {}

    # ---- mode / health -------------------------------------------------------------------------------------------
    def resolve_mode(self, requested: VerifierMode | None, backend: str | None) -> VerifierMode:
        return requested or self._mode_override or DEFAULT_VERIFIER_MODE.get(backend or "none", "gt_only")

    def _provider_reason(self) -> str | None:
        fn = getattr(self.provider, "unavailable_reason", None)
        reason = fn() if callable(fn) else None
        if reason:
            return reason
        if self._breaker_until > time.monotonic():
            return f"circuit open after {self.breaker_failures} failed calls (retry in {self._breaker_left():.0f} s)"
        return None

    def _breaker_left(self) -> float:
        return max(0.0, self._breaker_until - time.monotonic())

    def _block_code(self, effects: dict[str, Any]) -> str | None:
        """Why no provider call is made right now (None = go ahead)."""
        if "provider_down" in effects:
            return "provider_down"
        fn = getattr(self.provider, "unavailable_reason", None)
        if callable(fn) and fn():
            return "unavailable"
        if self._breaker_until > time.monotonic():
            return "breaker_open"
        return None

    def _record(self, call: ProviderCall) -> None:
        self.stats.record(call)
        if call.error_code is None:
            self._fails = 0
            return
        if call.error_code == "deadline":
            return
        self._fails += 1
        if self._fails >= self.breaker_failures and self._breaker_until <= time.monotonic():
            self._breaker_until = time.monotonic() + self.breaker_cooldown_s
            log.warning(
                "verifier: %s failed %d times (last %s); skipping it for %.0f s",
                self.provider.name,
                self._fails,
                call.error_code,
                self.breaker_cooldown_s,
            )

    # ---- inputs --------------------------------------------------------------------------------------------------
    async def _label(self, sku: Sku) -> str:
        if sku not in self._labels:
            self._labels[sku] = product_label(await self.local.product(sku), sku)
        return self._labels[sku]

    async def _state(
        self,
        robot_id: RobotId,
        slot_ids: Sequence[SlotId],
        approvals: dict[str, bool],
        *,
        counter: bool = False,
    ) -> tuple[StateFacts | None, str | None, str | None]:
        """-> (StateFacts, backend, store_id) from RobotState alone (None when the robot service can't answer)."""
        robot = self.s.robot
        if robot is None:
            return None, None, None
        try:
            st = await robot.state(robot_id)
        except Exception as exc:  # noqa: BLE001
            log.debug("verifier: robot state %s failed (%s)", robot_id, exc)
            return None, None, None
        holding_sku: Sku | None = None
        basket_skus = list(st.basket_skus)
        counter_counts: dict[Sku, int] = {}
        if st.holding or counter or (st.basket and len(basket_skus) != len(st.basket)):
            try:
                units = {u.instance_id: u for u in await robot.list_instances(robot_id)}
                counter_counts = dict(Counter(u.sku for u in units.values() if u.on_counter))
                holding_sku = units[st.holding].sku if st.holding in units else None
                if len(basket_skus) != len(st.basket):
                    basket_skus = [units[i].sku for i in st.basket if i in units]
            except Exception as exc:  # noqa: BLE001
                log.debug("verifier: list_instances %s failed (%s)", robot_id, exc)
        slots: dict[SlotId, SlotTruth] = {}
        try:
            geom = await self.local.slots(st.store_id or "")
            for sid in slot_ids:
                g = geom.get(sid)
                if g is not None:
                    slots[sid] = SlotTruth(
                        slot_id=sid, sku=g.sku, approach_pose=g.approach_pose, item_position=g.item_position
                    )
        except Exception as exc:  # noqa: BLE001
            log.debug("verifier: planogram for %s failed (%s)", st.store_id, exc)
        facts = StateFacts(
            robot_id=robot_id,
            pose=st.pose,
            battery_pct=st.battery_pct,
            holding_instance_id=st.holding,
            holding_sku=holding_sku,
            basket_counts=dict(Counter(basket_skus)),
            counter_counts=counter_counts,
            approvals=dict(approvals),
            slots=slots,
        )
        return facts, st.backend, st.store_id

    async def _neighbor_skus(self, store_id: str | None, facts: StateFacts | None) -> list[Sku]:
        if not store_id or facts is None or facts.pose is None:
            return []
        try:
            geom = await self.local.slots(store_id)
        except Exception:  # noqa: BLE001
            return []
        near = sorted(
            (facts.pose.distance_to(s.approach_pose), s.sku)
            for s in geom.values()
            if facts.pose.distance_to(s.approach_pose) <= NEIGHBOR_RADIUS_M
        )
        return list(dict.fromkeys(sku for _, sku in near))

    async def _geom(self, store_id: str | None) -> dict[SlotId, Any]:
        if not store_id:
            return {}
        try:
            return await self.local.slots(store_id)
        except Exception as exc:  # noqa: BLE001 - no planogram: fewer detect classes, nothing else
            log.debug("verifier: planogram for %s failed (%s)", store_id, exc)
            return {}

    async def _frame(self, ref: FrameRef) -> CameraFrame | None:
        evidence = getattr(self.s, "evidence", None)
        if evidence is None:
            return None
        try:
            return await evidence.get_frame(ref.frame_id)
        except KeyError:
            return None

    # ---- vision --------------------------------------------------------------------------------------------------
    async def _vision(
        self,
        req: VerifyRequest,
        frames: Sequence[FrameRef],
        stale: set[str],
        facts: StateFacts | None,
        store_id: str | None,
        end: float,
    ) -> tuple[dict[str, AtomVerdict], list[ProviderCall]]:
        """Provider answers for every camera atom that has a fresh frame -> ({path: camera verdict}, calls)."""
        cond = req.condition
        by_cam = {f.camera_id: f for f in frames if f.camera_id not in stale}
        plan: dict[str, list[tuple[str, Atom]]] = {}
        for path, atom in iter_atoms(cond):
            kinds = atom_cameras(atom)
            if not kinds:
                continue
            kind = kinds[1] if req.alternate_cameras and len(kinds) > 1 else kinds[0]
            cid = robot_camera_id(req.robot_id, kind)
            if cid in by_cam:
                plan.setdefault(cid, []).append((path, atom))
        if not plan:
            return {}, []
        atom_skus = [s for _, a in iter_atoms(cond) for s in self._skus_of(a)]
        bound = [b.target.sku for b in req.bindings.values()]
        state_held = [facts.holding_sku] if facts is not None and facts.holding_sku else []
        base_skus = list(req.baseline.basket_counts) if req.baseline is not None else []
        neighbors = await self._neighbor_skus(store_id, facts)
        geom = await self._geom(store_id)
        all_skus = list(dict.fromkeys([*atom_skus, *bound, *state_held, *base_skus, *neighbors]))
        names = {sku: await self._label(sku) for sku in all_skus}
        for sid in slot_ids_in(cond):
            if sid in geom and geom[sid].sku not in names:
                names[geom[sid].sku] = await self._label(geom[sid].sku)

        budget = min(self.call_timeout_s, end - time.monotonic() - SAFETY_S)
        if budget < MIN_CALL_BUDGET_S:
            return {}, [self._deadline_call()]
        answers: dict[str, CameraAnswers] = {}
        keys: list[tuple[str, str]] = []
        coros: list[Awaitable[Any]] = []

        def ask(cid: str, slot: str, aw: Awaitable[Any]) -> None:
            keys.append((cid, slot))
            coros.append(aw)

        p = self.provider
        for cid, atoms in plan.items():
            ref = by_cam[cid]
            frame = await self._frame(ref)
            if frame is None:
                continue
            ca = CameraAnswers(kind=cid.rsplit(".", 1)[1], frame=ref, names=names)
            answers[cid] = ca
            ops = {a.op for _, a in atoms}
            if ops & GRIPPER_OPS:
                held = [*[s for _, a in atoms for s in self._skus_of(a)], *state_held, *bound, *neighbors]
                classes = [names[s] for s in dict.fromkeys(held) if s in names][: MAX_CLASSES - 1]
                ask(cid, "held", p.detect(frame, [*classes, GENERIC_HELD_CLASS], timeout_s=budget))
                ask(cid, "empty", p.question(frame, EMPTY_QUESTION, "bool", timeout_s=budget))
            if "label_matches" in ops:
                ask(cid, "label", p.ocr(frame, LABEL_PROMPT, timeout_s=budget))
            if ops & BASKET_OPS:
                skus = [*[s for _, a in atoms for s in self._skus_of(a)], *base_skus]
                classes = [names[s] for s in dict.fromkeys(skus) if s in names][:MAX_CLASSES]
                ask(cid, "items", p.detect(frame, classes, timeout_s=budget))
                before = next(
                    (f for f in (req.baseline.frames if req.baseline is not None else []) if f.camera_id == cid), None
                )
                before_frame = await self._frame(before) if before is not None else None
                if before is not None and before_frame is not None:
                    ca.before_frame = before
                    ask(cid, "items_before", p.detect(before_frame, classes, timeout_s=budget))
            if ops & SHELF_OPS:
                skus = [s for _, a in atoms for s in self._skus_of(a)]
                skus += [geom[a.slot_id].sku for _, a in atoms if a.op == "slot_visible" and a.slot_id in geom]  # type: ignore[union-attr]
                classes = [names[s] for s in dict.fromkeys(skus) if s in names][:MAX_CLASSES]
                ask(cid, "items", p.detect(frame, classes, timeout_s=budget))
            if "shelf_tag_reads" in ops:
                sid = next(a.slot_id for _, a in atoms if a.op == "shelf_tag_reads")  # type: ignore[union-attr]
                name = names.get(geom[sid].sku, geom[sid].sku) if sid in geom else "product"
                ask(cid, "tag", p.ocr(frame, TAG_PROMPT.format(name=name), timeout_s=budget))
        results = await self._gather(coros, keys, end)
        calls: list[ProviderCall] = []
        for (cid, slot), ans in zip(keys, results, strict=True):
            setattr(answers[cid], slot, ans)
            calls.append(ans.call)
            self._record(ans.call)
        out: dict[str, AtomVerdict] = {}
        for cid, atoms in plan.items():
            ca = answers.get(cid)
            if ca is None:
                continue
            for path, atom in atoms:
                v = self._judge(path, atom, ca, req, geom)
                if v is not None:
                    out[path] = v
        return out, calls

    @staticmethod
    def _skus_of(atom: Atom) -> list[Sku]:
        sku = getattr(atom, "sku", None)
        counts = getattr(atom, "counts", None) or {}
        return [s for s in [sku, *counts] if s]

    def _deadline_call(self) -> ProviderCall:
        return ProviderCall(
            provider=self.provider.name, model=self.provider.model, call="verify", error_code="deadline"
        )  # type: ignore[arg-type]

    async def _gather(self, coros: list[Awaitable[Any]], keys: list[tuple[str, str]], end: float) -> list[Any]:
        """All provider calls in parallel, bounded by the deadline; exceptions / overruns become failed answers."""
        if not coros:
            return []
        t0 = time.monotonic()
        name, model = self.provider.name, self.provider.model
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*coros, return_exceptions=True), timeout=max(0.05, end - time.monotonic() - SAFETY_S / 2)
            )
        except TimeoutError:
            return [failed_answer(name, model, slot, None, "deadline", t0) for _, slot in keys]  # type: ignore[arg-type]
        out = []
        for (_, slot), r in zip(keys, results, strict=True):
            if isinstance(r, BaseException):
                log.warning("verifier: provider %s call %s raised %r", name, slot, r)
                out.append(failed_answer(name, model, slot, None, type(r).__name__, t0))  # type: ignore[arg-type]
            else:
                out.append(r)
        return out

    def _judge(
        self, path: str, atom: Atom, ca: CameraAnswers, req: VerifyRequest, geom: dict[SlotId, Any]
    ) -> AtomVerdict | None:
        m = self.method
        if atom.op in GRIPPER_OPS:
            return judge_gripper(path, atom, ca, m)  # type: ignore[arg-type]
        if atom.op == "label_matches":
            return judge_label(path, atom, ca, m)  # type: ignore[arg-type]
        if atom.op == "basket_delta":
            return judge_basket_delta(path, atom, ca, req.baseline, m)  # type: ignore[arg-type]
        if atom.op == "basket_contains":
            return judge_basket_contains(path, atom, ca, m)  # type: ignore[arg-type]
        if atom.op == "item_on_shelf":
            return judge_item_on_shelf(path, atom, ca, m)  # type: ignore[arg-type]
        if atom.op == "item_on_counter":
            return judge_item_on_counter(path, atom, ca, m)
        if atom.op == "slot_visible":
            slot = geom.get(atom.slot_id)  # type: ignore[union-attr]
            return judge_slot_visible(path, atom, ca, slot.sku if slot is not None else None, m)  # type: ignore[arg-type]
        if atom.op == "shelf_tag_reads":
            return judge_shelf_tag(path, atom, ca, m)  # type: ignore[arg-type]
        return None

    # ---- verify --------------------------------------------------------------------------------------------------
    async def verify(self, request: VerifyRequest) -> Verdict:
        cached = self._cached(request.verify_id)
        if cached is not None:
            return cached
        self.calls += 1
        t0 = time.monotonic()
        end = t0 + request.deadline_s
        req = request
        effects = await self._faults(req.robot_id, req.context, VERIFIER_FAULTS)
        if "verifier_unavailable" in effects:
            raise VerifierUnavailableError("verifier service unavailable (injected fault)")
        cond = req.condition
        slot_ids = slot_ids_in(cond)
        needs_counter = any(a.op == "item_on_counter" for _, a in iter_atoms(cond))
        facts, backend, store_id = await self._state(req.robot_id, slot_ids, dict(req.approvals), counter=needs_counter)
        mode = self.resolve_mode(req.mode, backend)
        cams = cameras_needed(cond, req.robot_id, alternate=req.alternate_cameras)
        ctxd = exec_ctx_dict(req.context, slot_ids)
        frames, stale = await _bounded(
            self._frames(req.robot_id, cams, req.frames, req.not_before, ctxd), end - SAFETY_S, ([], set())
        )
        truth = await self._truth_now(req.robot_id, slot_ids)
        if facts is None and truth is None:
            raise VerifierUnavailableError("no robot state and no ground truth to judge from")
        low = effects.get("low_confidence")
        low_conf = float(low.get("confidence", DEFAULT_LOW_CONFIDENCE)) if low is not None else None

        gt: tuple[str, float, list[AtomVerdict]] | None = None
        if truth is not None:
            ftruth, _ = await self._frame_truth(truth, frames, stale, cond, req.alternate_cameras)
            gt = evaluate_on_truth(  # type: ignore[assignment]
                cond,
                ftruth,
                baseline=req.baseline,
                bindings=req.bindings,
                camera_confidence=low_conf,
                approvals=req.approvals,
            )
        state_leaves: dict[str, AtomVerdict] = {}
        if facts is not None:
            _, _, leaves = evaluate_on_state(cond, facts, baseline=req.baseline, bindings=req.bindings)
            state_leaves = {a.path: a for a in leaves}
        gt_leaves = {a.path: a for a in gt[2]} if gt is not None else {}

        camera_atoms = [p for p, a in iter_atoms(cond) if atom_cameras(a)]
        vision: dict[str, AtomVerdict] = {}
        calls: list[ProviderCall] = []
        block: str | None = None
        if mode != "gt_only" and camera_atoms:
            block = self._block_code(effects)
            if block is None:
                vision, calls = await self._vision(req, frames, stale, facts, store_id, end)
        if low_conf is not None:
            vision = {p: v.model_copy(update={"confidence": low_conf}) for p, v in vision.items()}

        used: set[str] = set()

        def fused(path: str, atom: Atom) -> AtomVerdict:
            sources = ATOM_SOURCES[atom.op]
            st, g = state_leaves.get(path), gt_leaves.get(path)
            if "camera" not in sources:
                if st is not None:
                    return st
                if g is not None:
                    return g
                return AtomVerdict(path=path, op=atom.op, outcome="uncertain", confidence=0.0, reason="no state")
            vis = vision.get(path)
            if (
                vis is not None
                and backend in STATE_AUTHORITATIVE_GRIPPER_BACKENDS
                and atom.op in GRIPPER_OPS
                and st is not None
                and st.outcome != "uncertain"
                and vis.outcome not in ("uncertain", st.outcome)
            ):
                # On Isaac the gripper state IS the simulator's attach / detach (the sticky grasp), so a camera that
                # disagrees is wrong, not the state: measured on vhagar (taco run 4), Perceptron called a held cheese
                # bag and a meat tray "gripper is empty" with both cues (0.9) and its override sent the runner into
                # redo / re-ground loops while the unit sat in the jaws. The disagreement stays in the reason.
                used.add("state")
                why = f"{st.reason} (camera disagreed: {vis.reason}; the simulator's grasp state wins)"
                return st.model_copy(update={"reason": why, "confidence": min(st.confidence, 0.8)})
            if vis is not None:
                used.add("provider")
                return combine_verdicts(st, vis) if ("state" in sources and st is not None) else vis
            if g is not None:
                used.add("ground_truth")
                return g
            used.add("state")
            if st is not None:
                return st
            return AtomVerdict(
                path=path,
                op=atom.op,
                outcome="uncertain",
                confidence=0.0,
                reason="needs a camera check (vision unavailable)",
                decided_by="state",
                method="state",
            )

        f_out, f_conf, f_atoms = evaluate(cond, fused)
        f_atoms = self._attach_frames(cond, f_atoms, frames, req.robot_id, req.alternate_cameras)
        vision_code = block or next((c.error_code for c in calls if c.error_code), None)
        ok_calls = [c for c in calls if c.error_code is None]
        latency = lambda: (time.monotonic() - t0) * 1000  # noqa: E731

        if mode == "decide":
            outcome, conf, atoms = f_out, f_conf, f_atoms
            decided_by = "provider" if "provider" in used else ("ground_truth" if "ground_truth" in used else "state")
            fell_back = bool(camera_atoms) and ("ground_truth" in used or "state" in used)
            methods = [self.method] if (calls or block) else []
            methods += [m for m in ("ground_truth", "state") if m in used]
            deciding = ok_calls[0] if (decided_by == "provider" and ok_calls) else self._source_call(decided_by)
            reason = verdict_reason(outcome, atoms)
            if fell_back:
                source = "ground truth" if "ground_truth" in used else "robot state"
                reason = f"{reason} (vision {vision_code or 'had no fresh frame'}; fell back to {source})"
            agrees = (outcome == gt[0]) if gt is not None else None
            if gt is not None and "provider" in used:
                self.stats.record_agreement(outcome == gt[0])
            verdict = Verdict(
                verify_id=req.verify_id,
                robot_id=req.robot_id,
                phase=req.phase,
                outcome=outcome,
                confidence=conf,
                reason=reason,
                atoms=atoms,
                frames=frames,
                decided_by=decided_by,  # type: ignore[arg-type]
                provider=deciding,
                ground_truth_agrees=agrees,
                degraded=fell_back,
                methods_tried=list(dict.fromkeys(methods)) or ["state"],
                latency_ms=latency(),
            )
        else:
            if gt is not None:
                outcome, conf, atoms = (
                    gt[0],
                    gt[1],
                    self._attach_frames(  # type: ignore[assignment]
                        cond, gt[2], frames, req.robot_id, req.alternate_cameras
                    ),
                )
                decided_by, degraded, methods = "ground_truth", False, ["ground_truth"]
            else:  # no sim truth (Isaac without instances, robot arm truth unwired): robot state decides
                outcome, conf, atoms = evaluate_on_state(cond, facts, baseline=req.baseline, bindings=req.bindings)  # type: ignore[arg-type]
                decided_by, degraded, methods = "state", True, ["ground_truth", "state"]
            shadow: list[ShadowOpinion] = []
            agrees = None
            if "provider" in used and calls:
                shadow.append(
                    ShadowOpinion(
                        provider=ok_calls[0] if ok_calls else calls[0],
                        outcome=f_out,
                        confidence=f_conf,
                        reason=verdict_reason(f_out, f_atoms),
                        evidence=[e for a in f_atoms if a.decided_by == "provider" for e in a.evidence][:8],
                    )
                )
                if gt is not None:
                    agrees = f_out == gt[0]
                    self.stats.record_agreement(agrees)
            if calls or block:
                methods = [self.method, *methods]
            reason = verdict_reason(outcome, atoms)  # type: ignore[arg-type]
            verdict = Verdict(
                verify_id=req.verify_id,
                robot_id=req.robot_id,
                phase=req.phase,
                outcome=outcome,  # type: ignore[arg-type]
                confidence=conf,
                reason=reason,
                atoms=atoms,  # type: ignore[arg-type]
                frames=frames,
                decided_by=decided_by,  # type: ignore[arg-type]
                provider=self._source_call(decided_by),
                shadow=shadow,
                ground_truth_agrees=agrees if mode == "shadow" else (True if gt is not None else None),
                degraded=degraded,
                methods_tried=list(dict.fromkeys(methods)),
                latency_ms=latency(),
            )
        if stale and verdict.outcome != "pass":
            verdict = verdict.model_copy(
                update={"reason": f"stale frame (captured before the action finished): {verdict.reason}"}
            )
        return self._remember(verdict)

    def _source_call(self, decided_by: str) -> ProviderCall:
        if decided_by == "state":
            return ProviderCall(provider="state", call="robot_state")
        return ProviderCall(provider="ground_truth", call="sidecar")

    # ---- ground --------------------------------------------------------------------------------------------------
    async def ground(self, request: GroundRequest) -> GroundingResult:
        t0 = time.monotonic()
        end = t0 + request.deadline_s
        target = request.target
        slot_ids = [target.slot_id] if target.slot_id else []
        facts, backend, _ = await self._state(request.robot_id, slot_ids, {})
        mode = self.resolve_mode(request.mode, backend)
        truth = await self._truth_now(request.robot_id, slot_ids)
        frames = await _bounded(self._ground_frames(request), end - SAFETY_S, [])
        gt_result = await self._gt_ground(request, truth, frames, t0) if truth is not None else None
        if mode == "gt_only" or not frames:
            if gt_result is not None:
                return gt_result
            return GroundingResult(
                grounding_id=request.grounding_id,
                found=False,
                reason="no camera frame and no ground truth",
                methods_tried=["ground_truth"],
                degraded=True,
                latency_ms=(time.monotonic() - t0) * 1000,
            )
        effects = await self._faults(request.robot_id, request.context, ["provider_down"])
        block = self._block_code(effects)
        frame_ref = frames[0]
        frame = await self._frame(frame_ref)
        budget = min(self.call_timeout_s, end - time.monotonic() - SAFETY_S)
        if block is None and (frame is None or budget < MIN_CALL_BUDGET_S):
            block = "deadline" if frame is not None else "no_frame"
        prov: GroundingResult | None = None
        if block is None and frame is not None:
            p = self.provider
            coros: list[Awaitable[Any]] = [
                p.detect(frame, [target.description], timeout_s=budget),
                p.point(frame, GRASP_POINT_PROMPT.format(description=target.description), timeout_s=budget),
            ]
            keys = [("front", "detect"), ("front", "point")]
            if request.read_label:
                coros.append(p.ocr(frame, f"Read the label of the {target.description}.", timeout_s=budget))
                keys.append(("front", "ocr"))
            answers = await self._gather(coros, keys, end)
            for a in answers:
                self._record(a.call)
            units = await self._units_in_frame(frame)
            pose = truth.pose if truth else None
            prov = self._provider_grounding(request, frame_ref, frame, answers, units, pose, t0)
            if prov is None:
                block = next((a.call.error_code for a in answers if a.call.error_code), "error")
        if prov is None:  # provider could not answer: ground truth (degraded when the provider should have decided)
            if gt_result is None:
                return GroundingResult(
                    grounding_id=request.grounding_id,
                    found=False,
                    reason=f"vision {block}; no ground truth to fall back on",
                    methods_tried=[self.method],
                    degraded=True,
                    latency_ms=(time.monotonic() - t0) * 1000,
                )
            return gt_result.model_copy(
                update={
                    "degraded": mode == "decide",
                    "methods_tried": [self.method, "ground_truth"],
                    "reason": f"{gt_result.reason} (vision {block}; ground truth)",
                }
            )
        if gt_result is not None:
            self.stats.record_agreement(self._grounding_agrees(prov, gt_result))
        if mode == "shadow" and gt_result is not None:
            if not gt_result.found or gt_result.target is None:
                return gt_result.model_copy(update={"methods_tried": ["ground_truth", self.method]})
            extra = list(prov.target.candidates) if prov.target is not None else []
            if prov.target is not None and prov.target.box is not None:
                inside = (
                    gt_result.target.box is not None
                    and prov.target.grasp_point is not None
                    and point_in_box(prov.target.grasp_point, gt_result.target.box)
                )
                extra.insert(
                    0,
                    Evidence(
                        kind="box",
                        role="target",
                        frame=frame_ref,
                        mention=f"{prov.target.provider.attribution if prov.target.provider else 'provider'} opinion",
                        box=prov.target.box,
                        point=prov.target.grasp_point,
                        confidence=prov.target.confidence,
                        matches_expectation=inside,
                    ),
                )
            target_gt = gt_result.target.model_copy(update={"candidates": [*gt_result.target.candidates, *extra][:8]})
            return gt_result.model_copy(update={"target": target_gt, "methods_tried": ["ground_truth", self.method]})
        return prov

    @staticmethod
    def _grounding_agrees(prov: GroundingResult, gt: GroundingResult) -> bool:
        if prov.found != gt.found:
            return False
        if not prov.found:
            return True
        pt = prov.target.grasp_point if prov.target is not None else None
        box = gt.target.box if gt.target is not None else None
        return pt is not None and box is not None and point_in_box(pt, box)

    def _provider_grounding(
        self,
        request: GroundRequest,
        frame_ref: FrameRef,
        frame: CameraFrame,
        answers: Sequence[Any],
        units: Sequence[tuple[str, Sku, Any]],
        pose: Any,
        t0: float,
    ) -> GroundingResult | None:
        """detect + point -> GroundedTarget decided by the provider (None = both calls failed)."""
        det, pt = answers[0], answers[1]
        lbl = answers[2] if len(answers) > 2 else None
        if not answer_ok(det) and not answer_ok(pt):
            return None
        target = request.target
        boxes = [b for b in (det.boxes if answer_ok(det) else []) if b.box is not None]
        point = next((p.point for p in (pt.points if answer_ok(pt) else []) if p.point is not None), None)
        attribution = (det.call if answer_ok(det) else pt.call).attribution
        latency = (time.monotonic() - t0) * 1000
        if not boxes and point is None:
            return GroundingResult(
                grounding_id=request.grounding_id,
                found=False,
                reason=f"{attribution}: no {target.description} in view",
                methods_tried=[self.method],
                latency_ms=latency,
            )
        inside = [b for b in boxes if point is not None and point_in_box(point, b.box)]  # type: ignore[arg-type]
        chosen = (inside or boxes or [None])[0]
        box: NormBox | None = chosen.box if chosen is not None else None
        grasp = point or (box.center() if box is not None else None)
        conf = TWO_CUES_CONFIDENCE if inside else ONE_CUE_CONFIDENCE
        if conf < request.min_confidence:
            return GroundingResult(
                grounding_id=request.grounding_id,
                found=False,
                reason=f"{attribution}: low confidence ({conf:.2f})",
                methods_tried=[self.method],
                latency_ms=latency,
            )
        instance_id = self._instance_at(units, frame.w, frame.h, target.sku, target.instance_id, grasp)
        candidates = [
            b.model_copy(update={"frame": frame_ref, "role": "distractor", "matches_expectation": None})
            for b in boxes
            if b is not chosen
        ][:6]
        if lbl is not None and answer_ok(lbl) and (lbl.text or "").strip():
            hits = [h for h in target.label_hints if h.casefold() in lbl.text.casefold()]
            candidates.append(
                Evidence(
                    kind="text",
                    role="label",
                    frame=frame_ref,
                    text=lbl.text.strip()[:200],
                    matches_expectation=bool(hits) if target.label_hints else None,
                )
            )
        grounded = GroundedTarget(
            grounding_id=request.grounding_id,
            target=target.model_copy(update={"instance_id": instance_id}) if instance_id else target,
            frame=frame_ref,
            box=box,
            grasp_point=grasp,
            confidence=conf,
            decided_by="provider",
            provider=pt.call if answer_ok(pt) else det.call,
            instance_id=instance_id,
            candidates=candidates,
            robot_id=request.robot_id,
            robot_pose=pose or frame.pose,
        )
        return GroundingResult(
            grounding_id=request.grounding_id,
            found=True,
            target=grounded,
            reason=f"{attribution}: {target.description} at ({grasp.x}, {grasp.y})" if grasp else attribution,
            methods_tried=[self.method],
            latency_ms=latency,
        )

    async def _units_in_frame(self, frame: CameraFrame) -> list[tuple[str, Sku, Any]]:
        """(instance_id, sku, pixel box) of the sim units visible in a retained frame: ``frame.instances`` (Isaac / 2D
        fake frames), else the frame's sidecar (synthetic fake frames). Empty without ground truth."""
        units = [(i.instance_id, i.sku, i.bbox) for i in frame.instances if i.bbox is not None]
        if units:
            return units
        sc = await self._sidecar(frame.frame_id)
        if sc is None:
            return []
        t = sc.truth
        sku_of = {u.instance_id: u.sku for s in t.slots.values() for u in s.instances}
        sku_of.update({u.instance_id: u.sku for u in t.basket})
        if t.holding is not None:
            sku_of[t.holding.instance_id] = t.holding.sku
        return [(iid, sku_of[iid], bb) for iid, bb in sc.in_view.items() if bb is not None and iid in sku_of]

    @staticmethod
    def _instance_at(
        units: Sequence[tuple[str, Sku, Any]], w: int, h: int, sku: Sku, wanted: str | None, point: Any
    ) -> str | None:
        """Sim: the unit under the grasp point (the shopper's chosen unit wins when it is in view). None without
        ground truth (T0 then picks any unit of the slot)."""
        if wanted and any(iid == wanted for iid, _, _ in units):
            return wanted
        mine = [(iid, NormBox.from_px(bb, w, h)) for iid, s, bb in units if s == sku]
        if not mine or point is None:
            return wanted
        hit = [iid for iid, b in mine if point_in_box(point, b)]
        if hit:
            return hit[0]
        return min(mine, key=lambda ib: (ib[1].center().x - point.x) ** 2 + (ib[1].center().y - point.y) ** 2)[0]

    # ---- baseline / info -----------------------------------------------------------------------------------------
    async def baseline(self, robot_id: RobotId, condition: Condition | None, *, baseline_id: str) -> VerifyBaseline:
        if "verifier_unavailable" in await self._faults(robot_id, None, ["verifier_unavailable"]):
            raise VerifierUnavailableError("verifier service unavailable (injected fault)")
        slot_ids = slot_ids_in(condition)
        truth = await self._truth_now(robot_id, slot_ids)
        facts, backend, _ = await self._state(robot_id, slot_ids, {})
        if truth is None and facts is None:
            raise VerifierUnavailableError("no ground truth / robot state for a baseline")
        frames: list[FrameRef] = []
        basket_atoms = any(a.op in BASKET_OPS for _, a in iter_atoms(condition))
        if basket_atoms and self.resolve_mode(None, backend) != "gt_only" and self._block_code({}) is None:
            frames = await self._capture(robot_id, [robot_camera_id(robot_id, "basket")], {"slot_ids": []}, None)
        if truth is not None:
            return VerifyBaseline(
                baseline_id=baseline_id,
                robot_id=robot_id,
                basket_counts=truth.basket_counts(),
                slot_counts={sid: len(s.instances) for sid, s in truth.slots.items()},
                holding_sku=truth.holding.sku if truth.holding else None,
                holding_instance_id=truth.holding.instance_id if truth.holding else None,
                frames=frames,
            )
        assert facts is not None
        return VerifyBaseline(
            baseline_id=baseline_id,
            robot_id=robot_id,
            basket_counts=dict(facts.basket_counts),
            holding_sku=facts.holding_sku,
            holding_instance_id=facts.holding_instance_id,
            frames=frames,
            decided_by="state",
        )

    async def capabilities(self) -> VerifierCapabilities:
        reason = self._provider_reason()
        healthy = reason is None
        note = "" if self._mode_override else "mode follows the store backend: shadow on the 2D fake, decide on Isaac"
        return VerifierCapabilities(
            mode=self._mode_override or "shadow",
            methods=[*([self.method] if healthy else []), "ground_truth", "state"],
            provider=self.provider.name,  # type: ignore[arg-type]
            model=self.provider.model,
            healthy=healthy,
            reason="; ".join(x for x in (reason, note) if x),
        )

    async def set_mode(self, mode: VerifierMode) -> VerifierCapabilities:
        self._mode_override = mode
        self.mode = mode
        return await self.capabilities()

    async def providers(self) -> list[ProviderInfo]:
        """GET /api/verifier/providers: 'Vision by Perceptron perceptron-mk1.5 · 42 calls · p50 610 ms · 97% agree'."""
        reason = self._provider_reason()
        return [
            ProviderInfo(
                provider=self.provider.name,  # type: ignore[arg-type]
                model=self.provider.model,
                roles=["grounding", "verification", "label_reading"],
                available=reason is None,
                reason=reason,
                mode=self._mode_override or "shadow",
                calls=self.stats.calls,
                errors=self.stats.errors,
                p50_latency_ms=self.stats.p50_latency_ms,
                agreement_with_ground_truth=self.stats.agreement,
            )
        ]


def build_provider(services: Services, config: Any = None) -> VisionProvider:
    """The configured ``VisionProvider`` (raises when it can't run, so the gateway boots the fake verifier)."""
    from .loop_config import verifier_config
    from .vision.perceptron import PerceptronVisionProvider
    from .vision.sidecar import SidecarVisionProvider

    cfg = config or verifier_config()
    if cfg.vision_provider == "fake":
        return SidecarVisionProvider(services)
    if cfg.vision_provider in ("gemini", "claude"):
        raise RuntimeError(
            f"verifier: PICKY_VISION_PROVIDER={cfg.vision_provider} (general VLM fallback) is not built; "
            "use perceptron (PERCEPTRON_API_KEY) or fake"
        )
    provider = PerceptronVisionProvider(
        api_key=cfg.perceptron_api_key,
        model=cfg.perceptron_model,
        base_url=cfg.perceptron_base_url,
        timeout_s=cfg.call_timeout_s,
    )
    reason = provider.unavailable_reason()
    if reason is not None:
        raise RuntimeError(f"verifier: {reason}; the ground-truth fake verifier runs instead")
    return provider


def create_verifier(services: Services) -> PerceptionVerifier:
    """Real verifier (Perceptron). Raises without PERCEPTRON_API_KEY / the perceptron SDK unless
    PICKY_VISION_PROVIDER=fake (the gateway then boots ``create_verifier_fake``). Never calls services here."""
    from .loop_config import verifier_config

    cfg = verifier_config()
    return PerceptionVerifier(
        services, build_provider(services, cfg), mode=cfg.verifier_mode, call_timeout_s=cfg.call_timeout_s
    )


__all__ = [
    "build_provider",
    "create_verifier",
    "EMPTY_QUESTION",
    "GRASP_POINT_PROMPT",
    "LABEL_PROMPT",
    "TAG_PROMPT",
    "PerceptionVerifier",
]
