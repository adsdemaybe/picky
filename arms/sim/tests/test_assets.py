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
    products_ = load_products(str(REPO / "data"))
    assert set(sa.entries) == set(products_)
    renders = [e["render"] for e in sa.entries.values()]
    assert renders.count("asset") + renders.count("parts") + renders.count("primitive") == len(products_)
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
    for sid, spec in plain.stores.items():
        ids = [i.instance_id for i in spec.instances]
        assert ids == [i.instance_id for i in modeled.stores[sid].instances], sid
        per_slot = {s: len(spec.instances_in_slot(s)) for s in spec.slot_layouts}
        assert per_slot == {s: len(modeled.stores[sid].instances_in_slot(s)) for s in spec.slot_layouts}
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
