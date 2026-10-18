import importlib.util

from arms.gateway.orchestrator import TripOrchestrator
from arms.gateway.registry import build_registry
from contracts.services import (
    EXTRA_SERVICE_PROTOCOLS,
    GATEWAY_PROTOCOLS,
    SERVICE_FACTORIES,
    SERVICE_PROTOCOLS,
    TripController,
)
from utils.config import reset_settings, set_settings


def test_all_fakes_by_default() -> None:
    reg = build_registry(real_arms=set())
    for arm, proto in SERVICE_PROTOCOLS.items():
        svc = getattr(reg.services, arm)
        assert reg.modes[arm] in ("fake", "missing"), (arm, reg.modes[arm], reg.notes.get(arm))
        if svc is not None:
            assert isinstance(svc, proto), arm
    for name, proto in GATEWAY_PROTOCOLS.items():
        assert isinstance(getattr(reg.services, name), proto), name
    assert isinstance(reg.orchestrator, TripOrchestrator)
    assert isinstance(reg.orchestrator, TripController)


def test_fake_opts_applied_as_attributes() -> None:
    reg = build_registry(real_arms=set(), fake_opts={"messaging": {"reply_delay_s": 0.123}})
    if reg.services.messaging is not None:
        assert getattr(reg.services.messaging, "reply_delay_s", 0.123) == 0.123


def test_extra_services_are_wired_and_overridable() -> None:
    """cameras (perception) and brain (agent) come from SERVICE_FACTORIES; overrides replace any service."""
    reg = build_registry(real_arms=set(), overrides={"fleet": lambda s: "FLEET"})
    for name in SERVICE_FACTORIES:
        assert name in reg.modes, name
    assert reg.services.fleet == "FLEET" and reg.modes["fleet"] == "override"
    for name, proto in EXTRA_SERVICE_PROTOCOLS.items():
        svc = getattr(reg.services, name)
        if svc is not None:
            assert isinstance(svc, proto), name
    assert reg.approvals.envelope_hook is not None


def test_real_brain_alone_is_selectable() -> None:
    """PICKY_REAL_ARMS=brain asks for the real robot brain only (the shopper agent stays fake)."""
    reg = build_registry(real_arms={"brain"})
    assert reg.modes["agent"] == "fake"
    assert reg.modes["brain"] in ("real", "fake (real failed)", "missing"), reg.modes


def test_real_arm_without_prereqs_falls_back_to_fake() -> None:
    """PICKY_REAL_ARMS=<arm> without the extra/keys degrades at boot (visible in /health), not mid-trip."""
    set_settings(photon_api_key="", photon_project_id="")
    try:
        reg = build_registry(real_arms={"catalog", "messaging"})
    finally:
        reset_settings()
    assert reg.modes["messaging"] == "fake (real failed)", reg.notes
    if importlib.util.find_spec("sqlalchemy") is None:
        assert reg.modes["catalog"] == "fake (real failed)", reg.notes
        assert "extra catalog" in reg.notes["catalog"]
