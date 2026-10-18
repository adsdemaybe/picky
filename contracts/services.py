"""Service Protocols and the dependency-injection container.

Rules (CLAUDE.md §4):
- Each arm's ``create(services)`` / ``create_fake(services)`` returns an object satisfying its Protocol below. An arm
  may provide extra services through extra factories (``SERVICE_FACTORIES``): perception also provides ``cameras``
  (``create_cameras`` / ``create_cameras_fake``), agent also provides ``brain`` (``create_brain`` /
  ``create_brain_fake``).
- Factories must NOT call other services at construction time (only at call time), so wiring order is irrelevant.
- Arms never import other arms; they reach each other only through ``services.<field>`` at call time.
- The gateway owns the container, the Trip (``TripController``), the ApprovalBroker, the EventPublisher and the
  SimLink. Arms never mutate a ``Trip`` they are handed; every trip change goes through ``services.trips``.
- The FLEET owns every Goal / RequestPlan; the robot brain changes goals only through ``services.fleet.update_goal``.
- Multi-robot: every ``RobotService`` method takes ``robot_id`` first. Robot ids, names and docks come from
  data/fixtures/robots.yaml (``utils.data.load_robots``).
- Events: one producer per type (``contracts.events.EVENT_PRODUCERS``): gateway = trip flow + robot.state + live
  frames; fleet = request.* / goal.* / fleet.robot_updated; agent = agent.message + brain.step + in-store execution
  events of the goal its brain runs; catalog = stock.*; any arm = error. Shop-goal events carry envelope
  ``trip_id = goal.trip_id`` (``events.goal_envelope``); trip events use ``events.trip_envelope``.
- Errors at the HTTP edge (gateway): KeyError -> 404, ValueError (rule violation, e.g. restock over capacity) -> 409,
  pydantic ValidationError / bad enum -> 422.
- 0.2.1 (planner -> VLA -> verifier loop, additive): OPTIONAL ``Services`` fields ``planner`` / ``runner`` (agent),
  ``vla`` / ``truth`` / ``faults`` (robot), ``verifier`` / ``evidence`` (perception), all default None, built from
  ``LOOP_SERVICE_FACTORIES`` (kept apart from ``SERVICE_FACTORIES`` until the gateway wires them). ``exec_mode`` is the
  kill switch: anything but PICKY_EXEC_MODE=plan|tools with all four core loop services wired = the exact v0.2 path.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, fields
from typing import Any, Protocol, runtime_checkable

from contracts.api import AddLineResult, CheckoutStatus, CreateTripRequest
from contracts.bridge import ExecMode, ToolStepOutcome
from contracts.cameras import CameraInfo, VisionObservation
from contracts.catalog import (
    CategoryInfo,
    Deal,
    MerchantDashboard,
    NearExpiryItem,
    Product,
    ProductLocation,
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
    StoreOffer,
)
from contracts.common import (
    ApprovalId,
    CameraId,
    CameraName,
    CategoryId,
    Channel,
    GoalId,
    InstanceId,
    LineId,
    LotId,
    Pose2D,
    RequestId,
    RobotId,
    Sku,
    SlotId,
    StopId,
    StoreId,
    Timestamp,
    TripId,
    UserId,
)
from contracts.events import Event
from contracts.evidence import EvidenceCameras, TruthSource
from contracts.faults import FaultContext, FaultEffect, FaultFiring, FaultRule, FaultScenario
from contracts.fleet import (
    BrainReply,
    BrainStatus,
    BrainStep,
    FleetRequest,
    Goal,
    GoalItem,
    GoalKind,
    GoalSpec,
    GoalStatus,
    GoalUpdate,
    RequestDraft,
    RequesterRole,
    RequestPlan,
    RobotInfo,
)
from contracts.messaging import InboundMessage, OutboundMessage, ReplyInterpretation, SendResult
from contracts.perception import BasketCheck, PickChoice, ScanReport
from contracts.plan import (
    STALL_FACTOR,
    Condition,
    ExecId,
    PlanDraft,
    PlanId,
    PlanIssue,
    PlanRequest,
    PlanStep,
    PlanStepId,
    StepPlan,
)
from contracts.planogram import Planogram
from contracts.policy import PlanOutcome, PlanRunState, ReplanRequest, RetryPolicy, StepAttempt
from contracts.profile import Feedback, ProfileUpdate, PurchaseRecord, UserProfile
from contracts.routing import GridSpec, Route, RouteRequest, StopStatus
from contracts.shopping import Cart, CartLine, Intent, Recommendation, ShoppingList
from contracts.sim import CameraFrame, ItemInstance, RobotError, RobotState, SimLink
from contracts.tools import AgentReply
from contracts.trip import Trip
from contracts.trust import (
    ApprovalRequest,
    ApprovalResponse,
    CheckoutRequest,
    IntentMandate,
    MandateDecision,
    Receipt,
    TransparencyEntry,
    TrustLedger,
)
from contracts.verify import (
    GroundingResult,
    GroundRequest,
    Verdict,
    VerifierCapabilities,
    VerifierMode,
    VerifyBaseline,
    VerifyRequest,
    VisionAnswer,
    VisionExpect,
)
from contracts.vla import ExecRecord, VlaCapabilities, VlaError, VlaRequest, VlaStatus


@runtime_checkable
class CatalogService(Protocol):
    async def list_stores(self) -> list[StoreInfo]:
        """Physical stores + 'online' (with shipping_cents / free_shipping_over_cents / default_eta_hours)."""
        ...

    async def get_planogram(self, store_id: StoreId) -> Planogram:
        """Live planogram (stock reflects picks/scans). Raises KeyError for unknown / online stores."""
        ...

    async def search(self, query: SearchQuery) -> list[SearchHit]:
        """Search every requested store (default: all physical stores + 'online'); best hits first."""
        ...

    async def get_product(self, sku: Sku) -> Product | None: ...

    async def get_locations(self, sku: Sku, store_id: StoreId | None = None) -> list[ProductLocation]: ...

    async def get_offers(self, sku: Sku, store_ids: list[StoreId] | None = None) -> list[StoreOffer]:
        """Offers for one SKU across stores (default: all incl. 'online')."""
        ...

    async def get_deals(self, store_id: StoreId) -> list[Deal]: ...

    async def get_near_expiry(self, store_id: StoreId, within_days: int = 3) -> list[NearExpiryItem]: ...

    async def apply_scan(self, report: ScanReport) -> ScanApplyResult:
        """Update stock / freshness (per lot) / mispricing / location from a perception scan; records a StockMovement
        (reason 'scan', actor 'robot:<report.robot_id>', ref scan_id) when qty changed and publishes stock.*.

        Stock: ``count_is_exact`` -> qty = observed_count (lots from ``report.lot_counts``); not exact ->
        observed_count == 0 marks the slot empty, observed_count > 0 -> qty = max(qty, observed_count).
        """
        ...

    async def record_pick(
        self,
        store_id: StoreId,
        slot_id: SlotId,
        sku: Sku,
        qty: int = 1,
        trip_id: TripId | None = None,
        robot_id: RobotId | None = None,
        lot_id: LotId | None = None,
    ) -> ScanApplyResult:
        """Decrement stock after a robot picked (the robot brain's place_in_basket calls it, per unit); feeds
        HeatmapCell.picks. Decrements ``lot_id`` (the unit's ItemInstance.lot_id), else the earliest-expiry lot.
        Appends a StockMovement (reason 'pick', actor 'robot:<robot_id>', ref trip_id) and publishes stock.*."""
        ...

    async def merchant_dashboard(self, store_id: StoreId) -> MerchantDashboard: ...

    # ---- categories (data/categories.yaml) -------------------------------------------------------------------------
    async def list_categories(self, store_id: StoreId | None = None) -> list[CategoryInfo]:
        """Flat taxonomy in display order (top-level, then their subcategories) with product / stock counts for
        ``store_id`` (None = all physical stores). GET /api/categories."""
        ...

    async def category_products(self, category_id: CategoryId, store_id: StoreId | None = None) -> list[SearchHit]:
        """Products in a category or subcategory, with offers at ``store_id`` (None = every store incl. online).
        Raises KeyError for an unknown category. GET /api/categories/{id}/products."""
        ...

    # ---- stock management ("resource management") -----------------------------------------------------------------
    async def stock_levels(
        self,
        store_id: StoreId,
        status: StockStatus | None = None,
        category: CategoryId | None = None,
        q: str | None = None,
    ) -> list[StockLevel]:
        """One StockLevel per SKU stocked in the store (aggregated over slots; unit / size / image_url from the
        Product), filtered by status, category (matches Product.category OR Product.subcategory) and ``q``
        (case-insensitive substring of sku / name / brand). Worst first: out, low, ok; then name."""
        ...

    async def stock_alerts(self, store_id: StoreId) -> list[StockAlert]:
        """Active low / out alerts, out first."""
        ...

    async def restock(self, store_id: StoreId, request: RestockRequest) -> StockChangeResult:
        """Add units (new Lot), reason 'restock', up to capacity (``qty`` over the free capacity -> ValueError, nothing
        applied); clears the alert when back to ok. Publishes stock.updated (+ stock.alert), then best-effort
        ``services.robot.sync_stock(store_id, touched slot_ids)``. KeyError for an unknown store / sku not stocked."""
        ...

    async def adjust_stock(self, store_id: StoreId, adjustment: StockAdjustment) -> StockChangeResult:
        """Apply a delta / absolute count (damage, manual correction, cycle count) per the rules in contracts/catalog.py
        (qty within 0..capacity, set_qty needs slot_id for multi-slot SKUs; violations -> ValueError). Publishes
        stock.*, then best-effort ``services.robot.sync_stock``."""
        ...

    async def stock_history(
        self,
        store_id: StoreId,
        sku: Sku | None = None,
        limit: int = 100,
        before: Timestamp | None = None,
        reason: StockReason | None = None,
        actor: str | None = None,
    ) -> list[StockMovement]:
        """Movements, newest first (picks, scans, restocks, adjustments); ``before`` = only movements older than that
        ts (page with the last row's ts); ``actor`` matches exactly or by prefix ending in ':' ('robot:')."""
        ...


@runtime_checkable
class RecommenderService(Protocol):
    """Owns profiles. Every rec carries reason + signals + sponsored (default False) and ``trip_id`` when known."""

    async def get_profile(self, user_id: UserId) -> UserProfile:
        """Raises KeyError for unknown users."""
        ...

    async def find_user_by_handle(self, handle: str) -> UserProfile | None:
        """Profile whose phone or email equals ``messaging.normalize_handle(handle)`` (both sides normalized)."""
        ...

    async def save_profile(self, profile: UserProfile) -> UserProfile:
        """Replace editable preferences (web 'my preferences'); bumps version."""
        ...

    async def recommend(
        self,
        user_id: UserId,
        store_id: StoreId,
        shopping_list: ShoppingList,
        limit: int = 3,
        focus: str | None = None,
        exclude_skus: list[Sku] | None = None,
    ) -> list[Recommendation]:
        """Detour recommendations (kind='detour' or 'near_expiry_offer'), best first, each with reason+signals.

        Never recommends list/cart SKUs or ``exclude_skus`` (declined). trip_id = shopping_list.trip_id.
        """
        ...

    async def checkout_batch(
        self,
        user_id: UserId,
        store_id: StoreId,
        cart: Cart,
        limit: int = 3,
        focus: str | None = None,
        exclude_skus: list[Sku] | None = None,
    ) -> list[Recommendation]:
        """'Want any of these?' recs asked once at checkout (kind='checkout_batch'); trip_id = cart.trip_id."""
        ...

    async def near_expiry_offers(
        self,
        user_id: UserId,
        store_id: StoreId,
        limit: int = 3,
        focus: str | None = None,
        exclude_skus: list[Sku] | None = None,
        trip_id: TripId | None = None,
    ) -> list[Recommendation]:
        """Near-expiry markdowns, only for users likely to want them (kind='near_expiry_offer')."""
        ...

    async def record_feedback(self, feedback: Feedback) -> ProfileUpdate: ...

    async def record_purchase(self, records: list[PurchaseRecord]) -> None: ...


@runtime_checkable
class RoutingService(Protocol):
    async def plan_route(self, request: RouteRequest) -> Route:
        """Solve; fills Stop.detour_m/detour_s for every optional stop and leg waypoints that start/end exactly on the
        stop poses. Batching = stops carrying several user_ids (one SKU -> one stop, qty summed)."""
        ...

    async def plan_batched(self, requests: list[RouteRequest]) -> Route:
        """Merge several shoppers' requests (same store; stop_ids unique across them) and ``plan_route`` the union."""
        ...

    async def grid_spec(self, store_id: StoreId) -> GridSpec: ...


@runtime_checkable
class RobotService(Protocol):
    """MULTI-ROBOT control of every rover in every store (SO-101 on a lift; contracts.common robot model).

    The real impl picks the backend PER STORE (contracts/sim.py "Which backend drives a store"): a store is on Isaac
    iff a connected worker hosts it loaded with a matching data_hash (else it sends the store-level ResetCmd first),
    and then all its robots go to Isaac; otherwise all go to the built-in 2D fake, so the fleet always sees every robot
    (``RobotState.backend`` says which). On Isaac loss the in-flight command raises RobotError('disconnected') once and
    the robot continues on the fake from its last state (pose, basket, holding, battery; no reset). The robot arm owns
    battery (contracts.common battery model). The fake is the 2D kinematic sim for all robots in
    data/fixtures/robots.yaml, rendering synthetic camera frames (Pillow) for robot and ceiling cameras.

    Every method raises ``contracts.sim.RobotError`` (``.code`` = SimErrorCode; ``unknown_robot`` for unknown ids) on
    failure. One motion at a time PER ROBOT (navigate/scan/pick/place_in_basket/reset; else 'busy'); different robots
    move concurrently; capture/capture_camera/list_instances/state/stop always work.
    """

    async def list_robots(self) -> list[RobotState]:
        """Latest state of every robot, all stores (DISCONNECTED status for robots nobody drives)."""
        ...

    async def state(self, robot_id: RobotId) -> RobotState: ...

    async def reset(self, robot_id: RobotId, store_id: StoreId | None = None) -> RobotState:
        """Load the robot's store scene if needed (``services.catalog.get_planogram`` + products at call time, pushed
        in ResetCmd) and put the robot at its dock with an empty basket. Robots never change store: ``store_id`` must
        be None or the robot's own store (else RobotError('unknown_robot'))."""
        ...

    async def navigate(self, robot_id: RobotId, waypoints: list[Pose2D]) -> RobotState:
        """Follow waypoints (store frame); resolves on arrival at the final pose incl. its theta."""
        ...

    async def stop(self, robot_id: RobotId) -> RobotState:
        """Cancel that robot's in-flight motion (it raises RobotError('cancelled')) and hold position."""
        ...

    async def scan(self, robot_id: RobotId, slot_id: SlotId) -> CameraFrame:
        """Aim the front pan-tilt camera at a slot (robot at its approach pose) and capture a RAW (unblurred) frame;
        ``frame.instances`` lists EVERY non-basket unit of the slot (ground truth incl. pixel bbox or None when
        off-image, lot_id, freshness = the exact count) and ``frame.camera_pose`` the pose used."""
        ...

    async def capture(self, robot_id: RobotId, camera: CameraName = "front") -> CameraFrame:
        """Capture a RAW frame from one of the robot's cameras (front / wrist / basket; ``instances`` filled)."""
        ...

    async def capture_camera(self, camera_id: CameraId) -> CameraFrame:
        """Capture a RAW frame from ANY camera id, incl. store ceiling cameras ('store_ave.ceiling-1')."""
        ...

    async def list_instances(self, robot_id: RobotId, slot_id: SlotId | None = None) -> list[ItemInstance]:
        """Ground-truth instances in the robot's store (in a slot, or everywhere incl. its basket); bbox None."""
        ...

    async def pick(
        self, robot_id: RobotId, slot_id: SlotId, sku: Sku, instance_id: InstanceId | None = None
    ) -> ItemInstance:
        """Lift to the item height + SO-101 grasp (sim: magic grasp/attach). Reach = common.slot_reachable on the
        SLOT (any unit of a reachable slot works, incl. back rows); unreachable -> 'grasp_failed'."""
        ...

    async def place_in_basket(self, robot_id: RobotId) -> RobotState: ...

    async def unload_basket(self, robot_id: RobotId) -> list[ItemInstance]:
        """Empty the basket at checkout (sim: UnloadBasketCmd; fake: clear it) and return what was in it. The brain
        calls it when a shop / fetch goal ends at the checkout, before finish_goal."""
        ...

    async def sync_stock(self, store_id: StoreId, slot_ids: list[SlotId] | None = None) -> None:
        """Make the simulated shelves match the catalog after a restock / adjustment: reads
        ``services.catalog.get_planogram(store_id)`` at call time and sets each slot's units (Isaac: SetSlotStockCmd
        per slot; fake: respawn/remove units deterministically). None = every slot of the store."""
        ...

    def frames(self, camera_id: CameraId | None = None, robot_id: RobotId | None = None) -> AsyncIterator[CameraFrame]:
        """Live RAW frames, filtered to one camera / one robot (None = all); fans out to every subscriber. Opening an
        iterator for a camera makes that camera stream: on Isaac the robot arm sends SetStreamsCmd with the union of
        cameras that have open iterators (ref-counted, removed on close, fps = max requested, capped at 15); the fake
        renders on demand. Stream frames carry ``instances=[]``. CameraService blurs before serving."""
        ...

    def states(self, robot_id: RobotId | None = None) -> AsyncIterator[RobotState]:
        """Robot state updates (pose/status/battery) as they happen; None = every robot; fans out to every subscriber
        (gateway, fleet, cameras)."""
        ...


@runtime_checkable
class PerceptionService(Protocol):
    """Frames carry sim ground truth in ``frame.instances`` (sim/fake); real CV may use it as the fallback."""

    async def blur_faces(self, frame: CameraFrame) -> CameraFrame:
        """Return a copy with faces blurred and ``blurred=True``. Must be called before storing/streaming/LLM."""
        ...

    async def identify(
        self,
        frame: CameraFrame,
        store_id: StoreId,
        slot_id: SlotId | None = None,
        expected_sku: Sku | None = None,
    ) -> ScanReport:
        """ScanReport with detections, observed_count (+count_is_exact), lot_freshness, shelf_tag_cents, pose."""
        ...

    async def rank_freshness(self, frame: CameraFrame, slot_id: SlotId, sku: Sku, k: int = 3) -> PickChoice:
        """Top min(k, PICK_CHOICE_MAX=3) candidates, rank 1 = freshest, each with instance_id (for robot.pick),
        bbox and a face-free thumbnail."""
        ...

    async def verify_basket(self, frame: CameraFrame, expected: dict[Sku, int]) -> BasketCheck: ...


@runtime_checkable
class TrustService(Protocol):
    async def get_mandate(self, user_id: UserId) -> IntentMandate | None: ...

    async def set_mandate(self, mandate: IntentMandate) -> IntentMandate: ...

    async def evaluate(
        self,
        user_id: UserId,
        cart: Cart,
        line: CartLine | None = None,
        budget_cents: int | None = None,
        trip_id: TripId | None = None,
    ) -> MandateDecision:
        """Deterministic mandate check of the cart, or of the cart plus a proposed ``line`` (scope='line').

        Checks the standing mandate (cap -> DENY, ask_above per line -> NEEDS_APPROVAL, categories, merchants,
        expiry/revocation) AND the trip budget (``budget_cents``; over -> NEEDS_APPROVAL, OVER_BUDGET).
        """
        ...

    async def checkout(self, request: CheckoutRequest) -> Receipt:
        """Verify approval (kind=checkout, approved, hashes equal) + mandate (cap + budget), then charge (Stripe test
        via the VisaIntelligentCommerce adapter). Mints a mock ``online_order_id`` when the cart has online lines.
        A refused/declined checkout returns a Receipt with status 'declined'/'error' (no exception)."""
        ...

    async def record_transparency(self, entry: TransparencyEntry) -> None:
        """UPSERT on rec_id (gateway calls it at recs.ready and again when the approval resolves)."""
        ...

    async def ledger(self, user_id: UserId | None = None, trip_id: TripId | None = None) -> TrustLedger: ...


InboundHandler = Callable[[InboundMessage], Awaitable[None]]


@runtime_checkable
class MessagingService(Protocol):
    async def start(self) -> None:
        """Begin receiving (e.g. Photon polling). Inbound messages go to the registered handler with ``user_id``
        resolved via ``services.recommender.find_user_by_handle`` (None if unknown)."""
        ...

    async def stop(self) -> None: ...

    async def send(self, message: OutboundMessage) -> SendResult: ...

    async def send_approval_request(self, request: ApprovalRequest) -> SendResult:
        """Text an approval (YES/NO or numbered options) to the user's handle."""
        ...

    def on_inbound(self, handler: InboundHandler) -> None: ...


@runtime_checkable
class AgentService(Protocol):
    """Natural-language understanding + the shopper chat agent. Never mutates a Trip it is handed: side effects go
    through services.trips / approvals. (The per-robot managing LLM is ``RobotBrainService``, same arm.)"""

    async def interpret_request(
        self,
        text: str,
        user_id: UserId,
        store_id: StoreId | None = None,
        *,
        role: RequesterRole = "shopper",
        profile: UserProfile | None = None,
        history: list[str] | None = None,
    ) -> RequestDraft:
        """NL request -> draft plan (intent shop | fetch | shelf_scan | return_to_dock | status | chat). shop/fetch
        carry an expanded, UNRESOLVED ShoppingList (same as expand_intent); shelf_scan carries categories/aisles;
        store/robot hints when named. ``history`` = earlier turns of the same request, oldest first (original text,
        clarifying question, ...) when ``text`` answers a clarifying question (FleetRequest.in_reply_to). The fleet
        decides stores, goals and robots. Real: claude-sonnet-5-5 structured output via utils.llm; fake: keyword rules
        (taco demo list stays identical to expand_intent)."""
        ...

    async def expand_intent(self, intent: Intent, profile: UserProfile | None = None) -> ShoppingList:
        """'tacos for 6, under $40, one vegetarian' -> ShoppingList (items with query/qty/substitutes/dietary,
        servings, budget_cents). Items may carry a SKU guess but sourcing='unresolved'. Gateway emits list.ready."""
        ...

    async def source_list(self, shopping_list: ShoppingList, store_id: StoreId) -> ShoppingList:
        """Split sourcing (calls services.catalog at call time): every item gets sku, sourcing, store_id and
        est_line_cents — in stock at ``store_id`` -> 'in_store'; OOS / online-only -> 'online' (store_id='online');
        nothing found -> 'unresolved' with notes. Sets shopping_list.store_id. Gateway emits sourcing.split."""
        ...

    async def respond(self, trip: Trip, text: str, channel: Channel = "web") -> AgentReply:
        """Chat turn with tools (query_inventory, plan_route, recommend, add_to_cart, request_approval, checkout).

        Publishes streaming ``agent.message`` events itself; tool side effects only via services.trips/approvals.
        """
        ...

    async def interpret_reply(
        self, message: InboundMessage, pending: list[ApprovalRequest], trip: Trip | None = None
    ) -> ReplyInterpretation:
        """Classify a reply: approval (which one, yes/no/choice), feedback (rating; ``sku`` from
        trip.followup.about_skus / trip.receipt lines), new intent, or chat."""
        ...

    async def compose_followup(self, trip: Trip) -> OutboundMessage:
        """Post-purchase question ('How was the salsa? Reply 1-5.') with ``about_skus`` set; the gateway sends it,
        stores it in Trip.followup and publishes followup.sent."""
        ...


@runtime_checkable
class TripController(Protocol):
    """Gateway-owned. The ONLY way arms change a trip; each call updates the Trip, drives TripState and publishes
    the matching events (cart.updated, list.ready, route.planned, mandate.decision, ...)."""

    async def get_trip(self, trip_id: TripId) -> Trip:
        """Current snapshot (a copy). Raises KeyError for unknown trips."""
        ...

    async def create_trip(self, request: CreateTripRequest) -> Trip:
        """Same path as POST /api/trips: create + start planning in the background (intent -> list (skipped when
        ``request.shopping_list`` is given) -> sourcing -> recs -> route). The fleet calls it per chosen store with
        ``auto_execute=True``: robot_logged_in=True, no AWAITING_LOGIN; after route_planned the trip goes straight to
        EXECUTING and calls ``fleet.execute_trip(trip_id, request.robot_id)``."""
        ...

    async def assign_robot(self, trip_id: TripId, robot_id: RobotId, goal_id: GoalId | None = None) -> Trip:
        """Fleet -> trip: record which robot / goal executes the trip (Trip.robot_id / goal_id; replan starts from
        that robot's pose)."""
        ...

    async def set_stop_status(self, trip_id: TripId, stop_id: StopId, status: StopStatus, reason: str = "") -> Trip:
        """Robot brain -> trip: route stop progress (Trip.stop_status / current_stop_id); publishes stop.updated."""
        ...

    async def check_line(self, trip_id: TripId, line: CartLine) -> MandateDecision:
        """Mandate pre-check of the cart + a proposed line with the trip budget (trust.evaluate); publishes
        mandate.decision. The brain calls it BEFORE the first unit of a goal item, on the FULL line (qty = item.qty),
        so over-threshold items are asked about first."""
        ...

    async def ask(self, trip_id: TripId, request: ApprovalRequest) -> ApprovalResponse:
        """Ask the trip's shopper (the brain's ask_user for goals WITH a trip): sets trip_id / user_id / store_id,
        fills channels + expires_s from the gateway config (web + photon), drives EXECUTING -> AWAITING_APPROVAL ->
        EXECUTING around the wait, records the TransparencyEntry outcome for kind=recommendation, and returns the
        answer (timeout -> decision 'timeout')."""
        ...

    async def update_list(self, trip_id: TripId, shopping_list: ShoppingList) -> Trip:
        """Replace the shopping list (pre-trip edits); re-runs sourcing/recs/routing as the state allows."""
        ...

    async def add_line(self, trip_id: TripId, line: CartLine, approval_id: ApprovalId | None = None) -> AddLineResult:
        """trust.evaluate (with the trip budget) then add. needs_approval -> not added unless ``approval_id`` names
        an APPROVED approval for this add; deny -> never added. Recomputes Cart.shipping_cents."""
        ...

    async def remove_line(self, trip_id: TripId, line_id: LineId) -> Cart: ...

    async def replan(
        self, trip_id: TripId, include_recommendations: bool = True, max_optional_stops: int | None = None
    ) -> Route:
        """Re-plan from the CURRENT POSE OF THE TRIP'S ROBOT (Trip.robot_id) over the remaining stops (pending ones
        and any new list items); stores Trip.route, emits route.planned."""
        ...

    async def start_checkout(self, trip_id: TripId, note: str | None = None) -> CheckoutStatus:
        """Same path as POST /api/trips/{id}/checkout: mandate pre-check, raise the kind=checkout approval bound to
        the cart hash, and (after approval) trust.checkout. Returns the current status without waiting to pay."""
        ...


@runtime_checkable
class ApprovalBroker(Protocol):
    """Gateway-owned. Fans an ApprovalRequest out to web/voice/Photon and awaits the first answer."""

    async def request(self, request: ApprovalRequest) -> ApprovalResponse:
        """Publish approval.requested + wait; resolves with decision='timeout' after ``expires_s``."""
        ...

    async def resolve(self, response: ApprovalResponse) -> bool:
        """Deliver an answer; False if unknown/already resolved.

        For non-web channels (photon/voice/system) the broker copies ``cart_hash`` from the pending request into the
        response before resolving; a web answer to a checkout approval whose cart_hash differs is rejected (False).
        """
        ...

    def pending(
        self,
        trip_id: TripId | None = None,
        *,
        goal_id: GoalId | None = None,
        robot_id: RobotId | None = None,
        store_id: StoreId | None = None,
        user_id: UserId | None = None,
    ) -> list[ApprovalRequest]:
        """Pending requests matching every given filter (GET /api/approvals?trip_id=&goal_id=&robot_id=&store_id=
        &user_id=); no filter = all."""
        ...

    async def cancel(self, approval_id: ApprovalId, reason: str = "") -> bool:
        """Withdraw a pending request (its goal was cancelled / reassigned): resolves it with decision='timeout',
        channel='system', note=reason, and publishes approval.resolved. False if unknown / already resolved."""
        ...


@runtime_checkable
class EventPublisher(Protocol):
    """Gateway-owned event bus front door. Assigns bus-wide ``seq`` and fans out to websockets."""

    async def publish(self, event: Event) -> Event: ...


@runtime_checkable
class FleetService(Protocol):
    """Fleet manager (arms/fleet): organizes every robot across every store and gives them goals.

    NL request -> ``agent.interpret_request`` -> choose store(s) (catalog stock coverage + price; may split) ->
    Goal(s) -> assign a robot in that store with ``contracts.fleet.choose_robot`` (deterministic: idle, battery,
    distance) -> ``brain.run_goal`` as a background task -> monitor -> reassign on robot failure (MAX_GOAL_ATTEMPTS).
    Owns Goals / Assignments / RequestPlans and publishes request.* / goal.* / fleet.robot_updated. May call:
    agent, catalog, routing (distances), robot (states, stop), brain, trips (create_trip / assign_robot / get_trip),
    approvals (pending / cancel), events. Monitors battery: an idle robot below MIN_BATTERY_PCT gets a return_to_dock
    goal.
    """

    async def start(self) -> None:
        """Begin monitoring (robot states -> fleet.robot_updated, low battery -> return_to_dock). Idempotent; the
        gateway calls it at startup."""
        ...

    async def shutdown(self) -> None:
        """Cancel running goals' tasks and stop monitoring."""
        ...

    async def submit_request(self, request: FleetRequest) -> RequestPlan:
        """Interpret + plan + create/assign goals, then return (execution continues in the background; watch /ws with
        ``?request_id=``). Publishes request.received then request.planned (envelope request_id + user_id).

        - shop: one Trip per chosen store (``trips.create_trip(CreateTripRequest(shopping_list=<that store's
          sub-list>, request_id, robot_id=<pre-assigned>, auto_execute=True))``) + one PENDING shop Goal bound to it
          with the robot pre-assigned (Goal.robot_id set; that robot's RobotInfo lists it in queued_goal_ids and is
          not ``available``). Items stay empty until ``execute_trip`` builds them from the trip route. The trip runs
          by itself (auto_execute). Split budget: each sub-list's budget_cents = ``fleet.split_budget(budget,
          {store: StoreChoice.est_total_cents})[store]`` (shares sum to the stated budget; RequestPlan.budget_split).
        - status / chat (any role): reply only (RequestPlan.reply, status 'completed'), no goal.
        - fetch / shelf_scan / return_to_dock: staff only (shopper -> 'rejected'); goals via create_goal semantics.
        - ambiguous -> 'needs_input' with the clarifying question as reply. A FleetRequest with ``in_reply_to``
          re-interprets with ``history=[original text, question, ...]`` and updates THAT plan (same request_id,
          answer appended to RequestPlan.replies)."""
        ...

    async def get_request(self, request_id: RequestId) -> RequestPlan:
        """Raises KeyError for unknown requests."""
        ...

    async def list_requests(self, user_id: UserId | None = None, limit: int = 20) -> list[RequestPlan]:
        """Newest first (GET /api/requests?user_id=&limit=): the chat history of a user."""
        ...

    async def list_robots(self, store_id: StoreId | None = None) -> list[RobotInfo]: ...

    async def get_robot(self, robot_id: RobotId) -> RobotInfo:
        """Raises KeyError for unknown robots."""
        ...

    async def create_goal(self, spec: GoalSpec) -> Goal:
        """Create + assign (``spec.robot_id`` pins it; else choose_robot in spec.store_id) and start it in the
        background (queued if the robot is busy). shelf_scan: categories / aisle_ids (all empty = whole store) are
        expanded into Goal.slot_ids here. user_id None -> common.STAFF_USER_ID. shop without trip_id: creates an
        auto_execute trip for user_id + items. Publishes goal.created + goal.assigned."""
        ...

    async def list_goals(
        self,
        store_id: StoreId | None = None,
        robot_id: RobotId | None = None,
        status: GoalStatus | Sequence[GoalStatus] | None = None,
        *,
        active: bool | None = None,
        kind: GoalKind | None = None,
        request_id: RequestId | None = None,
        trip_id: TripId | None = None,
        limit: int = 50,
    ) -> list[Goal]:
        """Newest first, every filter ANDed: ``status`` one or several (OR), ``active=True`` = non-terminal
        (pending / assigned / active / waiting), ``active=False`` = terminal."""
        ...

    async def get_goal(self, goal_id: GoalId) -> Goal:
        """Raises KeyError for unknown goals."""
        ...

    async def update_goal(self, goal_id: GoalId, update: GoalUpdate) -> Goal:
        """The ONLY way the robot brain changes a goal (progress, items, add_items, scanned slots, status
        waiting / active-from-waiting / completed / failed). A same-status update is a no-op; an invalid transition
        raises ValueError. Publishes goal.updated / goal.completed / goal.failed (envelope ``goal_envelope``)."""
        ...

    async def cancel_goal(self, goal_id: GoalId, reason: str = "") -> Goal:
        """Cancel (stops the robot if it is running it, cancels the brain task, withdraws the goal's pending questions
        with ``approvals.cancel``). Terminal goals are returned unchanged."""
        ...

    async def execute_trip(self, trip_id: TripId, robot_id: RobotId | None = None) -> Goal:
        """Trip flow -> fleet at the EXECUTING step: get-or-create the trip's shop goal, (re)build its items with
        ``fleet.goal_items_from_trip(trip)``, assign a robot in trip.store_id (``robot_id`` / the pre-assigned robot
        when still eligible, else choose_robot; ``trips.assign_robot``), set ACTIVE, run ``brain.run_goal``,
        reassign on robot failure (MAX_GOAL_ATTEMPTS; the next robot rebuilds items from the trip and only expects the
        units IT places), and return the TERMINAL goal. The trip flow then:
        completed -> ``start_checkout``; failed -> ``start_checkout`` if the cart has active lines (plus an error
        event), else the trip goes FAILED; cancelled -> the trip goes FAILED with the reason, no checkout. No eligible
        robot at all (e.g. Hazel down at store_hill) -> failed with failure_code 'no_robot'."""
        ...

    async def message_robot(self, robot_id: RobotId, text: str, user_id: UserId | None = None) -> BrainReply:
        """Forward a user message to the robot's brain (``brain.handle_message``)."""
        ...

    async def stop_robot(self, robot_id: RobotId, reason: str = "") -> RobotInfo:
        """Halt the robot now (``robot.stop``) and cancel its active goal (status cancelled; its pending questions are
        withdrawn with ``approvals.cancel``)."""
        ...


@runtime_checkable
class CameraService(Protocol):
    """Cameras API (perception arm, ``create_cameras`` / ``create_cameras_fake``): registry from
    data/fixtures/robots.yaml + SimHello, frames from ``services.robot`` (capture_camera / frames), faces blurred with
    ``perception.blur_faces`` BEFORE anything leaves this service (every frame returned has ``blurred=True``)."""

    async def list_cameras(self, store_id: StoreId | None = None, robot_id: RobotId | None = None) -> list[CameraInfo]:
        """Robot cameras ('<robot_id>.front|wrist|basket') + store ceiling cameras, filtered."""
        ...

    async def get_camera(self, camera_id: CameraId) -> CameraInfo:
        """Raises KeyError for unknown cameras."""
        ...

    async def snapshot(self, camera_id: CameraId, max_width: int | None = None) -> CameraFrame:
        """A fresh BLURRED frame (GET /api/cameras/{id}/snapshot.jpg[?w=] serves base64-decoded ``jpeg_b64``),
        downscaled (aspect kept, Pillow) when ``max_width`` is smaller than the frame. Raw frames: capture_camera."""
        ...

    def stream(self, camera_id: CameraId, fps: float = 5.0, max_width: int | None = None) -> AsyncIterator[CameraFrame]:
        """Live BLURRED frames at <= fps (MJPEG stream.mjpg, WS /ws/cameras/{id} and the multiplexed /ws/cameras),
        downscaled like ``snapshot``. Source: ``robot.frames(camera_id)`` (opening it makes the camera stream); if no
        frame arrives within 1 s it falls back to polling ``robot.capture_camera`` at <= fps."""
        ...

    async def observe(
        self, camera_id: CameraId, question: str | None = None, *, goal_id: GoalId | None = None
    ) -> VisionObservation:
        """Snapshot -> blur -> vision (real: claude-haiku-5-5 via utils.llm with ground-truth hints in sim; fake:
        description from the frame's ground-truth instances). The robot brain's ``look`` tool."""
        ...


@runtime_checkable
class RobotBrainService(Protocol):
    """Per-robot managing LLM (agent arm, ``create_brain`` / ``create_brain_fake``).

    Given a goal it decides what to pick, where, in what order, and handles exceptions, using
    ``contracts.tools.BRAIN_TOOL_DEFINITIONS`` (executor mapping in contracts/tools.py). Real: Claude tool loop
    (claude-sonnet-5-5 plans; vision via CameraService.observe = claude-haiku-5-5) through utils.llm. Fake: a
    deterministic scripted executor running the SAME tool sequence with no LLM (for shop goals it reproduces the old
    per-stop loop exactly, so ``make demo`` tells the same story). Publishes ``brain.step`` for every thought / tool
    call / result / observation / message, plus robot.arrived / scan.result / pick.choices / item.picked /
    basket.verified (envelope ``events.goal_envelope(goal)``: shop-goal events carry trip_id). Changes goals only via
    ``services.fleet.update_goal``; trips only via ``services.trips``; asks people via ``services.trips.ask`` (goals
    with a trip) or ``services.approvals.request`` (goals without one). Keeps a per-robot ring of its recent
    BrainSteps (>= 500) for ``recent_steps``.
    """

    async def run_goal(self, robot_id: RobotId, goal: Goal) -> Goal:
        """Execute one goal with this robot until finish_goal (long-running; the fleet sets ACTIVE first, runs it as a
        task and cancels the task to cancel/stop; on cancellation the brain withdraws its pending question with
        ``approvals.cancel``). Returns the final goal snapshot (completed / failed).

        Raises ONLY ``RobotError`` and only for ``tools.BRAIN_FATAL_CODES`` (battery_low, unknown_robot) or a robot
        left in RobotStatus.ERROR, WITHOUT calling update_goal(failed) first, so the fleet can reassign.
        'disconnected' is retryable (the robot continues on the 2D fake): re-plan from the current pose. LLM / API
        failures mid-goal (overload, rate limit, refusal, max_tokens, malformed output) never propagate: publish an
        error event + brain.step(kind='error'), switch BrainStatus.mode to 'scripted' and finish the goal with the
        scripted executor from the current goal state.

        0.2.1 plan mode: when ``exec_mode(services, settings.picky_exec_mode) == 'plan'``, delegate to
        ``services.runner.run_goal(robot_id, goal)`` and return ``await services.fleet.update_goal(goal.goal_id,
        bridge.goal_update_from_outcome(outcome))`` (a RobotError from the runner propagates so the fleet reassigns).
        'direct' (the default) is this v0.2 path unchanged."""
        ...

    async def handle_message(self, robot_id: RobotId, text: str, user_id: UserId | None = None) -> BrainReply:
        """A user talks to the robot. Idle: answer (status, what it sees). Running a goal: the message is injected
        into the goal's context and may change the plan: 'also grab limes' -> add_item (update_goal(add_items) +
        trips.update_list + trips.replan for shop goals); 'skip the cheese' -> update_progress item_status skipped.
        The fake brain handles 'add <x>' / 'skip <x>' / 'status' by keyword."""
        ...

    async def status(self, robot_id: RobotId) -> BrainStatus:
        """GET /api/fleet/robots/{robot_id}/brain (mode llm / scripted, current tool, pending messages)."""
        ...

    async def recent_steps(
        self,
        robot_id: RobotId | None = None,
        goal_id: GoalId | None = None,
        limit: int = 50,
        before: Timestamp | None = None,
    ) -> list[BrainStep]:
        """Recent BrainSteps, NEWEST FIRST, filtered by robot and/or goal (a reassigned goal spans robots); ``before``
        pages by ts. GET /api/fleet/robots/{id}/steps and GET /api/goals/{id}/steps (the 'thinking log' history)."""
        ...


# ======================================================================================================================
# Planner -> VLA -> verifier loop (new in 0.2.1; every field below is OPTIONAL in Services, default None)
#
#     field     Protocol         arm         factories (LOOP_SERVICE_FACTORIES)  real impl
#     planner   PlannerService   agent       create_planner / _fake              Gemini PlanDraft + code compiler
#     runner    LoopRunner       agent       create_runner / _fake (*)           THE loop: pre/act/post/commit/recover
#     vla       VlaService       robot       create_vla / _fake                  T0 = RobotService; T1/T2 = RunSkillCmd
#     truth     TruthSource      robot       create_truth / _fake                2D fake world / Isaac instances
#     faults    FaultInjector    robot       create_faults / _fake               demo faults (PICKY_FAULTS, dev routes)
#     verifier  VerifierService  perception  create_verifier / _fake             VisionProvider (Perceptron) + truth
#     evidence  EvidenceCameras  perception  create_evidence / _fake             CameraService.snapshot + retention
#     (*) the runner is deterministic code: its "fake" is the same class wired to fakes.
#
# Kill switch (double): every loop field defaults to None AND PICKY_EXEC_MODE defaults to 'direct'. ``exec_mode``
# returns 'direct' unless the mode is 'plan' / 'tools' AND planner, runner, vla and verifier are all wired; 'direct'
# is the exact v0.2 brain path (tools call RobotService directly). Factories never call services at construction; a
# real factory that raises falls back to its fake (v0.2 registry rules); PICKY_REAL_ARMS=planner|vla|verifier selects
# real ones individually (field names, like 'brain' / 'cameras').
# ======================================================================================================================
@runtime_checkable
class PlannerService(Protocol):
    """NL / Goal -> typed StepPlan; re-plan on failure (agent arm). The LLM proposes a ``PlanDraft`` (WHAT, in which
    order); ``compile`` (deterministic code: ``plan.normalize_draft`` then the macros) decides HOW. Real: Gemini
    structured output (the utils.llm 'brain' model, default gemini-3.8-flash; PICKY_LLM_PROVIDER=anthropic switches to
    Claude) via ``utils.llm.complete_json(prompt, PlanDraft)`` inside ``asyncio.wait_for(PICKY_PLAN_TIMEOUT_S, default
    20 s)``; no key / timeout / refusal / invalid draft -> scripted draft (never raises for LLM errors;
    ``plan.planner.origin`` + ``fallback_reason`` say which). ``replan`` stays deterministic (scripted draft over the
    open tail: it runs mid-trip, so no LLM latency there). The runner bounds ``replan`` with the same timeout and ends
    only that item if it raises. Fake: scripted (taco rules + catalog lookups via the v0.2 CatalogService); the real
    planner subclasses it and overrides ``draft()`` only."""

    async def plan(self, request: PlanRequest) -> StepPlan:
        """READY StepPlan. Goal: its pending items in goal order (= the trip route for shop goals). Text: interpret
        (fake: keyword rules; taco list = v0.2 expand_intent's) and resolve items in ``request.store_id`` through
        ``catalog.search`` / ``get_locations``; items not on the shelf there are listed with status 'online' and get
        no steps. Validation errors -> ValueError (nothing runs)."""
        ...

    async def compile(
        self,
        draft: PlanDraft,
        request: PlanRequest,
        *,
        items: Sequence[GoalItem],
        plan_id: PlanId | None = None,
    ) -> StepPlan:
        """Deterministic, no LLM: approach poses / lift heights from the planogram, ``TargetSpec`` from the product,
        ``default_conditions``, timeouts, units = qty - picked_qty, optional -> ask first, produce -> pick_choice after
        the scan, audit + checkout handoff when end_at='checkout'. Unknown item ids in the draft are dropped (listed in
        ``planner.fixes``); drafts cannot add items."""
        ...

    async def replan(self, request: ReplanRequest) -> StepPlan:
        """New revision (revision+1, supersedes = old plan_id, same root_plan_id): CLOSED steps carried over verbatim
        (same ids -> same effect keys, nothing commits twice); only the tail changes (substitute SKU, skipped item, new
        order). The old plan becomes SUPERSEDED. Budget: RetryPolicy.budget.max_replans (the runner enforces it)."""
        ...

    async def expand_tool_call(self, plan: StepPlan, tool_name: str, tool_input: Mapping[str, Any]) -> list[PlanStep]:
        """Tools mode: ONE v0.2 brain motion tool call -> the steps to append to the goal's live plan
        (``bridge.BRAIN_TOOL_STEPS``; ids 't<NN>.<k>'). KeyError for non-motion tools (they keep the v0.2 executor)."""
        ...

    def validate(self, plan: StepPlan) -> list[PlanIssue]:
        """``plan.validate_plan`` + store checks (slots exist in the robot's store)."""
        ...


@runtime_checkable
class VlaService(Protocol):
    """Executes VLA steps (navigate_to, align, grasp, place, handoff) on one robot as an async status stream (robot
    arm); see contracts.vla for the stream / idempotency / tier / deadline rules. T0 wraps v0.2 RobotService (navigate =
    services.routing path + robot.navigate; align = a small robot.navigate to ``location.approach_pose`` only: v0.2
    has no lift / pan-tilt command, so 'lift_preposition' / 'aiming_camera' are UI stages, the lift moves inside
    robot.pick and the head is aimed by EvidenceCameras(aim_slot_id) / robot.scan; grasp = robot.pick(slot, sku,
    grounding.instance_id) (magic grasp, slot_reachable); place = robot.place_in_basket; handoff =
    robot.unload_basket), maps RobotError with ``VlaError.from_sim`` and is available on both backends, always.
    ``running`` heartbeats come from the service itself (a timer task + pose from robot.state); RobotService has no
    progress callback. T1/T2 (0.3.0 wire): ``vla.RunSkillCmd`` to the Isaac worker, availability from
    ``vla.PolicyServerStatus``. All timestamps the service emits (VlaStatus.ts, VlaOutcome.ended_at) are GATEWAY
    clock. Publishes nothing (the runner owns events)."""

    async def capabilities(self, robot_id: RobotId) -> list[VlaCapabilities]:
        """Every tier for that robot right now (available or not, with reason). KeyError for unknown robots."""
        ...

    def execute(self, request: VlaRequest, *, after_seq: int = 0) -> AsyncIterator[VlaStatus]:
        """Start (or re-attach to) ``request.exec_id`` and stream its statuses (seq > after_seq). Never raises for robot
        failures (they are 'failed' statuses); closing the iterator does NOT cancel the motion (use ``cancel``)."""
        ...

    async def get_exec(self, exec_id: ExecId) -> ExecRecord:
        """Request + every status so far (KeyError if unknown / expired)."""
        ...

    async def cancel(self, exec_id: ExecId, reason: str = "") -> VlaStatus:
        """Stop that execution (robot.stop on T0; abort the skill on T1/T2) and return its TERMINAL status. Idempotent:
        an already-terminal exec returns its terminal status unchanged. KeyError if unknown."""
        ...

    async def cancel_robot(self, robot_id: RobotId, reason: str = "") -> list[VlaStatus]:
        """Cancel every running execution of that robot (fleet stop / goal cancel)."""
        ...


StatusCallback = Callable[[VlaStatus], Awaitable[None]]


async def run_vla_to_terminal(
    vla: VlaService,
    request: VlaRequest,
    *,
    on_status: StatusCallback | None = None,
    stall_factor: float = STALL_FACTOR,
    max_reattach: int = 2,
    grace_heartbeats: float = 2.0,
) -> VlaStatus:
    """Drain ONE execution with the runner's safety nets and ALWAYS return exactly one TERMINAL status:

    - stall: no status for ``stall_factor x heartbeat_s`` -> cancel, synthesize failed(deadline_exceeded, stalled)
    - deadline: ``deadline_s`` (+ ``grace_heartbeats`` heartbeats) elapsed -> cancel, synthesize
      failed(deadline_exceeded). Checked BEFORE every wait, so a slow ``on_status`` (event publishing) can never let a
      TimeoutError escape: the passed-deadline branch is the same cancel + synthesize path as the wait timeout.
    - dropped stream (gateway hiccup, worker reconnect): re-attach with the same exec_id + ``after_seq`` (idempotent,
      no second motion), at most ``max_reattach`` times, then ``get_exec``
    - caller cancelled (goal cancel / fleet stop): cancel the exec (shielded), re-raise CancelledError
    Replayed statuses (seq <= last seen) are dropped, so ``on_status`` sees each seq once. A terminal status the runner
    obtains by cancelling or synthesizes is passed to ``on_status`` too, so observers never see an exec stuck in
    'accepted' / 'running' (a synthesized status has seq = last + 1 and exists only on the runner side).
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    budget = request.deadline_s + grace_heartbeats * request.heartbeat_s
    stall_s = stall_factor * request.heartbeat_s
    last: VlaStatus | None = None
    reattach_left = max_reattach

    def synth(code: str, message: str, base: VlaStatus | None, **details: Any) -> VlaStatus:
        seq = max(base.seq if base else 0, last.seq if last else 0) + 1
        tier = last.tier if last else None
        return VlaStatus(
            exec_id=request.exec_id,
            robot_id=request.robot_id,
            seq=seq,
            phase="failed",
            tier=tier,
            backend=last.backend if last else None,
            message=message,
            error=VlaError(code=code, message=message, tier=tier, details=details),  # type: ignore[arg-type]
            outcome=base.outcome if base else None,
        )

    async def deliver(status: VlaStatus) -> VlaStatus:
        if on_status is not None and (last is None or status.seq > last.seq):
            await on_status(status)
        return status

    async def give_up(stalled: bool) -> VlaStatus:
        final = await vla.cancel(request.exec_id, "stalled" if stalled else "deadline")
        if final.phase in ("succeeded", "failed"):  # it finished on its own while we were cancelling
            return await deliver(final)
        msg = f"no status for {stall_s:.1f}s" if stalled else f"deadline {request.deadline_s:.1f}s exceeded"
        return await deliver(synth("deadline_exceeded", msg, final, stalled=stalled))

    while True:
        it = vla.execute(request, after_seq=last.seq if last else 0).__aiter__()
        try:
            while True:
                remaining = budget - (loop.time() - started)
                if remaining <= 0:
                    return await give_up(stalled=False)
                try:
                    status = await asyncio.wait_for(it.__anext__(), timeout=min(remaining, stall_s))
                except TimeoutError:
                    return await give_up(stalled=stall_s < remaining)
                if last is not None and status.seq <= last.seq:
                    continue
                last = status
                if on_status is not None:
                    await on_status(status)
                if status.terminal:
                    return status
        except StopAsyncIteration:
            record = await vla.get_exec(request.exec_id)
            for s in record.statuses:
                if last is None or s.seq > last.seq:
                    last = s
                    if on_status is not None:
                        await on_status(s)
            if record.terminal is not None:
                return record.terminal
            if reattach_left <= 0:
                final = await vla.cancel(request.exec_id, "stream lost")
                return await deliver(
                    final if final.phase != "cancelled" else synth("internal", "status stream lost", final)
                )
            reattach_left -= 1
        except asyncio.CancelledError:
            await asyncio.shield(vla.cancel(request.exec_id, "caller cancelled"))
            raise
        finally:
            aclose = getattr(it, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 - closing a dead stream must not mask the result
                    pass


@runtime_checkable
class VisionProvider(Protocol):
    """One vision backend behind the verifier and grounding (perception arm, inside its VerifierService; not a
    Services field). Implementations: **Perceptron** (THE real provider: SDK ``perceptron`` 0.4.x, PERCEPTRON_API_KEY,
    base https://api.perceptron.inc/v1; one ``AsyncClient`` per event loop, model perceptron-mk1.5, fallback
    perceptron-mk1; every call wrapped in ``asyncio.wait_for``), an optional general-VLM fallback (Gemini via
    utils.llm by default, Claude when PICKY_LLM_PROVIDER=anthropic), and the fake (answers from sidecars). Every method
    REJECTS frames with ``blurred=False`` (ValueError): faces are blurred before any frame reaches Perceptron or an
    LLM (CLAUDE.md section 8). Provider errors (rate limit, quota, timeout) return a VisionAnswer with
    ``call.error_code`` set and no boxes; they never raise into the runner. Mapping of each check to SDK calls:
    ``verify.PERCEPTRON_CALLS`` and docs/vla-loop/perceptron.md.

    ``timeout_s`` (every method): this call's budget = min(provider default PICKY_VISION_CALL_TIMEOUT_S, what is
    left of ``VerifyRequest.deadline_s``); the verifier passes the remainder so a 2-3 call verify never overruns.
    Timing (Perceptron mk1.5, one 640x480 JPEG): ~0.5-1.5 s per call, Multilook ~ one call for 2-4 prompts."""

    name: str
    model: str | None

    async def detect(
        self, frame: CameraFrame, classes: Sequence[str], *, timeout_s: float | None = None
    ) -> VisionAnswer: ...

    async def point(self, frame: CameraFrame, query: str, *, timeout_s: float | None = None) -> VisionAnswer:
        """Grounded pointing ('Point to the best grasp spot on the mild salsa jar')."""
        ...

    async def question(
        self, frame: CameraFrame, question: str, expects: VisionExpect = "text", *, timeout_s: float | None = None
    ) -> VisionAnswer: ...

    async def ocr(
        self, frame: CameraFrame, prompt: str | None = None, *, timeout_s: float | None = None
    ) -> VisionAnswer: ...

    async def compare(
        self,
        before: CameraFrame,
        after: CameraFrame,
        question: str,
        expects: VisionExpect = "box",
        *,
        timeout_s: float | None = None,
    ) -> VisionAnswer:
        """Two-image question (annotations without asset_idx refer to the LAST asset = ``after``)."""
        ...

    async def multilook(
        self, frame: CameraFrame, prompts: Sequence[str], *, timeout_s: float | None = None
    ) -> list[VisionAnswer]:
        """Several prompts against ONE prefilled frame (Perceptron Multilook; others: sequential calls)."""
        ...


@runtime_checkable
class VerifierService(Protocol):
    """Evaluates Conditions on blurred camera frames + robot state; grounds targets (perception arm). Provider-agnostic
    (VisionProvider; Perceptron in the real one). Stateless apart from caches; publishes nothing (the runner publishes
    step.verified / target.grounded). See contracts.verify for the evaluation order, modes and fusion rules. Fake:
    exact (``verify.evaluate_on_truth`` on ``TruthSource.truth`` with the fault overlay applied)."""

    async def verify(self, request: VerifyRequest) -> Verdict:
        """Idempotent per ``verify_id``. Never raises for provider failures (worst case 'uncertain', degraded).
        Raises ``VerifierUnavailableError`` only when no verdict can be produced at all."""
        ...

    async def ground(self, request: GroundRequest) -> GroundingResult:
        """Box + grasp point of the target in a fresh frame (Perceptron: detect + question(expects='point')); sim:
        ``instance_id`` = the unit whose bbox contains the point, so T0 grabs exactly that unit."""
        ...

    async def baseline(self, robot_id: RobotId, condition: Condition | None, *, baseline_id: str) -> VerifyBaseline:
        """Pre-step snapshot for relative atoms (basket_delta): basket counts, slot counts, holding (+ frame refs)."""
        ...

    async def capabilities(self) -> VerifierCapabilities: ...

    async def set_mode(self, mode: VerifierMode) -> VerifierCapabilities:
        """decide / shadow / gt_only (PUT /api/verifier/mode)."""
        ...


@runtime_checkable
class FaultInjector(Protocol):
    """Deterministic demo failures (robot arm; dev / demo only). The fake VLA, fake camera and fake verifier call
    ``consult`` with their layer's effects; it applies ``faults.fault_fires`` (occurrence counted per rule over
    distinct step keys), records the firing and publishes ``demo.fault``."""

    async def load_scenario(self, scenario: FaultScenario | str) -> FaultScenario:
        """Replace the active rules with a scenario (name from DEMO_SCENARIOS, or a full FaultScenario)."""
        ...

    async def inject(self, rule: FaultRule) -> FaultRule: ...

    async def clear_faults(self) -> None: ...

    async def firings(self) -> list[FaultFiring]:
        """What fired so far (the presenter's cheat sheet; GET /api/dev/faults)."""
        ...

    async def consult(self, ctx: FaultContext, effects: Sequence[FaultEffect]) -> list[FaultFiring]:
        """Rules among ``effects`` that fire for ``ctx`` now (each firing recorded once)."""
        ...


@runtime_checkable
class LoopRunner(Protocol):
    """THE orchestration entry point (agent arm). Runs StepPlans. Per step: pre-check (verifier) -> act (VLA via
    ``run_vla_to_terminal`` / verifier / v0.2 scan + ask paths) -> baseline + postconditions (verifier; degrade policy
    if it is down) -> commit ``COMMIT_ON_VERIFIED`` effects once per effect key -> next step; on failure
    ``decide_recovery`` and apply it (insert steps, rerun on the next tier, open a breaker, ask via trips.ask /
    approvals, replan via PlannerService, end the item / goal). One plan per robot at a time. Publishes the loop events
    (plan.* / step.* / vla.* / target.grounded / camera.overlay) and mirrors one ``brain.step(kind='status')`` per
    step transition (``bridge.mirror_brain_step``); goal / trip changes go only through fleet.update_goal and
    services.trips (v0.2 rules)."""

    async def run_goal(self, robot_id: RobotId, goal: Goal) -> PlanOutcome:
        """plan (PlannerService) + run_plan. ``RobotBrainService.run_goal`` delegates here in PICKY_EXEC_MODE=plan and
        then finishes the goal itself: ``fleet.update_goal(goal_id, bridge.goal_update_from_outcome(outcome))`` and
        returns the fleet's terminal Goal. The runner patches goal ITEMS / progress and mirrors item status onto trip
        route stops (``bridge.ITEM_TO_STOP_STATUS``); it never sets the goal's status."""
        ...

    async def run_plan(self, plan: StepPlan, *, policy: RetryPolicy | None = None) -> PlanOutcome:
        """Run until completed / failed / cancelled (long-running; cancel the task or call ``cancel``). Raises only
        RobotError for fatal codes ('escalate': battery_low, unknown_robot, gripper_fault) WITHOUT failing the goal,
        so the fleet reassigns (v0.2 rule); CancelledError propagates after plan.completed(cancelled)."""
        ...

    async def run_tool_step(
        self, robot_id: RobotId, goal: Goal, tool_name: str, tool_input: Mapping[str, Any]
    ) -> ToolStepOutcome:
        """Tools mode: expand one v0.2 motion tool call, append its steps to the goal's live plan, run them with
        INNER_RECOVERIES only; outer decisions come back as ``ToolStepOutcome.error`` (BrainToolError)."""
        ...

    async def cancel(self, plan_id: PlanId, reason: str = "") -> PlanRunState:
        """Cancel the running exec (VlaService.cancel_robot), withdraw a pending question (approvals.cancel), mark
        CANCELLED. An in-flight effect commit finishes first (record_pick never lands without its cart line / goal
        item). Goal-backed plans are cancelled through ``fleet.cancel_goal`` (the fleet owns goal status and the brain
        task); call this directly only for goal-less dev plans (POST /api/dev/plans). A unit left in the gripper is
        reported in ``PlanRunState.holding_instance_id`` (never committed)."""
        ...

    async def get_plan(self, plan_id: PlanId) -> StepPlan:
        """Any revision (KeyError)."""
        ...

    async def current_plan(self, goal_id: GoalId) -> StepPlan | None:
        """Latest non-superseded revision for a goal (GET /api/goals/{id}/plan); in tools mode the live plan."""
        ...

    async def run_state(self, plan_id: PlanId) -> PlanRunState: ...

    async def attempts(self, plan_id: PlanId, step_id: PlanStepId | None = None) -> list[StepAttempt]: ...


@dataclass
class Services:
    """DI container. Every field is optional so partial wiring (tests, fakes) works. The loop fields (0.2.1) stay None
    until the gateway wires ``LOOP_SERVICE_FACTORIES``; None = the v0.2 path (``exec_mode`` -> 'direct')."""

    catalog: CatalogService | None = None
    recommender: RecommenderService | None = None
    routing: RoutingService | None = None
    robot: RobotService | None = None
    perception: PerceptionService | None = None
    trust: TrustService | None = None
    messaging: MessagingService | None = None
    agent: AgentService | None = None
    fleet: FleetService | None = None
    cameras: CameraService | None = None
    brain: RobotBrainService | None = None
    trips: TripController | None = None
    approvals: ApprovalBroker | None = None
    events: EventPublisher | None = None
    sim_link: SimLink | None = None
    # planner -> VLA -> verifier loop (0.2.1; LOOP_SERVICE_FACTORIES)
    planner: PlannerService | None = None
    runner: LoopRunner | None = None
    vla: VlaService | None = None
    verifier: VerifierService | None = None
    truth: TruthSource | None = None
    evidence: EvidenceCameras | None = None
    faults: FaultInjector | None = None

    def require(self, name: str) -> Any:
        """Return ``getattr(self, name)`` or raise a clear error if that service isn't wired."""
        value = getattr(self, name)
        if value is None:
            raise RuntimeError(f"service {name!r} is not wired into Services")
        return value

    @classmethod
    def field_names(cls) -> tuple[str, ...]:
        return tuple(f.name for f in fields(cls))


ARM_NAMES: tuple[str, ...] = (
    "catalog",
    "perception",
    "sim",
    "robot",
    "routing",
    "agent",
    "recommender",
    "trust",
    "messaging",
    "fleet",
    "gateway",
    "frontend",
)
"""All arms (folders under arms/). frontend = the web app (shopper + store staff + fleet views; no native app)."""

SERVICE_PROTOCOLS: dict[str, type] = {
    "catalog": CatalogService,
    "recommender": RecommenderService,
    "routing": RoutingService,
    "robot": RobotService,
    "perception": PerceptionService,
    "trust": TrustService,
    "messaging": MessagingService,
    "agent": AgentService,
    "fleet": FleetService,
}
"""Arms that provide their PRIMARY service (real via create(), fake via create_fake()); keys are both arm names and
Services field names, and the values accepted in PICKY_REAL_ARMS."""

EXTRA_SERVICE_PROTOCOLS: dict[str, type] = {
    "cameras": CameraService,
    "brain": RobotBrainService,
}
"""Services fields provided by an arm next to its primary service (see SERVICE_FACTORIES)."""

SERVICE_FACTORIES: dict[str, tuple[str, str, str]] = {
    **{name: (name, "create", "create_fake") for name in SERVICE_PROTOCOLS},
    "cameras": ("perception", "create_cameras", "create_cameras_fake"),
    "brain": ("agent", "create_brain", "create_brain_fake"),
}
"""Services field -> (arm, real factory, fake factory) exported by ``arms.<arm>``. A field is real when its own name
OR its arm is in PICKY_REAL_ARMS (e.g. ``PICKY_REAL_ARMS=brain`` = real robot brain, fake shopper agent); a real
factory that raises falls back to the fake."""

GATEWAY_PROTOCOLS: dict[str, type] = {
    "trips": TripController,
    "approvals": ApprovalBroker,
    "events": EventPublisher,
    "sim_link": SimLink,
}
"""Services fields the gateway itself provides."""

SERVICE_ARMS: tuple[str, ...] = tuple(SERVICE_PROTOCOLS)

LOOP_CORE_FIELDS: tuple[str, ...] = ("planner", "runner", "vla", "verifier")
"""Loop services that must ALL be wired before ``exec_mode`` leaves 'direct'."""


def exec_mode(services: Services, configured: str | None) -> ExecMode:
    """THE kill switch: 'direct' (exact v0.2) unless ``configured`` (settings PICKY_EXEC_MODE) is 'plan' / 'tools' AND
    planner, runner, vla and verifier are all wired."""
    if configured not in ("plan", "tools"):
        return "direct"
    if any(getattr(services, name, None) is None for name in LOOP_CORE_FIELDS):
        return "direct"
    return configured  # type: ignore[return-value]


LOOP_SERVICE_PROTOCOLS: dict[str, type] = {
    "planner": PlannerService,
    "runner": LoopRunner,
    "vla": VlaService,
    "verifier": VerifierService,
    "truth": TruthSource,
    "evidence": EvidenceCameras,
    "faults": FaultInjector,
}
"""Loop Services fields -> Protocol (0.2.1). ``VisionProvider`` is not a field: it lives inside the verifier."""

LOOP_SERVICE_FACTORIES: dict[str, tuple[str, str, str]] = {
    "planner": ("agent", "create_planner", "create_planner_fake"),
    "runner": ("agent", "create_runner", "create_runner_fake"),
    "vla": ("robot", "create_vla", "create_vla_fake"),
    "truth": ("robot", "create_truth", "create_truth_fake"),
    "faults": ("robot", "create_faults", "create_faults_fake"),
    "verifier": ("perception", "create_verifier", "create_verifier_fake"),
    "evidence": ("perception", "create_evidence", "create_evidence_fake"),
}
"""Loop Services field -> (arm, real factory, fake factory) exported by ``arms.<arm>`` (same rules as
``SERVICE_FACTORIES``: real when the field or its arm is in PICKY_REAL_ARMS; a raising real factory falls back to the
fake). Separate from ``SERVICE_FACTORIES`` on purpose: the gateway registry builds every SERVICE_FACTORIES entry, and
the arms export these factories only once they implement them. Gateway step: wire ``{**SERVICE_FACTORIES,
**LOOP_SERVICE_FACTORIES}`` and treat a missing loop factory as 'not wired' (None), never as an error."""

LOOP_ENV_VARS: dict[str, str] = {
    "PICKY_EXEC_MODE": "direct | plan | tools (default direct = exact v0.2 behaviour; the kill switch)",
    "PICKY_VLA_TIERS": "tiers to offer, e.g. 't0_scripted,t1_pretrained' (default t0_scripted)",
    "PICKY_VLA_T1_MODEL": "e.g. lerobot/smolvla_base (advertised by the Isaac worker as 'vla:t1:<model>')",
    "PICKY_VERIFIER_MODE": "decide | shadow | gt_only (default: shadow on 2D-fake stores, decide on Isaac)",
    "PICKY_VISION_PROVIDER": "perceptron | gemini | claude | fake (verifier backend; perceptron = THE real provider; "
    "default fake when PERCEPTRON_API_KEY is unset)",
    "PERCEPTRON_API_KEY": "Perceptron API key (the perceptron SDK reads it); unset = fake / ground truth only",
    "PERCEPTRON_MODEL": "default perceptron-mk1.5 (fallback perceptron-mk1)",
    "PERCEPTRON_BASE_URL": "default https://api.perceptron.inc/v1",
    "PICKY_VERIFY_TIMEOUT_S": "the runner's wait_for around each verifier call (RetryPolicy.degrade.verify_timeout_s, "
    "default 8); the verifier gets deadline_s = this - 1 s (verify.verify_deadline)",
    "PICKY_VISION_CALL_TIMEOUT_S": "cap per provider call inside the verifier (default 4; also bounded by the "
    "remaining deadline_s)",
    "PICKY_PLAN_TIMEOUT_S": "wait_for around the LLM planner draft call and the runner's replan call (default 20)",
    "PICKY_VLA_POLICY_URL": "Isaac worker side (vhagar): T1 policy server, e.g. ws://127.0.0.1:8000 (openpi wire)",
    "PICKY_VLA_POLICY_PROTOCOL": "openpi_ws (default; msgpack-numpy over websocket) | lerobot_grpc (later)",
    "PICKY_FAULTS": "demo fault scenario name from faults.DEMO_SCENARIOS (unset = no faults)",
    "PICKY_DEV": "1 enables /api/dev/faults, /api/dev/plans and /api/verify presets",
}
"""Env vars the loop reads (documentation; Settings fields + .env.example land in the utils PR). The LLM itself is
utils.llm's: PICKY_LLM_PROVIDER (gemini default | anthropic), GEMINI_API_KEY / ANTHROPIC_API_KEY."""

ArmFactory = Callable[[Services], Any]

__all__ = [
    "ARM_NAMES",
    "EXTRA_SERVICE_PROTOCOLS",
    "GATEWAY_PROTOCOLS",
    "LOOP_CORE_FIELDS",
    "LOOP_ENV_VARS",
    "LOOP_SERVICE_FACTORIES",
    "LOOP_SERVICE_PROTOCOLS",
    "SERVICE_ARMS",
    "SERVICE_FACTORIES",
    "SERVICE_PROTOCOLS",
    "AgentService",
    "ApprovalBroker",
    "ArmFactory",
    "CameraService",
    "CatalogService",
    "EventPublisher",
    "EvidenceCameras",
    "FaultInjector",
    "FleetService",
    "InboundHandler",
    "LoopRunner",
    "MessagingService",
    "PerceptionService",
    "PlannerService",
    "RecommenderService",
    "RobotBrainService",
    "RobotError",
    "RobotService",
    "RoutingService",
    "Services",
    "SimLink",
    "StatusCallback",
    "TripController",
    "TruthSource",
    "TrustService",
    "VerifierService",
    "VisionProvider",
    "VlaService",
    "exec_mode",
    "run_vla_to_terminal",
]
