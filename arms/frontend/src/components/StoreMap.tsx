"use client";
// Top-down store map in store meters (world frame: x right, y up, origin bottom-left).
// Geometry lives inside a y-flipped group; text stays outside it so it isn't mirrored.
import { STORES } from "@/lib/data";
import { useTrip, type Stop, type StopStatus } from "@/lib/trip";

export function StoreMap({ compact = false, poseOverride }: { compact?: boolean; poseOverride?: { x: number; y: number; theta?: number } }) {
  const { route, stops, stopStatus, robot: simRobot, baselinePath, activeStop, offRoute, storeId } = useTrip();
  const robot = poseOverride ? { ...simRobot, ...poseOverride } : simRobot;
  const store = STORES[storeId];
  const B = store.bounds;
  const fy = (y: number) => B.h - y; // world y → svg y for text outside the flipped group
  const grocery = store.kind === "grocery";

  const legPaths =
    route?.legs.map((l) => ({
      key: `${l.from}>${l.to}`,
      detour: offRoute.includes(l.from) || offRoute.includes(l.to),
      d: l.path.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" "),
    })) ?? [];
  const base = baselinePath.length ? baselinePath.map((p, i) => `${i ? "L" : "M"}${p.x.toFixed(2)},${p.y.toFixed(2)}`).join(" ") : "";
  const order = route?.order ?? [];

  return (
    <svg viewBox={`-0.4 -0.4 ${B.w + 0.8} ${B.h + 0.8}`} className="h-full w-full" role="img" aria-label="Store map with the robot's route">
      <rect x={0} y={0} width={B.w} height={B.h} rx={0.3} fill="var(--card)" stroke="var(--border)" strokeWidth={0.08} />
      <g transform={`translate(0 ${B.h}) scale(1 -1)`}>
        {store.fixtures.map((f) => (
          <rect
            key={f.fixture_id}
            x={f.rect.x}
            y={f.rect.y}
            width={f.rect.w}
            height={f.rect.h}
            fill={f.kind === "produce_bin" ? "var(--color-leaf-wash)" : f.kind === "fridge" || f.kind === "freezer" ? "#dfe6ec" : "var(--muted)"}
            stroke="var(--input)"
            strokeWidth={0.03}
          />
        ))}
        {/* checkout lane + entrance */}
        {grocery && <rect x={10.4} y={0.6} width={2.2} height={1.4} fill="none" stroke="var(--input)" strokeWidth={0.03} strokeDasharray="0.15 0.1" />}
        <rect x={store.entrance.x - 0.8} y={0} width={1.6} height={0.12} fill="var(--color-tomato)" />

        {base && <path d={base} fill="none" stroke="var(--muted-foreground)" strokeWidth={0.05} strokeDasharray="0.12 0.12" opacity={0.55} />}
        {legPaths.map((l) => (
          <path
            key={l.key}
            d={l.d}
            fill="none"
            stroke={l.detour ? "var(--color-amber)" : "var(--foreground)"}
            strokeWidth={l.detour ? 0.11 : 0.09}
            strokeDasharray={l.detour ? "0.3 0.18" : undefined}
            strokeLinejoin="round"
            strokeLinecap="round"
          />
        ))}

        {/* robot: body + camera wedge */}
        <g transform={`translate(${robot.x} ${robot.y}) rotate(${((robot.theta ?? 0) * 180) / Math.PI})`}>
          <path d="M0,0 L2.2,-0.9 L2.2,0.9 Z" fill="var(--color-leaf)" opacity={0.16} />
          <rect x={-0.34} y={-0.29} width={0.68} height={0.58} rx={0.14} fill="var(--color-leaf)" stroke="white" strokeWidth={0.06} />
          <rect x={0.16} y={-0.12} width={0.14} height={0.24} rx={0.05} fill="var(--color-lime)" />
        </g>
      </g>

      {/* stops (outside flip for crisp labels) */}
      {order.map((id, i) => {
        const s = stops[id];
        if (!s || s.kind === "start") return null;
        return <StopMark key={id} stop={s} n={s.kind === "end" ? null : i} status={stopStatus[id]} active={activeStop === id} compact={compact} fy={fy} scale={B.w / 14} />;
      })}

      {grocery &&
        !compact &&
        store.aisles
          .filter((a) => a.aisle_id.startsWith("aisle_"))
          .map((a) => (
            <text key={a.aisle_id} x={a.rect.x + a.rect.w / 2} y={fy(a.rect.y + a.rect.h) - 0.25} textAnchor="middle" fontSize={0.42} fill="var(--muted-foreground)" className="tabular">
              {a.aisle_id.replace("aisle_", "A")}
            </text>
          ))}
      <text x={Math.max(0.2, store.entrance.x - 0.7)} y={B.h - 0.3} fontSize={0.42 * (B.w / 14)} fill="var(--color-tomato)" className="tabular">
        ENTRANCE
      </text>
      {grocery ? (
        <>
          <text x={10.45} y={B.h - 2.25} fontSize={0.42} fill="var(--muted-foreground)" className="tabular">
            CHECKOUT
          </text>
          <text x={B.w / 2} y={0.6} textAnchor="middle" fontSize={0.42} fill="var(--muted-foreground)" className="tabular">
            DAIRY, MEAT, DELI
          </text>
          <text x={B.w / 2} y={fy(6.4)} textAnchor="middle" fontSize={0.42} fill="var(--color-leaf)" className="tabular">
            PRODUCE
          </text>
        </>
      ) : (
        store.fixtures.map((f) => (
          <text
            key={f.fixture_id}
            x={f.rect.x + f.rect.w / 2}
            y={fy(f.rect.y + f.rect.h / 2)}
            textAnchor="middle"
            fontSize={0.26}
            fill="var(--muted-foreground)"
            className="tabular"
            transform={f.rect.h > f.rect.w * 2 ? `rotate(-90 ${f.rect.x + f.rect.w / 2} ${fy(f.rect.y + f.rect.h / 2)})` : undefined}
          >
            {f.label.split(":")[0].toUpperCase()}
          </text>
        ))
      )}
    </svg>
  );
}

function StopMark({
  stop,
  n,
  status,
  active,
  compact,
  fy,
  scale,
}: {
  stop: Stop;
  n: number | null;
  status: StopStatus;
  active: boolean;
  compact: boolean;
  fy: (y: number) => number;
  scale: number;
}) {
  const x = stop.pose.x,
    y = fy(stop.pose.y);
  const optional = stop.kind === "optional";
  const r = (compact ? 0.32 : 0.38) * Math.max(0.7, scale);
  const fill =
    status === "done" ? "var(--color-leaf)" : active ? "var(--color-sky)" : status === "declined" ? "var(--card)" : optional ? "var(--color-amber-wash)" : "var(--card)";
  const stroke = status === "declined" ? "var(--muted-foreground)" : optional ? "var(--color-amber)" : "var(--foreground)";
  const text = status === "done" || active ? "#fff" : status === "declined" ? "var(--muted-foreground)" : "var(--foreground)";
  if (stop.kind === "end") return <rect x={x - 0.3} y={y - 0.3} width={0.6} height={0.6} fill="var(--foreground)" />;
  return (
    <g>
      {optional ? (
        <rect x={x - r} y={y - r} width={2 * r} height={2 * r} transform={`rotate(45 ${x} ${y})`} fill={fill} stroke={stroke} strokeWidth={0.06} />
      ) : (
        <circle cx={x} cy={y} r={r} fill={fill} stroke={stroke} strokeWidth={0.06} />
      )}
      {status === "declined" ? (
        <path d={`M${x - 0.18},${y - 0.18} L${x + 0.18},${y + 0.18} M${x + 0.18},${y - 0.18} L${x - 0.18},${y + 0.18}`} stroke="var(--muted-foreground)" strokeWidth={0.06} />
      ) : (
        <text x={x} y={y + r * 0.4} textAnchor="middle" fontSize={r * 1.05} fontWeight={600} fill={text} className="tabular">
          {n}
        </text>
      )}
    </g>
  );
}

export function MapLegend() {
  return (
    <div className="flex flex-wrap gap-x-4 gap-y-1 text-[12.5px] text-muted-foreground">
      <span className="flex items-center gap-1">
        <span className="inline-block w-5 border-t-[3px] border-foreground" /> Your list
      </span>
      <span className="flex items-center gap-1">
        <span className="inline-block w-5 border-t-[3px] border-dashed border-amber" /> Extra stop
      </span>
      <span className="flex items-center gap-1">
        <span className="inline-block w-5 border-t-2 border-dotted border-muted-foreground" /> Without extras
      </span>
    </div>
  );
}
