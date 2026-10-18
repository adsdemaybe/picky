"""Test-only stubs for the planner -> VLA -> verifier loop (agent arm tests; base deps only).

They implement the CONTRACT Protocols the runner needs, so the agent tests never import another arm (CLAUDE.md §4).
Ported from the API draft (docs/vla-loop, ``fakes/``); the robot / perception arms ship their own real fakes.

world.MiniCatalog / MiniRobot      v0.2 CatalogService / RobotService over a one-aisle mini store
fake_vla.FakeVla                   VlaService: T0 over RobotService, status streams, faults, simulated T1
fake_camera.FakeEvidenceCameras    EvidenceCameras + TruthSource: deterministic frames + ground-truth sidecar
fake_verifier.FakeVerifier         VerifierService: exact verdicts from the sidecar, fault-driven degrade
faults.InMemoryFaultInjector       FaultInjector: demo scenarios + the 2D fault overlay
bus.MemoryBus                      EventPublisher that stamps seq
"""

from __future__ import annotations

from typing import Any

from arms.agent.loop import PlanLoop
from arms.agent.planner_fake import ScriptedPlanner
from arms.agent.tests.loop_world.bus import MemoryBus
from arms.agent.tests.loop_world.fake_camera import FakeEvidenceCameras
from arms.agent.tests.loop_world.fake_verifier import FakeVerifier
from arms.agent.tests.loop_world.fake_vla import FakeVla
from arms.agent.tests.loop_world.faults import InMemoryFaultInjector
from arms.agent.tests.loop_world.world import MiniCatalog, MiniRobot
from contracts.policy import DEFAULT_RETRY_POLICY, RetryPolicy
from contracts.services import Services


def build_loop_services(
    scenario: Any = None,
    *,
    base: Services | None = None,
    motion_s: float = 0.0,
    simulate_t1: bool = False,
    verifier_mode: str = "shadow",
    heartbeat_s: float = 0.05,
    policy: RetryPolicy | None = None,
    use_camera_service: bool = False,
    planner_cls: type[ScriptedPlanner] = ScriptedPlanner,
) -> Services:
    """Wire every stub + the agent's planner and runner. ``base`` = existing v0.2 services to run over instead of the
    mini world. ``services.runner`` is the PlanLoop, ``services.events`` a MemoryBus."""
    s = Services()
    if base is not None:
        for name in Services.field_names():
            setattr(s, name, getattr(base, name))
    if s.catalog is None:
        s.catalog = MiniCatalog(s)
    if s.robot is None:
        s.robot = MiniRobot(s, motion_s=motion_s)
    bus = s.events if s.events is not None else MemoryBus()
    s.events = bus
    s.faults = InMemoryFaultInjector(scenario, publisher=bus)
    cams = FakeEvidenceCameras(s, use_camera_service=use_camera_service)
    s.evidence = cams
    s.truth = cams
    s.verifier = FakeVerifier(s, mode=verifier_mode)  # type: ignore[arg-type]
    s.vla = FakeVla(s, simulate_t1=simulate_t1)
    s.planner = planner_cls(s)
    # the loop's own tests answer every question generically; the 0.2.4 photo cards have their own tests
    s.runner = PlanLoop(s, heartbeat_s=heartbeat_s, policy=policy or DEFAULT_RETRY_POLICY, verify_items=False)
    return s


__all__ = [
    "FakeEvidenceCameras",
    "FakeVerifier",
    "FakeVla",
    "InMemoryFaultInjector",
    "MemoryBus",
    "MiniCatalog",
    "MiniRobot",
    "build_loop_services",
]
