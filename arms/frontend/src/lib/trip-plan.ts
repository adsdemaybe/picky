// The order preview and the replay share one plan, including exactly the same stop IDs and recommendation detours.
import { STORES, productBySku, slotsOf } from "./data";
import type { Draft } from "./order";
import { planRoute } from "./routing";
import type { Mode, Stop } from "./trip";

export function buildTripPlan(draft: Draft, accepted: readonly string[], mode: Mode = "with_me") {
  const selected = new Set(accepted);
  const store = STORES[draft.store_id];
  const slots = slotsOf(draft.store_id);
  const stops: Record<string, Stop> = {
    start: { id: "start", kind: "start", label: "Robot dock", pose: store.robot_home },
    end: { id: "end", kind: "end", label: mode === "pickup" ? "Pickup counter" : store.kind === "wireless" ? "Counter" : "Checkout", pose: store.checkout },
  };
  for (const line of draft.lines.filter((line) => line.sourcing === "in_store")) {
    const slot = slots[line.sku];
    stops[`s_${line.sku}`] = {
      id: `s_${line.sku}`, kind: "list", label: productBySku[line.sku].name, pose: slot.approach_pose,
      sku: line.sku, qty: line.qty, slot_id: slot.slot_id, unit_cents: line.unit_cents,
    };
  }
  for (const rec of draft.recs.filter((rec) => selected.has(rec.rec_id))) {
    const slot = slots[rec.sku];
    stops[`o_${rec.sku}`] = {
      id: `o_${rec.sku}`, kind: "optional", label: productBySku[rec.sku].name, pose: slot.approach_pose,
      sku: rec.sku, qty: 1, slot_id: slot.slot_id, rec_id: rec.rec_id, reason: rec.reason, unit_cents: rec.unit_cents,
    };
  }
  const middle = Object.values(stops).filter((stop) => stop.kind === "list" || stop.kind === "optional");
  const baseline = planRoute(store, stops.start, middle.filter((stop) => stop.kind === "list"), stops.end);
  const route = selected.size ? planRoute(store, stops.start, middle, stops.end) : baseline;
  const offRoute = middle.filter((stop) => stop.kind === "optional")
    .filter((stop) => route.total_m - planRoute(store, stops.start, middle.filter((s) => s.id !== stop.id), stops.end).total_m > 0.5)
    .map((stop) => stop.id);
  return {
    storeId: draft.store_id, stops, route, baseline_m: baseline.total_m,
    baselinePath: baseline.legs.flatMap((leg) => leg.path), offRoute,
    stopStatus: Object.fromEntries(Object.keys(stops).map((id) => [id, id === "start" ? "done" as const : "pending" as const])),
    robot: { ...store.robot_home, status: "idle" as const, speed: 0 }, activeStop: null,
  };
}

export type TripPlan = ReturnType<typeof buildTripPlan>;
