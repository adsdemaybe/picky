"""Wireless retail routes (contracts 0.2.3) for the T-Mobile-style store: thin pass-throughs to ``services.wireless``.

All math is in arms/wireless (deterministic); these routes only validate input and map errors (KeyError -> 404,
ValueError -> 409, validation -> 422, via the app's handlers). Every response is synthetic demo pricing.
"""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from contracts.wireless import (
    CompatibleAccessory,
    CostBreakdown,
    FinancingQuote,
    FinancingRequest,
    HouseholdOptimization,
    HouseholdRequest,
    PlanQuote,
    PromoEligibility,
    QuoteRequest,
    TradeInQuote,
    TradeInRequest,
    WirelessAccount,
    WirelessCatalogInfo,
)


def mount_wireless_routes(app: FastAPI, reg: Callable[[], Any]) -> None:
    """Add /api/wireless/* to ``app``; ``reg()`` returns the live ``Registry`` (503 before startup)."""

    def wireless() -> Any:
        s = getattr(reg().services, "wireless", None)
        if s is None:
            raise HTTPException(503, "wireless service not wired")
        return s

    async def eligibility(user_id: str | None) -> list[str]:
        if not user_id:
            return []
        account = await wireless().get_account(user_id)
        return list(account.eligibility) if account else []

    @app.get("/api/wireless", response_model=WirelessCatalogInfo)
    async def wireless_info() -> WirelessCatalogInfo:
        return await wireless().info()

    @app.get("/api/wireless/plans", response_model=list[PlanQuote])
    async def wireless_plans(lines: int = Query(1, ge=1, le=12), autopay: bool = True, user_id: str | None = None):
        return await wireless().compare_plans(lines, autopay, await eligibility(user_id))

    @app.get("/api/wireless/plans/{plan_id}/quote", response_model=PlanQuote)
    async def wireless_plan_quote(
        plan_id: str, lines: int = Query(1, ge=1, le=12), autopay: bool = True, user_id: str | None = None
    ) -> PlanQuote:
        return await wireless().quote_plan(plan_id, lines, autopay, await eligibility(user_id))

    @app.get("/api/wireless/compatible/{device_sku}", response_model=list[CompatibleAccessory])
    async def wireless_compatible(device_sku: str, store_id: str = "store_tmobile") -> list[CompatibleAccessory]:
        return await wireless().compatible_accessories(device_sku, store_id)

    @app.post("/api/wireless/trade-in", response_model=TradeInQuote)
    async def wireless_trade_in(body: TradeInRequest) -> TradeInQuote:
        return await wireless().estimate_trade_in(body)

    @app.post("/api/wireless/financing", response_model=FinancingQuote)
    async def wireless_financing(body: FinancingRequest) -> FinancingQuote:
        return await wireless().finance(body)

    @app.post("/api/wireless/promotions", response_model=list[PromoEligibility])
    async def wireless_promotions(body: QuoteRequest) -> list[PromoEligibility]:
        return await wireless().promotions(body)

    @app.get("/api/wireless/accounts", response_model=list[WirelessAccount])
    async def wireless_accounts() -> list[WirelessAccount]:
        return await wireless().list_accounts()

    @app.get("/api/wireless/accounts/{user_id}", response_model=WirelessAccount)
    async def wireless_account(user_id: str) -> WirelessAccount:
        account = await wireless().get_account(user_id)
        if account is None:
            raise HTTPException(404, f"no wireless account for {user_id}")
        return account

    @app.post("/api/wireless/household", response_model=HouseholdOptimization)
    async def wireless_household(body: HouseholdRequest) -> HouseholdOptimization:
        return await wireless().optimize_household(body)

    @app.post("/api/wireless/quote", response_model=CostBreakdown)
    async def wireless_quote(body: QuoteRequest) -> CostBreakdown:
        return await wireless().quote(body)
