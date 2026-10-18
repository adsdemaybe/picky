# How the robot brain's LLM tools map to plan steps

The v0.2 robot brain (`arms/agent`, `RobotBrainService.run_goal`) runs a Claude tool loop over `contracts.tools`
`BRAIN_TOOL_DEFINITIONS`. v0.3 keeps that loop and its prompts unchanged. Only the **motion** tools change underneath:
they become typed, verified plan steps (`contracts_v03/bridge.py`).

## Three exec modes (`PICKY_EXEC_MODE`, default `direct`)

| mode | brain | motion tools | effects (stock, cart, goal) |
|---|---|---|---|
| `direct` | v0.2 LLM / scripted loop | call `RobotService` directly (exact v0.2) | v0.2 executor, unverified |
| `tools` | v0.2 LLM / scripted loop | `LoopRunner.run_tool_step(robot, goal, tool, input)` → steps appended to the goal's ONE live plan → VLA + verifier, inner recoveries only | v0.2 executor, applied only after a VERIFIED `ToolStepOutcome` |
| `plan` | `run_goal` → `LoopRunner.run_goal` (PlannerService + runner; no LLM tool loop) | n/a (the plan IS the tool sequence) | the runner (`COMMIT_ON_VERIFIED`, once per effect key) |

`exec_mode(services, configured)` falls back to `direct` unless planner, runner, vla and verifier are all wired.

## Tool → steps (`bridge.BRAIN_TOOL_STEPS`)

| v0.2 brain tool | steps (tools mode) | executor | default pre → post | v0.2 result built from |
|---|---|---|---|---|
| `navigate_to` | `navigate_to` | VLA (classical planner inside) | battery ≥ 5 → robot_at(goal pose) | `RobotState` (unchanged) |
| `scan_shelf` | `align` (look_at), `scan_shelf` | VLA, runner (v0.2 scan path) | robot_at → slot_visible (advisory); scan: robot_at | `ScanShelfResult` (unchanged) |
| `pick_item` | `align`, `ground_target`, `grasp` | VLA, verifier, VLA | grasp: robot_at ∧ gripper_empty ∧ slot_reachable → gripper_holding(sku) ∧ label_matches (advisory) | `bridge.pick_result_from`: `picked` = VERIFIED holding, never the VLA claim |
| `place_in_basket` | `place` | VLA | gripper_holding(sku) → gripper_empty ∧ basket_delta(+1) | `bridge.place_result_from`: `basket_ok` True = verified, None = provisional, False = failed |
| `finish_goal` (end_at checkout) | `verify` (audit), `navigate_to` checkout, `handoff` | verifier, VLA, VLA | audit: basket ⊇ what was placed; handoff → basket empty | `fleet.Goal` (unchanged) |
| `locate_items`, `plan_route`, `look`, `ask_user`, `add_item`, `update_progress` | – (unchanged v0.2 executors) | – | – | unchanged |

The mandate check stays in the `pick_item` executor, before the micro-plan runs (`trips.check_line` on the FULL line
before the first unit). No motion happens for a denied line, and `PickResult.needs_approval` is still a result, not a
failure. In plan mode the runner does the same gate itself: it inserts `AskUserStep(ask_kind="over_threshold")` before
the grasp and reruns it with `GraspStep.approval_id`.

## Inner vs outer recoveries (no two planners fighting)

Inside one tool call the runner applies only `policy.INNER_RECOVERIES`: `retry`, `reverify`, `reground`, `realign`,
`downgrade_tier`, `accept_provisional` and `adopt_held` (the gripper already holds the target SKU: the pick succeeds
without motion). Any other decision (`ask_user`, `replan`, `substitute`, `skip_item`,
`mark_unavailable`, `fail_goal`) ends the call. The result is `ToolStepOutcome(ok=False, error=BrainToolError, decision)`,
and the LLM decides exactly as in v0.2. `PlannerService.replan` is never called in tools mode. `escalate` still raises
`RobotError` (battery_low / unknown_robot), so the fleet reassigns.

`bridge.brain_tool_error` maps codes back to the v0.2 vocabulary (`vla.VLA_TO_BRAIN_CODE`), so prompts keep working:

| v0.3 code | `BrainToolError.error_code` | retryable |
|---|---|---|
| `grasp_missed`, `grasp_slipped`, `wrong_item_grasped`, `place_failed`, `place_not_released`, `out_of_reach`, `atom:gripper_holding` | `grasp_failed` / `gripper_holding` | yes |
| `path_blocked` | `path_blocked` | yes |
| `nav_timeout`, `policy_timeout`, `deadline_exceeded` | `timeout` | yes |
| `disconnected`, `busy`, `not_ready`, `policy_unavailable` | same / `not_ready` | yes (free retry) |
| `no_instance`, `unknown_slot`, `atom:item_on_shelf` | `no_instance` / `unknown_slot` / `item_on_shelf` | no / yes |
| `battery_low`, `unknown_robot`, `gripper_fault` | – (raised as `RobotError`) | fatal |

The error text carries the suggested next action: `"grasp failed after 3 attempt(s): gripper closed on air. Suggested
next: ask_user."`

## Example (tools mode, `drop_on_first_grasp`)

```
LLM  → pick_item {slot_id: MINI-F1-1-02, sku: SALSA-MILD, item_id: i1}
loop   t02.1 align ✓ · t02.2 ground_target ✓ (box + point) · t02.3 grasp a1: VLA "succeeded"
       post FAIL "gripper is empty (expected SALSA-MILD)"  → reground (inner) → t02.3.r1.1 ground ✓ → grasp a2 ✓
LLM  ← PickResult{picked: true, instance: MINI-F1-1-02#01, note: "verified in the gripper"}
LLM  → place_in_basket {item_id: i1}
loop   t03.1 place ✓ (gripper empty ∧ basket +1)
exec   v0.2 side effects now: catalog.record_pick, item.picked, add_line (once per item), basket.verified
LLM  ← PlaceResult{basket_ok: true, picked_qty: 1}
```

## Plan mode: what the LLM planner writes

The real planner (agent arm, claude-sonnet-5-5, strict structured output via `utils.llm`) returns a `plan.PlanDraft`
whose `steps` are `DraftStep{kind, item_id?, slot_id?, target?, question?, note}`. `kind` is one of `fetch_item`,
`scan_shelf`, `ask_user`, `navigate_to`, `checkout` or `verify_basket`. That is 5 optional fields and 4 nullable
unions, inside the strict-tool budget. There is no `add_item`: only `handle_message` ("also grab limes") may add goal
items. `PlannerService.compile` (deterministic) expands each draft step with `plan.DRAFT_MACROS`, resolves poses and
lift heights from the planogram, attaches `default_conditions`, splits units, and drops draft steps that name unknown
items (listed in `StepPlan.planner.fixes`). If the LLM fails or returns an invalid draft, the scripted draft is used
(`planner.origin = "scripted"`).
