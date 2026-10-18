"use client";
import { create } from "zustand";
import { useTripGuide } from "../trip-guide";
import { useTrip } from "../trip";
import type { Card } from "./tools";

export type Msg = { id: number; role: "user" | "assistant"; text: string; cards?: Card[]; source?: string };

type AskStore = {
  open: boolean;
  busy: boolean;
  messages: Msg[];
  setOpen: (b: boolean) => void;
  ask: (text: string) => Promise<void>;
  /** Spoken question: base64 WAV → Gemini hears it and answers; the browser transcript is only a fallback. */
  askAudio: (wavB64: string, transcript?: string) => Promise<void>;
  clear: () => void;
};

let id = 0;
const hello: Msg = {
  id: 0,
  role: "assistant",
  text: "Hi Maya. Ask me if a price is good, where something is cheapest, what to skip or wait on, where your money went this month, or whether a purchase fits your budget.",
};

type Send = (get: () => AskStore, set: (partial: Partial<AskStore> | ((s: AskStore) => Partial<AskStore>)) => void, user: Msg, body: Record<string, unknown>, spoken?: boolean) => Promise<void>;
const send: Send = async (get, set, user, body, spoken) => {
  set((s) => ({ open: true, busy: true, messages: [...s.messages, user] }));
  try {
    const history = get()
      .messages.filter((m) => m.id !== 0)
      .map((m) => ({ role: m.role, content: m.text }));
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ messages: history, extraPurchases: useTrip.getState().extraPurchases, ...body }),
    });
    const data = (await res.json()) as { text: string; cards: Card[]; source: string };
    set((s) => ({ messages: [...s.messages, { id: ++id, role: "assistant", text: data.text, cards: data.cards, source: data.source }] }));
    if (spoken && data.text && typeof window !== "undefined" && "speechSynthesis" in window) {
      try {
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(data.text);
        u.lang = "en-US";
        window.speechSynthesis.speak(u);
      } catch { /* the text answer is still on screen */ }
    }
  } catch {
    set((s) => ({ messages: [...s.messages, { id: ++id, role: "assistant", text: "I lost the connection for a second. Try that again?" }] }));
  } finally {
    set({ busy: false });
  }
};

export const useAsk = create<AskStore>((set, get) => ({
  open: false,
  busy: false,
  messages: [hello],
  setOpen: (open) => set({ open }),
  clear: () => { useTripGuide.getState().reset(); set({ messages: [hello] }); },
  ask: async (text) => {
    const q = text.trim();
    if (!q || get().busy) return;
    const user: Msg = { id: ++id, role: "user", text: q };
    const tripReply = useTripGuide.getState().handle(q);
    if (tripReply) {
      set((state) => ({ open: true, messages: [...state.messages, user, { id: ++id, role: "assistant", text: tripReply, source: "offline" }] }));
      return;
    }
    await send(get, set, user, {});
  },
  askAudio: async (wavB64, transcript) => {
    if (!wavB64 || get().busy) return;
    const user: Msg = { id: ++id, role: "user", text: transcript?.trim() || "🎤 Spoken question" };
    await send(get, set, user, { audio_b64: wavB64 }, true);
  },
}));
