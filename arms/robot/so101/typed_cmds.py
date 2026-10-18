"""Typed terminal commands for step_ctl (kept free of camera/robot imports so it is unit-testable)."""

from __future__ import annotations

TYPED_HELP = """\
  up/down/forward/back/left/right CM   move the gripper tip (robot frame: forward = away from the base,
                                       left = robot's left, i.e. the viewer's right when facing the robot)
  xyz X Y Z [ROLL]                     tip to x,y,z in metres, pointing down          e.g. xyz 0.22 -0.10 0.06
  roll DEG                             wrist roll (jaws always close left-right in the wrist-camera image)
  open | close | grip N                gripper (0 closed .. 100 open; close stalls on what it holds)
  pick X Y ZTOP [ROLL]                 hover, straddle an object whose top is at ZTOP (m), close, lift
  place                                lower 2.5 cm, open, lift away
  j JOINT DEG                          nudge one joint, e.g. j elbow_flex -5
  status | find OBJECT | home | help | quit"""

MOVES = {
    "forward": (1, 0, 0),
    "back": (-1, 0, 0),
    "left": (0, 1, 0),
    "right": (0, -1, 0),
    "up": (0, 0, 1),
    "down": (0, 0, -1),
}


def parse_typed(line: str, tip_now: list[float], roll_now: float) -> list[dict]:
    """One typed line -> step_ctl commands (macros expand to several). Raises ValueError on bad input."""
    w = line.strip().split()
    if not w:
        return []
    op, a = w[0].lower(), w[1:]
    if op in MOVES:
        d = float(a[0]) / 100 if a else 0.01
        ux, uy, uz = MOVES[op]
        return [{"xyz": [tip_now[0] + ux * d, tip_now[1] + uy * d, tip_now[2] + uz * d], "roll": roll_now}]
    if op == "xyz":
        roll = float(a[3]) if len(a) > 3 else roll_now
        return [{"xyz": [float(a[0]), float(a[1]), float(a[2])], "roll": roll, "max_jump": 100}]
    if op == "roll":
        return [{"xyz": list(tip_now), "roll": float(a[0]), "max_jump": 100}]
    if op == "open":
        return [{"gripper": 97.0, "seconds": 1.5}]
    if op == "close":
        return [{"gripper": 5.0, "seconds": 1.5}]
    if op == "grip":
        return [{"gripper": float(a[0]), "seconds": 1.5}]
    if op == "pick":
        x, y, ztop = float(a[0]), float(a[1]), float(a[2])
        r = float(a[3]) if len(a) > 3 else roll_now
        return [
            {"xyz": [x, y, ztop + 0.01], "roll": r, "gripper": 97, "max_jump": 100},
            {"xyz": [x, y, ztop - 0.015], "roll": r},
            {"gripper": 5, "seconds": 2.0},
            {"xyz": [x, y, ztop + 0.015], "roll": r, "gripper": 5},
        ]
    if op == "place":
        x, y, z = tip_now
        return [
            {"xyz": [x, y, z - 0.025], "roll": roll_now},
            {"gripper": 97, "seconds": 1.5},
            {"xyz": [x, y, z + 0.01], "roll": roll_now},
        ]
    if op == "j":
        return [{"rel": {a[0]: float(a[1])}}]
    if op == "find":
        return [{"find": " ".join(a)}]
    if op == "home":
        return [{"home": True}]
    if op in ("quit", "exit"):
        return [{"quit": True}]
    if op == "status":
        return [{"status": True}]
    raise ValueError(f"unknown command {op!r}; type help")
