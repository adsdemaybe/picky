# Cart Whisperer: checkout suggestions that actually help (T-Mobile track)

> **Update (contracts 0.2.5):** the contract shipped as an additive **patch** (`contracts/whisper.py`,
> `contracts/CHANGES-0.2.5.md`), not the 0.3.0 proposed in §7, so the running 0.2.x Isaac worker keeps connecting.
> Names differ from §7: `WhisperCard` (kinds `complete | replenish | swap_save | qty | deal | remove | skip | wait`,
> actions `add | swap | set_qty | remove | skip | remind`), `WhisperResponse`, `ShopperContext`,
> `RecommenderService.whisper` / `whisper_feedback`, event `whisper.cards`, REST `GET /api/users/{user_id}/whisper`
> + `POST /api/users/{user_id}/whisper/{card_id}/accept|decline` (`contracts/api.md`, "Cart Whisperer"). The rest of
> this doc (pipeline, gates, scoring, trust rules) is unchanged.

> **Status: designed, not implemented in the backend.** Nothing in this doc runs in the gateway or recommender yet. It
> needs a contracts change that lands **after** the v0.2 build merges (proposed v0.3, §7). Implementation follows in
> the arms listed in §13. Owner: **recommender** arm. Trust gates it. Gateway, web app and messaging show it. The robot
> brain can trigger it.
>
> The web app (teammate, `arm/frontend` @ `6fc1c41`, draft PR #12; demo data, not wired to the gateway) **already
> shows** the planning-phase SKIP / WAIT cards ("Picky's advice": **Skip it** / **Worth waiting**) and the zero case
> ("Nothing worth a stop today") for both stores (`src/lib/order.ts`, `src/lib/prices.ts`,
> `src/data/stores/tmobile.ts`). Cart Whisperer runs in the grocery stores
> and in the T-Mobile-style wireless store (`store_tmobile`), which is a web-app fixture until
> `data/planograms/store_tmobile.yaml` lands after v0.2.
>
> Team source: [`docs/team-plan.md`](team-plan.md) §3.2 (T-Mobile) and §6.2 (recommender "says wait"). That file lives
> on `arm/frontend` until PR #12 merges. Read it with `git show origin/arm/frontend:docs/team-plan.md`.

## 1. Goal

**Track (DubHacks 2026, T-Mobile):** "Cart Whisperer: Checkout suggestions that actually help". The source is the
T-Mobile challenge brief (`UW_Hackathon_Challenge_Brief.docx`, cited in `docs/team-plan.md` §3.2 and §12.3). The brief
is about **phone shopping**. Our direction is **Cart Whisperer**, plus the brief's "bring the one that says wait".

**T-Mobile requirements (from the team plan's checklist):**

- [ ] Name one direction clearly: Cart Whisperer.
- [ ] Show a phone-shopping store in the demo (the T-Mobile-style wireless store, `store_tmobile`).
- [ ] Show at least one SKIP or WAIT moment live.
- [ ] Say "synthetic data only" on a slide.

**Team framing:** "take the entire context of what the person who's buying stuff has and then actually suggest", and be
**willing to tell you "don't buy that."**

A suggestion "actually helps" only if it does at least one of these things:

- completes what the shopper said they are making or setting up,
- saves money for the same need,
- prevents a mistake (wrong quantity, duplicate, allergen, over budget),
- matches their taste at a price they would want,
- or **stops a purchase that doesn't help**, with **SKIP** (you already own it, it doesn't fit) or **WAIT** (a known
  sale is coming, it puts you over budget, the upgrade is too early).

A suggestion that only adds more stuff does not count. **At most 3 cards per phase, SKIP and WAIT included. Zero is
valid:** below the score threshold nothing is shown, and the UI says "Nothing worth adding today". Every card says why
it is there, and each one can be accepted or declined with one tap.

**When it runs:**

- **Planning,** once the list is ready (`list.ready`), before the robot leaves. These are the web app's "Picky's
  advice" cards on the Robot order page (shipped on demo data).
- **Live,** every time the cart changes during a trip (debounced), and on physical triggers: `item.picked` ("what you
  just picked up") and `robot.arrived` at a shelf. Physical constraint: a suggestion that needs a pick is offered only
  while its slot is still ahead on the route, or with its detour cost shown.
- **"Before you pay":** a final pass when the robot starts its last leg to checkout, shown next to the checkout
  approval.

Cart Whisperer replaces the old checkout batch ("want any of these?", `RecommenderService.checkout_batch`).

### 1.1 T-Mobile rubric mapping

⚠️ **Rubric per the team's copy of the brief (`docs/team-plan.md` §3.2), unverified.** Re-read the docx before slides.

| Criterion (weight) | Where we earn it |
|---|---|
| **Customer value (30%)** | ≤3 cards per phase, and zero is a valid answer. Explicit SKIP and WAIT cards. Every card shows $ saved or $ avoided (§10). |
| **Use of signals (25%)** | Three signal families (§3). Commerce: cart, catalog, deals, promo calendar, price history. Customer: profile, history + cadence, budget + mandate, owned items, device age, installments left, household lines. Physical: where the robot is, the slot ahead, what it just picked up, detour cost. |
| **Explainability & trust (20%)** | Deterministic reason codes on every card; the LLM only phrases them (§5). "Why am I seeing this?" lists the signals. Nothing is sponsored. The mandate is never bypassed (§9). |
| **Feasibility & demo (25%)** | Deterministic fake, so `make demo` shows the same cards with no key. The web app already shows the planning cards on demo data. The robot runs in sim (Isaac on vhagar or the 2D fake). |

## 2. Where it sits in the pipeline

```
 list.ready (planning) ────────────────────┐
 cart.updated (add / pick / remove / qty) ─┤
 item.picked / robot.arrived at a shelf ───┼─▶ gateway builds ShopperContext ─▶ recommender.suggest(ctx) ─▶ 0–3 Suggestions
 route.planned / robot passes a stop ──────┤                                                     │
 robot heads to checkout ──────────────────┘                                                     │
     suggestions.updated (may be empty) ─▶ web app order page / trip card / checkout panel, Photon, robot brain (ask_user) ◀┘
 accept  ─▶ gateway: add_line / remove_line / set qty / swap ─▶ trust.evaluate (mandate + budget) ─▶ approval if needed
          ─▶ mid-trip add = optional stop ─▶ replan ─▶ fleet.update_goal (robot picks it)
 skip accept ─▶ remove the line if it's in the cart, else record "acknowledged"
 wait accept ─▶ don't add / remove the line ─▶ messaging schedules a reminder ("I'll text you Fri Oct 23")
 decline ─▶ recommender.respond ─▶ suppress + down-weight ─▶ profile.updated ─▶ trip 2 is different
```

## 3. ShopperContext: everything we know, assembled on the server

The gateway builds a `ShopperContext` (proposed model, §7) from state that already exists in v0.2, plus the v0.3 rows
marked **new**. It never goes to the browser. Only derived `signals` reach the UI. Inputs are grouped by the brief's
three signal families.

| Family | Input | What we read (real field names) | Owned by / where it comes from | Used for |
|---|---|---|---|---|
| Commerce | **Cart now** | `Cart.lines[]` (`sku`, `qty`, `unit_price_cents`, `source` in_store/online, `store_id`, `status`, `list_item_id`, `rec_id`), `shipping_cents`, `total_cents`, `online_cents` | gateway (`Trip.cart`, `TripController.add_line` / `remove_line`) | everything; shipping waste; duplicates |
| Commerce | **Store now** | `StoreOffer.price_cents` / `list_price_cents` / `qty` / `deal` / `lots`, `Deal.kind` / `reason` / `valid_until`, `NearExpiryItem.days_left`, `ProductLocation.slot_id` / `approach_pose`, `Product.tags` / `allergens` / `size` / `unit` / `shelf_life_days` / `category` | catalog (`search`, `get_planogram`, `get_deals`, `get_near_expiry`, `get_product`) | in-stock gate, prices, deals, unit price, where it is |
| Commerce | **Promo calendar + price history** (new) | Upcoming promos per SKU (`BUDS-RUN` $59.99 from Fri Oct 23; `PX9-128` $599.99 expected Nov 27; `COFFEE` $7.99 from Fri Oct 23) and a 13-week price history | Today: web-app demo data (`offers.json` from `scripts/gen-data.mjs`, `tmobileUpcoming` in `tmobile.ts`). Planned: catalog (v0.3), shared with Find & compare | WAIT; the good-price verdict |
| Commerce | **Compatibility** (new) | `compatible_with` on accessories (`CASE-PX9-CLR` → `PX9-128`; `CASE-PX7-BLK` → `PX7`) | Today: `tmobile.ts`. Planned: a catalog `Product` field (v0.3) | hard gate; SKIP card when the shopper asks for or picks an incompatible item |
| Customer | **Profile** | `UserProfile.prefs` (weights in [-1, 1]), `dislikes`, `disliked_skus`, `favorite_skus`, `dietary`, `allergies`, `household_size`, `default_budget_cents`, `taste_vector` (384-d) | recommender (`get_profile`); seed `data/fixtures/users.yaml` | taste score, allergen/dislike gates, cadence scaling |
| Customer | **Purchase history + cadence** | `PurchaseRecord.sku`, `qty`, `unit_price_cents`, `ts`, `rating` | recommender (`record_purchase`); seed `users.yaml` `purchases` | replenish ("probably out"), SKIP ("just bought it"), past ratings |
| Customer | **Feedback** | `Feedback.rating`, `tags` (e.g. `too_mild`), `text` | recommender (`record_feedback`) | taste deltas, cite "you said it was too mild" |
| Customer | **Intent** | `Trip.intent.text`, `ShoppingList.title`, `servings`, `budget_cents`, `dietary_notes`, `ListItem.query` / `qty` / `required` / `dietary` / `notes` / `sku`; `RequestDraft.summary` | agent (`interpret_request` / `expand_intent`), stored on the `Trip` by the gateway | complete (missing parts of the occasion), qty fix (servings), keep guests' dietary needs on swaps |
| Customer | **Money** | `IntentMandate.cap_cents`, `per_item_cap_cents`, `ask_above_cents`, `allowed_categories`, `blocked_categories`, `allow_online`; `MandateDecision.remaining_cents`; `ShoppingList.budget_cents` | trust (`get_mandate`, `evaluate`) | hard gates, budget_pressure, "back under budget" swaps, WAIT when over budget, `needs_approval` flag |
| Customer | **Devices, owned items, lines** (new) | Owned SKUs with purchase date (`CHG-20W` bought 2026-03-14); phone model + `age_months` (Pixel 7, 34); `installments_left` (4) × `monthly_installment_cents` (1650) = $66.00; `household_lines` (2) | Today: `tmobile.ts` `mayaDevices`. Planned: `users.yaml` + new profile fields (v0.3, data PR) | SKIP owned / incompatible; WAIT early upgrade |
| Customer | **Conversation** | the shopper's chat lines this trip (`POST /api/trips/{id}/messages`), robot chat (`BrainStep` with `role="user"`), `ApprovalResponse.note` | gateway (chat), agent (brain `recent_steps`) | "no cilantro" means never suggest cilantro; tone of the "why" |
| Customer | **Past suggestions** | earlier `Suggestion`s with outcome declined / ignored / accepted; `TransparencyEntry.outcome` for detour recs | recommender (new suppression store); trust ledger | suppress declined, down-weight that kind, never re-ask in the same trip |
| Physical | **Robot + route** | `Trip.route.stops[]` still pending (`Trip.stop_status`), `Stop.pose` / `slot_id`, `RobotState.pose`, `Trip.robot_id`, the slot the robot is at, `last_picked_sku` (new) | routing (`Route`), robot (`RobotService.state`), gateway (from `robot.arrived` / `item.picked`) | detour cost (m / s), "offer only while the slot is ahead", "what you just picked up" |

Not in the context: card data (never), raw camera frames, other shoppers' data. The demo users are fictional, and all
store, retailer, device and price data is synthetic.

## 4. Suggestion kinds

Grocery examples use real SKUs and prices from `data/` (store_ave unless noted). "Maya" is `data/fixtures/users.yaml`
maya (budget $40, cap $60, ask above $15, blocks alcohol; likes spicy 0.8 / vegetarian 0.6 / mexican 0.5; favorites
`TORT-CORN`, `BEANS-BLACK`, ...). Wireless examples use the web app's `store_tmobile` fixture (`tmobile.ts`,
`order.ts`; fictional) and are re-checked when that store moves into `data/`.

| Kind | Action | Fires when | Examples |
|---|---|---|---|
| **complete** | add | The stated occasion is missing a part: a dish or setup template (tacos → tortillas, protein, salsa, ...; new phone → case, glass), a pair rule (ground beef → seasoning, chips → salsa), or "party for N ≥ 6" → plates or napkins | "Tacos for 6 but no tortillas → `TORT-CORN` Corn Tortillas 30 ct, **$2.79**, you rated them 5★." · "2 lb `BEEF-GROUND`, no seasoning → `TACO-SEASONING` Taco Seasoning Mix 1 oz, **$1.49**, two slots from the tortillas (`AVE-G2-E1-02`), +0 m." · Wireless: "Pixel 9 case but no screen protector → `GLASS-PX9` 2-pk, **$23.99** (20% off with a Pixel 9 case), same wall." |
| **replenish** | add | Cadence says you are probably out: days since the last purchase ≥ the typical interval (your own history, or a category default scaled by `household_size`, labeled as a default) | "`OAT-MILK` Oat Milk 64 oz: you bought it Oct 8 (9 days ago); a household of 2 usually finishes that in about a week (category default). **$4.79**, back wall." |
| **swap/save** | swap | Same need, cheaper: lower unit price, a deal, or a cheaper same-diet / same-compatibility alternative; also "this swap gets you back under budget". Never swaps a SKU the shopper asked for by name or a favorite (except for an allergen) | "Swap 1 lb `BEEF-GROUND` ($5.49) for `SOYRIZO` Soy Chorizo 12 oz ($4.49): it also covers your vegetarian guest, and you rated it 4★ on Oct 8. **Save $1.00**, same fridge." · `CHEESE-COTIJA` $4.99 → `CHEESE-MEX` $3.79, both 8 oz, both vegetarian: **save $1.20** (only when the line came from a generic "cheese"). · Wireless: `CASE-PX9-RUG` $49.99 → `CASE-PX9-CLR` $29.99, both fit the Pixel 9: **save $20.00** (only when the line came from a generic "case"). |
| **qty fix** | set_qty | Quantity doesn't match servings and pack size (`ShoppingList.servings` × per-person rule vs `Product.size` count) | "2 packs of Corn Tortillas 30 ct = 60 tortillas for 6 people at 3 each (18). **1 pack is enough: −$2.79.**" (Real: the agent's recipe assumes 12-count packs.) · "1 guac kit (serves 4) for 6 people → make it 2." |
| **deal match** | add | A deal or near-expiry markdown that fits taste and intent, within headroom. Never duplicates a pending detour rec | "`CHIPS-TORTILLA` Restaurant Style Tortilla Chips, **$2.99** (25% off, 'Taco night' endcap). Goes with your salsa; you rated Chile Lime chips 5★." · `GUAC-KIT` **$3.99** (was $5.99, best by Oct 18): "you're eating tonight." |
| **remove/warn** | remove (or set_qty) | Allergen conflict, shipping waste, or over budget (what to drop). Owned, duplicate and just-bought items are now **skip** | Sam (peanut allergy) adds `TRAIL-MIX`: "**Contains peanuts**, and your profile lists a peanut allergy. Remove?" · "Cilantro is out here, so it ships: $1.29 + $2.99 shipping. **Drop it: −$4.28.**" · Wireless: "The power bank puts you $18.96 over your $90. Drop it?" |
| **skip** (new) | `none`, or `remove` if the item is in the cart | The item (on the list, in the cart, asked for, just picked, or a template candidate) is already owned or was bought within its cadence, is incompatible with the device it's for, or is a duplicate | Wireless (shipped): "**Skip the charger:** you bought a 20W USB-C charger in March and it works with the Pixel 9" (avoids $24.99). · Wireless (designed, in-trip): "That's the $9.99 clearance Pixel 7 case; it doesn't fit the Pixel 9. The Clear Case in your basket does." · Grocery: "**Skip re-buying Classic Red:** bought Sep 26, you go through one about every 5 weeks." |
| **wait** (new) | `remind` | A cheaper promo is announced soon, the item puts you over budget and a sale is coming, or a phone upgrade while `installments_left > 0` | Wireless (shipped): "SportFit earbuds $79.99 → **$59.99 from Fri Oct 23**. Picky texts you Friday" (save $20.00). · Wireless (shipped): "Your Pixel 7 has 4 installments left ($66.00); a $200-off Pixel 9 promo is expected Nov 27. I'll remind you." · Grocery (shipped): "Coffee: weekly ad $2 off Fri Oct 23 ($7.99). Buy 1 now, not 2." |

Allergen warnings always rank first, ignore the score threshold, and don't count toward the "declined, so suppress"
rule. When a SKIP and a detour rec touch the same category in one trip (Classic Red skip vs the Ghost Pepper detour),
the SKIP is phrased as "skip re-buying X", so the detour reads as "try something new".

## 5. Pipeline: candidates → gates → score + reason codes → LLM phrasing → 0–3

All deterministic except the wording in step 4. Money math is integer cents. Code decides what is shown and in what
order. The LLM only writes the sentence.

1. **Candidates (deterministic).** One generator per kind, reading only the `ShopperContext`:
   - complete: dish/setup templates and pair rules in `arms/recommender/complements.py` (new), matched on
     `Product.tags` and `subcategory`, plus co-purchase pairs from purchase history;
   - replenish: cadence from `PurchaseRecord.ts` per SKU, else a subcategory default × `household_size`;
   - swap/save: same subcategory, same `ListItem.dietary` and same `compatible_with`, compared on unit price and deals;
   - qty fix: servings × per-person rule vs the pack count parsed from `Product.size`;
   - deal match: `get_deals` + `get_near_expiry` × taste score (the existing `scoring.py`);
   - remove/warn: allergens ∩ `allergies`, online shipping > online subtotal, and the over-budget line with the least
     taste loss;
   - **skip:** owned SKUs or same-subcategory items bought within cadence or `shelf_life_days`, duplicate SKUs or
     subcategories, and `compatible_with` mismatches against the device on the list or owned;
   - **wait:** an upcoming promo cheaper than today's price before the item is needed, over budget with a sale soon,
     or a phone while `installments_left > 0`.
2. **Hard gates (code, never the LLM).** An add or swap candidate is dropped unless:
   - it is in stock (`StoreOffer.qty ≥ qty`, or an online offer when the source is online);
   - it is **compatible** with the device it's for (`incompatible × ∞`);
   - it is not already owned or bought within cadence (that turns it into a SKIP candidate instead);
   - there is no allergen conflict with any shopper on the trip, and no disliked SKU, tag or category (pref ≤ −0.5);
   - swaps keep the `ListItem.dietary` of the line they replace;
   - the mandate allows it (category not blocked, allowed if a list is set, online allowed, line ≤ `per_item_cap_cents`,
     projected total ≤ `cap_cents`);
   - it fits the budget headroom, OR it is paired with a saving, OR it carries `needs_approval=true` (OVER_BUDGET /
     ABOVE_ASK_THRESHOLD shown on the card);
   - it was not declined before (same user + kind + sku + replaces_sku) and was not already shown this trip;
   - nothing the shopper said contradicts it ("no cilantro");
   - its detour is ≤ `PICKY_SUGGEST_MAX_DETOUR_M` (default 15 m);
   - it is not a SKU already pending as a detour rec on the route.

   An item the shopper **asked for or picked** that fails a gate (incompatible, already owned) becomes a **SKIP card**
   instead of disappearing silently. The shown set is also checked **jointly**, so accepting all 3 never exceeds the
   cap without asking.
3. **Score (deterministic, [0, 1]) + reason codes.** The team formula (team-plan §6.2) mapped onto ours:

   | Team term | Our term | How |
   |---|---|---|
   | `complement` | `need` | how sure we are it's missing / wrong / running out (complete, qty fix, replenish) |
   | `preference_match` | `taste` | prefs + `taste_vector` cosine + past ratings |
   | `promo_value` | `money` | savings, deal value, or "back under budget" |
   | `− budget_pressure` | `− budget_pressure` (new, explicit) | the share of the remaining headroom an add uses; 0 for savings |
   | `− already_owned` | gate for adds, input to SKIP | an owned add never shows; it becomes a SKIP candidate |
   | `− incompatible × ∞` | hard gate | compatibility fails → dropped (or a SKIP card if the shopper asked for it) |
   | (ours) | `− detour`, `− fatigue` | detour_m / max; ×0.5 per decline of this kind, floor 0.1 |

   SKIP / WAIT score = avoided spend × certainty (normalized). Weights live in `arms/recommender/scoring.py`. Ties break
   by SKU, so the output is stable. **Below `PICKY_SUGGEST_MIN_SCORE` (proposal 0.35) nothing is shown, and an empty
   list is a normal result.**

   Every candidate carries deterministic **`reason_codes`**: `ALREADY_OWNED`, `INCOMPATIBLE`, `PROMO_SOON`,
   `INSTALLMENTS_LEFT`, `OVER_BUDGET`, `COMPLETES_OCCASION`, `QTY_MISMATCH`, `CHEAPER_SAME_NEED`, `RUNNING_LOW`,
   `DEAL_MATCH`, `ALLERGEN`, `SHIPPING_WASTE`.
4. **LLM phrasing only (real impl).** The order is already fixed. `claude-sonnet-5-5` via `utils.llm.complete_json`
   gets each shown candidate's kind, action, product, price delta, detour, `reason_codes`, `signals`, the request text
   and the last few shopper lines, and writes one line of "why" per id. It never gets the raw profile or history.
   - **Validation:** every number in a reason must appear in that candidate's signals or prices, and every reason must
     cite at least one of its reason codes / signals.
   - **What the LLM can't do:** reorder, add or drop ids, change sku/qty/price/action, un-gate anything, or set
     `sponsored`.
   - **Fallback:** if a reason fails validation, use the deterministic template. On timeout (~3 s), bad JSON or no key,
     use templated reasons for all.
   - Detour recs (`recommender.recommend`) keep their separate, already-built Sonnet rerank; this rule is for Cart
     Whisperer only.
5. **Top 0–3** go out as `Suggestion`s with action, `price_delta_cents`, `savings_cents`, `avoided_cents`,
   `detour_m` / `detour_s`, `reason_codes`, `signals`, `remind_on`, `needs_approval` and `sponsored=false`. They are
   published as `suggestions.updated` (an empty list included).

**Fake (`create_fake`):** steps 1–3 + 5 with templated reasons, for example "{title}: you have {have} but no
{missing}. {name} is {where} ({detour})." or "Skip {name}: you bought one {when}." It is deterministic, needs no key,
and also produces skip, wait and the zero case, so `make demo` shows the same cards every run.

**Detour cost:** the gateway passes the remaining route stops and the robot pose. The recommender estimates the cheapest
insertion with Manhattan distance between approach poses, which is enough for a 15 m gate. On accept, the gateway
re-plans with routing, and the next `suggestions.updated` carries the routed figure. Separately, orienteering already
gives **zero detours** when no rec beats its walking cost ("Nothing worth a stop today"), with no special case.

**Timing per kind:**

- Planning: skip and wait on list items, plus complete / qty fix on the list, before the robot leaves.
- qty fix, swap, skip and remove on in-store lines are offered before the robot reaches that stop. The robot brain
  checks pending suggestions before `pick_item`.
- `item.picked` of an incompatible or owned item → a SKIP card at that shelf.
- At checkout, removing an already-picked unit means the robot sets it aside at checkout (catalog `adjust_stock`
  puts it back).
- An accepted add at checkout that needs a pick sends the trip CHECKOUT → EXECUTING (an allowed transition) for one
  optional stop.

## 6. Accept / decline semantics

- **Accept** → `POST /api/trips/{id}/suggestions/{suggestion_id}` with `{"decision": "accept"}`. The gateway applies
  it through the normal cart path:
  - add: `TripController.add_line`;
  - remove: `remove_line`;
  - set_qty / swap: projected cart → `trust.evaluate` → apply both line changes atomically;
  - **skip:** remove the line if it's in the cart, else record "acknowledged";
  - **wait:** don't add the line (or remove it), and messaging schedules a reminder for `remind_on` (P1; the demo shows
    the confirmation "I'll text you Fri Oct 23").

  The mandate decides: APPROVE applies it, NEEDS_APPROVAL raises an approval (the "ask me above $X" path, unchanged),
  and DENY never applies. A mid-trip in-store add becomes an optional stop → `replan` → `fleet.update_goal(add_items)`,
  so the robot picks it. If a checkout approval is pending, it is withdrawn (`approvals.cancel`) and re-raised for the
  new cart hash.
- **Decline** → `recommender.respond(..., "decline")`: suppress that (kind, sku, replaces_sku) for this user (3 trips
  or 30 days) and down-weight the kind. Then `profile.updated` with a one-line summary such as "Got it: no meat swaps."
  A declined **skip** ("buy anyway") or **wait** ("buy now") is a normal add through the mandate.
  **Ignored** (shown, never answered, trip ended) counts as a soft decline.
- A **stale** suggestion (the cart changed so its line is gone) answers 409 and triggers a fresh `suggestions.updated`.
- Accepted SKIP / WAIT count as **avoided spend**, kept separate from savings (§10).

## 7. Proposed contracts v0.3 (separate `contracts/cart-whisperer` PR after v0.2 merges)

Bump `CONTRACTS_VERSION` to `0.3.0`. Affected arms: recommender, gateway, trust, agent, messaging (reminders), catalog
(compatibility, promo calendar, price history), frontend (TS types via `make gen-types`). Names are proposals; the
contracts PR decides.

```python
# contracts/shopping.py (or a new contracts/suggest.py)
SuggestionKind = Literal["complete", "replenish", "swap_save", "qty_fix", "deal_match", "remove_warn", "skip", "wait"]
SuggestionAction = Literal["add", "swap", "remove", "set_qty", "remind", "none"]
SuggestionPhase = Literal["planning", "in_trip", "checkout"]
SuggestionOutcome = Literal["shown", "accepted", "declined", "ignored", "expired"]
ReasonCode = Literal["ALREADY_OWNED", "INCOMPATIBLE", "PROMO_SOON", "INSTALLMENTS_LEFT", "OVER_BUDGET",
                     "COMPLETES_OCCASION", "QTY_MISMATCH", "CHEAPER_SAME_NEED", "RUNNING_LOW", "DEAL_MATCH",
                     "ALLERGEN", "SHIPPING_WASTE"]

class OwnedItem(Model):
    sku: Sku; bought_at: date

class Device(Model):
    model_sku: Sku; age_months: int; installments_left: int = 0; monthly_installment_cents: Cents = 0

class UpcomingPromo(Model):
    sku: Sku; starts: date; price_cents: Cents; reason: str

class ShopperContext(Model):          # built by the gateway; never sent to the browser
    user_id: UserId; trip_id: TripId; store_id: StoreId; phase: SuggestionPhase
    profile: UserProfile
    purchases: list[PurchaseRecord]   # recent window (e.g. 90 days)
    feedback: list[Feedback]
    intent_text: str | None           # Trip.intent.text
    shopping_list: ShoppingList | None
    cart: Cart
    mandate: IntentMandate | None
    budget_cents: Cents | None
    remaining_cents: int | None       # MandateDecision.remaining_cents
    owned: list[OwnedItem] = []       # customer signals (wireless)
    devices: list[Device] = []
    household_lines: int | None = None
    upcoming_promos: list[UpcomingPromo] = []   # commerce signal (catalog)
    robot_pose: Pose2D | None         # physical signals
    remaining_stops: list[Stop]       # route stops not done/declined
    last_picked_sku: Sku | None = None
    pending_rec_skus: list[Sku]       # detour recs still on the route (no double-asking)
    conversation: list[str]           # last ~10 shopper lines this trip (chat, robot, approval notes)

class Suggestion(Model):
    suggestion_id: str                # stable: hash(user, trip, kind, sku, replaces_sku)
    trip_id: TripId; user_id: UserId; store_id: StoreId
    kind: SuggestionKind; action: SuggestionAction; phase: SuggestionPhase
    sku: Sku                          # add / swap-in / the line's sku for remove + set_qty / skipped or waited sku
    replaces_sku: Sku | None = None   # swap
    line_id: LineId | None = None     # cart line acted on (swap / remove / set_qty / skip in cart)
    qty: int = 1                      # add/swap: units; set_qty: new qty; remove: 0
    source: LineSource = "in_store"
    title: str                        # "Add Taco Seasoning Mix" / "Skip the charger"
    reason: str = Field(min_length=1) # one line; cites reason codes + signals only
    reason_codes: list[ReasonCode]    # deterministic, set by code
    signals: dict[str, JsonScalar]    # {"servings": 6, "pack_count": 30, "per_person": 3, ...}
    price_delta_cents: int            # signed change to cart total incl. shipping
    savings_cents: Cents = 0
    avoided_cents: Cents = 0          # skip / wait (an estimate for wait, labeled "expected")
    remind_on: date | None = None     # wait
    detour_m: float | None = None; detour_s: float | None = None
    score: Score01
    needs_approval: bool = False      # mandate pre-check: accepting will ask
    sponsored: bool = False
    outcome: SuggestionOutcome = "shown"

class SuggestionDecision(Model):      # POST body
    decision: Literal["accept", "decline"]; channel: Channel = "web"; note: str | None = None

class SuggestionResult(Model):        # POST response
    suggestion: Suggestion; cart: Cart; mandate_decision: MandateDecision | None
    approval: ApprovalRequest | None  # set when the accept needs approval

class SuggestionMetrics(Model):       # per trip and per store
    shown: int; accepted: int; declined: int; ignored: int
    accept_rate: float; saved_cents: int; added_cents: int; avoided_cents: int
    basket_completeness: float; zero_suggestion_passes: int
```

- **`RecommenderService`:** add `async suggest(context: ShopperContext, limit: int = 3) -> list[Suggestion]` (may
  return `[]`) and `async respond(user_id, suggestion: Suggestion, decision: Literal["accept", "decline", "ignore"]) ->
  ProfileUpdate | None`. Keep `checkout_batch` (deprecated; the gateway stops calling it).
- **`Trip`:** add `suggestions: list[Suggestion]` (current) and `suggestion_metrics: SuggestionMetrics | None`. Keep
  `checkout_recs` (deprecated).
- **Catalog:** `Product.compatible_with: list[Sku]`, a promo calendar and a price history (shared with the web app's
  Find & compare; endpoints in the same v0.3 PR or the next one).
- **Profile / data:** owned items, devices and household lines for the demo users (`users.yaml`), plus a
  `store_tmobile` planogram and robot (data PR after v0.2).
- **Event:** `suggestions.updated` `{suggestions, phase, metrics}`; `suggestions` may be empty. Producer: gateway (from
  the recommender's return value, like `recs.ready`), with `trip_envelope(trip)`.
- **REST:** `GET /api/trips/{trip_id}/suggestions` → `list[Suggestion]`; `POST
  /api/trips/{trip_id}/suggestions/{suggestion_id}` with `SuggestionDecision` → `SuggestionResult` (404 unknown, 409
  stale). The merchant metrics need no new endpoint: `MerchantDashboard.kpis` gets `cw_accept_rate`,
  `cw_saved_cents`, `cw_added_cents`, `cw_avoided_cents`, `cw_basket_completeness`.
- **Trust:** `TransparencyEntry` also records suggestions: `kind` gains `"suggestion"`, `rec_id = suggestion_id`, and
  outcome is upserted on answer. Alternatively add `suggestion_kind`; the contracts PR decides.
- **Messaging:** a scheduled reminder for accepted WAITs (`remind_on`); the mechanism (gateway timer vs messaging
  scheduler) is the contracts PR's call.
- **Tools (optional, P2):** a brain tool `check_suggestions` (returns the current suggestions, so the brain can ask one
  through `ask_user`), and the shopper `recommend` tool gains `kind="suggestion"`.
- **Config** (`utils.config` PR + `.env.example`): `PICKY_SUGGEST_MAX=3`, `PICKY_SUGGEST_MIN_SCORE=0.35`,
  `PICKY_SUGGEST_MAX_DETOUR_M=15`, `PICKY_SUGGEST_SUPPRESS_TRIPS=3`.

## 8. UX

**Web app (frontend):**

- **Planning cards** (Robot order page, shipped on demo data): "Picky's advice" with **Skip it** (tomato) and
  **Worth waiting** (amber) cards, each with the amount saved. Accepting a WAIT shows "I'll text you Fri Oct 23"
  (designed; the shipped cards have no accept button yet).
- **Zero copy:** "Nothing worth adding today" (Cart Whisperer, checkout panel); "Nothing worth a stop today, so the
  robot goes straight down your list." (detours, shipped).
- **Live trip card:** up to 3 cards under the cart and budget bar. Each card shows title, price delta (green for
  savings), detour ("+0 m", "+6 m, 12 s") and **Accept** / **No thanks** buttons. A "Why am I seeing this?" expander
  lists the signals in plain words ("You bought it Oct 8 · household of 2 · category default 7 days"). A card that will
  ask for approval is labeled "Needs your OK ($ over budget)". Sponsored items would carry a "Sponsored" label (none
  are).
- **Checkout panel ("Before you pay"):** the same cards next to the checkout approval, plus the running result: "You
  saved $7.07 · added $1.49" (and "avoided $X, expected" when a SKIP / WAIT was accepted).
- **Trip 2:** a small "Hidden because you declined it on trip 1" line under the cards.
- **Merchant dashboard:** a Cart Whisperer tile with accepted rate, $ saved, $ added, avoided spend and basket
  completeness. Seeded history is labeled "simulated".

**Photon (messaging):**

> Before you check out: 1) Drop online cilantro, save $4.28 ($2.99 shipping on a $1.29 herb). 2) Add Taco
> Seasoning, $1.49, on your way. Reply 1, 2, ALL or NO.

Quick replies are `1`, `2`, `ALL` and `NO`. The reply goes to `agent.interpret_reply` → the same POST. Send at most one
suggestions text per phase. WAIT reminder on the promo date:

> SportFit earbuds are $59.99 today (were $79.99). Want them? Reply YES.

**Robot:** there is no screen on the robot. When the cart changes, the robot picks something, or it arrives at a stop
with a pending suggestion, the brain asks through `ask_user`, which reaches the shopper in the web app or on Photon
("Pico: one 30-count pack covers 6 people at 3 each. Grab just one?" / "That case is for a Pixel 7. Put it back?"). The
answer goes through the same accept / decline path. The brain never applies a suggestion itself.

## 9. Trust rules (15% of the score; CLAUDE.md §8)

- Every suggestion shows `reason` + `signals`. **Reasons come from deterministic reason codes; the LLM only phrases
  them.** The phrasing may only cite real signals; this is validated, and it falls back to a template.
- No paid placement: `sponsored=false` everywhere, and a sponsored item would be labeled. No suggestion is ranked by
  margin.
- Never bypasses the mandate: an accept is a normal cart change → `trust.evaluate` → approval when needed. "Ask me
  above $X" always asks. The checkout approval is re-bound to the new cart hash. A declined SKIP / WAIT ("buy anyway")
  goes through the same mandate path.
- Hard gates are code: allergens, dislikes, compatibility, blocked categories, cap, budget. The LLM cannot add, drop,
  reorder, reprice or un-gate anything.
- WAIT savings are estimates from the promo calendar, labeled "expected".
- The raw context stays on the server: the browser and Photon see only derived signals. The LLM gets derived signals,
  not the raw profile or history. Never claim "nothing leaves the store": model calls go to the Claude API.
- A decline is respected: suppressed, and never re-asked in the same trip. Every shown suggestion and its outcome goes
  to the transparency log (`GET /api/trust/ledger`).

## 10. Metrics (Commerce Impact, T-Mobile customer value)

| Metric | Definition | Shown |
|---|---|---|
| Accepted-suggestion rate | accepted / shown (ignored counts as not accepted) | trip card, merchant tile, demo |
| $ saved | Σ `savings_cents` of accepted swap / qty / remove suggestions | checkout panel, merchant tile |
| Avoided spend | Σ `avoided_cents` of accepted SKIP / WAIT, labeled as an estimate | checkout panel, merchant tile, T-Mobile slide |
| $ added | Σ positive `price_delta_cents` of accepted add suggestions | merchant tile (basket lift) |
| Basket completeness | required list items + accepted completes in the cart / (required list items + complete suggestions shown) | trip summary, merchant tile |
| Zero-suggestion rate | passes that returned `[]` / all passes | merchant tile; evidence that it doesn't nag |

Per trip on `Trip.suggestion_metrics`, aggregated per store in `MerchantDashboard.kpis`. Any baseline comparison is
seeded and labeled simulated, and every number comes from code.

## 11. Demo beats

Which store is the main live demo store is an **OPEN TEAM DECISION** (§14). T-Mobile judges see the wireless beat
(§11b) either way, because the brief needs a phone-shopping store.

### 11a. Grocery (store_ave, the taco story)

The numbers come from the current fixtures (the fake agent's taco list for Maya at store_ave):

| Line | Cost |
|---|---|
| 2× `TORT-CORN` | $5.58 |
| 2× `BEEF-GROUND` | $10.98 |
| `BEANS-BLACK` | $1.29 |
| `CHEESE-MEX` | $3.79 |
| `SALSA-MILD` | $3.49 |
| 3× `AVOCADO` | $3.87 |
| 2× `LIMES` | $0.90 |
| `ONION-WHITE` | $0.89 |
| `SOUR-CREAM` | $2.49 |
| `CILANTRO` online (out of stock in store) | $1.29 + $2.99 shipping |
| **Total** | **$37.56** (budget $40) |

With the Ghost Pepper hot-sauce detour accepted (`HOTSAUCE-GHOST`, $6.99 at store_ave), the cart is **$44.55, $4.55
over budget**. (Earlier drafts used $2.99, which is Classic Red's price.) Re-check these against `make demo` once it's
implemented.

**Trip 1:**

- **Planning (optional):** "Skip re-buying Classic Red: bought Sep 26, you go through one about every 5 weeks." It
  sits next to the Ghost Pepper detour, which reads as "try something new".
- **Mid-trip** (Pico reaches the tortillas in aisle 3), the live card shows:
  1. **Qty fix:** "One pack of Corn Tortillas 30 ct covers 6 people at 3 each. Pick 1, not 2: −$2.79." Maya taps
     **Accept**.
  2. **Complete:** "2 lb ground beef but no seasoning: Taco Seasoning Mix $1.49, two slots over (+0 m)." Maya taps
     **Accept**, and Pico grabs it.
  3. At the back wall, **swap/save:** "Swap 1 lb ground beef for Soy Chorizo: covers your vegetarian guest, you rated
     it 4★, save $1.00, same fridge." Maya: **No thanks** ("beef for everyone").
- **Checkout, "Before you pay":** **remove/warn** (`SHIPPING_WASTE`): "Cilantro is your only online item: $1.29 +
  $2.99 shipping. Drop it: −$4.28." Maya taps **Accept**. The checkout approval is re-raised for the new cart hash.
- **Panel total:** $38.97, back under $40: "You saved $7.07 · added $1.49 · 3 of 4 accepted". The Photon thread gets
  the same "Before you check out" text. Narrator: "Every card says why, nothing is sponsored, and it can't skip your
  approval."

**Trip 2** (same request, after the "too mild" follow-up):

- The Soy Chorizo swap is gone ("Hidden because you declined it on trip 1"), and meat swaps are down-weighted.
- The freed slot goes to a **deal match:** "Restaurant Style Tortilla Chips $2.99, 25% off ('Taco night' endcap), goes
  with your salsa".
- The tortilla qty fix shows again, marked "You took this last time".
- The salsa line is already the spicier one (existing feedback story).

**Optional beat:** a "you bought sour cream Tuesday, skip it? (−$2.49)" **skip** card needs a `SOUR-CREAM` purchase
for Maya on 2026-10-13 in `data/fixtures/users.yaml` (a data change, owned by the catalog/data side; not made here).

### 11b. Wireless (store_tmobile, the T-Mobile cut)

Numbers from `arm/frontend` `src/lib/order.ts` + `src/data/stores/tmobile.ts` (fictional; re-check when store_tmobile
moves into `data/`).

- **Request** at store_tmobile: *"case and screen protector for my new Pixel 9, under $90"*.
- **List:** Pixel 9 Clear Case **$29.99**; Pixel 9 Glass 2-pk **$23.99** (20% off with a Pixel 9 case).
- **Detours:** USB-C braided 2 m cable **$19.99** (+0 m, same wall), kept; 10,000 mAh power bank $34.99, not kept (it
  would put the cart over $90).
- **Picky's advice (planning):** **SKIP** the charger (−$24.99: "you bought a 20W USB-C charger in March and it works with
  the Pixel 9") and **WAIT** on the phone (4 installments left, $66.00; $200 off expected Nov 27; "I'll remind you").
- **Total $73.97.** Tech mandate (`im_maya_tech_001`): cap $150, per-item $100, ask above $50, so only the checkout
  approval asks.
- **In-trip (designed):** "grab the $9.99 clearance case too" → **SKIP** ("that's a Pixel 7 case; it doesn't fit the
  Pixel 9. The Clear Case in your basket does.").
- **Second request** *"earbuds for running, under $100"*: SportFit **$79.99** + **WAIT** (Fri Oct 23 $59.99, save
  $20, "I'll text you Friday") + **"Nothing worth a stop today"** (the zero case). The $79.99 line is above the $50
  ask threshold, so it asks too.
- **Story check (team call):** if Maya takes the WAIT on the phone, the case and glass are for a phone she doesn't
  have yet. Either the request becomes "I'm picking up my Pixel 9 today" and the WAIT is about the second line's
  upgrade, or the phone WAIT shows only when the phone is on the list.

## 12. Test plan

**Recommender unit tests** (base deps; `arms/recommender/tests/`, on top of `world.py`). Each generator gets a
positive and a negative case:

- complete: a taco cart with beef and no seasoning → `TACO-SEASONING`;
- replenish: Maya's `OAT-MILK` 9 days → yes, 2 days → no;
- qty fix: 2× `TORT-CORN` for 6 → 1;
- swap/save: cheapest same-diet swap, never a favorite or a SKU named in the request;
- deal match: endcap chips;
- remove/warn: Sam + `TRAIL-MIX` → peanuts, always first and above the threshold; online-only line with shipping >
  subtotal;
- **skip:** owned charger → skip; a recent shelf-stable duplicate → skip; an incompatible case → dropped by the gate,
  and a SKIP card when the shopper asked for or picked it;
- **wait:** a cheaper upcoming promo → wait with `remind_on`; `installments_left > 0` for a phone → wait; no promo →
  no wait;
- **zero:** all candidates below `PICKY_SUGGEST_MIN_SCORE` → `[]`.

Each gate drops its candidate:

- out of stock (`CILANTRO` at store_ave is never an in-store add);
- incompatible (`CASE-PX7-BLK` for a Pixel 9 list);
- already owned (`CHG-20W` for Maya, once owned items are in the data);
- disliked (`HOTSAUCE-GHOST` and the `extra_hot` tag for Jordan);
- blocked category (`LAGER-6PK` for Maya);
- over `cap_cents`;
- over budget without a pairing → `needs_approval`;
- detour > max;
- declined before;
- contradicted by the conversation ("no cilantro");
- already a pending detour rec.

Also: ≤3 shown with SKIP / WAIT counted in the cap, the joint set stays under the cap, `reason_codes` present on every
suggestion, stable ids and order (same input → same output), and `sponsored=False` everywhere.

**LLM phrasing** (mocked client via `utils.llm.set_client`):

- the output order equals the deterministic order, whatever the LLM returns;
- a reason with an invented number ("save $3") or one that cites no reason code / signal → template;
- an unknown id → ignored;
- malformed JSON, timeout or no key → templated reasons.

**Gateway:**

- accept add → `add_line` → APPROVE applies;
- an add whose line exceeds `ask_above_cents` → approval raised, applied only after approve;
- DENY → never applied;
- swap and set_qty are evaluated on the projected cart and applied atomically;
- skip accept removes an in-cart line; wait accept removes / doesn't add and schedules a reminder;
- a mid-trip add → optional stop + `replan` + `fleet.update_goal`;
- an accept while a checkout approval is pending → approval cancelled and re-raised with the new hash;
- decline → `respond` → `profile.updated`;
- `suggestions.updated` on `list.ready`, `cart.updated` (debounced), `item.picked` and at checkout, including an empty
  list;
- REST 404 / 409 (stale).

**Trust:** accepted suggestions get transparency entries with outcomes, and checkout still requires the hash-bound
approval.

**Messaging:** the "Before you check out" text renders ≤3 numbered options, `1` / `ALL` / `NO` map to the right
decisions, and an accepted WAIT produces one reminder text on `remind_on`.

**E2E** (`make demo`, all fakes): trip 1 prints the Cart Whisperer cards and outcomes; trip 2 does not contain the
declined suggestion, and `DEMO OK` asserts both.

## 13. Implementation checklist by arm (after v0.2 merges and the v0.3 contracts PR lands)

**contracts** (`contracts/cart-whisperer` PR)
- [ ] `ShopperContext` (incl. owned / devices / household_lines / upcoming_promos / last_picked_sku), `Suggestion` (incl. `skip` / `wait`, `remind` / `none`, `reason_codes`, `remind_on`, `avoided_cents`), `SuggestionDecision`, `SuggestionResult`, `SuggestionMetrics`; `Trip.suggestions` / `suggestion_metrics`; `suggestions.updated` event (may be empty) + producer; REST rows in `api.md`; `TransparencyEntry` kind; `Product.compatible_with`; bump to 0.3.0; update every fake so `main` stays green

**recommender** (owner of the feature)
- [ ] **P0** `suggest(context, limit=3)`: 8 candidate generators (complete, replenish, swap/save, qty fix, deal match, remove/warn, skip, wait)
- [ ] **P0** Hard gates in code: stock, compatibility (×∞), already owned, allergens / dislikes / guest dietary, mandate snapshot + budget headroom (or `needs_approval`), not declined, conversation, max detour, no pending-rec duplicates; joint-set cap check; a gated item the shopper asked for or picked → SKIP card
- [ ] **P0** Deterministic score (need + taste + money − budget_pressure − detour − fatigue), stable ids, ≤3 per phase incl. SKIP / WAIT; allergen warnings first
- [ ] **P0** Score threshold `PICKY_SUGGEST_MIN_SCORE`: zero suggestions is a normal result
- [ ] **P0** Deterministic `reason_codes` on every suggestion
- [ ] **P0** Fake: templated reasons citing reason codes + signals; deterministic; base deps only; reproduces the §11 cards incl. skip, wait and the zero case
- [ ] **P0** `respond(user_id, suggestion, decision)`: suppress declined (3 trips / 30 days), down-weight the kind, `ProfileUpdate` summary
- [ ] **P1** `complements.py`: dish / setup templates (tacos, new phone, ...) + pair rules (beef → seasoning, chips → salsa, party ≥ 6 → plates / napkins) + co-purchase pairs
- [ ] **P1** Cadence model: per-SKU interval from `PurchaseRecord.ts`, subcategory defaults × `household_size` (labeled "default" in signals)
- [ ] **P1** Real: Sonnet phrases the "why" from reason codes + signals (no rerank), validated, template fallback on any failure
- [ ] **P1** Wireless signals: owned items, device age, installments left, household lines, `compatible_with`, promo calendar (once v0.3 + data land)
- [ ] **P1** User-aware "Skip it" price verdict, shared with the web app's Find & compare
- [ ] **P1** Metrics: per-trip `SuggestionMetrics` (incl. avoided spend, zero-suggestion passes); per-store aggregates for `MerchantDashboard.kpis`
- [ ] **P2** Persist suppressions / outcomes in Postgres with the profile store

**catalog** (after v0.2; data + contracts PRs)
- [ ] **P1** `store_tmobile` planogram (reach-valid slots, allowed fixture kinds) + wireless SKUs + categories in `data/`
- [ ] **P1** `compatible_with`, promo calendar and 13-week price history (shared with Find & compare)

**trust**
- [ ] **P0** Accepts are evaluated like any cart change (`evaluate` on the projected cart for swap / set_qty); "ask me above $X" always asks; a declined SKIP / WAIT ("buy anyway") goes through the mandate
- [ ] **P0** Transparency entries for suggestions (shown → accepted / declined / ignored) in `ledger`
- [ ] **P1** Ledger / audit lines for suggestion accepts (actor `user:<id>`, action `suggestion.accepted`)

**gateway**
- [ ] **P0** Build `ShopperContext` from trip, profile, history, mandate, catalog, route, robot pose, last pick, chat; never serialize it to clients
- [ ] **P0** Trigger `suggest` on `list.ready`, `cart.updated` (debounced ~1 s), `item.picked`, `route.planned`, and when the robot heads to checkout; publish `suggestions.updated` (empty allowed); keep `Trip.suggestions`
- [ ] **P0** `GET /api/trips/{id}/suggestions`, `POST /api/trips/{id}/suggestions/{suggestion_id}` (accept / decline; 404 / 409)
- [ ] **P0** Accept → add / remove / set qty / swap / skip / wait via `TripController` + trust; approvals when needed; re-raise a pending checkout approval with the new cart hash
- [ ] **P1** Mid-trip add → optional stop → `replan` → `fleet.update_goal(add_items)`; checkout add → CHECKOUT → EXECUTING for one stop
- [ ] **P1** Stop calling `checkout_batch`; config knobs `PICKY_SUGGEST_*` (via a `utils/config` PR + `.env.example`)

**agent**
- [ ] **P1** Robot brain: before `pick_item` / on arrival / after an incompatible pick, ask a pending suggestion via `ask_user`; never apply it directly
- [ ] **P1** `interpret_reply` maps Photon replies (`1`, `ALL`, `NO`, "yes to the seasoning") to suggestion decisions
- [ ] **P2** Shopper chat can answer "why did you suggest that?" from the suggestion's reason codes + signals; optional `check_suggestions` brain tool

**messaging**
- [ ] **P1** "Before you check out: ..." text with ≤3 numbered options + quick replies `1` / `2` / `ALL` / `NO`; one text per phase
- [ ] **P1** WAIT reminder text on `remind_on` ("SportFit earbuds are $59.99 today. Want them? Reply YES.")
- [ ] **P2** Mid-trip suggestion text when the web app is not open

**frontend** (teammate)
- [ ] **P0** Planning SKIP / WAIT cards + zero copy: built on demo data ("Picky's advice", "Nothing worth a stop today"); wire to `suggestions.updated`
- [ ] **P0** Live trip card: ≤3 cards with price delta, detour, Accept / No thanks, "Why am I seeing this?" with signals, "Needs your OK" and "Sponsored" labels
- [ ] **P0** Checkout panel "Before you pay" next to the checkout approval; saved / added / avoided summary; "Nothing worth adding today"
- [ ] **P1** Merchant tile: accepted rate, $ saved, $ added, avoided spend, basket completeness (labeled simulated where seeded); trip 2 "hidden because you declined" line

**data** (data owner, after v0.2)
- [ ] **P1** Maya's devices / owned items / household lines; a robot for `store_tmobile` in `robots.yaml`
- [ ] **P2** Maya `SOUR-CREAM` purchase on 2026-10-13 if we want the "bought it Tuesday" skip beat

## 14. Open questions

| Question | Who / how | By |
|---|---|---|
| Confirm the T-Mobile brief docx wording and rubric (30 / 25 / 20 / 25 is from the team plan's copy) | Whoever has `UW_Hackathon_Challenge_Brief.docx`; T-Mobile table | Sat evening |
| **OPEN TEAM DECISION:** live demo store, grocery `store_ave` or wireless `store_tmobile`? T-Mobile judges see the wireless store either way | Team | Sat ~9–10 PM checkpoint |
| **OPEN TEAM DECISION:** sponsor-track limit. Last year's guide allowed **two sponsored tracks + the title track**; we list three side tracks (T-Mobile, Photon, SideShift). If a cut is forced, the team plan's default priority is Visa → T-Mobile → Photon → SideShift | Organizers / Hacker Guide, then team | Sat evening |
| One `IntentMandate` per user vs the web app's separate tech mandate (`im_maya_tech_001`): per-category caps or several mandates per user? | trust + contracts owners | v0.3 PR |
| Wireless story check (§11b): the phone WAIT vs accessories for a Pixel 9 she may not own yet | Frontend owner + narrator | Before rehearsal |
| Grocery story drift: the web app's taco preset shows "Skip hot sauce: you bought Classic Red on Oct 11" (seeded history; the fixture date is Sep 26) and detours to Chipotle salsa, guac kit and chips, with no Ghost Pepper detour (§11a, `docs/demo-script.md`). Align the preset with the backend story (and phrase it "skip re-buying Classic Red", §4), or narrate what's on screen | Frontend + recommender owners | Before rehearsal |
| Where complements live long term: `arms/recommender/complements.py` (now) vs a catalog-owned `data/` file | recommender + catalog owners | v0.3 PR |
| Detour estimate: Manhattan insertion in the recommender vs asking routing for a quick insertion cost | recommender + routing owners | v0.3 PR |
