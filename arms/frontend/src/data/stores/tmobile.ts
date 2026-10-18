// T-Mobile wireless store (T-Mobile track: Cart Whisperer), generated from the repo's source of truth:
//   data/planograms/store_tmobile.yaml (91 real products at t-mobile.com prices, Oct 17 2026; demo floor plan)
//   data/fixtures/wireless.yaml (SYNTHETIC shopper account + promo calendar, REAL line offers)
// via scripts/gen-data.mjs. Never hand-edit the generated JSON; change the YAML and run `pnpm gen-data`.
import productsJson from "@/data/generated/products.json";
import storeJson from "@/data/generated/store_tmobile.json";
import wirelessJson from "@/data/generated/wireless.json";
import type { Product, Store } from "@/lib/data";

export type WirelessProduct = Product & { compatible_with?: string[]; requires?: "ios" | "android" | null; description?: string | null };

export const tmobileStore = storeJson as unknown as Store;
const onFloor = new Set(tmobileStore.slots.map((s) => s.sku));
export const tmobileProducts = (productsJson as unknown as WirelessProduct[]).filter((p) => onFloor.has(p.sku));

type Account = {
  user_id: string;
  household_lines: number;
  phone: { sku: string; name: string; platform: "android" | "ios"; purchased: string; age_months: number; installments_left: number; monthly_installment_cents: number; payoff_cents: number };
  owned: { sku: string; bought: string; purchase_id?: string; fits: string[]; note?: string }[];
};
export type LinePromo = {
  sku?: string;
  categories?: string[];
  headline: string;
  credit_cents?: number;
  effective_price_cents?: number;
  percent_off?: number;
  min_items?: number;
  online_only?: boolean;
  requires?: string;
};
export const wireless = wirelessJson as unknown as {
  as_of: string;
  accounts: Account[];
  upcoming_promos: { sku: string; starts: string; price_cents: number; was_cents: number; reason: string; synthetic?: boolean }[];
  line_promos: LinePromo[];
  online_sale_prices: { sku: string; price_cents: number }[];
  sources: string[];
};

const acct = wireless.accounts.find((a) => a.user_id === "maya")!;
const nameOf = (sku: string) => tmobileProducts.find((p) => p.sku === sku)?.name ?? sku;

/** What Maya owns and her line (synthetic), used by Cart Whisperer's SKIP / WAIT. */
export const mayaDevices = {
  phone: {
    sku: acct.phone.sku,
    name: acct.phone.name.replace(/^Google /, ""),
    platform: acct.phone.platform,
    purchased: acct.phone.purchased,
    age_months: acct.phone.age_months,
    installments_left: acct.phone.installments_left,
    monthly_installment_cents: acct.phone.monthly_installment_cents,
    payoff_cents: acct.phone.payoff_cents,
  },
  owns: acct.owned.map((o) => ({ sku: o.sku, name: nameOf(o.sku), bought: o.bought, fits: o.fits, note: o.note ?? null })),
  lines: acct.household_lines,
};

/** Upcoming promos for wireless items (drives WAIT verdicts). Synthetic promo calendar. */
export const tmobileUpcoming: Record<string, { starts: string; price_cents: number; reason: string }> = Object.fromEntries(
  wireless.upcoming_promos.map((u) => [u.sku, { starts: u.starts, price_cents: u.price_cents, reason: u.reason }]),
);

/** Real t-mobile.com line offers (bill credits that need a new line / port-in / upgrade), by SKU. */
export const linePromoFor = (sku: string) => wireless.line_promos.find((p) => p.sku === sku) ?? null;
export const onlineSaleFor = (sku: string) => wireless.online_sale_prices.find((p) => p.sku === sku)?.price_cents ?? null;
