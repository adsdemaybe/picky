"""WirelessService over the pricing engine. Products and stock come from ``services.catalog`` at call time, so the
prices and shelf quantities the web app shows for store_tmobile are the catalog's (one source of truth); robot picks
and restocks that change catalog stock show up in the next quote."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

from arms.wireless.data import load_wireless
from arms.wireless.engine import WirelessEngine
from contracts.catalog import Product
from contracts.common import Sku, StoreId, UserId
from contracts.services import Services
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
    WirelessFile,
)
from utils.log import get_logger

log = get_logger(__name__)


class BaseWirelessService:
    """Shared WirelessService; subclasses supply ``_products`` / ``_stock``."""

    def __init__(self, data: WirelessFile) -> None:
        self.engine = WirelessEngine(data)

    async def _products(self, skus: Iterable[Sku]) -> dict[Sku, Product]:
        raise NotImplementedError

    async def _stock(self, skus: Iterable[Sku], store_id: StoreId) -> dict[Sku, int]:
        raise NotImplementedError

    async def _product(self, sku: Sku) -> Product:
        p = (await self._products([sku])).get(sku)
        if p is None:
            raise KeyError(f"unknown product {sku!r}")
        return p

    async def info(self) -> WirelessCatalogInfo:
        return self.engine.info()

    async def quote_plan(
        self, plan_id: str, lines: int, autopay: bool = True, eligibility: list[str] | None = None
    ) -> PlanQuote:
        return self.engine.quote_plan(plan_id, lines, autopay, eligibility)

    async def compare_plans(
        self, lines: int, autopay: bool = True, eligibility: list[str] | None = None
    ) -> list[PlanQuote]:
        return self.engine.compare_plans(lines, autopay, eligibility)

    async def compatible_accessories(
        self, device_sku: Sku, store_id: StoreId = "store_tmobile"
    ) -> list[CompatibleAccessory]:
        device = await self._product(device_sku)
        skus = list(self.engine.data.compatibility)
        products, stock = await asyncio.gather(self._products(skus), self._stock(skus, store_id))
        return self.engine.compatible(device, products, stock)

    async def estimate_trade_in(self, request: TradeInRequest) -> TradeInQuote:
        target = await self._product(request.target_sku) if request.target_sku else None
        return self.engine.estimate_trade_in(request, target)

    async def finance(self, request: FinancingRequest) -> FinancingQuote:
        return self.engine.finance(await self._product(request.sku), request)

    async def promotions(self, request: QuoteRequest) -> list[PromoEligibility]:
        return self.engine.promotions(request)

    async def get_account(self, user_id: UserId) -> WirelessAccount | None:
        return self.engine.account(user_id)

    async def list_accounts(self) -> list[WirelessAccount]:
        return list(self.engine.data.accounts)

    async def optimize_household(self, request: HouseholdRequest) -> HouseholdOptimization:
        return self.engine.optimize_household(request)

    async def quote(self, request: QuoteRequest) -> CostBreakdown:
        skus = [i.sku for i in request.items]
        fits = [f for s in skus for f in self.engine.data.compatibility.get(s, [])]  # names for compatibility warnings
        products, stock = await asyncio.gather(self._products([*skus, *fits]), self._stock(skus, request.store_id))
        return self.engine.quote(request, products, stock)


class WirelessService(BaseWirelessService):
    """Real: catalog-backed prices and live stock (``services.catalog``)."""

    def __init__(self, services: Services, data: WirelessFile | None = None) -> None:
        super().__init__(data or load_wireless())
        self.services = services

    async def _products(self, skus: Iterable[Sku]) -> dict[Sku, Product]:
        catalog = self.services.require("catalog")
        uniq = list(dict.fromkeys(skus))
        found = await asyncio.gather(*(catalog.get_product(s) for s in uniq))
        return {s: p for s, p in zip(uniq, found, strict=True) if p is not None}

    async def _stock(self, skus: Iterable[Sku], store_id: StoreId) -> dict[Sku, int]:
        catalog = self.services.require("catalog")
        uniq = list(dict.fromkeys(skus))
        locs = await asyncio.gather(*(catalog.get_locations(s, store_id) for s in uniq), return_exceptions=True)
        out: dict[Sku, int] = {}
        for sku, res in zip(uniq, locs, strict=True):
            if isinstance(res, BaseException):
                log.warning("stock lookup failed for %s in %s: %s", sku, store_id, res)
                out[sku] = 0
            else:
                out[sku] = sum(loc.qty for loc in res)
        return out
