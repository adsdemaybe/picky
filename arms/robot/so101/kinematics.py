"""SO-101 inverse kinematics on the official URDF (ikpy), mapped to LeRobot joint degrees."""

from __future__ import annotations

import math
import warnings

import numpy as np

from .config import RigConfig


class SO101Kinematics:
    def __init__(self, cfg: RigConfig):
        import ikpy.chain

        self.cfg = cfg
        with warnings.catch_warnings():  # the official URDF trips harmless ikpy "fixed link" warnings
            warnings.simplefilter("ignore", UserWarning)
            chain = ikpy.chain.Chain.from_urdf_file(str(cfg.urdf_path), base_elements=["base_link"])
            names = [link.name for link in chain.links]
            # Only the 5 arm joints are active; fixed links and the gripper jaw stay passive.
            mask = [any(j in n for j in cfg.joint_names) for n in names]
            self.chain = ikpy.chain.Chain.from_urdf_file(
                str(cfg.urdf_path), base_elements=["base_link"], active_links_mask=mask
            )
        self.active = [i for i, m in enumerate(mask) if m]
        # LeRobot-degree limits of the real arm (from its calibration); IK solutions outside them are rejected.
        self.limits_deg: dict[str, tuple[float, float]] = {}
        self.active_names = [next(j for j in cfg.joint_names if j in names[i]) for i in self.active]

    def ik(self, xyz: tuple[float, float, float], seed_deg: dict[str, float] | None = None) -> dict[str, float]:
        """Target gripper-tip position in the base frame -> LeRobot joint degrees.

        The SO-101 is small (~25 cm upper+lower arm), so a straight-down grasp is only reachable close in. Approach
        pitches are tried from vertical toward horizontal (tilted toward the target) and the steepest reachable wins.
        """
        target = np.array(xyz)
        heading = math.atan2(target[1], target[0])
        seeds = self._seeds(seed_deg)
        for pitch_deg in (90, 75, 60, 45, 30, 15):
            p = math.radians(pitch_deg)
            approach = np.array([math.cos(p) * math.cos(heading), math.cos(p) * math.sin(heading), -math.sin(p)])
            q, err = self._solve(target, approach, seeds)
            out = {n: self._rad_to_deg(n, q[i]) for i, n in zip(self.active, self.active_names, strict=True)}
            within = all(
                lo <= out[n] <= hi for n, (lo, hi) in self.limits_deg.items() if n in out and n != "wrist_roll"
            )
            if err < 0.008 and within:
                out["wrist_roll"] = 0.0  # keep the jaws square to the table edge
                return out
        raise ValueError(f"IK target {xyz} unreachable at any approach pitch (best error {err * 1000:.0f} mm)")

    def _solve(self, target: np.ndarray, approach: np.ndarray, seeds: list[np.ndarray]) -> tuple[np.ndarray, float]:
        best_q, best = seeds[0], float("inf")
        for q0 in seeds:
            q = self.chain.inverse_kinematics(
                target_position=target, target_orientation=approach, orientation_mode="Z", initial_position=q0
            )
            e = float(np.linalg.norm(self.chain.forward_kinematics(q)[:3, 3] - target))
            if e < best:
                best_q, best = q, e
            if best < 0.004:
                break
        return best_q, best

    def _seeds(self, seed_deg: dict[str, float] | None) -> list[np.ndarray]:
        n_links = len(self.chain.links)
        seeds = [np.zeros(n_links)]
        if seed_deg:
            q0 = np.zeros(n_links)
            for i, n in zip(self.active, self.active_names, strict=True):
                q0[i] = self._deg_to_rad(n, seed_deg.get(n, 0.0))
            seeds.insert(0, q0)
        rng = np.random.default_rng(0)
        for _ in range(6):
            q0 = np.zeros(n_links)
            for i in self.active:
                lo, hi = self.chain.links[i].bounds
                q0[i] = rng.uniform(max(lo, -2.5), min(hi, 2.5))
            seeds.append(q0)
        return seeds

    def _rad_to_deg(self, name: str, q: float) -> float:
        return self.cfg.joint_sign.get(name, 1.0) * math.degrees(q) + self.cfg.joint_offset_deg.get(name, 0.0)

    def _deg_to_rad(self, name: str, d: float) -> float:
        return math.radians((d - self.cfg.joint_offset_deg.get(name, 0.0)) / self.cfg.joint_sign.get(name, 1.0))
