"""Category taxonomy helpers shared by the fake and the Postgres catalog (pure functions, base deps only).

The taxonomy is ``data/categories.yaml`` (``contracts.planogram.CategoriesFile``). It is merged with what the
products actually use, so a product whose category / subcategory is missing from the file (or a missing file while the
data agent is still writing it) still shows up in ``GET /api/categories``: missing top-level ids get a display name
from ``DISPLAY_NAMES`` (else a title-cased id); missing subcategories hang under the product's category.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path

from contracts.catalog import STANDARD_CATEGORY_IDS, Product
from contracts.common import CategoryId
from contracts.planogram import CategoryDef
from utils import data as data_io
from utils.log import get_logger

log = get_logger(__name__)

DISPLAY_NAMES: dict[str, str] = {
    "produce": "Produce",
    "dairy": "Dairy & Eggs",
    "meat": "Meat & Seafood",
    "deli": "Deli & Prepared",
    "bakery": "Bakery",
    "pantry": "Pantry",
    "international": "International & Mexican",
    "snacks": "Snacks",
    "beverages": "Beverages",
    "frozen": "Frozen",
    "household": "Household",
    "personal_care": "Personal Care",
    "alcohol": "Beer & Wine",
}
"""Fallback display names (data/categories.yaml wins when present)."""

UNCATEGORIZED = "uncategorized"
"""StockLevel.category for a slot whose SKU is missing from products.yaml (should never happen with valid data)."""

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NUM_RE = re.compile(r"(\d+)")


def display_name(category_id: str) -> str:
    return DISPLAY_NAMES.get(category_id) or category_id.replace("_", " ").title()


def natural_key(label: str) -> tuple[object, ...]:
    """Sort 'Aisle 2' before 'Aisle 10'."""
    return tuple(int(part) if part.isdigit() else part.lower() for part in _NUM_RE.split(label))


def category_matches(product: Product, category_id: CategoryId) -> bool:
    """A product is in ``category_id`` when it is its category OR its subcategory (case-insensitive)."""
    cid = category_id.strip().lower()
    return cid in {product.category.lower(), (product.subcategory or "").lower()}


def load_taxonomy(data_root: str | Path | None, products: Iterable[Product]) -> list[CategoryDef]:
    """Flat taxonomy in display order: each top-level category (by ``sort``, then id) followed by its subcategories
    (by ``sort``, then name). File entries win; categories only the products use are appended."""
    try:
        defs = list(data_io.load_categories_file(data_root).categories)
    except data_io.DataError as exc:
        log.warning("no categories.yaml (%s); deriving the taxonomy from products", exc)
        defs = []
    except Exception as exc:  # a malformed file must not take the catalog down
        log.error("categories.yaml is invalid (%s); deriving the taxonomy from products", exc)
        defs = []
    return merge_taxonomy(defs, products)


def merge_taxonomy(defs: Iterable[CategoryDef], products: Iterable[Product]) -> list[CategoryDef]:
    """``defs`` (file / DB rows) + categories only the products use, in display order (see ``load_taxonomy``)."""
    by_id: dict[str, CategoryDef] = {c.category_id: c for c in defs}

    def standard_sort(cid: str) -> int:
        return (STANDARD_CATEGORY_IDS.index(cid) + 1) * 10 if cid in STANDARD_CATEGORY_IDS else 1000

    for p in products:
        top = p.category
        if top not in by_id and _ID_RE.match(top):
            by_id[top] = CategoryDef(category_id=top, name=display_name(top), sort=standard_sort(top))
        sub = p.subcategory
        if sub and sub not in by_id and _ID_RE.match(sub) and top in by_id and by_id[top].parent_id is None:
            by_id[sub] = CategoryDef(category_id=sub, name=display_name(sub), parent_id=top, sort=1000)

    tops = sorted((c for c in by_id.values() if c.parent_id is None), key=lambda c: (c.sort, c.category_id))
    out: list[CategoryDef] = []
    for top in tops:
        out.append(top)
        out.extend(
            sorted(
                (c for c in by_id.values() if c.parent_id == top.category_id), key=lambda c: (c.sort, c.name.lower())
            )
        )
    return out
