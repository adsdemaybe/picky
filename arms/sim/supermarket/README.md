# sim/supermarket — Genie Sim store + DISCOVERSE robot with basket (standalone teleop scene)

Standalone Isaac Sim 6.x app, separate from the arm's multi-store worker (`arms/sim/app.py` / `worker.py`). It is a
high-fidelity store + robot + grab-to-basket demo you can drive by keyboard; hooking it into the worker is a follow-up.

Isaac Sim 6.x world: a fully stocked **AgiBot Genie Sim supermarket** (~940 products, each a separate rigid-body prim)
and the **Picky robot**: DISCOVERSE `rm2_car` 4-wheel independent-steer base + AIRBOT Play 6-DoF arm + a basket welded
to the rear deck. Keyboard driving, arm jogging, and attach-on-contact grabbing into the basket.

## Quick start

**caraxes (UW CSE, RTX 5090) — already set up.** Isaac Sim 6.2 is the pip venv `~/isaacsim-env`; Picky data lives
on the local disk in `/data/picky` (`tools/` venv, `DISCOVERSE/`, `geniesim/`, `build/rm2_basket.usd`).

```bash
cd ~/picky && bash arms/sim/supermarket/run_sim.sh --livestream   # then connect with the streaming client (below)
bash arms/sim/supermarket/run_sim.sh --smoke                       # headless self-test; screenshots in /data/picky/smoke
```

**View and drive it from your laptop:** install the *Isaac Sim WebRTC Streaming Client 2.0.0*
([Windows](https://downloads.isaacsim.nvidia.com/isaacsim-webrtc-streaming-client-2.0.0-windows-x86_64.exe) ·
[macOS Apple silicon](https://downloads.isaacsim.nvidia.com/isaacsim-webrtc-streaming-client-2.0.0-macos-aarch64.dmg) ·
[macOS Intel](https://downloads.isaacsim.nvidia.com/isaacsim-webrtc-streaming-client-2.0.0-macos-x86_64.dmg)),
be on the tailnet, and connect to `100.93.119.54` (caraxes). Uses 49100/tcp + 47998/udp; one viewer at a time.
Click into the stream so keyboard input reaches the app.

**Fresh machine:** `bash arms/sim/supermarket/setup_linux.sh` installs Isaac Sim 5.1 standalone + tools, builds the robot and
fetches the scene (the app targets the Isaac Sim 6.x API; on a 5.1 install use a 6.x pip venv instead).

## Controls

| | |
|---|---|
| Fly the camera | hold right mouse + W/A/S/D/Q/E |
| Drive | I/K forward/back · J/L turn · U/O strafe · SPACE stop |
| Arm | 1–6 select joint · ↑/↓ move · H home · N reach-front (shelf height) · B over basket |
| Gripper | G open/close · P grab nearest product (≤30 cm) · R release |
| Camera | V chase camera ↔ free camera |

Demo loop: drive to a shelf → N → jog with ↑/↓ until the gripper is at a product → P → B → R (drops in the basket).

## What's where

| File | Does |
|---|---|
| `teleop_app.py` | standalone Isaac Sim app: opens scene, adds robot, swerve drive, teleop, grabbing, `--smoke`, `--livestream` |
| `robot/build_rm2_basket.py` | DISCOVERSE MJCF → self-contained MJCF with basket; OBJ→STL; `--check` validates in MuJoCo |
| `robot/finalize_usd.py` | flattens `mujoco-usd-converter` output, adds PhysX joint drives |
| `tools/fetch_geniesim.py` | downloads one scene + only the assets it references (0.4 GB instead of 29 GB) |
| `setup_linux.sh` / `run_sim.sh` | one-shot install / launch |
| `robot/solve_arm_ik.py` | MuJoCo IK tables for the demo (gripper level at any shelf height; reach into the basket) → `demo/arm_ik.json` |
| `demo/store_map.py` | store map from the stage: products, fixtures, sections, top-down height map, occupancy grid, A* |
| `demo/planner.py` | Gemini route planner (plan + replan), JSONL log of every request/response, no key in logs |
| `demo/autopilot.py` | `--demo`: rovers execute the plan (drive, pick, basket, checkout), cameras, frame recorder |
| `demo/edit_video.py` | cuts a recorded run into the demo video (captions, mini-map, gripper PiP, cards) |
| `demo/demo_config.json` | robots + spawns, checkout counter + slots, overview camera, optional fixed shopping list |
| `demo/so101_demo.py` | `--demo` with the SO-101 rover v3: graspable shopping list, Gemini plan, diff-drive A* trips, IK picks with jaw-contact attach, basket drop, checkout unload, camera director, recorder; `--offline` dry run without Isaac |
| `demo/so101_config.json` | SO-101 demo: spawn, checkout counter + parking slot, counter placement spots, open shelf sides |
| `demo/edit_so101.py` | cuts an SO-101 run: full cut, 45-60 s highlight, GIF, early cut, `--stills N` for review |
| `demo/record_so101.sh` | one command: record the SO-101 demo + cut the videos (on the GPU host) |
| `demo/vla_policy.py` / `demo/vla_pick.py` / `demo/vla_sidecar.py` | `--policy perceptron`: Mk1.5 tool policy (pure: prompt, tools, parsing, pixel → shelf geometry, guards), the Isaac-side planner → VLA → verifier loop, the localhost httpx sidecar to Perceptron |
| `robot/build_so101_rover.py` | robot v3 USD: PMB2 base + fixed riser + long-reach SO-101 (cut + carbon-tube extensions, reducers) + round wire basket + cameras, one articulation; writes `.kin.json` (chain, actuators, torque budget, jaw points) |
| `robot/setup_so101_rover.sh` | fetches SO-ARM100 + pmb2_robot at pinned commits, makes the tools venv, builds the USD, runs the reach check |
| `robot/so101_ik.py` / `robot/urdf_tree.py` | numpy FK/IK (DLS) for the long-reach chain; minimal URDF reader |
| `robot/reach_check.py` | reach table (shelf heights × standoffs, body clearance), basket drop, torque budget |
| `robot/preview_so101_rover.py` / `robot/validate_so101_rover.py` / `robot/make_validation_video.py` | studio renders; supermarket validation run (sweeps, diff drive, IK pick with jaw check, basket drop); review video + pick clip |

## Autopilot demo (`--demo`): Gemini plans the route, the SO-101 rover shops

`--demo` runs Picky's **SO-101 rover v3** (below) through the Genie Sim `supermarket_06` store; `--demo --demo-robot rm2`
keeps the earlier rm2_car/AIRBOT autopilot (`demo/autopilot.py`), and teleop / `--smoke` / `--livestream` still drive
the rm2_car/AIRBOT robot.

```bash
# on vhagar: one command (builds the robot USD first if it is missing), frames + videos stay on vhagar
bash arms/sim/supermarket/demo/record_so101.sh            # -> $RUN/video/picky_so101_supermarket_full.mp4, highlight, gif
bash arms/sim/supermarket/demo/record_so101.sh --plan-only  # map + shopping list + Gemini plan only (~2 min)
# offline dry run of the whole trip (no Isaac, perfect tracking) from a scene_boxes.json an Isaac run wrote:
python arms/sim/supermarket/demo/so101_demo.py --boxes RUN/scene_boxes.json --heights RUN/heights.npy \
    --topdown-meta RUN/plan.json --kin picky_so101_rover.kin.json --out /tmp/dry
```

What happens, and who decides it:

| step | how | physics / scripted / LLM |
|---|---|---|
| store map | product + fixture boxes from the stage, top-down depth render (pillars, walls), sections, 0.05 m grid inflated 0.32 m | scene data |
| shopping list | every product screened for the SO-101: ≤ 6.8 cm across the jaws, front row, free height above, a free parking pose with the shelf on the robot's right, exact 5-DoF IK (level or pitched approach), room beside it for the jaws; one SKU per section per round over ≥ 5 sections | scripted |
| product names | a close-up render of each item → Gemini vision names it ("Hand cream tube") | LLM |
| route | Gemini gets the map (sections, obstacles, ASCII grid), the items (section, position, height, parking pose) and orders the stops with a reason per leg; a stop that fails goes back to Gemini with what happened + alternatives (same SKU elsewhere) | LLM |
| driving | A* on the grid → differential drive: turn in place, pure pursuit along the path, park parallel to the shelf | joint drives on the planar base joints (scripted targets) |
| pick | IK pre-grasp → straight slide in (min-jerk) → close the moving jaw until it meets the product (jaw mesh points vs. its box) → attach only if both jaws touch it and it is between the pads → lift, retract | arm: physics drives with torque limits; attach: kinematic follow of the gripper |
| basket | tool down (yaw chosen so the item fits the round basket), swing 90° to the back, lower over the lowest part of the pile, open: the product falls in and rides on the basket floor | gravity + PhysX contacts + friction |
| checkout | park parallel to the counter, take each item out (tool down, jaws across its narrow side, same contact rule), lay it on the counter, move along the counter after six, "Paid" | as above |

Recording (`--record`): one director camera at 1920×1080 (chase while driving; a held framing per pick chosen from
the occupancy/height grid so the eye is in free air and sees the arm, the product and the basket; cuts instead of
flying through shelves) + the robot's wrist, front and basket cameras, one frame every 2 physics steps = 30 fps of sim
time. `frames.jsonl` / `events.jsonl` / `plan.json` / `llm_log.jsonl` / `stats.json` sit next to the frames;
`demo/edit_so101.py` adds the HUD, the live mini-map (Gemini's route, visited/remaining stops, the trail, the robot),
picture-in-picture and captions.

### `--policy perceptron`: Gemini plans, Perceptron Mk1.5 grasps (VLA) and verifies

Same trip, with Picky's live planner → VLA → verifier loop instead of the scripted picks (the default stays
`--policy scripted`). Mirrors the live T1 tier (`arms/robot/vla_mk15.py` + `vla_t1.py` on main) on this rover:

| step | who | how |
|---|---|---|
| route + **step plan** | Gemini (`demo/planner.py`, `PICKY_MODEL_FAST` first, the next model on HTTP 429) | per stop `steps` (approach, look, grasp, place_in_basket, verify) + a grasp hint, `checkout_steps`; close-up names also get a "looks" line (colours / shape) for the policy prompt; `demo/vla_pick.norm_steps` keeps known steps in order and adds missing required ones |
| look | Perceptron Mk1.5 | the arm unfolds to a pose 20 cm in front of the slot (store map = this scene's planogram), "is the &lt;product&gt; visible?" on the wrist frame |
| **grasp** | **Perceptron Mk1.5 as the VLA** | per step: mast + wrist JPEGs + arm state + the last steps → `demo/vla_sidecar.py` → Mk1.5 → ONE tool call (`reach_point` / `move_ee` / `set_gripper` / `lift` / `done`, the live tool set) → `vla_policy.map_action` validates + clamps (pixel ray → the product's aisle face from the map; height / along-shelf clamps) → IK (`so101_ik`) → joint drives. Close = jaws close until the moving jaw touches the product, attached only if it is between the pads. Physics is paused while Mk1.5 decides (no `app.update()`: sim time and the 30 fps recording only move with the arm) |
| fallback | scripted IK | after `--vla-attempts` (2) attempts of `--vla-max-calls` (8) steps without a grasp, or no sidecar / key: the scripted pick runs and `events.jsonl` says so (`vla_fallback`, `tier` on every `attached` / `grasp_check`); it is never counted as a VLA pick |
| place in basket | scripted IK | as in the scripted run |
| **verify** | Perceptron Mk1.5 | held: the live verifier's rule (detect the product in the wrist frame, a box over the jaws = IoU > 0.2 with `GRIPPER_ROI_WRIST`), after the retract; in basket: "is &lt;product&gt; inside the basket?" on the basket camera. A "no" re-checks once with a fresh frame; the sim's own state is logged next to every verdict (`agrees`) |
| checkout | scripted IK | unload onto the counter, "Paid" |

The mast camera (`/World/picky_vla_mast`, 640×480, 74° hfov) rides 0.3 m behind the arm's pan axis, 1.6 m up, and
looks at the slot during a pick: a sim-only camera (no mast is modelled). The sidecar runs in the repo's uv venv
(Isaac's python has no httpx, and Perceptron's CDN refuses urllib's default client); the Isaac app calls it on
localhost with urllib. Every Mk1.5 input frame is kept in `RUN/vla/`, every call in `RUN/vla_calls.jsonl` (no images,
no key), the per-item outcome in `RUN/vla_summary.json`.

```bash
# on vhagar (keys: PERCEPTRON_API_KEY + GEMINI_API_KEY in ~/.config/picky/secrets.env, never printed)
set -a; . ~/.config/picky/secrets.env; set +a
uv run python arms/sim/supermarket/demo/vla_sidecar.py --port 8766 --log $RUN/vla_calls.jsonl &
/data/advaiv2/isaacsim/python.sh arms/sim/supermarket/teleop_app.py --scene $SCENE --robot $ROBOT --demo --record \
    --out $RUN --policy perceptron --vla-url http://127.0.0.1:8766 --max-items 8 --rec-cams director,birdseye,side,wrist,basket
python demo/edit_so101.py $RUN $RUN/video   # step panel, VLA captions, Mk1.5 input inset, verifier verdicts
```

Tests (base deps, no Isaac / network): `arms/sim/supermarket/tests/test_vla_policy.py` (response parsing, pixel ray ↔
projection, tool call → Cartesian action with clamps, the live guards, detect boxes → held rule, action → grasp TCPs
→ IK on the rover's real SO-101 chain from `tests/fixtures/picky_so101_rover.kin.json`).

### Live stream to the web app (`demo/frame_stream.py`)

A stdlib-only relay (system `python3`, no GPU) serves the newest run's frames as they are written, plus the finished
MP4s, to the web app's Live page:

```bash
# on vhagar (tmux session picky-stream); --run RUN pins a run instead of following the newest one
python3 frame_stream.py --runs-root /data/advaiv2/picky-so101/runs --videos /data/advaiv2/picky-so101 --port 8120
# on the Mac
ssh -f -N -o ExitOnForwardFailure=yes -L 8120:localhost:8120 vhagar
curl localhost:8120/cams        # {"run", "cameras": [{"name", "frames", "latest"}]}; open localhost:8120/ for all cams
```

`/stream/<cam>.mjpg` (MJPEG, ~12 fps, `?fps=N`), `/frame/<cam>.jpg`, `/videos`, `/video/<name>.mp4` (Range support),
CORS `*`. Web app: Live → **Direct streams** → Head camera `http://localhost:8120/stream/director.mjpg`, Wrist
`.../wrist.mjpg`, Basket `.../side.mjpg`, Overview `.../birdseye.mjpg` (or an MP4 URL from `/videos`) → **Use these
streams**. Half-written frames are skipped (a frame is served once it is 0.2 s old and a whole JPEG); when the render
stops, the last frame is resent once a second.

The earlier rm2_car/AIRBOT autopilot (`--demo-robot rm2`, `demo/autopilot.py`, `demo/edit_video.py`):

```bash
PY=/data/advaiv2/isaacsim/python.sh; D=/data/advaiv2/picky-suraj
$PY arms/sim/supermarket/teleop_app.py --scene $D/geniesim/background/supermarket/supermarket_06/background.usda \
    --robot $D/build/rm2_basket.usd --demo --demo-robot rm2 --robots 2 --max-items 12 --record --out $D/runs/full
$D/tools/bin/python arms/sim/supermarket/demo/edit_video.py $D/runs/full $D/video   # needs pillow, imageio[ffmpeg]
```

## Picky SO-101 rover v3 (robot): PMB2 round base + long-reach Hiwonder SO-ARM101 + circular wire basket

Replaces the rm2_car/AIRBOT robot after review feedback ("the arm doesn't move like in real life, parts of the arm
disappear"), then two design rounds from the lead: no lift/mast ("a roller to raise the robot is weird": a short fixed
riser + a longer arm instead), and a bigger round base with a round wire basket that matches it.

| v1 problem | cause | v3 |
|---|---|---|
| parts vanish at some angles | OBJ→STL→USD meshes with inconsistent/flipped winding, single-sided | every STL cleaned, winding made consistent + outward per body (signed volume), 35° crease normals authored, `doubleSided = true`, `subdivisionScheme = none`, UsdPreviewSurface, no LODs |
| robot looks like boxes | DISCOVERSE ships no chassis meshes, primitives used | real meshes: exact SO-101 + exact PAL PMB2 parts; riser, arm extensions, reducers and basket generated as real geometry |
| arm motion unrealistic | preset-to-preset jumps through stiff drives | min-jerk trajectories within each actuator's speed limit (1.0–1.5 rad/s), position **and** velocity targets, arm links with gravity held by drives capped at the actuator torque, reflected rotor inertia as armature |
| product teleported onto the gripper | attach snapped the product to the gripper | close the moving jaw until it meets the product (jaw mesh points vs product box), attach only if it is between the jaws, keep its relative pose; release = gravity into the basket; it rides along by friction |

### Design and sources

| part | source | license |
|---|---|---|
| **Arm: SO-101 follower** (every STL, joint origin/axis/limit, wrist camera mount) | [TheRobotStudio/SO-ARM100 `Simulation/SO101/so101_new_calib_camera.urdf`](https://github.com/TheRobotStudio/SO-ARM100/tree/main/Simulation/SO101) @ `a758567` | Apache-2.0 |
| look: Hiwonder LeRobot SO-ARM101 follower = this open design, white PLA prints + black bus servos (HX-30HM, 30 kg·cm, 0.19 s/60°, 1:345), gripper camera | [hiwonder.com/products/lerobot-so-101](https://www.hiwonder.com/products/lerobot-so-101), [Hiwonder docs](https://docs.hiwonder.com/projects/LeRobot/en/latest/), [Hiwonder-official/hiwonder-SoArm-101](https://github.com/Hiwonder-official/hiwonder-SoArm-101) | — |
| **Base: PAL Robotics PMB2** (TIAGo base): shell, bumper ring, dock contacts, 2 drive wheels, 4 casters, poses from the xacros | [pal-robotics/pmb2_robot `pmb2_description`](https://github.com/pal-robotics/pmb2_robot/tree/humble-devel/pmb2_description) @ `e658413` | Apache-2.0 |
| riser, arm extensions (carbon tubes + printed couplers), reducer drums, basket, camera bodies | generated in `build_so101_rover.py` | (ours) |
| STS3250 servo data (50 kg·cm stall / 16 kg·cm rated @12 V, 0.133 s/60°, 74.5 g, 45.2×24.7×35 mm = STS3215 case) | [evelta.com STS3250](https://evelta.com/sts3250-12v-50kg-360deg-magnetic-encoder-double-shaft-ttl-servo/), [openelab STS3250-C002](https://openelab.io/products/feetech-sts3250-c002-servo-12v) | — |

Base candidates considered: LeKiwi (official SO-101 base, but 0.3 m and 3 small omni wheels: too small for a grocery
basket), TurtleBot 4 / Create 3 and Kobuki (~0.34–0.35 m: too small), Fetch base (~0.56 m, but COLLADA meshes and no
licence file GitHub detects on `fetch_ros`), **PMB2 (0.54 m, Apache-2.0, STL meshes, diff drive + 4 casters) → picked**.

**Layout** (robot frame x forward, origin on the floor at the base centre):
- **Base**: PMB2, footprint radius 0.269 m (bumper ring), top 0.297 m, wheel radius 0.0985 m, separation 0.4044 m.
  White shell, black bumper + top trim, one thin green accent line.
- **Riser**: fixed white column Ø 90 mm at x = +0.175 m, 0.337 m tall (no joint), black flanges; SO-101 base on top.
  Head camera on its front, basket camera on its back.
- **Arm**: the SO-101 mounted facing the robot's right (pan 0), so **pan −90° = forward (shelves), +90° = back (basket)**.
  Shoulder axis 0.75 m above the floor.
- **Basket**: Ø 0.32 m floor → Ø 0.37 m rim, 0.20 m deep, centred at x = −0.07 m so its back follows the base's curve
  1 cm inside the bumper. 40 vertical wires + 4 rings + floor wire grid (white powder-coat look) on a solid floor
  panel, black rolled rim with a green accent, black collar on the base. Physics: floor box + 16 wall boxes on base_link.

### Long-reach SO-101 (extension + motor upgrade)

The stock links are cut at plain solid sections (upper arm at link-frame x = −0.072 m, forearm at −0.058 m) and
lengthened like the usual hobbyist SO-100/101 extensions: **twin 16/14 mm pultruded carbon tubes between printed
couplers** that sleeve the cut ends (thin green band on each coupler). Upper arm **+0.25 m** (shoulder→elbow 0.116 →
0.364 m), forearm **+0.275 m** (elbow→wrist 0.135 → 0.410 m); wrist, roll, gripper and wrist camera stock. The URDF
joint origins (`elbow_flex`, `wrist_flex`) move by the same amounts, so the USD joints, `.kin.json` and the IK agree.

Lengths come from a reach study (`reach_check.py`, exact 3-D IK on the built chain, link points kept clear of base,
riser and basket): every shelf height 0.30–1.50 m at a standoff (pan axis → item, horizontal) of 0.35, 0.40 and 0.45 m
is reachable — horizontal approach from 0.6 m up, tool pitched down 5–20° for the bottom shelves (the SO-101's
shoulder only tilts 10° below horizontal), −5…−10° at 1.5 m. The basket drop (gripper straight down, 12 cm over the
rim) is exact. Parking: with a 0.40 m standoff the bumper stays ~0.2 m off the shelf face.

| shelf height | standoff 0.35 m | standoff 0.40 m | standoff 0.45 m |
|---|---|---|---|
| 0.30 m | +20° | +15° | +10° |
| 0.40 m | +20° | +15° | +5° |
| 0.50 m | +15° | +10° | +5° |
| 0.60 m | +5° | level | level |
| 0.70 m | level | level | level |
| 0.80 m | level | level | level |
| 0.90 m | level | level | level |
| 1.00 m | level | level | level |
| 1.10 m | level | level | level |
| 1.20 m | level | level | level |
| 1.30 m | level | level | level |
| 1.40 m | level | level | level |
| 1.50 m | level | -5° | -10° |

(tool pitch at the grasp: level = horizontal approach, + = pitched down)

**Torque check** (static, arm stretched out horizontally = the worst case for every pitch joint, + a 0.5 kg item at the
tool point; masses from the URDF inertials + servo upgrades + tubes 75 g/m each + couplers 12 g):

| joint | required | actuator | available (stall @12 V) | SF |
|---|---|---|---|---|
| `shoulder_lift` | 7.50 N·m (arm 3.04 + item 4.47, lever 0.91 m) | Feetech STS3250 + 3:1 reducer | 13.23 N·m | **1.76** |
| `elbow_flex` | 3.93 N·m (arm 1.24 + item 2.69, lever 0.55 m) | Feetech STS3250 + 2:1 reducer | 8.82 N·m | **2.24** |
| `wrist_flex` | 0.79 N·m (arm 0.12 + item 0.67, lever 0.14 m) | Feetech STS3215 / Hiwonder HX-30HM | 2.94 N·m | **3.72** |

Stock STS3215/HX-30HM (2.94 N·m) would give SF 0.39 at the shoulder and 0.75 at the elbow, and even a direct STS3250
(4.9 N·m) only 0.65 / 1.25, so both get a printed reducer (black drum on the pivot side of the joint): shoulder 3:1,
elbow 2:1 (η 0.9). Continuous (rated, 16 kg·cm) shoulder torque is 4.2 N·m: holding the arm alone (3.0 N·m) is
continuous-safe; a 0.5 kg item at full stretch is a short peak (the shelf grasps run far from full stretch).
`shoulder_pan` is a direct STS3250 (inertia of the long arm), wrist/roll/gripper stay stock.

### Joints, limits, drives (`.kin.json` → `actuators`)

| joint | limits | actuator | max torque | max speed | kp (N·m/rad) | kd (N·m·s/rad) | armature |
|---|---|---|---|---|---|---|---|
| `shoulder_pan` | -110° … +110° | STS3250 | 4.90 N·m | 1.2 rad/s | 163 | 11.4 | 0.028 |
| `shoulder_lift` | -100° … +100° | STS3250 3:1 | 13.23 N·m | 1.0 rad/s | 441 | 27.0 | 0.252 |
| `elbow_flex` | -97° … +97° | STS3250 2:1 | 8.82 N·m | 1.3 rad/s | 294 | 13.9 | 0.112 |
| `wrist_flex` | -95° … +95° | STS3215 / Hiwonder HX-30HM | 2.94 N·m | 1.5 rad/s | 98 | 3.4 | 0.028 |
| `wrist_roll` | -157° … +163° | STS3215 / Hiwonder HX-30HM | 2.94 N·m | 1.5 rad/s | 98 | 3.3 | 0.028 |
| `gripper` | -10° … +100° | STS3215 / Hiwonder HX-30HM | 2.94 N·m | 1.5 rad/s | 98 | 3.3 | 0.028 |
| `base_x`, `base_y` | ±100 m | virtual planar joints | 6 kN | 0.6 m/s (script) | 2e5 N/m | 2e4 | — |
| `base_yaw` | free | virtual | 4 kN·m | 0.9 rad/s (script) | 2e4 | 2e3 | — |
| `wheel_left/right_joint` | free | cosmetic spin from v, ω (diff drive) | 50 | — | 0 | 0.5 | — |

kp saturates the drive at 0.03 rad (1.7°) of error (stiff bus-servo loop; gravity sag ≤ 0.4°); kd is critical damping
for the outboard link inertia at a reference pose + armature (servo rotor inertia 0.028 kg·m² × ratio²).

**IK** (`robot/so101_ik.py`, numpy): damped least squares on a numerical Jacobian over the 5 arm joints, joint-limit
clamping, several seeds; targets = tool point + full tool rotation (shelf grasp: approach horizontal or pitched, jaws
horizontal with the wrist camera on top via `grasp_roll`) or just the approach axis (basket drop, gripper down).
Straight tool lines are IK-sampled waypoints followed with one min-jerk time scaling. The SO-101 grasps against its
fixed jaw: the product centre goes to tool x = 2 mm − width/2 − 1.5 mm, then the moving jaw closes to the contact angle.

**Cameras**: `cam_head` (riser front, 20° down), `cam_front` (base front), `cam_wrist` (SO-101 wrist camera lens, near
clip 5 mm), `cam_basket` (riser back, into the basket), `cam_chase` (behind/above, for videos).

**Navigation**: the store map inflates obstacles by `ROBOT_RADIUS = 0.32` m (footprint 0.269 m + 5 cm), see
`demo/store_map.py`; the contract rover radius (0.3 m) still bounds the footprint.

### Validation

Validation status: PENDING (vhagar unreachable while this was built; reach + torque checked offline with
`reach_check.py`, geometry checked with offline renders). Run the commands below.


### Rebuild / validate (vhagar; assets stay out of git)

```bash
PICKY_SO101=/data/advaiv2/picky-so101 bash arms/sim/supermarket/robot/setup_so101_rover.sh
# -> /data/advaiv2/picky-so101/build/picky_so101_rover.usd (+ .kin.json, reach_check.json); args: --dl1 --dl2 --shoulder-z
/data/advaiv2/isaacsim/python.sh arms/sim/supermarket/robot/preview_so101_rover.py \
    --robot /data/advaiv2/picky-so101/build/picky_so101_rover.usd --out /tmp/so101_preview   # studio renders
/data/advaiv2/isaacsim/python.sh arms/sim/supermarket/robot/validate_so101_rover.py \
    --scene /data/advaiv2/picky-suraj/geniesim/background/supermarket/supermarket_06/background.usda \
    --robot /data/advaiv2/picky-so101/build/picky_so101_rover.usd --out /data/advaiv2/picky-so101/runs/val
/data/advaiv2/picky-so101/venv/bin/python arms/sim/supermarket/robot/make_validation_video.py \
    /data/advaiv2/picky-so101/runs/val --speed 1.5
```

## Decisions & known limitations

- **Robot meshes.** DISCOVERSE publishes the rm2_car kinematics and AIRBOT arm meshes, but not the chassis/wheel/steering
  meshes (only Gaussian splats on HF). Kinematics and arm are exact; chassis, posts, deck, wheels are primitives.
- **Basket** is 5 box colliders on the base body (rigidly fixed, no joint to break). Inner 34×30×16 cm, rear of deck.
  The arm reaches a drop point 30 cm above the basket floor within 2 cm (IK-checked); arm presets in `app.py` are IK
  solutions from MuJoCo on the same model.
- **OBJ → STL** conversion is required: Isaac Sim 5.1 on DGX Spark cannot import OBJ.
- **Base driving defaults to kinematic** (pose integrated from the command, wheels/steering animated by their drives) for
  demo reliability. `--drive physics` uses the swerve wheel drives instead; wheel sign constants may need flipping.
- **Products** become kinematic rigid bodies at startup (they stay on the shelves) with their colliders switched on (the
  Genie Sim scene disables most of them). Grab = the product turns dynamic with gravity off and is teleported onto the
  gripper each frame via the tensor API; release = gravity back on, it drops into the basket. A fixed joint to the arm
  stalled PhysX (articulation rebuild), and USD xform edits don't move PhysX bodies, so neither is used.
  `--dynamic-radius R` makes stock within R m fully dynamic, but packed shelves then push themselves apart.
- **Arm vs store collisions are filtered** (collision groups) so the arm reaches into packed shelves without snagging;
  chassis and basket still collide. An invisible ground slab is added because dynamic bodies fall through the
  market's floor mesh (raycasts hit it, contacts don't).
- **Spawn** defaults to the scene's authored viewpoint (-0.4, 0.3); change with `--spawn X Y YAW_DEG`.
- **License:** Genie Sim assets are CC BY-NC-SA 4.0 (non-commercial; fine for the hackathon demo). DISCOVERSE is MIT.
- **Tested on caraxes (Isaac Sim 6.2, RTX 5090)** with `--smoke`: robot settles on the floor, drives 0.48 m, arm presets
  track within 0.008 rad (gripper lands within 1.6 cm of the MuJoCo IK targets), a shelf bottle is grabbed, carried
  and released into the basket (ends at base-frame (-0.18, 0.03, 0.90), on the basket floor). Keyboard teleop and
  the livestream launch are verified to start; driving through the stream was not tested by me (needs the client).
