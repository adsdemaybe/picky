"""Reach + torque check for the long-reach SO-101 rover (exact 3-D IK on the built chain, numpy only).

For shelf heights 0.30..1.50 m and standoffs 0.35/0.40/0.45 m (horizontal distance from the pan axis to the grasp
point, straight ahead of the robot) it solves the grasp pose with so101_ik.py: approach horizontal (+-20 deg) from
0.50 m up, pitched down by up to 40 deg below that (bottom shelf), jaws horizontal (grasp roll). A solution counts only
if the arm stays clear of the base top / riser / basket (link sample points vs. simple solids). Also checks the basket
drop (gripper pointing down 12 cm above the rim) and prints the static torque budget from <robot>.kin.json.

    python reach_check.py picky_so101_rover.kin.json [--json out.json]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from so101_ik import RoverKinematics  # noqa: E402


def clear_of_body(kin: RoverKinematics, q, k: dict, margin: float = 0.03) -> bool:
    """elbow / forearm / wrist / gripper sample points outside the base (+top), riser and basket volumes."""
    L = kin.links(q)
    pts = []
    chain = [
        L["so101_upper_arm"][:3, 3],
        L["so101_lower_arm"][:3, 3],
        L["so101_wrist"][:3, 3],
        L["so101_gripper"][:3, 3],
        L["tcp"][:3, 3],
    ]
    for a, b in zip(chain[:-1], chain[1:], strict=False):
        for s in np.linspace(0, 1, 12):
            pts.append(a + (b - a) * s)
    P = np.array(pts)
    r_base = k["footprint_radius"]
    top = k["riser"]["top_z"] - k["riser"]["height"]
    bk = k["basket"]
    rx = k["riser"]["x"]
    for p in P[3:]:  # skip the first samples (they are the shoulder itself, on top of the riser)
        if math.hypot(p[0], p[1]) < r_base + 0.01 and p[2] < top + margin:
            return False
        if math.hypot(p[0] - rx, p[1]) < k["riser"]["radius"] + margin and p[2] < k["riser"]["top_z"] - 0.02:
            return False
        if (
            math.hypot(p[0] - bk["center"][0], p[1] - bk["center"][1]) < bk["r_top"] + 0.01
            and p[2] < bk["rim_z"] + 0.01
        ):
            return False
    return True


def shelf_grasp(kin: RoverKinematics, k: dict, d: float, z: float):
    px, py = k["pan_axis_xy"]
    pos = np.array([px + d, py, z])
    tilts = sorted(range(-20, 21, 5), key=abs) if z >= 0.5 else list(range(0, 41, 5))
    for t in tilts:
        a = math.radians(t)
        appr = np.array([math.cos(a), 0.0, -math.sin(a)])  # +t = tool pitched down
        q, (pe, ae) = kin.solve(pos, approach=appr, roll=kin.grasp_roll, w_rot=0.12)
        if pe < 0.004 and ae < math.radians(5) and clear_of_body(kin, q, k):
            return {"tilt_deg": int(t), "q": np.round(q, 3).tolist(), "pos_err": round(pe, 4)}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("kin")
    ap.add_argument("--json")
    args = ap.parse_args()
    k = json.loads(Path(args.kin).read_text())
    kin = RoverKinematics(k)
    ds = (0.35, 0.40, 0.45)
    zs = np.round(np.arange(0.30, 1.501, 0.10), 2)
    table, ok = {}, 0
    print(
        f"shoulder axis z {k['shoulder_z']:.3f} m, riser {k['riser']['height']:.3f} m, upper arm "
        f"{k['extension']['upper_arm_m']:.3f} m, forearm {k['extension']['forearm_m']:.3f} m"
    )
    print("height  " + "  ".join(f"d={d:.2f}" for d in ds) + "   (tool pitch deg, + = down; '--' = unreachable)")
    for z in zs:
        row = []
        for d in ds:
            r = shelf_grasp(kin, k, d, float(z))
            table[f"{d:.2f},{z:.2f}"] = r
            ok += r is not None
            row.append(f"{r['tilt_deg']:+5d}  " if r else "   -- ")
        print(f"{z:5.2f}   " + "  ".join(row))
    # basket drop: gripper down, 12 cm above the rim at the basket centre (pan +90 deg)
    bk = k["basket"]
    drop = np.array([bk["center"][0], bk["center"][1], bk["rim_z"] + 0.12])
    best = None
    for pan in (1.5708, 1.3, 1.8):
        for s in ([pan, -0.6, 1.6, 0.6, 0.0], [pan, -0.2, 1.2, 0.9, 0.0], [pan, -1.0, 1.65, 0.2, 0.0]):
            q, (pe, ae) = kin.solve(drop, approach=(0, 0, -1), ori_mode="axis", seeds=[s])
            if best is None or pe + 0.05 * ae < best[1][0] + 0.05 * best[1][1]:
                best = (q, (pe, ae))
    q, (pe, ae) = best
    print(
        f"basket drop at {np.round(drop, 3)}: pos err {pe * 1000:.1f} mm, axis err {math.degrees(ae):.1f} deg, "
        f"q {np.round(q, 3)}"
    )
    print(f"shelf targets reachable: {ok}/{len(zs) * len(ds)}")
    print(f"static torque, arm stretched horizontally + {k['payload_kg']:.1f} kg at the tool point:")
    for jn, t in k["torque_budget"].items():
        a = k["actuators"][jn]
        print(
            f"  {jn:14s} required {t['required_Nm']:5.2f} N*m (arm {t['arm_only_Nm']:.2f} + item {t['payload_Nm']:.2f})"
            f"  available {t['available_Nm']:5.2f} N*m [{a['servo']}, {a['ratio']}:1]  SF {t['safety_factor']:.2f}"
        )
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"shelf": table, "basket_drop": {"q": np.round(q, 4).tolist(), "pos_err": pe, "ang_err": ae}}, indent=1
            )
        )


if __name__ == "__main__":
    main()
