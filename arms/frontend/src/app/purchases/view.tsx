"use client";
import { ChevronDown, Star } from "lucide-react";
import { useMemo, useState } from "react";
import { Money, Panel, ProductTile, SectionTitle } from "@/components/picky";
import { SpendChart } from "@/components/SpendChart";
import { Badge } from "@/components/ui/badge";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { MONTHLY_BUDGET_CENTS, fmtDate, mostBought, productBySku, storeName, weeklyByCategory } from "@/lib/data";
import { fmt } from "@/lib/money";
import { usePurchases } from "@/lib/usePurchases";
import { cn } from "@/lib/utils";

export default function SpendingPage() {
  const { purchases, trips } = usePurchases();
  const [open, setOpen] = useState<string | null>(trips[0]?.trip_id ?? null);
  const [filter, setFilter] = useState("all");
  const [ratings, setRatings] = useState<Record<string, number>>({});
  const rows = useMemo(() => weeklyByCategory(purchases, 12), [purchases]);
  const top = useMemo(() => mostBought(purchases, 8), [purchases]);
  const total12 = rows.reduce((s, r) => s + r.total, 0);
  const shown = trips.filter((t) => filter === "all" || (filter === "online" ? t.store_id === "online" : t.store_id !== "online"));
  const rated = Object.keys(ratings).length;

  return (
    <div className="flex flex-col gap-8">
      <section className="flex animate-rise flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[36px] leading-[1.05] font-bold md:text-[46px]">Where your money went</h1>
          <p className="mt-2 text-[16px] text-muted-foreground">Every grocery and tech purchase since July, by week and by trip.</p>
        </div>
        <div className="rounded-2xl bg-sky px-5 py-3 text-white">
          <div className="text-[13px] text-white/80">Last 12 weeks</div>
          <Money cents={total12} className="font-heading text-[30px] leading-tight font-bold" />
          <div className="text-[13px] text-white/80">about {fmt(Math.round(total12 / 12))} a week</div>
        </div>
      </section>

      <Panel className="p-5 md:p-6">
        <SectionTitle>Spending by week</SectionTitle>
        <SpendChart rows={rows} budgetPerWeek={Math.round((MONTHLY_BUDGET_CENTS * 12) / 52)} />
      </Panel>

      <div className="grid gap-8 lg:grid-cols-12">
        <section className="lg:col-span-8">
          <SectionTitle
            action={
              <ToggleGroup type="single" value={filter} onValueChange={(v) => v && setFilter(v)} className="rounded-full bg-muted p-1" aria-label="Filter trips">
                {[
                  ["all", "All"],
                  ["store", "In store"],
                  ["online", "Online"],
                ].map(([v, l]) => (
                  <ToggleGroupItem key={v} value={v} className="h-8 rounded-full px-3.5 text-[13px] data-[state=on]:bg-card data-[state=on]:shadow-sm">
                    {l}
                  </ToggleGroupItem>
                ))}
              </ToggleGroup>
            }
          >
            Trips
          </SectionTitle>
          <div className="flex flex-col gap-2">
            {shown.map((t) => {
              const isOpen = open === t.trip_id;
              return (
                <Panel key={t.trip_id} className="overflow-hidden">
                  <button
                    onClick={() => setOpen(isOpen ? null : t.trip_id)}
                    aria-expanded={isOpen}
                    className="flex w-full cursor-pointer items-center gap-3 p-4 text-left hover:bg-muted/60"
                  >
                    <div className="flex -space-x-2">
                      {t.lines.slice(0, 3).map((l) => {
                        const p = productBySku[l.sku];
                        return <ProductTile key={l.purchase_id} color={p.label_color} category={p.category} name={p.name} size={34} className="ring-2 ring-card" />;
                      })}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2 font-semibold">
                        {fmtDate(t.ts, { weekday: "long", month: "short", day: "numeric" })}
                        {t.trip_id.startsWith("trip_maya_1017") && <Badge variant="lime">Today, by robot</Badge>}
                      </div>
                      <div className="text-[13px] text-muted-foreground">
                        {t.items} items at {storeName(t.store_id)}
                      </div>
                    </div>
                    <Money cents={t.total_cents} className="font-heading text-[18px] font-semibold" />
                    <ChevronDown className={cn("size-5 text-muted-foreground transition-transform", isOpen && "rotate-180")} aria-hidden />
                  </button>
                  {isOpen && (
                    <ul className="animate-rise border-t border-border px-4 py-2">
                      {t.lines.map((l) => {
                        const p = productBySku[l.sku];
                        const r = ratings[l.purchase_id] ?? l.rating;
                        const off = l.unit_price_cents < p.price_cents ? Math.round((1 - l.unit_price_cents / p.price_cents) * 100) : 0;
                        return (
                          <li key={l.purchase_id} className="flex items-center gap-3 py-2">
                            <ProductTile color={p.label_color} category={p.category} name={p.name} size={32} />
                            <span className="min-w-0 flex-1 truncate text-[14px]">
                              <span className="text-muted-foreground">{l.qty} × </span>
                              {p.name}
                            </span>
                            {off > 0 && <Badge variant="lime">{off}% off</Badge>}
                            <Money cents={l.unit_price_cents} className="w-16 text-right text-[14px]" />
                            <RateInput name={p.name} value={r} onChange={(n) => setRatings((x) => ({ ...x, [l.purchase_id]: n }))} />
                          </li>
                        );
                      })}
                      {t.synthetic && <li className="pt-1 pb-2 text-[12px] text-muted-foreground">Demo history generated from Maya&apos;s profile.</li>}
                    </ul>
                  )}
                </Panel>
              );
            })}
          </div>
          {rated > 0 && (
            <p className="mt-3 rounded-xl bg-lime px-4 py-3 text-[14px] font-medium text-lime-ink" role="status">
              Thanks. Picky will use {rated === 1 ? "that rating" : `those ${rated} ratings`} the next time it suggests something.
            </p>
          )}
        </section>

        <section className="lg:col-span-4">
          <SectionTitle>What you buy most</SectionTitle>
          <Panel className="p-2">
            {top.map((r) => {
              const p = productBySku[r.sku];
              return (
                <div key={r.sku} className="flex items-center gap-3 rounded-xl p-2.5">
                  <ProductTile color={p.label_color} category={p.category} name={p.name} size={40} />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[14px] font-semibold">{p.name}</div>
                    <div className="text-[12.5px] text-muted-foreground">
                      {r.times} trips{r.avg_rating ? `, rated ${r.avg_rating.toFixed(1)}` : ""}
                    </div>
                  </div>
                  <div className="text-right">
                    <Money cents={r.avg_unit_cents} className="font-semibold" />
                    <div className="text-[11.5px] text-muted-foreground">usual price</div>
                  </div>
                </div>
              );
            })}
          </Panel>
        </section>
      </div>
    </div>
  );
}

function RateInput({ name, value, onChange }: { name: string; value: number | null; onChange: (n: number) => void }) {
  const [h, setH] = useState(0);
  const v = h || value || 0;
  return (
    <span className="inline-flex" onMouseLeave={() => setH(0)} role="radiogroup" aria-label={`Rate ${name}`}>
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          role="radio"
          aria-checked={value === n}
          onClick={() => onChange(n)}
          onMouseEnter={() => setH(n)}
          className="cursor-pointer p-0.5"
          aria-label={`${n} star${n > 1 ? "s" : ""}`}
        >
          <Star className={cn("size-4 transition-colors", n <= v ? "fill-amber text-amber" : "text-input")} strokeWidth={1.8} />
        </button>
      ))}
    </span>
  );
}
