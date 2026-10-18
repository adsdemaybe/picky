"""Fleet contracts: natural-language requests -> plans -> goals -> robot assignments, robot info, and the per-robot
managing LLM ("robot brain") trace. Owned by the fleet arm (``FleetService``) and the agent arm (``RobotBrainService``).

Architecture (CLAUDE.md §2):

    NL text (web app chat / text) ─▶ POST /api/requests ─▶ FleetService.submit_request
      ─▶ AgentService.interpret_request (LLM proposes a RequestDraft; code decides)
      ─▶ choose store(s) by stock coverage + price (may SPLIT a list across stores; items no store has -> online
         via the primary store's trip sourcing)
      ─▶ Goal(s) (kind shop | fetch | shelf_scan | return_to_dock) ─▶ assign a robot IN THAT STORE with
         ``choose_robot`` (deterministic: idle, battery, distance) ─▶ RobotBrainService.run_goal(robot_id, goal)
      ─▶ monitor: robot failure -> reassign (up to MAX_GOAL_ATTEMPTS) -> goal.completed / goal.failed

Ownership: the FLEET owns every ``Goal``, ``Assignment`` and ``RequestPlan`` and publishes their events
(request.*, goal.*, fleet.robot_updated). The robot BRAIN changes a goal only through
``services.fleet.update_goal(goal_id, GoalUpdate)`` and publishes ``brain.step`` events (plus the in-store execution
events robot.arrived / scan.result / pick.choices / item.picked / basket.verified for the goal it runs).

Shop goals wrap a Trip: planning (intent -> list -> sourcing -> recs -> route) and post-execution (checkout ->
follow-up) stay in the gateway trip flow; the IN-STORE execution is the brain's job. Trips the fleet creates from a
request have ``auto_execute=True``: no login, the trip flow goes ROUTE_PLANNED -> EXECUTING by itself and calls
``FleetService.execute_trip(trip_id)``, which gets-or-creates the trip's shop goal, (re)builds its items from the
trip's route with ``goal_items_from_trip`` (one GoalItem per route stop, ``item_id = stop_id``), runs it on a robot
and returns the terminal goal; the trip flow then checks out what is in the cart (see execute_trip for the
failed / cancelled cases).

Goal status ownership: the FLEET sets pending / assigned and sets ACTIVE just before calling ``run_goal``; the BRAIN
sets only waiting, active (back from waiting), completed and failed (via update_goal / finish_goal); cancelled comes
from cancel_goal / stop_robot. A same-status update is a no-op (never an error). If ``run_goal`` returns a
non-terminal goal the fleet finalizes it (completed when every non-optional item is in_basket/online, else failed).

Shelf scans: the fleet expands a shelf_scan goal's ``categories`` / ``aisle_ids`` (or, all empty, the whole store) into
concrete ``Goal.slot_ids`` at creation (``catalog.get_planogram``; a category matches Product.category or
subcategory). Every ``scan_shelf`` on a shelf_scan goal appends the slot to ``scanned_slot_ids`` (the executor calls
update_goal); progress = len(scanned_slot_ids) / len(slot_ids); a reassigned goal resumes with slot_ids minus
scanned_slot_ids.
"""

import secrets
from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, computed_field, model_validator

from contracts.cameras import VisionObservation
from contracts.common import (
    AisleId,
    AssignmentId,
    CameraId,
    CategoryId,
    Cents,
    Channel,
    GoalId,
    InstanceId,
    ItemId,
    Model,
    Pose2D,
    PositiveQty,
    RecId,
    RequestId,
    RobotId,
    Score01,
    Sku,
    SlotId,
    StepId,
    StopId,
    StoreId,
    Timestamp,
    TripId,
    UserId,
    utcnow,
)
from contracts.planogram import RobotKind, RobotSpec
from contracts.shopping import ShoppingList
from contracts.sim import RobotBackend, RobotState, RobotStatus
from contracts.trip import Trip

__all__ = [
    "ASSIGNMENT_WEIGHTS",
    "DISTANCE_SCALE_M",
    "GOAL_TRANSITIONS",
    "TERMINAL_GOAL_STATUSES",
    "MAX_GOAL_ATTEMPTS",
    "MIN_BATTERY_PCT",
    "Assignment",
    "AssignmentCandidate",
    "BrainReply",
    "BrainState",
    "BrainStatus",
    "BrainStep",
    "BrainStepKind",
    "FleetRequest",
    "Goal",
    "GoalEndAt",
    "GoalItem",
    "GoalItemStatus",
    "GoalItemUpdate",
    "GoalKind",
    "GoalPriority",
    "GoalRequest",
    "GoalSpec",
    "GoalStatus",
    "GoalUpdate",
    "RequestDraft",
    "RequestIntent",
    "RequestPlan",
    "RequestStatus",
    "RequesterRole",
    "RobotInfo",
    "RobotStatus",
    "StoreChoice",
    "can_goal_transition",
    "choose_robot",
    "goal_items_from_trip",
    "new_goal_item_id",
    "robot_info_from",
    "score_robot",
    "split_budget",
]

GoalKind = Literal["shop", "fetch", "shelf_scan", "return_to_dock"]
"""shop = a shopper's list -> basket -> checkout (wraps a Trip) · fetch = bring ONE item to ``end_at`` without a trip
or payment (staff: price checks, pickup orders; shopper purchases are always shop goals so mandates + approval +
checkout apply) · shelf_scan = scan aisles/categories/slots and update stock · return_to_dock = go charge."""
GoalPriority = Literal["low", "normal", "high", "urgent"]
GoalEndAt = Literal["checkout", "dock", "stay"]
"""Where the robot ends the goal. Defaults: shop/fetch -> checkout, shelf_scan -> stay, return_to_dock -> dock."""
RequesterRole = Literal["shopper", "staff"]
"""Shoppers may create shop goals and ask status / chat questions (reply only, no goal); fetch / shelf_scan /
return_to_dock are staff-only (a shopper asking for them -> RequestPlan status 'rejected'). Staff (web staff/fleet
views) may create any kind."""
MAX_GOAL_ATTEMPTS = 2
"""A goal whose robot fails is reassigned to another eligible robot in the same store up to this many attempts."""


class GoalStatus(StrEnum):
    PENDING = "pending"
    """Created; no robot yet (none free / eligible), or a shop goal waiting for its trip plan."""
    ASSIGNED = "assigned"
    """Robot chosen; queued behind that robot's current goal or about to start."""
    ACTIVE = "active"
    """The robot brain is running it."""
    WAITING = "waiting"
    """Blocked on the user (ask_user / approval). The brain sets it before asking and sets ACTIVE after the answer."""
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


_G = GoalStatus
GOAL_TRANSITIONS: dict[GoalStatus, frozenset[GoalStatus]] = {
    _G.PENDING: frozenset({_G.PENDING, _G.ASSIGNED, _G.CANCELLED, _G.FAILED}),
    _G.ASSIGNED: frozenset({_G.ACTIVE, _G.PENDING, _G.ASSIGNED, _G.CANCELLED, _G.FAILED}),
    _G.ACTIVE: frozenset({_G.ACTIVE, _G.WAITING, _G.COMPLETED, _G.FAILED, _G.CANCELLED, _G.PENDING, _G.ASSIGNED}),
    _G.WAITING: frozenset({_G.WAITING, _G.ACTIVE, _G.COMPLETED, _G.FAILED, _G.CANCELLED, _G.PENDING, _G.ASSIGNED}),
    _G.COMPLETED: frozenset(),
    _G.FAILED: frozenset(),
    _G.CANCELLED: frozenset(),
}
"""Allowed goal transitions. Same-status edges are allowed (update_goal treats them as no-ops). ACTIVE/WAITING ->
PENDING/ASSIGNED = the robot failed (or was lost while its brain waited on the user) and the goal is reassigned."""
TERMINAL_GOAL_STATUSES: frozenset[GoalStatus] = frozenset({_G.COMPLETED, _G.FAILED, _G.CANCELLED})


def can_goal_transition(src: GoalStatus, dst: GoalStatus) -> bool:
    return dst in GOAL_TRANSITIONS[src]


# --------------------------------------------------------------------------------------------------------------------
# Robots
# --------------------------------------------------------------------------------------------------------------------
BrainState = Literal["idle", "thinking", "acting", "waiting_user", "error"]


class RobotInfo(Model):
    """One fleet robot for the web app (GET /api/fleet/robots): static spec + live state + current work.

    Built by the fleet from ``RobotSpec`` (data/fixtures/robots.yaml) + ``RobotService`` state + its goals
    (``robot_info_from``)."""

    robot_id: RobotId
    name: str
    kind: RobotKind = "rover_so101"
    store_id: StoreId
    status: RobotStatus = Field(RobotStatus.DISCONNECTED, description="Live motion status ('disconnected' = no state).")
    available: bool = Field(
        False,
        description="Can take a new goal now (fleet: connected, idle, no current goal, NO queued/pre-assigned goal, "
        "battery >= MIN_BATTERY_PCT).",
    )
    connected: bool = False
    backend: RobotBackend | None = Field(None, description="'isaac' or 'fake' (2D fallback).")
    pose: Pose2D | None = None
    dock: Pose2D
    battery_pct: float | None = Field(None, ge=0, le=100)
    charging: bool = False
    lift_height_m: float | None = None
    holding: InstanceId | None = None
    basket_skus: list[Sku] = Field(default_factory=list)
    basket_capacity: int = Field(12, ge=1)
    current_goal_id: GoalId | None = None
    queued_goal_ids: list[GoalId] = Field(
        default_factory=list,
        description="Goals assigned to it but not started (incl. pre-assigned PENDING shop goals).",
    )
    brain_state: BrainState = "idle"
    last_thought: str | None = Field(None, description="Latest brain.step text (the 'what is it thinking' line).")
    cameras: list[CameraId] = Field(default_factory=list)
    color: str | None = None
    error: str | None = None
    updated_at: Timestamp = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def basket_count(self) -> int:
        return len(self.basket_skus)


def robot_info_from(spec: RobotSpec, state: RobotState | None = None, **extra: Any) -> RobotInfo:
    """RobotInfo from a spec + latest state (None = disconnected); ``extra`` sets fleet fields (goals, brain, ...)."""
    from contracts.cameras import robot_camera_id

    data: dict[str, Any] = {
        "robot_id": spec.robot_id,
        "name": spec.name,
        "kind": spec.kind,
        "store_id": spec.store_id,
        "dock": spec.dock,
        "basket_capacity": spec.basket_capacity,
        "battery_pct": spec.battery_pct,
        "cameras": [robot_camera_id(spec.robot_id, k) for k in spec.cameras],
        "color": spec.color,
    }
    if state is not None:
        data.update(
            status=state.status,
            connected=state.status != RobotStatus.DISCONNECTED,
            backend=state.backend,
            pose=state.pose,
            battery_pct=state.battery_pct if state.battery_pct is not None else spec.battery_pct,
            charging=state.charging,
            lift_height_m=state.lift_height_m,
            holding=state.holding,
            basket_skus=list(state.basket_skus),
            error=state.error,
            store_id=state.store_id or spec.store_id,
        )
    data.update(extra)
    return RobotInfo(**data)


# --------------------------------------------------------------------------------------------------------------------
# Goals
# --------------------------------------------------------------------------------------------------------------------
GoalItemStatus = Literal[
    "pending",
    "located",
    "picked",
    "in_basket",
    "substituted",
    "unavailable",
    "online",
    "skipped",
    "declined",
    "failed",
]
"""Per-item progress: located (slot known) -> picked (in gripper) -> in_basket. substituted = another SKU used
(``sku`` updated); unavailable = not on the shelf (shop: re-sourced online -> 'online'); declined = shopper said no;
skipped = not done on purpose (also how an item is removed from a running goal); failed = could not pick it (grasp /
reach / mandate deny) and NOT re-sourced online.

Shop goals mirror item status onto the trip route stop (``item.stop_id``) through ``trips.set_stop_status``:
in_basket -> done · declined -> declined (+ trips.replan) · unavailable -> failed (+ online line) · online -> failed
(already re-sourced) · skipped -> skipped · failed -> failed (no online reorder)."""


def new_goal_item_id() -> ItemId:
    """Default GoalItem id when the web app omits one ('item_<8 hex>')."""
    return f"item_{secrets.token_hex(4)}"


class GoalItem(Model):
    """Something a goal must pick (shop / fetch). Shop goals: one per trip route stop (``goal_items_from_trip``)."""

    item_id: ItemId = Field(default_factory=new_goal_item_id, description="Shop goals: = stop_id.")
    query: str = Field(description="What was asked for, e.g. 'mild salsa'.")
    sku: Sku | None = Field(None, description="Resolved SKU in the goal's store.")
    name: str | None = None
    qty: PositiveQty = 1
    slot_id: SlotId | None = Field(None, description="Where to pick it in the goal's store.")
    stop_id: StopId | None = Field(None, description="Shop goals: the trip route stop it is (trips.set_stop_status).")
    category: CategoryId | None = Field(None, description="Product.category ('produce' -> ask pick_choice).")
    substitutes: list[Sku] = Field(default_factory=list)
    optional: bool = Field(False, description="Recommendation detour: ask the shopper before picking.")
    rec_id: RecId | None = None
    list_item_id: ItemId | None = Field(None, description="Shop goals: the ShoppingList item it serves.")
    price_cents: Cents | None = Field(
        None, description="Unit price hint at the goal's store (check_line / add_line use the catalog price)."
    )
    status: GoalItemStatus = "pending"
    picked_qty: int = Field(0, ge=0)
    instance_ids: list[InstanceId] = Field(default_factory=list, description="Units placed in the basket.")
    robot_id: RobotId | None = Field(None, description="Robot whose basket holds its units (basket check scope).")
    line_id: str | None = Field(None, description="Shop goals: the cart line its units were added as.")
    notes: str = ""


class Assignment(Model):
    """One robot assignment of a goal (a goal has one per attempt; the last one is current)."""

    assignment_id: AssignmentId
    goal_id: GoalId
    robot_id: RobotId
    store_id: StoreId
    attempt: int = Field(1, ge=1)
    score: float = Field(description="score_robot total (higher = better).")
    breakdown: dict[str, float] = Field(default_factory=dict, description="{'idle': 1.0, 'battery': .82, ...}")
    reason: str = Field(description="Why this robot: 'Pico: idle, 82% battery, 6.1 m from the first stop'.")
    candidates: list["AssignmentCandidate"] = Field(default_factory=list, description="Every robot considered.")
    pinned: bool = Field(False, description="True when the robot was chosen explicitly (manual goal / login).")
    outcome: Literal["active", "completed", "failed", "cancelled", "reassigned"] = "active"
    note: str = ""
    assigned_at: Timestamp = Field(default_factory=utcnow)
    ended_at: Timestamp | None = None


class AssignmentCandidate(Model):
    robot_id: RobotId
    eligible: bool
    score: float = 0.0
    breakdown: dict[str, float] = Field(default_factory=dict)
    reason: str = ""


class Goal(Model):
    """A unit of robot work in ONE store, owned by the fleet (GET /api/goals/{goal_id})."""

    goal_id: GoalId
    kind: GoalKind
    status: GoalStatus = GoalStatus.PENDING
    store_id: StoreId
    robot_id: RobotId | None = Field(None, description="Current robot (last assignment).")
    title: str = Field(description="One line for the UI: 'Shop taco night (8 items) at Picky Ave'.")
    instructions: str = Field("", description="Natural-language brief for the robot brain.")
    priority: GoalPriority = "normal"
    end_at: GoalEndAt = "checkout"
    request_id: RequestId | None = Field(None, description="NL request that created it (None = manual goal).")
    trip_id: TripId | None = Field(None, description="kind='shop': the Trip it executes.")
    user_id: UserId | None = Field(None, description="Who it is for (ask_user goes to them).")
    items: list[GoalItem] = Field(default_factory=list, description="shop / fetch: what to pick.")
    categories: list[CategoryId] = Field(default_factory=list, description="shelf_scan: categories to scan.")
    aisle_ids: list[AisleId] = Field(default_factory=list, description="shelf_scan: aisles to scan.")
    slot_ids: list[SlotId] = Field(
        default_factory=list, description="shelf_scan: explicit slots (empty + no categories/aisles = whole store)."
    )
    scanned_slot_ids: list[SlotId] = Field(default_factory=list, description="shelf_scan progress.")
    progress: Score01 = 0.0
    progress_note: str = Field("", description="Latest status line from the brain: 'Picking salsa (3/7)'.")
    assignments: list[Assignment] = Field(default_factory=list, description="One per attempt; last = current.")
    summary: str | None = Field(None, description="Final result in words (finish_goal).")
    error: str | None = None
    failure_code: str | None = Field(None, description="SimErrorCode or brain code when failed.")
    created_at: Timestamp = Field(default_factory=utcnow)
    updated_at: Timestamp = Field(default_factory=utcnow)
    started_at: Timestamp | None = None
    finished_at: Timestamp | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_GOAL_STATUSES

    @property
    def assignment(self) -> Assignment | None:
        return self.assignments[-1] if self.assignments else None

    def item(self, item_id: ItemId) -> GoalItem | None:
        return next((i for i in self.items if i.item_id == item_id), None)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def attempts(self) -> int:
        return len(self.assignments)


class GoalRequest(Model):
    """A goal to create, without saying where yet (the body of POST /api/fleet/robots/{robot_id}/goals, where the
    path pins the robot; ``api.CreateGoalRequest``). ``GoalSpec`` = this + store_id or robot_id.

    - shop: ``trip_id`` (execute that trip) or ``user_id`` + ``items`` (the fleet creates an auto_execute trip).
    - fetch: exactly one item.  - shelf_scan: categories / aisle_ids / slot_ids (all empty = whole store; the fleet
      expands them into slot_ids).  - return_to_dock: nothing else.
    ``user_id`` = who robot questions go to; the gateway fills it from the caller, the fleet falls back to
    common.STAFF_USER_ID for staff goals without one.
    """

    kind: GoalKind
    store_id: StoreId | None = None
    robot_id: RobotId | None = Field(None, description="Pin a robot; None = fleet assigns with choose_robot.")
    title: str | None = None
    instructions: str = ""
    priority: GoalPriority = "normal"
    end_at: GoalEndAt | None = Field(None, description="None = the kind's default.")
    items: list[GoalItem] = Field(default_factory=list)
    categories: list[CategoryId] = Field(default_factory=list)
    aisle_ids: list[AisleId] = Field(default_factory=list)
    slot_ids: list[SlotId] = Field(default_factory=list)
    trip_id: TripId | None = None
    user_id: UserId | None = None
    request_id: RequestId | None = None

    @model_validator(mode="after")
    def _shape(self) -> "GoalRequest":
        if self.kind == "shop" and self.trip_id is None and not (self.user_id and self.items):
            raise ValueError("shop goals need trip_id, or user_id + items")
        if self.kind == "fetch" and len(self.items) != 1:
            raise ValueError("fetch goals need exactly one item")
        return self

    def resolved_end_at(self) -> GoalEndAt:
        if self.end_at is not None:
            return self.end_at
        return {"shop": "checkout", "fetch": "checkout", "shelf_scan": "stay", "return_to_dock": "dock"}[self.kind]


class GoalSpec(GoalRequest):
    """Create a goal: FleetService.create_goal and POST /api/goals (store_id and/or robot_id required; robot_id None =
    the fleet assigns with choose_robot; ``store_id`` defaults to the pinned robot's store)."""

    @model_validator(mode="after")
    def _target(self) -> "GoalSpec":
        if self.store_id is None and self.robot_id is None:
            raise ValueError("give store_id or robot_id")
        return self


class GoalItemUpdate(Model):
    item_id: ItemId
    status: GoalItemStatus | None = None
    picked_qty: int | None = Field(None, ge=0)
    instance_ids: list[InstanceId] | None = None
    sku: Sku | None = Field(None, description="Substitution: the SKU actually used.")
    slot_id: SlotId | None = None
    robot_id: RobotId | None = None
    line_id: str | None = None
    notes: str | None = None


class GoalUpdate(Model):
    """The brain's (and the fleet's own) partial update of a goal: FleetService.update_goal. None = unchanged."""

    status: GoalStatus | None = Field(
        None, description="Brain may set waiting / active (from waiting) / completed / failed; same status = no-op."
    )
    progress: Score01 | None = None
    note: str | None = Field(None, description="New progress_note.")
    items: list[GoalItemUpdate] = Field(default_factory=list, description="Patch existing items by item_id.")
    add_items: list[GoalItem] = Field(
        default_factory=list,
        description="Append items to a running goal ('also grab limes'; brain add_item tool). Remove = status skipped.",
    )
    scanned_slot_ids: list[SlotId] = Field(default_factory=list, description="Appended (shelf_scan).")
    summary: str | None = None
    error: str | None = None
    failure_code: str | None = None


# --------------------------------------------------------------------------------------------------------------------
# Natural-language requests
# --------------------------------------------------------------------------------------------------------------------
RequestIntent = Literal["shop", "fetch", "shelf_scan", "return_to_dock", "status", "chat"]
RequestStatus = Literal["received", "planned", "executing", "completed", "failed", "needs_input", "rejected"]


class FleetRequest(Model):
    """POST /api/requests: anything a user types in the web app ('tacos for 6 under $40, one veg',
    'scan the dairy aisle at Hill', 'Hazel, go charge')."""

    text: str = Field(min_length=1)
    user_id: UserId
    channel: Channel = "web"
    store_id: StoreId | None = Field(None, description="Store hint (UI selection); None = the fleet decides.")
    role: RequesterRole = "shopper"
    in_reply_to: RequestId | None = Field(
        None,
        description="Answer to that request's clarifying question (status needs_input): the fleet re-interprets with "
        "the earlier turns as history and updates THAT RequestPlan (same request_id) instead of creating a new one.",
    )


class RequestDraft(Model):
    """What the LLM thinks the request means (AgentService.interpret_request). The LLM proposes; the fleet decides
    stores, goals and robots deterministically."""

    intent: RequestIntent
    summary: str = Field(description="One line: 'Taco night for 6, under $40, one vegetarian'.")
    shopping_list: ShoppingList | None = Field(
        None, description="shop / fetch: expanded list (queries, qty, substitutes, budget); sourcing unresolved."
    )
    store_hint: StoreId | None = Field(None, description="Store named in the text, else the request's store_id.")
    robot_hint: RobotId | None = Field(None, description="Robot named in the text ('Hazel, ...').")
    categories: list[CategoryId] = Field(default_factory=list, description="shelf_scan targets.")
    aisle_ids: list[AisleId] = Field(default_factory=list)
    priority: GoalPriority = "normal"
    reply: str = Field("", description="What to tell the user right away.")
    clarifying_question: str | None = Field(None, description="Set when the request is ambiguous -> needs_input.")
    confidence: Score01 = 1.0


class StoreChoice(Model):
    """One candidate store for a shop/fetch request, with the deterministic reasoning (shown in the web app)."""

    store_id: StoreId
    store_name: str | None = None
    chosen: bool = False
    item_ids: list[ItemId] = Field(default_factory=list, description="List items this store will cover.")
    in_stock_count: int = Field(0, ge=0, description="Requested items in stock here.")
    total_items: int = Field(0, ge=0)
    est_total_cents: Cents = Field(0, description="Sum of in-stock line totals at this store.")
    missing: list[str] = Field(default_factory=list, description="Queries not in stock here.")
    available_robots: int = Field(0, ge=0)
    score: float = 0.0
    reason: str = Field("", description="'7/8 items in stock, $3.20 cheaper than Picky Hill, 1 robot free'.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def coverage(self) -> float:
        return round(self.in_stock_count / self.total_items, 3) if self.total_items else 0.0


class RequestPlan(Model):
    """Result of POST /api/requests (and GET /api/requests/{id}): interpretation, store choice, goals."""

    request_id: RequestId
    text: str
    user_id: UserId
    channel: Channel = "web"
    role: RequesterRole = "shopper"
    status: RequestStatus = "received"
    interpretation: RequestDraft | None = None
    store_choices: list[StoreChoice] = Field(default_factory=list, description="All candidates; chosen=True used.")
    goals: list[Goal] = Field(default_factory=list, description="Goal snapshots (live: GET /api/goals/{id}).")
    trip_ids: list[TripId] = Field(default_factory=list, description="shop: one Trip per chosen store.")
    budget_split: dict[StoreId, Cents] = Field(
        default_factory=dict, description="Split shop request: each trip's share of the budget (split_budget)."
    )
    replies: list[str] = Field(default_factory=list, description="User answers to clarifying questions (in_reply_to).")
    reply: str = Field("", description="Assistant message for the chat ('Pico is on it at Picky Ave ...').")
    error: str | None = None
    created_at: Timestamp = Field(default_factory=utcnow)
    updated_at: Timestamp = Field(default_factory=utcnow)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def split(self) -> bool:
        """True when the request was split across several stores."""
        return sum(1 for c in self.store_choices if c.chosen) > 1

    @computed_field  # type: ignore[prop-decorator]
    @property
    def est_total_cents(self) -> int:
        """Sum of the chosen stores' estimated in-store totals."""
        return sum(c.est_total_cents for c in self.store_choices if c.chosen)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def budget_cents(self) -> int | None:
        """The stated budget of the whole request (split across trips by ``budget_split``)."""
        draft = self.interpretation
        return draft.shopping_list.budget_cents if draft and draft.shopping_list else None


def split_budget(budget_cents: int, est_totals: dict[StoreId, int]) -> dict[StoreId, int]:
    """THE budget split for a request spread over several stores: each store's share of ``budget_cents`` is
    proportional to its estimated total (equal shares when every estimate is 0); shares are integers that sum EXACTLY
    to ``budget_cents`` (largest remainder; ties -> store_id order). Each trip's ShoppingList.budget_cents = its share.
    """
    if not est_totals:
        return {}
    weights = {sid: max(0, est) for sid, est in est_totals.items()}
    total = sum(weights.values())
    if total == 0:
        weights = {sid: 1 for sid in est_totals}
        total = len(weights)
    raw = {sid: budget_cents * w / total for sid, w in weights.items()}
    shares = {sid: int(v) for sid, v in raw.items()}
    left = budget_cents - sum(shares.values())
    for sid in sorted(raw, key=lambda k: (-(raw[k] - shares[k]), k))[:left]:
        shares[sid] += 1
    return shares


# --------------------------------------------------------------------------------------------------------------------
# Robot brain (per-robot managing LLM) trace
# --------------------------------------------------------------------------------------------------------------------
BrainStepKind = Literal["thought", "tool_call", "tool_result", "observation", "message", "status", "error"]


class BrainStep(Model):
    """One step of a robot brain, published as a ``brain.step`` event so the web app can show what each robot is
    thinking. The fake (scripted) brain emits the same tool sequence with ``model='scripted'``."""

    step_id: StepId
    robot_id: RobotId
    goal_id: GoalId | None = None
    trip_id: TripId | None = None
    store_id: StoreId | None = None
    step_no: int = Field(
        0, ge=0, description="Per-robot step counter (1-based once published). NOT the bus seq (event envelope)."
    )
    kind: BrainStepKind
    role: Literal["robot", "user", "system"] = "robot"
    text: str = Field("", description="Thought / message text, or a one-line summary of the tool call/result.")
    tool: str | None = Field(None, description="One of contracts.tools.BRAIN_TOOL_NAMES for tool_call/tool_result.")
    tool_use_id: str | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any = Field(None, description="Tool result (JSON-able, kept small; frames are never inlined).")
    ok: bool = True
    observation: VisionObservation | None = Field(None, description="kind='observation': what the camera saw.")
    camera_id: CameraId | None = None
    duration_ms: float = 0.0
    model: str | None = Field(None, description="'claude-sonnet-5-5' / 'scripted'.")
    ts: Timestamp = Field(default_factory=utcnow)


class BrainReply(Model):
    """Response of POST /api/fleet/robots/{robot_id}/messages (RobotBrainService.handle_message)."""

    robot_id: RobotId
    goal_id: GoalId | None = None
    text: str
    steps: list[BrainStep] = Field(default_factory=list)
    ts: Timestamp = Field(default_factory=utcnow)


class BrainStatus(Model):
    """RobotBrainService.status(robot_id)."""

    robot_id: RobotId
    state: BrainState = "idle"
    goal_id: GoalId | None = None
    mode: Literal["llm", "scripted"] = "scripted"
    model: str | None = None
    iterations: int = Field(0, ge=0, description="LLM turns spent on the current goal.")
    current_tool: str | None = None
    last_step: BrainStep | None = None
    pending_messages: int = Field(0, ge=0, description="User messages not yet consumed by the running goal.")
    updated_at: Timestamp = Field(default_factory=utcnow)


# --------------------------------------------------------------------------------------------------------------------
# THE assignment rule (deterministic; fake and real fleet both use it)
# --------------------------------------------------------------------------------------------------------------------
ASSIGNMENT_WEIGHTS: dict[str, float] = {"idle": 0.5, "battery": 0.3, "distance": 0.2}
MIN_BATTERY_PCT = 20.0
"""Below this a robot only takes return_to_dock goals."""
DISTANCE_SCALE_M = 10.0
"""distance score = 1 / (1 + d / DISTANCE_SCALE_M), d = straight-line meters robot -> goal target."""


def score_robot(
    robot: RobotInfo,
    *,
    store_id: StoreId,
    target: Pose2D | None = None,
    kind: GoalKind = "shop",
) -> AssignmentCandidate:
    """Score one robot for a goal in ``store_id`` (higher = better). Ineligible: other store, disconnected/error,
    battery < MIN_BATTERY_PCT (unless return_to_dock). idle = 1.0 when available with no current AND no queued
    (pre-assigned) goal, 0.25 otherwise (it would queue)."""
    rid = robot.robot_id
    if robot.store_id != store_id:
        return AssignmentCandidate(robot_id=rid, eligible=False, reason=f"{robot.name}: in another store")
    if not robot.connected or robot.status in (RobotStatus.DISCONNECTED, RobotStatus.ERROR):
        return AssignmentCandidate(robot_id=rid, eligible=False, reason=f"{robot.name}: {robot.status.value}")
    battery = robot.battery_pct if robot.battery_pct is not None else 100.0
    if battery < MIN_BATTERY_PCT and kind != "return_to_dock":
        return AssignmentCandidate(robot_id=rid, eligible=False, reason=f"{robot.name}: battery {battery:.0f}%")
    free = robot.available and robot.current_goal_id is None and not robot.queued_goal_ids
    idle = 1.0 if free else 0.25
    dist = robot.pose.distance_to(target) if (robot.pose is not None and target is not None) else 0.0
    breakdown = {
        "idle": idle,
        "battery": round(battery / 100.0, 4),
        "distance": round(1.0 / (1.0 + dist / DISTANCE_SCALE_M), 4),
    }
    score = round(sum(ASSIGNMENT_WEIGHTS[k] * v for k, v in breakdown.items()), 4)
    busy = "idle" if idle == 1.0 else "busy (would queue)"
    where = f", {dist:.1f} m away" if target is not None and robot.pose is not None else ""
    return AssignmentCandidate(
        robot_id=rid,
        eligible=True,
        score=score,
        breakdown=breakdown,
        reason=f"{robot.name}: {busy}, {battery:.0f}% battery{where}",
    )


def choose_robot(
    robots: list[RobotInfo],
    *,
    store_id: StoreId,
    target: Pose2D | None = None,
    kind: GoalKind = "shop",
) -> tuple[AssignmentCandidate | None, list[AssignmentCandidate]]:
    """Best eligible candidate (ties -> robot_id order) and every candidate, best first."""
    cands = [score_robot(r, store_id=store_id, target=target, kind=kind) for r in robots]
    cands.sort(key=lambda c: (not c.eligible, -c.score, c.robot_id))
    best = cands[0] if cands and cands[0].eligible else None
    return best, cands


# --------------------------------------------------------------------------------------------------------------------
# Shop goal <-> trip route
# --------------------------------------------------------------------------------------------------------------------
_STOP_TO_ITEM_STATUS: dict[str, GoalItemStatus] = {
    "done": "in_basket",
    "declined": "declined",
    "skipped": "skipped",
    "failed": "failed",
}


def goal_items_from_trip(trip: Trip) -> list[GoalItem]:
    """THE mapping trip route -> shop goal items (execute_trip always (re)builds a shop goal's items with it).

    One GoalItem per mandatory / optional stop of ``trip.route``, in visit order (then unvisited stops):
    ``item_id = stop_id = stop.stop_id``, ``qty = stop.qty``, ``slot_id``, ``sku = skus[0]``, ``rec_id``,
    ``optional = kind == 'optional'``, ``list_item_id = item_ids[0]``, query / category / substitutes from that list
    item, price from the matching recommendation (else the list item's est_line_cents / qty). Status comes from
    ``trip.stop_status`` (done -> in_basket, declined, skipped, failed), solver-skipped optional stops -> skipped,
    else pending (picked_qty = qty for done stops).
    """
    route = trip.route
    if route is None:
        return []
    recs = {r.rec_id: r for r in trip.recs}
    list_items = {i.item_id: i for i in (trip.shopping_list.items if trip.shopping_list else [])}
    order = {sid: n for n, sid in enumerate(route.order)}
    stops = [s for s in route.stops if s.kind in ("mandatory", "optional")]
    stops.sort(key=lambda s: (order.get(s.stop_id, len(order)), s.stop_id if s.stop_id not in order else ""))
    items: list[GoalItem] = []
    for stop in stops:
        rec = recs.get(stop.rec_id) if stop.rec_id else None
        li = list_items.get(stop.item_ids[0]) if stop.item_ids else None
        price: int | None = rec.price_cents if rec is not None else None
        if price is None and li is not None and li.est_line_cents is not None:
            price = li.est_line_cents // max(li.qty, 1)
        status: GoalItemStatus = _STOP_TO_ITEM_STATUS.get(trip.stop_status.get(stop.stop_id, ""), "pending")
        if status == "pending" and stop.stop_id in route.skipped_optional:
            status = "skipped"
        sku = stop.skus[0] if stop.skus else (li.sku if li else None)
        items.append(
            GoalItem(
                item_id=stop.stop_id,
                stop_id=stop.stop_id,
                query=(li.query if li else None) or (rec.product_name if rec else None) or stop.label or sku or "item",
                sku=sku,
                name=(rec.product_name if rec else None) or stop.label,
                qty=max(stop.qty, 1),
                slot_id=stop.slot_id,
                category=li.category if li else None,
                substitutes=list(li.substitutes) if li else [],
                optional=stop.kind == "optional",
                rec_id=stop.rec_id,
                list_item_id=stop.item_ids[0] if stop.item_ids else None,
                price_cents=price,
                status=status,
                picked_qty=max(stop.qty, 1) if status == "in_basket" else 0,
            )
        )
    return items


Assignment.model_rebuild()
