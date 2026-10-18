"use client";
// Weekly spend, stacked by category. Fixed category order/colors (CATS), 2px surface gaps between segments,
// rounded top data-end, recessive grid, per-bar hover tooltip, legend + table view.
import { useState } from "react";
import { CATS, type CatId } from "@/lib/data";
import { fmt, fmtShort } from "@/lib/money";
import { cn as cx } from "@/lib/utils";

function Swatch({ color }: { color: string }) {
  return <span className="inline-block size-3 shrink-0 rounded-[4px]" style={{ background: color }} aria-hidden />;
}

type Row = { week: Date; total: number; byCat: Record<CatId, number> };

export function SpendChart({ rows, budgetPerWeek }: { rows: Row[]; budgetPerWeek: number }) {
  const [hover, setHover] = useState<number | null>(null);
  const [table, setTable] = useState(false);
  const W = 760,
    H = 250,
    padL = 44,
    padR = 12,
    padT = 12,
    padB = 26;
  const max = Math.max(budgetPerWeek * 1.15, ...rows.map((r) => r.total));
  const step = max > 8000 ? 2500 : 2000;
  const top = Math.ceil(max / step) * step;
  const y = (c: number) => padT + (H - padT - padB) * (1 - c / top);
  const bw = (W - padL - padR) / rows.length;
  const barW = Math.min(34, bw * 0.6);
  const ticks = Array.from({ length: top / step + 1 }, (_, i) => i * step);
  const wk = (d: Date) => d.toLocaleDateString("en-US", { month: "short", day: "numeric" });

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-x-4 gap-y-1">
        {CATS.map((c) => (
          <span key={c.id} className="flex items-center gap-1.5 text-[13px]">
            <Swatch color={c.color} />
            {c.label}
          </span>
        ))}
        <span className="flex items-center gap-1.5 text-[13px]">
          <span className="inline-block w-4 border-t-2 border-dashed border-foreground" /> Weekly budget {fmtShort(budgetPerWeek)}
        </span>
        <button className="ml-auto cursor-pointer rounded-full bg-muted px-3 py-1.5 text-[13px] font-medium hover:bg-secondary" onClick={() => setTable((t) => !t)}>
          {table ? "Show chart" : "Show as table"}
        </button>
      </div>

      {table ? (
        <div className="overflow-x-auto">
          <table className="w-full text-[13px]">
            <thead>
              <tr className="border-b border-border text-left text-[12.5px] text-muted-foreground">
                <th className="py-1.5 pr-3 font-normal">Week of</th>
                {CATS.map((c) => (
                  <th key={c.id} className="px-2 py-1.5 text-right font-normal">
                    {c.label}
                  </th>
                ))}
                <th className="py-1.5 pl-2 text-right font-normal">Total</th>
              </tr>
            </thead>
            <tbody className="tabular">
              {rows.map((r) => (
                <tr key={r.week.toISOString()} className="border-b border-border/60">
                  <td className="py-1.5 pr-3 font-sans">{wk(r.week)}</td>
                  {CATS.map((c) => (
                    <td key={c.id} className="px-2 text-right text-muted-foreground">
                      {r.byCat[c.id] ? fmt(r.byCat[c.id]) : "–"}
                    </td>
                  ))}
                  <td className="pl-2 text-right font-medium">{fmt(r.total)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="relative">
          <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label="Weekly grocery spend by category, last 12 weeks">
            {ticks.map((t) => (
              <g key={t}>
                <line x1={padL} x2={W - padR} y1={y(t)} y2={y(t)} stroke="var(--border)" strokeWidth={t === 0 ? 1 : 0.6} />
                <text x={padL - 8} y={y(t) + 3.5} textAnchor="end" className="tabular" fontSize={10.5} fill="var(--muted-foreground)">
                  {fmtShort(t)}
                </text>
              </g>
            ))}
            <line
              x1={padL}
              x2={W - padR}
              y1={y(budgetPerWeek)}
              y2={y(budgetPerWeek)}
              stroke="var(--foreground)"
              strokeDasharray="4 3"
              strokeWidth={1}
            />
            {rows.map((r, i) => {
              const cx0 = padL + bw * i + (bw - barW) / 2;
              let acc = 0;
              const segs = CATS.filter((c) => r.byCat[c.id] > 0);
              return (
                <g key={i} opacity={hover === null || hover === i ? 1 : 0.45}>
                  {segs.map((c, k) => {
                    const v = r.byCat[c.id];
                    const y1 = y(acc + v);
                    const y0 = y(acc);
                    acc += v;
                    const h = Math.max(0, y0 - y1 - (k > 0 ? 2 : 0)); // 2px surface gap
                    const isTop = k === segs.length - 1;
                    const rr = isTop ? Math.min(6, h) : 0;
                    const x = cx0,
                      yy = y1,
                      w = barW;
                    const d = `M${x},${yy + h} L${x},${yy + rr} Q${x},${yy} ${x + rr},${yy} L${x + w - rr},${yy} Q${x + w},${yy} ${x + w},${yy + rr} L${x + w},${yy + h} Z`;
                    return <path key={c.id} d={d} fill={c.color} />;
                  })}
                  <text x={cx0 + barW / 2} y={H - 8} textAnchor="middle" fontSize={10.5} fill="var(--muted-foreground)" className="tabular">
                    {i % 2 === rows.length % 2 ? wk(r.week) : ""}
                  </text>
                  <rect
                    x={padL + bw * i}
                    y={padT}
                    width={bw}
                    height={H - padT - padB}
                    fill="transparent"
                    onMouseEnter={() => setHover(i)}
                    onMouseLeave={() => setHover(null)}
                  />
                </g>
              );
            })}
          </svg>
          {hover !== null && (
            <div
              className={cx("pointer-events-none absolute top-2 z-10 w-[210px] rounded-xl bg-card px-3 py-2.5 text-[13px] shadow-lg ring-1 ring-foreground/10")}
              style={{ left: `clamp(0px, calc(${((padL + bw * hover + bw / 2) / W) * 100}% - 100px), calc(100% - 200px))` }}
            >
              <div className="mb-1 flex justify-between font-semibold">
                <span>Week of {wk(rows[hover].week)}</span>
                <span className="tabular">{fmt(rows[hover].total)}</span>
              </div>
              {CATS.filter((c) => rows[hover].byCat[c.id] > 0).map((c) => (
                <div key={c.id} className="flex items-center justify-between gap-2 text-muted-foreground">
                  <span className="flex items-center gap-1.5">
                    <Swatch color={c.color} />
                    {c.label}
                  </span>
                  <span className="tabular">{fmt(rows[hover].byCat[c.id])}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
