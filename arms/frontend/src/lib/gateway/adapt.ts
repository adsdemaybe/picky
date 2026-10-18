// Gateway contract shapes → the UI shapes the replay already uses (lib/trip.ts, lib/order.ts). Pure functions.
// Field names on the right-hand side are contract names (events.ts, generated).
import type { Product, Purchase, StoreId } from "@/lib/data";
import { maya, productBySku } from "@/lib/data";
import { fmt } from "@/lib/money";
import type { Draft, ListLine, Rec } from "@/lib/order";
import type { Approval, CartLine, Phase, Receipt, RobotStatus, StopKind, StopStatus } from "@/lib/trip";
import { gatewayHref, type S } from "./client";
import type {
  ApprovalRequest,
  ApprovalResponse,
  Cart,
  GatewayEvent,
  ListItem,
  MandateDecision,
  MandateReason,
  PickChoice,
  Receipt as GwReceipt,
  Recommendation,
  RobotState,
  Stop as GwStop,
  Trip,
  TripState,
} from "./events";

type GwStopStatus = Trip["stop_status"][string];

export const PHASE: Record<TripState, Phase> = {
  intent: "shopping",
  list_ready: "shopping",
  recs_ready: "shopping",
  route_planned: "shopping",
  executing: "shopping",
  awaiting_approval: "shopping",
  awaiting_login: "waiting_login",
  checkout: "checkout",
  paid: "done",
  followup: "done",
  done: "done",
  failed: "done",
};

export const STOP: Record<GwStopStatus, StopStatus> = {
  pending: "pending",
  navigating: "active",
  arrived: "active",
  done: "done",
  skipped: "skipped",
  declined: "declined",
  failed: "declined",
};

export const KIND: Record<GwStop["kind"], StopKind> = { start: "start", mandatory: "list", optional: "optional", end: "end" };

const ROBOT: Record<RobotState["status"], RobotStatus> = {
  idle: "idle",
  navigating: "driving",
  scanning: "scanning",
  picking: "picking",
  placing: "picking",
  charging: "idle",
  error: "idle",
  disconnected: "idle",
};

/** Robot status as the UI shows it: the trip state wins while the robot waits for an answer or is finished. */
export function robotStatusFor(status: RobotState["status"] | null, tripState: string | null): RobotStatus | null {
  if (tripState === "awaiting_approval") return "waiting";
  if (tripState === "paid" || tripState === "followup" || tripState === "done" || tripState === "failed") return "done";
  return status ? (ROBOT[status] ?? "idle") : null;
}

export function productFromContract(p: S["Product"]): Product {
  return {
    sku: p.sku,
    name: p.name,
    brand: p.brand,
    category: p.category,
    subcategory: p.subcategory,
    tags: p.tags,
    dietary: p.dietary,
    price_cents: p.price_cents,
    unit: p.unit,
    size: p.size,
    perishable: p.perishable,
    label_color: p.asset.label_color,
    shape: p.asset.shape,
    dims_m: p.asset.dims_m,
  };
}

/** Stand-in until GET /api/products/{sku} answers (keeps productBySku[x].label_color etc. from crashing). */
export function placeholderProduct(e: { sku: string; name?: string | null; category?: string | null; price_cents?: number | null }): Product {
  return {
    sku: e.sku,
    name: e.name ?? e.sku,
    brand: null,
    category: e.category ?? "pantry",
    subcategory: null,
    tags: [],
    dietary: [],
    price_cents: e.price_cents ?? 0,
    unit: null,
    size: null,
    perishable: false,
    label_color: "#cfd6cf",
    shape: "box",
    dims_m: [0.15, 0.08, 0.2],
  };
}

export function recFromContract(r: Recommendation): Rec {
  const signals: Rec["signals"] = {};
  for (const [k, v] of Object.entries(r.signals)) if (v !== null) signals[k] = typeof v === "boolean" ? (v ? "yes" : "no") : v;
  return {
    rec_id: r.rec_id,
    sku: r.sku,
    unit_cents: r.price_cents ?? productBySku[r.sku]?.price_cents ?? 0,
    original_cents: r.deal?.original_cents ?? undefined,
    interest: r.interest,
    reason: r.reason,
    signals,
    sponsored: r.sponsored,
    detour_m: r.extra_distance_m ?? 0,
    detour_s: r.extra_time_s ?? 0,
  };
}

/** `est_line_cents` is a LINE total: divide by qty only to get a unit price. */
function unitOf(i: ListItem) {
  if (i.est_line_cents != null && i.qty) return Math.round(i.est_line_cents / i.qty);
  return i.sku ? (productBySku[i.sku]?.price_cents ?? 0) : 0;
}

export function lineFromItem(i: ListItem & { sku: string }): ListLine {
  return {
    line_id: i.item_id,
    sku: i.sku,
    qty: i.qty,
    unit_cents: unitOf(i),
    sourcing: i.sourcing === "online" ? "online" : "in_store",
    note: i.notes || undefined,
    vegetarian: i.dietary.includes("vegetarian") || undefined,
  };
}

export function linesFromItems(items: ListItem[]): ListLine[] {
  return items.filter((i): i is ListItem & { sku: string } => !!i.sku).map(lineFromItem);
}

function titleOf(text: string) {
  const t = text.split(/[,.]/)[0].trim();
  return t ? t[0].toUpperCase() + t.slice(1) : "Your order";
}

export function draftFromTrip(trip: Trip, recs: Rec[], title?: string): Draft {
  const intent = trip.intent?.text ?? "";
  return {
    store_id: trip.store_id as StoreId,
    intent,
    advice: [],
    title: trip.shopping_list?.title ?? title ?? titleOf(intent),
    budget_cents: trip.budget_cents ?? trip.shopping_list?.budget_cents ?? maya.profile.default_budget_cents,
    lines: linesFromItems(trip.shopping_list?.items ?? []),
    recs,
  };
}

export function cartFromContract(cart: Cart, verifiedSkus: Set<string>): CartLine[] {
  return cart.lines
    .filter((l) => l.status !== "declined")
    .map((l) => ({
      line_id: l.line_id,
      sku: l.sku,
      qty: l.qty,
      unit_cents: l.unit_price_cents,
      source: l.source,
      verified: l.source === "in_store" && (l.status === "verified" || verifiedSkus.has(l.sku)) ? true : null,
    }));
}

/** The gateway charges delivery only when there are online lines; Cart.total_cents already includes it. */
export function shippingOf(cart: Cart) {
  return cart.online_cents ? cart.shipping_cents : 0;
}

const PROVIDER: Record<GwReceipt["provider"], string> = {
  mock: "Mock receipt (test mode)",
  stripe_test: "Stripe test mode via Visa adapter",
  visa_sandbox: "Visa sandbox",
};

export function receiptFromContract(r: GwReceipt): Receipt {
  return {
    receipt_id: r.receipt_id,
    total_cents: r.amount_cents,
    in_store_cents: r.in_store_cents,
    online_cents: r.online_cents,
    shipping_cents: Math.max(0, r.amount_cents - r.in_store_cents - r.online_cents),
    // the contract's payment token is pm_card_visa (Stripe's test Visa 4242)
    card: "Visa •• 4242",
    provider: PROVIDER[r.provider] ?? r.provider,
    ts: r.created_at,
  };
}

/** Receipt lines → purchase history rows; the trip_maya_1017 prefix keeps the "Today, by robot" badge. */
export function purchasesFromReceipt(r: GwReceipt): Purchase[] {
  return r.lines
    .filter((l) => l.status !== "declined")
    .map((l) => ({
      purchase_id: `pur_gw_${l.line_id}`,
      user_id: r.user_id,
      sku: l.sku,
      qty: l.qty,
      unit_price_cents: l.unit_price_cents,
      store_id: l.store_id,
      trip_id: `trip_maya_1017_${r.trip_id}`,
      ts: r.created_at,
      rating: null,
      synthetic: false,
    }));
}

const BLOCK = new Set<MandateReason>([
  "over_cap",
  "over_item_cap",
  "category_blocked",
  "category_not_allowed",
  "merchant_not_allowed",
  "online_not_allowed",
  "no_mandate",
  "mandate_expired",
  "mandate_revoked",
  "cart_hash_mismatch",
]);

type Reason = { code: string; text: string; level: "ok" | "ask" | "block" };

export function reasonsFromDecision(d: MandateDecision | null, checkout: boolean): Reason[] {
  const out: Reason[] = (d?.reasons ?? [])
    .map((r, i): Reason => ({ code: r.toUpperCase(), text: d?.messages[i] ?? r, level: r === "ok" ? "ok" : BLOCK.has(r) ? "block" : "ask" }))
    .filter((r) => !(checkout && r.code === "CHECKOUT_REQUIRES_APPROVAL"));
  if (checkout) out.push({ code: "CHECKOUT_APPROVAL", text: "Checkout always waits for your explicit approval", level: "ask" });
  return out;
}

function pct(label: string) {
  const m = /(\d+(?:\.\d+)?)\s*%/.exec(label);
  return m ? Number(m[1]) / 100 : null;
}

export function approvalFromContract(r: ApprovalRequest, ctx: { pick?: PickChoice; mandate: MandateDecision | null; recs: Rec[] }): Approval {
  const p = r.payload as { item_id?: string; sponsored?: boolean; over_budget?: boolean; over_budget_cents?: number };
  if (r.kind === "recommendation" && r.sku && !r.multi) {
    const rec = ctx.recs.find((x) => x.rec_id === r.rec_id);
    return {
      id: r.approval_id,
      kind: "detour",
      stop_id: p.item_id ?? `rec:${r.rec_id}`,
      sku: r.sku,
      reason: r.body || r.title,
      unit_cents: r.amount_cents ?? rec?.unit_cents ?? 0,
      sponsored: !!p.sponsored,
      note: p.over_budget && p.over_budget_cents ? `Puts you ${fmt(p.over_budget_cents)} over budget` : undefined,
    };
  }
  if (r.kind === "pick_choice" && r.sku) {
    const cands = ctx.pick?.candidates ?? [];
    // produce review: `multi` + payload.qty (or need) = choose that many units; payload.photos = {option_id: url}
    const extra = r.payload as { qty?: number; need?: number; photos?: Record<string, string> };
    const need = Math.max(1, Math.round(Number(extra.need ?? extra.qty ?? 1)) || 1);
    const photo = (id: string) => {
      const u = extra.photos?.[id];
      return typeof u === "string" && u ? (u.startsWith("/") ? gatewayHref(u) : u) : undefined;
    };
    return {
      multi: r.multi && need > 1,
      need: r.multi ? need : 1,
      id: r.approval_id,
      kind: "pick",
      stop_id: p.item_id ?? "",
      sku: r.sku,
      candidates: r.options.map((o) => {
        const c = cands.find((x) => x.instance_id === o.option_id);
        return {
          instance_id: o.option_id,
          lot_id: o.option_id,
          freshness: c?.score ?? pct(o.label) ?? 0,
          expiry: null,
          note: (o.detail ?? c?.reason ?? o.label).replace(/\s*\([^)]*\)\s*$/, ""),
          thumb: o.thumbnail_b64 ?? c?.thumbnail_b64 ?? undefined,
          photo: photo(o.option_id),
        };
      }),
      recommended: cands.find((c) => c.rank === 1)?.instance_id ?? r.options[0]?.option_id,
    };
  }
  if (r.kind === "checkout") {
    return { id: r.approval_id, kind: "checkout", total_cents: r.amount_cents ?? 0, cart_hash: r.cart_hash ?? "", reasons: reasonsFromDecision(ctx.mandate, true) };
  }
  return {
    id: r.approval_id,
    kind: "question",
    title: r.title,
    body: r.body,
    sku: r.sku ?? undefined,
    // over_threshold titles already quote the price ("Ground Beef ($10.98) puts you …"): don't repeat it
    amount_cents: r.amount_cents != null && !r.title.includes(fmt(r.amount_cents)) ? r.amount_cents : undefined,
    options: r.options.map((o) => ({ id: o.option_id, label: o.label, detail: o.detail ?? undefined })),
    multi: r.multi,
  };
}

const UI_KIND: Record<ApprovalRequest["kind"], string> = {
  recommendation: "detour",
  pick_choice: "pick",
  checkout: "checkout",
  over_threshold: "question",
  substitution: "question",
  online_order: "question",
  question: "question",
  verify_item: "check",
};

/** approvalsLog row (same answer vocabulary as the replay: yes/no, approve/decline, the chosen instance). */
export function logFromResponse(res: ApprovalResponse, req: ApprovalRequest | undefined, kind?: ApprovalRequest["kind"] | null) {
  const k = req?.kind ?? kind ?? "question";
  const answer =
    res.decision === "choice"
      ? (res.choice ?? res.choices[0] ?? "yes")
      : res.decision === "approve"
        ? k === "checkout"
          ? "approve"
          : "yes"
        : k === "checkout"
          ? "decline"
          : "no";
  return { id: res.approval_id, kind: UI_KIND[k] ?? "question", label: req?.title ?? k.replace("_", " "), answer, t: Date.parse(res.ts) || Date.now() };
}

const money = (c: number | null | undefined) => (c == null ? "" : fmt(c));
const name = (sku: string | null | undefined) => (sku ? (productBySku[sku]?.name ?? sku) : "");

/** One-line summary for the "Behind the scenes" event log. */
export function summarize(ev: GatewayEvent): string {
  switch (ev.type) {
    case "trip.created":
    case "trip.snapshot":
      return `${ev.trip.shopping_list?.title ?? ev.trip.intent?.text ?? ev.trip.trip_id}, ${ev.trip.shopping_list?.items.length ?? 0} items${ev.trip.budget_cents != null ? `, budget ${money(ev.trip.budget_cents)}` : ""}`;
    case "trip.state_changed":
      return `${ev.from_state ?? "–"} → ${ev.to_state}${ev.reason ? `, ${ev.reason}` : ""}`;
    case "intent.received":
      return ev.intent.text;
    case "list.ready":
      return `${ev.shopping_list.items.length} items: ${ev.shopping_list.items.map((i) => `${i.qty}× ${i.query}`).join(", ")}`;
    case "sourcing.split":
      return `${ev.in_store.length} in store, ${ev.online.length} online, ${ev.unresolved.length} unresolved`;
    case "recs.ready":
      return `${ev.recs.length} suggestions, ${ev.recs.filter((r) => r.sponsored).length} sponsored`;
    case "route.planned":
      return `${ev.route.stops.filter((s) => s.kind !== "start" && s.kind !== "end").length} stops, ${ev.route.total_distance_m.toFixed(1)} m (${ev.route.solver})${ev.replan ? ", re-planned" : ""}`;
    case "stop.updated":
      return `${ev.stop_id}: ${ev.status}${ev.reason ? `, ${ev.reason}` : ""}`;
    case "robot.arrived":
      return `${ev.stop_id ?? ev.slot_id ?? "stop"}, (${ev.pose.x.toFixed(1)}, ${ev.pose.y.toFixed(1)})`;
    case "scan.result":
      return `${ev.report.slot_id ?? "shelf"}: ${ev.report.observed_count} × ${name(ev.report.expected_sku)}${ev.report.mismatch ? ", mismatch" : ""}`;
    case "pick.choices":
      return `top ${ev.choice.candidates.length} ${name(ev.choice.sku)} by freshness`;
    case "item.picked":
      return `${name(ev.sku)} → basket`;
    case "basket.verified":
      return ev.check.ok ? "basket matches" : `missing ${Object.keys(ev.check.missing).map(name).join(", ") || "items"}`;
    case "cart.updated":
      return `${money(ev.total_cents)}${ev.budget_cents != null ? ` of ${money(ev.budget_cents)}` : ""}`;
    case "approval.requested":
      return ev.request.title;
    case "approval.resolved":
      return `${ev.kind ?? "approval"} → ${ev.response.decision}${ev.response.choice ? ` (${ev.response.choice})` : ""}`;
    case "mandate.decision":
      return `${ev.decision.verdict}: ${ev.decision.reasons.join(", ")}`;
    case "checkout.completed":
      return `${ev.receipt.receipt_id}, ${money(ev.receipt.amount_cents)}, ${ev.receipt.provider} ${ev.receipt.status}`;
    case "followup.sent":
      return ev.message.text;
    case "feedback.received":
      return `${ev.feedback.rating ?? ""}${ev.feedback.rating != null ? "★ " : ""}${name(ev.feedback.sku)} ${ev.feedback.text}`.trim();
    case "profile.updated":
      return ev.update.summary || `${ev.update.changes.length} profile changes`;
    case "goal.created":
      return ev.goal.title;
    case "goal.assigned":
      return `assigned to ${ev.assignment.robot_id} (attempt ${ev.assignment.attempt})`;
    case "goal.updated":
      return `${ev.goal.status}${ev.note || ev.goal.progress_note ? `: ${ev.note || ev.goal.progress_note}` : ""}`;
    case "goal.completed":
      return `${ev.goal.status}${ev.goal.summary ? `, ${ev.goal.summary}` : ""}`;
    case "goal.failed":
      return ev.reason;
    case "error":
      return `${ev.arm}: ${ev.message}`;
    case "agent.message":
      return ev.text;
    default: {
      // events newer than this build's contracts (payment.agent_step, …): their own title / detail
      const raw = ev as unknown as { step?: { title?: string; detail?: string; kind?: string }; title?: string; detail?: string };
      const st = raw.step ?? raw;
      return [st.title, st.detail].filter(Boolean).join(": ");
    }
  }
}
