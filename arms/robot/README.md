# arms/robot

**Purpose.** Drives EVERY robot in EVERY store: the SO-101 rover (small base, radius 0.3 m, vertical lift 0.2–1.2 m,
SO-101 arm with ~0.35 m reach, basket on the back; `contracts.common` robot model). Pipeline stage **ROBOT SKILLS IN
SIM** (CLAUDE.md §2): each robot's brain (arms/agent) calls `robot.navigate` → `scan` → `pick` → `place_in_basket` →
`unload_basket` with its `robot_id`; the fleet reads `list_robots` / `states`; cameras (perception) read
`capture_camera` / `frames`; the catalog calls `sync_stock` after restocks. Real = Isaac Sim worker on "vhagar" over
the multiplexed `SimLink` with the 2D fake built in as the per-store fallback; fake = the 2D sim alone.

## Scope (CLAUDE.md §3)

- [x] **P0** MULTI-ROBOT `RobotService` (contracts v0.2): `robot_id` on every call, `list_robots`, per-robot busy /
  stop, `unload_basket`, `sync_stock`, `capture_camera`, filtered `frames(camera_id, robot_id)` / `states(robot_id)`
- [x] **P0** Fake: 2D kinematic sim of every robot in `data/fixtures/robots.yaml` (Pico / Rhea in store_ave, Hazel in
  store_hill; built-in fallback fleet while the file is missing), docked at spawn, concurrent motion in sim time
  (`realtime_factor`), pure pursuit at `max_speed_mps`, rovers yield to a moving rover with a smaller id
- [x] **P0** SO-101 rover model: lift to the item height (`rover.lift_for`), reach = `slot_reachable` (else
  `grasp_failed`), basket capacity, unload at checkout
- [x] **P0** Battery (robot arm owns `battery_pct`): per meter + idle drain, CHARGING while idle on the dock,
  `battery_low` below 5 % except driving to the dock; carried across backends
- [x] **P0** Deterministic ground truth per store (`instance_id(slot_id, n)`, `store_id` set, per-unit freshness),
  one shared world per store; `sync_stock` respawns / removes units (back rows first, ids never reused)
- [x] **P0** Camera frames (Pillow, `render.py`): `<robot>.front` first-person shelf view with label boxes + price tags
  (the physical tag: mispricings show) + OOS gaps + HUD; scans aim the pan-tilt head at the slot (zoomed view, every
  unit with a pixel box, `camera_pose` set); `<robot>.wrist` close-up (held item / slot in front); `<robot>.basket`
  top-down basket; `<store>.ceiling-N` store map with fixtures, docks, every robot + remaining route. RAW
  (`blurred=False`), marked synthetic (`fakeframe_` ids, "SIM" watermark, JPEG comment); streams on demand
- [x] **P0** Real `SimRobot`: per-store backend (Isaac iff the worker hosts the store with a matching `data_hash`, else
  store-level `ResetCmd` first; fake → Isaac only while the store is idle), `cmd_id` per command, `robot_id` on every
  motion, per-robot busy, `ok=False` → `RobotError(code)`, timeout → error event, Isaac loss → `disconnected` once and
  the store continues on the fake from the last state (pose, battery, holding, basket; shelves re-synced)
- [x] **P0** Real: per-camera frame cache, ref-counted `SetStreamsCmd` (union of open Isaac cameras, fps ≤ 15),
  `SetSlotStockCmd` per slot on `sync_stock`, `sim_status()` helper (`isaac_*` / `fake_*`)
- [x] **P0** Pure-pursuit follower `controllers/pure_pursuit.py` (pure Python, shared with Isaac)
- [ ] **P0** Isaac: `RoverSO101` built by `create_isaac_robot` (base + mast + lift + basket; SO-101 URDF when
  `PICKY_SO101_DIR` is set, else a proxy arm), stepped per robot by the sim worker with `NavigateAction` /
  `LiftAction` / `MagicGraspAction` / `PlaceAction` / `UnloadAction` (code written; vhagar unreachable, not deployed)
- [ ] **P1** Retry `grasp_failed` once with any instance (attach-on-contact fallback)
- [ ] **P1** Liveness: no `RobotState` for > 5 s → treat the store as lost (fall back to the fake)
- [ ] **Later** physical SO-101 on a real rover + LeRobot / LeIsaac VLA (keep `contracts.common` consistent)

## Contract

- Implements `contracts.services.RobotService` (`list_robots, state, reset, navigate, stop, scan, capture,
  capture_camera, list_instances, pick, place_in_basket, unload_basket, sync_stock, frames, states`). Every failure
  raises `contracts.sim.RobotError(code)` (`unknown_robot` for unknown robot / camera ids).
- In: `robot_id` + waypoints (final theta reached) / `slot_id` / `sku` / `instance_id` (from `PickChoice`); live
  planogram + products (`services.catalog` at call time, else `utils.data`); `RobotSpec`s + ceiling cameras
  (`utils.data.load_robots_file`).
- Out: `RobotState` (robot_id, store_id, battery, lift, `backend` = `isaac` | `fake`), RAW `CameraFrame`
  (`camera_id`, `camera_pose`, ground-truth `instances` on scans / captures, `[]` on streams), `ItemInstance`
  (`store_id`).
- Events: none by default (the gateway republishes `states()` as `robot.state`; `EVENT_PRODUCERS`). `SimRobot`
  publishes `error` (`arm="robot"`, with `store_id` / `robot_id`) on timeouts and Isaac loss.
  `FakeRobot(publish_state_events=True)` also publishes `robot.state` for standalone runs.
- Calls at call time only: `services.sim_link` (real; or `attach_link(link)`), `services.catalog.get_planogram` /
  `get_product`, `services.events` (errors).
- Extras (not in the Protocol): `set_task(robot_id, text)` (HUD line on the robot's frames), `set_trip(trip_id)`,
  `SimRobot.attach_link / is_connected / backend(store_id) / sim_status(base) / refresh_backends()`,
  `FakeRobot.reset_store / adopt_state / world / camera_ids / snapshot / robot_ids / spec`.

## Files

| File | What |
|---|---|
| `__init__.py` | `create(services, **opts)` → `SimRobot`, `create_fake(services, **opts)` → `FakeRobot`, `create_isaac_robot` |
| `fake.py` | `FakeRobot`: multi-robot 2D kinematic sim + on-demand camera streaming (base deps + Pillow) |
| `service.py` | `SimRobot`: per-store Isaac / fake backend over the multiplexed `SimLink` |
| `rover.py` | Robot model: lift, durations, `Battery`, specs (robots.yaml + built-in fallback fleet) |
| `world.py` | `StoreWorld`: per-store ground truth (planogram, products, units), `sync_stock` / `set_slot_stock` |
| `instances.py` | Deterministic instances, `resync_slot`, basket positions, synthetic bboxes (shared with the Isaac scene) |
| `render.py` | Pillow frames: front / scan, wrist, basket, ceiling map |
| `loopback.py` | `LoopbackSimLink`: a stand-in Isaac worker (hello, hosted, results, disconnect) over a `FakeRobot` |
| `controllers/pure_pursuit.py` | Pure-Python follower (holonomic + diff-drive, final-yaw phase) |
| `isaac/` | `RoverSO101` (lazy isaacsim/pxr imports), per-step actions (navigate, lift, magic grasp, place, unload) |
| `placeholder.py` | Constant JPEG used only if Pillow rendering ever fails |
| `tests/` | Fake (fleet, motion, battery, cameras, streams, stock sync), real over loopback (backends, reset, fallback, streams), renderer, Isaac actions, pure pursuit |

## How to run

```bash
uv run pytest arms/robot                       # base deps only (Pillow is a base dep)
make demo                                       # robot fake (FAKE_OPTS realtime_factor=120)
PICKY_REAL_ARMS=robot make demo                 # SimRobot; no worker -> every store on the built-in fake
make dev   # then: GET /api/sim/status, /api/fleet/robots, /api/cameras/<id>/snapshot.jpg, /stream.mjpg
```
Fake options (`create_fake(services, **opts)`; `create(services, **opts)` passes them to its built-in fake):
`realtime_factor` (default 5.0, `math.inf` = instant), `speed_mps` (None = spec speed 0.8), `stream_fps` (4),
`robots` / `store_cameras` (override robots.yaml), `strict_reach` (True), `render_in_thread` (True),
`publish_state_events` (False). `SimRobot` also takes `stream_fps` (Isaac `SetStreamsCmd`, ≤ 15) and `fallback`.
Isaac side: `make sim` on the GPU box (see arms/sim/README.md); `PICKY_SO101_DIR` = SO-ARM100 checkout for the URDF.

## Contributing to this arm

1. Branch: `arm/robot` until the arm merges, then `feat/robot-<slug>` / `fix/robot-<slug>` off `main` (CLAUDE.md §5).
2. Change only `arms/robot/**`. Need a contract change → separate `contracts/<slug>` PR. Never import another arm.
3. Keep `fake.py` working and deterministic; keep `controllers/` and `isaac/actions.py` pure Python (they run in Isaac).
4. Before pushing: `uv run ruff check --fix arms/robot && uv run ruff format arms/robot && uv run pytest arms/robot`,
   then `PICKY_REAL_ARMS=robot make demo`; after editing this README run `make rules`.
5. Open/update the PR with `gh pr create --base main --fill --label arm:robot`; tick the scope boxes above.

## Demo-ready checklist (CLAUDE.md §6)

- [x] Every `RobotService` method works on the demo path (no `NotImplementedError`), fake and real (real = fake
  fallback without a worker; Isaac path tested over `LoopbackSimLink`)
- [ ] `PICKY_REAL_ARMS=robot make demo` completes Maya's taco trips: yes in ~7 s when the demo passes
  `real_opts={"robot": {"realtime_factor": 120}}` (gateway `demo.py` passes `FAKE_OPTS` to fakes only; without it the
  built-in fake runs at the default 5x and the 30 s demo timeout hits)
- [x] Timeout / disconnect / `no_instance` / `path_blocked` / `battery_low` degrade without killing the trip (codes +
  `error` event; Isaac loss continues on the fake)
- [x] Removing `robot` from `PICKY_REAL_ARMS` switches back to the fake
- [x] Instance ids, per-unit freshness and basket offset identical between fake and Isaac (`contracts.sim`
  `instance_id` / `unit_freshness` / `BASKET_OFFSET_LOCAL`; `arms/gateway/tests/test_ground_truth.py`)
- [x] Tests green; no secrets; README current

## Known limitations

- Fake frames are schematic (flat label boxes, orthographic shelf face), not photoreal; no people are ever drawn.
- Fake collision check samples the commanded path against fixture rects (not inflated by `ROBOT_RADIUS_M`); pure
  pursuit cuts corners by up to the 0.5 m lookahead. Rovers yield only to MOVING rovers with a smaller id (parked
  rovers are passed through so a docked robot never blocks the demo).
- Battery idle drain / charging in the fake integrates wall-clock time × `realtime_factor` (instant mode: motion time
  only), so battery values are not bit-for-bit reproducible across runs at finite speed.
- Real: Isaac hosting is read from `SimLink.status()` (falls back to the last `SimHello` / `SimHosted`); a store switches
  to Isaac with the worker's robot poses (no teleport of the fake poses) unless a store reset respawns them at docks.
- Isaac code is written but not deployed (vhagar unreachable): SO-101 URDF import API and link names are TODOs.

**Owner:** TBD · **Branch:** `arm/robot`
