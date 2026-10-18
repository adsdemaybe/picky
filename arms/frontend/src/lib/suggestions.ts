"use client";
// "Running low" restock suggestions for Home: GET /api/users/{id}/suggestions in gateway mode, else (gateway off or
// down, endpoint missing, or NEXT_PUBLIC_SUGGESTIONS_MOCK=1) the same shape computed locally from the demo purchase
// history's cadence, so the card always renders.
import { useEffect, useMemo, useState } from "react";
import { STORES, TODAY, maya, productBySku, slotsOf, type Purchase } from "./data";
import { gw, productImageUrl, type Suggestion } from "./gateway/client";
import { useGateway } from "./gateway/useGateway";

export type { Suggestion };

const FORCE_MOCK = process.env.NEXT_PUBLIC_SUGGESTIONS_MOCK === "1";
const DAY = 86_400_000;
const MAX = 6;

export type SuggestionsState = {
  items: Suggestion[];
  /** where the rows came from: the gateway endpoint, or the local demo history */
  source: "gateway" | "demo";
  loading: boolean;
};

/** Cadence from purchase history: items bought ≥ 3 times, most overdue first (same math as data.runningLow). */
export function mockSuggestions(purchases: Purchase[], withImages: boolean): Suggestion[] {
  const bySku = new Map<string, number[]>();
  for (const p of purchases) bySku.set(p.sku, [...(bySku.get(p.sku) ?? []), Date.parse(p.ts)]);
  const slots = slotsOf("store_ave");
  const rows: Suggestion[] = [];
  const due = new Map<string, number>();
  for (const [sku, ts] of bySku) {
    const p = productBySku[sku];
    if (ts.length < 3 || !p || !slots[sku]) continue;
    const times = ts.sort((a, b) => a - b);
    const gaps = times.slice(1).map((t, i) => (t - times[i]) / DAY);
    const every = Math.round(gaps.reduce((s, g) => s + g, 0) / gaps.length);
    const since = Math.floor((TODAY.getTime() - times[times.length - 1]) / DAY);
    const slot = slots[sku];
    const deal = slot.deal;
    due.set(sku, every - since);
    const qtys = purchases.filter((x) => x.sku === sku).map((x) => x.qty);
    rows.push({
      sku,
      name: p.name,
      price_cents: deal?.price_cents ?? p.price_cents,
      last_bought_days: since,
      cadence_days: every,
      reason: since >= every ? `You buy it about every ${every} days, so you're probably out.` : `You buy it about every ${every} days, so it's due soon.`,
      store_id: "store_ave",
      in_stock: slot.qty > 0,
      aisle_label: STORES.store_ave.aisles.find((a) => a.aisle_id === slot.aisle_id)?.label ?? null,
      image_url: withImages ? productImageUrl(sku) : null,
      suggested_qty: Math.max(1, Math.round(qtys.reduce((a, b) => a + b, 0) / qtys.length)),
      label_color: p.label_color,
      category: p.category,
      sponsored: false,
      overdue_days: since - every,
      times_bought: ts.length,
      signals: { last_bought_days: since, cadence_days: every, overdue_days: since - every, times_bought: ts.length, source: "demo purchase history" },
    });
  }
  return rows.sort((a, b) => due.get(a.sku)! - due.get(b.sku)!).slice(0, MAX);
}

export function useSuggestions(purchases: Purchase[] = maya.purchases): SuggestionsState {
  const status = useGateway((s) => s.status);
  const live = status === "live";
  const [fetched, setFetched] = useState<Suggestion[] | "failed" | null>(null);
  const mock = useMemo(() => mockSuggestions(purchases, live), [purchases, live]);

  useEffect(() => {
    useGateway.getState().startPolling();
  }, []);

  useEffect(() => {
    if (FORCE_MOCK || !live) return;
    let alive = true;
    gw.suggestions(maya.profile.user_id)
      .then(({ data }) => alive && setFetched(Array.isArray(data) && data.length ? data.slice(0, MAX) : "failed"))
      // endpoint not deployed yet (404) or the gateway hiccuped: the demo rows keep the card working
      .catch(() => alive && setFetched("failed"));
    return () => {
      alive = false;
    };
  }, [live]);

  if (FORCE_MOCK || status === "off" || status === "down") return { items: mock, source: "demo", loading: false };
  if (status === "checking" || fetched === null) return { items: [], source: "demo", loading: true }; // ≤ 2.5 s
  if (fetched === "failed") return { items: mock, source: "demo", loading: false };
  return { items: fetched, source: "gateway", loading: false };
}

/** "You bought this 12 days ago" */
export function boughtAgo(days: number | null | undefined) {
  if (days == null) return "New for you";
  if (days <= 0) return "You bought this today";
  if (days === 1) return "You bought this yesterday";
  return `You bought this ${days} days ago`;
}

export const qtyOf = (s: Suggestion) => Math.max(1, Math.round(s.suggested_qty ?? s.qty ?? 1));

/** Where it is, short: "Aisle 3", "Produce", "Dairy, Meat & Deli". */
export function whereText(s: Suggestion): string | null {
  let label: unknown = s.aisle_label ?? s.location;
  if (label && typeof label === "object") {
    const o = label as Record<string, unknown>;
    label = o.aisle_label ?? o.label ?? o.aisle_name ?? o.aisle ?? o.aisle_id;
  }
  if (typeof label !== "string" || !label) return null;
  return label.match(/^Aisle \d+/i)?.[0] ?? label.split(" · ")[0].split(" - ")[0];
}

/** The "why" sentence: an AI-written reason when there is one, else the cadence reason without the "You bought this
 *  N days ago" part the card already shows above it. */
export function whyText(s: Suggestion): string {
  const ai = (s.why ?? s.ai_reason ?? "").trim();
  if (ai) return ai;
  const r = (s.reason ?? "").replace(/^you bought (this|it)[^.—–-]*[.—–-]\s*/i, "").trim();
  return r ? r[0].toUpperCase() + r.slice(1) : "";
}

const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : typeof v === "string" && v.trim() && !isNaN(Number(v)) ? Number(v) : null);

/** The signals behind a card, as short chips (each keeps its raw key = value as a tooltip). */
export function signalChips(s: Suggestion): { text: string; title: string; tone: "due" | "plain" }[] {
  const sig = s.signals ?? {};
  const times = num(sig.times_bought) ?? s.times_bought ?? null;
  const every = num(sig.cadence_days) ?? s.cadence_days ?? null;
  const over = num(sig.overdue_days) ?? s.overdue_days ?? (s.last_bought_days != null && every != null ? s.last_bought_days - every : null);
  const out: { text: string; title: string; tone: "due" | "plain" }[] = [];
  if (over != null) out.push({ text: over > 0 ? `${over} d overdue` : over === 0 ? "Due today" : `Due in ${-over} d`, title: `overdue_days = ${over}`, tone: over >= 0 ? "due" : "plain" });
  if (every != null) out.push({ text: `Every ${every} d`, title: `cadence_days = ${every}`, tone: "plain" });
  if (times != null) out.push({ text: `Bought ${times}×`, title: `times_bought = ${times}`, tone: "plain" });
  return out;
}

/** Why this card is here beyond "due again": substitute / complement / deal, as a short badge. */
export function kindBadge(s: Suggestion): { text: string; variant: "grape" | "sky" | "lime" } | null {
  const name = (sku: string | null | undefined) => (sku ? (productBySku[sku]?.name ?? sku) : null);
  if (s.kind === "substitute") return { text: name(s.substitute_for) ? `Instead of ${name(s.substitute_for)}` : "Substitute", variant: "grape" };
  if (s.kind === "complement") return { text: name(s.related_to) ? `Goes with ${name(s.related_to)}` : "Goes with your list", variant: "sky" };
  if (s.kind === "deal" || s.deal_text) return { text: s.deal_text || "On deal", variant: "lime" };
  return null;
}

/** Where the signals come from ("own purchase history", "demo purchase history"). */
export const signalSource = (s: Suggestion) => (typeof s.signals?.source === "string" ? s.signals.source : null);
