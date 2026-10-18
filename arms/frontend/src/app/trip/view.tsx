"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { LiveFeed } from "@/components/LiveFeed";
import { MapLegend, StoreMap } from "@/components/StoreMap";
import { Badge, Button, Card, CardHead, Meter, Money, PageHead, Swatch, cx } from "@/components/ui";
import { STORES, productBySku } from "@/lib/data";
import { fmt } from "@/lib/money";
import { cartTotal, useTrip, type Approval } from "@/lib/trip";

export default function TripPage() {
  const t = useTrip();
  const router = useRouter();

  if (t.phase === "idle" || !t.draft || !t.route) {
    return (
      <>
        <PageHead kicker="Live trip" title="No robot out right now" />
        <Card variant="flat" className="max-w-[560px] p-6">
          <p className="text-ink-2">
            Start an order and the robot&apos;s camera, route and basket show up here while it shops. Try{" "}
            <em>&ldquo;tacos for 6, under $40, one vegetarian&rdquo;</em>.
          </p>
          <Link href="/order">
            <Button variant="primary" className="mt-4">
              Start an order
            </Button>
          </Link>
        </Card>
      </>
    );
  }

  const listStops = t.route.order.filter((id) => t.stops[id]?.kind === "list" || t.stops[id]?.kind === "optional");
  const done = listStops.filter((id) => t.stopStatus[id] === "done" || t.stopStatus[id] === "declined").length;
  const extra = t.route.total_m - t.baseline_m;
  const total = cartTotal(t.cart);

  return (
    <>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4 border-b border-ink pb-4">
        <div>
          <div className="label-caps mb-1">
            Live trip · {t.mode === "pickup" ? "robot shopping for pickup" : "shopping with Maya"} · {STORES[t.storeId].name}
          </div>
          <h1 className="font-display text-[34px] leading-[1.05] font-semibold tracking-[-0.02em]">{t.draft.title}</h1>
        </div>
        <div className="flex items-center gap-5 text-right">
          <Stat k="Stops" v={`${done}/${listStops.length}`} />
          <Stat k="Route" v={`${t.route.total_m.toFixed(0)} m`} sub={extra > 0.5 ? `+${extra.toFixed(1)} m for detours` : "no detours"} />
          <Stat k="Basket" v={fmt(total)} sub={`budget ${fmt(t.draft.budget_cents)}`} />
        </div>
      </div>

      <Controls onRestart={() => (t.reset(), router.push("/order"))} />

      <div className="grid grid-cols-12 gap-5">
        {/* left: feed + timeline */}
        <div className="col-span-12 flex flex-col gap-5 xl:col-span-8">
          {t.phase === "waiting_login" ? <LoginPanel /> : <LiveFeed tripId={null} />}
          <ApprovalPanel />
          <StopTimeline />
        </div>

        {/* right: map + cart + log */}
        <div className="col-span-12 flex flex-col gap-5 xl:col-span-4">
          <Card>
            <CardHead title="Route" meta={`${listStops.length} stops`} />
            <div className="px-3 pt-3">
              <div className="mx-auto max-h-[520px]" style={{ aspectRatio: `${STORES[t.storeId].bounds.w + 0.8} / ${STORES[t.storeId].bounds.h + 0.8}` }}>
                <StoreMap />
              </div>
            </div>
            <div className="border-t border-line px-3 py-2">
              <MapLegend />
            </div>
          </Card>
          <CartPanel />
          <EventLog />
        </div>
      </div>
    </>
  );
}

function Stat({ k, v, sub }: { k: string; v: string; sub?: string }) {
  return (
    <div>
      <div className="label-caps">{k}</div>
      <div className="num text-[22px] leading-tight font-medium">{v}</div>
      {sub && <div className="text-[11.5px] text-muted">{sub}</div>}
    </div>
  );
}

function Controls({ onRestart }: { onRestart: () => void }) {
  const { speed, setSpeed, paused, togglePause, autoAnswer, setAutoAnswer, phase } = useTrip();
  return (
    <div className="mb-5 flex flex-wrap items-center gap-2 text-[13px]">
      <span className="label-caps mr-1">Demo playback</span>
      <div className="flex border border-line">
        {[1, 2, 4].map((n) => (
          <button key={n} onClick={() => setSpeed(n)} className={cx("num px-2.5 py-1", speed === n ? "bg-ink text-paper" : "hover:bg-card", n > 1 && "border-l border-line")}>
            {n}×
          </button>
        ))}
      </div>
      <Button variant="ghost" className="py-1" onClick={togglePause} disabled={phase === "done"}>
        {paused ? "Resume" : "Pause"}
      </Button>
      <label className="flex cursor-pointer items-center gap-1.5 px-2 text-ink-2">
        <input type="checkbox" checked={autoAnswer} onChange={(e) => setAutoAnswer(e.target.checked)} className="accent-[#c4472b]" />
        auto-answer approvals
      </label>
      <Button variant="quiet" onClick={onRestart} className="ml-auto">
        New order
      </Button>
    </div>
  );
}

function LoginPanel() {
  const { loginCode, robotLogin } = useTrip();
  return (
    <div className="grid border border-ink bg-ink text-paper md:grid-cols-[1fr_auto]">
      <div className="p-7">
        <div className="font-mono text-[11px] tracking-wider text-tomato uppercase">Step 1 of 1</div>
        <h2 className="mt-2 font-display text-[28px] leading-tight font-semibold">Meet the robot at the entrance and type this code.</h2>
        <p className="mt-2 max-w-[48ch] text-paper/70">
          It loads your list, budget and rules, then starts the route. Text <span className="num">&ldquo;here&rdquo;</span> to Picky if you&apos;d rather log in by
          iMessage.
        </p>
        <Button variant="primary" className="mt-6" onClick={robotLogin}>
          Simulate robot login
        </Button>
      </div>
      <div className="flex flex-col items-center justify-center border-white/15 px-10 py-8 md:border-l">
        <div className="label-caps text-paper/60!">Login code</div>
        <div className="num mt-2 text-[56px] leading-none font-medium tracking-[0.06em]">{loginCode}</div>
        <div className="mt-2 font-mono text-[11px] text-paper/50">expires in 10 min</div>
      </div>
    </div>
  );
}

function ApprovalPanel() {
  const { approval, answer, receipt, phase, mode } = useTrip();
  if (receipt) return <ReceiptPanel />;
  if (!approval) {
    if (phase === "done")
      return (
        <Card variant="inset" accent="tomato" className="p-4 text-[14px]">
          Checkout declined. Your basket is held at the pickup counter for 30 minutes.
        </Card>
      );
    return null;
  }
  return (
    <Card variant="inset" accent="turmeric" className="border-ink p-5">
      <div className="mb-1 flex items-center gap-2">
        <span className="live-dot size-2 rounded-full bg-turmeric" />
        <span className="label-caps">{mode === "pickup" ? "Sent to your phone · also answerable here" : "The robot is asking you"}</span>
      </div>
      <ApprovalBody a={approval} answer={answer} />
    </Card>
  );
}

function ApprovalBody({ a, answer }: { a: Approval; answer: (v: string) => void }) {
  if (a.kind === "detour") {
    const p = productBySku[a.sku];
    return (
      <div className="grid gap-4 md:grid-cols-[1fr_auto] md:items-end">
        <div>
          <h3 className="font-display text-[22px] font-semibold">
            Add {p.name}? <Money cents={a.unit_cents} className="ml-1 text-[18px] font-normal text-ink-2" />
          </h3>
          <p className="mt-1 max-w-[60ch] text-[14px] text-ink-2">{a.reason}</p>
          <p className="mt-1 font-mono text-[10.5px] text-muted">NOT SPONSORED · reason from recommender signals</p>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={() => answer("no")}>
            No thanks
          </Button>
          <Button variant="primary" onClick={() => answer("yes")}>
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
        <h3 className="font-display text-[22px] font-semibold">Which {a.sku === "AVOCADO" ? "avocados" : p.name}?</h3>
        <p className="mt-1 text-[14px] text-ink-2">The camera ranked what&apos;s on the table by freshness. Here are the top 3 lots.</p>
        <div className="mt-3 grid gap-2 sm:grid-cols-3">
          {a.candidates.map((c, i) => (
            <button key={c.instance_id} onClick={() => answer(c.instance_id)} className="border border-line bg-paper p-3 text-left hover:border-ink">
              <div className="flex items-center justify-between">
                <span className="num text-[12px] text-muted">#{i + 1}</span>
                <span className="num text-[13px] font-medium">{Math.round(c.freshness * 100)}% fresh</span>
              </div>
              <div className="mt-2 h-1.5 bg-sunk">
                <div className="h-full bg-basil" style={{ width: `${c.freshness * 100}%` }} />
              </div>
              <div className="mt-2 text-[14px] font-medium">{c.note}</div>
              <div className="num text-[11.5px] text-muted">
                {c.lot_id} · best by {c.expiry?.slice(5)}
              </div>
            </button>
          ))}
        </div>
      </div>
    );
  }
  const ask = a.reasons.filter((r) => r.level !== "ok");
  return (
    <div className="grid gap-4 md:grid-cols-[1fr_auto] md:items-end">
      <div>
        <h3 className="font-display text-[22px] font-semibold">
          Approve checkout for <Money cents={a.total_cents} />?
        </h3>
        <ul className="mt-2 flex flex-col gap-1 text-[13.5px]">
          {a.reasons.map((r) => (
            <li key={r.code + r.text} className="flex gap-2">
              <span className={cx("font-mono", r.level === "ok" ? "text-basil" : "text-turmeric")}>{r.level === "ok" ? "✓" : "?"}</span>
              <span className="text-ink-2">{r.text}</span>
              <span className="font-mono text-[10.5px] text-muted">{r.code}</span>
            </li>
          ))}
        </ul>
        <p className="mt-2 font-mono text-[10.5px] text-muted">
          MANDATE ENGINE: {ask.length ? "NEEDS_APPROVAL" : "APPROVE"} · cart hash {a.cart_hash} · Visa •• 4242 · TEST MODE
        </p>
      </div>
      <div className="flex gap-2">
        <Button variant="ghost" onClick={() => answer("decline")}>
          Not yet
        </Button>
        <Button variant="primary" onClick={() => answer("approve")}>
          Approve & pay
        </Button>
      </div>
    </div>
  );
}

function ReceiptPanel() {
  const { receipt, cart, mode } = useTrip();
  if (!receipt) return null;
  return (
    <Card variant="inset" accent="basil" className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="label-caps">Paid · {receipt.provider}</div>
          <h3 className="mt-1 font-display text-[26px] font-semibold">
            <Money cents={receipt.total_cents} /> on {receipt.card}
          </h3>
          <p className="mt-1 text-[14px] text-ink-2">
            {cart.filter((c) => c.source === "in_store").length} items {mode === "pickup" ? "waiting at the pickup counter" : "in your basket"} ·{" "}
            {cart.filter((c) => c.source === "online").length} arriving from Picky Online in ~2 h. Tomorrow Picky will ask how the salsa was.
          </p>
        </div>
        <div className="flex flex-col items-end gap-2">
          <Badge tone="turmeric">TEST MODE · NO REAL CHARGE</Badge>
          <span className="num text-[11.5px] text-muted">{receipt.receipt_id}</span>
          <Link href="/purchases" className="text-[13.5px] font-medium text-tomato hover:underline">
            See it in Purchases →
          </Link>
        </div>
      </div>
    </Card>
  );
}

function StopTimeline() {
  const { route, stops, stopStatus, activeStop, robot } = useTrip();
  if (!route) return null;
  const ids = route.order.filter((id) => stops[id]?.kind !== "start");
  return (
    <Card>
      <CardHead title="Stops in route order" meta="optimized from planogram" />
      <ol className="grid sm:grid-cols-2">
        {ids.map((id, i) => {
          const s = stops[id];
          const st = stopStatus[id];
          const active = activeStop === id && st === "active";
          const p = s.sku ? productBySku[s.sku] : null;
          return (
            <li key={id} className={cx("flex items-center gap-3 border-b border-line px-4 py-2 text-[13.5px] sm:odd:border-r", active && "bg-tomato-wash")}>
              <span className={cx("num w-5 text-right text-[12px]", st === "done" ? "text-basil" : "text-muted")}>{s.kind === "end" ? "■" : i + 1}</span>
              {p && <Swatch color={p.label_color} />}
              <span className={cx("min-w-0 flex-1 truncate", st === "declined" && "text-muted line-through")}>
                {s.qty && s.qty > 1 ? `${s.qty}× ` : ""}
                {s.label}
              </span>
              {s.kind === "optional" && <Badge tone="turmeric">detour</Badge>}
              <span className="w-[70px] text-right font-mono text-[10.5px] uppercase">
                {st === "done" ? (
                  <span className="text-basil">{s.kind === "end" ? "✓ paid" : "✓ picked"}</span>
                ) : st === "declined" ? (
                  <span className="text-muted">skipped</span>
                ) : active ? (
                  <span className="text-tomato">{robot.status}</span>
                ) : (
                  <span className="text-muted">·</span>
                )}
              </span>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

function CartPanel() {
  const { cart, draft } = useTrip();
  const total = cartTotal(cart);
  const budget = draft?.budget_cents ?? 4000;
  return (
    <Card>
      <CardHead title="Basket" meta={`${cart.reduce((s, c) => s + c.qty, 0)} items`} />
      <ul className="max-h-[300px] divide-y divide-line overflow-y-auto">
        {cart.length === 0 && <li className="px-4 py-3 text-[13px] text-muted">Empty. The first pick lands here.</li>}
        {cart.map((c) => {
          const p = productBySku[c.sku];
          return (
            <li key={c.sku + c.source} className="flex items-center gap-2 px-4 py-2 text-[13.5px]">
              <span className="num w-6 text-muted">{c.qty}×</span>
              <span className="min-w-0 flex-1 truncate">{p.name}</span>
              {c.source === "online" ? (
                <Badge tone="plum">online</Badge>
              ) : c.verified ? (
                <span className="font-mono text-[10.5px] text-basil" title="Verified by basket camera">
                  ✓ cam
                </span>
              ) : (
                <span className="font-mono text-[10.5px] text-muted">…</span>
              )}
              <Money cents={c.qty * c.unit_cents} className="w-[64px] text-right" />
            </li>
          );
        })}
      </ul>
      <div className="border-t border-ink px-4 py-3">
        <div className="flex items-baseline justify-between">
          <span className="font-semibold">Total</span>
          <Money cents={total} className={cx("text-[20px] font-medium", total > budget && "text-tomato")} />
        </div>
        <Meter value={total} max={budget} className="mt-2" />
        <div className="mt-1 text-right text-[12px] text-muted">
          {total <= budget ? `${fmt(budget - total)} left of ${fmt(budget)}` : `${fmt(total - budget)} over budget`}
        </div>
      </div>
    </Card>
  );
}

function EventLog() {
  const log = useTrip((s) => s.log);
  return (
    <Card variant="flat">
      <details>
        <summary className="cursor-pointer px-4 py-2.5 text-[13.5px] font-medium">
          Event stream <span className="font-mono text-[11px] text-muted">· {log.length} events (contracts.events names)</span>
        </summary>
        <ol className="max-h-[260px] overflow-y-auto border-t border-line px-4 py-2 font-mono text-[11px] leading-[1.7]">
          {[...log].reverse().map((e) => (
            <li key={e.seq} className="flex gap-2">
              <span className="w-6 shrink-0 text-right text-muted">{e.seq}</span>
              <span className="w-[118px] shrink-0 text-tomato-deep">{e.type}</span>
              <span className="text-ink-2">{e.summary}</span>
            </li>
          ))}
        </ol>
      </details>
    </Card>
  );
}
