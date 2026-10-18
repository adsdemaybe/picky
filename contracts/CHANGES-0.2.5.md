# contracts 0.2.5: Cart Whisperer (T-Mobile track: checkout suggestions that actually help, incl. SKIP / WAIT)

**Additive patch on 0.2.4.** One new module, one new event, one optional `Trip` field, two new
`RecommenderService` methods, one widened literal. Nothing existing changes meaning. Nothing touches the sim wire, and
`versions_compatible` compares major.minor, so the deployed 0.2.x Isaac workers (0.2.0 .. 0.2.4) keep connecting.
docs/cart-whisperer.md §7 proposed a 0.3.0 bump; it ships as a patch on purpose (contracts/README.md "Exception
(0.2.1)": a minor bump makes the gateway refuse the running worker).

**Affected arms:** recommender (owner: `whisper` / `whisper_feedback`; the fake ships a zero-card placeholder in
this PR, the engine follows in `feat/recommender-cart-whisperer`), gateway (REST + `whisper.cards` +
`Trip.whisper`; `feat/gateway-cart-whisperer`), frontend (TS types via `make gen-types`), trust (reads
`TransparencyEntry.kind = "whisper"`; no code change needed), messaging (WAIT reminders go through the existing
`MessagingService.send`; no contract change).

## `contracts/whisper.py` (new)

- `WhisperKind` = complete | replenish | swap_save | qty | deal | remove | skip | wait; `WhisperAction` = add | swap |
  set_qty | remove | skip | remind (`WHISPER_DEFAULT_ACTION`); `WhisperPhase` = planning | in_trip | checkout.
- `WhisperReasonCode` (deterministic, set by code): ALREADY_OWNED, BOUGHT_RECENTLY, DUPLICATE, INCOMPATIBLE,
  PROMO_SOON, PRICE_ABOVE_TYPICAL, INSTALLMENTS_LEFT, OVER_BUDGET, COMPLETES_OCCASION, QTY_MISMATCH,
  CHEAPER_SAME_NEED, RUNNING_LOW, DEAL_MATCH, ALLERGEN, SHIPPING_WASTE, ON_ROUTE.
- `WhisperSignal` `{name, value, source: commerce | customer | physical, label}`.
- `WhisperCard` `{card_id, kind, action, phase, user_id, store_id, trip_id, sku, line_sku, line_id, name, title,
  qty, price_cents, price_delta_cents, savings_cents, avoided_cents, reason, reason_codes (>= 1), signals, score,
  remind_on (required for remind), promo_price_cents, needs_approval, detour_m, aisle_label, image_url, phrased_by,
  sponsored=false}`; stable ids `whisper_card_id(kind, sku, line_sku)`.
- `WhisperResponse` `{user_id, store_id, phase, trip_id, cards (max 3), empty_reason, considered, hidden_declined,
  threshold, generated_at}`: zero cards is a valid answer.
- `WhisperDecisionRequest` (optional POST body), `WhisperDecisionResult` `{card_id, decision, outcome, message, card,
  cart, mandate_decision, approval, reminder, profile_update}`, `WhisperOutcome`, `WhisperReminder`.
- `ShopperContext`: what the gateway knows (trip, list, cart, budget, mandate, remaining_cents, robot pose, remaining
  stops, last pick, pending detour recs, conversation, answered cards). Server-side only.
- `whisper_transparency_entry(card, outcome)`: the trust-ledger row (`kind="whisper"`).

## Elsewhere

- `RecommenderService.whisper(context, limit=3) -> WhisperResponse` and
  `whisper_feedback(user_id, card, decision, trip_id=None) -> ProfileUpdate | None`.
- `events.WhisperCardsEvent` (`whisper.cards`, producer gateway): `phase`, `cards`, `empty_reason`.
- `Trip.whisper: WhisperResponse | None` (the latest pass; rides in `trip.snapshot`).
- `shopping.RecKind` += `"whisper"` (transparency entries for cards).
- `api.md`: `GET /api/users/{user_id}/whisper`, `POST /api/users/{user_id}/whisper/{card_id}/accept|decline`, the
  event.

Not in this patch (design §7 leftovers): `Product.compatible_with`, a catalog promo calendar / price history, profile
fields for devices / owned items, per-trip `SuggestionMetrics`, merchant KPIs. Until the catalog owns them, the
recommender keeps its own synthetic promo calendar + wireless account signals.
