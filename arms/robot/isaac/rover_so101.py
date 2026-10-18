"""SO-101 rover handle for Isaac Sim 6.2 (lazy imports; call only inside the sim worker).

THE robot (contracts.common): a small kinematic rover base (radius ``ROBOT_RADIUS_M`` = 0.3 m, ~0.46 x 0.36 m box)
carrying a vertical prismatic lift (``LIFT_MIN_M``..``LIFT_MAX_M`` = SO-101 shoulder height) with an SO-101 arm
mounted ``ARM_MOUNT_X_M`` forward of the base center, a pan-tilt front camera on the mast, a wrist camera on the
gripper and a basket on the back (``BASKET_OFFSET_LOCAL`` / ``BASKET_SIZE_M``).

Approach (docs/research.md §9.4): the base is moved KINEMATICALLY (``set_base_pose`` each physics step from the
pure-pursuit follower), the lift is a kinematic prismatic offset (``set_lift``), the arm is stowed while driving and
grasping is "magic": the gripper follows a scripted arc (``MagicGraspAction``) and the item is attached
(``attach_item``: reparent + kinematic), then reparented into the basket (``PlaceAction``). The physical SO-101 and
VLA fine-tuning (LeRobot / LeIsaac) come later; keep this model consistent with contracts.common so they drop in.

TODO (on the GPU box "vhagar"; numbered to match arms/robot/README.md scope):
 1. [P0] Import the SO-101 URDF (SO-ARM100 repo ``Simulation/SO101/so101_new_calib.urdf``, unverified path) with
    ``isaacsim.asset.importer.urdf`` under ``<prim>/Lift/SO101``; confirm link names (``gripper_frame_link``).
 2. [P0] ``set_base_pose``: store-frame Pose2D -> the rover prim inside ``/World/Stores/<store_id>`` (z = 0, yaw +Z).
 3. [P0] Basket: cube-shell prims with colliders parented to the base at ``BASKET_OFFSET_LOCAL``.
 4. [P1] Drive the SO-101 joints along the magic-grasp arc (IK target) instead of teleporting the item.
 5. [P2] cuMotion / RMPflow for real picking; later LeIsaac VLA policies.
Avoid deprecated 6.x APIs: ``isaacsim.robot.wheeled_robots`` controllers, ``isaacsim.robot_motion.lula``.
"""

import math
from typing import Any

from contracts.common import ARM_MOUNT_X_M, LIFT_MAX_M, LIFT_MIN_M, ROBOT_RADIUS_M
from contracts.sim import BASKET_OFFSET_LOCAL, BASKET_SIZE_M

SO101_URDF = "Simulation/SO101/so101_new_calib.urdf"
"""Relative to a checkout of github.com/TheRobotStudio/SO-ARM100 (set PICKY_SO101_DIR on the GPU box; unverified)."""
ROVER_BASE_SIZE_M: tuple[float, float, float] = (0.46, 0.36, 0.25)
"""Base box (x forward, y left, z up); its circumradius ~= ROBOT_RADIUS_M."""
MAST_HEIGHT_M = 1.5
GRIPPER_LINK = "gripper_frame_link"  # TODO(1): confirm in the imported articulation
STOW_JOINTS = [0.0, -1.6, 1.5, 1.0, 0.0, 0.0]
"""SO-101 joint positions (rad: shoulder_pan, shoulder_lift, elbow_flex, wrist_flex, wrist_roll, gripper), folded."""
ROBOT_ROOT = "/World/Stores/{store_id}/Robots/{robot_id}"


class RoverSO101:
    """Thin wrapper over one rover prim. Construct after the stage exists; ``load()`` builds the prims."""

    def __init__(self, prim_path: str = "/World/Robot", robot_id: str | None = None) -> None:
        self.prim_path = prim_path
        self.robot_id = robot_id
        self.lift_m = LIFT_MIN_M
        self._xform: Any = None
        self._lift_xform: Any = None
        self.arm_loaded = False

    def load(self) -> None:
        """Base box + mast + lift carriage (+ the SO-101 URDF when available; a proxy arm otherwise)."""
        import omni.usd  # lazy: Isaac only
        from pxr import Gf, UsdGeom

        stage = omni.usd.get_context().get_stage()
        root = UsdGeom.Xform.Define(stage, self.prim_path)
        bx, by, bz = ROVER_BASE_SIZE_M
        base = UsdGeom.Cube.Define(stage, f"{self.prim_path}/Base")
        base.AddScaleOp().Set(Gf.Vec3f(bx / 2, by / 2, bz / 2))
        base.AddTranslateOp(UsdGeom.XformOp.PrecisionDouble, "offset").Set(Gf.Vec3d(0, 0, 1.0))
        mast = UsdGeom.Cube.Define(stage, f"{self.prim_path}/Mast")
        mast.AddTranslateOp().Set(Gf.Vec3d(ARM_MOUNT_X_M, 0.0, MAST_HEIGHT_M / 2))
        mast.AddScaleOp().Set(Gf.Vec3f(0.02, 0.02, MAST_HEIGHT_M / 2))
        lift = UsdGeom.Xform.Define(stage, f"{self.prim_path}/Lift")
        self._lift_xform = lift
        lift.AddTranslateOp().Set(Gf.Vec3d(ARM_MOUNT_X_M, 0.0, self.lift_m))
        lx, ly, lz = BASKET_OFFSET_LOCAL
        sx, sy, sz = BASKET_SIZE_M
        basket = UsdGeom.Cube.Define(stage, f"{self.prim_path}/Basket")
        basket.AddTranslateOp().Set(Gf.Vec3d(lx, ly, lz - sz / 2))
        basket.AddScaleOp().Set(Gf.Vec3f(sx / 2, sy / 2, 0.005))
        self._xform = UsdGeom.Xformable(root.GetPrim())
        try:
            self._import_so101(f"{self.prim_path}/Lift/SO101")
            self.arm_loaded = True
        except Exception:  # proxy arm: a thin box (the demo still works with magic grasp)
            arm = UsdGeom.Cube.Define(stage, f"{self.prim_path}/Lift/ArmProxy")
            arm.AddTranslateOp().Set(Gf.Vec3d(0.15, 0.0, 0.0))
            arm.AddScaleOp().Set(Gf.Vec3f(0.15, 0.02, 0.02))

    def _import_so101(self, dest: str) -> None:
        import os

        so101_dir = os.environ.get("PICKY_SO101_DIR")
        if not so101_dir:
            raise RuntimeError("PICKY_SO101_DIR not set")
        from isaacsim.asset.importer.urdf import _urdf  # lazy; TODO(1): verify the 6.2 importer API

        importer = _urdf.acquire_urdf_interface()
        cfg = _urdf.ImportConfig()
        cfg.fix_base = True
        robot = importer.parse_urdf(so101_dir, SO101_URDF, cfg)
        importer.import_robot(so101_dir, SO101_URDF, robot, cfg, dest)

    def set_base_pose(self, x: float, y: float, theta: float) -> None:
        """Kinematic base pose in the STORE frame (the rover prim lives under the store's translation-only Xform)."""
        from pxr import Gf  # lazy

        if self._xform is None:
            import omni.usd
            from pxr import UsdGeom

            stage = omni.usd.get_context().get_stage()
            self._xform = UsdGeom.Xformable(stage.GetPrimAtPath(self.prim_path))
        ops = self._xform.GetOrderedXformOps()
        if len(ops) < 2:
            self._xform.ClearXformOpOrder()
            self._xform.AddTranslateOp()
            self._xform.AddRotateZOp()
            ops = self._xform.GetOrderedXformOps()
        ops[0].Set(Gf.Vec3d(x, y, 0.0))
        ops[1].Set(math.degrees(theta))

    def set_lift(self, height_m: float) -> None:
        """SO-101 shoulder height on the lift (clamped to LIFT_MIN_M..LIFT_MAX_M)."""
        from pxr import Gf  # lazy

        self.lift_m = min(max(height_m, LIFT_MIN_M), LIFT_MAX_M)
        if self._lift_xform is not None:
            ops = self._lift_xform.GetOrderedXformOps()
            if ops:
                ops[0].Set(Gf.Vec3d(ARM_MOUNT_X_M, 0.0, self.lift_m))

    def stow_arm(self) -> None:
        # TODO(4): set joint positions to STOW_JOINTS via the articulation.
        pass

    def basket_world(self, x: float, y: float, theta: float) -> tuple[float, float, float]:
        """Basket drop point in the store frame for a rover at (x, y, theta)."""
        return basket_world(x, y, theta)

    def gripper_rest_world(self, x: float, y: float, theta: float) -> tuple[float, float, float]:
        """Where the gripper rests (in front of the mast at the current lift height)."""
        reach = ARM_MOUNT_X_M + 0.25
        return x + math.cos(theta) * reach, y + math.sin(theta) * reach, self.lift_m


def basket_world(x: float, y: float, theta: float) -> tuple[float, float, float]:
    lx, ly, lz = BASKET_OFFSET_LOCAL
    c, s = math.cos(theta), math.sin(theta)
    return x + c * lx - s * ly, y + s * lx + c * ly, lz


def attach_item(item_prim_path: str, parent_prim_path: str) -> None:
    """Magic grasp: reparent the item under ``parent_prim_path`` (gripper or basket) keeping its world transform, and
    make it kinematic so physics doesn't fight the script."""
    import omni.kit.commands  # lazy
    import omni.usd
    from pxr import UsdPhysics

    stage = omni.usd.get_context().get_stage()
    prim = stage.GetPrimAtPath(item_prim_path)
    if prim.HasAPI(UsdPhysics.RigidBodyAPI):
        UsdPhysics.RigidBodyAPI(prim).CreateKinematicEnabledAttr(True)
    name = item_prim_path.rsplit("/", 1)[-1]
    omni.kit.commands.execute(
        "MovePrim", path_from=item_prim_path, path_to=f"{parent_prim_path}/{name}", keep_world_transform=True
    )


# v0.1 names (arms/sim/app.py may still import them): the Ridgeback-Franka was replaced by the SO-101 rover.
RidgebackFranka = RoverSO101
RIDGEBACK_FRANKA_USD = SO101_URDF
_ = ROBOT_RADIUS_M  # documented above; the footprint itself is drawn from ROVER_BASE_SIZE_M
