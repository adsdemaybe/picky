"""sim arm: the Isaac Sim worker process (multi-store, multi-rover; no backend Service).

Unlike the other arms there is no ``create(services)`` / ``create_fake(services)``: the backend-facing Protocol is
``RobotService`` (owned by the robot arm, whose real impl drives this worker through the gateway's ``SimLink`` and
whose fake is the 2D kinematic sim). This package exports the pure scene generator (planograms + robots -> primitives)
and the Isaac-free worker core; ``arms/sim/app.py`` is the Isaac process entrypoint (``python -m arms.sim.app``).
"""

from arms.sim.scene_spec import (
    PrimSpec,
    RoverSpec,
    SceneSpec,
    SceneSpecError,
    WorldSpec,
    build_rover_spec,
    build_scene_spec,
    build_world_spec,
    load_scene_spec,
    load_world_spec,
)
from arms.sim.worker import NullBackend, SimBackend, WorkerCore

__all__ = [
    "NullBackend",
    "PrimSpec",
    "RoverSpec",
    "SceneSpec",
    "SceneSpecError",
    "SimBackend",
    "WorkerCore",
    "WorldSpec",
    "build_rover_spec",
    "build_scene_spec",
    "build_world_spec",
    "load_scene_spec",
    "load_world_spec",
]
