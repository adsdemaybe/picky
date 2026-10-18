import numpy as np

from arms.robot.so101 import closed_loop
from arms.robot.so101.judge import Verdict, parse
from arms.robot.so101.pick_routine import GRIP_CLOSED, GRIP_OPEN, ScenePlan, Waypoint

NAMES = closed_loop.GRASP + closed_loop.CARRY + closed_loop.PLACE


def fake_plan(*args, **kwargs):
    wps = [Waypoint(n, None if n in ("close", "release") else (0.3, 0.0, 0.1), GRIP_CLOSED) for n in NAMES]
    return ScenePlan("gum", "plate", (0.3, -0.1), (0.3, 0.15), 0.0, 0.0, wps)


def setup(monkeypatch, verdicts):
    """verdicts: answers the judge gives, in order (True / False / None)."""
    ran, answers = [], list(verdicts)
    monkeypatch.setattr(closed_loop, "plan", fake_plan)
    monkeypatch.setattr(closed_loop, "grab_frame", lambda cfg: np.zeros((4, 4, 3), np.uint8))
    monkeypatch.setattr(
        closed_loop, "execute", lambda robot, kin, p, **k: ran.extend(w.name for w in p.waypoints) or []
    )
    monkeypatch.setattr(closed_loop, "go_home", lambda robot, home, g: ran.append(f"home:{g}"))
    monkeypatch.setattr(closed_loop, "detect_named", lambda *a: None)  # destination re-detection: keep the plan
    monkeypatch.setattr(closed_loop.time, "sleep", lambda s: None)

    def ask(frame, what, target, dest, model, effort="low"):
        a = answers.pop(0)
        return Verdict(a, 0.95 if a is not None else 0.0, f"{what}")

    monkeypatch.setattr(closed_loop, "ask", ask)
    return ran, answers


class Cfg:
    perceptron_model = "m"


def run(monkeypatch, verdicts, max_attempts=3):
    ran, answers = setup(monkeypatch, verdicts)
    attempts, learned = [], []
    closed_loop.run(
        None,
        None,
        None,
        Cfg(),
        "pick up the gum and put it on the plate",
        {},
        0.05,
        lambda: ({"grasp": "base", "drop": "base"}, {}),
        attempts,
        learn=learned.append,
        log=lambda m: None,
        max_attempts=max_attempts,
    )
    return ran, answers, attempts, learned


def test_clean_run_checks_each_phase(monkeypatch):
    ran, answers, attempts, learned = run(monkeypatch, [True, True, True])
    assert len(attempts) == 1 and not learned and not answers
    a = attempts[0]
    assert (a.held, a.placed) == (True, True)
    assert ran == [*NAMES, f"home:{GRIP_OPEN}"]


def test_missed_grasp_is_retried_without_carrying(monkeypatch):
    # attempt 1: "no" twice at the lift (confirmed miss); attempt 2: all good
    ran, answers, attempts, learned = run(monkeypatch, [False, False, True, True, True])
    assert len(attempts) == 2 and learned == [attempts[0]]
    assert attempts[0].held is False and attempts[0].note == "missed the grasp"
    first = ran[: ran.index(f"home:{GRIP_OPEN}")]
    assert "transit" not in first  # never carried an empty gripper
    assert attempts[1].placed is True


def test_one_no_without_a_second_no_does_not_abort(monkeypatch):
    ran, answers, attempts, learned = run(monkeypatch, [False, True, True, True])
    assert len(attempts) == 1 and attempts[0].placed is True


def test_unsure_never_aborts(monkeypatch):
    ran, answers, attempts, learned = run(monkeypatch, [None, None, True])
    assert len(attempts) == 1 and attempts[0].held is None and attempts[0].placed is True


def test_drop_during_carry_then_gives_up_after_max_attempts(monkeypatch):
    ran, answers, attempts, learned = run(monkeypatch, [True, False, False] * 2, max_attempts=2)
    assert len(attempts) == 2 and all(a.note == "dropped it during the carry" for a in attempts)
    assert len(learned) == 1  # the last attempt is left for the caller to score


def test_parse_judge_replies():
    assert parse('{"answer": "yes", "confidence": 0.9, "reason": "held"}').answer is True
    assert parse('blah {"answer": "no", "confidence": 1, "reason": "on table"} blah').answer is False
    assert parse('{"answer": "unsure", "confidence": 0.2, "reason": "hidden"}').answer is None
    assert parse("No, it is on the table.").answer is False
    assert parse("").answer is None
