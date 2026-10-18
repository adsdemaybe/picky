// Assistant tools: deterministic answers over Maya's purchases, budget and the price layer.
// The LLM picks tools and phrases results; every number comes from here ("the LLM proposes; code decides").
import { mayaDevices } from "@/data/stores/tmobile";
import { CATS, MONTHLY_BUDGET_CENTS, TODAY, catOf, isWireless, maya, mostBought, productBySku, tripsFrom, type CatId, type Purchase } from "../data";
import { fmt } from "../money";
import { effective, offersFor, searchProducts, upcomingFor, verdictFor } from "../prices";

export type Card =
  | { type: "price"; sku: string }
  | { type: "products"; skus: string[]; title: string }
  | { type: "spend"; title: string; rows: { label: string; cents: number; color?: string }[]; total: number }
  | { type: "budget"; spent: number; budget: number; pace: number; projected: number }
  | { type: "savings"; items: { text: string; save_cents: number; sku?: string }[]; total: number }
  | { type: "afford"; verdict: "yes" | "tight" | "no"; amount: number; left_before: number; left_after: number; note: string };

export type ToolResult = { summary: string; card?: Card };
const DAY = 86_400_000;

function inPeriod(p: Purchase, period: string) {
  const t = Date.parse(p.ts);
  if (period === "this_month") return new Date(t).getMonth() === TODAY.getMonth() && new Date(t).getFullYear() === TODAY.getFullYear();
  if (period === "last_month") return new Date(t).getMonth() === TODAY.getMonth() - 1;
  if (period === "last_30_days") return TODAY.getTime() - t <= 30 * DAY;
  return TODAY.getTime() - t <= 84 * DAY; // last_12_weeks
}

const PERIOD_LABEL: Record<string, string> = {
  this_month: "October so far",
  last_month: "September",
  last_30_days: "the last 30 days",
  last_12_weeks: "the last 12 weeks",
};

export function spendingSummary(purchases: Purchase[], period = "this_month"): ToolResult {
  const ps = purchases.filter((p) => inPeriod(p, period));
  const by: Record<CatId, number> = { produce: 0, pantry: 0, dairy: 0, snacks: 0, home: 0 };
  for (const p of ps) by[catOf(productBySku[p.sku]?.category ?? "")] += p.qty * p.unit_price_cents;
  const total = Object.values(by).reduce((s, v) => s + v, 0);
  const rows = CATS.map((c) => ({ label: c.label, cents: by[c.id], color: c.color })).sort((a, b) => b.cents - a.cents);
  const trips = tripsFrom(ps).length;
  const top = mostBought(ps, 3).map((m) => `${productBySku[m.sku].name} (${fmt(m.spend)})`);
  return {
    summary: `Spending in ${PERIOD_LABEL[period]}: ${fmt(total)} over ${trips} trips. By category: ${rows.map((r) => `${r.label} ${fmt(r.cents)}`).join(", ")}. Biggest line items: ${top.join(", ")}.`,
    card: { type: "spend", title: `Spending in ${PERIOD_LABEL[period]}`, rows, total },
  };
}

export function budgetStatus(purchases: Purchase[]): ToolResult {
  const spent = purchases.filter((p) => inPeriod(p, "this_month")).reduce((s, p) => s + p.qty * p.unit_price_cents, 0);
  const pace = Math.round((MONTHLY_BUDGET_CENTS * TODAY.getDate()) / 31);
  const projected = Math.round((spent / TODAY.getDate()) * 31);
  return {
    summary: `Monthly grocery budget ${fmt(MONTHLY_BUDGET_CENTS)}. Spent ${fmt(spent)} by Oct ${TODAY.getDate()} (pace line ${fmt(pace)}), ${fmt(MONTHLY_BUDGET_CENTS - spent)} left. Projected month total ${fmt(projected)}. Trip cap from the mandate: ${fmt(maya.mandate.cap_cents)}; asks before any item over ${fmt(maya.mandate.ask_above_cents)}.`,
    card: { type: "budget", spent, budget: MONTHLY_BUDGET_CENTS, pace, projected },
  };
}

export function priceCheck(query: string): ToolResult {
  const p = productBySku[query.toUpperCase()] ?? searchProducts(query, 1)[0];
  if (!p) return { summary: `No product matched "${query}".` };
  const offers = offersFor(p.sku);
  const v = verdictFor(p.sku);
  const best = offers[0];
  const up = upcomingFor(p.sku);
  return {
    summary: `${p.name}: verdict ${v.label} (${v.why}). Offers by effective per-item price: ${offers
      .map((o) => `${o.retailer} ${fmt(effective(o))}${o.pack > 1 ? ` (${o.pack}-pack ${fmt(o.price_cents)})` : ""}${o.fee_note ? ` [${o.fee_note}]` : ""}${o.in_stock ? "" : " OUT OF STOCK"}`)
      .join("; ")}. Cheapest: ${best?.retailer} at ${best ? fmt(effective(best)) : "n/a"}.${up ? ` Upcoming: ${up.reason}.` : ""}`,
    card: { type: "price", sku: p.sku },
  };
}

export function findProducts(query: string): ToolResult {
  const ps = searchProducts(query, 6);
  if (!ps.length) return { summary: `Nothing in the catalog matched "${query}".` };
  return {
    summary: `Matches for "${query}": ${ps.map((p) => `${p.name} [${p.sku}] best ${fmt(effective(offersFor(p.sku)[0]))}`).join("; ")}.`,
    card: { type: "products", skus: ps.map((p) => p.sku), title: `Results for "${query}"` },
  };
}

export function findSavings(purchases: Purchase[]): ToolResult {
  const recent = purchases.filter((p) => inPeriod(p, "last_12_weeks"));
  const items: { text: string; save_cents: number; sku?: string }[] = [];
  // 1) cheaper retailer for things bought repeatedly (monthly saving at current cadence)
  for (const m of mostBought(recent, 12)) {
    const offers = offersFor(m.sku).filter((o) => o.in_stock);
    const cheapest = offers[0];
    if (!cheapest) continue;
    const perMonth = (m.qty / 12) * 4.3;
    const save = Math.round((m.avg_unit_cents - effective(cheapest)) * perMonth);
    if (save >= 50 && cheapest.retailer_id !== "store_ave")
      items.push({
        text: `${productBySku[m.sku].name}: ${cheapest.retailer} is ${fmt(effective(cheapest))} vs the ${fmt(m.avg_unit_cents)} you usually pay${cheapest.pack > 1 ? ` (${cheapest.pack}-pack)` : ""}${cheapest.fee_note?.includes("membership") ? " (needs the $60 a year membership)" : ""}`,
        save_cents: save,
        sku: m.sku,
      });
  }
  // 2) wait for announced sales on staples
  for (const m of mostBought(recent, 15)) {
    const up = upcomingFor(m.sku);
    if (up && up.price_cents < m.avg_unit_cents)
      items.push({ text: `${productBySku[m.sku].name}: ${up.reason}. Hold off until then.`, save_cents: m.avg_unit_cents - up.price_cents, sku: m.sku });
  }
  items.sort((a, b) => b.save_cents - a.save_cents);
  const top = items.slice(0, 5);
  const total = top.reduce((s, i) => s + i.save_cents, 0);
  return {
    summary: `Savings ideas (estimated per month at current buying rate): ${top.map((i) => `${i.text} → save ~${fmt(i.save_cents)}`).join("; ")}. Total ~${fmt(total)}/month.`,
    card: { type: "savings", items: top, total },
  };
}

export function canIAfford(purchases: Purchase[], amount_cents: number, what: string): ToolResult {
  const p = searchProducts(what, 1)[0];
  const isTech = p && isWireless(p.sku) && p.subcategory !== "home_internet";
  if (isTech) {
    // wireless purchases: compare against discretionary tech room, the mandate, and the phone plan
    const owned = mayaDevices.owns.find((o) => o.sku === p.sku);
    const ph = mayaDevices.phone;
    const note = owned
      ? `You already own the ${owned.name}.`
      : p.subcategory === "phones"
        ? `Your ${ph.name} still has ${ph.installments_left} installments of ${fmt(ph.monthly_installment_cents)} left (${fmt(ph.installments_left * ph.monthly_installment_cents)}). Upgrading now adds that payoff on top.`
        : `Cheapest is ${offersFor(p.sku)[0].retailer} at ${fmt(effective(offersFor(p.sku)[0]))} (${fmt(p.price_cents)} new). It's tech, so it doesn't touch October's grocery budget.`;
    const verdict = owned ? "no" : amount_cents > 30000 ? "tight" : "yes";
    return {
      summary: `Affordability for ${what} at ${fmt(amount_cents)}: ${verdict}. ${note} Verdict on price: ${verdictFor(p.sku).label} (${verdictFor(p.sku).why}).`,
      card: { type: "afford", verdict, amount: amount_cents, left_before: 0, left_after: 0, note },
    };
  }
  const spent = purchases.filter((q) => inPeriod(q, "this_month")).reduce((s, q) => s + q.qty * q.unit_price_cents, 0);
  const left = MONTHLY_BUDGET_CENTS - spent;
  const daysLeft = 31 - TODAY.getDate();
  const weekly = mostBought(purchases.filter((q) => inPeriod(q, "last_12_weeks")), 50).reduce((s, m) => s + m.spend, 0) / 12;
  const needed = Math.round((weekly * daysLeft) / 7);
  const after = left - amount_cents;
  const verdict = after - needed >= 0 ? "yes" : after >= 0 ? "tight" : "no";
  const note =
    verdict === "yes"
      ? `After it you'd still have ${fmt(after)}, and your usual groceries for the rest of October run about ${fmt(needed)}.`
      : verdict === "tight"
        ? `It fits, but leaves ${fmt(after)} for ~${fmt(needed)} of usual groceries through Oct 31.`
        : `It would put October ${fmt(-after)} over budget.`;
  return {
    summary: `Affordability for ${what} at ${fmt(amount_cents)}: ${verdict}. Budget left this month ${fmt(left)}. ${note}`,
    card: { type: "afford", verdict, amount: amount_cents, left_before: left, left_after: after, note },
  };
}

// ---------------------------------------------------------------- offline responder
// Used when no Anthropic credentials are configured, so the demo always answers.

export function localAnswer(q: string, purchases: Purchase[]): { text: string; cards: Card[] } {
  const t = q.toLowerCase();
  const money = t.match(/\$\s?(\d+(?:\.\d{1,2})?)/);
  const cards: Card[] = [];
  const say = (r: ToolResult) => (r.card && cards.push(r.card), r);

  if (/afford|can i (buy|get)|should i (buy|get|upgrade)/.test(t)) {
    const what = t.replace(/.*(afford|buy|get|upgrade to|upgrade)\s+(an?\s+|the\s+)?/, "").replace(/\?.*$/, "").replace(/for \$.*/, "").trim() || "it";
    const p = searchProducts(what, 1)[0];
    const amount = money ? Math.round(Number(money[1]) * 100) : p ? effective(offersFor(p.sku)[0]) : 0;
    const r = say(canIAfford(purchases, amount, what));
    if (p) say(priceCheck(p.sku));
    const card = r.card as Extract<Card, { type: "afford" }>;
    const lead = card.verdict === "yes" ? "Yes." : card.verdict === "tight" ? "You can, but it's tight." : "I'd hold off.";
    return { text: `${lead} ${p ? `Price check: ${verdictFor(p.sku).label.toLowerCase()}. ${verdictFor(p.sku).why}` : card.note}`, cards };
  }
  if (/save|saving|cheaper|cut|reduce/.test(t)) {
    const r = say(findSavings(purchases));
    const c = r.card as Extract<Card, { type: "savings" }>;
    return { text: `I found about ${fmt(c.total)} a month you could keep without changing what you eat. The biggest one: ${c.items[0]?.text ?? "none"}.`, cards };
  }
  if (/budget|left|pace|on track/.test(t)) {
    const r = say(budgetStatus(purchases));
    const c = r.card as Extract<Card, { type: "budget" }>;
    return {
      text: `You've spent ${fmt(c.spent)} of ${fmt(c.budget)} this month, ${c.spent <= c.pace ? `${fmt(c.pace - c.spent)} under pace` : `${fmt(c.spent - c.pace)} over pace`}. At this rate October ends near ${fmt(c.projected)}.`,
      cards,
    };
  }
  if (/spend|spent|spending|where.*money|overspend/.test(t)) {
    const period = /last month|september/.test(t) ? "last_month" : /12 weeks|quarter|three months/.test(t) ? "last_12_weeks" : "this_month";
    const r = say(spendingSummary(purchases, period));
    const c = r.card as Extract<Card, { type: "spend" }>;
    return { text: `${fmt(c.total)} in ${PERIOD_LABEL[period]}. ${c.rows[0].label} is the biggest slice at ${fmt(c.rows[0].cents)}.`, cards };
  }
  if (/good (price|deal)|worth|price check|how much|cheapest|where.*(buy|get)|compare/.test(t)) {
    const what = t.replace(/^(is|what's|whats|where('s| is)?|how much is|find me|compare)\s+/, "").replace(/(a )?good (price|deal)|worth it|the cheapest|cheapest|price check|for|on|\?/g, " ").trim();
    const r = say(priceCheck(what));
    if (r.card?.type !== "price") return { text: `I couldn't find "${what}" in the stores I can see.`, cards };
    const p = productBySku[r.card.sku];
    const best = offersFor(p.sku)[0];
    const v = verdictFor(p.sku);
    return { text: `${p.name}: ${v.label.toLowerCase()}. ${v.why} Cheapest right now is ${best.retailer} at ${fmt(effective(best))}${best.pack > 1 ? ` each (${best.pack}-pack)` : ""}.`, cards };
  }
  const r = say(findProducts(t));
  if (r.card) return { text: `Here's what I found across the stores I can see.`, cards };
  return {
    text: "I can check whether a price is good, compare stores, find cheaper swaps, tell you where your money went, or whether something fits your budget. Try “Is coffee a good price right now?” or “Can I afford the Pixel Buds?”",
    cards,
  };
}
