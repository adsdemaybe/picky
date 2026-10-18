"use client";
// GatewayTripSource: follows ONE gateway trip and writes it into the existing trip store (lib/trip.ts), so the trip
// page, map, basket, approvals, live feed and home banner render a real robot exactly like the demo replay.
//   1. GET /api/trips/{id} → hydrate
//   2. WS /ws?trip_id=…: trip.snapshot first (re-hydrate), then the trip's ring events (seq ≤ snapshot: logged, not
//      re-applied), then live events. Reconnects resume with after_seq=<last seq> (contracts/api.md sync recipe).
//   3. every fresh event (seq > last applied) → apply(), mapping contract shapes with adapt.ts
// Approvals are answered over REST (POST /api/approvals/{id}) so 404 / 409 come back as status codes, and nothing
// here ever answers for the shopper (checkout needs their explicit yes). Imports only types from lib/trip.ts:
// trip.ts passes the store in, so there is no runtime import cycle.
import { toast } from "sonner";
import type { StoreApi, UseBoundStore } from "zustand";
import { STORES, maya, productBySku, registerProduct, registerStore, type Purchase, type StoreId } from "@/lib/data";
import { useFeed } from "@/lib/feed";
import type { Rec } from "@/lib/order";
import type { Approval, LogEvent, Mode, RobotStatus, Stop, StopStatus, ThinkStep, TripStore } from "@/lib/trip";
import {
  KIND,
  PHASE,
  STOP,
  approvalFromContract,
  cartFromContract,
  draftFromTrip,
  lineFromItem,
  linesFromItems,
  logFromResponse,
  placeholderProduct,
  productFromContract,
  purchasesFromReceipt,
  recFromContract,
  receiptFromContract,
  robotStatusFor,
  shippingOf,
  summarize,
} from "./adapt";
import { GATEWAY_URL, GatewayError, errorText, gw, type S } from "./client";
import { openEventSocket } from "./eventSocket";
import type { ApprovalRequest, BasketCheck, BrainStep, GatewayEvent, MandateDecision, PickChoice, Pose2D, ProfileUpdate, Receipt, RequestPlan, Route, Trip } from "./events";
import { useGateway } from "./useGateway";

type TripApi = UseBoundStore<StoreApi<TripStore>>;
export type FollowOptions = { tripId: string; requestId?: string | null; robotId?: string | null; mode?: Mode; title?: string };
type ProductHint = { sku: string | null | undefined; name?: string | null; category?: string | null; price_cents?: number | null };

/** Not shown in "Behind the scenes" (high rate; the thinking log and the map show them). */
const NOISY = new Set(["robot.state", "brain.step", "camera.frame", "frame"]);
const LOG_CAP = 300;
const STEP_CAP = 300;
/** One toast for the end of a trip: Stop robot, trip.state_changed → failed and goal.failed replace each other. */
export const TRIP_END_TOAST = "trip-end";

// ---- per-trip module state (reset by startGateway); `token` drops late results from a previous trip
let token = 0;
let opts: FollowOptions | null = null;
let closeSocket: (() => void) | null = null;
let queue: Promise<void> = Promise.resolve();
let lastSeq = 0;
let connectedOnce = false;
let userId = maya.profile.user_id;
let seenLog = new Set<number>();
let seenSteps = new Set<string>();
let pickChoices = new Map<string, PickChoice>();
let requests = new Map<string, ApprovalRequest>();
let answeredLocally = new Set<string>();
let verifiedSkus = new Set<string>();
let warned = new Set<string>();
let lastMandate: MandateDecision | null = null;
let boundRobot: string | null = null;
let gotRobotState = false;
let lastRobotError: string | null = null;
let logBuf: LogEvent[] = [];
let logTimer: ReturnType<typeof setTimeout> | null = null;
let tweenTimer: ReturnType<typeof setInterval> | null = null;
// shared across trips
const fetchingProducts = new Set<string>();
const fetchingStores = new Map<string, Promise<boolean>>();

// ------------------------------------------------------------------------------------------------ lifecycle

export function startGateway(store: TripApi, o: FollowOptions) {
  stopGateway();
  const my = ++token;
  opts = o;
  queue = Promise.resolve();
  lastSeq = 0;
  connectedOnce = false;
  seenLog = new Set();
  seenSteps = new Set();
  pickChoices = new Map();
  requests = new Map();
  answeredLocally = new Set();
  verifiedSkus = new Set();
  warned = new Set();
  lastMandate = null;
  gotRobotState = false;
  lastRobotError = null;
  const alive = () => my === token;

  void (async () => {
    try {
      const { data } = await gw.trip(o.tripId);
      if (!alive()) return;
      await enqueue(store, my, () => hydrate(store, data));
    } catch (e) {
      if (!alive()) return;
      if (e instanceof GatewayError && e.status === 404) {
        toast.error("That trip isn't on the gateway");
        store.getState().reset();
        return;
      }
      // the socket keeps retrying (the chip and the planning screen say "Reconnecting"); its snapshot hydrates the trip
      toast.error(`Couldn't load the trip: ${errorText(e)}`);
    }
    if (!alive()) return;
    closeSocket = openEventSocket({
      filters: [["trip_id", o.tripId]],
      afterSeq: () => (connectedOnce ? lastSeq : null),
      onOpen: () => {
        connectedOnce = true;
      },
      onEvent: (ev) => void enqueue(store, my, () => handle(store, ev)),
      onStatus: (st) => {
        if (alive()) useGateway.getState().setSocket(st);
      },
    });
    if (o.robotId && o.robotId !== boundRobot) bindRobot(store, o.robotId);
  })();
}

/** Close the socket and timers. The store itself is reset by the caller (placeOrder / reset / followGateway). */
export function stopGateway() {
  token++;
  closeSocket?.();
  closeSocket = null;
  if (tweenTimer) clearInterval(tweenTimer);
  tweenTimer = null;
  if (logTimer) clearTimeout(logTimer);
  logTimer = null;
  logBuf = [];
  boundRobot = null;
  useFeed.getState().unbindTrip();
  useGateway.getState().setSocket("idle");
}

function enqueue(store: TripApi, my: number, fn: () => void | Promise<void>) {
  // events are applied strictly in arrival order, even when one needs an await (unknown store → planogram)
  queue = queue.then(() => (my === token ? fn() : undefined)).catch((e) => console.warn("[gateway]", e));
  return queue;
}

// ------------------------------------------------------------------------------------------------ order flow

export type OrderResult =
  | { kind: "following"; url: string; reply: string }
  | { kind: "no_trip"; reply: string }
  | { kind: "failed"; reply: string }
  | { kind: "unreachable"; reply: string };

/** POST /api/requests (natural language → fleet → goals/trips). Request-created trips start by themselves. */
export async function startGatewayOrder(store: TripApi, o: { text: string; storeId: string; mode: Mode; title?: string }): Promise<OrderResult> {
  let plan: RequestPlan;
  try {
    plan = (await gw.createRequest({ text: o.text, user_id: maya.profile.user_id, channel: "web", role: "shopper", store_id: o.storeId })).data;
  } catch (e) {
    if (e instanceof GatewayError && e.status >= 400 && e.status < 500) return { kind: "failed", reply: e.detail };
    return { kind: "unreachable", reply: errorText(e) };
  }
  if (plan.status === "failed") return { kind: "failed", reply: plan.error ?? plan.reply };
  const tripId = plan.trip_ids[0];
  if (!tripId) return { kind: "no_trip", reply: plan.reply || "Picky needs a bit more to go on." };
  const robotId = plan.goals.find((g) => g.robot_id)?.robot_id ?? null;
  store.getState().followGateway({ tripId, requestId: plan.request_id, robotId, mode: o.mode, title: o.title });
  const split = plan.trip_ids.length > 1 ? ` Split across ${plan.trip_ids.length} stores; following the first.` : "";
  return { kind: "following", url: `/trip?trip=${encodeURIComponent(tripId)}&request=${encodeURIComponent(plan.request_id)}&mode=${o.mode}`, reply: plan.reply + split };
}

/** "Picky learned …" once per profile change: a POST /feedback answer and the profile.updated event it triggers on
 *  the trip socket carry the same update, so they share a toast id. */
export function toastLearned(u: ProfileUpdate) {
  if (u.changes.length) toast(`Picky learned: ${u.summary || u.changes[0].reason}`, { id: `learned:${u.user_id}:${u.version_after}` });
}

// ------------------------------------------------------------------------------------------------ approvals

export async function answerGateway(store: TripApi, a: Approval, value: string, label: string) {
  const my = token;
  const base = { channel: "web" as const, user_id: userId };
  let body: S["ApprovalDecisionRequest"];
  if (a.kind === "pick") body = { ...base, decision: "choice", choice: value };
  else if (a.kind === "checkout") body = value === "approve" ? { ...base, decision: "approve", cart_hash: a.cart_hash } : { ...base, decision: "reject" };
  else if (value === "yes") body = { ...base, decision: "approve" };
  else if (value === "no") body = { ...base, decision: "reject" };
  else body = a.kind === "question" && a.multi ? { ...base, decision: "choice", choices: [value] } : { ...base, decision: "choice", choice: value };

  // optimistic, like the replay: the card goes away and the answer is logged right away
  answeredLocally.add(a.id);
  store.setState((s) => {
    const pending = s.pending.filter((p) => p.id !== a.id);
    return {
      pending,
      approval: pending[0] ?? null,
      approvalsLog: [...s.approvalsLog.filter((l) => l.id !== a.id), { id: a.id, kind: a.kind, label: requests.get(a.id)?.title ?? label, answer: value, t: Date.now() }],
    };
  });
  try {
    await gw.answer(a.id, body);
  } catch (e) {
    answeredLocally.delete(a.id);
    if (my !== token) return;
    if (e instanceof GatewayError && e.status === 409) {
      toast("Your cart changed, here's the new total");
      await resync(store);
      return;
    }
    if (e instanceof GatewayError && e.status === 404) {
      toast("That question already closed");
      return;
    }
    store.setState((s) => {
      const pending = [a, ...s.pending.filter((p) => p.id !== a.id)];
      return { pending, approval: pending[0], approvalsLog: s.approvalsLog.filter((l) => l.id !== a.id) };
    });
    toast.error(`Couldn't send your answer: ${errorText(e)}`);
  }
}

async function resync(store: TripApi) {
  const my = token;
  const id = store.getState().tripId;
  if (!id) return;
  try {
    const { data } = await gw.trip(id);
    await enqueue(store, my, () => hydrate(store, data));
  } catch (e) {
    toast.error(`Couldn't refresh the trip: ${errorText(e)}`);
  }
}

function toApproval(store: TripApi, r: ApprovalRequest, recs?: Rec[]) {
  return approvalFromContract(r, { pick: pickChoices.get(r.approval_id), mandate: lastMandate, recs: recs ?? store.getState().draft?.recs ?? [] });
}

/** Re-map queued cards after pick.choices / mandate.decision add detail (scores, mandate reasons). */
function refreshPending(store: TripApi) {
  const s = store.getState();
  if (!s.pending.length) return;
  const pending = s.pending.map((a) => {
    const r = requests.get(a.id);
    return r ? toApproval(store, r) : a;
  });
  store.setState({ pending, approval: pending[0] ?? null });
}

// ------------------------------------------------------------------------------------------------ events

async function handle(store: TripApi, ev: GatewayEvent) {
  if (ev.type === "trip.snapshot") {
    await hydrate(store, ev.trip);
    lastSeq = Math.max(lastSeq, ev.seq);
    return;
  }
  if (typeof ev.seq !== "number") return;
  const fresh = ev.seq > lastSeq;
  if (fresh) lastSeq = ev.seq;
  if (!NOISY.has(ev.type) && !seenLog.has(ev.seq)) {
    seenLog.add(ev.seq);
    queueLog(store, ev);
  }
  if (ev.type === "brain.step") addSteps(store, [ev.step]);
  if (ev.type === "pick.choices" && ev.approval_id) {
    pickChoices.set(ev.approval_id, ev.choice);
    refreshPending(store);
  }
  // cart lines stay "picked"; the basket camera's verdict only exists as events, so replayed ones count too
  if (ev.type === "basket.verified") verifyBasket(store, ev.check, fresh);
  // the snapshot's route may be a re-plan that lists only the remaining stops: older routes fill in the finished ones
  if (ev.type === "route.planned" && !fresh) mergeOlderRoute(store, ev.route);
  // other replayed events (≤ the snapshot's seq) are already part of the snapshot
  if (fresh) await apply(store, ev);
}

function verifyBasket(store: TripApi, c: BasketCheck, fresh: boolean) {
  for (const [sku, n] of Object.entries(c.expected)) if ((c.observed[sku] ?? 0) >= n) verifiedSkus.add(sku);
  for (const sku of Object.keys(c.missing)) {
    verifiedSkus.delete(sku);
    if (fresh && !c.ok && !warned.has(`missing:${sku}`)) {
      warned.add(`missing:${sku}`);
      toast.warning(`Basket check: ${productBySku[sku]?.name ?? sku} missing`);
    }
  }
  store.setState((s) => ({
    cart: s.cart.map((l) => (l.source === "in_store" ? { ...l, verified: verifiedSkus.has(l.sku) ? true : c.missing[l.sku] ? null : l.verified } : l)),
  }));
}

/** The robot reached the checkout (the gateway has no stop.updated for "end"). */
function atCheckout(tripState: string | null) {
  return tripState === "checkout" || tripState === "paid" || tripState === "followup" || tripState === "done";
}

async function apply(store: TripApi, ev: GatewayEvent) {
  const s = store.getState();
  switch (ev.type) {
    case "trip.created":
      return hydrate(store, ev.trip);

    case "trip.state_changed": {
      const status = robotStatusFor(null, ev.to_state);
      const patch: Partial<TripStore> = { tripState: ev.to_state, phase: PHASE[ev.to_state] ?? s.phase };
      if (status) patch.robot = { ...s.robot, status, speed: 0 };
      else if (s.robot.status === "waiting" || s.robot.status === "done") patch.robot = { ...s.robot, status: "idle" };
      if (ev.to_state === "failed") {
        patch.error = ev.reason || "The trip failed";
        patch.activeStop = null; // nothing is in progress any more (Stop robot, cancelled goal)
        toast.error(`Trip failed: ${patch.error}`, { id: TRIP_END_TOAST });
      }
      if (atCheckout(ev.to_state) && s.stops.end) Object.assign(patch, { activeStop: "end", stopStatus: { ...s.stopStatus, end: "done" } });
      store.setState(patch);
      return;
    }

    case "intent.received":
      if (s.draft) store.setState({ draft: { ...s.draft, intent: ev.intent.text } });
      return;

    case "list.ready": {
      const sl = ev.shopping_list;
      ensureProducts(store, sl.items.map((i) => ({ sku: i.sku, category: i.category, name: i.query })));
      if (s.draft) store.setState({ draft: { ...s.draft, title: sl.title ?? s.draft.title, budget_cents: sl.budget_cents ?? s.draft.budget_cents, lines: linesFromItems(sl.items) } });
      return;
    }

    case "sourcing.split": {
      if (!s.draft) return;
      const all = [...ev.in_store, ...ev.online];
      ensureProducts(store, all.map((i) => ({ sku: i.sku, category: i.category, name: i.query })));
      const byId = new Map(all.map((i) => [i.item_id, i]));
      const lines = s.draft.lines.map((l) => {
        const i = byId.get(l.line_id);
        return i?.sku ? lineFromItem({ ...i, sku: i.sku }) : l;
      });
      for (const i of all) if (i.sku && !lines.some((l) => l.line_id === i.item_id)) lines.push(lineFromItem({ ...i, sku: i.sku }));
      for (const i of ev.unresolved) {
        const k = lines.findIndex((l) => l.line_id === i.item_id);
        if (k >= 0) lines[k] = { ...lines[k], note: "Not found in store" };
      }
      store.setState({ draft: { ...s.draft, lines } });
      return;
    }

    case "recs.ready": {
      ensureProducts(store, ev.recs.map((r) => ({ sku: r.sku, name: r.product_name, price_cents: r.price_cents })));
      const recs = ev.recs.map(recFromContract);
      if (s.draft) store.setState({ draft: { ...s.draft, recs }, accepted: recs.map((r) => r.rec_id) });
      return;
    }

    case "route.planned":
      applyRoute(store, ev.route, ev.replan);
      return;

    case "stop.updated": {
      const patch: Partial<TripStore> = { stopStatus: { ...s.stopStatus, [ev.stop_id]: STOP[ev.status] ?? "pending" } };
      if (ev.status === "navigating" || ev.status === "arrived") patch.activeStop = ev.stop_id;
      if (ev.status === "navigating" && s.robot.status !== "waiting") patch.robot = { ...s.robot, status: "driving" };
      store.setState(patch);
      return;
    }

    case "robot.state": {
      const st = ev.state;
      const rid = st.robot_id ?? ev.robot_id;
      if (s.robotId && rid && rid !== s.robotId) return;
      gotRobotState = true;
      moveRobot(store, st.pose, robotStatusFor(st.status, s.tripState) ?? s.robot.status, st.status === "navigating" ? 0.5 : 0);
      // once the trip is over (paid, failed, stopped) the robot's own errors aren't the shopper's business
      if (st.error && st.error !== lastRobotError && s.phase !== "done") toast.error(`${s.robotName ?? "Robot"}: ${st.error}`);
      lastRobotError = st.error;
      return;
    }

    case "robot.arrived":
      if (ev.stop_id) store.setState({ activeStop: ev.stop_id });
      return;

    case "item.picked":
      store.setState({ robot: { ...s.robot, status: "picking" } }); // the cart.updated that follows is authoritative
      return;

    case "cart.updated": {
      ensureProducts(store, ev.cart.lines.map((l) => ({ sku: l.sku, name: l.name, category: l.category, price_cents: l.unit_price_cents })));
      store.setState({
        cart: cartFromContract(ev.cart, verifiedSkus),
        shippingCents: shippingOf(ev.cart),
        draft: s.draft && ev.budget_cents != null ? { ...s.draft, budget_cents: ev.budget_cents } : s.draft,
      });
      return;
    }

    case "approval.requested": {
      const r = ev.request;
      if (r.trip_id && r.trip_id !== s.tripId) return;
      requests.set(r.approval_id, r);
      if (answeredLocally.has(r.approval_id) || s.pending.some((a) => a.id === r.approval_id)) return;
      ensureProducts(store, [{ sku: r.sku, price_cents: r.amount_cents }]);
      const pending = [...s.pending, toApproval(store, r)];
      store.setState({ pending, approval: pending[0], robot: { ...s.robot, status: "waiting" } });
      return;
    }

    case "approval.resolved": {
      const res = ev.response;
      answeredLocally.delete(res.approval_id);
      const had = s.pending.some((a) => a.id === res.approval_id);
      const pending = s.pending.filter((a) => a.id !== res.approval_id);
      const logged = s.approvalsLog.some((l) => l.id === res.approval_id);
      store.setState({
        pending,
        approval: pending[0] ?? null,
        approvalsLog: logged ? s.approvalsLog : [...s.approvalsLog, logFromResponse(res, requests.get(res.approval_id), ev.kind)],
      });
      if (res.decision === "timeout" && had) toast("The robot moved on without an answer");
      return;
    }

    case "mandate.decision":
      if (ev.decision.scope === "cart") {
        lastMandate = ev.decision;
        refreshPending(store);
      }
      return;

    case "checkout.completed": {
      const r = ev.receipt;
      if (r.status !== "succeeded") {
        toast.error(`Payment ${r.status} (test mode)${r.error ? `: ${r.error}` : ""}`);
        return;
      }
      store.setState({ receipt: receiptFromContract(r), phase: "done", robot: { ...s.robot, status: "done", speed: 0 }, extraPurchases: withPurchases(s.extraPurchases, r) });
      return;
    }

    case "followup.sent":
      store.setState({ followup: { text: ev.message.text, skus: ev.message.about_skus, quick: ev.message.quick_replies } });
      return;

    case "profile.updated":
      toastLearned(ev.update);
      return;

    case "goal.created":
    case "goal.assigned": {
      const rid = ev.type === "goal.assigned" ? ev.assignment.robot_id : (ev.goal.robot_id ?? null);
      store.setState({ goalId: ev.goal.goal_id });
      if (rid && rid !== boundRobot) bindRobot(store, rid); // reassignment: new robot, same goal
      return;
    }

    case "goal.failed":
      store.setState({ error: ev.reason });
      toast.error(`The robot couldn't finish: ${ev.reason}`, { id: TRIP_END_TOAST });
      return;

    case "error": {
      const key = `error:${ev.arm}:${ev.message}`;
      if (!warned.has(key)) {
        warned.add(key);
        toast.error(`${ev.arm}: ${ev.message}`);
      }
      if (!ev.recoverable) store.setState({ error: ev.message });
      return;
    }

    default:
      return; // goal.updated / completed, scan.result, feedback.received, agent.message: log only
  }
}

// ------------------------------------------------------------------------------------------------ state builders

/** Snapshot / cold load / trip.created: rebuild everything from the Trip (the server is authoritative). */
async function hydrate(store: TripApi, trip: Trip) {
  if (trip.trip_id !== store.getState().tripId) return;
  await ensureStore(store, trip.store_id);
  userId = trip.user_id || userId;
  lastMandate = trip.mandate_decision ?? lastMandate;
  for (const a of trip.approvals) requests.set(a.approval_id, a);
  for (const l of trip.cart.lines) if (l.status === "verified") verifiedSkus.add(l.sku);
  ensureProducts(store, [
    ...(trip.shopping_list?.items ?? []).map((i) => ({ sku: i.sku, category: i.category, name: i.query })),
    ...trip.recs.map((r) => ({ sku: r.sku, name: r.product_name, price_cents: r.price_cents })),
    ...trip.cart.lines.map((l) => ({ sku: l.sku, name: l.name, category: l.category, price_cents: l.unit_price_cents })),
    ...trip.approvals.map((a) => ({ sku: a.sku, price_cents: a.amount_cents })),
  ]);

  const s = store.getState();
  const recs = trip.recs.map(recFromContract);
  const answered = new Set(trip.approval_responses.map((r) => r.approval_id));
  const pending = trip.approvals
    .filter((a) => !answered.has(a.approval_id) && !answeredLocally.has(a.approval_id) && (!a.trip_id || a.trip_id === trip.trip_id))
    .map((a) => toApproval(store, a, recs));
  const approvalsLog = [...trip.approval_responses.map((r) => logFromResponse(r, requests.get(r.approval_id))), ...s.approvalsLog.filter((l) => !answered.has(l.id))].sort(
    (a, b) => a.t - b.t,
  );
  const receipt = trip.receipt?.status === "succeeded" ? trip.receipt : null;
  const status: RobotStatus = robotStatusFor(null, trip.state) ?? (pending.length ? "waiting" : s.robot.status);
  const known = STORES[trip.store_id as StoreId];
  const robotId = s.robotId ?? trip.robot_id;

  store.setState({
    storeId: known ? (trip.store_id as StoreId) : s.storeId,
    tripState: trip.state,
    phase: PHASE[trip.state] ?? "shopping",
    error: trip.error,
    robotId,
    goalId: trip.goal_id ?? s.goalId,
    requestId: s.requestId ?? trip.request_id,
    startedAt: Date.parse(trip.created_at) || s.startedAt,
    draft: draftFromTrip(trip, recs, opts?.title),
    accepted: recs.map((r) => r.rec_id),
    cart: cartFromContract(trip.cart, verifiedSkus),
    shippingCents: shippingOf(trip.cart),
    pending,
    approval: pending[0] ?? null,
    approvalsLog,
    receipt: receipt ? receiptFromContract(receipt) : null,
    followup: trip.followup ? { text: trip.followup.text, skus: trip.followup.about_skus, quick: trip.followup.quick_replies } : s.followup,
    robot: !gotRobotState && known ? { ...known.robot_home, status, speed: 0 } : { ...s.robot, status },
    extraPurchases: receipt ? withPurchases(s.extraPurchases, receipt) : s.extraPurchases,
  });
  if (trip.route) applyRoute(store, trip.route, false, trip.stop_status, trip.current_stop_id);
  if (atCheckout(trip.state) && store.getState().stops.end) store.setState((st) => ({ activeStop: "end", stopStatus: { ...st.stopStatus, end: "done" } }));
  if (trip.state === "failed") store.setState({ activeStop: null });
  if (robotId && robotId !== boundRobot) bindRobot(store, robotId);
}

const pose = (p: Pose2D) => ({ x: p.x, y: p.y, theta: p.theta });
const legOf = (l: Route["legs"][number]) => ({ from: l.from_stop_id, to: l.to_stop_id, path: l.waypoints.map(pose), length: l.distance_m });

function stopOf(st: Route["stops"][number], s: TripStore): Stop {
  const sku = st.skus[0];
  const rec = st.rec_id ? s.draft?.recs.find((r) => r.rec_id === st.rec_id) : undefined;
  return {
    id: st.stop_id,
    kind: KIND[st.kind] ?? "list",
    // mandatory stop labels are the list query ("tortillas"); show the product like the replay does
    label: st.kind === "start" ? "Robot dock" : st.kind === "end" ? (s.mode === "pickup" ? "Pickup counter" : "Checkout") : (sku && productBySku[sku]?.name) || st.label || sku || st.stop_id,
    pose: pose(st.pose),
    sku,
    qty: st.qty,
    slot_id: st.slot_id ?? undefined,
    rec_id: st.rec_id ?? undefined,
    reason: rec?.reason,
    unit_cents: rec?.unit_cents ?? (sku ? productBySku[sku]?.price_cents : undefined),
  };
}

/** The map and stop list number stops by position after "start" (a re-plan starts from where the robot is). */
const withStartFirst = (order: string[]) => (order.includes("start") ? ["start", ...order.filter((id) => id !== "start")] : order);

const finished = (s: TripStore, id: string) => ["done", "declined", "skipped"].includes(s.stopStatus[id]);

function applyRoute(store: TripApi, route: Route, replan: boolean, status?: Trip["stop_status"], current?: string | null) {
  ensureProducts(store, route.stops.flatMap((st) => st.skus.map((sku) => ({ sku, name: st.label }))));
  const s = store.getState();
  const stops: Record<string, Stop> = { ...s.stops }; // a re-plan may list only the remaining stops: keep the old ones
  for (const st of route.stops) stops[st.stop_id] = stopOf(st, s);
  const legs = route.legs.map(legOf);
  let order = route.order;
  let kept: NonNullable<TripStore["route"]>["legs"] = [];
  if (replan && s.route) {
    // finished stops stay in the timeline (and their legs on the map)
    const keepIds = s.route.order.filter((id) => !route.order.includes(id) && (id === "start" || finished(s, id)));
    order = withStartFirst([...keepIds, ...route.order]);
    kept = s.route.legs.filter((l) => keepIds.includes(l.to));
  }
  const keptLen = kept.reduce((a, l) => a + l.length, 0);
  const stopStatus: Record<string, StopStatus> = { ...s.stopStatus, start: "done" };
  if (status) for (const [id, st] of Object.entries(status)) stopStatus[id] = STOP[st] ?? "pending";
  for (const id of order) stopStatus[id] ??= "pending";
  const moving = status ? Object.entries(status).find(([, v]) => v === "navigating" || v === "arrived")?.[0] : undefined;
  store.setState({
    stops,
    route: { order, legs: [...kept, ...legs], total_m: keptLen + route.total_distance_m },
    baseline_m: keptLen + (route.baseline_distance_m ?? route.total_distance_m),
    baselinePath: route.baseline_waypoints.length ? route.baseline_waypoints.map(pose) : replan ? s.baselinePath : [],
    // same 0.5 m rule as the replay (fake routing reports no detours)
    offRoute: [...new Set([...(replan ? s.offRoute : []), ...route.stops.filter((st) => st.kind === "optional" && (st.detour_m ?? 0) > 0.5).map((st) => st.stop_id)])],
    stopStatus,
    activeStop: current ?? moving ?? s.activeStop,
  });
}

/** Cold load: a replayed (older) route adds the finished stops the snapshot's re-planned route no longer lists. */
function mergeOlderRoute(store: TripApi, route: Route) {
  const s = store.getState();
  if (!s.route) return;
  const have = new Set(s.route.order);
  const missing = route.order.filter((id) => !have.has(id) && finished(s, id));
  if (!missing.length) return;
  ensureProducts(store, route.stops.flatMap((st) => st.skus.map((sku) => ({ sku, name: st.label }))));
  const stops = { ...s.stops };
  for (const st of route.stops) if (missing.includes(st.stop_id)) stops[st.stop_id] ??= stopOf(st, s);
  const legs = route.legs.filter((l) => missing.includes(l.to_stop_id)).map(legOf);
  const add = legs.reduce((a, l) => a + l.length, 0);
  store.setState({
    stops,
    route: { order: withStartFirst([...missing, ...s.route.order]), legs: [...legs, ...s.route.legs], total_m: s.route.total_m + add },
    baseline_m: s.baseline_m + add,
  });
}

function withPurchases(prev: Purchase[], r: Receipt) {
  const add = purchasesFromReceipt(r);
  const tid = add[0]?.trip_id;
  return [...prev.filter((p) => p.trip_id !== tid), ...add];
}

/** robot.state arrives at ≤ 2 Hz on /ws: glide between poses so the map moves like the replay. */
function moveRobot(store: TripApi, to: Pose2D, status: RobotStatus, speed: number) {
  if (tweenTimer) clearInterval(tweenTimer);
  tweenTimer = null;
  const from = store.getState().robot;
  const d = Math.hypot(to.x - from.x, to.y - from.y);
  if (d < 0.01 || d > 3) {
    store.setState({ robot: { x: to.x, y: to.y, theta: to.theta, status, speed } });
    return;
  }
  store.setState({ robot: { ...from, theta: to.theta, status, speed } });
  const t0 = performance.now();
  tweenTimer = setInterval(() => {
    const k = Math.min(1, (performance.now() - t0) / 480);
    store.setState((st) => ({ robot: { ...st.robot, x: from.x + (to.x - from.x) * k, y: from.y + (to.y - from.y) * k } }));
    if (k >= 1 && tweenTimer) {
      clearInterval(tweenTimer);
      tweenTimer = null;
    }
  }, 50);
}

function queueLog(store: TripApi, ev: GatewayEvent) {
  logBuf.push({ seq: ev.seq, t: Date.parse(ev.ts) || Date.now(), type: ev.type, summary: summarize(ev) });
  // the first connect replays the whole trip: batch so that's a handful of renders, not hundreds
  logTimer ??= setTimeout(() => {
    logTimer = null;
    const add = logBuf;
    logBuf = [];
    store.setState((s) => ({ log: [...s.log, ...add].slice(-LOG_CAP) }));
  }, 60);
}

// ------------------------------------------------------------------------------------------------ robot + brain

function bindRobot(store: TripApi, robotId: string) {
  const my = token;
  boundRobot = robotId;
  if (store.getState().robotId !== robotId) store.setState({ robotId });
  useFeed.getState().bindTrip({ tripId: store.getState().tripId ?? "", robotId, gatewayUrl: GATEWAY_URL });
  gw.robot(robotId)
    .then(({ data }) => {
      if (my !== token) return;
      store.setState((s) => ({
        robotName: data.name,
        robot: !gotRobotState && data.pose ? { ...s.robot, x: data.pose.x, y: data.pose.y, theta: data.pose.theta } : s.robot,
      }));
    })
    .catch(() => {});
  void loadSteps(store, robotId, my);
}

/** Thinking-log history comes from REST (never from the replay): goal steps span robots after a reassignment. */
async function loadSteps(store: TripApi, robotId: string, my: number) {
  const { goalId, tripId } = store.getState();
  try {
    const { data } = goalId ? await gw.goalSteps(goalId, 50) : await gw.steps(robotId, { limit: 50 });
    if (my !== token) return;
    addSteps(
      store,
      data.filter((st) => goalId || !st.trip_id || st.trip_id === tripId),
    );
  } catch {
    /* live brain.step events still fill the log */
  }
}

function addSteps(store: TripApi, steps: BrainStep[]) {
  const fresh = steps.filter((st) => !seenSteps.has(st.step_id));
  if (!fresh.length) return;
  for (const st of fresh) seenSteps.add(st.step_id);
  const rows: ThinkStep[] = fresh.map((st) => ({ id: st.step_id, no: st.step_no, kind: st.kind, tool: st.tool, text: st.text, ok: st.ok, t: Date.parse(st.ts) || Date.now(), camera: st.camera_id }));
  store.setState((s) => ({ steps: [...s.steps, ...rows].sort((a, b) => a.t - b.t || a.no - b.no).slice(-STEP_CAP) }));
}

// ------------------------------------------------------------------------------------------------ data drift guards

/** Gateway SKUs this branch's demo data lacks get a placeholder now and the real product in the background. */
function ensureProducts(store: TripApi, hints: ProductHint[], fetchReal = true) {
  let added = false;
  for (const h of hints) {
    const sku = h.sku;
    if (!sku || productBySku[sku]) continue;
    registerProduct(placeholderProduct({ ...h, sku }));
    added = true;
    if (!fetchReal || fetchingProducts.has(sku)) continue;
    fetchingProducts.add(sku);
    gw.product(sku)
      .then(({ data }) => {
        const p = productFromContract(data.product);
        registerProduct(p);
        store.setState((s) => ({
          productsVersion: s.productsVersion + 1,
          stops: Object.fromEntries(Object.entries(s.stops).map(([id, st]) => [id, st.sku === sku && (st.kind === "list" || st.kind === "optional") ? { ...st, label: p.name } : st])),
        }));
      })
      .catch(() => fetchingProducts.delete(sku));
  }
  if (added) store.setState((s) => ({ productsVersion: s.productsVersion + 1 }));
}

/** Stores other than the demo ones (e.g. store_hill) come from GET /api/stores/{id}/planogram. */
function ensureStore(store: TripApi, storeId: string): Promise<boolean> {
  if (STORES[storeId as StoreId]) return Promise.resolve(true);
  let p = fetchingStores.get(storeId);
  if (!p) {
    p = gw
      .planogram(storeId)
      .then(({ data }) => {
        registerStore(data);
        ensureProducts(store, data.slots.map((sl) => ({ sku: sl.sku, price_cents: sl.price_cents })), false);
        return true;
      })
      .catch((e) => {
        fetchingStores.delete(storeId);
        toast.error(`Couldn't load the map for ${storeId}: ${errorText(e)}`);
        return false;
      });
    fetchingStores.set(storeId, p);
  }
  return p;
}
