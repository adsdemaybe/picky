"use client";
import { Bot, Minus, Plus, Search, Send, Sparkles, Store, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { toast } from "sonner";
import { Money, Panel, ProductTile, SectionTitle } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { linePromoFor, mayaDevices, tmobileProducts } from "@/data/stores/tmobile";
import { slotsOf } from "@/lib/data";
import { fmt } from "@/lib/money";
import { mandateCheck, type Draft } from "@/lib/order";
import { useTrip } from "@/lib/trip";
import { cn } from "@/lib/utils";
import { applySuggestion, cartTotal, suggest, targetDevice, unitPrice, type Suggestion, type WLine } from "@/lib/whisperer";
import { useTmoCart } from "@/lib/tmoCart";
import { useWhisper } from "@/lib/whisperStore";
import { MetricsStrip, niceDate, WhisperList } from "../bits";
import { usePhrasing } from "../phrasing";

const PRESETS = [
  { label: "New Pixel 9 setup", text: "case and screen protector for my new Pixel 9, under $100" },
  { label: "Running earbuds", text: "earbuds for running, under $100" },
  { label: "Upgrade to Galaxy S26", text: "I want the Galaxy S26, under $1100" },
  { label: "Just a charger", text: "a USB-C wall charger, under $30" },
];

const CATS = [
  { id: "phone", label: "Phones" },
  { id: "case", label: "Cases" },
  { id: "screen", label: "Screen" },
  { id: "charger", label: "Power" },
  { id: "audio", label: "Audio" },
  { id: "wearable", label: "Watches" },
  { id: "tablet", label: "Tablets" },
  { id: "home_internet", label: "Home internet" },
  { id: "connected", label: "Trackers & SIM" },
];

/** Deterministic stand-in for agent.interpret_request on the demo requests (same shape the gateway would return). */
function expand(text: string): { cart: WLine[]; budget: number; title: string } {
  const t = text.toLowerCase();
  const budget = Number(t.match(/\$\s?(\d[\d,]*)/)?.[1]?.replace(/,/g, "") ?? 100) * 100;
  if (t.includes("earbud") || t.includes("running")) return { cart: [{ sku: "BUDS-RUN", qty: 1 }], budget, title: "Running earbuds" };
  if (t.includes("charger") && !t.includes("pixel 9")) return { cart: [{ sku: "CHG-20W", qty: 1 }], budget, title: "Wall charger" };
  const dev = targetDevice({ cart: [], intent: text });
  if (t.includes("case") || t.includes("screen") || t.includes("protector")) {
    const pick = (cat: string) =>
      tmobileProducts.filter((p) => p.category === cat && (p.compatible_with ?? []).includes(dev.sku) && (slotsOf("store_tmobile")[p.sku]?.qty ?? 0) > 0).sort((a, b) => unitPrice(a.sku) - unitPrice(b.sku))[0];
    const cart: WLine[] = [];
    if (t.includes("case") && pick("case")) cart.push({ sku: pick("case")!.sku, qty: 1 });
    if ((t.includes("screen") || t.includes("protector")) && pick("screen")) cart.push({ sku: pick("screen")!.sku, qty: 1 });
    return { cart, budget, title: `${dev.name} setup` };
  }
  if (dev.isNew) return { cart: [{ sku: dev.sku, qty: 1 }], budget, title: `New ${dev.name}` };
  return { cart: [], budget, title: "T-Mobile trip" };
}

export default function TmoShop() {
  const { cart, intent, title, budget_cents: budget, touched } = useTmoCart();
  const [text, setText] = useState(intent || PRESETS[0].text);
  const setCart = (f: (c: WLine[]) => WLine[]) => useTmoCart.getState().set({ cart: f(useTmoCart.getState().cart) }, "web");
  const setBudget = (b: number) => useTmoCart.getState().set({ budget_cents: b }, "web");
  const [fromStore] = useState(() => touched === "store" && cart.length > 0);
  const [cat, setCat] = useState("case");
  const [q, setQ] = useState("");
  const w = useWhisper();
  const declined = useMemo(() => new Set(w.declined), [w.declined]);
  const placeOrder = useTrip((s) => s.placeOrder);
  const router = useRouter();

  const raw = useMemo(() => suggest({ cart, intent, budget_cents: budget, phase: "planning", declined }), [cart, intent, budget, declined]);
  const { cards, source } = usePhrasing(raw);
  const total = cartTotal(cart);
  const dev = targetDevice({ cart, intent });
  const verdict = mandateCheck(total, cart.map((l) => ({ sku: l.sku, unit_cents: unitPrice(l.sku) })), "store_tmobile");

  // one Cart Whisperer pass per cart change (debounced like the live trip, §1 "When it runs")
  const key = JSON.stringify([cart, intent, budget, w.declined.length]);
  const last = useRef("");
  useEffect(() => {
    const id = setTimeout(() => {
      if (last.current === key) return;
      last.current = key;
      useWhisper.getState().pass(raw);
    }, 250);
    return () => clearTimeout(id);
  }, [key, raw]);

  const run = (t: string) => {
    const e = expand(t);
    setText(t);
    useTmoCart.getState().set({ intent: t, cart: e.cart, budget_cents: e.budget, title: e.title }, "web");
    useWhisper.getState().reset();
  };
  // first visit: start from the demo's opening request (a basket from the store or an earlier visit is kept)
  useEffect(() => {
    // one-time seed of the shared basket store on mount, not derived state
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (useTmoCart.getState().touched === null) run(PRESETS[0].text);
  }, []);
  const add = (sku: string) => {
    useTmoCart.getState().add(sku, 1, "web");
    toast(`Added ${tmobileProducts.find((p) => p.sku === sku)?.name ?? sku}`, { duration: 1500 });
  };
  const setQty = (sku: string, qty: number) => useTmoCart.getState().setQty(sku, qty, "web");

  const accept = (s: Suggestion) => {
    w.accept(s);
    setCart((c) => applySuggestion(c, s));
    if (s.action === "remind") toast(`I'll text you on ${niceDate(s.remind_on!)}`, { description: s.title });
    else if (s.action === "none") toast("Noted. Nothing added.");
  };
  const decline = (s: Suggestion) => {
    w.decline(s);
    // "Buy anyway" / "Buy now" on a SKIP / WAIT keeps the line (it goes through the mandate like any add)
    toast(s.kind === "skip" || s.kind === "wait" ? "Kept in your cart. The mandate still checks it at checkout." : "Got it, I won't suggest that again this trip.");
  };

  const send = (mode: "with_me" | "pickup") => {
    if (!cart.length) return toast("Your cart is empty");
    const draft: Draft = {
      store_id: "store_tmobile",
      intent,
      title,
      budget_cents: budget,
      lines: cart.map((l) => ({ line_id: `ln_${l.sku.toLowerCase()}`, sku: l.sku, qty: l.qty, unit_cents: unitPrice(l.sku), sourcing: "in_store" as const })),
      recs: [],
      advice: cards.filter((c) => c.kind === "skip" || c.kind === "wait").map((c) => ({ kind: c.kind as "skip" | "wait", sku: c.sku, text: c.reason, save_cents: c.avoided_cents })),
    };
    placeOrder(draft, [], mode);
    router.push("/tmobile/trip");
  };

  const shelf = tmobileProducts
    .filter((p) => (q ? `${p.name} ${p.brand} ${p.tags.join(" ")}`.toLowerCase().includes(q.toLowerCase()) : p.category === cat))
    .sort((a, b) => Number((b.compatible_with ?? []).includes(dev.sku)) - Number((a.compatible_with ?? []).includes(dev.sku)) || unitPrice(a.sku) - unitPrice(b.sku));

  return (
    <div className="flex flex-col gap-6 pb-16">
      <section>
        <h1 className="text-[34px] leading-tight font-bold md:text-[42px]">What are you shopping for?</h1>
        <p className="mt-1 text-[15px] text-muted-foreground">
          Say it like you would to a store rep. Cart Whisperer reads your cart, your line and the store, and tells you what to add, what to skip, and when to wait.
        </p>
        <form
          className="mt-4 flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (text.trim()) run(text.trim());
          }}
        >
          <div className="relative flex-1">
            <Sparkles className="pointer-events-none absolute top-3 left-4 size-[18px] text-magenta" aria-hidden />
            <input value={text} onChange={(e) => setText(e.target.value)} aria-label="Your request" className="h-11 w-full rounded-full bg-card pr-4 pl-11 text-[15px] ring-1 ring-foreground/10 outline-none focus:ring-2 focus:ring-magenta" />
          </div>
          <Button type="submit" className="h-11 rounded-full bg-magenta px-5 text-white hover:bg-magenta-deep">
            Plan it
          </Button>
        </form>
        <div className="mt-3 flex flex-wrap gap-2">
          {PRESETS.map((p) => (
            <button key={p.label} onClick={() => run(p.text)} className={cn("cursor-pointer rounded-full px-3 py-1.5 text-[13px] font-medium ring-1", intent === p.text ? "bg-magenta-wash text-magenta ring-magenta" : "ring-foreground/10 hover:bg-muted")}>
              {p.label}
            </button>
          ))}
        </div>
      </section>

      {fromStore && (
        <p className="-mt-2 rounded-xl bg-magenta-wash px-4 py-2 text-[13.5px] font-medium text-magenta-deep">Picked up where you left off with Magenta in the store.</p>
      )}
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_420px] lg:grid-rows-[auto_1fr]">
          <Panel className="p-5 lg:col-start-1">
            <SectionTitle action={<span className="text-[13px] text-muted-foreground">for your {dev.name}{dev.isNew ? " (new)" : ""}</span>}>{title}</SectionTitle>
            {cart.length === 0 && <p className="text-[14px] text-muted-foreground">Nothing in the cart yet. Add something from the store below.</p>}
            <ul className="divide-y divide-border">
              {cart.map((l) => {
                const p = tmobileProducts.find((x) => x.sku === l.sku)!;
                const deal = slotsOf("store_tmobile")[l.sku]?.deal;
                const promo = linePromoFor(l.sku);
                return (
                  <li key={l.sku} className="flex flex-wrap items-start gap-x-3 gap-y-1 py-3">
                    <ProductTile color={p.label_color} category={p.category} name={p.name} size={40} />
                    <div className="min-w-0 flex-1">
                      <div className="line-clamp-2 text-[14.5px] leading-tight font-semibold">{p.name}</div>
                      <div className="text-[12.5px] text-muted-foreground">{p.brand}</div>
                      {deal && <div className="text-[12.5px] font-semibold text-leaf-deep">{deal.reason}</div>}
                      {promo && <div className="text-[12.5px] text-muted-foreground">Line offer: {promo.headline}{promo.requires ? ` (needs a ${promo.requires.replace(/_/g, " ")})` : ""}</div>}
                    </div>
                    {/* phones: quantity + price on their own row under the name; wider screens: inline */}
                    <div className="flex w-full items-center justify-between pl-[52px] sm:w-auto sm:gap-3 sm:pl-0">
                      <div className="flex items-center gap-1">
                        <Button size="icon" variant="ghost" className="size-7" onClick={() => setQty(l.sku, l.qty - 1)} aria-label="Fewer">
                          {l.qty === 1 ? <Trash2 className="size-3.5" /> : <Minus className="size-3.5" />}
                        </Button>
                        <span className="w-5 text-center text-[14px] tabular">{l.qty}</span>
                        <Button size="icon" variant="ghost" className="size-7" onClick={() => setQty(l.sku, l.qty + 1)} aria-label="More">
                          <Plus className="size-3.5" />
                        </Button>
                      </div>
                      <Money cents={l.qty * unitPrice(l.sku)} className="w-[72px] shrink-0 text-right text-[14.5px] font-semibold" />
                    </div>
                  </li>
                );
              })}
            </ul>
            <div className="mt-3 flex items-center justify-between border-t border-border pt-3">
              <span className="text-[14px] text-muted-foreground">
                Budget{" "}
                <input
                  type="number"
                  value={budget / 100}
                  min={0}
                  onChange={(e) => setBudget(Math.max(0, Math.round(Number(e.target.value) * 100)))}
                  className="ml-1 w-20 rounded-md bg-muted px-2 py-0.5 text-foreground tabular"
                  aria-label="Budget in dollars"
                />
              </span>
              <span className={cn("text-[18px] font-bold tabular", total > budget && "text-tomato")}>
                {fmt(total)} <span className="text-[13px] font-medium text-muted-foreground">of {fmt(budget)}</span>
              </span>
            </div>
          </Panel>

        <aside className="flex flex-col gap-4 lg:sticky lg:top-28 lg:col-start-2 lg:row-span-2 lg:row-start-1 lg:self-start">
          <div>
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-[20px] font-semibold">Cart Whisperer</h2>
              <span className="text-[11.5px] text-muted-foreground">{source === "claude" ? "Reasons phrased by Claude, checked by code" : "Deterministic reasons"}</span>
            </div>
            <WhisperList cards={cards} onAccept={accept} onDecline={decline} />
          </div>

          <Panel className="p-4">
            <div className="text-[13px] font-semibold">Your rules (checked in code before checkout)</div>
            <ul className="mt-2 space-y-1 text-[13px]">
              {verdict.reasons.map((r) => (
                <li key={r.code + r.text} className="flex gap-2">
                  <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", r.level === "ok" ? "bg-leaf" : r.level === "ask" ? "bg-amber" : "bg-tomato")} aria-hidden />
                  {r.text}
                </li>
              ))}
            </ul>
            <div className="mt-4 grid grid-cols-2 gap-2">
              <Button onClick={() => send("with_me")} className="bg-magenta text-white hover:bg-magenta-deep" disabled={verdict.verdict === "deny"}>
                <Bot className="size-4" aria-hidden /> Shop with robot
              </Button>
              <Button variant="outline" onClick={() => send("pickup")} disabled={verdict.verdict === "deny"}>
                <Send className="size-4" aria-hidden /> Robot picks it up
              </Button>
            </div>
            {verdict.verdict === "deny" && <p className="mt-2 text-[12.5px] text-tomato">Over your $150 robot-trip cap, so the robot can&apos;t buy this for you. Drop something, or finish in the store and pay yourself.</p>}
            <Button asChild variant="ghost" className="mt-2 w-full text-magenta hover:bg-magenta-wash hover:text-magenta-deep">
              <Link href="/tmobile/store">
                <Store className="size-4" aria-hidden /> Continue in store with Magenta
              </Link>
            </Button>
          </Panel>

          <MetricsStrip m={w.metrics} />
          <p className="text-[11.5px] text-muted-foreground">
            Your line: {mayaDevices.phone.name}, {mayaDevices.phone.age_months} months, {mayaDevices.phone.installments_left} payments left · {mayaDevices.lines} lines · owns {mayaDevices.owns.map((o) => o.name).join(", ")}. Synthetic demo account.
          </p>
        </aside>

          <Panel className="p-5 lg:col-start-1">
            <SectionTitle
              action={
                <div className="relative">
                  <Search className="pointer-events-none absolute top-2 left-2.5 size-4 text-muted-foreground" aria-hidden />
                  <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search 91 products" className="h-8 w-48 rounded-full bg-muted pr-3 pl-8 text-[13px] outline-none" />
                </div>
              }
            >
              In the store
            </SectionTitle>
            {!q && (
              <div className="mb-3 flex flex-wrap gap-1.5">
                {CATS.map((c) => (
                  <button key={c.id} onClick={() => setCat(c.id)} className={cn("cursor-pointer rounded-full px-3 py-1 text-[12.5px] font-semibold", cat === c.id ? "bg-foreground text-background" : "bg-muted text-muted-foreground hover:text-foreground")}>
                    {c.label}
                  </button>
                ))}
              </div>
            )}
            <div className="grid gap-2 sm:grid-cols-2">
              {shelf.map((p) => {
                const slot = slotsOf("store_tmobile")[p.sku];
                const fitsDev = !p.compatible_with?.length || p.compatible_with.includes(dev.sku);
                return (
                  <button key={p.sku} onClick={() => add(p.sku)} className="flex cursor-pointer items-center gap-3 rounded-xl p-2 text-left ring-1 ring-foreground/[0.06] hover:bg-muted/60">
                    <ProductTile color={p.label_color} category={p.category} name={p.name} size={36} />
                    <span className="min-w-0 flex-1">
                      <span className="line-clamp-2 text-[13px] leading-tight font-semibold">{p.name}</span>
                      <span className="mt-0.5 flex items-center gap-2 text-[11.5px] text-muted-foreground">
                        {(slot?.qty ?? 0) > 0 ? `${slot!.qty} in stock` : <span className="text-tomato">Out of stock</span>}
                        {p.compatible_with?.length ? fitsDev ? <span className="text-leaf-deep">fits your {dev.name}</span> : <span className="text-tomato">doesn&apos;t fit</span> : null}
                      </span>
                    </span>
                    <span className="text-right">
                      <Money cents={unitPrice(p.sku)} className="block text-[13.5px] font-semibold" />
                      {slot?.deal && <s className="text-[11px] text-muted-foreground tabular">{fmt(slot.deal.original_cents)}</s>}
                    </span>
                  </button>
                );
              })}
            </div>
          </Panel>
      </div>
    </div>
  );
}
