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

## Calibrate the table (once per camera placement)

Tape 4 markers on the table, measure each one's (x forward, y left) in meters from the centre of the SO-101 base, then:

```bash
python -m arms.robot.so101.calibrate_table --points "0.20,0.12 0.20,-0.12 0.34,-0.12 0.34,0.12"
```

Click the markers in the same order. It prints the reprojection error and writes `homography.json`.

## Run

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
- Top-down grasp only; wrist roll fixed at 0.
- Not yet tested on hardware — the SO-101, camera and Perceptron key were not connected when this was written.

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
