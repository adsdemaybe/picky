"use client";
// Camera panels for the projector page (/watch). Each panel walks a ladder of sources and its header says which one
// is on air, so a simulation is never passed off as something else:
//   gateway MJPEG (<img>, one connection) → polled snapshot.jpg → a fallback gateway camera → the in-browser replay
// A rung fails on an <img> error, on no first frame within 8 s, or after 3 snapshot errors in a row; rungs above the
// current one are re-probed every 15 s, so the Isaac stream comes back by itself when the worker (re)connects. While
// GET /health is down the gateway rungs are skipped. The gateway face-blurs every frame before serving it (CLAUDE.md §8).
import { Radio, VideoOff } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { cameraUrl, gw } from "@/lib/gateway/client";
import { useGateway } from "@/lib/gateway/useGateway";
import { cn } from "@/lib/utils";
import { SimCamera, WristCamera } from "./LiveFeed";
import { StoreMap } from "./StoreMap";

const RETRY_MS = 15_000;
const FIRST_FRAME_MS = 8_000;

type Meta = { title: string; badge: string; live?: boolean; note?: string };
export type Rung =
  | (Meta & { kind: "mjpeg"; src: string; probe: string })
  | (Meta & { kind: "snapshot"; src: string; every: number })
  | (Meta & { kind: "node"; node: ReactNode });

const probeOf = (r: Rung) => (r.kind === "mjpeg" ? r.probe : r.kind === "snapshot" ? r.src : null);
const withT = (u: string) => `${u}${u.includes("?") ? "&" : "?"}t=${Date.now()}`;

function useLadder(rungs: Rung[]) {
  const [idx, setIdx] = useState(0);
  const down = useGateway((s) => s.status === "down");
  const local = rungs.findIndex((r) => r.kind === "node");
  const at = Math.min(down && local >= 0 ? Math.max(idx, local) : idx, rungs.length - 1);

  const fail = useCallback((i: number) => setIdx((cur) => (cur === i ? Math.min(i + 1, rungs.length - 1) : cur)), [rungs.length]);

  // re-probe the better rungs (their snapshot URL); jump back to the best one that answers
  const rungsRef = useRef(rungs);
  useEffect(() => {
    rungsRef.current = rungs;
  });
  useEffect(() => {
    if (idx === 0) return;
    let alive = true;
    const t = setInterval(() => {
      rungsRef.current.slice(0, idx).forEach((r, i) => {
        const url = probeOf(r);
        if (!url) return;
        const img = new Image();
        img.onload = () => alive && setIdx((cur) => Math.min(cur, i));
        img.src = withT(url);
      });
    }, RETRY_MS);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [idx]);

  return { at, rung: rungs[at], fail };
}

function RungView({ rung, i, alt, fail }: { rung: Rung; i: number; alt: string; fail: (i: number) => void }) {
  if (rung.kind === "mjpeg") return <Mjpeg src={rung.src} alt={alt} onFail={() => fail(i)} />;
  if (rung.kind === "snapshot") return <Snapshots src={rung.src} every={rung.every} alt={alt} onFail={() => fail(i)} />;
  return <>{rung.node}</>;
}

/** A big camera panel: header (source badge + title + note) over the picture. `sig` changes → start from the top. */
export function CameraPanel({ rungs, alt, aspect = "16 / 9", overlay, onRung, className }: { rungs: Rung[]; alt: string; aspect?: string; overlay?: ReactNode; onRung?: (r: Rung) => void; className?: string }) {
  const sig = rungs.map((r) => (r.kind === "node" ? `node:${r.title}` : r.src)).join("|");
  return <Panel key={sig} rungs={rungs} alt={alt} aspect={aspect} overlay={overlay} onRung={onRung} className={className} />;
}

function Panel({ rungs, alt, aspect, overlay, onRung, className }: { rungs: Rung[]; alt: string; aspect: string; overlay?: ReactNode; onRung?: (r: Rung) => void; className?: string }) {
  const { at, rung, fail } = useLadder(rungs);
  const latest = useRef({ onRung, rung });
  useEffect(() => {
    latest.current = { onRung, rung };
  });
  useEffect(() => {
    latest.current.onRung?.(latest.current.rung);
  }, [at]);
  return (
    <section className={cn("flex flex-col overflow-hidden rounded-2xl bg-black text-white shadow-md", className)} aria-label={`${rung.title}: ${rung.badge}`}>
      <header className="flex min-h-11 items-center gap-2.5 border-b border-white/10 px-3.5 py-2">
        <span className={cn("flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-0.5 text-[12.5px] font-bold whitespace-nowrap", rung.live ? "bg-tomato" : "bg-white/15")}>
          <span className={cn("size-1.5 rounded-full bg-white", rung.live && "live-dot")} />
          {rung.badge}
        </span>
        <span className="min-w-0 truncate text-[14.5px] font-semibold">{rung.title}</span>
        <span className="ml-auto flex shrink-0 items-center gap-2 text-[12.5px] text-white/65">
          {rung.note && <span className="hidden lg:inline">{rung.note}</span>}
          {rung.kind !== "node" && <span className="rounded-full bg-white/15 px-2 py-0.5 text-[11.5px] text-white">Faces blurred</span>}
        </span>
      </header>
      <div className="relative w-full bg-black" style={{ aspectRatio: aspect }}>
        <RungView key={at} rung={rung} i={at} alt={alt} fail={fail} />
        {overlay}
      </div>
    </section>
  );
}

/** Small picture-in-picture camera (e.g. the wrist cam over the on-board view). */
export function CameraInset({ rungs, alt, className }: { rungs: Rung[]; alt: string; className?: string }) {
  const sig = rungs.map((r) => (r.kind === "node" ? `node:${r.title}` : r.src)).join("|");
  return <Inset key={sig} rungs={rungs} alt={alt} className={className} />;
}

function Inset({ rungs, alt, className }: { rungs: Rung[]; alt: string; className?: string }) {
  const { at, rung, fail } = useLadder(rungs);
  return (
    <div className={cn("relative aspect-[4/3] overflow-hidden rounded-lg bg-black ring-1 ring-white/30", className)}>
      <RungView key={at} rung={rung} i={at} alt={alt} fail={fail} />
      <span className="absolute bottom-1 left-1 rounded bg-black/70 px-1.5 py-0.5 text-[10.5px] font-semibold text-white">{rung.badge}</span>
    </div>
  );
}

/** MJPEG renders natively in <img>. The src is set in an effect and removed on unmount so the stream connection
 *  closes (browsers allow ~6 connections per host). */
function Mjpeg({ src, alt, onFail }: { src: string; alt: string; onFail: () => void }) {
  const ref = useRef<HTMLImageElement>(null);
  const failRef = useRef(onFail);
  useEffect(() => {
    failRef.current = onFail;
  });
  useEffect(() => {
    const img = ref.current;
    if (!img) return;
    img.src = src;
    const watchdog = setTimeout(() => !img.naturalWidth && failRef.current(), FIRST_FRAME_MS);
    return () => {
      clearTimeout(watchdog);
      img.removeAttribute("src");
    };
  }, [src]);
  return (
    // eslint-disable-next-line @next/next/no-img-element
    <img ref={ref} alt={alt} onError={() => ref.current?.getAttribute("src") && failRef.current()} className="absolute inset-0 h-full w-full object-contain" />
  );
}

/** snapshot.jpg polling with preload (no flicker). 3 errors in a row → fail (404 unknown camera, 503 no frame). */
function Snapshots({ src, every, alt, onFail }: { src: string; every: number; alt: string; onFail: () => void }) {
  const [shown, setShown] = useState<string | null>(null);
  const failRef = useRef(onFail);
  useEffect(() => {
    failRef.current = onFail;
  });
  useEffect(() => {
    let alive = true;
    let errs = 0;
    const tick = () => {
      const url = withT(src);
      const img = new Image();
      img.onload = () => {
        if (!alive) return;
        errs = 0;
        setShown(url);
      };
      img.onerror = () => {
        if (alive && ++errs >= 3) failRef.current();
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
  if (!shown) return <Waiting title="Waiting for the first frame" pulse />;
  // eslint-disable-next-line @next/next/no-img-element
  return <img src={shown} alt={alt} className="absolute inset-0 h-full w-full object-contain" />;
}

export function Waiting({ title, body, pulse }: { title: string; body?: string; pulse?: boolean }) {
  return (
    <div className="absolute inset-0 grid place-items-center bg-[#16181a] p-6 text-center text-white">
      <div>
        <span className={cn("mx-auto grid size-12 place-items-center rounded-full bg-white/10", pulse && "live-dot")}>
          {pulse ? <Radio className="size-6" /> : <VideoOff className="size-6" />}
        </span>
        <div className="mt-3 font-semibold">{title}</div>
        {body && <div className="mt-1 max-w-[44ch] text-[13px] text-white/60">{body}</div>}
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------------------------------ ladders

type LadderOpts = {
  /** null = no gateway robot (demo replay, or not assigned yet) */
  robotId: string | null;
  storeId: string;
  base: string;
  /** who renders a camera: "Isaac Sim" | "2D sim" (useSimNames) */
  simOf: (cameraId: string) => string;
  /** the real cameras arm is on (PICKY_REAL_ARMS=cameras): boxes come from Perceptron Mk1.5, else ground truth */
  perceptron: boolean;
  /** a gateway trip still waiting for its robot */
  waiting?: boolean;
};

/** Who renders each camera of this robot and store (GET /api/cameras → CameraInfo.source), re-read every 20 s, so a
 *  badge never says Isaac for the 2D fake. Unknown cameras fall back to GET /health sim_connected. */
export function useSimNames(robotId: string | null, storeId: string, base: string, isaac: boolean) {
  const [sources, setSources] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!robotId || !base) return;
    let alive = true;
    const load = () =>
      Promise.all([gw.cameras({ robot_id: robotId }), gw.cameras({ store_id: storeId })])
        .then((rs) => alive && setSources(Object.fromEntries(rs.flatMap((r) => r.data).map((c) => [c.camera_id, c.source ?? ""]))))
        .catch(() => {});
    void load();
    const t = setInterval(load, 20_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [robotId, storeId, base]);
  return useCallback((id: string) => (sources[id] === "isaac" ? "Isaac Sim" : sources[id] === "fake" ? "2D sim" : isaac ? "Isaac Sim" : "2D sim"), [sources, isaac]);
}

const mapNode = (
  <div className="absolute inset-0 grid place-items-center bg-[#f7f8f6] p-4">
    <StoreMap />
  </div>
);

/** Big panel: the Isaac director (chase) camera, then the store ceiling camera, then the replay's map. */
export function directorRungs({ robotId, storeId, base, simOf, waiting }: LadderOpts): Rung[] {
  const local: Rung = waiting
    ? { kind: "node", title: "Assigning a robot", badge: "Standby", node: <Waiting title="Picking the closest free robot" body="The live picture starts as soon as the fleet assigns one." pulse /> }
    : { kind: "node", title: "Store map", badge: "Demo replay", note: "Robot position from the trip", node: mapNode };
  if (!robotId || !base) return [local];
  const dir = `${robotId}.director`;
  const ceil = `${storeId}.ceiling-1`;
  const sim = simOf(ceil);
  return [
    { kind: "mjpeg", title: "Chase camera", badge: `Live · ${simOf(dir)}`, live: true, note: "Director camera", src: cameraUrl(dir, "stream", { fps: 10, w: 1280 }, base), probe: cameraUrl(dir, "snapshot", { w: 320 }, base) },
    { kind: "snapshot", title: "Chase camera", badge: `${simOf(dir)} · snapshots`, live: true, note: "Stream reconnecting", src: cameraUrl(dir, "snapshot", { w: 1280 }, base), every: 500 },
    { kind: "mjpeg", title: "Store ceiling camera", badge: `Live · ${sim}`, live: true, note: "Chase camera offline", src: cameraUrl(ceil, "stream", { fps: 8, w: 1280 }, base), probe: cameraUrl(ceil, "snapshot", { w: 320 }, base) },
    { kind: "snapshot", title: "Store ceiling camera", badge: `${sim} · snapshots`, live: true, src: cameraUrl(ceil, "snapshot", { w: 1280 }, base), every: 800 },
    local,
  ];
}

/** Big panel on a store ceiling camera (the bird's-eye view of the floor), then its snapshots, then the replay's map. */
export function overheadRungs({ robotId, base, simOf, waiting }: LadderOpts, cameraId: string, title: string): Rung[] {
  const local: Rung = waiting
    ? { kind: "node", title: "Assigning a robot", badge: "Standby", node: <Waiting title="Picking the closest free robot" body="The live picture starts as soon as the fleet assigns one." pulse /> }
    : { kind: "node", title: "Store map", badge: "Demo replay", note: "Robot position from the trip", node: mapNode };
  if (!robotId || !base) return [local];
  const sim = simOf(cameraId);
  return [
    { kind: "mjpeg", title, badge: `Live · ${sim}`, live: true, note: "Store ceiling camera", src: cameraUrl(cameraId, "stream", { fps: 8, w: 1280 }, base), probe: cameraUrl(cameraId, "snapshot", { w: 320 }, base) },
    { kind: "snapshot", title, badge: `${sim} · snapshots`, live: true, note: "Stream reconnecting", src: cameraUrl(cameraId, "snapshot", { w: 1280 }, base), every: 800 },
    local,
  ];
}

/** The store's ceiling cameras (GET /api/cameras?store_id, kind 'ceiling'): the bird's-eye views /watch can switch to. */
export function useCeilingCameras(storeId: string, base: string, enabled: boolean) {
  const [cams, setCams] = useState<{ id: string; name: string }[]>([]);
  useEffect(() => {
    if (!enabled || !base) return;
    let alive = true;
    gw.cameras({ store_id: storeId })
      .then((r) => alive && setCams(r.data.filter((c) => c.kind === "ceiling").map((c) => ({ id: c.camera_id, name: c.name ?? c.camera_id }))))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [storeId, base, enabled]);
  return cams;
}

/** Second panel: the robot's front camera with Perceptron boxes, then the raw front camera, then the replay camera. */
export function perceivedRungs({ robotId, base, simOf, perceptron, waiting }: LadderOpts): Rung[] {
  const local: Rung = waiting
    ? { kind: "node", title: "What the robot sees", badge: "Standby", node: <Waiting title="No robot yet" pulse /> }
    : { kind: "node", title: "What the robot sees", badge: "Demo replay", note: "Simulated camera", node: <SimCamera basketInset={false} /> };
  if (!robotId || !base) return [local];
  const per = `${robotId}.front.perceived`;
  const raw = `${robotId}.front`;
  const sim = simOf(raw);
  const boxes = perceptron ? "Perceptron Mk1.5" : "ground-truth boxes";
  return [
    { kind: "mjpeg", title: "What the robot sees", badge: `On-board camera · ${boxes}`, live: true, src: cameraUrl(per, "stream", { fps: 10, w: 960 }, base), probe: cameraUrl(per, "snapshot", { w: 320 }, base) },
    { kind: "snapshot", title: "What the robot sees", badge: `On-board camera · ${boxes}`, live: true, note: "snapshots", src: cameraUrl(per, "snapshot", { w: 960 }, base), every: 700 },
    { kind: "mjpeg", title: "What the robot sees", badge: `On-board camera · ${sim}`, live: true, note: "No detection boxes right now", src: cameraUrl(raw, "stream", { fps: 8, w: 960 }, base), probe: cameraUrl(raw, "snapshot", { w: 320 }, base) },
    { kind: "snapshot", title: "What the robot sees", badge: `On-board camera · ${sim}`, live: true, src: cameraUrl(raw, "snapshot", { w: 960 }, base), every: 900 },
    local,
  ];
}

/** Inset: the wrist camera (Perceptron boxes when available), polled. */
export function wristRungs({ robotId, base, perceptron, waiting }: LadderOpts): Rung[] {
  const local: Rung = waiting
    ? { kind: "node", title: "Wrist", badge: "Wrist", node: <Waiting title="" /> }
    : { kind: "node", title: "Wrist", badge: "Wrist · replay", node: <WristCamera /> };
  if (!robotId || !base) return [local];
  return [
    { kind: "snapshot", title: "Wrist", badge: perceptron ? "Wrist · Perceptron" : "Wrist · boxes", live: true, src: cameraUrl(`${robotId}.wrist.perceived`, "snapshot", { w: 360 }, base), every: 1200 },
    { kind: "snapshot", title: "Wrist", badge: "Wrist", live: true, src: cameraUrl(`${robotId}.wrist`, "snapshot", { w: 360 }, base), every: 1500 },
    local,
  ];
}
