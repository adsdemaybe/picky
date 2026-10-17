"""Service Protocols (one per arm) and the dependency-injection container.

Rules (CLAUDE.md §4):
- Each arm's ``create(services)`` / ``create_fake(services)`` returns an object satisfying its Protocol below.
- Factories must NOT call other services at construction time (only at call time), so wiring order is irrelevant.
- Arms never import other arms; they reach each other only through ``services.<field>`` at call time.
- The gateway owns the container, the Trip (``TripController``), the ApprovalBroker, the EventPublisher and the
  SimLink. Arms never mutate a ``Trip`` they are handed; every trip change goes through ``services.trips``.
- Events: the gateway publishes everything except ``agent.message`` (agent) and ``error`` (any arm); see
  ``contracts.events.EVENT_PRODUCERS``.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, fields
from typing import Any, Protocol, runtime_checkable

from contracts.api import AddLineResult, CheckoutStatus
from contracts.catalog import (
    Deal,
    MerchantDashboard,
    NearExpiryItem,
    Product,
    ProductLocation,
    ScanApplyResult,
    SearchHit,
    SearchQuery,
    StoreInfo,
    StoreOffer,
)
from contracts.common import (
    ApprovalId,
    CameraName,
    Channel,
    InstanceId,
    LineId,
    Pose2D,
    Sku,
    SlotId,
    StoreId,
    TripId,
    UserId,
)
from contracts.events import Event
from contracts.messaging import InboundMessage, OutboundMessage, ReplyInterpretation, SendResult
from contracts.perception import BasketCheck, PickChoice, ScanReport
from contracts.planogram import Planogram
from contracts.profile import Feedback, ProfileUpdate, PurchaseRecord, UserProfile
from contracts.routing import GridSpec, Route, RouteRequest
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
        """Update stock / freshness (per lot) / mispricing / location from a perception scan.

        Stock: ``count_is_exact`` -> qty = observed_count; else only observed_count == 0 marks the slot empty.
        """
        ...

    async def record_pick(
        self, store_id: StoreId, slot_id: SlotId, sku: Sku, qty: int = 1, trip_id: TripId | None = None
    ) -> ScanApplyResult:
        """Decrement stock after the robot picked (gateway calls it on item.picked); feeds HeatmapCell.picks."""
        ...

    async def merchant_dashboard(self, store_id: StoreId) -> MerchantDashboard: ...


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
    """Robot control. Real impl drives Isaac via services.sim_link; the fake is a 2D kinematic sim.

    Every method raises ``contracts.sim.RobotError`` (``.code`` = SimErrorCode) on failure. One motion at a time
    (navigate/scan/pick/place_in_basket/reset; else code 'busy'); capture/list_instances/state/stop always work.
    """

    async def state(self) -> RobotState: ...

    async def reset(self, store_id: StoreId) -> RobotState:
        """Load/reset the store scene and put the robot at its home pose. The real impl fetches
        ``services.catalog.get_planogram(store_id)`` + products at call time and pushes them in ResetCmd."""
        ...

    async def navigate(self, waypoints: list[Pose2D]) -> RobotState:
        """Follow waypoints (world frame); resolves on arrival at the final pose incl. its theta."""
        ...

    async def stop(self) -> RobotState:
        """Cancel the in-flight motion (it raises RobotError('cancelled')) and hold position."""
        ...

    async def scan(self, slot_id: SlotId) -> CameraFrame:
        """Point the head camera at a slot and capture a RAW (unblurred) frame; sim/fake fill ``frame.instances``
        (ground truth incl. pixel bbox, lot_id, freshness) so perception can crop and rank."""
        ...

    async def capture(self, camera: CameraName = "head") -> CameraFrame:
        """Capture a RAW (unblurred) frame from a camera (``instances`` filled in sim/fake)."""
        ...

    async def list_instances(self, slot_id: SlotId | None = None) -> list[ItemInstance]:
        """Ground-truth instances (in a slot, or everywhere incl. basket); bbox is None here."""
        ...

    async def pick(self, slot_id: SlotId, sku: Sku, instance_id: InstanceId | None = None) -> ItemInstance: ...

    async def place_in_basket(self) -> RobotState: ...

    def frames(self) -> AsyncIterator[CameraFrame]:
        """Live RAW frames (gateway blurs via perception before streaming on /ws/frames)."""
        ...

    def states(self) -> AsyncIterator[RobotState]:
        """Robot state updates (pose/status) as they happen."""
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
    """The LLM brain. Never mutates a Trip it is handed: side effects go through services.trips / approvals."""

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
        """Re-plan from the robot's current pose over the remaining stops; stores Trip.route, emits route.planned."""
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

    def pending(self, trip_id: TripId | None = None) -> list[ApprovalRequest]: ...


@runtime_checkable
class EventPublisher(Protocol):
    """Gateway-owned event bus front door. Assigns bus-wide ``seq`` and fans out to websockets."""

    async def publish(self, event: Event) -> Event: ...


@dataclass
class Services:
    """DI container. Every field is optional so partial wiring (tests, fakes) works."""

    catalog: CatalogService | None = None
    recommender: RecommenderService | None = None
    routing: RoutingService | None = None
    robot: RobotService | None = None
    perception: PerceptionService | None = None
    trust: TrustService | None = None
    messaging: MessagingService | None = None
    agent: AgentService | None = None
    trips: TripController | None = None
    approvals: ApprovalBroker | None = None
    events: EventPublisher | None = None
    sim_link: SimLink | None = None

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
    "gateway",
    "frontend",
)
"""All arms (folders under arms/)."""

SERVICE_PROTOCOLS: dict[str, type] = {
    "catalog": CatalogService,
    "recommender": RecommenderService,
    "routing": RoutingService,
    "robot": RobotService,
    "perception": PerceptionService,
    "trust": TrustService,
    "messaging": MessagingService,
    "agent": AgentService,
}
"""Arms that provide a service (real via create(), fake via create_fake()); keys are Services field names and the
values accepted in PICKY_REAL_ARMS."""

GATEWAY_PROTOCOLS: dict[str, type] = {
    "trips": TripController,
    "approvals": ApprovalBroker,
    "events": EventPublisher,
    "sim_link": SimLink,
}
"""Services fields the gateway itself provides."""

SERVICE_ARMS: tuple[str, ...] = tuple(SERVICE_PROTOCOLS)

ArmFactory = Callable[[Services], Any]

__all__ = [
    "ARM_NAMES",
    "GATEWAY_PROTOCOLS",
    "SERVICE_ARMS",
    "SERVICE_PROTOCOLS",
    "AgentService",
    "ApprovalBroker",
    "ArmFactory",
    "CatalogService",
    "EventPublisher",
    "InboundHandler",
    "MessagingService",
    "PerceptionService",
    "RecommenderService",
    "RobotError",
    "RobotService",
    "RoutingService",
    "Services",
    "SimLink",
    "TripController",
    "TrustService",
]
