"use client";
// Gateway mode bits: the TopBar chip (Live vs Demo data) and the ?trip= binder for /trip and /live.
import { useSearchParams } from "next/navigation";
import { useEffect } from "react";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { GATEWAY_URL } from "@/lib/gateway/client";
import { isReconnecting, useGateway } from "@/lib/gateway/useGateway";
import { useTrip } from "@/lib/trip";

export function GatewayChip() {
  const { status, socket, health, url } = useGateway();
  const following = useTrip((s) => s.source === "gateway");
  useEffect(() => useGateway.getState().startPolling(), []);

  const reconnecting = following && isReconnecting({ socket, status });
  const live = status === "live";
  const tip = live
    ? `Picky gateway ${url} · contracts ${health?.contracts_version ?? "?"} · ${health?.sim_connected ? "Isaac sim connected" : "2D robot fake"} · real arms: ${health?.real_arms.length ? health.real_arms.join(", ") : "none (all fakes)"}`
    : status === "off"
      ? "No gateway configured (NEXT_PUBLIC_GATEWAY_URL). Everything runs on demo data."
      : status === "checking"
        ? `Looking for the Picky gateway at ${url}`
        : `No gateway reachable at ${url}. Everything runs on demo data.`;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="ml-1 hidden md:inline-flex" tabIndex={0} role="status" aria-label={tip}>
          {reconnecting ? (
            <Badge variant="amber">
              <span className="live-dot size-1.5 rounded-full bg-current" /> Reconnecting
            </Badge>
          ) : live ? (
            <Badge variant="leaf">
              <span className="live-dot size-1.5 rounded-full bg-current" /> Live
            </Badge>
          ) : (
            <Badge variant="muted">{status === "checking" ? "Connecting" : "Demo data"}</Badge>
          )}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-[320px]">{tip}</TooltipContent>
    </Tooltip>
  );
}

/** `/trip?trip=<id>` (and /live) follow that gateway trip, e.g. after a reload or from a shared link.
 *  Render inside <Suspense fallback={null}> (useSearchParams). */
export function BindTripFromUrl() {
  const params = useSearchParams();
  const tripId = params.get("trip");
  const requestId = params.get("request");
  const mode = params.get("mode") === "with_me" ? "with_me" : "pickup";
  useEffect(() => {
    if (!tripId || !GATEWAY_URL || useTrip.getState().tripId === tripId) return;
    useTrip.getState().followGateway({ tripId, requestId, mode });
  }, [tripId, requestId, mode]);
  return null;
}
