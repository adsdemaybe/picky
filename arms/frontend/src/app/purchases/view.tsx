"use client";
import { Fragment, useMemo, useState } from "react";
import { SpendChart } from "@/components/SpendChart";
import { Badge, Card, CardHead, Money, PageHead, Stars, cx } from "@/components/ui";
import { MONTHLY_BUDGET_CENTS, fmtDate, mostBought, productBySku, storeName, weeklyByCategory } from "@/lib/data";
import { fmt } from "@/lib/money";
import { usePurchases } from "@/lib/usePurchases";

export default function PurchasesPage() {
  const { purchases, trips } = usePurchases();
  const [open, setOpen] = useState<string | null>(trips[0]?.trip_id ?? null);
  const [filter, setFilter] = useState<"all" | "store_ave" | "online">("all");
  const [ratings, setRatings] = useState<Record<string, number>>({});
  const rows = useMemo(() => weeklyByCategory(purchases, 12), [purchases]);
  const top = useMemo(() => mostBought(purchases, 8), [purchases]);
  const total12 = rows.reduce((s, r) => s + r.total, 0);
  const shown = trips.filter((t) => filter === "all" || t.store_id === filter);

  return (
    <>
      <PageHead kicker="Spending" title="Everything you've bought since July">
        <div className="text-right">
          <div className="label-caps">Last 12 weeks</div>
          <div className="num text-[26px] font-medium">{fmt(total12)}</div>
        </div>
      </PageHead>

      <Card className="mb-8 p-5">
        <div className="mb-4 flex items-baseline justify-between">
          <h2 className="font-display text-[19px] font-semibold">Weekly spend by category</h2>
          <span className="text-[13px] text-muted">avg {fmt(Math.round(total12 / 12))} / week</span>
        </div>
        <SpendChart rows={rows} budgetPerWeek={Math.round((MONTHLY_BUDGET_CENTS * 12) / 52)} />
      </Card>

      <div className="grid grid-cols-12 gap-6">
        <section className="col-span-12 xl:col-span-8">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="font-display text-[19px] font-semibold">Trips</h2>
            <div className="flex border border-line text-[12.5px]">
              {(["all", "store_ave", "online"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setFilter(f)}
                  className={cx("px-3 py-1", filter === f ? "bg-ink text-paper" : "text-ink-2 hover:bg-card", f !== "all" && "border-l border-line")}
                >
                  {f === "all" ? "All" : storeName(f)}
                </button>
              ))}
            </div>
          </div>
          <div className="border border-line bg-card">
            <table className="w-full text-[14px]">
              <thead>
                <tr className="label-caps border-b border-line text-left">
                  <th className="py-2 pl-4 font-normal">Date</th>
                  <th className="py-2 font-normal">Where</th>
                  <th className="py-2 font-normal">Items</th>
                  <th className="py-2 pr-4 text-right font-normal">Total</th>
                </tr>
              </thead>
              <tbody>
                {shown.map((t) => (
                  <Fragment key={t.trip_id}>
                    <tr
                      className={cx("cursor-pointer border-b border-line hover:bg-paper", open === t.trip_id && "bg-paper")}
                      onClick={() => setOpen(open === t.trip_id ? null : t.trip_id)}
                    >
                      <td className="py-2.5 pl-4">
                        <span className="mr-2 inline-block w-3 font-mono text-muted">{open === t.trip_id ? "−" : "+"}</span>
                        {fmtDate(t.ts, { weekday: "short", month: "short", day: "numeric" })}
                        {t.trip_id === "trip_maya_1017" && <Badge tone="tomato" className="ml-2">today · robot</Badge>}
                      </td>
                      <td>
                        <Badge tone={t.store_id === "online" ? "plum" : "basil"}>{storeName(t.store_id)}</Badge>
                      </td>
                      <td className="num text-ink-2">
                        {t.items} items · {t.lines.length} lines
                      </td>
                      <td className="num pr-4 text-right font-medium">{fmt(t.total_cents)}</td>
                    </tr>
                    {open === t.trip_id && (
                      <tr className="border-b border-line bg-paper">
                        <td colSpan={4} className="px-4 pt-1 pb-3">
                          <table className="w-full text-[13px]">
                            <tbody>
                              {t.lines.map((l) => {
                                const p = productBySku[l.sku];
                                const r = ratings[l.purchase_id] ?? l.rating;
                                const base = p.price_cents;
                                return (
                                  <tr key={l.purchase_id} className="border-b border-line/50 last:border-0">
                                    <td className="py-1.5 pl-5">
                                      <span className="num mr-2 text-muted">{l.qty}×</span>
                                      {p.name}
                                    </td>
                                    <td className="num text-right text-ink-2">
                                      {fmt(l.unit_price_cents)}
                                      {l.unit_price_cents < base && <span className="ml-1.5 text-basil">−{Math.round((1 - l.unit_price_cents / base) * 100)}%</span>}
                                    </td>
                                    <td className="w-[150px] pl-4 text-right">
                                      <RateInput value={r} onChange={(n) => setRatings((x) => ({ ...x, [l.purchase_id]: n }))} />
                                    </td>
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                          {t.synthetic && <div className="mt-2 font-mono text-[10.5px] text-muted">DEMO HISTORY · generated from the fixture profile</div>}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
          {Object.keys(ratings).length > 0 && (
            <p className="mt-2 text-[13px] text-basil">
              {Object.keys(ratings).length} new rating{Object.keys(ratings).length > 1 ? "s" : ""}. Your next order&apos;s suggestions will use them.
            </p>
          )}
        </section>

        <Card className="col-span-12 self-start xl:col-span-4">
          <CardHead title="What you buy most" meta="avg price paid" />
          <ol className="divide-y divide-line">
            {top.map((r, i) => (
              <li key={r.sku} className="grid grid-cols-[22px_1fr_auto] items-center gap-2 px-4 py-2.5">
                <span className="num text-[12px] text-muted">{i + 1}</span>
                <div className="min-w-0">
                  <div className="truncate text-[14px]">{productBySku[r.sku].name}</div>
                  <div className="text-[12px] text-muted">
                    {r.times} trips · {r.qty} units {r.avg_rating ? <>· <Stars n={Math.round(r.avg_rating)} /></> : null}
                  </div>
                </div>
                <Money cents={r.avg_unit_cents} className="text-[13.5px]" />
              </li>
            ))}
          </ol>
        </Card>
      </div>
    </>
  );
}

function RateInput({ value, onChange }: { value: number | null; onChange: (n: number) => void }) {
  const [h, setH] = useState(0);
  const v = h || value || 0;
  return (
    <span className="inline-flex" onMouseLeave={() => setH(0)} role="radiogroup" aria-label="Rate">
      {[1, 2, 3, 4, 5].map((n) => (
        <button
          key={n}
          onClick={(e) => {
            e.stopPropagation();
            onChange(n);
          }}
          onMouseEnter={() => setH(n)}
          className={cx("px-[1px] text-[14px] leading-none", n <= v ? "text-turmeric" : "text-line-strong hover:text-turmeric")}
          aria-label={`${n} stars`}
        >
          ★
        </button>
      ))}
    </span>
  );
}
