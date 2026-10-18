"""Agent arm: fake conformance + demo-scenario behavior (base deps only, no network)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from arms.agent import create, create_fake, logic
from arms.agent.service import ClaudeAgent, ToolExecutor
from arms.agent.tests.stubs import StubCatalog, StubEvents, StubTrips, make_trip
from contracts.common import utcnow
from contracts.messaging import InboundMessage, OutboundMessage
from contracts.profile import UserProfile
from contracts.services import AgentService, Services
from contracts.shopping import CartLine, Intent, TripState
from contracts.tools import TOOL_NAMES
from contracts.trust import ApprovalKind, ApprovalOption, ApprovalRequest
from utils.config import reset_settings, set_settings

DEMO = "tacos for 6, under $40, one vegetarian"


@pytest.fixture(autouse=True)
def _no_llm_key():
    set_settings(anthropic_api_key="")
    yield
    reset_settings()


def services(**kw: Any) -> Services:
    return Services(catalog=StubCatalog(), events=StubEvents(), **kw)


def intent(text: str = DEMO) -> Intent:
    return Intent(intent_id="int_demo", user_id="maya", text=text, trip_id="trip_1")


def msg(text: str) -> InboundMessage:
    return InboundMessage(message_id="m1", channel="photon", text=text, user_id="maya")


def approval(aid: str, kind: ApprovalKind = ApprovalKind.RECOMMENDATION, options=(), multi=False, age_s=0):
    return ApprovalRequest(
        approval_id=aid,
        trip_id="trip_1",
        user_id="maya",
        kind=kind,
        title="Add it?",
        options=[ApprovalOption(option_id=o, label=f"opt {o}") for o in options],
        multi=multi,
        created_at=utcnow() - timedelta(seconds=age_s),
    )


# --- conformance -------------------------------------------------------------------------------------------------
def test_factories_satisfy_protocol_without_calling_services():
    empty = Services()  # nothing wired: construction must not touch other services
    assert isinstance(create_fake(empty), AgentService)
    assert isinstance(create(empty), AgentService)


def test_tool_handlers_cover_every_tool():
    ex = ToolExecutor(Services(), make_trip())
    assert tuple(ex.handlers()) == TOOL_NAMES


# --- intent parsing ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "servings", "budget", "veg"),
    [
        (DEMO, 6, 4000, 1),
        ("Tacos for six under 40 dollars, 2 vegetarians", 6, 4000, 2),
        ("taco night for 4, budget of $25.50", 4, 2550, 0),
    ],
)
def test_parse_intent_text(text, servings, budget, veg):
    p = logic.parse_intent_text(text)
    assert (p.dish, p.servings, p.budget_cents, p.vegetarian_count) == ("tacos", servings, budget, veg)


def test_free_form_list():
    p = logic.parse_intent_text("milk, 2 eggs and bread under $20")
    assert p.dish is None and p.budget_cents == 2000
    assert [q for q, _ in p.free_items] == ["milk", "eggs", "bread"]
    assert dict(p.free_items)["eggs"] == 2


async def test_expand_intent_demo():
    agent = create_fake(services())
    sl = await agent.expand_intent(intent())
    assert sl.title == "Tacos for 6" and sl.servings == 6 and sl.budget_cents == 4000
    assert sl.trip_id == "trip_1" and sl.intent_id == "int_demo"
    assert sl.dietary_notes == ["1 vegetarian guest"]
    queries = [i.query for i in sl.items]
    for q in ("tortillas", "ground beef", "black beans", "salsa", "avocado", "limes", "cilantro"):
        assert q in queries
    beans = next(i for i in sl.items if i.query == "black beans")
    assert beans.dietary == ["vegetarian"] and beans.sku == "BEAN-01"
    assert all(i.sourcing == "unresolved" for i in sl.items)
    assert all(i.sku for i in sl.items)  # every demo item resolved to a guess
    # deterministic
    assert (await agent.expand_intent(intent())).model_dump(exclude={"items"}) == sl.model_dump(exclude={"items"})


async def test_all_vegetarian_drops_meat():
    sl = await create_fake(services()).expand_intent(intent("vegetarian tacos for 4"))
    assert "ground beef" not in [i.query for i in sl.items]


async def test_expand_without_catalog_degrades():
    sl = await create_fake(Services()).expand_intent(intent())
    assert sl.items and all(i.sku is None for i in sl.items)


async def test_profile_makes_trip_two_smarter():
    agent = create_fake(services())
    before = await agent.expand_intent(intent())
    spicy = UserProfile(user_id="maya", display_name="Maya", prefs={"spicy": 0.8})
    after = await agent.expand_intent(intent(), spicy)
    salsa = lambda sl: next(i for i in sl.items if i.query == "salsa")  # noqa: E731
    assert salsa(before).sku == "SALSA-MILD"
    assert salsa(after).sku == "SALSA-HOT"
    assert "SALSA-MILD" in salsa(after).substitutes


async def test_profile_budget_default_and_disliked_sku():
    prof = UserProfile(user_id="maya", display_name="Maya", default_budget_cents=5000, disliked_skus=["SALSA-MILD"])
    sl = await create_fake(services()).expand_intent(intent("tacos for 2"), prof)
    assert sl.budget_cents == 5000
    assert next(i for i in sl.items if i.query == "salsa").sku == "SALSA-HOT"


# --- sourcing ----------------------------------------------------------------------------------------------------
async def test_split_sourcing():
    agent = create_fake(services())
    sl = await agent.source_list(await agent.expand_intent(intent()), "store_ave")
    assert sl.store_id == "store_ave"
    cil = next(i for i in sl.items if i.query == "cilantro")
    assert cil.sourcing == "online" and cil.store_id == "online" and "OOS at store_ave" in cil.notes
    others = [i for i in sl.items if i.query != "cilantro"]
    assert all(i.sourcing == "in_store" and i.store_id == "store_ave" for i in others)
    tort = next(i for i in sl.items if i.query == "tortillas")
    assert tort.qty == 2 and tort.est_line_cents == 2 * 299
    assert sl.est_total_cents == sum(i.est_line_cents or 0 for i in sl.items)


async def test_sourcing_unresolved():
    agent = create_fake(services())
    sl = await agent.expand_intent(intent("unicorn steaks"))
    out = await agent.source_list(sl, "store_ave")
    assert out.items[0].sourcing == "unresolved" and "not found" in out.items[0].notes


# --- reply interpretation ----------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "decision"),
    [
        ("yes", "approve"),
        ("Y", "approve"),
        ("👍", "approve"),
        ("sure!", "approve"),
        ("no", "reject"),
        ("n", "reject"),
        ("nah skip it", "reject"),
        ("👎", "reject"),
    ],
)
async def test_yes_no(text, decision):
    agent = create_fake(Services())
    out = await agent.interpret_reply(msg(text), [approval("a_old", age_s=30), approval("a_new")])
    assert out.kind == "approval" and out.decision == decision and out.approval_id == "a_new"


async def test_pick_choice_and_multi():
    agent = create_fake(Services())
    pick = approval("pick", ApprovalKind.PICK_CHOICE, options=("1", "2", "3"))
    out = await agent.interpret_reply(msg("2"), [pick])
    assert out.kind == "approval" and out.choice == "2" and out.approval_id == "pick"
    batch = approval("batch", ApprovalKind.RECOMMENDATION, options=("r1", "r2", "r3"), multi=True)
    out = await agent.interpret_reply(msg("1 and 3"), [batch])
    assert out.choices == ["r1", "r3"]
    out = await agent.interpret_reply(msg("none"), [batch])
    assert out.decision == "reject"


def _paid_trip() -> Any:
    line = CartLine(line_id="l1", sku="SALSA-MILD", name="Casa Salsa Mild", unit_price_cents=349, store_id="store_ave")
    other = CartLine(line_id="l2", sku="BEEF-01", name="Ground Beef 1 lb", unit_price_cents=699, store_id="store_ave")
    trip = make_trip(state=TripState.PAID)
    return trip.model_copy(update={"cart": trip.cart.model_copy(update={"lines": [other, line]})})


async def test_followup_and_feedback():
    agent = create_fake(Services())
    trip = _paid_trip()
    out = await agent.compose_followup(trip)
    assert isinstance(out, OutboundMessage)
    assert out.about_skus == ["SALSA-MILD"] and "salsa" in out.text.lower() and out.trip_id == "trip_1"
    trip = trip.model_copy(update={"followup": out, "state": TripState.FOLLOWUP})
    fb = await agent.interpret_reply(msg("4/5"), [], trip)
    assert fb.kind == "feedback" and fb.rating == 4 and fb.sku == "SALSA-MILD"
    fb = await agent.interpret_reply(msg("it was too mild, want more heat"), [], trip)
    assert fb.kind == "feedback" and "want_spicier" in fb.feedback_tags and fb.sku == "SALSA-MILD"
    fb = await agent.interpret_reply(msg("the beef was great"), [], trip)
    assert fb.sku == "BEEF-01" and fb.rating == 4


async def test_intent_and_chat():
    agent = create_fake(Services())
    assert (await agent.interpret_reply(msg(DEMO), [])).kind == "intent"
    assert (await agent.interpret_reply(msg("thanks picky"), [])).kind == "chat"


# --- respond -----------------------------------------------------------------------------------------------------
async def test_respond_canned_and_events():
    svc = services()
    agent = create_fake(svc)
    trip = make_trip()
    trip = trip.model_copy(update={"shopping_list": (await agent.expand_intent(intent()))})
    r = await agent.respond(trip, "how much have I spent?")
    assert "$40.00" in r.text
    r = await agent.respond(trip, "where is the salsa?")
    assert "Aisle 3" in r.text and r.tool_calls[0].name == "query_inventory"
    r = await agent.respond(trip, "where are the cilantro")
    assert "online" in r.text
    types = [(e.type, e.role) for e in svc.events.events]
    assert ("agent.message", "user") in types and ("agent.message", "assistant") in types


async def test_respond_checkout_goes_through_trips():
    trips = StubTrips(make_trip())
    agent = create_fake(services(trips=trips))
    r = await agent.respond(trips.trip, "ok let's check out")
    assert trips.calls and trips.calls[0][0] == "start_checkout"
    assert r.tool_calls[0].name == "checkout" and "approve" in r.text


# --- real agent (scripted client, no network) --------------------------------------------------------------------
class ScriptedClient:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.messages = self

    async def create(self, **kwargs: Any) -> Any:
        self.requests.append(kwargs)
        return self.responses.pop(0)


def _tool_use(name: str, inp: dict[str, Any]) -> Any:
    block = SimpleNamespace(type="tool_use", id="tu_1", name=name, input=inp)
    return SimpleNamespace(content=[block], stop_reason="tool_use")


def _text(t: str) -> Any:
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=t)], stop_reason="end_turn")


async def test_real_agent_tool_loop_add_to_cart():
    trips = StubTrips(make_trip())
    svc = services(trips=trips)
    client = ScriptedClient([_tool_use("add_to_cart", {"sku": "SALSA-HOT", "qty": 1}), _text("Added the salsa.")])
    agent = ClaudeAgent(svc, client=client)
    r = await agent.respond(trips.trip, "add the spicy salsa")
    assert r.text == "Added the salsa." and r.tool_calls[0].name == "add_to_cart" and r.tool_calls[0].ok
    line = trips.calls[0][1]
    assert line.sku == "SALSA-HOT" and line.unit_price_cents == 379 and line.category == "pantry"
    req = client.requests[0]
    assert req["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert req["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert [t["name"] for t in req["tools"]] == list(TOOL_NAMES)


async def test_real_agent_falls_back_without_key():
    agent = create(services())
    sl = await agent.expand_intent(intent())
    assert sl.budget_cents == 4000 and sl.title == "Tacos for 6"
    r = await agent.respond(make_trip(), "hi")
    assert "Picky" in r.text
