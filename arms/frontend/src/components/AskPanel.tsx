"use client";
// Ask Picky: a chat you can open from anywhere (sheet) or full-page (/ask), plus the cards that render tool results.
import { ArrowUp, Mic, Square, Sparkles } from "lucide-react";
import { motion } from "motion/react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { useVoiceInput } from "@/lib/useVoiceInput";
import { useTripGuide } from "@/lib/trip-guide";
import { TripGuide } from "./TripGuide";
import { useAsk, type Msg } from "@/lib/assistant/store";
import type { Card } from "@/lib/assistant/tools";
import { productBySku } from "@/lib/data";
import { fmt } from "@/lib/money";
import { effective, offersFor, verdictFor } from "@/lib/prices";
import { cn } from "@/lib/utils";
import { LottieSlot } from "./media";
import { Meter, ProductTile, VerdictChip } from "./picky";
import { OffersTable, Sparkline } from "./PriceBits";

export const SUGGESTIONS = [
  "Let’s do a weekly restock",
  "Is coffee a good price right now?",
  "Where did my money go this month?",
  "How can I save on groceries?",
  "Can I afford the Pixel Buds Pro 2?",
  "Where are running earbuds cheapest?",
];

export function AskSheet() {
  const { open, setOpen } = useAsk();
  const path = usePathname();
  return (
    <Sheet open={open && path !== "/ask"} onOpenChange={setOpen}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-[480px]">
        <SheetHeader className="border-b border-border px-5 py-4">
          <SheetTitle className="flex items-center gap-2 font-heading text-[20px]">
            <span className="grid size-8 place-items-center rounded-full bg-sky text-white">
              <Sparkles className="size-4" aria-hidden />
            </span>
            Ask Picky
          </SheetTitle>
          <SheetDescription>Prices, stores and your budget. Every number comes from your data.</SheetDescription>
        </SheetHeader>
        <Conversation />
      </SheetContent>
    </Sheet>
  );
}

export function Conversation({ wide }: { wide?: boolean }) {
  const { messages, busy, ask } = useAsk();
  const [text, setText] = useState("");
  const phase = useTripGuide((state) => state.phase);
  const voice = useVoiceInput(setText, (transcript) => { void ask(transcript); setText(""); });
  const recording = voice.status !== "idle";
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // scrollIntoView returns a Promise in recent Chrome; never return it from the effect
    void end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, busy, phase]);
  return (
    <>
      <div className="flex-1 overflow-y-auto px-5 py-5">
        <ol className="flex flex-col gap-5">
          {messages.map((m) => (
            <Bubble key={m.id} m={m} wide={wide} />
          ))}
          {busy && (
            <li aria-live="polite" aria-label="Picky is thinking">
              <LottieSlot
                slot="thinking"
                hint={false}
                className="h-8 w-16"
                fallback={
                  <span className="flex h-8 items-center gap-1.5 pl-1">
                    {[0, 1, 2].map((i) => (
                      <motion.span
                        key={i}
                        className="size-2.5 rounded-full bg-sky"
                        animate={{ y: [0, -6, 0], opacity: [0.5, 1, 0.5] }}
                        transition={{ duration: 0.8, repeat: Infinity, delay: i * 0.14 }}
                      />
                    ))}
                  </span>
                }
              />
            </li>
          )}
        </ol>
        {messages.length <= 1 && (
          <div className="mt-6 flex flex-wrap gap-2">
            {SUGGESTIONS.map((s) => (
              <motion.button
                key={s}
                onClick={() => ask(s)}
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.15 + SUGGESTIONS.indexOf(s) * 0.06 }}
                whileTap={{ scale: 0.96 }}
                className="cursor-pointer rounded-full bg-sky-wash px-3.5 py-2 text-left text-[13.5px] font-medium text-sky transition-colors hover:bg-sky hover:text-white"
              >
                {s}
              </motion.button>
            ))}
          </div>
        )}
        <TripGuide />
        <div ref={end} />
      </div>
      <form
        className="border-t border-border p-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (recording) return;
          ask(text);
          setText("");
        }}
      >
        <div className="flex items-center gap-2 rounded-full bg-muted p-1.5 pl-5 focus-within:ring-2 focus-within:ring-sky">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Ask about a price, a store, or your budget"
            className="min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-muted-foreground"
            aria-label="Message Picky"
          />
          {voice.supported && <Button type="button" variant="ghost" size="icon" disabled={busy || voice.status === "stopping"} aria-label={recording ? "Stop voice message" : "Speak to Picky"} onClick={() => recording ? voice.stop() : voice.start("")}>{recording ? <Square className="size-4 text-tomato" /> : <Mic className="size-4" />}</Button>}
          <Button type="submit" size="icon" className="bg-sky hover:bg-sky/90" disabled={busy || recording || !text.trim()} aria-label="Send">
            <ArrowUp className="size-5" />
          </Button>
        </div>
        {voice.error && <p role="alert" className="mt-2 px-2 text-[12px] text-tomato">{voice.error}</p>}
      </form>
    </>
  );
}

function Bubble({ m, wide }: { m: Msg; wide?: boolean }) {
  if (m.role === "user")
    return (
      <motion.li initial={{ opacity: 0, y: 12, scale: 0.95 }} animate={{ opacity: 1, y: 0, scale: 1 }} style={{ originX: 1 }} className="self-end">
        <div className="max-w-[36ch] rounded-2xl rounded-br-md bg-foreground px-4 py-2.5 text-[14.5px] text-background">{m.text}</div>
      </motion.li>
    );
  return (
    <motion.li initial="hidden" animate="show" variants={{ hidden: {}, show: { transition: { staggerChildren: 0.12 } } }} className="flex flex-col gap-3">
      <motion.div variants={{ hidden: { opacity: 0, y: 8 }, show: { opacity: 1, y: 0 } }} className="max-w-[62ch] text-[15px] leading-relaxed whitespace-pre-line">
        {m.text}
      </motion.div>
      {m.cards?.map((c, i) => (
        <motion.div key={i} variants={{ hidden: { opacity: 0, y: 14, scale: 0.98 }, show: { opacity: 1, y: 0, scale: 1 } }}>
          <AssistantCard card={c} wide={wide} />
        </motion.div>
      ))}
      {m.source && (
        <div className="text-[12px] text-muted-foreground">
          {m.source === "claude" ? "Answered by Claude using your data" : "Answered offline from your data"}
        </div>
      )}
    </motion.li>
  );
}

const box = "rounded-2xl bg-card ring-1 ring-foreground/[0.08]";

export function AssistantCard({ card, wide }: { card: Card; wide?: boolean }) {
  switch (card.type) {
    case "price": {
      const p = productBySku[card.sku];
      const v = verdictFor(card.sku);
      return (
        <div className={cn(box, "p-4")}>
          <div className="flex items-start gap-3">
            <ProductTile color={p.label_color} category={p.category} name={p.name} size={44} />
            <div className="min-w-0 flex-1">
              <div className="leading-snug font-semibold">{p.name}</div>
              <div className="text-[13px] text-muted-foreground">{v.why}</div>
            </div>
            <VerdictChip verdict={v} />
          </div>
          <div className={cn("mt-3 grid gap-3", wide && "md:grid-cols-[1fr_auto] md:items-end")}>
            <OffersTable sku={card.sku} compact limit={4} />
            <Sparkline sku={card.sku} w={wide ? 220 : 380} h={56} />
          </div>
          <Link href={`/find?sku=${card.sku}`} className="mt-3 inline-block text-[13.5px] font-semibold text-leaf hover:underline">
            See all stores
          </Link>
        </div>
      );
    }
    case "products":
      return (
        <div className={cn(box, "p-2")}>
          {card.skus.map((s) => {
            const p = productBySku[s];
            const best = offersFor(s)[0];
            return (
              <Link key={s} href={`/find?sku=${s}`} className="flex items-center gap-3 rounded-xl p-2 hover:bg-muted">
                <ProductTile color={p.label_color} category={p.category} name={p.name} size={36} />
                <span className="min-w-0 flex-1 truncate text-[14px] font-medium">{p.name}</span>
                <VerdictChip verdict={verdictFor(s)} />
                <span className="tabular w-[70px] text-right font-semibold">{best ? fmt(effective(best)) : ""}</span>
              </Link>
            );
          })}
        </div>
      );
    case "spend": {
      const max = Math.max(...card.rows.map((r) => r.cents), 1);
      return (
        <div className={cn(box, "p-4")}>
          <div className="mb-3 flex items-baseline justify-between">
            <span className="font-semibold">{card.title}</span>
            <span className="tabular font-heading text-[20px] font-semibold">{fmt(card.total)}</span>
          </div>
          <ul className="flex flex-col gap-2">
            {card.rows.map((r) => (
              <li key={r.label} className="grid grid-cols-[120px_1fr_70px] items-center gap-3 text-[13.5px]">
                <span className="truncate">{r.label}</span>
                <span className="h-3 rounded-full bg-muted">
                  <span className="block h-full rounded-full" style={{ width: `${Math.max(2, (r.cents / max) * 100)}%`, background: r.color }} />
                </span>
                <span className="tabular text-right font-medium">{fmt(r.cents)}</span>
              </li>
            ))}
          </ul>
        </div>
      );
    }
    case "budget":
      return (
        <div className={cn(box, "p-4")}>
          <div className="flex items-baseline justify-between">
            <span className="font-semibold">October groceries</span>
            <span className="tabular text-[14px]">
              <strong className="font-heading text-[20px]">{fmt(card.spent)}</strong> of {fmt(card.budget)}
            </span>
          </div>
          <Meter value={card.spent} max={card.budget} pace={card.pace} className="mt-3" />
          <div className="mt-2 flex justify-between text-[12.5px] text-muted-foreground">
            <span>Should be near {fmt(card.pace)} by today</span>
            <span>On track for {fmt(card.projected)}</span>
          </div>
        </div>
      );
    case "savings":
      return (
        <div className={cn(box, "overflow-hidden")}>
          <div className="flex items-center justify-between bg-lime px-4 py-3 text-lime-ink">
            <span className="font-semibold">Ways to keep more money</span>
            <span className="tabular font-heading text-[18px] font-bold">{fmt(card.total)} a month</span>
          </div>
          <ul className="divide-y divide-border">
            {card.items.map((it) => {
              const p = it.sku ? productBySku[it.sku] : null;
              return (
                <li key={it.text} className="flex items-center gap-3 px-4 py-3 text-[13.5px]">
                  {p && <ProductTile color={p.label_color} category={p.category} name={p.name} size={32} />}
                  <span className="min-w-0 flex-1">{it.text}</span>
                  <span className="tabular shrink-0 font-semibold text-leaf">save {fmt(it.save_cents)}</span>
                </li>
              );
            })}
          </ul>
        </div>
      );
    case "afford":
      return (
        <div className={cn(box, "flex items-start gap-3 p-4")}>
          <span
            className={cn(
              "grid size-10 shrink-0 place-items-center rounded-full font-heading text-[14px] font-bold",
              card.verdict === "yes" ? "bg-leaf text-white" : card.verdict === "tight" ? "bg-amber text-amber-ink" : "bg-tomato text-white",
            )}
            aria-hidden
          >
            {card.verdict === "yes" ? "Yes" : card.verdict === "tight" ? "Hm" : "No"}
          </span>
          <div className="min-w-0 flex-1">
            <div className="flex items-baseline justify-between gap-2">
              <span className="font-semibold">{card.verdict === "yes" ? "It fits" : card.verdict === "tight" ? "It's tight" : "Not right now"}</span>
              <span className="tabular font-heading text-[18px] font-semibold">{fmt(card.amount)}</span>
            </div>
            <p className="mt-0.5 text-[13.5px] text-muted-foreground">{card.note}</p>
            {card.left_before > 0 && (
              <p className="tabular mt-1 text-[12.5px] text-muted-foreground">
                Left this month: {fmt(card.left_before)}, then {fmt(card.left_after)}
              </p>
            )}
          </div>
        </div>
      );
  }
}
