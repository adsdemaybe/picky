"""Composition root: builds the ``Services`` container (real vs fake per arm) plus the gateway-owned pieces.

- ``build_registry()`` -> ``Registry``: for each arm in ``contracts.services.ARM_NAMES`` that provides a service,
  ``arms.<arm>.create(services)`` if ``settings.is_real(arm)`` else ``arms.<arm>.create_fake(services)``. A real arm
  that fails to construct falls back to its fake (logged + noted in ``/health``); a missing fake leaves the field None.
- Gateway-provided services: ``GatewayPublisher`` (EventPublisher over ``utils.events.EventBus``; ``frame`` events go
  to the live-only ``FrameHub``), ``GatewayApprovalBroker`` (asyncio Futures, resolvable from REST, websocket or
  messaging inbound; timeouts), ``SimLinkHub`` (the ``/ws/sim`` worker connection as a ``SimLink``) and the
  ``TripOrchestrator`` (TripController).

Factories never call other services at construction time, so the order here doesn't matter.
"""

import asyncio
import contextlib
import importlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from contracts.events import LIVE_ONLY_EVENT_TYPES, ApprovalRequested, ApprovalResolved, ErrorEvent, Event, parse_event
from contracts.services import ARM_NAMES, SERVICE_PROTOCOLS, Services
from contracts.sim import CommandResult, SimMessage, SimMessageHandler, default_timeout_s
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse
from utils.config import Settings, get_settings
from utils.events import EventBus, event_to_dict
from utils.log import get_logger

log = get_logger(__name__)

ApprovalListener = Callable[[str, ApprovalRequest, ApprovalResponse | None], Awaitable[None] | None]
"""``listener(phase, request, response)``; phase = 'requested' | 'resolved'."""


# ----------------------------------------------------------------------------------------------------------------------
# Live-only frames (/ws/frames)
# ----------------------------------------------------------------------------------------------------------------------
class FrameSubscription:
    """Latest-only queue for one ``/ws/frames`` client (drop-oldest, small)."""

    def __init__(self, hub: "FrameHub", trip_id: str | None, maxsize: int = 4) -> None:
        self._hub = hub
        self.trip_id = trip_id
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)

    def offer(self, event: Any) -> None:
        if self.trip_id is not None and getattr(event, "trip_id", None) not in (None, self.trip_id):
            return
        if self.queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        self.queue.put_nowait(event)

    async def get(self) -> Any:
        return await self.queue.get()

    def close(self) -> None:
        self._hub._subs.discard(self)


class FrameHub:
    """Fan-out for ``frame`` events and full-rate ``robot.state`` (never sequenced, never replayed, seq=0)."""

    def __init__(self) -> None:
        self._subs: set[FrameSubscription] = set()
        self.last: Any = None

    def subscribe(self, trip_id: str | None = None) -> FrameSubscription:
        sub = FrameSubscription(self, trip_id)
        self._subs.add(sub)
        return sub

    def publish(self, event: Any) -> None:
        self.last = event
        for sub in list(self._subs):
            sub.offer(event)


# ----------------------------------------------------------------------------------------------------------------------
# EventPublisher
# ----------------------------------------------------------------------------------------------------------------------
class GatewayPublisher:
    """``contracts.services.EventPublisher``: stamps bus-wide ``seq`` (utils.events) and fans out to websockets."""

    def __init__(self, bus: EventBus, frames: FrameHub) -> None:
        self.bus = bus
        self.frames = frames

    async def publish(self, event: Event) -> Event:
        if isinstance(event, dict):
            event = parse_event(event)
        if getattr(event, "type", None) in LIVE_ONLY_EVENT_TYPES:
            self.frames.publish(event)
            return event
        return await self.bus.publish(event)


# ----------------------------------------------------------------------------------------------------------------------
# ApprovalBroker
# ----------------------------------------------------------------------------------------------------------------------
@dataclass
class _Pending:
    request: ApprovalRequest
    future: "asyncio.Future[ApprovalResponse]"
    timer: asyncio.TimerHandle | None = None


class GatewayApprovalBroker:
    """``contracts.services.ApprovalBroker``. One Future per request; the first answer wins.

    Answers arrive from REST (``POST /api/approvals/{id}``), the ``/ws`` client (``WsApproval``) or messaging inbound
    (gateway -> agent.interpret_reply -> ``resolve``). After ``expires_s`` the request resolves with
    ``decision='timeout'`` (channel 'system'). Photon is asked via ``services.messaging.send_approval_request`` when
    ``'photon' in request.channels``.
    """

    def __init__(self, services: Services) -> None:
        self._services = services
        self._pending: dict[str, _Pending] = {}
        self._listeners: list[ApprovalListener] = []
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- extras (gateway only) -------------------------------------------------------------------------------------
    def add_listener(self, listener: ApprovalListener) -> None:
        if listener not in self._listeners:
            self._listeners.append(listener)

    def get(self, approval_id: str) -> ApprovalRequest | None:
        p = self._pending.get(approval_id)
        return p.request if p else None

    def check(self, response: ApprovalResponse) -> str | None:
        """Why ``resolve(response)`` would fail: 'unknown' | 'cart_hash_mismatch' | None (it would succeed)."""
        p = self._pending.get(response.approval_id)
        if p is None or p.future.done():
            return "unknown"
        req = p.request
        if (
            req.kind == ApprovalKind.CHECKOUT
            and response.approved
            and response.channel == "web"
            and response.cart_hash != req.cart_hash
        ):
            return "cart_hash_mismatch"
        return None

    async def open(self, request: ApprovalRequest) -> "asyncio.Future[ApprovalResponse]":
        """Register + publish + fan out, then return the Future (``request`` = ``open`` + await)."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[ApprovalResponse] = loop.create_future()
        pending = _Pending(request=request, future=fut)
        self._pending[request.approval_id] = pending
        pending.timer = loop.call_later(request.expires_s, self._expire, request.approval_id)
        await self._notify("requested", request, None)
        await self._publish(ApprovalRequested(trip_id=request.trip_id, source="gateway", request=request))
        if "photon" in request.channels and self._services.messaging is not None:
            self._spawn(self._send_photon(request))
        return fut

    # -- Protocol --------------------------------------------------------------------------------------------------
    async def request(self, request: ApprovalRequest) -> ApprovalResponse:
        fut = await self.open(request)
        return await fut

    async def resolve(self, response: ApprovalResponse) -> bool:
        p = self._pending.get(response.approval_id)
        if p is None or p.future.done():
            return False
        req = p.request
        update: dict[str, Any] = {}
        if response.channel != "web" and req.cart_hash:
            update["cart_hash"] = req.cart_hash
        if response.user_id is None:
            update["user_id"] = req.user_id
        if update:
            response = response.model_copy(update=update)
        if self.check(response) is not None:
            return False
        await self._finish(p, response)
        return True

    def pending(self, trip_id: str | None = None) -> list[ApprovalRequest]:
        return [
            p.request
            for p in self._pending.values()
            if not p.future.done() and (trip_id is None or p.request.trip_id == trip_id)
        ]

    # -- internals -------------------------------------------------------------------------------------------------
    def _expire(self, approval_id: str) -> None:
        p = self._pending.get(approval_id)
        if p is None or p.future.done():
            return
        resp = ApprovalResponse(approval_id=approval_id, decision="timeout", channel="system", note="expired")
        self._spawn(self._finish(p, resp))

    async def _finish(self, p: _Pending, response: ApprovalResponse) -> None:
        if p.future.done():
            return
        if p.timer is not None:
            p.timer.cancel()
        self._pending.pop(response.approval_id, None)
        p.future.set_result(response)
        await self._notify("resolved", p.request, response)
        await self._publish(
            ApprovalResolved(trip_id=p.request.trip_id, source="gateway", kind=p.request.kind, response=response)
        )

    async def _send_photon(self, request: ApprovalRequest) -> None:
        messaging = self._services.messaging
        if messaging is None:
            return
        try:
            res = await messaging.send_approval_request(request)
            if not res.ok:
                await self._error(request.trip_id, f"photon approval send failed: {res.error}")
        except Exception as exc:  # noqa: BLE001 - the web channel still works
            log.warning("send_approval_request failed: %s", exc)
            await self._error(request.trip_id, f"photon approval send failed: {exc}")

    async def _notify(self, phase: str, request: ApprovalRequest, response: ApprovalResponse | None) -> None:
        for listener in list(self._listeners):
            try:
                res = listener(phase, request, response)
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:  # noqa: BLE001
                log.warning("approval listener failed: %s", exc)

    async def _publish(self, event: Any) -> None:
        events = self._services.events
        if events is None:
            return
        try:
            await events.publish(event)
        except Exception as exc:  # noqa: BLE001
            log.warning("publish %s failed: %s", getattr(event, "type", "?"), exc)

    async def _error(self, trip_id: str | None, message: str) -> None:
        await self._publish(ErrorEvent(trip_id=trip_id, source="gateway", arm="messaging", message=message))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


# ----------------------------------------------------------------------------------------------------------------------
# SimLink (the /ws/sim worker)
# ----------------------------------------------------------------------------------------------------------------------
Sender = Callable[[str], Awaitable[None]]


class SimLinkHub:
    """``contracts.sim.SimLink`` backed by the currently connected ``/ws/sim`` worker (or nothing)."""

    def __init__(self) -> None:
        self._sender: Sender | None = None
        self._pending: dict[str, asyncio.Future[CommandResult]] = {}
        self._handlers: list[SimMessageHandler] = []
        self.session_id: str | None = None
        self.worker_id: str | None = None
        self.store_id: str | None = None

    def is_connected(self) -> bool:
        return self._sender is not None

    def attach(self, sender: Sender, *, session_id: str, worker_id: str | None = None, store_id: str | None = None):
        if self._sender is not None:
            self.detach("replaced by a new worker connection")
        self._sender = sender
        self.session_id, self.worker_id, self.store_id = session_id, worker_id, store_id
        log.info("sim worker %s attached (session %s, store %s)", worker_id, session_id, store_id)

    def detach(self, reason: str = "disconnected") -> None:
        self._sender = None
        pending, self._pending = self._pending, {}
        for fut in pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError(f"sim worker {reason}"))
        log.warning("sim worker detached: %s (%d in-flight commands failed)", reason, len(pending))

    async def send(self, cmd: Any, timeout_s: float | None = None) -> CommandResult:
        sender = self._sender
        if sender is None:
            raise ConnectionError("no sim worker connected")
        fut: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        self._pending[cmd.cmd_id] = fut
        try:
            await sender(cmd.model_dump_json())
            return await asyncio.wait_for(fut, timeout_s if timeout_s is not None else default_timeout_s(cmd))
        finally:
            self._pending.pop(cmd.cmd_id, None)

    def subscribe(self, handler: SimMessageHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(handler)

        return _unsubscribe

    async def dispatch(self, msg: SimMessage) -> None:
        """Route one message from the worker: results to their waiting ``send``; everything else to subscribers."""
        if isinstance(msg, CommandResult):
            fut = self._pending.get(msg.cmd_id)
            if fut is not None and not fut.done():
                fut.set_result(msg)
            return
        for handler in list(self._handlers):
            try:
                await handler(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("sim message handler failed: %s", exc)


# ----------------------------------------------------------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------------------------------------------------------
@dataclass
class Registry:
    services: Services
    bus: EventBus
    publisher: GatewayPublisher
    approvals: GatewayApprovalBroker
    sim_link: SimLinkHub
    frames: FrameHub
    orchestrator: Any = None
    modes: dict[str, str] = field(default_factory=dict)
    """arm -> 'real' | 'fake' | 'fake (real failed)' | 'missing'."""
    notes: dict[str, str] = field(default_factory=dict)
    fake_opts: dict[str, dict[str, Any]] = field(default_factory=dict)
    _fallback_robot: Any = None

    def fallback_robot(self) -> Any:
        """The robot fake (2D kinematic sim) used when the Isaac worker disconnects. Built lazily."""
        if self._fallback_robot is None:
            if self.modes.get("robot") == "fake":
                self._fallback_robot = self.services.robot
            else:
                self._fallback_robot = _call_factory("robot", "create_fake", self.services, self.fake_opts.get("robot"))
        return self._fallback_robot

    def event_dicts(self, after_seq: int = 0) -> list[dict[str, Any]]:
        return [event_to_dict(e) for e in self.bus.recent(after_seq=after_seq)]


def _call_factory(arm: str, factory: str, services: Services, opts: dict[str, Any] | None) -> Any:
    module = importlib.import_module(f"arms.{arm}")
    fn = getattr(module, factory)
    if not opts:
        return fn(services)
    try:
        return fn(services, **opts)
    except TypeError:
        obj = fn(services)
        for key, value in opts.items():
            if hasattr(obj, key):
                setattr(obj, key, value)
        return obj


def build_registry(
    settings: Settings | None = None,
    *,
    real_arms: set[str] | None = None,
    fake_opts: dict[str, dict[str, Any]] | None = None,
    real_opts: dict[str, dict[str, Any]] | None = None,
    orchestrator_config: Any = None,
) -> Registry:
    """Wire everything. ``real_arms`` overrides ``PICKY_REAL_ARMS``; ``fake_opts[arm]`` are kwargs for that fake
    (applied as attributes when its factory takes only ``services``)."""
    from arms.gateway.orchestrator import TripOrchestrator  # local import: orchestrator imports nothing from here

    settings = settings or get_settings()
    fake_opts = fake_opts or {}
    real_opts = real_opts or {}

    def is_real(arm: str) -> bool:
        if real_arms is not None:
            return "all" in real_arms or arm in real_arms
        return settings.is_real(arm)

    services = Services()
    bus = EventBus()
    frames = FrameHub()
    publisher = GatewayPublisher(bus, frames)
    approvals = GatewayApprovalBroker(services)
    sim_link = SimLinkHub()
    reg = Registry(
        services=services,
        bus=bus,
        publisher=publisher,
        approvals=approvals,
        sim_link=sim_link,
        frames=frames,
        fake_opts=fake_opts,
    )
    services.events = publisher
    services.approvals = approvals
    services.sim_link = sim_link

    for arm in ARM_NAMES:
        if arm not in SERVICE_PROTOCOLS:
            continue  # sim (worker), gateway (us), frontend (Node)
        obj, mode = None, "missing"
        if is_real(arm):
            try:
                obj, mode = _call_factory(arm, "create", services, real_opts.get(arm)), "real"
            except Exception as exc:  # noqa: BLE001 - degrade to the fake, never die at boot
                log.warning("real %s failed to construct (%s); using its fake", arm, exc)
                reg.notes[arm] = f"real failed: {exc}"
        if obj is None:
            try:
                obj = _call_factory(arm, "create_fake", services, fake_opts.get(arm))
                mode = "fake" if mode == "missing" else mode
                if arm in reg.notes:
                    mode = "fake (real failed)"
            except Exception as exc:  # noqa: BLE001
                log.error("fake %s unavailable: %s", arm, exc)
                reg.notes[arm] = f"unavailable: {exc}"
                mode = "missing"
        if obj is not None and not isinstance(obj, SERVICE_PROTOCOLS[arm]):
            reg.notes[arm] = f"{type(obj).__name__} does not satisfy {SERVICE_PROTOCOLS[arm].__name__}"
            log.warning(reg.notes[arm])
        setattr(services, arm, obj)
        reg.modes[arm] = mode

    orchestrator = TripOrchestrator(services, config=orchestrator_config, fallback_robot=reg.fallback_robot)
    services.trips = orchestrator
    reg.orchestrator = orchestrator
    log.info("gateway wired: %s", ", ".join(f"{a}={m}" for a, m in reg.modes.items()))
    return reg


__all__ = [
    "FrameHub",
    "GatewayApprovalBroker",
    "GatewayPublisher",
    "Registry",
    "SimLinkHub",
    "build_registry",
]
