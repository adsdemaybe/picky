// Price intelligence (the "Phia" layer): offers across retailers, price history, good-price verdicts, cheaper swaps.
// Deterministic code: the assistant calls these as tools and only phrases the results.
import offersJson from "@/data/generated/offers.json";
import { fits, fitsOf, mayaDevices, tmobileUpcoming } from "@/data/stores/tmobile";
import { STORES, allProducts, maya, productBySku, slotsOf } from "./data";

export type Offer = {
  retailer_id: string;
  retailer: string;
  kind: "store" | "online" | "warehouse" | "delivery" | "resale" | "carrier";
  price_cents: number;
  regular_cents: number;
  pack: number;
  fee_cents: number;
  fee_note?: string;
  eta: string;
  in_stock: boolean;
  note: string | null;
  condition?: string;
  /** Fictional retailer with a generated price (scripts/gen-data.mjs); shown with a "synthetic" label. */
  synthetic?: boolean;
};

const G = offersJson as unknown as {
  offers: Record<string, Offer[]>;
  history: Record<string, number[]>;
  upcoming: Record<string, { starts: string; price_cents: number; reason: string }>;
};

function hash(s: string) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return (h >>> 0) / 4294967295;
}

export function offersFor(sku: string): Offer[] {
  const p = productBySku[sku];
  if (!p) return [];
  const list = G.offers[sku] ?? [];
  return [...list].sort((a, b) => effective(a) - effective(b));
}

/** Price per single item including delivery fees (pack prices are divided down). */
export function effective(o: Offer) {
  return Math.round((o.price_cents + o.fee_cents) / o.pack);
}

export function historyFor(sku: string): number[] {
  if (G.history[sku]) return G.history[sku];
  const p = productBySku[sku];
  if (!p) return [];
  const pts: number[] = [];
  for (let w = 0; w < 13; w++) {
    const r = hash(`${sku}:${w}`);
    pts.push(Math.round(p.price_cents * (r < 0.12 ? 0.85 : 1 + (r - 0.5) * 0.03)));
  }
  pts[12] = offersFor(sku).find((o) => o.retailer_id === "store_tmobile")?.price_cents ?? p.price_cents;
  return pts;
}

export function upcomingFor(sku: string) {
  return G.upcoming[sku] ?? tmobileUpcoming[sku] ?? null;
}

export type Verdict = { label: "Good price" | "Wait" | "Typical price" | "High right now" | "Skip it"; tone: "basil" | "turmeric" | "neutral" | "tomato"; why: string };

export function verdictFor(sku: string): Verdict {
  const p = productBySku[sku];
  const owned = mayaDevices.owns.find((o) => o.sku === sku);
  if (owned) return { label: "Skip it", tone: "tomato", why: `You already own one (${owned.name}).` };
  if (fitsOf(sku) && !fits(sku, "PX9-128") && !fits(sku, mayaDevices.phone.sku) && p?.subcategory !== "phones")
    return { label: "Skip it", tone: "tomato", why: `Doesn't fit your ${mayaDevices.phone.name} or the Pixel 9 you're looking at.` };
  const h = historyFor(sku);
  const now = h[h.length - 1];
  const low = Math.min(...h);
  const sorted = [...h].sort((a, b) => a - b);
  const median = sorted[Math.floor(sorted.length / 2)];
  const up = upcomingFor(sku);
  if (up && up.price_cents < now) return { label: "Wait", tone: "turmeric", why: `${up.reason} ($${(up.price_cents / 100).toFixed(2)}).` };
  if (now <= low * 1.02 && now < median) return { label: "Good price", tone: "basil", why: `Lowest it's been in 13 weeks (usually $${(median / 100).toFixed(2)}).` };
  if (now > median * 1.06) return { label: "High right now", tone: "tomato", why: `Usually $${(median / 100).toFixed(2)}; it's ${Math.round((now / median - 1) * 100)}% above that.` };
  return { label: "Typical price", tone: "neutral", why: `In line with the 13-week median of $${(median / 100).toFixed(2)}.` };
}

/** Cheaper items that do the same job (same subcategory, or category for wireless accessories). */
export function alternativesFor(sku: string, n = 3) {
  const p = productBySku[sku];
  if (!p) return [];
  const mine = Math.min(...offersFor(sku).map(effective));
  return allProducts
    .filter((q) => q.sku !== sku && (p.subcategory ? q.subcategory === p.subcategory : q.category === p.category))
    .filter((q) => !(maya.profile as unknown as { disliked_skus?: string[] }).disliked_skus?.includes(q.sku))
    .filter((q) => {
      const a = fitsOf(p.sku);
      const b = fitsOf(q.sku);
      return !(a && b && !b.some((c) => a.includes(c)));
    })
    .map((q) => ({ product: q, best: offersFor(q.sku)[0] }))
    .filter((x) => x.best && effective(x.best) < mine)
    .sort((a, b) => effective(a.best) - effective(b.best))
    .slice(0, n);
}

export function searchProducts(q: string, limit = 12) {
  const terms = q.toLowerCase().split(/\s+/).filter(Boolean);
  if (!terms.length) return [];
  return allProducts
    .map((p) => {
      const hay =
        `${p.name} ${p.brand ?? ""} ${p.category} ${p.subcategory ?? ""} ${p.tags.join(" ")} ${(p.ingredients ?? []).join(" ")}`.toLowerCase();
      const score = terms.reduce((s, t) => s + (hay.includes(t) ? (p.name.toLowerCase().includes(t) ? 2 : 1) : 0), 0);
      return { p, score };
    })
    .filter((x) => x.score > 0)
    .sort((a, b) => b.score - a.score)
    .slice(0, limit)
    .map((x) => x.p);
}

/** Where the product sits in a robot-served store, if any. */
export function robotStoreFor(sku: string) {
  if (slotsOf("store_ave")[sku]) return STORES.store_ave;
  if (slotsOf("store_tmobile")[sku]) return STORES.store_tmobile;
  return null;
}
