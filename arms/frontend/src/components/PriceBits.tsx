"use client";
// Shared price widgets for Find and Ask Picky: verdict chip, 13-week price line, and the store comparison list.
import { Package, Store, Truck, Warehouse, RefreshCcw } from "lucide-react";
import { motion } from "motion/react";
import { useState } from "react";
import { fmt } from "@/lib/money";
import { effective, historyFor, offersFor, verdictFor, type Offer } from "@/lib/prices";
import { cn } from "@/lib/utils";
import { VerdictChip } from "./picky";

export function VerdictBadge({ sku, big }: { sku: string; big?: boolean }) {
  return <VerdictChip verdict={verdictFor(sku)} size={big ? "lg" : "sm"} />;
}

/** 13 weekly prices. Dot = today; dashed line = typical (median). Hover any week to read its price. */
export function Sparkline({ sku, w = 240, h = 64, className }: { sku: string; w?: number; h?: number; className?: string }) {
  const pts = historyFor(sku);
  const [hover, setHover] = useState<number | null>(null);
  if (!pts.length) return null;
  const lo = Math.min(...pts) * 0.96,
    hi = Math.max(...pts) * 1.04;
  const x = (i: number) => 6 + (i / (pts.length - 1)) * (w - 12);
  const y = (v: number) => 8 + (1 - (v - lo) / (hi - lo || 1)) * (h - 16);
  const med = [...pts].sort((a, b) => a - b)[Math.floor(pts.length / 2)];
  const line = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `${line} L${x(pts.length - 1)},${h} L${x(0)},${h} Z`;
  const shown = hover ?? pts.length - 1;
  return (
    <figure className={cn("inline-block", className)}>
      <svg width={w} height={h} role="img" aria-label={`Price over the last 13 weeks, now ${fmt(pts[pts.length - 1])}`} onMouseLeave={() => setHover(null)}>
        <motion.path d={area} fill="var(--color-leaf-wash)" initial={{ opacity: 0 }} animate={{ opacity: 1 }} transition={{ delay: 0.5, duration: 0.4 }} />
        <line x1={6} x2={w - 6} y1={y(med)} y2={y(med)} stroke="var(--input)" strokeDasharray="4 4" strokeWidth={1.5} />
        <motion.path
          d={line}
          fill="none"
          stroke="var(--color-leaf)"
          strokeWidth={2.5}
          strokeLinejoin="round"
          strokeLinecap="round"
          initial={{ pathLength: 0 }}
          animate={{ pathLength: 1 }}
          transition={{ duration: 0.9, ease: "easeInOut" }}
        />
        <motion.circle animate={{ cx: x(shown), cy: y(pts[shown]) }} transition={{ type: "spring", stiffness: 500, damping: 35 }} r={5} fill={hover === null ? "var(--color-leaf)" : "var(--foreground)"} stroke="white" strokeWidth={2.5} />
        {pts.map((_, i) => (
          <rect key={i} x={x(i) - (w - 12) / pts.length / 2} y={0} width={(w - 12) / pts.length} height={h} fill="transparent" onMouseEnter={() => setHover(i)} />
        ))}
      </svg>
      <figcaption className="mt-1 flex justify-between text-[12px] text-muted-foreground">
        <span>{hover === null ? "13 weeks ago" : hover === pts.length - 1 ? "This week" : `${pts.length - 1 - hover} weeks ago`}</span>
        <span className="tabular font-semibold text-foreground">{fmt(pts[shown])}</span>
      </figcaption>
    </figure>
  );
}

const KIND_ICON = { store: Store, carrier: Store, online: Truck, delivery: Truck, warehouse: Warehouse, resale: RefreshCcw } as const;

function OfferRow({ o, best, compact }: { o: Offer; best: boolean; compact?: boolean }) {
  const Icon = KIND_ICON[o.kind] ?? Package;
  const extra = [o.in_stock ? o.eta : "Out of stock", o.fee_note, o.condition, o.note].filter(Boolean).join(", ");
  return (
    <motion.li
      layout
      variants={{ hidden: { opacity: 0, x: -10 }, show: { opacity: o.in_stock ? 1 : 0.55, x: 0 } }}
      className={cn("flex items-center gap-3 rounded-xl px-3", compact ? "py-2" : "py-3", best && "bg-lime/45")}
    >
      <span className={cn("grid size-9 shrink-0 place-items-center rounded-lg", best ? "bg-white text-leaf" : "bg-muted text-muted-foreground")}>
        <Icon className="size-[18px]" aria-hidden />
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="truncate font-semibold">{o.retailer}</span>
          {best && <span className="rounded-full bg-leaf px-2 py-px text-[11px] font-bold text-white">Best</span>}
        </div>
        {!compact && <div className="truncate text-[12.5px] text-muted-foreground">{extra}</div>}
      </div>
      <div className="text-right">
        <div className="tabular font-heading text-[17px] font-semibold">{fmt(effective(o))}</div>
        {(o.pack > 1 || o.fee_cents > 0) && (
          <div className="tabular text-[11.5px] text-muted-foreground">{o.pack > 1 ? `${fmt(o.price_cents)} for ${o.pack}` : `incl. ${fmt(o.fee_cents)} fee`}</div>
        )}
      </div>
    </motion.li>
  );
}

export function OffersTable({ sku, compact, limit }: { sku: string; compact?: boolean; limit?: number }) {
  const offers = offersFor(sku);
  const best = offers.find((o) => o.in_stock);
  return (
    <motion.ul className="flex flex-col gap-1" initial="hidden" animate="show" variants={{ hidden: {}, show: { transition: { staggerChildren: 0.05 } } }}>
      {offers.slice(0, limit ?? offers.length).map((o) => (
        <OfferRow key={o.retailer_id} o={o} best={o === best} compact={compact} />
      ))}
    </motion.ul>
  );
}
