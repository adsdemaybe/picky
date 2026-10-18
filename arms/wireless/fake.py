"""Deterministic fake WirelessService: the same engine and data/wireless.yaml, with prices from data/products.yaml and
stock from the store's planogram as shipped (no live stock changes). Base deps only."""

from __future__ import annotations

from collections.abc import Iterable

from arms.wireless.data import load_wireless
from arms.wireless.service import BaseWirelessService
from contracts.catalog import Product
from contracts.common import Sku, StoreId
from contracts.services import Services
from contracts.wireless import WirelessFile
from utils.data import DataError, load_planogram, load_products


class FakeWirelessService(BaseWirelessService):
    def __init__(self, services: Services | None = None, data: WirelessFile | None = None) -> None:
        super().__init__(data or load_wireless())
        self.services = services
        self._catalog = load_products()

    async def _products(self, skus: Iterable[Sku]) -> dict[Sku, Product]:
        return {s: self._catalog[s] for s in skus if s in self._catalog}

    async def _stock(self, skus: Iterable[Sku], store_id: StoreId) -> dict[Sku, int]:
        try:
            plan = load_planogram(store_id)
        except DataError:
            return {s: 0 for s in skus}
        want = set(skus)
        out = {s: 0 for s in want}
        for slot in plan.slots:
            if slot.sku in want:
                out[slot.sku] += slot.qty
        return out
