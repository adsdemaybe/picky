import logging

from utils.log import arm_of, get_logger


def test_arm_of():
    assert arm_of("arms.trust.service") == "trust"
    assert arm_of("utils.events") == "utils"
    assert arm_of("__main__") == "__main__"


def test_level_and_format(monkeypatch, capsys):
    monkeypatch.setenv("PICKY_LOG_LEVEL", "debug")
    log = get_logger("arms.routing.service")
    assert log.level == logging.DEBUG
    log.debug("hello %s", "world")
    err = capsys.readouterr().err
    assert "[routing] arms.routing.service: hello world" in err
    # idempotent handler install
    get_logger("x")
    assert sum(getattr(h, "name", None) == "picky" for h in logging.getLogger().handlers) == 1


def test_bad_level_defaults_info(monkeypatch):
    monkeypatch.setenv("PICKY_LOG_LEVEL", "nope")
    assert get_logger("arms.x").level == logging.INFO
