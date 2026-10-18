"""Restock every shelf slot so its front rows are full across the room it owns (a stocked-store look).

The sim lays a slot's units out WIDE (``arms.sim.scene_spec.slot_layout`` with ``slot_lateral_room``); a slot only
looks full when it holds enough units. This raises ``qty`` (and ``capacity``) to ``cols x FRONT_ROWS`` for every shelf
/ fridge / freezer / endcap slot that holds less, and scales its ``lots`` to match (the last lot takes the remainder).
Produce tables and anything already fuller are left alone. Edits ``data/planograms/*.yaml`` in place, keeping
comments (ruamel.yaml). Out-of-stock slots (qty 0) stay out of stock: they are part of the demo.

    uv run --with ruamel.yaml python scripts/restock_planograms.py [--rows 2] [--dry-run]
"""

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ruamel.yaml import YAML  # noqa: E402

from arms.sim.scene_spec import slot_lateral_room, slot_layout  # noqa: E402
from utils.data import load_planogram, load_products  # noqa: E402

SHELF_KINDS = {"shelf", "fridge", "freezer", "endcap"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=2, help="rows (front to back) to fill")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip", default="store_tmobile", help="comma list of stores to leave alone")
    args = ap.parse_args()
    products = load_products()
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 120
    for path in sorted((ROOT / "data/planograms").glob("store_*.yaml")):
        if path.stem in args.skip.split(","):
            continue
        plan = load_planogram(path.stem)
        fixtures = {f.fixture_id: f for f in plan.fixtures}
        doc = yaml.load(path.read_text())
        raw = {s["slot_id"]: s for s in doc["slots"]}
        before = after = changed = 0
        for slot in plan.slots:
            before += slot.qty
            fx = fixtures.get(slot.fixture_id)
            product = products.get(slot.sku)
            if fx is None or product is None or fx.kind not in SHELF_KINDS or slot.qty == 0:
                after += slot.qty
                continue
            room = slot_lateral_room(plan, slot, fx)
            lay = slot_layout(slot, fx, product, lateral_room_m=room, store_id=plan.store_id)
            positions = len(lay.positions)
            # columns actually used across the front at full stock = positions in the first row of layer 0
            front = lay.positions[0]
            cols = sum(1 for p in lay.positions if abs(p[2] - front[2]) < 1e-6)
            rows_avail = max(1, cols and positions // max(1, cols))
            target = min(positions, cols * min(args.rows, rows_avail) if cols else slot.qty)
            # wide fill needs capacity >= the units, or the layout caps the columns at the old capacity
            fit_cols = int((room * 0.9 + 0.005) // (lay.dims[0] + 0.005)) if room else cols
            target = max(target, min(positions, fit_cols * args.rows))
            if target <= slot.qty:
                after += slot.qty
                continue
            r = raw[slot.slot_id]
            r["qty"] = target
            r["capacity"] = max(int(r.get("capacity", 12)), target)
            lots = r.get("lots") or []
            if lots:
                total = sum(int(lot["qty"]) for lot in lots) or 1
                acc = 0
                for i, lot in enumerate(lots):
                    q = target - acc if i == len(lots) - 1 else round(int(lot["qty"]) * target / total)
                    lot["qty"] = max(0, q)
                    acc += lot["qty"]
            after += target
            changed += 1
        print(f"{plan.store_id}: {changed} slots restocked, units {before} -> {after}")
        if not args.dry_run:
            with path.open("w") as fh:
                yaml.dump(doc, fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
