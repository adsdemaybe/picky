# arms/trust

**Purpose.** The deterministic money gate: AP2-style mandates, checkout approval bound to the cart hash, Stripe
test-mode payment behind a `VisaIntelligentCommerceAdapter`, and the transparency + audit ledger. The LLM never decides
payment. Pipeline: `trips.add_line → trust.evaluate` (each add) · `trips.start_checkout → trust.evaluate → checkout
approval (cart_hash) → trust.checkout → checkout.completed` (CLAUDE.md §2).

Owner: TBD · Branch: `arm/trust`

## Scope (CLAUDE.md §3)
- [x] **P0** Mandate engine in code (not the LLM): cap → DENY `over_cap`, allowed/blocked categories, "ask me above $X"
  per line → NEEDS_APPROVAL, expiry/revocation, merchants/online, per-item cap, trip budget → NEEDS_APPROVAL `over_budget`
- [x] **P0** Explicit approval before checkout: kind=checkout, `approve`, unexpired, not replayed, hash-bound
- [x] **P0** Signed `CartMandate` (`compute_cart_hash`), signed mandates/receipts, hash-chained signed audit log
- [x] **P0** Checkout: mock receipt (fake) / Stripe test `PaymentIntent` (`pm_card_visa`, int cents) behind the VIC adapter
- [x] **P0** Recommendation transparency log (upsert on `rec_id`, `sponsored` visible)
- [ ] **P1** Persist ledger to Postgres (`contracts/db/schema.sql`); today it is in memory
- [ ] **P1** Visa sandbox provider (`visa_sandbox`) if access appears; Stripe stays the fallback
- [ ] **P1** Back the web app's Permissions page (approved memory + mandate + audit view) via `GET/PUT /api/users/{id}/profile`, `.../mandate`, `GET /api/trust/ledger`; pending-memory approval (`remember(fact)`) is P2 / v0.3
- [ ] **P1** Cart Whisperer (v0.3): accepts, and declined SKIP / WAIT ("buy anyway"), are evaluated like any cart change; transparency entries for suggestions (`docs/cart-whisperer.md` §13)
- [ ] **P1** Pickup mode ("robot shops, I pick up"): every off-list item asks; checkout never auto-approves. The web app's separate tech mandate (`im_maya_tech_001`) is a v0.3 contracts question (per-category caps or several mandates per user)
- [ ] **P2** Ed25519 (PyNaCl) signatures + RFC 9421 agent request signing (TAP-style identity)
- [ ] **P2** Stripe Shared Payment Tokens with `usage_limits` from the IntentMandate; passkey step-up

## Contract
- **Implements** `contracts.services.TrustService`: `get_mandate`, `set_mandate`, `evaluate`, `checkout`,
  `record_transparency`, `ledger`.
- **In:** `IntentMandate`, `Cart` + proposed `CartLine` + `budget_cents` + `trip_id`, `CheckoutRequest`
  (`approval_request` + `approval`), `TransparencyEntry`.
- **Out:** `MandateDecision` (verdict, `reasons`, per-line `violations`, `remaining_cents`, `cart_hash`), `Receipt`
  (`mock` | `stripe_test`; signed; `online_order_id` when online lines), `TrustLedger`, `CartMandate`
  (`mandates.build_cart_mandate`).
- **Events:** none published (the gateway publishes `mandate.decision`, `checkout.completed`).
- **Calls:** nothing (leaf). Data: `utils.data.load_demo_fixtures()` for demo mandates (lazy, at call time).

| Verdict | Reasons |
|---|---|
| DENY | `mandate_expired`, `mandate_revoked`, `over_cap`, `over_item_cap`, `category_blocked`, `category_not_allowed`, `merchant_not_allowed`, `online_not_allowed`, and at checkout `approval_missing/rejected/expired/replayed`, `cart_hash_mismatch` |
| NEEDS_APPROVAL | `above_ask_threshold` (line scope only, strictly above), `over_budget`, `no_mandate` |
| APPROVE | `ok` (at checkout, soft reasons stay listed but the hash-bound approval satisfies them) |

Checkout never raises on refusal: it returns a `Receipt` with `status="declined"`/`"error"` and the reason messages.

## Stack
Pick: pure Python + Pydantic, HMAC-SHA256 via `utils.signing` (`PICKY_SIGNING_KEY`); **Stripe test mode** on the
critical path (`stripe>=10`, extra `trust`). Fallback: mock signed receipt (automatic when Stripe is missing or the key
is empty). Gotchas (docs/research.md §9.8): integer cents only; `pm_card_visa` server-side, never a PAN; Stripe min
charge 50 cents; live keys (`sk_live_`) are refused; VIC has no self-serve sandbox or Python SDK, so the adapter is
labelled "VIC-ready, sandbox access pending"; never claim certification.

## Files
| File | What |
|---|---|
| `mandates.py` | Pure engine: `evaluate_mandate`, `check_checkout`, `approval_violations`, `build_cart_mandate`, `sign_model`/`verify_model`, `AuditChain` |
| `payments.py` | `MockPaymentAdapter`, `VisaIntelligentCommerceAdapter` (Stripe test PaymentIntent; `enroll_card`, `request_agent_token`, `create_payment_instruction`, `send_commerce_signal`) |
| `core.py` | `TrustCore`: the shared TrustService (in-memory ledger, idempotency, replay guard) |
| `fake.py` | `FakeTrustService` (mock payments, `demo_mandate` fallback: $60 cap, ask above $10) |
| `service.py` | `TrustService` (VIC/Stripe adapter, degrades to mock) |
| `tests/` | engine + service tests on the taco cart |

## How to run
```bash
uv run pytest arms/trust                                  # tests (base deps)
make demo                                                 # fake trust in the full pipeline
uv sync --extra trust && STRIPE_SECRET_KEY=sk_test_... PICKY_REAL_ARMS=trust make demo   # real Stripe test mode
```

## Demo-ready checklist (CLAUDE.md §6)
- [x] Full `TrustService` Protocol, no `NotImplementedError` on demo paths
- [ ] `PICKY_REAL_ARMS=trust make demo` completes maya's taco trip with a `stripe_test` receipt (pi_...)
- [x] Failures degrade: missing Stripe → mock; card decline / Stripe error / hash mismatch → declined receipt, no crash
- [x] Fake by env flag alone (drop `trust` from `PICKY_REAL_ARMS`)
- [x] Unit tests: over cap, blocked/not-allowed category, above ask, expired/revoked, approval replay, cart changed
  after approval, audit tamper detection
- [ ] Ledger screen shows mandate, decisions, transparency (no sponsored), receipts, audit chain
- [x] No secrets committed; env vars already in `.env.example` (`STRIPE_SECRET_KEY`, `VISA_*`, `PICKY_SIGNING_KEY`)

## Judges' demo (contracts 0.2.4)

- `agent.py`: the Visa payment agent. Wraps `TrustCore.checkout` (mandate re-check, adapter, signed receipt, audit
  chain) in a `payment.agent_step` timeline: cart mandate → intent check → passkey requested / verified → mock Visa
  Intelligent Commerce agentic token (no PAN, amount-capped, 10 min) → authorized → captured → receipt signed →
  ledger. The LLM only narrates (grounded; `PICKY_PAYMENT_AGENT_LLM`). `PICKY_PAYMENT_AGENT=false` = plain checkout.
- Limitation: the VIC token / auth are simulated (Visa sandbox access pending); Stripe test mode runs the charge when
  `STRIPE_SECRET_KEY` is set.

## Known limitations
- Ledger, used approvals and idempotency keys live in memory (lost on restart).
- Category matching is exact (case-insensitive) on `CartLine.category`; no taxonomy.
- Fake falls back to `demo_mandate` when a user has no fixture mandate; the real service returns `None` → `no_mandate`
  → every add asks.
- The VIC methods other than `create_payment_instruction` are local stubs.

## Contributing to this arm
Follow CLAUDE.md §5: work on `arm/trust` (later `feat/trust-<slug>` / `fix/trust-<slug>`), touch only `arms/trust/`,
commit as `trust: <imperative summary>`. Keep `mandates.py` pure (no I/O, no LLM, int cents) and add a test for every
new reason code. Before pushing: `uv run ruff check --fix arms/trust && uv run ruff format arms/trust && uv run pytest
arms/trust`, then `make check` and `PICKY_REAL_ARMS=trust make demo`. Need a contract change (new reason, field)?
Open a separate `contracts/<slug>` PR; never edit `contracts/` from this arm.
