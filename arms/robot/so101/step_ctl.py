"""Step-by-step remote control of the SO-101 with camera feedback after every move.

Holds the arm (torque on) and executes one small command at a time from a command file, so an operator (or an agent
over SSH) can look at the wrist + overhead/side camera after each step and decide the next one. On macOS start it
from Terminal so it may use the cameras.

    python -m arms.robot.so101.step_ctl --cams 0,1 --find "green gum packet"

Commands: write JSON to <dir>/cmd.json (write to a temp name, then rename):
    {"id": 1, "rel": {"shoulder_lift": 3}}          relative joint move in degrees (each |delta| <= 10)
    {"id": 2, "abs": {"wrist_roll": 90}}             absolute joint target (each step limited to 10 deg)
    {"id": 3, "gripper": 20}                         gripper 0 (closed) .. 100 (open)
    {"id": 4, "find": "green gum packet"}            just re-capture + detect
    {"id": 7, "xyz": [0.20, -0.05, 0.08], "roll": 130}  gripper tip to x,y,z (m, base frame), pointing down (IK)
    {"id": 5, "home": true}  /  {"id": 6, "quit": true}
After each command: <dir>/state_<id>.json, cam<i>_<id>.jpg. Any arm-servo error -> home + release + exit.
No command for --idle-timeout seconds -> home + release + exit.

Or type commands in the terminal running it (see TYPED_HELP): `up 2`, `pick 0.225 -0.10 0.05`, `place`, ...
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from pathlib import Path

import cv2

from .config import RigConfig
from .grounding import detect
from .kinematics import SO101Kinematics
from .pick_place import ARM_JOINTS, connect_robot, move, read_pose
from .typed_cmds import TYPED_HELP, parse_typed

MAX_STEP_DEG = 10.0


def servo_errors(robot) -> dict[str, int]:
    bad = {}
    for name, m in robot.bus.motors.items():
        _, comm, err = robot.bus.packet_handler.ping(robot.bus.port_handler, m.id)
        if comm != 0 or err != 0:
            bad[name] = err if comm == 0 else -1
    return bad


def settle(robot, target: dict[str, float], gripper: float, tol: float = 0.8, rounds: int = 4) -> float:
    """Software integral action: the servos' low P gain leaves a few degrees of steady-state error, so re-command
    each joint past its target by the remaining error (capped) until within `tol`. Returns the final max error."""
    err = 0.0
    for _ in range(rounds):
        pose, _ = read_pose(robot)
        errs = {j: target[j] - pose[j] for j in ARM_JOINTS}
        err = max(abs(e) for e in errs.values())
        if err < tol:
            break
        cmd = {f"{j}.pos": target[j] + max(-6.0, min(6.0, errs[j])) for j in ARM_JOINTS}
        cmd["gripper.pos"] = gripper
        robot.send_action(cmd)
        time.sleep(0.5)
    return err


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="/tmp/picky_ctl")
    ap.add_argument("--cams", default="0", help="comma-separated camera indices; detection runs on the first")
    ap.add_argument("--find", default="", help="object to detect in the first camera after every step")
    ap.add_argument("--idle-timeout", type=float, default=600.0)
    args = ap.parse_args()

    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*"):
        f.unlink()
    cfg = RigConfig()
    caps = {int(i): cv2.VideoCapture(int(i)) for i in args.cams.split(",")}
    for cap in caps.values():  # two full-HD streams on one USB hub tear each other's frames
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    find_label = ""

    def capture(tag: str) -> dict:
        paths = {}
        first = None
        for i, cap in caps.items():
            frame = None
            for _ in range(6):  # flush buffered frames so we see the current pose
                ok, f = cap.read()
                frame = f if ok else frame
            if frame is None:
                continue
            if frame.shape[1] > 1280:
                frame = cv2.resize(frame, (frame.shape[1] // 2, frame.shape[0] // 2))
            p = out / f"cam{i}_{tag}.jpg"
            cv2.imwrite(str(p), frame)
            paths[i] = str(p)
            first = frame if first is None else first
        det = None
        label = find_label or args.find
        if label and first is not None:
            try:
                d = detect(first, [label], cfg.perceptron_model).get(label)
                det = {"box": d.box_px, "center": d.center_px} if d else None
            except Exception as e:  # noqa: BLE001
                det = {"error": str(e)}
        return {"images": paths, "detection": det}

    kin = SO101Kinematics(cfg)

    def tip(pose: dict[str, float]) -> list[float]:
        import numpy as np

        q = np.zeros(len(kin.chain.links))
        for i, n in zip(kin.active, kin.active_names, strict=True):
            q[i] = kin._deg_to_rad(n, pose[n])
        return [round(float(v), 4) for v in kin.chain.forward_kinematics(q)[:3, 3]]

    robot = connect_robot(cfg)
    for name, cal in robot.calibration.items():  # degrees mode: 0 at mid-range, +-half the calibrated span
        half = (cal.range_max - cal.range_min) / 2 * 360 / 4096
        kin.limits_deg[name] = (-half + 3.0, half - 3.0)
    rest, rest_grip = read_pose(robot)
    state = {"id": 0, "joints": rest, "gripper": rest_grip, **capture("0")}
    (out / "state_0.json").write_text(json.dumps(state, indent=1))
    print("ready; holding start pose. Type commands here (help for the list).", flush=True)
    typed: queue.Queue[str] = queue.Queue()

    def read_stdin() -> None:
        for line in sys.stdin:
            typed.put(line)

    if sys.stdin.isatty():
        threading.Thread(target=read_stdin, daemon=True).start()
    pending: list[dict] = []
    next_id = 1000
    last_cmd = time.time()
    try:
        while True:
            cmd_file = out / "cmd.json"
            if not pending and not typed.empty():
                line = typed.get().strip()
                if line.lower() == "help":
                    print(TYPED_HELP, flush=True)
                    continue
                pose, _ = read_pose(robot)
                try:
                    pending = parse_typed(line, tip(pose), pose["wrist_roll"])
                except (ValueError, IndexError) as e:
                    print(f"? {e}", flush=True)
                for c in pending:
                    next_id += 1
                    c["id"], c["typed"] = next_id, True
            if pending:
                cmd = pending.pop(0)
            elif cmd_file.exists():
                cmd = json.loads(cmd_file.read_text())
                cmd_file.unlink()
            else:
                if time.time() - last_cmd > args.idle_timeout:
                    print("idle timeout", flush=True)
                    break
                time.sleep(0.1)
                continue
            last_cmd = time.time()
            cid = cmd.get("id", int(last_cmd))
            if cmd.get("quit"):
                break
            pose, grip = read_pose(robot)
            note = ""
            if cmd.get("status"):
                pass
            elif cmd.get("find"):
                find_label = cmd["find"]
            elif cmd.get("home"):
                move(robot, rest, rest_grip, seconds=2.5)
            elif "xyz" in cmd:
                try:
                    target = kin.ik(tuple(cmd["xyz"]), pose)
                    target["wrist_roll"] = float(cmd.get("roll", pose["wrist_roll"]))
                    jump = max(abs(target[j] - pose[j]) for j in ARM_JOINTS)
                    if jump > float(cmd.get("max_jump", 45)):
                        note = f"refused: IK solution needs a {jump:.0f} deg joint jump"
                    else:
                        g = float(cmd["gripper"]) if "gripper" in cmd else grip
                        move(robot, target, g, seconds=max(1.5, jump / 15.0), steps=60)
                        residual = settle(robot, target, g)
                        note = f"settled to {residual:.1f} deg"
                except ValueError as e:
                    note = f"IK failed: {e}"
            else:
                target = dict(pose)
                for j, d in (cmd.get("rel") or {}).items():
                    target[j] = pose[j] + max(-MAX_STEP_DEG, min(MAX_STEP_DEG, float(d)))
                for j, v in (cmd.get("abs") or {}).items():
                    target[j] = pose[j] + max(-MAX_STEP_DEG, min(MAX_STEP_DEG, float(v) - pose[j]))
                g = float(cmd["gripper"]) if "gripper" in cmd else grip
                move(robot, {j: target[j] for j in ARM_JOINTS}, g, seconds=float(cmd.get("seconds", 1.2)))
            time.sleep(0.4)
            bad = servo_errors(robot)
            arm_bad = {k: v for k, v in bad.items() if k != "gripper"}
            pose, grip = read_pose(robot)
            if arm_bad:
                note = f"arm servo error {arm_bad}: going home and stopping"
            note = note or ""
            load = robot.bus.sync_read("Present_Load", normalize=False, num_retry=2)  # contact sensing
            state = {
                "id": cid,
                "cmd": cmd,
                "joints": pose,
                "tip": tip(pose),
                "gripper": grip,
                "servo_errors": bad,
                "note": note,
                "load": {k: int(v) for k, v in load.items()},
            }
            state.update(capture(str(cid)))
            (out / f"state_{cid}.json").write_text(json.dumps(state, indent=1))
            if cmd.get("typed"):
                t = state["tip"]
                det = state.get("detection")
                seen = det.get("center") if isinstance(det, dict) else None
                print(
                    f"  tip x={t[0]:.3f} y={t[1]:.3f} z={t[2]:.3f}  roll={pose['wrist_roll']:.0f}  gripper={grip:.0f}"
                    + (f"  [{note}]" if note else "")
                    + (f"  {find_label or args.find} seen at {seen}" if seen else ""),
                    flush=True,
                )
                if note.startswith(("IK failed", "refused")):
                    pending.clear()  # don't run the rest of a macro after a failed step
            else:
                print(f"done {cid}: {cmd}", flush=True)
            if arm_bad:
                break
    finally:
        try:
            move(robot, rest, rest_grip, seconds=3.0)
        finally:
            robot.disconnect()
            for cap in caps.values():
                cap.release()
            (out / "stopped").write_text("1")
            print("stopped: start pose, torque off", flush=True)


if __name__ == "__main__":
    main()
