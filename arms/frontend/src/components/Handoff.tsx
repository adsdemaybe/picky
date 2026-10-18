"use client";
// Checkout handoff, shared by /trip and the projector page (/watch): the SO-101 arm takes each unit out of the basket
// and sets it on the counter before the payment agent runs. Gateway trips follow the loop's `handoff` step (and the
// robot's basket in robot.state); the replay animates the same thing unit by unit. Tiles fly basket → gripper →
// counter (shared layout ids), big enough for a projector.
import { ArrowRight, Check, Hand, PackageOpen, ShoppingBasket } from "lucide-react";
import { LayoutGroup, motion } from "motion/react";
import { Panel, ProductTile } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { productBySku } from "@/lib/data";
import { useTrip, type HandoffUnit } from "@/lib/trip";
import { cn } from "@/lib/utils";

export function HandoffPanel({ className }: { className?: string }) {
  const h = useTrip((s) => s.handoff);
  const source = useTrip((s) => s.source);
  const robot = useTrip((s) => s.robotName);
  if (!h) return null;
  const basket = h.units.filter((u) => u.state === "basket");
  const arm = h.units.find((u) => u.state === "arm") ?? null;
  const counter = h.units.filter((u) => u.state === "counter");
  const done = h.status === "done";
  const who = robot ?? "The robot";
  const driver = h.tier ?? (source === "gateway" ? "VLA loop" : "Demo replay");

  if (done)
    return (
      <Panel className={cn("flex flex-wrap items-center gap-3 p-3.5", className)} aria-label="Checkout handoff">
        <span className="grid size-9 shrink-0 place-items-center rounded-full bg-leaf text-white">
          <Check className="size-5" strokeWidth={3} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-semibold">
            {counter.length} item{counter.length === 1 ? "" : "s"} on the counter, basket empty
          </div>
          <div className="text-[12.5px] text-muted-foreground">The arm unloaded everything at checkout ({driver}).</div>
        </div>
        <div className="flex -space-x-2">
          {counter.slice(0, 6).map((u) => (
            <Tile key={u.key} u={u} size={30} className="ring-2 ring-card" />
          ))}
        </div>
      </Panel>
    );

  return (
    <Panel className={cn("@container p-4 ring-2 ring-sky", className)} aria-label="Checkout handoff" aria-live="polite">
      <div className="flex flex-wrap items-center gap-2">
        <PackageOpen className="size-5 text-sky" aria-hidden />
        <h2 className="text-[18px] leading-tight font-semibold">Unloading at the counter</h2>
        <Badge variant="sky">SO-101 arm · {driver}</Badge>
        <span className="tabular ml-auto text-[14px] font-semibold">
          {counter.length} of {h.units.length}
        </span>
      </div>
      <p className="mt-1 text-[14px] text-muted-foreground">{who} takes each item out of the basket and sets it on the checkout counter.</p>
      <LayoutGroup id="handoff">
        <div className="mt-3 grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-stretch gap-2">
          <Column title="Basket" icon={ShoppingBasket} units={basket} empty="Empty" />
          <div className="flex w-[84px] flex-col items-center justify-center gap-1.5 rounded-xl bg-sky-wash px-1 py-2" aria-label={arm ? `In the gripper: ${nameOf(arm.sku)}` : "Gripper"}>
            <span className="flex items-center gap-1 text-[11.5px] font-semibold text-sky">
              <Hand className="size-3.5" aria-hidden /> Gripper
            </span>
            <span className="grid min-h-[52px] place-items-center">
              {arm ? (
                <motion.span animate={{ y: [0, -5, 0] }} transition={{ duration: 0.9, repeat: Infinity, ease: "easeInOut" }} className="block">
                  <Tile u={arm} size={48} />
                </motion.span>
              ) : (
                <span className="size-12 rounded-lg border-2 border-dashed border-sky/30" />
              )}
            </span>
            <ArrowRight className="size-4 text-sky" aria-hidden />
          </div>
          <Column title="Counter" icon={Check} units={counter} empty="Nothing yet" good />
        </div>
      </LayoutGroup>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-muted" aria-hidden>
        <motion.div className="h-full rounded-full bg-leaf" animate={{ width: `${(counter.length / Math.max(1, h.units.length)) * 100}%` }} transition={{ duration: 0.4 }} />
      </div>
    </Panel>
  );
}

function Column({ title, icon: Icon, units, empty, good }: { title: string; icon: typeof Check; units: HandoffUnit[]; empty: string; good?: boolean }) {
  return (
    <div className={cn("min-w-0 rounded-xl p-2", good ? "bg-leaf-wash" : "bg-muted")}>
      <div className={cn("mb-1.5 flex items-center gap-1 text-[12.5px] font-semibold", good ? "text-leaf-deep" : "text-muted-foreground")}>
        <Icon className="size-3.5" aria-hidden /> {title}
        <span className="tabular ml-auto">{units.length}</span>
      </div>
      <ul className="flex min-h-[52px] flex-wrap content-start gap-1.5">
        {units.length === 0 && <li className="self-center text-[12.5px] text-muted-foreground">{empty}</li>}
        {units.map((u) => (
          <li key={u.key}>
            <Tile u={u} size={40} />
          </li>
        ))}
      </ul>
    </div>
  );
}

const nameOf = (sku: string) => productBySku[sku]?.name ?? sku;

function Tile({ u, size, className }: { u: HandoffUnit; size: number; className?: string }) {
  const p = productBySku[u.sku];
  return (
    <motion.span layoutId={`unit-${u.key}`} transition={{ type: "spring", stiffness: 260, damping: 26 }} className={cn("block rounded-lg", className)} title={nameOf(u.sku)}>
      <ProductTile color={p?.label_color ?? "#cfd6cf"} category={p?.category ?? "pantry"} name={nameOf(u.sku)} size={size} />
    </motion.span>
  );
}
