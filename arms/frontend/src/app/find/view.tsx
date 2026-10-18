"use client";
import { Bot, ChevronRight, Search, Sparkles } from "lucide-react";
import { motion } from "motion/react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { CountUp } from "@/components/motion";
import { Money, Panel, ProductTile, SectionTitle, VerdictChip, categoryIcon } from "@/components/picky";
import { OffersTable, Sparkline } from "@/components/PriceBits";
import { Button } from "@/components/ui/button";
import { useAsk } from "@/lib/assistant/store";
import { TODAY, allProducts, mostBought, productBySku, type Product } from "@/lib/data";
import { fmt } from "@/lib/money";
import { alternativesFor, effective, historyFor, offersFor, robotStoreFor, searchProducts, upcomingFor, verdictFor } from "@/lib/prices";
import { Ingredients } from "@/components/Ingredients";
import { usePurchases } from "@/lib/usePurchases";
import { cn } from "@/lib/utils";

// quick searches = things on the shelves of the grocery stores (each matches in-stock products at The Ave)
const CHIPS = [
  { label: "Salsa", q: "salsa", cat: "pantry" },
  { label: "Tortilla chips", q: "tortilla chips", cat: "snacks" },
  { label: "Avocados", q: "avocado", cat: "produce" },
  { label: "Cheese", q: "cheese", cat: "dairy" },
  { label: "Beans", q: "beans", cat: "pantry" },
  { label: "Oat milk", q: "oat milk", cat: "dairy" },
  { label: "Coffee", q: "coffee", cat: "pantry" },
  { label: "Olive oil", q: "olive oil", cat: "pantry" },
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
  const query = params.get("q") ?? "";
  const [q, setQ] = useState(query);
  const sku = params.get("sku");
  const results = searchProducts(query, 16);

  const go = (next: { q?: string; sku?: string }) => {
    const u = new URLSearchParams();
    const nq = next.q ?? query;
    if (nq) u.set("q", nq);
    if (next.sku) u.set("sku", next.sku);
    router.push(`/find?${u.toString()}`, { scroll: false });
  };

  return (
    <div className="flex flex-col gap-8">
      <section className="animate-rise">
        <h1 className="text-[36px] leading-[1.05] font-bold md:text-[46px]">Is it a good price?</h1>
        <p className="mt-2 max-w-[60ch] text-[16px] text-muted-foreground">
          Picky checks seven stores, including open-box and warehouse prices, and 13 weeks of price history before you buy.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            go({ q, sku: undefined });
          }}
          className="mt-5 flex items-center gap-2 rounded-full bg-card p-2 pl-5 shadow-md ring-1 ring-foreground/[0.06] focus-within:ring-2 focus-within:ring-ring"
        >
          <Search className="size-5 text-muted-foreground" aria-hidden />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Coffee, salsa, a Pixel 9 case…"
            aria-label="Search products"
            className="min-w-0 flex-1 bg-transparent text-[18px] outline-none placeholder:text-muted-foreground"
          />
          <Button type="submit" size="lg">
            Compare prices
          </Button>
        </form>
        <div className="mt-4 flex flex-wrap gap-2">
          {CHIPS.map((c) => {
            const Icon = categoryIcon(c.cat, c.label);
            const on = query === c.q;
            return (
              <button
                key={c.q}
                onClick={() => {
                  setQ(c.q);
                  go({ q: c.q });
                }}
                aria-pressed={on}
                className={cn(
                  "flex h-10 cursor-pointer items-center gap-2 rounded-full px-4 text-[14px] font-medium transition-colors",
                  on ? "bg-foreground text-background" : "bg-card text-foreground ring-1 ring-foreground/[0.08] hover:bg-muted",
                )}
              >
                <Icon className="size-4" aria-hidden />
                {c.label}
              </button>
            );
          })}
        </div>
      </section>

      <div className="grid gap-6 lg:grid-cols-12">
        <div className={cn(sku ? "lg:col-span-5" : "lg:col-span-12")}>
          {query ? (
            <ResultList products={results} selected={sku} onPick={(s) => go({ sku: s })} title={`${results.length} result${results.length === 1 ? "" : "s"} for “${query}”`} />
          ) : (
            <ForYou onPick={(s) => go({ sku: s })} selected={sku} wide={!sku} />
          )}
        </div>
        {sku && productBySku[sku] && (
          <div className="lg:col-span-7">
            <motion.div key={sku} initial={{ opacity: 0, y: 16, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} transition={{ type: "spring", stiffness: 300, damping: 28 }}>
              <Detail sku={sku} onPick={(s) => go({ sku: s })} />
            </motion.div>
          </div>
        )}
      </div>
    </div>
  );
}

function Row({ p, selected, onPick }: { p: Product; selected: boolean; onPick: () => void }) {
  const best = offersFor(p.sku).find((o) => o.in_stock) ?? offersFor(p.sku)[0];
  return (
    <motion.button
      onClick={onPick}
      aria-current={selected}
      variants={{ hidden: { opacity: 0, y: 10 }, show: { opacity: 1, y: 0 } }}
      whileTap={{ scale: 0.98 }}
      className={cn("relative flex w-full cursor-pointer items-center gap-3 rounded-xl p-3 text-left transition-colors", !selected && "hover:bg-muted")}
    >
      {selected && <motion.span layoutId="find-selected" className="absolute inset-0 rounded-xl bg-lime/50" transition={{ type: "spring", stiffness: 500, damping: 40 }} />}
      <ProductTile color={p.label_color} category={p.category} name={p.name} size={44} className="relative" />
      <div className="relative min-w-0 flex-1">
        <div className="truncate font-semibold">{p.name}</div>
        <div className="truncate text-[13px] text-muted-foreground">Cheapest at {best?.retailer.split(",")[0]}</div>
      </div>
      <div className="relative flex flex-col items-end gap-1">
        {best && <Money cents={effective(best)} className="font-heading text-[17px] font-semibold" />}
        <VerdictChip verdict={verdictFor(p.sku)} />
      </div>
    </motion.button>
  );
}

function ResultList({ products, selected, onPick, title }: { products: Product[]; selected: string | null; onPick: (s: string) => void; title: string }) {
  if (!products.length)
    return (
      <Panel className="p-6 text-center">
        <p className="font-semibold">Nothing matched that.</p>
        <p className="mt-1 text-muted-foreground">Try a simpler word, like “salsa” or “case”.</p>
      </Panel>
    );
  return (
    <div>
      <SectionTitle>{title}</SectionTitle>
      <Panel className="p-2">
        <motion.div initial="hidden" animate="show" variants={{ hidden: {}, show: { transition: { staggerChildren: 0.04 } } }}>
        {products.map((p) => (
          <Row key={p.sku} p={p} selected={selected === p.sku} onPick={() => onPick(p.sku)} />
        ))}
        </motion.div>
      </Panel>
    </div>
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
    <div className={cn("grid gap-8", wide && "lg:grid-cols-2")}>
      <div>
        <SectionTitle>Good buys on your usual</SectionTitle>
        <Panel className="p-2">
          <motion.div initial="hidden" animate="show" variants={{ hidden: {}, show: { transition: { staggerChildren: 0.05 } } }}>
            {good.map((s) => (
              <Row key={s} p={productBySku[s]} selected={selected === s} onPick={() => onPick(s)} />
            ))}
          </motion.div>
        </Panel>
      </div>
      <div>
        <SectionTitle>Worth waiting for</SectionTitle>
        <Panel className="p-2">
          {wait.map((s) => {
            const up = upcomingFor(s)!;
            const p = productBySku[s];
            return (
              <button
                key={s}
                onClick={() => onPick(s)}
                className={cn("flex w-full cursor-pointer items-center gap-3 rounded-xl p-3 text-left hover:bg-muted", selected === s && "bg-lime/50")}
              >
                <ProductTile color={p.label_color} category={p.category} name={p.name} size={44} />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-semibold">{p.name}</div>
                  <div className="truncate text-[13px] text-muted-foreground">{up.reason}</div>
                </div>
                <span className="rounded-full bg-amber-wash px-2.5 py-1 text-[13px] font-semibold text-amber-ink">
                  <Money cents={up.price_cents} />
                </span>
              </button>
            );
          })}
        </Panel>
      </div>
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
    <Panel className="overflow-hidden lg:sticky lg:top-24">
      <div className="flex items-start gap-4 p-5 pb-4">
        <ProductTile color={p.label_color} category={p.category} name={p.name} size={64} />
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] text-muted-foreground">{p.brand}</div>
          <h2 className="text-[26px] leading-tight font-bold">{p.name}</h2>
        </div>
      </div>

      <div className="mx-5 rounded-2xl bg-muted p-4">
        <VerdictChip verdict={v} size="lg" />
        <p className="mt-2 text-[17px] font-medium">{v.why}</p>
        <div className="mt-4 flex flex-wrap items-end justify-between gap-4">
          <div>
            <div className="text-[13px] text-muted-foreground">Best price today</div>
            <CountUp cents={effective(best)} className="font-heading text-[40px] leading-none font-bold" />
            <div className="mt-1 text-[13.5px] font-medium">at {best.retailer}</div>
            {effective(worst) > effective(best) && (
              <div className="mt-0.5 text-[13px] text-muted-foreground">
                {fmt(effective(worst) - effective(best))} less than {worst.retailer.split(",")[0]}
              </div>
            )}
          </div>
          <div>
            <Sparkline sku={sku} w={240} h={70} />
            <div className="tabular text-right text-[12px] text-muted-foreground">
              Low {fmt(Math.min(...h))}, high {fmt(Math.max(...h))}
            </div>
          </div>
        </div>
      </div>

      <Ingredients p={p} />

      <div className="p-5">
        <h3 className="mb-2 text-[16px] font-semibold">Every store we checked</h3>
        <OffersTable sku={sku} />
      </div>

      {alts.length > 0 && (
        <div className="border-t border-border p-5">
          <h3 className="mb-2 text-[16px] font-semibold">Cheaper swaps that do the same job</h3>
          <div className="flex flex-col gap-1">
            {alts.map((a) => (
              <button key={a.product.sku} onClick={() => onPick(a.product.sku)} className="flex cursor-pointer items-center gap-3 rounded-xl p-2 text-left hover:bg-muted">
                <ProductTile color={a.product.label_color} category={a.product.category} name={a.product.name} size={36} />
                <span className="min-w-0 flex-1 truncate font-medium">{a.product.name}</span>
                <span className="text-[13px] text-muted-foreground">{a.best.retailer.split(",")[0]}</span>
                <Money cents={effective(a.best)} className="font-semibold text-leaf" />
                <ChevronRight className="size-4 text-muted-foreground" aria-hidden />
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="flex flex-wrap items-center gap-2 border-t border-border bg-muted/50 p-4">
        {robot ? (
          <Button asChild size="lg">
            <Link href={`/order?store=${robot.store_id}&add=${sku}`}>
              <Bot /> Have the robot grab it
            </Link>
          </Button>
        ) : null}
        <Button size="lg" variant="outline" onClick={() => ask(`Is the ${p.name} a good price, and can I afford it?`)}>
          <Sparkles className="text-sky" /> Ask Picky
        </Button>
        <p className="w-full pt-1 text-[12.5px] text-muted-foreground">Ranked by price only. No store pays to appear here.</p>
      </div>
    </Panel>
  );
}
