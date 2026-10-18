"""No network: a fake client stands in for AsyncAnthropic. Provider-neutral helpers + the anthropic provider.

The Gemini adapter (translation, mocked google-genai client, SDK round trip) is in test_llm_gemini.py.
"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from utils import llm
from utils.config import reset_settings, set_settings


class Item(BaseModel):
    name: str
    qty: int


def text_msg(text, stop="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], stop_reason=stop)


def tool_msg(*uses):
    blocks = [SimpleNamespace(type="tool_use", id=f"tu{i}", name=n, input=a) for i, (n, a) in enumerate(uses)]
    return SimpleNamespace(content=blocks, stop_reason="tool_use")


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.messages = self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


BLANK = dict(
    _env_file=None,
    gemini_api_key="",
    anthropic_api_key="",
    picky_model_brain="",
    picky_model_fast="",
    picky_model_smart="",
    picky_gemini_thinking="low",
)


@pytest.fixture(autouse=True)
def _settings():
    set_settings(**BLANK, picky_llm_provider="anthropic")
    llm.set_client(None)
    llm._warned.clear()
    yield
    llm.set_client(None)
    reset_settings()


def test_models_and_missing_key():
    assert llm.provider() == "anthropic"
    assert llm.model("fast") == "claude-haiku-5-5"
    assert llm.MODEL_BRAIN == "claude-sonnet-5-5" and llm.MODEL_SMART == "claude-opus-5-5"
    assert not llm.is_configured()
    assert "ANTHROPIC_API_KEY" in llm.not_configured_reason()
    with pytest.raises(llm.LLMNotConfigured):
        llm.get_client()


def test_gemini_is_the_default_provider():
    set_settings(**BLANK)
    assert llm.provider() == "gemini"
    assert (llm.MODEL_BRAIN, llm.MODEL_FAST, llm.MODEL_SMART) == (
        "gemini-3.8-flash",
        "gemini-3.8-flash",
        "gemini-4-argon",
    )
    assert "GEMINI_API_KEY" in llm.not_configured_reason()
    with pytest.raises(llm.LLMNotConfigured, match="GEMINI_API_KEY"):
        llm.get_client()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("gemini", "gemini"), ("ANTHROPIC", "anthropic"), ("claude", "anthropic"), ("google", "gemini"), ("", "gemini")],
)
def test_provider_aliases(raw, expected):
    set_settings(**BLANK, picky_llm_provider=raw)
    assert llm.provider() == expected


def test_unknown_provider_means_gemini():
    set_settings(**BLANK, picky_llm_provider="openai")
    assert llm.provider() == "gemini" and llm.model() == "gemini-3.8-flash"


def test_model_overrides_and_stale_ids():
    set_settings(**{**BLANK, "picky_model_brain": "gemini-3.6-flash", "picky_model_smart": "claude-opus-5-5"})
    assert llm.model("brain") == "gemini-3.6-flash"
    assert llm.model("smart") == "gemini-4-argon"  # a Claude id never reaches Gemini (stale .env)
    set_settings(**{**BLANK, "picky_llm_provider": "anthropic", "picky_model_fast": "gemini-3.8-flash"})
    assert llm.model("fast") == "claude-haiku-5-5"
    set_settings(**{**BLANK, "picky_llm_provider": "anthropic", "picky_model_brain": "claude-sonnet-5-6"})
    assert llm.model("brain") == "claude-sonnet-5-6"


def test_anthropic_client_built_lazily_and_rebuilt_on_key_change():
    set_settings(**{**BLANK, "picky_llm_provider": "anthropic", "anthropic_api_key": "sk-ant-test"})
    assert llm.is_configured() and llm.not_configured_reason() is None
    c = llm.get_client()
    assert type(c).__name__ == "AsyncAnthropic" and llm.get_client() is c
    assert llm.client_provider(c) == "anthropic"
    set_settings(**{**BLANK, "picky_llm_provider": "anthropic", "anthropic_api_key": "sk-ant-other"})
    assert llm.get_client() is not c


def test_injected_client_wins_over_settings():
    fake = FakeClient([])
    llm.set_client(fake)
    assert llm.get_client() is fake  # no key needed
    set_settings(**BLANK)  # provider change does not drop an injected client
    assert llm.get_client() is fake


def test_cache_helpers_do_not_mutate():
    tools = [{"name": "a"}, {"name": "b"}]
    out = llm.cached_tools(tools)
    assert out[-1]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in tools[-1]
    assert "cache_control" not in out[0]
    assert llm.cached_system("sys") == [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]
    assert llm.cached_system(None) is None
    t = llm.tool_from_model("add", "Add item", Item)
    assert t["input_schema"]["properties"]["qty"]["type"] == "integer"


@pytest.mark.parametrize(
    "text",
    ['{"a": 1}', '```json\n{"a": 1}\n```', 'Sure! Here you go: {"a": 1} hope that helps', 'noise [ {"a": 1}'],
)
def test_extract_json(text):
    got = llm.extract_json(text)
    assert got == {"a": 1} or got == [{"a": 1}]


def test_extract_json_fail():
    with pytest.raises(ValueError):
        llm.extract_json("no json here")


async def test_complete_json_structured_with_retry():
    client = FakeClient([text_msg('{"name": "salsa"}'), text_msg('{"name": "salsa", "qty": 2}')])
    item = await llm.complete_json("give item", Item, system="be terse", client=client)
    assert item == Item(name="salsa", qty=2)
    first = client.calls[0]
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert first["output_config"]["format"]["schema"]["additionalProperties"] is False  # anthropic transform_schema
    assert first["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert first["model"] == "claude-sonnet-5-5"
    assert client.calls[1]["messages"][-1]["role"] == "user" and "invalid" in client.calls[1]["messages"][-1]["content"]


async def test_complete_json_gives_up():
    client = FakeClient([text_msg("nope"), text_msg("still nope")])
    with pytest.raises(llm.LLMOutputError):
        await llm.complete_json("x", Item, client=client)


async def test_complete_json_tool_mode_and_fallback():
    class BadRequestError(Exception):
        pass

    client = FakeClient([BadRequestError("schema"), tool_msg(("emit_result", {"name": "lime", "qty": 6}))])
    item = await llm.complete_json("x", Item, client=client, kind="fast")
    assert item.qty == 6
    assert client.calls[1]["tool_choice"] == {"type": "tool", "name": "emit_result"}
    assert client.calls[1]["model"] == "claude-haiku-5-5"


async def test_run_tool_loop():
    client = FakeClient(
        [
            tool_msg(("query_inventory", {"q": "salsa"}), ("boom", {}), ("missing", {})),
            text_msg("done: salsa in aisle 3"),
        ]
    )
    seen = []

    async def query_inventory(args):
        return [Item(name=args["q"], qty=1)]

    def boom(args):
        raise RuntimeError("kaput")

    result = await llm.run_tool_loop(
        [{"role": "user", "content": "find salsa"}],
        [{"name": "query_inventory", "description": "", "input_schema": {"type": "object"}}],
        {"query_inventory": query_inventory, "boom": boom},
        on_tool_call=seen.append,
        client=client,
    )
    assert result.text == "done: salsa in aisle 3" and result.iterations == 2 and not result.hit_max_iters
    assert [c.name for c in result.tool_calls] == ["query_inventory", "boom", "missing"]
    assert result.tool_calls[0].output == '[{"name": "salsa", "qty": 1}]'
    assert result.tool_calls[1].is_error and "kaput" in result.tool_calls[1].output
    assert len(seen) == 3
    tool_results = result.messages[2]["content"]
    assert [r["tool_use_id"] for r in tool_results] == ["tu0", "tu1", "tu2"]
    assert tool_results[1]["is_error"] is True and "is_error" not in tool_results[0]
    assert client.calls[0]["tools"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_effort_goes_in_output_config():
    client = FakeClient([text_msg('{"name": "a", "qty": 1}'), text_msg("ok")])
    await llm.complete_json("x", Item, effort="low", client=client)
    assert client.calls[0]["output_config"]["effort"] == "low"
    await llm.run_tool_loop([{"role": "user", "content": "x"}], [{"name": "t"}], {}, effort="medium", client=client)
    assert client.calls[1]["output_config"] == {"effort": "medium"}


async def test_run_tool_loop_max_iters():
    client = FakeClient([tool_msg(("t", {}))] * 3)
    result = await llm.run_tool_loop(
        [{"role": "user", "content": "x"}], [{"name": "t"}], {"t": lambda a: "ok"}, max_iters=3, client=client
    )
    assert result.hit_max_iters and result.iterations == 3 and len(result.tool_calls) == 3
