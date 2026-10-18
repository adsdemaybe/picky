"""utils.secrets: resolution order, keychain/file stores, Settings integration, CLI, age team bundle.

Every test runs with an in-memory keyring and a temporary HOME, so nothing here touches the real OS keychain or
``~/.config/picky``.
"""

from __future__ import annotations

import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from utils import secrets as sv
from utils.config import Settings, get_settings, reset_settings

SENTINEL = "sk-ant-SENTINEL-never-print-0123456789"
ROBOTICS = "prc-robotics-team-key-ABCD1234"


class MemoryKeyring:
    """In-memory stand-in for a keyring backend (same three methods utils.secrets uses)."""

    priority = 1

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self.store[(service, username)]


class BrokenKeyring(MemoryKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("keychain locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("keychain locked")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    """Temporary HOME, clean env, and keyring auto-detection pinned to memory (never the real keychain)."""
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.delenv(sv.BACKEND_ENV, raising=False)  # the repo conftest sets it to off
    for name in sv.NAMES:
        monkeypatch.delenv(name, raising=False)
    try:
        import keyring

        monkeypatch.setattr(keyring, "get_keyring", lambda: MemoryKeyring())
    except ImportError:
        pass
    sv.reset_vault()
    reset_settings()
    yield h
    sv.reset_vault()
    reset_settings()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    (r / "secrets").mkdir(parents=True)
    return r


@pytest.fixture
def kr():
    return MemoryKeyring()


@pytest.fixture
def vault(home, repo, kr):
    v = sv.Vault(keyring_backend=kr, home=home, repo_root=repo)
    sv.set_vault(v)
    return v


def _stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))


# --- registry / helpers ----------------------------------------------------------------------------------------------


def test_registry_names_descriptions_and_settings_fields():
    assert set(sv.NAMES) == {
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "PERCEPTRON_ROBOTICS_API_KEY",
        "PERCEPTRON_API_KEY",
        "STRIPE_SECRET_KEY",
        "VISA_API_KEY",
        "VISA_SHARED_SECRET",
        "PHOTON_API_KEY",
        "DEEPGRAM_API_KEY",
        "PICKY_SIM_TOKEN",
        "PICKY_SIGNING_KEY",
        "HF_TOKEN",
    }
    for s in sv.REGISTRY.values():
        assert s.description and s.where, s.name
    assert {n.lower() for n in sv.NAMES} <= set(Settings.model_fields)
    for s in sv.REGISTRY.values():  # dev values mirror Settings' dev-only defaults
        assert s.dev_value == (Settings.model_fields[s.name.lower()].default if s.dev_value else ""), s.name
    assert sv.spec("perceptron_robotics_api_key").name == "PERCEPTRON_ROBOTICS_API_KEY"
    with pytest.raises(sv.VaultError, match="unknown secret"):
        sv.spec("OPENAI_API_KEY")


def test_mask_and_clean_value():
    assert sv.mask(None) == "" and sv.mask("short") == "••••"
    assert sv.mask(SENTINEL) == "••••6789"
    assert sv.clean_value("HF_TOKEN", "  hf_abc \n") == "hf_abc"
    for bad in ("", "   ", "a\nb", "a\x00b"):
        with pytest.raises(sv.VaultError):
            sv.clean_value("HF_TOKEN", bad)
    with pytest.raises(sv.VaultError, match="live keys"):
        sv.clean_value("STRIPE_SECRET_KEY", "sk_live_123")
    assert sv.prefix_warning("STRIPE_SECRET_KEY", "pk_test_123")
    assert sv.prefix_warning("STRIPE_SECRET_KEY", "sk_test_123") is None


def test_dotenv_roundtrip_keeps_odd_values_literal():
    data = {"A": "plain-Value_1.2:3/4+5=6", "B": "it's a \\ back $HOME #not-a-comment", "C": 'q"uote'}
    assert sv.loads_env(sv.dumps_env(data, "# header")) == data


# --- resolution and stores -------------------------------------------------------------------------------------------


def test_resolution_order_env_keychain_file_dotenv(vault, repo, kr, monkeypatch):
    n = "PERCEPTRON_ROBOTICS_API_KEY"
    assert vault.resolve(n) == sv.Resolved(n, None, None)
    (repo / ".env").write_text(f"{n}=from-dotenv\nHF_TOKEN=\n")
    assert vault.resolve(n) == sv.Resolved(n, "from-dotenv", ".env")
    assert not vault.resolve("HF_TOKEN").is_set  # blank .env lines (copied .env.example) count as unset
    vault._write_file({n: "from-file"})
    assert vault.resolve(n) == sv.Resolved(n, "from-file", "file")
    kr.set_password("picky", n, "from-keychain")
    assert vault.resolve(n) == sv.Resolved(n, "from-keychain", "keychain")
    monkeypatch.setenv(n, "from-env")
    assert vault.resolve(n) == sv.Resolved(n, "from-env", "env")
    monkeypatch.setenv(n, "")
    r = vault.resolve(n)
    assert r.source == "env" and not r.is_set  # an exported empty var still wins, exactly like Settings
    assert {x.name: x.source for x in vault.resolve_all()}[n] == "env"


def test_set_prefers_keychain_and_drops_stale_file_copy(vault, kr):
    vault._write_file({"HF_TOKEN": "hf_old_value"})
    assert vault.mode == "keyring"
    assert vault.set("HF_TOKEN", "  hf_new_value \n") == "keychain"
    assert kr.store[("picky", "HF_TOKEN")] == "hf_new_value"
    assert "HF_TOKEN" not in vault._read_file()
    assert vault.delete("HF_TOKEN") == ["keychain"]
    assert vault.get_local("HF_TOKEN") == (None, None)


def test_file_fallback_permissions_and_odd_values(home, repo):
    v = sv.Vault(keyring_backend=None, home=home, repo_root=repo)
    assert v.mode == "file" and "no usable OS keychain" in v.describe()
    odd = "a'b\\c #x $HOME=y"
    assert v.set("HF_TOKEN", odd) == "file"
    f = home / ".config" / "picky" / "secrets.env"
    assert f == v.secrets_file
    assert _mode(f) == 0o600 and _mode(f.parent) == 0o700
    assert v.get_local("HF_TOKEN") == (odd, "file")
    os.chmod(f, 0o644)
    os.chmod(f.parent, 0o755)
    assert v.get_local("HF_TOKEN") == (odd, "file")
    assert _mode(f) == 0o600  # tightened on read
    v.set("GEMINI_API_KEY", "gemini-key-value")
    assert _mode(f.parent) == 0o700  # tightened on write
    assert v.delete("HF_TOKEN") == ["file"]
    assert v.get_local("GEMINI_API_KEY") == ("gemini-key-value", "file")


def test_keychain_failure_falls_back_to_file_without_logging_values(home, repo, caplog):
    v = sv.Vault(keyring_backend=BrokenKeyring(), home=home, repo_root=repo)
    assert v.set("ANTHROPIC_API_KEY", SENTINEL) == "file"
    assert v.get_local("ANTHROPIC_API_KEY") == (SENTINEL, "file")
    assert "keychain write failed" in caplog.text
    assert SENTINEL not in caplog.text


def test_auto_detection_and_backend_knob(home, repo, monkeypatch):
    import keyring
    import keyring.backends.fail

    def fresh() -> sv.Vault:
        return sv.Vault(home=home, repo_root=repo)

    monkeypatch.setattr(sys, "platform", "darwin")
    assert fresh().mode == "file"  # auto = the chmod-600 file, even with a usable keychain (no password prompts)
    monkeypatch.setenv(sv.BACKEND_ENV, "keyring")
    assert fresh().mode == "keyring"  # keychain only on request (the pinned in-memory one)
    monkeypatch.setattr(keyring, "get_keyring", lambda: keyring.backends.fail.Keyring())
    assert fresh().mode == "file"  # keyring's "no backend" -> the file fallback
    monkeypatch.delenv(sv.BACKEND_ENV)
    monkeypatch.setattr(keyring, "get_keyring", lambda: MemoryKeyring())
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert fresh().mode == "file"  # headless Linux (vhagar): never block on a D-Bus keyring
    monkeypatch.setenv(sv.BACKEND_ENV, "keyring")
    assert fresh().mode == "keyring"
    monkeypatch.setenv(sv.BACKEND_ENV, "file")
    assert fresh().mode == "file"


def test_backend_off_and_file_knob_with_injected_keyring(vault, kr, monkeypatch):
    monkeypatch.setenv(sv.BACKEND_ENV, "file")
    assert vault.set("HF_TOKEN", "hf_file_only") == "file" and not kr.store
    monkeypatch.setenv(sv.BACKEND_ENV, "off")
    assert vault.mode == "off" and vault.get_local("HF_TOKEN") == (None, None)
    with pytest.raises(sv.VaultError, match="off"):
        vault.set("HF_TOKEN", "hf_x")


# --- Settings integration --------------------------------------------------------------------------------------------


def test_settings_resolve_from_vault_with_env_winning(vault, tmp_path, monkeypatch):
    dotenv = tmp_path / "x.env"
    dotenv.write_text("ANTHROPIC_API_KEY=from-dotenv\nHF_TOKEN=hf_from_dotenv\n")
    vault.set("ANTHROPIC_API_KEY", "sk-ant-from-vault")
    vault.set("PERCEPTRON_ROBOTICS_API_KEY", ROBOTICS)
    s = Settings(_env_file=dotenv)
    assert s.anthropic_api_key == "sk-ant-from-vault"  # vault beats .env
    assert s.perceptron_robotics_api_key == ROBOTICS
    assert s.hf_token == "hf_from_dotenv"  # legacy .env still works
    assert get_settings().anthropic_api_key == "sk-ant-from-vault"  # the real .env path, vault wins anyway
    assert Settings(_env_file=dotenv, anthropic_api_key="init").anthropic_api_key == "init"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-from-env")
    assert Settings(_env_file=dotenv).anthropic_api_key == "sk-ant-from-env"  # env beats vault
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert Settings(_env_file=None).anthropic_api_key == ""  # hermetic: no .env, no vault
    monkeypatch.setenv(sv.BACKEND_ENV, "off")
    assert Settings(_env_file=dotenv).anthropic_api_key == "from-dotenv"


# --- CLI -------------------------------------------------------------------------------------------------------------


def test_cli_set_list_delete_from_stdin(vault, kr, capsys, monkeypatch):
    _stdin(monkeypatch, f"  {ROBOTICS}\n")
    assert sv.main(["set", "perceptron_robotics_api_key", "--from-stdin"]) == 0
    assert kr.store[("picky", "PERCEPTRON_ROBOTICS_API_KEY")] == ROBOTICS
    out = capsys.readouterr().out
    assert "saved PERCEPTRON_ROBOTICS_API_KEY" in out and ROBOTICS not in out

    assert sv.main(["list"]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith("PERCEPTRON_ROBOTICS_API_KEY"))
    assert "yes" in row and "keychain" in row and "••••1234" in row and ROBOTICS not in out
    assert next(line for line in out.splitlines() if line.startswith("PICKY_SIM_TOKEN")).split()[1] == "dev"

    assert sv.main(["delete", "PERCEPTRON_ROBOTICS_API_KEY"]) == 0
    assert "from keychain" in capsys.readouterr().out
    assert sv.main(["list", "-v"]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith("PERCEPTRON_ROBOTICS_API_KEY"))
    assert row.split()[1] == "-" and "robotics.perceptron.inc" in out


def test_cli_set_with_hidden_prompt(vault, kr, monkeypatch, capsys):
    prompts: list[str] = []
    monkeypatch.setattr(sv.getpass, "getpass", lambda prompt: prompts.append(prompt) or "sk-ant-typed-value-xyz")
    assert sv.main(["set", "ANTHROPIC_API_KEY"]) == 0
    assert kr.store[("picky", "ANTHROPIC_API_KEY")] == "sk-ant-typed-value-xyz"
    assert "hidden" in prompts[0]


def test_cli_rejects_unknown_empty_multiline_and_live_keys(vault, kr, capsys, monkeypatch):
    cases = [
        (["set", "OPENAI_API_KEY", "--from-stdin"], "x", "unknown secret"),
        (["set", "HF_TOKEN", "--from-stdin"], "   \n", "empty value"),
        (["set", "HF_TOKEN", "--from-stdin"], "hf_a\nhf_b", "single line"),
        (["set", "STRIPE_SECRET_KEY", "--from-stdin"], "sk_live_51abcdef", "live keys"),
        (["delete", "NOPE"], "", "unknown secret"),
    ]
    for argv, stdin, message in cases:
        _stdin(monkeypatch, stdin)
        assert sv.main(argv) == 2, argv
        assert message in capsys.readouterr().err
    assert not kr.store
    _stdin(monkeypatch, "not-an-anthropic-key-1234")
    assert sv.main(["set", "ANTHROPIC_API_KEY", "--from-stdin"]) == 0  # stored, with a warning
    assert "usually starts with sk-ant-" in capsys.readouterr().err


def test_cli_never_prints_values_except_env(vault, capsys, caplog, monkeypatch):
    _stdin(monkeypatch, SENTINEL)
    assert sv.main(["set", "ANTHROPIC_API_KEY", "--from-stdin"]) == 0
    for argv in (["list"], ["list", "-v"], ["check", "--arms", "all", "--env", "prod"], ["delete", "HF_TOKEN"]):
        sv.main(argv)
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err + caplog.text
    assert "••••6789" in captured.out

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    assert sv.main(["env"]) == 2  # refuses to dump values onto a terminal
    captured = capsys.readouterr()
    assert SENTINEL not in captured.out + captured.err
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert sv.main(["env", "ANTHROPIC_API_KEY", "HF_TOKEN"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"export ANTHROPIC_API_KEY={SENTINEL}\n"
    assert "warning" in captured.err and SENTINEL not in captured.err


def test_cli_env_quotes_for_the_shell(vault, capsys, monkeypatch):
    vault.set("HF_TOKEN", "hf_with space'and$dollar")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    assert sv.main(["env", "HF_TOKEN"]) == 0
    line = capsys.readouterr().out.strip()
    out = subprocess.run(["sh", "-c", f'{line}; printf %s "$HF_TOKEN"'], capture_output=True, text=True, check=True)
    assert out.stdout == "hf_with space'and$dollar"


def test_cli_check_exit_codes(vault, capsys):
    assert sv.main(["check", "--arms", "", "--env", "dev"]) == 0
    assert sv.main(["check", "--arms", "messaging,routing", "--env", "dev"]) == 1
    assert "MISSING  PHOTON_API_KEY" in capsys.readouterr().out
    vault.set("PHOTON_API_KEY", "photon-key-123456789")
    assert sv.main(["check", "--arms", "messaging", "--env", "dev"]) == 0
    assert sv.main(["check", "--arms", "", "--env", "prod"]) == 1  # dev defaults for the sim token / signing key
    out = capsys.readouterr().out
    assert "MISSING  PICKY_SIM_TOKEN" in out and "MISSING  PICKY_SIGNING_KEY" in out


def test_module_entrypoint_runs_isolated(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in sv.NAMES}
    env.update(HOME=str(tmp_path), PICKY_SECRETS_BACKEND="file")
    root = Path(sv.__file__).resolve().parents[1]
    res = subprocess.run(
        [sys.executable, "-m", "utils.secrets", "list"], cwd=root, env=env, capture_output=True, text=True
    )
    assert res.returncode == 0, res.stderr
    assert "PERCEPTRON_ROBOTICS_API_KEY" in res.stdout and str(tmp_path) in res.stdout


# --- team bundle (age) -----------------------------------------------------------------------------------------------


def test_identity_created_private_and_stable(vault, capsys):
    pytest.importorskip("pyrage")
    assert sv.main(["identity"]) == 0
    out = capsys.readouterr().out
    assert vault.identity_file.is_file() and _mode(vault.identity_file) == 0o600
    assert "AGE-SECRET-KEY" not in out
    public = next(line for line in out.splitlines() if line.startswith("age1"))
    assert sv.main(["identity"]) == 0
    out = capsys.readouterr().out
    assert "existing" in out and public in out


def test_push_pull_roundtrip_merge_and_conflicts(tmp_path, repo, capsys, caplog):
    pytest.importorskip("pyrage")
    alice = sv.Vault(keyring_backend=MemoryKeyring(), home=tmp_path / "alice", repo_root=repo)
    bob = sv.Vault(keyring_backend=None, home=tmp_path / "bob", repo_root=repo)  # vhagar-style file vault
    eve = sv.Vault(keyring_backend=MemoryKeyring(), home=tmp_path / "eve", repo_root=repo)
    pub_a, created = sv.create_identity(alice)
    assert created
    pub_b, _ = sv.create_identity(bob)
    sv.create_identity(eve)
    (repo / "secrets" / "recipients.txt").write_text(f"# team\n{pub_a}  # alice mac\n\n{pub_b} # bob vhagar\n")

    alice.set("PERCEPTRON_ROBOTICS_API_KEY", ROBOTICS)
    alice.set("ANTHROPIC_API_KEY", SENTINEL)
    assert sv.main(["push"], vault=alice) == 0
    blob = (repo / "secrets" / "team.age").read_text()
    assert blob.startswith("-----BEGIN AGE ENCRYPTED FILE-----")
    assert ROBOTICS not in blob and SENTINEL not in blob and "PERCEPTRON" not in blob

    assert sv.main(["pull"], vault=bob) == 0
    assert bob.get_local("PERCEPTRON_ROBOTICS_API_KEY") == (ROBOTICS, "file")
    assert bob.get_local("ANTHROPIC_API_KEY") == (SENTINEL, "file")

    assert sv.main(["pull"], vault=eve) == 2  # not a recipient
    assert "isn't a recipient" in capsys.readouterr().err

    # pull never overwrites a different local value without --force
    bob.set("ANTHROPIC_API_KEY", "sk-ant-bob-local-different")
    assert sv.main(["pull"], vault=bob) == 0
    assert "kept your different local value for: ANTHROPIC_API_KEY" in capsys.readouterr().out
    assert bob.get_local("ANTHROPIC_API_KEY")[0] == "sk-ant-bob-local-different"
    assert sv.main(["pull", "--force"], vault=bob) == 0
    assert bob.get_local("ANTHROPIC_API_KEY")[0] == SENTINEL

    # push merges: bob adds a key, alice keeps hers and receives his
    bob.set("STRIPE_SECRET_KEY", "sk_test_bob_123456789")
    assert sv.main(["push"], vault=bob) == 0
    assert "added: STRIPE_SECRET_KEY" in capsys.readouterr().out
    assert sv.main(["pull"], vault=alice) == 0
    assert alice.get_local("STRIPE_SECRET_KEY")[0] == "sk_test_bob_123456789"

    # rotation: explicit NAME overwrites; a stale teammate's plain push is refused instead of reverting it
    bob.set("ANTHROPIC_API_KEY", "sk-ant-rotated-by-bob-0000")
    assert sv.main(["push", "ANTHROPIC_API_KEY"], vault=bob) == 0
    alice.set("HF_TOKEN", "hf_alice_123456789")
    assert sv.main(["push"], vault=alice) == 1
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert sv.main(["push", "HF_TOKEN"], vault=alice) == 0
    team = sv.decrypt_bundle(bob)
    assert team["ANTHROPIC_API_KEY"] == "sk-ant-rotated-by-bob-0000"
    assert team["HF_TOKEN"] == "hf_alice_123456789" and team["PERCEPTRON_ROBOTICS_API_KEY"] == ROBOTICS

    captured = capsys.readouterr()
    everything = captured.out + captured.err + caplog.text
    for secret in (SENTINEL, ROBOTICS, "sk-ant-rotated-by-bob-0000", "hf_alice_123456789", "AGE-SECRET-KEY"):
        assert secret not in everything


def test_push_errors_are_clear(vault, repo, capsys):
    pytest.importorskip("pyrage")
    sv.create_identity(vault)
    vault.set("HF_TOKEN", "hf_value_123456789")
    assert sv.main(["push"]) == 2
    assert "no recipients" in capsys.readouterr().err
    (repo / "secrets" / "recipients.txt").write_text("ssh-ed25519 AAAA bob\n")
    assert sv.main(["push"]) == 2
    assert "expected an age public key" in capsys.readouterr().err
    (repo / "secrets" / "recipients.txt").write_text("age1notavalidkey\n")
    assert sv.main(["push"]) == 2
    assert "invalid age public key" in capsys.readouterr().err
    assert sv.main(["push", "GEMINI_API_KEY"]) == 2
    assert "not in your local vault" in capsys.readouterr().err
    assert sv.main(["pull"]) == 2
    assert "no team bundle" in capsys.readouterr().err


# --- review hardening: nothing echoes a value, dev defaults, push/pull edges --------------------------------------


def _exit_code(argv: list[str]) -> int:
    try:
        return sv.main(argv)
    except SystemExit as e:  # argparse usage errors
        return int(e.code or 0)


def test_cli_never_echoes_values_typed_in_the_wrong_place(vault, kr, capsys):
    for argv in (
        ["set", "HF_TOKEN", "hf_TypedOnTheCommandLine0000"],  # value as an argument
        ["set", "hf_TypedOnTheCommandLine0000", "--from-stdin"],  # value where the NAME goes
        ["hf_TypedOnTheCommandLine0000"],  # value as the command
        ["list", "hf_TypedOnTheCommandLine0000"],  # stray extra argument
    ):
        assert _exit_code(argv) == 2, argv
        captured = capsys.readouterr()
        assert "TypedOnTheCommandLine" not in captured.out + captured.err, argv
    assert not kr.store
    assert sv.main(["set", "OPENAI_API_KEY", "--from-stdin"]) == 2
    assert "'OPENAI_API_KEY'" in capsys.readouterr().err  # a NAME-shaped typo is still shown


def test_reprs_never_show_values(vault, tmp_path, monkeypatch):
    r = sv.Resolved("ANTHROPIC_API_KEY", SENTINEL, "env")
    assert SENTINEL not in repr(r) and r.is_set
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    s = Settings(_env_file=None)
    assert s.anthropic_api_key == SENTINEL
    assert SENTINEL not in repr(s) and SENTINEL not in str(s) and "gateway_port=8000" in repr(s)


def test_settings_survive_an_unreadable_vault_file(home, repo, tmp_path, caplog):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads mode-000 files")
    v = sv.Vault(keyring_backend=None, home=home, repo_root=repo)
    v.set("HF_TOKEN", "hf_unreadable_123456789")
    sv.set_vault(v)
    os.chmod(v.secrets_file, 0)
    try:
        dotenv = tmp_path / "x.env"
        dotenv.write_text("HF_TOKEN=hf_from_dotenv\n")
        assert Settings(_env_file=dotenv).hf_token == "hf_from_dotenv"  # degrades, doesn't crash the gateway
        assert "key vault read failed for HF_TOKEN" in caplog.text
    finally:
        os.chmod(v.secrets_file, 0o600)


def test_chained_or_plaintext_keyrings_are_never_used():
    class Chainer:
        __module__ = "keyring.backends.chainer"

        def __init__(self, backends):
            self.backends = backends

    class Plaintext(MemoryKeyring):
        __module__ = "keyrings.alt.file"

    real = MemoryKeyring()
    assert sv._pick_keyring(Chainer([Plaintext(), real])) is real
    assert sv._pick_keyring(Chainer([Plaintext()])) is None
    assert sv._pick_keyring(Plaintext()) is None
    assert sv._pick_keyring(real) is real


def test_dev_defaults_count_as_dev_not_set(vault, repo, capsys):
    (repo / ".env").write_text("PICKY_SIM_TOKEN=dev-sim-token\nPICKY_SIGNING_KEY=dev-signing-key\n")  # cp .env.example
    assert sv.main(["list"]) == 0
    row = next(line for line in capsys.readouterr().out.splitlines() if line.startswith("PICKY_SIM_TOKEN"))
    assert row.split()[1:3] == ["dev", ".env"]
    assert sv.main(["check", "--arms", "", "--env", "dev"]) == 0
    assert sv.main(["check", "--arms", "", "--env", "prod"]) == 1  # the dev default doesn't pass a prod check
    assert "MISSING  PICKY_SIM_TOKEN" in capsys.readouterr().out
    assert vault.shareable() == []  # and is never pushed to the team


def test_push_shares_vault_and_dotenv_but_not_shell_env(tmp_path, repo, capsys, monkeypatch):
    pytest.importorskip("pyrage")
    lead = sv.Vault(keyring_backend=MemoryKeyring(), home=tmp_path / "lead", repo_root=repo)
    pub, _ = sv.create_identity(lead)
    (repo / "secrets" / "recipients.txt").write_text(f"{pub}  # lead macbook\n")
    (repo / ".env").write_text("HF_TOKEN=hf_from_dotenv_123456789\nSTRIPE_SECRET_KEY=sk_live_nope_123456789\n")
    lead.set("PERCEPTRON_ROBOTICS_API_KEY", ROBOTICS)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)  # e.g. exported in ~/.zshrc: personal, not shared
    assert sv.main(["push"], vault=lead) == 0
    captured = capsys.readouterr()
    assert "lead macbook" in captured.out and "<- you" in captured.out
    assert "HF_TOKEN (from .env)" in captured.out
    assert "not shared (only in your shell env): ANTHROPIC_API_KEY" in captured.out
    assert "not pushing STRIPE_SECRET_KEY (from .env): live keys" in captured.err
    assert SENTINEL not in captured.out + captured.err and "sk_live_nope" not in captured.out + captured.err
    team = sv.decrypt_bundle(lead)
    assert team == {"PERCEPTRON_ROBOTICS_API_KEY": ROBOTICS, "HF_TOKEN": "hf_from_dotenv_123456789"}
    assert sv.main(["push", "ANTHROPIC_API_KEY"], vault=lead) == 2
    assert "only in your shell environment" in capsys.readouterr().err


def test_pull_from_stdin_and_clear_errors_for_damaged_bundles(tmp_path, repo, capsys, monkeypatch):
    pytest.importorskip("pyrage")
    lead = sv.Vault(keyring_backend=MemoryKeyring(), home=tmp_path / "lead", repo_root=repo)
    mate = sv.Vault(keyring_backend=None, home=tmp_path / "mate", repo_root=tmp_path / "elsewhere")
    pub_l, _ = sv.create_identity(lead)
    pub_m, _ = sv.create_identity(mate)
    (repo / "secrets" / "recipients.txt").write_text(f"{pub_l} # lead\n{pub_m} # mate\n")
    lead.set("PERCEPTRON_ROBOTICS_API_KEY", ROBOTICS)
    assert sv.main(["push"], vault=lead) == 0
    blob = (repo / "secrets" / "team.age").read_bytes()

    # pull before the PR merges: git show origin/BRANCH:secrets/team.age | ... pull --bundle -
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(blob)))
    assert sv.main(["pull", "--bundle", "-"], vault=mate) == 0
    assert mate.get_local("PERCEPTRON_ROBOTICS_API_KEY") == (ROBOTICS, "file")
    capsys.readouterr()

    conflicted = tmp_path / "conflict.age"
    conflicted.write_bytes(b"<<<<<<< HEAD\n" + blob + b"=======\n" + blob + b">>>>>>> origin/main\n")
    assert sv.main(["pull", "--bundle", str(conflicted)], vault=mate) == 2
    assert "merge-conflict markers" in capsys.readouterr().err
    damaged = tmp_path / "damaged.age"
    damaged.write_bytes(blob[:120])
    assert sv.main(["pull", "--bundle", str(damaged)], vault=mate) == 2
    err = capsys.readouterr().err
    assert "not a valid age bundle" in err and "isn't a recipient" not in err


def test_recipients_file_rejects_a_pasted_private_key(vault, repo, capsys):
    pytest.importorskip("pyrage")
    sv.create_identity(vault)
    vault.set("HF_TOKEN", "hf_value_123456789")
    secret_line = vault.identity_file.read_text().splitlines()[-1]
    (repo / "secrets" / "recipients.txt").write_text(secret_line + "  # oops\n")
    assert sv.main(["push"]) == 2
    err = capsys.readouterr().err
    assert "PRIVATE age key" in err and secret_line not in err and not vault.bundle_file.exists()
