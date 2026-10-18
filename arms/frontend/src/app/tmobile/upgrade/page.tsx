"use client";
// The Timekeeper: "should I upgrade?" answered honestly, including "not yet" and "keep it". Inputs: phone health,
// what's left to pay, the promo calendar and a trade-in value curve fitted to the trade-in table.
import { BellRing, CheckCircle2, CircleAlert, ShoppingBag, ThumbsUp } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { Panel } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { productBySku } from "@/lib/data";
import { money, upgrades, type Advice } from "@/lib/directions";
import { useTmoCart } from "@/lib/tmoCart";
import { cn } from "@/lib/utils";
import { AdvisorHeader } from "../advisors/nav";

const TONE = {
  wait: { cls: "bg-amber-wash text-amber-ink ring-amber/40", label: "Not yet" },
  keep: { cls: "bg-leaf-wash text-leaf-deep ring-leaf/30", label: "Keep it" },
  upgrade_now: { cls: "bg-magenta-wash text-magenta-deep ring-magenta/40", label: "Upgrade now" },
} as const;

export default function Timekeeper() {
  const all = useMemo(() => upgrades(), []);
  const [id, setId] = useState(all[0].c.id);
  const a = all.find((x) => x.c.id === id)!;
  return (
    <div className="flex flex-col gap-6 pb-16">
      <AdvisorHeader
        kicker="The Timekeeper"
        title="Say “don’t upgrade yet” and mean it"
        sub="The honest answer to “should I upgrade now?”, from what you still owe, what the promo calendar says, how fast your trade-in value is dropping, and how your phone is actually holding up."
      />
      <div className="flex flex-wrap gap-2" role="tablist" aria-label="Customer">
        {all.map((x) => (
          <button key={x.c.id} role="tab" aria-selected={x.c.id === id} onClick={() => setId(x.c.id)} className={cn("flex cursor-pointer items-center gap-2 rounded-full px-3.5 py-2 text-[13.5px] font-semibold ring-1", x.c.id === id ? "bg-foreground text-background ring-foreground" : "bg-card ring-foreground/10 hover:bg-muted")}>
            {x.c.name}
            <span className={cn("rounded-full px-2 py-0.5 text-[11px] ring-1", TONE[x.verdict].cls)}>{TONE[x.verdict].label}</span>
          </button>
        ))}
      </div>
      <View key={a.c.id} a={a} />
    </div>
  );
}

function View({ a }: { a: Advice }) {
  const add = useTmoCart((s) => s.add);
  const target = productBySku[a.c.target_sku];
  const device = a.health.find((h) => h.label === "Trade-in today");
  return (
    <div className="flex flex-col gap-4">
      <Panel className={cn("p-5 ring-2", TONE[a.verdict].cls)}>
        <div className="text-[13px] font-semibold uppercase">{TONE[a.verdict].label}</div>
        <h2 className="mt-1 text-[24px] leading-tight font-bold md:text-[28px]">{a.headline}</h2>
        <ul className="mt-3 space-y-1.5 text-[14.5px]">
          {a.reasons.map((r) => (
            <li key={r} className="flex gap-2">
              <span className="mt-2 size-1.5 shrink-0 rounded-full bg-current" aria-hidden />
              {r}
            </li>
          ))}
        </ul>
        <div className="mt-4 flex flex-wrap gap-2">
          {a.verdict === "wait" && (
            <Button className="bg-foreground text-background" onClick={() => toast(`I'll remind ${a.c.name} on ${a.later!.when}`, { description: "With the payoff and the promo price checked again that day." })}>
              <BellRing className="size-4" aria-hidden /> Remind me {a.later!.when}
            </Button>
          )}
          {a.verdict === "upgrade_now" && target && (
            <Button className="bg-magenta text-white hover:bg-magenta-deep" onClick={() => (add(target.sku, 1, "web"), toast(`${target.name} added to your basket`, { description: "Bring the old phone in for the trade-in." }))}>
              <ShoppingBag className="size-4" aria-hidden /> Add the {target.name.replace(/\s+\d+\s*GB$/, "")} to my basket
            </Button>
          )}
          {a.verdict === "keep" && (
            <Button className="bg-foreground text-background" onClick={() => toast("I'll check again in 6 months", { description: "Or sooner if the battery or updates change." })}>
              <ThumbsUp className="size-4" aria-hidden /> Check again in 6 months
            </Button>
          )}
        </div>
      </Panel>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <Panel className="p-5">
          <h3 className="text-[16px] font-semibold">
            {a.c.name}&apos;s phone, checked
          </h3>
          <ul className="mt-2 divide-y divide-border">
            {a.health.map((h) => (
              <li key={h.label} className="flex items-center gap-2 py-2 text-[14px]">
                {h.ok ? <CheckCircle2 className="size-4 shrink-0 text-leaf" aria-hidden /> : <CircleAlert className="size-4 shrink-0 text-tomato" aria-hidden />}
                <span className="text-muted-foreground">{h.label}</span>
                <span className="ml-auto font-semibold tabular">{h.value}</span>
              </li>
            ))}
          </ul>
        </Panel>

        <Panel className="p-5">
          <h3 className="text-[16px] font-semibold">Trade-in value, next 12 months</h3>
          <p className="text-[12.5px] text-muted-foreground">
            Loses about {money(a.value_loss_per_month_cents)} a month · {device?.value}
          </p>
          <Curve a={a} />
        </Panel>
      </div>

      <Panel className="p-5">
        <h3 className="text-[16px] font-semibold">What it costs: {a.later ? "today vs. waiting" : "if you upgraded today"}</h3>
        <div className={cn("mt-3 grid gap-3", a.later && "md:grid-cols-2")}>
          {[a.now, ...(a.later ? [a.later] : [])].map((o, i) => {
            const better = a.later && ((i === 1 && a.saves_cents > 0) || (i === 0 && a.saves_cents <= 0));
            return (
              <div key={o.label} className={cn("rounded-xl p-4 ring-1", better ? "bg-leaf-wash/60 ring-leaf/40" : "ring-foreground/10")}>
                <div className="flex items-center justify-between">
                  <span className="text-[14.5px] font-semibold">{o.label}</span>
                  {better && <span className="rounded-full bg-leaf px-2 py-0.5 text-[11px] font-bold text-white">Better</span>}
                </div>
                <ul className="mt-2 space-y-1 text-[13.5px]">
                  {o.parts.map(([l, c]) => (
                    <li key={l} className="flex justify-between gap-3">
                      <span className="text-muted-foreground">{l}</span>
                      <span className="tabular">{c < 0 ? `−${money(-c)}` : money(c)}</span>
                    </li>
                  ))}
                </ul>
                <div className="mt-2 flex justify-between border-t border-border pt-2 text-[15px] font-bold">
                  <span>Out of pocket</span>
                  <span className="tabular">{money(o.cost_cents)}</span>
                </div>
              </div>
            );
          })}
        </div>
        {a.later && (
          <p className="mt-3 text-[13.5px]">
            {a.saves_cents > 0 ? (
              <>
                Waiting saves <b>{money(a.saves_cents)}</b>, even after the trade-in loses value.
              </>
            ) : (
              <>Waiting doesn&apos;t save anything here ({money(-a.saves_cents)} more), so there&apos;s no reason to hold out.</>
            )}
          </p>
        )}
        <p className="mt-2 text-[11.5px] text-muted-foreground">Before taxes. Phone payments you&apos;d make anyway while waiting aren&apos;t counted. Trade-in values, payoffs and the promo date are synthetic demo data; the decay curve is fitted to the trade-in table by brand.</p>
      </Panel>
    </div>
  );
}

function Curve({ a }: { a: Advice }) {
  const W = 520, H = 170, P = 34;
  const max = Math.max(...a.curve.map((p) => p.v), 100);
  const x = (m: number) => P + (m / 12) * (W - P - 10);
  const y = (v: number) => H - 24 - (v / max) * (H - 44);
  const d = a.curve.map((p, i) => `${i ? "L" : "M"}${x(p.m).toFixed(1)},${y(p.v).toFixed(1)}`).join(" ");
  const mark = a.later ? a.curve[Math.min(12, a.later.months)] : null;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="mt-2 h-auto w-full" role="img" aria-label={`Trade-in value from ${money(a.curve[0].v)} today to ${money(a.curve[12].v)} in 12 months`}>
      {[0, 0.5, 1].map((f) => (
        <g key={f}>
          <line x1={P} x2={W - 10} y1={y(max * f)} y2={y(max * f)} stroke="var(--border)" />
          <text x={P - 4} y={y(max * f) + 4} textAnchor="end" fontSize={10} fill="var(--muted-foreground)">
            ${Math.round((max * f) / 100)}
          </text>
        </g>
      ))}
      <path d={`${d} L${x(12)},${y(0)} L${x(0)},${y(0)} Z`} fill="var(--color-magenta)" opacity={0.08} />
      <path d={d} fill="none" stroke="var(--color-magenta)" strokeWidth={2.5} strokeLinejoin="round" />
      <circle cx={x(0)} cy={y(a.curve[0].v)} r={4.5} fill="var(--color-magenta)" />
      <text x={x(0) + 8} y={y(a.curve[0].v) + 18} fontSize={11} fontWeight={600}>
        today {money(a.curve[0].v)}
      </text>
      {mark && a.later && (
        <g>
          <line x1={x(mark.m)} x2={x(mark.m)} y1={14} y2={H - 24} stroke="var(--color-amber)" strokeDasharray="4 3" strokeWidth={1.5} />
          <circle cx={x(mark.m)} cy={y(mark.v)} r={4.5} fill="var(--color-amber)" />
          <text x={Math.min(x(mark.m) + 6, W - 120)} y={22} fontSize={11} fontWeight={600} fill="var(--color-amber-ink)">
            {a.later.when}: {money(mark.v)}
          </text>
        </g>
      )}
      {[0, 3, 6, 9, 12].map((m) => (
        <text key={m} x={x(m)} y={H - 6} textAnchor="middle" fontSize={10} fill="var(--muted-foreground)">
          {m === 0 ? "now" : `+${m} mo`}
        </text>
      ))}
    </svg>
  );
}
