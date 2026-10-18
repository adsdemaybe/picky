// T-Mobile-style wireless store (store_tmobile). Nothing here is hand-written data: the store map, products, prices,
// stock and deals are generated from data/planograms/store_tmobile.yaml + data/products.yaml, and the wireless rules
// (plans, promos, trade-in values, compatibility, demo accounts) from data/wireless.yaml (scripts/gen-data.mjs).
// The gateway serves the same files, so demo mode and gateway mode show the same store. All prices are synthetic.
import productsJson from "@/data/generated/products.json";
import storeJson from "@/data/generated/store_tmobile.json";
import wirelessJson from "@/data/generated/wireless.json";
import type { Product, Store } from "@/lib/data";

/** Shape of data/wireless.yaml as generated (contracts/wireless.py WirelessFile). Read-only demo fallback: every
 *  price or estimate the app shows comes from the gateway's /api/wireless routes, never computed from this. */
export type WirelessData = {
  store_id: string;
  today: string;
  tax_rate_bps: number;
  service_taxes_fees_bps: number;
  new_line_fee_cents: number;
  financing: { months_options: number[]; apr_bps: number; max_financed_cents: number; min_financed_cents: number; financeable_subcategories: string[] };
  plans: {
    plan_id: string;
    name: string;
    tier: string;
    summary: string;
    line_prices_cents: number[];
    extra_line_cents: number;
    max_lines: number;
    data: string;
    priority_data_gb: number | null;
    hotspot_gb: number | null;
    perks: string[];
    taxes_and_fees_included?: boolean;
    requires?: string[];
  }[];
  promotions: {
    promo_id: string;
    name: string;
    kind: string;
    skus?: string[];
    with_skus?: string[];
    credit_cents?: number;
    percent_off?: number;
    credit_months?: number;
    monthly_cents?: number;
    requires?: string[];
    starts?: string;
    ends?: string;
    terms?: string;
  }[];
  trade_in_models: { model_id: string; name: string; brand: string; release_year: number; values_cents: Record<string, number> }[];
  compatibility: Record<string, string[]>;
  accounts: {
    account_id: string;
    user_id: string;
    persona: string;
    carrier?: string;
    plan_id?: string;
    current_monthly_cents: number;
    owned_accessories?: string[];
    eligibility?: string[];
    priorities?: string[];
    notes?: string;
    lines: {
      line_id: string;
      label: string;
      device_name: string;
      device_model_id?: string;
      device_sku?: string;
      device_age_months?: number;
      installments_left?: number;
      monthly_installment_cents?: number;
      usage_gb_month?: number;
      hotspot_gb_month?: number;
    }[];
  }[];
};

export const wireless = wirelessJson as unknown as WirelessData;
export const ANY_PHONE = "*";

/** The wireless catalog: every backend product with category "wireless" (phones, accessories, wearables, ...). */
export const tmobileProducts = (productsJson as Product[]).filter((p) => p.category === "wireless");
export const tmobileStore = storeJson as Store;

/** Device skus an accessory fits ('*' = any phone); null = no fit rule (not offered as compatible). */
export function fitsOf(sku: string): string[] | null {
  return wireless.compatibility[sku] ?? null;
}

/** Does the accessory fit this device? Phones match '*'; older owned devices (e.g. PX7) match by id. */
export function fits(accessorySku: string, deviceSku: string): boolean {
  const f = fitsOf(accessorySku);
  if (!f) return false;
  const isPhone = tmobileProducts.find((p) => p.sku === deviceSku)?.subcategory === "phones" || !tmobileProducts.some((p) => p.sku === deviceSku);
  return f.includes(deviceSku) || (isPhone && f.includes(ANY_PHONE));
}

const mayaAccount = wireless.accounts.find((a) => a.user_id === "maya");
const mayaLine = mayaAccount?.lines[0];

/** What Maya owns and her line (data/wireless.yaml acct_maya), used by the "skip / wait" advice. */
export const mayaDevices = {
  phone: {
    sku: mayaLine?.device_sku ?? "",
    name: mayaLine?.device_name ?? "phone",
    age_months: mayaLine?.device_age_months ?? 0,
    installments_left: mayaLine?.installments_left ?? 0,
    monthly_installment_cents: mayaLine?.monthly_installment_cents ?? 0,
  },
  owns: (mayaAccount?.owned_accessories ?? []).map((sku) => ({ sku, name: tmobileProducts.find((p) => p.sku === sku)?.name ?? sku })),
  lines: mayaAccount?.lines.length ?? 1,
};

const fmtDay = (iso: string) => new Date(`${iso}T12:00:00`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });

/** Upcoming sales (wireless.yaml promotions of kind "sale" that start after its `today`); drives WAIT verdicts. */
export const tmobileUpcoming: Record<string, { starts: string; price_cents: number; reason: string }> = Object.fromEntries(
  wireless.promotions
    .filter((p) => p.kind === "sale" && p.starts && p.starts > wireless.today)
    .flatMap((p) =>
      (p.skus ?? []).flatMap((sku) => {
        const prod = tmobileProducts.find((q) => q.sku === sku);
        if (!prod) return [];
        return [[sku, { starts: p.starts!, price_cents: Math.max(0, prod.price_cents - (p.credit_cents ?? 0)), reason: `${p.name}, starts ${fmtDay(p.starts!)}` }]];
      }),
    ),
);
