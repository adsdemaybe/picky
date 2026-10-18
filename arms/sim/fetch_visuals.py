"""Download the 3D models in arms/sim/visuals.yaml and convert them to USD for the Isaac backend.

Run once per sim host, with Isaac Sim's Python (the converter is Kit's omni.kit.asset_converter):

    /data/$USER/isaacsim/python.sh -m arms.sim.fetch_visuals            # vhagar
    OMNI_KIT_ACCEPT_EULA=YES python -m arms.sim.fetch_visuals            # pip isaacsim env

Writes ``<PICKY_SIM_ASSET_DIR>/<asset>/<asset>.usd`` (+ textures) and ATTRIBUTION.md (CC-BY credits). Already
converted assets are skipped. ``--glb-cache DIR`` reuses GLBs already on disk (an Objaverse download root containing
``glbs/...``). Without this step the backend draws plain boxes, so the sim never depends on it.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import urllib.request
from pathlib import Path

from arms.sim.visuals import asset_dir, asset_usd, load_visuals


def fetch_glb(src_base: str, rel: str, cache: Path, glb_cache: Path | None) -> Path:
    if glb_cache is not None and (glb_cache / rel).exists():
        return glb_cache / rel
    dst = cache / rel
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(".part")
        with urllib.request.urlopen(src_base + rel, timeout=120) as r, open(tmp, "wb") as f:  # noqa: S310 - fixed host
            shutil.copyfileobj(r, f)
        tmp.rename(dst)
    return dst


def attribution(path: Path) -> None:
    vis = load_visuals()
    lines = [
        "# 3D model credits (arms/sim/visuals.yaml)",
        "",
        "Downloaded from Objaverse 1.0 (Sketchfab uploads). Used unmodified apart from format conversion and scaling.",
        "",
    ]
    for key, a in vis.assets.items():
        lines.append(f'- **{key}**: "{a.name}" by {a.author} ({a.url}), {a.license}')
    path.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--glb-cache", type=Path, default=None, help="existing Objaverse root with glbs/...")
    ap.add_argument("--only", default="", help="comma list of asset keys")
    ap.add_argument("--force", action="store_true", help="re-convert even if the USD exists")
    args = ap.parse_args(argv)

    vis = load_visuals()
    keys = [k for k in vis.assets if not args.only or k in args.only.split(",")]
    out = asset_dir()
    out.mkdir(parents=True, exist_ok=True)
    todo = [k for k in keys if args.force or not asset_usd(k).exists()]
    print(f"{len(keys)} assets, {len(todo)} to convert -> {out}", flush=True)
    if not todo:
        attribution(out / "ATTRIBUTION.md")
        return 0

    from isaacsim import SimulationApp

    app = SimulationApp({"headless": True})
    import omni.kit.app
    import omni.kit.asset_converter as conv

    omni.kit.app.get_app().get_extension_manager().set_extension_enabled_immediate("omni.kit.asset_converter", True)
    failed = []
    for key in todo:
        src = vis.assets[key]
        try:
            glb = fetch_glb(vis.source_base, src.glb, out / "_glb", args.glb_cache)
        except OSError as exc:
            print(f"  {key}: download failed: {exc}", flush=True)
            failed.append(key)
            continue
        dst = asset_usd(key)
        dst.parent.mkdir(parents=True, exist_ok=True)

        async def convert(glb=glb, dst=dst):
            ctx = conv.AssetConverterContext()
            ctx.ignore_animations = True
            ctx.ignore_camera = True
            ctx.ignore_light = True
            task = conv.get_instance().create_converter_task(str(glb), str(dst), None, ctx)
            ok = await task.wait_until_finished()
            return ok, task.get_error_message()

        fut = asyncio.ensure_future(convert())
        while not fut.done():
            app.update()
        ok, err = fut.result()
        print(f"  {key}: {'ok' if ok else 'FAILED ' + str(err)}", flush=True)
        if not ok:
            failed.append(key)
    attribution(out / "ATTRIBUTION.md")
    app.close()
    if failed:
        print("failed:", ", ".join(failed), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
