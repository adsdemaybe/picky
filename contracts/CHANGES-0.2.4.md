# contracts 0.2.4: the judges' demo (checkout unload by the arm, photo verification, payment agent, purchase DB)

**Additive patch on 0.2.3.** New optional fields, one new atom, two new step options, two new sim commands, three new
events. Nothing existing changes meaning. `versions_compatible` compares major.minor, so deployed 0.2.x Isaac
workers keep connecting: they do not advertise `basket_unload`, the robot arm never sends them the new commands, and
the runner skips the per-unit unload there (the handoff unloads the basket at once, as before).

**Affected arms:** agent (planner compile + LoopRunner), robot (fake + SimRobot + T0 VLA), sim (worker actions),
perception (truth / verifiers: counter units, basket grounding), recommender (purchase DB + suggestions), trust
(payment agent), gateway (REST + events). Fakes updated in the same change; `make demo` shows the unload.

## 0.2.3 recap (already on this branch)

`cameras`: `<robot>.director` and `<robot>.front|wrist.perceived` livestream cameras · `fleet.RequestItem` +
`FleetRequest.items` (structured orders) · `profile.ReplenishSuggestion` (suggestion cards).

## Checkout unload by the arm (`plan.py`, `planogram.py`, `sim.py`, `vla.py`, `verify.py`, `evidence.py`)

- `GraspStep.source: 'shelf' | 'basket'` (default 'shelf'): 'basket' = take a unit out of the robot's own basket.
- `PlaceStep.destination` += `'counter'`, `PlaceStep.counter: CounterSpot | None`.
- New atom `ItemOnCounter(sku, min_count)` (`op='item_on_counter'`, sources camera + state, default camera front).
- `default_conditions(..., source=, destination=, counter_count=)`: grasp(basket) = (gripper free) -> (holding sku,
  basket -1 sku); place(counter) = (holding sku) -> (released, >= n x sku on the counter).
- `UNLOAD_UNIT_MACRO = (ground_target, grasp, place, verify)`; the checkout draft step compiles, per unit, a group
  `unload:<n>` ("Unloading 3/9: Mild Chunky Salsa → counter ✓") between the checkout drive and the handoff.
- `planogram.CounterSpot` + `counter_spots(planogram, count)`: deterministic spots on the checkout fixture's top
  (bays of 3, left -> right; virtual counter when a store has no checkout fixture).
- Sim wire: `PickFromBasketCmd` (`pick_from_basket`), `PlaceOnCounterCmd` (`place_on_counter`), both MOTION commands,
  capability `BASKET_UNLOAD_CAPABILITY = 'basket_unload'`; `ItemInstance.on_counter`.
- `VlaCapabilities.basket_unload`; `TruthSnapshot.counter` (+ `counter_counts()`); `StateFacts.counter_counts`.
- `policy.EffectKind` += `'unload_to_counter'` (place(counter) commits only that; nothing touches stock or the cart).

## Photo verification + payment agent (`trust.py`)

- `ApprovalKind.VERIFY_ITEM`; `ItemVerification` (photo of the exact unit, status pending / accepted / rejected /
  auto_accepted / cancelled, `auto_accept_s`, `attempt`).
- `PaymentAgentStep` (one step of the Visa payment agent's timeline; `simulated=True`, `PAYMENT_SANDBOX_LABEL`).

## Purchase history + suggestions (`profile.py`)

- `PurchaseRecord.name`, `.photo_urls`, `.source ('checkout' | 'seed')` (rows of the purchase-history database).
- `ReplenishSuggestion.kind`: `replenish | substitute | complement | deal`; cadence fields are optional (never-bought
  candidates), plus `score`, `ranked_by ('rules' | 'llm')`, `substitute_for`, `related_to`, `deal_text`; `signals`
  values may be bool.

## Events (`events.py`)

- `checkout.unload` (agent): one unit of the unload (started / placed / verified / skipped / done) with `n`, `total`,
  `text`, `counter_spot`, `photo_url`.
- `item.verification` (agent): a per-item photo verification card, created and resolved.
- `payment.agent_step` (gateway): the Visa payment agent's timeline.
