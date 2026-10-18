"use client";
// Robot camera views. Every camera can come from three sources (see lib/feed.ts):
//   sim      drawn in the browser from the trip replay + store layout
//   gateway  latest face-blurred JPEG per camera from WS /ws/frames, or, for a bound gateway trip, the robot's
//            MJPEG stream (one per page) and polled snapshots (thumbnails, insets); the gateway blurs every frame
//   stream   a direct URL per camera (MJPEG in <img>, MP4/WebM in <video>)
import { ExternalLink, Radio, VideoOff } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";
import { STORES, productBySku, slotsOf, type Slot, type StoreId } from "@/lib/data";
import { CAMERA_LABEL, useFeed, type Camera } from "@/lib/feed";
import { cameraUrl, robotCameraId } from "@/lib/gateway/client";
import { useTrip } from "@/lib/trip";
import { cn as cx } from "@/lib/utils";
import { StoreMap } from "./StoreMap";

const MODE_LABEL = { sim: "Simulated", gateway: "Robot gateway", stream: "Direct stream" } as const;

/** One camera, whatever the source. Fills its parent (give the parent an aspect ratio). */
export function CameraView({ camera, inset = false, compact = false }: { camera: Camera; inset?: boolean; compact?: boolean }) {
  const mode = useFeed((s) => s.mode);
  const frame = useFeed((s) => s.frames[camera]);
  const url = useFeed((s) => s.streams[camera]);
  const conn = useFeed((s) => s.conn);
  const telemetry = useFeed((s) => s.telemetry);
  const robotId = useFeed((s) => s.robotId);
  const gatewayUrl = useFeed((s) => s.gatewayUrl);

  if (mode === "sim") {
    if (camera === "head") return <SimCamera basketInset={inset} />;
    if (camera === "wrist") return <WristCamera />;
    if (camera === "basket") return <BasketCamera />;
    return (
      <div className="absolute inset-0 grid place-items-center bg-[#f7f8f6] p-3">
        <StoreMap compact />
      </div>
    );
  }
  if (mode === "stream") {
    if (url) return <UrlStream url={url} />;
    return <NoSignal compact={compact} title={`No URL for the ${CAMERA_LABEL[camera].toLowerCase()}`} body="Add one in the connection panel on the Live page." />;
  }
  // gateway
  if (frame) {
    // eslint-disable-next-line @next/next/no-img-element
    return <img src={frame.src} alt={`${CAMERA_LABEL[camera]} (faces blurred)`} className="absolute inset-0 h-full w-full object-cover" />;
  }
  if (camera === "overview" && (telemetry || robotId))
    return (
      <div className="absolute inset-0 grid place-items-center bg-[#f7f8f6] p-3">
        <StoreMap compact poseOverride={telemetry?.pose} />
      </div>
    );
  if (robotId && camera !== "overview") return <GatewayCamera camera={camera} robotId={robotId} base={gatewayUrl} compact={compact} inset={inset} />;
  return (
    <NoSignal
      compact={compact}
      title={conn === "live" || conn === "connecting" ? `Waiting for ${CAMERA_LABEL[camera].toLowerCase()} frames` : "Robot not connected"}
      body={conn === "idle" ? "Connect to the gateway on the Live page." : "Frames appear here as soon as the robot sends them."}
      pulse={conn === "connecting" || conn === "live"}
    />
  );
}

function NoSignal({ title, body, pulse, compact }: { title: string; body: string; pulse?: boolean; compact?: boolean }) {
  if (compact)
    return (
      <div className="absolute inset-0 grid place-items-center bg-[#16181a] text-white" title={title}>
        <span className={cx("grid size-9 place-items-center rounded-full bg-white/10", pulse && "live-dot")}>
          {pulse ? <Radio className="size-4" /> : <VideoOff className="size-4" />}
        </span>
      </div>
    );
  return (
    <div className="absolute inset-0 grid place-items-center bg-[#16181a] p-6 text-center text-white">
      <div>
        <span className={cx("mx-auto grid size-12 place-items-center rounded-full bg-white/10", pulse && "live-dot")}>
          {pulse ? <Radio className="size-6" /> : <VideoOff className="size-6" />}
        </span>
        <div className="mt-3 font-semibold">{title}</div>
        <div className="mt-1 text-[13px] text-white/60">{body}</div>
      </div>
    </div>
  );
}

function UrlStream({ url }: { url: string }) {
  const isVideo = /\.(mp4|webm|mov|m3u8)(\?|$)/i.test(url);
  if (isVideo) return <video src={url} autoPlay muted loop playsInline className="absolute inset-0 h-full w-full object-cover" />;
  // MJPEG streams render natively in <img>
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={url} alt="Robot camera stream" className="absolute inset-0 h-full w-full object-cover" />;
}

/** A bound gateway trip's robot camera: MJPEG for the big view, polled snapshots for thumbnails and insets. */
function GatewayCamera({ camera, robotId, base, compact, inset }: { camera: Exclude<Camera, "overview">; robotId: string; base: string; compact: boolean; inset: boolean }) {
  const id = robotCameraId(robotId, camera);
  const label = `${CAMERA_LABEL[camera]} (faces blurred)`;
  if (compact) return <PolledSnapshot src={cameraUrl(id, "snapshot", { w: 320 }, base)} every={1500} alt={label} />;
  return (
    <>
      <MjpegStream src={cameraUrl(id, "stream", { fps: 5, w: 960 }, base)} alt={label} />
      {inset && camera === "head" && (
        <div className="pointer-events-none absolute top-3 right-3 flex w-[26%] max-w-[220px] flex-col gap-2">
          {(["wrist", "basket"] as const).map((c) => (
            <div key={c} className="relative aspect-[4/3] overflow-hidden rounded-lg bg-black/60 ring-1 ring-white/25">
              <PolledSnapshot src={cameraUrl(robotCameraId(robotId, c), "snapshot", { w: 240 }, base)} every={2000} alt={`${CAMERA_LABEL[c]} (faces blurred)`} />
              <span className="absolute bottom-1 left-1 rounded bg-black/60 px-1.5 py-0.5 font-mono text-[10px] tracking-wide text-white">{c.toUpperCase()}</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

/** MJPEG renders natively in <img>; on error show a reconnect state and retry every 2 s.
 *  The src is attached in an effect so a hidden (kept-alive) route lets go of its stream connection. */
function MjpegStream({ src, alt }: { src: string; alt: string }) {
  const ref = useRef<HTMLImageElement>(null);
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState(false);
  const url = attempt ? `${src}&r=${attempt}` : src;
  useEffect(() => {
    const img = ref.current;
    if (!img || failed) return;
    img.src = url;
    return () => img.removeAttribute("src");
  }, [url, failed]);
  useEffect(() => {
    if (!failed) return;
    const t = setTimeout(() => {
      setFailed(false);
      setAttempt((a) => a + 1);
    }, 2000);
    return () => clearTimeout(t);
  }, [failed]);
  if (failed) return <NoSignal title="Reconnecting to the robot camera" body="The picture comes back as soon as the gateway answers." pulse />;
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img ref={ref} alt={alt} onError={() => ref.current?.getAttribute("src") && setFailed(true)} className="absolute inset-0 h-full w-full object-cover" />
  );
}

/** snapshot.jpg polling (≤ 6 connections per host, so only one MJPEG per page). Preloads, so no flicker. */
function PolledSnapshot({ src, every, alt }: { src: string; every: number; alt: string }) {
  const [shown, setShown] = useState<string | null>(null);
  const [errors, setErrors] = useState(0);
  useEffect(() => {
    let alive = true;
    let errs = 0;
    const tick = () => {
      const url = `${src}${src.includes("?") ? "&" : "?"}t=${Date.now()}`;
      const img = new Image();
      img.onload = () => {
        if (!alive) return;
        errs = 0;
        setErrors(0);
        setShown(url);
      };
      img.onerror = () => {
        if (!alive) return;
        setErrors(++errs); // 503 = no frame yet
      };
      img.src = url;
    };
    tick();
    const id = setInterval(tick, every);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [src, every]);
  if (errors >= 3) return <NoSignal compact title="No frame from this camera yet" body="" />;
  if (!shown) return <NoSignal compact title="Waiting for the first frame" body="" pulse />;
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={shown} alt={alt} className="absolute inset-0 h-full w-full object-cover" />;
}

/** The trip page's camera panel: head camera plus a link to the full Live section. */
export function LiveFeed({ className }: { className?: string }) {
  const mode = useFeed((s) => s.mode);
  const status = useTrip((s) => s.robot.status);
  const paused = useTrip((s) => s.paused);
  return (
    <div className={cx("relative flex flex-col overflow-hidden rounded-2xl bg-black text-white shadow-md", className)}>
      <div className="flex items-center gap-3 border-b border-white/10 px-4 py-2.5 text-[13px]">
        <span className="flex items-center gap-1.5 rounded-full bg-tomato px-2.5 py-0.5 text-[12px] font-bold">
          <span className={cx("size-1.5 rounded-full bg-white", !paused && "live-dot")} />
          {paused ? "Paused" : "Live"}
        </span>
        <span className="hidden text-white/75 sm:inline">Head camera, {MODE_LABEL[mode].toLowerCase()}</span>
        {mode === "gateway" && <span className="rounded-full bg-white/15 px-2 py-0.5 text-[11.5px]">Faces blurred</span>}
        <span className="ml-auto text-white/60 capitalize">{status}</span>
        <Link href="/live" className="flex items-center gap-1.5 rounded-full bg-white/10 px-3 py-1 text-white/90 hover:bg-white/20">
          All cameras <ExternalLink className="size-3.5" aria-hidden />
        </Link>
      </div>
      <div className="relative aspect-video w-full">
        <CameraView camera="head" inset />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------- simulated cameras

/** Shared canvas loop: (ctx, W, H, now) → draws one frame. */
function useCanvas(draw: (ctx: CanvasRenderingContext2D, W: number, H: number, now: number) => void, W = 960, H = 540) {
  const ref = useRef<HTMLCanvasElement>(null);
  const drawRef = useRef(draw);
  useEffect(() => {
    drawRef.current = draw;
  });
  useEffect(() => {
    const cv = ref.current!;
    const ctx = cv.getContext("2d")!;
    cv.width = W;
    cv.height = H;
    let raf = 0;
    const loop = (now: number) => {
      drawRef.current(ctx, W, H, now);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, [W, H]);
  return ref;
}

/** Wrist camera: close-up of whatever the gripper is working on. */
function WristCamera() {
  const ref = useCanvas((ctx, W, H, now) => {
    const s = useTrip.getState();
    const stop = s.activeStop ? s.stops[s.activeStop] : null;
    const working = stop?.sku && ["scanning", "picking", "waiting"].includes(s.robot.status);
    ctx.fillStyle = "#1d2124";
    ctx.fillRect(0, 0, W, H);
    if (!working || !stop?.sku) {
      ctx.fillStyle = "#2a2f33";
      for (let i = 0; i < 9; i++) ctx.fillRect(0, i * 64 + ((now / 40) % 64), W, 2);
      ctx.fillStyle = "rgba(255,255,255,0.75)";
      ctx.font = "600 26px ui-sans-serif, system-ui";
      ctx.textAlign = "center";
      ctx.fillText(s.phase === "idle" ? "Arm parked" : "Arm stowed while driving", W / 2, H / 2);
      ctx.textAlign = "left";
      drawHudLine(ctx, W, H, "WRIST", "stowed");
      return;
    }
    const p = productBySku[stop.sku];
    const t = (now % 4000) / 4000;
    const zoom = s.robot.status === "picking" ? 1.15 : 1 + 0.03 * Math.sin(t * Math.PI * 2);
    const bw = 420 * zoom,
      bh = 380 * zoom;
    const x = W / 2 - bw / 2,
      y = H / 2 - bh / 2 + 10;
    ctx.fillStyle = "#2c3236";
    ctx.fillRect(0, H * 0.78, W, H);
    ctx.fillStyle = p.label_color;
    ctx.beginPath();
    ctx.roundRect(x, y, bw, bh, 18);
    ctx.fill();
    ctx.fillStyle = "rgba(255,255,255,0.9)";
    ctx.fillRect(x + 24, y + bh * 0.32, bw - 48, 64 * zoom);
    ctx.fillStyle = "#111";
    ctx.font = `700 ${Math.round(30 * zoom)}px ui-sans-serif, system-ui`;
    ctx.fillText(p.name.slice(0, 22), x + 40, y + bh * 0.32 + 42 * zoom);
    // barcode
    for (let i = 0; i < 46; i++) {
      ctx.fillStyle = i % 3 === 0 ? "#111" : i % 2 ? "#111" : "transparent";
      ctx.fillRect(x + 40 + i * 6, y + bh - 90 * zoom, i % 4 === 0 ? 4 : 2, 56 * zoom);
    }
    // gripper fingers
    const close = s.robot.status === "picking" ? Math.min(1, ((now % 1500) / 1500) * 2) : 0;
    ctx.fillStyle = "#0f1214";
    ctx.fillRect(x - 70 + close * 40, 0, 60, H);
    ctx.fillRect(x + bw + 10 - close * 40, 0, 60, H);
    // scan line
    if (s.robot.status === "scanning") {
      ctx.fillStyle = "rgba(217,242,107,0.85)";
      ctx.fillRect(x, y + ((now / 6) % bh), bw, 3);
    }
    drawHudLine(ctx, W, H, "WRIST", s.robot.status === "picking" ? "grip closing" : s.robot.status);
  });
  return <canvas ref={ref} className="absolute inset-0 h-full w-full" aria-label="Simulated wrist camera" />;
}

/** Basket camera: top-down view of what's been picked, newest item checked. */
function BasketCamera() {
  const ref = useCanvas((ctx, W, H, now) => {
    const { cart } = useTrip.getState();
    const lines = cart.filter((c) => c.source === "in_store");
    ctx.fillStyle = "#24292c";
    ctx.fillRect(0, 0, W, H);
    // basket walls
    ctx.fillStyle = "#33393d";
    ctx.beginPath();
    ctx.roundRect(70, 50, W - 140, H - 100, 26);
    ctx.fill();
    ctx.strokeStyle = "rgba(255,255,255,0.12)";
    ctx.lineWidth = 3;
    for (let i = 1; i < 12; i++) {
      ctx.beginPath();
      ctx.moveTo(70 + ((W - 140) / 12) * i, 50);
      ctx.lineTo(70 + ((W - 140) / 12) * i, H - 50);
      ctx.stroke();
    }
    const items = lines.flatMap((c) => Array.from({ length: Math.min(c.qty, 3) }, (_, k) => ({ c, k })));
    items.slice(-18).forEach(({ c }, i) => {
      const p = productBySku[c.sku];
      const col = i % 6,
        row = Math.floor(i / 6);
      const x = 110 + col * 125,
        y = H - 160 - row * 120;
      ctx.fillStyle = p.label_color;
      ctx.beginPath();
      ctx.roundRect(x, y, 108, 100, 12);
      ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,0.85)";
      ctx.fillRect(x + 8, y + 36, 92, 22);
      ctx.fillStyle = "#111";
      ctx.font = "600 12px ui-sans-serif, system-ui";
      ctx.fillText(p.name.slice(0, 13), x + 12, y + 51);
    });
    const newest = lines[lines.length - 1];
    if (newest) {
      ctx.fillStyle = newest.verified ? "rgba(15,122,74,0.95)" : `rgba(242,165,22,${0.65 + 0.3 * Math.sin(now / 150)})`;
      ctx.beginPath();
      ctx.roundRect(90, 66, 360, 40, 20);
      ctx.fill();
      ctx.fillStyle = "#fff";
      ctx.font = "700 17px ui-sans-serif, system-ui";
      ctx.fillText(`${newest.verified ? "Checked" : "Checking"}: ${productBySku[newest.sku].name.slice(0, 24)}`, 110, 92);
    } else {
      ctx.fillStyle = "rgba(255,255,255,0.7)";
      ctx.font = "600 24px ui-sans-serif, system-ui";
      ctx.textAlign = "center";
      ctx.fillText("Basket is empty", W / 2, H / 2);
      ctx.textAlign = "left";
    }
    drawHudLine(ctx, W, H, "BASKET", `${lines.reduce((s, c) => s + c.qty, 0)} items`);
  });
  return <canvas ref={ref} className="absolute inset-0 h-full w-full" aria-label="Simulated basket camera" />;
}

function drawHudLine(ctx: CanvasRenderingContext2D, W: number, H: number, cam: string, state: string) {
  ctx.font = "12px ui-monospace, monospace";
  const text = `CAM ${cam}  ${state.toUpperCase()}`;
  ctx.fillStyle = "rgba(0,0,0,0.55)";
  ctx.fillRect(10, H - 32, ctx.measureText(text).width + 16, 22);
  ctx.fillStyle = "#f4f4f2";
  ctx.fillText(text, 18, H - 17);
}

type Face = { fixture: string; face: string };
function faceOf(slot: Slot): Face {
  const parts = slot.slot_id.split("-");
  const tag = parts[parts.length - 2]; // e.g. E1, W2, S0, N0
  return { fixture: slot.fixture_id, face: tag[0] };
}

export function SimCamera({ basketInset = true }: { basketInset?: boolean }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const cv = ref.current!;
    const ctx = cv.getContext("2d")!;
    let raf = 0;
    let statusSince = performance.now();
    let lastStatus = "";
    let lastStop: string | null = null;
    let odo = 0;
    let last = { x: 0, y: 0 };
    const W = 960,
      H = 540;
    cv.width = W;
    cv.height = H;

    const draw = (now: number) => {
      const s = useTrip.getState();
      const { robot, activeStop, stops, phase } = s;
      if (robot.status !== lastStatus || activeStop !== lastStop) {
        lastStatus = robot.status;
        lastStop = activeStop;
        statusSince = now;
      }
      odo += Math.hypot(robot.x - last.x, robot.y - last.y);
      last = { x: robot.x, y: robot.y };
      const t = (now - statusSince) / 1000;
      const stop = activeStop ? stops[activeStop] : null;
      const atShelf = stop?.sku && slotsOf(s.storeId)[stop.sku] && (robot.status === "scanning" || robot.status === "picking" || robot.status === "waiting");

      if (phase === "idle" || phase === "waiting_login") drawDock(ctx, W, H, now, s.storeId);
      else if (atShelf && stop?.sku) drawShelf(ctx, W, H, s.storeId, stop.sku, robot.status, t * s.speed, now);
      else drawCorridor(ctx, W, H, odo, robot, now);
      if (basketInset && phase !== "idle" && phase !== "waiting_login") drawBasketCam(ctx, W, H, s.cart, now);

      drawHud(ctx, W, H, robot, stop?.label ?? "", now);
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);
    return () => cancelAnimationFrame(raf);
  }, [basketInset]);
  return <canvas ref={ref} className="absolute inset-0 h-full w-full" aria-label="Simulated robot camera view" />;
}

function drawDock(ctx: CanvasRenderingContext2D, W: number, H: number, now: number, storeId: StoreId) {
  const g = ctx.createLinearGradient(0, 0, 0, H);
  g.addColorStop(0, "#2a302c");
  g.addColorStop(1, "#1a1e1b");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, W, H);
  // entrance doors
  ctx.fillStyle = "#3a423d";
  ctx.fillRect(W * 0.3, H * 0.18, W * 0.4, H * 0.55);
  ctx.strokeStyle = "#5c6a62";
  ctx.lineWidth = 3;
  ctx.strokeRect(W * 0.3, H * 0.18, W * 0.4, H * 0.55);
  ctx.beginPath();
  ctx.moveTo(W / 2, H * 0.18);
  ctx.lineTo(W / 2, H * 0.73);
  ctx.stroke();
  ctx.fillStyle = "#d93a30";
  ctx.font = "600 22px ui-monospace, monospace";
  ctx.textAlign = "center";
  ctx.fillText(STORES[storeId].name.toUpperCase(), W / 2, H * 0.13);
  ctx.fillStyle = "rgba(244,241,234," + (0.4 + 0.3 * Math.sin(now / 500)) + ")";
  ctx.font = "16px ui-monospace, monospace";
  ctx.fillText("robot docked · waiting for shopper", W / 2, H * 0.86);
  ctx.textAlign = "left";
}

function drawCorridor(ctx: CanvasRenderingContext2D, W: number, H: number, odo: number, robot: { x: number; y: number }, now: number) {
  const vx = W / 2,
    vy = H * 0.42;
  ctx.fillStyle = "#e9e4d8";
  ctx.fillRect(0, 0, W, H);
  // ceiling
  ctx.fillStyle = "#d8d2c3";
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(W, 0);
  ctx.lineTo(vx + 60, vy - 40);
  ctx.lineTo(vx - 60, vy - 40);
  ctx.fill();
  // floor
  ctx.fillStyle = "#cfc8b6";
  ctx.beginPath();
  ctx.moveTo(0, H);
  ctx.lineTo(W, H);
  ctx.lineTo(vx + 60, vy + 30);
  ctx.lineTo(vx - 60, vy + 30);
  ctx.fill();
  // floor tiles moving toward camera
  ctx.strokeStyle = "rgba(90,80,60,0.18)";
  ctx.lineWidth = 1;
  for (let k = 0; k < 14; k++) {
    const z = ((k + (odo * 1.6) % 1) / 14) ** 2;
    const y = vy + 30 + (H - vy - 30) * z;
    ctx.beginPath();
    ctx.moveTo(vx - 60 - (vx - 60) * z, y);
    ctx.lineTo(vx + 60 + (W - vx - 60) * z, y);
    ctx.stroke();
  }
  // shelves both sides with colored product blocks
  const colors = ["#e8c15a", "#d93a30", "#2f6b45", "#f0a830", "#6d3f74", "#3c6e9a", "#d9d2c2", "#8a6d00"];
  for (const side of [-1, 1]) {
    for (let level = 0; level < 4; level++) {
      for (let k = 0; k < 18; k++) {
        const z0 = ((k + (odo * 1.6) % 1) / 18) ** 2;
        const z1 = ((k + 1 + (odo * 1.6) % 1) / 18) ** 2;
        const xa = side < 0 ? (vx - 60) * (1 - z0) : vx + 60 + (W - vx - 60) * z0;
        const xb = side < 0 ? (vx - 60) * (1 - z1) : vx + 60 + (W - vx - 60) * z1;
        const top = (z: number) => vy - 40 - (vy - 40) * z * 0.9 + (level * (H * 0.9 * z + 70)) / 4.6;
        const bot = (z: number) => top(z) + (H * 0.9 * z + 70) / 5.6;
        const seed = Math.floor(k + Math.floor(odo * 1.6) + level * 7 + (side + 1) * 13 + Math.floor(robot.x));
        ctx.fillStyle = colors[Math.abs(seed) % colors.length];
        ctx.globalAlpha = 0.25 + 0.75 * z0;
        ctx.beginPath();
        ctx.moveTo(xa, top(z0));
        ctx.lineTo(xb, top(z1));
        ctx.lineTo(xb, bot(z1));
        ctx.lineTo(xa, bot(z0));
        ctx.fill();
      }
    }
  }
  ctx.globalAlpha = 1;
  // ceiling lights
  ctx.fillStyle = "rgba(255,255,255,0.7)";
  for (let k = 0; k < 6; k++) {
    const z = ((k + (odo * 0.8) % 1) / 6) ** 2;
    const w = 20 + 200 * z;
    ctx.fillRect(vx - w / 2, vy - 40 - (vy - 40) * z, w, 3 + 10 * z);
  }
  void now;
}

function drawShelf(ctx: CanvasRenderingContext2D, W: number, H: number, storeId: StoreId, sku: string, status: string, t: number, now: number) {
  const target = slotsOf(storeId)[sku];
  const f = faceOf(target);
  const slots = STORES[storeId].slots.filter((s) => s.fixture_id === f.fixture && faceOf(s).face === f.face);
  const levels = Math.max(...slots.map((s) => s.level)) + 1;
  const produce = target.fixture_id.startsWith("PRODUCE");
  const cols = Math.max(4, Math.max(...slots.map((s) => s.index)) + 1);

  ctx.fillStyle = produce ? "#d9cfb6" : "#e6e0d2";
  ctx.fillRect(0, 0, W, H);
  // back panel
  ctx.fillStyle = produce ? "#8a6a43" : "#cfc6b2";
  ctx.fillRect(W * 0.04, H * 0.06, W * 0.92, H * 0.86);

  const rows = produce ? 1 : Math.max(levels, 4);
  const rowH = (H * 0.86) / rows;
  const colW = (W * 0.92) / cols;
  const filler = ["#b9a98a", "#8f9c86", "#a88f7a", "#9aa5b1", "#c2b38f", "#a3918a"];
  let targetBox: { x: number; y: number; w: number; h: number } | null = null;

  for (let r = 0; r < rows; r++) {
    const level = rows - 1 - r; // level 0 at bottom
    const y0 = H * 0.06 + r * rowH;
    // shelf lip
    ctx.fillStyle = "#9a917c";
    ctx.fillRect(W * 0.04, y0 + rowH - 10, W * 0.92, 10);
    // other products on this shelf (not in the demo catalog): unlabeled stock so the shelf reads full
    if (!produce) {
      const used = new Set(slots.filter((q) => q.level === level).map((q) => q.index));
      for (let c = 0; c < cols; c++) {
        if (used.has(c)) continue;
        for (let k = 0; k < 3; k++) {
          const bw = (colW - 20) / 3;
          const bh = rowH * (0.5 + ((c + k + r) % 3) * 0.1);
          ctx.fillStyle = filler[(c * 3 + k + r * 5) % filler.length];
          ctx.fillRect(W * 0.04 + c * colW + 13 + k * bw, y0 + rowH - 10 - bh, bw - 6, bh);
        }
      }
    }
    ctx.fillStyle = "#f4f1ea";
    for (const s of slots.filter((q) => q.level === level)) {
      const p = productBySku[s.sku];
      const x0 = W * 0.04 + s.index * colW + 10;
      const n = produce ? 1 : Math.max(1, Math.min(4, s.facings + 1));
      const bw = (colW - 20) / n;
      for (let k = 0; k < n; k++) {
        const bh = produce ? rowH * 0.5 : rowH * (0.55 + ((s.index + k) % 3) * 0.08);
        const bx = x0 + k * bw + 3;
        const by = y0 + rowH - 10 - bh;
        if (produce) {
          // heap of round items
          for (let j = 0; j < 14; j++) {
            const cx = bx + 20 + ((j * 37) % (bw - 40));
            const cy = y0 + rowH * 0.45 + ((j * 23) % (rowH * 0.4));
            ctx.fillStyle = p.label_color;
            ctx.beginPath();
            ctx.arc(cx, cy, 16, 0, Math.PI * 2);
            ctx.fill();
            ctx.fillStyle = "rgba(0,0,0,0.15)";
            ctx.beginPath();
            ctx.arc(cx + 5, cy + 5, 10, 0, Math.PI * 2);
            ctx.fill();
          }
        } else {
          ctx.fillStyle = p.label_color;
          ctx.fillRect(bx, by, bw - 6, bh);
          ctx.fillStyle = "rgba(255,255,255,0.85)";
          ctx.fillRect(bx + 4, by + bh * 0.35, bw - 14, Math.min(28, bh * 0.3));
        }
        if (k === 0) {
          ctx.fillStyle = "#1c2420";
          ctx.font = "600 12px ui-sans-serif, system-ui";
          const name = p.name.length > 18 ? p.name.slice(0, 17) + "…" : p.name;
          if (!produce) ctx.fillText(name, bx + 7, by + bh * 0.35 + 18);
        }
      }
      // shelf tag
      ctx.fillStyle = s.deal ? "#f5e7c4" : "#fbf9f4";
      ctx.fillRect(x0, y0 + rowH - 10, 70, 18);
      ctx.fillStyle = "#1c2420";
      ctx.font = "11px ui-monospace, monospace";
      ctx.fillText(`$${((s.deal?.price_cents ?? p.price_cents) / 100).toFixed(2)}`, x0 + 6, y0 + rowH + 3);
      if (s.slot_id === target.slot_id) targetBox = { x: x0, y: y0 + 6, w: colW - 20, h: rowH - 18 };
    }
  }

  if (targetBox) {
    const b = targetBox;
    // pick: reach → grasp → lift → carry down into the basket (t is scaled by playback speed)
    if (status === "picking") drawPick(ctx, W, H, b, productBySku[sku], t);
    // reticle
    const pulse = 0.6 + 0.4 * Math.sin(now / 180);
    ctx.strokeStyle = status === "waiting" ? `rgba(201,132,0,${pulse})` : `rgba(217,58,48,${pulse})`;
    ctx.lineWidth = 3;
    const L = 22;
    const corners: [number, number, number, number][] = [
      [b.x, b.y, 1, 1],
      [b.x + b.w, b.y, -1, 1],
      [b.x, b.y + b.h, 1, -1],
      [b.x + b.w, b.y + b.h, -1, -1],
    ];
    for (const [x, y, dx, dy] of corners) {
      ctx.beginPath();
      ctx.moveTo(x + dx * L, y);
      ctx.lineTo(x, y);
      ctx.lineTo(x, y + dy * L);
      ctx.stroke();
    }
    if (status === "scanning") {
      const sy = b.y + ((t * 0.9) % 1) * b.h;
      ctx.fillStyle = "rgba(217,58,48,0.55)";
      ctx.fillRect(b.x, sy, b.w, 2);
    }
    // label chip
    const p = productBySku[sku];
    const txt =
      status === "scanning"
        ? `SCAN ${target.slot_id} · ${p.name.toUpperCase()}`
        : status === "picking"
          ? `PICK ${p.name.toUpperCase()} → BASKET`
          : `WAITING FOR SHOPPER · ${p.name.toUpperCase()}`;
    ctx.font = "600 13px ui-monospace, monospace";
    const tw = ctx.measureText(txt).width + 16;
    const lx = Math.min(W - tw - 10, Math.max(10, b.x));
    const ly = Math.max(44, b.y - 28);
    ctx.fillStyle = status === "waiting" ? "#c98400" : "#d93a30";
    ctx.fillRect(lx, ly, tw, 22);
    ctx.fillStyle = "#fff";
    ctx.fillText(txt, lx + 8, ly + 15);
    if (status === "scanning" && t > 0.4) {
      const conf = Math.min(0.99, 0.71 + t * 0.2).toFixed(2);
      ctx.fillStyle = "rgba(18,22,20,0.8)";
      ctx.fillRect(lx, ly + 24, 230, 20);
      ctx.fillStyle = "#9fd3ad";
      ctx.font = "12px ui-monospace, monospace";
      ctx.fillText(`match ${conf} · ${target.qty} on shelf`, lx + 8, ly + 38);
    }
  }
}

function drawHud(ctx: CanvasRenderingContext2D, W: number, H: number, robot: { x: number; y: number; theta?: number; status: string; speed: number }, next: string, now: number) {
  // vignette + scanlines
  const v = ctx.createRadialGradient(W / 2, H / 2, H * 0.35, W / 2, H / 2, H * 0.95);
  v.addColorStop(0, "rgba(0,0,0,0)");
  v.addColorStop(1, "rgba(0,0,0,0.45)");
  ctx.fillStyle = v;
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = "rgba(0,0,0,0.05)";
  for (let y = 0; y < H; y += 3) ctx.fillRect(0, y, W, 1);

  ctx.font = "12px ui-monospace, monospace";
  ctx.fillStyle = "rgba(18,22,20,0.65)";
  ctx.fillRect(10, 10, 182, 22);
  ctx.fillRect(W - 152, 10, 142, 22);
  ctx.fillStyle = "#f4f1ea";
  ctx.fillText("CAM head · 960×540 · 15fps", 18, 25);
  const d = new Date(now + Date.UTC(2026, 9, 18, 2, 30) - performance.now());
  ctx.fillText(d.toISOString().slice(11, 19) + " UTC", W - 140, 25);

  const line = `x ${robot.x.toFixed(2)}  y ${robot.y.toFixed(2)}  θ ${(robot.theta ?? 0).toFixed(2)}  v ${robot.speed.toFixed(1)} m/s${next ? `  → ${next}` : ""}`;
  ctx.fillStyle = "rgba(18,22,20,0.65)";
  ctx.fillRect(10, H - 32, ctx.measureText(line).width + 16, 22);
  ctx.fillStyle = "#f4f1ea";
  ctx.fillText(line, 18, H - 17);
}

function ease(x: number) {
  return x < 0.5 ? 2 * x * x : 1 - (-2 * x + 2) ** 2 / 2;
}

/** Two-link arm from the robot (bottom centre) to the target; the item rides in the gripper down to the basket. */
function drawPick(
  ctx: CanvasRenderingContext2D,
  W: number,
  H: number,
  b: { x: number; y: number; w: number; h: number },
  p: { name: string; label_color: string },
  t: number,
) {
  const k = Math.min(1, t / 1.45);
  const shoulder = { x: W * 0.5, y: H + 30 };
  const grab = { x: b.x + b.w / 2, y: b.y + b.h * 0.55 };
  const basket = { x: W * 0.5, y: H * 0.86 };
  let hand: { x: number; y: number };
  let carrying = false;
  if (k < 0.35) {
    const e = ease(k / 0.35);
    hand = { x: shoulder.x + (grab.x - shoulder.x) * e, y: H * 0.75 + (grab.y - H * 0.75) * e };
  } else if (k < 0.5) {
    hand = grab;
    carrying = k > 0.43;
  } else {
    const e = ease((k - 0.5) / 0.5);
    carrying = true;
    hand = { x: grab.x + (basket.x - grab.x) * e, y: grab.y - 40 * Math.sin(Math.PI * e) + (basket.y - grab.y) * e };
  }
  // elbow: perpendicular offset from the shoulder→hand midpoint
  const mx = (shoulder.x + hand.x) / 2,
    my = (shoulder.y + hand.y) / 2;
  const dx = hand.x - shoulder.x,
    dy = hand.y - shoulder.y;
  const len = Math.hypot(dx, dy) || 1;
  const bend = Math.max(0, 260 - len * 0.35);
  const elbow = { x: mx + (-dy / len) * bend, y: my + (dx / len) * bend };
  ctx.lineCap = "round";
  ctx.strokeStyle = "#1c2420";
  ctx.lineWidth = 26;
  ctx.beginPath();
  ctx.moveTo(shoulder.x, shoulder.y);
  ctx.lineTo(elbow.x, elbow.y);
  ctx.lineTo(hand.x, hand.y);
  ctx.stroke();
  ctx.strokeStyle = "#e8e3d6";
  ctx.lineWidth = 6;
  ctx.beginPath();
  ctx.moveTo(shoulder.x, shoulder.y);
  ctx.lineTo(elbow.x, elbow.y);
  ctx.lineTo(hand.x, hand.y);
  ctx.stroke();
  ctx.fillStyle = "#d93a30";
  ctx.beginPath();
  ctx.arc(elbow.x, elbow.y, 13, 0, Math.PI * 2);
  ctx.fill();
  // the item
  if (carrying) {
    const iw = Math.min(110, b.w * 0.55),
      ih = Math.min(130, b.h * 0.8);
    const s = k > 0.5 ? 1 - 0.35 * ease((k - 0.5) / 0.5) : 1;
    ctx.fillStyle = p.label_color;
    ctx.fillRect(hand.x - (iw * s) / 2, hand.y - ih * s - 6, iw * s, ih * s);
    ctx.fillStyle = "rgba(255,255,255,0.88)";
    ctx.fillRect(hand.x - (iw * s) / 2 + 5, hand.y - ih * s * 0.6, iw * s - 10, 18 * s);
    ctx.fillStyle = "#1c2420";
    ctx.font = `600 ${Math.round(11 * s)}px ui-sans-serif, system-ui`;
    ctx.fillText(p.name.slice(0, 14), hand.x - (iw * s) / 2 + 8, hand.y - ih * s * 0.6 + 13 * s);
  }
  // gripper fingers
  const open = carrying ? 0 : 14;
  ctx.fillStyle = "#1c2420";
  ctx.fillRect(hand.x - 26 - open, hand.y - 10, 12, 30);
  ctx.fillRect(hand.x + 14 + open, hand.y - 10, 12, 30);
  ctx.fillRect(hand.x - 26 - open, hand.y + 14, 52 + open * 2, 10);
  // basket rim at the bottom of the frame
  ctx.strokeStyle = "#3d4640";
  ctx.lineWidth = 8;
  ctx.beginPath();
  ctx.moveTo(W * 0.32, H - 6);
  ctx.lineTo(W * 0.36, H * 0.9);
  ctx.lineTo(W * 0.64, H * 0.9);
  ctx.lineTo(W * 0.68, H - 6);
  ctx.stroke();
  ctx.lineCap = "butt";
}

/** Picture-in-picture from the basket camera: what's in the basket, newest item highlighted once verified. */
function drawBasketCam(
  ctx: CanvasRenderingContext2D,
  W: number,
  H: number,
  cart: { sku: string; qty: number; source: string; verified: boolean | null }[],
  now: number,
) {
  const pw = 232,
    ph = 138,
    px = W - pw - 12,
    py = 40; // top-right, clear of the arm sweeping up from the bottom
  ctx.fillStyle = "rgba(18,22,20,0.92)";
  ctx.fillRect(px, py, pw, ph);
  ctx.strokeStyle = "rgba(244,241,234,0.35)";
  ctx.lineWidth = 1;
  ctx.strokeRect(px + 0.5, py + 0.5, pw - 1, ph - 1);
  ctx.fillStyle = "#f4f1ea";
  ctx.font = "11px ui-monospace, monospace";
  ctx.fillText("BASKET CAM", px + 8, py + 15);
  const lines = cart.filter((c) => c.source === "in_store");
  const items = lines.flatMap((c) => Array.from({ length: Math.min(c.qty, 4) }, () => c));
  ctx.fillStyle = "rgba(244,241,234,0.55)";
  ctx.textAlign = "right";
  ctx.fillText(`${lines.reduce((s, c) => s + c.qty, 0)} items`, px + pw - 8, py + 15);
  ctx.textAlign = "left";
  // basket floor
  ctx.fillStyle = "#2c332f";
  ctx.fillRect(px + 10, py + 24, pw - 20, ph - 34);
  const cols = 8;
  items.slice(-24).forEach((c, i) => {
    const col = i % cols,
      row = Math.floor(i / cols);
    const cw = (pw - 28) / cols;
    const x = px + 14 + col * cw,
      y = py + ph - 14 - (row + 1) * 30;
    ctx.fillStyle = productBySku[c.sku]?.label_color ?? "#999";
    ctx.fillRect(x, y, cw - 3, 26);
  });
  const newest = lines[lines.length - 1];
  if (newest) {
    const ok = newest.verified;
    ctx.fillStyle = ok ? "rgba(15,122,74,0.95)" : `rgba(201,132,0,${0.6 + 0.3 * Math.sin(now / 150)})`;
    ctx.font = "600 11px ui-monospace, monospace";
    const label = `${ok ? "✓ VERIFIED" : "… CHECKING"} ${productBySku[newest.sku]?.name.toUpperCase().slice(0, 16) ?? ""}`;
    const tw = ctx.measureText(label).width + 12;
    ctx.fillRect(px + 10, py + 22, tw, 18);
    ctx.fillStyle = "#fff";
    ctx.fillText(label, px + 16, py + 35);
  }
}
