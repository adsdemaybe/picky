"use client";
import { Ban, Bot, Check, ChevronDown, Clock, Pause, Play, Receipt, RotateCcw, ShoppingBasket, Smartphone } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { RobotArt } from "@/components/art";
import { LiveFeed } from "@/components/LiveFeed";
import { LottieSlot } from "@/components/media";
import { Confetti, CountUp } from "@/components/motion";
import { Meter, Money, Panel, ProductTile, SectionTitle } from "@/components/picky";
import { MapLegend, StoreMap } from "@/components/StoreMap";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { STORES, productBySku } from "@/lib/data";
import { fmt } from "@/lib/money";
import { cartTotal, useTrip, type Approval } from "@/lib/trip";
import { cn } from "@/lib/utils";

export default function TripPage() {
  const t = useTrip();
  const router = useRouter();

  if (t.phase === "idle" || !t.draft || !t.route) {
    return (
      <div className="mx-auto flex max-w-[560px] animate-rise flex-col items-center py-16 text-center">
        <LottieSlot slot="robotIdle" className="aspect-[8/7] w-[300px]" fallback={<RobotArt sleeping />} />
        <h1 className="mt-6 text-[34px] leading-tight font-bold">The robot is resting</h1>
        <p className="mt-2 text-[16px] text-muted-foreground">
          Send it shopping and you can watch it here: the route, the camera, every pick, and a question whenever it needs you.
        </p>
        <Button asChild size="lg" className="mt-6">
          <Link href="/order">Plan a trip</Link>
        </Button>
      </div>
    );
  }

  const store = STORES[t.storeId];
  const listStops = t.route.order.filter((id) => t.stops[id]?.kind === "list" || t.stops[id]?.kind === "optional");
  const done = listStops.filter((id) => t.stopStatus[id] === "done" || t.stopStatus[id] === "declined").length;
  const extra = t.route.total_m - t.baseline_m;
  const total = cartTotal(t.cart);
  const StoreIcon = store.kind === "grocery" ? ShoppingBasket : Smartphone;

  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="flex items-center gap-2 text-[14px] font-medium text-muted-foreground">
            <span className={cn("grid size-6 place-items-center rounded-md text-white", store.kind === "grocery" ? "bg-leaf" : "bg-grape")}>
              <StoreIcon className="size-3.5" aria-hidden />
            </span>
            {store.name}
            <span aria-hidden>/</span>
            {t.mode === "pickup" ? "Shopping for pickup" : "Shopping with you"}
          </div>
          <h1 className="mt-1 text-[34px] leading-tight font-bold md:text-[42px]">{t.draft.title}</h1>
        </div>
        <div className="flex gap-2">
          <Stat label="Stops" value={`${done} of ${listStops.length}`} />
          <Stat label="Walk" value={`${t.route.total_m.toFixed(0)} m`} note={extra > 0.5 ? `+${extra.toFixed(0)} m for extras` : undefined} />
          <Stat label="Basket" value={fmt(total)} note={`of ${fmt(t.draft.budget_cents)}`} tone={total > t.draft.budget_cents ? "tomato" : undefined} />
        </div>
      </section>

      <Controls onRestart={() => (t.reset(), router.push("/order"))} />

      <div className="grid gap-6 xl:grid-cols-12">
        <div className="flex flex-col gap-6 xl:col-span-8">
          {t.phase === "waiting_login" ? <LoginPanel /> : <LiveFeed />}
          <ApprovalPanel />
          <StopList />
        </div>
        <div className="flex flex-col gap-6 xl:col-span-4">
          <Panel className="p-4">
            <div className="mb-2 flex items-center justify-between">
              <h2 className="text-[17px] font-semibold">Route</h2>
              <span className="text-[13px] text-muted-foreground">Planned from the store layout</span>
            </div>
            <div className="mx-auto max-h-[480px]" style={{ aspectRatio: `${store.bounds.w + 0.8} / ${store.bounds.h + 0.8}` }}>
              <StoreMap />
            </div>
            <div className="mt-2">
              <MapLegend />
            </div>
          </Panel>
          <Basket />
          <EventLog />
        </div>
      </div>
    </div>
  );
}

function Stat({ label, value, note, tone }: { label: string; value: string; note?: string; tone?: "tomato" }) {
  return (
    <div className="min-w-[96px] rounded-2xl bg-card px-4 py-2.5 ring-1 ring-foreground/[0.07]">
      <div className="text-[12.5px] text-muted-foreground">{label}</div>
      <div className={cn("tabular font-heading text-[22px] leading-tight font-bold", tone === "tomato" && "text-tomato")}>{value}</div>
      {note && <div className="text-[12px] text-muted-foreground">{note}</div>}
    </div>
  );
}

function Controls({ onRestart }: { onRestart: () => void }) {
  const { speed, setSpeed, paused, togglePause, autoAnswer, setAutoAnswer, phase } = useTrip();
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-2xl bg-card px-3 py-2 ring-1 ring-foreground/[0.07]">
      <Button variant="ghost" size="sm" onClick={togglePause} disabled={phase === "done"} aria-label={paused ? "Resume" : "Pause"}>
        {paused ? <Play /> : <Pause />} {paused ? "Resume" : "Pause"}
      </Button>
      <ToggleGroup type="single" value={String(speed)} onValueChange={(v) => v && setSpeed(Number(v))} className="rounded-full bg-muted p-1" aria-label="Playback speed">
        {[1, 2, 4].map((n) => (
          <ToggleGroupItem key={n} value={String(n)} className="tabular h-7 rounded-full px-3 text-[13px] data-[state=on]:bg-card data-[state=on]:shadow-sm">
            {n}×
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <label className="flex cursor-pointer items-center gap-2 text-[13.5px]">
        <Switch checked={autoAnswer} onCheckedChange={setAutoAnswer} aria-label="Answer questions automatically" />
        Answer questions for me
      </label>
      <span className="hidden text-[12.5px] text-muted-foreground md:inline">Demo playback controls</span>
      <Button variant="ghost" size="sm" onClick={onRestart} className="ml-auto">
        <RotateCcw /> New trip
      </Button>
    </div>
  );
}

function LoginPanel() {
  const { loginCode, robotLogin } = useTrip();
  return (
    <div className="grid animate-rise overflow-hidden rounded-2xl bg-foreground text-background md:grid-cols-[1fr_auto]">
      <div className="p-7 md:p-8">
        <span className="inline-flex items-center gap-2 rounded-full bg-lime px-3 py-1 text-[13px] font-semibold text-lime-ink">
          <Bot className="size-4" /> Your robot is at the entrance
        </span>
        <h2 className="mt-4 text-[30px] leading-tight font-bold">Type this code on its screen and it starts your list.</h2>
        <p className="mt-2 max-w-[48ch] text-background/70">It already has your list, budget and rules. Or text “here” to Picky to log in from your phone.</p>
        <Button variant="lime" size="lg" className="mt-6" onClick={robotLogin}>
          I&apos;ve entered the code
        </Button>
      </div>
      <div className="flex flex-col items-center justify-center gap-2 bg-white/5 px-10 py-8">
        <div className="text-[13px] text-background/60">Login code</div>
        <div className="tabular font-heading text-[54px] leading-none font-bold tracking-wider">{loginCode}</div>
        <div className="text-[12.5px] text-background/50">Expires in 10 minutes</div>
      </div>
    </div>
  );
}

function ApprovalPanel() {
  const { approval, answer, receipt, phase, mode } = useTrip();
  if (receipt) return <ReceiptPanel />;
  if (!approval && phase === "done")
    return <Panel className="p-5 text-[15px]">No problem. Your basket is held at the pickup counter for 30 minutes in case you change your mind.</Panel>;
  return (
    <AnimatePresence mode="popLayout">
      {approval && (
        <motion.div
          key={approval.id}
          initial={{ opacity: 0, y: 24, scale: 0.97 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -12, scale: 0.97, transition: { duration: 0.18 } }}
          transition={{ type: "spring", stiffness: 380, damping: 28 }}
          className="rounded-2xl bg-card p-5 shadow-md ring-2 ring-amber md:p-6"
          role="alertdialog"
          aria-label="The robot needs an answer"
        >
      <div className="mb-2 flex items-center gap-2 text-[13.5px] font-semibold text-amber-ink">
        <span className="live-dot size-2 rounded-full bg-amber" />
        {mode === "pickup" ? "Sent to your phone. You can answer here too." : "The robot is asking you"}
      </div>
          <ApprovalBody a={approval} answer={answer} />
        </motion.div>
      )}
    </AnimatePresence>
  );
}

function ApprovalBody({ a, answer }: { a: Approval; answer: (v: string) => void }) {
  if (a.kind === "detour") {
    const p = productBySku[a.sku];
    return (
      <div className="flex flex-col gap-4 md:flex-row md:items-center">
        <ProductTile color={p.label_color} category={p.category} name={p.name} size={64} />
        <div className="min-w-0 flex-1">
          <h3 className="text-[24px] leading-tight font-bold">
            Grab the {p.name}? <Money cents={a.unit_cents} className="text-[20px] font-semibold text-muted-foreground" />
          </h3>
          <p className="mt-1 max-w-[60ch] text-[15px] text-muted-foreground">{a.reason}</p>
          <p className="mt-1 text-[12.5px] text-muted-foreground">Not sponsored</p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" size="lg" onClick={() => answer("no")}>
            No thanks
          </Button>
          <Button size="lg" onClick={() => answer("yes")}>
            Yes, add it
          </Button>
        </div>
      </div>
    );
  }
  if (a.kind === "pick") {
    const p = productBySku[a.sku];
    return (
      <div>
        <h3 className="text-[24px] leading-tight font-bold">Which {a.sku === "AVOCADO" ? "avocados" : p.name.toLowerCase()} should it take?</h3>
        <p className="mt-1 text-[15px] text-muted-foreground">The camera ranked everything on the table by ripeness. Here are the best three batches.</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-3">
          {a.candidates.map((c, i) => (
            <motion.button
              key={c.instance_id}
              onClick={() => answer(c.instance_id)}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.08 * i }}
              whileHover={{ y: -4 }}
              whileTap={{ scale: 0.97 }}
              className="group cursor-pointer rounded-2xl bg-muted p-4 text-left ring-2 ring-transparent transition-colors hover:bg-leaf-wash hover:ring-leaf"
            >
              <div className="flex items-center justify-between">
                <ProductTile color={p.label_color} category={p.category} name={p.name} size={40} />
                {i === 1 && <Badge variant="lime">Picky&apos;s pick</Badge>}
              </div>
              <div className="mt-3 font-heading text-[18px] font-semibold">{c.note}</div>
              <div className="mt-2 h-2 overflow-hidden rounded-full bg-white">
                <motion.div className="h-full rounded-full bg-leaf" initial={{ width: 0 }} animate={{ width: `${c.freshness * 100}%` }} transition={{ delay: 0.2 + 0.08 * i, duration: 0.6 }} />
              </div>
              <div className="mt-1.5 text-[13px] text-muted-foreground">
                {Math.round(c.freshness * 100)}% fresh, best by {new Date(c.expiry + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}
              </div>
            </motion.button>
          ))}
        </div>
      </div>
    );
  }
  const asks = a.reasons.filter((r) => r.level !== "ok").length;
  return (
    <div className="flex flex-col gap-4 md:flex-row md:items-end">
      <div className="min-w-0 flex-1">
        <h3 className="text-[24px] leading-tight font-bold">
          Ready to pay <Money cents={a.total_cents} />?
        </h3>
        <ul className="mt-3 flex flex-col gap-1.5 text-[14.5px]">
          {a.reasons.map((r) => (
            <li key={r.code + r.text} className="flex items-start gap-2">
              {r.level === "ok" ? <Check className="mt-0.5 size-4 shrink-0 text-leaf" /> : <Clock className="mt-0.5 size-4 shrink-0 text-amber-ink" />}
              {r.text}
            </li>
          ))}
        </ul>
        <p className="mt-3 text-[12.5px] text-muted-foreground">
          Your rules {asks ? "ask for your approval" : "allow this"}. Paying with Visa ending 4242 in test mode, so no real money moves.
        </p>
      </div>
      <div className="flex gap-2">
        <Button variant="outline" size="lg" onClick={() => answer("decline")}>
          Not yet
        </Button>
        <Button size="lg" onClick={() => answer("approve")}>
          Approve and pay
        </Button>
      </div>
    </div>
  );
}

function ReceiptPanel() {
  const { receipt, cart, mode } = useTrip();
  if (!receipt) return null;
  const inStore = cart.filter((c) => c.source === "in_store");
  const online = cart.filter((c) => c.source === "online");
  return (
    <motion.div
      initial={{ opacity: 0, scale: 0.96 }}
      animate={{ opacity: 1, scale: 1 }}
      transition={{ type: "spring", stiffness: 300, damping: 22 }}
      className="relative overflow-hidden rounded-2xl bg-leaf text-white"
    >
      <div className="flex flex-wrap items-center gap-4 p-6">
        <span className="relative grid size-14 shrink-0 place-items-center">
          <Confetti />
          <LottieSlot
            slot="celebrate"
            hint={false}
            loop={false}
            className="size-14"
            fallback={
              <motion.span
                className="grid size-14 place-items-center rounded-full bg-lime text-lime-ink"
                initial={{ scale: 0, rotate: -90 }}
                animate={{ scale: 1, rotate: 0 }}
                transition={{ type: "spring", stiffness: 400, damping: 14, delay: 0.1 }}
              >
                <Check className="size-7" strokeWidth={3} />
              </motion.span>
            }
          />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="text-[26px] leading-tight font-bold">
            Paid <CountUp cents={receipt.total_cents} />
          </h3>
          <p className="mt-0.5 text-white/85">
            {inStore.length} items {mode === "pickup" ? "are waiting at the pickup counter" : "are in your basket"}
            {online.length ? `, and ${online.length} more arrive from Picky Online in about 2 hours` : ""}.
          </p>
        </div>
        <Button asChild variant="lime" size="lg">
          <Link href="/purchases">
            <Receipt /> See it in Spending
          </Link>
        </Button>
      </div>
      <div className="flex flex-wrap gap-x-6 gap-y-1 bg-black/15 px-6 py-3 text-[13px] text-white/85">
        <span>Visa ending 4242</span>
        <span>Test mode, no real charge</span>
        <span>Receipt {receipt.receipt_id}</span>
        <span>Tomorrow Picky will ask how it all was</span>
      </div>
    </motion.div>
  );
}

function StopList() {
  const { route, stops, stopStatus, activeStop, robot } = useTrip();
  if (!route) return null;
  const ids = route.order.filter((id) => stops[id]?.kind !== "start");
  return (
    <section>
      <SectionTitle>Stops</SectionTitle>
      <Panel className="p-2">
        <ol className="grid gap-1 sm:grid-cols-2">
          {ids.map((id, i) => {
            const s = stops[id];
            const st = stopStatus[id];
            const active = activeStop === id && st === "active";
            const p = s.sku ? productBySku[s.sku] : null;
            return (
              <motion.li
                key={id}
                layout
                animate={active ? { scale: [1, 1.015, 1] } : { scale: 1 }}
                transition={active ? { duration: 1.6, repeat: Infinity } : undefined}
                className={cn("flex items-center gap-3 rounded-xl p-2.5 transition-colors", active && "bg-sky-wash")}
              >
                <span
                  className={cn(
                    "tabular grid size-7 shrink-0 place-items-center rounded-full text-[12.5px] font-bold",
                    st === "done" ? "bg-leaf text-white" : active ? "bg-sky text-white" : st === "declined" ? "bg-muted text-muted-foreground" : "bg-muted",
                  )}
                >
                  {st === "done" ? <Check className="size-4 animate-pop" strokeWidth={3} /> : st === "declined" ? <Ban className="size-3.5" /> : s.kind === "end" ? "$" : i + 1}
                </span>
                {p && <ProductTile color={p.label_color} category={p.category} name={p.name} size={30} />}
                <span className={cn("min-w-0 flex-1 truncate text-[14px]", st === "declined" && "text-muted-foreground line-through")}>
                  {s.qty && s.qty > 1 ? `${s.qty} × ` : ""}
                  {s.label}
                </span>
                {s.kind === "optional" && <Badge variant="amber">Extra</Badge>}
                {active && <span className="text-[12.5px] font-semibold text-sky capitalize">{robot.status}</span>}
              </motion.li>
            );
          })}
        </ol>
      </Panel>
    </section>
  );
}

function Basket() {
  const { cart, draft } = useTrip();
  const total = cartTotal(cart);
  const budget = draft?.budget_cents ?? 4000;
  return (
    <Panel className="p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-[17px] font-semibold">Basket</h2>
        <span className="text-[13px] text-muted-foreground">{cart.reduce((s, c) => s + c.qty, 0)} items</span>
      </div>
      <ul className="flex max-h-[300px] flex-col gap-1 overflow-y-auto">
        {cart.length === 0 && <li className="py-3 text-[14px] text-muted-foreground">Empty for now. The first pick lands here.</li>}
        <AnimatePresence initial={false}>
        {cart.map((c) => {
          const p = productBySku[c.sku];
          return (
            <motion.li
              key={c.sku + c.source}
              layout
              initial={{ opacity: 0, x: 40, scale: 0.9 }}
              animate={{ opacity: 1, x: 0, scale: 1 }}
              exit={{ opacity: 0, x: -20 }}
              transition={{ type: "spring", stiffness: 420, damping: 30 }}
              className="flex items-center gap-3 py-1.5"
            >
              <ProductTile color={p.label_color} category={p.category} name={p.name} size={32} />
              <span className="min-w-0 flex-1 truncate text-[14px]">
                <span className="text-muted-foreground">{c.qty} × </span>
                {p.name}
              </span>
              {c.source === "online" ? (
                <Badge variant="grape">Online</Badge>
              ) : c.verified ? (
                <span className="flex items-center gap-1 text-[12px] font-semibold text-leaf" title="Checked by the basket camera">
                  <Check className="size-3.5 animate-pop" strokeWidth={3} /> Checked
                </span>
              ) : (
                <span className="text-[12px] text-muted-foreground">Checking</span>
              )}
              <Money cents={c.qty * c.unit_cents} className="w-[64px] text-right text-[14px] font-medium" />
            </motion.li>
          );
        })}
        </AnimatePresence>
      </ul>
      <div className="mt-3 border-t border-border pt-3">
        <div className="flex items-baseline justify-between">
          <span className="font-semibold">Total</span>
          <CountUp cents={total} className={cn("font-heading text-[24px] font-bold", total > budget && "text-tomato")} />
        </div>
        <Meter value={total} max={budget} className="mt-2" />
        <div className="mt-1.5 text-right text-[13px] text-muted-foreground">
          {total <= budget ? `${fmt(budget - total)} left of ${fmt(budget)}` : `${fmt(total - budget)} over budget`}
        </div>
      </div>
    </Panel>
  );
}

function EventLog() {
  const log = useTrip((s) => s.log);
  const [open, setOpen] = useState(false);
  return (
    <Panel className="overflow-hidden">
      <button onClick={() => setOpen((o) => !o)} aria-expanded={open} className="flex w-full cursor-pointer items-center justify-between p-4 text-left hover:bg-muted/60">
        <span>
          <span className="font-semibold">Behind the scenes</span>
          <span className="block text-[12.5px] text-muted-foreground">{log.length} events from the robot and the planner</span>
        </span>
        <ChevronDown className={cn("size-5 text-muted-foreground transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <ol className="max-h-[280px] overflow-y-auto border-t border-border p-3 text-[12.5px] leading-relaxed">
          {[...log].reverse().map((e) => (
            <li key={e.seq} className="grid grid-cols-[120px_1fr] gap-2 py-0.5">
              <span className="font-semibold text-sky">{e.type}</span>
              <span className="text-muted-foreground">{e.summary}</span>
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}
