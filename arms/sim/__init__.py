"""sim arm: the Isaac Sim worker process (no backend Service).

Unlike the other arms there is no ``create(services)`` / ``create_fake(services)``: the backend-facing Protocol is
``RobotService`` (owned by the robot arm, whose real impl drives this worker through the gateway's ``SimLink`` and
whose fake is the 2D kinematic sim). This package exports the pure scene generator (planogram -> primitives) and the
Isaac-free worker core; ``arms/sim/app.py`` is the Isaac process entrypoint (``python -m arms.sim.app``).
"""

from arms.sim.scene_spec import PrimSpec, SceneSpec, SceneSpecError, build_scene_spec, load_scene_spec

__all__ = ["PrimSpec", "SceneSpec", "SceneSpecError", "build_scene_spec", "load_scene_spec"]
