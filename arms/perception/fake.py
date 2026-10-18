"""Deterministic perception fake: the ground-truth bypass (CLAUDE.md §9 fallback). Base deps only.

Every frame produced by the sim or the robot fake carries ``CameraFrame.instances`` (sku, instance_id, lot_id,
freshness, pixel bbox). This fake reads that ground truth instead of pixels:

- ``identify``       -> ScanReport with ``method='ground_truth'`` detections, exact counts, per-lot freshness and the
                        shelf-tag price (planogram via ``services.catalog`` at call time; ``utils.data`` if unwired).
- ``rank_freshness`` -> top-3 instances by ground-truth freshness, each with a shopper-facing reason.
- ``verify_basket``  -> multiset diff of the expected SKUs vs the instances seen in the basket.
- ``blur_faces``     -> see ``is_synthetic_frame``: the fake runs no face detector, so it only certifies frames that
                        cannot contain a real person.
"""

import asyncio
import logging
import time
from collections import Counter, defaultdict

from contracts.catalog import Product
from contracts.common import Sku, SlotId, StoreId
from contracts.perception import (
    PICK_CHOICE_MAX,
    BasketCheck,
    Detection,
    FreshnessCandidate,
    PickChoice,
    ScanReport,
)
from contracts.planogram import Planogram, Slot
from contracts.services import Services
from contracts.sim import CameraFrame, ItemInstance
from utils.ids import IdFactory
from utils.log import get_logger

log = get_logger(__name__)

SYNTHETIC_FRAME_PREFIXES: tuple[str, ...] = ("sim", "fake", "synthetic", "canned")
"""frame_id prefixes that mark a rendered frame even when it carries no ground-truth instances."""

NO_FRESHNESS_SCORE = 0.5
"""Score used for instances without a freshness value (non-produce); they sort after any scored instance."""


def is_synthetic_frame(frame: CameraFrame) -> bool:
    """True when the frame was rendered (Isaac sim or the robot fake), so it cannot show a real person's face.

    Why the fake only blurs these: the fake has no face detector. Setting ``blurred=True`` is a privacy claim the
    gateway relies on before streaming/storing/LLM-ing a frame (CLAUDE.md §8). For a rendered frame the claim is
    trivially true (no real faces exist), for anything else (a webcam, a phone photo) it would be a lie, so those
    come back with ``blurred=False`` and the gateway must not stream them.

    A frame counts as rendered if it carries sim ground truth (``instances``; only the sim and the robot fake fill
    it) or its ``frame_id`` starts with one of ``SYNTHETIC_FRAME_PREFIXES``.
    """
    if frame.instances:
        return True
    return frame.frame_id.lower().startswith(SYNTHETIC_FRAME_PREFIXES)


def freshness_reason(score: float | None, rank: int, n: int) -> str:
    """Shopper-facing one-liner for a freshness score (1.0 = freshest)."""
    if score is None:
        return f"No freshness signal for this item; #{rank} of {n} on the shelf"
    if score >= 0.85:
        label = "Peak fresh: firm, vibrant color, no blemishes"
    elif score >= 0.7:
        label = "Fresh: good color, only minor marks"
    elif score >= 0.5:
        label = "Ripe: best used within 1-2 days"
    elif score >= 0.3:
        label = "Very ripe: soft spots, use today"
    else:
        label = "Past its prime: bruising or browning"
    return f"{label} (freshness {score:.2f}, #{rank} of {n} on the shelf)"


def shelf_tag_price(slot: Slot, product: Product | None) -> int | None:
    """What the physical tag shows: an explicit (mis)priced tag, else the store price, else the list price."""
    if slot.shelf_tag_cents is not None:
        return slot.shelf_tag_cents
    if slot.price_cents is not None:
        return slot.price_cents
    return product.price_cents if product is not None else None


def slot_instances(frame: CameraFrame, slot_id: SlotId | None) -> list[ItemInstance]:
    """Shelf instances in the frame that belong to ``slot_id`` (unassigned ones count; basket items never do)."""
    out = [i for i in frame.instances if not i.in_basket]
    if slot_id is not None:
        out = [i for i in out if i.slot_id in (slot_id, None)]
    return out


def basket_instances(frame: CameraFrame) -> list[ItemInstance]:
    """Instances seen in the basket: flagged ``in_basket``, or everything when the frame is from the basket camera."""
    flagged = [i for i in frame.instances if i.in_basket]
    if flagged or frame.camera != "basket":
        return flagged
    return list(frame.instances)


class FakePerception:
    """PerceptionService over sim ground truth. Deterministic: ids come from a seeded IdFactory."""

    def __init__(self, services: Services | None = None, seed: int | str = "perception") -> None:
        self._services = services or Services()
        self._ids = IdFactory(seed)
        self._warned_unsynthetic = False

    # ---------------------------------------------------------------------------------------------------------------
    # Catalog access (call time only; falls back to data/ when catalog isn't wired)
    # ---------------------------------------------------------------------------------------------------------------
    async def _planogram(self, store_id: StoreId) -> Planogram | None:
        try:
            if self._services.catalog is not None:
                return await self._services.catalog.get_planogram(store_id)
            from utils.data import load_planogram

            return await asyncio.to_thread(load_planogram, store_id)
        except Exception as exc:  # unknown/online store, missing data file, catalog down: degrade, don't crash
            log.warning("perception: no planogram for %s (%s)", store_id, exc)
            return None

    async def _products(self, skus: set[Sku]) -> dict[Sku, Product]:
        out: dict[Sku, Product] = {}
        if not skus:
            return out
        try:
            if self._services.catalog is not None:
                for sku in sorted(skus):
                    product = await self._services.catalog.get_product(sku)
                    if product is not None:
                        out[sku] = product
                return out
            from utils.data import load_products

            every = await asyncio.to_thread(load_products)
            return {s: every[s] for s in skus if s in every}
        except Exception as exc:
            log.warning("perception: product lookup failed (%s)", exc)
            return out

    # ---------------------------------------------------------------------------------------------------------------
    # Protocol
    # ---------------------------------------------------------------------------------------------------------------
    async def blur_faces(self, frame: CameraFrame) -> CameraFrame:
        if frame.blurred:
            return frame.model_copy()
        if is_synthetic_frame(frame):
            return frame.model_copy(update={"blurred": True})
        # Warn once, then debug: live streams would otherwise log this for every frame.
        level = logging.DEBUG if self._warned_unsynthetic else logging.WARNING
        self._warned_unsynthetic = True
        log.log(level, "perception fake: frame %s is not synthetic; left blurred=False (do not stream)", frame.frame_id)
        return frame.model_copy()

    async def identify(
        self,
        frame: CameraFrame,
        store_id: StoreId,
        slot_id: SlotId | None = None,
        expected_sku: Sku | None = None,
    ) -> ScanReport:
        t0 = time.perf_counter()
        slot_id = slot_id or frame.slot_id
        plan = await self._planogram(store_id)
        slot = plan.slot(slot_id) if (plan is not None and slot_id is not None) else None

        seen = slot_instances(frame, slot_id)
        if expected_sku is None and slot is not None:
            expected_sku = slot.sku
        if expected_sku is None and seen:
            expected_sku = Counter(i.sku for i in seen).most_common(1)[0][0]

        products = await self._products({i.sku for i in seen} | ({expected_sku} if expected_sku else set()))
        detections = [
            Detection(
                sku=i.sku,
                confidence=1.0,
                method="ground_truth",
                bbox=i.bbox,
                instance_id=i.instance_id,
                lot_id=i.lot_id,
                text=products[i.sku].barcode if i.sku in products else None,
            )
            for i in sorted(seen, key=lambda i: i.instance_id)
        ]

        # Ground truth is exact when the frame carries instances, or when the robot aimed at this very slot and the
        # sim reported nothing there (= truly empty). Otherwise we know nothing: echo the planogram qty as a
        # non-exact count so catalog.apply_scan leaves stock alone instead of zeroing it.
        has_truth = bool(frame.instances) or (slot_id is not None and frame.slot_id == slot_id)
        notes: list[str] = []
        if has_truth:
            observed = sum(1 for i in seen if i.sku == expected_sku)
            wrong = sorted({i.sku for i in seen if expected_sku is not None and i.sku != expected_sku})
            mismatch = observed == 0 or bool(wrong)
            if wrong:
                notes.append("unexpected sku(s): " + ", ".join(wrong))
            if observed == 0:
                notes.append("slot empty")
        else:
            observed = slot.qty if slot is not None else 0
            mismatch = False
            notes.append("no ground truth in frame; echoed planogram qty (non-exact)")

        freshness = {i.instance_id: i.freshness for i in seen if i.freshness is not None}
        by_lot: dict[str, list[float]] = defaultdict(list)
        for i in seen:
            if i.lot_id is not None and i.freshness is not None:
                by_lot[i.lot_id].append(i.freshness)
        lot_freshness = {lot: round(sum(v) / len(v), 4) for lot, v in sorted(by_lot.items())}
        # Units per lot of the expected sku: only meaningful (and only sent) when the count is exact.
        lot_counts: dict[str, int] = {}
        if has_truth:
            counted = Counter(i.lot_id for i in seen if i.lot_id is not None and i.sku == expected_sku)
            lot_counts = dict(sorted(counted.items()))

        product = products.get(expected_sku) if expected_sku else None
        return ScanReport(
            scan_id=self._ids("scan"),
            store_id=store_id,
            slot_id=slot_id,
            expected_sku=expected_sku,
            detections=detections,
            observed_count=observed,
            count_is_exact=has_truth,
            mismatch=mismatch,
            freshness=freshness,
            lot_freshness=lot_freshness,
            lot_counts=lot_counts,
            shelf_tag_cents=shelf_tag_price(slot, product) if slot is not None else None,
            pose=frame.pose,
            frame_id=frame.frame_id,
            latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            notes="; ".join(notes),
        )

    async def rank_freshness(self, frame: CameraFrame, slot_id: SlotId, sku: Sku, k: int = 3) -> PickChoice:
        k = max(0, min(k, PICK_CHOICE_MAX))
        pool = [i for i in slot_instances(frame, slot_id) if i.sku == sku]
        # Freshest first; unscored items after scored ones; instance_id breaks ties deterministically.
        pool.sort(key=lambda i: (i.freshness is None, -(i.freshness or 0.0), i.instance_id))
        top = pool[:k]
        candidates = [
            FreshnessCandidate(
                instance_id=i.instance_id,
                sku=i.sku,
                score=i.freshness if i.freshness is not None else NO_FRESHNESS_SCORE,
                rank=rank,
                reason=freshness_reason(i.freshness, rank, len(pool)),
                bbox=i.bbox,
                thumbnail_b64=None,  # no image decoding in the fake; the real service crops from the blurred frame
            )
            for rank, i in enumerate(top, start=1)
        ]
        return PickChoice(
            choice_id=self._ids("pick"),
            slot_id=slot_id,
            sku=sku,
            candidates=candidates,
            frame_id=frame.frame_id,
        )

    async def verify_basket(self, frame: CameraFrame, expected: dict[Sku, int]) -> BasketCheck:
        observed = [i.sku for i in basket_instances(frame)]
        return BasketCheck.compare(expected, observed, method="ground_truth", frame_id=frame.frame_id)


def create_fake(services: Services) -> FakePerception:
    return FakePerception(services)
