"use client";
// Projector view of the current trip (1080p and 1440×900): the Isaac Sim director (chase) camera as the big picture,
// the robot's on-board camera with Perceptron bounding boxes (+ wrist inset) beside it, then the plan, approvals,
// basket and budget, the route map, the robot's thinking log and the raw events.
// Gateway trips come from /watch?trip=<id> (or the trip store); without a gateway it shows the in-browser replay.
import { Eye, ShoppingBasket, Sparkles, Wrench } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Suspense, useState } from "react";
import { RobotArt } from "@/components/art";
import { BindTripFromUrl } from "@/components/GatewayBits";
import { HandoffPanel } from "@/components/Handoff";
import { LottieSlot } from "@/components/media";
import { Panel, ProductTile } from "@/components/picky";
import { CameraInset, CameraPanel, directorRungs, overheadRungs, perceivedRungs, useCeilingCameras, useSimNames, wristRungs, type Rung } from "@/components/StageCamera";
import { VerifyStack } from "@/components/TripReview";
import { TripWhisperPanel } from "@/components/Whisper";
import { MapLegend, StoreMap } from "@/components/StoreMap";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { STORES, productBySku, type StoreId } from "@/lib/data";
import { GATEWAY_URL } from "@/lib/gateway/client";
import { useGateway } from "@/lib/gateway/useGateway";
import { useTrip, type LogEvent } from "@/lib/trip";
import { cn } from "@/lib/utils";
import { ApprovalPanel, Basket, Controls, EventLog, RobotThinking } from "../trip/view";

export default function WatchPage() {
  const t = useTrip();
  const router = useRouter();
  const isaac = useGateway((s) => !!s.health?.sim_connected);
  const perceptron = useGateway((s) => !!s.health?.real_arms.includes("cameras"));
  const [mapOnStage, setMapOnStage] = useState(false);
  const [view, setView] = useState("chase");
  // Store switch: "trip" follows the active trip; a store id pins every camera to that floor (Picky Market ↔ T-Mobile)
  const [storePick, setStorePick] = useState<"trip" | StoreId>("trip");
  const gwLive = useGateway((s) => s.status) === "live";
  const gwBase = gwLive ? GATEWAY_URL : "";
  const tripStore: StoreId = STORES[t.storeId] ? (t.storeId as StoreId) : "store_ave";
  const camStore: StoreId = storePick === "trip" ? tripStore : storePick;
  const STORE_ROVER: Record<StoreId, string> = { store_ave: "rover-ave-1", store_tmobile: "rover-tmo-1" };
  const camRobot = storePick === "trip" ? (t.source === "gateway" ? t.robotId : null) : STORE_ROVER[camStore];
  const ceilings = useCeilingCameras(camStore, gwBase, gwLive);
  const simOf = useSimNames(gwBase ? camRobot : null, camStore, gwBase, isaac);
  // T-Mobile floor fallback: a labeled recorded Isaac run (video 12) when the live cameras aren't reachable
  const isaacReplay: Rung | null =
    camStore === "store_tmobile"
      ? {
          kind: "node",
          title: "T-Mobile store · Isaac Sim",
          badge: "Recorded run",
          note: "Recorded Isaac run, labeled; the live cameras above stream when the worker is up",
          node: <video key="tmo-replay" className="h-full w-full object-cover" src="/streams/store-tmobile-isaac.mp4" autoPlay muted loop playsInline />,
        }
      : null;
  const bind = (
    <Suspense fallback={null}>
      <BindTripFromUrl />
    </Suspense>
  );

  if (t.source !== "gateway" && (t.phase === "idle" || !t.draft)) {
    return (
      <div className="mx-auto flex max-w-[560px] animate-rise flex-col items-center py-16 text-center">
        {bind}
        <LottieSlot slot="robotIdle" className="aspect-[8/7] w-[300px]" fallback={<RobotArt sleeping />} />
        <h1 className="mt-6 text-[34px] leading-tight font-bold">Nothing to watch yet</h1>
        <p className="mt-2 text-[16px] text-muted-foreground">
          Pick what&apos;s running low on Home and the robot&apos;s cameras show up here while it shops: the live picture, what it detects, every pick and every
          question.
        </p>
        <Button asChild size="lg" className="mt-6">
          <Link href="/">Pick what&apos;s running low</Link>
        </Button>
      </div>
    );
  }

  const gateway = t.source === "gateway";
  const base = gateway ? GATEWAY_URL : "";
  const opts = { robotId: camRobot, storeId: camStore, base: gwLive ? GATEWAY_URL : base, simOf, perceptron, waiting: gwLive && !camRobot };
  const store = STORES[camStore];
  const overhead = ceilings.find((c) => c.id === view);
  const baseRungs = overhead ? overheadRungs(opts, overhead.id, `Bird's-eye · ${overhead.name.replace(/^.*·\s*/, "")}`) : directorRungs(opts);
  const stageRungs = isaacReplay && !overhead ? [...baseRungs.slice(0, -1), isaacReplay, ...baseRungs.slice(-1)] : baseRungs;
  const views = [{ id: "chase", label: "Chase cam" }, ...ceilings.map((c, i) => ({ id: c.id, label: ceilings.length > 1 ? `Bird's-eye ${i + 1}` : "Bird's-eye" }))];
  const who = t.robotName ?? (gateway ? (t.robotId ?? "Assigning a robot") : "Demo robot");
  const phaseText =
    t.tripState === "failed" ? "Stopped" : t.phase === "done" ? "Done" : t.phase === "checkout" ? "At checkout" : t.mode === "pickup" ? "Shopping for pickup" : "Shopping with you";

  return (
    <div className="relative left-1/2 flex w-[min(1840px,calc(100vw-2rem))] -translate-x-1/2 flex-col gap-4">
      {bind}
      <section className="flex flex-wrap items-center gap-x-6 gap-y-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2 text-[14px] font-medium text-muted-foreground">
            <span className="grid size-6 place-items-center rounded-md bg-leaf text-white">
              <ShoppingBasket className="size-3.5" aria-hidden />
            </span>
            {store?.name ?? t.storeId}
            <span aria-hidden>/</span>
            {who}
            <span aria-hidden>/</span>
            {phaseText}
          </div>
          <h1 className="mt-0.5 text-[28px] leading-tight font-bold xl:text-[34px]">{t.draft?.title ?? `${t.robotName ?? "The robot"} is planning your trip`}</h1>
        </div>
        <div className="min-w-0 flex-1 basis-[520px]">
          <Controls onRestart={() => (t.reset(), router.push("/"))} switchTo="trip" />
        </div>
      </section>

      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1.75fr)_minmax(340px,1fr)]">
        <div className="flex min-w-0 flex-col gap-4">
          <div role="group" aria-label="Store cameras" className="-mb-1 flex flex-wrap items-center gap-1.5">
            <Eye className="mr-0.5 size-3.5 text-muted-foreground" aria-hidden />
            {([["trip", `This trip · ${STORES[tripStore].short}`], ["store_ave", "Picky Market"], ["store_tmobile", "T-Mobile"]] as [typeof storePick, string][]).map(([id, label]) => (
              <button
                key={id}
                type="button"
                aria-pressed={storePick === id}
                onClick={() => {
                  setStorePick(id);
                  setView("chase");
                }}
                className={cn(
                  "min-h-8 cursor-pointer rounded-full px-3 py-1 text-[13px] font-semibold transition-colors",
                  storePick === id ? "bg-sky text-white" : "bg-muted text-muted-foreground hover:text-foreground",
                )}
              >
                {label}
              </button>
            ))}
          </div>
          {views.length > 1 && (
            <div role="tablist" aria-label="Camera view" className="-mb-2 flex flex-wrap items-center gap-1.5">
              {views.map((v) => (
                <button
                  key={v.id}
                  role="tab"
                  aria-selected={(overhead?.id ?? "chase") === v.id}
                  onClick={() => setView(v.id)}
                  className={cn(
                    "rounded-full px-3 py-1 text-[13px] font-semibold transition-colors",
                    (overhead?.id ?? "chase") === v.id ? "bg-foreground text-background" : "bg-muted text-muted-foreground hover:text-foreground",
                  )}
                >
                  {v.label}
                </button>
              ))}
            </div>
          )}
          <CameraPanel
            rungs={stageRungs}
            alt={overhead ? `Isaac Sim ceiling camera over ${store?.name ?? t.storeId}` : `Isaac Sim chase camera following ${who}`}
            onRung={(r: Rung) => setMapOnStage(r.kind === "node" && r.title === "Store map")}
          />
          <PlanStrip />
          <div className={cn("grid items-start gap-4", !mapOnStage && "xl:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]")}>
            {!mapOnStage && <RouteCard />}
            {gateway ? <RobotThinking /> : <ReplayLog />}
          </div>
        </div>
        <div className="flex min-w-0 flex-col gap-4">
          <CameraPanel
            rungs={perceivedRungs(opts)}
            alt={`${who}'s on-board camera with detected products boxed`}
            overlay={
              <div className="pointer-events-none absolute right-2.5 bottom-2.5 w-[32%] max-w-[210px]">
                <CameraInset rungs={wristRungs(opts)} alt={`${who}'s wrist camera`} />
              </div>
            }
          />
          <HandoffPanel />
          <ApprovalPanel />
          <TripWhisperPanel />
          <VerifyStack />
          <Basket />
          <EventLog />
        </div>
      </div>
    </div>
  );
}

function RouteCard() {
  const route = useTrip((s) => s.route);
  const storeId = useTrip((s) => s.storeId);
  const store = STORES[storeId];
  return (
    <Panel className="p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-[17px] font-semibold">Route</h2>
        <span className="text-[13px] text-muted-foreground">{route ? `${route.total_m.toFixed(0)} m, planned from the store layout` : "Planning"}</span>
      </div>
      {route && store ? (
        <>
          <div className="mx-auto max-h-[420px]" style={{ aspectRatio: `${store.bounds.w + 0.8} / ${store.bounds.h + 0.8}` }}>
            <StoreMap />
          </div>
          <div className="mt-2">
            <MapLegend />
          </div>
        </>
      ) : (
        <Skeleton className="h-[320px] w-full rounded-xl" />
      )}
    </Panel>
  );
}

const FINISHED = ["done", "declined", "skipped"];

/** The plan as one row of stops: done ✓, the current one highlighted with what the robot is doing. */
function PlanStrip() {
  const { route, stops, stopStatus, activeStop, robot, error, source, tripState, handoff } = useTrip();
  if (!route)
    return (
      <Panel className="p-4">
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-[17px] font-semibold">Plan</h2>
          <span className="text-[13px] text-muted-foreground">{error && source === "gateway" ? error : "Reading your list and checking the shelves"}</span>
        </div>
        <div className="flex flex-wrap gap-2" aria-hidden>
          {[120, 160, 140, 110, 150].map((w, i) => (
            <Skeleton key={i} className="h-10 rounded-full" style={{ width: w }} />
          ))}
        </div>
      </Panel>
    );
  const ids = route.order.filter((id) => stops[id] && stops[id].kind !== "start");
  const done = ids.filter((id) => FINISHED.includes(stopStatus[id])).length;
  return (
    <Panel className="p-4">
      <div className="mb-3 flex items-baseline justify-between gap-3">
        <h2 className="text-[17px] font-semibold">Plan</h2>
        <span className="text-[13px] text-muted-foreground">
          {done} of {ids.length} stops done, {route.total_m.toFixed(0)} m to walk
        </span>
      </div>
      <ol className="flex flex-wrap gap-2">
        {ids.map((id, i) => {
          const s = stops[id];
          const st = stopStatus[id];
          const active = activeStop === id && !FINISHED.includes(st) && tripState !== "failed";
          const p = s.sku ? productBySku[s.sku] : null;
          return (
            <motion.li
              key={id}
              layout
              className={cn(
                "flex h-10 items-center gap-2 rounded-full py-1 pr-3.5 pl-1 text-[14px] ring-1",
                st === "done" ? "bg-leaf-wash ring-leaf/25" : active ? "bg-sky-wash ring-2 ring-sky" : "bg-card ring-foreground/10",
                (st === "declined" || st === "skipped") && "text-muted-foreground line-through",
              )}
            >
              <span
                className={cn(
                  "tabular grid size-8 shrink-0 place-items-center rounded-full text-[12.5px] font-bold",
                  st === "done" ? "bg-leaf text-white" : active ? "bg-sky text-white" : "bg-muted",
                )}
              >
                {st === "done" ? "✓" : s.kind === "end" ? "$" : i + 1}
              </span>
              {p && <ProductTile color={p.label_color} category={p.category} name={p.name} size={24} className="rounded-md" />}
              <span className="max-w-[18ch] truncate font-medium">
                {s.qty && s.qty > 1 ? `${s.qty} × ` : ""}
                {s.label}
              </span>
              {s.kind === "optional" && <Badge variant="amber">Extra</Badge>}
              {active && (
                <span className="text-[12.5px] font-semibold text-sky capitalize">
                  {s.kind === "end" && handoff?.status === "active"
                    ? handoff.n && handoff.total
                      ? `Unloading ${handoff.n}/${handoff.total}` // the backend's unit number (checkout.unload)
                      : `Unloading ${Math.min(handoff.units.filter((u) => u.state === "counter").length + 1, handoff.units.length)}/${handoff.units.length}`
                    : robot.status}
                </span>
              )}
            </motion.li>
          );
        })}
      </ol>
    </Panel>
  );
}

const LOG_ICON: Record<string, typeof Sparkles> = { "robot.arrived": Eye, "item.picked": Wrench, "scan.result": Eye };

/** Demo replay: the trip's events as the robot's running commentary (gateway trips show brain.step instead). */
function ReplayLog() {
  const log = useTrip((s) => s.log);
  const rows: LogEvent[] = [...log].reverse().slice(0, 60);
  return (
    <Panel className="p-4">
      <div className="mb-2 flex items-center justify-between">
        <h2 className="text-[17px] font-semibold">What the robot is doing</h2>
        <span className="text-[13px] text-muted-foreground">Demo replay</span>
      </div>
      <ol className="flex max-h-[320px] flex-col overflow-y-auto">
        {rows.length === 0 && <li className="py-3 text-[14px] text-muted-foreground">Its steps show up here as it works.</li>}
        <AnimatePresence initial={false}>
          {rows.map((e) => {
            const Icon = LOG_ICON[e.type] ?? Sparkles;
            return (
              <motion.li
                key={e.seq}
                layout
                initial={{ opacity: 0, y: -8 }}
                animate={{ opacity: 1, y: 0 }}
                className="flex gap-2.5 border-b border-border/60 py-2 text-[13.5px] last:border-0"
              >
                <Icon className="mt-0.5 size-4 shrink-0 text-leaf" strokeWidth={1.5} aria-hidden />
                <div className="min-w-0 flex-1">
                  <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11.5px]">{e.type}</code>
                  <p className="mt-0.5 break-words">{e.summary}</p>
                </div>
              </motion.li>
            );
          })}
        </AnimatePresence>
      </ol>
    </Panel>
  );
}
