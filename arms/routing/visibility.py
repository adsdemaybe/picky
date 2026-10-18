"""Obstacle-aware shortest paths on a visibility graph (base deps only, pure Python).

Used by the fake (so its legs never cut through shelves) and by the real service as the fallback when the
``routing`` extra (scikit-image) is missing. Fixture footprints and obstacles are axis-aligned rects, so the
shortest collision-free path for a disc of radius ``clearance_m`` bends only at the corners of the rects inflated by
that radius. Graph nodes = those corners (pushed out a few cm) + the query poses; edges = mutually visible pairs.

A query pose that already sits inside an inflated rect (e.g. a pose closer than ``clearance_m`` to a shelf) is still
allowed to leave it: for segments touching that pose only the raw footprint of that rect is checked.
"""

import heapq
import math
from collections.abc import Sequence

from contracts.common import ROBOT_RADIUS_M, Pose2D
from contracts.planogram import Planogram

Box = tuple[float, float, float, float]  # x0, y0, x1, y1

DEFAULT_CLEARANCE_M = ROBOT_RADIUS_M - 0.1
"""Approach poses keep >= ROBOT_RADIUS_M from fixtures (utils.data checks it); a little less leaves them outside."""
NODE_MARGIN_M = 0.05
_EPS = 1e-9


def _seg_hits_box(ax: float, ay: float, bx: float, by: float, box: Box) -> bool:
    """True when segment a-b passes through the open interior of ``box`` (touching an edge/corner is fine)."""
    x0, y0, x1, y1 = box
    if max(ax, bx) <= x0 + _EPS or min(ax, bx) >= x1 - _EPS or max(ay, by) <= y0 + _EPS or min(ay, by) >= y1 - _EPS:
        return False
    dx, dy = bx - ax, by - ay
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
        if abs(p) < 1e-12:
            if q <= _EPS:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 >= t1:
            return False
    return (t1 - t0) * math.hypot(dx, dy) > 1e-6


def _inside(x: float, y: float, box: Box) -> bool:
    return box[0] + _EPS < x < box[2] - _EPS and box[1] + _EPS < y < box[3] - _EPS


class VisibilityMap:
    """Corner graph of one planogram's geometry. Build once per layout; ``paths()`` per route request."""

    def __init__(self, plan: Planogram, clearance_m: float = DEFAULT_CLEARANCE_M) -> None:
        rects = [f.rect for f in plan.fixtures] + list(plan.obstacles)
        self.raw: list[Box] = [(r.x, r.y, r.x_max, r.y_max) for r in rects]
        c = clearance_m
        self.inflated: list[Box] = [(x0 - c, y0 - c, x1 + c, y1 + c) for x0, y0, x1, y1 in self.raw]
        b = plan.bounds
        self.bounds: Box = (b.x, b.y, b.x_max, b.y_max)
        inner: Box = (b.x + c, b.y + c, b.x_max - c, b.y_max - c)
        m = c + NODE_MARGIN_M
        corners: list[tuple[float, float]] = []
        for x0, y0, x1, y1 in self.raw:
            for x, y in ((x0 - m, y0 - m), (x1 + m, y0 - m), (x0 - m, y1 + m), (x1 + m, y1 + m)):
                if not _inside(x, y, inner) or any(_inside(x, y, box) for box in self.inflated):
                    continue
                if all(math.hypot(x - u, y - v) > 1e-6 for u, v in corners):
                    corners.append((round(x, 4), round(y, 4)))
        self.corners = corners
        n = len(corners)
        self.corner_adj: list[list[tuple[int, float]]] = [[] for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                if self._visible(corners[i], corners[j], (), ()):
                    d = math.dist(corners[i], corners[j])
                    self.corner_adj[i].append((j, d))
                    self.corner_adj[j].append((i, d))

    def _containing(self, x: float, y: float) -> tuple[int, ...]:
        return tuple(k for k, box in enumerate(self.inflated) if _inside(x, y, box))

    def _visible(
        self, a: tuple[float, float], b: tuple[float, float], skip_a: tuple[int, ...], skip_b: tuple[int, ...]
    ) -> bool:
        ax, ay = a
        bx, by = b
        for k, box in enumerate(self.inflated):
            check = self.raw[k] if (k in skip_a or k in skip_b) else box
            if _seg_hits_box(ax, ay, bx, by, check):
                return False
        return True

    def paths(self, poses: Sequence[Pose2D]) -> "PathTable":
        """All-pairs shortest paths between ``poses`` (Dijkstra from each pose over corners + poses)."""
        return PathTable(self, poses)


class PathTable:
    """Distance matrix + leg waypoints between query poses; unreachable pairs fall back to a straight segment."""

    def __init__(self, vmap: VisibilityMap, poses: Sequence[Pose2D]) -> None:
        self.poses = list(poses)
        self.unreachable: set[tuple[int, int]] = set()
        nc = len(vmap.corners)
        q = [(p.x, p.y) for p in self.poses]
        skips = [vmap._containing(x, y) for x, y in q]
        nodes = list(vmap.corners) + q
        adj: list[list[tuple[int, float]]] = [list(a) for a in vmap.corner_adj] + [[] for _ in q]
        for i, pt in enumerate(q):
            u = nc + i
            for j, c in enumerate(vmap.corners):
                if vmap._visible(pt, c, skips[i], ()):
                    d = math.dist(pt, c)
                    adj[u].append((j, d))
                    adj[j].append((u, d))
            for k in range(i + 1, len(q)):
                if vmap._visible(pt, q[k], skips[i], skips[k]):
                    d = math.dist(pt, q[k])
                    adj[u].append((nc + k, d))
                    adj[nc + k].append((u, d))
        self._nodes = nodes
        self._nc = nc
        n = len(q)
        self.matrix: list[list[float]] = [[math.inf] * n for _ in range(n)]
        self._prev: list[list[int]] = []
        for i in range(n):
            dist, prev = _dijkstra(adj, nc + i, len(nodes))
            self._prev.append(prev)
            for j in range(n):
                self.matrix[i][j] = dist[nc + j]
        for i in range(n):  # symmetric up to float noise; unreachable -> straight line (logged by the caller)
            self.matrix[i][i] = 0.0
            for j in range(n):
                if math.isinf(self.matrix[i][j]):
                    self.matrix[i][j] = math.dist(q[i], q[j])
                    self.unreachable.add((i, j))

    def dist(self, a: int, b: int) -> float:
        return self.matrix[a][b]

    def leg(self, a: int, b: int, pa: Pose2D, pb: Pose2D) -> list[Pose2D]:
        """Waypoints a -> b: exactly ``pa`` first and ``pb`` last; corners in between face along the path."""
        if a == b or (a, b) in self.unreachable:
            return [pa.model_copy(), pb.model_copy()]
        prev = self._prev[a]
        target = self._nc + b
        chain: list[int] = []
        node = target
        while node != -1 and node != self._nc + a:
            chain.append(node)
            node = prev[node]
        chain.reverse()
        out = [pa.model_copy()]
        px, py = pa.x, pa.y
        for node in chain[:-1]:
            x, y = self._nodes[node]
            out.append(Pose2D(x=x, y=y, theta=math.atan2(y - py, x - px)))
            px, py = x, y
        out.append(pb.model_copy())
        return out


def _dijkstra(adj: list[list[tuple[int, float]]], src: int, n: int) -> tuple[list[float], list[int]]:
    dist = [math.inf] * n
    prev = [-1] * n
    dist[src] = 0.0
    heap = [(0.0, src)]
    while heap:
        d, u = heapq.heappop(heap)
        if d > dist[u]:
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v] - 1e-12:
                dist[v] = nd
                prev[v] = u
                heapq.heappush(heap, (nd, v))
    return dist, prev


_CACHE: dict[str, VisibilityMap] = {}


def visibility_map(plan: Planogram, clearance_m: float = DEFAULT_CLEARANCE_M) -> VisibilityMap:
    """Cached per layout geometry (fixtures, obstacles, bounds), not per stock change."""
    key = "|".join(
        [plan.store_id, f"{clearance_m:.3f}", plan.bounds.model_dump_json()]
        + [f.rect.model_dump_json() for f in plan.fixtures]
        + [o.model_dump_json() for o in plan.obstacles]
    )
    vmap = _CACHE.get(key)
    if vmap is None:
        if len(_CACHE) > 16:
            _CACHE.clear()
        vmap = _CACHE[key] = VisibilityMap(plan, clearance_m)
    return vmap


__all__ = ["DEFAULT_CLEARANCE_M", "PathTable", "VisibilityMap", "visibility_map"]
