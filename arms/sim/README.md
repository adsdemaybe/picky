# arms/sim: Isaac Sim worker (multi-store, multi-rover)

**Purpose.** This arm runs the simulated stores. One Isaac Sim process on the GPU host **vhagar** holds **every store** side by side
and **every rover**: a small diff-drive base with a vertical lift, an SO-101 arm and a basket. The scenes are built from the
planograms. Each rover does kinematic pure-pursuit navigation, a scripted "magic grasp" pick and a basket place/unload, and
renders four camera kinds: robot `front` (pan-tilt), `wrist` and `basket`, plus store `ceiling`. It sits in the **ROBOT
SKILLS IN SIM** stage of the pipeline:
robot brain → `RobotService` (robot arm, real) → gateway `SimLink` → **`ws /ws/sim`** → this worker → `CommandResult` /
`RobotState` / RAW `CameraFrame` → perception (face blur) → cameras API / web app.

> **No backend Service.** The backend-facing Protocol is `RobotService`, which the **robot** arm owns. This package has no
> `create()` / `create_fake()` / `fake.py`. The always-available fallback is the robot fake, a 2D sim with Pillow frames.
> `arms/sim/__init__.py` exports the pure scene builders (`build_scene_spec`, `build_world_spec`, `build_rover_spec`,
> `load_world_spec`, …) and the Isaac-free `WorkerCore` / `NullBackend`.

## Scope (CLAUDE.md §3)

- [x] **P0** Scene spec for ALL stores (`scene_spec.py`). Each store sits under a translation-only `/World/Stores/<store>` Xform, with stores ≥ the largest store + 20 m apart (pitch 60 m). Every coordinate is store-local. The spec covers fixtures, boards, panels, ceiling light panels and one product prim + `ItemInstance` per stocked unit. **Every** unit is spawned, so scans count exactly; overflow shares the back rows
- [x] **P0** Rovers: base + wheels + lift mast + carriage + SO-101 (primitive links sized from the SO-ARM100 URDF) + basket at `BASKET_OFFSET_LOCAL` + camera heads, docked per `robots.yaml`
- [x] **P0** Worker protocol v0.2: `SimHello` (stores + `loaded` + `data_hash`, robots, cameras, contracts version) and `SimHosted` after resets. One `CommandResult` per `cmd_id` echoes `robot_id`. Unparsable commands get `command_error_result`. One `RobotState` per robot after welcome, ≤ 10 Hz each
- [x] **P0** Per-robot semantics: the `unknown_robot` / `busy` / `cancelled` / `not_ready` / `unknown_slot` / `no_instance` / `grasp_failed` (reach = `slot_reachable`; also gripper full / basket full) / `path_blocked` (outside the store, or another rover blocks > 10 s) / `timeout` / `battery_low` (navigate except to the dock, scan, pick) codes. Stop with `robot_id=None` stops all robots. Store-level and robot-level resets work, including stores pushed in `ResetCmd`
- [x] **P0** Kinematic pure-pursuit navigation (`navigation.py`). Magic grasp: lift to the item height, extend the SO-101, attach, retract. Place into the basket (capacity enforced). `unload_basket` at checkout. Held and basket units ride with the robot
- [x] **P0** Cameras: `<robot>.front|wrist|basket` + `<store>.ceiling-<n>`. Scans aim the pan-tilt front camera at the slot and list EVERY non-basket unit (bbox None when off-image). Captures list the visible units. `SetStreamsCmd` streams (default: every front camera at 2 fps) carry no instances. `camera_pose` is filled. Frames are RAW (`blurred=False`)
- [x] **P0** `SetSlotStockCmd`: removes the highest n first, spawns the lowest free n, applies lots in order, and emits `InstanceInfo`
- [x] **P0** Outbound WS link (`app.py`) with reconnect; the WS thread only parses + enqueues. Latest stream frame per camera only (no socket backlog)
- [x] **P0** Verified in the pipeline with the Pillow backend: gateway `/api/sim/status` shows both stores + 3 robots on `isaac`; the camera snapshots come from the worker; a shop goal ran through the worker
- [x] **P1** Readable label textures (`labels.py`): brand, name, size, price and a real EAN-13 barcode as a PNG mapped on a label quad on each unit's -y face (Isaac)
- [x] **P1** Pillow `SketchBackend` (`sketch.py`): flat-shaded 3D raster of the same specs with label text, so frames look like a store without Isaac
- [x] **P0** Isaac backend run on vhagar (`IsaacBackend`, Isaac Sim 6.0.0-rc.59, RTX 5090): both stores + 3 rovers + all 12 cameras render (labelled products, shelves, rovers); the taco request ran end to end on Isaac. Synchronous `rep.orchestrator.step` per changed capture (plain `app.update()` gave stale frames). Steps + measured timings are in `DEPLOY.md`
- [ ] **P1** Real SO-101 URDF on the lift (`--so101-urdf`, experimental importer path; joints stay at zero)
- [ ] **P2** Physics grasp (FixedJoint), animated SO-101 joints, VLA (LeRobot / LeIsaac) later, per CLAUDE.md "Later"

## Contract

| | |
|---|---|
| Implements | Worker side of `contracts/sim.py` v0.2 (no `contracts/services.py` Protocol) |
| In | `GatewayToSim` via `parse_gateway_to_sim` (lenient): `welcome`, `navigate`, `scan`, `pick`, `place_in_basket`, `unload_basket`, `capture`, `list_instances`, `reset` (one robot or a whole store; uses pushed `planogram` / `products` / `robots` / `cameras`, else `utils.data`), `stop`, `set_streams`, `set_slot_stock` |
| Out | `SimHello`, `SimHosted`, `CommandResult` (exactly one per `cmd_id`, with `state` for robot commands), `RobotState` per robot, RAW `CameraFrame` (`camera_id`, `robot_id`, `store_id`, `camera_pose`, `instances`), `InstanceInfo` (per store after builds; per robot on pick/place; per slot on set_slot_stock), `SimLog` |
| Events | none (the gateway / robot arm turn these into `robot.state` / `camera.frame` / … events) |
| `services.*` | none: wire only. `app.py` is the Isaac process's composition root and imports only `arms.sim`, `contracts`, `utils` |
| Frames | Store-local everywhere (Z-up, meters). Item label (-y face) points out of the pick face; item yaw = pick_dir + π/2 |
| Ids | `contracts.sim.instance_id(slot_id, n)` = `<slot>#nn` (front first; lots in planogram order) and `unit_freshness`, both shared with the robot fake |
| Robot model | `contracts.common`: radius 0.3 m, mast at `ARM_MOUNT_X_M`, lift `LIFT_MIN_M..LIFT_MAX_M`, reach `slot_reachable`, battery `BATTERY_*` (advisory; the robot arm owns it), dock = CHARGING |

## Files

| File | What |
|---|---|
| `scene_spec.py` | PURE: `build_scene_spec` (one store), `slot_layout` (unit positions; also used by set_slot_stock), `build_rover_spec`, `build_world_spec` / `load_world_spec` (all stores + offsets + rovers + `CameraInfo`s), defaults for missing robots/cameras. `python -m arms.sim.scene_spec` prints a summary |
| `geometry.py` | `CameraPose` (pinhole, USD matrix rows), `look_at`, `project_box`, mount → camera pose for front / wrist / basket / ceiling, gripper pose on the lift |
| `navigation.py` | `PurePursuit` diff-drive follower (lookahead 0.45 m, rotate-in-place, slow-down, final yaw) |
| `worker.py` | Isaac-free `WorkerCore` (stores, rovers, actions, cameras, streams, resets, set_slot_stock), the `SimBackend` seam, `NullBackend` |
| `sketch.py` | `SketchBackend`: Pillow 3D raster (no Isaac) |
| `labels.py` | EAN-13 modules + label PNG (Pillow) |
| `app.py` | `GatewayLink` (WS thread), `IsaacBackend` (pxr/replicator), `run()` main loop, CLI |
| `DEPLOY.md` | exact vhagar steps |
| `tests/` | scene spec (multi-store, rovers), pure pursuit + cameras + labels, worker protocol (two stores, three robots), WS round-trip, sketch frames |

## How to run

```bash
uv run python -m arms.sim.scene_spec                                    # inspect all stores (any machine)
uv run python -m arms.sim.app --backend sketch                          # full sim link, Pillow frames (Mac OK)
uv run python -m arms.sim.app --backend null --max-seconds 30           # link only, grey frames
/data/$USER/isaacsim/python.sh -m arms.sim.app                        # vhagar, real Isaac (see DEPLOY.md)
uv run pytest arms/sim                                                  # tests (base deps only)
```
Flags / env: `--stores` / `PICKY_SIM_STORES` (comma list, default all planograms), `--worker-id` / `PICKY_SIM_WORKER_ID`,
`--label-dir` / `PICKY_SIM_LABEL_DIR`, `--so101-urdf` / `PICKY_SO101_URDF`, `--backend isaac|sketch|null`,
`--max-seconds`. `PICKY_GATEWAY_URL` and `PICKY_SIM_TOKEN` come from `utils.config`.

**In the pipeline:** start the gateway with `robot` real (`PICKY_REAL_ARMS=robot`), then start the worker. Stores whose hash
matches move to the worker (`GET /api/sim/status` → `isaac_store_ids`). Stop the worker and they continue on the 2D fake.
**Fallback flag:** drop `robot` from `PICKY_REAL_ARMS`, or just don't run the worker.

## Demo-ready checklist (CLAUDE.md §6)

- [x] Worker attaches to the gateway; both stores + 3 robots are routed to it (Pillow backend, local gateway)
- [x] A request-created shop goal executes on the worker: navigate / scan / pick / place through `/ws/sim`
- [x] Every command gets exactly one result (also bad JSON with a `cmd_id`), and every failure has an error code
- [ ] `PICKY_REAL_ARMS=robot make demo` with **Isaac** on vhagar + a screen-recorded backup video (the same request via `POST /api/requests` against a gateway on vhagar completed on Isaac: 10/10 items, ~195 s)
- [ ] Scan bboxes line up with the Isaac render (they line up in the Pillow render, which uses the same camera math)
- [ ] Live view ≥ 8 fps on Isaac (tune `SetStreamsCmd` fps / number of streams)

## Known limitations

- Isaac frames cost one synchronous replicator step whenever the camera or the scene changed (~60-135 ms with 1-3 enabled
  render products on vhagar): fine for 2 fps streams + snapshots, not for many high-fps live views. Idle render products
  pause after 3 s. The URDF importer path (`--so101-urdf`) is still untested on 6.0.
- Rover self-view: the front camera (on the mast) sees the lift carriage + arm when it tilts down to a low shelf, and
  the wrist camera sees the jaws (clipped by the 5 cm near plane). The held unit rides just ahead of the jaws so the
  wrist camera is never inside it.
- Motion is set-pose and grasping is magic: no physics, and the SO-101 joints don't animate. The arm group only stretches
  along x and the lift moves.
- The front camera sits at 1.45 m on the mast, so bottom-shelf scans are steep top-down views and the camera can see its own
  rover.
- Rover avoidance is a simple "don't get closer within 2R" rule. Routing doesn't know about other rovers, so a parked
  rover on a path makes the mover wait, then fail with `path_blocked` after 10 s.
- Slots holding more units than fit stack the extras in the back rows (hidden). They are still listed by scans so counts stay
  exact.
- When disconnected, outgoing messages are dropped. The gateway resyncs from the per-robot `RobotState` + `InstanceInfo` sent on
  every welcome. In-flight actions abort silently, and the gateway has already failed them with `disconnected`.
- New env vars (`PICKY_SIM_STORES`, `PICKY_SIM_WORKER_ID`, `PICKY_SIM_LABEL_DIR`, `PICKY_SO101_URDF`) are read with
  `os.environ` in `app.py` (worker-host only). They still need lines in `.env.example`, which another PR owns.

## Contributing to this arm

Follow CLAUDE.md §5. Work on `arm/sim` (after merge: `feat/sim-<slug>` / `fix/sim-<slug>`) and touch only `arms/sim/`.
Keep `scene_spec.py`, `geometry.py`, `navigation.py`, `worker.py`, `sketch.py` free of Isaac imports so they stay testable on
a Mac. All `omni` / `pxr` / `isaacsim` code goes in `app.py`, behind `require_isaac()`. Any wire change is a
`contracts/<slug>` PR (robot and gateway are affected). Before the PR:
`uv run ruff check --fix arms/sim && uv run ruff format arms/sim && uv run pytest arms/sim`, then `make check`, then
`make rules` (this README feeds `.cursor/rules`).

**Owner:** TBD · **Branch:** `arm/sim`
