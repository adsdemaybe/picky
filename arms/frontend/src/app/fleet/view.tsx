"use client";
// Fleet view (gateway only): every robot per store (status, battery, basket, current goal, last thought), the store's
// goals, the selected robot's brain steps, and a live event feed over WS /ws?store_id= (goal.* / brain.step /
// fleet.robot_updated). Endpoints: GET /api/stores, /api/fleet/robots, /api/goals, /api/fleet/robots/{id}/steps,
// POST /api/fleet/robots/{id}/stop.
import { BatteryMedium, Bot, Brain, OctagonX } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";
import { ago, GatewayNeeded, StorePicker, useLiveGateway, usePoll, useStores } from "@/components/LiveOnly";
import { Panel, SectionTitle } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { errorText, gw } from "@/lib/gateway/client";
import { openEventSocket } from "@/lib/gateway/eventSocket";
import type { GatewayEvent } from "@/lib/gateway/events";
import { cn } from "@/lib/utils";

type FeedRow = { seq: number; type: string; ts: string; text: string };

export default function FleetPage() {
  const status = useLiveGateway();
  const live = status === "live";
  const { stores, err: storesErr } = useStores(live);
  const [picked, setPicked] = useState<string | null>(null);
  const storeId = picked ?? stores[0]?.store_id ?? "";
  const on = live && !!storeId;

  const robots = usePoll(on ? () => gw.robots({ store_id: storeId }).then((r) => r.data) : null, 3000, [storeId]);
  const goals = usePoll(on ? () => gw.goals({ store_id: storeId, limit: 15 }).then((r) => r.data) : null, 4000, [storeId]);
  const [robotPick, setRobotPick] = useState<string | null>(null);
  const robotId = robotPick && robots.data?.some((r) => r.robot_id === robotPick) ? robotPick : (robots.data?.[0]?.robot_id ?? null);
  const steps = usePoll(live && robotId ? () => gw.steps(robotId, { limit: 25 }).then((r) => r.data) : null, 3000, [robotId]);
  const feed = useStoreFeed(on ? storeId : null);

  async function stop(id: string, name: string) {
    try {
      await gw.stopRobot(id);
      toast.success(`${name} stopped`);
      robots.refresh();
    } catch (e) {
      toast.error(`Stop failed: ${errorText(e)}`);
    }
  }

  if (!live) {
    return (
      <div className="flex flex-col gap-6">
        <Header />
        <GatewayNeeded status={status} what="robots and goals" />
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-8">
      <Header />
      <StorePicker stores={stores} value={storeId} onChange={setPicked} />
      {storesErr && <p className="text-[14px] text-tomato">Stores: {storesErr}</p>}
      {robots.err && <p className="text-[14px] text-tomato">Robots: {robots.err}</p>}

      <section>
        <SectionTitle>Robots</SectionTitle>
        {robots.data && robots.data.length === 0 && <Panel className="p-4 text-[14px] text-muted-foreground">No robots in this store.</Panel>}
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {(robots.data ?? []).map((r) => (
            <Panel
              key={r.robot_id}
              data-testid="fleet-robot"
              className={cn("cursor-pointer p-4 transition-shadow", r.robot_id === robotId && "ring-2 ring-sky")}
              onClick={() => setRobotPick(r.robot_id)}
            >
              <div className="flex items-center gap-3">
                <span className="grid size-10 place-items-center rounded-full text-white" style={{ background: r.color ?? "#2563eb" }}>
                  <Bot className="size-5" />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="font-semibold">
                    {r.name} <span className="text-[12.5px] font-normal text-muted-foreground">{r.robot_id}</span>
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5 text-[12.5px] text-muted-foreground">
                    <Badge variant={r.status === "error" ? "destructive" : "secondary"}>{r.status}</Badge>
                    <span>{r.backend}</span>
                    {!r.connected && <span className="text-tomato">offline</span>}
                  </div>
                </div>
                <span className="flex items-center gap-1 text-[13px] tabular">
                  <BatteryMedium className="size-4" /> {Math.round(r.battery_pct ?? 0)}%
                </span>
              </div>
              <p className="mt-3 line-clamp-2 min-h-[2.6em] text-[13.5px]">{r.last_thought ?? "No thoughts yet."}</p>
              <div className="mt-3 flex items-center justify-between text-[12.5px] text-muted-foreground">
                <span>
                  Basket {r.basket_skus.length}/{r.basket_capacity}
                  {r.current_goal_id ? ` · ${r.current_goal_id}` : ""}
                </span>
                {r.status !== "idle" && (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={(e) => {
                      e.stopPropagation();
                      void stop(r.robot_id, r.name);
                    }}
                  >
                    <OctagonX /> Stop
                  </Button>
                )}
              </div>
            </Panel>
          ))}
        </div>
      </section>

      <div className="grid gap-8 lg:grid-cols-12">
        <section className="lg:col-span-6">
          <SectionTitle>
            <span className="inline-flex items-center gap-2">
              <Brain className="size-5" /> What {robots.data?.find((r) => r.robot_id === robotId)?.name ?? "the robot"} is thinking
            </span>
          </SectionTitle>
          <Panel className="max-h-[480px] divide-y divide-border overflow-y-auto text-[13.5px]">
            {steps.err && <p className="p-4 text-tomato">{steps.err}</p>}
            {steps.data && steps.data.length === 0 && <p className="p-4 text-muted-foreground">No brain steps yet.</p>}
            {(steps.data ?? []).map((s) => (
              <div key={s.step_id} className="flex gap-3 px-4 py-2.5" data-testid="brain-step">
                <span className="w-20 shrink-0 text-[12px] text-muted-foreground">{s.tool ?? s.kind}</span>
                <span className={cn("min-w-0 flex-1", !s.ok && "text-tomato")}>{s.text}</span>
                <span className="shrink-0 text-[12px] text-muted-foreground tabular">{ago(s.ts)}</span>
              </div>
            ))}
          </Panel>
        </section>

        <section className="lg:col-span-6">
          <SectionTitle>Goals</SectionTitle>
          <Panel className="divide-y divide-border text-[14px]">
            {goals.err && <p className="p-4 text-tomato">{goals.err}</p>}
            {goals.data && goals.data.length === 0 && <p className="p-4 text-muted-foreground">No goals in this store yet.</p>}
            {(goals.data ?? []).map((g) => (
              <div key={g.goal_id} className="px-4 py-3" data-testid="fleet-goal">
                <div className="flex items-center justify-between gap-3">
                  <span className="min-w-0 truncate font-medium">{g.title}</span>
                  <Badge variant={g.status === "failed" ? "destructive" : g.status === "completed" ? "outline" : "secondary"}>{g.status}</Badge>
                </div>
                <div className="mt-1 flex items-center gap-3 text-[12.5px] text-muted-foreground">
                  <span>{g.kind}</span>
                  <span>{g.robot_id ?? "unassigned"}</span>
                  <span>{g.items.length} items</span>
                  <span className="ml-auto tabular">{Math.round(g.progress * 100)}%</span>
                </div>
                <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-muted">
                  <div className="h-full bg-sky" style={{ width: `${Math.round(Math.min(1, g.progress) * 100)}%` }} />
                </div>
              </div>
            ))}
          </Panel>

          <SectionTitle className="mt-8">Live events</SectionTitle>
          <Panel className="max-h-[300px] divide-y divide-border overflow-y-auto font-mono text-[12px]">
            {feed.length === 0 && <p className="p-4 font-sans text-[13.5px] text-muted-foreground">Waiting for events from this store...</p>}
            {feed.map((f) => (
              <div key={f.seq} className="flex gap-3 px-4 py-1.5" data-testid="fleet-event">
                <span className="w-36 shrink-0 text-sky">{f.type}</span>
                <span className="min-w-0 flex-1 truncate">{f.text}</span>
              </div>
            ))}
          </Panel>
        </section>
      </div>
    </div>
  );
}

/** WS /ws?store_id=: newest first, last 40 events. */
function useStoreFeed(storeId: string | null) {
  const [feed, setFeed] = useState<{ sid: string | null; rows: FeedRow[] }>({ sid: null, rows: [] });
  useEffect(() => {
    if (!storeId) return;
    let last: number | null = null;
    return openEventSocket({
      filters: [["store_id", storeId]],
      afterSeq: () => last,
      onEvent: (ev: GatewayEvent) => {
        const e = ev as unknown as { seq?: number; type: string; ts?: string; payload?: Record<string, unknown> };
        if (typeof e.seq === "number") last = e.seq;
        const p = e.payload ?? {};
        const text = String(p.text ?? p.message ?? p.last_thought ?? p.title ?? p.status ?? p.goal_id ?? p.robot_id ?? "");
        const row = { seq: e.seq ?? Date.now() + Math.random(), type: e.type, ts: e.ts ?? "", text };
        setFeed((f) => ({ sid: storeId, rows: [row, ...(f.sid === storeId ? f.rows : [])].slice(0, 40) }));
      },
    });
  }, [storeId]);
  return feed.sid === storeId ? feed.rows : [];
}

function Header() {
  return (
    <section className="animate-rise">
      <h1 className="text-[36px] leading-[1.05] font-bold md:text-[46px]">Fleet</h1>
      <p className="mt-2 max-w-[62ch] text-[16px] text-muted-foreground">
        Every Picky robot, per store: what it is doing, what it is thinking, and the goals the fleet manager gave it.
      </p>
    </section>
  );
}
