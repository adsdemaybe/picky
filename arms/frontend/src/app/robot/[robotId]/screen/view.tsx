"use client";
// The robot's touch screen: header (name, store, status, battery, link state), three big tabs (Ask me, Where is,
// Checkout) and a "right now" column with a mini store map and the brain's latest steps. Everything is live gateway
// data (useRobotScreen); nothing here answers an approval for the shopper.
import {
  ArrowLeft,
  Battery,
  BatteryCharging,
  BatteryFull,
  BatteryLow,
  BatteryMedium,
  Bot,
  CircleAlert,
  MapPin,
  MessageCircle,
  Receipt,
  RefreshCw,
  WifiOff,
} from "lucide-react";
import { motion } from "motion/react";
import Link from "next/link";
import { useState } from "react";
import { Panel } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { GATEWAY_URL, type S } from "@/lib/gateway/client";
import type { BrainStep, RobotInfo } from "@/lib/gateway/events";
import { cn } from "@/lib/utils";
import { CheckoutTab } from "./checkout";
import { ChatTab, useSpeakSetting } from "./chat";
import { type Conn, useRobotScreen } from "./useRobotScreen";
import { WhereTab, type Target } from "./where";

type Tab = "ask" | "where" | "checkout";

export default function RobotScreen({ robotId }: { robotId: string }) {
  const data = useRobotScreen(robotId);
  const { robot, robotError, planogram, steps, trip, approvals, conn } = data;
  const [tab, setTab] = useState<Tab>("ask");
  const [target, setTarget] = useState<Target | null>(null);
  const speech = useSpeakSetting();

  // a new question / checkout card brings the Checkout tab forward (adjust-state-during-render, no effect)
  const approvalKey = approvals.map((a) => a.approval_id).join(",");
  const [seenKey, setSeenKey] = useState("");
  if (approvalKey !== seenKey) {
    const before = new Set(seenKey.split(",").filter(Boolean));
    setSeenKey(approvalKey);
    if (approvals.some((a) => !before.has(a.approval_id))) setTab("checkout");
  }

  if (!GATEWAY_URL)
    return <FullMessage icon={WifiOff} title="This screen needs the robot gateway" body="Set NEXT_PUBLIC_GATEWAY_URL to the Picky gateway and reload." />;
  if (robotError?.notFound)
    return <FullMessage icon={CircleAlert} title={`There's no robot called ${robotId}`} body="Check the address. The fleet has rover-ave-1, rover-ave-2 and rover-hill-1." back />;
  if (!robot && robotError)
    return (
      <FullMessage
        icon={WifiOff}
        title="Can't reach the Picky gateway"
        body={`${robotError.text}. This screen keeps trying every few seconds.`}
        action={
          <Button size="lg" onClick={() => void data.refresh()}>
            <RefreshCw /> Try again
          </Button>
        }
      />
    );
  if (!robot) return <ScreenSkeleton />;

  const pending = approvals.length;
  return (
    <div className="flex h-dvh flex-col overflow-hidden bg-background">
      <Header robot={robot} storeName={planogram?.name ?? null} conn={conn} />
      {conn === "offline" || conn === "reconnecting" ? (
        <div role="status" className="flex items-center gap-2 bg-amber-wash px-4 py-1.5 text-[13.5px] font-medium text-amber-ink">
          <WifiOff className="size-4 shrink-0" aria-hidden />
          {conn === "offline" ? "This screen is offline. What you see may be out of date." : "Reconnecting to the robot. What you see may be a few seconds old."}
        </div>
      ) : null}
      <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-y-auto p-3 md:flex-row md:overflow-hidden">
        <Panel className="flex min-h-[460px] flex-1 flex-col overflow-hidden md:min-h-0">
          <Tabs value={tab} onValueChange={(v) => setTab(v as Tab)} className="flex min-h-0 flex-1 flex-col gap-0">
            <TabsList className="m-2 mb-0 grid h-12! w-auto shrink-0 grid-cols-3 rounded-full bg-muted p-1">
              <ScreenTab value="ask" icon={MessageCircle} label="Ask me" />
              <ScreenTab value="where" icon={MapPin} label="Where is…" />
              <ScreenTab value="checkout" icon={Receipt} label="Checkout" count={pending} />
            </TabsList>
            <TabsContent value="ask" className="flex min-h-0 flex-col data-[state=inactive]:hidden">
              <ChatTab robot={robot} userId={trip?.user_id ?? null} speech={speech} />
            </TabsContent>
            <TabsContent value="where" className="flex min-h-0 flex-col data-[state=inactive]:hidden">
              <WhereTab robot={robot} onTarget={setTarget} speech={speech} />
            </TabsContent>
            <TabsContent value="checkout" className="flex min-h-0 flex-col data-[state=inactive]:hidden">
              <CheckoutTab robot={robot} trip={trip} approvals={approvals} refresh={data.refresh} setApprovals={data.setApprovals} />
            </TabsContent>
          </Tabs>
        </Panel>
        <NowPanel robot={robot} planogram={planogram} steps={steps} target={target} />
      </div>
    </div>
  );
}

function ScreenTab({ value, icon: Icon, label, count = 0 }: { value: Tab; icon: typeof Bot; label: string; count?: number }) {
  return (
    <TabsTrigger
      value={value}
      className="h-full! rounded-full text-[15px] font-semibold data-[state=active]:bg-card data-[state=active]:text-foreground data-[state=active]:shadow-sm"
    >
      <Icon className="size-[18px]!" aria-hidden />
      {label}
      {count > 0 && (
        <span className="live-dot grid h-5 min-w-5 place-items-center rounded-full bg-amber px-1 text-[11.5px] font-bold text-amber-ink" aria-label={`${count} waiting`}>
          {count}
        </span>
      )}
    </TabsTrigger>
  );
}

// ------------------------------------------------------------------------------------------------ header

const STATUS: Record<RobotInfo["status"], { label: string; tone: "leaf" | "sky" | "amber" | "tomato" | "muted" }> = {
  idle: { label: "Ready to help", tone: "leaf" },
  navigating: { label: "On the move", tone: "sky" },
  scanning: { label: "Scanning a shelf", tone: "sky" },
  picking: { label: "Picking an item", tone: "sky" },
  placing: { label: "Putting it in the basket", tone: "sky" },
  charging: { label: "Docked", tone: "leaf" },
  error: { label: "Needs a hand", tone: "tomato" },
  disconnected: { label: "Offline", tone: "muted" },
};

export function robotStatus(r: RobotInfo) {
  if (r.brain_state === "waiting_user") return { label: "Waiting for you", tone: "amber" as const };
  if (r.status === "charging" && r.charging) return { label: "Charging", tone: "leaf" as const };
  return STATUS[r.status] ?? { label: r.status, tone: "muted" as const };
}

function BatteryChip({ pct, charging }: { pct: number | null; charging: boolean }) {
  if (pct == null) return null;
  const Icon = charging ? BatteryCharging : pct >= 80 ? BatteryFull : pct >= 45 ? BatteryMedium : pct >= 20 ? BatteryLow : Battery;
  return (
    <span className={cn("tabular flex items-center gap-1.5 text-[15px] font-semibold", pct < 20 ? "text-tomato" : "text-foreground")} aria-label={`Battery ${Math.round(pct)} percent${charging ? ", charging" : ""}`}>
      <Icon className="size-5" aria-hidden />
      {Math.round(pct)}%
    </span>
  );
}

function ConnChip({ conn }: { conn: Conn }) {
  if (conn === "live")
    return (
      <Badge variant="leaf" className="hidden sm:inline-flex">
        <span className="live-dot size-1.5 rounded-full bg-current" /> Live
      </Badge>
    );
  if (conn === "offline") return <Badge variant="tomato">Offline</Badge>;
  return <Badge variant="amber">{conn === "connecting" ? "Connecting" : "Reconnecting"}</Badge>;
}

function Header({ robot, storeName, conn }: { robot: RobotInfo; storeName: string | null; conn: Conn }) {
  const st = robotStatus(robot);
  const tone = { leaf: "bg-leaf-wash text-leaf-deep", sky: "bg-sky-wash text-sky", amber: "bg-amber-wash text-amber-ink", tomato: "bg-tomato-wash text-tomato", muted: "bg-muted text-muted-foreground" }[st.tone];
  return (
    <header className="flex h-16 shrink-0 items-center gap-3 border-b border-border/70 bg-card px-3 md:px-4">
      <Link href="/live" aria-label="Leave the robot screen" className="-ml-1 grid size-10 shrink-0 place-items-center rounded-full text-muted-foreground hover:bg-muted">
        <ArrowLeft className="size-5" aria-hidden />
      </Link>
      <span className="grid size-11 shrink-0 place-items-center rounded-2xl text-white" style={{ background: robot.color ?? "var(--color-leaf)" }} aria-hidden>
        <Bot className="size-6" />
      </span>
      <div className="min-w-0">
        <div className="truncate font-heading text-[22px] leading-tight font-bold">
          <span className="hidden sm:inline">Hi, I&apos;m </span>
          {robot.name}
        </div>
        <div className="truncate text-[13px] text-muted-foreground">{storeName ?? robot.store_id}</div>
      </div>
      <span className={cn("ml-auto inline-flex h-9 shrink-0 items-center gap-2 rounded-full px-3 text-[13px] font-semibold sm:px-3.5 sm:text-[14px]", tone)} role="status">
        {st.tone === "sky" || st.tone === "amber" ? <span className="live-dot size-2 rounded-full bg-current" aria-hidden /> : null}
        {st.label}
      </span>
      <BatteryChip pct={robot.battery_pct} charging={robot.charging} />
      <ConnChip conn={conn} />
    </header>
  );
}

// ------------------------------------------------------------------------------------------------ right column

function NowPanel({ robot, planogram, steps, target }: { robot: RobotInfo; planogram: S["Planogram"] | null; steps: BrainStep[]; target: Target | null }) {
  const recent = steps.filter((s) => s.text.trim()).slice(0, 8);
  return (
    <Panel className="flex shrink-0 flex-col overflow-hidden md:w-[300px] lg:w-[340px]">
      <div className="border-b border-border/70 px-4 py-3">
        <div className="text-[12.5px] font-medium text-muted-foreground">Right now</div>
        <p className="mt-0.5 line-clamp-2 text-[15px] leading-snug font-medium">{robot.error ?? robot.last_thought ?? (robot.current_goal_id ? "Working on an order." : "Free to help. Ask me anything.")}</p>
      </div>
      <div className="relative shrink-0 border-b border-border/70 bg-muted/40 p-2">
        {planogram ? (
          <MiniMap planogram={planogram} pose={robot.pose} target={target} />
        ) : (
          <Skeleton className="h-[170px] w-full rounded-xl" />
        )}
        {target && (
          <div className="pointer-events-none absolute right-3 bottom-3 flex items-center gap-1 rounded-full bg-card/95 px-2.5 py-1 text-[12px] font-semibold text-tomato shadow-sm">
            <MapPin className="size-3.5" aria-hidden /> {target.name}
          </div>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3">
        <div className="mb-2 text-[12.5px] font-medium text-muted-foreground">What I&apos;ve been doing</div>
        {recent.length === 0 ? (
          <p className="text-[14px] text-muted-foreground">Nothing yet today.</p>
        ) : (
          <ol className="flex flex-col gap-2">
            {recent.map((s, i) => (
              <motion.li key={s.step_id} initial={{ opacity: 0, x: 8 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: i < 3 ? 0.04 * i : 0 }} className="flex gap-2 text-[13.5px] leading-snug">
                <span className={cn("mt-1.5 size-2 shrink-0 rounded-full", !s.ok ? "bg-tomato" : s.role === "user" ? "bg-sky" : s.kind === "message" ? "bg-leaf" : "bg-input")} aria-hidden />
                <span className="min-w-0 flex-1">
                  <span className={cn(!s.ok && "text-tomato")}>{s.role === "user" ? `You: ${s.text}` : s.text}</span>
                  <span className="ml-1.5 text-[12px] whitespace-nowrap text-muted-foreground">{ago(s.ts)}</span>
                </span>
              </motion.li>
            ))}
          </ol>
        )}
      </div>
    </Panel>
  );
}

function ago(ts: string) {
  const s = Math.max(0, (Date.now() - Date.parse(ts)) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(ts).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

/** Top-down store map in meters: fixtures, checkout, the robot and the searched product's shelf. */
function MiniMap({ planogram, pose, target }: { planogram: S["Planogram"]; pose: RobotInfo["pose"]; target: Target | null }) {
  const B = planogram.bounds;
  const fy = (y: number) => B.y + B.h - (y - B.y);
  // landscape: draw the store rotated a quarter turn so a tall store fills the short, wide box
  const rotate = B.h > B.w;
  const vw = rotate ? B.h : B.w;
  const vh = rotate ? B.w : B.h;
  const pad = 0.4;
  const t = target?.pos;
  return (
    <svg viewBox={`${-pad} ${-pad} ${vw + 2 * pad} ${vh + 2 * pad}`} className="mx-auto block h-[170px] w-full" role="img" aria-label={`Store map${target ? `, ${target.name} marked` : ""}`}>
      <g transform={rotate ? `translate(${B.h} 0) rotate(90)` : undefined}>
        <g transform={`translate(${-B.x} ${-B.y})`}>
          <rect x={B.x} y={B.y} width={B.w} height={B.h} rx={0.3} fill="var(--card)" stroke="var(--border)" strokeWidth={0.08} />
          {planogram.fixtures.map((f) => (
            <rect
              key={f.fixture_id}
              x={f.rect.x}
              y={fy(f.rect.y + f.rect.h)}
              width={f.rect.w}
              height={f.rect.h}
              rx={0.05}
              fill={
                target && f.fixture_id === target.fixtureId
                  ? "var(--color-tomato-wash)"
                  : f.kind === "produce_bin"
                    ? "var(--color-leaf-wash)"
                    : f.kind === "fridge" || f.kind === "freezer"
                      ? "#dfe6ec"
                      : f.kind === "checkout"
                        ? "var(--color-sky-wash)"
                        : "var(--muted)"
              }
              stroke={target && f.fixture_id === target.fixtureId ? "var(--color-tomato)" : "var(--input)"}
              strokeWidth={target && f.fixture_id === target.fixtureId ? 0.08 : 0.03}
            />
          ))}
          <rect x={planogram.entrance.x - 0.8} y={fy(B.y) - 0.12} width={1.6} height={0.12} fill="var(--color-tomato)" />
          {t && (
            <g transform={`translate(${t.x} ${fy(t.y)})`}>
              <circle r={0.7} fill="var(--color-tomato)" opacity={0.18}>
                <animate attributeName="r" values="0.5;0.95;0.5" dur="1.6s" repeatCount="indefinite" />
              </circle>
              <circle r={0.32} fill="var(--color-tomato)" stroke="white" strokeWidth={0.08} />
            </g>
          )}
          {pose && (
            <g transform={`translate(${pose.x} ${fy(pose.y)}) rotate(${(-(pose.theta ?? 0) * 180) / Math.PI})`} style={{ transition: "transform 0.5s linear" }}>
              <path d="M0,0 L1.6,-0.7 L1.6,0.7 Z" fill="var(--color-leaf)" opacity={0.18} />
              <rect x={-0.34} y={-0.29} width={0.68} height={0.58} rx={0.14} fill="var(--color-leaf)" stroke="white" strokeWidth={0.07} />
              <rect x={0.16} y={-0.12} width={0.14} height={0.24} rx={0.05} fill="var(--color-lime)" />
            </g>
          )}
        </g>
      </g>
    </svg>
  );
}

// ------------------------------------------------------------------------------------------------ states

function FullMessage({ icon: Icon, title, body, action, back }: { icon: typeof Bot; title: string; body: string; action?: React.ReactNode; back?: boolean }) {
  return (
    <div className="flex h-dvh flex-col items-center justify-center gap-3 bg-background px-6 text-center" role="alert">
      <span className="grid size-16 place-items-center rounded-3xl bg-muted text-muted-foreground">
        <Icon className="size-8" aria-hidden />
      </span>
      <h1 className="max-w-[24ch] text-[28px] leading-tight font-bold">{title}</h1>
      <p className="max-w-[52ch] text-[16px] text-muted-foreground">{body}</p>
      {action}
      {back && (
        <Button asChild variant="outline" size="lg">
          <Link href="/live">Back to Live</Link>
        </Button>
      )}
    </div>
  );
}

export function ScreenSkeleton() {
  return (
    <div className="flex h-dvh flex-col bg-background" aria-busy="true" aria-label="Loading the robot screen">
      <div className="flex h-16 items-center gap-3 border-b border-border/70 bg-card px-4">
        <Skeleton className="size-11 rounded-2xl" />
        <div className="flex flex-col gap-1.5">
          <Skeleton className="h-5 w-40" />
          <Skeleton className="h-3.5 w-56" />
        </div>
        <Skeleton className="ml-auto h-9 w-32 rounded-full" />
      </div>
      <div className="flex flex-1 gap-3 p-3">
        <Skeleton className="flex-1 rounded-2xl" />
        <Skeleton className="hidden w-[300px] rounded-2xl md:block" />
      </div>
    </div>
  );
}
