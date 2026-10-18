"use client";

import { Check, Route } from "lucide-react";
import { useMemo } from "react";
import { STORES } from "@/lib/data";
import { GROCERY_TRIPS, type GroceryTripId } from "@/lib/grocery-trips";
import { expandIntent } from "@/lib/order";
import { buildTripPlan } from "@/lib/trip-plan";
import { cn } from "@/lib/utils";

export function GroceryTrips({ selected, onSelect }: { selected?: GroceryTripId; onSelect: (text: string) => void }) {
  const previews = useMemo(() => GROCERY_TRIPS.map((trip) => {
    const draft = expandIntent(trip.text);
    return { ...trip, draft, plan: buildTripPlan(draft, []) };
  }), []);
  const store = STORES.store_ave;

  return (
    <section aria-labelledby="grocery-trips-title" className="mt-6">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="grocery-trips-title" className="text-[18px] font-semibold">What kind of grocery trip?</h2>
        <p className="text-[13px] text-muted-foreground">Different baskets, different paths through the same store</p>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-5" role="group" aria-label="Grocery trip types">
        {previews.map((trip) => {
          const active = trip.id === selected;
          const path = trip.plan.route.legs.flatMap((leg) => leg.path);
          return (
            <button
              key={trip.id} type="button" aria-pressed={active} onClick={() => onSelect(trip.text)}
              className={cn("relative flex cursor-pointer flex-col overflow-hidden rounded-2xl bg-card p-3 text-left ring-1 transition-colors hover:bg-muted/50 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ring", active ? "ring-2 ring-leaf" : "ring-foreground/[0.08]")}
            >
              {active && <span className="absolute top-3 right-3 grid size-6 place-items-center rounded-full bg-leaf text-white"><Check className="size-4" aria-hidden /></span>}
              <svg viewBox={`-0.5 -0.5 ${store.bounds.w + 1} ${store.bounds.h + 1}`} aria-hidden className="mb-3 h-28 w-full rounded-xl bg-muted/50">
                <g transform={`translate(0 ${store.bounds.h}) scale(1 -1)`}>
                  {store.fixtures.map((f) => <rect key={f.fixture_id} x={f.rect.x} y={f.rect.y} width={f.rect.w} height={f.rect.h} rx={0.12} fill="var(--border)" />)}
                  <path d={path.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ")} fill="none" stroke={trip.color} strokeWidth={0.28} strokeLinecap="round" strokeLinejoin="round" />
                  {Object.values(trip.plan.stops).filter((s) => s.kind === "list").map((stop) => <circle key={stop.id} cx={stop.pose.x} cy={stop.pose.y} r={0.4} fill={trip.color} stroke="var(--card)" strokeWidth={0.15} />)}
                </g>
              </svg>
              <span className="text-[15px] font-semibold">{trip.label}</span>
              <span className="mt-1 flex-1 text-[12.5px] leading-snug text-muted-foreground">{trip.description}</span>
              <span className="mt-3 flex items-center gap-1.5 text-[12px] font-medium"><Route className="size-3.5" aria-hidden />{trip.plan.route.total_m.toFixed(0)} m <span className="text-muted-foreground">/ {trip.draft.lines.length} products</span></span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
