import assert from "node:assert/strict";
import fs from "node:fs";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import Module, { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import ts from "typescript";

const load = createRequire(import.meta.url);
const src = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../src");
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, ...rest) {
  return originalResolve.call(this, request.startsWith("@/") ? path.join(src, request.slice(2)) : request, parent, ...rest);
};
Module._extensions[".ts"] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, "utf8"), { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true }, fileName: filename });
  module._compile(outputText, filename);
};
const { parseMemoryInput, MEMORY_TEXT_LIMIT } = load("../src/lib/memory/model.ts");
const { createMemoryRepository } = load("../src/lib/memory/server.ts");
const { memoryRecall, savedInformation } = load("../src/lib/memory/context.ts");
const { mergeTranscript, speechErrorMessage } = load("../src/lib/useVoiceInput.ts");

const directory = await mkdtemp(path.join(os.tmpdir(), "picky-memory-check-"));
try {
  const filename = path.join(directory, "notes.json");
  const repo = createMemoryRepository(filename);
  assert.deepEqual(await repo.list(), []);
  const input = { text: "  I like spicy food.  ", kind: "preference", source: "voice" };
  assert.equal(parseMemoryInput(input).text, "I like spicy food.");
  for (const invalid of [null, {}, { ...input, text: " " }, { ...input, text: "x".repeat(MEMORY_TEXT_LIMIT + 1) }, { ...input, kind: "system" }, { ...input, source: "unknown" }]) assert.throws(() => parseMemoryInput(invalid));
  const entry = await repo.add(input);
  assert.equal(entry.source, "voice");
  assert.equal(entry.user_id, "maya");
  assert(!("audio" in entry));
  const parallel = await Promise.all(Array.from({ length: 12 }, (_, i) => repo.add({ text: `Shopping note ${i}`, kind: "shopping", source: "typed" })));
  assert.equal((await repo.list()).length, 13, "concurrent writes lost a note");
  assert.equal(new Set(parallel.map((item) => item.id)).size, 12);
  const changed = await repo.update(entry.id, { ...input, text: "I prefer mild salsa." });
  assert.equal(changed.id, entry.id);
  assert.equal(changed.created_at, entry.created_at);
  const restarted = createMemoryRepository(filename);
  assert.equal((await restarted.list()).find((item) => item.id === entry.id).text, "I prefer mild salsa.");
  const entries = await restarted.list();
  assert(savedInformation(entries).entries.some((item) => item.text === "I prefer mild salsa."));
  assert(memoryRecall("What do you remember about me?", [changed]).text.includes("mild salsa"));
  assert(memoryRecall("What are my preferences?", []).text.includes("haven't saved"));
  assert(memoryRecall("What have I saved?", null).text.includes("couldn't load"));
  assert.equal(memoryRecall("How much is coffee?", entries), null);
  await restarted.remove(entry.id);
  assert(!(await repo.list()).some((item) => item.id === entry.id));
  assert(!memoryRecall("What do you remember?", await repo.list()).text.includes("mild salsa"));
  await assert.rejects(restarted.update("missing", input));
  await repo.add({ text: "Write still works after a failed operation", kind: "note", source: "typed" });
  await writeFile(filename, "corrupted data");
  await assert.rejects(repo.list());
  await assert.rejects(repo.add(input));
  assert.equal(await readFile(filename, "utf8"), "corrupted data", "bad storage was silently overwritten");
  const result = (text, isFinal = false) => ({ 0: { transcript: text }, isFinal });
  assert.equal(mergeTranscript("", [result("I like sp" )]), "I like sp");
  assert.equal(mergeTranscript("", [result("I like spicy food", true)]), "I like spicy food");
  assert.equal(mergeTranscript("Saved sentence.", [result("I like spicy food", true), result("and oat milk")]), "Saved sentence. I like spicy food and oat milk");
  assert(speechErrorMessage("not-allowed").includes("denied"));
  assert(speechErrorMessage("network").includes("transcript is still here"));
  console.log("Voice memory OK: validated inputs, atomic concurrent saves, restart persistence, edits/deletes, assistant recall, transcript replacement and microphone errors.");
} finally { await rm(directory, { recursive: true, force: true }); }
