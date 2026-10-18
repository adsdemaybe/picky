"""``SidecarVisionProvider``: a deterministic ``VisionProvider`` that answers from the frame's ground-truth sidecar.

It plays Perceptron's part with no key and no network (``PICKY_VISION_PROVIDER=fake``): boxes come from the sidecar's
pixel boxes (converted to the 0-1000 grid, ``mention`` = the detect class it matches, never a SKU), "is the gripper
empty?" from what the wrist really held, OCR from the unit labels / shelf tag. So the REAL verifier's judging code
(ROI rule, mention -> SKU matching, cue agreement, fusion, deadlines) runs end to end in tests and in the demo.
A frame without a sidecar answers ``error_code='no_sidecar'`` (the verifier then degrades like on a Perceptron error).
"""

from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any

from contracts.common import BBox
from contracts.evidence import GroundTruthSidecar
from contracts.grounding import Evidence, NormBox, ProviderCall
from contracts.services import Services
from contracts.sim import CameraFrame
from contracts.verify import VisionAnswer, VisionExpect, match_mention_to_sku
from utils.money import fmt_cents

from .common import failed_answer, require_blurred

GENERIC_CLASS_WORDS = frozenset({"item", "items", "object", "objects", "product", "products", "grocery", "thing"})
SIDE_CONFIDENCE = 0.85


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.casefold()) if len(t) > 1}


class SidecarVisionProvider:
    """Answers from ``services.evidence.ground_truth(frame_id)`` (this arm's evidence store)."""

    name: str = "fake"

    def __init__(self, services: Services, *, model: str = "sidecar") -> None:
        self.s = services
        self.model: str | None = model

    async def _sidecar(self, frame: CameraFrame) -> GroundTruthSidecar | None:
        evidence = getattr(self.s, "evidence", None)
        lookup = getattr(evidence, "ground_truth", None)
        if lookup is None:
            return None
        try:
            return await lookup(frame.frame_id)
        except KeyError:
            return None

    def _call(self, call: str, prompt: str | None, t0: float) -> ProviderCall:
        return ProviderCall(
            provider="fake", model=self.model, call=call, prompt=prompt, latency_ms=(time.monotonic() - t0) * 1000
        )

    @staticmethod
    def _units(sc: GroundTruthSidecar) -> list[tuple[str, str, BBox]]:
        """(instance_id, label, pixel box) of every unit visible in the frame."""
        t = sc.truth
        sku_of = {u.instance_id: u.sku for s in t.slots.values() for u in s.instances}
        sku_of.update({u.instance_id: u.sku for u in t.basket})
        if t.holding is not None:
            sku_of[t.holding.instance_id] = t.holding.sku
        return [
            (iid, t.labels.get(iid, sku_of.get(iid, iid)), bb)
            for iid, bb in sc.in_view.items()
            if bb is not None and not sc.occluded
        ]

    async def detect(
        self, frame: CameraFrame, classes: Sequence[str], *, timeout_s: float | None = None
    ) -> VisionAnswer:
        require_blurred(frame)
        t0 = time.monotonic()
        prompt = "Detect: " + "; ".join(classes)
        sc = await self._sidecar(frame)
        if sc is None:
            return failed_answer("fake", self.model, "detect", prompt, "no_sidecar", t0)
        names = {str(i): c for i, c in enumerate(classes)}
        generic = any(_tokens(c) & GENERIC_CLASS_WORDS for c in classes)
        boxes: list[Evidence] = []
        for _, label, bb in self._units(sc):
            idx = match_mention_to_sku(label, names) if names else None
            if idx is not None:
                mention = classes[int(idx)]
            elif generic or not classes:
                mention = label
            else:
                continue
            boxes.append(
                Evidence(
                    kind="box", mention=mention, box=NormBox.from_px(bb, frame.w, frame.h), confidence=SIDE_CONFIDENCE
                )
            )
        conf = 0.3 if sc.occluded else SIDE_CONFIDENCE  # a blocked view answers "nothing", weakly
        return VisionAnswer(text="", boxes=boxes, confidence=conf, call=self._call("detect", prompt, t0))

    async def point(self, frame: CameraFrame, query: str, *, timeout_s: float | None = None) -> VisionAnswer:
        require_blurred(frame)
        t0 = time.monotonic()
        sc = await self._sidecar(frame)
        if sc is None:
            return failed_answer("fake", self.model, "question:point", query, "no_sidecar", t0)
        want = _tokens(query)
        scored = sorted(
            ((len(want & _tokens(label)), -n, label, bb) for n, (_, label, bb) in enumerate(self._units(sc))),
            reverse=True,
        )
        points: list[Evidence] = []
        if scored and scored[0][0] > 0:
            _, _, label, bb = scored[0]
            points.append(
                Evidence(
                    kind="point",
                    mention=label,
                    point=NormBox.from_px(bb, frame.w, frame.h).center(),
                    confidence=SIDE_CONFIDENCE,
                )
            )
        return VisionAnswer(points=points, confidence=SIDE_CONFIDENCE, call=self._call("question:point", query, t0))

    async def question(
        self, frame: CameraFrame, question: str, expects: VisionExpect = "text", *, timeout_s: float | None = None
    ) -> VisionAnswer:
        require_blurred(frame)
        t0 = time.monotonic()
        call = f"question:{expects}"
        sc = await self._sidecar(frame)
        if sc is None:
            return failed_answer("fake", self.model, call, question, "no_sidecar", t0)
        if expects in ("box", "point"):
            ans = await (self.point(frame, question) if expects == "point" else self.detect(frame, [question]))
            return ans.model_copy(update={"call": self._call(call, question, t0)})
        if sc.occluded:
            return VisionAnswer(
                text="I can't tell: the view is blocked.", confidence=0.3, call=self._call(call, question, t0)
            )
        q = question.casefold()
        held = sc.truth.holding if (frame.camera == "wrist" or "gripper" in q) else None
        if "empty" in q:
            yes = held is None
            why = (
                "nothing is between the gripper jaws"
                if yes
                else f"the gripper holds {sc.truth.labels.get(held.instance_id, held.sku)}"
            )  # type: ignore[union-attr]
        elif "holding" in q or "held" in q:
            yes = held is not None
            why = (
                f"the gripper holds {sc.truth.labels.get(held.instance_id, held.sku)}"  # type: ignore[union-attr]
                if yes
                else "nothing is between the gripper jaws"
            )
        else:
            yes = bool(self._units(sc))
            why = "items are visible" if yes else "nothing relevant is visible"
        text = f"{'Yes' if yes else 'No'}, {why}."
        return VisionAnswer(
            text=text,
            yes=yes if expects == "bool" else None,
            confidence=SIDE_CONFIDENCE,
            call=self._call(call, question, t0),
        )

    async def ocr(
        self, frame: CameraFrame, prompt: str | None = None, *, timeout_s: float | None = None
    ) -> VisionAnswer:
        require_blurred(frame)
        t0 = time.monotonic()
        sc = await self._sidecar(frame)
        if sc is None:
            return failed_answer("fake", self.model, "ocr", prompt, "no_sidecar", t0)
        if sc.occluded:
            return VisionAnswer(text="", confidence=0.3, call=self._call("ocr", prompt, t0))
        t = sc.truth
        p = (prompt or "").casefold()
        if "price" in p or "tag" in p:
            sids = [frame.slot_id] if frame.slot_id in t.slots else list(t.visible_slots.get(sc.camera_id, []))
            sids = sids or list(t.slots)
            cents = [t.slots[s].shelf_tag_cents for s in sids if s in t.slots and t.slots[s].shelf_tag_cents]
            text = " ".join(fmt_cents(c) for c in cents[:1] if c is not None)
        elif frame.camera == "wrist" and t.holding is not None:
            text = t.labels.get(t.holding.instance_id, t.holding.sku)
        else:
            text = " · ".join(dict.fromkeys(label for _, label, _ in self._units(sc)))
        return VisionAnswer(text=text, confidence=SIDE_CONFIDENCE if text else 0.4, call=self._call("ocr", prompt, t0))

    async def compare(
        self,
        before: CameraFrame,
        after: CameraFrame,
        question: str,
        expects: VisionExpect = "box",
        *,
        timeout_s: float | None = None,
    ) -> VisionAnswer:
        require_blurred(before)
        ans = (
            await self.detect(after, [question]) if expects == "box" else await self.question(after, question, expects)
        )
        return ans.model_copy(update={"call": ans.call.model_copy(update={"call": "compare"})})

    async def multilook(
        self, frame: CameraFrame, prompts: Sequence[str], *, timeout_s: float | None = None
    ) -> list[VisionAnswer]:
        out: list[VisionAnswer] = []
        for p in prompts:
            low = p.casefold()
            if low.startswith(("box", "detect", "segment")):
                ans: Any = await self.detect(frame, [p])
            elif low.startswith("point"):
                ans = await self.point(frame, p)
            elif low.startswith("read"):
                ans = await self.ocr(frame, p)
            else:
                ans = await self.question(frame, p, "bool")
            out.append(ans.model_copy(update={"call": ans.call.model_copy(update={"call": "multilook"})}))
        return out


__all__ = ["SidecarVisionProvider"]
