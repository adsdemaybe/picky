"""Top-down SO-101 IK (gripper pointing down) on #19's ArmChain, multi-start. Pure python."""

import itertools
import math

from arms.sim import rover_puppet as puppet

NAMES = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex"]


def _solve(ch, mi, m, target_robot, lift_dz, creep, seed, down_w=0.25, iters=300):
    x, y, z = target_robot
    tgt = puppet.apply(mi, (x - creep, y, z))
    base = puppet.robot_delta_in_model(m, (0.0, 0.0, lift_dz))
    dv = puppet.rotate(mi, (0.0, 0.0, -1.0))
    q = dict(seed)

    def r_(qq):
        p = ch.fk(qq, base)
        t = ch.tcp_point(p)
        w = puppet.position(p["wrist"])
        v = [t[i] - w[i] for i in range(3)]
        n = math.sqrt(sum(a * a for a in v)) or 1.0
        return [t[i] - tgt[i] for i in range(3)] + [down_w * (v[i] / n - dv[i]) for i in range(3)]

    lam, eps = 1e-3, 1e-6
    r = r_(q)
    c = sum(a * a for a in r)
    for _ in range(iters):
        cols = []
        for nm in NAMES:
            qp = dict(q)
            qp[nm] += eps
            rp = r_(qp)
            cols.append([(rp[i] - r[i]) / eps for i in range(len(r))])
        jtj = [
            [sum(cols[a][i] * cols[b][i] for i in range(len(r))) + (lam if a == b else 0) for b in range(4)]
            for a in range(4)
        ]
        jtr = [-sum(cols[a][i] * r[i] for i in range(len(r))) for a in range(4)]
        dq = puppet._solve(jtj, jtr)
        tr = dict(q)
        for nm, dd in zip(NAMES, dq, strict=False):
            tr[nm] = ch.joint(nm).clamp(tr[nm] + max(-0.3, min(0.3, dd)))
        rt = r_(tr)
        ct = sum(a * a for a in rt)
        if ct < c:
            q, r, c, lam = tr, rt, ct, max(1e-8, lam * 0.3)
        else:
            lam *= 10
            if lam > 1e6:
                break
    p = ch.fk(q, base)
    return q, math.dist(ch.tcp_point(p), tgt)


def solve_down(ch, m, target_robot, lift_dz, creep, roll, grip, prev=None):
    """Best of a seed grid (+ the previous solution, for continuity)."""
    mi = puppet.inverse(m)
    seeds = [dict(prev)] if prev else []
    for sl, ef, wf in itertools.product([-1.2, -0.6, 0.0, 0.6, 1.0], [-1.2, -0.6, 0.0, 0.8], [-1.2, 0.0, 1.2, 1.6]):
        seeds.append({"shoulder_pan": 0.0, "shoulder_lift": sl, "elbow_flex": ef, "wrist_flex": wf})
    best = None
    for s in seeds:
        s = dict(s, wrist_roll=roll, gripper=grip)
        q, err = _solve(ch, mi, m, target_robot, lift_dz, creep, s)
        jump = sum(abs(q[n] - prev[n]) for n in NAMES) if prev else 0.0
        key = err + 0.002 * jump  # prefer continuity among equally good solutions
        if best is None or key < best[0]:
            best = (key, q, err)
    q = dict(best[1], wrist_roll=roll, gripper=grip)
    return q, best[2]
