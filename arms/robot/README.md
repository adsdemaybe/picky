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
- [ ] **P0** Loop (contracts 0.2.1): `T0Vla` (`VlaService`, `create_vla` / `create_vla_fake`): navigate / align /
  grasp (magic) / place / put back / unload as streamed executions over THIS RobotService (2D fake and Isaac alike):
  idempotent `exec_id` (re-attach + replay, `idempotency_conflict`), one exec per robot (`busy`), typed errors
  (`VlaError.from_sim` + `not_at_pose` / `out_of_reach` reach pre-check, `place_failed`, `unsupported_step`),
  `deadline_s`, cancel = `robot.stop`, heartbeats with path progress + ETA, capabilities per tier, `ended_at` on the
  gateway clock, `capture_after` via `services.evidence` (front aimed at the slot). Implemented + tested
  (`tests/test_vla.py`; the draft's 68 loop tests pass with it); ticks once the gateway wires `LOOP_SERVICE_FACTORIES`
- [ ] **P0** Loop: `InMemoryFaultInjector` (`FaultInjector`, `create_faults` / `_fake`, scenario `PICKY_FAULTS`,
  `demo.fault` events with `source="robot"`) + `RobotTruth` (`TruthSource`, `create_truth` / `_fake`). Faults change
  the WORLD, so the verifier sees the real outcome: `drop_after_lift` (claim, nothing attached), `grasp_miss`,
  `wrong_item` (2D fake: really grabs the neighbouring SKU, `put_back` returns it; Isaac: overlay), `place_miss`,
  `nav_blocked`, `vla_hang`, `policy_unavailable`, `disconnect`, `mislabel`. Same tick rule as above
- [ ] **P2** T1 tier (`t1.py` `T1Adapter`), unavailable unless configured (T0 takes over): `PICKY_VLA_T1=perceptron`
  = THE T1 policy, Perceptron Mk1.5 calling Cartesian tools (`vla_mk15.py`: reach_point on the side / wrist image ->
  ray to the planogram shelf face -> worker IK (`ArmIkCmd`) -> joint rows; set_gripper; move_ee; done), driven joint
  by joint on Isaac (`vla_t1.py`, contracts 0.2.2 `ArmJointsCmd`, sticky grasp); `perceptron_vla` (robotics VLA
  service, team key), `smolvla` (vhagar :8200), `scripted`; legacy `t1_sim`, `perceptron_tool`, `openpi_ws`
- [x] **P1** Pictures while moving (fake + Isaac): front camera at `PICKY_VLA_NAV_CAPTURE_HZ` (2) while driving ->
  blurred, retained evidence + running statuses; Perceptron look-out on the approach ('observing' status,
  `PICKY_VLA_NAV_LOOK`); T1 side + wrist at `PICKY_VLA_T1_CAPTURE_HZ` (4) while the arm moves -> the robot's live
  front / wrist view (`SimRobot.feed_policy_frames`), the last pair = the next observation
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
- Loop services (contracts 0.2.1, `LOOP_SERVICE_FACTORIES`; every factory takes `services` only, calls nothing at
  construction):
  - `vla` = `T0Vla` (`contracts.services.VlaService`). Real (`create_vla`) = T0 + the T1 adapter from settings; fake
    (`create_vla_fake`) = the same T0 code, T1 only as the simulated tier (`t1_sim`). Publishes nothing.
  - `truth` = `RobotTruth` (`contracts.evidence.TruthSource`): `RobotState` + `list_instances` + live planogram
    (facing slots, shelf tags, '<brand> <name> <size>' labels) + the fault overlay; `KeyError` for unknown robots.
    `ground_truth(frame_id)` serves only sidecars registered with `remember(sidecar)` (else `KeyError`, per contract).
  - `faults` = `InMemoryFaultInjector` (`contracts.services.FaultInjector`) + `.overlay` (`virtual_holding`,
    `virtual_basket`, `mislabels`) + `rule(fault_id)` + `bind_robot(robot_id, trip_id=, store_id=)`; publishes
    `demo.fault` (`source="robot"`) on `services.events` at call time.
- Extras (not in the Protocol): `set_task(robot_id, text)` (HUD line on the robot's frames), `set_trip(trip_id)`,
  `SimRobot.attach_link / is_connected / backend(store_id) / sim_status(base) / refresh_backends()`,
  `FakeRobot.reset_store / adopt_state / world / camera_ids / snapshot / robot_ids / spec`; for the VLA layer `can_put_back / put_back(robot_id, slot_id) / grab_unit(robot_id, slot_id, instance_id)` on `FakeRobot` and `SimRobot` (2D fake only; Isaac raises `internal`).

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
| `vla.py` | `T0Vla`: the VlaService (streams, idempotency, deadlines, cancel, T0 motions, VLA-layer faults) |
| `faults.py` | `InMemoryFaultInjector` + `FaultOverlay` (`PICKY_FAULTS`, `demo.fault`) |
| `truth.py` | `RobotTruth`: the TruthSource (state + instances + planogram + overlay) |
| `t1.py` | `T1Adapter` / `T1Config`: the T1 tier (policy via PICKY_VLA_T1 / simulated / perceptron_tool / openpi_ws) |
| `vla_t1.py` | `run_policy_grasp`: the T1 policy loop over `ArmJointsCmd` (pictures while moving, PolicyContext) |
| `vla_mk15.py` | `Mk15Policy`: Perceptron Mk1.5 tool policy (+ `look` for navigation, per-trip call budget) |
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
Loop (once the gateway wires it): `PICKY_EXEC_MODE=plan PICKY_FAULTS=drop_on_first_grasp make demo` (THE demo
moment); other scenarios: `slow_vla`, `flaky_t1` (with `PICKY_VLA_TIERS=t0_scripted,t1_sim`), `isaac_drop`,
`wrong_salsa` (2D fake), `foggy_basket`. T1 env: `PICKY_VLA_TIERS` (`t1_pretrained` / `t1_sim`),
`PICKY_VLA_POLICY_PROTOCOL` (`openpi_ws` default | `perceptron_tool`), `PICKY_VLA_POLICY_URL`, `PICKY_VLA_T1_MODEL`,
`PERCEPTRON_API_KEY`; learned / Mk1.5 T1: `PICKY_VLA_T1` (perceptron | perceptron_vla | smolvla | scripted | off),
`PICKY_VLA_T1_TIMEOUT_S` / `_MAX_CHUNKS` / `_EXEC_STEPS` / `_HOME_DEG` / `_INSTRUCTION` / `_CAPTURE_HZ`,
`PICKY_PERCEPTRON_CALL_BUDGET`, `PICKY_VLA_MK15_TIMEOUT_S`; pictures while driving: `PICKY_VLA_NAV_CAPTURE_HZ`,
`PICKY_VLA_NAV_LOOK` (isaac | always | off), `PICKY_VLA_NAV_LOOK_M`. All are read through `utils.config.settings` with `getattr` defaults, so they take effect once
the utils step adds the Settings fields. Fallback: drop `vla` / `robot` from `PICKY_REAL_ARMS`, or
`PICKY_EXEC_MODE=direct` (the exact v0.2 brain path).
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

## Judges' demo (contracts 0.2.4)

- Optional RobotService extension for the checkout unload: `can_unload_to_counter`, `pick_from_basket`,
  `place_on_counter`, `counter_units` on the 2D fake (always) and `SimRobot` (Isaac when the worker advertises
  `basket_unload`, else not available there). Counter units are `ItemInstance.on_counter` (excluded from shelves in
  truth) and are bagged by `unload_basket`.
- T0: grasp `source=basket` → `pick_from_basket`; place `destination=counter` → reach check (`slot_reachable` on the
  spot) + `place_on_counter`. Learned tiers fall back to T0 for the unload (they are trained on shelf grasps).

## Known limitations

- Fake frames are schematic (flat label boxes, orthographic shelf face), not photoreal; no people are ever drawn.
- Fake collision check samples the commanded path against fixture rects (not inflated by `ROBOT_RADIUS_M`); pure
  pursuit cuts corners by up to the 0.5 m lookahead. Rovers yield only to MOVING rovers with a smaller id (parked
  rovers are passed through so a docked robot never blocks the demo).
- Battery idle drain / charging in the fake integrates wall-clock time × `realtime_factor` (instant mode: motion time
  only), so battery values are not bit-for-bit reproducible across runs at finite speed.
- Real: Isaac hosting is read from `SimLink.status()` (falls back to the last `SimHello` / `SimHosted`); a store switches
  to Isaac with the worker's robot poses (no teleport of the fake poses) unless a store reset respawns them at docks.
- Loop: put-back and `wrong_item` are real only on the 2D fake (`grab_unit` / `put_back`); on Isaac a wrong unit is
  virtual (fault overlay: truth reports it held, nothing is attached in the scene) and a REAL held unit cannot be put
  back (`unsupported_step` -> the runner's `cannot_return` rule). `drop_after_lift` on T0 is a grasp that never
  attached (same camera view as a drop). T1 is a stub until the 0.3.0 wire. The T0 grasp reach pre-check uses
  `slot_reachable` even when the fake runs with `strict_reach=False`.
- Isaac code is written but not deployed (vhagar unreachable): SO-101 URDF import API and link names are TODOs.

**Owner:** TBD · **Branch:** `arm/robot`
