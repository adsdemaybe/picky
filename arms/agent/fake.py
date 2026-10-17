"""Deterministic, rule-based AgentService (base deps only, no LLM).

- expand_intent: regex parse of the demo phrases ('tacos for 6, under $40, one vegetarian') + catalog search at call
  time for SKU guesses (personalized by the profile, so trip 2 ranks a spicier salsa first after feedback).
- source_list: deterministic split sourcing (in store -> robot, OOS / online-only -> online order).
- respond: canned, helpful replies (budget, where-is, recs, route, checkout) + agent.message events.
- interpret_reply: yes/no/y/n/👍/numbers/ratings via regex.
- compose_followup: 'How was the salsa? Reply 1-5.'
"""

from __future__ import annotations

import re
from typing import Any

from arms.agent import logic
from contracts.common import ONLINE_STORE_ID, Channel, StoreId, money
from contracts.events import make_event
from contracts.messaging import InboundMessage, OutboundMessage, ReplyInterpretation
from contracts.profile import UserProfile
from contracts.services import Services
from contracts.shopping import Intent, ShoppingList
from contracts.tools import AgentReply, ToolCallRecord
from contracts.trip import Trip
from contracts.trust import ApprovalRequest
from utils.ids import IdFactory
from utils.log import get_logger

log = get_logger(__name__)

_WHERE_RE = re.compile(
    r"\b(?:where(?:'s| is| are)?(?: the)?|find(?: me)?(?: the)?|do you have(?: any)?|is there(?: any)?|"
    r"have any|in stock)\s+(.+?)[?.!]*$",
    re.I,
)


class FakeAgent:
    """Rule-based AgentService. Never mutates the Trip; side effects only via services.trips / events."""

    def __init__(self, services: Services) -> None:
        self._services = services
        self._ids = IdFactory("agent-fake")

    # -- planning ------------------------------------------------------------------------------------------------
    async def expand_intent(self, intent: Intent, profile: UserProfile | None = None) -> ShoppingList:
        return await logic.expand_intent_rules(self._services.catalog, intent, profile)

    async def source_list(self, shopping_list: ShoppingList, store_id: StoreId) -> ShoppingList:
        return await logic.source_items(self._services.catalog, shopping_list, store_id)

    # -- chat ----------------------------------------------------------------------------------------------------
    async def respond(self, trip: Trip, text: str, channel: Channel = "web") -> AgentReply:
        await self._publish(trip.trip_id, text, "user", channel)
        reply, calls = await self._canned(trip, text)
        await self._publish(trip.trip_id, reply, "assistant", channel)
        return AgentReply(trip_id=trip.trip_id, text=reply, tool_calls=calls, stop_reason="end_turn")

    async def _canned(self, trip: Trip, text: str) -> tuple[str, list[ToolCallRecord]]:
        low = text.lower().strip()
        cart = trip.cart
        budget = trip.budget_cents

        if re.search(r"\b(?:check ?out|pay|i'?m done|done shopping|that'?s all)\b", low):
            trips = self._services.trips
            if trips is None:
                return "Ready when you are. Checkout needs your explicit approval of the exact cart.", []
            try:
                status = await trips.start_checkout(trip.trip_id)
            except Exception as e:
                log.warning("start_checkout failed: %s", e)
                return "I couldn't start checkout just now; try again in a moment.", [
                    ToolCallRecord(name="checkout", ok=False, error=str(e))
                ]
            call = ToolCallRecord(name="checkout", output=status.model_dump(mode="json"))
            return (
                f"Checkout started: {money(status.total_cents)}. Please approve the exact cart to pay; "
                "nothing is charged until you do.",
                [call],
            )

        if re.search(r"\b(?:budget|spent|total|how much|cost so far|over budget)\b", low):
            total = cart.total_cents
            if budget is None:
                return f"Your cart is at {money(total)} so far (no budget set).", []
            left = budget - total
            status = f"{money(left)} left" if left >= 0 else f"{money(-left)} over"
            return f"You're at {money(total)} of your {money(budget)} budget ({status}).", []

        m = _WHERE_RE.search(text.strip())
        if m:
            query = m.group(1).strip()
            return await self._where(trip, query)

        if re.search(r"\b(?:recommend|suggest|what else|ideas|deals?)\b", low):
            recs = trip.recs[:3]
            if not recs:
                return "No suggestions yet for this trip; I'll flag good ones on the way.", []
            parts = [f"{r.product_name or r.sku} ({r.reason})" for r in recs]
            return "A few ideas: " + "; ".join(parts) + ". Want me to add any?", []

        if re.search(r"\b(?:route|next stop|how long|stops?|where are we)\b", low):
            route = trip.route
            if route is None:
                return "The route isn't planned yet; it'll be ready once your list is sourced.", []
            n = len([s for s in route.stops if s.kind not in ("start", "end")])
            return f"{n} stops planned, about {route.total_distance_m:.0f} m total.", []

        if re.search(r"^(?:hi|hello|hey|yo|help)\b", low):
            return (
                "Hi! I'm Picky. I can find items, track your budget, suggest add-ons and check you out. "
                "Try 'where are the tortillas?' or 'how much have I spent?'",
                [],
            )

        return (
            "Got it. I can find items ('where is the salsa?'), check your budget, suggest add-ons, or start checkout.",
            [],
        )

    async def _where(self, trip: Trip, query: str) -> tuple[str, list[ToolCallRecord]]:
        hits = await logic.search_candidates(
            self._services.catalog, query, store_ids=[trip.store_id, ONLINE_STORE_ID], limit=5
        )
        out: list[dict[str, Any]] = [{"sku": h.product.sku, "name": h.product.name} for h in hits]
        call = ToolCallRecord(name="query_inventory", input={"query": query}, output=out)
        if not hits:
            return f"I couldn't find {query} here or online.", [call]
        hit = hits[0]
        here = hit.offer_for(trip.store_id)
        if here is not None and here.in_stock:
            where = here.location.aisle_label if here.location and here.location.aisle_label else "in this store"
            return f"{hit.product.name} is {where}, {money(here.price_cents)}.", [call]
        online = hit.offer_for(ONLINE_STORE_ID)
        if online is not None and online.in_stock:
            text = f"{hit.product.name} is out of stock here; I can add it to the online order for "
            return text + f"{money(online.price_cents)}.", [call]
        return f"{hit.product.name} is out of stock right now.", [call]

    async def _publish(self, trip_id: str, text: str, role: str, channel: Channel) -> None:
        events = self._services.events
        if events is None:
            return
        try:
            await events.publish(
                make_event(
                    "agent.message",
                    trip_id=trip_id,
                    source="agent",
                    message_id=self._ids("msg"),
                    role=role,
                    text=text,
                    done=True,
                    channel=channel,
                )
            )
        except Exception as e:  # events are best effort; never kill the turn
            log.warning("publish agent.message failed: %s", e)

    # -- replies + follow-up -------------------------------------------------------------------------------------
    async def interpret_reply(
        self, message: InboundMessage, pending: list[ApprovalRequest], trip: Trip | None = None
    ) -> ReplyInterpretation:
        return logic.interpret_reply_rules(message, pending, trip)

    async def compose_followup(self, trip: Trip) -> OutboundMessage:
        return logic.compose_followup_rules(trip)
