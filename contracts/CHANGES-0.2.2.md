# contracts 0.2.2: joint-level SO-101 control for learned policies (T1 on Isaac)

**Additive patch on 0.2.1.** One new sim command, two new nested result models, two new optional `CommandResult`
fields. Nothing existing changes shape. `versions_compatible` compares major.minor, so the deployed 0.2.0 / 0.2.1
workers keep connecting; they answer an `arm_joints` command with `ok=False, error_code='internal'` (unparseable
command), and the robot arm never sends one to a worker whose `SimHello.capabilities` lacks `arm_joints`.

**Affected arms:** sim (implements it: `arms/sim/worker.py` + `arm_joints.py`, Isaac hooks in `app.py`), robot
(`SimRobot.arm_joints` + the T1 policy executor `arms/robot/vla_t1.py`), gateway (none: `SimLinkHub` routes by
`robot_id` and the new command is in `MOTION_COMMANDS` / `SIM_DEFAULT_TIMEOUTS_S`). Fakes: unchanged; the 2D fake has
no single-joint control, so T1 reports itself unavailable there and T0 runs.

## What is new (`contracts/sim.py`)

- `ArmJointsCmd` (`type='arm_joints'`, motion, robot_id required): up to `ARM_JOINTS_MAX_STEPS` (50) rows of 6 USD
  joint degrees in `SO101_JOINT_NAMES` order, applied at `hz` (default `ARM_JOINTS_HZ` = 30); optional
  `lift_height_m` (moved first, then fixed), `capture` (policy cameras rendered after the last row), `capture_each_step`
  (dataset recording), `end` (leave joint mode), `exec_id` (logs). Rows are validated (6 finite values).
- `CommandResult.arm: ArmJointState` (q_deg, joint_mode, lift, TCP in the store frame, gripper_closed, attached unit,
  steps_applied, grasp `events`) and `CommandResult.policy_frames: list[PolicyFrame]` (camera 'side' | 'wrist', step,
  RAW JPEG, 640x480, camera pose). Policy cameras are not hosted cameras (no camera id, never streamed).
- `ARM_JOINTS_CAPABILITY = 'arm_joints'` (worker hello capability), `POLICY_CAMERAS`, `PolicyCameraName`.
- `MOTION_COMMANDS` += `arm_joints`; `SIM_DEFAULT_TIMEOUTS_S['arm_joints'] = 30`.

## Why the robot arm drives the policy loop (and not the worker, `vla.RunSkillCmd`)

`RunSkillCmd` (proposed for 0.3.0) runs the policy ON the worker. 0.2.2 instead keeps the worker a policy-agnostic
joint executor + renderer and runs the loop in the robot arm (backend), one `ArmJointsCmd` per action chunk:

    reset(instruction) ─▶ ArmJointsCmd([], lift)            → state + side/wrist frames
    loop: blur ─▶ backend.predict(state, side, wrist) ─▶ ArmJointsCmd(chunk[:k]) → state + frames (+ grasp events)
    until attached (sticky grasp) / timeout / max chunks ─▶ ArmJointsCmd([], end=True)

- Latency: one websocket round trip per chunk (sub-millisecond on vhagar's loopback) next to the policy's own
  inference (SmolVLA ~140 ms on vhagar, Perceptron remote ~0.3-1 s); a chunk of 25 rows takes 0.83 s to play.
- The same `PolicyBackend` interface serves Perceptron (remote HTTP with short-lived session tokens: those secrets
  never reach the GPU box), SmolVLA on vhagar and a scripted backend for tests.
- Frames are blurred in the backend before they reach any policy (CLAUDE.md section 8), with perception's blur.
- No contracts version bump of the minor: the deployed workers keep working.

## Sticky grasp (physics substitute)

The worker runs no physics. When the gripper joint crosses below its closing threshold with a free unit within ~3 cm
of the jaw center, that unit attaches to the gripper link (`events: 'sticky_grasp:<id>'`, a `SimLog` says "sticky
grasp (physics substitute)"); opening releases it into the basket (over it), back onto its shelf spot (near it) or
onto the floor. The VLA's success is still only a claim: the verifier checks the postcondition.
