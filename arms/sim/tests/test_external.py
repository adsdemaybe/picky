"""External scenes (arms.sim.external): a store drawn by its own USD, units = the USD's product prims."""

import math

import pytest
import yaml

from arms.sim.external import ExternalScene, item_dims, load_external_scene
from arms.sim.scene_spec import build_scene_spec, unit_prim
from arms.sim.tests.sim_fixtures import loader, planogram, products, robots_loader
from arms.sim.worker import NullBackend, WorkerCore
from contracts.common import Pose2D
from contracts.sim import CommandResult, PickCmd, PlaceInBasketCmd, SetSlotStockCmd, UnloadBasketCmd

R1 = "rover-test-1"
SALSA_AP = Pose2D(x=2.98, y=5.0, theta=math.pi)
# s_salsa faces -x (approach theta = pi): item yaw = pi/2, so the item's width runs along world y
SALSA_UNITS = [
    {
        "path": f"background/Group_01/salsa_{n:03d}",
        "c": [2.55 - 0.1 * (n // 3), 5.0 + (0.0, 0.1, -0.1)[n % 3], 0.61],
        "s": [0.07, 0.09, 0.12],
    }
    for n in range(4)
]
DOC = {
    "schema": "picky external scene v1",
    "store_id": "store_test",
    "usd": {
        "root_env": "TEST_SCENE_ROOT",
        "path": "market/scene.usda",
        "default_prim": "/World",
        "translate": [3, 2, 0],
    },
    "fixtures_in_usd": ["f1", "g1", "p1"],
    "slots": {"s_salsa": SALSA_UNITS},
}


def scene(usd: str | None = "/scenes/market/scene.usda") -> ExternalScene:
    return ExternalScene.model_validate({**DOC, "usd_file": usd})


@pytest.fixture
def data_root(tmp_path):
    (tmp_path / "sim").mkdir()
    (tmp_path / "sim" / "store_test_prims.yaml").write_text(yaml.safe_dump(DOC))
    usd = tmp_path / "scenes" / "market" / "scene.usda"
    usd.parent.mkdir(parents=True)
    usd.write_text("#usda 1.0\n")
    return tmp_path


def test_load_resolves_the_usd_on_this_host_only(data_root):
    env = {"TEST_SCENE_ROOT": str(data_root / "scenes")}
    sc = load_external_scene("store_test", data_root, env)
    assert sc is not None and sc.active and sc.usd_file == str(data_root / "scenes/market/scene.usda")
    assert [u.path for u in sc.units("s_salsa")] == [u["path"] for u in SALSA_UNITS]
    assert not load_external_scene("store_test", data_root, {}).active  # USD missing here: primitives
    override = {"PICKY_EXTERNAL_USD_STORE_TEST": str(data_root / "scenes/market/scene.usda")}
    assert load_external_scene("store_test", data_root, override).active
    assert load_external_scene("store_test", data_root, {**env, "PICKY_EXTERNAL_SCENES": "0"}) is None
    assert load_external_scene("store_ave", data_root, env) is None  # no prim map = not external


def test_item_dims_follow_the_shelf_direction():
    u = scene().units("s_salsa")[0]
    assert item_dims(u, math.pi / 2) == (0.09, 0.07, 0.12)  # width along world y
    assert item_dims(u, 0.0) == (0.07, 0.09, 0.12)


def test_spec_keeps_the_ids_and_points_units_at_usd_prims():
    plain = build_scene_spec(planogram(), products())
    ext = build_scene_spec(planogram(), products(), external=scene())
    assert ext.data_hash == plain.data_hash  # the gateway's hash check never depends on the host
    assert [i.instance_id for i in ext.instances] == [i.instance_id for i in plain.instances]
    assert ext.external_usd == "/scenes/market/scene.usda" and ext.external_root == f"{ext.root_path}/Scene"
    assert ext.external_translate == (3, 2, 0)
    roles = {p.role for p in ext.prims}
    assert not roles & {"floor", "wall", "ceiling", "light"}  # the USD is the building
    fixtures = {p.fixture_id for p in ext.prims if p.role in ("board", "panel", "base") and p.fixture_id}
    assert fixtures == {"co"}  # only the checkout counter (not in the USD) is drawn
    salsa = [p for p in ext.product_prims() if p.slot_id == "s_salsa"]
    assert len(salsa) == 6  # qty 6, 4 mapped prims: units #04 / #05 are primitive boxes behind them
    assert [p.render for p in salsa] == ["external"] * 4 + ["primitive"] * 2
    assert [p.external_path for p in salsa[:4]] == [u["path"] for u in SALSA_UNITS]
    inst = {i.instance_id: i for i in ext.instances}
    assert (inst["s_salsa#00"].position.x, inst["s_salsa#00"].position.z) == (2.55, 0.61)
    assert ext.item_dims["s_salsa#00"] == (0.09, 0.07, 0.12)
    assert ext.external_hidden == []
    tort = [p for p in ext.product_prims() if p.slot_id == "s_tort_w"]
    assert tort and all(p.render == "primitive" for p in tort)  # unmapped slots keep primitives


def test_spare_prims_are_hidden_and_inactive_scene_is_plain():
    plan = planogram()
    slot = plan.slot("s_salsa")
    slot.qty, slot.lots = 2, []
    ext = build_scene_spec(plan, products(), external=scene())
    assert ext.external_hidden == [u["path"] for u in SALSA_UNITS[2:]]
    off = build_scene_spec(planogram(), products(), external=scene(usd=None))
    assert off.external_usd is None and any(p.role == "floor" for p in off.prims)


def test_restock_maps_instance_index_to_its_prim():
    ext = build_scene_spec(planogram(), products(), external=scene())
    layout = ext.slot_layouts["s_salsa"]
    p = unit_prim("store_test", layout, "s_salsa#02", 0, None, None)
    assert p.render == "external" and p.external_path == SALSA_UNITS[2]["path"]
    assert p.center == tuple(SALSA_UNITS[0]["c"])  # rank 0 = the front position
    assert unit_prim("store_test", layout, "s_salsa#07", 5, None, None).render == "primitive"


def test_worker_picks_and_unloads_usd_units():
    out: list = []
    t = [0.0]
    backend = NullBackend()
    core = WorkerCore(
        backend,
        out.append,
        clock=lambda: t[0],
        loader=loader,
        robots_loader=robots_loader,
        external_loader=lambda sid: scene() if sid == "store_test" else None,
    )
    core.load_stores(["store_test"])
    assert any("external USD /scenes/market/scene.usda (4 of" in getattr(m, "message", "") for m in out)

    def run(cmd):
        core.submit(cmd)
        for _ in range(4000):
            if any(isinstance(m, CommandResult) and m.cmd_id == cmd.cmd_id for m in out):
                break
            t[0] += 0.05
            core.tick(0.05)
        return next(m for m in out if isinstance(m, CommandResult) and m.cmd_id == cmd.cmd_id)

    core.rovers[R1].pose = SALSA_AP
    pick = run(PickCmd(cmd_id="p", robot_id=R1, slot_id="s_salsa", sku="salsa_hot"))
    assert pick.ok and pick.instance_id == "s_salsa#00"  # the front prim, nearest the approach pose
    assert run(PlaceInBasketCmd(cmd_id="pl", robot_id=R1)).ok
    assert backend.instance_prims[("store_test", "s_salsa#00")].external_path == SALSA_UNITS[0]["path"]
    unload = run(UnloadBasketCmd(cmd_id="u", robot_id=R1))
    assert [i.instance_id for i in unload.instances] == ["s_salsa#00"]
    assert ("store_test", "s_salsa#00") not in backend.instance_prims
    up = run(SetSlotStockCmd(cmd_id="up", store_id="store_test", slot_id="s_salsa", sku="salsa_hot", qty=6))
    assert up.ok and "s_salsa#00" in [i.instance_id for i in up.instances]
    assert backend.instance_prims[("store_test", "s_salsa#00")].external_path == SALSA_UNITS[0]["path"]
