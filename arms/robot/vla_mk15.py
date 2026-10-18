"""T1 "Perceptron Mk1.5 policy" (``PICKY_VLA_T1=perceptron``): the hosted Perceptron Mk1.5 vision model drives the
SO-101 through a few Cartesian tools, and CODE turns every tool call into joint rows. A ``PolicyBackend`` (the same
interface as ``arms.robot.policy_backends``), so ``arms.robot.vla_t1.run_policy_grasp`` runs it like SmolVLA.

    predict(state, side, wrist)   one Mk1.5 call: instruction + arm state + the last steps + side and wrist JPEGs
        Mk1.5 -> ONE tool call    reach_point(camera, x, y) · move_ee(forward_cm, left_cm, up_cm) ·
                                  set_gripper(open|close) · lift(up_cm) · done(success, note)
        code validates + clamps   coords 0-1000, nudges <= 6 cm, targets inside the arm's workspace
        pixel -> 3D               the camera's pose + roll + hfov (PolicyFrame of THAT frame) cast a ray; its depth is
                                  the shelf face of the slot from the PLANOGRAM (a vertical plane through
                                  slot.item_position, GRASP_DEPTH_M behind it): vision gives left/right/up/down, the
                                  planogram gives how far the shelf is. Never the sim's ground-truth unit position.
        3D -> joints              worker-side IK on the real SO-101 model (contracts 0.2.2 ``ArmIkCmd``)
        joints -> chunk           rows interpolated in joint space at 30 Hz (<= MAX_DEG_PER_ROW per joint per row)

reach_point = open jaws to a standoff STANDOFF_M in front of the point, then straight in; set_gripper close = the
sticky grasp attaches a unit when the jaws close within ~3 cm of it (worker physics substitute). The executor stops
as soon as a unit is held; ``done`` without a held unit ends the attempt (grasp_missed). The result is a CLAIM.

Every Mk1.5 call counts against a per-trip budget (``PICKY_PERCEPTRON_CALL_BUDGET``, shared with the navigation
look-out ``look``); a spent budget / missing key / auth or rate-limit error is ``policy_unavailable`` (the runner
then downgrades to T0). Frames reach Perceptron only blurred (``frames_blurred=True`` is required, like every
backend). HTTP: httpx with explicit headers against the OpenAI-compatible ``PERCEPTRON_BASE_URL`` (urllib's default
user agent is refused by their CDN); ``tool_choice`` must stay 'auto' ('required' is rejected).
"""

from __future__ import annotations

import base64
import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from arms.robot.perceptron_vla import resolve_setting
from arms.robot.policy_backends import ActionChunk, BackendInfo, PolicyBackendError, check_rows
from contracts.common import RobotId
from contracts.sim import RobotBackend
from contracts.vla import VlaCapabilities
from utils.config import get_settings
from utils.log import get_logger

log = get_logger(__name__)

NAME = "perceptron_mk15"
DEFAULT_MODEL = "perceptron-mk1.5"
DEFAULT_BASE_URL = "https://api.perceptron.inc/v1"
KEY_ENV = "PERCEPTRON_API_KEY"
CALL_TIMEOUT_S = 12.0
DEFAULT_CALL_BUDGET = 80
"""Perceptron calls per trip (policy steps + navigation looks)."""
BUDGET_TTL_S = 6 * 3600.0
HZ = 30.0
MAX_ROWS = 50
MAX_DEG_PER_ROW = 2.5
"""75 deg/s per joint at 30 Hz; a move needing more rows than MAX_ROWS is played faster (kinematic arm)."""
MIN_SEGMENT_ROWS = 6
GRIPPER_OPEN_DEG = 40.0
GRIPPER_CLOSED_DEG = -5.0
GRIPPER_ROWS = 12
SIDE_GRASP_ROLL_DEG = 90.0
"""Wrist roll for a side grasp (jaws close across the unit's width), as the scripted rig and the recorded demos."""
STANDOFF_M = 0.07
GRASP_DEPTH_M = 0.03
"""The grasp point sits this far behind the shelf face (inside the front unit's box)."""
NUDGE_MAX_CM = 6.0
LIFT_MAX_CM = 10.0
MAX_REACH_FROM_TCP_M = 0.6
Z_ABOVE_BOARD_M = (0.015, 0.40)
"""A grasp target is clamped to this height band above the slot's shelf board (planogram)."""
HISTORY = 6

SYSTEM_PROMPT = (
    "You control a small robot arm (SO-101, parallel-jaw gripper) on a store robot that is parked facing a shelf. "
    "You see two cameras: image 1 = SIDE camera (behind the arm, looking at the shelf, the gripper is visible), "
    "image 2 = WRIST camera (on the gripper, looking past the two jaws). Pick up exactly the product named in the "
    "task. Respond ONLY by calling exactly one tool. Image points use a 0-1000 grid for each image (x to the right, "
    "y down). Typical sequence: reach_point on the product (its middle, the unit in front) -> set_gripper close once "
    "the product sits between the open jaws in the wrist image -> done. Use move_ee for small corrections."
)
TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "reach_point",
            "description": "Move the OPEN gripper to the product at this point of one image (0-1000 grid).",
            "parameters": {
                "type": "object",
                "properties": {
                    "camera": {"type": "string", "enum": ["side", "wrist"], "description": "side = image 1"},
                    "x": {"type": "number", "description": "0-1000, left to right"},
                    "y": {"type": "number", "description": "0-1000, top to bottom"},
                },
                "required": ["camera", "x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_ee",
            "description": "Small gripper correction in centimeters relative to the robot (each within +-6).",
            "parameters": {
                "type": "object",
                "properties": {
                    "forward_cm": {"type": "number", "description": "toward the shelf (+) / away (-)"},
                    "left_cm": {"type": "number", "description": "to the robot's left (+) / right (-)"},
                    "up_cm": {"type": "number", "description": "up (+) / down (-)"},
                },
                "required": ["forward_cm", "left_cm", "up_cm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_gripper",
            "description": "Open or close the gripper (close = grasp what is between the jaws).",
            "parameters": {
                "type": "object",
                "properties": {"state": {"type": "string", "enum": ["open", "close"]}},
                "required": ["state"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lift",
            "description": "Raise the gripper (and what it holds) by up_cm centimeters (0-10).",
            "parameters": {
                "type": "object",
                "properties": {"up_cm": {"type": "number"}},
                "required": ["up_cm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Finish: the product is held (success=true) or cannot be picked (success=false).",
            "parameters": {
                "type": "object",
                "properties": {"success": {"type": "boolean"}, "note": {"type": "string"}},
                "required": ["success"],
            },
        },
    },
]


def _setting(name: str, default: Any) -> Any:
    try:
        value = getattr(get_settings(), name, None)
    except Exception:  # noqa: BLE001
        return default
    return default if value is None or value == "" else value


def api_key() -> str:
    return resolve_setting(KEY_ENV, secret=True)[0]


# ---- per-trip call budget -------------------------------------------------------------------------------------------
class CallBudget:
    """Perceptron calls per key (trip id, else goal / robot). ``take`` is False once ``limit`` calls were made."""

    def __init__(self, limit: int | None = None) -> None:
        self._limit = limit
        self._used: dict[str, tuple[int, float]] = {}

    @property
    def limit(self) -> int:
        if self._limit is not None:
            return self._limit
        try:
            return max(0, int(_setting("picky_perceptron_call_budget", DEFAULT_CALL_BUDGET)))
        except (TypeError, ValueError):
            return DEFAULT_CALL_BUDGET

    def used(self, key: str) -> int:
        return self._used.get(key, (0, 0.0))[0]

    def take(self, key: str) -> bool:
        now = time.monotonic()
        for k in [k for k, (_, t) in self._used.items() if now - t > BUDGET_TTL_S]:
            del self._used[k]
        n = self.used(key)
        if n >= self.limit:
            return False
        self._used[key] = (n + 1, now)
        return True


BUDGET = CallBudget()


# ---- HTTP client ----------------------------------------------------------------------------------------------------
class Mk15Client:
    """Minimal async OpenAI-compatible chat client for Perceptron's hosted Mk1.5 (one per backend / event loop)."""

    def __init__(
        self,
        *,
        key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.key = key if key is not None else api_key()
        self.base_url = (base_url or str(_setting("perceptron_base_url", DEFAULT_BASE_URL))).rstrip("/")
        self.model = model or str(_setting("perceptron_model", DEFAULT_MODEL))
        self.timeout_s = timeout_s or float(_setting("picky_vla_mk15_timeout_s", CALL_TIMEOUT_S))
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self.calls = 0

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                transport=self._transport,
                timeout=self.timeout_s,
                headers={"User-Agent": "picky-robot/0.2 (+httpx)", "Accept": "application/json"},
            )
        return self._http

    async def chat(self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None) -> dict:
        if not self.key:
            raise PolicyBackendError("policy_unavailable", f"{KEY_ENV} not set")
        body: dict[str, Any] = {"model": self.model, "messages": messages, "max_completion_tokens": 300}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        self.calls += 1
        try:
            r = await self._client().post(
                f"{self.base_url}/chat/completions", json=body, headers={"Authorization": f"Bearer {self.key}"}
            )
        except httpx.TimeoutException as exc:
            raise PolicyBackendError(
                "policy_timeout", f"Perceptron Mk1.5 timed out after {self.timeout_s:.0f}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise PolicyBackendError("policy_unavailable", f"Perceptron Mk1.5 unreachable: {exc}") from exc
        if r.status_code in (401, 403):
            raise PolicyBackendError("policy_unavailable", f"Perceptron Mk1.5 refused the key (HTTP {r.status_code})")
        if r.status_code == 429:
            raise PolicyBackendError("policy_unavailable", "Perceptron Mk1.5 rate limit / quota (HTTP 429)")
        if r.status_code >= 400:
            raise PolicyBackendError("policy_unavailable", f"Perceptron Mk1.5 HTTP {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as exc:
            raise PolicyBackendError("policy_diverged", "Perceptron Mk1.5 answered non-JSON") from exc

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None


def image_part(jpeg: bytes) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}}


def first_tool_call(resp: dict) -> tuple[str, dict[str, Any]] | None:
    """(name, arguments) of the first tool call of a chat completion, None when it answered in text."""
    try:
        msg = resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return None
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        name = fn.get("name")
        raw = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw) if isinstance(raw, str) else dict(raw)
        except (ValueError, TypeError):
            args = {}
        if name:
            return str(name), args if isinstance(args, dict) else {}
    return None


def answer_text(resp: dict) -> str:
    try:
        return str(resp["choices"][0]["message"].get("content") or "").strip()
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""


# ---- geometry (pure) ------------------------------------------------------------------------------------------------
Vec3 = tuple[float, float, float]


def camera_basis(yaw: float, pitch: float, roll: float = 0.0) -> tuple[Vec3, Vec3, Vec3]:
    """(right, up, forward) of a pinhole camera in the store frame (= arms/sim/geometry.CameraPose.basis)."""
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    f = (cy * cp, sy * cp, sp)
    r = (sy, -cy, 0.0)
    u = (r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0])
    if roll:
        cr, sr = math.cos(roll), math.sin(roll)
        r, u = (
            (r[0] * cr + u[0] * sr, r[1] * cr + u[1] * sr, r[2] * cr + u[2] * sr),
            (u[0] * cr - r[0] * sr, u[1] * cr - r[1] * sr, u[2] * cr - r[2] * sr),
        )
    return r, u, f


def pixel_ray(mount: Any, roll: float, x1000: float, y1000: float) -> tuple[Vec3, Vec3]:
    """(origin, unit direction) in the store frame of the ray through a 0-1000 image point of a camera
    (``CameraMount`` frame='world': x, y, z, yaw, pitch, hfov_deg, width, height)."""
    w, h = float(mount.width), float(mount.height)
    fx = (w / 2) / math.tan(math.radians(mount.hfov_deg) / 2)
    px, py = x1000 / 1000.0 * w, y1000 / 1000.0 * h
    a, b = (px - w / 2) / fx, (h / 2 - py) / fx
    r, u, f = camera_basis(mount.yaw, mount.pitch, roll)
    d = (f[0] + a * r[0] + b * u[0], f[1] + a * r[1] + b * u[1], f[2] + a * r[2] + b * u[2])
    n = math.sqrt(sum(c * c for c in d))
    return (float(mount.x), float(mount.y), float(mount.z)), (d[0] / n, d[1] / n, d[2] / n)


def project(mount: Any, roll: float, p: Sequence[float]) -> tuple[float, float] | None:
    """Store point -> 0-1000 image point (None behind the camera). Inverse of ``pixel_ray`` (tests, prompts)."""
    r, u, f = camera_basis(mount.yaw, mount.pitch, roll)
    v = (p[0] - mount.x, p[1] - mount.y, p[2] - mount.z)
    xc, yc, zc = (
        sum(v[i] * r[i] for i in range(3)),
        sum(v[i] * u[i] for i in range(3)),
        sum(v[i] * f[i] for i in range(3)),
    )
    if zc <= 1e-6:
        return None
    w, h = float(mount.width), float(mount.height)
    fx = (w / 2) / math.tan(math.radians(mount.hfov_deg) / 2)
    return (w / 2 + fx * xc / zc) / w * 1000.0, (h / 2 - fx * yc / zc) / h * 1000.0


def shelf_plane(slot: Any, depth_m: float = GRASP_DEPTH_M) -> tuple[Vec3, Vec3]:
    """(point, normal) of the vertical grasp plane of a slot from the PLANOGRAM: the shelf face through
    ``slot.item_position`` pushed ``depth_m`` into the shelf along the approach heading (normal = that heading)."""
    th = slot.approach_pose.theta
    n = (math.cos(th), math.sin(th), 0.0)
    p = slot.item_position
    return (p.x + depth_m * n[0], p.y + depth_m * n[1], p.z), n


def ray_plane(origin: Vec3, d: Vec3, point: Vec3, normal: Vec3) -> Vec3 | None:
    denom = sum(d[i] * normal[i] for i in range(3))
    if abs(denom) < 1e-3:
        return None
    t = sum((point[i] - origin[i]) * normal[i] for i in range(3)) / denom
    if t <= 0.0 or t > 3.0:
        return None
    return (origin[0] + t * d[0], origin[1] + t * d[1], origin[2] + t * d[2])


def joint_rows(
    start: Sequence[float], waypoints: Sequence[Sequence[float]], max_rows: int = MAX_ROWS
) -> list[list[float]]:
    """Joint-space rows from ``start`` through ``waypoints`` (each segment >= MIN_SEGMENT_ROWS rows at <=
    MAX_DEG_PER_ROW per joint), compressed to ``max_rows`` total (faster) when needed."""
    segs: list[tuple[list[float], list[float], int]] = []
    prev = [float(v) for v in start]
    for wp in waypoints:
        wp = [float(v) for v in wp]
        delta = max(abs(a - b) for a, b in zip(prev, wp, strict=True))
        segs.append((prev, wp, max(MIN_SEGMENT_ROWS, math.ceil(delta / MAX_DEG_PER_ROW))))
        prev = wp
    total = sum(n for _, _, n in segs)
    if total > max_rows:
        scale = max_rows / total
        segs = [(a, b, max(2, int(n * scale))) for a, b, n in segs]
        while sum(n for _, _, n in segs) > max_rows:
            i = max(range(len(segs)), key=lambda k: segs[k][2])
            a, b, n = segs[i]
            segs[i] = (a, b, n - 1)
    rows: list[list[float]] = []
    for a, b, n in segs:
        for i in range(n):
            t = (i + 1) / n
            t = t * t * (3 - 2 * t)  # smoothstep: no velocity jump at the waypoints
            rows.append([x + (y - x) * t for x, y in zip(a, b, strict=True)])
    return rows


# ---- the policy -----------------------------------------------------------------------------------------------------
@dataclass
class Mk15Chunk(ActionChunk):
    """An ActionChunk plus the tool call that produced it. ``execute_all``: play every row (one tool call = one
    motion; the executor otherwise caps a chunk at PICKY_VLA_T1_EXEC_STEPS)."""

    tool: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    done: bool = False
    note: str = ""
    execute_all: bool = True


class Mk15Policy:
    """``PolicyBackend`` driven by Perceptron Mk1.5 tool calls (see module docstring). Needs ``set_context`` (the
    T1 executor sets it before every predict) for camera poses, the slot and worker-side IK."""

    def __init__(self, *, client: Mk15Client | None = None, budget: CallBudget | None = None) -> None:
        self.client = client or Mk15Client()
        self.budget = budget or BUDGET
        self.instruction = ""
        self.ctx: Any = None
        self.history: list[str] = []
        self.sequence = 0
        self.calls = 0
        self.last_tool: tuple[str, dict[str, Any]] | None = None

    @property
    def info(self) -> BackendInfo:
        return BackendInfo(
            name=NAME,  # type: ignore[arg-type]
            model_id=self.client.model,
            action_hz=HZ,
            chunk_size=None,
            supports_rtc=False,
            host="api.perceptron.inc",
            default_execute=MAX_ROWS,
        )

    async def reset(self, instruction: str) -> None:
        if not self.client.key:
            raise PolicyBackendError("policy_unavailable", f"{KEY_ENV} not set (Perceptron Mk1.5)")
        self.instruction = instruction
        self.history = []
        self.sequence = 0
        self.last_tool = None

    def set_context(self, ctx: Any) -> None:
        self.ctx = ctx

    # ---- prompt ----------------------------------------------------------------------------------------------------
    def _state_text(self, state: Sequence[float]) -> str:
        ctx = self.ctx
        grip = "closed" if state[5] < 10.0 else "open"
        parts = [f"gripper {grip}"]
        if ctx is not None and ctx.tcp is not None:
            (px, py, pz), n = shelf_plane(ctx.slot, 0.0)
            t = ctx.tcp
            ahead = (px - t.x) * n[0] + (py - t.y) * n[1]
            above = (t.z - pz) * 100
            parts.append(f"jaw center {ahead * 100:.0f} cm in front of the shelf face, {above:.0f} cm above the board")
        if ctx is not None and ctx.attached:
            parts.append("something is held in the gripper")
        return "; ".join(parts)

    def _messages(self, state: Sequence[float], side: bytes, wrist: bytes) -> list[dict[str, Any]]:
        steps = "\n".join(f"- {h}" for h in self.history[-HISTORY:]) or "- (none yet)"
        text = (
            f"Task: {self.instruction}.\nArm: {self._state_text(state)}.\nSteps so far:\n{steps}\n"
            "Image 1 = side camera, image 2 = wrist camera. Call exactly one tool."
        )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [{"type": "text", "text": text}, image_part(side), image_part(wrist)]},
        ]

    # ---- predict ---------------------------------------------------------------------------------------------------
    async def predict(
        self,
        state_deg: Sequence[float],
        side_jpeg: bytes,
        wrist_jpeg: bytes,
        *,
        frames_blurred: bool,
        executed: int | None = None,
        delay: int = 0,
    ) -> ActionChunk:
        if not frames_blurred:
            raise ValueError("frames must be face-blurred before they reach Perceptron (CLAUDE.md section 8)")
        if not side_jpeg or not wrist_jpeg:
            raise ValueError("both the side and the wrist image are required")
        state = [float(v) for v in state_deg]
        if len(state) != 6:
            raise ValueError(f"state needs 6 joint values, got {len(state)}")
        ctx = self.ctx
        if ctx is None or ctx.ik is None:
            raise PolicyBackendError(
                "policy_unavailable", "Mk1.5 policy needs worker-side IK (Isaac worker with arm_ik)"
            )
        key = getattr(ctx, "budget_key", None) or ctx.robot_id
        t0 = time.monotonic()
        call = None
        for attempt in range(2):
            if not self.budget.take(key):
                raise PolicyBackendError(
                    "policy_unavailable", f"Perceptron call budget spent ({self.budget.limit} per trip)"
                )
            msgs = self._messages(state, side_jpeg, wrist_jpeg)
            if attempt:
                msgs.append({"role": "user", "content": "You must call one of the tools now."})
            resp = await self.client.chat(msgs, tools=TOOLS)
            self.calls += 1
            call = first_tool_call(resp)
            if call is not None:
                break
            log.info("Mk1.5 answered without a tool call: %r", answer_text(resp)[:160])
        if call is None:
            raise PolicyBackendError("policy_diverged", "Perceptron Mk1.5 called no tool twice")
        name, args = call
        self.last_tool = call
        rows, outcome, done = await self._execute(name, args, state)
        self.history.append(f"{_fmt_call(name, args)} -> {outcome}")
        rows_deg, clipped = check_rows(rows)
        seq = self.sequence
        self.sequence += 1
        log.info("Mk1.5 step %d: %s -> %s (%d rows)", seq, _fmt_call(name, args), outcome, len(rows_deg))
        return Mk15Chunk(
            actions_deg=rows_deg,
            action_hz=HZ,
            backend=NAME,  # type: ignore[arg-type]
            model=self.client.model,
            chunk_id=f"mk15-{seq}",
            sequence=seq,
            execute_count=len(rows_deg),
            latency_ms=round((time.monotonic() - t0) * 1000, 1),
            clipped=clipped,
            policy_actions=rows,
            tool=name,
            args=args,
            done=done,
            note=outcome,
        )

    async def _execute(
        self, name: str, args: dict[str, Any], state: list[float]
    ) -> tuple[list[list[float]], str, bool]:
        """One validated tool call -> (rows, outcome text for the history, done)."""
        ctx = self.ctx
        hold = [list(state)]
        if name == "done":
            ok = bool(args.get("success", False))
            note = f": {str(args.get('note'))[:80]}" if args.get("note") else ""
            return hold, f"done ({'success' if ok else 'gave up'}{note})", True
        if name == "set_gripper":
            close = str(args.get("state", "")).lower().startswith("c")
            target = GRIPPER_CLOSED_DEG if close else GRIPPER_OPEN_DEG
            rows = [[*state[:5], state[5] + (target - state[5]) * (i + 1) / GRIPPER_ROWS] for i in range(GRIPPER_ROWS)]
            return rows, "closed the gripper" if close else "opened the gripper", False
        if name in ("move_ee", "lift"):
            if ctx.tcp is None or ctx.robot_pose is None:
                return hold, "ignored (no TCP / pose)", False
            if name == "lift":
                fwd, left, up = 0.0, 0.0, _clamp(_num(args.get("up_cm"), 0.0), 0.0, LIFT_MAX_CM)
            else:
                fwd = _clamp(_num(args.get("forward_cm"), 0.0), -NUDGE_MAX_CM, NUDGE_MAX_CM)
                left = _clamp(_num(args.get("left_cm"), 0.0), -NUDGE_MAX_CM, NUDGE_MAX_CM)
                up = _clamp(_num(args.get("up_cm"), 0.0), -NUDGE_MAX_CM, NUDGE_MAX_CM)
            th = ctx.robot_pose.theta
            c, s = math.cos(th), math.sin(th)
            t = ctx.tcp
            goal = (t.x + (fwd * c - left * s) / 100, t.y + (fwd * s + left * c) / 100, t.z + up / 100)
            sols = await ctx.ik([_ik_target(goal, state[5], None)])
            if not sols:
                return hold, "ignored (IK gave no answer)", False
            sol = sols[0]
            rows = joint_rows(state, [sol.q_deg])
            return rows, f"moved {fwd:+.0f}/{left:+.0f}/{up:+.0f} cm (ik error {sol.error_m * 100:.1f} cm)", False
        if name == "reach_point":
            cam = "wrist" if str(args.get("camera", "side")).lower().startswith("w") else "side"
            x = _clamp(_num(args.get("x"), 500.0), 0.0, 1000.0)
            y = _clamp(_num(args.get("y"), 500.0), 0.0, 1000.0)
            frame = ctx.cameras.get(cam)
            if frame is None or frame.camera_pose is None:
                return hold, f"ignored (no {cam} camera pose)", False
            origin, d = pixel_ray(frame.camera_pose, float(getattr(frame, "roll", 0.0) or 0.0), x, y)
            plane_pt, normal = shelf_plane(ctx.slot)
            hit = ray_plane(origin, d, plane_pt, normal)
            if hit is None:
                return hold, f"ignored: ({x:.0f}, {y:.0f}) on the {cam} image does not hit the shelf", False
            board = ctx.slot.item_position.z
            z = _clamp(hit[2], board + Z_ABOVE_BOARD_M[0], board + Z_ABOVE_BOARD_M[1])
            target = (hit[0], hit[1], z)
            if ctx.tcp is not None and math.dist(target, (ctx.tcp.x, ctx.tcp.y, ctx.tcp.z)) > MAX_REACH_FROM_TCP_M:
                return (
                    hold,
                    f"ignored: that point is {math.dist(target, (ctx.tcp.x, ctx.tcp.y, ctx.tcp.z)):.2f} m away",
                    False,
                )
            standoff = (target[0] - STANDOFF_M * normal[0], target[1] - STANDOFF_M * normal[1], target[2])
            sols = await ctx.ik(
                [
                    _ik_target(standoff, GRIPPER_OPEN_DEG, SIDE_GRASP_ROLL_DEG),
                    _ik_target(target, GRIPPER_OPEN_DEG, SIDE_GRASP_ROLL_DEG),
                ]
            )
            if len(sols) < 2:
                return hold, "ignored (IK gave no answer)", False
            if not sols[1].ok:
                note = f"target out of reach (ik error {sols[1].error_m * 100:.1f} cm)"
                if sols[1].error_m > 0.08:
                    return hold, f"ignored: {note}", False
            else:
                note = f"reached with ik error {sols[1].error_m * 100:.1f} cm"
            rows = joint_rows(state, [sols[0].q_deg, sols[1].q_deg])
            return rows, f"moved the open jaws to the point ({note})", False
        return hold, f"unknown tool {name!r} ignored", False

    async def close(self) -> None:
        await self.client.close()


def _ik_target(p: Sequence[float], gripper_deg: float | None, roll_deg: float | None) -> dict[str, Any]:
    out: dict[str, Any] = {"point": {"x": float(p[0]), "y": float(p[1]), "z": float(p[2])}, "level": True}
    if gripper_deg is not None:
        out["gripper_deg"] = float(gripper_deg)
    if roll_deg is not None:
        out["wrist_roll_deg"] = float(roll_deg)
    return out


def _num(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _fmt_call(name: str, args: dict[str, Any]) -> str:
    inner = ", ".join(f"{k}={round(v) if isinstance(v, float) else v}" for k, v in args.items())
    return f"{name}({inner})"


# ---- navigation look-out --------------------------------------------------------------------------------------------
LOOK_PROMPT = (
    "This is the front camera of a store robot driving to a shelf. Is {target} visible in this image? Answer yes or "
    "no first, then in at most 12 words where it is."
)


async def look(
    jpeg: bytes, target: str, *, budget_key: str, client: Mk15Client | None = None, budget: CallBudget | None = None
) -> str | None:
    """One rate-limited Mk1.5 question on a BLURRED front-camera frame ("is <target> visible?"); None when there is
    no key / budget or the call failed (logged). Callers rate-limit per exec; this enforces the per-trip budget."""
    own = client is None
    client = client or Mk15Client()
    budget = budget or BUDGET
    try:
        if not client.key or not budget.take(budget_key):
            return None
        msgs = [
            {
                "role": "user",
                "content": [{"type": "text", "text": LOOK_PROMPT.format(target=target)}, image_part(jpeg)],
            }
        ]
        return answer_text(await client.chat(msgs))[:200] or None
    except PolicyBackendError as exc:
        log.info("Mk1.5 look-out failed: %s", exc)
        return None
    finally:
        if own:
            await client.close()


# ---- capability -----------------------------------------------------------------------------------------------------
def mk15_capability(robot_id: RobotId, robot_backend: RobotBackend | None) -> VlaCapabilities:
    """T1 row of the Mk1.5 policy: available = PERCEPTRON_API_KEY set (the executor adds the arm_ik / Isaac check)."""
    why = "" if api_key() else f"{KEY_ENV} not set (Perceptron Mk1.5)"
    return VlaCapabilities(
        robot_id=robot_id,
        tier="t1_pretrained",
        available=not why,
        reason=why,
        backend=robot_backend,
        step_kinds=["grasp"],
        model_id=f"{_setting('perceptron_model', DEFAULT_MODEL)} (tool policy)",
        policy_host="api.perceptron.inc",
        action_hz=HZ,
        chunk_size=MAX_ROWS,
        needs_grounding=False,
        cameras=["front", "wrist"],
        input_frames_blurred=True,
        cancel_latency_ms=100.0,
    )


__all__ = [
    "BUDGET",
    "NAME",
    "CallBudget",
    "Mk15Chunk",
    "Mk15Client",
    "Mk15Policy",
    "camera_basis",
    "first_tool_call",
    "joint_rows",
    "look",
    "mk15_capability",
    "pixel_ray",
    "project",
    "ray_plane",
    "shelf_plane",
]
