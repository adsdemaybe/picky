"""``PerceptronVisionProvider``: THE real vision provider of the verifier (Perceptron Mk1.5 hosted API).

SDK ``perceptron`` 0.4.0 (perception extra), key ``PERCEPTRON_API_KEY``, base ``https://api.perceptron.inc/v1``, model
``perceptron-mk1.5`` (fallback ``perceptron-mk1``). Request shapes checked against the 0.4.0 wheel:

- ``AsyncClient(api_key=, base_url=, model=, provider="perceptron", timeout=)``; ONE client per event loop (its httpx
  pool belongs to the loop it runs on). The SDK does not retry and its default timeout is 125 s (multilook >= 305 s),
  so every call here runs inside ``asyncio.wait_for(min(PICKY_VISION_CALL_TIMEOUT_S, caller's timeout_s))``.
- ``client.chat.completions.create(model=, messages=, max_completion_tokens=, reasoning_effort=, vision_config=
  {"annotation_format": "box"|"point"}, timeout=)`` -> ``ChatCompletion``: ``.text``, ``.annotations()`` ->
  ``AnnotationCollection.boxes`` (``BoundingBox.top_left/bottom_right/mention/asset_idx``) / ``.points``
  (``SinglePoint.x/y/mention``), ``.request_id``, ``.resolve_asset_idx(a)``.
- ``client.chat.completions.multilook(model=, context=[message], prompts=[str], timeout=)`` -> ``MultilookResponse.
  results[i]``: ``prompt_index``, ``ok``, ``completions[0]`` (``.text`` / ``.annotations()``), ``error.code``.
- Message content is sent as wire parts (``{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,..."}}``
  and ``{"type": "text", ...}``), exactly what the SDK's ``image()`` node lowers to; detect / OCR use the SDK's own
  high-level prompt templates (system message "Your goal is to segment out the following categories: ..." / the OCR
  system instruction). Coordinates stay on Perceptron's 0-1000 grid (``NormBox`` / ``NormPoint``).

Errors never raise into the verifier: they come back as a ``VisionAnswer`` with ``call.error_code`` set
('unavailable' no key / SDK, 'timeout', 'rate_limit', 'insufficient_quota', 'auth', 'server_error', 'deadline', ...).
``RateLimitError.retry_after`` is honoured: one in-budget retry after the wait, and every call during the wait fails
fast with 'rate_limit' (so the verifier falls back to ground truth / robot state at once instead of hammering).
Frames with ``blurred=False`` are rejected with ``ValueError`` (CLAUDE.md section 8) before anything is sent.
"""

from __future__ import annotations

import asyncio
import importlib.util
import time
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from contracts.grounding import Evidence, ProviderCall
from contracts.sim import CameraFrame
from contracts.verify import DEFAULT_VISION_CALL_TIMEOUT_S, VisionAnswer, VisionExpect
from utils.log import get_logger

from .common import failed_answer, norm_box, norm_point, parse_yes_no, require_blurred

log = get_logger(__name__)

DEFAULT_MODEL = "perceptron-mk1.5"
FALLBACK_MODEL = "perceptron-mk1"
DEFAULT_BASE_URL = "https://api.perceptron.inc/v1"
DEFAULT_REASONING_EFFORT = "none"
"""Perception checks need no chain of thought; 'none' is the fast tier (~1 s answers)."""
DETECT_SYSTEM = "Your goal is to segment out the following categories: {categories}"
"""perceptron 0.4.0 high-level ``detect()`` template (prompting._ISAAC_PROFILE, also used for Mk1.5)."""
OCR_SYSTEM = (
    "You are an OCR (Optical Character Recognition) system. Accurately detect, extract, and transcribe all readable "
    "text from the image."
)
"""perceptron 0.4.0 high-level ``ocr()`` system instruction."""
YES_NO_SUFFIX = " Answer yes or no first, then give one short sentence of visual evidence."
MIN_CALL_S = 0.2
"""Below this budget a call is not started (answers 'deadline')."""
ANSWER_CONFIDENCE = 0.85
"""Perceptron returns no calibrated score: a parsed answer (box / point / yes-no / text) gets this, nothing parsed 0.4.
The verifier derives its own confidence from how many cues agree (verify.VISION_OVERRIDE_CONFIDENCE rule)."""

_CLASS_CODES = {
    "QuotaExceededError": "insufficient_quota",
    "RateLimitError": "rate_limit",
    "TimeoutError": "timeout",
    "IncompleteStreamError": "transport",
    "TransportError": "transport",
    "PermissionDeniedError": "auth",
    "AuthError": "auth",
    "ServerError": "server_error",
    "NotFoundError": "not_found",
    "BadRequestError": "bad_request",
    "ParseError": "parse_error",
}


def error_code_of(exc: BaseException) -> str:
    """Stable short code for a perceptron SDK error (``SDKError.code`` when set, else by class)."""
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code:
        return code
    for cls in type(exc).__mro__:
        if cls.__name__ in _CLASS_CODES:
            return _CLASS_CODES[cls.__name__]
    return type(exc).__name__


def _image_part(frame: CameraFrame) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{frame.jpeg_b64}"}}


def _text_part(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _short(prompt: str | None, n: int = 160) -> str | None:
    if prompt is None:
        return None
    return prompt if len(prompt) <= n else prompt[: n - 1] + "…"


class PerceptronVisionProvider:
    """``contracts.services.VisionProvider`` over the Perceptron API. ``client`` / ``client_factory`` inject a mock
    (tests never touch the network)."""

    name: str = "perceptron"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        timeout_s: float = DEFAULT_VISION_CALL_TIMEOUT_S,
        reasoning_effort: str | None = DEFAULT_REASONING_EFFORT,
        max_completion_tokens: int = 512,
        client: Any = None,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.api_key = api_key or ""
        self.model: str | None = model or DEFAULT_MODEL
        self.base_url = base_url or DEFAULT_BASE_URL
        self.timeout_s = timeout_s
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens
        self._fixed_client = client
        self._client_factory = client_factory
        self._clients: dict[int, tuple[asyncio.AbstractEventLoop, Any]] = {}
        self._cooldown_until = 0.0
        self.last_error: str | None = None

    # ---- availability --------------------------------------------------------------------------------------------
    @staticmethod
    def sdk_available() -> bool:
        return importlib.util.find_spec("perceptron") is not None

    def unavailable_reason(self) -> str | None:
        injected = self._fixed_client is not None or self._client_factory is not None
        if injected:
            return None
        if not self.api_key:
            return "PERCEPTRON_API_KEY not set"
        if not self.sdk_available():
            return "perceptron SDK not installed (uv sync --extra perception)"
        return None

    @property
    def available(self) -> bool:
        return self.unavailable_reason() is None

    # ---- plumbing ------------------------------------------------------------------------------------------------
    def _sdk_client(self) -> Any:
        from perceptron import AsyncClient  # optional extra: perception

        return AsyncClient(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            provider="perceptron",
            timeout=max(self.timeout_s, 1.0) + 5.0,
        )

    def _client(self) -> Any:
        if self._fixed_client is not None:
            return self._fixed_client
        loop = asyncio.get_running_loop()
        for key, (lp, _) in list(self._clients.items()):
            if lp.is_closed():
                del self._clients[key]
        entry = self._clients.get(id(loop))
        if entry is None or entry[0] is not loop:
            client = (self._client_factory or self._sdk_client)()
            self._clients[id(loop)] = (loop, client)
            return client
        return entry[1]

    def _budget(self, timeout_s: float | None) -> float:
        return min(self.timeout_s, timeout_s) if timeout_s is not None else self.timeout_s

    def _fail(self, call: str, prompt: str | None, code: str, t0: float) -> VisionAnswer:
        self.last_error = code
        return failed_answer("perceptron", self.model, call, _short(prompt), code, t0)

    async def _run(
        self,
        call: str,
        prompt: str | None,
        make: Callable[[Any, float], Awaitable[Any]],
        timeout_s: float | None,
    ) -> tuple[Any, VisionAnswer | None, float]:
        """Run one SDK call inside the budget -> (result, None, t0) or (None, failed answer, t0)."""
        t0 = time.monotonic()
        if self.unavailable_reason() is not None:
            return None, self._fail(call, prompt, "unavailable", t0), t0
        budget = self._budget(timeout_s)
        if budget < MIN_CALL_S:
            return None, self._fail(call, prompt, "deadline", t0), t0
        if self._cooldown_until > t0:
            return None, self._fail(call, prompt, "rate_limit", t0), t0
        attempt = 0
        while True:
            attempt += 1
            left = budget - (time.monotonic() - t0)
            if left < MIN_CALL_S:
                return None, self._fail(call, prompt, "deadline", t0), t0
            try:
                client = self._client()
                result = await asyncio.wait_for(make(client, left), timeout=left)
                self.last_error = None
                return result, None, t0
            except ImportError:
                return None, self._fail(call, prompt, "sdk_missing", t0), t0
            except TimeoutError:  # asyncio.wait_for (builtin); perceptron.TimeoutError is an SDKError (below)
                return None, self._fail(call, prompt, "timeout", t0), t0
            except Exception as exc:  # noqa: BLE001 - every SDK error degrades (never raises into the verifier)
                code = error_code_of(exc)
                retry_after = getattr(exc, "retry_after", None)
                if code == "rate_limit" and isinstance(retry_after, int | float) and retry_after >= 0:
                    self._cooldown_until = time.monotonic() + float(retry_after)
                    fits = (time.monotonic() - t0) + float(retry_after) + MIN_CALL_S <= budget
                    if attempt == 1 and fits:
                        log.info("perceptron %s: rate limited, retrying after %.2f s", call, retry_after)
                        await asyncio.sleep(float(retry_after))
                        continue
                log.warning("perceptron %s failed: %s (%s)", call, code, exc)
                return None, self._fail(call, prompt, code, t0), t0

    def _parse(
        self, completion: Any, call: str, prompt: str | None, expects: VisionExpect, t0: float, *, n_images: int = 1
    ) -> VisionAnswer:
        text = getattr(completion, "text", None) or ""
        if not isinstance(text, str):
            text = str(text)
        ann: Any = None
        annotations = getattr(completion, "annotations", None)
        if callable(annotations):
            try:
                ann = annotations()
            except Exception as exc:  # noqa: BLE001 - malformed markup: keep the text
                log.debug("perceptron %s: annotation parse failed (%s)", call, exc)
        resolve = getattr(completion, "resolve_asset_idx", None)

        def on_last_image(a: Any) -> bool:
            if n_images <= 1:
                return True
            try:
                idx = resolve(a) if callable(resolve) else getattr(a, "asset_idx", None)
            except Exception:  # noqa: BLE001
                idx = getattr(a, "asset_idx", None)
            return idx is None or idx == n_images - 1

        boxes: list[Evidence] = []
        for b in getattr(ann, "boxes", None) or []:
            if not on_last_image(b):
                continue
            try:
                box = norm_box(b.top_left.x, b.top_left.y, b.bottom_right.x, b.bottom_right.y)
            except Exception:  # noqa: BLE001 - skip one malformed box
                continue
            boxes.append(
                Evidence(kind="box", mention=getattr(b, "mention", None) or "", box=box, confidence=ANSWER_CONFIDENCE)
            )
        points: list[Evidence] = []
        for p in getattr(ann, "points", None) or []:
            if not on_last_image(p):
                continue
            try:
                pt = norm_point(p.x, p.y)
            except Exception:  # noqa: BLE001
                continue
            points.append(
                Evidence(
                    kind="point", mention=getattr(p, "mention", None) or "", point=pt, confidence=ANSWER_CONFIDENCE
                )
            )
        yes = parse_yes_no(text)
        parsed = bool(boxes or points or (yes is not None and expects in ("bool", "text")) or text.strip())
        return VisionAnswer(
            text=text,
            yes=yes,
            boxes=boxes,
            points=points,
            confidence=ANSWER_CONFIDENCE if parsed else 0.4,
            call=ProviderCall(
                provider="perceptron",
                model=getattr(completion, "model", None) or self.model,
                call=call,
                prompt=_short(prompt),
                request_id=getattr(completion, "request_id", None),
                latency_ms=(time.monotonic() - t0) * 1000,
            ),
        )

    async def _chat(
        self,
        call: str,
        prompt: str,
        messages: list[dict[str, Any]],
        expects: VisionExpect,
        timeout_s: float | None,
        *,
        n_images: int = 1,
    ) -> VisionAnswer:
        def make(client: Any, left: float) -> Awaitable[Any]:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "messages": messages,
                "max_completion_tokens": self.max_completion_tokens,
                "timeout": left,
            }
            if self.reasoning_effort:
                kwargs["reasoning_effort"] = self.reasoning_effort
            if expects in ("box", "point"):
                kwargs["vision_config"] = {"annotation_format": expects}
            return client.chat.completions.create(**kwargs)

        completion, failed, t0 = await self._run(call, prompt, make, timeout_s)
        if failed is not None:
            return failed
        return self._parse(completion, call, prompt, expects, t0, n_images=n_images)

    # ---- VisionProvider ------------------------------------------------------------------------------------------
    async def detect(
        self, frame: CameraFrame, classes: Sequence[str], *, timeout_s: float | None = None
    ) -> VisionAnswer:
        require_blurred(frame)
        cats = [c.replace(",", " ").strip() for c in classes if c and c.strip()]
        system = DETECT_SYSTEM.format(categories=", ".join(cats)) if cats else "Your goal is to segment out the objects"
        messages = [{"role": "system", "content": system}, {"role": "user", "content": [_image_part(frame)]}]
        return await self._chat("detect", "Detect: " + "; ".join(cats), messages, "box", timeout_s)

    async def point(self, frame: CameraFrame, query: str, *, timeout_s: float | None = None) -> VisionAnswer:
        require_blurred(frame)
        messages = [{"role": "user", "content": [_image_part(frame), _text_part(query)]}]
        return await self._chat("question:point", query, messages, "point", timeout_s)

    async def question(
        self, frame: CameraFrame, question: str, expects: VisionExpect = "text", *, timeout_s: float | None = None
    ) -> VisionAnswer:
        require_blurred(frame)
        asked = question + YES_NO_SUFFIX if expects == "bool" else question
        messages = [{"role": "user", "content": [_image_part(frame), _text_part(asked)]}]
        return await self._chat(f"question:{expects}", question, messages, expects, timeout_s)

    async def ocr(
        self, frame: CameraFrame, prompt: str | None = None, *, timeout_s: float | None = None
    ) -> VisionAnswer:
        require_blurred(frame)
        content: list[dict[str, Any]] = [_image_part(frame)]
        if prompt:
            content.append(_text_part(prompt))
        messages = [{"role": "system", "content": OCR_SYSTEM}, {"role": "user", "content": content}]
        return await self._chat("ocr", prompt or "Read all text", messages, "text", timeout_s)

    async def compare(
        self,
        before: CameraFrame,
        after: CameraFrame,
        question: str,
        expects: VisionExpect = "box",
        *,
        timeout_s: float | None = None,
    ) -> VisionAnswer:
        require_blurred(before)
        require_blurred(after)
        asked = question + YES_NO_SUFFIX if expects == "bool" else question
        messages = [
            {"role": "user", "content": [_image_part(before), _image_part(after), _text_part(asked)]},
        ]
        # Annotations without asset_idx refer to the LAST image (= after): only those are kept.
        return await self._chat("compare", question, messages, expects, timeout_s, n_images=2)

    async def multilook(
        self, frame: CameraFrame, prompts: Sequence[str], *, timeout_s: float | None = None
    ) -> list[VisionAnswer]:
        require_blurred(frame)
        plist = list(prompts)
        if not plist:
            return []

        def make(client: Any, left: float) -> Awaitable[Any]:
            kwargs: dict[str, Any] = {
                "model": self.model,
                "context": [{"role": "user", "content": [_image_part(frame)]}],
                "prompts": plist,
                "max_completion_tokens": self.max_completion_tokens,
                "timeout": left,
            }
            if self.reasoning_effort:
                kwargs["reasoning_effort"] = self.reasoning_effort
            return client.chat.completions.multilook(**kwargs)

        resp, failed, t0 = await self._run("multilook", plist[0], make, timeout_s)
        if failed is not None:
            code = failed.call.error_code or "error"
            return [self._fail("multilook", p, code, t0) for p in plist]
        out: list[VisionAnswer | None] = [None] * len(plist)
        for r in getattr(resp, "results", None) or []:
            idx = getattr(r, "prompt_index", None)
            if not isinstance(idx, int) or not 0 <= idx < len(plist):
                continue
            completions = getattr(r, "completions", None) or []
            if not getattr(r, "ok", False) or not completions:
                code = getattr(getattr(r, "error", None), "code", None) or "prompt_failed"
                out[idx] = self._fail("multilook", plist[idx], code, t0)
                continue
            out[idx] = self._parse(completions[0], "multilook", plist[idx], "text", t0)
        return [
            a if a is not None else self._fail("multilook", p, "missing_result", t0)
            for a, p in zip(out, plist, strict=True)
        ]


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "DETECT_SYSTEM",
    "FALLBACK_MODEL",
    "OCR_SYSTEM",
    "YES_NO_SUFFIX",
    "PerceptronVisionProvider",
    "error_code_of",
]
