"""Record scripted shelf-pick demonstrations of the LeKiwi + SO-101 rover in Isaac Sim (raw frames + joint arrays).

Isaac-python script for the GPU host (arms/sim/DEPLOY.md env: PICKY_ASSET_ROOT + PICKY_ROVER_USD set, repo root as cwd):

    cd /data/$USER/picky/repo-rec && source /data/$USER/picky/rec.env
    $PY -m arms.sim.tools.record_lerobot.record_isaac --out /data/$USER/picky-vla/datasets/raw_shelf_pick_v1 \
        --episodes 60

It builds ONE store (default store_ave) with the per-item models and ONE rover (the visual puppet of picky_rover.usda),
then per episode parks the rover at a slot's approach pose, fixes the lift, and plays ``plan.PickPlanner``'s
trajectory at 30 Hz. Every frame renders two 640x480 RGB cameras (``side``: rides the lift behind-right of the arm;
``wrist``: LeIsaac's wrist camera on the gripper link) with one synchronous replicator step.

Output per episode ``<out>/ep_000123/``: ``side/000000.jpg ...``, ``wrist/000000.jpg ...`` and ``episode.json``
(state / action in LeRobot SO-101 units, timestamps, task, metadata). ``episode.json`` is written last, so a crashed
episode is simply redone on the next run (episodes with an ``episode.json`` are skipped; the sampling is deterministic
per episode index). ``convert_lerobot.py`` turns the raw dir into the LeRobot dataset.

No person ever appears in these frames (an empty sim store), so there is nothing to blur.
"""

import argparse
import json
import math
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from arms.sim.tools.record_lerobot import plan as P

STORE_DEFAULT = "store_ave"
MIN_UNIT_Z_M = 0.55
"""Units lower than this are skipped: with the lift at its bottom the folded-back arm cannot clear the basket rim."""
MAX_WIDTH_M = 0.095
MAX_DEPTH_M = 0.12
MAX_HEIGHT_M = 0.31
FRONT_TOL_M = 0.012
MAX_LATERAL_M = 0.11
MAX_IK_ERROR_M = 0.012
W, H = 640, 480


def _store_to_robot(px: float, py: float, base: tuple[float, float, float]) -> tuple[float, float]:
    bx, by, th = base
    dx, dy = px - bx, py - by
    c, s = math.cos(th), math.sin(th)
    return c * dx + s * dy, -s * dx + c * dy


def _robot_to_store(p: tuple[float, float, float], base: tuple[float, float, float]) -> tuple[float, float, float]:
    bx, by, th = base
    c, s = math.cos(th), math.sin(th)
    return bx + c * p[0] - s * p[1], by + s * p[0] + c * p[1], p[2]


def candidate_units(spec: Any, products: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """sku -> front-row units of graspable size within the arm's lateral range of their slot's approach pose."""
    from utils.data import load_planogram

    plan = load_planogram(spec.store_id)
    slots = {s.slot_id: s for s in plan.slots}
    out: dict[str, list[dict[str, Any]]] = {}
    for inst in spec.instances:
        slot = slots.get(inst.slot_id or "")
        if slot is None or slot.approach_pose is None:
            continue
        w, d, h = spec.item_dims.get(inst.instance_id, (1.0, 1.0, 1.0))
        if w > MAX_WIDTH_M or d > MAX_DEPTH_M or h > MAX_HEIGHT_M or inst.position.z < MIN_UNIT_Z_M:
            continue
        ap = slot.approach_pose
        x, y = _store_to_robot(inst.position.x, inst.position.y, (ap.x, ap.y, ap.theta))
        front = x - d / 2
        ip = slot.item_position
        tag_x, _ = _store_to_robot(ip.x, ip.y, (ap.x, ap.y, ap.theta))
        if abs(front - tag_x) > FRONT_TOL_M or abs(y) > MAX_LATERAL_M:
            continue
        # bottom layer only (stacked units would need the top one first)
        layout = spec.slot_layouts.get(inst.slot_id)
        if layout is not None and inst.position.z > min(p[2] for p in layout.positions) + 1e-3:
            continue
        prod = products.get(inst.sku)
        out.setdefault(inst.sku, []).append(
            {
                "instance_id": inst.instance_id,
                "slot_id": inst.slot_id,
                "sku": inst.sku,
                "name": prod.name if prod else inst.sku,
                "position": (inst.position.x, inst.position.y, inst.position.z),
                "yaw": spec.item_yaw.get(inst.instance_id, 0.0),
                "dims": (w, d, h),
                "approach": (ap.x, ap.y, ap.theta),
            }
        )
    return out


def sample_episode(index: int, seed: int, cands: dict[str, list[dict[str, Any]]]) -> tuple[P.EpisodeSpec, dict]:
    """Deterministic per (seed, index): SKUs round-robin (shuffled once), a random front unit, pose noise."""
    skus = sorted(cands)
    random.Random(seed).shuffle(skus)
    rng = random.Random(seed * 100_003 + index)
    sku = skus[index % len(skus)]
    unit = rng.choice(cands[sku])
    ax, ay, ath = unit["approach"]
    fwd, lat, dyaw = rng.uniform(-0.02, 0.02), rng.uniform(-0.025, 0.025), math.radians(rng.uniform(-3.0, 3.0))
    c, s = math.cos(ath), math.sin(ath)
    base = (ax + c * fwd - s * lat, ay + s * fwd + c * lat, ath + dyaw)
    ux, uy, uz = unit["position"]
    rx, ry = _store_to_robot(ux, uy, base)
    w, d, h = unit["dims"]
    spec = P.EpisodeSpec(
        episode_index=index,
        slot_id=unit["slot_id"],
        sku=sku,
        product_name=unit["name"],
        instance_id=unit["instance_id"],
        unit_robot=(rx, ry, uz),
        unit_dims=(w, d, h),
        unit_yaw_rel=unit["yaw"] - base[2],
        seed=seed * 7919 + index,
        basket_pan_deg=rng.choice((-1.0, 1.0)) * rng.uniform(*P.BASKET_PAN_DEG),
        speed=rng.uniform(0.9, 1.2),
        start_noise_deg={k: rng.uniform(-5.0, 5.0) for k in P.REST_DEG},
        grasp_dz_m=rng.uniform(-0.12, 0.12) * h,
    )
    light = {"dome": rng.uniform(350.0, 950.0), "ambient": rng.uniform(0.05, 0.18), "warm": rng.uniform(-0.06, 0.06)}
    return spec, {"base": base, "unit": unit, "light": light}


def jaw_points(stage: Any, ref_path: str) -> dict[str, list[tuple[float, float, float]]]:
    """Visual mesh points of the gripper + jaw links in each link's own frame (subsampled)."""
    from pxr import Gf, Usd, UsdGeom  # type: ignore[import-not-found]

    xc = UsdGeom.XformCache()
    out: dict[str, list[tuple[float, float, float]]] = {}
    for link in ("gripper", "jaw"):
        lp = stage.GetPrimAtPath(f"{ref_path}/{link}")
        inv = xc.GetLocalToWorldTransform(lp).GetInverse()
        pts: list[tuple[float, float, float]] = []
        for mp in Usd.PrimRange(lp, Usd.TraverseInstanceProxies()):
            if not mp.IsA(UsdGeom.Mesh) or "collision" in str(mp.GetPath()).lower():
                continue
            if UsdGeom.Imageable(mp).ComputePurpose() not in (UsdGeom.Tokens.default_, UsdGeom.Tokens.render):
                continue
            to_link = xc.GetLocalToWorldTransform(mp) * inv
            raw = list(UsdGeom.Mesh(mp).GetPointsAttr().Get() or [])
            for v in raw[:: max(1, len(raw) // 400)]:
                q = to_link.Transform(Gf.Vec3d(v))
                pts.append((q[0], q[1], q[2]))
        out[link] = pts
    return out


class Recorder:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        from arms.sim.app import IsaacBackend, _quiet_kit_logging, require_isaac
        from arms.sim.assets import ASSET_ROOT_ENV, ROVER_USD_ENV, SKU_ASSETS_ENV, SkuAssets, rover_usd_from_env

        SimulationApp = require_isaac()
        self.sim_app = SimulationApp({"headless": True, "width": 1280, "height": 720})
        _quiet_kit_logging()
        self.assets = SkuAssets.from_env(
            {
                ASSET_ROOT_ENV: os.environ.get(ASSET_ROOT_ENV, ""),
                SKU_ASSETS_ENV: os.environ.get(SKU_ASSETS_ENV, ""),
            }
        )
        self.rover_usd = rover_usd_from_env({ROVER_USD_ENV: os.environ.get(ROVER_USD_ENV, "")})
        if not self.rover_usd:
            raise SystemExit("PICKY_ROVER_USD is not set / missing: the recorder needs the LeKiwi + SO-101 rover USD")
        self.backend = IsaacBackend(self.sim_app, label_dir=args.label_dir, assets=self.assets)
        self.stage = self.backend.stage

    def build(self) -> None:
        from arms.sim.scene_spec import build_rover_spec, build_scene_spec
        from arms.sim.worker import default_robots_loader, default_scene_loader

        t0 = time.monotonic()
        plan, products = default_scene_loader(self.args.store)
        self.products = {p.sku: p for p in products}
        self.spec = build_scene_spec(plan, products, seed=0, assets=self.assets)
        self.backend.build_store(self.spec, (0.0, 0.0, 0.0))
        robots, _ = default_robots_loader()
        robot = next((r for r in robots if r.store_id == self.args.store), None)
        if robot is None:
            raise SystemExit(f"no robot for {self.args.store} in data/fixtures/robots.yaml")
        self.rover = build_rover_spec(robot, model_usd=self.rover_usd)
        self.backend.spawn_robot(self.rover)
        self.info = self.backend._robots[robot.robot_id]
        rig = self.info.get("puppet")
        if rig is None:
            raise SystemExit("the rover USD puppet failed (see the log); refusing to record the primitive rover")
        self.rig = rig
        ref_path = self.rover.model_path or f"{self.rover.root_path}/Model/LeKiwi"
        self.planner = P.PickPlanner(rig["geo"], jaw_points(self.stage, ref_path))
        print(f"[rec] store + rover built in {time.monotonic() - t0:.1f}s", flush=True)
        self._make_cameras()

    def _make_cameras(self) -> None:
        import omni.replicator.core as rep  # type: ignore[import-not-found]
        from pxr import Gf, UsdGeom  # type: ignore[import-not-found]

        self.cams: dict[str, tuple[Any, Any]] = {}
        h_ap = 20.955
        specs = {
            "side": (h_ap / (2 * math.tan(math.radians(P.SIDE_HFOV_DEG) / 2)), h_ap),
            "wrist": (P.WRIST_FOCAL_MM, P.WRIST_H_APERTURE_MM),
        }
        UsdGeom.Xform.Define(self.stage, "/World/RecCams")
        for name, (focal, ap) in specs.items():
            path = f"/World/RecCams/{name}"
            c = UsdGeom.Camera.Define(self.stage, path)
            c.CreateHorizontalApertureAttr(ap)
            c.CreateVerticalApertureAttr(ap * H / W)
            c.CreateFocalLengthAttr(focal)
            c.CreateClippingRangeAttr(Gf.Vec2f(0.01, 100.0))
            op = UsdGeom.Xformable(c).AddTransformOp()
            rp_ = rep.create.render_product(path, (W, H))
            ann = rep.AnnotatorRegistry.get_annotator("rgb")
            ann.attach([rp_])
            self.cams[name] = (op, ann)

    # -- posing
    def set_lights(self, light: dict[str, float]) -> None:
        from pxr import Gf, UsdLux  # type: ignore[import-not-found]

        dome = self.stage.GetPrimAtPath("/World/Lights/Dome")
        if dome:
            light_api = UsdLux.DomeLight(dome)
            light_api.CreateIntensityAttr().Set(float(light["dome"]))
            warm = light["warm"]
            light_api.CreateColorAttr().Set(Gf.Vec3f(1.0, 1.0 - abs(warm) * 0.5, 1.0 - warm))
        try:
            import carb.settings  # type: ignore[import-not-found]

            carb.settings.get_settings().set("/rtx/sceneDb/ambientLightIntensity", float(light["ambient"]))
        except Exception:  # noqa: BLE001 - cosmetic
            pass

    def pose_frame(self, ep: P.EpisodePlan, t: int, base: tuple[float, float, float]) -> None:
        from pxr import Gf  # type: ignore[import-not-found]

        from arms.sim.app import gf_rows
        from contracts.common import Point3

        dz = ep.lift_dz
        poses = self.planner.model_poses(ep.q[t], dz)
        for name, op in self.rig["ops"].items():
            op.Set(Gf.Matrix4d(*gf_rows(poses[name])))
        world = P.robot_to_store(*base)
        side = P.rp.mat_mul(world, P.side_camera_matrix(self.planner, dz))
        wrist = P.rp.mat_mul(world, P.wrist_camera_matrix(self.planner, ep.q[t], dz))
        self.cams["side"][0].Set(Gf.Matrix4d(*gf_rows(side)))
        self.cams["wrist"][0].Set(Gf.Matrix4d(*gf_rows(wrist)))
        u = ep.unit[t]
        if u is not None:
            (px, py, pz), yaw_rel = u
            sx, sy, sz = _robot_to_store((px, py, pz), base)
            self.backend.set_instance_pose(
                self.args.store, ep.spec.instance_id, Point3(x=sx, y=sy, z=sz), base[2] + yaw_rel
            )

    def place_rover(self, base: tuple[float, float, float], dz: float) -> None:
        from pxr import Gf  # type: ignore[import-not-found]

        self.info["xf"].SetTranslate(Gf.Vec3d(base[0], base[1], 0.0))
        self.info["xf"].SetRotate(Gf.Vec3f(0.0, 0.0, math.degrees(base[2])))
        self.info["lift"].Set(Gf.Vec3d(0.0, 0.0, dz))

    def step(self, subframes: int) -> None:
        import omni.replicator.core as rep  # type: ignore[import-not-found]

        rep.orchestrator.step(rt_subframes=subframes, pause_timeline=True)

    def grab(self) -> dict[str, Any]:
        out = {}
        for name, (_, ann) in self.cams.items():
            data = ann.get_data()
            if data is None or getattr(data, "size", 0) == 0:
                raise RuntimeError(f"camera {name}: empty annotator frame")
            out[name] = data[:, :, :3].copy()
        return out

    # -- episodes
    def run(self) -> int:
        from PIL import Image

        args = self.args
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        cands = candidate_units(self.spec, self.products)
        n_units = sum(len(v) for v in cands.values())
        print(f"[rec] {len(cands)} SKUs / {n_units} graspable front units: {sorted(cands)}", flush=True)
        if len(cands) < 1:
            raise SystemExit("no graspable units")
        (out / "candidates.json").write_text(json.dumps({k: [u["instance_id"] for u in v] for k, v in cands.items()}))
        pool = ThreadPoolExecutor(max_workers=6)
        done = 0
        warmed = False
        t_all = time.monotonic()
        for index in range(args.start, args.start + args.episodes):
            ep_dir = out / f"ep_{index:06d}"
            if (ep_dir / "episode.json").exists():
                done += 1
                continue
            spec, extra = sample_episode(index, args.seed, cands)
            plan = self.planner.plan(spec)
            tries = 0
            while plan.max_ik_error_m > MAX_IK_ERROR_M and tries < 5:
                tries += 1
                spec, extra = sample_episode(index + 1000 * tries, args.seed, cands)
                spec.episode_index = index
                plan = self.planner.plan(spec)
            if plan.max_ik_error_m > MAX_IK_ERROR_M:
                print(f"[rec] ep {index}: IK error {plan.max_ik_error_m * 1000:.0f} mm, skipped", flush=True)
                continue
            base = extra["base"]
            unit = extra["unit"]
            self.place_rover(base, plan.lift_dz)
            self.set_lights(extra["light"])
            frames = (
                range(len(plan.q))
                if not args.probe
                else sorted({0, plan.grasp_frame - 20, plan.grasp_frame, plan.release_frame - 10, len(plan.q) - 1})
            )
            (ep_dir / "side").mkdir(parents=True, exist_ok=True)
            (ep_dir / "wrist").mkdir(parents=True, exist_ok=True)
            self.pose_frame(plan, 0, base)
            for _ in range(args.warmup if not warmed else args.preroll):
                self.step(2)
            warmed = True
            t0 = time.monotonic()
            futs = []
            for t in frames:
                self.pose_frame(plan, t, base)
                self.step(args.subframes)
                imgs = self.grab()
                for name, arr in imgs.items():
                    futs.append(
                        pool.submit(
                            lambda a, p: Image.fromarray(a).save(p, quality=args.jpeg_quality),
                            arr,
                            ep_dir / name / f"{t:06d}.jpg",
                        )
                    )
            for f in futs:
                f.result()
            dt = time.monotonic() - t0
            # put the unit back on its shelf spot
            from contracts.common import Point3

            ux, uy, uz = unit["position"]
            self.backend.set_instance_pose(args.store, spec.instance_id, Point3(x=ux, y=uy, z=uz), unit["yaw"])
            meta = {
                "episode_index": index,
                "task": spec.task,
                "sku": spec.sku,
                "product_name": spec.product_name,
                "slot_id": spec.slot_id,
                "instance_id": spec.instance_id,
                "store_id": args.store,
                "fps": P.FPS,
                "n_frames": len(plan.q),
                "frames": list(frames),
                "feature_names": P.FEATURE_NAMES,
                "state": plan.states,
                "action": plan.actions,
                "timestamp": [round(i / P.FPS, 6) for i in range(len(plan.q))],
                "phases": plan.phases,
                "grasp_frame": plan.grasp_frame,
                "release_frame": plan.release_frame,
                "lift_dz_m": round(plan.lift_dz, 4),
                "base_pose": [round(v, 4) for v in base],
                "unit_robot": [round(v, 4) for v in spec.unit_robot],
                "unit_dims": list(spec.unit_dims),
                "light": extra["light"],
                "max_ik_error_m": round(plan.max_ik_error_m, 5),
                "render_s": round(dt, 2),
                "probe": bool(args.probe),
            }
            tmp = ep_dir / "episode.json.tmp"
            tmp.write_text(json.dumps(meta))
            tmp.rename(ep_dir / "episode.json")
            done += 1
            print(
                f"[rec] ep {index}: {spec.sku} {spec.slot_id} frames={len(plan.q)} lift={plan.lift_dz:.2f} "
                f"ik={plan.max_ik_error_m * 1000:.1f}mm render {dt:.1f}s ({len(list(frames)) / max(dt, 1e-6):.1f} fps) "
                f"total {time.monotonic() - t_all:.0f}s",
                flush=True,
            )
        pool.shutdown()
        return done


def main(argv: list[str] | None = None) -> int:
    from utils.config import get_settings  # before SimulationApp: Kit's typing-extensions breaks a later first import

    get_settings()
    ap = argparse.ArgumentParser(prog="python -m arms.sim.tools.record_lerobot.record_isaac")
    ap.add_argument("--out", required=True, help="raw dataset dir (episodes go to <out>/ep_XXXXXX)")
    ap.add_argument("--store", default=STORE_DEFAULT)
    ap.add_argument("--episodes", type=int, default=60)
    ap.add_argument("--start", type=int, default=0, help="first episode index")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--subframes", type=int, default=1, help="RTX subframes per recorded frame")
    ap.add_argument("--warmup", type=int, default=40, help="replicator steps before the first episode (textures)")
    ap.add_argument("--preroll", type=int, default=4, help="steps after moving the rover before recording")
    ap.add_argument("--jpeg-quality", type=int, default=95)
    ap.add_argument("--label-dir", default=os.environ.get("PICKY_SIM_LABEL_DIR", "/tmp/picky-sim-labels"))
    ap.add_argument("--probe", action="store_true", help="render only 5 key frames per episode (camera check)")
    args = ap.parse_args(argv)
    rec = Recorder(args)
    try:
        rec.build()
        n = rec.run()
        print(f"[rec] done: {n} episodes in {args.out}", flush=True)
    finally:
        rec.sim_app.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
