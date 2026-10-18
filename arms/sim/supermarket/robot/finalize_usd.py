"""Flatten the mujoco-usd-converter output into one USD and add PhysX joint drives for Isaac Sim.

The converter authors MuJoCo actuator schemas (MjcActuator), which Isaac Sim's PhysX ignores, so without this step no
joint can be commanded. Runs with any `pxr` (usd-core, or Isaac Sim's python.sh).

    cd arms/sim/supermarket
    python robot/finalize_usd.py assets/build/rm2_basket/usd/picky_rm2_basket.usda assets/build/rm2_basket.usd
"""

from __future__ import annotations

import sys

from pxr import Sdf, Usd, UsdPhysics

# USD angular drive gains are per degree; linear per meter.
#   arm/steer: position drives; wheels: velocity drives (stiffness 0).
DRIVES = {
    "arm_joint1": ("angular", 400.0, 40.0, 80.0),
    "arm_joint2": ("angular", 400.0, 40.0, 80.0),
    "arm_joint3": ("angular", 400.0, 40.0, 80.0),
    "arm_joint4": ("angular", 150.0, 15.0, 30.0),
    "arm_joint5": ("angular", 150.0, 15.0, 30.0),
    "gripper_base_joint": ("angular", 150.0, 15.0, 30.0),
    "finger_left_joint": ("linear", 5000.0, 200.0, 60.0),
    "finger_right_joint": ("linear", 5000.0, 200.0, 60.0),
    **{f"steer_joint{i}": ("angular", 300.0, 30.0, 60.0) for i in range(1, 5)},
    **{f"wheel_joint{i}": ("angular", 0.0, 50.0, 120.0) for i in range(1, 5)},
}


def main(src: str, dst: str) -> None:
    flat = Usd.Stage.Open(src).Flatten()
    stage = Usd.Stage.Open(flat)
    found = set()
    for prim in stage.Traverse():
        spec = DRIVES.get(prim.GetName())
        if spec is None or not prim.IsA(UsdPhysics.Joint):
            continue
        kind, stiffness, damping, max_force = spec
        drive = UsdPhysics.DriveAPI.Apply(prim, kind)
        drive.CreateTypeAttr("force")
        drive.CreateStiffnessAttr(stiffness)
        drive.CreateDampingAttr(damping)
        drive.CreateMaxForceAttr(max_force)
        drive.CreateTargetPositionAttr(0.0)
        drive.CreateTargetVelocityAttr(0.0)
        found.add(prim.GetName())
    missing = sorted(set(DRIVES) - found)
    if missing:
        raise SystemExit(f"joints not found in {src}: {missing}")
    # Isaac Sim wants the physics scene in the world stage, not inside a referenced robot asset.
    for prim in list(stage.Traverse()):
        if prim.IsA(UsdPhysics.Scene):
            stage.RemovePrim(prim.GetPath())
    stage.GetRootLayer().Export(dst)
    print(f"wrote {dst}: {len(found)} drives, default prim {Sdf.Path(stage.GetDefaultPrim().GetPath())}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
