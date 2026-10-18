// Local demo persistence. Single Node process, serialized mutations, atomic file replacement.
// No audio is written. The file is ignored by Git; a server deployment needs a persistent volume.
import { randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { MEMORY_ENTRY_LIMIT, parseMemoryInput, type SavedMemory } from "./model";

export function createMemoryRepository(filename: string) {
  let queue: Promise<unknown> = Promise.resolve();
  const read = async (): Promise<SavedMemory[]> => {
    try {
      const data: unknown = JSON.parse(await readFile(filename, "utf8"));
      if (!Array.isArray(data)) throw new Error("Invalid saved information file.");
      return data.map((entry) => {
        const input = parseMemoryInput(entry);
        if (typeof entry.id !== "string" || entry.user_id !== "maya" || typeof entry.created_at !== "string" || typeof entry.updated_at !== "string") throw new Error("Invalid saved information entry.");
        return { id: entry.id, user_id: "maya", ...input, created_at: entry.created_at, updated_at: entry.updated_at };
      });
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return [];
      throw error; // Don't silently overwrite a corrupted or unreadable file.
    }
  };
  const mutate = <T>(operation: (entries: SavedMemory[]) => T): Promise<T> => {
    const result = queue.then(async () => {
      const entries = await read();
      const output = operation(entries);
      await mkdir(path.dirname(filename), { recursive: true, mode: 0o700 });
      const temporary = `${filename}.${randomUUID()}.tmp`;
      await writeFile(temporary, JSON.stringify(entries, null, 2) + "\n", { mode: 0o600 });
      await rename(temporary, filename);
      return output;
    });
    queue = result.catch(() => undefined);
    return result;
  };
  return {
    async list() { await queue; return read(); },
    add(value: unknown) {
      const input = parseMemoryInput(value);
      return mutate((entries) => {
        if (entries.length >= MEMORY_ENTRY_LIMIT) throw new Error("Your saved information is full. Delete a note before adding another.");
        const ts = new Date().toISOString();
        const entry: SavedMemory = { ...input, id: randomUUID(), user_id: "maya", created_at: ts, updated_at: ts };
        entries.unshift(entry);
        return entry;
      });
    },
    update(id: string, value: unknown) {
      const input = parseMemoryInput(value);
      return mutate((entries) => {
        const index = entries.findIndex((entry) => entry.id === id);
        if (index < 0) throw new Error("That note no longer exists. Refresh your saved information.");
        const updated = { ...entries[index], ...input, updated_at: new Date().toISOString() };
        entries[index] = updated;
        return updated;
      });
    },
    remove(id: string) {
      return mutate((entries) => {
        const index = entries.findIndex((entry) => entry.id === id);
        if (index < 0) throw new Error("That note no longer exists. Refresh your saved information.");
        entries.splice(index, 1);
      });
    },
  };
}

// Reuse the queue across dev hot reloads, so two API handlers can't overwrite each other's writes.
const globalMemory = globalThis as typeof globalThis & { pickyMemory?: ReturnType<typeof createMemoryRepository> };
export const memoryRepository = globalMemory.pickyMemory ??= createMemoryRepository(path.join(process.cwd(), ".data", "shopper-memory.json"));
