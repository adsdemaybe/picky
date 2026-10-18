// Check grocery scenarios and the planner/replay used by the website without starting Next.
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import Module, { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import ts from "typescript";
const load = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const src = path.resolve(here, "../src");
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, parent, ...rest) {
  return originalResolve.call(this, request.startsWith("@/") ? path.join(src, request.slice(2)) : request, parent, ...rest);
};
Module._extensions[".ts"] = (module, filename) => {
  const { outputText } = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020, esModuleInterop: true },
    fileName: filename,
  });
  module._compile(outputText, filename);
};
const { GROCERY_TRIPS, groupFoodLines } = load("../src/lib/grocery-trips.ts");
const { expandIntent, defaultAccepted, draftTotal } = load("../src/lib/order.ts");
const { buildTripPlan } = load("../src/lib/trip-plan.ts");
const { STORES, productBySku, slotsOf } = load("../src/lib/data.ts");
const { useTrip } = load("../src/lib/trip.ts");

function checkPaths(plan) {
  const store = STORES[plan.storeId];
  const obstacles = [...store.fixtures.map((f) => f.rect), ...store.obstacles];
  for (const leg of plan.route.legs) {
    assert.deepEqual(leg.path[0], { x: plan.stops[leg.from].pose.x, y: plan.stops[leg.from].pose.y });
    assert.deepEqual(leg.path.at(-1), { x: plan.stops[leg.to].pose.x, y: plan.stops[leg.to].pose.y });
    for (let i = 1; i < leg.path.length; i++) {
      const a = leg.path[i - 1], b = leg.path[i];
      const samples = Math.max(1, Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) / 0.05));
      for (let j = 0; j <= samples; j++) {
        const x = a.x + (b.x - a.x) * j / samples, y = a.y + (b.y - a.y) * j / samples;
        assert(x >= 0 && x <= store.bounds.w && y >= 0 && y <= store.bounds.h, "path leaves store");
        assert(!obstacles.some((r) => x > r.x && x < r.x + r.w && y > r.y && y < r.y + r.h), `path crosses fixture: ${leg.from} to ${leg.to}`);
      }
    }
  }
}

async function main() {
  const paths = new Set();
  const distances = {};
  for (const scenario of GROCERY_TRIPS) {
    const draft = expandIntent(scenario.text);
    assert.equal(draft.trip_type, scenario.id);
    assert.equal(draft.lines.length, scenario.items.length);
    assert.equal(groupFoodLines(draft.lines).flatMap((g) => g.lines).length, draft.lines.length);
    assert.equal(new Set(draft.lines.map((l) => l.sku)).size, draft.lines.length);
    for (const line of [...draft.lines, ...draft.recs]) {
      assert(productBySku[line.sku], `unknown SKU ${line.sku}`);
      assert(Number.isInteger(line.unit_cents) && line.unit_cents >= 0);
    }
    const baseline = buildTripPlan(draft, []);
    const expected = draft.lines.filter((l) => l.sourcing === "in_store");
    assert.equal(baseline.route.order.length, expected.length + 2);
    checkPaths(baseline);
    paths.add(JSON.stringify(baseline.route.legs.map((l) => l.path)));
    distances[scenario.id] = Math.round(baseline.route.total_m);
    const accepted = [...defaultAccepted(draft)];
    const preview = buildTripPlan(draft, accepted);
    checkPaths(preview);
    for (const line of draft.lines.filter((l) => l.sourcing === "online")) {
      assert(!Object.values(preview.stops).some((stop) => stop.sku === line.sku), "online item has a shelf stop");
    }
    useTrip.getState().placeOrder(draft, accepted, "with_me");
    assert.equal(useTrip.getState().phase, "waiting_login");
    assert.deepEqual(useTrip.getState().route, preview.route, "preview and playback routes differ");
    useTrip.setState({ speed: 10000, autoAnswer: true });
    useTrip.getState().robotLogin();
    const deadline = Date.now() + 10000;
    while (!useTrip.getState().receipt && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 10));
    const result = useTrip.getState();
    assert(result.receipt, `${scenario.id} did not finish checkout`);
    assert.equal(result.receipt.total_cents, draftTotal(draft, new Set(accepted)).total);
    assert(expected.every((line) => result.cart.some((picked) => picked.sku === line.sku && picked.qty === line.qty && picked.verified)));
    useTrip.getState().reset();
  }
  assert.equal(paths.size, GROCERY_TRIPS.length, "trip types must have distinct paths");
  assert(distances.produce < distances.basics, "produce loop should be shorter than weekly restock");
  const vegetarian = expandIntent("all vegetarian tacos for 6 under $40");
  assert(!vegetarian.lines.some((line) => line.sku === "BEEF-GROUND"));
  assert(vegetarian.lines.some((line) => line.sku === "SOYRIZO"));
  assert.equal(expandIntent("breakfast under $23.50").budget_cents, 2350);
  for (const text of ["case and screen protector for my new Pixel 9, under $90", "earbuds for running, under $100"]) {
    const draft = expandIntent(text, "store_tmobile");
    const plan = buildTripPlan(draft, [...defaultAccepted(draft)]);
    assert(plan.route.legs.length > 0);
    assert(draft.lines.every((line) => slotsOf("store_tmobile")[line.sku]));
  }
  console.log("Grocery trips OK: five distinct obstacle-free routes; preview matches playback; all five trips reach an approved demo receipt.");
  console.table(distances);
}
main().catch((error) => { console.error(error); process.exitCode = 1; });
