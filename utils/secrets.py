"""Team key vault: where picky's API keys live on each machine, and how the team shares them.

Every secret picky knows is in :data:`REGISTRY` (fixed list; unknown names are rejected). Each name resolves, first
hit wins::

    process env  >  OS keychain (keyring, service "picky")  >  ~/.config/picky/secrets.env  >  repo .env (legacy)

The keychain is the macOS Keychain on Macs (Secret Service on a Linux desktop). Where no usable keychain exists
(headless Linux such as the GPU host), the per-user file ``~/.config/picky/secrets.env`` (dir 700, file 600) is
used instead, automatically. ``utils.config.Settings`` reads the vault through :func:`settings_sources`, so
``settings.anthropic_api_key`` just works; ``Settings(_env_file=None)`` stays hermetic (env + defaults only).

CLI (values are never logged or printed, except by ``env``, which exists to feed ``eval``)::

    uv run python -m utils.secrets set PERCEPTRON_ROBOTICS_API_KEY      # hidden prompt (or --from-stdin)
    uv run python -m utils.secrets list                                 # name, set?, source, ••••last4
    uv run python -m utils.secrets delete NAME | env [NAME...] | check
    uv run --extra secrets python -m utils.secrets identity | push [NAME...] | pull

Team bundle: ``secrets/team.age`` is committed, age-encrypted to every public key in ``secrets/recipients.txt``.
``identity`` creates your age key (``~/.config/picky/age.key``), ``push`` encrypts, ``pull`` decrypts into your local
vault. The age bindings (``pyrage``) are in the optional ``secrets`` extra; everything else needs base deps only.

Process-env knob (not in Settings, not read from ``.env``): ``PICKY_SECRETS_BACKEND=auto|keyring|file|off``.
"""

from __future__ import annotations

import argparse
import getpass
import io
import os
import shlex
import stat
import sys
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

from utils.log import get_logger

log = get_logger(__name__)

SERVICE = "picky"
"""Keyring service name: entries are (service="picky", username=<SECRET NAME>)."""
BACKEND_ENV = "PICKY_SECRETS_BACKEND"
BUNDLE_HEADER = "# picky team secrets bundle v1 (decrypt with: make secrets-pull)"
MASK = "••••"
_REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class SecretSpec:
    """One known secret. ``needed_by`` = arms whose REAL impl needs it (``check`` fails without it)."""

    name: str
    description: str
    where: str
    used_by: tuple[str, ...] = ()
    needed_by: tuple[str, ...] = ()
    dev_default: bool = False
    """Settings has a dev-only default; ``check`` requires a real value when ``PICKY_ENV`` is not dev."""
    prefixes: tuple[str, ...] = ()
    """Expected value prefixes; ``set`` warns (never refuses) on a mismatch, e.g. a key pasted into the wrong name."""
    forbidden_prefixes: tuple[str, ...] = ()
    """``set`` refuses these, e.g. live Stripe keys (test/sandbox payments only)."""


REGISTRY: dict[str, SecretSpec] = {
    s.name: s
    for s in (
        SecretSpec(
            "ANTHROPIC_API_KEY",
            "Claude API: agent, robot brains, shopper chat, vision observe",
            "https://platform.claude.com -> API keys",
            used_by=("agent", "perception", "recommender"),
            needed_by=("agent", "perception"),
            prefixes=("sk-ant-",),
        ),
        SecretSpec(
            "GEMINI_API_KEY",
            "Gemini API: LLM planner when the provider is gemini (vla-loop)",
            "https://aistudio.google.com/apikey",
            used_by=("agent",),
        ),
        SecretSpec(
            "PERCEPTRON_ROBOTICS_API_KEY",
            "Perceptron Isaac SO100 participant API (team key)",
            "Perceptron's DubHacks team key for https://robotics.perceptron.inc (one per team; ask the lead)",
            used_by=("sim", "robot"),
        ),
        SecretSpec(
            "PERCEPTRON_API_KEY",
            "Perceptron hosted vision (verifier grounding/OCR); optional",
            "https://api.perceptron.inc (platform API key); unset = fake / ground truth",
            used_by=("perception",),
        ),
        SecretSpec(
            "STRIPE_SECRET_KEY",
            "Stripe TEST secret key (sk_test_...): checkout",
            "https://dashboard.stripe.com/test/apikeys (test mode only)",
            used_by=("trust",),
            needed_by=("trust",),
            prefixes=("sk_test_", "rk_test_"),
            forbidden_prefixes=("sk_live_", "rk_live_"),
        ),
        SecretSpec(
            "VISA_API_KEY",
            "Visa Intelligent Commerce sandbox API key; optional",
            "https://developer.visa.com (sandbox project); blank = Stripe test",
            used_by=("trust",),
        ),
        SecretSpec(
            "VISA_SHARED_SECRET",
            "Visa sandbox shared secret; optional",
            "https://developer.visa.com (same sandbox project as VISA_API_KEY)",
            used_by=("trust",),
        ),
        SecretSpec(
            "PHOTON_API_KEY",
            "Photon 2.0 messaging agent (iMessage line)",
            "https://app.beta.photon.codes -> project -> API key",
            used_by=("messaging",),
            needed_by=("messaging",),
        ),
        SecretSpec(
            "DEEPGRAM_API_KEY",
            "Deepgram speech-to-text; optional (browser speech otherwise)",
            "https://console.deepgram.com (free credit)",
            used_by=("agent",),
        ),
        SecretSpec(
            "PICKY_SIM_TOKEN",
            "Shared token the Isaac worker sends on /ws/sim",
            "make one: python -c 'import secrets; print(secrets.token_urlsafe(24))' (same on gateway + vhagar)",
            used_by=("gateway", "sim"),
            dev_default=True,
        ),
        SecretSpec(
            "PICKY_SIGNING_KEY",
            "HMAC key for mandates, cart hashes, receipts",
            "make one: python -c 'import secrets; print(secrets.token_urlsafe(32))'",
            used_by=("trust",),
            dev_default=True,
        ),
        SecretSpec(
            "HF_TOKEN",
            "Hugging Face token: gated model/dataset downloads",
            "https://huggingface.co/settings/tokens (read scope)",
            used_by=("perception", "recommender", "sim"),
            prefixes=("hf_",),
        ),
    )
}

NAMES: tuple[str, ...] = tuple(REGISTRY)


class VaultError(Exception):
    """A user-facing vault problem (unknown name, empty value, no identity, ...). The message never has a value."""


@dataclass(frozen=True)
class Resolved:
    """Where a name resolved. ``source``: env | keychain | file | .env | None (unset). ``value`` stays in memory."""

    name: str
    value: str | None
    source: str | None

    @property
    def is_set(self) -> bool:
        return bool(self.value)


def mask(value: str | None) -> str:
    """``••••`` + last 4 characters, and only for values long enough that 4 characters don't give them away."""
    if not value:
        return ""
    return MASK + value[-4:] if len(value) >= 12 else MASK


def spec(name: str) -> SecretSpec:
    """Registry entry for ``name`` (case-insensitive); :class:`VaultError` for unknown names."""
    key = (name or "").strip().upper()
    if key not in REGISTRY:
        raise VaultError(f"unknown secret {name!r}; known names: {', '.join(NAMES)}")
    return REGISTRY[key]


def clean_value(name: str, raw: str | None) -> str:
    """Strip whitespace and validate a value for ``name``; raises :class:`VaultError` (message has no value)."""
    s = spec(name)
    value = (raw or "").strip()
    if not value:
        raise VaultError(f"{s.name}: empty value, nothing stored")
    if any(ch in value for ch in "\r\n\x00") or not value.isprintable():
        raise VaultError(f"{s.name}: value must be a single line of printable characters")
    if any(value.startswith(p) for p in s.forbidden_prefixes):
        raise VaultError(f"{s.name}: live keys are not allowed (test/sandbox only); use a test-mode key")
    return value


def prefix_warning(name: str, value: str) -> str | None:
    """A hint when ``value`` doesn't look like ``name`` (wrong field pasted); never includes the value."""
    s = spec(name)
    if s.prefixes and not any(value.startswith(p) for p in s.prefixes):
        expected = " or ".join(s.prefixes)
        return f"{s.name} usually starts with {expected}; stored anyway, double-check you pasted the right key"
    return None


# --- dotenv-style files (secrets.env, the team bundle plaintext) ----------------------------------------------------

_SAFE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.:/+=@~,%^")


def _quote(value: str) -> str:
    if value and set(value) <= _SAFE_CHARS:
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def dumps_env(data: Mapping[str, str], header: str = "") -> str:
    """``NAME=value`` lines that python-dotenv parses back exactly (single-quoted when needed)."""
    lines = [header] if header else []
    lines += [f"{k}={_quote(v)}" for k, v in sorted(data.items())]
    return "\n".join(lines) + "\n"


def loads_env(text: str, *, interpolate: bool = False) -> dict[str, str]:
    """Parse dotenv text; blank values are dropped."""
    from dotenv import dotenv_values  # python-dotenv ships with pydantic-settings

    return {k: v for k, v in dotenv_values(stream=io.StringIO(text), interpolate=interpolate).items() if v}


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix" and stat.S_IMODE(path.stat().st_mode) & 0o077:
        os.chmod(path, 0o700)


def _check_private_file(path: Path) -> None:
    """Tighten a group/world-readable secrets file to 600 (warn, don't refuse)."""
    if os.name != "posix":
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        log.warning("%s was mode %o; tightened to 600", path, mode)
        os.chmod(path, 0o600)


def _atomic_write(path: Path, text: str, mode: int = 0o600) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # mkstemp = 0600
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# --- keyring ---------------------------------------------------------------------------------------------------------

_AUTO: Any = object()


def _keyring_label(kr: Any) -> str:
    mod = type(kr).__module__
    if mod.endswith("macOS"):
        return "macOS Keychain"
    if "SecretService" in mod:
        return "Secret Service"
    if mod.endswith("Windows"):
        return "Windows Credential Locker"
    return f"keyring ({type(kr).__name__})"


def _usable_keyring(kr: Any) -> bool:
    """A real, persistent backend: not keyring's fail/null ones, not keyrings.alt (plaintext or prompt-y)."""
    if kr is None:
        return False
    mod = type(kr).__module__
    if mod.startswith("keyrings.alt") or mod.endswith(("backends.fail", "backends.null")):
        return False
    try:
        return float(type(kr).priority) > 0
    except Exception:  # backends signal "not viable" by raising from priority
        return False


def _headless_linux() -> bool:
    return sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


# --- the vault -------------------------------------------------------------------------------------------------------


class Vault:
    """Resolves and stores registry secrets on this machine.

    Args (all for tests; defaults are the real machine):
        keyring_backend: a keyring-like object (``get_password`` / ``set_password`` / ``delete_password``), ``None``
            for no keychain, or omitted to auto-detect ``keyring.get_keyring()``.
        home: user home (``~/.config/picky`` lives under it); default ``Path.home()`` at call time.
        repo_root: monorepo root (repo ``.env``, ``secrets/``).
    """

    def __init__(self, *, keyring_backend: Any = _AUTO, home: Path | None = None, repo_root: Path | None = None):
        self._kr_arg = keyring_backend
        self._kr_cache: Any = _AUTO
        self._kr_broken = False
        self._home = Path(home) if home is not None else None
        self.repo_root = Path(repo_root) if repo_root is not None else _REPO_ROOT

    # paths ----------------------------------------------------------------------------------------------------------
    @property
    def config_dir(self) -> Path:
        return (self._home or Path.home()) / ".config" / "picky"

    @property
    def secrets_file(self) -> Path:
        return self.config_dir / "secrets.env"

    @property
    def identity_file(self) -> Path:
        return self.config_dir / "age.key"

    @property
    def repo_env(self) -> Path:
        return self.repo_root / ".env"

    @property
    def bundle_file(self) -> Path:
        return self.repo_root / "secrets" / "team.age"

    @property
    def recipients_file(self) -> Path:
        return self.repo_root / "secrets" / "recipients.txt"

    # backend selection ----------------------------------------------------------------------------------------------
    @staticmethod
    def requested_backend() -> str:
        raw = os.environ.get(BACKEND_ENV, "auto").strip().lower() or "auto"
        if raw in {"off", "none", "0", "false", "disabled"}:
            return "off"
        if raw not in {"auto", "keyring", "file"}:
            log.warning("%s=%r is not auto|keyring|file|off; using auto", BACKEND_ENV, raw)
            return "auto"
        return raw

    def _keyring(self) -> Any:
        """The keyring backend to use, or None (file fallback)."""
        if self._kr_broken:
            return None
        wanted = self.requested_backend()
        if wanted in {"off", "file"}:
            return None
        if self._kr_arg is not _AUTO:
            return self._kr_arg
        if self._kr_cache is _AUTO:
            kr = None
            if wanted == "keyring" or not _headless_linux():
                try:
                    import keyring

                    candidate = keyring.get_keyring()
                    kr = candidate if _usable_keyring(candidate) else None
                except Exception as e:  # keyring missing or broken -> file fallback
                    log.debug("keyring unavailable: %s", type(e).__name__)
            if kr is None and wanted == "keyring":
                log.warning("%s=keyring but no usable keyring backend; using %s", BACKEND_ENV, self.secrets_file)
            self._kr_cache = kr
        return self._kr_cache

    @property
    def mode(self) -> str:
        """``keyring`` | ``file`` | ``off``: where ``set`` writes."""
        if self.requested_backend() == "off":
            return "off"
        return "keyring" if self._keyring() is not None else "file"

    def describe(self) -> str:
        """Human description of the local store (no values)."""
        if self.mode == "off":
            return f"vault off ({BACKEND_ENV}=off): env and repo .env only"
        if self.mode == "keyring":
            return f"{_keyring_label(self._keyring())} (service {SERVICE!r}), fallback file {self.secrets_file}"
        return f"file {self.secrets_file} (no usable OS keychain here)"

    def _keyring_failed(self, op: str, name: str, exc: BaseException) -> None:
        log.warning(
            "keychain %s failed for %s (%s); using %s for this session", op, name, type(exc).__name__, self.secrets_file
        )
        self._kr_broken = True

    # file store -----------------------------------------------------------------------------------------------------
    def _read_file(self) -> dict[str, str]:
        path = self.secrets_file
        if not path.is_file():
            return {}
        _check_private_file(path)
        return loads_env(path.read_text(encoding="utf-8"))

    def _write_file(self, data: Mapping[str, str]) -> None:
        _ensure_private_dir(self.config_dir)
        header = "# picky secrets (utils.secrets file fallback). Mode 600. Never commit, copy or paste this file."
        _atomic_write(self.secrets_file, dumps_env(data, header), 0o600)

    # reads ----------------------------------------------------------------------------------------------------------
    def get_local(self, name: str) -> tuple[str | None, str | None]:
        """(value, source) from the local vault only: keychain, then the file. ``(None, None)`` when unset."""
        s = spec(name)
        if self.mode == "off":
            return None, None
        kr = self._keyring()
        if kr is not None:
            try:
                value = kr.get_password(SERVICE, s.name)
            except Exception as e:
                self._keyring_failed("read", s.name, e)
            else:
                if value:
                    return value, "keychain"
        value = self._read_file().get(s.name)
        return (value, "file") if value else (None, None)

    def local_values(self, names: Iterable[str] | None = None) -> dict[str, str]:
        """Every set registry secret from the local vault (keychain + file), by name."""
        out: dict[str, str] = {}
        for n in names or NAMES:
            value, _ = self.get_local(n)
            if value:
                out[spec(n).name] = value
        return out

    def _repo_env_values(self) -> dict[str, str]:
        if not self.repo_env.is_file():
            return {}
        try:
            return loads_env(self.repo_env.read_text(encoding="utf-8"), interpolate=True)
        except Exception as e:  # a malformed .env must not break resolution
            log.warning("could not parse %s (%s); ignoring it", self.repo_env, type(e).__name__)
            return {}

    def resolve(self, name: str) -> Resolved:
        """Full resolution: env > keychain > file > repo .env. An env var set to "" still wins (as in Settings)."""
        s = spec(name)
        if s.name in os.environ:
            return Resolved(s.name, os.environ[s.name], "env")
        value, source = self.get_local(s.name)
        if value:
            return Resolved(s.name, value, source)
        value = self._repo_env_values().get(s.name)
        if value:
            return Resolved(s.name, value, ".env")
        return Resolved(s.name, None, None)

    def resolve_all(self) -> list[Resolved]:
        repo = self._repo_env_values()
        out = []
        for n in NAMES:
            if n in os.environ:
                out.append(Resolved(n, os.environ[n], "env"))
                continue
            value, source = self.get_local(n)
            if not value and repo.get(n):
                value, source = repo[n], ".env"
            out.append(Resolved(n, value or None, source if value else None))
        return out

    # writes ---------------------------------------------------------------------------------------------------------
    def set(self, name: str, value: str) -> str:
        """Store ``value`` (validated) in the local vault; returns where it went ("keychain" | "file")."""
        s = spec(name)
        value = clean_value(s.name, value)
        if self.mode == "off":
            raise VaultError(f"the vault is off ({BACKEND_ENV}=off); unset it to store secrets")
        kr = self._keyring()
        if kr is not None:
            try:
                kr.set_password(SERVICE, s.name, value)
            except Exception as e:
                self._keyring_failed("write", s.name, e)
            else:
                data = self._read_file()
                if data.pop(s.name, None) is not None:  # don't leave a stale copy behind in the fallback file
                    self._write_file(data)
                return "keychain"
        data = self._read_file()
        data[s.name] = value
        self._write_file(data)
        return "file"

    def delete(self, name: str) -> list[str]:
        """Remove ``name`` from the keychain and the file; returns the stores it was removed from."""
        s = spec(name)
        removed: list[str] = []
        kr = self._keyring()
        if kr is not None:
            try:
                if kr.get_password(SERVICE, s.name) is not None:
                    kr.delete_password(SERVICE, s.name)
                    removed.append("keychain")
            except Exception as e:
                self._keyring_failed("delete", s.name, e)
        data = self._read_file()
        if data.pop(s.name, None) is not None:
            self._write_file(data)
            removed.append("file")
        return removed


_default_vault: Vault | None = None


def get_vault() -> Vault:
    """The process-wide vault (real machine paths, auto-detected keychain)."""
    global _default_vault
    if _default_vault is None:
        _default_vault = Vault()
    return _default_vault


def set_vault(vault: Vault | None) -> None:
    """Replace the process-wide vault (tests: an in-memory keyring + tmp home)."""
    global _default_vault
    _default_vault = vault


def reset_vault() -> None:
    set_vault(None)


# --- pydantic-settings integration -----------------------------------------------------------------------------------


class VaultSettingsSource(PydanticBaseSettingsSource):
    """Settings source for registry fields from the local vault (keychain, then ~/.config/picky/secrets.env)."""

    def __init__(self, settings_cls: type[BaseSettings], vault: Vault | None = None):
        super().__init__(settings_cls)
        self._vault = vault

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        if field_name.upper() not in REGISTRY:
            return None, field_name, False
        value, _ = (self._vault or get_vault()).get_local(field_name)
        return value, field_name, False

    def __call__(self) -> dict[str, Any]:
        vault = self._vault or get_vault()
        if vault.mode == "off":
            return {}
        out: dict[str, Any] = {}
        for field_name in self.settings_cls.model_fields:
            if field_name.upper() in REGISTRY:
                value, _ = vault.get_local(field_name)
                if value:
                    out[field_name] = value
        return out


def settings_sources(
    settings_cls: type[BaseSettings],
    init_settings: PydanticBaseSettingsSource,
    env_settings: PydanticBaseSettingsSource,
    dotenv_settings: PydanticBaseSettingsSource,
    file_secret_settings: PydanticBaseSettingsSource,
) -> tuple[PydanticBaseSettingsSource, ...]:
    """Source order for ``Settings``: init > env > vault > repo .env > secrets dir.

    ``Settings(_env_file=None)`` (tests) also skips the vault, so it stays env + defaults only.
    """
    if getattr(dotenv_settings, "env_file", None) is None:
        return init_settings, env_settings, dotenv_settings, file_secret_settings
    return init_settings, env_settings, VaultSettingsSource(settings_cls), dotenv_settings, file_secret_settings


# --- team bundle (age) -----------------------------------------------------------------------------------------------


def _pyrage() -> Any:
    try:
        import pyrage
    except ImportError as e:
        raise VaultError(
            "the team bundle needs the 'secrets' extra: uv sync --extra secrets (make secrets-pull/-push do it for you)"
        ) from e
    return pyrage


def create_identity(vault: Vault) -> tuple[str, bool]:
    """Create ``~/.config/picky/age.key`` (600) if missing. Returns (public key, created)."""
    pyrage = _pyrage()
    path = vault.identity_file
    if path.is_file():
        return str(load_identity(vault).to_public()), False
    ident = pyrage.x25519.Identity.generate()
    public = str(ident.to_public())
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    _ensure_private_dir(path.parent)
    _atomic_write(path, f"# created: {created}\n# public key: {public}\n{ident}\n", 0o600)
    return public, True


def load_identity(vault: Vault, path: Path | None = None) -> Any:
    pyrage = _pyrage()
    path = path or vault.identity_file
    if not path.is_file():
        raise VaultError(f"no age identity at {path}; create one with: make secrets-identity")
    _check_private_file(path)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("AGE-SECRET-KEY-"):
            try:
                return pyrage.x25519.Identity.from_str(line)
            except Exception as e:
                raise VaultError(f"{path}: not a valid age identity ({type(e).__name__})") from e
    raise VaultError(f"{path}: no AGE-SECRET-KEY line found")


def read_recipients(path: Path) -> list[str]:
    """Public keys from ``secrets/recipients.txt``: one ``age1...`` per line, ``#`` comments allowed."""
    if not path.is_file():
        return []
    keys: list[str] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        token = line.split()[0]
        if not token.startswith("age1"):
            raise VaultError(f"{path}:{lineno}: expected an age public key (age1...), got {token[:12]!r}")
        keys.append(token)
    return list(dict.fromkeys(keys))


def decrypt_bundle(vault: Vault, identity_path: Path | None = None) -> dict[str, str]:
    pyrage = _pyrage()
    if not vault.bundle_file.is_file():
        raise VaultError(f"no team bundle at {vault.bundle_file} yet; someone runs make secrets-push first")
    ident = load_identity(vault, identity_path)
    try:
        plain = pyrage.decrypt(vault.bundle_file.read_bytes(), [ident])
    except Exception as e:  # pyrage.DecryptError
        raise VaultError(
            f"can't decrypt {vault.bundle_file} with your identity: your public key isn't a recipient yet. Add it to "
            f"{vault.recipients_file}, commit, and ask a teammate who can decrypt to run make secrets-push"
        ) from e
    return loads_env(plain.decode("utf-8"))


def encrypt_bundle(vault: Vault, data: Mapping[str, str], recipients: Sequence[str]) -> None:
    pyrage = _pyrage()
    if not recipients:
        raise VaultError(f"no recipients in {vault.recipients_file}; add age public keys (make secrets-identity)")
    try:
        parsed = [pyrage.x25519.Recipient.from_str(r) for r in recipients]
    except Exception as e:
        raise VaultError(f"{vault.recipients_file}: invalid age public key ({type(e).__name__})") from e
    blob = pyrage.encrypt(dumps_env(data, BUNDLE_HEADER).encode("utf-8"), parsed, armored=True)
    vault.bundle_file.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(vault.bundle_file, blob.decode("ascii"), 0o644)


# --- CLI -------------------------------------------------------------------------------------------------------------


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _cmd_set(vault: Vault, args: argparse.Namespace) -> int:
    s = spec(args.name)
    if args.from_stdin:
        raw = sys.stdin.read()
    else:
        raw = getpass.getpass(f"{s.name} ({s.description}). Paste it, input is hidden: ")
    value = clean_value(s.name, raw)
    where = vault.set(s.name, value)
    store = vault.describe().split(",")[0] if where == "keychain" else str(vault.secrets_file)
    print(f"saved {s.name} to {store}")
    hint = prefix_warning(s.name, value)
    if hint:
        _err(f"warning: {hint}")
    if s.name in os.environ:
        _err(f"note: {s.name} is also set in this shell's environment, which wins over the vault")
    return 0


def _cmd_delete(vault: Vault, args: argparse.Namespace) -> int:
    s = spec(args.name)
    removed = vault.delete(s.name)
    print(f"deleted {s.name} from {' and '.join(removed)}" if removed else f"{s.name} was not in the local vault")
    left = vault.resolve(s.name)
    if left.source in {"env", ".env"}:
        _err(f"note: {s.name} is still set in {left.source} (not managed by the vault)")
    return 0


def _cmd_list(vault: Vault, args: argparse.Namespace) -> int:
    print(f"local vault: {vault.describe()}")
    print("resolution: env > OS keychain > ~/.config/picky/secrets.env > repo .env   (values never shown)")
    rows = [("NAME", "SET", "SOURCE", "VALUE", "DESCRIPTION")]
    for r in vault.resolve_all():
        s = REGISTRY[r.name]
        if r.is_set:
            state = "yes"
        elif r.source == "env":
            state = "blank"
        else:
            state = "dev" if s.dev_default else "-"
        rows.append((r.name, state, r.source or "", mask(r.value), s.description))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    for row in rows:
        print("  ".join(c.ljust(w) for c, w in zip(row[:4], widths, strict=True)) + "  " + row[4])
        if args.verbose and row[0] in REGISTRY:
            s = REGISTRY[row[0]]
            print(" " * (sum(widths) + 8) + f"get it: {s.where}" + (f" | used by: {', '.join(s.used_by)}"))
    ident = "present" if vault.identity_file.is_file() else "none (make secrets-identity)"
    try:
        n_rec = len(read_recipients(vault.recipients_file))
    except VaultError:
        n_rec = -1
    bundle = "present" if vault.bundle_file.is_file() else "not pushed yet"
    print(f"team bundle: secrets/team.age {bundle}, {n_rec} recipient(s); your age identity: {ident}")
    print("dev = unset, Settings uses its dev-only default.  Add one: make secret NAME=<NAME>")
    return 0


def _cmd_env(vault: Vault, args: argparse.Namespace) -> int:
    names = [spec(n).name for n in args.names] or list(NAMES)
    if sys.stdout.isatty() and not args.tty:
        _err('refusing to print secret values to a terminal. Use: eval "$(uv run python -m utils.secrets env)"')
        _err("(or pass --tty if you really mean it)")
        return 2
    _err("warning: printing secret values on stdout for eval; never paste or log this output")
    for n in names:
        r = vault.resolve(n)
        if r.is_set:
            print(f"export {n}={shlex.quote(r.value or '')}")
    return 0


def _cmd_check(vault: Vault, args: argparse.Namespace) -> int:
    if args.arms is None or args.env is None:
        from utils.config import get_settings  # lazy: utils.config imports this module

        settings = get_settings()
    arms = settings.real_arms() if args.arms is None else {a.strip().lower() for a in args.arms.split(",") if a.strip()}
    env = settings.picky_env if args.env is None else args.env
    is_dev = env.lower() in {"dev", "development", "test"}
    missing = 0
    print(f"real arms: {', '.join(sorted(arms)) or '(none: all fakes, no keys needed)'} | PICKY_ENV={env}")
    for r in vault.resolve_all():
        s = REGISTRY[r.name]
        needers = sorted(a for a in s.needed_by if "all" in arms or a in arms)
        if needers:
            why = f"needed by real {', '.join(needers)}"
        elif s.dev_default and not is_dev:
            why = "PICKY_ENV is not dev: replace the dev default"
        else:
            why = ""
        if r.is_set:
            print(f"  ok       {r.name:<28} ({r.source})" + (f"  {why}" if why else ""))
        elif why:
            missing += 1
            print(f"  MISSING  {r.name:<28} {why}. Add it: make secret NAME={r.name}")
        else:
            print(f"  -        {r.name:<28} optional")
    if missing:
        print(f"{missing} required secret(s) missing; those arms fall back to their fakes (GET /health says why).")
        return 1
    print("all required secrets present.")
    return 0


def _cmd_identity(vault: Vault, args: argparse.Namespace) -> int:
    public, created = create_identity(vault)
    print(("created" if created else "existing") + f" age identity: {vault.identity_file} (private, mode 600)")
    print("your public key (safe to share):")
    print(public)
    recipients = read_recipients(vault.recipients_file)
    has_bundle = vault.bundle_file.is_file()
    if public in recipients:
        print("already in secrets/recipients.txt; " + ("run make secrets-pull" if has_bundle else "make secrets-push"))
    elif has_bundle:
        print(
            f"next: add this line to secrets/recipients.txt:  {public}  # <your name> <machine>\n"
            "      commit + push it, then a teammate who can decrypt runs make secrets-push; then you make secrets-pull"
        )
    else:
        print(
            f"next: add this line to secrets/recipients.txt:  {public}  # <your name> <machine>\n"
            "      (no bundle yet: you start it) make secret NAME=..., then make secrets-push and commit secrets/"
        )
    return 0


def _cmd_push(vault: Vault, args: argparse.Namespace) -> int:
    recipients = read_recipients(vault.recipients_file)
    explicit = [spec(n).name for n in args.names]
    local = {r.name: r.value for r in vault.resolve_all() if r.is_set and r.value}
    for n in explicit:
        if n not in local:
            raise VaultError(f"{n} is not set locally; make secret NAME={n} first")
    existing: dict[str, str] = {}
    if vault.bundle_file.is_file() and not args.replace:
        try:
            existing = decrypt_bundle(vault, args.identity)
        except VaultError as e:
            raise VaultError(f"{e}. (Or --replace to overwrite the bundle with only your local secrets.)") from e
    merged = dict(existing)
    added, updated, conflicts = [], [], []
    for name, value in local.items():
        if name not in existing:
            if not explicit or name in explicit:
                merged[name] = value
                added.append(name)
        elif existing[name] != value:
            if name in explicit or args.force:
                merged[name] = value
                updated.append(name)
            elif not explicit:
                conflicts.append(name)
    if conflicts:
        _err("refusing to push: your local value differs from the bundle for: " + ", ".join(sorted(conflicts)))
        _err("  make secrets-pull first (take the team's), or push NAME... / --force to overwrite with yours")
        return 1
    if not merged:
        raise VaultError("nothing to push: no registry secrets set locally (make secret NAME=...)")
    encrypt_bundle(vault, merged, recipients)
    try:
        me = str(load_identity(vault, args.identity).to_public())
    except VaultError:
        me = ""
    if me and me not in recipients:
        _err("warning: your own public key is not in secrets/recipients.txt; you won't be able to decrypt this")
    kept = sorted(set(merged) - set(added) - set(updated))
    rel = vault.bundle_file.relative_to(vault.repo_root)
    print(f"wrote {rel}: {len(merged)} secret(s) for {len(recipients)} recipient(s)")
    for label, names in (("added", added), ("updated", updated), ("kept", kept)):
        if names:
            print(f"  {label}: {', '.join(sorted(names))}")
    print("commit it: git add secrets/team.age && git commit -m 'chore: update team secrets bundle'")
    return 0


def _cmd_pull(vault: Vault, args: argparse.Namespace) -> int:
    if vault.mode == "off":
        raise VaultError(f"the vault is off ({BACKEND_ENV}=off); nothing to pull into")
    data = decrypt_bundle(vault, args.identity)
    stored, same, skipped = [], [], []
    for name, value in sorted(data.items()):
        if name not in REGISTRY:
            _err(f"skipping {name}: not in this checkout's registry (git pull to get the newer utils.secrets)")
            continue
        current, _ = vault.get_local(name)
        if current == value:
            same.append(name)
            continue
        if current and not args.force:
            skipped.append(name)
            continue
        vault.set(name, value)
        stored.append(name)
        env_value = os.environ.get(name)
        if env_value is not None and env_value != value:
            _err(f"note: {name} is also set in this shell's environment, which wins over the vault")
    print(f"pulled {len(data)} secret(s) from {vault.bundle_file.relative_to(vault.repo_root)} into {vault.describe()}")
    for label, names in (("stored", stored), ("unchanged", same)):
        if names:
            print(f"  {label}: {', '.join(names)}")
    if skipped:
        print(f"  kept your different local value for: {', '.join(skipped)} (--force to take the team's)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m utils.secrets",
        description="Picky key vault. Resolution: env > OS keychain > ~/.config/picky/secrets.env > repo .env. "
        "Values are never printed (except by `env`, for eval).",
        epilog="examples:\n"
        "  make secret NAME=PERCEPTRON_ROBOTICS_API_KEY        # hidden prompt\n"
        "  pbpaste | uv run python -m utils.secrets set ANTHROPIC_API_KEY --from-stdin\n"
        "  make secrets                                        # what is set, from where\n"
        "  make secrets-identity && make secrets-pull          # join the team bundle\n"
        '  eval "$(uv run python -m utils.secrets env HF_TOKEN)"  # export for a non-Python tool\n'
        "docs: docs/secrets.md",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    s = sub.add_parser("set", help="store a secret in your local vault (hidden prompt or --from-stdin)")
    s.add_argument("name", metavar="NAME", help="a registry name, e.g. PERCEPTRON_ROBOTICS_API_KEY")
    s.add_argument("--from-stdin", action="store_true", help="read the value from stdin (for piping)")
    s.set_defaults(func=_cmd_set)

    s = sub.add_parser("list", help="table of known secrets: set?, source, masked ••••last4, description")
    s.add_argument("-v", "--verbose", action="store_true", help="also show where to get each key and who uses it")
    s.set_defaults(func=_cmd_list)

    s = sub.add_parser("delete", help="remove a secret from the keychain and the fallback file")
    s.add_argument("name", metavar="NAME")
    s.set_defaults(func=_cmd_delete)

    s = sub.add_parser("env", help="print `export NAME=...` lines for eval (prints values!)")
    s.add_argument("names", nargs="*", metavar="NAME", help="default: every set secret")
    s.add_argument("--tty", action="store_true", help="allow printing to a terminal")
    s.set_defaults(func=_cmd_env)

    s = sub.add_parser("check", help="exit 1 if a key a real arm (PICKY_REAL_ARMS) needs is missing")
    s.add_argument("--arms", default=None, help="comma list instead of PICKY_REAL_ARMS ('all' = every arm)")
    s.add_argument("--env", default=None, help="PICKY_ENV to check against (default from settings)")
    s.set_defaults(func=_cmd_check)

    s = sub.add_parser("identity", help="create/show your age identity (~/.config/picky/age.key) and public key")
    s.set_defaults(func=_cmd_identity)

    s = sub.add_parser("push", help="encrypt local secrets (merged into the bundle) to secrets/recipients.txt")
    s.add_argument("names", nargs="*", metavar="NAME", help="only these, overwriting the bundle's value")
    s.add_argument("--force", action="store_true", help="your local values win over the bundle's")
    s.add_argument("--replace", action="store_true", help="ignore the existing bundle (it can't be decrypted)")
    s.add_argument("--identity", type=Path, default=None, help="age identity file (default ~/.config/picky/age.key)")
    s.set_defaults(func=_cmd_push)

    s = sub.add_parser("pull", help="decrypt secrets/team.age into your local vault")
    s.add_argument("--force", action="store_true", help="overwrite local values that differ from the bundle")
    s.add_argument("--identity", type=Path, default=None, help="age identity file (default ~/.config/picky/age.key)")
    s.set_defaults(func=_cmd_pull)
    return p


def main(argv: Sequence[str] | None = None, vault: Vault | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(vault or get_vault(), args))
    except VaultError as e:
        _err(f"error: {e}")
        return 2
    except (KeyboardInterrupt, EOFError):
        _err("cancelled")
        return 130


if __name__ == "__main__":
    # Run the canonical `utils.secrets` module (not a second `__main__` copy) so utils.config sees the same vault.
    from utils.secrets import main as _main

    sys.exit(_main())
