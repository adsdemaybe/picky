"""Per-item grocery models + rover USD puppet: mapping, fallbacks and geometry, all without Isaac (NullBackend = the
fake isaac layer; the pxr authoring itself only runs on the GPU host)."""

import math
from pathlib import Path

import pytest

from arms.sim import rover_puppet as puppet
from arms.sim.assets import (
    SKU_ASSETS_FILE,
    LabelSpec,
    SkuAssets,
    item_to_native,
    jitter_deg,
    label_meshes,
    rover_usd_from_env,
)
from arms.sim.geometry import ARM_FULL_EXT_M, ARM_STOW_EXT_M
from arms.sim.scene_spec import build_rover_spec, build_scene_spec, load_world_spec
from arms.sim.tests.sim_fixtures import loader, planogram, products, robots, robots_loader
from arms.sim.worker import NullBackend, WorkerCore
from contracts.common import ARM_MOUNT_X_M, Pose2D
from contracts.sim import PickCmd, SetSlotStockCmd

REPO = Path(__file__).resolve().parents[3]
CAN = "items/sr_can/item.usda"
BOX = "items/rc_box/geom.usda"


def entries() -> dict:
    """A miniature sku_assets.yaml for the fixture products (same schema as data/sim/sku_assets.yaml)."""
    return {
        "salsa_hot": {
            "render": "asset",
            "match": "close",
            "usd": CAN,
            "scale_xyz": [0.9, 0.9, 1.1],
            "target_dims_m": [0.085, 0.085, 0.13],
            "facing": {"yaw_offset_deg": 0},
            "label": {"mode": "wrap", "band_z": [0.1, 0.9], "png_px": [467, 384]},
            "mass_kg": 0.45,
        },
        "tortillas": {
            "render": "parts",
            "match": "composite",
            "target_dims_m": [0.2, 0.05, 0.22],
            "facing": {"yaw_offset_deg": 0},
            "parts": [
                {
                    "asset": "rc_box",
                    "usd": BOX,
                    "rotate": [["x", 90]],
                    "scale_xyz": [1, 1, 1],
                    "offsets_m": [[0, 0, 0]],
                },
                {
                    "prim": "box",
                    "kind": "carrier",
                    "size_m": [0.2, 0.05, 0.02],
                    "offset_m": [0, 0, 0],
                    "collider": True,
                    "color": "label_color",  # symbolic in the generated map: the product's label color
                },
                {"prim": "box", "kind": "film", "size_m": [0.2, 0.05, 0.22], "offset_m": [0, 0, 0], "collider": False},
            ],
            "label": {"mode": "panel", "cover_wh": [0.9, 0.8], "center_z": 0.5},
            "mass_kg": 0.9,
        },
        "avocado": {
            "render": "asset",
            "match": "exact",
            "usd": "items/rc_avocado/item.usda",  # file deliberately missing
            "target_dims_m": [0.09, 0.09, 0.1],
            "facing": {"yaw_offset_deg": 0, "yaw_jitter_deg": 35},
            "label": {"mode": "none"},
        },
        "beans": {
            "render": "asset",
            "match": "gap",
            "usd": CAN,
            "target_dims_m": [0.07, 0.07, 0.1],
            "facing": {},
            "label": {"mode": "wrap"},
        },
    }


@pytest.fixture
def asset_root(tmp_path: Path) -> Path:
    for rel in (CAN, BOX):
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("#usda 1.0\n")
    return tmp_path


@pytest.fixture
def assets(asset_root: Path) -> SkuAssets:
    return SkuAssets(entries(), asset_root)


# ---- mapping + fallbacks -------------------------------------------------------------------------------------------
def test_resolution_and_fallback_reasons(assets: SkuAssets, asset_root: Path):
    salsa = assets.resolve("salsa_hot")
    assert salsa is not None and salsa.render == "asset"
    assert salsa.usd == str(asset_root / CAN) and salsa.dims == (0.085, 0.085, 0.13)
    assert salsa.scale_xyz == (0.9, 0.9, 1.1) and salsa.mass_kg == 0.45 and salsa.label.mode == "wrap"
    tort = assets.resolve("tortillas")
    assert tort is not None and tort.render == "parts"
    assert [m.usd for m in tort.models] == [str(asset_root / BOX)] and tort.models[0].rotate == (("x", 90.0),)
    assert [b.kind for b in tort.boxes] == ["carrier"]  # clear film skipped (translucency unverified in RTX)
    assert tort.boxes[0].color is None  # 'label_color' -> the backend uses the product's label color
    assert assets.resolve("avocado") is None and "missing model file" in assets.reasons["avocado"]
    assert assets.resolve("beans") is None and assets.reasons["beans"].startswith("gap")
    assert assets.resolve("unknown_sku") is None and "not in the sku assets map" in assets.reasons["unknown_sku"]
    assert assets.resolve(None) is None
    summary = assets.summary(["salsa_hot", "tortillas", "avocado", "beans"])
    assert summary["models"] == 2 and summary["primitive"] == 2


def test_from_env_degrades_to_primitives(tmp_path: Path, asset_root: Path):
    assert not SkuAssets.from_env({}).active  # PICKY_ASSET_ROOT unset (the default everywhere)
    assert SkuAssets.from_env({}).resolve("salsa_hot") is None
    assert not SkuAssets.from_env({"PICKY_ASSET_ROOT": str(tmp_path / "nope")}).active
    off = SkuAssets.from_env({"PICKY_ASSET_ROOT": str(asset_root), "PICKY_SIM_ASSETS": "off"})
    assert not off.active and off.resolve("salsa_hot") is None and "off" in off.describe
    broken = SkuAssets.from_env({"PICKY_ASSET_ROOT": str(asset_root), "PICKY_SKU_ASSETS": str(tmp_path / "x.yaml")})
    assert broken.resolve("salsa_hot") is None  # unreadable map: nothing resolves, nothing raises
    import yaml

    m = tmp_path / "map.yaml"
    m.write_text(yaml.safe_dump({"schema": "picky sku_assets v1", "skus": entries()}))
    ok = SkuAssets.from_env({"PICKY_ASSET_ROOT": str(asset_root), "PICKY_SKU_ASSETS": str(m)})
    assert ok.active and ok.resolve("salsa_hot") is not None


def test_committed_map_covers_every_product():
    from utils.data import load_products

    path = REPO / "data" / SKU_ASSETS_FILE
    sa = SkuAssets.load(path, "/assets", exists=lambda p: True)
    all_products = load_products(str(REPO / "data"))
    # The map covers the stores it was generated from (sku_assets.yaml -> generated_from.planograms); SKUs that only
    # live in other stores (e.g. store_tmobile) are unmapped on purpose and render as today's primitive + label.
    mapped_stores = _mapped_store_ids(path)
    products_ = {s: p for s, p in all_products.items() if s in sa.entries or _stocked_in(s, mapped_stores)}
    assert set(sa.entries) == set(products_)
    renders = [e["render"] for e in sa.entries.values()]
    assert renders.count("asset") + renders.count("parts") + renders.count("primitive") == len(products_)
    assert all(sa.resolve(s) is None for s in set(all_products) - set(sa.entries))  # unmapped -> primitive fallback
    resolved = {s: sa.resolve(s) for s in products_}
    gaps = {s for s, e in sa.entries.items() if e.get("match") == "gap"}
    by_design = {s for s, e in sa.entries.items() if e["render"] == "primitive"}
    assert gaps and by_design
    assert {s for s, u in resolved.items() if u is None} == gaps | by_design
    for sku, ua in resolved.items():
        if ua is None:
            continue
        assert min(ua.dims) > 0 and all(f.startswith("/assets/") for f in ua.files()), sku
        assert ua.render == "parts" or ua.usd.endswith("/item.usda"), sku
        assert all(b.color is None or b.color.startswith("#") for b in ua.boxes), sku


def _mapped_store_ids(map_path) -> set[str]:
    """Store ids named in sku_assets.yaml's generated_from.planograms ("data/planograms/store_ave.yaml, store_hill.yaml")."""
    import re

    import yaml

    planos = yaml.safe_load(open(map_path))["generated_from"]["planograms"]
    return set(re.findall(r"(store_[a-z0-9_]+)\.yaml", planos))


def _stocked_in(sku: str, store_ids: set[str]) -> bool:
    from utils.data import load_planogram

    return any(slot.sku == sku for sid in store_ids for slot in load_planogram(sid, str(REPO / "data")).slots)


# ---- scene: unit counts, ids, dims ---------------------------------------------------------------------------------
def test_unit_count_and_ids_per_slot_unchanged(assets: SkuAssets):
    plain = build_scene_spec(planogram(), products())
    modeled = build_scene_spec(planogram(), products(), assets=assets)
    assert [i.instance_id for i in modeled.instances] == [i.instance_id for i in plain.instances]
    assert [(i.slot_id, i.lot_id, i.freshness) for i in modeled.instances] == [
        (i.slot_id, i.lot_id, i.freshness) for i in plain.instances
    ]
    assert modeled.slot_layouts["s_salsa"].dims == (0.085, 0.085, 0.13)  # target_dims_m
    assert modeled.slot_layouts["s_salsa"].render == "asset"
    assert modeled.slot_layouts["s_tort_w"].render == "parts"
    assert modeled.slot_layouts["s_avo"].render == "primitive"  # missing file -> products.yaml dims
    assert modeled.slot_layouts["s_avo"].dims == plain.slot_layouts["s_avo"].dims
    renders = {p.instance_id: p.render for p in modeled.product_prims()}
    assert renders["s_salsa#00"] == "asset" and renders["s_avo#00"] == "primitive"
    salsa = [p for p in modeled.product_prims() if p.slot_id == "s_salsa"]
    assert all(p.size == (0.085, 0.085, 0.13) for p in salsa)
    # the front unit's base still sits on the board: center z = board + h/2
    assert salsa[0].center[2] == pytest.approx(0.55 + 0.13 / 2)
    assert all(p.render == "primitive" for p in plain.product_prims())


def test_real_stores_keep_every_unit_with_models():
    sa = SkuAssets.load(REPO / "data" / SKU_ASSETS_FILE, "/assets", exists=lambda p: True)
    plain = load_world_spec(data_root=str(REPO / "data"))
    modeled = load_world_spec(data_root=str(REPO / "data"), assets=sa)
    mapped_stores = _mapped_store_ids(REPO / "data" / SKU_ASSETS_FILE)
    for sid, spec in plain.stores.items():
        ids = [i.instance_id for i in spec.instances]
        assert ids == [i.instance_id for i in modeled.stores[sid].instances], sid
        per_slot = {s: len(spec.instances_in_slot(s)) for s in spec.slot_layouts}
        assert per_slot == {s: len(modeled.stores[sid].instances_in_slot(s)) for s in spec.slot_layouts}
        if sid not in mapped_stores:  # stores the map wasn't generated for keep every unit on the primitive path
            continue
        on_models = [p for p in modeled.stores[sid].product_prims() if p.render != "primitive"]
        assert len(on_models) > 0.8 * len(spec.instances), sid


def test_worker_restock_uses_model_dims(assets: SkuAssets):
    out: list = []
    backend = NullBackend()
    core = WorkerCore(backend, out.append, loader=loader, robots_loader=robots_loader, assets=assets)
    core.load_stores(["store_test"])
    assert any(getattr(m, "type", "") == "log" and "per-item models" in m.message for m in out)
    scene = core.stores["store_test"]
    scene.spec.slot_layouts.pop("s_salsa")  # force the restock path to recompute the layout
    core.submit(SetSlotStockCmd(cmd_id="up", store_id="store_test", slot_id="s_salsa", sku="salsa_hot", qty=8))
    assert scene.spec.slot_layouts["s_salsa"].dims == (0.085, 0.085, 0.13)
    new = backend.instance_prims[("store_test", "s_salsa#07")]
    assert new.render == "asset" and new.size == (0.085, 0.085, 0.13)
    assert len([i for i in scene.instances.values() if i.slot_id == "s_salsa"]) == 8


# ---- rover USD -----------------------------------------------------------------------------------------------------
def test_rover_reference_path_and_puppet_prims(tmp_path: Path):
    usd = tmp_path / "picky_rover.usda"
    usd.write_text("#usda 1.0\n")
    assert rover_usd_from_env({}) is None
    assert rover_usd_from_env({"PICKY_ROVER_USD": str(tmp_path / "missing.usda")}) is None
    assert rover_usd_from_env({"PICKY_ROVER_USD": str(usd)}) == str(usd)

    out: list = []
    backend = NullBackend()
    core = WorkerCore(backend, out.append, loader=loader, robots_loader=robots_loader, rover_usd=str(usd))
    core.load_stores(["store_test"])
    spec = backend.rovers["rover-test-1"]
    assert spec.model_usd == str(usd)
    assert spec.model_path == "/World/Stores/store_test/Robots/rover_test_1/Model/LeKiwi"
    kept = {p.path.rsplit("/", 1)[-1]: p for p in spec.puppet_prims}
    assert {"mast", "front_cam_head", "basket_floor", "basket_post", "carriage"} <= set(kept)
    assert not {"chassis", "wheel_0", "so101_upper_arm"} & set(kept)  # the USD draws base + arm
    assert all(p.group != "arm" for p in spec.puppet_prims) and kept["carriage"].group == "lift"
    for name in ("mast", "basket_post"):  # both stand on LeKiwi's deck
        p = kept[name]
        assert p.center[2] - p.size[2] / 2 == pytest.approx(puppet.DECK_Z_M)
    assert len(spec.prims) == len(build_rover_spec(robots()[0]).prims)  # primitive fallback stays complete
    plain = build_rover_spec(robots()[0])
    assert plain.model_usd is None and plain.model_path is None and plain.puppet_prims == []


def test_gripper_hook_follows_the_grasp():
    out: list = []
    backend = NullBackend()
    core = WorkerCore(backend, out.append, loader=loader, robots_loader=robots_loader)
    core.load_stores(["store_test"])
    rover = core.rovers["rover-test-1"]
    core.set_pose(rover, Pose2D(x=2.98, y=5.0, theta=math.pi))
    assert backend.grippers["rover-test-1"] is True  # stowed + empty: closed
    core.submit(PickCmd(cmd_id="p", robot_id="rover-test-1", slot_id="s_salsa", sku="salsa_hot"))
    seen = set()
    for _ in range(200):
        core.tick(0.05)
        seen.add((rover.arm_ext > ARM_STOW_EXT_M + 1e-6, rover.holding is not None, backend.grippers["rover-test-1"]))
    assert (True, False, False) in seen  # reaching: open
    assert (True, True, True) in seen  # retracting with the unit: closed


# ---- label decals --------------------------------------------------------------------------------------------------
def test_label_meshes_sit_on_the_aisle_face():
    dims = (0.1, 0.06, 0.2)
    panel = label_meshes(LabelSpec(mode="panel", cover_wh=(0.9, 0.8), center_z=0.5), dims)
    assert [m.name for m in panel] == ["Label"]
    ys = {p[1] for p in panel[0].points}
    assert len(ys) == 1 and ys.pop() < -0.03  # in front of the -y face
    assert max(p[0] for p in panel[0].points) == pytest.approx(0.045)
    assert len(label_meshes(LabelSpec(mode="panel", sleeve=True), dims)) == 4
    wrap = label_meshes(LabelSpec(mode="wrap", band_z=(0.1, 0.9)), dims)
    front, back = wrap
    assert front.textured and not back.textured and len(front.uvs) == len(front.points)
    assert all(p[1] <= 1e-9 for p in front.points) and all(p[1] >= -1e-9 for p in back.points)
    assert min(p[2] for p in front.points) == pytest.approx(0.02) and max(p[2] for p in front.points) == pytest.approx(
        0.18
    )
    top = label_meshes(LabelSpec(mode="top", cover_wd=(0.5, 0.5)), dims)
    assert [m.name for m in top] == ["Label", "LabelFront"] and all(p[2] > 0.2 for p in top[0].points)
    assert label_meshes(LabelSpec(mode="none"), dims) == []


def test_item_to_native_undoes_scale_and_yaw_offset():
    assert item_to_native((0.1, -0.05, 0.2), (2.0, 0.5, 4.0), 0.0) == pytest.approx((0.05, -0.1, 0.05))
    x, y, z = item_to_native((0.0, -1.0, 0.0), (1.0, 1.0, 1.0), 90.0)  # front -y in item = -x native (yaw 90)
    assert (x, y, z) == pytest.approx((-1.0, 0.0, 0.0))
    assert jitter_deg("s_avo#03", 35) == jitter_deg("s_avo#03", 35)
    assert -35 <= jitter_deg("s_avo#03", 35) <= 35 and jitter_deg("x", 0) == 0.0


def _flat(m: puppet.Mat) -> list[float]:
    return [v for row in m for v in row]


# ---- rover puppet math ---------------------------------------------------------------------------------------------
def _planar_arm() -> puppet.ArmChain:
    """Synthetic SO-101-like chain in a 'model' frame (x forward, z up): shoulder at the origin, upper arm 0.12 up,
    forearm 0.14 forward, wrist + gripper 0.10 forward; pitch joints about -y (positive = tip down)."""
    rest = {
        "shoulder": puppet.IDENTITY,
        "upper_arm": puppet.translation(0.0, 0.0, 0.05),
        "lower_arm": puppet.translation(0.0, 0.0, 0.17),
        "wrist": puppet.translation(0.14, 0.0, 0.17),
        "gripper": puppet.translation(0.18, 0.0, 0.17),
    }
    flip = puppet.axis_rotation("Z", math.pi)  # joint axis X turned to -X: positive pitch tips the arm down
    joints = [
        puppet.ArmJoint("shoulder_lift", "shoulder", "upper_arm", flip, "Y", -100, 100),
        puppet.ArmJoint("elbow_flex", "upper_arm", "lower_arm", flip, "Y", -100, 100),
        puppet.ArmJoint("wrist_flex", "lower_arm", "wrist", flip, "Y", -100, 100),
        puppet.ArmJoint("wrist_roll", "wrist", "gripper", puppet.IDENTITY, "X", -160, 160),
    ]
    return puppet.ArmChain(
        root="shoulder",
        rest=rest,
        joints=joints,
        root_joint=puppet.ArmJoint("shoulder_pan", "carriage", "shoulder", puppet.IDENTITY, "Z", -110, 110),
        tcp_link="gripper",
        tcp=(0.06, 0.0, 0.0),
    )


def test_fk_reproduces_rest_and_ik_reaches():
    chain = _planar_arm()
    poses = chain.fk({})
    for name, rest in chain.rest.items():
        assert _flat(poses[name]) == pytest.approx(_flat(rest))
    assert chain.tcp_point(poses) == pytest.approx((0.24, 0.0, 0.17))
    lifted = chain.fk({}, puppet.translation(0.0, 0.0, 0.5))
    assert chain.tcp_point(lifted)[2] == pytest.approx(0.67)
    res = puppet.solve_reach(chain, (0.2, 0.0, 0.12))
    assert res.error_m < 0.002
    p = chain.fk(res.q)
    assert chain.tcp_point(p)[2] == pytest.approx(puppet.position(p["wrist"])[2], abs=0.01)  # gripper level
    side = puppet.solve_reach(chain, (0.18, 0.06, 0.17))  # pan swings the arm sideways
    assert side.error_m < 0.003 and side.q["shoulder_pan"] > 0.1
    table = puppet.ReachTable.build(chain, lambda e, d: (e, 0.0, 0.17 - d), (0.15, 0.24), drops=(0.0, 0.04), samples=4)
    assert table.max_error_m < 0.005
    mid = table.config(0.195, 0.02)
    assert set(mid) >= {"shoulder_lift", "elbow_flex", "wrist_flex"}
    assert chain.tcp_point(chain.fk(mid))[0] == pytest.approx(0.195, abs=0.01)


def test_puppet_frames_and_lift():
    m = puppet.model_matrix()  # lekiwi's arm reaches along -Y of its root: heading offset maps it to robot +x
    assert puppet.rotate(m, (0.0, -1.0, 0.0)) == pytest.approx((1.0, 0.0, 0.0))
    assert puppet.apply(m, (0.0, 0.0, 0.0))[2] == pytest.approx(puppet.FLOOR_OFFSET_M)
    assert puppet.lift_offset(0.2, 0.32) == 0.0 and puppet.lift_offset(0.9, 0.32) == pytest.approx(0.58)
    assert puppet.lift_offset(5.0, 0.32) == puppet.LIFT_TRAVEL_M
    d = puppet.robot_delta_in_model(m, (0.1, 0.0, 0.3))
    assert puppet.rotate(m, puppet.position(d)) == pytest.approx((0.1, 0.0, 0.3))
    q = puppet.quat_pose(math.cos(math.pi / 4), 0.0, 0.0, math.sin(math.pi / 4))  # 90 deg about z
    assert puppet.rotate(q, (1.0, 0.0, 0.0)) == pytest.approx((0.0, 1.0, 0.0))
    assert _flat(puppet.mat_mul(q, puppet.inverse(q))) == pytest.approx(_flat(puppet.IDENTITY))
    tcp = puppet.tcp_from_tips((0.4, 0.01, 0.3), (0.4, -0.01, 0.3), (0.2, 0.0, 0.3), back_m=0.02)
    assert tcp == pytest.approx((0.38, 0.0, 0.3))
    assert puppet.names_ok(["base", *puppet.ARM_LINKS]) and not puppet.names_ok(["base", "shoulder"])
    assert ARM_MOUNT_X_M + ARM_STOW_EXT_M < ARM_MOUNT_X_M + ARM_FULL_EXT_M


# ---- fallbacks without Isaac: CLI config, Pillow sketch, Isaac backend failure paths (stubbed stage) ---------------
def _args(**kw):
    import argparse

    base = {"backend": "isaac", "asset_root": "", "sku_assets": "", "no_assets": False, "rover_usd": ""}
    return argparse.Namespace(**{**base, **kw})


def test_cli_visual_config_degrades(tmp_path: Path, asset_root: Path, monkeypatch: pytest.MonkeyPatch):
    import yaml

    import arms.sim.app as app

    monkeypatch.delenv("PICKY_SIM_ASSETS", raising=False)
    m = tmp_path / "map.yaml"
    m.write_text(yaml.safe_dump({"skus": entries()}))
    usd = tmp_path / "picky_rover.usda"
    usd.write_text("#usda 1.0\n")
    assets, rover = app.visual_config(_args())  # nothing set: primitives everywhere
    assert not assets.active and rover is None
    assets, rover = app.visual_config(_args(asset_root=str(asset_root), sku_assets=str(m), rover_usd=str(usd)))
    assert assets.active and assets.resolve("salsa_hot") is not None and rover == str(usd)
    assets, _ = app.visual_config(_args(asset_root=str(asset_root), sku_assets=str(m), no_assets=True))
    assert not assets.active and assets.resolve("salsa_hot") is None  # --no-assets wins over the asset root
    monkeypatch.setenv("PICKY_SIM_ASSETS", "off")
    assert not app.visual_config(_args(asset_root=str(asset_root), sku_assets=str(m)))[0].active
    _, rover = app.visual_config(_args(backend="sketch", rover_usd=str(usd)))
    assert rover is None  # the rover USD is Isaac-only
    _, rover = app.visual_config(_args(rover_usd=str(tmp_path / "missing.usda")))
    assert rover is None


def test_sketch_backend_renders_with_models_and_rover_usd_set(assets: SkuAssets, tmp_path: Path):
    """Without Isaac the same specs (model dims, rover USD plan) still render as primitives."""
    pytest.importorskip("PIL")
    import base64
    import io

    from PIL import Image

    from arms.sim.sketch import SketchBackend

    usd = tmp_path / "picky_rover.usda"
    usd.write_text("#usda 1.0\n")
    out: list = []
    backend = SketchBackend()
    core = WorkerCore(
        backend, out.append, loader=loader, robots_loader=robots_loader, assets=assets, rover_usd=str(usd)
    )
    core.load_stores(["store_test"])
    assert backend.rovers["rover-test-1"].model_usd == str(usd) and backend.rovers["rover-test-1"].prims
    for cid in ("rover-test-1.front", "rover-test-1.wrist", "rover-test-1.basket"):
        frame = core.capture(cid)
        img = Image.open(io.BytesIO(base64.b64decode(frame.jpeg_b64)))
        assert img.size == (frame.w, frame.h)
        colors = img.convert("RGB").getcolors(maxcolors=1_000_000)
        assert colors is not None and len(colors) > 20, cid


class _Stage:
    """Just enough of a Usd.Stage for the backend's fallback paths."""

    def __init__(self, children: tuple[str, ...] = ()) -> None:
        self.removed: list[str] = []
        self.children = children

    def GetPrimAtPath(self, path: str):  # noqa: N802 - pxr API
        return True

    def RemovePrim(self, path: str) -> None:  # noqa: N802 - pxr API
        self.removed.append(path)

    def DefinePrim(self, path: str, kind: str = ""):  # noqa: N802 - pxr API
        from unittest.mock import MagicMock

        prim = MagicMock()
        kids = []
        for name in self.children:
            kid = MagicMock()
            kid.GetName.return_value = name
            kids.append(kid)
        prim.GetChildren.return_value = kids
        return prim


def test_isaac_unit_falls_back_to_the_primitive_when_the_model_fails(assets: SkuAssets):
    import arms.sim.app as app

    spec = build_scene_spec(planogram(), products(), assets=assets)
    salsa = next(p for p in spec.product_prims() if p.slot_id == "s_salsa")
    avo = next(p for p in spec.product_prims() if p.slot_id == "s_avo")
    assert salsa.render == "asset" and avo.render == "primitive"
    be = object.__new__(app.IsaacBackend)  # no Kit: only the fallback logic runs
    be.assets, be.stage, be._units = assets, _Stage(), {}
    calls: list[str] = []

    def broken_model(spec_, prim, ua):
        calls.append(prim.path)
        be._units[(prim.store_id or "", prim.instance_id or "")] = ("half-built",)
        raise RuntimeError("bad usd")

    be._define_model_unit = broken_model
    be._define_primitive_unit = lambda spec_, prim: f"primitive:{prim.path}"
    assert be._define_unit(spec, salsa) == f"primitive:{salsa.path}"
    assert calls == [salsa.path] and be.stage.removed == [salsa.path]  # the half-built model prim is removed
    assert not be._units  # and forgotten, so set_instance_pose moves the primitive
    assert be._define_unit(spec, avo) == f"primitive:{avo.path}" and calls == [salsa.path]
    assert be._define_unit(None, salsa) == f"primitive:{salsa.path}"  # no scene spec (late add_instance)


def test_isaac_rover_usd_without_so101_links_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import sys
    import types
    from unittest.mock import MagicMock

    import arms.sim.app as app

    fake_pxr = types.SimpleNamespace(Gf=MagicMock(), Usd=MagicMock(), UsdGeom=MagicMock(), UsdPhysics=MagicMock())
    monkeypatch.setitem(sys.modules, "pxr", fake_pxr)
    usd = tmp_path / "picky_rover.usda"
    usd.write_text("#usda 1.0\n")
    rover = build_rover_spec(robots()[0], model_usd=str(usd))
    be = object.__new__(app.IsaacBackend)
    be.stage, be._puppet_geo = _Stage(children=("base_link", "wheel_0")), {}
    assert be._spawn_puppet(rover) is None  # caller then draws rover.prims (the full primitive rover)
    assert be.stage.removed == [f"{rover.root_path}/{puppet.MODEL_PRIM_NAME}"]  # nothing of the USD left behind
    assert rover.prims and len(rover.prims) > len(rover.puppet_prims)
