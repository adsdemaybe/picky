"""RobotBrainService: the per-robot managing LLM (and its deterministic fake).

- ``ScriptedBrain`` (``create_brain_fake``): runs ``brain_script.ScriptedRunner`` over the brain tools; no LLM.
- ``ClaudeBrain`` (``create_brain``): a Claude tool loop (claude-sonnet-5-5 via ``utils.llm``) over
  ``brain_tool_definitions(strict=True)``, <= MAX_ITERS model turns per goal with progress checkpoints (incl. a front
  camera summary = the video input) and user messages injected between turns. Any LLM failure (no key, overload,
  refusal, max_tokens, malformed output, iteration cap) -> error event + brain.step(kind='error'), mode 'scripted',
  and the scripted runner finishes the goal from its current state.

Both keep a per-robot ring of recent BrainSteps (``recent_steps``), publish every step as ``brain.step`` and handle
``handle_message`` (add / skip / status / what do you see) by keyword; the Claude brain additionally queues messages
for the running LLM goal.

Plan mode (contracts 0.2.1, ``loop.resolve_exec_mode`` == 'plan': PICKY_EXEC_MODE=plan, or unset with the VLA +
verifier wired): shop / fetch goals with items run through ``services.runner.run_goal`` (planner -> VLA -> verifier,
``arms/agent/loop.py``); the brain then finishes the goal with ``fleet.update_goal(bridge.goal_update_from_outcome)``.
A RobotError from the runner propagates (the fleet reassigns); a runner / planner crash falls back to this v0.2
executor from the fleet's current goal state. 'direct' (and every other goal kind) is the exact v0.2 path.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from arms.agent.brain_exec import ACTIVE_ITEM_STATUSES, BrainExecutor, apply_update_locally
from arms.agent.brain_script import ScriptedRunner
from arms.agent.loop import PlanLoop, resolve_exec_mode
from contracts.bridge import MIRROR_MODEL, goal_update_from_outcome
from contracts.common import RobotId, Timestamp, UserId, utcnow
from contracts.events import goal_envelope, make_event
from contracts.fleet import (
    BrainReply,
    BrainState,
    BrainStatus,
    BrainStep,
    BrainStepKind,
    Goal,
    GoalStatus,
)
from contracts.plan import CLOSED_STEP_STATUSES
from contracts.services import Services
from contracts.sim import RobotError
from contracts.tools import BrainToolError, brain_tool_definitions
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)

STEP_RING = 1000
"""BrainSteps kept per robot (contract: >= 500)."""
MAX_ITERS = 25
"""LLM turns per goal before the scripted executor takes over."""
CHECKPOINT_EVERY = 6
"""Every N turns the loop adds a progress checkpoint + front-camera summary to the conversation."""
SCRIPTED = "scripted"
PLAN_MODE_GOAL_KINDS = ("shop", "fetch")
"""Goal kinds the loop runner executes in plan mode; shelf_scan / return_to_dock stay on the v0.2 executor."""

_ADD_RE = re.compile(
    r"^\s*(?:(?:can you |could you |please )?(?:also |and )?(?:add|grab|get|pick up|buy)|i also need|also)\s+"
    r"(?:me\s+)?(?:some\s+|a\s+|an\s+)?(?:(\d+)\s+)?(.+?)\s*(?:too|as well|please)?[.!?]*$",
    re.I,
)
_SKIP_RE = re.compile(
    r"^\s*(?:please\s+)?(?:skip|drop|remove|forget(?: about)?|no|cancel|don'?t (?:get|buy|grab))\s+(?:the\s+)?(.+?)"
    r"[.!?]*$",
    re.I,
)
_WHERE_RE = re.compile(
    r"(?:where(?:'s| is| are| do you keep| can i (?:find|get))|do you (?:have|sell|carry)|i'?m looking for|"
    r"looking for|can you find(?: me)?|help me find)\s+(?:the |some |a |an |any )?(?P<q>[^?.!]+)",
    re.IGNORECASE,
)
_CHEAPER_RE = re.compile(
    r"(?:cheaper|less expensive|budget|cheapest)\s+(?:alternative|option|version|one|brand)?s?\s*"
    r"(?:to|for|than|of)?\s+(?:the |a |an |that |this )?(?P<q>[^?.!]+)",
    re.IGNORECASE,
)
_SEE_RE = re.compile(r"\b(?:see|look|camera|in front of you|around you|what'?s there)\b", re.I)
_STATUS_RE = re.compile(
    r"\b(?:what(?:'re| are) you (?:doing|up to|working on)|status|progress|how(?:'s| is) it going|where are you"
    r"|how far|are you done|what'?s up)\b",
    re.I,
)
_CLAUSE_SPLIT_RE = re.compile(r"\band\b|\balso\b|[,;?!.]", re.I)


def _see_clause(text: str) -> str:
    """The part of a combined question that is about the cameras ('what are you doing and what do you see?' ->
    'what do you see'), so the vision answer isn't matched against the status words."""
    parts = [p.strip() for p in _CLAUSE_SPLIT_RE.split(text) if p and p.strip()]
    seen = [p for p in parts if _SEE_RE.search(p)]
    return " ".join(seen) if seen else text


BRAIN_SYSTEM = """You are the managing brain of one Picky store robot: a small wheeled rover (radius 0.3 m) carrying \
a vertical lift (0.2-1.2 m) with an SO-101 robot arm (reach about 0.35 m) and a basket on its back. You work inside \
one physical grocery store and see through three cameras: front (mast pan-tilt head, the shelf ahead), wrist (close-up \
from the gripper) and basket. Faces are blurred before you see anything.

You are given ONE goal. Achieve it with the tools, then call finish_goal exactly once.

How to work:
- Shopping goal: plan_route first. For each item in route order: if it is optional (a recommendation detour) ask_user \
kind=recommendation BEFORE driving there, and on 'no' update_progress item_status=declined. Otherwise navigate_to the \
item's stop, look (front) and scan_shelf. Empty shelf -> update_progress item_status=unavailable (it gets ordered \
online). Produce -> ask_user kind=pick_choice and pick the instance the shopper chose. pick_item one unit at a \
time and place_in_basket after each unit. If pick_item says needs_approval, ask_user kind=over_threshold with the \
item_id and pick again with that approval_id; 'no' -> item declined. grasp_failed -> retry once, then mark the \
item failed.
- Fetch: locate the item if needed, navigate, scan, pick, place, finish.
- Shelf scan: plan_route over the slots, then navigate_to + scan_shelf each slot; report out-of-stock and mispriced.
- Return to dock: navigate_to dock, finish.
- Errors: path_blocked / timeout / disconnected -> plan_route again from where you are and retry once. Never invent \
SKUs, slots or prices; use locate_items. Money is integer cents. You cannot pay; checkout happens after you finish.
- User messages may arrive mid-goal ('also grab limes' -> add_item; 'skip the cheese' -> update_progress skipped).
- Keep thoughts to one short sentence. Report progress with update_progress now and then."""


@dataclass
class BrainRuntime:
    robot_id: RobotId
    state: BrainState = "idle"
    goal: Goal | None = None
    mode: str = SCRIPTED
    model: str | None = None
    iterations: int = 0
    current_tool: str | None = None
    last_step: BrainStep | None = None
    step_no: int = 0
    steps: deque[BrainStep] = field(default_factory=lambda: deque(maxlen=STEP_RING))
    inbox: list[str] = field(default_factory=list)
    executor: BrainExecutor | None = None
    plan_mode: bool = False
    """True while ``services.runner`` runs this robot's goal (plan mode)."""
    updated_at: Timestamp = field(default_factory=utcnow)


class _BrainBase:
    """Steps ring, status, messages and the run_goal wrapper shared by the fake and the real brain."""

    model_name: str = SCRIPTED

    def __init__(self, services: Services) -> None:
        self._services = services
        self._rt: dict[RobotId, BrainRuntime] = {}

    def _runtime(self, robot_id: RobotId) -> BrainRuntime:
        rt = self._rt.get(robot_id)
        if rt is None:
            rt = self._rt[robot_id] = BrainRuntime(robot_id=robot_id)
        return rt

    # -- steps ---------------------------------------------------------------------------------------------------
    async def _emit(
        self,
        robot_id: RobotId,
        kind: BrainStepKind,
        text: str = "",
        *,
        goal: Goal | None = None,
        model: str | None = None,
        role: str = "robot",
        store_id: str | None = None,
        **kw: Any,
    ) -> BrainStep:
        rt = self._runtime(robot_id)
        rt.step_no += 1
        step = BrainStep(
            step_id=new_id("step"),
            robot_id=robot_id,
            goal_id=goal.goal_id if goal else None,
            trip_id=goal.trip_id if goal else None,
            store_id=goal.store_id if goal else store_id,
            step_no=rt.step_no,
            kind=kind,
            role=role,  # type: ignore[arg-type]
            text=text,
            model=model or rt.model or self.model_name,
            **kw,
        )
        rt.steps.append(step)
        rt.last_step = step
        rt.updated_at = step.ts
        if goal is not None and role == "robot" and rt.state != "error":
            if kind == "tool_call":
                rt.state = "waiting_user" if kw.get("tool") == "ask_user" else "acting"
            elif kind == "thought":
                rt.state = "thinking"
        events = self._services.events
        if events is not None:
            env = goal_envelope(goal, robot_id) if goal else {"trip_id": None, "store_id": step.store_id}
            env.setdefault("robot_id", robot_id)
            try:
                await events.publish(make_event("brain.step", **env, source="agent", step=step))
            except Exception as e:
                log.warning("publish brain.step failed: %s", e)
        return step

    def _executor(self, robot_id: RobotId, goal: Goal, model: str) -> BrainExecutor:
        rt = self._runtime(robot_id)

        async def emit(kind: BrainStepKind, text: str = "", **kw: Any) -> BrainStep:
            return await self._emit(robot_id, kind, text, **kw)

        def on_tool(name: str | None) -> None:
            rt.current_tool = name
            if name is None and rt.state in ("acting", "waiting_user"):
                rt.state = "thinking"

        ex = BrainExecutor(self._services, robot_id, goal, emit=emit, model=model, on_tool=on_tool)
        rt.executor = ex
        return ex

    # -- RobotBrainService ---------------------------------------------------------------------------------------
    async def run_goal(self, robot_id: RobotId, goal: Goal) -> Goal:
        if self._wants_plan_mode(goal):
            finished, goal = await self._run_plan_mode(robot_id, goal)
            if finished:
                return goal
            # the planner / runner crashed: the v0.2 executor finishes from the fleet's current goal state
        return await self._run_direct(robot_id, goal)

    async def _run_direct(self, robot_id: RobotId, goal: Goal) -> Goal:
        rt = self._runtime(robot_id)
        rt.goal, rt.iterations, rt.inbox = goal, 0, []
        rt.state, rt.mode, rt.model = "thinking", SCRIPTED, SCRIPTED
        ex = self._executor(robot_id, goal, SCRIPTED)
        try:
            await self._run(rt, ex)
            if not ex.finished and not ex.goal.terminal:
                await ex.call("finish_goal", {"status": "failed", "summary": "Stopped before finishing."})
            return ex.goal
        except asyncio.CancelledError:
            await self._withdraw(ex, "goal cancelled")
            raise
        except RobotError as e:  # fatal (battery_low / unknown_robot / robot in ERROR): let the fleet reassign
            rt.state = "error"
            await ex.emit("error", f"Robot failure: {e.code}. Handing the goal back to the fleet.")
            await self._withdraw(ex, f"robot failure: {e.code}")
            raise
        except Exception as e:  # a bug in the brain: never kill the fleet task
            log.exception("brain %s crashed on %s", robot_id, goal.goal_id)
            await ex.emit("error", f"Internal error: {e}", ok=False)
            await ex.error(f"robot brain crashed: {e}", code="internal")
            if not ex.finished and not ex.goal.terminal:
                with contextlib.suppress(Exception):
                    await ex.call("finish_goal", {"status": "failed", "summary": f"Brain error: {e}"})
            return ex.goal
        finally:
            if rt.state != "error":
                rt.state = "idle"
            rt.current_tool = None
            rt.goal = ex.goal if not ex.goal.terminal else None
            if rt.goal is None:
                rt.executor = None

    async def _run(self, rt: BrainRuntime, ex: BrainExecutor) -> None:
        await ScriptedRunner(ex).run()

    # -- plan mode (0.2.1: planner -> VLA -> verifier) -------------------------------------------------------------
    def _wants_plan_mode(self, goal: Goal) -> bool:
        if goal.kind not in PLAN_MODE_GOAL_KINDS or not goal.items:
            return False
        try:
            mode = resolve_exec_mode(self._services)
        except Exception as e:  # a broken setting never blocks a goal: v0.2 path
            log.warning("exec mode check failed (%s); direct", e)
            return False
        if mode == "tools":
            log.info("PICKY_EXEC_MODE=tools is not wired into the brain executor yet; running direct")
        return mode == "plan"

    def _record_mirror(self, step: BrainStep) -> None:
        """Runner hook: keep the mirrored ``brain.step`` (already published) in the robot's ring and status."""
        rt = self._runtime(step.robot_id)
        rt.steps.append(step)
        rt.last_step = step
        rt.current_tool = step.tool
        rt.updated_at = step.ts
        if rt.plan_mode and rt.state != "error":
            rt.state = "waiting_user" if step.tool == "ask_user" and step.ok else "acting"

    async def _run_plan_mode(self, robot_id: RobotId, goal: Goal) -> tuple[bool, Goal]:
        """Run the goal through the loop runner and finish it: (True, the fleet's final Goal), or (False, the goal to
        continue on the v0.2 executor) when the planner / runner crashed. RobotError and cancellation propagate."""
        rt = self._runtime(robot_id)
        services = self._services
        runner = services.require("runner")
        if isinstance(runner, PlanLoop):
            runner.on_brain_step = self._record_mirror
        rt.goal, rt.iterations, rt.inbox, rt.executor = goal, 0, [], None
        rt.state, rt.mode, rt.model, rt.plan_mode = "thinking", SCRIPTED, MIRROR_MODEL, True
        await self._emit(
            robot_id, "thought", f"Planning '{goal.title}' as verified steps (plan -> VLA -> verifier).", goal=goal
        )
        rt.state = "acting"
        try:
            outcome = await runner.run_goal(robot_id, goal)
        except asyncio.CancelledError:
            await self._withdraw_goal(goal.goal_id, "goal cancelled")
            rt.plan_mode = False
            rt.state, rt.goal, rt.current_tool = "idle", None, None
            raise
        except RobotError as e:  # escalate: hand the goal back to the fleet without failing it
            rt.state = "error"
            text = f"Robot failure: {e.code}. Handing the goal back to the fleet."
            await self._emit(robot_id, "error", text, goal=goal)
            await self._withdraw_goal(goal.goal_id, f"robot failure: {e.code}")
            rt.plan_mode, rt.goal, rt.current_tool = False, None, None
            raise
        except Exception as e:  # a planner / runner bug never kills the fleet task: finish on the v0.2 executor
            log.exception("plan mode crashed for %s on %s; v0.2 executor takes over", robot_id, goal.goal_id)
            rt.plan_mode = False
            await self._emit(robot_id, "error", f"Plan mode failed ({type(e).__name__}: {e}); continuing.", goal=goal)
            await self._publish_error(goal, robot_id, f"plan mode failed, v0.2 executor takes over: {e}")
            return False, await self._fresh_goal(goal)
        update = goal_update_from_outcome(outcome)
        final = await self._finish_plan_goal(goal, outcome, update)
        rt.plan_mode = False
        rt.state = "idle" if rt.state != "error" else rt.state
        rt.current_tool = None
        rt.goal = final if not final.terminal else None
        await self._emit(robot_id, "message", outcome.summary, goal=final)
        return True, final

    async def _finish_plan_goal(self, goal: Goal, outcome: Any, update: Any) -> Goal:
        fleet = self._services.fleet
        if fleet is None:  # tests / standalone: apply the runner's item state + the terminal update locally
            local = goal.model_copy(update={"items": [i.model_copy(deep=True) for i in outcome.plan.items]})
            return apply_update_locally(local, update)
        try:
            return await fleet.update_goal(goal.goal_id, update)
        except (KeyError, ValueError) as e:  # e.g. the goal was cancelled meanwhile: report the fleet's view
            log.warning("finishing %s failed: %s", goal.goal_id, e)
            with contextlib.suppress(Exception):
                return await fleet.get_goal(goal.goal_id)
            return goal

    async def _fresh_goal(self, goal: Goal) -> Goal:
        fleet = self._services.fleet
        if fleet is not None:
            with contextlib.suppress(Exception):
                return await fleet.get_goal(goal.goal_id)
        return goal

    async def _withdraw_goal(self, goal_id: str, reason: str) -> None:
        approvals = self._services.approvals
        if approvals is None:
            return
        with contextlib.suppress(Exception):
            for a in approvals.pending(goal_id=goal_id):
                with contextlib.suppress(Exception):
                    await approvals.cancel(a.approval_id, reason)

    async def _plan_status_text(self, goal: Goal) -> str | None:
        runner = self._services.runner
        if runner is None:
            return None
        try:
            plan = await runner.current_plan(goal.goal_id)
            if plan is None:
                return f"Planning '{goal.title}'."
            state = await runner.run_state(plan.plan_id)
        except Exception:  # noqa: BLE001 - status text is best effort
            return None
        done = sum(1 for s in plan.steps if s.status in CLOSED_STEP_STATUSES)
        current = next((s for s in plan.steps if s.step_id == state.current_step_id), None)
        items = [i for i in plan.items if not i.optional or i.status == "in_basket"]
        got = sum(1 for i in items if i.status == "in_basket")
        line = (
            f"Working on '{goal.title}' (verified plan): step {done}/{len(plan.steps)}, "
            f"{got}/{len(items)} items in the basket"
        )
        return f"{line}. Now: {current.title}." if current is not None else f"{line}."

    async def _publish_error(self, goal: Goal, robot_id: RobotId, message: str) -> None:
        events = self._services.events
        if events is None:
            return
        with contextlib.suppress(Exception):
            env = goal_envelope(goal, robot_id)
            await events.publish(
                make_event("error", **env, source="agent", arm="agent", message=message, code="internal")
            )

    async def _withdraw(self, ex: BrainExecutor, reason: str) -> None:
        approvals = self._services.approvals
        if approvals is None:
            return
        ids = set(ex.pending_approval_ids)
        with contextlib.suppress(Exception):
            ids |= {a.approval_id for a in approvals.pending(goal_id=ex.goal.goal_id)}
        for aid in sorted(ids):
            with contextlib.suppress(Exception):
                await approvals.cancel(aid, reason)

    async def status(self, robot_id: RobotId) -> BrainStatus:
        rt = self._runtime(robot_id)
        return BrainStatus(
            robot_id=robot_id,
            state=rt.state,
            goal_id=rt.goal.goal_id if rt.goal else None,
            mode="llm" if rt.mode == "llm" else "scripted",
            model=rt.model,
            iterations=rt.iterations,
            current_tool=rt.current_tool,
            last_step=rt.last_step,
            pending_messages=len(rt.inbox),
            updated_at=rt.updated_at,
        )

    async def recent_steps(
        self,
        robot_id: RobotId | None = None,
        goal_id: str | None = None,
        limit: int = 50,
        before: Timestamp | None = None,
    ) -> list[BrainStep]:
        rings = [self._rt[robot_id].steps] if robot_id is not None and robot_id in self._rt else []
        if robot_id is None:
            rings = [rt.steps for rt in self._rt.values()]
        out = [
            s
            for ring in rings
            for s in ring
            if (goal_id is None or s.goal_id == goal_id) and (before is None or s.ts < before)
        ]
        out.sort(key=lambda s: (s.ts, s.step_no), reverse=True)
        return out[: max(0, limit)]

    # -- talking to a robot ----------------------------------------------------------------------------------------
    async def handle_message(self, robot_id: RobotId, text: str, user_id: UserId | None = None) -> BrainReply:
        rt = self._runtime(robot_id)
        goal = rt.executor.goal if rt.executor is not None and rt.goal is not None else None
        if goal is None and rt.plan_mode:
            goal = rt.goal
        start = rt.step_no
        store_id = goal.store_id if goal else await self._store_of(robot_id)
        await self._emit(robot_id, "message", text, goal=goal, role="user", store_id=store_id)
        reply = await self._reply(rt, goal, text.strip(), store_id)
        await self._emit(robot_id, "message", reply, goal=goal, store_id=store_id)
        steps = [s for s in rt.steps if s.step_no > start and not _is_mirror(s)]
        return BrainReply(robot_id=robot_id, goal_id=goal.goal_id if goal else None, text=reply, steps=steps)

    async def _store_of(self, robot_id: RobotId) -> str | None:
        robot = self._services.robot
        if robot is not None:
            with contextlib.suppress(Exception):
                return (await robot.state(robot_id)).store_id
        with contextlib.suppress(Exception):
            from utils.data import load_robot

            return load_robot(robot_id).store_id
        return None

    async def _reply(self, rt: BrainRuntime, goal: Goal | None, text: str, store_id: str | None) -> str:
        ex = rt.executor if goal is not None else None
        add = _ADD_RE.match(text)
        skip = _SKIP_RE.match(text)
        if rt.plan_mode and goal is not None and (add or skip):
            return (
                f"I'm in the middle of a verified step plan for '{goal.title}', so I can't change the list right now. "
                "Change it in the shopper app and I'll pick it up on the next goal."
            )
        if ex is not None and goal is not None and goal.kind in ("shop", "fetch") and (add or skip):
            if self._queue_for_llm(rt, text):
                return "Got it, I'll take care of that."
            if add:
                qty = int(add.group(1)) if add.group(1) else 1
                res = await ex.call("add_item", {"query": add.group(2).strip(), "qty": max(1, min(qty, 20))})
                if isinstance(res, BrainToolError):
                    return f"Sorry, I couldn't find {add.group(2).strip()} here."
                return f"Added {res.qty}x {res.name or res.query}; I'll grab it on the way."
            assert skip is not None
            target = _match_item(goal, skip.group(1))
            if target is None:
                return f"I don't have {skip.group(1).strip()} on my list."
            if target.status not in ACTIVE_ITEM_STATUSES:
                return f"{target.name or target.query} is already {target.status.replace('_', ' ')}."
            await ex.call(
                "update_progress",
                {
                    "note": f"Skipping {target.name or target.query} (asked by the user)",
                    "item_id": target.item_id,
                    "item_status": "skipped",
                },
            )
            return f"OK, skipping {target.name or target.query}."
        if goal is not None and self._queue_for_llm(rt, text):
            return "Got it."
        where = await self._where(text, store_id)
        if where:
            return where
        if _SEE_RE.search(text):
            wants_status = bool(_STATUS_RE.search(text))
            seen = await self._look(rt, goal, _see_clause(text) if wants_status else text, store_id)
            if seen:
                return f"{await self._status_text(rt, goal)} {seen}" if wants_status else seen
        return await self._status_text(rt, goal)

    def _queue_for_llm(self, rt: BrainRuntime, text: str) -> bool:
        return False

    async def _where(self, text: str, store_id: str | None) -> str | None:
        """In-store help from the live catalog (deterministic, so it works with or without an LLM key):
        'where is the salsa?', 'do you have oat milk' -> aisle, section, shelf, price and stock;
        'what's a cheaper alternative to the salsa?' -> cheaper in-stock products of the same category here."""
        catalog = self._services.catalog
        if catalog is None or not store_id:
            return None
        cheaper = _CHEAPER_RE.search(text)
        m = cheaper or _WHERE_RE.search(text)
        if m is None:
            return None
        from contracts.catalog import SearchQuery

        query = m.group("q").strip()
        try:
            hits = await catalog.search(SearchQuery(text=query, store_ids=[store_id], limit=3))
        except Exception:  # noqa: BLE001 - the shopper still gets an answer
            return None
        if not hits:
            return f"Sorry, I couldn't find {query} in this store."
        hit = hits[0]
        p = hit.product
        offer = hit.offer_for(store_id)
        words = [w for w in re.findall(r"[a-z]+", query.lower()) if len(w) > 2]
        exact = all(w.rstrip("s") in p.name.lower() for w in words)
        lead = "" if exact else f"I don't see {query} exactly; the closest is "
        if cheaper:
            return await self._cheaper(p, offer, store_id, lead)
        locs = await catalog.get_locations(p.sku, store_id)
        if not locs:
            return f"We don't carry {p.name} in this store."
        loc = locs[0]
        where, shelf = await self._shelf_words(loc, store_id)
        price = f" It's ${offer.price_cents / 100:.2f}" if offer is not None else ""
        stock = ""
        if offer is not None:
            stock = f", {offer.qty} on the shelf" if offer.qty > 0 else ", but it's out of stock right now"
        return f"{lead}{p.name} is in {where}, {shelf}.{price}{stock}. Want me to take you there?"

    async def _shelf_words(self, loc: Any, store_id: str) -> tuple[str, str]:
        fx = None
        with contextlib.suppress(Exception):
            plan = await self._services.catalog.get_planogram(store_id)
            fx = next((f for f in plan.fixtures if f.fixture_id == loc.fixture_id), None)
        where = loc.aisle_label or (fx.label if fx is not None and fx.label else None) or "the store"
        if fx is not None and fx.kind == "produce_bin":
            return where, f"on {fx.label or 'the produce table'}"
        levels = {0: "bottom shelf", 1: "second shelf", 2: "third shelf", 3: "top shelf"}
        return where, levels.get(loc.level, f"shelf {loc.level + 1}")

    async def _cheaper(self, p: Any, offer: Any, store_id: str, lead: str) -> str:
        from contracts.catalog import SearchQuery

        catalog = self._services.catalog
        base = offer.price_cents if offer is not None else p.price_cents
        hits = await catalog.search(
            SearchQuery(category=p.category, store_ids=[store_id], in_stock_only=True, max_price_cents=base, limit=20)
        )
        alts = []
        for h in hits:
            o = h.offer_for(store_id)
            if h.product.sku == p.sku or o is None or o.price_cents >= base:
                continue
            if p.subcategory and h.product.subcategory and h.product.subcategory != p.subcategory:
                continue  # same kind of thing (salsa for salsa, not salsa for beans)
            alts.append((o.price_cents, h.product, o))
        alts.sort(key=lambda a: a[0])
        if not alts:
            return f"{lead}{p.name} (${base / 100:.2f}) is already the cheapest of its kind here."
        cents, alt, _o = alts[0]
        locs = await catalog.get_locations(alt.sku, store_id)
        where = ""
        if locs:
            aisle, shelf = await self._shelf_words(locs[0], store_id)
            where = f" It's in {aisle}, {shelf}."
        more = (
            f" I also see {len(alts) - 1} other cheaper option{'s' if len(alts) > 2 else ''}." if len(alts) > 1 else ""
        )
        return (
            f"{lead}{p.name} is ${base / 100:.2f}. {alt.name} is ${cents / 100:.2f}, "
            f"saving ${(base - cents) / 100:.2f}.{where}{more} Want me to grab it or take you there?"
        )

    async def _look(self, rt: BrainRuntime, goal: Goal | None, question: str, store_id: str | None) -> str | None:
        cameras = self._services.cameras
        if cameras is None:
            return None
        try:
            obs = await cameras.observe(f"{rt.robot_id}.front", question, goal_id=goal.goal_id if goal else None)
        except Exception as e:
            log.warning("observe failed: %s", e)
            return None
        await self._emit(
            rt.robot_id,
            "observation",
            obs.answer,
            goal=goal,
            observation=obs,
            camera_id=obs.camera_id,
            store_id=store_id,
        )
        return obs.answer

    async def _status_text(self, rt: BrainRuntime, goal: Goal | None) -> str:
        if goal is not None and rt.plan_mode:
            text = await self._plan_status_text(goal)
            if text:
                return text
        if goal is not None:
            items = goal.items
            done = sum(1 for i in items if i.status not in ACTIVE_ITEM_STATUSES)
            line = f"Working on '{goal.title}'"
            if items:
                line += f": {done}/{len(items)} items handled"
            elif goal.kind == "shelf_scan" and goal.slot_ids:
                line += f": scanned {len(goal.scanned_slot_ids)}/{len(goal.slot_ids)} slots"
            note = goal.progress_note or (rt.last_step.text if rt.last_step else "")
            if goal.status == GoalStatus.WAITING:
                note = f"waiting for an answer: {goal.progress_note}"
            return f"{line}. {note}".strip()
        robot = self._services.robot
        if robot is not None:
            with contextlib.suppress(Exception):
                st = await robot.state(rt.robot_id)
                batt = f", battery {st.battery_pct:.0f}%" if st.battery_pct is not None else ""
                at = f"{st.status.value} at ({st.pose.x:.1f}, {st.pose.y:.1f})"
                where = "charging on my dock" if st.charging else at
                return f"I'm free: {where}{batt}. Send me a goal!"
        return "I'm free right now. Send me a goal!"


def _is_mirror(step: BrainStep) -> bool:
    """A plan-mode step mirrored by the loop runner (``bridge.mirror_brain_step``), not one this brain emitted."""
    return step.kind == "status" and step.model == MIRROR_MODEL


def _match_item(goal: Goal, text: str) -> Any:
    words = {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) > 2}
    words |= {w[:-1] for w in words if w.endswith("s")}
    best, score = None, 0
    for item in goal.items:
        hay = " ".join(filter(None, [item.query, item.name, item.sku])).lower()
        toks = set(re.findall(r"[a-z]+", hay))
        toks |= {w[:-1] for w in toks if w.endswith("s")}
        s = len(words & toks)
        if s > score or (s == score and s and best is not None and best.status not in ACTIVE_ITEM_STATUSES):
            best, score = item, s
    return best if score else None


class ScriptedBrain(_BrainBase):
    """Deterministic fake brain (no LLM): ScriptedRunner over the same tools."""


class ClaudeBrain(_BrainBase):
    """Claude tool loop per robot; falls back to the scripted runner on any LLM failure."""

    def __init__(self, services: Services, client: Any = None) -> None:
        super().__init__(services)
        self._client = client

    @property
    def model_name(self) -> str:  # type: ignore[override]
        from utils import llm

        return llm.model("brain")

    def _llm_ready(self) -> bool:
        from utils import llm

        return self._client is not None or llm.is_configured()

    def _queue_for_llm(self, rt: BrainRuntime, text: str) -> bool:
        if rt.mode == "llm" and rt.goal is not None:
            rt.inbox.append(text)
            return True
        return False

    async def _run(self, rt: BrainRuntime, ex: BrainExecutor) -> None:
        if not self._llm_ready():
            await ScriptedRunner(ex).run()
            return
        rt.mode, rt.model = "llm", self.model_name
        ex.model = rt.model
        try:
            await self._loop(rt, ex)
        except (asyncio.CancelledError, RobotError):
            raise
        except Exception as e:  # LLM / API / output failures: finish the goal deterministically
            log.warning("brain LLM failed for %s (%s); scripted fallback", rt.robot_id, e)
            rt.mode = SCRIPTED
            rt.model = SCRIPTED
            ex.model = SCRIPTED
            await ex.emit("error", f"LLM unavailable ({type(e).__name__}: {e}); continuing scripted.", ok=False)
            await ex.error(f"robot brain LLM failed, scripted fallback: {e}", code="llm_fallback")
            if not ex.finished:
                await ScriptedRunner(ex).run()

    async def _loop(self, rt: BrainRuntime, ex: BrainExecutor) -> None:
        from utils import llm

        client = self._client or llm.get_client()
        tools = llm.cached_tools(brain_tool_definitions(strict=True))
        system = llm.cached_system(BRAIN_SYSTEM)
        messages: list[dict[str, Any]] = [{"role": "user", "content": await self._brief(ex)}]
        nudged = False
        for i in range(1, MAX_ITERS + 1):
            rt.iterations = i
            rt.state = "thinking"
            response = await client.messages.create(
                model=rt.model, max_tokens=2048, system=system, tools=tools, messages=messages
            )
            content = [llm.block_to_param(b) for b in response.content]
            messages.append({"role": "assistant", "content": content})
            text = llm.response_text(response).strip()
            if text:
                await ex.thought(text)
            uses = llm.tool_uses(response)
            if not uses:
                if ex.finished:
                    return
                if response.stop_reason in ("refusal", "max_tokens") or nudged:
                    raise RuntimeError(f"model stopped without finishing (stop_reason={response.stop_reason})")
                nudged = True
                messages.append({"role": "user", "content": "Keep going with the tools; call finish_goal when done."})
                continue
            results: list[dict[str, Any]] = []
            for use in uses:
                res = await ex.call(use.name, dict(use.input or {}), tool_use_id=use.id)
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": use.id,
                    "content": llm.serialize_tool_result(_compact(res)),
                }
                if isinstance(res, BrainToolError):
                    block["is_error"] = True
                results.append(block)
                if ex.finished:
                    return
            extra: list[dict[str, Any]] = []
            for msg in rt.inbox:
                extra.append({"type": "text", "text": f"Message from the user: {msg}"})
            rt.inbox.clear()
            if i % CHECKPOINT_EVERY == 0:
                extra.append({"type": "text", "text": await self._checkpoint(ex, i)})
            messages.append({"role": "user", "content": results + extra})
        raise RuntimeError(f"hit the {MAX_ITERS}-turn limit")

    async def _brief(self, ex: BrainExecutor) -> str:
        g = ex.goal
        items = [
            {
                k: v
                for k, v in {
                    "item_id": i.item_id,
                    "query": i.query,
                    "name": i.name,
                    "sku": i.sku,
                    "qty": i.qty,
                    "slot_id": i.slot_id,
                    "stop_id": i.stop_id,
                    "category": i.category,
                    "optional": i.optional or None,
                    "status": i.status,
                    "picked_qty": i.picked_qty or None,
                }.items()
                if v is not None
            }
            for i in g.items
        ]
        brief: dict[str, Any] = {
            "robot": {"robot_id": ex.robot_id, "name": ex.robot_name, "store_id": ex.store_id},
            "goal": {
                "goal_id": g.goal_id,
                "kind": g.kind,
                "title": g.title,
                "instructions": g.instructions,
                "end_at": g.end_at,
                "priority": g.priority,
                "has_trip": g.trip_id is not None,
            },
        }
        if items:
            brief["items"] = items
        if g.kind == "shelf_scan":
            done = set(g.scanned_slot_ids)
            brief["slots_to_scan"] = [s for s in g.slot_ids if s not in done]
        pose = await ex.pose()
        if pose is not None:
            brief["robot"]["pose"] = {"x": round(pose.x, 2), "y": round(pose.y, 2)}
        return "Your goal:\n" + json.dumps(brief, indent=1)

    async def _checkpoint(self, ex: BrainExecutor, i: int) -> str:
        g = ex.goal
        left = [i.item_id for i in g.items if i.status in ACTIVE_ITEM_STATUSES]
        text = f"Checkpoint: turn {i} of {MAX_ITERS}. Goal progress {g.progress:.0%}."
        if g.items:
            text += f" Items still open: {', '.join(left) or 'none'}."
        if g.kind == "shelf_scan":
            text += f" Scanned {len(g.scanned_slot_ids)}/{len(g.slot_ids)} slots."
        cameras = self._services.cameras
        if cameras is not None:
            with contextlib.suppress(Exception):
                obs = await cameras.observe(f"{ex.robot_id}.front", None, goal_id=g.goal_id)
                text += f" Front camera now: {obs.answer}"
        if i >= MAX_ITERS - CHECKPOINT_EVERY:
            text += " You are close to the turn limit: wrap up and call finish_goal soon."
        return text


def _compact(result: Any) -> Any:
    """Tool result for the model: drop bulky fields (thumbnails never go to the model; frames are never inlined)."""
    if hasattr(result, "model_dump"):
        data = result.model_dump(mode="json", exclude_none=True)
        if isinstance(data, dict):
            data.pop("assignments", None)
            dec = data.get("decision")
            if isinstance(dec, dict):
                data["decision"] = {k: dec[k] for k in ("verdict", "reasons", "messages") if k in dec}
        return data
    return result
