"""Pure parts of the supermarket demo's Perceptron VLA loop (demo/vla_policy.py): response parsing, the tool call ->
Cartesian action mapping (pixel ray -> shelf face from the store map, clamps), the live policy's guards, and the
action -> IK -> joints path on the rover's real SO-101 chain. Base deps only (numpy); no Isaac, no network."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

DEMO = Path(__file__).resolve().parents[1] / "demo"
ROBOT = Path(__file__).resolve().parents[1] / "robot"
sys.path.insert(0, str(DEMO))
sys.path.insert(0, str(ROBOT))

import vla_policy as vp  # noqa: E402


def _cam(origin=(0.0, 0.0, 1.0), fwd=(1.0, 0.0, 0.0), hfov=90.0, w=640, h=480) -> vp.Cam:
    """A level camera at origin looking along fwd (horizontal)."""
    f = np.asarray(fwd, float) / np.linalg.norm(fwd)
    right = np.cross(f, [0.0, 0.0, 1.0])
    up = np.cross(right, f)
    M = np.eye(4)
    M[:3, 0], M[:3, 1], M[:3, 2], M[:3, 3] = right, up, -f, origin
    return vp.Cam.from_matrix(M, hfov, w, h)


def _slot() -> vp.Slot:
    # shelf face at x = 1.0 (the shelf is at +x), normal -x towards the aisle; board at z 0.9; a 20 cm tall product
    return vp.Slot(face=(1.0, 0.0, 1.0), normal=(-1.0, 0.0, 0.0), board_z=0.9, height=0.2, width=0.05)


def test_pixel_ray_and_project_are_inverse():
    cam = _cam()
    for p in ([1.0, 0.0, 1.0], [1.2, -0.4, 1.3], [0.8, 0.3, 0.7]):
        uv = vp.project(cam, p)
        assert uv is not None
        o, d = vp.pixel_ray(cam, *uv)
        t = (p[0] - o[0]) / d[0]
        assert np.allclose(o + t * d, p, atol=1e-9)
    assert vp.project(cam, [-1.0, 0.0, 1.0]) is None  # behind the camera
    assert vp.project(cam, [1.0, 0.0, 1.0]) == pytest.approx((500.0, 500.0))


def test_cam_from_usd_matrix_axes():
    cam = _cam(fwd=(0.0, -1.0, 0.0))
    assert np.allclose(cam.fwd, (0.0, -1.0, 0.0))
    assert np.allclose(cam.up, (0.0, 0.0, 1.0))
    assert np.allclose(cam.right, (-1.0, 0.0, 0.0))  # looking -y, +x is on the left
    assert vp.hfov_deg(4.0, 20.955) == pytest.approx(138.2, abs=0.1)  # the SO-101 wrist camera


def test_reach_point_hits_the_shelf_face_from_the_map():
    cam = _cam()
    slot = _slot()
    uv = vp.project(cam, [1.0, 0.05, 1.0])  # the product's face, 5 cm to the left of the slot centre... in +y
    a = vp.map_action("reach_point", {"camera": "mast", "x": uv[0], "y": uv[1]}, {"mast": cam}, slot, (0.7, 0, 1))
    assert a.kind == "reach" and not a.clipped
    assert np.allclose(a.point, [1.0, 0.05, 1.0], atol=1e-6)


def test_reach_point_clamps_height_and_along():
    cam = _cam()
    slot = _slot()
    hi = vp.project(cam, [1.0, 0.0, 1.35])  # above the product (0.9 + 0.85 * 0.2 = 1.07 max)
    a = vp.map_action("reach_point", {"camera": "mast", "x": hi[0], "y": hi[1]}, {"mast": cam}, slot, None)
    assert a.kind == "reach" and a.point[2] == pytest.approx(0.9 + 0.85 * 0.2)
    assert a.clipped and "height" in a.clipped[0]
    far = vp.project(cam, [1.0, 0.6, 1.0])  # 60 cm along the shelf: clamped to the slot extent
    a = vp.map_action("reach_point", {"camera": "mast", "x": far[0], "y": far[1]}, {"mast": cam}, slot, None)
    assert a.point[1] == pytest.approx(vp.ALONG_MAX_M)


def test_reach_point_rejects_bad_calls():
    slot = _slot()
    cam = _cam(fwd=(-1.0, 0.0, 0.0))  # looking away from the shelf: the ray never hits its face
    a = vp.map_action("reach_point", {"camera": "mast", "x": 500, "y": 500}, {"mast": cam}, slot, None)
    assert a.kind == "noop" and "does not hit" in a.note
    a = vp.map_action("reach_point", {"camera": "wrist", "x": 500, "y": 500}, {"mast": _cam()}, slot, None)
    assert a.kind == "noop" and "no wrist camera" in a.note
    a = vp.map_action("reach_point", {"camera": "mast", "x": 500, "y": 500}, {"mast": _cam()}, slot, (5.0, 5.0, 1))
    assert a.kind == "noop" and "away" in a.note
    assert vp.map_action("wave", {}, {}, slot, None).kind == "noop"


def test_move_ee_lift_gripper_done():
    slot = _slot()
    a = vp.map_action("move_ee", {"forward_cm": 9, "left_cm": 3, "up_cm": "x"}, {}, slot, None)
    assert np.allclose(a.delta, [0.06, 0.03, 0.0])  # forward = into the shelf (+x), clamped to 6 cm; left = +y
    a = vp.map_action("lift", {"up_cm": 25}, {}, slot, None)
    assert a.kind == "lift" and np.allclose(a.delta, [0, 0, 0.10])
    assert vp.map_action("set_gripper", {"state": "close"}, {}, slot, None).close is True
    assert vp.map_action("set_gripper", {"state": "open"}, {}, slot, None).close is False
    d = vp.map_action("done", {"success": True, "note": "held"}, {}, slot, None)
    assert d.kind == "done" and d.success and "success" in d.note


def test_parse_tool_call_and_text():
    resp = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"function": {"name": "reach_point", "arguments": json.dumps({"camera": "wrist", "x": 512})}}
                    ],
                }
            }
        ]
    }
    assert vp.first_tool_call(resp) == ("reach_point", {"camera": "wrist", "x": 512})
    bad = {"choices": [{"message": {"tool_calls": [{"function": {"name": "lift", "arguments": "{not json"}}]}}]}
    assert vp.first_tool_call(bad) == ("lift", {})
    assert vp.first_tool_call({"choices": [{"message": {"content": "I would reach"}}]}) is None
    assert vp.first_tool_call({}) is None
    text = vp.answer_text({"choices": [{"message": {"content": " Yes, between the jaws. "}}]})
    assert text == "Yes, between the jaws."
    assert vp.yes_no("Yes, it is held") is True
    assert vp.yes_no("no - the basket is empty") is False
    assert vp.yes_no("<point> (1,2) </point>") is None
    assert vp.short_call("reach_point", {"camera": "wrist", "x": 512.2, "y": 430}) == "reach (wrist 512,430)"
    assert vp.short_call("move_ee", {"forward_cm": 1, "left_cm": -2, "up_cm": 0}) == "nudge +1/-2/+0 cm"


def test_session_guards_and_history():
    s = vp.Session("pick up the soap and put it in the basket")
    slot = _slot()
    close = vp.Action("gripper", close=True)
    s.record("set_gripper", {"state": "close"}, vp.CLOSED)
    s.resolve_close(held=False)
    assert s.history[-1].endswith(vp.MISSED)
    # open jaws at the same spot after a miss: the close is refused until it re-aims
    assert s.guard(close, gripper_open=True, held=False).kind == "noop"
    # closed + empty: a second close opens the jaws instead
    g = s.guard(close, gripper_open=False, held=False)
    assert g.kind == "gripper" and g.close is False
    s.record("reach_point", {"camera": "wrist", "x": 500, "y": 400}, "moved the open jaws to the point")
    assert not s.missed_here()
    assert s.guard(close, gripper_open=True, held=False) is close
    text = s.user_text(s.state_text(True, False, (0.8, 0.0, 1.0), slot))
    assert "Task: pick up the soap" in text and "20 cm in front" in text and "reach_point" in text


def test_messages_shape():
    m = vp.messages("hi", ["AAA", "BBB"])
    assert m[0]["role"] == "system" and m[1]["content"][0] == {"type": "text", "text": "hi"}
    assert m[1]["content"][2]["image_url"]["url"].startswith("data:image/jpeg;base64,BBB")
    names = [t["function"]["name"] for t in vp.TOOLS]
    assert names == ["reach_point", "move_ee", "set_gripper", "lift", "done"]


def test_reach_action_to_joints_on_the_so101_chain():
    """action -> IK -> joints: a reach Action's point becomes grasp + pre-grasp TCPs (vla_policy.grasp_targets), both
    solvable on the rover's real SO-101 chain (robot/so101_ik.py + the built robot's kin json), and forward kinematics
    of the joint solution puts the jaw centre on the pointed product."""
    so101_ik = pytest.importorskip("so101_ik")
    kin = so101_ik.RoverKinematics(Path(__file__).parent / "fixtures" / "picky_so101_rover.kin.json")
    pan_x = float(kin.k["pan_axis_xy"][0])
    # rover at (2, 3) facing +x; a shelf on its right (-y): the product's aisle face 0.48 m out, 1.0 m up
    Tb = np.eye(4)
    Tb[:2, 3] = (2.0, 3.0)
    normal = np.array([0.0, 1.0, 0.0])  # out of the shelf, towards the rover
    cam = _cam(origin=(2.0 + pan_x, 3.0, 1.0), fwd=(0.0, -1.0, 0.0))
    slot = vp.Slot(face=(2.0 + pan_x, 2.52, 0.98), normal=tuple(normal), board_z=0.9, height=0.18, width=0.05)
    uv = vp.project(cam, [2.0 + pan_x + 0.01, 2.52, 1.0])
    act = vp.map_action("reach_point", {"camera": "wrist", "x": uv[0], "y": uv[1]}, {"wrist": cam}, slot, None)
    assert act.kind == "reach"
    a_b = np.array([0.0, -1.0, 0.0])  # approach into the shelf, base frame (yaw 0)
    R = kin.target_rotation(a_b, kin.grasp_roll)
    x_mid = -0.02
    tcp, far = vp.grasp_targets(act.point, normal, Tb, R, a_b, x_mid, 0.03, 0.12)
    assert np.allclose(far, tcp - a_b * 0.12)
    jaw_b = tcp + R[:, 0] * x_mid
    assert np.allclose(jaw_b + [2.0, 3.0, 0.0], act.point - normal * 0.03, atol=1e-9)
    so101_demo = pytest.importorskip("so101_demo")  # ik_natural: the IK vla_pick.VlaRig.execute uses
    q, (pe, ae) = so101_demo.ik_natural(kin, tcp, R, roll=kin.grasp_roll)
    assert pe < 0.003 and ae < math.radians(3)
    qf, (pf, _) = kin.solve(far, R=R, w_rot=0.1, seeds=[q])  # as vla_pick: the pre-grasp within 8 mm
    assert pf < 0.008
    T = kin.fk(q)
    assert np.allclose(T[:3, 3] + T[:3, 0] * x_mid + [2.0, 3.0, 0.0], act.point - normal * 0.03, atol=0.004)


def test_detect_boxes_and_the_held_rule():
    """Measured Mk1.5 detect answers on this rover's wrist camera: held after the retract vs the shelf unit far away."""
    held = '<point_box mention="vitamin e lotion" asset_idx="0"> (512,252) (688,850) </point_box>'
    far = (
        '<collection mention="vitamin e lotion" asset_idx="0">\n <point_box> (488,639) (506,732) </point_box>\n'
        " <point_box> (511,642) (531,733) </point_box>\n</collection>"
    )
    assert vp.parse_boxes(held) == [(512, 252, 688, 850)]
    assert vp.parse_boxes(far) == [(488, 639, 506, 732), (511, 642, 531, 733)]
    assert vp.parse_boxes("no boxes here") == []
    ok, box, iou = vp.held_box(vp.parse_boxes(held))
    assert ok and box == (512, 252, 688, 850) and iou > 0.3
    ok, box, iou = vp.held_box(vp.parse_boxes(far))
    assert not ok and box is not None and iou < 0.05
    assert vp.held_box([]) == (False, None, 0.0)
    assert vp.box_iou((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(1 / 3)
