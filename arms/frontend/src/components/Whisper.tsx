"use client";
// Cart Whisperer cards (docs/cart-whisperer.md §8): ≤3 per phase, zero is valid, every card says why (reason + reason
// codes + signals by source), one tap to accept or decline, nothing sponsored. Data and rules: lib/whisper.ts.
import { ArrowLeftRight, Ban, BellRing, Check, ChevronDown, Clock, Hash, Loader2, Plus, RotateCcw, ShieldAlert, Sparkles, Tag, Trash2, type LucideIcon } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useEffect, useId, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { Panel, ProductTile, SectionTitle } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { productBySku } from "@/lib/data";
import { useGateway } from "@/lib/gateway/useGateway";
import { fmt } from "@/lib/money";
import type { Draft } from "@/lib/order";
import { useTrip } from "@/lib/trip";
import { cn } from "@/lib/utils";
import {
  ACCEPT_LABEL,
  KIND_LABEL,
  SOURCE_LABEL,
  applyToDraft,
  codeLabel,
  lineSkuOf,
  signalText,
  signalsBySource,
  tally,
  usePlanningWhisper,
  useTripWhisper,
  type WhisperCard,
  type WhisperKind,
  type WhisperOutcome,
  type WhisperPhase,
} from "@/lib/whisper";

const TONE: Record<WhisperKind, { wash: string; ink: string; Icon: LucideIcon }> = {
  skip: { wash: "bg-tomato-wash", ink: "text-tomato", Icon: Ban },
  wait: { wash: "bg-amber-wash", ink: "text-amber-ink", Icon: Clock },
  remove: { wash: "bg-tomato-wash", ink: "text-tomato", Icon: Trash2 },
  swap_save: { wash: "bg-leaf-wash", ink: "text-leaf-deep", Icon: ArrowLeftRight },
  qty: { wash: "bg-sky-wash", ink: "text-sky", Icon: Hash },
  deal: { wash: "bg-grape-wash", ink: "text-grape", Icon: Tag },
  complete: { wash: "bg-card ring-1 ring-foreground/[0.08]", ink: "text-leaf-deep", Icon: Plus },
  replenish: { wash: "bg-card ring-1 ring-foreground/[0.08]", ink: "text-leaf-deep", Icon: RotateCcw },
};

type Answer = (card: WhisperCard, decision: "accept" | "decline") => void;

export function WhisperCardView({ card, outcome, busy, disabled, inList, readOnly, onAnswer }: { card: WhisperCard; outcome?: WhisperOutcome; busy?: boolean; disabled?: boolean; inList?: boolean; readOnly?: boolean; onAnswer: Answer }) {
  const [open, setOpen] = useState(false);
  const whyId = useId();
  const tone = TONE[card.kind];
  const sku = card.sku ?? card.line_sku;
  const p = sku ? productBySku[sku] : null;
  const keepWord = card.kind === "skip" || card.kind === "wait" || card.kind === "remove";
  const groups = signalsBySource(card.signals);
  const money =
    card.savings_cents && card.savings_cents > 0
      ? `${card.kind === "wait" ? "Expected to save" : card.kind === "skip" ? "Avoids" : "Saves"} ${fmt(card.savings_cents)}`
      : card.action === "add"
        ? `+${fmt(card.price_cents * (card.qty ?? 1))}`
        : null;
  return (
    <motion.article layout className={cn("flex flex-col rounded-2xl p-4", tone.wash)} data-testid="whisper-card" data-kind={card.kind} aria-label={`${KIND_LABEL[card.kind]}: ${card.name}`}>
      <div className="flex items-center justify-between gap-2">
        <span className={cn("flex items-center gap-1.5 font-semibold", tone.ink)}>
          <tone.Icon className="size-4" aria-hidden />
          {KIND_LABEL[card.kind]}
        </span>
        {money && <span className="rounded-full bg-white/70 px-2.5 py-0.5 text-[12.5px] font-semibold whitespace-nowrap dark:bg-black/30">{money}</span>}
      </div>
      <div className="mt-2 flex items-start gap-3">
        {p && <ProductTile color={p.label_color} category={p.category} name={p.name} size={36} />}
        <div className="min-w-0 flex-1">
          <div className="leading-snug font-semibold">{card.title || card.name}</div>
          <div className="text-[12.5px] text-muted-foreground">
            {[card.title ? card.name : null, card.price_cents > 0 ? fmt(card.price_cents) : null, card.aisle_label, card.detour_m != null ? (card.detour_m < 0.5 ? "on the route" : `+${card.detour_m.toFixed(0)} m walk`) : null]
              .filter(Boolean)
              .join(" · ")}
          </div>
          {card.reason && <p className="mt-1 text-[14.5px] leading-snug">{card.reason}</p>}
        </div>
      </div>
      {(card.needs_approval || card.sponsored) && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {card.needs_approval && <Badge variant="amber">Needs your OK</Badge>}
          {card.sponsored && <Badge variant="grape">Sponsored</Badge>}
        </div>
      )}

      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={whyId}
        className="mt-2 flex w-fit cursor-pointer items-center gap-1 rounded-md text-[13px] font-semibold text-muted-foreground hover:text-foreground"
      >
        <ChevronDown className={cn("size-3.5 transition-transform", open && "rotate-180")} aria-hidden />
        Why am I seeing this?
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div id={whyId} initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }} exit={{ height: 0, opacity: 0 }} className="overflow-hidden">
            <div className="mt-2 flex flex-col gap-2 rounded-xl bg-white/60 p-3 text-[13px] dark:bg-black/25">
              {card.reason_codes.length > 0 && (
                <div>
                  <div className="text-[11.5px] font-semibold tracking-wide text-muted-foreground uppercase">Reason codes</div>
                  <ul className="mt-1 flex flex-wrap gap-1.5">
                    {card.reason_codes.map((c) => (
                      <li key={c} className="flex items-center gap-1.5 rounded-full bg-card px-2 py-0.5 ring-1 ring-foreground/[0.08]">
                        <code className="font-mono text-[11px] text-muted-foreground">{c}</code>
                        <span>{codeLabel(c)}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
              {groups.map((g) => (
                <div key={g.source}>
                  <div className="text-[11.5px] font-semibold tracking-wide text-muted-foreground uppercase">{SOURCE_LABEL[g.source]}</div>
                  <dl className="mt-1 grid grid-cols-[auto_1fr] gap-x-3 gap-y-0.5">
                    {g.rows.map((s) => {
                      const t = signalText(s);
                      return (
                        <div key={s.name} className="contents">
                          <dt className="text-muted-foreground">{t.label}</dt>
                          <dd className="tabular font-medium break-words">{t.value}</dd>
                        </div>
                      );
                    })}
                  </dl>
                </div>
              ))}
              <p className="text-muted-foreground">
                {card.sponsored ? "Sponsored placement, labeled." : "Not sponsored."} Picked by Picky&apos;s rules from these signals
                {card.score ? ` (score ${card.score.toFixed(2)})` : ""}; your spending rules still apply when you accept.
              </p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      <div className="mt-3 border-t border-foreground/[0.08] pt-3">
        {outcome ? (
          <p
            role="status"
            className={cn(
              "flex items-start gap-2 text-[14px] font-medium",
              outcome.status === "denied" ? "text-tomato" : outcome.status === "ask" ? "text-amber-ink" : outcome.decision === "decline" ? "text-muted-foreground" : "text-leaf-deep",
            )}
          >
            {outcome.status === "denied" ? (
              <ShieldAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
            ) : card.action === "remind" && outcome.decision === "accept" ? (
              <BellRing className="mt-0.5 size-4 shrink-0" aria-hidden />
            ) : (
              <Check className="mt-0.5 size-4 shrink-0" aria-hidden />
            )}
            {outcome.text}
          </p>
        ) : readOnly ? (
          <p className="text-[13.5px] text-muted-foreground">Not answered</p>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={busy || disabled} onClick={() => onAnswer(card, "accept")}>
              {busy ? <Loader2 className="animate-spin" /> : null}
              {ACCEPT_LABEL[card.action]}
            </Button>
            <Button size="sm" variant="ghost" disabled={busy || disabled} onClick={() => onAnswer(card, "decline")}>
              {keepWord && inList ? "Keep it" : "No thanks"}
            </Button>
          </div>
        )}
      </div>
    </motion.article>
  );
}

/** Up to 3 cards, the zero state, and what the accepted ones saved / avoided / added. */
export function WhisperCards({
  cards,
  outcomes,
  busy,
  onAnswer,
  zeroText,
  emptyReason,
  hidden = 0,
  inList,
  readOnly,
  className,
}: {
  cards: WhisperCard[];
  outcomes: Record<string, WhisperOutcome>;
  busy: string | null;
  onAnswer: Answer;
  zeroText: string;
  emptyReason?: string | null;
  /** WhisperResponse.hidden_declined */
  hidden?: number;
  inList?: (c: WhisperCard) => boolean;
  readOnly?: boolean;
  className?: string;
}) {
  const shown = cards.slice(0, 3);
  const t = tally(shown, outcomes);
  const hiddenLine = hidden > 0 && (
    <p className="text-[13px] text-muted-foreground">
      {hidden === 1 ? "1 suggestion is" : `${hidden} suggestions are`} hidden because you said no before.
    </p>
  );
  if (!shown.length)
    return (
      <div className="flex flex-col gap-2">
        <Panel className="p-5" data-testid="whisper-zero">
          <p className="font-semibold">{zeroText}</p>
          <p className="mt-0.5 text-[14px] text-muted-foreground">
            {emptyReason && !/^nothing worth/i.test(emptyReason) ? emptyReason : "Picky only speaks up when it saves you money, completes your list or stops a buy you don't need."}
          </p>
        </Panel>
        {hiddenLine}
      </div>
    );
  const parts = [t.saved > 0 && `saved ${fmt(t.saved)}`, t.avoided > 0 && `avoided ${fmt(t.avoided)}${t.waitEstimate ? " (expected)" : ""}`, t.added > 0 && `added ${fmt(t.added)}`].filter(Boolean);
  return (
    <div className="flex flex-col gap-2">
      <div className={cn("grid gap-3", className)}>
        {shown.map((c) => (
          <WhisperCardView key={c.card_id} card={c} outcome={outcomes[c.card_id]} busy={busy === c.card_id} disabled={!!busy && busy !== c.card_id} inList={inList?.(c)} readOnly={readOnly} onAnswer={onAnswer} />
        ))}
      </div>
      {parts.length > 0 && (
        <p className="text-[13.5px] font-medium text-leaf-deep" data-testid="whisper-tally">
          You {parts.join(" · ")}.
        </p>
      )}
      {hiddenLine}
    </div>
  );
}

function CardSkeletons({ n = 2 }: { n?: number }) {
  return (
    <div className="grid gap-3 md:grid-cols-2" aria-hidden>
      {Array.from({ length: n }, (_, i) => (
        <Skeleton key={i} className="h-[150px] rounded-2xl" />
      ))}
    </div>
  );
}

const Heading = ({ children }: { children: ReactNode }) => (
  <SectionTitle
    action={
      <span className="flex items-center gap-1.5 text-[12.5px] font-medium text-muted-foreground">
        <Sparkles className="size-3.5 text-sky" aria-hidden /> Cart Whisperer · not sponsored
      </span>
    }
  >
    {children}
  </SectionTitle>
);

// ---------------------------------------------------------------------------------------------------- /order

/** "Picky's advice" on the order page. Gateway mode with Cart Whisperer: the planning pass for this list (accept edits
 *  the list through `onDraft`); otherwise `fallback` (the demo SKIP / WAIT advice), unchanged. */
export function PlanningWhisper({ draft, onDraft, fallback }: { draft: Draft; onDraft: (fn: (d: Draft) => Draft) => void; fallback: ReactNode }) {
  const live = useGateway((s) => s.status === "live");
  const cw = usePlanningWhisper(draft, live);
  if (cw.status === "idle" || cw.status === "missing" || cw.status === "error") return <>{fallback}</>;

  const onAnswer: Answer = async (card, decision) => {
    const r = await cw.answer(card, decision);
    if (!r.ok) {
      toast.error(r.stale ? "That advice is out of date, so Picky is checking again" : `Couldn't send that: ${r.error}`);
      if (r.stale) cw.refresh();
      return;
    }
    if (decision === "accept" && r.outcome.status !== "denied") onDraft((d) => applyToDraft(d, card));
    if (r.outcome.status === "denied") toast.error(r.outcome.text);
  };

  return (
    <section data-testid="whisper-planning">
      <Heading>Picky&apos;s advice</Heading>
      {cw.status === "loading" || !cw.set ? (
        <CardSkeletons />
      ) : (
        <WhisperCards
          cards={cw.set.cards}
          outcomes={cw.outcomes}
          busy={cw.busy}
          onAnswer={onAnswer}
          zeroText="Nothing worth a stop today"
          emptyReason={cw.set.empty_reason}
          hidden={cw.set.hidden_declined}
          inList={(c) => draft.lines.some((l) => l.sku === lineSkuOf(c))}
          className="md:grid-cols-2"
        />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------------------------------- /watch, /trip

const DONE_STATES = new Set(["paid", "followup", "done", "failed"]);
const TITLE: Record<WhisperPhase, string> = { planning: "Before it leaves", in_trip: "While it shops", checkout: "Before you pay" };

/** The live trip's cards: whisper.cards from the trip socket (in-trip, at checkout), plus one GET per phase. Hidden
 *  for demo trips and for a gateway without Cart Whisperer (the checkout extras approval still shows there). */
export function TripWhisperPanel({ className }: { className?: string }) {
  const source = useTrip((s) => s.source);
  const tripId = useTrip((s) => s.tripId);
  const storeId = useTrip((s) => s.storeId);
  const tripState = useTrip((s) => s.tripState);
  const paid = useTrip((s) => !!s.receipt);
  const cartSkus = useTrip((s) => s.cart.map((l) => l.sku).join(","));
  const w = useTripWhisper();
  const gateway = source === "gateway" && !!tripId;
  const { bind, load } = w;

  useEffect(() => {
    if (gateway && tripId) bind(tripId);
  }, [gateway, tripId, bind]);
  useEffect(() => {
    if (!gateway || !tripId || !tripState || DONE_STATES.has(tripState)) return;
    void load(tripId, storeId, tripState === "checkout" ? "checkout" : "in_trip");
  }, [gateway, tripId, storeId, tripState, load]);

  if (!gateway || !tripId || w.tripId !== tripId || !w.available) return null;
  const phase: WhisperPhase | null = w.phases.checkout ? "checkout" : w.phases.in_trip ? "in_trip" : w.phases.planning ? "planning" : null;
  if (!phase) return null;
  const set = w.phases[phase]!;
  const over = paid || (!!tripState && DONE_STATES.has(tripState));
  // after payment only the answered cards stay (what was skipped, waited on, added)
  const cards = over ? Object.values(w.seen).filter((c) => w.outcomes[c.card_id]) : set.cards;
  if (over && !cards.length) return null;
  const inCart = new Set(cartSkus.split(","));

  return (
    <Panel className={cn("p-4", className)} data-testid="whisper-trip" aria-label="Cart Whisperer">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-[17px] font-semibold">{over ? "Picky's advice this trip" : TITLE[phase]}</h2>
        <span className="flex items-center gap-1.5 text-[12.5px] font-medium text-muted-foreground">
          <Sparkles className="size-3.5 text-sky" aria-hidden /> Cart Whisperer · not sponsored
        </span>
      </div>
      <WhisperCards
        cards={cards}
        outcomes={w.outcomes}
        busy={w.busy}
        readOnly={over}
        onAnswer={(card, decision) => void w.answer(card, decision, { tripId, storeId, phase })}
        zeroText="Nothing worth adding today"
        emptyReason={set.empty_reason}
        hidden={over ? 0 : set.hidden_declined}
        inList={(c) => inCart.has(lineSkuOf(c) ?? "")}
      />
    </Panel>
  );
}
