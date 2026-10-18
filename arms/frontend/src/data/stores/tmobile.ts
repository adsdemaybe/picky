// Demo wireless store (T-Mobile style) for the "shop anything" story. Fictional data, prices in integer cents.
// Frontend-only fixture for now: when the catalog arm adds data/planograms/store_tmobile.yaml, generate this instead.
// Floor: 10 m × 8 m, entrance bottom-left, accessory wall on the left, audio + wearables on the right,
// two phone display tables in the middle, counter (checkout) bottom-right.
import type { Product, Slot, Store } from "@/lib/data";

type P = Omit<Product, "subcategory" | "unit" | "size" | "perishable" | "shape" | "dims_m" | "dietary"> & { compatible_with?: string[] };

const prod = (p: P): Product & { compatible_with?: string[] } => ({
  subcategory: null,
  unit: "each",
  size: null,
  perishable: false,
  shape: "box",
  dims_m: [0.12, 0.03, 0.18],
  dietary: [],
  ...p,
});

export const tmobileProducts = [
  prod({ sku: "PX9-128", name: "Pixel 9 128 GB", brand: "Google", category: "phone", tags: ["phone", "android", "pixel"], price_cents: 79999, label_color: "#2b2f33" }),
  prod({ sku: "IP16-128", name: "iPhone 16 128 GB", brand: "Apple", category: "phone", tags: ["phone", "ios"], price_cents: 82999, label_color: "#d8d8d8" }),
  prod({ sku: "GS25-128", name: "Galaxy S25 128 GB", brand: "Samsung", category: "phone", tags: ["phone", "android"], price_cents: 79999, label_color: "#4a5a7a" }),
  prod({ sku: "CASE-PX9-CLR", name: "Pixel 9 Clear Case", brand: "Shellcraft", category: "case", tags: ["case", "pixel"], price_cents: 2999, label_color: "#cfe3ea", compatible_with: ["PX9-128"] }),
  prod({ sku: "CASE-PX9-RUG", name: "Pixel 9 Rugged Case", brand: "Armorline", category: "case", tags: ["case", "pixel", "rugged"], price_cents: 4999, label_color: "#3c3f3a", compatible_with: ["PX9-128"] }),
  prod({ sku: "CASE-PX7-BLK", name: "Pixel 7 Slim Case", brand: "Shellcraft", category: "case", tags: ["case", "pixel"], price_cents: 1999, label_color: "#1f2326", compatible_with: ["PX7"] }),
  prod({ sku: "CASE-IP16-MAG", name: "iPhone 16 MagSafe Case", brand: "Shellcraft", category: "case", tags: ["case", "ios"], price_cents: 3999, label_color: "#9cb8d6", compatible_with: ["IP16-128"] }),
  prod({ sku: "GLASS-PX9", name: "Pixel 9 Glass Screen Protector 2-pk", brand: "ClearShield", category: "screen", tags: ["screen", "pixel"], price_cents: 2999, label_color: "#e7f0f4", compatible_with: ["PX9-128"] }),
  prod({ sku: "CHG-20W", name: "20W USB-C Wall Charger", brand: "Voltix", category: "charger", tags: ["charger", "usb-c"], price_cents: 2499, label_color: "#f4f4f2" }),
  prod({ sku: "CHG-45W", name: "45W USB-C Fast Charger", brand: "Voltix", category: "charger", tags: ["charger", "usb-c", "fast"], price_cents: 3999, label_color: "#202020" }),
  prod({ sku: "CBL-USBC-2M", name: "USB-C Braided Cable 2 m", brand: "Voltix", category: "charger", tags: ["cable", "usb-c"], price_cents: 1999, label_color: "#c4472b" }),
  prod({ sku: "BUDS-PRO", name: "Pixel Buds Pro 2", brand: "Google", category: "audio", tags: ["earbuds", "anc"], price_cents: 22999, label_color: "#e9dfcf" }),
  prod({ sku: "BUDS-RUN", name: "SportFit Wireless Earbuds", brand: "Pulse", category: "audio", tags: ["earbuds", "running", "sport"], price_cents: 7999, label_color: "#2f6b45" }),
  prod({ sku: "WATCH-PX3", name: "Pixel Watch 3 (LTE)", brand: "Google", category: "wearable", tags: ["watch", "lte"], price_cents: 39999, label_color: "#3a3d40" }),
  prod({ sku: "GW-5G", name: "5G Home Internet Gateway", brand: "Carrier", category: "home_internet", tags: ["home", "internet"], price_cents: 0, label_color: "#e20074" }),
  prod({ sku: "PB-10K", name: "10,000 mAh Power Bank", brand: "Voltix", category: "charger", tags: ["battery", "travel"], price_cents: 3499, label_color: "#5a6470" }),
];

const fx = (fixture_id: string, kind: string, label: string, x: number, y: number, w: number, h: number, levels = 4) => ({
  fixture_id,
  kind,
  label,
  rect: { x, y, w, h },
  levels,
});

// slot ids follow the grocery convention <fixture>-<face><level>-<index> (the camera view groups by face + level)
const slot = (fixture_id: string, face: string, level: number, index: number, sku: string, ix: number, iy: number, ax: number, ay: number, theta: number, qty = 6): Slot => ({
  slot_id: `${fixture_id}-${face}${level}-${String(index).padStart(2, "0")}`,
  fixture_id,
  aisle_id: fixture_id.toLowerCase(),
  sku,
  level,
  index,
  item_position: { x: ix, y: iy, z: 0.4 + level * 0.4 },
  approach_pose: { x: ax, y: ay, theta },
  facings: 2,
  qty,
  lots: [],
  deal: null,
});

const E = 0; // facing +x
const W = Math.PI; // facing -x
const N = Math.PI / 2; // facing +y

export const tmobileStore: Store = {
  store_id: "store_tmobile",
  name: "T-Mobile, University Way",
  address: "4300 University Way NE, Seattle, WA (demo layout)",
  bounds: { x: 0, y: 0, w: 10, h: 8 },
  entrance: { x: 1.2, y: 0.6, theta: N },
  checkout: { x: 8.2, y: 1.7, theta: -N },
  robot_home: { x: 2.2, y: 1.0, theta: N },
  obstacles: [],
  fixtures: [
    fx("ACC-WALL", "shelf", "Accessory wall: cases, glass, chargers", 0, 2.4, 0.6, 5.0),
    fx("AUDIO-WALL", "shelf", "Audio & power wall", 9.4, 2.4, 0.6, 5.0),
    fx("TABLE-A", "display_table", "Phone table A", 3.0, 3.2, 1.4, 2.6, 1),
    fx("TABLE-B", "display_table", "Phone table B", 5.6, 3.2, 1.4, 2.6, 1),
    fx("WEAR-CASE", "endcap", "Wearables & home internet", 3.6, 7.2, 2.8, 0.6, 3),
    fx("COUNTER", "counter", "Checkout counter", 7.2, 0.4, 2.4, 0.8, 1),
  ],
  aisles: [{ aisle_id: "floor", label: "Sales floor", rect: { x: 0.6, y: 1.2, w: 8.8, h: 6.0 } }],
  slots: [
    slot("ACC-WALL", "E", 3, 0, "CASE-PX9-CLR", 0.6, 3.0, 1.35, 3.0, W, 14),
    slot("ACC-WALL", "E", 3, 1, "CASE-PX9-RUG", 0.6, 3.8, 1.35, 3.8, W, 6),
    slot("ACC-WALL", "E", 2, 0, "CASE-IP16-MAG", 0.6, 3.0, 1.35, 3.0, W, 9),
    slot("ACC-WALL", "E", 2, 1, "CASE-PX7-BLK", 0.6, 3.8, 1.35, 3.8, W, 3),
    slot("ACC-WALL", "E", 1, 2, "GLASS-PX9", 0.6, 4.8, 1.35, 4.8, W, 20),
    slot("ACC-WALL", "E", 0, 3, "CHG-20W", 0.6, 5.8, 1.35, 5.8, W, 12),
    slot("ACC-WALL", "E", 0, 2, "CHG-45W", 0.6, 4.8, 1.35, 4.8, W, 8),
    slot("ACC-WALL", "E", 1, 3, "CBL-USBC-2M", 0.6, 5.8, 1.35, 5.8, W, 25),
    slot("AUDIO-WALL", "W", 2, 0, "BUDS-PRO", 9.4, 3.2, 8.65, 3.2, E, 5),
    slot("AUDIO-WALL", "W", 2, 1, "BUDS-RUN", 9.4, 4.2, 8.65, 4.2, E, 9),
    slot("AUDIO-WALL", "W", 1, 2, "PB-10K", 9.4, 5.2, 8.65, 5.2, E, 11),
    slot("TABLE-A", "N", 0, 0, "PX9-128", 3.7, 4.5, 2.25, 4.5, 0, 4),
    slot("TABLE-A", "N", 0, 1, "IP16-128", 3.7, 4.0, 2.25, 4.0, 0, 4),
    slot("TABLE-B", "N", 0, 0, "GS25-128", 6.3, 4.5, 7.75, 4.5, W, 4),
    slot("WEAR-CASE", "S", 1, 0, "WATCH-PX3", 4.4, 7.2, 4.4, 6.45, N, 3),
    slot("WEAR-CASE", "S", 1, 1, "GW-5G", 5.6, 7.2, 5.6, 6.45, N, 10),
  ],
};

/** Deals live in the store data like the grocery planogram's slot deals. */
for (const s of tmobileStore.slots) {
  if (s.sku === "GLASS-PX9") s.deal = { kind: "percent_off", price_cents: 2399, original_cents: 2999, percent_off: 20, reason: "20% off glass with any Pixel 9 case" };
  if (s.sku === "CASE-PX7-BLK") s.deal = { kind: "clearance", price_cents: 999, original_cents: 1999, percent_off: 50, reason: "Clearance: last-gen case" };
}

/** What Maya owns and her line, used by the "says wait" advice (fictional). */
export const mayaDevices = {
  phone: { sku: "PX7", name: "Pixel 7", age_months: 34, installments_left: 4, monthly_installment_cents: 1650 },
  owns: [
    { sku: "CHG-20W", name: "20W USB-C Wall Charger", bought: "2026-03-14" },
    { sku: "CASE-PX7-BLK", name: "Pixel 7 Slim Case", bought: "2023-12-02" },
  ],
  lines: 2,
};

/** Upcoming promos for wireless items (drives WAIT verdicts). */
export const tmobileUpcoming: Record<string, { starts: string; price_cents: number; reason: string }> = {
  "BUDS-RUN": { starts: "2026-10-23", price_cents: 5999, reason: "Weekend sale: $20 off starts Fri Oct 23" },
  "PX9-128": { starts: "2026-11-27", price_cents: 59999, reason: "Holiday promo expected: $200 off" },
};
