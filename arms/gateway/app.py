"""FastAPI gateway: every endpoint in contracts/api.md for the web app (shopper, store-staff and fleet views).

- REST: requests (NL -> fleet), fleet robots / brains / goals, categories, stock management, cameras (JPEG snapshot +
  MJPEG ``<img>`` stream + observe), sim status, trips, approvals, profiles, mandates, merchant, trust ledger.
- WebSockets: ``/ws`` (events, filters trip_id / store_id / robot_id / user_id / request_id, repeatable; client
  actions), ``/ws/frames`` (live view of one robot: camera.frame for every camera + full-rate robot.state),
  ``/ws/cameras/{id}`` and the multiplexed ``/ws/cameras`` (camera grid), ``/ws/sim`` (Isaac workers -> SimLink).
- ``/debug``: no-build debug page (fleet, cameras, stock alerts, NL request box, trip view).

Every GET response carries ``X-Event-Seq`` (bus ``last_seq`` read BEFORE the handler ran) for the sync recipe in
api.md. Errors: KeyError -> 404, ValueError (rule violation) -> 409, validation -> 422, RobotError -> 409.

Run: ``make dev`` (= ``uvicorn arms.gateway.app:app --reload``). One process, one worker: the event bus is in-process.
"""

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from pydantic import ValidationError

from arms.gateway.loop_api import mount_loop_routes
from arms.gateway.orchestrator import OrchestratorConfig, TripOrchestrator
from arms.gateway.registry import NOT_WIRED, Registry, build_registry
from arms.gateway.wireless_api import mount_wireless_routes
from contracts import CONTRACTS_VERSION
from contracts.api import (
    ApprovalDecisionRequest,
    CancelGoalRequest,
    CheckoutStatus,
    CreateGoalRequest,
    CreateTripRequest,
    HealthStatus,
    ObserveRequest,
    PostMessageRequest,
    RobotLoginRequest,
    RobotMessageRequest,
    StopRobotRequest,
    VoiceTurnResponse,
    WsApproval,
    WsChat,
    WsRobotChat,
    parse_client_message,
)
from contracts.cameras import ROBOT_CAMERA_KINDS, CameraInfo, VisionObservation, robot_camera_id
from contracts.catalog import (
    STAFF_STOCK_REASONS,
    CategoryInfo,
    Deal,
    MerchantDashboard,
    NearExpiryItem,
    RestockRequest,
    ScanApplyResult,
    SearchHit,
    SearchQuery,
    StockAdjustment,
    StockAlert,
    StockChangeResult,
    StockLevel,
    StockMovement,
    StockReason,
    StockStatus,
    StoreInfo,
)
from contracts.common import STAFF_USER_ID
from contracts.events import WS_FILTER_KEYS, TripSnapshot, event_json_schema, event_matches, make_event
from contracts.fleet import (
    BrainReply,
    BrainStatus,
    BrainStep,
    FleetRequest,
    Goal,
    GoalKind,
    GoalSpec,
    GoalStatus,
    RequestPlan,
    RobotInfo,
)
from contracts.messaging import InboundMessage, OutboundMessage, ReplyInterpretation
from contracts.perception import ScanReport
from contracts.planogram import Planogram
from contracts.profile import Feedback, ProfileUpdate, UserProfile
from contracts.routing import GridSpec
from contracts.sim import (
    RobotError,
    SimHello,
    SimStatus,
    SimWelcome,
    parse_sim_message,
    planogram_hash,
    versions_compatible,
)
from contracts.tools import AgentReply
from contracts.trip import Trip
from contracts.trust import ApprovalRequest, ApprovalResponse, IntentMandate, TrustLedger
from utils.config import get_settings
from utils.events import event_to_dict
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)
STATIC_DIR = Path(__file__).parent / "static"
MJPEG_BOUNDARY = "frame"
MAX_STREAM_FPS = 15.0
MAX_MUX_FPS = 5.0
"""Per-camera cap on the multiplexed /ws/cameras socket."""


def create_app(registry: Registry | None = None, orchestrator_config: OrchestratorConfig | None = None) -> FastAPI:
    """App factory. ``registry=None`` builds one from settings in the lifespan (``PICKY_REAL_ARMS``)."""
    state: dict[str, Any] = {"registry": registry}

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if state["registry"] is None:
            # the fake messaging's scripted shopper would answer every approval (checkout included) within 0.3 s, so
            # a person could never approve from the web app: opt-in only (PICKY_SCRIPTED_SHOPPER=1); make demo scripts
            scripted = get_settings().picky_scripted_shopper
            state["registry"] = build_registry(
                fake_opts={"messaging": {"reply_mode": "auto" if scripted else "off"}},
                orchestrator_config=orchestrator_config,
            )
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
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Event-Seq"],
    )

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
            raise HTTPException(503, f"{name} service not wired")
        return s

    def trip_or_404(trip_id: str) -> Trip:
        try:
            return orch().peek(trip_id)
        except KeyError:
            raise HTTPException(404, f"unknown trip {trip_id}") from None

    # ------------------------------------------------------------------------------------------------- error mapping
    @app.exception_handler(KeyError)
    async def _key_error(_: Request, exc: KeyError) -> JSONResponse:
        detail = str(exc.args[0]) if exc.args else "not found"
        return JSONResponse({"detail": f"not found: {detail}"}, status_code=404)

    @app.exception_handler(ValidationError)
    async def _validation_error(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ValueError)
    async def _value_error(_: Request, exc: ValueError) -> JSONResponse:
        if isinstance(exc, ValidationError):
            return JSONResponse({"detail": str(exc)}, status_code=422)
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(RobotError)
    async def _robot_error(_: Request, exc: RobotError) -> JSONResponse:
        status = 404 if exc.code == "unknown_robot" else 409
        return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=status)

    @app.middleware("http")
    async def _event_seq_header(request: Request, call_next: Any) -> Any:
        r = state["registry"]
        seq = r.bus.last_seq if r is not None else 0
        response = await call_next(request)
        if request.method == "GET":
            response.headers["X-Event-Seq"] = str(seq)
        return response

    # ------------------------------------------------------------------------------------------------------ health
    @app.get("/health", response_model=HealthStatus)
    async def health() -> HealthStatus:
        r = reg()
        details = {arm: mode for arm, mode in r.modes.items()}
        details.update({f"{arm}_note": note for arm, note in r.notes.items()})
        details["exec_mode"] = r.exec_mode  # direct | plan | tools (the planner -> VLA -> verifier loop)
        faults = r.services.faults
        if faults is not None and getattr(faults, "scenario", None):
            details["fault_scenario"] = str(faults.scenario)
        degraded = any(m not in ("real", "fake", "override", NOT_WIRED) for m in r.modes.values())
        robots = 0
        fleet = r.services.fleet
        if fleet is not None:
            with contextlib.suppress(Exception):
                robots = len(await fleet.list_robots())
        if not robots:
            with contextlib.suppress(Exception):
                from utils.data import load_robots

                robots = len(load_robots())
        return HealthStatus(
            status="degraded" if degraded else "ok",
            contracts_version=CONTRACTS_VERSION,
            env=get_settings().picky_env,
            real_arms=sorted(a for a, m in r.modes.items() if m == "real"),
            sim_connected=r.sim_link.is_connected(),
            robots=robots,
            details=details,
        )

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/debug")

    @app.get("/debug", include_in_schema=False)
    async def debug_page() -> FileResponse:
        return FileResponse(STATIC_DIR / "debug.html", media_type="text/html", headers={"Cache-Control": "no-store"})

    # --------------------------------------------------------------------------------------- NL requests + fleet
    @app.post("/api/requests", response_model=RequestPlan)
    async def submit_request(req: FleetRequest) -> RequestPlan:
        if "role" not in req.model_fields_set and req.user_id == STAFF_USER_ID:
            req = req.model_copy(update={"role": "staff"})  # the staff account is staff unless told otherwise
        return await svc("fleet").submit_request(req)

    @app.get("/api/requests", response_model=list[RequestPlan])
    async def list_requests(user_id: str | None = None, limit: int = 20) -> list[RequestPlan]:
        return await svc("fleet").list_requests(user_id=user_id, limit=max(1, min(limit, 200)))

    @app.get("/api/requests/{request_id}", response_model=RequestPlan)
    async def get_request(request_id: str) -> RequestPlan:
        return await svc("fleet").get_request(request_id)

    @app.get("/api/fleet/robots", response_model=list[RobotInfo])
    async def list_robots(store_id: str | None = None) -> list[RobotInfo]:
        return await svc("fleet").list_robots(store_id)

    @app.get("/api/fleet/robots/{robot_id}", response_model=RobotInfo)
    async def get_robot(robot_id: str) -> RobotInfo:
        return await svc("fleet").get_robot(robot_id)

    @app.get("/api/fleet/robots/{robot_id}/brain", response_model=BrainStatus)
    async def robot_brain(robot_id: str) -> BrainStatus:
        await svc("fleet").get_robot(robot_id)  # 404 for unknown robots
        return await svc("brain").status(robot_id)

    @app.get("/api/fleet/robots/{robot_id}/steps", response_model=list[BrainStep])
    async def robot_steps(
        robot_id: str, goal_id: str | None = None, limit: int = 50, before: datetime | None = None
    ) -> list[BrainStep]:
        return await svc("brain").recent_steps(
            robot_id=robot_id, goal_id=goal_id, limit=max(1, min(limit, 500)), before=before
        )

    @app.post("/api/fleet/robots/{robot_id}/messages", response_model=BrainReply)
    async def message_robot(robot_id: str, req: RobotMessageRequest) -> BrainReply:
        return await svc("fleet").message_robot(robot_id, req.text, req.user_id)

    @app.post("/api/fleet/robots/{robot_id}/goals", response_model=Goal)
    async def robot_goal(robot_id: str, req: CreateGoalRequest) -> Goal:
        await svc("fleet").get_robot(robot_id)
        return await svc("fleet").create_goal(req.to_spec(robot_id, req.user_id))

    @app.post("/api/fleet/robots/{robot_id}/stop", response_model=RobotInfo)
    async def stop_robot(robot_id: str, req: StopRobotRequest | None = Body(None)) -> RobotInfo:  # noqa: B008
        reason = req.reason if req is not None else StopRobotRequest().reason
        return await svc("fleet").stop_robot(robot_id, reason)

    @app.post("/api/fleet/robots/{robot_id}/resume", response_model=RobotInfo)
    async def resume_robot(robot_id: str) -> RobotInfo:
        fleet = svc("fleet")
        resume = getattr(fleet, "resume_robot", None)  # gateway extra (not in the FleetService Protocol)
        if resume is None:
            return await fleet.get_robot(robot_id)
        return await resume(robot_id)

    @app.post("/api/goals", response_model=Goal)
    async def create_goal(spec: GoalSpec) -> Goal:
        return await svc("fleet").create_goal(spec)

    @app.get("/api/goals", response_model=list[Goal])
    async def list_goals(
        store_id: str | None = None,
        robot_id: str | None = None,
        status: list[GoalStatus] | None = Query(None),  # noqa: B008
        active: bool | None = None,
        kind: GoalKind | None = None,
        request_id: str | None = None,
        trip_id: str | None = None,
        limit: int = 50,
    ) -> list[Goal]:
        return await svc("fleet").list_goals(
            store_id,
            robot_id,
            status or None,
            active=active,
            kind=kind,
            request_id=request_id,
            trip_id=trip_id,
            limit=max(1, min(limit, 500)),
        )

    @app.get("/api/goals/{goal_id}", response_model=Goal)
    async def get_goal(goal_id: str) -> Goal:
        return await svc("fleet").get_goal(goal_id)

    @app.get("/api/goals/{goal_id}/steps", response_model=list[BrainStep])
    async def goal_steps(goal_id: str, limit: int = 50, before: datetime | None = None) -> list[BrainStep]:
        await svc("fleet").get_goal(goal_id)
        return await svc("brain").recent_steps(goal_id=goal_id, limit=max(1, min(limit, 500)), before=before)

    @app.post("/api/goals/{goal_id}/cancel", response_model=Goal)
    async def cancel_goal(goal_id: str, req: CancelGoalRequest | None = Body(None)) -> Goal:  # noqa: B008
        reason = req.reason if req is not None else CancelGoalRequest().reason
        return await svc("fleet").cancel_goal(goal_id, reason)

    @app.get("/api/sim/status", response_model=SimStatus)
    async def sim_status() -> SimStatus:
        return await _sim_status(reg())

    # ------------------------------------------------------------------------------------ categories + stock
    @app.get("/api/categories", response_model=list[CategoryInfo])
    async def list_categories(store_id: str | None = None) -> list[CategoryInfo]:
        return await svc("catalog").list_categories(store_id)

    @app.get("/api/categories/{category_id}/products", response_model=list[SearchHit])
    async def category_products(category_id: str, store_id: str | None = None) -> list[SearchHit]:
        return await svc("catalog").category_products(category_id, store_id)

    @app.get("/api/stores/{store_id}/stock", response_model=list[StockLevel])
    async def stock_levels(
        store_id: str, status: StockStatus | None = None, category: str | None = None, q: str | None = None
    ) -> list[StockLevel]:
        return await svc("catalog").stock_levels(store_id, status=status, category=category, q=q or None)

    @app.get("/api/stores/{store_id}/stock/alerts", response_model=list[StockAlert])
    async def stock_alerts(store_id: str) -> list[StockAlert]:
        return await svc("catalog").stock_alerts(store_id)

    @app.post("/api/stores/{store_id}/stock/restock", response_model=StockChangeResult)
    async def restock(store_id: str, req: RestockRequest) -> StockChangeResult:
        return await svc("catalog").restock(store_id, req)

    @app.post("/api/stores/{store_id}/stock/adjust", response_model=StockChangeResult)
    async def adjust_stock(store_id: str, req: StockAdjustment) -> StockChangeResult:
        if req.reason not in STAFF_STOCK_REASONS:
            raise HTTPException(422, f"reason must be one of {list(STAFF_STOCK_REASONS)} (got {req.reason!r})")
        return await svc("catalog").adjust_stock(store_id, req)

    @app.get("/api/stores/{store_id}/stock/history", response_model=list[StockMovement])
    async def stock_history(
        store_id: str,
        sku: str | None = None,
        limit: int = 100,
        before: datetime | None = None,
        reason: StockReason | None = None,
        actor: str | None = None,
    ) -> list[StockMovement]:
        return await svc("catalog").stock_history(
            store_id, sku=sku, limit=max(1, min(limit, 1000)), before=before, reason=reason, actor=actor
        )

    # ---------------------------------------------------------------------------------------------- cameras
    @app.get("/api/cameras", response_model=list[CameraInfo])
    async def list_cameras(store_id: str | None = None, robot_id: str | None = None) -> list[CameraInfo]:
        return await svc("cameras").list_cameras(store_id=store_id, robot_id=robot_id)

    @app.get("/api/cameras/{camera_id}/snapshot.jpg", response_class=Response)
    async def camera_snapshot(camera_id: str, w: int | None = None, t: str | None = None) -> Response:
        cams = svc("cameras")
        try:
            frame = await cams.snapshot(camera_id, max_width=_width(w))
        except KeyError:
            raise HTTPException(404, f"unknown camera {camera_id}") from None
        except Exception as exc:  # noqa: BLE001 - camera offline / no frame yet
            raise HTTPException(503, f"no frame from {camera_id}: {exc}") from None
        jpeg = _jpeg_bytes(frame)
        if jpeg is None:
            raise HTTPException(503, f"no blurred frame from {camera_id}")
        return Response(content=jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/cameras/{camera_id}/stream.mjpg", response_class=StreamingResponse)
    async def camera_stream(camera_id: str, request: Request, fps: float = 5.0, w: int | None = None) -> Any:
        cams = svc("cameras")
        try:
            await cams.get_camera(camera_id)
        except KeyError:
            raise HTTPException(404, f"unknown camera {camera_id}") from None
        rate = max(0.2, min(fps, MAX_STREAM_FPS))
        return StreamingResponse(
            _mjpeg(cams, camera_id, rate, _width(w), request),
            media_type=f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
                "Pragma": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/api/cameras/{camera_id}/observe", response_model=VisionObservation)
    async def camera_observe(camera_id: str, req: ObserveRequest | None = Body(None)) -> VisionObservation:  # noqa: B008
        question = req.question if req is not None else None
        return await svc("cameras").observe(camera_id, question)

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
        return await svc("catalog").apply_scan(report)  # stock.* events come from the catalog

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
    async def list_approvals(
        trip_id: str | None = None,
        goal_id: str | None = None,
        robot_id: str | None = None,
        store_id: str | None = None,
        user_id: str | None = None,
    ) -> list[ApprovalRequest]:
        return reg().approvals.pending(trip_id, goal_id=goal_id, robot_id=robot_id, store_id=store_id, user_id=user_id)

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
            note=req.note,
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

    # ------------------------------------------------------------------------- planner -> VLA -> verifier loop
    mount_loop_routes(app, reg)
    mount_wireless_routes(app, reg)

    @app.get("/api/_schema/events")
    async def events_schema() -> dict[str, Any]:
        return event_json_schema()

    # ------------------------------------------------------------------------------------------------------ websockets
    @app.websocket("/ws")
    async def ws_events(ws: WebSocket) -> None:
        await ws.accept()
        r = reg()
        filters = {k: ws.query_params.getlist(k) for k in WS_FILTER_KEYS if ws.query_params.getlist(k)}
        raw_after = ws.query_params.get("after_seq")
        after_seq = int(raw_after) if raw_after not in (None, "") and raw_after.lstrip("-").isdigit() else None
        trip_ids = filters.get("trip_id", [])
        sub = r.bus.subscribe(None, name=f"ws:{json.dumps(filters, sort_keys=True)}")
        sender_lock = asyncio.Lock()

        async def send(data: dict[str, Any]) -> None:
            async with sender_lock:
                await ws.send_json(data)

        async def sender() -> None:
            start = r.bus.last_seq
            sent_upto = start
            for tid in trip_ids:
                try:
                    trip = await r.orchestrator.get_trip(tid)
                    snap = TripSnapshot(trip=trip, seq=start, source="gateway", **_trip_env(trip))
                    await send(event_to_dict(snap))
                except KeyError:
                    await send(_error_dict(tid, f"unknown trip {tid}"))
            replay_from = after_seq
            if replay_from is None and (trip_ids or filters.get("request_id")):
                replay_from = 0
            if replay_from is not None:
                for ev in r.bus.recent(after_seq=replay_from):
                    if ev.seq > start:
                        break
                    if event_matches(ev, filters):
                        await send(event_to_dict(ev))
            async for ev in sub:
                if ev.seq <= sent_upto or not event_matches(ev, filters):
                    continue
                sent_upto = ev.seq
                await send(event_to_dict(ev))

        async def receiver() -> None:
            while True:
                raw = await ws.receive_text()
                await _handle_client_message(r, send, trip_ids, raw)

        await _run_pair(ws, sender(), receiver())
        sub.close()

    @app.websocket("/ws/frames")
    async def ws_frames(ws: WebSocket) -> None:
        """Live view of ONE robot (``?robot_id=`` or the robot executing ``?trip_id=``): camera.frame for every camera
        of the robot + full-rate robot.state; legacy ``frame`` (front only) instead of its camera.frame with
        ``?legacy=1``."""
        await ws.accept()
        r = reg()
        trip_id = ws.query_params.get("trip_id")
        robot_id = ws.query_params.get("robot_id")
        legacy = ws.query_params.get("legacy") in ("1", "true", "yes")
        width = _width(_int(ws.query_params.get("w")))
        fps = max(0.2, min(_float(ws.query_params.get("fps"), 5.0), MAX_STREAM_FPS))
        lock = asyncio.Lock()

        async def send(data: dict[str, Any]) -> None:
            async with lock:
                await ws.send_json(data)

        async def resolve_robot() -> str | None:
            if robot_id:
                return robot_id
            while trip_id:
                with contextlib.suppress(KeyError):
                    trip = r.orchestrator.peek(trip_id)
                    if trip.robot_id:
                        return trip.robot_id
                    if trip.state.value in ("done", "failed"):
                        return None
                await asyncio.sleep(0.5)
            return None

        async def sender() -> None:
            rid = await resolve_robot()
            if rid is None and not trip_id:
                await send(_error_dict(None, "/ws/frames needs ?robot_id= or ?trip_id="))
                return
            sub = r.frames.subscribe(trip_id if rid is None else None, rid)
            tasks: list[asyncio.Task[Any]] = []
            try:
                if rid is not None:
                    for cid in await _robot_cameras(r, rid):
                        as_legacy = legacy and cid.endswith(".front")
                        tasks.append(asyncio.ensure_future(_pump_camera(r, cid, fps, width, send, trip_id, as_legacy)))
                while True:
                    ev = await sub.get()
                    if ev.type in ("robot.state", "camera.overlay") or (legacy and ev.type == "frame"):
                        await send(event_to_dict(ev))
            finally:
                sub.close()
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        await _run_pair(ws, sender(), _drain(ws))

    @app.websocket("/ws/cameras/{camera_id}")
    async def ws_camera(ws: WebSocket, camera_id: str) -> None:
        await ws.accept()
        r = reg()
        fps = max(0.2, min(_float(ws.query_params.get("fps"), 5.0), MAX_STREAM_FPS))
        width = _width(_int(ws.query_params.get("w")))
        lock = asyncio.Lock()

        async def send(data: dict[str, Any]) -> None:
            async with lock:
                await ws.send_json(data)

        await _run_pair(
            ws, _pump_camera(r, camera_id, fps, width, send), _pump_overlays(r, {camera_id}, send), _drain(ws)
        )

    @app.websocket("/ws/cameras")
    async def ws_cameras(ws: WebSocket) -> None:
        """Multiplexed camera grid: ``?camera_id=a&camera_id=b`` | ``store_id=`` | ``robot_id=`` (+ fps <= 5, w)."""
        await ws.accept()
        r = reg()
        fps = max(0.2, min(_float(ws.query_params.get("fps"), 2.0), MAX_MUX_FPS))
        width = _width(_int(ws.query_params.get("w")))
        camera_ids = ws.query_params.getlist("camera_id")
        store_ids = ws.query_params.getlist("store_id")
        robot_ids = ws.query_params.getlist("robot_id")
        lock = asyncio.Lock()

        async def send(data: dict[str, Any]) -> None:
            async with lock:
                await ws.send_json(data)

        async def mux() -> None:
            ids = list(camera_ids)
            cams = r.services.cameras
            if cams is not None and (store_ids or robot_ids or not ids):
                listed: list[CameraInfo] = []
                for sid in store_ids or [None]:
                    for rid in robot_ids or [None]:
                        with contextlib.suppress(Exception):
                            listed += await cams.list_cameras(store_id=sid, robot_id=rid)
                ids += [c.camera_id for c in listed if c.camera_id not in ids]
            if not ids:
                await send(_error_dict(None, "no cameras match"))
                return
            tasks = [asyncio.ensure_future(_pump_camera(r, cid, fps, width, send)) for cid in ids]
            tasks.append(asyncio.ensure_future(_pump_overlays(r, set(ids), send)))
            try:
                await asyncio.gather(*tasks)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        await _run_pair(ws, mux(), _drain(ws))

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
        await _log_stale_stores(r, first)
        session_id = new_id("sim")
        lock = asyncio.Lock()

        async def send_text(text: str) -> None:
            async with lock:
                await ws.send_text(text)

        await send_text(SimWelcome(ok=True, session_id=session_id).model_dump_json())
        robot = r.services.robot
        if robot is not None and hasattr(robot, "attach_link"):
            with contextlib.suppress(Exception):
                robot.attach_link(r.sim_link)
        await r.sim_link.accept(first, send_text, session_id)
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = parse_sim_message(raw)
                except (ValidationError, ValueError) as exc:
                    log.warning("bad sim message: %s", exc)
                    continue
                await r.sim_link.dispatch(msg, session_id)
        except WebSocketDisconnect:
            pass
        finally:
            r.sim_link.detach("disconnected", session_id=session_id)

    return app


# ----------------------------------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------------------------------
def _int(raw: str | None) -> int | None:
    try:
        return int(raw) if raw not in (None, "") else None
    except ValueError:
        return None


def _float(raw: str | None, default: float) -> float:
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _width(w: int | None) -> int | None:
    return max(16, min(w, 4096)) if w else None


def _jpeg_bytes(frame: Any) -> bytes | None:
    """JPEG bytes of a frame, ONLY if it is face-blurred (CLAUDE.md §8: never serve a raw frame)."""
    if frame is None or not getattr(frame, "blurred", False) or not getattr(frame, "jpeg_b64", ""):
        return None
    try:
        return base64.b64decode(frame.jpeg_b64)
    except (ValueError, TypeError):
        return None


async def _mjpeg(cams: Any, camera_id: str, fps: float, width: int | None, request: Request) -> AsyncIterator[bytes]:
    """multipart/x-mixed-replace body: one JPEG part per blurred frame (works as ``<img src>``)."""
    it = cams.stream(camera_id, fps=fps, max_width=width)
    try:
        async for frame in it:
            if await request.is_disconnected():
                break
            jpeg = _jpeg_bytes(frame)
            if jpeg is None:
                continue
            head = f"--{MJPEG_BOUNDARY}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
            yield head.encode("ascii") + jpeg + b"\r\n"
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - end the stream; the <img> shows its last frame
        log.warning("mjpeg stream %s ended: %s", camera_id, exc)
    finally:
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


async def _robot_cameras(r: Registry, robot_id: str) -> list[str]:
    cams = r.services.cameras
    if cams is not None:
        with contextlib.suppress(Exception):
            listed = await cams.list_cameras(robot_id=robot_id)
            if listed:
                return [c.camera_id for c in listed]
    return [robot_camera_id(robot_id, k) for k in ROBOT_CAMERA_KINDS]


async def _pump_camera(
    r: Registry,
    camera_id: str,
    fps: float,
    width: int | None,
    send: Any,
    trip_id: str | None = None,
    legacy: bool = False,
) -> None:
    """cameras.stream -> camera.frame events (or the legacy ``frame``) on one socket, latest-only, blurred only."""
    cams = r.services.cameras
    if cams is None:
        await send(_error_dict(trip_id, "cameras service not wired"))
        return
    it = cams.stream(camera_id, fps=fps, max_width=width)
    try:
        async for frame in it:
            if not getattr(frame, "blurred", False):
                continue  # never send an unblurred frame
            env = {"trip_id": trip_id, "store_id": frame.store_id, "robot_id": frame.robot_id}
            try:
                if legacy:
                    ev = make_event("frame", source="gateway", frame=frame, **env)
                else:
                    ev = make_event("camera.frame", source="gateway", camera_id=camera_id, frame=frame, **env)
            except ValueError:
                continue
            await send(event_to_dict(ev))
    except asyncio.CancelledError:
        raise
    except KeyError:
        await send(_error_dict(trip_id, f"unknown camera {camera_id}"))
    except Exception as exc:  # noqa: BLE001
        log.warning("camera %s stream ended: %s", camera_id, exc)
    finally:
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            with contextlib.suppress(Exception):
                await aclose()


async def _pump_overlays(r: Registry, camera_ids: set[str], send: Any) -> None:
    """Live-only ``camera.overlay`` events (the loop's evidence boxes) for these cameras, latest-only."""
    sub = r.frames.subscribe()
    try:
        while True:
            ev = await sub.get()
            if getattr(ev, "type", None) == "camera.overlay" and ev.camera_id in camera_ids:
                await send(event_to_dict(ev))
    finally:
        sub.close()


async def _sim_status(r: Registry) -> SimStatus:
    """SimLink status completed with the robot arm's per-robot backend (isaac vs 2D fake)."""
    status = r.sim_link.status()
    robot = r.services.robot
    if robot is None:
        return status
    try:
        states = await robot.list_robots()
    except Exception:  # noqa: BLE001
        return status
    isaac_r, fake_r, isaac_s, fake_s = [], [], set(), set()
    for st in states:
        rid = getattr(st, "robot_id", None)
        if rid is None:
            continue
        if getattr(st, "backend", None) == "isaac":
            isaac_r.append(rid)
            if st.store_id:
                isaac_s.add(st.store_id)
        else:
            fake_r.append(rid)
            if st.store_id:
                fake_s.add(st.store_id)
    return status.model_copy(
        update={
            "isaac_robot_ids": sorted(isaac_r),
            "fake_robot_ids": sorted(fake_r),
            "isaac_store_ids": sorted(isaac_s),
            "fake_store_ids": sorted(fake_s - isaac_s),
        }
    )


async def _log_stale_stores(r: Registry, hello: SimHello) -> None:
    catalog = r.services.catalog
    if catalog is None:
        return
    stores = list(hello.stores)
    for st in stores:
        if not st.data_hash:
            continue
        with contextlib.suppress(Exception):
            plan = await catalog.get_planogram(st.store_id)
            if planogram_hash(plan) != st.data_hash:
                log.warning("sim store %s is stale (data_hash differs); the robot arm resets it", st.store_id)


def _trip_env(trip: Trip) -> dict[str, Any]:
    from contracts.events import trip_envelope

    return trip_envelope(trip)


def _error_dict(trip_id: str | None, message: str) -> dict[str, Any]:
    return event_to_dict(make_event("error", trip_id=trip_id, source="gateway", arm="gateway", message=message))


async def _drain(ws: WebSocket) -> None:
    while True:
        await ws.receive_text()  # ignore; detects disconnect


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


async def _handle_client_message(r: Registry, send: Any, trip_ids: list[str], raw: str) -> None:
    """Client -> server on /ws: api.ClientMessage (chat | approval | robot_chat | ping) plus the gateway extension
    ``{"type": "login", "user_id": ..., "robot_id"?: ...}`` (robot login; same as POST /api/trips/{id}/login)."""
    single = trip_ids[0] if len(trip_ids) == 1 else None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await send(_error_dict(single, "invalid JSON"))
        return
    if isinstance(data, dict) and data.get("type") == "login":
        tid = data.get("trip_id") or single
        if tid is None:
            await send(_error_dict(None, "login needs ?trip_id= (exactly one) or trip_id"))
            return
        try:
            req = RobotLoginRequest(user_id=data.get("user_id") or "", robot_id=data.get("robot_id"))
            await r.orchestrator.login(tid, req)
        except Exception as exc:  # noqa: BLE001
            await send(_error_dict(tid, f"login failed: {exc}"))
        return
    try:
        msg = parse_client_message(data)
    except ValidationError as exc:
        await send(_error_dict(single, f"bad client message: {exc.errors()[:1]}"))
        return
    if isinstance(msg, WsChat):
        tid = msg.trip_id or single
        if tid is None:
            await send(_error_dict(None, "chat needs trip_id (or exactly one ?trip_id= filter)"))
            return
        asyncio.ensure_future(r.orchestrator.chat(tid, msg.text, msg.channel))  # noqa: RUF006
    elif isinstance(msg, WsRobotChat):
        fleet = r.services.fleet
        if fleet is None:
            await send(_error_dict(None, "fleet not wired"))
            return

        async def talk() -> None:
            try:
                await fleet.message_robot(msg.robot_id, msg.text, msg.user_id)
            except Exception as exc:  # noqa: BLE001
                await send(_error_dict(None, f"robot chat failed: {exc}"))

        asyncio.ensure_future(talk())  # noqa: RUF006
    elif isinstance(msg, WsApproval):
        resp = msg.response
        problem = r.approvals.check(resp) if resp.channel == "web" else None
        if problem is not None or not await r.approvals.resolve(resp):
            await send(_error_dict(single, f"approval {resp.approval_id} not accepted ({problem or 'unknown'})"))
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
