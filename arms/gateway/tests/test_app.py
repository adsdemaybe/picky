"""FastAPI surface with all fakes (TestClient runs the lifespan)."""

import json

import pytest
from fastapi.testclient import TestClient

from arms.gateway.app import create_app
from arms.gateway.export_schemas import export
from arms.gateway.orchestrator import OrchestratorConfig
from arms.gateway.registry import build_registry
from arms.gateway.tests.helpers import FAST_FAKES, needs_data
from contracts import CONTRACTS_VERSION
from contracts.sim import SimHello, parse_gateway_to_sim
from utils.config import get_settings, reset_settings, set_settings


@pytest.fixture
def client():
    reg = build_registry(
        real_arms=set(), fake_opts=FAST_FAKES, orchestrator_config=OrchestratorConfig(approval_channels=("web",))
    )
    with TestClient(create_app(registry=reg)) as c:
        c.registry = reg  # type: ignore[attr-defined]
        yield c


def test_health_and_schema(client: TestClient) -> None:
    h = client.get("/health").json()
    assert h["contracts_version"] == CONTRACTS_VERSION
    assert h["sim_connected"] is False
    assert client.get("/api/_schema/events").json()
    assert client.get("/debug").status_code == 200


@needs_data
def test_rest_catalog_and_trip(client: TestClient) -> None:
    stores = client.get("/api/stores").json()
    assert any(s["store_id"] == "online" for s in stores)
    assert client.get("/api/stores/store_ave/planogram").status_code == 200
    assert client.get("/api/products/search", params={"q": "salsa"}).status_code == 200
    assert client.get("/api/users/maya/profile").json()["user_id"] == "maya"
    trip = client.post("/api/trips", json={"user_id": "maya", "text": "tacos for 6, under $40, one vegetarian"}).json()
    tid = trip["trip_id"]
    assert client.get(f"/api/trips/{tid}").json()["trip_id"] == tid
    assert any(t["trip_id"] == tid for t in client.get("/api/trips", params={"user_id": "maya"}).json())
    with client.websocket_connect(f"/ws?trip_id={tid}") as ws:
        first = ws.receive_json()
        assert first["type"] == "trip.snapshot" and first["trip"]["trip_id"] == tid
        second = ws.receive_json()
        assert second["type"] == "trip.created"
    assert client.get("/api/trips/nope").status_code == 404
    assert client.post("/api/approvals/nope", json={"decision": "approve"}).status_code == 404


def test_sim_handshake(client: TestClient) -> None:
    token = get_settings().picky_sim_token
    v = CONTRACTS_VERSION
    with client.websocket_connect("/ws/sim") as ws:
        ws.send_text(SimHello(token="wrong", store_id="store_ave", contracts_version=v).model_dump_json())
        welcome = parse_gateway_to_sim(ws.receive_text())
        assert welcome.type == "welcome" and welcome.ok is False
    with client.websocket_connect("/ws/sim") as ws:
        ws.send_text(SimHello(token=token, store_id="store_ave", contracts_version=v).model_dump_json())
        welcome = parse_gateway_to_sim(ws.receive_text())
        assert welcome.ok is True
        assert client.get("/health").json()["sim_connected"] is True
    assert client.registry.sim_link.is_connected() is False  # type: ignore[attr-defined]


def test_export_schemas(tmp_path) -> None:  # noqa: ANN001
    paths = export(tmp_path)
    names = {p.name for p in paths}
    assert {"openapi.json", "events.schema.json", "client_messages.schema.json"} <= names
    openapi = json.loads((tmp_path / "openapi.json").read_text())
    assert "/api/trips" in openapi["paths"]


def test_dev_inbound_is_dev_only(client: TestClient) -> None:
    """Outside dev, POST /api/dev/inbound must not let anyone text 'YES' on the shopper's behalf."""
    body = {"message_id": "msg_x", "channel": "photon", "handle": "+15550000000", "text": "YES"}
    set_settings(picky_env="prod")
    try:
        assert client.post("/api/dev/inbound", json=body).status_code == 404
    finally:
        reset_settings()
