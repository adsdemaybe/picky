# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Lateral overlap between neighbouring slots (same fixture/level/face): products.yaml dims vs sku_assets target dims.
Run with the repo venv: PYTHONPATH=<repo> python -B overlap_check.py <repo>/data <sku_assets.yaml>"""

import collections
import math
import sys

import yaml

sys.dont_write_bytecode = True
from arms.sim.scene_spec import ITEM_GAP_M, item_yaw_for, pick_face_dir  # noqa: E402
from utils.data import list_store_ids, load_planogram, load_products  # noqa: E402

R, SAP = sys.argv[1:3]
SA = yaml.safe_load(open(SAP))["skus"]
prods = load_products(R)


def extent(slot, fx, dims):
    cols = max(slot.facings, 4) if fx.kind == "produce_bin" else slot.facings
    return cols * (dims[0] + ITEM_GAP_M) - ITEM_GAP_M


for sid in list_store_ids(R):
    plan = load_planogram(sid, R)
    groups = collections.defaultdict(list)
    for s in plan.slots:
        groups[(s.fixture_id, s.level, round(pick_face_dir(s), 2))].append(s)
    res = {"products.yaml": [], "sku_assets": []}
    for key, ss in groups.items():
        fx = plan.fixture(key[0])
        yaw = item_yaw_for(ss[0])
        lat = (math.cos(yaw), math.sin(yaw))
        ss = sorted(ss, key=lambda s: s.item_position.x * lat[0] + s.item_position.y * lat[1])
        for a, b in zip(ss, ss[1:]):
            gap = (b.item_position.x - a.item_position.x) * lat[0] + (b.item_position.y - a.item_position.y) * lat[1]
            for tag, getd in (
                ("products.yaml", lambda s: prods[s.sku].asset.dims_m),
                ("sku_assets", lambda s: SA[s.sku]["target_dims_m"]),
            ):
                need = extent(a, fx, getd(a)) / 2 + extent(b, fx, getd(b)) / 2
                if need > gap + 0.005:
                    res[tag].append(f"{a.sku}|{b.sku} ({fx.kind}) gap {gap:.2f} need {need:.2f}")
    for tag, v in res.items():
        print(f"{sid} {tag}: {len(v)} overlapping neighbour pairs")
        for x in v:
            print("   ", x)
