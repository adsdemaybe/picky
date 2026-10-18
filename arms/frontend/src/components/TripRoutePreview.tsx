"use client";

import { MapPin, PackageCheck, Route } from "lucide-react";
import { useMemo, useState } from "react";
import { STORES, productBySku } from "@/lib/data";
import { departmentFor } from "@/lib/grocery-trips";
import type { Draft } from "@/lib/order";
import type { Mode } from "@/lib/trip";
import { buildTripPlan } from "@/lib/trip-plan";
import { cn } from "@/lib/utils";
import { MapLegend, StoreMap } from "./StoreMap";
import { Panel } from "./picky";

export function TripRoutePreview({ draft, accepted, mode }: { draft: Draft; accepted: Set<string>; mode: Mode }) {
  const [withExtras, setWithExtras] = useState(true);
  const plan = useMemo(() => buildTripPlan(draft, withExtras ? [...accepted] : [], mode), [draft, accepted, mode, withExtras]);
  const stops = plan.route.order.map((id) => plan.stops[id]).filter((stop) => stop.kind === "list" || stop.kind === "optional");
  const online = draft.lines.filter((line) => line.sourcing === "online");
  const extra = Math.max(0, plan.route.total_m - plan.baseline_m);
  const departments = new Set(stops.filter((stop) => stop.sku).map((stop) => departmentFor(productBySku[stop.sku!]).id));
  const store = STORES[draft.store_id];

  return (
    <section aria-labelledby="route-preview-title">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <h2 id="route-preview-title" className="flex items-center gap-2 text-[20px] font-semibold"><Route className="size-5 text-leaf" aria-hidden />Your path through the store</h2>
        <div className="flex rounded-full bg-muted p-1" role="group" aria-label="Preview route">
          {[false, true].map((extras) => (
            <button key={String(extras)} type="button" aria-pressed={withExtras === extras} onClick={() => setWithExtras(extras)} className={cn("min-h-10 cursor-pointer rounded-full px-3 text-[13px] font-medium", withExtras === extras && "bg-card text-leaf shadow-sm")}>
              {extras ? "With selected extras" : "List only"}
            </button>
          ))}
        </div>
      </div>
      <Panel className="overflow-hidden">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-1 border-b border-border px-5 py-3 text-[14px]" aria-live="polite">
          <span><strong className="font-heading text-[24px]">{plan.route.total_m.toFixed(0)} m</strong> planned walk</span>
          <span>{stops.length} shelf stops</span>
          {store.kind === "grocery" && <span>{departments.size} departments</span>}
          {extra > 0.5 && <span className="text-amber-ink">+{extra.toFixed(0)} m for extras</span>}
        </div>
        <div className="grid sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
          <div className="flex flex-col items-center bg-muted/20 p-4">
            <div className="h-[330px] w-full"><StoreMap preview={plan} /></div>
            <div className="mt-3"><MapLegend /></div>
          </div>
          <div className="border-t border-border p-5 sm:border-t-0 sm:border-l">
            <h3 className="text-[14px] font-semibold">In picking order</h3>
            <p className="mt-1 text-[12.5px] text-muted-foreground">Start at the robot dock, finish at {mode === "pickup" ? "pickup" : "checkout"}.</p>
            <ol className="mt-3 max-h-[280px] space-y-2 overflow-y-auto pr-1">
              {stops.map((stop, i) => (
                <li key={stop.id} className="flex items-start gap-2.5 text-[13px]">
                  <span className={cn("mt-0.5 grid size-6 shrink-0 place-items-center rounded-full text-[11px] font-semibold", stop.kind === "optional" ? "bg-amber-wash text-amber-ink" : "bg-leaf-wash text-leaf")}>{i + 1}</span>
                  <span className="min-w-0"><span className="block font-medium">{stop.label}{stop.kind === "optional" && <span className="ml-1 text-amber-ink">(extra)</span>}</span><span className="text-muted-foreground">{stop.qty} {stop.qty === 1 ? "unit" : "units"}{store.kind === "grocery" && stop.sku ? `, ${departmentFor(productBySku[stop.sku]).label.toLowerCase()}` : ""}</span></span>
                </li>
              ))}
            </ol>
            {online.length > 0 && <p className="mt-4 flex items-start gap-2 border-t border-border pt-3 text-[12.5px] text-grape"><PackageCheck className="mt-0.5 size-4 shrink-0" aria-hidden /><span>{online.map((line) => productBySku[line.sku].name).join(", ")} comes online and adds no shelf stop.</span></p>}
            {stops.length === 0 && <p className="mt-4 flex items-center gap-2 text-[13px] text-muted-foreground"><MapPin className="size-4" aria-hidden />No in-store picks for this list.</p>}
          </div>
        </div>
      </Panel>
    </section>
  );
}
