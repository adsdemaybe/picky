"use client";
import { ArrowLeft, Bot, Check, ShoppingBasket, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useRef } from "react";
import { useAsk } from "@/lib/assistant/store";
import { groupFoodLines } from "@/lib/grocery-trips";
import { productBySku } from "@/lib/data";
import { fmt } from "@/lib/money";
import { guideTotals, useTripGuide } from "@/lib/trip-guide";
import { Money, Panel, ProductTile } from "./picky";
import { Badge } from "./ui/badge";
import { Button } from "./ui/button";
import { cn } from "@/lib/utils";

export function TripGuide() {
  const guide = useTripGuide();
  const { phase, draft } = guide;
  if (!draft || !["review", "addons", "confirm"].includes(phase)) return null;
  const totals = guideTotals(draft, guide.selected);
  const step = phase === "review" ? 1 : phase === "addons" ? 2 : 3;
  return (
    <Panel className="my-4 overflow-hidden border-2 border-leaf/40" aria-label="Confirm your shopping trip">
      <div className="flex items-center justify-between gap-2 border-b border-border bg-leaf-wash px-4 py-3">
        <span className="flex items-center gap-2 text-[14px] font-semibold text-leaf"><ShoppingBasket className="size-4" aria-hidden />{draft.title}</span>
        <Badge variant="leaf">Step {step} of 3</Badge>
      </div>
      <div className="p-4">
        <ol className="mb-4 flex gap-2 text-[11.5px]" aria-label="Trip confirmation steps">{["Review list", "Choose add-ons", "Confirm start"].map((label, i) => <li key={label} aria-current={step === i + 1 ? "step" : undefined} className={cn("flex-1 rounded-lg px-2 py-2 text-center", step >= i + 1 ? "bg-leaf-wash font-semibold text-leaf" : "bg-muted text-muted-foreground")}>{i + 1}. {label}</li>)}</ol>
        <p className="text-[16px] font-semibold" aria-live="polite">{phase === "review" ? "Does this list look right?" : phase === "addons" ? "Anything else before the robot goes?" : "Ready to send the robot?"}</p>
        {phase === "review" && <>
          <p className="mt-1 text-[13px] text-muted-foreground">{draft.lines.length} products, about {fmt(totals.list + totals.shipping)} before extras. Budget: {fmt(draft.budget_cents)}.</p>
          <div className="mt-3 max-h-64 space-y-3 overflow-y-auto pr-1">{groupFoodLines(draft.lines).map((department) => <div key={department.id}><h3 className="text-[12px] font-semibold text-muted-foreground">{department.label}</h3><ul className="mt-1 space-y-1">{department.lines.map((line) => <li key={line.line_id} className="flex items-start justify-between gap-3 text-[13px]"><span>{line.qty} × {productBySku[line.sku].name}{line.sourcing === "online" && <span className="ml-1 text-grape">(online)</span>}</span><Money cents={line.qty * line.unit_cents} className="shrink-0" /></li>)}</ul></div>)}</div>
          <Button className="mt-4 w-full" onClick={guide.review}><Check className="size-4" />Yes, choose add-ons</Button>
        </>}
        {phase === "addons" && <>
          <p className="mt-1 text-[13px] text-muted-foreground">Extras are optional. Choose any you want, or continue with just your list.</p>
          <div className="mt-3 space-y-2">{draft.recs.map((rec, i) => {
            const product = productBySku[rec.sku];
            const checked = guide.selected.includes(rec.rec_id);
            return <label key={rec.rec_id} className={cn("flex cursor-pointer items-start gap-3 rounded-xl border p-3", checked ? "border-leaf bg-leaf-wash" : "border-border")}>
              <input type="checkbox" checked={checked} onChange={() => guide.toggle(rec.rec_id)} className="mt-1 size-4 shrink-0 accent-leaf" />
              <ProductTile color={product.label_color} category={product.category} name={product.name} size={34} />
              <span className="min-w-0 flex-1"><span className="block text-[13px] font-semibold">{i + 1}. {product.name} <Money cents={rec.unit_cents} /></span><span className="mt-1 block text-[12px] text-muted-foreground">{rec.reason}</span><span className="mt-1 block text-[11px] text-muted-foreground">{rec.sponsored ? "Sponsored" : "Not sponsored"}</span></span>
            </label>;
          })}</div>
          <Button className="mt-4 w-full" onClick={guide.addons}>{guide.selected.length ? `Review with ${guide.selected.length} add-on${guide.selected.length > 1 ? "s" : ""}` : "No extras, continue"}</Button>
        </>}
        {phase === "confirm" && <>
          <dl className="mt-3 space-y-1 text-[14px]">
            <div className="flex justify-between"><dt>Your list</dt><dd><Money cents={totals.list} /></dd></div>
            <div className="flex justify-between"><dt>Add-ons ({guide.selected.length})</dt><dd><Money cents={totals.recs} /></dd></div>
            {totals.shipping > 0 && <div className="flex justify-between"><dt>Shipping</dt><dd><Money cents={totals.shipping} /></dd></div>}
            <div className="flex justify-between border-t border-border pt-2 font-semibold"><dt>Estimated total</dt><dd><Money cents={totals.total} /></dd></div>
          </dl>
          <p className="mt-2 text-[12.5px] text-muted-foreground">{guide.selected.length ? `Extras: ${draft.recs.filter((rec) => guide.selected.includes(rec.rec_id)).map((rec) => productBySku[rec.sku].name).join(", ")}.` : "No add-ons selected."}</p>
          {totals.total > draft.budget_cents && <p className="mt-2 rounded-lg bg-tomato-wash p-2 text-[13px] font-medium text-tomato">This is {fmt(totals.total - draft.budget_cents)} over your {fmt(draft.budget_cents)} budget. Go back to remove extras, or confirm this higher estimate.</p>}
          <fieldset className="mt-4 space-y-2"><legend className="mb-2 text-[13px] font-semibold">How should it shop?</legend>{([ ["pickup", "Shop for me; I'll pick up"], ["with_me", "Wait for me at the store"] ] as const).map(([mode, label]) => <label key={mode} className="flex min-h-10 cursor-pointer items-center gap-2 rounded-lg bg-muted px-3 text-[13px]"><input type="radio" name="guided-fulfillment" checked={guide.mode === mode} onChange={() => guide.setMode(mode)} className="accent-leaf" />{label}</label>)}</fieldset>
          {totals.decision.reasons.filter((reason) => reason.level === "block").map((reason) => <p key={reason.code + reason.text} className="mt-2 text-[13px] text-tomato">{reason.text}</p>)}
          <p className="mt-3 text-[12.5px] text-muted-foreground">Confirming starts this shopping trip. Payment still waits for your separate checkout approval.</p>
          <Button className="mt-4 w-full" disabled={totals.decision.verdict === "deny"} onClick={guide.start}><Bot className="size-4" />Confirm and {guide.mode === "pickup" ? "start shopping" : "prepare my trip"}</Button>
        </>}
        {guide.error && <p role="alert" className="mt-3 text-[13px] text-tomato">{guide.error}</p>}
        <div className="mt-3 flex items-center justify-between">
          {phase !== "review" ? <Button variant="ghost" size="sm" onClick={guide.back}><ArrowLeft className="size-3.5" />Back</Button> : <span className="text-[11.5px] text-muted-foreground">The robot is waiting for your confirmation.</span>}
          <Button variant="ghost" size="sm" onClick={guide.cancel}><X className="size-3.5" />Cancel trip</Button>
        </div>
      </div>
    </Panel>
  );
}

// A single navigation listener handles confirmations made through buttons, chat or voice.
export function TripGuideNavigation() {
  const version = useTripGuide((state) => state.startVersion);
  const handled = useRef(version);
  const router = useRouter();
  useEffect(() => {
    if (version <= handled.current) return;
    handled.current = version;
    useAsk.getState().setOpen(false);
    router.push("/trip");
  }, [version, router]);
  return null;
}
