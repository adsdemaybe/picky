"use client";
// Trip store + demo replay engine.
// Events use contracts.events names (route.planned, robot.state, item.picked, approval.requested, ...), so the
// GatewaySource (lib/gateway/tripSource.ts) feeds the same store from a real gateway trip (`source: "gateway"`).
// In demo mode the engine below produces them client-side and pauses on every approval until the shopper answers.
import { create } from "zustand";
import { STORES, maya, online, productBySku, slotsOf, type Pose, type Purchase, type StoreId } from "./data";
import { answerGateway, answerVerification, startGateway, stopGateway, type FollowOptions } from "./gateway/tripSource";
import { draftTotal, mandateCheck, type Draft } from "./order";
import { planRoute, type PlannedRoute } from "./routing";

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
      /** `thumb` = base64 JPEG crop of that unit; `photo` = an image URL (either one makes it a photo card) */
      candidates: { instance_id: string; lot_id: string; freshness: number; expiry: string | null; note: string; thumb?: string; photo?: string }[];
      /** instance the camera ranked first (gateway); the replay recommends the 2nd candidate */
      recommended?: string;
      /** produce review: choose `need` units (answer = the chosen ids joined by ","); else one tap answers */
      multi?: boolean;
      need?: number;
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
/** Per-item purchase check: a photo of what the robot just put in the basket, ✓ / ✗, accepted automatically when the
 *  countdown runs out. Gateway checks answer their approval; client-side ones (no gateway support yet) only flag. */
export type Verification = {
  id: string;
  sku: string;
  qty: number;
  /** object URL / data URI / gateway URL of the photo; null = illustration */
  photo: string | null;
  /** tried when `photo` fails to load (gateway product image) */
  alt_photo?: string | null;
  note?: string;
  /** the camera's own verdict, when the gateway sends one */
  verdict?: "match" | "mismatch" | "uncertain" | null;
  auto_ms: number;
  created: number;
  status: "pending" | "accepted" | "auto" | "rejected";
  /** gateway approval id to answer; null = a client-side check */
  approval_id: string | null;
  /** which camera took the photo, for the caption */
  camera?: string;
};
export type PayKind = "mandate" | "token" | "approval" | "authorized" | "receipt";
export type PayStatus = "pending" | "active" | "done" | "failed";
/** payment.agent_step (gateway) or nothing: the Visa payment agent timeline is otherwise derived from the trip. */
export type PayEvent = {
  kind: PayKind;
  status: PayStatus;
  title?: string;
  detail?: string;
  /** the agent's one-line narration (PaymentAgentStep.narration) */
  narration?: string;
  /** PaymentAgentStep.label, e.g. "Sandbox / simulated — no real money" */
  label?: string;
  /** raw step name (cart_mandate, passkey_requested, token_issued, …) */
  name?: string;
  t: number;
};
/** One unit at the checkout handoff: still in the basket, in the gripper, or set down on the counter. */
export type HandoffUnit = { key: string; sku: string; state: "basket" | "arm" | "counter" };
/** Checkout handoff: the arm takes each unit out of the basket and sets it on the counter (the loop's `handoff` step). */
export type Handoff = {
  status: "active" | "done";
  units: HandoffUnit[];
  /** who drives the arm: "Scripted skill" / "SmolVLA" (gateway step tier), null = the replay */
  tier: string | null;
  startedAt: number;
  /** checkout.unload (0.2.4): the backend's line for the current unit ("Unloading 3/9: Mild Chunky Salsa → counter ✓") */
  text?: string;
  n?: number;
  total?: number;
};
type CheckoutApproval = Extract<Approval, { kind: "checkout" }>;
export type RobotStatus = "idle" | "driving" | "scanning" | "picking" | "unloading" | "waiting" | "done";
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
  /** per-item photo checks, oldest first */
  verifications: Verification[];
  /** payment.agent_step events (gateway); empty = the timeline is derived */
  payEvents: PayEvent[];
  /** the last checkout card (kept after it is answered, for the payment timeline) */
  lastCheckout: CheckoutApproval | null;
  /** basket → counter at checkout (null until the robot starts unloading) */
  handoff: Handoff | null;
  // actions
  placeOrder: (draft: Draft, accepted: string[], mode: Mode) => void;
  followGateway: (o: FollowOptions) => void;
  robotLogin: () => void;
  answer: (value: string) => void;
  /** ✓ (ok) / ✗ on a purchase check; `auto` = the countdown ran out */
  verify: (id: string, ok: boolean, auto?: boolean) => void;
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
  verifications: [] as Verification[],
  payEvents: [] as PayEvent[],
  lastCheckout: null as CheckoutApproval | null,
  handoff: null as Handoff | null,
};

/** Seconds a purchase check waits before it is accepted automatically. */
export const VERIFY_AUTO_MS = 8000;

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

  /** The handoff step of the replay: each unit goes basket → gripper → counter (what the VLA loop's handoff does). */
  const unload = async (token: number) => {
    const units: HandoffUnit[] = get()
      .cart.filter((c) => c.source === "in_store")
      .flatMap((c) => Array.from({ length: c.qty }, (_, k) => ({ key: `${c.sku}#${k}`, sku: c.sku, state: "basket" as const })));
    if (!units.length) return;
    set((s) => ({ phase: "checkout", activeStop: "end", robot: { ...s.robot, status: "unloading" }, handoff: { status: "active", units, tier: null, startedAt: Date.now() } }));
    emit("step.started", `handoff: unload ${units.length} items onto the counter`);
    const mark = (i: number, state: HandoffUnit["state"]) =>
      set((s) => (s.handoff ? { handoff: { ...s.handoff, units: s.handoff.units.map((u, j) => (j === i ? { ...u, state } : u)) } } : {}));
    for (let i = 0; i < units.length; i++) {
      mark(i, "arm");
      await sleep(1250, token);
      mark(i, "counter");
      emit("vla.status", `${productBySku[units[i].sku]?.name ?? units[i].sku}: basket → counter (${i + 1}/${units.length})`);
      await sleep(250, token);
    }
    set((s) => ({ handoff: s.handoff && { ...s.handoff, status: "done" } }));
    emit("step.completed", `handoff: ${units.length} items on the counter, basket empty`);
    await sleep(500, token);
  };

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
        const need = Math.max(1, stop.qty ?? 1);
        const lots = [...slot.lots].sort((a, b) => (b.freshness ?? 0) - (a.freshness ?? 0)).slice(0, 3);
        // one lot per card for a single item; for several, individual units (two per lot) to choose from
        const units = need > 1 ? lots.flatMap((l, i) => [0, 1].map((k) => ({ l, i, k }))) : lots.map((l, i) => ({ l, i, k: 0 }));
        const cands = units.map(({ l, i, k }) => ({
          instance_id: `inst_${l.lot_id}${need > 1 ? `_${k}` : ""}`,
          lot_id: l.lot_id,
          freshness: Math.max(0, (l.freshness ?? 0) - k * 0.04),
          expiry: l.expiry,
          note: UNIT_NOTES[i][k],
        }));
        const pickIds = need > 1 ? cands.slice(0, need).map((c) => c.instance_id) : [cands[1].instance_id];
        set((s) => ({ robot: { ...s.robot, status: "waiting" } }));
        const choice = await ask(
          { id: `ap_pick_${stop.id}`, kind: "pick", stop_id: stop.id, sku: stop.sku!, candidates: cands, multi: need > 1, need, recommended: pickIds[0] },
          token,
          pickIds.join(","),
        );
        const first = choice.split(",")[0];
        lot = cands.find((c) => c.instance_id === first)?.lot_id;
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
      // the shopper sees what went in: ✓ / ✗, or it is accepted when the countdown runs out (the robot keeps going)
      set((s) => ({
        verifications: [
          ...s.verifications,
          { id: `ver_${stop.id}`, sku: stop.sku!, qty: stop.qty ?? 1, photo: null, auto_ms: VERIFY_AUTO_MS, created: Date.now(), status: "pending", approval_id: null, camera: "Wrist camera" },
        ],
      }));
    }

    // checkout: the arm unloads the basket onto the counter, unit by unit, then the payment agent takes over
    await unload(token);
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
      const acc = new Set(accepted);
      const store = STORES[draft.store_id];
      const slotBySku = slotsOf(draft.store_id);
      const stops: Record<string, Stop> = {
        start: { id: "start", kind: "start", label: "Robot dock", pose: store.robot_home },
        end: { id: "end", kind: "end", label: mode === "pickup" ? "Pickup counter" : store.kind === "wireless" ? "Counter" : "Checkout", pose: store.checkout },
      };
      for (const l of draft.lines.filter((l) => l.sourcing === "in_store")) {
        const slot = slotBySku[l.sku];
        stops[`s_${l.sku}`] = {
          id: `s_${l.sku}`,
          kind: "list",
          label: productBySku[l.sku].name,
          pose: slot.approach_pose,
          sku: l.sku,
          qty: l.qty,
          slot_id: slot.slot_id,
          unit_cents: l.unit_cents,
        };
      }
      for (const r of draft.recs.filter((r) => acc.has(r.rec_id))) {
        const slot = slotBySku[r.sku];
        stops[`o_${r.sku}`] = {
          id: `o_${r.sku}`,
          kind: "optional",
          label: productBySku[r.sku].name,
          pose: slot.approach_pose,
          sku: r.sku,
          qty: 1,
          slot_id: slot.slot_id,
          rec_id: r.rec_id,
          reason: r.reason,
          unit_cents: r.unit_cents,
        };
      }
      const mid = Object.values(stops).filter((s) => s.kind === "list" || s.kind === "optional");
      const route = planRoute(store, stops.start, mid, stops.end);
      const base = planRoute(
        store,
        stops.start,
        mid.filter((s) => s.kind === "list"),
        stops.end,
      );
      const offRoute = mid
        .filter((s) => s.kind === "optional")
        .filter((o) => route.total_m - planRoute(store, stops.start, mid.filter((s) => s.id !== o.id), stops.end).total_m > 0.5)
        .map((o) => o.id);
      const onlineLines: CartLine[] = draft.lines
        .filter((l) => l.sourcing === "online")
        .map((l) => ({ sku: l.sku, qty: l.qty, unit_cents: l.unit_cents, source: "online", verified: null }));
      set({
        ...initial,
        storeId: draft.store_id,
        robot: { ...store.robot_home, status: "idle", speed: 0 },
        phase: mode === "with_me" ? "waiting_login" : "shopping",
        mode,
        draft,
        accepted,
        loginCode: "482 913",
        stops,
        route,
        baseline_m: base.total_m,
        baselinePath: base.legs.flatMap((l) => l.path),
        offRoute,
        stopStatus: Object.fromEntries(Object.keys(stops).map((k) => [k, k === "start" ? "done" : "pending"])),
        cart: onlineLines,
        startedAt: Date.now(),
      });
      emit("trip.created", `${draft.title}, ${draft.lines.length} items, budget $${draft.budget_cents / 100}`);
      emit("list.ready", draft.lines.map((l) => `${l.qty}× ${l.sku}`).join(", "));
      const nOnline = onlineLines.length;
      emit("sourcing.split", `${draft.lines.length - nOnline} in store at ${store.name}, ${nOnline} online (${online.name})`);
      emit("recs.ready", `${draft.recs.length} suggestions, ${accepted.length} kept, 0 sponsored`);
      emit("route.planned", `${route.order.length - 2} stops, ${route.total_m.toFixed(1)} m (list-only ${base.total_m.toFixed(1)} m)`);
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

    verify: (id, ok, auto) => {
      const v = get().verifications.find((x) => x.id === id);
      if (!v || v.status !== "pending") return;
      const status: Verification["status"] = auto ? "auto" : ok ? "accepted" : "rejected";
      set((s) => ({
        verifications: s.verifications.map((x) => (x.id === id ? { ...x, status } : x)),
        // ✗ flags the line until the robot (or the gateway) sorts it out
        cart: ok ? s.cart : s.cart.map((c) => (c.sku === v.sku && c.source === "in_store" ? { ...c, verified: false } : c)),
      }));
      const name = productBySku[v.sku]?.name ?? v.sku;
      emit("purchase.check", `${name}: ${auto ? "accepted automatically" : ok ? "looks right" : "flagged by the shopper"}`);
      // the gateway auto-accepts on its own clock (labelled auto_accepted); only the shopper's taps are sent
      if (get().source === "gateway" && !auto) void answerVerification(useTrip, v, ok);
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
      if (b && a) get().answer(a.kind === "pick" ? autoPick(a) : a.kind === "checkout" ? "approve" : "yes");
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

// remember the checkout card after it is answered: the payment timeline still shows its mandate reasons and cart hash
useTrip.subscribe((s) => {
  const co = s.approval?.kind === "checkout" ? s.approval : s.pending.find((p): p is CheckoutApproval => p.kind === "checkout");
  if (co && co !== s.lastCheckout) useTrip.setState({ lastCheckout: co });
});

const UNIT_NOTES = [
  ["Firm, ready in ~3 days", "Firm, a little green"],
  ["Ripe by tonight", "Ripe tomorrow"],
  ["Ripe now, use today", "Soft spot near the stem"],
];

/** What "answer for me" picks: the camera's top `need` units (or the replay's 2nd batch for a single pick). */
function autoPick(a: Extract<Approval, { kind: "pick" }>) {
  if (a.multi && (a.need ?? 1) > 1) return a.candidates.slice(0, a.need).map((c) => c.instance_id).join(",");
  return (a.candidates[1] ?? a.candidates[0]).instance_id;
}

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
