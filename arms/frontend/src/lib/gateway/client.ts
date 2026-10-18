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
  /** talk to one robot's brain (its steps also stream as brain.step) */
  robotMessage: (id: string, b: S["RobotMessageRequest"]) => req<S["BrainReply"]>(`/api/fleet/robots/${enc(id)}/messages`, { method: "POST", body: b }, 30_000),
  /** a manual goal pinned to that robot */
  robotGoal: (id: string, b: S["CreateGoalRequest"]) => req<S["Goal"]>(`/api/fleet/robots/${enc(id)}/goals`, { method: "POST", body: b }),
  goal: (id: string) => req<S["Goal"]>(`/api/goals/${enc(id)}`),
  /** newest first */
  goals: (q: { robot_id?: string; store_id?: string; trip_id?: string; kind?: string; limit?: number }) => req<S["Goal"][]>(`/api/goals${qs(q)}`),
  /** pending approvals (filters AND together) */
  approvals: (q: { trip_id?: string; robot_id?: string; goal_id?: string; store_id?: string; user_id?: string }) => req<S["ApprovalRequest"][]>(`/api/approvals${qs(q)}`),
  search: (q: string, storeId?: string, limit = 8) => req<S["SearchHit"][]>(`/api/products/search${qs({ q, store_id: storeId, limit })}`),
  planogram: (storeId: string) => req<S["Planogram"]>(`/api/stores/${enc(storeId)}/planogram`),
  product: (sku: string) => req<S["SearchHit"]>(`/api/products/${enc(sku)}`),
  feedback: (userId: string, b: S["Feedback"]) => req<ProfileUpdate>(`/api/users/${enc(userId)}/feedback`, { method: "POST", body: b }),
  mandate: (userId: string) => req<S["IntentMandate-Output"]>(`/api/users/${enc(userId)}/mandate`),
  putMandate: (userId: string, m: S["IntentMandate-Input"]) => req<S["IntentMandate-Output"]>(`/api/users/${enc(userId)}/mandate`, { method: "PUT", body: m }),
  profile: (userId: string) => req<S["UserProfile-Output"]>(`/api/users/${enc(userId)}/profile`),
  putProfile: (userId: string, p: S["UserProfile-Input"]) => req<S["UserProfile-Output"]>(`/api/users/${enc(userId)}/profile`, { method: "PUT", body: p }),
};

/** Camera URLs serve face-blurred frames only (the gateway blurs before serving). MJPEG works as <img src>. */
export function cameraUrl(cameraId: string, kind: "stream" | "snapshot", q: { w?: number; fps?: number; r?: number } = {}, base = GATEWAY_URL) {
  return `${base.replace(/\/$/, "")}/api/cameras/${enc(cameraId)}/${kind === "stream" ? "stream.mjpg" : "snapshot.jpg"}${qs(q)}`;
}

export function wsUrl(path: string, params: [string, string][], base = GATEWAY_URL) {
  const p = new URLSearchParams(params).toString();
  return `${base.replace(/\/$/, "").replace(/^http/, "ws")}${path}${p ? `?${p}` : ""}`;
}

/** UI camera names → gateway camera ids (`rover-ave-1.front`, `.wrist`, `.basket`). */
export function robotCameraId(robotId: string, cam: "head" | "wrist" | "basket") {
  return `${robotId}.${cam === "head" ? "front" : cam}`;
}

export function errorText(e: unknown) {
  return e instanceof GatewayError ? e.detail : String((e as Error)?.message ?? e);
}
