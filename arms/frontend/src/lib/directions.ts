// The four other T-Mobile challenge directions, as deterministic engines over generated data (directions.json from
// data/wireless.yaml + data/fixtures/tmobile_directions.yaml). Pure functions, no LLM, same answer every run:
//   match()      The Matchmaker      a few answers -> 2-3 device + plan bundles with reasons and trade-offs
//   moment()     Next Best Moment    account signals -> churn risk (explained) + the cheapest action that fixes it
//   upgrade()    The Timekeeper      phone health + payoff + trade-in decay + promos -> upgrade now / wait / keep
//   household()  Household Mix       every line in a household -> the cheapest plan mix that fits everyone
// All money is integer cents. Everything is synthetic demo data (see the YAML headers).
import d from "@/data/generated/directions.json";
import { productBySku } from "./data";

// ------------------------------------------------------------------------------------------------ shared

export type Plan = {
  plan_id: string;
  name: string;
  tier: string;
  summary: string;
  line_prices_cents: number[];
  extra_line_cents: number;
  max_lines: number;
  autopay_discount_cents_per_line: number;
  data: string;
  priority_data_gb: number | null;
  hotspot_gb: number;
  perks: string[];
  taxes_and_fees_included?: boolean;
  requires?: string[];
};
export type Line = {
  line_id: string;
  label: string;
  device_name: string;
  device_model_id?: string;
  device_sku?: string;
  device_age_months?: number;
  installments_left?: number;
  monthly_installment_cents?: number;
  usage_gb_month: number;
  hotspot_gb_month: number;
  current_plan_id?: string;
  current_monthly_cents?: number;
};
export type Account = {
  account_id: string;
  user_id: string;
  persona: string;
  carrier: string;
  plan_id?: string;
  autopay: boolean;
  current_monthly_cents: number;
  priorities: string[];
  eligibility?: string[];
  notes?: string;
  lines: Line[];
};
type TradeModel = { model_id: string; name: string; brand: string; release_year: number; values_cents: Record<Condition, number> };
export type Condition = "excellent" | "good" | "fair" | "broken";

export const DATA = d as unknown as {
  as_of: string;
  plans: Plan[];
  accounts: Account[];
  trade_in_models: TradeModel[];
  financing: { months_options: number[]; max_financed_cents: number };
  new_line_fee_cents: number;
  add_ons: { protection: { monthly_cents: number } | null; home_internet: { monthly_cents: number } | null };
  phone_traits: Record<string, { camera: number; battery: number; performance: number; value: number; size: string }>;
  matchmaker_presets: Preset[];
  retention_accounts: RetentionAccount[];
  retention_actions: { id: string; label: string; cost_cents: number | null; fixes: string[]; kind: "fix" | "touch" | "discount" }[];
  upgrade_cases: UpgradeCase[];
  households: Household[];
};

export const money = (c: number) => `${c < 0 ? "−" : ""}$${(Math.abs(c) / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
export const money0 = (c: number) => `${c < 0 ? "−" : ""}$${Math.round(Math.abs(c) / 100).toLocaleString("en-US")}`;
const PLAN = Object.fromEntries(DATA.plans.map((p) => [p.plan_id, p]));
export const planOf = (id: string | undefined) => (id ? (PLAN[id] ?? null) : null);
export const accountOf = (id: string | undefined) => DATA.accounts.find((a) => a.account_id === id) ?? null;
const TODAY = new Date(`${DATA.as_of}T12:00:00-07:00`);
const monthLabel = (iso: string) => new Date(`${iso.length === 7 ? `${iso}-15` : iso}T12:00:00-07:00`).toLocaleDateString("en-US", { month: "short", year: "numeric" });
export const dayLabel = (iso: string) => new Date(`${iso}T12:00:00-07:00`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
const monthsUntil = (iso: string) => Math.max(0, Math.round((new Date(`${iso.length === 7 ? `${iso}-15` : iso}T12:00:00-07:00`).getTime() - TODAY.getTime()) / (30.44 * 864e5)));

/** Monthly service price for n lines on a plan, with AutoPay (before taxes). Prepaid is one line per account. */
export function planCost(p: Plan, n: number, autopay = true) {
  if (n <= 0) return 0;
  if (p.max_lines === 1) return n * (p.line_prices_cents[0] - (autopay ? p.autopay_discount_cents_per_line : 0));
  const k = Math.min(n, p.line_prices_cents.length);
  const base = p.line_prices_cents[k - 1] + Math.max(0, n - p.line_prices_cents.length) * p.extra_line_cents;
  return base - (autopay ? p.autopay_discount_cents_per_line * n : 0);
}

/** Does this line's usage fit the plan? Heavy users need priority data; hotspot needs to fit the allowance. */
export function lineFit(p: Plan, l: Line): { ok: boolean; why: string } {
  if (p.priority_data_gb === 10 && l.usage_gb_month > 10) return { ok: false, why: `uses ${l.usage_gb_month} GB, plan has 10 GB` };
  if (l.hotspot_gb_month > p.hotspot_gb) return { ok: false, why: `${l.hotspot_gb_month} GB hotspot, plan allows ${p.hotspot_gb} GB` };
  if (l.usage_gb_month >= 50 && p.priority_data_gb !== null && p.priority_data_gb < l.usage_gb_month)
    return { ok: false, why: `${l.usage_gb_month} GB/mo would be slowed (only ${p.priority_data_gb} GB priority)` };
  return { ok: true, why: l.usage_gb_month >= 50 ? "enough priority data" : `${l.usage_gb_month} GB/mo fits` };
}
const streamingPerks = (p: Plan) => (p.perks.some((x) => /two streaming/i.test(x)) ? 2 : p.perks.some((x) => /streaming/i.test(x)) ? 1 : 0);

// ------------------------------------------------------------------------------------------------ trade-in decay

const ageMonths = (m: TradeModel) => (TODAY.getFullYear() - m.release_year) * 12 + (TODAY.getMonth() - 8); // ~September launches
/** Monthly value retention per brand, fitted (log-linear least squares) to the trade-in table's excellent values. */
const RETENTION: Record<string, number> = (() => {
  const out: Record<string, number> = {};
  for (const brand of new Set(DATA.trade_in_models.map((m) => m.brand))) {
    const pts = DATA.trade_in_models.filter((m) => m.brand === brand && m.values_cents.excellent > 0).map((m) => [ageMonths(m), Math.log(m.values_cents.excellent)]);
    if (pts.length < 2) continue;
    const mx = pts.reduce((s, p) => s + p[0], 0) / pts.length, my = pts.reduce((s, p) => s + p[1], 0) / pts.length;
    const slope = pts.reduce((s, p) => s + (p[0] - mx) * (p[1] - my), 0) / pts.reduce((s, p) => s + (p[0] - mx) ** 2, 0);
    out[brand] = Math.exp(slope);
  }
  return out;
})();
export function tradeModel(id: string | undefined) {
  return DATA.trade_in_models.find((m) => m.model_id === id) ?? null;
}
export function tradeValue(id: string | undefined, condition: Condition, monthsFromNow = 0) {
  const m = tradeModel(id);
  if (!m) return 0;
  const r = RETENTION[m.brand] ?? 0.97;
  return Math.round((m.values_cents[condition] * r ** monthsFromNow) / 100) * 100;
}
export const monthlyRetention = (id: string | undefined) => RETENTION[tradeModel(id)?.brand ?? ""] ?? 0.97;

// ------------------------------------------------------------------------------------------------ The Matchmaker

export type Usage = "light" | "medium" | "heavy";
export type Priority = "price" | "camera" | "battery" | "performance" | "size";
export type Preset = {
  id: string;
  label: string;
  account_id?: string;
  says: string;
  lines: number;
  budget_monthly_cents: number;
  priorities: Priority[];
  usage: Usage;
  keep_phone: boolean;
  keep_second_phone?: boolean;
  switching?: boolean;
  age_55?: boolean;
  trade_in?: { model_id: string; condition: Condition };
};
export type Answers = Omit<Preset, "id" | "label" | "says" | "account_id">;
export type Bundle = {
  key: string;
  title: string;
  badge: string;
  phone_sku: string | null;
  plan: Plan;
  monthly_cents: number;
  plan_cents: number;
  device_cents: number;
  trade_credit_cents: number;
  due_today_cents: number;
  fits_budget: boolean;
  why: string[];
  tradeoffs: string[];
};

const USAGE_LINE: Record<Usage, { usage_gb_month: number; hotspot_gb_month: number }> = {
  light: { usage_gb_month: 6, hotspot_gb_month: 0 },
  medium: { usage_gb_month: 25, hotspot_gb_month: 5 },
  heavy: { usage_gb_month: 120, hotspot_gb_month: 40 },
};

function phoneScore(sku: string, pr: Priority[]) {
  const t = DATA.phone_traits[sku];
  if (!t) return 0;
  const w = (k: Priority) => (pr.includes(k) ? 3 - pr.indexOf(k) * 0.5 : 0.6);
  return w("camera") * t.camera + w("battery") * t.battery + w("performance") * t.performance + w("price") * t.value * 1.4 + (pr.includes("size") && t.size === "compact" ? 3 : 0);
}

/** 2-3 bundles for a shopper's answers. Plans are filtered to ones that fit their usage; phones ranked by what
 * they said matters; monthly = plan (AutoPay) + 24-month installments − trade-in spread over 24 months. */
export function match(a: Answers): { bundles: Bundle[]; notes: string[] } {
  const line: Line = { line_id: "x", label: "You", device_name: "", ...USAGE_LINE[a.usage] };
  const plans = DATA.plans
    .filter((p) => (p.max_lines >= a.lines || p.max_lines === 1) && (!p.requires?.includes("age_55") || a.age_55) && (p.max_lines > 1 || a.lines === 1) && lineFit(p, line).ok)
    .sort((x, y) => planCost(x, a.lines) - planCost(y, a.lines));
  const cheapest = plans[0];
  const premiumFit = plans.find((p) => p.tier === "premium") ?? plans.at(-1)!;
  const trade = a.trade_in ? tradeValue(a.trade_in.model_id, a.trade_in.condition) : 0;
  const tradeName = a.trade_in ? tradeModel(a.trade_in.model_id)?.name : null;
  const fee = a.switching || a.lines > 1 ? DATA.new_line_fee_cents * (a.switching ? a.lines : 0) : 0;
  const notes: string[] = [];
  const usageWords = { light: "you mostly text, call and browse", medium: "you stream and use maps a fair amount", heavy: "you use a lot of data and hotspot" }[a.usage];

  const fitsBudget = (c: number) => c <= a.budget_monthly_cents;
  const build = (key: string, title: string, badge: string, sku: string | null, plan: Plan): Bundle => {
    const price = sku ? (productBySku[sku]?.price_cents ?? 0) : 0;
    const device = Math.round(price / 24);
    const credit = sku && trade >= 5000 ? Math.round(trade / 24) : 0;
    const planC = planCost(plan, a.lines);
    const monthly = planC + device - credit;
    const t = sku ? DATA.phone_traits[sku] : null;
    const why: string[] = [];
    if (a.lines > 1) why.push(`${plan.name} for ${a.lines} lines is ${money(planC)}/mo with AutoPay${a.switching ? "" : ""}`);
    else why.push(`${plan.name} is ${money(planC)}/mo ${plan.taxes_and_fees_included ? "with taxes and fees included" : "with AutoPay"}, and ${usageWords}`);
    if (plan.requires?.includes("age_55")) why.push("You qualify for 55+ pricing");
    if (plan.hotspot_gb >= USAGE_LINE[a.usage].hotspot_gb_month && a.usage === "heavy") why.push(`${plan.priority_data_gb === null ? "Unlimited" : plan.priority_data_gb + " GB"} priority data and ${plan.hotspot_gb} GB hotspot cover your ${USAGE_LINE[a.usage].hotspot_gb_month} GB of tethering`);
    if (sku && t) {
      const best = (a.priorities.filter((p) => p !== "size" && p !== "price") as ("camera" | "battery" | "performance")[]).filter((k) => t[k] >= 4);
      if (best.length) why.push(`${nice(sku)}: strong ${best.join(" and ")} (${best.map((k) => `${t[k]}/5`).join(", ")})`);
      if (a.priorities.includes("price") && t.value >= 4) why.push(`${nice(sku)} is one of the best values here at ${money(price)}`);
      if (credit) why.push(`Your ${tradeName} (${a.trade_in!.condition}) is worth about ${money(trade)} in trade, ${money(credit)}/mo off`);
      else if (a.trade_in) why.push(`Your ${tradeName} is only worth about ${money(trade)} in trade, so keep it as a backup or recycle it`);
    }
    if (!sku) why.push(a.keep_phone ? "You said your phone works fine, so keep it: no device payment at all" : "Bring your own phone");
    if (!sku && plan.priority_data_gb !== 10 && plan.max_lines > 1 && a.usage === "light") why.push("Unlimited data, so you never think about it");
    if (a.keep_second_phone) why.push("The second line keeps its current phone");
    const tradeoffs: string[] = [];
    if (plan.priority_data_gb === 0) tradeoffs.push("Data can slow down when the network is busy (no priority data)");
    if (plan.tier === "premium" && a.priorities.includes("price")) tradeoffs.push(`Costs ${money(planCost(plan, a.lines) - planCost(cheapest, a.lines))}/mo more than ${cheapest.name}`);
    if (plan.plan_id === "prepaid_10gb") tradeoffs.push("Only 10 GB of data; no device promos");
    if (sku && t && a.priorities.includes("camera") && t.camera < 4) tradeoffs.push("Camera is good, not great");
    if (sku && t && t.battery <= 3 && a.priorities.includes("battery")) tradeoffs.push("Battery is average");
    if (sku) tradeoffs.push(`${money(price)} phone over 24 months; leave early and the balance is due`);
    if (!fitsBudget(planC + device - credit)) tradeoffs.push(`${money(planC + device - credit - a.budget_monthly_cents)}/mo over your ${money(a.budget_monthly_cents)} budget`);
    return { key, title, badge, phone_sku: sku, plan, monthly_cents: monthly, plan_cents: planC, device_cents: device, trade_credit_cents: credit, due_today_cents: fee, fits_budget: monthly <= a.budget_monthly_cents, why, tradeoffs };
  };

  const phones = Object.keys(DATA.phone_traits).filter((s) => productBySku[s]);
  const ranked = phones.sort((x, y) => phoneScore(y, a.priorities) - phoneScore(x, a.priorities) || (productBySku[x].price_cents - productBySku[y].price_cents));
  const bundles: Bundle[] = [];
  // the plan that fits their usage best: the cheapest that fits, unless heavy use needs more
  const fitPlan = a.usage === "heavy" ? premiumFit : cheapest;
  if (a.keep_phone) {
    bundles.push(build("keep", `Keep your phone on ${fitPlan.name}`, "Best value", null, fitPlan));
    const unlimited = plans.find((p) => p.data === "Unlimited" && p.plan_id !== fitPlan.plan_id && p.tier !== "premium");
    if (fitPlan.data !== "Unlimited" && unlimited) bundles.push(build("keep-unl", `Keep your phone on ${unlimited.name}`, "No data worries", null, unlimited));
    const p = ranked.find((s) => fitsBudget(build("t", "", "", s, fitPlan).monthly_cents)) ?? ranked[0];
    bundles.push(build("upgrade", `If you do want a new phone: ${nice(p)}`, "Optional", p, fitPlan));
  } else {
    const affordable = ranked.filter((s) => build("t", "", "", s, fitPlan).monthly_cents <= a.budget_monthly_cents);
    const top = affordable[0] ?? ranked[ranked.length - 1];
    bundles.push(build("best", `${nice(top)} on ${fitPlan.name}`, "Best fit", top, fitPlan));
    const must = a.priorities.find((p) => p === "camera" || p === "battery" || p === "performance");
    const cheaperPhone = [...affordable]
      .filter((s) => !must || DATA.phone_traits[s][must] >= 4)
      .sort((x, y) => productBySku[x].price_cents - productBySku[y].price_cents)
      .find((s) => s !== top && productBySku[s].price_cents < productBySku[top].price_cents - 15000);
    if (cheaperPhone) bundles.push(build("less", `${nice(cheaperPhone)} on ${cheapest.name}`, "Spend less", cheaperPhone, cheapest));
    const splurge = ranked.find((s) => s !== top && productBySku[s].price_cents > productBySku[top].price_cents);
    if (splurge && bundles.length < 3) bundles.push(build("more", `${nice(splurge)} on ${fitPlan.name}`, "Stretch pick", splurge, fitPlan));
    if (!affordable.length) notes.push(`Nothing new fits ${money(a.budget_monthly_cents)}/mo with your plan; the closest is shown. Keeping your phone is the honest cheapest option.`);
  }
  if (a.switching) notes.push(`Switching ${a.lines} line${a.lines > 1 ? "s" : ""}: ${money(DATA.new_line_fee_cents)} connection fee per line, due today.`);
  if (a.age_55) notes.push("55+ pricing applied: you qualify.");
  return { bundles: bundles.slice(0, 3), notes };
}
const nice = (sku: string) => (productBySku[sku]?.name ?? sku).replace(/\s+\d+\s*GB$/i, "");

// ------------------------------------------------------------------------------------------------ Next Best Moment

export type RetentionAccount = {
  account_id: string;
  name: string;
  monthly_revenue_cents: number;
  tenure_months: number;
  support_contacts_90d: string[];
  bill_change_pct_6mo: number;
  competitor_signals: string[];
  promo_credit_ending: { month: string; monthly_cents: number; what: string } | null;
  plan_fit: "ok" | "overpaying" | "throttled" | "undersized" | "eligible_cheaper";
  device_age_months?: number;
};
export type Driver = { id: string; weight: number; text: string; root: boolean; fixed_by?: string };
export type Moment = {
  acct: RetentionAccount;
  risk: number;
  level: "high" | "medium" | "low";
  drivers: Driver[];
  plan: { id: string; label: string; cost_cents: number; detail: string; kind: string }[];
  risk_after: number;
  revenue_now_cents: number;
  revenue_after_cents: number;
  /** expected service revenue over the next 12 months: do nothing vs. do the plan (minus its cost) */
  expected_no_action_cents: number;
  expected_with_plan_cents: number;
  cost_cents: number;
  discount_only_cents: number;
  verdict: string;
};

const SIGNAL: Record<string, [string, number, string]> = {
  port_out_pin: ["competitor_shopping", 0.35, "Asked for a number-transfer PIN"],
  payoff_lookup: ["competitor_shopping", 0.15, "Looked up their device payoff amount"],
  switching_help_page: ["competitor_shopping", 0.12, "Read the 'switching carriers' help page"],
  competitor_device_deals: ["competitor_shopping", 0.12, "Browsing competitor phone deals"],
  speed_test_competitor_ssid: ["competitor_shopping", 0.1, "Ran speed tests on a competitor's network"],
};
const TOPIC: Record<string, string> = {
  bill_increase: "bill went up",
  coverage_home: "coverage at home",
  slow_data: "slow data",
  hotspot_throttled: "hotspot slowed",
  bill_question: "bill question",
  battery_drain: "battery drain",
  data_cap: "hit the data cap",
};

export function moment(a: RetentionAccount): Moment {
  const drivers: Driver[] = [];
  const add = (id: string, w: number, text: string) => {
    const ex = drivers.find((x) => x.id === id);
    if (ex) {
      ex.weight += w;
      ex.text += `; ${text}`;
    } else drivers.push({ id, weight: w, text, root: !SYMPTOMS.has(id) });
  };
  if (a.support_contacts_90d.length)
    add("support_friction", Math.min(0.28, a.support_contacts_90d.length * 0.07), `${a.support_contacts_90d.length} support contact${a.support_contacts_90d.length > 1 ? "s" : ""} in 90 days (${[...new Set(a.support_contacts_90d.map((t) => TOPIC[t] ?? t))].join(", ")})`);
  if (a.support_contacts_90d.some((t) => t.startsWith("coverage"))) add("coverage", 0.1, "Called about coverage at home");
  if (a.bill_change_pct_6mo >= 10) add("bill_shock", Math.min(0.3, a.bill_change_pct_6mo * 0.012), `Bill up ${a.bill_change_pct_6mo}% in 6 months`);
  if (a.promo_credit_ending) add("promo_ending", 0.2, `${a.promo_credit_ending.what} in ${monthLabel(a.promo_credit_ending.month)}: bill goes up ${money(a.promo_credit_ending.monthly_cents)}/mo`);
  for (const s of a.competitor_signals) if (SIGNAL[s]) add(SIGNAL[s][0], SIGNAL[s][1], SIGNAL[s][2]);
  if (a.plan_fit === "overpaying") add("overpaying", 0.15, "Pays for more plan than they use");
  if (a.plan_fit === "throttled") add("throttled", 0.2, "Heavy data on a plan with no priority data: slowed down");
  if (a.plan_fit === "undersized") add("undersized", 0.2, "Hits the data cap every month");
  if ((a.device_age_months ?? 0) >= 42) add("old_device", 0.1, `Phone is ${a.device_age_months} months old`);
  let risk = drivers.reduce((s, x) => s + x.weight, 0);
  if (a.tenure_months < 12) risk += 0.1;
  if (a.tenure_months > 96) risk -= 0.1;
  risk = Math.max(0.02, Math.min(0.95, Math.round(risk * 100) / 100));
  const level = risk >= 0.55 ? "high" : risk >= 0.25 ? "medium" : "low";
  drivers.sort((x, y) => y.weight - x.weight);

  // what we know about their lines (household optimizer) prices the right-size and the plan they'd move to
  const hh = DATA.households.find((h) => h.account_ids?.includes(a.account_id));
  const opt = hh ? household(hh) : null;
  const revenueNow = opt ? opt.current_service_cents : a.monthly_revenue_cents;
  let revenueAfter = revenueNow;
  const roots = drivers.filter((x) => x.root && x.weight >= 0.1);

  // Policy: fix the cause, cheapest fix first. Competitor shopping and support calls are symptoms: when there is a
  // cause to fix, fixing it addresses them. Discounts only when there's nothing to fix.
  const plan: Moment["plan"] = [];
  const act = (id: string) => DATA.retention_actions.find((x) => x.id === id)!;
  const push = (id: string, cost: number) => {
    if (plan.some((p) => p.id === id)) return;
    const x = act(id);
    plan.push({ id, label: x.label, cost_cents: cost, detail: detail(id, a, opt), kind: x.kind });
  };
  if (level === "low") {
    if (a.plan_fit === "eligible_cheaper" && opt && opt.saves_cents > 0) {
      plan.push({ id: "right_size", label: "Tell them about the cheaper plan they qualify for", cost_cents: 0, detail: detail("right_size", a, opt), kind: "fix" });
      revenueAfter = opt.best.service_cents;
    } else plan.push({ id: "none", label: "Do nothing. Don't send an offer.", cost_cents: 0, detail: "No risk signals. A discount here would just give money away and teach them to ask for one.", kind: "touch" });
  } else {
    for (const dr of roots) {
      if (dr.fixed_by) continue;
      const pick = DATA.retention_actions.filter((x) => x.kind === "fix" && x.fixes.includes(dr.id)).sort((x, y) => (x.cost_cents ?? 0) - (y.cost_cents ?? 0))[0];
      if (!pick) continue;
      let cost = pick.cost_cents ?? 0;
      if (pick.id === "right_size") {
        if (!opt || opt.best.service_cents >= revenueNow) continue;
        revenueAfter = opt.best.service_cents;
        cost = 0; // the price change shows up in revenue, not as a cost
      }
      if (pick.id === "fit_up" && opt) revenueAfter = opt.best.service_cents;
      if (pick.id === "fit_up" && !opt) revenueAfter = a.plan_fit === "undersized" ? planCost(planOf("value_unlimited")!, 1) : revenueNow;
      drivers.filter((x) => pick.fixes.includes(x.id)).forEach((x) => (x.fixed_by = pick.label));
      push(pick.id, cost);
    }
    const rootFixed = plan.length > 0;
    for (const dr of drivers.filter((x) => !x.root)) {
      if (rootFixed) dr.fixed_by = `fixing the cause (${roots.filter((r) => r.fixed_by).map((r) => r.id.replace(/_/g, " ")).join(", ")})`;
      else if (dr.id === "support_friction") {
        push("check_in", act("check_in").cost_cents!);
        dr.fixed_by = act("check_in").label;
      } else if (dr.id === "competitor_shopping") {
        push("perk", act("perk").cost_cents!);
        dr.fixed_by = act("perk").label;
      }
    }
  }

  const fixed = drivers.filter((x) => x.fixed_by).reduce((s, x) => s + x.weight, 0);
  const riskAfter = level === "low" ? risk : Math.max(0.03, Math.round((risk - fixed * 0.8) * 100) / 100);
  const cost = plan.reduce((s, p) => s + p.cost_cents, 0);
  const noAction = Math.round(revenueNow * 12 * (1 - risk));
  const withPlan = Math.round(revenueAfter * 12 * (1 - riskAfter)) - cost;
  const discountOnly = act("loyalty_credit").cost_cents!;
  const cost0 = (c: number) => (c ? money0(c) : "$0");
  const verdict =
    level === "low"
      ? plan[0].id === "none"
        ? "Low risk. Leave them alone: no offer, no discount."
        : `Low risk, but they could pay ${money0((revenueNow - revenueAfter) * 12)} a year less. It costs us that revenue; tell them anyway, before they find out on their own.`
      : `${level === "high" ? "High" : "Medium"} risk, driven by ${roots.map((r) => r.id.replace(/_/g, " ")).join(" and ") || "competitor shopping"}. Fix that (${cost0(cost)}${revenueAfter < revenueNow ? `, and ${money0(revenueNow - revenueAfter)}/mo less on the bill` : revenueAfter > revenueNow ? `, and they pay ${money0(revenueAfter - revenueNow)}/mo more for a plan that works` : ""}) instead of a blanket discount.`;
  return {
    acct: a,
    risk,
    level,
    drivers,
    plan,
    risk_after: riskAfter,
    revenue_now_cents: revenueNow,
    revenue_after_cents: revenueAfter,
    expected_no_action_cents: noAction,
    expected_with_plan_cents: withPlan,
    cost_cents: cost,
    discount_only_cents: discountOnly,
    verdict,
  };
}

const SYMPTOMS = new Set(["competitor_shopping", "support_friction"]);

function detail(id: string, a: RetentionAccount, opt: ReturnType<typeof household> | null) {
  switch (id) {
    case "right_size":
      return opt ? `${opt.best.summary}: ${money(opt.best.monthly_cents)}/mo instead of ${money(opt.current_service_cents)}. They save ${money(opt.saves_cents)}/mo; we keep the account.` : "Move to the plan that matches their usage.";
    case "fit_up":
      return a.plan_fit === "throttled"
        ? "Their plan has no priority data, which is exactly why data slows. Move them to the plan with priority data and hotspot that fits, first month on us."
        : "They hit the cap every month. Move them to unlimited, first month on us.";
    case "bill_explainer":
      return a.promo_credit_ending ? `Message before ${monthLabel(a.promo_credit_ending.month)}: the ${money(a.promo_credit_ending.monthly_cents)} credit ends because the phone is paid off, not a price hike. Include their upgrade options.` : "Walk through what changed on the bill and why.";
    case "trade_in_offer":
      return "Send their personal trade-in estimate and when upgrading makes sense (The Timekeeper), instead of a generic deal.";
    case "tech_visit":
      return "Have an engineer check coverage at their address; send a signal booster if it's weak.";
    case "check_in":
      return "A short message from a real person: we saw your calls, here's what we fixed.";
    default:
      return "";
  }
}

/** The note the customer actually gets (empty when the right move is to leave them alone). */
export function messageFor(m: Moment): string {
  const a = m.acct;
  // greet the account holder by first name (the first line on the account), else the name on file
  const first = accountOf(a.account_id)?.lines[0]?.label ?? a.name.replace(/ family$/, "").split(/[ ,]/)[0];
  const hh = DATA.households.find((h) => h.account_ids?.includes(a.account_id));
  const opt = hh ? household(hh) : null;
  const parts: string[] = [];
  for (const p of m.plan) {
    if (p.id === "right_size" && opt)
      parts.push(`we looked at how your ${opt.lines.length > 1 ? `${opt.lines.length} lines` : "line"} actually use data, and you're paying for more than you need. ${opt.best.summary} covers everyone for ${money(opt.best.monthly_cents)}/mo${opt.best.streaming_paid_cents ? " (keeping your streaming)" : ""}, ${money(opt.saves_cents)} less than today. Want us to switch it? Nothing else changes.`);
    if (p.id === "fit_up")
      parts.push(a.plan_fit === "throttled" ? "your data has been slowing down because your plan has no priority data, not because of your phone. Moving you to a plan with priority data and a bigger hotspot fixes it, and your first month on it is on us." : "you've hit your data cap every month lately. An unlimited plan would stop that; your first month on it is on us.");
    if (p.id === "bill_explainer" && a.promo_credit_ending)
      parts.push(`a heads-up so it's not a surprise: in ${monthLabel(a.promo_credit_ending.month)} your bill goes up ${money(a.promo_credit_ending.monthly_cents)} because the ${a.promo_credit_ending.what.replace(/ ends$/, "")} finishes. Your plan price isn't changing. If you'd like, we can show you your options.`);
    if (p.id === "trade_in_offer") parts.push("your phone is getting older, so here's your personal trade-in estimate and an honest take on when upgrading actually makes sense, no pressure.");
    if (p.id === "tech_visit") parts.push("you mentioned coverage at home. We're having an engineer check your address, and if the signal is weak we'll send a booster at no cost.");
  }
  return parts.length ? `Hi ${first}, ${parts.join(" Also, ")}` : "";
}

export const moments = () => DATA.retention_accounts.map(moment).sort((x, y) => y.risk - x.risk);

// ------------------------------------------------------------------------------------------------ The Timekeeper

export type UpgradeCase = {
  id: string;
  name: string;
  account_id: string;
  line_id: string;
  device_model_id: string;
  condition: Condition;
  battery_health_pct: number;
  storage_used_pct: number;
  os_support_until: string;
  issues: string[];
  target_sku: string;
  expected_promo?: { starts: string; price_cents: number; reason: string };
};
export type Advice = {
  c: UpgradeCase;
  line: Line | null;
  verdict: "upgrade_now" | "wait" | "keep";
  headline: string;
  reasons: string[];
  now: { label: string; cost_cents: number; parts: [string, number][] };
  later: { label: string; when: string; months: number; cost_cents: number; parts: [string, number][] } | null;
  saves_cents: number;
  curve: { m: number; v: number }[];
  value_loss_per_month_cents: number;
  health: { label: string; value: string; ok: boolean }[];
};

export function upgrade(c: UpgradeCase): Advice {
  const acct = accountOf(c.account_id);
  const line = acct?.lines.find((l) => l.line_id === c.line_id) ?? null;
  const model = tradeModel(c.device_model_id)!;
  const payoff = (line?.installments_left ?? 0) * (line?.monthly_installment_cents ?? 0);
  const price = productBySku[c.target_sku]?.price_cents ?? 0;
  const target = nice(c.target_sku);
  const tradeNow = tradeValue(c.device_model_id, c.condition);
  const curve = Array.from({ length: 13 }, (_, m) => ({ m, v: tradeValue(c.device_model_id, c.condition, m) }));
  const lossPerMonth = Math.round((tradeNow - curve[3].v) / 3);
  const supportEnded = monthsUntil(c.os_support_until) <= 0;
  const health = [
    { label: "Battery health", value: `${c.battery_health_pct}%`, ok: c.battery_health_pct >= 80 },
    { label: "Storage used", value: `${c.storage_used_pct}%`, ok: c.storage_used_pct < 90 },
    { label: "Security updates", value: supportEnded ? "ended" : `until ${monthLabel(c.os_support_until)}`, ok: !supportEnded },
    { label: "Payments left", value: payoff ? `${line!.installments_left} × ${money(line!.monthly_installment_cents!)}` : "paid off", ok: !payoff },
    { label: "Trade-in today", value: `${money(tradeNow)} (${c.condition})`, ok: true },
  ];
  const nowParts: [string, number][] = [[`${target}`, price], [`Trade in your ${model.name}`, -tradeNow]];
  if (payoff) nowParts.push([`Pay off the ${model.name} early`, payoff]);
  const nowCost = nowParts.reduce((s, p) => s + p[1], 0);

  // waiting: until the promo (if any), else until the phone is paid off, else 6 months
  const waitMonths = c.expected_promo ? Math.max(1, monthsUntil(c.expected_promo.starts)) : payoff ? line!.installments_left! : 6;
  const when = c.expected_promo ? dayLabel(c.expected_promo.starts) : `in ${waitMonths} months`;
  const tradeLater = tradeValue(c.device_model_id, c.condition, waitMonths);
  const laterPrice = c.expected_promo?.price_cents ?? price;
  const laterParts: [string, number][] = [[c.expected_promo ? `${target} at the expected promo price` : target, laterPrice], [`Trade in (worth less by then)`, -tradeLater]];
  const laterCost = laterParts.reduce((s, p) => s + p[1], 0);
  const saves = nowCost - laterCost;

  const problems = [...c.issues];
  let verdict: Advice["verdict"];
  let headline: string;
  const reasons: string[] = [];
  if (supportEnded || problems.length >= 2) {
    verdict = "upgrade_now";
    headline = supportEnded ? `Upgrade now: your ${model.name} stopped getting security updates` : `Upgrade now: your ${model.name} is holding you back`;
    reasons.push(...problems);
    reasons.push(`Its trade-in value is ${money(tradeNow)} and drops about ${money(lossPerMonth)} a month, so waiting doesn't pay.`);
    if (c.target_sku) reasons.push(`The ${target} at ${money(price)} is the sensible pick here, not the most expensive one.`);
  } else if (saves >= 5000) {
    verdict = "wait";
    headline = `Wait until ${when}: you save ${money0(saves)}`;
    if (payoff) reasons.push(`Your ${model.name} still has ${line!.installments_left} payments left (${money(payoff)}). Upgrading now means paying that off on top.`);
    if (c.expected_promo) reasons.push(`${c.expected_promo.reason} is expected ${dayLabel(c.expected_promo.starts)}: ${money(c.expected_promo.price_cents)} instead of ${money(price)}.`);
    reasons.push(`Waiting costs you about ${money(tradeNow - tradeLater)} in trade-in value. That's far less than you save.`);
    reasons.push("Nothing about your phone needs fixing today.");
  } else {
    verdict = "keep";
    headline = `Keep your ${model.name}. It's doing fine.`;
    reasons.push(`Battery ${c.battery_health_pct}%, storage ${c.storage_used_pct}% full, security updates until ${monthLabel(c.os_support_until)}.`);
    reasons.push(`A new ${target} would cost ${money(nowCost)} after trade-in for a phone that does the same things for you.`);
    reasons.push(tradeNow >= 10000 ? `Your trade-in value (${money(tradeNow)}) slips about ${money(lossPerMonth)} a month; that's not a reason to buy early.` : `Its trade-in value is already low (${money(tradeNow)}), so there's no rush to cash it in.`);
  }
  return {
    c,
    line,
    verdict,
    headline,
    reasons,
    now: { label: "Upgrade today", cost_cents: nowCost, parts: nowParts },
    later: verdict === "keep" ? null : { label: `Upgrade ${c.expected_promo ? "on " : ""}${when}`, when, months: waitMonths, cost_cents: laterCost, parts: laterParts },
    saves_cents: saves,
    curve,
    value_loss_per_month_cents: lossPerMonth,
    health,
  };
}
export const upgrades = () => DATA.upgrade_cases.map(upgrade);

// ------------------------------------------------------------------------------------------------ Household Mix

export type Household = {
  id: string;
  name: string;
  account_ids?: string[];
  separate?: boolean;
  lines?: Line[];
  age_55_lines?: string[];
  streaming: { name: string; monthly_cents: number }[];
  note: string;
};
export type Mix = {
  assign: Record<string, string>;
  groups: { plan: Plan; lines: Line[]; cents: number }[];
  service_cents: number;
  streaming_paid_cents: number;
  streaming_covered: string[];
  monthly_cents: number;
  summary: string;
};

export function householdLines(h: Household): { lines: Line[]; current_service_cents: number; current_label: string; eligible55: Set<string> } {
  if (h.separate && h.lines) {
    return { lines: h.lines, current_service_cents: h.lines.reduce((s, l) => s + (l.current_monthly_cents ?? 0), 0), current_label: `${h.lines.length} separate accounts`, eligible55: new Set(h.age_55_lines ?? []) };
  }
  const accts = (h.account_ids ?? []).map(accountOf).filter((a): a is Account => !!a);
  const lines = accts.flatMap((a) => a.lines.map((l) => ({ ...l, current_plan_id: a.plan_id })));
  const cur = accts.reduce((s, a) => {
    const p = planOf(a.plan_id);
    return s + (p ? planCost(p, a.lines.length, a.autopay) : a.current_monthly_cents);
  }, 0);
  const label = accts.map((a) => (planOf(a.plan_id) ? `${planOf(a.plan_id)!.name}, ${a.lines.length} line${a.lines.length > 1 ? "s" : ""}` : `${a.carrier}`)).join(" + ");
  const e55 = new Set([...(h.age_55_lines ?? []), ...accts.filter((a) => a.eligibility?.includes("age_55")).flatMap((a) => a.lines.map((l) => l.line_id))]);
  return { lines, current_service_cents: cur, current_label: label, eligible55: e55 };
}

function priceMix(assign: Record<string, string>, lines: Line[], streaming: Household["streaming"]): Mix {
  const groups: Mix["groups"] = [];
  for (const pid of new Set(Object.values(assign))) {
    const plan = planOf(pid)!;
    const ls = lines.filter((l) => assign[l.line_id] === pid);
    groups.push({ plan, lines: ls, cents: planCost(plan, ls.length) });
  }
  const service = groups.reduce((s, g) => s + g.cents, 0);
  let perks = groups.reduce((s, g) => s + streamingPerks(g.plan), 0);
  const covered: string[] = [];
  let paid = 0;
  for (const st of [...streaming].sort((x, y) => y.monthly_cents - x.monthly_cents)) {
    if (perks > 0) {
      perks--;
      covered.push(st.name);
    } else paid += st.monthly_cents;
  }
  const summary = groups.map((g) => `${g.lines.length > 1 ? `${g.lines.length} lines on ` : ""}${g.plan.name}${g.lines.length === 1 && groups.length > 1 ? ` (${g.lines[0].label})` : ""}`).join(" + ");
  return { assign, groups, service_cents: service, streaming_paid_cents: paid, streaming_covered: covered, monthly_cents: service + paid, summary };
}

/** Try every way to put each line on a plan (a plan used by several lines = one shared account), keep the ones where
 * every line fits its plan, and rank by what the household actually pays (service + streaming not covered by perks). */
export function household(h: Household) {
  const { lines, current_service_cents, current_label, eligible55 } = householdLines(h);
  const options = (l: Line) => DATA.plans.filter((p) => (!p.requires?.includes("age_55") || eligible55.has(l.line_id)) && lineFit(p, l).ok);
  const mixes: Mix[] = [];
  const walk = (i: number, assign: Record<string, string>) => {
    if (i === lines.length) {
      const counts: Record<string, number> = {};
      Object.values(assign).forEach((p) => (counts[p] = (counts[p] ?? 0) + 1));
      if (Object.entries(counts).every(([p, n]) => planOf(p)!.max_lines === 1 || n <= planOf(p)!.max_lines)) mixes.push(priceMix({ ...assign }, lines, h.streaming));
      return;
    }
    for (const p of options(lines[i])) walk(i + 1, { ...assign, [lines[i].line_id]: p.plan_id });
  };
  walk(0, {});
  mixes.sort((x, y) => x.monthly_cents - y.monthly_cents || x.groups.length - y.groups.length);
  const seen = new Set<string>();
  const ranked = mixes.filter((m) => (seen.has(m.summary) ? false : (seen.add(m.summary), true)));
  const best = ranked[0];
  const curStreamingPaid = (() => {
    const curAssign = Object.fromEntries(lines.map((l) => [l.line_id, l.current_plan_id ?? ""]));
    if (Object.values(curAssign).some((p) => !planOf(p))) return h.streaming.reduce((s, x) => s + x.monthly_cents, 0);
    return priceMix(curAssign, lines, h.streaming).streaming_paid_cents;
  })();
  const current_total = current_service_cents + curStreamingPaid;
  const currentFits = lines.map((l) => {
    const p = planOf(l.current_plan_id);
    return p ? lineFit(p, l) : { ok: true, why: "other carrier" };
  });
  const oneAccount = ranked.find((m) => m.groups.length === 1) ?? null;
  return {
    h,
    lines,
    current_label,
    current_service_cents,
    current_streaming_cents: curStreamingPaid,
    current_total_cents: current_total,
    current_misfits: lines.flatMap((l, i) => (currentFits[i].ok ? [] : [`${l.label}: ${currentFits[i].why}`])),
    best,
    alternatives: ranked.slice(1, 4),
    one_account: oneAccount && oneAccount !== best ? oneAccount : null,
    saves_cents: current_total - best.monthly_cents,
    combos_checked: mixes.length,
  };
}
export const households = () => DATA.households.map(household);
