"use client";
// WS /ws with filters (contracts/api.md): reconnect with backoff, keepalive ping, and `after_seq` resume.
// Server → client only: approvals are answered over REST so the UI gets status codes (404 / 409).
import { wsUrl } from "./client";
import type { GatewayEvent } from "./events";

export type WsFilter = [key: "trip_id" | "robot_id" | "store_id" | "user_id" | "request_id", value: string];

export function openEventSocket(opts: {
  filters: WsFilter[];
  /** null on the first connect (trip sockets then replay the trip's ring events after the snapshot); lastSeq after. */
  afterSeq: () => number | null;
  onEvent: (ev: GatewayEvent) => void;
  onStatus?: (s: "connecting" | "live" | "reconnecting") => void;
  onOpen?: () => void;
}): () => void {
  let ws: WebSocket | null = null;
  let closed = false;
  let attempts = 0;
  let retry: ReturnType<typeof setTimeout> | null = null;
  let ping: ReturnType<typeof setInterval> | null = null;

  const schedule = () => {
    if (closed) return;
    opts.onStatus?.("reconnecting");
    const delay = Math.min(10_000, 800 * 2 ** attempts++);
    retry = setTimeout(connect, delay);
  };

  function connect() {
    if (closed) return;
    const after = opts.afterSeq();
    const params: [string, string][] = [...opts.filters];
    if (after !== null) params.push(["after_seq", String(after)]);
    opts.onStatus?.(attempts ? "reconnecting" : "connecting");
    try {
      ws = new WebSocket(wsUrl("/ws", params));
    } catch {
      schedule();
      return;
    }
    ws.onopen = () => {
      attempts = 0;
      opts.onStatus?.("live");
      opts.onOpen?.();
      ping = setInterval(() => {
        if (ws?.readyState === WebSocket.OPEN) ws.send('{"type":"ping"}');
      }, 20_000);
    };
    ws.onmessage = (m) => {
      let ev: GatewayEvent;
      try {
        ev = JSON.parse(m.data);
      } catch {
        return;
      }
      if (ev && typeof ev.type === "string") opts.onEvent(ev);
    };
    ws.onclose = () => {
      if (ping) clearInterval(ping);
      ping = null;
      ws = null;
      schedule();
    };
    ws.onerror = () => {
      /* onclose follows and schedules the retry */
    };
  }

  connect();
  return () => {
    closed = true;
    if (retry) clearTimeout(retry);
    if (ping) clearInterval(ping);
    if (ws) {
      ws.onclose = null;
      ws.close();
      ws = null;
    }
  };
}
