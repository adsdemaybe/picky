import enum

import pytest

from utils.fsm import IllegalTransition, StateMachine


class S(enum.StrEnum):
    A = "a"
    B = "b"
    C = "c"
    FAILED = "failed"


def make(**kw):
    return StateMachine({S.A: [S.B], S.B: [S.C, S.A]}, initial=S.A, any_to=[S.FAILED], terminal=[S.C, S.FAILED], **kw)


def test_happy_path_and_history():
    seen = []
    sm = make(on_transition=seen.append)
    sm.transition(S.B, reason="go")
    sm.transition(S.A)
    sm.transition(S.B)
    sm.transition(S.C)
    assert sm.state is S.C and sm.is_terminal
    assert sm.path() == [S.A, S.B, S.A, S.B, S.C]
    assert sm.history[0].reason == "go" and len(seen) == 4


def test_illegal():
    sm = make()
    with pytest.raises(IllegalTransition, match="a -> c"):
        sm.transition(S.C)
    assert sm.state is S.A and sm.history == []


def test_any_to_and_terminal():
    sm = make()
    assert sm.can(S.FAILED)
    sm.transition(S.FAILED)
    assert sm.allowed() == frozenset()
    with pytest.raises(IllegalTransition):
        sm.transition(S.FAILED)


def test_plain_strings():
    sm = StateMachine({"x": ["y"]}, initial="x")
    sm.transition("y")
    assert sm.state == "y" and not sm.can("x")
