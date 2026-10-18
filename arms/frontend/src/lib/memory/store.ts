"use client";
import { create } from "zustand";
import type { MemoryInput, SavedMemory } from "./model";

type MemoryStore = {
  entries: SavedMemory[];
  loaded: boolean;
  loading: boolean;
  saving: boolean;
  error: string | null;
  load: () => Promise<void>;
  save: (input: MemoryInput, id?: string) => Promise<boolean>;
  remove: (id: string) => Promise<boolean>;
};
async function request(body: object, method: string) {
  const response = await fetch("/api/memory", { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error ?? "Couldn't save your information.");
  return data;
}
export const useMemory = create<MemoryStore>((set, get) => ({
  entries: [], loaded: false, loading: false, saving: false, error: null,
  load: async () => {
    if (get().loading) return;
    set({ loading: true, error: null });
    try {
      const response = await fetch("/api/memory", { cache: "no-store" });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error ?? "Couldn't load your information.");
      set({ entries: data.entries, loaded: true });
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Couldn't load your information." });
    } finally { set({ loading: false }); }
  },
  save: async (input, id) => {
    if (get().saving || get().loading) return false;
    set({ saving: true, error: null });
    try {
      const { entry } = await request({ ...input, ...(id ? { id } : {}) }, id ? "PATCH" : "POST");
      set((state) => ({ entries: id ? state.entries.map((old) => old.id === id ? entry : old) : [entry, ...state.entries] }));
      return true;
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Couldn't save your information." });
      return false;
    } finally { set({ saving: false }); }
  },
  remove: async (id) => {
    if (get().saving || get().loading) return false;
    set({ saving: true, error: null });
    try {
      await request({ id }, "DELETE");
      set((state) => ({ entries: state.entries.filter((entry) => entry.id !== id) }));
      return true;
    } catch (error) {
      set({ error: error instanceof Error ? error.message : "Couldn't delete your information." });
      return false;
    } finally { set({ saving: false }); }
  },
}));
