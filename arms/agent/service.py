"""Real AgentService: Claude brain over ``utils.llm`` (tool loop, structured output, prompt caching).

Models come from settings via ``utils.llm.model``: brain = PICKY_MODEL_BRAIN (chat + intent expansion),
fast = PICKY_MODEL_FAST (voice turns, reply classification). Every LLM path falls back to the deterministic rules in
``logic.py`` / ``fake.py`` when the key is missing or the model output is unusable, so the trip never dies here.

Trust (CLAUDE.md §7): the LLM proposes, code decides. Budgets are parsed by regex (not the model); add_to_cart goes
through ``services.trips.add_line`` (mandate-checked); checkout goes through ``services.trips.start_checkout`` (mandate
engine + explicit shopper approval bound to the cart hash). The agent never calls ``trust.checkout``.

TODO (numbered, maps to CLAUDE.md §3 agent row / README scope):
  1. [P0] respond(): stream text deltas as agent.message (done=False chunks) instead of one final message.
  2. [P1] Voice: Deepgram STT/TTS turn handler (POST /api/trips/{id}/voice) using kind="fast" + low effort.
  3. [P1] Strict tools (``tool_definitions(strict=True)``) once the schemas are warmed at startup.
  4. [P1] compose_followup(): Haiku phrasing personalized from the profile (keep the deterministic SKU choice).
  5. [P2] Batched shoppers: expand several intents into one list with ``ListItem.for_user_id``.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from arms.agent import logic
from arms.agent.fake import FakeAgent
from contracts.api import AddLineResult
from contracts.catalog import SearchQuery
from contracts.common import ONLINE_STORE_ID, Channel, StoreId, UserId, money
from contracts.events import make_event
from contracts.fleet import GoalPriority, RequestDraft, RequesterRole, RequestIntent
from contracts.messaging import InboundMessage, OutboundMessage, ReplyInterpretation
from contracts.profile import UserProfile
from contracts.services import Services
from contracts.shopping import CartLine, Intent, ShoppingList
from contracts.tools import (
    TOOL_DEFINITIONS,
    TOOL_NAMES,
    AddToCartInput,
    AgentReply,
    CheckoutInput,
    PlanRouteInput,
    QueryInventoryInput,
    RecommendInput,
    RequestApprovalInput,
    ToolCallRecord,
)
from contracts.trip import Trip
from contracts.trust import ApprovalKind, ApprovalOption, ApprovalRequest
from utils.ids import new_id
from utils.log import get_logger

log = get_logger(__name__)

MAX_ITERS = 8
HISTORY_TURNS = 6

AGENT_SYSTEM = """You are Picky, a friendly shopping-assistant robot in a grocery store (and over text \
before/after the trip). You help the shopper get exactly what is on their list, within budget, and you \
suggest a few extras only when they clearly fit the shopper's tastes.

Rules:
- Use tools for facts. Never invent prices, stock, aisles or SKUs: call query_inventory.
- Money is shown as dollars to the shopper; tool inputs/outputs use integer cents.
- add_to_cart is checked by the shopper's mandate. If it says an approval is needed, ask with request_approval and \
retry add_to_cart with that approval_id. If it is denied, explain why and do not retry.
- Only call checkout when the shopper asks to pay/check out. Checkout always asks the shopper to approve the exact \
cart; you cannot pay on their behalf.
- Every suggestion must say why (taste, deal, near-expiry markdown). Never hide sponsored items.
- Be brief: 1-3 short sentences (voice replies: one sentence)."""

INTENT_SYSTEM = """You turn a shopper's request into a grocery shopping list for a US supermarket.
Return generic product queries (e.g. 'corn tortillas', 'ground beef', 'black beans'), not brands. qty is an integer \
count of selling units (packs, cans, bunches, each), sized for the number of servings. Add vegetarian options when \
guests are vegetarian (dietary=['vegetarian'] on that item). Respect the shopper's allergies and dislikes. \
budget_cents is the stated budget in integer cents (null if none)."""

REQUEST_SYSTEM = """You read one message typed into the Picky web app (a fleet of shopping robots in grocery \
stores) and say what it asks for. intent: 'shop' (the user wants groceries bought: list items as generic product \
queries with integer qty sized for the servings, vegetarian options get dietary=['vegetarian']), 'fetch' (staff: bring \
ONE item somewhere), 'shelf_scan' (staff: scan shelves / check stock; categories = category ids such as produce, \
dairy, meat, bakery, pantry, international, snacks, beverages, frozen, household, personal_care), 'return_to_dock' \
(a robot should go charge), 'status' (a question about progress, an order or where a product is), or 'chat'. \
robot_name / store_name only when the text names one. Ask a clarifying_question only when you cannot act at all. \
reply = one short friendly sentence for the user."""

REPLY_SYSTEM = """Classify a shopper's text reply. kind: 'approval' (answers one of the pending questions; set \
approval_id from the list, decision approve/reject, choice/choices = option ids), 'feedback' (rating 1-5 about a \
purchased product; sku from the follow-up skus), 'intent' (a new shopping request; intent_text), or 'chat'."""


# --- structured-output drafts (converted into contracts models in code) ------------------------------------------
class DraftItemOut(BaseModel):
    query: str
    qty: int = Field(1, ge=1, le=50)
    category: str | None = None
    dietary: list[str] = Field(default_factory=list)
    required: bool = True
    notes: str = ""


class IntentDraft(BaseModel):
    title: str | None = None
    servings: int | None = Field(None, ge=1, le=100)
    budget_cents: int | None = Field(None, ge=0)
    dietary_notes: list[str] = Field(default_factory=list)
    items: list[DraftItemOut] = Field(default_factory=list)


class RequestDraftOut(BaseModel):
    intent: RequestIntent
    summary: str
    title: str | None = None
    servings: int | None = Field(None, ge=1, le=100)
    items: list[DraftItemOut] = Field(default_factory=list)
    robot_name: str | None = None
    store_name: str | None = None
    categories: list[str] = Field(default_factory=list)
    aisle_numbers: list[int] = Field(default_factory=list)
    urgent: bool = False
    reply: str = ""
    clarifying_question: str | None = None
    confidence: float = Field(0.8, ge=0, le=1)


# --- tool executor -----------------------------------------------------------------------------------------------
class ToolExecutor:
    """Runs the six LLM tools for one trip. Trip context comes from here, never from the model."""

    def __init__(self, services: Services, trip: Trip, channel: Channel = "web") -> None:
        self.services = services
        self.trip = trip
        self.channel = channel

    def handlers(self) -> dict[str, Any]:
        handlers = {
            "query_inventory": self.query_inventory,
            "plan_route": self.plan_route,
            "recommend": self.recommend,
            "add_to_cart": self.add_to_cart,
            "request_approval": self.request_approval,
            "checkout": self.checkout,
        }
        assert tuple(handlers) == TOOL_NAMES, "tool handler mapping must cover contracts.tools.TOOL_NAMES exactly"
        return handlers

    async def _fresh_trip(self) -> Trip:
        trips = self.services.trips
        if trips is None:
            return self.trip
        try:
            self.trip = await trips.get_trip(self.trip.trip_id)
        except Exception as e:
            log.warning("get_trip failed, using the snapshot: %s", e)
        return self.trip

    async def query_inventory(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        inp = QueryInventoryInput.model_validate(args)
        catalog = self.services.require("catalog")
        hits = await catalog.search(
            SearchQuery(
                text=inp.query,
                category=inp.category,
                dietary=inp.dietary,
                store_ids=[inp.store_id] if inp.store_id else None,
                in_stock_only=inp.in_stock_only,
                limit=inp.max_results,
            )
        )
        return [
            {
                "sku": h.product.sku,
                "name": h.product.name,
                "category": h.product.category,
                "tags": h.product.tags,
                "dietary": h.product.dietary,
                "offers": [
                    {
                        "store_id": o.store_id,
                        "price_cents": o.price_cents,
                        "qty": o.qty,
                        "aisle": o.location.aisle_label if o.location else None,
                        "deal": o.deal.reason if o.deal else None,
                    }
                    for o in h.offers
                ],
            }
            for h in hits
        ]

    async def plan_route(self, args: dict[str, Any]) -> dict[str, Any]:
        inp = PlanRouteInput.model_validate(args)
        route = await self.services.require("trips").replan(
            self.trip.trip_id,
            include_recommendations=inp.include_recommendations,
            max_optional_stops=inp.max_optional_stops,
        )
        return {
            "route_id": route.route_id,
            "stops": [
                {"stop_id": s.stop_id, "kind": s.kind, "label": s.label, "skus": s.skus, "detour_m": s.detour_m}
                for s in route.ordered_stops()
            ],
            "total_distance_m": round(route.total_distance_m, 1),
            "extra_distance_m": None if route.extra_distance_m is None else round(route.extra_distance_m, 1),
            "skipped_optional": route.skipped_optional,
        }

    async def recommend(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        inp = RecommendInput.model_validate(args)
        trip = await self._fresh_trip()
        rec = self.services.require("recommender")
        exclude = [r.sku for r in trip.recs]
        if inp.kind == "checkout_batch":
            recs = await rec.checkout_batch(trip.user_id, trip.store_id, trip.cart, inp.limit, inp.focus, exclude)
        elif inp.kind == "near_expiry_offer":
            recs = await rec.near_expiry_offers(
                trip.user_id, trip.store_id, inp.limit, inp.focus, exclude, trip.trip_id
            )
        else:
            sl = trip.shopping_list or ShoppingList(list_id=f"list_{trip.trip_id}", user_id=trip.user_id)
            recs = await rec.recommend(trip.user_id, trip.store_id, sl, inp.limit, inp.focus, exclude)
        return [
            {
                "rec_id": r.rec_id,
                "sku": r.sku,
                "name": r.product_name,
                "price_cents": r.price_cents,
                "reason": r.reason,
                "kind": r.kind,
                "sponsored": r.sponsored,
                "interest": r.interest,
            }
            for r in recs
        ]

    async def add_to_cart(self, args: dict[str, Any]) -> dict[str, Any]:
        inp = AddToCartInput.model_validate(args)
        trip = await self._fresh_trip()
        catalog = self.services.require("catalog")
        store: StoreId = inp.store_id or (ONLINE_STORE_ID if inp.source == "online" else trip.store_id)
        source = "online" if store == ONLINE_STORE_ID else "in_store"
        offers = await catalog.get_offers(inp.sku, [store])
        offer = next((o for o in offers if o.store_id == store), None)
        if offer is None:
            raise ValueError(f"{inp.sku} is not sold at {store}; query_inventory for alternatives")
        product = await catalog.get_product(inp.sku)
        line = CartLine(
            line_id=new_id("line"),
            sku=inp.sku,
            name=product.name if product else None,
            category=product.category if product else "uncategorized",
            qty=inp.qty,
            unit_price_cents=offer.price_cents,
            source=source,
            store_id=store,
            slot_id=offer.location.slot_id if offer.location else None,
            rec_id=inp.rec_id,
            deal_id=offer.deal.deal_id if offer.deal else None,
            user_id=trip.user_id,
        )
        result: AddLineResult = await self.services.require("trips").add_line(
            trip.trip_id, line, approval_id=inp.approval_id
        )
        d = result.decision
        return {
            "added": result.added,
            "line_id": result.line_id,
            "verdict": str(d.verdict),
            "reasons": [str(r) for r in d.reasons],
            "messages": d.messages,
            "cart_total_cents": result.cart.total_cents,
            "hint": None
            if result.added or str(d.verdict) == "deny"
            else "needs approval: call request_approval (kind over_threshold, same sku, amount_cents >= the line "
            "total), then add_to_cart again with that approval_id (one approval covers one add)",
        }

    async def request_approval(self, args: dict[str, Any]) -> dict[str, Any]:
        inp = RequestApprovalInput.model_validate(args)
        trip = self.trip
        channels: list[Channel] = ["web"] if self.channel == "web" else ["web", self.channel]
        req = ApprovalRequest(
            approval_id=new_id("appr"),
            trip_id=trip.trip_id,
            user_id=trip.user_id,
            kind=ApprovalKind(inp.kind),
            title=inp.title,
            body=inp.body,
            amount_cents=inp.amount_cents,
            sku=inp.sku,
            rec_id=inp.rec_id,
            options=[ApprovalOption(option_id=str(i + 1), label=label) for i, label in enumerate(inp.options)],
            channels=channels,
        )
        resp = await self.services.require("approvals").request(req)
        return {
            "approval_id": req.approval_id,
            "decision": resp.decision,
            "approved": resp.approved,
            "chosen": resp.chosen(),
        }

    async def checkout(self, args: dict[str, Any]) -> dict[str, Any]:
        inp = CheckoutInput.model_validate(args)
        status = await self.services.require("trips").start_checkout(self.trip.trip_id, inp.note)
        return {
            "state": str(status.state),
            "total_cents": status.total_cents,
            "verdict": str(status.decision.verdict) if status.decision else None,
            "awaiting_shopper_approval": status.approval is not None,
            "paid": status.receipt is not None and status.receipt.status == "succeeded",
        }


def trip_context(trip: Trip) -> str:
    sl = trip.shopping_list
    lines = [
        f"trip_id={trip.trip_id} store={trip.store_id} state={trip.state}",
        f"cart_total={money(trip.cart.total_cents)} budget={money(trip.budget_cents) if trip.budget_cents else 'none'}",
    ]
    if sl:
        lines.append(
            "list: " + "; ".join(f"{i.qty}x {i.query} [{i.sourcing}{' ' + i.sku if i.sku else ''}]" for i in sl.items)
        )
    if trip.recs:
        lines.append("recs: " + "; ".join(f"{r.sku} ({r.reason})" for r in trip.recs[:5]))
    if trip.cart.lines:
        lines.append(
            "cart: " + "; ".join(f"{c.qty}x {c.name or c.sku} {c.status}" for c in trip.cart.active_lines()[:20])
        )
    return "\n".join(lines)


# --- the agent ---------------------------------------------------------------------------------------------------
class ClaudeAgent:
    def __init__(self, services: Services, client: Any = None) -> None:
        self._services = services
        self._client = client
        self._fallback = FakeAgent(services)
        self._history: dict[str, list[dict[str, Any]]] = {}

    def _llm_ready(self) -> bool:
        from utils import llm

        return self._client is not None or llm.is_configured()

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
        """Claude structured output -> RequestDraft; code resolves names, money and the list (rules on failure)."""
        from arms.agent import interpret

        if not self._llm_ready():
            return await self._fallback.interpret_request(
                text, user_id, store_id, role=role, profile=profile, history=history
            )
        from utils.llm import complete_json

        hints = interpret.load_hints()
        turns = "".join(f"\n{'User' if n % 2 == 0 else 'You asked'}: {h}" for n, h in enumerate(history or []))
        ctx = (
            f"Requester role: {role}. Selected store: {store_id or 'none'}.\n"
            f"Robots: {', '.join(f'{name} ({rid}, {sid})' for rid, (name, sid) in hints.robots.items())}.\n"
            f"Stores: {', '.join(f'{n} ({sid})' for sid, n in hints.store_names.items()) or 'store_ave, store_hill'}."
            + (f"\nEarlier turns:{turns}" if turns else "")
            + f"\nMessage: {text!r}"
        )
        if profile is not None:
            ctx += f"\nShopper: dietary={profile.dietary} allergies={profile.allergies} dislikes={profile.dislikes}"
        try:
            out = await complete_json(ctx, RequestDraftOut, system=REQUEST_SYSTEM, kind="brain", client=self._client)
        except Exception as e:
            log.warning("interpret_request via LLM failed (%s); using rules", e)
            await self._error(None, f"request interpretation fell back to rules: {e}", "llm_fallback")
            return await self._fallback.interpret_request(
                text, user_id, store_id, role=role, profile=profile, history=history
            )
        return await self._draft_from_llm(out, text, user_id, store_id, role, profile, history, hints)

    async def _draft_from_llm(
        self,
        out: RequestDraftOut,
        text: str,
        user_id: UserId,
        store_id: StoreId | None,
        role: RequesterRole,
        profile: UserProfile | None,
        history: list[str] | None,
        hints: Any,
    ) -> RequestDraft:
        """The LLM proposes; code decides: only known robots / stores / categories survive, the budget is parsed by
        regex, and the shopping list is built (and SKU-guessed) by the deterministic helpers."""
        from arms.agent import interpret

        full = interpret.combined_text(text, history)
        robot = interpret.find_robot(full, hints)
        if robot is None and out.robot_name:
            robot = interpret.find_robot(out.robot_name, hints)
        store = interpret.find_store(full, hints) or (
            interpret.find_store(out.store_name, hints) if out.store_name else None
        )
        if store is None and robot is not None:
            store = hints.robots[robot][1]
        store = store or store_id
        intent: RequestIntent = out.intent
        if intent == "fetch" and role != "staff":
            intent = "shop"  # shopper purchases always go through a trip (mandate + approval + checkout)
        cats = [c for c in out.categories if c in hints.categories] or interpret.find_categories(full, hints)
        labels = hints.aisles.get(store or "", {})
        aisles = [labels[f"aisle {n}"] for n in out.aisle_numbers if f"aisle {n}" in labels]
        priority: GoalPriority = "urgent" if out.urgent else "normal"
        shopping_list = None
        if intent in ("shop", "fetch") and out.clarifying_question is None:
            parsed = logic.parse_intent_text(full, profile)
            items = [
                logic.DraftItem(i.query, i.qty, i.category, [d.lower() for d in i.dietary], i.required, i.notes)
                for i in out.items
            ] or logic.draft_items(parsed)
            if intent == "fetch":
                items = items[:1]
            budget = parsed.budget_cents
            if budget is None and profile is not None:
                budget = profile.default_budget_cents
            intent_obj = Intent(intent_id=new_id("int"), user_id=user_id, text=full, trip_id=None)
            sl = logic.build_shopping_list(
                intent_obj,
                items,
                title=out.title or logic.title_for(parsed),
                servings=out.servings or parsed.servings,
                budget_cents=budget,
                dietary_notes=parsed.dietary_notes,
            )
            if not sl.items:
                return await self._fallback.interpret_request(
                    text, user_id, store_id, role=role, profile=profile, history=history
                )
            guessed = await logic.guess_skus(self._services.catalog, sl.items, profile)
            shopping_list = sl.model_copy(update={"items": guessed})
        reply = out.reply
        if intent == "status":
            answer = await interpret.answer_question(self._services.catalog, text, store)
            reply = answer or reply
        return RequestDraft(
            intent=intent,
            summary=out.summary,
            shopping_list=shopping_list,
            store_hint=store,
            robot_hint=robot,
            categories=cats if intent == "shelf_scan" else [],
            aisle_ids=aisles if intent == "shelf_scan" else [],
            priority=priority,
            reply=reply,
            clarifying_question=out.clarifying_question,
            confidence=out.confidence,
        )

    async def expand_intent(self, intent: Intent, profile: UserProfile | None = None) -> ShoppingList:
        if not self._llm_ready():
            return await self._fallback.expand_intent(intent, profile)
        from utils.llm import complete_json

        prof = ""
        if profile is not None:
            prof = (
                f"\nShopper: household={profile.household_size} dietary={profile.dietary} "
                f"allergies={profile.allergies} dislikes={profile.dislikes}"
            )
        try:
            draft = await complete_json(
                f"Request: {intent.text!r}{prof}",
                IntentDraft,
                system=INTENT_SYSTEM,
                kind="brain",
                client=self._client,
            )
        except Exception as e:  # LLMNotConfigured / LLMOutputError / API errors -> rules
            log.warning("intent expansion via LLM failed (%s); using rules", e)
            await self._error(intent.trip_id, f"intent expansion fell back to rules: {e}", "llm_fallback")
            return await self._fallback.expand_intent(intent, profile)

        parsed = logic.parse_intent_text(intent.text, profile)
        budget = parsed.budget_cents  # money from code, not the model
        if budget is None:
            budget = draft.budget_cents if draft.budget_cents is not None else None
        if budget is None and profile is not None:
            budget = profile.default_budget_cents
        items = [
            logic.DraftItem(i.query, i.qty, i.category, [d.lower() for d in i.dietary], i.required, i.notes)
            for i in draft.items
        ] or logic.draft_items(parsed)
        sl = logic.build_shopping_list(
            intent,
            items,
            title=draft.title or logic.title_for(parsed),
            servings=draft.servings or parsed.servings,
            budget_cents=budget,
            dietary_notes=draft.dietary_notes or parsed.dietary_notes,
        )
        guessed = await logic.guess_skus(self._services.catalog, sl.items, profile)
        return sl.model_copy(update={"items": guessed})

    async def source_list(self, shopping_list: ShoppingList, store_id: StoreId) -> ShoppingList:
        return await logic.source_items(self._services.catalog, shopping_list, store_id)

    async def respond(self, trip: Trip, text: str, channel: Channel = "web") -> AgentReply:
        if not self._llm_ready():
            return await self._fallback.respond(trip, text, channel)
        from utils.llm import run_tool_loop

        await self._fallback._publish(trip.trip_id, text, "user", channel)
        executor = ToolExecutor(self._services, trip, channel)
        history = self._history.setdefault(trip.trip_id, [])
        user_msg = {"role": "user", "content": f"<trip_context>\n{trip_context(trip)}\n</trip_context>\n\n{text}"}
        try:
            result = await run_tool_loop(
                [*history, user_msg],
                TOOL_DEFINITIONS,
                executor.handlers(),
                system=AGENT_SYSTEM,
                kind="fast" if channel == "voice" else "brain",
                max_iters=MAX_ITERS,
                client=self._client,
            )
        except Exception as e:
            log.warning("agent tool loop failed (%s); using canned reply", e)
            await self._error(trip.trip_id, f"agent LLM failed: {e}", "llm_error")
            reply, calls = await self._fallback._canned(trip, text)
            await self._fallback._publish(trip.trip_id, reply, "assistant", channel)
            return AgentReply(trip_id=trip.trip_id, text=reply, tool_calls=calls, stop_reason="fallback")

        reply_text = result.text.strip() or "Done."
        history.extend([{"role": "user", "content": text}, {"role": "assistant", "content": reply_text}])
        del history[: max(0, len(history) - 2 * HISTORY_TURNS)]
        await self._fallback._publish(trip.trip_id, reply_text, "assistant", channel)
        calls = [
            ToolCallRecord(
                tool_use_id=c.id,
                name=c.name,
                input=c.input,
                output=_maybe_json(c.output),
                ok=not c.is_error,
                error=c.output if c.is_error else None,
            )
            for c in result.tool_calls
        ]
        stop = "max_iters" if result.hit_max_iters else result.stop_reason
        return AgentReply(trip_id=trip.trip_id, text=reply_text, tool_calls=calls, stop_reason=stop)

    async def interpret_reply(
        self, message: InboundMessage, pending: list[ApprovalRequest], trip: Trip | None = None
    ) -> ReplyInterpretation:
        rules = logic.interpret_reply_rules(message, pending, trip)
        if rules.kind != "chat" or not self._llm_ready():
            return rules
        from utils.llm import complete_json

        ctx = {
            "text": message.text,
            "pending": [
                {
                    "approval_id": a.approval_id,
                    "kind": str(a.kind),
                    "title": a.title,
                    "options": [o.option_id for o in a.options],
                }
                for a in pending
            ],
            "followup_skus": list(trip.followup.about_skus) if trip and trip.followup else [],
        }
        try:
            out = await complete_json(
                json.dumps(ctx), ReplyInterpretation, system=REPLY_SYSTEM, kind="fast", client=self._client
            )
        except Exception as e:
            log.warning("reply classification via LLM failed (%s); using rules", e)
            return rules
        valid_ids = {a.approval_id for a in pending}
        if out.kind == "approval" and out.approval_id not in valid_ids:
            return rules  # the model named an approval that isn't pending: don't act on it
        if out.kind == "feedback" and out.sku is None and ctx["followup_skus"]:
            out = out.model_copy(update={"sku": ctx["followup_skus"][0]})
        return out

    async def compose_followup(self, trip: Trip) -> OutboundMessage:
        # TODO(4): Haiku phrasing; the SKU choice stays deterministic.
        return logic.compose_followup_rules(trip)

    async def _error(self, trip_id: str | None, message: str, code: str) -> None:
        events = self._services.events
        if events is None:
            return
        try:
            await events.publish(
                make_event("error", trip_id=trip_id, source="agent", arm="agent", message=message, code=code)
            )
        except Exception as e:
            log.warning("publish error event failed: %s", e)


def _maybe_json(s: str) -> Any:
    try:
        return json.loads(s)
    except (ValueError, TypeError):
        return s
