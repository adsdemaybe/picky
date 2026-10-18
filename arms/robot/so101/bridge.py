"""Real SO-101 -> gateway bridge: the physical bench rig connects to the app EXACTLY like an Isaac sim worker.

    python -m arms.robot.so101.bridge --dry --camera fake          # Mac, no hardware: the whole gateway <-> rig path
    python -m arms.robot.so101.bridge --confirm                    # the real arm, asks y/N before every motion

Wire (contracts/sim.py, same as arms/sim/app.py): outbound websocket to ``<PICKY_GATEWAY_URL>/ws/sim``; the first
message is ``SimHello`` (``token`` = ``PICKY_SIM_TOKEN``, ``contracts_version`` = ``CONTRACTS_VERSION``: the gateway
accepts any 0.2.x); after ``SimWelcome(ok=True)`` the bridge sends its ``RobotState``, streams its front camera at 2 fps
until the gateway sends ``SetStreamsCmd``, and answers every ``SimCommand`` with exactly one ``CommandResult`` (echoing
``cmd_id`` + ``robot_id``). Reconnects with backoff like the Isaac worker.

What it hosts (two modes, see README "Connect the real SO-101 to the app"):
- standalone (default): store ``store_table`` + robot ``so101-real`` + camera ``so101-real.front`` (+ ``.wrist``).
  The gateway lists the worker in GET /api/sim/status and the cameras in GET /api/cameras; it has no planogram, so
  ``data_hash`` is None. NOTE: the gateway's robot arm drives only robots in data/fixtures/robots.yaml, so it sends
  this robot no commands and does not relay its frames until it learns about worker-only robots (a gateway change).
- stand-in (``--stand-in rover-hill-1``): the bridge hosts a FLEET robot and its store (``loaded=False``), so the robot
  arm pushes the store-level ``ResetCmd`` (planogram, products, robots) and then routes that store's robot to the real
  arm: fleet goals, the robot brain, the live camera and the cart all work with no gateway change. The Isaac worker must
  NOT host that store at the same time (run it with ``PICKY_SIM_STORES`` listing the other store only).

Command mapping (the arm is stationary on the table; the plate / basket next to it is the robot's basket):
  navigate -> instant success (pose = last waypoint) · scan -> real frame of the table (instances = the catalog's units
  of that slot, not seen) · pick -> Perceptron grounding of the product + IK + grasp + short lift · place_in_basket ->
  carry, release on the plate, home · unload_basket -> bookkeeping (a person empties the plate) · capture -> real
  frame · stop -> halts the arm at the next interpolation step (in-flight motion answers ``cancelled``) · reset ->
  home (robot) / adopt the gateway's store data (store) · set_streams / list_instances / set_slot_stock -> as Isaac ·
  arm_joints / arm_ik / anything else -> ``ok=False, error_code='internal'`` (typed error, never dropped).
Every real frame is face-blurred on the laptop before it leaves (bridge_rig.py).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

from arms.robot.so101 import config as _rig_config  # noqa: F401 - loads ~/.config/picky/env into os.environ first
from arms.robot.so101.bridge_rig import (
    DryRig,
    FakeCamera,
    FrameSource,
    MotionCancelled,
    RigError,
    open_camera_source,
)
from contracts import CONTRACTS_VERSION
from contracts.cameras import DEFAULT_ROBOT_CAMERA_MOUNTS, CameraInfo, parse_camera_id, robot_camera_id
from contracts.common import Point3, Pose2D, normalize_camera
from contracts.sim import (
    CameraFrame,
    CommandResult,
    InstanceInfo,
    ItemInstance,
    RobotState,
    RobotStatus,
    SimHello,
    SimHosted,
    SimLog,
    SimRobotInfo,
    SimStoreInfo,
    command_error_result,
    instance_id,
    parse_gateway_to_sim,
)
from utils.ids import new_id
from utils.log import get_logger

log = get_logger("arms.robot.so101.bridge")  # stable name also under `python -m`

DEFAULT_STORE = "store_table"
DEFAULT_ROBOT = "so101-real"
DEFAULT_STREAM_FPS = 2.0
MAX_STREAM_FPS = 15.0
RECONNECT_MAX_S = 30.0
ROBOT_KIND = "so101_bench"
CAPABILITIES: tuple[str, ...] = (
    "navigate",
    "scan",
    "pick",
    "place_in_basket",
    "unload_basket",
    "capture",
    "list_instances",
    "set_streams",
    "set_slot_stock",
    "stop",
    "reset",
    "camera:front",
    "real_hardware",
    "stationary_base",
)
"""Advertised in SimHello. ``real_hardware`` / ``stationary_base`` tell a gateway that motions are physical (slow: a
pick takes ~10-20 s) and that navigate is a no-op. ``arm_joints`` / ``arm_ik`` are NOT offered."""
UNSUPPORTED = {"arm_joints", "arm_ik"}
ROBOT_COMMANDS = {"navigate", "scan", "pick", "place_in_basket", "unload_basket"}
"""Commands that REQUIRE robot_id == ours (contracts/sim.py: missing or unknown -> unknown_robot)."""
_STOCK_FIELDS = {"qty", "lots", "price_cents", "shelf_tag_cents", "deal", "capacity", "low_threshold"}
TABLE_Z_M = 0.025
"""Nominal height of a gripped unit's centre on the table (store frame z for ItemInstance.position)."""

Emit = Callable[[Any], None]


def gateway_ws_url(gateway_url: str, token: str | None = None) -> str:
    """http(s)://host[/prefix] -> ws(s)://host[/prefix]/ws/sim[?token=...] (same as arms/sim/app.py)."""
    parts = urlsplit(gateway_url)
    scheme = {"https": "wss", "wss": "wss"}.get(parts.scheme, "ws")
    path = parts.path.rstrip("/") + "/ws/sim"
    query = f"token={quote(token)}" if token else ""
    return urlunsplit((scheme, parts.netloc, path, query, ""))


def raw_planogram_hash(planogram: dict[str, Any]) -> str:
    """``contracts.sim.planogram_hash`` computed on the planogram JSON exactly as the gateway sent it (stock fields
    dropped from slots), so it matches the gateway's hash even when this checkout's Planogram model is older."""
    data = json.loads(json.dumps(planogram))
    for slot in data.get("slots") or []:
        if isinstance(slot, dict):
            for k in _STOCK_FIELDS:
                slot.pop(k, None)
    blob = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class BridgeConfig:
    robot_id: str = DEFAULT_ROBOT
    store_id: str = DEFAULT_STORE
    name: str = "SO-101 (real)"
    worker_id: str = "so101-real-bridge"
    stand_in: bool = False
    """True: robot/store belong to the fleet; advertise the store unloaded so the gateway pushes its data (reset)."""
    sku_names: dict[str, str] = field(default_factory=dict)
    """SKU -> what to ask the camera for (the physical prop on the table), e.g. {'SKU-SALSA-16': 'gum'}."""
    default_prop: str | None = None
    """Prop to pick for any SKU not in ``sku_names`` (None = the catalog product name)."""
    dock: Pose2D = field(default_factory=lambda: Pose2D(x=0.0, y=0.0, theta=0.0))


# =====================================================================================================================
# protocol core (no sockets, no hardware: unit-tested)
# =====================================================================================================================
class BridgeCore:
    """SimCommand -> rig mapping for ONE stationary robot. ``submit(raw)`` never raises and every command ends in
    exactly one ``CommandResult`` (motions answer from a worker thread when they finish). ``emit`` must be
    thread-safe (GatewayLink.send is)."""

    def __init__(
        self,
        cfg: BridgeConfig,
        rig: Any,
        cameras: dict[str, FrameSource],
        emit: Emit,
        *,
        confirm: Callable[[str, threading.Event], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
        threaded: bool = True,
    ) -> None:
        if "front" not in cameras:
            raise ValueError("the bridge needs a 'front' camera")
        self.cfg = cfg
        self.rig = rig
        self.cameras = cameras
        self.emit = emit
        self.clock = clock
        self.threaded = threaded
        self._confirm = confirm
        self.pose = cfg.dock
        self.status = RobotStatus.IDLE
        self.holding: ItemInstance | None = None
        self.basket: list[ItemInstance] = []
        self.error: str | None = None
        self.detail = ""
        self.products: dict[str, dict[str, Any]] = {}
        self.slots: dict[str, dict[str, Any]] = {}
        self.data_hash: str | None = None
        self.loaded = not cfg.stand_in
        self.streams: dict[str, float] = {}
        self.stream_fps = DEFAULT_STREAM_FPS
        self.stop_event = threading.Event()
        self._active: tuple[str, str] | None = None  # (cmd_id, type) of the running motion
        self._lock = threading.RLock()
        self._last_state_key: tuple[Any, ...] | None = None
        self._thread: threading.Thread | None = None
        self._said_stationary = False
        for src in cameras.values():
            src.hud = self.hud_text

    # ---- hosting ---------------------------------------------------------------------------------------------------
    def camera_kinds(self) -> list[str]:
        return list(self.cameras)

    def camera_ids(self) -> list[str]:
        return [robot_camera_id(self.cfg.robot_id, k) for k in self.cameras]

    def camera_infos(self) -> list[CameraInfo]:
        out = []
        for kind, src in self.cameras.items():
            label = {"front": "table camera", "wrist": "wrist camera"}.get(kind, f"{kind} camera")
            out.append(
                CameraInfo(
                    camera_id=robot_camera_id(self.cfg.robot_id, kind),
                    kind=kind,  # type: ignore[arg-type]
                    name=f"{self.cfg.name} · {label}{'' if src.real else ' (synthetic)'}",
                    store_id=self.cfg.store_id,
                    robot_id=self.cfg.robot_id,
                    mount=DEFAULT_ROBOT_CAMERA_MOUNTS.get(kind, DEFAULT_ROBOT_CAMERA_MOUNTS["front"]).model_copy(),
                    source="isaac",
                )
            )
        return out

    def store_infos(self) -> list[SimStoreInfo]:
        return [SimStoreInfo(store_id=self.cfg.store_id, data_hash=self.data_hash, camera_ids=[], loaded=self.loaded)]

    def robot_infos(self) -> list[SimRobotInfo]:
        return [
            SimRobotInfo(
                robot_id=self.cfg.robot_id, store_id=self.cfg.store_id, kind=ROBOT_KIND, camera_ids=self.camera_ids()
            )
        ]

    def hello(self, token: str) -> SimHello:
        return SimHello(
            token=token,
            worker_id=self.cfg.worker_id,
            stores=self.store_infos(),
            robots=self.robot_infos(),
            cameras=self.camera_infos(),
            capabilities=[*CAPABILITIES, *(f"camera:{k}" for k in self.cameras if k != "front")],
            sim_version=f"so101-bridge/{getattr(self.rig, 'name', 'rig')}",
            contracts_version=CONTRACTS_VERSION,
        )

    def hosted(self) -> SimHosted:
        return SimHosted(
            worker_id=self.cfg.worker_id,
            stores=self.store_infos(),
            robots=self.robot_infos(),
            cameras=self.camera_infos(),
        )

    # ---- state -----------------------------------------------------------------------------------------------------
    def state(self) -> RobotState:
        with self._lock:
            return RobotState(
                robot_id=self.cfg.robot_id,
                store_id=self.cfg.store_id,
                pose=self.pose,
                status=self.status,
                holding=self.holding.instance_id if self.holding else None,
                basket=[i.instance_id for i in self.basket],
                basket_skus=[i.sku for i in self.basket],
                battery_pct=100.0,
                charging=False,
                current_cmd_id=self._active[0] if self._active else None,
                error=self.error,
            )

    def publish_state(self, force: bool = False) -> None:
        st = self.state()
        key = (st.pose, st.status, st.holding, tuple(st.basket), st.current_cmd_id, st.error)
        if force or key != self._last_state_key:
            self._last_state_key = key
            self.emit(st)

    def hud_text(self) -> str:
        with self._lock:
            parts = [self.cfg.name, self.status.value]
            if self.detail:
                parts.append(self.detail)
            elif self.holding:
                parts.append(f"holding {self.holding.sku}")
            if self.basket:
                parts.append(f"basket {len(self.basket)}")
        return " · ".join(parts)

    def log(self, message: str, level: str = "info") -> None:
        getattr(log, "warning" if level == "warning" else level, log.info)("%s", message)
        self.emit(SimLog(level=level, robot_id=self.cfg.robot_id, message=message[:500]))  # type: ignore[arg-type]

    # ---- connection lifecycle --------------------------------------------------------------------------------------
    def on_connected(self) -> None:
        """Gateway accepted us: default stream (front at 2 fps), our RobotState, the basket ground truth."""
        self.set_streams([robot_camera_id(self.cfg.robot_id, "front")], DEFAULT_STREAM_FPS)
        self.publish_state(force=True)
        mine = [*self.basket, *([self.holding] if self.holding else [])]
        self.emit(InstanceInfo(store_id=self.cfg.store_id, robot_id=self.cfg.robot_id, instances=mine))
        mode = "stand-in for a fleet robot" if self.cfg.stand_in else "standalone"
        self.log(
            f"real SO-101 bridge online ({mode}, rig={getattr(self.rig, 'name', '?')}, cameras={self.camera_ids()})"
        )

    def on_disconnected(self) -> None:
        """Link lost: stop streaming and halt any motion (no unattended arm motion while the app is gone)."""
        self.streams = {}
        if self._active is not None:
            self.stop_event.set()

    # ---- streams ---------------------------------------------------------------------------------------------------
    def set_streams(self, camera_ids: list[str], fps: float) -> list[str]:
        now = self.clock()
        known = set(self.camera_ids())
        self.stream_fps = max(0.1, min(MAX_STREAM_FPS, fps))
        self.streams = {cid: now for cid in camera_ids if cid in known}
        return [cid for cid in camera_ids if cid not in known]

    def stream_due(self, now: float | None = None) -> list[str]:
        now = self.clock() if now is None else now
        period = 1.0 / self.stream_fps
        due = []
        for cid, t in self.streams.items():
            if now >= t:
                due.append(cid)
                self.streams[cid] = max(t + period, now)
        return due

    # ---- frames ----------------------------------------------------------------------------------------------------
    def resolve_camera(self, camera_id: str | None, robot_id: str | None, camera: str = "front") -> str:
        """Hosted camera id for a capture; other kinds of OUR robot (e.g. 'basket') alias to the table camera."""
        if camera_id:
            try:
                owner, kind = parse_camera_id(camera_id)
            except ValueError as exc:
                raise RigError("unknown_robot", str(exc)) from exc
        elif robot_id:
            owner, kind = robot_id, normalize_camera(camera)  # type: ignore[assignment]
        else:
            raise RigError("unknown_robot", "capture needs camera_id or robot_id")
        if owner != self.cfg.robot_id:
            raise RigError("unknown_robot", f"camera {camera_id or kind!r} is not hosted by this bridge")
        return robot_camera_id(owner, kind)

    def frame(self, camera_id: str, slot_id: str | None = None) -> CameraFrame:
        _, kind = parse_camera_id(camera_id)
        src = self.cameras.get(kind) or self.cameras["front"]
        jpeg, w, h = src.read_jpeg()
        return CameraFrame(
            frame_id=new_id("frame"),
            camera=kind,  # type: ignore[arg-type]
            camera_id=camera_id,
            robot_id=self.cfg.robot_id,
            store_id=self.cfg.store_id,
            jpeg_b64=base64.b64encode(jpeg).decode("ascii"),
            w=w,
            h=h,
            blurred=False,  # RAW on the wire (contracts/sim.py); real frames were face-blurred on this laptop anyway
            slot_id=slot_id,
            pose=self.pose,
        )

    # ---- results ---------------------------------------------------------------------------------------------------
    def _result(self, data: dict[str, Any], ok: bool, t0: float | None = None, **kw: Any) -> CommandResult:
        return CommandResult(
            cmd_id=data["cmd_id"],
            robot_id=data.get("robot_id") if isinstance(data.get("robot_id"), str) else None,
            ok=ok,
            duration_ms=round((self.clock() - t0) * 1000, 1) if t0 is not None else 0.0,
            **kw,
        )

    def _reply(self, data: dict[str, Any], t0: float | None = None, **kw: Any) -> None:
        self.emit(self._result(data, True, t0, state=self.state(), **kw))

    def _fail(self, data: dict[str, Any], code: str, message: str, t0: float | None = None) -> None:
        self.emit(self._result(data, False, t0, error_code=code, error=message[:500], state=self.state()))

    # ---- commands --------------------------------------------------------------------------------------------------
    def submit(self, raw: str | bytes | dict[str, Any]) -> None:
        """Handle one gateway -> sim message (JSON text or dict)."""
        try:
            data = json.loads(raw) if isinstance(raw, str | bytes) else dict(raw)
        except (ValueError, TypeError) as exc:
            self.emit(SimLog(level="error", message=f"unparsable message: {exc}"[:500]))
            return
        if not isinstance(data, dict):
            self.emit(SimLog(level="error", message="message is not a JSON object"))
            return
        kind = data.get("type")
        if kind == "welcome":
            return
        if kind in UNSUPPORTED and isinstance(data.get("cmd_id"), str):  # newer wire types this checkout may not know
            msg = f"{kind} is not supported by the real SO-101 bridge (scripted pick/place only)"
            self._fail(data, "internal", msg)
            return
        cmd: Any = None
        try:
            cmd = parse_gateway_to_sim(data)
        except Exception as exc:  # noqa: BLE001 - version drift: reset is handled from the raw JSON, rest answered
            if kind != "reset" or not isinstance(data.get("cmd_id"), str):
                reply = command_error_result(data, exc)
                self.emit(reply if reply is not None else SimLog(level="error", message=f"bad message: {exc}"[:500]))
                return
        if not isinstance(data.get("cmd_id"), str):
            return
        try:
            self._dispatch(kind, cmd, data)
        except RigError as exc:
            self._fail(data, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - one result per command, whatever happens
            log.exception("command %s failed", kind)
            self._fail(data, "internal", f"{type(exc).__name__}: {exc}")

    def _dispatch(self, kind: str | None, cmd: Any, data: dict[str, Any]) -> None:
        rid = data.get("robot_id")
        if kind in UNSUPPORTED:
            raise RigError("internal", f"{kind} is not supported by the real SO-101 bridge (scripted pick/place only)")
        if kind in ROBOT_COMMANDS and rid != self.cfg.robot_id:
            raise RigError("unknown_robot", f"robot {rid!r} is not hosted by this bridge ({self.cfg.robot_id})")
        if kind == "stop":
            self._stop(data)
        elif kind == "set_streams":
            missing = self.set_streams(list(cmd.camera_ids), cmd.fps)
            if missing:
                self.emit(SimLog(level="warning", message=f"set_streams: not hosted here: {', '.join(missing)}"))
            self._reply(data)
        elif kind == "capture":
            cid = self.resolve_camera(cmd.camera_id, rid, cmd.camera)
            self._reply(data, frame=self.frame(cid))
        elif kind == "list_instances":
            self._check_store(data.get("store_id"))
            self._reply(data, instances=self._units(cmd.slot_id))
        elif kind == "set_slot_stock":
            self._check_store(cmd.store_id)
            self.slots.setdefault(cmd.slot_id, {"slot_id": cmd.slot_id})
            self.slots[cmd.slot_id].update(sku=cmd.sku, qty=cmd.qty)
            units = self._slot_units(cmd.slot_id)
            self.emit(InstanceInfo(store_id=self.cfg.store_id, slot_id=cmd.slot_id, instances=units))
            self._reply(data, instances=units)
        elif kind == "reset":
            self._reset(data)
        elif kind == "navigate":
            self._busy_check()
            self.pose = cmd.waypoints[-1]
            if not self._said_stationary:
                self._said_stationary = True
                self.log("navigate: the real SO-101 is a stationary arm on a table; arriving instantly")
            self.publish_state()
            self._reply(data)
        elif kind == "scan":
            self._busy_check()
            frame = self.frame(robot_camera_id(self.cfg.robot_id, "front"), slot_id=cmd.slot_id)
            frame = frame.model_copy(update={"instances": self._slot_units(cmd.slot_id)})
            self._reply(data, frame=frame)
        elif kind == "pick":
            self._start_motion(data, RobotStatus.PICKING, lambda t0: self._pick(data, cmd, t0))
        elif kind == "place_in_basket":
            if self.holding is None:
                raise RigError("no_instance", "the gripper is empty")
            self._start_motion(data, RobotStatus.PLACING, lambda t0: self._place(data, t0))
        elif kind == "unload_basket":
            self._busy_check()
            with self._lock:
                unloaded = [i.model_copy(update={"in_basket": False}) for i in self.basket]
                self.basket = []
            self.log(f"unload_basket: {len(unloaded)} unit(s) handed off (a person empties the plate)")
            self.publish_state()
            self._reply(data, instances=unloaded)
        else:
            raise RigError("internal", f"unsupported command {kind!r}")

    def _check_store(self, store_id: str | None) -> None:
        if store_id and store_id != self.cfg.store_id:
            raise RigError(
                "not_ready", f"store {store_id!r} is not hosted here (this bridge hosts {self.cfg.store_id})"
            )

    def _busy_check(self) -> None:
        if self._active is not None:
            raise RigError("busy", f"{self._active[1]} {self._active[0]} is running on {self.cfg.robot_id}")

    def _stop(self, data: dict[str, Any]) -> None:
        rid = data.get("robot_id")
        if rid not in (None, self.cfg.robot_id):
            raise RigError("unknown_robot", f"robot {rid!r} is not hosted by this bridge")
        if self._active is not None:
            self.stop_event.set()
            self.log(f"stop: halting {self._active[1]} {self._active[0]}", "warning")
        self._reply(data)

    def emergency_stop(self) -> None:
        """Operator stop from the bridge terminal (same effect as a gateway StopCmd)."""
        if self._active is not None:
            self.stop_event.set()
            self.log("operator stop: halting the arm", "warning")

    def _reset(self, data: dict[str, Any]) -> None:
        store_id = data.get("store_id") or self.cfg.store_id
        self._check_store(store_id)
        before = (self.data_hash, self.loaded)
        robots = data.get("robots") if isinstance(data.get("robots"), list) else []
        for r in robots:
            if isinstance(r, dict) and r.get("robot_id") == self.cfg.robot_id and isinstance(r.get("dock"), dict):
                with self._lock:
                    self.cfg.dock = Pose2D(**{k: float(r["dock"].get(k, 0.0)) for k in ("x", "y", "theta")})
                if r.get("name"):
                    self.cfg.name = f"{r['name']} (real SO-101)"
        for p in data.get("products") or []:
            if isinstance(p, dict) and p.get("sku"):
                self.products[p["sku"]] = p
        plan = data.get("planogram")
        if isinstance(plan, dict):
            self.slots = {
                s["slot_id"]: dict(s) for s in plan.get("slots") or [] if isinstance(s, dict) and "slot_id" in s
            }
            self.data_hash = raw_planogram_hash(plan)
            self.loaded = True
        elif data.get("robot_id") is None:
            self.loaded = True
        if data.get("robot_id") is None:
            # store-level: respawn bookkeeping only (the arm is not moved; a held unit stays held)
            with self._lock:
                self.pose, self.basket, self.error = self.cfg.dock, [], None
            self.publish_state(force=True)
            if (self.data_hash, self.loaded) != before:
                self.emit(self.hosted())
            self.log(f"store reset: {len(self.slots)} slots, {len(self.products)} products from the gateway")
            self._reply(data)
            return
        if data.get("robot_id") != self.cfg.robot_id:
            raise RigError("unknown_robot", f"robot {data.get('robot_id')!r} is not hosted by this bridge")
        if (self.data_hash, self.loaded) != before:
            self.emit(self.hosted())
        self._start_motion(data, RobotStatus.IDLE, lambda t0: self._home(data, t0))

    # ---- motions (one at a time; worker thread) --------------------------------------------------------------------
    def _start_motion(self, data: dict[str, Any], status: RobotStatus, body: Callable[[float], None]) -> None:
        with self._lock:
            self._busy_check()
            self._active = (data["cmd_id"], data.get("type", "?"))
            self.status, self.error = status, None
        self.stop_event.clear()
        self.publish_state(force=True)
        t0 = self.clock()

        def run() -> None:
            try:
                body(t0)
            except MotionCancelled as exc:
                self._end_motion(error=None)
                self._fail(data, "cancelled", str(exc) or "stopped", t0)
            except RigError as exc:
                self._end_motion(error=exc.message)
                self._fail(data, exc.code, exc.message, t0)
            except Exception as exc:  # noqa: BLE001 - one result per command
                log.exception("%s failed", data.get("type"))
                self._end_motion(error=f"{type(exc).__name__}: {exc}")
                self._fail(data, "internal", f"{type(exc).__name__}: {exc}", t0)
            self.publish_state(force=True)

        if self.threaded:
            self._thread = threading.Thread(target=run, name=f"so101-{data.get('type')}", daemon=True)
            self._thread.start()
        else:
            run()

    def _end_motion(self, error: str | None = None) -> None:
        with self._lock:
            self._active = None
            self.status = RobotStatus.ERROR if error else RobotStatus.IDLE
            self.error = error
            self.detail = ""

    def wait_idle(self, timeout: float = 10.0) -> bool:
        """Tests / shutdown: wait for the running motion to finish."""
        t = self._thread
        if t is not None:
            t.join(timeout)
        return self._active is None

    def _confirm_fn(self) -> Callable[[str], bool]:
        if self._confirm is None:
            return lambda _p: True
        return lambda prompt: self._confirm(prompt, self.stop_event)  # type: ignore[misc]

    def product_name(self, sku: str, slot_id: str | None = None) -> tuple[str, str]:
        """(what to ask the camera for, catalog display name) for a SKU."""
        prod = self.products.get(sku) or {}
        display = str(prod.get("name") or sku)
        prop = self.cfg.sku_names.get(sku) or self.cfg.default_prop or display
        return prop, display

    def _next_instance(self, slot_id: str) -> str:
        used = {i.instance_id for i in self.basket} | ({self.holding.instance_id} if self.holding else set())
        n = 0
        while instance_id(slot_id, n) in used:
            n += 1
        return instance_id(slot_id, n)

    def _to_store(self, xy: tuple[float, float] | None) -> Point3:
        """Robot-base (x forward, y left) table position -> store frame (the arm sits at ``pose``)."""
        if xy is None:
            return Point3(x=self.pose.x, y=self.pose.y, z=TABLE_Z_M)
        c, s = math.cos(self.pose.theta), math.sin(self.pose.theta)
        return Point3(x=self.pose.x + c * xy[0] - s * xy[1], y=self.pose.y + s * xy[0] + c * xy[1], z=TABLE_Z_M)

    def _pick(self, data: dict[str, Any], cmd: Any, t0: float) -> None:
        if self.holding is not None:
            raise RigError("grasp_failed", f"already holding {self.holding.instance_id}: place it first")
        prop, display = self.product_name(cmd.sku, cmd.slot_id)
        with self._lock:
            self.detail = f"picking {prop}"
        self.publish_state(force=True)
        if prop != display:
            self.log(f"pick {cmd.sku} ({display}): grasping the table prop {prop!r}")
        out = self.rig.pick(prop, self.stop_event, self._confirm_fn())
        iid = cmd.instance_id or self._next_instance(cmd.slot_id)
        unit = ItemInstance(
            instance_id=iid,
            sku=cmd.sku,
            store_id=self.cfg.store_id,
            slot_id=cmd.slot_id,
            position=self._to_store(out.target_xy),
            in_basket=False,
        )
        with self._lock:
            self.holding = unit
            slot = self.slots.get(cmd.slot_id)
            if slot is not None:  # one unit fewer on the shelf until the catalog's next set_slot_stock (as Isaac)
                slot["qty"] = max(0, int(slot.get("qty") or 0) - 1)
            self._active, self.status, self.detail = None, RobotStatus.IDLE, ""
        note = f" ({out.note})" if out.note else ""
        self.log(f"picked {prop!r} in {self.clock() - t0:.1f}s{note}")
        self._reply(data, t0, instance_id=iid, instances=[unit])

    def _place(self, data: dict[str, Any], t0: float) -> None:
        unit = self.holding
        assert unit is not None
        with self._lock:
            self.detail = f"placing {unit.sku}"
        self.publish_state(force=True)
        self.rig.place(self.stop_event, self._confirm_fn())
        placed = unit.model_copy(update={"in_basket": True, "position": self._to_store(None)})
        with self._lock:
            self.holding = None
            self.basket.append(placed)
            self._active, self.status, self.detail = None, RobotStatus.IDLE, ""
        self.emit(InstanceInfo(store_id=self.cfg.store_id, robot_id=self.cfg.robot_id, instances=[placed]))
        self._reply(data, t0, instance_id=placed.instance_id)

    def _home(self, data: dict[str, Any], t0: float) -> None:
        with self._lock:
            self.detail = "going home"
        self.rig.home(self.stop_event, self._confirm_fn())
        with self._lock:
            self.pose, self.basket = self.cfg.dock, []
            self._active, self.status, self.detail = None, RobotStatus.IDLE, ""
        self._reply(data, t0)

    # ---- ground truth (bookkeeping, not vision) --------------------------------------------------------------------
    def _slot_units(self, slot_id: str | None) -> list[ItemInstance]:
        """The catalog's units of a slot (from the gateway's planogram / set_slot_stock), minus the ones taken."""
        if slot_id is None or slot_id not in self.slots:
            return []
        slot = self.slots[slot_id]
        sku, qty = slot.get("sku"), int(slot.get("qty") or 0)
        if not sku:
            return []
        taken = {i.instance_id for i in self.basket} | ({self.holding.instance_id} if self.holding else set())
        pos = slot.get("item_position") if isinstance(slot.get("item_position"), dict) else None
        point = Point3(**{k: float(pos.get(k, 0.0)) for k in ("x", "y", "z")}) if pos else self._to_store(None)
        out, n = [], 0
        while len(out) < qty and n < qty + len(taken) + 1:
            iid = instance_id(slot_id, n)
            n += 1
            if iid not in taken:
                out.append(
                    ItemInstance(instance_id=iid, sku=sku, store_id=self.cfg.store_id, slot_id=slot_id, position=point)
                )
        return out

    def _units(self, slot_id: str | None = None) -> list[ItemInstance]:
        held = [self.holding] if self.holding else []
        mine = [*self.basket, *held]
        if slot_id is not None:
            return [*self._slot_units(slot_id), *(i for i in mine if i.slot_id == slot_id)]
        return [*(u for sid in self.slots for u in self._slot_units(sid)), *mine]  # whole store (as Isaac)


# =====================================================================================================================
# websocket link (mirrors arms/sim/app.py GatewayLink; arms may not import each other)
# =====================================================================================================================
class GatewayLink(threading.Thread):
    """Outbound websocket client with reconnect. Main loop reads ``inbox``: ("connected", welcome) /
    ("disconnected", reason) / ("cmd", raw JSON text). ``send`` / ``offer_frame`` are thread-safe."""

    def __init__(self, url: str, hello: Callable[[], SimHello], recv_poll_s: float = 0.01) -> None:
        super().__init__(name="so101-bridge-link", daemon=True)
        self.url = url
        self.hello = hello
        self.recv_poll_s = recv_poll_s
        self.inbox: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._out: queue.Queue[str] = queue.Queue()
        self._frames: dict[str, str] = {}
        self._frame_lock = threading.Lock()
        self._stop = threading.Event()
        self.connected = threading.Event()
        self.rejected: str | None = None

    def send(self, msg: Any) -> None:
        if not self.connected.is_set():
            return
        if isinstance(msg, CameraFrame):
            self.offer_frame(msg)
            return
        self._out.put(msg.model_dump_json() if hasattr(msg, "model_dump_json") else json.dumps(msg))

    def offer_frame(self, frame: CameraFrame) -> None:
        """Stream frames: keep only the latest per camera (never back up the socket on a slow uplink)."""
        if self.connected.is_set():
            with self._frame_lock:
                self._frames[frame.camera_id or frame.frame_id] = frame.model_dump_json()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect

        backoff = 1.0
        while not self._stop.is_set():
            try:
                with connect(self.url, open_timeout=10, max_size=None, close_timeout=2) as ws:
                    ws.send(self.hello().model_dump_json())
                    welcome = parse_gateway_to_sim(ws.recv(timeout=15))
                    if getattr(welcome, "type", None) != "welcome" or not welcome.ok:
                        err = getattr(welcome, "error", None) or f"unexpected {getattr(welcome, 'type', welcome)}"
                        self.rejected = err
                        log.error("gateway rejected the bridge: %s", err)
                        backoff = RECONNECT_MAX_S
                        raise ConnectionError(err)
                    self.rejected = None
                    log.info("linked to %s (session %s)", self.url.split("?")[0], welcome.session_id)
                    backoff = 1.0
                    self._drain_out()
                    self.connected.set()
                    self.inbox.put(("connected", welcome))
                    self._pump(ws)
            except (ConnectionClosed, OSError, TimeoutError, ConnectionError) as exc:
                log.warning("gateway link down (%s); retry in %.0fs", exc, backoff)
            except Exception:  # noqa: BLE001 - keep the bridge alive
                log.exception("gateway link crashed; retry in %.0fs", backoff)
            if self.connected.is_set():
                self.connected.clear()
                self.inbox.put(("disconnected", "link lost"))
            self._stop.wait(backoff)
            backoff = min(backoff * 2, RECONNECT_MAX_S)

    def _drain_out(self) -> None:
        while not self._out.empty():
            self._out.get_nowait()
        with self._frame_lock:
            self._frames = {}

    def _pump(self, ws: Any) -> None:
        while not self._stop.is_set():
            while True:
                try:
                    ws.send(self._out.get_nowait())
                except queue.Empty:
                    break
            with self._frame_lock:
                frames, self._frames = list(self._frames.values()), {}
            for frame in frames:
                ws.send(frame)
            try:
                raw = ws.recv(timeout=self.recv_poll_s)
            except TimeoutError:
                continue
            self.inbox.put(("cmd", raw))


# =====================================================================================================================
# operator terminal: y/N confirmations + emergency stop
# =====================================================================================================================
class Operator:
    """Reads the bridge's terminal on a thread (works on Windows too). While a confirmation is pending, the next line
    answers it (y / yes = go; anything else, a gateway stop or the timeout = no). Otherwise 's' / 'stop' halts the arm
    and 'q' quits."""

    def __init__(self, timeout_s: float = 20.0) -> None:
        self.timeout_s = timeout_s
        self.lines: queue.Queue[str] = queue.Queue()
        self.on_stop: Callable[[], None] = lambda: None
        self.quit = threading.Event()
        self._pending = threading.Event()
        threading.Thread(target=self._read, name="so101-bridge-stdin", daemon=True).start()

    def _read(self) -> None:
        for line in sys.stdin:
            text = line.strip().lower()
            if self._pending.is_set():
                self.lines.put(text)
            elif text in ("s", "stop"):
                self.on_stop()
            elif text in ("q", "quit", "exit"):
                self.quit.set()

    def confirm(self, prompt: str, stop: threading.Event) -> bool:
        print(f"\n>>> {prompt} - execute? [y/N] (auto-no in {self.timeout_s:.0f}s) ", end="", flush=True)
        while not self.lines.empty():
            self.lines.get_nowait()
        self._pending.set()
        try:
            end = time.monotonic() + self.timeout_s
            while time.monotonic() < end and not stop.is_set():
                try:
                    ans = self.lines.get(timeout=0.1)
                except queue.Empty:
                    continue
                return ans in ("y", "yes")
            print("(no answer: not moving)")
            return False
        finally:
            self._pending.clear()


# =====================================================================================================================
# process
# =====================================================================================================================
def fleet_robot(robot_id: str) -> tuple[str, str] | None:
    """(store_id, name) of a fleet robot from data/fixtures/robots.yaml, or None."""
    try:
        import yaml

        from utils.config import get_settings

        path = Path(get_settings().data_dir) / "fixtures" / "robots.yaml"
        for r in (yaml.safe_load(path.read_text()) or {}).get("robots") or []:
            if r.get("robot_id") == robot_id:
                return str(r["store_id"]), str(r.get("name") or robot_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read robots.yaml (%s)", exc)
    return None


def parse_map(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        sku, sep, name = item.partition("=")
        if not sep or not sku.strip() or not name.strip():
            raise SystemExit(f"--map expects SKU=NAME, got {item!r}")
        out[sku.strip()] = name.strip()
    return out


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m arms.robot.so101.bridge",
        description="Connect the physical SO-101 rig to the Picky gateway like an Isaac sim worker.",
    )
    ap.add_argument("--gateway", default=None, help="gateway base URL (default PICKY_GATEWAY_URL)")
    ap.add_argument("--token", default=None, help="sim token (default PICKY_SIM_TOKEN)")
    ap.add_argument("--dry", action="store_true", help="no serial port: arm motion is simulated and logged")
    ap.add_argument(
        "--camera", default=None, help="'fake' | 'image:<file or folder>' | camera index / name (default SO101_CAMERA)"
    )
    ap.add_argument("--wrist-camera", default=None, help="second camera index / name, advertised as '<robot>.wrist'")
    ap.add_argument("--robot", default=DEFAULT_ROBOT, help=f"robot id (default {DEFAULT_ROBOT})")
    ap.add_argument("--store", default=None, help=f"store id (default {DEFAULT_STORE}, or the stand-in robot's store)")
    ap.add_argument(
        "--stand-in", metavar="ROBOT_ID", default=None, help="host this FLEET robot + its store (e.g. rover-hill-1)"
    )
    ap.add_argument("--dest", default="plate", help="what the arm drops picks into on the table (default 'plate')")
    ap.add_argument("--map", action="append", default=[], metavar="SKU=NAME", help="SKU -> table prop to grasp")
    ap.add_argument("--default-prop", default=None, help="prop to grasp for any unmapped SKU (e.g. 'gum')")
    ap.add_argument("--confirm", action="store_true", help="ask y/N in this terminal before every arm motion")
    ap.add_argument("--confirm-timeout", type=float, default=20.0, help="seconds before an unanswered prompt = no")
    ap.add_argument("--step-s", type=float, default=0.4, help="--dry: simulated seconds per waypoint")
    ap.add_argument("--worker-id", default=None, help="worker id shown in /api/sim/status")
    return ap


def main(argv: list[str] | None = None) -> None:
    import os

    from utils.config import get_settings

    args = build_parser().parse_args(argv)
    settings = get_settings()
    gateway = args.gateway or os.environ.get("PICKY_GATEWAY_URL") or settings.picky_gateway_url
    token = args.token or os.environ.get("PICKY_SIM_TOKEN") or settings.picky_sim_token
    robot_id, store_id, name = args.robot, args.store or DEFAULT_STORE, "SO-101 (real)"
    if args.stand_in:
        robot_id = args.stand_in
        found = fleet_robot(robot_id)
        if args.store:
            store_id = args.store
        elif found:
            store_id = found[0]
        else:
            raise SystemExit(f"--stand-in {robot_id}: unknown fleet robot; pass --store")
        name = f"{found[1] if found else robot_id} (real SO-101)"
    cfg = BridgeConfig(
        robot_id=robot_id,
        store_id=store_id,
        name=name,
        worker_id=args.worker_id or f"so101-bridge-{robot_id}",
        stand_in=bool(args.stand_in),
        sku_names=parse_map(args.map),
        default_prop=args.default_prop,
    )
    cam_spec = args.camera or os.environ.get("SO101_CAMERA", "0")
    front: FrameSource = open_camera_source(cam_spec)
    cameras: dict[str, FrameSource] = {"front": front}
    if args.wrist_camera:
        cameras["wrist"] = open_camera_source(args.wrist_camera, register=False)

    operator = Operator(timeout_s=args.confirm_timeout)

    def say(msg: str) -> None:
        print(msg, flush=True)

    if args.dry:
        planner = None
        if front.real:
            try:
                from arms.robot.so101.bridge_rig import RoutinePlanner

                planner = RoutinePlanner(dest=args.dest)
                say("dry: real perception + IK planning enabled (camera calibration found)")
            except Exception as exc:  # noqa: BLE001 - no calibration / ikpy / URDF: simulate the plan
                say(f"dry: planning simulated ({type(exc).__name__}: {exc})")
        rig: Any = DryRig(front, step_s=args.step_s, planner=planner, say=say)
    else:
        if isinstance(front, FakeCamera):
            raise SystemExit("the real arm needs the real camera: drop --camera fake, or add --dry")
        from arms.robot.so101.bridge_rig import So101Rig

        home_now = True
        if args.confirm:
            home_now = operator.confirm("connect and move the arm to its home pose", threading.Event())
        rig = So101Rig(front, dest=args.dest, say=say, home_on_connect=home_now)

    url = gateway_ws_url(gateway, token)
    link: GatewayLink | None = None

    def emit(msg: Any) -> None:
        if link is not None:
            link.send(msg)

    core = BridgeCore(cfg, rig, cameras, emit, confirm=operator.confirm if args.confirm else None)
    operator.on_stop = core.emergency_stop
    link = GatewayLink(url, hello=lambda: core.hello(token))
    say(
        f"SO-101 bridge -> {url.split('?')[0]}  worker={cfg.worker_id}  store={cfg.store_id}  robot={cfg.robot_id}  "
        f"cameras={core.camera_ids()}  rig={rig.name}{' (confirm each motion)' if args.confirm else ''}\n"
        "type 's' + Enter to stop the arm, 'q' + Enter to quit"
    )
    link.start()
    try:
        while not operator.quit.is_set():
            try:
                kind, item = link.inbox.get(timeout=0.02)
            except queue.Empty:
                kind, item = None, None
            if kind == "connected":
                core.on_connected()
            elif kind == "disconnected":
                core.on_disconnected()
                say("gateway link lost; reconnecting (the arm was halted)")
            elif kind == "cmd":
                core.submit(item)
            if link.connected.is_set():
                for cid in core.stream_due():
                    try:
                        link.offer_frame(core.frame(cid))
                    except Exception as exc:  # noqa: BLE001 - a camera hiccup must not kill the link
                        log.warning("stream frame %s failed: %s", cid, exc)
                core.publish_state()
    except KeyboardInterrupt:
        pass
    finally:
        say("shutting down: halting the arm and going home")
        core.stop_event.set()
        core.wait_idle(15)
        link.stop()
        try:
            rig.close()
        finally:
            for src in cameras.values():
                src.close()


if __name__ == "__main__":
    main()
