"use client";
// Next Best Moment: a care-team console. Accounts ranked by an explained churn-risk score; for each, the cheapest action
// that fixes the cause (not a blanket discount), the 12-month revenue math, and the message the customer would get.
import { ArrowRight, Ban, CheckCircle2, MessageSquareText, ShieldCheck, Wrench } from "lucide-react";
import { useMemo, useState } from "react";
import { Panel } from "@/components/picky";
import { messageFor, money0, moments, type Moment } from "@/lib/directions";
import { cn } from "@/lib/utils";
import { AdvisorHeader } from "../advisors/nav";

const LEVEL = { high: "bg-tomato text-white", medium: "bg-amber text-amber-ink", low: "bg-leaf-wash text-leaf-deep" } as const;
const KIND = { fix: { label: "Fixes the cause", cls: "bg-leaf-wash text-leaf-deep", Icon: Wrench }, touch: { label: "Attention", cls: "bg-sky-wash text-sky", Icon: MessageSquareText }, discount: { label: "Discount", cls: "bg-amber-wash text-amber-ink", Icon: Ban } } as const;

export default function NextBestMoment() {
  const all = useMemo(() => moments(), []);
  const [sel, setSel] = useState(all[0].acct.account_id);
  const m = all.find((x) => x.acct.account_id === sel)!;
  const discounts = all.flatMap((x) => x.plan).filter((p) => p.kind === "discount").length;
  return (
    <div className="flex flex-col gap-6 pb-16">
      <AdvisorHeader
        kicker="Next Best Moment"
        title="Catch someone before they walk"
        sub="Every account gets an explained risk score. The recommendation is the cheapest action that fixes the reason they'd leave: right-size the plan, fix the slowdown, explain the bill. Discounts are a last resort, and low-risk customers are left alone."
      />
      <div className="grid gap-3 sm:grid-cols-3">
        <Stat label="Accounts scored" value={String(all.length)} />
        <Stat label="High / medium risk" value={`${all.filter((x) => x.level === "high").length} / ${all.filter((x) => x.level === "medium").length}`} />
        <Stat label="Blanket discounts recommended" value={String(discounts)} note="fixes, not bribes" />
      </div>

      <div className="grid gap-5 lg:grid-cols-[340px_minmax(0,1fr)]">
        <Panel className="p-2">
          <ul className="flex flex-col">
            {all.map((x) => (
              <li key={x.acct.account_id}>
                <button onClick={() => setSel(x.acct.account_id)} className={cn("flex w-full cursor-pointer items-center gap-3 rounded-xl p-3 text-left", x.acct.account_id === sel ? "bg-magenta-wash" : "hover:bg-muted")} aria-pressed={x.acct.account_id === sel}>
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-[15px] font-semibold">{x.acct.name}</span>
                      <span className={cn("rounded-full px-2 py-0.5 text-[11px] font-bold uppercase", LEVEL[x.level])}>{x.level}</span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
                      <div className={cn("h-full rounded-full", x.level === "high" ? "bg-tomato" : x.level === "medium" ? "bg-amber" : "bg-leaf")} style={{ width: `${x.risk * 100}%` }} />
                    </div>
                    <div className="mt-1 truncate text-[12px] text-muted-foreground">{x.plan[0]?.label}</div>
                  </div>
                  <span className="text-[18px] font-bold tabular">{Math.round(x.risk * 100)}%</span>
                </button>
              </li>
            ))}
          </ul>
        </Panel>
        <Detail m={m} />
      </div>
      <p className="text-[11.5px] text-muted-foreground">Risk is a transparent weighted score over synthetic signals (support contacts, bill change, promo credits ending, plan fit vs usage, device age, and in-channel competitor proxies like a number-transfer PIN request), adjusted for tenure. Revenue = service price with AutoPay; expected = revenue × 12 × chance they stay.</p>
    </div>
  );
}

function Detail({ m }: { m: Moment }) {
  const msg = messageFor(m);
  const max = Math.max(m.expected_no_action_cents, m.expected_with_plan_cents, 1);
  return (
    <div className="flex flex-col gap-4">
      <Panel className="p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 className="text-[22px] font-bold">{m.acct.name}</h2>
            <div className="text-[13px] text-muted-foreground">
              {Math.floor(m.acct.tenure_months / 12)} yr {m.acct.tenure_months % 12} mo with us · pays {money0(m.revenue_now_cents)}/mo
            </div>
          </div>
          <div className="text-right">
            <div className="text-[34px] leading-none font-bold tabular">{Math.round(m.risk * 100)}%</div>
            <div className="text-[12px] text-muted-foreground">chance they leave this year</div>
          </div>
        </div>
        <p className={cn("mt-3 rounded-xl px-3 py-2 text-[14.5px] font-medium", m.level === "low" ? "bg-leaf-wash text-leaf-deep" : "bg-magenta-wash text-magenta-deep")}>{m.verdict}</p>

        <h3 className="mt-4 text-[14px] font-semibold">Why: the signals, weighted</h3>
        {m.drivers.length === 0 ? (
          <p className="mt-1 flex items-center gap-2 text-[14px] text-muted-foreground">
            <ShieldCheck className="size-4 text-leaf" aria-hidden /> No risk signals: no support calls, no bill change, nothing in-channel that looks like shopping around.
          </p>
        ) : (
          <ul className="mt-2 flex flex-col gap-2">
            {m.drivers.map((d) => (
              <li key={d.id} className="grid grid-cols-[1fr_auto] items-center gap-x-3 gap-y-1 text-[13.5px]">
                <div>
                  <span className="font-semibold">{d.id.replace(/_/g, " ")}</span>
                  <span className={cn("ml-2 rounded px-1.5 py-0.5 text-[11px] font-semibold", d.root ? "bg-foreground text-background" : "bg-muted text-muted-foreground")}>{d.root ? "cause" : "symptom"}</span>
                  <div className="text-muted-foreground">{d.text}</div>
                  {d.fixed_by && (
                    <div className="flex items-center gap-1 text-[12.5px] text-leaf-deep">
                      <CheckCircle2 className="size-3.5" aria-hidden /> addressed by {d.fixed_by.charAt(0).toLowerCase() + d.fixed_by.slice(1)}
                    </div>
                  )}
                </div>
                <span className="font-semibold tabular">+{Math.round(d.weight * 100)}</span>
                <div className="col-span-2 h-1.5 overflow-hidden rounded-full bg-muted">
                  <div className={cn("h-full rounded-full", d.root ? "bg-magenta" : "bg-muted-foreground/50")} style={{ width: `${Math.min(100, d.weight * 220)}%` }} />
                </div>
              </li>
            ))}
            {m.acct.tenure_months > 96 && <li className="text-[12.5px] text-muted-foreground">−10 for {Math.floor(m.acct.tenure_months / 12)} years of tenure</li>}
            {m.acct.tenure_months < 12 && <li className="text-[12.5px] text-muted-foreground">+10: new customers (under a year) leave more often</li>}
          </ul>
        )}
      </Panel>

      <Panel className="p-5">
        <h3 className="text-[16px] font-semibold">Recommended next step</h3>
        <ul className="mt-2 flex flex-col gap-2">
          {m.plan.map((p) => {
            const k = KIND[p.kind as keyof typeof KIND] ?? KIND.touch;
            return (
              <li key={p.id} className="rounded-xl bg-muted/50 p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={cn("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11.5px] font-semibold", k.cls)}>
                    <k.Icon className="size-3.5" aria-hidden /> {k.label}
                  </span>
                  <span className="text-[14.5px] font-semibold">{p.label}</span>
                  <span className="ml-auto text-[13px] font-semibold tabular">{p.cost_cents ? `costs ${money0(p.cost_cents)}` : "no cost"}</span>
                </div>
                <p className="mt-1 text-[13.5px] text-muted-foreground">{p.detail}</p>
              </li>
            );
          })}
        </ul>
        {m.level !== "low" && (
          <>
            <h4 className="mt-4 text-[13.5px] font-semibold">Next 12 months of revenue (expected)</h4>
            <div className="mt-2 flex flex-col gap-2 text-[13px]">
              {[
                ["Do nothing", m.expected_no_action_cents, `${Math.round(m.risk * 100)}% chance they leave`],
                ["This plan", m.expected_with_plan_cents, `${Math.round(m.risk_after * 100)}% chance they leave${m.revenue_after_cents !== m.revenue_now_cents ? `, at ${money0(m.revenue_after_cents)}/mo` : ""}`],
              ].map(([label, v, note]) => (
                <div key={label as string} className="grid grid-cols-[96px_1fr_auto] items-center gap-2">
                  <span className="font-medium">{label}</span>
                  <div className="h-5 overflow-hidden rounded-md bg-muted">
                    <div className={cn("h-full rounded-md", label === "This plan" ? "bg-magenta" : "bg-muted-foreground/40")} style={{ width: `${((v as number) / max) * 100}%` }} />
                  </div>
                  <span className="font-semibold tabular">{money0(v as number)}</span>
                  <span />
                  <span className="col-span-2 -mt-1 text-[11.5px] text-muted-foreground">{note}</span>
                </div>
              ))}
            </div>
            <p className="mt-2 text-[12.5px] text-muted-foreground">A blanket {money0(m.discount_only_cents)} loyalty credit would cost more and leave the actual problem in place.</p>
          </>
        )}
      </Panel>

      {msg && (
        <Panel className="p-5">
          <h3 className="flex items-center gap-2 text-[16px] font-semibold">
            <MessageSquareText className="size-4 text-magenta" aria-hidden /> What the customer gets (T-Life message)
          </h3>
          <div className="mt-2 max-w-[560px] rounded-2xl rounded-tl-md bg-magenta-wash px-4 py-3 text-[14.5px] leading-snug">{msg}</div>
          <p className="mt-2 flex items-center gap-1 text-[12px] text-muted-foreground">
            <ArrowRight className="size-3.5" aria-hidden /> One tap to accept; nothing changes on the account until they do.
          </p>
        </Panel>
      )}
    </div>
  );
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <Panel className="px-4 py-3">
      <div className="text-[12.5px] text-muted-foreground">{label}</div>
      <div className="text-[24px] font-bold tabular">{value}</div>
      {note && <div className="text-[11.5px] text-muted-foreground">{note}</div>}
    </Panel>
  );
}
