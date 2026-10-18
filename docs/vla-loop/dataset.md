# Sim dataset for the Perceptron VLA: `picky_shelf_pick_v1`

Perceptron's robotics API (`https://robotics.perceptron.inc`, Isaac SO100 participant API) fine-tunes its VLA on a
LeRobot SO100/SO101 dataset. We have no teleoperated SO-101, so this dataset comes from our sim: scripted shelf-pick
demonstrations by the LeKiwi + SO-101 rover in the Isaac store (`store_ave`, per-item grocery models). The tools are in
`arms/sim/tools/record_lerobot/`.

| Step | Where | Command |
|---|---|---|
| 1. Record raw episodes | vhagar, Isaac Python (`/data/$USER/isaacsim/python.sh`) | `record_isaac.py` |
| 2. Convert to LeRobot | any env with `lerobot[dataset]==0.6.1` | `convert_lerobot.py convert` |
| 3. Validate (Perceptron checklist) | same env | `convert_lerobot.py validate` |
| 4. Previews (MP4 / GIF / key frames) | same env | `convert_lerobot.py preview` |

## What one episode is

The rover is parked at a slot's approach pose, with up to ±2 cm forward, ±2.5 cm lateral and ±3° yaw of noise. The
lift is set once, so that a level gripper reaches the target unit. **The lift is not one of the 6 action values, so it
never moves during an episode.** The planner or robot sets the lift before it hands control to the VLA, as the
T0 tier does today. The SO-101 then plays a smooth trajectory at 30 Hz (`plan.PickPlanner`, minimum-jerk segments,
damped-least-squares IK from `arms/sim/rover_puppet.py`):

`start` (LeIsaac rest pose ±5°, random wrist roll) → `approach` (joint space, jaws open, roll → 90°) → `reach`
(straight line to the unit) → `close` (jaws close to the unit width) → `lift` (+3 cm) → `retract` → `to_basket` (arm
folded back over the rover, pan ±14–22° so it clears the mast) → `release` (jaws open, the unit drops into the
basket) → `hold`.

- Episodes are ~7–9 s, or 215–260 frames, with a random speed scale of 0.9–1.2.
- The task string is `"pick up the <product name> and put it in the basket"`.
- The targets are front-row, bottom-layer units of graspable size: width ≤ 9.5 cm, depth ≤ 12 cm, height ≤ 31 cm,
  unit center ≥ 0.55 m.
- `store_ave` has 37 SKUs and 112 such units. The SKUs are sampled round-robin, so 60 episodes cover every SKU once
  or twice.
- The grasp height jitters by ±12% of the unit height.
- Lighting is jittered per episode: dome intensity 350–950, RTX ambient 0.05–0.18, warm/cool tint.
- The sim runs no physics. The held unit follows the gripper link rigidly from the grasp to the release, then
  falls under gravity into the basket (`BASKET_OFFSET_LOCAL`). After the episode the unit goes back to its shelf
  spot.

## Format (what Perceptron's checklist asks, and what we write)

| Checklist item | `picky_shelf_pick_v1` |
|---|---|
| LeRobot v2.0 / v2.1 / v3.0 | v3.0 (what lerobot 0.6.1 writes) |
| 30 FPS | `fps: 30`; `timestamp = frame_index / 30` per episode |
| RGB side + wrist cameras, 640×480, named `observation.images.side` / `observation.images.wrist` | both are video features (lerobot's default codec), 640×480×3 |
| 6 state + action values in order `shoulder_pan.pos, shoulder_lift.pos, elbow_flex.pos, wrist_flex.pos, wrist_roll.pos, gripper.pos` | `observation.state` and `action`, float32[6], with those names |
| Arm joints in degrees, gripper calibrated 0–100 | arm = the `lekiwi.usd` joint angles in degrees (LeIsaac's USD zero is the calibrated mid pose); gripper = LeIsaac's USD range −10..100° mapped linearly to 0..100 (`convert_leisaac_action_to_lerobot`) |
| Action | `action[t] = state[t+1]` (next frame's joint targets); the last frame repeats its state |
| ≥ 1 train + ≥ 1 validation episode | `meta/info.json` `splits = {"train": "0:N", "validation": "N:M"}`. Every 10th raw episode is validation, written last |
| info.json, episode metadata, data chunks, both videos, timestamps, episode boundaries | as lerobot writes them, plus `meta/picky_episodes.json` (raw episode, SKU, slot, split) and `meta/picky_validation.json` (the checks) |

`robot_type` is `so101_follower`.

## Cameras

- **side.** Mounted on the lift carriage with the arm, behind and to the right of the SO-101 pan axis
  (−0.12, −0.30, +0.30 m), aimed 0.27 m ahead of it. Horizontal FOV 72°. It sees the arm, the gripper, the target
  unit and the shelf. It rides the lift, so every shelf level is framed the same way.
  - LeIsaac's tabletop "front" camera sits 0.5 m in front of the arm base and 0.6 m up. In a store aisle that spot
    is inside the shelf, so this mount replaces it.
  - The basket is behind the camera, so the drop is seen only by the wrist camera.
- **wrist.** Rigidly mounted on the gripper link, just above the servo housing, 4.5 cm behind and 10 cm above the
  TCP, pitched about 35° down past the jaw tips. Horizontal FOV 78°.
  - LeIsaac's LeKiwi wrist offset looks straight down at the 90° wrist roll our side grasps use, so it never saw the
    target.
  - A mount 10 cm behind the TCP saw only the servo housing.
  - Over the basket the arm is upside down, and the wrist view is dark.
- Both cameras render with one synchronous RTX replicator step per frame, at about 13–14 rendered frames/s on the
  RTX 5090. No people are in these frames (an empty sim store), so nothing needs blurring.

## Run it (vhagar)

```bash
# 0. a SEPARATE repo copy (never the live demo's /data/$USER/picky/repo) + env without the gateway link
rsync -az --delete --exclude .venv --exclude .git --exclude node_modules --exclude arms/frontend --exclude __pycache__ \
  ./ vhagar:/data/$USER/picky/repo-rec/
#    /data/$USER/picky/rec.env = sim.env with repo -> repo-rec (PICKY_ASSET_ROOT + PICKY_ROVER_USD must be set)

# 1. record (own tmux SOCKET: a tmux server crash must not take the demo sessions with it)
tmux -L picky-rec new -d -s picky-rec "source /data/$USER/picky/rec.env; cd /data/$USER/picky/repo-rec && \
  \$PY -m arms.sim.tools.record_lerobot.record_isaac --out /data/$USER/picky-vla/datasets/raw_shelf_pick_v1 \
  --episodes 60 > /data/$USER/picky/logs/rec_v1_full.log 2>&1"
#    --probe renders 5 key frames per episode (camera check, seconds); episodes with an episode.json are skipped,
#    so a re-run resumes; sampling is deterministic per (--seed, episode index)

# 2-4. LeRobot env: a separate venv (adding lerobot[dataset] to the SmolVLA venv would downgrade its av/pandas/fsspec)
V=/data/$USER/picky-vla/lerobot-ds/.venv/bin/python      # uv venv + lerobot[dataset]==0.6.1 + torch 2.11 cu128
C=/data/$USER/picky/repo-rec/arms/sim/tools/record_lerobot/convert_lerobot.py
D=/data/$USER/picky-vla/datasets
$V $C convert --raw $D/raw_shelf_pick_v1 --out $D/picky_shelf_pick_v1 --overwrite
$V $C validate --root $D/picky_shelf_pick_v1           # exit 0 = every check passed
$V $C preview --raw $D/raw_shelf_pick_v1 --out-dir $D/previews --episodes 0 7
```

Timing on vhagar: startup takes ~22 s (store + rover). Rendering runs at ~13–14 frames/s, and each episode takes
~20 s with JPEG writes, so 60 episodes take ~20 min. Conversion takes about 1 s per episode. The rendering adds
~4 GB of GPU memory.

## Upload to Perceptron (once the team key exists)

Direct upload: `POST /v1/datasets {"name": "picky_shelf_pick_v1", "source_kind": "upload"}`, then
`POST /v1/datasets/{id}/uploads` with every file under the dataset root, as relative path + size + sha256, in
batches of ≤ 500. PUT each file to its signed URL, then `POST /v1/datasets/{id}/finalize` and poll
`GET /v1/datasets/{id}/validation`. Our extra `meta/picky_*.json` files are plain `.json`, an allowed extension. To
use the Hugging Face path instead, push the folder to a public HF dataset repo and register its `repo_id`.

## Known limitations

- **Visual puppet, not physics.** The jaws close to the unit width estimated from the jaw-tip gap. Contact is not
  simulated, and the open jaws can clip into neighbouring units on a packed shelf.
- **Over-basket pose.** The folded-back pose and the drop are kinematic, and the unit falls straight down.
  For high shelves the lift stays up, so the drop is up to ~0.55 m.
- **Fixed lift.** The lift stays fixed per episode, so the policy must run with the lift preset by the planner.
- **Unverified split key.** The `validation` split key follows Hugging Face naming. Perceptron's validator may
  expect another key. `GET /v1/datasets/{id}/validation` will tell.
- **No sim-to-real transfer.** The cameras are sim mounts with no real counterpart yet. The dataset trains and tests
  the Perceptron pipeline end to end. It is not meant for sim-to-real transfer.
