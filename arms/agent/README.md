# agent

**Purpose.** Natural-language understanding + the shopper chat agent (`AgentService`) **and** the per-robot managing
LLM, the "robot brain" (`RobotBrainService`). The agent turns "tacos for 6, under $40, one vegetarian" (or "Hazel, go
charge", "scan dairy at Hill") into a `RequestDraft`, expands lists, splits sourcing, chats with tools, classifies
replies and writes the follow-up. The brain takes one fleet `Goal` and executes it on one rover with the eleven brain
tools (locate → route → navigate → look → scan → pick → place → ask → add item → progress → finish), emitting a
`brain.step` for every thought / tool call / result / observation / message so the web app shows what each robot thinks.
**Pipeline position:** REQUEST (`interpret_request`), PLAN (`expand_intent` → `source_list`), IN-STORE EXECUTION (brain
`run_goal` for shop / fetch / shelf_scan / return_to_dock goals, called by the fleet), SHOP chat (`respond`,
`interpret_reply`), AFTER (`compose_followup`), fleet view chat with one robot (`handle_message`).

## Scope (CLAUDE.md §3)

`[x]` = works in `make demo` / `PICKY_REAL_ARMS=agent make demo` (no `ANTHROPIC_API_KEY`: the real classes degrade to
the same rules / scripted executor). Claude paths are unit-tested with a mocked client only.

- [x] **P0** `interpret_request`: NL → `RequestDraft` (shop list = `expand_intent`'s; shelf_scan categories / aisles;
  return_to_dock; staff-only fetch; status / product questions answered from the catalog; vague → clarifying question;
  `history` for answers; robot / store hints from robots.yaml + planograms). Real: Claude structured output, code keeps
  only known robots / stores / categories and parses the budget by regex (mocked-client tests)
- [x] **P0** Intent expansion → items + qty + substitutes + dietary + budget (`expand_intent`)
- [x] **P0** Split sourcing: in-store vs online, one combined checkout (`source_list`, deterministic)
- [x] **P0** Robot brain, scripted (fake): shop goals reproduce the old per-stop loop (route order, detour ask first →
  declined re-plans, navigate → look → scan, empty shelf → online line, mandate check on the FULL line → deny / ask
  over_threshold, produce freshness top-3 pick_choice, pick + place per unit with one retry, ONE cart line per item,
  basket check on this robot's units, checkout → unload → finish); fetch, shelf_scan (scanned_slot_ids, resumable),
  return_to_dock
- [x] **P0** Brain failure handling: retryable robot errors → re-plan + retry once; item failed but goal goes on;
  fatal (`battery_low`, `unknown_robot`, robot in ERROR) → `run_goal` raises without failing the goal (fleet
  reassigns); cancellation withdraws the pending question (`approvals.cancel`); basket full mid-goal → drop-off at the
  checkout and come back
- [x] **P0** `brain.step` + `robot.arrived` / `scan.result` / `pick.choices` / `item.picked` / `basket.verified` with
  `goal_envelope` (shop events carry the trip_id); per-robot ring of 1000 steps (`recent_steps`), `status`
- [x] **P0** `handle_message`: "also grab limes" → `add_item` (goal + trip list + re-plan), "skip the cilantro" → item
  skipped, "status?" / idle → status, "what do you see?" → front camera `observe`
- [x] **P1** Real brain: Claude tool loop over `brain_tool_definitions(strict=True)`, ≤ 25 turns, checkpoints every 6
  turns with a front-camera summary (video input) + the `look` tool, user messages injected between turns; any LLM
  failure (no key, API error, refusal, max_tokens, malformed output, turn cap) → error event + `brain.step(kind=error)`
  and the scripted executor finishes from the current goal state (mocked-client tests; untested against the API)
- [ ] **P0** LLM shopper chat with tools `query_inventory`, `plan_route`, `recommend`, `add_to_cart`, `request_approval`, `checkout` (`respond`; code exists, needs a key)
- [x] **P0** Reply interpretation for approvals and follow-up ratings (`interpret_reply`)
- [x] **P0** Follow-up "how was the salsa?" with `about_skus` (`compose_followup`)
- [x] **P0** Trip 2 is smarter: SKU guesses ranked by profile prefs, favorites and dislikes
- [ ] **P1** Streaming `agent.message` deltas (TODO 1 in `service.py`)
- [ ] **P1** Voice in store: Deepgram STT/TTS, push-to-talk, Haiku turns (TODO 2)
- [ ] **P1** Follow-up phrasing personalized by Haiku (TODO 4)
- [ ] **P2** Batched shoppers: several intents → one list with `for_user_id` (TODO 5)

## Contract

- **Implements** `contracts.services.AgentService` (`interpret_request`, `expand_intent`, `source_list`, `respond`,
  `interpret_reply`, `compose_followup`) and `contracts.services.RobotBrainService` (`run_goal`, `handle_message`,
  `status`, `recent_steps`); factories `create` / `create_fake` / `create_brain` / `create_brain_fake`.
- **Brain tools** follow the executor mapping in `contracts/tools.py` (`brain_exec.py`, one method per tool). Results:
  `LocateItemsResult`, `RouteSummary`, `RobotState`, `VisionObservation`, `ScanShelfResult`, `PickResult`,
  `PlaceResult`, `AskUserResult`, `GoalItem`, `Goal`; failures `BrainToolError` (`retryable` from
  `BRAIN_RETRYABLE_CODES`; extra codes `invalid_input`, `not_found`, `rejected`, `nothing_to_choose`, `nothing_held`,
  `blur_failed`, `mandate_denied`, `internal`).
- **Events:** `agent.message`, `brain.step`, `robot.arrived`, `scan.result`, `pick.choices`, `item.picked`,
  `basket.verified`, `error` (`llm_fallback`, `llm_error`, `internal`, robot codes).
- **Calls at call time only:** catalog (search / get_locations / get_offers / get_product / get_planogram /
  apply_scan / record_pick), routing (plan_route, non-trip goals), robot (state / navigate / scan / capture / pick /
  place_in_basket / unload_basket), perception (blur_faces / identify / rank_freshness / verify_basket), cameras
  (observe), trips (get_trip / replan / set_stop_status / check_line / ask / add_line / update_list / start_checkout),
  approvals (request for goals without a trip, pending / cancel), fleet (update_goal / get_goal only), events.
  Never `trust.checkout`.

## Stack

- **Pick:** `claude-sonnet-5-5` (`PICKY_MODEL_BRAIN`) for chat and intent expansion; `claude-haiku-5-5`
  (`PICKY_MODEL_FAST`) for voice turns and reply classification. The tool loop is hand-written
  (`utils.llm.run_tool_loop`) and stops after 8 iterations. Prompt caching covers the system prompt and the tools; the
  trip context goes in the user turn so the cached prefix stays stable. Intent expansion uses structured output
  (`utils.llm.complete_json` → `IntentDraft` → `ShoppingList` in code); `interpret_request` → `RequestDraftOut` →
  `RequestDraft` in code.
- **Robot brain:** one hand-written loop per goal (not `run_tool_loop`: it must stop on `finish_goal`, let fatal
  `RobotError`s and cancellation through, inject user messages and checkpoints): `claude-sonnet-5-5`, cached system
  prompt (SO-101 rover + rules) and tools (`brain_tool_definitions(strict=True)`: 6 strict tools, within the strict
  limits), tools run sequentially (one robot), ≤ 25 turns. Vision = `CameraService.observe` (`claude-haiku-5-5` in the
  perception arm) via the `look` tool and the checkpoint summaries.
- **Fallback:** the rule-based path in `logic.py` / `fake.py`, used automatically when `ANTHROPIC_API_KEY` is missing
  or the LLM fails. You can also swap models by env var. Voice falls back to the browser Web Speech API.
- **Gotchas (docs/research.md §9.6):** use `anthropic` 1.15; `output_config`, not `output_format`; strict tools allow no
  numeric bounds (`tool_definitions(strict=True)` moves them into descriptions); changing `output_config.format`
  invalidates the cache; don't send `thinking.budget_tokens`; spend-cap 429s have no retry-after.
- **Trust:** the budget is parsed by regex and the model's number is ignored when the text states one. Sourcing is
  deterministic code. The model cannot pay.

## Files

| File | What |
|---|---|
| `__init__.py` | `create` → `ClaudeAgent`, `create_fake` → `FakeAgent`, `create_brain` → `ClaudeBrain`, `create_brain_fake` → `ScriptedBrain` |
| `interpret.py` | deterministic `interpret_request` rules + robot / store / category / aisle hints from `data/` |
| `brain_exec.py` | `BrainExecutor`: the eleven brain tools against services, steps + events, local goal updates without a fleet |
| `brain_script.py` | `ScriptedRunner`: the deterministic tool sequence per goal kind (fake brain + LLM fallback) |
| `brain.py` | `ScriptedBrain` / `ClaudeBrain`: run_goal wrapper, steps ring, status, handle_message, Claude tool loop |
| `logic.py` | deterministic core: intent regex + recipes, personalized SKU ranking, split sourcing, reply regex, follow-up |
| `fake.py` | `FakeAgent`: rules + canned replies (budget, where-is, recs, route, checkout) |
| `service.py` | `ClaudeAgent` + `ToolExecutor` (explicit handler for each of the six tools) + numbered TODOs |
| `tests/` | `stubs.py` / `brain_stubs.py` (in-memory store world), `test_agent.py`, `test_interpret.py`, `test_brain.py` (scripted goals, failures, cancellation, messages, mocked Claude loop) |

## How to run

```bash
uv run pytest arms/agent                       # tests (base deps, no network)
make demo                                      # all fakes
PICKY_REAL_ARMS=agent make demo                # real agent + real brain (needs ANTHROPIC_API_KEY; else rules / scripted)
PICKY_REAL_ARMS=brain make demo                # real robot brain only (Claude tool loop), fake shopper agent
```

## Demo-ready checklist (CLAUDE.md §6)

- [ ] `PICKY_REAL_ARMS=agent make demo` completes the taco trip: list.ready → sourcing.split shows one item online
- [ ] Every `AgentService` method on the demo path runs without `NotImplementedError` (true today; keep it that way)
- [ ] No API key, a timeout or malformed JSON → rules fallback plus an `error` event, and the trip continues
- [ ] Photon "YES"/"NO"/"2"/"4/5" replies resolve the right approval or feedback SKU
- [ ] Trip 2, after "salsa was too mild", ranks the spicier salsa first (needs the recommender to update `prefs`)
- [ ] `add_to_cart` above the ask threshold → `request_approval` → retry with `approval_id`; checkout only via approval
- [ ] Switching back to the fake by env flag alone works; README is current

## Known limitations

- Robot brain (real) has never run against the API (no key this weekend); it is tested with a mocked client. Each
  goal costs up to 25 Sonnet turns plus a Haiku `observe` per checkpoint; the scripted executor takes over on any
  failure, so the demo never depends on it.
- Mid-goal drop-off at the checkout when the basket is full is executor-internal (no tool, not in the contract): the
  capacity comes from `RobotSpec.basket_capacity` (robots.yaml, default 12) because `RobotState` has no capacity field.
- An empty shelf on a shop goal adds the online line right away; if the mandate wants an approval for it the brain asks
  (`online_order`) instead of deferring it to checkout like the old orchestrator did.
- `pick_choice` is skipped (scripted) when the scan shows no freshness difference; an LLM asking anyway gets
  `nothing_to_choose`.
- `handle_message` uses keywords ("add/also grab X", "skip X", "status", "what do you see"); with the real brain the
  message is queued for the running LLM goal instead.
- Shelf-scan routes for `end_at='stay'` are solved to the checkout pose (RouteRequest needs an end); the robot just
  doesn't drive the last leg.
- Recipes: only tacos is built in. Other requests become a comma-split item list (LLM path: any dish).
- SKU guessing ranks by: not disliked → the recipe line's category ("limes" = produce, not chile-lime chips) → most
  query words matched ("ground beef" beats "Ground Coffee") → favorite → prefs. Substitutes stay in that category.
- Feedback parsing: "too mild" / "too spicy" without an explicit number counts as 2/5 for the item the follow-up
  asked about, even when the reply praises something else ("too mild, loved the hot sauce").
- `respond` publishes one final `agent.message`, with no streaming deltas yet. Conversation history is in memory,
  per trip, for the last 6 turns.
- Tests use an in-test stub catalog. `data/` YAML wasn't written yet when this was scaffolded, so there is no
  data-backed test yet.
- `ReplyInterpretation.decision` is `approve`/`reject` only. For a pick, the agent returns `approve` + `choice`, and
  the gateway maps that to `ApprovalResponse.decision='choice'`.

**Owner:** TBD · **Branch:** `arm/agent`
