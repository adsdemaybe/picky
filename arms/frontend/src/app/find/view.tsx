"use client";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { OffersTable, Sparkline, VerdictBadge } from "@/components/PriceBits";
import { Badge, Button, Card, CardHead, PageHead, Swatch, cx } from "@/components/ui";
import { useAsk } from "@/lib/assistant/store";
import { TODAY, allProducts, mostBought, productBySku, type Product } from "@/lib/data";
import { fmt } from "@/lib/money";
import { alternativesFor, effective, historyFor, offersFor, robotStoreFor, searchProducts, upcomingFor, verdictFor } from "@/lib/prices";
import { usePurchases } from "@/lib/usePurchases";

const CHIPS = [
  { label: "Coffee", q: "coffee" },
  { label: "Salsa", q: "salsa" },
  { label: "Olive oil", q: "olive oil" },
  { label: "Phone cases", q: "case" },
  { label: "Earbuds", q: "earbuds" },
  { label: "Chargers", q: "charger" },
  { label: "Phones", q: "phone" },
];

export default function FindPage() {
  return (
    <Suspense>
      <Find />
    </Suspense>
  );
}

function Find() {
  const params = useSearchParams();
  const router = useRouter();
  const [q, setQ] = useState(params.get("q") ?? "");
  const sku = params.get("sku");
  const results = useMemo(() => searchProducts(params.get("q") ?? "", 16), [params]);

  const go = (next: { q?: string; sku?: string }) => {
    const u = new URLSearchParams();
    if (next.q ?? params.get("q")) u.set("q", next.q ?? params.get("q")!);
    if (next.sku) u.set("sku", next.sku);
    router.push(`/find?${u.toString()}`);
  };

  return (
    <>
      <PageHead kicker="Find & compare" title="Is it a good price, and where is it cheapest?" />
      <form
        onSubmit={(e) => {
          e.preventDefault();
          go({ q, sku: undefined });
        }}
        className="mb-3 flex border border-ink bg-card focus-within:outline-2 focus-within:outline-tomato"
      >
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Search groceries, phones, accessories…"
          className="min-w-0 flex-1 bg-transparent px-4 py-3.5 font-display text-[21px] tracking-tight outline-none placeholder:text-muted"
          aria-label="Search products"
        />
        <button className="border-l border-ink bg-ink px-5 text-[14px] font-medium text-paper hover:bg-night">Compare</button>
      </form>
      <div className="mb-7 flex flex-wrap items-center gap-2 text-[13px] text-muted">
        Popular:
        {CHIPS.map((c) => (
          <button
            key={c.q}
            onClick={() => {
              setQ(c.q);
              go({ q: c.q });
            }}
            className="border border-line px-2 py-0.5 text-ink-2 hover:border-ink hover:text-ink"
          >
            {c.label}
          </button>
        ))}
        <span className="ml-auto">Compares 7 retailers incl. open-box and warehouse.</span>
      </div>

      <div className="grid grid-cols-12 gap-6">
        <div className={cx("col-span-12", sku ? "lg:col-span-5" : "")}>
          {params.get("q") ? (
            <ResultList products={results} selected={sku} onPick={(s) => go({ sku: s })} title={`${results.length} result${results.length === 1 ? "" : "s"} for "${params.get("q")}"`} />
          ) : (
            <ForYou onPick={(s) => go({ sku: s })} selected={sku} wide={!sku} />
          )}
        </div>
        {sku && productBySku[sku] && (
          <div className="col-span-12 lg:col-span-7">
            <Detail sku={sku} onPick={(s) => go({ sku: s })} />
          </div>
        )}
      </div>
    </>
  );
}

function ResultList({ products, selected, onPick, title }: { products: Product[]; selected: string | null; onPick: (s: string) => void; title: string }) {
  if (!products.length) return <Card variant="flat" className="p-5 text-ink-2">Nothing matched. Try a broader word, like &ldquo;salsa&rdquo; or &ldquo;case&rdquo;.</Card>;
  return (
    <Card>
      <CardHead title={title} meta="best price incl. fees" />
      <ul className="divide-y divide-line">
        {products.map((p) => {
          const best = offersFor(p.sku).find((o) => o.in_stock) ?? offersFor(p.sku)[0];
          return (
            <li key={p.sku}>
              <button onClick={() => onPick(p.sku)} className={cx("flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-paper", selected === p.sku && "bg-paper")}>
                <Swatch color={p.label_color} className="size-3.5" />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[14px] font-medium">{p.name}</div>
                  <div className="text-[12px] text-muted">
                    {p.brand} · best at {best?.retailer}
                  </div>
                </div>
                <VerdictBadge sku={p.sku} />
                <span className="num w-[78px] text-right text-[14px]">{best ? fmt(effective(best)) : "·"}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </Card>
  );
}

function ForYou({ onPick, selected, wide }: { onPick: (s: string) => void; selected: string | null; wide: boolean }) {
  const { purchases } = usePurchases();
  const staples = mostBought(
    purchases.filter((p) => TODAY.getTime() - Date.parse(p.ts) < 84 * 86_400_000),
    14,
  ).map((m) => m.sku);
  const good = staples.filter((s) => verdictFor(s).label === "Good price" || offersFor(s).some((o) => o.note));
  const wait = [...staples, ...allProducts.map((p) => p.sku)].filter((s, i, a) => a.indexOf(s) === i && upcomingFor(s)).slice(0, 5);
  return (
    <div className={cx("grid gap-6", wide && "lg:grid-cols-2")}>
      <ResultList products={good.map((s) => productBySku[s])} selected={selected} onPick={onPick} title="Good buys on things you already get" />
      <Card>
        <CardHead title="Worth waiting for" meta="announced sales" />
        <ul className="divide-y divide-line">
          {wait.map((s) => {
            const up = upcomingFor(s)!;
            const p = productBySku[s];
            return (
              <li key={s}>
                <button onClick={() => onPick(s)} className="flex w-full items-center gap-3 px-4 py-2.5 text-left hover:bg-paper">
                  <Swatch color={p.label_color} className="size-3.5" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[14px] font-medium">{p.name}</div>
                    <div className="text-[12px] text-muted">{up.reason}</div>
                  </div>
                  <span className="num text-[13px] text-basil">{fmt(up.price_cents)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </Card>
    </div>
  );
}

function Detail({ sku, onPick }: { sku: string; onPick: (s: string) => void }) {
  const p = productBySku[sku];
  const v = verdictFor(sku);
  const offers = offersFor(sku);
  const best = offers.find((o) => o.in_stock) ?? offers[0];
  const worst = offers[offers.length - 1];
  const alts = alternativesFor(sku);
  const robot = robotStoreFor(sku);
  const ask = useAsk((s) => s.ask);
  const h = historyFor(sku);
  return (
    <Card className="lg:sticky lg:top-6">
      <div className="border-b border-line px-5 pt-4 pb-4">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="label-caps">
              {p.brand} · {p.category.replace("_", " ")}
            </div>
            <h2 className="mt-1 font-display text-[26px] leading-tight font-semibold tracking-tight">{p.name}</h2>
          </div>
          <VerdictBadge sku={sku} big />
        </div>
        <p className="mt-2 max-w-[56ch] text-[14.5px] text-ink-2">{v.why}</p>
        <div className="mt-4 flex flex-wrap items-end gap-8">
          <div>
            <div className="label-caps">Best price</div>
            <div className="num text-[32px] leading-none font-medium">{fmt(effective(best))}</div>
            <div className="mt-1 text-[12.5px] text-muted">{best.retailer}</div>
          </div>
          {worst !== best && effective(worst) > effective(best) && (
            <div>
              <div className="label-caps">Spread</div>
              <div className="num text-[20px]">{fmt(effective(worst) - effective(best))}</div>
              <div className="mt-1 text-[12.5px] text-muted">vs {worst.retailer}</div>
            </div>
          )}
          <div className="ml-auto">
            <div className="label-caps mb-1">The Ave, 13 weeks</div>
            <Sparkline sku={sku} w={240} h={56} />
            <div className="font-mono text-[10.5px] text-muted">
              low {fmt(Math.min(...h))} · high {fmt(Math.max(...h))}
            </div>
          </div>
        </div>
      </div>

      <div className="px-5 py-2">
        <OffersTable sku={sku} />
      </div>

      {alts.length > 0 && (
        <div className="border-t border-line px-5 py-3">
          <div className="label-caps mb-2">Cheaper swaps that do the same job</div>
          <ul className="flex flex-col gap-1.5">
            {alts.map((a) => (
              <li key={a.product.sku}>
                <button onClick={() => onPick(a.product.sku)} className="flex w-full items-center gap-2 text-left text-[13.5px] hover:underline">
                  <Swatch color={a.product.label_color} />
                  <span className="flex-1">{a.product.name}</span>
                  <span className="num text-basil">
                    {fmt(effective(a.best))} <span className="text-muted">at {a.best.retailer.split(" ·")[0]}</span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-ink px-5 py-3">
        {robot ? (
          <Link href={`/order?store=${robot.store_id}&add=${sku}`}>
            <Button variant="primary">Add to a robot order · {robot.short}</Button>
          </Link>
        ) : (
          <Badge>not stocked in a robot store</Badge>
        )}
        <Button variant="ghost" onClick={() => ask(`Is the ${p.name} a good price, and can I afford it?`)}>
          Ask Picky about this
        </Button>
        <span className="ml-auto font-mono text-[10.5px] text-muted">NO AFFILIATE OR SPONSORED RANKING</span>
      </div>
    </Card>
  );
}
