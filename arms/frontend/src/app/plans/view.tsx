"use client";
// T-Mobile store: plans, household optimizer, trade-in, financing, compatible accessories and a full cost quote.
// Every price, credit, saving and total on this screen comes from the gateway's wireless arm (POST/GET
// /api/wireless/*, deterministic code over data/wireless.yaml + the catalog). The web app never computes one; with
// the gateway down it shows the static plan list from the same YAML and says that estimates need the backend.
import { ArrowRight, Bot, CircleAlert, Info, Minus, Plus, ShieldCheck, Smartphone, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { Money, Panel, ProductTile, SectionTitle } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { tmobileProducts, wireless } from "@/data/stores/tmobile";
import { STORES, productBySku } from "@/lib/data";
import { errorText } from "@/lib/gateway/client";
import { useGateway } from "@/lib/gateway/useGateway";
import { wirelessApi } from "@/lib/gateway/wireless";
import type {
  CompatibleAccessory,
  CostBreakdown,
  FinancingQuote,
  HouseholdOptimization,
  PlanQuote,
  TradeInCondition,
  TradeInQuote,
} from "@/lib/gateway/wireless-types";
import { fmt } from "@/lib/money";
import { cn } from "@/lib/utils";

const CONDITIONS: TradeInCondition[] = ["excellent", "good", "fair", "broken"];
const phones = tmobileProducts.filter((p) => p.subcategory === "phones");
const store = STORES.store_tmobile;

/** Fetch from the gateway when it's live. While a new `key` loads, the last result stays (dimmed); `error` = the
 *  gateway refused (404/409/422) or didn't answer, shown in place of the result. */
function useLive<T>(live: boolean, key: string, load: () => Promise<T>) {
  const [state, setState] = useState<{ key: string; data: T | null; error: string | null }>({ key: "", data: null, error: null });
  useEffect(() => {
    if (!live) return;
    let off = false;
    load()
      .then((data) => !off && setState({ key, data, error: null }))
      .catch((e) => !off && setState({ key, data: null, error: errorText(e) }));
    return () => {
      off = true;
    };
    // `key` captures every input of `load`
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [live, key]);
  const loading = state.key !== key;
  return { data: state.data, error: loading ? null : state.error, loading };
}

export default function PlansPage() {
  const status = useGateway((s) => s.status);
  const live = status === "live";
  useEffect(() => useGateway.getState().startPolling(), []);

  const first = wireless.accounts.find((a) => a.user_id === "maya") ?? wireless.accounts[0];
  const [lines, setLines] = useState(first?.lines.length || 1);
  const [autopay, setAutopay] = useState(true);
  const [userId, setUserId] = useState(first?.user_id ?? "maya");
  const [planId, setPlanId] = useState<string | null>(first?.plan_id ?? null);
  const [phoneSku, setPhoneSku] = useState("PX9-128");
  const [financed, setFinanced] = useState(true);
  const [months, setMonths] = useState(wireless.financing.months_options[0] ?? 24);
  const [newLines, setNewLines] = useState(0);
  const [portIn, setPortIn] = useState(false);
  const [tradeModel, setTradeModel] = useState<string>(first?.lines[0]?.device_model_id ?? "");
  const [condition, setCondition] = useState<TradeInCondition>("good");
  const [accessories, setAccessories] = useState<string[]>([]);
  const [protection, setProtection] = useState(false);

  const account = wireless.accounts.find((a) => a.user_id === userId) ?? null;
  // picking a household defaults the line count, plan and trade-in device to what that household has
  const selectAccount = (id: string) => {
    const a = wireless.accounts.find((x) => x.user_id === id);
    setUserId(id);
    if (!a) return;
    setLines(a.lines.length || 1);
    setNewLines(0);
    setPlanId(a.plan_id ?? null);
    setTradeModel(a.lines[0]?.device_model_id ?? "");
    setAccessories([]);
  };

  const plans = useLive<PlanQuote[]>(live, `plans:${lines}:${autopay}:${userId}`, () => wirelessApi.plans({ lines, autopay, user_id: userId }));
  const household = useLive<HouseholdOptimization>(live, `hh:${userId}`, () => wirelessApi.household({ user_id: userId }));
  const finance = useLive<FinancingQuote>(live && financed, `fin:${phoneSku}:${months}:${planId}:${newLines}:${!!tradeModel}:${portIn}`, () =>
    wirelessApi.financing({ sku: phoneSku, months, plan_id: planId, new_line: newLines > 0, trade_in: !!tradeModel, port_in: portIn }),
  );
  const compat = useLive<CompatibleAccessory[]>(live, `compat:${phoneSku}`, () => wirelessApi.compatible(phoneSku));
  const trade = useLive<TradeInQuote>(live && !!tradeModel, `trade:${tradeModel}:${condition}:${userId}:${phoneSku}:${planId}`, () =>
    wirelessApi.tradeIn({ model_id: tradeModel, condition, user_id: userId, target_sku: phoneSku, plan_id: planId }),
  );
  const quoteReq = useMemo(
    () => ({
      store_id: "store_tmobile",
      user_id: userId,
      items: [{ sku: phoneSku, qty: 1, financed, months }, ...accessories.map((sku) => ({ sku, qty: 1 }))],
      plan_id: planId,
      lines,
      new_lines: Math.min(newLines, lines),
      autopay,
      port_in: portIn,
      trade_in: tradeModel ? { model_id: tradeModel, condition } : null,
      add_on_promo_ids: protection ? ["protection_360"] : [],
      term_months: months,
    }),
    [userId, phoneSku, financed, months, accessories, planId, lines, newLines, autopay, portIn, tradeModel, condition, protection],
  );
  const quote = useLive<CostBreakdown>(live, `quote:${JSON.stringify(quoteReq)}`, () => wirelessApi.quote(quoteReq));

  const toggleAcc = (sku: string) => setAccessories((a) => (a.includes(sku) ? a.filter((x) => x !== sku) : [...a, sku]));

  return (
    <div className="flex flex-col gap-8">
      <section className="animate-rise">
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant="grape">{store.name}</Badge>
          <Badge variant={live ? "leaf" : "muted"}>{live ? "Live estimates from the backend" : status === "checking" ? "Checking the backend…" : "Backend offline"}</Badge>
        </div>
        <h1 className="mt-3 text-[34px] leading-[1.05] font-bold md:text-[44px]">Plans, trade-in and financing</h1>
        <p className="mt-2 max-w-2xl text-[15.5px] text-muted-foreground">
          Compare plans for your household, see what your phone is worth, and get a full estimate with one-time and monthly costs kept apart. Picky will
          tell you when keeping your phone or your plan is the better deal.
        </p>
        <Notice className="mt-4" />
      </section>

      {/* ---------------------------------------------------------------- plans */}
      <section>
        <SectionTitle
          action={
            <label className="flex items-center gap-2 text-[13.5px] font-medium">
              Autopay <Switch checked={autopay} onCheckedChange={setAutopay} aria-label="Autopay" />
            </label>
          }
        >
          Plans
        </SectionTitle>
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <span className="text-[14px] font-medium">Lines</span>
          <Stepper value={lines} min={1} max={8} onChange={setLines} label="Lines" />
          <span className="text-[13px] text-muted-foreground">for {account ? `${account.persona.toLowerCase()} (${account.user_id})` : "you"}</span>
        </div>
        {live ? (
          <Loadable state={plans}>
            {(qs) => (
              <div className="grid gap-3 md:grid-cols-2">
                {qs.map((q) => (
                  <PlanCard key={q.plan_id} q={q} selected={planId === q.plan_id} current={account?.plan_id === q.plan_id} onSelect={() => setPlanId(q.plan_id)} />
                ))}
              </div>
            )}
          </Loadable>
        ) : (
          <div className="grid gap-3 md:grid-cols-2">
            {wireless.plans.map((p) => (
              <Panel key={p.plan_id} className="p-4">
                <div className="flex items-baseline justify-between gap-3">
                  <h3 className="font-heading text-[17px] font-semibold">{p.name}</h3>
                  <span className="text-[13px] text-muted-foreground">
                    from <Money cents={p.line_prices_cents[0]} />
                    /mo, 1 line
                  </span>
                </div>
                <p className="mt-1 text-[13.5px] text-muted-foreground">{p.summary}</p>
                <p className="mt-2 text-[13px]">
                  {p.data} data · {p.hotspot_gb === null ? "unlimited" : `${p.hotspot_gb} GB`} hotspot · up to {p.max_lines} lines
                </p>
              </Panel>
            ))}
            <Offline className="md:col-span-2">Prices for {lines} lines, autopay and your eligibility need the backend.</Offline>
          </div>
        )}
      </section>

      {/* ---------------------------------------------------------------- household */}
      <section>
        <SectionTitle>Is your household on the right plan?</SectionTitle>
        <div className="mb-3 flex flex-wrap gap-2" role="radiogroup" aria-label="Demo household">
          {wireless.accounts.map((a) => (
            <button
              key={a.user_id}
              role="radio"
              aria-checked={a.user_id === userId}
              onClick={() => selectAccount(a.user_id)}
              className={cn(
                "h-9 cursor-pointer rounded-full px-3.5 text-[13.5px] font-medium ring-1",
                a.user_id === userId ? "bg-grape-wash text-grape ring-2 ring-grape" : "bg-card ring-foreground/[0.08] hover:bg-muted",
              )}
            >
              {a.persona}
            </button>
          ))}
        </div>
        {account && (
          <p className="mb-3 text-[13.5px] text-muted-foreground">
            {account.lines.length} line{account.lines.length === 1 ? "" : "s"} on {account.carrier}
            {account.plan_id ? `, ${wireless.plans.find((p) => p.plan_id === account.plan_id)?.name ?? account.plan_id}` : ""}:{" "}
            {account.lines.map((l) => `${l.label} (${l.device_name}, ${l.usage_gb_month ?? 0} GB/mo)`).join(", ")}. Fictional account.
          </p>
        )}
        {live ? (
          <Loadable state={household}>
            {(h) => (
              <Panel className="p-4">
                <div className="flex items-start gap-3">
                  <span className={cn("grid size-9 shrink-0 place-items-center rounded-full", h.keep_current ? "bg-leaf-wash text-leaf-deep" : "bg-grape-wash text-grape")}>
                    <ShieldCheck className="size-5" aria-hidden />
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className="font-semibold">{h.keep_current ? "Keep your current plan" : "A better fit"}</p>
                    <p className="text-[14px] text-muted-foreground">{h.reason}</p>
                    {h.current_monthly_cents !== null && (
                      <p className="mt-1 text-[13px]">
                        Today: <Money cents={h.current_monthly_cents} />
                        /mo for service
                      </p>
                    )}
                  </div>
                  {h.recommended_plan_id && (
                    <Button size="sm" variant="outline" onClick={() => setPlanId(h.recommended_plan_id)}>
                      Use it
                    </Button>
                  )}
                </div>
                <ul className="mt-3 divide-y divide-border">
                  {h.options.slice(0, 4).map((o) => (
                    <li key={o.plan.plan_id} className="flex flex-wrap items-baseline gap-x-3 gap-y-1 py-2 text-[13.5px]">
                      <span className="font-medium">{o.plan.plan_name}</span>
                      <span className="tabular">
                        <Money cents={o.plan.monthly_cents} />
                        /mo
                      </span>
                      {h.current_monthly_cents !== null && (
                        <span className={cn("tabular", o.savings_monthly_cents > 0 ? "text-leaf-deep" : o.savings_monthly_cents < 0 ? "text-tomato" : "text-muted-foreground")}>
                          {o.savings_monthly_cents > 0 ? "saves " : o.savings_monthly_cents < 0 ? "costs " : ""}
                          {o.savings_monthly_cents !== 0 && <Money cents={Math.abs(o.savings_monthly_cents)} />}
                          {o.savings_monthly_cents === 0 ? "same price" : "/mo"}
                        </span>
                      )}
                      {!o.fits_usage && <Badge variant="tomato">Doesn&apos;t fit usage</Badge>}
                      {o.tradeoffs.length > 0 && <span className="w-full text-[12.5px] text-muted-foreground">{o.tradeoffs.join(" ")}</span>}
                    </li>
                  ))}
                </ul>
                <Assumptions items={h.assumptions} />
              </Panel>
            )}
          </Loadable>
        ) : (
          <Offline>The household comparison runs on the backend.</Offline>
        )}
      </section>

      <div className="grid gap-8 lg:grid-cols-2">
        {/* ---------------------------------------------------------------- phone + financing */}
        <section>
          <SectionTitle>Phone and financing</SectionTitle>
          <Panel className="p-4">
            <label className="block text-[13.5px] font-medium" htmlFor="phone">
              Phone
            </label>
            <select
              id="phone"
              value={phoneSku}
              onChange={(e) => {
                setPhoneSku(e.target.value);
                setAccessories([]);
              }}
              className="mt-1 h-10 w-full rounded-lg bg-background px-3 text-[15px] ring-1 ring-input"
            >
              {phones.map((p) => (
                <option key={p.sku} value={p.sku}>
                  {p.name} ({fmt(p.price_cents)})
                </option>
              ))}
            </select>
            <div className="mt-3 flex flex-wrap items-center gap-4 text-[13.5px]">
              <label className="flex items-center gap-2 font-medium">
                Monthly installments <Switch checked={financed} onCheckedChange={setFinanced} aria-label="Finance the phone" />
              </label>
              {financed && (
                <span className="flex gap-1" role="radiogroup" aria-label="Term">
                  {wireless.financing.months_options.map((m) => (
                    <button
                      key={m}
                      role="radio"
                      aria-checked={months === m}
                      onClick={() => setMonths(m)}
                      className={cn("h-8 cursor-pointer rounded-full px-3 ring-1", months === m ? "bg-grape-wash text-grape ring-grape" : "ring-foreground/[0.08]")}
                    >
                      {m} mo
                    </button>
                  ))}
                </span>
              )}
            </div>
            <div className="mt-3 flex flex-wrap items-center gap-4 text-[13.5px]">
              <span className="flex items-center gap-2 font-medium">
                New lines <Stepper value={newLines} min={0} max={lines} onChange={setNewLines} label="New lines" />
              </span>
              <label className="flex items-center gap-2 font-medium">
                Switching carriers <Switch checked={portIn} onCheckedChange={setPortIn} aria-label="Port in a number" />
              </label>
            </div>
            {financed &&
              (live ? (
                <Loadable state={finance}>
                  {(f) => (
                    <div className="mt-4 rounded-xl bg-muted/60 p-3">
                      <div className="flex items-baseline justify-between gap-2">
                        <span className="text-[13.5px] text-muted-foreground">Installment</span>
                        <span className="text-[20px] font-semibold tabular">
                          <Money cents={f.net_monthly_cents} />
                          /mo
                        </span>
                      </div>
                      <p className="text-[12.5px] text-muted-foreground">
                        <Money cents={f.monthly_cents} /> × {f.months}
                        {f.promo_credit_monthly_cents > 0 && (
                          <>
                            {" "}
                            minus <Money cents={f.promo_credit_monthly_cents} />
                            /mo promo credit
                          </>
                        )}
                        . Down <Money cents={f.down_payment_cents} />, total <Money cents={f.total_cost_cents} />.
                      </p>
                      <Assumptions items={f.assumptions} />
                    </div>
                  )}
                </Loadable>
              ) : (
                <Offline className="mt-4">Installments are estimated by the backend.</Offline>
              ))}
          </Panel>
        </section>

        {/* ---------------------------------------------------------------- trade-in */}
        <section>
          <SectionTitle>Trade-in</SectionTitle>
          <Panel className="p-4">
            <label className="block text-[13.5px] font-medium" htmlFor="trade">
              Your current phone
            </label>
            <select id="trade" value={tradeModel} onChange={(e) => setTradeModel(e.target.value)} className="mt-1 h-10 w-full rounded-lg bg-background px-3 text-[15px] ring-1 ring-input">
              <option value="">No trade-in</option>
              {wireless.trade_in_models.map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.name} ({m.release_year})
                </option>
              ))}
            </select>
            {tradeModel && (
              <div className="mt-3 flex flex-wrap gap-1" role="radiogroup" aria-label="Condition">
                {CONDITIONS.map((c) => (
                  <button
                    key={c}
                    role="radio"
                    aria-checked={condition === c}
                    onClick={() => setCondition(c)}
                    className={cn("h-8 cursor-pointer rounded-full px-3 text-[13px] capitalize ring-1", condition === c ? "bg-grape-wash text-grape ring-grape" : "ring-foreground/[0.08]")}
                  >
                    {c}
                  </button>
                ))}
              </div>
            )}
            {tradeModel &&
              (live ? (
                <Loadable state={trade}>
                  {(t) => (
                    <div className={cn("mt-4 rounded-xl p-3", t.keep_recommended ? "bg-amber-wash" : "bg-leaf-wash")}>
                      <p className="font-semibold">{t.keep_recommended ? "Picky says: keep it for now" : "Trading in makes sense"}</p>
                      <p className="text-[13.5px]">
                        Worth <Money cents={t.value_cents} />
                        {t.bonus_cents > 0 && (
                          <>
                            {" "}
                            + <Money cents={t.bonus_cents} /> promo
                          </>
                        )}
                        {t.remaining_installments_cents > 0 && (
                          <>
                            {" "}
                            − <Money cents={t.remaining_installments_cents} /> still owed
                          </>
                        )}{" "}
                        = <Money cents={t.net_cents} /> net
                      </p>
                      <ul className="mt-1 list-disc pl-5 text-[13px]">
                        {t.reasons.map((r) => (
                          <li key={r}>{r}</li>
                        ))}
                      </ul>
                      <Assumptions items={t.assumptions} />
                    </div>
                  )}
                </Loadable>
              ) : (
                <Offline className="mt-4">Trade-in estimates come from the backend.</Offline>
              ))}
          </Panel>
        </section>
      </div>

      {/* ---------------------------------------------------------------- accessories */}
      <section>
        <SectionTitle>Accessories that fit the {productBySku[phoneSku]?.name.replace(/^(Apple|Samsung|Google) /, "")}</SectionTitle>
        <p className="-mt-2 mb-3 text-[13.5px] text-muted-foreground">All optional. Skip anything you already have.</p>
        {live ? (
          <Loadable state={compat}>
            {(list) =>
              list.length === 0 ? (
                <Panel className="p-4 text-[14px] text-muted-foreground">Nothing in this store is made for this phone.</Panel>
              ) : (
                <Panel className="divide-y divide-border">
                  {list.map((a) => {
                    const p = productBySku[a.sku];
                    const on = accessories.includes(a.sku);
                    return (
                      <div key={a.sku} className="flex items-center gap-3 p-3">
                        {p && <ProductTile color={p.label_color} category={p.category} name={p.name} size={40} />}
                        <div className="min-w-0 flex-1">
                          <div className="font-medium">{a.name}</div>
                          <div className="text-[12.5px] text-muted-foreground">
                            {a.why} {a.in_stock > 0 ? `${a.in_stock} in store.` : "Out of stock here."}
                          </div>
                        </div>
                        <Money cents={a.price_cents} className="text-[14px] font-semibold" />
                        <Button size="sm" variant={on ? "dark" : "outline"} onClick={() => toggleAcc(a.sku)} disabled={!on && a.in_stock === 0} aria-pressed={on}>
                          {on ? "Added" : "Add"}
                        </Button>
                      </div>
                    );
                  })}
                </Panel>
              )
            }
          </Loadable>
        ) : (
          <Offline>Compatibility and stock come from the backend.</Offline>
        )}
      </section>

      {/* ---------------------------------------------------------------- quote */}
      <section>
        <SectionTitle
          action={
            <label className="flex items-center gap-2 text-[13.5px] font-medium">
              Device protection <Switch checked={protection} onCheckedChange={setProtection} aria-label="Add device protection" />
            </label>
          }
        >
          Your estimate
        </SectionTitle>
        {live ? (
          <Loadable state={quote}>{(q) => <Breakdown q={q} accessories={accessories} />}</Loadable>
        ) : (
          <Offline>The estimate (installments, credits, taxes and the monthly total) is calculated by the backend. Start the gateway to see it.</Offline>
        )}
      </section>
    </div>
  );
}

function PlanCard({ q, selected, current, onSelect }: { q: PlanQuote; selected: boolean; current: boolean; onSelect: () => void }) {
  return (
    <Panel className={cn("flex flex-col p-4", selected && "ring-2 ring-grape", !q.eligible && "opacity-60")}>
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="font-heading text-[17px] font-semibold">{q.plan_name}</h3>
        <span className="text-[20px] font-semibold tabular">
          <Money cents={q.monthly_cents} />
          <span className="text-[13px] font-normal text-muted-foreground">/mo</span>
        </span>
      </div>
      <p className="text-[12.5px] text-muted-foreground">
        <Money cents={q.per_line_cents} /> per line · {q.est_taxes_fees_cents ? <>+ ~{fmt(q.est_taxes_fees_cents)} taxes & fees</> : "taxes & fees included"}
      </p>
      {q.perks.length > 0 && <p className="mt-2 text-[13px]">{q.perks.join(" · ")}</p>}
      {!q.eligible && <p className="mt-2 text-[13px] text-tomato">{q.ineligible_reason}</p>}
      <div className="mt-auto flex items-center gap-2 pt-3">
        {current && <Badge variant="muted">Current plan</Badge>}
        <Button size="sm" variant={selected ? "dark" : "outline"} className="ml-auto" onClick={onSelect} disabled={!q.eligible} aria-pressed={selected}>
          {selected ? "Selected" : "Choose"}
        </Button>
      </div>
    </Panel>
  );
}

function Breakdown({ q, accessories }: { q: CostBreakdown; accessories: string[] }) {
  const upfront = q.lines.filter((l) => l.upfront_cents !== 0 || (l.kind === "trade_in" && !l.monthly_cents));
  const monthly = q.lines.filter((l) => l.monthly_cents !== 0);
  return (
    <div className="flex flex-col gap-4">
      {q.errors.length > 0 && (
        <Panel className="flex gap-2 bg-tomato-wash p-3 text-[13.5px] text-tomato">
          <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <ul>
            {q.errors.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </Panel>
      )}
      {q.warnings.length > 0 && (
        <Panel className="flex gap-2 bg-amber-wash p-3 text-[13.5px] text-amber-ink">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <ul>
            {q.warnings.map((e) => (
              <li key={e}>{e}</li>
            ))}
          </ul>
        </Panel>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        <Panel className="p-4">
          <h3 className="font-heading text-[16px] font-semibold">Today (one-time)</h3>
          <Rows rows={upfront.map((l) => ({ label: l.label, note: l.note, cents: l.upfront_cents }))} />
          <Total label="Due today" cents={q.upfront_cents} />
        </Panel>
        <Panel className="p-4">
          <h3 className="font-heading text-[16px] font-semibold">Every month</h3>
          <Rows rows={monthly.map((l) => ({ label: l.label, note: l.note, cents: l.monthly_cents }))} />
          <Total label="Monthly (month 1)" cents={q.monthly_cents} />
        </Panel>
      </div>
      <Panel className="flex flex-wrap items-baseline justify-between gap-3 p-4">
        <span className="text-[14px] text-muted-foreground">Estimated total over {q.term_months} months</span>
        <span className="text-[24px] font-bold tabular">
          <Money cents={q.total_over_term_cents} />
        </span>
      </Panel>
      {q.promos.length > 0 && (
        <Panel className="p-4">
          <h3 className="font-heading text-[16px] font-semibold">Promotions we checked</h3>
          <ul className="mt-2 flex flex-col gap-1.5 text-[13.5px]">
            {q.promos.map((p) => (
              <li key={p.promo.promo_id} className="flex flex-wrap gap-x-2">
                <Badge variant={p.eligible ? "leaf" : p.upcoming ? "amber" : "muted"}>{p.eligible ? "Applied" : p.upcoming ? "Coming" : "Not eligible"}</Badge>
                <span className="font-medium">{p.promo.name}</span>
                <span className="text-muted-foreground">{p.reasons.join(" ")}</span>
              </li>
            ))}
          </ul>
        </Panel>
      )}
      <Assumptions items={q.assumptions} open />
      {accessories.length > 0 && q.ok && (
        <Link
          href={`/order?store=store_tmobile&add=${encodeURIComponent(accessories[0])}`}
          className="inline-flex items-center gap-2 self-start rounded-full bg-grape px-4 py-2 text-[14px] font-semibold text-white hover:opacity-90"
        >
          <Bot className="size-4" aria-hidden /> Have the robot bring the {productBySku[accessories[0]]?.name ?? "accessory"} <ArrowRight className="size-4" aria-hidden />
        </Link>
      )}
      <Notice />
    </div>
  );
}

function Rows({ rows }: { rows: { label: string; note: string; cents: number }[] }) {
  if (!rows.length) return <p className="mt-2 text-[13.5px] text-muted-foreground">Nothing.</p>;
  return (
    <ul className="mt-2 flex flex-col gap-2">
      {rows.map((r, i) => (
        <li key={`${r.label}:${i}`} className="flex items-baseline justify-between gap-3 text-[13.5px]">
          <span className="min-w-0">
            <span>{r.label}</span>
            {r.note && <span className="block text-[12px] text-muted-foreground">{r.note}</span>}
          </span>
          <Money cents={r.cents} className={cn("shrink-0", r.cents < 0 && "text-leaf-deep")} />
        </li>
      ))}
    </ul>
  );
}

function Total({ label, cents }: { label: string; cents: number }) {
  return (
    <div className="mt-3 flex items-baseline justify-between border-t border-border pt-2">
      <span className="font-semibold">{label}</span>
      <Money cents={cents} className="text-[18px] font-bold" />
    </div>
  );
}

function Stepper({ value, min, max, onChange, label }: { value: number; min: number; max: number; onChange: (n: number) => void; label: string }) {
  return (
    <span className="inline-flex items-center gap-1 rounded-full bg-card p-1 ring-1 ring-foreground/[0.08]">
      <button className="grid size-7 cursor-pointer place-items-center rounded-full hover:bg-muted disabled:opacity-40" onClick={() => onChange(Math.max(min, value - 1))} disabled={value <= min} aria-label={`Fewer ${label.toLowerCase()}`}>
        <Minus className="size-3.5" />
      </button>
      <span className="w-6 text-center font-semibold tabular" aria-live="polite">
        {value}
      </span>
      <button className="grid size-7 cursor-pointer place-items-center rounded-full hover:bg-muted disabled:opacity-40" onClick={() => onChange(Math.min(max, value + 1))} disabled={value >= max} aria-label={`More ${label.toLowerCase()}`}>
        <Plus className="size-3.5" />
      </button>
    </span>
  );
}

function Loadable<T>({ state, children }: { state: { data: T | null; error: string | null; loading: boolean }; children: (d: T) => React.ReactNode }) {
  if (state.error)
    return (
      <Panel className="flex items-start gap-2 p-4 text-[14px] text-tomato">
        <CircleAlert className="mt-0.5 size-4 shrink-0" aria-hidden /> {state.error}
      </Panel>
    );
  if (!state.data) return <Panel className="h-24 animate-pulse bg-muted/60" aria-busy />;
  return <div className={cn(state.loading && "opacity-60 transition-opacity")}>{children(state.data)}</div>;
}

function Offline({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <Panel className={cn("flex items-start gap-2 p-4 text-[14px] text-muted-foreground", className)}>
      <Smartphone className="mt-0.5 size-4 shrink-0" aria-hidden />
      <span>
        {children} <span className="font-medium text-foreground">Estimates need the backend</span> (set NEXT_PUBLIC_GATEWAY_URL and start the gateway).
      </span>
    </Panel>
  );
}

function Assumptions({ items, open }: { items: string[]; open?: boolean }) {
  if (!items.length) return null;
  return (
    <details className="mt-2 text-[12.5px] text-muted-foreground" open={open}>
      <summary className="cursor-pointer font-medium">Assumptions</summary>
      <ul className="mt-1 list-disc pl-5">
        {items.map((a) => (
          <li key={a}>{a}</li>
        ))}
      </ul>
    </details>
  );
}

function Notice({ className }: { className?: string }) {
  return (
    <p className={cn("flex items-start gap-2 text-[12.5px] text-muted-foreground", className)}>
      <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
      Synthetic demo pricing: not a real T-Mobile offer, approved financing or trade-in value. People and accounts are fictional. No payment is taken here.
    </p>
  );
}
