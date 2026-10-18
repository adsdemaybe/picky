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


def test_parse_point_boxes():
    from arms.robot.so101.vlm_server import parse_boxes

    text = (
        '<point_box mention="basket"> (681,512) (945,883) </point_box> and '
        '<point_box mention="cola">(91,538)(204,836)</point_box>'
    )
    assert parse_boxes(text) == [("basket", (681, 512, 945, 883)), ("cola", (91, 538, 204, 836))]


def test_to_pixels():
    from arms.robot.so101.grounding import _to_pixels

    assert _to_pixels("x", (500, 250, 1000, 1000), 640, 480).box_px == (320, 120, 640, 480)
