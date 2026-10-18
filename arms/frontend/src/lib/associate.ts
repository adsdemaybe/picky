// Magenta: ONE agent for the T-Mobile store, on the website and on the robot on the floor. Five skills behind one
// conversation: Cart Whisperer (basket), The Matchmaker (right phone + plan), Next Best Moment (what to raise before a
// customer walks: her opener at check-in), The Timekeeper (upgrade now / wait / keep), Household Mix (whole-family
// plan). On the floor she also walks shoppers to fixtures and fetches boxed stock from the back.
// Every fact it says comes from these deterministic tools over data/ (generated JSON). /api/associate lets Grok pick
// the tools and phrase the answer when XAI_API_KEY is set; `respond()` below is the offline brain over the SAME tools,
// so the demo works with no key, no network and no backend. Pure module: runs on the server and in the browser.
import { linePromoFor, mayaDevices, tmobileProducts, tmobileStore, tmobileUpcoming, wireless, type WirelessProduct } from "@/data/stores/tmobile";
import type { Pose } from "./data";
import { customerOf, householdResult, matchFor, momentFor, presetFor, upgradesFor, type Customer } from "./customers";
import { accountOf, match, messageFor, planOf, tradeModel, type Advice, type Answers, type Priority, type Usage } from "./directions";
import { cartTotal, shortName, suggest, targetDevice, unitPrice, type WLine } from "./whisperer";

export type AssocAction =
  | { type: "show"; skus: string[] }
  | { type: "guide"; fixture_id: string; sku?: string }
  | { type: "add"; sku: string; qty: number }
  | { type: "remove"; sku: string }
  | { type: "fetch"; sku: string }
  | { type: "expert"; reason: string }
  | { type: "checkout" };

export type AssocCtx = {
  cart: WLine[];
  budget_cents: number;
  /** the product we were just talking about ("add it", "where is it") */
  focus?: string | null;
  /** the phone this conversation is about ("a case for my Pixel 9"), so fit checks use it */
  device?: string | null;
  /** Maya's device-level data is available (owned items, installments): drives Cart Whisperer's SKIP / WAIT */
  known: boolean;
  /** who checked in with the app (lib/customers.ts); null = guest. Unlocks account, upgrade and plan answers */
  customer?: string | null;
  declined?: string[];
};
/** Rich results shown under a reply (rendered from the same engines on the client). */
export type AgentCard =
  | { kind: "bundles"; answers: Answers }
  | { kind: "upgrade"; case_id: string }
  | { kind: "household"; customer_id: string }
  | { kind: "basket" };
export type ToolResult = { summary: string; actions?: AssocAction[]; focus?: string | null; cards?: AgentCard[] };
export type AssocReply = { text: string; actions: AssocAction[]; cards: AgentCard[]; chips: string[]; focus: string | null; device: string | null; source: "grok" | "offline" };

const P: Record<string, WirelessProduct> = Object.fromEntries(tmobileProducts.map((p) => [p.sku, p]));
export const productOf = (sku: string) => P[sku] ?? null;
const money = (c: number) => (c === 0 ? "free" : `$${(c / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const day = (iso: string) => new Date(`${iso}T12:00:00-07:00`).toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
const nice = (name: string) => name.replace(/\s+\d+\s*GB$/i, "");

// ------------------------------------------------------------------------------------------------ store geometry

export const FIXTURES = Object.fromEntries(tmobileStore.fixtures.map((f) => [f.fixture_id, f]));
/** "Accessory wall: cases & glass ..." -> "the accessory wall" */
export function fixtureName(id: string) {
  const short: Record<string, string> = {
    "ACC-WALL": "the accessory wall",
    "TABLE-A": "the Apple table",
    "TABLE-B": "the Google and Motorola table",
    "TABLE-C": "the Samsung table",
    "AUDIO-WALL": "the audio wall",
    "HOME-INET": "the Home Internet pedestal",
    "WEAR-CASE": "the wearables case",
    COUNTER: "the counter",
    "BACK-STOCK": "the back room",
  };
  return short[id] ?? FIXTURES[id]?.label.split(":")[0] ?? "the store";
}
const slotsBySku = new Map<string, typeof tmobileStore.slots>();
for (const s of tmobileStore.slots) slotsBySku.set(s.sku, [...(slotsBySku.get(s.sku) ?? []), s]);
/** Where a shopper sees it: the demo unit / shelf facing (not the staff-only back room). */
export function displaySlot(sku: string) {
  const all = slotsBySku.get(sku) ?? [];
  return all.find((s) => s.fixture_id !== "BACK-STOCK") ?? all[0] ?? null;
}
/** Boxed units the robot can bring from the back room. */
export function backStock(sku: string) {
  return (slotsBySku.get(sku) ?? []).filter((s) => s.fixture_id === "BACK-STOCK").reduce((n, s) => n + (s.qty ?? 0), 0);
}
/** A shelf deal on any of the product's slots. */
export function dealOf(sku: string) {
  return (slotsBySku.get(sku) ?? []).find((s) => s.deal)?.deal ?? null;
}
/** The description without its leading "Brand Name, 128 GB." restatement (that's already on screen / in the sentence). */
export function blurb(sku: string) {
  const d = P[sku]?.description ?? "";
  return d.replace(/^[^.]*\b\d+\s*GB\.\s*/i, "").trim();
}
export function stockOf(sku: string) {
  const back = backStock(sku);
  if (back) return back;
  return (slotsBySku.get(sku) ?? []).reduce((n, s) => n + (s.qty ?? 0), 0);
}
export function approachOf(fixtureId: string): Pose {
  const s = tmobileStore.slots.find((x) => x.fixture_id === fixtureId);
  if (s) return s.approach_pose;
  const r = FIXTURES[fixtureId]?.rect;
  return r ? { x: r.x + r.w / 2, y: r.y - 0.4 } : tmobileStore.entrance;
}

// ------------------------------------------------------------------------------------------------ understanding

const STOP = new Set("a an the i im i'm me my you your for to of and or with is are do does can could would should will it this that there here please want need looking look find get show some any have has on in at what where which how much about just like really one new buy".split(" "));
const CATEGORY_WORDS: [RegExp, (p: WirelessProduct) => boolean][] = [
  [/\b(ear ?buds?|airpods?|buds|headphones?|earphones?|beats|audio|speaker|music|running)\b/, (p) => p.category === "audio"],
  [/\b(cases?|covers?)\b/, (p) => p.category === "case" && p.subcategory !== "grip"],
  [/\b(screen protector|protector|glass|screen)\b/, (p) => p.category === "screen"],
  [/\b(charg(er|ers|ing)|cables?|power ?banks?|batter(y|ies)|magsafe|wireless charg\w*|car)\b/, (p) => p.category === "charger"],
  [/\b(watch(es)?|wearables?|smartwatch)\b/, (p) => p.category === "wearable"],
  [/\b(tablets?|ipads?)\b/, (p) => p.category === "tablet"],
  [/\b(home internet|internet|wi-?fi|hotspot|router|gateway)\b/, (p) => p.category === "home_internet"],
  [/\b(trackers?|airtags?|sim( card| kit)?|esim)\b/, (p) => p.category === "connected"],
  [/\b(grips?|popsockets?)\b/, (p) => p.subcategory === "grip"],
];

type Device = { sku: string; name: string; owned: boolean };
const PHONE_KEYS: { key: RegExp; sku: string }[] = (() => {
  const out: { key: string; sku: string }[] = [];
  for (const p of tmobileProducts.filter((x) => x.category === "phone")) {
    const base = nice(p.name).toLowerCase();
    const vars = new Set([base, base.replace(/^galaxy (z )?/, ""), base.replace(/(\D)(\d)/g, "$1 $2"), base.replace(/\+/g, " plus"), base.replace(/^galaxy z /, "galaxy ")]);
    for (const v of vars) out.push({ key: v, sku: p.sku });
  }
  out.push({ key: "pixel 7", sku: mayaDevices.phone.sku });
  return out
    .sort((a, b) => b.key.length - a.key.length)
    .map((o) => ({ key: new RegExp(`(^|[^a-z0-9])${o.key.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s*")}(?![a-z0-9])`), sku: o.sku }));
})();

/** Phones named in the text, in order of appearance (longest name wins: "pixel 11 pro" before "pixel 11"). */
export function phonesIn(text: string): string[] {
  let t = ` ${text.toLowerCase()} `;
  const found: { at: number; sku: string }[] = [];
  for (const { key, sku } of PHONE_KEYS) {
    const m = key.exec(t);
    if (!m) continue;
    found.push({ at: m.index, sku });
    t = t.slice(0, m.index) + " ".repeat(m[0].length) + t.slice(m.index + m[0].length);
  }
  const seen = new Set<string>();
  return found.sort((a, b) => a.at - b.at).map((f) => f.sku).filter((s) => !seen.has(s) && (seen.add(s), true));
}

/** The device we're shopping for: one named, else "my phone" (if checked in), else the phone in the basket. */
function deviceFor(text: string, ctx: AssocCtx): Device | null {
  const named = phonesIn(text)[0];
  if (named) return { sku: named, name: shortName(named), owned: named === mayaDevices.phone.sku };
  if (/\bmy (phone|pixel)\b/i.test(text) && ctx.known && !ctx.device) return { sku: mayaDevices.phone.sku, name: mayaDevices.phone.name, owned: true };
  return deviceOf(ctx);
}

/** The phone accessories are for, without new words: the one we're talking about, a phone in the basket, else
 * (checked in) the one they own. Null for a guest with no phone in sight. */
export function deviceOf(ctx: AssocCtx): Device | null {
  if (ctx.device && (P[ctx.device] || ctx.device === mayaDevices.phone.sku)) return { sku: ctx.device, name: shortName(ctx.device), owned: ctx.device === mayaDevices.phone.sku };
  if (ctx.cart.some((l) => P[l.sku]?.category === "phone") || ctx.known) {
    const d = targetDevice({ cart: ctx.cart });
    if (!ctx.known && d.sku === mayaDevices.phone.sku) return null;
    return { sku: d.sku, name: d.name, owned: !d.isNew };
  }
  return null;
}

/** Ranked catalog matches for free text. */
export function searchSkus(text: string, limit = 4): string[] {
  const t = text.toLowerCase();
  const cats = CATEGORY_WORDS.filter(([re]) => re.test(t)).map(([, f]) => f);
  const words = t.replace(/[^a-z0-9+ ]/g, " ").split(/\s+/).filter((w) => w.length > 1 && !STOP.has(w));
  const phones = phonesIn(t);
  const scored = tmobileProducts.map((p) => {
    const name = p.name.toLowerCase();
    const meta = `${p.brand} ${p.tags.join(" ")} ${p.subcategory ?? ""}`.toLowerCase();
    const desc = (p.description ?? "").toLowerCase();
    let s = 0;
    for (const w of words) s += name.includes(w) ? 3 : meta.includes(w) ? 2 : desc.includes(w) ? 0.5 : 0;
    if (cats.length) s = cats.some((f) => f(p)) ? s + 5 : s - 4;
    if (phones.length) {
      if (phones.includes(p.sku) && !cats.length) s += 12;
      else if (phones.some((d) => p.compatible_with?.includes(d))) s += 6;
      else if (p.compatible_with?.length) s -= 6;
    }
    if (/\b(running|workout|gym)\b/.test(t) && p.tags.some((x) => /sport|running|fitness/.test(x))) s += 4;
    if (/\b(cheap|budget|affordable|inexpensive)\b/.test(t)) s += 2 - unitPrice(p.sku) / 50000;
    if (stockOf(p.sku) <= 0) s -= 1;
    return { sku: p.sku, s };
  });
  return scored
    .filter((x) => x.s > 1.5)
    .sort((a, b) => b.s - a.s || unitPrice(a.sku) - unitPrice(b.sku))
    .slice(0, limit)
    .map((x) => x.sku);
}

// ------------------------------------------------------------------------------------------------ tools

function where(sku: string) {
  const s = displaySlot(sku);
  if (!s) return null;
  const f = s.fixture_id;
  return { fixture_id: f, say: f === "BACK-STOCK" ? "in the back room (staff only), so I'll bring it out" : `on ${fixtureName(f)}` };
}

function line(sku: string) {
  const p = P[sku];
  const deal = dealOf(sku);
  const w = where(sku);
  const bits = [`${nice(p.name)} (${sku}): ${money(unitPrice(sku))}`];
  if (deal) bits.push(`on sale, was ${money(deal.original_cents)} (${deal.reason})`);
  bits.push(stockOf(sku) > 0 ? `${stockOf(sku)} in stock` : "out of stock");
  if (w) bits.push(w.say);
  if (p.compatible_with?.length) bits.push(`fits ${p.compatible_with.map(shortName).join(", ")}`);
  return bits.join(", ");
}

export const tools = {
  search_products(query: string): ToolResult {
    const skus = searchSkus(query);
    if (!skus.length) return { summary: `No products in this store match "${query}".` };
    return { summary: skus.map(line).join("\n"), actions: [{ type: "show", skus }], focus: skus[0] };
  },

  product_details(sku: string, ctx: AssocCtx): ToolResult {
    const p = P[sku];
    if (!p) return { summary: `Unknown SKU ${sku}.` };
    const out = [line(sku), blurb(sku)];
    const promo = linePromoFor(sku);
    if (promo) out.push(`Line offer (bill credits over 24 months, not a shelf price): ${promo.headline}${promo.requires ? `; needs a ${promo.requires.replace(/_/g, " ")}` : ""}.`);
    const up = tmobileUpcoming[sku];
    if (up) out.push(`Promo calendar (expected, synthetic): ${money(up.price_cents)} from ${day(up.starts)} (${up.reason}).`);
    if (p.category === "phone" && backStock(sku)) out.push(`Boxed units in the back: ${backStock(sku)}. The robot can fetch one.`);
    if (ctx.known && p.category === "phone") {
      const ph = mayaDevices.phone;
      out.push(`Shopper's current phone: ${ph.name}, ${ph.age_months} months old, ${ph.installments_left} payments of ${money(ph.monthly_installment_cents)} left (${money(ph.payoff_cents)}).`);
    }
    return { summary: out.filter(Boolean).join("\n"), actions: [{ type: "show", skus: [sku] }], focus: sku };
  },

  where_is(sku: string): ToolResult {
    const w = where(sku);
    if (!w) return { summary: `${sku} isn't on the floor in this store.` };
    if (w.fixture_id === "BACK-STOCK") return { summary: `${nice(P[sku].name)} is ${w.say}.`, actions: [{ type: "fetch", sku }], focus: sku };
    return { summary: `${nice(P[sku].name)} is ${w.say}. Walking the shopper there.`, actions: [{ type: "guide", fixture_id: w.fixture_id, sku }], focus: sku };
  },

  accessories_for(device_sku: string, category?: string): ToolResult {
    const all = tmobileProducts.filter((p) => p.compatible_with?.includes(device_sku) && (!category || p.category === category) && stockOf(p.sku) > 0);
    if (!all.length) return { summary: `No ${category ?? "accessories"} made specifically for the ${shortName(device_sku)} in stock. Universal ones (chargers, earbuds) work with any phone.` };
    const skus = all.sort((a, b) => unitPrice(a.sku) - unitPrice(b.sku)).slice(0, 4).map((p) => p.sku);
    return { summary: skus.map(line).join("\n"), actions: [{ type: "show", skus }], focus: skus[0] };
  },

  check_fit(accessory_sku: string, device_sku: string): ToolResult {
    const p = P[accessory_sku];
    if (!p) return { summary: `Unknown SKU ${accessory_sku}.` };
    const c = p.compatible_with ?? [];
    if (!c.length) return { summary: `${nice(p.name)} is universal: it works with the ${shortName(device_sku)}.`, focus: accessory_sku };
    if (c.includes(device_sku)) return { summary: `Yes: ${nice(p.name)} fits the ${shortName(device_sku)}.`, focus: accessory_sku };
    const right = tmobileProducts.find((q) => q.category === p.category && q.compatible_with?.includes(device_sku) && stockOf(q.sku) > 0);
    return {
      summary: `No: ${nice(p.name)} is made for the ${c.map(shortName).join(", ")}, not the ${shortName(device_sku)}.${right ? ` One that fits: ${line(right.sku)}.` : ""}`,
      actions: right ? [{ type: "show", skus: [right.sku] }] : [],
      focus: right?.sku ?? accessory_sku,
    };
  },

  my_account(ctx: AssocCtx): ToolResult {
    if (!ctx.known) return { summary: "The shopper hasn't checked in with the app, so the robot doesn't know their account. Ask them to tap 'Check in' to use it (optional)." };
    const ph = mayaDevices.phone;
    const up = Object.entries(tmobileUpcoming).filter(([sku]) => P[sku]?.category === "phone");
    return {
      summary: [
        `Customer: Maya, ${mayaDevices.lines} lines on the account (synthetic demo account).`,
        `Phone: ${ph.name}, bought ${day(ph.purchased)}, ${ph.age_months} months old, ${ph.installments_left} payments of ${money(ph.monthly_installment_cents)} left (${money(ph.payoff_cents)} to pay off).`,
        `Owns: ${mayaDevices.owns.map((o) => `${o.name} (bought ${day(o.bought)}${o.note ? `; ${o.note}` : ""})`).join("; ")}.`,
        ...up.map(([sku, u]) => `Expected promo: ${nice(P[sku].name)} ${money(u.price_cents)} from ${day(u.starts)} (${u.reason}).`),
      ].join("\n"),
    };
  },

  basket_advice(ctx: AssocCtx): ToolResult {
    if (!ctx.cart.length) return { summary: "The basket is empty." };
    const items = ctx.cart.map((l) => `${l.qty} × ${nice(P[l.sku]?.name ?? l.sku)} ${money(l.qty * unitPrice(l.sku))}`).join("; ");
    const cards = suggest({ cart: ctx.cart, budget_cents: ctx.budget_cents, phase: "in_trip", declined: new Set(ctx.declined ?? []) }).filter((c) => ctx.known || !c.reason_codes.some((r) => r === "ALREADY_OWNED" || r === "INSTALLMENTS_LEFT"));
    return {
      summary: `Basket: ${items}. Total ${money(cartTotal(ctx.cart))} (budget ${money(ctx.budget_cents)}).\n${cards.length ? `Cart Whisperer: ${cards.map((c) => `${c.title}: ${c.reason}`).join(" | ")}` : "Cart Whisperer: nothing to change."}`,
    };
  },

  deals(): ToolResult {
    const shelf = tmobileStore.slots.filter((s) => s.deal && stockOf(s.sku) > 0);
    const seen = new Set<string>();
    const skus = shelf.map((s) => s.sku).filter((s) => !seen.has(s) && (seen.add(s), true));
    return {
      summary: [
        ...skus.map((sku) => `Shelf deal: ${nice(P[sku].name)} ${money(unitPrice(sku))}, was ${money(dealOf(sku)!.original_cents)} (${dealOf(sku)!.reason}).`),
        ...wireless.line_promos.filter((p) => p.sku && P[p.sku]).slice(0, 4).map((p) => `Line offer: ${p.headline} (bill credits${p.requires ? `, needs a ${p.requires.replace(/_/g, " ")}` : ""}).`),
        ...wireless.upcoming_promos.map((u) => `Coming soon (expected): ${nice(P[u.sku]?.name ?? u.sku)} ${money(u.price_cents)} from ${day(u.starts)}.`),
      ].join("\n"),
      actions: skus.length ? [{ type: "show", skus: skus.slice(0, 4) }] : [],
    };
  },

  add_to_basket(sku: string, qty: number, ctx: AssocCtx, force = false): ToolResult {
    const p = P[sku];
    if (!p) return { summary: `Unknown SKU ${sku}.` };
    if (stockOf(sku) <= 0) return { summary: `${nice(p.name)} is out of stock here.`, focus: sku };
    const dev = deviceOf(ctx);
    if (!force && dev && p.compatible_with?.length && !p.compatible_with.includes(dev.sku) && p.category !== "phone")
      return { summary: `NOT ADDED: ${nice(p.name)} doesn't fit the ${dev.name} (it's for the ${p.compatible_with.map(shortName).join(", ")}). Ask before adding anyway.`, focus: sku };
    const acts: AssocAction[] = [{ type: "add", sku, qty }];
    if (backStock(sku)) acts.push({ type: "fetch", sku });
    return { summary: `Added ${qty} × ${nice(p.name)} (${money(unitPrice(sku) * qty)}).${backStock(sku) ? " The robot is bringing a boxed one from the back." : ""}`, actions: acts, focus: sku };
  },

  remove_from_basket(sku: string, ctx: AssocCtx): ToolResult {
    if (!ctx.cart.some((l) => l.sku === sku)) return { summary: `${nice(P[sku]?.name ?? sku)} isn't in the basket.` };
    return { summary: `Removed ${nice(P[sku].name)}.`, actions: [{ type: "remove", sku }] };
  },

  upgrade_advice(ctx: AssocCtx, person?: string): ToolResult {
    const c = customerOf(ctx.customer);
    if (!c) return { summary: "Not checked in: can't see their phone, payments or trade-in. Ask what matters to them (phone_match) or suggest checking in with the app." };
    const all = upgradesFor(c).filter((a) => !person || a.c.name.toLowerCase().includes(person.toLowerCase()));
    if (!all.length) return { summary: `No phone on file for ${c.name}${c.prospect ? " (not a customer yet)" : ""}. Use phone_match instead.` };
    return {
      summary: all.map((a) => [`Timekeeper for ${a.c.name}'s ${tradeModel(a.c.device_model_id)?.name}: ${a.headline}.`, ...a.reasons, `Upgrade today to the ${nice(P[a.c.target_sku]?.name ?? "")}: ${money(a.now.cost_cents)} out of pocket after trade-in.`, a.later ? `${a.later.label}: ${money(a.later.cost_cents)}.` : ""].filter(Boolean).join("\n")).join("\n\n"),
      cards: all.map((a) => ({ kind: "upgrade" as const, case_id: a.c.id })),
      focus: all[0].c.target_sku,
    };
  },

  plan_check(ctx: AssocCtx): ToolResult {
    const c = customerOf(ctx.customer);
    if (!c) return { summary: "Not checked in: can't see their lines or plan. Offer the whole-household check after they check in." };
    const r = householdResult(c);
    if (!r) return { summary: `No lines on file for ${c.name}. Use phone_match to build a plan from scratch.` };
    return {
      summary: `Household Mix for ${c.name}: today ${r.current_label}, ${money(r.current_total_cents)}/mo for ${r.lines.map((l) => `${l.label} (${l.usage_gb_month} GB, ${l.hotspot_gb_month} GB hotspot)`).join(", ")}${r.current_misfits.length ? `; doesn't fit: ${r.current_misfits.join("; ")}` : ""}. Best fit: ${r.best.summary}, ${money(r.best.monthly_cents)}/mo${r.best.streaming_paid_cents ? ` incl. ${money(r.best.streaming_paid_cents)} streaming paid separately` : ""}. ${r.saves_cents > 0 ? `Saves ${money(r.saves_cents)}/mo.` : r.saves_cents < 0 ? `Costs ${money(-r.saves_cents)}/mo more but it's the cheapest that fits.` : "Already the cheapest setup that fits; no change needed."} Checked ${r.combos_checked} combinations.`,
      cards: [{ kind: "household", customer_id: c.id }],
    };
  },

  phone_match(ctx: AssocCtx, o: { priorities?: string[]; budget_cents?: number; lines?: number; usage?: string; keep_phone?: boolean } = {}): ToolResult {
    const c = customerOf(ctx.customer);
    const preset = c ? presetFor(c) : null;
    const pr = (o.priorities ?? []).filter((p): p is Priority => ["price", "camera", "battery", "performance", "size"].includes(p));
    const answers: Answers = {
      lines: o.lines ?? preset?.lines ?? 1,
      budget_monthly_cents: o.budget_cents ?? preset?.budget_monthly_cents ?? 10000,
      priorities: pr.length ? pr : (preset?.priorities ?? ["price"]),
      usage: (["light", "medium", "heavy"].includes(o.usage ?? "") ? o.usage : (preset?.usage ?? "medium")) as Usage,
      keep_phone: o.keep_phone ?? preset?.keep_phone ?? false,
      keep_second_phone: preset?.keep_second_phone,
      switching: preset?.switching,
      age_55: preset?.age_55,
      trade_in: preset?.trade_in,
    };
    const r = match(answers);
    const top = r.bundles;
    return {
      summary: [...r.notes, ...top.map((b) => `${b.badge}: ${b.title}, ${money(b.monthly_cents)}/mo (plan ${money(b.plan_cents)} + phone ${money(b.device_cents)}${b.trade_credit_cents ? ` − trade-in ${money(b.trade_credit_cents)}` : ""}). Why: ${b.why.join("; ")}. Trade-offs: ${b.tradeoffs.join("; ")}.`)].join("\n"),
      actions: [],
      cards: [{ kind: "bundles", answers }],
      focus: top.find((b) => b.phone_sku)?.phone_sku ?? null,
    };
  },

  account_check(ctx: AssocCtx): ToolResult {
    const c = customerOf(ctx.customer);
    if (!c) return { summary: "Not checked in." };
    const m = momentFor(c);
    if (!m) return { summary: `${c.name} isn't a T-Mobile customer yet (${c.blurb}). Nothing to fix on an account; help them choose (phone_match, plan_check).` };
    return {
      summary: `Next Best Moment for ${c.name}: risk ${Math.round(m.risk * 100)}% (${m.level}). Signals: ${m.drivers.map((d) => `${d.id} (${d.text})`).join("; ") || "none"}. Recommended: ${m.plan.map((p) => `${p.label}: ${p.detail}`).join(" | ")}. ${m.verdict} Customer-facing message: ${messageFor(m) || "(none: leave them alone, no offer)"}`,
    };
  },

  call_expert(reason: string): ToolResult {
    return { summary: `Paged a T-Mobile expert to the shopper (${reason}). Typical wait: about 3 minutes.`, actions: [{ type: "expert", reason }] };
  },

  start_checkout(ctx: AssocCtx): ToolResult {
    if (!ctx.cart.length) return { summary: "The basket is empty; nothing to check out." };
    return { summary: `Checkout started at the counter: ${money(cartTotal(ctx.cart))}. The shopper confirms and pays on the screen.`, actions: [{ type: "checkout" }] };
  },
};

// ------------------------------------------------------------------------------------------------ offline brain

const HUMAN = /\b(human|real person|person|employee|staff|someone|expert|manager|representative|rep)\b/;
const ACCOUNT = /\b(bill|billing|port|transfer (my )?number|activat\w*|sim swap|change (my )?plan|switch plans?|cancel|payment arrangement|unlock|password|pin)\b/;
const pick = <T,>(xs: T[], seed: string) => xs[[...seed].reduce((n, c) => n + c.charCodeAt(0), 0) % xs.length];

function reply(text: string, r: ToolResult[] = [], focus?: string | null): Omit<AssocReply, "source" | "chips" | "device"> {
  return { text, actions: r.flatMap((x) => x.actions ?? []), cards: r.flatMap((x) => x.cards ?? []), focus: focus ?? [...r].reverse().find((x) => x.focus)?.focus ?? null };
}

/** Naming a phone sets the conversation's device. "Does the Pixel 7 case fit a Pixel 9" / "case for my Pixel 9": the
 * phone after for / fit / with / my wins; otherwise the last one named. "My phone" is the one they own (checked in). */
export function conversationDevice(text: string, ctx: AssocCtx): string | null {
  const t = text.toLowerCase();
  const tail = /\b(for|fits?|with|on|my)\s+(a |an |the |my )?(new )?(.+)$/.exec(t)?.[4];
  const named = (tail && phonesIn(tail)[0]) || phonesIn(t).at(-1);
  if (named) return named;
  if (/\bmy (current |old )?phone\b/.test(t) && ctx.known) return mayaDevices.phone.sku;
  return ctx.device ?? null;
}

/** Rule-based brain (no LLM). Same tools, same facts; phrased from templates. */
export function respond(input: string, ctx: AssocCtx): AssocReply {
  const t = input.toLowerCase().trim();
  const device = conversationDevice(t, ctx);
  ctx = { ...ctx, device };
  const r = respondInner(t, ctx) as ReturnType<typeof respondInner> & { chips?: string[] };
  return { ...r, device, chips: r.chips ?? chipsFor({ ...ctx, cart: applyActions(ctx.cart, r.actions), focus: r.focus ?? ctx.focus }, r.actions), source: "offline" };
}

/** The basket after a reply's add / remove actions (what the client will hold once it applies them). */
export function applyActions(cart: WLine[], actions: AssocAction[]): WLine[] {
  let c = cart;
  for (const x of actions) {
    if (x.type === "add") c = c.some((l) => l.sku === x.sku) ? c.map((l) => (l.sku === x.sku ? { ...l, qty: l.qty + x.qty } : l)) : [...c, { sku: x.sku, qty: x.qty }];
    if (x.type === "remove") c = c.filter((l) => l.sku !== x.sku);
  }
  return c;
}

function respondInner(t: string, ctx: AssocCtx): Omit<AssocReply, "source" | "chips" | "device"> {
  const c = customerOf(ctx.customer);
  const it = /\b(it|that|this|that one|this one|them)\b/.test(t) ? (ctx.focus ?? null) : null;
  const named = searchSkus(t, 3);
  const phones = phonesIn(t).filter((s) => P[s]);
  const subject = (phones.length && !CATEGORY_WORDS.some(([re]) => re.test(t)) ? phones[0] : null) ?? (named[0] && !it ? named[0] : null) ?? it ?? named[0] ?? null;
  const dev = deviceFor(t, ctx);

  if ((!t || (/^(hi|hello|hey|yo|good (morning|afternoon|evening)|howdy)\b/.test(t) && t.split(" ").length < 4)) && c) return openerInner(c, ctx);
  if (!t || /^(hi|hello|hey|yo|good (morning|afternoon|evening)|howdy)\b/.test(t) && t.split(" ").length < 4)
    return reply(
      ctx.known
        ? `Hi Maya, good to see you. I can find anything here, walk you to it, check what fits your Pixel 7, and grab boxed phones from the back. What are you looking for today?`
        : `Hi, I'm Magenta, the robot helper here. I can find anything in the store, walk you over, check what fits your phone, and bring boxed phones out from the back. What are you looking for?`,
    );

  if (/\b(thank|thanks|that's all|thats all|bye|goodbye|all set)\b/.test(t))
    return reply(ctx.cart.length ? `Happy to help. When you're ready, say "check out" and I'll ring you up at the counter.` : `Anytime. I'll be right here by the door if you need me.`);

  if (/\b(what can you do|how does this work|who are you)\b/.test(t))
    return reply(`I'm Magenta. I can find you the right phone and plan, tell you honestly whether to upgrade now or wait, check whether your whole household is on the right plan, and keep your basket honest at checkout. In the store I'll also walk you to things and bring sealed phones from the back.`);

  // Next Best Moment, asked directly
  if (/\b(anything i should know|check my account|how'?s my account|am i (ok|okay|good)|my account)\b/.test(t)) {
    if (!c) return reply(`Check in with the Picky app and I'll look over your account: your plan, your phones, anything that's about to change on your bill.`);
    return openerInner(c, ctx, true);
  }

  if (ACCOUNT.test(t) || (HUMAN.test(t) && /\b(talk|speak|get|need|want|call)\b/.test(t))) {
    const reason = ACCOUNT.exec(t)?.[0] ?? "asked for a person";
    const x = tools.call_expert(reason);
    return reply(
      ACCOUNT.test(t)
        ? `Account changes like that need a T-Mobile expert and your ID, so I've paged one. They'll be over in about 3 minutes. I can keep helping you shop while you wait.`
        : `Of course. I've paged a T-Mobile expert, they'll be over in about 3 minutes. Want me to keep looking at anything meanwhile?`,
      [x],
    );
  }

  if (/\btrade.?in|trade (it|my)/.test(t)) {
    const x = tools.call_expert("trade-in quote");
    const ph = mayaDevices.phone;
    return reply(
      ctx.known
        ? `Trade-in values depend on the phone's condition, so an expert checks it in person, and I've paged one. Honest heads-up: your ${ph.name} still has ${ph.installments_left} payments left, ${money(ph.payoff_cents)}, which usually comes out of the trade value.`
        : `Trade-in values depend on the phone's condition, so an expert checks it in person. I've paged one, about 3 minutes.`,
      [x],
    );
  }

  if (/\b(check ?out|ring (me )?up|pay|i'?m done|that'?s it|ready to (pay|buy))\b/.test(t)) {
    if (!ctx.cart.length) return reply(`Your basket is empty right now. Tell me what you're after and I'll find it.`);
    const adv = suggest({ cart: ctx.cart, budget_cents: ctx.budget_cents, phase: "checkout", declined: new Set(ctx.declined ?? []) }).filter((c) => ctx.known || !c.reason_codes.some((r) => r === "ALREADY_OWNED" || r === "INSTALLMENTS_LEFT"));
    const x = tools.start_checkout(ctx);
    return reply(`That's ${money(cartTotal(ctx.cart))} for ${ctx.cart.reduce((n, l) => n + l.qty, 0)} item${ctx.cart.reduce((n, l) => n + l.qty, 0) > 1 ? "s" : ""}. ${adv.length ? `Before you pay: ${adv[0].title.toLowerCase()}. ${adv[0].reason} ` : ""}Confirm on the screen and I'll meet you at the counter.`, [x]);
  }

  if (/\b(basket|cart|what do i have|what'?s in|total|how much (is|for) (it all|everything))\b/.test(t) && !/\badd\b/.test(t)) {
    if (!ctx.cart.length) return reply(`Your basket is empty. What can I find for you?`);
    const adv = suggest({ cart: ctx.cart, budget_cents: ctx.budget_cents, phase: "in_trip", declined: new Set(ctx.declined ?? []) }).filter((c) => ctx.known || !c.reason_codes.some((r) => r === "ALREADY_OWNED" || r === "INSTALLMENTS_LEFT"));
    return reply(
      `You've got ${ctx.cart.map((l) => `${l.qty > 1 ? `${l.qty} ` : ""}${nice(P[l.sku]?.name ?? l.sku)}`).join(", ")}, ${money(cartTotal(ctx.cart))} total. ${adv.length ? `${adv.length > 1 ? "Two things" : "One thing"}: ${adv.slice(0, 2).map((c, i) => (i ? c.reason[0].toLowerCase() + c.reason.slice(1) : c.reason)).join(" Also, ")}` : "Everything in there makes sense to me."}`,
    );
  }

  if (/\b(remove|take (it |that )?out|drop|don'?t want|put (it|that) back|never ?mind)\b/.test(t)) {
    const target = it ?? ctx.cart.find((l) => named.includes(l.sku))?.sku ?? (ctx.cart.length === 1 ? ctx.cart[0].sku : null);
    if (!target) return reply(`Which one should I take out? You have ${ctx.cart.map((l) => nice(P[l.sku]?.name ?? l.sku)).join(", ") || "nothing yet"}.`);
    const x = tools.remove_from_basket(target, ctx);
    return reply(x.actions?.length ? `Done, I took the ${nice(P[target].name)} out. You're at ${money(cartTotal(ctx.cart.filter((l) => l.sku !== target)))}.` : `The ${nice(P[target].name)} isn't in your basket.`, [x]);
  }

  // do I need a charger / should I upgrade: the honest answers
  if (/\b(need|should i (get|buy)|do i need)\b.*\bcharger\b/.test(t)) {
    if (!ctx.known) return reply(`If you already have a USB-C charger that's 20 watts or more, you don't need a new one, it'll charge any recent phone. If not, the GoTo 20W is ${money(unitPrice("CHG-20W"))} on the accessory wall.`, [tools.where_is("CHG-20W")]);
    const own = mayaDevices.owns.find((o) => o.sku === "CHG-20W")!;
    return reply(`Honestly, skip it. You bought the ${nice(P[own.sku]?.name ?? own.sku)} on ${day(own.bought)}, and it's USB-C PD, so it charges any of these phones. That's ${money(unitPrice("CHG-20W"))} you keep.`);
  }
  if (/\b(upgrade|should i (get|buy) a new phone|is it worth|time for a new)\b/.test(t) && !/\b(add|i'?ll take|buy it)\b/.test(t)) {
    if (!c)
      return reply(`Happy to help with that. If you check in with the Picky app I can look at your phone's health, what you still owe and your trade-in value, and tell you honestly whether to upgrade now or wait. Or tell me what matters most to you in a phone.`);
    const ups = upgradesFor(c);
    if (!ups.length) {
      const line = c.account_id ? accountOf(c.account_id)?.lines[0] : null;
      if (!c.prospect && line) {
        const hh = householdResult(c);
        const planIssue = hh && hh.saves_cents < 0;
        return reply(`Your ${line.device_name} is about ${Math.round((line.device_age_months ?? 24) / 12)} years old and nothing on file says it's struggling, so I'd keep it.${planIssue ? ` If things feel slow, that's your plan, not the phone: ${hh!.best.summary} fixes it for ${money(-hh!.saves_cents)} more a month.` : ""}`, planIssue ? [tools.plan_check(ctx)] : []);
      }
      const x = tools.phone_match(ctx);
      return reply(`You're not on T-Mobile yet, so there's nothing to pay off. Here's what I'd pick for you, with the trade-offs.`, [x]);
    }
    const x = tools.upgrade_advice(ctx);
    return reply(ups.map((a) => upgradeLine(a, ups.length > 1)).join(" "), [x, ...(ups.length === 1 ? [tools.product_details(ups[0].c.target_sku, ctx)] : [])], ups[0].c.target_sku);
  }

  // Household Mix: is my plan right?
  if (/\b(right plan|my plan|overpay\w*|paying too much|save on (my )?(bill|plan)|save money|cheaper plan|family plan|best plan|which plan|lower (my )?bill)\b/.test(t)) {
    if (!c) return reply(`I can check your whole household, every line against every plan, once you check in with the Picky app.`);
    const r = householdResult(c);
    if (!r) return reply(`You don't have lines with us yet, so let's build it from scratch.`, [tools.phone_match(ctx)]);
    return reply(householdLine(c, r), [tools.plan_check(ctx)]);
  }

  // Matchmaker: which phone?
  if (/\b(which phone|what phone|best phone|right phone|find me a phone|recommend a phone|help me (choose|pick)|phone for me|cheapest good phone|fastest phone)\b/.test(t)) {
    const pr = (["camera", "battery", "price", "performance"] as const).filter((k) => t.includes(k) || (k === "price" && /\b(cheap\w*|budget|afford)/.test(t)) || (k === "performance" && /\b(fast\w*|gaming)\b/.test(t)));
    const preset = c ? presetFor(c) : null;
    if (!pr.length && !preset) return { ...reply(`Happy to help you pick. What matters most to you: camera, battery, speed, or price? And roughly what monthly budget?`), chips: MATCH_CHIPS } as ReturnType<typeof reply>;
    const budget = t.match(/\$\s?(\d+)/) ? Number(t.match(/\$\s?(\d+)/)![1]) * 100 : undefined;
    const x = tools.phone_match(ctx, { priorities: pr.length ? [...pr] : undefined, budget_cents: budget });
    const b = match((x.cards![0] as { answers: Answers }).answers).bundles[0];
    const where = b.phone_sku ? displaySlot(b.phone_sku)?.fixture_id : null;
    return reply(
      `${preset && !pr.length ? `Based on what you told me, ` : `For ${pr.join(" and ")}${budget ? ` at about ${money(budget)} a month` : ""}, `}I'd go with ${b.phone_sku ? `the ${nice(P[b.phone_sku]?.name ?? "")}` : "keeping your phone"} on ${b.plan.name}: ${money(b.monthly_cents)} a month all in. I put the other options and what you'd give up on screen.${where ? ` Want to hold one? It's on ${fixtureName(where)}.` : ""}`,
      [x],
    );
  }

  // compare two phones
  if (phones.length >= 2 && /\b(vs|versus|compare|difference|better|or)\b/.test(t)) {
    const [a, b] = phones;
    const da = tmobileUpcoming[a], db = tmobileUpcoming[b];
    const pa = linePromoFor(a), pb = linePromoFor(b);
    const cheaper = unitPrice(a) <= unitPrice(b) ? a : b;
    return reply(
      `The ${shortName(a)} is ${money(unitPrice(a))} and the ${shortName(b)} is ${money(unitPrice(b))}, so the ${shortName(cheaper)} saves ${money(Math.abs(unitPrice(a) - unitPrice(b)))}. ${[blurb(a) && `The ${shortName(a)}: ${blurb(a)}`, blurb(b) && `The ${shortName(b)}: ${blurb(b)}`].filter(Boolean).join(" ")} ${[pa && `${shortName(a)} line offer: ${pa.headline}.`, pb && `${shortName(b)} line offer: ${pb.headline}.`, da && `${shortName(a)} is expected at ${money(da.price_cents)} on ${day(da.starts)}.`, db && `${shortName(b)} is expected at ${money(db.price_cents)} on ${day(db.starts)}.`].filter(Boolean).join(" ")} Both are on the tables if you want to hold them.`,
      [{ summary: "", actions: [{ type: "show", skus: [a, b] }] }],
      cheaper,
    );
  }

  // does X fit
  if (/\b(fit|fits|compatible|work with|works with)\b/.test(t)) {
    const acc = (it && P[it]?.category !== "phone" ? it : null) ?? named.find((s) => P[s].category !== "phone");
    const d = phones.find((s) => s !== acc) ?? dev?.sku;
    if (!acc) return reply(`Which accessory? Tell me the case or charger and the phone, and I'll check.`);
    if (!d) return reply(`Which phone is it for? Say the model, or check in with the app and I'll use yours.`, [], acc);
    const x = tools.check_fit(acc, d);
    return reply(x.summary.replace(/^Yes: /, "Yes, ").replace(/^No: /, "No, ").replace(/\. One that fits: (.*?) \(([A-Z0-9-]+)\): (\$[\d.]+).*$/, ". The $1 does, for $3."), [x]);
  }

  // deals
  if (/\b(deals?|sales?|on sale|discounts?|promos?|offers?|specials?)\b/.test(t) && !subject) {
    const shelf = tmobileStore.slots.filter((s) => s.deal && stockOf(s.sku) > 0).map((s) => s.sku);
    const x = tools.deals();
    const top = [...new Set(shelf)].slice(0, 2);
    return reply(
      `${top.length ? `On the shelf right now: ${top.map((s) => `${nice(P[s].name)} for ${money(unitPrice(s))}`).join(", and ")}. ` : ""}The big phone offers are bill credits that need a new line or an upgrade, so they're not a shelf price. ${ctx.known ? "And since you're not in a rush, the Pixel 9 is expected to drop on Nov 27." : "Want me to check one for you?"}`,
      [x],
    );
  }

  // add / take
  if (/\b(add|i'?ll take|i will take|i'?ll get|get me|grab|buy|i want (it|that|this|one|the)|put .* in|bring me|i'?ll have)\b/.test(t)) {
    const sku = it ?? subject;
    if (!sku) return reply(`Sure, which one? Tell me the product and I'll grab it.`);
    const n = t.match(/\b(2|3|4|5|two|three|four|five)\s+(of (them|those|these)|cases|chargers|cables|protectors|units|boxes|packs?)\b/)?.[1];
    const qty = n ? ({ two: 2, three: 3, four: 4, five: 5 } as Record<string, number>)[n] ?? Number(n) : 1;
    const x = tools.add_to_basket(sku, Math.min(qty, 5), ctx, /\b(anyway|still|yes)\b/.test(t));
    if (x.summary.startsWith("NOT ADDED")) {
      const d = deviceOf(ctx)!;
      const right = tmobileProducts.find((q) => q.category === P[sku].category && q.compatible_with?.includes(d.sku) && stockOf(q.sku) > 0);
      return reply(`Hold on, the ${nice(P[sku].name)} is made for the ${P[sku].compatible_with!.map(shortName).join(", ")}, so it won't fit your ${d.name}.${right ? ` The ${nice(P[right.sku].name)} does, for ${money(unitPrice(right.sku))}. Want that one instead?` : ""}`, right ? [{ summary: "", actions: [{ type: "show", skus: [right.sku] }] }] : [], right?.sku ?? sku);
    }
    if (x.summary.includes("out of stock")) return reply(`Sorry, we're out of the ${nice(P[sku].name)} today. Want me to find something similar?`, [], sku);
    const adv = suggest({ cart: [...ctx.cart.filter((l) => l.sku !== sku), { sku, qty }], budget_cents: ctx.budget_cents, phase: "in_trip", declined: new Set(ctx.declined ?? []) }).filter((c) => ctx.known || !c.reason_codes.some((r) => r === "ALREADY_OWNED" || r === "INSTALLMENTS_LEFT"))[0];
    return reply(
      `${pick(["Got it", "Done", "You got it"], sku)}, the ${nice(P[sku].name)} is in your basket, ${money(unitPrice(sku) * qty)}.${backStock(sku) ? " I'll grab a sealed box from the back, one moment." : ""}${adv ? ` ${adv.kind === "skip" || adv.kind === "wait" ? "Quick heads-up:" : "One idea:"} ${adv.reason}` : ""}`,
      [x],
    );
  }

  // where is
  if (/\b(where|find|located|location|aisle|show me|take me|walk me|which (wall|table))\b/.test(t)) {
    const sku = subject ?? it;
    if (!sku) return reply(`I couldn't find that here. Try the product name, like "Pixel 9 case" or "AirPods".`);
    const x = tools.where_is(sku);
    const w = where(sku)!;
    return reply(
      w.fixture_id === "BACK-STOCK"
        ? `The ${nice(P[sku].name)} is kept in the back, so let me bring one out to you.`
        : `The ${nice(P[sku].name)} is on ${fixtureName(w.fixture_id)}. Follow me, I'll walk you there. It's ${money(unitPrice(sku))}${stockOf(sku) > 0 ? "" : ", though we're out right now"}.`,
      [x],
    );
  }

  // case / glass etc. for a device
  if (dev && CATEGORY_WORDS.slice(1, 3).some(([re]) => re.test(t))) {
    const cat = /\b(glass|screen|protector)\b/.test(t) ? "screen" : "case";
    const x = tools.accessories_for(dev.sku, cat);
    const skus = (x.actions?.[0] as { skus?: string[] })?.skus ?? [];
    if (!skus.length) return reply(`We don't stock a ${cat === "screen" ? "screen protector" : "case"} made for the ${dev.name}, sorry. An expert can order one in.`);
    return reply(
      `For the ${dev.name} we have ${skus.length} ${cat === "screen" ? "screen protectors" : "cases"}, from ${money(unitPrice(skus[0]))} for the ${nice(P[skus[0]].name)}. They're on the accessory wall. Want me to walk you over?`,
      [x],
    );
  }

  // price / details
  if (subject && /\b(how much|price|cost|tell me about|what is|what's|details|specs|any good|good)\b/.test(t)) {
    const x = tools.product_details(subject, ctx);
    const p = P[subject];
    const deal = dealOf(subject);
    const up = tmobileUpcoming[subject];
    const promo = linePromoFor(subject);
    return reply(
      `The ${nice(p.name)} is ${money(unitPrice(subject))}${deal ? `, marked down from ${money(deal.original_cents)}` : ""}. ${blurb(subject)}${promo ? ` There's also a line offer, ${promo.headline}, paid as bill credits.` : ""}${up ? ` Heads-up: it's expected at ${money(up.price_cents)} from ${day(up.starts)}.` : ""}`,
      [x],
    );
  }

  // anything else that names products: show them
  if (named.length) {
    const x = tools.search_products(t);
    const first = named[0];
    const w = where(first);
    return reply(
      named.length === 1
        ? `We have the ${nice(P[first].name)} for ${money(unitPrice(first))}, ${w?.fixture_id === "BACK-STOCK" ? "in the back" : `on ${fixtureName(w!.fixture_id)}`}. Want me to show you?`
        : `Here's what I found: ${named.slice(0, 3).map((s) => `the ${nice(P[s].name)} for ${money(unitPrice(s))}`).join(", ")}. Want me to walk you to ${w?.fixture_id === "BACK-STOCK" ? "them" : fixtureName(w!.fixture_id)}?`,
      [x],
    );
  }

  return reply(`I'm not sure I caught that. I can find a product and walk you to it, check if something fits your phone, compare phones, or get you a T-Mobile expert. What would help?`);
}

const MATCH_CHIPS = ["Best camera under $100/mo", "Best battery under $75/mo", "Cheapest good phone", "Fastest phone under $150/mo"];

/** One spoken sentence or two for a Timekeeper verdict. */
function upgradeLine(a: Advice, named: boolean) {
  const model = tradeModel(a.c.device_model_id)?.name ?? "phone";
  const who = named ? `${a.c.name.replace(/ \(.*\)$/, "")}'s ${model}: ` : "";
  const target = nice(P[a.c.target_sku]?.name ?? "");
  const issues = a.c.issues.map((i) => i.split(":")[0]);
  const issueText = issues.length ? `${issues.map((x, i) => (i ? x.charAt(0).toLowerCase() + x.slice(1) : x)).join(" and ")}.` : "";
  if (a.verdict === "upgrade_now")
    return `${who}${named ? issueText.charAt(0).toLowerCase() + issueText.slice(1) : `Honestly, your ${model} is due. ${issueText}`} The ${target} is ${money(a.now.cost_cents)} after the trade-in, and waiting doesn't save anything.`;
  if (a.verdict === "wait")
    return `${who}${named ? "" : "Honest answer: not yet. "}${a.line?.installments_left ? `Your ${model} still has ${a.line.installments_left} payments left, and ` : ""}${a.c.expected_promo ? `the ${target} is expected to drop to ${money(a.c.expected_promo.price_cents)} on ${day(a.c.expected_promo.starts)}. ` : ""}Upgrading today costs ${money(a.now.cost_cents)}; waiting costs ${money(a.later!.cost_cents)}, so you save ${money(a.saves_cents)}.`;
  return `${who}${named ? "keep it" : `Keep your ${model}`}: battery ${a.c.battery_health_pct}%, updates until ${new Date(`${a.c.os_support_until}-15T12:00:00Z`).toLocaleDateString("en-US", { month: "long", year: "numeric" })}. A new ${target} would cost ${money(a.now.cost_cents)} for no real gain.`;
}

function householdLine(c: Customer, r: NonNullable<ReturnType<typeof householdResult>>) {
  if (r.saves_cents > 0)
    return `${c.prospect ? `Bringing ${r.lines.length > 1 ? "your lines" : "your line"} over` : `You could save ${money(r.saves_cents)} a month`}: ${r.best.summary} covers ${r.lines.length > 1 ? "everyone" : "you"} for ${money(r.best.monthly_cents)} a month${r.best.streaming_paid_cents ? " including the streaming you pay for" : r.best.streaming_covered.length ? " with your streaming included as a perk" : ""}${c.prospect ? `, ${money(r.saves_cents)} less than you pay now` : `, instead of ${money(r.current_total_cents)}`}. I checked ${r.combos_checked} combinations.`;
  if (r.saves_cents < 0)
    return `Your plan doesn't fit how you use it: ${r.current_misfits.map((m) => (r.lines.length === 1 ? m.replace(/^[^:]+: /, "") : m)).join("; ")}. ${r.best.summary} fixes that for ${money(-r.saves_cents)} more a month. Nothing cheaper actually fits.`;
  return `Good news: you're already on the best setup. ${r.current_label} is ${money(r.current_total_cents)} a month, and I checked all ${r.combos_checked} combinations; nothing cheaper fits how ${r.lines.length > 2 ? "you all" : r.lines.length === 2 ? "you both" : "you"} use data.`;
}

/** What Magenta raises first when someone checks in: Next Best Moment, said to the customer. Low-risk customers just
 * get a hello (no offer); prospects get a head start on choosing. */
function openerInner(c: Customer, ctx: AssocCtx, asked = false): Omit<AssocReply, "source" | "chips" | "device"> {
  const m = momentFor(c);
  const hh = householdResult(c);
  const ups = upgradesFor(c);
  const lines: string[] = [];
  const res: ToolResult[] = [];
  if (m && m.level !== "low") {
    for (const p of m.plan) {
      if ((p.id === "right_size" || p.id === "fit_up") && hh) {
        const l0 = hh.lines[0];
        lines.push(
          p.id === "right_size"
            ? `Before anything else, you're paying for more plan than you use. ${householdLine(c, hh)}`
            : `I noticed your data has been slowing down. That's your plan, not your phone: you use about ${l0.usage_gb_month} GB a month and ${l0.hotspot_gb_month} GB of hotspot, and ${planOf(l0.current_plan_id)?.name ?? "your plan"} has ${planOf(l0.current_plan_id)?.priority_data_gb ? `${planOf(l0.current_plan_id)!.priority_data_gb} GB of` : "no"} priority data and ${planOf(l0.current_plan_id)?.hotspot_gb ?? 0} GB of hotspot. ${hh.best.summary} fixes both for ${money(-hh.saves_cents)} more a month, and the first month is on us.`,
        );
        res.push(tools.plan_check(ctx));
      }
      if (p.id === "trade_in_offer" && ups[0]) {
        lines.push(upgradeLine(ups[0], false));
        res.push(tools.upgrade_advice(ctx));
      }
      if (p.id === "tech_visit") lines.push("And about coverage at home: I can have an engineer check your address and send a signal booster if it's weak, at no cost.");
      if (p.id === "bill_explainer") lines.push(messageFor(m).replace(/^Hi \w+, /, "").replace(/^./, (x) => x.toUpperCase()));
    }
  } else if (m && m.plan[0]?.id === "right_size" && hh && hh.saves_cents > 0) {
    lines.push(`Quick thing while you're here: ${hh.best.summary} would cover how you use your phone for ${money(hh.best.monthly_cents)} a month instead of ${money(hh.current_total_cents)}, so ${money(hh.saves_cents)} less. Want me to set that up?`);
    res.push(tools.plan_check(ctx));
  } else if (c.prospect && hh && hh.saves_cents > 0) {
    lines.push(householdLine(c, hh));
    res.push(tools.plan_check(ctx));
  } else if (c.prospect && matchFor(c)) {
    const b = matchFor(c)!.bundles[0];
    lines.push(`From what you told the app, I'd start with the ${b.title}, ${money(b.monthly_cents)} a month all in. Your options and the trade-offs are on screen.`);
    res.push(tools.phone_match(ctx));
  } else if (asked) {
    lines.push(`Everything looks good on your account: no changes coming to your bill and your plan fits how you use it. Nothing I'd change today.`);
  } else lines.push("What can I help with today? Your basket, the right phone, whether to upgrade, or your plan.");
  return reply(`${asked ? "" : c.prospect ? `Hi ${c.first}, welcome to T-Mobile. ` : `Hi ${c.first}, good to see you. `}${lines.join(" ")}`, res);
}

/** Magenta's opener when someone checks in (deterministic, instant). */
export function opener(ctx: AssocCtx): AssocReply {
  const c = customerOf(ctx.customer);
  if (!c) return respond("hi", ctx);
  const r = openerInner(c, ctx);
  return { ...r, device: ctx.device ?? null, chips: chipsFor({ ...ctx, focus: r.focus }, r.actions), source: "offline" };
}

/** Follow-ups that make sense right now (shown as tap targets under the conversation). */
export function chipsFor(ctx: AssocCtx, actions: AssocAction[] = []): string[] {
  const f = ctx.focus && P[ctx.focus] ? ctx.focus : null;
  const out: string[] = [];
  if (f) {
    const p = P[f];
    if (!ctx.cart.some((l) => l.sku === f)) out.push(p.category === "phone" ? "I'll take it" : "Add it to my basket");
    if (!actions.some((a) => a.type === "guide" || a.type === "fetch")) out.push("Where is it?");
    if (p.category === "phone") out.push(`Cases for the ${shortName(f)}`);
    else if (p.compatible_with?.length) out.push("Does it fit my phone?");
  }
  if (ctx.cart.length) out.push("What's in my basket?", "Check out");
  if (!f && !ctx.cart.length) out.push(...(ctx.customer ? ["Should I upgrade?", "Am I on the right plan?", "Find me the right phone", "Anything I should know?"] : ["Find me the right phone", "Should I upgrade?", "Case for my phone", "Any deals today?"]));
  out.push("Talk to a person");
  return [...new Set(out)].slice(0, 5);
}
