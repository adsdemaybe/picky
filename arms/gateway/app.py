"""FastAPI gateway: every endpoint in contracts/api.md, ``/ws`` (events + client actions), ``/ws/frames`` (live view),
``/ws/sim`` (Isaac worker -> SimLink) and ``/debug`` (no-build debug page).

Run: ``make dev`` (= ``uvicorn arms.gateway.app:app --reload``). One process, one worker: the event bus is in-process.
"""

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import ValidationError

from arms.gateway.orchestrator import OrchestratorConfig, TripOrchestrator
from arms.gateway.registry import Registry, build_registry
from contracts import CONTRACTS_VERSION
from contracts.api import (
    ApprovalDecisionRequest,
    CheckoutStatus,
    CreateTripRequest,
    HealthStatus,
    PostMessageRequest,
    RobotLoginRequest,
    VoiceTurnResponse,
    WsApproval,
    WsChat,
    parse_client_message,
)
from contracts.catalog import (
    Deal,
    MerchantDashboard,
    NearExpiryItem,
    ScanApplyResult,
    SearchHit,
    SearchQuery,
    StoreInfo,
)
from contracts.events import TripSnapshot, event_json_schema
from contracts.messaging import InboundMessage, OutboundMessage, ReplyInterpretation
from contracts.perception import ScanReport
from contracts.planogram import Planogram
from contracts.profile import Feedback, ProfileUpdate, UserProfile
from contracts.routing import GridSpec
from contracts.sim import SimHello, SimWelcome, parse_sim_message, planogram_hash, versions_compatible
from contracts.tools import AgentReply
from contracts.trip import Trip
from contracts.trust import ApprovalRequest, ApprovalResponse, IntentMandate, TrustLedger
from utils.config import get_settings
from utils.events import event_to_dict
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)
STATIC_DIR = Path(__file__).parent / "static"


def create_app(registry: Registry | None = None, orchestrator_config: OrchestratorConfig | None = None) -> FastAPI:
    """App factory. ``registry=None`` builds one from settings in the lifespan (``PICKY_REAL_ARMS``)."""
    state: dict[str, Any] = {"registry": registry}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if state["registry"] is None:
            state["registry"] = build_registry(orchestrator_config=orchestrator_config)
        reg: Registry = state["registry"]
        app.state.registry = reg
        await reg.orchestrator.start()
        log.info("gateway up (env=%s, real=%s)", get_settings().picky_env, sorted(get_settings().real_arms()))
        try:
            yield
        finally:
            await reg.orchestrator.stop()
            reg.bus.close()

    app = FastAPI(title="Picky gateway", version=CONTRACTS_VERSION, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def reg() -> Registry:
        r = state["registry"]
        if r is None:
            raise HTTPException(503, "gateway not started")
        return r

    def orch() -> TripOrchestrator:
        return reg().orchestrator

    def svc(name: str) -> Any:
        s = getattr(reg().services, name)
        if s is None:
            raise HTTPException(503, f"{name} arm not wired")
        return s

    def trip_or_404(trip_id: str) -> Trip:
        try:
            return orch().peek(trip_id)
        except KeyError:
            raise HTTPException(404, f"unknown trip {trip_id}") from None

    # ------------------------------------------------------------------------------------------------------ health
    @app.get("/health", response_model=HealthStatus)
    async def health() -> HealthStatus:
        r = reg()
        details = {arm: mode for arm, mode in r.modes.items()}
        details.update({f"{arm}_note": note for arm, note in r.notes.items()})
        degraded = any(m != "real" and m != "fake" for m in r.modes.values())
        return HealthStatus(
            status="degraded" if degraded else "ok",
            contracts_version=CONTRACTS_VERSION,
            env=get_settings().picky_env,
            real_arms=sorted(a for a, m in r.modes.items() if m == "real"),
            sim_connected=r.sim_link.is_connected(),
            details=details,
        )

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/debug")

    @app.get("/debug", include_in_schema=False)
    async def debug_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "debug.html", media_type="text/html")

    # ------------------------------------------------------------------------------------------------------ catalog
    @app.get("/api/stores", response_model=list[StoreInfo])
    async def list_stores() -> list[StoreInfo]:
        return await svc("catalog").list_stores()

    @app.get("/api/stores/{store_id}/planogram", response_model=Planogram)
    async def get_planogram(store_id: str) -> Planogram:
        try:
            return await svc("catalog").get_planogram(store_id)
        except KeyError:
            raise HTTPException(404, f"no planogram for {store_id}") from None

    @app.get("/api/stores/{store_id}/grid", response_model=GridSpec)
    async def get_grid(store_id: str) -> GridSpec:
        try:
            return await svc("routing").grid_spec(store_id)
        except KeyError:
            raise HTTPException(404, f"no grid for {store_id}") from None

    @app.get("/api/products/search", response_model=list[SearchHit])
    async def search_products(
        q: str = "",
        store_id: list[str] | None = Query(None),  # noqa: B008
        category: str | None = None,
        dietary: list[str] | None = Query(None),  # noqa: B008
        in_stock_only: bool = False,
        limit: int = 10,
    ) -> list[SearchHit]:
        query = SearchQuery(
            text=q,
            store_ids=store_id or None,
            category=category,
            dietary=dietary or [],
            in_stock_only=in_stock_only,
            limit=max(1, min(limit, 100)),
        )
        return await svc("catalog").search(query)

    @app.get("/api/products/{sku}", response_model=SearchHit)
    async def get_product(sku: str, store_id: list[str] | None = Query(None)) -> SearchHit:  # noqa: B008
        catalog = svc("catalog")
        product = await catalog.get_product(sku)
        if product is None:
            raise HTTPException(404, f"unknown sku {sku}")
        offers = await catalog.get_offers(sku, store_id or None)
        return SearchHit(product=product, offers=offers, score=1.0)

    @app.get("/api/stores/{store_id}/deals", response_model=list[Deal])
    async def get_deals(store_id: str) -> list[Deal]:
        return await svc("catalog").get_deals(store_id)

    @app.get("/api/stores/{store_id}/near-expiry", response_model=list[NearExpiryItem])
    async def get_near_expiry(store_id: str, within_days: int = 3) -> list[NearExpiryItem]:
        return await svc("catalog").get_near_expiry(store_id, within_days)

    @app.post("/api/scans", response_model=ScanApplyResult)
    async def post_scan(report: ScanReport) -> ScanApplyResult:
        result = await svc("catalog").apply_scan(report)
        with contextlib.suppress(Exception):
            await reg().publisher.publish(
                _event("scan.result", report.trip_id, report=report)  # store-level when trip_id is None
            )
        return result

    @app.get("/api/merchant/{store_id}/dashboard", response_model=MerchantDashboard)
    async def merchant_dashboard(store_id: str) -> MerchantDashboard:
        return await svc("catalog").merchant_dashboard(store_id)

    # ------------------------------------------------------------------------------------------------------ users
    @app.get("/api/users/{user_id}/profile", response_model=UserProfile)
    async def get_profile(user_id: str) -> UserProfile:
        try:
            return await svc("recommender").get_profile(user_id)
        except KeyError:
            raise HTTPException(404, f"unknown user {user_id}") from None

    @app.put("/api/users/{user_id}/profile", response_model=UserProfile)
    async def put_profile(user_id: str, profile: UserProfile) -> UserProfile:
        if profile.user_id != user_id:
            raise HTTPException(400, "user_id mismatch")
        return await svc("recommender").save_profile(profile)

    @app.post("/api/users/{user_id}/feedback", response_model=ProfileUpdate)
    async def post_feedback(user_id: str, feedback: Feedback) -> ProfileUpdate:
        if feedback.user_id != user_id:
            raise HTTPException(400, "user_id mismatch")
        return await orch().submit_feedback(feedback)

    @app.get("/api/users/{user_id}/mandate", response_model=IntentMandate)
    async def get_mandate(user_id: str) -> IntentMandate:
        mandate = await svc("trust").get_mandate(user_id)
        if mandate is None:
            raise HTTPException(404, f"no mandate for {user_id}")
        return mandate

    @app.put("/api/users/{user_id}/mandate", response_model=IntentMandate)
    async def put_mandate(user_id: str, mandate: IntentMandate) -> IntentMandate:
        if mandate.user_id != user_id:
            raise HTTPException(400, "user_id mismatch")
        return await svc("trust").set_mandate(mandate)

    # ------------------------------------------------------------------------------------------------------ trips
    @app.post("/api/trips", response_model=Trip)
    async def create_trip(req: CreateTripRequest) -> Trip:
        return await orch().create_trip(req)

    @app.get("/api/trips", response_model=list[Trip])
    async def list_trips(user_id: str | None = None) -> list[Trip]:
        return orch().list_trips(user_id)

    @app.get("/api/trips/{trip_id}", response_model=Trip)
    async def get_trip(trip_id: str) -> Trip:
        trip_or_404(trip_id)
        return await orch().get_trip(trip_id)

    @app.post("/api/trips/{trip_id}/login", response_model=Trip)
    async def login(trip_id: str, req: RobotLoginRequest) -> Trip:
        trip_or_404(trip_id)
        try:
            return await orch().login(trip_id, req)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None

    @app.post("/api/trips/{trip_id}/messages", response_model=AgentReply)
    async def post_message(trip_id: str, req: PostMessageRequest) -> AgentReply:
        trip_or_404(trip_id)
        return await orch().chat(trip_id, req.text, req.channel)

    @app.post("/api/trips/{trip_id}/voice", response_model=VoiceTurnResponse)
    async def post_voice(trip_id: str, request: Request) -> VoiceTurnResponse:
        trip_or_404(trip_id)
        key = get_settings().deepgram_api_key
        if not key:
            raise HTTPException(503, "voice needs DEEPGRAM_API_KEY; use Web Speech + WsChat(channel='voice')")
        audio = await request.body()
        mime = request.headers.get("content-type", "audio/webm")
        try:
            transcript = await _deepgram_stt(key, audio, mime)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"speech-to-text failed: {exc}") from None
        reply = await orch().chat(trip_id, transcript, "voice")
        tts_b64 = None
        with contextlib.suppress(Exception):
            tts_b64 = await _deepgram_tts(key, reply.text)
        return VoiceTurnResponse(
            transcript=transcript, reply=reply, tts_b64=tts_b64, tts_mime="audio/mpeg" if tts_b64 else None
        )

    @app.post("/api/trips/{trip_id}/checkout", response_model=CheckoutStatus)
    async def checkout(trip_id: str) -> CheckoutStatus:
        trip_or_404(trip_id)
        return await orch().start_checkout(trip_id)

    @app.post("/api/trips/{trip_id}/followup", response_model=OutboundMessage)
    async def followup(trip_id: str) -> OutboundMessage:
        trip_or_404(trip_id)
        return await orch().send_followup(trip_id)

    # ------------------------------------------------------------------------------------------------------ approvals
    @app.get("/api/approvals", response_model=list[ApprovalRequest])
    async def list_approvals(trip_id: str | None = None) -> list[ApprovalRequest]:
        return reg().approvals.pending(trip_id)

    @app.post("/api/approvals/{approval_id}", response_model=ApprovalResponse)
    async def answer_approval(approval_id: str, req: ApprovalDecisionRequest) -> ApprovalResponse:
        broker = reg().approvals
        response = ApprovalResponse(
            approval_id=approval_id,
            decision=req.decision,
            choice=req.choice,
            choices=list(req.choices),
            channel=req.channel,
            user_id=req.user_id,
            cart_hash=req.cart_hash,
        )
        problem = broker.check(response) if response.channel == "web" else None
        if problem == "cart_hash_mismatch":
            raise HTTPException(409, "cart changed since this checkout approval was raised (cart_hash mismatch)")
        if not await broker.resolve(response):
            raise HTTPException(404, f"approval {approval_id} is unknown or already resolved")
        return response

    # ------------------------------------------------------------------------------------------------------ trust
    @app.get("/api/trust/ledger", response_model=TrustLedger)
    async def trust_ledger(user_id: str | None = None, trip_id: str | None = None) -> TrustLedger:
        return await svc("trust").ledger(user_id=user_id, trip_id=trip_id)

    # ------------------------------------------------------------------------------------------------------ dev
    @app.post("/api/dev/inbound", response_model=ReplyInterpretation)
    async def dev_inbound(msg: InboundMessage) -> ReplyInterpretation:
        # dev only: anyone who can reach PICKY_GATEWAY_URL could otherwise text "YES" as the shopper (CLAUDE.md §8)
        if not get_settings().is_dev:
            raise HTTPException(404, "not found")
        if msg.user_id is None and msg.handle:
            with contextlib.suppress(Exception):
                from contracts.messaging import normalize_handle

                profile = await svc("recommender").find_user_by_handle(normalize_handle(msg.handle))
                if profile is not None:
                    msg = msg.model_copy(update={"user_id": profile.user_id})
        return await orch().handle_inbound(msg)

    @app.get("/api/_schema/events")
    async def events_schema() -> dict[str, Any]:
        return event_json_schema()

    # ------------------------------------------------------------------------------------------------------ websockets
    @app.websocket("/ws")
    async def ws_events(
        ws: WebSocket, trip_id: str | None = None, store_id: str | None = None, after_seq: int = 0
    ) -> None:
        await ws.accept()
        r = reg()
        sub = r.bus.subscribe(None, trip_id=trip_id, name=f"ws:{trip_id or store_id or '*'}")
        sent_upto = after_seq

        def visible(ev: Any) -> bool:
            if store_id is None or trip_id is not None:
                return True
            tid = getattr(ev, "trip_id", None)
            if tid is None:
                return True
            try:
                return r.orchestrator.peek(tid).store_id == store_id
            except KeyError:
                return False

        async def sender() -> None:
            nonlocal sent_upto
            if trip_id is not None:
                try:
                    trip = await r.orchestrator.get_trip(trip_id)
                    snap = TripSnapshot(trip_id=trip_id, trip=trip, seq=r.bus.last_seq, source="gateway")
                    await ws.send_json(event_to_dict(snap))
                except KeyError:
                    await ws.send_json(_error_dict(trip_id, f"unknown trip {trip_id}"))
            for ev in r.bus.recent(trip_id=trip_id, after_seq=after_seq):
                if visible(ev):
                    await ws.send_json(event_to_dict(ev))
                    sent_upto = max(sent_upto, ev.seq)
            async for ev in sub:
                if ev.seq <= sent_upto or not visible(ev):
                    continue
                sent_upto = ev.seq
                await ws.send_json(event_to_dict(ev))

        async def receiver() -> None:
            while True:
                raw = await ws.receive_text()
                await _handle_client_message(r, ws, trip_id, raw)

        await _run_pair(ws, sender(), receiver())
        sub.close()

    @app.websocket("/ws/frames")
    async def ws_frames(ws: WebSocket, trip_id: str | None = None) -> None:
        await ws.accept()
        r = reg()
        sub = r.frames.subscribe(trip_id)

        async def sender() -> None:
            while True:
                ev = await sub.get()
                await ws.send_json(event_to_dict(ev))

        async def receiver() -> None:
            while True:
                await ws.receive_text()  # ignore; detects disconnect

        await _run_pair(ws, sender(), receiver())
        sub.close()

    @app.websocket("/ws/sim")
    async def ws_sim(ws: WebSocket, token: str | None = None) -> None:
        settings = get_settings()
        if token is not None and token != settings.picky_sim_token:
            await ws.close(code=4401)
            return
        await ws.accept()
        r = reg()
        try:
            first = parse_sim_message(await ws.receive_text())
        except (ValidationError, ValueError, WebSocketDisconnect) as exc:
            with contextlib.suppress(Exception):
                await ws.send_text(SimWelcome(ok=False, error=f"expected hello: {exc}").model_dump_json())
                await ws.close(code=4400)
            return
        if not isinstance(first, SimHello) or first.token != settings.picky_sim_token:
            await ws.send_text(SimWelcome(ok=False, error="bad token or missing hello").model_dump_json())
            await ws.close(code=4401)
            return
        if not versions_compatible(first.contracts_version, CONTRACTS_VERSION):
            await ws.send_text(
                SimWelcome(
                    ok=False, error=f"contracts mismatch: worker {first.contracts_version} vs {CONTRACTS_VERSION}"
                ).model_dump_json()
            )
            await ws.close(code=4409)
            return
        if first.data_hash and r.services.catalog is not None:
            with contextlib.suppress(Exception):
                plan = await r.services.catalog.get_planogram(first.store_id)
                if planogram_hash(plan) != first.data_hash:
                    log.warning("sim data_hash differs from the gateway planogram; reset will push the planogram")
        session_id = new_id("sim")
        lock = asyncio.Lock()

        async def send_text(text: str) -> None:
            async with lock:
                await ws.send_text(text)

        await send_text(SimWelcome(ok=True, session_id=session_id).model_dump_json())
        r.sim_link.attach(send_text, session_id=session_id, worker_id=first.worker_id, store_id=first.store_id)
        robot = r.services.robot
        if robot is not None and hasattr(robot, "attach_link"):
            robot.attach_link(r.sim_link)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = parse_sim_message(raw)
                except (ValidationError, ValueError) as exc:
                    log.warning("bad sim message: %s", exc)
                    continue
                await r.sim_link.dispatch(msg)
        except WebSocketDisconnect:
            pass
        finally:
            if r.sim_link.session_id == session_id:
                r.sim_link.detach("disconnected")

    return app


# ----------------------------------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------------------------------
def _event(type_: str, trip_id: str | None, **payload: Any) -> Any:
    from contracts.events import make_event

    return make_event(type_, trip_id=trip_id, source="gateway", **payload)


def _error_dict(trip_id: str | None, message: str) -> dict[str, Any]:
    return event_to_dict(_event("error", trip_id, arm="gateway", message=message))


async def _run_pair(ws: WebSocket, *coros: Any) -> None:
    """Run sender + receiver until either ends (disconnect), then cancel the other."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect | RuntimeError):
                log.warning("websocket task ended: %r", exc)
    finally:
        for t in tasks:
            t.cancel()
        with contextlib.suppress(Exception):
            await ws.close()


async def _handle_client_message(r: Registry, ws: WebSocket, trip_id: str | None, raw: str) -> None:
    """Client -> server on /ws: api.ClientMessage (chat | approval | ping) plus the gateway extension
    ``{"type": "login", "user_id": ...}`` (robot login; same as POST /api/trips/{id}/login)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await ws.send_json(_error_dict(trip_id, "invalid JSON"))
        return
    if isinstance(data, dict) and data.get("type") == "login":
        if trip_id is None:
            await ws.send_json(_error_dict(None, "login needs ?trip_id="))
            return
        try:
            await r.orchestrator.login(
                trip_id, RobotLoginRequest(user_id=data.get("user_id") or "", robot_id="robot-1")
            )
        except Exception as exc:  # noqa: BLE001
            await ws.send_json(_error_dict(trip_id, f"login failed: {exc}"))
        return
    try:
        msg = parse_client_message(data)
    except ValidationError as exc:
        await ws.send_json(_error_dict(trip_id, f"bad client message: {exc.errors()[:1]}"))
        return
    if isinstance(msg, WsChat):
        if trip_id is None:
            await ws.send_json(_error_dict(None, "chat needs ?trip_id="))
            return
        asyncio.ensure_future(r.orchestrator.chat(trip_id, msg.text, msg.channel))  # noqa: RUF006
    elif isinstance(msg, WsApproval):
        resp = msg.response
        problem = r.approvals.check(resp) if resp.channel == "web" else None
        if problem is not None or not await r.approvals.resolve(resp):
            await ws.send_json(
                _error_dict(trip_id, f"approval {resp.approval_id} not accepted ({problem or 'unknown'})")
            )
    # ping: nothing to do (the receive itself keeps the socket alive)


async def _deepgram_stt(key: str, audio: bytes, mime: str) -> str:
    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(
            "https://api.deepgram.com/v1/listen",
            params={"model": "nova-3", "smart_format": "true"},
            headers={"Authorization": f"Token {key}", "Content-Type": mime},
            content=audio,
        )
        res.raise_for_status()
        data = res.json()
    return data["results"]["channels"][0]["alternatives"][0]["transcript"]


async def _deepgram_tts(key: str, text: str) -> str:
    import base64

    async with httpx.AsyncClient(timeout=20) as client:
        res = await client.post(
            "https://api.deepgram.com/v1/speak",
            params={"model": "aura-2-thalia-en"},
            headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
            json={"text": text},
        )
        res.raise_for_status()
    return base64.b64encode(res.content).decode("ascii")


app = create_app()
"""``uvicorn arms.gateway.app:app`` (one worker)."""


def main(argv: list[str] | None = None) -> None:
    """``python -m arms.gateway.app [--reload]`` (= ``make dev``): host/port from settings (.env), not the shell."""
    import sys

    import uvicorn

    args = sys.argv[1:] if argv is None else argv
    settings = get_settings()
    uvicorn.run(
        "arms.gateway.app:app", host=settings.gateway_host, port=settings.gateway_port, reload="--reload" in args
    )


if __name__ == "__main__":
    main()
