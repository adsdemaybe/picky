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

## Autopilot demo (`--demo`): Gemini plans the route, the rovers shop

The rover's navigation brain is an LLM: before driving, the app builds a **store map from the scene** (every product
prim's world box grouped into sections by the shelf it sits on, shelf/fixture boxes plus an orthographic top-down
**depth render** as obstacles — that catches pillars and walls that are only in the building mesh — the rover
spawns and the checkout counter) and sends Gemini the shopping list + a compact map (sections, obstacle boxes, a
0.25 m ASCII occupancy picture). Gemini splits the list between the rovers, orders each route and gives a one-line
reason per leg. The rovers then **execute** that plan: grid A* (0.05 m, inflated by the rover's footprint) between
stops, driven through the same swerve command path as the keyboard (`cmd` → wheels steer and spin, kinematic base).
A stop that fails (approach spot blocked, no path, product not at the gripper) goes back to Gemini with what
happened and the product's alternatives (same SKU elsewhere) for a **replan**. Every LLM call is logged to
`<out>/llm_log.jsonl` (model, latency, prompt, response; never the key).

Per pick (Suraj's arm + grab, nothing learned): IK-table pre-grasp 11 cm in front of the product at its height →
straight slide in → measure the gripper↔product error in the sim and correct it (arm x/z + a sideways base strafe)
→ close → `grab(target, keep_offset=True)`: the product that is actually at the gripper (≤ 5 cm, checked) is
attached **where it is** — never snapped/teleported to the gripper → lift, retract → B pose → release (gravity drops
it into the basket). Settled basket items then ride with the base (re-posed with it every step: the kinematic base is
teleported, which friction can't carry). Checkout: drive to the counter, take each item out of the basket (IK into
the basket, same attach rule), put it on the counter, "paid".

```bash
# on vhagar (Isaac Sim 6.0 standalone; key in ~/.config/picky/secrets.env as GEMINI_API_KEY)
PY=/data/advaiv2/isaacsim/python.sh; D=/data/advaiv2/picky-suraj
$PY arms/sim/supermarket/teleop_app.py --scene $D/geniesim/background/supermarket/supermarket_06/background.usda \
    --robot $D/build/rm2_basket.usd --demo --robots 2 --max-items 12 --record --out $D/runs/full
# --plan-only: build the map + ask Gemini, write plan.json / topdown.jpg, exit (fast iteration on the config)
$D/tools/bin/python arms/sim/supermarket/demo/edit_video.py $D/runs/full $D/video   # needs pillow, imageio[ffmpeg]
```

Recording: replicator render products + `rgb` annotators (chase cam per rover, wrist cam per rover, side tracking cam,
overview, intro/outro orbit), one frame every 2 physics steps (30 fps of sim time), JPEGs written by a thread pool;
cameras the edit doesn't need in a phase are switched off. The scene is `supermarket_06` (10 sections: snacks,
drinks, cleaning, paper goods, personal care, …); fetch it with `tools/fetch_geniesim.py --scene supermarket_06`
(its dome-light HDR is not referenced as a USD layer, so fetch `background/common/HDR/*.hdr` too).

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
