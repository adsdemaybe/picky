"""FakeMessaging: in-process console channel (deterministic, base deps only).

- ``send()`` logs the text (handle masked), keeps it in ``transcript`` and publishes an ``agent.message`` event
  (``role='assistant'``, ``channel='photon'``, ``source='messaging'``) so the UI shows the "phone" conversation.
- ``send_approval_request()`` texts the approval, then after ``reply_delay_s`` the scripted shopper answers:
  recommendation -> NO if ``request.payload['over_budget']`` (the gateway flags detours that bust the trip budget),
  else YES if ``interest >= interest_threshold`` (read from ``request.payload['interest']``; missing -> YES) else NO;
  over_threshold -> NO if ``'over_budget'`` is in ``request.payload['reasons']``, else YES; checkout / substitution /
  online_order -> YES; pick_choice -> "1"; multi-select checkout batch -> "1".
- A follow-up (outbound with ``about_skus``, i.e. after checkout) gets the scripted feedback reply
  once per trip: the demo-script line "too mild, loved the hot sauce though" the first time, then
  "perfect heat this time, 5/5", so the fake pipeline exercises trip-2 learning.

Replies are delivered like real texts: an ``InboundMessage`` to the ``on_inbound`` handler (gateway -> agent
.interpret_reply -> approvals.resolve). With ``reply_mode='auto'`` (default) and no handler registered, approval
replies resolve ``services.approvals`` directly with an ``ApprovalResponse(channel='photon')``.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from arms.messaging.directory import HandleDirectory
from arms.messaging.format import (
    approval_quick_replies,
    format_approval,
    mask_handle,
    option_id_for_reply,
    parse_numbers,
    parse_yes_no,
)
from contracts.common import Channel, TripId, UserId
from contracts.events import AgentMessage
from contracts.messaging import InboundMessage, OutboundMessage, SendResult
from contracts.services import InboundHandler, Services
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse
from utils.ids import IdFactory
from utils.log import get_logger

log = get_logger(__name__)

ReplyMode = Literal["auto", "inbound", "resolve", "off"]
DEFAULT_FEEDBACK_TEXT = "too mild, loved the hot sauce though"
LATER_FEEDBACK_TEXT = "perfect heat this time, 5/5"
"""Scripted answer to every follow-up after the first (trip 2 got it right)."""


@dataclass
class TranscriptEntry:
    direction: Literal["out", "in"]
    message_id: str
    text: str
    user_id: UserId | None
    handle: str | None
    trip_id: TripId | None
    approval_id: str | None = None
    about_skus: list[str] = field(default_factory=list)


def scripted_reply(request: ApprovalRequest, interest_threshold: float = 0.6) -> str:
    """The scripted shopper's text answer to an approval (deterministic)."""
    if request.options:
        return "1"  # pick_choice: freshest candidate; checkout batch: first suggestion
    if request.kind == ApprovalKind.RECOMMENDATION:
        if request.payload.get("over_budget"):
            return "NO"  # she sticks to her budget
        interest = request.payload.get("interest")
        if interest is None:
            return "YES"
        return "YES" if float(interest) >= interest_threshold else "NO"
    if request.kind == ApprovalKind.OVER_THRESHOLD and "over_budget" in request.payload.get("reasons", []):
        return "NO"  # above her ask-me limit is fine, over the trip budget is not
    return "YES"  # checkout, over_threshold, substitution, online_order


def reply_to_response(request: ApprovalRequest, text: str, user_id: UserId | None) -> ApprovalResponse:
    """Deterministic text -> ApprovalResponse (used only when there is no inbound handler / agent in the loop)."""
    if request.options:
        nums = parse_numbers(text)
        ids = [oid for n in nums if (oid := option_id_for_reply(request, n)) is not None]
        if ids:
            if request.multi:
                return ApprovalResponse(
                    approval_id=request.approval_id, decision="choice", choices=ids, channel="photon", user_id=user_id
                )
            return ApprovalResponse(
                approval_id=request.approval_id, decision="choice", choice=ids[0], channel="photon", user_id=user_id
            )
        return ApprovalResponse(approval_id=request.approval_id, decision="reject", channel="photon", user_id=user_id)
    yes = parse_yes_no(text)
    return ApprovalResponse(
        approval_id=request.approval_id,
        decision="approve" if yes else "reject",
        channel="photon",
        user_id=user_id,
        cart_hash=request.cart_hash,
    )


class FakeMessaging:
    """Console messaging channel. Satisfies ``contracts.services.MessagingService``."""

    channel: Channel = "photon"

    def __init__(
        self,
        services: Services,
        *,
        reply_delay_s: float = 0.3,
        reply_mode: ReplyMode = "auto",
        interest_threshold: float = 0.6,
        feedback_text: str | None = DEFAULT_FEEDBACK_TEXT,
        later_feedback_text: str | None = LATER_FEEDBACK_TEXT,
        policy: Callable[[ApprovalRequest], str] | None = None,
        emit_events: bool = True,
        seed: int = 7,
    ) -> None:
        self._services = services
        self._dir = HandleDirectory(services)
        self._ids = IdFactory(seed)
        self._handler: InboundHandler | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._feedback_sent: set[str] = set()
        self._running = False
        self.reply_delay_s = reply_delay_s
        self.reply_mode: ReplyMode = reply_mode
        self.interest_threshold = interest_threshold
        self.feedback_text = feedback_text
        self.later_feedback_text = later_feedback_text
        self.policy = policy
        self.emit_events = emit_events
        self.transcript: list[TranscriptEntry] = []

    # ---------------------------------------------------------------- Protocol
    async def start(self) -> None:
        self._running = True
        log.info("fake messaging (console channel) started")

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()

    def on_inbound(self, handler: InboundHandler) -> None:
        self._handler = handler

    async def send(self, message: OutboundMessage) -> SendResult:
        msg_id = message.message_id or self._ids("msg")
        handle = message.to_handle or await self._dir.handle_for_user(message.user_id)
        log.info(
            "[photon-fake] -> %s (%s) trip=%s: %s",
            message.user_id or "?",
            mask_handle(handle),
            message.trip_id,
            message.text.replace("\n", " | "),
        )
        self.transcript.append(
            TranscriptEntry(
                direction="out",
                message_id=msg_id,
                text=message.text,
                user_id=message.user_id,
                handle=handle,
                trip_id=message.trip_id,
                approval_id=message.approval_id,
                about_skus=list(message.about_skus),
            )
        )
        await self._publish(message.trip_id, msg_id, message.text, role="assistant")
        if message.about_skus and self.feedback_text and self.reply_mode != "off":
            key = message.trip_id or msg_id
            if key not in self._feedback_sent:
                first = not self._feedback_sent
                self._feedback_sent.add(key)
                text = self.feedback_text if first or not self.later_feedback_text else self.later_feedback_text
                self._spawn(self._reply_inbound(message, msg_id, text))
        return SendResult(ok=True, message_id=msg_id, channel=self.channel, provider_ref=f"console:{msg_id}")

    async def send_approval_request(self, request: ApprovalRequest) -> SendResult:
        out = OutboundMessage(
            channel=self.channel,
            user_id=request.user_id,
            text=format_approval(request),
            trip_id=request.trip_id,
            approval_id=request.approval_id,
            quick_replies=approval_quick_replies(request),
        )
        result = await self.send(out)
        if self.reply_mode != "off" and result.message_id:
            text = self.policy(request) if self.policy else scripted_reply(request, self.interest_threshold)
            self._spawn(self._answer_approval(request, out, result.message_id, text))
        return result

    # ---------------------------------------------------------------- dev / test helpers
    async def inject(
        self, text: str, *, handle: str | None = None, user_id: UserId | None = None, in_reply_to: str | None = None
    ) -> InboundMessage:
        """Simulate the shopper texting in (like POST /api/dev/inbound). Delivered to the handler if one is set."""
        if user_id is None:
            user_id = await self._dir.user_for_handle(handle)
        if handle is None:
            handle = await self._dir.handle_for_user(user_id)
        msg = InboundMessage(
            message_id=self._ids("in"),
            channel=self.channel,
            handle=handle,
            user_id=user_id,
            text=text,
            in_reply_to=in_reply_to,
        )
        await self._deliver(msg, trip_id=None)
        return msg

    async def wait_idle(self) -> None:
        """Await all scheduled scripted replies (tests; wrap in ``asyncio.timeout`` if needed)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    # ---------------------------------------------------------------- internals
    def _spawn(self, coro) -> None:  # type: ignore[no-untyped-def]
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish(self, trip_id: TripId | None, msg_id: str, text: str, role: str) -> None:
        events = self._services.events
        if not self.emit_events or events is None:
            return
        try:
            await events.publish(
                AgentMessage(
                    trip_id=trip_id,
                    source="messaging",
                    message_id=msg_id,
                    role=role,  # type: ignore[arg-type]
                    text=text,
                    done=True,
                    channel=self.channel,
                )
            )
        except Exception as exc:  # noqa: BLE001 - never kill a trip over a UI event
            log.warning("publishing agent.message failed: %s", exc)

    async def _deliver(self, msg: InboundMessage, trip_id: TripId | None) -> bool:
        self.transcript.append(
            TranscriptEntry(
                direction="in",
                message_id=msg.message_id,
                text=msg.text,
                user_id=msg.user_id,
                handle=msg.handle,
                trip_id=trip_id,
            )
        )
        log.info("[photon-fake] <- %s (%s): %s", msg.user_id or "?", mask_handle(msg.handle), msg.text)
        await self._publish(trip_id, msg.message_id, msg.text, role="user")
        if self._handler is None:
            return False
        try:
            await self._handler(msg)
        except Exception as exc:  # noqa: BLE001 - a bad handler must not kill the channel
            log.exception("inbound handler failed: %s", exc)
        return True

    async def _reply_inbound(self, out: OutboundMessage, out_id: str, text: str) -> None:
        """Scripted shopper texts back ``text`` in reply to outbound ``out_id`` (after ``reply_delay_s``)."""
        await asyncio.sleep(self.reply_delay_s)
        handle = out.to_handle or await self._dir.handle_for_user(out.user_id)
        msg = InboundMessage(
            message_id=self._ids("in"),
            channel=self.channel,
            handle=handle,
            user_id=out.user_id,
            text=text,
            in_reply_to=out_id,
        )
        await self._deliver(msg, trip_id=out.trip_id)

    async def _answer_approval(self, request: ApprovalRequest, out: OutboundMessage, out_id: str, text: str) -> None:
        if self.reply_mode in ("auto", "inbound") and self._handler is not None:
            await self._reply_inbound(out, out_id, text)
            return
        await asyncio.sleep(self.reply_delay_s)
        if self.reply_mode == "inbound":
            log.info("[photon-fake] no inbound handler; reply %r for %s dropped", text, request.approval_id)
            return
        # resolve directly (no agent in the loop)
        self.transcript.append(
            TranscriptEntry(
                direction="in",
                message_id=self._ids("in"),
                text=text,
                user_id=request.user_id,
                handle=None,
                trip_id=request.trip_id,
                approval_id=request.approval_id,
            )
        )
        await self._publish(request.trip_id, self.transcript[-1].message_id, text, role="user")
        approvals = self._services.approvals
        if approvals is None:
            log.info("[photon-fake] no approvals broker; reply %r for %s not resolved", text, request.approval_id)
            return
        try:
            ok = await approvals.resolve(reply_to_response(request, text, request.user_id))
            log.info("[photon-fake] resolved %s with %r (accepted=%s)", request.approval_id, text, ok)
        except Exception as exc:  # noqa: BLE001
            log.warning("approvals.resolve failed: %s", exc)
