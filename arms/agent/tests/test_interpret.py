"""AgentService.interpret_request: keyword rules (fake) + Claude structured output with a mocked client."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest

from arms.agent import create, create_fake
from arms.agent.interpret import Hints, combined_text, find_robot, find_store
from arms.agent.service import ClaudeAgent
from arms.agent.tests.stubs import StubCatalog, StubEvents
from contracts.services import Services
from contracts.shopping import Intent
from utils.config import reset_settings, set_settings

DEMO = "tacos for 6, under $40, one vegetarian"


@pytest.fixture(autouse=True)
def _no_llm_key():
    set_settings(anthropic_api_key="")
    yield
    reset_settings()


def run(coro):
    return asyncio.run(coro)


def services() -> Services:
    return Services(catalog=StubCatalog(), events=StubEvents())


def interpret(text: str, **kw: Any):
    agent = create_fake(services())
    return run(agent.interpret_request(text, "maya", **kw))


def test_taco_request_matches_expand_intent():
    agent = create_fake(services())
    draft = run(agent.interpret_request(DEMO, "maya", "store_ave"))
    sl = run(agent.expand_intent(Intent(intent_id="int_x", user_id="maya", text=DEMO)))
    assert draft.intent == "shop" and draft.clarifying_question is None
    assert draft.shopping_list is not None
    got = [(i.query, i.qty, i.sku, i.dietary) for i in draft.shopping_list.items]
    want = [(i.query, i.qty, i.sku, i.dietary) for i in sl.items]
    assert got == want
    assert draft.shopping_list.budget_cents == 4000
    assert draft.store_hint == "store_ave"
    assert "$40.00" in draft.summary and draft.reply


def test_go_charge_names_the_robot():
    draft = interpret("Hazel, go charge")
    assert draft.intent == "return_to_dock"
    assert draft.robot_hint == "rover-hill-1"
    assert draft.store_hint == "store_hill"
    assert "Hazel" in draft.reply


def test_shelf_scan_with_category_and_store():
    draft = interpret("scan dairy at Hill", role="staff")
    assert draft.intent == "shelf_scan"
    assert draft.categories == ["dairy"]
    assert draft.store_hint == "store_hill"
    assert draft.shopping_list is None


def test_whole_store_scan():
    draft = interpret("do a stock check", role="staff", store_id="store_ave")
    assert draft.intent == "shelf_scan" and draft.categories == [] and draft.store_hint == "store_ave"


def test_product_question_is_answered_from_the_catalog():
    draft = interpret("where is the salsa?", store_id="store_ave")
    assert draft.intent == "status"
    assert "Salsa" in draft.reply and "Aisle 3" in draft.reply


def test_status_and_chat():
    assert interpret("what is Pico doing?").intent == "status"
    assert interpret("what is Pico doing?").robot_hint == "rover-ave-1"
    assert interpret("thanks!").intent == "chat"


def test_vague_request_needs_input_and_history_resolves_it():
    draft = interpret("groceries")
    assert draft.clarifying_question and draft.shopping_list is None
    answered = interpret("milk, 2 eggs and bread", history=["groceries", draft.clarifying_question])
    assert answered.intent == "shop" and answered.clarifying_question is None
    assert [i.query for i in answered.shopping_list.items] == ["milk", "eggs", "bread"]


def test_fetch_is_staff_only():
    staff = interpret("fetch the salsa to the front", role="staff")
    assert staff.intent == "fetch" and len(staff.shopping_list.items) == 1
    shopper = interpret("bring me salsa")
    assert shopper.intent == "shop"


def test_name_helpers():
    hints = Hints()
    assert find_robot("Rhea please scan aisle 3", hints) == "rover-ave-2"
    assert find_robot("hello there", hints) is None
    assert find_store("at the Ave", hints) == "store_ave"
    assert combined_text("2 limes", ["tacos", "how many?"]) == "tacos, 2 limes"
    assert combined_text("milk", ["groceries", "what?"]) == "milk"


# --- Claude path ---------------------------------------------------------------------------------------------------
class JsonClient:
    def __init__(self, payload: dict[str, Any] | Exception) -> None:
        self.payload = payload
        self.calls = 0
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kw: Any) -> Any:
        self.calls += 1
        if isinstance(self.payload, Exception):
            raise self.payload
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(self.payload))], stop_reason="end_turn"
        )


def test_claude_interpret_request_code_decides_names_and_money():
    client = JsonClient(
        {
            "intent": "shop",
            "summary": "Taco night",
            "items": [{"query": "corn tortillas", "qty": 2}, {"query": "salsa", "qty": 1}],
            "robot_name": "Zorg",  # unknown: dropped
            "store_name": "Picky Ave",
            "reply": "On it!",
            "confidence": 0.9,
        }
    )
    agent = ClaudeAgent(services(), client=client)
    draft = run(agent.interpret_request("tacos under $25 at the ave", "maya"))
    assert client.calls == 1
    assert draft.intent == "shop" and draft.robot_hint is None and draft.store_hint == "store_ave"
    assert draft.shopping_list.budget_cents == 2500  # regex, not the model
    assert [i.query for i in draft.shopping_list.items] == ["corn tortillas", "salsa"]
    assert draft.shopping_list.items[0].sku == "TORT-01"


def test_claude_interpret_request_shopper_fetch_becomes_shop_and_unknown_category_dropped():
    client = JsonClient(
        {"intent": "fetch", "summary": "Get salsa", "items": [{"query": "salsa"}], "categories": ["weird"]}
    )
    draft = run(ClaudeAgent(services(), client=client).interpret_request("bring me salsa", "maya"))
    assert draft.intent == "shop" and draft.categories == []


def test_claude_interpret_request_falls_back_to_rules():
    svc = services()
    client = JsonClient(RuntimeError("overloaded"))
    draft = run(ClaudeAgent(svc, client=client).interpret_request("Hazel, go charge", "maya"))
    assert draft.intent == "return_to_dock" and draft.robot_hint == "rover-hill-1"
    assert any(e.type == "error" and e.code == "llm_fallback" for e in svc.events.events)


def test_real_agent_without_key_uses_rules():
    draft = run(create(services()).interpret_request(DEMO, "maya"))
    assert draft.intent == "shop" and len(draft.shopping_list.items) >= 8
