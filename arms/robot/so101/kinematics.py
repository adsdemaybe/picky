"""SO-101 inverse kinematics on the official URDF (ikpy), mapped to LeRobot joint degrees."""

from __future__ import annotations

import math

import numpy as np

from .config import RigConfig


class SO101Kinematics:
    def __init__(self, cfg: RigConfig):
        import ikpy.chain

        self.cfg = cfg
        chain = ikpy.chain.Chain.from_urdf_file(str(cfg.urdf_path), base_elements=["base_link"])
        names = [link.name for link in chain.links]
        # Only the 5 arm joints are active; fixed links and the gripper jaw stay passive.
        mask = [any(j in n for j in cfg.joint_names) for n in names]
        self.chain = ikpy.chain.Chain.from_urdf_file(
            str(cfg.urdf_path), base_elements=["base_link"], active_links_mask=mask
        )
        self.active = [i for i, m in enumerate(mask) if m]
        self.active_names = [next(j for j in cfg.joint_names if j in names[i]) for i in self.active]

    def ik(self, xyz: tuple[float, float, float], seed_deg: dict[str, float] | None = None) -> dict[str, float]:
        """Target gripper position in the base frame -> LeRobot joint degrees (gripper pointing down)."""
        q0 = np.zeros(len(self.chain.links))
        if seed_deg:
            for i, n in zip(self.active, self.active_names):
                q0[i] = self._deg_to_rad(n, seed_deg.get(n, 0.0))
        q = self.chain.inverse_kinematics(
            target_position=np.array(xyz),
            target_orientation=np.array([0.0, 0.0, -1.0]),
            orientation_mode="Z",
            initial_position=q0,
        )
        reached = self.chain.forward_kinematics(q)[:3, 3]
        err = float(np.linalg.norm(reached - np.array(xyz)))
        if err > 0.02:
            raise ValueError(f"IK target {xyz} unreachable (error {err * 1000:.0f} mm)")
        out = {n: self._rad_to_deg(n, q[i]) for i, n in zip(self.active, self.active_names)}
        # Keep the wrist roll aligned with the pan so the jaws stay square to the table edge.
        out["wrist_roll"] = 0.0
        return out

    def _rad_to_deg(self, name: str, q: float) -> float:
        return self.cfg.joint_sign.get(name, 1.0) * math.degrees(q) + self.cfg.joint_offset_deg.get(name, 0.0)

    def _deg_to_rad(self, name: str, d: float) -> float:
        return math.radians((d - self.cfg.joint_offset_deg.get(name, 0.0)) / self.cfg.joint_sign.get(name, 1.0))
