"use client";
// Trip store + demo replay engine.
// Events use contracts.events names (route.planned, robot.state, item.picked, approval.requested, ...), so the
// GatewaySource (lib/gateway/tripSource.ts) feeds the same store from a real gateway trip (`source: "gateway"`).
// In demo mode the engine below produces them client-side and pauses on every approval until the shopper answers.
import { create } from "zustand";
import { STORES, maya, online, productBySku, slotsOf, type Pose, type Purchase, type StoreId } from "./data";
import { answerGateway, startGateway, stopGateway, type FollowOptions } from "./gateway/tripSource";
import { draftTotal, mandateCheck, type Draft } from "./order";
import type { PlannedRoute } from "./routing";
import { buildTripPlan } from "./trip-plan";

export type Mode = "with_me" | "pickup";
export type StopKind = "start" | "list" | "optional" | "end";
export type StopStatus = "pending" | "active" | "done" | "declined" | "skipped";
export type Stop = {
  id: string;
  kind: StopKind;
  label: string;
  pose: Pose;
  sku?: string;
  qty?: number;
  slot_id?: string;
  rec_id?: string;
  reason?: string;
  unit_cents?: number;
};
export type CartLine = {
  sku: string;
  qty: number;
  unit_cents: number;
  source: "in_store" | "online";
  verified: boolean | null;
  lot?: string;
  /** gateway cart line id (gateway trips only) */
  line_id?: string;
};
export type Approval =
  | { id: string; kind: "detour"; stop_id: string; sku: string; reason: string; unit_cents: number; sponsored?: boolean; note?: string }
  | {
      id: string;
      kind: "pick";
      stop_id: string;
      sku: string;
      candidates: { instance_id: string; lot_id: string; freshness: number; expiry: string | null; note: string; thumb?: string }[];
      /** instance the camera ranked first (gateway); the replay recommends the 2nd candidate */
      recommended?: string;
    }
  | {
      id: string;
      kind: "checkout";
      total_cents: number;
      cart_hash: string;
      reasons: { code: string; text: string; level: "ok" | "ask" | "block" }[];
    }
  /** gateway: over_threshold / substitution / online_order / question / multi-item recommendation */
  | {
      id: string;
      kind: "question";
      title: string;
      body: string;
      sku?: string;
      amount_cents?: number;
      options: { id: string; label: string; detail?: string }[];
      multi: boolean;
    };
export type LogEvent = { seq: number; t: number; type: string; summary: string };
export type RobotStatus = "idle" | "driving" | "scanning" | "picking" | "waiting" | "done";
export type Receipt = {
  receipt_id: string;
  total_cents: number;
  in_store_cents: number;
  online_cents: number;
  shipping_cents: number;
  card: string;
  provider: string;
  ts: string;
};

export type Phase = "idle" | "waiting_login" | "shopping" | "checkout" | "done";
/** One row of the robot brain's thinking log (gateway brain.step). */
export type ThinkStep = { id: string; no: number; kind: string; tool: string | null; text: string; ok: boolean; t: number; camera: string | null };

export type TripStore = {
  storeId: StoreId;
  phase: Phase;
  mode: Mode;
  draft: Draft | null;
  accepted: string[];
  loginCode: string;
  stops: Record<string, Stop>;
  route: PlannedRoute | null;
  baseline_m: number;
  baselinePath: Pose[];
  /** Optional stops that actually cost extra walking (drawn as dashed detours). */
  offRoute: string[];
  stopStatus: Record<string, StopStatus>;
  robot: Pose & { status: RobotStatus; speed: number };
  activeStop: string | null;
  cart: CartLine[];
  approval: Approval | null;
  approvalsLog: { id: string; kind: string; label: string; answer: string; t: number }[];
  log: LogEvent[];
  receipt: Receipt | null;
  extraPurchases: Purchase[];
  speed: number;
  autoAnswer: boolean;
  paused: boolean;
  startedAt: number | null;
  // gateway trips (lib/gateway/tripSource.ts); demo trips keep the defaults
  source: "demo" | "gateway";
  tripId: string | null;
  requestId: string | null;
  goalId: string | null;
  robotId: string | null;
  robotName: string | null;
  /** raw contract TripState */
  tripState: string | null;
  error: string | null;
  /** online delivery fee charged by the gateway (Cart.shipping_cents when there are online lines) */
  shippingCents: number;
  /** approval queue; `approval` stays pending[0] */
  pending: Approval[];
  steps: ThinkStep[];
  followup: { text: string; skus: string[]; quick: string[] } | null;
  /** bumped when gateway products are registered at runtime (forces a re-render) */
  productsVersion: number;
  // actions
  placeOrder: (draft: Draft, accepted: string[], mode: Mode) => void;
  followGateway: (o: FollowOptions) => void;
  robotLogin: () => void;
  answer: (value: string) => void;
  setSpeed: (n: number) => void;
  setAutoAnswer: (b: boolean) => void;
  togglePause: () => void;
  reset: () => void;
};

let seq = 0;
let runToken = 0;
let resolveApproval: ((v: string) => void) | null = null;

const initial = {
  storeId: "store_ave" as StoreId,
  phase: "idle" as Phase,
  mode: "with_me" as Mode,
  draft: null,
  accepted: [],
  loginCode: "",
  stops: {},
  route: null,
  baseline_m: 0,
  baselinePath: [],
  offRoute: [] as string[],
  stopStatus: {},
  robot: { ...STORES.store_ave.robot_home, status: "idle" as RobotStatus, speed: 0 },
  activeStop: null,
  cart: [],
  approval: null,
  approvalsLog: [],
  log: [],
  receipt: null,
  paused: false,
  startedAt: null,
  source: "demo" as TripStore["source"],
  tripId: null as string | null,
  requestId: null as string | null,
  goalId: null as string | null,
  robotId: null as string | null,
  robotName: null as string | null,
  tripState: null as string | null,
  error: null as string | null,
  shippingCents: 0,
  pending: [] as Approval[],
  steps: [] as ThinkStep[],
  followup: null as TripStore["followup"],
  productsVersion: 0,
};

export const useTrip = create<TripStore>((set, get) => {
  const emit = (type: string, summary: string) =>
    set((s) => ({ log: [...s.log.slice(-199), { seq: ++seq, t: Date.now(), type, summary }] }));

  const sleep = async (ms: number, token: number) => {
    let left = ms;
    while (left > 0) {
      if (token !== runToken) throw new Error("cancelled");
      const step = Math.min(50, left);
      await new Promise((r) => setTimeout(r, step));
      if (!get().paused) left -= step * get().speed;
    }
  };

  const ask = (a: Approval, token: number, auto: string): Promise<string> => {
    set({ approval: a });
    emit(a.kind === "pick" ? "pick.choices" : "approval.requested", approvalLabel(a));
    if (get().autoAnswer) {
      return sleep(1400, token).then(() => {
        get().answer(auto);
        return auto;
      });
    }
    return new Promise((r) => (resolveApproval = r));
  };

  const drive = async (path: Pose[], token: number) => {
    set((s) => ({ robot: { ...s.robot, status: "driving" } }));
    const v = 0.9; // m/s at 1×
    for (let i = 1; i < path.length; i++) {
      const a = path[i - 1],
        b = path[i];
      const len = Math.hypot(b.x - a.x, b.y - a.y);
      const theta = Math.atan2(b.y - a.y, b.x - a.x);
      let d = 0;
      while (d < len) {
        await sleep(50, token);
        if (get().paused) continue;
        d = Math.min(len, d + v * 0.05 * get().speed);
        const k = len ? d / len : 1;
        set((s) => ({ robot: { ...s.robot, x: a.x + (b.x - a.x) * k, y: a.y + (b.y - a.y) * k, theta, speed: v } }));
      }
    }
    set((s) => ({ robot: { ...s.robot, speed: 0 } }));
  };

  const addToCart = (line: CartLine) =>
    set((s) => {
      const i = s.cart.findIndex((c) => c.sku === line.sku && c.source === line.source);
      const cart = [...s.cart];
      if (i >= 0) cart[i] = { ...cart[i], qty: cart[i].qty + line.qty };
      else cart.push(line);
      return { cart };
    });

  const run = async (token: number) => {
    const s0 = get();
    const route = s0.route!;
    const draft = s0.draft!;
    emit("trip.state_changed", "planned → executing");
    for (const leg of route.legs) {
      const stop = get().stops[leg.to];
      set((s) => ({ activeStop: stop.id, stopStatus: { ...s.stopStatus, [stop.id]: "active" } }));
      emit("robot.command", `goto ${stop.label}`);
      await drive(leg.path, token);
      emit("robot.arrived", `${stop.label}, (${stop.pose.x.toFixed(1)}, ${stop.pose.y.toFixed(1)})`);

      if (stop.kind === "end") break;
      const p = productBySku[stop.sku!];

      if (stop.kind === "optional") {
        set((s) => ({ robot: { ...s.robot, status: "waiting" } }));
        const ans = await ask(
          { id: `ap_${stop.id}`, kind: "detour", stop_id: stop.id, sku: stop.sku!, reason: stop.reason!, unit_cents: stop.unit_cents! },
          token,
          "yes",
        );
        if (ans !== "yes") {
          set((s) => ({ stopStatus: { ...s.stopStatus, [stop.id]: "declined" } }));
          emit("stop.updated", `${p.name}: declined, no pick`);
          continue;
        }
      }

      set((s) => ({ robot: { ...s.robot, status: "scanning" } }));
      await sleep(1200, token);
      const slot = slotsOf(get().storeId)[stop.sku!];
      emit("scan.result", `${slot.slot_id}: ${p.name}, ${slot.qty} on shelf${slot.deal ? `, deal ${slot.deal.percent_off ?? ""}% off` : ""}`);

      let lot: string | undefined;
      const firstFreshPick = p.category === "produce" && slot.lots.length >= 3 && !get().approvalsLog.some((a) => a.kind === "pick");
      if (firstFreshPick) {
        const cands = [...slot.lots]
          .sort((a, b) => (b.freshness ?? 0) - (a.freshness ?? 0))
          .slice(0, 3)
          .map((l, i) => ({
            instance_id: `inst_${l.lot_id}`,
            lot_id: l.lot_id,
            freshness: l.freshness ?? 0,
            expiry: l.expiry,
            note: i === 0 ? "Firm, ready in ~3 days" : i === 1 ? "Ripe by tonight" : "Ripe now, use today",
          }));
        set((s) => ({ robot: { ...s.robot, status: "waiting" } }));
        const choice = await ask({ id: `ap_pick_${stop.id}`, kind: "pick", stop_id: stop.id, sku: stop.sku!, candidates: cands }, token, cands[1].instance_id);
        lot = cands.find((c) => c.instance_id === choice)?.lot_id;
      }

      set((s) => ({ robot: { ...s.robot, status: "picking" } }));
      await sleep(1500, token);
      addToCart({ sku: stop.sku!, qty: stop.qty ?? 1, unit_cents: stop.unit_cents!, source: "in_store", verified: null, lot });
      emit("item.picked", `${stop.qty ?? 1} × ${p.name}${lot ? ` (lot ${lot})` : ""} → basket`);
      await sleep(500, token);
      set((s) => ({
        cart: s.cart.map((c) => (c.sku === stop.sku && c.source === "in_store" ? { ...c, verified: true } : c)),
        stopStatus: { ...s.stopStatus, [stop.id]: "done" },
      }));
      emit("basket.verified", `${p.name} matched by basket camera`);
      emit("cart.updated", `cart total ${(cartTotal(get().cart) / 100).toFixed(2)}`);
    }

    // checkout
    set((s) => ({ phase: "checkout", activeStop: "end", stopStatus: { ...s.stopStatus, end: "done" }, robot: { ...s.robot, status: "waiting" } }));
    const cart = get().cart;
    const totals = draftTotal(draft, new Set(get().accepted));
    const inStore = cart.filter((c) => c.source === "in_store").reduce((s, c) => s + c.qty * c.unit_cents, 0);
    const onlineCents = cart.filter((c) => c.source === "online").reduce((s, c) => s + c.qty * c.unit_cents, 0);
    const total = inStore + onlineCents + totals.shipping;
    const decision = mandateCheck(
      total,
      cart.map((c) => ({ sku: c.sku, unit_cents: c.unit_cents })),
      get().storeId,
    );
    emit("mandate.decision", `${decision.verdict}, ${decision.reasons.map((r) => r.code).join(", ")}`);
    const hash = cartHash(cart);
    const ans = await ask({ id: "ap_checkout", kind: "checkout", total_cents: total, cart_hash: hash, reasons: decision.reasons }, token, "approve");
    if (ans !== "approve") {
      emit("approval.resolved", "checkout declined, cart kept for later");
      set((s) => ({ phase: "done", robot: { ...s.robot, status: "done" } }));
      return;
    }
    await sleep(900, token);
    const ts = "2026-10-17T19:42:00Z";
    const receipt: Receipt = {
      receipt_id: `rcpt_${hash.slice(0, 8)}`,
      total_cents: total,
      in_store_cents: inStore,
      online_cents: onlineCents,
      shipping_cents: totals.shipping,
      card: "Visa •• 4242",
      provider: "Stripe test mode via Visa adapter",
      ts,
    };
    const tripId = `trip_maya_1017_${get().storeId}`;
    const newPurchases: Purchase[] = cart.map((c, i) => ({
      purchase_id: `pur_maya_live_${i}`,
      user_id: "maya",
      sku: c.sku,
      qty: c.qty,
      unit_price_cents: c.unit_cents,
      store_id: c.source === "online" ? "online" : get().storeId,
      trip_id: tripId,
      ts,
      rating: null,
      synthetic: false,
    }));
    emit("checkout.completed", `${receipt.receipt_id}, $${(total / 100).toFixed(2)}, test mode`);
    set((s) => ({
      phase: "done",
      receipt,
      robot: { ...s.robot, status: "done" },
      extraPurchases: [...s.extraPurchases.filter((p) => p.trip_id !== tripId), ...newPurchases],
    }));
  };

  return {
    ...initial,
    extraPurchases: [],
    speed: 1,
    autoAnswer: false,

    placeOrder: (draft, accepted, mode) => {
      stopGateway();
      runToken++;
      resolveApproval = null;
      seq = 0;
      const store = STORES[draft.store_id];
      const plan = buildTripPlan(draft, accepted, mode);
      const { route, baseline_m } = plan;
      const onlineLines: CartLine[] = draft.lines
        .filter((l) => l.sourcing === "online")
        .map((l) => ({ sku: l.sku, qty: l.qty, unit_cents: l.unit_cents, source: "online", verified: null }));
      set({
        ...initial,
        phase: mode === "with_me" ? "waiting_login" : "shopping",
        mode,
        draft,
        accepted,
        loginCode: "482 913",
        ...plan,
        cart: onlineLines,
        startedAt: Date.now(),
      });
      emit("trip.created", `${draft.title}, ${draft.lines.length} items, budget $${draft.budget_cents / 100}`);
      emit("list.ready", draft.lines.map((l) => `${l.qty}× ${l.sku}`).join(", "));
      const nOnline = onlineLines.length;
      emit("sourcing.split", `${draft.lines.length - nOnline} in store at ${store.name}, ${nOnline} online (${online.name})`);
      emit("recs.ready", `${draft.recs.length} suggestions, ${accepted.length} kept, 0 sponsored`);
      emit("route.planned", `${route.order.length - 2} stops, ${route.total_m.toFixed(1)} m (list-only ${baseline_m.toFixed(1)} m)`);
      if (mode === "pickup") {
        const t = runToken;
        run(t).catch(() => {});
      }
    },

    followGateway: (o) => {
      runToken++;
      resolveApproval = null;
      set({ ...initial, source: "gateway", tripId: o.tripId, requestId: o.requestId ?? null, robotId: o.robotId ?? null, mode: o.mode ?? "pickup", phase: "shopping" });
      startGateway(useTrip, o);
    },

    robotLogin: () => {
      if (get().phase !== "waiting_login" || get().source === "gateway") return;
      set({ phase: "shopping" });
      emit("robot.login", `${maya.profile.display_name} logged in at the robot with code ${get().loginCode}`);
      const t = runToken;
      run(t).catch(() => {});
    },

    answer: (value) => {
      const a = get().approval;
      if (!a) return;
      if (get().source === "gateway") return void answerGateway(useTrip, a, value, approvalLabel(a));
      set((s) => ({
        approval: null,
        approvalsLog: [...s.approvalsLog, { id: a.id, kind: a.kind, label: approvalLabel(a), answer: value, t: Date.now() }],
      }));
      emit("approval.resolved", `${approvalLabel(a)} → ${value}`);
      const r = resolveApproval;
      resolveApproval = null;
      r?.(value);
    },

    // playback controls drive the replay only; a real robot is never auto-answered (checkout must be the shopper's yes)
    setSpeed: (n) => {
      if (get().source === "gateway") return;
      set({ speed: n });
    },
    setAutoAnswer: (b) => {
      if (get().source === "gateway") return;
      set({ autoAnswer: b });
      const a = get().approval;
      if (b && a) get().answer(a.kind === "pick" ? a.candidates[1].instance_id : a.kind === "checkout" ? "approve" : "yes");
    },
    togglePause: () => {
      if (get().source === "gateway") return;
      set((s) => ({ paused: !s.paused }));
    },
    reset: () => {
      stopGateway();
      runToken++;
      resolveApproval = null;
      set({ ...initial });
    },
  };
});

export function cartTotal(cart: CartLine[]) {
  return cart.reduce((s, c) => s + c.qty * c.unit_cents, 0);
}

function cartHash(cart: CartLine[]) {
  const str = cart.map((c) => `${c.sku}:${c.qty}:${c.unit_cents}`).join("|");
  let h = 2166136261;
  for (let i = 0; i < str.length; i++) h = Math.imul(h ^ str.charCodeAt(i), 16777619);
  return (h >>> 0).toString(16).padStart(8, "0") + "c0ffee";
}

export function approvalLabel(a: Approval) {
  if (a.kind === "detour") return `Add ${productBySku[a.sku]?.name ?? a.sku}?`;
  if (a.kind === "pick") return `Pick which ${productBySku[a.sku]?.name ?? a.sku}?`;
  if (a.kind === "question") return a.title;
  return `Approve checkout $${(a.total_cents / 100).toFixed(2)}?`;
}
