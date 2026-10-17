"""Robot arm: RobotService implementations (real over the Isaac SimLink, fake = 2D kinematic sim).

Public API (the gateway and arms/sim/app.py import only these):
- ``create(services)`` -> ``SimRobot`` (real; uses ``services.sim_link`` or ``attach_link(link)``)
- ``create_fake(services, **opts)`` -> ``FakeRobot`` (opts: realtime_factor, speed_mps, publish_state_events, ...)
- ``PurePursuit`` / ``PurePursuitConfig`` (pure-Python follower, also used inside Isaac)
- ``build_instances`` / ``instance_id`` (deterministic ground-truth instances from a planogram)
- ``LoopbackSimLink`` (SimLink executing on a FakeRobot; tests and GPU-less runs)
- Isaac side (the robot arm owns the Isaac robot; ``arms/sim/app.py`` hosts it): ``create_isaac_robot(prim_path)``
  -> loaded ``RidgebackFranka``, plus the per-physics-step actions ``NavigateAction`` / ``MagicGraspAction`` /
  ``PlaceAction`` / ``ActionStatus`` and ``RIDGEBACK_FRANKA_USD``. Importing them is free (Isaac imports are lazy);
  call them only inside the sim worker after ``SimulationApp`` exists.
"""

from typing import Any

from arms.robot.controllers.pure_pursuit import PurePursuit, PurePursuitConfig
from arms.robot.fake import FakeRobot
from arms.robot.instances import build_instances, instance_id
from arms.robot.isaac import (
    RIDGEBACK_FRANKA_USD,
    ActionStatus,
    MagicGraspAction,
    NavigateAction,
    PlaceAction,
    RidgebackFranka,
)
from arms.robot.loopback import LoopbackSimLink
from arms.robot.service import SimRobot
from contracts.services import Services


def create(services: Services) -> SimRobot:
    return SimRobot(services)


def create_fake(services: Services, **opts: Any) -> FakeRobot:
    return FakeRobot(services, **opts)


def create_isaac_robot(prim_path: str = "/World/Robot/Body") -> RidgebackFranka:
    """Isaac only: add the Ridgeback-Franka under ``prim_path`` and return its handle (raises if the asset is
    unreachable, so the sim worker can fall back to its proxy robot). The worker moves the parent xform."""
    robot = RidgebackFranka(prim_path)
    robot.load()
    return robot


__all__ = [
    "RIDGEBACK_FRANKA_USD",
    "ActionStatus",
    "FakeRobot",
    "LoopbackSimLink",
    "MagicGraspAction",
    "NavigateAction",
    "PlaceAction",
    "PurePursuit",
    "PurePursuitConfig",
    "RidgebackFranka",
    "SimRobot",
    "build_instances",
    "create",
    "create_fake",
    "create_isaac_robot",
    "instance_id",
]
