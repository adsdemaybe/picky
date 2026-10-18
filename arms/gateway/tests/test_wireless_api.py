"""/api/wireless/* over the fake wireless arm (TestClient, in-process): routing, error mapping, schema export."""

import pytest
from fastapi.testclient import TestClient

from arms.gateway.app import create_app
from arms.gateway.orchestrator import OrchestratorConfig
from arms.gateway.registry import build_registry
from arms.gateway.tests.helpers import FAST_FAKES
from arms.gateway.tests.stubs import StubBrain, StubCameras, StubFleet


@pytest.fixture
def client():
    reg = build_registry(
        real_arms=set(),
        fake_opts=FAST_FAKES,
        overrides={"fleet": StubFleet, "brain": StubBrain, "cameras": StubCameras},
        orchestrator_config=OrchestratorConfig(approval_channels=("web",)),
    )
    with TestClient(create_app(registry=reg)) as c:
        yield c


def test_info_and_plans(client: TestClient) -> None:
    info = client.get("/api/wireless").json()
    assert info["store_id"] == "store_tmobile" and len(info["plans"]) >= 4 and "Synthetic" in info["notice"]
    plans = client.get("/api/wireless/plans", params={"lines": 2, "user_id": "ruth"}).json()
    assert plans[0]["plan_id"] == "unlimited_55"  # ruth is 55+ eligible
    q = client.get("/api/wireless/plans/value_unlimited/quote", params={"lines": 4}).json()
    assert q["monthly_cents"] == 13000


def test_errors_map_to_404_409_422(client: TestClient) -> None:
    assert client.get("/api/wireless/plans/nope/quote").status_code == 404
    assert client.get("/api/wireless/plans/unlimited_55/quote", params={"lines": 3}).status_code == 409
    assert client.get("/api/wireless/accounts/nobody").status_code == 404
    assert client.post("/api/wireless/financing", json={"sku": "CASE-PX9-CLR"}).status_code == 409
    assert client.post("/api/wireless/trade-in", json={"model_id": "pixel-7", "condition": "mint"}).status_code == 422


def test_quote_household_trade_in_compat(client: TestClient) -> None:
    body = {
        "user_id": "maya",
        "items": [{"sku": "PX9-128", "financed": True}],
        "plan_id": "value_unlimited",
        "lines": 2,
    }
    b = client.post("/api/wireless/quote", json=body).json()
    assert b["ok"] and b["synthetic"] and b["monthly_cents"] > 9000
    h = client.post("/api/wireless/household", json={"user_id": "garcia"}).json()
    assert h["recommended_plan_id"] == "value_unlimited"
    t = client.post(
        "/api/wireless/trade-in", json={"model_id": "pixel-7", "condition": "good", "user_id": "maya"}
    ).json()
    assert t["remaining_installments_cents"] == 6600
    acc = client.get("/api/wireless/compatible/PX9-128").json()
    assert acc[0]["sku"] == "CASE-PX9-CLR"
    assert client.get("/api/wireless/accounts/maya").json()["lines"][0]["device_name"] == "Pixel 7"


def test_wireless_in_openapi(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert {"/api/wireless", "/api/wireless/quote", "/api/wireless/household"} <= set(paths)
