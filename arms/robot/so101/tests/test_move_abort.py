import pytest

from arms.robot.so101 import pick_place


class FakeRobot:
    def __init__(self):
        self.sent = []

    def get_observation(self):
        return {f"{j}.pos": 0.0 for j in (*pick_place.ARM_JOINTS, "gripper")}

    def send_action(self, a):
        self.sent.append(a)


def test_label_mid_move_stops_it(monkeypatch):
    robot, calls = FakeRobot(), {"n": 0}

    def check():
        calls["n"] += 1
        return calls["n"] > 3  # "s" typed after 3 interpolation steps

    monkeypatch.setattr(pick_place, "ABORT_CHECK", check)
    with pytest.raises(pick_place.Interrupted):
        pick_place.move(robot, dict.fromkeys(pick_place.ARM_JOINTS, 10.0), 50.0, seconds=0.01, steps=30)
    assert len(robot.sent) == 3


def test_no_check_runs_the_whole_move():
    robot = FakeRobot()
    pick_place.move(robot, dict.fromkeys(pick_place.ARM_JOINTS, 10.0), 50.0, seconds=0.01, steps=30)
    assert len(robot.sent) == 30
