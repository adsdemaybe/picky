"use client";
import { Ban, Bot, Check, Clock, Info, PackageCheck, Sparkles, Store, UserRound } from "lucide-react";
import { motion } from "motion/react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { toast } from "sonner";
import { StoreThumb } from "@/components/art";
import { ImageSlot } from "@/components/media";
import { CountUp } from "@/components/motion";
import { Meter, Money, Panel, ProductTile, SectionTitle } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { STORES, STORE_IDS, maya, productBySku, slotsOf, type StoreId } from "@/lib/data";
import { startGatewayOrder } from "@/lib/gateway/tripSource";
import { useGateway } from "@/lib/gateway/useGateway";
import { fmt } from "@/lib/money";
import { PRESETS, defaultAccepted, draftTotal, expandIntent, mandateCheck, type Draft } from "@/lib/order";
import { planRoute } from "@/lib/routing";
import { useTrip, type Mode } from "@/lib/trip";

export default function OrderPage() {
  return (
    <Suspense>
      <OrderBuilder />
    </Suspense>
  );
}

function OrderBuilder() {
  const params = useSearchParams();
  const add = params.get("add");
  const [storeId, setStoreId] = useState<StoreId>(params.get("store") === "store_tmobile" ? "store_tmobile" : "store_ave");
  const initial = params.get("preset") === "basics" ? PRESETS.store_ave[1].text : PRESETS[storeId][0].text;
  const [text, setText] = useState<string>(initial);
  const [draft, setDraft] = useState<Draft>(() => expandIntent(initial, storeId, add));
  const store = STORES[storeId];
  const slotBySku = slotsOf(storeId);
  const [accepted, setAccepted] = useState<Set<string>>(() => defaultAccepted(draft));
  const [mode, setMode] = useState<Mode>("with_me");
  const [thinking, setThinking] = useState(false);
  const [sending, setSending] = useState(false);
  const placeOrder = useTrip((s) => s.placeOrder);
  const router = useRouter();
  const gatewayLive = useGateway((s) => s.status === "live");
  const live = gatewayLive && store.kind === "grocery";

  const runDemo = () => {
    placeOrder(draft, [...accepted], mode);
    router.push("/trip");
  };

  // Gateway mode (grocery): the gateway plans from live stock and the robot starts by itself, so this only happens on
  // "Send", never on "Plan it". The preview above stays the instant local plan. T-Mobile always stays demo.
  const send = async () => {
    const gateway = store.kind === "grocery" && (gatewayLive || (await useGateway.getState().check()));
    if (!gateway) {
      if (store.kind === "grocery" && useGateway.getState().status === "down") toast("No gateway reachable, so this is the demo trip");
      return runDemo();
    }
    setSending(true);
    const text = draft.intent + (add && productBySku[add] ? `, plus 1 ${productBySku[add].name}` : "");
    const r = await startGatewayOrder(useTrip, { text, storeId, mode, title: draft.title });
    setSending(false);
    if (r.kind === "following") {
      toast(r.reply);
      router.push(r.url);
    } else if (r.kind === "unreachable") {
      toast("The gateway didn't answer, so this is the demo trip", { description: r.reply });
      runDemo();
    } else if (r.kind === "failed") {
      toast.error(r.reply, { action: { label: "Run the demo trip", onClick: runDemo } });
    } else {
      toast(r.reply);
    }
  };

  const submit = (t: string, sid: StoreId = storeId) => {
    setText(t);
    setThinking(true);
    setTimeout(() => {
      const d = expandIntent(t, sid);
      setDraft(d);
      setAccepted(defaultAccepted(d));
      setThinking(false);
    }, 650);
  };

  const detours = useMemo(() => {
    const start = { id: "start", pose: store.robot_home };
    const end = { id: "end", pose: store.checkout };
    const list = draft.lines.filter((l) => l.sourcing === "in_store").map((l) => ({ id: l.sku, pose: slotBySku[l.sku].approach_pose }));
    const base = planRoute(store, start, list, end).total_m;
    const out: Record<string, number> = { __base: base };
    for (const r of draft.recs) out[r.rec_id] = planRoute(store, start, [...list, { id: r.sku, pose: slotBySku[r.sku].approach_pose }], end).total_m - base;
    return out;
  }, [draft, store, slotBySku]);

  const totals = draftTotal(draft, accepted);
  const lines = [
    ...draft.lines.map((l) => ({ sku: l.sku, unit_cents: l.unit_cents })),
    ...draft.recs.filter((r) => accepted.has(r.rec_id)).map((r) => ({ sku: r.sku, unit_cents: r.unit_cents })),
  ];
  const verdict = mandateCheck(totals.total, lines, storeId);
  const over = totals.total > draft.budget_cents;

  const toggle = (id: string) =>
    setAccepted((s) => {
      const n = new Set(s);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });

  return (
    <div className="flex flex-col gap-8">
      <section className="animate-rise">
        <h1 className="text-[36px] leading-[1.05] font-bold md:text-[46px]">Send the robot shopping</h1>
        <p className="mt-2 text-[16px] text-muted-foreground">Pick a store, say what you need, and it plans the shortest route past everything.</p>

        <div className="mt-5 grid gap-3 sm:grid-cols-2" role="radiogroup" aria-label="Store">
          {STORE_IDS.map((id) => {
            const on = storeId === id;
            const grocery = STORES[id].kind === "grocery";
            return (
              <motion.button
                key={id}
                role="radio"
                aria-checked={on}
                whileTap={{ scale: 0.985 }}
                onClick={() => {
                  if (on) return;
                  setStoreId(id);
                  submit(PRESETS[id][0].text, id);
                }}
                className={cn(
                  "relative flex cursor-pointer items-center gap-4 rounded-2xl bg-card p-3 pr-5 text-left ring-1 transition-colors",
                  on ? (grocery ? "bg-leaf-wash ring-2 ring-leaf" : "bg-grape-wash ring-2 ring-grape") : "ring-foreground/[0.08] hover:bg-muted/60",
                )}
              >
                <ImageSlot
                  slot={grocery ? "storeGrocery" : "storeWireless"}
                  alt={STORES[id].name}
                  className="size-20 shrink-0 rounded-xl"
                  fallback={<StoreThumb grocery={grocery} />}
                  hint={false}
                />
                <span className="min-w-0 flex-1">
                  <span className="block font-heading text-[18px] font-semibold">{STORES[id].short}</span>
                  <span className="mt-0.5 block text-[13.5px] text-muted-foreground">
                    {grocery ? "Groceries at The Ave, 5 aisles" : "Phones and accessories on University Way"}
                  </span>
                </span>
                <span
                  className={cn(
                    "grid size-6 shrink-0 place-items-center rounded-full ring-2",
                    on ? (grocery ? "bg-leaf ring-leaf" : "bg-grape ring-grape") : "ring-input",
                  )}
                  aria-hidden
                >
                  {on && (
                    <motion.span initial={{ scale: 0 }} animate={{ scale: 1 }} transition={{ type: "spring", stiffness: 600, damping: 20 }}>
                      <Check className="size-3.5 text-white" strokeWidth={3.5} />
                    </motion.span>
                  )}
                </span>
              </motion.button>
            );
          })}
        </div>

        <form
          className="mt-4"
          onSubmit={(e) => {
            e.preventDefault();
            submit(text);
          }}
        >
          <div className="flex items-center gap-2 rounded-full bg-card p-2 pl-5 shadow-md ring-1 ring-foreground/[0.06] focus-within:ring-2 focus-within:ring-ring">
            <Sparkles className="size-5 text-sky" aria-hidden />
            <input
              value={text}
              onChange={(e) => setText(e.target.value)}
              className="min-w-0 flex-1 bg-transparent text-[18px] outline-none placeholder:text-muted-foreground"
              placeholder={PRESETS[storeId][0].text}
              aria-label="What do you need?"
            />
            <Button type="submit" size="lg" disabled={thinking}>
              {thinking ? "Planning…" : "Plan it"}
            </Button>
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {PRESETS[storeId].map((p) => (
              <button
                key={p.id}
                type="button"
                onClick={() => submit(p.text)}
                className="h-9 cursor-pointer rounded-full bg-card px-3.5 text-[13.5px] font-medium ring-1 ring-foreground/[0.08] hover:bg-muted"
              >
                {p.label}
              </button>
            ))}
            <span className="ml-auto hidden text-[13px] text-muted-foreground sm:block">You can also text this to Picky on iMessage.</span>
          </div>
        </form>
      </section>

      <div className={cn("grid gap-8 transition-opacity lg:grid-cols-12", thinking && "pointer-events-none opacity-40")}>
        <div className="flex flex-col gap-8 lg:col-span-8">
          <section>
            <SectionTitle>
              {draft.title} <span className="font-sans text-[15px] font-medium text-muted-foreground">for {maya.profile.display_name}</span>
            </SectionTitle>
            {live && (
              <p className="-mt-2 mb-3 text-[13px] text-muted-foreground">Preview. The robot re-plans from live store stock when you send it, and asks before any extra.</p>
            )}
            <Panel className="p-2">
              {draft.lines.map((l) => {
                const p = productBySku[l.sku];
                return (
                  <div key={l.line_id} className="flex items-center gap-3 rounded-xl p-3">
                    <ProductTile color={p.label_color} category={p.category} name={p.name} size={44} />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-semibold">
                          <span className="tabular text-muted-foreground">{l.qty} × </span>
                          {p.name}
                        </span>
                        {l.vegetarian && <Badge variant="leaf">Vegetarian</Badge>}
                      </div>
                      {l.note && <div className="text-[13px] text-muted-foreground">{l.note}</div>}
                      {l.sourcing === "online" && (
                        <Badge variant="grape" className="mt-1 sm:hidden">
                          <PackageCheck /> Online, 2 h
                        </Badge>
                      )}
                    </div>
                    {l.sourcing === "in_store" ? (
                      <Badge variant="leaf" className="hidden sm:inline-flex">
                        <Store /> In store
                      </Badge>
                    ) : (
                      <Badge variant="grape" className="hidden sm:inline-flex">
                        <PackageCheck /> Online, 2 h
                      </Badge>
                    )}
                    <Money cents={l.qty * l.unit_cents} className="w-[72px] text-right font-heading text-[16px] font-semibold" />
                  </div>
                );
              })}
            </Panel>
          </section>

          {draft.advice.length > 0 && (
            <section>
              <SectionTitle>Picky&apos;s advice</SectionTitle>
              <div className="grid gap-3 md:grid-cols-2">
                {draft.advice.map((a) => {
                  const skip = a.kind === "skip";
                  return (
                    <div key={a.sku + a.kind} className={cn("rounded-2xl p-4", skip ? "bg-tomato-wash" : "bg-amber-wash")}>
                      <div className="flex items-center justify-between gap-2">
                        <span className={cn("flex items-center gap-1.5 font-semibold", skip ? "text-tomato" : "text-amber-ink")}>
                          {skip ? <Ban className="size-4" aria-hidden /> : <Clock className="size-4" aria-hidden />}
                          {skip ? "Skip it" : "Worth waiting"}
                        </span>
                        {a.save_cents ? <span className="rounded-full bg-white/70 px-2.5 py-0.5 text-[12.5px] font-semibold">Saves {fmt(a.save_cents)}</span> : null}
                      </div>
                      <p className="mt-2 text-[14.5px] leading-snug">{a.text}</p>
                    </div>
                  );
                })}
              </div>
            </section>
          )}

          <section>
            <SectionTitle>Worth a stop on the way?</SectionTitle>
            {draft.recs.length === 0 ? (
              <Panel className="p-5 text-muted-foreground">Nothing worth a stop today, so the robot goes straight down your list.</Panel>
            ) : (
              <div className="grid gap-3 md:grid-cols-3">
                {draft.recs.map((r) => {
                  const p = productBySku[r.sku];
                  const on = accepted.has(r.rec_id);
                  const d = detours[r.rec_id] ?? 0;
                  const overBy = totals.total + r.unit_cents - draft.budget_cents;
                  return (
                    <motion.div key={r.rec_id} layout whileHover={{ y: -3 }} className={cn("flex flex-col rounded-2xl bg-card p-4 ring-1 ring-foreground/[0.07] transition-shadow", on && "shadow-md ring-2 ring-leaf")}>
                      <div className="flex items-start gap-3">
                        <ProductTile color={p.label_color} category={p.category} name={p.name} size={44} />
                        <div className="min-w-0 flex-1">
                          <div className="leading-snug font-semibold">{p.name}</div>
                          <div className="mt-0.5 flex items-baseline gap-1.5">
                            <Money cents={r.unit_cents} className="font-semibold" />
                            {r.original_cents && <Money cents={r.original_cents} className="text-[13px] text-muted-foreground line-through" />}
                          </div>
                        </div>
                      </div>
                      <p className="mt-3 flex-1 text-[14px] leading-snug text-muted-foreground">{r.reason}</p>
                      {!on && overBy > 0 && <p className="mt-2 text-[13px] font-medium text-tomato">Would put you {fmt(overBy)} over budget.</p>}
                      <div className="mt-3 flex items-center justify-between gap-2 border-t border-border pt-3">
                        <label className="flex cursor-pointer items-center gap-2.5 text-[14px] font-semibold">
                          <Switch checked={on} onCheckedChange={() => toggle(r.rec_id)} aria-label={`Stop for ${p.name}`} />
                          {on ? "Stopping here" : "Skip"}
                        </label>
                        <div className="flex items-center gap-1 text-[12.5px] whitespace-nowrap text-muted-foreground">
                          {d < 0.5 ? "On your route" : `+${d.toFixed(0)} m walk`}
                          <Tooltip>
                            <TooltipTrigger asChild>
                              <button className="cursor-pointer rounded-full p-1 hover:bg-muted" aria-label="Why this suggestion">
                                <Info className="size-3.5" />
                              </button>
                            </TooltipTrigger>
                            <TooltipContent className="max-w-[240px]">
                              Not sponsored. Based on {Object.keys(r.signals).map((k) => k.replace(/_/g, " ")).join(", ")}.
                            </TooltipContent>
                          </Tooltip>
                        </div>
                      </div>
                    </motion.div>
                  );
                })}
              </div>
            )}
          </section>
        </div>

        <aside className="lg:col-span-4">
          <div className="flex flex-col gap-4 lg:sticky lg:top-24">
            <Panel className="p-4">
              <h2 className="mb-3 text-[17px] font-semibold">How should it shop?</h2>
              <div className="flex flex-col gap-2" role="radiogroup" aria-label="Fulfillment">
                {(
                  [
                    ["with_me", UserRound, "Shop with me", "Meet it at the entrance. You say yes or no as it goes."],
                    ["pickup", PackageCheck, "Shop for me, I'll pick up", "It shops alone and texts before any extra. About 15 minutes."],
                  ] as const
                ).map(([id, Icon, t, d]) => {
                  const on = mode === id;
                  return (
                    <button
                      key={id}
                      role="radio"
                      aria-checked={on}
                      onClick={() => setMode(id)}
                      className={cn("flex cursor-pointer gap-3 rounded-xl p-3 text-left transition-colors", on ? "bg-leaf-wash ring-2 ring-leaf" : "bg-muted hover:bg-secondary")}
                    >
                      <Icon className={cn("mt-0.5 size-5 shrink-0", on ? "text-leaf" : "text-muted-foreground")} aria-hidden />
                      <span>
                        <span className="block font-semibold">{t}</span>
                        <span className="block text-[13px] leading-snug text-muted-foreground">{d}</span>
                      </span>
                    </button>
                  );
                })}
              </div>
            </Panel>

            <Panel className="p-4">
              <dl className="flex flex-col gap-1.5 text-[14.5px]">
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Your list</dt>
                  <dd><Money cents={totals.list} /></dd>
                </div>
                <div className="flex justify-between">
                  <dt className="text-muted-foreground">Extra stops ({accepted.size})</dt>
                  <dd><Money cents={totals.recs} /></dd>
                </div>
                {totals.onlineSub > 0 && (
                  <div className="flex justify-between">
                    <dt className="text-muted-foreground">Online delivery</dt>
                    <dd>{totals.shipping ? <Money cents={totals.shipping} /> : "Free with the robot order"}</dd>
                  </div>
                )}
              </dl>
              <div className="mt-3 flex items-baseline justify-between border-t border-border pt-3">
                <span className="font-semibold">About</span>
                <CountUp cents={totals.total} className={cn("font-heading text-[30px] font-bold", over && "text-tomato")} />
              </div>
              <Meter value={totals.total} max={draft.budget_cents} className="mt-3" />
              <p className={cn("mt-2 text-[13.5px] font-medium", over ? "text-tomato" : "text-leaf")}>
                {over ? `${fmt(totals.total - draft.budget_cents)} over your ${fmt(draft.budget_cents)} budget` : `${fmt(draft.budget_cents - totals.total)} under your ${fmt(draft.budget_cents)} budget`}
              </p>
            </Panel>

            <Panel className="p-4">
              <h2 className="text-[15px] font-semibold">Your rules, checked</h2>
              <ul className="mt-2 flex flex-col gap-1.5 text-[13.5px]">
                {verdict.reasons.map((r) => (
                  <li key={r.code + r.text} className="flex gap-2">
                    {r.level === "ok" ? (
                      <Check className="mt-0.5 size-4 shrink-0 text-leaf" aria-hidden />
                    ) : r.level === "ask" ? (
                      <Clock className="mt-0.5 size-4 shrink-0 text-amber-ink" aria-hidden />
                    ) : (
                      <Ban className="mt-0.5 size-4 shrink-0 text-tomato" aria-hidden />
                    )}
                    <span>{r.text}</span>
                  </li>
                ))}
              </ul>
            </Panel>

            <Button
              size="lg"
              className="h-14 text-[16px]"
              disabled={verdict.verdict === "deny" || thinking || sending}
              onClick={send}
            >
              <Bot className="size-5" /> {sending ? "Sending…" : `Send the robot, about ${fmt(totals.total)}`}
            </Button>
            <p className="text-center text-[13px] text-muted-foreground">Nothing is charged until you approve checkout.</p>
          </div>
        </aside>
      </div>
    </div>
  );
}
