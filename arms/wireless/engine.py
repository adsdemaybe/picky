"""Deterministic wireless pricing engine (pure functions over ``WirelessFile`` + catalog products). No I/O, no LLM.

Every number the web app or the assistant shows for plans, installments, trade-ins, promos and wireless carts comes
from here. Conventions: integer cents; installments round DOWN and the last month absorbs the remainder; percentages
round half up; tax is estimated on the full retail price of devices (due upfront even when financed) and on plan
prices that don't include taxes & fees.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from contracts.catalog import Product
from contracts.common import Sku
from contracts.wireless import (
    CompatibleAccessory,
    CostBreakdown,
    FinancingQuote,
    FinancingRequest,
    HouseholdOptimization,
    HouseholdOption,
    HouseholdRequest,
    Line,
    PlanQuote,
    PlanTier,
    PromoCondition,
    PromoEligibility,
    PromoKind,
    Promotion,
    QuoteLine,
    QuoteRequest,
    ServicePlan,
    StockCheck,
    TradeInQuote,
    TradeInRequest,
    WirelessAccount,
    WirelessCatalogInfo,
    WirelessFile,
)

ANY_PHONE = "*"
DEVICE_PROMO_KINDS = (PromoKind.DEVICE_CREDIT, PromoKind.TRADE_IN_BONUS)
ADD_ON_KINDS = (PromoKind.PROTECTION, PromoKind.HOME_INTERNET)
KEEP_DEVICE_MAX_AGE_MONTHS = 24
"""A phone this young (with no trade-in promo) is worth keeping: the honest-recommendation rule."""
WAIT_WINDOW_DAYS = 60
"""An upcoming sale on the target phone within this many days -> recommend waiting."""
HEAVY_USER_GB = 50
"""Above this monthly usage, deprioritized 'Unlimited' data no longer counts as a fit."""


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    return f"{sign}${abs(cents) / 100:,.2f}"


def pct(cents: int, bps: int) -> int:
    """cents * bps / 10000, rounded half up (non-negative inputs)."""
    return (cents * bps + 5000) // 10000


@dataclass
class _Ctx:
    """What a cart shows for promo eligibility."""

    plan: ServicePlan | None = None
    lines: int = 1
    new_lines: int = 0
    trade_in: bool = False
    port_in: bool = False
    financed: set[Sku] = field(default_factory=set)
    cart_skus: set[Sku] = field(default_factory=set)


@dataclass
class _Line:
    line: QuoteLine
    months: int = 0
    """How long ``line.monthly_cents`` recurs (0 = for the whole term)."""
    last_extra: int = 0
    """Added once in the line's last month (installment rounding remainder)."""


class WirelessEngine:
    def __init__(self, data: WirelessFile) -> None:
        self.data = data
        self.plans = {p.plan_id: p for p in data.plans}
        self.promos = {p.promo_id: p for p in data.promotions}
        self.models = {m.model_id: m for m in data.trade_in_models}
        self.accounts = {a.user_id: a for a in data.accounts}

    # ---- static info --------------------------------------------------------------------------------------------
    def info(self) -> WirelessCatalogInfo:
        d = self.data
        return WirelessCatalogInfo(
            plans=d.plans,
            promotions=d.promotions,
            trade_in_models=d.trade_in_models,
            financing=d.financing,
            compatibility=d.compatibility,
            tax_rate_bps=d.tax_rate_bps,
            store_id=d.store_id,
            today=d.today,
        )

    def plan(self, plan_id: str) -> ServicePlan:
        if plan_id not in self.plans:
            raise KeyError(f"unknown plan {plan_id!r}")
        return self.plans[plan_id]

    def account(self, user_id: str) -> WirelessAccount | None:
        return self.accounts.get(user_id)

    # ---- plans --------------------------------------------------------------------------------------------------
    def advertised_cents(self, plan: ServicePlan, lines: int) -> int:
        if lines < 1 or lines > plan.max_lines:
            raise ValueError(f"{plan.name} allows 1-{plan.max_lines} lines, not {lines}")
        table = plan.line_prices_cents
        if lines <= len(table):
            return table[lines - 1]
        return table[-1] + (lines - len(table)) * plan.extra_line_cents

    def quote_plan(
        self, plan_id: str, lines: int, autopay: bool = True, eligibility: list[str] | None = None
    ) -> PlanQuote:
        plan = self.plan(plan_id)
        base = self.advertised_cents(plan, lines)
        monthly = base if autopay else base + plan.autopay_discount_cents_per_line * min(lines, 8)
        missing = [r for r in plan.requires if r not in (eligibility or [])]
        assumptions = [f"Advertised {money(base)}/mo for {lines} line(s) with autopay."]
        if not autopay and plan.autopay_discount_cents_per_line:
            assumptions.append(f"Without autopay: +{money(plan.autopay_discount_cents_per_line)}/line.")
        taxes = 0 if plan.taxes_and_fees_included else pct(monthly, self.data.service_taxes_fees_bps)
        assumptions.append(
            "Taxes & fees included."
            if plan.taxes_and_fees_included
            else f"Taxes & fees estimated at {self.data.service_taxes_fees_bps / 100:.0f}% ({money(taxes)}/mo)."
        )
        return PlanQuote(
            plan_id=plan.plan_id,
            plan_name=plan.name,
            lines=lines,
            autopay=autopay,
            advertised_monthly_cents=base,
            monthly_cents=monthly,
            per_line_cents=monthly // lines,
            est_taxes_fees_cents=taxes,
            perks=list(plan.perks),
            assumptions=assumptions,
            eligible=not missing,
            ineligible_reason=f"Requires: {', '.join(missing)}" if missing else None,
        )

    def compare_plans(self, lines: int, autopay: bool = True, eligibility: list[str] | None = None) -> list[PlanQuote]:
        quotes = [
            self.quote_plan(p.plan_id, lines, autopay, eligibility) for p in self.data.plans if lines <= p.max_lines
        ]
        return sorted(quotes, key=lambda q: (not q.eligible, q.monthly_cents + q.est_taxes_fees_cents, q.plan_id))

    # ---- promos -------------------------------------------------------------------------------------------------
    def _unmet(self, promo: Promotion, ctx: _Ctx) -> tuple[list[PromoCondition], list[str]]:
        unmet: list[PromoCondition] = []
        reasons: list[str] = []
        tier = ctx.plan.tier if ctx.plan else None
        for cond in promo.requires:
            ok = {
                PromoCondition.NEW_LINE: ctx.new_lines > 0,
                PromoCondition.TRADE_IN: ctx.trade_in,
                PromoCondition.PORT_IN: ctx.port_in,
                PromoCondition.PLAN_PREMIUM: tier == PlanTier.PREMIUM,
                PromoCondition.PLAN_STANDARD_OR_BETTER: tier in (PlanTier.STANDARD, PlanTier.PREMIUM),
                PromoCondition.FINANCED: bool(set(promo.skus) & ctx.financed) if promo.skus else bool(ctx.financed),
                PromoCondition.WITH_DEVICE: bool(set(promo.with_skus) & ctx.cart_skus),
                PromoCondition.ACTIVE: True,
            }[cond]
            if not ok:
                unmet.append(cond)
                reasons.append(_COND_TEXT[cond])
        if promo.min_lines > ctx.lines:
            reasons.append(f"Needs {promo.min_lines}+ lines.")
        if promo.plan_ids and (ctx.plan is None or ctx.plan.plan_id not in promo.plan_ids):
            reasons.append(f"Only on {', '.join(self.plans[p].name for p in promo.plan_ids if p in self.plans)}.")
        if promo.kind in DEVICE_PROMO_KINDS and ctx.plan is not None and not ctx.plan.device_promos_eligible:
            reasons.append(f"{ctx.plan.name} isn't eligible for device promos.")
        today = self.data.today
        if (promo.starts and promo.starts > today) or (promo.ends and promo.ends < today):
            unmet.append(PromoCondition.ACTIVE)
            reasons.append(
                f"Starts {promo.starts:%b %d}." if promo.starts and promo.starts > today else "This promo has ended."
            )
        return unmet, reasons

    def eligibility(self, promo: Promotion, ctx: _Ctx) -> PromoEligibility:
        unmet, reasons = self._unmet(promo, ctx)
        upcoming = bool(promo.starts and promo.starts > self.data.today)
        eligible = not reasons
        return PromoEligibility(
            promo=promo,
            eligible=eligible,
            unmet=unmet,
            upcoming=upcoming,
            reasons=reasons or ["All conditions met."],
        )

    def _ctx(self, req: QuoteRequest) -> _Ctx:
        plan = self.plans.get(req.plan_id) if req.plan_id else None
        return _Ctx(
            plan=plan,
            lines=req.lines,
            new_lines=req.new_lines,
            trade_in=req.trade_in is not None,
            port_in=req.port_in,
            financed={i.sku for i in req.items if i.financed},
            cart_skus={i.sku for i in req.items},
        )

    def promotions(self, req: QuoteRequest) -> list[PromoEligibility]:
        ctx = self._ctx(req)
        return [self.eligibility(p, ctx) for p in self.data.promotions]

    def _relevant(self, promo: Promotion, req: QuoteRequest) -> bool:
        skus = {i.sku for i in req.items}
        if promo.kind in ADD_ON_KINDS:
            return promo.promo_id in req.add_on_promo_ids
        if promo.kind == PromoKind.LINE_DISCOUNT:
            return req.plan_id is not None
        return bool(set(promo.skus) & skus)

    # ---- financing ----------------------------------------------------------------------------------------------
    def financeable(self, product: Product) -> bool:
        f = self.data.financing
        return product.subcategory in f.financeable_subcategories and product.price_cents >= f.min_financed_cents

    def _device_promos(self, sku: Sku, ctx: _Ctx) -> list[Promotion]:
        return [
            p
            for p in self.data.promotions
            if p.kind in DEVICE_PROMO_KINDS and sku in p.skus and self.eligibility(p, ctx).eligible
        ]

    def finance(self, product: Product, req: FinancingRequest, ctx: _Ctx | None = None) -> FinancingQuote:
        f = self.data.financing
        if not self.financeable(product):
            raise ValueError(f"{product.name} can't be financed (paid upfront)")
        if req.months not in f.months_options:
            raise ValueError(f"financing term must be one of {f.months_options} months, not {req.months}")
        if ctx is None:
            plan = self.plan(req.plan_id) if req.plan_id else None
            ctx = _Ctx(
                plan=plan,
                new_lines=int(req.new_line),
                trade_in=req.trade_in,
                port_in=req.port_in,
                financed={product.sku},
                cart_skus={product.sku},
            )
        price = product.price_cents
        down = min(max(req.down_payment_cents, f.min_down_cents), price)
        financed = price - down
        if financed > f.max_financed_cents:
            down += financed - f.max_financed_cents
            financed = f.max_financed_cents
        monthly = financed // req.months
        last = financed - monthly * (req.months - 1)
        promos = self._device_promos(product.sku, ctx)
        credit_monthly = 0
        credit_total = 0
        for p in promos:
            total = min(p.credit_cents, price)
            per = total // p.credit_months
            credit_monthly += per
            credit_total += per * p.credit_months
        assumptions = [
            f"{f.apr_bps / 100:.0f}% APR, {req.months} monthly installments (demo terms; not approved financing).",
            f"Down payment {money(down)}"
            + (" (amount above the financing limit)." if down > req.down_payment_cents else "."),
            "Sales tax on the full retail price is due upfront.",
        ]
        for p in promos:
            assumptions.append(
                f"{p.name}: {money(p.credit_cents // p.credit_months)}/mo for {p.credit_months} mo. {p.terms}"
            )
        return FinancingQuote(
            sku=product.sku,
            name=product.name,
            price_cents=price,
            down_payment_cents=down,
            months=req.months,
            monthly_cents=monthly,
            last_month_cents=last,
            promo_credit_monthly_cents=credit_monthly,
            promo_credit_total_cents=credit_total,
            promo_ids=[p.promo_id for p in promos],
            net_monthly_cents=max(0, monthly - credit_monthly),
            total_cost_cents=max(0, down + financed - credit_total),
            assumptions=assumptions,
        )

    # ---- trade-in -----------------------------------------------------------------------------------------------
    def estimate_trade_in(self, req: TradeInRequest, target: Product | None = None) -> TradeInQuote:
        if req.model_id not in self.models:
            raise KeyError(f"unknown trade-in model {req.model_id!r}")
        model = self.models[req.model_id]
        value = model.values_cents[req.condition]
        account = self.accounts.get(req.user_id) if req.user_id else None
        line: Line | None = next(
            (ln for ln in (account.lines if account else []) if ln.device_model_id == model.model_id), None
        )
        remaining = line.installments_left * line.monthly_installment_cents if line else 0
        reasons: list[str] = []
        bonus = 0
        promo_ids: list[str] = []
        if target is not None:
            plan = self.plans.get(req.plan_id) if req.plan_id else None
            ctx = _Ctx(plan=plan, trade_in=True, financed={target.sku}, cart_skus={target.sku})
            for p in self._device_promos(target.sku, ctx):
                if p.kind == PromoKind.TRADE_IN_BONUS:
                    bonus += max(0, min(p.credit_cents, target.price_cents) - value)
                    promo_ids.append(p.promo_id)
                    reasons.append(f"{p.name} replaces the trade-in value (credits over {p.credit_months} mo).")
        if remaining:
            reasons.append(
                f"You still owe {money(remaining)} ({line.installments_left} payments of "  # type: ignore[union-attr]
                f"{money(line.monthly_installment_cents)}) on it; that's due if you trade it in now."  # type: ignore[union-attr]
            )
        net = value + bonus - remaining
        keep = False
        if remaining >= value + bonus:
            keep = True
            reasons.append("The trade-in value doesn't cover what you still owe: keep it until it's paid off.")
        age = line.device_age_months if line else None
        if age is not None and age <= KEEP_DEVICE_MAX_AGE_MONTHS and not bonus:
            keep = True
            reasons.append(f"Your {model.name} is only {age} months old: it's fine to keep it.")
        if target is not None:
            for p in self.data.promotions:
                if p.kind == PromoKind.SALE and target.sku in p.skus and p.starts and p.starts > self.data.today:
                    days = (p.starts - self.data.today).days
                    if days <= WAIT_WINDOW_DAYS:
                        keep = True
                        reasons.append(
                            f"{p.name} starts {p.starts:%b %d} ({days} days): waiting saves {money(p.credit_cents)}."
                        )
        if not reasons:
            reasons.append(f"Trading in gives you {money(net)} toward your next phone.")
        return TradeInQuote(
            model_id=model.model_id,
            name=model.name,
            condition=req.condition,
            value_cents=value,
            bonus_cents=bonus,
            promo_ids=promo_ids,
            remaining_installments_cents=remaining,
            net_cents=net,
            keep_recommended=keep,
            reasons=reasons,
            assumptions=[
                "Illustrative value for the stated condition; the final value depends on inspection.",
                "Trade-in value applied as an instant credit unless a trade-in promo replaces it.",
                "Future trade-in values are not guaranteed.",
            ],
        )

    # ---- household ----------------------------------------------------------------------------------------------
    @staticmethod
    def fits(plan: ServicePlan, max_gb: int, max_hotspot: int) -> tuple[bool, list[str]]:
        notes: list[str] = []
        ok = True
        if plan.data != "Unlimited" and plan.priority_data_gb is not None and max_gb > plan.priority_data_gb:
            ok = False
            notes.append(f"{plan.data} is less than the heaviest line's {max_gb} GB/mo.")
        if max_gb > HEAVY_USER_GB and plan.priority_data_gb is not None and plan.priority_data_gb < max_gb:
            ok = False
            notes.append(f"Only {plan.priority_data_gb} GB of priority data for a {max_gb} GB/mo line.")
        if plan.hotspot_gb is not None and max_hotspot > plan.hotspot_gb:
            ok = False
            notes.append(f"{plan.hotspot_gb} GB hotspot is less than the {max_hotspot} GB used.")
        if plan.priority_data_gb == 0:
            notes.append("Data can slow down when the network is busy.")
        return ok, notes

    def optimize_household(self, req: HouseholdRequest) -> HouseholdOptimization:
        account = self.accounts.get(req.user_id) if req.user_id else None
        if req.user_id and account is None and req.lines is None:
            raise KeyError(f"no wireless account for {req.user_id!r}")
        lines = req.lines or (len(account.lines) if account else 1)
        max_gb = (
            req.max_usage_gb
            if req.max_usage_gb is not None
            else max((ln.usage_gb_month for ln in account.lines), default=0)
            if account
            else 0
        )
        max_hs = (
            req.max_hotspot_gb
            if req.max_hotspot_gb is not None
            else max((ln.hotspot_gb_month for ln in account.lines), default=0)
            if account
            else 0
        )
        eligibility = list({*(req.eligibility or []), *(account.eligibility if account else [])})
        current_plan = self.plans.get(account.plan_id) if account and account.plan_id else None
        current = (
            req.current_monthly_cents
            if req.current_monthly_cents is not None
            else (account.current_monthly_cents if account else None)
        )
        options: list[HouseholdOption] = []
        for q in self.compare_plans(lines, account.autopay if account else True, eligibility):
            if not q.eligible:
                continue
            plan = self.plans[q.plan_id]
            ok, notes = self.fits(plan, max_gb, max_hs)
            if current_plan is not None:
                lost = [p for p in current_plan.perks if p not in plan.perks]
                if lost:
                    notes.append("Loses: " + ", ".join(lost) + ".")
            savings = (current - q.monthly_cents) if current is not None else 0
            options.append(HouseholdOption(plan=q, savings_monthly_cents=savings, fits_usage=ok, tradeoffs=notes))
        options.sort(key=lambda o: (not o.fits_usage, o.plan.monthly_cents, o.plan.plan_id))
        best = next((o for o in options if o.fits_usage), None)
        assumptions = [
            f"{lines} line(s); heaviest line {max_gb} GB/mo data, {max_hs} GB/mo hotspot.",
            "Service prices before taxes & fees, autopay as on the account; device installments are unchanged.",
        ]
        if best is None:
            return HouseholdOptimization(
                account_id=account.account_id if account else None,
                lines=lines,
                current_plan_id=current_plan.plan_id if current_plan else None,
                current_monthly_cents=current,
                options=options,
                recommended_plan_id=None,
                keep_current=True,
                reason="No plan in the catalog fits this usage better; keep the current plan.",
                assumptions=assumptions,
            )
        current_fits = current_plan is not None and any(
            o.fits_usage and o.plan.plan_id == current_plan.plan_id for o in options
        )
        same = current_plan is not None and best.plan.plan_id == current_plan.plan_id
        keep = same or (current is not None and current_fits and best.savings_monthly_cents <= 0)
        if keep:
            name = f" ({current_plan.name})" if current_plan else ""
            reason = f"Your current plan{name} already fits at the lowest price: keep it."
        elif current_plan is not None and not current_fits and best.savings_monthly_cents < 0:
            why = "; ".join(n.rstrip(".") for n in self.fits(current_plan, max_gb, max_hs)[1])
            reason = (
                f"{current_plan.name} doesn't fit your usage ({why}). "
                f"{best.plan.plan_name} does, for {money(-best.savings_monthly_cents)}/mo more."
            )
        elif current is None:
            reason = f"{best.plan.plan_name} is the lowest-cost plan that fits: {money(best.plan.monthly_cents)}/mo."
        else:
            reason = (
                f"{best.plan.plan_name} fits your usage and saves {money(best.savings_monthly_cents)}/mo "
                f"({money(best.savings_monthly_cents * 12)}/yr)."
            )
        return HouseholdOptimization(
            account_id=account.account_id if account else None,
            lines=lines,
            current_plan_id=current_plan.plan_id if current_plan else None,
            current_monthly_cents=current,
            options=options,
            recommended_plan_id=None if keep else best.plan.plan_id,
            keep_current=keep,
            reason=reason,
            assumptions=assumptions,
        )

    # ---- compatibility ------------------------------------------------------------------------------------------
    def compatible(
        self, device: Product, products: Mapping[Sku, Product], stock: Mapping[Sku, int]
    ) -> list[CompatibleAccessory]:
        is_phone = device.subcategory == "phones"
        out: list[CompatibleAccessory] = []
        for sku, fits in self.data.compatibility.items():
            if device.sku not in fits and not (is_phone and ANY_PHONE in fits):
                continue
            p = products.get(sku)
            if p is None:
                continue
            out.append(
                CompatibleAccessory(
                    sku=sku,
                    name=p.name,
                    subcategory=p.subcategory,
                    price_cents=p.price_cents,
                    in_stock=stock.get(sku, 0),
                    optional=True,
                    why=_why(p, device, exact=device.sku in fits),
                )
            )
        order = {"cases": 0, "screen_protectors": 1, "chargers": 2, "audio": 3}
        out.sort(key=lambda a: (order.get(a.subcategory or "", 9), a.price_cents, a.sku))
        return out

    # ---- full quote ---------------------------------------------------------------------------------------------
    def quote(self, req: QuoteRequest, products: Mapping[Sku, Product], stock: Mapping[Sku, int]) -> CostBreakdown:
        d = self.data
        errors: list[str] = []
        rows: list[_Line] = []
        ctx = self._ctx(req)
        promos = [e for e in self.promotions(req) if self._relevant(e.promo, req)]
        financing: list[FinancingQuote] = []
        stock_checks: list[StockCheck] = []
        taxable = 0
        device_promo_used: set[str] = set()
        phones = 0

        if req.plan_id is not None and req.plan_id not in self.plans:
            errors.append(f"Unknown plan {req.plan_id!r}.")
            ctx.plan = None
        if req.new_lines > req.lines:
            errors.append(f"{req.new_lines} new lines is more than the {req.lines} lines on the quote.")

        for item in req.items:
            p = products.get(item.sku)
            if p is None:
                errors.append(f"Unknown product {item.sku!r}.")
                continue
            avail = stock.get(item.sku, 0)
            stock_checks.append(StockCheck(sku=item.sku, requested=item.qty, available=avail, ok=avail >= item.qty))
            if avail < item.qty:
                errors.append(f"Only {avail} {p.name} in stock at {req.store_id} (asked for {item.qty}).")
            if p.subcategory == "phones":
                phones += item.qty
            unit = p.price_cents
            for e in promos:
                pr = e.promo
                if e.eligible and pr.kind == PromoKind.SALE and p.sku in pr.skus:
                    unit = max(0, unit - pr.credit_cents)
                    rows.append(
                        _Line(
                            QuoteLine(
                                kind="credit",
                                label=pr.name,
                                sku=p.sku,
                                promo_id=pr.promo_id,
                                upfront_cents=-min(pr.credit_cents, p.price_cents) * item.qty,
                            )
                        )
                    )
            taxable += unit * item.qty
            if item.financed:
                try:
                    fq = self.finance(
                        p.model_copy(update={"price_cents": unit}),
                        FinancingRequest(
                            sku=p.sku,
                            months=item.months,
                            down_payment_cents=item.down_payment_cents,
                            plan_id=req.plan_id,
                        ),
                        ctx,
                    )
                except ValueError as exc:
                    errors.append(str(exc))
                    continue
                financing.append(fq)
                device_promo_used.update(fq.promo_ids)
                rows.append(
                    _Line(
                        QuoteLine(
                            kind="device",
                            label=f"{p.name} ({fq.months} mo)",
                            sku=p.sku,
                            upfront_cents=fq.down_payment_cents * item.qty,
                            monthly_cents=fq.monthly_cents * item.qty,
                            note=f"{money(fq.price_cents)} retail, {money(fq.monthly_cents)}/mo",
                        ),
                        months=fq.months,
                        last_extra=(fq.last_month_cents - fq.monthly_cents) * item.qty,
                    )
                )
                for pid in fq.promo_ids:
                    pr = self.promos[pid]
                    per = min(pr.credit_cents, unit) // pr.credit_months
                    rows.append(
                        _Line(
                            QuoteLine(
                                kind="credit",
                                label=pr.name,
                                sku=p.sku,
                                promo_id=pid,
                                monthly_cents=-per * item.qty,
                                note=pr.terms,
                            ),
                            months=pr.credit_months,
                        )
                    )
            else:
                kind = "device" if self.financeable(p) else "accessory"
                rows.append(
                    _Line(
                        QuoteLine(
                            kind=kind,
                            label=p.name if item.qty == 1 else f"{p.name} x{item.qty}",
                            sku=p.sku,
                            upfront_cents=unit * item.qty,
                        )
                    )
                )
                for e in promos:
                    pr = e.promo
                    if e.eligible and pr.kind == PromoKind.ACCESSORY_BUNDLE and p.sku in pr.skus and pr.percent_off:
                        off = pct(unit, pr.percent_off * 100)
                        taxable -= off * item.qty
                        rows.append(
                            _Line(
                                QuoteLine(
                                    kind="credit",
                                    label=pr.name,
                                    sku=p.sku,
                                    promo_id=pr.promo_id,
                                    upfront_cents=-off * item.qty,
                                )
                            )
                        )

        plan_quote: PlanQuote | None = None
        tax_monthly = 0
        if ctx.plan is not None:
            try:
                plan_quote = self.quote_plan(ctx.plan.plan_id, req.lines, req.autopay, self._eligibility_for(req))
            except ValueError as exc:
                errors.append(str(exc))
            if plan_quote is not None:
                if not plan_quote.eligible:
                    errors.append(f"{plan_quote.plan_name}: {plan_quote.ineligible_reason}.")
                rows.append(
                    _Line(
                        QuoteLine(
                            kind="plan",
                            label=f"{plan_quote.plan_name}, {req.lines} line(s)",
                            monthly_cents=plan_quote.monthly_cents,
                            note="before taxes & fees"
                            if not ctx.plan.taxes_and_fees_included
                            else "taxes & fees included",
                        )
                    )
                )
                tax_monthly = plan_quote.est_taxes_fees_cents
                for e in promos:
                    pr = e.promo
                    if e.eligible and pr.kind == PromoKind.LINE_DISCOUNT:
                        rows.append(
                            _Line(
                                QuoteLine(
                                    kind="credit",
                                    label=pr.name,
                                    promo_id=pr.promo_id,
                                    monthly_cents=-pr.credit_cents,
                                    note=pr.terms,
                                )
                            )
                        )
        if req.new_lines:
            rows.append(
                _Line(
                    QuoteLine(
                        kind="fee",
                        label=f"New line connection x{req.new_lines}",
                        upfront_cents=d.new_line_fee_cents * req.new_lines,
                    )
                )
            )

        for pid in req.add_on_promo_ids:
            e = next((x for x in promos if x.promo.promo_id == pid), None)
            if e is None:
                errors.append(f"Unknown add-on {pid!r}.")
                continue
            if not e.eligible:
                errors.append(f"{e.promo.name}: {' '.join(e.reasons)}")
                continue
            count = max(phones, 1) if e.promo.kind == PromoKind.PROTECTION else 1
            label = e.promo.name if count == 1 else f"{e.promo.name} x{count}"
            rows.append(
                _Line(
                    QuoteLine(
                        kind="add_on",
                        label=label,
                        promo_id=pid,
                        monthly_cents=e.promo.monthly_cents * count,
                        note=e.promo.terms,
                    )
                )
            )

        trade: TradeInQuote | None = None
        if req.trade_in is not None:
            target = next(
                (products[i.sku] for i in req.items if i.sku in products and products[i.sku].subcategory == "phones"),
                None,
            )
            try:
                trade = self.estimate_trade_in(
                    TradeInRequest(
                        model_id=req.trade_in.model_id,
                        condition=req.trade_in.condition,
                        user_id=req.user_id,
                        target_sku=target.sku if target else None,
                        plan_id=req.plan_id,
                    ),
                    target,
                )
            except KeyError as exc:
                errors.append(str(exc).strip("'\""))
            if trade is not None:
                replaced = any(self.promos[p].kind == PromoKind.TRADE_IN_BONUS for p in device_promo_used)
                if replaced:
                    rows.append(
                        _Line(
                            QuoteLine(
                                kind="trade_in",
                                label=f"Trade-in: {trade.name} ({trade.condition.value})",
                                note="value replaced by the trade-in promo credits above",
                            )
                        )
                    )
                else:
                    rows.append(
                        _Line(
                            QuoteLine(
                                kind="trade_in",
                                label=f"Trade-in: {trade.name} ({trade.condition.value})",
                                upfront_cents=-trade.value_cents,
                                note="instant credit (estimate)",
                            )
                        )
                    )
                if trade.remaining_installments_cents:
                    rows.append(
                        _Line(
                            QuoteLine(
                                kind="fee",
                                label=f"Remaining balance on {trade.name}",
                                upfront_cents=trade.remaining_installments_cents,
                                note="paid off when traded in",
                            )
                        )
                    )

        warnings = self._compat_warnings(req, products)
        tax_upfront = pct(max(taxable, 0), d.tax_rate_bps)
        if tax_upfront:
            rows.append(
                _Line(
                    QuoteLine(
                        kind="tax",
                        label=f"Est. sales tax ({d.tax_rate_bps / 100:.2f}%)",
                        upfront_cents=tax_upfront,
                        note="on the retail price of devices + accessories",
                    )
                )
            )
        if tax_monthly:
            rows.append(_Line(QuoteLine(kind="tax", label="Est. service taxes & fees", monthly_cents=tax_monthly)))

        upfront = sum(r.line.upfront_cents for r in rows)
        monthly = sum(r.line.monthly_cents for r in rows)
        term = req.term_months
        total = upfront + sum(
            r.line.monthly_cents * (min(term, r.months) if r.months else term)
            + (r.last_extra if r.months and term >= r.months else 0)
            for r in rows
        )
        assumptions = [
            "Synthetic demo pricing: not a real T-Mobile offer, approved financing or trade-in value.",
            f"Monthly = month 1; the {term}-month total counts each installment and credit only for its own term.",
            "Promotions apply only when every condition is met; see the promotion list for why others don't.",
        ]
        return CostBreakdown(
            request=req,
            lines=[r.line for r in rows],
            plan=plan_quote,
            financing=financing,
            trade_in=trade,
            promos=promos,
            stock=stock_checks,
            upfront_cents=upfront,
            monthly_cents=monthly,
            est_tax_upfront_cents=tax_upfront,
            est_tax_monthly_cents=tax_monthly,
            total_over_term_cents=total,
            term_months=term,
            tax_rate_bps=d.tax_rate_bps,
            assumptions=assumptions,
            errors=errors,
            warnings=warnings,
        )

    def _compat_warnings(self, req: QuoteRequest, products: Mapping[Sku, Product]) -> list[str]:
        """Accessories with a fit list that matches no phone in the cart and no device on the account."""
        account = self.accounts.get(req.user_id) if req.user_id else None
        devices = {
            i.sku
            for i in req.items
            if i.sku in products and products[i.sku].subcategory in ("phones", "wearables", "tablets")
        }
        devices |= {ln.device_sku for ln in (account.lines if account else []) if ln.device_sku}
        has_phone = any(products[s].subcategory == "phones" for s in devices if s in products) or bool(
            account and account.lines
        )
        out: list[str] = []
        for item in req.items:
            fits = self.data.compatibility.get(item.sku)
            p = products.get(item.sku)
            if not fits or p is None or (ANY_PHONE in fits and has_phone) or set(fits) & devices:
                continue
            names = [products[s].name if s in products else s for s in fits if s != ANY_PHONE]
            out.append(f"{p.name} fits {', '.join(names) or 'phones'}, not the devices on this quote.")
        return out

    def _eligibility_for(self, req: QuoteRequest) -> list[str]:
        account = self.accounts.get(req.user_id) if req.user_id else None
        return list(account.eligibility) if account else []


_COND_TEXT = {
    PromoCondition.NEW_LINE: "Needs a new line.",
    PromoCondition.TRADE_IN: "Needs a trade-in.",
    PromoCondition.PORT_IN: "Needs a number ported in from another carrier.",
    PromoCondition.PLAN_PREMIUM: "Needs Premium Unlimited.",
    PromoCondition.PLAN_STANDARD_OR_BETTER: "Needs Plus or Premium Unlimited.",
    PromoCondition.FINANCED: "The device must be on monthly installments.",
    PromoCondition.WITH_DEVICE: "Needs a phone in the same purchase.",
    PromoCondition.ACTIVE: "Not active today.",
}


def _why(acc: Product, device: Product, exact: bool) -> str:
    short = device.name.removeprefix("Apple ").removeprefix("Samsung ").removeprefix("Google ")
    sub = acc.subcategory
    if sub == "cases":
        return f"Made for the {short}; protects it from drops."
    if sub == "screen_protectors":
        return f"Cut for the {short} screen."
    if sub == "chargers":
        if exact:
            return f"Designed for the {short}."
        return "USB-C; phones ship without a charger. Skip it if you already have one."
    if sub == "audio":
        return "Bluetooth: works with any phone. Optional."
    return "Optional."
