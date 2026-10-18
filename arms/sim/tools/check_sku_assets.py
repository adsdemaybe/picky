# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Validate sku_assets.yaml against data/products.yaml + index.yaml and print coverage / per-store prim budget.

Usage: python -I check_sku_assets.py <products.yaml> <index.yaml> <sku_assets.yaml> <slot_stats.json>
Exit code 1 on any error.
"""

import collections
import json
import sys

import yaml

products_p, index_p, sku_p, stats_p = sys.argv[1:5]
products = yaml.safe_load(open(products_p))["products"]
assets = yaml.safe_load(open(index_p))["assets"]
doc = yaml.safe_load(open(sku_p))
stats = json.load(open(stats_p))
skus = doc["skus"]
errors: list[str] = []


def err(msg):
    errors.append(msg)


prod = {p["sku"]: p for p in products}
missing = sorted(set(prod) - set(skus))
extra = sorted(set(skus) - set(prod))
if missing:
    err(f"missing SKUs: {missing}")
if extra:
    err(f"unknown SKUs: {extra}")

REQ = ["name", "render", "match", "target_dims_m", "mass_kg", "facing", "label", "grasp", "flags", "fallback_primitive"]
for sku, e in skus.items():
    for k in REQ:
        if k not in e:
            err(f"{sku}: missing {k}")
    T = e["target_dims_m"]
    if len(T) != 3 or min(T) <= 0:
        err(f"{sku}: bad target_dims_m {T}")
    if abs(e["mass_kg"] - float(prod[sku]["asset"]["mass_kg"])) > 1e-9:
        err(f"{sku}: mass differs from products.yaml")
    r = e["render"]
    if r == "asset":
        aid = e["asset_id"]
        if aid not in assets:
            err(f"{sku}: asset {aid} not in index")
            continue
        if e["usd"] != f"items/{aid}/item.usda":
            err(f"{sku}: usd path {e['usd']}")
        s = e["scale_xyz"]
        if min(s) <= 0:
            err(f"{sku}: non-positive scale {s}")
        b = assets[aid]["bbox_m"]
        rend = [b[i] * s[i] for i in range(3)]  # native axes
        if e["facing"]["yaw_offset_deg"] % 180 == 90:
            rend = [rend[1], rend[0], rend[2]]
        for i in range(3):
            if abs(rend[i] - T[i]) > 0.002:
                err(f"{sku}: bbox*scale {[round(x, 4) for x in rend]} != target {T}")
                break
        pc = e["facing"]["pose_check"]
        if pc == "UNVERIFIED":
            err(f"{sku}: pose of {aid} unverified")
    elif r == "parts":
        if not e.get("parts"):
            err(f"{sku}: parts empty")
        for part in e.get("parts", []):
            if "asset" in part:
                if part["asset"] not in assets:
                    err(f"{sku}: part asset {part['asset']} not in index")
                if len(part["offsets_m"]) != part["count"]:
                    err(f"{sku}: part count mismatch")
                md = part["member_dims_m"]
                fx = part.get("pivot_fix_m", [0.0, 0.0, 0.0])
                for o0 in part["offsets_m"]:
                    o = [o0[i] - fx[i] for i in range(3)]  # bbox bottom-center
                    if (
                        abs(o[0]) + md[0] / 2 > T[0] / 2 + 0.006
                        or abs(o[1]) + md[1] / 2 > T[1] / 2 + 0.006
                        or o[2] + md[2] > T[2] + 0.006
                    ):
                        err(f"{sku}: part {part['asset']} at {o} ({md}) outside target {T}")
                        break
            else:
                sz = part["size_m"]
                if any(sz[i] > T[i] + 1e-6 for i in range(3)):
                    err(f"{sku}: wrapper {sz} larger than target {T}")
    elif r == "primitive":
        if e["primitive"]["shape"] not in ("box", "cylinder", "sphere"):
            err(f"{sku}: bad primitive shape")
    else:
        err(f"{sku}: bad render {r}")
    lab = e["label"]
    if lab["mode"] not in ("wrap", "panel", "top", "sticker", "none"):
        err(f"{sku}: bad label mode")
    if lab["mode"] != "none" and "png_px" not in lab:
        err(f"{sku}: label without png_px")
    g = e["grasp"]
    if g.get("graspable") not in ("yes", "marginal", "no"):
        err(f"{sku}: bad grasp")

# ---------------------------------------------------------------------------------------------------- report
print(
    f"SKUs in products.yaml: {len(prod)}; mapped: {len(set(prod) & set(skus))} "
    f"({100.0 * len(set(prod) & set(skus)) / len(prod):.1f}%)"
)
print("match:", dict(collections.Counter(e["match"] for e in skus.values())))
print("render:", dict(collections.Counter(e["render"] for e in skus.values())))
print("grasp:", dict(collections.Counter(e["grasp"]["graspable"] for e in skus.values())))
flag_counts = collections.Counter(f for e in skus.values() for f in e["flags"])
print("flags:", dict(sorted(flag_counts.items())))
used = set()
for e in skus.values():
    if e.get("asset_id") and e["render"] == "asset":
        used.add(e["asset_id"])
    for part in e.get("parts") or []:
        if "asset" in part:
            used.add(part["asset"])
print(
    f"distinct source models used: {len(used)} "
    f"(simready {sum(a.startswith('sr_') for a in used)}, robocasa {sum(a.startswith('rc_') for a in used)}, "
    f"lightwheel {sum(a.startswith('lw_') for a in used)})"
)
risk = sorted(a for a in used if assets[a].get("brand_risk"))
print("brand-risk models still used (art covered by our wrap/sleeve):", risk)

# per-store prim budget (units from the planograms)
per_store = collections.defaultdict(lambda: collections.Counter())
for sku, e in skus.items():
    st = stats.get(sku)
    if not st:
        continue
    for sid, n in st["units"].items():
        c = per_store[sid]
        c["units"] += n
        c["unit_root_xforms"] += n
        if e["render"] == "asset":
            c["asset_instances"] += n
        elif e["render"] == "parts":
            k = sum(p["count"] for p in e["parts"] if "asset" in p)
            w = sum(1 for p in e["parts"] if "prim" in p)
            c["asset_instances"] += n * k
            c["wrapper_gprims"] += n * w
        else:
            c["primitive_gprims"] += n
        if e["label"]["mode"] != "none":
            c["label_instances"] += n
        c[f"render_{e['render']}"] += n
        c["skus"] += 1 if n else 0
for sid, c in sorted(per_store.items()):
    authored = (
        c["unit_root_xforms"]
        + c["asset_instances"]
        + c["wrapper_gprims"]
        + c["primitive_gprims"]
        + c["label_instances"]
    )
    print(f"{sid}: {dict(c)}; authored prims ~{authored}")
protos = set(used)
label_protos = sum(1 for sku, e in skus.items() if e["label"]["mode"] != "none" and stats.get(sku))
print(f"instance prototypes: {len(protos)} asset geoms + {label_protos} per-SKU label decals")
if errors:
    print("\nERRORS:")
    for m in errors:
        print(" -", m)
    sys.exit(1)
print("\nOK: sku_assets.yaml parses, covers every SKU, scales reproduce target dims, parts fit their boxes")
