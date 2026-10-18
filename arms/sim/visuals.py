"""Visual dressing for the Isaac backend: 3D models for products, per-store colors, fixture styles and decor.

Pure (no Isaac imports) so scene_spec stays testable on a Mac. Reads ``arms/sim/visuals.yaml``. Nothing here changes
ground truth: slot ids, unit counts and the wire are planogram-derived; a fixture style may only change where a slot's
units stand on the fixture (``scene_spec.slot_layout``) and how the fixture is drawn.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

HERE = Path(__file__).resolve().parent
VISUALS_FILE = HERE / "visuals.yaml"
DEFAULT_ASSET_DIR = HERE / "assets" / "build" / "visuals"
"""Converted USD models (``<dir>/<asset>/<asset>.usd``); override with PICKY_SIM_ASSET_DIR. Gitignored."""

FixtureStyle = Literal["table"]
DecorKind = Literal["panel", "sign", "asset"]
Vec3 = tuple[float, float, float]


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssetSource(_M):
    glb: str = Field(description="Path under VisualsFile.source_base.")
    name: str
    author: str
    license: str
    url: str
    yaw_deg: float = Field(0.0, description="Extra turn about +z so the model's front faces the item's -y face.")
    up: Literal["y", "z"] = "y"


class ProductVisual(_M):
    asset: str
    display: Literal["stand", "first"] | None = Field(
        None,
        description="'stand': every unit is the device upright on a display puck, leaned back `tilt_deg`. "
        "'first': the slot's front unit is the device (at `model_dims`), the rest stay labeled retail boxes.",
    )
    tilt_deg: float = 12.0
    model_dims: Vec3 | None = Field(None, description="True device size (w, d, h) when units are boxed packaging.")


class TableStyle(_M):
    top_color: str = "#d8c3a2"
    base_color: str = "#ffffff"
    top_thickness_m: float = 0.04
    base_inset_m: float = 0.1
    row_pitch_m: float = 0.16
    max_cols: int = Field(4, description="Units side by side along the table edge before starting a second row.")


class Decor(_M):
    kind: DecorKind
    name: str
    center: Vec3 = Field(description="Store frame (meters). 'asset' props: center of the footprint on the floor.")
    size: Vec3
    yaw: float = 0.0
    color: str = "#cccccc"
    text: str | None = None
    text_color: str = "#ffffff"
    asset: str | None = None


class StoreVisuals(_M):
    colors: dict[str, str] = Field(default_factory=dict, description="floor/wall/ceiling + fixture kind -> color.")
    fixture_styles: dict[str, FixtureStyle] = Field(default_factory=dict)
    table: TableStyle = Field(default_factory=TableStyle)
    decor: list[Decor] = Field(default_factory=list)


class VisualsFile(_M):
    schema_version: int = 1
    source_base: str
    assets: dict[str, AssetSource] = Field(default_factory=dict)
    products: dict[str, ProductVisual] = Field(default_factory=dict)
    stores: dict[str, StoreVisuals] = Field(default_factory=dict)


@lru_cache(maxsize=4)
def load_visuals(path: str | None = None) -> VisualsFile:
    """The visuals file (cached). A missing file means no dressing at all."""
    file = Path(path) if path else VISUALS_FILE
    if not file.exists():
        return VisualsFile(source_base="")
    return VisualsFile.model_validate(yaml.safe_load(file.read_text()))


def store_visuals(store_id: str) -> StoreVisuals:
    return load_visuals().stores.get(store_id, StoreVisuals())


def fixture_style(store_id: str | None, fixture_id: str) -> FixtureStyle | None:
    if store_id is None:
        return None
    return store_visuals(store_id).fixture_styles.get(fixture_id)


def product_visual(sku: str | None) -> ProductVisual | None:
    return load_visuals().products.get(sku) if sku else None


def asset_dir() -> Path:
    return Path(os.environ.get("PICKY_SIM_ASSET_DIR") or DEFAULT_ASSET_DIR)


def asset_usd(asset: str) -> Path:
    """Where the converted USD of ``asset`` lives (it may not exist yet: the backend then draws the box)."""
    return asset_dir() / asset / f"{asset}.usd"
