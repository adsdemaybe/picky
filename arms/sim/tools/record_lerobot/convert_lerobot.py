"""Raw Isaac recordings -> LeRobot dataset (Perceptron SO100/SO101 format) + validation + previews.

Runs in a Python env with ``lerobot[dataset]==0.6.1`` (+ PyAV, Pillow, numpy); NOT in Isaac's Python and NOT in the
repo's base env (lerobot is a heavy, arm-local tool dependency). On vhagar:

    V=/data/$USER/picky-vla/lerobot-ds/.venv/bin/python
    $V arms/sim/tools/record_lerobot/convert_lerobot.py convert \
        --raw /data/$USER/picky-vla/datasets/raw_shelf_pick_v1 --out /data/$USER/picky-vla/datasets/picky_shelf_pick_v1
    $V arms/sim/tools/record_lerobot/convert_lerobot.py validate \
        --root /data/$USER/picky-vla/datasets/picky_shelf_pick_v1
    $V arms/sim/tools/record_lerobot/convert_lerobot.py preview --raw ... --out-dir previews/ --episodes 0 7
    $V arms/sim/tools/record_lerobot/convert_lerobot.py sheet --root ... --out-dir previews/ --episodes 0 59

Self-contained on purpose (no repo imports): the raw ``episode.json`` already carries LeRobot units.

Perceptron's recording checklist (docs: robotics.perceptron.inc/docs), checked by ``validate``:
30 FPS; RGB side + wrist 640x480 named observation.images.side / observation.images.wrist; 6 state + action values in
order shoulder_pan.pos, shoulder_lift.pos, elbow_flex.pos, wrist_flex.pos, wrist_roll.pos, gripper.pos (arm degrees,
gripper 0-100); LeRobot v2.0 / v2.1 / v3.0; meta/info.json + episode metadata + data + both videos + timestamps +
episode boundaries consistent; >= 1 train + >= 1 validation episode.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

FPS = 30
W, H = 640, 480
NAMES = ["shoulder_pan.pos", "shoulder_lift.pos", "elbow_flex.pos", "wrist_flex.pos", "wrist_roll.pos", "gripper.pos"]
CAMERAS = ("side", "wrist")
ARM_LIMITS = [(-110.0, 110.0), (-100.0, 100.0), (-100.0, 90.0), (-95.0, 95.0), (-160.0, 160.0), (0.0, 100.0)]
SUPPORTED_VERSIONS = ("v2.0", "v2.1", "v3.0")
REPO_ID = "picky/picky_shelf_pick_v1"


def raw_episodes(raw: Path) -> list[tuple[Path, dict]]:
    out = []
    for ep_dir in sorted(raw.glob("ep_*")):
        meta_p = ep_dir / "episode.json"
        if not meta_p.exists():
            continue
        meta = json.loads(meta_p.read_text())
        if meta.get("probe"):
            continue
        n = meta["n_frames"]
        if any(not (ep_dir / cam / f"{n - 1:06d}.jpg").exists() for cam in CAMERAS):
            print(f"skip {ep_dir.name}: missing frames", file=sys.stderr)
            continue
        out.append((ep_dir, meta))
    return out


def split_order(eps: list[tuple[Path, dict]], val_every: int) -> tuple[list[tuple[Path, dict]], int]:
    """Train episodes first, then validation (every ``val_every``-th raw episode, at least one)."""
    val = [e for i, e in enumerate(eps) if i % val_every == val_every - 1]
    if not val and len(eps) >= 2:
        val = [eps[-1]]
    train = [e for e in eps if e not in val]
    return train + val, len(train)


def features() -> dict:
    vid = {"dtype": "video", "shape": (H, W, 3), "names": ["height", "width", "channels"]}
    vec = {"dtype": "float32", "shape": (6,), "names": NAMES}
    return {
        "observation.images.side": dict(vid),
        "observation.images.wrist": dict(vid),
        "observation.state": dict(vec),
        "action": dict(vec),
    }


def convert(args: argparse.Namespace) -> int:
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from PIL import Image

    raw, out = Path(args.raw), Path(args.out)
    eps = raw_episodes(raw)
    if args.limit:
        eps = eps[: args.limit]
    if len(eps) < 2:
        print(f"need >= 2 complete episodes in {raw}, found {len(eps)}", file=sys.stderr)
        return 1
    ordered, n_train = split_order(eps, args.val_every)
    if out.exists():
        if not args.overwrite:
            print(f"{out} exists (use --overwrite)", file=sys.stderr)
            return 1
        shutil.rmtree(out)
    ds = LeRobotDataset.create(
        repo_id=args.repo_id,
        fps=FPS,
        root=out,
        robot_type="so101_follower",
        features=features(),
        use_videos=True,
        image_writer_threads=8,
    )
    episode_map = []
    for k, (ep_dir, meta) in enumerate(ordered):
        n = meta["n_frames"]
        # clamp to the USD joint limits exactly like the puppet's FK did when the frame was rendered (raw episodes
        # recorded before plan.py clamped its start pose carry up to 5 deg of noise past the rest-pose limits)
        lo = np.asarray([a for a, _ in ARM_LIMITS], dtype=np.float32)
        hi = np.asarray([b for _, b in ARM_LIMITS], dtype=np.float32)
        state = np.clip(np.asarray(meta["state"], dtype=np.float32), lo, hi)
        action = np.clip(np.asarray(meta["action"], dtype=np.float32), lo, hi)
        assert state.shape == (n, 6) and action.shape == (n, 6), ep_dir
        for t in range(n):
            frame = {
                "observation.state": state[t],
                "action": action[t],
                "task": meta["task"],
            }
            for cam in CAMERAS:
                img = np.asarray(Image.open(ep_dir / cam / f"{t:06d}.jpg").convert("RGB"))
                assert img.shape == (H, W, 3), (ep_dir, cam, img.shape)
                frame[f"observation.images.{cam}"] = img
            ds.add_frame(frame)
        ds.save_episode()
        episode_map.append(
            {
                "episode_index": k,
                "raw": ep_dir.name,
                "split": "train" if k < n_train else "validation",
                "task": meta["task"],
                "sku": meta["sku"],
                "slot_id": meta["slot_id"],
                "n_frames": n,
                "grasp_frame": meta.get("grasp_frame"),
                "release_frame": meta.get("release_frame"),
            }
        )
        print(f"episode {k} <- {ep_dir.name} ({n} frames, {meta['sku']})", flush=True)
    if hasattr(ds, "finalize"):
        ds.finalize()
    info_p = out / "meta" / "info.json"
    info = json.loads(info_p.read_text())
    n = len(ordered)
    info["splits"] = {"train": f"0:{n_train}", "validation": f"{n_train}:{n}"}
    info_p.write_text(json.dumps(info, indent=4))
    (out / "meta" / "picky_episodes.json").write_text(json.dumps(episode_map, indent=1))
    print(f"wrote {n} episodes ({n_train} train / {n - n_train} validation) to {out}")
    return 0


def _video_frames(path: Path) -> tuple[int, int, int, float, str]:
    import av

    with av.open(str(path)) as c:
        s = c.streams.video[0]
        n = sum(1 for _ in c.decode(s))
        return n, s.codec_context.width, s.codec_context.height, float(s.average_rate or 0), s.codec_context.name


def validate(args: argparse.Namespace) -> int:
    import numpy as np
    import pandas as pd
    from lerobot.datasets.lerobot_dataset import LeRobotDataset

    root = Path(args.root)
    checks: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, bool(ok), detail))

    info = json.loads((root / "meta" / "info.json").read_text())
    feats = info["features"]
    check(
        "codebase_version supported", info.get("codebase_version") in SUPPORTED_VERSIONS, info.get("codebase_version")
    )
    check("fps == 30", info.get("fps") == FPS, str(info.get("fps")))
    for cam in CAMERAS:
        key = f"observation.images.{cam}"
        f = feats.get(key, {})
        check(
            f"{key} is a 640x480 RGB video",
            f.get("dtype") == "video" and list(f.get("shape", [])) == [H, W, 3],
            json.dumps({k: f.get(k) for k in ("dtype", "shape")}),
        )
        vi = f.get("info") or f.get("video_info") or {}
        if vi:
            check(f"{key} video fps 30", int(round(float(vi.get("video.fps", FPS)))) == FPS, json.dumps(vi))
    for key in ("observation.state", "action"):
        f = feats.get(key, {})
        check(
            f"{key} = 6 float32 in the required order",
            f.get("dtype") == "float32" and list(f.get("shape", [])) == [6] and list(f.get("names") or []) == NAMES,
            json.dumps(f.get("names")),
        )
    splits = info.get("splits", {})

    def span(s: str) -> int:
        a, b = (int(x) for x in s.split(":"))
        return b - a

    n_ep = info.get("total_episodes", 0)
    n_tr = span(splits["train"]) if "train" in splits else 0
    n_va = span(splits["validation"]) if "validation" in splits else 0
    check(
        ">= 1 train and >= 1 validation episode",
        n_tr >= 1 and n_va >= 1 and n_tr + n_va == n_ep,
        f"splits={splits} total_episodes={n_ep}",
    )
    for rel in ("meta/info.json", "meta/stats.json"):
        check(f"{rel} present", (root / rel).exists())
    check(
        "tasks metadata present", (root / "meta" / "tasks.parquet").exists() or (root / "meta" / "tasks.jsonl").exists()
    )

    ds = LeRobotDataset(args.repo_id, root=root)
    check("reloads with LeRobotDataset", True, f"{ds.num_episodes} episodes / {ds.num_frames} frames")
    check("num_episodes matches info", ds.num_episodes == n_ep, f"{ds.num_episodes} vs {n_ep}")
    hf = ds.hf_dataset.with_format(None)
    df = pd.DataFrame(
        {
            "episode_index": np.asarray(hf["episode_index"]),
            "frame_index": np.asarray(hf["frame_index"]),
            "timestamp": np.asarray(hf["timestamp"], dtype=np.float64),
            "index": np.asarray(hf["index"]),
        }
    )
    state = np.stack([np.asarray(x, dtype=np.float32) for x in hf["observation.state"]])
    action = np.stack([np.asarray(x, dtype=np.float32) for x in hf["action"]])
    check("global index contiguous", (df["index"].to_numpy() == np.arange(len(df))).all())
    ep_meta = ds.meta.episodes
    lengths_ok, ts_ok, bounds_ok, act_ok, vid_ok = True, True, True, True, True
    vid_detail = []
    for e in range(ds.num_episodes):
        rows = df[df["episode_index"] == e]
        n = len(rows)
        m_len = int(ep_meta[e]["length"]) if ep_meta is not None else n
        lengths_ok &= n == m_len and n > 0
        fi = rows["frame_index"].to_numpy()
        bounds_ok &= (fi == np.arange(n)).all()
        ts = rows["timestamp"].to_numpy()
        ts_ok &= np.allclose(ts, fi / FPS, atol=1e-4)
        idx = rows.index.to_numpy()
        act_ok &= np.allclose(action[idx[:-1]], state[idx[1:]], atol=1e-3) and np.allclose(
            action[idx[-1]], state[idx[-1]]
        )
        for cam in CAMERAS:
            key = f"observation.images.{cam}"
            try:
                vpath = root / ds.meta.get_video_file_path(e, key)
            except Exception:  # noqa: BLE001 - older API
                vpath = None
            if vpath is not None and vpath.exists():
                from_ts = float(ep_meta[e].get(f"videos/{key}/from_timestamp", 0.0))
                to_ts = float(ep_meta[e].get(f"videos/{key}/to_timestamp", n / FPS))
                vid_ok &= abs((to_ts - from_ts) * FPS - n) < 1.01
                if e < 1 or e == ds.num_episodes - 1:
                    nf, vw, vh, rate, codec = _video_frames(vpath)
                    vid_detail.append(f"{vpath.relative_to(root)}: {nf} frames {vw}x{vh} @{rate:.0f} {codec}")
                    vid_ok &= (vw, vh) == (W, H) and int(round(rate)) == FPS
            else:
                vid_ok = False
                vid_detail.append(f"missing video for episode {e} {key}")
    check("episode lengths agree (data rows == episode metadata)", lengths_ok)
    check("frame_index restarts at 0 and is contiguous per episode", bounds_ok)
    check("timestamps == frame_index / 30 per episode", ts_ok)
    check("action[t] == state[t+1] within each episode", act_ok)
    check("video spans match episode lengths; 640x480 @ 30 fps", vid_ok, "; ".join(vid_detail[:4]))
    lo = state.min(axis=0)
    hi = state.max(axis=0)
    in_range = all(lim[0] - 1e-3 <= a and b <= lim[1] + 1e-3 for a, b, lim in zip(lo, hi, ARM_LIMITS, strict=True))
    check(
        "joint values within SO-101 ranges (arm deg, gripper 0-100)",
        in_range,
        json.dumps({n: [round(float(a), 1), round(float(b), 1)] for n, a, b in zip(NAMES, lo, hi, strict=True)}),
    )
    # decode a few frames through the dataset itself
    sample_ok, shapes = True, set()
    for i in (0, len(ds) // 2, len(ds) - 1):
        item = ds[i]
        for cam in CAMERAS:
            t = item[f"observation.images.{cam}"]
            shapes.add(tuple(t.shape))
            sample_ok &= tuple(t.shape) == (3, H, W)
        sample_ok &= tuple(item["observation.state"].shape) == (6,) and isinstance(item.get("task"), str)
    check("decoded samples: images (3, 480, 640), state (6,), task string", sample_ok, str(sorted(shapes)))
    tasks = sorted(set(ds.meta.tasks.index)) if hasattr(ds.meta.tasks, "index") else []
    report = {
        "root": str(root),
        "codebase_version": info.get("codebase_version"),
        "total_episodes": n_ep,
        "total_frames": info.get("total_frames"),
        "splits": splits,
        "n_tasks": len(tasks),
        "tasks_sample": tasks[:5],
        "checks": [{"check": c, "ok": ok, "detail": d} for c, ok, d in checks],
        "all_ok": all(ok for _, ok, _ in checks),
    }
    (root / "meta" / "picky_validation.json").write_text(json.dumps(report, indent=1))
    for c, ok, d in checks:
        print(f"[{'PASS' if ok else 'FAIL'}] {c}" + (f"  ({d})" if d else ""))
    print("ALL OK" if report["all_ok"] else "SOME CHECKS FAILED")
    return 0 if report["all_ok"] else 1


def preview(args: argparse.Namespace) -> int:
    import av
    import numpy as np
    from PIL import Image

    raw, out = Path(args.raw), Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    eps = raw_episodes(raw)
    for k in args.episodes:
        ep_dir, meta = eps[k]
        n = meta["n_frames"]
        name = f"{ep_dir.name}_{meta['sku'].lower()}"
        frames = []
        for t in range(n):
            side = Image.open(ep_dir / "side" / f"{t:06d}.jpg").convert("RGB")
            wrist = Image.open(ep_dir / "wrist" / f"{t:06d}.jpg").convert("RGB")
            both = Image.new("RGB", (2 * W, H))
            both.paste(side, (0, 0))
            both.paste(wrist, (W, 0))
            frames.append(both)
        mp4 = out / f"{name}.mp4"
        with av.open(str(mp4), "w") as c:
            try:
                s = c.add_stream("libx264", rate=FPS)
            except Exception:  # noqa: BLE001 - PyAV build without x264
                s = c.add_stream("mpeg4", rate=FPS)
            s.width, s.height, s.pix_fmt = 2 * W, H, "yuv420p"
            for im in frames:
                for pkt in s.encode(av.VideoFrame.from_ndarray(np.asarray(im), format="rgb24")):
                    c.mux(pkt)
            for pkt in s.encode():
                c.mux(pkt)
        small = [im.resize((640, 240)) for im in frames[::3]]
        gif = out / f"{name}.gif"
        small[0].save(gif, save_all=True, append_images=small[1:], duration=100, loop=0)
        for label, t in (("start", 0), ("grasp", meta["grasp_frame"]), ("release", meta["release_frame"])):
            frames[t].save(out / f"{name}_{label}.jpg", quality=90)
        print(f"{mp4} {gif} ({n} frames, task: {meta['task']})")
    return 0


def sheet(args: argparse.Namespace) -> int:
    """Key-frame contact sheets decoded FROM THE LEROBOT DATASET (not the raw JPEGs): proves the AV1 videos decode,
    line up with the episode boundaries, and show what the policy will see (side | wrist per row, state overlaid)."""
    import numpy as np
    from lerobot.datasets.lerobot_dataset import LeRobotDataset
    from PIL import Image, ImageDraw

    root, out = Path(args.root), Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ds = LeRobotDataset(args.repo_id, root=root)
    ep_map_p = root / "meta" / "picky_episodes.json"
    ep_map = {m["episode_index"]: m for m in json.loads(ep_map_p.read_text())} if ep_map_p.exists() else {}
    for e in args.episodes:
        em = ds.meta.episodes[e]
        a, b = int(em["dataset_from_index"]), int(em["dataset_to_index"])
        n = b - a
        m = ep_map.get(e, {})
        g, r = m.get("grasp_frame"), m.get("release_frame")
        if (g is None or r is None) and args.raw and m.get("raw"):
            raw_meta = json.loads((Path(args.raw) / m["raw"] / "episode.json").read_text())
            g, r = raw_meta.get("grasp_frame"), raw_meta.get("release_frame")
        keys = [("start", 0)]
        if g is not None and r is not None:
            keys += [("pre-grasp", max(0, g - 20)), ("grasp", g), ("lifted", min(n - 1, g + 15)), ("release", r)]
        else:
            keys += [(f"{int(f * 100)}%", int(f * (n - 1))) for f in (0.3, 0.5, 0.65, 0.85)]
        keys.append(("end", n - 1))
        rows = []
        for label, t in keys:
            item = ds[a + t]
            assert int(item["episode_index"]) == e and int(item["frame_index"]) == t, (e, t)
            ims = []
            for cam in CAMERAS:
                x = item[f"observation.images.{cam}"].permute(1, 2, 0).numpy()
                ims.append((np.clip(x, 0.0, 1.0) * 255).astype(np.uint8))
            row = Image.fromarray(np.concatenate(ims, axis=1)).resize((W, H // 2))
            st = ", ".join(f"{v:.0f}" for v in item["observation.state"].tolist())
            d = ImageDraw.Draw(row)
            d.rectangle((0, 0, W, 14), fill=(0, 0, 0))
            d.text((4, 2), f"ep {e} t={t}/{n - 1} {label}  state=[{st}]", fill=(255, 255, 255))
            rows.append(row)
        grid = Image.new("RGB", (W, len(rows) * (H // 2)))
        for i, row in enumerate(rows):
            grid.paste(row, (0, i * (H // 2)))
        split = m.get("split", "?")
        name = f"ds_ep{e:03d}_{split}_{str(m.get('sku', 'item')).lower()}.jpg"
        grid.save(out / name, quality=88)
        print(f"{out / name} ({n} frames, task: {item['task']})")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert")
    c.add_argument("--raw", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--repo-id", default=REPO_ID)
    c.add_argument("--val-every", type=int, default=10, help="every n-th raw episode goes to the validation split")
    c.add_argument("--limit", type=int, default=0)
    c.add_argument("--overwrite", action="store_true")
    v = sub.add_parser("validate")
    v.add_argument("--root", required=True)
    v.add_argument("--repo-id", default=REPO_ID)
    p = sub.add_parser("preview")
    p.add_argument("--raw", required=True)
    p.add_argument("--out-dir", required=True)
    p.add_argument("--episodes", type=int, nargs="+", default=[0, 1])
    k = sub.add_parser("sheet", help="key-frame contact sheets decoded from the LeRobot dataset")
    k.add_argument("--root", required=True)
    k.add_argument("--repo-id", default=REPO_ID)
    k.add_argument("--out-dir", required=True)
    k.add_argument("--raw", default="", help="raw episodes dir, for grasp/release frames of older conversions")
    k.add_argument("--episodes", type=int, nargs="+", default=[0])
    args = ap.parse_args(argv)
    return {"convert": convert, "validate": validate, "preview": preview, "sheet": sheet}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
