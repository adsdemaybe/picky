# data/: store data (the source of truth)

These YAML files are the single source of truth for the stores, the fleet and the catalog (CLAUDE.md §4 rule 5).
The DB seed, the Isaac sim scene (every store + every rover + ceiling cameras), the routing occupancy grid, the 2D
robot fake and the web app's store map are all **generated** from them. Fix the data here, never a generated
artifact. The schemas are in `contracts/planogram.py` and `contracts/catalog.py`. Load the files only through
`utils/data.py`.

```bash
uv run python -m utils.data        # validate every file: schema, cross-refs, EAN-13, rover clearance, SO-101 reach,
                                   # slot ids unique across stores, qty/low <= capacity, lot sums, docks, cameras
```

Current state: `data OK` with **0 problems and 0 warnings** (contracts v0.2.0).

| File | Model | What |
|---|---|---|
| `products.yaml` | `ProductsFile` | **157 SKUs**: 122 in-store + 3 `online_only` (hand-written) + 32 `GN-*` store_genie SKUs (the generated block at the end of the file), across all 13 top-level categories. Made-up brands, realistic 2026 US prices and pack sizes in int cents, category + subcategory, tags + dietary + allergens, `spice_level`, valid EAN-13 barcodes (`200…` prefix), `perishable` / `shelf_life_days`, sim `asset` hints (`shape`, `dims_m`, `label_color`, `mass_kg`) |
| `categories.yaml` | `CategoriesFile` | Taxonomy: the 13 `STANDARD_CATEGORY_IDS` (display name, icon, color, aliases, sort = store walk order) + 66 subcategories (one parent each, max depth 2). Where each category sits per store is documented in the file header and derived from the planogram aisles |
| `planograms/store_ave.yaml` | `Planogram` | Main demo store "Picky Market - The Ave", 14 m x 22 m: **124 slots / 122 SKUs / 1331 units** |
| `planograms/store_hill.yaml` | `Planogram` | Second store "Picky Express - Capitol Hill", 9 m x 14 m: **64 slots / 64 SKUs / 589 units**, overlapping SKUs at different prices |
| `planograms/store_genie.yaml` | `Planogram` | **GENERATED** (`scripts/genie_to_planogram.py`, never hand-edit): the AgiBot Genie Sim `supermarket_06` scene as "Picky Market - Genie Sim Supermarket", 8.45 m x 6.75 m: **145 slots / 32 SKUs / 678 units** (boards up to 1.15 m: the white SO-101 rover v3 reaches a 1.2 m TCP) on the scene's real shelving (21 shelf / freezer boxes + the checkout counter the sim adds). Store frame = USD frame + (3.25, 2.10) m |
| `sim/store_genie_prims.yaml` | sim-only | **GENERATED**: unit n of each store_genie slot = a product prim of the scene USD (path + its box in the store frame) for the Isaac worker's external-scene mode (`arms/sim/external.py`) |
| `sim/genie/` | generator inputs | `supermarket_06.json` = the scene dump (1,091 product prims with asset ids + boxes, fixture boxes, Genie's object descriptions, obstacles from a top-down depth render); `store_genie_skus.yaml` = the hand-written naming table (one fictional SKU per Genie product type: made-up brand, US price in cents, category) |
| `online_store.yaml` | `OnlineCatalog` | Mock online store with 19 items. Shipping $2.99, free at $25+. Default ETA 24 h; groceries 2 h |
| `fixtures/users.yaml` | `FixturesFile` | Fictional demo users `maya`, `jordan`, `sam`: profile, IntentMandate, purchase history, feedback |
| `fixtures/robots.yaml` | `RobotsFile` | The fleet: `rover-ave-1` **Pico** + `rover-ave-2` **Rhea** (store_ave), `rover-hill-1` **Hazel** (store_hill), `rover-tmo-1` **Magenta** (store_tmobile), `rover-genie-1` **Juniper** (store_genie), kind `rover_so101`, dock, battery, basket capacity, UI color; ceiling cameras `store_ave.ceiling-1`, `store_ave.ceiling-2`, `store_hill.ceiling-1`, `store_tmobile.ceiling-1`, `store_genie.ceiling-1` |

SKU ids are uppercase and hyphenated (`SALSA-CHIPOTLE`). All dates are relative to the demo day, **2026-10-17**.

## Categories (top-level, `Product.category`)

| id | Name | SKUs | Example subcategories |
|---|---|---|---|
| `produce` | Produce | 17 | fruit, vegetables, herbs, greens |
| `dairy` | Dairy & Eggs | 14 | milk, cheese, yogurt, eggs, butter, cultured, plant_milk, cheese_dip |
| `meat` | Meat & Seafood | 7 | beef, poultry, pork, seafood |
| `deli` | Deli & Prepared | 6 | dips, fresh_salsa, plant_protein, deli_meat |
| `bakery` | Bakery & Tortillas | 7 | bread, tortillas, pastry |
| `pantry` | Pantry | 31 | canned, salsa, hot_sauce, spices, sauces, grains, pasta, baking, cereal, coffee, spreads, soup, oils, taco_shells |
| `international` | International & Mexican | 4 | mexican, asian |
| `snacks` | Snacks | 10 | chips, pretzels, crackers, cookies, bars, popcorn, nuts, candy |
| `beverages` | Beverages | 8 | soda, water, juice, sports, drinks |
| `frozen` | Frozen | 6 | meals, frozen_vegetables, frozen_seafood, dessert |
| `household` | Household | 9 | paper, cleaning, laundry, bags, kitchen |
| `personal_care` | Personal Care | 4 | oral_care, hair_care, bath_body |
| `alcohol` | Beer & Wine | 2 | beer, wine |

The taco-demo SKUs keep their v0.1 categories (salsa / hot sauce / beans are `pantry`, tortillas `bakery`) so the
agent's recipe hints and the recommender scores do not move. Four subcategories were renamed so every subcategory has
exactly one parent: `TACO-SHELLS` → `taco_shells`, `PICO-FRESH` → `fresh_salsa`, `QUESO-DIP` → `cheese_dip`,
`CORN-FROZEN` → `frozen_vegetables`.

## Robot model and geometry rules (contracts.common)

The robot is a small rover (radius `ROBOT_RADIUS_M` = 0.3 m) carrying a vertical lift (0.2-1.2 m) with an SO-101 arm
(mast 0.12 m forward of the base center, reach 0.35 m) and a basket. Every slot obeys:

- `item_position` = front-center of the item base on the fixture's pick-face edge, `z = level_z_m[level]`, within
  **0.1-1.5 m**. Gondola boards: 0.15 / 0.6 / 1.05 / 1.5 m. Fridge + freezer boards: 0.25 / 0.65 / 1.05 / 1.45 m
  (lowered from 1.6 m so the top board is reachable). Produce tables 0.8 m, endcaps 0.3 / 0.8 / 1.2 m.
- `approach_pose` = `item_position` + **0.375 m** along the pick-face normal (design range 0.35-0.40 m), facing the
  shelf (`theta = atan2(item - approach)`). Slots in the same column on different levels share the approach pose.
- Every approach pose, dock, entrance and checkout keeps >= 0.3 m from every fixture, obstacle and wall. Aisles are
  1.6 m (store_hill aisle 3: 2.0 m), so two rovers can pass.
- Checked beyond the validator for this release: a flood fill on a 0.05 m grid inflated by 0.3 m reaches every
  approach pose, dock, entrance and checkout from the start pose; the routing fake's visibility graph has 0
  unreachable stop pairs; and the multi-robot 2D fake (`strict_reach=True`) drove one rover per store through every
  slot, scanned it (instances == slot qty) and picked one unit from every stocked slot.
- Slot ids are unique across stores: `<STORE>-<fixture>-<face><level>-<index>`, e.g. `AVE-G2-E1-04` = store_ave,
  gondola 2, east face, level 1, column 4 (index 0 = leftmost as the robot faces the shelf; shared across levels).
  Store prefixes `AVE` / `HILL`; lot ids `ave-<SKU>-L<n>` / `hil-<SKU>-L<n>`.
- Every slot has an explicit `capacity` and `low_threshold` (= ceil(25% of capacity), the
  `contracts.catalog.default_low_threshold` rule). `qty == sum(lots)`.

## Store layouts (world frame: meters, x right, y up, origin bottom-left, theta CCW from +x)

**store_ave** (14 x 22):

```
y=22 ┌──────── FRIDGE-BACK: meat (left) | deli | dairy (right), faces -y ────────┐
     │                    back_wall corridor y 19.0-21.2                      │
y=19 │W  a1  G1  a2  G2  a3  G3  a4  G4  a5  F│   gondolas G1-G4 are double-sided, 1.2 m deep
     │A      ║       ║       ║       ║       R│   aisles a1-a5 are 1.6 m wide
     │L      ║       ║       ║       ║       Z│   WALL-L = household + personal care, FREEZER-R = frozen
y=9  │L     [EC-G2]         [EC-G4]           │   front endcaps (promo deals) at y 8.4-9.0
     │   [PRODUCE-A]  [PRODUCE-B]  [PRODUCE-C] │   produce tables y 5.0-6.2, picked from both sides
     │                                         │
     │ ENTRANCE (1.2,1.2) Pico▪ Rhea▪ ▪pillar [CHECKOUT-1]  checkout pose (11.25, 2.75)
y=0  └─────────────────────────────────────────┘ x=14
```

- Along x: WALL-L 0-0.6 | a1 0.6-2.2 | G1 2.2-3.4 | a2 | G2 5.0-6.2 | a3 | G3 7.8-9.0 | a4 | G4 10.6-11.8 | a5 | FREEZER-R 13.4-14.0.
- Aisle 1 Household, Personal Care & Drinks (WALL-L + G1 west: water, sports drinks, sparkling, cane cola, lager, wine) ·
  Aisle 2 Snacks & Breakfast (G1 east snacks, G2 west cereal/coffee/peanut butter/honey) · Aisle 3 International &
  Mexican, Canned (G2 east: tortillas, taco shells + seasoning, salsas, hot sauces, enchilada sauce, masa, soy sauce,
  ramen, coconut milk; G3 west: beans, rice, diced tomatoes, broth, tuna) · Aisle 4 Pasta, Baking & Bakery (G3 east,
  G4 west bread/bagels/buns/muffins) · Aisle 5 Chips, Soda & Frozen (G4 east, FREEZER-R) · Back wall Meat, Deli &
  Dairy.
- Docks: Pico (2.6, 1.6) = the planogram `robot_home`; Rhea (3.6, 1.6). Ceiling cameras: `store_ave.ceiling-1` over the
  entrance looking up the store, `store_ave.ceiling-2` in the back-right corner looking down the aisles.

**store_hill** (9 x 14) has the produce table up front, then WALL-L (pantry + canned) | a1 | G1 (taco, Mexican &
international | snacks) | a2 | G2 (breakfast | household, personal care & drinks) | a3 (2.0 m) | FRIDGE-R (meat, deli
& dairy) on the right wall. It carries **no cilantro and no sour cream**, so the online store stays the obvious
fallback. Hazel docks at (3.0, 1.0); `store_hill.ceiling-1` sits over the entrance.

## The demo scenario these files encode

**Trip 1. Maya texts: "tacos for 6, under $40, one vegetarian".** The fake agent expands this to: TORT-CORN x2,
BEEF-GROUND x2, BEANS-BLACK x1, CHEESE-MEX, SALSA-MILD, AVOCADO x3, LIMES x2, ONION-WHITE, CILANTRO, SOUR-CREAM
(sourcing: everything in store at store_ave except CILANTRO → online). The basket comes to about **$37.56**: $34.57
of goods plus $2.99 delivery for the online cilantro. Verified unchanged after the v0.2 data refresh (same list, same
sourcing, same recommender ranking for maya, trip 2 still switches to SALSA-HOT).

| Hook | Where | Why |
|---|---|---|
| **Out of stock → split sourcing** | `CILANTRO` qty 0 at store_ave (slot `AVE-PRODUCE-B-S0-02`, no lots); online $1.29, ETA 2 h | Forces an online line on the combined checkout. Also an OOS row + stock alert for staff |
| **Mispricing** | `BEANS-REFRIED` at store_ave (`AVE-G3-W1-04`): system $1.99, `shelf_tag_cents` 249 | Merchant dashboard mispricing row (+$0.50) |
| **Near-expiry markdowns** | `GUAC-KIT` lot `ave-GUAC-KIT-L1` best by 10-18, $5.99 → $3.99. `YOGURT-GREEK` L1 10-19, $5.99 → $3.49. `BREAD-SOURDOUGH` L1 10-18, half price | Recommendation "a guac kit that expires tomorrow, $2 off". New v0.2 SKUs keep their earliest lot >= 7 days out, so the near-expiry list is still exactly the v0.1 set |
| **Deals** | ENDCAP-G2: `CHIPS-TORTILLA` 25% off ($2.99). ENDCAP-G4: `PAPER-PLATES` markdown $3.99, `COLA` BOGO ($1.50 effective). store_hill: `SALSA-CHIPOTLE` 10% off. Online: `HOTSAUCE-GHOST` 10% off, `CHILE-LIME-SEASONING` BOGO | Deal detours; the endcaps sit off the mandatory path |
| **Recommendation (taste)** | `HOTSAUCE-GHOST` (spice 5, on ENDCAP-G2), `SALSA-CHIPOTLE`, `CHIPS-SPICY-LIME` | Maya's `prefs.spicy` is 0.8 and she rated HOTSAUCE-RED 5/5. No new spicy / taco-tagged SKUs were added, so the ranking is unchanged |
| **Produce freshness top-3** | Every produce slot has 1-4 lots with different `freshness` (e.g. AVOCADO 0.95 / 0.82 / 0.58 / 0.31) | Perception ranks lots and shows the shopper the top 3 |
| **Mandate block** | `LAGER-6PK`, `WINE-RED` (category `alcohol`, aisle 1) | Maya's and Sam's mandates block alcohol → `CATEGORY_BLOCKED` |
| **Ask-above** | Maya `ask_above_cents` 1500, cap $60, per-item cap $25 | Any line over $15 triggers an approval. The over-cap path denies |
| **Staff shelf_scan / stock alerts** | store_ave aisle 2 snacks (G1 east): `PRETZELS` **out** (qty 0, `AVE-G1-E1-00`), `CRACKERS` **low** (2/12), `COOKIES-CHOC-CHIP` **low** (1/12); store_hill `CREAM-CHEESE` **low** (2/10) | Boot-time stock alerts: store_ave = CILANTRO + PRETZELS out, CRACKERS + COOKIES low; store_hill = CREAM-CHEESE low. "Rhea, scan the snacks aisle" / "scan dairy at Hill" demo, then restock |
| **Multi-store split** | PRETZELS are out at the Ave but stocked at the Hill ($4.19) | The fleet can split a list across stores by stock coverage + price |

**Follow-up → trip 2.** `SALSA-MILD` is in Maya's `favorite_skus` (her usual, rated **3/5**) and every hot item is
rated 4-5, so trip 1 picks SALSA-MILD. The follow-up "how was the salsa?" → "too mild, loved the hot sauce" is read
as 2/5 + `too_mild`: SALSA-MILD leaves her favorites and becomes disliked, and `spicy` goes 0.8 → 1.0. **Trip 2
picks `SALSA-HOT`** ("Habanero Fire", spice 5), and the recs move toward `HOTSAUCE-GHOST`.

**Fleet.** Pico (95% battery, docked at the store entrance) wins the deterministic assignment score for store_ave
shop goals over Rhea (81%); Hazel (88%) serves store_hill. Robot names in requests ("Hazel, go charge") map to
`robot_hint`.

**Other users.**
- `jordan` is a budget, deals-driven shopper who likes kid-friendly items and dislikes `extra_hot`. Home store is
  store_hill. Cap $90, asks above $20.
- `sam` is a gluten-free, peanut-allergic breakfast shopper. Use this user for the batched-shoppers demo with Maya at
  store_ave. Cap $50, asks above $12, blocks alcohol.
- Phones `+1555555010x` are placeholders. Map them to the real Photon handles at the event.

## How the files feed the pipeline

- **DB seed / catalog** (`arms/catalog/seed.py`, catalog fake): products → product rows; each slot → an inventory
  row keyed by `(store_id, slot_id)` with price = `price_cents` or the base price, capacity, low threshold and lots;
  slot deals → `Deal` with `deal_id = f"{store_id}:{slot_id}"`; online items → the `online` store (deal ids
  `online:<sku>`); `shelf_tag_cents` → seeded mispricings; `categories.yaml` → the categories API (counts, aisle
  labels from `Aisle.categories`).
- **Fleet** (`arms/fleet`): `fixtures/robots.yaml` → robots per store, docks (return_to_dock target), battery at boot.
- **Sim scene** (`arms/sim`, Isaac on vhagar): every store under `/World/Stores/<store_id>`; fixture rects +
  `height_m` / `level_z_m` → shelf boxes; slot `item_position` + product `asset` → textured props; lots →
  `ItemInstance.lot_id`; robots spawn at their docks; ceiling cameras from `store_cameras`.
- **2D robot fake** (`arms/robot`): the same robots + cameras, Pillow frames for `<robot_id>.front|wrist|basket` and
  `<store_id>.ceiling-<n>`.
- **Occupancy grid** (`arms/routing`): `bounds` + fixture rects + `obstacles` → 0.05 m grid inflated by
  `ROBOT_RADIUS_M`; slot `approach_pose`s, `entrance` and `checkout` are the routing nodes.
- **Recommender / agent**: product `tags`, `dietary`, `spice_level` and `description` feed the embeddings and filters;
  `fixtures/users.yaml` seeds profiles, mandates and history.

## Contributing to the data

Same flow as CLAUDE.md §5: a short-lived `feat/catalog-<slug>` or `fix/catalog-<slug>` branch and a PR reviewed by
the catalog owner.

1. Edit the YAML by hand. These files are the source, not generated output. Keep money in integer cents, dates as
   ISO dates, and SKUs unique and UPPER-HYPHEN.
2. New product: unique `sku`, top-level `category` from `categories.yaml`, a `subcategory` that is a child of it (add
   it to `categories.yaml` if new), and a unique, **valid EAN-13**:
   `python -c "from contracts.catalog import ean13_check_digit as c; b='200000002400'; print(b+str(c(b)))"`.
3. New slot: id `<STORE>-<fixture>-<face><level>-<index>`; `item_position` on the fixture's pick-face edge at
   `z = level_z_m[level]`; `approach_pose` 0.375 m out along the face normal, facing the item; no other slot on the
   same face + level within 0.5 m; `qty <= capacity`, `low_threshold = ceil(capacity / 4)`, `qty == sum(lot qty)`;
   a deal's `lot_id` must be one of the slot's lots.
4. Run `uv run python -m utils.data`, then `make check` and `make demo`. A data change that breaks the demo scenario
   above (the OOS item, the mispricing, the markdowns, the salsa ranking) must update this README in the same PR.

## Known limitations

- `CategoryDef` has no aisle field, so "which aisles" per category lives in the planogram (`Aisle.categories`) and in
  the `categories.yaml` header comment; the catalog derives `CategoryInfo.aisle_labels` from the planogram.
- The demo SKUs for salsa / hot sauce / beans stay under `pantry` (not `international`) on purpose (agent recipe
  category hints + recommender scores); the International & Mexican aisle shows them through aisle 3's categories.
- Weighted goods are fixed packs (`unit: pack`, e.g. "1.5 lb"), never sold by weight (qty is an integer everywhere).
- store_hill has no freezer.
