"""All LLM calls go through here: one place for the provider, model ids, retries, JSON and tool helpers.

Providers (``PICKY_LLM_PROVIDER``):

- ``gemini`` (default): Google Gemini through ``google-genai`` 2.31 (the ``agent`` extra), key ``GEMINI_API_KEY``.
- ``anthropic`` (optional): Claude through ``anthropic`` 1.15 (base dep), key ``ANTHROPIC_API_KEY``.

Callers speak ONE request/response shape whatever the provider: Anthropic Messages. Requests are
``messages=[{"role", "content"}]`` with ``text`` / ``image`` / ``tool_use`` / ``tool_result`` blocks and tools as
``{name, description, input_schema}``; responses have ``.content`` blocks (``text`` / ``tool_use``) and
``.stop_reason``. For Gemini, :class:`GeminiMessages` translates both ways (function-call ids and thought signatures
are echoed back on the next request), so ``get_client().messages.create(...)`` works the same on both providers and
arms never import a provider SDK.

- ``provider()``, ``model("brain"|"fast"|"smart")`` / ``MODEL_BRAIN``, ``MODEL_FAST``, ``MODEL_SMART``: from settings.
  Blank ``PICKY_MODEL_*`` = the provider's default in :data:`DEFAULT_MODELS`.
- ``is_configured()`` / ``not_configured_reason()``. ``get_client()`` raises :class:`LLMNotConfigured` when the key
  or the SDK is missing, so callers fall back to their deterministic path.
- ``complete_json(prompt, Model)``: structured output (JSON schema) validated by pydantic, one repair retry with the
  validation error fed back; falls back to tool forcing if the API rejects the schema.
- ``run_tool_loop(messages, tools, handlers)``: generic async tool-use loop (<= ``max_iters`` model calls).
- Prompt caching: ``cache_control`` goes on the system prompt and the last tool (Anthropic). Gemini caches
  implicitly; the adapter drops the markers.

Every helper takes an optional ``client=`` (and there is ``set_client``) so tests inject a fake and never touch the
network. A raw ``google.genai.Client`` passed there is wrapped in :class:`GeminiMessages` automatically.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import importlib.util
import inspect
import json
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError

from utils.config import get_settings
from utils.log import get_logger

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)
ModelKind = Literal["brain", "fast", "smart"]
Provider = Literal["gemini", "anthropic"]
Effort = Literal["low", "medium", "high"]

PROVIDERS: tuple[Provider, ...] = ("gemini", "anthropic")
DEFAULT_PROVIDER: Provider = "gemini"
DEFAULT_MODELS: dict[Provider, dict[ModelKind, str]] = {
    # ai.google.dev model list, 2026-10-17: 3.8 Flash = stable agent/tool-use model, 4 Argon = flagship reasoning.
    "gemini": {"brain": "gemini-3.8-flash", "fast": "gemini-3.8-flash", "smart": "gemini-4-argon"},
    "anthropic": {"brain": "claude-sonnet-5-5", "fast": "claude-haiku-5-5", "smart": "claude-opus-5-5"},
}
API_KEY_ENV: dict[Provider, str] = {"gemini": "GEMINI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
_PROVIDER_ALIASES: dict[str, Provider] = {"google": "gemini", "claude": "anthropic"}

CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}
JSON_TOOL_NAME = "emit_result"
RETRY_ATTEMPTS = 3
"""Attempts per request, both SDKs (429 / 5xx / timeouts)."""

GEMINI_THINKING_LEVELS = ("minimal", "low", "medium", "high")
GEMINI_THINKING_HEADROOM = 8192
"""Gemini counts thinking tokens against ``max_output_tokens``. Callers pass ``max_tokens`` sized for the visible
answer, so the adapter adds this much room for thoughts (it is only a cap: unused room costs nothing)."""
GEMINI_MAX_OUTPUT_TOKENS = 65536
"""Synchronous output cap on the Gemini API (a larger max_output_tokens is a 400)."""
GEMINI_SKIP_SIGNATURE = b"skip_thought_signature_validator"
"""Documented dummy thought signature for a function call the API did not generate (e.g. history built by hand).
Gemini 3+ rejects an unsigned first function call of a step in the current turn with a 400."""
GEMINI_LOCAL_ID_PREFIX = "gcall_"
"""Prefix of tool_use ids made up locally when Gemini returns a function call without an id. They are not sent
back, so Gemini matches those calls and responses by name and order."""

ToolHandler = Callable[[dict[str, Any]], Any] | Callable[[dict[str, Any]], Awaitable[Any]]


class LLMNotConfigured(RuntimeError):
    """Key or SDK missing for the configured provider: callers should fall back to their deterministic/fake path."""


class LLMOutputError(RuntimeError):
    """The model failed to produce valid output after retries."""


class BadRequestError(RuntimeError):
    """A provider rejected the request as invalid (HTTP 400). Raised by :class:`GeminiMessages`; carries the same
    class name as ``anthropic.BadRequestError`` so ``complete_json``'s tool-forcing fallback treats both alike."""

    status_code = 400


# --- provider + models -------------------------------------------------------------------------------------------

_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args: Any) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg, *args)


def provider() -> Provider:
    """``PICKY_LLM_PROVIDER`` (``gemini`` default | ``anthropic``; ``google`` / ``claude`` accepted as aliases).
    An unknown value logs a warning once and means ``gemini``."""
    raw = (get_settings().picky_llm_provider or "").strip().lower() or DEFAULT_PROVIDER
    p = _PROVIDER_ALIASES.get(raw, raw)
    if p in PROVIDERS:
        return p  # type: ignore[return-value]
    _warn_once(f"provider:{raw}", "PICKY_LLM_PROVIDER=%r is not one of %s; using %s", raw, PROVIDERS, DEFAULT_PROVIDER)
    return DEFAULT_PROVIDER


def _fits_provider(model_id: str, p: Provider) -> bool:
    mid = model_id.lower().removeprefix("models/")
    return not mid.startswith("claude") if p == "gemini" else not mid.startswith("gemini")


def model(kind: ModelKind = "brain") -> str:
    """Model id for ``kind``: ``PICKY_MODEL_BRAIN`` / ``_FAST`` / ``_SMART`` when set, else the provider default.

    A configured id that obviously belongs to the other provider (a stale ``claude-*`` with ``gemini``, or the
    reverse) is ignored with a warning, so an old ``.env`` cannot send a Claude model id to Gemini."""
    s = get_settings()
    configured = {"brain": s.picky_model_brain, "fast": s.picky_model_fast, "smart": s.picky_model_smart}[kind]
    p = provider()
    configured = (configured or "").strip()
    default = DEFAULT_MODELS[p][kind]
    if configured and _fits_provider(configured, p):
        return configured
    if configured:
        _warn_once(
            f"model:{kind}:{configured}:{p}",
            "PICKY_MODEL_%s=%s is not a %s model; using %s",
            kind.upper(),
            configured,
            p,
            default,
        )
    return default


def __getattr__(name: str) -> Any:
    # MODEL_BRAIN / MODEL_FAST / MODEL_SMART read env lazily (and honour set_settings in tests).
    if name in {"MODEL_BRAIN", "MODEL_FAST", "MODEL_SMART"}:
        return model(name.removeprefix("MODEL_").lower())  # type: ignore[arg-type]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _api_key(p: Provider) -> str:
    s = get_settings()
    key = s.gemini_api_key if p == "gemini" else s.anthropic_api_key
    return (key or "").strip()


def _sdk_available(p: Provider) -> bool:
    name = "google.genai" if p == "gemini" else "anthropic"
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):  # parent package missing / broken
        return False


def not_configured_reason() -> str | None:
    """Why the configured provider cannot be used (missing key or SDK), or ``None`` when it can. For /health."""
    p = provider()
    if not _api_key(p):
        return f"{API_KEY_ENV[p]} is not set (PICKY_LLM_PROVIDER={p}; add it to .env)"
    if not _sdk_available(p):
        hint = "uv sync --extra agent" if p == "gemini" else "uv sync"
        return f"the {p} SDK is not installed ({hint})"
    return None


def is_configured() -> bool:
    """True when the configured provider has its key and SDK (callers then take the real LLM path)."""
    return not_configured_reason() is None


# --- client ------------------------------------------------------------------------------------------------------

_client: Any = None
_client_key: tuple[str, str, float] | None = None
"""(provider, api key, timeout) the cached real client was built for; ``None`` for an injected client."""


def _wanted_key() -> tuple[str, str, float]:
    p = provider()
    return (p, _api_key(p), float(get_settings().picky_llm_timeout_s))


def get_client() -> Any:
    """Shared Anthropic-Messages-shaped client for the configured provider (created on first use).

    gemini -> :class:`GeminiMessages` over ``google.genai.Client``; anthropic -> ``anthropic.AsyncAnthropic``. Both:
    ``PICKY_LLM_TIMEOUT_S`` per request, :data:`RETRY_ATTEMPTS` attempts. A client injected with :func:`set_client`
    is always returned as is."""
    global _client, _client_key
    if _client is not None and (_client_key is None or _client_key == _wanted_key()):
        return _client
    reason = not_configured_reason()
    if reason:
        raise LLMNotConfigured(f"{reason}; use the arm's fake/fallback path")
    p, key, timeout_s = _wanted_key()
    if p == "gemini":
        from google import genai

        raw = genai.Client(
            api_key=key,
            http_options={
                "timeout": int(timeout_s * 1000),  # milliseconds
                "retry_options": {"attempts": RETRY_ATTEMPTS, "max_delay": 8.0},
            },
        )
        client: Any = GeminiMessages(raw)
    else:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=key, max_retries=RETRY_ATTEMPTS - 1, timeout=timeout_s)
    _client, _client_key = client, (p, key, timeout_s)
    return client


def set_client(client: Any) -> None:
    """Inject a client (tests / custom timeouts): anything with ``.messages.create`` (Anthropic-shaped) or a raw
    ``google.genai.Client``. ``None`` resets to lazy creation."""
    global _client, _client_key
    _client = as_messages_client(client) if client is not None else None
    _client_key = None


def as_messages_client(client: Any) -> Any:
    """``client`` as an Anthropic-Messages-shaped client: a raw ``google.genai.Client`` (has ``.aio.models``) gets
    wrapped in :class:`GeminiMessages`; anything else passes through."""
    if client is None or hasattr(client, "messages"):
        return client
    aio = getattr(client, "aio", None)
    if aio is not None and hasattr(aio, "models"):
        return GeminiMessages(client)
    return client


def client_provider(client: Any) -> Provider:
    """Provider a client talks to: its ``provider`` attribute (:class:`GeminiMessages` says ``gemini``), else the
    configured one (``AsyncAnthropic`` and test fakes)."""
    p = getattr(client, "provider", None)
    if p in PROVIDERS:
        return p  # type: ignore[return-value]
    if type(client).__module__.startswith("anthropic"):
        return "anthropic"
    return provider()


# --- pure helpers ------------------------------------------------------------------------------------------------


def cached_system(system: str | Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]] | None:
    """System prompt as text blocks with ``cache_control`` on the last block."""
    if not system:
        return None
    blocks = [{"type": "text", "text": system}] if isinstance(system, str) else [dict(b) for b in system]
    blocks[-1]["cache_control"] = CACHE_CONTROL
    return blocks


def cached_tools(tools: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Copy of ``tools`` with ``cache_control`` on the last one (caches the whole tools prefix)."""
    if not tools:
        return None
    out = [dict(t) for t in tools]
    out[-1]["cache_control"] = CACHE_CONTROL
    return out


def tool_from_model(name: str, description: str, input_model: type[BaseModel]) -> dict[str, Any]:
    """Tool definition ``{name, description, input_schema}`` from a pydantic model (works for both providers)."""
    return {"name": name, "description": description, "input_schema": input_model.model_json_schema()}


def json_schema_for(schema_model: type[BaseModel], p: Provider | None = None) -> dict[str, Any]:
    """JSON schema sent as the structured-output format: Anthropic's strict-mode transform for ``anthropic``, the
    plain pydantic schema for ``gemini`` (it takes ``$defs`` / ``$ref`` / ``anyOf`` and ignores unknown keys)."""
    if (p or provider()) == "anthropic":
        from anthropic import transform_schema

        return transform_schema(schema_model)
    return schema_model.model_json_schema()


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse JSON from model text, tolerating ```json fences and prose around the object/array."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = _FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except json.JSONDecodeError:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch in "{[":
            try:
                return dec.raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON found in model output: {text[:200]!r}")


def response_text(response: Any) -> str:
    """Concatenated text blocks of a Message."""
    return "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", None) == "text")


def tool_uses(response: Any) -> list[Any]:
    return [b for b in response.content if getattr(b, "type", None) == "tool_use"]


def block_to_param(block: Any) -> Any:
    """SDK content block -> plain dict for re-sending in ``messages`` (dicts pass through). Gemini blocks keep their
    ``thought_signature`` so the adapter can echo it back."""
    if hasattr(block, "model_dump"):
        return block.model_dump(mode="json", exclude_none=True)
    return block


def serialize_tool_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, BaseModel):
        return result.model_dump_json()
    if isinstance(result, list) and result and all(isinstance(r, BaseModel) for r in result):
        return json.dumps([r.model_dump(mode="json") for r in result])
    return json.dumps(result, default=str)


def _strip_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


# --- Gemini adapter: Anthropic Messages <-> google-genai generate_content ----------------------------------------


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str
    thought_signature: str | None = None
    """Gemini only: base64 of the part's opaque signature, sent back unchanged on the next request."""


class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any] = Field(default_factory=dict)
    thought_signature: str | None = None
    """Gemini only: base64 of the function call's signature (mandatory to echo for Gemini 3+)."""


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    """Visible output plus thinking tokens (what Gemini bills as output)."""


class LLMResponse(BaseModel):
    """Anthropic-``Message``-shaped response built from a Gemini ``GenerateContentResponse``."""

    id: str | None = None
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    model: str = ""
    provider: Provider = "gemini"
    content: list[TextBlock | ToolUseBlock] = Field(default_factory=list)
    stop_reason: str | None = None
    """end_turn | tool_use | max_tokens | refusal | malformed_tool_call (Gemini MALFORMED_FUNCTION_CALL & co)."""
    finish_reason: str | None = None
    """Gemini's raw finish reason (e.g. STOP, MAX_TOKENS, SAFETY)."""
    usage: Usage = Field(default_factory=Usage)


_REFUSAL_FINISH = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "LANGUAGE",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_RECITATION",
}
_MALFORMED_TOOL_FINISH = {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL", "TOO_MANY_TOOL_CALLS"}


def _as_dict(obj: Any) -> dict[str, Any]:
    if isinstance(obj, Mapping):
        return dict(obj)
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    return dict(vars(obj))


def _b64decode(s: str | bytes | None) -> bytes | None:
    if s is None or isinstance(s, bytes):
        return s
    try:
        return base64.b64decode(s, validate=True)
    except (binascii.Error, ValueError):
        return s.encode()


def _b64encode(b: bytes | str | None) -> str | None:
    if b is None or b == b"":
        return None
    return b if isinstance(b, str) else base64.b64encode(b).decode("ascii")


def _system_text(system: str | Sequence[Any] | None) -> str | None:
    if not system:
        return None
    if isinstance(system, str):
        return system
    return "\n\n".join(str(_as_dict(b).get("text", "")) for b in system if _as_dict(b).get("text")) or None


def _media_part(source: Mapping[str, Any]) -> dict[str, Any] | None:
    kind = source.get("type")
    if kind == "base64":
        return {
            "inline_data": {"mime_type": source.get("media_type", "image/jpeg"), "data": _b64decode(source["data"])}
        }
    if kind == "url":
        return {"file_data": {"file_uri": source["url"], "mime_type": source.get("media_type", "image/jpeg")}}
    return None


def _tool_result_part(block: Mapping[str, Any], names: Mapping[str, str]) -> dict[str, Any]:
    use_id = str(block.get("tool_use_id", ""))
    raw = block.get("content", "")
    media: list[dict[str, Any]] = []
    if isinstance(raw, str):
        text = raw
    else:
        texts = []
        for b in raw or []:
            b = _as_dict(b)
            if b.get("type") == "text":
                texts.append(str(b.get("text", "")))
            elif b.get("type") in ("image", "document") and isinstance(b.get("source"), Mapping):
                part = _media_part(b["source"])
                if part:
                    media.append(part)
        text = "\n".join(texts)
    value: Any = text
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict | list):
            value = parsed
    except (json.JSONDecodeError, TypeError):
        pass
    fr: dict[str, Any] = {
        "name": names.get(use_id) or str(block.get("name") or "unknown_tool"),
        "response": {"error" if block.get("is_error") else "output": value},
    }
    if use_id and not use_id.startswith(GEMINI_LOCAL_ID_PREFIX):
        fr["id"] = use_id
    if media:
        fr["parts"] = media
    return {"function_response": fr}


def to_gemini_contents(messages: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Anthropic ``messages`` -> Gemini ``contents`` (plain dicts the SDK validates).

    text -> ``text``; image/document (base64 or url) -> ``inline_data`` / ``file_data``; ``tool_use`` ->
    ``function_call`` (id + thought signature echoed; the first call of a step without one gets
    :data:`GEMINI_SKIP_SIGNATURE`); ``tool_result`` -> ``function_response`` (name looked up from the call, ``is_error``
    -> ``{"error": ...}`` else ``{"output": ...}``, JSON results passed as objects). Thinking blocks and
    ``cache_control`` markers are dropped."""
    names: dict[str, str] = {}
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                b = _as_dict(b)
                if b.get("type") == "tool_use":
                    names[str(b.get("id"))] = str(b.get("name"))

    contents: list[dict[str, Any]] = []
    for m in messages:
        m = _as_dict(m)
        role = "model" if m.get("role") == "assistant" else "user"
        content = m.get("content")
        blocks = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        parts: list[dict[str, Any]] = []
        first_call = True
        for b in blocks:
            b = _as_dict(b)
            kind = b.get("type")
            sig = _b64decode(b.get("thought_signature"))
            if kind == "text":
                if not b.get("text") and not sig:
                    continue
                part: dict[str, Any] = {"text": b.get("text", "")}
            elif kind in ("image", "document") and isinstance(b.get("source"), Mapping):
                media = _media_part(b["source"])
                if media is None:
                    log.warning("gemini: unsupported %s source %r dropped", kind, b["source"].get("type"))
                    continue
                part = media
            elif kind == "tool_use":
                call: dict[str, Any] = {"name": b.get("name"), "args": dict(b.get("input") or {})}
                use_id = str(b.get("id") or "")
                if use_id and not use_id.startswith(GEMINI_LOCAL_ID_PREFIX):
                    call["id"] = use_id
                part = {"function_call": call}
                if sig is None and first_call:
                    sig = GEMINI_SKIP_SIGNATURE
                first_call = False
            elif kind == "tool_result":
                part = _tool_result_part(b, names)
            elif kind in ("thinking", "redacted_thinking"):
                continue
            else:
                log.warning("gemini: content block type %r not supported; dropped", kind)
                continue
            if sig:
                part["thought_signature"] = sig
            parts.append(part)
        if parts:
            contents.append({"role": role, "parts": parts})
    return contents


def to_gemini_tools(tools: Sequence[Any] | None) -> list[dict[str, Any]] | None:
    """Anthropic tool definitions -> one Gemini ``Tool`` with ``function_declarations`` (``input_schema`` goes in
    as ``parameters_json_schema`` untouched; ``strict`` / ``cache_control`` dropped; server tools skipped)."""
    decls: list[dict[str, Any]] = []
    for t in tools or []:
        t = _as_dict(t)
        if not t.get("name") or t.get("type") not in (None, "custom"):
            continue
        d: dict[str, Any] = {"name": t["name"]}
        if t.get("description"):
            d["description"] = t["description"]
        if t.get("input_schema"):
            d["parameters_json_schema"] = t["input_schema"]
        decls.append(d)
    return [{"function_declarations": decls}] if decls else None


def to_gemini_tool_config(tool_choice: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Anthropic ``tool_choice`` -> Gemini ``tool_config``: auto -> AUTO, any -> ANY, tool -> ANY + that name,
    none -> NONE."""
    if not tool_choice:
        return None
    kind = tool_choice.get("type")
    if kind == "tool":
        return {"function_calling_config": {"mode": "ANY", "allowed_function_names": [tool_choice["name"]]}}
    mode = {"auto": "AUTO", "any": "ANY", "none": "NONE"}.get(str(kind))
    return {"function_calling_config": {"mode": mode}} if mode else None


def _thinking_level(effort: str | None) -> str | None:
    level = (effort or get_settings().picky_gemini_thinking or "").strip().lower()
    level = {"xhigh": "high", "max": "high"}.get(level, level)
    if not level:
        return None
    if level not in GEMINI_THINKING_LEVELS:
        _warn_once(
            f"thinking:{level}",
            "Gemini thinking level %r unknown (use %s); model default",
            level,
            "|".join(GEMINI_THINKING_LEVELS),
        )
        return None
    return level.upper()


def to_gemini_request(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    max_tokens: int | None = None,
    system: str | Sequence[Any] | None = None,
    tools: Sequence[Any] | None = None,
    tool_choice: Mapping[str, Any] | None = None,
    output_config: Mapping[str, Any] | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    top_k: int | None = None,
    stop_sequences: Sequence[str] | None = None,
    **ignored: Any,
) -> dict[str, Any]:
    """Anthropic ``messages.create`` kwargs -> ``generate_content(model=, contents=, config=)`` kwargs.

    ``max_tokens`` gets :data:`GEMINI_THINKING_HEADROOM` added (thinking counts against the cap);
    ``output_config.format`` json_schema -> ``response_mime_type`` JSON + ``response_json_schema``;
    ``output_config.effort`` (or ``PICKY_GEMINI_THINKING``) -> ``thinking_config.thinking_level``.
    Anthropic-only kwargs (``metadata``, ``thinking``, ``extra_headers`` ...) are ignored."""
    if ignored:
        log.debug("gemini: ignoring Anthropic-only kwargs %s", sorted(ignored))
    config: dict[str, Any] = {}
    sys_text = _system_text(system)
    if sys_text:
        config["system_instruction"] = sys_text
    if max_tokens:
        config["max_output_tokens"] = min(int(max_tokens) + GEMINI_THINKING_HEADROOM, GEMINI_MAX_OUTPUT_TOKENS)
    for key, value in (("temperature", temperature), ("top_p", top_p), ("top_k", top_k)):
        if value is not None:
            config[key] = value
    if stop_sequences:
        config["stop_sequences"] = list(stop_sequences)
    gem_tools = to_gemini_tools(tools)
    if gem_tools:
        config["tools"] = gem_tools
        config["automatic_function_calling"] = {"disable": True}
        tool_config = to_gemini_tool_config(tool_choice)
        if tool_config:
            config["tool_config"] = tool_config
    out = dict(output_config or {})
    fmt = out.get("format") or {}
    if fmt.get("type") == "json_schema":
        config["response_mime_type"] = "application/json"
        if fmt.get("schema"):
            config["response_json_schema"] = fmt["schema"]
    level = _thinking_level(out.get("effort"))
    if level:
        config["thinking_config"] = {"thinking_level": level}
    return {"model": model, "contents": to_gemini_contents(messages), "config": config}


def _enum_name(value: Any) -> str | None:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    return str(getattr(value, "name", None) or raw).upper()


def from_gemini_response(raw: Any, *, model: str = "") -> LLMResponse:
    """Gemini ``GenerateContentResponse`` (or any object of that shape) -> :class:`LLMResponse`.

    Text parts -> ``text`` blocks, function calls -> ``tool_use`` blocks (Gemini's id, or a local
    ``gcall_...`` one when absent), thought summaries skipped, signatures kept base64. ``stop_reason``: any function
    call -> tool_use; MAX_TOKENS -> max_tokens; safety-type finishes or no candidate -> refusal; malformed function
    call -> malformed_tool_call; else end_turn."""
    candidates = list(getattr(raw, "candidates", None) or [])
    usage_raw = getattr(raw, "usage_metadata", None)
    usage = Usage(
        input_tokens=int(getattr(usage_raw, "prompt_token_count", 0) or 0),
        output_tokens=int(getattr(usage_raw, "candidates_token_count", 0) or 0)
        + int(getattr(usage_raw, "thoughts_token_count", 0) or 0),
    )
    rid = getattr(raw, "response_id", None)
    mid = getattr(raw, "model_version", None) or model
    if not candidates:
        feedback = getattr(raw, "prompt_feedback", None)
        reason = _enum_name(getattr(feedback, "block_reason", None)) or "NO_CANDIDATES"
        return LLMResponse(id=rid, model=mid, stop_reason="refusal", finish_reason=reason, usage=usage)

    cand = candidates[0]
    content = getattr(cand, "content", None)
    blocks: list[TextBlock | ToolUseBlock] = []
    for part in list(getattr(content, "parts", None) or []):
        sig = _b64encode(getattr(part, "thought_signature", None))
        fc = getattr(part, "function_call", None)
        if fc is not None:
            args = getattr(fc, "args", None) or {}
            blocks.append(
                ToolUseBlock(
                    id=getattr(fc, "id", None) or f"{GEMINI_LOCAL_ID_PREFIX}{uuid.uuid4().hex[:12]}",
                    name=str(getattr(fc, "name", "") or ""),
                    input=dict(args),
                    thought_signature=sig,
                )
            )
            continue
        if getattr(part, "thought", None):
            continue  # thought summary (include_thoughts): not part of the answer
        text = getattr(part, "text", None)
        if text:
            blocks.append(TextBlock(text=text, thought_signature=sig))

    finish = _enum_name(getattr(cand, "finish_reason", None))
    if any(isinstance(b, ToolUseBlock) for b in blocks):
        stop = "tool_use"
    elif finish == "MAX_TOKENS":
        stop = "max_tokens"
    elif finish in _REFUSAL_FINISH:
        stop = "refusal"
    elif finish in _MALFORMED_TOOL_FINISH:
        stop = "malformed_tool_call"
    else:
        stop = "end_turn"
    return LLMResponse(id=rid, model=mid, content=blocks, stop_reason=stop, finish_reason=finish, usage=usage)


def _is_bad_request(exc: Exception) -> bool:
    return getattr(exc, "code", None) == 400 or getattr(exc, "status_code", None) == 400


# Tried in order when the requested Gemini model is overloaded (503), unknown/retired (404) or out of quota (429:
# Gemini API rate limits are per model, so a sibling model usually still answers; measured on vhagar 2026-10-18, the
# planner's first call hit 429 RESOURCE_EXHAUSTED on gemini-3.8-flash while the request interpreter had just used it).
GEMINI_FALLBACK_MODELS: tuple[str, ...] = ("gemini-3.5-flash", "gemini-3.7-flash", "gemini-flash-latest")
_GEMINI_FALLBACK_CODES = frozenset({404, 429, 500, 503})


def _status_code(exc: Exception) -> int | None:
    for attr in ("code", "status_code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    return None


class GeminiMessages:
    """Anthropic-Messages-shaped facade over a ``google.genai.Client``: ``await c.messages.create(**kwargs)``
    returns an :class:`LLMResponse`. Translation: :func:`to_gemini_request` / :func:`from_gemini_response`.
    A 400 from the API is re-raised as :class:`BadRequestError` (original as ``__cause__``); a 503/404/500 on the
    requested model retries the call on :data:`GEMINI_FALLBACK_MODELS` before raising (429 too)."""

    provider: Provider = "gemini"

    def __init__(self, genai_client: Any) -> None:
        self.genai = genai_client

    @property
    def messages(self) -> GeminiMessages:
        return self

    async def create(self, **kwargs: Any) -> LLMResponse:
        request = to_gemini_request(**kwargs)
        models = [request["model"], *(m for m in GEMINI_FALLBACK_MODELS if m != request["model"])]
        for i, model_id in enumerate(models):
            try:
                raw = await self.genai.aio.models.generate_content(**{**request, "model": model_id})
            except Exception as e:
                if _is_bad_request(e):
                    raise BadRequestError(f"gemini rejected the request: {e}") from e
                if _status_code(e) in _GEMINI_FALLBACK_CODES and i + 1 < len(models):
                    log.warning("gemini %s unavailable (%s); retrying on %s", model_id, _status_code(e), models[i + 1])
                    continue
                raise
            return from_gemini_response(raw, model=model_id)
        raise AssertionError("unreachable")  # the last model either returns or raises


# --- complete_json -----------------------------------------------------------------------------------------------


async def complete_json(
    prompt: str | list[dict[str, Any]],
    schema_model: type[T],
    *,
    system: str | None = None,
    model_id: str | None = None,
    kind: ModelKind = "brain",
    max_tokens: int = 2048,
    mode: Literal["structured", "tool"] = "structured",
    retries: int = 1,
    effort: Effort | None = None,
    client: Any = None,
) -> T:
    """Ask for JSON matching ``schema_model`` and return a validated instance.

    ``prompt`` is a user string or a full ``messages`` list (Anthropic shape, images allowed). ``effort`` (optional)
    -> Anthropic ``output_config.effort`` / Gemini ``thinking_level``. Raises :class:`LLMOutputError` after
    ``retries`` re-asks, :class:`LLMNotConfigured` without a key/SDK.
    """
    client = as_messages_client(client) or get_client()
    prov = client_provider(client)
    messages: list[dict[str, Any]] = (
        [{"role": "user", "content": prompt}] if isinstance(prompt, str) else [dict(m) for m in prompt]
    )
    mid = model_id or model(kind)
    last_err: Exception | None = None

    for attempt in range(retries + 1):
        kwargs: dict[str, Any] = _strip_none(
            {"model": mid, "max_tokens": max_tokens, "messages": messages, "system": cached_system(system)}
        )
        output_config: dict[str, Any] = {"effort": effort} if effort else {}
        if mode == "structured":
            output_config["format"] = {"type": "json_schema", "schema": json_schema_for(schema_model, prov)}
        else:
            kwargs["tools"] = cached_tools(
                [tool_from_model(JSON_TOOL_NAME, f"Return the {schema_model.__name__} result.", schema_model)]
            )
            kwargs["tool_choice"] = {"type": "tool", "name": JSON_TOOL_NAME}
        if output_config:
            kwargs["output_config"] = output_config

        try:
            response = await client.messages.create(**kwargs)
        except Exception as e:  # schema rejected by structured outputs -> tool forcing
            if mode == "structured" and type(e).__name__ == "BadRequestError":
                log.warning("structured output rejected (%s); falling back to tool forcing", e)
                return await complete_json(
                    messages,
                    schema_model,
                    system=system,
                    model_id=mid,
                    max_tokens=max_tokens,
                    mode="tool",
                    retries=retries,
                    effort=effort,
                    client=client,
                )
            raise

        raw: Any = None
        try:
            if mode == "tool":
                uses = tool_uses(response)
                if not uses:
                    raise ValueError("model did not call the result tool")
                raw = uses[0].input
            else:
                raw = extract_json(response_text(response))
            return schema_model.model_validate(raw)
        except (ValueError, ValidationError) as e:
            last_err = e
            log.warning("complete_json attempt %d invalid for %s: %s", attempt + 1, schema_model.__name__, e)
            assistant_content = [block_to_param(b) for b in response.content] or [{"type": "text", "text": "{}"}]
            messages = [*messages, {"role": "assistant", "content": assistant_content}]
            feedback = f"That output was invalid: {e}. Reply again with ONLY valid JSON for {schema_model.__name__}."
            if mode == "tool" and tool_uses(response):
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tool_uses(response)[0].id,
                                "content": feedback,
                                "is_error": True,
                            }
                        ],
                    }
                )
            else:
                messages.append({"role": "user", "content": feedback})

    raise LLMOutputError(f"invalid {schema_model.__name__} after {retries + 1} attempts: {last_err}")


# --- tool loop ---------------------------------------------------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]
    output: str
    is_error: bool = False


@dataclass
class ToolLoopResult:
    text: str
    messages: list[dict[str, Any]]
    iterations: int
    stop_reason: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    hit_max_iters: bool = False


async def _call_handler(handler: ToolHandler, args: dict[str, Any]) -> Any:
    result = handler(args)
    if inspect.isawaitable(result):
        result = await result
    return result


async def run_tool_loop(
    messages: list[dict[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    handlers: Mapping[str, ToolHandler],
    *,
    system: str | None = None,
    model_id: str | None = None,
    kind: ModelKind = "brain",
    max_iters: int = 8,
    max_tokens: int = 2048,
    tool_choice: Mapping[str, Any] | None = None,
    on_tool_call: Callable[[ToolCall], Any] | None = None,
    parallel_tools: bool = True,
    effort: Effort | None = None,
    client: Any = None,
) -> ToolLoopResult:
    """Call the model, run requested tools via ``handlers[name](input)`` (sync or async), feed results back, repeat
    until the model stops asking for tools or ``max_iters`` model calls are made.

    Handler exceptions and unknown tools become ``is_error`` tool results (the model can recover); they never
    escape. ``on_tool_call`` (sync or async) fires after each tool runs, e.g. to publish an event. Results go back
    with the call's id (Gemini: ``function_response.id`` = ``function_call.id``, signatures echoed).
    """
    client = as_messages_client(client) or get_client()
    convo = [dict(m) for m in messages]
    calls: list[ToolCall] = []
    mid = model_id or model(kind)
    sys_blocks = cached_system(system)
    tool_defs = cached_tools(tools)
    response: Any = None

    for i in range(1, max_iters + 1):
        kwargs = _strip_none(
            {
                "model": mid,
                "max_tokens": max_tokens,
                "messages": convo,
                "system": sys_blocks,
                "tools": tool_defs,
                "tool_choice": dict(tool_choice) if (tool_choice and i == 1) else None,
                "output_config": {"effort": effort} if effort else None,
            }
        )
        response = await client.messages.create(**kwargs)
        convo.append({"role": "assistant", "content": [block_to_param(b) for b in response.content]})
        uses = tool_uses(response)
        if response.stop_reason != "tool_use" or not uses:
            return ToolLoopResult(response_text(response), convo, i, response.stop_reason, calls)

        async def run_one(use: Any) -> ToolCall:
            args = dict(use.input or {})
            handler = handlers.get(use.name)
            if handler is None:
                return ToolCall(use.id, use.name, args, f"unknown tool: {use.name}", True)
            try:
                return ToolCall(use.id, use.name, args, serialize_tool_result(await _call_handler(handler, args)))
            except Exception as e:  # surfaced to the model, logged for us
                log.warning("tool %s failed: %s", use.name, e)
                return ToolCall(use.id, use.name, args, f"{type(e).__name__}: {e}", True)

        if parallel_tools:
            results = list(await asyncio.gather(*(run_one(u) for u in uses)))
        else:
            results = [await run_one(u) for u in uses]

        for call in results:
            calls.append(call)
            if on_tool_call is not None:
                r = on_tool_call(call)
                if inspect.isawaitable(r):
                    await r
        convo.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": c.id,
                        "content": c.output,
                        **({"is_error": True} if c.is_error else {}),
                    }
                    for c in results
                ],
            }
        )

    log.warning("tool loop hit max_iters=%d", max_iters)
    return ToolLoopResult(
        response_text(response) if response is not None else "",
        convo,
        max_iters,
        getattr(response, "stop_reason", None),
        calls,
        hit_max_iters=True,
    )
