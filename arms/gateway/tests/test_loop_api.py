"""Planner -> VLA -> verifier loop through the gateway (contracts 0.2.1): registry wiring + exec mode, the loop REST
routes over the real arms' fakes, the evidence route's blur rule, the annotation index and the dev-only fault routes.
No network: every loop service is a deterministic fake (TestClient runs the lifespan)."""

import base64
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from arms.gateway.app import create_app
from arms.gateway.loop_api import EvidenceAnnotations
from arms.gateway.orchestrator import OrchestratorConfig
from arms.gateway.registry import NOT_WIRED, build_registry
from arms.gateway.tests.helpers import FAST_FAKES, needs_data
from contracts.events import make_event
from contracts.evidence import FrameRef
from contracts.grounding import Evidence, GroundedTarget, NormBox, NormPoint, TargetSpec
from contracts.services import LOOP_SERVICE_FACTORIES, LOOP_SERVICE_PROTOCOLS
from contracts.sim import CameraFrame
from contracts.verify import AtomVerdict, Verdict
from utils.config import reset_settings, set_settings

TERMINAL = ("completed", "failed", "cancelled")


@pytest.fixture(autouse=True)
def _settings():
    set_settings(picky_exec_mode="", picky_faults="", picky_dev=False, gemini_api_key="", anthropic_api_key="")
    yield
    reset_settings()


def _client(fake_opts: dict[str, Any] | None = None, **kw: Any) -> TestClient:
    reg = build_registry(
        real_arms=set(),
        fake_opts=fake_opts or FAST_FAKES,
        orchestrator_config=OrchestratorConfig(approval_channels=("web",)),
        **kw,
    )
    c = TestClient(create_app(registry=reg))
    c.registry = reg  # type: ignore[attr-defined]
    return c


def _answer_pending(c: TestClient) -> None:
    for req in c.get("/api/approvals").json():
        body: dict[str, Any] = {"decision": "approve", "channel": "web"}
        if req["options"]:
            body = {"decision": "choice", "choice": req["options"][0]["option_id"], "channel": "web"}
        if req["kind"] == "checkout":
            body["cart_hash"] = req["cart_hash"]
        c.post(f"/api/approvals/{req['approval_id']}", json=body)


def _run_dev_plan(c: TestClient, text: str, robot_id: str = "rover-ave-2", timeout_s: float = 30.0) -> dict:
    plan = c.post("/api/dev/plans", json={"robot_id": robot_id, "text": text, "store_id": "store_ave"})
    assert plan.status_code == 200, plan.text
    pid = plan.json()["plan_id"]
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = c.get(f"/api/plans/{pid}/run").json()
        if run["status"] in TERMINAL:
            return {"plan": plan.json(), "run": run}
        _answer_pending(c)
        time.sleep(0.05)
    raise AssertionError(f"dev plan {pid} did not finish: {c.get(f'/api/plans/{pid}/run').json()['status']}")


# ---- registry --------------------------------------------------------------------------------------------------------
def test_loop_services_are_wired_as_fakes_and_plan_mode_is_the_default() -> None:
    reg = build_registry(real_arms=set())
    for name, proto in LOOP_SERVICE_PROTOCOLS.items():
        svc = getattr(reg.services, name)
        assert reg.modes[name] == "fake", (name, reg.modes[name], reg.notes.get(name))
        assert isinstance(svc, proto), name
    assert reg.exec_mode == "plan"  # PICKY_EXEC_MODE blank = plan once the VLA + verifier are wired
    set_settings(picky_exec_mode="direct")
    assert reg.exec_mode == "direct"  # the kill switch, with everything still wired


def test_loop_can_be_left_unwired() -> None:
    reg = build_registry(real_arms=set(), loop=False)
    for name in LOOP_SERVICE_FACTORIES:
        assert getattr(reg.services, name) is None and reg.modes[name] == NOT_WIRED
    assert reg.exec_mode == "direct"
    set_settings(picky_exec_mode="plan")
    assert reg.exec_mode == "direct"  # plan needs planner + runner + vla + verifier
    reg2 = build_registry(real_arms=set(), overrides={"verifier": None})
    assert reg2.services.verifier is None and reg2.modes["verifier"] == NOT_WIRED and reg2.exec_mode == "direct"


def test_missing_loop_factory_means_not_wired(monkeypatch: pytest.MonkeyPatch) -> None:
    import arms.gateway.registry as registry

    monkeypatch.setitem(registry.LOOP_SERVICE_FACTORIES, "planner", ("agent", "no_such_real", "no_such_fake"))
    reg = build_registry(real_arms=set())
    assert reg.services.planner is None and reg.modes["planner"] == NOT_WIRED
    assert "planner" not in reg.notes and reg.exec_mode == "direct"


def test_real_loop_fields_fall_back_to_fakes_without_keys() -> None:
    set_settings(perceptron_api_key="", picky_vision_provider="perceptron", gemini_api_key="", anthropic_api_key="")
    reg = build_registry(real_arms={"verifier", "vla"})
    assert reg.modes["vla"] == "real"
    assert reg.modes["verifier"] == "fake (real failed)" and "PERCEPTRON_API_KEY" in reg.notes["verifier"]
    assert reg.modes["planner"] == "fake" and reg.exec_mode == "plan"


# ---- annotations -----------------------------------------------------------------------------------------------------
def _ref(frame_id: str = "rover-ave-1.wrist:00001:abc") -> FrameRef:
    return FrameRef(frame_id=frame_id, camera_id="rover-ave-1.wrist", robot_id="rover-ave-1")


def test_annotation_index_collects_verdicts_groundings_and_overlays() -> None:
    idx = EvidenceAnnotations(max_frames=2)
    ref = _ref()
    ev = Evidence(kind="box", role="held", frame=ref, mention="empty gripper", box=NormBox(x1=1, y1=2, x2=3, y2=4))
    verdict = Verdict(
        verify_id="v1",
        robot_id="rover-ave-1",
        outcome="fail",
        confidence=0.9,
        reason="gripper is empty",
        frames=[ref],
        atoms=[AtomVerdict(path="0", op="gripper_holding", outcome="fail", confidence=0.9, evidence=[ev])],
    )
    idx.observe(
        make_event(
            "step.verified", trip_id=None, source="agent", plan_id="p", step_id="s1", phase="post", verdict=verdict
        )
    )
    target = TargetSpec(sku="SALSA-MILD", description="mild salsa jar")
    g = GroundedTarget(
        grounding_id="g1",
        target=target,
        frame=ref,
        box=NormBox(x1=10, y1=10, x2=20, y2=20),
        grasp_point=NormPoint(x=15, y=15),
        confidence=0.8,
    )
    idx.observe(make_event("target.grounded", trip_id=None, source="agent", plan_id="p", step_id="s2", grounding=g))
    entry = idx.get(ref.frame_id)
    assert entry is not None and entry.verify_ids == ["v1"] and entry.grounding_ids == ["g1"]
    assert [e["mention"] for e in entry.evidence] == ["empty gripper", "mild salsa jar"]
    idx.observe(make_event("error", trip_id=None, arm="x", message="ignored"))
    for n in range(3):  # bounded
        idx.add_verdict(verdict.model_copy(update={"verify_id": f"v{n + 2}", "frames": [_ref(f"f{n}")], "atoms": []}))
    assert idx.get(ref.frame_id) is None and idx.get("f2") is not None


# ---- routes ----------------------------------------------------------------------------------------------------------
def test_loop_routes_answer_503_when_not_wired() -> None:
    with _client(loop=False) as c:
        assert c.get("/health").json()["details"]["exec_mode"] == "direct"
        for path in (
            "/api/goals/g/plan",
            "/api/plans/p",
            "/api/verifier",
            "/api/fleet/robots/rover-ave-1/vla",
            "/api/evidence/f.jpg",
        ):
            assert c.get(path).status_code == 503, path


def test_dev_routes_need_picky_dev() -> None:
    with _client() as c:
        assert c.get("/api/dev/faults").status_code == 404
        assert c.post("/api/dev/faults/scenario", json={"name": "drop_on_first_grasp"}).status_code == 404
        assert (
            c.post(
                "/api/dev/plans", json={"robot_id": "rover-ave-1", "text": "salsa", "store_id": "store_ave"}
            ).status_code
            == 404
        )
        set_settings(picky_dev=True)
        st = c.get("/api/dev/faults").json()
        assert st == {"scenario": None, "rules": [], "firings": []}
        sc = c.post("/api/dev/faults/scenario", json={"name": "drop_on_first_grasp"})
        assert sc.status_code == 200 and sc.json()["rules"][0]["effect"] == "drop_after_lift"
        assert c.get("/health").json()["details"]["fault_scenario"] == "drop_on_first_grasp"
        assert c.post("/api/dev/faults/scenario", json={"name": "nope"}).status_code == 404
        assert c.post("/api/dev/faults/scenario", json={}).status_code == 422
        rule = {"fault_id": "f-x", "effect": "grasp_miss", "match": {"step_kind": "grasp"}}
        assert c.post("/api/dev/faults", json=rule).json()["fault_id"] == "f-x"
        assert c.delete("/api/dev/faults").status_code == 204
        assert c.get("/api/dev/faults").json()["rules"] == []


@needs_data
def test_verifier_vla_and_verify_routes() -> None:
    with _client() as c:
        caps = c.get("/api/verifier").json()
        assert caps["provider"] == "fake" and caps["healthy"]
        assert c.put("/api/verifier/mode", json={"mode": "decide"}).json()["mode"] == "decide"
        assert c.put("/api/verifier/mode", json={"mode": "nope"}).status_code == 422
        assert c.get("/api/verifier/providers").json()[0]["provider"] == "fake"
        tiers = {t["tier"]: t for t in c.get("/api/fleet/robots/rover-ave-1/vla").json()}
        assert tiers["t0_scripted"]["available"] and not tiers["t1_pretrained"]["available"]
        assert c.get("/api/fleet/robots/nope/vla").status_code == 404
        assert c.get("/api/vla/execs/nope").status_code == 404
        v = c.post("/api/verify", json={"robot_id": "rover-ave-1", "preset": "basket"})
        assert v.status_code == 200 and v.json()["outcome"] == "pass", v.text
        assert c.post("/api/verify", json={"robot_id": "nope", "preset": "basket"}).status_code == 404
        assert c.post("/api/verify", json={"robot_id": "rover-ave-1"}).status_code == 422
        assert c.get("/api/goals/nope/plan").status_code == 404
        assert c.get("/api/plans/nope/run").status_code == 404
        assert c.get("/api/evidence/nope.jpg").status_code == 404
        assert c.get("/api/evidence/nope/annotations").status_code == 404


@needs_data
def test_dev_plan_catches_the_injected_drop_and_serves_its_evidence() -> None:
    set_settings(picky_dev=True)
    with _client() as c:
        c.post("/api/dev/faults/scenario", json={"name": "drop_on_first_grasp"})
        done = _run_dev_plan(c, "salsa and sour cream")
        run, pid = done["run"], done["plan"]["plan_id"]
        assert run["status"] == "completed", run
        grasp = next(a for a in run["attempts"] if a["failure"] and a["failure"]["caught_by_verifier"])
        assert grasp["fault_ids"] == ["f-drop-1"] and grasp["vla_claimed_success"]
        retries = c.get(f"/api/plans/{pid}/attempts", params={"step_id": grasp["step_id"]}).json()
        assert [a["attempt"] for a in retries] == [1, 2] and retries[1]["failure"] is None
        plan = c.get(f"/api/plans/{pid}").json()
        assert next(s for s in plan["steps"] if s["step_id"] == grasp["step_id"])["status"] == "succeeded"
        record = c.get(f"/api/vla/execs/{grasp['exec_id']}").json()
        assert record["statuses"][-1]["phase"] == "succeeded"  # the VLA claimed success; the verifier said no
        cancelled = c.post(f"/api/vla/execs/{grasp['exec_id']}/cancel", json={})
        assert cancelled.json()["phase"] == "succeeded"  # idempotent on a terminal exec
        frame = grasp["post"]["frames"][0]
        jpg = c.get(f"/api/evidence/{frame['frame_id']}.jpg", params={"w": 64})
        assert jpg.status_code == 200 and jpg.headers["content-type"] == "image/jpeg" and jpg.content[:2] == b"\xff\xd8"
        notes = c.get(f"/api/evidence/{frame['frame_id']}/annotations").json()
        assert grasp["post"]["verify_id"] in notes["verify_ids"]
        firings = c.get("/api/dev/faults").json()["firings"]
        assert [f["fault_id"] for f in firings] == ["f-drop-1"]
        types = {e.type for e in c.registry.bus.recent()}  # type: ignore[attr-defined]
        assert {
            "plan.created",
            "step.started",
            "vla.status",
            "step.verified",
            "step.failed",
            "step.completed",
            "plan.completed",
            "demo.fault",
        } <= types


@needs_data
def test_cancel_a_goal_less_dev_plan() -> None:
    set_settings(picky_dev=True)
    with _client(fake_opts={**FAST_FAKES, "robot": {"realtime_factor": 1.0}}) as c:
        plan = c.post("/api/dev/plans", json={"robot_id": "rover-ave-2", "text": "salsa", "store_id": "store_ave"})
        pid = plan.json()["plan_id"]
        time.sleep(0.3)
        state = c.post(f"/api/plans/{pid}/cancel", json={"reason": "test"})
        assert state.status_code == 200 and state.json()["status"] == "cancelled", state.text


class _UnblurredEvidence:
    async def capture_refs(self, *a: Any, **kw: Any) -> list[FrameRef]:
        return []

    async def get_frame(self, frame_id: str) -> CameraFrame:
        jpeg = base64.b64encode(b"\xff\xd8\xff\xd9").decode()
        return CameraFrame(frame_id=frame_id, camera_id="rover-ave-1.front", jpeg_b64=jpeg, w=1, h=1, blurred=False)


def test_unblurred_evidence_is_never_served() -> None:
    with _client(overrides={"evidence": lambda s: _UnblurredEvidence()}) as c:
        assert c.get("/api/evidence/f1.jpg").status_code == 503
