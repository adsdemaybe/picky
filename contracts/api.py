"""Gateway HTTP/WS request + response models that are not already domain models. See contracts/api.md."""

from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter

from contracts.common import Channel, LineId, Model, StoreId, TripId, UserId
from contracts.shopping import Cart, TripState
from contracts.tools import AgentReply
from contracts.trust import ApprovalRequest, ApprovalResponse, MandateDecision, Receipt


class HealthStatus(Model):
    status: Literal["ok", "degraded"] = "ok"
    contracts_version: str
    env: str = "dev"
    real_arms: list[str] = Field(default_factory=list)
    sim_connected: bool = False
    details: dict[str, str] = Field(default_factory=dict, description="Per-arm notes, e.g. {'trust': 'stripe_test'}.")


class CreateTripRequest(Model):
    """POST /api/trips: start planning from an intent ('tacos for 6, under $40, one vegetarian')."""

    user_id: UserId
    text: str
    channel: Channel = "web"
    store_id: StoreId | None = None
    user_ids: list[UserId] = Field(default_factory=list, description="Extra shoppers for a batched trip.")


class RobotLoginRequest(Model):
    """POST /api/trips/{id}/login: shopper logs in to the robot in-store; resumes the trip."""

    user_id: UserId
    robot_id: str = "robot-1"


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
# WebSocket client -> server messages on /ws?trip_id=  (server -> client messages are contracts.events.Event)
# --------------------------------------------------------------------------------------------------------------------
class WsChat(Model):
    """Chat text; also the Web Speech fallback for voice (browser STT, channel='voice')."""

    type: Literal["chat"] = "chat"
    text: str
    channel: Channel = "web"


class WsApproval(Model):
    """Answer any approval, incl. pick_choice (decision='choice', choice=<option_id = instance_id>)."""

    type: Literal["approval"] = "approval"
    response: ApprovalResponse


class WsPing(Model):
    type: Literal["ping"] = "ping"


ClientMessage = Annotated[WsChat | WsApproval | WsPing, Field(discriminator="type")]
CLIENT_MESSAGE_ADAPTER: TypeAdapter[Any] = TypeAdapter(ClientMessage)


def parse_client_message(raw: str | bytes | dict[str, Any]) -> ClientMessage:
    if isinstance(raw, dict):
        return CLIENT_MESSAGE_ADAPTER.validate_python(raw)
    return CLIENT_MESSAGE_ADAPTER.validate_json(raw)
