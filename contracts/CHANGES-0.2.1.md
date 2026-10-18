# contracts 0.2.1: the planner → VLA → verifier loop

**Additive patch on v0.2.0.** Nothing in v0.2 changes shape. The release adds new modules, new OPTIONAL `Services`
fields (default `None`), new event types in the `Event` union and new REST routes in `api.md`. `make check` and
`make demo` stay green with every arm on its current fake. Once the gateway wires the loop services, a blank
`PICKY_EXEC_MODE` means `plan`; `PICKY_EXEC_MODE=direct` is the kill switch (the exact v0.2 brain path).

**Affected arms:** agent (planner + runner), robot (VLA + truth + faults), perception (verifier + evidence +
Perceptron provider), gateway (wiring, routes, events) and frontend (PlanPanel, VLA bar, "Verifier caught it" card).
The sim arm is not affected until 0.3.0 (T1 wire).
Design: `docs/vla-loop/` (`README.md`, `IMPLEMENTING.md`, `api.md`, `tools.md`, `perceptron.md`,
`research-design.md`). Those files still use the draft's `contracts_v03.*` names; the mapping is below.

```
NL / Goal ─▶ PlannerService.plan ─▶ StepPlan (typed steps + machine-checkable pre/post Conditions)
  ─▶ LoopRunner, per step:
       pre   VerifierService.verify(pre)            state atoms first (free); camera atoms on blurred frames
       act   VlaService.execute(step) ═▶ accepted ▸ running(progress) ▸ succeeded | failed(typed) | cancelled
       post  VerifierService.verify(post, VLA frames, baseline, not_before = VlaOutcome.ended_at)
       ok    commit effects once per effect key (record_pick · goal item · cart line · stop status)
       fail  decide_recovery(RetryPolicy) ─▶ retry · reground · realign · downgrade tier · reverify · ask · replan · skip
```

## Version policy

The version is `CONTRACTS_VERSION = "0.2.1"`. The gateway accepts an Isaac worker when `sim.versions_compatible`
says major.minor match. The worker deployed on vhagar reports 0.2.0, so it keeps working with no rebuild. This was
checked against the real `/ws/sim` handshake: a 0.2.0 hello gets `welcome ok=True`, and a 0.3.0 hello is refused
with `contracts mismatch`. The T0 loop needs no wire change. `vla.RunSkillCmd`, `vla.SkillProgress`,
`vla.PolicyServerStatus` and `contracts/skill_server.py` are INERT in 0.2.1: no `GatewayToSim` / `SimMessage` union
or `MOTION_COMMANDS` entry references them. They join the wire only with the 0.3.0 bump, together with an Isaac worker
rebuild (T1, P2). README rule 2 would call new models / events a minor bump. They ship as a patch on purpose
(see the exception noted there): a minor bump would refuse the deployed worker, and nothing on the wire changed.

## LLM and vision providers (lead decisions)

- **LLM = Gemini** through `utils.llm`. The SDK is `google-genai` 2.31.x (add it to the `agent` extra) and the key is
  `GEMINI_API_KEY`. The default model is `gemini-3.8-flash` for planner, brain and fast work; the smart model is
  `gemini-4-argon`. Anthropic stays available as an option with `PICKY_LLM_PROVIDER=anthropic`. In the contracts this
  shows up only in descriptions: `PlannerInfo.model` and `fallback_reason` ("no GEMINI_API_KEY"), the
  `PlannerService` docs, and `VisionProviderName`, which adds `"gemini"`.
- **Perceptron is THE real vision provider of the verifier** (grounding / point, yes/no checks, OCR). The SDK is
  `perceptron` 0.4.0 (add it to the `perception` extra), the key is `PERCEPTRON_API_KEY` and the base URL is
  `https://api.perceptron.inc/v1`. The models are `perceptron-mk1.5`, with `perceptron-mk1` as fallback. The SDK
  mapping per check is `verify.PERCEPTRON_CALLS` plus `docs/vla-loop/perceptron.md`.
- Every `VisionProvider` method raises `ValueError` on a frame with `blurred=False`, so faces are blurred before any
  frame reaches Perceptron or an LLM. `evidence.FrameRef.blurred` is `Literal[True]`.
- No keys exist yet. Everything runs on deterministic fakes, and real clients are tested with mocked clients only.

## Modules (new)

| Module | What | Draft source |
|---|---|---|
| `plan.py` | ids (`make_plan_id` per fleet attempt, `make_exec_id`, `make_verify_id`, `make_effect_key`), the condition language (13 atoms + `all` / `any` / `not`, advisory flag, `ATOM_SOURCES`, `ATOM_DEFAULT_CAMERAS`), 9 step kinds (`PlanStep` union; `look_at` = `align`), `default_conditions`, `StepPlan`, `PlannerInfo`, `DraftStep` / `PlanDraft` (the LLM-facing schema), `normalize_draft`, `PlanRequest`, `validate_plan` / `plan_errors` | `contracts_v03/plan.py` |
| `vla.py` | tiers, `VlaErrorCode` + `ERROR_TRAITS`, `SIM_TO_VLA_ERROR`, `VLA_TO_BRAIN_CODE`, `VlaCapabilities`, `VlaRequest` (idempotent `exec_id` + `body_hash`), `VlaStatus` stream, `VlaOutcome` (`ended_at`, gateway clock), `ExecRecord`, `resolve_tier`; inert 0.3.0 wire: `RunSkillCmd`, `SkillProgress`, `PolicyServerStatus`, `skill_error_text`, `vla_error_from_skill_result`, `t1_capability_from_status` | `contracts_v03/vla.py` |
| `verify.py` | `VerifyRequest` / `Verdict` / `AtomVerdict` / `VerifyBaseline`, modes (`decide` / `shadow` / `gt_only`), Kleene `combine`, `combine_verdicts` (camera vs state), `evaluate_on_truth` (fake), `evaluate_on_state` (degrade), `GroundRequest` / `GroundingResult`, `VisionAnswer`, `VerifierCapabilities`, `ProviderInfo`, `VerifyNowRequest`, helpers (`verify_deadline`, `match_mention_to_sku`, `count_boxes_by_sku`, `parse_price_cents`), `PERCEPTRON_CALLS` | `contracts_v03/verify.py` |
| `grounding.py` | `NormBox` / `NormPoint` (0–1000 grid), `box_iou`, `GRIPPER_ROI_WRIST`, `Evidence`, `ProviderCall`, `SlotLocation`, `TargetSpec`, `GroundedTarget` | `contracts_v03/grounding.py` |
| `evidence.py` | `FrameRef` (blurred-only pointer, `url = /api/evidence/{frame_id}.jpg`), `SlotTruth`, `TruthSnapshot`, `GroundTruthSidecar`, `FrameAnnotations`, Protocols `EvidenceCameras` (perception) + `TruthSource` (robot) | `contracts_v03/camera_fake.py`. It is not merged into `cameras.py` because it needs `sim.CameraFrame` / `ItemInstance`, and `sim` imports `cameras` (import cycle). |
| `faults.py` | `FaultEffect` (16), `FAULT_LAYERS`, `FaultMatch` (incl. `occurrence`), `FaultRule`, `FaultContext`, `fault_matches` / `fault_fires`, `FaultFiring`, `FaultScenario`, `DEMO_SCENARIOS` | `contracts_v03/faults.py` |
| `policy.py` | `RetryPolicy` (rules + ladders + precondition repairs + budgets + breaker + degrade), `decide_recovery`, `failure_from_verdict` / `failure_from_vla`, `DEFAULT_RETRY_POLICY`, `COMMIT_ON_VERIFIED`, `EffectRecord`, `StepAttempt`, `PlanRunState`, `LoopMetrics`, `PlanOutcome`, `ReplanRequest` | `contracts_v03/policy.py` |
| `bridge.py` | brain tool → steps (`BRAIN_TOOL_STEPS`), `ToolStepOutcome`, `brain_tool_error`, `pick_result_from`, `place_result_from`, `ITEM_TO_STOP_STATUS`, `goal_update_from_outcome`, `ExecMode`, **new:** `mirror_brain_step` (THE `brain.step(kind='status')` mirror, `MIRROR_MODEL='loop'`) | `contracts_v03/bridge.py`. It is not merged into `tools.py` because `plan` imports `tools` (import cycle). |
| `skill_server.py` | worker-side T1 policy-server adapter (inert until 0.3.0): `PolicyServerSpec`, `PolicyServerMetadata`, `PolicyClient`, `PolicyError`, `SO101_JOINT_NAMES`, `build_observation`, `check_action_chunk` | `contracts_v03/skill_server.py` |

Additions to existing modules:
- `services.py`:
  - the Protocols `PlannerService`, `VlaService`, `VisionProvider`, `VerifierService`, `FaultInjector` and
    `LoopRunner`
  - `run_vla_to_terminal`, `StatusCallback` and `exec_mode`
  - 7 OPTIONAL `Services` fields: `planner`, `runner`, `vla`, `verifier`, `truth`, `evidence`, `faults`
  - `LOOP_CORE_FIELDS`, `LOOP_SERVICE_PROTOCOLS`, `LOOP_SERVICE_FACTORIES` and `LOOP_ENV_VARS`
- `events.py`:
  - 12 event classes in the `Event` union, plus `LOOP_EVENT_TYPES`, `LOOP_P0_EVENT_TYPES` and
    `THROTTLED_EVENT_TYPES`
  - `plan_envelope`
  - producers: every loop event belongs to `agent` (the runner), except `demo.fault`, which belongs to `robot`
  - `camera.overlay` added to `LIVE_ONLY_EVENT_TYPES`
- `__init__.py`: the version is now 0.2.1, and the main loop names are re-exported.

### Rename table (draft → contracts)

Porting the draft's fakes is a mechanical rename. The draft's 68 tests pass against these contracts through a
plain alias shim of this table.

| draft (`contracts_v03`) | contracts 0.2.1 |
|---|---|
| `contracts_v03.<m>` (plan, vla, verify, grounding, policy, faults, bridge, skill_server) | `contracts.<m>` |
| `contracts_v03.camera_fake` | `contracts.evidence` |
| `contracts_v03.services.ServicesV03` | `contracts.services.Services` (same fields, now on the one container) |
| `EXTRA_SERVICE_PROTOCOLS_V03` / `SERVICE_FACTORIES_V03` / `ENV_VARS_V03` | `LOOP_SERVICE_PROTOCOLS` / `LOOP_SERVICE_FACTORIES` / `LOOP_ENV_VARS` |
| `contracts_v03.events.make_event_v03(t, trip_id=, ...)` | `contracts.events.make_event(t, trip_id=, ..., source="agent")`. The source is no longer defaulted, so pass `source="agent"` (runner) or `source="robot"` (`demo.fault`). |
| `parse_event_v03` / `EVENT_ADAPTER_V03` / `event_json_schema_v03` | `parse_event` / `EVENT_ADAPTER` / `event_json_schema` (one union) |
| `EVENT_TYPES_V03` / `EVENT_CLASSES_V03` / `ALL_EVENT_CLASSES_V03` | `LOOP_EVENT_TYPES` (tuple) / `EVENT_TYPES` / `EVENT_CLASSES` |
| `P0_EVENT_TYPES` / `THROTTLED_EVENT_TYPES_V03` / `LIVE_ONLY_EVENT_TYPES_V03` / `EVENT_PRODUCERS_V03` | `LOOP_P0_EVENT_TYPES` / `THROTTLED_EVENT_TYPES` / `LIVE_ONLY_EVENT_TYPES` / `EVENT_PRODUCERS` |
| `CONTRACTS_VERSION_P0` / `CONTRACTS_VERSION_WIRE` / `LOOP_API_VERSION` | `CONTRACTS_VERSION` (0.2.1); the T1 wire is 0.3.0 later |
| loop `_mirror` (inline `BrainStep(...)`) | `bridge.mirror_brain_step(plan, step, text, step_no=, ok=)` |

## Services, factories, kill switch

| `Services` field | Protocol | Arm | Factories (`LOOP_SERVICE_FACTORIES`) |
|---|---|---|---|
| `planner` | `PlannerService` | agent | `create_planner` / `create_planner_fake` |
| `runner` | `LoopRunner` | agent | `create_runner` / `create_runner_fake` (same deterministic class) |
| `vla` | `VlaService` | robot | `create_vla` / `create_vla_fake` |
| `truth` | `TruthSource` | robot | `create_truth` / `create_truth_fake` |
| `faults` | `FaultInjector` | robot | `create_faults` / `create_faults_fake` |
| `verifier` | `VerifierService` (with a `VisionProvider` inside: Perceptron) | perception | `create_verifier` / `create_verifier_fake` |
| `evidence` | `EvidenceCameras` | perception | `create_evidence` / `create_evidence_fake` |

- `LOOP_SERVICE_FACTORIES` is kept apart from `SERVICE_FACTORIES` on purpose: a v0.2 registry builds every
  `SERVICE_FACTORIES` entry and must not fail on an arm that has no loop factory.
- The 0.2.1 gateway wires `{**SERVICE_FACTORIES, **LOOP_SERVICE_FACTORIES}` and treats a missing loop factory as
  "not wired" (`None`); agent, robot and perception export all of them on this branch.
- Selecting the real implementation works like v0.2: `PICKY_REAL_ARMS` takes the field name (`planner`, `vla`,
  `verifier`) or the arm name.
- `exec_mode(services, settings.picky_exec_mode)` returns `plan` or `tools` only when that mode is configured AND
  planner, runner, vla and verifier are all wired. Anything else returns `direct`.
- `RobotBrainService.run_goal` (documented on the Protocol): in plan mode it does `outcome = await
  services.runner.run_goal(robot_id, goal)`, then `return await services.fleet.update_goal(goal.goal_id,
  bridge.goal_update_from_outcome(outcome))`. A `RobotError` from the runner propagates, so the fleet reassigns.

## Events (new, on `/ws`, envelope `events.plan_envelope(plan)` = the `goal_envelope` keys)

| Pri | type | class | producer |
|---|---|---|---|
| P0 | `plan.created` | `PlanCreated` | agent (runner) |
| P0 | `plan.revised` | `PlanRevised` | agent |
| P0 | `step.started` | `StepStarted` | agent |
| P0 | `vla.status` | `VlaStatusEvent` (≤ 2 Hz per exec on `/ws`; phase changes always) | agent |
| P0 | `step.verified` | `StepVerified` (`caught_vla_false_success`) | agent |
| P0 | `step.failed` | `StepFailed` (`decision.reason` = the moment card) | agent |
| P0 | `step.completed` | `StepCompleted` | agent |
| P0 | `plan.completed` | `PlanCompleted` (`LoopMetrics`) | agent |
| P1 | `target.grounded` | `TargetGroundedEvent` | agent |
| P1 | `vla.tier_changed` | `VlaTierChanged` | agent |
| P1 | `demo.fault` | `DemoFaultEvent` | robot (FaultInjector) |
| P1 | `camera.overlay` | `CameraOverlay` (LIVE-ONLY, like `camera.frame`) | agent |

The VLA service and the verifier publish nothing. In plan mode the runner also mirrors one `brain.step(kind="status")`
per step transition (`bridge.mirror_brain_step`) and keeps firing the v0.2 execution events (`robot.arrived`,
`scan.result`, `pick.choices`, `item.picked`, `basket.verified`; `cart.updated` and stop status go through trips). A
v0.2 web app keeps working unchanged. `direct` mode emits none of the loop events.

## REST (new; full table in `api.md` → "Planner → VLA → verifier loop")

P0:
- `GET /api/goals/{goal_id}/plan`
- `GET /api/plans/{plan_id}`
- `GET /api/plans/{plan_id}/run`
- `POST /api/plans/{plan_id}/cancel` (a goal plan is cancelled through `fleet.cancel_goal`)
- `GET /api/evidence/{frame_id}.jpg`

P1:
- `GET /api/plans/{plan_id}/attempts`
- `GET /api/fleet/robots/{robot_id}/vla` (VLA tier status per robot)
- `GET /api/vla/execs/{exec_id}` and `POST /api/vla/execs/{exec_id}/cancel`
- `GET /api/verifier`, `PUT /api/verifier/mode` and `GET /api/verifier/providers`
- `POST /api/verify`
- `GET /api/evidence/{frame_id}/annotations`

Dev (`PICKY_DEV=1`):
- `GET /api/dev/faults`, `POST /api/dev/faults` and `DELETE /api/dev/faults`
- `POST /api/dev/faults/scenario`
- `POST /api/dev/plans`

## What each arm builds (each step keeps `make demo` green; detail in `docs/vla-loop/IMPLEMENTING.md`)

Status on `contracts/vla-loop`: steps 1-5 landed (fakes in `make demo`; the Gemini and Perceptron clients are tested
with mocked clients only, no keys yet). Step 6 (frontend) is open; the gateway's `/debug` page has the plan +
verifier panels. `camera.overlay` is forwarded on the live sockets but nothing emits it yet (P1).

1. **utils / pyproject** (shared PR):
   - Settings fields + `.env.example` lines for the `LOOP_ENV_VARS` keys, plus `PICKY_LLM_PROVIDER` and
     `GEMINI_API_KEY`
   - make Gemini the default `utils.llm` provider (Anthropic stays optional)
   - extras: `agent += google-genai`, `perception += perceptron`, each with import guards
   - `uv lock`
2. **robot**:
   - `create_vla[_fake]`: T0 over `RobotService`, navigating through `services.routing`
   - `create_truth[_fake]`
   - `create_faults[_fake]` (`PICKY_FAULTS`)
   - publish `demo.fault` with `source="robot"`
3. **perception**:
   - `create_evidence[_fake]`: `CameraService.snapshot` + a retention ring, frames re-stamped on the gateway clock
   - `create_verifier[_fake]`
   - the Perceptron `VisionProvider`, mocked in tests; ship it in `shadow` first
4. **agent**:
   - `create_planner[_fake]`: Gemini `draft()` override + scripted fallback
   - `create_runner[_fake]`
   - the plan-mode hook in `RobotBrainService.run_goal`
5. **gateway**:
   - the loop fields
   - the P0 routes and the evidence JPEG route
   - the loop events on `/ws`, the `vla.status` throttle and `camera.overlay` on the live sockets
   - `/api/dev/faults`
   - checkout blocked while `PlanRunState.provisional_open` is true
6. **frontend**: PlanPanel from `plan.*` / `step.*`, the moment card from `step.failed`, and the VLA bar from
   `vla.status`. Run `make gen-types` first: the event schema now includes the loop events.

Demo: `make demo` runs plan mode with `drop_on_first_grasp` injected (`ARGS="--faults <scenario>"` for another one).
That run shows the verifier catching the dropped item and the robot retrying. `PICKY_EXEC_MODE=direct` (or
`ARGS="--exec-mode direct"`) is the kill switch at any time.

## Tests

- `contracts/tests/test_loop_models.py` ports the draft's model and decision tests: conditions, fusion, diagnosis,
  recovery ladders and budgets, tiers, faults, `normalize_draft`, skill-result mapping, T1 availability,
  policy-server shapes and verifier helpers.
- `contracts/tests/test_loop_services_events.py` covers:
  - Protocol conformance
  - the optional `Services` fields and the factory split
  - the kill switch
  - the `run_vla_to_terminal` deadline, stall, re-attach and caller-cancel paths
  - the merged event union, producers, envelopes and live-only overlay
  - the bridge helpers
  - the version policy
- v0.2 tests changed only where 0.2.1 adds something: the version string, the expected event types, and
  `camera.overlay` as live-only.
