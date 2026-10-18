"""Perceptron Mk1.5 as the SO-101 grasp policy of the supermarket demo (``--policy perceptron``): the pure parts.

Mirrors Picky's live T1 tier (``arms/robot/vla_mk15.py`` + ``vla_t1.py`` on main): the hosted Perceptron Mk1.5 vision
model sees the arm's cameras and the arm state and answers with ONE tool call per step; CODE validates + clamps the
call and turns it into a Cartesian goal; IK (``robot/so101_ik.py``) turns that into joint rows the rover's arm drives.

    tools        reach_point(camera, x, y) · move_ee(forward_cm, left_cm, up_cm) · set_gripper(open|close) ·
                 lift(up_cm) · done(success, note)                       (the live tool set, worded for this rover)
    images       image 1 = MAST camera (on the rover, looking at the shelf, the arm in view), image 2 = WRIST camera
    pixel -> 3D  the camera's world pose + hfov cast a ray through the 0-1000 point; its depth is the product's aisle
                 face from the STORE MAP (this scene's planogram: product boxes from the USD): vision gives
                 left/right/up/down, the map gives how far the shelf is. Never the sim's live body pose.
    close        "sticky grasp" (the worker's physics substitute in the live system): the jaws close until they touch
                 the product; it is attached only if it really sits between the jaw pads (jaw-contact check).

This module has no Isaac imports (numpy + stdlib), so the Isaac app (``demo/vla_pick.py``), the policy sidecar
(``demo/vla_sidecar.py``, the repo's uv venv: httpx) and the tests (``arms/sim/supermarket/tests``) share it.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

MODEL = "perceptron-mk1.5"
BASE_URL = "https://api.perceptron.inc/v1"
NUDGE_MAX_CM = 6.0
LIFT_MAX_CM = 10.0
Z_BAND = (0.015, 0.40)
"""A reach target is clamped to this height band above the product's shelf board (map), and to <= 85 % of its
height (the jaw pads must close on the body, not on the cap)."""
ALONG_MAX_M = 0.15
"""A reach target is clamped to this distance along the shelf from the slot's centre (the slot's extent)."""
MAX_REACH_FROM_TCP_M = 0.8
HISTORY = 6
CLOSED = "closed the gripper"
MISSED = "closed the gripper on NOTHING (missed)"

SYSTEM_PROMPT = (
    "You control a long-reach robot arm (SO-101, parallel-jaw gripper) on a shopping rover that is parked BESIDE a "
    "supermarket shelf. You see two cameras: image 1 = MAST camera (on the rover, looking at the shelf, the arm is "
    "visible), image 2 = WRIST camera (on the gripper, looking past the two jaws at the shelf). Pick up exactly the "
    "product named in the task. Respond ONLY by calling exactly one tool. Image points use a 0-1000 grid for each "
    "image (x to the right, y down). Typical sequence: reach_point on the product (the middle of its front, the unit "
    "nearest the aisle) -> set_gripper close once the product sits between the open jaws in the wrist image -> "
    "lift -> done. Prefer pointing in the wrist image when the product is visible there (it is closer). Use move_ee "
    "for small corrections."
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
                    "camera": {"type": "string", "enum": ["mast", "wrist"], "description": "mast = image 1"},
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
            "description": "Small gripper correction in centimeters (each within +-6), as seen facing the shelf.",
            "parameters": {
                "type": "object",
                "properties": {
                    "forward_cm": {"type": "number", "description": "into the shelf (+) / out toward the aisle (-)"},
                    "left_cm": {"type": "number", "description": "along the shelf to the left (+) / right (-)"},
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
            "parameters": {"type": "object", "properties": {"up_cm": {"type": "number"}}, "required": ["up_cm"]},
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
NUDGE_RETRY = "You must call one of the tools now."

DETECT_SYSTEM = "Your goal is to segment out the following categories: {categories}"
"""The live verifier's detect template (arms/perception/vision/perceptron.py, perceptron SDK 0.4 ``detect()``)."""
GRIPPER_ROI_WRIST = (300, 350, 700, 900)
"""Where the space between the SO-101 jaws appears in the wrist frame (0-1000 grid; = contracts.grounding
GRIPPER_ROI_WRIST of the live verifier). A product held by this rover's jaws fills it (box ~ (510, 250)-(690, 850))."""
HELD_ROI_MIN_IOU = 0.2
"""gripper_holding: a detected box of the product with IoU > this with the jaw ROI (the live rule). The live 'half the
box inside the ROI' alternative (for a small lime in the jaws) is left out here: this wrist camera sits right behind
the jaws, so far-away units of the same product on the shelf would land inside the ROI too (measured: a 2 x 9 %
box of the shelf unit inside it, held = 18 x 60 %)."""
BASKET_PROMPT = (
    "This camera looks down into a robot's round wire shopping basket. Is {product} inside the basket? Answer yes or "
    "no first, then in at most 12 words where it is."
)
LOOK_PROMPT = (
    "This is the wrist camera of a store robot's arm, aimed at a supermarket shelf. Is {product} visible in this "
    "image? Answer yes or no first, then in at most 12 words where it is."
)


# ---- response parsing (OpenAI-compatible chat completions) --------------------------------------------------------
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


def yes_no(text: str) -> bool | None:
    """'Yes, between the jaws' -> True; 'No ...' -> False; anything else -> None (uncertain)."""
    m = re.match(r"\W*(yes|no)\b", (text or "").strip().lower())
    return None if m is None else m.group(1) == "yes"


_BOX = re.compile(r"<point_box[^>]*>\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)\s*</point_box>")


def parse_boxes(text: str) -> list[tuple[int, int, int, int]]:
    """'<point_box mention=".."> (512,252) (688,850) </point_box>' (alone or inside a <collection>) -> 0-1000 boxes"""
    out = []
    for m in _BOX.finditer(text or ""):
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        out.append((min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)))
    return out


def box_iou(a: Sequence[float], b: Sequence[float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def held_box(boxes: Sequence[Sequence[float]], roi=GRIPPER_ROI_WRIST, min_iou: float = HELD_ROI_MIN_IOU):
    """(held?, best box, its IoU with the jaw ROI) for the wrist frame's detections of the product"""
    best, best_iou = None, 0.0
    for b in boxes:
        iou = box_iou(b, roi)
        if iou > best_iou:
            best, best_iou = tuple(b), iou
    return best_iou > min_iou, best, round(best_iou, 3)


def fmt_call(name: str, args: dict[str, Any]) -> str:
    inner = ", ".join(f"{k}={round(v) if isinstance(v, float) else v}" for k, v in args.items())
    return f"{name}({inner})"


def short_call(name: str, args: dict[str, Any]) -> str:
    """Caption form: 'reach (wrist 512,430)' / 'close' / 'lift 6 cm' / 'nudge +1/-2/+0 cm' / 'done'."""
    if name == "reach_point":
        return f"reach ({args.get('camera', '?')} {_num(args.get('x'), 0):.0f},{_num(args.get('y'), 0):.0f})"
    if name == "set_gripper":
        return "close" if str(args.get("state", "")).lower().startswith("c") else "open"
    if name == "lift":
        return f"lift {_num(args.get('up_cm'), 0):.0f} cm"
    if name == "move_ee":
        f, lf, u = (_num(args.get(k), 0) for k in ("forward_cm", "left_cm", "up_cm"))
        return f"nudge {f:+.0f}/{lf:+.0f}/{u:+.0f} cm"
    return name


def _num(v: Any, default: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ---- geometry -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Cam:
    """A pinhole camera in the store frame: position + its right / up / forward unit axes, horizontal fov, size."""

    origin: tuple[float, float, float]
    right: tuple[float, float, float]
    up: tuple[float, float, float]
    fwd: tuple[float, float, float]
    hfov_deg: float
    width: int
    height: int

    @classmethod
    def from_matrix(cls, T: Any, hfov_deg: float, width: int, height: int) -> Cam:
        """T = 4x4 camera-to-world (column vectors) of a USD camera: looks along -z, +y up, +x right."""
        M = np.asarray(T, float)
        R = M[:3, :3]
        return cls(
            origin=tuple(float(v) for v in M[:3, 3]),
            right=tuple(float(v) for v in R[:, 0]),
            up=tuple(float(v) for v in R[:, 1]),
            fwd=tuple(float(-v) for v in R[:, 2]),
            hfov_deg=float(hfov_deg),
            width=int(width),
            height=int(height),
        )

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("origin", "right", "up", "fwd", "hfov_deg", "width", "height")}


def hfov_deg(focal_mm: float, h_aperture_mm: float) -> float:
    return math.degrees(2.0 * math.atan(h_aperture_mm / (2.0 * focal_mm)))


def pixel_ray(cam: Cam, x1000: float, y1000: float) -> tuple[np.ndarray, np.ndarray]:
    """(origin, unit direction) of the ray through a 0-1000 image point (square pixels)."""
    w, h = float(cam.width), float(cam.height)
    fx = (w / 2) / math.tan(math.radians(cam.hfov_deg) / 2)
    a = (x1000 / 1000.0 * w - w / 2) / fx
    b = (h / 2 - y1000 / 1000.0 * h) / fx
    d = np.asarray(cam.fwd) + a * np.asarray(cam.right) + b * np.asarray(cam.up)
    return np.asarray(cam.origin, float), d / np.linalg.norm(d)


def project(cam: Cam, p: Sequence[float]) -> tuple[float, float] | None:
    """Store point -> 0-1000 image point (None behind the camera); the inverse of ``pixel_ray``."""
    v = np.asarray(p, float) - np.asarray(cam.origin)
    xc, yc, zc = float(v @ np.asarray(cam.right)), float(v @ np.asarray(cam.up)), float(v @ np.asarray(cam.fwd))
    if zc <= 1e-6:
        return None
    w, h = float(cam.width), float(cam.height)
    fx = (w / 2) / math.tan(math.radians(cam.hfov_deg) / 2)
    return (w / 2 + fx * xc / zc) / w * 1000.0, (h / 2 - fx * yc / zc) / h * 1000.0


def ray_plane(origin, d, point, normal, max_t: float = 4.0) -> np.ndarray | None:
    denom = float(np.dot(d, normal))
    if abs(denom) < 1e-3:
        return None
    t = float(np.dot(np.asarray(point) - origin, normal)) / denom
    if t <= 0.0 or t > max_t:
        return None
    return np.asarray(origin) + t * np.asarray(d)


@dataclass(frozen=True)
class Slot:
    """The target product's slot from the store map (the scene's planogram): its aisle face and the shelf board."""

    face: tuple[float, float, float]
    """centre of the product's aisle-side face"""
    normal: tuple[float, float, float]
    """horizontal unit normal out of the shelf (towards the aisle)"""
    board_z: float
    height: float
    width: float

    @property
    def along(self) -> np.ndarray:
        """Unit vector along the shelf, to the LEFT as seen facing the shelf."""
        n = np.asarray(self.normal, float)
        return np.array([n[1], -n[0], 0.0])

    def to_json(self) -> dict:
        return {k: getattr(self, k) for k in ("face", "normal", "board_z", "height", "width")}


@dataclass
class Action:
    """A validated tool call. kind: reach (point = jaw target on the product face) | nudge / lift (delta, world) |
    gripper (close) | done (success) | noop (ignored, note says why)."""

    kind: str
    point: np.ndarray | None = None
    delta: np.ndarray | None = None
    close: bool | None = None
    success: bool | None = None
    note: str = ""
    clipped: list[str] = field(default_factory=list)


def map_action(
    name: str, args: dict[str, Any], cams: dict[str, Cam], slot: Slot, tcp: Sequence[float] | None
) -> Action:
    """One Mk1.5 tool call -> an Action (validated + clamped like arms/robot/vla_mk15.Mk15Policy._execute)."""
    if name == "done":
        ok = bool(args.get("success", False))
        note = str(args.get("note") or "")[:80]
        return Action("done", success=ok, note=f"done ({'success' if ok else 'gave up'}{': ' + note if note else ''})")
    if name == "set_gripper":
        close = str(args.get("state", "")).lower().startswith("c")
        return Action("gripper", close=close, note="close" if close else "open")
    if name == "lift":
        up = _clamp(_num(args.get("up_cm"), 0.0), 0.0, LIFT_MAX_CM)
        return Action("lift", delta=np.array([0.0, 0.0, up / 100.0]), note=f"lift {up:.0f} cm")
    if name == "move_ee":
        f = _clamp(_num(args.get("forward_cm"), 0.0), -NUDGE_MAX_CM, NUDGE_MAX_CM)
        lf = _clamp(_num(args.get("left_cm"), 0.0), -NUDGE_MAX_CM, NUDGE_MAX_CM)
        u = _clamp(_num(args.get("up_cm"), 0.0), -NUDGE_MAX_CM, NUDGE_MAX_CM)
        n = np.asarray(slot.normal, float)
        d = (-n * f + slot.along * lf + np.array([0.0, 0.0, u])) / 100.0
        return Action("nudge", delta=d, note=f"nudge {f:+.0f}/{lf:+.0f}/{u:+.0f} cm")
    if name == "reach_point":
        cam = "wrist" if str(args.get("camera", "mast")).lower().startswith("w") else "mast"
        x = _clamp(_num(args.get("x"), 500.0), 0.0, 1000.0)
        y = _clamp(_num(args.get("y"), 500.0), 0.0, 1000.0)
        c = cams.get(cam)
        if c is None:
            return Action("noop", note=f"ignored (no {cam} camera)")
        o, d = pixel_ray(c, x, y)
        hit = ray_plane(o, d, np.asarray(slot.face, float), np.asarray(slot.normal, float))
        if hit is None:
            return Action("noop", note=f"ignored: ({x:.0f}, {y:.0f}) on the {cam} image does not hit the shelf")
        clipped = []
        z_lo = slot.board_z + Z_BAND[0]
        z_hi = slot.board_z + min(Z_BAND[1], 0.85 * slot.height)
        z = _clamp(float(hit[2]), z_lo, max(z_lo, z_hi))
        if abs(z - hit[2]) > 0.005:
            clipped.append(f"height {100 * (hit[2] - slot.board_z):.0f} -> {100 * (z - slot.board_z):.0f} cm")
        face = np.asarray(slot.face, float)
        s = float((hit - face) @ slot.along)
        s2 = _clamp(s, -ALONG_MAX_M, ALONG_MAX_M)
        if abs(s2 - s) > 0.005:
            clipped.append(f"along {100 * s:.0f} -> {100 * s2:.0f} cm")
        p = face + slot.along * s2
        p[2] = z
        if tcp is not None and float(np.linalg.norm(p - np.asarray(tcp, float))) > MAX_REACH_FROM_TCP_M:
            return Action("noop", note=f"ignored: that point is {np.linalg.norm(p - np.asarray(tcp)):.2f} m away")
        return Action("reach", point=p, note=f"reach {cam} ({x:.0f}, {y:.0f})", clipped=clipped)
    return Action("noop", note=f"unknown tool {name!r} ignored")


def grasp_targets(
    point_w: Sequence[float],
    normal_w: Sequence[float],
    T_base: Any,
    R_b: Any,
    a_b: Sequence[float],
    x_mid: float,
    shift_in: float,
    far_d: float,
) -> tuple[np.ndarray, np.ndarray]:
    """A reach Action's point (on the product's aisle face, world) -> (grasp TCP, pre-grasp TCP), robot base frame.

    The jaw centre goes ``shift_in`` behind the face (the jaw pads are ~4 cm long), the TCP sits ``x_mid`` along the
    tool x axis from the jaw centre (the middle between the open jaws), and the pre-grasp is ``far_d`` back along the
    approach ``a_b``: IK then solves both and the arm slides in on a straight line between them."""
    p = np.asarray(point_w, float) - np.asarray(normal_w, float) * shift_in
    pb = (np.linalg.inv(np.asarray(T_base, float)) @ np.r_[p, 1.0])[:3]
    tcp = pb - np.asarray(R_b, float)[:, 0] * x_mid
    return tcp, tcp - np.asarray(a_b, float) * far_d


# ---- the per-attempt session (history + the live guards) -------------------------------------------------------------
class Session:
    """History of one grasp attempt as Mk1.5 sees it (``Steps so far``) + the guards the live policy learned on Isaac:
    no second close at a spot that just missed (re-aim first); closing an already-closed empty gripper opens it."""

    def __init__(self, instruction: str) -> None:
        self.instruction = instruction
        self.history: list[str] = []

    def record(self, name: str, args: dict[str, Any], outcome: str) -> None:
        self.history.append(f"{fmt_call(name, args)} -> {outcome}")

    def resolve_close(self, held: bool) -> None:
        """After a close: mark it MISSED when nothing is held (the outcome is known one step later)."""
        if self.history and self.history[-1].endswith(CLOSED) and not held:
            self.history[-1] = self.history[-1][: -len(CLOSED)] + MISSED

    def missed_here(self) -> bool:
        for h in reversed(self.history):
            if h.endswith(MISSED):
                return True
            if h.startswith(("reach_point", "move_ee", "lift")):
                return False
        return False

    def guard(self, action: Action, gripper_open: bool, held: bool) -> Action:
        """The live policy's guards for set_gripper (arms/robot/vla_mk15.Mk15Policy._execute)."""
        if action.kind != "gripper" or not action.close:
            return action
        if gripper_open and self.missed_here():
            return Action("noop", note="refused: the close at this spot missed; reach_point at the product first")
        if not gripper_open and not held:
            return Action("gripper", close=False, note="gripper was already closed on nothing: opened it again")
        return action

    def state_text(self, gripper_open: bool, held: bool, tcp: Sequence[float] | None, slot: Slot) -> str:
        parts = [f"gripper {'open' if gripper_open else 'closed'}"]
        if tcp is not None:
            t = np.asarray(tcp, float)
            face = np.asarray(slot.face, float)
            ahead = float((t - face) @ np.asarray(slot.normal, float))
            above = (float(t[2]) - slot.board_z) * 100
            parts.append(
                f"jaw center {ahead * 100:.0f} cm in front of the product's shelf face, {above:.0f} cm above the board"
            )
        if held:
            parts.append("something is held in the gripper")
        elif not gripper_open and any(h.startswith("set_gripper(state=close)") for h in self.history[-2:]):
            parts.append("the gripper closed on NOTHING (missed): open it and reach_point at the product again")
        elif self.missed_here():
            parts.append(
                "the last close at this spot MISSED: call reach_point on the middle of a product unit (pick a "
                "different point) before closing again"
            )
        return "; ".join(parts)

    def user_text(self, state: str) -> str:
        steps = "\n".join(f"- {h}" for h in self.history[-HISTORY:]) or "- (none yet)"
        return (
            f"Task: {self.instruction}.\nArm: {state}.\nSteps so far:\n{steps}\n"
            "Image 1 = mast camera, image 2 = wrist camera. Call exactly one tool."
        )


def messages(user_text: str, images_b64: Sequence[str]) -> list[dict[str, Any]]:
    """Chat messages for one policy step (the sidecar adds the nudge on a text answer)."""
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for b in images_b64:
        content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b}})
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]


__all__ = [
    "BASKET_PROMPT",
    "CLOSED",
    "DETECT_SYSTEM",
    "GRIPPER_ROI_WRIST",
    "LOOK_PROMPT",
    "MISSED",
    "MODEL",
    "SYSTEM_PROMPT",
    "TOOLS",
    "Action",
    "Cam",
    "Session",
    "Slot",
    "answer_text",
    "box_iou",
    "first_tool_call",
    "fmt_call",
    "held_box",
    "parse_boxes",
    "grasp_targets",
    "hfov_deg",
    "map_action",
    "messages",
    "pixel_ray",
    "project",
    "ray_plane",
    "short_call",
    "yes_no",
]
