"use client";
// Shared price widgets for Find and the assistant: verdict badge, 13-week sparkline, offers table.
import { useState } from "react";
import { fmt } from "@/lib/money";
import { effective, historyFor, offersFor, verdictFor } from "@/lib/prices";
import { Badge, cx } from "./ui";

export function VerdictBadge({ sku, big }: { sku: string; big?: boolean }) {
  const v = verdictFor(sku);
  return (
    <Badge tone={v.tone === "neutral" ? "neutral" : v.tone} className={big ? "px-2 text-[12.5px] leading-[22px]" : undefined}>
      {v.label === "Good price" ? "✓ " : v.label === "Wait" ? "◷ " : v.label === "Skip it" ? "✕ " : ""}
      {v.label}
    </Badge>
  );
}

/** 13 weekly prices; dot = today; hairline = median. Hover shows the week's price. */
export function Sparkline({ sku, w = 220, h = 54 }: { sku: string; w?: number; h?: number }) {
  const pts = historyFor(sku);
  const [hover, setHover] = useState<number | null>(null);
  if (!pts.length) return null;
  const lo = Math.min(...pts) * 0.97,
    hi = Math.max(...pts) * 1.03;
  const x = (i: number) => 4 + (i / (pts.length - 1)) * (w - 8);
  const y = (v: number) => 6 + (1 - (v - lo) / (hi - lo || 1)) * (h - 14);
  const med = [...pts].sort((a, b) => a - b)[Math.floor(pts.length / 2)];
  const d = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const shown = hover ?? pts.length - 1;
  return (
    <div className="relative inline-block">
      <svg width={w} height={h} role="img" aria-label="Price over the last 13 weeks" onMouseLeave={() => setHover(null)}>
        <line x1={4} x2={w - 4} y1={y(med)} y2={y(med)} stroke="var(--color-line-strong)" strokeDasharray="3 3" strokeWidth={1} />
        <path d={d} fill="none" stroke="var(--color-ink)" strokeWidth={2} strokeLinejoin="round" />
        <circle cx={x(shown)} cy={y(pts[shown])} r={4.5} fill={hover === null ? "var(--color-tomato)" : "var(--color-ink)"} stroke="var(--color-card)" strokeWidth={2} />
        {pts.map((_, i) => (
          <rect key={i} x={x(i) - (w - 8) / pts.length / 2} y={0} width={(w - 8) / pts.length} height={h} fill="transparent" onMouseEnter={() => setHover(i)} />
        ))}
      </svg>
      <div className="flex justify-between font-mono text-[10.5px] text-muted">
        <span>{hover === null ? "13 wks ago" : `${12 - hover} wk${12 - hover === 1 ? "" : "s"} ago`}</span>
        <span className="text-ink">{fmt(pts[shown])}</span>
      </div>
    </div>
  );
}

export function OffersTable({ sku, compact }: { sku: string; compact?: boolean }) {
  const offers = offersFor(sku);
  const best = offers.find((o) => o.in_stock);
  return (
    <table className="w-full text-[13px]">
      <tbody>
        {offers.map((o) => (
          <tr key={o.retailer_id} className={cx("border-b border-line last:border-0", !o.in_stock && "text-muted")}>
            <td className={cx(compact ? "py-1.5" : "py-2.5", "pr-2")}>
              <div className="flex items-center gap-2">
                <span className={cx("font-medium", o === best && "text-basil")}>{o.retailer}</span>
                {o === best && <Badge tone="basil">best</Badge>}
                {o.kind === "resale" && <Badge tone="plum">open box</Badge>}
                {o.note && !compact && <Badge tone="turmeric">deal</Badge>}
              </div>
              {!compact && (
                <div className="text-[12px] text-muted">
                  {o.in_stock ? o.eta : "out of stock"}
                  {o.fee_note ? ` · ${o.fee_note}` : ""}
                  {o.condition ? ` · ${o.condition}` : ""}
                  {o.note ? ` · ${o.note}` : ""}
                </div>
              )}
            </td>
            <td className="num text-right whitespace-nowrap">
              {fmt(effective(o))}
              {o.pack > 1 && <span className="text-[11px] text-muted"> /ea</span>}
              {(o.fee_cents > 0 || o.pack > 1) && !compact && (
                <div className="text-[11px] text-muted">
                  {o.pack > 1 ? `${fmt(o.price_cents)} for ${o.pack}` : `${fmt(o.price_cents)} + ${fmt(o.fee_cents)}`}
                </div>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
