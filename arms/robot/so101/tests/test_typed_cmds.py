import pytest

from arms.robot.so101.typed_cmds import parse_typed

TIP = [0.22, -0.10, 0.06]


def test_relative_moves_in_cm():
    assert parse_typed("up 2", TIP, 120.0)[0]["xyz"] == pytest.approx([0.22, -0.10, 0.08])
    assert parse_typed("left 1.5", TIP, 120.0)[0]["xyz"] == pytest.approx([0.22, -0.085, 0.06])
    assert parse_typed("forward", TIP, 120.0)[0]["xyz"] == pytest.approx([0.23, -0.10, 0.06])


def test_absolute_and_gripper():
    cmd = parse_typed("xyz 0.2 -0.05 0.04 90", TIP, 120.0)[0]
    assert cmd["xyz"] == [0.2, -0.05, 0.04] and cmd["roll"] == 90.0
    assert parse_typed("close", TIP, 0)[0]["gripper"] == 5.0
    assert parse_typed("grip 30", TIP, 0)[0]["gripper"] == 30.0


def test_pick_macro_straddles_the_top():
    steps = parse_typed("pick 0.225 -0.10 0.05", TIP, 120.0)
    zs = [s["xyz"][2] for s in steps if "xyz" in s]
    assert zs == pytest.approx([0.06, 0.035, 0.065])  # hover above, descend below the top, lift
    assert any(s.get("gripper") == 5 for s in steps)


def test_unknown_command():
    with pytest.raises(ValueError):
        parse_typed("jump", TIP, 0)
