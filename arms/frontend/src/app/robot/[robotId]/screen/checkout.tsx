"use client";
// Checkout + questions for the shopper standing at the robot. Pending approvals come from GET /api/approvals
// (robot_id / trip_id) and are answered with POST /api/approvals/{id}: checkout approvals echo the cart_hash the
// mandate engine evaluated (409 = the basket changed, refetch and show the new total). Nothing is ever answered
// automatically: every card waits for a tap.
import { Check, CircleAlert, Clock, Loader2, Receipt, ShieldCheck, ShoppingBasket } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useState } from "react";
import { toast } from "sonner";
import { Confetti } from "@/components/motion";
import { Money } from "@/components/picky";
import { Button } from "@/components/ui/button";
import { GatewayError, errorText, gw, type S } from "@/lib/gateway/client";
import type { ApprovalRequest, CartLine, RobotInfo, Trip } from "@/lib/gateway/events";
import { cn } from "@/lib/utils";

type Props = {
  robot: RobotInfo;
  trip: Trip | null;
  approvals: ApprovalRequest[];
  refresh: () => Promise<void>;
  setApprovals: (f: (a: ApprovalRequest[]) => ApprovalRequest[]) => void;
};

const PROVIDER: Record<string, string> = { stripe_test: "Stripe test mode", visa_sandbox: "Visa sandbox", mock: "Test payment" };

export function CheckoutTab({ robot, trip, approvals, refresh, setApprovals }: Props) {
  const ordered = [...approvals].sort((a, b) => Number(b.kind === "checkout") - Number(a.kind === "checkout"));
  const receipt = trip?.receipt ?? null;

  const answer = async (a: ApprovalRequest, body: Decision) => {
    try {
      await gw.answer(a.approval_id, { ...body, channel: "web", user_id: a.user_id });
      setApprovals((xs) => xs.filter((x) => x.approval_id !== a.approval_id));
      return true;
    } catch (e) {
      if (e instanceof GatewayError && e.status === 409) toast.warning("The basket changed while you were looking. Here's the new total.");
      else if (e instanceof GatewayError && e.status === 404) toast("That question was already answered.");
      else toast.error(`Couldn't send your answer: ${errorText(e)}`);
      return false;
    } finally {
      void refresh();
    }
  };

  return (
    <div className="min-h-0 flex-1 overflow-y-auto p-3">
      <AnimatePresence mode="popLayout" initial={false}>
        {ordered.map((a) => (
          <motion.div
            key={a.approval_id}
            layout
            initial={{ opacity: 0, y: 20, scale: 0.97 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -12, scale: 0.97, transition: { duration: 0.16 } }}
            transition={{ type: "spring", stiffness: 380, damping: 28 }}
            className="mb-3"
          >
            {a.kind === "checkout" ? <CheckoutCard a={a} trip={trip} answer={answer} /> : <QuestionCard a={a} robot={robot} answer={answer} />}
          </motion.div>
        ))}
      </AnimatePresence>

      {ordered.length > 0 ? null : receipt ? (
        <ReceiptCard receipt={receipt} />
      ) : trip && heldBasket(trip) ? (
        <HeldBasket trip={trip} refresh={refresh} />
      ) : trip && !["done", "failed"].includes(trip.state) ? (
        <InProgress trip={trip} robot={robot} />
      ) : trip?.state === "failed" ? (
        <Empty icon={CircleAlert} title="This order stopped" body={trip.error ? `${trip.error}. Nothing was charged.` : "Nothing was charged."} />
      ) : (
        <Empty icon={ShoppingBasket} title="No order on me right now" body={`When ${robot.name} is shopping for someone, their basket and checkout show up here.`} />
      )}
    </div>
  );
}

function Lines({ lines }: { lines: CartLine[] }) {
  return (
    <ul className="flex flex-col divide-y divide-border/70">
      {lines.map((l) => (
        <li key={l.line_id} className="flex items-center gap-3 py-2 text-[15px]">
          <span className="tabular w-8 shrink-0 text-muted-foreground">{l.qty}×</span>
          <span className="min-w-0 flex-1 truncate">{l.name ?? l.sku}</span>
          {l.source === "online" && <span className="rounded-full bg-sky-wash px-2 py-0.5 text-[12px] font-semibold text-sky">Online</span>}
          {(l.status === "verified" || l.status === "picked") && <Check className="size-4 shrink-0 text-leaf" aria-label="in the basket" />}
          <Money cents={l.total_cents} className="w-16 shrink-0 text-right font-semibold" />
        </li>
      ))}
    </ul>
  );
}

function Totals({ cart }: { cart: Trip["cart"] }) {
  return (
    <div className="mt-2 flex flex-col gap-0.5 text-[14px]">
      {cart.online_cents > 0 && (
        <Row label="Online items" cents={cart.online_cents} />
      )}
      {cart.shipping_cents > 0 && <Row label="Delivery" cents={cart.shipping_cents} />}
      <div className="flex items-baseline justify-between pt-1">
        <span className="text-[15px] font-semibold">Total</span>
        <Money cents={cart.total_cents} className="font-heading text-[26px] font-bold" />
      </div>
    </div>
  );
}

function Row({ label, cents }: { label: string; cents: number }) {
  return (
    <div className="flex justify-between text-muted-foreground">
      <span>{label}</span>
      <Money cents={cents} />
    </div>
  );
}

type Decision = Omit<S["ApprovalDecisionRequest"], "channel" | "user_id">;
type Answer = (a: ApprovalRequest, body: Decision) => Promise<boolean>;

function CheckoutCard({ a, trip, answer }: { a: ApprovalRequest; trip: Trip | null; answer: Answer }) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const lines = (trip?.cart.lines ?? []).filter((l) => l.status !== "declined");
  const total = a.amount_cents ?? trip?.cart.total_cents ?? 0;
  const md = trip?.mandate_decision;
  const go = async (decision: "approve" | "reject") => {
    setBusy(decision);
    // approving binds the payment to exactly the cart the mandate engine checked
    await answer(a, decision === "approve" ? { decision, cart_hash: a.cart_hash } : { decision });
    setBusy(null);
  };
  return (
    <section className="rounded-2xl bg-card p-4 shadow-md ring-2 ring-amber" role="alertdialog" aria-label="Approve checkout">
      <div className="mb-1 flex items-center gap-2 text-[13.5px] font-semibold text-amber-ink">
        <span className="live-dot size-2 rounded-full bg-amber" /> Waiting for your OK
      </div>
      <h2 className="text-[24px] leading-tight font-bold">
        Ready to pay <Money cents={total} />?
      </h2>
      {a.body && <p className="mt-1 text-[14.5px] text-muted-foreground">{a.body}</p>}
      {lines.length > 0 && (
        <div className="mt-3 rounded-xl bg-muted/50 px-3">
          <Lines lines={lines} />
        </div>
      )}
      {trip && <Totals cart={trip.cart} />}
      {md && md.messages.length > 0 && (
        <ul className="mt-2 flex flex-col gap-1 text-[13.5px]">
          {md.messages.map((m, i) => (
            <li key={i} className="flex items-start gap-2">
              {md.verdict === "approve" ? <ShieldCheck className="mt-0.5 size-4 shrink-0 text-leaf" aria-hidden /> : <Clock className="mt-0.5 size-4 shrink-0 text-amber-ink" aria-hidden />}
              {m}
            </li>
          ))}
        </ul>
      )}
      <p className="mt-2 text-[12.5px] text-muted-foreground">Checked against your spending rules. Test-mode payment, so no real money moves. I never pay without your tap.</p>
      <div className="mt-3 flex gap-2">
        <Button variant="outline" size="lg" className="flex-1" disabled={!!busy} onClick={() => go("reject")}>
          {busy === "reject" && <Loader2 className="animate-spin" />} Not yet
        </Button>
        <Button size="lg" className="flex-[2]" disabled={!!busy || !a.cart_hash} onClick={() => go("approve")}>
          {busy === "approve" && <Loader2 className="animate-spin" />} Approve and pay <Money cents={total} />
        </Button>
      </div>
    </section>
  );
}

function QuestionCard({ a, robot, answer }: { a: ApprovalRequest; robot: RobotInfo; answer: Answer }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const go = async (key: string, body: Decision) => {
    setBusy(key);
    await answer(a, body);
    setBusy(null);
  };
  const who = a.robot_id === robot.robot_id || !a.robot_id ? robot.name : a.robot_id;
  return (
    <section className="rounded-2xl bg-card p-4 shadow-md ring-2 ring-amber" role="alertdialog" aria-label={a.title}>
      <div className="mb-1 flex items-center gap-2 text-[13.5px] font-semibold text-amber-ink">
        <span className="live-dot size-2 rounded-full bg-amber" /> {who} asks
      </div>
      <h2 className="text-[22px] leading-tight font-bold">
        {a.title} {a.amount_cents != null && <Money cents={a.amount_cents} className="text-[18px] font-semibold text-muted-foreground" />}
      </h2>
      {a.body && <p className="mt-1 text-[14.5px] text-muted-foreground">{a.body}</p>}
      <div className="mt-3 flex flex-wrap gap-2">
        {a.options.length === 0 ? (
          <>
            <Button variant="outline" size="lg" disabled={!!busy} onClick={() => go("no", { decision: "reject" })}>
              {busy === "no" && <Loader2 className="animate-spin" />} No thanks
            </Button>
            <Button size="lg" disabled={!!busy} onClick={() => go("yes", { decision: "approve" })}>
              {busy === "yes" && <Loader2 className="animate-spin" />} Yes
            </Button>
          </>
        ) : a.multi ? (
          <>
            {a.options.map((o) => (
              <Button
                key={o.option_id}
                variant={picked.includes(o.option_id) ? "default" : "outline"}
                size="lg"
                aria-pressed={picked.includes(o.option_id)}
                onClick={() => setPicked((p) => (p.includes(o.option_id) ? p.filter((x) => x !== o.option_id) : [...p, o.option_id]))}
                title={o.detail ?? undefined}
              >
                {o.label}
              </Button>
            ))}
            <Button variant="ghost" size="lg" disabled={!!busy} onClick={() => go("none", { decision: "reject" })}>
              None
            </Button>
            <Button size="lg" disabled={!!busy || picked.length === 0} onClick={() => go("multi", { decision: "choice", choices: picked })}>
              {busy === "multi" && <Loader2 className="animate-spin" />} Done
            </Button>
          </>
        ) : (
          <>
            {a.options.map((o) => (
              <Button key={o.option_id} variant="outline" size="lg" disabled={!!busy} onClick={() => go(o.option_id, { decision: "choice", choice: o.option_id })} title={o.detail ?? undefined}>
                {busy === o.option_id && <Loader2 className="animate-spin" />}
                {o.thumbnail_b64 && (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={`data:image/jpeg;base64,${o.thumbnail_b64}`} alt="" className="size-7 rounded-md object-cover" />
                )}
                {o.label}
              </Button>
            ))}
            <Button variant="ghost" size="lg" disabled={!!busy} onClick={() => go("skip", { decision: "reject" })}>
              Skip
            </Button>
          </>
        )}
      </div>
    </section>
  );
}

/** The last checkout card was declined (or timed out) and nothing was paid: the gateway holds the basket. */
function heldBasket(trip: Trip) {
  if (trip.receipt || ["done", "failed"].includes(trip.state)) return false;
  const checkoutIds = new Set(trip.approvals.filter((a) => a.kind === "checkout").map((a) => a.approval_id));
  const last = [...trip.approval_responses].reverse().find((r) => checkoutIds.has(r.approval_id));
  return !!last && (last.decision === "reject" || last.decision === "timeout");
}

function HeldBasket({ trip, refresh }: { trip: Trip; refresh: () => Promise<void> }) {
  const [busy, setBusy] = useState(false);
  const lines = trip.cart.lines.filter((l) => l.status !== "declined");
  const start = async () => {
    setBusy(true);
    try {
      await gw.checkout(trip.trip_id); // asks again: a new checkout approval appears here, still waiting for a tap
    } catch (e) {
      toast.error(`Couldn't start checkout: ${errorText(e)}`);
    } finally {
      setBusy(false);
      void refresh();
    }
  };
  return (
    <section className="rounded-2xl bg-muted/50 p-4">
      <h2 className="text-[20px] leading-tight font-bold">Basket on hold</h2>
      <p className="mt-1 text-[14px] text-muted-foreground">Checkout wasn&apos;t approved, so nothing was charged. Ready now?</p>
      {lines.length > 0 && (
        <div className="mt-2">
          <Lines lines={lines} />
          <Totals cart={trip.cart} />
        </div>
      )}
      <Button size="lg" className="mt-3 w-full" onClick={start} disabled={busy || lines.length === 0}>
        {busy ? <Loader2 className="animate-spin" /> : <Receipt />} Check out now
      </Button>
    </section>
  );
}

function InProgress({ trip, robot }: { trip: Trip; robot: RobotInfo }) {
  const lines = trip.cart.lines.filter((l) => l.status !== "declined");
  return (
    <section className="rounded-2xl bg-muted/50 p-4">
      <h2 className="text-[20px] leading-tight font-bold">Basket so far</h2>
      <p className="mt-1 text-[14px] text-muted-foreground">
        When {robot.name} reaches the register, checkout shows up here for you to approve.
      </p>
      {lines.length ? (
        <div className="mt-2">
          <Lines lines={lines} />
          <Totals cart={trip.cart} />
        </div>
      ) : (
        <p className="mt-3 text-[15px]">Nothing in the basket yet.</p>
      )}
    </section>
  );
}

function ReceiptCard({ receipt }: { receipt: NonNullable<Trip["receipt"]> }) {
  const ok = receipt.status === "succeeded";
  return (
    <section className={cn("relative overflow-hidden rounded-2xl p-5", ok ? "bg-leaf-wash" : "bg-tomato-wash")} aria-label="Receipt">
      {ok && <Confetti />}
      <div className={cn("flex items-center gap-2 text-[14px] font-semibold", ok ? "text-leaf-deep" : "text-tomato")}>
        {ok ? <Receipt className="size-5" aria-hidden /> : <CircleAlert className="size-5" aria-hidden />}
        {ok ? "Paid" : receipt.status === "declined" ? "Payment declined" : "Payment didn't go through"}
        <span className="ml-auto rounded-full bg-card px-2.5 py-0.5 text-[12px] text-muted-foreground">{PROVIDER[receipt.provider] ?? receipt.provider}</span>
      </div>
      <div className="mt-1 font-heading text-[40px] leading-none font-bold">
        <Money cents={receipt.amount_cents} />
      </div>
      {receipt.error && <p className="mt-1 text-[14px] text-tomato">{receipt.error}</p>}
      {receipt.lines.length > 0 && (
        <div className="mt-3 rounded-xl bg-card px-3">
          <Lines lines={receipt.lines.filter((l) => l.status !== "declined")} />
        </div>
      )}
      <p className="mt-2 text-[12.5px] text-muted-foreground">{ok ? "Thanks for shopping with me. Your receipt is in the Picky app too." : "Nothing was charged."}</p>
    </section>
  );
}

function Empty({ icon: Icon, title, body }: { icon: typeof Receipt; title: string; body: string }) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 py-8 text-center">
      <span className="grid size-14 place-items-center rounded-3xl bg-muted text-muted-foreground">
        <Icon className="size-7" aria-hidden />
      </span>
      <h2 className="text-[22px] font-bold">{title}</h2>
      <p className="max-w-[44ch] text-[15px] text-muted-foreground">{body}</p>
    </div>
  );
}
