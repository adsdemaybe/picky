# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Run on vhagar with the asset venv: triangles per used prototype x instance counts (JSON on stdin)."""

import json, os, sys
from pxr import Usd, UsdGeom

root = os.environ.get("PICKY_ASSET_ROOT") or f"/data/{os.environ.get('USER', 'user')}/picky-assets/products"
inst = json.load(sys.stdin)
tot = {}
per = {}
for aid, by in inst.items():
    st = Usd.Stage.Open(f"{root}/items/{aid}/geom.usda")
    n = 0
    for p in st.Traverse():
        if p.IsA(UsdGeom.Mesh) and UsdGeom.Imageable(p).ComputePurpose() not in ("guide", "proxy"):
            c = UsdGeom.Mesh(p).GetFaceVertexCountsAttr().Get()
            n += sum(x - 2 for x in c) if c else 0
    per[aid] = n
    for sid, k in by.items():
        tot[sid] = tot.get(sid, 0) + n * k
print(json.dumps({"per_prototype": per, "instanced_tris": tot, "prototype_tris": sum(per.values())}))
