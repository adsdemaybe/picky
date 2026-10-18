// Deterministic stand-in for the agent's intent expansion + recommender, for the demo presets.
// The real path is POST /api/trips on the gateway; this produces the same shapes so the UI is identical.
import { mayaDevices } from "@/data/stores/tmobile";
import { STORES, maya, online, productBySku, slotsOf, type StoreId } from "./data";
import { upcomingFor } from "./prices";

export type Sourcing = "in_store" | "online";
export type ListLine = {
  line_id: string;
  sku: string;
  qty: number;
  unit_cents: number;
  sourcing: Sourcing;
  note?: string;
  vegetarian?: boolean;
};
export type Rec = {
  rec_id: string;
  sku: string;
  unit_cents: number;
  original_cents?: number;
  interest: number;
  reason: string;
  signals: Record<string, string | number>;
  sponsored: boolean;
  detour_m: number;
  detour_s: number;
};
/** Advice that isn't a stop: "skip it, you own one" / "wait, it's on sale Friday". */
export type Advice = { kind: "skip" | "wait"; sku: string; text: string; save_cents?: number };
export type Draft = {
  store_id: StoreId;
  intent: string;
  advice: Advice[];
  title: string;
  budget_cents: number;
  lines: ListLine[];
  recs: Rec[];
};

export const PRESETS: Record<StoreId, { id: string; label: string; text: string }[]> = {
  store_ave: [
    { id: "tacos", label: "Tacos for 6", text: "tacos for 6, under $40, one vegetarian" },
    { id: "basics", label: "Weekly basics", text: "my usual weekly basics, under $35" },
  ],
  store_tmobile: [
    { id: "pixel", label: "New Pixel 9 setup", text: "case and screen protector for my new Pixel 9, under $90" },
    { id: "run", label: "Running earbuds", text: "earbuds for running, under $100" },
  ],
};

let cur: StoreId = "store_ave";

function priceOf(sku: string): number {
  const deal = slotsOf(cur)[sku]?.deal;
  return deal?.price_cents ?? productBySku[sku].price_cents;
}

function line(sku: string, qty: number, extra: Partial<ListLine> = {}): ListLine {
  const inStock = (slotsOf(cur)[sku]?.qty ?? 0) > 0;
  const onlineItem = online.items.find((i) => i.sku === sku);
  const sourcing: Sourcing = inStock ? "in_store" : "online";
  return {
    line_id: `ln_${sku.toLowerCase()}`,
    sku,
    qty,
    unit_cents: sourcing === "online" && onlineItem ? onlineItem.price_cents : priceOf(sku),
    sourcing,
    note: sourcing === "online" ? `Out of stock at ${STORES[cur].short} · arrives in ${onlineItem?.eta_hours ?? 24} h` : undefined,
    ...extra,
  };
}

export function expandIntent(text: string, storeId: StoreId = "store_ave", add?: string | null): Draft {
  cur = storeId;
  const d = storeId === "store_tmobile" ? expandWireless(text) : expandGrocery(text);
  if (add && productBySku[add] && slotsOf(storeId)[add] && !d.lines.some((l) => l.sku === add)) {
    d.lines.unshift(line(add, 1, { note: "Added from Find" }));
    d.recs = d.recs.filter((r) => r.sku !== add);
  }
  return d;
}

function expandWireless(text: string): Draft {
  const t = text.toLowerCase();
  const budget = Number(t.match(/\$(\d+)/)?.[1] ?? 90) * 100;
  const ph = mayaDevices.phone;
  if (t.includes("earbud") || t.includes("run")) {
    const up = upcomingFor("BUDS-RUN");
    return {
      store_id: "store_tmobile",
      intent: text,
      title: "Running earbuds",
      budget_cents: budget,
      lines: [line("BUDS-RUN", 1, { note: "Sweat-proof, secure fit · 4.5★ from runners" })],
      recs: [],
      advice: up
        ? [{ kind: "wait", sku: "BUDS-RUN", text: `${up.reason}: $${(up.price_cents / 100).toFixed(2)} instead of $79.99. Picky can text you Friday morning.`, save_cents: 7999 - up.price_cents }]
        : [],
    };
  }
  return {
    store_id: "store_tmobile",
    intent: text,
    title: "Pixel 9 setup",
    budget_cents: budget,
    lines: [
      line("CASE-PX9-CLR", 1, { note: "Fits Pixel 9 · clear, slim" }),
      line("GLASS-PX9", 1, { note: "20% off with a Pixel 9 case" }),
    ],
    recs: [
      {
        rec_id: "rec_cable",
        sku: "CBL-USBC-2M",
        unit_cents: priceOf("CBL-USBC-2M"),
        interest: 0.55,
        reason: "Pixel 9 ships with a 1 m cable. Yours is from 2023 and you charge at the desk; a 2 m braided one reaches.",
        signals: { owns_cable_age_months: 34, same_wall: "yes" },
        sponsored: false,
        detour_m: 0,
        detour_s: 5,
      },
      {
        rec_id: "rec_powerbank",
        sku: "PB-10K",
        unit_cents: priceOf("PB-10K"),
        interest: 0.32,
        reason: "You've bought travel-size items twice this fall. A 10,000 mAh bank is about 2 full Pixel 9 charges.",
        signals: { travel_signal: 2, compatible: "usb-c" },
        sponsored: false,
        detour_m: 0,
        detour_s: 10,
      },
    ],
    advice: [
      { kind: "skip", sku: "CHG-20W", text: "Skip the charger: you bought a 20W USB-C charger in March and it works with the Pixel 9.", save_cents: 2499 },
      {
        kind: "wait",
        sku: "PX9-128",
        text: `About the phone itself: your ${ph.name} has ${ph.installments_left} installments left (${"$"}${((ph.installments_left * ph.monthly_installment_cents) / 100).toFixed(2)}). A $200-off holiday promo is expected Nov 27. Picky will remind you.`,
        save_cents: 20000,
      },
    ],
  };
}

function expandGrocery(text: string): Draft {
  const t = text.toLowerCase();
  if (t.includes("taco")) {
    const party = Number(t.match(/for (\d+)/)?.[1] ?? 6);
    const budget = Number(t.match(/\$(\d+)/)?.[1] ?? 40) * 100;
    const k = party / 6;
    return {
      store_id: "store_ave",
      advice: [
        { kind: "skip", sku: "HOTSAUCE-RED", text: "Skip hot sauce: you bought Classic Red on Oct 11 and usually go through one every ~5 weeks." },
      ],
      intent: text,
      title: `Taco night for ${party}`,
      budget_cents: budget,
      lines: [
        line("TORT-CORN", Math.max(1, Math.round(k)), { note: "30 ct covers ~3 tacos each" }),
        line("BEEF-GROUND", Math.max(1, Math.round(2 * k))),
        line("BEANS-BLACK", Math.max(1, Math.round(2 * k)), { vegetarian: true, note: "Protein for the vegetarian guest" }),
        line("CHEESE-MEX", 1),
        line("SALSA-MILD", 1, { note: "Your usual · rated 3★ last time" }),
        line("AVOCADO", Math.max(2, Math.round(3 * k)), { note: "Robot picks the ripest 3 of 22" }),
        line("LIMES", 4),
        line("ONION-WHITE", 1),
        line("CILANTRO", 1),
        line("SOUR-CREAM", 1),
      ],
      recs: [
        {
          rec_id: "rec_salsa_chipotle",
          sku: "SALSA-CHIPOTLE",
          unit_cents: priceOf("SALSA-CHIPOTLE"),
          interest: 0.82,
          reason: "You rated Classic Red Hot Sauce 5★ but the mild salsa only 3★. This one is smoky and hotter.",
          signals: { spicy_pref: maya.profile.prefs.spicy ?? 0, past_rating_mild: 3, similarity: 0.78 },
          sponsored: false,
          detour_m: 0,
          detour_s: 6,
        },
        {
          rec_id: "rec_guac_kit",
          sku: "GUAC-KIT",
          unit_cents: priceOf("GUAC-KIT"),
          original_cents: productBySku["GUAC-KIT"].price_cents,
          interest: 0.64,
          reason: "33% off because it's best by tomorrow. You're making tacos tonight, so it won't go to waste.",
          signals: { discount_pct: 33, near_expiry_days: 1, fresh_pref: maya.profile.prefs.fresh ?? 0 },
          sponsored: false,
          detour_m: 7.4,
          detour_s: 14,
        },
        {
          rec_id: "rec_chips",
          sku: "CHIPS-TORTILLA",
          unit_cents: priceOf("CHIPS-TORTILLA"),
          original_cents: productBySku["CHIPS-TORTILLA"].price_cents,
          interest: 0.51,
          reason: "25% off on the taco-night endcap, about 4 m off your route. Goes with the salsa.",
          signals: { discount_pct: 25, on_route: "endcap", deals_pref: maya.profile.prefs.deals ?? 0 },
          sponsored: false,
          detour_m: 4.1,
          detour_s: 8,
        },
      ],
    };
  }
  const budget = Number(t.match(/\$(\d+)/)?.[1] ?? 35) * 100;
  const coffee = upcomingFor("COFFEE");
  return {
    store_id: "store_ave",
    advice: coffee ? [{ kind: "wait", sku: "COFFEE", text: `Coffee: ${coffee.reason} ($${(coffee.price_cents / 100).toFixed(2)}). You have ~6 days left in your bag, so it's still on the list, but buy 1 now, not 2.` }] : [],
    intent: text,
    title: "Weekly basics",
    budget_cents: budget,
    lines: [
      line("OAT-MILK", 1, { note: "Every ~10 days · last bought Oct 8" }),
      line("EGGS", 1),
      line("BANANAS", 1),
      line("LIMES", 4, { note: "You buy limes every ~9 days" }),
      line("AVOCADO", 3),
      line("BEANS-BLACK", 2),
      line("BREAD-WHEAT", 1),
      line("COFFEE", 1),
    ],
    recs: [
      {
        rec_id: "rec_yogurt",
        sku: "YOGURT-GREEK",
        unit_cents: priceOf("YOGURT-GREEK"),
        original_cents: productBySku["YOGURT-GREEK"].price_cents,
        interest: 0.58,
        reason: "42% off, best by Oct 19. You buy Greek yogurt about every 12 days.",
        signals: { discount_pct: 42, cadence_days: 12 },
        sponsored: false,
        detour_m: 0,
        detour_s: 5,
      },
      {
        rec_id: "rec_sourdough",
        sku: "BREAD-SOURDOUGH",
        unit_cents: priceOf("BREAD-SOURDOUGH"),
        original_cents: productBySku["BREAD-SOURDOUGH"].price_cents,
        interest: 0.4,
        reason: "Half price today ($2.49). It's in the same aisle as your wheat bread.",
        signals: { discount_pct: 50, same_aisle: "yes" },
        sponsored: false,
        detour_m: 1.2,
        detour_s: 4,
      },
    ],
  };
}

export function draftTotal(d: Draft, acceptedRecs: Set<string>) {
  const list = d.lines.reduce((s, l) => s + l.qty * l.unit_cents, 0);
  const recs = d.recs.filter((r) => acceptedRecs.has(r.rec_id)).reduce((s, r) => s + r.unit_cents, 0);
  const onlineSub = d.lines.filter((l) => l.sourcing === "online").reduce((s, l) => s + l.qty * l.unit_cents, 0);
  // Online add-ons ride along with a robot order (one combined checkout), so delivery is waived for them.
  const shipping = onlineSub > 0 && onlineSub < online.free_shipping_over_cents && d.lines.every((l) => l.sourcing === "online") ? online.shipping_cents : 0;
  return { list, recs, shipping, total: list + recs + shipping, onlineSub };
}

/** Deterministic mandate pre-check (mirrors the trust arm's rules for display; the gateway is authoritative). */
/** Tech purchases run under a separate demo mandate (bigger cap, asks above $50). */
export const WIRELESS_MANDATE = { ...maya.mandate, mandate_id: "im_maya_tech_001", cap_cents: 15000, per_item_cap_cents: 10000, ask_above_cents: 5000 };

export function mandateCheck(total_cents: number, lines: { sku: string; unit_cents: number }[], storeId: StoreId = "store_ave") {
  const m = STORES[storeId].kind === "wireless" ? WIRELESS_MANDATE : maya.mandate;
  const reasons: { code: string; text: string; level: "ok" | "ask" | "block" }[] = [];
  if (total_cents > m.cap_cents)
    reasons.push({ code: "OVER_CAP", text: `Total exceeds your $${m.cap_cents / 100} trip cap`, level: "block" });
  else reasons.push({ code: "WITHIN_CAP", text: `Within your $${m.cap_cents / 100} trip cap`, level: "ok" });
  for (const l of lines) {
    const p = productBySku[l.sku];
    if (m.blocked_categories.includes(p.category))
      reasons.push({ code: "BLOCKED_CATEGORY", text: `${p.name}: ${p.category} is blocked`, level: "block" });
    if (l.unit_cents > m.ask_above_cents)
      reasons.push({ code: "ASK_ABOVE", text: `${p.name} is over your $${m.ask_above_cents / 100} ask-me line`, level: "ask" });
  }
  if (m.require_checkout_approval)
    reasons.push({ code: "CHECKOUT_APPROVAL", text: "Checkout always waits for your explicit approval", level: "ask" });
  const verdict = reasons.some((r) => r.level === "block") ? "deny" : reasons.some((r) => r.level === "ask") ? "needs_approval" : "approve";
  return { verdict, reasons } as const;
}

/** Default rec selection: keep recs in interest order while the estimate stays within budget. */
export function defaultAccepted(d: Draft): Set<string> {
  const keep = new Set<string>();
  let total = draftTotal(d, keep).total;
  for (const r of [...d.recs].sort((a, b) => b.interest - a.interest)) {
    if (total + r.unit_cents <= d.budget_cents) {
      keep.add(r.rec_id);
      total += r.unit_cents;
    }
  }
  return keep;
}
