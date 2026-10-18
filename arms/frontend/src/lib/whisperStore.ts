// Cart Whisperer session state shared by the T-Mobile shop and robot-trip pages: declines (never re-asked this
// trip), metrics (docs/cart-whisperer.md §10) and the reminders accepted WAIT cards scheduled.
import { create } from "zustand";
import { emptyMetrics, recordAccept, type Metrics, type Suggestion } from "./whisperer";

type S = {
  declined: string[];
  shownIds: string[];
  metrics: Metrics;
  reminders: { sku: string; on: string; text: string }[];
  /** every accept / decline, for the transparency log */
  log: { t: number; id: string; kind: string; title: string; outcome: "accepted" | "declined" }[];
  pass: (cards: Suggestion[]) => void;
  accept: (s: Suggestion) => void;
  decline: (s: Suggestion) => void;
  reset: () => void;
};

export const useWhisper = create<S>((set) => ({
  declined: [],
  shownIds: [],
  metrics: emptyMetrics(),
  reminders: [],
  log: [],
  pass: (cards) =>
    set((st) => {
      const fresh = cards.filter((c) => !st.shownIds.includes(c.id));
      return {
        shownIds: [...st.shownIds, ...fresh.map((c) => c.id)],
        metrics: { ...st.metrics, passes: st.metrics.passes + 1, zero_passes: st.metrics.zero_passes + (cards.length ? 0 : 1), shown: st.metrics.shown + fresh.length },
      };
    }),
  accept: (s) =>
    set((st) => ({
      metrics: recordAccept(st.metrics, s),
      reminders: s.action === "remind" && s.remind_on ? [...st.reminders, { sku: s.sku, on: s.remind_on, text: s.title }] : st.reminders,
      log: [...st.log, { t: Date.now(), id: s.id, kind: s.kind, title: s.title, outcome: "accepted" }],
      // an answered card is never re-asked in the same trip
      declined: [...st.declined, s.id],
    })),
  decline: (s) =>
    set((st) => ({
      declined: [...st.declined, s.id],
      metrics: { ...st.metrics, declined: st.metrics.declined + 1 },
      log: [...st.log, { t: Date.now(), id: s.id, kind: s.kind, title: s.title, outcome: "declined" }],
    })),
  reset: () => set({ declined: [], shownIds: [], metrics: emptyMetrics(), reminders: [], log: [] }),
}));
