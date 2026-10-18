"""InMemoryFaultInjector: the robot arm's fake ``FaultInjector`` + the 2D world's fault OVERLAY.

The overlay holds world perturbations that v0.2 RobotService has no primitive for (a "virtual" wrong unit in the
gripper, mislabels, blocked cameras). The fake VLA writes it, the fake truth / camera read it, so the VLA claim, the
frames and the verdict stay consistent. Isaac has no equivalent yet (scenarios marked ``two_d_fake_only``).
"""

from collections.abc import Sequence
from typing import Any

from contracts.events import make_event
from contracts.faults import (
    DEMO_SCENARIOS,
    FaultContext,
    FaultEffect,
    FaultFiring,
    FaultRule,
    FaultScenario,
    fault_fires,
    fault_matches,
)
from contracts.sim import ItemInstance


class FaultOverlay:
    """Per-robot world perturbations shared by the fake VLA (writer) and the fake truth / camera (readers)."""

    def __init__(self) -> None:
        self.virtual_holding: dict[str, ItemInstance] = {}
        self.mislabels: dict[str, str] = {}
        self.occluded: dict[str, int] = {}

    def occlude(self, camera_id: str, captures: int = 1) -> None:
        self.occluded[camera_id] = self.occluded.get(camera_id, 0) + captures


class InMemoryFaultInjector:
    """Deterministic: occurrence = n-th distinct step_key that matched a rule's static fields; ``times`` caps firings.
    ``publisher`` (a v0.2 EventPublisher) receives ``demo.fault`` events when set."""

    def __init__(self, scenario: FaultScenario | str | None = None, *, publisher: Any = None) -> None:
        self.rules: list[FaultRule] = []
        self.scenario: str | None = None
        self.publisher = publisher
        self.overlay = FaultOverlay()
        self._fired: dict[str, int] = {}
        self._seen: dict[str, list[str]] = {}
        self._firings: list[FaultFiring] = []
        if scenario is not None:
            sc = DEMO_SCENARIOS[scenario] if isinstance(scenario, str) else scenario
            self.rules = [r.model_copy() for r in sc.rules]
            self.scenario = sc.name

    async def load_scenario(self, scenario: FaultScenario | str) -> FaultScenario:
        sc = DEMO_SCENARIOS[scenario] if isinstance(scenario, str) else scenario
        await self.clear_faults()
        self.rules = [r.model_copy() for r in sc.rules]
        self.scenario = sc.name
        return sc

    async def inject(self, rule: FaultRule) -> FaultRule:
        self.rules.append(rule)
        return rule

    async def clear_faults(self) -> None:
        self.rules = []
        self.scenario = None
        self._fired.clear()
        self._seen.clear()
        self.overlay = FaultOverlay()

    async def firings(self) -> list[FaultFiring]:
        return list(self._firings)

    async def consult(self, ctx: FaultContext, effects: Sequence[FaultEffect]) -> list[FaultFiring]:
        out: list[FaultFiring] = []
        for rule in self.rules:
            if rule.effect not in effects or not fault_matches(rule, ctx):
                continue
            seen = self._seen.setdefault(rule.fault_id, [])
            key = ctx.step_key or ctx.exec_id or f"{ctx.robot_id}:{len(seen)}"
            if key not in seen:
                seen.append(key)
            ctx_n = ctx.model_copy(update={"occurrence": seen.index(key) + 1})
            if not fault_fires(rule, ctx_n, self._fired.get(rule.fault_id, 0)):
                continue
            self._fired[rule.fault_id] = self._fired.get(rule.fault_id, 0) + 1
            firing = FaultFiring(
                fault_id=rule.fault_id, effect=rule.effect, context=ctx_n, scenario=self.scenario, note=rule.note
            )
            self._firings.append(firing)
            out.append(firing)
            if self.publisher is not None:
                await self.publisher.publish(
                    make_event(
                        "demo.fault",
                        trip_id=None,
                        source="robot",
                        robot_id=ctx.robot_id,
                        firing=firing,
                        scenario=self.scenario,
                    )
                )
        return out

    def rule(self, fault_id: str) -> FaultRule | None:
        return next((r for r in self.rules if r.fault_id == fault_id), None)
