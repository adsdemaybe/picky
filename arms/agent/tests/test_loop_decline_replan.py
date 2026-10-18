"""Plan mode: a declined optional stop re-solves the trip route, so the web map matches what the robot drives."""

from typing import Any

from arms.agent.tests.loop_world import build_loop_services
from arms.agent.tests.loop_world.helpers import run
from arms.agent.tests.test_loop_review_fixes import MILD_SLOT, ROBOT, RecordingTrips, _goal
from contracts.fleet import GoalItem
from contracts.trust import ApprovalRequest, ApprovalResponse


class ReplanningTrips(RecordingTrips):
    def __init__(self, answer: str, fail: bool = False) -> None:
        super().__init__()
        self.answer, self.fail = answer, fail
        self.log: list[tuple[str, ...]] = []
        self.replans: list[tuple[bool, Any]] = []

    async def set_stop_status(self, trip_id: str, stop_id: str, status: str, reason: str = "") -> None:
        await super().set_stop_status(trip_id, stop_id, status, reason)
        self.log.append(("stop", stop_id, status))

    async def ask(self, trip_id: str, req: ApprovalRequest) -> ApprovalResponse:
        if req.kind.value == "recommendation" and self.answer == "first_no":
            self.answer = "approve"  # no to the first rec, yes to the next one
            return ApprovalResponse(approval_id=req.approval_id, decision="reject")
        ok = req.kind.value != "recommendation" or self.answer == "approve"
        return ApprovalResponse(approval_id=req.approval_id, decision="approve" if ok else "reject")

    async def replan(self, trip_id: str, include_recommendations: bool = True, max_optional_stops: Any = None) -> Any:
        self.log.append(("replan", trip_id))
        self.replans.append((include_recommendations, max_optional_stops))
        if self.fail:
            raise RuntimeError("routing down")
        return None


def _trip_goal() -> Any:
    return _goal(
        GoalItem(item_id="stop_1", query="salsa", sku="SALSA-MILD", slot_id=MILD_SLOT, stop_id="stop_1"),
        GoalItem(item_id="stop_2", query="hot salsa", sku="SALSA-HOT", optional=True, stop_id="stop_2"),
        trip_id="trip-1",
    )


def test_declined_recommendation_replans_the_trip_route_after_marking_the_stop() -> None:
    s = build_loop_services()
    s.trips = ReplanningTrips("reject")  # type: ignore[assignment]
    out = run(s.runner.run_goal(ROBOT, _trip_goal()))
    assert out.status == "completed" and out.item_status["stop_2"] == "declined"
    log = s.trips.log  # type: ignore[attr-defined]
    assert log.count(("replan", "trip-1")) == 1
    assert log.index(("stop", "stop_2", "declined")) < log.index(("replan", "trip-1"))  # declined stop leaves the route
    assert s.trips.replans == [(False, None)]  # type: ignore[attr-defined]  # no optional stop left to drive to


def test_replan_keeps_only_as_many_optional_stops_as_the_plan_still_drives_to() -> None:
    s = build_loop_services()
    s.trips = ReplanningTrips("first_no")  # type: ignore[assignment]
    goal = _goal(
        GoalItem(item_id="stop_1", query="salsa", sku="SALSA-MILD", slot_id=MILD_SLOT, stop_id="stop_1"),
        GoalItem(item_id="stop_2", query="hot salsa", sku="SALSA-HOT", optional=True, stop_id="stop_2"),
        GoalItem(item_id="stop_3", query="salsa again", sku="SALSA-MILD", optional=True, stop_id="stop_3"),
        trip_id="trip-1",
    )
    out = run(s.runner.run_goal(ROBOT, goal))
    assert out.status == "completed"
    declined = [k for k, v in out.item_status.items() if v == "declined"]
    assert len(declined) == 1 and "in_basket" in {out.item_status["stop_2"], out.item_status["stop_3"]}
    assert s.trips.replans == [(True, 1)]  # type: ignore[attr-defined]  # the other rec is still ahead


def test_accepted_recommendation_keeps_the_planned_route() -> None:
    s = build_loop_services()
    s.trips = ReplanningTrips("approve")  # type: ignore[assignment]
    out = run(s.runner.run_goal(ROBOT, _trip_goal()))
    assert out.status == "completed" and out.item_status["stop_2"] == "in_basket"
    assert ("replan", "trip-1") not in s.trips.log  # type: ignore[attr-defined]


def test_a_routing_failure_after_a_decline_never_stops_the_run() -> None:
    s = build_loop_services()
    s.trips = ReplanningTrips("reject", fail=True)  # type: ignore[assignment]
    out = run(s.runner.run_goal(ROBOT, _trip_goal()))
    assert out.status == "completed" and out.item_status == {"stop_1": "in_basket", "stop_2": "declined"}
