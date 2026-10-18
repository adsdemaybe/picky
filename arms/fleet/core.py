"""FleetManager: the deterministic dispatcher shared by the fake and the real fleet.

Owns every RequestPlan, Goal and Assignment; publishes request.* / goal.* / fleet.robot_updated. The only difference
between fake and real is interpretation (``mode``): the fake classifies with keyword rules (``rules.py``) and asks
the agent arm only for the shop list; the real fleet asks ``agent.interpret_request`` for everything (LLM in the real
agent) and falls back to the rules on any failure. No LLM calls happen in this arm.

Execution model: every goal runs in a "driver" task: pick a robot (``contracts.fleet.choose_robot``), wait for that
robot's lock (one goal per robot at a time = the queue), set ACTIVE, run ``brain.run_goal`` as a child task, then
finalize; a ``RobotError`` from the brain reassigns to another robot in the same store (``MAX_GOAL_ATTEMPTS``).
Shop goals are started by the trip flow via ``execute_trip`` (the trip is created ``auto_execute=True``).
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import Sequence
from typing import Any, Literal

from arms.fleet import rules
from arms.fleet.stores import choose_stores, evaluate_stores, sub_list
from contracts.api import CreateTripRequest
from contracts.common import (
    DEFAULT_STORE_ID,
    DOCK_TOLERANCE_M,
    ONLINE_STORE_ID,
    STAFF_USER_ID,
    Pose2D,
    utcnow,
)
from contracts.events import goal_envelope, make_event
from contracts.fleet import (
    MAX_GOAL_ATTEMPTS,
    MIN_BATTERY_PCT,
    TERMINAL_GOAL_STATUSES,
    Assignment,
    AssignmentCandidate,
    BrainReply,
    FleetRequest,
    Goal,
    GoalItem,
    GoalKind,
    GoalSpec,
    GoalStatus,
    GoalUpdate,
    RequestDraft,
    RequestPlan,
    RobotInfo,
    can_goal_transition,
    choose_robot,
    goal_items_from_trip,
    robot_info_from,
    score_robot,
    split_budget,
)
from contracts.planogram import RobotSpec
from contracts.shopping import Intent, ListItem, ShoppingList
from contracts.sim import RobotError, RobotState, RobotStatus
from utils.ids import new_id
from utils.log import get_logger
from utils.money import fmt_cents

log = get_logger(__name__)

_PERSONAL_STATUS_RE = re.compile(
    r"\b(my|me|mine|order|trip|robot|goal|status|eta|it|going|done|ready|pico|rhea|hazel)\b", re.IGNORECASE
)
"""Status questions about the asker's own goals; anything else ("where is the salsa?") goes to the agent."""

Mode = Literal["fake", "real"]

ACTIVE_STATUSES = frozenset({GoalStatus.PENDING, GoalStatus.ASSIGNED, GoalStatus.ACTIVE, GoalStatus.WAITING})
DONE_ITEM_STATUSES = frozenset({"in_basket", "online", "substituted", "unavailable", "skipped", "declined", "failed"})
SUCCESS_ITEM_STATUSES = frozenset({"in_basket", "online", "substituted"})

DEFAULT_ROBOTS: tuple[tuple[str, str, str, str, int], ...] = (
    ("rover-ave-1", "Pico", "store_ave", "#2563eb", 0),
    ("rover-ave-2", "Rhea", "store_ave", "#db2777", 1),
    ("rover-hill-1", "Hazel", "store_hill", "#16a34a", 0),
)
"""Fallback fleet when data/fixtures/robots.yaml is missing (robot_id, name, store, color, dock slot)."""

_STEP_FAILURE = re.compile(r"^([a-z][a-z0-9_]*): (.+)$", re.DOTALL)


def robot_failure(err: RobotError) -> tuple[str, str]:
    """(code, plain reason) of a robot failure for ``goal.failure_code`` and the shopper. The agent's plan runner hands
    a goal back ('escalate') as ``RobotError('unknown_robot', '<step code>: <message>')`` because SimErrorCode has no
    hand-off code, so that step's own code and message are read back here; any other error is kept as it is."""
    msg = (err.message or "").strip()
    m = _STEP_FAILURE.match(msg)
    if m is not None and (err.code == "unknown_robot" or m.group(1) == err.code):
        return m.group(1), m.group(2).strip()
    return err.code, msg or err.code


_GENERIC_LIST_TITLES = frozenset({"", "shopping", "shopping list", "shopping trip", "groceries", "list"})
_COUNT_SUFFIX = re.compile(r"\s*\(\d+ items?\)$")
"""A list title that already carries its count ('Your order (3 items)'): replaced, so the count is said once."""


def plural(n: int, word: str) -> str:
    """'1 item' / '3 items'."""
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def shop_goal_title(list_title: str | None, n_items: int, store_name: str, store_id: str | None = None) -> str:
    """'Shop Tacos for 6 (10 items) at Picky Market - The Ave'; a split sub-list's '(<store>)' suffix and generic
    titles ('Shopping') are dropped so the store is named once: 'Shop 3 items at Picky Express - Capitol Hill'."""
    title = (list_title or "").strip()
    for name in {store_name, store_id or ""} - {""}:
        if title.endswith(f"({name})"):
            title = title[: -len(f"({name})")].rstrip()
    if title.lower().startswith("shop "):
        title = title[5:].strip()
    title = _COUNT_SUFFIX.sub("", title)
    if title.lower() in _GENERIC_LIST_TITLES:
        return f"Shop {plural(n_items, 'item')} at {store_name}"
    return f"Shop {title} ({plural(n_items, 'item')}) at {store_name}"


def _copy(goal: Goal) -> Goal:
    return goal.model_copy(deep=True)


class FleetManager:
    """Implements ``contracts.services.FleetService``. ``mode='fake'`` = keyword interpretation; ``'real'`` =
    ``agent.interpret_request`` first. Options are plain attributes so the gateway's fake_opts can tune them."""

    def __init__(
        self,
        services: Any,
        *,
        mode: Mode = "fake",
        throttle_s: float = 1.0,
        robot_wait_s: float = 60.0,
        robot_poll_s: float = 1.0,
        planning_wait_s: float = 15.0,
        state_timeout_s: float = 2.0,
        robots: dict[str, RobotSpec] | list[RobotSpec] | None = None,
    ) -> None:
        self.services = services
        self.mode: Mode = mode
        self.throttle_s = throttle_s
        self.robot_wait_s = robot_wait_s
        self.robot_poll_s = robot_poll_s
        self.planning_wait_s = planning_wait_s
        self.state_timeout_s = state_timeout_s

        self._specs: dict[str, RobotSpec] | None = None
        if robots is not None:
            items = robots.values() if isinstance(robots, dict) else robots
            self._specs = {r.robot_id: r for r in items}
        self._states: dict[str, RobotState] = {}
        self._disconnected: set[str] = set()
        self._goals: dict[str, Goal] = {}
        self._requests: dict[str, RequestPlan] = {}
        self._drivers: dict[str, asyncio.Task[Any]] = {}
        self._brain_tasks: dict[str, asyncio.Task[Any]] = {}
        self._running: dict[str, str] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._planning: dict[str, asyncio.Event] = {}
        self._failed_robots: dict[str, set[str]] = {}
        self._last_failure: dict[str, tuple[str, str, str]] = {}
        """goal_id -> (robot_id, code, reason) of its latest robot failure (robot_failure)."""
        self._held: dict[str, str] = {}
        """robot_id -> why it was stopped. A held robot takes no goal from its queue (and no new automatic goal)
        until a manual goal for it or ``resume_robot``."""
        self._last_pub: dict[str, tuple[tuple[Any, ...], Pose2D | None, float]] = {}
        self._store_names: dict[str, str] = {}
        self._monitor: asyncio.Task[Any] | None = None
        self._bg: set[asyncio.Task[Any]] = set()
        self._closed = False

    # ================================================================================================================
    # lifecycle
    # ================================================================================================================
    async def start(self) -> None:
        if self._monitor is not None and not self._monitor.done():
            return
        self._closed = False
        self._monitor = asyncio.create_task(self._monitor_loop(), name="fleet-monitor")

    async def shutdown(self) -> None:
        self._closed = True
        tasks = [t for t in [self._monitor, *self._drivers.values(), *self._brain_tasks.values(), *self._bg] if t]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(BaseException):
                await t
        self._monitor = None

    # ================================================================================================================
    # robots
    # ================================================================================================================
    def _robot_specs(self) -> dict[str, RobotSpec]:
        if self._specs is None:
            specs: dict[str, RobotSpec] = {}
            try:
                from utils.data import load_robots

                specs = dict(load_robots())
            except Exception as exc:
                log.warning("robots.yaml unavailable (%s); using the built-in 3-robot fleet", exc)
            if not specs:
                specs = self._default_specs()
            self._specs = specs
        return self._specs

    @staticmethod
    def _default_specs() -> dict[str, RobotSpec]:
        out: dict[str, RobotSpec] = {}
        for rid, name, store, color, slot in DEFAULT_ROBOTS:
            try:
                from utils.data import load_planogram

                home = load_planogram(store).start_pose
            except Exception:
                home = Pose2D(x=1.0, y=1.0, theta=0.0)
            dock = Pose2D(x=home.x + 0.8 * slot, y=home.y, theta=home.theta)
            out[rid] = RobotSpec(robot_id=rid, name=name, store_id=store, dock=dock, color=color)
        return out

    def _spec(self, robot_id: str) -> RobotSpec:
        specs = self._robot_specs()
        if robot_id not in specs:
            raise KeyError(f"unknown robot {robot_id!r}")
        return specs[robot_id]

    def _note_state(self, st: RobotState) -> None:
        """Remember a robot state; robots the fleet has never heard of (an Isaac worker's extra rovers) join it."""
        rid = st.robot_id
        if not rid:
            return
        specs = self._robot_specs()
        if rid not in specs and st.store_id:
            try:
                specs[rid] = RobotSpec(robot_id=rid, name=rid, store_id=st.store_id, dock=st.pose)
            except Exception as exc:
                log.warning("ignoring state of unknown robot %s: %s", rid, exc)
                return
        self._states[rid] = st
        self._disconnected.discard(rid)

    def _synthetic_state(self, robot_id: str) -> RobotState:
        spec = self._spec(robot_id)
        return RobotState(
            robot_id=robot_id,
            store_id=spec.store_id,
            pose=spec.dock,
            status=RobotStatus.IDLE,
            battery_pct=spec.battery_pct,
        )

    async def _fetch_state(self, robot_id: str) -> RobotState | None:
        """Fresh state from the robot arm (None = disconnected). With no robot arm wired, or one that does not speak
        the multi-robot API yet, robots are assumed idle on their dock so the pipeline keeps running."""
        robot = self.services.robot if self.services is not None else None
        if robot is None:
            return self._states.get(robot_id) or self._synthetic_state(robot_id)
        try:
            st = await asyncio.wait_for(robot.state(robot_id), self.state_timeout_s)
        except RobotError as exc:
            log.debug("robot %s state: %s", robot_id, exc)
            self._states.pop(robot_id, None)
            self._disconnected.add(robot_id)
            return None
        except (TypeError, AttributeError, NotImplementedError):
            return self._states.get(robot_id) or self._synthetic_state(robot_id)
        except Exception as exc:
            log.warning("robot %s state failed: %s", robot_id, exc)
            return self._states.get(robot_id)
        if st is None:
            return None
        if not st.robot_id:
            st = st.model_copy(update={"robot_id": robot_id})
        self._note_state(st)
        return st

    def _cached_state(self, robot_id: str) -> RobotState | None:
        if robot_id in self._disconnected:
            return None
        st = self._states.get(robot_id)
        if st is None and (self.services is None or self.services.robot is None):
            return self._synthetic_state(robot_id)
        return st

    def _queued_ids(self, robot_id: str) -> list[str]:
        running = self._running.get(robot_id)
        return [
            g.goal_id
            for g in self._goals.values()
            if g.robot_id == robot_id and g.status in (GoalStatus.PENDING, GoalStatus.ASSIGNED) and g.goal_id != running
        ]

    def _current_id(self, robot_id: str) -> str | None:
        gid = self._running.get(robot_id)
        if gid and gid in self._goals and not self._goals[gid].terminal:
            return gid
        return None

    async def _brain_view(self, robot_id: str) -> dict[str, Any]:
        brain = self.services.brain if self.services is not None else None
        if brain is None:
            return {}
        try:
            st = await asyncio.wait_for(brain.status(robot_id), 1.0)
        except Exception:
            return {}
        out: dict[str, Any] = {"brain_state": st.state}
        if st.last_step is not None and st.last_step.text:
            out["last_thought"] = st.last_step.text[:240]
        return out

    def _info(self, robot_id: str, state: RobotState | None, brain: dict[str, Any] | None = None) -> RobotInfo:
        spec = self._spec(robot_id)
        current = self._current_id(robot_id)
        queued = self._queued_ids(robot_id)
        extra: dict[str, Any] = {"current_goal_id": current, "queued_goal_ids": queued, "updated_at": utcnow()}
        if brain:
            extra.update(brain)
        elif current is not None:
            g = self._goals[current]
            extra["brain_state"] = "waiting_user" if g.status == GoalStatus.WAITING else "acting"
        info = robot_info_from(spec, state, **extra)
        battery = info.battery_pct if info.battery_pct is not None else 100.0
        info.available = (
            info.connected
            and info.status in (RobotStatus.IDLE, RobotStatus.CHARGING)
            and current is None
            and not queued
            and battery >= MIN_BATTERY_PCT
            and robot_id not in self._held
        )
        if robot_id in self._held and info.error is None:
            info.error = f"stopped ({self._held[robot_id]}): on hold until it gets a new goal or is resumed"
        return info

    async def _robot_infos(self, store_id: str | None = None, *, fresh: bool = True) -> list[RobotInfo]:
        out: list[RobotInfo] = []
        for rid, spec in list(self._robot_specs().items()):
            if store_id is not None and spec.store_id != store_id:
                continue
            st = await self._fetch_state(rid) if fresh else self._cached_state(rid)
            out.append(self._info(rid, st, await self._brain_view(rid) if fresh else None))
        return out

    async def list_robots(self, store_id: str | None = None) -> list[RobotInfo]:
        return await self._robot_infos(store_id)

    async def get_robot(self, robot_id: str) -> RobotInfo:
        self._spec(robot_id)
        return self._info(robot_id, await self._fetch_state(robot_id), await self._brain_view(robot_id))

    async def _publish_robot(self, robot_id: str) -> None:
        try:
            info = self._info(robot_id, self._cached_state(robot_id))
        except KeyError:
            return
        self._last_pub[robot_id] = (self._signature(info), info.pose, time.monotonic())
        await self._publish("fleet.robot_updated", trip_id=None, store_id=info.store_id, robot_id=robot_id, robot=info)

    @staticmethod
    def _signature(info: RobotInfo) -> tuple[Any, ...]:
        return (
            info.status,
            info.available,
            info.connected,
            info.backend,
            int(info.battery_pct or 0),
            info.charging,
            info.holding,
            tuple(info.basket_skus),
            info.current_goal_id,
            tuple(info.queued_goal_ids),
            info.error,
        )

    # ---- monitor ---------------------------------------------------------------------------------------------------
    async def _monitor_loop(self) -> None:
        backoff = 1.0
        while not self._closed:
            robot = self.services.robot if self.services is not None else None
            if robot is None:
                await asyncio.sleep(5.0)
                continue
            try:
                async for st in robot.states():
                    backoff = 1.0
                    await self._on_state(st)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("robot state stream ended: %s (retry in %.0fs)", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10.0)

    async def _on_state(self, st: RobotState) -> None:
        if not st.robot_id:
            return
        self._note_state(st)
        rid = st.robot_id
        try:
            info = self._info(rid, st)
        except KeyError:
            return
        sig = self._signature(info)
        last = self._last_pub.get(rid)
        now = time.monotonic()
        if last is None or last[0] != sig:
            await self._publish_robot(rid)
        elif info.pose != last[1] and now - last[2] >= self.throttle_s:
            await self._publish_robot(rid)
        await self._check_battery(info)

    async def _check_battery(self, info: RobotInfo) -> None:
        if info.battery_pct is None or info.battery_pct >= MIN_BATTERY_PCT or info.charging:
            return
        if info.robot_id in self._held:  # stopped by a person: don't drive it anywhere on our own
            return
        if info.status != RobotStatus.IDLE or info.current_goal_id or info.queued_goal_ids:
            return
        if info.pose is not None and info.pose.distance_to(info.dock) <= DOCK_TOLERANCE_M:
            return
        if any(
            g.kind == "return_to_dock" and g.robot_id == info.robot_id and not g.terminal for g in self._goals.values()
        ):
            return
        log.info("%s battery %.0f%% < %.0f%%: return_to_dock", info.robot_id, info.battery_pct, MIN_BATTERY_PCT)
        try:
            await self.create_goal(
                GoalSpec(
                    kind="return_to_dock",
                    robot_id=info.robot_id,
                    priority="high",
                    title=f"{info.name}: low battery ({info.battery_pct:.0f}%), return to dock",
                    instructions="Battery low: drive to your dock and charge.",
                )
            )
        except Exception as exc:
            log.warning("low-battery return_to_dock for %s failed: %s", info.robot_id, exc)

    # ================================================================================================================
    # events
    # ================================================================================================================
    async def _publish(self, type_: str, **kw: Any) -> None:
        events = self.services.events if self.services is not None else None
        if events is None:
            return
        try:
            await events.publish(make_event(type_, source="fleet", **kw))
        except Exception as exc:
            log.warning("publish %s failed: %s", type_, exc)

    async def _goal_event(self, type_: str, goal: Goal, **kw: Any) -> None:
        await self._publish(type_, **goal_envelope(goal), goal=_copy(goal), **kw)

    async def _error(self, message: str, *, code: str | None = None, goal: Goal | None = None, **env: Any) -> None:
        envelope = goal_envelope(goal) if goal is not None else {"trip_id": None, **env}
        await self._publish("error", **envelope, arm="fleet", message=message, code=code, recoverable=True)

    # ================================================================================================================
    # goals: registry + updates
    # ================================================================================================================
    def _register(self, goal: Goal) -> Goal:
        self._goals[goal.goal_id] = goal
        return goal

    def _goal(self, goal_id: str) -> Goal:
        try:
            return self._goals[goal_id]
        except KeyError:
            raise KeyError(f"unknown goal {goal_id!r}") from None

    async def get_goal(self, goal_id: str) -> Goal:
        return _copy(self._goal(goal_id))

    async def list_goals(
        self,
        store_id: str | None = None,
        robot_id: str | None = None,
        status: GoalStatus | Sequence[GoalStatus] | None = None,
        *,
        active: bool | None = None,
        kind: GoalKind | None = None,
        request_id: str | None = None,
        trip_id: str | None = None,
        limit: int = 50,
    ) -> list[Goal]:
        if status is None:
            statuses = None
        elif isinstance(status, str):
            statuses = {GoalStatus(status)}
        else:
            statuses = {GoalStatus(s) for s in status}
        out: list[Goal] = []
        for g in reversed(list(self._goals.values())):
            if store_id is not None and g.store_id != store_id:
                continue
            if robot_id is not None and g.robot_id != robot_id:
                continue
            if statuses is not None and g.status not in statuses:
                continue
            if active is not None and (g.status in ACTIVE_STATUSES) != active:
                continue
            if kind is not None and g.kind != kind:
                continue
            if request_id is not None and g.request_id != request_id:
                continue
            if trip_id is not None and g.trip_id != trip_id:
                continue
            out.append(_copy(g))
            if len(out) >= max(1, limit):
                break
        return out

    async def update_goal(self, goal_id: str, update: GoalUpdate) -> Goal:
        goal = self._goal(goal_id)
        return _copy(await self._apply(goal, update))

    async def _apply(self, goal: Goal, update: GoalUpdate, *, publish: bool = True, event_note: str = "") -> Goal:
        """Apply a GoalUpdate (validated transition), publish the matching goal.* event, return the goal."""
        new_status = update.status
        if new_status is not None and new_status != goal.status and not can_goal_transition(goal.status, new_status):
            raise ValueError(f"goal {goal.goal_id}: cannot go {goal.status.value} -> {GoalStatus(new_status).value}")
        if goal.terminal:
            return goal  # same-status / no-status update of a finished goal: no-op
        items_changed = False
        if update.items:
            by_id = {i.item_id: i for i in goal.items}
            for patch in update.items:
                item = by_id.get(patch.item_id)
                if item is None:
                    log.warning("goal %s: update for unknown item %s ignored", goal.goal_id, patch.item_id)
                    continue
                for key, value in patch.model_dump(exclude={"item_id"}, exclude_none=True).items():
                    setattr(item, key, value)
                items_changed = True
        if update.add_items:
            have = {i.item_id for i in goal.items}
            for item in update.add_items:
                if item.item_id not in have:
                    goal.items.append(item.model_copy(deep=True))
                    have.add(item.item_id)
                    items_changed = True
        scanned_changed = False
        for sid in update.scanned_slot_ids:
            if sid not in goal.scanned_slot_ids:
                goal.scanned_slot_ids.append(sid)
                scanned_changed = True
        if update.progress is not None:
            goal.progress = update.progress
        elif scanned_changed and goal.slot_ids:
            done = len(set(goal.scanned_slot_ids) & set(goal.slot_ids))
            goal.progress = round(min(1.0, done / len(goal.slot_ids)), 4)
        elif items_changed and goal.items:
            todo = [i for i in goal.items if not (i.optional and i.status in ("skipped", "declined"))] or goal.items
            done = sum(1 for i in todo if i.status in DONE_ITEM_STATUSES)
            goal.progress = max(goal.progress, round(done / len(todo), 4))
        if update.note is not None:
            goal.progress_note = update.note
        if update.summary is not None:
            goal.summary = update.summary
        if update.error is not None:
            goal.error = update.error
        if update.failure_code is not None:
            goal.failure_code = update.failure_code
        old_status = goal.status
        if new_status is not None and new_status != old_status:
            goal.status = GoalStatus(new_status)
            if goal.status == GoalStatus.ACTIVE and goal.started_at is None:
                goal.started_at = utcnow()
        goal.updated_at = utcnow()
        terminal_now = goal.status in TERMINAL_GOAL_STATUSES and old_status not in TERMINAL_GOAL_STATUSES
        if terminal_now:
            goal.finished_at = goal.updated_at
            if goal.status == GoalStatus.COMPLETED:
                goal.progress = 1.0
            a = goal.assignment
            if a is not None and a.outcome == "active":
                a.outcome = goal.status.value  # type: ignore[assignment]
                a.ended_at = goal.updated_at
        if publish:
            if terminal_now and goal.status == GoalStatus.COMPLETED:
                await self._goal_event("goal.completed", goal)
            elif terminal_now:
                await self._goal_event("goal.failed", goal, reason=goal.error or goal.failure_code or "")
            else:
                await self._goal_event("goal.updated", goal, note=event_note or (update.note or ""))
        if goal.robot_id and (old_status != goal.status):
            await self._publish_robot(goal.robot_id)
        if terminal_now:
            await self._on_goal_terminal(goal)
        return goal

    async def _set_status(self, goal: Goal, status: GoalStatus, **fields: Any) -> Goal:
        return await self._apply(goal, GoalUpdate(status=status, **fields))

    async def _on_goal_terminal(self, goal: Goal) -> None:
        if goal.status == GoalStatus.CANCELLED:
            await self._withdraw_questions(goal)
        if goal.request_id:
            await self._refresh_request(goal.request_id)

    async def _withdraw_questions(self, goal: Goal) -> None:
        approvals = self.services.approvals if self.services is not None else None
        if approvals is None:
            return
        try:
            pending = approvals.pending(goal_id=goal.goal_id)
        except TypeError:
            pending = [p for p in approvals.pending() if getattr(p, "goal_id", None) == goal.goal_id]
        except Exception as exc:
            log.warning("approvals.pending failed: %s", exc)
            return
        for req in pending:
            try:
                await approvals.cancel(req.approval_id, reason=goal.error or "goal cancelled")
            except Exception as exc:
                log.warning("approvals.cancel(%s) failed: %s", req.approval_id, exc)

    # ================================================================================================================
    # goal creation + assignment
    # ================================================================================================================
    def _store_name(self, store_id: str) -> str:
        return self._store_names.get(store_id) or store_id

    async def _load_store_names(self) -> dict[str, str]:
        if not self._store_names:
            catalog = self.services.catalog if self.services is not None else None
            names: dict[str, str] = {}
            if catalog is not None:
                try:
                    for s in await catalog.list_stores():
                        if s.store_id != ONLINE_STORE_ID and getattr(s, "kind", "physical") == "physical":
                            names[s.store_id] = s.name
                except Exception as exc:
                    log.warning("catalog.list_stores failed: %s", exc)
            if not names:
                try:
                    from utils.data import list_store_ids, load_planogram

                    for sid in list_store_ids():
                        names[sid] = load_planogram(sid).name
                except Exception:
                    names = {s.store_id: s.store_id for s in self._robot_specs().values()}
            self._store_names = names
        return self._store_names

    def _new_goal(self, spec: GoalSpec, store_id: str, *, title: str | None = None) -> Goal:
        user = spec.user_id or STAFF_USER_ID
        return Goal(
            goal_id=new_id("goal"),
            kind=spec.kind,
            store_id=store_id,
            title=title or spec.title or self._default_title(spec, store_id),
            instructions=spec.instructions,
            priority=spec.priority,
            end_at=spec.resolved_end_at(),
            request_id=spec.request_id,
            trip_id=spec.trip_id,
            user_id=user,
            items=[i.model_copy(deep=True) for i in spec.items],
            categories=list(spec.categories),
            aisle_ids=list(spec.aisle_ids),
            slot_ids=list(spec.slot_ids),
        )

    def _default_title(self, spec: GoalSpec, store_id: str) -> str:
        where = self._store_name(store_id)
        if spec.kind == "shop":
            return shop_goal_title(None, len(spec.items), where, store_id) if spec.items else f"Shop at {where}"
        if spec.kind == "fetch":
            item = spec.items[0]
            return f"Fetch {item.name or item.query}" + (f" x{item.qty}" if item.qty > 1 else "") + f" at {where}"
        if spec.kind == "shelf_scan":
            what = ", ".join(spec.categories + spec.aisle_ids) or ("selected shelves" if spec.slot_ids else "store")
            return f"Shelf scan: {what} at {where}"
        name = self._robot_specs().get(spec.robot_id or "")
        return f"{name.name if name else 'Robot'}: return to dock"

    async def _goal_target(self, goal: Goal) -> Pose2D | None:
        """Where the goal starts (distance term of choose_robot)."""
        if goal.kind == "return_to_dock":
            return None
        trip_route = None
        if goal.kind == "shop" and goal.trip_id and self.services.trips is not None:
            with contextlib.suppress(Exception):
                trip_route = (await self.services.trips.get_trip(goal.trip_id)).route
        if trip_route is not None:
            for stop in trip_route.ordered_stops():
                if stop.kind in ("mandatory", "optional"):
                    return stop.pose
        slot_id = None
        if goal.kind == "shelf_scan":
            remaining = [s for s in goal.slot_ids if s not in goal.scanned_slot_ids]
            slot_id = remaining[0] if remaining else None
        elif goal.items:
            slot_id = next((i.slot_id for i in goal.items if i.slot_id), None)
        if slot_id is None:
            return None
        plan = await self._planogram(goal.store_id)
        slot = plan.slot(slot_id) if plan is not None else None
        return slot.approach_pose if slot is not None else None

    async def _planogram(self, store_id: str) -> Any:
        catalog = self.services.catalog if self.services is not None else None
        if catalog is not None:
            try:
                return await catalog.get_planogram(store_id)
            except Exception as exc:
                log.debug("catalog.get_planogram(%s) failed: %s", store_id, exc)
        try:
            from utils.data import load_planogram

            return load_planogram(store_id)
        except Exception:
            return None

    def _make_assignment(
        self,
        goal: Goal,
        cand: AssignmentCandidate,
        cands: list[AssignmentCandidate],
        *,
        pinned: bool,
    ) -> Assignment:
        name = self._robot_specs()[cand.robot_id].name
        reason = cand.reason or name
        if pinned:
            reason = f"{reason} (chosen explicitly)"
        return Assignment(
            assignment_id=new_id("asg"),
            goal_id=goal.goal_id,
            robot_id=cand.robot_id,
            store_id=goal.store_id,
            attempt=len(goal.assignments) + 1,
            score=cand.score,
            breakdown=dict(cand.breakdown),
            reason=reason,
            candidates=list(cands),
            pinned=pinned,
        )

    async def _choose(
        self, goal: Goal, *, preferred: str | None = None, pinned: bool = False, exclude: set[str] | None = None
    ) -> tuple[AssignmentCandidate | None, list[AssignmentCandidate], bool]:
        """(candidate, all candidates, pinned). ``pinned`` = only ``preferred`` may take it; else ``preferred`` wins
        when it is eligible, otherwise choose_robot over the store."""
        exclude = (exclude or set()) | set(self._held)  # a stopped robot takes nothing until resumed
        infos = [i for i in await self._robot_infos(goal.store_id) if i.robot_id not in exclude]
        target = await self._goal_target(goal)
        best, cands = choose_robot(infos, store_id=goal.store_id, target=target, kind=goal.kind)
        if preferred:
            pref = next((c for c in cands if c.robot_id == preferred), None)
            if pref is not None and pref.eligible:
                return pref, cands, pinned
            if pinned:
                return None, cands, True
        return best, cands, False

    async def _assign(
        self,
        goal: Goal,
        cand: AssignmentCandidate,
        cands: list[AssignmentCandidate],
        *,
        pinned: bool,
        status: GoalStatus = GoalStatus.ASSIGNED,
        publish: bool = True,
    ) -> Assignment:
        a = self._make_assignment(goal, cand, cands, pinned=pinned)
        prev = goal.robot_id
        goal.assignments.append(a)
        goal.robot_id = a.robot_id
        if goal.status != status and can_goal_transition(goal.status, status):
            goal.status = status
        goal.updated_at = utcnow()
        if publish:
            await self._goal_event("goal.assigned", goal, assignment=a)
            await self._publish_robot(a.robot_id)
            if prev and prev != a.robot_id:
                await self._publish_robot(prev)
        if goal.trip_id and self.services.trips is not None:
            try:
                await self.services.trips.assign_robot(goal.trip_id, a.robot_id, goal.goal_id)
            except Exception as exc:
                log.warning("trips.assign_robot(%s) failed: %s", goal.trip_id, exc)
        return a

    async def _resolve_store(self, spec: GoalSpec) -> str:
        if spec.robot_id is not None:
            rstore = self._spec(spec.robot_id).store_id
            if spec.store_id is not None and spec.store_id != rstore:
                raise ValueError(f"robot {spec.robot_id} works in {rstore}, not {spec.store_id}")
            return rstore
        assert spec.store_id is not None
        if spec.store_id == ONLINE_STORE_ID:
            raise ValueError("robots only work in physical stores")
        return spec.store_id

    async def _expand_scan(self, goal: Goal) -> None:
        plan = await self._planogram(goal.store_id)
        if plan is None:
            raise ValueError(f"no planogram for store {goal.store_id!r}")
        explicit = list(goal.slot_ids)
        if not goal.categories and not goal.aisle_ids:
            slots = explicit or [s.slot_id for s in plan.slots]
        else:
            cats = {c.lower() for c in goal.categories}
            aisles = set(goal.aisle_ids)
            products = await self._products([s.sku for s in plan.slots]) if cats else {}
            slots = list(explicit)
            for s in plan.slots:
                p = products.get(s.sku)
                in_cat = p is not None and (
                    (p.category or "").lower() in cats or (getattr(p, "subcategory", None) or "").lower() in cats
                )
                if (in_cat or (s.aisle_id is not None and s.aisle_id in aisles)) and s.slot_id not in slots:
                    slots.append(s.slot_id)
        if not slots:
            what = ", ".join(goal.categories + goal.aisle_ids)
            raise ValueError(f"no shelves match {what!r} at {goal.store_id}")
        goal.slot_ids = slots

    async def _products(self, skus: list[str]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            from utils.data import load_products

            out = dict(load_products())
        except Exception:
            out = {}
        catalog = self.services.catalog if self.services is not None else None
        for sku in dict.fromkeys(skus):
            if sku in out or catalog is None:
                continue
            with contextlib.suppress(Exception):
                p = await catalog.get_product(sku)
                if p is not None:
                    out[sku] = p
        return out

    async def _resolve_fetch_item(self, goal: Goal) -> None:
        item = goal.items[0]
        catalog = self.services.catalog if self.services is not None else None
        if catalog is None or (item.sku and item.slot_id):
            return
        try:
            sku = item.sku
            if sku is None:
                from contracts.catalog import SearchQuery

                hits = await catalog.search(SearchQuery(text=item.query, store_ids=[goal.store_id], limit=3))
                if hits:
                    sku = hits[0].product.sku
                    item.name = item.name or hits[0].product.name
            if sku is None:
                return
            item.sku = sku
            locs = await catalog.get_locations(sku, goal.store_id)
            if locs:
                stocked = [loc for loc in locs if loc.qty > 0] or locs
                item.slot_id = stocked[0].slot_id
        except Exception as exc:
            log.warning("fetch item resolve failed (%s): %s", item.query, exc)

    async def create_goal(self, spec: GoalSpec) -> Goal:
        store_id = await self._resolve_store(spec)
        if spec.robot_id is not None and self._held.pop(spec.robot_id, None) is not None:
            log.info("%s resumed by a manual goal", spec.robot_id)
        await self._load_store_names()
        goal = self._new_goal(spec, store_id)
        if goal.kind == "shelf_scan":
            await self._expand_scan(goal)
        elif goal.kind == "fetch":
            await self._resolve_fetch_item(goal)

        if goal.kind == "shop":
            return _copy(await self._create_shop_goal(goal, spec))

        self._register(goal)
        cand, cands, pinned = await self._choose(goal, preferred=spec.robot_id, pinned=spec.robot_id is not None)
        await self._goal_event("goal.created", goal)
        if cand is not None:
            await self._assign(goal, cand, cands, pinned=pinned)
        self._spawn_driver(goal.goal_id, preferred=spec.robot_id, pinned=spec.robot_id is not None)
        return _copy(goal)

    async def _create_shop_goal(self, goal: Goal, spec: GoalSpec) -> Goal:
        """Shop goals wait for their trip: pre-assign a robot (PENDING), create the auto_execute trip when needed;
        the trip flow calls execute_trip once the route is planned."""
        cand, cands, pinned = await self._choose(goal, preferred=spec.robot_id, pinned=False)
        self._register(goal)
        if goal.trip_id is None:
            trips = self.services.require("trips")
            items = [
                ListItem(item_id=i.item_id, query=i.query, sku=i.sku, qty=i.qty, substitutes=list(i.substitutes))
                for i in goal.items
            ]
            owner = goal.user_id or STAFF_USER_ID
            sl = ShoppingList(list_id=new_id("list"), user_id=owner, title=goal.title, items=items)
            trip = await trips.create_trip(
                CreateTripRequest(
                    user_id=goal.user_id or STAFF_USER_ID,
                    text=goal.instructions or goal.title,
                    store_id=goal.store_id,
                    shopping_list=sl,
                    request_id=goal.request_id,
                    robot_id=cand.robot_id if cand else None,
                    auto_execute=True,
                )
            )
            goal.trip_id = trip.trip_id
        goal.items = []
        await self._goal_event("goal.created", goal)
        if cand is not None:
            await self._assign(goal, cand, cands, pinned=pinned, status=GoalStatus.PENDING)
        return goal

    # ================================================================================================================
    # execution
    # ================================================================================================================
    def _lock(self, robot_id: str) -> asyncio.Lock:
        if robot_id not in self._locks:
            self._locks[robot_id] = asyncio.Lock()
        return self._locks[robot_id]

    def _spawn_driver(self, goal_id: str, *, preferred: str | None = None, pinned: bool = False, wait: bool = True):
        task = self._drivers.get(goal_id)
        if task is not None and not task.done():
            return task
        task = asyncio.create_task(
            self._drive(goal_id, preferred=preferred, pinned=pinned, wait_for_robot=wait), name=f"goal-{goal_id}"
        )
        self._drivers[goal_id] = task

        def _forget(t: asyncio.Task[Any], gid: str = goal_id) -> None:
            if self._drivers.get(gid) is t:
                self._drivers.pop(gid, None)

        task.add_done_callback(_forget)
        return task

    async def _ensure_robot(
        self, goal: Goal, *, preferred: str | None, pinned: bool, wait_for_robot: bool
    ) -> tuple[str | None, str]:
        """Robot to run the goal now (keeping a still-eligible current assignment), or (None, failure_code)."""
        deadline = time.monotonic() + (self.robot_wait_s if wait_for_robot else 0.0)
        while True:
            exclude = self._failed_robots.get(goal.goal_id, set()) | set(self._held)
            cur = goal.assignment
            if cur is not None and cur.outcome == "active" and cur.robot_id not in exclude:
                info = self._info(cur.robot_id, await self._fetch_state(cur.robot_id))
                if score_robot(info, store_id=goal.store_id, kind=goal.kind).eligible:
                    if preferred and preferred != cur.robot_id and goal.started_at is None and not cur.pinned:
                        cand, cands, _ = await self._choose(goal, preferred=preferred, exclude=exclude)
                        if cand is not None and cand.robot_id == preferred:
                            goal.assignments.pop()
                            await self._assign(goal, cand, cands, pinned=False)
                            return preferred, ""
                    return cur.robot_id, ""
                if cur.pinned and goal.started_at is None and not wait_for_robot:
                    return None, "no_robot"
            pin = pinned or (cur is not None and cur.pinned)
            want = preferred or (cur.robot_id if cur is not None and cur.pinned else None)
            cand, cands, pinned_now = await self._choose(goal, preferred=want, pinned=pin, exclude=exclude)
            if cand is not None:
                if cur is not None and cur.outcome == "active":
                    if goal.started_at is None:
                        goal.assignments.pop()  # never started: swap the pre-assignment, not a new attempt
                    else:
                        cur.outcome = "reassigned"
                        cur.ended_at = utcnow()
                        cur.note = cur.note or "robot became unavailable"
                await self._assign(goal, cand, cands, pinned=pinned_now)
                return cand.robot_id, ""
            # A stopped robot in this store = wait for its resume instead of failing; but a goal a robot already
            # failed (handed off mid-trip) ends now with the reason instead of sitting 'assigned' until a resume.
            wait_for_resume = self._held_in_store(goal.store_id) and not self._failed_robots.get(goal.goal_id)
            if self._closed or (time.monotonic() >= deadline and not wait_for_resume):
                return None, "no_robot"
            await asyncio.sleep(self.robot_poll_s)
            goal = self._goals[goal.goal_id]
            if goal.terminal:
                return None, ""

    async def _drive(self, goal_id: str, *, preferred: str | None, pinned: bool, wait_for_robot: bool) -> None:
        """Run one goal to a terminal state (reassigning on robot failure)."""
        try:
            while True:
                goal = self._goals[goal_id]
                if goal.terminal:
                    return
                rid, code = await self._ensure_robot(
                    goal, preferred=preferred, pinned=pinned, wait_for_robot=wait_for_robot
                )
                goal = self._goals[goal_id]
                if goal.terminal:
                    return
                if rid is None:
                    msg, code = "no eligible robot in this store", code or "no_robot"
                    last = self._last_failure.get(goal_id)
                    if last is not None:
                        msg, code = self._no_takeover(goal, last[0], last[2]), last[1]
                    await self._set_status(goal, GoalStatus.FAILED, error=msg, failure_code=code)
                    return
                async with self._lock(rid):
                    goal = self._goals[goal_id]
                    if goal.terminal:
                        return
                    if goal.robot_id != rid or rid in self._held:
                        continue  # reassigned while queued, or its robot was stopped: choose again
                    self._running[rid] = goal_id
                    try:
                        err = await self._run_once(goal, rid)
                    finally:
                        if self._running.get(rid) == goal_id:
                            self._running.pop(rid, None)
                        await self._publish_robot(rid)
                if err is None:
                    return
                goal = self._goals[goal_id]
                if goal.terminal:
                    return
                self._failed_robots.setdefault(goal_id, set()).add(rid)
                code, why = robot_failure(err)
                self._last_failure[goal_id] = (rid, code, why)
                cur = goal.assignment
                if cur is not None:
                    cur.outcome = "reassigned"
                    cur.ended_at = utcnow()
                    cur.note = f"{code}: {why}"
                await self._stop_robot_quietly(rid)
                if len(goal.assignments) >= MAX_GOAL_ATTEMPTS:
                    if cur is not None:
                        cur.outcome = "failed"
                    await self._set_status(
                        goal,
                        GoalStatus.FAILED,
                        error=f"{self._spec(rid).name} could not finish: {why}; out of attempts",
                        failure_code=code,
                    )
                    return
                await self._apply(
                    goal,
                    GoalUpdate(status=GoalStatus.PENDING, note=f"{self._spec(rid).name} failed ({code}); reassigning"),
                )
                preferred, pinned, wait_for_robot = None, False, False
                exclude = self._failed_robots[goal_id]
                cand, cands, _ = await self._choose(goal, exclude=exclude)
                if cand is None:
                    if cur is not None:
                        cur.outcome = "failed"
                    await self._set_status(
                        goal, GoalStatus.FAILED, error=self._no_takeover(goal, rid, why), failure_code=code
                    )
                    return
                await self._assign(goal, cand, cands, pinned=False)
        except asyncio.CancelledError:
            goal = self._goals.get(goal_id)
            if goal is not None and not goal.terminal and self._closed:
                with contextlib.suppress(Exception):
                    await self._set_status(goal, GoalStatus.CANCELLED, error="fleet shutting down")
            raise
        except Exception as exc:  # never let a driver die silently
            log.exception("goal %s driver crashed", goal_id)
            goal = self._goals.get(goal_id)
            if goal is not None and not goal.terminal:
                with contextlib.suppress(Exception):
                    await self._set_status(goal, GoalStatus.FAILED, error=str(exc), failure_code="fleet_error")
                    await self._error(f"goal {goal_id} failed: {exc}", code="fleet_error", goal=goal)

    def _no_takeover(self, goal: Goal, failed_rid: str, why: str) -> str:
        """Shopper-readable reason for a goal whose robot failed and that no other robot in the store can take."""
        failed = self._failed_robots.get(goal.goal_id, set())
        others = []
        for rid, spec in self._robot_specs().items():
            if spec.store_id != goal.store_id or rid == failed_rid:
                continue
            if rid in self._held:
                others.append(f"{spec.name} is stopped")
            elif rid in failed:
                others.append(f"{spec.name} already tried")
            else:
                cand = score_robot(self._info(rid, self._cached_state(rid)), store_id=goal.store_id, kind=goal.kind)
                if not cand.eligible:
                    others.append(cand.reason)
        tail = f" ({', '.join(others)})" if others else ""
        store = self._store_name(goal.store_id)
        return f"{self._spec(failed_rid).name} could not finish: {why}; no other robot at {store} can take over{tail}"

    async def _run_once(self, goal: Goal, robot_id: str) -> RobotError | None:
        """One attempt on one robot. Returns the RobotError to reassign on, else None (goal is terminal)."""
        if goal.kind == "shop" and goal.trip_id and self.services.trips is not None:
            try:
                trip = await self.services.trips.get_trip(goal.trip_id)
                goal.items = goal_items_from_trip(trip)
            except Exception as exc:
                log.warning("goal %s: could not rebuild items from trip: %s", goal.goal_id, exc)
        name = self._spec(robot_id).name
        if goal.status == GoalStatus.PENDING:  # pre-assigned shop goal: pending -> assigned -> active
            goal.status = GoalStatus.ASSIGNED
        await self._apply(goal, GoalUpdate(status=GoalStatus.ACTIVE, note=f"{name} is on it"))
        brain = self.services.brain if self.services is not None else None
        if brain is None:
            await self._set_status(goal, GoalStatus.FAILED, error="robot brain not wired", failure_code="no_brain")
            await self._error("robot brain not wired", code="no_brain", goal=goal)
            return None
        task = asyncio.create_task(brain.run_goal(robot_id, _copy(goal)), name=f"brain-{robot_id}-{goal.goal_id}")
        self._brain_tasks[goal.goal_id] = task
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
        finally:
            if self._brain_tasks.get(goal.goal_id) is task:
                self._brain_tasks.pop(goal.goal_id, None)
        goal = self._goals[goal.goal_id]
        if task.cancelled():
            if not goal.terminal:
                await self._set_status(goal, GoalStatus.CANCELLED, error=goal.error or "brain task cancelled")
            return None
        exc = task.exception()
        if exc is None:
            await self._finalize(goal, task.result())
            return None
        if isinstance(exc, RobotError):
            if goal.terminal:
                return None
            log.warning("goal %s: robot %s failed: %s", goal.goal_id, robot_id, exc)
            return exc
        log.error("goal %s: brain raised %r", goal.goal_id, exc)
        if not goal.terminal:
            await self._set_status(goal, GoalStatus.FAILED, error=f"brain error: {exc}", failure_code="brain_error")
            await self._error(f"robot brain failed on goal {goal.goal_id}: {exc}", code="brain_error", goal=goal)
        return None

    async def _finalize(self, goal: Goal, result: Any) -> None:
        """run_goal returned: make sure the goal is terminal (brain normally finished it via update_goal)."""
        if goal.terminal:
            return
        res = result if isinstance(result, Goal) else None
        if res is not None:
            if not goal.items and res.items:
                goal.items = [i.model_copy(deep=True) for i in res.items]
            if res.summary and not goal.summary:
                goal.summary = res.summary
            if res.status in TERMINAL_GOAL_STATUSES and res.status != GoalStatus.CANCELLED:
                await self._set_status(goal, res.status, error=res.error, failure_code=res.failure_code)
                return
        required = [i for i in goal.items if not i.optional]
        if goal.kind in ("shop", "fetch") and required and not all(i.status in SUCCESS_ITEM_STATUSES for i in required):
            await self._set_status(
                goal, GoalStatus.FAILED, error="not every list item was picked", failure_code="incomplete"
            )
        else:
            await self._set_status(goal, GoalStatus.COMPLETED, summary=goal.summary or "done")

    async def _stop_robot_quietly(self, robot_id: str) -> None:
        robot = self.services.robot if self.services is not None else None
        if robot is None:
            return
        with contextlib.suppress(Exception):
            await asyncio.wait_for(robot.stop(robot_id), self.state_timeout_s)

    def _goal_for_trip(self, trip_id: str) -> Goal | None:
        goals = [g for g in self._goals.values() if g.trip_id == trip_id and g.kind == "shop"]
        live = [g for g in goals if not g.terminal]
        if live:
            return live[-1]
        return goals[-1] if goals else None

    async def execute_trip(self, trip_id: str, robot_id: str | None = None) -> Goal:
        trips = self.services.require("trips")
        trip = await trips.get_trip(trip_id)
        ev = self._planning.get(trip.request_id or "")
        if ev is not None:
            with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
                await asyncio.wait_for(ev.wait(), self.planning_wait_s)
        await self._load_store_names()
        goal = self._goal_for_trip(trip_id)
        if goal is not None and goal.status == GoalStatus.CANCELLED:
            return _copy(goal)
        items = goal_items_from_trip(trip)
        if goal is None or goal.terminal:
            sl = trip.shopping_list
            n = sum(1 for i in items if not i.optional)
            goal = self._register(
                Goal(
                    goal_id=new_id("goal"),
                    kind="shop",
                    store_id=trip.store_id,
                    title=shop_goal_title(sl.title if sl else None, n, self._store_name(trip.store_id), trip.store_id),
                    instructions=trip.intent.text if trip.intent else "",
                    end_at="checkout",
                    request_id=trip.request_id,
                    trip_id=trip_id,
                    user_id=trip.user_id,
                )
            )
            await self._goal_event("goal.created", goal)
        goal.items = items
        task = self._spawn_driver(goal.goal_id, preferred=robot_id, pinned=False, wait=False)
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError:
            task.cancel()
            raise
        goal = self._goals[goal.goal_id]
        if not goal.terminal:  # driver ended without finishing (should not happen)
            await self._set_status(goal, GoalStatus.FAILED, error="goal ended unexpectedly", failure_code="fleet_error")
        return _copy(goal)

    async def cancel_goal(self, goal_id: str, reason: str = "") -> Goal:
        goal = self._goal(goal_id)
        if goal.terminal:
            return _copy(goal)
        rid = goal.robot_id if self._running.get(goal.robot_id or "") == goal_id else None
        await self._set_status(goal, GoalStatus.CANCELLED, error=reason or "cancelled")
        brain_task = self._brain_tasks.get(goal_id)
        if brain_task is not None and not brain_task.done():
            brain_task.cancel()
        driver = self._drivers.get(goal_id)
        if driver is not None and not driver.done() and brain_task is None:
            driver.cancel()
        if rid is not None:
            await self._stop_robot_quietly(rid)
        return _copy(goal)

    async def message_robot(self, robot_id: str, text: str, user_id: str | None = None) -> BrainReply:
        self._spec(robot_id)
        brain = self.services.brain if self.services is not None else None
        if brain is None:
            return BrainReply(robot_id=robot_id, goal_id=self._current_id(robot_id), text="My brain is offline.")
        return await brain.handle_message(robot_id, text, user_id)

    def _held_in_store(self, store_id: str) -> bool:
        specs = self._robot_specs()
        return any(rid in specs and specs[rid].store_id == store_id for rid in self._held)

    async def stop_robot(self, robot_id: str, reason: str = "") -> RobotInfo:
        """Halt the robot, cancel its current goal, and HOLD it: its queued goals move to another robot in the store
        (choose_robot) when one is eligible, otherwise (or when pinned to it) they wait; nothing new starts on it
        until a manual goal for it (``create_goal`` with its robot_id) or ``resume_robot``."""
        spec = self._spec(robot_id)
        why = reason or "robot stopped"
        self._held[robot_id] = why
        await self._stop_robot_quietly(robot_id)
        current = self._current_id(robot_id)
        for g in list(self._goals.values()):
            if g.robot_id == robot_id and (g.goal_id == current or g.status in (GoalStatus.ACTIVE, GoalStatus.WAITING)):
                await self.cancel_goal(g.goal_id, why)
        for g in list(self._goals.values()):
            if g.robot_id != robot_id or g.terminal or g.status not in (GoalStatus.PENDING, GoalStatus.ASSIGNED):
                continue
            cur = g.assignment
            if cur is not None and cur.pinned:
                continue  # explicitly for this robot: waits for its resume
            cand, cands, _ = await self._choose(g, exclude=self._failed_robots.get(g.goal_id))
            if cand is None:
                continue  # its driver chooses again once this robot's lock frees (a handed-off goal then fails)
            if cur is not None and cur.outcome == "active":
                if g.started_at is None:
                    g.assignments.pop()  # never started: swap the pre-assignment
                else:
                    cur.outcome, cur.ended_at, cur.note = "reassigned", utcnow(), f"{spec.name} was stopped"
            await self._assign(g, cand, cands, pinned=False, status=g.status)
        await self._publish_robot(robot_id)
        return await self.get_robot(robot_id)

    async def resume_robot(self, robot_id: str) -> RobotInfo:
        """Release a stopped robot: its queued goals (and new ones) may start on it again."""
        self._spec(robot_id)
        self._held.pop(robot_id, None)
        await self._publish_robot(robot_id)
        return await self.get_robot(robot_id)

    # ================================================================================================================
    # natural-language requests
    # ================================================================================================================
    def _request(self, request_id: str) -> RequestPlan:
        try:
            return self._requests[request_id]
        except KeyError:
            raise KeyError(f"unknown request {request_id!r}") from None

    def _snapshot(self, plan: RequestPlan) -> RequestPlan:
        out = plan.model_copy(deep=True)
        ids = [g.goal_id for g in plan.goals]
        out.goals = [_copy(self._goals[g]) if g in self._goals else g2 for g, g2 in zip(ids, out.goals, strict=True)]
        return out

    async def get_request(self, request_id: str) -> RequestPlan:
        return self._snapshot(self._request(request_id))

    async def list_requests(self, user_id: str | None = None, limit: int = 20) -> list[RequestPlan]:
        plans = [p for p in reversed(list(self._requests.values())) if user_id is None or p.user_id == user_id]
        return [self._snapshot(p) for p in plans[: max(1, limit)]]

    async def _publish_plan(self, plan: RequestPlan) -> None:
        plan.updated_at = utcnow()
        await self._publish(
            "request.planned",
            trip_id=None,
            request_id=plan.request_id,
            user_id=plan.user_id,
            plan=self._snapshot(plan),
        )

    async def _refresh_request(self, request_id: str) -> None:
        plan = self._requests.get(request_id)
        if plan is None or plan.status != "executing" or not plan.goals:
            return
        goals = [self._goals.get(g.goal_id, g) for g in plan.goals]
        if not all(g.terminal for g in goals):
            return
        ok = [g for g in goals if g.status == GoalStatus.COMPLETED]
        plan.status = "completed" if ok else "failed"
        if not ok:
            plan.error = "; ".join(g.error or g.failure_code or g.status.value for g in goals)
        await self._publish_plan(plan)

    async def submit_request(self, request: FleetRequest) -> RequestPlan:
        history: list[str] | None = None
        if request.in_reply_to:
            plan = self._request(request.in_reply_to)
            question = (plan.interpretation.clarifying_question if plan.interpretation else None) or plan.reply
            history = [plan.text, *([question] if question else []), *plan.replies]
            plan.replies.append(request.text)
            plan.status = "received"
            plan.error = None
        else:
            plan = RequestPlan(
                request_id=new_id("req"),
                text=request.text.strip() or await self._order_text(request),
                user_id=request.user_id,
                channel=request.channel,
                role=request.role,
            )
            self._requests[plan.request_id] = plan
        await self._publish(
            "request.received", trip_id=None, request_id=plan.request_id, user_id=plan.user_id, request=request
        )
        ev = asyncio.Event()
        self._planning[plan.request_id] = ev
        try:
            await self._plan_request(plan, request, history)
        except Exception as exc:
            log.exception("request %s failed", plan.request_id)
            plan.status = "failed"
            plan.error = str(exc)
            plan.reply = f"Sorry, I couldn't plan that: {exc}"
            await self._error(
                f"request failed: {exc}", code="request_failed", request_id=plan.request_id, user_id=plan.user_id
            )
        finally:
            ev.set()
            if self._planning.get(plan.request_id) is ev:
                self._planning.pop(plan.request_id, None)
        await self._publish_plan(plan)
        return self._snapshot(plan)

    # ---- interpretation --------------------------------------------------------------------------------------------
    async def _rule_context(self) -> rules.RuleContext:
        specs = self._robot_specs()
        names = await self._load_store_names()
        ctx = rules.RuleContext(
            robots={rid: s.name for rid, s in specs.items()},
            robot_stores={rid: s.store_id for rid, s in specs.items()},
            stores={sid: rules.store_keywords(sid, name) for sid, name in names.items()},
        )
        try:
            from utils.data import load_categories

            for cid, c in load_categories().items():
                ctx.categories[cid] = rules.category_keywords(cid, c.name, list(c.aliases))
        except Exception:
            pass
        catalog = self.services.catalog if self.services is not None else None
        if catalog is not None:
            with contextlib.suppress(Exception):
                for c in await catalog.list_categories():
                    if c.category_id not in ctx.categories:
                        ctx.categories[c.category_id] = rules.category_keywords(c.category_id, c.name)
        if not ctx.categories:
            from contracts.catalog import STANDARD_CATEGORY_IDS

            for cid in STANDARD_CATEGORY_IDS:
                ctx.categories[cid] = rules.category_keywords(cid, None)
        for sid in names:
            plan = await self._planogram(sid)
            if plan is not None:
                ctx.aisles[sid] = {a.aisle_id: a.label for a in plan.aisles}
        return ctx

    async def _profile(self, user_id: str, role: str) -> Any:
        rec = self.services.recommender if self.services is not None else None
        if rec is None or role != "shopper":
            return None
        try:
            return await rec.get_profile(user_id)
        except Exception:
            return None

    def _clean_draft(self, draft: RequestDraft, ctx: rules.RuleContext, fallback: RequestDraft) -> RequestDraft:
        """Code decides: drop hints that name unknown robots/stores, fill them from the rules."""
        upd: dict[str, Any] = {}
        if draft.robot_hint not in ctx.robots:
            upd["robot_hint"] = fallback.robot_hint
        if draft.store_hint not in ctx.stores:
            upd["store_hint"] = fallback.store_hint
        if draft.intent == "shelf_scan" and not draft.categories and not draft.aisle_ids:
            upd["categories"] = fallback.categories
            upd["aisle_ids"] = fallback.aisle_ids
        return draft.model_copy(update=upd) if upd else draft

    # ---- structured orders (0.2.3: exact SKUs, no LLM) ---------------------------------------------------------
    @staticmethod
    def _merged_items(request: FleetRequest) -> dict[str, tuple[int, str]]:
        merged: dict[str, tuple[int, str]] = {}
        for it in request.items:
            qty, note = merged.get(it.sku, (0, ""))
            merged[it.sku] = (qty + it.qty, note or it.note)
        return merged

    async def _order_text(self, request: FleetRequest) -> str:
        """Chat-history text of a structured order: 'Order: 1 x Mild Chunky Salsa 16 oz, 4 x Lime'."""
        merged = self._merged_items(request)
        products = await self._products(list(merged))
        parts = [f"{qty} x {getattr(products.get(sku), 'name', None) or sku}" for sku, (qty, _) in merged.items()]
        return "Order: " + ", ".join(parts)

    async def _structured_draft(self, request: FleetRequest) -> RequestDraft:
        """A structured order -> a shop draft whose list pins every SKU (sourcing keeps the exact SKU in store, else
        orders it online); unknown SKUs are dropped and named in the summary."""
        merged = self._merged_items(request)
        products = await self._products(list(merged))
        unknown = [sku for sku in merged if sku not in products]
        list_id = new_id("list")
        items: list[ListItem] = []
        for sku, (qty, note) in merged.items():
            product = products.get(sku)
            if product is None:
                continue
            items.append(
                ListItem(
                    item_id=f"{list_id}_i{len(items) + 1}",
                    query=product.name,
                    sku=sku,
                    qty=qty,
                    category=product.category,
                    notes=note or "ordered by the shopper",
                )
            )
        if not items:
            return RequestDraft(
                intent="shop",
                summary="Order",
                clarifying_question=f"I couldn't find {', '.join(unknown)} in the catalog. What should I get?",
                confidence=0.0,
            )
        title = f"Your order ({plural(len(items), 'item')})"
        sl = ShoppingList(list_id=list_id, user_id=request.user_id, title=title, items=items)
        summary = title + (f"; not in the catalog: {', '.join(unknown)}" if unknown else "")
        return RequestDraft(
            intent="shop", summary=summary, shopping_list=sl, store_hint=request.store_id, confidence=1.0
        )

    async def _interpret(self, plan: RequestPlan, request: FleetRequest, history: list[str] | None) -> RequestDraft:
        if request.items and not history:
            return await self._structured_draft(request)
        ctx = await self._rule_context()
        text = request.text
        combined = " ".join([history[0], text]) if history else text
        profile = await self._profile(request.user_id, request.role)
        base = rules.interpret(combined, role=request.role, store_id=request.store_id, ctx=ctx)
        # an answer to "what should I get?" stands alone
        if history and base.clarifying_question and base.intent == "shop":
            base = rules.interpret(text, role=request.role, store_id=request.store_id, ctx=ctx)
        agent = self.services.agent if self.services is not None else None

        if self.mode == "real" and agent is not None and hasattr(agent, "interpret_request"):
            try:
                draft = await agent.interpret_request(
                    text, request.user_id, request.store_id, role=request.role, profile=profile, history=history
                )
                draft = self._clean_draft(RequestDraft.model_validate(draft.model_dump()), ctx, base)
                if (
                    draft.intent == "shop"
                    and not draft.clarifying_question
                    and not (draft.shopping_list and draft.shopping_list.items)
                ):
                    sl = await self._shopping_list(text, request, profile, history, want_shop=True)
                    draft = draft.model_copy(update={"shopping_list": sl})
                return draft
            except Exception as exc:
                log.warning("agent.interpret_request failed (%s); using keyword rules", exc)
                await self._error(
                    f"interpretation fell back to rules: {exc}",
                    code="interpret_fallback",
                    request_id=plan.request_id,
                    user_id=plan.user_id,
                )

        if (
            base.intent == "status"
            and not base.reply
            and hasattr(agent, "interpret_request")
            and not _PERSONAL_STATUS_RE.search(text)
        ):
            # a product question ("where is the salsa?"): the agent answers it from the catalog (rules in the fake)
            try:
                d = await agent.interpret_request(
                    text, request.user_id, request.store_id, role=request.role, profile=profile, history=history
                )
                if d.intent == "status" and d.reply:
                    base = base.model_copy(update={"reply": d.reply})
            except Exception as exc:  # noqa: BLE001 - fall back to the goal-status reply
                log.debug("agent.interpret_request unavailable for a product question (%s)", exc)
        if base.intent not in ("shop", "fetch") or base.clarifying_question:
            return base
        shop_text = text if (history and base.intent == "shop" and len(text.split()) > 2) else combined
        sl = await self._shopping_list(shop_text, request, profile, history, want_shop=base.intent == "shop")
        if base.intent == "fetch":
            query, qty = rules.fetch_query(combined, ctx)
            return base.model_copy(update={"summary": f"Fetch {query}" + (f" x{qty}" if qty > 1 else "")})
        if sl is None or not sl.items:
            return base.model_copy(
                update={
                    "clarifying_question": "What should I get? For example: 'tacos for 6, under $40, one vegetarian'.",
                    "confidence": 0.3,
                }
            )
        summary = sl.title or base.summary
        if sl.budget_cents is not None:
            summary = f"{summary}, under {fmt_cents(sl.budget_cents)}"
        return base.model_copy(update={"shopping_list": sl, "summary": summary})

    async def _shopping_list(
        self, text: str, request: FleetRequest, profile: Any, history: list[str] | None, *, want_shop: bool
    ) -> ShoppingList | None:
        agent = self.services.agent if self.services is not None else None
        if agent is None or not want_shop:
            return None
        if hasattr(agent, "interpret_request"):
            try:
                d = await agent.interpret_request(
                    text, request.user_id, request.store_id, role=request.role, profile=profile, history=history
                )
                if d.intent == "shop" and d.shopping_list is not None and d.shopping_list.items:
                    return d.shopping_list
            except Exception as exc:
                log.debug("agent.interpret_request unavailable (%s); using expand_intent", exc)
        try:
            intent = Intent(
                intent_id=new_id("intent"),
                user_id=request.user_id,
                text=text,
                channel=request.channel,
                store_id=request.store_id,
            )
            return await agent.expand_intent(intent, profile)
        except Exception as exc:
            log.warning("expand_intent failed: %s", exc)
            return None

    # ---- planning --------------------------------------------------------------------------------------------------
    async def _plan_request(self, plan: RequestPlan, request: FleetRequest, history: list[str] | None) -> None:
        draft = await self._interpret(plan, request, history)
        plan.interpretation = draft
        intent = draft.intent
        if draft.clarifying_question and intent not in ("status", "chat"):
            plan.status = "needs_input"
            plan.reply = draft.clarifying_question
            return
        if intent == "chat":
            plan.status = "completed"
            plan.reply = draft.reply or "Hi! Tell me what you need, e.g. 'tacos for 6, under $40, one vegetarian'."
            return
        if intent == "status":
            plan.status = "completed"
            plan.reply = draft.reply or self._status_reply(request.user_id, draft)
            return
        if intent in ("fetch", "shelf_scan", "return_to_dock") and request.role != "staff":
            plan.status = "rejected"
            what = {"fetch": "fetch runs", "shelf_scan": "shelf scans", "return_to_dock": "robot docking"}[intent]
            plan.reply = f"Only store staff can request {what}. I can shop for you though: tell me what you need."
            return
        if intent == "shop":
            await self._plan_shop(plan, request, draft)
        elif intent == "fetch":
            await self._plan_fetch(plan, request, draft)
        elif intent == "shelf_scan":
            await self._plan_scan(plan, request, draft)
        else:
            await self._plan_dock(plan, request, draft)

    def _status_reply(self, user_id: str, draft: RequestDraft) -> str:
        live = [
            g
            for g in self._goals.values()
            if not g.terminal
            and (g.user_id == user_id or (draft.robot_hint is not None and g.robot_id == draft.robot_hint))
        ]
        if not live:
            recent = [g for g in self._goals.values() if g.user_id == user_id]
            if recent:
                g = recent[-1]
                return f"Your last goal '{g.title}' is {g.status.value}" + (f": {g.summary}" if g.summary else ".")
            return "Nothing is running for you right now. Tell me what you need!"
        parts = []
        specs = self._robot_specs()
        for g in live:
            who = specs[g.robot_id].name if g.robot_id in specs else "A robot"
            done = sum(1 for i in g.items if i.status in DONE_ITEM_STATUSES)
            prog = f" ({done}/{plural(len(g.items), 'item')})" if g.items else ""
            note = f" - {g.progress_note}" if g.progress_note else ""
            parts.append(f"{who}: {g.title}, {g.status.value}{prog}{note}")
        return "; ".join(parts)

    async def _plan_shop(self, plan: RequestPlan, request: FleetRequest, draft: RequestDraft) -> None:
        sl = draft.shopping_list
        if sl is None or not sl.items:
            plan.status = "needs_input"
            plan.reply = "What should I get? For example: 'tacos for 6, under $40, one vegetarian'."
            return
        trips = self.services.require("trips")
        names = await self._load_store_names()
        hint = draft.store_hint if draft.store_hint in names else None
        if hint is None and request.store_id in names:
            hint = request.store_id
        infos = await self._robot_infos()
        free: dict[str, int] = {}
        for info in infos:
            if score_robot(info, store_id=info.store_id, kind="shop").eligible:
                free[info.store_id] = free.get(info.store_id, 0) + 1
        catalog = self.services.catalog if self.services is not None else None
        evals, online = await evaluate_stores(catalog, sl.items, names, free)
        profile = await self._profile(request.user_id, request.role)
        home = getattr(profile, "home_store_id", None) or self._default_store(names)
        sp = choose_stores(sl.items, evals, online, hint=hint, home=home, allow_split=True)
        plan.store_choices = sp.choices
        chosen = sp.chosen
        split = len(chosen) > 1
        est = {c.store_id: c.est_total_cents for c in sp.choices if c.chosen}
        shares = split_budget(sl.budget_cents, est) if (split and sl.budget_cents is not None) else {}
        plan.budget_split = shares if split else ({chosen[0]: sl.budget_cents} if sl.budget_cents is not None else {})
        lines: list[str] = []
        for sid in chosen:
            sub = sub_list(
                sl,
                sid,
                sp.assignment[sid],
                sp.evals,
                primary=sid == sp.primary,
                split=split,
                budget_cents=shares.get(sid, sl.budget_cents) if split else sl.budget_cents,
                store_name=names.get(sid),
            )
            goal = Goal(
                goal_id=new_id("goal"),
                kind="shop",
                store_id=sid,
                title=shop_goal_title(sub.title or draft.summary, len(sub.items), names.get(sid, sid), sid),
                instructions=draft.summary,
                priority=draft.priority,
                end_at="checkout",
                request_id=plan.request_id,
                user_id=request.user_id,
            )
            cand, cands, _ = await self._choose(goal, preferred=draft.robot_hint if split is False else None)
            self._register(goal)
            trip = await trips.create_trip(
                CreateTripRequest(
                    user_id=request.user_id,
                    text=plan.text,
                    channel=request.channel,
                    store_id=sid,
                    shopping_list=sub,
                    request_id=plan.request_id,
                    robot_id=cand.robot_id if cand else None,
                    auto_execute=True,
                )
            )
            goal.trip_id = trip.trip_id
            await self._goal_event("goal.created", goal)
            if cand is not None:
                await self._assign(goal, cand, cands, pinned=False, status=GoalStatus.PENDING)
            plan.goals.append(_copy(goal))
            plan.trip_ids.append(trip.trip_id)
            who = self._robot_specs()[cand.robot_id].name if cand else "the next free robot"
            lines.append(f"{who} at {names.get(sid, sid)} ({plural(len(sub.items), 'item')})")
        plan.status = "executing"
        budget = f", budget {fmt_cents(sl.budget_cents)}" if sl.budget_cents is not None else ""
        if split:
            plan.reply = f"Splitting your list{budget}: " + "; ".join(lines) + "."
        else:
            missing = [q for c in sp.choices if c.chosen for q in c.missing]
            extra = ""
            if missing:
                extra = f" {', '.join(missing)} isn't in stock there, so it will come from the online store."
            plan.reply = f"On it: {lines[0]}{budget}.{extra}"

    def _store_for(self, draft: RequestDraft, request: FleetRequest) -> str:
        if draft.robot_hint in self._robot_specs():
            return self._robot_specs()[draft.robot_hint].store_id
        names = self._store_names
        if draft.store_hint in names:
            return draft.store_hint  # type: ignore[return-value]
        if request.store_id in names:
            return request.store_id  # type: ignore[return-value]
        return self._default_store(names)

    @staticmethod
    def _default_store(names: dict[str, str]) -> str:
        try:
            from utils.config import get_settings

            default = get_settings().picky_default_store
        except Exception:
            default = DEFAULT_STORE_ID
        return default if default in names or not names else next(iter(names))

    async def _plan_goal(self, plan: RequestPlan, spec: GoalSpec) -> Goal:
        goal = await self.create_goal(spec)
        plan.goals.append(goal)
        plan.status = "executing"
        return goal

    def _who(self, goal: Goal) -> str:
        spec = self._robot_specs().get(goal.robot_id or "")
        return spec.name if spec else "The next free robot"

    async def _plan_fetch(self, plan: RequestPlan, request: FleetRequest, draft: RequestDraft) -> None:
        ctx_text = " ".join([plan.text, *plan.replies])
        query, qty = rules.fetch_query(ctx_text, await self._rule_context())
        item = GoalItem(query=query, qty=qty)
        if draft.shopping_list is not None and draft.shopping_list.items:
            li = draft.shopping_list.items[0]
            item = GoalItem(query=li.query, sku=li.sku, qty=li.qty, substitutes=list(li.substitutes))
        store = await self._fetch_store(item, draft, request)
        robot = draft.robot_hint if draft.robot_hint and self._spec(draft.robot_hint).store_id == store else None
        goal = await self._plan_goal(
            plan,
            GoalSpec(
                kind="fetch",
                store_id=store,
                robot_id=robot,
                items=[item],
                user_id=request.user_id,
                request_id=plan.request_id,
                priority=draft.priority,
                instructions=plan.text,
            ),
        )
        plan.reply = f"{self._who(goal)} will fetch {item.query} at {self._store_name(store)}."

    async def _fetch_store(self, item: GoalItem, draft: RequestDraft, request: FleetRequest) -> str:
        if draft.robot_hint or draft.store_hint or request.store_id:
            return self._store_for(draft, request)
        names = await self._load_store_names()
        catalog = self.services.catalog if self.services is not None else None
        li = ListItem(item_id="fetch", query=item.query, sku=item.sku, qty=item.qty, substitutes=item.substitutes)
        evals, online = await evaluate_stores(catalog, [li], names, {sid: 1 for sid in names})
        sp = choose_stores([li], evals, online, allow_split=False)
        return sp.primary

    async def _plan_scan(self, plan: RequestPlan, request: FleetRequest, draft: RequestDraft) -> None:
        store = self._store_for(draft, request)
        robot = draft.robot_hint if draft.robot_hint and self._spec(draft.robot_hint).store_id == store else None
        aisles = list(draft.aisle_ids)
        p = await self._planogram(store)
        if p is not None and aisles:
            aisles = [a for a in aisles if p.aisle(a) is not None]
        goal = await self._plan_goal(
            plan,
            GoalSpec(
                kind="shelf_scan",
                store_id=store,
                robot_id=robot,
                categories=list(draft.categories),
                aisle_ids=aisles,
                user_id=request.user_id,
                request_id=plan.request_id,
                priority=draft.priority,
                instructions=plan.text,
            ),
        )
        what = ", ".join(draft.categories + aisles) or "the whole store"
        plan.reply = (
            f"{self._who(goal)} will scan {what} at {self._store_name(store)} ({len(goal.slot_ids)} shelf slots)."
        )

    async def _plan_dock(self, plan: RequestPlan, request: FleetRequest, draft: RequestDraft) -> None:
        rid = draft.robot_hint
        if rid not in self._robot_specs():
            plan.status = "needs_input"
            plan.reply = "Which robot should go charge? " + ", ".join(s.name for s in self._robot_specs().values())
            return
        spec = self._spec(rid)  # type: ignore[arg-type]
        # stop what it is doing first: docking wins over its current goal
        current = self._current_id(spec.robot_id)
        if current is not None:
            await self.cancel_goal(current, "sent to the dock")
        await self._plan_goal(
            plan,
            GoalSpec(
                kind="return_to_dock",
                robot_id=spec.robot_id,
                user_id=request.user_id,
                request_id=plan.request_id,
                priority="high",
                instructions=plan.text,
            ),
        )
        plan.reply = f"Sending {spec.name} back to the dock to charge."


__all__ = ["DEFAULT_ROBOTS", "FleetManager"]
