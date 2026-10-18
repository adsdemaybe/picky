"""LlmPlanner: the real ``PlannerService`` (``create_planner``). The LLM proposes a ``PlanDraft``; code decides.

It subclasses ``planner_fake.ScriptedPlanner`` and overrides ONLY ``draft()``:

1. the prompt is the RESOLVED goal (items with ids, qty, picked_qty, optional, category, slot, aisle), budget, end_at,
   instructions and the robot pose; the system prompt is frozen (cache-friendly).
2. ``utils.llm.complete_json(prompt, PlanDraft, kind="brain")`` (provider-agnostic: Gemini by default,
   ``PICKY_LLM_PROVIDER=anthropic`` for Claude) inside ``asyncio.wait_for(PICKY_PLAN_TIMEOUT_S, default 20 s)``.
3. ANY failure (no key, timeout, API error, refusal, malformed / invalid JSON, wrong SDK signature) -> the scripted
   draft with ``PlannerInfo(origin="scripted", fallback_reason=...)``, an ``error`` event (code ``llm_fallback``) and a
   log line. Never raises for LLM problems.

``compile`` (inherited, deterministic) then runs ``plan.normalize_draft`` (drops duplicates / unknown items, adds
forgotten picks, keeps the trip route order, exactly one checkout last) and the macros; if an LLM draft still fails
to compile, ``ScriptedPlanner.plan`` recompiles the scripted draft. ``replan`` stays scripted (it runs mid-trip and
must be fast); ``expand_tool_call`` is deterministic.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from arms.agent.planner_fake import ScriptedPlanner, parse_budget_cents
from contracts.events import make_event
from contracts.fleet import GoalItem
from contracts.plan import OPEN_ITEM_STATUSES, PlanDraft, PlannerInfo, PlanRequest
from utils.log import get_logger

log = get_logger(__name__)

CompleteJson = Callable[..., Awaitable[PlanDraft]]

PLANNER_SYSTEM = """You plan the work of ONE Picky store robot: a small rover with a vertical lift, an SO-101 arm \
and a basket, inside one grocery store. You get the goal's items (already found in the store, with their shelf \
slots) and return a PlanDraft: WHAT to do, in which order. Code turns your draft into motions and checks every step \
with cameras, so keep it short and literal.

Rules:
- Exactly one fetch_item per item that still needs picking (status pending / located / picked AND it has a slot_id). \
fetch_item picks every remaining unit of that item; never repeat it.
- Copy item_id EXACTLY from the list. Never invent items, SKUs, slots or prices.
- Optional items (recommendations) need an ask_user with that item_id BEFORE their fetch_item: one short yes/no \
question that says why the shopper might want it.
- Items with status online / unavailable / in_basket / skipped / declined get no fetch_item.
- If the goal has route stops (stop_id), keep the given item order: the shopper sees that route on the map.
- When end_at is "checkout", the last step is checkout (basket audit + drive to checkout + hand off). When end_at is \
"dock" or "none", there is no checkout step.
- Use scan_shelf (with a slot_id from the list) only when the instructions ask you to check a shelf.
- summary: a plan title of at most 60 characters. rationale: one or two sentences on the order you chose.
- Money is integer cents."""


PLANNER_EFFORT = "low"
"""utils.llm effort for the draft (Gemini thinking_level / Anthropic effort): drafts are small; keep them fast."""


def _key_name() -> str:
    try:
        from utils import llm

        provider = str(llm.provider())
    except Exception:  # noqa: BLE001 - older utils.llm without provider(): read the setting
        from arms.agent.loop import setting

        provider = str(setting("picky_llm_provider", "gemini")).lower()
    return "ANTHROPIC_API_KEY" if provider == "anthropic" else "GEMINI_API_KEY"


def _accepts(fn: Any, name: str) -> bool:
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class LlmPlanner(ScriptedPlanner):
    """Real planner: an LLM ``draft()`` (utils.llm, Gemini by default) over the deterministic ScriptedPlanner."""

    def __init__(
        self,
        services: Any,
        *,
        complete_json: CompleteJson | None = None,
        timeout_s: float | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(services)
        self._complete_json = complete_json
        """Injected structured-output call (tests); default ``utils.llm.complete_json`` looked up at call time."""
        self._timeout_s = timeout_s
        self._model = model

    # ---- config ----------------------------------------------------------------------------------------------------
    def timeout_s(self) -> float:
        if self._timeout_s is not None:
            return self._timeout_s
        from arms.agent.loop import plan_timeout_from_settings

        return plan_timeout_from_settings()

    def model_name(self) -> str | None:
        if self._model is not None:
            return self._model
        try:
            from utils import llm

            return llm.model("brain")
        except Exception:  # noqa: BLE001 - a model id is display-only
            return None

    def llm_ready(self) -> bool:
        if self._complete_json is not None:
            return True
        try:
            from utils import llm

            return bool(llm.is_configured())
        except Exception:  # noqa: BLE001 - an unimportable provider SDK = not configured
            return False

    # ---- the seam --------------------------------------------------------------------------------------------------
    async def draft(
        self, request: PlanRequest, items: Sequence[GoalItem], summary: str
    ) -> tuple[PlanDraft, PlannerInfo]:
        end_at = request.end_at or (request.goal.end_at if request.goal else "checkout")
        scripted = self.scripted_draft(items, summary, end_at=end_at)
        if not any(_needs_steps(i) for i in items):  # nothing to decide: no LLM round trip
            return scripted, PlannerInfo(origin="scripted", mode="scripted")
        if not self.llm_ready():
            return scripted, PlannerInfo(origin="scripted", mode="scripted", fallback_reason=f"no {_key_name()}")

        model = self.model_name()
        timeout = self.timeout_s()
        t0 = time.monotonic()
        try:
            prompt = await self.prompt(request, items, summary, end_at)
            complete = self._complete_json
            if complete is None:
                from utils import llm

                complete = llm.complete_json
            kwargs: dict[str, Any] = {"system": PLANNER_SYSTEM, "kind": "brain", "max_tokens": 4000, "retries": 1}
            if _accepts(complete, "effort"):
                kwargs["effort"] = PLANNER_EFFORT
            draft = await asyncio.wait_for(complete(prompt, PlanDraft, **kwargs), timeout=timeout)
            if not isinstance(draft, PlanDraft):
                draft = PlanDraft.model_validate(draft)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - every LLM problem degrades to the scripted draft
            reason = _fallback_reason(exc, timeout)
            latency = round((time.monotonic() - t0) * 1000, 1)
            log.warning("planner LLM failed (%s); scripted draft", reason)
            await self._fallback_event(request, reason)
            return scripted, PlannerInfo(
                origin="scripted", mode="scripted", model=model, latency_ms=latency, fallback_reason=reason
            )
        latency = round((time.monotonic() - t0) * 1000, 1)
        return draft, PlannerInfo(origin="llm", mode="llm", model=_served(model), latency_ms=latency)

    async def prompt(self, request: PlanRequest, items: Sequence[GoalItem], summary: str, end_at: str) -> str:
        """The user turn: JSON of the resolved goal (the system prompt stays frozen)."""
        store_id = request.store_id or ""
        rows: list[dict[str, Any]] = []
        for it in items:
            aisle = None
            if it.slot_id:
                try:
                    loc = await self._location(store_id, it.slot_id)
                    aisle = loc.aisle_label if loc else None
                except Exception:  # noqa: BLE001 - the aisle label is a hint only
                    aisle = None
            row = {
                "item_id": it.item_id,
                "name": it.name or it.query,
                "qty": it.qty,
                "picked_qty": it.picked_qty or None,
                "optional": it.optional or None,
                "category": it.category,
                "slot_id": it.slot_id,
                "aisle": aisle,
                "stop_id": it.stop_id,
                "status": it.status,
                "price_cents": it.price_cents,
            }
            rows.append({k: v for k, v in row.items() if v is not None})
        goal = request.goal
        budget = request.budget_cents
        if budget is None and goal is None and request.text:
            budget = parse_budget_cents(request.text)
        brief: dict[str, Any] = {
            "goal": {
                "title": summary,
                "kind": goal.kind if goal else "shop",
                "end_at": end_at,
                "budget_cents": budget,
                "instructions": request.instructions or (goal.instructions if goal else None) or None,
            },
            "items": rows,
        }
        try:
            pose = await self._start_pose(request.robot_id, store_id)
            brief["robot"] = {"robot_id": request.robot_id, "pose": {"x": round(pose.x, 2), "y": round(pose.y, 2)}}
        except Exception:  # noqa: BLE001 - pose is context only
            brief["robot"] = {"robot_id": request.robot_id}
        brief["goal"] = {k: v for k, v in brief["goal"].items() if v is not None}
        return "Plan this goal. Reply with a PlanDraft.\n" + json.dumps(brief, indent=1)

    async def _fallback_event(self, request: PlanRequest, reason: str) -> None:
        events = getattr(self.s, "events", None)
        if events is None:
            return
        goal = request.goal
        try:
            await events.publish(
                make_event(
                    "error",
                    trip_id=goal.trip_id if goal else None,
                    source="agent",
                    store_id=request.store_id,
                    robot_id=request.robot_id,
                    user_id=request.user_id or (goal.user_id if goal else None),
                    arm="agent",
                    message=f"planner LLM unavailable, scripted plan instead: {reason}",
                    code="llm_fallback",
                    recoverable=True,
                )
            )
        except Exception as exc:  # noqa: BLE001 - events are best effort
            log.warning("publish planner fallback error failed: %s", exc)


def _served(model: str | None) -> str | None:
    """The model that really answered (utils.llm falls back to a sibling Gemini model on 429 / 503)."""
    try:
        from utils import llm

        return llm.served_model(model)
    except Exception:  # noqa: BLE001 - display only
        return model


def _needs_steps(it: GoalItem) -> bool:
    return it.status in OPEN_ITEM_STATUSES and bool(it.slot_id) and bool(it.sku) and it.picked_qty < it.qty


def _fallback_reason(exc: BaseException, timeout_s: float) -> str:
    name = type(exc).__name__
    if isinstance(exc, TimeoutError):
        return f"LLM timeout after {timeout_s:g} s"
    if name == "LLMNotConfigured":
        return f"no {_key_name()}"
    if name in ("LLMOutputError", "ValidationError", "ValueError"):
        return f"invalid draft: {exc}"[:300]
    return f"{name}: {exc}"[:300]


__all__ = ["PLANNER_EFFORT", "PLANNER_SYSTEM", "LlmPlanner"]
