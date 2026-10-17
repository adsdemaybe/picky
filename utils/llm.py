"""Thin wrapper over the Anthropic SDK (``anthropic`` 1.15, ``AsyncAnthropic``). All LLM calls go through here.

- ``get_client()``: lazy shared ``AsyncAnthropic``; raises :class:`LLMNotConfigured` if ``ANTHROPIC_API_KEY`` is unset.
- ``model("brain"|"fast"|"smart")`` / ``MODEL_BRAIN``, ``MODEL_FAST``, ``MODEL_SMART``: model ids from env.
- ``complete_json(prompt, Model)``: structured output (``output_config.format`` json_schema) validated by pydantic,
  one retry with the validation error fed back; falls back to tool-forcing if the API rejects the schema.
- ``run_tool_loop(messages, tools, handlers)``: generic async tool-use loop (<= ``max_iters`` model calls).
- Prompt caching: ``cache_control`` is put on the system prompt and on the last tool definition.

Every helper takes an optional ``client=`` so tests can inject a fake and never touch the network.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, ValidationError

from utils.config import get_settings
from utils.log import get_logger

log = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)
ModelKind = Literal["brain", "fast", "smart"]
CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}
JSON_TOOL_NAME = "emit_result"

ToolHandler = Callable[[dict[str, Any]], Any] | Callable[[dict[str, Any]], Awaitable[Any]]


class LLMNotConfigured(RuntimeError):
    """ANTHROPIC_API_KEY missing: callers should fall back to their deterministic/fake path."""


class LLMOutputError(RuntimeError):
    """The model failed to produce valid output after retries."""


# --- models ----------------------------------------------------------------------------------------------------


def model(kind: ModelKind = "brain") -> str:
    s = get_settings()
    return {"brain": s.picky_model_brain, "fast": s.picky_model_fast, "smart": s.picky_model_smart}[kind]


def __getattr__(name: str) -> Any:
    # MODEL_BRAIN / MODEL_FAST / MODEL_SMART read env lazily (and honour set_settings in tests).
    if name in {"MODEL_BRAIN", "MODEL_FAST", "MODEL_SMART"}:
        return model(name.removeprefix("MODEL_").lower())  # type: ignore[arg-type]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# --- client ----------------------------------------------------------------------------------------------------

_client: Any = None


def is_configured() -> bool:
    return bool(get_settings().anthropic_api_key.strip())


def get_client() -> Any:
    """Shared ``anthropic.AsyncAnthropic`` (created on first use)."""
    global _client
    if _client is None:
        key = get_settings().anthropic_api_key.strip()
        if not key:
            raise LLMNotConfigured("ANTHROPIC_API_KEY is not set (add it to .env); use the arm's fake/fallback path")
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=key, max_retries=2)
    return _client


def set_client(client: Any) -> None:
    """Inject a client (tests / custom timeouts). ``None`` resets to lazy creation."""
    global _client
    _client = client


# --- pure helpers ----------------------------------------------------------------------------------------------


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
    """Anthropic tool definition ``{name, description, input_schema}`` from a pydantic model."""
    return {"name": name, "description": description, "input_schema": input_model.model_json_schema()}


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
    """SDK content block -> plain dict for re-sending in ``messages`` (dicts pass through)."""
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


# --- complete_json ---------------------------------------------------------------------------------------------


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
    client: Any = None,
) -> T:
    """Ask for JSON matching ``schema_model`` and return a validated instance.

    ``prompt`` is a user string or a full ``messages`` list. Raises :class:`LLMOutputError` after ``retries``
    re-asks, :class:`LLMNotConfigured` without an API key.
    """
    client = client or get_client()
    messages: list[dict[str, Any]] = (
        [{"role": "user", "content": prompt}] if isinstance(prompt, str) else [dict(m) for m in prompt]
    )
    mid = model_id or model(kind)
    last_err: Exception | None = None

    for attempt in range(retries + 1):
        kwargs: dict[str, Any] = _strip_none(
            {"model": mid, "max_tokens": max_tokens, "messages": messages, "system": cached_system(system)}
        )
        if mode == "structured":
            from anthropic import transform_schema

            kwargs["output_config"] = {"format": {"type": "json_schema", "schema": transform_schema(schema_model)}}
        else:
            kwargs["tools"] = cached_tools(
                [tool_from_model(JSON_TOOL_NAME, f"Return the {schema_model.__name__} result.", schema_model)]
            )
            kwargs["tool_choice"] = {"type": "tool", "name": JSON_TOOL_NAME}

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


# --- tool loop -------------------------------------------------------------------------------------------------


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
    client: Any = None,
) -> ToolLoopResult:
    """Call the model, run requested tools via ``handlers[name](input)`` (sync or async), feed results back, repeat
    until the model stops asking for tools or ``max_iters`` model calls are made.

    Handler exceptions and unknown tools become ``is_error`` tool results (the model can recover); they never
    escape. ``on_tool_call`` (sync or async) fires after each tool runs, e.g. to publish an event.
    """
    client = client or get_client()
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
