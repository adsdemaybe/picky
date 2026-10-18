"""The Trip aggregate: one shopping trip moving through the gateway state machine (TripState).

Defined here (not in shopping.py) because it aggregates routing + trust models; ``contracts.shopping.Trip`` and
``contracts.Trip`` both resolve to this class. The gateway owns every Trip: arms read it, and change it only through
``services.trips`` (``contracts.services.TripController``).
"""

from pydantic import Field, computed_field

from contracts.common import GoalId, Model, RequestId, RobotId, StopId, StoreId, Timestamp, TripId, UserId, utcnow
from contracts.messaging import OutboundMessage
from contracts.routing import Route, StopStatus
from contracts.shopping import Cart, Intent, Recommendation, ShoppingList, TripState
from contracts.trust import ApprovalRequest, ApprovalResponse, MandateDecision, Receipt


class TripTransition(Model):
    from_state: TripState | None = None
    to_state: TripState
    reason: str = ""
    ts: Timestamp = Field(default_factory=utcnow)


class Trip(Model):
    trip_id: TripId
    user_id: UserId = Field(description="Primary shopper (owner of the mandate and payment).")
    user_ids: list[UserId] = Field(default_factory=list, description="All shoppers on a batched trip (incl. owner).")
    store_id: StoreId
    state: TripState = TripState.INTENT
    intent: Intent | None = None
    shopping_list: ShoppingList | None = None
    recs: list[Recommendation] = Field(default_factory=list, description="Detour + near-expiry recs on the route.")
    checkout_recs: list[Recommendation] = Field(default_factory=list, description="Batched 'want any of these?'.")
    route: Route | None = None
    current_stop_id: StopId | None = None
    stop_status: dict[StopId, StopStatus] = Field(
        default_factory=dict,
        description="Source of truth for stop progress across re-plans (done/declined stops stay here).",
    )
    cart: Cart
    approvals: list[ApprovalRequest] = Field(default_factory=list)
    approval_responses: list[ApprovalResponse] = Field(default_factory=list)
    mandate_decision: MandateDecision | None = None
    receipt: Receipt | None = None
    followup: OutboundMessage | None = Field(
        None, description="The post-purchase question sent ('How was the salsa?'); about_skus = what it asks about."
    )
    robot_logged_in: bool = False
    auto_execute: bool = Field(
        False,
        description="Created from an NL request (CreateTripRequest.auto_execute): no login step; the trip goes "
        "ROUTE_PLANNED -> EXECUTING by itself and the fleet runs its shop goal.",
    )
    robot_id: RobotId | None = Field(None, description="Robot executing the trip (TripController.assign_robot).")
    goal_id: GoalId | None = Field(None, description="The fleet's shop Goal that executes this trip in store.")
    request_id: RequestId | None = Field(None, description="NL request (POST /api/requests) that created the trip.")
    history: list[TripTransition] = Field(default_factory=list)
    error: str | None = None
    created_at: Timestamp = Field(default_factory=utcnow)
    updated_at: Timestamp = Field(default_factory=utcnow)

    def pending_approvals(self) -> list[ApprovalRequest]:
        resolved = {r.approval_id for r in self.approval_responses}
        return [a for a in self.approvals if a.approval_id not in resolved]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def budget_cents(self) -> int | None:
        return self.shopping_list.budget_cents if self.shopping_list else None
