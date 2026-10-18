"""Robot arm: MULTI-ROBOT RobotService (real = Isaac over the multiplexed SimLink with the 2D fake built in as the
per-store fallback; fake = 2D kinematic sim of every robot in data/fixtures/robots.yaml with Pillow camera frames).

Public API (the gateway and arms/sim/app.py import only these):
- ``create(services, **opts)`` -> ``SimRobot`` (real; ``services.sim_link`` at call time or ``attach_link(link)``;
  opts: ``stream_fps``, ``fallback`` and any ``FakeRobot`` option for the built-in fake)
- ``create_fake(services, **opts)`` -> ``FakeRobot`` (opts: realtime_factor, speed_mps, stream_fps, robots,
  store_cameras, strict_reach, publish_state_events, ...)
- ``PurePursuit`` / ``PurePursuitConfig`` (pure-Python follower, also used inside Isaac)
- ``build_instances`` / ``instance_id`` / ``resync_slot`` (deterministic ground-truth instances from a planogram)
- ``LoopbackSimLink`` (SimLink whose "worker" is an in-process FakeRobot; tests and GPU-less runs)
- rendering helpers ``render_front`` / ``render_wrist`` / ``render_basket`` / ``render_ceiling`` (Pillow)
- planner -> VLA -> verifier loop (contracts 0.2.1, ``LOOP_SERVICE_FACTORIES``):
  ``create_vla`` / ``create_vla_fake`` -> ``T0Vla`` (``VlaService``: T0 over this RobotService, 2D fake and Isaac alike;
  real = T1 adapter from settings, fake = T1 only with ``t1_sim``), ``create_truth`` / ``create_truth_fake`` ->
  ``RobotTruth`` (``TruthSource``), ``create_faults`` / ``create_faults_fake`` -> ``InMemoryFaultInjector``
  (``FaultInjector``, scenario from PICKY_FAULTS). Every factory takes ``services`` only and calls nothing at
  construction.
- Isaac side (the robot arm owns the Isaac robot; ``arms/sim/app.py`` hosts it): ``create_isaac_robot(prim_path)``
  -> loaded ``RoverSO101``, plus the per-physics-step actions ``NavigateAction`` / ``LiftAction`` /
  ``MagicGraspAction`` / ``PlaceAction`` / ``UnloadAction`` / ``ActionStatus``. Importing them is free (Isaac imports
  are lazy); call them only inside the sim worker after ``SimulationApp`` exists. ``RidgebackFranka`` /
  ``RIDGEBACK_FRANKA_USD`` remain as v0.1 aliases.
"""

from typing import Any

from arms.robot.controllers.pure_pursuit import PurePursuit, PurePursuitConfig
from arms.robot.fake import FakeRobot
from arms.robot.faults import FaultOverlay, InMemoryFaultInjector, scenario_from_settings
from arms.robot.instances import build_instances, instance_id, resync_slot
from arms.robot.isaac import (
    RIDGEBACK_FRANKA_USD,
    SO101_URDF,
    ActionStatus,
    LiftAction,
    MagicGraspAction,
    NavigateAction,
    PlaceAction,
    RidgebackFranka,
    RoverSO101,
    UnloadAction,
)
from arms.robot.loopback import LoopbackSimLink
from arms.robot.render import render_basket, render_ceiling, render_front, render_wrist
from arms.robot.rover import lift_for, load_specs
from arms.robot.service import SimRobot
from arms.robot.t1 import T1Adapter, T1Config
from arms.robot.truth import RobotTruth
from arms.robot.vla import T0Vla
from contracts.faults import FaultScenario
from contracts.services import Services


def create(services: Services, **opts: Any) -> SimRobot:
    return SimRobot(services, **opts)


def create_fake(services: Services, **opts: Any) -> FakeRobot:
    return FakeRobot(services, **opts)


def create_vla(services: Services, *, t1: T1Adapter | None = None) -> T0Vla:
    """Real VlaService: T0 over ``services.robot`` (SimRobot = Isaac or the built-in fake per store) + the T1 adapter
    from settings (PICKY_VLA_TIERS / PICKY_VLA_POLICY_PROTOCOL / ...; unavailable unless configured)."""
    return T0Vla(services, t1=t1 or T1Adapter.from_settings())


def create_vla_fake(services: Services, *, simulate_t1: bool | None = None) -> T0Vla:
    """Fake VlaService: the same T0 code over the wired (fake) RobotService. T1 is offered only as the simulated tier
    (``simulate_t1=True`` or ``t1_sim`` in PICKY_VLA_TIERS: the breaker demo, scenario ``flaky_t1``)."""
    if simulate_t1 is None:
        simulate_t1 = T1Config.from_settings().backend == "simulated"
    return T0Vla(services, t1=T1Adapter(T1Config.simulated() if simulate_t1 else T1Config()))


def create_truth(services: Services) -> RobotTruth:
    """TruthSource over ``services.robot`` (+ the fault overlay of ``services.faults``)."""
    return RobotTruth(services)


def create_truth_fake(services: Services) -> RobotTruth:
    return RobotTruth(services)


def create_faults(services: Services, *, scenario: FaultScenario | str | None = None) -> InMemoryFaultInjector:
    """FaultInjector; ``scenario`` defaults to PICKY_FAULTS (unset = no rules). Publishes ``demo.fault`` on
    ``services.events`` (read at call time)."""
    return InMemoryFaultInjector(scenario if scenario is not None else scenario_from_settings(), services=services)


def create_faults_fake(services: Services, *, scenario: FaultScenario | str | None = None) -> InMemoryFaultInjector:
    return create_faults(services, scenario=scenario)


def create_isaac_robot(prim_path: str = "/World/Robot/Body", robot_id: str | None = None) -> RoverSO101:
    """Isaac only: build the SO-101 rover under ``prim_path`` and return its handle (the SO-101 URDF is imported when
    PICKY_SO101_DIR is set, else a proxy arm is drawn). The worker moves it with ``set_base_pose`` / ``set_lift``."""
    robot = RoverSO101(prim_path, robot_id=robot_id)
    robot.load()
    return robot


__all__ = [
    "RIDGEBACK_FRANKA_USD",
    "SO101_URDF",
    "ActionStatus",
    "FakeRobot",
    "FaultOverlay",
    "InMemoryFaultInjector",
    "LiftAction",
    "LoopbackSimLink",
    "MagicGraspAction",
    "NavigateAction",
    "PlaceAction",
    "PurePursuit",
    "PurePursuitConfig",
    "RidgebackFranka",
    "RobotTruth",
    "RoverSO101",
    "SimRobot",
    "T0Vla",
    "T1Adapter",
    "T1Config",
    "UnloadAction",
    "build_instances",
    "create",
    "create_fake",
    "create_faults",
    "create_faults_fake",
    "create_isaac_robot",
    "create_truth",
    "create_truth_fake",
    "create_vla",
    "create_vla_fake",
    "instance_id",
    "lift_for",
    "load_specs",
    "render_basket",
    "render_ceiling",
    "render_front",
    "render_wrist",
    "resync_slot",
]
