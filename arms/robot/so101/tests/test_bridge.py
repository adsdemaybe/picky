"""SO-101 gateway bridge: protocol mapping (no hardware, no OpenCV, no network except a local loopback socket)."""

import base64
import io
import json
import threading
import time
from dataclasses import dataclass

import numpy as np
import pytest
from PIL import Image

from arms.robot.so101.bridge import (
    CAPABILITIES,
    BridgeConfig,
    BridgeCore,
    GatewayLink,
    gateway_ws_url,
    parse_map,
    raw_planogram_hash,
)
from arms.robot.so101.bridge_rig import DryRig, FakeCamera, pixelate, plan_error_code, split_plan
from contracts import CONTRACTS_VERSION
from contracts.sim import (
    CameraFrame,
    CommandResult,
    InstanceInfo,
    RobotState,
    SimHello,
    SimHosted,
    SimLog,
    parse_sim_message,
    planogram_hash,
    versions_compatible,
)

ROBOT = "so101-real"


class Sink:
    """Collects everything the core emits; ``result(cmd_id)`` waits for that command's CommandResult."""

    def __init__(self) -> None:
        self.msgs: list = []
        self._cv = threading.Condition()

    def __call__(self, msg) -> None:
        # round-trip through the wire format, like the gateway would parse it
        parsed = parse_sim_message(msg.model_dump_json())
        with self._cv:
            self.msgs.append(parsed)
            self._cv.notify_all()

    def result(self, cmd_id: str, timeout: float = 5.0) -> CommandResult:
        end = time.monotonic() + timeout
        with self._cv:
            while True:
                for m in self.msgs:
                    if isinstance(m, CommandResult) and m.cmd_id == cmd_id:
                        return m
                left = end - time.monotonic()
                if left <= 0:
                    raise AssertionError(f"no result for {cmd_id}; got {[type(m).__name__ for m in self.msgs]}")
                self._cv.wait(left)

    def of(self, kind: type) -> list:
        return [m for m in self.msgs if isinstance(m, kind)]


def make(stand_in: bool = False, threaded: bool = False, step_s: float = 0.0, **cfg_kw):
    sink = Sink()
    cam = FakeCamera(w=160, h=120)
    rig = DryRig(cam, step_s=step_s, say=lambda m: None)
    store = "store_hill" if stand_in else "store_table"
    robot = "rover-hill-1" if stand_in else ROBOT
    cfg = BridgeConfig(robot_id=robot, store_id=store, stand_in=stand_in, **cfg_kw)
    core = BridgeCore(cfg, rig, {"front": cam}, sink, threaded=threaded)
    return core, sink, rig


def cmd(kind: str, cmd_id: str, robot_id: str | None = ROBOT, **kw) -> str:
    return json.dumps({"type": kind, "cmd_id": cmd_id, "robot_id": robot_id, **kw})


# ---- handshake ------------------------------------------------------------------------------------------------------
def test_hello_advertises_one_store_robot_and_camera():
    core, _, _ = make()
    hello = parse_sim_message(core.hello("tok").model_dump_json())
    assert isinstance(hello, SimHello)
    assert hello.token == "tok" and hello.worker_id == "so101-real-bridge"
    assert versions_compatible(hello.contracts_version, CONTRACTS_VERSION)
    assert versions_compatible(hello.contracts_version, "0.2.3")  # the judges gateway
    assert [s.store_id for s in hello.stores] == ["store_table"]
    assert hello.stores[0].loaded and hello.stores[0].data_hash is None
    assert [(r.robot_id, r.store_id) for r in hello.robots] == [(ROBOT, "store_table")]
    assert [c.camera_id for c in hello.cameras] == [f"{ROBOT}.front"]
    assert hello.robots[0].camera_ids == [f"{ROBOT}.front"]
    assert "arm_joints" not in hello.capabilities and "real_hardware" in hello.capabilities
    assert set(CAPABILITIES) <= set(hello.capabilities)


def test_stand_in_store_is_advertised_unloaded_so_the_gateway_pushes_its_data():
    core, _, _ = make(stand_in=True)
    hello = core.hello("tok")
    assert hello.stores[0].store_id == "store_hill" and hello.stores[0].loaded is False
    assert hello.robots[0].robot_id == "rover-hill-1"


def test_on_connected_sends_state_and_starts_the_default_stream():
    core, sink, _ = make()
    core.on_connected()
    states = sink.of(RobotState)
    assert states and states[-1].robot_id == ROBOT and states[-1].store_id == "store_table"
    assert core.streams == {f"{ROBOT}.front": pytest.approx(core.clock(), abs=1.0)}
    assert sink.of(SimLog)


# ---- cameras --------------------------------------------------------------------------------------------------------
def _jpeg_size(frame: CameraFrame) -> tuple[int, int]:
    with Image.open(io.BytesIO(base64.b64decode(frame.jpeg_b64))) as im:
        return im.size


def test_capture_by_camera_id_and_by_robot_camera():
    core, sink, _ = make()
    core.submit(cmd("capture", "c1", None, camera_id=f"{ROBOT}.front"))
    core.submit(cmd("capture", "c2", ROBOT, camera="front"))
    core.submit(cmd("capture", "c3", ROBOT, camera="basket"))  # not a physical camera: aliases the table camera
    for cid in ("c1", "c2", "c3"):
        res = sink.result(cid)
        assert res.ok and res.frame is not None
        assert res.frame.robot_id == ROBOT and res.frame.store_id == "store_table"
        assert res.frame.blurred is False  # RAW on the wire; the gateway's perception owns the flag
        assert _jpeg_size(res.frame) == (160, 120) == (res.frame.w, res.frame.h)
    assert sink.result("c3").frame.camera_id == f"{ROBOT}.basket"


def test_capture_of_a_camera_we_do_not_host_is_unknown_robot():
    core, sink, _ = make()
    core.submit(cmd("capture", "c1", None, camera_id="rover-ave-1.front"))
    core.submit(cmd("capture", "c2", None))
    assert sink.result("c1").error_code == "unknown_robot"
    assert sink.result("c2").error_code == "unknown_robot"


def test_set_streams_keeps_hosted_cameras_and_paces_frames():
    t = [100.0]
    core, sink, _ = make()
    core.clock = lambda: t[0]
    core.submit(
        json.dumps({"type": "set_streams", "cmd_id": "s1", "camera_ids": [f"{ROBOT}.front", "x.front"], "fps": 4})
    )
    assert sink.result("s1").ok
    assert list(core.streams) == [f"{ROBOT}.front"]
    assert any("not hosted" in m.message for m in sink.of(SimLog))
    assert core.stream_due() == [f"{ROBOT}.front"]
    assert core.stream_due() == []
    t[0] += 0.26
    assert core.stream_due() == [f"{ROBOT}.front"]
    core.submit(json.dumps({"type": "set_streams", "cmd_id": "s2", "camera_ids": []}))
    assert sink.result("s2").ok and core.stream_due() == []


# ---- motions --------------------------------------------------------------------------------------------------------
def test_navigate_is_instant_and_moves_the_reported_pose():
    core, sink, _ = make()
    core.submit(cmd("navigate", "n1", waypoints=[{"x": 1, "y": 2, "theta": 0}, {"x": 3.5, "y": 4.0, "theta": 1.57}]))
    res = sink.result("n1")
    assert res.ok and res.state is not None
    assert (res.state.pose.x, res.state.pose.y) == (3.5, 4.0)
    assert any("stationary" in m.message for m in sink.of(SimLog))


def test_pick_place_unload_round_trip():
    core, sink, _ = make()
    core.submit(cmd("pick", "p1", slot_id="A1-S2-03", sku="SKU-GUM"))
    res = sink.result("p1")
    assert res.ok, res.error
    assert res.instance_id == "A1-S2-03#00" and res.instances[0].sku == "SKU-GUM"
    assert res.state.holding == "A1-S2-03#00"
    core.submit(cmd("place_in_basket", "p2"))
    res = sink.result("p2")
    assert res.ok and res.state.holding is None and res.state.basket == ["A1-S2-03#00"]
    assert res.state.basket_skus == ["SKU-GUM"]
    assert any(isinstance(m, InstanceInfo) and m.instances and m.instances[0].in_basket for m in sink.msgs)
    core.submit(cmd("pick", "p3", slot_id="A1-S2-03", sku="SKU-GUM"))
    assert sink.result("p3").instance_id == "A1-S2-03#01"  # next unit, not a duplicate id
    core.submit(cmd("place_in_basket", "p4"))
    core.submit(cmd("unload_basket", "u1"))
    res = sink.result("u1")
    assert res.ok and [i.instance_id for i in res.instances] == ["A1-S2-03#00", "A1-S2-03#01"]
    assert res.state.basket == []


def test_place_with_an_empty_gripper_is_no_instance():
    core, sink, _ = make()
    core.submit(cmd("place_in_basket", "p1"))
    assert sink.result("p1").error_code == "no_instance"


def test_rig_failures_keep_their_error_code():
    core, sink, rig = make()
    rig.fail_next = ("no_instance", "not found in the camera view: ['gum']")
    core.submit(cmd("pick", "p1", slot_id="s", sku="SKU-GUM"))
    res = sink.result("p1")
    assert not res.ok and res.error_code == "no_instance" and "not found" in res.error
    assert res.state.status == "error" and res.state.holding is None
    assert plan_error_code("waypoint 'straddle' unreachable") == "grasp_failed"


def test_robot_commands_for_another_robot_are_unknown_robot():
    core, sink, _ = make()
    core.submit(cmd("pick", "p1", "rover-ave-1", slot_id="s", sku="k"))
    core.submit(cmd("navigate", "n1", None, waypoints=[{"x": 0, "y": 0}]))
    assert sink.result("p1").error_code == "unknown_robot"
    assert sink.result("n1").error_code == "unknown_robot"


def test_unsupported_and_unknown_commands_get_a_typed_error():
    core, sink, _ = make()
    core.submit(cmd("arm_joints", "a1", targets=[]))
    core.submit(cmd("dance", "d1"))
    for cid in ("a1", "d1"):
        res = sink.result(cid)
        assert not res.ok and res.error_code == "internal"
    assert "not supported" in sink.result("a1").error


def test_busy_while_a_pick_runs_and_stop_cancels_it():
    core, sink, _ = make(threaded=True, step_s=0.5)
    core.submit(cmd("pick", "p1", slot_id="s", sku="SKU-GUM"))
    time.sleep(0.1)
    core.submit(cmd("navigate", "n1", waypoints=[{"x": 1, "y": 1}]))
    assert sink.result("n1").error_code == "busy"
    core.submit(cmd("capture", "c1"))  # non-motion commands still work mid-motion
    assert sink.result("c1").ok
    core.submit(cmd("stop", "x1"))
    assert sink.result("x1").ok
    res = sink.result("p1")
    assert not res.ok and res.error_code == "cancelled"
    assert core.wait_idle(2) and core.state().holding is None and core.state().status == "idle"
    core.submit(cmd("pick", "p2", slot_id="s", sku="SKU-GUM"))  # the stop does not stick to the next motion
    assert sink.result("p2").ok


def test_emergency_stop_from_the_terminal_and_disconnect_halt_motion():
    core, sink, _ = make(threaded=True, step_s=0.5)
    core.submit(cmd("pick", "p1", slot_id="s", sku="k"))
    time.sleep(0.05)
    core.emergency_stop()
    assert sink.result("p1").error_code == "cancelled"
    core.submit(cmd("pick", "p2", slot_id="s", sku="k"))
    time.sleep(0.05)
    core.on_disconnected()
    assert sink.result("p2").error_code == "cancelled" and core.streams == {}


def test_confirmation_declined_does_not_move():
    core, sink, _ = make()
    asked = []
    core._confirm = lambda prompt, stop: asked.append(prompt) or False
    core.submit(cmd("pick", "p1", slot_id="s", sku="k"))
    res = sink.result("p1")
    assert res.error_code == "cancelled" and "declined" in res.error and asked


# ---- store data (stand-in mode) -------------------------------------------------------------------------------------
PLANOGRAM = {
    "store_id": "store_hill",
    "slots": [
        {
            "slot_id": "H1-S1-01",
            "sku": "SKU-SALSA",
            "qty": 3,
            "price_cents": 399,
            "item_position": {"x": 2.0, "y": 3.0, "z": 0.9},
        }
    ],
    "future_field": {"added": "in 0.2.9"},
}


def test_store_reset_adopts_the_gateway_data_and_scan_lists_catalog_units():
    core, sink, _ = make(stand_in=True, default_prop="gum")
    core.submit(
        json.dumps(
            {
                "type": "reset",
                "cmd_id": "r1",
                "robot_id": None,
                "store_id": "store_hill",
                "planogram": PLANOGRAM,
                "products": [{"sku": "SKU-SALSA", "name": "Casa Verde Mild Salsa"}],
                "robots": [{"robot_id": "rover-hill-1", "name": "Hazel", "dock": {"x": 3.0, "y": 1.0, "theta": 1.5}}],
            }
        )
    )
    res = sink.result("r1")
    assert res.ok, res.error
    assert core.loaded and core.data_hash == raw_planogram_hash(PLANOGRAM)
    hosted = sink.of(SimHosted)
    assert hosted and hosted[-1].stores[0].loaded and hosted[-1].stores[0].data_hash == core.data_hash
    assert (core.pose.x, core.pose.y) == (3.0, 1.0) and "Hazel" in core.cfg.name
    core.submit(cmd("scan", "s1", "rover-hill-1", slot_id="H1-S1-01"))
    scan = sink.result("s1")
    assert scan.ok and [i.instance_id for i in scan.frame.instances] == ["H1-S1-01#00", "H1-S1-01#01", "H1-S1-01#02"]
    assert scan.frame.slot_id == "H1-S1-01"
    core.submit(cmd("pick", "p1", "rover-hill-1", slot_id="H1-S1-01", sku="SKU-SALSA"))
    assert sink.result("p1").ok
    assert any("Casa Verde Mild Salsa" in m.message and "'gum'" in m.message for m in sink.of(SimLog))
    core.submit(cmd("list_instances", "l1", "rover-hill-1", store_id="store_hill", slot_id="H1-S1-01"))
    ids = [i.instance_id for i in sink.result("l1").instances]
    assert "H1-S1-01#00" in ids and len(ids) == 3  # 2 left on the shelf + the held one


def test_reset_for_another_store_is_refused():
    core, sink, _ = make()
    core.submit(json.dumps({"type": "reset", "cmd_id": "r1", "robot_id": None, "store_id": "store_ave"}))
    assert sink.result("r1").error_code == "not_ready"


def test_robot_reset_goes_home_and_clears_the_basket():
    core, sink, _ = make()
    core.submit(cmd("pick", "p1", slot_id="s", sku="k"))
    core.submit(cmd("place_in_basket", "p2"))
    core.submit(cmd("navigate", "n1", waypoints=[{"x": 5, "y": 5}]))
    core.submit(json.dumps({"type": "reset", "cmd_id": "r1", "robot_id": ROBOT, "store_id": "store_table"}))
    res = sink.result("r1")
    assert res.ok and res.state.basket == [] and (res.state.pose.x, res.state.pose.y) == (0.0, 0.0)


def test_raw_planogram_hash_matches_the_contract_hash():
    from utils.data import load_planogram

    plan = load_planogram("store_hill")
    assert raw_planogram_hash(json.loads(plan.model_dump_json())) == planogram_hash(plan)


def test_set_slot_stock_updates_the_bookkeeping():
    core, sink, _ = make()
    core.submit(
        json.dumps(
            {
                "type": "set_slot_stock",
                "cmd_id": "k1",
                "store_id": "store_table",
                "slot_id": "T1",
                "sku": "SKU-GUM",
                "qty": 2,
            }
        )
    )
    res = sink.result("k1")
    assert res.ok and [i.instance_id for i in res.instances] == ["T1#00", "T1#01"]


# ---- helpers --------------------------------------------------------------------------------------------------------
def test_gateway_ws_url_and_sku_map():
    assert gateway_ws_url("http://localhost:8100", "t k") == "ws://localhost:8100/ws/sim?token=t%20k"
    assert gateway_ws_url("https://x.ts.net/picky/") == "wss://x.ts.net/picky/ws/sim"
    assert parse_map(["SKU-1=gum", "SKU-2 = oreo"]) == {"SKU-1": "gum", "SKU-2": "oreo"}
    with pytest.raises(SystemExit):
        parse_map(["nope"])


def test_pixelate_keeps_shape_and_hides_detail():
    img = (np.random.default_rng(0).random((120, 160, 3)) * 255).astype(np.uint8)
    out = pixelate(img, block=24)
    assert out.shape == img.shape and out.dtype == img.dtype
    assert len(np.unique(out[:24, :24].reshape(-1, 3), axis=0)) == 1


def test_blur_faces_runs_when_opencv_is_installed():
    pytest.importorskip("cv2")
    from arms.robot.so101.bridge_rig import blur_faces_bgr

    img = np.full((240, 320, 3), 128, np.uint8)
    out, n = blur_faces_bgr(img)
    assert out.shape == img.shape and n in (0, -1)


@dataclass
class _W:
    name: str


@dataclass
class _Plan:
    waypoints: list


def test_split_plan_cuts_after_the_short_lift():
    names = ["hover over target", "straddle", "close", "lift straight up", "lift high", "transit", "release"]
    pick, place = split_plan(_Plan([_W(n) for n in names]))
    assert [w.name for w in pick.waypoints] == names[:4]
    assert [w.name for w in place.waypoints] == names[4:]


# ---- the websocket link against a stand-in gateway ------------------------------------------------------------------
def test_link_handshake_command_and_result_over_a_real_socket():
    from websockets.sync.server import serve

    got: dict = {}

    def gateway(ws):
        got["hello"] = json.loads(ws.recv(timeout=5))
        ws.send(json.dumps({"type": "welcome", "ok": True, "session_id": "sim_test"}))
        ws.send(json.dumps({"type": "capture", "cmd_id": "cmd_1", "robot_id": ROBOT, "camera": "front"}))
        while "result" not in got:
            msg = json.loads(ws.recv(timeout=5))
            if msg.get("type") == "result":
                got["result"] = msg

    with serve(gateway, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        core, _, _ = make()
        link = GatewayLink(gateway_ws_url(f"http://127.0.0.1:{port}", "tok"), hello=lambda: core.hello("tok"))
        core.emit = link.send
        link.start()
        try:
            end = time.monotonic() + 10
            while "result" not in got and time.monotonic() < end:
                try:
                    kind, item = link.inbox.get(timeout=0.05)
                except Exception:
                    continue
                if kind == "connected":
                    core.on_connected()
                elif kind == "cmd":
                    core.submit(item)
        finally:
            link.stop()
            server.shutdown()
    assert got["hello"]["type"] == "hello" and got["hello"]["token"] == "tok"
    assert got["result"]["cmd_id"] == "cmd_1" and got["result"]["ok"] is True
    assert got["result"]["frame"]["camera_id"] == f"{ROBOT}.front"


def test_recorded_frames_are_served_privacy_safe(tmp_path):
    from arms.robot.so101.bridge_rig import ImageLoopCamera

    Image.new("RGB", (64, 48), (200, 30, 30)).save(tmp_path / "a.png")
    cam = ImageLoopCamera(str(tmp_path))
    jpeg, w, h = cam.read_jpeg()
    assert (w, h) == (64, 48) and jpeg[:2] == b"\xff\xd8"
    assert cam.real  # recorded footage is treated as real: face blur (or whole-frame pixelation) applies
