"""T1 policy grasps (``arms.robot.vla_t1``) and the Perceptron Mk1.5 tool policy (``arms.robot.vla_mk15``) without
Isaac. ``FakeArm`` plays the worker's ArmJointsCmd / ArmIkCmd semantics on a toy arm (TCP = base + q[:3] cm, gripper
= q[5], sticky grasp within 3 cm, policy frames every ``capture_every`` rows + after the last row); policies are a
scripted stub or Mk1.5 behind an ``httpx.MockTransport`` (no network, no key leaves the test)."""

import base64
import json
import math
from typing import Any

import httpx
import pytest

from arms.robot.t1 import MK15, NO_ARM_IK, ON_FAKE, T1Adapter, T1Config
from arms.robot.tests.conftest import R1
from arms.robot.vla_mk15 import (
    CallBudget,
    Mk15Client,
    Mk15Policy,
    first_tool_call,
    joint_rows,
    pixel_ray,
    project,
    ray_plane,
    shelf_plane,
)
from arms.robot.vla_t1 import PolicyGraspConfig, run_policy_grasp
from contracts.cameras import CameraMount
from contracts.common import Point3, Pose2D
from contracts.planogram import Slot
from contracts.sim import ArmJointState, CommandResult, IkSolution, IkTarget, ItemInstance, PolicyFrame, RobotState

UP = math.pi / 2
SLOT = Slot(
    slot_id="S-salsa",
    fixture_id="F1",
    sku="salsa-mild",
    item_position=Point3(x=3.0, y=4.0, z=0.4),
    approach_pose=Pose2D(x=3.0, y=3.62, theta=UP),
    qty=4,
)
UNIT = Point3(x=3.0, y=4.04, z=0.46)
"""Center of the front unit: 4 cm behind the shelf face, 6 cm above the board."""
SIDE = CameraMount(frame="world", x=3.25, y=3.55, z=0.78, yaw=UP + 0.35, pitch=-0.45, hfov_deg=72.0)
WRIST = CameraMount(frame="world", x=3.0, y=3.7, z=0.6, yaw=UP, pitch=-0.3, hfov_deg=78.0)
NO_HOME = PolicyGraspConfig(home_deg=None, timeout_s=20.0, max_chunks=6, exec_steps=25, capture_hz=4.0)


class FakeArm:
    """The robot-arm view of an Isaac worker with arm_joints + arm_ik, on a toy kinematic model."""

    def __init__(self, *, ik: bool = True, start_tcp: tuple[float, float, float] = (3.0, 3.80, 0.55)) -> None:
        self.base = (2.8, 3.6, 0.3)
        self.q = [*self.q_of(start_tcp), 0.0, 0.0, 35.0]
        self.attached: str | None = None
        self.calls: list[dict[str, Any]] = []
        self.ik_calls: list[list[IkTarget]] = []
        self.fed: list[PolicyFrame] = []
        self.ended = False
        self.n = 0
        self.last_side: list[bytes] = []
        """Per arm_joints call: the side frame after its last row (what the next predict must see)."""
        self.has_ik = ik

    def q_of(self, p: tuple[float, float, float]) -> list[float]:
        return [(p[i] - self.base[i]) * 100 for i in range(3)]

    def tcp(self) -> tuple[float, float, float]:
        return tuple(self.base[i] + self.q[i] / 100 for i in range(3))  # type: ignore[return-value]

    def supports_arm_joints(self, robot_id: str) -> bool:
        return True

    def supports_arm_ik(self, robot_id: str) -> bool:
        return self.has_ik

    async def list_instances(self, robot_id: str, slot_id: str) -> list[ItemInstance]:
        return [ItemInstance(instance_id=f"{slot_id}#00", sku="salsa-mild", slot_id=slot_id, position=UNIT)]

    async def stop(self, robot_id: str) -> None:
        return None

    def _frames(self, step: int, capture: Any) -> list[PolicyFrame]:
        self.n += 1
        out = []
        for cam in capture:
            data = base64.b64encode(f"{cam}-{self.n}".encode()).decode()
            pose = SIDE if cam == "side" else WRIST
            out.append(
                PolicyFrame(
                    camera=cam,
                    step=step,
                    jpeg_b64=data,
                    w=640,
                    h=480,
                    camera_pose=pose,
                    roll=0.0 if cam == "side" else 0.2,
                )
            )
        return out

    async def arm_joints(
        self,
        robot_id: str,
        targets: list[list[float]],
        *,
        hz: float = 30.0,
        lift_height_m: float | None = None,
        capture: Any = ("side", "wrist"),
        capture_every: int = 0,
        end: bool = False,
        exec_id: str | None = None,
        **_: Any,
    ) -> CommandResult:
        self.calls.append({"rows": len(targets), "capture_every": capture_every, "end": end, "lift": lift_height_m})
        events: list[str] = []
        frames: list[PolicyFrame] = []
        for i, row in enumerate(targets, 1):
            prev = self.q[5]
            self.q = [float(v) for v in row]
            if self.attached is None and prev >= 10.0 > self.q[5]:
                if math.dist(self.tcp(), (UNIT.x, UNIT.y, UNIT.z)) <= 0.03:
                    self.attached = f"{SLOT.slot_id}#00"
                    events.append(f"sticky_grasp:{self.attached}")
                else:
                    events.append("close_empty")
            if capture_every and i % capture_every == 0 and i < len(targets):
                frames += self._frames(i, capture)
        if capture:
            frames += self._frames(len(targets), capture)
        sides = [f for f in frames if f.camera == "side"]
        self.last_side.append(base64.b64decode(sides[-1].jpeg_b64) if sides else b"")
        t = self.tcp()
        return CommandResult(
            cmd_id=f"c{len(self.calls)}",
            ok=True,
            arm=ArmJointState(
                q_deg=list(self.q),
                joint_mode=not end,
                lift_height_m=0.46,
                tcp=Point3(x=t[0], y=t[1], z=t[2]),
                gripper_closed=self.q[5] < 10.0,
                attached=self.attached,
                steps_applied=len(targets),
                events=events,
            ),
            policy_frames=frames,
            state=RobotState(robot_id=robot_id, store_id="store_test", pose=SLOT.approach_pose),
        )

    async def arm_ik(self, robot_id: str, targets: list[Any], **_: Any) -> CommandResult:
        tg = [IkTarget.model_validate(t) for t in targets]
        self.ik_calls.append(tg)
        sols = []
        for t in tg:
            q = [
                *self.q_of((t.point.x, t.point.y, t.point.z)),
                0.0,
                t.wrist_roll_deg or 0.0,
                t.gripper_deg or self.q[5],
            ]
            sols.append(IkSolution(q_deg=q, error_m=0.002, ok=True, tcp=t.point))
        return CommandResult(cmd_id="ik", ok=True, ik=sols)

    def feed_policy_frames(self, robot_id: str, frames: list[PolicyFrame]) -> int:
        self.fed += frames
        return len(frames)

    def end_policy_feed(self, robot_id: str) -> None:
        self.ended = True


class StubPolicy:
    """Scripted chunks (reach the unit open, then close); records what it was fed."""

    def __init__(self, arm: FakeArm, *, close: bool = True, done_after: int | None = None) -> None:
        self.arm = arm
        self.close = close
        self.done_after = done_after
        self.seen: list[tuple[list[float], bytes, bytes, bool]] = []
        self.instruction = ""

    async def reset(self, instruction: str) -> None:
        self.instruction = instruction

    async def predict(self, state, side, wrist, *, frames_blurred, executed=None, delay=0):  # noqa: ANN001, ANN201
        from arms.robot.policy_backends import ActionChunk

        self.seen.append((list(state), side, wrist, frames_blurred))
        n = len(self.seen)
        at = [*self.arm.q_of((UNIT.x, UNIT.y, UNIT.z)), 0.0, 90.0, 40.0]
        if n == 1:
            rows = joint_rows(state, [at])
        elif self.close:
            rows = [[*at[:5], 40.0 - 45.0 * (i + 1) / 12] for i in range(12)]
        else:
            rows = [list(state)] * 12
        chunk = ActionChunk(actions_deg=rows, action_hz=30.0, backend="scripted", execute_count=len(rows))
        if self.done_after is not None and n >= self.done_after:
            chunk.done = True  # type: ignore[attr-defined]
        return chunk

    async def close(self) -> None:
        return None


# ---- the T1 loop ----------------------------------------------------------------------------------------------------
async def test_policy_grasp_takes_pictures_while_moving_and_feeds_fresh_frames():
    arm = FakeArm()
    policy = StubPolicy(arm)
    res = await run_policy_grasp(
        arm, R1, backend=policy, slot=SLOT, sku="salsa-mild", product_name="Mild Salsa", cfg=NO_HOME
    )
    assert res.ok and res.holding == "S-salsa#00" and res.on_target and res.chunks == 2, res.message
    assert policy.instruction == "pick up the mild salsa and put it in the basket"
    # every predict got the LAST frames of the previous command (closed loop, never a stale frame), blurred-flagged
    moving = [c for c in arm.calls if c["rows"] > 1]
    assert all(c["capture_every"] == 8 for c in moving)  # 30 rows/s / 4 pictures/s -> a picture every 8 rows
    observed = [s[1] for s in policy.seen]
    assert observed == [arm.last_side[0], arm.last_side[1]] and observed[0] != observed[1]
    assert all(s[3] for s in policy.seen)
    first_chunk = [f for f in arm.fed if f.camera == "side"][1:]  # after the observation of entering joint mode
    assert [f.step for f in first_chunk][:4] == [8, 16, 24, moving[0]["rows"]]  # mid-chunk pictures, then the last
    # pictures while the arm moved: more frames than observations, all on the live feed, the feed ended
    assert res.frames == len(arm.fed) and res.frames > 2 * (res.chunks + 1) and arm.ended
    assert arm.calls[-1]["end"]  # joint mode left (the scripted rig takes the arm back)


async def test_done_without_a_held_unit_is_grasp_missed():
    arm = FakeArm()
    res = await run_policy_grasp(
        arm,
        R1,
        backend=StubPolicy(arm, close=False, done_after=2),
        slot=SLOT,
        sku="salsa-mild",
        product_name="Mild Salsa",
        cfg=NO_HOME,
    )
    assert not res.ok and res.code == "grasp_missed" and "nothing is held" in res.message and arm.calls[-1]["end"]


# ---- Perceptron Mk1.5 tool policy -----------------------------------------------------------------------------------
class FakeMk15:
    """Mock Perceptron: answers with the tool calls in ``script`` (callables get the request body)."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.bodies: list[dict[str, Any]] = []
        self.headers: list[httpx.Headers] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        self.headers.append(request.headers)
        step = self.script.pop(0) if self.script else ("done", {"success": False})
        if callable(step):
            step = step(body)
        if isinstance(step, httpx.Response):
            return step
        name, args = step
        if name is None:
            msg = {"role": "assistant", "content": args}
        else:
            call = {"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
            msg = {"role": "assistant", "content": None, "tool_calls": [call]}
        return httpx.Response(200, json={"choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls"}]})

    def policy(self, budget: CallBudget | None = None) -> Mk15Policy:
        client = Mk15Client(
            key="test-key", base_url="https://mk15.test/v1", transport=httpx.MockTransport(self.handler)
        )
        return Mk15Policy(client=client, budget=budget or CallBudget(limit=50))


def side_point_of(p: Point3) -> dict[str, float]:
    x, y = project(SIDE, 0.0, (p.x, p.y, p.z))  # type: ignore[misc]
    return {"camera": "side", "x": round(x, 1), "y": round(y, 1)}


def test_pixel_rays_invert_the_projection_and_hit_the_planogram_shelf_face():
    for mount, roll in ((SIDE, 0.0), (WRIST, 0.2), (WRIST, -1.1)):
        p = (3.02, 4.03, 0.47)
        uv = project(mount, roll, p)
        assert uv is not None
        origin, d = pixel_ray(mount, roll, *uv)
        plane_pt, n = shelf_plane(SLOT)
        hit = ray_plane(origin, d, plane_pt, n)
        assert hit is not None and math.dist(hit, p) < 1e-6
    assert shelf_plane(SLOT)[0] == pytest.approx((3.0, 4.03, 0.4))
    rows = joint_rows([0.0] * 6, [[30.0] * 6, [35.0] * 6])
    assert len(rows) == 12 + 6 and rows[-1] == pytest.approx([35.0] * 6)
    assert len(joint_rows([0.0] * 6, [[200.0] * 6])) == 50  # long moves are compressed into one chunk


async def test_mk15_points_reaches_closes_and_the_sticky_grasp_holds_the_unit():
    target = Point3(x=3.0, y=4.03, z=0.46)  # what Mk1.5 points at: the unit's middle, on the side image
    mk = FakeMk15([("reach_point", side_point_of(target)), ("set_gripper", {"state": "close"})])
    arm = FakeArm()
    budget = CallBudget(limit=50)
    res = await run_policy_grasp(
        arm,
        R1,
        backend=mk.policy(budget),
        slot=SLOT,
        sku="salsa-mild",
        product_name="Mild Salsa",
        cfg=NO_HOME,
        budget_key="trip-9",
    )
    assert res.ok and res.holding == "S-salsa#00" and res.chunks == 2, res.message
    assert budget.used("trip-9") == 2
    # reach_point -> IK on a standoff 7 cm in front of the ray/shelf-face point, then the point itself, jaws open
    standoff, grasp = arm.ik_calls[0]
    assert math.dist((grasp.point.x, grasp.point.y, grasp.point.z), (3.0, 4.03, 0.46)) < 0.005
    assert grasp.point.y - standoff.point.y == pytest.approx(0.07) and grasp.gripper_deg == 40.0
    assert grasp.wrist_roll_deg == 90.0
    body = mk.bodies[0]
    assert (
        body["model"]
        and body["tool_choice"] == "auto"
        and {t["function"]["name"] for t in body["tools"]}
        >= {
            "reach_point",
            "move_ee",
            "set_gripper",
            "done",
        }
    )
    images = [c for c in body["messages"][1]["content"] if c["type"] == "image_url"]
    assert len(images) == 2 and images[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert mk.headers[0]["authorization"] == "Bearer test-key" and "picky" in mk.headers[0]["user-agent"]
    second = mk.bodies[1]["messages"][1]["content"][0]["text"]
    assert "reach_point" in second and "Mild Salsa".lower() in second  # the history goes back to the model


async def test_mk15_nudges_are_clamped_and_text_answers_are_retried_once():
    mk = FakeMk15(
        [
            (None, "I think the salsa is on the left."),
            ("move_ee", {"forward_cm": 40, "left_cm": -2, "up_cm": 1}),
            ("done", {"success": False, "note": "cannot see it"}),
        ]
    )
    arm = FakeArm()
    before = arm.tcp()
    res = await run_policy_grasp(
        arm, R1, backend=mk.policy(), slot=SLOT, sku="salsa-mild", product_name="Mild Salsa", cfg=NO_HOME
    )
    assert not res.ok and res.code == "grasp_missed" and "cannot see it" in res.message
    moved = arm.ik_calls[0][0].point
    assert moved.y - before[1] == pytest.approx(0.06)  # 40 cm forward clamped to 6 cm
    assert moved.x - before[0] == pytest.approx(0.02) and moved.z - before[2] == pytest.approx(0.01)
    assert "You must call one of the tools" in json.dumps(mk.bodies[1])


async def test_mk15_without_key_budget_or_ik_is_policy_unavailable():
    arm = FakeArm()
    nokey = Mk15Policy(client=Mk15Client(key=""), budget=CallBudget(limit=5))
    res = await run_policy_grasp(arm, R1, backend=nokey, slot=SLOT, sku="s", product_name="Salsa", cfg=NO_HOME)
    assert not res.ok and res.code == "policy_unavailable" and "PERCEPTRON_API_KEY" in res.message
    spent = FakeMk15([]).policy(CallBudget(limit=0))
    res = await run_policy_grasp(FakeArm(), R1, backend=spent, slot=SLOT, sku="s", product_name="Salsa", cfg=NO_HOME)
    assert res.code == "policy_unavailable" and "budget" in res.message
    no_ik = FakeMk15([("set_gripper", {"state": "open"})]).policy()
    res = await run_policy_grasp(
        FakeArm(ik=False), R1, backend=no_ik, slot=SLOT, sku="s", product_name="Salsa", cfg=NO_HOME
    )
    assert res.code == "policy_unavailable" and "arm_ik" in res.message
    auth = FakeMk15([httpx.Response(403, json={"error": "nope"})]).policy()
    res = await run_policy_grasp(FakeArm(), R1, backend=auth, slot=SLOT, sku="s", product_name="Salsa", cfg=NO_HOME)
    assert res.code == "policy_unavailable" and "403" in res.message


def test_tool_call_parsing():
    resp = {"choices": [{"message": {"tool_calls": [{"function": {"name": "lift", "arguments": '{"up_cm": 4}'}}]}}]}
    assert first_tool_call(resp) == ("lift", {"up_cm": 4})
    assert first_tool_call({"choices": [{"message": {"content": "hi"}}]}) is None
    assert first_tool_call({}) is None


# ---- tier selection -------------------------------------------------------------------------------------------------
def test_picky_vla_t1_perceptron_selects_the_mk15_policy_and_its_capability():
    from utils.config import reset_settings, set_settings

    try:
        set_settings(picky_vla_t1="perceptron")
        assert T1Config.from_settings().policy == MK15
        set_settings(picky_vla_t1="perceptron_vla")
        assert T1Config.from_settings().policy == "perceptron_vla"
        set_settings(picky_vla_t1="off", picky_vla_tiers="t1_sim")
        assert T1Config.from_settings().backend == "none"
        set_settings(picky_vla_t1="perceptron", perceptron_api_key="k-test")
        t1 = T1Adapter(T1Config.learned("perceptron"))
        assert t1.capability(R1, "fake", robot=FakeArm()).reason == ON_FAKE
        assert t1.capability(R1, "isaac", robot=FakeArm(ik=False)).reason == NO_ARM_IK
        cap = t1.capability(R1, "isaac", robot=FakeArm())
        assert cap.available and cap.step_kinds == ["grasp"] and cap.input_frames_blurred, cap.reason
    finally:
        reset_settings()


async def test_t1_adapter_builds_one_mk15_backend_per_robot():
    t1 = T1Adapter(T1Config.learned("perceptron"))
    a = await t1.policy_backend(R1)
    assert isinstance(a, Mk15Policy) and a is await t1.policy_backend(R1)
    await t1.close()


# ---- pictures while driving (T0 navigate, every backend) ------------------------------------------------------------
class NavEvidence:
    """Evidence stub: retained, blurred front pictures with the robot pose; get_frame serves them back."""

    def __init__(self, robot: Any) -> None:
        self.robot = robot
        self.calls: list[dict[str, Any]] = []
        self.frames: dict[str, Any] = {}

    async def capture_refs(self, robot_id, cameras, *, not_before=None, context=None, aim_slot_id=None):  # noqa: ANN001, ANN201
        from contracts.evidence import FrameRef
        from contracts.sim import CameraFrame

        self.calls.append({"cameras": list(cameras), "context": dict(context or {}), "aim": aim_slot_id})
        pose = (await self.robot.state(robot_id)).pose
        out = []
        for cam in cameras:
            fid = f"{robot_id}.{cam}:{len(self.calls)}"
            self.frames[fid] = CameraFrame(
                frame_id=fid, camera=cam, robot_id=robot_id, jpeg_b64="AA==", w=640, h=480, blurred=True, pose=pose
            )
            out.append(FrameRef(frame_id=fid, camera_id=f"{robot_id}.{cam}", robot_id=robot_id, pose=pose))
        return out

    async def get_frame(self, frame_id):  # noqa: ANN001, ANN201
        return self.frames[frame_id]


async def test_navigation_takes_front_pictures_while_driving_and_looks_out_on_the_approach(services, monkeypatch):
    from arms.robot.tests.test_vla import drain, nav, request, wire
    from utils.config import reset_settings, set_settings

    asked: list[tuple[bytes, str, str]] = []

    async def fake_look(jpeg: bytes, target: str, *, budget_key: str, **_: Any) -> str:
        asked.append((jpeg, target, budget_key))
        return "yes, on the bottom shelf in the middle"

    monkeypatch.setattr("arms.robot.vla_mk15.look", fake_look)
    try:
        set_settings(picky_vla_nav_capture_hz=10.0, picky_vla_nav_look="always", picky_vla_nav_look_m=50.0)
        wire(services, realtime_factor=1.0)
        services.evidence = NavEvidence(services.robot)  # type: ignore[assignment]
        terminal, seen = await drain(services.vla, request(nav()))
    finally:
        reset_settings()
    assert terminal.phase == "succeeded"
    pics = [s for s in seen if s.message.startswith("front camera picture")]
    assert len(pics) >= 3 and all(s.phase == "running" and s.pose is not None for s in pics)
    calls = services.evidence.calls
    assert all(c["cameras"] == ["front"] and c["context"]["purpose"] == "nav_picture" for c in calls)
    looks = [s for s in seen if s.stage == "observing"]
    assert 1 <= len(looks) <= 2 and "is mild salsa visible? yes" in looks[0].message
    assert asked[0][1] == "mild salsa" and asked[0][2] == "trip-1"


async def test_navigation_pictures_are_off_at_zero_hz_and_never_look_on_the_fake_by_default(services, monkeypatch):
    from arms.robot.tests.test_vla import drain, nav, request, wire
    from utils.config import reset_settings, set_settings

    async def boom(*a: Any, **k: Any) -> str:
        raise AssertionError("no look-out on the 2D fake by default")

    monkeypatch.setattr("arms.robot.vla_mk15.look", boom)
    wire(services, realtime_factor=1.0)
    services.evidence = NavEvidence(services.robot)  # type: ignore[assignment]
    terminal, seen = await drain(services.vla, request(nav()))
    assert terminal.phase == "succeeded" and services.evidence.calls  # default 2 Hz: pictures, no look-out
    try:
        set_settings(picky_vla_nav_capture_hz=0.0)
        services.evidence.calls.clear()
        terminal, _ = await drain(services.vla, request(nav("s09", goal=Pose2D(x=5.0, y=3.62, theta=UP))))
    finally:
        reset_settings()
    assert terminal.phase == "succeeded" and not services.evidence.calls
