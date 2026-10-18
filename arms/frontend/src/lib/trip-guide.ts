"use client";
// Explicit pre-trip gates. A proposal never moves the robot; only the final confirmation may place an order.
import { create } from "zustand";
import { productBySku } from "./data";
import { GROCERY_TRIPS } from "./grocery-trips";
import { fmt } from "./money";
import { draftTotal, expandIntent, mandateCheck, type Draft } from "./order";
import { useTrip, type Mode } from "./trip";

export type GuidePhase = "idle" | "review" | "addons" | "confirm" | "started" | "cancelled";
const YES = /^(?:yes|yeah|yep|sure|ok(?:ay)?|confirm(?:ed)?|approve|looks good|go ahead|do it|start(?: shopping)?|send (?:it|the robot))(?: please)?[.!\s]*$/i;
const NO = /^(?:no(?: thanks)?|nope|nah|cancel(?: it| the trip)?|stop|never mind|nevermind|not now)[.!\s]*$/i;
const WAKE = /^(?:hey\s+)?(?:voice|picky)[,!:]?\s*/i;

export function tripRequest(text: string): Draft | null {
  const request = text.trim().replace(WAKE, "");
  if (/^(?:remember|save|what|why|how|when|don't|do not|cancel|stop)\b/i.test(request)) return null;
  const scenario = [...GROCERY_TRIPS.filter((trip) => trip.id !== "basics"), GROCERY_TRIPS[1]].find((trip) => trip.matches.test(request));
  const action = /\b(?:let'?s|lets|let us|do|plan|start|need|want|order|shop|restock|refill|get|weekly basics|breakfast run|taco night|fresh produce|pantry refill)\b/i.test(request);
  return scenario && action ? expandIntent(request) : null;
}

export function addonReply(draft: Draft, text: string): string[] | null {
  const reply = text.toLowerCase().trim();
  if (/^(?:no(?: thanks)?(?: extras| add[ -]?ons)?|none|skip(?: (?:them|extras|add[ -]?ons))?|just (?:the )?list|list only)[.!\s]*$/.test(reply)) return [];
  if (/^(?:yes[, ]+)?(?:all(?: of them)?|both)(?: please)?[.!\s]*$/.test(reply)) return draft.recs.map((rec) => rec.rec_id);
  const clauses = reply.split(/[,;]|\band\b|\bbut\b/);
  const selected = new Set<string>();
  let understood = false;
  for (const clause of clauses) {
    const negative = /\b(?:no|not|skip|without|don't|do not|remove)\b/.test(clause);
    draft.recs.forEach((rec, i) => {
      const words = productBySku[rec.sku].name.toLowerCase().match(/[a-z]+/g)?.filter((word) => word.length > 3 && !["plain", "fresh", "style", "original"].includes(word)) ?? [];
      if (words.some((word) => new RegExp(`\\b${word}\\b`).test(clause)) || new RegExp(`\\b${i + 1}\\b`).test(clause)) {
        understood = true;
        if (!negative) selected.add(rec.rec_id);
      }
    });
  }
  return understood ? [...selected] : null;
}

export function guideTotals(draft: Draft, selected: string[]) {
  const totals = draftTotal(draft, new Set(selected));
  const lines = [...draft.lines, ...draft.recs.filter((rec) => selected.includes(rec.rec_id))];
  return { ...totals, decision: mandateCheck(totals.total, lines, draft.store_id) };
}

function confirmation(draft: Draft, selected: string[], mode: Mode) {
  const totals = guideTotals(draft, selected);
  const extras = draft.recs.filter((rec) => selected.includes(rec.rec_id)).map((rec) => productBySku[rec.sku].name);
  const over = Math.max(0, totals.total - draft.budget_cents);
  return `Ready to send the robot for ${draft.title.toLowerCase()}? Your list${extras.length ? ` plus ${extras.join(" and ")}` : ", with no add-ons,"} comes to about ${fmt(totals.total)}${over ? `, ${fmt(over)} above your ${fmt(draft.budget_cents)} budget` : ` within your ${fmt(draft.budget_cents)} budget`}. ${mode === "pickup" ? "It will shop for pickup." : "It will wait for you to log in at the store."} Say “confirm” to start, or “cancel”. Payment still needs a separate approval.`;
}

type TripGuide = {
  phase: GuidePhase;
  draft: Draft | null;
  selected: string[];
  mode: Mode;
  prompt: string;
  error: string | null;
  reviewConfirmed: boolean;
  startVersion: number;
  handle: (text: string) => string | null;
  review: () => void;
  toggle: (recId: string) => void;
  addons: () => void;
  setMode: (mode: Mode) => void;
  back: () => void;
  cancel: () => void;
  start: () => boolean;
  reset: () => void;
};

export const useTripGuide = create<TripGuide>((set, get) => ({
  phase: "idle", draft: null, selected: [], mode: "pickup", prompt: "", error: null, reviewConfirmed: false, startVersion: 0,
  handle: (text) => {
    const reply = text.trim().replace(WAKE, "");
    const state = get();
    if (["review", "addons", "confirm"].includes(state.phase)) {
      if ((state.phase !== "addons" && NO.test(reply)) || /^(?:cancel|stop|never mind|nevermind)\b/i.test(reply)) {
        state.cancel(); return get().prompt;
      }
      if (/^(?:go )?back[.!\s]*$/i.test(reply)) { state.back(); return get().prompt; }
      if (state.phase === "review" && YES.test(reply)) { state.review(); return get().prompt; }
      if (state.phase === "addons" && state.draft) {
        const choices = !state.draft.recs.length && YES.test(reply) ? [] : addonReply(state.draft, reply);
        if (choices !== null) { set({ selected: choices }); get().addons(); return get().prompt; }
      }
      if (state.phase === "confirm" && YES.test(reply)) { state.start(); return get().error ?? get().prompt; }
      // Don't guess which add-ons "yes" means, and don't start a different trip in the middle of a confirmation.
      const clarification = state.phase === "addons" ? "Which add-ons would you like? Name them or say “no extras”. You can also choose below." : state.phase === "review" ? "Does the proposed list look right? Say “yes” to choose add-ons, or “cancel”. You can also use the buttons below." : confirmation(state.draft!, state.selected, state.mode);
      set({ prompt: clarification });
      return clarification;
    }
    const draft = tripRequest(text);
    if (!draft) return null;
    const total = draftTotal(draft, new Set()).total;
    const prompt = `Let's plan your ${draft.title.toLowerCase()}: ${draft.lines.length} products, about ${fmt(total)} before extras. Does this list look right? Say “yes” to see add-ons, or “cancel”.`;
    set({ phase: "review", draft, selected: [], mode: "pickup", prompt, error: null, reviewConfirmed: false });
    return prompt;
  },
  review: () => {
    const { phase, draft } = get();
    if (phase !== "review" || !draft) return;
    const suggestions = draft.recs.map((rec, i) => `${i + 1}. ${productBySku[rec.sku].name} (${fmt(rec.unit_cents)})`).join("; ");
    set({ phase: "addons", reviewConfirmed: true, error: null, prompt: suggestions ? `Would you like any add-ons? ${suggestions}. Name your picks, say “both” or “no extras”. ${get().selected.length ? "Your previous selections are still checked." : "Nothing is selected yet."}` : "No extra suggestions for this trip. Continue to the final confirmation?" });
  },
  toggle: (recId) => {
    const { phase, draft, selected } = get();
    if (phase !== "addons" || !draft?.recs.some((rec) => rec.rec_id === recId)) return;
    set({ selected: selected.includes(recId) ? selected.filter((id) => id !== recId) : [...selected, recId], error: null });
  },
  addons: () => {
    const { phase, draft, selected, mode, reviewConfirmed } = get();
    if (phase !== "addons" || !draft || !reviewConfirmed) return;
    set({ phase: "confirm", error: null, prompt: confirmation(draft, selected, mode) });
  },
  setMode: (mode) => {
    const { phase, draft, selected } = get();
    if (phase !== "confirm" || !draft) return;
    set({ mode, prompt: confirmation(draft, selected, mode), error: null });
  },
  back: () => {
    const { phase, draft } = get();
    if (!draft) return;
    if (phase === "confirm") { set({ phase: "review" }); get().review(); }
    else if (phase === "addons") set({ phase: "review", reviewConfirmed: false, prompt: "Review your shopping list. Does it look right? Say “yes” to choose add-ons, or “cancel”." });
  },
  cancel: () => set({ phase: "cancelled", draft: null, selected: [], reviewConfirmed: false, error: null, prompt: "Cancelled this proposal. No new trip was started." }),
  start: () => {
    const state = get();
    if (state.phase !== "confirm" || !state.draft || !state.reviewConfirmed) return false;
    if (["waiting_login", "shopping", "checkout"].includes(useTrip.getState().phase)) { set({ error: "A trip is already in progress. Finish it before sending another order." }); return false; }
    const totals = guideTotals(state.draft, state.selected);
    if (totals.decision.verdict === "deny") { set({ error: "This order is blocked by your spending rules. Go back and change the add-ons or cancel." }); return false; }
    // Consume the confirmation before invoking the replay: double clicks and repeated spoken answers cannot start twice.
    set({ phase: "started", prompt: state.mode === "pickup" ? "Confirmed. The robot is starting your trip. Checkout will ask before payment." : "Confirmed. Your trip is ready; log in to the robot to start shopping.", error: null });
    useTrip.setState({ autoAnswer: false });
    useTrip.getState().placeOrder(state.draft, state.selected, state.mode);
    set({ startVersion: state.startVersion + 1 });
    return true;
  },
  reset: () => set({ phase: "idle", draft: null, selected: [], mode: "pickup", prompt: "", error: null, reviewConfirmed: false }),
}));
