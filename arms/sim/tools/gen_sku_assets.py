# ruff: noqa  -- standalone asset-pipeline scripts (vhagar / usd-core / Isaac python), not imported by the worker
"""Generate sku_assets.yaml: every SKU in data/products.yaml -> how the Isaac worker renders ONE shelf unit.

Inputs (read-only): <repo>/data/products.yaml, index.yaml (asset catalog fetched from vhagar), slot_stats.json
(per-SKU planogram slots, from slot_stats.py). Output: sku_assets.yaml next to index.yaml.

Conventions (match index.yaml): Z-up, meters, kg. Asset origin = bottom center, front label toward -Y (native
frame). "Item frame" = the frame scene_spec.py uses for a unit: x = width (left->right facing the shelf),
y = depth (-y faces the aisle), z = up; the unit's world yaw is scene_spec.item_yaw_for(slot).

Usage: python gen_sku_assets.py <products.yaml> <index.yaml> <slot_stats.json> <out.yaml>
  e.g. uv run python arms/sim/tools/gen_sku_assets.py data/products.yaml $PICKY_ASSET_ROOT/index.yaml \
         /tmp/slot_stats.json data/sim/sku_assets.yaml     (arms/sim/ASSETS.md)
"""

import json
import math
import sys

import yaml

PRODUCTS, INDEX, STATS, OUT = sys.argv[1:5]
products = yaml.safe_load(open(PRODUCTS))["products"]
ASSETS = yaml.safe_load(open(INDEX))["assets"]
STATS_ = json.load(open(STATS))

GRIP_USABLE_M = 0.075  # SO-101 jaw opening we plan with (hard max ~0.08)
GRIP_MAX_M = 0.080
TOP_CLEAR_M = 0.10  # free height above an item needed for a top-down grasp (wrist + jaws)
PINCH_CLEAR_M = 0.06  # free height above a bag to pinch its top seal from the front-top
PAYLOAD_KG = 0.5  # advisory practical SO-101 payload (magic grasp ignores it)
FILL = 0.96  # parts fill fraction of their cell

# Explore/catalog renders I checked by eye (label toward the camera at -Y, upright) for assets the index has not
# marked front_checked. Everything else used here is front_checked in index.yaml.
EXPLORE_VIEWED = {
    "rc_salami_11",
    "rc_yogurt_1",
    "lw_jar_jar010",
    "lw_juice_juice002",
    "lw_cinnamon_cinnamon006",
    "lw_mustard_mustard001",
    "rc_spaghetti_box_6",
    "rc_spaghetti_box_10",
    "lw_tupperware_tupperware039",
    "lw_tupperware_tupperware040",
    "rc_bacon_9",
    "rc_pork_loin_3",
    "lw_turkey_slice_turkeyslice006",
    "rc_egg_13",
    "lw_lettuce_lettuce007",
}
# orientation does not matter (round / jittered natural items)
ORIENT_FREE = {"sr_produce_strawberry_a01"}


def bb(aid):
    return [float(v) for v in ASSETS[aid]["bbox_m"]]


def r3(v):
    return [round(float(x), 4) for x in v]


# ---------------------------------------------------------------------------------------------------------------------
# Label plans
# ---------------------------------------------------------------------------------------------------------------------
def wrap(z0, z1, note=None):
    d = {
        "mode": "wrap",
        "band_z": [z0, z1],
        "front_arc_deg": 180,
        "back": "solid label_color",
        "radius": "auto (mesh-sampled per z slice) + 0.001",
    }
    if note:
        d["note"] = note
    return d


def panel(cw, ch, zc, sleeve=False, note=None):
    d = {
        "mode": "panel",
        "face": "front",
        "cover_wh": [cw, ch],
        "center_z": zc,
        "surface": "auto (front heightfield) + 0.0015",
    }
    if sleeve:
        d["sleeve"] = "back + both sides in solid label_color (hides source art)"
    if note:
        d["note"] = note
    return d


def top(cw, cd, strip=True, note=None):
    d = {"mode": "top", "face": "top", "cover_wd": [cw, cd], "front_strip": strip}
    if note:
        d["note"] = note
    return d


def sticker(w, h, zc, note=None):
    d = {"mode": "sticker", "face": "front", "size_m": [w, h], "center_z": zc}
    if note:
        d["note"] = note
    return d


NONE = {"mode": "none", "note": "asset texture as-is (loose produce); identity from appearance + shelf-edge tag"}

# ---------------------------------------------------------------------------------------------------------------------
# Per-SKU decisions. fit: exact (per-axis to target dims) | cyl (round cross-section kept) | uniform (natural items).
# dims: override of products.yaml asset.dims_m (item frame) when that box is unrealistic or in the wrong orientation.
# ---------------------------------------------------------------------------------------------------------------------
BAG = panel(0.82, 0.70, 0.47)
BOX = panel(0.94, 0.94, 0.5)
CAN = wrap(0.06, 0.94)
JAR = wrap(0.12, 0.80)
BOTTLE = wrap(0.14, 0.60)
TUB = wrap(0.10, 0.88)
GABLE = panel(0.92, 0.70, 0.38)

S = {}


def asset(sku, aid, match, fit, label, note, dims=None, yaw=0, s=None, flags=(), alt=None):
    S[sku] = dict(
        render="asset",
        asset=aid,
        match=match,
        fit=fit,
        label=label,
        note=note,
        dims=dims,
        yaw=yaw,
        s=s,
        flags=list(flags),
        alt=alt,
    )


def parts(sku, match, parts_, label, note, dims=None, flags=(), main=None):
    S[sku] = dict(
        render="parts", asset=main, match=match, parts=parts_, label=label, note=note, dims=dims, flags=list(flags)
    )


def prim(sku, match, shape, color, label, note, dims=None, material="matte", flags=(), alt=None):
    S[sku] = dict(
        render="primitive",
        asset=alt,
        match=match,
        shape=shape,
        color=color,
        label=label,
        note=note,
        dims=dims,
        material=material,
        flags=list(flags),
    )


# ---- parts helpers ---------------------------------------------------------------------------------------------------
def rot_bbox(b, rot):
    x, y, z = b
    for ax, deg in rot:
        if int(round(deg)) % 180 == 0:
            continue
        if ax == "z":
            x, y = y, x
        elif ax == "x":
            y, z = z, y
        elif ax == "y":
            x, z = z, x
    return [x, y, z]


def pivot_fix(b, native_scale, rot):
    """Translation that puts a scaled + rotated member's bbox bottom-center back on its offset (the model origin is
    its native bottom-center; a rotation about x or y moves it). Ops are applied scale -> rot[-1] ... rot[0]."""
    import itertools

    corners = []
    for sx, sy, sz in itertools.product((-0.5, 0.5), (-0.5, 0.5), (0.0, 1.0)):
        v = [sx * b[0] * native_scale[0], sy * b[1] * native_scale[1], sz * b[2] * native_scale[2]]
        for ax, deg in reversed(rot):
            c, s_ = math.cos(math.radians(deg)), math.sin(math.radians(deg))
            x, y, z = v
            if ax == "x":
                v = [x, y * c - z * s_, y * s_ + z * c]
            elif ax == "y":
                v = [x * c + z * s_, y, -x * s_ + z * c]
            else:
                v = [x * c - y * s_, x * s_ + y * c, z]
        corners.append(v)
    lo = [min(cn[i] for cn in corners) for i in range(3)]
    hi = [max(cn[i] for cn in corners) for i in range(3)]
    return [-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]]


def grid(aid, n, g, T, rot=(), nonuniform=False, z0=0.0, jitter=0, region=None, scale=None, shift=(0.0, 0.0)):
    """n members of asset aid on a g=(nx,ny,nz) grid filling region (default: whole T box), bottom-center offsets."""
    nx, ny, nz = g
    R = region or [T[0], T[1], T[2] - z0]
    b = rot_bbox(bb(aid), rot)
    cell = [R[0] / nx, R[1] / ny, R[2] / nz]
    if scale is not None:
        sc = [scale] * 3
    elif nonuniform:
        sc = [FILL * cell[i] / b[i] for i in range(3)]
    else:
        u = FILL * min(cell[i] / b[i] for i in range(3))
        sc = [u, u, u]
    # scale is applied in the member's native axes (before rot); convert from item-axis scale
    native = list(sc)
    for ax, deg in reversed(rot):
        if int(round(deg)) % 180 == 0:
            continue
        if ax == "z":
            native[0], native[1] = native[1], native[0]
        elif ax == "x":
            native[1], native[2] = native[2], native[1]
        elif ax == "y":
            native[0], native[2] = native[2], native[0]
    m = [b[i] * sc[i] for i in range(3)]
    fix = pivot_fix(bb(aid), native, rot)
    out = []
    k = 0
    for iz in range(nz):
        for iy in range(ny):
            for ix in range(nx):
                if k >= n:
                    break
                x = (ix - (nx - 1) / 2) * cell[0] + shift[0]
                y = (iy - (ny - 1) / 2) * cell[1] + shift[1]
                z = z0 + iz * m[2]
                out.append([round(x + fix[0], 4), round(y + fix[1], 4), round(z + fix[2], 4)])
                k += 1
    return {
        "asset": aid,
        "count": len(out),
        "rotate": [[a, d] for a, d in rot],
        "scale_xyz": r3(native),
        "member_dims_m": r3(m),
        "offsets_m": out,
        "pivot_fix_m": r3(fix),
        "yaw_jitter_deg": jitter,
        "instanceable": True,
        "ref": "geom.usda (NOT item.usda: one rigid body per unit)",
    }


def fit_part(aid, W, D, H, z0=0.0, rot=()):
    b = rot_bbox(bb(aid), rot)
    sc = [W / b[0], D / b[1], H / b[2]]
    return {
        "asset": aid,
        "count": 1,
        "rotate": [[a, d] for a, d in rot],
        "scale_xyz": r3(sc),
        "member_dims_m": r3([W, D, H]),
        "offsets_m": [[0.0, 0.0, round(z0, 4)]],
        "instanceable": True,
        "ref": "geom.usda (NOT item.usda: one rigid body per unit)",
    }


def wrapper(kind, size, z0=0.0, color=None, note=None):
    mats = {
        "film": "clear film: OmniGlass.mdl (thin_walled, IOR 1.3), fallback UsdPreviewSurface opacity 0.25",
        "clamshell": "clear PET: OmniGlass.mdl, fallback UsdPreviewSurface opacity 0.3",
        "mesh_bag": "netting: UsdPreviewSurface opacity 0.35 + label_color tint",
        "carrier": "printed paperboard (matte, label_color)",
        "carton": "printed paperboard (matte, label_color)",
        "board": "printed paperboard (matte, label_color)",
        "foam_tray": "white foam (matte #f4f4f0)",
    }
    d = {
        "prim": "box",
        "kind": kind,
        "size_m": r3(size),
        "offset_m": [0.0, 0.0, round(z0, 4)],
        "material": mats[kind],
        "collider": kind in ("carrier", "carton", "board", "foam_tray"),
    }
    if color:
        d["color"] = color
    if note:
        d["note"] = note
    return d


# ===================================================================================================== Mexican pantry
asset(
    "TORT-CORN",
    "sr_processed_chips_l01",
    "gap",
    "exact",
    panel(0.92, 0.86, 0.5, sleeve=True),
    "No tortilla pack in any permissive set. Non-brand-risk snack-bag stand-in (Lysa bag d01 dropped) under a "
    "near-full front label + solid sleeve so it reads as a printed tortilla bag.",
    flags=["gap", "stand_in"],
)
asset(
    "TORT-FLOUR",
    "sr_processed_chips_l01",
    "gap",
    "exact",
    panel(0.92, 0.86, 0.5, sleeve=True),
    "Same stand-in as TORT-CORN (Lysa bag e01 dropped); label tells them apart.",
    flags=["gap", "stand_in"],
)
asset("TACO-SHELLS", "sr_processed_boxedFood_e01", "close", "exact", BOX, "Printed food box, landscape-ish.")
asset(
    "BEANS-BLACK",
    "sr_canned_beans_a01",
    "exact",
    "cyl",
    CAN,
    "15 oz can. Source 'Monte Delight' (Del Monte-like) art is fully covered by the 360 deg wrap.",
    flags=["brand_risk_covered"],
)
asset(
    "BEANS-PINTO",
    "sr_canned_beans_a02",
    "exact",
    "cyl",
    CAN,
    "15 oz can; art covered by wrap.",
    flags=["brand_risk_covered"],
)
asset(
    "BEANS-REFRIED",
    "sr_canned_beans_a03",
    "exact",
    "cyl",
    CAN,
    "16 oz can; art covered by wrap.",
    flags=["brand_risk_covered"],
)
asset(
    "SALSA-MILD",
    "lw_mayonnaise_mayonnaise005",
    "close",
    "cyl",
    JAR,
    "16 oz jar with lid; mayo label covered by wrap. Previous uniform scale rendered 0.088 m wide.",
)
asset(
    "SALSA-VERDE",
    "sr_processed_jam_b02",
    "close",
    "cyl",
    JAR,
    "Glass jar + black lid (jam jar) stretched 1.26x tall to a 16 oz salsa jar. rc_salsa_10 is a real salsa jar "
    "but squat (8.6x7.8 cm): 1.67x z stretch, and the old uniform fit made it 0.11 m wide (overlapped "
    "neighbours).",
    alt="rc_salsa_10",
)
asset("SALSA-CHIPOTLE", "sr_processed_jam_b02", "close", "cyl", JAR, "Glass jar + black lid, orange contents.")
asset("SALSA-HOT", "sr_processed_jam_b01", "close", "cyl", JAR, "Glass jar + black lid, red contents.")
asset(
    "HOTSAUCE-RED",
    "lw_syrup_bottle_syrupbottle002",
    "close",
    "cyl",
    BOTTLE,
    "Woozy-style bottle scaled 0.6x to a 5 oz hot sauce bottle.",
)
asset(
    "HOTSAUCE-JALAPENO",
    "lw_syrup_bottle_syrupbottle002",
    "close",
    "cyl",
    BOTTLE,
    "Same woozy bottle as HOTSAUCE-RED (rc_condiment_4 needed a 1.5x z stretch).",
    alt="rc_condiment_4",
)
asset(
    "HOTSAUCE-GHOST",
    "lw_syrup_bottle_syrupbottle005",
    "close",
    "exact",
    BOTTLE,
    "Flask-style red bottle; flat flask squared to 4.5 cm.",
)
asset(
    "TACO-SEASONING",
    "sr_processed_chips_k01",
    "close",
    "exact",
    panel(0.94, 0.90, 0.5),
    "Small pillow bag flattened to 2 cm = foil seasoning packet (was a chocolate bar).",
)
asset("ENCHILADA-SAUCE", "sr_processed_cannedFood_b01", "exact", "cyl", CAN, "10 oz can.")
asset("RICE-WHITE", "lw_flour_bag_flourbag001", "close", "exact", BAG, "2 lb paper/poly bag.")
asset("OLIVE-OIL", "rc_olive_oil_bottle_10", "exact", "cyl", wrap(0.15, 0.55), "Dark glass oil bottle.")
asset("PEANUT-BUTTER", "rc_peanut_butter_jar_12", "exact", "cyl", JAR, "PB jar.")
asset("PASTA", "sr_packaged_pasta_spaghetti_a01", "exact", "exact", panel(0.92, 0.92, 0.5), "Spaghetti box.")
asset("MARINARA", "lw_mayonnaise_mayonnaise006", "close", "cyl", JAR, "24 oz jar.")
asset("MASA-HARINA", "lw_flour_bag_flourbag002", "close", "exact", BAG, "4.4 lb flour-style paper bag.")
asset("SOY-SAUCE", "rc_vinegar_10", "exact", "cyl", BOTTLE, "Dark condiment bottle.")
asset("COCONUT-MILK", "sr_processed_cannedFood_k01", "exact", "cyl", CAN, "13.5 oz can.")
asset("TOMATOES-DICED", "sr_processed_cannedFood_d01", "exact", "cyl", CAN, "14.5 oz can (tomato art).")
asset("TUNA", "sr_processed_cannedFood_p01", "exact", "cyl", wrap(0.08, 0.92), "5 oz tuna can.")
asset(
    "BROTH-CHICKEN",
    "sr_beverage_boxedDrink_f01",
    "close",
    "exact",
    panel(0.92, 0.75, 0.42),
    "32 oz aseptic carton with cap (boxedDrink_g01 needed 1.75x widening).",
)
asset(
    "MAC-CHEESE",
    "sr_processed_boxedFood_b01",
    "exact",
    "exact",
    BOX,
    "Portrait pasta box (boxedFood_d01 'Mac & Cheese' art needed a 1.59x face stretch).",
)
asset(
    "HONEY",
    "lw_honey_bottle_honeybottle001",
    "exact",
    "exact",
    panel(0.80, 0.55, 0.45),
    "Oval honey squeeze bottle.",
    dims=[0.075, 0.05, 0.14],
)
asset(
    "RAMEN",
    "rc_spaghetti_box_10",
    "close",
    "exact",
    BOX,
    "5-pack noodle box, turned 90 deg so the long side faces the aisle (chip bag c01 needed a 1.9x face stretch).",
    yaw=90,
)
# ===================================================================================================== cereal / baking
asset(
    "CEREAL-OATS",
    "sr_processed_boxed_cereal_a05",
    "exact",
    "exact",
    panel(0.94, 0.94, 0.5, sleeve=True),
    "Cereal box. Replaces boxed_cereal_a02 ('Cruci Flakes' pope parody art).",
    alt="sr_processed_boxed_cereal_a02",
)
asset("GRANOLA", "sr_processed_boxed_cereal_a05", "close", "exact", BOX, "Cereal-style box.")
asset(
    "OATMEAL",
    "lw_jar_jar010",
    "close",
    "cyl",
    wrap(0.04, 0.90),
    "Straight-walled plain canister = oats canister (plain grey, all art is ours).",
)
asset("COFFEE", "lw_flour_bag_flourbag001", "close", "exact", BAG, "Kraft gusseted paper bag = coffee bag.")
asset("FLOUR-AP", "lw_flour_bag_flourbag011", "exact", "exact", BAG, "5 lb flour bag.")
asset("SUGAR", "lw_flour_bag_flourbag012", "close", "exact", BAG, "4 lb paper bag.")
# ===================================================================================================== snacks
asset("CHIPS-TORTILLA", "sr_processed_chips_p01", "exact", "exact", BAG, "Family-size chip bag.")
asset(
    "CHIPS-SPICY-LIME",
    "sr_processed_chips_f01",
    "exact",
    "exact",
    BAG,
    "Chip bag. Replaces chips_m01 ('Lysa' = Lay's-like art).",
    alt="sr_processed_chips_m01",
)
asset("CHIPS-POTATO", "sr_processed_chips_a01", "exact", "exact", BAG, "Kettle chip bag.")
asset(
    "PRETZELS",
    "sr_processed_chips_p01",
    "exact",
    "exact",
    BAG,
    "Large snack bag. Replaces chips_j01 ('Lysa' art).",
    alt="sr_processed_chips_j01",
)
asset("POPCORN", "sr_processed_boxedFood_h01", "close", "exact", BOX, "Microwave popcorn box.")
asset("TRAIL-MIX", "sr_processed_chips_g01", "close", "exact", BAG, "Stand-up pouch.")
asset("CHOC-DARK", "sr_processed_chocolate_b01", "exact", "exact", panel(0.94, 0.94, 0.5), "Chocolate bar.")
asset("CRACKERS", "sr_processed_boxedFood_c01", "exact", "exact", BOX, "Cracker box.")
asset(
    "COOKIES-CHOC-CHIP",
    "sr_processed_boxedFood_h01",
    "close",
    "exact",
    BOX,
    "Square box (boxedFood_a01 cookie box is landscape: 2.6x z stretch).",
    alt="sr_processed_boxedFood_a01",
)
asset("GRANOLA-BARS", "sr_processed_boxedFood_g01", "close", "exact", BOX, "Bar box.")
# ===================================================================================================== dairy / deli
asset("CHEESE-MEX", "sr_processed_chips_b01", "close", "exact", BAG, "Shredded-cheese zip pouch = snack-bag form.")
asset("CHEESE-COTIJA", "rc_yogurt_1", "close", "cyl", wrap(0.15, 0.85), "White flanged tub.")
asset("SOUR-CREAM", "sr_processed_yogurt_a01", "close", "cyl", TUB, "White dairy cup.")
asset(
    "YOGURT-GREEK",
    "sr_processed_yogurt_a01",
    "exact",
    "cyl",
    TUB,
    "The source cup is literally a Greek yogurt tub, scaled to 32 oz (was the tall 'Kinako' cup).",
    alt="sr_processed_yogurt_c01",
)
asset("HUMMUS", "sr_processed_yogurt_b01", "close", "cyl", wrap(0.10, 0.80), "Flanged deli cup.")
asset(
    "PICO-FRESH",
    "sr_processed_yogurt_b01",
    "close",
    "cyl",
    wrap(0.10, 0.80),
    "Flanged deli cup (real pico tubs are clear: see flags).",
    flags=["clear_container_shown_opaque"],
)
asset("QUESO-DIP", "sr_processed_jam_a02", "close", "cyl", TUB, "Wide tub.")
asset("MILK-2PCT", "sr_processed_milk_a01", "exact", "exact", GABLE, "Half-gallon gable carton.")
asset("OAT-MILK", "sr_processed_milk_d01", "exact", "exact", GABLE, "64 oz carton with cap.")
asset("HALF-HALF", "sr_processed_milk_g01", "exact", "exact", GABLE, "Pint gable carton.")
asset(
    "MILK-WHOLE",
    "rc_canola_oil_1",
    "proxy",
    "exact",
    panel(0.62, 0.45, 0.45),
    "No gallon milk jug model: handled plastic jug (canola-oil jug) is closer than a 1.8x-widened carton.",
    flags=["weak_proxy"],
    alt="sr_processed_milk_e01",
)
asset("BUTTER", "rc_boxed_food_5", "close", "exact", BOX, "Butter carton.")
asset(
    "CREAM-CHEESE",
    "rc_boxed_food_1",
    "close",
    "exact",
    BOX,
    "Brick carton, turned 90 deg to stand long side out.",
    yaw=90,
)
asset("STRING-CHEESE", "sr_processed_chips_l01", "proxy", "exact", BAG, "Flat resealable bag.")
asset("OJ", "sr_beverage_boxedDrink_d01", "exact", "exact", GABLE, "52 oz gable carton.")
asset("HORCHATA", "lw_juice_juice011", "close", "cyl", BOTTLE, "White drink bottle.")
asset(
    "SOYRIZO",
    "rc_salami_11",
    "close",
    "cyl",
    wrap(0.25, 0.80),
    "12 oz chub: standing salami casing (was a lying sausage 16x2.7 cm squashed into a standing box).",
)
# ===================================================================================================== produce (loose)
asset(
    "AVOCADO",
    "sr_produce_avocado_b01",
    "exact",
    "uniform",
    NONE,
    "Lies on its side like in a bin (10 cm Hass).",
    s=1.05,
    flags=["yaw_jitter"],
)
asset("LIMES", "sr_produce_lime_a01", "exact", "uniform", NONE, "Lime.", flags=["yaw_jitter"])
asset(
    "LEMONS", "sr_produce_lemon_a01", "exact", "uniform", NONE, "Lemon at native 7.4 cm.", s=1.0, flags=["yaw_jitter"]
)
asset(
    "ONION-WHITE",
    "sr_produce_onion_b01",
    "close",
    "uniform",
    NONE,
    "Brown-skinned onion (no white onion).",
    flags=["yaw_jitter", "color_mismatch"],
)
asset("PEPPER-RED", "sr_produce_bellPepper_a01", "exact", "uniform", NONE, "Red bell pepper.", flags=["yaw_jitter"])
asset(
    "GARLIC",
    "sr_produce_garlic_a01",
    "exact",
    "uniform",
    NONE,
    "One bulb per unit (products.yaml dims are one bulb; '3 ct sleeve' would be 3 bulbs in a mesh sleeve).",
    flags=["yaw_jitter"],
)
asset(
    "BROCCOLI",
    "sr_produce_broccoli_b01",
    "exact",
    "uniform",
    NONE,
    "Broccoli crown, 14 cm wide.",
    s=0.8,
    flags=["yaw_jitter"],
)
asset("BANANAS", "sr_produce_banana_bunch_a01", "exact", "uniform", NONE, "Banana bunch.")
asset(
    "LETTUCE-ROMAINE",
    "lw_lettuce_lettuce005",
    "close",
    "exact",
    panel(0.85, 0.30, 0.18),
    "Romaine-like head stretched 1.5x tall (= romaine hearts); printed bag band at the base.",
)
asset(
    "CILANTRO",
    "lw_lettuce_lettuce005",
    "gap",
    "exact",
    sticker(0.05, 0.03, 0.2, "twist-tie tag"),
    "No herb-bunch model: leafy romaine head squeezed to a flat 4 cm bunch.",
    flags=["gap", "stand_in"],
)
# ===================================================================================================== bakery
asset(
    "BREAD-SOURDOUGH",
    "sr_prepared_bread_farmerSourdough_a01",
    "exact",
    "exact",
    sticker(0.07, 0.05, 0.55, "bakery sticker"),
    "Unwrapped sourdough loaf.",
)
asset(
    "BREAD-WHEAT",
    "sr_packaged_bread_loaf_a01",
    "exact",
    "exact",
    panel(0.55, 0.70, 0.5),
    "Bagged sliced loaf lying long side out. products.yaml has it standing on end (0.12 x 0.12 x 0.28).",
    dims=[0.30, 0.12, 0.12],
    flags=["heavy_texture_192MB"],
)
asset(
    "BUNS-HAMBURGER",
    "sr_packaged_bread_rolls_a01",
    "close",
    "exact",
    panel(0.45, 0.60, 0.5),
    "Bagged rolls (8 ct is about 30 x 17 x 8 cm).",
    dims=[0.30, 0.17, 0.08],
    flags=["heavy_texture_246MB"],
)
# ===================================================================================================== frozen
asset("ICE-CREAM", "sr_processed_jam_a01", "close", "cyl", TUB, "48 oz round tub.")
asset("CORN-FROZEN", "sr_processed_chips_i01", "close", "exact", BAG, "Frozen-veg bag.")
asset("PEAS-FROZEN", "sr_processed_chips_i01", "close", "exact", BAG, "Frozen-veg bag.")
asset("SHRIMP-FROZEN", "sr_processed_chips_k01", "close", "exact", BAG, "Frozen seafood bag.")
asset("BURRITOS-FROZEN", "sr_processed_boxedFood_g01", "close", "exact", BOX, "Frozen meal box.")
asset("PIZZA-FROZEN", "sr_processed_boxedFood_h01", "close", "exact", BOX, "Square pizza box standing on edge.")
# ===================================================================================================== beverages
asset("COLA", "sr_beverage_bottledDrink_d01", "close", "cyl", wrap(0.30, 0.55), "PET bottle scaled to 2 L.")
asset("SPORTS-DRINK", "lw_juice_juice005", "close", "cyl", BOTTLE, "28 oz bottle.")
asset(
    "APPLE-JUICE",
    "lw_juice_juice002",
    "close",
    "cyl",
    wrap(0.15, 0.62),
    "64 oz plastic juice bottle (boxedDrink_f01 now used by broth).",
)
asset(
    "WINE-RED",
    "sr_beverage_champagneBottle_a01",
    "close",
    "cyl",
    wrap(0.18, 0.50),
    "Wine-shaped bottle; body is opaque white, not dark glass.",
    flags=["color_mismatch"],
)
# ===================================================================================================== household / care
asset(
    "DISH-SOAP",
    "sr_cleaning_dishSoap_a01",
    "exact",
    "exact",
    panel(0.70, 0.45, 0.38),
    "Oval squeeze bottle; products.yaml treats it as a 0.08 m cylinder.",
    dims=[0.09, 0.052, 0.236],
)
asset(
    "BODY-WASH",
    "sr_cleaning_dishSoap_a01",
    "close",
    "exact",
    panel(0.70, 0.45, 0.38),
    "Oval squeeze bottle at near-uniform 0.79 (soapDispenser_a02 needed a 1.8x oval squash).",
    alt="sr_cleaning_soapDispenser_a02",
)
asset("SHAMPOO", "sr_cleaning_soapDispenser_c01", "close", "cyl", BOTTLE, "Pump bottle.")
asset(
    "DEODORANT",
    "lw_mustard_mustard001",
    "proxy",
    "exact",
    panel(0.80, 0.55, 0.5),
    "Oval squeeze tube with the cap at the bottom stands in for a stick.",
    flags=["weak_proxy"],
)
asset(
    "TOOTHPASTE",
    "lw_aluminum_foil_aluminumfoil001",
    "close",
    "exact",
    BOX,
    "Long thin carton (foil box art fully covered).",
)
asset("ALUMINUM-FOIL", "lw_aluminum_foil_aluminumfoil001", "exact", "exact", BOX, "Foil roll box.")
asset("LAUNDRY-DETERGENT", "rc_canola_oil_1", "close", "exact", panel(0.62, 0.45, 0.45), "Handled jug.")
asset(
    "TRASH-BAGS",
    "rc_spaghetti_box_6",
    "close",
    "exact",
    BOX,
    "Printed carton (boxedFood_i01 needed a 3.2x depth stretch).",
    alt="sr_processed_boxedFood_i01",
)
asset(
    "MOLCAJETE",
    "rc_bowl_1",
    "close",
    "cyl",
    sticker(0.06, 0.04, 0.5, "hang tag"),
    "Grey stone bowl (no legs). Online-only: never spawned on a shelf.",
)
asset("OREGANO-MEX", "lw_cinnamon_cinnamon009", "close", "cyl", JAR, "Spice jar. Online-only.")
asset("CHILE-LIME-SEASONING", "lw_cinnamon_cinnamon006", "close", "cyl", JAR, "Tall spice shaker. Online-only.")

# ===================================================================================================== parts (multi-unit)
T_ = {p["sku"]: [float(v) for v in p["asset"]["dims_m"]] for p in products}


def tray_pack(sku, filler, n, g, dims, filler_rot=(), note="", flags=()):
    W, D, H = dims
    tray_h = min(0.6 * H, 0.03)
    tray = fit_part("lw_tupperware_tupperware040", W, D, tray_h)
    meat = grid(filler, n, g, [W * 0.86, D * 0.80, H * 0.92], rot=filler_rot, z0=0.006, jitter=8)
    film = wrapper("film", [W, D, H * 0.98], color=None, note="overwrap film")
    parts(
        sku,
        "close",
        [tray, meat, film],
        top(0.42, 0.36, True, "scale-label sticker on the film, front-left"),
        note,
        dims=dims,
        flags=["gap_tray_composite", *flags],
        main="lw_tupperware_tupperware040",
    )


tray_pack(
    "BEEF-GROUND",
    "rc_steak_3",
    2,
    (2, 1, 1),
    [0.20, 0.14, 0.05],
    note="Foam-tray composite: grey tray + red meat + film. No tray-with-film model exists (was an empty white tub).",
)
tray_pack("TURKEY-GROUND", "rc_pork_loin_3", 3, (3, 1, 1), [0.20, 0.14, 0.05], note="Tray + pale pink meat + film.")
tray_pack(
    "CHICKEN-BREAST",
    "rc_chicken_breast_2",
    2,
    (2, 1, 1),
    [0.24, 0.17, 0.05],
    note="Tray + 2 breasts + film. products.yaml has the tray standing on edge (0.24 x 0.05 x 0.17).",
)
tray_pack(
    "CHICKEN-THIGHS",
    "rc_chicken_breast_2",
    4,
    (2, 2, 1),
    [0.24, 0.17, 0.05],
    note="Tray + 4 pieces + film (no thigh model).",
    flags=["shape_proxy"],
)
tray_pack("STEAK-FLANK", "rc_steak_3", 1, (1, 1, 1), [0.28, 0.16, 0.04], note="Tray + one flat steak + film.")
tray_pack(
    "SALMON-FILLET",
    "rc_steak_8",
    1,
    (1, 1, 1),
    [0.26, 0.14, 0.04],
    note="Tray + fillet + film. rc_steak_8 is dark red: needs an orange-tinted variant on vhagar.",
    flags=["tint_todo_orange"],
)
parts(
    "BACON",
    "proxy",
    [
        wrapper("board", [0.25, 0.15, 0.004], color="label_color", note="printed board"),
        grid("rc_bacon_9", 3, (1, 3, 1), [0.24, 0.14, 0.016], rot=(("z", 90),), nonuniform=True, z0=0.004),
        wrapper("film", [0.25, 0.15, 0.02]),
    ],
    top(0.50, 0.90, True, "printed board header; bacon visible in the window"),
    "Flat pack lying down: printed board + 3 bacon strips + film. products.yaml has it standing (0.25 x 0.02 x 0.15).",
    dims=[0.25, 0.15, 0.02],
    flags=["gap_tray_composite"],
    main="rc_bacon_9",
)
parts(
    "TURKEY-DELI",
    "close",
    [
        fit_part("lw_tupperware_tupperware039", 0.18, 0.16, 0.035),
        grid("lw_turkey_slice_turkeyslice006", 2, (2, 1, 1), [0.16, 0.14, 0.03], z0=0.004, jitter=10),
        wrapper("film", [0.18, 0.16, 0.04], note="clear lid"),
    ],
    top(0.60, 0.50, True),
    "Rectangular deli tub + folded turkey slices + clear lid (lying).",
    dims=[0.18, 0.16, 0.04],
    main="lw_tupperware_tupperware039",
)
parts(
    "TOFU-FIRM",
    "close",
    [
        fit_part("lw_tupperware_tupperware039", 0.12, 0.10, 0.045),
        fit_part("rc_tofu_8", 0.10, 0.08, 0.04, z0=0.004),
        wrapper("film", [0.12, 0.10, 0.05], note="sealed film lid"),
    ],
    top(0.90, 0.85, True),
    "Water-pack tray + tofu block + film, lying (products.yaml: standing 0.12 x 0.05 x 0.10).",
    dims=[0.12, 0.10, 0.05],
    main="rc_tofu_8",
)
parts(
    "GUAC-KIT",
    "close",
    [
        fit_part("lw_tupperware_tupperware039", 0.18, 0.12, 0.06),
        grid("sr_produce_avocado_b01", 2, (1, 2, 1), [0.11, 0.11, 0.07], z0=0.004, jitter=15, shift=(-0.03, 0.0)),
        {
            **grid("sr_produce_lime_a01", 1, (1, 1, 1), [0.05, 0.05, 0.05], z0=0.004, scale=0.75),
            "offsets_m": [[0.055, -0.025, 0.004]],
        },
        {
            **grid("sr_produce_onion_b01", 1, (1, 1, 1), [0.05, 0.05, 0.05], z0=0.004, scale=0.6),
            "offsets_m": [[0.055, 0.03, 0.004]],
        },
        wrapper("clamshell", [0.18, 0.12, 0.08]),
    ],
    panel(0.90, 0.35, 0.80, note="band label on the clamshell front, top edge"),
    "Taco-trip hero item: clear box with 2 avocados, a lime and an onion (was an opaque take-out box).",
    dims=[0.18, 0.12, 0.08],
    main="sr_produce_avocado_b01",
)
parts(
    "TOMATO-ROMA",
    "close",
    [grid("sr_produce_tomato_g01", 4, (2, 2, 1), [0.18, 0.12, 0.07], rot=(("z", 90),), jitter=12)],
    NONE,
    "1 lb = 4 romas lying in a 2 x 2 cluster (the source tomato is elongated along y).",
    main="sr_produce_tomato_g01",
)
parts(
    "JALAPENO",
    "close",
    [grid("rc_chili_pepper_0", 4, (1, 2, 2), [0.14, 0.10, 0.05], nonuniform=True, jitter=10)],
    NONE,
    "4 chilies, 2 x 2 stack. Source chili is RED: make a green-tinted variant (hue-shift its albedo) on vhagar.",
    flags=["tint_todo_green"],
    main="rc_chili_pepper_0",
)
parts(
    "APPLES",
    "close",
    [
        grid("sr_produce_apple_b01", 9, (3, 1, 3), [0.22, 0.10, 0.22], jitter=30),
        wrapper("mesh_bag", [0.22, 0.10, 0.23], color="#c0392b"),
    ],
    panel(0.90, 0.20, 0.90, note="bag header label"),
    "3 lb bag: 9 apples in a red mesh bag + printed header.",
    main="sr_produce_apple_b01",
)
parts(
    "POTATO-RUSSET",
    "close",
    [
        grid("sr_produce_potato_a01", 12, (3, 1, 4), [0.30, 0.12, 0.36], jitter=30),
        wrapper("film", [0.30, 0.12, 0.40], note="poly bag; kraft colour band printed by the label"),
    ],
    panel(0.92, 0.45, 0.75, note="printed bag top half; potatoes visible below"),
    "5 lb bag: 12 potatoes in a poly bag.",
    main="sr_produce_potato_a01",
)
parts(
    "CARROTS",
    "close",
    [
        grid("sr_produce_freshCarrot_a02", 8, (4, 1, 2), [0.18, 0.06, 0.28], rot=(("x", 90),), jitter=5),
        wrapper("film", [0.18, 0.06, 0.30], color="#f39c12"),
    ],
    panel(0.92, 0.30, 0.82, note="printed header band"),
    "2 lb bag: 8 carrots standing in an orange-tinted bag.",
    main="sr_produce_freshCarrot_a02",
)
parts(
    "STRAWBERRIES",
    "gap",
    [
        grid("sr_produce_strawberry_a01", 20, (5, 4, 1), [0.17, 0.11, 0.035], z0=0.003, jitter=60),
        grid("sr_produce_strawberry_a01", 12, (4, 3, 1), [0.15, 0.09, 0.065], z0=0.03, jitter=60),
        wrapper("clamshell", [0.18, 0.12, 0.08]),
    ],
    top(0.70, 0.60, True, "lid label"),
    "Clear PET clamshell with 32 strawberries (no clamshell model exists).",
    flags=["gap_composite"],
    main="sr_produce_strawberry_a01",
)
parts(
    "SPINACH-BABY",
    "gap",
    [fit_part("lw_lettuce_lettuce007", 0.17, 0.11, 0.065, z0=0.003), wrapper("clamshell", [0.19, 0.13, 0.08])],
    top(0.80, 0.70, True, "lid label"),
    "Clear clamshell with a squashed leafy head as the greens.",
    flags=["gap_composite"],
    main="lw_lettuce_lettuce007",
)
parts(
    "BAGELS",
    "close",
    [grid("sr_prepared_bagel_b01", 6, (2, 1, 3), [0.21, 0.10, 0.11], jitter=8), wrapper("film", [0.22, 0.10, 0.12])],
    panel(0.70, 0.35, 0.80, note="printed bag top"),
    "6 bagels stacked 2 x 3 in a bag.",
    main="sr_prepared_bagel_b01",
)
parts(
    "MUFFINS-BLUEBERRY",
    "proxy",
    [
        grid("sr_prepared_cupcake_a01", 4, (2, 2, 1), [0.19, 0.19, 0.09], jitter=20),
        wrapper("clamshell", [0.20, 0.20, 0.10]),
    ],
    top(0.60, 0.40, True, "lid label"),
    "4 in a clear clamshell. Source is a frosted cupcake, not a muffin.",
    flags=["shape_proxy"],
    main="sr_prepared_cupcake_a01",
)
parts(
    "COLA-CANE",
    "close",
    [
        grid("sr_beverage_beerBottle_a01", 4, (2, 2, 1), [0.12, 0.12, 0.245]),
        wrapper("carrier", [0.13, 0.13, 0.12], color="label_color", note="open-top carrier with printed sides"),
    ],
    panel(0.94, 0.85, 0.24, note="on the carrier front"),
    "4 glass bottles in a printed carrier.",
    main="sr_beverage_beerBottle_a01",
)
parts(
    "LAGER-6PK",
    "close",
    [
        grid("sr_beverage_beerBottle_q01", 6, (3, 2, 1), [0.20, 0.13, 0.24]),
        wrapper("carrier", [0.20, 0.13, 0.13], color="label_color"),
    ],
    panel(0.94, 0.85, 0.26, note="on the carrier front"),
    "6 brown-glass bottles in a printed carrier.",
    main="sr_beverage_beerBottle_q01",
)
parts(
    "WATER-24PK",
    "close",
    [
        grid("sr_beverage_bottledWater_c01", 24, (6, 4, 1), [0.40, 0.27, 0.21], nonuniform=True),
        wrapper("film", [0.40, 0.27, 0.21], note="shrink wrap"),
    ],
    panel(0.90, 0.25, 0.5, note="printed band on the shrink wrap"),
    "24 bottles in shrink wrap.",
    main="sr_beverage_bottledWater_c01",
)
parts(
    "PAPER-TOWELS",
    "close",
    [
        grid("sr_consumable_paperTowel_roll_a01", 6, (3, 2, 1), [0.30, 0.20, 0.28], nonuniform=True),
        wrapper("film", [0.30, 0.20, 0.28]),
    ],
    panel(0.90, 0.55, 0.55, note="printed film front"),
    "6 rolls in printed film.",
    main="sr_consumable_paperTowel_roll_a01",
)
parts(
    "TOILET-PAPER",
    "close",
    [
        grid("sr_consumable_paperTowel_roll_a01", 12, (3, 2, 2), [0.30, 0.20, 0.30], nonuniform=True),
        wrapper("film", [0.30, 0.20, 0.30]),
    ],
    panel(0.90, 0.55, 0.55, note="printed film front"),
    "12 rolls (2 layers) in printed film.",
    main="sr_consumable_paperTowel_roll_a01",
)

# ===================================================================================================== primitives
prim(
    "SPARKLING-LIME",
    "primitive",
    "box",
    "label_color",
    panel(0.96, 0.92, 0.5, sleeve=True),
    "12-pack fridge carton: a printed paperboard box IS the real look (cans are hidden). Parts option: 12 x "
    "sr_beverage_can_b01 under film.",
    material="printed paperboard",
    alt="sr_beverage_can_b01",
)
prim(
    "EGGS",
    "gap",
    "box",
    "#cdbfa5",
    top(0.92, 0.80, True, "lid print + front strip"),
    "Closed pulp egg carton = box primitive. P1: open lid with 12 x rc_egg_13 (brown egg) on a 6 x 2 grid.",
    material="molded pulp (matte)",
    alt="rc_egg_13",
)
prim(
    "CHEESE-CHEDDAR",
    "gap",
    "box",
    "#f2a83b",
    panel(0.80, 0.80, 0.5),
    "Vacuum-sealed block: an orange box primitive + label is the real look (rc_cheese_5 is an unwrapped wedge "
    "that would need a 4.5x stretch). Check sr_dairy_cheese_i01 on vhagar (unseen).",
    material="glossy film (roughness 0.25)",
    alt="sr_dairy_cheese_i01",
)
prim(
    "NAPKINS",
    "proxy",
    "box",
    "#f7f7f2",
    top(0.90, 0.90, True),
    "Film-wrapped flat stack (rc_boxed_food_1 needed a 2.5x face stretch).",
    material="soft film (roughness 0.4)",
)
prim(
    "PAPER-PLATES",
    "proxy",
    "cylinder",
    "#fafafa",
    top(0.80, 0.80, True),
    "Film-wrapped stack of plates (lw_tray011 was a dark wooden serving tray).",
    material="soft film (roughness 0.4)",
    alt="lw_tray_tray011",
)

# ===================================================================================================== wireless store
# store_tmobile (data/planograms/store_tmobile.yaml): retail boxes, blister cards and clamshells. No model in the
# index fits them better than a printed box, so every wireless SKU is a box primitive under our label. The tethered
# live demo units (phones / tablets / watches on the tables) are sized from data/sim/demo_units.yaml, not here.
WIRELESS_CATEGORIES = {
    "phone",
    "tablet",
    "wearable",
    "audio",
    "case",
    "screen",
    "charger",
    "home_internet",
    "connected",
}
for _p in products:
    if _p["category"] in WIRELESS_CATEGORIES and _p["sku"] not in S:
        prim(
            _p["sku"],
            "primitive",
            "box",
            "label_color",
            BOX,
            "Retail box / blister card in a wireless store: a printed box primitive + our label is the real look.",
            material="printed paperboard",
        )


# ---------------------------------------------------------------------------------------------------------------------
# Derived fields
# ---------------------------------------------------------------------------------------------------------------------
def item_bbox(aid, yaw):
    b = bb(aid)
    return [b[1], b[0], b[2]] if yaw % 180 == 90 else b


def fit_scale(T, B, mode, s_override=None):
    if mode == "exact":
        return [T[i] / B[i] for i in range(3)]
    if mode == "cyl":
        if abs(B[0] - B[1]) / max(B[0], B[1]) > 0.10:  # not round: exact
            return [T[i] / B[i] for i in range(3)]
        sxy = math.sqrt((T[0] / B[0]) * (T[1] / B[1]))
        return [sxy, sxy, T[2] / B[2]]
    if mode == "uniform":
        if s_override:
            return [s_override] * 3
        ts, bs = sorted(T), sorted(B)
        g = (ts[0] / bs[0] * ts[1] / bs[1] * ts[2] / bs[2]) ** (1 / 3)
        return [g, g, g]
    raise ValueError(mode)


def slot_info(sku):
    st = STATS_.get(sku)
    if not st:
        return None
    rooms = [s["room_m"] for s in st["slots"] if s["kind"] != "produce_bin"]
    open_top = any(s["kind"] == "produce_bin" for s in st["slots"])
    return {
        "units": st["units"],
        "fixtures": sorted({s["kind"] for s in st["slots"]}),
        "min_room_m": min(rooms) if rooms else None,
        "open_top": open_top and not rooms,
        "slots": [s["slot"] for s in st["slots"]],
    }


def grasp_for(sku, form, dims, mass, si, render):
    w, d, h = dims
    clear = None
    if si:
        clear = 1.0 if si["open_top"] else (round(si["min_room_m"] - h, 3) if si["min_room_m"] is not None else None)
    g = {}
    roundish = form in ("can", "jar", "bottle", "tub", "shaker", "canister", "chub", "produce")

    def ok(width):
        return "yes" if width <= GRIP_USABLE_M else ("marginal" if width <= GRIP_MAX_M else "no")

    if form == "bag":
        if clear is None or clear >= PINCH_CLEAR_M:
            g = {
                "mode": "top_pinch",
                "approach": "front-top, wrist pitched down",
                "width_m": 0.025,
                "at": "crimped top seal",
                "graspable": "yes",
            }
        else:
            g = {
                "mode": "top_pinch",
                "approach": "front-top",
                "width_m": 0.025,
                "at": "crimped top seal",
                "graspable": "marginal",
                "why": f"only {clear} m above the bag in its slot",
            }
    elif form == "bunch":
        g = {"mode": "side", "approach": "front", "width_m": 0.04, "at": "crown / stems", "graspable": "yes"}
    elif form == "pack_lying":
        g = {
            "mode": "none",
            "width_m": round(min(w, d), 3),
            "graspable": "no",
            "why": "flat tray/pack wider than the jaws; magic grasp only (suction cup or tray-edge pinch later)",
        }
    elif form == "produce":
        m = min(w, d)
        g = {
            "mode": "top" if (si and si["open_top"]) else "side",
            "approach": "top-down into the bin" if (si and si["open_top"]) else "front",
            "width_m": round(m, 3),
            "at": "across the narrower horizontal side",
            "graspable": ok(m),
        }
    elif form in ("jar", "tub") and w > GRIP_MAX_M and 0.8 * w <= GRIP_MAX_M:
        lid = 0.8 * w
        top_ok = clear is None or clear >= TOP_CLEAR_M
        g = {
            "mode": "top",
            "approach": "top-down",
            "width_m": round(lid, 3),
            "at": "lid rim (about 0.8 x body)",
            "graspable": ok(lid) if top_ok else "marginal",
        }
        if not top_ok:
            g["why"] = f"only {clear} m above the item (needs {TOP_CLEAR_M})"
    elif form == "bottle" and w > GRIP_MAX_M:
        top_ok = clear is None or clear >= TOP_CLEAR_M
        g = {
            "mode": "top",
            "approach": "top-down",
            "width_m": 0.035,
            "at": "neck / cap",
            "graspable": "yes" if top_ok else "marginal",
        }
        if not top_ok:
            g["why"] = f"only {clear} m above the bottle (needs {TOP_CLEAR_M})"
    elif roundish and w <= GRIP_MAX_M:
        z = 0.5 if form in ("can", "jar", "tub", "produce", "chub") else 0.38
        g = {
            "mode": "side",
            "approach": "front",
            "width_m": round(w, 3),
            "at": f"body at {z:.2f} h",
            "graspable": ok(w),
        }
    elif form in ("box", "carton", "gable", "jug", "bar") and min(w, d) <= GRIP_MAX_M:
        top_ok = clear is None or clear >= TOP_CLEAR_M
        side = (
            {"mode": "side", "approach": "front", "width_m": round(w, 3), "at": "body at 0.40 h", "graspable": ok(w)}
            if w <= GRIP_MAX_M
            else None
        )
        topg = (
            {
                "mode": "top",
                "approach": "top-down, jaws across depth",
                "width_m": round(d, 3),
                "at": "top edge",
                "graspable": ok(d) if top_ok else "marginal",
            }
            if d <= GRIP_MAX_M
            else None
        )
        if topg and not top_ok:
            topg["why"] = f"only {clear} m above the item (needs {TOP_CLEAR_M})"
        rank = {"yes": 0, "marginal": 1, "no": 2}
        opts = [o for o in (side, topg) if o]
        g = min(opts, key=lambda o: (rank[o["graspable"]], o["width_m"]))
    elif form == "gable" or (form == "carton" and sku in ("MILK-2PCT", "OJ", "HALF-HALF")):
        g = {"mode": "top_pinch", "approach": "top-down", "width_m": 0.012, "at": "gable fin", "graspable": "yes"}
    elif form == "jug":
        g = {
            "mode": "side",
            "approach": "front-side",
            "width_m": 0.035,
            "at": "handle",
            "graspable": "marginal",
            "why": "handle is on the side; needs a 90 deg wrist roll",
        }
    elif roundish:
        g = {
            "mode": "none",
            "width_m": round(w, 3),
            "graspable": "no",
            "why": f"{w:.3f} m wide > {GRIP_MAX_M} m jaw opening",
        }
    else:
        g = {
            "mode": "none",
            "width_m": round(min(w, d), 3),
            "graspable": "no",
            "why": f"smallest horizontal side {min(w, d):.3f} m > {GRIP_MAX_M} m jaw opening",
        }
    if clear is not None:
        g["clearance_above_m"] = clear
    g["payload_ok"] = mass <= PAYLOAD_KG
    return g


def form_of(sku, sp):
    if sp["render"] == "parts":
        lying = {
            "BEEF-GROUND",
            "TURKEY-GROUND",
            "CHICKEN-BREAST",
            "CHICKEN-THIGHS",
            "STEAK-FLANK",
            "SALMON-FILLET",
            "BACON",
            "TURKEY-DELI",
            "TOFU-FIRM",
            "GUAC-KIT",
            "STRAWBERRIES",
            "SPINACH-BABY",
            "MUFFINS-BLUEBERRY",
            "TOMATO-ROMA",
            "JALAPENO",
        }
        if sku in lying:
            return "pack_lying"
        if sku in ("APPLES", "POTATO-RUSSET", "CARROTS", "BAGELS"):
            return "bag"
        return "multipack"
    if sp["render"] == "primitive":
        return {"PAPER-PLATES": "pack_lying", "NAPKINS": "pack_lying", "EGGS": "pack_lying"}.get(sku, "box")
    a = ASSETS[sp["asset"]]["form"]
    m = {
        "can": "can",
        "jar": "jar",
        "tub": "tub",
        "shaker": "shaker",
        "bag": "bag",
        "box": "box",
        "carton": "carton",
        "bar": "bar",
        "bottle": "bottle",
        "produce": "produce",
        "produce_bunch_or_head": "produce",
        "bakery": "produce",
        "meat_seafood": "chub",
        "dairy_deli": "box",
        "other": "bowl",
        "paper_roll": "canister",
    }
    f = m.get(a, "box")
    if sp["asset"].startswith("lw_mayonnaise"):
        f = "jar"
    if sku in ("MILK-2PCT", "OAT-MILK", "HALF-HALF", "OJ"):
        f = "gable"
    if sku in ("MILK-WHOLE", "LAUNDRY-DETERGENT"):
        f = "jug"
    if sku in ("OATMEAL",):
        f = "canister"
    if sku in ("BANANAS", "BROCCOLI", "LETTUCE-ROMAINE", "CILANTRO"):
        f = "bunch"
    if sku in ("BREAD-WHEAT", "BUNS-HAMBURGER"):
        f = "bag"
    if sku in ("BREAD-SOURDOUGH", "PASTA"):
        f = "box"
    if sku in ("APPLE-JUICE", "HORCHATA", "SPORTS-DRINK"):
        f = "bottle"
    if sku == "MOLCAJETE":
        f = "bowl"
    return f


out_skus = {}
prod_by = {p["sku"]: p for p in products}
for p in products:
    sku = p["sku"]
    sp = S[sku]
    pa = p["asset"]
    T0 = [float(v) for v in pa["dims_m"]]
    T = [float(v) for v in (sp.get("dims") or T0)]
    mass = float(pa["mass_kg"])
    si = slot_info(sku)
    match = {"parts": "composite", "primitive": "primitive"}.get(sp["render"], sp["match"])
    e = {
        "name": p["name"],
        "category": f"{p['category']}/{p['subcategory']}",
        "size": p.get("size"),
        "render": sp["render"],
        "match": match,
    }
    if sp["render"] == "asset":
        aid = sp["asset"]
        a = ASSETS[aid]
        B = item_bbox(aid, sp["yaw"])
        s_item = fit_scale(T, B, sp["fit"], sp.get("s"))
        dims = [B[i] * s_item[i] for i in range(3)]
        native = [s_item[1], s_item[0], s_item[2]] if sp["yaw"] % 180 == 90 else s_item
        e.update(
            {
                "asset_id": aid,
                "usd": f"items/{aid}/item.usda",
                "asset_form": a["form"],
                "asset_bbox_m": r3(bb(aid)),
                "fit": sp["fit"],
                "scale_xyz": r3(native),
                "distortion": round(max(s_item) / min(s_item), 3),
                "face_distortion": round(s_item[0] / s_item[2], 3),
            }
        )
        pose_ok = (
            "index front_checked"
            if a.get("front_checked")
            else (
                "explore render (label toward -Y)"
                if aid in EXPLORE_VIEWED
                else ("orientation-free" if aid in ORIENT_FREE else "UNVERIFIED")
            )
        )
        e["facing"] = {
            "front": "-Y (item frame) -> aisle",
            "yaw_offset_deg": sp["yaw"],
            "front_face_native": "-X" if sp["yaw"] % 180 == 90 else "-Y",
            "upright": True,
            "pose_check": pose_ok,
        }
        if "yaw_jitter" in sp["flags"]:
            e["facing"]["yaw_jitter_deg"] = 35
        br = a.get("brand_risk")
        if br:
            e["source_brand_risk"] = br
    elif sp["render"] == "parts":
        dims = T
        e.update(
            {
                "asset_id": sp["asset"],
                "usd": None,
                "parts": sp["parts"],
                "facing": {
                    "front": "-Y (item frame) -> aisle",
                    "yaw_offset_deg": 0,
                    "upright": True,
                    "pose_check": "parts placed in the item frame",
                },
            }
        )
        for part in sp["parts"]:
            if "asset" in part:
                part["usd"] = f"items/{part['asset']}/geom.usda"
    else:
        dims = T
        e.update(
            {
                "asset_id": None,
                "usd": None,
                "primitive": {
                    "shape": sp["shape"],
                    "dims_m": r3(T),
                    "color": pa["label_color"] if sp["color"] == "label_color" else sp["color"],
                    "material": sp["material"],
                },
                "facing": {
                    "front": "-Y (item frame) -> aisle",
                    "yaw_offset_deg": 0,
                    "upright": True,
                    "pose_check": "primitive",
                },
            }
        )
        if sp.get("asset"):
            e["candidate_asset"] = sp["asset"]
    e["target_dims_m"] = r3(dims)
    diff = max(abs(dims[i] - T0[i]) / T0[i] for i in range(3))
    e["dims_source"] = "products.yaml" if diff <= 0.03 else "sku_assets (override)"
    if diff > 0.10:
        e["products_yaml_dims_m"] = r3(T0)
    e["mass_kg"] = mass
    lab = dict(sp["label"])
    if lab["mode"] != "none":
        # PNG pixel size matched to the physical label area (label_image px, height 384)
        if lab["mode"] == "wrap":
            wm = math.pi * dims[0] / 2
            hm = (lab["band_z"][1] - lab["band_z"][0]) * dims[2]
        elif lab["mode"] == "panel":
            wm, hm = lab["cover_wh"][0] * dims[0], lab["cover_wh"][1] * dims[2]
        elif lab["mode"] == "top":
            wm, hm = lab["cover_wd"][0] * dims[0], lab["cover_wd"][1] * dims[1]
        else:
            wm, hm = lab["size_m"]
        lab["area_m"] = r3([wm, hm])
        lab["png_px"] = [max(160, min(1024, int(round(384 * wm / hm)))), 384]
        lab["texture"] = (
            "arms.sim.labels.label_png(name, brand, size, price_cents, barcode, color=label_color, px=png_px)"
        )
        lab["target"] = "wrapper part (first carrier/board/film/clamshell)" if sp["render"] == "parts" else "unit"
    e["label"] = lab
    form = form_of(sku, sp)
    e["grasp_form"] = form
    e["grasp"] = grasp_for(sku, form, dims, mass, si, sp["render"])
    flags = list(sp["flags"])
    if not e["grasp"]["payload_ok"]:
        flags.append("heavy_for_so101")
    if e["grasp"]["graspable"] == "no":
        flags.append("too_big_to_grasp")
    if sp["render"] == "asset" and max(e["face_distortion"], 1 / e["face_distortion"]) > 1.4:
        flags.append("high_face_distortion")
    if p.get("online_only"):
        flags.append("online_only")
    if si is None:
        flags.append("not_on_any_shelf")
    if e["dims_source"] != "products.yaml" and diff > 0.10:
        flags.append("products_dims_fix")
    e["flags"] = sorted(set(flags))
    e["stock"] = (
        {"units": si["units"], "fixtures": si["fixtures"], "slots": si["slots"]}
        if si
        else {"units": {}, "fixtures": [], "slots": []}
    )
    if sp.get("alt") and sp["render"] == "asset":
        e["replaced_or_alt"] = sp["alt"]
    e["fallback_primitive"] = {
        "shape": pa["shape"],
        "dims_m": r3(T),
        "color": pa["label_color"],
        "label": "front quad (current _define_unit)",
    }
    e["note"] = sp["note"]
    out_skus[sku] = e

assert set(out_skus) == set(prod_by), set(prod_by) ^ set(out_skus)

header = {
    "schema": "picky sku_assets v1",
    "generated_from": {
        "products": f"data/products.yaml ({len(products)} SKUs)",
        "index": "picky-assets/products/index.yaml (463 assets)",
        "planograms": "data/planograms/"
        + ", ".join(f"{s}.yaml" for s in sorted({x["store"] for v in STATS_.values() for x in v["slots"]})),
    },
    "asset_root": {
        "env": "PICKY_ASSET_ROOT",
        "default": "/data/advaiv2/picky-assets/products",
        "paths": "usd / parts[].usd are relative to the asset root",
    },
    "conventions": (
        "Z-up, meters, kg. Item frame = scene_spec.py's unit frame (x width, y depth, -y faces the aisle, z up); "
        "world yaw = scene_spec.item_yaw_for(slot) + facing.yaw_offset_deg. Asset origin = bottom center, so the "
        "unit translate is PrimSpec.center - (0, 0, target_dims_m[2]/2). render=asset: reference <usd> once per unit, "
        "xformOps translate . rotateZ . scale(scale_xyz); scale_xyz is in the asset's NATIVE axes (with "
        "yaw_offset 90 the native x maps to item y). target_dims_m is the rendered bbox in the item frame: use it as "
        "SlotLayout.dims / item_dims so spacing, scan bboxes and basket stacking match the mesh. render=parts: unit "
        "root gets RigidBodyAPI + MassAPI itself; each part references geom.usda (instanceable) with its own "
        "rotate/scale at offsets_m (bottom-center, item frame); box wrappers are UsdGeom.Cube. render=primitive: the "
        "existing _define_unit path with primitive.{shape,dims_m,color} + label."
    ),
    "gripper": {
        "model": "SO-101 parallel jaw",
        "usable_open_m": GRIP_USABLE_M,
        "max_open_m": GRIP_MAX_M,
        "top_grasp_clearance_m": TOP_CLEAR_M,
        "bag_pinch_clearance_m": PINCH_CLEAR_M,
        "payload_advisory_kg": PAYLOAD_KG,
        "note": "advisory only: the sim pick is a scripted magic grasp; jaw/payload numbers are planning "
        "values, not measured",
    },
    "match_levels": {
        "exact": "right kind of object, rescaled to the SKU",
        "close": "same packaging form, different product (our label covers the front)",
        "proxy": "different form standing in",
        "gap": "no usable model; a stand-in asset under our label",
        "composite": "no single model; built from several real models (+ film/tray/carrier boxes)",
        "primitive": "parametric box/cylinder + label is the realistic look (cartons, flat packs)",
    },
}
doc = dict(header)
doc["skus"] = out_skus
with open(OUT, "w") as f:
    f.write(
        "# Picky: every SKU -> how one shelf unit is rendered in Isaac Sim (generated by\n"
        "# arms/sim/tools/gen_sku_assets.py; do not hand-edit, change the decision table in the generator).\n"
        "# See arms/sim/ASSETS.md.\n"
    )

    class NoAlias(yaml.SafeDumper):
        def ignore_aliases(self, data):
            return True

    yaml.dump(doc, f, Dumper=NoAlias, sort_keys=False, width=120, allow_unicode=True)
print("wrote", OUT, len(out_skus), "skus")
