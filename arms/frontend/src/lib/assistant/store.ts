"use client";
import { create } from "zustand";
import { useTrip } from "../trip";
import type { Card } from "./tools";

export type Msg = { id: number; role: "user" | "assistant"; text: string; cards?: Card[]; source?: string };

type AskStore = {
  open: boolean;
  busy: boolean;
  messages: Msg[];
  setOpen: (b: boolean) => void;
  ask: (text: string) => Promise<void>;
  clear: () => void;
};

let id = 0;
const hello: Msg = {
  id: 0,
  role: "assistant",
  text: "Hi Maya. Ask me if a price is good, where something is cheapest, where your money went this month, or whether a purchase fits your budget.",
};

export const useAsk = create<AskStore>((set, get) => ({
  open: false,
  busy: false,
  messages: [hello],
  setOpen: (open) => set({ open }),
  clear: () => set({ messages: [hello] }),
  ask: async (text) => {
    const q = text.trim();
    if (!q || get().busy) return;
    const user: Msg = { id: ++id, role: "user", text: q };
    set((s) => ({ open: true, busy: true, messages: [...s.messages, user] }));
    try {
      const history = get()
        .messages.filter((m) => m.id !== 0)
        .map((m) => ({ role: m.role, content: m.text }));
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ messages: history, extraPurchases: useTrip.getState().extraPurchases }),
      });
      const data = (await res.json()) as { text: string; cards: Card[]; source: string };
      set((s) => ({ messages: [...s.messages, { id: ++id, role: "assistant", text: data.text, cards: data.cards, source: data.source }] }));
    } catch {
      set((s) => ({ messages: [...s.messages, { id: ++id, role: "assistant", text: "I lost the connection for a second. Try that again?" }] }));
    } finally {
      set({ busy: false });
    }
  },
}));
