"""FastAPI surface (TestClient runs the lifespan). Fleet / brain / cameras are the gateway test stubs (tests/stubs.py)
so this checks the gateway's routing, error mapping, headers, websockets and MJPEG framing, not those arms."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from arms.gateway.app import _jpeg_bytes, _mjpeg, create_app
from arms.gateway.export_schemas import export
from arms.gateway.orchestrator import OrchestratorConfig
from arms.gateway.registry import build_registry
from arms.gateway.tests.helpers import FAST_FAKES, needs_data
from arms.gateway.tests.stubs import StubBrain, StubCameras, StubFleet
from contracts import CONTRACTS_VERSION
from contracts.events import make_event
from contracts.sim import SimHello, SimRobotInfo, SimStoreInfo, parse_gateway_to_sim
from utils.config import get_settings, reset_settings, set_settings

STUBS = {"fleet": StubFleet, "brain": StubBrain, "cameras": StubCameras}


def _client(**overrides):  # noqa: ANN003, ANN202
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        overrides={**STUBS, **overrides},
        orchestrator_config=OrchestratorConfig(approval_channels=("web",)),
    )
    c = TestClient(create_app(registry=reg))
    c.registry = reg  # type: ignore[attr-defined]
    return c


@pytest.fixture
def client():
    with _client() as c:
        yield c


def publish(client: TestClient, **kw) -> None:  # noqa: ANN003
    ev = make_event(kw.pop("type", "error"), trip_id=kw.pop("trip_id", None), arm="test", message="m", **kw)
    client.portal.call(client.registry.bus.publish, ev)  # type: ignore[attr-defined]


def test_health_schema_and_event_seq_header(client: TestClient) -> None:
    res = client.get("/health")
    h = res.json()
    assert h["contracts_version"] == CONTRACTS_VERSION
    assert h["sim_connected"] is False and h["robots"] == 3
    assert {"fleet", "brain", "cameras"} <= set(h["details"])
    assert res.headers["X-Event-Seq"].isdigit()
    publish(client)
    assert int(client.get("/api/fleet/robots").headers["X-Event-Seq"]) >= 1
    schema = client.get("/api/_schema/events").json()
    assert "brain.step" in json.dumps(schema) and "camera.frame" in json.dumps(schema)
    assert client.get("/debug").status_code == 200


def test_requests_and_fleet_endpoints(client: TestClient) -> None:
    plan = client.post("/api/requests", json={"text": "tacos for 6", "user_id": "maya"}).json()
    rid = plan["request_id"]
    assert plan["trip_ids"]
    assert client.get(f"/api/requests/{rid}").json()["request_id"] == rid
    assert [p["request_id"] for p in client.get("/api/requests", params={"user_id": "maya"}).json()] == [rid]
    assert client.get("/api/requests/nope").status_code == 404
    assert client.post("/api/requests", json={"text": "", "user_id": "maya"}).status_code == 422

    robots = client.get("/api/fleet/robots", params={"store_id": "store_ave"}).json()
    assert {r["robot_id"] for r in robots} == {"rover-ave-1", "rover-ave-2"}
    assert client.get("/api/fleet/robots/rover-hill-1").json()["name"] == "Hazel"
    assert client.get("/api/fleet/robots/nope").status_code == 404
    assert client.get("/api/fleet/robots/rover-ave-1/brain").json()["robot_id"] == "rover-ave-1"
    assert client.get("/api/fleet/robots/rover-ave-1/steps", params={"limit": 5}).json()[0]["kind"] == "thought"
    reply = client.post("/api/fleet/robots/rover-ave-1/messages", json={"text": "status?"}).json()
    assert reply["text"] == "ok: status?"

    goal = client.post("/api/fleet/robots/rover-ave-2/goals", json={"kind": "shelf_scan", "categories": ["snacks"]})
    assert goal.status_code == 200, goal.text
    g = goal.json()
    assert g["robot_id"] == "rover-ave-2" and g["store_id"] == "store_ave" and g["user_id"] == "staff"
    assert client.get(f"/api/goals/{g['goal_id']}").json()["goal_id"] == g["goal_id"]
    assert client.get("/api/goals", params={"robot_id": "rover-ave-2", "status": ["assigned", "active"]}).json()
    assert client.get("/api/goals/nope").status_code == 404
    assert client.get(f"/api/goals/{g['goal_id']}/steps").status_code == 200
    assert client.post(f"/api/goals/{g['goal_id']}/cancel").json()["status"] == "cancelled"
    assert client.post("/api/goals", json={"kind": "return_to_dock"}).status_code == 422  # needs store or robot
    assert client.post("/api/goals", json={"kind": "fetch", "store_id": "store_ave"}).status_code == 422
    assert client.post("/api/goals", json={"kind": "shelf_scan", "store_id": "store_ave"}).status_code == 200
    conflict = client.post(
        "/api/goals", json={"kind": "return_to_dock", "robot_id": "rover-ave-1", "title": "conflict"}
    )
    assert conflict.status_code == 409 and "busy" in conflict.json()["detail"]
    assert client.post("/api/fleet/robots/rover-ave-1/stop").json()["robot_id"] == "rover-ave-1"
    assert client.get("/api/sim/status").json()["connected"] is False


def test_cameras_snapshot_observe_and_errors(client: TestClient) -> None:
    cams = client.get("/api/cameras", params={"robot_id": "rover-ave-1"}).json()
    assert {c["camera_id"] for c in cams} == {"rover-ave-1.front", "rover-ave-1.wrist", "rover-ave-1.basket"}
    assert cams[0]["snapshot_url"].startswith("/api/cameras/") and cams[0]["stream_url"].endswith("stream.mjpg")
    res = client.get("/api/cameras/rover-ave-1.front/snapshot.jpg", params={"w": 320, "t": 123})
    assert res.status_code == 200 and res.headers["content-type"] == "image/jpeg"
    assert res.headers["cache-control"] == "no-store" and res.content[:2] == b"\xff\xd8"
    assert client.get("/api/cameras/nope.front/snapshot.jpg").status_code == 404
    assert client.get("/api/cameras/nope.front/stream.mjpg").status_code == 404
    obs = client.post("/api/cameras/store_ave.ceiling-1/observe", json={"question": "any people?"}).json()
    assert obs["question"] == "any people?" and obs["blurred"] is True
    assert client.post("/api/cameras/store_ave.ceiling-1/observe").status_code == 200


def test_unblurred_frames_are_never_served() -> None:
    with _client(cameras=lambda s: StubCameras(s, raw=True)) as c:
        assert c.get("/api/cameras/rover-ave-1.front/snapshot.jpg").status_code == 503
    assert _jpeg_bytes(None) is None


async def test_mjpeg_framing() -> None:
    class Req:
        def __init__(self) -> None:
            self.calls = 0

        async def is_disconnected(self) -> bool:
            self.calls += 1
            return self.calls > 2

    cams = StubCameras()
    parts = [p async for p in _mjpeg(cams, "rover-ave-1.front", 50.0, None, Req())]  # type: ignore[arg-type]
    assert len(parts) == 2
    for part in parts:
        head, _, body = part.partition(b"\r\n\r\n")
        assert head.startswith(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: ")
        length = int(head.rsplit(b": ", 1)[1])
        assert body[:2] == b"\xff\xd8" and len(body) == length + 2 and body.endswith(b"\r\n")
    raw = StubCameras(raw=True)
    assert [p async for p in _mjpeg(raw, "rover-ave-1.front", 50.0, None, Req())] == []  # type: ignore[arg-type]


def test_ws_cameras_multiplexed_and_single(client: TestClient) -> None:
    with client.websocket_connect("/ws/cameras?store_id=store_hill&fps=5&w=320") as ws:
        seen = set()
        for _ in range(8):
            ev = ws.receive_json()
            assert ev["type"] == "camera.frame" and ev["frame"]["blurred"] is True and ev["seq"] == 0
            seen.add(ev["camera_id"])
        assert seen <= {"rover-hill-1.front", "rover-hill-1.wrist", "rover-hill-1.basket"} and len(seen) >= 2
    with client.websocket_connect("/ws/cameras/rover-ave-2.wrist?fps=10") as ws:
        ev = ws.receive_json()
        assert ev["camera_id"] == "rover-ave-2.wrist" and base64.b64decode(ev["frame"]["jpeg_b64"])[:2] == b"\xff\xd8"


def test_ws_frames_sends_every_robot_camera(client: TestClient) -> None:
    with client.websocket_connect("/ws/frames?robot_id=rover-ave-1&fps=10") as ws:
        seen = {ws.receive_json()["camera_id"] for _ in range(9)}
        assert seen == {"rover-ave-1.front", "rover-ave-1.wrist", "rover-ave-1.basket"}
    with client.websocket_connect("/ws/frames?robot_id=rover-ave-1&fps=10&legacy=1") as ws:
        types = {(e["type"], e.get("camera_id")) for e in (ws.receive_json() for _ in range(9))}
        assert ("frame", None) in types and ("camera.frame", "rover-ave-1.front") not in types


def test_ws_filters_and_replay(client: TestClient) -> None:
    publish(client, store_id="store_ave", request_id="req_old")
    with client.websocket_connect("/ws?store_id=store_hill&robot_id=rover-hill-1&robot_id=rover-x") as ws:
        publish(client, store_id="store_ave", robot_id="rover-ave-1")
        publish(client, store_id="store_hill", robot_id="rover-ave-1")
        publish(client, store_id="store_hill", robot_id="rover-hill-1", user_id="u1")
        ev = ws.receive_json()
        assert ev["store_id"] == "store_hill" and ev["robot_id"] == "rover-hill-1" and ev["user_id"] == "u1"
    with client.websocket_connect("/ws?request_id=req_old") as ws:  # request filter replays the ring
        ev = ws.receive_json()
        assert ev["request_id"] == "req_old"
    with client.websocket_connect("/ws?after_seq=0&store_id=store_ave") as ws:
        replayed = []
        while not any(e["type"] == "error" for e in replayed):  # robot.state of the fake may come first
            replayed.append(ws.receive_json())
        assert replayed[-1]["request_id"] == "req_old"
        assert all(e["store_id"] == "store_ave" for e in replayed)
        ws.send_text(json.dumps({"type": "chat", "text": "hi"}))  # no trip -> error back
        ev = ws.receive_json()
        while not (ev["type"] == "error" and "chat" in ev["message"]):
            ev = ws.receive_json()


@needs_data
def test_rest_catalog_trip_and_snapshot(client: TestClient) -> None:
    stores = client.get("/api/stores").json()
    assert any(s["store_id"] == "online" for s in stores)
    assert client.get("/api/stores/store_ave/planogram").status_code == 200
    assert client.get("/api/products/search", params={"q": "salsa"}).status_code == 200
    assert client.get("/api/users/maya/profile").json()["user_id"] == "maya"
    trip = client.post("/api/trips", json={"user_id": "maya", "text": "tacos for 6, under $40, one vegetarian"}).json()
    tid = trip["trip_id"]
    assert trip["auto_execute"] is False
    assert client.get(f"/api/trips/{tid}").json()["trip_id"] == tid
    assert any(t["trip_id"] == tid for t in client.get("/api/trips", params={"user_id": "maya"}).json())
    with client.websocket_connect(f"/ws?trip_id={tid}") as ws:
        first = ws.receive_json()
        assert first["type"] == "trip.snapshot" and first["trip"]["trip_id"] == tid and first["user_id"] == "maya"
        second = ws.receive_json()
        assert second["type"] == "trip.created" and second["store_id"] == trip["store_id"]
    assert client.get("/api/trips/nope").status_code == 404
    assert client.post("/api/approvals/nope", json={"decision": "approve"}).status_code == 404
    assert client.get("/api/approvals", params={"user_id": "staff", "store_id": "store_ave"}).json() == []


def test_stock_adjust_rejects_robot_reasons(client: TestClient) -> None:
    res = client.post("/api/stores/store_ave/stock/adjust", json={"sku": "X", "delta": -1, "reason": "pick"})
    assert res.status_code == 422


def test_stock_and_categories_when_the_catalog_supports_them(client: TestClient) -> None:
    catalog = client.registry.services.catalog  # type: ignore[attr-defined]
    if not hasattr(catalog, "stock_levels") or not hasattr(catalog, "list_categories"):
        pytest.skip("catalog arm has no stock / categories API yet")
    cats = client.get("/api/categories", params={"store_id": "store_ave"})
    assert cats.status_code == 200 and cats.json()
    assert client.get("/api/categories/nope-category/products").status_code == 404
    assert client.get("/api/stores/store_ave/stock").status_code == 200
    assert client.get("/api/stores/store_ave/stock/alerts").status_code == 200
    assert client.get("/api/stores/store_ave/stock/history", params={"limit": 5}).status_code == 200


def test_sim_handshake_multiplexed(client: TestClient) -> None:
    token = get_settings().picky_sim_token
    v = CONTRACTS_VERSION
    with client.websocket_connect("/ws/sim") as ws:
        ws.send_text(SimHello(token="wrong", store_id="store_ave", contracts_version=v).model_dump_json())
        welcome = parse_gateway_to_sim(ws.receive_text())
        assert welcome.type == "welcome" and welcome.ok is False
    with client.websocket_connect("/ws/sim") as ws:
        ws.send_text(SimHello(token=token, contracts_version="0.1.0").model_dump_json())
        assert parse_gateway_to_sim(ws.receive_text()).ok is False
    hello = SimHello(
        token=token,
        contracts_version=v,
        worker_id="isaac-vhagar",
        stores=[SimStoreInfo(store_id="store_ave"), SimStoreInfo(store_id="store_hill")],
        robots=[SimRobotInfo(robot_id="rover-hill-1", store_id="store_hill")],
    )
    with client.websocket_connect("/ws/sim") as ws:
        ws.send_text(hello.model_dump_json())
        welcome = parse_gateway_to_sim(ws.receive_text())
        assert welcome.ok is True
        assert client.get("/health").json()["sim_connected"] is True
        st = client.get("/api/sim/status").json()
        assert st["connected"] and st["workers"][0]["worker_id"] == "isaac-vhagar"
        assert client.registry.sim_link.is_connected("rover-hill-1")  # type: ignore[attr-defined]
    assert client.registry.sim_link.is_connected() is False  # type: ignore[attr-defined]


def test_export_schemas(tmp_path) -> None:  # noqa: ANN001
    paths = export(tmp_path)
    names = {p.name for p in paths}
    assert {"openapi.json", "events.schema.json", "client_messages.schema.json"} <= names
    openapi = json.loads((tmp_path / "openapi.json").read_text())
    for path in (
        "/api/trips",
        "/api/requests",
        "/api/fleet/robots",
        "/api/goals",
        "/api/cameras/{camera_id}/stream.mjpg",
        "/api/stores/{store_id}/stock/alerts",
        "/api/categories",
        "/api/sim/status",
    ):
        assert path in openapi["paths"], path
    events = (tmp_path / "events.schema.json").read_text()
    for t in ("brain.step", "goal.assigned", "stock.alert", "camera.frame", "fleet.robot_updated"):
        assert t in events, t
    assert "robot_chat" in (tmp_path / "client_messages.schema.json").read_text()


def test_dev_inbound_is_dev_only(client: TestClient) -> None:
    """Outside dev, POST /api/dev/inbound must not let anyone text 'YES' on the shopper's behalf."""
    body = {"message_id": "msg_x", "channel": "photon", "handle": "+15550000000", "text": "YES"}
    set_settings(picky_env="prod")
    try:
        assert client.post("/api/dev/inbound", json=body).status_code == 404
    finally:
        reset_settings()


def test_staff_account_defaults_to_staff_role(client: TestClient) -> None:
    """POST /api/requests from the staff account is a staff request unless the body says otherwise."""
    fleet = client.registry.services.fleet  # type: ignore[attr-defined]
    seen = []
    orig = fleet.submit_request

    async def capture(req):  # noqa: ANN001, ANN202
        seen.append(req)
        return await orig(req)

    fleet.submit_request = capture
    for body in (
        {"text": "scan the dairy aisle at Hill", "user_id": "staff"},
        {"text": "tacos for 6", "user_id": "staff", "role": "shopper"},
        {"text": "tacos for 6", "user_id": "maya"},
    ):
        assert client.post("/api/requests", json=body).status_code == 200
    assert [r.role for r in seen] == ["staff", "shopper", "shopper"]


@pytest.mark.parametrize(("scripted", "mode"), [(False, "off"), (True, "auto")])
def test_dev_server_scripted_shopper_is_opt_in(scripted: bool, mode: str) -> None:
    """`make dev` must not answer approvals (checkout included) by itself: PICKY_SCRIPTED_SHOPPER=1 opts in."""
    set_settings(picky_scripted_shopper=scripted, picky_real_arms="")
    try:
        app = create_app()
        with TestClient(app) as c:
            assert c.get("/health").status_code == 200
            assert app.state.registry.services.messaging.reply_mode == mode
    finally:
        reset_settings()


def test_resume_endpoint_returns_robot_info(client: TestClient) -> None:
    r = client.post("/api/fleet/robots/rover-ave-1/resume")
    assert r.status_code == 200 and r.json()["robot_id"] == "rover-ave-1"
