"use client";
import {
  Ban,
  Bot,
  Check,
  ChevronDown,
  Eye,
  ListChecks,
  MessageSquare,
  MonitorPlay,
  Pause,
  Play,
  Receipt,
  RotateCcw,
  ShoppingBasket,
  Smartphone,
  Sparkles,
  TriangleAlert,
  Wrench,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Suspense, useState } from "react";
import { toast } from "sonner";
import { RobotArt } from "@/components/art";
import { PaymentAgent, PickReview, VerifyStack, paymentTimeline } from "@/components/TripReview";
import { BindTripFromUrl } from "@/components/GatewayBits";
import { HandoffPanel } from "@/components/Handoff";
import { LiveFeed } from "@/components/LiveFeed";
import { LottieSlot } from "@/components/media";
import { Confetti, CountUp } from "@/components/motion";
import { Meter, Money, Panel, ProductTile, SectionTitle } from "@/components/picky";
import { MapLegend, StoreMap } from "@/components/StoreMap";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogClose, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { STORES, maya, productBySku } from "@/lib/data";
import { errorText, gw } from "@/lib/gateway/client";
import { TRIP_END_TOAST, toastLearned } from "@/lib/gateway/tripSource";
import { isReconnecting, useGateway } from "@/lib/gateway/useGateway";
import { fmt } from "@/lib/money";
import { cartTotal, useTrip, type Approval, type ThinkStep } from "@/lib/trip";
import { cn } from "@/lib/utils";

export default function TripPage() {
  const t = useTrip();
  const router = useRouter();
  const reconnecting = useGateway(isReconnecting);
  // /trip?trip=<id> follows that gateway trip (reload, shared link)
  const bind = (
    <Suspense fallback={null}>
      <BindTripFromUrl />
    </Suspense>
  );

  if (t.source === "gateway" && (!t.draft || !t.route || !STORES[t.draft.store_id])) {
    return (
      <>
        {bind}
        <Planning name={t.robotName} error={t.error} reconnecting={reconnecting} onRestart={() => (t.reset(), router.push("/order"))} />
      </>
    );
  }

  if (t.phase === "idle" || !t.draft || !t.route) {
    return (
      <div className="mx-auto flex max-w-[560px] animate-rise flex-col items-center py-16 text-center">
        {bind}
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
  const total = cartTotal(t.cart) + t.shippingCents;
  const StoreIcon = store.kind === "grocery" ? ShoppingBasket : Smartphone;

  return (
    <div className="flex flex-col gap-6">
      {bind}
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

      <Controls onRestart={() => (t.reset(), router.push(t.storeId === "store_tmobile" ? "/tmobile/shop" : "/order"))} />

      <div className="grid gap-6 xl:grid-cols-12">
        <div className="flex flex-col gap-6 xl:col-span-8">
          {t.phase === "waiting_login" && t.source !== "gateway" ? <LoginPanel /> : <LiveFeed />}
          <HandoffPanel />
          <ApprovalPanel />
          <VerifyStack />
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
          {t.source === "gateway" && <RobotThinking />}
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

/** Gateway trip, before the list and route arrive (or while the gateway is out of reach). */
function Planning({ name, error, reconnecting, onRestart }: { name: string | null; error: string | null; reconnecting: boolean; onRestart: () => void }) {
  return (
    <div className="mx-auto flex max-w-[560px] animate-rise flex-col items-center py-16 text-center">
      <LottieSlot slot="robotIdle" className="aspect-[8/7] w-[300px]" fallback={<RobotArt />} />
      <h1 className="mt-6 text-[34px] leading-tight font-bold">
        {error ? "The trip couldn't start" : reconnecting ? "Reconnecting to the robot" : `${name ?? "The robot"} is planning your route`}
      </h1>
      <p className="mt-2 text-[16px] text-muted-foreground">
        {error ??
          (reconnecting
            ? "The Picky gateway isn't answering right now. The trip picks up where it is as soon as it's back."
            : "Reading your list, checking what's on the shelves right now, and working out the shortest way past everything.")}
      </p>
      {error ? (
        <Button size="lg" className="mt-6" onClick={onRestart}>
          Plan a trip
        </Button>
      ) : (
        <div className="mt-6 flex w-full max-w-[360px] flex-col gap-2" aria-hidden>
          {[0, 1, 2].map((i) => (
            <Skeleton key={i} className="h-10 w-full rounded-xl" />
          ))}
        </div>
      )}
    </div>
  );
}

/** The other view of the same trip: /trip (details) ⇄ /watch (projector). */
function SwitchView({ to }: { to: "watch" | "trip" }) {
  const tripId = useTrip((s) => (s.source === "gateway" ? s.tripId : null));
  return (
    <Button asChild variant="ghost" size="sm">
      <Link href={`/${to}${tripId ? `?trip=${encodeURIComponent(tripId)}` : ""}`}>
        {to === "watch" ? <MonitorPlay /> : <ListChecks />} {to === "watch" ? "Projector view" : "Trip details"}
      </Link>
    </Button>
  );
}

export function Controls({ onRestart, switchTo = "watch" }: { onRestart: () => void; switchTo?: "watch" | "trip" }) {
  const { speed, setSpeed, paused, togglePause, autoAnswer, setAutoAnswer, phase, source } = useTrip();
  if (source === "gateway") return <GatewayControls onRestart={onRestart} switchTo={switchTo} />;
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
      <span className="ml-auto" />
      <SwitchView to={switchTo} />
      <Button variant="ghost" size="sm" onClick={onRestart}>
        <RotateCcw /> New trip
      </Button>
    </div>
  );
}

/** A real robot: no pause / speed / auto-answer (checkout always waits for the shopper). */
function GatewayControls({ onRestart, switchTo }: { onRestart: () => void; switchTo: "watch" | "trip" }) {
  const { robotName, robotId, phase } = useTrip();
  const reconnecting = useGateway(isReconnecting);
  const [stopping, setStopping] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const name = robotName ?? "the robot";
  const stop = async () => {
    setConfirming(false);
    if (!robotId) return;
    setStopping(true);
    try {
      await gw.stopRobot(robotId);
      toast(`${robotName ?? "The robot"} stopped`, { id: TRIP_END_TOAST }); // the trip's failure notice replaces this
    } catch (e) {
      toast.error(`Couldn't stop ${name}: ${errorText(e)}`);
    } finally {
      setStopping(false);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-2xl bg-card px-3 py-2 ring-1 ring-foreground/[0.07]">
      <Badge variant={reconnecting ? "amber" : "leaf"}>
        <span className="live-dot size-1.5 rounded-full bg-current" /> {reconnecting ? "Reconnecting" : "Live"}
      </Badge>
      <span className="text-[13.5px]">
        Live from the Picky gateway{robotName ? ` · ${robotName}` : ""}
      </span>
      {robotId && phase !== "done" && (
        <Dialog open={confirming} onOpenChange={setConfirming}>
          <DialogTrigger asChild>
            <Button variant="ghost" size="sm" disabled={stopping}>
              <Ban /> Stop robot
            </Button>
          </DialogTrigger>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>Stop {name} where it is?</DialogTitle>
              <DialogDescription>It halts right away and this trip ends. Nothing is charged.</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <DialogClose asChild>
                <Button variant="outline">Keep shopping</Button>
              </DialogClose>
              <Button onClick={stop}>Stop {name}</Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      )}
      <span className="ml-auto" />
      <SwitchView to={switchTo} />
      <Button variant="ghost" size="sm" onClick={onRestart}>
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

/** Gateway reasons are lower-case fragments ("the in-store goal was cancelled: …"): show them as a sentence. */
function sentence(s: string | null) {
  const t = (s ?? "").trim();
  return t ? t[0].toUpperCase() + t.slice(1) + (/[.!?]$/.test(t) ? "" : ".") : "";
}

export function ApprovalPanel() {
  const t = useTrip();
  const { approval, answer, receipt, phase, mode, source, tripState, error, approvalsLog } = t;
  if (receipt)
    return (
      <div className="flex flex-col gap-3">
        <ReceiptPanel />
        <PaymentAgent compact />
      </div>
    );
  if (source === "gateway" && (tripState === "failed" || (error && phase === "done")))
    return (
      <Panel className="flex items-start gap-3 bg-tomato-wash p-5 text-[15px] text-tomato">
        <TriangleAlert className="mt-0.5 size-5 shrink-0" aria-hidden />
        <span>
          <span className="font-semibold">The trip stopped.</span> {sentence(error) || "The robot couldn't finish."} Nothing was charged.
        </span>
      </Panel>
    );
  // a declined checkout leaves the gateway trip open with the basket held: offer to check out later
  const last = approvalsLog[approvalsLog.length - 1];
  if (!approval && source === "gateway" && last?.kind === "checkout" && last.answer === "decline") return <HeldBasket />;
  if (!approval && phase === "done")
    return <Panel className="p-5 text-[15px]">No problem. Your basket is held at the pickup counter for 30 minutes in case you change your mind.</Panel>;
  // checkout = the Visa payment agent: its passkey button is the approval; after it, authorizing until the receipt
  const pay = paymentTimeline(t);
  if (approval?.kind === "checkout" || (!approval && pay.started && pay.steps.find((s) => s.kind === "approval")?.status === "done")) return <PaymentAgent />;
  // paid, receipt on its way (checkout.completed follows trip.state_changed)
  if (!approval && source === "gateway" && (tripState === "paid" || tripState === "followup")) return null;
  return (
    <AnimatePresence mode="popLayout">
      {approval && (
        <motion.div
          key={approval.id}
          initial={{ opacity: 0, y: 24, scale: 0.97 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: -12, scale: 0.97, transition: { duration: 0.18 } }}
          transition={{ type: "spring", stiffness: 380, damping: 28 }}
          className="@container rounded-2xl bg-card p-5 shadow-md ring-2 ring-amber md:p-6"
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
      <div className="flex flex-col gap-4 @xl:flex-row @xl:items-center">
        <ProductTile color={p.label_color} category={p.category} name={p.name} size={64} />
        <div className="min-w-0 flex-1">
          <h3 className="text-[24px] leading-tight font-bold">
            Grab the {p.name}? <Money cents={a.unit_cents} className="text-[20px] font-semibold text-muted-foreground" />
          </h3>
          <p className="mt-1 max-w-[60ch] text-[15px] text-muted-foreground">{a.reason}</p>
          {a.note && <p className="mt-1 text-[13.5px] font-medium text-amber-ink">{a.note}</p>}
          <p className="mt-1 text-[12.5px] text-muted-foreground">{a.sponsored ? "Sponsored" : "Not sponsored"}</p>
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
  if (a.kind === "pick") return <PickReview a={a} answer={answer} />;
  if (a.kind === "question") return <QuestionBody a={a} answer={answer} />;
  return <PaymentAgent />;
}

function HeldBasket() {
  const tripId = useTrip((s) => s.tripId);
  const [busy, setBusy] = useState(false);
  const checkout = async () => {
    if (!tripId) return;
    setBusy(true);
    try {
      await gw.checkout(tripId); // the new checkout card arrives over /ws
    } catch (e) {
      toast.error(`Couldn't start checkout: ${errorText(e)}`);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Panel className="flex flex-wrap items-center gap-4 p-5 text-[15px]">
      <span className="min-w-0 flex-1">No problem. Your basket is held at the pickup counter for 30 minutes in case you change your mind.</span>
      <Button onClick={checkout} disabled={busy}>
        <Receipt /> Check out now
      </Button>
    </Panel>
  );
}

/** Gateway questions: over budget, substitutions, online orders, open questions. */
function QuestionBody({ a, answer }: { a: Extract<Approval, { kind: "question" }>; answer: (v: string) => void }) {
  const p = a.sku ? productBySku[a.sku] : null;
  return (
    <div className="flex flex-col gap-4 @xl:flex-row @xl:items-center">
      {p && <ProductTile color={p.label_color} category={p.category} name={p.name} size={64} />}
      <div className="min-w-0 flex-1">
        <h3 className="text-[24px] leading-tight font-bold">
          {a.title} {a.amount_cents != null && <Money cents={a.amount_cents} className="text-[20px] font-semibold text-muted-foreground" />}
        </h3>
        {a.body && <p className="mt-1 max-w-[60ch] text-[15px] text-muted-foreground">{a.body}</p>}
      </div>
      <div className="flex flex-wrap gap-2">
        {a.options.length ? (
          <>
            {a.options.map((o) => (
              <Button key={o.id} size="lg" variant="outline" onClick={() => answer(o.id)} title={o.detail}>
                {o.label}
              </Button>
            ))}
            {/* "Want any of these before checkout?" (multi): saying no is an answer too */}
            {a.multi && (
              <Button size="lg" variant="ghost" onClick={() => answer("no")}>
                No thanks
              </Button>
            )}
          </>
        ) : (
          <>
            <Button variant="outline" size="lg" onClick={() => answer("no")}>
              No
            </Button>
            <Button size="lg" onClick={() => answer("yes")}>
              Yes
            </Button>
          </>
        )}
      </div>
    </div>
  );
}

function ReceiptPanel() {
  const { receipt, cart, mode, source, followup } = useTrip();
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
        {source === "gateway" ? <span>{receipt.provider}</span> : <span>Tomorrow Picky will ask how it all was</span>}
      </div>
      {source === "gateway" && followup && <FollowUp text={followup.text} sku={followup.skus[0]} quick={followup.quick} />}
    </motion.div>
  );
}

/** The post-purchase question (followup.sent), answerable here: 1–5 → POST /api/users/{id}/feedback (trip 2 gets smarter). */
function FollowUp({ text, sku, quick }: { text: string; sku?: string; quick: string[] }) {
  const tripId = useTrip((s) => s.tripId);
  const [sent, setSent] = useState<string | null>(null);
  const replies = quick.length ? quick : ["1", "2", "3", "4", "5"];
  const send = async (reply: string) => {
    setSent(reply);
    const rating = /^[1-5]$/.test(reply) ? Number(reply) : null;
    try {
      const { data } = await gw.feedback(maya.profile.user_id, {
        feedback_id: crypto.randomUUID(),
        user_id: maya.profile.user_id,
        sku: sku ?? null,
        trip_id: tripId,
        rating,
        text: rating ? "" : reply,
        channel: "web",
        tags: [],
      });
      toastLearned(data);
    } catch (e) {
      setSent(null);
      toast.error(`Couldn't send that: ${errorText(e)}`);
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-3 border-t border-white/15 px-6 py-3 text-[14px]">
      <MessageSquare className="size-4 shrink-0" aria-hidden />
      <span className="min-w-0 flex-1">{text}</span>
      {sent ? (
        <span className="font-semibold">Thanks!</span>
      ) : (
        <div className="flex gap-1.5">
          {replies.map((r) => (
            <Button key={r} size="sm" variant="lime" className="min-w-9" onClick={() => send(r)}>
              {r}
            </Button>
          ))}
        </div>
      )}
    </div>
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
                // the pulse repeats; scope it to scale so the row's layout move (approval card in/out) still settles
                transition={active ? { scale: { duration: 1.6, repeat: Infinity } } : undefined}
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

export function Basket() {
  const { cart, draft, shippingCents } = useTrip();
  const total = cartTotal(cart) + shippingCents;
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
              key={c.line_id ?? c.sku + c.source}
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
              ) : c.verified === false ? (
                <span className="flex items-center gap-1 text-[12px] font-semibold text-tomato" title="You flagged this pick">
                  <TriangleAlert className="size-3.5" /> Flagged
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
        {shippingCents > 0 && (
          <div className="mb-1 flex items-baseline justify-between text-[14px]">
            <span className="text-muted-foreground">Delivery</span>
            <Money cents={shippingCents} />
          </div>
        )}
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

const STEP_ICON = { thought: Sparkles, tool_call: Wrench, observation: Eye, message: MessageSquare } as const;

/** The robot brain's thinking log: REST history + live brain.step (gateway trips). */
export function RobotThinking() {
  const steps = useTrip((s) => s.steps);
  const name = useTrip((s) => s.robotName);
  // tool results echo the call; keep them only when they failed
  const rows = steps.filter((st) => st.kind !== "tool_result" || !st.ok).slice(-80).reverse();
  return (
    <Panel className="p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-[17px] font-semibold">What {name ?? "the robot"} is thinking</h2>
        <span className="text-[13px] text-muted-foreground">{steps.length} steps</span>
      </div>
      <ol className="flex max-h-[320px] flex-col overflow-y-auto">
        {rows.length === 0 && <li className="py-3 text-[14px] text-muted-foreground">Its reasoning shows up here as it works.</li>}
        <AnimatePresence initial={false}>
          {rows.map((st) => (
            <ThinkRow key={st.id} st={st} />
          ))}
        </AnimatePresence>
      </ol>
    </Panel>
  );
}

function ThinkRow({ st }: { st: ThinkStep }) {
  const bad = !st.ok || st.kind === "error";
  const Icon = bad ? TriangleAlert : (STEP_ICON[st.kind as keyof typeof STEP_ICON] ?? Sparkles);
  const text = st.kind === "tool_call" && st.tool && st.text.startsWith(`${st.tool}(`) ? st.text.slice(st.tool.length + 1, -1) : st.text;
  const cam = st.camera?.split(".").pop(); // "rover-ave-1.front" → "front"
  return (
    <motion.li
      layout
      initial={{ opacity: 0, y: -8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ type: "spring", stiffness: 420, damping: 32 }}
      className="flex gap-2.5 border-b border-border/60 py-2 text-[13.5px] last:border-0"
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", bad ? "text-tomato" : st.kind === "thought" ? "text-leaf" : "text-muted-foreground")} strokeWidth={1.5} aria-hidden />
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          {st.tool && <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11.5px]">{st.tool}</code>}
          {cam && st.kind === "observation" && <span className="text-[12px] text-muted-foreground">{cam} camera</span>}
          <span className="tabular ml-auto text-[11.5px] text-muted-foreground">
            {new Date(st.t).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit", second: "2-digit" })}
          </span>
        </div>
        {text && <p className={cn("mt-0.5 break-words", st.kind === "tool_call" ? "font-mono text-[12px] text-muted-foreground" : bad ? "text-tomato" : "")}>{text}</p>}
      </div>
    </motion.li>
  );
}

export function EventLog() {
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
