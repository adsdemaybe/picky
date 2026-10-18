"""Scripted (deterministic) robot brain: runs the SAME brain tools the LLM would, in a fixed order.

For shop goals it reproduces the gateway's old per-stop loop: stops in route order; optional detour -> ask first
(declined -> re-plan); navigate -> look -> scan (empty slot -> online order); mandate check on the full line (deny ->
failed, needs approval -> ask over_threshold); produce -> freshness top-3 -> shopper picks; pick + place every unit
(retry once on grasp_failed / busy / a vanished unit); cart line once all units are in; basket check; then checkout,
unload and finish. fetch / shelf_scan / return_to_dock get their own short scripts. Also the LLM brain's fallback: it
resumes from the goal's current state (items already handled are skipped).
"""

from __future__ import annotations

from typing import Any

from arms.agent.brain_exec import ACTIVE_ITEM_STATUSES, BrainExecutor
from contracts.common import money
from contracts.fleet import GoalItem
from contracts.tools import AskUserResult, BrainToolError, PickResult, PlaceResult, RouteSummary, ScanShelfResult
from contracts.trust import MandateDecision, MandateReason

NAV_RETRY_CODES = frozenset({"path_blocked", "timeout", "disconnected", "busy", "not_ready"})


def ok(result: Any) -> bool:
    return result is not None and not isinstance(result, BrainToolError)


def over_title(name: str, cents: int, decision: MandateDecision | None) -> str:
    """Shopper-facing question for a line the mandate wants confirmed, naming the rule that fired."""
    reasons = set(decision.reasons) if decision else set()
    if decision and MandateReason.OVER_BUDGET in reasons and decision.budget_cents is not None:
        over = (decision.total_cents or 0) - decision.budget_cents
        return (
            f"{name} ({money(cents)}) puts you {money(over)} over your {money(decision.budget_cents)} budget. "
            "Add it anyway?"
        )
    if MandateReason.ABOVE_ASK_THRESHOLD in reasons:
        return f"{name} is {money(cents)}, above your ask-me limit. Add it?"
    return f"{name} is {money(cents)}. Add it anyway?"


class ScriptedRunner:
    def __init__(self, ex: BrainExecutor) -> None:
        self.ex = ex
        self.processed: set[str] = set()

    async def run(self) -> None:
        kind = self.ex.goal.kind
        if kind == "shop":
            await self.shop()
        elif kind == "fetch":
            await self.fetch()
        elif kind == "shelf_scan":
            await self.shelf_scan()
        else:
            await self.return_to_dock()

    # -- shared --------------------------------------------------------------------------------------------------
    async def navigate(self, **args: Any) -> Any:
        """navigate_to, re-planning and retrying once on a retryable failure (path_blocked, timeout, disconnected)."""
        res = await self.ex.call("navigate_to", args)
        if isinstance(res, BrainToolError) and res.error_code in NAV_RETRY_CODES:
            await self.ex.thought(f"Navigation failed ({res.error_code}); re-planning from where I am and retrying.")
            if self.ex.goal.kind == "shop" or args.get("target") == "slot":
                await self.ex.call("plan_route", self._replan_args())
            res = await self.ex.call("navigate_to", args)
        return res

    def _replan_args(self) -> dict[str, Any]:
        if self.ex.goal.kind == "shop":
            return {}
        return {"slot_ids": self.ex._pending_slots(), "end_at": self.ex.goal.end_at}

    async def finish(self, summary: str, status: str = "completed", failure_code: str | None = None) -> None:
        args: dict[str, Any] = {"status": status, "summary": summary}
        if failure_code:
            args["failure_code"] = failure_code
        await self.ex.call("finish_goal", args)

    async def pick_units(self, item: GoalItem, chosen: list[str | None]) -> str:
        """Pick + place the remaining units of ``item``. Returns 'done' | 'declined' | 'denied' | 'failed'."""
        ex = self.ex
        approval_id: str | None = None
        remaining = max(item.qty - item.picked_qty, 0)
        for n in range(remaining):
            inst = chosen[n] if n < len(chosen) else None
            args: dict[str, Any] = {"slot_id": item.slot_id, "sku": item.sku, "item_id": item.item_id}
            if inst:
                args["instance_id"] = inst
            if approval_id:
                args["approval_id"] = approval_id
            res = await ex.call("pick_item", args)
            if isinstance(res, PickResult) and res.needs_approval:
                cents = item.qty * await ex.unit_price(item)
                name = item.name or item.query
                product = await ex.product(item.sku)
                if product is not None:
                    name = product.name
                ask = await ex.call(
                    "ask_user",
                    {
                        "kind": "over_threshold",
                        "question": over_title(name, cents, res.decision),
                        "body": "; ".join(res.decision.messages) if res.decision else "",
                        "item_id": item.item_id,
                    },
                )
                if not (isinstance(ask, AskUserResult) and ask.approved):
                    await ex.call(
                        "update_progress",
                        {
                            "note": f"Skipping {name}: over your limit and you said no",
                            "item_id": item.item_id,
                            "item_status": "declined",
                        },
                    )
                    return "declined"
                approval_id = ask.approval_id
                args["approval_id"] = approval_id
                res = await ex.call("pick_item", args)
            if isinstance(res, PickResult) and res.error_code == "mandate_denied":
                return "denied"
            if isinstance(res, BrainToolError):
                retry = res.error_code in ("grasp_failed", "busy") or (res.error_code == "no_instance" and inst)
                if not retry:
                    break
                await ex.thought(f"Pick failed ({res.error_code}); trying once more.")
                args.pop("instance_id", None)
                res = await ex.call("pick_item", args)
            if not (isinstance(res, PickResult) and res.picked):
                break
            last = n == remaining - 1
            placed = await ex.call("place_in_basket", {"item_id": item.item_id, "verify": last})
            if not isinstance(placed, PlaceResult):
                break
        fresh = ex.item(item.item_id) or item
        if fresh.status == "in_basket":
            return "done"
        if fresh.picked_qty > 0:
            await ex.call(
                "update_progress",
                {
                    "note": f"Got {fresh.picked_qty} of {fresh.qty} {fresh.name or fresh.query}",
                    "item_id": fresh.item_id,
                    "item_status": "in_basket",
                },
            )
            return "done"
        await ex.call(
            "update_progress",
            {"note": f"Could not pick {fresh.name or fresh.query}", "item_id": fresh.item_id, "item_status": "failed"},
        )
        return "failed"

    async def freshness_order(self, item: GoalItem) -> list[str | None]:
        """Produce: ask the shopper to pick among the freshest units; returns instance ids in pick order."""
        qty = max(item.qty - item.picked_qty, 0)
        res = await self.ex.call(
            "ask_user",
            {
                "kind": "pick_choice",
                "question": f"Which {item.name or item.query}? #1 is the freshest",
                "item_id": item.item_id,
            },
        )
        if not isinstance(res, AskUserResult) or not res.options:
            return [None] * qty
        ranked = [o.option_id for o in res.options]
        picked = ([res.choice] if res.choice else []) + list(res.choices)
        first = picked[0] if res.approved and picked else ranked[0]
        order: list[str | None] = [first, *[i for i in ranked if i != first]][:qty]
        return order + [None] * (qty - len(order))

    # -- shop ----------------------------------------------------------------------------------------------------
    def next_item(self) -> GoalItem | None:
        ex = self.ex
        order = {sid: n for n, sid in enumerate(ex.route.order)} if ex.route else {}
        cands = [
            (n, i)
            for n, i in enumerate(ex.goal.items)
            if i.status in ACTIVE_ITEM_STATUSES and i.item_id not in self.processed
        ]
        if not cands:
            return None
        cands.sort(key=lambda t: (order.get(t[1].stop_id or "", len(order) + t[0]), t[0]))
        return cands[0][1]

    async def shop(self) -> None:
        ex = self.ex
        todo = [i for i in ex.goal.items if i.status in ACTIVE_ITEM_STATUSES]
        await ex.thought(f"{ex.goal.title}: {len(todo)} item(s) to get. Planning my route.")
        skus = sorted({i.sku for i in todo if i.sku})
        if skus:
            await ex.call("locate_items", {"skus": skus})
        route = await ex.call("plan_route", {})
        if isinstance(route, RouteSummary):
            n = sum(1 for s in route.stops if s.kind in ("mandatory", "optional"))
            await ex.thought(f"Route planned: {n} stops, {route.total_distance_m:.0f} m.")
        while (item := self.next_item()) is not None:
            self.processed.add(item.item_id)
            await self.shop_item(item)
        await self.finish(self.shop_summary())

    async def detour_question(self, item: GoalItem) -> tuple[str, str]:
        ex = self.ex
        trip = await ex.trip()
        rec = next((r for r in trip.recs if r.rec_id == item.rec_id), None) if trip and item.rec_id else None
        stop = trip.route.stop(item.stop_id) if trip and trip.route and item.stop_id else None
        if stop is None and ex.route is not None and item.stop_id:
            stop = ex.route.stop(item.stop_id)
        name = (rec.product_name if rec else None) or item.name or item.query
        price = f" ({money(rec.price_cents)})" if rec and rec.price_cents is not None else ""
        extra = ""
        if stop is not None and stop.detour_m is not None:
            extra = f" Detour: +{stop.detour_m:.0f} m"
            if stop.detour_s is not None:
                extra += f", {stop.detour_s:.0f} s"
        over = await ex.budget_overage(rec.price_cents if rec else None)
        title = f"Grab {name}{price}?"
        if over and trip is not None and trip.budget_cents is not None:
            title += f" Heads up: it puts you {money(over)} over your {money(trip.budget_cents)} budget."
        body = ((rec.reason if rec else "") + ("." if rec else "") + extra).strip()
        return title, body

    async def shop_item(self, item: GoalItem) -> None:
        ex = self.ex
        name = item.name or item.query
        if not item.sku or not item.slot_id:
            await ex.call(
                "update_progress",
                {"note": f"{name}: no shelf location", "item_id": item.item_id, "item_status": "failed"},
            )
            return
        if item.optional:
            title, body = await self.detour_question(item)
            ans = await ex.call(
                "ask_user", {"kind": "recommendation", "question": title, "body": body, "item_id": item.item_id}
            )
            if not (isinstance(ans, AskUserResult) and ans.approved):
                await ex.call(
                    "update_progress",
                    {"note": f"Shopper said no to {name}", "item_id": item.item_id, "item_status": "declined"},
                )
                return
        await ex.thought(f"Next: {item.qty}x {name} ({item.slot_id}).")
        nav_args: dict[str, Any] = (
            {"target": "stop", "stop_id": item.stop_id} if item.stop_id else {"target": "slot", "slot_id": item.slot_id}
        )
        nav = await self.navigate(**nav_args)
        if isinstance(nav, BrainToolError):
            await ex.error(f"could not reach {name}: {nav.error}", code=nav.error_code)
            await ex.call(
                "update_progress",
                {
                    "note": f"Could not reach {name} ({nav.error_code})",
                    "item_id": item.item_id,
                    "item_status": "failed",
                },
            )
            return
        await ex.call("look", {"camera": "front", "question": f"Is there {name} on this shelf? How many?"})
        scan = await ex.call("scan_shelf", {"slot_id": item.slot_id, "expected_sku": item.sku})
        if isinstance(scan, ScanShelfResult) and not scan.in_stock:
            await ex.call(
                "update_progress",
                {
                    "note": f"{name} is out of stock here; ordering it online",
                    "item_id": item.item_id,
                    "item_status": "unavailable",
                },
            )
            return
        item = ex.item(item.item_id) or item
        chosen: list[str | None] = [None] * item.qty
        if await ex.is_produce(item) and isinstance(scan, ScanShelfResult):
            if await ex.freshness_available(item):
                chosen = await self.freshness_order(item)
            else:
                await ex.thought(f"All the {name} look equally fresh; I'll take any.")
        outcome = await self.pick_units(item, chosen)
        if outcome == "done":
            done = sum(1 for i in ex.goal.items if i.status not in ACTIVE_ITEM_STATUSES)
            await ex.call(
                "update_progress",
                {
                    "note": f"Got {name} ({done}/{len(ex.goal.items)} items handled)",
                    "progress": round(done / max(len(ex.goal.items), 1), 3),
                },
            )

    def shop_summary(self) -> str:
        items = self.ex.goal.items
        got = [i for i in items if i.status == "in_basket"]
        online = [i.name or i.query for i in items if i.status == "online"]
        want = [i for i in items if not i.optional or i.status == "in_basket"]
        text = f"Got {len(got)} of {len(want)} items"
        if online:
            text += f"; {', '.join(online)} ordered online"
        declined = [i.name or i.query for i in items if i.status == "declined" and i.optional]
        if declined:
            text += f"; skipped {', '.join(declined)}"
        failed = [i.name or i.query for i in items if i.status in ("failed", "unavailable")]
        if failed:
            text += f"; could not get {', '.join(failed)}"
        return text + ". Heading to checkout."

    # -- fetch ---------------------------------------------------------------------------------------------------
    async def fetch(self) -> None:
        ex = self.ex
        items = [i for i in ex.goal.items if i.status in ACTIVE_ITEM_STATUSES]
        if not items:
            await self.finish("Nothing to fetch.")
            return
        item = items[0]
        name = item.name or item.query
        await ex.thought(f"Fetching {item.qty}x {name}.")
        if not item.slot_id:
            await ex.call("locate_items", {"skus": [item.sku]} if item.sku else {"queries": [item.query]})
            item = ex.item(item.item_id) or item
        if not item.slot_id or not item.sku:
            await ex.call(
                "update_progress",
                {"note": f"{name} is not in this store", "item_id": item.item_id, "item_status": "unavailable"},
            )
            await self.finish(f"Could not find {name} in this store.", "failed", "not_found")
            return
        await ex.call("plan_route", {"slot_ids": [item.slot_id], "end_at": ex.goal.end_at})
        nav = await self.navigate(target="slot", slot_id=item.slot_id)
        if isinstance(nav, BrainToolError):
            await ex.call(
                "update_progress", {"note": f"Could not reach {name}", "item_id": item.item_id, "item_status": "failed"}
            )
            await self.finish(f"Could not reach {name} ({nav.error_code}).", "failed", nav.error_code)
            return
        await ex.call("look", {"camera": "front", "question": f"Is there {name} on this shelf?"})
        scan = await ex.call("scan_shelf", {"slot_id": item.slot_id, "expected_sku": item.sku})
        if isinstance(scan, ScanShelfResult) and not scan.in_stock:
            await ex.call(
                "update_progress",
                {"note": f"{name} is out of stock", "item_id": item.item_id, "item_status": "unavailable"},
            )
            await self.finish(f"{name} is out of stock.", "failed", "no_instance")
            return
        outcome = await self.pick_units(ex.item(item.item_id) or item, [None] * item.qty)
        if outcome == "done":
            where = {"checkout": "the checkout", "dock": "my dock"}.get(ex.goal.end_at, "here")
            await self.finish(f"Fetched {name}; it's at {where}.")
        else:
            await self.finish(f"Could not pick {name}.", "failed", "grasp_failed")

    # -- shelf scan ----------------------------------------------------------------------------------------------
    async def shelf_scan(self) -> None:
        ex = self.ex
        done = set(ex.goal.scanned_slot_ids)
        slots = [s for s in ex.goal.slot_ids if s not in done]
        if not slots:
            await self.finish("Nothing left to scan.")
            return
        await ex.thought(f"Scanning {len(slots)} slot(s).")
        route = await ex.call("plan_route", {"slot_ids": slots, "end_at": ex.goal.end_at})
        order = [s.slot_id for s in route.stops if s.slot_id] if isinstance(route, RouteSummary) else []
        order = [s for s in order if s in slots] + [s for s in slots if s not in order]
        empty: list[str] = []
        mispriced: list[str] = []
        failed: list[str] = []
        for n, slot_id in enumerate(order, 1):
            nav = await self.navigate(target="slot", slot_id=slot_id)
            if isinstance(nav, BrainToolError):
                failed.append(slot_id)
                continue
            scan = await ex.call("scan_shelf", {"slot_id": slot_id})
            if isinstance(scan, ScanShelfResult):
                if not scan.in_stock:
                    empty.append(scan.expected_sku or slot_id)
                if scan.mispriced:
                    mispriced.append(scan.expected_sku or slot_id)
            else:
                failed.append(slot_id)
            if n % 5 == 0 or n == len(order):
                await ex.call("update_progress", {"note": f"Scanned {n}/{len(order)} slots"})
        text = f"Scanned {len(order) - len(failed)} of {len(order)} slots"
        if empty:
            text += f"; out of stock: {', '.join(empty)}"
        if mispriced:
            text += f"; mispriced: {', '.join(mispriced)}"
        if failed:
            text += f"; could not scan {len(failed)}"
        status = "completed" if len(failed) < len(order) else "failed"
        await self.finish(text + ".", status, None if status == "completed" else "path_blocked")

    # -- return to dock ------------------------------------------------------------------------------------------
    async def return_to_dock(self) -> None:
        ex = self.ex
        await ex.thought("Heading back to my dock to charge.")
        nav = await self.navigate(target="dock")
        if isinstance(nav, BrainToolError):
            await self.finish(f"Could not reach the dock ({nav.error_code}).", "failed", nav.error_code)
            return
        await self.finish("Docked and charging.")
