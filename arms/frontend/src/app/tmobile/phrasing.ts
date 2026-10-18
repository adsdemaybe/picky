"use client";
// Swaps in LLM-phrased reasons (Grok or Claude) from /api/whisper when they pass validation; the cards themselves never change.
import { useEffect, useState } from "react";
import type { Suggestion } from "@/lib/whisperer";

const cache = new Map<string, string>();

export function usePhrasing(cards: Suggestion[]) {
  const [, bump] = useState(0);
  const [source, setSource] = useState<"template" | "claude" | "grok">("template");
  const key = cards.map((c) => `${c.id}:${c.reason}`).join("|");
  useEffect(() => {
    const todo = cards.filter((c) => !cache.has(`${c.id}:${c.reason}`));
    if (!todo.length) return;
    const ctl = new AbortController();
    const id = setTimeout(() => {
      fetch("/api/whisper", {
        method: "POST",
        headers: { "content-type": "application/json" },
        signal: ctl.signal,
        body: JSON.stringify({ cards: todo.map(({ id, kind, title, reason, reason_codes, signals }) => ({ id, kind, title, reason, reason_codes, signals })) }),
      })
        .then((r) => r.json())
        .then((r: { reasons: Record<string, string>; source: "template" | "claude" | "grok" }) => {
          for (const c of todo) cache.set(`${c.id}:${c.reason}`, r.reasons[c.id] ?? c.reason);
          setSource(r.source);
          bump((n) => n + 1);
        })
        .catch(() => {});
    }, 300);
    return () => (clearTimeout(id), ctl.abort());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);
  return { cards: cards.map((c) => ({ ...c, reason: cache.get(`${c.id}:${c.reason}`) ?? c.reason })), source };
}
