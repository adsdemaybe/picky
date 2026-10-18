"use client";
// Shared bits for the gateway-only screens (store staff, fleet): a store picker fed by GET /api/stores and the
// "needs the gateway" notice shown when NEXT_PUBLIC_GATEWAY_URL is unset or /health stops answering.
import { useEffect, useState } from "react";
import { Panel } from "@/components/picky";
import { errorText, gw, type S } from "@/lib/gateway/client";
import { useGateway } from "@/lib/gateway/useGateway";
import { cn } from "@/lib/utils";

export function useLiveGateway() {
  const status = useGateway((s) => s.status);
  useEffect(() => useGateway.getState().startPolling(), []);
  return status;
}

export function GatewayNeeded({ status, what }: { status: string; what: string }) {
  return (
    <Panel className="p-6 text-[15px] text-muted-foreground">
      {status === "checking"
        ? "Connecting to the Picky gateway..."
        : status === "down"
          ? `The Picky gateway is not answering right now, so ${what} can't load. It retries every 15 s.`
          : `${what[0].toUpperCase()}${what.slice(1)} come straight from the Picky gateway (live stock, robots and goals). Set NEXT_PUBLIC_GATEWAY_URL to see them; there is no demo copy of this view.`}
    </Panel>
  );
}

/** Physical stores from GET /api/stores; the first one is selected by default. */
export function useStores(live: boolean) {
  const [stores, setStores] = useState<S["StoreInfo"][]>([]);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    if (!live) return;
    let on = true;
    gw.stores()
      .then(({ data }) => on && setStores(data.filter((s) => s.kind === "physical")))
      .catch((e) => on && setErr(errorText(e)));
    return () => {
      on = false;
    };
  }, [live]);
  return { stores, err };
}

export function StorePicker({ stores, value, onChange }: { stores: S["StoreInfo"][]; value: string; onChange: (id: string) => void }) {
  return (
    <div className="flex flex-wrap gap-2" role="tablist" aria-label="Store">
      {stores.map((s) => (
        <button
          key={s.store_id}
          role="tab"
          aria-selected={s.store_id === value}
          onClick={() => onChange(s.store_id)}
          className={cn(
            "cursor-pointer rounded-full px-4 py-2 text-[14px] font-medium ring-1 ring-foreground/10 transition-colors",
            s.store_id === value ? "bg-foreground text-background" : "bg-card hover:bg-muted",
          )}
        >
          {s.name}
        </button>
      ))}
    </div>
  );
}

/** Poll a loader every `ms` while `enabled`; returns the latest data, the last error and a manual refresh. */
export function usePoll<T>(load: (() => Promise<T>) | null, ms: number, deps: unknown[]) {
  const [data, setData] = useState<T | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!load) return;
    let on = true;
    const run = () =>
      load()
        .then((d) => {
          if (!on) return;
          setData(d);
          setErr(null);
        })
        .catch((e) => on && setErr(errorText(e)));
    void run();
    const t = setInterval(() => document.visibilityState === "visible" && void run(), ms);
    return () => {
      on = false;
      clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick, !!load]);
  return { data, err, refresh: () => setTick((n) => n + 1) };
}

export function ago(ts: string | null | undefined) {
  if (!ts) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 1000));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${Math.round(s / 3600)}h ago`;
}
