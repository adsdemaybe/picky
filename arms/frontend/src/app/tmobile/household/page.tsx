"use client";
// Household Mix: optimize the whole household, not one line. Every way to put each line on a plan is priced (shared
// accounts get multi-line pricing), lines that don't fit their plan are ruled out, streaming perks are counted, and
// the cheapest mix is compared with what they pay today.
import { CheckCircle2, CircleAlert, Tv, Users } from "lucide-react";
import { useMemo, useState } from "react";
import { Panel } from "@/components/picky";
import { household, lineFit, money, money0, planOf, DATA } from "@/lib/directions";
import { cn } from "@/lib/utils";
import { AdvisorHeader } from "../advisors/nav";

export default function HouseholdMix() {
  const all = useMemo(() => DATA.households.map(household), []);
  const [id, setId] = useState(all[0].h.id);
  const r = all.find((x) => x.h.id === id)!;
  const saves = r.saves_cents > 0;
  return (
    <div className="flex flex-col gap-6 pb-16">
      <AdvisorHeader
        kicker="Household Mix"
        title="Fix the whole family, not one line"
        sub="Every line's usage, every plan, every way to group them: the optimizer prices each combination, rules out any where someone's usage doesn't fit, counts the streaming the household already pays for, and shows the cheapest setup next to what they pay today."
      />
      <div className="flex flex-wrap gap-2" role="tablist" aria-label="Household">
        {all.map((x) => (
          <button key={x.h.id} role="tab" aria-selected={x.h.id === id} onClick={() => setId(x.h.id)} className={cn("flex cursor-pointer items-center gap-2 rounded-full px-3.5 py-2 text-[13.5px] font-semibold ring-1", x.h.id === id ? "bg-foreground text-background ring-foreground" : "bg-card ring-foreground/10 hover:bg-muted")}>
            <Users className="size-4" aria-hidden />
            {x.h.name}
            <span className={cn("rounded-full px-2 py-0.5 text-[11px]", x.saves_cents > 0 ? "bg-leaf-wash text-leaf-deep" : "bg-amber-wash text-amber-ink")}>{x.saves_cents > 0 ? `−${money0(x.saves_cents)}/mo` : `+${money0(-x.saves_cents)}/mo`}</span>
          </button>
        ))}
      </div>

      <div className="grid gap-4 md:grid-cols-[1fr_auto_1fr] md:items-stretch">
        <Panel className="p-5">
          <div className="text-[12.5px] font-semibold text-muted-foreground uppercase">Today</div>
          <div className="mt-1 text-[16px] font-semibold">{r.current_label}</div>
          <div className="mt-2 text-[32px] leading-none font-bold tabular">{money(r.current_total_cents)}</div>
          <div className="text-[12.5px] text-muted-foreground">
            per month
            {r.current_streaming_cents
              ? ` (${money(r.current_service_cents)} service + ${money(r.current_streaming_cents)} streaming they pay separately)`
              : r.h.streaming.length
                ? `, including their ${r.h.streaming.length > 1 ? `${r.h.streaming.length} streaming services` : "streaming service"} as plan perks`
                : ""}
          </div>
          <p className="mt-2 text-[13px] text-muted-foreground">{r.h.note}</p>
          {r.current_misfits.length > 0 && (
            <p className="mt-2 flex gap-1.5 rounded-lg bg-tomato-wash px-2.5 py-1.5 text-[12.5px] text-tomato">
              <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden />
              Doesn&apos;t fit today: {r.current_misfits.join("; ")}
            </p>
          )}
        </Panel>
        <div className="grid place-items-center text-[28px] text-muted-foreground" aria-hidden>
          →
        </div>
        <Panel className={cn("p-5 ring-2", saves ? "ring-leaf" : "ring-amber")}>
          <div className={cn("text-[12.5px] font-semibold uppercase", saves ? "text-leaf-deep" : "text-amber-ink")}>{saves ? "Best fit for everyone" : "Cheapest setup that actually fits"}</div>
          <div className="mt-1 text-[16px] font-semibold">{r.best.summary}</div>
          <div className="mt-2 text-[32px] leading-none font-bold tabular">{money(r.best.monthly_cents)}</div>
          <div className="text-[12.5px] text-muted-foreground">
            per month{r.best.streaming_paid_cents ? ` (${money(r.best.service_cents)} service + ${money(r.best.streaming_paid_cents)} streaming)` : ""}
          </div>
          <div className={cn("mt-2 rounded-lg px-2.5 py-1.5 text-[14px] font-semibold", saves ? "bg-leaf-wash text-leaf-deep" : "bg-amber-wash text-amber-ink")}>
            {saves ? `Saves ${money(r.saves_cents)}/mo · ${money0(r.saves_cents * 12)} a year` : `Costs ${money(-r.saves_cents)}/mo more, and stops the slowdowns. Cheaper plans don't fit this usage.`}
          </div>
        </Panel>
      </div>

      <Panel className="overflow-x-auto p-5">
        <h3 className="text-[16px] font-semibold">Line by line</h3>
        <table className="mt-2 w-full min-w-[620px] text-left text-[13.5px]">
          <thead className="text-[12px] text-muted-foreground">
            <tr className="border-b border-border">
              <th className="py-2 font-medium">Who</th>
              <th className="py-2 font-medium">Uses / month</th>
              <th className="py-2 font-medium">Today</th>
              <th className="py-2 font-medium">Recommended</th>
              <th className="py-2 font-medium">Fit</th>
            </tr>
          </thead>
          <tbody>
            {r.lines.map((l) => {
              const p = planOf(r.best.assign[l.line_id])!;
              const f = lineFit(p, l);
              const cur = planOf(l.current_plan_id);
              return (
                <tr key={l.line_id} className="border-b border-border last:border-0">
                  <td className="py-2">
                    <div className="font-semibold">{l.label}</div>
                    <div className="text-[12px] text-muted-foreground">{l.device_name}</div>
                  </td>
                  <td className="py-2 tabular">
                    {l.usage_gb_month} GB{l.hotspot_gb_month ? ` · ${l.hotspot_gb_month} GB hotspot` : ""}
                  </td>
                  <td className="py-2">{cur ? cur.name : "Other carrier"}</td>
                  <td className="py-2 font-semibold">{p.name}</td>
                  <td className="py-2">
                    <span className={cn("inline-flex items-center gap-1", f.ok ? "text-leaf-deep" : "text-tomato")}>
                      <CheckCircle2 className="size-4" aria-hidden /> {f.why}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {r.h.streaming.length > 0 && (
          <p className="mt-3 flex items-start gap-2 text-[13px] text-muted-foreground">
            <Tv className="mt-0.5 size-4 shrink-0" aria-hidden />
            Streaming they pay for: {r.h.streaming.map((s) => `${s.name} ${money(s.monthly_cents)}`).join(", ")}.{" "}
            {r.best.streaming_covered.length ? `Covered by the new plan's perks: ${r.best.streaming_covered.join(", ")}.` : "The cheapest setup has no streaming perk, so that's counted as paid separately, and it's still cheaper."}
          </p>
        )}
      </Panel>

      <Panel className="p-5">
        <h3 className="text-[16px] font-semibold">Other setups it considered</h3>
        <p className="text-[12.5px] text-muted-foreground">{r.combos_checked} combinations priced where every line fits; the next best:</p>
        <ul className="mt-2 divide-y divide-border">
          {r.alternatives.map((alt) => (
            <li key={alt.summary} className="flex items-center justify-between gap-3 py-2 text-[14px]">
              <span>{alt.summary}</span>
              <span className="font-semibold tabular">
                {money(alt.monthly_cents)} <span className="text-[12px] font-normal text-muted-foreground">(+{money(alt.monthly_cents - r.best.monthly_cents)})</span>
              </span>
            </li>
          ))}
          {!r.alternatives.length && <li className="py-2 text-[14px] text-muted-foreground">Only one plan fits this usage.</li>}
        </ul>
      </Panel>
      <p className="text-[11.5px] text-muted-foreground">Prices with AutoPay, before taxes. A line fits if its hotspot use is within the plan&apos;s allowance and heavy users (50 GB+) get enough priority data. Plans, prices, usage and streaming costs are synthetic demo data.</p>
    </div>
  );
}
