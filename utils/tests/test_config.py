from pathlib import Path

import pytest

from utils import config
from utils.config import ENV_VARS, Settings, get_settings, reset_settings, set_settings

EXPECTED = {
    "PICKY_ENV": "dev",
    "PICKY_REAL_ARMS": "",
    "PICKY_DEFAULT_STORE": "store_ave",
    "PICKY_DATA_DIR": "data",
    "DATABASE_URL": "postgresql+psycopg://picky:picky@localhost:5432/picky",
    "PICKY_LLM_PROVIDER": "gemini",
    "GEMINI_API_KEY": "",
    "ANTHROPIC_API_KEY": "",
    "PICKY_MODEL_BRAIN": "",
    "PICKY_MODEL_FAST": "",
    "PICKY_MODEL_SMART": "",
    "PICKY_GEMINI_THINKING": "low",
    "PICKY_LLM_TIMEOUT_S": 60.0,
    "PERCEPTRON_API_KEY": "",
    "PERCEPTRON_MODEL": "perceptron-mk1.5",
    "PERCEPTRON_BASE_URL": "https://api.perceptron.inc/v1",
    "PICKY_VISION_PROVIDER": "perceptron",
    "PICKY_VERIFIER_MODE": "",
    "PICKY_VISION_CALL_TIMEOUT_S": 4.0,
    "PICKY_EXEC_MODE": "",
    "PICKY_FAULTS": "",
    "PICKY_DEV": False,
    "PICKY_PLAN_TIMEOUT_S": 20.0,
    "PICKY_VERIFY_TIMEOUT_S": 8.0,
    "PICKY_VLA_TIERS": "",
    "PICKY_VLA_T1_MODEL": "",
    "PICKY_VLA_POLICY_URL": "",
    "PICKY_VLA_POLICY_PROTOCOL": "",
    "GATEWAY_HOST": "0.0.0.0",
    "GATEWAY_PORT": 8000,
    "PICKY_GATEWAY_URL": "http://localhost:8000",
    "PICKY_SIM_TOKEN": "dev-sim-token",
    "PICKY_SIGNING_KEY": "dev-signing-key",
    "STRIPE_SECRET_KEY": "",
    "VISA_API_KEY": "",
    "VISA_SHARED_SECRET": "",
    "PHOTON_API_KEY": "",
    "PHOTON_PROJECT_ID": "",
    "DEEPGRAM_API_KEY": "",
    "PERCEPTRON_ROBOTICS_API_KEY": "",
    "HF_TOKEN": "",
    "PICKY_REC_MAX_STOPS": 3,
    "PICKY_SCRIPTED_SHOPPER": False,
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in EXPECTED:
        monkeypatch.delenv(k, raising=False)
    reset_settings()
    yield
    reset_settings()


def test_exact_env_vars_and_defaults():
    assert set(ENV_VARS) == set(EXPECTED)
    s = Settings(_env_file=None)
    for k, v in EXPECTED.items():
        assert getattr(s, k.lower()) == v, k


def test_env_override_and_real_arms(monkeypatch):
    monkeypatch.setenv("PICKY_REAL_ARMS", " Routing, trust ,")
    monkeypatch.setenv("GATEWAY_PORT", "9001")
    s = Settings(_env_file=None)
    assert s.real_arms() == {"routing", "trust"}
    assert s.is_real("routing") and s.is_real("TRUST") and not s.is_real("catalog")
    assert s.gateway_port == 9001
    assert Settings(_env_file=None, picky_real_arms="all").is_real("anything")


def test_paths():
    s = Settings(_env_file=None)
    assert s.repo_root == config.repo_root() == Path(config.__file__).resolve().parents[1]
    assert s.data_dir == s.repo_root / "data"
    assert Settings(_env_file=None, picky_data_dir="/tmp/x").data_dir == Path("/tmp/x")


def test_lazy_singleton_and_override():
    a = get_settings()
    assert config.settings is a
    b = set_settings(picky_default_store="store_hill")
    assert config.settings is b and b.picky_default_store == "store_hill"
    c = set_settings(b, picky_rec_max_stops=5)
    assert c.picky_rec_max_stops == 5 and c.picky_default_store == "store_hill"
    reset_settings()
    assert get_settings() is not c


def test_env_example_matches_if_present():
    path = config.repo_root() / ".env.example"
    if not path.exists():
        pytest.skip(".env.example not written yet")
    keys = {
        line.split("=", 1)[0].strip()
        for line in path.read_text().splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    assert keys == set(EXPECTED)
