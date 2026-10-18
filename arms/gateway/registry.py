"""Composition root: builds the ``Services`` container (real vs fake per service) plus the gateway-owned pieces.

- ``build_registry()`` -> ``Registry``: for every field in ``contracts.services.SERVICE_FACTORIES`` (the arms' primary
  services + perception's ``cameras`` + agent's ``brain``): ``arms.<arm>.<real factory>(services)`` when the field OR
  its arm is in ``PICKY_REAL_ARMS``, else ``arms.<arm>.<fake factory>(services)``. A real factory that fails falls back
  to the fake (logged + noted in ``/health``); a missing fake leaves the field None ("missing").
- The planner -> VLA -> verifier loop (contracts 0.2.1, ``LOOP_SERVICE_FACTORIES``): ``planner`` / ``runner`` (agent),
  ``vla`` / ``truth`` / ``faults`` (robot), ``verifier`` / ``evidence`` (perception), same real / fake rules (field or
  arm name in ``PICKY_REAL_ARMS``). An arm that does not export a loop factory leaves that field None ("not wired",
  never an error); ``loop=False`` skips them all (the v0.2 path). Whether goals then run through the loop is
  ``exec_mode`` (``PICKY_EXEC_MODE``; blank = plan when the VLA and the verifier are wired).
- Gateway-provided services: ``GatewayPublisher`` (EventPublisher over ``utils.events.EventBus``; live-only ``frame`` /
  ``camera.frame`` events go to the ``FrameHub``), ``GatewayApprovalBroker`` (asyncio Futures, resolvable from REST,
  websocket or messaging inbound; timeouts; filters; cancel), ``SimLinkHub`` (every connected ``/ws/sim`` worker as
  ONE multiplexed ``SimLink``, routed by robot_id / store_id / camera_id) and the ``TripOrchestrator``
  (TripController).

Factories never call other services at construction time, so the order here doesn't matter.
"""

import asyncio
import contextlib
import importlib
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from arms.gateway.judges import JudgesIndex
from arms.gateway.loop_api import EvidenceAnnotations
from contracts.cameras import CameraInfo, parse_camera_id
from contracts.common import utcnow
from contracts.events import LIVE_ONLY_EVENT_TYPES, ApprovalRequested, ApprovalResolved, ErrorEvent, Event, parse_event
from contracts.services import (
    EXTRA_SERVICE_PROTOCOLS,
    LOOP_SERVICE_FACTORIES,
    LOOP_SERVICE_PROTOCOLS,
    SERVICE_FACTORIES,
    SERVICE_PROTOCOLS,
    Services,
    exec_mode,
)
from contracts.sim import (
    CommandResult,
    SetStreamsCmd,
    SimHello,
    SimHosted,
    SimMessage,
    SimMessageHandler,
    SimRobotInfo,
    SimStatus,
    SimStoreInfo,
    SimWorkerStatus,
    StopCmd,
    default_timeout_s,
)
from contracts.trust import ApprovalKind, ApprovalRequest, ApprovalResponse
from utils.config import Settings, get_settings
from utils.events import EventBus, event_to_dict
from utils.log import get_logger

log = get_logger(__name__)
MAX_MESSAGE_MAP = 500
"""How many outbound approval texts the broker remembers for reply matching."""

ApprovalListener = Callable[[str, ApprovalRequest, ApprovalResponse | None], Awaitable[None] | None]
"""``listener(phase, request, response)``; phase = 'requested' | 'resolved'."""

EnvelopeHook = Callable[[ApprovalRequest], Awaitable[dict[str, Any]] | dict[str, Any]]
"""Returns the event envelope (trip_id, store_id, robot_id, user_id, request_id) for an approval's events."""

ALL_SERVICE_PROTOCOLS: dict[str, type] = {**SERVICE_PROTOCOLS, **EXTRA_SERVICE_PROTOCOLS, **LOOP_SERVICE_PROTOCOLS}
ANNOTATED_EVENT_TYPES = frozenset({"step.verified", "target.grounded", "camera.overlay"})
NOT_WIRED = "not wired"
"""Registry mode of a loop field whose arm exports no factory (or ``loop=False``): None in Services, not an error."""


def configured_exec_mode(services: Services, settings: Settings | None = None) -> str:
    """``PICKY_EXEC_MODE`` as the agent's brain reads it: 'direct' | 'plan' | 'tools'; blank / 'auto' = 'plan' when
    ``Services.vla`` and ``Services.verifier`` are wired, else 'direct' (mirror of arms.agent.loop.configured_exec_mode,
    which the gateway may not import)."""
    s = settings or get_settings()
    raw = str(getattr(s, "picky_exec_mode", "") or "").strip().lower()
    if raw in ("", "auto"):
        return "plan" if services.vla is not None and services.verifier is not None else "direct"
    return raw


def current_exec_mode(services: Services, settings: Settings | None = None) -> str:
    """The mode goals run in right now: ``contracts.services.exec_mode`` (plan / tools only when planner, runner, vla
    and verifier are all wired; otherwise the v0.2 'direct' path)."""
    return exec_mode(services, configured_exec_mode(services, settings))


# ----------------------------------------------------------------------------------------------------------------------
# Live-only frames + full-rate robot.state (/ws/frames)
# ----------------------------------------------------------------------------------------------------------------------
class FrameSubscription:
    """Latest-only queue for one live client (drop-oldest, small), filtered by trip_id and/or robot_id."""

    def __init__(self, hub: "FrameHub", trip_id: str | None, robot_id: str | None = None, maxsize: int = 8) -> None:
        self._hub = hub
        self.trip_id = trip_id
        self.robot_id = robot_id
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=maxsize)

    def wants(self, event: Any) -> bool:
        if self.robot_id is not None and getattr(event, "robot_id", None) not in (None, self.robot_id):
            return False
        if self.trip_id is not None and self.robot_id is None:
            return getattr(event, "trip_id", None) in (None, self.trip_id)
        return True

    def offer(self, event: Any) -> None:
        if not self.wants(event):
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
    """Fan-out for live-only events (``frame`` / ``camera.frame``) and full-rate ``robot.state`` (seq=0, no replay)."""

    def __init__(self) -> None:
        self._subs: set[FrameSubscription] = set()
        self.last: Any = None

    def subscribe(self, trip_id: str | None = None, robot_id: str | None = None) -> FrameSubscription:
        sub = FrameSubscription(self, trip_id, robot_id)
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
    """``contracts.services.EventPublisher``: stamps bus-wide ``seq`` (utils.events) and fans out to websockets.
    Loop evidence (``step.verified`` / ``target.grounded`` / ``camera.overlay``) also feeds the annotation index."""

    def __init__(self, bus: EventBus, frames: FrameHub, annotations: EvidenceAnnotations | None = None) -> None:
        self.bus = bus
        self.frames = frames
        self.annotations = annotations
        self.judges = JudgesIndex()
        """0.2.4: verification cards + payment agent timelines (REST views of the event stream)."""

    async def publish(self, event: Event) -> Event:
        if isinstance(event, dict):
            event = parse_event(event)
        if getattr(event, "type", None) in ("item.verification", "payment.agent_step"):
            self.judges.observe(event)
        if self.annotations is not None and getattr(event, "type", None) in ANNOTATED_EVENT_TYPES:
            self.annotations.observe(event)
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
    envelope: dict[str, Any] = field(default_factory=dict)


class GatewayApprovalBroker:
    """``contracts.services.ApprovalBroker``. One Future per request; the first answer wins.

    Answers arrive from REST (``POST /api/approvals/{id}``), the ``/ws`` client (``WsApproval``) or messaging inbound
    (gateway -> agent.interpret_reply -> ``resolve``). After ``expires_s`` the request resolves with
    ``decision='timeout'`` (channel 'system'); ``cancel`` withdraws one the same way (goal cancelled / reassigned).
    Photon is asked via ``services.messaging.send_approval_request`` when ``'photon' in request.channels``.
    Events carry the request's envelope (``envelope_hook``, set by the orchestrator, fills request_id from the trip /
    goal).
    """

    def __init__(self, services: Services) -> None:
        self._services = services
        self._pending: dict[str, _Pending] = {}
        self._listeners: list[ApprovalListener] = []
        self._tasks: set[asyncio.Task[Any]] = set()
        self._by_message: dict[str, ApprovalRequest] = {}
        """Outbound Photon message_id -> the approval it asked (so a text reply finds its approval and trip)."""
        self.envelope_hook: EnvelopeHook | None = None

    # -- extras (gateway only) -------------------------------------------------------------------------------------
    def approval_for_message(self, message_id: str | None) -> ApprovalRequest | None:
        """The approval that the outbound text ``message_id`` asked (pending or already answered), else None."""
        return self._by_message.get(message_id) if message_id else None

    def is_pending(self, approval_id: str) -> bool:
        p = self._pending.get(approval_id)
        return p is not None and not p.future.done()

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
        pending = _Pending(request=request, future=fut, envelope=await self._envelope(request))
        self._pending[request.approval_id] = pending
        pending.timer = loop.call_later(request.expires_s, self._expire, request.approval_id)
        await self._notify("requested", request, None)
        await self._publish(ApprovalRequested(source="gateway", request=request, **pending.envelope))
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

    def pending(
        self,
        trip_id: str | None = None,
        *,
        goal_id: str | None = None,
        robot_id: str | None = None,
        store_id: str | None = None,
        user_id: str | None = None,
    ) -> list[ApprovalRequest]:
        wanted = {"trip_id": trip_id, "goal_id": goal_id, "robot_id": robot_id, "store_id": store_id}
        wanted["user_id"] = user_id
        out = []
        for p in self._pending.values():
            if p.future.done():
                continue
            req = p.request
            if all(v is None or getattr(req, k, None) == v for k, v in wanted.items()):
                out.append(req)
        return out

    async def cancel(self, approval_id: str, reason: str = "") -> bool:
        p = self._pending.get(approval_id)
        if p is None or p.future.done():
            return False
        resp = ApprovalResponse(
            approval_id=approval_id, decision="timeout", channel="system", note=reason or "withdrawn"
        )
        await self._finish(p, resp)
        return True

    # -- internals -------------------------------------------------------------------------------------------------
    async def _envelope(self, request: ApprovalRequest) -> dict[str, Any]:
        env: dict[str, Any] = {
            "trip_id": request.trip_id,
            "store_id": request.store_id,
            "robot_id": request.robot_id,
            "user_id": request.user_id,
        }
        hook = self.envelope_hook
        if hook is not None:
            try:
                extra = hook(request)
                if inspect.isawaitable(extra):
                    extra = await extra
                env.update({k: v for k, v in (extra or {}).items() if v is not None})
            except Exception as exc:  # noqa: BLE001 - the approval itself still works
                log.debug("approval envelope hook failed: %s", exc)
        return env

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
        await self._publish(ApprovalResolved(source="gateway", kind=p.request.kind, response=response, **p.envelope))

    async def _send_photon(self, request: ApprovalRequest) -> None:
        messaging = self._services.messaging
        if messaging is None:
            return
        try:
            res = await messaging.send_approval_request(request)
            if res.ok and res.message_id:
                self._by_message[res.message_id] = request
                while len(self._by_message) > MAX_MESSAGE_MAP:
                    self._by_message.pop(next(iter(self._by_message)))
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
# SimLink (the /ws/sim workers, multiplexed)
# ----------------------------------------------------------------------------------------------------------------------
Sender = Callable[[str], Awaitable[None]]


@dataclass
class SimWorker:
    """One connected ``/ws/sim`` worker and what it hosts (latest SimHello / SimHosted)."""

    session_id: str
    sender: Sender
    worker_id: str = "isaac-0"
    stores: list[SimStoreInfo] = field(default_factory=list)
    robots: list[SimRobotInfo] = field(default_factory=list)
    cameras: list[CameraInfo] = field(default_factory=list)
    sim_version: str | None = None
    contracts_version: str | None = None
    streaming: list[str] = field(default_factory=list)
    connected_at: Any = field(default_factory=utcnow)
    last_message_at: Any = None

    def hosts_robot(self, robot_id: str) -> bool:
        return any(r.robot_id == robot_id for r in self.robots)

    def hosts_store(self, store_id: str) -> bool:
        return any(s.store_id == store_id for s in self.stores)

    def camera_ids(self) -> set[str]:
        ids = {c.camera_id for c in self.cameras}
        for r in self.robots:
            ids.update(r.camera_ids)
        for s in self.stores:
            ids.update(s.camera_ids)
        return ids

    def hosts_camera(self, camera_id: str) -> bool:
        if camera_id in self.camera_ids():
            return True
        try:
            owner, kind = parse_camera_id(camera_id)
        except ValueError:
            return False
        return self.hosts_store(owner) if kind == "ceiling" else self.hosts_robot(owner)

    def apply_hosting(self, msg: SimHello | SimHosted) -> None:
        stores = list(msg.stores)
        legacy = getattr(msg, "store_id", None)
        if legacy and not any(s.store_id == legacy for s in stores):
            stores.append(SimStoreInfo(store_id=legacy, data_hash=getattr(msg, "data_hash", None)))
        self.stores, self.robots, self.cameras = stores, list(msg.robots), list(msg.cameras)

    def status(self) -> SimWorkerStatus:
        return SimWorkerStatus(
            worker_id=self.worker_id,
            session_id=self.session_id,
            connected=True,
            connected_at=self.connected_at,
            last_message_at=self.last_message_at,
            sim_version=self.sim_version,
            contracts_version=self.contracts_version,
            stores=list(self.stores),
            robots=list(self.robots),
            cameras=list(self.cameras),
            streaming_camera_ids=list(self.streaming),
        )


class SimLinkHub:
    """``contracts.sim.SimLink`` over every connected ``/ws/sim`` worker (usually one: Isaac on vhagar).

    Commands are routed by ``robot_id`` (motion), ``store_id`` (reset / list_instances / set_slot_stock) or
    ``camera_id`` (capture) to the worker that hosts the target; ``stop(robot_id=None)`` and ``set_streams`` go to every
    worker. With exactly one worker connected, an unhosted target still goes to it (the worker answers
    ``unknown_robot`` / builds the store on a store-level reset). Subscribers get every unsolicited message plus each
    accepted ``SimHello`` and every ``SimHosted``.
    """

    def __init__(self) -> None:
        self._workers: dict[str, SimWorker] = {}
        self._pending: dict[str, tuple[str, asyncio.Future[CommandResult]]] = {}
        self._handlers: list[SimMessageHandler] = []
        self._bg: set[asyncio.Task[None]] = set()

    # -- connection management (gateway /ws/sim) -----------------------------------------------------------------------
    @property
    def workers(self) -> list[SimWorker]:
        return list(self._workers.values())

    def attach(
        self,
        sender: Sender,
        *,
        session_id: str,
        worker_id: str | None = None,
        store_id: str | None = None,
        hello: SimHello | None = None,
    ) -> SimWorker:
        """Register a worker connection (a worker reconnecting with the same worker_id replaces its old session)."""
        wid = worker_id or (hello.worker_id if hello is not None else None) or session_id
        for old in [w for w in self._workers.values() if w.worker_id == wid]:
            self.detach("replaced by a new connection", session_id=old.session_id)
        worker = SimWorker(session_id=session_id, sender=sender, worker_id=wid)
        if hello is not None:
            worker.apply_hosting(hello)
            worker.sim_version, worker.contracts_version = hello.sim_version, hello.contracts_version
        elif store_id:
            worker.stores = [SimStoreInfo(store_id=store_id)]
        self._workers[session_id] = worker
        log.info(
            "sim worker %s attached (session %s, stores %s, robots %s)",
            wid,
            session_id,
            [s.store_id for s in worker.stores],
            [r.robot_id for r in worker.robots],
        )
        return worker

    async def accept(self, hello: SimHello, sender: Sender, session_id: str) -> SimWorker:
        """attach + tell subscribers about the accepted hello (the robot arm picks backends per store from it)."""
        worker = self.attach(sender, session_id=session_id, hello=hello)
        await self._fan_out(hello)
        return worker

    def detach(self, reason: str = "disconnected", session_id: str | None = None) -> None:
        """Drop one worker (``session_id``) or all (None); its in-flight commands fail with ConnectionError."""
        sessions = [session_id] if session_id is not None else list(self._workers)
        for sid in sessions:
            worker = self._workers.pop(sid, None)
            failed = 0
            for cmd_id, (owner, fut) in list(self._pending.items()):
                if owner != sid:
                    continue
                self._pending.pop(cmd_id, None)
                if not fut.done():
                    fut.set_exception(ConnectionError(f"sim worker {reason}"))
                    failed += 1
            if worker is not None:
                log.warning(
                    "sim worker %s detached: %s (%d in-flight commands failed)", worker.worker_id, reason, failed
                )
                self._schedule_hosting_update()

    def _schedule_hosting_update(self) -> None:
        """After a worker leaves, tell subscribers what is still hosted, so the robot arm moves the stores it no
        longer has onto the 2D fake right away (not only at their next command)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._fan_out_hosting())
        self._bg.add(task)
        task.add_done_callback(self._bg.discard)

    async def _fan_out_hosting(self) -> None:
        # computed when it runs: a reconnecting worker (attach -> detach old session) is already in _workers
        workers = list(self._workers.values())
        stores = {st.store_id: st for w in workers for st in w.stores}
        robots = {r.robot_id: r for w in workers for r in w.robots}
        cameras = {c.camera_id: c for w in workers for c in w.cameras}
        await self._fan_out(
            SimHosted(
                worker_id=workers[0].worker_id if len(workers) == 1 else "gateway",
                stores=list(stores.values()),
                robots=list(robots.values()),
                cameras=list(cameras.values()),
            )
        )

    # -- SimLink Protocol --------------------------------------------------------------------------------------------
    def is_connected(self, robot_id: str | None = None) -> bool:
        if robot_id is None:
            return bool(self._workers)
        return any(w.hosts_robot(robot_id) for w in self._workers.values())

    async def send(self, cmd: Any, timeout_s: float | None = None) -> CommandResult:
        if not self._workers:
            raise ConnectionError("no sim worker connected")
        targets = self._route(cmd)
        if not targets:
            raise ConnectionError(f"no sim worker hosts the target of {getattr(cmd, 'type', '?')} {self._target(cmd)}")
        timeout = timeout_s if timeout_s is not None else default_timeout_s(cmd)
        if len(targets) == 1:
            return await self._send_one(targets[0], cmd, timeout)
        results = await asyncio.gather(
            *(
                self._send_one(w, cmd.model_copy(update={"cmd_id": f"{cmd.cmd_id}.{n}"}), timeout, cmd.cmd_id)
                for n, w in enumerate(targets)
            ),
            return_exceptions=True,
        )
        ok = [r for r in results if isinstance(r, CommandResult)]
        if not ok:
            raise next(r for r in results if isinstance(r, BaseException))  # type: ignore[misc]
        bad = next((r for r in ok if not r.ok), None)
        return bad or ok[0]

    def subscribe(self, handler: SimMessageHandler) -> Callable[[], None]:
        self._handlers.append(handler)

        def _unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._handlers.remove(handler)

        return _unsubscribe

    def status(self) -> SimStatus:
        workers = [w.status() for w in self._workers.values()]
        stores = sorted({s.store_id for w in self._workers.values() for s in w.stores if s.loaded})
        robots = sorted({r.robot_id for w in self._workers.values() for r in w.robots})
        return SimStatus(
            connected=bool(workers),
            workers=workers,
            isaac_store_ids=stores,
            isaac_robot_ids=robots,
            note="" if workers else "no Isaac worker connected; every store runs on the 2D robot fake",
        )

    # -- inbound ---------------------------------------------------------------------------------------------------
    async def dispatch(self, msg: SimMessage, session_id: str | None = None) -> None:
        """Route one message from a worker: results to their waiting ``send``; hosting updates to that worker; the
        rest (and hello / hosted) to subscribers."""
        worker = self._workers.get(session_id) if session_id is not None else None
        if worker is None and len(self._workers) == 1:
            worker = next(iter(self._workers.values()))
        if worker is not None:
            worker.last_message_at = utcnow()
        if isinstance(msg, CommandResult):
            entry = self._pending.get(msg.cmd_id)
            if entry is not None and not entry[1].done():
                entry[1].set_result(msg)
            return
        if isinstance(msg, SimHosted | SimHello) and worker is not None:
            worker.apply_hosting(msg)
        await self._fan_out(msg)

    # -- internals -------------------------------------------------------------------------------------------------
    async def _fan_out(self, msg: Any) -> None:
        for handler in list(self._handlers):
            try:
                await handler(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("sim message handler failed: %s", exc)

    @staticmethod
    def _target(cmd: Any) -> str:
        for key in ("robot_id", "camera_id", "store_id"):
            value = getattr(cmd, key, None)
            if value:
                return f"{key}={value}"
        return ""

    def _route(self, cmd: Any) -> list[SimWorker]:
        workers = list(self._workers.values())
        single = workers if len(workers) == 1 else []
        robot_id = getattr(cmd, "robot_id", None)
        if isinstance(cmd, SetStreamsCmd) or (isinstance(cmd, StopCmd) and robot_id is None):
            return workers
        camera_id = getattr(cmd, "camera_id", None)
        if camera_id:
            return [w for w in workers if w.hosts_camera(camera_id)][:1] or single
        if robot_id:
            return [w for w in workers if w.hosts_robot(robot_id)][:1] or single
        store_id = getattr(cmd, "store_id", None)
        if store_id:
            hosting = [w for w in workers if w.hosts_store(store_id)]
            if hosting:
                return hosting[:1]
            if getattr(cmd, "type", None) == "reset":  # build a new store scene on the least loaded worker
                return sorted(workers, key=lambda w: (len(w.stores), w.worker_id))[:1]
        return single

    async def _send_one(self, worker: SimWorker, cmd: Any, wait_s: float, reply_id: str | None = None) -> CommandResult:
        fut: asyncio.Future[CommandResult] = asyncio.get_running_loop().create_future()
        self._pending[cmd.cmd_id] = (worker.session_id, fut)
        if isinstance(cmd, SetStreamsCmd):
            known = worker.camera_ids()
            if known:
                cmd = cmd.model_copy(update={"camera_ids": [c for c in cmd.camera_ids if c in known]})
            worker.streaming = list(cmd.camera_ids)
        try:
            await worker.sender(cmd.model_dump_json())
            result = await asyncio.wait_for(fut, wait_s)
            return result.model_copy(update={"cmd_id": reply_id}) if reply_id else result
        finally:
            self._pending.pop(cmd.cmd_id, None)


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
    annotations: EvidenceAnnotations = field(default_factory=EvidenceAnnotations)
    orchestrator: Any = None
    modes: dict[str, str] = field(default_factory=dict)
    """service field -> 'real' | 'fake' | 'fake (real failed)' | 'missing' | 'override' | 'not wired' (loop fields)."""
    notes: dict[str, str] = field(default_factory=dict)
    fake_opts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def event_dicts(self, after_seq: int = 0) -> list[dict[str, Any]]:
        return [event_to_dict(e) for e in self.bus.recent(after_seq=after_seq)]

    @property
    def exec_mode(self) -> str:
        """direct | plan | tools (see ``current_exec_mode``)."""
        return current_exec_mode(self.services)


def _call_factory(arm: str, factory: str, services: Services, opts: dict[str, Any] | None) -> Any:
    module = importlib.import_module(f"arms.{arm}")
    fn = getattr(module, factory, None)
    if fn is None:
        raise AttributeError(f"arms.{arm} has no {factory}()")
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
    overrides: dict[str, Any] | None = None,
    loop: bool = True,
) -> Registry:
    """Wire everything. ``real_arms`` overrides ``PICKY_REAL_ARMS`` (arm names or service fields, e.g. ``brain``,
    ``planner``, ``vla``, ``verifier``); ``fake_opts[field]`` are kwargs for that fake (applied as attributes when its
    factory takes only ``services``); ``overrides[field]`` = ``factory(services)`` (or a ready object) that replaces
    that service outright (tests; ``None`` leaves a loop field unwired). ``loop=False`` leaves every loop field None."""
    from arms.gateway.orchestrator import TripOrchestrator  # local import: orchestrator imports nothing from here

    settings = settings or get_settings()
    fake_opts = fake_opts or {}
    real_opts = real_opts or {}
    overrides = overrides or {}

    def is_real(name: str) -> bool:
        if real_arms is not None:
            return "all" in real_arms or name in real_arms
        return settings.is_real(name)

    services = Services()
    bus = EventBus()
    frames = FrameHub()
    annotations = EvidenceAnnotations()
    publisher = GatewayPublisher(bus, frames, annotations)
    approvals = GatewayApprovalBroker(services)
    sim_link = SimLinkHub()
    reg = Registry(
        services=services,
        bus=bus,
        publisher=publisher,
        approvals=approvals,
        sim_link=sim_link,
        frames=frames,
        annotations=annotations,
        fake_opts=fake_opts,
    )
    services.events = publisher
    services.approvals = approvals
    services.sim_link = sim_link

    factories = {**SERVICE_FACTORIES, **LOOP_SERVICE_FACTORIES}
    for name, (arm, real_factory, fake_factory) in factories.items():
        is_loop = name in LOOP_SERVICE_FACTORIES
        if name in overrides:
            factory = overrides[name]
            setattr(services, name, factory(services) if callable(factory) else factory)
            reg.modes[name] = "override" if getattr(services, name) is not None else NOT_WIRED
            continue
        if is_loop and (not loop or not _exports(arm, real_factory, fake_factory)):
            reg.modes[name] = NOT_WIRED  # the arm doesn't implement it yet: the v0.2 path, never an error
            continue
        obj, mode = None, "missing"
        if is_real(name) or is_real(arm):
            try:
                obj, mode = _call_factory(arm, real_factory, services, real_opts.get(name)), "real"
            except Exception as exc:  # noqa: BLE001 - degrade to the fake, never die at boot
                log.warning("real %s failed to construct (%s); using its fake", name, exc)
                reg.notes[name] = f"real failed: {exc}"
        if obj is None:
            try:
                obj = _call_factory(arm, fake_factory, services, fake_opts.get(name))
                mode = "fake (real failed)" if name in reg.notes else "fake"
            except Exception as exc:  # noqa: BLE001
                log.error("fake %s unavailable: %s", name, exc)
                reg.notes[name] = f"unavailable: {exc}"
                mode = "missing"
        proto = ALL_SERVICE_PROTOCOLS.get(name)
        if obj is not None and proto is not None and not isinstance(obj, proto):
            reg.notes[name] = f"{type(obj).__name__} does not satisfy {proto.__name__}"
            log.warning(reg.notes[name])
        setattr(services, name, obj)
        reg.modes[name] = mode

    orchestrator = TripOrchestrator(services, config=orchestrator_config)
    services.trips = orchestrator
    reg.orchestrator = orchestrator
    orchestrator.photo_lookup = publisher.judges.photos
    approvals.envelope_hook = orchestrator.approval_envelope
    log.info("gateway wired: %s | exec_mode=%s", ", ".join(f"{a}={m}" for a, m in reg.modes.items()), reg.exec_mode)
    return reg


def _exports(arm: str, *factories: str) -> bool:
    """Does ``arms.<arm>`` export at least one of ``factories``? (False if the arm can't be imported.)"""
    try:
        module = importlib.import_module(f"arms.{arm}")
    except Exception as exc:  # noqa: BLE001 - the primary service build reports import errors
        log.warning("arms.%s not importable: %s", arm, exc)
        return False
    return any(callable(getattr(module, f, None)) for f in factories)


__all__ = [
    "NOT_WIRED",
    "FrameHub",
    "GatewayApprovalBroker",
    "GatewayPublisher",
    "Registry",
    "SimLinkHub",
    "SimWorker",
    "build_registry",
    "configured_exec_mode",
    "current_exec_mode",
]
