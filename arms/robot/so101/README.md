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
