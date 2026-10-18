// Cart Whisperer (T-Mobile track): checkout suggestions that actually help. docs/cart-whisperer.md §4–§5.
// Deterministic, in the browser on demo data until the recommender's `suggest()` lands behind the gateway (contracts
// v0.3): candidates → hard gates (code) → score + reason codes → top 0–3. Zero is a valid answer. An LLM may only
// re-phrase `reason` afterwards (/api/whisper validates every number); it never adds, drops, reorders or reprices.
import { mayaDevices, tmobileProducts, tmobileUpcoming, type WirelessProduct } from "@/data/stores/tmobile";
import { STORES, slotsOf } from "./data";
import { WIRELESS_MANDATE } from "./order";

export type Kind = "complete" | "swap_save" | "qty_fix" | "remove_warn" | "skip" | "wait" | "deal_match";
export type Action = "add" | "swap" | "remove" | "set_qty" | "remind" | "none";
export type Phase = "planning" | "in_trip" | "checkout";
export type ReasonCode =
  | "ALREADY_OWNED"
  | "INCOMPATIBLE"
  | "PROMO_SOON"
  | "INSTALLMENTS_LEFT"
  | "OVER_BUDGET"
  | "COMPLETES_OCCASION"
  | "QTY_MISMATCH"
  | "CHEAPER_SAME_NEED"
  | "DEAL_MATCH";

export type WLine = { sku: string; qty: number };
export type Suggestion = {
  id: string;
  kind: Kind;
  action: Action;
  sku: string;
  replaces_sku?: string;
  qty: number;
  title: string;
  reason: string;
  reason_codes: ReasonCode[];
  /** derived signals only (what the shopper may see, and all the LLM gets) */
  signals: Record<string, string | number>;
  /** plain-words "Why am I seeing this?" list */
  why: string[];
  price_delta_cents: number;
  savings_cents: number;
  avoided_cents: number;
  remind_on?: string;
  detour_m: number;
  where: string;
  score: number;
  needs_approval: boolean;
  sponsored: false;
};

export type WContext = {
  cart: WLine[];
  intent?: string;
  budget_cents: number;
  phase: Phase;
  declined?: Set<string>;
  /** sku the robot just picked (in-trip physical trigger) */
  last_picked?: string | null;
};

export const MIN_SCORE = 0.35;
export const MAX_CARDS = 3;
const TMO = "store_tmobile" as const;

const P: Record<string, WirelessProduct> = Object.fromEntries(tmobileProducts.map((p) => [p.sku, p]));
const money = (c: number) => `$${(c / 100).toFixed(2)}`;
const date = (iso: string) => new Date(`${iso}T12:00:00-07:00`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });

export function unitPrice(sku: string) {
  return slotsOf(TMO)[sku]?.deal?.price_cents ?? P[sku]?.price_cents ?? 0;
}
export function cartTotal(cart: WLine[]) {
  return cart.reduce((s, l) => s + l.qty * unitPrice(l.sku), 0);
}

function fixtureLabel(sku: string) {
  const slot = slotsOf(TMO)[sku];
  const f = STORES[TMO].fixtures.find((x) => x.fixture_id === slot?.fixture_id);
  return f ? f.label.split(":")[0] : "in store";
}
/** Extra walking to add this slot: Manhattan distance to the nearest stop already on the list (§5 "detour cost"). */
function detourM(sku: string, cart: WLine[]) {
  const s = slotsOf(TMO)[sku];
  if (!s) return 0;
  const others = cart.map((l) => slotsOf(TMO)[l.sku]).filter(Boolean);
  if (!others.length) return 0;
  if (others.some((o) => o.fixture_id === s.fixture_id)) return 0;
  const d = Math.min(...others.map((o) => Math.abs(o.approach_pose.x - s.approach_pose.x) + Math.abs(o.approach_pose.y - s.approach_pose.y)));
  return Math.round(d * 10) / 10;
}
const inStock = (sku: string) => (slotsOf(TMO)[sku]?.qty ?? 0) > 0;
const isPhone = (sku: string) => P[sku]?.category === "phone";
const fits = (acc: string, device: string) => {
  const c = P[acc]?.compatible_with ?? [];
  return c.length === 0 || c.includes(device);
};

/** Which phone the accessories are for: a phone in the cart, a phone named in the request, else the one she owns. */
export function targetDevice(ctx: Pick<WContext, "cart" | "intent">): { sku: string; name: string; isNew: boolean } {
  const inCart = [...ctx.cart].reverse().find((l) => isPhone(l.sku));
  if (inCart) return { sku: inCart.sku, name: shortName(inCart.sku), isNew: true };
  const t = (ctx.intent ?? "").toLowerCase();
  const named = tmobileProducts
    .filter((p) => p.category === "phone")
    .map((p) => ({ p, key: p.name.toLowerCase().replace(/\s+\d+\s*gb$/, "") }))
    .sort((a, b) => b.key.length - a.key.length)
    .find(({ key }) => t.includes(key));
  if (named) return { sku: named.p.sku, name: shortName(named.p.sku), isNew: true };
  // accessories in the cart that all fit one phone imply that phone
  const fitSets = ctx.cart.map((l) => P[l.sku]?.compatible_with ?? []).filter((c) => c.length === 1);
  if (fitSets.length && fitSets.every((c) => c[0] === fitSets[0][0]) && P[fitSets[0][0]])
    return { sku: fitSets[0][0], name: shortName(fitSets[0][0]), isNew: fitSets[0][0] !== mayaDevices.phone.sku };
  return { sku: mayaDevices.phone.sku, name: mayaDevices.phone.name, isNew: false };
}
export function shortName(sku: string) {
  if (sku === mayaDevices.phone.sku) return mayaDevices.phone.name;
  return (P[sku]?.name ?? sku).replace(/\s+\d+\s*GB.*$/, "");
}

function card(c: Omit<Suggestion, "id" | "sponsored" | "needs_approval" | "where" | "detour_m"> & { detour_m?: number; where?: string }): Suggestion {
  return {
    id: `sg_${c.kind}_${c.sku}${c.replaces_sku ? `_${c.replaces_sku}` : ""}`.toLowerCase(),
    sponsored: false,
    needs_approval: false,
    detour_m: c.detour_m ?? 0,
    where: c.where ?? fixtureLabel(c.sku),
    ...c,
  };
}

/** All candidates, gated and scored (before the threshold and the top-3 cut). Exported for the "how it decided" view. */
export function candidates(ctx: WContext): Suggestion[] {
  const out: Suggestion[] = [];
  const dev = targetDevice(ctx);
  const ph = mayaDevices.phone;
  const total = cartTotal(ctx.cart);
  const has = (pred: (p: WirelessProduct) => boolean) => ctx.cart.some((l) => P[l.sku] && pred(P[l.sku]));
  const owned = new Map(mayaDevices.owns.map((o) => [o.sku, o]));

  for (const l of ctx.cart) {
    const p = P[l.sku];
    if (!p) continue;
    const line = l.qty * unitPrice(l.sku);
    // SKIP: accessory that doesn't fit the device it's for (hard gate turned into a card, §5 step 2)
    if (p.compatible_with?.length && !p.compatible_with.includes(dev.sku) && p.category !== "phone") {
      const right = ctx.cart.find((o) => o.sku !== l.sku && P[o.sku]?.category === p.category && fits(o.sku, dev.sku));
      const forName = p.compatible_with.length === 1 ? shortName(p.compatible_with[0]) : "other phones";
      out.push(
        card({
          kind: "skip",
          action: "remove",
          sku: l.sku,
          qty: 0,
          title: `Skip the ${p.name.replace(/\s*\(.*\)/, "")}`,
          reason: `It's made for the ${forName}, so it won't fit your ${dev.name}.${right ? ` The ${P[right.sku].name.split(" for ")[0]} in your cart does.` : ""}${slotsOf(TMO)[l.sku]?.deal ? ` The ${money(line)} clearance price isn't a deal if it doesn't fit.` : ""}`,
          reason_codes: ["INCOMPATIBLE"],
          signals: { fits: forName, your_device: dev.name, line_cents: line },
          why: [`Fits: ${forName}`, `Your device: ${dev.name}${dev.isNew ? " (the one you're buying)" : ""}`, `Saves ${money(line)}`],
          price_delta_cents: -line,
          savings_cents: 0,
          avoided_cents: line,
          score: 0.95,
        }),
      );
      continue;
    }
    // SKIP: already owned and still works with the device (owned charger is universal USB-C PD)
    const own = owned.get(l.sku) ?? (p.subcategory === "wall_charger" ? owned.get("CHG-20W") : undefined);
    if (own && (own.fits.length === 0 || own.fits.includes(dev.sku))) {
      out.push(
        card({
          kind: "skip",
          action: "remove",
          sku: l.sku,
          qty: 0,
          title: `Skip the ${p.subcategory === "wall_charger" ? "charger" : p.name}`,
          reason: `You bought a ${own.name} on ${date(own.bought)} and it works with the ${dev.name}.`,
          reason_codes: ["ALREADY_OWNED"],
          signals: { owned: own.name, bought: own.bought, line_cents: line },
          why: [`You own: ${own.name} (bought ${date(own.bought)})`, "Universal USB-C PD, works with any USB-C phone", `Saves ${money(line)}`],
          price_delta_cents: -line,
          savings_cents: 0,
          avoided_cents: line,
          score: 0.9,
        }),
      );
      continue;
    }
    // QTY: more than one case / glass / phone for one device is almost always a mistake
    if (l.qty > 1 && ["case", "screen", "phone"].includes(p.category)) {
      const cut = (l.qty - 1) * unitPrice(l.sku);
      out.push(
        card({
          kind: "qty_fix",
          action: "set_qty",
          sku: l.sku,
          qty: 1,
          title: `Make it 1 ${p.category === "screen" ? "screen protector" : p.category}`,
          reason: `${l.qty} × ${p.name} for one ${dev.name}. One is enough: −${money(cut)}.`,
          reason_codes: ["QTY_MISMATCH"],
          signals: { qty: l.qty, devices: 1, cut_cents: cut },
          why: [`Quantity: ${l.qty}`, `Phones it's for: 1 (${dev.name})`],
          price_delta_cents: -cut,
          savings_cents: cut,
          avoided_cents: 0,
          score: 0.8,
        }),
      );
    }
    // WAIT: a cheaper promo is on the calendar
    const up = tmobileUpcoming[l.sku];
    if (up && up.price_cents < unitPrice(l.sku)) {
      const save = (unitPrice(l.sku) - up.price_cents) * l.qty;
      const installments = isPhone(l.sku) && ph.installments_left > 0;
      out.push(
        card({
          kind: "wait",
          action: "remind",
          sku: l.sku,
          qty: 0,
          title: `Wait on the ${shortName(l.sku)}`,
          reason: installments
            ? `Your ${ph.name} still has ${ph.installments_left} installments left (${money(ph.installments_left * ph.monthly_installment_cents)}), and ${up.reason.toLowerCase()} on ${date(up.starts)}. I'll remind you.`
            : `${up.reason}: ${money(up.price_cents)} instead of ${money(unitPrice(l.sku))}. I'll text you on ${date(up.starts)}.`,
          reason_codes: installments ? ["PROMO_SOON", "INSTALLMENTS_LEFT"] : ["PROMO_SOON"],
          signals: { promo_price_cents: up.price_cents, today_cents: unitPrice(l.sku), starts: up.starts, ...(installments ? { installments_left: ph.installments_left, payoff_cents: ph.installments_left * ph.monthly_installment_cents } : {}) },
          why: [
            `Promo calendar: ${money(up.price_cents)} from ${date(up.starts)} (expected)`,
            `Today: ${money(unitPrice(l.sku))}`,
            ...(installments ? [`${ph.name}: ${ph.installments_left} payments left, ${ph.age_months} months old`] : []),
          ],
          price_delta_cents: -l.qty * unitPrice(l.sku),
          savings_cents: 0,
          avoided_cents: save,
          remind_on: up.starts,
          score: Math.min(1, 0.45 + save / 40000 + (installments ? 0.2 : 0)),
        }),
      );
    }
    // SWAP: same need, same phone, cheaper (cases only, ≥ $10 saved)
    if (p.category === "case" && p.subcategory !== "grip") {
      const alt = tmobileProducts
        .filter((q) => q.category === "case" && q.subcategory !== "grip" && q.sku !== l.sku && fits(q.sku, dev.sku) && (q.compatible_with?.length ?? 0) > 0 && inStock(q.sku))
        .filter((q) => !ctx.cart.some((c) => c.sku === q.sku))
        .sort((a, b) => unitPrice(a.sku) - unitPrice(b.sku) || a.sku.localeCompare(b.sku))[0];
      const save = alt ? (unitPrice(l.sku) - unitPrice(alt.sku)) * l.qty : 0;
      if (alt && save >= 1000)
        out.push(
          card({
            kind: "swap_save",
            action: "swap",
            sku: alt.sku,
            replaces_sku: l.sku,
            qty: l.qty,
            title: `Swap to the ${alt.brand} case`,
            reason: `${alt.name} also fits the ${dev.name} and is ${money(unitPrice(alt.sku))} vs ${money(unitPrice(l.sku))}: save ${money(save)}, same wall.`,
            reason_codes: ["CHEAPER_SAME_NEED"],
            signals: { fits: dev.name, save_cents: save, from_cents: unitPrice(l.sku), to_cents: unitPrice(alt.sku) },
            why: [`Both fit: ${dev.name}`, `${money(unitPrice(l.sku))} → ${money(unitPrice(alt.sku))}`, "Same accessory wall, +0 m"],
            price_delta_cents: -save,
            savings_cents: save,
            avoided_cents: 0,
            score: Math.min(0.9, 0.35 + save / 4000),
          }),
        );
    }
  }

  // QTY: two different cases (or screen protectors) for the same phone; keep the cheaper one
  for (const cat of ["case", "screen"]) {
    const same = ctx.cart.filter((l) => P[l.sku]?.category === cat && P[l.sku].subcategory !== "grip" && (P[l.sku].compatible_with?.length ?? 0) > 0 && fits(l.sku, dev.sku));
    if (same.length < 2) continue;
    const [keep, ...extra] = [...same].sort((a, b) => unitPrice(a.sku) - unitPrice(b.sku) || a.sku.localeCompare(b.sku));
    const drop = extra.at(-1)!;
    const cut = unitPrice(drop.sku) * drop.qty;
    const label = cat === "screen" ? "screen protectors" : "cases";
    out.push(
      card({
        kind: "qty_fix",
        action: "remove",
        sku: drop.sku,
        qty: 0,
        title: `Two ${label} for one ${dev.name}?`,
        reason: `You have the ${P[keep.sku].name.split(" for ")[0]} (${money(unitPrice(keep.sku))}) and the ${P[drop.sku].name.split(" for ")[0]} (${money(unitPrice(drop.sku))}). Keep one: drop the pricier one and save ${money(cut)}.`,
        reason_codes: ["QTY_MISMATCH"],
        signals: { device: dev.name, keep_cents: unitPrice(keep.sku), drop_cents: cut },
        why: [`${same.length} ${label} in the cart`, `Phones they're for: 1 (${dev.name})`, `Keeping the cheaper one saves ${money(cut)}`],
        price_delta_cents: -cut,
        savings_cents: cut,
        avoided_cents: 0,
        score: 0.82,
      }),
    );
  }

  // COMPLETE: a new phone (or its case) without protection
  if (dev.isNew) {
    const hasCase = has((p) => p.category === "case" && p.subcategory !== "grip" && fits(p.sku, dev.sku) && !!p.compatible_with?.length);
    const hasGlass = has((p) => p.category === "screen" && fits(p.sku, dev.sku));
    const pick = (cat: string) =>
      tmobileProducts
        .filter((q) => q.category === cat && q.subcategory !== "grip" && (q.compatible_with ?? []).includes(dev.sku) && inStock(q.sku))
        .sort((a, b) => Number(!!slotsOf(TMO)[b.sku]?.deal) - Number(!!slotsOf(TMO)[a.sku]?.deal) || unitPrice(a.sku) - unitPrice(b.sku))[0];
    const want = hasCase && !hasGlass ? pick("screen") : !hasCase && (hasGlass || has((p) => p.sku === dev.sku)) ? pick("case") : undefined;
    if (want) {
      const deal = slotsOf(TMO)[want.sku]?.deal;
      const price = unitPrice(want.sku);
      const d = detourM(want.sku, ctx.cart);
      out.push(
        card({
          kind: deal ? "deal_match" : "complete",
          action: "add",
          sku: want.sku,
          qty: 1,
          title: `Add ${want.category === "screen" ? "a screen protector" : "a case"}`,
          reason: `You have a ${hasCase ? "case" : "screen protector"} for the ${dev.name} but no ${hasCase ? "screen protector" : "case"}. ${want.name} is ${money(price)}${deal ? ` (${deal.reason.toLowerCase()})` : ""}, ${d === 0 ? "same wall" : `+${d} m`}.`,
          reason_codes: deal ? ["COMPLETES_OCCASION", "DEAL_MATCH"] : ["COMPLETES_OCCASION"],
          signals: { device: dev.name, price_cents: price, ...(deal ? { was_cents: deal.original_cents } : {}), detour_m: d },
          why: [`Setting up: ${dev.name}`, `Missing: ${hasCase ? "screen protector" : "case"}`, ...(deal ? [`Deal: ${deal.reason}`] : []), d === 0 ? "On your route (+0 m)" : `Detour +${d} m`],
          price_delta_cents: price,
          savings_cents: deal ? deal.original_cents - deal.price_cents : 0,
          avoided_cents: 0,
          detour_m: d,
          score: 0.55 + (deal ? 0.1 : 0),
        }),
      );
    }
    // SKIP before it happens: new phones prompt a charger; hers already works (planning template candidate)
    const chg = mayaDevices.owns.find((o) => o.sku === "CHG-20W");
    if (chg && ctx.phase === "planning" && !has((p) => p.subcategory === "wall_charger"))
      out.push(
        card({
          kind: "skip",
          action: "none",
          sku: "CHG-20W",
          qty: 0,
          title: "Skip a new charger",
          reason: `You bought a ${chg.name} on ${date(chg.bought)}. It's USB-C PD, so it charges the ${dev.name} too.`,
          reason_codes: ["ALREADY_OWNED"],
          signals: { owned: chg.name, bought: chg.bought, typical_add_cents: unitPrice("CHG-20W") },
          why: [`You own: ${chg.name} (bought ${date(chg.bought)})`, "New phones don't include a charger, so most people add one", `Avoids at least ${money(unitPrice("CHG-20W"))}`],
          price_delta_cents: 0,
          savings_cents: 0,
          avoided_cents: unitPrice("CHG-20W"),
          score: 0.5,
        }),
      );
    // WAIT on the upgrade itself when she's still paying off her phone and no phone is in the cart
    const up = tmobileUpcoming[dev.sku];
    if (!has((p) => p.category === "phone") && ph.installments_left > 0 && up && dev.sku !== ph.sku)
      out.push(
        card({
          kind: "wait",
          action: "remind",
          sku: dev.sku,
          qty: 0,
          title: `About the ${dev.name} itself`,
          reason: `Your ${ph.name} has ${ph.installments_left} installments left (${money(ph.installments_left * ph.monthly_installment_cents)}). ${up.reason} on ${date(up.starts)}: ${money(up.price_cents)}. I'll remind you.`,
          reason_codes: ["INSTALLMENTS_LEFT", "PROMO_SOON"],
          signals: { installments_left: ph.installments_left, payoff_cents: ph.installments_left * ph.monthly_installment_cents, promo_price_cents: up.price_cents, starts: up.starts },
          why: [`${ph.name}: ${ph.installments_left} payments left (${money(ph.monthly_installment_cents)}/mo)`, `Promo calendar: ${money(up.price_cents)} from ${date(up.starts)} (expected)`],
          price_delta_cents: 0,
          savings_cents: 0,
          avoided_cents: unitPrice(dev.sku) - up.price_cents,
          remind_on: up.starts,
          score: 0.6,
        }),
      );
  }

  // OVER BUDGET: drop the add that hurts least (the cheapest non-phone line that isn't protection)
  if (total > ctx.budget_cents && ctx.cart.length > 1) {
    const over = total - ctx.budget_cents;
    const drop = [...ctx.cart]
      .filter((l) => P[l.sku] && !["phone", "case", "screen"].includes(P[l.sku].category))
      .sort((a, b) => Math.abs(unitPrice(a.sku) * a.qty - over) - Math.abs(unitPrice(b.sku) * b.qty - over))[0];
    if (drop)
      out.push(
        card({
          kind: "remove_warn",
          action: "remove",
          sku: drop.sku,
          qty: 0,
          title: `Drop the ${P[drop.sku].name.split(" with ")[0]}?`,
          reason: `It puts you ${money(over)} over your ${money(ctx.budget_cents)} budget. Without it you're at ${money(total - unitPrice(drop.sku) * drop.qty)}.`,
          reason_codes: ["OVER_BUDGET"],
          signals: { over_cents: over, budget_cents: ctx.budget_cents, line_cents: unitPrice(drop.sku) * drop.qty },
          why: [`Cart ${money(total)} vs budget ${money(ctx.budget_cents)}`, `Over by ${money(over)}`],
          price_delta_cents: -unitPrice(drop.sku) * drop.qty,
          savings_cents: unitPrice(drop.sku) * drop.qty,
          avoided_cents: 0,
          score: 0.85,
        }),
      );
  }

  // Hard gates that apply to every card (code, never the LLM)
  const m = WIRELESS_MANDATE;
  return out
    .filter((s) => !ctx.declined?.has(s.id))
    .filter((s) => s.action !== "add" || (inStock(s.sku) && !ctx.cart.some((l) => l.sku === s.sku) && !m.blocked_categories.includes(P[s.sku]?.category ?? "")))
    .filter((s) => s.action !== "add" || unitPrice(s.sku) <= m.per_item_cap_cents)
    .map((s) => {
      const after = total + s.price_delta_cents;
      const ask = s.price_delta_cents > 0 && (after > ctx.budget_cents || unitPrice(s.sku) > m.ask_above_cents || after > m.cap_cents);
      return ask ? { ...s, needs_approval: true, why: [...s.why, after > ctx.budget_cents ? `Needs your OK: ${money(after - ctx.budget_cents)} over budget` : "Needs your OK (above your ask-me line)"] } : s;
    });
}

/** The cards to show: score ≥ threshold, de-duplicated per SKU, best first, at most 3. May be empty. */
export function suggest(ctx: WContext): Suggestion[] {
  const seen = new Set<string>();
  return candidates(ctx)
    .filter((s) => s.score >= MIN_SCORE)
    .sort((a, b) => b.score - a.score || a.sku.localeCompare(b.sku))
    .filter((s) => (seen.has(s.sku) ? false : (seen.add(s.sku), true)))
    .slice(0, MAX_CARDS);
}

/** Apply an accepted card to the cart (normal cart change; the mandate check runs on the result). */
export function applySuggestion(cart: WLine[], s: Suggestion): WLine[] {
  switch (s.action) {
    case "add":
      return [...cart, { sku: s.sku, qty: s.qty }];
    case "swap":
      return cart.map((l) => (l.sku === s.replaces_sku ? { sku: s.sku, qty: l.qty } : l));
    case "set_qty":
      return cart.map((l) => (l.sku === s.sku ? { ...l, qty: s.qty } : l));
    case "remove":
    case "remind":
      return cart.filter((l) => l.sku !== s.sku);
    default:
      return cart;
  }
}

export type Metrics = { passes: number; zero_passes: number; shown: number; accepted: number; declined: number; saved_cents: number; avoided_cents: number; added_cents: number };
export const emptyMetrics = (): Metrics => ({ passes: 0, zero_passes: 0, shown: 0, accepted: 0, declined: 0, saved_cents: 0, avoided_cents: 0, added_cents: 0 });
export function recordAccept(m: Metrics, s: Suggestion): Metrics {
  return {
    ...m,
    accepted: m.accepted + 1,
    saved_cents: m.saved_cents + s.savings_cents + (s.kind === "remove_warn" || s.kind === "qty_fix" ? 0 : 0),
    avoided_cents: m.avoided_cents + s.avoided_cents,
    added_cents: m.added_cents + Math.max(0, s.price_delta_cents),
  };
}
