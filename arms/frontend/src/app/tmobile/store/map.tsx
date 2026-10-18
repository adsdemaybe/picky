"use client";
// Top-down T-Mobile floor (store meters, y up) for the In store tab: fixtures, the robot, the shopper following it,
// the path it's walking and the fixture we're headed to. Geometry sits in a y-flipped group; text stays upright.
import { tmobileStore } from "@/data/stores/tmobile";
import type { Pose } from "@/lib/data";
import { fixtureName } from "@/lib/associate";
import { cn } from "@/lib/utils";

const LABEL: Record<string, string> = {
  "ACC-WALL": "Accessories",
  "TABLE-A": "Apple · value",
  "TABLE-B": "Samsung · Google",
  "TABLE-C": "Samsung",
  "AUDIO-WALL": "Audio",
  "HOME-INET": "Home Internet",
  "WEAR-CASE": "Watches · tablets · internet",
  COUNTER: "Counter",
  "BACK-STOCK": "Back room",
};

export function StoreFloor({ robot, you, path, target, className }: { robot: Pose; you: Pose | null; path: Pose[]; target: string | null; className?: string }) {
  const B = tmobileStore.bounds;
  const fy = (y: number) => B.h - y;
  const d = path.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ");
  return (
    <svg viewBox={`-0.3 -0.3 ${B.w + 0.6} ${B.h + 0.9}`} className={cn("h-auto w-full", className)} role="img" aria-label={target ? `Store map: heading to ${fixtureName(target)}` : "Store map"}>
      <rect x={0} y={0} width={B.w} height={B.h} rx={0.25} fill="var(--card)" stroke="var(--border)" strokeWidth={0.06} />
      <g transform={`translate(0 ${B.h}) scale(1 -1)`}>
        {tmobileStore.fixtures.map((f) => {
          const on = f.fixture_id === target;
          const staff = f.fixture_id === "BACK-STOCK";
          return (
            <rect
              key={f.fixture_id}
              x={f.rect.x}
              y={f.rect.y}
              width={f.rect.w}
              height={f.rect.h}
              rx={0.08}
              fill={on ? "var(--color-magenta)" : staff ? "var(--muted)" : "var(--color-magenta-wash)"}
              stroke={on ? "var(--color-magenta-deep)" : "var(--input)"}
              strokeWidth={0.04}
              strokeDasharray={staff ? "0.12 0.08" : undefined}
              className={on ? "animate-pulse" : undefined}
            />
          );
        })}
        {/* entrance */}
        <rect x={tmobileStore.entrance.x - 0.7} y={-0.06} width={1.4} height={0.12} fill="var(--color-magenta)" />
        {d && <path d={d} fill="none" stroke="var(--color-magenta)" strokeWidth={0.07} strokeDasharray="0.18 0.12" strokeLinecap="round" strokeLinejoin="round" opacity={0.85} />}
        {you && (
          <g transform={`translate(${you.x} ${you.y})`}>
            <circle r={0.26} fill="var(--color-sky)" opacity={0.2} />
            <circle r={0.14} fill="var(--color-sky)" stroke="white" strokeWidth={0.04} />
          </g>
        )}
        <g transform={`translate(${robot.x} ${robot.y}) rotate(${((robot.theta ?? Math.PI / 2) * 180) / Math.PI})`}>
          <circle r={0.32} fill="var(--color-magenta)" opacity={0.18} />
          <circle r={0.2} fill="var(--color-magenta)" stroke="white" strokeWidth={0.05} />
          <path d="M0.08,-0.09 L0.26,0 L0.08,0.09 Z" fill="white" />
        </g>
      </g>
      {tmobileStore.fixtures.map((f) => (
        <text
          key={f.fixture_id}
          x={f.rect.x + f.rect.w / 2}
          y={fy(f.rect.y + f.rect.h / 2) + 0.1}
          textAnchor="middle"
          fontSize={0.3}
          fontWeight={600}
          fill={f.fixture_id === target ? "white" : "var(--foreground)"}
          transform={f.rect.h > f.rect.w * 2 ? `rotate(-90 ${f.rect.x + f.rect.w / 2} ${fy(f.rect.y + f.rect.h / 2)})` : undefined}
        >
          {LABEL[f.fixture_id] ?? f.fixture_id}
        </text>
      ))}
      <text x={tmobileStore.entrance.x} y={B.h + 0.45} textAnchor="middle" fontSize={0.28} fill="var(--muted-foreground)">
        Entrance
      </text>
    </svg>
  );
}
