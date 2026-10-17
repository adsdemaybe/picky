"""User-visible events: gateway bus -> websocket ``/ws?trip_id=`` -> frontend.

Every event is a JSON object discriminated on ``type`` and carries the envelope fields ``trip_id``, ``ts`` and
``seq``. Producers build events with ``seq=0``; the gateway's EventPublisher stamps ``seq`` strictly increasing
ACROSS THE WHOLE BUS (gaps are normal when a client filters by trip; clients dedupe by seq and resume with
``after_seq``). TS types: ``event_json_schema()`` -> json-schema-to-typescript.

Producers (``EVENT_PRODUCERS``): the gateway publishes every event except ``agent.message`` (the agent, for
streaming) and ``error`` (any arm). Nobody else publishes, so nothing is published twice.

Live-only events (``LIVE_ONLY_EVENT_TYPES`` = ``frame``) never go through the sequenced bus/replay ring: the gateway
sends them latest-only on ``/ws/frames?trip_id=`` with ``seq=0``. ``robot.state`` on ``/ws`` is throttled to <= 2 Hz
(plus every status change); full-rate robot.state also goes on ``/ws/frames``.
"""

from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, field_validator

from contracts.common import (
    Channel,
    Model,
    Pose2D,
    RecId,
    Sku,
    SlotId,
    StopId,
    StoreId,
    Timestamp,
    TripId,
    utcnow,
)
from contracts.messaging import OutboundMessage
from contracts.perception import BasketCheck, PickChoice, ScanReport
from contracts.profile import Feedback, ProfileUpdate
from contracts.routing import Route, StopStatus
from contracts.shopping import Cart, Intent, ListItem, Recommendation, ShoppingList, TripState
from contracts.sim import CameraFrame, RobotState
from contracts.trip import Trip
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse, MandateDecision, Receipt


class EventBase(Model):
    trip_id: TripId | None = Field(None, description="None only for store-level events (e.g. merchant scans).")
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
    stop_id: StopId
    slot_id: SlotId | None = None
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
    """Live camera frame for the frontend, sent ONLY on ``/ws/frames`` (latest-only, seq=0, never replayed).

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
    AgentMessage,
    ErrorEvent,
)

EVENT_TYPES: dict[str, type[EventBase]] = {cls.model_fields["type"].default: cls for cls in EVENT_CLASSES}
"""Map from ``type`` string to event class."""

LIVE_ONLY_EVENT_TYPES: frozenset[str] = frozenset({"frame"})
"""Never sequenced, stored or replayed; sent latest-only on /ws/frames."""

EVENT_PRODUCERS: dict[str, str] = {t: "gateway" for t in EVENT_TYPES} | {"agent.message": "agent", "error": "any"}
"""Who publishes each event type (see module docstring)."""


def make_event(type_: str, *, trip_id: TripId | None, source: str | None = None, **payload: Any) -> Event:
    """Build an event envelope by type name: ``make_event("route.planned", trip_id=t, route=r)``."""
    try:
        cls = EVENT_TYPES[type_]
    except KeyError as exc:
        raise ValueError(f"unknown event type {type_!r}") from exc
    return cls(trip_id=trip_id, source=source, **payload)  # type: ignore[return-value]


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
