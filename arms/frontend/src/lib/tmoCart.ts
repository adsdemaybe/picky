// The T-Mobile basket, shared by the website (Shop) and the in-store robot (In store): start on your phone at home,
// keep going with Magenta at the store, check out at the counter or send the robot. Cart Whisperer reads this cart.
// Kept in this browser's localStorage (a per-viewer convenience) so a reload or a new tab keeps it. Hydrated after
// mount (hydrateTmoCart) so the server render and the first client render match.
import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";
import type { WLine } from "./whisperer";

type S = {
  cart: WLine[];
  intent: string;
  title: string;
  budget_cents: number;
  /** where the last change came from, so the other surface can say "picked up where you left off" */
  touched: "web" | "store" | null;
  set: (p: Partial<Pick<S, "cart" | "intent" | "title" | "budget_cents">>, from?: "web" | "store") => void;
  add: (sku: string, qty?: number, from?: "web" | "store") => void;
  setQty: (sku: string, qty: number, from?: "web" | "store") => void;
};

export const useTmoCart = create<S>()(
  persist(
    (set) => ({
      cart: [],
      intent: "",
      title: "Your T-Mobile basket",
      budget_cents: 10000,
      touched: null,
      set: (p, from = "web") => set({ ...p, touched: from }),
      add: (sku, qty = 1, from = "web") =>
        set((s) => ({
          touched: from,
          cart: s.cart.some((l) => l.sku === sku) ? s.cart.map((l) => (l.sku === sku ? { ...l, qty: l.qty + qty } : l)) : [...s.cart, { sku, qty }],
        })),
      setQty: (sku, qty, from = "web") =>
        set((s) => ({ touched: from, cart: qty <= 0 ? s.cart.filter((l) => l.sku !== sku) : s.cart.map((l) => (l.sku === sku ? { ...l, qty } : l)) })),
    }),
    {
      name: "picky.tmobile.basket",
      version: 1,
      storage: createJSONStorage(() => localStorage),
      partialize: ({ cart, intent, title, budget_cents, touched }) => ({ cart, intent, title, budget_cents, touched }),
      skipHydration: true,
    },
  ),
);

let hydrated: Promise<void> | null = null;
/** Load the saved basket once (safe to call from every page; storage errors just leave the empty basket). */
export function hydrateTmoCart(): Promise<void> {
  hydrated ??= Promise.resolve(useTmoCart.persist.rehydrate()).catch(() => {});
  return hydrated;
}
