"""Minimal stand-ins for the fleet, robot brain and camera services, so the gateway's own logic (trip flow around
``fleet.execute_trip``, REST / WS surface, MJPEG) is tested without depending on those arms' progress.

``StubFleet.execute_trip`` walks the trip's stops through the SAME TripController calls the robot brain makes
(assign_robot, ask for detours, set_stop_status, check_line, add_line), so the gateway side of the contract is
exercised end to end.
"""

import asyncio
import base64
import io
from collections.abc import AsyncIterator
from typing import Any

from contracts.cameras import CameraInfo, VisionObservation, parse_camera_id
from contracts.common import STAFF_USER_ID
from contracts.fleet import (
    BrainReply,
    BrainStatus,
    BrainStep,
    FleetRequest,
    Goal,
    GoalSpec,
    GoalStatus,
    GoalUpdate,
    RequestPlan,
    RobotInfo,
    goal_items_from_trip,
)
from contracts.services import Services
from contracts.shopping import CartLine
from contracts.sim import CameraFrame
from contracts.trust import ApprovalKind, ApprovalRequest, MandateVerdict
from utils.ids import new_id

ROBOTS = {
    "rover-ave-1": ("Pico", "store_ave"),
    "rover-ave-2": ("Rhea", "store_ave"),
    "rover-hill-1": ("Hazel", "store_hill"),
}


class StubFleet:
    def __init__(self, services: Services) -> None:
        self.services = services
        self.goals: dict[str, Goal] = {}
        self.requests: dict[str, RequestPlan] = {}
        self.started = False
        self.executed: list[str] = []
        self.outcome: GoalStatus = GoalStatus.COMPLETED

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.started = False

    async def submit_request(self, request: FleetRequest) -> RequestPlan:
        from contracts.api import CreateTripRequest

        rid = new_id("req")
        trip = await self.services.require("trips").create_trip(
            CreateTripRequest(
                user_id=request.user_id,
                text=request.text,
                store_id=request.store_id or "store_ave",
                request_id=rid,
                robot_id="rover-ave-1",
                auto_execute=True,
            )
        )
        plan = RequestPlan(
            request_id=rid, text=request.text, user_id=request.user_id, status="executing", trip_ids=[trip.trip_id]
        )
        self.requests[rid] = plan
        return plan

    async def get_request(self, request_id: str) -> RequestPlan:
        return self.requests[request_id]

    async def list_requests(self, user_id: str | None = None, limit: int = 20) -> list[RequestPlan]:
        return [p for p in self.requests.values() if user_id is None or p.user_id == user_id][:limit]

    async def list_robots(self, store_id: str | None = None) -> list[RobotInfo]:
        from contracts.common import Pose2D

        return [
            RobotInfo(robot_id=rid, name=name, store_id=sid, dock=Pose2D(x=1, y=1))
            for rid, (name, sid) in ROBOTS.items()
            if store_id is None or sid == store_id
        ]

    async def get_robot(self, robot_id: str) -> RobotInfo:
        for r in await self.list_robots():
            if r.robot_id == robot_id:
                return r
        raise KeyError(robot_id)

    async def create_goal(self, spec: GoalSpec) -> Goal:
        if spec.title == "conflict":
            raise ValueError("robot is busy")
        store = spec.store_id or ROBOTS.get(spec.robot_id or "", ("", "store_ave"))[1]
        goal = Goal(
            goal_id=new_id("goal"),
            kind=spec.kind,
            store_id=store,
            robot_id=spec.robot_id,
            title=spec.title or spec.kind,
            user_id=spec.user_id or STAFF_USER_ID,
            slot_ids=list(spec.slot_ids),
            status=GoalStatus.ASSIGNED,
        )
        self.goals[goal.goal_id] = goal
        return goal

    async def list_goals(self, store_id=None, robot_id=None, status=None, **kw: Any) -> list[Goal]:  # noqa: ANN001
        statuses = {status} if isinstance(status, str) else set(status or [])
        return [
            g
            for g in self.goals.values()
            if (store_id is None or g.store_id == store_id)
            and (robot_id is None or g.robot_id == robot_id)
            and (not statuses or g.status in statuses)
        ]

    async def get_goal(self, goal_id: str) -> Goal:
        return self.goals[goal_id]

    async def update_goal(self, goal_id: str, update: GoalUpdate) -> Goal:
        goal = self.goals[goal_id]
        if update.status is not None:
            goal = goal.model_copy(update={"status": update.status})
        self.goals[goal_id] = goal
        return goal

    async def cancel_goal(self, goal_id: str, reason: str = "") -> Goal:
        goal = self.goals[goal_id]
        if goal.terminal:
            return goal
        goal = goal.model_copy(update={"status": GoalStatus.CANCELLED, "error": reason})
        self.goals[goal_id] = goal
        return goal

    async def message_robot(self, robot_id: str, text: str, user_id: str | None = None) -> BrainReply:
        await self.get_robot(robot_id)
        return BrainReply(robot_id=robot_id, text=f"ok: {text}")

    async def stop_robot(self, robot_id: str, reason: str = "") -> RobotInfo:
        return await self.get_robot(robot_id)

    async def execute_trip(self, trip_id: str, robot_id: str | None = None) -> Goal:
        trips = self.services.require("trips")
        catalog = self.services.require("catalog")
        self.executed.append(trip_id)
        trip = await trips.get_trip(trip_id)
        robot_id = robot_id or trip.robot_id or "rover-ave-1"
        goal = Goal(
            goal_id=new_id("goal"),
            kind="shop",
            store_id=trip.store_id,
            robot_id=robot_id,
            trip_id=trip_id,
            user_id=trip.user_id,
            title="shop",
            status=GoalStatus.ACTIVE,
            items=goal_items_from_trip(trip),
        )
        self.goals[goal.goal_id] = goal
        await trips.assign_robot(trip_id, robot_id, goal.goal_id)
        for item in goal.items:
            if item.status != "pending" or item.sku is None:
                continue
            offers = await catalog.get_offers(item.sku, [trip.store_id])
            price = offers[0].price_cents if offers else (item.price_cents or 0)
            if item.optional:
                resp = await trips.ask(
                    trip_id,
                    ApprovalRequest(
                        approval_id=new_id("appr"),
                        user_id=trip.user_id,
                        kind=ApprovalKind.RECOMMENDATION,
                        title=f"Grab {item.name}?",
                        sku=item.sku,
                        rec_id=item.rec_id,
                        amount_cents=price * item.qty,
                        robot_id=robot_id,
                        goal_id=goal.goal_id,
                    ),
                )
                if not resp.approved:
                    await trips.set_stop_status(trip_id, item.stop_id, "declined", "shopper said no")
                    await trips.replan(trip_id)
                    continue
            await trips.set_stop_status(trip_id, item.stop_id, "navigating")
            await trips.set_stop_status(trip_id, item.stop_id, "arrived")
            line = CartLine(
                line_id=new_id("line"),
                sku=item.sku,
                name=item.name,
                category=item.category or "uncategorized",
                qty=item.qty,
                unit_price_cents=price,
                source="in_store",
                store_id=trip.store_id,
                slot_id=item.slot_id,
                status="picked",
                rec_id=item.rec_id,
                list_item_id=item.list_item_id,
            )
            approval_id = None
            decision = await trips.check_line(trip_id, line)
            if decision.verdict == MandateVerdict.DENY:
                await trips.set_stop_status(trip_id, item.stop_id, "failed", "mandate")
                continue
            if decision.verdict == MandateVerdict.NEEDS_APPROVAL:
                req = ApprovalRequest(
                    approval_id=new_id("appr"),
                    user_id=trip.user_id,
                    kind=ApprovalKind.OVER_THRESHOLD,
                    title=f"{item.name} needs an OK",
                    sku=item.sku,
                    amount_cents=line.total_cents,
                    payload={"reasons": [str(r) for r in decision.reasons]},
                )
                resp = await trips.ask(trip_id, req)
                if not resp.approved:
                    await trips.set_stop_status(trip_id, item.stop_id, "declined", "over threshold")
                    continue
                approval_id = req.approval_id
            await trips.add_line(trip_id, line, approval_id=approval_id)
            await trips.set_stop_status(trip_id, item.stop_id, "done", f"picked {item.qty}")
            await asyncio.sleep(0)
        goal = goal.model_copy(update={"status": self.outcome})
        self.goals[goal.goal_id] = goal
        return goal


class StubBrain:
    def __init__(self, services: Services) -> None:
        self.services = services

    async def run_goal(self, robot_id: str, goal: Goal) -> Goal:
        return goal.model_copy(update={"status": GoalStatus.COMPLETED})

    async def handle_message(self, robot_id: str, text: str, user_id: str | None = None) -> BrainReply:
        return BrainReply(robot_id=robot_id, text="hi")

    async def status(self, robot_id: str) -> BrainStatus:
        return BrainStatus(robot_id=robot_id)

    async def recent_steps(self, robot_id=None, goal_id=None, limit=50, before=None) -> list[BrainStep]:  # noqa: ANN001
        return [BrainStep(step_id="s1", robot_id=robot_id or "rover-ave-1", goal_id=goal_id, kind="thought", text="hm")]


def tiny_jpeg(color: tuple[int, int, int] = (200, 30, 30), w: int = 32, h: int = 24) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (w, h), color).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


class StubCameras:
    """Blurred tiny JPEGs; ``raw=True`` serves an UNBLURRED frame (the gateway must refuse it)."""

    def __init__(self, services: Services | None = None, raw: bool = False) -> None:
        self.raw = raw
        self.ids = [f"{rid}.{k}" for rid in ROBOTS for k in ("front", "wrist", "basket")] + ["store_ave.ceiling-1"]

    def _info(self, cid: str) -> CameraInfo:
        owner, kind = parse_camera_id(cid)
        robot = owner if kind != "ceiling" else None
        store = ROBOTS[owner][1] if robot else owner
        return CameraInfo(camera_id=cid, kind=kind, name=cid, store_id=store, robot_id=robot)

    async def list_cameras(self, store_id: str | None = None, robot_id: str | None = None) -> list[CameraInfo]:
        infos = [self._info(c) for c in self.ids]
        return [
            i
            for i in infos
            if (store_id is None or i.store_id == store_id) and (robot_id is None or i.robot_id == robot_id)
        ]

    async def get_camera(self, camera_id: str) -> CameraInfo:
        if camera_id not in self.ids:
            raise KeyError(camera_id)
        return self._info(camera_id)

    def _frame(self, camera_id: str) -> CameraFrame:
        info = self._info(camera_id)
        return CameraFrame(
            frame_id=new_id("frame"),
            camera_id=camera_id,
            robot_id=info.robot_id,
            store_id=info.store_id,
            jpeg_b64=tiny_jpeg(),
            w=32,
            h=24,
            blurred=not self.raw,
        )

    async def snapshot(self, camera_id: str, max_width: int | None = None) -> CameraFrame:
        await self.get_camera(camera_id)
        return self._frame(camera_id)

    async def stream(
        self, camera_id: str, fps: float = 5.0, max_width: int | None = None
    ) -> AsyncIterator[CameraFrame]:
        await self.get_camera(camera_id)
        while True:
            yield self._frame(camera_id)
            await asyncio.sleep(1.0 / max(fps, 0.1))

    async def observe(
        self, camera_id: str, question: str | None = None, *, goal_id: str | None = None
    ) -> VisionObservation:
        await self.get_camera(camera_id)
        return VisionObservation(observation_id=new_id("obs"), camera_id=camera_id, question=question, answer="shelves")
