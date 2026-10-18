"""External scenes: a store whose shelves and products come from an existing USD (e.g. the AgiBot Genie Sim
supermarket, ``store_genie``) instead of being generated from the planogram's primitives.

PURE python (pydantic + pyyaml). The data file is sim-only and generated next to the planogram
(``data/sim/<store_id>_prims.yaml``, written by ``scripts/genie_to_planogram.py``; no contract change)::

    schema: picky external scene v1
    store_id: store_genie
    usd: {root_env: PICKY_GENIESIM_ROOT, path: background/.../background.usda, default_prim: /World,
          translate: [3.25, 2.1, 0.0]}          # USD frame -> store frame (translation only)
    fixtures_in_usd: [GEN-SHELF-01, ...]        # drawn by the USD: the worker skips their primitives
    slots:
      GEN-SHELF-01-N3-00:                       # unit n of the slot = units[n] (contracts.sim.instance_id order)
      - {path: background/Group_10/benchmark_snack_263_000, c: [0.962, 0.431, 1.593], s: [0.207, 0.145, 0.276]}

``c`` / ``s`` are the unit's box center and world-aligned size in the STORE frame: they are the ground truth the worker
reports (``ItemInstance.position``) and grasps with, so ``WorkerCore`` never needs the USD. Only the Isaac backend
touches the stage: it references the USD under ``<store root>/Scene`` and moves / hides the mapped prims.

The mode is on only when the USD exists on this host (``<$root_env>/<path>``, or ``PICKY_EXTERNAL_USD_<STORE_ID>``
as a full-path override): anywhere else (laptop, CI, the sketch backend) the same store is built from the planogram's
primitives, so ids, counts and the data hash never depend on the host.
"""

import math
import os
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

Vec3 = tuple[float, float, float]
SCHEMA = "picky external scene v1"
OVERRIDE_ENV_PREFIX = "PICKY_EXTERNAL_USD_"
"""``PICKY_EXTERNAL_USD_STORE_GENIE=/abs/path/background.usda`` points one store at a USD directly."""
SWITCH_ENV = "PICKY_EXTERNAL_SCENES"
"""``PICKY_EXTERNAL_SCENES=0`` turns every external scene off (primitive stores everywhere)."""


class ExternalUnit(BaseModel):
    model_config = ConfigDict(extra="ignore")

    path: str = Field(description="Prim path relative to the USD's default prim.")
    c: Vec3 = Field(description="Box center, store frame (m).")
    s: Vec3 = Field(description="World-aligned box size (dx, dy, dz), m.")


class ExternalUsd(BaseModel):
    model_config = ConfigDict(extra="ignore")

    root_env: str | None = None
    path: str
    default_prim: str = "/World"
    translate: Vec3 = (0.0, 0.0, 0.0)


class ExternalScene(BaseModel):
    """One store's external scene (``data/sim/<store_id>_prims.yaml``); ``usd_file`` set = active on this host."""

    model_config = ConfigDict(extra="ignore")

    schema_: str = Field(SCHEMA, alias="schema")
    store_id: str
    usd: ExternalUsd
    fixtures_in_usd: list[str] = Field(default_factory=list)
    slots: dict[str, list[ExternalUnit]] = Field(default_factory=dict)
    usd_file: str | None = Field(None, description="Resolved absolute USD path (None = not on this host).")

    @property
    def active(self) -> bool:
        return self.usd_file is not None

    def units(self, slot_id: str) -> list[ExternalUnit]:
        return self.slots.get(slot_id, [])


def scene_file(store_id: str, data_root: str | Path | None = None) -> Path:
    from utils.data import data_dir

    return data_dir(data_root) / "sim" / f"{store_id}_prims.yaml"


def resolve_usd(scene: ExternalScene, env: Mapping[str, str] | None = None) -> str | None:
    """The USD file on this host: ``PICKY_EXTERNAL_USD_<STORE>`` > ``$<root_env>/<path>``; None when missing."""
    env = os.environ if env is None else env
    override = env.get(OVERRIDE_ENV_PREFIX + scene.store_id.upper())
    candidates = [override] if override else []
    if scene.usd.root_env and env.get(scene.usd.root_env):
        candidates.append(os.path.join(env[scene.usd.root_env], scene.usd.path))
    if os.path.isabs(scene.usd.path):
        candidates.append(scene.usd.path)
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def load_external_scene(
    store_id: str, data_root: str | Path | None = None, env: Mapping[str, str] | None = None
) -> ExternalScene | None:
    """``data/sim/<store_id>_prims.yaml`` with ``usd_file`` resolved on this host; None when the store has none, the
    file is unreadable or ``PICKY_EXTERNAL_SCENES=0``. The scene is returned even when its USD is missing here
    (``active`` False): callers build the primitive store then."""
    import yaml

    env = os.environ if env is None else env
    if str(env.get(SWITCH_ENV, "1")).strip().lower() in ("0", "false", "no", "off"):
        return None
    path = scene_file(store_id, data_root)
    if not path.is_file():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    scene = ExternalScene.model_validate(raw)
    if scene.store_id != store_id:
        raise ValueError(f"{path}: store_id {scene.store_id!r} != {store_id!r}")
    return scene.model_copy(update={"usd_file": resolve_usd(scene, env)})


def item_dims(unit: ExternalUnit, item_yaw: float) -> Vec3:
    """Unit size in the item frame (x = width along the shelf, y = depth, z = up) from its world-aligned box: the
    item's x axis runs along world x when ``item_yaw`` is ~0 / pi, along world y when it is ~+-pi/2."""
    dx, dy, dz = unit.s
    along_x = abs(math.cos(item_yaw)) >= abs(math.sin(item_yaw))
    return (dx, dy, dz) if along_x else (dy, dx, dz)
