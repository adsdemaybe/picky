// Generates the app's demo data from the repo's source-of-truth YAML (CLAUDE.md rule 5).
//   data/products.yaml, data/planograms/{store_ave,store_tmobile}.yaml, data/online_store.yaml,
//   data/fixtures/{users,wireless}.yaml
//   -> src/data/generated/{products,store_ave,store_tmobile,wireless,online,maya}.json
// Purchase history beyond the fixture trips is synthesized deterministically (seeded) and flagged synthetic=true.
// Run: pnpm gen-data   (never hand-edit the output)

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parse } from "yaml";

const here = dirname(fileURLToPath(import.meta.url));
const repo = join(here, "..", "..", "..");
const out = join(here, "..", "src", "data", "generated");
mkdirSync(out, { recursive: true });

const load = (p) => parse(readFileSync(join(repo, p), "utf8"));
const write = (name, obj) => writeFileSync(join(out, name), JSON.stringify(obj, null, 1) + "\n");

// ---- products ----
const { products } = load("data/products.yaml");
write(
  "products.json",
  products.map((p) => ({
    sku: p.sku,
    name: p.name,
    brand: p.brand ?? null,
    category: p.category,
    subcategory: p.subcategory ?? null,
    tags: p.tags ?? [],
    dietary: p.dietary ?? [],
    price_cents: p.price_cents,
    unit: p.unit ?? null,
    size: p.size ?? null,
    perishable: !!p.perishable,
    label_color: p.asset?.label_color ?? "#cccccc",
    shape: p.asset?.shape ?? "box",
    dims_m: p.asset?.dims_m ?? [0.15, 0.08, 0.2],
    description: p.description ?? null,
    // accessories carry `fits:<sku>` tags (lowercase); the app compares against upper-case SKUs
    compatible_with: (p.tags ?? []).filter((t) => t.startsWith("fits:")).map((t) => t.slice(5).toUpperCase()),
    requires: (p.tags ?? []).includes("requires_ios") ? "ios" : (p.tags ?? []).includes("requires_android") ? "android" : null,
  })),
);
const bySku = Object.fromEntries(products.map((p) => [p.sku, p]));

// ---- store layouts ----
const storeJson = (store) => ({
  store_id: store.store_id,
  name: store.name,
  address: store.address,
  bounds: store.bounds,
  entrance: store.entrance,
  checkout: store.checkout,
  robot_home: store.robot_home,
  obstacles: store.obstacles ?? [],
  fixtures: store.fixtures.map((f) => ({
    fixture_id: f.fixture_id,
    kind: f.kind,
    label: f.label,
    rect: f.rect,
    levels: f.levels,
  })),
  aisles: store.aisles.map((a) => ({ aisle_id: a.aisle_id, label: a.label, rect: a.rect })),
  slots: store.slots.map((s) => ({
    slot_id: s.slot_id,
    fixture_id: s.fixture_id,
    aisle_id: s.aisle_id,
    sku: s.sku,
    level: s.level,
    index: s.index,
    item_position: s.item_position,
    approach_pose: s.approach_pose,
    facings: s.facings ?? 1,
    qty: s.qty ?? 0,
    lots: (s.lots ?? []).map((l) => ({ lot_id: l.lot_id, qty: l.qty, expiry: l.expiry ?? null, freshness: l.freshness ?? null })),
    deal: s.deal ?? null,
  })),
});
const store = load("data/planograms/store_ave.yaml");
write("store_ave.json", storeJson(store));
const tmo = load("data/planograms/store_tmobile.yaml");
write("store_tmobile.json", storeJson(tmo));

// ---- wireless account signals + promo calendar (Cart Whisperer, T-Mobile track) ----
const wireless = load("data/fixtures/wireless.yaml");
write("wireless.json", {
  as_of: wireless.as_of,
  accounts: wireless.accounts,
  upcoming_promos: wireless.upcoming_promos ?? [],
  line_promos: wireless.line_promos ?? [],
  online_sale_prices: wireless.online_sale_prices ?? [],
  purchases: load("data/fixtures/users.yaml").users.find((u) => u.user_id === "maya").purchases.filter((p) => p.store_id === "store_tmobile"),
  sources: wireless.sources ?? [],
});

// ---- online store ----
const online = load("data/online_store.yaml");
write("online.json", {
  store_id: online.store_id,
  name: online.name,
  shipping_cents: online.shipping_cents,
  free_shipping_over_cents: online.free_shipping_over_cents,
  items: online.items.map((i) => ({
    sku: i.sku,
    price_cents: i.price_cents ?? bySku[i.sku]?.price_cents,
    qty: i.qty,
    eta_hours: i.eta_hours ?? online.default_eta_hours,
  })),
});

// ---- maya: profile, mandate, purchases (+ seeded history) ----
const { users } = load("data/fixtures/users.yaml");
const maya = users.find((u) => u.user_id === "maya");

function mulberry32(seed) {
  return () => {
    seed |= 0;
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}
const rand = mulberry32(20261017);

// [sku, every N days, qty range, probability when due]
const staples = [
  ["LIMES", 9, [3, 5], 0.95],
  ["AVOCADO", 7, [2, 4], 0.9],
  ["TORT-CORN", 12, [1, 1], 0.9],
  ["BEANS-BLACK", 10, [1, 3], 0.85],
  ["BANANAS", 7, [1, 1], 0.8],
  ["OAT-MILK", 10, [1, 1], 0.9],
  ["EGGS", 11, [1, 1], 0.8],
  ["COFFEE", 24, [1, 1], 0.95],
  ["CHEESE-MEX", 14, [1, 1], 0.7],
  ["ONION-WHITE", 9, [1, 2], 0.7],
  ["TOMATO-ROMA", 8, [1, 1], 0.6],
  ["HOTSAUCE-RED", 35, [1, 1], 0.9],
  ["SOYRIZO", 16, [1, 1], 0.6],
  ["GRANOLA", 18, [1, 1], 0.6],
  ["BREAD-WHEAT", 9, [1, 1], 0.5],
  ["DISH-SOAP", 40, [1, 1], 0.9],
  ["PAPER-TOWELS", 30, [1, 1], 0.9],
  ["SPARKLING-LIME", 14, [1, 1], 0.6],
  ["CHIPS-SPICY-LIME", 15, [1, 1], 0.5],
  ["YOGURT-GREEK", 12, [1, 1], 0.5],
];
const onlineSkus = new Set(online.items.map((i) => i.sku));

const DAY = 86400000;
const fixtureDates = new Set(maya.purchases.map((p) => p.ts.slice(0, 10)));
const lastBought = {};
const purchases = [];
// Weekly-ish trips from mid-July to Oct 14, skipping fixture trip dates.
let t = Date.parse("2026-07-18T18:00:00Z");
let n = 0;
while (t < Date.parse("2026-10-15T00:00:00Z")) {
  const date = new Date(t).toISOString().slice(0, 10);
  if (!fixtureDates.has(date)) {
    n += 1;
    const tripId = `trip_maya_${date.slice(5).replace("-", "")}`;
    const viaOnline = rand() < 0.18;
    const storeId = viaOnline ? "online" : "store_ave";
    const trip = [];
    for (const [sku, every, [lo, hi], p] of staples) {
      const since = lastBought[sku] == null ? Infinity : (t - lastBought[sku]) / DAY;
      if (since + 1 < every || rand() > p) continue;
      if (viaOnline && !onlineSkus.has(sku)) continue;
      const base = bySku[sku].price_cents;
      const wobble = rand() < 0.2 ? -Math.round(base * 0.15) : rand() < 0.15 ? Math.round(base * 0.08) : 0;
      trip.push({
        purchase_id: `pur_maya_s${String(n).padStart(2, "0")}${String(trip.length + 1).padStart(2, "0")}`,
        user_id: "maya",
        sku,
        qty: lo + Math.floor(rand() * (hi - lo + 1)),
        unit_price_cents: base + wobble,
        store_id: storeId,
        trip_id: tripId,
        ts: new Date(t).toISOString().replace(".000", ""),
        rating: rand() < 0.25 ? 3 + Math.floor(rand() * 3) : null,
        synthetic: true,
      });
    }
    // Tiny baskets read as noise in the ledger; fold them into the next trip instead.
    if (trip.length >= 3) {
      for (const p of trip) lastBought[p.sku] = t;
      purchases.push(...trip);
    }
  } else {
    for (const p of maya.purchases.filter((q) => q.ts.startsWith(date))) lastBought[p.sku] = Date.parse(p.ts);
  }
  t += (6 + Math.floor(rand() * 4)) * DAY;
}
// T-Mobile purchases belong to the separate T-Mobile site (wireless.json), not the grocery history
for (const p of maya.purchases.filter((q) => q.store_id !== "store_tmobile")) purchases.push({ ...p, rating: p.rating ?? null, synthetic: false });
purchases.sort((a, b) => a.ts.localeCompare(b.ts));

write("maya.json", { profile: maya.profile, mandate: maya.mandate, purchases });

console.log(
  `gen-data: ${products.length} products, ${store.slots.length} + ${tmo.slots.length} slots, ${purchases.length} purchases (${purchases.filter((p) => p.synthetic).length} synthetic) -> ${out}`,
);

// ---- retailer offers + price history (Find / price check) ----
// Two real demo stores + Picky Online come from the YAML. Two more retailers are synthetic and fictional:
// a warehouse club (cheaper per unit, sold in 2-packs, membership) and a delivery app (markup + fee).
const hill = load("data/planograms/store_hill.yaml");
const prand = mulberry32(4242);
const aveSlot = Object.fromEntries(store.slots.map((s) => [s.sku, s]));
const hillSlot = Object.fromEntries(hill.slots.map((s) => [s.sku, s]));
const onlineItem = Object.fromEntries(online.items.map((i) => [i.sku, i]));
const offers = {};
const history = {};
const upcoming = {};
for (const p of products) {
  const list = [];
  const a = aveSlot[p.sku];
  if (a)
    list.push({
      retailer_id: "store_ave",
      retailer: "Picky Market, The Ave",
      kind: "store",
      price_cents: a.deal?.price_cents ?? a.price_cents ?? p.price_cents,
      regular_cents: a.deal ? (a.deal.original_cents ?? p.price_cents) : (a.price_cents ?? p.price_cents),
      pack: 1,
      fee_cents: 0,
      eta: "robot picks today",
      in_stock: (a.qty ?? 0) > 0,
      note: a.deal?.reason ?? null,
    });
  const h = hillSlot[p.sku];
  if (h)
    list.push({
      retailer_id: "store_hill",
      retailer: "Picky Express, Capitol Hill",
      kind: "store",
      price_cents: h.deal?.price_cents ?? h.price_cents ?? p.price_cents,
      regular_cents: h.price_cents ?? p.price_cents,
      pack: 1,
      fee_cents: 0,
      eta: "1.6 km away",
      in_stock: (h.qty ?? 1) > 0,
      note: h.deal?.reason ?? null,
    });
  const o = onlineItem[p.sku];
  if (o)
    list.push({
      retailer_id: "online",
      retailer: "Picky Online",
      kind: "online",
      price_cents: o.deal?.price_cents ?? o.price_cents ?? p.price_cents,
      regular_cents: o.price_cents ?? p.price_cents,
      pack: 1,
      fee_cents: online.shipping_cents,
      fee_note: `$${(online.shipping_cents / 100).toFixed(2)} delivery under $${online.free_shipping_over_cents / 100}`,
      eta: `${o.eta_hours ?? online.default_eta_hours} h`,
      in_stock: (o.qty ?? 1) > 0,
      note: o.deal?.reason ?? null,
    });
  if (!p.perishable || ["dairy", "pantry", "household", "snacks", "beverages"].includes(p.category)) {
    const unit = Math.round(p.price_cents * (0.78 + prand() * 0.08));
    list.push({
      retailer_id: "cascade",
      retailer: "Cascade Bulk Co.",
      kind: "warehouse",
      price_cents: unit * 2,
      regular_cents: unit * 2,
      pack: 2,
      fee_cents: 0,
      fee_note: "Sold as a 2-pack, $60 a year membership",
      eta: "8 km away",
      in_stock: true,
      note: null,
    });
  }
  list.push({
    retailer_id: "errand",
    retailer: "Errand Express",
    kind: "delivery",
    price_cents: Math.round((p.price_cents * 1.14) / 10) * 10 + 9,
    regular_cents: Math.round((p.price_cents * 1.14) / 10) * 10 + 9,
    pack: 1,
    fee_cents: 399,
    fee_note: "$3.99 delivery + 14% markup",
    eta: "~1 h",
    in_stock: true,
    note: null,
  });
  offers[p.sku] = list;

  // 13 weekly prices at The Ave ending today; occasional sales, slight drift.
  const base = a?.price_cents ?? p.price_cents;
  const pts = [];
  for (let w = 12; w >= 1; w--) {
    const sale = prand() < 0.16;
    const drift = 1 + (prand() - 0.5) * 0.06;
    pts.push(Math.round(base * drift * (sale ? 0.78 + prand() * 0.1 : 1)));
  }
  pts.push(list[0]?.retailer_id === "store_ave" ? list[0].price_cents : base);
  history[p.sku] = pts;
  if (!a?.deal && prand() < 0.14) {
    upcoming[p.sku] = { starts: "2026-10-23", price_cents: Math.round(base * 0.8), reason: "Weekly ad: 20% off starts Fri Oct 23" };
  }
}
// Demo hook: a staple Maya buys often goes on sale next week (drives a "wait" verdict in the demo).
upcoming["COFFEE"] = { starts: "2026-10-23", price_cents: 799, reason: "Weekly ad: $2 off coffee starts Fri Oct 23" };
write("offers.json", { offers, history, upcoming });
console.log(`gen-data: offers for ${Object.keys(offers).length} products, ${Object.keys(upcoming).length} upcoming sales`);
