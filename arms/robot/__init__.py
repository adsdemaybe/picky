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
- Isaac side (the robot arm owns the Isaac robot; ``arms/sim/app.py`` hosts it): ``create_isaac_robot(prim_path)``
  -> loaded ``RoverSO101``, plus the per-physics-step actions ``NavigateAction`` / ``LiftAction`` /
  ``MagicGraspAction`` / ``PlaceAction`` / ``UnloadAction`` / ``ActionStatus``. Importing them is free (Isaac imports
  are lazy); call them only inside the sim worker after ``SimulationApp`` exists. ``RidgebackFranka`` /
  ``RIDGEBACK_FRANKA_USD`` remain as v0.1 aliases.
"""

from typing import Any

from arms.robot.controllers.pure_pursuit import PurePursuit, PurePursuitConfig
from arms.robot.fake import FakeRobot
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
from contracts.services import Services


def create(services: Services, **opts: Any) -> SimRobot:
    return SimRobot(services, **opts)


def create_fake(services: Services, **opts: Any) -> FakeRobot:
    return FakeRobot(services, **opts)


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
    "LiftAction",
    "LoopbackSimLink",
    "MagicGraspAction",
    "NavigateAction",
    "PlaceAction",
    "PurePursuit",
    "PurePursuitConfig",
    "RidgebackFranka",
    "RoverSO101",
    "SimRobot",
    "UnloadAction",
    "build_instances",
    "create",
    "create_fake",
    "create_isaac_robot",
    "instance_id",
    "lift_for",
    "load_specs",
    "render_basket",
    "render_ceiling",
    "render_front",
    "render_wrist",
    "resync_slot",
]
