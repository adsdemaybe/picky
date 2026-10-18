# recommender

**Purpose.** Owns shopper profiles and produces explainable extra-item suggestions: detour stops for the route,
"want any of these?" at checkout, and near-expiry markdowns. Post-purchase feedback updates the profile, so trip 2
ranks differently. Also owns **Cart Whisperer** (T-Mobile track): ≤3 explained cards per phase (planning / in-trip /
checkout), SKIP and WAIT included, zero is valid; grocery stores and the T-Mobile-style wireless store; designed, not
implemented (see below).
**Pipeline:** `sourcing.split` → **`recommender.recommend`** → `recs.ready` → routing (optional stops, skip penalty =
`interest`) … `followup` reply → `agent.interpret_reply` → **`recommender.record_feedback`** → `profile.updated`.

**Owner:** TBD · **Branch:** `arm/recommender` · **Extra:** `uv sync --extra recommender`

## Scope (CLAUDE.md §3)

- [x] **P0** Profile store: prefs (weights), dislikes, budget, purchase history, ratings (fixtures via `utils.data`)
- [x] **P0** Every rec carries `reason` + `signals` + `sponsored=False` + `trip_id`; never recommends list/cart/declined SKUs
- [x] **P0** Deterministic heuristic ranking (tag/category overlap − dislikes + deal / near-expiry boosts) — the fake
- [x] **P0** Post-purchase feedback updates the profile (`record_feedback` → `ProfileUpdate` with changes + summary)
- [x] **P0** Cap rec stops per trip (`limit`, gateway passes `PICKY_REC_MAX_STOPS`); rest batched at checkout (max 3)
- [x] **P1** Near-expiry markdowns only to users likely to want them (deal-friendly prefs or product affinity)
- [x] **P1** Product embeddings (`BAAI/bge-small-en-v1.5`, 384-d) → profile taste vector → cosine blend (numpy scan)
- [x] **P1** LLM (Sonnet) rerank of the shortlist + "why you'd like this" (falls back to heuristic reasons)
- [ ] **P1** Persist profiles / feedback / purchases / taste vectors in Postgres (`service.py` TODO 1, 3)
- [ ] **P2** pgvector `product_embeddings` + HNSW `<=>` query instead of the numpy scan (TODO 2)
- [ ] **P2** Voyage `voyage-4-lite` embeddings upgrade (TODO 4)

## Cart Whisperer (T-Mobile track)

Design: [`docs/cart-whisperer.md`](../../docs/cart-whisperer.md). **Designed, not implemented:** it needs the
proposed contracts v0.3 (`ShopperContext`, `Suggestion`, `RecommenderService.suggest` / `respond`,
`suggestions.updated`), which lands after the v0.2 build merges. It replaces the checkout batch above
(`checkout_batch` stays until then). From the shopper's full context (commerce, customer and real-time physical
signals) it shows ≤3 explained cards per phase: planning (list ready), in-trip (cart changes, item picked, robot at a
shelf) and "before you pay". SKIP (owned, incompatible, duplicate) and WAIT (promo soon, over budget, upgrade too
early) are card kinds, and zero cards is a valid answer. Ranking is deterministic with reason codes; the LLM only
phrases the "why". It runs in the grocery stores and the T-Mobile-style wireless store (`store_tmobile`). The web app
already shows the planning-phase SKIP / WAIT cards and the zero case on demo data (`arm/frontend`, not wired).

- [ ] **P0** `suggest(context, limit=3)`: 8 candidate generators (complete, replenish, swap/save, qty fix, deal match, remove/warn, skip, wait)
- [ ] **P0** Hard gates in code: stock, compatibility (`compatible_with`, ×∞), already owned, allergens / dislikes / guest dietary, mandate snapshot + budget headroom (or `needs_approval`), not declined, conversation ("no cilantro"), max detour, no pending-rec duplicates; joint-set cap check; a gated item the shopper asked for or picked becomes a SKIP card
- [ ] **P0** Deterministic score (need + taste + money − budget_pressure − detour − fatigue; SKIP / WAIT = avoided spend × certainty), stable ids, ≤3 per phase incl. SKIP / WAIT; allergen warnings first
- [ ] **P0** Score threshold (`PICKY_SUGGEST_MIN_SCORE`, proposal 0.35): zero suggestions is a normal result ("Nothing worth adding today")
- [ ] **P0** Deterministic `reason_codes` on every suggestion (`ALREADY_OWNED`, `INCOMPATIBLE`, `PROMO_SOON`, `INSTALLMENTS_LEFT`, `OVER_BUDGET`, ...)
- [ ] **P0** Fake: templated reasons citing reason codes + signals; deterministic; base deps only; reproduces the demo cards incl. skip, wait and the zero case (doc §11)
- [ ] **P0** `respond(user_id, suggestion, decision)`: suppress declined (3 trips / 30 days), down-weight the kind, `ProfileUpdate` summary; WAIT accept → reminder via messaging
- [ ] **P1** `complements.py`: dish / setup templates (tacos, new phone, ...) + pair rules (beef → seasoning, chips → salsa, party ≥ 6 → plates / napkins) + co-purchase pairs
- [ ] **P1** Cadence model: per-SKU interval from `PurchaseRecord.ts`, subcategory defaults × `household_size` (labeled "default" in signals)
- [ ] **P1** Real: Sonnet phrases the "why" from reason codes + signals (no rerank), validated against them; template fallback on any failure
- [ ] **P1** Wireless signals: owned items, device age, installments left, household lines, `compatible_with`, promo calendar (once v0.3 + data land)
- [ ] **P1** User-aware "Skip it" price verdict, shared with the web app's Find & compare
- [ ] **P1** Metrics: per-trip `SuggestionMetrics` (accepted rate, $ saved, avoided spend, $ added, basket completeness, zero-suggestion rate); per-store aggregates for `MerchantDashboard.kpis`
- [ ] **P2** Persist suppressions / outcomes in Postgres with the profile store

## Contract

- **Implements** `contracts.services.RecommenderService` (`get_profile`, `find_user_by_handle`, `save_profile`,
  `recommend`, `checkout_batch`, `near_expiry_offers`, `record_feedback`, `record_purchase`).
- **In:** `UserProfile`, `ShoppingList`, `Cart`, `focus`, `exclude_skus`, `Feedback` (with `sku`), `PurchaseRecord`.
- **Out:** `Recommendation` (`kind` = `detour` | `near_expiry_offer` | `checkout_batch`; `price_cents` = unit price
  with deal applied; stable `rec_id` = hash(user, trip, sku, kind) so the transparency log can upsert), `ProfileUpdate`.
- **Events:** none published here. The gateway publishes `recs.ready` and `profile.updated` from our return values.
- **Calls (at call time only):** `services.catalog.get_planogram` (live stock/slots), `get_deals`, `get_near_expiry`,
  `get_product`. Without a wired catalog it reads `data/planograms/*` directly. `utils.llm` for the rerank.
- `find_user_by_handle` normalizes both sides with `contracts.messaging.normalize_handle`.

## Stack

| | |
|---|---|
| Pick | `sentence-transformers` 6.1.0 + `BAAI/bge-small-en-v1.5` (local, MPS/CPU) → pgvector 0.5.1 `vector(384)`; Sonnet rerank via `utils.llm.complete_json` |
| Fallback | numpy cosine scan (what `service.py` does now); no extra → deterministic heuristic only |
| Gotchas | Anthropic has no embedding model. `EMBED_DIM` is fixed in contracts (384): never mix models in one column. HNSW index is only used when the operator matches the op class (`vector_cosine_ops` ↔ `<=>`). First model load downloads ~130 MB: warm it before the demo. |

## Files

- `scoring.py` — pure scoring, reason strings, feedback → profile changes (shared by fake + real)
- `fake.py` — `FakeRecommenderService` (in-memory, deterministic, base deps); `create_fake`
- `service.py` — `RecommenderService(FakeRecommenderService)`: embeddings blend + LLM rerank; `create`
- `tests/` — `world.py` (tiny in-memory taco store), `test_fake.py`, `test_service.py`, `test_demo_data.py`

## How to run

```bash
uv run pytest arms/recommender                         # base deps; data/ test skips until data/ exists
make demo                                              # fake (default)
uv sync --extra recommender && PICKY_REAL_ARMS=recommender make demo   # real (needs ANTHROPIC_API_KEY for the rerank)
```

## How to contribute

Full flow: CLAUDE.md §5. For this arm:
1. `git fetch && git switch arm/recommender && git merge origin/main` (after merge: `feat/recommender-<slug>` /
   `fix/recommender-<slug>` off `main`). Never push to `main`.
2. Change only `arms/recommender/**`. Need a contract change? Separate `contracts/<slug>` PR. Shared helper? `utils/<slug>`.
3. Keep `fake.py` deterministic and base-deps-only; heavy imports stay inside functions (extra `recommender`).
4. Before pushing: `uv run ruff check --fix arms/recommender && uv run ruff format arms/recommender && make check`,
   then `PICKY_REAL_ARMS=recommender make demo`. Edited this README? `make rules`.
5. Commits: `recommender: <imperative summary>`. Open/update the PR with the template and tick the checklists.

## Demo-ready checklist (CLAUDE.md §6)

- [ ] No `NotImplementedError` on demo paths (only the Postgres `_pg_*` helpers raise, and nothing calls them yet)
- [ ] `PICKY_REAL_ARMS=recommender make demo`: maya's taco trip gets 3 recs incl. a near-expiry guac kit with "40% off, expires tomorrow"
- [ ] "how was the salsa?" → "too mild" → `profile.updated` summary "You like it hotter…" → trip 2 top rec changes
- [ ] Missing extra / no API key / bad LLM JSON / catalog error → heuristic recs, warning logged (never kills the trip)
- [ ] Switch back with `PICKY_REAL_ARMS` minus `recommender`
- [ ] `sponsored=False` everywhere; every rec has reason + signals (trust rubric)
- [ ] Embedding model warmed on the demo machine

## Known limitations

- Profiles live in memory (reset on restart); Postgres persistence is TODO.
- Heuristic needs the data's `tags`/`subcategory`/`spice_level` to be filled for good reasons.
- Real service subclasses the fake; LLM rerank can reorder but never changes prices, filters or `sponsored`.
- Recs for `store_id="online"` return `[]` (detours only make sense in a physical store).
- Cart Whisperer SKIP / WAIT signals for the wireless store need v0.3 fields and `data/planograms/store_tmobile.yaml`
  (not in `data/` yet; today they exist only in the web app's `tmobile.ts`).
- Detour mix (`rank(..., mix=True)`): one slot each for the best taste pick, the best near-expiry *markdown* (a
  discount is required for `near_expiry_offer`) and the best deal, then fill by score. SKUs bought on the last trip
  (`record_purchase`) get `-0.75` and lose reserved slots to something new. `SPICE_GAIN` is 3.0 so "too mild"
  visibly reorders trip 2. Spice feedback ("too mild") only moves spice prefs, not every tag of the product.
