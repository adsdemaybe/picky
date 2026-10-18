// Client-side route planner for demo mode: occupancy grid from the planogram → A* → stop order (NN + 2-opt).
// The routing arm (OR-Tools orienteering) is authoritative when the gateway is connected; this mirrors its output shape.
import type { Pose, Rect, Store } from "./data";

const RES = 0.2; // m per cell
const INFLATE = 0.35; // robot radius, m

type Grid = { w: number; h: number; blocked: Uint8Array; store: Store };

let cached: Grid | null = null;

function grid(store: Store): Grid {
  if (cached && cached.store === store) return cached;
  const w = Math.ceil(store.bounds.w / RES);
  const h = Math.ceil(store.bounds.h / RES);
  const blocked = new Uint8Array(w * h);
  const block = (r: Rect, pad: number) => {
    const x0 = Math.max(0, Math.floor((r.x - pad) / RES));
    const x1 = Math.min(w - 1, Math.ceil((r.x + r.w + pad) / RES));
    const y0 = Math.max(0, Math.floor((r.y - pad) / RES));
    const y1 = Math.min(h - 1, Math.ceil((r.y + r.h + pad) / RES));
    for (let y = y0; y <= y1; y++) for (let x = x0; x <= x1; x++) blocked[y * w + x] = 1;
  };
  for (const f of store.fixtures) block(f.rect, INFLATE);
  for (const o of store.obstacles) block(o, INFLATE);
  // walls
  for (let x = 0; x < w; x++) {
    blocked[x] = 1;
    blocked[(h - 1) * w + x] = 1;
  }
  for (let y = 0; y < h; y++) {
    blocked[y * w] = 1;
    blocked[y * w + w - 1] = 1;
  }
  cached = { w, h, blocked, store };
  return cached;
}

const cell = (g: Grid, p: Pose) => [
  Math.min(g.w - 1, Math.max(0, Math.round(p.x / RES))),
  Math.min(g.h - 1, Math.max(0, Math.round(p.y / RES))),
];

function nearestFree(g: Grid, cx: number, cy: number): [number, number] {
  if (!g.blocked[cy * g.w + cx]) return [cx, cy];
  for (let r = 1; r < 20; r++)
    for (let dy = -r; dy <= r; dy++)
      for (let dx = -r; dx <= r; dx++) {
        const x = cx + dx,
          y = cy + dy;
        if (x >= 0 && y >= 0 && x < g.w && y < g.h && !g.blocked[y * g.w + x]) return [x, y];
      }
  return [cx, cy];
}

/** A* on the occupancy grid; returns a smoothed polyline in meters. */
export function astar(store: Store, a: Pose, b: Pose): { path: Pose[]; length: number } {
  const g = grid(store);
  const [sx, sy] = nearestFree(g, ...(cell(g, a) as [number, number]));
  const [tx, ty] = nearestFree(g, ...(cell(g, b) as [number, number]));
  const N = g.w * g.h;
  const gScore = new Float32Array(N).fill(Infinity);
  const came = new Int32Array(N).fill(-1);
  const closed = new Uint8Array(N);
  const start = sy * g.w + sx;
  const goal = ty * g.w + tx;
  gScore[start] = 0;
  // binary heap of [f, idx]
  const heap: number[] = [];
  const hf = (i: number) => {
    const x = i % g.w,
      y = (i / g.w) | 0;
    const dx = Math.abs(x - tx),
      dy = Math.abs(y - ty);
    return Math.max(dx, dy) + (Math.SQRT2 - 1) * Math.min(dx, dy);
  };
  const fOf = new Float32Array(N);
  const push = (i: number) => {
    heap.push(i);
    let k = heap.length - 1;
    while (k > 0) {
      const p = (k - 1) >> 1;
      if (fOf[heap[p]] <= fOf[heap[k]]) break;
      [heap[p], heap[k]] = [heap[k], heap[p]];
      k = p;
    }
  };
  const pop = () => {
    const top = heap[0];
    const last = heap.pop()!;
    if (heap.length) {
      heap[0] = last;
      let k = 0;
      for (;;) {
        const l = 2 * k + 1,
          r = l + 1;
        let m = k;
        if (l < heap.length && fOf[heap[l]] < fOf[heap[m]]) m = l;
        if (r < heap.length && fOf[heap[r]] < fOf[heap[m]]) m = r;
        if (m === k) break;
        [heap[m], heap[k]] = [heap[k], heap[m]];
        k = m;
      }
    }
    return top;
  };
  fOf[start] = hf(start);
  push(start);
  const D = [
    [1, 0, 1],
    [-1, 0, 1],
    [0, 1, 1],
    [0, -1, 1],
    [1, 1, Math.SQRT2],
    [1, -1, Math.SQRT2],
    [-1, 1, Math.SQRT2],
    [-1, -1, Math.SQRT2],
  ];
  while (heap.length) {
    const cur = pop();
    if (cur === goal) break;
    if (closed[cur]) continue;
    closed[cur] = 1;
    const cx = cur % g.w,
      cy = (cur / g.w) | 0;
    for (const [dx, dy, c] of D) {
      const nx = cx + dx,
        ny = cy + dy;
      if (nx < 0 || ny < 0 || nx >= g.w || ny >= g.h) continue;
      const ni = ny * g.w + nx;
      if (g.blocked[ni] || closed[ni]) continue;
      if (dx && dy && (g.blocked[cy * g.w + nx] || g.blocked[ny * g.w + cx])) continue; // no corner cutting
      const ng = gScore[cur] + c;
      if (ng < gScore[ni]) {
        gScore[ni] = ng;
        came[ni] = cur;
        fOf[ni] = ng + hf(ni);
        push(ni);
      }
    }
  }
  const cells: number[] = [];
  for (let i = goal; i !== -1; i = came[i]) {
    cells.push(i);
    if (i === start) break;
  }
  cells.reverse();
  if (cells[0] !== start) {
    // unreachable: straight line fallback (never expected on the demo map)
    return { path: [a, b], length: Math.hypot(b.x - a.x, b.y - a.y) };
  }
  const smooth = stringPull(g, cells);
  const path: Pose[] = [{ x: a.x, y: a.y }, ...smooth.map((i) => ({ x: (i % g.w) * RES, y: ((i / g.w) | 0) * RES })), { x: b.x, y: b.y }];
  let length = 0;
  for (let i = 1; i < path.length; i++) length += Math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y);
  return { path, length };
}

function clearLine(g: Grid, i: number, j: number) {
  const x0 = i % g.w,
    y0 = (i / g.w) | 0,
    x1 = j % g.w,
    y1 = (j / g.w) | 0;
  const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0)) * 2;
  for (let k = 0; k <= n; k++) {
    const x = Math.round(x0 + ((x1 - x0) * k) / n),
      y = Math.round(y0 + ((y1 - y0) * k) / n);
    if (g.blocked[y * g.w + x]) return false;
  }
  return true;
}

function stringPull(g: Grid, cells: number[]) {
  if (cells.length < 3) return cells;
  const out = [cells[0]];
  let anchor = 0;
  for (let k = 2; k < cells.length; k++) {
    if (!clearLine(g, cells[anchor], cells[k])) {
      anchor = k - 1;
      out.push(cells[anchor]);
    }
  }
  out.push(cells[cells.length - 1]);
  return out;
}

export type PlannedStop = { id: string; pose: Pose };
export type PlannedRoute = {
  order: string[]; // stop ids incl. start/end
  legs: { from: string; to: string; path: Pose[]; length: number }[];
  total_m: number;
};

/** Order stops start → … → end: nearest neighbour then 2-opt on A* distances. */
export function planRoute(store: Store, start: PlannedStop, stops: PlannedStop[], end: PlannedStop): PlannedRoute {
  const all = [start, ...stops, end];
  const n = all.length;
  const dist: number[][] = Array.from({ length: n }, () => Array(n).fill(0));
  const paths = new Map<string, Pose[]>();
  for (let i = 0; i < n; i++)
    for (let j = i + 1; j < n; j++) {
      const r = astar(store, all[i].pose, all[j].pose);
      dist[i][j] = dist[j][i] = r.length;
      paths.set(`${i}>${j}`, r.path);
      paths.set(`${j}>${i}`, [...r.path].reverse());
    }
  // nearest neighbour over the middle stops
  const order = [0];
  const left = new Set(stops.map((_, k) => k + 1));
  while (left.size) {
    const last = order[order.length - 1];
    let best = -1;
    for (const k of left) if (best < 0 || dist[last][k] < dist[last][best]) best = k;
    order.push(best);
    left.delete(best);
  }
  order.push(n - 1);
  // 2-opt (endpoints fixed)
  let improved = true;
  while (improved) {
    improved = false;
    for (let i = 1; i < order.length - 2; i++)
      for (let k = i + 1; k < order.length - 1; k++) {
        const a = order[i - 1],
          b = order[i],
          c = order[k],
          d = order[k + 1];
        if (dist[a][c] + dist[b][d] + 1e-6 < dist[a][b] + dist[c][d]) {
          order.splice(i, k - i + 1, ...order.slice(i, k + 1).reverse());
          improved = true;
        }
      }
  }
  const legs = order.slice(1).map((to, k) => {
    const from = order[k];
    return { from: all[from].id, to: all[to].id, path: paths.get(`${from}>${to}`)!, length: dist[from][to] };
  });
  return { order: order.map((i) => all[i].id), legs, total_m: legs.reduce((s, l) => s + l.length, 0) };
}
