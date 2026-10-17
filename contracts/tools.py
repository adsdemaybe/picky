"""LLM tool contracts for the agent brain (Anthropic tool-use format).

Tool names are fixed: query_inventory, plan_route, recommend, add_to_cart, request_approval, checkout.
The trip context (trip_id, user_id, store) is supplied by the tool executor, never by the model.

Side effects (CLAUDE.md §7): the executor changes the trip ONLY through ``services.trips`` (TripController) and asks
the shopper ONLY through ``services.approvals`` (ApprovalBroker):
- add_to_cart      -> services.trips.add_line(trip_id, line, approval_id=?)   (mandate-checked; may need approval)
- plan_route       -> services.trips.replan(trip_id, ...)
- request_approval -> services.approvals.request(ApprovalRequest(...))
- checkout         -> services.trips.start_checkout(trip_id, note)  (same path as POST /api/trips/{id}/checkout;
                      the mandate engine + explicit shopper approval decide, never the model)
- query_inventory  -> services.catalog.search(...);  recommend -> services.recommender.*

``TOOL_DEFINITIONS`` is non-strict (bounds/defaults kept in the schema; ``parse_tool_input`` validates).
``tool_definitions(strict=True)`` returns strict-mode definitions built with ``anthropic.transform_schema``.
"""

from typing import Any, Literal

from pydantic import Field, model_validator

from contracts.common import ONLINE_STORE_ID, Model, RecId, Sku, StoreId, TripId
from contracts.shopping import LineSource, RecKind

ToolName = Literal["query_inventory", "plan_route", "recommend", "add_to_cart", "request_approval", "checkout"]
TOOL_NAMES: tuple[str, ...] = (
    "query_inventory",
    "plan_route",
    "recommend",
    "add_to_cart",
    "request_approval",
    "checkout",
)

ToolApprovalKind = Literal["recommendation", "over_threshold", "substitution", "online_order", "pick_choice"]
"""trust.ApprovalKind minus 'checkout' (checkout approval is raised by the checkout flow, not by the model)."""


class QueryInventoryInput(Model):
    query: str = Field(description="What to look for, e.g. 'corn tortillas' or 'vegetarian protein'.")
    store_id: StoreId | None = Field(None, description="Limit to one store id; omit to search all stores + online.")
    category: str | None = Field(None, description="Optional category filter, e.g. 'produce'.")
    dietary: list[str] = Field(
        default_factory=list, description="Products must satisfy all of these, e.g. ['vegetarian']."
    )
    in_stock_only: bool = Field(False, description="Only return offers with stock > 0.")
    max_results: int = Field(5, ge=1, le=20)


class PlanRouteInput(Model):
    include_recommendations: bool = Field(True, description="Add recommendation detours as optional stops.")
    max_optional_stops: int | None = Field(None, ge=0, le=10, description="Cap on detours; omit for the default.")


class RecommendInput(Model):
    limit: int = Field(3, ge=1, le=10)
    kind: RecKind = "detour"
    focus: str | None = Field(None, description="Optional theme, e.g. 'spicy' or 'dessert'.")


class AddToCartInput(Model):
    sku: Sku
    qty: int = Field(1, ge=1, le=50)
    source: LineSource = "in_store"
    store_id: StoreId | None = Field(
        None, description="Store to source from; omit for the trip's store (or 'online' when source is 'online')."
    )
    rec_id: RecId | None = Field(None, description="Recommendation this add came from, if any.")
    approval_id: str | None = Field(None, description="Approval the shopper already granted for this add, if any.")
    reason: str | None = Field(None, description="Short note shown to the shopper.")

    @model_validator(mode="after")
    def _source_store(self) -> "AddToCartInput":
        if self.source == "online" and self.store_id is None:
            self.store_id = ONLINE_STORE_ID
        if (self.store_id == ONLINE_STORE_ID) != (self.source == "online"):
            raise ValueError("store_id 'online' requires source 'online' (and vice versa)")
        return self


class RequestApprovalInput(Model):
    kind: ToolApprovalKind = Field(
        description="What is being asked. Checkout approval is requested by the checkout tool, not here."
    )
    title: str = Field(description="Short question for the shopper, e.g. 'Swap to Jack cheese? $1 cheaper'.")
    body: str = ""
    sku: Sku | None = None
    rec_id: RecId | None = Field(None, description="Recommendation this asks about (kind='recommendation').")
    amount_cents: int | None = Field(None, ge=0)
    options: list[str] = Field(default_factory=list, description="Choice labels; empty = yes/no.")


class CheckoutInput(Model):
    note: str | None = Field(None, description="Optional message to show with the checkout approval.")


TOOL_INPUT_MODELS: dict[str, type[Model]] = {
    "query_inventory": QueryInventoryInput,
    "plan_route": PlanRouteInput,
    "recommend": RecommendInput,
    "add_to_cart": AddToCartInput,
    "request_approval": RequestApprovalInput,
    "checkout": CheckoutInput,
}

TOOL_DESCRIPTIONS: dict[str, str] = {
    "query_inventory": (
        "Search the catalog across the trip's store, other stores and the online store. Returns products with "
        "per-store price, stock, aisle location and active deals. Use it to resolve list items to SKUs and to "
        "decide in-store vs online sourcing."
    ),
    "plan_route": (
        "Plan (or re-plan) the robot's route through the store for the current shopping list: every in-store item "
        "is a mandatory stop and recommendations are optional detours. Returns the stop order, distance and the "
        "extra distance the detours add."
    ),
    "recommend": (
        "Get personalized recommendations for this shopper, each with a reason and signals. 'detour' = worth a stop "
        "on the route, 'checkout_batch' = ask once at checkout, 'near_expiry_offer' = discounted near-expiry items."
    ),
    "add_to_cart": (
        "Add a SKU to the trip cart (in-store pick or online order). The mandate engine checks it; if it is above "
        "the shopper's ask threshold the result says an approval is needed instead of adding it: ask with "
        "request_approval, then call add_to_cart again with that approval_id."
    ),
    "request_approval": (
        "Ask the shopper a yes/no or multiple-choice question on their active channel (web, voice or iMessage) and "
        "wait for the answer. Use for recommendation detours, substitutions, online orders and over-threshold items."
    ),
    "checkout": (
        "Start checkout for the current cart. Runs the deterministic mandate check and asks the shopper for explicit "
        "approval of the exact cart; payment only happens after they approve. You cannot pay without approval."
    ),
}


def _strip_titles(schema: Any) -> Any:
    """Drop pydantic's schema-level 'title' keys (but keep properties that happen to be named 'title')."""
    if isinstance(schema, list):
        return [_strip_titles(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "title" and isinstance(value, str):
            continue
        if key in ("properties", "$defs") and isinstance(value, dict):
            out[key] = {name: _strip_titles(sub) for name, sub in value.items()}
        else:
            out[key] = _strip_titles(value)
    return out


def tool_input_schema(name: str, strict: bool = False) -> dict[str, Any]:
    """JSON Schema (object) for a tool's input, without pydantic 'title' noise.

    ``strict=True`` runs ``anthropic.transform_schema`` (moves min/max/default into descriptions, closes objects),
    as required by strict tool use. Validation of bounds stays in ``parse_tool_input``.
    """
    model = TOOL_INPUT_MODELS[name]
    if strict:
        from anthropic import transform_schema  # base dep; imported lazily to keep contracts import light

        schema = transform_schema(model)
    else:
        schema = model.model_json_schema()
    schema = _strip_titles(schema)
    schema["type"] = "object"
    return schema


def tool_definition(name: str, strict: bool = False) -> dict[str, Any]:
    """One tool in Anthropic format: {name, description, input_schema} (+ ``strict: True`` when strict)."""
    tool: dict[str, Any] = {
        "name": name,
        "description": TOOL_DESCRIPTIONS[name],
        "input_schema": tool_input_schema(name, strict=strict),
    }
    if strict:
        tool["strict"] = True
    return tool


def tool_definitions(strict: bool = False) -> list[dict[str, Any]]:
    """All six tools, in TOOL_NAMES order."""
    return [tool_definition(n, strict=strict) for n in TOOL_NAMES]


TOOL_DEFINITIONS: list[dict[str, Any]] = tool_definitions()
"""Non-strict definitions: pass directly as ``tools=`` to ``anthropic.AsyncAnthropic().messages.create``."""


def parse_tool_input(name: str, data: dict[str, Any]) -> Model:
    """Validate a model-supplied tool input dict into its Pydantic model (raises ValidationError / KeyError)."""
    return TOOL_INPUT_MODELS[name].model_validate(data)


class ToolCallRecord(Model):
    """One executed tool call (for logs, transcripts and AgentReply)."""

    tool_use_id: str | None = None
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: Any = None
    ok: bool = True
    error: str | None = None
    duration_ms: float = 0.0


class AgentReply(Model):
    """Result of AgentService.respond. Side effects already happened through services.trips / services.approvals."""

    trip_id: TripId | None = None
    text: str
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    stop_reason: str | None = None
