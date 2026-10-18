"use client";
import { Bot, ChevronRight, Plus, Check } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import { motion } from "motion/react";
import { HowItWorks, RobotArt } from "@/components/art";
import { LottieSlot } from "@/components/media";
import { CountUp, Reveal, itemVariants, listVariants } from "@/components/motion";
import { Meter, Money, Panel, ProductTile, SectionTitle, VerdictChip } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { useAsk } from "@/lib/assistant/store";
import { MONTHLY_BUDGET_CENTS, TODAY, fmtDate, mostBought, productBySku, runningLow, storeName } from "@/lib/data";
import { fmt } from "@/lib/money";
import { effective, offersFor, upcomingFor, verdictFor } from "@/lib/prices";
import { cartTotal, useTrip } from "@/lib/trip";
import { savedCents, usePurchases } from "@/lib/usePurchases";
import { cn } from "@/lib/utils";

export default function Home() {
  const { purchases, trips } = usePurchases();
  const month = trips.filter((t) => new Date(t.ts).getMonth() === TODAY.getMonth());
  const spent = month.reduce((s, t) => s + t.total_cents, 0);
  const pace = Math.round((MONTHLY_BUDGET_CENTS * TODAY.getDate()) / 31);
  const projected = Math.round((spent / TODAY.getDate()) * 31);
  const saved = savedCents(purchases.filter((p) => new Date(p.ts).getMonth() === TODAY.getMonth()));
  const low = runningLow(purchases).slice(0, 5);
  const staples = mostBought(purchases.filter((p) => TODAY.getTime() - Date.parse(p.ts) < 84 * 86_400_000), 14).map((m) => m.sku);
  const goodBuys = staples.filter((s) => ["Good price", "Wait"].includes(verdictFor(s).label) || offersFor(s).some((o) => o.note)).slice(0, 4);
  const coffee = upcomingFor("COFFEE");
  const ask = useAsk((s) => s.ask);

  return (
    <div className="flex flex-col gap-10">
      <LiveBanner />

      <section className="relative overflow-hidden rounded-[28px] bg-leaf text-white">
        <div className="pointer-events-none absolute -top-24 -right-24 size-[420px] rounded-full bg-lime/20 blur-3xl" aria-hidden />
        <div className="relative grid items-center gap-2 p-6 md:grid-cols-[1.25fr_1fr] md:p-10">
          <motion.div initial="hidden" animate="show" variants={listVariants}>
            <motion.p variants={itemVariants} className="text-[15px] font-medium text-white/75">
              Saturday, October 17
            </motion.p>
            <motion.h1 variants={itemVariants} className="mt-1 text-[40px] leading-[1.02] font-bold md:text-[56px]">
              Good afternoon, Maya.
            </motion.h1>
            <motion.p variants={itemVariants} className="mt-3 max-w-[46ch] text-[17px] text-white/85">
              You&apos;re <span className="rounded-md bg-lime px-1.5 font-semibold text-lime-ink">{fmt(pace - spent)} under</span> your grocery pace, and{" "}
              {low.length} things you buy regularly are probably running low.{coffee ? " Hold off on coffee: it's $2 off from Friday." : ""}
            </motion.p>
            <motion.div variants={itemVariants} className="mt-6 flex flex-wrap gap-2">
              <Button asChild size="lg" variant="lime">
                <Link href="/order">
                  <Bot /> Send the robot
                </Link>
              </Button>
              <Button size="lg" variant="outline" className="border-white/30 bg-white/10 text-white hover:bg-white/20 hover:text-white" onClick={() => ask("How can I save on groceries?")}>
                How can I save this month?
              </Button>
            </motion.div>
          </motion.div>
          <motion.div initial={{ opacity: 0, x: 40 }} animate={{ opacity: 1, x: 0 }} transition={{ type: "spring", stiffness: 120, damping: 18, delay: 0.2 }}>
            <LottieSlot slot="robotHero" className="mx-auto aspect-[8/7] w-full max-w-[380px]" fallback={<RobotArt />} />
          </motion.div>
        </div>
      </section>

      <section className="grid gap-6 lg:grid-cols-[380px_1fr]">
        <Reveal>
          <Panel className="h-full p-5">
            <div className="flex items-baseline justify-between">
              <span className="font-semibold">October groceries</span>
              <Link href="/purchases" className="text-[13.5px] font-semibold text-sky hover:underline">
                Details
              </Link>
            </div>
            <div className="mt-2 flex items-baseline gap-2">
              <CountUp cents={spent} className="font-heading text-[40px] leading-none font-bold" />
              <span className="text-muted-foreground">
                of <Money cents={MONTHLY_BUDGET_CENTS} />
              </span>
            </div>
            <Meter value={spent} max={MONTHLY_BUDGET_CENTS} pace={pace} tone="sky" className="mt-4" />
            <div className="mt-2 text-[12.5px] text-muted-foreground">The line marks where you&apos;d be on budget today</div>
            <dl className="mt-4 grid grid-cols-3 gap-2 text-center">
              {(
                [
                  ["Trips", null, month.length],
                  ["On track for", projected, null],
                  ["Saved on deals", saved, null],
                ] as const
              ).map(([k, cents, n]) => (
                <div key={k} className="rounded-xl bg-muted px-2 py-2.5">
                  <dd className="font-heading text-[18px] font-semibold">{cents !== null ? <CountUp cents={cents} /> : n}</dd>
                  <dt className="text-[12px] text-muted-foreground">{k}</dt>
                </div>
              ))}
            </dl>
          </Panel>
        </Reveal>
        <Reveal delay={0.08}>
          <Panel className="h-full p-6 md:p-8">
            <h2 className="mb-6 text-[20px] font-semibold">How Picky works</h2>
            <HowItWorks />
          </Panel>
        </Reveal>
      </section>

      <section>
        <SectionTitle
          action={
            <Button asChild variant="ghost" size="sm">
              <Link href="/order?preset=basics">
                Restock with the robot <ChevronRight />
              </Link>
            </Button>
          }
        >
          Probably running low
        </SectionTitle>
        <RunningLow items={low} />
      </section>

      <section className="grid gap-8 lg:grid-cols-2">
        <div>
          <SectionTitle
            action={
              <Button asChild variant="ghost" size="sm">
                <Link href="/find">
                  Compare anything <ChevronRight />
                </Link>
              </Button>
            }
          >
            Prices on things you buy
          </SectionTitle>
          <Panel className="p-2">
            {goodBuys.map((s) => {
              const p = productBySku[s];
              const best = offersFor(s).find((o) => o.in_stock) ?? offersFor(s)[0];
              const v = verdictFor(s);
              return (
                <Link key={s} href={`/find?sku=${s}`} className="flex items-center gap-3 rounded-xl p-3 transition-colors hover:bg-muted">
                  <ProductTile color={p.label_color} category={p.category} name={p.name} size={44} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-semibold">{p.name}</div>
                    <div className="truncate text-[13px] text-muted-foreground">{v.why}</div>
                  </div>
                  <div className="flex flex-col items-end gap-1">
                    <Money cents={effective(best)} className="font-heading text-[17px] font-semibold" />
                    <VerdictChip verdict={v} />
                  </div>
                </Link>
              );
            })}
          </Panel>
        </div>

        <div>
          <SectionTitle
            action={
              <Button asChild variant="ghost" size="sm">
                <Link href="/purchases">
                  All {trips.length} trips <ChevronRight />
                </Link>
              </Button>
            }
          >
            Recent trips
          </SectionTitle>
          <Panel className="p-2">
            {trips.slice(0, 4).map((t) => (
              <Link key={t.trip_id} href="/purchases" className="flex items-center gap-3 rounded-xl p-3 transition-colors hover:bg-muted">
                <div className="flex -space-x-2">
                  {t.lines.slice(0, 4).map((l) => {
                    const p = productBySku[l.sku];
                    return <ProductTile key={l.purchase_id} color={p.label_color} category={p.category} name={p.name} size={36} className="ring-2 ring-card" />;
                  })}
                </div>
                <div className="min-w-0 flex-1">
                  <div className="font-semibold">{fmtDate(t.ts, { weekday: "long", month: "short", day: "numeric" })}</div>
                  <div className="text-[13px] text-muted-foreground">
                    {t.items} items at {storeName(t.store_id)}
                  </div>
                </div>
                <Money cents={t.total_cents} className="font-heading text-[17px] font-semibold" />
              </Link>
            ))}
          </Panel>
        </div>
      </section>
    </div>
  );
}

function RunningLow({ items }: { items: ReturnType<typeof runningLow> }) {
  const [added, setAdded] = useState<Set<string>>(new Set());
  return (
    <motion.div
      className="-mx-4 flex snap-x gap-3 overflow-x-auto px-4 pb-2 md:mx-0 md:grid md:grid-cols-3 md:overflow-visible md:px-0 lg:grid-cols-5"
      variants={listVariants}
      initial="hidden"
      whileInView="show"
      viewport={{ once: true, margin: "-60px" }}
    >
      {items.map((r) => {
        const p = productBySku[r.sku];
        const on = added.has(r.sku);
        const best = offersFor(r.sku).find((o) => o.in_stock);
        return (
          <motion.div
            key={r.sku}
            variants={itemVariants}
            whileHover={{ y: -4 }}
            className="flex w-[160px] shrink-0 snap-start flex-col rounded-2xl bg-card p-3 ring-1 ring-foreground/[0.07] transition-shadow hover:shadow-md md:w-auto"
          >
            <div className="grid aspect-[4/3] place-items-center rounded-xl" style={{ background: `${p.label_color}33` }}>
              <motion.div whileHover={{ rotate: -6, scale: 1.08 }} transition={{ type: "spring", stiffness: 400, damping: 12 }}>
                <ProductTile color={p.label_color} category={p.category} name={p.name} size={56} />
              </motion.div>
            </div>
            <div className="mt-3 line-clamp-2 min-h-[2.6em] text-[14px] leading-tight font-semibold">{p.name}</div>
            <div className={cn("mt-1 text-[12.5px]", r.due_in_days <= 0 ? "font-semibold text-tomato" : "text-muted-foreground")}>
              {r.due_in_days <= 0 ? `Due, bought ${r.since_days} days ago` : `Due in ${r.due_in_days} days`}
            </div>
            <div className="mt-3 flex items-center justify-between">
              {best && <Money cents={effective(best)} className="font-semibold" />}
              <Button
                size="icon-sm"
                variant={on ? "default" : "outline"}
                className="size-9 rounded-full"
                aria-label={on ? `Remove ${p.name}` : `Add ${p.name} to the next robot order`}
                aria-pressed={on}
                onClick={() =>
                  setAdded((s) => {
                    const n = new Set(s);
                    if (n.has(r.sku)) n.delete(r.sku);
                    else n.add(r.sku);
                    return n;
                  })
                }
              >
                {on ? <Check className="animate-pop" /> : <Plus />}
              </Button>
            </div>
          </motion.div>
        );
      })}
    </motion.div>
  );
}

function LiveBanner() {
  const { phase, draft, cart, robot, activeStop, stops } = useTrip();
  if (phase === "idle" || !draft) return null;
  const label =
    phase === "waiting_login"
      ? "Waiting for you at the entrance"
      : phase === "done"
        ? "All done. Your receipt is ready."
        : robot.status === "waiting"
          ? "The robot has a question for you"
          : `Heading to ${activeStop ? stops[activeStop]?.label : "the next stop"}`;
  return (
    <Link href="/trip" className="flex items-center gap-4 rounded-2xl bg-foreground p-4 text-background transition-transform hover:scale-[1.005]">
      <span className="grid size-11 place-items-center rounded-xl bg-lime text-lime-ink">
        <Bot className="size-6" />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2 font-semibold">
          {phase !== "done" && <span className="live-dot size-2 rounded-full bg-lime" />}
          {draft.title}
        </div>
        <div className="truncate text-[14px] text-background/70">{label}</div>
      </div>
      <span className="tabular hidden text-[14px] text-background/80 sm:block">
        {cart.length} items, {fmt(cartTotal(cart))}
      </span>
      <ChevronRight className="size-5" />
    </Link>
  );
}
