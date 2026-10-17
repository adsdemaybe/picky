# arms/robot

**Purpose.** Drives the shopping robot: follow route waypoints, scan a slot, pick the exact item, place it in the
basket. Pipeline stage **SHOP** (CLAUDE.md §2): gateway calls `robot.navigate` → `robot.scan` → (perception) →
`robot.pick` → `robot.place_in_basket` for every route stop. Real = Isaac Sim worker over the `SimLink`; fake = 2D
kinematic sim (always available, the demo fallback).

## Scope (CLAUDE.md §3)

- [x] **P0** `RobotService` fake: 2D kinematic sim, pure pursuit at 0.8 m/s in sim time, `realtime_factor` speed-up
- [x] **P0** Deterministic ground-truth instances from planogram lots (`<slot_id>#NN`, per-unit freshness spread)
- [x] **P0** pick / place update holding + basket; scan/capture frames carry `instances` with pixel bboxes (fake)
- [x] **P0** Real `SimRobot` over `SimLink`: cmd_id per command, result → `RobotError(code)`, timeout/disconnect → `error` event
- [x] **P0** Waypoint follower `controllers/pure_pursuit.py` (pure Python, shared with Isaac)
- [ ] **P0** Isaac: Ridgeback-Franka loaded via `create_isaac_robot` (exported; `arms/sim/app.py` calls it under `/World/Robot/Body`, proxy if it raises), kinematic base following `NavigateAction` (`isaac/`). This arm owns the Isaac robot; sim owns the scene/worker/bridge and adds the `WorkerCore` hook that steps these actions
- [ ] **P0** Isaac: magic grasp (scripted arc + attach/reparent) and fixed basket drop pose
- [ ] **P1** Retry `grasp_failed` once with any instance (attach-on-contact fallback)
- [ ] **P1** Liveness: no `RobotState` for > 5 s → report `disconnected` so the gateway falls back to the fake
- [ ] **P2** RMPflow / cuMotion picking (Franka on a fixed base), arm motion instead of teleported item

## Contract

- Implements `contracts.services.RobotService` (`state, reset, navigate, stop, scan, capture, list_instances, pick,
  place_in_basket, frames, states`). Every failure raises `contracts.sim.RobotError(code)` with a `SimErrorCode`.
- In: `RouteLeg.waypoints` (final theta reached), `slot_id` / `sku` / `instance_id` (from `PickChoice`), planogram.
- Out: `RobotState`, RAW `CameraFrame` (with ground-truth `instances`), `ItemInstance`.
- Events: none by default (gateway republishes `states()` as `robot.state`, per `EVENT_PRODUCERS`). `SimRobot`
  publishes `error` (`arm="robot"`) on timeout/disconnect. `FakeRobot(publish_state_events=True)` also publishes
  `robot.state` for standalone runs.
- Calls at call time only: `services.sim_link` (real; or `attach_link(link)`), `services.catalog.get_planogram` /
  `get_product` (reset; fake falls back to `utils.data`), `services.events` (errors).
- Extras (not in the Protocol): `set_trip(trip_id)`, `SimRobot.attach_link(link)`, `SimRobot.is_connected()`.

## Stack

- Pick: Ridgeback-Franka (`/Isaac/Robots/Clearpath/RidgebackFranka/ridgeback_franka.usd`), kinematic base driven by
  pure pursuit via `set_world_pose`, scripted magic grasp + reparent into the basket. Isaac Sim 6.2.0, Python 3.12.
- Fallback: Franka on a kinematic box base; and the 2D fake for the whole demo.
- Gotchas (docs/research.md §9.4): `isaacsim.robot.wheeled_robots` controllers and `isaacsim.robot_motion.lula` are
  deprecated in 6.x; verify prim/joint names (`panda_hand`) in the stage browser; physical grasping is flaky (use
  magic grasp); never touch USD from the websocket thread.

## Files

| File | What |
|---|---|
| `__init__.py` | `create(services)` → `SimRobot`, `create_fake(services, **opts)` → `FakeRobot` |
| `fake.py` | `FakeRobot`: 2D kinematic sim (base deps only) |
| `service.py` | `SimRobot`: RobotService over `SimLink` |
| `loopback.py` | `LoopbackSimLink`: a `SimLink` that runs commands on a `FakeRobot` (tests, GPU-less runs) |
| `controllers/pure_pursuit.py` | Pure-Python follower (holonomic + diff-drive, final-yaw phase) |
| `instances.py` | Deterministic instances, basket positions, synthetic bboxes (reuse in the Isaac scene builder) |
| `placeholder.py` | Constant 160x120 synthetic JPEG |
| `isaac/` | `RidgebackFranka` (lazy isaacsim/pxr imports), `NavigateAction`, `MagicGraspAction`, `PlaceAction` |
| `tests/` | Pure pursuit, fake behavior, service over loopback, Isaac action state machines |

## How to run

```bash
uv run pytest arms/robot                      # base deps only
make demo                                      # robot fake (default)
PICKY_REAL_ARMS=robot make demo                # SimRobot; needs the Isaac worker connected to /ws/sim
PICKY_REAL_ARMS=robot make demo ARGS=--allow-errors   # no worker: falls back to the 2D fake, errors become warnings
```
Fake options (gateway passes them to `create_fake`): `realtime_factor` (default 5.0, `math.inf` = instant),
`speed_mps` (0.8), `publish_state_events` (False). Isaac side: `make sim` on the GPU box (see arms/sim/README.md).

## Contributing to this arm

1. Branch: `arm/robot` until the arm merges, then `feat/robot-<slug>` / `fix/robot-<slug>` off `main` (CLAUDE.md §5).
2. Change only `arms/robot/**`. Need a contract change → separate `contracts/<slug>` PR. Never import another arm.
3. Keep `fake.py` working and deterministic; keep `controllers/` pure Python (it runs inside Isaac).
4. Before pushing: `uv run ruff check --fix arms/robot && uv run ruff format arms/robot && uv run pytest arms/robot`,
   then `PICKY_REAL_ARMS=robot make demo`; after editing this README run `make rules`.
5. Open/update the PR with `gh pr create --base main --fill --label arm:robot`; tick the scope boxes above.

## Demo-ready checklist (CLAUDE.md §6)

- [ ] Every `RobotService` method works on the demo path (no `NotImplementedError`), fake and real
- [ ] `PICKY_REAL_ARMS=robot make demo` completes Maya's taco trip (navigate, avocado top-3 pick, basket)
- [ ] Timeout / disconnect / `no_instance` / `path_blocked` degrade without killing the trip (`error` event, fake fallback)
- [ ] Removing `robot` from `PICKY_REAL_ARMS` switches back to the fake
- [x] Instance ids, per-unit freshness and basket offset identical between fake and Isaac (`contracts.sim.instance_id` / `unit_freshness` / `BASKET_OFFSET_LOCAL`; `arms/gateway/tests/test_ground_truth.py`), so `PickChoice.instance_id` works on both
- [ ] Tests green; no secrets; README current

## Known limitations

- Fake frames are one constant grey JPEG (bboxes are synthetic layout, not drawn). Marked `blurred=True` because the
  image contains no people (the contract otherwise expects RAW frames that perception blurs).
- Fake `pick` does not require being at the approach pose (logs a warning) so the demo never stalls.
- Fake collision check samples the path against fixture rects (not inflated by `ROBOT_RADIUS_M`).
- Isaac code is a skeleton: prim names, articulation wrapper and arm motion are TODOs.

**Owner:** TBD · **Branch:** `arm/robot`
