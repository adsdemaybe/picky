"""Precompute AIRBOT Play IK tables for the --demo autopilot (MuJoCo, same model as the Isaac robot).

The app's presets (H/N/B) were solved the same way; the demo needs a family of them so the gripper can line up with a
product at any shelf height and slide straight in. Two tables, both in the robot base frame (x forward, z up):

  front: tool point at (x, ARM_Y, z), gripper pointing +x, fingers closing sideways (y)   -> shelf grasps / counter
  down:  tool point at (x, y, z) inside the basket, gripper roughly down (position only)  -> unloading the basket

The tool point is the centre between the fingertips (TCP_OFFSET along the approach axis from the arm_link6 origin).

    python robot/solve_arm_ik.py build/rm2_basket/rm2_basket.xml arm_ik.json
"""

from __future__ import annotations

import json
import sys

import mujoco
import numpy as np

ARM = ["arm_joint1", "arm_joint2", "arm_joint3", "arm_joint4", "arm_joint5", "gripper_base_joint"]
HOME = [0.0, -0.431, 0.853, 1.571, -1.859, 0.251]
FRONT = [0.121, -1.254, 0.694, 2.559, 0.297, -1.626]
BASKET = [-0.48, -0.69, 3.16, -1.571, -1.859, -1.4]
ARM_Y = -0.17  # arm base y in the base frame: the base lines the product up with the arm, so y is fixed
FRONT_X = np.round(np.arange(0.45, 0.901, 0.025), 3)
FRONT_Z = np.round(np.arange(0.35, 1.651, 0.025), 3)
DOWN_X = np.round(np.arange(-0.29, 0.051, 0.04), 3)
DOWN_Y = np.round(np.arange(-0.10, 0.181, 0.04), 3)
DOWN_Z = np.round(np.arange(0.90, 1.301, 0.05), 3)


def main(xml: str, out: str) -> None:
    m = mujoco.MjModel.from_xml_path(xml)
    d = mujoco.MjData(m)
    jid = [m.joint(n).id for n in ARM]
    qadr, dadr = m.jnt_qposadr[jid], m.jnt_dofadr[jid]
    lo, hi = m.jnt_range[jid, 0], m.jnt_range[jid, 1]
    link6 = m.body("arm_link6").id
    fbody = m.body("finger_right_link").id

    def fk(q):
        d.qpos[:] = 0
        d.qpos[3] = 1.0
        d.qpos[qadr] = q
        mujoco.mj_kinematics(m, d)
        mujoco.mj_comPos(m, d)
        return d.xpos[link6].copy(), d.xmat[link6].reshape(3, 3).copy()

    # approach axis = the link6 axis that points down in the home pose ("gripper down"); finger axis from the model
    _, r_home = fk(HOME)
    a_idx = int(np.argmin(r_home[2]))  # most negative world-z component
    a_loc = np.zeros(3)
    a_loc[a_idx] = 1.0
    f_loc = r_home.T @ d.xmat[fbody].reshape(3, 3)[:, 1]
    # fingertip centre: finger geoms' mesh vertices in link6 frame, extent along the approach axis
    tips = []
    for g in range(m.ngeom):
        if m.geom_bodyid[g] in (fbody, m.body("finger_left_link").id) and m.geom_type[g] == mujoco.mjtGeom.mjGEOM_MESH:
            mid = m.geom_dataid[g]
            v = m.mesh_vert[m.mesh_vertadr[mid] : m.mesh_vertadr[mid] + m.mesh_vertnum[mid]]
            rg = d.geom_xmat[g].reshape(3, 3)
            w = v @ rg.T + d.geom_xpos[g]
            p6, r6 = d.xpos[link6], d.xmat[link6].reshape(3, 3)
            tips.append(((w - p6) @ r6)[:, a_idx])
    tip = float(np.max(np.concatenate(tips))) if tips else 0.0
    tcp = max(0.0, tip - 0.035)  # grasp ~3.5 cm back from the very tip
    print(
        f"approach axis = link6 local {a_loc}, finger axis local {np.round(f_loc, 3)}, tip {tip:.3f} m, tcp {tcp:.3f}"
    )

    def tcp_of(q):
        p, r = fk(q)
        return p + r @ a_loc * tcp, r

    for name, q in (("home", HOME), ("front", FRONT), ("basket", BASKET)):
        p, r = tcp_of(q)
        print(f"preset {name}: tcp {np.round(p, 3)} approach {np.round(r @ a_loc, 2)} fingers {np.round(r @ f_loc, 2)}")

    jp, jr = np.zeros((3, m.nv)), np.zeros((3, m.nv))

    def solve(q0, p_t, a_t, f_t, w_f=1.0, iters=300, tol_deg=6.0, w_a=1.0):
        q = np.array(q0, dtype=float)
        for _ in range(iters):
            p, r = tcp_of(q)
            a, f = r @ a_loc, r @ f_loc
            s = 1.0 if f_t is None or f @ f_t >= 0 else -1.0
            e_p = p_t - p
            e_r = (w_a * np.cross(a, a_t) if a_t is not None else np.zeros(3)) + (
                w_f * np.cross(f, s * f_t) if f_t is not None else 0.0
            )
            if np.linalg.norm(e_p) < 0.002 and np.linalg.norm(e_r) < 0.02 * max(w_a, 0.05):
                return q, True
            mujoco.mj_jac(m, d, jp, jr, p, link6)
            j = np.vstack([jp[:, dadr], 0.5 * jr[:, dadr]])
            e = np.concatenate([e_p, 0.5 * e_r])
            dq = j.T @ np.linalg.solve(j @ j.T + 1e-3 * np.eye(6), e)
            q = np.clip(q + np.clip(dq, -0.2, 0.2), lo, hi)
        p, r = tcp_of(q)
        tilt = 0.0 if a_t is None else np.degrees(np.arccos(np.clip((r @ a_loc) @ a_t, -1, 1)))
        ok = np.linalg.norm(p_t - p) < 0.01 and tilt < tol_deg
        return q, bool(ok)

    rng = np.random.default_rng(0)
    restarts = [lo + (hi - lo) * rng.random(6) for _ in range(6)]

    def table(xs, ys, zs, a_t, f_t, seed, w_f, tol_deg=6.0, w_a=1.0):
        # neighbours seed each solve (continuation) so adjacent entries stay on one IK branch and can be interpolated
        grid, okc = {}, 0
        seeds = {}
        dx = float(xs[1] - xs[0]) if len(xs) > 1 else 0.0
        dy = float(ys[1] - ys[0]) if len(ys) > 1 else 0.0
        dz = float(zs[1] - zs[0])
        for x in xs:
            for y in ys:
                for z in zs:
                    keys = [(x, y, round(z - dz, 3)), (round(x - dx, 3), y, z), (x, round(y - dy, 3), z)]
                    near = [seeds[k] for k in keys if k in seeds]
                    best = None
                    for s0 in [*near, seed, FRONT, HOME, *restarts]:
                        q, ok = solve(s0, np.array([x, y, z]), a_t, f_t, w_f, tol_deg=tol_deg, w_a=w_a)
                        if ok and (not near or np.abs(q - near[0]).max() < 0.8 or s0 is restarts[-1]):
                            best = q
                            break
                    if best is not None:
                        seeds[(x, y, z)] = best
                        grid[f"{x:.3f},{y:.3f},{z:.3f}"] = [round(float(v), 4) for v in best]
                        okc += 1
        # fill holes: retry missing points seeded from any solved neighbour until nothing new is found
        steps = [(dx, 0, 0), (-dx, 0, 0), (0, dy, 0), (0, -dy, 0), (0, 0, dz), (0, 0, -dz)]
        changed = True
        while changed:
            changed = False
            for x in xs:
                for y in ys:
                    for z in zs:
                        if (x, y, z) in seeds:
                            continue
                        for sx, sy, sz in steps:
                            k = (round(x + sx, 3), round(y + sy, 3), round(z + sz, 3))
                            if k not in seeds:
                                continue
                            q, ok = solve(seeds[k], np.array([x, y, z]), a_t, f_t, w_f, tol_deg=tol_deg, w_a=w_a)
                            if ok and np.abs(q - seeds[k]).max() < 0.8:
                                seeds[(x, y, z)] = q
                                grid[f"{x:.3f},{y:.3f},{z:.3f}"] = [round(float(v), 4) for v in q]
                                okc += 1
                                changed = True
                                break
        print(f"table: {okc}/{len(xs) * len(ys) * len(zs)} reachable")
        return grid

    front = table(FRONT_X, [ARM_Y], FRONT_Z, np.array([1.0, 0, 0]), np.array([0, 1.0, 0]), FRONT, 1.0)
    # over the basket the arm cannot point straight down (it is mounted right next to it): position only, seeded
    # from the B preset (gripper mostly down) so the solutions stay on that branch
    down = table(DOWN_X, DOWN_Y, DOWN_Z, None, None, BASKET, 0.0)
    json.dump(
        {
            "tcp_offset": tcp,
            "approach_axis_link6": a_loc.tolist(),
            "arm_y": ARM_Y,
            "front": {"x": FRONT_X.tolist(), "z": FRONT_Z.tolist(), "q": front},
            "down": {"x": DOWN_X.tolist(), "y": DOWN_Y.tolist(), "z": DOWN_Z.tolist(), "q": down},
        },
        open(out, "w"),
    )
    print(f"wrote {out}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
