"use client";
// Typed REST client for the Picky gateway (contracts/api.md). The browser talks to FastAPI directly (the gateway
// allows CORS from any origin): no Next proxy, no Server Actions. Types are generated (api-types.ts / events.ts).
import type { components } from "./api-types";
import type { ApprovalResponse, BrainStep, ProfileUpdate, RequestPlan, RobotInfo, Trip } from "./events";

export type S = components["schemas"];

/** Empty = demo data only (no gateway mode). */
export const GATEWAY_URL = (process.env.NEXT_PUBLIC_GATEWAY_URL ?? "").trim().replace(/\/$/, "");

export class GatewayError extends Error {
  constructor(
    public status: number,
    public detail: string,
  ) {
    super(detail);
    this.name = "GatewayError";
  }
}

const enc = encodeURIComponent;

function qs(q: Record<string, string | number | null | undefined>) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** fetch JSON with a timeout; non-2xx → GatewayError(status, body.detail); network/timeout → GatewayError(0, …). */
async function req<T>(path: string, init: { method?: string; body?: unknown } = {}, timeoutMs = 15_000): Promise<{ data: T; seq: number | null }> {
  if (!GATEWAY_URL) throw new GatewayError(0, "NEXT_PUBLIC_GATEWAY_URL is not set");
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(GATEWAY_URL + path, {
      method: init.method ?? "GET",
      headers: init.body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: init.body !== undefined ? JSON.stringify(init.body) : undefined,
      signal: ctl.signal,
      cache: "no-store",
    });
    if (!r.ok) {
      let detail = r.statusText || `HTTP ${r.status}`;
      try {
        const b = await r.json();
        if (b?.detail) detail = typeof b.detail === "string" ? b.detail : JSON.stringify(b.detail);
      } catch {
        /* not JSON */
      }
      throw new GatewayError(r.status, detail);
    }
    const seq = r.headers.get("X-Event-Seq");
    return { data: (await r.json()) as T, seq: seq ? Number(seq) : null };
  } catch (e) {
    if (e instanceof GatewayError) throw e;
    throw new GatewayError(0, ctl.signal.aborted ? `The gateway didn't answer within ${timeoutMs / 1000} s` : String((e as Error)?.message ?? e));
  } finally {
    clearTimeout(timer);
  }
}

export const gw = {
  health: () => req<S["HealthStatus"]>("/health", undefined, 2500),
  createRequest: (b: S["FleetRequest"]) => req<RequestPlan>("/api/requests", { method: "POST", body: b }, 30_000),
  trip: (id: string) => req<Trip>(`/api/trips/${enc(id)}`),
  answer: (id: string, b: S["ApprovalDecisionRequest"]) => req<ApprovalResponse>(`/api/approvals/${enc(id)}`, { method: "POST", body: b }),
  /** start (or restart) checkout; the checkout approval arrives as approval.requested */
  checkout: (tripId: string) => req<S["CheckoutStatus"]>(`/api/trips/${enc(tripId)}/checkout`, { method: "POST" }),
  robot: (id: string) => req<RobotInfo>(`/api/fleet/robots/${enc(id)}`),
  /** newest first */
  steps: (robotId: string, q: { goal_id?: string | null; limit?: number } = {}) =>
    req<BrainStep[]>(`/api/fleet/robots/${enc(robotId)}/steps${qs({ goal_id: q.goal_id, limit: q.limit ?? 50 })}`),
  /** newest first; spans robots after a reassignment */
  goalSteps: (goalId: string, limit = 50) => req<BrainStep[]>(`/api/goals/${enc(goalId)}/steps${qs({ limit })}`),
  stopRobot: (id: string) => req<RobotInfo>(`/api/fleet/robots/${enc(id)}/stop`, { method: "POST", body: {} }),
  planogram: (storeId: string) => req<S["Planogram"]>(`/api/stores/${enc(storeId)}/planogram`),
  product: (sku: string) => req<S["SearchHit"]>(`/api/products/${enc(sku)}`),
  feedback: (userId: string, b: S["Feedback"]) => req<ProfileUpdate>(`/api/users/${enc(userId)}/feedback`, { method: "POST", body: b }),
  mandate: (userId: string) => req<S["IntentMandate-Output"]>(`/api/users/${enc(userId)}/mandate`),
  putMandate: (userId: string, m: S["IntentMandate-Input"]) => req<S["IntentMandate-Output"]>(`/api/users/${enc(userId)}/mandate`, { method: "PUT", body: m }),
  profile: (userId: string) => req<S["UserProfile-Output"]>(`/api/users/${enc(userId)}/profile`),
  putProfile: (userId: string, p: S["UserProfile-Input"]) => req<S["UserProfile-Output"]>(`/api/users/${enc(userId)}/profile`, { method: "PUT", body: p }),
  /** CameraInfo rows; `source` = who renders it right now ("isaac" | "fake" | "none"). */
  cameras: (q: { robot_id?: string; store_id?: string }) => req<{ camera_id: string; source?: string; online?: boolean; kind?: string; name?: string }[]>(`/api/cameras${qs(q)}`, undefined, 5000),
  /** Purchase history, newest first (a bare list, or {purchases: [...]} / {items: [...]}). */
  purchases: (userId: string, limit = 60) => req<PurchaseRow[] | { purchases?: PurchaseRow[]; items?: PurchaseRow[] }>(`/api/users/${enc(userId)}/purchases${qs({ limit })}`, undefined, 5000),
  /** "Running low" restock suggestions from purchase cadence (most due first). */
  // the LLM re-rank takes 5-8 s when the gateway's cache is cold (it bounds it at 8 s, then rule order)
  suggestions: (userId: string) => req<Suggestion[]>(`/api/users/${enc(userId)}/suggestions`, undefined, 15_000),
};

/** GET /api/users/{id}/suggestions row. Hand-typed until the endpoint lands in schemas/openapi.json (pnpm gen-types). */
export type Suggestion = {
  sku: string;
  name: string;
  price_cents: number;
  last_bought_days: number | null;
  cadence_days: number | null;
  reason: string;
  store_id: string | null;
  in_stock: boolean;
  /** "Aisle 3 - International & Mexican, Canned · shelf 2" */
  aisle_label?: string | null;
  /** older shape: an aisle label or a slot-ish object */
  location?: unknown;
  /** root-relative on the gateway (/api/products/{sku}/image.jpg) or absolute */
  image_url?: string | null;
  suggested_qty?: number | null;
  qty?: number | null;
  label_color?: string | null;
  category?: string | null;
  /** paid placement must be labeled (CLAUDE.md §8); replenish suggestions never are */
  sponsored?: boolean;
  overdue_days?: number | null;
  times_bought?: number | null;
  /** an AI-written "why" when the recommender adds one (else `reason` is the cadence sentence) */
  why?: string | null;
  ai_reason?: string | null;
  /** why, as numbers (transparency): last_bought_days, cadence_days, overdue_days, times_bought, source, … */
  signals?: Record<string, number | string | boolean> | null;
  /** 0.2.3 generators: replenish (due by your cadence) | substitute | complement | deal */
  kind?: "replenish" | "substitute" | "complement" | "deal" | string;
  /** "llm" = order + reason from the LLM re-rank (grounded in `signals`), else the deterministic rules */
  ranked_by?: "rules" | "llm" | string;
  substitute_for?: string | null;
  related_to?: string | null;
  /** "20% off" / "was $4.99" */
  deal_text?: string | null;
  stock_qty?: number | null;
};

/** GET /api/users/{id}/purchases row: contracts.profile.PurchaseRecord, optionally with display fields. */
export type PurchaseRow = {
  purchase_id: string;
  user_id?: string;
  sku: string;
  qty: number;
  unit_price_cents: number;
  store_id: string;
  trip_id?: string | null;
  ts: string;
  rating?: number | null;
  name?: string | null;
  category?: string | null;
  image_url?: string | null;
  label_color?: string | null;
  /** 0.2.3: root-relative photos of the exact units bought (the robot's verification pictures) */
  photo_urls?: string[];
  /** 0.2.3: "seed" = demo history, "checkout" = bought through Picky */
  source?: string;
};

/** Gateway paths ("/api/...") → absolute URLs; absolute URLs pass through. */
export function gatewayHref(path: string, base = GATEWAY_URL) {
  return /^https?:\/\//.test(path) || !base ? path : `${base.replace(/\/$/, "")}${path.startsWith("/") ? "" : "/"}${path}`;
}

/** Camera URLs serve face-blurred frames only (the gateway blurs before serving). MJPEG works as <img src>. */
export function cameraUrl(cameraId: string, kind: "stream" | "snapshot", q: { w?: number; fps?: number; r?: number } = {}, base = GATEWAY_URL) {
  return `${base.replace(/\/$/, "")}/api/cameras/${enc(cameraId)}/${kind === "stream" ? "stream.mjpg" : "snapshot.jpg"}${qs(q)}`;
}

export function wsUrl(path: string, params: [string, string][], base = GATEWAY_URL) {
  const p = new URLSearchParams(params).toString();
  return `${base.replace(/\/$/, "").replace(/^http/, "ws")}${path}${p ? `?${p}` : ""}`;
}

/** Product photo served by the gateway (the sim's label texture / catalog image). */
export function productImageUrl(sku: string, base = GATEWAY_URL) {
  return base ? `${base.replace(/\/$/, "")}/api/products/${enc(sku)}/image.jpg` : "";
}

/** UI camera names → gateway camera ids (`rover-ave-1.front`, `.wrist`, `.basket`). */
export function robotCameraId(robotId: string, cam: "head" | "wrist" | "basket") {
  return `${robotId}.${cam === "head" ? "front" : cam}`;
}

export function errorText(e: unknown) {
  return e instanceof GatewayError ? e.detail : String((e as Error)?.message ?? e);
}
