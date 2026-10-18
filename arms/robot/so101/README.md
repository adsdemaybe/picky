# SO-101 bench rig — natural-language pick & place

Real SO-101 (LeRobot SO-ARM101) picks the product you name off a table and drops it in the basket next to it.

```
overhead camera frame ─▶ Perceptron Mk1.5 detect(["<product>", "basket"]) ─▶ box centres (px)
   ─▶ homography (calibrate_table.py) ─▶ table xy in robot base frame
   ─▶ ikpy IK on so101_new_calib.urdf ─▶ LeRobot joint degrees
   ─▶ scripted hover → descend → grip → lift → over basket → release → home
```

**Why not a VLA end-to-end?** Perceptron Mk1 / Mk1.5 / Isaac-0.1/0.2 are perception VLMs (points, boxes), not
action models. Their only VLA, Isaac-0.5, is 36B params, runs only through Perceptron's pinned LeRobot fork, and has no
SO-101 support — not viable in 24 h. Grounding + IK needs no training data and works zero-shot on any product name.
Stretch: record ~50 episodes and fine-tune SmolVLA (commands at the bottom), keeping Perceptron as a verifier.

## Setup (Linux box; Windows works with COM ports)

```bash
pip install "lerobot[feetech]" "perceptron>=0.4.0" ikpy opencv-python
export PERCEPTRON_API_KEY=sk_live_...          # from perceptron.inc
bash arms/robot/so101/get_assets.sh            # SO-101 URDF + meshes -> arms/robot/so101/assets/

lerobot-find-port                               # unplug/replug to identify the follower port
sudo chmod 666 /dev/ttyACM*
lerobot-setup-motors --robot.type=so101_follower --robot.port=/dev/ttyACM0      # only on a fresh arm
lerobot-calibrate    --robot.type=so101_follower --robot.port=/dev/ttyACM0 --robot.id=follower
lerobot-find-cameras opencv
export SO101_PORT=/dev/ttyACM0 SO101_CAMERA=0
```

## Grounding backend: self-hosted (default for the team) or Perceptron API

`detect()` uses the **self-hosted Isaac-0.2-1B server** whenever `PICKY_VLM_URL` is set, otherwise the Perceptron API
(`PERCEPTRON_API_KEY`; the team key currently returns *QuotaExceeded*). Both read from `~/.config/picky/env`.

```bash
# on caraxes (RTX 5090), venv /data/picky/vlm: torch cu128, transformers==4.56.2, fastapi, uvicorn, pillow
cd /data/picky && HF_HOME=/data/picky/hf nohup vlm/bin/python vlm_server.py --host 127.0.0.1 --port 8765     > vlm_server.log 2>&1 &
# on the robot Mac (~/.config/picky/env)
PICKY_VLM_URL=http://100.93.119.54:8765
```

caraxes runs Tailscale in userspace mode, so the server binds `127.0.0.1` and tailnet traffic still reaches it
(no exposure to the campus network). Measured: ~2 s for target + basket including the network hop, boxes within
~2 px on a synthetic test frame; 2.5 GB VRAM. Model license: CC-BY-NC-4.0 (fine for the hackathon, not commercial).

## Calibrate the table (once per camera placement)

Tape 4 markers on the table, measure each one's (x forward, y left) in meters from the centre of the SO-101 base, then:

```bash
python -m arms.robot.so101.calibrate_table --points "0.20,0.12 0.20,-0.12 0.34,-0.12 0.34,0.12"
```

Click the markers in the same order. It prints the reprojection error and writes `homography.json`.

## Run

**Chat window (recommended)** — run in Terminal on the Mac that's plugged into the arm:

```bash
cd ~/picky   # PERCEPTRON_API_KEY from the env, or a KEY=value line in ~/.config/picky/env (chmod 600, never commit)
~/lerobot/.venv/bin/python -m arms.robot.so101.chat          # asks "execute? [y/N]" before every motion
~/lerobot/.venv/bin/python -m arms.robot.so101.chat --dry    # plan only, no arm connection
```

```
you> pick up the oat milk and put it in the basket
robot> target: 'oat milk'   put in: 'basket'   (1.4s)
robot> oat milk at (0.24, -0.06) m, basket at (0.18, 0.15) m
       hover / descend / grip / lift / over_basket / release   (joint angles shown)
execute? [y/N] y
robot> done — oat milk should be in the basket
```

Slash commands: `/dry` `/status` `/home` (pose captured at connect) `/open` `/close` `/snap` `/help` `/quit`.
On macOS the camera only works from Terminal (or a `screen` session started there), not from an SSH shell.

One-shot CLI:

```bash
python -m arms.robot.so101.pick_place "pick up the oat milk and put it in the basket" --dry-run
python -m arms.robot.so101.pick_place "pick up the oat milk and put it in the basket"
```

`--dry-run` detects, saves the annotated frame (`last_detection.jpg`) and prints the joint plan without moving the arm.
**Always dry-run first after recalibrating** and check that the hover pose looks sane.

## Tuning on the bench

- `config.py` → `grasp_z`, `hover_z`, `place_z`, gripper open/closed values, `home_deg`.
- If IK poses are mirrored or offset vs. the real arm, set `joint_sign` / `joint_offset_deg` per joint in `config.py`
  (the `so101_new_calib` URDF is meant to match LeRobot's calibration zero, but verify by commanding `home_deg`).

## Camera-guided pick & place + learning from your labels (laptop rig)

```bat
:: one-time per camera placement (~2 min, the arm touches 8 spots; keep hands clear)
..\.venv-robot\Scripts\python -m arms.robot.so101.calibrate_camera
:: plan only, then for real
..\.venv-robot\Scripts\python -m arms.robot.so101.pick_routine "pick up the gum and put it on the plate" --dry
..\.venv-robot\Scripts\python -m arms.robot.so101.pick_routine "pick up the gum and put it on the plate"
:: human-in-the-loop RL: Enter = run, type s / f + Enter (any time) = success / failure, q = quit
arms\robot\so101\train.bat "pick up the gum and put it on the plate"
```

**Closed loop (`closed_loop.py`):** the run is planned and checked one phase at a time. After the grasp (lift high) and after the carry (over the plate), Perceptron Mk1.5 is asked from the overhead camera whether the object is in the gripper; after placing and returning home, whether it is on the destination (`judge.py`, about 1 s per question). A confident "no" confirmed by a second look opens the gripper, goes home and retries from a fresh frame (up to `--attempts 3`); "unsure" never aborts. The carry is re-planned from a fresh look at the destination once the object is in hand.

Learning is Q-learning with TD updates, using those checks as step rewards: a missed or dropped grasp scores the grasp tweak 0; a held object scores the drop tweak by whether it landed on the destination, and the grasp tweak bootstraps from the best drop value. Failed attempts are learned from before the retry; your s / f label (or the camera's verdict, Enter to accept) scores the last one. State: `picky/runs/q_policy.json`; episodes in `picky/runs/<timestamp>/`; every success is also a replayable recipe: `python -m arms.robot.so101.pick_routine --replay gum_to_plate` (or `success_<timestamp>`, `last_run`). Close the Windows Camera app first - it locks the webcam. Config in `~/.config/picky/env` (SO101_PORT, SO101_CAMERA, PERCEPTRON_API_KEY).

## First successful pick (2026-10-17, team SO-101)

Picked a gum pack standing on its long edge (2 cm thick) and set it back down, driven step-by-step with
`step_ctl` (camera feedback after every move):

```text
{"xyz": [0.215, -0.115, 0.06], "roll": 40,  "max_jump": 100, "gripper": 97}   hover 1 cm above the pack
{"xyz": [0.215, -0.115, 0.06], "roll": 120, "max_jump": 100}                  jaws close across the 2 cm thickness
{"xyz": [0.225, -0.10, 0.06],  "roll": 120}                                   centre over the pack (both cameras)
{"xyz": [0.225, -0.10, 0.035], "roll": 120}                                   straddle: one jaw each side
{"gripper": 5}                                                                stalls at ~11 = holding
{"xyz": [0.225, -0.10, 0.065], "roll": 120, "gripper": 5}                    lift clear of the table
```

What made it work: `SO101_JOINT_OFFSETS=wrist_flex=92` (this arm's wrist was calibrated bent 90°; derived from
camera frames of the gripper pointing straight down), IK limited to the calibrated joint ranges, and `settle()`
closing the ~3° steady-state error of LeRobot's P=16 gains. Lessons: the jaws always close left↔right in the
wrist-camera image (use it to set `roll`), the front-facing camera can't judge depth, and a 5 × 7.5 cm pack lying
flat is wider than the jaws can open.

## Known limitations

- Fixed grasp height per run (no depth): fine for upright boxes/cans of similar height.
- Approach pitch is auto-selected: straight down when reachable, tilting toward the target further out (the SO-101's
  upper+lower arm is only ~25 cm). Wrist roll fixed at 0.
- **Hardware-tested on the team SO-101 (Mac, LeRobot 0.6.1):** safe connect (goals preset to present position before
  torque-on: zero drift), every arm joint tracks small moves within 1°, gripper closes/opens. Not yet tested: the
  overhead camera + Perceptron path and a full pick (needs the camera mounted overhead, the API key and calibration).
- `connect_robot` tolerates servo status flags (a gripper holding something reports *Overload*; stock LeRobot then
  refuses to connect until a power cycle). A joint that does not answer at all still aborts.
- Rig notes from bench testing: shoulder_pan stops at ~+26° (physical obstruction — check cables); wrist_flex rests at
  the edge of its calibrated range (range_max 4205 > 4095) and tripped its overload once — re-run `lerobot-calibrate`
  for the follower if the wrist misbehaves.
- URDF ↔ LeRobot joint mapping (`joint_sign` / `joint_offset_deg`) is still unverified on hardware: dry-run and
  check the printed hover pose before the first real pick.

## Connect the real SO-101 to the app

`bridge.py` connects the rig to the gateway **exactly like the Isaac sim worker**: outbound websocket to
`<PICKY_GATEWAY_URL>/ws/sim`, `SimHello` with `PICKY_SIM_TOKEN` and `contracts_version` 0.2.x (the gateway accepts any
0.2), then one `CommandResult` per `SimCommand`, `RobotState` on change and camera frames for `SetStreamsCmd`
(front camera at 2 fps by default). It reconnects with backoff on its own.

**1. Once, on the laptop wired to the arm** (the robot venv already has OpenCV, LeRobot, ikpy, perceptron):

```bat
..\.venv-robot\Scripts\python -m pip install "websockets>=13" "pydantic>=2.8" "pydantic-settings>=2.4" pyyaml pillow
```

Add to `~/.config/picky/env` (next to `PERCEPTRON_API_KEY`, `SO101_PORT`, `SO101_CAMERA`):

```
PICKY_GATEWAY_URL=http://localhost:8100
PICKY_SIM_TOKEN=<the token the vhagar gateway runs with>
```

**2. Reach the vhagar gateway** (it listens on vhagar's loopback): keep `ssh -N -L 8100:localhost:8100 vhagar` open in
a second terminal. A tailnet URL (`http://<vhagar tailnet IP>:8100`) works only if the gateway was started with
`GATEWAY_HOST=0.0.0.0`.

**3. Run it from the repo root** (`python` = `..\.venv-robot\Scripts\python` on Windows, `~/lerobot/.venv/bin/python`
on the Mac; on macOS run it from Terminal.app so it may use the camera):

```bash
python -m arms.robot.so101.bridge --dry                                    # real camera + plan, NO arm motion
python -m arms.robot.so101.bridge --confirm                                # the real arm, y/N before every motion
python -m arms.robot.so101.bridge --stand-in rover-hill-1 --default-prop gum --confirm   # drive Hazel's goals
python -m arms.robot.so101.bridge --dry --camera fake                      # no hardware at all (Mac test)
```

Options: `--camera fake | image:<file or folder> | <index or name>` (default `SO101_CAMERA`), `--wrist-camera <index>`
(adds `<robot>.wrist`), `--dest plate` (what picks are dropped into), `--map SKU=gum` (SKU -> the prop on the table),
`--default-prop gum` (prop for every unmapped SKU; otherwise the catalog name is grounded), `--gateway`, `--token`.

**Two modes.**

| | standalone (default) | `--stand-in rover-hill-1` |
|---|---|---|
| hosts | store `store_table`, robot `so101-real`, camera `so101-real.front` | the fleet robot + its store (`store_hill`) |
| app shows | worker in `GET /api/sim/status`, camera in `GET /api/cameras` | Hazel's live camera = the real table camera, Hazel's goals / brain steps / cart driven by the real arm, `isaac_store_ids` includes `store_hill` |
| needs | a gateway change before frames / commands flow (see gaps) | the Isaac worker must NOT host that store (`PICKY_SIM_STORES=store_ave` on vhagar) |

In stand-in mode the bridge advertises the store `loaded=false`; the robot arm then sends the store-level `ResetCmd`
with the planogram, products and robots (the bridge keeps the SKU names, the slots and Hazel's dock) and routes
`store_hill` to the bridge. Tested on a Mac against the `feat/judges-demo` gateway with `--dry --camera fake`: the
request "get me salsa and tortilla chips" at Capitol Hill went fleet -> Hazel -> bridge (navigate, scan, pick, place).

**Command mapping** (the arm is stationary; the plate next to it is the basket):

| command | real rig |
|---|---|
| `navigate` | instant success, reported pose = last waypoint (logged once: "stationary arm") |
| `scan` | real table frame; `instances` = the catalog's units of that slot (bookkeeping, not vision) |
| `pick` | Perceptron grounding of the product (or its `--map` / `--default-prop` prop) + IK plan (`pick_routine.plan`, learned steering) + hover / straddle / close / short lift. Not found -> `no_instance`; unreachable -> `grasp_failed` |
| `place_in_basket` | lift high, carry, lower onto the plate, release, retreat, home |
| `unload_basket` | bookkeeping only (a person empties the plate) |
| `capture` | real frame (`basket` / unknown kinds of this robot alias the table camera) |
| `stop` | halts at the next interpolation step; the running motion answers `cancelled`, the arm holds its pose |
| `reset` | robot: home + empty basket; store: adopt the gateway's planogram / products (no motion) |
| `set_streams`, `list_instances`, `set_slot_stock` | as the Isaac worker |
| `arm_joints`, `arm_ik`, anything else | `ok=false, error_code="internal"` (typed error, never dropped) |

**Safety.** `--confirm` asks `execute? [y/N]` in the bridge terminal before every motion (connect + home, each pick,
each place, reset); no answer in 20 s = no (`--confirm-timeout`). Type `s` + Enter at any time to stop the arm; a
gateway `stop` (fleet stop button) does the same; losing the gateway link also halts the arm; Ctrl-C / `q` halts,
homes and releases the bus. **Privacy:** every real frame is face-blurred on the laptop (OpenCV Haar cascades; whole
frame pixelated if the detector is missing) before it is streamed or sent to Perceptron, because the gateway lists
worker cameras as rendered and does not face-check them.

**Known gaps (need the gateway or physical work):**
- Standalone `so101-real` is listed but its camera shows "offline (unknown_robot)": the gateway robot arm only drives
  robots in `data/fixtures/robots.yaml` and drops other workers' frames. Use stand-in mode, or teach `SimRobot` about
  worker-only robots (capture by `camera_id` + relay their frames + include their cameras in `set_streams`).
- The gateway waits 15 s for a `pick` / `place_in_basket` (`SIM_DEFAULT_TIMEOUT_S`); a real pick is ~10-20 s and
  `--confirm` adds your reaction time. A late result is dropped and the brain sees `timeout`. Fix on the gateway: a
  longer timeout for workers advertising the `real_hardware` capability.
- When the bridge connects after Isaac, its `SimHello` replaces the robot arm's capability set, so Isaac's
  `arm_joints` / `arm_ik` look unavailable until Isaac reconnects (gateway keeps one capability set, not one per worker).
- Not yet run on the arm: the split pick (grip + short lift) / place (carry + release) path, timing, and the Haar
  blur on the real camera.

## Stretch: SmolVLA

```bash
lerobot-record --robot.type=so101_follower --robot.port=$SO101_PORT --robot.id=follower \
  --robot.cameras="{front: {type: opencv, index_or_path: 0, width: 640, height: 480, fps: 30}}" \
  --teleop.type=so101_leader --teleop.port=/dev/ttyACM1 --teleop.id=leader \
  --dataset.repo_id=$HF_USER/picky_grocery --dataset.num_episodes=50 \
  --dataset.single_task="Pick up the oat milk and put it in the basket"
lerobot-train --policy.path=lerobot/smolvla_base --dataset.repo_id=$HF_USER/picky_grocery \
  --batch_size=64 --steps=20000 --policy.device=cuda --policy.repo_id=$HF_USER/smolvla_picky
lerobot-rollout --strategy.type=base --policy.path=$HF_USER/smolvla_picky --robot.type=so101_follower \
  --robot.port=$SO101_PORT --robot.id=follower --task="Pick up the oat milk and put it in the basket"
```
