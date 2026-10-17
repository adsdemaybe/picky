# agent

**Purpose.** The LLM brain. Turns "tacos for 6, under $40, one vegetarian" into a `ShoppingList`, splits sourcing
(in store → robot, out of stock or online-only → online order), chats with tools, classifies replies (YES/NO, picks,
ratings) and writes the post-purchase follow-up.
**Pipeline position:** PLAN (`expand_intent` → `source_list`), SHOP (`respond`, `interpret_reply` for approvals),
AFTER (`compose_followup` → `interpret_reply` for feedback). The gateway drives every call.

## Scope (CLAUDE.md §3)

Ticked P0s pass `PICKY_REAL_ARMS=agent make demo` with the real impl in rules mode (no `ANTHROPIC_API_KEY`); the
Claude path is untested until a key is set.

- [x] **P0** Intent expansion → items + qty + substitutes + dietary + budget (`expand_intent`; rules today, Claude structured output in `service.py`)
- [x] **P0** Split sourcing: in-store vs online, one combined checkout (`source_list`, deterministic)
- [ ] **P0** LLM brain with tools `query_inventory`, `plan_route`, `recommend`, `add_to_cart`, `request_approval`, `checkout` (`respond`)
- [x] **P0** Reply interpretation for approvals and follow-up ratings (`interpret_reply`)
- [x] **P0** Follow-up "how was the salsa?" with `about_skus` (`compose_followup`)
- [x] **P0** Trip 2 is smarter: SKU guesses ranked by profile prefs, favorites and dislikes
- [ ] **P1** Streaming `agent.message` deltas (TODO 1 in `service.py`)
- [ ] **P1** Voice in store: Deepgram STT/TTS, push-to-talk, Haiku turns (TODO 2)
- [ ] **P1** Strict tool schemas, warmed at startup (TODO 3)
- [ ] **P1** Follow-up phrasing personalized by Haiku (TODO 4)
- [ ] **P2** Batched shoppers: several intents → one list with `for_user_id` (TODO 5)

## Contract

- **Implements** `contracts.services.AgentService`: `expand_intent`, `source_list`, `respond`, `interpret_reply`,
  `compose_followup` (see `contracts/OVERVIEW.md`, agent row).
- **In:** `Intent`, `UserProfile`, read-only `Trip`, `InboundMessage` + pending `ApprovalRequest`s.
- **Out:** `ShoppingList` (`expand_intent`: items with a SKU guess, `sourcing='unresolved'`; `source_list`: sku,
  sourcing, store_id, est_line_cents, notes such as `OOS at store_ave -> online`), `AgentReply` (+ `ToolCallRecord`s),
  `ReplyInterpretation` (approval_id / choice / choices / rating / sku / feedback_tags), `OutboundMessage`
  (`about_skus`, channel `photon`).
- **Events:** publishes `agent.message` itself (the shopper's text as `role=user`, then the reply as `role=assistant`)
  and `error` (code `llm_fallback` / `llm_error`) when the LLM path degrades. The gateway publishes everything else.
- **Calls at call time only:** `catalog.search/get_offers/get_product`, `recommender.recommend/checkout_batch/
  near_expiry_offers`, `trips.get_trip/add_line/replan/start_checkout`, `approvals.request`, `events.publish`.
  Never `trust.checkout`: money moves only through `trips.start_checkout` (mandate engine + approval bound to the
  cart hash).

## Stack

- **Pick:** `claude-sonnet-5-5` (`PICKY_MODEL_BRAIN`) for chat and intent expansion; `claude-haiku-5-5`
  (`PICKY_MODEL_FAST`) for voice turns and reply classification. The tool loop is hand-written
  (`utils.llm.run_tool_loop`) and stops after 8 iterations. Prompt caching covers the system prompt and the tools; the
  trip context goes in the user turn so the cached prefix stays stable. Intent expansion uses structured output
  (`utils.llm.complete_json` → `IntentDraft` → `ShoppingList` in code).
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
| `__init__.py` | `create(services)` → `ClaudeAgent`, `create_fake(services)` → `FakeAgent` |
| `logic.py` | deterministic core: intent regex + recipes, personalized SKU ranking, split sourcing, reply regex, follow-up |
| `fake.py` | `FakeAgent`: rules + canned replies (budget, where-is, recs, route, checkout) |
| `service.py` | `ClaudeAgent` + `ToolExecutor` (explicit handler for each of the six tools) + numbered TODOs |
| `tests/` | `stubs.py` (in-memory catalog/trips/events), `test_agent.py` (conformance + demo scenario + scripted LLM) |

## How to run

```bash
uv run pytest arms/agent                       # tests (base deps, no network)
make demo                                      # all fakes
PICKY_REAL_ARMS=agent make demo                # real agent (needs ANTHROPIC_API_KEY; otherwise it degrades to rules)
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
