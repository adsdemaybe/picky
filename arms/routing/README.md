# routing

## Purpose
Turns the shopping list + recommendation detours into a robot route through the store: occupancy grid from the
planogram → grid distance matrix → orienteering (list items mandatory, recs optional with skip penalty = interest).
Pipeline: `recs.ready` → **`routing.plan_route`** → `route.planned` → robot executes legs; re-solved via
`trips.replan` after every yes/no.

## Scope (CLAUDE.md §3)
- [x] **P0** Occupancy grid from the planogram, 0.05 m, inflated by `ROBOT_RADIUS_M` (`grid.py`, numpy; scipy EDT if present)
- [x] **P0** Distance matrix + collision-free paths between stop poses (skimage `MCP_Geometric`, `service.py`)
- [x] **P0** Orienteering: list items = mandatory, recs = `AddDisjunction` penalty `interest × 15 m` in int cm (OR-Tools)
- [x] **P0** `detour_m`/`detour_s` on every optional stop, `visited_optional`/`skipped_optional`, baseline route
- [x] **P0** Deterministic fake + greedy fallback (nearest-neighbor + 2-opt + cheapest insertion)
- [ ] **P0** Path smoothing (line-of-sight shortcut + ~0.25 m resample); now collinear-pruned grid paths
- [ ] **P0** Verified on `data/planograms/store_ave.yaml` (solve < 1.5 s) once the data lands
- [ ] **P1** Reachability check (`scipy.ndimage.label`) → skip unreachable stops + `error` event instead of 1e6 m
- [ ] **P1** Penalty constant `PENALTY_K_M` agreed with recommender; cached demo route JSON as last resort
- [x] **P2** Batched shoppers: `plan_batched` merges stops (one slot/SKU → one stop, qty summed, `user_ids` unioned)
- [ ] **P2** Basket capacity dimension for batched trips

## Contract
- Implements `contracts.services.RoutingService`: `plan_route(RouteRequest) -> Route`,
  `plan_batched(list[RouteRequest]) -> Route`, `grid_spec(store_id) -> GridSpec`.
- Models: `contracts/routing.py` (`GridSpec`, `Stop`, `RouteRequest`, `RouteLeg`, `Route`).
- Route shape: `order` = `start` … `end` (ids renamed `_start`/`_end` only on collision); `end` defaults to the
  planogram `checkout`; `legs[i].waypoints[0/-1]` are exactly the stop poses (incl. theta);
  `Route.solver` ∈ `fake | greedy | ortools`.
- Events: none (gateway publishes `route.planned` / `stop.updated`). Calls `services.catalog.get_planogram` at call
  time only (falls back to `utils.data.load_planogram` when catalog isn't wired). Unknown/online store → `KeyError`.

## Stack
- Pick: numpy grid → `skimage.graph.MCP_Geometric(fully_connected=True)` one Dijkstra per source → OR-Tools
  `RoutingModel` (1 vehicle, start/end nodes, PATH_CHEAPEST_ARC + GUIDED_LOCAL_SEARCH, `time_limit_s` from request).
- Fallback: no scikit-image → visibility-graph shortest paths (`visibility.py`, base deps); no ortools / no solution →
  greedy (`core.greedy_order(improve=True)`).
- Extras (`uv sync --extra routing`): `ortools>=9.10`, `scipy>=1.13`, `scikit-image>=0.24`.
- Gotchas (docs/research.md §9.5): numpy is `[row, col] = [y, x]`; never `penalty=-1` for mandatory (just no
  disjunction); penalties and arc costs in the same int-cm unit; GLS always runs to the time limit.
- Measured (synthetic 14×22 m store, 16 nodes): grid 0.09 s, MCP matrix 0.13 s, OR-Tools 1.0 s (time limit).

## Files
| File | What |
|---|---|
| `__init__.py` | `create(services)` → `GridRoutingService`, `create_fake(services)` → `FakeRoutingService` |
| `grid.py` | `make_grid_spec`, `rasterize`, `inflate`, `build_grid` → `OccupancyGrid`, `nearest_free` (pure numpy) |
| `core.py` | node indexing, greedy solver, `assemble_route` (detours/baseline/legs), `merge_requests`, `get_planogram` |
| `fake.py` | visibility-graph distances + legs (never cross a fixture), NN mandatory order, optional (interest ≥ 0.5) cheapest insertion |
| `visibility.py` | pure-Python shortest paths around fixtures inflated by `ROBOT_RADIUS_M - 0.1` (corner graph + Dijkstra), cached per layout |
| `service.py` | grid + MCP paths + OR-Tools orienteering with fallbacks; TODO list at the top |
| `tests/` | synthetic taco-trip store (`conftest.py`), grid, fake conformance/behavior, service (extras via importorskip) |

## How to run
```bash
uv run pytest arms/routing                       # base deps (extras tests skip)
uv sync --extra routing                          # ortools + scipy + scikit-image
uv run pytest arms/routing                       # now runs MCP + OR-Tools tests too
# without touching the env: uv run --with 'ortools>=9.10' --with scipy --with scikit-image pytest arms/routing
PICKY_REAL_ARMS=routing make demo                # full trip with the real router
```

## Demo-ready checklist (§6)
- [ ] `RoutingService` fully implemented, no `NotImplementedError` on demo paths (done for all three methods)
- [ ] `PICKY_REAL_ARMS=routing make demo` completes the taco trip on `store_ave`; map shows route + 3 detours
- [ ] Re-plan after each yes/no returns in < 2 s; declined rec disappears from the route
- [ ] Missing extras / solver failure degrades to greedy without killing the trip (logged)
- [ ] Switch back with `PICKY_REAL_ARMS=` (fake) alone
- [ ] Tests green with base deps and with `--extra routing`; README updated

## Known limitations
- Paths are grid staircases pruned to corners (no smoothing yet); intermediate thetas are segment headings.
- Poses inside inflated space are snapped to the nearest free cell for the distance matrix (leg endpoints stay exact).
- Discretization: an obstacle is the set of cells whose center is inside a rect, so effective clearance to a rect edge
  is `ROBOT_RADIUS_M - res/2`.
- Optional stops beyond `max_optional_stops` (by interest) are never offered to the solver; the fake/greedy ignore
  stops with interest < 0.5.

Owner: TBD · Branch: `arm/routing`
