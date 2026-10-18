"""Forward / inverse kinematics for Picky's long-reach SO-101 on its fixed riser (5 arm joints), numpy only.

The chain comes from `<robot>.kin.json` written by build_so101_rover.py: the exact SO-101 URDF joint origins, axes and
limits with the elbow_flex / wrist_flex origins moved out by the upper-arm / forearm extensions, mounted on the riser
(`arm_base`). IK is damped least squares on a numerical Jacobian with joint-limit clamping and several seeds; targets
are a tool-point position plus either a full tool orientation or just an approach direction (tool z). Also min-jerk
helpers and straight-line tool moves (IK-sampled waypoints).

    kin = RoverKinematics("picky_so101_rover.kin.json")
    q, (pos_err, ang_err) = kin.solve(p_base, approach=(1, 0, 0), roll=kin.grasp_roll)
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
LINKS = ["so101_shoulder", "so101_upper_arm", "so101_lower_arm", "so101_wrist", "so101_gripper"]


def _axis_angle(axis, a: float) -> np.ndarray:
    x, y, z = axis
    c, s = math.cos(a), math.sin(a)
    C = 1 - c
    return np.array(
        [
            [c + x * x * C, x * y * C - z * s, x * z * C + y * s],
            [y * x * C + z * s, c + y * y * C, y * z * C - x * s],
            [z * x * C - y * s, z * y * C + x * s, c + z * z * C],
        ]
    )


def minjerk(s: float) -> float:
    s = min(1.0, max(0.0, s))
    return s * s * s * (10 - 15 * s + 6 * s * s)


def minjerk_vel(s: float) -> float:
    """d minjerk / ds"""
    s = min(1.0, max(0.0, s))
    return 30 * s * s * (1 - s) * (1 - s)


class RoverKinematics:
    joints = JOINTS

    def __init__(self, kin: str | Path | dict):
        k = kin if isinstance(kin, dict) else json.loads(Path(kin).read_text())
        self.k = k
        self.base0 = np.array(k["arm_base"])
        self.chain = [
            (np.array(j["T"]), np.asarray(j["axis"], float) / np.linalg.norm(j["axis"])) for j in k["arm_chain"]
        ]
        self.tcp = np.array(k["tcp_in_gripper_link"])
        g = k["gripper"]
        self.jaw_T, self.jaw_axis = np.array(g["T"]), np.asarray(g["axis"], float)
        self.lo = np.array([j["lower"] for j in k["arm_chain"]])
        self.hi = np.array([j["upper"] for j in k["arm_chain"]])
        self.grasp_roll = float(k.get("grasp_roll", math.pi / 2))
        self.vmax = np.array([k["actuators"][n]["vmax_rad_s"] for n in JOINTS])

    # --- forward kinematics (robot base frame) ------------------------------------------------------------------------
    def links(self, q) -> dict:
        """q = [pan, lift, elbow, wrist_flex, roll] -> poses of the SO-101 links + tcp (4x4, robot base frame)."""
        T = self.base0.copy()
        out = {"so101_base": T.copy()}
        for (Tj, ax), qi, n in zip(self.chain, q[:5], LINKS, strict=False):
            T = T @ Tj
            R = np.eye(4)
            R[:3, :3] = _axis_angle(ax, qi)
            T = T @ R
            out[n] = T.copy()
        out["tcp"] = T @ self.tcp
        return out

    def fk(self, q) -> np.ndarray:
        return self.links(q)["tcp"]

    def jaw(self, q, g: float) -> np.ndarray:
        """moving-jaw link pose for gripper angle g"""
        Tg = self.links(q)["so101_gripper"]
        R = np.eye(4)
        R[:3, :3] = _axis_angle(self.jaw_axis / np.linalg.norm(self.jaw_axis), g)
        return Tg @ self.jaw_T @ R

    # --- inverse kinematics ------------------------------------------------------------------------------------------
    def target_rotation(self, approach, roll: float | None = None) -> np.ndarray:
        """Tool rotation whose z = approach. roll picks the rotation about it: reference = the tool frame of the arm
        stretched out horizontally along `approach`'s heading with wrist_roll = roll."""
        a = np.asarray(approach, float)
        a /= np.linalg.norm(a)
        roll = self.grasp_roll if roll is None else roll
        heading = math.atan2(a[1], a[0]) if abs(a[2]) < 0.99 else 0.0
        q0 = np.array([0.0, 1.2, -1.2, 0.0, roll])  # upper arm forward, forearm level-ish
        R0 = self.fk(q0)[:3, :3]
        z0 = R0[:, 2].copy()
        z0[2] = 0.0
        h0 = math.atan2(z0[1], z0[0])
        R = _axis_angle((0, 0, 1), heading - h0) @ R0
        z = R[:, 2]
        v = np.cross(z, a)
        s, c = np.linalg.norm(v), float(np.dot(z, a))
        if s > 1e-9:
            R = _axis_angle(v / s, math.atan2(s, c)) @ R
        return R

    def solve(self, pos, approach=None, R=None, roll=None, seeds=None, w_rot=0.25, iters=300, ori_mode="full"):
        """Returns (q[5], (pos_err_m, ang_err_rad)). ori_mode: 'full' (tool rotation), 'axis' (tool z == approach),
        'none'."""
        pos = np.asarray(pos, float)
        if R is None and approach is not None:
            R = self.target_rotation(approach, roll)
        if R is None:
            ori_mode = "none"
        if seeds is None:
            pan = self._pan_seed(pos)
            r0 = self.grasp_roll if roll is None else roll
            seeds = [
                [pan, 0.6, -0.2, -0.4, r0],
                [pan, 1.0, -1.0, 0.0, r0],
                [pan, 0.2, 0.6, -0.8, r0],
                [pan, -0.3, 1.3, -1.0, r0],
                [pan, 1.4, -1.5, 0.3, r0],
                [pan, 0.9, 0.4, 0.4, r0],
            ]
        best = None
        for s in seeds:
            q = np.clip(np.array(s, float), self.lo, self.hi)
            q, e = self._dls(q, pos, R, w_rot, iters, ori_mode)
            score = e[0] + 0.05 * e[1]
            if best is None or score < best[2]:
                best = (q, e, score)
        return best[0], best[1]

    def _pan_seed(self, pos) -> float:
        """pan whose stretched-out arm heading points closest to pos (seed only)"""
        best, bp = 1e9, 0.0
        for pan in np.linspace(self.lo[0], self.hi[0], 45):
            L = self.links([pan, 1.2, -1.2, 0, 0])
            o, t = L["so101_shoulder"][:3, 3], L["tcp"][:3, 3]
            d = math.atan2(t[1] - o[1], t[0] - o[0]) - math.atan2(pos[1] - o[1], pos[0] - o[0])
            d = abs((d + math.pi) % (2 * math.pi) - math.pi)
            if d < best:
                best, bp = d, float(pan)
        return bp

    def _err(self, q, pos, R, w_rot, ori_mode):
        T = self.fk(q)
        e = [pos - T[:3, 3]]
        if ori_mode == "full":
            Rc = T[:3, :3]
            e.append(w_rot * 0.5 * sum(np.cross(Rc[:, i], R[:, i]) for i in range(3)))
        elif ori_mode == "axis":
            e.append(w_rot * np.cross(T[:3, 2], R[:, 2]))
        return np.concatenate(e)

    def _fk_axes(self, q):
        """tcp pose + each joint's world axis and origin (for the analytic Jacobian), one FK pass"""
        T = self.base0
        zs, os_ = [], []
        for (Tj, ax), qi in zip(self.chain, q[:5], strict=False):
            T = T @ Tj
            zs.append(T[:3, :3] @ ax)
            os_.append(T[:3, 3])
            R = np.eye(4)
            R[:3, :3] = _axis_angle(ax, qi)
            T = T @ R
        return T @ self.tcp, np.array(zs), np.array(os_)

    def _err_jac(self, q, pos, R, w_rot, ori_mode):
        """error e (as in _err) and J = -de/dq from the geometric Jacobian (one FK instead of six)"""
        T, Z, org = self._fk_axes(q)
        p = T[:3, 3]
        Jv = np.cross(Z, p - org).T  # 3 x 5
        e = [pos - p]
        J = [Jv]
        if ori_mode == "full":
            Rc = T[:3, :3]
            e.append(w_rot * 0.5 * sum(np.cross(Rc[:, k], R[:, k]) for k in range(3)))
            Jw = np.zeros((3, 5))
            for i in range(5):
                Jw[:, i] = -0.5 * sum(np.cross(np.cross(Z[i], Rc[:, k]), R[:, k]) for k in range(3))
            J.append(w_rot * Jw)
        elif ori_mode == "axis":
            zc = T[:3, 2]
            e.append(w_rot * np.cross(zc, R[:, 2]))
            J.append(w_rot * np.array([-np.cross(np.cross(Z[i], zc), R[:, 2]) for i in range(5)]).T)
        return np.concatenate(e), np.vstack(J)

    def _dls(self, q, pos, R, w_rot, iters, ori_mode):
        lam = 0.02
        best, stall = 1e9, 0
        for _ in range(iters):
            e, J = self._err_jac(q, pos, R, w_rot, ori_mode)
            if np.linalg.norm(e[:3]) < 2e-4 and (len(e) == 3 or np.linalg.norm(e[3:]) < 2e-3 * w_rot):
                break
            ne = float(np.linalg.norm(e))
            stall = stall + 1 if ne > best - 1e-7 else 0
            best = min(best, ne)
            if stall > 12:  # converged as far as the limits / orientation allow
                break
            step = J.T @ np.linalg.solve(J @ J.T + lam * lam * np.eye(len(e)), e)
            n = np.linalg.norm(step)
            if n > 0.25:
                step *= 0.25 / n
            q = np.clip(q + step, self.lo, self.hi)
        T = self.fk(q)
        pe = float(np.linalg.norm(pos - T[:3, 3]))
        if ori_mode == "full":
            Rr = R.T @ T[:3, :3]
            ae = float(math.acos(max(-1.0, min(1.0, (np.trace(Rr) - 1) / 2))))
        elif ori_mode == "axis":
            ae = float(math.acos(max(-1.0, min(1.0, float(np.dot(T[:3, 2], R[:, 2]))))))
        else:
            ae = 0.0
        return q, (pe, ae)

    # --- trajectories ------------------------------------------------------------------------------------------------
    def duration(self, q0, q1, vscale: float = 0.8, tmin: float = 0.6) -> float:
        """min-jerk duration so no joint exceeds vscale * its max speed (min-jerk peak speed = 1.875 dq / T)."""
        dq = np.abs(np.asarray(q1, float) - np.asarray(q0, float))
        return max(tmin, float(np.max(1.875 * dq / (vscale * self.vmax))))

    def line(self, q_start, p0, p1, R=None, approach=None, roll=None, n=12, ori_mode="full", w_rot=0.25):
        """Joint waypoints for a straight tool move p0 -> p1 (each IK seeded by the previous solution)."""
        out, q = [], np.array(q_start, float)
        for s in np.linspace(0, 1, n):
            p = (1 - s) * np.asarray(p0) + s * np.asarray(p1)
            q, e = self.solve(p, approach=approach, R=R, roll=roll, seeds=[q], ori_mode=ori_mode, w_rot=w_rot)
            out.append((q.copy(), e))
        return out
