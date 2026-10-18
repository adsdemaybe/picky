"""Gemini provider of utils.llm. No network, ever.

- Translation + adapter tests use SimpleNamespace stand-ins for google-genai objects (base deps only).
- The round-trip tests (``importorskip("google.genai")``, i.e. ``uv sync --extra agent``) drive the REAL SDK
  2.31 over an ``httpx.MockTransport``: they check the exact JSON the SDK would put on the wire and parse a REST-shaped
  response back.
"""

from __future__ import annotations

import base64
import json
import sys
import types as pytypes
from types import SimpleNamespace as NS

import pytest
from pydantic import BaseModel

from utils import llm
from utils.config import reset_settings, set_settings


class Item(BaseModel):
    name: str
    qty: int


BLANK = dict(
    _env_file=None,
    picky_llm_provider="gemini",
    gemini_api_key="",
    anthropic_api_key="",
    picky_model_brain="",
    picky_model_fast="",
    picky_model_smart="",
    picky_gemini_thinking="low",
)


@pytest.fixture(autouse=True)
def _settings():
    set_settings(**BLANK)
    llm.set_client(None)
    llm._warned.clear()
    yield
    llm.set_client(None)
    reset_settings()


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


# --- stand-ins for google-genai response objects -----------------------------------------------------------------


def g_text(text, sig=None):
    return NS(text=text, function_call=None, thought=None, thought_signature=sig)


def g_call(name, args, call_id=None, sig=None):
    return NS(text=None, function_call=NS(id=call_id, name=name, args=args), thought=None, thought_signature=sig)


def g_resp(*parts, finish="STOP", prompt_tokens=10, out_tokens=5, thoughts=3):
    return NS(
        candidates=[NS(content=NS(role="model", parts=list(parts)), finish_reason=finish)],
        usage_metadata=NS(
            prompt_token_count=prompt_tokens, candidates_token_count=out_tokens, thoughts_token_count=thoughts
        ),
        response_id="r1",
        model_version="gemini-3.8-flash",
        prompt_feedback=None,
    )


class FakeGenai:
    """Shape of ``google.genai.Client``: ``await client.aio.models.generate_content(model=, contents=, config=)``."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[dict] = []
        self.aio = NS(models=self)

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class ClientError(Exception):
    """Same name + ``code`` attribute as ``google.genai.errors.ClientError``."""

    def __init__(self, code, msg="bad"):
        super().__init__(f"{code} {msg}")
        self.code = code


# --- request translation -----------------------------------------------------------------------------------------


def test_request_config_mapping():
    tools = llm.cached_tools(
        [
            {"name": "find", "description": "Find it", "input_schema": {"type": "object"}, "strict": True},
            {"name": "noargs", "description": ""},
            {"type": "web_search_20250305", "name": "web_search"},  # Anthropic server tool: skipped
        ]
    )
    req = llm.to_gemini_request(
        model="gemini-3.8-flash",
        max_tokens=1024,
        system=llm.cached_system("be terse"),
        tools=tools,
        tool_choice={"type": "tool", "name": "find"},
        output_config={"format": {"type": "json_schema", "schema": {"type": "object"}}},
        temperature=0.2,
        stop_sequences=["END"],
        messages=[{"role": "user", "content": "hi"}],
        metadata={"user_id": "x"},  # Anthropic-only: ignored
    )
    cfg = req["config"]
    assert req["model"] == "gemini-3.8-flash"
    assert req["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]
    assert cfg["system_instruction"] == "be terse"
    assert cfg["max_output_tokens"] == 1024 + llm.GEMINI_THINKING_HEADROOM
    assert cfg["tools"] == [
        {
            "function_declarations": [
                {"name": "find", "description": "Find it", "parameters_json_schema": {"type": "object"}},
                {"name": "noargs"},
            ]
        }
    ]
    assert cfg["automatic_function_calling"] == {"disable": True}
    assert cfg["tool_config"] == {"function_calling_config": {"mode": "ANY", "allowed_function_names": ["find"]}}
    assert cfg["response_mime_type"] == "application/json" and cfg["response_json_schema"] == {"type": "object"}
    assert cfg["thinking_config"] == {"thinking_level": "LOW"}  # PICKY_GEMINI_THINKING default
    assert cfg["temperature"] == 0.2 and cfg["stop_sequences"] == ["END"]


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        ({"type": "auto"}, {"function_calling_config": {"mode": "AUTO"}}),
        ({"type": "any"}, {"function_calling_config": {"mode": "ANY"}}),
        ({"type": "none"}, {"function_calling_config": {"mode": "NONE"}}),
        (None, None),
    ],
)
def test_tool_choice(choice, expected):
    assert llm.to_gemini_tool_config(choice) == expected


def test_thinking_levels_and_caps():
    msgs = [{"role": "user", "content": "x"}]
    assert llm.to_gemini_request(model="m", messages=msgs, output_config={"effort": "high"})["config"][
        "thinking_config"
    ] == {"thinking_level": "HIGH"}
    assert llm.to_gemini_request(model="m", messages=msgs, output_config={"effort": "max"})["config"][
        "thinking_config"
    ] == {"thinking_level": "HIGH"}
    set_settings(**{**BLANK, "picky_gemini_thinking": ""})
    cfg = llm.to_gemini_request(model="m", messages=msgs, max_tokens=10**6)["config"]
    assert "thinking_config" not in cfg and cfg["max_output_tokens"] == llm.GEMINI_MAX_OUTPUT_TOKENS
    set_settings(**{**BLANK, "picky_gemini_thinking": "turbo"})  # unknown: model default, warning
    assert "thinking_config" not in llm.to_gemini_request(model="m", messages=msgs)["config"]
    assert "tools" not in cfg and "tool_config" not in cfg


def test_contents_translation():
    jpeg = b"\xff\xd8\xff\xe0jpeg"
    msgs = [
        {
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(jpeg)}},
                {"type": "image", "source": {"type": "url", "url": "https://x/y.png", "media_type": "image/png"}},
                {"type": "text", "text": "what is this?"},
                {"type": "text", "text": ""},  # empty: dropped
            ],
        },
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "s"},  # Anthropic-only: dropped
                {"type": "text", "text": "Looking.", "thought_signature": b64(b"sigT")},
                {
                    "type": "tool_use",
                    "id": "fc1",
                    "name": "find",
                    "input": {"q": "salsa"},
                    "thought_signature": b64(b"sig1"),
                },
                {"type": "tool_use", "id": "fc2", "name": "route", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "fc1", "content": '{"sku": "A1", "aisle": 3}'},
                {"type": "tool_result", "tool_use_id": "fc2", "content": "RuntimeError: no route", "is_error": True},
                {"type": "text", "text": "Message from the user: hurry"},
            ],
        },
        {"role": "assistant", "content": []},  # nothing left: skipped
    ]
    contents = llm.to_gemini_contents(msgs)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    user, model_turn, results = contents
    assert user["parts"] == [
        {"inline_data": {"mime_type": "image/jpeg", "data": jpeg}},
        {"file_data": {"file_uri": "https://x/y.png", "mime_type": "image/png"}},
        {"text": "what is this?"},
    ]
    assert model_turn["parts"] == [
        {"text": "Looking.", "thought_signature": b"sigT"},
        {"function_call": {"name": "find", "args": {"q": "salsa"}, "id": "fc1"}, "thought_signature": b"sig1"},
        {"function_call": {"name": "route", "args": {}, "id": "fc2"}},  # parallel call: only the first is signed
    ]
    assert results["parts"] == [
        {"function_response": {"name": "find", "response": {"output": {"sku": "A1", "aisle": 3}}, "id": "fc1"}},
        {"function_response": {"name": "route", "response": {"error": "RuntimeError: no route"}, "id": "fc2"}},
        {"text": "Message from the user: hurry"},
    ]


def test_unsigned_and_local_calls():
    """Hand-built history: the first call of a step gets the documented skip signature; local ids are not sent."""
    msgs = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "gcall_abc", "name": "a", "input": {}},
                {"type": "tool_use", "id": "x2", "name": "b", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "gcall_abc",
                    "content": [
                        {"type": "text", "text": "frame attached"},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(b"j")}},
                    ],
                },
                {"type": "tool_result", "tool_use_id": "x2", "content": "7"},
            ],
        },
    ]
    _, model_turn, results = llm.to_gemini_contents(msgs)
    first, second = model_turn["parts"]
    assert first == {"function_call": {"name": "a", "args": {}}, "thought_signature": llm.GEMINI_SKIP_SIGNATURE}
    assert second == {"function_call": {"name": "b", "args": {}, "id": "x2"}}
    fr_a, fr_b = (p["function_response"] for p in results["parts"])
    assert fr_a == {
        "name": "a",
        "response": {"output": "frame attached"},
        "parts": [{"inline_data": {"mime_type": "image/jpeg", "data": b"j"}}],
    }
    assert fr_b == {"name": "b", "response": {"output": "7"}, "id": "x2"}  # scalars stay text


# --- response translation ----------------------------------------------------------------------------------------


def test_response_translation():
    raw = g_resp(
        NS(text="private reasoning", function_call=None, thought=True, thought_signature=None),  # thought summary
        g_text("On it."),
        g_call("find", {"q": "salsa", "qty": 2}, call_id="fc9", sig=b"sigA"),
        g_call("route", {}),  # no id from the API: a local one
        g_text("", sig=b"tail"),  # empty signature-only text part: not an answer block
    )
    r = llm.from_gemini_response(raw, model="gemini-3.8-flash")
    assert r.stop_reason == "tool_use" and r.finish_reason == "STOP" and r.id == "r1"
    assert llm.response_text(r) == "On it."
    find, route = llm.tool_uses(r)
    assert (find.id, find.name, find.input) == ("fc9", "find", {"q": "salsa", "qty": 2})
    assert find.thought_signature == b64(b"sigA")
    assert route.id.startswith(llm.GEMINI_LOCAL_ID_PREFIX) and route.thought_signature is None
    assert r.usage.input_tokens == 10 and r.usage.output_tokens == 8
    dumped = [llm.block_to_param(b) for b in r.content]
    assert dumped[1] == {
        "type": "tool_use",
        "id": "fc9",
        "name": "find",
        "input": {"q": "salsa", "qty": 2},
        "thought_signature": b64(b"sigA"),
    }
    # Echoing the turn back restores the exact bytes on the exact part.
    echoed = llm.to_gemini_contents([{"role": "assistant", "content": dumped}])[0]["parts"]
    assert echoed[1]["thought_signature"] == b"sigA"
    assert "id" not in echoed[2]["function_call"] and "thought_signature" not in echoed[2]


@pytest.mark.parametrize(
    ("finish", "stop"),
    [
        ("STOP", "end_turn"),
        ("MAX_TOKENS", "max_tokens"),
        ("SAFETY", "refusal"),
        ("PROHIBITED_CONTENT", "refusal"),
        ("MALFORMED_FUNCTION_CALL", "malformed_tool_call"),
        (NS(name="MAX_TOKENS", value="MAX_TOKENS"), "max_tokens"),  # enum-like
        (None, "end_turn"),
    ],
)
def test_stop_reasons(finish, stop):
    assert llm.from_gemini_response(g_resp(g_text("x"), finish=finish)).stop_reason == stop


def test_blocked_prompt_is_a_refusal():
    raw = NS(candidates=[], prompt_feedback=NS(block_reason="SAFETY"), usage_metadata=None)
    r = llm.from_gemini_response(raw, model="gemini-3.8-flash")
    assert r.stop_reason == "refusal" and r.finish_reason == "SAFETY" and r.content == []
    assert r.model == "gemini-3.8-flash"


# --- client wiring -----------------------------------------------------------------------------------------------


def test_raw_genai_client_is_wrapped():
    raw = FakeGenai([])
    llm.set_client(raw)
    c = llm.get_client()
    assert isinstance(c, llm.GeminiMessages) and c.genai is raw and c.messages is c
    assert llm.client_provider(c) == "gemini"
    assert llm.as_messages_client(c) is c


def test_key_without_sdk_is_not_configured(monkeypatch):
    set_settings(**{**BLANK, "gemini_api_key": "g-test"})
    monkeypatch.setattr(llm, "_sdk_available", lambda p: False)
    assert not llm.is_configured()
    assert "uv sync --extra agent" in llm.not_configured_reason()
    with pytest.raises(llm.LLMNotConfigured, match="SDK"):
        llm.get_client()


def test_get_client_builds_genai_client(monkeypatch):
    built = []

    class StubClient:
        def __init__(self, **kwargs):
            built.append(kwargs)
            self.aio = NS(models=NS())

    stub = pytypes.ModuleType("google.genai")
    stub.Client = StubClient
    google_pkg = pytypes.ModuleType("google")
    google_pkg.genai = stub
    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", stub)
    monkeypatch.setattr(llm, "_sdk_available", lambda p: True)
    set_settings(**{**BLANK, "gemini_api_key": "g-test", "picky_llm_timeout_s": 12.5})

    c = llm.get_client()
    assert isinstance(c, llm.GeminiMessages) and llm.get_client() is c
    assert built == [
        {"api_key": "g-test", "http_options": {"timeout": 12500, "retry_options": {"attempts": 3, "max_delay": 8.0}}}
    ]
    set_settings(**{**BLANK, "gemini_api_key": "g-other"})
    assert llm.get_client() is not c and built[-1]["api_key"] == "g-other"


# --- complete_json / run_tool_loop over the adapter ----------------------------------------------------------------


async def test_complete_json_structured_with_retry():
    raw = FakeGenai([g_resp(g_text('{"name": "salsa"}')), g_resp(g_text('```json\n{"name": "salsa", "qty": 2}\n```'))])
    item = await llm.complete_json("give item", Item, system="be terse", client=raw)
    assert item == Item(name="salsa", qty=2)
    first, second = raw.calls
    assert first["model"] == "gemini-3.8-flash"
    assert first["config"]["response_json_schema"] == Item.model_json_schema()
    assert first["config"]["response_mime_type"] == "application/json"
    assert first["config"]["system_instruction"] == "be terse"
    assert [c["role"] for c in second["contents"]] == ["user", "model", "user"]
    assert "invalid" in second["contents"][-1]["parts"][0]["text"]


async def test_complete_json_bad_request_falls_back_to_tool_forcing():
    raw = FakeGenai(
        [ClientError(400, "schema too complex"), g_resp(g_call("emit_result", {"name": "lime", "qty": 6}, "c1", b"s"))]
    )
    item = await llm.complete_json("x", Item, kind="smart", effort="medium", client=raw)
    assert item == Item(name="lime", qty=6)
    tool_req = raw.calls[1]
    assert tool_req["model"] == "gemini-4-argon"
    assert tool_req["config"]["tool_config"] == {
        "function_calling_config": {"mode": "ANY", "allowed_function_names": ["emit_result"]}
    }
    assert tool_req["config"]["thinking_config"] == {"thinking_level": "MEDIUM"}
    assert "response_json_schema" not in tool_req["config"]


async def test_other_api_errors_propagate():
    raw = FakeGenai([ClientError(429, "quota")])
    with pytest.raises(ClientError):
        await llm.complete_json("x", Item, client=raw)
    with pytest.raises(llm.BadRequestError):
        await llm.GeminiMessages(FakeGenai([ClientError(400)])).messages.create(model="m", messages=[])


async def test_run_tool_loop_echoes_ids_and_signatures():
    raw = FakeGenai(
        [
            g_resp(g_call("query_inventory", {"q": "salsa"}, "fc_1", b"sigA"), g_call("boom", {}, "fc_2")),
            g_resp(g_text("salsa is in aisle 3")),
        ]
    )

    def boom(args):
        raise RuntimeError("kaput")

    result = await llm.run_tool_loop(
        [{"role": "user", "content": "find salsa"}],
        [
            {"name": "query_inventory", "description": "Find stock", "input_schema": {"type": "object"}},
            {"name": "boom"},
        ],
        {"query_inventory": lambda a: [Item(name=a["q"], qty=1)], "boom": boom},
        system="You are a robot.",
        tool_choice={"type": "any"},
        client=raw,
    )
    assert result.text == "salsa is in aisle 3" and result.iterations == 2 and result.stop_reason == "end_turn"
    assert [c.id for c in result.tool_calls] == ["fc_1", "fc_2"] and result.tool_calls[1].is_error
    first, second = raw.calls
    assert first["config"]["tool_config"] == {"function_calling_config": {"mode": "ANY"}}
    assert "tool_config" not in second["config"]  # tool_choice applies to the first call only
    _, model_turn, results = second["contents"]
    assert model_turn["parts"][0]["thought_signature"] == b"sigA"
    assert model_turn["parts"][0]["function_call"]["id"] == "fc_1"
    assert "thought_signature" not in model_turn["parts"][1]
    assert [p["function_response"]["id"] for p in results["parts"]] == ["fc_1", "fc_2"]
    assert results["parts"][0]["function_response"]["response"] == {"output": [{"name": "salsa", "qty": 1}]}
    assert "kaput" in results["parts"][1]["function_response"]["response"]["error"]


async def test_run_tool_loop_max_iters():
    raw = FakeGenai([g_resp(g_call("t", {}, f"c{i}", b"s")) for i in range(3)])
    result = await llm.run_tool_loop(
        [{"role": "user", "content": "x"}], [{"name": "t"}], {"t": lambda a: "ok"}, max_iters=3, client=raw
    )
    assert result.hit_max_iters and result.iterations == 3 and len(result.tool_calls) == 3


# --- the real google-genai SDK over a mocked HTTP transport --------------------------------------------------------


def _sdk_client(handler):
    genai = pytest.importorskip("google.genai")
    httpx = pytest.importorskip("httpx")
    return genai.Client(
        api_key="test-key",
        http_options={"httpx_async_client": httpx.AsyncClient(transport=httpx.MockTransport(handler))},
    )


def _rest(parts, finish="STOP"):
    return {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4, "thoughtsTokenCount": 6},
        "modelVersion": "gemini-3.8-flash",
        "responseId": "resp-1",
    }


async def test_sdk_round_trip_tool_loop():
    import httpx

    bodies: list[dict] = []
    replies = [
        _rest(
            [
                {"text": "Checking stock."},
                {
                    "functionCall": {"id": "fc_1", "name": "query_inventory", "args": {"q": "salsa"}},
                    "thoughtSignature": b64(b"sigA"),
                },
                {"functionCall": {"id": "fc_2", "name": "plan_route", "args": {"stops": 2}}},
            ]
        ),
        _rest([{"text": "Salsa is in aisle 3."}]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models/gemini-3.8-flash:generateContent")
        assert request.headers["x-goog-api-key"] == "test-key"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json=replies[len(bodies) - 1])

    llm.set_client(_sdk_client(handler))
    jpeg = b"\xff\xd8\xff\xe0frame"
    result = await llm.run_tool_loop(
        [
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(jpeg)}},
                    {"type": "text", "text": "find salsa"},
                ],
            }
        ],
        [
            {
                "name": "query_inventory",
                "description": "Find stock",
                "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
                "strict": True,
            },
            {"name": "plan_route", "description": "Plan", "input_schema": {"type": "object", "properties": {}}},
        ],
        {"query_inventory": lambda a: {"sku": "SALSA-1", "aisle": 3}, "plan_route": lambda a: "ok"},
        system="You are Pico.",
        max_tokens=1024,
    )
    assert result.text == "Salsa is in aisle 3." and result.iterations == 2
    assert [(c.id, c.name, c.input) for c in result.tool_calls] == [
        ("fc_1", "query_inventory", {"q": "salsa"}),
        ("fc_2", "plan_route", {"stops": 2}),
    ]

    first, second = bodies
    assert first["systemInstruction"]["parts"] == [{"text": "You are Pico."}]
    decl = first["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "query_inventory" and decl["parameters_json_schema"]["required"] == ["q"]
    gen = first["generationConfig"]
    assert gen["maxOutputTokens"] == 1024 + llm.GEMINI_THINKING_HEADROOM
    assert gen["thinkingConfig"] == {"thinking_level": "LOW"}
    image = first["contents"][0]["parts"][0]["inlineData"]
    assert base64.urlsafe_b64decode(image["data"]) == jpeg

    user, model_turn, results = second["contents"]
    assert model_turn["role"] == "model"
    assert model_turn["parts"][0] == {"text": "Checking stock."}
    signed, unsigned = model_turn["parts"][1], model_turn["parts"][2]
    assert signed["functionCall"]["id"] == "fc_1" and base64.b64decode(signed["thoughtSignature"]) == b"sigA"
    assert unsigned["functionCall"] == {"id": "fc_2", "args": {"stops": 2}, "name": "plan_route"}
    assert "thoughtSignature" not in unsigned
    assert results["role"] == "user"
    assert [p["functionResponse"] for p in results["parts"]] == [
        {"id": "fc_1", "name": "query_inventory", "response": {"output": {"sku": "SALSA-1", "aisle": 3}}},
        {"id": "fc_2", "name": "plan_route", "response": {"output": "ok"}},
    ]


async def test_sdk_round_trip_complete_json_400_fallback():
    import httpx

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            err = {"error": {"code": 400, "message": "schema too complex", "status": "INVALID_ARGUMENT"}}
            return httpx.Response(400, json=err)
        call = {"functionCall": {"id": "c1", "name": "emit_result", "args": {"name": "lime", "qty": 6}}}
        return httpx.Response(200, json=_rest([{**call, "thoughtSignature": b64(b"s")}]))

    item = await llm.complete_json("six limes", Item, client=_sdk_client(handler))
    assert item == Item(name="lime", qty=6)
    assert bodies[0]["generationConfig"]["responseJsonSchema"] == Item.model_json_schema()
    assert bodies[0]["generationConfig"]["responseMimeType"] == "application/json"
    assert bodies[1]["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["emit_result"]}
    }


def test_sdk_accepts_translated_request():
    """Every dict we build validates against the SDK's own pydantic types (extra='forbid' catches typos)."""
    types = pytest.importorskip("google.genai.types")
    req = llm.to_gemini_request(
        model="gemini-3.8-flash",
        max_tokens=512,
        system=llm.cached_system("sys"),
        tools=llm.cached_tools([llm.tool_from_model("emit_result", "Return it", Item)]),
        tool_choice={"type": "tool", "name": "emit_result"},
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": Item.model_json_schema()}},
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(b"j")}}
                ],
            },
            {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "emit_result", "input": {}}]},
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "a", "content": "{}", "is_error": True}],
            },
        ],
    )
    cfg = types.GenerateContentConfig.model_validate(req["config"])
    assert cfg.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert cfg.tool_config.function_calling_config.mode == types.FunctionCallingConfigMode.ANY
    contents = [types.Content.model_validate(c) for c in req["contents"]]
    assert contents[1].parts[0].thought_signature == llm.GEMINI_SKIP_SIGNATURE
    assert contents[2].parts[0].function_response.response == {"error": {}}
