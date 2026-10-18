# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Per-SKU slot stats from the repo planograms (read-only import of arms.sim.scene_spec).

usage: uv run python arms/sim/tools/slot_stats.py <out.json>   (input of gen_sku_assets.py)
"""

import json, sys, pathlib

sys.dont_write_bytecode = True
REPO = str(pathlib.Path(__file__).resolve().parents[3])
sys.path.insert(0, REPO)
from utils.data import load_planogram, load_products, list_store_ids  # noqa: E402
from arms.sim.scene_spec import level_heights, _vertical_room, BOARD_THICKNESS_M  # noqa: E402

prods = load_products(f"{REPO}/data")
out = {}
for sid in list_store_ids(f"{REPO}/data"):
    plan = load_planogram(sid, f"{REPO}/data")
    for s in plan.slots:
        fx = plan.fixture(s.fixture_id)
        room = _vertical_room(fx, s.level)
        hs = level_heights(fx)
        d = out.setdefault(s.sku, {"slots": [], "units": {}})
        d["units"][sid] = d["units"].get(sid, 0) + s.qty
        d["slots"].append(
            {
                "store": sid,
                "slot": s.slot_id,
                "kind": fx.kind,
                "level": s.level,
                "board_z": round(hs[s.level], 3) if s.level < len(hs) else None,
                "room_m": round(room, 3),
                "facings": s.facings,
                "qty": s.qty,
                "cap": s.capacity,
            }
        )
json.dump(out, open(sys.argv[1], "w"), indent=1)
print(len(out), "skus in planograms;", sum(sum(v["units"].values()) for v in out.values()), "units")
missing = sorted(set(prods) - set(out))
print("not stocked in any store:", missing)
