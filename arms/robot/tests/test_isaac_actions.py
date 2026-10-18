"""Pure-Python parts of the Isaac skeleton (no isaacsim needed)."""

import math

from arms.robot.isaac import (
    ActionStatus,
    LiftAction,
    MagicGraspAction,
    NavigateAction,
    PlaceAction,
    RidgebackFranka,
    RoverSO101,
    UnloadAction,
    basket_world,
)
from contracts.common import LIFT_MAX_M, LIFT_MIN_M
from contracts.sim import BASKET_OFFSET_LOCAL


def test_importing_isaac_package_does_not_need_isaacsim():
    rover = RoverSO101("/World/Stores/store_ave/Robots/rover-ave-1", robot_id="rover-ave-1")  # constructor is lazy
    assert rover.lift_m == LIFT_MIN_M and not rover.arm_loaded
    assert RidgebackFranka is RoverSO101  # v0.1 alias for arms/sim/app.py


def test_basket_world_matches_contract_offset():
    x, y, z = basket_world(1.0, 2.0, math.pi / 2)  # facing +y: the basket (x=-0.12 local) sits at -y
    assert (round(x, 6), round(y, 6), z) == (1.0, round(2.0 + BASKET_OFFSET_LOCAL[0], 6), BASKET_OFFSET_LOCAL[2])


def test_navigate_action_drives_kinematic_base():
    pose = [0.0, 0.0, 0.0]

    def set_pose(x, y, th):  # noqa: ANN001, ANN202
        pose[:] = [x, y, th]

    act = NavigateAction([(2.0, 0.0, math.pi / 2)], lambda: tuple(pose), set_pose)
    status = ActionStatus.RUNNING
    for _ in range(2000):
        status = act.step(1 / 60)
        if status != ActionStatus.RUNNING:
            break
    assert status == ActionStatus.SUCCEEDED
    assert math.hypot(pose[0] - 2, pose[1]) <= 0.05 and abs(pose[2] - math.pi / 2) <= 0.05


def test_navigate_action_times_out():
    act = NavigateAction([(50.0, 0.0, 0.0)], lambda: (0.0, 0.0, 0.0), lambda *a: None, timeout_s=0.1)
    statuses = [act.step(0.05) for _ in range(5)]
    assert statuses[-1] == ActionStatus.FAILED and act.error_code == "timeout"


def _run(action, n=500):  # noqa: ANN001, ANN202
    status = ActionStatus.RUNNING
    for _ in range(n):
        status = action.step(0.05)
        if status != ActionStatus.RUNNING:
            break
    return status


def test_magic_grasp_sequence_and_attach():
    hand: list[tuple] = []
    attached = []
    act = MagicGraspAction(
        item=(5.0, 4.05, 0.47),
        approach_theta=math.pi / 2,
        move_hand=hand.append,
        attach=lambda: attached.append(True) or True,
    )
    assert _run(act) == ActionStatus.SUCCEEDED
    assert attached == [True]
    assert any(abs(h[0] - 5.0) < 1e-6 and abs(h[1] - 4.05) < 1e-6 for h in hand)  # touched the item
    assert hand[-1][2] > 0.47  # lifted
    assert hand[-1][1] < 4.05  # retreated toward the robot (approach faces +y)


def test_magic_grasp_failure_is_grasp_failed():
    act = MagicGraspAction(item=(0, 0, 0.5), approach_theta=0.0, move_hand=lambda p: None, attach=lambda: False)
    assert _run(act) == ActionStatus.FAILED and act.error_code == "grasp_failed"


def test_place_action_releases_over_basket():
    hand: list[tuple] = []
    released = []
    act = PlaceAction(
        basket=(1.0, 1.0, 0.55),
        hand_start=(1.5, 1.0, 0.9),
        move_hand=hand.append,
        release=lambda: released.append(1) or True,
    )
    assert _run(act) == ActionStatus.SUCCEEDED
    assert released == [1] and hand[-1] == (1.0, 1.0, 0.55)


def test_lift_action_moves_at_lift_speed_and_clamps():
    seen: list[float] = []
    act = LiftAction(0.45, 5.0, seen.append)
    assert _run(act, 400) == ActionStatus.SUCCEEDED
    assert seen[-1] == LIFT_MAX_M
    assert all(b - a <= 0.25 * 0.05 + 1e-9 for a, b in zip(seen, seen[1:], strict=False))


def test_magic_grasp_raises_the_lift_first():
    lifts: list[float] = []
    hand: list[tuple] = []
    act = MagicGraspAction(
        item=(5.0, 4.05, 1.06),
        approach_theta=math.pi / 2,
        move_hand=hand.append,
        attach=lambda: True,
        set_lift=lifts.append,
        lift_from=0.45,
        lift_to=1.1,
    )
    assert act.phase == "lift"
    assert _run(act) == ActionStatus.SUCCEEDED
    assert lifts[-1] == 1.1 and hand  # lift done before the arm moved
    first_hand_step = len(lifts)
    assert first_hand_step > 1


def test_unload_action_removes_every_item():
    removed: list[int] = []
    act = UnloadAction(count=3, remove=lambda i: removed.append(i) or True)
    assert _run(act) == ActionStatus.SUCCEEDED and removed == [0, 1, 2]
    bad = UnloadAction(count=2, remove=lambda i: False)
    assert _run(bad) == ActionStatus.FAILED and bad.error_code == "internal"
