import assert from "node:assert/strict";
import fs from "node:fs";
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
const { useTripGuide: guide, tripRequest, addonReply, guideTotals } = load("../src/lib/trip-guide.ts");
const { useTrip: trip } = load("../src/lib/trip.ts");
const { useAsk: ask } = load("../src/lib/assistant/store.ts");
const { expandIntent } = load("../src/lib/order.ts");
const reset = () => { guide.getState().reset(); trip.getState().reset(); };
const send = (text) => guide.getState().handle(text);

assert.equal(tripRequest("Voice, let's do a weekly restock").trip_type, "basics");
assert.equal(tripRequest("Picky, plan breakfast for the week").trip_type, "breakfast");
assert.equal(tripRequest("start a pantry refill").trip_type, "pantry");
for (const text of ["What is a weekly restock?", "Remember I usually do a weekly restock", "don't do a weekly restock", "is coffee a good price?"]) assert.equal(tripRequest(text), null);
const weekly = expandIntent("weekly restock");
const yogurt = weekly.recs.find((rec) => /YOGURT/.test(rec.sku)).rec_id;
const bread = weekly.recs.find((rec) => /BREAD/.test(rec.sku)).rec_id;
assert.deepEqual(addonReply(weekly, "add the yogurt"), [yogurt]);
assert.deepEqual(addonReply(weekly, "both"), [yogurt, bread]);
assert.deepEqual(addonReply(weekly, "no yogurt, add the sourdough"), [bread]);
assert.deepEqual(addonReply(weekly, "yogurt but not bread"), [yogurt]);
assert.deepEqual(addonReply(weekly, "no extras"), []);
assert.equal(addonReply(weekly, "yes"), null, "an ambiguous yes must not select every add-on");

reset();
assert.equal(guide.getState().start(), false);
send("Voice, let's do a weekly restock");
assert.equal(guide.getState().phase, "review");
assert.equal(trip.getState().phase, "idle");
assert.equal(guide.getState().start(), false, "review must not start robot");
send("yes please");
assert.equal(guide.getState().phase, "addons");
assert.deepEqual(guide.getState().selected, [], "add-ons must be opt-in");
assert.equal(guide.getState().start(), false, "add-ons must not start robot");
send("yes");
assert.equal(guide.getState().phase, "addons");
send("add the yogurt");
assert.equal(guide.getState().phase, "confirm");
assert.deepEqual(guide.getState().selected, [yogurt]);
assert.equal(trip.getState().phase, "idle");
assert(guide.getState().prompt.includes("Yogurt"));
const totals = guideTotals(guide.getState().draft, guide.getState().selected);
assert(totals.recs > 0);
assert(guide.getState().prompt.includes((totals.total / 100).toFixed(2)));
guide.getState().setMode("with_me");
send("confirm");
assert.equal(guide.getState().phase, "started");
assert.equal(trip.getState().phase, "waiting_login");
assert.equal(trip.getState().autoAnswer, false);
assert(Object.values(trip.getState().stops).some((stop) => stop.rec_id === yogurt));
assert(!Object.values(trip.getState().stops).some((stop) => stop.rec_id === bread));
const version = guide.getState().startVersion;
assert.equal(guide.getState().start(), false);
assert.equal(guide.getState().startVersion, version, "repeated confirmation started twice");
// A second proposal can't replace the trip already waiting at the store.
guide.getState().reset();
send("let's do breakfast"); send("yes"); send("no extras");
assert.equal(guide.getState().start(), false);
assert(guide.getState().error.includes("already in progress"));
assert.equal(trip.getState().draft.trip_type, "basics");

for (const target of ["review", "addons", "confirm"]) {
  reset(); send("let's do a weekly restock");
  if (target !== "review") send("yes");
  if (target === "confirm") send("no extras");
  send("cancel");
  assert.equal(guide.getState().phase, "cancelled");
  assert.equal(guide.getState().start(), false);
  assert.equal(trip.getState().phase, "idle");
}
reset(); send("let's do a weekly restock"); send("yes"); send("both");
guide.getState().back();
assert.equal(guide.getState().phase, "addons");
assert.deepEqual(guide.getState().selected, [yogurt, bread]);
guide.getState().toggle(yogurt); guide.getState().addons();
assert.deepEqual(guide.getState().selected, [bread]);
const denied = { ...guide.getState().draft, lines: [{ ...weekly.lines[0], unit_cents: 100000 }] };
guide.setState({ draft: denied });
assert.equal(guide.getState().start(), false, "mandate denial must block dispatch");
assert.equal(trip.getState().phase, "idle");

reset();
let networkCalls = 0;
const originalFetch = globalThis.fetch;
globalThis.fetch = async () => { networkCalls++; throw new Error("Unexpected network call"); };
try {
  await ask.getState().ask("Voice, let's do a weekly restock");
  await ask.getState().ask("yes");
  await ask.getState().ask("no extras");
  assert.equal(guide.getState().phase, "confirm");
  assert.equal(networkCalls, 0, "guided chat must work offline");
  assert.equal(trip.getState().phase, "idle");
} finally { globalThis.fetch = originalFetch; }

// Confirm a pickup, let it reach checkout, and verify payment is still gated.
trip.setState({ speed: 10000, autoAnswer: true });
send("confirm");
assert.equal(trip.getState().autoAnswer, false, "guided dispatch must turn off unattended approvals");
const deadline = Date.now() + 10000;
while (Date.now() < deadline) {
  const current = trip.getState();
  if (current.approval?.kind === "checkout") break;
  if (current.approval?.kind === "pick") current.answer(current.approval.candidates[0].instance_id);
  else if (current.approval?.kind === "detour") current.answer("no");
  await new Promise((resolve) => setTimeout(resolve, 10));
}
assert.equal(trip.getState().approval?.kind, "checkout");
assert.equal(trip.getState().receipt, null, "dispatch approval must not authorize payment");
trip.getState().answer("decline");
reset();
console.log("Guided trips OK: voice/chat recognition, list approval, opt-in add-ons, final confirmation, cancellation, back, mandate denial, duplicate/active-trip guards and separate checkout approval.");
