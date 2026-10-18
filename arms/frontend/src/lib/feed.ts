"use client";
// Live feed connection layer: one store the UI reads, three interchangeable sources.
//
//   sim      the in-browser trip replay draws every camera (works with no backend)
//   gateway  arms/gateway  WS /ws/frames?trip_id=<id>  (contracts/api.md)
//              ← {"type":"frame","frame":{camera,jpeg_b64,w,h,blurred,pose?,slot_id?,ts}}   only blurred=true is drawn
//              ← {"type":"robot.state","state":{pose:{x,y,theta},status,holding,basket_skus,error,ts}}
//            plus GET /health → {status, contracts_version, real_arms, sim_connected}
//   stream   one direct URL per camera (MJPEG renders in <img>, MP4/WebM/HLS-in-Safari in <video>)
//
// Config persists per browser (localStorage) and can be preset with NEXT_PUBLIC_GATEWAY_URL / NEXT_PUBLIC_STREAM_URL.
import { create } from "zustand";
import type { Pose } from "./data";

export const CAMERAS = ["head", "wrist", "basket", "overview"] as const;
export type Camera = (typeof CAMERAS)[number];
export const CAMERA_LABEL: Record<Camera, string> = {
  head: "Head camera",
  wrist: "Wrist camera",
  basket: "Basket camera",
  overview: "Overhead",
};
export const CAMERA_HELP: Record<Camera, string> = {
  head: "What the robot sees as it drives and scans shelves",
  wrist: "Close-up from the gripper while it picks",
  basket: "Looks down into the basket to check every item",
  overview: "Top-down view of the store with the route",
};

export type Mode = "sim" | "gateway" | "stream";
export type Conn = "idle" | "connecting" | "live" | "offline" | "error";

export type Frame = { src: string; w: number; h: number; blurred: boolean; ts: number };
export type Telemetry = {
  pose: Pose;
  status: string;
  holding: string | null;
  basket_skus: string[];
  error: string | null;
  ts: number;
};
export type Health = { status: string; contracts_version: string; real_arms: string[]; sim_connected: boolean } | null;
export type FeedLog = { t: number; level: "info" | "warn" | "error"; text: string };

type Config = { mode: Mode; gatewayUrl: string; tripId: string; streams: Record<Camera, string> };

type FeedStore = Config & {
  conn: Conn;
  frames: Partial<Record<Camera, Frame>>;
  telemetry: Telemetry | null;
  health: Health;
  stats: { fps: number; frames: number; lastFrameAt: number | null; latencyMs: number | null };
  log: FeedLog[];
  /** Pull saved config from this browser (call once on mount; kept out of module init to avoid hydration mismatches). */
  hydrate: () => void;
  setConfig: (c: Partial<Config>) => void;
  setStream: (cam: Camera, url: string) => void;
  connect: () => void;
  disconnect: () => void;
};

const KEY = "picky.feed.v1";
const envGateway = process.env.NEXT_PUBLIC_GATEWAY_URL ?? "";
const envStream = process.env.NEXT_PUBLIC_STREAM_URL ?? "";

function defaults(): Config {
  return {
    mode: envStream ? "stream" : "sim",
    gatewayUrl: envGateway || "http://localhost:8000",
    tripId: "",
    streams: { head: envStream, wrist: "", basket: "", overview: "" },
  };
}
function loadSaved(): Partial<Config> {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return {};
    const saved = JSON.parse(raw) as Partial<Config>;
    return { ...saved, streams: { ...defaults().streams, ...saved.streams } };
  } catch {
    return {};
  }
}
function saveConfig(c: Config) {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(c));
  } catch {
    /* storage unavailable: config lives for this tab only */
  }
}

let ws: WebSocket | null = null;
let retry: ReturnType<typeof setTimeout> | null = null;
let attempts = 0;
let frameTimes: number[] = [];
let wanted = false;

export const useFeed = create<FeedStore>((set, get) => {
  const log = (level: FeedLog["level"], text: string) => set((s) => ({ log: [...s.log.slice(-59), { t: Date.now(), level, text }] }));
  const cfg = () => {
    const { mode, gatewayUrl, tripId, streams } = get();
    return { mode, gatewayUrl, tripId, streams };
  };

  const onFrame = (camera: Camera, f: Frame) => {
    const now = Date.now();
    frameTimes = [...frameTimes.filter((t) => now - t < 2000), now];
    set((s) => ({
      conn: "live",
      frames: { ...s.frames, [camera]: f },
      stats: { fps: frameTimes.length / 2, frames: s.stats.frames + 1, lastFrameAt: now, latencyMs: f.ts ? Math.max(0, now - f.ts) : s.stats.latencyMs },
    }));
  };

  const openGateway = () => {
    const { gatewayUrl, tripId } = get();
    const httpBase = gatewayUrl.replace(/\/$/, "");
    const wsUrl = `${httpBase.replace(/^http/, "ws")}/ws/frames${tripId ? `?trip_id=${encodeURIComponent(tripId)}` : ""}`;
    set({ conn: "connecting" });
    log("info", `Connecting to ${wsUrl}`);

    fetch(`${httpBase}/health`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((h) => {
        set({ health: h });
        log("info", `Gateway ${h.status}, contracts ${h.contracts_version}, sim ${h.sim_connected ? "connected" : "not connected"}`);
      })
      .catch((e) => log("warn", `Health check failed: ${String(e.message ?? e)}`));

    try {
      ws = new WebSocket(wsUrl);
    } catch (e) {
      set({ conn: "error" });
      log("error", `Bad gateway URL: ${String(e)}`);
      return;
    }
    ws.onopen = () => {
      attempts = 0;
      set({ conn: "connecting" });
      log("info", "Socket open, waiting for frames");
    };
    ws.onmessage = (m) => {
      let ev: { type?: string; frame?: Record<string, unknown>; state?: Record<string, unknown> };
      try {
        ev = JSON.parse(m.data);
      } catch {
        return;
      }
      if (ev.type === "frame" && ev.frame) {
        const f = ev.frame as { camera?: Camera; jpeg_b64?: string; w?: number; h?: number; blurred?: boolean; ts?: string };
        if (f.blurred !== true) {
          log("warn", "Dropped a frame that was not face-blurred");
          return;
        }
        onFrame(f.camera ?? "head", {
          src: `data:image/jpeg;base64,${f.jpeg_b64}`,
          w: f.w ?? 640,
          h: f.h ?? 360,
          blurred: true,
          ts: f.ts ? Date.parse(f.ts) : 0,
        });
      } else if (ev.type === "robot.state" && ev.state) {
        const st = ev.state as { pose: Pose; status: string; holding?: string | null; basket_skus?: string[]; error?: string | null; ts?: string };
        set({
          conn: get().conn === "connecting" ? "live" : get().conn,
          telemetry: {
            pose: st.pose,
            status: st.status,
            holding: st.holding ?? null,
            basket_skus: st.basket_skus ?? [],
            error: st.error ?? null,
            ts: st.ts ? Date.parse(st.ts) : Date.now(),
          },
        });
      }
    };
    ws.onerror = () => log("error", "Socket error");
    ws.onclose = () => {
      ws = null;
      if (!wanted) return;
      set({ conn: "offline" });
      const delay = Math.min(10_000, 800 * 2 ** attempts++);
      log("warn", `Disconnected. Retrying in ${(delay / 1000).toFixed(1)} s`);
      retry = setTimeout(openGateway, delay);
    };
  };

  return {
    ...defaults(),
    conn: "idle",
    frames: {},
    telemetry: null,
    health: null,
    stats: { fps: 0, frames: 0, lastFrameAt: null, latencyMs: null },
    log: [],

    hydrate: () => set(loadSaved()),
    setConfig: (c) => {
      set(c);
      saveConfig(cfg());
    },
    setStream: (cam, url) => {
      set((s) => ({ streams: { ...s.streams, [cam]: url } }));
      saveConfig(cfg());
    },

    connect: () => {
      get().disconnect();
      wanted = true;
      const { mode } = get();
      if (mode === "gateway") openGateway();
      else if (mode === "stream") {
        set({ conn: "live" });
        log("info", "Using direct stream URLs");
      } else {
        set({ conn: "live" });
        log("info", "Simulated cameras running from the in-browser trip");
      }
    },

    disconnect: () => {
      wanted = false;
      if (retry) clearTimeout(retry);
      retry = null;
      attempts = 0;
      frameTimes = [];
      if (ws) {
        ws.onclose = null;
        ws.close();
        ws = null;
      }
      set({ conn: "idle", frames: {}, stats: { fps: 0, frames: 0, lastFrameAt: null, latencyMs: null } });
    },
  };
});
