"""PhotonMessaging: real MessagingService on Photon 2.0 (beta shared iMessage line) via ``photonhq-api`` 0.5.0.

Install: ``uv sync --extra messaging`` (``photonhq-api==0.5.0``, ``import photon_api``). Env: ``PHOTON_API_KEY``
(project key ``pho_sk_...``), ``PHOTON_PROJECT_ID`` (``pho_prj_...``).

How it works (docs/research.md §9.9):
- ``start()`` spawns a poll task: every ``poll_interval_s`` (~2 s) ``projects.list_messages`` with
  ``direction=received`` + ``createdSince=<cursor>``; dedupe on Photon message id (delivery is at-least-once).
- Each received text -> ``InboundMessage(channel='photon', handle=E.164, user_id=recommender.find_user_by_handle)``
  -> the ``on_inbound`` handler (gateway).
- The shared line can't text first: a user must have texted the line. We learn ``handle -> spaceId`` (and our own
  agent user id, ``receivedBy.id``, used as ``senderId``) from their inbound messages.
- ``send()`` -> ``projects.send_message`` with an ``Idempotency-Key`` (= approval_id for approvals, else message id),
  which also makes the SDK retry it safely.

TODO (maps to CLAUDE.md §3 messaging + §6 demo-ready):
  1. [P0] Verify list_messages/send_message payload shapes against the live beta API (Terminal echo test).
  2. [P0] Text-in intent before the trip: unknown sender + no active trip -> gateway treats text as an Intent.
  3. [P0] YES/NO approvals mid-trip: correlate replies to the latest pending approval (agent.interpret_reply).
  4. [P0] Post-purchase follow-up ("how was the salsa?") + feedback reply -> recommender.record_feedback.
  5. [P1] Persist the dedupe set / cursor + handle->space map (Postgres) so restarts don't replay or forget.
  6. [P1] Account linking ("link ABC123") for handles not in the fixtures.
  7. [P2] spectrum-ts sidecar (polls, tapbacks, persistent connection); Telegram fallback channel.
"""

import asyncio
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

from arms.messaging.directory import HandleDirectory
from arms.messaging.format import approval_quick_replies, format_approval, mask_handle
from contracts.common import Channel, UserId
from contracts.events import ErrorEvent
from contracts.messaging import InboundMessage, OutboundMessage, SendResult, normalize_handle
from contracts.services import InboundHandler, Services
from contracts.trust import ApprovalRequest
from utils.config import get_settings
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)

DEDUPE_MAX = 5000


class MessagingNotConfigured(RuntimeError):
    """photonhq-api not installed, or PHOTON_API_KEY / PHOTON_PROJECT_ID missing."""


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dump(obj: Any) -> dict[str, Any]:
    """SDK model (RootModel or BaseModel) or dict -> plain JSON dict (robust to SDK drift)."""
    if isinstance(obj, dict):
        return obj
    dumped = obj.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return dumped if isinstance(dumped, dict) else {}


def parse_photon_message(raw: Any) -> dict[str, Any] | None:
    """Photon ``Message`` -> {id, text, handle, space_id, agent_id, created_at}; None for non-text / non-received."""
    d = _dump(raw)
    if d.get("status") not in (None, "received"):
        return None
    content = d.get("content") or {}
    if content.get("type") != "text" or not str(content.get("text", "")).strip():
        return None  # attachments, typing, reactions, polls, ... -> ignored (default branch)
    sent_by = d.get("sentBy") or {}
    space = d.get("space") or {}
    received_by = d.get("receivedBy") or {}
    agent_id = received_by.get("id")
    if not agent_id:  # direct space: the participant that isn't the sender is us
        others = [p for p in space.get("participantIds") or [] if p != sent_by.get("id")]
        agent_id = others[0] if len(others) == 1 else None
    return {
        "id": d.get("id"),
        "text": str(content["text"]).strip(),
        "handle": sent_by.get("handle"),
        "space_id": space.get("id"),
        "agent_id": agent_id,
        "created_at": d.get("createdAt"),
        "parent_id": d.get("parentMessageId"),
    }


class PhotonMessaging:
    """Satisfies ``contracts.services.MessagingService``."""

    channel: Channel = "photon"

    def __init__(
        self,
        services: Services,
        *,
        api_key: str | None = None,
        project_id: str | None = None,
        poll_interval_s: float = 2.0,
        client: Any = None,
    ) -> None:
        s = get_settings()
        self._services = services
        self._api_key = api_key if api_key is not None else s.photon_api_key
        self._project_id = project_id if project_id is not None else s.photon_project_id
        self._poll_interval_s = poll_interval_s
        self._client = client  # AsyncPhoton (injected in tests)
        self._own_client = client is None
        self._dir = HandleDirectory(services)
        self._handler: InboundHandler | None = None
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._space_by_handle: dict[str, str] = {}
        self._agent_id: str | None = None
        self._cursor: str = _iso(datetime.now(UTC) - timedelta(seconds=5))
        self._task: asyncio.Task[None] | None = None

    # ---------------------------------------------------------------- Protocol
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._ensure_client()
        self._task = asyncio.get_running_loop().create_task(self._poll_loop())
        log.info("photon messaging polling every %.1fs (project %s)", self._poll_interval_s, self._project_id)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._client is not None and self._own_client:
            try:
                await self._client.close()
            except Exception as exc:  # noqa: BLE001
                log.debug("closing photon client: %s", exc)
            self._client = None

    def on_inbound(self, handler: InboundHandler) -> None:
        self._handler = handler

    async def send(self, message: OutboundMessage) -> SendResult:
        msg_id = message.message_id or new_id("msg")
        handle = message.to_handle or await self._dir.handle_for_user(message.user_id)
        if not handle:
            return SendResult(ok=False, message_id=msg_id, channel=self.channel, error="no_handle")
        space_id = self._space_by_handle.get(normalize_handle(handle))
        if space_id is None or self._agent_id is None:
            # Shared line: can't text first. The user must text the line (e.g. "ping") before we can reply.
            log.warning("photon send to %s skipped: user hasn't texted the line yet", mask_handle(handle))
            return SendResult(ok=False, message_id=msg_id, channel=self.channel, error="no_space_user_must_text_first")
        try:
            client = self._ensure_client()
            from photon_api.rpc_generated import SendMessageInput

            req = SendMessageInput.model_validate(
                {
                    "path": {"id": self._project_id},
                    "headers": {"idempotency-key": message.approval_id or msg_id},
                    "body": {
                        "content": {"type": "text", "format": "plain", "text": message.text},
                        "senderId": self._agent_id,
                        "spaceId": space_id,
                    },
                }
            )
            sent = await client.projects.send_message(req)
            ref = _dump(sent).get("id")
            log.info("photon -> %s: %s", mask_handle(handle), message.text.replace("\n", " | "))
            return SendResult(ok=True, message_id=msg_id, channel=self.channel, provider_ref=ref)
        except Exception as exc:  # noqa: BLE001 - not configured / ApiError / TransportError / validation: degrade, never raise
            log.warning("photon send failed: %s", exc)
            await self._emit_error(message.trip_id, f"photon send failed: {exc}", code="photon_send_failed")
            return SendResult(ok=False, message_id=msg_id, channel=self.channel, error=str(exc))

    async def send_approval_request(self, request: ApprovalRequest) -> SendResult:
        return await self.send(
            OutboundMessage(
                channel=self.channel,
                user_id=request.user_id,
                text=format_approval(request),
                trip_id=request.trip_id,
                approval_id=request.approval_id,
                quick_replies=approval_quick_replies(request),
            )
        )

    # ---------------------------------------------------------------- polling
    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self._api_key or not self._project_id:
            raise MessagingNotConfigured("set PHOTON_API_KEY and PHOTON_PROJECT_ID (or run the messaging fake)")
        try:
            from photon_api import AsyncPhoton
        except ImportError as exc:
            raise MessagingNotConfigured("photonhq-api not installed: uv sync --extra messaging") from exc
        self._client = AsyncPhoton(headers={"Authorization": f"Bearer {self._api_key}"})
        self._own_client = True
        return self._client

    async def _poll_loop(self) -> None:
        backoff = self._poll_interval_s
        while True:
            try:
                await self.poll_once()
                backoff = self._poll_interval_s
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep polling; Photon beta is flaky
                log.warning("photon poll failed: %s", exc)
                backoff = min(backoff * 2, 30.0)
            await asyncio.sleep(backoff)

    async def poll_once(self) -> list[InboundMessage]:
        """One poll: fetch received messages since the cursor, dedupe, deliver. Returns what was delivered."""
        from photon_api.rpc_generated import ListMessagesInput

        client = self._ensure_client()
        query: dict[str, Any] = {"direction": "received", "createdSince": self._cursor, "order": "asc"}
        delivered: list[InboundMessage] = []
        page_token: str | None = None
        while True:
            if page_token:
                query["pageToken"] = page_token
            page = _dump(
                await client.projects.list_messages(
                    ListMessagesInput.model_validate({"path": {"id": self._project_id}, "query": query})
                )
            )
            for raw in page.get("items") or []:
                msg = await self.handle_raw(raw)
                if msg is not None:
                    delivered.append(msg)
            page_token = page.get("nextPageToken")
            if not page_token:
                break
        return delivered

    async def handle_raw(self, raw: Any) -> InboundMessage | None:
        """Normalize + dedupe one Photon message and hand it to the inbound handler."""
        p = parse_photon_message(raw)
        if p is None or not p["id"] or p["id"] in self._seen:
            return None
        self._seen[p["id"]] = None
        while len(self._seen) > DEDUPE_MAX:
            self._seen.popitem(last=False)
        if p["created_at"] and p["created_at"] > self._cursor:
            self._cursor = p["created_at"]
        handle = normalize_handle(p["handle"]) if p["handle"] else None
        if handle and p["space_id"]:
            self._space_by_handle[handle] = p["space_id"]
        if p["agent_id"]:
            self._agent_id = p["agent_id"]
        user_id: UserId | None = await self._dir.user_for_handle(handle)
        msg = InboundMessage(message_id=p["id"], channel=self.channel, handle=handle, user_id=user_id, text=p["text"])
        log.info("photon <- %s (user=%s): %s", mask_handle(handle), user_id, p["text"])
        if self._handler is not None:
            try:
                await self._handler(msg)
            except Exception as exc:  # noqa: BLE001
                log.exception("inbound handler failed: %s", exc)
        return msg

    async def _emit_error(self, trip_id: str | None, message: str, code: str) -> None:
        events = self._services.events
        if events is None:
            return
        try:
            await events.publish(
                ErrorEvent(trip_id=trip_id, source="messaging", arm="messaging", message=message, code=code)
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("error event publish failed: %s", exc)
