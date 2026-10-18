// Saved natural-language notes are shopper data, never payment authority or executable instructions.
export const MEMORY_KINDS = ["preference", "shopping", "note"] as const;
export type MemoryKind = (typeof MEMORY_KINDS)[number];
export const MEMORY_LABELS: Record<MemoryKind, string> = { preference: "Preference", shopping: "Shopping request", note: "Note" };
export const MEMORY_TEXT_LIMIT = 2000;
export const MEMORY_ENTRY_LIMIT = 100;

export type SavedMemory = {
  id: string;
  user_id: "maya";
  text: string;
  kind: MemoryKind;
  source: "voice" | "typed";
  created_at: string;
  updated_at: string;
};
export type MemoryInput = Pick<SavedMemory, "text" | "kind" | "source">;

export function parseMemoryInput(value: unknown): MemoryInput {
  if (!value || typeof value !== "object") throw new Error("Enter something for Picky to remember.");
  const input = value as Record<string, unknown>;
  if (typeof input.text !== "string" || !input.text.trim()) throw new Error("Enter something for Picky to remember.");
  if (input.text.trim().length > MEMORY_TEXT_LIMIT) throw new Error(`Keep each note under ${MEMORY_TEXT_LIMIT} characters.`);
  if (!MEMORY_KINDS.includes(input.kind as MemoryKind)) throw new Error("Choose a valid type of information.");
  if (input.source !== "voice" && input.source !== "typed") throw new Error("Choose a valid input source.");
  return { text: input.text.trim(), kind: input.kind as MemoryKind, source: input.source };
}
