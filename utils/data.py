"""Loaders + validators for the data files under ``PICKY_DATA_DIR`` (default ``<repo>/data``) -> contracts models.

    data/products.yaml                -> ProductsFile      (load_products / load_products_file)
    data/planograms/<store_id>.yaml   -> Planogram         (load_planogram / list_store_ids)
    data/online_store.yaml            -> OnlineCatalog     (load_online_catalog)
    data/fixtures/users.yaml          -> FixturesFile      (load_demo_fixtures / load_demo_user)

Results are cached per (file, mtime), so edits are picked up without a restart. Every call returns a deep copy, so
callers may mutate what they get (e.g. stock levels) without corrupting the cache.

Run ``python -m utils.data`` to validate the data directory (cross-file + physical-consistency checks). Problems
prefixed ``warning:`` don't fail the run.
"""

import math
import sys
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from contracts.catalog import Product, is_valid_ean13
from contracts.common import ONLINE_STORE_ID, ROBOT_RADIUS_M, Pose2D, Sku, StoreId, UserId, angle_diff, utcnow
from contracts.planogram import DemoUser, FixturesFile, OnlineCatalog, Planogram, ProductsFile
from utils.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

PRODUCTS_FILE = "products.yaml"
PLANOGRAM_DIR = "planograms"
ONLINE_FILE = "online_store.yaml"
USERS_FILE = "fixtures/users.yaml"

M = TypeVar("M", bound=BaseModel)


class DataError(Exception):
    """A data file is missing, unparsable or fails contract validation."""


def data_dir(override: str | Path | None = None) -> Path:
    """Resolve the data directory: explicit override > ``settings.data_dir`` (PICKY_DATA_DIR from env/.env, default
    'data'). Relative paths are repo-relative."""
    if override is None:
        return get_settings().data_dir
    path = Path(override).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError as exc:
        raise DataError(f"data file not found: {path}") from exc


@lru_cache(maxsize=64)
def _load_cached(path_str: str, mtime_ns: int, model: type[BaseModel], wrap_key: str | None) -> BaseModel:
    path = Path(path_str)
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DataError(f"{path}: invalid YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if wrap_key is not None and isinstance(raw, list):  # tolerate a bare top-level list
        raw = {wrap_key: raw}
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        raise DataError(f"{path}: does not match contracts.{model.__name__}:\n{exc}") from exc


def _load(path: Path, model: type[M], wrap_key: str | None = None) -> M:
    obj = _load_cached(str(path.resolve()), _mtime(path), model, wrap_key)
    return obj.model_copy(deep=True)  # type: ignore[return-value]


def clear_cache() -> None:
    _load_cached.cache_clear()


# --------------------------------------------------------------------------------------------------------------------
# Loaders
# --------------------------------------------------------------------------------------------------------------------
def load_products_file(data_root: str | Path | None = None) -> ProductsFile:
    return _load(data_dir(data_root) / PRODUCTS_FILE, ProductsFile, "products")


def load_products(data_root: str | Path | None = None) -> dict[Sku, Product]:
    """All products keyed by SKU (file order preserved)."""
    return load_products_file(data_root).by_sku()


def list_store_ids(data_root: str | Path | None = None, include_online: bool = False) -> list[StoreId]:
    """Physical store ids (one per data/planograms/*.yaml), sorted; optionally followed by 'online'."""
    pdir = data_dir(data_root) / PLANOGRAM_DIR
    ids = sorted(p.stem for p in pdir.glob("*.yaml")) if pdir.is_dir() else []
    if include_online and (data_dir(data_root) / ONLINE_FILE).exists():
        ids.append(ONLINE_STORE_ID)
    return ids


def load_planogram(store_id: StoreId, data_root: str | Path | None = None) -> Planogram:
    path = data_dir(data_root) / PLANOGRAM_DIR / f"{store_id}.yaml"
    if not path.exists():
        known = ", ".join(list_store_ids(data_root)) or "none"
        raise DataError(f"no planogram for store {store_id!r} at {path} (known: {known})")
    plan = _load(path, Planogram)
    if plan.store_id != store_id:
        raise DataError(f"{path}: store_id {plan.store_id!r} does not match file name {store_id!r}")
    return plan


def load_planograms(data_root: str | Path | None = None) -> dict[StoreId, Planogram]:
    return {sid: load_planogram(sid, data_root) for sid in list_store_ids(data_root)}


def load_online_catalog(data_root: str | Path | None = None) -> OnlineCatalog:
    return _load(data_dir(data_root) / ONLINE_FILE, OnlineCatalog, "items")


def load_demo_fixtures(data_root: str | Path | None = None) -> FixturesFile:
    return _load(data_dir(data_root) / USERS_FILE, FixturesFile, "users")


def load_demo_user(user_id: UserId, data_root: str | Path | None = None) -> DemoUser:
    user = load_demo_fixtures(data_root).user(user_id)
    if user is None:
        raise KeyError(f"unknown demo user {user_id!r}")
    return user


# --------------------------------------------------------------------------------------------------------------------
# Cross-file validation
# --------------------------------------------------------------------------------------------------------------------
WARNING = "warning: "
_EPS = 1e-6
MANDATE_MIN_VALIDITY = timedelta(hours=48)


def is_warning(problem: str) -> bool:
    return problem.startswith(WARNING)


def _clearance_problems(where: str, pose: Pose2D, plan: Planogram) -> list[str]:
    """Robot footprint (ROBOT_RADIUS_M) must fit at ``pose``: away from fixtures, obstacles and the store outline."""
    out: list[str] = []
    r = ROBOT_RADIUS_M
    if not plan.bounds.inflated(-r + _EPS).contains(pose.x, pose.y):
        out.append(f"{where} ({pose.x}, {pose.y}) is closer than ROBOT_RADIUS_M={r} to the store outline")
    for fx in plan.fixtures:
        d = fx.rect.distance_to(pose.x, pose.y)
        if d < r - _EPS:
            out.append(f"{where} ({pose.x}, {pose.y}) is {d:.2f} m from fixture {fx.fixture_id} (< {r})")
    for i, ob in enumerate(plan.obstacles):
        d = ob.distance_to(pose.x, pose.y)
        if d < r - _EPS:
            out.append(f"{where} ({pose.x}, {pose.y}) is {d:.2f} m from obstacle #{i} (< {r})")
    return out


def _validate_planogram(plan: Planogram, products: dict[Sku, Product]) -> list[str]:
    problems: list[str] = []
    sid = plan.store_id
    named_poses = [("entrance", plan.entrance), ("checkout", plan.checkout)]
    if plan.robot_home is not None:
        named_poses.append(("robot_home", plan.robot_home))
    for name, pose in named_poses:
        problems += _clearance_problems(f"{sid}: {name}", pose, plan)

    for slot in plan.slots:
        where = f"{sid}/{slot.slot_id}"
        if slot.sku not in products:
            problems.append(f"{where}: unknown sku {slot.sku!r}")
        elif products[slot.sku].online_only:
            problems.append(f"{where}: sku {slot.sku!r} is online_only but stocked in store")
        ap, item = slot.approach_pose, slot.item_position
        problems += _clearance_problems(f"{where}: approach_pose", ap, plan)
        if not plan.bounds.contains(item.x, item.y):
            problems.append(f"{where}: item_position outside store bounds")
        if math.hypot(item.x - ap.x, item.y - ap.y) > _EPS:
            heading = math.atan2(item.y - ap.y, item.x - ap.x)
            if abs(angle_diff(heading, ap.theta)) >= math.pi / 4:
                problems.append(
                    f"{where}: approach_pose.theta {ap.theta:.2f} does not face item_position "
                    f"(heading {heading:.2f}); set theta = atan2(item - approach)"
                )
        fixture = plan.fixture(slot.fixture_id)
        if fixture is None:
            continue
        if not fixture.rect.inflated(0.1).contains(item.x, item.y):
            problems.append(f"{where}: item_position ({item.x}, {item.y}) is not on fixture {fixture.fixture_id}")
        if slot.level >= fixture.levels:
            problems.append(f"{where}: level {slot.level} >= fixture {fixture.fixture_id} levels {fixture.levels}")
        elif fixture.level_z_m is not None and abs(item.z - fixture.level_z_m[slot.level]) > 0.02:
            problems.append(
                f"{where}: item_position.z {item.z} != fixture {fixture.fixture_id} level_z_m[{slot.level}] "
                f"{fixture.level_z_m[slot.level]}"
            )
        if item.z > fixture.height_m:
            problems.append(f"{where}: item_position.z {item.z} above fixture height {fixture.height_m}")
        if slot.aisle_id and fixture.aisle_id and slot.aisle_id != fixture.aisle_id:
            problems.append(f"{where}: aisle_id {slot.aisle_id!r} != fixture aisle_id {fixture.aisle_id!r}")
        if slot.lots and sum(lot.qty for lot in slot.lots) != slot.qty:
            problems.append(f"{where}: lot qty sum {sum(lot.qty for lot in slot.lots)} != slot qty {slot.qty}")
        if slot.deal is not None and slot.deal.lot_id is not None:
            if slot.deal.lot_id not in {lot.lot_id for lot in slot.lots}:
                problems.append(f"{where}: deal lot_id {slot.deal.lot_id!r} is not one of the slot's lots")
    return problems


def _validate_user(user: DemoUser, products: dict[Sku, Product], store_ids: list[StoreId]) -> list[str]:
    problems: list[str] = []
    uid = user.user_id
    prof, mandate = user.profile, user.mandate
    skus = [*prof.favorite_skus, *prof.disliked_skus, *(p.sku for p in user.purchases)]
    for sku in skus:
        if sku not in products:
            problems.append(f"user {uid}: unknown sku {sku!r}")
    if prof.home_store_id not in store_ids:
        problems.append(f"user {uid}: home_store_id {prof.home_store_id!r} has no planogram")
    if mandate.expires_at <= utcnow() + MANDATE_MIN_VALIDITY:
        problems.append(f"{WARNING}user {uid}: mandate expires {mandate.expires_at.isoformat()} (< 48 h from now)")
    if mandate.ask_above_cents > mandate.cap_cents:
        problems.append(f"user {uid}: mandate ask_above_cents {mandate.ask_above_cents} > cap_cents")
    if mandate.revoked:
        problems.append(f"user {uid}: demo mandate is revoked")
    allowed, blocked = set(mandate.allowed_categories), set(mandate.blocked_categories)
    for sku in [*prof.favorite_skus, *(p.sku for p in user.purchases)]:
        cat = products[sku].category if sku in products else None
        if cat is None:
            continue
        if cat in blocked or (allowed and cat not in allowed):
            problems.append(f"user {uid}: mandate blocks category {cat!r} of their own sku {sku!r}")
    return problems


def validate_data(data_root: str | Path | None = None) -> list[str]:
    """Load every file and run cross-file + physical checks. Returns problems (empty = all good); entries starting
    with ``warning:`` are advisory."""
    problems: list[str] = []
    try:
        products = load_products(data_root)
    except DataError as exc:
        return [str(exc)]
    for sku, prod in products.items():
        if len(prod.barcode) == 13 and not is_valid_ean13(prod.barcode):
            problems.append(f"product {sku}: barcode {prod.barcode} has a wrong EAN-13 check digit")
    store_ids = list_store_ids(data_root)
    if not store_ids:
        problems.append(f"no planograms in {data_dir(data_root) / PLANOGRAM_DIR}")

    for sid in store_ids:
        try:
            plan = load_planogram(sid, data_root)
        except DataError as exc:
            problems.append(str(exc))
            continue
        problems += _validate_planogram(plan, products)

    try:
        online = load_online_catalog(data_root)
        for item in online.items:
            if item.sku not in products:
                problems.append(f"online: unknown sku {item.sku!r}")
    except DataError as exc:
        problems.append(str(exc))

    try:
        fixtures = load_demo_fixtures(data_root)
        for user in fixtures.users:
            problems += _validate_user(user, products, store_ids)
    except DataError as exc:
        problems.append(str(exc))
    return problems


def main() -> int:
    problems = validate_data()
    root = data_dir()
    errors = [p for p in problems if not is_warning(p)]
    for p in problems:
        print(f"  - {p}")
    if errors:
        print(f"{len(errors)} problem(s) in {root}")
        return 1
    print(f"data OK: {root} (stores: {', '.join(list_store_ids(include_online=True))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
