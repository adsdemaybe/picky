"""Isaac Sim 6.2 side of the robot arm: the SO-101 rover handle (kinematic base + lift + SO-101), base navigation,
lift, magic grasp, basket place / unload.

Runs ONLY inside the sim worker process (``arms/sim/app.py``, Python 3.12, after ``SimulationApp`` is created).
Every ``isaacsim`` / ``omni`` / ``pxr`` import is lazy (inside functions), so importing this package is free and the
pure-Python parts (action state machines) are unit-tested in the backend.

The sim main loop owns stepping: each physics step it calls ``action.step(dt)`` on every robot's active action
(non-blocking state machines, never ``await``; one per robot, robots step concurrently), then publishes per-robot
RobotState / frames.
"""

from arms.robot.isaac.actions import (
    ActionStatus,
    LiftAction,
    MagicGraspAction,
    NavigateAction,
    PlaceAction,
    UnloadAction,
)
from arms.robot.isaac.rover_so101 import (
    RIDGEBACK_FRANKA_USD,
    SO101_URDF,
    RidgebackFranka,
    RoverSO101,
    attach_item,
    basket_world,
)

__all__ = [
    "RIDGEBACK_FRANKA_USD",
    "SO101_URDF",
    "ActionStatus",
    "LiftAction",
    "MagicGraspAction",
    "NavigateAction",
    "PlaceAction",
    "RidgebackFranka",
    "RoverSO101",
    "UnloadAction",
    "attach_item",
    "basket_world",
]
