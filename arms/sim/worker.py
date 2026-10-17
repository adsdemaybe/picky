"""Isaac-free core of the sim worker: command handling + non-blocking action state machines + ground truth.

``arms/sim/app.py`` owns the process: a websocket thread fills ``inbox`` and drains ``outbox``; the main loop calls
``WorkerCore.submit(cmd)`` for every queued command and ``WorkerCore.tick(dt)`` once per rendered frame. All USD /
rendering goes through a ``SimBackend`` (Isaac in app.py, ``NullBackend`` here for tests and the kinematic mode), so
the protocol logic (busy / cancelled / error codes / one CommandResult per cmd_id) is unit-tested on a laptop.

Motion is kinematic (set-pose) and grasping is "magic" (snap the item to the gripper, then to the basket drop pose),
as picked in CLAUDE.md §9 (robot row): reliable under demo pressure, no controller APIs.
"""

import base64
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from arms.sim.scene_spec import SceneSpec, basket_drop_position, build_scene_spec, gripper_position
from contracts import CONTRACTS_VERSION
from contracts.catalog import Product
from contracts.common import BBox, CameraName, Point3, Pose2D
from contracts.planogram import Planogram
from contracts.sim import (
    MOTION_COMMANDS,
    CameraFrame,
    CaptureCmd,
    CommandResult,
    InstanceInfo,
    ItemInstance,
    ListInstancesCmd,
    NavigateCmd,
    PickCmd,
    PlaceInBasketCmd,
    ResetCmd,
    RobotState,
    RobotStatus,
    ScanCmd,
    SimErrorCode,
    SimHello,
    SimLog,
    StopCmd,
)

DEFAULT_SPEED_MPS = 0.8
YAW_RATE_RADPS = 1.8
SCAN_S = 0.6
PICK_S = 1.5
PLACE_S = 1.0
REACH_M = 1.5
"""Max distance from the slot's approach pose for a pick (else grasp_failed: out of reach)."""
STATE_MIN_PERIOD_S = 0.1  # RobotState <= 10 Hz
HEAD_HEIGHT_M = 1.35
FRAME_W, FRAME_H = 960, 540
HFOV_RAD = math.radians(70)
CAPABILITIES = ["navigate", "scan", "pick", "place_in_basket", "capture", "list_instances", "reset", "camera:head"]

# 1x1 grey baseline JPEG: placeholder image for NullBackend (no PIL / cv2 in base deps).
PLACEHOLDER_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////"
    "////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)


# --------------------------------------------------------------------------------------------------------------------
# Camera math (shared by Isaac + null backends so bboxes match what is rendered)
# --------------------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class CameraPose:
    """Pinhole camera in the world frame. yaw CCW from +x, pitch > 0 looks up. USD camera: local -Z = forward."""

    x: float
    y: float
    z: float
    yaw: float
    pitch: float = 0.0
    hfov: float = HFOV_RAD
    w: int = FRAME_W
    h: int = FRAME_H

    def basis(self) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
        """(right, up, forward) unit vectors in the world frame."""
        cy, sy, cp, sp = math.cos(self.yaw), math.sin(self.yaw), math.cos(self.pitch), math.sin(self.pitch)
        f = (cy * cp, sy * cp, sp)
        r = (sy, -cy, 0.0)
        u = (r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0])
        return r, u, f

    def project(self, p: tuple[float, float, float]) -> tuple[float, float] | None:
        r, u, f = self.basis()
        v = (p[0] - self.x, p[1] - self.y, p[2] - self.z)
        zc = v[0] * f[0] + v[1] * f[1] + v[2] * f[2]
        if zc <= 0.05:
            return None
        xc = v[0] * r[0] + v[1] * r[1] + v[2] * r[2]
        yc = v[0] * u[0] + v[1] * u[1] + v[2] * u[2]
        fx = (self.w / 2) / math.tan(self.hfov / 2)
        return self.w / 2 + fx * xc / zc, self.h / 2 - fx * yc / zc


def look_at(eye: tuple[float, float, float], target: tuple[float, float, float], **kw: Any) -> CameraPose:
    dx, dy, dz = target[0] - eye[0], target[1] - eye[1], target[2] - eye[2]
    return CameraPose(eye[0], eye[1], eye[2], math.atan2(dy, dx), math.atan2(dz, math.hypot(dx, dy)), **kw)


def head_camera(pose: Pose2D, target: Point3 | None = None) -> CameraPose:
    eye = (pose.x + 0.2 * math.cos(pose.theta), pose.y + 0.2 * math.sin(pose.theta), HEAD_HEIGHT_M)
    if target is None:
        return CameraPose(eye[0], eye[1], eye[2], pose.theta, -0.15)
    return look_at(eye, (target.x, target.y, target.z))


def project_box(cam: CameraPose, center: Point3, dims: tuple[float, float, float], yaw: float) -> BBox | None:
    """Pixel bbox of an oriented box (clipped to the image), or None when it is not visible."""
    c, s = math.cos(yaw), math.sin(yaw)
    pts = []
    for sx in (-0.5, 0.5):
        for sy in (-0.5, 0.5):
            for sz in (-0.5, 0.5):
                lx, ly = sx * dims[0], sy * dims[1]
                pts.append((center.x + c * lx - s * ly, center.y + s * lx + c * ly, center.z + sz * dims[2]))
    px = [q for q in (cam.project(p) for p in pts) if q is not None]
    if len(px) < 8:
        return None
    x0, x1 = max(0.0, min(q[0] for q in px)), min(float(cam.w), max(q[0] for q in px))
    y0, y1 = max(0.0, min(q[1] for q in px)), min(float(cam.h), max(q[1] for q in px))
    if x1 - x0 < 1 or y1 - y0 < 1:
        return None
    return BBox(x=round(x0, 1), y=round(y0, 1), w=round(x1 - x0, 1), h=round(y1 - y0, 1))


# --------------------------------------------------------------------------------------------------------------------
# Backend seam
# --------------------------------------------------------------------------------------------------------------------
class SimBackend(Protocol):
    """What the core needs from the renderer. Isaac implements it in app.py; NullBackend below."""

    def build(self, spec: SceneSpec) -> None: ...

    def set_robot_pose(self, pose: Pose2D) -> None: ...

    def set_instance_position(self, instance_id: str, position: Point3) -> None: ...

    def render(self, camera: CameraName, cam: CameraPose) -> tuple[bytes, int, int] | None:
        """JPEG bytes + (w, h) of the given camera pose, or None when no frame is ready yet."""
        ...


class NullBackend:
    """No renderer: records calls and returns a placeholder JPEG. Used by tests and ``app.py --backend kinematic``."""

    def __init__(self) -> None:
        self.built: SceneSpec | None = None
        self.robot_pose: Pose2D | None = None
        self.positions: dict[str, Point3] = {}

    def build(self, spec: SceneSpec) -> None:
        self.built = spec
        self.positions = {i.instance_id: i.position for i in spec.instances}

    def set_robot_pose(self, pose: Pose2D) -> None:
        self.robot_pose = pose

    def set_instance_position(self, instance_id: str, position: Point3) -> None:
        self.positions[instance_id] = position

    def render(self, camera: CameraName, cam: CameraPose) -> tuple[bytes, int, int] | None:
        return PLACEHOLDER_JPEG, cam.w, cam.h


# --------------------------------------------------------------------------------------------------------------------
# Actions (non-blocking state machines: step(dt) -> CommandResult when done, else None)
# --------------------------------------------------------------------------------------------------------------------
class _Fail(Exception):
    def __init__(self, code: SimErrorCode, message: str = "") -> None:
        self.code = code
        self.message = message or code
        super().__init__(message)


class _Action:
    status = RobotStatus.IDLE

    def __init__(self, core: "WorkerCore", cmd: Any) -> None:
        self.core, self.cmd, self.elapsed = core, cmd, 0.0

    def step(self, dt: float) -> CommandResult | None:  # pragma: no cover - interface
        raise NotImplementedError


class _Navigate(_Action):
    status = RobotStatus.NAVIGATING

    def __init__(self, core: "WorkerCore", cmd: NavigateCmd) -> None:
        super().__init__(core, cmd)
        b = core.spec.bounds if core.spec else None
        final = cmd.waypoints[-1]
        if b is not None and not b.contains(final.x, final.y):
            raise _Fail("path_blocked", f"final waypoint ({final.x}, {final.y}) is outside the store")
        self.queue = list(cmd.waypoints)
        self.speed = cmd.speed_mps or DEFAULT_SPEED_MPS
        start = core.pose
        length = sum(
            math.hypot(b2.x - a.x, b2.y - a.y) for a, b2 in zip([start, *cmd.waypoints], cmd.waypoints, strict=False)
        )
        self.budget_s = 3 * length / self.speed + 10.0

    def step(self, dt: float) -> CommandResult | None:
        self.elapsed += dt
        if self.elapsed > self.budget_s:
            raise _Fail("timeout", f"navigate exceeded {self.budget_s:.0f}s")
        pose, cmd = self.core.pose, self.cmd
        budget = self.speed * dt
        while self.queue and budget > 0:
            target = self.queue[0]
            dx, dy = target.x - pose.x, target.y - pose.y
            dist = math.hypot(dx, dy)
            final = len(self.queue) == 1
            tol = cmd.tolerance_m if final else max(cmd.tolerance_m, 0.1)
            if dist <= tol:
                if final:
                    break
                self.queue.pop(0)
                continue
            move = min(budget, dist)
            heading = math.atan2(dy, dx)
            pose = Pose2D(x=pose.x + move * dx / dist, y=pose.y + move * dy / dist, theta=heading)
            budget -= move
        if len(self.queue) == 1 and math.hypot(self.queue[0].x - pose.x, self.queue[0].y - pose.y) <= cmd.tolerance_m:
            goal = self.queue[0]
            err = math.atan2(math.sin(goal.theta - pose.theta), math.cos(goal.theta - pose.theta))
            turn = max(-YAW_RATE_RADPS * dt, min(YAW_RATE_RADPS * dt, err))
            pose = Pose2D(
                x=pose.x, y=pose.y, theta=math.atan2(math.sin(pose.theta + turn), math.cos(pose.theta + turn))
            )
            if abs(err - turn) <= cmd.yaw_tolerance_rad:
                self.core.set_pose(pose)
                return self.core.result(cmd, ok=True)
        self.core.set_pose(pose)
        return None


class _Scan(_Action):
    status = RobotStatus.SCANNING

    def __init__(self, core: "WorkerCore", cmd: ScanCmd) -> None:
        super().__init__(core, cmd)
        self.slot = core.require_slot(cmd.slot_id)

    def step(self, dt: float) -> CommandResult | None:
        self.elapsed += dt
        if self.elapsed < SCAN_S:
            return None
        frame = self.core.capture_frame(self.cmd.camera, slot_id=self.cmd.slot_id)
        return self.core.result(self.cmd, ok=True, frame=frame)


class _Pick(_Action):
    status = RobotStatus.PICKING

    def __init__(self, core: "WorkerCore", cmd: PickCmd) -> None:
        super().__init__(core, cmd)
        slot = core.require_slot(cmd.slot_id)
        if core.holding is not None:
            raise _Fail("grasp_failed", f"gripper already holds {core.holding}")
        if core.pose.distance_to(slot.approach_pose) > REACH_M:
            raise _Fail("grasp_failed", f"slot {cmd.slot_id} out of reach; navigate to its approach pose first")
        candidates = [
            i for i in core.instances.values() if i.slot_id == cmd.slot_id and i.sku == cmd.sku and not i.in_basket
        ]
        if cmd.instance_id is not None:
            candidates = [i for i in candidates if i.instance_id == cmd.instance_id]
        if not candidates:
            raise _Fail("no_instance", f"no {cmd.sku} in slot {cmd.slot_id}")
        ap = slot.approach_pose
        self.target = min(candidates, key=lambda i: (i.position.x - ap.x) ** 2 + (i.position.y - ap.y) ** 2)

    def step(self, dt: float) -> CommandResult | None:
        self.elapsed += dt
        if self.elapsed < PICK_S:
            return None
        inst = self.core.instances[self.target.instance_id]
        self.core.holding = inst.instance_id
        self.core.move_instance(inst.instance_id, gripper_position(self.core.pose))
        held = self.core.instances[inst.instance_id]
        return self.core.result(self.cmd, ok=True, instance_id=held.instance_id, instances=[held])


class _Place(_Action):
    status = RobotStatus.PLACING

    def __init__(self, core: "WorkerCore", cmd: PlaceInBasketCmd) -> None:
        super().__init__(core, cmd)
        if core.holding is None or (cmd.instance_id is not None and cmd.instance_id != core.holding):
            raise _Fail("no_instance", f"not holding {cmd.instance_id or 'anything'}")
        self.iid = core.holding

    def step(self, dt: float) -> CommandResult | None:
        self.elapsed += dt
        if self.elapsed < PLACE_S:
            return None
        core = self.core
        inst = core.instances[self.iid]
        core.instances[self.iid] = inst.model_copy(update={"in_basket": True})
        core.basket.append(self.iid)
        core.holding = None
        core.sync_carried()
        return core.result(self.cmd, ok=True, instance_id=self.iid, instances=[core.instances[self.iid]])


# --------------------------------------------------------------------------------------------------------------------
# Core
# --------------------------------------------------------------------------------------------------------------------
SpecLoader = Callable[[str, int], tuple[Planogram, list[Product]]]


def _default_loader(store_id: str, seed: int) -> tuple[Planogram, list[Product]]:
    from utils.data import load_planogram, load_products

    return load_planogram(store_id), list(load_products().values())


class WorkerCore:
    """Single-threaded: call only from the main (render) loop. Outgoing messages go through ``emit``."""

    def __init__(
        self,
        backend: SimBackend,
        emit: Callable[[Any], None],
        store_id: str,
        clock: Callable[[], float] = time.monotonic,
        loader: SpecLoader = _default_loader,
    ) -> None:
        self.backend, self.emit, self.store_id, self.clock, self.loader = backend, emit, store_id, clock, loader
        self.spec: SceneSpec | None = None
        self.planogram: Planogram | None = None
        self.instances: dict[str, ItemInstance] = {}
        self.pose = Pose2D(x=0.0, y=0.0)
        self.holding: str | None = None
        self.basket: list[str] = []
        self.active: _Action | None = None
        self.active_started = 0.0
        self.error: str | None = None
        self._frame_n = 0
        self._last_state: tuple[Any, ...] | None = None
        self._last_state_t = -1e9

    # ---- scene -------------------------------------------------------------------------------------------------
    def load_scene(
        self,
        store_id: str | None = None,
        seed: int | None = None,
        planogram: Planogram | None = None,
        products: list[Product] | None = None,
    ) -> SceneSpec:
        sid = (planogram.store_id if planogram else None) or store_id or self.store_id
        seed = seed or 0
        if planogram is None or products is None:
            loaded_plan, loaded_products = self.loader(sid, seed)
            planogram = planogram or loaded_plan
            products = products or loaded_products
        spec = build_scene_spec(planogram, products, seed=seed)
        for w in spec.warnings:
            self.emit(SimLog(level="warning", message=f"scene: {w}"))
        self.backend.build(spec)
        self.spec, self.planogram, self.store_id = spec, planogram, spec.store_id
        self.instances = {i.instance_id: i for i in spec.instances}
        self.holding, self.basket, self.error = None, [], None
        self.set_pose(spec.robot_home)
        self.emit(InstanceInfo(instances=list(self.instances.values())))
        return spec

    def hello(self, token: str, worker_id: str = "isaac-0", sim_version: str | None = None) -> SimHello:
        return SimHello(
            token=token,
            store_id=self.store_id,
            worker_id=worker_id,
            capabilities=CAPABILITIES,
            sim_version=sim_version,
            contracts_version=CONTRACTS_VERSION,
            data_hash=self.spec.data_hash if self.spec else None,
        )

    def require_slot(self, slot_id: str) -> Any:
        if self.planogram is None:
            raise _Fail("not_ready", "no scene loaded; send reset")
        slot = self.planogram.slot(slot_id)
        if slot is None:
            raise _Fail("unknown_slot", f"unknown slot {slot_id!r}")
        return slot

    # ---- robot + instances ----------------------------------------------------------------------------------------
    def set_pose(self, pose: Pose2D) -> None:
        self.pose = pose
        self.backend.set_robot_pose(pose)
        self.sync_carried()

    def move_instance(self, iid: str, position: Point3) -> None:
        self.instances[iid] = self.instances[iid].model_copy(update={"position": position})
        self.backend.set_instance_position(iid, position)

    def sync_carried(self) -> None:
        """Held + basket items ride with the robot."""
        if self.holding is not None and self.holding in self.instances:
            self.move_instance(self.holding, gripper_position(self.pose))
        for k, iid in enumerate(self.basket):
            dims = self.spec.item_dims.get(iid, (0.08, 0.08, 0.15)) if self.spec else (0.08, 0.08, 0.15)
            self.move_instance(iid, basket_drop_position(self.pose, k, dims))

    @property
    def status(self) -> RobotStatus:
        if self.error:
            return RobotStatus.ERROR
        return self.active.status if self.active else RobotStatus.IDLE

    def state(self) -> RobotState:
        return RobotState(
            store_id=self.store_id,
            pose=self.pose,
            status=self.status,
            holding=self.holding,
            basket=list(self.basket),
            basket_skus=[self.instances[i].sku for i in self.basket if i in self.instances],
            current_cmd_id=self.active.cmd.cmd_id if self.active else None,
            error=self.error,
        )

    # ---- frames ----------------------------------------------------------------------------------------------------
    def capture_frame(self, camera: CameraName = "head", slot_id: str | None = None) -> CameraFrame | None:
        target = None
        if slot_id and self.planogram and (slot := self.planogram.slot(slot_id)):
            target = Point3(x=slot.item_position.x, y=slot.item_position.y, z=slot.item_position.z + 0.1)
        cam = head_camera(self.pose, target)
        rendered = self.backend.render(camera, cam)
        if rendered is None:
            return None
        jpeg, w, h = rendered
        cam = CameraPose(cam.x, cam.y, cam.z, cam.yaw, cam.pitch, cam.hfov, w, h)
        visible: list[ItemInstance] = []
        if self.spec is not None:
            for inst in self.instances.values():
                if slot_id is not None and inst.slot_id != slot_id:
                    continue
                if inst.in_basket or inst.instance_id == self.holding:
                    continue
                dims = self.spec.item_dims.get(inst.instance_id, (0.08, 0.08, 0.15))
                bbox = project_box(cam, inst.position, dims, self.spec.item_yaw.get(inst.instance_id, 0.0))
                if bbox is not None:
                    visible.append(inst.model_copy(update={"bbox": bbox}))
        self._frame_n += 1
        return CameraFrame(
            frame_id=f"{self.store_id}-f{self._frame_n:06d}",
            camera=camera,
            jpeg_b64=base64.b64encode(jpeg).decode("ascii"),
            w=w,
            h=h,
            slot_id=slot_id,
            pose=self.pose,
            instances=visible,
        )

    # ---- commands --------------------------------------------------------------------------------------------------
    def result(self, cmd: Any, ok: bool, **kw: Any) -> CommandResult:
        started = self.active_started if self.active and self.active.cmd is cmd else self.clock()
        return CommandResult(cmd_id=cmd.cmd_id, ok=ok, duration_ms=round((self.clock() - started) * 1000, 1), **kw)

    def _finish(self, res: CommandResult) -> None:
        self.active = None
        self.emit(res.model_copy(update={"state": self.state()}))
        self.publish_state(force=True)

    def _fail(self, cmd: Any, code: SimErrorCode, message: str) -> None:
        res = self.result(cmd, ok=False, error_code=code, error=message)
        if self.active is not None and self.active.cmd is cmd:
            self._finish(res)
        else:
            self.emit(res.model_copy(update={"state": self.state()}))

    def submit(self, cmd: Any) -> None:
        """Handle one GatewayToSim command (never raises; always ends in exactly one CommandResult)."""
        try:
            if isinstance(cmd, StopCmd):
                if self.active is not None:
                    self._fail(self.active.cmd, "cancelled", f"stopped by {cmd.cmd_id}")
                self.emit(self.result(cmd, ok=True, state=self.state()))
                return
            if isinstance(cmd, ListInstancesCmd):
                found = [i for i in self.instances.values() if cmd.slot_id is None or i.slot_id == cmd.slot_id]
                self.emit(self.result(cmd, ok=True, instances=found, state=self.state()))
                return
            if isinstance(cmd, CaptureCmd):
                frame = self.capture_frame(cmd.camera)
                if frame is None:
                    raise _Fail("not_ready", "renderer has no frame yet")
                self.emit(self.result(cmd, ok=True, frame=frame, state=self.state()))
                return
            if getattr(cmd, "type", None) in MOTION_COMMANDS and self.active is not None:
                raise _Fail("busy", f"{self.active.cmd.type} {self.active.cmd.cmd_id} is running")
            if isinstance(cmd, ResetCmd):
                t0 = self.clock()
                self.load_scene(cmd.store_id, cmd.seed, cmd.planogram, cmd.products)
                res = CommandResult(cmd_id=cmd.cmd_id, ok=True, duration_ms=round((self.clock() - t0) * 1000, 1))
                self.emit(res.model_copy(update={"state": self.state()}))
                self.publish_state(force=True)
                return
            if self.spec is None:
                raise _Fail("not_ready", "no scene loaded; send reset")
            action_cls = {NavigateCmd: _Navigate, ScanCmd: _Scan, PickCmd: _Pick, PlaceInBasketCmd: _Place}.get(
                type(cmd)
            )
            if action_cls is None:
                raise _Fail("internal", f"unsupported command {getattr(cmd, 'type', cmd)!r}")
            self.active = action_cls(self, cmd)
            self.active_started = self.clock()
            self.publish_state(force=True)
        except _Fail as exc:
            self._fail(cmd, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - one result per cmd, whatever happens
            self.active = None if self.active is not None and self.active.cmd is cmd else self.active
            self._fail(cmd, "internal", f"{type(exc).__name__}: {exc}")

    def tick(self, dt: float) -> None:
        """Advance the in-flight action by ``dt`` seconds (call once per main-loop iteration)."""
        if self.active is not None:
            cmd = self.active.cmd
            try:
                res = self.active.step(dt)
                if res is not None:
                    self._finish(res)
            except _Fail as exc:
                self._fail(cmd, exc.code, exc.message)
            except Exception as exc:  # noqa: BLE001
                self._fail(cmd, "internal", f"{type(exc).__name__}: {exc}")
        self.publish_state()

    def abort(self, reason: str = "disconnected") -> None:
        """Gateway link dropped: abort the in-flight action silently (its result would be dropped anyway)."""
        self.active = None

    def publish_state(self, force: bool = False) -> None:
        now = self.clock()
        st = self.state()
        key = (round(st.pose.x, 2), round(st.pose.y, 2), round(st.pose.theta, 2), st.status, st.holding, len(st.basket))
        if force or (key != self._last_state and now - self._last_state_t >= STATE_MIN_PERIOD_S):
            self._last_state, self._last_state_t = key, now
            self.emit(st)
