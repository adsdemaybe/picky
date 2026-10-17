# data/: store data (the source of truth)

These YAML files are the single source of truth for the stores (CLAUDE.md §4 rule 5). The DB seed, the Isaac
sim scene, the routing occupancy grid and the frontend store map are all **generated** from them. Fix the data
here, never a generated artifact. The schemas are in `contracts/planogram.py` and `contracts/catalog.py`. Load the
files only through `utils/data.py`.

```bash
uv run python -m utils.data        # validate every file: schema, cross-refs, EAN-13, robot clearance, lot sums
```

| File | Model | What |
|---|---|---|
| `products.yaml` | `ProductsFile` | 73 SKUs (70 in-store and 3 `online_only`) with fake brands, int-cent prices, category, tags + dietary, `spice_level`, valid EAN-13 barcodes (`200…` prefix), `perishable` / `shelf_life_days`, and sim `asset` hints (`shape`, `dims_m`, `label_color`, `mass_kg`) |
| `planograms/store_ave.yaml` | `Planogram` | Main demo store: "Picky Market - The Ave", 14 m x 22 m |
| `planograms/store_hill.yaml` | `Planogram` | Second store: "Picky Express - Capitol Hill", 9 m x 14 m, 3 aisles, overlapping SKUs at different prices |
| `online_store.yaml` | `OnlineCatalog` | Mock online store with 19 items. Shipping is $2.99, free at $25 or more. The default ETA is 24 h; groceries arrive in 2 h |
| `fixtures/users.yaml` | `FixturesFile` | Fictional demo users `maya`, `jordan`, `sam`: profile, IntentMandate, purchase history, feedback |

SKU ids are uppercase and hyphenated (`SALSA-CHIPOTLE`). All dates are relative to the demo day, **2026-10-17**.

## Store geometry (world frame: meters, x right, y up, origin bottom-left, theta CCW from +x)

**store_ave** (14 x 22):

```
y=22 ┌──────────── FRIDGE-BACK (dairy / meat / deli, faces -y) ─────────────┐
     │                    back_wall corridor y 19.0-21.2                    │
y=19 │W  a1  G1  a2  G2  a3  G3  a4  G4  a5  F│   gondolas G1-G4 are double-sided, 1.2 m deep
     │A      ║       ║       ║       ║       R│   aisles a1-a5 are 1.6 m wide (robot center line = aisle center)
     │L      ║       ║       ║       ║       Z│   WALL-L = household, FREEZER-R = frozen
y=9  │L     [EC-G2]         [EC-G4]           │   front endcaps (promo deals) at y 8.4-9.0
     │   [PRODUCE-A]  [PRODUCE-B]  [PRODUCE-C] │   produce tables y 5.0-6.2, picked from both sides
     │                                         │
     │ ENTRANCE (1.2,1.2)   ▪pillar  [CHECKOUT-1]  checkout pose (11.25, 2.75)
y=0  └─────────────────────────────────────────┘ x=14
```

- Aisle layout along x: WALL-L 0-0.6 | a1 0.6-2.2 | G1 2.2-3.4 | a2 | G2 5.0-6.2 | a3 | G3 7.8-9.0 | a4 | G4 10.6-11.8 | a5 | FREEZER-R 13.4-14.0.
- Aisle contents: a1 Household & Beverages, a2 Snacks & Breakfast, a3 Taco & Mexican + Canned, a4 Pantry & Bakery, a5 Chips, Soda & Frozen.
- Gondola levels are at z = 0.15 / 0.6 / 1.05 / 1.5 m, fridge and freezer levels at 0.3 / 0.75 / 1.2 / 1.6 m, and produce tables at 0.8 m. Most demo items sit on level 1 (within the arm's reach).
- Every `approach_pose` sits in the aisle 0.75-1.0 m off the shelf face and faces the shelf (`theta = atan2(item - approach)`). Every pose has at least 0.75 m of clearance from every fixture, obstacle and wall, against `ROBOT_RADIUS_M` = 0.6. A flood fill on a 0.05 m grid inflated by r + 0.05 reaches every approach pose, the entrance and the checkout pose from the robot's start pose.
- Slot ids follow `<fixture>-<face><level>-<index>`. For example, `G2-E1-03` is gondola 2, east face, level 1, 4th from the left as the robot faces it.

**store_hill** (9 x 14) has the produce table up front, then WALL-L | a1 | G1 | a2 | G2 | a3 (2.0 m wide) | FRIDGE-R on the right wall. It stocks 37 SKUs and carries **no cilantro and no sour cream**, so the online store stays the obvious fallback.

## The demo scenario these files encode

**Trip 1. Maya texts: "tacos for 6, under $40, one vegetarian".** The fake agent expands this to: TORT-CORN x2,
BEEF-GROUND x2, BEANS-BLACK x1, CHEESE-MEX, SALSA-MILD, AVOCADO x3, LIMES x2, ONION-WHITE, CILANTRO, SOUR-CREAM.
The basket comes to about **$37.56**: $34.57 of goods plus $2.99 delivery for the online cilantro. That leaves a
little headroom, so taking a recommendation shows the over-budget approval flow.

| Hook | Where | Why |
|---|---|---|
| **Out of stock → split sourcing** | `CILANTRO` qty 0 at store_ave (slot `PRODUCE-B-S0-02`, no lots); online $1.29, ETA 2 h | Forces an online line on the combined checkout. It also appears as an OOS row on the merchant dashboard |
| **Mispricing** | `BEANS-REFRIED` at store_ave: system $1.99, `shelf_tag_cents` 249 | Merchant dashboard mispricing row (+$0.50) |
| **Near-expiry markdowns** | `GUAC-KIT` lot `ave-GUAC-KIT-L1` best by 10-18, $5.99 → $3.99. `YOGURT-GREEK` L1 10-19, $5.99 → $3.49. `BREAD-SOURDOUGH` L1 10-18, half price | Recommendation 2 is "a guac kit that expires tomorrow, $2 off". It goes only to shoppers likely to want it |
| **Deals** | ENDCAP-G2: `CHIPS-TORTILLA` 25% off ($2.99). ENDCAP-G4: `PAPER-PLATES` markdown $3.99, `COLA` BOGO ($1.50 effective). store_hill: `SALSA-CHIPOTLE` 10% off. Online: `HOTSAUCE-GHOST` 10% off, `CHILE-LIME-SEASONING` BOGO | Recommendation 3 is a deal detour. The endcaps sit off the mandatory path, so taking them is a visible detour |
| **Recommendation 1 (taste)** | `HOTSAUCE-GHOST` (spice 5, on ENDCAP-G2), `SALSA-CHIPOTLE`, `CHIPS-SPICY-LIME` | Maya's `prefs.spicy` is 0.8 and she rated HOTSAUCE-RED 5/5 |
| **Produce freshness top-3** | Every produce slot has 2-4 lots with different `freshness` (for example, AVOCADO 0.95 / 0.82 / 0.58 / 0.31) | Perception ranks lots and shows the shopper the top 3 |
| **Mandate block** | `LAGER-6PK` (category `alcohol`, aisle 1) | Maya's mandate blocks alcohol → `CATEGORY_BLOCKED` if anything tries to add it |
| **Ask-above** | Maya `ask_above_cents` 1500, cap $60, per-item cap $25 | Any line over $15 triggers an approval. The over-cap path denies |

**Follow-up → trip 2.** `SALSA-MILD` is in Maya's `favorite_skus` (her usual, rated **3/5**) and every hot item is
rated 4-5, so trip 1 picks SALSA-MILD. The follow-up "how was the salsa?" → "too mild, loved the hot sauce" is read
as 2/5 + `too_mild`: SALSA-MILD leaves her favorites and becomes disliked, and `spicy` goes 0.8 → 1.0. **Trip 2
picks `SALSA-HOT`** ("Habanero Fire", spice 5), and the recs move from HOTSAUCE-RED / GUAC-KIT / CHIPS-TORTILLA to
`HOTSAUCE-GHOST` / BREAD-SOURDOUGH / COLA (what she just bought is not pushed again). Trip 1's detours are one taste
pick, one near-expiry markdown and one deal, as in the demo script.

**Other users.**
- `jordan` is a budget, deals-driven shopper who likes kid-friendly items and dislikes `extra_hot`. Home store is store_hill. Cap $90, asks above $20.
- `sam` is a gluten-free, peanut-allergic breakfast shopper. Use this user for the batched-shoppers demo with Maya at store_ave. Cap $50, asks above $12, blocks alcohol.
- Phones `+1555555010x` are placeholders. Map them to the real Photon handles at the event.

## How the files feed the pipeline

- **DB seed / catalog** (`arms/catalog/seed.py`, catalog fake):
  - products → product rows
  - each slot → an inventory row keyed by `(store_id, slot_id)`, with price = `price_cents` or the base price, plus its lots
  - slot deals → `Deal` with `deal_id = f"{store_id}:{slot_id}"`
  - online items → the `online` store, with online deal ids `online:<sku>`
  - `shelf_tag_cents` → seeded mispricings
- **Sim scene** (`arms/sim`):
  - fixture rects + `height_m` / `level_z_m` → shelf boxes
  - slot `item_position` = front-center of the item base on the board
  - `asset` gives the prop's primitive, size and label color
  - lots → `ItemInstance.lot_id`, so freshness is per instance
- **Occupancy grid** (`arms/routing`):
  - `bounds` + fixture rects + `obstacles` → 0.05 m grid, inflated by `ROBOT_RADIUS_M`
  - slot `approach_pose`s, `entrance` and `checkout` are the routing nodes
- **Recommender / agent**: product `tags`, `dietary`, `spice_level` and `description` feed the embeddings and filters. `fixtures/users.yaml` seeds profiles, mandates and history.

## Contributing to the data

The flow is the same as CLAUDE.md §5: a short-lived `feat/catalog-<slug>` or `fix/catalog-<slug>` branch and a PR
reviewed by the catalog owner.

1. Edit the YAML by hand. These files are the source, not generated output. Keep money in integer cents, dates as
   ISO dates, and SKUs unique and UPPER-HYPHEN.
2. New product: give it a unique `sku` and a unique, **valid EAN-13**:
   `python -c "from contracts.catalog import ean13_check_digit as c; b='200000001234'; print(b+str(c(b)))"`.
3. New slot: put `item_position` on the fixture's pick-face edge at `z = level_z_m[level]`. Put `approach_pose` in
   the aisle, at least 0.6 m (design for 0.75 m) from every fixture, facing the item. Make `qty` equal the sum of
   the lot `qty` values. A deal's `lot_id` must be one of the slot's lots.
4. Run `uv run python -m utils.data`, then `make check` and `make demo`. A data change that breaks the demo
   scenario above (the OOS item, the mispricing, the markdowns, the salsa ranking) must update this README in the
   same PR.
