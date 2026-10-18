"""Fetch real ingredient lists for every product in data/products.yaml from Open Food Facts -> data/ingredients.yaml.

Our products are fictional brands, so each SKU is matched to a real, similar product: search
(search.openfoodfacts.org) by the product's name without its size, then read candidates
(world.openfoodfacts.org/api/v2/product/<code>) until one has English ingredients. Every entry keeps the matched
product and its URL as the source. Fresh single-ingredient products (produce, plain meat, eggs) that have no
label are recorded as themselves with source "fresh, single ingredient". Requests are paced (Open Food Facts asks
for <= 10 searches / min). Data: Open Food Facts, ODbL (https://world.openfoodfacts.org/data), attributed in the file.

    uv run python scripts/fetch_ingredients.py [--only SKU,SKU] [--pause 6.5]
"""

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utils.data import load_products  # noqa: E402

OUT = ROOT / "data/ingredients.yaml"
UA = "PickyHackathon/0.1 (+https://github.com/adsdemaybe/picky)"
SEARCH = "https://search.openfoodfacts.org/search?q={q}&page_size=12&fields=code,product_name,brands"
PRODUCT = "https://world.openfoodfacts.org/api/v2/product/{code}?fields=product_name,brands,ingredients_text_en,allergens_tags"
FRESH_CATEGORIES = {"produce", "meat"}  # plain produce / raw meat & fish: the item is its own ingredient
QUERY_OVERRIDES = {  # names our fictional catalog words differently from real labels
    "CHEESE-COTIJA": "cotija cheese",
    "BUTTER": "salted butter",
    "POPCORN": "microwave butter popcorn",
    "COLA-CANE": "mexican cola cane sugar",
    "LAGER-6PK": "mexican lager beer",
    "GUAC-KIT": "guacamole",
}
NONFOOD_CATEGORIES = {"wireless", "household", "personal_care"}  # no food label to match
MIN_OVERLAP = 0.5  # share of the query's words the matched product's name must contain
FRESH_WORDS = re.compile(
    r"\b(lime|lemon|avocado|banana|apple|onion|garlic|cilantro|tomato|pepper|carrot|potato|lettuce"
    r"|berries|strawberr|grape|orange|cucumber|broccoli|spinach|egg|eggs)\b",
    re.I,
)


def _get(url: str) -> dict | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001 - 503s happen; back off
            time.sleep(5 * (attempt + 1))
    return None


def _query(name: str) -> str:
    q = re.sub(r"\b\d+(\.\d+)?\s*(oz|fl oz|lb|lbs|ct|g|kg|ml|l|gal|gallon|pack|pk|count)\b", " ", name, flags=re.I)
    q = re.sub(r"\b(half gallon|1 gal)\b", " ", q, flags=re.I)
    return re.sub(r"\s+", " ", q).strip()


def _split(text: str) -> list[str]:
    """Top-level ingredients (commas outside parentheses), tidied."""
    out, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    out.append(cur)
    clean = []
    for s in out:
        s = re.sub(r"\s+", " ", s).strip(" .;:*_")
        if s:
            clean.append(s[:1].upper() + s[1:].lower() if s.isupper() else s)
    return clean[:40]


def _words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", s.lower()) if len(w) > 2}


def _close(want: set[str], name: str) -> bool:
    return bool(want) and len(want & _words(name)) / len(want) >= MIN_OVERLAP


def match(product, pause: float) -> dict | None:
    q = QUERY_OVERRIDES.get(product.sku) or _query(product.name)
    found = _get(SEARCH.format(q=urllib.parse.quote(q)))
    time.sleep(pause)
    if not found:
        return None
    want = _words(q)
    hits = sorted(found.get("hits", []), key=lambda h: -len(want & _words(h.get("product_name") or "")))
    for h in hits[:6]:
        code = h.get("code")
        if not code or not _close(want, h.get("product_name") or ""):
            continue
        rec = _get(PRODUCT.format(code=code))
        time.sleep(pause / 4)
        p = (rec or {}).get("product") or {}
        text = (p.get("ingredients_text_en") or "").strip()
        if len(text) < 3:
            continue
        brands = p.get("brands") or ""
        return {
            "ingredients": _split(text),
            "ingredients_text": text,
            "allergens": [a.split(":", 1)[-1] for a in p.get("allergens_tags") or []],
            "matched_product": f"{brands} {p.get('product_name') or ''}".strip(),
            "source": "Open Food Facts",
            "source_url": f"https://world.openfoodfacts.org/product/{code}",
        }
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    ap.add_argument("--pause", type=float, default=6.5, help="seconds between searches")
    ap.add_argument(
        "--prune", action="store_true", help="drop non-food and weak matches already in the file, then exit"
    )
    args = ap.parse_args()
    products = load_products()
    data = yaml.safe_load(OUT.read_text()) if OUT.exists() else {}
    entries: dict = data.get("ingredients", {}) if data else {}
    if args.prune:
        drop = [
            s
            for s, e in entries.items()
            if s not in products
            or products[s].category in NONFOOD_CATEGORIES
            or (
                e.get("matched_product")
                and not _close(_words(QUERY_OVERRIDES.get(s) or _query(products[s].name)), e["matched_product"])
            )
        ]
        for s in drop:
            print(f"pruned {s}: {entries[s].get('matched_product')}")
            entries.pop(s)
        OUT.write_text(_dump(entries))
        print(f"{len(entries)} entries kept")
        return 0
    only = {s.strip() for s in args.only.split(",") if s.strip()}
    for sku, p in products.items():
        if only and sku not in only:
            continue
        if sku in entries and not only:
            continue  # resume
        if p.category in NONFOOD_CATEGORIES:
            continue
        single = p.category == "meat" or (
            (p.category in FRESH_CATEGORIES or p.perishable) and FRESH_WORDS.search(p.name) and len(_words(p.name)) <= 3
        )
        hit = None if single else match(p, args.pause)
        if hit is None and (single or p.category in FRESH_CATEGORIES):
            hit = {
                "ingredients": [_query(p.name)],
                "ingredients_text": _query(p.name),
                "allergens": [],
                "matched_product": None,
                "source": "fresh, single ingredient",
                "source_url": None,
            }
        if hit is None:
            print(f"{sku}: no match", flush=True)
            continue
        entries[sku] = hit
        print(
            f"{sku}: {hit['source']} {hit.get('matched_product') or ''} ({len(hit['ingredients'])} ingredients)",
            flush=True,
        )
        OUT.write_text(_dump(entries))
    OUT.write_text(_dump(entries))
    print(f"done: {len(entries)}/{len(products)} products have ingredients", flush=True)
    return 0


def _dump(entries: dict) -> str:
    head = (
        "# Ingredient lists per SKU (generated by scripts/fetch_ingredients.py; re-run, don't hand-edit).\n"
        "# Our brands are fictional, so each SKU is matched to a real similar product; source_url is that product.\n"
        "# Data from Open Food Facts (https://world.openfoodfacts.org), licensed ODbL 1.0; contents DbCL 1.0.\n"
    )
    return head + yaml.safe_dump({"ingredients": entries}, sort_keys=True, allow_unicode=True, width=120)


if __name__ == "__main__":
    sys.exit(main())
