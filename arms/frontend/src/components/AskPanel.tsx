"use client";
// "Ask Picky": a chat drawer available on every page, plus the cards that render tool results.
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useAsk, type Msg } from "@/lib/assistant/store";
import type { Card } from "@/lib/assistant/tools";
import { productBySku } from "@/lib/data";
import { fmt } from "@/lib/money";
import { effective, offersFor, verdictFor } from "@/lib/prices";
import { OffersTable, Sparkline, VerdictBadge } from "./PriceBits";
import { Meter, Swatch, cx } from "./ui";

export const SUGGESTIONS = [
  "Is coffee a good price right now?",
  "Where did my money go this month?",
  "How can I save on groceries?",
  "Can I afford the Pixel Buds Pro 2?",
  "Where are running earbuds cheapest?",
];

export function AskPanel() {
  const { open, setOpen } = useAsk();
  const path = usePathname();
  if (!open || path === "/ask") return null; // the /ask page shows the same conversation full-size
  return (
    <aside className="fixed inset-y-0 right-0 z-40 flex w-[min(460px,100vw)] flex-col border-l border-ink bg-paper">
      <header className="flex items-center justify-between border-b border-ink px-4 py-3">
        <div>
          <div className="font-display text-[18px] font-semibold">Ask Picky</div>
          <div className="text-[12px] text-muted">Prices, stores, budget. Numbers come from your data, not guesses.</div>
        </div>
        <button onClick={() => setOpen(false)} className="px-2 text-[20px] text-ink-2 hover:text-ink" aria-label="Close">
          ×
        </button>
      </header>
      <Conversation />
    </aside>
  );
}

export function Conversation({ wide }: { wide?: boolean }) {
  const { messages, busy, ask } = useAsk();
  const [text, setText] = useState("");
  const end = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // scrollIntoView returns a Promise in recent Chrome; never return it from the effect
    void end.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length, busy]);
  return (
    <>
      <div className="flex-1 overflow-y-auto px-4 py-4">
        <ol className="flex flex-col gap-4">
          {messages.map((m) => (
            <Bubble key={m.id} m={m} wide={wide} />
          ))}
          {busy && (
            <li className="flex items-center gap-2 font-mono text-[12px] text-muted">
              <span className="live-dot size-1.5 rounded-full bg-tomato" /> checking your data…
            </li>
          )}
        </ol>
        {messages.length <= 1 && (
          <div className="mt-5 flex flex-col items-start gap-1.5">
            {SUGGESTIONS.map((s) => (
              <button key={s} onClick={() => ask(s)} className="border border-line bg-card px-2.5 py-1 text-left text-[13px] text-ink-2 hover:border-ink hover:text-ink">
                {s}
              </button>
            ))}
          </div>
        )}
        <div ref={end} />
      </div>
      <form
        className="flex border-t border-ink bg-card"
        onSubmit={(e) => {
          e.preventDefault();
          ask(text);
          setText("");
        }}
      >
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Ask about a price, a store, or your budget"
          className="min-w-0 flex-1 bg-transparent px-4 py-3.5 text-[14.5px] outline-none placeholder:text-muted"
          aria-label="Message Picky"
        />
        <button disabled={busy || !text.trim()} className="border-l border-ink bg-ink px-4 text-[13.5px] font-medium text-paper disabled:opacity-40">
          Send
        </button>
      </form>
    </>
  );
}

function Bubble({ m, wide }: { m: Msg; wide?: boolean }) {
  if (m.role === "user")
    return (
      <li className="self-end">
        <div className="max-w-[34ch] bg-ink px-3 py-2 text-[14px] text-paper">{m.text}</div>
      </li>
    );
  return (
    <li className="flex flex-col gap-2">
      <div className="max-w-[60ch] text-[14.5px] leading-relaxed whitespace-pre-line">{m.text}</div>
      {m.cards?.map((c, i) => (
        <AssistantCard key={i} card={c} wide={wide} />
      ))}
      {m.source && <div className="font-mono text-[10px] text-muted uppercase">{m.source === "claude" ? "Claude · tools over your data" : "offline answers · same tools"}</div>}
    </li>
  );
}

export function AssistantCard({ card, wide }: { card: Card; wide?: boolean }) {
  const box = "border border-line bg-card";
  switch (card.type) {
    case "price": {
      const p = productBySku[card.sku];
      const v = verdictFor(card.sku);
      return (
        <div className={box}>
          <div className="flex items-start justify-between gap-3 border-b border-line px-3 py-2.5">
            <div>
              <div className="font-medium">{p.name}</div>
              <div className="text-[12.5px] text-ink-2">{v.why}</div>
            </div>
            <VerdictBadge sku={card.sku} />
          </div>
          <div className={cx("grid gap-3 px-3 py-2", wide && "md:grid-cols-[1fr_auto]")}>
            <OffersTable sku={card.sku} compact />
            <Sparkline sku={card.sku} w={wide ? 220 : 200} h={46} />
          </div>
          <div className="border-t border-line px-3 py-2 text-[12.5px]">
            <Link href={`/find?sku=${card.sku}`} className="font-medium text-tomato hover:underline">
              Open in Find →
            </Link>
          </div>
        </div>
      );
    }
    case "products":
      return (
        <div className={box}>
          <div className="label-caps border-b border-line px-3 py-1.5">{card.title}</div>
          <ul className="divide-y divide-line">
            {card.skus.map((s) => {
              const p = productBySku[s];
              const best = offersFor(s)[0];
              return (
                <li key={s}>
                  <Link href={`/find?sku=${s}`} className="flex items-center gap-2 px-3 py-2 text-[13.5px] hover:bg-paper">
                    <Swatch color={p.label_color} />
                    <span className="min-w-0 flex-1 truncate">{p.name}</span>
                    <VerdictBadge sku={s} />
                    <span className="num w-[70px] text-right">{best ? fmt(effective(best)) : "·"}</span>
                  </Link>
                </li>
              );
            })}
          </ul>
        </div>
      );
    case "spend": {
      const max = Math.max(...card.rows.map((r) => r.cents), 1);
      return (
        <div className={cx(box, "px-3 py-2.5")}>
          <div className="mb-2 flex justify-between">
            <span className="label-caps">{card.title}</span>
            <span className="num font-medium">{fmt(card.total)}</span>
          </div>
          <ul className="flex flex-col gap-1.5">
            {card.rows.map((r) => (
              <li key={r.label} className="grid grid-cols-[110px_1fr_64px] items-center gap-2 text-[12.5px]">
                <span className="truncate text-ink-2">{r.label}</span>
                <span className="h-2.5 bg-sunk">
                  <span className="block h-full" style={{ width: `${(r.cents / max) * 100}%`, background: r.color }} />
                </span>
                <span className="num text-right">{fmt(r.cents)}</span>
              </li>
            ))}
          </ul>
        </div>
      );
    }
    case "budget":
      return (
        <div className={cx(box, "px-3 py-2.5")}>
          <div className="flex items-baseline justify-between">
            <span className="label-caps">October budget</span>
            <span className="num text-[13px]">
              <strong>{fmt(card.spent)}</strong> / {fmt(card.budget)}
            </span>
          </div>
          <Meter value={card.spent} max={card.budget} marks={[card.pace]} className="mt-2" />
          <div className="mt-1.5 flex justify-between text-[12px] text-muted">
            <span>pace {fmt(card.pace)}</span>
            <span>projected {fmt(card.projected)}</span>
          </div>
        </div>
      );
    case "savings":
      return (
        <div className={box}>
          <div className="flex justify-between border-b border-line px-3 py-1.5">
            <span className="label-caps">Ways to save</span>
            <span className="num text-[12.5px] text-basil">~{fmt(card.total)}/mo</span>
          </div>
          <ul className="divide-y divide-line">
            {card.items.map((it) => (
              <li key={it.text} className="flex gap-3 px-3 py-2 text-[13px]">
                <span className="min-w-0 flex-1 text-ink-2">{it.text}</span>
                <span className="num shrink-0 text-basil">−{fmt(it.save_cents)}</span>
              </li>
            ))}
          </ul>
        </div>
      );
    case "afford":
      return (
        <div className={cx(box, "border-l-[3px] px-3 py-2.5", card.verdict === "yes" ? "border-l-basil" : card.verdict === "tight" ? "border-l-turmeric" : "border-l-tomato")}>
          <div className="flex items-baseline justify-between">
            <span className="font-display text-[16px] font-semibold">{card.verdict === "yes" ? "Fits" : card.verdict === "tight" ? "Tight" : "Not now"}</span>
            <span className="num text-[13px]">{fmt(card.amount)}</span>
          </div>
          <p className="mt-1 text-[13px] text-ink-2">{card.note}</p>
          {card.left_before > 0 && (
            <div className="mt-2 font-mono text-[11px] text-muted">
              left this month {fmt(card.left_before)} → {fmt(card.left_after)}
            </div>
          )}
        </div>
      );
  }
}
