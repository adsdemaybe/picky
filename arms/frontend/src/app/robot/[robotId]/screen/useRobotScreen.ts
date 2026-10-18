"use client";
// Live data for the robot's own screen, all from the gateway (contracts/api.md):
//   GET /api/fleet/robots/{id}          header (status, battery, availability, last thought)
//   GET /api/stores/{store}/planogram   store name + mini map
//   GET /api/fleet/robots/{id}/steps    "what I'm doing" history (newest first)
//   GET /api/goals?robot_id=&kind=shop  -> the robot's current / latest shop trip -> GET /api/trips/{id}
//   GET /api/approvals?robot_id= | ?trip_id=   pending questions + the checkout approval
//   WS  /ws?robot_id=                   robot.state, fleet.robot_updated, brain.step, approval.*, cart.updated, ...
// REST is polled every 4 s as a safety net (the robot socket only carries live events) and refetched on events.
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { GatewayError, errorText, gw, type S } from "@/lib/gateway/client";
import type { ApprovalRequest, BrainStep, GatewayEvent, RobotInfo, Trip } from "@/lib/gateway/events";
import { openEventSocket } from "@/lib/gateway/eventSocket";

export type Conn = "connecting" | "live" | "reconnecting" | "offline";

const POLL_MS = 4000;
/** A trip the robot is no longer running (paid, failed, basket held) stays on screen this long, then the screen goes back to idle. */
const RECENT_TRIP_MS = 10 * 60_000;
const REFETCH_ON = new Set([
  "approval.requested",
  "approval.resolved",
  "cart.updated",
  "checkout.completed",
  "mandate.decision",
  "trip.state_changed",
  "goal.created",
  "goal.assigned",
  "goal.updated",
  "goal.completed",
  "goal.failed",
]);

function subscribeOnline(cb: () => void) {
  window.addEventListener("online", cb);
  window.addEventListener("offline", cb);
  return () => {
    window.removeEventListener("online", cb);
    window.removeEventListener("offline", cb);
  };
}
const useOnline = () => useSyncExternalStore(subscribeOnline, () => navigator.onLine, () => true);

export function useRobotScreen(robotId: string) {
  const [robot, setRobot] = useState<RobotInfo | null>(null);
  /** 404 = no such robot; anything else = can't reach the gateway right now */
  const [robotError, setRobotError] = useState<{ notFound: boolean; text: string } | null>(null);
  const [planogram, setPlanogram] = useState<S["Planogram"] | null>(null);
  const [steps, setSteps] = useState<BrainStep[]>([]);
  const [trip, setTrip] = useState<Trip | null>(null);
  const [approvals, setApprovals] = useState<ApprovalRequest[]>([]);
  const [socket, setSocket] = useState<"connecting" | "live" | "reconnecting">("connecting");
  const online = useOnline();
  const storeId = robot?.store_id ?? null;
  const inflight = useRef(false);

  const refresh = useCallback(async () => {
    if (inflight.current) return;
    inflight.current = true;
    try {
      const { data: r } = await gw.robot(robotId);
      setRobot(r);
      setRobotError(null);

      // the robot's current (or latest) shop goal carries the shopper's trip
      let tripId: string | null = null;
      let current = false;
      if (r.current_goal_id) {
        const { data: g } = await gw.goal(r.current_goal_id).catch(() => ({ data: null }));
        tripId = g?.trip_id ?? null;
        current = !!tripId;
      }
      if (!tripId) {
        const { data: gs } = await gw.goals({ robot_id: robotId, kind: "shop", limit: 1 }).catch(() => ({ data: [] as S["Goal-Output"][] }));
        tripId = gs[0]?.trip_id ?? null;
      }
      let t: Trip | null = null;
      if (tripId) {
        t = (await gw.trip(tripId).catch(() => ({ data: null }))).data;
        // the robot's running trip always shows; a finished or parked one only while it's recent
        if (t && !current && Date.now() - Date.parse(t.updated_at) > RECENT_TRIP_MS) t = null;
      }
      setTrip(t);

      const [byRobot, byTrip] = await Promise.all([
        gw.approvals({ robot_id: robotId }).then((x) => x.data),
        t ? gw.approvals({ trip_id: t.trip_id }).then((x) => x.data) : Promise.resolve([] as ApprovalRequest[]),
      ]);
      const seen = new Set<string>();
      setApprovals([...byTrip, ...byRobot].filter((a) => !seen.has(a.approval_id) && (seen.add(a.approval_id), true)));
    } catch (e) {
      setRobotError({ notFound: e instanceof GatewayError && e.status === 404, text: errorText(e) });
    } finally {
      inflight.current = false;
    }
  }, [robotId]);

  // debounced refetch for bursts of events
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const soon = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => void refresh(), 250);
  }, [refresh]);

  // first load + polling while visible
  useEffect(() => {
    const first = setTimeout(() => void refresh(), 0);
    const id = setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, POLL_MS);
    return () => {
      clearTimeout(first);
      clearInterval(id);
      if (timer.current) clearTimeout(timer.current);
    };
  }, [refresh]);

  // thinking history (once per robot; brain.step keeps it live)
  useEffect(() => {
    let alive = true;
    gw.steps(robotId, { limit: 30 })
      .then(({ data }) => alive && setSteps(data))
      .catch(() => {
        /* the header's error state covers an unreachable gateway */
      });
    return () => {
      alive = false;
    };
  }, [robotId]);

  // store layout + name
  useEffect(() => {
    if (!storeId) return;
    let alive = true;
    gw.planogram(storeId)
      .then(({ data }) => alive && setPlanogram(data))
      .catch(() => alive && setPlanogram(null));
    return () => {
      alive = false;
    };
  }, [storeId]);

  // live events for this robot
  useEffect(() => {
    let close: (() => void) | null = null;
    // deferred a tick: dev StrictMode mounts effects twice and would open + abort a socket right away
    const t = setTimeout(() => {
      close = openEventSocket({
        filters: [["robot_id", robotId]],
        afterSeq: () => null,
        onStatus: setSocket,
        onOpen: () => void refresh(),
        onEvent: (ev: GatewayEvent) => {
          if (ev.type === "robot.state") {
            const s = ev.state;
            setRobot((r) =>
              r ? { ...r, pose: s.pose, status: s.status, battery_pct: s.battery_pct ?? r.battery_pct, charging: s.charging, holding: s.holding, basket_skus: s.basket_skus } : r,
            );
          } else if (ev.type === "fleet.robot_updated") {
            setRobot(ev.robot);
          } else if (ev.type === "brain.step") {
            const st = ev.step;
            setSteps((xs) => (xs.some((x) => x.step_id === st.step_id) ? xs : [st, ...xs].slice(0, 60)));
            setRobot((r) => (r && st.text && st.role === "robot" ? { ...r, last_thought: st.text } : r));
          }
          if (REFETCH_ON.has(ev.type)) soon();
        },
      });
    }, 0);
    return () => {
      clearTimeout(t);
      close?.();
    };
  }, [robotId, refresh, soon]);

  const conn: Conn = !online ? "offline" : robotError && !robotError.notFound ? "reconnecting" : socket;

  return { robot, robotError, planogram, steps, trip, approvals, conn, refresh, setApprovals };
}
