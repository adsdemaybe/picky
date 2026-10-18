"""Perceptron robotics VLA ("Isaac SO100 participant API", https://robotics.perceptron.inc): typed async client + CLI.

The flow it drives (docs/vla-loop/perceptron-vla.md; spec = the service's ``/openapi.json`` + ``/docs``)::

    team key ─▶ POST /v1/datasets (huggingface repo | upload: /uploads -> PUT signed URLs -> /finalize)
            ─▶ GET /v1/datasets/{id} + /validation          (poll until ready, inspect validation)
            ─▶ POST /v1/jobs (backend, mode, action_horizon, rtc_enabled, steps)  ─▶ logs / metrics (cursors)
            ─▶ checkpoint ─▶ ready deployment ─▶ GET /v1/deployments/{id}/contract (contract_id)
            ─▶ POST /v1/deployments/{id}/sessions (grant: endpoint_url + application/modal session tokens)
    session tokens ─▶ POST {endpoint}/deployments/{id}/sessions (instruction, contract_id, rtc_enabled)
                   ─▶ POST {endpoint}/sessions/{sid}/predict  multipart: metadata JSON + side + wrist images
                   ─▶ action chunk (rows of 6 values: arm degrees, gripper 0-100) ─▶ reset / DELETE

Rules kept here (all from the spec):
- The team key goes ONLY to the control plane. Upload PUTs (signed URL + returned headers), 303 polling URLs and every
  inference call never carry it; ``_KeyGuard`` refuses any request that would (defense in depth).
- ``Idempotency-Key`` is required on POST /v1/datasets and POST /v1/jobs. The default key is derived from the request
  body (same submission = same key = the service replays it, so re-running a command resumes instead of duplicating);
  ``fresh=True`` / ``--fresh`` adds a nonce for a deliberate second submission.
- Errors arrive as ``{"error": {"code", "message", "fields"}}`` -> ``RoboticsAPIError``. 429 honours ``Retry-After``;
  5xx / network errors are retried only for GETs and idempotency-keyed POSTs; authenticated JSON bodies are capped at
  512 KiB before sending; a 303 is polled at its same-origin ``Location`` without credentials.
- Response bodies are untyped in the OpenAPI document, so records keep every field (``extra="allow"``) and expose
  tolerant accessors (``phase``, ``checkpoint_ref`` ...). Session tokens are ``SecretStr`` and never printed.

Key: ``PERCEPTRON_ROBOTICS_API_KEY`` (vault name; Settings field once utils.secrets lands) -> process env -> the key
vault (macOS Keychain service ``picky`` / ``~/.config/picky/secrets.env``) -> repo ``.env``. The Mk1.5 vision key
(``PERCEPTRON_API_KEY``) is a different key and is rejected here. Optional env: ``PERCEPTRON_ROBOTICS_BASE_URL``,
``PERCEPTRON_ROBOTICS_DEPLOYMENT_ID`` (the deployment ``policy_backends.PerceptronBackend`` uses by default).

CLI (one command once the key exists)::

    uv run python -m arms.robot.perceptron_vla pipeline DIR_OR_REPO [--steps 1000] [--instruction "..."]
    uv run python -m arms.robot.perceptron_vla doctor | capabilities | dataset-check DIR | dataset-hf REPO
        | dataset-upload DIR | dataset-status ID | train DATASET_ID [--steps N] | jobs | job ID | cancel ID
        | logs ID [--follow] | metrics ID | checkpoint ID | deployments | contract ID | session DEPLOYMENT_ID
        | predict-test DEPLOYMENT_ID [--side F --wrist F --frames-blurred]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import math
import os
import re
import secrets as _secrets
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from contracts.skill_server import SO101_JOINT_NAMES
from utils.config import get_settings, repo_root
from utils.log import get_logger

log = get_logger(__name__)

# ---- service constants (openapi.json + /docs) -----------------------------------------------------------------------
DEFAULT_BASE_URL = "https://robotics.perceptron.inc"
KEY_NAME = "PERCEPTRON_ROBOTICS_API_KEY"
BASE_URL_ENV = "PERCEPTRON_ROBOTICS_BASE_URL"
DEPLOYMENT_ENV = "PERCEPTRON_ROBOTICS_DEPLOYMENT_ID"
SUPPORTED_EXTENSIONS: tuple[str, ...] = (".json", ".jsonl", ".parquet", ".mp4", ".png", ".jpg", ".jpeg")
MAX_UPLOAD_BATCH = 500
MAX_FILE_BYTES = 4 * 1024**3
MAX_PATH_CHARS = 512
MAX_JSON_BODY_BYTES = 512 * 1024
"""413 above this for authenticated requests (checked locally before sending)."""
MAX_TTL_S = 21_600
ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$")
REPO_ID_PATTERN = re.compile(r"^[\w.-]+/[\w.-]+$")
STATE_NAMES: tuple[str, ...] = tuple(f"{j}.pos" for j in SO101_JOINT_NAMES)
"""Required state / action order: shoulder_pan.pos ... gripper.pos (arm degrees, gripper 0-100)."""
IMAGE_KEYS: tuple[str, str] = ("observation.images.side", "observation.images.wrist")
IMAGE_SHAPE: tuple[int, int, int] = (480, 640, 3)
DATASET_FPS = 30
CODEBASE_VERSIONS: frozenset[str] = frozenset({"v2.0", "v2.1", "v3.0"})
REST_POSE_USD_DEG: tuple[float, ...] = (0.0, -100.0, 90.0, 50.0, 0.0, -10.0)
"""LeIsaac / lekiwi.usd SO-101 rest pose in USD joint degrees (gripper closed)."""

Backend = Literal["standard", "shared"]
Mode = Literal["expert_only", "backbone_lora", "full"]
ImageEncoding = Literal["png", "jpeg", "rgb24"]

DATASET_READY = frozenset({"ready", "validated", "succeeded", "completed", "complete", "available", "ingested"})
DATASET_FAILED = frozenset({"failed", "invalid", "rejected", "error", "errored", "cancelled", "canceled"})
VALIDATION_OK = frozenset({"passed", "pass", "valid", "ok", "ready", "succeeded", "success", "completed"})
VALIDATION_FAILED = frozenset({"failed", "fail", "invalid", "error", "errored", "rejected"})
JOB_SUCCEEDED = frozenset({"succeeded", "success", "completed", "complete", "done", "finished"})
JOB_FAILED = frozenset({"failed", "error", "errored", "cancelled", "canceled", "timed_out", "timeout", "rejected"})
DEPLOYMENT_READY = frozenset({"ready"})
DEPLOYMENT_FAILED = frozenset({"failed", "error", "errored", "stopped", "deleted", "terminated", "retired"})

STATE_FILE = Path.home() / ".cache" / "picky" / "perceptron-vla.json"
"""Last pipeline ids (dataset / job / checkpoint / deployment / contract). Ids only: never keys or tokens."""


# ---- errors ---------------------------------------------------------------------------------------------------------
class RoboticsError(Exception):
    """Base error of this module. Messages never contain a key or a token."""


class RoboticsConfigError(RoboticsError):
    """Missing key / bad local input (nothing was sent)."""


class RoboticsTimeout(RoboticsError):
    """A waiter ran out of time (the remote operation may still finish; re-run to resume)."""


class RoboticsAPIError(RoboticsError):
    """Non-2xx answer. ``code`` / ``message`` / ``fields`` from the ``{"error": {...}}`` envelope when present."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str = "",
        *,
        fields: list[dict[str, Any]] | None = None,
        retry_after: float | None = None,
        method: str = "",
        path: str = "",
    ) -> None:
        self.status = status
        self.code = code
        self.message = message or code
        self.fields = fields
        self.retry_after = retry_after
        self.method = method
        self.path = path
        hint = ""
        if status == 401 and path.startswith("/v1") and not path.startswith("/v1/inference"):
            hint = (
                f" (the team key was rejected: {KEY_NAME} must be the robotics team key issued by Perceptron staff; "
                "the Mk1.5 vision key PERCEPTRON_API_KEY does not work here)"
            )
        where = f"{method} {path} " if method else ""
        detail = f" fields={json.dumps(fields)[:400]}" if fields else ""
        super().__init__(f"{where}-> {status} {code}: {self.message}{detail}{hint}")


# ---- key / settings -------------------------------------------------------------------------------------------------
def _settings_value(name: str) -> str:
    try:
        return str(getattr(get_settings(), name.lower(), "") or "").strip()
    except Exception:  # noqa: BLE001 - a broken .env must not hide the env var
        return ""


def _dotenv_value(name: str) -> str:
    path = repo_root() / ".env"
    if not path.exists():
        return ""
    try:
        from dotenv import dotenv_values

        return str(dotenv_values(path).get(name) or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _vault_value(name: str) -> tuple[str, str]:
    """utils.secrets when this checkout has it; else the same local stores it uses (Keychain / secrets.env)."""
    try:
        from utils import secrets as vault  # type: ignore[attr-defined]  # lands with the key-vault PR

        res = vault.get_vault().resolve(name)
        if res.value:
            return str(res.value).strip(), str(res.source or "vault")
    except Exception:  # noqa: BLE001 - module missing or vault unusable: fall through to the raw stores
        pass
    if sys.platform == "darwin" and shutil.which("security"):
        try:
            out = subprocess.run(
                ["security", "find-generic-password", "-s", "picky", "-a", name, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip(), "keychain"
        except (OSError, subprocess.SubprocessError):
            pass
    path = Path.home() / ".config" / "picky" / "secrets.env"
    if path.exists():
        try:
            from dotenv import dotenv_values

            value = str(dotenv_values(path).get(name) or "").strip()
            if value:
                return value, "file"
        except Exception:  # noqa: BLE001
            pass
    return "", ""


def resolve_setting(name: str, *, secret: bool = False) -> tuple[str, str]:
    """(value, source) for an env-style name: Settings field -> process env -> [vault] -> repo .env. Never logged."""
    value = _settings_value(name)
    if value:
        return value, "settings"
    value = os.environ.get(name, "").strip()
    if value:
        return value, "env"
    if secret:
        value, source = _vault_value(name)
        if value:
            return value, source
    value = _dotenv_value(name)
    return (value, ".env") if value else ("", "")


def resolve_api_key() -> tuple[str, str]:
    """(team key, source) or ("", "")."""
    return resolve_setting(KEY_NAME, secret=True)


def resolve_base_url() -> str:
    return (resolve_setting(BASE_URL_ENV)[0] or DEFAULT_BASE_URL).rstrip("/")


def mask(value: str) -> str:
    return f"set (…{value[-4:]})" if len(value) >= 16 else ("set" if value else "not set")


def _fingerprint(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def idempotency_key(kind: str, body: Mapping[str, Any], *, extra: Any = None, fresh: bool = False) -> str:
    """Stable key for one logical submission: same body (+ extra) -> same key -> the service replays the original."""
    payload = json.dumps({"body": body, "extra": extra}, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
    nonce = f"-{_secrets.token_hex(4)}" if fresh else ""
    return f"picky-{kind}-{digest}{nonce}"


# ---- request models (components/schemas, extra="forbid" like the service) -------------------------------------------
class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DatasetRequest(_Req):
    name: str = Field(min_length=1, max_length=100)
    source_kind: Literal["huggingface", "upload"] = "huggingface"
    repo_id: str | None = Field(None, pattern=REPO_ID_PATTERN.pattern)
    revision: str = Field("main", min_length=1, max_length=128)

    @model_validator(mode="after")
    def _check_source(self) -> DatasetRequest:
        if self.source_kind == "huggingface" and not self.repo_id:
            raise ValueError("a huggingface dataset needs repo_id ('org/name')")
        if self.source_kind == "upload" and self.repo_id:
            raise ValueError("an upload dataset must omit repo_id")
        return self

    def body(self) -> dict[str, Any]:
        if self.source_kind == "upload":
            return {"name": self.name, "source_kind": "upload"}
        return self.model_dump(mode="json")


class UploadFile(_Req):
    path: str = Field(min_length=1, max_length=MAX_PATH_CHARS)
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class UploadRequest(_Req):
    files: list[UploadFile] = Field(min_length=1, max_length=MAX_UPLOAD_BATCH)


class JobRequest(_Req):
    dataset_id: str = Field(pattern=ID_PATTERN.pattern)
    backend: Backend = "standard"
    mode: Mode = "expert_only"
    action_horizon: Literal[20, 30] = 30
    rtc_enabled: bool = True
    steps: int = Field(1000, ge=1, le=10_000)

    @model_validator(mode="after")
    def _shared_is_expert_only(self) -> JobRequest:
        if self.backend == "shared" and self.mode != "expert_only":
            raise ValueError("the shared backend supports mode expert_only only")
        return self


class SessionGrantRequest(_Req):
    session_id: str = Field(pattern=ID_PATTERN.pattern)
    ttl_seconds: int = Field(MAX_TTL_S, ge=1, le=MAX_TTL_S)


class InferenceSessionRequest(_Req):
    instruction: str = Field(min_length=1)
    contract_id: str = Field(min_length=1)
    rtc_enabled: bool = True


class RtcHint(_Req):
    """Real-time chunking: the prior chunk id, the prior-chunk row the new chunk aligns with, the rows that will run
    while this inference is in flight (``delay``) and, optionally, exactly those rows (contract units)."""

    prior_chunk_id: str
    prior_start_index: int = Field(ge=0)
    delay: int = Field(ge=0)
    committed_actions: list[list[float]] | None = None

    @model_validator(mode="after")
    def _rows(self) -> RtcHint:
        if self.committed_actions is not None:
            if any(len(r) != 6 for r in self.committed_actions):
                raise ValueError("committed_actions rows need 6 values each")
            if len(self.committed_actions) != self.delay:
                raise ValueError("committed_actions needs one row per delayed step")
        return self


class PredictMetadata(_Req):
    contract_id: str
    observation_id: str
    sequence: int = Field(ge=0)
    timestamp_seconds: float
    state: list[float] = Field(min_length=6, max_length=6)
    image_encoding: ImageEncoding = "jpeg"
    image_shape: list[int] | None = None
    rtc: RtcHint | None = None
    capture_age_ms: float | None = Field(None, ge=0)
    deadline_ms: float | None = Field(None, gt=0)

    @model_validator(mode="after")
    def _finite(self) -> PredictMetadata:
        if not all(math.isfinite(v) for v in self.state):
            raise ValueError("state values must be finite")
        if self.image_encoding == "rgb24" and not self.image_shape:
            raise ValueError("image_encoding rgb24 needs image_shape")
        return self


# ---- response records (untyped in the spec: keep everything, read tolerantly) ---------------------------------------
def _find(obj: Any, names: Iterable[str], depth: int = 3) -> Any:
    """First value under any of ``names`` (breadth-first, dicts only, up to ``depth`` levels)."""
    wanted = tuple(names)
    level: list[Any] = [obj]
    for _ in range(depth + 1):
        nxt: list[Any] = []
        for o in level:
            if not isinstance(o, Mapping):
                continue
            for n in wanted:
                if o.get(n) is not None:
                    return o[n]
            nxt.extend(v for v in o.values() if isinstance(v, Mapping))
        level = nxt
    return None


def _ref(obj: Any, kind: str) -> str | None:
    """'<kind>_id' anywhere, or '<kind>': {'id': ...} / '<kind>': 'id'."""
    v = _find(obj, (f"{kind}_id",))
    if isinstance(v, str | int):
        return str(v)
    v = _find(obj, (kind,))
    if isinstance(v, Mapping) and v.get("id") is not None:
        return str(v["id"])
    if isinstance(v, str):
        return v
    return None


class Record(BaseModel):
    """A service record: ``id`` + status, every other field kept (``extra``)."""

    model_config = ConfigDict(extra="allow")
    ID_KEYS: ClassVar[tuple[str, ...]] = ("id",)

    id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _fill_id(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and not data.get("id"):
            for k in cls.ID_KEYS:
                if data.get(k):
                    data = {**data, "id": str(data[k])}
                    break
        if isinstance(data, Mapping) and data.get("id") is not None and not isinstance(data["id"], str):
            data = {**data, "id": str(data["id"])}
        return data

    @property
    def raw(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    @property
    def phase(self) -> str:
        """Lower-cased status ('status' | 'state' | 'phase', nested {'status': {'phase': ...}} too)."""
        raw = self.raw
        for key in ("status", "state", "phase"):
            v = raw.get(key)
            if isinstance(v, Mapping):
                v = v.get("phase") or v.get("state") or v.get("status")
            if isinstance(v, str) and v:
                return v.lower()
        return ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)


class Dataset(Record):
    ID_KEYS = ("id", "dataset_id")


class Validation(Record):
    ID_KEYS = ("id", "dataset_id")

    @property
    def ok(self) -> bool | None:
        """True passed / False failed / None still running or unknown."""
        raw = self.raw
        for key in ("valid", "passed", "ok"):
            if isinstance(raw.get(key), bool):
                return bool(raw[key])
        if self.phase in VALIDATION_OK:
            return True
        if self.phase in VALIDATION_FAILED:
            return False
        return None

    def issues(self, limit: int = 12) -> list[str]:
        found = _find(self.raw, ("errors", "issues", "failures", "problems", "warnings"))
        out: list[str] = []
        for item in found if isinstance(found, list) else []:
            if isinstance(item, Mapping):
                text = item.get("message") or item.get("detail") or item.get("code") or json.dumps(item)
                out.append(str(text))
            else:
                out.append(str(item))
        return out[:limit]


class Job(Record):
    ID_KEYS = ("id", "job_id")

    @property
    def checkpoint_ref(self) -> str | None:
        return _ref(self.raw, "checkpoint")

    @property
    def deployment_ref(self) -> str | None:
        return _ref(self.raw, "deployment")


class Checkpoint(Record):
    ID_KEYS = ("id", "checkpoint_id")

    @property
    def deployment_ref(self) -> str | None:
        return _ref(self.raw, "deployment")


class Deployment(Record):
    ID_KEYS = ("id", "deployment_id")

    @property
    def checkpoint_ref(self) -> str | None:
        return _ref(self.raw, "checkpoint")

    @property
    def job_ref(self) -> str | None:
        return _ref(self.raw, "job")

    @property
    def is_modal(self) -> bool:
        """The list distinguishes Modal production from private-cluster qualification; prefer Modal."""
        text = json.dumps(self.raw).lower()
        return "modal" in text or "production" in text


class LogPage(Record):
    events: list[Any] = Field(default_factory=list)
    next_cursor: int | None = None
    truncated_before: Any = None

    @model_validator(mode="before")
    @classmethod
    def _items(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "events" not in data:
            for k in ("items", "logs", "entries"):
                if isinstance(data.get(k), list):
                    return {**data, "events": data[k]}
        return data


class MetricsPage(Record):
    metrics: list[Any] = Field(default_factory=list)
    next_cursor: int | None = None
    truncated_before: Any = None

    @model_validator(mode="before")
    @classmethod
    def _items(cls, data: Any) -> Any:
        if isinstance(data, Mapping) and "metrics" not in data:
            for k in ("items", "points", "events"):
                if isinstance(data.get(k), list):
                    return {**data, "metrics": data[k]}
        return data


class ServingContract(BaseModel):
    """GET /v1/deployments/{id}/contract: ``contract_id`` (canonical SHA-256) + the immutable SO100 wire contract.
    Accessors read the fields the guide names (cameras, state order, units, timing) and fall back to the checklist
    defaults when the contract words them differently; ``raw`` is always printed by the CLI for a human check."""

    model_config = ConfigDict(extra="allow")
    contract_id: str
    contract: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _flat(cls, data: Any) -> Any:
        if isinstance(data, Mapping):
            if not data.get("contract_id"):
                cid = data.get("id") or data.get("sha256") or _find(data.get("contract"), ("contract_id", "sha256"))
                data = {**data, "contract_id": str(cid or "")}
            if not isinstance(data.get("contract"), Mapping):
                data = {**data, "contract": {k: v for k, v in data.items() if k != "contract_id"}}
        return data

    def _num(self, *names: str) -> float | None:
        v = _find(self.contract, names, depth=4)
        if isinstance(v, int | float) and not isinstance(v, bool):
            return float(v)
        return None

    @property
    def state_names(self) -> list[str]:
        for key in ("state_names", "state_order", "joint_names", "names", "order"):
            v = _find(self.contract, (key,), depth=4)
            if isinstance(v, list) and len(v) == 6 and all(isinstance(x, str) for x in v):
                return list(v)
        return list(STATE_NAMES)

    @property
    def action_hz(self) -> float:
        return self._num("fps", "control_hz", "action_hz", "control_frequency_hz", "frequency_hz", "hz") or 30.0

    @property
    def action_horizon(self) -> int | None:
        v = self._num("action_horizon", "horizon", "chunk_size", "chunk_length")
        return int(v) if v else None

    @property
    def execution_horizon(self) -> int | None:
        v = self._num("execution_horizon", "execute_steps", "execution_steps", "actions_per_chunk", "execution_prefix")
        return int(v) if v else None

    @property
    def cameras(self) -> dict[str, tuple[int, int]]:
        """camera name -> (width, height); default side + wrist at 640x480."""
        found = _find(self.contract, ("cameras", "images"), depth=4)
        out: dict[str, tuple[int, int]] = {}
        items: Iterable[tuple[str, Any]] = []
        if isinstance(found, Mapping):
            items = found.items()
        elif isinstance(found, list):
            items = [(str(c.get("name") or c.get("key") or ""), c) for c in found if isinstance(c, Mapping)]
        for name, spec in items:
            short = name.rsplit(".", 1)[-1]
            w, h = 640, 480
            if isinstance(spec, Mapping):
                shape = spec.get("shape")
                if isinstance(shape, list) and len(shape) >= 2 and all(isinstance(x, int) for x in shape):
                    if len(shape) == 3 and shape[0] in (1, 3, 4) and shape[2] not in (1, 3, 4):
                        shape = [shape[1], shape[2], shape[0]]  # CHW -> HWC
                    h, w = int(shape[0]), int(shape[1])
                w = int(spec.get("width") or w)
                h = int(spec.get("height") or h)
            if short:
                out[short] = (w, h)
        return out or {"side": (640, 480), "wrist": (640, 480)}

    @property
    def arm_units(self) -> Literal["degrees", "radians"]:
        units = json.dumps(_find(self.contract, ("units", "state_units", "unit"), depth=4) or "").lower()
        return "radians" if "rad" in units and "deg" not in units else "degrees"

    @property
    def gripper_range(self) -> tuple[float, float]:
        v = _find(self.contract, ("gripper_range", "gripper_limits"), depth=4)
        if isinstance(v, list) and len(v) == 2 and all(isinstance(x, int | float) for x in v):
            return float(v[0]), float(v[1])
        return 0.0, 100.0

    def summary(self) -> str:
        cams = ", ".join(f"{k} {w}x{h}" for k, (w, h) in self.cameras.items())
        return (
            f"contract {self.contract_id[:12]}…  cameras: {cams}  state: {', '.join(self.state_names)}  "
            f"units: arm {self.arm_units}, gripper {self.gripper_range[0]:g}-{self.gripper_range[1]:g}  "
            f"timing: {self.action_hz:g} Hz, horizon {self.action_horizon or '?'}, "
            f"execute {self.execution_horizon or '?'}"
        )


class SessionGrant(BaseModel):
    """POST /v1/deployments/{id}/sessions (201). Tokens are secrets: ``SecretStr``, never printed or persisted."""

    model_config = ConfigDict(extra="allow")
    session_id: str
    endpoint_url: str
    application_session_token: SecretStr
    modal_session_token: SecretStr | None = None
    expires_at: Any = None

    def public(self) -> dict[str, Any]:
        """Printable view (no tokens)."""
        return {
            "session_id": self.session_id,
            "endpoint_url": self.endpoint_url,
            "expires_at": self.expires_at,
            "modal_session_token": "present" if self.modal_session_token else "absent",
        }


class PredictResult(BaseModel):
    """One action chunk. ``actions`` rows are in CONTRACT units (arm degrees, gripper 0-100)."""

    model_config = ConfigDict(extra="allow")
    actions: list[list[float]]
    chunk_id: str = ""
    sequence: int | None = None
    execute_from: int | None = None
    execute_count: int | None = None
    timings: dict[str, Any] = Field(default_factory=dict)
    roundtrip_ms: float = 0.0

    @classmethod
    def from_api(cls, data: Mapping[str, Any], roundtrip_ms: float = 0.0) -> PredictResult:
        rows = data.get("actions")
        if rows is None:
            for key in ("action_chunk", "action_rows", "chunk", "action"):
                v = data.get(key)
                if isinstance(v, Mapping):
                    v = v.get("actions") or v.get("rows")
                if isinstance(v, list):
                    rows = v
                    break
        if not isinstance(rows, list) or not rows:
            raise RoboticsAPIError(200, "malformed_chunk", "predict response has no action rows")
        chunk = data.get("chunk") if isinstance(data.get("chunk"), Mapping) else {}
        prefix = data.get("execution_prefix", chunk.get("execution_prefix"))
        start: int | None = None
        count: int | None = None
        if isinstance(prefix, bool):
            prefix = None
        if isinstance(prefix, int):
            count = prefix
        elif isinstance(prefix, Mapping):
            s = prefix.get("start_index", prefix.get("start", prefix.get("offset")))
            c = prefix.get("length", prefix.get("count", prefix.get("steps")))
            start = int(s) if isinstance(s, int) else None
            count = int(c) if isinstance(c, int) else None
            if count is None and isinstance(prefix.get("end"), int) and start is not None:
                count = int(prefix["end"]) - start
        elif isinstance(prefix, list) and prefix and isinstance(prefix[0], list):
            count = len(prefix)
        timings = data.get("timings") or data.get("timing") or {}
        try:
            parsed = [[float(v) for v in row] for row in rows]
        except (TypeError, ValueError) as exc:
            raise RoboticsAPIError(200, "malformed_chunk", f"non-numeric action rows: {exc}") from exc
        seq = data.get("sequence", chunk.get("sequence"))
        return cls(
            actions=parsed,
            chunk_id=str(data.get("chunk_id") or chunk.get("chunk_id") or chunk.get("id") or ""),
            sequence=int(seq) if isinstance(seq, int) else None,
            execute_from=start,
            execute_count=count,
            timings=dict(timings) if isinstance(timings, Mapping) else {},
            roundtrip_ms=roundtrip_ms,
            **{k: v for k, v in data.items() if k not in cls.model_fields and k not in ("chunk", "timing")},
        )


# ---- HTTP plumbing --------------------------------------------------------------------------------------------------
Sleep = Callable[[float], Awaitable[None]]
Progress = Callable[[str], None]


def _noop(_: str) -> None:
    return None


class _KeyGuard:
    """httpx request hook: refuses any request whose credentials carry the team key (by SHA-256 fingerprint)."""

    def __init__(self, fingerprints: Iterable[str]) -> None:
        self.fingerprints = frozenset(f for f in fingerprints if f)

    async def __call__(self, request: httpx.Request) -> None:
        if not self.fingerprints:
            return
        for name in ("authorization", "modal-authorization", "x-api-key", "proxy-authorization"):
            value = request.headers.get(name)
            if not value:
                continue
            token = value.split(" ", 1)[1] if value.lower().startswith("bearer ") else value
            if _fingerprint(token.strip()) in self.fingerprints:
                raise RoboticsConfigError(f"refusing to send the team API key to {request.url.host} ({name} header)")


def _retry_after(resp: httpx.Response, default: float) -> float:
    try:
        return max(0.0, float(resp.headers.get("retry-after", default)))
    except ValueError:
        return default


def _api_error(resp: httpx.Response, method: str, path: str) -> RoboticsAPIError:
    code, message, fields = f"http_{resp.status_code}", "", None
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        body = None
    if isinstance(body, Mapping):
        err = body.get("error")
        if isinstance(err, Mapping):
            code = str(err.get("code") or code)
            message = str(err.get("message") or "")
            fields = err.get("fields") if isinstance(err.get("fields"), list) else None
        elif isinstance(err, str):
            message = err
        elif "detail" in body:
            message = json.dumps(body["detail"])[:500]
    elif resp.text:
        message = resp.text[:300]
    retry = _retry_after(resp, 0.0) if resp.status_code == 429 else None
    return RoboticsAPIError(resp.status_code, code, message, fields=fields, retry_after=retry, method=method, path=path)


def _same_origin(a: str, b: str) -> bool:
    sa, sb = urlsplit(a), urlsplit(b)
    return (sa.scheme, sa.hostname, sa.port) == (sb.scheme, sb.hostname, sb.port)


async def follow_poll(
    client: httpx.AsyncClient,
    origin: str,
    location: str,
    *,
    timeout_s: float,
    sleep: Sleep = asyncio.sleep,
    method: str = "",
    path: str = "",
) -> Any:
    """Poll a 303 ``Location`` until the final response: same origin as ``origin`` only (HTTPS unless the origin is a
    plain-HTTP test server), exact path + query, GET, NO credentials, original body never replayed."""
    deadline = time.monotonic() + timeout_s
    url = urljoin(origin + "/", location)
    while True:
        if not _same_origin(url, origin) or urlsplit(url).scheme not in ("https", urlsplit(origin).scheme):
            raise RoboticsAPIError(303, "unsafe_redirect", "polling URL is not same-origin", method=method, path=path)
        resp = await client.get(url, headers={"Accept": "application/json"})
        nxt = resp.headers.get("location")
        pending = resp.status_code == 303 or (resp.status_code == 202 and (nxt or "retry-after" in resp.headers))
        if not pending:
            if resp.status_code >= 400:
                raise _api_error(resp, method, path)
            return resp.json() if resp.content else {}
        if time.monotonic() >= deadline:
            raise RoboticsTimeout(f"{method} {path}: operation still pending after {timeout_s:.0f} s")
        if nxt:
            url = urljoin(url, nxt)
        await sleep(min(_retry_after(resp, 1.0), 30.0))


# ---- local dataset files --------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LocalFile:
    path: str
    """POSIX path relative to the dataset root (what the service stores)."""
    abs_path: Path
    size_bytes: int
    sha256: str

    def upload_file(self) -> UploadFile:
        return UploadFile(path=self.path, size_bytes=self.size_bytes, sha256=self.sha256)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def scan_dataset_dir(root: str | Path, progress: Progress = _noop) -> tuple[list[LocalFile], list[str]]:
    """Every uploadable file under ``root`` (supported extension, >= 1 byte, <= 4 GiB, path <= 512 chars, no hidden
    path parts) + the warnings for what was skipped. Raises when ``meta/info.json`` is missing."""
    base = Path(root).expanduser().resolve()
    if not (base / "meta" / "info.json").is_file():
        raise RoboticsConfigError(f"{base} is not a LeRobot dataset export (meta/info.json missing)")
    files: list[LocalFile] = []
    skipped: list[str] = []
    for p in sorted(base.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(base).as_posix()
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
            skipped.append(f"{rel}: unsupported extension")
            continue
        size = p.stat().st_size
        if size < 1:
            skipped.append(f"{rel}: empty file")
            continue
        if size > MAX_FILE_BYTES:
            raise RoboticsConfigError(f"{rel}: {size} bytes exceeds the 4 GiB per-file limit")
        if len(rel) > MAX_PATH_CHARS:
            raise RoboticsConfigError(f"{rel[:60]}…: path longer than {MAX_PATH_CHARS} characters")
        files.append(LocalFile(rel, p, size, _sha256_file(p)))
    progress(f"dataset dir: {len(files)} files, {sum(f.size_bytes for f in files) / 1e6:.1f} MB")
    return files, skipped


def _dir_name(root: str | Path) -> str:
    return Path(root).expanduser().resolve().name


def _is_dir(path: Path) -> bool:
    return path.is_dir()


def manifest_digest(files: Sequence[LocalFile]) -> str:
    h = hashlib.sha256()
    for f in sorted(files, key=lambda x: x.path):
        h.update(f"{f.path}\0{f.size_bytes}\0{f.sha256}\n".encode())
    return h.hexdigest()


@dataclass
class DatasetCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _names(feature: Mapping[str, Any]) -> list[str]:
    names = feature.get("names")
    if isinstance(names, Mapping):  # LeRobot v2: {"motors": [...]} / {"axes": [...]}
        for v in names.values():
            if isinstance(v, list):
                return [str(x) for x in v]
        return []
    return [str(x) for x in names] if isinstance(names, list) else []


def check_dataset_dir(root: str | Path) -> DatasetCheck:
    """Local pre-flight against the recording checklist (no network, no key): LeRobot v2.0/v2.1/v3.0, 30 FPS, RGB
    side + wrist at 640x480, 6-value state + action in the required order, >= 2 episodes, data + videos present.
    Units (arm degrees, gripper 0-100) cannot be checked without reading the parquet: the service validates them."""
    out = DatasetCheck()
    base = Path(root).expanduser()
    info_path = base / "meta" / "info.json"
    if not info_path.is_file():
        out.errors.append("meta/info.json missing (not a LeRobot export)")
        return out
    try:
        info = json.loads(info_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        out.errors.append(f"meta/info.json unreadable: {exc}")
        return out
    out.info = {k: info.get(k) for k in ("codebase_version", "robot_type", "fps", "total_episodes", "total_frames")}
    if info.get("codebase_version") not in CODEBASE_VERSIONS:
        out.errors.append(f"codebase_version {info.get('codebase_version')!r} not in {sorted(CODEBASE_VERSIONS)}")
    if info.get("fps") != DATASET_FPS:
        out.errors.append(f"fps {info.get('fps')!r} (needs {DATASET_FPS})")
    feats = info.get("features") or {}
    for key in IMAGE_KEYS:
        f = feats.get(key)
        if not isinstance(f, Mapping):
            out.errors.append(f"feature {key} missing")
            continue
        shape = list(f.get("shape") or [])
        names = _names(f)
        hwc = shape
        if names[:3] == ["channels", "height", "width"] and len(shape) == 3:
            hwc = [shape[1], shape[2], shape[0]]
        if hwc != list(IMAGE_SHAPE):
            out.errors.append(f"{key} shape {shape} (needs 480x640x3 RGB)")
        if f.get("dtype") not in ("video", "image"):
            out.warnings.append(f"{key} dtype {f.get('dtype')!r} (expected video)")
    extra_cams = sorted(k for k in feats if k.startswith("observation.images.") and k not in IMAGE_KEYS)
    if extra_cams:
        out.warnings.append(f"extra cameras ignored by the contract: {', '.join(extra_cams)}")
    for key in ("observation.state", "action"):
        f = feats.get(key)
        if not isinstance(f, Mapping):
            out.errors.append(f"feature {key} missing")
            continue
        if list(f.get("shape") or []) != [6]:
            out.errors.append(f"{key} shape {f.get('shape')} (needs [6])")
        names = _names(f)
        if names and names != list(STATE_NAMES):
            out.errors.append(f"{key} names {names} (needs {list(STATE_NAMES)})")
        elif not names:
            out.warnings.append(f"{key} has no names (needs {list(STATE_NAMES)})")
    episodes = info.get("total_episodes")
    if not isinstance(episodes, int) or episodes < 2:
        out.errors.append(f"total_episodes {episodes!r}: needs >= 1 train + >= 1 validation episode")
    splits = info.get("splits") or {}
    if isinstance(splits, Mapping) and splits and not any(k in splits for k in ("validation", "val", "test")):
        out.warnings.append(f"splits {dict(splits)} name no validation split (the service may split by itself)")
    if not list(base.glob("data/**/*.parquet")):
        out.errors.append("no data/**/*.parquet files")
    for key in IMAGE_KEYS:
        if feats.get(key, {}).get("dtype") == "video" and not list(base.glob(f"videos/**/{key}/**/*.mp4")):
            if not list(base.glob(f"videos/**/*{key}*.mp4")):
                out.errors.append(f"no videos for {key}")
    robot = str(info.get("robot_type") or "")
    if robot and not any(t in robot.lower() for t in ("so100", "so101", "so-100", "so-101", "so_10", "lekiwi")):
        out.warnings.append(f"robot_type {robot!r} is not an SO100/SO101")
    return out


# ---- the control-plane client ---------------------------------------------------------------------------------------
class RoboticsClient:
    """Async client for the control plane (team key). One instance per event loop; ``async with`` closes it."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        poll_timeout_s: float = 600.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        key = api_key if api_key is not None else resolve_api_key()[0]
        if not key:
            raise RoboticsConfigError(
                f"{KEY_NAME} is not set. Ask Perceptron staff for the robotics team key, then store it with "
                f"`uv run python -m utils.secrets set {KEY_NAME}` (or export it). The Mk1.5 key PERCEPTRON_API_KEY "
                "is a different key and is rejected by robotics.perceptron.inc."
            )
        self._key = key
        self.key_fingerprint = _fingerprint(key)
        self.base_url = (base_url or resolve_base_url()).rstrip("/")
        self.timeout_s = timeout_s
        self.max_retries = max_retries
        self.poll_timeout_s = poll_timeout_s
        self._transport = transport
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=self.base_url, timeout=timeout_s, transport=transport, follow_redirects=False
        )
        # credential-free client for signed upload URLs and 303 polling (the key guard refuses the team key there)
        self._bare = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0),
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [_KeyGuard([self.key_fingerprint])]},
        )

    async def __aenter__(self) -> RoboticsClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._bare.aclose()

    def __repr__(self) -> str:
        return f"RoboticsClient(base_url={self.base_url!r}, key={mask(self._key)})"

    # -- core
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        params: Mapping[str, Any] | None = None,
        idem_key: str | None = None,
        auth: bool = True,
        retry_safe: bool | None = None,
    ) -> Any:
        headers = {"Accept": "application/json", "User-Agent": "picky-perceptron-vla/1"}
        if auth:
            headers["Authorization"] = f"Bearer {self._key}"
        content: bytes | None = None
        if json_body is not None:
            content = json.dumps(json_body, separators=(",", ":")).encode()
            if auth and len(content) > MAX_JSON_BODY_BYTES:
                raise RoboticsConfigError(f"{method} {path}: body {len(content)} B exceeds the 512 KiB request limit")
            headers["Content-Type"] = "application/json"
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        safe = retry_safe if retry_safe is not None else (method == "GET" or idem_key is not None)
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._http.request(method, path, content=content, params=params, headers=headers)
            except httpx.TimeoutException as exc:
                if safe and attempt <= self.max_retries:
                    await self._sleep(min(2.0**attempt, 20.0))
                    continue
                raise RoboticsAPIError(
                    0,
                    "timeout",
                    f"no answer within {self.timeout_s:g} s ({exc.__class__.__name__})",
                    method=method,
                    path=path,
                ) from exc
            except httpx.TransportError as exc:
                if safe and attempt <= self.max_retries:
                    await self._sleep(min(2.0**attempt, 20.0))
                    continue
                raise RoboticsAPIError(0, "network", exc.__class__.__name__, method=method, path=path) from exc
            if resp.status_code == 429 and attempt <= self.max_retries:
                await self._sleep(min(_retry_after(resp, 2.0), 60.0))
                continue
            if resp.status_code in (500, 502, 503, 504) and safe and attempt <= self.max_retries:
                await self._sleep(min(_retry_after(resp, 2.0**attempt), 30.0))
                continue
            if resp.status_code == 303 and resp.headers.get("location"):
                return await follow_poll(
                    self._bare,
                    self.base_url,
                    resp.headers["location"],
                    timeout_s=self.poll_timeout_s,
                    sleep=self._sleep,
                    method=method,
                    path=path,
                )
            if resp.status_code >= 400:
                raise _api_error(resp, method, path)
            if not resp.content:
                return {}
            try:
                return resp.json()
            except (ValueError, json.JSONDecodeError) as exc:
                raise RoboticsAPIError(resp.status_code, "bad_json", resp.text[:200], method=method, path=path) from exc

    @staticmethod
    def _items(data: Any) -> list[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, Mapping):
            for key in ("items", "data", "results"):
                if isinstance(data.get(key), list):
                    return list(data[key])
        return []

    # -- health / discovery
    async def healthz(self) -> dict[str, Any]:
        return await self._request("GET", "/healthz", auth=False)

    async def openapi(self) -> dict[str, Any]:
        return await self._request("GET", "/openapi.json", auth=False)

    async def capabilities(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/capabilities")

    # -- datasets
    async def create_dataset(self, req: DatasetRequest, *, idem_key: str | None = None, fresh: bool = False) -> Dataset:
        body = req.body()
        key = idem_key or idempotency_key(f"dataset-{req.source_kind}", body, fresh=fresh)
        return Dataset.model_validate(await self._request("POST", "/v1/datasets", json_body=body, idem_key=key))

    async def create_dataset_hf(
        self, repo_id: str, *, name: str | None = None, revision: str = "main", fresh: bool = False
    ) -> Dataset:
        req = DatasetRequest(
            name=(name or repo_id.replace("/", "-"))[:100],
            source_kind="huggingface",
            repo_id=repo_id,
            revision=revision,
        )
        return await self.create_dataset(req, fresh=fresh)

    async def create_dataset_upload(self, name: str, *, manifest: str | None = None, fresh: bool = False) -> Dataset:
        req = DatasetRequest(name=name[:100], source_kind="upload")
        key = idempotency_key("dataset-upload", req.body(), extra=manifest, fresh=fresh)
        return await self.create_dataset(req, idem_key=key)

    async def reserve_uploads(self, dataset_id: str, files: Sequence[UploadFile]) -> list[dict[str, Any]]:
        body = UploadRequest(files=list(files)).model_dump(mode="json")
        data = await self._request("POST", f"/v1/datasets/{dataset_id}/uploads", json_body=body, retry_safe=True)
        return [dict(x) for x in self._items(data.get("files") if isinstance(data, Mapping) else data)]

    async def put_file(self, target: Mapping[str, Any], local: LocalFile) -> None:
        """PUT one file's raw bytes to its signed URL with exactly the returned method + headers (no team key)."""
        url = str(target.get("url") or "")
        if not url:
            raise RoboticsAPIError(200, "bad_upload_target", f"no url for {local.path}")
        method = str(target.get("method") or "PUT").upper()
        headers = {str(k): str(v) for k, v in (target.get("headers") or {}).items()}
        if not any(k.lower() == "content-length" for k in headers):
            headers["Content-Length"] = str(local.size_bytes)
        if any(k.lower() == "authorization" for k in headers):
            raise RoboticsConfigError("upload target asks for an Authorization header; refusing to add credentials")
        if local.size_bytes <= 16 * 1024 * 1024:
            data = await asyncio.to_thread(local.abs_path.read_bytes)
            resp = await self._bare.request(method, url, content=data, headers=headers)
        else:
            resp = await self._bare.request(method, url, content=_file_chunks(local.abs_path), headers=headers)
        if resp.status_code >= 400:
            raise _api_error(resp, method, f"upload {local.path}")

    async def finalize_dataset(self, dataset_id: str) -> Dataset:
        return Dataset.model_validate(
            await self._request("POST", f"/v1/datasets/{dataset_id}/finalize", retry_safe=False)
        )

    async def get_dataset(self, dataset_id: str) -> Dataset:
        return Dataset.model_validate(await self._request("GET", f"/v1/datasets/{dataset_id}"))

    async def get_validation(self, dataset_id: str) -> Validation:
        return Validation.model_validate(await self._request("GET", f"/v1/datasets/{dataset_id}/validation"))

    async def upload_dataset_dir(
        self,
        root: str | Path,
        *,
        name: str | None = None,
        fresh: bool = False,
        concurrency: int = 4,
        progress: Progress = _noop,
    ) -> Dataset:
        """Register an upload dataset, reserve URLs in batches of <= 500, PUT every file, finalize. Re-running with the
        same files replays the same dataset (idempotency key = name + manifest digest); a dataset that was already
        finalized (409 on reserve / finalize) is returned as is."""
        files, skipped = await asyncio.to_thread(scan_dataset_dir, root, progress)
        for s in skipped[:10]:
            progress(f"  skipped {s}")
        if not files:
            raise RoboticsConfigError("no uploadable files")
        digest = manifest_digest(files)
        ds_name = (name or f"picky-{_dir_name(root)}")[:100]
        ds = await self.create_dataset_upload(ds_name, manifest=digest, fresh=fresh)
        progress(f"dataset {ds.id} registered for upload ({ds.phase or 'created'})")
        by_path = {f.path: f for f in files}
        sem = asyncio.Semaphore(max(1, concurrency))
        done = 0
        total_bytes = sum(f.size_bytes for f in files)
        sent_bytes = 0

        async def _one(target: Mapping[str, Any]) -> None:
            nonlocal done, sent_bytes
            local = by_path.get(str(target.get("path")))
            if local is None:
                raise RoboticsAPIError(200, "bad_upload_target", f"unexpected path {target.get('path')!r}")
            async with sem:
                for attempt in range(1, 4):
                    try:
                        await self.put_file(target, local)
                        break
                    except RoboticsAPIError as exc:
                        if attempt == 3 or (400 <= exc.status < 500 and exc.status not in (403, 408, 410, 429)):
                            raise
                        if exc.status in (403, 410):  # signed URL expired (15 min): reserve this file again
                            fresh_targets = await self.reserve_uploads(ds.id, [local.upload_file()])
                            target = fresh_targets[0] if fresh_targets else target
                        await self._sleep(min(2.0**attempt, 10.0))
            done += 1
            sent_bytes += local.size_bytes
            if done == len(files) or done % 25 == 0:
                progress(f"  uploaded {done}/{len(files)} files ({sent_bytes / max(total_bytes, 1):.0%})")

        try:
            for i in range(0, len(files), MAX_UPLOAD_BATCH):
                batch = files[i : i + MAX_UPLOAD_BATCH]
                targets = await self.reserve_uploads(ds.id, [f.upload_file() for f in batch])
                if len(targets) != len(batch):
                    raise RoboticsAPIError(200, "bad_upload_targets", f"{len(targets)} URLs for {len(batch)} files")
                await asyncio.gather(*(_one(t) for t in targets))
            fin = await self.finalize_dataset(ds.id)
            progress(f"dataset {ds.id} finalized ({fin.phase or 'queued'})")
        except RoboticsAPIError as exc:
            if exc.status != 409:
                raise
            progress(f"dataset {ds.id} already finalized ({exc.code}); continuing")
        return await self.get_dataset(ds.id)

    async def wait_dataset(
        self, dataset_id: str, *, timeout_s: float = 3600.0, poll_s: float = 5.0, progress: Progress = _noop
    ) -> tuple[Dataset, Validation | None]:
        """Poll until the dataset is ready, then read validation (raises on failed ingestion / failed validation)."""
        deadline = time.monotonic() + timeout_s
        last = None
        delay = poll_s
        while True:
            ds = await self.get_dataset(dataset_id)
            if ds.phase != last:
                progress(f"dataset {dataset_id}: {ds.phase or '?'}")
                last = ds.phase
            if ds.phase in DATASET_FAILED:
                val = await self._validation_or_none(dataset_id)
                issues = "; ".join(val.issues()) if val else ""
                raise RoboticsError(f"dataset {dataset_id} {ds.phase}: {issues or json.dumps(ds.raw)[:400]}")
            if ds.phase in DATASET_READY:
                val = await self._validation_or_none(dataset_id)
                if val is not None and val.ok is False:
                    raise RoboticsError(f"dataset {dataset_id} validation failed: {'; '.join(val.issues())}")
                return ds, val
            if time.monotonic() >= deadline:
                raise RoboticsTimeout(f"dataset {dataset_id} still {ds.phase or 'pending'} after {timeout_s:.0f} s")
            await self._sleep(delay)
            delay = min(delay * 1.5, 30.0)

    async def _validation_or_none(self, dataset_id: str) -> Validation | None:
        try:
            return await self.get_validation(dataset_id)
        except RoboticsAPIError as exc:
            if exc.status in (404, 409):
                return None
            raise

    # -- jobs
    async def submit_job(self, req: JobRequest, *, idem_key: str | None = None, fresh: bool = False) -> Job:
        body = req.model_dump(mode="json")
        key = idem_key or idempotency_key("train", body, fresh=fresh)
        return Job.model_validate(await self._request("POST", "/v1/jobs", json_body=body, idem_key=key))

    async def list_jobs(self) -> list[Job]:
        return [Job.model_validate(x) for x in self._items(await self._request("GET", "/v1/jobs"))]

    async def get_job(self, job_id: str) -> Job:
        return Job.model_validate(await self._request("GET", f"/v1/jobs/{job_id}"))

    async def cancel_job(self, job_id: str) -> Job:
        return Job.model_validate(await self._request("POST", f"/v1/jobs/{job_id}/cancel", retry_safe=True))

    async def job_logs(self, job_id: str, cursor: int = 0) -> LogPage:
        return LogPage.model_validate(await self._request("GET", f"/v1/jobs/{job_id}/logs", params={"cursor": cursor}))

    async def job_metrics(self, job_id: str, cursor: int = 0) -> MetricsPage:
        data = await self._request("GET", f"/v1/jobs/{job_id}/metrics", params={"cursor": cursor})
        return MetricsPage.model_validate(data)

    async def iter_logs(self, job_id: str, cursor: int = 0) -> AsyncIterator[tuple[Any, int]]:
        """Every event currently retained from ``cursor`` on: (event, next cursor). Stops when a page is empty."""
        while True:
            page = await self.job_logs(job_id, cursor)
            nxt = page.next_cursor if page.next_cursor is not None else cursor + len(page.events)
            for ev in page.events:
                yield ev, nxt
            if not page.events or nxt <= cursor:
                return
            cursor = nxt

    async def wait_job(
        self,
        job_id: str,
        *,
        timeout_s: float = 6 * 3600.0,
        poll_s: float = 15.0,
        progress: Progress = _noop,
        stream_logs: bool = True,
    ) -> Job:
        """Poll until the job finishes; streams new log events + the latest metrics with their cursors."""
        deadline = time.monotonic() + timeout_s
        log_cursor = metric_cursor = 0
        last = None
        while True:
            job = await self.get_job(job_id)
            if job.phase != last:
                progress(f"job {job_id}: {job.phase or '?'}")
                last = job.phase
            if stream_logs:
                try:
                    page = await self.job_logs(job_id, log_cursor)
                    if page.truncated_before:
                        progress(f"  (older log events truncated: {page.truncated_before})")
                    for ev in page.events:
                        progress(f"  log: {format_event(ev)}")
                    log_cursor = page.next_cursor if page.next_cursor is not None else log_cursor + len(page.events)
                    mpage = await self.job_metrics(job_id, metric_cursor)
                    if mpage.metrics:
                        progress(f"  metrics: {format_event(mpage.metrics[-1])}")
                    metric_cursor = (
                        mpage.next_cursor if mpage.next_cursor is not None else metric_cursor + len(mpage.metrics)
                    )
                except RoboticsAPIError as exc:  # logs are best effort; the status poll decides
                    if exc.status not in (404, 409):
                        raise
            if job.phase in JOB_SUCCEEDED:
                return job
            if job.phase in JOB_FAILED:
                reason = _find(job.raw, ("error", "failure_reason", "reason", "message"))
                raise RoboticsError(f"job {job_id} {job.phase}: {json.dumps(reason)[:400] if reason else ''}")
            if time.monotonic() >= deadline:
                raise RoboticsTimeout(f"job {job_id} still {job.phase or 'pending'} after {timeout_s:.0f} s")
            await self._sleep(poll_s)

    # -- checkpoints / deployments
    async def get_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        return Checkpoint.model_validate(await self._request("GET", f"/v1/checkpoints/{checkpoint_id}"))

    async def list_deployments(self) -> list[Deployment]:
        return [Deployment.model_validate(x) for x in self._items(await self._request("GET", "/v1/deployments"))]

    async def get_deployment(self, deployment_id: str) -> Deployment:
        return Deployment.model_validate(await self._request("GET", f"/v1/deployments/{deployment_id}"))

    async def get_contract(self, deployment_id: str) -> ServingContract:
        data = await self._request("GET", f"/v1/deployments/{deployment_id}/contract")
        contract = ServingContract.model_validate(data)
        if not contract.contract_id:
            raise RoboticsAPIError(200, "malformed_contract", "contract response has no contract_id")
        return contract

    def _match_deployment(self, deps: Sequence[Deployment], job: Job, checkpoint_id: str | None) -> Deployment | None:
        wanted = {x for x in (job.deployment_ref,) if x}
        cands = [d for d in deps if d.id in wanted]
        if not cands:
            cands = [
                d
                for d in deps
                if (checkpoint_id and d.checkpoint_ref == checkpoint_id) or (job.id and d.job_ref == job.id)
            ]
        if not cands:
            return None
        cands.sort(key=lambda d: (d.phase in DEPLOYMENT_READY, d.is_modal, str(d.get("created_at") or "")))
        return cands[-1]

    async def wait_deployment(
        self, job: Job, *, timeout_s: float = 3600.0, poll_s: float = 10.0, progress: Progress = _noop
    ) -> Deployment:
        """The ready deployment of a succeeded job (via its deployment / checkpoint ids)."""
        deadline = time.monotonic() + timeout_s
        checkpoint_id = job.checkpoint_ref
        if checkpoint_id:
            try:
                ckpt = await self.get_checkpoint(checkpoint_id)
                progress(f"checkpoint {ckpt.id}: {ckpt.phase or 'ok'}")
                if ckpt.deployment_ref and not job.deployment_ref:
                    job = job.model_copy(update={"deployment_id": ckpt.deployment_ref})
            except RoboticsAPIError as exc:
                progress(f"checkpoint {checkpoint_id}: {exc.code}")
        last = None
        while True:
            dep: Deployment | None
            if job.deployment_ref:
                try:
                    dep = await self.get_deployment(job.deployment_ref)
                except RoboticsAPIError as exc:
                    if exc.status != 404:
                        raise
                    dep = None
            else:
                dep = self._match_deployment(await self.list_deployments(), job, checkpoint_id)
            state = dep.phase if dep else "not listed yet"
            if state != last:
                progress(f"deployment {dep.id if dep else '?'}: {state}")
                last = state
            if dep is not None and dep.phase in DEPLOYMENT_READY:
                return dep
            if dep is not None and dep.phase in DEPLOYMENT_FAILED:
                raise RoboticsError(f"deployment {dep.id} {dep.phase}")
            if time.monotonic() >= deadline:
                raise RoboticsTimeout(f"no ready deployment for job {job.id} after {timeout_s:.0f} s")
            await self._sleep(poll_s)

    # -- sessions
    async def grant_session(
        self, deployment_id: str, *, session_id: str | None = None, ttl_seconds: int = MAX_TTL_S
    ) -> SessionGrant:
        req = SessionGrantRequest(session_id=session_id or new_session_id(), ttl_seconds=ttl_seconds)
        data = await self._request(
            "POST",
            f"/v1/deployments/{deployment_id}/sessions",
            json_body=req.model_dump(mode="json"),
            retry_safe=False,
        )
        grant = SessionGrant.model_validate(data)
        if grant.session_id != req.session_id:
            raise RoboticsAPIError(201, "session_mismatch", "grant returned a different session_id")
        return grant

    async def open_session(
        self,
        deployment_id: str,
        instruction: str,
        *,
        contract: ServingContract | None = None,
        rtc_enabled: bool = True,
        session_id: str | None = None,
        ttl_seconds: int = MAX_TTL_S,
        timeout_s: float = 10.0,
    ) -> InferenceSession:
        """Contract (team key) + grant (team key) + create the robot session on the inference endpoint (tokens)."""
        contract = contract or await self.get_contract(deployment_id)
        grant = await self.grant_session(deployment_id, session_id=session_id, ttl_seconds=ttl_seconds)
        session = InferenceSession(
            grant,
            deployment_id,
            contract=contract,
            timeout_s=timeout_s,
            transport=self._transport,
            forbidden_fingerprints=[self.key_fingerprint],
            sleep=self._sleep,
        )
        try:
            await session.create(instruction, rtc_enabled=rtc_enabled)
        except BaseException:
            await session.aclose()
            raise
        return session


async def _file_chunks(path: Path, size: int = 1 << 20) -> AsyncIterator[bytes]:
    f = await asyncio.to_thread(path.open, "rb")
    try:
        while True:
            block = await asyncio.to_thread(f.read, size)
            if not block:
                return
            yield block
    finally:
        await asyncio.to_thread(f.close)


def new_session_id(prefix: str = "picky") -> str:
    return f"{prefix}-{int(time.time())}-{_secrets.token_hex(4)}"


def inference_base(endpoint_url: str) -> str:
    """The grant's ``endpoint_url`` as the base of the inference routes. The guide calls them relative to it
    (``$ENDPOINT_URL/sessions/{sid}/predict``) while the reference lists ``/v1/inference/...``: a bare origin gets
    ``/v1/inference`` appended, anything with a path is used as is."""
    url = endpoint_url.rstrip("/")
    path = urlsplit(url).path
    if path in ("", "/"):
        return url + "/v1/inference"
    if path.endswith("/v1"):
        return url + "/inference"
    return url


# ---- the inference session (session tokens only) --------------------------------------------------------------------
class InferenceSession:
    """One robot session on the assigned GPU: ``create`` -> ``predict``* -> ``reset`` / ``delete``. Sends ONLY the two
    session tokens; ``forbidden_fingerprints`` (the team key's SHA-256) are refused by a request hook."""

    def __init__(
        self,
        grant: SessionGrant,
        deployment_id: str,
        *,
        contract: ServingContract,
        timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
        forbidden_fingerprints: Iterable[str] = (),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self.grant = grant
        self.deployment_id = deployment_id
        self.contract = contract
        self.session_id = grant.session_id
        self.base = inference_base(grant.endpoint_url)
        self.timeout_s = timeout_s
        self.sequence = 0
        self.created = False
        self.instruction = ""
        self.rtc_enabled = True
        self._t0 = time.monotonic()
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            follow_redirects=False,
            event_hooks={"request": [_KeyGuard(forbidden_fingerprints)]},
        )

    def __repr__(self) -> str:
        return f"InferenceSession(session_id={self.session_id!r}, base={self.base!r}, sequence={self.sequence})"

    async def __aenter__(self) -> InferenceSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.grant.application_session_token.get_secret_value()}",
            "Accept": "application/json",
        }
        if self.grant.modal_session_token is not None:
            h["Modal-Authorization"] = f"Bearer {self.grant.modal_session_token.get_secret_value()}"
        return h

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        files: Any = None,
        data: Any = None,
        request_timeout: float | None = None,
        retries: int = 1,
    ) -> Any:
        headers = self._headers()
        kwargs: dict[str, Any] = {"headers": headers}
        if json_body is not None:
            kwargs["json"] = json_body
        if files is not None:
            kwargs["files"] = files
        if data is not None:
            kwargs["data"] = data
        if request_timeout is not None:
            kwargs["timeout"] = request_timeout
        for attempt in range(retries + 1):
            try:
                resp = await self._http.request(method, self.base + path, **kwargs)
            except httpx.TimeoutException as exc:
                raise RoboticsAPIError(0, "timeout", f"inference {path} timed out", method=method, path=path) from exc
            except httpx.TransportError as exc:
                raise RoboticsAPIError(0, "network", exc.__class__.__name__, method=method, path=path) from exc
            if resp.status_code == 429 and attempt < retries:
                await self._sleep(min(_retry_after(resp, 1.0), 10.0))
                continue
            if resp.status_code == 303 and resp.headers.get("location"):
                return await follow_poll(
                    self._http,
                    self.base,
                    resp.headers["location"],
                    timeout_s=self.timeout_s * 6,
                    sleep=self._sleep,
                    method=method,
                    path=path,
                )
            if resp.status_code >= 400:
                raise _api_error(resp, method, path)
            return resp.json() if resp.content else {}
        raise RoboticsAPIError(429, "rate_limited", "inference rate limit", method=method, path=path)

    async def create(self, instruction: str, *, rtc_enabled: bool = True) -> dict[str, Any]:
        body = InferenceSessionRequest(
            instruction=instruction, contract_id=self.contract.contract_id, rtc_enabled=rtc_enabled
        ).model_dump(mode="json")
        data = await self._call("POST", f"/deployments/{self.deployment_id}/sessions", json_body=body)
        if isinstance(data, Mapping) and data.get("contract_id") and data["contract_id"] != self.contract.contract_id:
            raise RoboticsAPIError(200, "contract_mismatch", "session bound to a different contract")
        self.created = True
        self.instruction = instruction
        self.rtc_enabled = rtc_enabled
        self.sequence = 0
        self._t0 = time.monotonic()
        return dict(data) if isinstance(data, Mapping) else {}

    async def inference_contract(self) -> ServingContract:
        return ServingContract.model_validate(await self._call("GET", f"/deployments/{self.deployment_id}/contract"))

    async def predict(
        self,
        state: Sequence[float],
        side: bytes,
        wrist: bytes,
        *,
        image_encoding: ImageEncoding = "jpeg",
        image_shape: Sequence[int] | None = None,
        rtc: RtcHint | None = None,
        capture_age_ms: float | None = None,
        deadline_ms: float | None = None,
        timestamp_seconds: float | None = None,
        observation_id: str | None = None,
    ) -> PredictResult:
        """One observation -> one action chunk. ``state`` in CONTRACT units (arm degrees, gripper 0-100). ``sequence``
        and ``timestamp_seconds`` increase on every call (session clock); ``deadline_ms`` also caps the HTTP wait."""
        if not self.created:
            raise RoboticsConfigError("create the session before predict")
        seq = self.sequence
        ts = timestamp_seconds if timestamp_seconds is not None else round(time.monotonic() - self._t0, 4)
        meta = PredictMetadata(
            contract_id=self.contract.contract_id,
            observation_id=observation_id or f"{self.session_id}-{seq:06d}",
            sequence=seq,
            timestamp_seconds=ts,
            state=[float(v) for v in state],
            image_encoding=image_encoding,
            image_shape=list(image_shape) if image_shape else None,
            rtc=rtc if self.rtc_enabled else None,
            capture_age_ms=capture_age_ms,
            deadline_ms=deadline_ms,
        )
        ext, ctype = {"jpeg": ("jpg", "image/jpeg"), "png": ("png", "image/png")}.get(
            image_encoding, ("rgb", "application/octet-stream")
        )
        meta_json = meta.model_dump_json(exclude_none=True)
        if len(side) + len(wrist) + len(meta_json) > MAX_JSON_BODY_BYTES - 4096:
            raise RoboticsConfigError(
                f"observation is {(len(side) + len(wrist)) // 1024} KiB: re-encode the images smaller (512 KiB cap)"
            )
        files = {"side": (f"side.{ext}", side, ctype), "wrist": (f"wrist.{ext}", wrist, ctype)}
        timeout = None
        if deadline_ms:
            timeout = max(0.5, deadline_ms / 1000.0 + 0.5)
        t0 = time.perf_counter()
        self.sequence += 1  # a sent sequence number is never reused, even when the call fails
        data = await self._call(
            "POST",
            f"/sessions/{self.session_id}/predict",
            files=files,
            data={"metadata": meta_json},
            request_timeout=timeout,
            retries=0,
        )
        return PredictResult.from_api(data, (time.perf_counter() - t0) * 1000.0)

    async def reset(self) -> dict[str, Any]:
        data = await self._call("POST", f"/sessions/{self.session_id}/reset")
        self.sequence = 0
        self._t0 = time.monotonic()
        return dict(data) if isinstance(data, Mapping) else {}

    async def delete(self) -> dict[str, Any]:
        data = await self._call("DELETE", f"/sessions/{self.session_id}")
        self.created = False
        return dict(data) if isinstance(data, Mapping) else {}

    async def health(self) -> dict[str, Any]:
        return await self._call("GET", "/health")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def close(self) -> None:
        """Delete the session (best effort) and close the connection pool."""
        try:
            if self.created:
                await self.delete()
        except RoboticsError as exc:
            log.info("perceptron session %s delete failed: %s", self.session_id, exc)
        finally:
            await self.aclose()


# ---- pipeline state + helpers ---------------------------------------------------------------------------------------
def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        return json.loads((path or STATE_FILE).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(update: Mapping[str, Any], path: Path | None = None) -> None:
    path = path or STATE_FILE
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {**load_state(path), **{k: v for k, v in update.items() if v is not None}}
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        path.write_text(json.dumps(data, indent=2))
    except OSError as exc:
        log.info("could not write %s: %s", path, exc)


def default_deployment_id() -> str:
    """PERCEPTRON_ROBOTICS_DEPLOYMENT_ID, else the last pipeline's ready deployment (state file)."""
    return resolve_setting(DEPLOYMENT_ENV)[0] or str(load_state().get("deployment_id") or "")


def format_event(ev: Any) -> str:
    if isinstance(ev, Mapping):
        msg = ev.get("message") or ev.get("msg") or ev.get("text") or ev.get("event")
        if msg:
            prefix = " ".join(str(ev[k]) for k in ("level", "stage", "step") if ev.get(k) is not None)
            return f"{prefix} {msg}".strip()
        return json.dumps(ev, separators=(",", ":"), default=str)[:300]
    return str(ev)[:300]


def synthetic_frames(size: tuple[int, int] = (640, 480)) -> tuple[bytes, bytes]:
    """Deterministic SYNTHETIC side + wrist JPEGs (a shelf with boxes, no people) for a smoke predict."""
    from PIL import Image, ImageDraw

    w, h = size
    frames = []
    for view in ("side", "wrist"):
        img = Image.new("RGB", (w, h), (196, 200, 204))
        d = ImageDraw.Draw(img)
        if view == "side":
            for row, y in enumerate((h * 0.25, h * 0.55, h * 0.85)):
                d.rectangle([0, y, w, y + 8], fill=(120, 110, 100))
                for i in range(6):
                    x = 30 + i * (w - 60) / 6
                    color = ((60 + 30 * i) % 255, (180 - 20 * row) % 255, (90 + 40 * i) % 255)
                    d.rectangle([x, y - 70, x + 60, y], fill=color, outline=(30, 30, 30))
        else:
            d.rectangle([w * 0.3, h * 0.25, w * 0.7, h * 0.95], fill=(220, 120, 40), outline=(40, 40, 40), width=4)
            d.rectangle([0, h * 0.9, w, h], fill=(90, 90, 95))
        d.text((8, 8), f"PICKY SYNTHETIC {view.upper()}", fill=(20, 20, 20))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        frames.append(buf.getvalue())
    return frames[0], frames[1]


@dataclass
class PipelineResult:
    dataset_id: str
    job_id: str
    deployment_id: str
    contract_id: str
    checkpoint_id: str | None = None
    chunk_rows: int = 0
    chunk_id: str = ""
    first_action: list[float] = field(default_factory=list)
    roundtrip_ms: float = 0.0


def _check_capabilities(caps: Any, req: JobRequest, progress: Progress) -> None:
    """Warn (never refuse: the document's shape is not in the spec, the service answers 422 itself) when the
    capabilities document lists allowed values and ours is not among them."""
    if not isinstance(caps, Mapping):
        return
    checks = {
        "mode": (("modes", "training_modes"), req.mode),
        "action_horizon": (("action_horizons", "horizons"), req.action_horizon),
        "backend": (("backends",), req.backend),
    }
    for label, (keys, value) in checks.items():
        allowed = _find(caps, keys, depth=3)
        if isinstance(allowed, Mapping):
            allowed = list(allowed.keys())
        if isinstance(allowed, list) and allowed:
            flat = [a.get("name") or a.get("id") if isinstance(a, Mapping) else a for a in allowed]
            if all(isinstance(a, str | int) for a in flat) and str(value) not in map(str, flat):
                progress(f"  warning: {label} {value!r} not in the capabilities list {flat}; submitting anyway")
    progress(f"submitting backend={req.backend} mode={req.mode} horizon={req.action_horizon} steps={req.steps}")


async def run_pipeline(
    client: RoboticsClient,
    source: str,
    *,
    job: Mapping[str, Any] | None = None,
    instruction: str = "pick up the item from the shelf",
    dataset_name: str | None = None,
    side: bytes | None = None,
    wrist: bytes | None = None,
    frames_blurred: bool = False,
    state_usd_deg: Sequence[float] = REST_POSE_USD_DEG,
    fresh: bool = False,
    skip_check: bool = False,
    timeouts: Mapping[str, float] | None = None,
    progress: Progress = _noop,
) -> PipelineResult:
    """register/upload -> validation -> train -> ready deployment -> contract -> session -> one predict."""
    t = {"dataset": 3600.0, "job": 6 * 3600.0, "deployment": 3600.0, **(timeouts or {})}
    path = Path(source).expanduser()  # noqa: ASYNC240 - no I/O
    if _is_dir(path):
        if not skip_check:
            chk = check_dataset_dir(path)
            for w in chk.warnings:
                progress(f"  check warning: {w}")
            if not chk.ok:
                raise RoboticsConfigError("local dataset check failed: " + "; ".join(chk.errors))
            progress(f"local dataset check ok: {chk.info}")
        ds = await client.upload_dataset_dir(path, name=dataset_name, fresh=fresh, progress=progress)
    elif REPO_ID_PATTERN.match(source):
        ds = await client.create_dataset_hf(source, name=dataset_name, fresh=fresh)
        progress(f"dataset {ds.id} registered from huggingface:{source} ({ds.phase or 'queued'})")
    else:
        raise RoboticsConfigError(f"{source!r} is neither a local dataset directory nor an 'org/name' repo id")
    save_state({"dataset_id": ds.id, "base_url": client.base_url})
    ds, val = await client.wait_dataset(ds.id, timeout_s=t["dataset"], progress=progress)
    progress(f"dataset {ds.id} ready; validation: {val.phase if val else 'n/a'}")

    req = JobRequest(dataset_id=ds.id, **dict(job or {}))
    _check_capabilities(await client.capabilities(), req, progress)
    j = await client.submit_job(req, fresh=fresh)
    progress(f"job {j.id} submitted ({j.phase or 'queued'}): {req.model_dump_json()}")
    save_state({"job_id": j.id})
    j = await client.wait_job(j.id, timeout_s=t["job"], progress=progress)

    dep = await client.wait_deployment(j, timeout_s=t["deployment"], progress=progress)
    contract = await client.get_contract(dep.id)
    progress(contract.summary())
    save_state({"checkpoint_id": j.checkpoint_ref, "deployment_id": dep.id, "contract_id": contract.contract_id})

    from arms.robot.policy_backends import PerceptronBackend  # same arm; imported late (it imports this module)

    if side is None or wrist is None:
        side, wrist = synthetic_frames(contract.cameras.get("side", (640, 480)))
        frames_blurred = True  # synthetic: drawn boxes, no people
    backend = PerceptronBackend(client=client, deployment_id=dep.id, contract=contract, rtc_enabled=req.rtc_enabled)
    try:
        await backend.reset(instruction)
        chunk = await backend.predict(state_usd_deg, side, wrist, frames_blurred=frames_blurred)
    finally:
        await backend.close()
    progress(
        f"predict ok: {len(chunk.actions_deg)} x 6 actions @ {chunk.action_hz:g} Hz, chunk {chunk.chunk_id or '?'}, "
        f"execute rows {chunk.execute_from}..{chunk.execute_from + (chunk.execute_count or 0)}, "
        f"{chunk.latency_ms:.0f} ms"
    )
    return PipelineResult(
        dataset_id=ds.id,
        job_id=j.id,
        deployment_id=dep.id,
        contract_id=contract.contract_id,
        checkpoint_id=j.checkpoint_ref,
        chunk_rows=len(chunk.actions_deg),
        chunk_id=chunk.chunk_id,
        first_action=chunk.actions_deg[0] if chunk.actions_deg else [],
        roundtrip_ms=chunk.latency_ms,
    )


# ---- CLI ------------------------------------------------------------------------------------------------------------
def _say(msg: str) -> None:
    print(msg, flush=True)


def _dump(obj: Any) -> None:
    if isinstance(obj, BaseModel):
        obj = obj.model_dump(mode="json")
    print(json.dumps(obj, indent=2, default=str), flush=True)


def _job_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--steps", type=int, default=1000, help="1-10000 (default 1000)")
    p.add_argument("--backend", choices=["standard", "shared"], default="standard")
    p.add_argument("--mode", choices=["expert_only", "backbone_lora", "full"], default="expert_only")
    p.add_argument("--horizon", type=int, choices=[20, 30], default=30, help="action_horizon")
    p.add_argument("--no-rtc", action="store_true", help="rtc_enabled=false")


def _job_body(a: argparse.Namespace) -> dict[str, Any]:
    return {
        "backend": a.backend,
        "mode": a.mode,
        "action_horizon": a.horizon,
        "rtc_enabled": not a.no_rtc,
        "steps": a.steps,
    }


def _frames(a: argparse.Namespace) -> tuple[bytes, bytes, bool]:
    if a.side or a.wrist:
        if not (a.side and a.wrist):
            raise RoboticsConfigError("--side and --wrist go together")
        if not a.frames_blurred:
            raise RoboticsConfigError(
                "pass --frames-blurred to confirm these frames are face-blurred or contain no people (CLAUDE.md §8)"
            )
        return Path(a.side).read_bytes(), Path(a.wrist).read_bytes(), True
    side, wrist = synthetic_frames()
    return side, wrist, True


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m arms.robot.perceptron_vla",
        description="Perceptron robotics VLA (robotics.perceptron.inc): datasets, training, deployments, inference.",
    )
    p.add_argument("--base-url", default=None, help=f"default ${BASE_URL_ENV} or {DEFAULT_BASE_URL}")
    p.add_argument("--json", action="store_true", help="print raw JSON records")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="key presence (masked) + service health + whether the key is accepted")
    sub.add_parser("capabilities", help="GET /v1/capabilities")
    s = sub.add_parser("dataset-check", help="local checklist pre-flight of a LeRobot export (no key)")
    s.add_argument("dir")
    s = sub.add_parser("dataset-hf", help="register a public Hugging Face LeRobot dataset")
    s.add_argument("repo")
    s.add_argument("--name")
    s.add_argument("--revision", default="main")
    s.add_argument("--fresh", action="store_true", help="new idempotency key (a deliberate second submission)")
    s.add_argument("--wait", action="store_true", help="poll until ready + validation")
    s = sub.add_parser("dataset-upload", help="upload a local LeRobot export (signed URLs) and finalize")
    s.add_argument("dir")
    s.add_argument("--name")
    s.add_argument("--fresh", action="store_true")
    s.add_argument("--skip-check", action="store_true")
    s.add_argument("--wait", action="store_true")
    s = sub.add_parser("dataset-status", help="dataset record + validation")
    s.add_argument("id")
    s = sub.add_parser("train", help="POST /v1/jobs")
    s.add_argument("dataset_id")
    _job_args(s)
    s.add_argument("--fresh", action="store_true")
    s.add_argument("--wait", action="store_true", help="follow logs/metrics until the job ends")
    sub.add_parser("jobs", help="list the team's jobs")
    s = sub.add_parser("job", help="job record")
    s.add_argument("id")
    s = sub.add_parser("cancel", help="request job cancellation")
    s.add_argument("id")
    s = sub.add_parser("logs", help="job events from a cursor")
    s.add_argument("id")
    s.add_argument("--cursor", type=int, default=0)
    s.add_argument("--follow", action="store_true")
    s = sub.add_parser("metrics", help="job metrics from a cursor")
    s.add_argument("id")
    s.add_argument("--cursor", type=int, default=0)
    s = sub.add_parser("checkpoint", help="checkpoint record")
    s.add_argument("id")
    sub.add_parser("deployments", help="list the team's deployments")
    s = sub.add_parser("contract", help="serving contract of a deployment")
    s.add_argument("id")
    for name, hlp in (
        ("session", "grant + create a robot session, print it (no tokens), delete it"),
        ("predict-test", "open a session and run predict(s) on sample frames"),
    ):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("deployment_id", nargs="?", default=None, help=f"default ${DEPLOYMENT_ENV} / last pipeline")
        s.add_argument("--instruction", default="pick up the item from the shelf")
        s.add_argument("--ttl", type=int, default=3600)
        s.add_argument("--no-rtc", action="store_true")
        if name == "predict-test":
            s.add_argument("--side", help="side camera image (jpeg)")
            s.add_argument("--wrist", help="wrist camera image (jpeg)")
            s.add_argument("--frames-blurred", action="store_true", help="confirm the given frames are face-safe")
            s.add_argument(
                "--state",
                type=float,
                nargs=6,
                default=list(REST_POSE_USD_DEG),
                help="6 USD joint degrees (default: rest pose)",
            )
            s.add_argument("--n", type=int, default=2, help="predicts (the 2nd+ use RTC when enabled)")
    s = sub.add_parser(
        "pipeline", help="dataset -> validation -> train -> deployment -> contract -> session -> predict"
    )
    s.add_argument("source", help="local LeRobot export dir or public HF repo id (org/name)")
    _job_args(s)
    s.add_argument("--name", help="dataset name")
    s.add_argument("--instruction", default="pick up the item from the shelf")
    s.add_argument("--fresh", action="store_true")
    s.add_argument("--skip-check", action="store_true")
    s.add_argument("--side")
    s.add_argument("--wrist")
    s.add_argument("--frames-blurred", action="store_true")
    s.add_argument("--dataset-timeout", type=float, default=3600.0)
    s.add_argument("--job-timeout", type=float, default=6 * 3600.0)
    s.add_argument("--deployment-timeout", type=float, default=3600.0)
    return p


async def _cli(a: argparse.Namespace) -> int:
    if a.cmd == "dataset-check":
        chk = check_dataset_dir(a.dir)
        _say(f"info: {chk.info}")
        for w in chk.warnings:
            _say(f"warning: {w}")
        for e in chk.errors:
            _say(f"ERROR: {e}")
        _say("dataset check: OK" if chk.ok else "dataset check: FAILED")
        return 0 if chk.ok else 1
    if a.cmd == "doctor":
        key, source = resolve_api_key()
        base = (a.base_url or resolve_base_url()).rstrip("/")
        _say(f"{KEY_NAME}: {mask(key)}{f' (from {source})' if source else ''}")
        _say(f"base url: {base}")
        _say(f"default deployment: {default_deployment_id() or '-'}")
        async with httpx.AsyncClient(base_url=base, timeout=15.0) as h:
            try:
                r = await h.get("/healthz")
                _say(f"GET /healthz -> {r.status_code} {r.text[:80]}")
            except httpx.HTTPError as exc:
                _say(f"GET /healthz failed: {exc.__class__.__name__}")
        if not key:
            _say("no team key: ask Perceptron staff, then `uv run python -m utils.secrets set " + KEY_NAME + "`")
            return 2
        async with RoboticsClient(key, base_url=base) as c:
            try:
                caps = await c.capabilities()
                _say("team key accepted (GET /v1/capabilities 200)")
                if a.json:
                    _dump(caps)
            except RoboticsAPIError as exc:
                _say(f"team key check failed: {exc}")
                return 3
        return 0

    async with RoboticsClient(base_url=a.base_url) as c:
        if a.cmd == "capabilities":
            _dump(await c.capabilities())
        elif a.cmd == "dataset-hf":
            ds = await c.create_dataset_hf(a.repo, name=a.name, revision=a.revision, fresh=a.fresh)
            _say(f"dataset {ds.id}: {ds.phase or 'queued'}")
            save_state({"dataset_id": ds.id, "base_url": c.base_url})
            if a.wait:
                ds, val = await c.wait_dataset(ds.id, progress=_say)
                _say(f"ready; validation: {val.phase if val else 'n/a'}")
            if a.json:
                _dump(ds)
        elif a.cmd == "dataset-upload":
            if not a.skip_check:
                chk = check_dataset_dir(a.dir)
                for w in chk.warnings:
                    _say(f"warning: {w}")
                if not chk.ok:
                    for e in chk.errors:
                        _say(f"ERROR: {e}")
                    _say("local check failed (use --skip-check to upload anyway)")
                    return 1
            ds = await c.upload_dataset_dir(a.dir, name=a.name, fresh=a.fresh, progress=_say)
            save_state({"dataset_id": ds.id, "base_url": c.base_url})
            if a.wait:
                ds, val = await c.wait_dataset(ds.id, progress=_say)
                _say(f"ready; validation: {val.phase if val else 'n/a'}")
            _say(f"dataset {ds.id}: {ds.phase}")
            if a.json:
                _dump(ds)
        elif a.cmd == "dataset-status":
            ds = await c.get_dataset(a.id)
            _say(f"dataset {ds.id}: {ds.phase}")
            val = await c._validation_or_none(a.id)
            if val is not None:
                _say(f"validation: {val.phase or '?'} ok={val.ok}")
                for issue in val.issues():
                    _say(f"  - {issue}")
            if a.json:
                _dump({"dataset": ds.raw, "validation": val.raw if val else None})
        elif a.cmd == "train":
            req = JobRequest(dataset_id=a.dataset_id, **_job_body(a))
            j = await c.submit_job(req, fresh=a.fresh)
            save_state({"job_id": j.id, "dataset_id": a.dataset_id})
            _say(f"job {j.id}: {j.phase or 'queued'}  ({req.model_dump_json()})")
            if a.wait:
                j = await c.wait_job(j.id, progress=_say)
                _say(f"job {j.id}: {j.phase}  checkpoint={j.checkpoint_ref} deployment={j.deployment_ref}")
            if a.json:
                _dump(j)
        elif a.cmd == "jobs":
            for j in await c.list_jobs():
                _say(f"{j.id}  {j.phase:<10}  dataset={j.get('dataset_id')}  checkpoint={j.checkpoint_ref}")
        elif a.cmd == "job":
            j = await c.get_job(a.id)
            _say(f"job {j.id}: {j.phase}  checkpoint={j.checkpoint_ref} deployment={j.deployment_ref}")
            if a.json:
                _dump(j)
        elif a.cmd == "cancel":
            j = await c.cancel_job(a.id)
            _say(f"job {j.id or a.id}: {j.phase or 'cancel requested'}")
        elif a.cmd == "logs":
            cursor = a.cursor
            while True:
                page = await c.job_logs(a.id, cursor)
                if page.truncated_before:
                    _say(f"(truncated before: {page.truncated_before})")
                for ev in page.events:
                    _say(format_event(ev))
                cursor = page.next_cursor if page.next_cursor is not None else cursor + len(page.events)
                if not a.follow:
                    _say(f"next cursor: {cursor}")
                    break
                j = await c.get_job(a.id)
                if j.phase in JOB_SUCCEEDED | JOB_FAILED and not page.events:
                    _say(f"job {a.id}: {j.phase}")
                    break
                await asyncio.sleep(5.0)
        elif a.cmd == "metrics":
            page = await c.job_metrics(a.id, a.cursor)
            for m in page.metrics:
                _say(format_event(m))
            _say(f"next cursor: {page.next_cursor}")
        elif a.cmd == "checkpoint":
            _dump(await c.get_checkpoint(a.id))
        elif a.cmd == "deployments":
            deps = await c.list_deployments()
            for d in deps:
                _say(f"{d.id}  {d.phase:<10}  checkpoint={d.checkpoint_ref}  modal={d.is_modal}")
            if not deps:
                _say("no deployments")
        elif a.cmd == "contract":
            contract = await c.get_contract(a.id)
            _say(contract.summary())
            _dump(contract)
        elif a.cmd in ("session", "predict-test"):
            dep_id = a.deployment_id or default_deployment_id()
            if not dep_id:
                raise RoboticsConfigError(f"no deployment id (argument, ${DEPLOYMENT_ENV} or a finished pipeline)")
            if a.cmd == "session":
                contract = await c.get_contract(dep_id)
                _say(contract.summary())
                session = await c.open_session(
                    dep_id, a.instruction, contract=contract, rtc_enabled=not a.no_rtc, ttl_seconds=a.ttl
                )
                async with session:
                    _say(f"session created: {json.dumps(session.grant.public())}")
                    try:
                        _say(f"inference health: {json.dumps(await session.health())[:200]}")
                    except RoboticsAPIError as exc:
                        _say(f"inference health: {exc.code}")
                _say("session deleted")
            else:
                from arms.robot.policy_backends import PerceptronBackend

                side, wrist, blurred = _frames(a)
                backend = PerceptronBackend(
                    client=c, deployment_id=dep_id, rtc_enabled=not a.no_rtc, session_ttl_s=a.ttl
                )
                try:
                    await backend.reset(a.instruction)
                    _say(backend.contract.summary() if backend.contract else "")
                    state = list(a.state)
                    executed: int | None = None
                    for i in range(max(1, a.n)):
                        chunk = await backend.predict(
                            state, side, wrist, frames_blurred=blurred, executed=executed, delay=2 if i else 0
                        )
                        _say(
                            f"predict {i}: {len(chunk.actions_deg)}x6 @ {chunk.action_hz:g} Hz chunk={chunk.chunk_id} "
                            f"execute_from={chunk.execute_from} count={chunk.execute_count} "
                            f"{chunk.latency_ms:.0f} ms rtc={chunk.rtc_used}"
                        )
                        _say(f"  first row (USD deg): {[round(v, 2) for v in chunk.actions_deg[0]]}")
                        executed = min(chunk.execute_from + 8, len(chunk.actions_deg) - 1)  # as if 8 rows ran
                        state = chunk.actions_deg[executed]
                finally:
                    await backend.close()
        elif a.cmd == "pipeline":
            side = wrist = None
            blurred = False
            if a.side or a.wrist:
                side, wrist, blurred = _frames(a)
            res = await run_pipeline(
                c,
                a.source,
                job=_job_body(a),
                instruction=a.instruction,
                dataset_name=a.name,
                side=side,
                wrist=wrist,
                frames_blurred=blurred,
                fresh=a.fresh,
                skip_check=a.skip_check,
                timeouts={"dataset": a.dataset_timeout, "job": a.job_timeout, "deployment": a.deployment_timeout},
                progress=_say,
            )
            _say(
                f"PIPELINE OK: dataset={res.dataset_id} job={res.job_id} deployment={res.deployment_id} "
                f"contract={res.contract_id[:12]}… chunk={res.chunk_rows}x6"
            )
            _say(f"T1 backend ready: PICKY_VLA_POLICY_PROTOCOL=perceptron_vla {DEPLOYMENT_ENV}={res.deployment_id}")
            if a.json:
                _dump(res.__dict__)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_cli(args))
    except RoboticsConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RoboticsTimeout as exc:
        print(f"timeout: {exc} (re-run the same command to resume)", file=sys.stderr)
        return 4
    except RoboticsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130


__all__ = [
    "DEFAULT_BASE_URL",
    "KEY_NAME",
    "STATE_NAMES",
    "Checkpoint",
    "Dataset",
    "DatasetCheck",
    "DatasetRequest",
    "Deployment",
    "InferenceSession",
    "Job",
    "JobRequest",
    "LocalFile",
    "LogPage",
    "MetricsPage",
    "PipelineResult",
    "PredictMetadata",
    "PredictResult",
    "RoboticsAPIError",
    "RoboticsClient",
    "RoboticsConfigError",
    "RoboticsError",
    "RoboticsTimeout",
    "RtcHint",
    "ServingContract",
    "SessionGrant",
    "UploadFile",
    "Validation",
    "check_dataset_dir",
    "default_deployment_id",
    "follow_poll",
    "idempotency_key",
    "inference_base",
    "resolve_api_key",
    "run_pipeline",
    "scan_dataset_dir",
    "synthetic_frames",
]


if __name__ == "__main__":
    raise SystemExit(main())
