"use client";
import { Bot, Hand } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { toast } from "sonner";
import TripPage from "@/app/trip/view";
import { Panel } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { productBySku } from "@/lib/data";
import { useTrip } from "@/lib/trip";
import { suggest, unitPrice, type Suggestion, type WLine } from "@/lib/whisperer";
import { useWhisper } from "@/lib/whisperStore";
import { MetricsStrip, WhisperList } from "../bits";
import { usePhrasing } from "../phrasing";

// In-trip demo beat (docs/cart-whisperer.md §11b): the shopper asks the robot to grab the $9.99 clearance case too.
const TEMPT = "CASE-PX7-BLK";

export default function TmoTrip() {
  const t = useTrip();
  const w = useWhisper();
  const [extra, setExtra] = useState<WLine[]>([]);
  const [setAside, setSetAside] = useState<string[]>([]);
  const ours = t.storeId === "store_tmobile" && t.phase !== "idle" && !!t.draft;

  const cart: WLine[] = useMemo(() => {
    const picked = t.cart.map((l) => ({ sku: l.sku, qty: l.qty }));
    const planned = (t.draft?.lines ?? []).filter((l) => !picked.some((p) => p.sku === l.sku)).map((l) => ({ sku: l.sku, qty: l.qty }));
    return [...picked, ...planned, ...extra].filter((l) => !setAside.includes(l.sku));
  }, [t.cart, t.draft, extra, setAside]);
  const phase = t.phase === "checkout" || t.phase === "done" ? "checkout" : "in_trip";
  const raw = useMemo(
    () => (ours ? suggest({ cart, intent: t.draft?.intent, budget_cents: t.draft?.budget_cents ?? 10000, phase, declined: new Set(w.declined), last_picked: t.cart.at(-1)?.sku }) : []),
    [ours, cart, t.draft, phase, w.declined, t.cart],
  );
  const { cards } = usePhrasing(raw);

  if (!ours)
    return (
      <div className="mx-auto flex max-w-[560px] flex-col items-center py-16 text-center">
        <Bot className="size-16 text-magenta" aria-hidden />
        <h1 className="mt-4 text-[32px] font-bold">Magenta is at the dock</h1>
        <p className="mt-2 text-[15px] text-muted-foreground">Plan a T-Mobile trip and watch the robot drive the store, pick from the back stock and the accessory wall, and ask before anything extra.</p>
        <Button asChild size="lg" className="mt-6 bg-magenta text-white hover:bg-magenta-deep">
          <Link href="/tmobile/shop">Plan a T-Mobile trip</Link>
        </Button>
      </div>
    );

  const accept = (s: Suggestion) => {
    w.accept(s);
    if (s.action === "remove" || s.action === "remind") {
      setSetAside((x) => [...x, s.sku]);
      setExtra((e) => e.filter((l) => l.sku !== s.sku));
      toast(t.cart.some((l) => l.sku === s.sku) ? "Magenta will set it aside at the counter" : "Taken off the list");
    } else if (s.action === "add") {
      setExtra((e) => [...e, { sku: s.sku, qty: s.qty }]);
      toast("Added as an optional stop. Your rules still check it at checkout.");
    }
  };

  return (
    <div className="flex flex-col gap-6 pb-16">
      <Panel className="border-l-4 border-magenta p-5">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h2 className="text-[20px] font-semibold">{phase === "checkout" ? "Before you pay" : "Cart Whisperer, live"}</h2>
            <p className="text-[13.5px] text-muted-foreground">Re-checks every time the cart changes or the robot picks something. At most 3 cards; none is fine.</p>
          </div>
          {!extra.some((l) => l.sku === TEMPT) && !setAside.includes(TEMPT) && phase !== "checkout" && (
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setExtra((e) => [...e, { sku: TEMPT, qty: 1 }]);
                toast(`You: "grab the ${fmtPrice(TEMPT)} clearance case too"`);
              }}
            >
              <Hand className="size-4" aria-hidden /> &quot;Grab the {fmtPrice(TEMPT)} clearance case too&quot;
            </Button>
          )}
        </div>
        <div className="mt-4">
          <WhisperList cards={cards} onAccept={accept} onDecline={(s) => (w.decline(s), toast("OK, keeping it"))} empty={phase === "checkout" ? "Nothing to change before you pay." : "Nothing worth adding right now."} />
        </div>
        <div className="mt-4">
          <MetricsStrip m={w.metrics} />
        </div>
        {w.reminders.length > 0 && (
          <p className="mt-3 text-[13px] text-muted-foreground">Reminders: {w.reminders.map((r) => `${productBySku[r.sku]?.name ?? r.sku} on ${r.on}`).join(" · ")}</p>
        )}
      </Panel>
      <TripPage />
    </div>
  );
}

const fmtPrice = (sku: string) => `$${(unitPrice(sku) / 100).toFixed(2)}`;
