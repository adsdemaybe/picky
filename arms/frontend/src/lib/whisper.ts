"use client";
// Cart Whisperer (T-Mobile track, docs/cart-whisperer.md) in the web app: ≤3 explained cards per phase from the gateway
//   GET  /api/users/{user}/whisper?store_id=&phase=planning|in_trip|checkout[&trip_id=]   → WhisperResponse
//   POST /api/users/{user}/whisper/{card_id}/accept | decline   ({trip_id?, channel}) → WhisperDecisionResult
//   WS   whisper.cards { phase, cards, empty_reason } on the trip socket; the latest pass is also Trip.whisper
// The gateway decides everything (deterministic gates, score, reason codes, mandate); this file only normalizes the
// shape, keeps the answers and applies an accepted planning card to the local draft. A gateway without the endpoint
// (404 / 405 / 501) leaves every screen on its current behaviour (demo advice cards, the checkout extras approval).
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { create } from "zustand";
import { maya, online, productBySku, registerProduct, slotsOf, type StoreId } from "@/lib/data";
import { GatewayError, errorText, gw } from "@/lib/gateway/client";
import { placeholderProduct } from "@/lib/gateway/adapt";
import { fmt } from "@/lib/money";
import type { Draft, ListLine } from "@/lib/order";

// TODO(contracts 0.2.5 whisper): narrow local copy of contracts/whisper.py (WhisperCard / WhisperResponse /
// WhisperDecisionResult). Replace with the generated types (S["WhisperCard"], events.ts) once the contracts PR is
// merged and `make gen-types` has run; normalizeCard() then only maps the design doc's older names.
export type WhisperPhase = "planning" | "in_trip" | "checkout";
export type WhisperKind = "complete" | "replenish" | "swap_save" | "qty" | "deal" | "remove" | "skip" | "wait";
export type WhisperAction = "add" | "swap" | "set_qty" | "remove" | "skip" | "remind";
export type SignalSource = "commerce" | "customer" | "physical" | "other";
export type WhisperSignal = { name: string; value: string | number | boolean | null; source: SignalSource; label?: string };
export type WhisperCard = {
  card_id: string;
  kind: WhisperKind;
  /** the product to add / swap in / skip / wait on; null for a skip / wait / remove of a cart line → line_sku */
  sku: string | null;
  /** the list / cart line the card acts on (swap: the line it replaces) */
  line_sku: string | null;
  name: string;
  /** card headline ("Skip the charger"); "" = use the kind label + name */
  title: string;
  price_cents: number;
  /** the money chip: saved for the same need (swap / qty / remove), else avoided spend (skip / wait) */
  savings_cents: number | null;
  /** WAIT: the expected price on remind_on */
  promo_price_cents: number | null;
  detour_m: number | null;
  aisle_label: string | null;
  /** shopper-facing sentence (deterministic template or LLM phrasing of the reason codes) */
  reason: string;
  reason_codes: string[];
  signals: WhisperSignal[];
  score: number;
  action: WhisperAction;
  /** YYYY-MM-DD, WAIT only */
  remind_on: string | null;
  sponsored: boolean;
  /** add / swap: units; set_qty: the new quantity; remove / skip / wait: 0 */
  qty: number | null;
  needs_approval: boolean;
};
export type WhisperSet = {
  phase: WhisperPhase;
  store_id: string;
  cards: WhisperCard[];
  empty_reason: string | null;
  /** candidates hidden because the shopper declined them before (trip 2 is visibly different) */
  hidden_declined: number;
};
export type WhisperOutcome = {
  decision: "accept" | "decline";
  /** ok = applied / recorded; ask = the mandate wants the shopper's OK; denied = the mandate blocked it */
  status: "ok" | "ask" | "denied";
  text: string;
  remind_on?: string | null;
};

const KIND_ALIASES: Record<string, WhisperKind> = {
  complete: "complete",
  replenish: "replenish",
  swap_save: "swap_save",
  swap: "swap_save",
  qty: "qty",
  qty_fix: "qty",
  deal: "deal",
  deal_match: "deal",
  remove: "remove",
  remove_warn: "remove",
  skip: "skip",
  wait: "wait",
};
const DEFAULT_ACTION: Record<WhisperKind, WhisperAction> = {
  complete: "add",
  replenish: "add",
  swap_save: "swap",
  qty: "set_qty",
  deal: "add",
  remove: "remove",
  skip: "skip",
  wait: "remind",
};
const ACTIONS = new Set<WhisperAction>(["add", "swap", "set_qty", "remove", "skip", "remind"]);
const SOURCES = new Set<SignalSource>(["commerce", "customer", "physical"]);

const str = (v: unknown) => (typeof v === "string" && v.trim() ? v : null);
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : null);
const scalar = (v: unknown): WhisperSignal["value"] => (typeof v === "string" || typeof v === "number" || typeof v === "boolean" ? v : v == null ? null : JSON.stringify(v));

function normalizeSignals(raw: unknown): WhisperSignal[] {
  if (Array.isArray(raw))
    return raw.flatMap((s) => {
      if (!s || typeof s !== "object") return [];
      const o = s as Record<string, unknown>;
      const name = str(o.name) ?? str(o.key);
      if (!name) return [];
      const src = str(o.source) as SignalSource | null;
      return [{ name, value: scalar(o.value), source: src && SOURCES.has(src) ? src : "other", label: str(o.label) ?? undefined }];
    });
  // the design doc's `signals: dict[str, scalar]` (no source): shown under "Other signals"
  if (raw && typeof raw === "object") return Object.entries(raw as Record<string, unknown>).map(([name, v]) => ({ name, value: scalar(v), source: "other" as const }));
  return [];
}

/** One card from the gateway, tolerant of the design doc's field names (suggestion_id, qty_fix, title, …). */
export function normalizeCard(raw: unknown, i = 0): WhisperCard | null {
  if (!raw || typeof raw !== "object") return null;
  const o = raw as Record<string, unknown>;
  const kind = KIND_ALIASES[String(o.kind ?? "").toLowerCase()];
  if (!kind) return null;
  const sku = str(o.sku);
  const line_sku = str(o.line_sku) ?? str(o.replaces_sku);
  const act = String(o.action ?? "").toLowerCase() as WhisperAction;
  const action = ACTIONS.has(act) ? act : DEFAULT_ACTION[kind];
  const target = sku ?? line_sku;
  const name = str(o.name) ?? str(o.product_name) ?? (target ? (productBySku[target]?.name ?? target) : str(o.title) ?? "This item");
  return {
    card_id: str(o.card_id) ?? str(o.suggestion_id) ?? `${kind}:${target ?? i}`,
    kind,
    sku,
    line_sku,
    name,
    title: str(o.title) ?? "",
    price_cents: num(o.price_cents) ?? (target ? (productBySku[target]?.price_cents ?? 0) : 0),
    savings_cents: num(o.savings_cents) || num(o.avoided_cents),
    promo_price_cents: num(o.promo_price_cents),
    detour_m: num(o.detour_m),
    aisle_label: str(o.aisle_label),
    reason: str(o.reason) ?? str(o.title) ?? "",
    reason_codes: Array.isArray(o.reason_codes) ? o.reason_codes.filter((c): c is string => typeof c === "string") : [],
    signals: normalizeSignals(o.signals),
    score: num(o.score) ?? 0,
    action,
    remind_on: str(o.remind_on),
    sponsored: o.sponsored === true,
    qty: num(o.qty) ?? num(o.new_qty),
    needs_approval: o.needs_approval === true,
  };
}

export function normalizeSet(raw: unknown, fallback: { phase: WhisperPhase; store_id: string }): WhisperSet {
  const o = (raw && typeof raw === "object" ? raw : {}) as Record<string, unknown>;
  const list = Array.isArray(raw) ? raw : Array.isArray(o.cards) ? o.cards : Array.isArray(o.suggestions) ? o.suggestions : [];
  const phase = (["planning", "in_trip", "checkout"] as const).find((p) => p === o.phase) ?? fallback.phase;
  return {
    phase,
    store_id: str(o.store_id) ?? fallback.store_id,
    // ≤3 per phase is the gateway's rule; the UI never shows more
    cards: list.map(normalizeCard).filter((c): c is WhisperCard => !!c).slice(0, 3),
    empty_reason: str(o.empty_reason),
    hidden_declined: num(o.hidden_declined) ?? 0,
  };
}

// ---------------------------------------------------------------------------------------------------- display

export const KIND_LABEL: Record<WhisperKind, string> = {
  complete: "Completes your list",
  replenish: "Running low",
  swap_save: "Swap and save",
  qty: "Fix the amount",
  deal: "A deal you'd like",
  remove: "Worth dropping",
  skip: "Skip it",
  wait: "Worth waiting",
};

export const ACCEPT_LABEL: Record<WhisperAction, string> = {
  add: "Add it",
  swap: "Swap it",
  set_qty: "Change it",
  remove: "Drop it",
  skip: "Skip it",
  remind: "Remind me",
};

const CODE_LABEL: Record<string, string> = {
  ALREADY_OWNED: "You already own one",
  INCOMPATIBLE: "Doesn't fit your device",
  PROMO_SOON: "Sale coming soon",
  INSTALLMENTS_LEFT: "Phone installments left",
  OVER_BUDGET: "Over your budget",
  COMPLETES_OCCASION: "Completes what you're making",
  QTY_MISMATCH: "Amount doesn't match",
  CHEAPER_SAME_NEED: "Cheaper for the same need",
  RUNNING_LOW: "You're probably out",
  DEAL_MATCH: "On sale and your taste",
  ALLERGEN: "Allergen in your profile",
  SHIPPING_WASTE: "Shipping costs more than the item",
  BOUGHT_RECENTLY: "Bought recently",
  DUPLICATE: "Already on your list",
  PRICE_ABOVE_TYPICAL: "Priced above typical",
  ON_ROUTE: "On the robot's route",
};
export const codeLabel = (code: string) => CODE_LABEL[code] ?? sentenceCase(code.replace(/_/g, " ").toLowerCase());

export const SOURCE_LABEL: Record<SignalSource, string> = {
  customer: "About you",
  commerce: "Prices and stock",
  physical: "In the store",
  other: "Other signals",
};

function sentenceCase(s: string) {
  return s ? s[0].toUpperCase() + s.slice(1) : s;
}

export function signalText(s: WhisperSignal): { label: string; value: string } {
  const label = s.label ?? sentenceCase(s.name.replace(/_cents$/, "").replace(/_/g, " "));
  const v = s.value;
  let value: string;
  if (typeof v === "boolean") value = v ? "yes" : "no";
  else if (typeof v === "number" && /_cents$/.test(s.name)) value = fmt(v);
  else if (typeof v === "number" && /_m$/.test(s.name)) value = `${Number.isInteger(v) || v >= 10 ? Math.round(v) : v.toFixed(1)} m`;
  else if (typeof v === "number" && /_days$|^days_/.test(s.name)) value = `${v} ${v === 1 ? "day" : "days"}`;
  else value = v == null ? "none" : String(v);
  return { label, value };
}

export function signalsBySource(signals: WhisperSignal[]) {
  const order: SignalSource[] = ["customer", "commerce", "physical", "other"];
  return order.map((src) => ({ source: src, rows: signals.filter((s) => s.source === src) })).filter((g) => g.rows.length);
}

/** "2026-10-23" → "Fri, Oct 23" (local noon, so no time-zone day shift). */
export function remindDate(d: string | null | undefined) {
  if (!d) return null;
  const t = new Date(/^\d{4}-\d{2}-\d{2}$/.test(d) ? `${d}T12:00:00` : d);
  return Number.isNaN(t.getTime()) ? d : t.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
}

/** The line a card acts on, if it is on the list / in the cart. */
export const lineSkuOf = (c: WhisperCard) => (c.action === "add" ? null : (c.line_sku ?? (c.action === "swap" ? null : c.sku)));

/** Saved (swap / qty / remove), avoided (skip / wait; WAIT is an estimate) and added, from accepted cards. */
export function tally(cards: WhisperCard[], outcomes: Record<string, WhisperOutcome>) {
  let saved = 0;
  let avoided = 0;
  let added = 0;
  let waitEstimate = false;
  for (const c of cards) {
    const o = outcomes[c.card_id];
    if (o?.decision !== "accept" || o.status === "denied") continue;
    if (c.action === "add") added += c.price_cents * (c.qty ?? 1);
    else if (c.kind === "skip" || c.kind === "wait") {
      avoided += c.savings_cents ?? 0;
      if (c.kind === "wait") waitEstimate = true;
    } else saved += c.savings_cents ?? 0;
  }
  return { saved, avoided, added, waitEstimate };
}

// ---------------------------------------------------------------------------------------------------- gateway calls

/** set once a gateway answered 404 / 405 / 501: every screen stays on its pre-Cart-Whisperer behaviour */
let endpointMissing = false;
export const whisperMissing = () => endpointMissing;
const isMissing = (e: unknown) => e instanceof GatewayError && (e.status === 404 || e.status === 405 || e.status === 501);

export type FetchResult = { kind: "ok"; set: WhisperSet } | { kind: "missing" } | { kind: "error"; error: string };

export async function fetchWhisper(
  q: { storeId: string; phase: WhisperPhase; tripId?: string | null; hints?: { skus?: string; intent?: string } },
  userId = maya.profile.user_id,
): Promise<FetchResult> {
  if (endpointMissing) return { kind: "missing" };
  try {
    // skus ("SKU:qty,…") / intent: optional hints for the planning pass before a trip exists (the gateway may ignore them)
    const hints = q.hints && !q.tripId ? { skus: q.hints.skus || undefined, intent: q.hints.intent?.slice(0, 300) || undefined } : {};
    const { data } = await gw.whisper(userId, { store_id: q.storeId, phase: q.phase, trip_id: q.tripId ?? undefined, ...hints });
    return { kind: "ok", set: normalizeSet(data, { phase: q.phase, store_id: q.storeId }) };
  } catch (e) {
    if (isMissing(e)) {
      endpointMissing = true;
      return { kind: "missing" };
    }
    return { kind: "error", error: errorText(e) };
  }
}

type AnswerResult = { ok: true; outcome: WhisperOutcome; raw: Record<string, unknown> } | { ok: false; stale: boolean; error: string };

/** POST accept / decline. The gateway applies an accept through the normal cart path (mandate check, approval when
 *  needed); the outcome text comes from its answer (mandate verdict, reminder date), never from guessing. */
export async function answerWhisper(card: WhisperCard, decision: "accept" | "decline", ctx: { storeId: string; tripId?: string | null; phase: WhisperPhase }, userId = maya.profile.user_id): Promise<AnswerResult> {
  // WhisperDecisionRequest {trip_id?, channel, note?}; without a trip_id the gateway uses the live trip (or none)
  const body = { channel: "web", ...(ctx.tripId ? { trip_id: ctx.tripId } : {}) };
  let data: unknown;
  try {
    try {
      data = (await gw.whisperAnswer(userId, card.card_id, decision, body)).data;
    } catch (e) {
      // a stricter or body-less endpoint answers 422: the same call without a body
      if (!(e instanceof GatewayError && e.status === 422)) throw e;
      data = (await gw.whisperAnswer(userId, card.card_id, decision)).data;
    }
  } catch (e) {
    if (isMissing(e) && e instanceof GatewayError && e.status !== 404) endpointMissing = true;
    const stale = e instanceof GatewayError && (e.status === 404 || e.status === 409 || e.status === 410);
    return { ok: false, stale, error: errorText(e) };
  }
  const raw = (data && typeof data === "object" ? data : {}) as Record<string, unknown>;
  if (String(raw.outcome ?? "") === "stale") return { ok: false, stale: true, error: str(raw.message) ?? "out of date" };
  return { ok: true, outcome: outcomeFrom(card, decision, raw), raw };
}

function outcomeFrom(card: WhisperCard, decision: "accept" | "decline", raw: Record<string, unknown>): WhisperOutcome {
  // WhisperDecisionResult.outcome: applied | needs_approval | denied | reminder_scheduled | acknowledged | declined |
  // stale | not_applied (no trip: recorded only); `message` is the gateway's shopper-facing line
  const outcome = String(raw.outcome ?? "").toLowerCase();
  const message = str(raw.message);
  const md = (raw.mandate_decision ?? null) as { verdict?: string; reasons?: { text?: string; message?: string }[] } | null;
  const verdict = String(md?.verdict ?? "").toLowerCase();
  if (decision === "decline")
    return { decision, status: "ok", text: card.kind === "skip" || card.kind === "wait" ? "Kept it. Picky won't ask about this again." : "Got it. Picky won't suggest this again." };
  if (outcome === "denied" || verdict === "deny") {
    const why = md?.reasons?.map((r) => r.text ?? r.message).find(Boolean);
    return { decision, status: "denied", text: message ?? `Your rules blocked this${why ? `: ${why}` : "."}` };
  }
  if (card.action === "remind" || outcome === "reminder_scheduled") {
    const reminder = (raw.reminder ?? null) as { remind_on?: string; price_cents?: number | null } | null;
    const on = reminder?.remind_on ?? card.remind_on;
    const day = remindDate(on);
    const price = num(reminder?.price_cents) ?? card.promo_price_cents;
    return { decision, status: "ok", remind_on: on ?? null, text: day ? `We'll remind you on ${day}${price ? `, when it should be ${fmt(price)}` : ""}.` : "We'll remind you when the price drops." };
  }
  if (outcome === "needs_approval" || raw.approval || verdict === "needs_approval")
    return { decision, status: "ask", text: message ?? "Picky will ask for your OK first, like any change over your ask-me line." };
  if (message && outcome === "applied") return { decision, status: "ok", text: message };
  const save = card.savings_cents ? ` Saves ${fmt(card.savings_cents)}.` : "";
  switch (card.action) {
    case "add":
      return { decision, status: "ok", text: `Added ${card.name}.` };
    case "swap":
      return { decision, status: "ok", text: `Swapped in ${card.name}.${save}` };
    case "set_qty":
      return { decision, status: "ok", text: `Changed the amount.${save}` };
    case "remove":
      return { decision, status: "ok", text: `Dropped ${card.name}.${save}` };
    default:
      return { decision, status: "ok", text: `Skipped ${card.name}.${card.savings_cents ? ` Avoids ${fmt(card.savings_cents)}.` : ""}` };
  }
}

// ---------------------------------------------------------------------------------------------------- planning (/order)

function lineFor(storeId: StoreId, sku: string, qty: number, unit_cents: number, note: string): ListLine {
  if (!productBySku[sku]) registerProduct(placeholderProduct({ sku, price_cents: unit_cents }));
  const inStore = (slotsOf(storeId)[sku]?.qty ?? 0) > 0;
  const onlineItem = online.items.find((i) => i.sku === sku);
  return {
    line_id: `ln_cw_${sku.toLowerCase()}`,
    sku,
    qty,
    unit_cents: inStore || !onlineItem ? unit_cents : onlineItem.price_cents,
    // a SKU this store doesn't stock can't become a robot stop (the route preview needs its slot)
    sourcing: inStore || !onlineItem ? "in_store" : "online",
    note,
  };
}

/** An accepted planning card applied to the local draft (the order preview, its route and its total). */
export function applyToDraft(d: Draft, c: WhisperCard): Draft {
  const storeId = d.store_id;
  const lines = [...d.lines];
  const at = (sku: string | null) => (sku ? lines.findIndex((l) => l.sku === sku) : -1);
  const target = lineSkuOf(c);
  switch (c.action) {
    case "add": {
      if (!c.sku || (!slotsOf(storeId)[c.sku] && !online.items.some((i) => i.sku === c.sku))) return d;
      const k = at(c.sku);
      if (k >= 0) lines[k] = { ...lines[k], qty: lines[k].qty + (c.qty ?? 1) };
      else lines.push(lineFor(storeId, c.sku, c.qty ?? 1, c.price_cents, "Added from Picky's advice"));
      break;
    }
    case "swap": {
      const k = at(c.line_sku);
      if (!c.sku || k < 0 || (!slotsOf(storeId)[c.sku] && !online.items.some((i) => i.sku === c.sku))) return d;
      lines[k] = { ...lineFor(storeId, c.sku, lines[k].qty, c.price_cents, `Swapped in by Picky's advice`), vegetarian: lines[k].vegetarian };
      break;
    }
    case "set_qty": {
      const k = at(target);
      if (k < 0 || c.qty == null) return d;
      if (c.qty <= 0) lines.splice(k, 1);
      else lines[k] = { ...lines[k], qty: c.qty };
      break;
    }
    case "remove":
    case "skip":
    case "remind": {
      // skip / wait on something that isn't on the list (the charger, the phone) only records the answer
      const k = at(target);
      if (k < 0) return d;
      if (c.action !== "remove" && c.qty != null && c.qty > 0) lines[k] = { ...lines[k], qty: c.qty };
      else lines.splice(k, 1);
      break;
    }
  }
  return { ...d, lines };
}

/** /order in gateway mode: the planning pass for the shown draft. status "missing" / "error" = keep the demo advice. */
export function usePlanningWhisper(draft: Draft, live: boolean) {
  const storeId = draft.store_id;
  const intent = draft.intent;
  const skus = draft.lines.map((l) => `${l.sku}:${l.qty}`).join(",");
  const planKey = `${storeId}|${intent}`;
  const [res, setRes] = useState<{ storeId: string; r: FetchResult } | null>(null);
  // answers belong to one plan: a new store or a re-plan starts clean
  const [answers, setAnswers] = useState<{ plan: string; map: Record<string, WhisperOutcome> }>({ plan: planKey, map: {} });
  const [busy, setBusy] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    if (!live) return;
    let cancelled = false;
    // debounced: an accepted card edits the list, and the next pass sees the edited list
    const timer = setTimeout(async () => {
      const r = await fetchWhisper({ storeId, phase: "planning", hints: { skus, intent } });
      if (!cancelled) setRes({ storeId, r });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [live, storeId, skus, intent, nonce]);

  // the previous pass for this store stays on screen while the next one loads
  const current = res && res.storeId === storeId ? res.r : null;
  const status: "idle" | "loading" | "ok" | "missing" | "error" = !live ? "idle" : whisperMissing() || current?.kind === "missing" ? "missing" : !current ? "loading" : current.kind;
  const outcomes = answers.plan === planKey ? answers.map : {};

  const answer = async (card: WhisperCard, decision: "accept" | "decline") => {
    setBusy(card.card_id);
    const r = await answerWhisper(card, decision, { storeId, phase: "planning" });
    setBusy(null);
    if (r.ok) setAnswers((a) => ({ plan: planKey, map: { ...(a.plan === planKey ? a.map : {}), [card.card_id]: r.outcome } }));
    return r;
  };

  return { status, set: current?.kind === "ok" ? current.set : null, outcomes, busy, answer, refresh: () => setNonce((n) => n + 1) };
}

// ---------------------------------------------------------------------------------------------------- trips (/watch, /trip)

type PhaseCards = WhisperSet & { seq: number };
type TripWhisperStore = {
  tripId: string | null;
  /** the gateway has Cart Whisperer: an event arrived or a GET answered */
  available: boolean;
  phases: Partial<Record<WhisperPhase, PhaseCards>>;
  /** every card seen this trip, so answered ones keep their outcome after the next pass replaces the set */
  seen: Record<string, WhisperCard>;
  outcomes: Record<string, WhisperOutcome>;
  busy: string | null;
  loaded: Partial<Record<WhisperPhase, boolean>>;
  bind: (tripId: string | null) => void;
  /** whisper.cards on the trip socket (replayed ones too: the newest seq per phase wins) */
  fromEvent: (tripId: string, raw: Record<string, unknown>, seq: number) => void;
  load: (tripId: string, storeId: string, phase: WhisperPhase) => Promise<void>;
  answer: (card: WhisperCard, decision: "accept" | "decline", ctx: { tripId: string; storeId: string; phase: WhisperPhase }) => Promise<void>;
};

const EMPTY = { available: false, phases: {}, seen: {}, outcomes: {}, busy: null, loaded: {} };

export const useTripWhisper = create<TripWhisperStore>((set, get) => ({
  tripId: null,
  ...EMPTY,

  bind: (tripId) => {
    if (get().tripId !== tripId) set({ tripId, ...EMPTY });
  },

  fromEvent: (tripId, raw, seq) => {
    get().bind(tripId);
    const s = normalizeSet(raw, { phase: "in_trip", store_id: String(raw.store_id ?? "") });
    const prev = get().phases[s.phase];
    if (prev && prev.seq > seq) return;
    set((st) => ({
      available: true,
      phases: { ...st.phases, [s.phase]: { ...s, seq } },
      seen: { ...st.seen, ...Object.fromEntries(s.cards.map((c) => [c.card_id, c])) },
    }));
  },

  load: async (tripId, storeId, phase) => {
    get().bind(tripId);
    if (get().loaded[phase] || whisperMissing()) return;
    set((st) => ({ loaded: { ...st.loaded, [phase]: true } }));
    const r = await fetchWhisper({ storeId, phase, tripId });
    if (get().tripId !== tripId || r.kind !== "ok") return;
    // an event that arrived meanwhile is newer than this GET
    if (get().phases[phase]) return set({ available: true });
    set((st) => ({
      available: true,
      phases: { ...st.phases, [phase]: { ...r.set, phase, seq: -1 } },
      seen: { ...st.seen, ...Object.fromEntries(r.set.cards.map((c) => [c.card_id, c])) },
    }));
  },

  answer: async (card, decision, ctx) => {
    if (get().busy) return;
    set({ busy: card.card_id });
    const r = await answerWhisper(card, decision, ctx);
    if (get().tripId !== ctx.tripId) return;
    set({ busy: null });
    if (!r.ok) {
      toast.error(r.stale ? "That suggestion is out of date, so Picky is checking again" : `Couldn't send that: ${r.error}`);
      if (r.stale) {
        set((st) => ({ loaded: { ...st.loaded, [ctx.phase]: false }, phases: { ...st.phases, [ctx.phase]: undefined } }));
        void get().load(ctx.tripId, ctx.storeId, ctx.phase);
      }
      return;
    }
    set((st) => ({ outcomes: { ...st.outcomes, [card.card_id]: r.outcome } }));
  },
}));
