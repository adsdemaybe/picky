"""Minimal URDF reader + forward kinematics (numpy only), used by build_so101_rover.py.

Reads links, visuals (mesh file + origin + material colour), and joints (origin, axis, limits, type) and computes
every link's pose in the root frame for given joint values. No ROS / urdfpy dependency.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np


def rpy_matrix(r: float, p: float, y: float) -> np.ndarray:
    """URDF fixed-axis roll-pitch-yaw: R = Rz(y) @ Ry(p) @ Rx(r)."""
    cr, sr, cp, sp, cy, sy = math.cos(r), math.sin(r), math.cos(p), math.sin(p), math.cos(y), math.sin(y)
    return np.array(
        [
            [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
            [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
            [-sp, cp * sr, cp * cr],
        ]
    )


def origin_matrix(el) -> np.ndarray:
    T = np.eye(4)
    if el is None:
        return T
    xyz = [float(v) for v in el.get("xyz", "0 0 0").split()]
    rpy = [float(v) for v in el.get("rpy", "0 0 0").split()]
    T[:3, :3] = rpy_matrix(*rpy)
    T[:3, 3] = xyz
    return T


def axis_angle(axis, angle: float) -> np.ndarray:
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    K = np.array([[0, -a[2], a[1]], [a[2], 0, -a[0]], [-a[1], a[0], 0]])
    R = np.eye(3) + math.sin(angle) * K + (1 - math.cos(angle)) * K @ K
    T = np.eye(4)
    T[:3, :3] = R
    return T


class Urdf:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        root = ET.parse(self.path).getroot()
        self.name = root.get("name")
        colors = {}
        for m in root.findall("material"):
            c = m.find("color")
            if c is not None:
                colors[m.get("name")] = [float(v) for v in c.get("rgba").split()]
        self.links = [lk.get("name") for lk in root.findall("link")]
        self.visuals: dict[str, list[dict]] = {}
        self.inertial: dict[str, dict] = {}
        for lk in root.findall("link"):
            vis = []
            for v in lk.findall("visual"):
                mesh = v.find("geometry/mesh")
                if mesh is None:
                    continue
                mat = v.find("material")
                rgba = None
                if mat is not None:
                    c = mat.find("color")
                    rgba = [float(x) for x in c.get("rgba").split()] if c is not None else colors.get(mat.get("name"))
                vis.append(
                    {
                        "file": mesh.get("filename"),
                        "scale": [float(x) for x in mesh.get("scale", "1 1 1").split()],
                        "T": origin_matrix(v.find("origin")),
                        "material": mat.get("name") if mat is not None else None,
                        "rgba": rgba,
                    }
                )
            self.visuals[lk.get("name")] = vis
            inn = lk.find("inertial")
            if inn is not None:
                Iner = inn.find("inertia")
                self.inertial[lk.get("name")] = {
                    "mass": float(inn.find("mass").get("value")),
                    "com": origin_matrix(inn.find("origin"))[:3, 3],
                    "Iner": [float(Iner.get(k, 0)) for k in ("ixx", "iyy", "izz", "ixy", "ixz", "iyz")]
                    if Iner is not None
                    else None,
                }
        self.joints: dict[str, dict] = {}
        for j in root.findall("joint"):
            lim = j.find("limit")
            ax = j.find("axis")
            self.joints[j.get("name")] = {
                "name": j.get("name"),
                "type": j.get("type"),
                "parent": j.find("parent").get("link"),
                "child": j.find("child").get("link"),
                "T": origin_matrix(j.find("origin")),
                "axis": [float(v) for v in ax.get("xyz").split()] if ax is not None else [1.0, 0.0, 0.0],
                "lower": float(lim.get("lower")) if lim is not None and lim.get("lower") else None,
                "upper": float(lim.get("upper")) if lim is not None and lim.get("upper") else None,
            }
        children = {j["child"] for j in self.joints.values()}
        roots = [lk for lk in self.links if lk not in children]
        self.root = roots[0]
        self.child_joints: dict[str, list[dict]] = {}
        for j in self.joints.values():
            self.child_joints.setdefault(j["parent"], []).append(j)

    def mesh_path(self, filename: str) -> Path:
        f = filename.replace("package://", "")
        return (self.path.parent / f).resolve()

    def fk(self, q: dict[str, float] | None = None, root_T: np.ndarray | None = None, skip=()) -> dict[str, np.ndarray]:
        """Pose of every link in the root frame. q: joint name -> position (rad / m). skip: links not to descend."""
        q = q or {}
        out = {self.root: np.eye(4) if root_T is None else root_T}
        stack = [self.root]
        while stack:
            p = stack.pop()
            for j in self.child_joints.get(p, []):
                if j["child"] in skip:
                    continue
                M = out[p] @ j["T"]
                v = q.get(j["name"], 0.0)
                if j["type"] in ("revolute", "continuous") and v:
                    M = M @ axis_angle(j["axis"], v)
                elif j["type"] == "prismatic" and v:
                    t = np.eye(4)
                    t[:3, 3] = np.asarray(j["axis"]) * v
                    M = M @ t
                out[j["child"]] = M
                stack.append(j["child"])
        return out

    def subtree(self, link: str) -> list[str]:
        out, stack = [], [link]
        while stack:
            lk = stack.pop()
            out.append(lk)
            stack += [j["child"] for j in self.child_joints.get(lk, [])]
        return out
