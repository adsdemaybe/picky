# Sim assets: per-item grocery models and the rover USD

The Isaac worker can draw every stocked unit as its own 3D model, and every rover as the LeKiwi base + SO-101 arm on a
lift, instead of primitive boxes. Both are optional, and both fall back to today's primitives on their own. The
assets (~3 GB) stay on the GPU host. The repo holds only the SKU map, which is data, and the tools that rebuild
everything.

| What | In the repo | On vhagar |
|---|---|---|
| SKU -> model map (125 SKUs) | `data/sim/sku_assets.yaml` (generated, never hand-edited) | |
| Normalized product models | | `/data/$USER/picky-assets/products/items/<asset_id>/{item,geom}.usda` |
| Raw sources + conversions | | `.../products/{simready,robocasa,lightwheel,converted}/` |
| Asset catalog (463 assets) | | `.../products/index.yaml` |
| License notice | `arms/sim/tools/ATTRIBUTION.md` | copied to `.../products/` and `.../picky-rover/` |
| Rover USD | | `/data/$USER/picky-assets/robots/picky-rover/{picky_rover.usda,lekiwi.usd}` |
| Tools (download, normalize, map, rover) | `arms/sim/tools/` | |

Nothing binary is committed: no USD, mesh or texture, and not LeIsaac's `lekiwi.usd`, which is fetched (below).

## Environment (worker host only; process env, not `.env`)

| Variable / flag | Default | Effect |
|---|---|---|
| `PICKY_ASSET_ROOT` / `--asset-root` | unset = off | The `products/` asset tree. When it is unset or missing, every SKU is drawn as a primitive. |
| `PICKY_SKU_ASSETS` / `--sku-assets` | `$PICKY_DATA_DIR/sim/sku_assets.yaml` | The SKU map. |
| `PICKY_SIM_ASSETS=off` / `--no-assets` | on | Forces primitives everywhere, even with an asset root. |
| `PICKY_ROVER_USD` / `--rover-usd` | unset = primitive rover | `picky_rover.usda`. Used only by the Isaac backend; the Pillow sketch always draws primitives. |

On startup the worker logs `per-item models from ... (125 SKUs mapped ...)`. Each store then logs one line:
`SimLog: store_ave scene: N SKUs on per-item models, M primitive (gap: ...; render=primitive by design: ...)`.
After that comes `store store_ave: N units on per-item models, K shelf tags`. For a rover USD the log reads
`rover USD ...: SO-101 puppet ready in ~2 s`.

## What falls back, and to what

Each item below falls back to **today's primitive box/cylinder with our label quad** for that SKU:

- `PICKY_ASSET_ROOT` is unset or not a directory, `PICKY_SIM_ASSETS=off`, or the map file is missing or unreadable.
- The SKU is not in the map.
- The SKU is a `gap` match: TORT-CORN and TORT-FLOUR use a snack-bag stand-in, and CILANTRO uses lettuce.
- The SKU is `render: primitive` by design: EGGS, SPARKLING-LIME, NAPKINS, PAPER-PLATES, CHEESE-CHEDDAR.
- Any `item.usda` / `geom.usda` the SKU needs is missing. The worker logs one warning per SKU.
- Authoring a model unit raises. That unit is redrawn as a primitive.

Each item below falls back to **the primitive rover** (the full `RoverSpec.prims`):

- `PICKY_ROVER_USD` is unset or missing, or the backend is not Isaac.
- The USD has no SO-101 links (`shoulder ... jaw`), or building the arm chain fails. The worker logs a warning.

What does not change, whether the models are on or off:

- Instance ids, unit counts per slot, lots and freshness.
- The wire messages and the contracts.
- The data hash, so the gateway's routing is unaffected.
- The camera rig, so scan bboxes still come from `geometry.py`.

A SKU drawn as a model uses the model's `target_dims_m` as its unit size, so spacing, scan bboxes and basket
stacking match the mesh. These dims equal `products.yaml` except for the 18 SKUs flagged `products_dims_fix`, which
include the lying loaf, the flat meat trays and oval bottles. For those SKUs the 2D robot fake still uses the
`products.yaml` dims, until a data PR applies that patch.

## How the worker uses the map

The map is `data/sim/sku_assets.yaml`, read by `arms/sim/assets.py`. Each SKU has a `render` mode:

| `render` | SKUs | Unit prim |
|---|---|---|
| `asset` | 96 (93 drawn: 3 are gaps) | Xform that references `items/<id>/item.usda`, with ops `translate . rotateZ . scale(scale_xyz)`. The origin sits at the bottom center. `scale_xyz` is in the model's native axes. |
| `parts` | 24 | Xform with one child per member (`P<k>_<i>/G` references `geom.usda`, instanceable) and the opaque wrapper boxes (carrier, board). Clear film, clamshell and netting boxes are **skipped**: real-time RTX may draw UsdPreviewSurface opacity as opaque, which would hide the tray contents. This is unverified. |
| `primitive` | 5 | Today's `_define_unit`. |

Details:

- **Physics.** `item.usda` makes the unit root a rigid body. The worker sets `physics:kinematicEnabled = true` on every
  unit root and authors `mass_kg`. Nothing is simulated, so ~1,900 units stay put. A held unit follows its own
  xformOps through `set_instance_pose`, which offsets the bottom-center origin and the yaw.
- **Instancing.** `/Item/Geom` and each part's `G` are instanceable, so all units of one model share one prototype:
  ~100 prototypes for ~3,700 references. Per-unit differences (label, scale) live on the unit root, outside the
  instance.
- **Labels.** Labels are decal meshes under the unit root that carry our label PNG, `arms.sim.labels.label_png`. It
  is the same artwork as the primitive label and perception's references, with brand, name, size, price and EAN-13.
  The decal is authored in the item frame, then mapped into the model's native, scaled frame:

  | `label.mode` | Decal |
  |---|---|
  | `wrap` | The front half-ring carries the texture. The back half-ring is solid label color, which hides source art such as the "Monte Delight" bean cans. |
  | `panel` | A front quad. A `sleeve` adds solid back and side quads. |
  | `top` | A lid quad plus a front strip. |
  | `sticker` | A small front quad. |
  | `none` | Loose produce keeps its own texture. The worker adds a **shelf-edge price tag**, our PNG, in front of the slot, so the price stays readable. |

- **Produce piles.** A deterministic per-instance yaw jitter (crc32 of the instance id) applies to `yaw_jitter_deg`
  SKUs.

## Rover: LeKiwi + SO-101 + lift as a visual puppet

`picky_rover.usda` is the `compose_picky_rover.py` overlay on LeIsaac's `lekiwi.usd`. It adds the prismatic lift
(0 to 1.08 m), a mast, a basket and three cameras. The worker references it once per rover under
`/World/Stores/<store>/Robots/<robot>/Model/LeKiwi`. `Model` carries a +90° yaw, because the LeKiwi arm reaches
along -Y, and +0.0443 m so the wheels touch the floor. The worker runs **no physics**, so it treats the robot as a
kinematic puppet (`arms/sim/rover_puppet.py`):

- **Physics removed.** Joints are deactivated, and `ArticulationRootAPI` and `RigidBodyAPI` are removed. The
  `collisions` children and the virtual `base_x/base_y/base_theta` links are deactivated.
- **Base.** The base pose is the rover root Xform, the same as the primitive rover.
- **Lift.** The six SO-101 links (`shoulder ... jaw`) each get one `xformOp:transform:puppet`, which is forward
  kinematics from the USD's rest poses and joint frames plus the lift travel. The pan axis sits at
  `ARM_MOUNT_X_M + 0.04`.
- **Arm.** A reach table is precomputed once per USD: damped least squares IK over arm extension × drop below the
  reference height, about 2 s. It puts the TCP (between the jaw tips) at `geometry.gripper_local(lift, ext)` with the
  gripper level. The jaws therefore meet the unit the worker holds, within a few mm. Wrist roll is 90°, so the jaws
  close across the width. The jaws are open while reaching and closed when holding or stowed, through the backend's
  `set_robot_gripper` hook.
- **Primitives kept.** These stay as primitives at the contracts geometry: the lift column (moved to the base
  center, behind the arm), the camera head and neck, a carriage bracket that rides the lift, and the basket at
  `BASKET_OFFSET_LOCAL` on a post. The overlay's own `lift_carriage`, `picky_mast` and `picky_basket` are hidden.
- **Cameras.** Cameras keep today's rig from `geometry.py`, so front pan-tilt scans, wrist and basket frames and the
  scan bboxes are unchanged.

## Sources, licenses, attribution

| # | Source | License | Used for |
|---|---|---|---|
| 1 | NVIDIA SimReady Assets, (c) NVIDIA, https://huggingface.co/datasets/nvidia/simready-assets | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (per-package `.metadata/LICENSE.txt`) | 60 of the 100 product models used (`sr_*`) |
| 2 | RoboCasa365 object library, AI-generated objects. Nasiriany et al., "RoboCasa: Large-Scale Simulation of Everyday Tasks for Generalist Robots", RSS 2024, https://robocasa.ai | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) per the [RoboCasa README](https://github.com/robocasa/robocasa#license) (the zip has no license file) | 18 models (`rc_*`) |
| 3 | Lightwheel kitchen objects via NVIDIA PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF, https://huggingface.co/datasets/nvidia/PhysicalAI-Robotics-Manipulation-Objects-Kitchen-MJCF | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) (dataset card) | 22 models (`lw_*`) |
| 4 | LeIsaac `lekiwi.usd` (LightwheelAI), https://huggingface.co/LightwheelAI/leisaac_env (`assets/robots/lekiwi.usd`) | [Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0) (model card + GitHub) | Rover base + SO-101 arm (fetched, never committed) |
| 5 | Upstream robot designs behind `lekiwi.usd`: LeKiwi (SIGRobotics-UIUC, https://github.com/SIGRobotics-UIUC/LeKiwi) and SO-101 / SO-ARM100 (TheRobotStudio, https://github.com/TheRobotStudio/SO-ARM100) | Apache-2.0 | Credited, not downloaded |

**Which models exactly.** The models drawn are the `asset_id` and `parts[].asset` values in `data/sim/sku_assets.yaml`
(100 distinct). For each one, `index.yaml` on vhagar records `source`, `source_id`, `source_usd`, `license` and
`attribution`, and every SimReady package keeps its own `.metadata/LICENSE.txt`. `arms/sim/tools/ATTRIBUTION.md` is
the license notice that travels with the asset tree (rebuild step 5 copies it in).

**Credit line.** Use this in the README, the demo credits and on Devpost:

> Product 3D models: NVIDIA SimReady Assets (CC BY 4.0), RoboCasa (Nasiriany et al., RSS 2024; CC BY 4.0),
> Lightwheel/NVIDIA PhysicalAI Kitchen objects (CC BY 4.0); modified by the Picky team. Robot model: LeIsaac LeKiwi +
> SO-101 (LightwheelAI, Apache-2.0; LeKiwi by SIGRobotics-UIUC, SO-101 by TheRobotStudio), with a lift overlay by the
> Picky team. CC BY 4.0: https://creativecommons.org/licenses/by/4.0/

What we changed:

- Normalized the models to Z-up, meters, origin at the bottom center, upright with the front toward -Y.
- Swapped SDF colliders for convex hulls and moved the rigid body to the unit root.
- Converted MJCF/OBJ to USD.
- Rescaled each model to its SKU.
- Covered the front with our labels.
- Rover: our overlay `picky_rover.usda` sublayers the untouched `lekiwi.usd` and adds the lift, mast, basket and
  cameras; the worker hides the overlay's own lift parts and poses the arm links kinematically.

**Trademark caution.** CC BY covers copyright, not trademarks. Some SimReady art resembles real brands: Dr. Oetker,
Nesquik, the Coca-Cola contour bottle, a Heinz-like label, "Lysa" ≈ Lay's, "Doradoss" ≈ Doritos, and "Monte Delight"
≈ Del Monte (see `brand_risk` in `index.yaml`). The map avoids all of these except the three bean cans, and on those
the 360° wrap covers the art. Before a public demo, check one basket-camera frame: the back of a carton or a member
of a multipack can still show source art.

Not used:

- Non-commercial licenses: HOPE / LIBERO, Lightwheel SimReady and BEHAVIOR-1K.
- Real-brand textures: YCB and Google Scanned Objects.
- Assets with no license file.

## Rebuild everything on vhagar (exact commands)

```bash
ssh vhagar
export PICKY_ASSET_ROOT=/data/$USER/picky-assets/products R=/data/$USER/picky-assets/products
REPO=/data/$USER/picky/repo                       # the rsynced repo (DEPLOY.md step 1)
mkdir -p $R/_tools $R/_logs
# 0) tool venv: usd-core + hf + numpy (NOT Isaac's python); needs uv, tmux, curl and unzip on the host
uv venv $R/.venv --python 3.12 && uv pip install --python $R/.venv/bin/python "huggingface_hub>=0.35" usd-core numpy pyyaml pillow
# 1) downloads (~2.2 GB, ~4 min): SimReady folders, Lightwheel zips, 64 RoboCasa objects via HTTP Range reads.
#    The env is passed inline: a tmux server that is already running does not see this shell's exports.
tmux new -d -s picky-assets-dl "PICKY_ASSET_ROOT=$R bash $REPO/arms/sim/tools/dl.sh > $R/_logs/dl.log 2>&1"
#    RoboCasa's category scales: kitchen_objects.py from the RoboCasa repo (path as of 2026-10; adjust if it moved)
curl -fsSL https://raw.githubusercontent.com/robocasa/robocasa/main/robocasa/models/objects/kitchen_objects.py \
  -o $R/_tools/kitchen_objects.py
sha256sum $R/_tools/kitchen_objects.py   # 17ca0549417be86abff519ce51024255bfed44f7787a16eadd55fea872fa4585 when built
#    WAIT for the downloads: `tail -f $R/_logs/dl.log` until it prints "== done" (each step prints rc=0)
# 2) convert MJCF/OBJ -> USD, normalize every asset, write items/*/{item,geom}.usda + index.yaml (~3 min)
$R/.venv/bin/python -I $REPO/arms/sim/tools/build_items.py --convert
# 3) the SKU map (repo venv, any machine with index.yaml): planogram slot stats -> generator -> validator
cd $REPO && uv run python arms/sim/tools/slot_stats.py /tmp/slot_stats.json
uv run python arms/sim/tools/gen_sku_assets.py data/products.yaml $R/index.yaml /tmp/slot_stats.json data/sim/sku_assets.yaml
uv run python arms/sim/tools/check_sku_assets.py data/products.yaml $R/index.yaml data/sim/sku_assets.yaml /tmp/slot_stats.json
git diff --stat data/sim/sku_assets.yaml  # in a git checkout: no change unless products / planograms / index changed
# 4) optional checks: contact sheets / test shelf (Isaac), triangle budget (asset venv)
/data/$USER/isaacsim/python.sh $REPO/arms/sim/tools/render_assets.py shelf
# 5) the license notice travels with the assets
cp $REPO/arms/sim/tools/ATTRIBUTION.md $R/
```

Step 3 also works from the Mac with a copy of `index.yaml`, and the generator reproduces the committed map byte for
byte. Rerun step 3 after any change to `products.yaml` or the planograms. Data files are the source of truth
(CLAUDE.md rule 5): fix the generator, never the YAML.

**Rover USD.** `lekiwi.usd` is downloaded once (35.7 MB, never committed), then composed with usd-core:

```bash
D=/data/$USER/picky-assets/robots/picky-rover && mkdir -p $D
$R/.venv/bin/hf download LightwheelAI/leisaac_env assets/robots/lekiwi.usd --local-dir /tmp/leisaac
cp /tmp/leisaac/assets/robots/lekiwi.usd $D/
$R/.venv/bin/python -I $REPO/arms/sim/tools/compose_picky_rover.py $D/lekiwi.usd $D/picky_rover.usda
cp $REPO/arms/sim/tools/ATTRIBUTION.md $D/
sha256sum $D/lekiwi.usd $D/picky_rover.usda
#   636d148716e3c231d8d8b68dc223fbff00b7596fbf69e446883ffa2e7397c0da  lekiwi.usd        (the file we used)
#   79e6150f81a966861e219f49f6e19d88c74db3752315cf541177b51f4bb0d16a  picky_rover.usda  (composer output for it)
# optional physics sanity check of the overlay (plays physics, renders 4 views):
/data/$USER/isaacsim/python.sh $REPO/arms/sim/tools/isaac_check_rover.py $D/picky_rover.usda /data/$USER/picky-assets/robots/renders 0.8
```

If upstream changes `lekiwi.usd`, the hashes differ. The puppet checks the SO-101 link names when it loads the file
and falls back to the primitive rover when they are missing. Keep `lekiwi.usd` next to `picky_rover.usda`, because
the overlay sublayers it by relative path. For the same reason, move or copy the whole `products/` tree as one unit:
each `geom.usda` reaches its sources through relative paths.

## Tools (`arms/sim/tools/`)

| File | What it does |
|---|---|
| `dl.sh`, `hf_folders.py`, `hf_ids.txt` | Downloads the 157 SimReady folders from the tree API plus parallel `/resolve`, and the Lightwheel zips. |
| `rzip.py`, `rc_select.json` | Extracts only the 64 chosen RoboCasa objects from the 5.77 GB zip with HTTP Range reads. |
| `obj2usd.py` | Turns MJCF + OBJ into `geom.usda`: visual meshes, UVs, diffuse texture, convex-decomposition colliders. |
| `build_items.py`, `poses.yaml` | Normalizes every source: shelf pose, meters / Z-up, bottom-center origin, SDF to convex hull. Writes `item.usda` and `index.yaml`. |
| `slot_stats.py`, `gen_sku_assets.py`, `check_sku_assets.py` | Build the SKU map (decision table: model, fit, label plan, grasp hints) and validate it. |
| `overlap_check.py`, `tri_budget.py`, `render_assets.py` | Check produce-bin overlap and the triangle budget, and render contact sheets. |
| `compose_picky_rover.py`, `isaac_check_rover.py` | Compose and check the rover overlay. |
| `ATTRIBUTION.md` | The license notice copied into the asset tree and the rover folder. |

The tools are standalone scripts for usd-core, Isaac's python or the repo venv. The worker never imports them, and
they carry `# ruff: noqa`.

## Known limitations / open items

- **Real-time RTX render.** The real-time RTX render of the full stores still needs a frame-time check on the first
  GPU run. Two textures are heavy: `sr_packaged_bread_rolls_a01` (246 MB) and `sr_packaged_bread_loaf_a01`
  (192 MB). Down-res them to 2k, and decimate the 664k-triangle rolls.
- **Folded arm on the bottom shelf.** At the lowest lift (0.2 m), the folded arm cannot get its jaws down to a unit
  near the floor. The held unit then floats up to ~10 cm ahead of the jaws until the lift rises.
- **Produce bins and wrappers.** Produce bins pile 4+ units wide, as before, so a few neighbouring piles overlap.
  Clear wrappers are not drawn, so meat trays show the meat and the label floats at film height.
- **Fake vs Isaac dims.** The `products.yaml` dims patch for the 18 `products_dims_fix` SKUs is not applied yet, so
  the 2D fake and Isaac disagree slightly for those SKUs after a mid-trip fallback.
