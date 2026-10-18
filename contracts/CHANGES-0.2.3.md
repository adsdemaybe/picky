# contracts 0.2.3: wireless retail (plans, trade-in, financing, promos) for the T-Mobile-style store

**Additive patch on 0.2.2.** One new module (`contracts/wireless.py`), one new service Protocol
(`services.WirelessService`), one new optional `Services` field (`wireless`) and one new arm name (`wireless`).
Nothing existing changes shape; `versions_compatible` compares major.minor, so deployed 0.2.x Isaac workers keep
connecting (the sim link carries no wireless messages).

**Affected arms:** wireless (new: implements it, `arms/wireless`), gateway (wires it from `SERVICE_FACTORIES`, adds
`/api/wireless/*` in `arms/gateway/wireless_api.py`), frontend (generated TS types; the `/plans` view and Ask Picky's
wireless tools call these routes). Every other arm: unchanged; their fakes are untouched.

## What is new

- `contracts/wireless.py`: `ServicePlan`, `PlanQuote`, `Promotion` / `PromoKind` / `PromoCondition` /
  `PromoEligibility`, `TradeInModel` / `TradeInCondition` / `TradeInRequest` / `TradeInQuote`, `FinancingTerms` /
  `FinancingRequest` / `FinancingQuote`, `Line` / `WirelessAccount`, `HouseholdRequest` / `HouseholdOption` /
  `HouseholdOptimization`, `CompatibleAccessory`, `QuoteRequest` / `QuoteItem` / `TradeInSelection` / `QuoteLine` /
  `StockCheck` / `CostBreakdown`, `WirelessCatalogInfo`, and `WirelessFile` (schema of `data/wireless.yaml`).
- `services.WirelessService`: `info`, `quote_plan`, `compare_plans`, `compatible_accessories`, `estimate_trade_in`,
  `finance`, `promotions`, `get_account`, `list_accounts`, `optimize_household`, `quote`.
- `ARM_NAMES` += `wireless`; `SERVICE_PROTOCOLS["wireless"]`; `Services.wireless`.
- `data/wireless.yaml` (synthetic): 5 plans, 9 promotions (2 upcoming sales for WAIT), 12 trade-in models,
  accessory → device compatibility for the store_tmobile catalog, 7 fictional household accounts (maya, jordan + 5
  wireless personas).
- REST: see `api.md` "wireless retail".

## Rules carried in the contract

- Integer cents; all values synthetic (`synthetic=True`, `SYNTHETIC_NOTICE`), never a real offer or approval.
- Promotions apply only when every condition is met in code; ineligible ones are reported with the unmet conditions.
- Upfront (one-time) and monthly (recurring) charges are kept apart in `CostBreakdown`.
- This is NOT the planned v0.3 Cart Whisperer `ShopperContext` / `Suggestion`; it gives that work the device,
  installment, compatibility and promo-calendar signals it needs (`WirelessAccount`, `compatibility`, `upcoming`).
