"use client";
// The Matchmaker: a short chat that turns "I don't know what I need" into 2-3 device + plan bundles with plain-
// language reasons and trade-offs. Deterministic (lib/directions.ts match()); presets replay real-sounding shoppers.
import { Check, MinusCircle, RotateCcw, ShoppingBag, Sparkles } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Panel, ProductTile } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { productBySku } from "@/lib/data";
import { DATA, match, money, tradeModel, tradeValue, type Answers, type Condition, type Preset, type Priority, type Usage } from "@/lib/directions";
import { useTmoCart } from "@/lib/tmoCart";
import { cn } from "@/lib/utils";
import { AdvisorHeader } from "../advisors/nav";
import { Face } from "../store/face";

type Draft = Partial<Answers> & { phone?: "keep" | "trade" | "none"; extras?: string[]; done?: boolean };
type Opt = { label: string; apply: (d: Draft) => Draft };
type Step = { id: string; ask: string; multi?: boolean; options: Opt[]; answered: (d: Draft) => string | null };

const PRIORITY: { id: Priority; label: string }[] = [
  { id: "price", label: "Low price" },
  { id: "camera", label: "Great camera" },
  { id: "battery", label: "All-day battery" },
  { id: "performance", label: "Fast" },
  { id: "size", label: "Small, one-handed" },
];

function steps(d: Draft): Step[] {
  const s: Step[] = [
    {
      id: "lines",
      ask: "Hi! I'll help you find the right phone and plan. Who's it for?",
      options: [1, 2, 3, 4].map((n) => ({ label: n === 1 ? "Just me" : n === 2 ? "Me + 1" : `${n} of us`, apply: (x) => ({ ...x, lines: n }) })),
      answered: (x) => (x.lines ? (x.lines === 1 ? "Just me" : x.lines === 2 ? "Me + 1" : `${x.lines} of us`) : null),
    },
    {
      id: "usage",
      ask: "How do you use your phone most days?",
      options: (
        [
          ["light", "Texts, calls, maps"],
          ["medium", "Social + some streaming"],
          ["heavy", "Lots of streaming and hotspot"],
        ] as [Usage, string][]
      ).map(([u, l]) => ({ label: l, apply: (x) => ({ ...x, usage: u }) })),
      answered: (x) => (x.usage ? { light: "Texts, calls, maps", medium: "Social + some streaming", heavy: "Lots of streaming and hotspot" }[x.usage] : null),
    },
    {
      id: "priorities",
      ask: "What matters most in the phone? Pick up to two.",
      multi: true,
      options: PRIORITY.map((p) => ({ label: p.label, apply: (x) => ({ ...x, priorities: (x.priorities ?? []).includes(p.id) ? x.priorities!.filter((q) => q !== p.id) : [...(x.priorities ?? []), p.id].slice(-2) }) })),
      answered: (x) => (x.priorities?.length && x.done !== false ? x.priorities.map((p) => PRIORITY.find((q) => q.id === p)!.label).join(" + ") : null),
    },
    {
      id: "phone",
      ask: "And the phone you have now?",
      options: [
        { label: "It works fine, keep it", apply: (x) => ({ ...x, phone: "keep", keep_phone: true, trade_in: undefined }) },
        { label: "Trade it in", apply: (x) => ({ ...x, phone: "trade", keep_phone: false }) },
        { label: "I need a new one, no trade-in", apply: (x) => ({ ...x, phone: "none", keep_phone: false, trade_in: undefined }) },
      ],
      answered: (x) => (x.phone ? { keep: "It works fine, keep it", trade: "Trade it in", none: "I need a new one, no trade-in" }[x.phone] : null),
    },
  ];
  if (d.phone === "trade")
    s.push(
      {
        id: "model",
        ask: "Which phone is it?",
        options: DATA.trade_in_models.map((m) => ({ label: m.name, apply: (x) => ({ ...x, trade_in: { model_id: m.model_id, condition: x.trade_in?.condition ?? "good" } }) })),
        answered: (x) => (x.trade_in?.model_id ? tradeModel(x.trade_in.model_id)!.name : null),
      },
      {
        id: "condition",
        ask: "What shape is it in?",
        options: (
          [
            ["excellent", "Like new"],
            ["good", "Light wear"],
            ["fair", "Scratched but works"],
            ["broken", "Cracked / won't turn on"],
          ] as [Condition, string][]
        ).map(([c, l]) => ({ label: l, apply: (x) => ({ ...x, trade_in: { model_id: x.trade_in!.model_id, condition: c }, _cond: true }) as Draft })),
        answered: (x) => ((x as Draft & { _cond?: boolean })._cond && x.trade_in ? { excellent: "Like new", good: "Light wear", fair: "Scratched but works", broken: "Cracked / won't turn on" }[x.trade_in.condition] : null),
      },
    );
  s.push(
    {
      id: "extras",
      ask: "Anything else I should know?",
      multi: true,
      options: [
        { label: "Switching from another carrier", apply: (x) => ({ ...x, switching: !x.switching }) },
        { label: "I'm 55 or older", apply: (x) => ({ ...x, age_55: !x.age_55 }) },
      ],
      answered: (x) => (x.extras ? [x.switching && "Switching carriers", x.age_55 && "55+"].filter(Boolean).join(", ") || "Nope" : null),
    },
    {
      id: "budget",
      ask: "Last one: what's your monthly budget for everything (plan + phone)?",
      options: [5000, 7500, 10000, 15000, 20000].map((c) => ({ label: `${money(c).replace(".00", "")}/mo`, apply: (x) => ({ ...x, budget_monthly_cents: c }) })),
      answered: (x) => (x.budget_monthly_cents ? `${money(x.budget_monthly_cents).replace(".00", "")}/mo` : null),
    },
  );
  return s;
}

const fromPreset = (p: Preset): Draft => ({ ...p, phone: p.keep_phone ? "keep" : p.trade_in ? "trade" : "none", extras: [], _cond: true } as Draft);

export default function Matchmaker() {
  const [d, setD] = useState<Draft>({});
  const [preset, setPreset] = useState<Preset | null>(null);
  const [pending, setPending] = useState<Draft | null>(null); // multi-select in progress
  const all = steps(d);
  const current = all.find((s) => s.answered(d) === null) ?? null;
  const complete = !current;
  const result = useMemo(() => (complete ? match({ lines: d.lines!, budget_monthly_cents: d.budget_monthly_cents!, priorities: d.priorities ?? ["price"], usage: d.usage!, keep_phone: !!d.keep_phone, keep_second_phone: d.keep_second_phone, switching: d.switching, age_55: d.age_55, trade_in: d.trade_in }) : null), [complete, d]);
  const add = useTmoCart((s) => s.add);

  const pick = (o: Opt) => {
    if (!current) return;
    if (current.multi) setPending(o.apply(pending ?? { ...d, priorities: current.id === "priorities" ? [] : d.priorities }));
    else setD(o.apply(d));
  };
  const confirmMulti = () => {
    if (!current) return;
    const base = pending ?? d;
    setD({ ...base, ...(current.id === "extras" ? { extras: [] } : {}), ...(current.id === "priorities" && !base.priorities?.length ? { priorities: ["price"] } : {}) });
    setPending(null);
  };
  const reset = () => (setD({}), setPreset(null), setPending(null));
  const live = pending ?? d;

  return (
    <div className="flex flex-col gap-6 pb-16">
      <AdvisorHeader kicker="The Matchmaker" title="Chat your way to the right phone" sub="A few questions in, 2–3 phone + plan bundles out, each with the reasons and what you give up. It will tell you to keep your phone when that's the better deal." />

      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[13px] font-medium text-muted-foreground">Try a shopper:</span>
        {DATA.matchmaker_presets.map((p) => (
          <button key={p.id} onClick={() => (setD(fromPreset(p)), setPreset(p), setPending(null))} className={cn("cursor-pointer rounded-full px-3 py-1.5 text-[13px] font-medium ring-1", preset?.id === p.id ? "bg-magenta-wash text-magenta ring-magenta" : "ring-foreground/10 hover:bg-muted")}>
            {p.label}
          </button>
        ))}
        <Button variant="ghost" size="sm" onClick={reset}>
          <RotateCcw className="size-4" aria-hidden /> Start over
        </Button>
      </div>

      <div className="grid gap-5 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
        <Panel className="flex flex-col gap-3 p-4">
          <div className="flex items-center gap-3 border-b border-border pb-3">
            <Face mode={complete ? "talking" : "idle"} size={64} />
            <div>
              <div className="text-[16px] font-semibold">Magenta</div>
              <div className="text-[12.5px] text-muted-foreground">Asks, then shows the math</div>
            </div>
          </div>
          <div role="log" className="flex flex-col gap-2.5" aria-live="polite">
            {preset && <Bubble you>{preset.says}</Bubble>}
            {all.map((s) => {
              const a = s.answered(d);
              if (a === null && s !== current) return null;
              return (
                <div key={s.id} className="flex flex-col gap-2.5">
                  {!preset && <Bubble>{s.ask}</Bubble>}
                  {a !== null ? !preset && <Bubble you>{a}</Bubble> : null}
                  {a === null && s === current && (
                    <div className="flex flex-wrap gap-1.5 pl-1">
                      {s.options.map((o) => {
                        const on = s.multi && isOn(s.id, o.label, live);
                        return (
                          <button key={o.label} onClick={() => pick(o)} className={cn("cursor-pointer rounded-full px-3 py-1.5 text-[13.5px] font-medium ring-1", on ? "bg-magenta text-white ring-magenta" : "bg-card ring-foreground/15 hover:bg-muted")}>
                            {on && <Check className="mr-1 inline size-3.5" aria-hidden />}
                            {o.label}
                          </button>
                        );
                      })}
                      {s.multi && (
                        <Button size="sm" className="rounded-full bg-foreground text-background" onClick={confirmMulti}>
                          {s.id === "extras" && !live.switching && !live.age_55 ? "Nope, that's it" : "Done"}
                        </Button>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
            {preset && (
              <Bubble>
                Got it: {d.lines === 1 ? "one line" : `${d.lines} lines`}, {d.usage} use, you care about {(d.priorities ?? []).join(" and ")}, {d.keep_phone ? "keeping your phone" : d.trade_in ? `trading in a ${tradeModel(d.trade_in.model_id)?.name} (worth about ${money(tradeValue(d.trade_in.model_id, d.trade_in.condition))})` : "no trade-in"}, about {money(d.budget_monthly_cents ?? 0)}/mo. Here&apos;s what I&apos;d do.
              </Bubble>
            )}
          </div>
        </Panel>

        <div className="flex flex-col gap-3">
          {!result ? (
            <Panel className="flex min-h-[300px] flex-col items-center justify-center gap-2 p-8 text-center text-muted-foreground">
              <Sparkles className="size-8 text-magenta" aria-hidden />
              <p className="text-[15px]">Answer a few questions, or tap a shopper above, and your bundles show up here.</p>
            </Panel>
          ) : (
            <>
              {result.notes.map((n) => (
                <p key={n} className="rounded-xl bg-sky-wash px-3 py-2 text-[13.5px] text-sky">
                  {n}
                </p>
              ))}
              <AnimatePresence mode="popLayout">
                {result.bundles.map((b, i) => {
                  const p = b.phone_sku ? productBySku[b.phone_sku] : null;
                  return (
                    <motion.div key={b.key + b.title} initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: i * 0.08 }}>
                      <Panel className={cn("p-5", i === 0 && "ring-2 ring-magenta")}>
                        <div className="flex flex-wrap items-start gap-3">
                          {p ? <ProductTile color={p.label_color} category={p.category} name={p.name} size={48} /> : <span className="grid size-12 place-items-center rounded-xl bg-leaf-wash text-[20px]">✓</span>}
                          <div className="min-w-0 flex-1">
                            <span className={cn("rounded-full px-2.5 py-0.5 text-[12px] font-semibold", i === 0 ? "bg-magenta text-white" : "bg-muted text-muted-foreground")}>{b.badge}</span>
                            <h3 className="mt-1 text-[19px] leading-tight font-semibold">{b.title}</h3>
                            <div className="text-[13px] text-muted-foreground">
                              {b.plan.name} {b.plan.data === "Unlimited" ? "· unlimited data" : `· ${b.plan.data}`} · {b.plan.hotspot_gb} GB hotspot
                            </div>
                          </div>
                          <div className="text-right">
                            <div className="text-[26px] leading-none font-bold tabular">{money(b.monthly_cents)}</div>
                            <div className="text-[12px] text-muted-foreground">per month</div>
                            <div className={cn("mt-1 text-[12px] font-semibold", b.fits_budget ? "text-leaf-deep" : "text-tomato")}>{b.fits_budget ? "Fits your budget" : "Over budget"}</div>
                          </div>
                        </div>
                        <div className="mt-3 grid grid-cols-3 gap-2 rounded-xl bg-muted/60 p-2.5 text-center text-[12.5px]">
                          <div>
                            <div className="text-muted-foreground">Plan</div>
                            <div className="font-semibold tabular">{money(b.plan_cents)}</div>
                          </div>
                          <div>
                            <div className="text-muted-foreground">Phone (24 mo)</div>
                            <div className="font-semibold tabular">{money(b.device_cents)}</div>
                          </div>
                          <div>
                            <div className="text-muted-foreground">Trade-in credit</div>
                            <div className="font-semibold tabular">{b.trade_credit_cents ? `−${money(b.trade_credit_cents)}` : "–"}</div>
                          </div>
                        </div>
                        <div className="mt-3 grid gap-3 md:grid-cols-2">
                          <div>
                            <div className="text-[12.5px] font-semibold text-leaf-deep">Why this fits you</div>
                            <ul className="mt-1 space-y-1 text-[13.5px]">
                              {b.why.map((w) => (
                                <li key={w} className="flex gap-2">
                                  <Check className="mt-0.5 size-4 shrink-0 text-leaf" aria-hidden />
                                  {w}
                                </li>
                              ))}
                            </ul>
                          </div>
                          <div>
                            <div className="text-[12.5px] font-semibold text-amber-ink">What you give up</div>
                            <ul className="mt-1 space-y-1 text-[13.5px]">
                              {(b.tradeoffs.length ? b.tradeoffs : ["Nothing big for how you use your phone"]).map((w) => (
                                <li key={w} className="flex gap-2">
                                  <MinusCircle className="mt-0.5 size-4 shrink-0 text-amber" aria-hidden />
                                  {w}
                                </li>
                              ))}
                            </ul>
                          </div>
                        </div>
                        <div className="mt-3 flex flex-wrap items-center gap-2">
                          {b.phone_sku && (
                            <Button size="sm" className="bg-magenta text-white hover:bg-magenta-deep" onClick={() => (add(b.phone_sku!, 1, "web"), toast(`${p!.name} added to your basket`, { description: "Cart Whisperer will check it before you pay." }))}>
                              <ShoppingBag className="size-4" aria-hidden /> Add the phone to my basket
                            </Button>
                          )}
                          <Button size="sm" variant="outline" asChild>
                            <Link href="/tmobile/store">Ask Magenta in store</Link>
                          </Button>
                          {b.due_today_cents > 0 && <span className="text-[12px] text-muted-foreground">Due today: {money(b.due_today_cents)} connection fees</span>}
                        </div>
                      </Panel>
                    </motion.div>
                  );
                })}
              </AnimatePresence>
              <p className="text-[11.5px] text-muted-foreground">Monthly = plan with AutoPay (before taxes) + phone over 24 months at 0% − trade-in spread over 24 months. Plan prices, trade-in values and ratings are synthetic demo data; phone prices from t-mobile.com.</p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function isOn(step: string, label: string, d: Draft) {
  if (step === "priorities") return (d.priorities ?? []).includes(PRIORITY.find((p) => p.label === label)?.id as Priority);
  if (step === "extras") return label.startsWith("Switching") ? !!d.switching : !!d.age_55;
  return false;
}

function Bubble({ children, you = false }: { children: React.ReactNode; you?: boolean }) {
  return (
    <motion.div initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} className={cn("max-w-[88%] rounded-2xl px-3.5 py-2 text-[14.5px] leading-snug", you ? "self-end rounded-br-md bg-foreground text-background" : "self-start rounded-bl-md bg-magenta-wash")}>
      {children}
    </motion.div>
  );
}
