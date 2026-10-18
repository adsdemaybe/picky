"""T1 learned grasp: drive a ``PolicyBackend`` (arms.robot.policy_backends: Perceptron / SmolVLA / scripted) joint by
joint on the Isaac worker over contracts 0.2.2 ``ArmJointsCmd``. Called by ``T0Vla`` for a grasp step resolved to T1.

    reset(instruction)                                    "pick up the <product> and put it in the basket"
    ArmJointsCmd([], lift_height_m = target unit height)  joint mode at a fixed lift (policies drive 6 joints only)
    ArmJointsCmd(home rows)                               to the recorded demos' start pose (PICKY_VLA_T1_HOME_DEG)
    loop (<= max_chunks, <= timeout_s, inside the step deadline):
        side + wrist frames of the last result -> blur (perception) -> [backend.set_context(PolicyContext)]
        -> backend.predict(state, side, wrist)
        -> ArmJointsCmd(chunk rows[:exec_steps] at the chunk's action_hz, capture_every = rows per picture)
           -> state, grasp events, pictures taken WHILE the arm moves (PICKY_VLA_T1_CAPTURE_HZ, default 4 Hz)
        stop when the sticky grasp attached a unit with the jaws closed (or the policy says done: grasp_missed)
    ArmJointsCmd([], end=True)                            the scripted rig takes the arm back (unit stays held)

Pictures while moving: every chunk asks the worker for side + wrist frames every ``capture_every`` rows on top of
the observation after its last row. ALL of them go to the robot's live view (``SimRobot.feed_policy_frames``: the
web app's front / wrist tiles show the policy cameras during the grasp, blurred by CameraService like any frame);
the LAST pair of each chunk is the next observation (closed loop: a policy never sees a stale frame).

Cartesian policies (``arms.robot.vla_mk15``: Perceptron Mk1.5 pointing in the images) get a ``PolicyContext`` before
each predict: camera poses + roll of the frames they see, the TCP, the slot (planogram depth of the shelf face) and
``ik()`` = worker-side inverse kinematics (contracts 0.2.2 ``ArmIkCmd``) on the real SO-101 model.

Design (why the robot arm runs the loop and the worker only executes joints + renders): contracts/CHANGES-0.2.2.md.
One websocket round trip per chunk next to the policy's own inference; Perceptron's session tokens stay in the
backend; the worker stays policy-agnostic. The result is a CLAIM: the verifier checks the postcondition.

Privacy: frames are blurred with ``services.perception.blur_faces`` before ``predict``. Isaac store frames are
synthetic renders with no people in the scene, so when no perception service is wired they go to the policy as is
(``frames_blurred=True`` = "known to hold no faces", the evidence-camera convention); it is logged once.
"""

import asyncio
import base64
import contextlib
import math
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from contracts.common import LIFT_MAX_M, LIFT_MIN_M, InstanceId, RobotId, Sku, utcnow
from contracts.planogram import Slot
from contracts.sim import ARM_JOINTS_MAX_STEPS, ArmJointState, CameraFrame, CommandResult, RobotError
from contracts.vla import VlaErrorCode
from utils.config import get_settings
from utils.log import get_logger

log = get_logger(__name__)

DEFAULT_HOME_DEG: tuple[float, ...] = (0.0, -100.0, 90.0, 50.0, 45.0, 7.0)
"""Start pose of the recorded demos (arms/sim/tools/record_lerobot: LeIsaac rest + noise, wrist roll U(0, 90),
gripper closed + U(0, 8)): their means, in USD joint degrees."""
DEFAULT_INSTRUCTION = "pick up the {name} and put it in the basket"
"""= the recorded dataset's task string (record_lerobot.plan.EpisodeSpec.task); {name} = product name, lower case."""
HOME_S = 1.0
"""Seconds to move from the scripted stow pose to the home pose (30 rows)."""
END_TIMEOUT_S = 8.0
UNIT_LIFT_FALLBACK_M = 0.06
"""Without ground truth: the target unit's center ~ this far above its shelf board (slot.item_position.z)."""
STEP_RESERVE_S = 2.5
"""Kept free of the step deadline for leaving joint mode + the terminal status."""
DEFAULT_CAPTURE_HZ = 4.0
"""Pictures per second while a chunk plays (side + wrist), on top of the observation after its last row."""


def _setting(name: str, default: Any) -> Any:
    try:
        value = getattr(get_settings(), name, None)
    except Exception:  # noqa: BLE001 - config errors must not take the robot down
        return default
    return default if value is None or value == "" else value


def parse_home(value: str | Sequence[float] | None) -> tuple[float, ...] | None:
    """'0,-100,90,50,45,7' -> 6 floats; blank / malformed -> None (start from the scripted stow pose)."""
    if value is None:
        return None
    if isinstance(value, str):
        parts = [p for p in value.replace(" ", "").split(",") if p]
        try:
            vals = tuple(float(p) for p in parts)
        except ValueError:
            return None
    else:
        vals = tuple(float(v) for v in value)
    return vals if len(vals) == 6 and all(math.isfinite(v) for v in vals) else None


@dataclass(frozen=True)
class PolicyGraspConfig:
    timeout_s: float = 25.0
    max_chunks: int = 12
    exec_steps: int = 25
    home_deg: tuple[float, ...] | None = DEFAULT_HOME_DEG
    instruction: str = DEFAULT_INSTRUCTION
    hz: float | None = None
    """Rows per second sent to the worker; None = the chunk's action_hz (30 for SO-101 policies)."""
    capture_hz: float = DEFAULT_CAPTURE_HZ
    """Pictures per second while the arm moves (0 = only after each chunk)."""

    def capture_every(self, hz: float, rows: int) -> int:
        """Rows between two pictures for a chunk of ``rows`` played at ``hz`` (0 = none mid-chunk)."""
        if self.capture_hz <= 0 or rows <= 1:
            return 0
        every = max(1, round(hz / self.capture_hz))
        return every if every < rows else 0

    @classmethod
    def from_settings(cls) -> "PolicyGraspConfig":
        """PICKY_VLA_T1_TIMEOUT_S / _MAX_CHUNKS / _EXEC_STEPS / _HOME_DEG / _INSTRUCTION through utils.config."""
        home_raw = _setting("picky_vla_t1_home_deg", None)
        try:
            home_blank = home_raw is not None and not str(home_raw).strip()
            return cls(
                timeout_s=max(1.0, float(_setting("picky_vla_t1_timeout_s", 25.0))),
                max_chunks=max(1, int(_setting("picky_vla_t1_max_chunks", 12))),
                exec_steps=max(1, min(ARM_JOINTS_MAX_STEPS, int(_setting("picky_vla_t1_exec_steps", 25)))),
                home_deg=None if home_blank else (parse_home(home_raw) if home_raw else DEFAULT_HOME_DEG),
                instruction=str(_setting("picky_vla_t1_instruction", DEFAULT_INSTRUCTION)),
                capture_hz=max(0.0, float(_setting("picky_vla_t1_capture_hz", DEFAULT_CAPTURE_HZ))),
            )
        except (TypeError, ValueError):
            return cls()

    def instruction_for(self, product_name: str, sku: str) -> str:
        try:
            return self.instruction.format(name=product_name.lower(), sku=sku)
        except (KeyError, IndexError, ValueError):
            return DEFAULT_INSTRUCTION.format(name=product_name.lower())


@dataclass
class PolicyGraspResult:
    ok: bool
    message: str
    code: VlaErrorCode | None = None
    holding: InstanceId | None = None
    on_target: bool | None = None
    """The attached unit belongs to the target slot (the sim knows; the verifier still decides)."""
    chunks: int = 0
    steps: int = 0
    events: list[str] = field(default_factory=list)
    instruction: str = ""
    model: str = ""
    arm: ArmJointState | None = None
    ended_at: datetime | None = None
    infer_ms: list[float] = field(default_factory=list)
    frames: int = 0
    """Policy-camera frames received (pictures taken while the arm moved + observations)."""
    moving_s: float = 0.0
    """Seconds the policy's chunks took to play (``moving_frames`` / 2 / moving_s = side + wrist pairs per second)."""
    moving_frames: int = 0


ChunkCallback = Callable[[int, CommandResult, Any], Awaitable[None]]


@dataclass
class PolicyContext:
    """What a Cartesian policy needs next to (state, side, wrist): set on the backend (``set_context``) before every
    predict. Frames are the SAME blurred bytes predict gets; ``cameras`` holds their poses (store frame, + roll)."""

    robot_id: RobotId
    instruction: str
    sku: Sku
    product_name: str
    slot: Slot
    lift_height_m: float
    q_deg: list[float]
    tcp: Any = None
    """Point3 (store frame) of the jaw center now."""
    robot_pose: Any = None
    cameras: dict[str, Any] = field(default_factory=dict)
    """'side' / 'wrist' -> ``PolicyFrame`` (camera_pose + roll + w/h; its jpeg is the RAW one, do not send it)."""
    attached: InstanceId | None = None
    gripper_closed: bool = False
    chunk_no: int = 0
    ik: Callable[..., Awaitable[list[Any]]] | None = None
    """``await ik([IkTarget | dict], lift_height_m=None, seed_deg=None) -> [IkSolution]`` (worker-side IK)."""
    events: list[str] = field(default_factory=list)
    budget_key: str = ""
    """Per-trip key for remote policy call budgets (trip id, else goal id, else robot id)."""


class _Stop(Exception):
    def __init__(self, code: VlaErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: VlaErrorCode = code
        self.message = message


def lerp_rows(start: Sequence[float], end: Sequence[float], n: int) -> list[list[float]]:
    n = max(1, n)
    return [[a + (b - a) * (i + 1) / n for a, b in zip(start, end, strict=True)] for i in range(n)]


def frames_of(res: CommandResult) -> dict[str, bytes]:
    """camera -> JPEG bytes of a result's policy frames (the last one per camera)."""
    out: dict[str, bytes] = {}
    for f in res.policy_frames:
        out[f.camera] = base64.b64decode(f.jpeg_b64)
    return out


async def target_lift(robot: Any, robot_id: RobotId, slot: Slot, sku: Sku, instance_id: InstanceId | None) -> float:
    """Lift height for the episode = the target unit's center height (ground truth from the worker), clamped: the
    recorder fixes the lift there too (a level TCP at the unit), so the policy sees the shelf as in its demos."""
    z: float | None = None
    try:
        units = [u for u in await robot.list_instances(robot_id, slot.slot_id) if u.sku == sku and not u.in_basket]
        unit = next((u for u in units if u.instance_id == instance_id), None) if instance_id else None
        if unit is None and units:
            ap = slot.approach_pose
            unit = min(units, key=lambda u: ((u.position.x - ap.x) ** 2 + (u.position.y - ap.y) ** 2, u.instance_id))
        if unit is not None:
            z = unit.position.z
    except Exception as exc:  # noqa: BLE001 - the shelf height is a fine fallback
        log.warning("target_lift: list_instances failed (%s); using the shelf height", exc)
    if z is None:
        z = slot.item_position.z + UNIT_LIFT_FALLBACK_M
    return max(LIFT_MIN_M, min(LIFT_MAX_M, z))


_warned_unblurred = False


async def blur_pair(perception: Any, robot_id: RobotId, side: bytes, wrist: bytes) -> tuple[bytes, bytes]:
    """Faces blurred by perception (CameraFrame round trip) before the frames reach a policy."""
    global _warned_unblurred
    blur = getattr(perception, "blur_faces", None) if perception is not None else None
    if blur is None:
        if not _warned_unblurred:
            _warned_unblurred = True
            log.info("T1: no perception service wired; Isaac policy frames (synthetic, no people) go unblurred")
        return side, wrist
    out: list[bytes] = []
    for cam, data in (("front", side), ("wrist", wrist)):
        frame = CameraFrame(
            # 'sim-' = rendered by the Isaac worker's policy cameras (perception's synthetic-frame convention)
            frame_id=f"sim-t1-{robot_id}-{cam}-{time.monotonic_ns()}",
            camera=cam,  # type: ignore[arg-type]
            robot_id=robot_id,
            jpeg_b64=base64.b64encode(data).decode("ascii"),
            w=640,
            h=480,
        )
        blurred = await blur(frame)
        if not blurred.blurred:  # never send a frame perception did not clear to a policy (CLAUDE.md section 8)
            raise _Stop("policy_unavailable", f"perception did not blur the {cam} policy frame; not sent to the policy")
        out.append(base64.b64decode(blurred.jpeg_b64))
    return out[0], out[1]


async def run_policy_grasp(
    robot: Any,
    robot_id: RobotId,
    *,
    backend: Any,
    slot: Slot,
    sku: Sku,
    product_name: str,
    instance_id: InstanceId | None = None,
    cfg: PolicyGraspConfig | None = None,
    perception: Any = None,
    on_stage: Callable[[str, float], Awaitable[None]] | None = None,
    on_chunk: ChunkCallback | None = None,
    budget_s: float | None = None,
    exec_id: str | None = None,
    budget_key: str | None = None,
) -> PolicyGraspResult:
    """One learned grasp attempt (no retries: the runner's RetryPolicy decides). RobotError (busy / disconnected /
    battery_low ...) propagates; policy failures come back as ``ok=False`` with a VlaErrorCode."""
    cfg = cfg or PolicyGraspConfig()
    t0 = time.monotonic()
    budget = min(cfg.timeout_s, budget_s) if budget_s is not None else cfg.timeout_s
    info = getattr(backend, "info", None)
    model = str(getattr(info, "model_id", "") or getattr(info, "name", "") or type(backend).__name__)
    instruction = cfg.instruction_for(product_name, sku)
    result = PolicyGraspResult(ok=False, message="", instruction=instruction, model=model)
    entered = False
    failed = True

    async def stage(name: str, progress: float) -> None:
        if on_stage is not None:
            await on_stage(name, progress)

    def left() -> float:
        return budget - (time.monotonic() - t0)

    try:
        await stage("policy_reset", 0.12)
        try:
            await asyncio.wait_for(backend.reset(instruction), timeout=max(1.0, left()))
        except TimeoutError as exc:
            raise _Stop("policy_timeout", f"policy reset took longer than {budget:.0f}s") from exc
        except Exception as exc:  # noqa: BLE001 - typed below
            raise _Stop(_policy_code(exc), f"policy reset failed: {exc}") from exc
        lift = await target_lift(robot, robot_id, slot, sku, instance_id)
        await stage("lift_preposition", 0.15)
        home = cfg.home_deg
        res: CommandResult = await robot.arm_joints(
            robot_id, [], lift_height_m=lift, capture=[] if home else ["side", "wrist"], exec_id=exec_id
        )
        entered = True
        _feed(robot, robot_id, res, result)
        if home and res.arm is not None:
            await stage("homing", 0.18)
            rows = lerp_rows(res.arm.q_deg, home, round(HOME_S * 30))
            every = cfg.capture_every(30.0, len(rows))
            res = await robot.arm_joints(
                robot_id, rows, hz=30.0, exec_id=exec_id, **({"capture_every": every} if every else {})
            )
            _feed(robot, robot_id, res, result)
        executed: int | None = None
        while result.chunks < cfg.max_chunks:
            if left() <= 0:
                raise _Stop("policy_timeout", f"no grasp within {budget:.0f}s ({result.chunks} chunks)")
            frames = frames_of(res)
            if "side" not in frames or "wrist" not in frames:
                raise _Stop("internal", "the worker returned no side / wrist frame")
            side, wrist = await blur_pair(perception, robot_id, frames["side"], frames["wrist"])
            state = list(res.arm.q_deg) if res.arm is not None else []
            set_ctx = getattr(backend, "set_context", None)
            if callable(set_ctx):
                ctx = _context(robot, robot_id, res, slot, sku, product_name, instruction, lift, result)
                ctx.budget_key = budget_key or robot_id
                set_ctx(ctx)
            t_inf = time.monotonic()
            try:
                chunk = await asyncio.wait_for(
                    backend.predict(state, side, wrist, frames_blurred=True, executed=executed, delay=0),
                    timeout=max(0.5, left()),
                )
            except TimeoutError as exc:
                raise _Stop("policy_timeout", f"predict took longer than the remaining {left():.1f}s") from exc
            except Exception as exc:  # noqa: BLE001 - typed below
                raise _Stop(_policy_code(exc), f"predict failed: {exc}") from exc
            result.infer_ms.append(round((time.monotonic() - t_inf) * 1000, 1))
            rows = _rows(chunk, cfg.exec_steps)
            start = int(getattr(chunk, "execute_from", 0) or 0)
            executed = start + len(rows)
            hz = cfg.hz or float(getattr(chunk, "action_hz", 30.0) or 30.0)
            every = cfg.capture_every(hz, len(rows))
            t_move = time.monotonic()
            res = await robot.arm_joints(
                robot_id, rows, hz=hz, exec_id=exec_id, **({"capture_every": every} if every else {})
            )
            result.moving_s += time.monotonic() - t_move
            result.moving_frames += len(res.policy_frames)
            _feed(robot, robot_id, res, result)
            result.chunks += 1
            result.steps += len(rows)
            arm = res.arm
            if arm is not None:
                result.events.extend(arm.events)
            if on_chunk is not None:
                await on_chunk(result.chunks, res, chunk)
            if arm is not None and arm.attached and arm.gripper_closed:
                result.ok, result.holding, result.arm = True, arm.attached, arm
                result.on_target = arm.attached.rsplit("#", 1)[0] == slot.slot_id
                where = "" if result.on_target else f" (a unit of {arm.attached.rsplit('#', 1)[0]}, not {slot.slot_id})"
                result.message = f"grasped {sku} ({arm.attached}){where} with {model} in {result.chunks} chunks"
                failed = False
                break
            if getattr(chunk, "done", False):
                raise _Stop(
                    "grasp_missed",
                    f"the policy finished after {result.chunks} chunks but nothing is held"
                    + (f" ({getattr(chunk, 'note', '')})" if getattr(chunk, "note", "") else ""),
                )
        else:
            closed = any(e == "close_empty" for e in result.events)
            raise _Stop(
                "grasp_missed" if closed else "policy_timeout",
                f"no grasp after {result.chunks} chunks ({result.steps} steps)"
                + ("; the jaws closed on air" if closed else ""),
            )
    except _Stop as stop:
        result.ok, result.code, result.message = False, stop.code, stop.message
    finally:
        result.ended_at = utcnow()
        if entered:
            await _leave(robot, robot_id, stop_first=failed, exec_id=exec_id, result=result)
        end_feed = getattr(robot, "end_policy_feed", None)
        if callable(end_feed):
            end_feed(robot_id)
    log.info(
        "T1 %s %s: %s (%s; chunks %d, steps %d, frames %d, %.1f side+wrist pairs/s while the arm moved, infer ms %s)",
        robot_id,
        "ok" if result.ok else (result.code or "failed"),
        result.message,
        instruction,
        result.chunks,
        result.steps,
        result.frames,
        result.moving_frames / 2 / result.moving_s if result.moving_s > 0 else 0.0,
        result.infer_ms[:6],
    )
    return result


def _feed(robot: Any, robot_id: RobotId, res: CommandResult, result: PolicyGraspResult) -> None:
    """The observation (last frame per camera) of a result -> the robot's live view (raw; CameraService blurs it on
    the way out). The pictures taken mid-chunk already went out live (the worker streams them as it renders)."""
    result.frames += len(res.policy_frames)
    feed = getattr(robot, "feed_policy_frames", None)
    last = list({f.camera: f for f in res.policy_frames}.values())
    if callable(feed) and last:
        try:
            feed(robot_id, last)
        except Exception as exc:  # noqa: BLE001 - the live view is best effort
            log.debug("feed_policy_frames failed: %s", exc)


def _context(
    robot: Any,
    robot_id: RobotId,
    res: CommandResult,
    slot: Slot,
    sku: Sku,
    product_name: str,
    instruction: str,
    lift: float,
    result: PolicyGraspResult,
) -> PolicyContext:
    cams: dict[str, Any] = {}
    for f in res.policy_frames:
        cams[f.camera] = f  # the last one per camera = the observation
    arm = res.arm
    ik_fn = getattr(robot, "arm_ik", None)
    probe = getattr(robot, "supports_arm_ik", None)
    has_ik = callable(ik_fn) and (probe is None or bool(probe(robot_id)))

    async def ik(targets: list[Any], *, lift_height_m: float | None = None, seed_deg: Any = None) -> list[Any]:
        out = await ik_fn(robot_id, targets, lift_height_m=lift_height_m, seed_deg=seed_deg)  # type: ignore[misc]
        return list(out.ik)

    return PolicyContext(
        robot_id=robot_id,
        instruction=instruction,
        sku=sku,
        product_name=product_name,
        slot=slot,
        lift_height_m=arm.lift_height_m if arm is not None else lift,
        q_deg=list(arm.q_deg) if arm is not None else [],
        tcp=arm.tcp if arm is not None else None,
        robot_pose=res.state.pose if res.state is not None else None,
        cameras=cams,
        attached=arm.attached if arm is not None else None,
        gripper_closed=bool(arm.gripper_closed) if arm is not None else False,
        chunk_no=result.chunks,
        ik=ik if has_ik else None,
        events=list(result.events),
    )


async def _leave(robot: Any, robot_id: RobotId, *, stop_first: bool, exec_id: str | None, result: Any) -> None:
    """Leave joint mode (best effort, also when cancelled): stop a chunk still playing, then end=True."""
    with contextlib.suppress(Exception):
        if stop_first:
            await asyncio.wait_for(robot.stop(robot_id), timeout=END_TIMEOUT_S)
        res = await asyncio.wait_for(
            robot.arm_joints(robot_id, [], capture=[], end=True, exec_id=exec_id), timeout=END_TIMEOUT_S
        )
        if res.arm is not None and result.arm is None:
            result.arm = res.arm


def _rows(chunk: Any, exec_steps: int) -> list[list[float]]:
    """The rows of a chunk to play now (the service's execution prefix wins; at most ``exec_steps``, or every row up
    to ARM_JOINTS_MAX_STEPS for a chunk marked ``execute_all``: one Cartesian tool call = one whole motion)."""
    limit = ARM_JOINTS_MAX_STEPS if getattr(chunk, "execute_all", False) else exec_steps
    pick = getattr(chunk, "rows_to_execute", None)
    rows = pick(limit) if callable(pick) else list(getattr(chunk, "actions_deg", chunk) or [])
    rows = [list(map(float, r)) for r in rows][: max(1, min(limit, ARM_JOINTS_MAX_STEPS))]
    if not rows or any(len(r) != 6 or not all(math.isfinite(v) for v in r) for r in rows):
        raise _Stop("policy_diverged", "the policy returned an empty or malformed action chunk")
    return rows


def _policy_code(exc: BaseException) -> VlaErrorCode:
    code = getattr(exc, "code", None)
    if code in ("policy_unavailable", "policy_timeout", "policy_diverged", "internal"):
        return code  # type: ignore[return-value]
    if isinstance(exc, RobotError):
        return "internal"
    if isinstance(exc, ValueError):
        return "policy_diverged"
    return "policy_unavailable"


__all__ = [
    "DEFAULT_CAPTURE_HZ",
    "DEFAULT_HOME_DEG",
    "DEFAULT_INSTRUCTION",
    "PolicyContext",
    "PolicyGraspConfig",
    "PolicyGraspResult",
    "blur_pair",
    "frames_of",
    "lerp_rows",
    "parse_home",
    "run_policy_grasp",
    "target_lift",
]
