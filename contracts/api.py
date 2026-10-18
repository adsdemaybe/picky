"""Gateway HTTP/WS request + response models that are not already domain models. See contracts/api.md."""

from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter

from contracts.common import Channel, LineId, Model, RequestId, RobotId, StoreId, TripId, UserId
from contracts.fleet import GoalRequest, GoalSpec
from contracts.shopping import Cart, ShoppingList, TripState
from contracts.tools import AgentReply
from contracts.trust import ApprovalRequest, ApprovalResponse, MandateDecision, Receipt


class HealthStatus(Model):
    status: Literal["ok", "degraded"] = "ok"
    contracts_version: str
    env: str = "dev"
    real_arms: list[str] = Field(default_factory=list)
    sim_connected: bool = False
    robots: int = Field(0, description="Fleet robots known (data/fixtures/robots.yaml).")
    details: dict[str, str] = Field(default_factory=dict, description="Per-arm notes, e.g. {'trust': 'stripe_test'}.")


class CreateTripRequest(Model):
    """POST /api/trips: start planning from an intent ('tacos for 6, under $40, one vegetarian')."""

    user_id: UserId
    text: str
    channel: Channel = "web"
    store_id: StoreId | None = None
    user_ids: list[UserId] = Field(default_factory=list, description="Extra shoppers for a batched trip.")
    shopping_list: ShoppingList | None = Field(
        None, description="Pre-expanded list (the fleet's per-store split): skips agent.expand_intent."
    )
    request_id: RequestId | None = Field(None, description="NL request that created this trip (fleet).")
    robot_id: RobotId | None = Field(None, description="Robot to execute it (None = the fleet assigns).")
    auto_execute: bool = Field(
        False,
        description="True (the fleet sets it for request / shop-goal trips): no login step; robot_logged_in=True and "
        "the trip goes ROUTE_PLANNED -> EXECUTING -> fleet.execute_trip(trip_id, robot_id) by itself. False (manual "
        "POST /api/trips): waits in AWAITING_LOGIN for POST /api/trips/{id}/login.",
    )


class RobotLoginRequest(Model):
    """POST /api/trips/{id}/login: shopper logs in to a robot in-store; resumes the trip (state -> executing)."""

    user_id: UserId
    robot_id: RobotId | None = Field(None, description="The robot the shopper is at; None = the fleet picks one.")


class PostMessageRequest(Model):
    """POST /api/trips/{id}/messages: chat/voice text to the agent."""

    text: str
    channel: Channel = "web"
    user_id: UserId | None = None


class ApprovalDecisionRequest(Model):
    """POST /api/approvals/{id}: answer an approval from the web UI."""

    decision: Literal["approve", "reject", "choice"]
    choice: str | None = None
    choices: list[str] = Field(default_factory=list, description="Multi-select answers (request.multi=True).")
    cart_hash: str | None = Field(None, description="Required when approving a checkout approval (echo it).")
    user_id: UserId | None = None
    channel: Channel = "web"
    note: str | None = Field(None, description="Optional free text with the answer (-> ApprovalResponse.note).")


class CheckoutStatus(Model):
    """Response of POST /api/trips/{id}/checkout and TripController.start_checkout."""

    trip_id: TripId
    state: TripState
    cart_hash: str
    total_cents: int = Field(ge=0)
    decision: MandateDecision | None = None
    approval: ApprovalRequest | None = Field(None, description="The pending checkout approval, if any.")
    receipt: Receipt | None = None


class AddLineResult(Model):
    """Result of TripController.add_line (and the add_to_cart tool output)."""

    added: bool = Field(description="True = the line is in the cart now (cart.updated was published).")
    line_id: LineId | None = None
    decision: MandateDecision = Field(
        description="approve -> added; needs_approval -> NOT added (ask, then retry with approval_id); deny -> never."
    )
    cart: Cart
    approval_id: str | None = Field(None, description="Approval that unlocked a needs_approval add, if any.")


class VoiceTurnResponse(Model):
    """POST /api/trips/{id}/voice (audio body: audio/webm|wav|mpeg): STT -> agent -> TTS (Deepgram)."""

    transcript: str
    reply: AgentReply
    tts_b64: str | None = Field(None, description="Base64 audio of reply.text; None -> browser speechSynthesis.")
    tts_mime: str | None = Field(None, description="e.g. 'audio/mpeg'.")


# --------------------------------------------------------------------------------------------------------------------
# Fleet / cameras REST bodies (responses are domain models: fleet.*, cameras.*, catalog.*)
# --------------------------------------------------------------------------------------------------------------------
class RobotMessageRequest(Model):
    """POST /api/fleet/robots/{robot_id}/messages: talk to a robot's brain ('also grab limes', 'status?')."""

    text: str = Field(min_length=1)
    user_id: UserId | None = None
    channel: Channel = "web"


class StopRobotRequest(Model):
    """POST /api/fleet/robots/{robot_id}/stop (body optional): halt now and cancel its active goal."""

    reason: str = "stopped from the web app"


class CancelGoalRequest(Model):
    """POST /api/goals/{goal_id}/cancel (body optional)."""

    reason: str = "cancelled from the web app"


class CreateGoalRequest(GoalRequest):
    """POST /api/fleet/robots/{robot_id}/goals: a manual goal for THAT robot (no store_id / robot_id needed; the path
    pins the robot and its store). POST /api/goals takes a full ``fleet.GoalSpec`` instead."""

    def to_spec(self, robot_id: RobotId, user_id: UserId | None = None) -> GoalSpec:
        """The GoalSpec the gateway passes to fleet.create_goal (path robot wins; caller fills a missing user_id)."""
        data = self.model_dump(exclude_unset=True)
        data["robot_id"] = robot_id
        if data.get("user_id") is None and user_id is not None:
            data["user_id"] = user_id
        return GoalSpec.model_validate(data)


class ObserveRequest(Model):
    """POST /api/cameras/{camera_id}/observe: snapshot -> blur -> vision -> cameras.VisionObservation."""

    question: str | None = Field(None, description="What to look for; None = describe the scene.")


# --------------------------------------------------------------------------------------------------------------------
# WebSocket client -> server messages on /ws  (server -> client messages are contracts.events.Event)
# --------------------------------------------------------------------------------------------------------------------
class WsChat(Model):
    """Chat text to the shopper agent of a trip (= POST /api/trips/{id}/messages); also the Web Speech fallback for
    voice (browser STT, channel='voice'). ``trip_id`` None = the socket's single trip_id filter (error if not exactly
    one). New natural-language requests go to POST /api/requests, not here."""

    type: Literal["chat"] = "chat"
    text: str
    channel: Channel = "web"
    trip_id: TripId | None = None


class WsApproval(Model):
    """Answer any approval, incl. pick_choice (decision='choice', choice=<option_id = instance_id>)."""

    type: Literal["approval"] = "approval"
    response: ApprovalResponse


class WsRobotChat(Model):
    """Talk to a robot's brain over the socket (= POST /api/fleet/robots/{id}/messages); replies arrive as brain.step
    events (kind='message')."""

    type: Literal["robot_chat"] = "robot_chat"
    robot_id: RobotId
    text: str
    user_id: UserId | None = None


class WsPing(Model):
    type: Literal["ping"] = "ping"


ClientMessage = Annotated[WsChat | WsApproval | WsRobotChat | WsPing, Field(discriminator="type")]
CLIENT_MESSAGE_ADAPTER: TypeAdapter[Any] = TypeAdapter(ClientMessage)


def parse_client_message(raw: str | bytes | dict[str, Any]) -> ClientMessage:
    if isinstance(raw, dict):
        return CLIENT_MESSAGE_ADAPTER.validate_python(raw)
    return CLIENT_MESSAGE_ADAPTER.validate_json(raw)
