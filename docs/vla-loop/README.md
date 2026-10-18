# Planner → VLA → Verifier: final API draft (becomes contracts v0.3)

The project lead's brief: "the LLM takes the natural language input, and then it splits into steps (movement to the
actual location + actually grabbing the stuff from the shelf) which these instructions get sent to the vla api. tie
in a verifier with a fake camera api". This package is that API, plus deterministic fakes, the orchestrator and tests.
It is additive on contracts v0.2: it only imports `contracts` and changes nothing in it.

```
NL / Goal ─▶ PlannerService.plan ─▶ StepPlan (typed steps + machine-checkable pre/post Conditions)
  ─▶ LoopRunner, per step:
       pre   VerifierService.verify(pre)            state atoms first (free); camera atoms on blurred frames
       act   VlaService.execute(step) ═▶ accepted ▸ running(progress) ▸ succeeded | failed(typed) | cancelled
       post  VerifierService.verify(post, VLA frames, baseline, not_before = VlaOutcome.ended_at)
       ok    commit effects once per effect key (record_pick · goal item · cart line · stop status)
       fail  decide_recovery(RetryPolicy) ─▶ retry · reground · realign · downgrade tier · reverify · ask · replan · skip
```

Run the tests from the picky repo:
`PYTHONPATH=<this dir> uv run --frozen --no-sync pytest -q <this dir>/tests -p no:cacheprovider` (67 tests, ~5 s, base
deps only; `test_integration_v02.py` also runs the loop over the real `arms.catalog` / `arms.robot` / `arms.routing`
fakes and is skipped when they can't be imported). Lint: `uv run ruff check --config <this dir>/ruff.toml <this dir>`.

## Files → future `contracts/` modules

| here | becomes | what |
|---|---|---|
| `contracts_v03/plan.py` | `contracts/plan.py` | ids, condition language (13 atoms + all/any/not, advisory flag), 9 step kinds (`look_at` = `align`), `StepPlan`, `PlanDraft`, `PlanRequest`, `default_conditions`, `validate_plan` |
| `contracts_v03/vla.py` | `contracts/vla.py` | tiers, `VlaErrorCode` + `ERROR_TRAITS`, `VlaCapabilities`, `VlaRequest` (idempotent `exec_id` + `body_hash`), `VlaStatus` stream, `VlaOutcome`, `ExecRecord`, `resolve_tier`, PROPOSED `RunSkillCmd` / `SkillProgress` / `PolicyServerStatus` + `vla_error_from_skill_result` / `t1_capability_from_status` (0.3.0 wire) |
| `contracts_v03/skill_server.py` | `contracts/skill_server.py` (0.3.0) | worker-side T1 policy-server adapter: `PolicyServerSpec`, `PolicyServerMetadata`, `PolicyClient` (sync, openpi websocket wire), `PolicyError`, `SO101_JOINT_NAMES`, `build_observation`, `check_action_chunk` |
| `contracts_v03/verify.py` | `contracts/verify.py` | `VerifyRequest` / `Verdict` / `AtomVerdict`, modes, `combine` (Kleene), `combine_verdicts` (camera vs state), `evaluate_on_truth` (fake), `evaluate_on_state` (degrade), grounding requests, `PERCEPTRON_CALLS` |
| `contracts_v03/grounding.py` | `contracts/grounding.py` | `NormBox` / `NormPoint` (0–1000 grid), `Evidence`, `ProviderCall`, `SlotLocation`, `TargetSpec`, `GroundedTarget` |
| `contracts_v03/camera_fake.py` | additions to `contracts/cameras.py` | `FrameRef`, `TruthSnapshot`, `GroundTruthSidecar`, `EvidenceCameras` + `TruthSource` Protocols |
| `contracts_v03/faults.py` | `contracts/faults.py` | `FaultEffect` (16), `FaultRule` / `FaultMatch` (incl. `occurrence`), `fault_fires`, `DEMO_SCENARIOS` |
| `contracts_v03/policy.py` | `contracts/policy.py` | `RetryPolicy` (rules + ladders + precondition repairs + budgets + breaker + degrade), `decide_recovery`, `failure_from_*`, `COMMIT_ON_VERIFIED`, `EffectRecord`, `StepAttempt`, `PlanRunState`, `LoopMetrics`, `PlanOutcome`, `ReplanRequest` |
| `contracts_v03/events.py` | additions to `contracts/events.py` | 12 event types (7 P0), `plan_envelope`, union adapter, producers, live-only + throttle tables |
| `contracts_v03/bridge.py` | additions to `contracts/tools.py` | brain tool → steps, `ToolStepOutcome`, `brain_tool_error`, `pick_result_from`, `place_result_from` |
| `contracts_v03/services.py` | additions to `contracts/services.py` | `PlannerService`, `VlaService`, `VisionProvider`, `VerifierService`, `FaultInjector`, `LoopRunner` (THE entry point), `run_vla_to_terminal`, `ServicesV03`, `exec_mode`, factories, env vars |
| `contracts_v03/api.md` · `tools.md` · `perceptron.md` | `contracts/api.md` additions · docs | REST / WS / events / mermaid sequence · tool mapping · Perceptron mapping |
| `fakes/` | the arms' `fake.py`s | `FakePlanner` (agent), `PlanLoop` (agent), `FakeVla` + `InMemoryFaultInjector` (robot), `FakeEvidenceCameras` (perception + robot truth), `FakeVerifier` (perception), `PerceptronVisionProvider` (perception, real slot), `MiniCatalog` / `MiniRobot` (test-only v0.2 world), `MemoryBus`, `ScriptedPolicyClient` (sim worker tests, no GPU) |

Per-arm implementation plan for the real pieces (classes, files, hours): `IMPLEMENTING.md`.

## Build order tonight (each step keeps `make demo` green: `PICKY_EXEC_MODE` stays `direct` until step 4)

1. **contracts PR** (`contracts/vla-loop`, `CONTRACTS_VERSION = "0.2.1"`): copy these modules in. Name agent, robot,
   perception and gateway as affected arms.
2. **robot**: `create_vla_fake` / `create_vla` = T0 over `RobotService` (copy `fakes/fake_vla.py`; navigate through
   `services.routing`), `create_truth`, `create_faults` (`PICKY_FAULTS`).
3. **perception**: `create_evidence` (`CameraService.snapshot` + retention ring), `create_verifier_fake` (copy
   `fake_verifier.py`).
4. **agent**: `create_planner_fake` (copy `fake_planner.py`), `create_runner` (copy `loop.py`); in
   `RobotBrainService.run_goal`: `if exec_mode(...) == "plan": out = await runner.run_goal(robot_id, goal)`, then
   `return await fleet.update_goal(goal.goal_id, bridge.goal_update_from_outcome(out))` (the runner never sets goal
   status; a `RobotError` from the runner propagates so the fleet reassigns).
5. **gateway**: wire the `ServicesV03` fields, the P0 routes, the new event types on `/ws`, `/api/dev/faults`.
6. **frontend**: PlanPanel + moment card from `step.failed` + VLA bar from `vla.status`.
7. P1: the Perceptron verifier in `shadow` mode, then `decide` on Isaac. P2: T1 SmolVLA through `RunSkillCmd` with the
   0.3.0 bump and an Isaac worker rebuild.

## What the judge flagged, and where it is handled

- **Deadline escape in `run_vla_to_terminal`**: the passed-deadline branch now cancels and synthesizes like the
  timeout branch. A synthesized terminal is also delivered to `on_status`, so no exec is left stuck at "accepted"
  (`test_deadline_cannot_escape_when_on_status_lags`).
- **Ladder rungs skipped on mixed failures**: rungs are now counted per matched rule (`RecoveryContext.rule_failures`;
  test).
- **0.3.0 refuses v0.2 workers**: ship as 0.2.1, and 0.3.0 only with the wire change (`CONTRACTS_VERSION_P0/_WIRE`;
  test).
- **T1 frames unblurred**: `VlaCapabilities.input_frames_blurred` gates `resolve_tier`, and
  `RunSkillCmd.blur_faces: Literal[True]` requires the worker to blur.
- **Place-back primitive**: `PlaceStep.destination="shelf"` needs `VlaCapabilities.place_back`. It only works on the 2D
  fake's overlay (`wrong_salsa` is marked `two_d_fake_only`).
- **Name clash**: the event is `TargetGroundedEvent`; the atom stays `TargetGrounded`.
- **Perceptron backend**: raises `ValueError` instead of `assert`, `compare` sends both frames, real Multilook, one
  `AsyncClient` per loop, and `wait_for` on every call.
- **Provisional commits**: the line gets `status="picked"`, the audit runs before the handoff, and checkout is blocked
  while `provisional_open` is true.
- **Too much surface**: P0 / P1 split in api.md, and the budgets live in one place (`RetryPolicy.budget`).
- **Over-picking on replan**: units are `qty - picked_qty`, `picked_qty` is updated on each verified place, and closed
  steps are carried over (`test_replan_keeps_progress_and_never_over_picks`).
- **Free retries**: `disconnected` / `busy` don't consume attempts (`counts_attempt=False`), and the reason text says
  "free retry".
- **Place that never released**: diagnosed as `place_not_released`, which retries the place
  (`gripper_empty` comes before `basket_delta`).
- **Stall / re-attach / cancel on all paths**: `run_vla_to_terminal`.
- **Inline thumbnails**: replaced by `FrameRef` everywhere.
- **Typed step params with approach poses**: `NavigateToStep.goal_pose`, `AlignStep.location`,
  `TargetSpec.location`.
- **Effect ledger**: one entry per effect key (tests assert uniqueness).
- **`add_item` exemption**: removed. Drafts can't add items, and unknown item ids are errors.
- **Perceptron model ids**: mk1.5 / mk1 only.

## Things found while building (fixed here, worth knowing)

- **Stale-frame cutoff.** `not_before` must be the motion END (`VlaOutcome.ended_at`), not the terminal status
  timestamp. `capture_after` frames are taken after the motion but before the terminal status, so the terminal
  timestamp would mark every frame stale.
- **Breakers.** The VLA service doesn't know the runner's breakers, so the runner sends the concrete resolved tier,
  never "auto".
- **Advisory sanity atoms.** Camera-only sanity atoms (shelf in view after align, item on shelf before grounding) are
  advisory. Otherwise state-only degrade asks the shopper at every shelf.
- **Routed paths.** T0 navigate must follow a routed path. The real 2D fake rejects straight lines through shelves
  ("path crosses an obstacle").
- **Per-frame judging.** The fake verifier judges each camera atom from that frame's sidecar, so a stale frame really
  would cause a false "gripper is empty" without `not_before` (checked).

## Adversarial review (fixed here; `tests/test_review_fixes.py`, 12 tests)

- **Crash on put-back**: `return_item` read `GraspStep.slot_id`, which does not exist. Any wrong-item recovery where
  the held SKU is also on the list raised `AttributeError`. It now returns the unit to its own slot
  (`ItemInstance.slot_id`).
- **Held unit counted twice / as the wrong item**: the old `place_held` repair put whatever the gripper held into the
  basket as a unit of the current item. A leftover jar gave 2 picks but a 1-unit cart line, and a foreign SKU was
  recorded as the item. Now a grasp pre-check that finds the target SKU gives `already_holding` → `adopt_held` (no
  motion, committed once). Any other SKU gives `return_held`, which puts it back on its shelf. On T0 there is no
  place-back, so rule `cannot_return` asks and then escalates. `goal_item_progress` also never goes above `qty`.
- **Produce choice lost**: a re-ground cleared the shopper's unit, and only the first unit of a multi-unit item got
  a choice. Now each chosen unit goes to its own unit's ground and grasp steps, survives a re-ground, and
  `multi=True` when more than one unit is open. The ranking uses v0.2 `perception.rank_freshness` on the last
  blurred scan frame, with ground truth as the fallback. The run publishes v0.2 `pick.choices`, so the web app's
  chooser renders. The planner fills `category` from the product, because fleet items may omit it.
- **Route stops never completed**: the runner now mirrors item status onto trip stops (`bridge.ITEM_TO_STOP_STATUS`,
  the v0.2 rule).
- **Goal status had no owner** in plan mode: added `bridge.goal_update_from_outcome`, which the brain applies.
  `POST /api/plans/{id}/cancel` goes through `fleet.cancel_goal` for goal plans. The "Who writes what" table is in
  api.md.
- **Cancel races**: a cancel during an effect commit used to leave stock decremented with no cart line. The commit
  now finishes first (`_run_to_completion`). A cancel during an ask now withdraws the question (`approvals.cancel`).
  `PlanRunState.holding_instance_id` reports a unit left in the gripper.
- **Substitute after partial picks**: a substitute re-prices the item's single line, so it is offered only while
  `picked_qty == 0`. Otherwise the rest is ordered online and the partial in-store line is kept.
- **`approval_granted` could never pass**: the verifier has no broker. `VerifyRequest.approvals` (and
  `StateFacts.approvals`) now carry the runner's answers.
- **`plan.revised` was P1**, but out-of-stock → substitute re-plans on a P0 path. It is P0 now.
- **False out-of-stock from the camera**: Perceptron sees only front facings, so `item_on_shelf` with fewer visible
  units is now 'uncertain', never 'fail'. The scan step is the only out-of-stock authority.
- **A reassigned goal reused every id**: the plan id was `plan-<goal_id>`, so when the fleet reassigned a goal, the
  new robot's exec ids re-attached to the old robot's execs (`idempotency_conflict` on every VLA step, plus a question
  to the shopper each time) and could get verdicts cached for the old robot. Now `plan.make_plan_id(goal_id,
  goal_attempt(goal))` gives `plan-g1` and then `plan-g1-a2` (from `Goal.assignments`); tools-mode live plans use the
  same key.
- **Two robots, one last unit**: tested (`MiniRobot(robot_ids=...)`). Both scan 1. The loser gets `no_instance` →
  rescan → order online. Nothing is sold twice and nothing crashes.

## Known limitations

- **Isaac wire.** T1/T2 on Isaac need `RunSkillCmd` / `SkillProgress` and a worker rebuild (0.3.0). Until then T1 can
  only be simulated (`FakeVla(simulate_t1=True)`).
- **Isaac-only faults.** `wrong_item`, `drop_after_lift` as a real detach, and place-back have no Isaac primitive yet.
  On T0 a "drop" is a grasp that never attached, which looks the same to the camera.
- **Fake cameras.** The fake camera serves a constant synthetic JPEG unless `use_camera_service=True`, which uses the
  v0.2 `CameraService.snapshot` images. Perceptron on Pillow frames should stay in `shadow` mode.
- **No slot lease.** Two robots can still drive to the same approach pose. Contention is resolved only at the grasp
  (`no_instance` → re-source), and Isaac would let them collide. A fleet-level slot reservation is the follow-up.
- **Audit mismatch recovery is coarse.** A failed pre-checkout basket audit asks the shopper "try again?" and does not
  reconcile lines unit by unit. The gateway's `provisional_open` guard keeps checkout blocked.
- **Put-back on T0.** `VlaCapabilities.place_back` is only real for the 2D overlay's virtual units. A real foreign unit
  ends in `cannot_return` → escalate.
- **Plan event size.** `plan.created` carries the whole plan (~85 KB for the 82-step taco plan). That is fine on
  localhost; send diffs in `plan.revised` if the network is slow.
