"""Loaders + validators for the data files under ``PICKY_DATA_DIR`` (default ``<repo>/data``) -> contracts models.

    data/products.yaml                -> ProductsFile      (load_products / load_products_file)
    data/planograms/<store_id>.yaml   -> Planogram         (load_planogram / list_store_ids)
    data/online_store.yaml            -> OnlineCatalog     (load_online_catalog)
    data/fixtures/users.yaml          -> FixturesFile      (load_demo_fixtures / load_demo_user)
    data/fixtures/robots.yaml         -> RobotsFile        (load_robots / load_robot / robots_for_store /
                                                            load_store_cameras / load_cameras)
    data/categories.yaml              -> CategoriesFile    (load_categories_file / load_categories)

Results are cached per (file, mtime), so edits are picked up without a restart. Every call returns a deep copy, so
callers may mutate what they get (e.g. stock levels) without corrupting the cache.

Run ``python -m utils.data`` to validate the data directory (cross-file + physical-consistency checks: robot
clearance, SO-101 reach via ``contracts.common.slot_reachable``, slot ids unique across stores, qty / low_threshold
within capacity, dock spacing, ...). Problems prefixed ``warning:`` don't fail the run.
"""

import math
import sys
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from contracts.cameras import DEFAULT_ROBOT_CAMERA_MOUNTS, CameraInfo, robot_camera_id
from contracts.catalog import Product, is_valid_ean13
from contracts.common import (
    APPROACH_DIST_MAX_M,
    APPROACH_DIST_MIN_M,
    ARM_MOUNT_X_M,
    ARM_REACH_M,
    ONLINE_STORE_ID,
    REACH_Z_MAX_M,
    REACH_Z_MIN_M,
    ROBOT_RADIUS_M,
    CategoryId,
    Pose2D,
    RobotId,
    Sku,
    StoreId,
    UserId,
    angle_diff,
    slot_reachable,
    utcnow,
)
from contracts.planogram import (
    CategoriesFile,
    CategoryDef,
    DemoUser,
    FixturesFile,
    OnlineCatalog,
    Planogram,
    ProductsFile,
    RobotsFile,
    RobotSpec,
    StoreCameraSpec,
)
from utils.config import get_settings

REPO_ROOT = Path(__file__).resolve().parents[1]

PRODUCTS_FILE = "products.yaml"
PLANOGRAM_DIR = "planograms"
ONLINE_FILE = "online_store.yaml"
USERS_FILE = "fixtures/users.yaml"
ROBOTS_FILE = "fixtures/robots.yaml"
CATEGORIES_FILE = "categories.yaml"

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


def load_robots_file(data_root: str | Path | None = None) -> RobotsFile:
    """data/fixtures/robots.yaml (fleet robots + store ceiling cameras)."""
    return _load(data_dir(data_root) / ROBOTS_FILE, RobotsFile, "robots")


def load_robots(data_root: str | Path | None = None) -> dict[RobotId, RobotSpec]:
    """Every fleet robot keyed by robot_id (file order preserved)."""
    return {r.robot_id: r for r in load_robots_file(data_root).robots}


def load_robot(robot_id: RobotId, data_root: str | Path | None = None) -> RobotSpec:
    robot = load_robots_file(data_root).robot(robot_id)
    if robot is None:
        raise KeyError(f"unknown robot {robot_id!r}")
    return robot


def robots_for_store(store_id: StoreId, data_root: str | Path | None = None) -> list[RobotSpec]:
    return load_robots_file(data_root).robots_in(store_id)


def load_store_cameras(store_id: StoreId | None = None, data_root: str | Path | None = None) -> list[StoreCameraSpec]:
    rf = load_robots_file(data_root)
    return rf.store_cameras if store_id is None else rf.cameras_in(store_id)


def load_cameras(
    store_id: StoreId | None = None, robot_id: RobotId | None = None, data_root: str | Path | None = None
) -> list[CameraInfo]:
    """CameraInfo for every configured camera: each robot's cameras ('<robot_id>.<kind>', DEFAULT_ROBOT_CAMERA_MOUNTS)
    then the store ceiling cameras. ``source='fake'``/``online=True`` (CameraService overlays live Isaac info)."""
    rf = load_robots_file(data_root)
    out: list[CameraInfo] = []
    for r in rf.robots:
        if (store_id is not None and r.store_id != store_id) or (robot_id is not None and r.robot_id != robot_id):
            continue
        for kind in r.cameras:
            out.append(
                CameraInfo(
                    camera_id=robot_camera_id(r.robot_id, kind),
                    kind=kind,
                    name=f"{r.name} · {kind}",
                    store_id=r.store_id,
                    robot_id=r.robot_id,
                    mount=DEFAULT_ROBOT_CAMERA_MOUNTS[kind].model_copy(),
                )
            )
    if robot_id is None:
        for c in rf.store_cameras:
            if store_id is None or c.store_id == store_id:
                out.append(
                    CameraInfo(camera_id=c.camera_id, kind="ceiling", name=c.name, store_id=c.store_id, mount=c.mount)
                )
    return out


def load_categories_file(data_root: str | Path | None = None) -> CategoriesFile:
    """data/categories.yaml (category taxonomy)."""
    return _load(data_dir(data_root) / CATEGORIES_FILE, CategoriesFile, "categories")


def load_categories(data_root: str | Path | None = None) -> dict[CategoryId, CategoryDef]:
    """Every category (top-level and subcategories) keyed by id, in file order."""
    return {c.category_id: c for c in load_categories_file(data_root).categories}


# --------------------------------------------------------------------------------------------------------------------
# Cross-file validation
# --------------------------------------------------------------------------------------------------------------------
WARNING = "warning: "
_EPS = 1e-6
_REACH_SLACK = 0.02
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
        reach = math.hypot(item.x - ap.x, item.y - ap.y)
        reachable = slot_reachable(ap, ap, item)  # THE predicate the robot fake and the sim use
        if not reachable and reach > ARM_MOUNT_X_M + ARM_REACH_M + _EPS:
            problems.append(
                f"{where}: item_position is {reach:.2f} m from approach_pose, beyond the SO-101 reach "
                f"({ARM_MOUNT_X_M + ARM_REACH_M:.2f} m); put the approach pose {APPROACH_DIST_MIN_M}-"
                f"{APPROACH_DIST_MAX_M} m off the shelf face"
            )
        elif not APPROACH_DIST_MIN_M - _REACH_SLACK <= reach <= APPROACH_DIST_MAX_M + _REACH_SLACK:
            problems.append(
                f"{WARNING}{where}: approach_pose is {reach:.2f} m from item_position "
                f"(design range {APPROACH_DIST_MIN_M}-{APPROACH_DIST_MAX_M} m)"
            )
        if not reachable and not REACH_Z_MIN_M - _EPS <= item.z <= REACH_Z_MAX_M + _EPS:
            problems.append(
                f"{where}: item_position.z {item.z} outside the lift + arm range {REACH_Z_MIN_M}-{REACH_Z_MAX_M} m"
            )
        if reach > _EPS:
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
        if slot.qty > slot.capacity:
            problems.append(f"{where}: qty {slot.qty} > capacity {slot.capacity}")
        if slot.low_threshold is not None and slot.low_threshold > slot.capacity:
            problems.append(f"{where}: low_threshold {slot.low_threshold} > capacity {slot.capacity}")
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


DOCK_SPACING_M = 2 * ROBOT_RADIUS_M + 0.1
"""Minimum distance between two docks in the same store (rovers must not spawn inside each other)."""


def _validate_robots(rf: RobotsFile, plans: dict[StoreId, Planogram]) -> list[str]:
    problems: list[str] = []
    for r in rf.robots:
        plan = plans.get(r.store_id)
        if plan is None:
            problems.append(f"robot {r.robot_id}: store {r.store_id!r} has no planogram")
            continue
        problems += _clearance_problems(f"robot {r.robot_id}: dock", r.dock, plan)
        for slot in plan.slots:
            d = r.dock.distance_to(slot.approach_pose)
            if d < ROBOT_RADIUS_M - _EPS:
                problems.append(
                    f"robot {r.robot_id}: dock is {d:.2f} m from {plan.store_id}/{slot.slot_id} approach_pose "
                    f"(< ROBOT_RADIUS_M={ROBOT_RADIUS_M})"
                )
    for sid in plans:
        docks = rf.robots_in(sid)
        for i, a in enumerate(docks):
            for b in docks[i + 1 :]:
                d = a.dock.distance_to(b.dock)
                if d < DOCK_SPACING_M - _EPS:
                    problems.append(
                        f"robot {a.robot_id} / {b.robot_id}: docks are {d:.2f} m apart (< {DOCK_SPACING_M:.1f} m)"
                    )
    for c in rf.store_cameras:
        plan = plans.get(c.store_id)
        if plan is None:
            problems.append(f"camera {c.camera_id}: store {c.store_id!r} has no planogram")
            continue
        if not plan.bounds.contains(c.mount.x, c.mount.y) or not 0.0 < c.mount.z <= 8.0:
            problems.append(f"camera {c.camera_id}: mount ({c.mount.x}, {c.mount.y}, {c.mount.z}) is outside the store")
    for sid in plans:
        if not rf.robots_in(sid):
            problems.append(f"{WARNING}store {sid} has no robots in {ROBOTS_FILE}")
    return problems


def _validate_categories(
    cats: CategoriesFile, products: dict[Sku, Product], plans: dict[StoreId, Planogram], users: list[DemoUser]
) -> list[str]:
    problems: list[str] = []
    top = {c.category_id for c in cats.top_level()}
    for sku, prod in products.items():
        if prod.category not in top:
            problems.append(f"product {sku}: category {prod.category!r} is not a top-level id in {CATEGORIES_FILE}")
        elif prod.subcategory and prod.subcategory not in {c.category_id for c in cats.children(prod.category)}:
            problems.append(f"{WARNING}product {sku}: subcategory {prod.subcategory!r} is not under {prod.category!r}")
    known = {c.category_id for c in cats.categories}
    for sid, plan in plans.items():
        for aisle in plan.aisles:
            for cat in aisle.categories:
                if cat not in known:
                    problems.append(f"{WARNING}{sid}/{aisle.aisle_id}: unknown category {cat!r}")
    for user in users:
        for cat in [*user.mandate.allowed_categories, *user.mandate.blocked_categories]:
            if cat not in top:
                problems.append(f"{WARNING}user {user.user_id}: mandate category {cat!r} is not a top-level id")
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

    plans: dict[StoreId, Planogram] = {}
    for sid in store_ids:
        try:
            plan = load_planogram(sid, data_root)
        except DataError as exc:
            problems.append(str(exc))
            continue
        plans[sid] = plan
        problems += _validate_planogram(plan, products)
    slot_owner: dict[str, StoreId] = {}
    for sid, plan in plans.items():  # slot ids are global (sim instance ids '<slot_id>#nn' must not collide)
        for slot in plan.slots:
            other = slot_owner.setdefault(slot.slot_id, sid)
            if other != sid:
                problems.append(
                    f"{sid}/{slot.slot_id}: slot_id also used in {other}; slot ids must be unique across stores "
                    f"(prefix them with the store, e.g. 'AVE-...')"
                )

    try:
        online = load_online_catalog(data_root)
        for item in online.items:
            if item.sku not in products:
                problems.append(f"online: unknown sku {item.sku!r}")
    except DataError as exc:
        problems.append(str(exc))

    users: list[DemoUser] = []
    try:
        users = load_demo_fixtures(data_root).users
        for user in users:
            problems += _validate_user(user, products, store_ids)
    except DataError as exc:
        problems.append(str(exc))

    root = data_dir(data_root)
    if (root / ROBOTS_FILE).exists():
        try:
            problems += _validate_robots(load_robots_file(data_root), plans)
        except DataError as exc:
            problems.append(str(exc))
    else:
        problems.append(f"{WARNING}no {ROBOTS_FILE} (fleet robots + store cameras)")
    if (root / CATEGORIES_FILE).exists():
        try:
            problems += _validate_categories(load_categories_file(data_root), products, plans, users)
        except DataError as exc:
            problems.append(str(exc))
    else:
        problems.append(f"{WARNING}no {CATEGORIES_FILE} (category taxonomy)")
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
    robots = ", ".join(load_robots(root)) if (root / ROBOTS_FILE).exists() else "none"
    print(f"data OK: {root} (stores: {', '.join(list_store_ids(include_online=True))}; robots: {robots})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
