"""Demo fault injection (``contracts.services.FaultInjector``, ``Services.faults``) + the 2D world's fault OVERLAY.

The robot arm owns both, because faults perturb the WORLD the robot lives in, never a verdict:

- ``InMemoryFaultInjector``: deterministic rules (``contracts.faults.fault_fires``; occurrence = n-th DISTINCT step key
  that matched a rule's static fields, ``times`` caps firings). The robot VLA (layer 'vla' + the 'mislabel' world
  effect), perception's fake camera (layer 'camera') and fake verifier (layer 'verifier') all call ``consult``. Every
  firing is recorded (``firings``) and published as ``demo.fault`` (``source='robot'``; staff / presenter view).
- ``FaultOverlay``: perturbations RobotService has no primitive for. The VLA writes it; ``RobotTruth`` applies it, so
  the claim, the frames' ground truth and the verdict stay consistent. On the 2D fake a wrong unit is REALLY grabbed
  (``FakeRobot.grab_unit``) and nothing is virtual; the overlay is the Isaac stand-in (scenario ``two_d_fake_only``).

Scenario from ``PICKY_FAULTS`` (a ``contracts.faults.DEMO_SCENARIOS`` name; unset = no rules), or the dev routes
(``load_scenario`` / ``inject`` / ``clear_faults``; PICKY_DEV=1).
"""

from collections.abc import Sequence
from typing import Any

from contracts.common import RobotId, StoreId, TripId
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
from contracts.services import Services
from contracts.sim import ItemInstance
from utils.config import get_settings
from utils.log import get_logger

log = get_logger(__name__)


class FaultOverlay:
    """Per-robot world perturbations shared by the VLA (writer) and ``RobotTruth`` (reader).

    - ``virtual_holding[robot_id]``: the unit the gripper REALLY holds when RobotService could not do it (Isaac
      'wrong_item': no grab-the-neighbour primitive). Truth reports it as held and takes it off its shelf.
    - ``virtual_basket[robot_id]``: virtual units placed in the basket (cleared by the handoff).
    - ``mislabels[instance_id]``: label text a unit reads ('mislabel' effect, ``params.text``).
    - ``occluded[camera_id]``: captures left blocked (perception's fake camera may use it).
    """

    def __init__(self) -> None:
        self.virtual_holding: dict[RobotId, ItemInstance] = {}
        self.virtual_basket: dict[RobotId, list[ItemInstance]] = {}
        self.mislabels: dict[str, str] = {}
        self.occluded: dict[str, int] = {}

    def occlude(self, camera_id: str, captures: int = 1) -> None:
        self.occluded[camera_id] = self.occluded.get(camera_id, 0) + captures

    def clear_robot(self, robot_id: RobotId) -> None:
        self.virtual_holding.pop(robot_id, None)
        self.virtual_basket.pop(robot_id, None)


def scenario_from_settings() -> str | None:
    """``PICKY_FAULTS`` (read through utils.config; None until the Settings field exists or when unset)."""
    try:
        value = getattr(get_settings(), "picky_faults", None)
    except Exception:  # noqa: BLE001 - a broken .env must not stop the robot arm
        return None
    value = str(value or "").strip()
    return value or None


class InMemoryFaultInjector:
    """``FaultInjector``. ``services.events`` (read at call time) receives ``demo.fault``; ``publisher`` overrides it.

    Extras (not in the Protocol): ``overlay``, ``scenario``, ``rules``, ``rule(fault_id)`` (the rule's ``params``:
    the fake verifier reads ``confidence``, the VLA ``text``), ``bind_robot`` (event envelope per robot)."""

    def __init__(
        self,
        scenario: FaultScenario | str | None = None,
        *,
        services: Services | None = None,
        publisher: Any = None,
    ) -> None:
        self.services = services
        self.publisher = publisher
        self.rules: list[FaultRule] = []
        self.scenario: str | None = None
        self.overlay = FaultOverlay()
        self._fired: dict[str, int] = {}
        self._seen: dict[str, list[str]] = {}
        self._firings: list[FaultFiring] = []
        self._envelopes: dict[RobotId, dict[str, Any]] = {}
        if scenario is not None:
            try:
                sc = self._resolve(scenario)
            except KeyError:
                log.warning("unknown fault scenario %r (PICKY_FAULTS); no faults loaded", scenario)
            else:
                self.rules = [r.model_copy() for r in sc.rules]
                self.scenario = sc.name
                log.info("fault scenario %r loaded (%d rules)", sc.name, len(self.rules))

    # ---- FaultInjector -----------------------------------------------------------------------------------------------
    async def load_scenario(self, scenario: FaultScenario | str) -> FaultScenario:
        """Replace the active rules (KeyError for an unknown name -> 404 at the gateway)."""
        sc = self._resolve(scenario)
        await self.clear_faults()
        self.rules = [r.model_copy() for r in sc.rules]
        self.scenario = sc.name
        return sc

    async def inject(self, rule: FaultRule) -> FaultRule:
        self.rules = [r for r in self.rules if r.fault_id != rule.fault_id] + [rule]
        self._fired.pop(rule.fault_id, None)
        self._seen.pop(rule.fault_id, None)
        return rule

    async def clear_faults(self) -> None:
        """No rules, counters reset, overlay cleared. The firing history stays (the presenter's cheat sheet)."""
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
            log.info("fault %s fired: %s on %s (%s)", rule.fault_id, rule.effect, ctx.robot_id, key)
            await self._publish(firing)
        return out

    # ---- extras ------------------------------------------------------------------------------------------------------
    def rule(self, fault_id: str) -> FaultRule | None:
        return next((r for r in self.rules if r.fault_id == fault_id), None)

    def bind_robot(self, robot_id: RobotId, *, trip_id: TripId | None = None, store_id: StoreId | None = None) -> None:
        """Envelope (trip / store) stamped on the robot's later ``demo.fault`` events (the VLA calls it per exec)."""
        self._envelopes[robot_id] = {"trip_id": trip_id, "store_id": store_id}

    @staticmethod
    def _resolve(scenario: FaultScenario | str) -> FaultScenario:
        return DEMO_SCENARIOS[scenario] if isinstance(scenario, str) else scenario

    async def _publish(self, firing: FaultFiring) -> None:
        bus = self.publisher or (getattr(self.services, "events", None) if self.services is not None else None)
        if bus is None:
            return
        env = self._envelopes.get(firing.context.robot_id, {})
        try:
            await bus.publish(
                make_event(
                    "demo.fault",
                    trip_id=env.get("trip_id"),
                    source="robot",
                    store_id=env.get("store_id"),
                    robot_id=firing.context.robot_id,
                    firing=firing,
                    scenario=self.scenario,
                )
            )
        except Exception as exc:  # noqa: BLE001 - the presenter feed must never break a motion
            log.warning("demo.fault publish failed: %s", exc)


__all__ = ["FaultOverlay", "InMemoryFaultInjector", "scenario_from_settings"]
