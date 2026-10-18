# ruff: noqa  -- standalone asset-pipeline script (GPU host, asset venv), not imported by the worker
"""Download the T-Mobile store's 3D models listed in models.yaml (no login, CC-BY-4.0 / CC0) into
$TMO_ASSET_ROOT/raw/<model_id>/ and write raw/<model_id>/SOURCE.yaml (url, license, author, sha256, bytes).

  $TMO_ASSET_ROOT/.venv/bin/python -I arms/sim/tmobile/fetch_models.py [model_id ...]

Formats: glb (Objaverse HF mirror: one .glb), gso_zip (Google Scanned Objects zip: meshes/model.obj + texture),
polyhaven_gltf (Poly Haven API: the 1k glTF + its bin / textures). Already-downloaded models are skipped.
"""

import hashlib
import json
import os
import sys
import urllib.request
import zipfile

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.environ.get("TMO_ASSET_ROOT") or f"/data/{os.environ.get('USER', 'advaiv2')}/picky-assets/tmobile"
UA = {"User-Agent": "picky-dubhacks-asset-fetch/1.0"}


def get(url: str, dest: str) -> int:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r, open(dest + ".part", "wb") as f:
        n = 0
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            n += len(b)
    os.replace(dest + ".part", dest)
    return n


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch(mid: str, e: dict) -> str:
    d = f"{ROOT}/raw/{mid}"
    if os.path.isfile(f"{d}/SOURCE.yaml"):
        return f"skip {mid}"
    os.makedirs(d, exist_ok=True)
    files = []
    if e["format"] == "glb":
        p = f"{d}/model.glb"
        get(e["download_url"], p)
        files.append(p)
    elif e["format"] == "gso_zip":
        z = f"{d}/model.zip"
        get(e["download_url"], z)
        files.append(z)
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():  # keep only the mesh, material and texture (no path tricks)
                base = os.path.basename(name)
                if not base or ".." in name or name.startswith("/"):
                    continue
                if base in ("model.obj", "model.mtl", "texture.png", "metadata.pbtxt"):
                    with zf.open(name) as src, open(f"{d}/{base}", "wb") as dst:
                        dst.write(src.read())
    elif e["format"] == "polyhaven_gltf":
        req = urllib.request.Request(e["download_url"], headers=UA)
        info = json.load(urllib.request.urlopen(req, timeout=60))
        g = info["gltf"]["1k"]["gltf"]
        p = f"{d}/model.gltf"
        get(g["url"], p)
        files.append(p)
        for rel, inc in (g.get("include") or {}).items():
            if ".." in rel or rel.startswith("/"):
                continue
            get(inc["url"], f"{d}/{rel}")
    else:
        raise ValueError(f"unknown format {e['format']}")
    src = {
        "model_id": mid,
        "download_url": e["download_url"],
        "page_url": e["page_url"],
        "license": e["license"],
        "author": e["author"],
        "title": e["title"],
        "source": e["source"],
        "files": [{"path": os.path.basename(f), "bytes": os.path.getsize(f), "sha256": sha256(f)} for f in files],
    }
    with open(f"{d}/SOURCE.yaml", "w") as f:
        yaml.safe_dump(src, f, sort_keys=False)
    return f"ok {mid} {sum(x['bytes'] for x in src['files'])} B"


def main() -> None:
    models = yaml.safe_load(open(f"{HERE}/models.yaml"))["models"]
    only = sys.argv[1:]
    for mid, e in models.items():
        if only and mid not in only:
            continue
        try:
            print(fetch(mid, e), flush=True)
        except Exception as ex:  # one failure must not stop the rest
            print(f"ERR {mid}: {ex!r}", flush=True)


if __name__ == "__main__":
    main()
