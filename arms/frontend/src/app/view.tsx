"use client";
import Link from "next/link";
import { SUGGESTIONS } from "@/components/AskPanel";
import { Badge, Button, Card, CardHead, Meter, Money, PageHead } from "@/components/ui";
import { useAsk } from "@/lib/assistant/store";
import { MONTHLY_BUDGET_CENTS, TODAY, fmtDate, productBySku, runningLow, storeName } from "@/lib/data";
import { fmt } from "@/lib/money";
import { cartTotal, useTrip } from "@/lib/trip";
import { savedCents, usePurchases } from "@/lib/usePurchases";

export default function Overview() {
  const { purchases, trips } = usePurchases();
  const month = trips.filter((t) => new Date(t.ts).getMonth() === TODAY.getMonth());
  const spent = month.reduce((s, t) => s + t.total_cents, 0);
  const daysInMonth = 31;
  const pace = Math.round((MONTHLY_BUDGET_CENTS * TODAY.getDate()) / daysInMonth);
  const projected = Math.round((spent / TODAY.getDate()) * daysInMonth);
  const low = runningLow(purchases).slice(0, 6);
  const saved = savedCents(purchases.filter((p) => new Date(p.ts).getMonth() === TODAY.getMonth()));
  const avgTrip = Math.round(trips.slice(0, 8).reduce((s, t) => s + t.total_cents, 0) / Math.min(8, trips.length));

  return (
    <>
      <PageHead kicker="Saturday, October 17" title="Good afternoon, Maya.">
        <div className="flex gap-2">
          <Link href="/find">
            <Button variant="ghost">Compare prices</Button>
          </Link>
          <Link href="/order">
            <Button variant="primary">Send the robot</Button>
          </Link>
        </div>
      </PageHead>

      <LiveStrip />
      <AskBar />

      <div className="grid grid-cols-12 gap-6">
        {/* the one big number */}
        <section className="col-span-12 lg:col-span-7">
          <div className="label-caps">Groceries in October</div>
          <div className="mt-1 flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <span className="num font-display text-[72px] leading-none font-semibold tracking-[-0.04em]">{fmt(spent)}</span>
            <span className="text-[17px] text-ink-2">
              of <Money cents={MONTHLY_BUDGET_CENTS} /> budget
            </span>
          </div>
          <Meter value={spent} max={MONTHLY_BUDGET_CENTS} marks={[pace]} className="mt-5" />
          <div className="mt-2 flex justify-between font-mono text-[11.5px] text-muted">
            <span>Oct 1</span>
            <span>│ today&apos;s pace {fmt(pace)}</span>
            <span>Oct 31</span>
          </div>
          <p className="mt-4 max-w-[52ch] text-[15px] text-ink-2">
            {spent <= pace ? (
              <>
                You&apos;re <strong className="text-basil">{fmt(pace - spent)} under pace</strong> after {month.length} trips. At this rate October
                lands near <span className="num">{fmt(projected)}</span>.
              </>
            ) : (
              <>
                You&apos;re <strong className="text-tomato">{fmt(spent - pace)} over pace</strong>. At this rate October lands near{" "}
                <span className="num">{fmt(projected)}</span>.
              </>
            )}
          </p>

          <dl className="mt-7 grid grid-cols-3 border-y border-line">
            {[
              ["Trips this month", String(month.length)],
              ["Average basket", fmt(avgTrip)],
              ["Saved on deals", fmt(saved)],
            ].map(([k, v], i) => (
              <div key={k} className={i ? "border-l border-line px-4 py-3" : "py-3 pr-4"}>
                <dt className="label-caps">{k}</dt>
                <dd className="num mt-1 text-[22px] font-medium">{v}</dd>
              </div>
            ))}
          </dl>
        </section>

        {/* running low */}
        <Card className="col-span-12 self-start lg:col-span-5 lg:mt-[-8px]">
          <CardHead title="Probably running low" meta="from your purchase rhythm" />
          <ul className="divide-y divide-line">
            {low.map((r) => {
              const p = productBySku[r.sku];
              return (
                <li key={r.sku} className="flex items-center justify-between gap-3 px-4 py-2.5">
                  <div className="min-w-0">
                    <div className="truncate text-[14px] font-medium">{p.name}</div>
                    <div className="text-[12.5px] text-muted">
                      every ~{r.every_days} days · last bought {r.since_days} days ago
                    </div>
                  </div>
                  {r.due_in_days <= 0 ? <Badge tone="tomato">due</Badge> : <Badge tone="turmeric">in {r.due_in_days}d</Badge>}
                </li>
              );
            })}
          </ul>
          <div className="border-t border-line px-4 py-3">
            <Link href="/order?preset=basics" className="text-[13.5px] font-medium text-tomato hover:underline">
              Add these to a basics order →
            </Link>
          </div>
        </Card>

        {/* recent trips */}
        <section className="col-span-12">
          <div className="mb-2 flex items-baseline justify-between">
            <h2 className="font-display text-[19px] font-semibold">Recent trips</h2>
            <Link href="/purchases" className="text-[13.5px] text-ink-2 hover:text-ink hover:underline">
              All {trips.length} trips →
            </Link>
          </div>
          <div className="grid gap-px border border-line bg-line sm:grid-cols-3">
            {trips.slice(0, 3).map((t) => (
              <div key={t.trip_id} className="bg-card px-4 py-3.5">
                <div className="flex items-center justify-between">
                  <span className="font-mono text-[12px] text-muted">{fmtDate(t.ts, { weekday: "short", month: "short", day: "numeric" })}</span>
                  <Badge tone={t.store_id === "online" ? "plum" : "basil"}>{storeName(t.store_id)}</Badge>
                </div>
                <div className="num mt-2 text-[24px] font-medium">{fmt(t.total_cents)}</div>
                <div className="mt-1 line-clamp-2 text-[13px] text-ink-2">
                  {t.lines
                    .slice(0, 4)
                    .map((l) => productBySku[l.sku]?.name.split(" ").slice(0, 2).join(" "))
                    .join(", ")}
                  {t.lines.length > 4 ? ` +${t.lines.length - 4} more` : ""}
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </>
  );
}

function LiveStrip() {
  const { phase, draft, cart, robot, mode, activeStop, stops } = useTrip();
  if (phase === "idle" || !draft) return null;
  const label =
    phase === "waiting_login"
      ? "Waiting for you to log in at the robot"
      : phase === "done"
        ? "Trip finished · receipt ready"
        : robot.status === "waiting"
          ? "Robot is waiting on you"
          : `Robot heading to ${activeStop ? stops[activeStop]?.label : "…"}`;
  return (
    <Link href="/trip" className="mb-7 flex items-center gap-4 border border-ink bg-ink px-4 py-3 text-paper hover:bg-night">
      <span className="flex items-center gap-2 font-mono text-[11px] tracking-wider text-tomato uppercase">
        <span className={phase === "done" ? "size-2 rounded-full bg-basil" : "live-dot size-2 rounded-full bg-tomato"} />
        {phase === "done" ? "done" : "live"}
      </span>
      <span className="font-semibold">{draft.title}</span>
      <span className="text-paper/70">{label}</span>
      <span className="num ml-auto text-paper/80">
        {cart.length} lines · {fmt(cartTotal(cart))} {mode === "pickup" ? "· pickup" : ""}
      </span>
      <span aria-hidden>→</span>
    </Link>
  );
}

function AskBar() {
  const ask = useAsk((s) => s.ask);
  return (
    <form
      className="mb-8"
      onSubmit={(e) => {
        e.preventDefault();
        const f = new FormData(e.currentTarget);
        ask(String(f.get("q") ?? ""));
        e.currentTarget.reset();
      }}
    >
      <div className="flex border border-ink bg-card focus-within:outline-2 focus-within:outline-tomato">
        <span className="grid w-12 shrink-0 place-items-center border-r border-ink bg-tomato font-display text-[18px] font-bold text-white">?</span>
        <input
          name="q"
          placeholder="Ask Picky: “is coffee a good price?”, “where did my money go?”, “can I afford the Pixel Buds?”"
          className="min-w-0 flex-1 bg-transparent px-4 py-3 text-[15.5px] outline-none placeholder:text-muted"
          aria-label="Ask Picky"
        />
        <button className="border-l border-ink bg-ink px-5 text-[14px] font-medium text-paper hover:bg-night">Ask</button>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {SUGGESTIONS.slice(0, 4).map((s) => (
          <button key={s} type="button" onClick={() => ask(s)} className="border border-line px-2 py-0.5 text-[12.5px] text-ink-2 hover:border-ink hover:text-ink">
            {s}
          </button>
        ))}
      </div>
    </form>
  );
}
