"""Messaging contracts: inbound/outbound messages across Photon (iMessage), web chat and voice.

Handles: ``normalize_handle`` is THE normalization (E.164 for phones, lowercase for emails). Messaging resolves
``InboundMessage.user_id`` with ``services.recommender.find_user_by_handle(normalize_handle(handle))`` before handing
the message to the gateway; the gateway maps user_id -> the user's active trip for ``trip_id``.
"""

import re
from typing import Literal

from pydantic import Field

from contracts.common import (
    ApprovalId,
    Channel,
    MessageId,
    Model,
    Rating,
    Score01,
    Sku,
    Timestamp,
    TripId,
    UserId,
    utcnow,
)

ReplyKind = Literal["approval", "feedback", "intent", "chat"]


def normalize_handle(handle: str) -> str:
    """'(555) 555-0100' -> '+15555550100'; '+44 20 7946 0958' -> '+442079460958'; emails lowercased.

    10 bare digits are assumed US (+1). Anything else is returned stripped.
    """
    h = handle.strip()
    if "@" in h:
        return h.lower()
    digits = re.sub(r"\D", "", h)
    if not digits:
        return h
    if len(digits) == 10 and not h.startswith("+"):
        return "+1" + digits
    return "+" + digits


class InboundMessage(Model):
    message_id: MessageId
    channel: Channel
    handle: str | None = Field(None, description="Sender handle (phone/email for Photon), as received.")
    user_id: UserId | None = Field(None, description="Resolved by messaging via recommender.find_user_by_handle.")
    text: str
    trip_id: TripId | None = Field(None, description="Filled by the gateway (user's active trip).")
    in_reply_to: MessageId | None = None
    ts: Timestamp = Field(default_factory=utcnow)


class OutboundMessage(Model):
    message_id: MessageId | None = Field(None, description="Assigned by the messaging service if None.")
    channel: Channel
    user_id: UserId | None = None
    to_handle: str | None = Field(None, description="Explicit destination; else resolved from the user's profile.")
    text: str
    trip_id: TripId | None = None
    approval_id: ApprovalId | None = Field(None, description="Set when this message asks for an approval.")
    about_skus: list[Sku] = Field(
        default_factory=list, description="Products the message asks about (follow-ups: binds feedback to a SKU)."
    )
    quick_replies: list[str] = Field(default_factory=list, description="e.g. ['YES', 'NO'].")
    in_reply_to: MessageId | None = None
    ts: Timestamp = Field(default_factory=utcnow)


class SendResult(Model):
    ok: bool
    message_id: MessageId | None = None
    channel: Channel | None = None
    provider_ref: str | None = None
    error: str | None = None


class ReplyInterpretation(Model):
    """What an inbound reply means (from AgentService.interpret_reply)."""

    kind: ReplyKind
    decision: Literal["approve", "reject"] | None = None
    approval_id: ApprovalId | None = Field(None, description="Which pending approval the reply answers.")
    choice: str | None = Field(None, description="option_id for pick_choice approvals ('2' -> second candidate).")
    choices: list[str] = Field(default_factory=list, description="Multi-select answers ('1 and 3').")
    rating: Rating | None = None
    sku: Sku | None = Field(None, description="Feedback target (from trip.followup.about_skus / receipt lines).")
    sentiment: float | None = Field(None, ge=-1.0, le=1.0)
    feedback_tags: list[str] = Field(default_factory=list, description="e.g. ['too_mild'].")
    intent_text: str | None = Field(None, description="Normalized intent when kind='intent'.")
    reply_text: str | None = Field(None, description="Suggested reply to send back.")
    confidence: Score01 = 1.0
