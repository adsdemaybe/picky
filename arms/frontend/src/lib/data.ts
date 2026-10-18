// Typed access to the generated demo data (scripts/gen-data.mjs) plus derived views for the Track screens.
import productsJson from "@/data/generated/products.json";
import storeJson from "@/data/generated/store_ave.json";
import onlineJson from "@/data/generated/online.json";
import mayaJson from "@/data/generated/maya.json";
import { tmobileProducts, tmobileStore } from "@/data/stores/tmobile";

export type Pose = { x: number; y: number; theta?: number };
export type Rect = { x: number; y: number; w: number; h: number };

export type Product = {
  sku: string;
  name: string;
  brand: string | null;
  category: string;
  subcategory: string | null;
  tags: string[];
  dietary: string[];
  price_cents: number;
  unit: string | null;
  size: string | null;
  perishable: boolean;
  label_color: string;
  shape: string;
  dims_m: number[];
};

export type Lot = { lot_id: string; qty: number; expiry: string | null; freshness: number | null };
export type Deal = {
  kind: string;
  price_cents: number;
  original_cents: number;
  percent_off?: number;
  reason: string;
  valid_until?: string;
};
export type Slot = {
  slot_id: string;
  fixture_id: string;
  aisle_id: string;
  sku: string;
  level: number;
  index: number;
  item_position: { x: number; y: number; z: number };
  approach_pose: Pose;
  facings: number;
  qty: number;
  lots: Lot[];
  deal: Deal | null;
};
export type Fixture = { fixture_id: string; kind: string; label: string; rect: Rect; levels: number };
export type Store = {
  store_id: string;
  name: string;
  address: string;
  bounds: Rect;
  entrance: Pose;
  checkout: Pose;
  robot_home: Pose;
  obstacles: Rect[];
  fixtures: Fixture[];
  aisles: { aisle_id: string; label: string; rect: Rect }[];
  slots: Slot[];
};

export type Purchase = {
  purchase_id: string;
  user_id: string;
  sku: string;
  qty: number;
  unit_price_cents: number;
  store_id: string;
  trip_id: string;
  ts: string;
  rating: number | null;
  synthetic: boolean;
};

export const products = productsJson as Product[];
export const store = storeJson as Store;
export const online = onlineJson as {
  store_id: string;
  name: string;
  shipping_cents: number;
  free_shipping_over_cents: number;
  items: { sku: string; price_cents: number; qty: number; eta_hours: number }[];
};
export const maya = mayaJson as unknown as {
  profile: {
    user_id: string;
    display_name: string;
    household_size: number;
    prefs: Record<string, number>;
    favorite_skus: string[];
    default_budget_cents: number;
    home_store_id: string;
  };
  mandate: {
    mandate_id: string;
    cap_cents: number;
    per_item_cap_cents: number;
    ask_above_cents: number;
    blocked_categories: string[];
    allow_online: boolean;
    require_checkout_approval: boolean;
    expires_at: string;
  };
  purchases: Purchase[];
};

export type StoreId = "store_ave" | "store_tmobile";
export const STORES: Record<StoreId, Store & { kind: "grocery" | "wireless"; short: string }> = {
  store_ave: { ...store, kind: "grocery", short: "Picky Market" },
  store_tmobile: { ...tmobileStore, kind: "wireless", short: "T-Mobile" },
};

/** Every product the assistant can help with: groceries + wireless store. */
export const allProducts: (Product & { compatible_with?: string[] })[] = [...products, ...tmobileProducts];
export const productBySku: Record<string, Product & { compatible_with?: string[] }> = Object.fromEntries(allProducts.map((p) => [p.sku, p]));
/** Grocery store slots (store_ave). Use slotsOf() for any store. */
export const slotBySku: Record<string, Slot> = Object.fromEntries(store.slots.map((s) => [s.sku, s]));
const slotIdx: Record<string, Record<string, Slot>> = {};
export function slotsOf(storeId: StoreId): Record<string, Slot> {
  return (slotIdx[storeId] ??= Object.fromEntries(STORES[storeId].slots.map((s) => [s.sku, s])));
}

// Runtime registration for gateway data the generated demo JSON doesn't have yet (lib/gateway/tripSource.ts):
// products the gateway sends that aren't in data/ on this branch, and stores other than the demo ones.
export function registerProduct(p: Product) {
  productBySku[p.sku] = p;
}
export function registerStore(planogram: { store_id: string; name: string; entrance: Pose; robot_home?: Pose | null }, kind: "grocery" | "wireless" = "grocery") {
  const id = planogram.store_id as StoreId;
  STORES[id] = { ...(planogram as unknown as Store), robot_home: planogram.robot_home ?? planogram.entrance, kind, short: planogram.name };
  delete slotIdx[id];
}
export const isWireless = (sku: string) => tmobileProducts.some((p) => p.sku === sku);

/** The demo's "today" (Sat Oct 17, 2026). Fixed so prerendering is deterministic. */
export const TODAY = new Date("2026-10-17T12:00:00-07:00");
const DAY = 86_400_000;

export const MONTHLY_BUDGET_CENTS = 32_000;

// ---------- chart categories (≤5, fixed order: color follows the category) ----------
export const CATS = [
  { id: "produce", label: "Produce", color: "var(--color-cat-1)" },
  { id: "pantry", label: "Pantry & bakery", color: "var(--color-cat-2)" },
  { id: "dairy", label: "Dairy, meat & deli", color: "var(--color-cat-3)" },
  { id: "snacks", label: "Snacks & drinks", color: "var(--color-cat-4)" },
  { id: "home", label: "Home & tech", color: "var(--color-cat-5)" },
] as const;
export type CatId = (typeof CATS)[number]["id"];

export function catOf(category: string): CatId {
  switch (category) {
    case "produce":
      return "produce";
    case "pantry":
    case "bakery":
      return "pantry";
    case "dairy":
    case "meat":
    case "deli":
    case "frozen":
      return "dairy";
    case "snacks":
    case "beverages":
    case "alcohol":
      return "snacks";
    default:
      return "home";
  }
}

// ---------- trips ----------
export type TripSummary = {
  trip_id: string;
  ts: string;
  store_id: string;
  lines: Purchase[];
  total_cents: number;
  items: number;
  synthetic: boolean;
};

export function tripsFrom(purchases: Purchase[]): TripSummary[] {
  const by = new Map<string, Purchase[]>();
  for (const p of purchases) {
    const arr = by.get(p.trip_id) ?? [];
    arr.push(p);
    by.set(p.trip_id, arr);
  }
  return [...by.entries()]
    .map(([trip_id, lines]) => ({
      trip_id,
      ts: lines[0].ts,
      // a robot trip with online add-ons still counts as an in-store trip
      store_id: lines.find((l) => l.store_id !== "online")?.store_id ?? lines[0].store_id,
      lines,
      total_cents: lines.reduce((s, l) => s + l.qty * l.unit_price_cents, 0),
      items: lines.reduce((s, l) => s + l.qty, 0),
      synthetic: lines.every((l) => l.synthetic),
    }))
    .sort((a, b) => b.ts.localeCompare(a.ts));
}

export function monthSpend(trips: TripSummary[], month = TODAY.getMonth(), year = TODAY.getFullYear()): number {
  return trips
    .filter((t) => {
      const d = new Date(t.ts);
      return d.getMonth() === month && d.getFullYear() === year;
    })
    .reduce((s, t) => s + t.total_cents, 0);
}

/** Weekly spend by category for the last `weeks` weeks (Mon-start), oldest first. */
export function weeklyByCategory(purchases: Purchase[], weeks = 12) {
  const end = startOfWeek(TODAY).getTime() + 7 * DAY;
  const start = end - weeks * 7 * DAY;
  const rows = Array.from({ length: weeks }, (_, i) => ({
    week: new Date(start + i * 7 * DAY),
    total: 0,
    byCat: Object.fromEntries(CATS.map((c) => [c.id, 0])) as Record<CatId, number>,
  }));
  for (const p of purchases) {
    const t = Date.parse(p.ts);
    if (t < start || t >= end) continue;
    const i = Math.floor((t - start) / (7 * DAY));
    const cents = p.qty * p.unit_price_cents;
    const c = catOf(productBySku[p.sku]?.category ?? "other");
    rows[i].byCat[c] += cents;
    rows[i].total += cents;
  }
  return rows;
}

function startOfWeek(d: Date) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  const dow = (x.getDay() + 6) % 7;
  return new Date(x.getTime() - dow * DAY);
}

// ---------- running low (purchase cadence) ----------
export type RunningLow = { sku: string; every_days: number; since_days: number; due_in_days: number; last_qty: number };

export function runningLow(purchases: Purchase[]): RunningLow[] {
  const bySku = new Map<string, Purchase[]>();
  for (const p of purchases) bySku.set(p.sku, [...(bySku.get(p.sku) ?? []), p]);
  const out: RunningLow[] = [];
  for (const [sku, ps] of bySku) {
    if (ps.length < 3) continue;
    const times = ps.map((p) => Date.parse(p.ts)).sort((a, b) => a - b);
    const gaps = times.slice(1).map((t, i) => (t - times[i]) / DAY);
    const every = Math.round(gaps.reduce((s, g) => s + g, 0) / gaps.length);
    const since = Math.floor((TODAY.getTime() - times[times.length - 1]) / DAY);
    out.push({ sku, every_days: every, since_days: since, due_in_days: every - since, last_qty: ps[ps.length - 1].qty });
  }
  return out.filter((r) => r.due_in_days <= 2).sort((a, b) => a.due_in_days - b.due_in_days);
}

export function mostBought(purchases: Purchase[], n = 8) {
  const m = new Map<string, { qty: number; spend: number; times: number; ratings: number[] }>();
  for (const p of purchases) {
    const r = m.get(p.sku) ?? { qty: 0, spend: 0, times: 0, ratings: [] };
    r.qty += p.qty;
    r.spend += p.qty * p.unit_price_cents;
    r.times += 1;
    if (p.rating) r.ratings.push(p.rating);
    m.set(p.sku, r);
  }
  return [...m.entries()]
    .map(([sku, r]) => ({
      sku,
      ...r,
      avg_unit_cents: Math.round(r.spend / r.qty),
      avg_rating: r.ratings.length ? r.ratings.reduce((s, x) => s + x, 0) / r.ratings.length : null,
    }))
    .sort((a, b) => b.times - a.times || b.spend - a.spend)
    .slice(0, n);
}

export function fmtDate(ts: string | Date, opts: Intl.DateTimeFormatOptions = { month: "short", day: "numeric" }) {
  return new Date(ts).toLocaleDateString("en-US", { timeZone: "America/Los_Angeles", ...opts });
}

export function storeName(id: string) {
  return id === "online" ? "Picky Online" : id === "store_ave" ? "The Ave" : id === "store_tmobile" ? "T-Mobile" : id;
}
