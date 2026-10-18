"""Cameras API (CameraService): registry, blurred snapshots, streams, observe (fake + real with a mocked client).

Base deps only (pillow). Robots / sim / catalog are small stubs, so these tests don't depend on other arms.
"""

import asyncio
import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

import arms.perception.cameras as cams_mod
from arms.perception import create_cameras, create_cameras_fake, mjpeg_part
from arms.perception.cameras import (
    FakeCameras,
    RealCameras,
    default_cameras,
    is_placeholder,
    match_items,
    obscure_frame,
    scale_frame,
)
from contracts.cameras import CameraInfo, ObservedItem
from contracts.catalog import Product
from contracts.common import BBox, Point3, Pose2D, Rect2D
from contracts.planogram import Fixture, Planogram, Slot
from contracts.services import CameraService, Services
from contracts.sim import CameraFrame, ItemInstance, SimStatus, SimWorkerStatus
from utils import llm

STORE = "store_ave"


def _jpeg(w: int = 640, h: int = 480, color=(200, 120, 40)) -> str:
    img = Image.new("RGB", (w, h), color)
    for x in range(0, w, 40):  # some structure so pixelation is observable
        img.paste((20, 20, 20), (x, 0, x + 10, h))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


JPEG = _jpeg()

PRODUCTS = {
    "salsa_mild": Product(
        sku="salsa_mild",
        name="Mild Salsa",
        brand="Casa Verde",
        size="16 oz",
        category="pantry",
        price_cents=349,
        barcode="4000000000024",
    ),
    "avocado": Product(
        sku="avocado", name="Hass Avocado", category="produce", price_cents=129, barcode="4000000000017"
    ),
    "lime": Product(sku="lime", name="Lime", category="produce", price_cents=39, barcode="4000000000031"),
}


def _slot(slot_id: str, sku: str, qty: int, x: float, **kw) -> Slot:
    return Slot(
        slot_id=slot_id,
        fixture_id="F1",
        sku=sku,
        qty=qty,
        item_position=Point3(x=x, y=1.0, z=0.5),
        approach_pose=Pose2D(x=x, y=1.4, theta=-1.5708),
        **kw,
    )


PLAN = Planogram(
    store_id=STORE,
    name="Test Ave",
    bounds=Rect2D(x=0, y=0, w=20, h=10),
    entrance=Pose2D(x=1, y=1),
    checkout=Pose2D(x=19, y=1),
    fixtures=[Fixture(fixture_id="F1", kind="shelf", rect=Rect2D(x=1, y=0.2, w=10, h=0.6), yaw=1.5708)],
    slots=[
        _slot("S_SALSA", "salsa_mild", 4, 2.0, price_cents=349, shelf_tag_cents=399),
        _slot("S_AVO", "avocado", 3, 2.5),
        _slot("S_LIME", "lime", 0, 8.0),
    ],
)


class StubCatalog:
    async def get_planogram(self, store_id: str) -> Planogram:
        if store_id != STORE:
            raise KeyError(store_id)
        return PLAN.model_copy(deep=True)

    async def get_product(self, sku: str) -> Product | None:
        return PRODUCTS.get(sku)


def _inst(iid: str, sku: str, slot: str | None, fresh: float | None = None, **kw) -> ItemInstance:
    return ItemInstance(
        instance_id=iid,
        sku=sku,
        slot_id=slot,
        position=Point3(x=2.0, y=1.0, z=0.5),
        freshness=fresh,
        bbox=BBox(x=100, y=100, w=40, h=60),
        **kw,
    )


SALSA = [_inst(f"S_SALSA#0{i}", "salsa_mild", "S_SALSA") for i in range(4)]


class StubRobot:
    """capture_camera + frames(camera_id=...) like the v0.2 RobotService."""

    def __init__(self, instances=(), slot_id=None, fail: Exception | None = None, live: list | None = None):
        self.instances = list(instances)
        self.slot_id = slot_id
        self.fail = fail
        self.live = live  # None = no live frames at all (iterator blocks forever)
        self.captures = 0
        self.closed = False
        self.frames_kwargs: dict | None = None

    async def capture_camera(self, camera_id: str) -> CameraFrame:
        if self.fail is not None:
            raise self.fail
        self.captures += 1
        robot_id = camera_id.rsplit(".", 1)[0] if not camera_id.startswith("store_") else None
        return CameraFrame(
            frame_id=f"fake_cap_{self.captures}",
            camera=camera_id.rsplit(".", 1)[1].split("-")[0],
            camera_id=camera_id,
            robot_id=robot_id,
            jpeg_b64=JPEG,
            w=640,
            h=480,
            slot_id=self.slot_id,
            pose=Pose2D(x=2.0, y=1.4, theta=-1.5708),
            instances=list(self.instances),
        )

    def frames(self, camera_id=None, robot_id=None):
        self.frames_kwargs = {"camera_id": camera_id, "robot_id": robot_id}
        return self._gen(camera_id)

    async def _gen(self, camera_id):
        try:
            if self.live is None:
                await asyncio.Event().wait()
            for n in range(len(self.live)):
                await asyncio.sleep(0.01)
                yield CameraFrame(frame_id=f"live_{n}", camera_id=camera_id, jpeg_b64=JPEG, w=640, h=480)
            await asyncio.Event().wait()
        finally:
            self.closed = True


class StubSimLink:
    def __init__(self, status: SimStatus):
        self._status = status

    def status(self) -> SimStatus:
        return self._status


class NoBlurPerception:
    """A perception that can't certify anything (as with a real webcam and no detector)."""

    async def blur_faces(self, frame: CameraFrame) -> CameraFrame:
        return frame.model_copy()


@pytest.fixture
def no_data(tmp_path):
    return tmp_path  # empty data root -> built-in default registry


def _cams(tmp_path, **svc) -> FakeCameras:
    return FakeCameras(Services(catalog=StubCatalog(), **svc), data_root=tmp_path)


def _decode(frame: CameraFrame) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(frame.jpeg_b64))).convert("RGB")


# ------------------------------------------------------------------------------------------------------------------
# Conformance + factories
# ------------------------------------------------------------------------------------------------------------------
def test_factories_conform_and_are_lazy(monkeypatch):
    calls = []

    class Spy:
        def __getattr__(self, name):
            calls.append(name)
            raise AssertionError("factories must not call services")

    services = Services(robot=Spy(), catalog=Spy())
    assert isinstance(create_cameras_fake(services), CameraService)
    assert isinstance(RealCameras(services, client=object()), CameraService)
    from utils import llm

    monkeypatch.setattr(llm, "is_configured", lambda: False)
    with pytest.raises(llm.LLMNotConfigured):
        create_cameras(services)
    monkeypatch.setattr(llm, "is_configured", lambda: True)
    assert isinstance(create_cameras(services), RealCameras)
    assert calls == []


def test_real_cameras_name_the_missing_key_of_the_configured_provider() -> None:
    """/health shows this note: it must name GEMINI_API_KEY (the default provider), not a stale Anthropic key."""
    from utils.config import reset_settings, set_settings

    set_settings(picky_llm_provider="gemini", gemini_api_key="")
    try:
        with pytest.raises(llm.LLMNotConfigured, match="GEMINI_API_KEY"):
            create_cameras(Services())
    finally:
        reset_settings()


# ------------------------------------------------------------------------------------------------------------------
# Registry
# ------------------------------------------------------------------------------------------------------------------
async def test_registry_defaults_and_filters(no_data):
    c = _cams(no_data, robot=StubRobot())
    every = await c.list_cameras()
    ids = [x.camera_id for x in every]
    assert len(ids) == 12 and ids == [x.camera_id for x in default_cameras()]
    assert {"rover-ave-1.front", "rover-ave-2.basket", "rover-hill-1.wrist", "store_ave.ceiling-2"} <= set(ids)
    ave = await c.list_cameras(store_id="store_ave")
    assert len(ave) == 8 and all(x.store_id == "store_ave" for x in ave)
    pico = await c.list_cameras(robot_id="rover-ave-1")
    assert [x.kind for x in pico] == ["front", "wrist", "basket"]
    assert all(x.source == "fake" and x.online for x in pico)
    ceiling = await c.get_camera("store_ave.ceiling-1")
    assert ceiling.robot_id is None and ceiling.kind == "ceiling"
    assert ceiling.snapshot_url == "/api/cameras/store_ave.ceiling-1/snapshot.jpg"
    assert (await c.get_camera("rover-ave-1.head")).camera_id == "rover-ave-1.front"  # v0.1 alias
    with pytest.raises(KeyError):
        await c.get_camera("rover-zzz.front")


async def test_registry_from_robots_yaml(tmp_path):
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "robots.yaml").write_text(
        """
robots:
  - {robot_id: rover-x-1, name: Zed, store_id: store_x, dock: {x: 1, y: 1}, cameras: [front, basket]}
store_cameras:
  - {camera_id: store_x.ceiling-1, store_id: store_x, name: X ceiling, mount: {frame: world, x: 1, y: 1, z: 3}}
"""
    )
    c = FakeCameras(Services(), data_root=tmp_path)
    cams = await c.list_cameras()
    assert [x.camera_id for x in cams] == ["rover-x-1.front", "rover-x-1.basket", "store_x.ceiling-1"]
    assert cams[0].name == "Zed · front"
    assert all(x.source == "none" and not x.online for x in cams)  # no robot service wired


async def test_registry_overlays_sim_hello(no_data):
    announced = CameraInfo(camera_id="rover-ave-1.front", kind="front", name="Pico front (isaac)", store_id=STORE)
    extra = CameraInfo(camera_id="store_ave.ceiling-3", kind="ceiling", name="Ave ceiling 3", store_id=STORE)
    status = SimStatus(
        connected=True,
        workers=[SimWorkerStatus(worker_id="isaac-0", cameras=[announced, extra])],
        isaac_store_ids=["store_hill"],
    )
    c = _cams(no_data, robot=StubRobot(), sim_link=StubSimLink(status))
    by_id = {x.camera_id: x for x in await c.list_cameras()}
    assert by_id["rover-ave-1.front"].source == "isaac"
    assert by_id["rover-ave-1.wrist"].source == "fake"
    assert by_id["store_hill.ceiling-1"].source == "isaac"  # store hosted by Isaac
    assert by_id["store_ave.ceiling-3"].source == "isaac"  # camera only the worker knows about
    assert len(by_id) == 13


# ------------------------------------------------------------------------------------------------------------------
# Snapshots + privacy
# ------------------------------------------------------------------------------------------------------------------
async def test_snapshot_is_blurred_scaled_and_tracked(no_data):
    robot = StubRobot(SALSA, "S_SALSA")
    c = _cams(no_data, robot=robot)
    f = await c.snapshot("rover-ave-1.front")
    assert f.blurred and f.camera_id == "rover-ave-1.front" and f.robot_id == "rover-ave-1" and f.store_id == STORE
    assert (f.w, f.h) == (640, 480)
    small = await c.snapshot("rover-ave-1.front", max_width=320)
    assert (small.w, small.h) == (320, 240) and _decode(small).size == (320, 240)
    assert small.instances[0].bbox == BBox(x=50, y=50, w=20, h=30)
    big = await c.snapshot("rover-ave-1.front", max_width=4000)  # never upscales
    assert big.w == 640
    info = await c.get_camera("rover-ave-1.front")
    assert info.last_frame_at is not None and info.fps is not None


async def test_unverifiable_frame_is_pixelated_not_trusted(no_data):
    c = _cams(no_data, robot=StubRobot(), perception=NoBlurPerception())
    raw = CameraFrame(frame_id="webcam_1", jpeg_b64=JPEG, w=640, h=480)
    rendered = (await c.get_camera("rover-ave-1.front")).model_copy(update={"source": "fake"})
    assert (await c._blur(raw, rendered)).blurred  # sim-rendered camera: certified
    unknown = rendered.model_copy(update={"source": "none"})
    safe = await c._blur(raw, unknown)
    assert safe.blurred and safe.jpeg_b64 != raw.jpeg_b64
    # pixelated: neighbouring columns inside one cell are identical
    img = _decode(safe)
    assert img.getpixel((5, 200)) == img.getpixel((25, 200))
    broken = raw.model_copy(update={"jpeg_b64": "/9j/AAAA"})
    withheld = await c._blur(broken, unknown)
    assert withheld.blurred and is_placeholder(withheld)


async def test_robot_failure_and_no_robot_give_placeholders(no_data):
    from contracts.sim import RobotError

    c = _cams(no_data, robot=StubRobot(fail=RobotError("disconnected", "sim gone")))
    f = await c.snapshot("rover-ave-2.wrist", max_width=160)
    assert f.blurred and is_placeholder(f) and f.w == 160 and f.camera_id == "rover-ave-2.wrist"
    c2 = _cams(no_data)
    f2 = await c2.snapshot("store_ave.ceiling-1")
    f3 = await c2.snapshot("store_ave.ceiling-1")
    assert is_placeholder(f2) and f2.frame_id != f3.frame_id
    assert f2.frame_id == "synthetic-offline-store_ave.ceiling-1-00001"  # deterministic
    obs = await c2.observe("store_ave.ceiling-1")
    assert "offline" in obs.answer and obs.method == "fake" and obs.blurred


def test_frame_helpers():
    f = CameraFrame(frame_id="sim_1", jpeg_b64=JPEG, w=640, h=480)
    part = mjpeg_part(f)
    assert part.startswith(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ")
    assert part.endswith(b"\r\n") and base64.b64decode(JPEG) in part
    assert scale_frame(f, None) is f
    assert obscure_frame(f).blurred


# ------------------------------------------------------------------------------------------------------------------
# Streams
# ------------------------------------------------------------------------------------------------------------------
async def test_stream_uses_live_frames_and_closes_source(no_data):
    robot = StubRobot(live=list(range(100)))  # a live frame every 10 ms
    c = _cams(no_data, robot=robot)
    it = c.stream("rover-ave-1.front", fps=15, max_width=320)
    got = [await it.__anext__() for _ in range(3)]
    assert robot.frames_kwargs == {"camera_id": "rover-ave-1.front", "robot_id": None}
    assert all(f.blurred and f.w == 320 for f in got)
    assert all(f.frame_id.startswith("live_") for f in got) and robot.captures == 0
    await it.aclose()
    await asyncio.sleep(0)
    assert robot.closed


async def test_stream_falls_back_to_polling_when_stalled(no_data, monkeypatch):
    monkeypatch.setattr(cams_mod, "STREAM_STALL_S", 0.05)
    robot = StubRobot(SALSA, live=None)  # live iterator never yields
    c = _cams(no_data, robot=robot)
    it = c.stream("rover-ave-1.basket", fps=15)
    got = [await it.__anext__() for _ in range(3)]
    assert [f.frame_id for f in got] == ["fake_cap_1", "fake_cap_2", "fake_cap_3"]
    assert all(f.blurred for f in got)
    await it.aclose()


async def test_stream_without_robot_yields_placeholders(no_data):
    c = _cams(no_data)
    it = c.stream("rover-hill-1.front", fps=15)
    f = await it.__anext__()
    assert is_placeholder(f) and f.blurred
    await it.aclose()


async def test_stream_supports_old_unfiltered_frames(no_data):
    class OldRobot(StubRobot):
        def frames(self):  # v0.1 signature: no filters
            return self._mixed()

        async def _mixed(self):
            yield CameraFrame(frame_id="other", camera_id="rover-ave-2.front", jpeg_b64=JPEG, w=640, h=480)
            yield CameraFrame(frame_id="mine", camera_id="rover-ave-1.front", jpeg_b64=JPEG, w=640, h=480)
            await asyncio.Event().wait()

    c = _cams(no_data, robot=OldRobot())
    it = c.stream("rover-ave-1.front", fps=15)
    assert (await it.__anext__()).frame_id == "mine"
    await it.aclose()


# ------------------------------------------------------------------------------------------------------------------
# observe (fake: ground truth)
# ------------------------------------------------------------------------------------------------------------------
async def test_observe_describes_ground_truth_with_mispricing(no_data):
    c = _cams(no_data, robot=StubRobot(SALSA, "S_SALSA"))
    obs = await c.observe("rover-ave-1.front", goal_id="goal_1")
    assert obs.method == "ground_truth" and obs.blurred and obs.goal_id == "goal_1" and obs.people == 0
    assert obs.robot_id == "rover-ave-1" and obs.store_id == STORE and obs.frame_id == "fake_cap_1"
    [item] = obs.items
    assert (item.sku, item.count, item.slot_id, item.shelf_tag_cents) == ("salsa_mild", 4, "S_SALSA", 399)
    assert item.label == "Casa Verde Mild Salsa 16 oz" and item.bbox is not None
    assert "4 × Casa Verde Mild Salsa 16 oz" in obs.answer and "possible mispricing" in obs.answer

    yes = await c.observe("rover-ave-1.front", "Is the mild salsa in stock?")
    assert yes.answer.startswith("Yes: 4 × Casa Verde Mild Salsa") and yes.question == "Is the mild salsa in stock?"
    no = await c.observe("rover-ave-1.front", "any limes?")
    assert no.answer.startswith("I don't see anything matching")
    generic = await c.observe("rover-ave-1.front", "what are you doing and what do you see right now?")
    assert generic.answer.startswith("I see 4 × Casa Verde Mild Salsa"), generic.answer  # describe, don't "match"


async def test_observe_empty_slot_and_map_fallback(no_data):
    c = _cams(no_data, robot=StubRobot([], "S_LIME"))
    obs = await c.observe("rover-ave-1.front", "do you see limes?")
    assert obs.items[0].count == 0 and obs.items[0].sku == "lime"
    assert obs.answer.startswith("No: the Lime spot is empty")

    c2 = _cams(no_data, robot=StubRobot([], None))  # nothing aimed, no instances: planogram near the robot pose
    near = await c2.observe("rover-ave-1.front")
    assert near.method == "ground_truth" and [i.sku for i in near.items] == ["salsa_mild", "avocado"]
    assert near.items[0].confidence < 1 and "store map" in near.answer


async def test_observe_basket_camera(no_data):
    basket = [
        _inst("S_AVO#01", "avocado", "S_AVO", 0.9, in_basket=True),
        _inst("S_AVO#02", "avocado", "S_AVO", 0.8, in_basket=True),
        _inst("S_SALSA#01", "salsa_mild", "S_SALSA", in_basket=True),
    ]
    c = _cams(no_data, robot=StubRobot(basket))
    obs = await c.observe("rover-ave-1.basket", "how many avocados?")
    counts = {i.sku: i.count for i in obs.items}
    assert counts == {"avocado": 2, "salsa_mild": 1}
    assert obs.items[0].freshness == pytest.approx(0.85)
    assert obs.answer.startswith("Yes: 2 × Hass Avocado")


def test_match_items_plurals_and_stopwords():
    items = [ObservedItem(label="Hass Avocado", sku="avocado"), ObservedItem(label="Lime", sku="lime")]
    assert [i.sku for i in match_items("how many avocados are there?", items)] == ["avocado"]
    assert match_items("what do you see?", items) == []


# ------------------------------------------------------------------------------------------------------------------
# observe (real: mocked Claude client)
# ------------------------------------------------------------------------------------------------------------------
class FakeMessages:
    def __init__(self, payload=None, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(self.payload))])


async def test_real_observe_uses_vision_with_ground_truth_counts(no_data):
    payload = {
        "answer": "Yes, the mild salsa is on the shelf, but the tag reads $3.99.",
        "items": [
            {"label": "salsa jars", "sku": "salsa_mild", "count": 2, "confidence": 0.9, "shelf_tag_cents": 399},
            {"label": "mystery box", "sku": "not_a_sku", "count": 1, "confidence": 1.7},
        ],
        "people": 1,
    }
    messages = FakeMessages(payload)
    client = SimpleNamespace(messages=messages)
    services = Services(catalog=StubCatalog(), robot=StubRobot(SALSA, "S_SALSA"))
    c = RealCameras(services, client=client, data_root=no_data)
    obs = await c.observe("rover-ave-1.front", "Is the mild salsa in stock?")
    assert obs.method == "vlm" and obs.model == llm.model("fast") and obs.people == 1
    assert obs.answer.startswith("Yes, the mild salsa")
    salsa, mystery = obs.items
    assert (salsa.sku, salsa.count, salsa.shelf_tag_cents, salsa.slot_id) == ("salsa_mild", 4, 399, "S_SALSA")
    assert mystery.sku is None and mystery.confidence == 1.0

    [call] = messages.calls
    assert call["model"] == llm.model("fast")  # gemini-3.8-flash by default (PICKY_LLM_PROVIDER)
    content = call["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[1]["type"] == "text"  # image before text
    assert "sku=salsa_mild" in content[1]["text"] and "Is the mild salsa in stock?" in content[1]["text"]


async def test_real_observe_falls_back_on_llm_error(no_data):
    client = SimpleNamespace(messages=FakeMessages(error=RuntimeError("overloaded")))
    services = Services(catalog=StubCatalog(), robot=StubRobot(SALSA, "S_SALSA"))
    c = RealCameras(services, client=client, data_root=no_data)
    obs = await c.observe("rover-ave-1.front")
    assert obs.method == "ground_truth" and obs.items[0].count == 4


async def test_real_observe_skips_llm_when_offline(no_data):
    messages = FakeMessages({"answer": "x"})
    c = RealCameras(Services(catalog=StubCatalog()), client=SimpleNamespace(messages=messages), data_root=no_data)
    obs = await c.observe("rover-ave-1.front")
    assert "offline" in obs.answer and messages.calls == []
