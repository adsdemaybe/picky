"use client";
import { Eye, Lock, Sparkles } from "lucide-react";
import { useLayoutEffect } from "react";
import { Conversation } from "@/components/AskPanel";
import { Panel } from "@/components/picky";
import { useAsk } from "@/lib/assistant/store";
import { maya } from "@/lib/data";
import { fmt } from "@/lib/money";

export default function AskPage() {
  const setOpen = useAsk((s) => s.setOpen);
  // the full page replaces the sheet
  useLayoutEffect(() => {
    setOpen(false);
  }, [setOpen]);
  return (
    <div className="grid gap-6 lg:grid-cols-[1fr_340px]">
      <Panel className="flex h-[calc(100dvh-180px)] min-h-[540px] animate-rise flex-col overflow-hidden">
        <div className="flex items-center gap-3 border-b border-border px-5 py-4">
          <span className="grid size-10 place-items-center rounded-full bg-sky text-white">
            <Sparkles className="size-5" aria-hidden />
          </span>
          <div>
            <h1 className="text-[22px] leading-tight font-bold">Ask Picky</h1>
            <p className="text-[13.5px] text-muted-foreground">Your shopping and money assistant</p>
          </div>
        </div>
        <Conversation wide />
      </Panel>
      <aside className="flex flex-col gap-4">
        <Panel className="p-5">
          <div className="flex items-center gap-2 font-semibold">
            <Eye className="size-4 text-sky" aria-hidden /> What Picky looks at
          </div>
          <ul className="mt-3 flex flex-col gap-2 text-[14px] text-muted-foreground">
            <li>Your purchases since July</li>
            <li>Prices at Picky Market, Picky Express, T-Mobile, Picky Online, a warehouse club, an open-box shop and a delivery app</li>
            <li>13 weeks of price history and announced sales</li>
            <li>
              Your {fmt(32000)} monthly grocery budget and your rule to ask before anything over {fmt(maya.mandate.ask_above_cents)}
            </li>
          </ul>
        </Panel>
        <Panel className="bg-sky-wash p-5 ring-0">
          <div className="flex items-center gap-2 font-semibold text-sky">
            <Lock className="size-4" aria-hidden /> How answers work
          </div>
          <p className="mt-2 text-[14px]">
            Claude decides which lookups to run. The prices, totals and savings come from plain code over your data, never from the AI&apos;s memory. Picky can&apos;t
            buy anything or move money.
          </p>
        </Panel>
      </aside>
    </div>
  );
}
