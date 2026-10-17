"""Non-blocking action state machines the Isaac main loop steps once per physics step (pure Python).

Each action exposes ``step(dt) -> ActionStatus`` and talks to the sim only through small callbacks, so the logic is
testable without Isaac. The worker maps ``FAILED`` + ``error_code`` to ``CommandResult(ok=False, error_code=...)``.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from arms.robot.controllers.pure_pursuit import PurePursuit, PurePursuitConfig

Vec3 = tuple[float, float, float]
Pose = tuple[float, float, float]


class ActionStatus(Enum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class NavigateAction:
    """Pure pursuit on the kinematic base: ``get_pose()`` -> (x, y, theta); ``set_pose(x, y, theta)``."""

    def __init__(
        self,
        waypoints: list[Pose],
        get_pose: Callable[[], Pose],
        set_pose: Callable[[float, float, float], None],
        config: PurePursuitConfig | None = None,
        timeout_s: float = 90.0,
    ) -> None:
        self._pp = PurePursuit([get_pose(), *waypoints], config)
        self._get, self._set = get_pose, set_pose
        self._t, self._timeout = 0.0, timeout_s
        self.error_code: str | None = None

    def step(self, dt: float) -> ActionStatus:
        if self._pp.done:
            return ActionStatus.SUCCEEDED
        self._t += dt
        if self._t > self._timeout:
            self.error_code = "timeout"
            return ActionStatus.FAILED
        self._set(*self._pp.step(self._get(), dt))
        return ActionStatus.SUCCEEDED if self._pp.done else ActionStatus.RUNNING


@dataclass
class _Segment:
    name: str
    target: Vec3
    duration_s: float


@dataclass
class MagicGraspAction:
    """Scripted end-effector arc: pre-grasp (in front of the item) -> grasp -> attach -> lift -> retreat.

    Callbacks: ``move_hand(xyz)`` sets the IK/hand target; ``attach()`` reparents the item to the hand (returns
    False -> ``grasp_failed``). ``item`` = item CENTER in world; ``approach_theta`` = robot heading (faces the shelf).
    """

    item: Vec3
    approach_theta: float
    move_hand: Callable[[Vec3], None]
    attach: Callable[[], bool]
    standoff_m: float = 0.15
    lift_m: float = 0.08
    segment_s: float = 0.6
    _segments: list[_Segment] = field(default_factory=list, init=False)
    _i: int = field(default=0, init=False)
    _t: float = field(default=0.0, init=False)
    _start: Vec3 | None = field(default=None, init=False)
    _attached: bool = field(default=False, init=False)
    error_code: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        x, y, z = self.item
        bx, by = -math.cos(self.approach_theta), -math.sin(self.approach_theta)  # back toward the robot
        pre = (x + bx * self.standoff_m, y + by * self.standoff_m, z + 0.02)
        self._segments = [
            _Segment("pregrasp", pre, self.segment_s),
            _Segment("grasp", (x, y, z), self.segment_s),
            _Segment("attach", (x, y, z), 0.0),
            _Segment("lift", (x, y, z + self.lift_m), self.segment_s),
            _Segment("retreat", (pre[0], pre[1], z + self.lift_m), self.segment_s),
        ]

    @property
    def phase(self) -> str:
        return self._segments[self._i].name if self._i < len(self._segments) else "done"

    def step(self, dt: float, hand_now: Vec3 | None = None) -> ActionStatus:
        if self.error_code:
            return ActionStatus.FAILED
        if self._i >= len(self._segments):
            return ActionStatus.SUCCEEDED
        seg = self._segments[self._i]
        if seg.name == "attach":
            if not self.attach():
                self.error_code = "grasp_failed"
                return ActionStatus.FAILED
            self._attached = True
            self._advance(seg.target)
            return ActionStatus.RUNNING
        if self._start is None:
            self._start = hand_now or seg.target
        self._t += dt
        a = 1.0 if seg.duration_s <= 0 else min(1.0, self._t / seg.duration_s)
        s = self._start
        self.move_hand(tuple(s[k] + (seg.target[k] - s[k]) * a for k in range(3)))  # type: ignore[arg-type]
        if a >= 1.0:
            self._advance(seg.target)
        return ActionStatus.SUCCEEDED if self._i >= len(self._segments) else ActionStatus.RUNNING

    def _advance(self, end: Vec3) -> None:
        self._i += 1
        self._t = 0.0
        self._start = end


@dataclass
class PlaceAction:
    """Move the held item over the basket drop pose, lower, release (reparent into the basket)."""

    basket: Vec3
    hand_start: Vec3
    move_hand: Callable[[Vec3], None]
    release: Callable[[], bool]
    segment_s: float = 0.6
    _t: float = field(default=0.0, init=False)
    _phase: int = field(default=0, init=False)
    error_code: str | None = field(default=None, init=False)

    def step(self, dt: float) -> ActionStatus:
        above = (self.basket[0], self.basket[1], self.basket[2] + 0.25)
        legs = [(self.hand_start, above), (above, self.basket)]
        if self._phase < len(legs):
            a0, a1 = legs[self._phase]
            self._t += dt
            f = min(1.0, self._t / self.segment_s)
            self.move_hand(tuple(a0[k] + (a1[k] - a0[k]) * f for k in range(3)))  # type: ignore[arg-type]
            if f >= 1.0:
                self._phase += 1
                self._t = 0.0
            return ActionStatus.RUNNING
        if self._phase == len(legs):
            self._phase += 1
            if not self.release():
                self.error_code = "grasp_failed"
                return ActionStatus.FAILED
        return ActionStatus.SUCCEEDED
