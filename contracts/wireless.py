"""Wireless retail contracts (contracts 0.2.3, additive): service plans, lines, accounts, trade-in, device financing,
promotions, accessory compatibility and the deterministic cost quote.

Used by the T-Mobile-style wireless store ``store_tmobile`` (products live in data/products.yaml under
``category: wireless``; the store floor in data/planograms/store_tmobile.yaml). Plans, promos, trade-in values,
financing terms, compatibility and demo accounts live in ``data/wireless.yaml``.

Rules (CLAUDE.md §7-8):
- Money is integer cents. Every price, credit and estimate here is SYNTHETIC demo data (``synthetic=True`` on every
  quote), never a real carrier offer, approved financing or a real trade-in value.
- All math (plan totals, installments, trade-in, savings, taxes) is deterministic code in ``arms/wireless``. The LLM
  only phrases results; it never supplies a price, a credit or an eligibility decision.
- A promotion applies only when every one of its ``requires`` conditions is met by the request; ineligible promos are
  listed with ``eligible=False`` and the unmet conditions, never silently applied.
"""

from datetime import date
from enum import StrEnum

from pydantic import Field, computed_field

from contracts.common import Cents, Model, Sku, StoreId, UserId

PlanId = str
PromoId = str
TradeInModelId = str
AccountId = str
LineId = str

DEFAULT_FINANCE_MONTHS = 24
SYNTHETIC_NOTICE = "Synthetic demo pricing: not a real T-Mobile offer, approved financing or trade-in value."


class PlanTier(StrEnum):
    PREPAID = "prepaid"
    VALUE = "value"
    STANDARD = "standard"
    PREMIUM = "premium"


class ServicePlan(Model):
    """One postpaid/prepaid plan from data/wireless.yaml. ``line_prices_cents[n-1]`` = advertised monthly total for n
    lines WITH autopay; lines beyond the table cost ``extra_line_cents`` each."""

    plan_id: PlanId = Field(min_length=1)
    name: str
    tier: PlanTier
    summary: str = ""
    line_prices_cents: list[Cents] = Field(min_length=1, description="Monthly total (autopay) for 1..N lines.")
    extra_line_cents: Cents = Field(description="Each line beyond len(line_prices_cents), up to max_lines.")
    max_lines: int = Field(ge=1, le=12)
    autopay_discount_cents_per_line: Cents = Field(0, description="Added back per line (max 8) without autopay.")
    data: str = Field(description="Human data allowance, e.g. 'Unlimited' or '10 GB'.")
    priority_data_gb: int | None = Field(None, ge=0, description="Premium data before deprioritization; None = all.")
    hotspot_gb: int | None = Field(None, ge=0, description="High-speed hotspot per line per month; None = unlimited.")
    perks: list[str] = Field(default_factory=list)
    taxes_and_fees_included: bool = False
    requires: list[str] = Field(default_factory=list, description="Eligibility codes, e.g. 'age_55'.")
    device_promos_eligible: bool = Field(True, description="False = no device promo credits on this plan.")


class TradeInCondition(StrEnum):
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    BROKEN = "broken"


class TradeInModel(Model):
    model_id: TradeInModelId = Field(min_length=1)
    name: str
    brand: str
    release_year: int = Field(ge=2010, le=2030)
    values_cents: dict[TradeInCondition, Cents] = Field(description="Illustrative value per condition.")


class PromoKind(StrEnum):
    DEVICE_CREDIT = "device_credit"
    TRADE_IN_BONUS = "trade_in_bonus"
    LINE_DISCOUNT = "line_discount"
    ACCESSORY_BUNDLE = "accessory_bundle"
    PROTECTION = "protection"
    HOME_INTERNET = "home_internet"
    SALE = "sale"
    """Instant price drop on ``skus`` (credit_cents off each, paid upfront); usually dated (drives WAIT)."""


class PromoCondition(StrEnum):
    """What a request must show for a promo to apply (checked in code, never by the LLM)."""

    NEW_LINE = "new_line"
    TRADE_IN = "trade_in"
    PORT_IN = "port_in"
    PLAN_PREMIUM = "plan_premium"
    PLAN_STANDARD_OR_BETTER = "plan_standard_or_better"
    FINANCED = "financed"
    WITH_DEVICE = "with_device"
    ACTIVE = "active"
    """Implicit: today is within [starts, ends]."""


class Promotion(Model):
    promo_id: PromoId = Field(min_length=1)
    name: str
    kind: PromoKind
    skus: list[Sku] = Field(default_factory=list, description="Products it applies to; empty = any in its kind.")
    with_skus: list[Sku] = Field(default_factory=list, description="WITH_DEVICE: one of these must be in the cart.")
    credit_cents: Cents = Field(0, description="Total credit (device credits are spread over credit_months).")
    percent_off: int | None = Field(None, ge=1, le=100, description="Accessory bundles: % off the listed skus.")
    credit_months: int = Field(1, ge=1, le=36)
    monthly_cents: Cents = Field(0, description="Recurring price (protection / home internet add-ons).")
    requires: list[PromoCondition] = Field(default_factory=list)
    min_lines: int = Field(1, ge=1, le=12, description="Line discounts: account must have at least this many lines.")
    plan_ids: list[PlanId] = Field(default_factory=list, description="Only on these plans (empty = any plan).")
    starts: date | None = None
    ends: date | None = None
    terms: str = ""


class PromoEligibility(Model):
    promo: Promotion
    eligible: bool
    unmet: list[PromoCondition] = Field(default_factory=list)
    upcoming: bool = Field(False, description="Starts after today (drives a Cart Whisperer WAIT).")
    reasons: list[str] = Field(default_factory=list, description="Human, deterministic: why it does / doesn't apply.")


class Line(Model):
    line_id: LineId
    label: str = Field(description="Who uses it, e.g. 'Maya' or 'Line 2'.")
    device_name: str
    device_model_id: TradeInModelId | None = None
    device_sku: Sku | None = None
    device_age_months: int = Field(0, ge=0)
    installments_left: int = Field(0, ge=0)
    monthly_installment_cents: Cents = 0
    usage_gb_month: int = Field(0, ge=0)
    hotspot_gb_month: int = Field(0, ge=0)


class WirelessAccount(Model):
    """Synthetic household account (data/wireless.yaml ``accounts``; fictional people only)."""

    account_id: AccountId
    user_id: UserId
    persona: str = Field(description="Short persona, e.g. 'Budget-conscious student'.")
    carrier: str = Field("T-Mobile", description="Current carrier; anything else = a switcher (port-in eligible).")
    plan_id: PlanId | None = None
    autopay: bool = True
    current_monthly_cents: Cents = Field(description="What the account pays today for service (synthetic).")
    lines: list[Line] = Field(default_factory=list)
    eligibility: list[str] = Field(default_factory=list, description="e.g. 'age_55', 'military'.")
    owned_accessories: list[Sku] = Field(default_factory=list, description="Accessories they already own (SKIP).")
    priorities: list[str] = Field(default_factory=list, description="camera, battery, price, performance, data...")
    notes: str = ""

    @computed_field
    @property
    def device_installments_cents(self) -> int:
        return sum(ln.monthly_installment_cents for ln in self.lines if ln.installments_left > 0)


class FinancingTerms(Model):
    months_options: list[int] = Field(description="Allowed installment terms, e.g. [24, 36].")
    apr_bps: int = Field(0, ge=0, description="Annual rate in basis points (0 = 0% APR demo financing).")
    min_down_cents: Cents = 0
    max_financed_cents: Cents = Field(description="Above this the difference is due as down payment.")
    min_financed_cents: Cents = Field(4900, description="Devices below this aren't financed.")
    financeable_subcategories: list[str] = Field(default_factory=lambda: ["phones", "tablets", "wearables"])


class PlanQuote(Model):
    plan_id: PlanId
    plan_name: str
    lines: int = Field(ge=1)
    autopay: bool
    advertised_monthly_cents: Cents = Field(description="Base plan price for these lines with autopay.")
    monthly_cents: Cents = Field(description="Estimated service total incl. autopay adjustment, before taxes.")
    per_line_cents: Cents
    est_taxes_fees_cents: Cents = 0
    perks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    eligible: bool = True
    ineligible_reason: str | None = None
    synthetic: bool = True


class FinancingQuote(Model):
    sku: Sku
    name: str
    price_cents: Cents
    down_payment_cents: Cents
    months: int
    monthly_cents: Cents = Field(description="Installment (the last one absorbs rounding).")
    last_month_cents: Cents
    promo_credit_monthly_cents: Cents = 0
    promo_credit_total_cents: Cents = 0
    promo_ids: list[PromoId] = Field(default_factory=list)
    net_monthly_cents: Cents = Field(description="Installment minus promo credit (never below 0).")
    total_cost_cents: Cents = Field(description="Down + all installments - promo credits.")
    assumptions: list[str] = Field(default_factory=list)
    synthetic: bool = True


class TradeInQuote(Model):
    model_id: TradeInModelId
    name: str
    condition: TradeInCondition
    value_cents: Cents
    bonus_cents: Cents = Field(0, description="Eligible trade-in promo on top of the value (spread as credits).")
    promo_ids: list[PromoId] = Field(default_factory=list)
    remaining_installments_cents: Cents = Field(0, description="Still owed on the old device (from the account).")
    net_cents: int = Field(description="value + bonus - remaining balance; may be negative.")
    keep_recommended: bool = Field(False, description="Deterministic: trading in doesn't pay off yet.")
    reasons: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    synthetic: bool = True


class TradeInRequest(Model):
    model_id: TradeInModelId
    condition: TradeInCondition
    user_id: UserId | None = None
    target_sku: Sku | None = Field(None, description="Phone they'd upgrade to (promo eligibility).")
    plan_id: PlanId | None = None


class FinancingRequest(Model):
    sku: Sku
    months: int = DEFAULT_FINANCE_MONTHS
    down_payment_cents: Cents = 0
    plan_id: PlanId | None = None
    new_line: bool = False
    trade_in: bool = False
    port_in: bool = False


class QuoteItem(Model):
    sku: Sku
    qty: int = Field(1, ge=1, le=10)
    financed: bool = Field(False, description="Phones/tablets/watches only; accessories are paid upfront.")
    months: int = DEFAULT_FINANCE_MONTHS
    down_payment_cents: Cents = 0


class TradeInSelection(Model):
    model_id: TradeInModelId
    condition: TradeInCondition


class QuoteRequest(Model):
    """Everything a wireless cart needs to be priced (the web app's cart -> POST /api/wireless/quote)."""

    store_id: StoreId = "store_tmobile"
    user_id: UserId | None = None
    items: list[QuoteItem] = Field(default_factory=list)
    plan_id: PlanId | None = None
    lines: int = Field(1, ge=1, le=12)
    new_lines: int = Field(0, ge=0, le=12, description="How many of ``lines`` are new (NEW_LINE promos).")
    autopay: bool = True
    port_in: bool = False
    trade_in: TradeInSelection | None = None
    add_on_promo_ids: list[PromoId] = Field(default_factory=list, description="Opt-in protection / home internet.")
    term_months: int = Field(DEFAULT_FINANCE_MONTHS, ge=1, le=36, description="Period for the total-cost estimate.")


class QuoteLine(Model):
    kind: str = Field(description="device | accessory | plan | add_on | credit | trade_in | tax | fee")
    label: str
    sku: Sku | None = None
    promo_id: PromoId | None = None
    upfront_cents: int = 0
    monthly_cents: int = 0
    note: str = ""


class StockCheck(Model):
    sku: Sku
    requested: int
    available: int
    ok: bool


class CostBreakdown(Model):
    """Deterministic estimate: one-time (upfront) charges kept apart from recurring monthly ones."""

    request: QuoteRequest
    lines: list[QuoteLine] = Field(default_factory=list)
    plan: PlanQuote | None = None
    financing: list[FinancingQuote] = Field(default_factory=list)
    trade_in: TradeInQuote | None = None
    promos: list[PromoEligibility] = Field(default_factory=list)
    stock: list[StockCheck] = Field(default_factory=list)
    upfront_cents: int
    monthly_cents: int
    est_tax_upfront_cents: Cents
    est_tax_monthly_cents: Cents
    total_over_term_cents: int
    term_months: int
    tax_rate_bps: int
    assumptions: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list, description="Unknown SKU, out of stock, invalid plan/lines...")
    warnings: list[str] = Field(
        default_factory=list, description="Not blocking: accessory doesn't fit any phone in the cart or on the account."
    )
    synthetic: bool = True
    notice: str = SYNTHETIC_NOTICE

    @computed_field
    @property
    def ok(self) -> bool:
        return not self.errors


class HouseholdOption(Model):
    plan: PlanQuote
    savings_monthly_cents: int = Field(description="current - proposed (negative = costs more).")
    fits_usage: bool
    tradeoffs: list[str] = Field(default_factory=list)


class HouseholdRequest(Model):
    user_id: UserId | None = None
    lines: int | None = Field(None, ge=1, le=12, description="Defaults to the account's line count.")
    current_monthly_cents: Cents | None = None
    max_usage_gb: int | None = Field(None, ge=0)
    max_hotspot_gb: int | None = Field(None, ge=0)
    eligibility: list[str] = Field(default_factory=list)


class HouseholdOptimization(Model):
    account_id: AccountId | None = None
    lines: int
    current_plan_id: PlanId | None = None
    current_monthly_cents: Cents | None = None
    options: list[HouseholdOption] = Field(description="Cheapest-that-fits first.")
    recommended_plan_id: PlanId | None = None
    keep_current: bool = Field(description="True when no plan that fits saves money.")
    reason: str
    assumptions: list[str] = Field(default_factory=list)
    synthetic: bool = True


class CompatibleAccessory(Model):
    sku: Sku
    name: str
    subcategory: str | None = None
    price_cents: Cents
    in_stock: int = 0
    optional: bool = True
    why: str = ""


class WirelessCatalogInfo(Model):
    """GET /api/wireless: everything static the web app needs (generated into the frontend's demo fallback too)."""

    plans: list[ServicePlan]
    promotions: list[Promotion]
    trade_in_models: list[TradeInModel]
    financing: FinancingTerms
    compatibility: dict[Sku, list[Sku]] = Field(description="Accessory sku -> device skus it fits.")
    tax_rate_bps: int
    store_id: StoreId
    today: date = Field(description="The engine's 'today' (promo windows), fixed in data for a repeatable demo.")
    notice: str = SYNTHETIC_NOTICE


class WirelessFile(Model):
    """Schema of data/wireless.yaml (validated by arms/wireless and mirrored by the web app's gen-data)."""

    schema_version: int = 1
    store_id: StoreId
    today: date
    tax_rate_bps: int = Field(ge=0, le=2500)
    service_taxes_fees_bps: int = Field(ge=0, le=3000)
    new_line_fee_cents: Cents = 0
    financing: FinancingTerms
    plans: list[ServicePlan]
    promotions: list[Promotion] = Field(default_factory=list)
    trade_in_models: list[TradeInModel] = Field(default_factory=list)
    compatibility: dict[Sku, list[Sku]] = Field(default_factory=dict)
    accounts: list[WirelessAccount] = Field(default_factory=list)
