"use client";
import { useLayoutEffect } from "react";
import { Conversation } from "@/components/AskPanel";
import { Card, PageHead } from "@/components/ui";
import { useAsk } from "@/lib/assistant/store";
import { maya } from "@/lib/data";
import { fmt } from "@/lib/money";

export default function AskPage() {
  const setOpen = useAsk((s) => s.setOpen);
  // the full page replaces the drawer
  useLayoutEffect(() => {
    setOpen(false);
  }, [setOpen]);
  return (
    <>
      <PageHead kicker="Ask Picky" title="Your shopping and money assistant" />
      <div className="grid grid-cols-12 gap-6">
        <div className="col-span-12 flex h-[calc(100dvh-220px)] min-h-[520px] flex-col border border-ink bg-card lg:col-span-8">
          <Conversation wide />
        </div>
        <aside className="col-span-12 flex flex-col gap-4 lg:col-span-4">
          <Card variant="inset" accent="basil" className="p-4 text-[13.5px]">
            <div className="label-caps mb-2">What Picky can see</div>
            <ul className="flex flex-col gap-1.5 text-ink-2">
              <li>Every purchase since July (107 lines, 15 trips)</li>
              <li>Prices at 2 Picky stores, a T-Mobile store, Picky Online, a warehouse club, an open-box shop and a delivery app</li>
              <li>13 weeks of price history and announced sales</li>
              <li>Your budget ({fmt(32000)}/month) and rules (asks above {fmt(maya.mandate.ask_above_cents)})</li>
            </ul>
          </Card>
          <Card variant="flat" className="p-4 text-[13px] text-ink-2">
            <div className="label-caps mb-2">How answers work</div>
            Claude chooses which lookups to run; the prices, totals and savings come from deterministic code over your data. It can&apos;t buy anything or move
            money. Purchases still go through your rules and an explicit approval.
          </Card>
        </aside>
      </div>
    </>
  );
}
