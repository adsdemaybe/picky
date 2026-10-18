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
    {"id": 5, "home": true}  /  {"id": 6, "quit": true}
After each command: <dir>/state_<id>.json, cam<i>_<id>.jpg. Any arm-servo error -> home + release + exit.
No command for --idle-timeout seconds -> home + release + exit.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import cv2

from .config import RigConfig
from .grounding import detect
from .pick_place import ARM_JOINTS, connect_robot, move, read_pose

MAX_STEP_DEG = 10.0


def servo_errors(robot) -> dict[str, int]:
    bad = {}
    for name, m in robot.bus.motors.items():
        _, comm, err = robot.bus.packet_handler.ping(robot.bus.port_handler, m.id)
        if comm != 0 or err != 0:
            bad[name] = err if comm == 0 else -1
    return bad


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
        if args.find and first is not None:
            try:
                d = detect(first, [args.find], cfg.perceptron_model).get(args.find)
                det = {"box": d.box_px, "center": d.center_px} if d else None
            except Exception as e:  # noqa: BLE001
                det = {"error": str(e)}
        return {"images": paths, "detection": det}

    robot = connect_robot(cfg)
    rest, rest_grip = read_pose(robot)
    state = {"id": 0, "joints": rest, "gripper": rest_grip, **capture("0")}
    (out / "state_0.json").write_text(json.dumps(state, indent=1))
    print("ready; holding start pose", flush=True)
    last_cmd = time.time()
    try:
        while True:
            cmd_file = out / "cmd.json"
            if not cmd_file.exists():
                if time.time() - last_cmd > args.idle_timeout:
                    print("idle timeout", flush=True)
                    break
                time.sleep(0.1)
                continue
            cmd = json.loads(cmd_file.read_text())
            cmd_file.unlink()
            last_cmd = time.time()
            cid = cmd.get("id", int(last_cmd))
            if cmd.get("quit"):
                break
            pose, grip = read_pose(robot)
            note = ""
            if cmd.get("home"):
                move(robot, rest, rest_grip, seconds=2.5)
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
            load = robot.bus.sync_read("Present_Load", normalize=False, num_retry=2)  # contact sensing
            state = {
                "id": cid,
                "cmd": cmd,
                "joints": pose,
                "gripper": grip,
                "servo_errors": bad,
                "note": note,
                "load": {k: int(v) for k, v in load.items()},
            }
            state.update(capture(str(cid)))
            (out / f"state_{cid}.json").write_text(json.dumps(state, indent=1))
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
