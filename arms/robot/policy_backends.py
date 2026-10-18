"""Policy backends for the learned VLA tiers: ONE interface, several policies, so the same in-sim executor runs any.

    executor (T1 skill loop: Isaac worker PolicyThread or the gateway-side exec agent)
      reset(instruction)                                  new grasp attempt / episode
      loop: blur frames -> predict(state_deg[6], side_jpeg, wrist_jpeg, frames_blurred=True,
                                   executed=<next row of the previous chunk>, delay=<rows that run during inference>)
            -> ActionChunk.actions_deg (N x 6 USD joint DEGREES, SO-101 order) -> run rows
               chunk.execute_from : execute_from + (execute_count or info.default_execute) at chunk.action_hz
      close()

Backends:
- ``PerceptronBackend``: the Perceptron robotics VLA (robotics.perceptron.inc) behind a ready deployment. Opens a
  robot session (team key -> contract + grant; session tokens -> create / predict / reset / delete), maps units per
  the serving contract (arm degrees pass through; gripper 0-100 <-> our USD gripper -10..100 deg, LeIsaac's mapping),
  real-time chunking (``rtc``: prior chunk id + start index + delay + committed rows), re-opens an expired session once.
- ``SmolVLABackend``: the SmolVLA SO-101 policy server on vhagar (``localhost:8200``; HTTP ``POST /infer``, the schema
  of ``vla-server/client.py``). Its wire is already USD degrees, so no unit mapping. No RTC.
- ``ScriptedBackend``: deterministic chunks toward a target pose (tests / GPU-less executor runs).

Cameras: the policy's ``side`` view is our robot ``front`` camera (``CAMERA_MAP``); ``wrist`` is ``wrist``. Privacy
(CLAUDE.md section 8): every ``predict`` takes ``frames_blurred`` and refuses ``False`` (ValueError), so frames reach a
policy only after the caller blurred faces (perception's YuNet on Isaac frames) or knows they hold no people.

Errors: ``PolicyBackendError.code`` is a ``VlaErrorCode`` (policy_unavailable / policy_timeout / policy_diverged /
internal) the VLA service reports as is. ``policy_capability`` builds the T1 ``VlaCapabilities`` row for a backend.
``SyncPolicyBackend`` runs any backend on a private event loop for synchronous callers (the Isaac main loop's thread).
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import io
import math
import threading
import time
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

import httpx

from arms.robot.perceptron_vla import (
    DEFAULT_BASE_URL,
    DEPLOYMENT_ENV,
    KEY_NAME,
    MAX_TTL_S,
    InferenceSession,
    RoboticsAPIError,
    RoboticsClient,
    RoboticsConfigError,
    RoboticsError,
    RtcHint,
    ServingContract,
    default_deployment_id,
    new_session_id,
    resolve_api_key,
    resolve_setting,
)
from contracts.cameras import RobotCameraKind
from contracts.common import RobotId
from contracts.plan import StepKind, VlaTier
from contracts.sim import RobotBackend
from contracts.skill_server import SO101_JOINT_NAMES
from contracts.vla import VlaCapabilities
from utils.log import get_logger

log = get_logger(__name__)

PolicyErrorCode = Literal["policy_unavailable", "policy_timeout", "policy_diverged", "internal"]
BackendName = Literal["perceptron", "smolvla", "scripted"]

USD_JOINT_LIMITS_DEG: dict[str, tuple[float, float]] = {
    "shoulder_pan": (-110.0, 110.0),
    "shoulder_lift": (-100.0, 100.0),
    "elbow_flex": (-100.0, 90.0),
    "wrist_flex": (-95.0, 95.0),
    "wrist_roll": (-160.0, 160.0),
    "gripper": (-10.0, 100.0),
}
"""SO-101 USD joint limits (LeIsaac ``SO101_FOLLOWER_USD_JOINT_LIMLITS``; lekiwi.usd authors the same)."""
GRIPPER_USD_RANGE_DEG: tuple[float, float] = USD_JOINT_LIMITS_DEG["gripper"]
DIVERGED_MARGIN_DEG = 60.0
"""A value further than this outside its joint's USD limits = policy_diverged; closer = clipped (+ counted)."""
CAMERA_MAP: dict[str, RobotCameraKind] = {"side": "front", "wrist": "wrist"}
"""Policy camera -> robot camera."""
DEFAULT_IMAGE_SIZE: tuple[int, int] = (640, 480)
JPEG_MAX_BYTES = 200_000
"""Per image: two images + metadata stay well under the service's 512 KiB request cap."""
REST_POSE_USD_DEG: tuple[float, ...] = (0.0, -100.0, 90.0, 50.0, 0.0, -10.0)


class PolicyBackendError(Exception):
    """A backend failure, typed as the ``VlaErrorCode`` the VLA service reports."""

    def __init__(self, code: PolicyErrorCode, message: str = "") -> None:
        self.code: PolicyErrorCode = code
        self.message = message or code
        super().__init__(f"{code}: {self.message}")


# ---- units ----------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class So101UnitMap:
    """USD joint degrees (our articulation, SO101_JOINT_NAMES order) <-> a policy's state / action units.

    Arm joints: ``policy = (deg + arm_offset_deg) * arm_scale`` (degrees -> scale 1, radians -> pi/180). Gripper:
    linear from ``gripper_usd`` onto ``gripper_policy`` (Perceptron / LeRobot calibrated 0-100; LeIsaac's
    ``convert_leisaac_action_to_lerobot`` mapping)."""

    arm_scale: float = 1.0
    arm_offset_deg: tuple[float, float, float, float, float] = (0.0, 0.0, 0.0, 0.0, 0.0)
    gripper_usd: tuple[float, float] = GRIPPER_USD_RANGE_DEG
    gripper_policy: tuple[float, float] = (0.0, 100.0)

    def to_policy(self, usd_deg: Sequence[float]) -> list[float]:
        q = _six(usd_deg, "state")
        arm = [(q[i] + self.arm_offset_deg[i]) * self.arm_scale for i in range(5)]
        (u0, u1), (p0, p1) = self.gripper_usd, self.gripper_policy
        return [*arm, (q[5] - u0) / (u1 - u0) * (p1 - p0) + p0]

    def from_policy(self, row: Sequence[float]) -> list[float]:
        q = _six(row, "action")
        arm = [q[i] / self.arm_scale - self.arm_offset_deg[i] for i in range(5)]
        (u0, u1), (p0, p1) = self.gripper_usd, self.gripper_policy
        return [*arm, (q[5] - p0) / (p1 - p0) * (u1 - u0) + u0]

    @classmethod
    def from_contract(cls, contract: ServingContract | None) -> So101UnitMap:
        """Perceptron's checklist units (arm degrees, gripper 0-100) unless the contract states others."""
        if contract is None:
            return cls()
        scale = math.pi / 180.0 if contract.arm_units == "radians" else 1.0
        return cls(arm_scale=scale, gripper_policy=contract.gripper_range)


IDENTITY_UNITS = So101UnitMap(gripper_policy=GRIPPER_USD_RANGE_DEG)
"""Policy wire already in USD degrees (the SmolVLA server)."""


def _six(values: Sequence[float], what: str) -> list[float]:
    out = [float(v) for v in values]
    if len(out) != 6:
        raise ValueError(f"{what} needs 6 values ({', '.join(SO101_JOINT_NAMES)}), got {len(out)}")
    return out


def check_rows(rows_deg: Sequence[Sequence[float]]) -> tuple[list[list[float]], int]:
    """(rows clipped to the USD limits, number of clipped values). PolicyBackendError('policy_diverged') for an empty
    chunk, a row without 6 values, NaN / inf, or a value more than DIVERGED_MARGIN_DEG outside its limits."""
    if not rows_deg:
        raise PolicyBackendError("policy_diverged", "empty action chunk")
    limits = [USD_JOINT_LIMITS_DEG[j] for j in SO101_JOINT_NAMES]
    out: list[list[float]] = []
    clipped = 0
    for i, row in enumerate(rows_deg):
        if len(row) != 6:
            raise PolicyBackendError("policy_diverged", f"action {i} has {len(row)} values, expected 6")
        new = []
        for j, v in enumerate(row):
            v = float(v)
            lo, hi = limits[j]
            if not math.isfinite(v) or v < lo - DIVERGED_MARGIN_DEG or v > hi + DIVERGED_MARGIN_DEG:
                raise PolicyBackendError("policy_diverged", f"action {i} {SO101_JOINT_NAMES[j]} = {v}")
            if v < lo or v > hi:
                clipped += 1
                v = min(hi, max(lo, v))
            new.append(v)
        out.append(new)
    return out, clipped


# ---- images ---------------------------------------------------------------------------------------------------------
def prepare_jpeg(data: bytes, size: tuple[int, int] = DEFAULT_IMAGE_SIZE, max_bytes: int = JPEG_MAX_BYTES) -> bytes:
    """A JPEG of exactly ``size`` (w, h) and at most ``max_bytes``: returned untouched when it already is one, else
    resized / re-encoded (quality 90 -> 50). PolicyBackendError('internal') when the bytes are not an image."""
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        fmt = img.format
        img_size = img.size
    except (UnidentifiedImageError, OSError) as exc:
        raise PolicyBackendError("internal", f"camera frame is not a decodable image: {exc}") from exc
    if fmt == "JPEG" and img_size == size and len(data) <= max_bytes:
        return data
    img = img.convert("RGB")
    if img.size != size:
        img = img.resize(size, Image.BILINEAR)
    out = b""
    for quality in (90, 80, 70, 60, 50):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        out = buf.getvalue()
        if len(out) <= max_bytes:
            break
    return out


def _check_frames(frames_blurred: bool, side_jpeg: bytes, wrist_jpeg: bytes) -> None:
    if not frames_blurred:
        raise ValueError(
            "frames must be face-blurred before they reach a policy (CLAUDE.md section 8): blur them, then pass "
            "frames_blurred=True"
        )
    if not side_jpeg or not wrist_jpeg:
        raise ValueError("both the side (front camera) and the wrist image are required")


# ---- the interface --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class BackendInfo:
    name: BackendName
    model_id: str
    action_hz: float = 30.0
    chunk_size: int | None = None
    cameras: tuple[str, ...] = ("side", "wrist")
    supports_rtc: bool = False
    host: str = ""
    default_execute: int = 15
    """Rows to run per chunk when the chunk names no execution prefix (then predict again with fresh frames)."""


@dataclass
class ActionChunk:
    """One prediction. ``actions_deg`` = N x 6 USD joint degrees (SO101_JOINT_NAMES order), clipped to the joint
    limits; run ``rows_to_execute()`` at ``action_hz``. ``policy_actions`` = the same rows in the policy's units."""

    actions_deg: list[list[float]]
    action_hz: float
    backend: BackendName
    model: str = ""
    chunk_id: str = ""
    sequence: int = 0
    execute_from: int = 0
    """First row to run: with RTC the rows before it were committed (they run from the previous chunk meanwhile)."""
    execute_count: int | None = None
    """Rows to run before the next predict (the service's execution prefix); None = ``BackendInfo.default_execute``."""
    latency_ms: float = 0.0
    rtc_used: bool = False
    clipped: int = 0
    warnings: list[str] = field(default_factory=list)
    policy_actions: list[list[float]] = field(default_factory=list, repr=False)

    def rows_to_execute(self, default_count: int | None = None) -> list[list[float]]:
        count = self.execute_count or default_count or len(self.actions_deg)
        return self.actions_deg[self.execute_from : self.execute_from + count]


@runtime_checkable
class PolicyBackend(Protocol):
    """What the in-sim executor drives. Async; one backend per robot arm; not safe for concurrent predicts."""

    @property
    def info(self) -> BackendInfo: ...

    async def reset(self, instruction: str) -> None:
        """Start a new episode (grasp attempt) for ``instruction``: clears history / RTC state. Raises
        PolicyBackendError('policy_unavailable') when the policy cannot serve (no key, no deployment, server down)."""
        ...

    async def predict(
        self,
        state_deg: Sequence[float],
        side_jpeg: bytes,
        wrist_jpeg: bytes,
        *,
        frames_blurred: bool,
        executed: int | None = None,
        delay: int = 0,
    ) -> ActionChunk:
        """One observation -> one chunk. ``state_deg``: 6 USD joint degrees. ``side_jpeg``: the robot FRONT camera,
        ``wrist_jpeg``: the wrist camera (640x480 preferred; resized otherwise). ``executed``: index (into the
        previous chunk's rows) of the next row the executor will run when this request goes out, None on the first
        call; ``delay``: rows that will run while this inference is in flight (RTC; ignored without RTC)."""
        ...

    async def close(self) -> None: ...


# ---- Perceptron -----------------------------------------------------------------------------------------------------
class PerceptronBackend:
    """Perceptron robotics VLA behind a ready deployment (see module docstring). The team key is used only for the
    contract + session grant; inference carries the session tokens only (enforced in ``InferenceSession``)."""

    def __init__(
        self,
        deployment_id: str | None = None,
        *,
        client: RoboticsClient | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        contract: ServingContract | None = None,
        rtc_enabled: bool = True,
        session_ttl_s: int = 3600,
        request_timeout_s: float = 5.0,
        deadline_ms: float | None = None,
        unit_map: So101UnitMap | None = None,
        session_prefix: str = "picky",
        jpeg_max_bytes: int = JPEG_MAX_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.deployment_id = deployment_id or default_deployment_id()
        self._client = client
        self._own_client = client is None
        self._api_key = api_key
        self._base_url = base_url
        self._transport = transport
        self.contract = contract
        self.rtc_enabled = rtc_enabled
        self.session_ttl_s = max(1, min(int(session_ttl_s), MAX_TTL_S))
        self.request_timeout_s = request_timeout_s
        self.deadline_ms = deadline_ms
        self._unit_map = unit_map
        self.session_prefix = session_prefix
        self.jpeg_max_bytes = jpeg_max_bytes
        self.session: InferenceSession | None = None
        self.instruction = ""
        self._prior_id = ""
        self._prior_rows: list[list[float]] = []
        self.sequence = 0

    @classmethod
    def from_settings(cls, **opts: Any) -> PerceptronBackend:
        """Deployment from PERCEPTRON_ROBOTICS_DEPLOYMENT_ID or the last ``pipeline`` run; key from the vault."""
        return cls(default_deployment_id() or None, **opts)

    @property
    def unit_map(self) -> So101UnitMap:
        return self._unit_map or So101UnitMap.from_contract(self.contract)

    @property
    def info(self) -> BackendInfo:
        c = self.contract
        horizon = c.action_horizon if c else None
        return BackendInfo(
            name="perceptron",
            model_id=f"perceptron/{self.deployment_id or '?'}",
            action_hz=c.action_hz if c else 30.0,
            chunk_size=horizon,
            cameras=("side", "wrist"),
            supports_rtc=self.rtc_enabled,
            host=(self._client.base_url if self._client else self._base_url or DEFAULT_BASE_URL),
            default_execute=(c.execution_horizon if c and c.execution_horizon else max(1, (horizon or 30) // 2)),
        )

    async def _ensure_client(self) -> RoboticsClient:
        if self._client is None:
            try:
                self._client = RoboticsClient(self._api_key, base_url=self._base_url, transport=self._transport)
            except RoboticsConfigError as exc:
                raise PolicyBackendError("policy_unavailable", str(exc)) from exc
        return self._client

    async def _open(self, instruction: str) -> None:
        if not self.deployment_id:
            raise PolicyBackendError(
                "policy_unavailable", f"no Perceptron deployment ({DEPLOYMENT_ENV} unset and no finished pipeline)"
            )
        client = await self._ensure_client()
        try:
            if self.contract is None:
                self.contract = await client.get_contract(self.deployment_id)
            self.session = await client.open_session(
                self.deployment_id,
                instruction,
                contract=self.contract,
                rtc_enabled=self.rtc_enabled,
                session_id=new_session_id(self.session_prefix),
                ttl_seconds=self.session_ttl_s,
                timeout_s=self.request_timeout_s,
            )
        except RoboticsAPIError as exc:
            raise _policy_error(exc) from exc
        except RoboticsError as exc:
            raise PolicyBackendError("policy_unavailable", str(exc)) from exc

    async def _drop_session(self) -> None:
        if self.session is not None:
            session, self.session = self.session, None
            await session.close()

    async def reset(self, instruction: str) -> None:
        self._prior_id, self._prior_rows = "", []
        self.sequence = 0
        if self.session is not None and self.session.created and self.instruction == instruction:
            try:
                await self.session.reset()
                return
            except RoboticsError as exc:  # expired / gone: open a fresh one below
                log.info("perceptron session reset failed (%s); opening a new session", exc)
        await self._drop_session()  # the instruction is bound at session creation
        await self._open(instruction)
        self.instruction = instruction

    def _rtc(self, executed: int | None, delay: int) -> RtcHint | None:
        if not self.rtc_enabled or executed is None or not self._prior_id or not self._prior_rows:
            return None
        start = int(executed)
        if start < 0 or start >= len(self._prior_rows):
            return None  # the previous chunk is used up: a plain prediction
        d = max(0, min(int(delay), len(self._prior_rows) - start))
        committed = [list(r) for r in self._prior_rows[start : start + d]] if d else None
        return RtcHint(prior_chunk_id=self._prior_id, prior_start_index=start, delay=d, committed_actions=committed)

    async def predict(
        self,
        state_deg: Sequence[float],
        side_jpeg: bytes,
        wrist_jpeg: bytes,
        *,
        frames_blurred: bool,
        executed: int | None = None,
        delay: int = 0,
        capture_age_ms: float | None = None,
    ) -> ActionChunk:
        _check_frames(frames_blurred, side_jpeg, wrist_jpeg)
        state = self.unit_map.to_policy(state_deg)
        if self.session is None:
            raise PolicyBackendError("policy_unavailable", "call reset(instruction) before predict")
        cams = self.contract.cameras if self.contract else {}
        side, wrist = await asyncio.gather(
            asyncio.to_thread(prepare_jpeg, side_jpeg, cams.get("side", DEFAULT_IMAGE_SIZE), self.jpeg_max_bytes),
            asyncio.to_thread(prepare_jpeg, wrist_jpeg, cams.get("wrist", DEFAULT_IMAGE_SIZE), self.jpeg_max_bytes),
        )
        rtc = self._rtc(executed, delay)
        t0 = time.perf_counter()
        try:
            res = await self.session.predict(
                state,
                side,
                wrist,
                image_encoding="jpeg",
                rtc=rtc,
                capture_age_ms=capture_age_ms,
                deadline_ms=self.deadline_ms,
            )
        except RoboticsAPIError as exc:
            if exc.status not in (401, 403, 404, 410):
                raise _policy_error(exc) from exc
            # session expired after inactivity: fresh grant + new session (never reuse stale state), RTC dropped
            log.info("perceptron session expired (%s %s); re-opening once", exc.status, exc.code)
            await self._drop_session()
            await self._open(self.instruction)
            assert self.session is not None
            rtc = None
            try:
                res = await self.session.predict(
                    state,
                    side,
                    wrist,
                    image_encoding="jpeg",
                    capture_age_ms=capture_age_ms,
                    deadline_ms=self.deadline_ms,
                )
            except RoboticsAPIError as exc2:
                raise _policy_error(exc2) from exc2
        except RoboticsConfigError as exc:
            raise PolicyBackendError("internal", str(exc)) from exc
        try:
            rows_deg = [self.unit_map.from_policy(r) for r in res.actions]
        except ValueError as exc:
            raise PolicyBackendError("policy_diverged", str(exc)) from exc
        rows_deg, clipped = check_rows(rows_deg)
        self._prior_id = res.chunk_id
        self._prior_rows = [list(r) for r in res.actions]
        seq = res.sequence if res.sequence is not None else self.sequence
        self.sequence += 1
        execute_from = res.execute_from if res.execute_from is not None else (rtc.delay if rtc else 0)
        warnings = [f"{clipped} values clipped to the USD joint limits"] if clipped else []
        if not res.chunk_id and self.rtc_enabled:
            warnings.append("no chunk_id in the response: RTC disabled for the next request")
        return ActionChunk(
            actions_deg=rows_deg,
            action_hz=self.info.action_hz,
            backend="perceptron",
            model=self.info.model_id,
            chunk_id=res.chunk_id,
            sequence=seq,
            execute_from=min(max(0, execute_from), len(rows_deg) - 1),
            execute_count=res.execute_count,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            rtc_used=rtc is not None,
            clipped=clipped,
            warnings=warnings,
            policy_actions=[list(r) for r in res.actions],
        )

    async def close(self) -> None:
        try:
            await self._drop_session()
        finally:
            if self._own_client and self._client is not None:
                await self._client.aclose()
                self._client = None

    async def __aenter__(self) -> PerceptronBackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


def _policy_error(exc: RoboticsAPIError) -> PolicyBackendError:
    if exc.code == "timeout" or exc.status in (408, 504):
        return PolicyBackendError("policy_timeout", str(exc))
    if exc.code == "malformed_chunk":
        return PolicyBackendError("policy_diverged", str(exc))
    if exc.status in (400, 413, 422):
        return PolicyBackendError("internal", str(exc))
    return PolicyBackendError("policy_unavailable", str(exc))


# ---- SmolVLA (vhagar policy server) ---------------------------------------------------------------------------------
class SmolVLABackend:
    """The SmolVLA SO-101 server (vla-server/server.py on vhagar, ``127.0.0.1:8200``; from the Mac through
    ``ssh -N -L 8200:127.0.0.1:8200 vhagar``). Request ``{instruction, state_deg, images: {front, wrist} (base64
    JPEG), reset, clip, n_actions?, seed?}`` -> ``{actions_deg (50 x 6), model, latency_ms, warnings, ...}`` or
    ``{error}``. ``ws://`` URLs are accepted and served over the HTTP endpoint (same schema, same latency)."""

    DEFAULT_URL = "http://127.0.0.1:8200"

    def __init__(
        self,
        url: str | None = None,
        *,
        timeout_s: float = 5.0,
        n_actions: int | None = None,
        seed: int | None = None,
        clip: bool = True,
        default_execute: int = 20,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        raw = (url or self.DEFAULT_URL).rstrip("/")
        if raw.startswith(("ws://", "wss://")):
            raw = "http" + raw[2:]
            raw = raw[: -len("/ws")] if raw.endswith("/ws") else raw
        self.url = raw
        self.timeout_s = timeout_s
        self.n_actions = n_actions
        self.seed = seed
        self.clip = clip
        self.default_execute = default_execute
        self.instruction = ""
        self.model = "smolvla"
        self.chunk_size: int | None = 50
        self._reset_next = True
        self.sequence = 0
        self._http = httpx.AsyncClient(base_url=self.url, timeout=timeout_s, transport=transport)

    @property
    def info(self) -> BackendInfo:
        return BackendInfo(
            name="smolvla",
            model_id=self.model,
            action_hz=30.0,
            chunk_size=self.chunk_size,
            cameras=("side", "wrist"),
            supports_rtc=False,
            host=self.url,
            default_execute=self.default_execute,
        )

    async def health(self) -> dict[str, Any]:
        try:
            resp = await self._http.get("/health")
        except httpx.HTTPError as exc:
            raise PolicyBackendError("policy_unavailable", f"SmolVLA server {self.url} unreachable: {exc}") from exc
        try:
            return dict(resp.json())
        except ValueError as exc:
            raise PolicyBackendError("policy_unavailable", f"SmolVLA /health answered {resp.status_code}") from exc

    async def reset(self, instruction: str) -> None:
        h = await self.health()
        status = str(h.get("status") or "")
        if status != "ok":
            raise PolicyBackendError("policy_unavailable", f"SmolVLA server status {status or '?'}")
        self.instruction = instruction
        self._reset_next = True
        self.sequence = 0

    async def predict(
        self,
        state_deg: Sequence[float],
        side_jpeg: bytes,
        wrist_jpeg: bytes,
        *,
        frames_blurred: bool,
        executed: int | None = None,
        delay: int = 0,
    ) -> ActionChunk:
        _check_frames(frames_blurred, side_jpeg, wrist_jpeg)
        state = _six(state_deg, "state")
        if not self.instruction:
            raise PolicyBackendError("policy_unavailable", "call reset(instruction) before predict")
        side, wrist = await asyncio.gather(
            asyncio.to_thread(prepare_jpeg, side_jpeg), asyncio.to_thread(prepare_jpeg, wrist_jpeg)
        )
        body: dict[str, Any] = {
            "instruction": self.instruction,
            "state_deg": state,
            "images": {"front": base64.b64encode(side).decode(), "wrist": base64.b64encode(wrist).decode()},
            "reset": self._reset_next,
            "clip": self.clip,
        }
        if self.n_actions is not None:
            body["n_actions"] = int(self.n_actions)
        if self.seed is not None:
            body["seed"] = int(self.seed)
        t0 = time.perf_counter()
        try:
            resp = await self._http.post("/infer", json=body)
        except httpx.TimeoutException as exc:
            raise PolicyBackendError("policy_timeout", f"SmolVLA /infer > {self.timeout_s:g} s") from exc
        except httpx.HTTPError as exc:
            raise PolicyBackendError("policy_unavailable", f"SmolVLA server {self.url}: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise PolicyBackendError("policy_unavailable", f"SmolVLA /infer answered {resp.status_code}") from exc
        if not isinstance(data, Mapping) or "error" in data or resp.status_code >= 400:
            msg = str(data.get("error") if isinstance(data, Mapping) else data)[:300]
            code: PolicyErrorCode = "internal" if resp.status_code == 400 else "policy_unavailable"
            raise PolicyBackendError(code, f"SmolVLA: {msg}")
        rows = data.get("actions_deg")
        if not isinstance(rows, list):
            raise PolicyBackendError("policy_diverged", "SmolVLA response has no actions_deg")
        rows_deg, clipped = check_rows(rows)
        self._reset_next = False
        self.model = str(data.get("model") or self.model)
        self.chunk_size = int(data.get("chunk_size") or len(rows_deg))
        seq = self.sequence
        self.sequence += 1
        warnings = [str(w) for w in data.get("warnings") or []]
        if data.get("limit_violations"):
            warnings.append(f"{data['limit_violations']} raw values outside the USD limits (server clipped)")
        return ActionChunk(
            actions_deg=rows_deg,
            action_hz=30.0,
            backend="smolvla",
            model=self.model,
            chunk_id=f"smolvla-{seq}",
            sequence=seq,
            execute_from=0,
            execute_count=None,
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            rtc_used=False,
            clipped=clipped,
            warnings=warnings,
            policy_actions=[list(map(float, r)) for r in rows],
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> SmolVLABackend:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()


# ---- scripted (deterministic, no network) ---------------------------------------------------------------------------
class ScriptedBackend:
    """Deterministic chunks: ``chunk_size`` rows moving linearly from the current state toward ``target_deg`` by at
    most ``max_step_deg`` per row (per joint). For executor tests and GPU-less runs; honours the same checks."""

    def __init__(
        self,
        target_deg: Sequence[float] = (0.0, -20.0, 40.0, 60.0, 90.0, 35.0),
        *,
        chunk_size: int = 30,
        max_step_deg: float = 3.0,
        action_hz: float = 30.0,
        default_execute: int = 15,
    ) -> None:
        self.target = _six(target_deg, "target")
        self.chunk_size = chunk_size
        self.max_step_deg = max_step_deg
        self.action_hz = action_hz
        self.default_execute = default_execute
        self.instruction = ""
        self.sequence = 0
        self.resets = 0

    @property
    def info(self) -> BackendInfo:
        return BackendInfo(
            name="scripted",
            model_id="scripted",
            action_hz=self.action_hz,
            chunk_size=self.chunk_size,
            supports_rtc=False,
            host="local",
            default_execute=self.default_execute,
        )

    async def reset(self, instruction: str) -> None:
        self.instruction = instruction
        self.sequence = 0
        self.resets += 1

    async def predict(
        self,
        state_deg: Sequence[float],
        side_jpeg: bytes,
        wrist_jpeg: bytes,
        *,
        frames_blurred: bool,
        executed: int | None = None,
        delay: int = 0,
    ) -> ActionChunk:
        _check_frames(frames_blurred, side_jpeg, wrist_jpeg)
        q = _six(state_deg, "state")
        rows = []
        for _ in range(self.chunk_size):
            step = self.max_step_deg
            q = [c + max(-step, min(step, t - c)) for c, t in zip(q, self.target, strict=True)]
            rows.append(list(q))
        rows_deg, clipped = check_rows(rows)
        seq = self.sequence
        self.sequence += 1
        return ActionChunk(
            actions_deg=rows_deg,
            action_hz=self.action_hz,
            backend="scripted",
            model="scripted",
            chunk_id=f"scripted-{seq}",
            sequence=seq,
            clipped=clipped,
            policy_actions=rows,
        )

    async def close(self) -> None:
        return None


# ---- factory, capability --------------------------------------------------------------------------------------------
_ALIASES: dict[str, BackendName] = {
    "perceptron": "perceptron",
    "perceptron_vla": "perceptron",
    "smolvla": "smolvla",
    "smolvla_http": "smolvla",
    "scripted": "scripted",
}


def backend_name_from_settings() -> BackendName | None:
    """PICKY_VLA_POLICY_PROTOCOL = perceptron_vla | smolvla | scripted -> the backend name; anything else -> None
    (openpi_ws / perceptron_tool stay with ``arms.robot.t1``)."""
    value, _ = resolve_setting("PICKY_VLA_POLICY_PROTOCOL")
    return _ALIASES.get(value.strip().lower())


def create_policy_backend(name: str | None = None, **opts: Any) -> PolicyBackend:
    """``perceptron`` (opts: deployment_id, client, rtc_enabled, deadline_ms, ...), ``smolvla`` (url defaults to
    PICKY_VLA_POLICY_URL when it is http(s)/ws, else localhost:8200) or ``scripted``. ``name`` None = settings."""
    resolved = _ALIASES.get((name or "").strip().lower()) if name else backend_name_from_settings()
    if resolved == "perceptron":
        return PerceptronBackend(**opts)
    if resolved == "smolvla":
        if "url" not in opts:
            url, _ = resolve_setting("PICKY_VLA_POLICY_URL")
            opts["url"] = url if url.startswith(("http://", "https://", "ws://", "wss://")) else None
        return SmolVLABackend(**opts)
    if resolved == "scripted":
        return ScriptedBackend(**opts)
    raise ValueError(f"unknown policy backend {name!r} (perceptron | smolvla | scripted)")


def policy_capability(
    robot_id: RobotId,
    name: BackendName,
    robot_backend: RobotBackend | None,
    *,
    tier: VlaTier = "t1_pretrained",
    reachable: bool | None = None,
    reason: str = "",
    step_kinds: Sequence[StepKind] = ("grasp",),
) -> VlaCapabilities:
    """The T1 capability row for a policy backend. Available = configured (perceptron: team key + deployment id) AND
    the store runs on Isaac (an articulated SO-101 + rendered cameras; scripted also runs on the 2D fake) AND
    ``reachable`` is not False (the caller's last health check). ``input_frames_blurred`` is True because every
    ``predict`` refuses unblurred frames."""
    why = reason
    model = name
    host = ""
    if name == "perceptron":
        key, _ = resolve_api_key()
        dep = default_deployment_id()
        model, host = f"perceptron/{dep or '?'}", "robotics.perceptron.inc (Modal GPU)"
        if not key:
            why = why or f"{KEY_NAME} not set (team key from Perceptron staff)"
        elif not dep:
            why = why or f"no ready deployment ({DEPLOYMENT_ENV} unset; run the pipeline)"
    elif name == "smolvla":
        model, host = "smolvla (vhagar)", "vhagar:8200"
    if not why and name != "scripted" and robot_backend != "isaac":
        why = "store on the 2D fake: learned policies need the Isaac SO-101"
    if not why and reachable is False:
        why = "policy server unreachable"
    return VlaCapabilities(
        robot_id=robot_id,
        tier=tier,
        available=not why,
        reason=why,
        backend=robot_backend,
        step_kinds=list(step_kinds),
        model_id=model,
        policy_host=host or None,
        action_hz=30.0,
        chunk_size=30 if name == "perceptron" else 50 if name == "smolvla" else None,
        needs_grounding=False,
        cameras=["front", "wrist"],
        input_frames_blurred=True,
        cancel_latency_ms=100.0,
    )


# ---- sync wrapper for the Isaac worker ------------------------------------------------------------------------------
_T = TypeVar("_T")


class SyncPolicyBackend:
    """Runs an async PolicyBackend on a private event loop thread so synchronous code (the Isaac worker's policy
    thread) can call ``reset`` / ``predict`` / ``close``. The backend is built INSIDE that loop by ``factory``."""

    def __init__(self, factory: Callable[[], PolicyBackend], *, timeout_s: float = 30.0) -> None:
        self.timeout_s = timeout_s
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="policy-backend", daemon=True)
        self._thread.start()

        async def _build() -> PolicyBackend:
            return factory()

        self.backend: PolicyBackend = self._run(_build())

    def _run(self, coro: Coroutine[Any, Any, _T], timeout: float | None = None) -> _T:
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return fut.result(timeout or self.timeout_s)
        except concurrent.futures.TimeoutError as exc:
            fut.cancel()
            raise PolicyBackendError("policy_timeout", "policy backend call timed out") from exc

    @property
    def info(self) -> BackendInfo:
        return self.backend.info

    def reset(self, instruction: str) -> None:
        self._run(self.backend.reset(instruction))

    def predict(self, state_deg: Sequence[float], side_jpeg: bytes, wrist_jpeg: bytes, **kw: Any) -> ActionChunk:
        return self._run(self.backend.predict(state_deg, side_jpeg, wrist_jpeg, **kw))

    def close(self) -> None:
        try:
            self._run(self.backend.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5.0)


__all__ = [
    "CAMERA_MAP",
    "IDENTITY_UNITS",
    "REST_POSE_USD_DEG",
    "USD_JOINT_LIMITS_DEG",
    "ActionChunk",
    "BackendInfo",
    "BackendName",
    "PerceptronBackend",
    "PolicyBackend",
    "PolicyBackendError",
    "ScriptedBackend",
    "SmolVLABackend",
    "So101UnitMap",
    "SyncPolicyBackend",
    "backend_name_from_settings",
    "check_rows",
    "create_policy_backend",
    "policy_capability",
    "prepare_jpeg",
]
