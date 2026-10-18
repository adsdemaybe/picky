"use client";
// Photo review cards and the Visa payment agent, shared by /trip and the projector page (/watch):
//   PickReview   produce review: photos of the units the camera ranked, tap one (or choose N for several)
//   VerifyStack  per-item purchase check: a photo of each pick, ✓ / ✗, accepted automatically after a countdown
//   PaymentAgent checkout as the agent's timeline: mandate check → token → your approval (passkey) → authorized → receipt
// Sizes are projector-first (1440×900 and 1080p): big photos, big tap targets, one idea per card.
import { Check, CreditCard, Fingerprint, Loader2, Receipt as ReceiptIcon, ShieldCheck, KeyRound, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { Money, Panel, ProductTile } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { productBySku } from "@/lib/data";
import { fmt } from "@/lib/money";
import { cartTotal, useTrip, type Approval, type PayKind, type PayStatus, type TripStore, type Verification } from "@/lib/trip";
import { cn } from "@/lib/utils";

// ------------------------------------------------------------------------------------------------ photos

/** An <img> that walks its sources on error and ends on the illustration. */
function Photo({ srcs, alt, fallback, className }: { srcs: (string | null | undefined)[]; alt: string; fallback: React.ReactNode; className?: string }) {
  const list = srcs.filter((s): s is string => !!s);
  const [i, setI] = useState(0);
  if (i >= list.length) return <>{fallback}</>;
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={list[i]} alt={alt} onError={() => setI((n) => n + 1)} className={cn("h-full w-full object-cover", className)} />;
}

/** No photo (the replay): one unit drawn in its ripeness colour, so the cards still read as "this one vs that one". */
function UnitArt({ color, freshness, seed }: { color: string; freshness: number; seed: number }) {
  const ripe = Math.max(0, Math.min(1, 1 - freshness));
  const tilt = ((seed * 37) % 24) - 12;
  return (
    <svg viewBox="0 0 120 90" className="h-full w-full" aria-hidden>
      <rect width="120" height="90" fill={color} opacity="0.16" />
      <ellipse cx="60" cy="78" rx="30" ry="5" fill="#000" opacity="0.12" />
      <g transform={`rotate(${tilt} 60 46)`}>
        <ellipse cx="60" cy="46" rx="24" ry="31" fill={color} />
        <ellipse cx="60" cy="46" rx="24" ry="31" fill="#1c1408" opacity={0.15 + ripe * 0.55} />
        <ellipse cx="51" cy="33" rx="6" ry="10" fill="#fff" opacity="0.28" />
      </g>
    </svg>
  );
}

// ------------------------------------------------------------------------------------------------ produce review

type PickApproval = Extract<Approval, { kind: "pick" }>;

export function PickReview({ a, answer }: { a: PickApproval; answer: (v: string) => void }) {
  const p = productBySku[a.sku];
  const need = a.multi ? Math.max(1, a.need ?? 1) : 1;
  const [chosen, setChosen] = useState<string[]>([]);
  const name = p?.name ?? a.sku;
  const plural = a.sku === "AVOCADO" || /avocado/i.test(name) ? "avocados" : name.toLowerCase();
  const recommended = a.recommended ?? a.candidates[1]?.instance_id;
  const tap = (id: string) => {
    if (need === 1) return answer(id);
    setChosen((c) => (c.includes(id) ? c.filter((x) => x !== id) : c.length < need ? [...c, id] : [...c.slice(1), id]));
  };
  const anyPhoto = a.candidates.some((c) => c.thumb || c.photo);
  return (
    <div>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h3 className="text-[24px] leading-tight font-bold">{need > 1 ? `Choose ${need} ${plural}` : `Which ${plural} should it take?`}</h3>
        <Badge variant="sky">Produce review</Badge>
      </div>
      <p className="mt-1 text-[15px] text-muted-foreground">
        {anyPhoto ? "Photos from the robot's camera" : "What the camera saw"}, ranked by ripeness. {need > 1 ? `Tap the ${need} you want.` : "Tap the one you want."}
      </p>
      <div className={cn("mt-4 grid grid-cols-2 gap-3", a.candidates.length > 4 ? "@md:grid-cols-3" : "@lg:grid-cols-3")}>
        {a.candidates.map((c, i) => {
          const on = chosen.includes(c.instance_id);
          const src = c.thumb ? (c.thumb.startsWith("data:") ? c.thumb : `data:image/jpeg;base64,${c.thumb}`) : c.photo;
          return (
            <motion.button
              key={c.instance_id}
              type="button"
              onClick={() => tap(c.instance_id)}
              aria-pressed={need > 1 ? on : undefined}
              aria-label={`${name}, option ${i + 1}: ${c.note}, ${Math.round(c.freshness * 100)}% fresh`}
              initial={{ opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: 0.06 * i }}
              whileTap={{ scale: 0.97 }}
              className={cn(
                "group cursor-pointer overflow-hidden rounded-2xl bg-muted text-left ring-[3px] transition-colors hover:bg-leaf-wash",
                on ? "ring-leaf" : "ring-transparent hover:ring-leaf/50",
              )}
            >
              <span className="relative block aspect-[4/3] overflow-hidden bg-black/5">
                <Photo srcs={[src]} alt={`${name}, option ${i + 1}`} fallback={<UnitArt color={p?.label_color ?? "#6b8f3a"} freshness={c.freshness} seed={i + 1} />} />
                <span className="absolute top-2 left-2 grid size-8 place-items-center rounded-full bg-black/65 text-[14px] font-bold text-white">{i + 1}</span>
                {need > 1 && (
                  <span
                    className={cn("absolute top-2 right-2 grid size-8 place-items-center rounded-full ring-2 transition-colors", on ? "bg-leaf text-white ring-white" : "bg-white/85 text-transparent ring-black/10")}
                    aria-hidden
                  >
                    <Check className="size-5" strokeWidth={3} />
                  </span>
                )}
                {c.instance_id === recommended && (
                  <span className="absolute bottom-2 left-2">
                    <Badge variant="lime">Picky&apos;s pick</Badge>
                  </span>
                )}
              </span>
              <span className="block p-3">
                <span className="block font-heading text-[15.5px] leading-snug font-semibold @2xl:text-[17px]">{c.note}</span>
                <span className="mt-2 block h-2 overflow-hidden rounded-full bg-white">
                  <motion.span
                    className="block h-full rounded-full bg-leaf"
                    initial={{ width: 0 }}
                    animate={{ width: `${c.freshness * 100}%` }}
                    transition={{ delay: 0.2 + 0.06 * i, duration: 0.6 }}
                  />
                </span>
                <span className="mt-1.5 block text-[13px] text-muted-foreground">
                  {Math.round(c.freshness * 100)}% fresh
                  {c.expiry && `, best by ${new Date(c.expiry + "T12:00:00").toLocaleDateString("en-US", { month: "short", day: "numeric" })}`}
                </span>
              </span>
            </motion.button>
          );
        })}
      </div>
      {need > 1 && (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3">
          <span className="text-[14.5px] text-muted-foreground" aria-live="polite">
            {chosen.length} of {need} chosen
          </span>
          <div className="flex gap-2">
            <Button variant="ghost" size="lg" onClick={() => answer(a.candidates.slice(0, need).map((c) => c.instance_id).join(","))}>
              Use Picky&apos;s picks
            </Button>
            <Button size="lg" disabled={chosen.length !== need} onClick={() => answer(chosen.join(","))}>
              Take these {need}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ purchase checks

/** Every pick, photographed: the oldest open check is big (✓ / ✗ + countdown), the finished ones are a strip. */
export function VerifyStack({ className }: { className?: string }) {
  const verifications = useTrip((s) => s.verifications);
  const verify = useTrip((s) => s.verify);
  const open = verifications.filter((v) => v.status === "pending");
  const done = verifications.filter((v) => v.status !== "pending");
  const cur = open[0];
  if (!verifications.length) return null;
  return (
    <Panel className={cn("@container p-4", cur && "ring-2 ring-sky", className)} aria-label="Purchase checks">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="flex items-center gap-2 text-[17px] font-semibold">
          <ShieldCheck className="size-5 text-sky" aria-hidden /> Check each pick
        </h2>
        <span className="text-[13px] text-muted-foreground">
          {done.length} checked{open.length > 1 ? `, ${open.length - 1} more waiting` : ""}
        </span>
      </div>
      <AnimatePresence mode="popLayout" initial={false}>
        {cur && <CheckCard key={cur.id} v={cur} onAnswer={(ok, auto) => verify(cur.id, ok, auto)} />}
      </AnimatePresence>
      {done.length > 0 && (
        <ul className="mt-3 flex flex-wrap gap-2" aria-label="Checked picks">
          {done.map((v) => (
            <DoneThumb key={v.id} v={v} />
          ))}
        </ul>
      )}
    </Panel>
  );
}

function CheckCard({ v, onAnswer }: { v: Verification; onAnswer: (ok: boolean, auto?: boolean) => void }) {
  const p = productBySku[v.sku];
  const name = p?.name ?? v.sku;
  const left = useCountdown(v.auto_ms, () => onAnswer(true, true));
  const verdict =
    v.verdict === "mismatch"
      ? { text: "The camera thinks this is wrong", variant: "tomato" as const }
      : v.verdict === "uncertain"
        ? { text: "The camera isn't sure", variant: "amber" as const }
        : v.verdict === "match"
          ? { text: "The camera matched the label", variant: "leaf" as const }
          : null;
  return (
    <motion.div
      initial={{ opacity: 0, y: 14, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, x: -24, transition: { duration: 0.18 } }}
      transition={{ type: "spring", stiffness: 380, damping: 30 }}
      className="mt-3 grid gap-4 @md:grid-cols-[minmax(0,180px)_1fr] @md:items-center @2xl:grid-cols-[minmax(0,240px)_1fr]"
    >
      <div className="relative aspect-[4/3] overflow-hidden rounded-xl bg-[#16181a]">
        <Photo
          srcs={[v.photo, v.alt_photo]}
          alt={`${v.camera ?? "Robot camera"} photo of the ${name} it picked`}
          fallback={
            <span className="grid h-full w-full place-items-center" style={{ background: `${p?.label_color ?? "#888"}26` }}>
              <ProductTile color={p?.label_color ?? "#888"} category={p?.category ?? "pantry"} name={name} size={84} />
            </span>
          }
        />
        <span className="absolute bottom-1.5 left-1.5 rounded bg-black/70 px-1.5 py-0.5 text-[11px] font-semibold text-white">{v.camera ?? "Robot camera"}</span>
      </div>
      <div className="min-w-0">
        <div className="text-[13.5px] font-semibold text-sky">Just went in the basket</div>
        <h3 className="mt-0.5 text-[22px] leading-tight font-bold">
          {v.qty > 1 ? `${v.qty} × ` : ""}
          {name}
        </h3>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-[14px] text-muted-foreground">
          {p && <Money cents={p.price_cents * v.qty} className="font-semibold text-foreground" />}
          {verdict && <Badge variant={verdict.variant}>{verdict.text}</Badge>}
          {v.note && <span className="line-clamp-1">{v.note}</span>}
        </div>
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Button variant="outline" size="lg" className="h-14 min-w-0 gap-1.5 border-tomato/40 px-2 text-[15.5px] text-tomato hover:bg-tomato-wash" onClick={() => onAnswer(false)}>
            <X className="size-5" strokeWidth={3} /> Wrong item
          </Button>
          <Button size="lg" className="h-14 min-w-0 gap-1.5 px-2 text-[15.5px]" onClick={() => onAnswer(true)}>
            <Check className="size-5" strokeWidth={3} /> Looks right
          </Button>
        </div>
        <div className="mt-2.5">
          <div className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden>
            <div className="h-full rounded-full bg-sky" style={{ width: `${(left / v.auto_ms) * 100}%`, transition: "width 250ms linear" }} />
          </div>
          <div className="mt-1 text-[12.5px] text-muted-foreground" aria-live="off">
            Accepted automatically in {Math.ceil(left / 1000)} s
          </div>
        </div>
      </div>
    </motion.div>
  );
}

function DoneThumb({ v }: { v: Verification }) {
  const p = productBySku[v.sku];
  const name = p?.name ?? v.sku;
  const bad = v.status === "rejected";
  return (
    <motion.li layout initial={{ opacity: 0, scale: 0.8 }} animate={{ opacity: 1, scale: 1 }} className="relative" title={`${name}: ${bad ? "flagged" : v.status === "auto" ? "accepted automatically" : "looks right"}`}>
      <span className={cn("block size-14 overflow-hidden rounded-lg bg-muted ring-2", bad ? "ring-tomato" : "ring-leaf/40")}>
        <Photo
          srcs={[v.photo, v.alt_photo]}
          alt={name}
          fallback={
            <span className="grid h-full w-full place-items-center">
              <ProductTile color={p?.label_color ?? "#888"} category={p?.category ?? "pantry"} name={name} size={36} />
            </span>
          }
        />
      </span>
      <span className={cn("absolute -right-1 -bottom-1 grid size-5 place-items-center rounded-full text-white ring-2 ring-card", bad ? "bg-tomato" : "bg-leaf")}>
        {bad ? <X className="size-3" strokeWidth={3.5} /> : <Check className="size-3" strokeWidth={3.5} />}
      </span>
    </motion.li>
  );
}

/** ms left of `total`, ticking every 250 ms from mount; `onDone` once at zero. */
function useCountdown(total: number, onDone: () => void) {
  const [left, setLeft] = useState(total);
  const done = useRef(onDone);
  useEffect(() => {
    done.current = onDone;
  });
  useEffect(() => {
    const end = Date.now() + total;
    let fired = false;
    const t = setInterval(() => {
      const l = Math.max(0, end - Date.now());
      setLeft(l);
      if (l === 0 && !fired) {
        fired = true;
        clearInterval(t);
        done.current();
      }
    }, 250);
    return () => clearInterval(t);
  }, [total]);
  return left;
}

// ------------------------------------------------------------------------------------------------ payment agent

type Step = { kind: PayKind; title: string; detail: string; status: PayStatus; narration?: string };
const STEP_ICON: Record<PayKind, typeof Check> = { mandate: ShieldCheck, token: KeyRound, approval: Fingerprint, authorized: CreditCard, receipt: ReceiptIcon };

/** Checkout as the payment agent's steps. Derived from the trip (mandate decision, checkout card, your answer, receipt);
 *  payment.agent_step events from the gateway override a step's status and text. */
export function paymentTimeline(s: Pick<TripStore, "approval" | "pending" | "approvalsLog" | "receipt" | "phase" | "tripState" | "payEvents" | "lastCheckout">) {
  const co = s.lastCheckout;
  const waiting = s.approval?.kind === "checkout" || s.pending.some((p) => p.kind === "checkout");
  const answer = [...s.approvalsLog].reverse().find((l) => l.kind === "checkout")?.answer;
  const approved = !waiting && (answer === "approve" || !!s.receipt || s.tripState === "paid");
  const declined = !waiting && !approved && !!answer;
  const started = !!co || !!s.receipt || s.payEvents.length > 0;
  const blocked = co?.reasons.some((r) => r.level === "block");
  const okText = co?.reasons.filter((r) => r.level === "ok").map((r) => r.text) ?? [];
  const askText = co?.reasons.filter((r) => r.level !== "ok").map((r) => r.text) ?? [];
  const steps: Step[] = [
    {
      kind: "mandate",
      title: "Mandate check",
      detail: blocked ? (askText[0] ?? "Outside your rules") : co ? [okText[0] ?? "Within your rules", askText[0]].filter(Boolean).join(". ") : "Checking your spending rules",
      status: blocked ? "failed" : co || s.receipt ? "done" : started ? "active" : "pending",
    },
    {
      kind: "token",
      title: "Payment token requested",
      detail: co ? `Visa network token for card •• 4242, bound to cart ${co.cart_hash.slice(0, 8) || "hash"}. No card number leaves your wallet.` : "A one-time token for this cart only",
      status: co || s.receipt ? (blocked ? "pending" : "done") : "pending",
    },
    {
      kind: "approval",
      title: "Your approval",
      detail: waiting ? "Waiting for you. Nothing is charged until you approve." : approved ? "Approved with your passkey" : declined ? "Not yet. The basket is held for you." : "Checkout always waits for you",
      status: waiting ? "active" : approved ? "done" : declined ? "failed" : "pending",
    },
    {
      kind: "authorized",
      title: "Authorized",
      detail: s.receipt ? `${fmt(s.receipt.total_cents)} authorized, ${s.receipt.provider}` : approved ? "Authorizing with the Visa sandbox" : "The card network authorizes the exact total",
      status: s.receipt ? "done" : approved ? "active" : "pending",
    },
    {
      kind: "receipt",
      title: "Receipt",
      detail: s.receipt ? `Receipt ${s.receipt.receipt_id}` : "Signed receipt, saved to your history",
      status: s.receipt ? "done" : "pending",
    },
  ];
  for (const e of s.payEvents) {
    const i = steps.findIndex((x) => x.kind === e.kind);
    // the shopper's open checkout card outranks a stale step: the approval row stays "waiting for you"
    const status = e.kind === "approval" && waiting && e.status === "done" ? "active" : e.status;
    if (i >= 0) steps[i] = { ...steps[i], status, title: e.title ?? steps[i].title, detail: e.detail ?? steps[i].detail, narration: e.narration };
  }
  const label = [...s.payEvents].reverse().find((e) => e.label)?.label;
  return { steps, started, waiting, live: s.payEvents.length > 0, label };
}

/** The Visa payment agent card. While the checkout card is open it carries the passkey button (= the explicit
 *  approval CLAUDE.md requires); `compact` = the finished timeline under the receipt. */
export function PaymentAgent({ compact }: { compact?: boolean }) {
  const s = useTrip();
  const { steps, waiting, label: liveLabel } = paymentTimeline(s);
  const co = s.approval?.kind === "checkout" ? s.approval : null;
  const total = co?.total_cents ?? s.lastCheckout?.total_cents ?? s.receipt?.total_cents ?? cartTotal(s.cart) + s.shippingCents;
  const [busy, setBusy] = useState(false);
  // always a sandbox: the gateway's own label when its payment agent reports steps
  const label = liveLabel ?? "Sandbox / simulated";

  const approve = () => {
    if (!co || busy) return;
    setBusy(true);
    // a passkey-style confirmation beat before the answer goes out (no real WebAuthn in the demo)
    setTimeout(() => {
      setBusy(false);
      s.answer("approve");
    }, 650);
  };

  if (compact)
    return (
      <div className="rounded-2xl bg-card p-3 ring-1 ring-foreground/[0.07]" aria-label="Visa payment agent steps">
        <div className="mb-2 flex items-center gap-2 text-[13px] font-semibold">
          <CreditCard className="size-4 text-[#1a1f71]" aria-hidden /> Visa payment agent
          <Badge variant="muted" className="ml-auto">
            {label}
          </Badge>
        </div>
        <ol className="flex flex-wrap gap-1.5">
          {steps.map((st) => (
            <li key={st.kind} className={cn("flex items-center gap-1 rounded-full px-2.5 py-1 text-[12.5px] font-medium", st.status === "done" ? "bg-leaf-wash text-leaf-deep" : st.status === "failed" ? "bg-tomato-wash text-tomato" : "bg-muted text-muted-foreground")}>
              {st.status === "done" ? <Check className="size-3.5" strokeWidth={3} /> : st.status === "failed" ? <X className="size-3.5" strokeWidth={3} /> : null}
              {st.title}
            </li>
          ))}
        </ol>
      </div>
    );

  return (
    <motion.section
      initial={{ opacity: 0, y: 20, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 340, damping: 28 }}
      className={cn("@container overflow-hidden rounded-2xl bg-card shadow-md ring-2", waiting ? "ring-amber" : "ring-foreground/10")}
      role={waiting ? "alertdialog" : undefined}
      aria-label="Visa payment agent"
    >
      <header className="flex flex-wrap items-center gap-2.5 bg-[#1a1f71] px-5 py-3 text-white">
        <CreditCard className="size-5" aria-hidden />
        <span className="text-[16px] font-bold">Visa payment agent</span>
        <span className="rounded-full bg-white/15 px-2.5 py-0.5 text-[12px] font-semibold">{label}</span>
        <span className="ml-auto text-[13px] text-white/75">
          Paying <Money cents={total} className="font-semibold text-white" />
        </span>
      </header>
      <ol className="flex flex-col px-5 pt-4 pb-2">
        {steps.map((st, i) => {
          const Icon = STEP_ICON[st.kind];
          return (
            <li key={st.kind} className="relative flex gap-3 pb-3.5">
              {i < steps.length - 1 && <span className={cn("absolute top-9 bottom-0 left-[17px] w-0.5", st.status === "done" ? "bg-leaf/40" : "bg-border")} aria-hidden />}
              <span
                className={cn(
                  "relative z-10 grid size-9 shrink-0 place-items-center rounded-full",
                  st.status === "done" ? "bg-leaf text-white" : st.status === "active" ? "bg-amber text-white" : st.status === "failed" ? "bg-tomato text-white" : "bg-muted text-muted-foreground",
                )}
              >
                {st.status === "done" ? (
                  <Check className="size-4.5" strokeWidth={3} />
                ) : st.status === "failed" ? (
                  <X className="size-4.5" strokeWidth={3} />
                ) : st.status === "active" && st.kind !== "approval" ? (
                  <Loader2 className="size-4.5 animate-spin" />
                ) : (
                  <Icon className="size-4.5" />
                )}
              </span>
              <div className="min-w-0 flex-1 pt-1">
                <div className={cn("text-[16px] leading-tight font-semibold", st.status === "pending" && "text-muted-foreground")}>{st.title}</div>
                <div className="mt-0.5 text-[13.5px] leading-snug text-muted-foreground">{st.detail}</div>
                {st.narration && st.status !== "pending" && <div className="mt-1 text-[13.5px] leading-snug text-foreground/80 italic">&ldquo;{st.narration}&rdquo;</div>}
                {st.kind === "approval" && co && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    <Button size="lg" className="h-14 min-w-[240px] flex-1 text-[16.5px] font-semibold" onClick={approve} disabled={busy} aria-label={`Approve and pay ${fmt(total)} with passkey`}>
                      {busy ? <Loader2 className="size-5 animate-spin" /> : <Fingerprint className="size-6" />}
                      {busy ? "Checking your passkey" : `Approve ${fmt(total)} with passkey`}
                    </Button>
                    <Button variant="outline" size="lg" className="h-14" onClick={() => s.answer("decline")} disabled={busy}>
                      Not yet
                    </Button>
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>
      <p className="border-t border-border px-5 py-2.5 text-[12.5px] text-muted-foreground">
        Test mode: Stripe test card behind the Visa adapter, so no real money moves. The mandate is checked in code, never by the AI.
      </p>
    </motion.section>
  );
}
