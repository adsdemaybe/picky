"""FaultInjector (scenarios, occurrence counting, demo.fault events) and RobotTruth (the ground truth the verifier
judges from) over the robot arm's 2D fake."""

import pytest

from arms.robot import FakeRobot, InMemoryFaultInjector, RobotTruth, create_faults, create_truth
from arms.robot.faults import scenario_from_settings
from arms.robot.tests.conftest import R1, STORE, UP, fleet_opts
from arms.robot.truth import label_text
from contracts.catalog import Product
from contracts.common import Pose2D
from contracts.evidence import GroundTruthSidecar
from contracts.faults import DEMO_SCENARIOS, FaultContext, FaultMatch, FaultRule
from contracts.services import FaultInjector, Services, TruthSource

AT_SALSA = Pose2D(x=3.0, y=3.62, theta=UP)


def ctx(step: str, attempt: int = 1, **kw) -> FaultContext:  # noqa: ANN003
    return FaultContext(robot_id=R1, step_kind="grasp", step_id=step, step_key=f"p:{step}", attempt=attempt, **kw)


async def test_occurrence_is_counted_over_distinct_steps():
    inj = InMemoryFaultInjector()
    await inj.inject(
        FaultRule(
            fault_id="f", effect="grasp_miss", match=FaultMatch(step_kind="grasp", occurrence=2, attempt=1), times=0
        )
    )
    got = [
        len(await inj.consult(ctx(s, a), ["grasp_miss"]))
        for s, a in (("s1", 1), ("s1", 2), ("s2", 1), ("s2", 2), ("s3", 1))
    ]
    assert got == [0, 0, 1, 0, 0]
    assert DEMO_SCENARIOS["drop_on_first_grasp"].rules[0].match.occurrence == 1


async def test_times_caps_firings_and_effects_filter():
    inj = InMemoryFaultInjector("drop_on_first_grasp")
    assert inj.scenario == "drop_on_first_grasp" and len(inj.rules) == 1
    assert await inj.consult(ctx("s1"), ["grasp_miss"]) == []  # not one of the asked effects
    assert len(await inj.consult(ctx("s1"), ["drop_after_lift"])) == 1
    assert await inj.consult(ctx("s1"), ["drop_after_lift"]) == []  # times=1
    assert [f.effect for f in await inj.firings()] == ["drop_after_lift"]


async def test_scenarios_load_clear_and_unknown_names(caplog):
    inj = InMemoryFaultInjector("no_such_scenario")  # PICKY_FAULTS typo: warn, no rules, never crash
    assert inj.rules == [] and inj.scenario is None
    with pytest.raises(KeyError):
        await inj.load_scenario("no_such_scenario")
    sc = await inj.load_scenario("slow_vla")
    assert sc.name == "slow_vla" and inj.rule("f-hang-1") is not None
    inj.overlay.mislabels["x#00"] = "Hot"
    await inj.clear_faults()
    assert inj.rules == [] and inj.scenario is None and inj.overlay.mislabels == {}
    await inj.inject(FaultRule(fault_id="a", effect="grasp_miss"))
    await inj.inject(FaultRule(fault_id="a", effect="place_miss"))  # same id replaces
    assert [r.effect for r in inj.rules] == ["place_miss"]


async def test_demo_fault_events_carry_the_robot_envelope(services):
    inj = create_faults(services, scenario="drop_on_first_grasp")
    assert isinstance(inj, FaultInjector) and services.events.events == []
    inj.bind_robot(R1, trip_id="trip-9", store_id=STORE)
    await inj.consult(ctx("s1"), ["drop_after_lift"])
    (ev,) = services.events.events
    assert ev.type == "demo.fault" and ev.source == "robot" and ev.trip_id == "trip-9" and ev.store_id == STORE
    assert ev.robot_id == R1 and ev.scenario == "drop_on_first_grasp" and ev.firing.context.occurrence == 1


async def test_a_failing_bus_never_breaks_consult():
    class Broken:
        async def publish(self, event):  # noqa: ANN001, ANN201
            raise RuntimeError("bus down")

    inj = InMemoryFaultInjector("drop_on_first_grasp", publisher=Broken())
    assert len(await inj.consult(ctx("s1"), ["drop_after_lift"])) == 1


def test_scenario_from_settings_is_optional():
    from utils.config import reset_settings, set_settings

    set_settings(picky_faults="")
    assert scenario_from_settings() is None
    set_settings(picky_faults=" slow_vla ")
    assert scenario_from_settings() == "slow_vla"
    assert create_faults(Services()).scenario == "slow_vla"
    reset_settings()


# ---- truth -----------------------------------------------------------------------------------------------------------
def wired(services: Services) -> Services:
    services.robot = FakeRobot(services, **fleet_opts())
    services.faults = InMemoryFaultInjector()
    services.truth = create_truth(services)
    return services


async def test_truth_reports_pose_facing_slots_tags_and_labels(services):
    wired(services)
    assert isinstance(services.truth, TruthSource)
    await services.robot.navigate(R1, [AT_SALSA])
    t = await services.truth.truth(R1, ["S-limes"])
    assert t.store_id == STORE and t.source == "fake" and t.pose.distance_to(AT_SALSA) < 1e-6
    assert t.visible_slots == {f"{R1}.front": ["S-salsa"]} and set(t.slots) == {"S-limes", "S-salsa"}
    assert len(t.slots["S-salsa"].instances) == 4 and t.slots["S-limes"].instances == []  # limes are out of stock
    assert t.slots["S-salsa"].shelf_tag_cents == 399 and t.labels["S-salsa#00"] == "Mild Salsa"
    assert t.truth_hash() == (await services.truth.truth(R1, ["S-limes"])).truth_hash()  # deterministic
    with pytest.raises(KeyError):
        await services.truth.truth("rover-nope")


async def test_truth_applies_the_overlay(services):
    wired(services)
    await services.robot.navigate(R1, [AT_SALSA])
    unit = (await services.robot.list_instances(R1, "S-avocado"))[0]
    services.faults.overlay.virtual_holding[R1] = unit
    services.faults.overlay.mislabels[unit.instance_id] = "Hass Avocado (Organic)"
    t = await services.truth.truth(R1, ["S-avocado"])
    assert t.holding == unit and t.labels[unit.instance_id] == "Hass Avocado (Organic)"
    assert unit.instance_id not in {u.instance_id for u in t.slots["S-avocado"].instances}
    services.faults.overlay.virtual_holding.clear()
    services.faults.overlay.virtual_basket[R1] = [unit.model_copy(update={"in_basket": True})]
    assert (await services.truth.truth(R1)).basket_counts() == {"avocado": 1}


async def test_ground_truth_serves_only_remembered_sidecars(services):
    wired(services)
    with pytest.raises(KeyError):
        await services.truth.ground_truth("someone-elses-frame")
    t = await services.truth.truth(R1)
    side = GroundTruthSidecar(frame_id="f1", camera_id=f"{R1}.wrist", robot_id=R1, truth=t)
    services.truth.remember(side)
    assert (await services.truth.ground_truth("f1")) == side


def test_label_text():
    p = Product(
        sku="s",
        name="Mild Salsa",
        brand="Sol de Casa",
        size="16 oz",
        category="x",
        price_cents=399,
        barcode="0000000000017",
    )
    assert label_text(p, "s") == "Sol de Casa Mild Salsa 16 oz" and label_text(None, "s") == "s"


async def test_truth_needs_a_robot_service():
    with pytest.raises(RuntimeError):
        await RobotTruth(Services()).truth(R1)
