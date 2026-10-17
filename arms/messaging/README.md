# messaging

**Purpose.** Lets the shopper reach picky by text (Photon 2.0 on iMessage): send the list before the trip, answer
YES/NO and numbered approvals during the trip, and reply to the follow-up afterwards, which updates their taste profile.
**Pipeline position:** it is the first step (text in → `intent`), runs alongside `approval.requested` mid-trip, and is
the last step (`agent.compose_followup` → `messaging.send` → reply → `agent.interpret_reply` → `recommender.record_feedback`).

## Scope (CLAUDE.md §3)
- [ ] **P0** Text your list/intent before the trip ("tacos for 6, under $40, one vegetarian" → `InboundMessage` → gateway → intent)
- [x] **P0** Mid-trip approval requests by text (YES/NO; numbered options for pick_choice / checkout batch) (fake)
- [x] **P0** Checkout approval by text (the broker binds it to `cart_hash`; the LLM never decides payment) (fake)
- [x] **P0** Post-purchase follow-up ("how was the salsa?") → reply → profile update → trip 2 is smarter (fake)
- [ ] **P0** Live on at least 1 Photon channel (the beta shared iMessage line), which the Photon track requires
- [x] **P0** Deterministic fake (console channel + scripted shopper) so the pipeline runs without Photon
- [ ] **P1** Mid-trip suggestions pushed proactively ("they have a markdown guacamole kit, want it?")
- [ ] **P1** Persist the dedupe set, poll cursor and handle→space map (Postgres) across restarts
- [ ] **P1** Account linking (`link ABC123`) for handles that are not in the fixtures
- [ ] **P2** spectrum-ts sidecar (native polls, tapbacks, persistent connection) · Telegram as a second channel

## Contract
- **Implements** `contracts.services.MessagingService`: `start`, `stop`, `send(OutboundMessage) -> SendResult`,
  `send_approval_request(ApprovalRequest) -> SendResult`, `on_inbound(handler)`.
- **In:** `OutboundMessage` (`about_skus` set on follow-ups), `ApprovalRequest`. **Out:** `SendResult`,
  `InboundMessage` (`channel='photon'`, `handle` normalized to E.164, `user_id` resolved) passed to the handler. The
  gateway fills `trip_id`.
- **Calls (at call time only):** `services.recommender.find_user_by_handle(normalize_handle(h))` and
  `get_profile(user_id)` (for the `to_handle`). If the recommender is not wired, it falls back to `data/fixtures/users.yaml` via `utils.data`.
  The fake also calls `services.approvals.resolve` (only when no inbound handler is set) and `services.events.publish`.
- **Events:** the fake publishes `agent.message` (`source='messaging'`, `channel='photon'`, role assistant/user) so
  the UI can show the phone thread. Note that `EVENT_PRODUCERS` lists `agent.message` as agent-only, so turn this off
  with `FakeMessaging(emit_events=False)` if the gateway already mirrors these. The real service publishes `error`
  (`code='photon_send_failed'`) when a send fails.
- Contracts: [`contracts/messaging.py`](../../contracts/messaging.py), [`contracts/services.py`](../../contracts/services.py),
  [`contracts/OVERVIEW.md`](../../contracts/OVERVIEW.md).

## Stack
- **Pick:** Python `photonhq-api==0.5.0` (`import photon_api`, `AsyncPhoton`), extra `messaging`. Every ~2 s the service
  polls `projects.list_messages` (`direction=received`, `createdSince` cursor) and dedupes on the Photon message id.
  It sends with `projects.send_message`, passing an `Idempotency-Key` header (the approval_id, or else the message id),
  which also lets the SDK retry the send safely.
- **Fallback:** the spectrum-ts sidecar, then Telegram, then web chat (the same agent endpoints).
  The console fake is always available.
- **Gotchas:**
  - **The shared line cannot text first**, so every demo phone must text the number "ping" before it can receive anything.
  - We learn `handle → spaceId` and our own `senderId` (`receivedBy.id`) from the shopper's first text. Until then,
    `send()` returns `ok=False, error='no_space_user_must_text_first'`.
  - The line supports DMs only (no groups or calls). Delivery is at-least-once, so we dedupe. Sends are accepted asynchronously and delivery is not confirmed.
  - Quotas: 5,000 msgs/day, 50 new conversations/day per line, and about 5 req/s (unverified). Polling at 2 s is fine.
  - Never depend on polls or tapbacks: text YES/NO always works. Other content types (reactions, typing, attachments) are ignored.
  - Never log full phone numbers: `format.mask_handle` turns `+15555550100` into `***0100`.
  - The beta docs are partly stubs. If something breaks, ask in the Photon Discord.

### Photon setup (hour 0)
1. Sign up for the beta at [photon.co/beta](https://photon.co/beta), then go to [app.beta.photon.codes](https://app.beta.photon.codes) and create an org and a project.
   Copy the **Project ID** (`pho_prj_...`) and a **project API key** (`pho_sk_...`). The key is shown only once.
2. On the iMessage page, add your phone or Apple ID, then click **Assign** to get a number from the shared pool.
3. **Text the number "ping" from every demo phone** (judges' phones too).
4. Put the credentials in `.env` as `PHOTON_API_KEY=pho_sk_...` and `PHOTON_PROJECT_ID=pho_prj_...`.
5. Make sure the demo users' `phone` values in `data/fixtures/users.yaml` match the real demo phones in E.164 form. Edit them locally only and never commit real numbers.

## Files
| File | What |
|---|---|
| `__init__.py` | `create(services)` → `PhotonMessaging`, `create_fake(services)` → `FakeMessaging` |
| `service.py` | Real Photon implementation: polling, dedupe, handle↔space map, idempotent send, `MessagingNotConfigured` |
| `fake.py` | Console channel with a scripted shopper (`scripted_reply`), `inject()` for simulated texts, and a `transcript` |
| `format.py` | Approval text rendering, YES/NO and number parsing, `mask_handle` (base deps only) |
| `directory.py` | `HandleDirectory`: maps handle ↔ user_id through the recommender, falling back to fixtures |
| `tests/` | Fake conformance and demo-scenario behavior, plus Photon parsing, dedupe and send (no network) |

**The fake's scripted policy** (the decision is made by `scripted_reply`, and the text is delivered after `reply_delay_s=0.3`):
- A recommendation the gateway flagged `payload['over_budget']` gets `NO` (she sticks to her budget). Otherwise it gets `YES` when `payload['interest'] >= 0.6`, or when `interest` is missing, and `NO` below that.
- over_threshold gets `NO` when `'over_budget'` is in `payload['reasons']` (above the ask-me limit alone is `YES`).
- Checkout, substitution and online_order get `YES`.
- pick_choice and multi-select get `1`.
- A follow-up (an outbound message with `about_skus`) gets one reply per trip: the demo-script line
  `"too mild, loved the hot sauce though"` the first time, then `"perfect heat this time, 5/5"`.
- How the reply is delivered depends on `reply_mode`. In `auto` mode, the reply goes to the inbound handler if one is set, and otherwise to `services.approvals.resolve`. The other modes are `inbound`, `resolve` and `off`.

## How to run
**Run it real** (prerequisites, else the gateway boots the fake and `/health` says why): `uv sync --extra messaging`,
`PHOTON_API_KEY` + `PHOTON_PROJECT_ID` in `.env`, and **text the Photon line "ping" from the demo phone first** (the
shared line can't text first; until then sends fail with `no_space_user_must_text_first` and the trip waits in
`followup`). `make setup` removes extras, so re-run `uv sync --extra messaging` after it.

```bash
uv run pytest arms/messaging                               # tests (base deps only)
make demo                                                  # full trip, messaging = console fake
uv sync --extra messaging                                  # real: photonhq-api 0.5.0
PICKY_REAL_ARMS=messaging make demo                        # needs PHOTON_API_KEY + PHOTON_PROJECT_ID and a phone that texted first
uv run --with photonhq-api==0.5.0 pytest arms/messaging    # also runs the SDK-shape send test
```

## Demo-ready checklist (CLAUDE.md §6)
- [ ] Every Protocol method works on the real path. Verify the shapes against the live beta: a "ping" comes in and a reply goes out.
- [ ] `PICKY_REAL_ARMS=messaging make demo`: Maya texts the intent, approves 3 detours and the checkout by text, and the follow-up reply updates her profile.
- [ ] Predictable failures degrade instead of killing the trip: Photon down or a 429 → backoff and keep polling; a send failure → `error` event and `SendResult(ok=False)`; an unknown sender → `user_id=None`.
- [ ] Removing `messaging` from `PICKY_REAL_ARMS` switches back to the fake.
- [ ] Tests pass and CI is green. Nothing secret is committed and no real phone numbers are in the repo.
- [ ] A backup video exists of the iMessage thread during a full trip.

## Known limitations
- The cursor, dedupe set and handle→space map live only in memory, so a restart forgets who texted first. Re-text "ping" to recover.
- The real service does not correlate replies with approvals. It forwards every text, and `agent.interpret_reply` matches it against `approvals.pending()`.
- `senderId` is learned from inbound `receivedBy.id` (or the other participant in the direct space). This is unverified against the live beta.
- Only text content is handled: no images, polls or tapbacks.

## Contributing to this arm
The full flow is in CLAUDE.md §5. The parts specific to this arm:
1. Work only in `arms/messaging/` on branch `arm/messaging`. After it merges, use `feat/messaging-<slug>` or `fix/messaging-<slug>`. Commit messages look like `messaging: <imperative>`.
2. Keep `fake.py` on base deps only. Import `photon_api` **inside functions only**. Never import another arm: reach the
   recommender, approvals and events only through `services.<field>` at call time.
3. If a contract doesn't fit (for example, approval payload keys), open a `contracts/<slug>` PR instead of working around it here.
4. Before you push, run `uv run ruff check --fix arms/messaging && uv run ruff format arms/messaging && uv run pytest arms/messaging`, then `make check` and `PICKY_REAL_ARMS=messaging make demo`.
5. After you edit this README, run `make rules`.

**Owner:** TBD · **Branch:** `arm/messaging`
