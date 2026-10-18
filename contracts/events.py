"""User-visible events: event bus -> websocket ``/ws`` -> the web app (shopper, store staff and fleet views).

Every event is a JSON object discriminated on ``type`` and carries the envelope fields ``trip_id``, ``store_id``,
``robot_id``, ``user_id``, ``request_id``, ``ts`` and ``seq``. Producers build events with ``seq=0``; the gateway's
EventPublisher stamps ``seq`` strictly increasing ACROSS THE WHOLE BUS (gaps are normal when a client filters; clients
dedupe by seq and resume with ``after_seq``). TS types: ``event_json_schema()`` -> json-schema-to-typescript.

Envelope rules (producers MUST follow them, or filtered sockets miss events):
- Every event about a SHOP goal (goal.*, brain.step, robot.arrived, scan.result, pick.choices, item.picked,
  basket.verified, error, and approval.requested / resolved for its questions) sets envelope ``trip_id = goal.trip_id``
  plus store_id, robot_id, user_id and request_id: build it with ``make_event(type, **goal_envelope(goal), ...)``.
- Trip-flow events: ``make_event(type, **trip_envelope(trip), ...)`` (trip_id, store_id, robot_id, user_id,
  request_id from the Trip). request.* events: ``request_id`` + ``user_id`` (no trip_id). Store-level events (stock.*,
  fleet.robot_updated, non-shop goals) leave trip_id None and set store_id (+ robot_id when about a robot).
- Filters: ``/ws?trip_id=&store_id=&robot_id=&user_id=&request_id=`` - a key may repeat (OR within a key), keys
  combine with AND; an event passes when, for every given key, its envelope field is one of the values (null never
  matches). ``event_matches`` is THE rule. A split request's shopper view uses ``?request_id=<id>`` (or repeats
  trip_id) on ONE socket.

Producers (``EVENT_PRODUCERS``; exactly one per type, so nothing is published twice): the arm that owns the state.
- gateway: trip flow (trip.*, intent/list/sourcing/recs/route, stop.updated, cart, approvals, mandate, checkout,
  follow-up, feedback/profile), robot.state, and the live-only frame / camera.frame.
- fleet: request.received / request.planned, goal.created / assigned / updated / completed / failed,
  fleet.robot_updated.
- agent: agent.message (shopper chat streaming); the robot brain: brain.step plus the in-store execution events of
  the goal it runs (robot.arrived, scan.result, pick.choices, item.picked, basket.verified).
- catalog: stock.updated, stock.alert.   - any arm: error.

Live-only events (``LIVE_ONLY_EVENT_TYPES`` = ``frame``, ``camera.frame``) never go through the sequenced bus/replay
ring: they are sent latest-only with ``seq=0`` on ``/ws/frames?trip_id=|robot_id=``, ``/ws/cameras/{camera_id}`` and
the multiplexed ``/ws/cameras?camera_id=..|store_id=|robot_id=``. ``camera.frame`` (keyed by camera_id) is THE live
frame event; ``frame`` is legacy (front camera only) and is never sent for an image that also goes out as
``camera.frame`` on the same socket.
``robot.state`` on ``/ws`` is throttled to <= 2 Hz per robot (plus every status change); full-rate robot.state also
goes on ``/ws/frames``.
"""

from collections.abc import Collection, Mapping
from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, field_validator

from contracts.catalog import StockAlert, StockLevel, StockMovement
from contracts.common import (
    CameraId,
    Channel,
    GoalId,
    Model,
    Pose2D,
    RecId,
    RequestId,
    RobotId,
    Sku,
    SlotId,
    StopId,
    StoreId,
    Timestamp,
    TripId,
    UserId,
    utcnow,
)
from contracts.fleet import Assignment, BrainStep, FleetRequest, Goal, RequestPlan, RobotInfo
from contracts.messaging import OutboundMessage
from contracts.perception import BasketCheck, PickChoice, ScanReport
from contracts.profile import Feedback, ProfileUpdate
from contracts.routing import Route, StopStatus
from contracts.shopping import Cart, Intent, ListItem, Recommendation, ShoppingList, TripState
from contracts.sim import CameraFrame, RobotState
from contracts.trip import Trip
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse, MandateDecision, Receipt


class EventBase(Model):
    trip_id: TripId | None = Field(
        None,
        description="Trip it belongs to (filter key). Shop-goal events MUST carry goal.trip_id; None only for "
        "store/fleet-level events (stock, non-shop goals, robots) and request.*.",
    )
    store_id: StoreId | None = Field(None, description="Store the event is about (filter key).")
    robot_id: RobotId | None = Field(None, description="Robot the event is about (filter key).")
    user_id: UserId | None = Field(None, description="User it is for (filter key; Trip/Goal/RequestPlan.user_id).")
    request_id: RequestId | None = Field(None, description="NL request it stems from (filter key).")
    ts: Timestamp = Field(default_factory=utcnow)
    seq: int = Field(0, ge=0, description="Assigned by the publisher; 0 = not yet published.")
    source: str | None = Field(None, description="Producing arm, e.g. 'gateway', 'robot'.")


class TripCreated(EventBase):
    type: Literal["trip.created"] = "trip.created"
    trip: Trip


class TripSnapshot(EventBase):
    """Sent first on every /ws connect (and on resync): the full current trip. ``seq`` = bus seq at snapshot time."""

    type: Literal["trip.snapshot"] = "trip.snapshot"
    trip: Trip


class TripStateChanged(EventBase):
    type: Literal["trip.state_changed"] = "trip.state_changed"
    from_state: TripState | None = None
    to_state: TripState
    reason: str = ""


class IntentReceived(EventBase):
    type: Literal["intent.received"] = "intent.received"
    intent: Intent


class ListReady(EventBase):
    type: Literal["list.ready"] = "list.ready"
    shopping_list: ShoppingList


class SourcingSplit(EventBase):
    type: Literal["sourcing.split"] = "sourcing.split"
    store_id: StoreId
    in_store: list[ListItem] = Field(default_factory=list)
    online: list[ListItem] = Field(default_factory=list)
    unresolved: list[ListItem] = Field(default_factory=list)


class RecsReady(EventBase):
    """Field names match Trip.recs / Trip.checkout_recs."""

    type: Literal["recs.ready"] = "recs.ready"
    recs: list[Recommendation] = Field(default_factory=list, description="Detour/near-expiry recs.")
    checkout_recs: list[Recommendation] = Field(default_factory=list, description="Batched 'want any of these?'.")


class RoutePlanned(EventBase):
    type: Literal["route.planned"] = "route.planned"
    route: Route
    replan: bool = Field(False, description="True when re-solved mid-trip (after a yes/no).")


class RobotStateEvent(EventBase):
    type: Literal["robot.state"] = "robot.state"
    state: RobotState


class StopUpdated(EventBase):
    """A route stop changed status (mirrors Trip.stop_status; survives re-plans)."""

    type: Literal["stop.updated"] = "stop.updated"
    stop_id: StopId
    status: StopStatus
    reason: str = ""
    rec_id: RecId | None = None
    slot_id: SlotId | None = None


class RobotArrived(EventBase):
    type: Literal["robot.arrived"] = "robot.arrived"
    stop_id: StopId | None = Field(None, description="Trip route stop (shop goals); None for other goals.")
    slot_id: SlotId | None = None
    goal_id: GoalId | None = None
    pose: Pose2D


class ScanResult(EventBase):
    type: Literal["scan.result"] = "scan.result"
    report: ScanReport


class PickChoices(EventBase):
    type: Literal["pick.choices"] = "pick.choices"
    choice: PickChoice
    approval_id: str | None = None


class ItemPicked(EventBase):
    type: Literal["item.picked"] = "item.picked"
    sku: Sku
    slot_id: SlotId | None = None
    instance_id: str | None = None
    line_id: str | None = None
    stop_id: StopId | None = None
    goal_id: GoalId | None = None


class BasketVerified(EventBase):
    type: Literal["basket.verified"] = "basket.verified"
    check: BasketCheck


class CartUpdated(EventBase):
    type: Literal["cart.updated"] = "cart.updated"
    cart: Cart
    total_cents: int = Field(ge=0)
    budget_cents: int | None = None
    remaining_cents: int | None = Field(None, description="budget - total (negative = over budget).")


class ApprovalRequested(EventBase):
    type: Literal["approval.requested"] = "approval.requested"
    request: ApprovalRequest


class ApprovalResolved(EventBase):
    type: Literal["approval.resolved"] = "approval.resolved"
    kind: ApprovalKind | None = None
    response: ApprovalResponse


class MandateDecisionEvent(EventBase):
    type: Literal["mandate.decision"] = "mandate.decision"
    decision: MandateDecision


class CheckoutCompleted(EventBase):
    type: Literal["checkout.completed"] = "checkout.completed"
    receipt: Receipt


class FollowupSent(EventBase):
    type: Literal["followup.sent"] = "followup.sent"
    message: OutboundMessage


class FeedbackReceived(EventBase):
    type: Literal["feedback.received"] = "feedback.received"
    feedback: Feedback


class ProfileUpdated(EventBase):
    type: Literal["profile.updated"] = "profile.updated"
    update: ProfileUpdate


class FrameEvent(EventBase):
    """LEGACY live frame (front camera only; prefer ``camera.frame``), sent ONLY on ``/ws/frames`` (latest-only,
    seq=0, never replayed) to v0.1 clients.

    MUST be face-blurred (CLAUDE.md §8).
    """

    type: Literal["frame"] = "frame"
    frame: CameraFrame

    @field_validator("frame")
    @classmethod
    def _must_be_blurred(cls, v: CameraFrame) -> CameraFrame:
        if not v.blurred:
            raise ValueError("frame events must carry a face-blurred frame (blurred=True)")
        return v


class CameraFrameEvent(EventBase):
    """Live frame of one camera (robot or ceiling), live-only on ``/ws/cameras/{camera_id}``, the multiplexed
    ``/ws/cameras`` and ``/ws/frames`` (every camera of the robot).

    MUST be face-blurred (CLAUDE.md §8)."""

    type: Literal["camera.frame"] = "camera.frame"
    camera_id: CameraId
    frame: CameraFrame

    @field_validator("frame")
    @classmethod
    def _must_be_blurred(cls, v: CameraFrame) -> CameraFrame:
        if not v.blurred:
            raise ValueError("camera.frame events must carry a face-blurred frame (blurred=True)")
        return v


# ---- fleet (producer: fleet) -----------------------------------------------------------------------------------------
class RequestReceived(EventBase):
    type: Literal["request.received"] = "request.received"
    request_id: RequestId
    request: FleetRequest


class RequestPlanned(EventBase):
    """Interpretation + store choice + goals are ready (also re-sent when a request finishes)."""

    type: Literal["request.planned"] = "request.planned"
    plan: RequestPlan


class GoalCreated(EventBase):
    type: Literal["goal.created"] = "goal.created"
    goal: Goal


class GoalAssigned(EventBase):
    """A robot was (re)assigned: ``assignment.attempt`` > 1 is a reassignment after a failure."""

    type: Literal["goal.assigned"] = "goal.assigned"
    goal: Goal
    assignment: Assignment


class GoalUpdated(EventBase):
    """Status (active / waiting / pending), progress, item or scan progress changed."""

    type: Literal["goal.updated"] = "goal.updated"
    goal: Goal
    note: str = ""


class GoalCompleted(EventBase):
    type: Literal["goal.completed"] = "goal.completed"
    goal: Goal


class GoalFailed(EventBase):
    """Terminal failure (reassignments are goal.assigned, not goal.failed). Cancellation also uses goal.failed with
    ``goal.status == 'cancelled'``."""

    type: Literal["goal.failed"] = "goal.failed"
    goal: Goal
    reason: str = ""


class FleetRobotUpdated(EventBase):
    """A robot's fleet view changed (status, availability, battery, goal, brain state). Throttled to <= 1 Hz per
    robot for pose-only changes."""

    type: Literal["fleet.robot_updated"] = "fleet.robot_updated"
    robot: RobotInfo


# ---- robot brain (producer: agent) -----------------------------------------------------------------------------------
class BrainStepEvent(EventBase):
    type: Literal["brain.step"] = "brain.step"
    step: BrainStep


# ---- stock (producer: catalog) ---------------------------------------------------------------------------------------
class StockUpdated(EventBase):
    type: Literal["stock.updated"] = "stock.updated"
    level: StockLevel
    movement: StockMovement | None = None


class StockAlertEvent(EventBase):
    """A SKU went low/out (``alert.active``) or was restocked back to ok (``alert.active=False``)."""

    type: Literal["stock.alert"] = "stock.alert"
    alert: StockAlert


class AgentMessage(EventBase):
    type: Literal["agent.message"] = "agent.message"
    message_id: str
    role: Literal["user", "assistant", "system", "tool"] = "assistant"
    text: str = Field("", description="Full text so far (or final text when done).")
    delta: str | None = Field(None, description="Streaming: the newly appended chunk.")
    done: bool = True
    channel: Channel | None = None


class ErrorEvent(EventBase):
    type: Literal["error"] = "error"
    arm: str
    message: str
    recoverable: bool = True
    code: str | None = None


Event = Annotated[
    TripCreated
    | TripSnapshot
    | TripStateChanged
    | IntentReceived
    | ListReady
    | SourcingSplit
    | RecsReady
    | RoutePlanned
    | StopUpdated
    | RobotStateEvent
    | RobotArrived
    | ScanResult
    | PickChoices
    | ItemPicked
    | BasketVerified
    | CartUpdated
    | ApprovalRequested
    | ApprovalResolved
    | MandateDecisionEvent
    | CheckoutCompleted
    | FollowupSent
    | FeedbackReceived
    | ProfileUpdated
    | FrameEvent
    | CameraFrameEvent
    | RequestReceived
    | RequestPlanned
    | GoalCreated
    | GoalAssigned
    | GoalUpdated
    | GoalCompleted
    | GoalFailed
    | FleetRobotUpdated
    | BrainStepEvent
    | StockUpdated
    | StockAlertEvent
    | AgentMessage
    | ErrorEvent,
    Field(discriminator="type"),
]

EVENT_ADAPTER: TypeAdapter[Any] = TypeAdapter(Event)

EVENT_CLASSES: tuple[type[EventBase], ...] = (
    TripCreated,
    TripSnapshot,
    TripStateChanged,
    IntentReceived,
    ListReady,
    SourcingSplit,
    RecsReady,
    RoutePlanned,
    StopUpdated,
    RobotStateEvent,
    RobotArrived,
    ScanResult,
    PickChoices,
    ItemPicked,
    BasketVerified,
    CartUpdated,
    ApprovalRequested,
    ApprovalResolved,
    MandateDecisionEvent,
    CheckoutCompleted,
    FollowupSent,
    FeedbackReceived,
    ProfileUpdated,
    FrameEvent,
    CameraFrameEvent,
    RequestReceived,
    RequestPlanned,
    GoalCreated,
    GoalAssigned,
    GoalUpdated,
    GoalCompleted,
    GoalFailed,
    FleetRobotUpdated,
    BrainStepEvent,
    StockUpdated,
    StockAlertEvent,
    AgentMessage,
    ErrorEvent,
)

EVENT_TYPES: dict[str, type[EventBase]] = {cls.model_fields["type"].default: cls for cls in EVENT_CLASSES}
"""Map from ``type`` string to event class."""

LIVE_ONLY_EVENT_TYPES: frozenset[str] = frozenset({"frame", "camera.frame"})
"""Never sequenced, stored or replayed; sent latest-only on /ws/frames and /ws/cameras/{camera_id}."""

_FLEET_EVENTS = (
    "request.received",
    "request.planned",
    "goal.created",
    "goal.assigned",
    "goal.updated",
    "goal.completed",
    "goal.failed",
    "fleet.robot_updated",
)
_BRAIN_EVENTS = ("brain.step", "robot.arrived", "scan.result", "pick.choices", "item.picked", "basket.verified")

EVENT_PRODUCERS: dict[str, str] = (
    {t: "gateway" for t in EVENT_TYPES}
    | {t: "fleet" for t in _FLEET_EVENTS}
    | {t: "agent" for t in _BRAIN_EVENTS}
    | {"agent.message": "agent", "stock.updated": "catalog", "stock.alert": "catalog", "error": "any"}
)
"""Who publishes each event type (see module docstring). 'agent' covers both the shopper agent and the robot brain."""


def make_event(
    type_: str,
    *,
    trip_id: TripId | None,
    source: str | None = None,
    store_id: StoreId | None = None,
    robot_id: RobotId | None = None,
    user_id: UserId | None = None,
    request_id: RequestId | None = None,
    **payload: Any,
) -> Event:
    """Build an event envelope by type name: ``make_event("route.planned", trip_id=t, route=r)``; shop-goal events:
    ``make_event("goal.updated", **goal_envelope(goal), goal=goal, source="fleet")``."""
    try:
        cls = EVENT_TYPES[type_]
    except KeyError as exc:
        raise ValueError(f"unknown event type {type_!r}") from exc
    for key, value in (
        ("store_id", store_id),
        ("robot_id", robot_id),
        ("user_id", user_id),
        ("request_id", request_id),
    ):
        if value is not None:
            payload[key] = value
    return cls(trip_id=trip_id, source=source, **payload)  # type: ignore[return-value]


def goal_envelope(goal: Goal, robot_id: RobotId | None = None) -> dict[str, Any]:
    """Envelope kwargs for any event about ``goal`` (trip_id = goal.trip_id for shop goals; ``robot_id`` overrides
    the goal's current robot, e.g. for the robot that is executing it)."""
    return {
        "trip_id": goal.trip_id,
        "store_id": goal.store_id,
        "robot_id": robot_id or goal.robot_id,
        "user_id": goal.user_id,
        "request_id": goal.request_id,
    }


def trip_envelope(trip: Trip) -> dict[str, Any]:
    """Envelope kwargs for any event about ``trip``."""
    return {
        "trip_id": trip.trip_id,
        "store_id": trip.store_id,
        "robot_id": trip.robot_id,
        "user_id": trip.user_id,
        "request_id": trip.request_id,
    }


WS_FILTER_KEYS: tuple[str, ...] = ("trip_id", "store_id", "robot_id", "user_id", "request_id")
"""Query keys of ``/ws`` (each may repeat)."""


def event_matches(event: EventBase, filters: Mapping[str, Collection[str] | str | None]) -> bool:
    """THE /ws filter rule: for every key in WS_FILTER_KEYS with values, the event's envelope field must be one of them
    (OR within a key, AND across keys; null never matches). No filters = everything."""
    for key in WS_FILTER_KEYS:
        wanted = filters.get(key)
        if not wanted:
            continue
        values = {wanted} if isinstance(wanted, str) else set(wanted)
        if getattr(event, key, None) not in values:
            return False
    return True


def with_seq(event: Event, seq: int) -> Event:
    """Copy of ``event`` with its envelope ``seq`` set (used by the publisher)."""
    return event.model_copy(update={"seq": seq})


def parse_event(raw: str | bytes | dict[str, Any]) -> Event:
    if isinstance(raw, dict):
        return EVENT_ADAPTER.validate_python(raw)
    return EVENT_ADAPTER.validate_json(raw)


def event_json_schema() -> dict[str, Any]:
    """JSON Schema of the Event union (for json-schema-to-typescript / `make gen-types`)."""
    return EVENT_ADAPTER.json_schema(mode="serialization")
