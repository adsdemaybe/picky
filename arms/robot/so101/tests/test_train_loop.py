import numpy as np
import pytest

pytest.importorskip("cv2")  # the rig modules import OpenCV (robot venv); base deps skip

from arms.robot.so101.train_loop import DROP_ACTIONS, GRASP_ACTIONS, QPolicy


def test_starts_greedy_on_the_base_routine():
    p = QPolicy()
    assert p.greedy() == {"grasp": "base", "drop": "base"}
    assert all(v == 0.0 for v in p.steer(p.greedy()).values())


def test_td_updates():
    p = QPolicy()
    qd, qg = p.q_drop["x+2cm"], p.q_grasp["deeper"]
    p.update({"grasp": "deeper", "drop": "x+2cm"}, success=True)
    new_qd = qd + p.alpha * (1.0 - qd)
    assert p.q_drop["x+2cm"] == pytest.approx(new_qd)
    assert p.q_grasp["deeper"] == pytest.approx(qg + p.alpha * (p.gamma * new_qd - qg))
    assert (p.episodes, p.successes) == (1, 1)


def test_repeated_failures_switch_away_from_base():
    p = QPolicy()
    for _ in range(4):
        p.update({"grasp": "base", "drop": "base"}, success=False)
    assert p.greedy()["grasp"] != "base" and p.greedy()["drop"] != "base"


def test_missed_grasp_only_blames_the_grasp():
    p = QPolicy()
    drop_before = dict(p.q_drop)
    for _ in range(3):
        p.learn({"grasp": "base", "drop": "base"}, held=False, placed=None)
    assert p.greedy()["grasp"] != "base" and p.q_drop == drop_before


def test_held_but_missed_the_plate_only_blames_the_drop():
    p = QPolicy()
    for _ in range(4):
        p.learn({"grasp": "base", "drop": "base"}, held=True, placed=False)
    assert p.greedy() == {"grasp": "base", "drop": p.greedy()["drop"]} and p.greedy()["drop"] != "base"


def test_successes_make_a_tweak_the_greedy_choice():
    p = QPolicy()
    for _ in range(3):
        p.update({"grasp": "roll+15", "drop": "base"}, success=True)
    assert p.greedy() == {"grasp": "roll+15", "drop": "base"}
    assert p.steer(p.greedy())["roll"] == 15.0


def test_act_only_returns_known_actions():
    p, rng = QPolicy(epsilon=1.0), np.random.default_rng(0)
    for _ in range(30):
        a = p.act(rng)
        assert a["grasp"] in GRASP_ACTIONS and a["drop"] in DROP_ACTIONS
