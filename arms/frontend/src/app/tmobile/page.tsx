"use client";
import { ArrowRight, BatteryCharging, CalendarClock, Smartphone, Tag } from "lucide-react";
import Link from "next/link";
import { Money, Panel, ProductTile, SectionTitle } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { mayaDevices, tmobileProducts, tmobileStore, wireless } from "@/data/stores/tmobile";
import { productBySku } from "@/lib/data";
import { fmt } from "@/lib/money";
import { suggest } from "@/lib/whisperer";
import { niceDate } from "./bits";
import { Face } from "./store/face";

export default function TmoHome() {
  const ph = mayaDevices.phone;
  // what Cart Whisperer would say before she even builds a cart for the Pixel 9 she's been looking at
  const advice = suggest({ cart: [{ sku: "CASE-PX9-CLR", qty: 1 }], intent: "my new Pixel 9", budget_cents: 10000, phase: "planning" }).filter((s) => s.kind === "skip" || s.kind === "wait");
  const offers = wireless.line_promos.filter((p) => p.sku && productBySku[p.sku]).slice(0, 6);
  const phones = tmobileProducts.filter((p) => p.category === "phone").length;

  return (
    <div className="flex flex-col gap-8 pb-16">
      <section className="grid gap-6 md:grid-cols-[1.3fr_1fr] md:items-end">
        <div>
          <p className="text-[14px] font-semibold text-magenta">Hi Maya</p>
          <h1 className="mt-1 text-[38px] leading-[1.05] font-bold md:text-[50px]">The phone store assistant that will tell you not to buy.</h1>
          <p className="mt-3 max-w-[560px] text-[16px] text-muted-foreground">
            Cart Whisperer knows your phone, what you already own, your budget and what&apos;s on the shelf. It suggests at most three things, and some of them are &quot;skip it&quot; or &quot;wait&quot;.
          </p>
          <div className="mt-5 flex flex-wrap gap-2">
            <Button asChild size="lg" className="bg-magenta text-white hover:bg-magenta-deep">
              <Link href="/tmobile/shop">
                Start shopping <ArrowRight className="size-4" aria-hidden />
              </Link>
            </Button>
            <Button asChild size="lg" variant="outline">
              <Link href="/tmobile/track">How it decides</Link>
            </Button>
          </div>
        </div>
        <Panel className="p-5">
          <div className="flex items-center gap-3">
            <span className="grid size-11 place-items-center rounded-xl bg-magenta-wash text-magenta">
              <Smartphone className="size-6" aria-hidden />
            </span>
            <div>
              <div className="text-[13px] text-muted-foreground">Your phone</div>
              <div className="text-[20px] font-semibold">{ph.name}</div>
            </div>
          </div>
          <dl className="mt-4 grid grid-cols-2 gap-3 text-[14px]">
            <div>
              <dt className="text-muted-foreground">Age</dt>
              <dd className="font-semibold">{ph.age_months} months</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Payments left</dt>
              <dd className="font-semibold">
                {ph.installments_left} × {fmt(ph.monthly_installment_cents)} = {fmt(ph.payoff_cents)}
              </dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Lines</dt>
              <dd className="font-semibold">{mayaDevices.lines}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground">Tech trip cap</dt>
              <dd className="font-semibold">$150, asks above $50</dd>
            </div>
          </dl>
          <div className="mt-4 border-t border-border pt-3">
            <div className="text-[13px] text-muted-foreground">You already own</div>
            <ul className="mt-1 space-y-1 text-[14px]">
              {mayaDevices.owns.map((o) => (
                <li key={o.sku} className="flex items-center gap-2">
                  <BatteryCharging className="size-4 text-muted-foreground" aria-hidden />
                  {o.name} <span className="text-muted-foreground">· bought {niceDate(o.bought)}</span>
                </li>
              ))}
            </ul>
          </div>
        </Panel>
      </section>

      <Panel className="flex flex-col items-start gap-4 overflow-hidden bg-[#17141a] p-5 text-white sm:flex-row sm:items-center">
        <Face mode="idle" size={110} />
        <div className="min-w-0 flex-1">
          <div className="text-[13px] font-semibold text-magenta">One agent for everything</div>
          <div className="text-[20px] leading-tight font-semibold">Ask Magenta: phone, plan, upgrade, basket</div>
          <p className="mt-1 text-[14px] text-white/75">She picks the right phone, tells you honestly whether to upgrade, fixes the family plan, keeps your basket honest, and in the store walks you to things and brings your new phone over.</p>
        </div>
        <Button asChild size="lg" className="shrink-0 bg-magenta text-white hover:bg-magenta-deep">
          <Link href="/tmobile/store">
            Talk to Magenta <ArrowRight className="size-4" aria-hidden />
          </Link>
        </Button>
      </Panel>

      <section>
        <SectionTitle>Honest advice before you go</SectionTitle>
        <div className="grid gap-3 md:grid-cols-2">
          {advice.map((s) => (
            <Panel key={s.id} className={s.kind === "skip" ? "border-l-4 border-tomato p-4" : "border-l-4 border-amber p-4"}>
              <div className="flex items-center gap-2 text-[12.5px] font-semibold">
                {s.kind === "skip" ? <span className="text-tomato">Skip it</span> : <span className="text-amber-ink">Worth waiting</span>}
                <span className="text-muted-foreground">· {s.kind === "wait" ? "expected saving" : "avoids"} {fmt(s.avoided_cents)}</span>
              </div>
              <div className="mt-1 text-[16px] font-semibold">{s.title}</div>
              <p className="mt-1 text-[14px] text-muted-foreground">{s.reason}</p>
            </Panel>
          ))}
        </div>
      </section>

      <section>
        <SectionTitle action={<span className="text-[12.5px] text-muted-foreground">real t-mobile.com offers, Oct 17 · bill credits, not shelf prices</span>}>Line offers, with the fine print up front</SectionTitle>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {offers.map((o) => {
            const p = productBySku[o.sku!];
            return (
              <Panel key={o.headline} className="flex items-start gap-3 p-4">
                <ProductTile color={p.label_color} category={p.category} name={p.name} size={40} />
                <div className="min-w-0">
                  <div className="text-[14px] font-semibold">{o.headline}</div>
                  <div className="text-[12.5px] text-muted-foreground">
                    {p.name} · <Money cents={p.price_cents} /> full price
                  </div>
                  <div className="mt-1 inline-flex items-center gap-1 text-[12px] text-amber-ink">
                    <Tag className="size-3.5" aria-hidden />
                    {o.requires ? `Needs a ${o.requires.replace(/_/g, " ")}` : "New line, port-in or upgrade"} · paid as monthly credits
                  </div>
                </div>
              </Panel>
            );
          })}
        </div>
      </section>

      <section className="grid gap-3 md:grid-cols-3">
        <Panel className="p-4">
          <div className="text-[13px] text-muted-foreground">The store</div>
          <div className="text-[17px] font-semibold">{tmobileStore.name}</div>
          <p className="mt-1 text-[13px] text-muted-foreground">
            {tmobileProducts.length} products, {phones} phones, {tmobileStore.fixtures.length} fixtures. Phones on the tables are demo units; the robot picks boxed stock from behind the counter.
          </p>
        </Panel>
        <Panel className="p-4">
          <div className="text-[13px] text-muted-foreground">The robot</div>
          <div className="text-[17px] font-semibold">Magenta (rover + lift + SO-101 arm)</div>
          <p className="mt-1 text-[13px] text-muted-foreground">Runs in Isaac Sim with real physics: it drives around the tables, lifts each phone out of its holder and sets it down in the basket.</p>
        </Panel>
        <Panel className="p-4">
          <div className="flex items-center gap-2 text-[13px] text-muted-foreground">
            <CalendarClock className="size-4" aria-hidden /> Promo calendar
          </div>
          <ul className="mt-1 space-y-1 text-[13.5px]">
            {wireless.upcoming_promos.map((u) => (
              <li key={u.sku}>
                <span className="font-semibold">{productBySku[u.sku]?.name}</span>: {fmt(u.was_cents)} → {fmt(u.price_cents)} from {niceDate(u.starts)}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-[11.5px] text-muted-foreground">Synthetic, for the demo.</p>
        </Panel>
      </section>
    </div>
  );
}
