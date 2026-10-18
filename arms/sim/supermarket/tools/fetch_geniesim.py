"""Download one AgiBot Genie Sim supermarket scene plus only the assets it references.

Genie Sim assets (huggingface.co/datasets/agibot-world/GenieSimAssets, CC BY-NC-SA 4.0, ungated) are ~29 GB in total;
one supermarket scene with its products is ~0.4 GB. Products are separate prims with rigid-body physics and convex
collision already authored, so the robot can pick them.

    python arms/sim/supermarket/tools/fetch_geniesim.py --scene supermarket_12 --root ~/assets/geniesim
    python arms/sim/supermarket/tools/fetch_geniesim.py --scene supermarket_12 --list   # print what would be fetched

The repo's folder layout is kept because scenes reference assets by relative path (../../../objects/...).
"""

from __future__ import annotations

import argparse
import posixpath
import re
from pathlib import Path

REPO = "agibot-world/GenieSimAssets"
REF_RE = re.compile(r"@([^@\n]+)@")
USD_EXT = (".usd", ".usda", ".usdc", ".usdz")


def refs_in(path: Path) -> list[str]:
    """Asset paths referenced by a USD layer (text or binary crate)."""
    try:
        from pxr import Sdf

        layer = Sdf.Layer.FindOrOpen(str(path))
        return [*layer.GetCompositionAssetDependencies(), *layer.externalReferences]
    except ImportError:
        return REF_RE.findall(path.read_text(encoding="utf-8", errors="ignore"))


def resolve(layer_repo_path: str, ref: str) -> str | None:
    if ref.startswith(("file:", "http", "omniverse:")) or posixpath.isabs(ref):
        return None  # a few scenes carry authoring-machine absolute paths; harmless if left unresolved
    return posixpath.normpath(posixpath.join(posixpath.dirname(layer_repo_path), ref))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default="supermarket_12")
    ap.add_argument("--root", type=Path, default=Path.home() / "assets" / "geniesim")
    ap.add_argument("--list", action="store_true", help="fetch only the scene layer and print its asset folders")
    args = ap.parse_args()

    from huggingface_hub import HfApi, hf_hub_download

    files = set(HfApi().list_repo_files(REPO, repo_type="dataset"))
    scene = f"background/supermarket/{args.scene}/background.usda"
    if scene not in files:
        scenes = sorted({f.split("/")[2] for f in files if f.startswith("background/supermarket/")})
        raise SystemExit(f"unknown scene {args.scene}; available: {scenes}")

    def fetch(repo_path: str) -> Path:
        return Path(hf_hub_download(REPO, repo_path, repo_type="dataset", local_dir=args.root))

    queue, seen, n = [scene], set(), 0
    while queue:
        rp = queue.pop()
        if rp in seen:
            continue
        seen.add(rp)
        # A referenced USD asset brings its whole folder (payload layers, textures); anything else comes alone.
        folder = posixpath.dirname(rp) + "/"
        group = sorted(f for f in files if f.startswith(folder)) if rp != scene and rp.endswith(USD_EXT) else [rp]
        if args.list and rp != scene:
            print(f"{len(group):4d} files  {folder}")
            continue
        for f in group:
            local = fetch(f)
            n += 1
            if f.endswith(USD_EXT):
                for ref in refs_in(local):
                    target = resolve(f, ref)
                    if target in files and target not in seen:
                        queue.append(target)
                    elif target and target not in files and not target.startswith(folder):
                        print(f"warning: {f} references missing {target}")
        seen.update(group)

    print(f"fetched {n} files; scene: {args.root / scene}")


if __name__ == "__main__":
    main()
