"""A tiny deterministic v0.2 world for the loop tests (no other arm imported): a 3-slot planogram, a teleporting
robot with real pixel boxes on its frames, a scripted fault injector and scripted vision providers."""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from collections.abc import Sequence
from typing import Any

from contracts.cameras import parse_camera_id, robot_camera_id
from contracts.catalog import Lot, Product
from contracts.common import BBox, Point3, Pose2D, Rect2D
from contracts.evidence import FrameRef
from contracts.faults import FaultContext, FaultFiring
from contracts.grounding import Evidence, NormBox, NormPoint, ProviderCall
from contracts.planogram import Aisle, Fixture, Planogram, Slot
from contracts.services import Services
from contracts.sim import CameraFrame, ItemInstance, RobotError, RobotState, RobotStatus
from contracts.verify import VisionAnswer

STORE = "store_ave"
ROBOT = "rover-ave-1"
TINY_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDABsSFBcUERsXFhceHBsgKEIrKCUlKFE6PTBCYFVlZF9VXVtqeJmBanGQc1tdhbWGkJ6jq62rZ4C8ybqmx5mo"
    "q6T/2wBDARweHigjKE4rK06kbl1upKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKSkpKT/wAARCAAwAEADASIAAhEB"
    "AxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS"
    "0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKz"
    "tLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgEC"
    "BAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpj"
    "ZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6"
    "/9oADAMBAAIRAxEAPwDVooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigD/9k="
)
W, H = 640, 480

PRODUCTS = {
    "SALSA-MILD": Product(
        sku="SALSA-MILD",
        name="Mild Chunky Salsa 16 oz",
        brand="Sol de Casa",
        category="pantry",
        price_cents=349,
        barcode="4000000000011",
    ),
    "SALSA-HOT": Product(
        sku="SALSA-HOT",
        name="Habanero Fire Salsa 16 oz",
        brand="Fuego Ridge",
        category="pantry",
        price_cents=499,
        barcode="4000000000028",
    ),
    "LIMES": Product(
        sku="LIMES", name="Lime", brand="Picky Farms", category="produce", price_cents=45, barcode="4000000000035"
    ),
}
SLOT = {"SALSA-MILD": "F1-1-00", "SALSA-HOT": "F1-1-01", "LIMES": "F1-1-02"}


def planogram(qty: int = 4) -> Planogram:
    slots = []
    for idx, sku in enumerate(["SALSA-MILD", "SALSA-HOT", "LIMES"]):
        x = 2.4 + idx * 0.7
        sid = SLOT[sku]
        slots.append(
            Slot(
                slot_id=sid,
                fixture_id="F1",
                aisle_id="A1",
                sku=sku,
                level=1,
                index=idx,
                item_position=Point3(x=x, y=3.0, z=0.8),
                approach_pose=Pose2D(x=x, y=2.62, theta=math.pi / 2),
                capacity=12,
                qty=qty,
                lots=[Lot(lot_id=f"{sid}-L1", qty=qty)],
                shelf_tag_cents=399 if sku == "SALSA-HOT" else None,
            )
        )
    return Planogram(
        store_id=STORE,
        name="mini",
        bounds=Rect2D(x=0, y=0, w=8, h=6),
        entrance=Pose2D(x=0.8, y=0.8, theta=0.0),
        checkout=Pose2D(x=7.0, y=0.8, theta=0.0),
        fixtures=[
            Fixture(
                fixture_id="F1",
                kind="shelf",
                aisle_id="A1",
                rect=Rect2D(x=2.0, y=3.0, w=3.0, h=0.6),
                levels=3,
                level_z_m=[0.3, 0.8, 1.3],
            )
        ],
        aisles=[Aisle(aisle_id="A1", label="Aisle 1", rect=Rect2D(x=1.0, y=1.5, w=6.0, h=1.4))],
        slots=slots,
    )


class MiniCatalog:
    def __init__(self, qty: int = 4) -> None:
        self.plan = planogram(qty)

    async def get_planogram(self, store_id: str) -> Planogram:
        return self.plan

    async def get_product(self, sku: str) -> Product | None:
        return PRODUCTS.get(sku)


def held_box() -> BBox:
    return BBox(x=W * 0.35, y=H * 0.35, w=W * 0.3, h=H * 0.4)


class MiniRobot:
    """Teleporting robot over the mini planogram; frames carry RAW (unblurred) JPEGs + instances with pixel boxes."""

    def __init__(self, catalog: MiniCatalog, *, backend: str = "fake", worker_clock_skew_s: float = 0.0) -> None:
        self.backend = backend
        self.skew = worker_clock_skew_s
        self.pose = Pose2D(x=2.4, y=2.62, theta=math.pi / 2)  # facing the mild salsa
        self.status = RobotStatus.IDLE
        self.holding: ItemInstance | None = None
        self.basket: list[ItemInstance] = []
        self.calls: Counter[str] = Counter()
        self.shelves: dict[str, list[ItemInstance]] = {}
        for s in catalog.plan.slots:
            self.shelves[s.slot_id] = [
                ItemInstance(
                    instance_id=f"{s.slot_id}#{n:02d}",
                    sku=s.sku,
                    store_id=STORE,
                    slot_id=s.slot_id,
                    position=s.item_position,
                )
                for n in range(1, s.qty + 1)
            ]
        self._n = 0

    def face(self, sku: str) -> None:
        x = 2.4 + ["SALSA-MILD", "SALSA-HOT", "LIMES"].index(sku) * 0.7
        self.pose = Pose2D(x=x, y=2.62, theta=math.pi / 2)

    def facing_slot(self) -> str | None:
        for sku, sid in SLOT.items():
            x = 2.4 + ["SALSA-MILD", "SALSA-HOT", "LIMES"].index(sku) * 0.7
            if abs(self.pose.x - x) < 0.1:
                return sid
        return None

    async def state(self, robot_id: str) -> RobotState:
        if robot_id != ROBOT:
            raise RobotError("unknown_robot", robot_id)
        return RobotState(
            robot_id=ROBOT,
            store_id=STORE,
            pose=self.pose,
            status=self.status,
            holding=self.holding.instance_id if self.holding else None,
            basket=[u.instance_id for u in self.basket],
            basket_skus=[u.sku for u in self.basket],
            battery_pct=80.0,
            backend=self.backend,  # type: ignore[arg-type]
        )

    async def list_instances(self, robot_id: str, slot_id: str | None = None) -> list[ItemInstance]:
        if slot_id is not None:
            return [u.model_copy() for u in self.shelves.get(slot_id, [])]
        out = [u.model_copy() for units in self.shelves.values() for u in units]
        if self.holding is not None:
            out.append(self.holding.model_copy())
        return out + [u.model_copy() for u in self.basket]

    def _frame(self, kind: str, instances: list[ItemInstance], slot_id: str | None = None) -> CameraFrame:
        from datetime import timedelta

        from contracts.common import utcnow

        self._n += 1
        return CameraFrame(
            frame_id=f"mini-{kind}-{self._n:04d}",
            camera=kind,  # type: ignore[arg-type]
            camera_id=robot_camera_id(ROBOT, kind),
            robot_id=ROBOT,
            store_id=STORE,
            jpeg_b64=TINY_JPEG_B64,
            w=W,
            h=H,
            blurred=False,
            slot_id=slot_id,
            pose=self.pose,
            instances=instances,
            ts=utcnow() - timedelta(seconds=self.skew),
        )

    def _front_units(self, slot_id: str | None) -> list[ItemInstance]:
        units = self.shelves.get(slot_id or "", [])
        return [
            u.model_copy(update={"bbox": BBox(x=60.0 + i * 130, y=200, w=110, h=180) if i < 4 else None})
            for i, u in enumerate(units)
        ]

    async def scan(self, robot_id: str, slot_id: str) -> CameraFrame:
        self.calls["scan"] += 1
        if self.status != RobotStatus.IDLE:
            raise RobotError("busy", "moving")
        return self._frame("front", self._front_units(slot_id), slot_id)

    async def capture(self, robot_id: str, camera: str = "front") -> CameraFrame:
        self.calls[f"capture:{camera}"] += 1
        if camera == "wrist":
            held = [self.holding.model_copy(update={"bbox": held_box()})] if self.holding else []
            return self._frame("wrist", held)
        if camera == "basket":
            boxes = [
                u.model_copy(update={"bbox": BBox(x=40.0 + i * 95, y=60, w=80, h=120)})
                for i, u in enumerate(self.basket)
            ]
            return self._frame("basket", boxes)
        sid = self.facing_slot()
        return self._frame("front", self._front_units(sid), sid)

    async def capture_camera(self, camera_id: str) -> CameraFrame:
        _, kind = parse_camera_id(camera_id)
        return await self.capture(ROBOT, kind)

    # motion used by tests
    def pick(self, sku: str) -> ItemInstance:
        unit = self.shelves[SLOT[sku]].pop(0)
        self.holding = unit
        return unit

    def place(self) -> None:
        assert self.holding is not None
        self.basket.append(self.holding.model_copy(update={"in_basket": True}))
        self.holding = None


class _Rule:
    def __init__(self, params: dict[str, Any]) -> None:
        self.params = params


class ScriptedFaults:
    """Fires the given effects (with params) every time they are consulted (``times`` = 0) or ``times`` times."""

    def __init__(self, effects: dict[str, dict[str, Any]] | None = None, times: int = 0) -> None:
        self.effects = dict(effects or {})
        self.times = times
        self.fired: Counter[str] = Counter()
        self.contexts: list[FaultContext] = []

    async def consult(self, ctx: FaultContext, effects: Sequence[str]) -> list[FaultFiring]:
        self.contexts.append(ctx)
        out = []
        for e in effects:
            if e in self.effects and (self.times == 0 or self.fired[e] < self.times):
                self.fired[e] += 1
                out.append(FaultFiring(fault_id=f"f-{e}", effect=e, context=ctx))  # type: ignore[arg-type]
        return out

    def rule(self, fault_id: str) -> _Rule:
        return _Rule(self.effects.get(fault_id.removeprefix("f-"), {}))


def world(*, backend: str = "fake", qty: int = 4, **robot_kw: Any) -> tuple[Services, MiniRobot]:
    catalog = MiniCatalog(qty)
    robot = MiniRobot(catalog, backend=backend, **robot_kw)
    return Services(catalog=catalog, robot=robot), robot  # type: ignore[arg-type]


class ScriptedProvider:
    """A VisionProvider returning canned answers per call kind (``answers['detect'] = VisionAnswer | callable``)."""

    name = "perceptron"
    model: str | None = "perceptron-mk1.5"

    def __init__(self, answers: dict[str, Any] | None = None, *, delay_s: float = 0.0) -> None:
        self.answers = dict(answers or {})
        self.delay_s = delay_s
        self.calls: list[tuple[str, str]] = []

    def unavailable_reason(self) -> str | None:
        return None

    async def _answer(self, kind: str, frame: CameraFrame, prompt: str) -> VisionAnswer:
        if not frame.blurred:
            raise ValueError("unblurred")
        self.calls.append((kind, prompt))
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        a = self.answers.get(kind)
        if callable(a):
            a = a(frame, prompt)
        if a is None:
            return answer(kind, prompt, error="no_answer")
        return a.model_copy(update={"call": a.call.model_copy(update={"call": kind, "prompt": prompt[:100]})})

    async def detect(
        self, frame: CameraFrame, classes: Sequence[str], *, timeout_s: float | None = None
    ) -> VisionAnswer:
        return await self._answer("detect", frame, "; ".join(classes))

    async def point(self, frame: CameraFrame, query: str, *, timeout_s: float | None = None) -> VisionAnswer:
        return await self._answer("point", frame, query)

    async def question(
        self, frame: CameraFrame, question: str, expects: str = "text", *, timeout_s: float | None = None
    ) -> VisionAnswer:
        return await self._answer("question", frame, question)

    async def ocr(
        self, frame: CameraFrame, prompt: str | None = None, *, timeout_s: float | None = None
    ) -> VisionAnswer:
        return await self._answer("ocr", frame, prompt or "")

    async def compare(
        self,
        before: CameraFrame,
        after: CameraFrame,
        question: str,
        expects: str = "box",
        *,
        timeout_s: float | None = None,
    ) -> VisionAnswer:
        return await self._answer("compare", after, question)

    async def multilook(
        self, frame: CameraFrame, prompts: Sequence[str], *, timeout_s: float | None = None
    ) -> list[VisionAnswer]:
        return [await self._answer("question", frame, p) for p in prompts]


def answer(
    kind: str = "detect",
    prompt: str = "",
    *,
    boxes: list[tuple[str, tuple[int, int, int, int]]] | None = None,
    points: list[tuple[str, tuple[int, int]]] | None = None,
    text: str = "",
    yes: bool | None = None,
    error: str | None = None,
) -> VisionAnswer:
    return VisionAnswer(
        text=text,
        yes=yes,
        boxes=[
            Evidence(kind="box", mention=m, box=NormBox(x1=b[0], y1=b[1], x2=b[2], y2=b[3])) for m, b in boxes or []
        ],
        points=[Evidence(kind="point", mention=m, point=NormPoint(x=p[0], y=p[1])) for m, p in points or []],
        confidence=0.0 if error else 0.85,
        call=ProviderCall(provider="perceptron", model="perceptron-mk1.5", call=kind, prompt=prompt, error_code=error),
    )


def ref_of(frame: CameraFrame) -> FrameRef:
    return FrameRef(frame_id=frame.frame_id, camera_id=frame.camera_id or "", robot_id=frame.robot_id, ts=frame.ts)
