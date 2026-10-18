"""LLM tool contracts (Anthropic tool-use format) for the two LLM loops in the agent arm:

1. SHOPPER tools (``TOOL_NAMES`` / ``TOOL_DEFINITIONS``; ``AgentService.respond``): query_inventory, plan_route,
   recommend, add_to_cart, request_approval, checkout. The trip context (trip_id, user_id, store) is supplied by the
   tool executor, never by the model.
2. ROBOT BRAIN tools (``BRAIN_TOOL_NAMES`` / ``BRAIN_TOOL_DEFINITIONS``; ``RobotBrainService.run_goal``): the per-robot
   managing LLM decides what to pick, where, in what order and handles exceptions. The executor supplies the robot,
   its store and the goal (robot_id / store_id / goal_id / trip_id), never the model. See "Brain tools" below.

Side effects (CLAUDE.md §7): the executor changes the trip ONLY through ``services.trips`` (TripController) and asks
the shopper ONLY through ``services.approvals`` (ApprovalBroker):
- add_to_cart      -> services.trips.add_line(trip_id, line, approval_id=?)   (mandate-checked; may need approval)
- plan_route       -> services.trips.replan(trip_id, ...)
- request_approval -> services.approvals.request(ApprovalRequest(...))
- checkout         -> services.trips.start_checkout(trip_id, note)  (same path as POST /api/trips/{id}/checkout;
                      the mandate engine + explicit shopper approval decide, never the model)
- query_inventory  -> services.catalog.search(...);  recommend -> services.recommender.*

``TOOL_DEFINITIONS`` is non-strict (bounds/defaults kept in the schema; ``parse_tool_input`` validates).
``tool_definitions(strict=True)`` returns strict-mode definitions built with ``anthropic.transform_schema``.
Strict-mode limits (Anthropic structured outputs, combined over ALL strict schemas in ONE request): <= 20 strict
tools, <= 24 optional parameters, <= 16 union-typed parameters (``anyOf`` / type arrays, incl. ``X | None``). The
shopper set (all 6 strict) and the brain set (only ``BRAIN_STRICT_TOOLS`` strict) each fit; contracts tests enforce
it, so adding a nullable field to a strict tool needs a budget check.
"""

from typing import Any, Literal

from pydantic import Field, model_validator

from contracts.cameras import RobotCameraKind
from contracts.common import (
    ONLINE_STORE_ID,
    CategoryId,
    InstanceId,
    ItemId,
    Model,
    RecId,
    RouteId,
    Sku,
    SlotId,
    StopId,
    StoreId,
    TripId,
)
from contracts.fleet import GoalEndAt, GoalItemStatus
from contracts.routing import Route, StopKind
from contracts.shopping import LineSource, RecKind
from contracts.sim import ItemInstance
from contracts.trust import ApprovalDecision, MandateDecision

ToolName = Literal["query_inventory", "plan_route", "recommend", "add_to_cart", "request_approval", "checkout"]
TOOL_NAMES: tuple[str, ...] = (
    "query_inventory",
    "plan_route",
    "recommend",
    "add_to_cart",
    "request_approval",
    "checkout",
)

ToolApprovalKind = Literal["recommendation", "over_threshold", "substitution", "online_order", "pick_choice"]
"""trust.ApprovalKind minus 'checkout' (checkout approval is raised by the checkout flow, not by the model)."""


class QueryInventoryInput(Model):
    query: str = Field(description="What to look for, e.g. 'corn tortillas' or 'vegetarian protein'.")
    store_id: StoreId | None = Field(None, description="Limit to one store id; omit to search all stores + online.")
    category: str | None = Field(None, description="Optional category filter, e.g. 'produce'.")
    dietary: list[str] = Field(
        default_factory=list, description="Products must satisfy all of these, e.g. ['vegetarian']."
    )
    in_stock_only: bool = Field(False, description="Only return offers with stock > 0.")
    max_results: int = Field(5, ge=1, le=20)


class PlanRouteInput(Model):
    include_recommendations: bool = Field(True, description="Add recommendation detours as optional stops.")
    max_optional_stops: int | None = Field(None, ge=0, le=10, description="Cap on detours; omit for the default.")


class RecommendInput(Model):
    limit: int = Field(3, ge=1, le=10)
    kind: RecKind = "detour"
    focus: str | None = Field(None, description="Optional theme, e.g. 'spicy' or 'dessert'.")


class AddToCartInput(Model):
    sku: Sku
    qty: int = Field(1, ge=1, le=50)
    source: LineSource = "in_store"
    store_id: StoreId | None = Field(
        None, description="Store to source from; omit for the trip's store (or 'online' when source is 'online')."
    )
    rec_id: RecId | None = Field(None, description="Recommendation this add came from, if any.")
    approval_id: str | None = Field(None, description="Approval the shopper already granted for this add, if any.")
    reason: str | None = Field(None, description="Short note shown to the shopper.")

    @model_validator(mode="after")
    def _source_store(self) -> "AddToCartInput":
        if self.source == "online" and self.store_id is None:
            self.store_id = ONLINE_STORE_ID
        if (self.store_id == ONLINE_STORE_ID) != (self.source == "online"):
            raise ValueError("store_id 'online' requires source 'online' (and vice versa)")
        return self


class RequestApprovalInput(Model):
    kind: ToolApprovalKind = Field(
        description="What is being asked. Checkout approval is requested by the checkout tool, not here."
    )
    title: str = Field(description="Short question for the shopper, e.g. 'Swap to Jack cheese? $1 cheaper'.")
    body: str = ""
    sku: Sku | None = None
    rec_id: RecId | None = Field(None, description="Recommendation this asks about (kind='recommendation').")
    amount_cents: int | None = Field(None, ge=0)
    options: list[str] = Field(default_factory=list, description="Choice labels; empty = yes/no.")


class CheckoutInput(Model):
    note: str | None = Field(None, description="Optional message to show with the checkout approval.")


TOOL_INPUT_MODELS: dict[str, type[Model]] = {
    "query_inventory": QueryInventoryInput,
    "plan_route": PlanRouteInput,
    "recommend": RecommendInput,
    "add_to_cart": AddToCartInput,
    "request_approval": RequestApprovalInput,
    "checkout": CheckoutInput,
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "query_inventory": (
        "Search the catalog across the trip's store, other stores and the online store. Returns products with "
        "per-store price, stock, aisle location and active deals. Use it to resolve list items to SKUs and to "
        "decide in-store vs online sourcing."
    ),
    "plan_route": (
        "Plan (or re-plan) the robot's route through the store for the current shopping list: every in-store item "
        "is a mandatory stop and recommendations are optional detours. Returns the stop order, distance and the "
        "extra distance the detours add."
    ),
    "recommend": (
        "Get personalized recommendations for this shopper, each with a reason and signals. 'detour' = worth a stop "
        "on the route, 'checkout_batch' = ask once at checkout, 'near_expiry_offer' = discounted near-expiry items."
    ),
    "add_to_cart": (
        "Add a SKU to the trip cart (in-store pick or online order). The mandate engine checks it; if it is above "
        "the shopper's ask threshold the result says an approval is needed instead of adding it: ask with "
        "request_approval, then call add_to_cart again with that approval_id."
    ),
    "request_approval": (
        "Ask the shopper a yes/no or multiple-choice question on their active channel (web, voice or iMessage) and "
        "wait for the answer. Use for recommendation detours, substitutions, online orders and over-threshold items."
    ),
    "checkout": (
        "Start checkout for the current cart. Runs the deterministic mandate check and asks the shopper for explicit "
        "approval of the exact cart; payment only happens after they approve. You cannot pay without approval."
    ),
}


def _strip_titles(schema: Any) -> Any:
    """Drop pydantic's schema-level 'title' keys (but keep properties that happen to be named 'title')."""
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            out[key] = {name: _strip_titles(sub) for name, sub in value.items()}
        else:
            out[key] = _strip_titles(value)
    return out


def _model_schema(model: type[Model], strict: bool) -> dict[str, Any]:
    if strict:
        from anthropic import transform_schema  # base dep; imported lazily to keep contracts import light

        schema = transform_schema(model)
    else:
        schema = model.model_json_schema()
    schema = _strip_titles(schema)
    schema["type"] = "object"
    return schema


def tool_input_schema(name: str, strict: bool = False) -> dict[str, Any]:
    """JSON Schema (object) for a shopper tool's input, without pydantic 'title' noise.

    ``strict=True`` runs ``anthropic.transform_schema`` (moves min/max/default into descriptions, closes objects),
    as required by strict tool use. Validation of bounds stays in ``parse_tool_input``.
    """
    return _model_schema(TOOL_INPUT_MODELS[name], strict)


def tool_definition(name: str, strict: bool = False) -> dict[str, Any]:
    """One tool in Anthropic format: {name, description, input_schema} (+ ``strict: True`` when strict)."""
    tool: dict[str, Any] = {
        "name": name,
        "description": TOOL_DESCRIPTIONS[name],
        "input_schema": tool_input_schema(name, strict=strict),
    }
    if strict:
        tool["strict"] = True
    return tool


def tool_definitions(strict: bool = False) -> list[dict[str, Any]]:
    """All six tools, in TOOL_NAMES order."""
    return [tool_definition(n, strict=strict) for n in TOOL_NAMES]


TOOL_DEFINITIONS: list[dict[str, Any]] = tool_definitions()
"""Non-strict definitions: pass directly as ``tools=`` to ``anthropic.AsyncAnthropic().messages.create``."""


def parse_tool_input(name: str, data: dict[str, Any]) -> Model:
    """Validate a model-supplied tool input dict into its Pydantic model (raises ValidationError / KeyError)."""
    return TOOL_INPUT_MODELS[name].model_validate(data)


class ToolCallRecord(Model):
    """One executed tool call (for logs, transcripts and AgentReply)."""

    tool_use_id: str | None = None
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    ok: bool = True
    error: str | None = None
    duration_ms: float = 0.0


class AgentReply(Model):
    """Result of AgentService.respond. Side effects already happened through services.trips / services.approvals."""

    trip_id: TripId | None = None
    text: str
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    stop_reason: str | None = None


# ====================================================================================================================
# Brain tools (per-robot managing LLM; RobotBrainService)
# ====================================================================================================================
# Executor mapping (robot_id / store_id / goal / trip come from the executor, never the model; every call also emits
# brain.step tool_call + tool_result events; every event about a shop goal carries envelope trip_id = goal.trip_id,
# store_id, robot_id, user_id, request_id: ``contracts.events.goal_envelope(goal)``):
# - locate_items    -> services.catalog.get_locations / search(store_ids=[store])         -> LocateItemsResult
# - plan_route      -> shop: services.trips.replan(trip_id) (slot_ids / end_at are IGNORED: the trip route decides)
#                      else services.routing.plan_route over slot_ids (shelf_scan: empty = slot_ids minus
#                      scanned_slot_ids; fetch: the item's slot) ending at end_at          -> RouteSummary
# - navigate_to     -> path from routing (route leg / plan_route to one stop) -> services.robot.navigate(robot_id, ...)
#                      shop: services.trips.set_stop_status(navigating -> arrived) + robot.arrived  -> sim.RobotState
# - look            -> services.cameras.observe('<robot_id>.<camera>', question)            -> cameras.VisionObservation
# - scan_shelf      -> robot.scan -> perception.blur_faces -> perception.identify -> catalog.apply_scan (stock +
#                      alerts) + scan.result; shelf_scan goals: update_goal(scanned_slot_ids=[slot]) -> ScanShelfResult
# - pick_item       -> shop: BEFORE the first unit of a GoalItem, services.trips.check_line on the FULL line
#                      (qty = item.qty x unit price): deny -> needs nothing more (item 'failed'); needs_approval ->
#                      result needs_approval=True (model: ask_user kind over_threshold item_id=..., then pick_item
#                      again with that approval_id); approve / approved -> services.robot.pick(robot_id, slot, sku,
#                      instance_id)                                                       -> PickResult
# - place_in_basket -> robot.place_in_basket -> catalog.record_pick(lot_id=instance.lot_id, robot_id) -> item.picked
#                      (per unit). Shop goals: units ACCUMULATE on the GoalItem; services.trips.add_line(trip, line
#                      with qty = picked units, approval_id) is called ONCE per GoalItem, when picked_qty == qty or
#                      when update_progress closes the item; verify -> perception.verify_basket(expected = the units
#                      THIS robot placed, i.e. GoalItem.instance_ids of items with robot_id == this robot, never the
#                      whole trip cart) + basket.verified                                  -> PlaceResult
# - ask_user        -> goals WITH a trip: services.trips.ask(trip_id, ApprovalRequest) (gateway fills channels +
#                      expiry from its config, drives EXECUTING <-> AWAITING_APPROVAL, records the transparency
#                      outcome for kind=recommendation); goals WITHOUT a trip: services.approvals.request (user_id =
#                      goal.user_id or STAFF_USER_ID). For line-level kinds (over_threshold, recommendation,
#                      substitution, online_order) the executor fills sku, rec_id and amount_cents (= item.qty x
#                      unit price) from the GoalItem named by item_id and ignores model-sent values; payload keys:
#                      over_threshold {"reasons": [MandateReason...]} from check_line; recommendation {"interest",
#                      "sponsored", "over_budget", "over_budget_cents"}. pick_choice runs perception.rank_freshness on
#                      the last scan frame + publishes pick.choices; options in freshness rank order -> AskUserResult
# - add_item        -> catalog.get_locations / search in the store -> update_goal(add_items=[GoalItem]); shop goals
#                      also trips.update_list (gateway appends a list item + pending stop) then trips.replan
#                                                                                         -> fleet.GoalItem
# - update_progress -> services.fleet.update_goal(goal_id, GoalUpdate); shop goals mirror item_status onto the route
#                      stop with trips.set_stop_status: in_basket -> done; declined -> declined (+ trips.replan);
#                      unavailable -> failed + online line via trips.add_line(source='online') (item -> 'online');
#                      skipped -> skipped; failed -> failed (no online reorder)            -> fleet.Goal
# - finish_goal     -> end_at='checkout' (shop / fetch): navigate to checkout, then services.robot.unload_basket
#                      (the basket is emptied at checkout), then services.fleet.update_goal(status=completed|failed);
#                      ends run_goal                                                       -> fleet.Goal
# Exceptions: RobotError codes come back as tool results ``BrainToolError`` (ok=False, error_code, retryable) so the
# model can re-plan. 'disconnected' is RETRYABLE (Isaac loss: the robot continues on the 2D fake from its last state;
# re-plan from the current pose). run_goal RAISES only for BRAIN_FATAL_CODES (battery_low, unknown_robot) or a robot
# left in RobotStatus.ERROR, and never calls update_goal(failed) before raising (the fleet reassigns).

BrainToolName = Literal[
    "locate_items",
    "plan_route",
    "navigate_to",
    "look",
    "scan_shelf",
    "pick_item",
    "place_in_basket",
    "ask_user",
    "add_item",
    "update_progress",
    "finish_goal",
]
BRAIN_TOOL_NAMES: tuple[str, ...] = (
    "locate_items",
    "plan_route",
    "navigate_to",
    "look",
    "scan_shelf",
    "pick_item",
    "place_in_basket",
    "ask_user",
    "add_item",
    "update_progress",
    "finish_goal",
)
BRAIN_STRICT_TOOLS: frozenset[str] = frozenset(
    {"pick_item", "place_in_basket", "ask_user", "add_item", "update_progress", "finish_goal"}
)
"""Side-effecting brain tools marked ``strict`` in ``brain_tool_definitions(strict=True)`` (the others stay non-strict
so the request fits the strict-schema limits; ``parse_brain_tool_input`` validates every tool anyway)."""
BRAIN_RETRYABLE_CODES: frozenset[str] = frozenset(
    {"path_blocked", "timeout", "grasp_failed", "busy", "not_ready", "disconnected", "no_instance", "unknown_slot"}
)
"""RobotError codes the model may work around (re-plan, retry, skip, re-source)."""
BRAIN_FATAL_CODES: frozenset[str] = frozenset({"battery_low", "unknown_robot"})
"""RobotError codes that end run_goal by raising (the fleet reassigns / docks the robot)."""
NavTarget = Literal["slot", "stop", "checkout", "dock", "entrance"]
AskKind = Literal["question", "recommendation", "over_threshold", "substitution", "online_order", "pick_choice"]
"""trust.ApprovalKind values the brain may raise (never 'checkout': payment approval belongs to the trip flow)."""


class LocateItemsInput(Model):
    skus: list[Sku] = Field(default_factory=list, description="SKUs to find in this robot's store.")
    queries: list[str] = Field(default_factory=list, description="Free-text items to find, e.g. 'mild salsa'.")
    in_stock_only: bool = Field(False, description="Only return slots with stock > 0.")

    @model_validator(mode="after")
    def _something(self) -> "LocateItemsInput":
        if not self.skus and not self.queries:
            raise ValueError("give skus or queries")
        return self


class BrainPlanRouteInput(Model):
    slot_ids: list[SlotId] = Field(
        default_factory=list, description="Slots to visit; empty = every pending item/stop of the goal."
    )
    include_optional: bool = Field(True, description="Shop goals: keep recommendation detours as optional stops.")
    end_at: GoalEndAt = Field("checkout", description="Where the route ends.")


class NavigateToInput(Model):
    target: NavTarget = Field(description="'slot' (needs slot_id), 'stop' (route stop_id), checkout, dock, entrance.")
    slot_id: SlotId | None = None
    stop_id: StopId | None = None

    @model_validator(mode="after")
    def _ids(self) -> "NavigateToInput":
        if self.target == "slot" and not self.slot_id:
            raise ValueError("target 'slot' needs slot_id")
        if self.target == "stop" and not self.stop_id:
            raise ValueError("target 'stop' needs stop_id")
        return self


class LookInput(Model):
    camera: RobotCameraKind = Field(
        "front", description="front = mast camera (shelf ahead), wrist = SO-101 close-up, basket = basket contents."
    )
    question: str | None = Field(None, description="What to check, e.g. 'Is there mild salsa on this shelf?'.")


class ScanShelfInput(Model):
    slot_id: SlotId = Field(description="Slot to scan (the robot should be at its approach pose).")
    expected_sku: Sku | None = Field(None, description="SKU the planogram says is there (defaults to the slot's).")


class PickItemInput(Model):
    slot_id: SlotId
    sku: Sku
    instance_id: InstanceId | None = Field(None, description="A specific unit (e.g. the shopper's freshness pick).")
    item_id: ItemId | None = Field(None, description="Goal item this unit is for.")
    approval_id: str | None = Field(
        None, description="Approval from ask_user (kind over_threshold) that unlocks this item, if one was needed."
    )


class PlaceInBasketInput(Model):
    item_id: ItemId | None = Field(None, description="Goal item the held unit belongs to.")
    verify: bool = Field(True, description="Check the basket camera afterwards.")


class AskUserInput(Model):
    kind: AskKind = Field("question", description="Why you ask; pick_choice = let the shopper pick produce units.")
    question: str = Field(description="Short question, e.g. 'Mild salsa is out. Take the medium one ($0.50 more)?'.")
    body: str = ""
    options: list[str] = Field(
        default_factory=list, description="Choice labels; empty = yes/no (pick_choice: filled from the last scan)."
    )
    item_id: ItemId | None = Field(
        None, description="Goal item this is about (needed for over_threshold / recommendation / pick_choice)."
    )
    sku: Sku | None = Field(None, description="Product this is about when there is no goal item (e.g. a substitute).")
    rec_id: RecId | None = Field(None, description="Recommendation this asks about (kind='recommendation').")


class AddItemInput(Model):
    query: str = Field(description="What to add, e.g. 'limes'.")
    qty: int = Field(1, ge=1, le=20)
    sku: Sku | None = Field(None, description="Exact SKU if you already know it (from locate_items).")


class UpdateProgressInput(Model):
    note: str = Field(description="Status line for the web app, e.g. 'Picked 3 of 7; heading to dairy'.")
    progress: float | None = Field(None, ge=0, le=1)
    item_id: ItemId | None = None
    item_status: GoalItemStatus | None = Field(
        None, description="New status of item_id; 'unavailable' on a shop goal orders it online instead."
    )
    substitute_sku: Sku | None = Field(None, description="item_status='substituted': the SKU used instead.")


class FinishGoalInput(Model):
    status: Literal["completed", "failed"] = "completed"
    summary: str = Field(description="What happened, for the user: 'Got 7 of 8 items; cilantro ordered online.'")
    failure_code: str | None = None


BRAIN_TOOL_INPUT_MODELS: dict[str, type[Model]] = {
    "locate_items": LocateItemsInput,
    "plan_route": BrainPlanRouteInput,
    "navigate_to": NavigateToInput,
    "look": LookInput,
    "scan_shelf": ScanShelfInput,
    "pick_item": PickItemInput,
    "place_in_basket": PlaceInBasketInput,
    "ask_user": AskUserInput,
    "add_item": AddItemInput,
    "update_progress": UpdateProgressInput,
    "finish_goal": FinishGoalInput,
}

BRAIN_TOOL_DESCRIPTIONS: dict[str, str] = {
    "locate_items": (
        "Find where items are in YOUR store: slot, aisle, shelf level, approach pose, price and stock for SKUs or "
        "free-text queries. Use it before planning a route or when an item is missing to find alternatives."
    ),
    "plan_route": (
        "Plan the visiting order through the store for the given slots (or every pending item of your goal). For "
        "shopping goals this re-plans the trip route (recommendation detours stay optional). Returns ordered stops "
        "with distances."
    ),
    "navigate_to": (
        "Drive to a slot's approach pose (facing the shelf), a route stop, the checkout, your dock or the entrance. "
        "Blocks until you arrive or fails with a reason (path_blocked, timeout, battery_low)."
    ),
    "look": (
        "Look through one of your cameras and describe what you see (faces are blurred first). front = the shelf "
        "ahead, wrist = close-up from your SO-101 gripper (labels, price tags, freshness), basket = what you carry. "
        "Ask a specific question to get a specific answer."
    ),
    "scan_shelf": (
        "Scan a slot with your camera: identifies the products, counts units, reads the shelf price tag and "
        "updates the store's stock (out-of-stock and mispricing alerts). Do this at each slot before picking."
    ),
    "pick_item": (
        "Pick ONE unit from a slot with your SO-101 arm (the lift moves to the shelf height). For shopping goals "
        "the shopper's spending rules are checked for the whole item first; if a confirmation is needed the result "
        "says needs_approval: ask_user with kind over_threshold and the item_id, then pick again with that "
        "approval_id."
    ),
    "place_in_basket": (
        "Put the unit in your gripper into your basket. This updates the store stock, adds the item to the cart "
        "once all its units are in (shopping goals), and checks the basket camera."
    ),
    "ask_user": (
        "Ask the user (shopper or staff) a question on their channel and wait for the answer: yes/no or options. "
        "Use it for recommendation detours, substitutions, over-threshold items, online orders and produce picks "
        "(kind pick_choice shows the freshest units from your last scan)."
    ),
    "add_item": (
        "Add an item to your goal when the user asks for something extra ('also grab limes'). It is located in your "
        "store and, for shopping goals, added to the shopping list and route. Remove an item with update_progress "
        "item_status 'skipped'."
    ),
    "update_progress": (
        "Report progress on your goal (shown live in the web app) and update an item's status. Marking an item of "
        "a shopping goal 'unavailable' orders it online instead; 'failed' means you could not pick it."
    ),
    "finish_goal": (
        "Finish your goal as completed or failed with a short summary for the user. Call it exactly once, at the end."
    ),
}


def brain_tool_input_schema(name: str, strict: bool = False) -> dict[str, Any]:
    """JSON Schema of a brain tool's input (see ``tool_input_schema``)."""
    return _model_schema(BRAIN_TOOL_INPUT_MODELS[name], strict)


def brain_tool_definition(name: str, strict: bool = False) -> dict[str, Any]:
    """One brain tool. ``strict=True`` makes it strict only if it is in BRAIN_STRICT_TOOLS (else non-strict)."""
    strict = strict and name in BRAIN_STRICT_TOOLS
    tool: dict[str, Any] = {
        "name": name,
        "description": BRAIN_TOOL_DESCRIPTIONS[name],
        "input_schema": brain_tool_input_schema(name, strict=strict),
    }
    if strict:
        tool["strict"] = True
    return tool


def brain_tool_definitions(strict: bool = False) -> list[dict[str, Any]]:
    """All eleven brain tools, in BRAIN_TOOL_NAMES order (``strict=True``: BRAIN_STRICT_TOOLS strict, rest not)."""
    return [brain_tool_definition(n, strict=strict) for n in BRAIN_TOOL_NAMES]


def strict_schema_budget(tools: list[dict[str, Any]]) -> dict[str, int]:
    """Count what the strict-mode limits measure over the ``strict`` tools of one request: {'strict_tools',
    'optional', 'union'} (optional = properties not in ``required``; union = properties using anyOf / a type array;
    nested objects and $defs included). Limits: STRICT_TOOL_LIMITS."""
    out = {"strict_tools": 0, "optional": 0, "union": 0}

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for v in node:
                walk(v)
            return
        if not isinstance(node, dict):
            return
        props = node.get("properties")
        if isinstance(props, dict):
            required = set(node.get("required", []))
            for pname, sub in props.items():
                out["optional"] += pname not in required
                if isinstance(sub, dict) and ("anyOf" in sub or isinstance(sub.get("type"), list)):
                    out["union"] += 1
        for key, value in node.items():
            if key == "properties" and isinstance(value, dict):
                for sub in value.values():
                    walk(sub)
            elif isinstance(value, dict | list):
                walk(value)

    for tool in tools:
        if tool.get("strict"):
            out["strict_tools"] += 1
            walk(tool["input_schema"])
    return out


STRICT_TOOL_LIMITS: dict[str, int] = {"strict_tools": 20, "optional": 24, "union": 16}
"""Anthropic strict-mode limits per request (combined over all strict schemas)."""


BRAIN_TOOL_DEFINITIONS: list[dict[str, Any]] = brain_tool_definitions()
"""Non-strict brain tool definitions for ``messages.create(tools=...)`` (via utils.llm.run_tool_loop)."""


def parse_brain_tool_input(name: str, data: dict[str, Any]) -> Model:
    """Validate a model-supplied brain tool input dict (raises ValidationError / KeyError)."""
    return BRAIN_TOOL_INPUT_MODELS[name].model_validate(data)


# ---- brain tool results (what the executor returns to the model as tool_result JSON) --------------------------------
class LocatedItem(Model):
    query: str = Field(description="The sku or query this answers.")
    sku: Sku | None = None
    name: str | None = None
    category: CategoryId | None = Field(None, description="Product.category ('produce' -> ask pick_choice).")
    slot_id: SlotId | None = None
    aisle_label: str | None = None
    level: int | None = None
    item_z_m: float | None = Field(None, description="Shelf height (lift target).")
    qty: int = 0
    price_cents: int | None = None


class LocateItemsResult(Model):
    found: list[LocatedItem] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)


class ScanShelfResult(Model):
    slot_id: SlotId
    expected_sku: Sku | None = None
    observed_count: int = 0
    in_stock: bool = True
    mismatch: bool = False
    shelf_tag_cents: int | None = None
    mispriced: bool = False
    scan_id: str | None = None
    note: str = ""


class PlaceResult(Model):
    sku: Sku | None = None
    item_id: ItemId | None = None
    picked_qty: int = Field(0, description="Units of that goal item now in the basket.")
    basket_count: int = 0
    line_id: str | None = Field(None, description="Cart line added (shopping goals, once the item is complete).")
    cart_total_cents: int | None = None
    basket_ok: bool | None = Field(None, description="Basket camera check (None = not verified).")
    note: str = ""


class PickResult(Model):
    """Result of pick_item."""

    picked: bool = Field(description="True = the unit is in the gripper now.")
    instance: ItemInstance | None = None
    needs_approval: bool = Field(False, description="Mandate needs a confirmation: ask_user over_threshold first.")
    decision: MandateDecision | None = Field(None, description="check_line result (shopping goals, first unit).")
    error_code: str | None = Field(None, description="SimErrorCode / 'mandate_denied' when not picked.")
    note: str = ""


class ChoiceOption(Model):
    option_id: str = Field(description="pick_choice: the instance_id to pass to pick_item.")
    label: str
    detail: str | None = None


class AskUserResult(Model):
    """Result of ask_user (thumbnails are never sent to the model)."""

    approval_id: str
    decision: ApprovalDecision
    approved: bool
    choice: str | None = None
    choices: list[str] = Field(default_factory=list)
    options: list[ChoiceOption] = Field(
        default_factory=list, description="What was offered; pick_choice options in freshness rank order (best first)."
    )


class RouteSummaryStop(Model):
    stop_id: StopId
    kind: StopKind
    slot_id: SlotId | None = None
    label: str | None = None
    skus: list[Sku] = Field(default_factory=list)
    qty: int = 1
    rec_id: RecId | None = None
    item_ids: list[ItemId] = Field(default_factory=list)
    distance_from_prev_m: float = 0.0


class RouteSummary(Model):
    """Result of the brain's plan_route: the visiting order without waypoints (the executor keeps the full Route)."""

    route_id: RouteId
    stops: list[RouteSummaryStop] = Field(default_factory=list, description="In visit order, incl. start and end.")
    total_distance_m: float = 0.0
    est_time_s: float | None = None
    skipped_optional: list[StopId] = Field(default_factory=list)

    @classmethod
    def from_route(cls, route: Route) -> "RouteSummary":
        leg_to = {leg.to_stop_id: leg.distance_m for leg in route.legs}
        stops = [
            RouteSummaryStop(
                stop_id=s.stop_id,
                kind=s.kind,
                slot_id=s.slot_id,
                label=s.label,
                skus=list(s.skus),
                qty=s.qty,
                rec_id=s.rec_id,
                item_ids=list(s.item_ids),
                distance_from_prev_m=round(leg_to.get(s.stop_id, 0.0), 2),
            )
            for s in route.ordered_stops()
        ]
        return cls(
            route_id=route.route_id,
            stops=stops,
            total_distance_m=route.total_distance_m,
            est_time_s=route.est_time_s,
            skipped_optional=list(route.skipped_optional),
        )


class BrainToolError(Model):
    """Tool result for a failed brain tool call (sent with is_error=True)."""

    ok: Literal[False] = False
    error_code: str = Field(description="SimErrorCode, 'invalid_input', 'mandate_denied', 'not_found', ...")
    error: str
    retryable: bool = Field(description="True for BRAIN_RETRYABLE_CODES / invalid input: re-plan or retry.")
