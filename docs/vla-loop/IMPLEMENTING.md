# Implementing the real pieces tonight

> **Landed as contracts 0.2.1** (branch `contracts/vla-loop`): the `contracts_v03.*` names in this folder map to
> `contracts.*` (`camera_fake` → `contracts.evidence`, `ServicesV03` → `contracts.services.Services`, `*_V03` tables →
> `LOOP_*`, events merged into `contracts.events`). Rename table + what changed: `contracts/CHANGES-0.2.1.md`. LLM
> provider decision since this was written: the planner / brain use **Gemini** via `utils.llm` (Claude optional with
> `PICKY_LLM_PROVIDER=anthropic`); read "Claude planner" below as "LLM planner (Gemini)".

Who this is for: the people writing the REAL Claude planner (agent), the T0 VLA on Isaac (robot + sim), the T1
policy-server adapter (sim + robot) and the Perceptron verifier (perception). It was written from a review of this
package as that engineer: for each piece, does the API give you every input, output, error code and timing you need,
and does it ask for anything you can't produce? Section 0 lists the gaps that were fixed in the package (tests:
`tests/test_impl_gaps.py`). Sections 1 to 4 give, per arm, the exact files, classes and functions to write, with hour
estimates. Section 5 is the order of work.

Time budget (from CLAUDE.md): it is Saturday evening. The thin E2E loop is due on `main` Sat ~9-10 PM and feature
freeze is Sun 5:30 AM. Planner, T0 and verifier fit in that window. **T1 does not fit (section 4): treat it as P2.**

## 0. Gaps found and fixed in this package

| # | Gap (who hits it) | Fix |
|---|---|---|
| 1 | Frame timestamps from vhagar are on the worker's clock, but `not_before` = `VlaOutcome.ended_at` is on the gateway clock. A worker clock that runs 0.5 s behind makes every `capture_after` frame look stale, so every check comes back uncertain. (perception, robot) | `FrameRef.ts` and `VlaOutcome.ended_at` are defined as GATEWAY clock. The evidence ring re-stamps frames when it receives them. |
| 2 | The front camera only looks at a slot during `scan`; a v0.2 `capture` points straight ahead. Grounding and the `item_on_shelf` / `slot_visible` / `shelf_tag_reads` checks on Isaac would judge the wrong view. (perception) | `EvidenceCameras.capture_refs(..., aim_slot_id=)`. On Isaac the real impl captures 'front' with `RobotService.scan(robot, slot)`; the fakes and callers pass it through. |
| 3 | The real robot-arm `TruthSource.ground_truth(frame_id)` can't know frames retained by perception, and grasp-point -> `instance_id` needs per-frame bboxes. (perception) | Documented contract: `get_frame` keeps `CameraFrame.instances` (they never leave the gateway). `ground_truth` raises KeyError for foreign frames, and the verifier falls back to `truth()` + `get_frame().instances`. |
| 4 | Verifier budget = runner timeout (both 8 s), so a verifier that used its whole budget lost the race and the loop degraded. `PICKY_VERIFY_TIMEOUT_S` meant both "per call" and "total". (perception, agent) | `verify.verify_deadline()` (timeout - 1 s). The runner sends it as `VerifyRequest.deadline_s` / `GroundRequest.deadline_s`. Every `VisionProvider` method takes `timeout_s`. New env var `PICKY_VISION_CALL_TIMEOUT_S` (4 s) caps each call. |
| 5 | The rule "held box overlaps the gripper ROI (IoU > 0.2)" had no ROI and no IoU. Box mention -> SKU and shelf-tag price parsing were not specified. (perception) | `grounding.GRIPPER_ROI_WRIST`, `HELD_ROI_MIN_IOU`, `box_iou`, `point_in_box`; `verify.match_mention_to_sku` (ambiguous = no match), `count_boxes_by_sku`, `parse_price_cents`. |
| 6 | Perceptron has no calibrated confidence. A flat 0.85 would let one noisy answer override the exact robot state of a T0 magic grasp. (perception) | Documented rule on `VISION_OVERRIDE_CONFIDENCE`: >= 0.9 only when two cues agree, 0.7 for one cue. |
| 7 | `compile` trusted the draft. An LLM that repeats `fetch_item` would pick every unit twice. Forgetting an item would silently skip it. Checking out early would hand the basket off mid-trip. (agent) | `plan.normalize_draft` (deterministic) runs first in compile. It drops duplicates, unknown items and slot-less navigates, moves item asks before their pick, re-sequences picks into route order for shop goals, adds the forgotten picks and puts exactly one checkout last (or none for end_at dock / none). |
| 8 | The fake planner had no seam, so a real planner would have to re-implement plan / compile / replan. (agent) | `FakePlanner.draft(request, items, summary) -> (PlanDraft, PlannerInfo)`. `ClaudePlanner` overrides only that. If an LLM draft still fails compile, the planner recompiles the scripted draft (`PlannerInfo.fallback_reason`). |
| 9 | Nothing bounded the planner. `utils.llm` uses the SDK's 10-minute default timeout, and a raising `replan` crashed `run_plan`. (agent) | `DEFAULT_PLAN_TIMEOUT_S` / `PICKY_PLAN_TIMEOUT_S` (20 s). The runner wraps `replan` in `wait_for` + try and on failure ends only that item. `PlannerInfo.fallback_reason` records why the scripted draft ran. |
| 10 | DraftStep fields had no descriptions, so the LLM saw bare names. (agent) | Field descriptions on `DraftStep` / `PlanDraft`; they are part of the JSON schema `complete_json` sends. |
| 11 | `VlaService` docs promised that T0 `align` does a lift pre-position and a pan-tilt aim. v0.2 has no command for either. (robot) | Docs now match reality: T0 align = navigate to the approach pose; the lift moves inside `pick`; aiming = `aim_slot_id` / scan. Heartbeats are service-generated. |
| 12 | `RunSkillCmd` lacked what a worker needs to run a pretrained SO-101 policy: which cameras feed it, the lift pre-position (the policy drives only the 6 arm joints), a stop rule, a chunk cap, the model id and attach assist. Nothing told the gateway that a policy server came up after the hello (`SimHosted` has no capabilities). Result error text had no parser. (robot, sim) | `RunSkillCmd` gains `cameras`, `lift_height_m`, `max_chunks`, `success_check`, `assist_attach` and `model_id`. New `PolicyServerStatus` message. New helpers `skill_error_text` / `vla_error_from_skill_result` / `t1_capability_from_status` (encodes the availability + privacy rule). |
| 13 | No contract for the policy-server adapter itself: wire, observation keys, joint order and units, chunk checks. (sim) | New `contracts_v03/skill_server.py`: openpi websocket wire (checked against openpi's server and client source), `PolicyServerSpec`, `PolicyServerMetadata`, sync `PolicyClient` Protocol, `PolicyError`, `SO101_JOINT_NAMES`, `build_observation`, `check_action_chunk`, held-geometry constants. Plus `fakes/fake_policy.py` `ScriptedPolicyClient` so the worker can be tested without a GPU. |
| 14 | The reference Perceptron provider's SDK calls were unverified. | Checked against the `perceptron` 0.4.0 wheel. `chat.completions.create(model, messages, vision_config={"annotation_format": ...}, timeout=)`, `.annotations().boxes/points`, `multilook(context, prompts, timeout=)` -> `results[i].prompt_index/ok/completions/error` are all correct. Caveat: `perceptron.TimeoutError` is an `SDKError`, not the builtin; it is handled through `.code`. |

Not changed, but worth knowing:
- `normalize_draft` makes the LLM's job small on purpose. For shop goals the route order is fixed (the shopper sees it
  on the map), so the LLM only words questions, adds scans and writes the rationale.
- New wire message types (`run_skill`, `skill_progress`, `policy_status`) would fail v0.2 `parse_sim_message`, which
  only drops unknown KEYS. They ship together in the 0.3.0 bump. The 0.2.1 T0 loop needs no wire change.

## Prerequisites (shared, ~1.5 h, before the arm work)

| What | Where | Est. |
|---|---|---|
| contracts PR `contracts/vla-loop` (CONTRACTS_VERSION 0.2.1): copy `contracts_v03/*.py` to `contracts/` (plan, vla, verify, grounding, faults, policy, skill_server, plus additions to cameras / events / tools / services). `skill_server` + `PolicyServerStatus` are inert classes (not in any wire union), so they are 0.2.1-safe. | `contracts/` | 0.75 h |
| utils PR `utils/v03-settings`: Settings fields for every `ENV_VARS_V03` key (`picky_exec_mode`, `picky_plan_timeout_s`, `picky_verify_timeout_s`, `picky_vision_call_timeout_s`, `picky_verifier_mode`, `picky_vision_provider`, `perceptron_api_key`, `perceptron_model`, `picky_vla_tiers`, `picky_vla_t1_model`, `picky_vla_policy_url`, `picky_vla_policy_protocol`, `picky_faults`, `picky_dev`). Add `effort: str \| None = None` to `utils.llm.complete_json` (merged into `output_config`), so the planner can run Sonnet 5.5 at `low` / `medium` effort (default `high` = slower drafts). `.env.example` lines. | `utils/config.py`, `utils/llm.py` | 0.5 h |
| extras: `perception += ["perceptron>=0.4.0"]`; `sim += ["msgpack>=1.0"]` (T1 only). | `pyproject.toml` | 0.1 h |
| gateway (gateway owner; listed so nobody waits on it): `ServicesV03` fields through `SERVICE_FACTORIES_V03`, real/fake chosen by `PICKY_REAL_ARMS` keys `planner` / `vla` / `verifier` (field names, not arm names). P0 routes from api.md, `GET /api/evidence/{frame_id}.jpg` (`evidence.get_frame`, bytes only), new events on `/ws`. | `arms/gateway/registry.py`, `app.py` | 2 h |

## 1. Claude planner (agent arm): 2.5 h

Files (all new unless noted):
- `arms/agent/planner_fake.py`: copy of `fakes/fake_planner.py` (`FakePlanner`, unchanged).
- `arms/agent/planner.py`: `ClaudePlanner(FakePlanner)`.
- `arms/agent/runner.py`: copy of `fakes/loop.py` (`PlanLoop`). The runner is deterministic, so its "fake" is the same class.
- `arms/agent/__init__.py` (edit): `create_planner`, `create_planner_fake`, `create_runner`, `create_runner_fake`.
- `arms/agent/brain.py` (edit, `_BrainBase.run_goal`, line ~199).
- `arms/agent/tests/test_planner.py`.

`ClaudePlanner` overrides **only** `async def draft(self, request, items, summary) -> tuple[PlanDraft, PlannerInfo]`:
1. Build the prompt. The system prompt is frozen (cached by `utils.llm.cached_system`): the six draft kinds, "one
   fetch_item per item", "copy item_id exactly", "checkout last", "for a shop goal keep the given order". The user
   message is JSON of the RESOLVED items (`item_id, name, qty, picked_qty, optional, category, slot_id, aisle_label`;
   the aisle comes from `catalog.get_locations`), plus `budget_cents`, `end_at`, `request.instructions` and the robot
   pose.
2. `await asyncio.wait_for(utils.llm.complete_json(msgs, PlanDraft, system=SYSTEM, kind="brain", max_tokens=4000,
   retries=1, effort="low"), settings.picky_plan_timeout_s)`.
3. Success: return `PlannerInfo(origin="llm", mode="llm", model=utils.llm.model("brain"), latency_ms=...)`.
4. Failures map to the scripted draft (`self.scripted_draft(...)`) with `PlannerInfo(origin="scripted",
   fallback_reason=...)`:
   - `LLMNotConfigured` -> "no ANTHROPIC_API_KEY"
   - `TimeoutError` -> "LLM timeout after 20 s"
   - `LLMOutputError` -> "invalid draft: ..."; this also covers a `refusal`, which yields no JSON
   - `anthropic.APIStatusError` / `APIConnectionError` -> the class name
   - never raise
5. Do not override `compile`, `replan` or `expand_tool_call`. `compile` runs `normalize_draft` and the macros. If
   compile still raises for an LLM draft, `FakePlanner.plan` recompiles the scripted draft (fix 8). `replan` stays
   scripted: it runs mid-trip and must be fast.

Brain hook, in `_BrainBase.run_goal`:

```python
if exec_mode(self.services, settings.picky_exec_mode) == "plan":
    outcome = await self.services.runner.run_goal(robot_id, goal)   # RobotError propagates -> fleet reassigns
    return await self.services.fleet.update_goal(goal.goal_id, goal_update_from_outcome(outcome))
```

`goal_update_from_outcome` is in `bridge.py`. Keep the v0.2 path untouched for `direct`. The text-only path
(`PlanRequest.text`, dev route `/api/dev/plans`) keeps the scripted `interpret_text`. In the pipeline, NL ->
items happens upstream in `agent.interpret_request`, so the planner always gets a Goal.

What the API gives you, and what it costs:
- In: `PlanRequest` (goal with items, store, budget, end_at). Items are already resolved by `FakePlanner.plan`
  (catalog search / locations).
- Out: `StepPlan` with `planner.origin/model/latency_ms/rationale/fixes/fallback_reason`.
- Errors: only `ValueError` (validation), and that can't happen after the fallback.
- Timing: one Sonnet 5.5 structured call per goal, before any motion. At low effort it typically takes a few seconds;
  the 20 s cap is a hard bound. The UI shows `plan.created` when it lands.

Tests (mock with `utils.llm.set_client`):
- valid JSON -> `origin == "llm"`
- garbage twice -> scripted with `fallback_reason`
- a client that sleeps past the timeout -> scripted
- a draft with duplicates -> every unit picked once (mirror `test_llm_planner_seam_messy_draft_still_picks_every_unit_exactly_once`)

Estimate:

| Task | Est. |
|---|---|
| `ClaudePlanner` | 1.25 h |
| runner copy + exports + brain hook | 0.75 h |
| tests | 0.5 h |

## 2. T0 VLA on Isaac (robot arm; the sim worker needs NO change): 2.5 h

T0 runs in the gateway process over the existing `RobotService` (`SimRobot`). On an Isaac store, its
navigate / pick / place / unload calls are Isaac commands. `fakes/fake_vla.py` already IS T0 over `RobotService`, so
start from it.

Files:
- `arms/robot/vla.py`: `class T0Vla` (implements `VlaService`).
- `arms/robot/faults.py`: copy `fakes/faults.py` (`InMemoryFaultInjector` + overlay; `PICKY_FAULTS`).
- `arms/robot/truth.py`: `class RobotTruth` (`TruthSource`). Isaac or fake: `RobotState` + `list_instances` +
  planogram slot geometry; the body of `FakeEvidenceCameras.truth`.
- `arms/robot/__init__.py` (edit): `create_vla`, `create_vla_fake`, `create_truth`, `create_truth_fake`,
  `create_faults`, `create_faults_fake`.
- `arms/robot/tests/test_vla.py`.

Changes from `FakeVla` in `T0Vla`:
- **Progress**: navigate progress = 1 - remaining / total path length, read from `robot.states(robot_id)` (pose
  stream), instead of fixed stages. Keep the heartbeat task: `RobotService` has no progress callback.
- **Reach**: pre-check `slot_reachable(state.pose, slot.approach_pose, slot.item_position)` from
  `catalog.get_planogram` and fail `out_of_reach` before calling `pick` (the worker only says `grasp_failed`).
- **`ended_at`**: `utcnow()` immediately after the `RobotService` call returns.
- **Evidence**: capture with `capture_refs(..., aim_slot_id=)` for 'front'.
- **Capabilities**:
  - T0 row: backend from `robot.state()`.
  - T1 row: `t1_capability_from_status(robot_id, self.policy_status, backend)`. `policy_status` stays None until the
    0.3.0 wire, so T1 is unavailable with a reason.
  - Drop `simulate_t1` in prod, or gate it behind `PICKY_VLA_TIERS=...,t1_sim` for the breaker demo.
- **Exec records**: `_execs` keeps `ExecRecord`s >= 1 h, pruned on insert.
- **Unchanged**:
  - `cancel` -> `robot.stop` (Isaac StopCmd; the in-flight command resolves `cancelled` within a physics step)
  - `RobotError` -> `VlaError.from_sim`
  - `disconnected` = free retry; the robot continues on the 2D fake from its last state

Timing on Isaac (from `arms/sim/worker.py` constants), plus a `/ws/sim` round trip:

| Step | Time |
|---|---|
| navigate | path length / `max_speed_mps` |
| pick | lift travel / 0.3 m/s + 1.3 s |
| place | 0.8 s + lift |
| unload | 1.2 s |
| scan | 0.6 s |

The default step timeouts (navigate 90 s, grasp 30 s, place 15 s) cover all of these.

Tests: run the existing contract scenarios against `T0Vla` over `FakeRobot`, and over `SimRobot` +
`LoopbackSimLink` to exercise the Isaac code path without a GPU.

Estimate:

| Task | Est. |
|---|---|
| `T0Vla` | 1.25 h |
| truth + faults copies | 0.5 h |
| tests | 0.75 h |

## 3. Perceptron verifier (perception arm): 4.5 h

Files:
- `arms/perception/evidence.py`: `class EvidenceRing` (`EvidenceCameras`).
- `arms/perception/vision/perceptron.py`: copy of `fakes/perceptron_provider.py` (`PerceptronVisionProvider`).
- `arms/perception/vision/claude.py`: optional `ClaudeVisionProvider` (haiku via `utils.llm`), P1.
- `arms/perception/verifier.py`: `class PerceptionVerifier` (`VerifierService`).
- `arms/perception/verifier_fake.py`: copy of `fakes/fake_verifier.py`.
- `arms/perception/__init__.py` (edit): `create_verifier`, `create_verifier_fake`, `create_evidence`,
  `create_evidence_fake`.
- `arms/perception/tests/test_verifier.py`.

`EvidenceRing.capture_refs(robot_id, cameras, *, not_before, context, aim_slot_id)`:
1. Capture each camera:
   - 'front' + `aim_slot_id` while the robot is idle: `robot.scan(robot_id, slot)`. It returns RAW, so blur it with
     `PerceptionService.blur_faces`.
   - otherwise: `CameraService.snapshot(robot_camera_id(robot_id, kind))`, which is already blurred.
2. Re-stamp `ts = utcnow()`.
3. Set `frame_id = f"{camera_id}:{n:06d}"`.
4. Retain the frame WITH `instances` in an `OrderedDict` ring (500 frames / 10 min).
5. Return `FrameRef`s.

`get_frame` serves the retained frame; KeyError when expired.

`PerceptionVerifier.verify(req)`:
1. Return the cached verdict for `verify_id`. Set `end = loop.time() + req.deadline_s`.
2. **State atoms first**, for free. Build `StateFacts` from `robot.state` + `list_instances` + slot geometry and
   decide `robot_at` / `slot_reachable` / `battery_above` / `approval_granted` / `target_grounded` with
   `verify.evaluate_on_state`.
3. **Frames**: use the VLA's `req.frames` that are `ts >= not_before`. Capture the rest (front aimed at
   `slot_ids_in(cond)[0]`).
4. **One provider round trip per camera**, `timeout_s = min(call cap, end - now)`:
   - wrist: `multilook(["Box the object held between the gripper jaws", "Is the gripper empty? Answer yes or no.",
     <OCR prompt if label_matches>])`
   - basket: `detect(classes)` on the current frame. Before-counts come from `VerifyBaseline`, which captured a
     basket frame.
   - front: `detect` / `ocr`
5. **Judge**:
   - holding: a box with `box_iou(box, GRIPPER_ROI_WRIST) > HELD_ROI_MIN_IOU` whose mention matches the product
     (`match_mention_to_sku`). Confidence 0.9 if the "empty?" answer agrees, else 0.7.
   - label: casefold substring of `expect_text` / `label_hints`.
   - basket: `count_boxes_by_sku` delta.
   - shelf: count >= `min_count`.
   - tag: `parse_price_cents`.
6. **Fuse** dual-source atoms with `combine_verdicts(state_v, vision_v)`, then `evaluate(cond, atom_fn)` and
   `verdict_reason`.
7. **Mode** (default `DEFAULT_VERIFIER_MODE[state.backend]`):
   - `decide`: the provider decides.
   - `shadow`: `evaluate_on_truth(truth)` decides; the provider's opinion goes in `Verdict.shadow` with
     `ground_truth_agrees`.
   - `gt_only`: no provider calls.
   - A provider error falls back to ground truth with `degraded=True` and `methods_tried=["perceptron",
     "ground_truth"]`.
8. **Out of time**: undecided camera atoms are uncertain and the verdict is degraded. Raise `VerifierUnavailableError`
   only when there is no state AND no truth.

`ground(req)`:
1. Capture front aimed at `target.slot_id`.
2. In parallel: `detect([description])` + `point(f"Point to the best grasp spot on the {description}")`.
3. Pick the box containing the point (else the highest). `instance_id` = the `frame.instances` unit whose
   `NormBox.from_px(bbox)` contains the point.
4. No box -> `found=False` (recovery `grounding`).
5. In shadow mode, ground truth picks the unit and the provider's box is attached as a candidate.

`baseline()`: state basket counts + a basket `FrameRef` when the condition has basket atoms.
`capabilities` / `set_mode` / provider stats (calls, errors, p50, agreement) back `GET /api/verifier/providers`.

What the API gives you, and what it costs:
- Every input is in `VerifyRequest` / `GroundRequest` + `RobotService` + `TruthSource` + `EvidenceRing`.
- Errors never raise (provider codes go in `ProviderCall.error_code`).
- Timing:

| Call | Time | Budget |
|---|---|---|
| post-grasp check | capture + 1 multilook (+ 1 OCR) ~ 1.5-3 s | 7 s |
| ground | scan 0.6 s + 2 parallel calls ~ 2-3 s | |

Ship order:
1. `shadow` everywhere first. Ground truth decides and Perceptron's opinion is visible, so the demo is safe even if
   Perceptron misreads Pillow or Isaac frames.
2. Calibrate `GRIPPER_ROI_WRIST` on the Isaac wrist camera.
3. Flip Isaac stores to `decide` only once `/api/verifier/providers` shows >= 90 % agreement.

Tests: a scripted `VisionProvider` returning canned `VisionAnswer`s covering:
- holding vs empty, the disagreement override threshold, a timeout -> degraded
- a stale `capture_after` frame rejected
- grounding point -> `instance_id`

Estimate:

| Task | Est. |
|---|---|
| `EvidenceRing` | 1 h |
| verifier core (state / camera split, fusion, modes, deadline) | 1.5 h |
| judges | 0.75 h |
| ground | 0.5 h |
| tests | 0.75 h |

## 4. T1 VLA policy-server adapter (sim + robot): 8-12 h, P2 (do not start before the T0 loop is merged)

Honest assessment:
- The worker has no SO-101 articulation: `arm_ext` is a scalar, and the URDF import is TODO(1) in
  `arms/robot/isaac/rover_so101.py`.
- A pretrained `lerobot/smolvla_base` has never seen this scene, so zero-shot grasps will mostly fail.
- T1's demo value is the tier chip + breaker + fallback to T0, and `FakeVla(simulate_t1=True)` (`flaky_t1`
  scenario) already shows that.

If someone has spare GPU hours, build in this order. Hours are for someone who knows Isaac.

**a. Policy server on vhagar (1.5 h + env).**
- File: `arms/sim/policy_server.py`. Run it in the LeRobot env: `python -m arms.sim.policy_server --model
  lerobot/smolvla_base --port 8000`.
- openpi wire (`skill_server` docstring):
  - `websockets.asyncio.server` with compression off, max_size None
  - first frame = msgpack `PolicyServerMetadata` built from `policy.config.input_features`
  - each request: msgpack-numpy obs -> `policy` chunk -> `{"actions": ndarray[chunk, 6], "server_timing": ...}`
  - errors: a text frame, then close 1011
  - `GET /healthz`

**b. Worker client (1.5 h).**
- File: `arms/sim/policy_client.py`.
- `class OpenPiWsClient`: implements `skill_server.PolicyClient`. Uses `websockets.sync.client.connect(url,
  compression=None, max_size=None)` and `openpi_client.msgpack_numpy` (or `msgpack` with numpy ext hooks). Checks the
  metadata against `PolicyServerSpec` and raises `PolicyError('policy_unavailable')` on a mismatch. `infer` is bounded
  by `infer_timeout_s` -> `policy_timeout`. Validates with `check_action_chunk` -> `policy_diverged`.
- `class PolicyThread`: owns the client; request / result queues; polls `/healthz`; emits `PolicyServerStatus` on
  change and every 30 s via `core.emit`.

**c. SO-101 articulation in Isaac (3-5 h, biggest risk).** In `RoverSO101._import_so101` + TODO 4:
- URDF import; map joint names to `SO101_JOINT_NAMES`
- joint position targets in radians: convert from `range_m100_100` with the URDF limits
- gripper friction / physics material
- products near the grasp as dynamic rigid bodies

**d. `_RunSkill(_Action)` in `arms/sim/worker.py` (2 h).** Register it as `_ACTIONS["run_skill"]` (motion command).
1. Refuse at once on: model mismatch, policy not ready, or a robot not in `PolicyServerStatus.robot_ids`
   (`policy_unavailable`).
2. Lift to `lift_height_m` with `_approach_lift`.
3. Every `1/action_hz`: pop the next action -> joint targets.
4. When the queue is <= `refill_fraction` full: render `cmd.cameras` (only at chunk-request rate), blur with YuNet
   (`cv2.FaceDetectorYN`), `build_observation(...)` -> `PolicyThread.request`.
5. Emit `SkillProgress` >= 1 Hz.
6. Stop on `success_check` (`HELD_LIFT_MIN_M`, `HELD_JAW_BOX_M` around the TCP), `max_chunks` or `deadline_s`.
7. `assist_attach` -> `rover.holding = iid`.
8. Result: ok with `state` / `instance_id`, or `skill_error_text(code, msg)`.
9. `stop` cancels it.

Unit-test it with `fakes.fake_policy.ScriptedPolicyClient` + `NullBackend`.

**e. Wire bump 0.3.0 (0.5 h, contracts PR).**
- `RunSkillCmd` into `GatewayToSim`
- `SkillProgress` + `PolicyServerStatus` into `SimMessage`
- `MOTION_COMMANDS += run_skill`
- worker `CAPABILITIES += "vla:t1:<model>"`
- rebuild the worker

**f. Gateway side (1.5 h).**
- `T0Vla` becomes `Vla` with `_run_t1(ex)`:
  1. send `RunSkillCmd` through `SimRobot`'s link (`timeout_s = deadline_s + 2`)
  2. map `SkillProgress` (by `exec_id`) -> `VlaStatus` running (`chunk_no`, `progress`, `self_confidence`)
  3. on the result: `ended_at = utcnow()`; succeeded with `outcome.self_report = "success"` if the success check
     passed, else "unsure", plus `chunks_executed`; or failed via `vla_error_from_skill_result`
- `SimRobot._on_message` handles `PolicyServerStatus` -> `vla.policy_status`.

Timing: measure `p50_infer_ms` on vhagar. With 50-action chunks at 30 Hz and refill at 50 %, each request has
~0.8 s of slack before the queue runs dry. If inference is slower, lower `action_hz` (LeRobot's own advice).
`cancel_latency_ms` ~ 2 action ticks.

## 5. Order of work tonight

1. One person: contracts PR + utils settings / effort PR + extras (~1.5 h). Everyone else pulls `main` after it merges.
2. In parallel: agent (section 1, 2.5 h), robot (section 2, 2.5 h), perception (section 3 in `shadow`, 4.5 h),
   gateway (fields + P0 routes, 2 h).
3. Check: `PICKY_EXEC_MODE=plan make demo` green all-fake (the fakes are already wired) as soon as the gateway lands.
4. Check: `PICKY_EXEC_MODE=plan PICKY_REAL_ARMS=planner,vla,verifier make demo` on the Isaac store, with
   `PICKY_FAULTS=drop_on_first_grasp`. This is THE demo moment ("Verifier caught it").
5. Kill switch, any time: `PICKY_EXEC_MODE=direct` = the exact v0.2 brain path.
6. Only after the freeze-safe loop works: Perceptron `decide` on Isaac, then section 4 if anyone has hours left.

| Arm | Piece | Est. | Priority |
|---|---|---|---|
| shared | contracts 0.2.1 + utils settings / effort + extras | 1.5 h | P0 |
| agent | `ClaudePlanner` + runner + brain hook | 2.5 h | P0 |
| robot | `T0Vla` + `RobotTruth` + faults | 2.5 h | P0 |
| perception | `EvidenceRing` + `PerceptionVerifier` (shadow) + `PerceptronVisionProvider` | 4.5 h | P0 (shadow), P1 (decide) |
| gateway | `ServicesV03` wiring + P0 routes + evidence route | 2 h | P0 |
| sim + robot | T1: policy server, client, SO-101 articulation, `_RunSkill`, 0.3.0 wire, gateway T1 path | 8-12 h | P2 |
