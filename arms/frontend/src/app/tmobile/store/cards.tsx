"use client";
// Cards Magenta shows under a reply: the agent's skills rendered from the same deterministic engines it answered from
// (lib/directions.ts via lib/customers.ts). Compact, for a conversation; the full breakdowns live under "How it works".
import { CalendarClock, Check, MessagesSquare, MinusCircle, ShoppingBag, Users } from "lucide-react";
import Link from "next/link";
import { toast } from "sonner";
import { ProductTile } from "@/components/picky";
import { Button } from "@/components/ui/button";
import type { AgentCard } from "@/lib/associate";
import { customerOf, householdResult } from "@/lib/customers";
import { productBySku } from "@/lib/data";
import { DATA, match, money, money0, upgrade, type Advice } from "@/lib/directions";
import { useTmoCart } from "@/lib/tmoCart";
import { cn } from "@/lib/utils";

export function AgentCards({ cards }: { cards: AgentCard[] }) {
  if (!cards.length) return null;
  return (
    <div className="flex w-full max-w-[680px] flex-col gap-2">
      {cards.map((c, i) => (
        <div key={i}>
          {c.kind === "bundles" && <Bundles answers={c.answers} />}
          {c.kind === "upgrade" && <Upgrade a={upgrade(DATA.upgrade_cases.find((u) => u.id === c.case_id)!)} />}
          {c.kind === "household" && <HouseholdCard customerId={c.customer_id} />}
        </div>
      ))}
    </div>
  );
}

function Shell({ icon: Icon, skill, children, href }: { icon: typeof Users; skill: string; children: React.ReactNode; href: string }) {
  return (
    <div className="rounded-2xl bg-card p-4 ring-1 ring-foreground/[0.08]">
      <div className="mb-2 flex items-center gap-1.5 text-[11.5px] font-semibold tracking-wide text-magenta uppercase">
        <Icon className="size-3.5" aria-hidden /> {skill}
        <Link href={href} className="ml-auto font-medium tracking-normal text-muted-foreground normal-case hover:text-foreground">
          Full breakdown →
        </Link>
      </div>
      {children}
    </div>
  );
}

function Bundles({ answers }: { answers: Parameters<typeof match>[0] }) {
  const { bundles, notes } = match(answers);
  const add = useTmoCart((s) => s.add);
  return (
    <Shell icon={MessagesSquare} skill="Matchmaker · your options" href="/tmobile/match">
      {notes.length > 0 && <p className="mb-2 text-[12.5px] text-muted-foreground">{notes.join(" ")}</p>}
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
        {bundles.map((b, i) => {
          const p = b.phone_sku ? productBySku[b.phone_sku] : null;
          return (
            <div key={b.key} className={cn("flex flex-col gap-1.5 rounded-xl p-3 ring-1", i === 0 ? "bg-magenta-wash/50 ring-magenta/40" : "ring-foreground/10")}>
              <div className="flex items-center gap-2">
                {p ? <ProductTile color={p.label_color} category={p.category} name={p.name} size={30} /> : <span className="grid size-[30px] place-items-center rounded-lg bg-leaf-wash text-leaf-deep">✓</span>}
                <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-semibold", i === 0 ? "bg-magenta text-white" : "bg-muted text-muted-foreground")}>{b.badge}</span>
              </div>
              <div className="text-[14px] leading-tight font-semibold">{b.title}</div>
              <div className="text-[20px] leading-none font-bold tabular">
                {money(b.monthly_cents)}
                <span className="text-[12px] font-normal text-muted-foreground">/mo</span>
                {!b.fits_budget && <span className="ml-1 text-[11.5px] font-semibold text-tomato">over budget</span>}
              </div>
              <div className="text-[11.5px] text-muted-foreground">
                plan {money(b.plan_cents)} + phone {money(b.device_cents)}
                {b.trade_credit_cents ? ` − trade-in ${money(b.trade_credit_cents)}` : ""}
              </div>
              <ul className="space-y-0.5 text-[12.5px]">
                {b.why.slice(0, 2).map((w) => (
                  <li key={w} className="flex gap-1.5">
                    <Check className="mt-0.5 size-3.5 shrink-0 text-leaf" aria-hidden />
                    {w}
                  </li>
                ))}
                {b.tradeoffs.slice(0, 1).map((w) => (
                  <li key={w} className="flex gap-1.5 text-muted-foreground">
                    <MinusCircle className="mt-0.5 size-3.5 shrink-0 text-amber" aria-hidden />
                    {w}
                  </li>
                ))}
              </ul>
              {b.phone_sku && (
                <Button size="sm" variant="outline" className="mt-auto" onClick={() => (add(b.phone_sku!, 1, "store"), toast(`${p!.name} added to your basket`))}>
                  <ShoppingBag className="size-3.5" aria-hidden /> Add phone
                </Button>
              )}
            </div>
          );
        })}
      </div>
    </Shell>
  );
}

const TONE = {
  wait: { cls: "bg-amber-wash text-amber-ink", label: "Not yet" },
  keep: { cls: "bg-leaf-wash text-leaf-deep", label: "Keep it" },
  upgrade_now: { cls: "bg-magenta-wash text-magenta-deep", label: "Upgrade now" },
} as const;

function Upgrade({ a }: { a: Advice }) {
  const max = Math.max(...a.curve.map((p) => p.v), 100);
  const pts = a.curve.map((p) => `${(p.m / 12) * 100},${30 - (p.v / max) * 26}`).join(" ");
  return (
    <Shell icon={CalendarClock} skill={`Timekeeper · ${a.c.name}`} href="/tmobile/upgrade">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1">
          <span className={cn("rounded-full px-2 py-0.5 text-[11.5px] font-semibold", TONE[a.verdict].cls)}>{TONE[a.verdict].label}</span>
          <div className="mt-1 text-[15px] leading-snug font-semibold">{a.headline}</div>
          <div className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-[12px] text-muted-foreground">
            {a.health.slice(0, 4).map((h) => (
              <span key={h.label} className={h.ok ? "" : "font-semibold text-tomato"}>
                {h.label}: {h.value}
              </span>
            ))}
          </div>
        </div>
        <div className="grid shrink-0 grid-cols-2 gap-2 text-center text-[12px]">
          <div className={cn("rounded-lg px-2.5 py-1.5 ring-1 ring-foreground/10", a.later && a.saves_cents <= 0 && "bg-leaf-wash/60")}>
            <div className="text-muted-foreground">Today</div>
            <div className="text-[15px] font-bold tabular">{money0(a.now.cost_cents)}</div>
          </div>
          {a.later ? (
            <div className={cn("rounded-lg px-2.5 py-1.5 ring-1 ring-foreground/10", a.saves_cents > 0 && "bg-leaf-wash/60")}>
              <div className="text-muted-foreground">{a.later.when}</div>
              <div className="text-[15px] font-bold tabular">{money0(a.later.cost_cents)}</div>
            </div>
          ) : (
            <div className="rounded-lg bg-leaf-wash/60 px-2.5 py-1.5 ring-1 ring-foreground/10">
              <div className="text-muted-foreground">Keep it</div>
              <div className="text-[15px] font-bold">$0</div>
            </div>
          )}
        </div>
      </div>
      <div className="mt-2 flex items-center gap-2 text-[11.5px] text-muted-foreground">
        <svg viewBox="0 0 100 32" className="h-8 w-40" preserveAspectRatio="none" aria-hidden>
          <polyline points={pts} fill="none" stroke="var(--color-magenta)" strokeWidth={2} vectorEffect="non-scaling-stroke" />
        </svg>
        trade-in {money(a.curve[0].v)} today → {money(a.curve[12].v)} in a year
      </div>
    </Shell>
  );
}

function HouseholdCard({ customerId }: { customerId: string }) {
  const c = customerOf(customerId);
  const r = c ? householdResult(c) : null;
  if (!c || !r) return null;
  const saves = r.saves_cents > 0;
  return (
    <Shell icon={Users} skill={`Household Mix · ${r.lines.length} line${r.lines.length > 1 ? "s" : ""}`} href="/tmobile/household">
      <div className="grid grid-cols-[1fr_auto_1fr] items-center gap-2">
        <div className="rounded-lg bg-muted/60 p-2.5">
          <div className="text-[11.5px] text-muted-foreground">Today · {r.current_label}</div>
          <div className="text-[18px] font-bold tabular">{money(r.current_total_cents)}</div>
        </div>
        <span className="text-muted-foreground" aria-hidden>
          →
        </span>
        <div className={cn("rounded-lg p-2.5", saves ? "bg-leaf-wash" : "bg-amber-wash")}>
          <div className="text-[11.5px] text-muted-foreground">{r.best.summary}</div>
          <div className="text-[18px] font-bold tabular">{money(r.best.monthly_cents)}</div>
        </div>
      </div>
      <div className={cn("mt-2 text-[13px] font-semibold", saves ? "text-leaf-deep" : r.saves_cents < 0 ? "text-amber-ink" : "text-muted-foreground")}>
        {saves ? `Saves ${money(r.saves_cents)}/mo · ${money0(r.saves_cents * 12)} a year` : r.saves_cents < 0 ? `${money(-r.saves_cents)}/mo more, and it actually fits` : "Already the best setup"}
        <span className="ml-2 font-normal text-muted-foreground">· {r.combos_checked} combinations checked</span>
      </div>
      <ul className="mt-1.5 grid gap-x-4 text-[12px] text-muted-foreground sm:grid-cols-2">
        {r.lines.map((l) => (
          <li key={l.line_id}>
            {l.label}: {l.usage_gb_month} GB{l.hotspot_gb_month ? ` + ${l.hotspot_gb_month} GB hotspot` : ""}
          </li>
        ))}
      </ul>
    </Shell>
  );
}
