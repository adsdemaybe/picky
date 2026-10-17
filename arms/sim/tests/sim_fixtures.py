"""Synthetic store for sim tests (independent of data/, which is written by the data agent)."""

import math

from contracts.catalog import Product, SimAsset
from contracts.planogram import Planogram


def products() -> list[Product]:
    def p(sku: str, name: str, cat: str, cents: int, code: str, **asset) -> Product:
        return Product(sku=sku, name=name, category=cat, price_cents=cents, barcode=code, asset=SimAsset(**asset))

    return [
        p("salsa_hot", "Hot Salsa", "pantry", 399, "0000000000017", shape="cylinder", dims_m=(0.08, 0.08, 0.12)),
        p("tortillas", "Corn Tortillas", "bakery", 299, "0000000000024", dims_m=(0.2, 0.05, 0.25)),
        p("avocado", "Hass Avocado", "produce", 129, "0000000000031", shape="sphere", dims_m=(0.08, 0.08, 0.1)),
        p("beans", "Black Beans", "pantry", 149, "0000000000048", shape="cylinder", dims_m=(0.075, 0.075, 0.11)),
    ]


def planogram() -> Planogram:
    return Planogram.model_validate(
        {
            "store_id": "store_test",
            "name": "Test Store",
            "bounds": {"x": 0, "y": 0, "w": 14, "h": 22},
            "entrance": {"x": 1, "y": 1, "theta": math.pi / 2},
            "checkout": {"x": 3, "y": 1.5},
            "fixtures": [
                {
                    "fixture_id": "f1",
                    "kind": "shelf",
                    "aisle_id": "a1",
                    "rect": {"x": 2, "y": 4, "w": 0.6, "h": 8},
                    "yaw": 0.0,
                    "height_m": 1.8,
                    "levels": 4,
                    "level_z_m": [0.1, 0.55, 1.0, 1.45],
                },
                {
                    "fixture_id": "g1",
                    "kind": "shelf",
                    "rect": {"x": 6, "y": 4, "w": 1.0, "h": 8},
                    "yaw": 0.0,
                    "double_sided": True,
                    "levels": 2,
                    "level_z_m": [0.3, 0.9],
                },
                {
                    "fixture_id": "p1",
                    "kind": "produce_bin",
                    "rect": {"x": 10, "y": 15, "w": 2, "h": 1},
                    "yaw": -math.pi / 2,
                    "height_m": 0.9,
                    "levels": 1,
                    "level_z_m": [0.8],
                },
                {
                    "fixture_id": "co",
                    "kind": "checkout",
                    "rect": {"x": 4, "y": 0.5, "w": 1.5, "h": 0.6},
                    "height_m": 0.9,
                },
            ],
            "aisles": [{"aisle_id": "a1", "label": "Aisle 1", "rect": {"x": 2.6, "y": 4, "w": 1.6, "h": 8}}],
            "slots": [
                {
                    "slot_id": "s_salsa",
                    "fixture_id": "f1",
                    "aisle_id": "a1",
                    "sku": "salsa_hot",
                    "level": 1,
                    "item_position": {"x": 2.6, "y": 5.0, "z": 0.55},
                    "approach_pose": {"x": 3.3, "y": 5.0, "theta": math.pi},
                    "facings": 2,
                    "qty": 6,
                    "lots": [{"lot_id": "L1", "qty": 6, "expiry": "2026-10-25"}],
                },
                {
                    "slot_id": "s_tort_w",
                    "fixture_id": "g1",
                    "sku": "tortillas",
                    "level": 0,
                    "item_position": {"x": 6.0, "y": 6.0, "z": 0.3},
                    "approach_pose": {"x": 5.2, "y": 6.0, "theta": 0.0},
                    "qty": 3,
                },
                {
                    "slot_id": "s_beans_e",
                    "fixture_id": "g1",
                    "sku": "beans",
                    "level": 1,
                    "item_position": {"x": 7.0, "y": 6.0, "z": 0.9},
                    "approach_pose": {"x": 7.8, "y": 6.0, "theta": math.pi},
                    "qty": 0,
                },
                {
                    "slot_id": "s_avo",
                    "fixture_id": "p1",
                    "sku": "avocado",
                    "level": 0,
                    "item_position": {"x": 11.0, "y": 15.0, "z": 0.8},
                    "approach_pose": {"x": 11.0, "y": 14.2, "theta": math.pi / 2},
                    "qty": 6,
                    "lots": [
                        {"lot_id": "AV_OLD", "qty": 3, "freshness": 0.5},
                        {"lot_id": "AV_NEW", "qty": 3, "freshness": 0.9},
                    ],
                },
                {
                    "slot_id": "s_ghost",
                    "fixture_id": "f1",
                    "sku": "does_not_exist",
                    "level": 0,
                    "item_position": {"x": 2.6, "y": 8.0, "z": 0.1},
                    "approach_pose": {"x": 3.3, "y": 8.0, "theta": math.pi},
                    "qty": 4,
                },
            ],
        }
    )
