"use client";
// Home "Running low": restock suggestions from purchase cadence (GET /api/users/maya/suggestions, or the demo history).
// Tap to pick (multi-select), then one button orders exactly those SKUs: POST /api/requests with `items` in gateway
// mode → the projector page (/watch) for the new trip; the in-browser replay of the same order otherwise.
import { Bot, Check, ChevronRight, History, Loader2, Sparkles } from "lucide-react";
import Link from "next/link";
import { motion } from "motion/react";
import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import { itemVariants, listVariants } from "@/components/motion";
import { Money, Panel, ProductTile } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { STORES, productBySku } from "@/lib/data";
import { gatewayHref } from "@/lib/gateway/client";
import { startGatewayOrder } from "@/lib/gateway/tripSource";
import { useGateway } from "@/lib/gateway/useGateway";
import { fmt } from "@/lib/money";
import { restockDraft } from "@/lib/order";
import { boughtAgo, kindBadge, qtyOf, signalChips, signalSource, useSuggestions, whereText, whyText, type Suggestion } from "@/lib/suggestions";
import { useTrip } from "@/lib/trip";
import { usePurchases } from "@/lib/usePurchases";
import { cn } from "@/lib/utils";

const STORE_ID = "store_ave";

export function Suggestions() {
  const { purchases } = usePurchases();
  const { items, source, loading } = useSuggestions(purchases);
  const [picked, setPicked] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const router = useRouter();
  const placeOrder = useTrip((s) => s.placeOrder);

  const chosen = useMemo(() => items.filter((s) => picked.includes(s.sku)), [items, picked]);
  const total = chosen.reduce((s, x) => s + x.price_cents * qtyOf(x), 0);
  const toggle = (sku: string) => setPicked((p) => (p.includes(sku) ? p.filter((x) => x !== sku) : [...p, sku]));

  const order = async () => {
    if (!chosen.length || sending) return;
    const lines = chosen.map((s) => ({ sku: s.sku, qty: qtyOf(s), note: s.reason }));
    const runDemo = () => {
      const draft = restockDraft(lines, STORE_ID);
      if (!draft.lines.length) return void toast.error("None of those are in the demo store's catalog");
      placeOrder(draft, draft.recs.map((r) => r.rec_id), "pickup");
      router.push("/watch");
    };
    const live = useGateway.getState().status === "live" || (await useGateway.getState().check());
    if (!live) return runDemo();
    setSending(true);
    const text = `Restock: ${chosen.map((s) => `${qtyOf(s)} ${s.name}`).join(", ")}`;
    const r = await startGatewayOrder(useTrip, { text, storeId: STORE_ID, mode: "pickup", title: "Restock", items: lines, page: "/watch" });
    setSending(false);
    if (r.kind === "following") {
      toast(r.reply);
      router.push(r.url);
    } else if (r.kind === "unreachable") {
      toast("The gateway didn't answer, so this is the demo trip", { description: r.reply });
      runDemo();
    } else toast.error(r.reply);
  };

  return (
    <Panel className="p-4 md:p-6" aria-labelledby="running-low">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h2 id="running-low" className="text-[22px] leading-tight font-bold md:text-[26px]">
            Running low
          </h2>
          <p className="mt-1 text-[14.5px] text-muted-foreground">
            From how often you buy them. Tap what you need and a robot picks it up at {STORES[STORE_ID].short}.
          </p>
        </div>
        <div className="flex items-center gap-2">
          {!loading && <Badge variant={source === "demo" ? "muted" : "leaf"}>{source === "demo" ? "Demo history" : "From your purchase history"}</Badge>}
          <Button asChild variant="ghost" size="sm">
            <Link href="/purchases#history">
              <History /> Purchase history <ChevronRight />
            </Link>
          </Button>
        </div>
      </div>

      {loading ? (
        <div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" aria-hidden>
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="h-[300px] rounded-2xl" />
          ))}
        </div>
      ) : (
        <motion.ul className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6" variants={listVariants} initial="hidden" animate="show">
          {items.map((s) => (
            <SuggestionCard key={s.sku} s={s} on={picked.includes(s.sku)} onToggle={() => toggle(s.sku)} />
          ))}
        </motion.ul>
      )}

      <div
        className={cn(
          "z-10 mt-4 flex flex-wrap items-center justify-between gap-3 rounded-2xl bg-card/95 p-3 ring-1 ring-foreground/[0.08] backdrop-blur md:pl-5",
          // once something is picked the button follows the scroll, so it's always one tap away
          chosen.length > 0 && "sticky bottom-20 md:bottom-5",
        )}
      >
        <p className="text-[14px] text-muted-foreground" aria-live="polite">
          {chosen.length
            ? `${chosen.length} picked. The robot shops them, asks before any extras, and you approve the total.`
            : "Nothing picked yet. Checkout always waits for your approval."}
        </p>
        <Button size="lg" className="h-14 px-7 text-[17px] font-semibold" disabled={!chosen.length || sending} onClick={order}>
          {sending ? <Loader2 className="animate-spin" /> : <Bot />}
          {chosen.length ? `Order these (${chosen.length}) — ${fmt(total)}` : "Pick items to order"}
        </Button>
      </div>
    </Panel>
  );
}

function SuggestionCard({ s, on, onToggle }: { s: Suggestion; on: boolean; onToggle: () => void }) {
  const p = productBySku[s.sku];
  const [broken, setBroken] = useState(false);
  const overdue = s.last_bought_days != null && s.cadence_days != null && s.last_bought_days >= s.cadence_days;
  const where = whereText(s);
  const color = p?.label_color ?? s.label_color ?? "#e6e0d2";
  const qty = qtyOf(s);
  const why = whyText(s);
  const chips = signalChips(s);
  const from = signalSource(s);
  const badge = kindBadge(s);
  const ai = s.ranked_by === "llm";
  return (
    <motion.li variants={itemVariants}>
      <button
        type="button"
        onClick={onToggle}
        aria-pressed={on}
        aria-label={`${s.name}, ${fmt(s.price_cents)}. ${boughtAgo(s.last_bought_days)}.`}
        className={cn(
          "group relative flex h-full w-full cursor-pointer flex-col rounded-2xl bg-card p-2.5 text-left ring-1 transition-[box-shadow,transform] hover:-translate-y-0.5 focus-visible:ring-[3px] focus-visible:ring-ring focus-visible:outline-none",
          on ? "ring-[3px] ring-leaf" : "ring-foreground/[0.08] hover:ring-foreground/20",
        )}
      >
        <span className="relative grid aspect-[4/3] place-items-center overflow-hidden rounded-xl" style={{ background: `${color}2e` }}>
          {s.image_url && !broken ? (
            // eslint-disable-next-line @next/next/no-img-element
            <img src={gatewayHref(s.image_url)} alt="" onError={() => setBroken(true)} className="h-full w-full object-contain p-2" loading="lazy" />
          ) : (
            <ProductTile color={color} category={p?.category ?? s.category ?? "pantry"} name={s.name} size={60} />
          )}
          <span
            className={cn(
              "absolute top-2 right-2 grid size-7 place-items-center rounded-full ring-2 transition-colors",
              on ? "bg-leaf text-white ring-white" : "bg-white/85 text-transparent ring-foreground/10",
            )}
            aria-hidden
          >
            <Check className={cn("size-4", on && "animate-pop")} strokeWidth={3} />
          </span>
          <span className="absolute right-2 bottom-2 left-2 flex flex-wrap gap-1">
            {badge && (
              <Badge variant={badge.variant} className="max-w-full truncate">
                {badge.text}
              </Badge>
            )}
            {!s.in_stock && <Badge variant="grape">Online</Badge>}
            {s.sponsored && <Badge variant="amber">Sponsored</Badge>}
          </span>
        </span>
        <span className="mt-2.5 line-clamp-2 min-h-[2.5em] px-0.5 text-[14.5px] leading-tight font-semibold">{s.name}</span>
        <span className={cn("mt-1 px-0.5 text-[13.5px] leading-snug font-semibold", overdue ? "text-tomato" : "text-foreground")}>{boughtAgo(s.last_bought_days)}</span>
        {why && (
          <span
            className="mt-1.5 flex gap-1.5 rounded-lg bg-muted/70 px-2 py-1.5 text-[12.5px] leading-snug"
            title={`${ai ? "Written by Picky's AI from these signals only" : "Why Picky suggests it"}${from ? `, from your ${from}` : ""}`}
          >
            <Sparkles className="mt-0.5 size-3.5 shrink-0 text-leaf" aria-hidden />
            <span className="line-clamp-3">
              <span className="sr-only">Why: </span>
              {ai && <span className="mr-1 font-semibold text-leaf-deep">AI:</span>}
              {why}
            </span>
          </span>
        )}
        {chips.length > 0 && (
          <span className="mt-1.5 flex flex-wrap gap-1" aria-label="Signals">
            {chips.map((c) => (
              <span key={c.title} title={c.title} className={cn("rounded-full px-2 py-0.5 text-[11.5px] font-medium", c.tone === "due" ? "bg-tomato-wash text-tomato" : "bg-sky-wash text-sky")}>
                {c.text}
              </span>
            ))}
          </span>
        )}
        {where && <span className="mt-1 px-0.5 text-[12px] text-muted-foreground">In {where}</span>}
        <span className="mt-auto flex items-baseline justify-between px-0.5 pt-2">
          <Money cents={s.price_cents} className="font-heading text-[18px] font-bold" />
          {qty > 1 && <span className="text-[12.5px] text-muted-foreground">× {qty}</span>}
        </span>
      </button>
    </motion.li>
  );
}
