"use client";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { Badge, Button, Card, CardHead, Meter, Money, PageHead, Swatch, cx } from "@/components/ui";
import { STORES, maya, productBySku, slotsOf, type StoreId } from "@/lib/data";
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
  const placeOrder = useTrip((s) => s.placeOrder);
  const router = useRouter();

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

  return (
    <>
      <PageHead kicker="Robot order" title="What should the robot shop for?">
        <div className="flex border border-ink" role="tablist" aria-label="Store">
          {(Object.keys(STORES) as StoreId[]).map((id) => (
            <button
              key={id}
              role="tab"
              aria-selected={storeId === id}
              onClick={() => {
                setStoreId(id);
                submit(PRESETS[id][0].text, id);
              }}
              className={cx("px-3.5 py-2 text-left text-[13px] leading-tight", storeId === id ? "bg-ink text-paper" : "bg-card hover:bg-paper", id !== "store_ave" && "border-l border-ink")}
            >
              <span className="block font-semibold">{STORES[id].short}</span>
              <span className={cx("block text-[11.5px]", storeId === id ? "text-paper/70" : "text-muted")}>{STORES[id].kind === "grocery" ? "groceries · The Ave" : "phones & accessories"}</span>
            </button>
          ))}
        </div>
      </PageHead>

      <form
        className="mb-8 flex flex-col gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          submit(text);
        }}
      >
        <div className="flex border border-ink bg-card focus-within:outline-2 focus-within:outline-tomato">
          <input
            value={text}
            onChange={(e) => setText(e.target.value)}
            className="min-w-0 flex-1 bg-transparent px-4 py-3.5 font-display text-[22px] tracking-tight outline-none placeholder:text-muted"
            placeholder={PRESETS[storeId][0].text}
            aria-label="What do you need?"
          />
          <button type="submit" className="border-l border-ink bg-ink px-5 text-[14px] font-medium text-paper hover:bg-night">
            {thinking ? "Planning…" : "Plan it"}
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-[13px] text-muted">
          Try:
          {PRESETS[storeId].map((p) => (
            <button key={p.id} type="button" onClick={() => submit(p.text)} className="border border-line px-2 py-0.5 text-ink-2 hover:border-ink hover:text-ink">
              {p.text}
            </button>
          ))}
          <span className="ml-auto">You can also text this to Picky on iMessage.</span>
        </div>
      </form>

      <div className={cx("grid grid-cols-12 gap-6 transition-opacity", thinking && "opacity-40")}>
        <div className="col-span-12 flex flex-col gap-6 lg:col-span-8">
          <Card>
            <CardHead title={draft.title} meta={`${draft.lines.length} items · for ${maya.profile.display_name}`} />
            <table className="w-full text-[14px]">
              <tbody>
                {draft.lines.map((l) => {
                  const p = productBySku[l.sku];
                  return (
                    <tr key={l.line_id} className="border-b border-line last:border-0">
                      <td className="num w-12 py-2.5 pl-4 text-ink-2">{l.qty}×</td>
                      <td className="py-2.5">
                        <div className="flex items-center gap-2">
                          <Swatch color={p.label_color} />
                          <span className="font-medium">{p.name}</span>
                          {l.vegetarian && <Badge tone="basil">veg</Badge>}
                        </div>
                        {l.note && <div className="pl-[18px] text-[12.5px] text-muted">{l.note}</div>}
                      </td>
                      <td className="w-[110px]">
                        {l.sourcing === "in_store" ? <Badge tone="basil">in store</Badge> : <Badge tone="plum">online · 2 h</Badge>}
                      </td>
                      <td className="w-[90px] pr-4 text-right">
                        <Money cents={l.qty * l.unit_cents} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>

          {draft.advice.length > 0 && (
            <section>
              <h2 className="mb-2 font-display text-[19px] font-semibold">Picky says</h2>
              <div className="grid gap-3 md:grid-cols-2">
                {draft.advice.map((a) => (
                  <Card key={a.sku + a.kind} variant="inset" accent={a.kind === "skip" ? "tomato" : "turmeric"} className="p-4">
                    <div className="flex items-center justify-between">
                      <span className={cx("font-mono text-[11px] font-medium tracking-wider uppercase", a.kind === "skip" ? "text-tomato-deep" : "text-[#7a5408]")}>{a.kind === "skip" ? "✕ skip" : "◷ wait"}</span>
                      {a.save_cents ? <span className="num text-[12.5px] text-basil">saves {fmt(a.save_cents)}</span> : null}
                    </div>
                    <p className="mt-1.5 text-[14px] leading-snug text-ink-2">{a.text}</p>
                  </Card>
                ))}
              </div>
            </section>
          )}

          <section>
            <div className="mb-2 flex items-baseline justify-between">
              <h2 className="font-display text-[19px] font-semibold">Worth a detour?</h2>
              <span className="text-[13px] text-muted">The robot stops at each one and asks before adding it.</span>
            </div>
            {draft.recs.length === 0 && (
              <Card variant="flat" className="p-4 text-[14px] text-ink-2">
                Nothing worth a stop today. The robot goes straight to your list.
              </Card>
            )}
            <div className="grid gap-3 md:grid-cols-3">
              {draft.recs.map((r) => {
                const p = productBySku[r.sku];
                const on = accepted.has(r.rec_id);
                const d = detours[r.rec_id] ?? 0;
                return (
                  <Card key={r.rec_id} variant="inset" accent={on ? "turmeric" : "tomato"} className={cx("flex flex-col p-4", !on && "bg-paper")}>
                    <div className="flex items-start justify-between gap-2">
                      <div className="font-medium leading-snug">{p.name}</div>
                      <div className="text-right">
                        <Money cents={r.unit_cents} className="font-medium" />
                        {r.original_cents && (
                          <div className="num text-[12px] text-muted line-through">{fmt(r.original_cents)}</div>
                        )}
                      </div>
                    </div>
                    <p className="mt-2 text-[13.5px] leading-snug text-ink-2">{r.reason}</p>
                    <div className="mt-3 flex flex-wrap gap-1">
                      {Object.entries(r.signals).map(([k, v]) => (
                        <span key={k} className="border border-line bg-paper px-1.5 font-mono text-[10.5px] text-muted">
                          {k}={String(v)}
                        </span>
                      ))}
                    </div>
                    {!on && totals.total + r.unit_cents > draft.budget_cents && (
                      <p className="mt-2 text-[12.5px] text-tomato-deep">Left off: adds {fmt(r.unit_cents)} and puts you {fmt(totals.total + r.unit_cents - draft.budget_cents)} over budget.</p>
                    )}
                    <div className="mt-3 flex items-center justify-between border-t border-line pt-3 text-[12.5px]">
                      <span className="num text-ink-2">{d < 0.5 ? "on your route" : `+${d.toFixed(1)} m detour`}</span>
                      <span className="font-mono text-[10.5px] text-muted">{r.sponsored ? "SPONSORED" : "NOT SPONSORED"}</span>
                    </div>
                    <Button
                      variant={on ? "ghost" : "secondary"}
                      className="mt-3"
                      onClick={() =>
                        setAccepted((s) => {
                          const n = new Set(s);
                          if (n.has(r.rec_id)) n.delete(r.rec_id);
                          else n.add(r.rec_id);
                          return n;
                        })
                      }
                    >
                      {on ? "Skip this stop" : "Keep this stop"}
                    </Button>
                  </Card>
                );
              })}
            </div>
          </section>
        </div>

        <aside className="col-span-12 lg:col-span-4">
          <div className="sticky top-6 flex flex-col gap-4">
            <Card>
              <CardHead title="How do you want it?" />
              <div className="flex flex-col gap-2 p-3">
                {(
                  [
                    ["with_me", "Shop with the robot", "Meet it at the entrance. It drives the route and you approve as you go."],
                    ["pickup", "Robot shops, I pick up", "It shops alone and texts you before every extra. Ready in ~15 min."],
                  ] as const
                ).map(([id, t, d]) => (
                  <label key={id} className={cx("flex cursor-pointer gap-3 border p-3", mode === id ? "border-ink bg-paper" : "border-line hover:border-line-strong")}>
                    <input type="radio" name="mode" checked={mode === id} onChange={() => setMode(id)} className="mt-1 accent-[#c4472b]" />
                    <span>
                      <span className="block text-[14px] font-semibold">{t}</span>
                      <span className="block text-[12.5px] leading-snug text-muted">{d}</span>
                    </span>
                  </label>
                ))}
              </div>
            </Card>

            <Card className="p-4">
              <dl className="flex flex-col gap-1.5 text-[14px]">
                <Row k="List" v={totals.list} />
                <Row k={`Extras (${accepted.size})`} v={totals.recs} />
                {totals.onlineSub > 0 && <Row k="Online add-on delivery" v={totals.shipping} note={totals.shipping ? "" : "free with robot order"} />}
                <div className="mt-1 flex items-baseline justify-between border-t border-ink pt-2">
                  <dt className="font-semibold">Estimated total</dt>
                  <dd className={cx("num text-[22px] font-medium", over && "text-tomato")}>{fmt(totals.total)}</dd>
                </div>
              </dl>
              <Meter value={totals.total} max={draft.budget_cents} className="mt-3" />
              <div className="mt-1.5 flex justify-between text-[12.5px] text-muted">
                <span>budget {fmt(draft.budget_cents)}</span>
                <span className={over ? "text-tomato" : "text-basil"}>
                  {over ? `${fmt(totals.total - draft.budget_cents)} over` : `${fmt(draft.budget_cents - totals.total)} left`}
                </span>
              </div>
            </Card>

            <Card variant="inset" accent={verdict.verdict === "deny" ? "tomato" : "basil"} className="p-4">
              <div className="label-caps mb-2">Your rules · checked in code, not by the AI</div>
              <ul className="flex flex-col gap-1.5 text-[13px]">
                {verdict.reasons.map((r) => (
                  <li key={r.code + r.text} className="flex gap-2">
                    <span className={cx("font-mono", r.level === "ok" ? "text-basil" : r.level === "ask" ? "text-turmeric" : "text-tomato")}>
                      {r.level === "ok" ? "✓" : r.level === "ask" ? "?" : "✕"}
                    </span>
                    <span className="text-ink-2">{r.text}</span>
                  </li>
                ))}
              </ul>
            </Card>

            <Button
              variant="primary"
              className="py-3 text-[15px]"
              disabled={verdict.verdict === "deny" || thinking}
              onClick={() => {
                placeOrder(draft, [...accepted], mode);
                router.push("/trip");
              }}
            >
              Send to robot · {fmt(totals.total)}
            </Button>
            <p className="text-center text-[12px] text-muted">Nothing is charged until you approve checkout.</p>
          </div>
        </aside>
      </div>
    </>
  );
}

function Row({ k, v, note }: { k: string; v: number; note?: string }) {
  return (
    <div className="flex justify-between">
      <dt className="text-ink-2">{k}</dt>
      <dd className="num">{note || fmt(v)}</dd>
    </div>
  );
}
