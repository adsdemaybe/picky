"""Render (and optionally apply) the Postgres seed from ``data/`` (planogram = source of truth, CLAUDE.md §4 rule 5).

    uv run python -m arms.catalog.seed                       # SQL to stdout
    uv run python -m arms.catalog.seed --out build/seed.sql  # SQL to a file
    uv run python -m arms.catalog.seed --schema --apply      # schema.sql + seed, executed on DATABASE_URL
                                                             # (needs `uv sync --extra catalog` for psycopg)

The seed TRUNCATEs the tables it fills (catalog + demo users) and re-inserts everything in one transaction, so it is
idempotent. Slot deals get ``deal_id = f"{store_id}:{slot_id}"``; online item deals ``f"online:{sku}"``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from contracts.catalog import DealTerms
from contracts.common import ONLINE_STORE_ID
from contracts.planogram import FixturesFile, OnlineCatalog, Planogram
from utils import data as data_io
from utils.log import get_logger

log = get_logger(__name__)

SCHEMA_SQL = Path(__file__).resolve().parents[2] / "contracts" / "db" / "schema.sql"

# Truncated (CASCADE) before re-inserting; order irrelevant with CASCADE.
SEEDED_TABLES = (
    "stores",
    "products",
    "slots",
    "inventory",
    "lots",
    "online_items",
    "deals",
    "users",
    "profiles",
    "mandates",
    "purchases",
    "feedback",
)


# ----------------------------------------------------------------------------------------------------------- literals
class Raw(str):
    """Already-rendered SQL (no quoting)."""


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def lit(value: Any) -> str:
    """Python value -> SQL literal. Lists -> text[]; dicts / models -> jsonb; datetimes -> timestamptz."""
    if isinstance(value, Raw):
        return str(value)
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, datetime):
        return f"{_quote(value.isoformat())}::timestamptz"
    if isinstance(value, date):
        return f"{_quote(value.isoformat())}::date"
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, BaseModel):
        return f"{_quote(value.model_dump_json())}::jsonb"
    if isinstance(value, dict):
        return f"{_quote(json.dumps(value, sort_keys=True, default=str))}::jsonb"
    if isinstance(value, list | tuple):
        if all(isinstance(v, str) for v in value):
            return "ARRAY[" + ", ".join(_quote(v) for v in value) + "]::text[]" if value else "'{}'::text[]"
        return f"{_quote(json.dumps(list(value), default=str))}::jsonb"
    raise TypeError(f"cannot render {type(value).__name__} as SQL")


def insert(table: str, row: dict[str, Any]) -> str:
    cols = ", ".join(row)
    vals = ", ".join(lit(v) for v in row.values())
    return f"INSERT INTO {table} ({cols}) VALUES ({vals});"


def _jsonb_list(models: Sequence[BaseModel]) -> Raw:
    return Raw(f"{_quote(json.dumps([m.model_dump(mode='json') for m in models]))}::jsonb")


# ----------------------------------------------------------------------------------------------------------- rows
def _deal_row(deal_id: str, store_id: str, sku: str, terms: DealTerms) -> dict[str, Any]:
    return {
        "deal_id": deal_id,
        "store_id": store_id,
        "sku": sku,
        "kind": terms.kind,
        "price_cents": terms.price_cents,
        "original_cents": terms.original_cents,
        "percent_off": terms.percent_off,
        "reason": terms.reason,
        "lot_id": terms.lot_id,
        "valid_until": terms.valid_until,
    }


def store_statements(plan: Planogram) -> list[str]:
    sid = plan.store_id
    out = [
        insert(
            "stores",
            {
                "store_id": sid,
                "name": plan.name,
                "kind": "physical",
                "address": plan.address,
                "width_m": plan.bounds.w,
                "depth_m": plan.bounds.h,
                "planogram": Raw(f"{_quote(plan.model_dump_json())}::jsonb"),
            },
        )
    ]
    for s in plan.slots:
        fixture = plan.fixture(s.fixture_id)
        aisle_id = s.aisle_id or (fixture.aisle_id if fixture else None)
        aisle = plan.aisle(aisle_id) if aisle_id else None
        out.append(
            insert(
                "slots",
                {
                    "store_id": sid,
                    "slot_id": s.slot_id,
                    "sku": s.sku,
                    "fixture_id": s.fixture_id,
                    "aisle_id": aisle_id,
                    "aisle_label": aisle.label if aisle else None,
                    "level": s.level,
                    "idx": s.index,
                    "item_x": s.item_position.x,
                    "item_y": s.item_position.y,
                    "item_z": s.item_position.z,
                    "approach_x": s.approach_pose.x,
                    "approach_y": s.approach_pose.y,
                    "approach_theta": s.approach_pose.theta,
                    "facings": s.facings,
                    "capacity": s.capacity,
                    "price_cents": s.price_cents,
                    "shelf_tag_cents": s.shelf_tag_cents,
                },
            )
        )
        out.append(insert("inventory", {"store_id": sid, "slot_id": s.slot_id, "sku": s.sku, "qty": s.qty}))
        for lot in s.lots:
            out.append(
                insert(
                    "lots",
                    {
                        "store_id": sid,
                        "slot_id": s.slot_id,
                        "lot_id": lot.lot_id,
                        "sku": s.sku,
                        "qty": lot.qty,
                        "expiry": lot.expiry,
                        "freshness": lot.freshness,
                    },
                )
            )
        if s.deal is not None:
            out.append(insert("deals", _deal_row(f"{sid}:{s.slot_id}", sid, s.sku, s.deal)))
    return out


def online_statements(online: OnlineCatalog) -> list[str]:
    out = [
        insert(
            "stores",
            {
                "store_id": online.store_id or ONLINE_STORE_ID,
                "name": online.name,
                "kind": "online",
                "planogram": None,
                "shipping_cents": online.shipping_cents,
                "free_shipping_over_cents": online.free_shipping_over_cents,
                "default_eta_hours": online.default_eta_hours,
            },
        )
    ]
    for item in online.items:
        out.append(
            insert(
                "online_items",
                {"sku": item.sku, "price_cents": item.price_cents, "qty": item.qty, "eta_hours": item.eta_hours},
            )
        )
        if item.deal is not None:
            deal_id = f"{ONLINE_STORE_ID}:{item.sku}"
            out.append(insert("deals", _deal_row(deal_id, ONLINE_STORE_ID, item.sku, item.deal)))
    return out


def user_statements(fixtures: FixturesFile) -> list[str]:
    out: list[str] = []
    for u in fixtures.users:
        p, m = u.profile, u.mandate
        out.append(
            insert("users", {"user_id": u.user_id, "display_name": p.display_name, "phone": p.phone, "email": p.email})
        )
        out.append(
            insert(
                "profiles",
                {
                    "user_id": u.user_id,
                    "home_store_id": p.home_store_id,
                    "household_size": p.household_size,
                    "prefs": p.prefs,
                    "dislikes": p.dislikes,
                    "disliked_skus": p.disliked_skus,
                    "favorite_skus": p.favorite_skus,
                    "dietary": p.dietary,
                    "allergies": p.allergies,
                    "default_budget_cents": p.default_budget_cents,
                    "taste_vector": Raw(f"{_quote(json.dumps(p.taste_vector))}::vector") if p.taste_vector else None,
                    "version": p.version,
                },
            )
        )
        out.append(
            insert(
                "mandates",
                {
                    "mandate_id": m.mandate_id,
                    "user_id": m.user_id,
                    "cap_cents": m.cap_cents,
                    "per_item_cap_cents": m.per_item_cap_cents,
                    "ask_above_cents": m.ask_above_cents,
                    "allowed_categories": m.allowed_categories,
                    "blocked_categories": m.blocked_categories,
                    "allowed_merchants": m.allowed_merchants,
                    "allow_online": m.allow_online,
                    "require_checkout_approval": m.require_checkout_approval,
                    "expires_at": m.expires_at,
                    "revoked": m.revoked,
                    "signature": m.signature,
                    "created_at": m.created_at,
                },
            )
        )
        for pr in u.purchases:
            out.append(
                insert(
                    "purchases",
                    {
                        "purchase_id": pr.purchase_id,
                        "user_id": pr.user_id,
                        "trip_id": pr.trip_id,
                        "sku": pr.sku,
                        "qty": pr.qty,
                        "unit_price_cents": pr.unit_price_cents,
                        "store_id": pr.store_id,
                        "rating": pr.rating,
                        "ts": pr.ts,
                    },
                )
            )
        for fb in u.feedback:
            out.append(
                insert(
                    "feedback",
                    {
                        "feedback_id": fb.feedback_id,
                        "user_id": fb.user_id,
                        "trip_id": fb.trip_id,
                        "sku": fb.sku,
                        "rating": fb.rating,
                        "text": fb.text,
                        "channel": fb.channel,
                        "tags": fb.tags,
                        "ts": fb.ts,
                    },
                )
            )
    return out


def render_seed(data_root: str | Path | None = None, include_schema: bool = False, include_users: bool = True) -> str:
    """The full seed script (one transaction). Raises utils.data.DataError if a required data file is missing."""
    products = data_io.load_products_file(data_root)
    plans = data_io.load_planograms(data_root)
    try:
        online: OnlineCatalog | None = data_io.load_online_catalog(data_root)
    except data_io.DataError as exc:
        log.warning("seed: skipping online store (%s)", exc)
        online = None
    fixtures: FixturesFile | None = None
    if include_users:
        try:
            fixtures = data_io.load_demo_fixtures(data_root)
        except data_io.DataError as exc:
            log.warning("seed: skipping demo users (%s)", exc)

    lines: list[str] = ["-- Generated by `python -m arms.catalog.seed` from data/. Do not edit; fix the data."]
    if include_schema:
        lines += [SCHEMA_SQL.read_text(encoding="utf-8"), ""]
    lines += ["BEGIN;", f"TRUNCATE {', '.join(SEEDED_TABLES)} CASCADE;", "", "-- products"]
    for p in products.products:
        row = p.model_dump(exclude={"asset"})
        row["asset"] = p.asset
        lines.append(insert("products", row))
    for sid, plan in plans.items():
        lines += ["", f"-- store {sid}", *store_statements(plan)]
    if online is not None:
        lines += ["", "-- online store", *online_statements(online)]
    if fixtures is not None:
        lines += ["", "-- demo users", *user_statements(fixtures)]
    lines += ["COMMIT;", ""]
    return "\n".join(lines)


# ----------------------------------------------------------------------------------------------------------- apply
def psycopg_url(database_url: str) -> str:
    """SQLAlchemy URL -> libpq URL ('postgresql+psycopg://' -> 'postgresql://')."""
    scheme, sep, rest = database_url.partition("://")
    return f"{scheme.split('+', 1)[0]}{sep}{rest}"


def apply_sql(sql: str, database_url: str) -> None:
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - extra not installed
        raise SystemExit("psycopg not installed: run `uv sync --extra catalog`") from exc
    with psycopg.connect(psycopg_url(database_url), connect_timeout=5, autocommit=True) as conn:
        conn.execute(sql)  # multi-statement script; BEGIN/COMMIT inside keeps it atomic


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m arms.catalog.seed", description=__doc__.splitlines()[0])
    ap.add_argument("--out", help="write SQL to this file instead of stdout")
    ap.add_argument("--data-dir", help="data directory (default: PICKY_DATA_DIR)")
    ap.add_argument("--schema", action="store_true", help="prepend contracts/db/schema.sql")
    ap.add_argument("--no-users", action="store_true", help="skip demo users / profiles / mandates")
    ap.add_argument("--apply", action="store_true", help="execute on DATABASE_URL (needs the catalog extra)")
    args = ap.parse_args(list(argv) if argv is not None else None)

    try:
        sql = render_seed(args.data_dir, include_schema=args.schema, include_users=not args.no_users)
    except data_io.DataError as exc:
        print(f"seed: {exc}", file=sys.stderr)
        return 1
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(sql, encoding="utf-8")
        print(f"seed: wrote {out} ({sql.count('INSERT INTO')} inserts)", file=sys.stderr)
    elif not args.apply:
        sys.stdout.write(sql)
    if args.apply:
        from utils.config import get_settings

        url = get_settings().database_url
        try:
            apply_sql(sql, url)
        except SystemExit:
            raise
        except Exception as exc:  # unreachable DB, auth, SQL error
            print(f"seed: apply failed on {psycopg_url(url).split('@')[-1]}: {exc}", file=sys.stderr)
            return 2
        print(f"seed: applied {sql.count('INSERT INTO')} inserts", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
