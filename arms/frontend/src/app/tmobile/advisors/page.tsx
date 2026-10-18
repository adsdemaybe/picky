"use client";
// Hub: all five challenge directions, each with its headline example computed live from the demo data.
import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { Panel } from "@/components/picky";
import { DATA, household, match, moments, money, money0, upgrade } from "@/lib/directions";
import { DIRECTIONS } from "./nav";

export default function Advisors() {
  const priya = match(DATA.matchmaker_presets.find((p) => p.id === "priya")!).bundles[0];
  const ms = moments();
  const high = ms.filter((m) => m.level === "high");
  const discounts = ms.flatMap((m) => m.plan).filter((p) => p.kind === "discount").length;
  const maya = upgrade(DATA.upgrade_cases.find((c) => c.id === "maya")!);
  const garcia = household(DATA.households.find((h) => h.id === "garcia")!);
  const example = [
    "Pixel 9 setup: skip the new charger (you own one) and wait on the phone itself: $200 off is expected Nov 27.",
    `Priya, a student on a $60 budget: ${priya.title}, ${money(priya.monthly_cents)}/mo, with the trade-offs spelled out.`,
    `${ms.length} accounts scored. ${high.length} high risk, each fixed at the cause; ${discounts} blanket discounts; ${ms.filter((m) => m.plan[0]?.id === "none").length} customers deliberately left alone.`,
    `Maya asks "should I upgrade?" Answer: not yet. ${maya.headline}.`,
    `The Garcias pay for Premium but use under 25 GB each: ${garcia.best.summary} saves ${money0(garcia.saves_cents)}/mo (${money0(garcia.saves_cents * 12)} a year).`,
  ];
  return (
    <div className="flex flex-col gap-6 pb-16">
      <section>
        <p className="text-[14px] font-semibold text-magenta">DubHacks 2026 · T-Mobile track</p>
        <h1 className="mt-1 text-[32px] leading-tight font-bold md:text-[42px]">All five directions, working</h1>
        <p className="mt-1 max-w-[760px] text-[15px] text-muted-foreground">
          Cart Whisperer is our main build. The other four run on the same synthetic customer, catalog and plan data, with deterministic engines that explain every answer. Each one has worked examples you can open.
        </p>
      </section>
      <div className="grid gap-3 md:grid-cols-2">
        {DIRECTIONS.map((d, i) => (
          <Link key={d.href} href={d.href} className="group">
            <Panel className="flex h-full flex-col gap-2 p-5 transition-shadow group-hover:shadow-md">
              <div className="flex items-center gap-3">
                <span className="grid size-10 place-items-center rounded-xl bg-magenta-wash text-magenta">
                  <d.Icon className="size-5" aria-hidden />
                </span>
                <div>
                  <div className="text-[18px] font-semibold">{d.name}</div>
                  <div className="text-[13px] text-muted-foreground">{d.line}</div>
                </div>
                <ArrowRight className="ml-auto size-5 text-muted-foreground transition-transform group-hover:translate-x-1" aria-hidden />
              </div>
              <p className="rounded-xl bg-muted/60 px-3 py-2 text-[14px]">{example[i]}</p>
            </Panel>
          </Link>
        ))}
      </div>
      <p className="text-[12px] text-muted-foreground">All people, accounts, usage, support histories and plan prices are synthetic demo data. Device prices are from t-mobile.com (Oct 17, 2026).</p>
    </div>
  );
}
