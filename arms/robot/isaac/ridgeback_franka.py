"""Ridgeback-Franka handle for Isaac Sim 6.2 (lazy imports; call only inside the sim worker).

Approach (docs/research.md §9.4): the base is moved KINEMATICALLY (``set_world_pose`` each physics step from the
pure-pursuit follower), the arm is stowed while driving, and grasping is "magic": the hand follows a scripted arc and
the item is attached with a FixedJoint (or reparented with gravity off), then reparented into the basket.

TODO (on the GPU box; numbered to match arms/robot/README.md scope):
 1. [P0] Confirm the asset path under ``get_assets_root_path()`` and read the real prim/joint names in the stage
    browser (base link, ``panda_hand``, finger joints) -> fill the constants below.
 2. [P0] ``set_base_pose``: map the store-frame Pose2D to the base prim (z = 0, yaw about +Z).
 3. [P0] Basket: cube-shell prims with colliders parented to the base link at ``BASKET_OFFSET_LOCAL``.
 4. [P1] Drive the Franka joints along the magic-grasp arc (IK target) instead of teleporting the item.
 5. [P2] cuMotion RMPflow (``isaacsim.robot_motion.cumotion``) for real picking; arm-only model, stationary base.
Avoid deprecated 6.x APIs: ``isaacsim.robot.wheeled_robots`` controllers, ``isaacsim.robot_motion.lula``.
"""

import math
from typing import Any

from arms.robot.instances import BASKET_OFFSET_LOCAL

RIDGEBACK_FRANKA_USD = "/Isaac/Robots/Clearpath/RidgebackFranka/ridgeback_franka.usd"
"""Relative to ``isaacsim.storage.native.get_assets_root_path()`` (verify the prefix in the stage browser)."""

ROBOT_PRIM = "/World/Robot"
HAND_LINK = "panda_hand"  # TODO(1): confirm
STOW_JOINTS = [0.0, -1.2, 0.0, -2.6, 0.0, 1.6, 0.8]
"""Franka joint positions (rad) with the arm folded over the base while driving."""


class RidgebackFranka:
    """Thin wrapper over the robot prim. Construct after the stage exists; ``load()`` adds the USD reference."""

    def __init__(self, prim_path: str = ROBOT_PRIM) -> None:
        self.prim_path = prim_path
        self._xform: Any = None
        self._articulation: Any = None

    def load(self) -> None:
        from isaacsim.core.utils.stage import add_reference_to_stage  # lazy: Isaac only
        from isaacsim.storage.native import get_assets_root_path

        root = get_assets_root_path()
        if not root:
            raise RuntimeError("Isaac asset root not reachable (the GPU box needs internet)")
        add_reference_to_stage(usd_path=root + RIDGEBACK_FRANKA_USD, prim_path=self.prim_path)
        # TODO(1): wrap the articulation (6.x experimental API) once prim names are confirmed.

    def set_base_pose(self, x: float, y: float, theta: float) -> None:
        """Kinematic base pose in the store frame (USD x/y == store x/y, Z-up)."""
        from pxr import Gf, UsdGeom  # lazy

        if self._xform is None:
            import omni.usd

            stage = omni.usd.get_context().get_stage()
            self._xform = UsdGeom.Xformable(stage.GetPrimAtPath(self.prim_path))
        ops = self._xform.GetOrderedXformOps()
        if not ops:
            self._xform.AddTranslateOp()
            self._xform.AddRotateZOp()
            ops = self._xform.GetOrderedXformOps()
        ops[0].Set(Gf.Vec3d(x, y, 0.0))
        ops[1].Set(math.degrees(theta))

    def stow_arm(self) -> None:
        # TODO(4): set joint positions to STOW_JOINTS via the articulation.
        pass

    def basket_world(self, x: float, y: float, theta: float) -> tuple[float, float, float]:
        lx, ly, lz = BASKET_OFFSET_LOCAL
        c, s = math.cos(theta), math.sin(theta)
        return x + c * lx - s * ly, y + s * lx + c * ly, lz


def attach_item(item_prim_path: str, parent_prim_path: str) -> None:
    """Magic grasp: reparent the item under ``parent_prim_path`` (hand or basket) keeping its world transform, and
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
