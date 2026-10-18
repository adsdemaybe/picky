"use client";
import { Activity, Bot, Camera as CameraIcon, ChevronDown, Expand, MonitorPlay, Plug, PlugZap, Radio, Server, Unplug } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { CameraView } from "@/components/LiveFeed";
import { Panel } from "@/components/picky";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { productBySku } from "@/lib/data";
import { CAMERAS, CAMERA_HELP, CAMERA_LABEL, useFeed, type Camera, type Conn, type Mode } from "@/lib/feed";
import { useTrip } from "@/lib/trip";
import { cn } from "@/lib/utils";

const MODES: { id: Mode; label: string; icon: typeof Bot; help: string }[] = [
  { id: "sim", label: "Simulated", icon: MonitorPlay, help: "Cameras drawn from the in-browser trip. Works with no backend." },
  { id: "gateway", label: "Robot gateway", icon: Server, help: "Frames and robot state from the Picky gateway over WebSocket." },
  { id: "stream", label: "Direct streams", icon: Radio, help: "Point each camera at an MJPEG or video URL from the robot." },
];

const CONN_STYLE: Record<Conn, { label: string; cls: string }> = {
  idle: { label: "Not connected", cls: "bg-muted text-muted-foreground" },
  connecting: { label: "Connecting", cls: "bg-amber-wash text-amber-ink" },
  live: { label: "Live", cls: "bg-leaf text-white" },
  offline: { label: "Reconnecting", cls: "bg-amber-wash text-amber-ink" },
  error: { label: "Error", cls: "bg-tomato-wash text-tomato" },
};

export default function LivePage() {
  const feed = useFeed();
  const [main, setMain] = useState<Camera>("head");
  const stage = useRef<HTMLDivElement>(null);
  const phase = useTrip((s) => s.phase);
  const approval = useTrip((s) => s.approval);

  // restore this browser's saved connection settings, and start the simulated feed by default
  useEffect(() => {
    useFeed.getState().hydrate();
    if (useFeed.getState().conn === "idle" && useFeed.getState().mode === "sim") useFeed.getState().connect();
  }, []);

  const conn = CONN_STYLE[feed.conn];
  return (
    <div className="flex flex-col gap-6">
      <section className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[36px] leading-[1.05] font-bold md:text-[46px]">Live feed</h1>
          <p className="mt-2 max-w-[60ch] text-[16px] text-muted-foreground">Every camera on the robot, its position and what it&apos;s holding, as it happens.</p>
        </div>
        <div className="flex items-center gap-2">
          <span className={cn("flex h-9 items-center gap-2 rounded-full px-3.5 text-[13.5px] font-semibold", conn.cls)} role="status">
            {(feed.conn === "live" || feed.conn === "connecting") && <span className="live-dot size-2 rounded-full bg-current" />}
            {conn.label}
          </span>
          {feed.conn === "idle" || feed.conn === "error" ? (
            <Button onClick={feed.connect}>
              <Plug /> Connect
            </Button>
          ) : (
            <Button variant="outline" onClick={feed.disconnect}>
              <Unplug /> Disconnect
            </Button>
          )}
        </div>
      </section>

      <div className="grid gap-6 xl:grid-cols-12">
        <div className="flex flex-col gap-4 xl:col-span-8">
          <div ref={stage} className="relative aspect-video overflow-hidden rounded-2xl bg-black shadow-md">
            <AnimatePresence mode="wait">
              <motion.div key={main + feed.mode} className="absolute inset-0" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.2 }}>
                <CameraView camera={main} />
              </motion.div>
            </AnimatePresence>
            <div className="pointer-events-none absolute inset-x-0 top-0 flex items-center gap-2 bg-gradient-to-b from-black/60 to-transparent p-3 text-white">
              <span className="flex items-center gap-1.5 rounded-full bg-tomato px-2.5 py-0.5 text-[12px] font-bold">
                <span className="live-dot size-1.5 rounded-full bg-white" /> {CAMERA_LABEL[main]}
              </span>
              {feed.mode === "gateway" && feed.frames[main] && <span className="rounded-full bg-white/15 px-2 py-0.5 text-[11.5px]">Faces blurred</span>}
            </div>
            <Button
              size="icon-sm"
              variant="dark"
              className="absolute right-3 bottom-3 bg-black/60"
              aria-label="Full screen"
              onClick={() => stage.current?.requestFullscreen?.()}
            >
              <Expand />
            </Button>
          </div>

          <div className="grid grid-cols-2 gap-3 md:grid-cols-4" role="tablist" aria-label="Cameras">
            {CAMERAS.map((c) => (
              <button
                key={c}
                role="tab"
                aria-selected={main === c}
                onClick={() => setMain(c)}
                className="group relative cursor-pointer text-left"
              >
                <div className="relative aspect-video overflow-hidden rounded-xl bg-black">
                  {main !== c && <CameraView camera={c} compact />}
                  {main === c && <div className="absolute inset-0 grid place-items-center text-[12.5px] font-medium text-white/70">On the big screen</div>}
                  {main === c && <motion.span layoutId="cam-ring" className="pointer-events-none absolute inset-0 rounded-xl ring-3 ring-leaf ring-inset" />}
                </div>
                <div className="mt-1.5 flex items-center gap-1.5 text-[13.5px] font-semibold">
                  <CameraIcon className="size-3.5 text-muted-foreground" aria-hidden />
                  {CAMERA_LABEL[c]}
                </div>
                <div className="text-[12px] leading-snug text-muted-foreground">{CAMERA_HELP[c]}</div>
              </button>
            ))}
          </div>

          {approval && (
            <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }} className="flex flex-wrap items-center gap-4 rounded-2xl bg-amber-wash p-4 ring-2 ring-amber">
              <span className="live-dot size-2.5 rounded-full bg-amber" />
              <p className="min-w-0 flex-1 text-[14.5px] font-medium text-amber-ink">The robot is waiting for your answer before it moves on.</p>
              <Button asChild>
                <Link href="/trip">Answer on the trip page</Link>
              </Button>
            </motion.div>
          )}

          {feed.mode === "sim" && phase === "idle" && (
            <Panel className="flex flex-wrap items-center gap-4 p-4">
              <Bot className="size-6 text-leaf" aria-hidden />
              <p className="min-w-0 flex-1 text-[14.5px]">The simulated cameras come alive when the robot is on a trip.</p>
              <Button asChild>
                <Link href="/order">Start a trip</Link>
              </Button>
            </Panel>
          )}
        </div>

        <div className="flex flex-col gap-4 xl:col-span-4">
          <Telemetry />
          <Connection />
          <IntegrationNotes />
        </div>
      </div>
    </div>
  );
}

function Telemetry() {
  const mode = useFeed((s) => s.mode);
  const tel = useFeed((s) => s.telemetry);
  const stats = useFeed((s) => s.stats);
  const robot = useTrip((s) => s.robot);
  const cart = useTrip((s) => s.cart);
  const stops = useTrip((s) => s.stops);
  const activeStop = useTrip((s) => s.activeStop);
  const sim = { robot, cart, stops, activeStop };
  const [now, setNow] = useState(0); // set on the client; reading the clock during prerender is not allowed
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const live = mode === "sim";
  const pose = live ? sim.robot : tel?.pose;
  const status = live ? sim.robot.status : tel?.status;
  const holding = live ? (sim.robot.status === "picking" && sim.activeStop ? sim.stops[sim.activeStop]?.sku : null) : tel?.holding;
  const basket = live ? sim.cart.filter((c) => c.source === "in_store").reduce((s, c) => s + c.qty, 0) : (tel?.basket_skus.length ?? 0);
  const age = !live && tel && now ? Math.max(0, Math.round((now - tel.ts) / 1000)) : null;

  return (
    <Panel className="p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="flex items-center gap-2 text-[17px] font-semibold">
          <Activity className="size-4 text-leaf" aria-hidden /> Robot
        </h2>
        {status ? <Badge variant={status === "error" ? "tomato" : "leaf"} className="capitalize">{status}</Badge> : <Badge variant="muted">No data</Badge>}
      </div>
      <dl className="grid grid-cols-3 gap-2 text-center">
        {(
          [
            ["x", pose ? `${pose.x.toFixed(2)} m` : "–"],
            ["y", pose ? `${pose.y.toFixed(2)} m` : "–"],
            ["heading", pose ? `${Math.round(((pose.theta ?? 0) * 180) / Math.PI)}°` : "–"],
          ] as const
        ).map(([k, v]) => (
          <div key={k} className="rounded-xl bg-muted px-2 py-2">
            <dd className="tabular font-heading text-[16px] font-semibold">{v}</dd>
            <dt className="text-[12px] text-muted-foreground">{k}</dt>
          </div>
        ))}
      </dl>
      <dl className="mt-3 flex flex-col gap-1.5 text-[14px]">
        <div className="flex justify-between gap-3">
          <dt className="text-muted-foreground">Holding</dt>
          <dd className="truncate font-medium">{holding ? (productBySku[holding]?.name ?? holding) : "Nothing"}</dd>
        </div>
        <div className="flex justify-between">
          <dt className="text-muted-foreground">In the basket</dt>
          <dd className="font-medium">{basket} items</dd>
        </div>
        {mode === "gateway" && (
          <>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Frames</dt>
              <dd className="tabular font-medium">
                {stats.frames} at {stats.fps.toFixed(1)} fps
              </dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Delay</dt>
              <dd className="tabular font-medium">{stats.latencyMs !== null ? `${stats.latencyMs} ms` : "–"}</dd>
            </div>
            <div className="flex justify-between">
              <dt className="text-muted-foreground">Last update</dt>
              <dd className="tabular font-medium">{age !== null ? `${age} s ago` : "–"}</dd>
            </div>
          </>
        )}
        {tel?.error && <div className="mt-1 rounded-lg bg-tomato-wash px-3 py-2 text-[13px] text-tomato">{tel.error}</div>}
      </dl>
    </Panel>
  );
}

function Connection() {
  const f = useFeed();
  const trip = useTrip((s) => s.phase);
  return (
    <Panel className="p-4">
      <h2 className="mb-3 flex items-center gap-2 text-[17px] font-semibold">
        <PlugZap className="size-4 text-sky" aria-hidden /> Source
      </h2>
      <ToggleGroup
        type="single"
        value={f.mode}
        onValueChange={(v) => {
          if (!v) return;
          f.disconnect();
          f.setConfig({ mode: v as Mode });
          if (v === "sim") useFeed.getState().connect();
        }}
        className="grid w-full grid-cols-3 rounded-xl bg-muted p-1"
        aria-label="Feed source"
      >
        {MODES.map((m) => (
          <ToggleGroupItem key={m.id} value={m.id} className="h-auto flex-col gap-1 rounded-lg py-2 text-[12.5px] data-[state=on]:bg-card data-[state=on]:shadow-sm">
            <m.icon className="size-4" aria-hidden />
            {m.label}
          </ToggleGroupItem>
        ))}
      </ToggleGroup>
      <p className="mt-2 text-[13px] text-muted-foreground">{MODES.find((m) => m.id === f.mode)?.help}</p>

      {f.mode === "gateway" && (
        <div className="mt-4 flex flex-col gap-3">
          <Field label="Gateway URL" hint="The FastAPI gateway, e.g. http://192.168.1.20:8000">
            <Input value={f.gatewayUrl} onChange={(e) => f.setConfig({ gatewayUrl: e.target.value })} placeholder="http://localhost:8000" />
          </Field>
          <Field label="Trip ID" hint="Optional. Leave empty to see every robot frame.">
            <Input value={f.tripId} onChange={(e) => f.setConfig({ tripId: e.target.value })} placeholder={trip === "idle" ? "trip_…" : "trip id from the gateway"} />
          </Field>
          <Button onClick={f.connect}>
            <Plug /> {f.conn === "idle" ? "Connect" : "Reconnect"}
          </Button>
          {f.health && (
            <div className="rounded-xl bg-muted p-3 text-[13px]">
              <div className="font-semibold">Gateway says {f.health.status}</div>
              <div className="text-muted-foreground">
                Contracts {f.health.contracts_version}. Sim {f.health.sim_connected ? "connected" : "not connected"}. Real arms:{" "}
                {f.health.real_arms.length ? f.health.real_arms.join(", ") : "none (all fakes)"}.
              </div>
            </div>
          )}
        </div>
      )}

      {f.mode === "stream" && (
        <div className="mt-4 flex flex-col gap-3">
          {CAMERAS.map((c) => (
            <Field key={c} label={CAMERA_LABEL[c]}>
              <Input value={f.streams[c]} onChange={(e) => f.setStream(c, e.target.value)} placeholder="http://robot.local:8080/head.mjpg" />
            </Field>
          ))}
          <Button onClick={f.connect}>
            <Plug /> Use these streams
          </Button>
        </div>
      )}

      {f.log.length > 0 && (
        <ol className="mt-4 max-h-[140px] overflow-y-auto rounded-xl bg-muted p-2.5 text-[12.5px]">
          {[...f.log].reverse().map((l) => (
            <li key={l.t + l.text} className={cn("py-0.5", l.level === "error" ? "text-tomato" : l.level === "warn" ? "text-amber-ink" : "text-muted-foreground")}>
              {new Date(l.t).toLocaleTimeString()} {l.text}
            </li>
          ))}
        </ol>
      )}
    </Panel>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1.5">
      <span className="text-[13.5px] font-semibold">{label}</span>
      {children}
      {hint && <span className="text-[12px] text-muted-foreground">{hint}</span>}
    </label>
  );
}

function IntegrationNotes() {
  const [open, setOpen] = useState(false);
  return (
    <Panel className="overflow-hidden">
      <button onClick={() => setOpen((o) => !o)} aria-expanded={open} className="flex w-full cursor-pointer items-center justify-between p-4 text-left hover:bg-muted/60">
        <span>
          <span className="font-semibold">For the robot team</span>
          <span className="block text-[12.5px] text-muted-foreground">What the backend needs to send</span>
        </span>
        <ChevronDown className={cn("size-5 text-muted-foreground transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="border-t border-border p-4 text-[13px]">
          <p className="mb-2">
            Open <code className="rounded bg-muted px-1">WS /ws/frames?trip_id=…</code> on the gateway. Each message is one JSON event:
          </p>
          <pre className="overflow-x-auto rounded-xl bg-foreground p-3 text-[11.5px] leading-relaxed text-background">{`{"type":"frame","frame":{
  "camera":"head",        // head | wrist | basket | overview
  "jpeg_b64":"/9j/4AAQ…", // no data: prefix
  "w":960,"h":540,
  "blurred":true,         // frames without this are dropped
  "pose":{"x":4.2,"y":11.0,"theta":1.57},
  "ts":"2026-10-17T20:15:03Z"}}

{"type":"robot.state","state":{
  "pose":{"x":4.2,"y":11.0,"theta":1.57},
  "status":"picking",     // idle | navigating | scanning | picking | placing | error
  "holding":"inst_42",
  "basket_skus":["AVOCADO","LIMES"],
  "error":null,
  "ts":"2026-10-17T20:15:03Z"}}`}</pre>
          <p className="mt-2 text-muted-foreground">
            Shapes come from <code className="rounded bg-muted px-1">contracts/sim.py</code> (CameraFrame, RobotState) and{" "}
            <code className="rounded bg-muted px-1">contracts/events.py</code>. GET <code className="rounded bg-muted px-1">/health</code> fills the status box.
            The gateway must allow CORS from this app. For quick tests, any MJPEG URL works under Direct streams.
          </p>
        </div>
      )}
    </Panel>
  );
}
