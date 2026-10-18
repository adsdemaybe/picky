"""Tiny explicit state machine: an allowed-transitions table, ``transition()`` raising on illegal moves, history.

States can be strings or (str) Enums::

    sm = StateMachine({"a": ["b"], "b": ["c"]}, initial="a", any_to=["failed"], terminal=["c", "failed"])
    sm.transition("b", reason="ok")
"""

from __future__ import annotations

import time
from collections.abc import Callable, Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


class IllegalTransition(ValueError):
    def __init__(self, src: Any, dst: Any, allowed: Iterable[Any]) -> None:
        self.src, self.dst, self.allowed = src, dst, sorted(map(_name, allowed))
        super().__init__(f"illegal transition {_name(src)} -> {_name(dst)} (allowed: {self.allowed or 'none'})")


def _name(s: Any) -> str:
    return str(getattr(s, "value", s))


@dataclass(frozen=True)
class Transition:
    src: Any
    dst: Any
    ts: float
    reason: str | None = None


@dataclass
class StateMachine:
    transitions: Mapping[Hashable, Iterable[Hashable]]
    initial: Hashable
    any_to: Iterable[Hashable] = ()
    terminal: Iterable[Hashable] = ()
    on_transition: Callable[[Transition], None] | None = None
    state: Any = field(init=False)
    history: list[Transition] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        self.transitions = {k: frozenset(v) for k, v in self.transitions.items()}
        self.any_to = frozenset(self.any_to)
        self.terminal = frozenset(self.terminal)
        self.state = self.initial

    def allowed(self, src: Hashable | None = None) -> frozenset[Any]:
        src = self.state if src is None else src
        if src in self.terminal:
            return frozenset()
        return frozenset(self.transitions.get(src, frozenset())) | self.any_to

    def can(self, dst: Hashable) -> bool:
        return dst in self.allowed()

    @property
    def is_terminal(self) -> bool:
        return self.state in self.terminal

    def transition(self, dst: Hashable, reason: str | None = None) -> Transition:
        if not self.can(dst):
            raise IllegalTransition(self.state, dst, self.allowed())
        t = Transition(self.state, dst, time.time(), reason)
        self.state = dst
        self.history.append(t)
        if self.on_transition is not None:
            self.on_transition(t)
        return t

    def path(self) -> list[Any]:
        """States visited, starting at ``initial``."""
        return [self.initial, *(t.dst for t in self.history)]
