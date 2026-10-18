"use client";
import { Ban, Check, ChevronDown, Clock, Hourglass, Plus, Repeat, ShieldCheck, ShoppingBag, Smartphone, TriangleAlert, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useState } from "react";
import { ProductTile } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { productBySku } from "@/lib/data";
import { fmt } from "@/lib/money";
import { hydrateTmoCart, useTmoCart } from "@/lib/tmoCart";
import { useTrip } from "@/lib/trip";
import { cn } from "@/lib/utils";
import type { Metrics, Suggestion } from "@/lib/whisperer";

const NAV = [
  { href: "/tmobile", label: "My line", match: (p: string) => p === "/tmobile" },
  { href: "/tmobile/shop", label: "Shop online", match: (p: string) => p.startsWith("/tmobile/shop") },
  { href: "/tmobile/store", label: "In store", match: (p: string) => p.startsWith("/tmobile/store") },
  { href: "/tmobile/trip", label: "Robot trip", match: (p: string) => p.startsWith("/tmobile/trip") },
  { href: "/tmobile/track", label: "How it works", match: (p: string) => p.startsWith("/tmobile/track") },
];

export function TmoHeader() {
  const path = usePathname();
  const phase = useTrip((s) => s.phase);
  const live = phase === "shopping" || phase === "checkout" || phase === "waiting_login";
  const count = useTmoCart((s) => s.cart.reduce((n, l) => n + l.qty, 0));
  useEffect(() => void hydrateTmoCart(), []);
  return (
    <header className="sticky top-0 z-30 text-white">
      {/* full-bleed color past the page's max width, without widening the layout */}
      <div className="bg-magenta shadow-[0_0_0_100vmax_var(--color-magenta)] [clip-path:inset(0_-100vmax)]">
        <div className="mx-auto flex max-w-[1240px] flex-wrap items-center gap-x-4 px-4 md:h-16 md:flex-nowrap md:px-8">
          <Link href="/tmobile" className="flex h-14 items-center gap-2.5 md:h-auto" aria-label="Picky at T-Mobile home">
            <span className="grid size-9 place-items-center rounded-xl bg-white text-magenta">
              <Smartphone className="size-5" strokeWidth={2.2} aria-hidden />
            </span>
            <span className="leading-none">
              <span className="block font-heading text-[19px] font-bold tracking-tight md:text-[20px]">Cart Whisperer</span>
              <span className="block text-[11.5px] font-medium text-white/80">Picky at T-Mobile, University Way NE</span>
            </span>
          </Link>
          <Link href="/tmobile/shop" className="ml-auto flex h-9 items-center gap-1.5 rounded-full bg-white/15 px-3 text-[13px] font-semibold md:order-last md:ml-0" aria-label={`Basket, ${count} item${count === 1 ? "" : "s"}`}>
            <ShoppingBag className="size-4" aria-hidden />
            <span className="tabular">{count}</span>
          </Link>
          <nav className="-mx-4 flex w-[calc(100%+2rem)] items-center gap-1 overflow-x-auto px-4 pb-2.5 md:mx-0 md:ml-auto md:w-auto md:px-0 md:pb-0" aria-label="T-Mobile">
            {NAV.map((n) => {
              const on = n.match(path);
              return (
                <Link
                  key={n.href}
                  href={n.href}
                  aria-current={on ? "page" : undefined}
                  className={cn("flex h-9 shrink-0 items-center gap-2 rounded-full px-3 text-[13.5px] font-semibold whitespace-nowrap transition-colors", on ? "bg-white text-magenta" : "text-white/85 hover:bg-white/15")}
                >
                  {n.label}
                  {n.href === "/tmobile/trip" && live && <span className="live-dot size-2 rounded-full bg-lime" aria-label="robot is shopping" />}
                </Link>
              );
            })}
            <Link href="/" className="ml-1 hidden shrink-0 rounded-full px-3 py-1.5 text-[12.5px] font-medium text-white/75 ring-1 ring-white/30 hover:bg-white/10 lg:block">
              Grocery app
            </Link>
          </nav>
        </div>
      </div>
      <div className="bg-magenta-deep px-4 py-1 text-center text-[11px] font-medium text-white/85 shadow-[0_0_0_100vmax_var(--color-magenta-deep)] [clip-path:inset(0_-100vmax)] md:text-[11.5px]">
        <span className="md:hidden">Demo · synthetic account · real t-mobile.com prices (Oct 17) · not affiliated</span>
        <span className="hidden md:inline">Demo · synthetic shopper, devices and promo calendar · products and prices from t-mobile.com (Oct 17, 2026) · not affiliated with T-Mobile</span>
      </div>
    </header>
  );
}

/** "2026-11-27" -> "Fri, Nov 27" */
export const niceDate = (iso: string) => new Date(`${iso}T12:00:00-07:00`).toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", timeZone: "America/Los_Angeles" });

const KIND: Record<Suggestion["kind"], { label: string; cls: string; Icon: typeof Ban }> = {
  skip: { label: "Skip it", cls: "bg-tomato-wash text-tomato", Icon: Ban },
  wait: { label: "Worth waiting", cls: "bg-amber-wash text-amber-ink", Icon: Hourglass },
  complete: { label: "Completes your setup", cls: "bg-leaf-wash text-leaf-deep", Icon: Plus },
  deal_match: { label: "Completes it, on sale", cls: "bg-leaf-wash text-leaf-deep", Icon: Plus },
  swap_save: { label: "Same thing, cheaper", cls: "bg-sky-wash text-sky", Icon: Repeat },
  qty_fix: { label: "Quantity check", cls: "bg-sky-wash text-sky", Icon: TriangleAlert },
  remove_warn: { label: "Over budget", cls: "bg-tomato-wash text-tomato", Icon: TriangleAlert },
};

const ACCEPT: Record<Suggestion["action"], string> = {
  add: "Add it",
  swap: "Swap",
  remove: "Remove it",
  set_qty: "Make it 1",
  remind: "Remind me",
  none: "Got it",
};

export function WhisperCard({ s, onAccept, onDecline, compact = false }: { s: Suggestion; onAccept: () => void; onDecline: () => void; compact?: boolean }) {
  const [open, setOpen] = useState(false);
  const k = KIND[s.kind];
  const p = productBySku[s.sku];
  const money = s.savings_cents || s.avoided_cents;
  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, x: -24 }}
      className="rounded-2xl bg-card p-4 ring-1 ring-foreground/[0.08]"
    >
      <div className="flex items-start gap-3">
        {p && !compact && <ProductTile color={p.label_color} category={p.category} name={p.name} size={44} />}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className={cn("inline-flex h-6 items-center gap-1 rounded-full px-2.5 text-[12px] font-semibold", k.cls)}>
              <k.Icon className="size-3.5" aria-hidden />
              {k.label}
            </span>
            {s.needs_approval && <span className="rounded-full bg-amber-wash px-2 py-0.5 text-[11.5px] font-semibold text-amber-ink">Needs your OK</span>}
            <span className="text-[11.5px] font-medium text-muted-foreground">Not sponsored</span>
          </div>
          <div className="mt-1.5 text-[15.5px] leading-snug font-semibold">{s.title}</div>
          <p className="mt-1 text-[14px] leading-snug text-muted-foreground">{s.reason}</p>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px]">
            {money > 0 && (
              <span className="font-semibold text-leaf-deep tabular">
                {s.kind === "wait" ? "Expected saving " : s.avoided_cents ? "Avoids " : "Saves "}
                {fmt(money)}
              </span>
            )}
            {s.price_delta_cents > 0 && <span className="font-semibold tabular">+{fmt(s.price_delta_cents)}</span>}
            {s.action === "add" && <span className="text-muted-foreground">{s.detour_m === 0 ? "+0 m, on your route" : `+${s.detour_m} m detour`}</span>}
            {s.remind_on && (
              <span className="inline-flex items-center gap-1 text-muted-foreground">
                <Clock className="size-3.5" aria-hidden /> remind {niceDate(s.remind_on)}
              </span>
            )}
          </div>
        </div>
      </div>
      <button onClick={() => setOpen((o) => !o)} className="mt-3 flex cursor-pointer items-center gap-1 text-[12.5px] font-semibold text-magenta" aria-expanded={open}>
        Why am I seeing this? <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <ul className="mt-2 space-y-1 text-[13px]">
              {s.why.map((w) => (
                <li key={w} className="flex gap-2">
                  <span className="mt-1.5 size-1.5 shrink-0 rounded-full bg-magenta" aria-hidden />
                  {w}
                </li>
              ))}
            </ul>
            <div className="mt-2 flex flex-wrap gap-1">
              {s.reason_codes.map((c) => (
                <code key={c} className="rounded bg-muted px-1.5 py-0.5 text-[11px]">
                  {c}
                </code>
              ))}
              <code className="rounded bg-muted px-1.5 py-0.5 text-[11px]">score {s.score.toFixed(2)}</code>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
      <div className="mt-3 flex gap-2">
        <Button size="sm" onClick={onAccept} className="bg-magenta text-white hover:bg-magenta-deep">
          <Check className="size-4" aria-hidden /> {ACCEPT[s.action]}
        </Button>
        <Button size="sm" variant="outline" onClick={onDecline}>
          <X className="size-4" aria-hidden /> {s.kind === "skip" ? "Buy anyway" : s.kind === "wait" ? "Buy now" : "No thanks"}
        </Button>
      </div>
    </motion.div>
  );
}

export function WhisperList({ cards, onAccept, onDecline, empty = "Nothing worth adding today.", compact }: { cards: Suggestion[]; onAccept: (s: Suggestion) => void; onDecline: (s: Suggestion) => void; empty?: string; compact?: boolean }) {
  return (
    <div className="flex flex-col gap-3">
      <AnimatePresence mode="popLayout">
        {cards.map((s) => (
          <WhisperCard key={s.id} s={s} compact={compact} onAccept={() => onAccept(s)} onDecline={() => onDecline(s)} />
        ))}
      </AnimatePresence>
      {cards.length === 0 && (
        <div className="flex items-center gap-2 rounded-2xl bg-muted/60 p-4 text-[14px] text-muted-foreground">
          <ShieldCheck className="size-5 text-leaf" aria-hidden />
          {empty} Zero cards is a valid answer: Cart Whisperer only speaks up when it helps.
        </div>
      )}
    </div>
  );
}

export function MetricsStrip({ m }: { m: Metrics }) {
  const rate = m.shown ? Math.round((m.accepted / m.shown) * 100) : 0;
  const items = [
    { label: "Accepted", value: `${m.accepted} of ${m.shown}`, note: m.shown ? `${rate}%` : "–" },
    { label: "Saved", value: fmt(m.saved_cents), note: "swaps, qty, drops" },
    { label: "Avoided", value: fmt(m.avoided_cents), note: "skip + wait (est.)" },
    { label: "Added", value: fmt(m.added_cents), note: "basket lift" },
    { label: "Quiet passes", value: `${m.zero_passes} of ${m.passes}`, note: "zero cards shown" },
  ];
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-5">
      {items.map((i) => (
        <div key={i.label} className="rounded-xl bg-card px-3 py-2 ring-1 ring-foreground/[0.07]">
          <div className="text-[11.5px] font-medium text-muted-foreground">{i.label}</div>
          <div className="text-[17px] font-semibold tabular">{i.value}</div>
          <div className="text-[11px] text-muted-foreground">{i.note}</div>
        </div>
      ))}
    </div>
  );
}
