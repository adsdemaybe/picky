"""THE robot model as the robot arm uses it: a small rover (radius ``ROBOT_RADIUS_M``) carrying a vertical lift with an
SO-101 arm and a basket on its back (contracts.common robot model; sim and fake share these numbers).

- Lift: the SO-101 shoulder rides a prismatic lift ``LIFT_MIN_M``..``LIFT_MAX_M``; it is stowed at ``LIFT_STOW_M``
  while driving and raised to ``lift_for(item_z)`` to pick.
- Timings (simulated seconds) for every non-driving motion: ``motion_duration``.
- Battery: ``Battery`` integrates the contracts.common model (per meter driven, idle drain, charging on the dock).
  The robot arm OWNS ``RobotState.battery_pct`` and carries it across backends (Isaac <-> 2D fake).
- Specs: ``load_specs()`` reads data/fixtures/robots.yaml (``utils.data``) and falls back to the fixed fleet
  (Pico / Rhea in store_ave, Hazel in store_hill) docked next to each store's robot home when the file is missing.
"""

import math
from dataclasses import dataclass

from contracts.common import (
    BATTERY_CHARGE_PCT_PER_MIN,
    BATTERY_DRAIN_PCT_PER_M,
    BATTERY_IDLE_DRAIN_PCT_PER_MIN,
    BATTERY_MOTION_MIN_PCT,
    DOCK_TOLERANCE_M,
    LIFT_MAX_M,
    LIFT_MIN_M,
    REACH_Z_MAX_M,
    REACH_Z_MIN_M,
    ROBOT_RADIUS_M,
    Pose2D,
    RobotId,
    StoreId,
)
from contracts.planogram import Planogram, RobotSpec, StoreCameraSpec
from contracts.sim import RobotError
from utils.log import get_logger

log = get_logger(__name__)

LIFT_STOW_M = 0.45
"""Lift height while driving / idle (SO-101 folded over the base, basket reachable)."""
LIFT_SPEED_MPS = 0.25
"""Lift travel speed (m/s)."""
SHOULDER_ABOVE_ITEM_M = 0.10
"""The shoulder sits this far above the item base for a level grasp (clamped to the lift range)."""

BASE_DURATIONS_S: dict[str, float] = {
    "reset": 0.5,
    "scan": 0.8,
    "pick": 2.2,  # + lift travel
    "place_in_basket": 1.5,  # + lift travel back to stow
    "unload_basket": 1.0,  # + per unit
    "pick_from_basket": 2.0,  # 0.2.4 checkout unload: + lift travel down to the basket
    "place_on_counter": 2.0,  # + lift travel up to the counter
}
UNLOAD_PER_UNIT_S = 0.25

FALLBACK_COLORS = ("#2563eb", "#db2777", "#16a34a", "#f59e0b", "#7c3aed")


def lift_for(item_z: float) -> float:
    """Lift (shoulder) height to grasp an item whose base is at ``item_z``."""
    z = min(max(item_z, REACH_Z_MIN_M), REACH_Z_MAX_M)
    return round(min(max(z + SHOULDER_ABOVE_ITEM_M, LIFT_MIN_M), LIFT_MAX_M), 3)


def lift_travel_s(a: float, b: float) -> float:
    return abs(a - b) / LIFT_SPEED_MPS


def on_dock(pose: Pose2D, spec: RobotSpec) -> bool:
    return pose.distance_to(spec.dock) <= DOCK_TOLERANCE_M + 1e-9


def heading_to_dock(waypoints: list[Pose2D], spec: RobotSpec) -> bool:
    """True when a navigate ends on the robot's dock (allowed below BATTERY_MOTION_MIN_PCT)."""
    return bool(waypoints) and on_dock(waypoints[-1], spec)


@dataclass
class Battery:
    """Battery state of one robot (percentage points, 0..100)."""

    pct: float = 100.0
    last_t: float = 0.0

    def drive(self, meters: float) -> None:
        self.pct = max(0.0, self.pct - BATTERY_DRAIN_PCT_PER_M * max(meters, 0.0))

    def settle(self, now_s: float, charging: bool) -> None:
        """Integrate idle drain (or charging) for the sim time elapsed since the last settle."""
        dt = now_s - self.last_t
        self.last_t = now_s
        if dt <= 0:
            return
        if charging:
            self.pct = min(100.0, self.pct + BATTERY_CHARGE_PCT_PER_MIN * dt / 60.0)
        else:
            self.pct = max(0.0, self.pct - BATTERY_IDLE_DRAIN_PCT_PER_MIN * dt / 60.0)

    def check_motion(self, robot_id: RobotId, to_dock: bool = False) -> None:
        if self.pct < BATTERY_MOTION_MIN_PCT and not to_dock:
            raise RobotError(
                "battery_low",
                f"{robot_id} battery {self.pct:.1f}% < {BATTERY_MOTION_MIN_PCT:.0f}%: send it to its dock",
            )

    @property
    def display(self) -> float:
        return round(min(100.0, max(0.0, self.pct)), 1)


def motion_duration(name: str, lift_from: float | None = None, lift_to: float | None = None, units: int = 0) -> float:
    base = BASE_DURATIONS_S.get(name, 0.5)
    if lift_from is not None and lift_to is not None:
        base += lift_travel_s(lift_from, lift_to)
    if name == "unload_basket":
        base += UNLOAD_PER_UNIT_S * units
    return base


# ---------------------------------------------------------------------------------------------------------------------
# Specs (data/fixtures/robots.yaml, with a built-in fallback for the fixed fleet)
# ---------------------------------------------------------------------------------------------------------------------
FALLBACK_FLEET: tuple[tuple[RobotId, str, StoreId], ...] = (
    ("rover-ave-1", "Pico", "store_ave"),
    ("rover-ave-2", "Rhea", "store_ave"),
    ("rover-hill-1", "Hazel", "store_hill"),
)


def _clear(plan: Planogram, x: float, y: float) -> bool:
    if not plan.bounds.inflated(-ROBOT_RADIUS_M).contains(x, y):
        return False
    rects = [f.rect for f in plan.fixtures] + list(plan.obstacles)
    if any(r.distance_to(x, y) < ROBOT_RADIUS_M for r in rects):
        return False
    return all(math.hypot(s.approach_pose.x - x, s.approach_pose.y - y) >= ROBOT_RADIUS_M for s in plan.slots)


def fallback_dock(plan: Planogram, k: int) -> Pose2D:
    """k-th dock next to the store's robot home: 0.8 m steps along +x/-x, +y; the home itself if nothing is clear."""
    home = plan.start_pose
    if k == 0:
        return home.model_copy()
    for dx, dy in ((0.8 * k, 0.0), (-0.8 * k, 0.0), (0.0, 0.8 * k), (0.8 * k, 0.8 * k)):
        if _clear(plan, home.x + dx, home.y + dy):
            return Pose2D(x=round(home.x + dx, 3), y=round(home.y + dy, 3), theta=home.theta)
    return home.model_copy()


def fallback_specs() -> list[RobotSpec]:
    """The fixed fleet, docked next to each store's robot home (used only while robots.yaml is missing)."""
    from utils.data import load_planogram

    specs: list[RobotSpec] = []
    per_store: dict[StoreId, int] = {}
    for n, (robot_id, name, store_id) in enumerate(FALLBACK_FLEET):
        k = per_store.get(store_id, 0)
        per_store[store_id] = k + 1
        try:
            dock = fallback_dock(load_planogram(store_id), k)
        except Exception:  # store data missing: dock at a harmless default
            dock = Pose2D(x=1.0 + 0.8 * k, y=1.0, theta=0.0)
        specs.append(
            RobotSpec(
                robot_id=robot_id,
                name=name,
                store_id=store_id,
                dock=dock,
                battery_pct=100.0,
                color=FALLBACK_COLORS[n % len(FALLBACK_COLORS)],
                notes="built-in fallback (data/fixtures/robots.yaml missing)",
            )
        )
    return specs


def load_specs() -> tuple[list[RobotSpec], list[StoreCameraSpec]]:
    """(robots, store ceiling cameras) from data/fixtures/robots.yaml, else the built-in fallback fleet."""
    try:
        from utils.data import load_robots_file

        rf = load_robots_file()
        if rf.robots:
            return list(rf.robots), list(rf.store_cameras)
    except Exception as exc:
        log.info("robots.yaml unavailable (%s); using the built-in fleet", exc)
    return fallback_specs(), []


def fallback_ceiling_camera(plan: Planogram) -> StoreCameraSpec:
    """'<store>.ceiling-1' looking down at the store center (when robots.yaml lists no ceiling camera)."""
    from contracts.cameras import CameraMount

    c = plan.bounds.center
    return StoreCameraSpec(
        camera_id=f"{plan.store_id}.ceiling-1",
        store_id=plan.store_id,
        name=f"{plan.name} ceiling 1",
        mount=CameraMount(frame="world", x=c.x, y=c.y, z=4.0, pitch=-1.5708, hfov_deg=110.0),
    )


__all__ = [
    "BASE_DURATIONS_S",
    "FALLBACK_FLEET",
    "LIFT_SPEED_MPS",
    "LIFT_STOW_M",
    "Battery",
    "fallback_ceiling_camera",
    "fallback_dock",
    "fallback_specs",
    "heading_to_dock",
    "lift_for",
    "load_specs",
    "motion_duration",
    "on_dock",
]
