"""Keyword rules, store choice (pure) and the real fleet's agent-first interpretation."""

from __future__ import annotations

from arms.fleet import rules
from arms.fleet.service import RealFleet
from arms.fleet.stores import ItemOffer, StoreEval, choose_stores, sub_list
from arms.fleet.tests.stubs import SPECS
from arms.fleet.tests.test_fleet import TACO, World, until
from contracts.fleet import FleetRequest, GoalStatus, RequestDraft
from contracts.shopping import ListItem, ShoppingList


def ctx() -> rules.RuleContext:
    return rules.RuleContext(
        robots={"rover-ave-1": "Pico", "rover-ave-2": "Rhea", "rover-hill-1": "Hazel"},
        robot_stores={"rover-ave-1": "store_ave", "rover-ave-2": "store_ave", "rover-hill-1": "store_hill"},
        stores={
            "store_ave": rules.store_keywords("store_ave", "Picky Market - The Ave"),
            "store_hill": rules.store_keywords("store_hill", "Picky Express - Capitol Hill"),
        },
        categories={
            "dairy": rules.category_keywords("dairy", "Dairy & Eggs", ["milk"]),
            "snacks": rules.category_keywords("snacks", "Snacks"),
        },
        aisles={"store_ave": {"ave-a3": "Aisle 3"}},
    )


# ---------------------------------------------------------------------------------------------------------------------
# rules
# ---------------------------------------------------------------------------------------------------------------------
def test_store_keywords():
    assert "hill" in rules.store_keywords("store_hill", "Picky Express - Capitol Hill")
    assert "capitol hill" in rules.store_keywords("store_hill", "Picky Express - Capitol Hill")
    assert "ave" in rules.store_keywords("store_ave", "Picky Market - The Ave")
    c = ctx()
    assert rules.find_store("i have no idea", c) is None  # 'ave' inside 'have' does not count
    assert rules.find_store("tacos at Capitol Hill", c) == "store_hill"


def test_interpret_intents():
    c = ctx()
    d = rules.interpret("Hazel, go charge", role="staff", ctx=c)
    assert d.intent == "return_to_dock" and d.robot_hint == "rover-hill-1" and d.store_hint == "store_hill"
    d = rules.interpret("scan the dairy aisle at Hill", role="staff", ctx=c)
    assert d.intent == "shelf_scan" and d.categories == ["dairy"] and d.store_hint == "store_hill"
    d = rules.interpret("audit aisle 3 at the ave", role="staff", ctx=c)
    assert d.intent == "shelf_scan" and d.aisle_ids == ["ave-a3"]
    d = rules.interpret("Rhea, bring 2 cartons of oat milk to checkout", role="staff", ctx=c)
    assert d.intent == "fetch" and d.robot_hint == "rover-ave-2"
    assert rules.fetch_query("Rhea, bring 2 cartons of oat milk to checkout", c) == ("cartons of oat milk", 2)
    # a shopper saying "grab me..." is shopping, not a staff fetch
    assert rules.interpret("grab me tortillas and salsa", role="shopper", ctx=c).intent == "shop"
    assert rules.interpret("where is my order?", ctx=c).intent == "status"
    assert rules.interpret("hello!", ctx=c).intent == "chat"
    d = rules.interpret("tacos for 6, under $40, one vegetarian", ctx=c)
    assert d.intent == "shop" and d.clarifying_question is None and d.store_hint is None
    assert rules.interpret("get me some stuff", ctx=c).clarifying_question
    assert rules.interpret("go charge", role="staff", ctx=c).clarifying_question


# ---------------------------------------------------------------------------------------------------------------------
# store choice
# ---------------------------------------------------------------------------------------------------------------------
def _ev(store: str, offers: dict[str, int], robots: int = 1) -> StoreEval:
    return StoreEval(
        store_id=store,
        name=store,
        robots=robots,
        offers={i: ItemOffer(sku=i.upper(), line_cents=c, full_qty=True) for i, c in offers.items()},
    )


ITEMS = [ListItem(item_id=i, query=i, sku=i.upper()) for i in ("a", "b", "c", "d")]


def test_coverage_then_price_then_home_store():
    evals = {"s1": _ev("s1", {"a": 100, "b": 100, "c": 100}), "s2": _ev("s2", {"a": 95, "b": 95, "c": 95})}
    assert choose_stores(ITEMS, evals, {}).primary == "s2"  # same coverage, cheaper
    assert choose_stores(ITEMS, evals, {}, home="s1").primary == "s1"  # home store within 10%
    evals["s2"] = _ev("s2", {"a": 50, "b": 50, "c": 50})
    assert choose_stores(ITEMS, evals, {}, home="s1").primary == "s2"  # home store far pricier
    evals["s1"] = _ev("s1", {"a": 100, "b": 100, "c": 100, "d": 100})
    assert choose_stores(ITEMS, evals, {}).primary == "s1"  # coverage first
    evals["s1"].robots = 0
    assert choose_stores(ITEMS, evals, {}).primary == "s2"  # a store with no robot loses


def test_split_rules():
    evals = {"s1": _ev("s1", {"a": 100, "b": 100}), "s2": _ev("s2", {"c": 100})}
    plan = choose_stores(ITEMS, evals, {"c": True, "d": True})
    assert plan.chosen == ["s1"]  # one missing item that is sold online: no split
    assert plan.assignment["s1"] == ["a", "b", "c", "d"]
    plan = choose_stores(ITEMS, evals, {"c": False})
    assert plan.chosen == ["s1", "s2"] and plan.assignment == {"s1": ["a", "b", "d"], "s2": ["c"]}
    evals["s2"] = _ev("s2", {"c": 100, "d": 100})
    plan = choose_stores(ITEMS, evals, {"c": True, "d": True})
    assert plan.chosen == ["s1", "s2"]  # two missing items: split even though online has them
    assert choose_stores(ITEMS, evals, {}, hint="s1").chosen == ["s1"]  # a store hint never splits
    sl = ShoppingList(list_id="L", user_id="u", title="T", budget_cents=1000, items=ITEMS)
    sub = sub_list(sl, "s2", ["c", "d"], evals, primary=False, split=True, budget_cents=400, store_name="Hill")
    assert sub.list_id == "L-s2" and [i.item_id for i in sub.items] == ["c", "d"] and sub.budget_cents == 400
    same = sub_list(sl, "s1", ["a", "b", "c", "d"], evals, primary=True, split=False, budget_cents=1000)
    assert same == sl


# ---------------------------------------------------------------------------------------------------------------------
# real fleet: agent.interpret_request first, rules on failure
# ---------------------------------------------------------------------------------------------------------------------
class InterpretingAgent:
    def __init__(self, inner, draft: RequestDraft | None = None, fail: bool = False) -> None:
        self.inner = inner
        self.draft = draft
        self.fail = fail
        self.calls: list[tuple[str, list[str] | None]] = []

    async def interpret_request(self, text, user_id, store_id=None, *, role="shopper", profile=None, history=None):
        self.calls.append((text, history))
        if self.fail:
            raise RuntimeError("LLM overloaded")
        return self.draft

    async def expand_intent(self, intent, profile=None):
        return await self.inner.expand_intent(intent, profile)


async def test_real_fleet_uses_agent_and_cleans_hints():
    w = World(auto=False)
    draft = RequestDraft(intent="shelf_scan", summary="scan dairy", categories=["dairy"], robot_hint="robot-x")
    w.services.agent = InterpretingAgent(w.agent, draft)  # type: ignore[assignment]
    fleet = RealFleet(w.services, robots=SPECS, robot_poll_s=0.01, robot_wait_s=0.2)
    w.services.fleet = fleet  # type: ignore[assignment]
    w.brain.behavior["rover-hill-1"] = "block"
    plan = await fleet.submit_request(FleetRequest(text="Hazel please scan dairy", user_id="sam", role="staff"))
    assert plan.interpretation.robot_hint == "rover-hill-1"  # unknown hint replaced by the rules' one
    assert plan.goals[0].store_id == "store_hill" and plan.goals[0].robot_id == "rover-hill-1"
    await fleet.shutdown()


async def test_real_fleet_falls_back_to_rules():
    w = World()
    w.services.agent = InterpretingAgent(w.agent, fail=True)  # type: ignore[assignment]
    fleet = RealFleet(w.services, robots=SPECS, robot_poll_s=0.01, robot_wait_s=0.2)
    w.services.fleet = fleet  # type: ignore[assignment]
    plan = await fleet.submit_request(FleetRequest(text="tacos for 6", user_id="maya"))
    assert plan.status == "executing" and plan.interpretation.shopping_list is not None
    assert [i.query for i in plan.interpretation.shopping_list.items] == [q for q, _, _ in TACO]
    assert any(e.type == "error" and e.code == "interpret_fallback" for e in w.events.events)
    await until(lambda: plan.trip_ids[0] in w.trips.results)
    assert w.trips.results[plan.trip_ids[0]].status == GoalStatus.COMPLETED
    await fleet.shutdown()
