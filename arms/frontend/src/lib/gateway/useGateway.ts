"use client";
// Which mode is live: gateway mode = NEXT_PUBLIC_GATEWAY_URL is set AND GET /health answers (2.5 s timeout).
// Otherwise every screen keeps running on the demo data + in-browser replay (the fallback ladder).
// Re-checked every 15 s while the tab is visible and on window focus.
import { create } from "zustand";
import { GATEWAY_URL, gw, type S } from "./client";

export type GatewayStatus = "off" | "checking" | "live" | "down";
export type SocketStatus = "idle" | "connecting" | "live" | "reconnecting";

type GatewayStore = {
  url: string;
  status: GatewayStatus;
  health: S["HealthStatus"] | null;
  checkedAt: number | null;
  /** The trip socket (/ws?trip_id=) while a gateway trip is followed. */
  socket: SocketStatus;
  /** url "" → "off"; else GET /health → "live" | "down". Resolves true when live. */
  check: () => Promise<boolean>;
  /** Idempotent: check now, then every 15 s while visible, and on focus. */
  startPolling: () => void;
  setSocket: (s: SocketStatus) => void;
};

let polling = false;
let inflight: Promise<boolean> | null = null;

export const useGateway = create<GatewayStore>((set, get) => ({
  url: GATEWAY_URL,
  status: GATEWAY_URL ? "checking" : "off",
  health: null,
  checkedAt: null,
  socket: "idle",

  check: () => {
    if (!GATEWAY_URL) {
      set({ status: "off" });
      return Promise.resolve(false);
    }
    inflight ??= gw
      .health()
      .then(({ data }) => {
        set({ status: "live", health: data, checkedAt: Date.now() });
        return true;
      })
      .catch(() => {
        set({ status: "down", checkedAt: Date.now() });
        return false;
      })
      .finally(() => {
        inflight = null;
      });
    return inflight;
  },

  startPolling: () => {
    if (polling || typeof window === "undefined") return;
    polling = true;
    void get().check();
    if (!GATEWAY_URL) return;
    setInterval(() => {
      if (document.visibilityState === "visible") void get().check();
    }, 15_000);
    window.addEventListener("focus", () => void get().check());
  },

  setSocket: (socket) => {
    if (get().socket !== socket) set({ socket });
  },
}));

export const isLive = () => useGateway.getState().status === "live";
