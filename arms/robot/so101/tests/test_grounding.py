import pytest

from arms.robot.so101.grounding import Detection, parse_command


@pytest.mark.parametrize(
    ("text", "target", "dest"),
    [
        ("pick up the oat milk and put it in the basket", "oat milk", "basket"),
        ("Grab a red soda can.", "red soda can", "basket"),
        ("take the granola bar and place it into the blue bin", "granola bar", "blue bin"),
        ("pick the apple", "apple", "basket"),
        ("bananas", "bananas", "basket"),
    ],
)
def test_parse_command(text, target, dest):
    assert parse_command(text) == (target, dest)


def test_detection_center():
    assert Detection("x", (10, 20, 30, 60)).center_px == (20.0, 40.0)
