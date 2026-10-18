"""ScriptedPlanner: the deterministic ``PlannerService`` (scripted draft + THE deterministic compiler).

``create_planner_fake`` returns it; the real planner (``planner.LlmPlanner``) subclasses it and overrides ``draft()``
only, so plan / compile / replan / expand_tool_call are the same code either way ("the LLM proposes, code decides").

NL path (no Goal; dev route POST /api/dev/plans): keyword rules mirror the v0.2 fake agent (``logic.py`` RECIPES):
'tacos for 6, under $40, one vegetarian' -> tortillas 2, ground beef 2, black beans 1, shredded cheese 1, salsa 1,
avocado 3, limes 2, onion 1, cilantro 1, sour cream 1 (the same list as v0.2 expand_intent); anything else -> comma /
'and' separated items (the generic catalog-driven fallback). Items are resolved through the v0.2 CatalogService
(``get_product`` / ``search`` / ``get_locations``) in the robot's store; what is not on its shelves is listed with
status 'online' and gets no steps. Order: greedy nearest neighbour from the robot's pose.

Goal path (the pipeline: the fleet's Goal, items already sourced): the goal's open items in goal order (= the trip
route for shop goals); items without a SKU / slot are resolved the same way.

``compile`` is THE deterministic compiler a real LLM planner also uses: ``normalize_draft`` first, then poses + lift
heights from the planogram, TargetSpec from the product, default_conditions, units = qty - picked_qty, optional ->
ask first, produce -> pick_choice after the scan, audit + checkout handoff when end_at='checkout'.
Ported from the API draft (``fakes/fake_planner.py``).
"""

import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from contracts.catalog import Product, SearchQuery
from contracts.common import ONLINE_STORE_ID, Pose2D
from contracts.evidence import fingerprint
from contracts.fleet import GoalItem
from contracts.grounding import SlotLocation, TargetSpec
from contracts.plan import (
    CLOSED_STEP_STATUSES,
    OPEN_ITEM_STATUSES,
    AlignStep,
    AskUserStep,
    DraftStep,
    GraspStep,
    GroundTargetStep,
    HandoffStep,
    NavigateToStep,
    PlaceStep,
    PlanDraft,
    PlanIssue,
    PlannerInfo,
    PlanRequest,
    PlanStatus,
    PlanStep,
    ScanShelfStep,
    StepPlan,
    VerifyStep,
    binding_key,
    default_conditions,
    goal_attempt,
    lift_height_for,
    make_plan_id,
    normalize_draft,
    plan_errors,
    validate_plan,
)
from contracts.planogram import Planogram
from contracts.policy import ReplanRequest
from contracts.tools import parse_brain_tool_input

_NUM = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "a": 1,
    "an": 1,
}
_NUM_RE = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"


def _per(k: int) -> Callable[[int, int, int], int]:
    return lambda n, m, v: max(1, math.ceil(n / k))


TACO_RECIPE: list[tuple[str, str, Callable[[int, int, int], int], str]] = [
    ("tortillas", "bakery", lambda n, m, v: max(1, math.ceil(n * 3 / 12)), "TORT-CORN"),
    ("ground beef", "meat", lambda n, m, v: math.ceil(m / 4) if m else 0, "BEEF-GROUND"),
    ("black beans", "pantry", lambda n, m, v: max(1, math.ceil(v / 2)) if v else 0, "BEANS-BLACK"),
    ("shredded cheese", "dairy", _per(6), "CHEESE-MEX"),
    ("salsa", "pantry", _per(8), "SALSA-MILD"),
    ("avocado", "produce", _per(2), "AVOCADO"),
    ("limes", "produce", _per(3), "LIMES"),
    ("onion", "produce", _per(8), "ONION-WHITE"),
    ("cilantro", "produce", _per(10), "CILANTRO"),
    ("sour cream", "dairy", _per(8), "SOUR-CREAM"),
]
"""(query, category, qty(n servings, m meat eaters, v vegetarians), SKU hint): the v0.2 taco recipe."""


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUM.get(tok.lower(), 1)


def parse_budget_cents(text: str) -> int | None:
    m = re.search(
        r"(?:under|below|less than|max(?:imum)?|budget(?: of)?|up to)\s*\$\s*(\d+)(?:\.(\d{1,2}))?", text, re.I
    )
    m = m or re.search(r"\$\s*(\d+)(?:\.(\d{1,2}))?", text)
    if not m:
        return None
    cents = (m.group(2) or "0").ljust(2, "0")
    return int(m.group(1)) * 100 + int(cents)


def interpret_text(text: str) -> tuple[list[tuple[str, int, str | None, str | None]], int | None, str]:
    """NL -> ([(query, qty, category, sku_hint)], budget_cents, summary). Deterministic keyword rules."""
    low = text.lower()
    budget = parse_budget_cents(text)
    if re.search(r"\btacos?\b", low):
        m = re.search(rf"\bfor\s+{_NUM_RE}\b", low) or re.search(rf"\b{_NUM_RE}\s+(?:people|guests|servings)\b", low)
        n = _num(m.group(1)) if m else 4
        vm = re.search(rf"\b{_NUM_RE}\s+(?:of them\s+)?vegetarians?\b", low)
        v = min(_num(vm.group(1)), n) if vm else (n if "vegetarian" in low else 0)
        items = [(q, qty, cat, sku) for q, cat, f, sku in TACO_RECIPE if (qty := f(n, n - v, v)) > 0]
        veg = f", {v} vegetarian" if v else ""
        return items, budget, f"Tacos for {n}{veg}" + (f", under ${budget // 100}" if budget else "")
    cleaned = re.sub(r"(?:under|below|less than|budget(?: of)?|up to)?\s*\$\s*\d+(?:\.\d{1,2})?", " ", text, flags=re.I)
    cleaned = re.sub(r"\b(?:i need|i want|get me|please|grab|buy|pick up|some)\b", " ", cleaned, flags=re.I)
    out: list[tuple[str, int, str | None, str | None]] = []
    for frag in re.split(r",|;|\band\b|&|\+", cleaned, flags=re.I):
        frag = re.sub(r"[^\w\s'-]", " ", frag).strip().lower()
        if not frag:
            continue
        qty = 1
        qm = re.match(rf"^{_NUM_RE}\s+(.+)$", frag)
        if qm:
            qty, frag = _num(qm.group(1)), qm.group(2).strip()
        if len(frag) > 1:
            out.append((frag, max(1, qty), None, None))
    return out, budget, ", ".join(q for q, *_ in out) or text.strip()


class ScriptedPlanner:
    """Deterministic PlannerService (scripted draft). Subclass and override ``draft()`` for an LLM planner."""

    def __init__(self, services: Any) -> None:
        self.s = services
        self._plans: dict[str, Planogram] = {}
        self._products: dict[str, Product | None] = {}

    # ---- lookups (v0.2 CatalogService only) --------------------------------------------------------------------------
    async def _planogram(self, store_id: str) -> Planogram:
        if store_id not in self._plans:
            self._plans[store_id] = await self.s.require("catalog").get_planogram(store_id)
        return self._plans[store_id]

    async def _product(self, sku: str) -> Product | None:
        if sku not in self._products:
            self._products[sku] = await self.s.require("catalog").get_product(sku)
        return self._products[sku]

    async def _location(self, store_id: str, slot_id: str) -> SlotLocation | None:
        plan = await self._planogram(store_id)
        slot = plan.slot(slot_id)
        if slot is None:
            return None
        aisle = plan.aisle(slot.aisle_id or "")
        return SlotLocation(
            slot_id=slot_id,
            aisle_label=aisle.label if aisle else None,
            level=slot.level,
            approach_pose=slot.approach_pose,
            item_position=slot.item_position,
            lift_height_m=lift_height_for(slot.item_position.z),
        )

    async def _resolve(
        self, store_id: str, n: int, query: str, qty: int, category: str | None, sku_hint: str | None
    ) -> GoalItem:
        catalog = self.s.require("catalog")
        sku = sku_hint if sku_hint and await self._product(sku_hint) is not None else None
        if sku is None:
            hits = await catalog.search(SearchQuery(text=query, store_ids=[store_id, ONLINE_STORE_ID], limit=5))
            stocked = [h for h in hits if (o := h.offer_for(store_id)) is not None and o.in_stock]
            hit = stocked[0] if stocked else (hits[0] if hits else None)
            sku = hit.product.sku if hit else None
        item_id = f"item_{n:02d}"
        if sku is None:
            return GoalItem(
                item_id=item_id,
                query=query,
                qty=qty,
                category=category,
                status="unavailable",
                notes="not found in the catalog",
            )
        product = await self._product(sku)
        locs = [loc for loc in await catalog.get_locations(sku, store_id) if loc.store_id == store_id]
        loc = next((x for x in locs if x.qty >= 1), None)
        offers = await catalog.get_offers(sku, [store_id, ONLINE_STORE_ID])
        offer = next((o for o in offers if o.store_id == store_id), None) or next(iter(offers), None)
        return GoalItem(
            item_id=item_id,
            query=query,
            sku=sku,
            name=product.name if product else sku,
            qty=qty,
            slot_id=loc.slot_id if loc else None,
            category=product.category if product else category,
            price_cents=offer.price_cents if offer else (product.price_cents if product else None),
            status="located" if loc else "online",
            notes="" if loc else f"not on the shelf at {store_id}: order online",
        )

    async def _start_pose(self, robot_id: str, store_id: str) -> Pose2D:
        robot = getattr(self.s, "robot", None)
        if robot is not None:
            try:
                return (await robot.state(robot_id)).pose
            except Exception:  # noqa: BLE001 - ordering falls back to the store entrance
                pass
        return (await self._planogram(store_id)).start_pose

    async def _order(self, items: list[GoalItem], store_id: str, start: Pose2D) -> list[GoalItem]:
        """Greedy nearest neighbour over approach poses (deterministic; ties by item_id)."""
        plan = await self._planogram(store_id)
        todo = [i for i in items if i.slot_id and plan.slot(i.slot_id)]
        rest = [i for i in items if i not in todo]
        out: list[GoalItem] = []
        pose = start
        while todo:
            nxt = min(
                todo, key=lambda i: (round(pose.distance_to(plan.slot(i.slot_id or "").approach_pose), 6), i.item_id)
            )  # type: ignore[union-attr]
            todo.remove(nxt)
            out.append(nxt)
            pose = plan.slot(nxt.slot_id or "").approach_pose  # type: ignore[union-attr]
        return out + rest

    # ---- PlannerService ----------------------------------------------------------------------------------------------
    async def plan(self, request: PlanRequest) -> StepPlan:
        store_id = request.store_id or ""
        if request.goal is not None:
            goal = request.goal
            items = []
            for n, it in enumerate(goal.items, start=1):
                if it.status in OPEN_ITEM_STATUSES and (it.slot_id is None or it.sku is None):
                    got = await self._resolve(store_id, n, it.query, it.qty, it.category, it.sku)
                    it = it.model_copy(
                        update={
                            "sku": got.sku,
                            "slot_id": got.slot_id,
                            "name": it.name or got.name,
                            "price_cents": it.price_cents or got.price_cents,
                            "category": it.category or got.category,
                            "status": got.status,
                        }
                    )
                items.append(it)
            summary = goal.title
            budget = request.budget_cents
        else:
            parsed, budget, summary = interpret_text(request.text or "")
            budget = request.budget_cents or budget
            items = [
                await self._resolve(store_id, n, q, qty, cat, sku)
                for n, (q, qty, cat, sku) in enumerate(parsed, start=1)
            ]
            items = await self._order(items, store_id, await self._start_pose(request.robot_id, store_id))
        draft, info = await self.draft(request, items, summary)
        try:
            return await self.compile(draft, request, items=items, budget_cents=budget, info=info)
        except ValueError as exc:
            if info.origin == "scripted":
                raise
            end_at = request.end_at or (request.goal.end_at if request.goal else "checkout")
            fallback = PlannerInfo(
                origin="scripted",
                mode="scripted",
                model=info.model,
                latency_ms=info.latency_ms,
                fallback_reason=f"LLM draft did not compile: {exc}"[:300],
            )
            scripted = self.scripted_draft(items, summary, end_at=end_at)
            return await self.compile(scripted, request, items=items, budget_cents=budget, info=fallback)

    async def draft(
        self, request: PlanRequest, items: Sequence[GoalItem], summary: str
    ) -> tuple[PlanDraft, PlannerInfo]:
        """THE seam for the real LLM planner (``arms/agent/planner.py`` LlmPlanner overrides only this): propose
        WHAT to do, in which order, for these RESOLVED items. Must not raise: on any LLM problem return the scripted
        draft with ``PlannerInfo(origin='scripted', fallback_reason=...)``. ``compile`` then repairs the draft
        (``normalize_draft``) and decides everything else."""
        end_at = request.end_at or (request.goal.end_at if request.goal else "checkout")
        return self.scripted_draft(items, summary, end_at=end_at), PlannerInfo(origin="scripted", mode="scripted")

    @staticmethod
    def scripted_draft(items: Sequence[GoalItem], summary: str, *, end_at: str = "checkout") -> PlanDraft:
        steps: list[DraftStep] = []
        for it in items:
            if it.status not in OPEN_ITEM_STATUSES or not it.slot_id:
                continue
            if it.optional:
                steps.append(
                    DraftStep(
                        kind="ask_user", item_id=it.item_id, question=f"Want {it.name or it.query}? It's on my way."
                    )
                )
            steps.append(DraftStep(kind="fetch_item", item_id=it.item_id))
        if end_at == "checkout":
            steps.append(DraftStep(kind="checkout"))
        if not steps:
            steps.append(DraftStep(kind="navigate_to", target="dock" if end_at == "dock" else "checkout"))
        return PlanDraft(summary=summary, steps=steps, rationale="scripted: route order, one fetch per item")

    async def compile(
        self,
        draft: PlanDraft,
        request: PlanRequest,
        *,
        items: Sequence[GoalItem],
        plan_id: str | None = None,
        budget_cents: int | None = None,
        revision: int = 1,
        root_plan_id: str | None = None,
        carried: Sequence[PlanStep] = (),
        id_prefix: str = "s",
        info: PlannerInfo | None = None,
    ) -> StepPlan:
        goal = request.goal
        store_id = request.store_id or ""
        end_at = request.end_at or (goal.end_at if goal else "checkout")
        draft, draft_fixes = normalize_draft(
            draft, items, end_at=end_at, lock_order=goal is not None and any(i.stop_id for i in items)
        )
        goal_id = goal.goal_id if goal else f"adhoc-{fingerprint([request.text, request.robot_id, store_id])[:8]}"
        root = root_plan_id or make_plan_id(goal_id, goal_attempt(goal))
        pid = plan_id or (root if revision == 1 else f"{root}-r{revision}")
        planogram = await self._planogram(store_id)
        known = {i.item_id: i for i in items}
        fixes: list[str] = [*(info.fixes if info else []), *draft_fixes]
        steps: list[PlanStep] = list(carried)
        counter = [0]
        asked: set[str] = {
            s.item_id for s in carried if isinstance(s, AskUserStep) and s.item_id and s.ask_kind == "recommendation"
        }

        def sid() -> str:
            counter[0] += 1
            return f"{id_prefix}{counter[0]:02d}"

        async def fetch(it: GoalItem) -> None:
            if it.status not in OPEN_ITEM_STATUSES or not it.slot_id or not it.sku:
                fixes.append(f"no steps for {it.item_id} ({it.status})")
                return
            loc = await self._location(store_id, it.slot_id)
            if loc is None:
                fixes.append(f"unknown slot {it.slot_id} for {it.item_id}")
                return
            p = await self._product(it.sku)
            name = it.name or (p.name if p else it.sku)
            group = {"item_id": it.item_id, "group_id": f"item:{it.item_id}", "group_label": f"{name} x{it.qty}"}
            if it.optional and it.item_id not in asked:
                fixes.append(f"asked before picking optional {it.item_id}")
                steps.append(
                    AskUserStep(
                        step_id=sid(),
                        title=f"Ask about {name}",
                        ask_kind="recommendation",
                        question=f"Want {name}? It's on my way.",
                        sku=it.sku,
                        rec_id=it.rec_id,
                        **group,
                    )
                )
                asked.add(it.item_id)
            pre, post = default_conditions("navigate_to", goal_pose=loc.approach_pose)
            steps.append(
                NavigateToStep(
                    step_id=sid(),
                    title=f"Go to {name} ({loc.aisle_label or it.slot_id})",
                    target="stop" if it.stop_id else "slot",
                    slot_id=it.slot_id,
                    stop_id=it.stop_id,
                    goal_pose=loc.approach_pose,
                    preconditions=pre,
                    postconditions=post,
                    **group,
                )
            )
            pre, post = default_conditions("align", slot_id=it.slot_id, approach_pose=loc.approach_pose)
            steps.append(
                AlignStep(
                    step_id=sid(),
                    title=f"Look at the {name} shelf",
                    slot_id=it.slot_id,
                    location=loc,
                    lift_height_m=loc.lift_height_m,
                    preconditions=pre,
                    postconditions=post,
                    **group,
                )
            )
            pre, _ = default_conditions("scan_shelf", approach_pose=loc.approach_pose)
            remaining = max(it.qty - it.picked_qty, 0)
            steps.append(
                ScanShelfStep(
                    step_id=sid(),
                    title=f"Scan {name}",
                    slot_id=it.slot_id,
                    expected_sku=it.sku,
                    location=loc,
                    min_count=1,
                    preconditions=pre,
                    **group,
                )
            )
            if (it.category or (p.category if p else None)) == "produce":  # fleet items may omit the category
                steps.append(
                    AskUserStep(
                        step_id=sid(),
                        title=f"Let the shopper pick {name}",
                        ask_kind="pick_choice",
                        question=f"Which {name} should I take?",
                        sku=it.sku,
                        on_decline="continue",
                        **group,
                    )
                )
            desc = ", ".join(x for x in (f"{p.brand} {p.name}" if p and p.brand else name, p.size if p else None) if x)
            hints = [x for x in ((p.brand if p else None), (p.size if p else None)) if x]
            for k in range(1, remaining + 1):
                unit = it.picked_qty + k
                bkey = binding_key(it.item_id, unit)
                target = TargetSpec(sku=it.sku, slot_id=it.slot_id, description=desc, label_hints=hints, location=loc)
                tag = f"({k}/{remaining})"
                pre, post = default_conditions("ground_target", sku=it.sku, slot_id=it.slot_id, binding=bkey)
                steps.append(
                    GroundTargetStep(
                        step_id=sid(),
                        title=f"Find a {name} {tag}",
                        target=target,
                        binding=bkey,
                        unit=unit,
                        preconditions=pre,
                        postconditions=post,
                        **group,
                    )
                )
                pre, post = default_conditions(
                    "grasp",
                    sku=it.sku,
                    slot_id=it.slot_id,
                    approach_pose=loc.approach_pose,
                    expect_text=p.name if p else None,
                )
                steps.append(
                    GraspStep(
                        step_id=sid(),
                        title=f"Grab {name} {tag}",
                        target=target,
                        binding=bkey,
                        instruction=f"pick up the {name}",
                        unit=unit,
                        preconditions=pre,
                        postconditions=post,
                        **group,
                    )
                )
                pre, post = default_conditions("place", sku=it.sku)
                steps.append(
                    PlaceStep(
                        step_id=sid(),
                        title=f"Put {name} in the basket {tag}",
                        sku=it.sku,
                        unit=unit,
                        preconditions=pre,
                        postconditions=post,
                        **group,
                    )
                )

        dock = _dock_pose(request.robot_id, store_id) or planogram.start_pose

        def pose_for(target: str | None) -> Pose2D:
            if target == "checkout":
                return planogram.checkout
            if target == "entrance":
                return planogram.entrance
            return dock

        for d in draft.steps:
            if d.item_id is not None and d.item_id not in known:
                fixes.append(f"dropped {d.kind} for unknown item {d.item_id}")
                continue
            if d.kind == "fetch_item" and d.item_id:
                await fetch(known[d.item_id])
            elif d.kind == "ask_user":
                it = known.get(d.item_id or "")
                kind = "recommendation" if it is not None and it.optional else "question"
                if it is not None:
                    asked.add(it.item_id)
                steps.append(
                    AskUserStep(
                        step_id=sid(),
                        title=d.question or "Question",
                        ask_kind=kind,
                        question=d.question or "Continue?",
                        item_id=d.item_id,
                        sku=it.sku if it else None,
                        rec_id=it.rec_id if it else None,
                        group_id=f"item:{d.item_id}" if d.item_id else None,
                    )
                )
            elif d.kind == "scan_shelf" and d.slot_id:
                loc = await self._location(store_id, d.slot_id)
                if loc is None:
                    fixes.append(f"dropped scan of unknown slot {d.slot_id}")
                    continue
                pre, post = default_conditions("navigate_to", goal_pose=loc.approach_pose)
                steps.append(
                    NavigateToStep(
                        step_id=sid(),
                        title=f"Go to {d.slot_id}",
                        target="slot",
                        slot_id=d.slot_id,
                        goal_pose=loc.approach_pose,
                        preconditions=pre,
                        postconditions=post,
                    )
                )
                steps.append(
                    ScanShelfStep(
                        step_id=sid(), title=f"Scan {d.slot_id}", slot_id=d.slot_id, location=loc, min_count=0
                    )
                )
            elif d.kind == "navigate_to":
                pose = pose_for(d.target)
                pre, post = default_conditions("navigate_to", goal_pose=pose)
                steps.append(
                    NavigateToStep(
                        step_id=sid(),
                        title=f"Go to the {d.target or 'checkout'}",
                        target=d.target or "checkout",
                        goal_pose=pose,
                        preconditions=pre,
                        postconditions=post,
                    )
                )
            elif d.kind in ("checkout", "verify_basket"):
                counts: dict[str, int] = {}
                for it in items:
                    if it.sku and it.status in (*OPEN_ITEM_STATUSES, "in_basket"):
                        counts[it.sku] = counts.get(it.sku, 0) + it.qty
                _, post = default_conditions("verify", basket_counts=counts)
                steps.append(
                    VerifyStep(
                        step_id=sid(),
                        title="Check the basket before checkout",
                        condition=post,  # type: ignore[arg-type]
                        purpose="audit",
                        group_id="checkout",
                        group_label="Checkout",
                    )
                )
                if d.kind == "checkout":
                    pre, post = default_conditions("navigate_to", goal_pose=planogram.checkout)
                    steps.append(
                        NavigateToStep(
                            step_id=sid(),
                            title="Go to checkout",
                            target="checkout",
                            goal_pose=planogram.checkout,
                            preconditions=pre,
                            postconditions=post,
                            group_id="checkout",
                            group_label="Checkout",
                        )
                    )
                    pre, post = default_conditions("handoff", goal_pose=planogram.checkout)
                    steps.append(
                        HandoffStep(
                            step_id=sid(),
                            title="Hand the basket off at checkout",
                            preconditions=pre,
                            postconditions=post,
                            group_id="checkout",
                            group_label="Checkout",
                        )
                    )

        plan = StepPlan(
            plan_id=pid,
            root_plan_id=root,
            revision=revision,
            goal_id=goal_id,
            robot_id=request.robot_id,
            store_id=store_id,
            trip_id=goal.trip_id if goal else None,
            user_id=request.user_id or (goal.user_id if goal else None),
            request_id=request.request_id or (goal.request_id if goal else None),
            mode=request.mode,
            title=draft.summary,
            planner=(info or PlannerInfo()).model_copy(
                update={
                    "origin": (info.origin if info else "scripted") if revision == 1 else "replan",
                    "rationale": draft.rationale,
                    "fixes": fixes,
                }
            ),
            items=list(items),
            budget_cents=budget_cents,
            end_at=end_at,
            steps=steps,
            status=PlanStatus.READY,
        )
        errors = plan_errors(plan)
        if errors:
            raise ValueError("invalid plan: " + "; ".join(f"{e.code} {e.step_id or ''} {e.message}" for e in errors))
        return plan

    async def replan(self, request: ReplanRequest) -> StepPlan:
        old = request.plan
        carried = [s for s in old.steps if s.status in CLOSED_STEP_STATUSES]
        items: list[GoalItem] = []
        for it in old.items:
            if it.item_id in request.skip_item_ids and it.status in OPEN_ITEM_STATUSES:
                it = it.model_copy(update={"status": "skipped"})
            sub = request.substitutions.get(it.item_id)
            if sub and it.status in OPEN_ITEM_STATUSES:
                got = await self._resolve(old.store_id, 0, sub, it.qty, it.category, sub)
                it = it.model_copy(
                    update={
                        "sku": got.sku,
                        "slot_id": got.slot_id,
                        "name": got.name,
                        "status": got.status,
                        "price_cents": got.price_cents,
                        "notes": f"substituted for {it.sku}",
                    }
                )
            items.append(it)
        rev = old.revision + 1
        preq = PlanRequest(
            robot_id=old.robot_id,
            text=old.title or "replan",
            store_id=old.store_id,
            user_id=old.user_id,
            request_id=old.request_id,
            end_at=old.end_at,
            mode=old.mode,
        )
        draft = self.scripted_draft(items, old.title, end_at=old.end_at)
        new = await self.compile(
            draft,
            preq,
            items=items,
            budget_cents=old.budget_cents,
            revision=rev,
            root_plan_id=old.root_plan_id,
            carried=carried,
            id_prefix=f"r{rev}s",
        )
        return new.model_copy(
            update={
                "goal_id": old.goal_id,
                "trip_id": old.trip_id,
                "supersedes": old.plan_id,
                "bindings": dict(old.bindings),
                "reason": request.reason or (request.decision.reason if request.decision else ""),
            }
        )

    async def expand_tool_call(self, plan: StepPlan, tool_name: str, tool_input: Mapping[str, Any]) -> list[PlanStep]:
        if tool_name not in ("navigate_to", "scan_shelf", "pick_item", "place_in_basket", "finish_goal"):
            raise KeyError(tool_name)
        inp: Any = parse_brain_tool_input(tool_name, dict(tool_input))
        n = 1 + len({s.step_id.split(".")[0] for s in plan.steps if s.origin == "tool"})
        k = [0]

        def sid() -> str:
            k[0] += 1
            return f"t{n:02d}.{k[0]}"

        planogram = await self._planogram(plan.store_id)
        out: list[PlanStep] = []
        base = {"origin": "tool"}
        if tool_name == "navigate_to":
            slot_id = inp.slot_id
            if inp.target == "stop":
                it = next((i for i in plan.items if i.stop_id == inp.stop_id), None)
                slot_id = it.slot_id if it else None
            loc = await self._location(plan.store_id, slot_id) if slot_id else None
            pose = (
                loc.approach_pose
                if loc
                else {"checkout": planogram.checkout, "entrance": planogram.entrance}.get(
                    inp.target, _dock_pose(plan.robot_id, plan.store_id) or planogram.start_pose
                )
            )
            pre, post = default_conditions("navigate_to", goal_pose=pose)
            out.append(
                NavigateToStep(
                    step_id=sid(),
                    title=f"Go to {slot_id or inp.target}",
                    target=inp.target,
                    slot_id=slot_id,
                    stop_id=inp.stop_id,
                    goal_pose=pose,
                    preconditions=pre,
                    postconditions=post,
                    **base,
                )
            )
        elif tool_name in ("scan_shelf", "pick_item"):
            loc = await self._location(plan.store_id, inp.slot_id)
            if loc is None:
                raise ValueError(f"unknown slot {inp.slot_id}")
            item_id = getattr(inp, "item_id", None)
            pre, post = default_conditions("align", slot_id=inp.slot_id, approach_pose=loc.approach_pose)
            out.append(
                AlignStep(
                    step_id=sid(),
                    title=f"Look at {inp.slot_id}",
                    slot_id=inp.slot_id,
                    location=loc,
                    lift_height_m=loc.lift_height_m,
                    item_id=item_id,
                    preconditions=pre,
                    postconditions=post,
                    **base,
                )
            )
            if tool_name == "scan_shelf":
                out.append(
                    ScanShelfStep(
                        step_id=sid(),
                        title=f"Scan {inp.slot_id}",
                        slot_id=inp.slot_id,
                        expected_sku=inp.expected_sku,
                        location=loc,
                        min_count=0,
                        **base,
                    )
                )
            else:
                p = await self._product(inp.sku)
                name = p.name if p else inp.sku
                bkey = f"target:{item_id or 'tool'}:{n}"
                target = TargetSpec(
                    sku=inp.sku,
                    slot_id=inp.slot_id,
                    instance_id=inp.instance_id,
                    description=f"{p.brand} {p.name}" if p and p.brand else name,
                    location=loc,
                )
                pre, post = default_conditions("ground_target", sku=inp.sku, slot_id=inp.slot_id, binding=bkey)
                out.append(
                    GroundTargetStep(
                        step_id=sid(),
                        title=f"Find a {name}",
                        target=target,
                        binding=bkey,
                        item_id=item_id,
                        preconditions=pre,
                        postconditions=post,
                        **base,
                    )
                )
                pre, post = default_conditions(
                    "grasp",
                    sku=inp.sku,
                    slot_id=inp.slot_id,
                    approach_pose=loc.approach_pose,
                    expect_text=p.name if p else None,
                )
                out.append(
                    GraspStep(
                        step_id=sid(),
                        title=f"Grab {name}",
                        target=target,
                        binding=bkey,
                        item_id=item_id,
                        instruction=f"pick up the {name}",
                        approval_id=inp.approval_id,
                        preconditions=pre,
                        postconditions=post,
                        **base,
                    )
                )
        elif tool_name == "place_in_basket":
            last = next((s for s in reversed(plan.steps) if isinstance(s, GraspStep)), None)
            sku = last.target.sku if last else (plan.item(inp.item_id).sku if plan.item(inp.item_id) else None)
            if sku is None:
                raise ValueError("place_in_basket: nothing grasped in this goal yet")
            pre, post = default_conditions("place", sku=sku)
            out.append(
                PlaceStep(
                    step_id=sid(),
                    title=f"Put {sku} in the basket",
                    sku=sku,
                    item_id=inp.item_id or (last.item_id if last else None),
                    preconditions=pre,
                    postconditions=post if inp.verify else None,
                    **base,
                )
            )
        elif tool_name == "finish_goal" and plan.end_at == "checkout":
            _, audit = default_conditions("verify", basket_counts={})
            out.append(
                VerifyStep(
                    step_id=sid(),
                    title="Check the basket before checkout",
                    condition=audit,  # type: ignore[arg-type]
                    purpose="audit",
                    **base,
                )
            )
            pre, post = default_conditions("navigate_to", goal_pose=planogram.checkout)
            out.append(
                NavigateToStep(
                    step_id=sid(),
                    title="Go to checkout",
                    target="checkout",
                    goal_pose=planogram.checkout,
                    preconditions=pre,
                    postconditions=post,
                    **base,
                )
            )
            pre, post = default_conditions("handoff", goal_pose=planogram.checkout)
            out.append(
                HandoffStep(step_id=sid(), title="Hand the basket off", preconditions=pre, postconditions=post, **base)
            )
        return out

    def validate(self, plan: StepPlan) -> list[PlanIssue]:
        """``plan.validate_plan`` + store checks: every slot the plan drives to / scans exists in the robot's store
        (when that planogram is already loaded; validate is sync)."""
        issues = validate_plan(plan)
        planogram = self._plans.get(plan.store_id)
        if planogram is not None:
            for step in plan.steps:
                slot_id = getattr(step, "slot_id", None)
                if slot_id and planogram.slot(slot_id) is None:
                    issues.append(
                        PlanIssue(
                            severity="error",
                            step_id=step.step_id,
                            code="unknown_slot",
                            message=f"slot {slot_id} is not in {plan.store_id}",
                        )
                    )
        return issues


def _dock_pose(robot_id: str, store_id: str) -> Pose2D | None:
    """The robot's dock (data/fixtures/robots.yaml) when it is in this store, like the v0.2 executor; else None."""
    try:
        from utils.data import load_robot

        spec = load_robot(robot_id)
        return spec.dock if spec.store_id == store_id else None
    except Exception:  # noqa: BLE001 - unknown robot / no fixture: the caller falls back to the store start pose
        return None
