"""PerceptronVisionProvider against a MOCKED client (no SDK, no network): request shapes, 0-1000 grid parsing,
blurred-only frames, per-call budgets, timeouts, RateLimitError.retry_after, error codes, one client per loop."""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arms.perception.tests.loop_world import TINY_JPEG_B64
from arms.perception.vision.perceptron import (
    DETECT_SYSTEM,
    OCR_SYSTEM,
    YES_NO_SUFFIX,
    PerceptronVisionProvider,
    error_code_of,
)
from contracts.services import VisionProvider
from contracts.sim import CameraFrame

FRAME = CameraFrame(
    frame_id="f1", camera="wrist", camera_id="rover-ave-1.wrist", jpeg_b64=TINY_JPEG_B64, w=640, h=480, blurred=True
)
RAW = FRAME.model_copy(update={"blurred": False})


def pt(x, y, mention="", asset_idx=None):
    return SimpleNamespace(x=x, y=y, mention=mention, asset_idx=asset_idx)


def bx(x1, y1, x2, y2, mention="", asset_idx=None):
    return SimpleNamespace(top_left=pt(x1, y1), bottom_right=pt(x2, y2), mention=mention, asset_idx=asset_idx)


class Completion:
    def __init__(self, text="", boxes=(), points=(), asset_count=1, request_id="req-1"):
        self.text, self.request_id, self.model = text, request_id, "perceptron-mk1.5"
        self._ann = SimpleNamespace(boxes=list(boxes), points=list(points))
        self.asset_count = asset_count

    def annotations(self):
        return self._ann

    def resolve_asset_idx(self, a):
        return a.asset_idx if a.asset_idx is not None else self.asset_count - 1


class RateLimitError(Exception):  # stands in for perceptron.RateLimitError (code + retry_after)
    def __init__(self, retry_after=None):
        super().__init__("429")
        self.code, self.retry_after = "rate_limit", retry_after


class QuotaExceededError(RateLimitError):
    def __init__(self):
        super().__init__(retry_after=0.0)
        self.code = "insufficient_quota"


class SDKError(Exception):
    code = None


class TimeoutError_(SDKError):  # noqa: N801 - perceptron.TimeoutError is an SDKError, not the builtin
    pass


TimeoutError_.__name__ = "TimeoutError"


class MockCompletions:
    def __init__(self, results: list[Any]):
        self.results = list(results)
        self.requests: list[dict[str, Any]] = []

    async def _next(self, kw):
        self.requests.append(kw)
        r = self.results.pop(0) if self.results else Completion("")
        if isinstance(r, float):
            await asyncio.sleep(r)
            return Completion("late")
        if isinstance(r, BaseException):
            raise r
        return r

    async def create(self, **kw):
        return await self._next(kw)

    async def multilook(self, **kw):
        return await self._next(kw)


def provider(*results, **kw) -> tuple[PerceptronVisionProvider, MockCompletions]:
    comps = MockCompletions(list(results))
    client = SimpleNamespace(chat=SimpleNamespace(completions=comps))
    return PerceptronVisionProvider(client=client, **kw), comps


def test_is_a_vision_provider():
    p, _ = provider()
    assert isinstance(p, VisionProvider) and p.name == "perceptron" and p.model == "perceptron-mk1.5"


async def test_detect_request_shape_and_box_parsing():
    p, comps = provider(Completion(boxes=[bx(620, 800, 380, 420, "Sol de Casa Mild Salsa"), bx(-5, 10, 1200, 30)]))
    ans = await p.detect(FRAME, ["Sol de Casa Mild Salsa, 16 oz", "other grocery item"], timeout_s=2.0)
    [req] = comps.requests
    system, user = req["messages"]
    cats = "Sol de Casa Mild Salsa  16 oz, other grocery item"  # commas stripped: the template joins with ", "
    assert system == {"role": "system", "content": DETECT_SYSTEM.format(categories=cats)}
    assert user["content"] == [{"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{TINY_JPEG_B64}"}}]
    assert req["vision_config"] == {"annotation_format": "box"} and req["model"] == "perceptron-mk1.5"
    assert req["reasoning_effort"] == "none" and req["timeout"] <= 2.0
    a, b = ans.boxes
    assert (a.box.x1, a.box.y1, a.box.x2, a.box.y2) == (380, 420, 620, 800) and a.mention == "Sol de Casa Mild Salsa"
    assert (b.box.x1, b.box.x2) == (0, 1000)  # clamped to the 0-1000 grid
    assert ans.call.provider == "perceptron" and ans.call.request_id == "req-1" and ans.call.error_code is None
    assert ans.call.attribution == "Perceptron perceptron-mk1.5"


async def test_question_bool_point_and_ocr():
    p, comps = provider(
        Completion("Yes, nothing is held between the jaws."),
        Completion(points=[pt(512, 488, "jar")]),
        Completion("SOL DE CASA Mild Chunky Salsa 16 oz"),
    )
    yes = await p.question(FRAME, "Is the gripper empty?", "bool")
    assert yes.yes is True and comps.requests[0]["messages"][0]["content"][1]["text"].endswith(YES_NO_SUFFIX)
    assert "vision_config" not in comps.requests[0]
    point = await p.point(FRAME, "Point to the jar")
    assert (point.points[0].point.x, point.points[0].point.y) == (512, 488)
    assert comps.requests[1]["vision_config"] == {"annotation_format": "point"}
    ocr = await p.ocr(FRAME, "Read the label")
    assert ocr.text.startswith("SOL DE CASA") and comps.requests[2]["messages"][0]["content"] == OCR_SYSTEM


async def test_compare_keeps_only_annotations_on_the_after_image():
    p, comps = provider(Completion(boxes=[bx(1, 1, 5, 5, "before", 0), bx(10, 10, 50, 50, "after")], asset_count=2))
    ans = await p.compare(FRAME, FRAME, "Box every salsa jar")
    assert [b.mention for b in ans.boxes] == ["after"]
    assert len([c for c in comps.requests[0]["messages"][0]["content"] if c["type"] == "image_url"]) == 2


async def test_multilook_maps_results_by_prompt_index():
    ok = SimpleNamespace(prompt_index=1, ok=True, completions=[Completion("No, a jar is held.")], error=None)
    bad = SimpleNamespace(prompt_index=0, ok=False, completions=None, error=SimpleNamespace(code="overloaded"))
    p, comps = provider(SimpleNamespace(results=[ok, bad]))
    first, second = await p.multilook(FRAME, ["Box the held object", "Is the gripper empty?"])
    assert first.call.error_code == "overloaded" and second.yes is False
    req = comps.requests[0]
    assert req["prompts"] == ["Box the held object", "Is the gripper empty?"]
    assert req["context"][0]["content"][0]["type"] == "image_url"


async def test_unblurred_frames_are_rejected_before_any_call():
    p, comps = provider()
    for call in (p.detect(RAW, ["x"]), p.point(RAW, "x"), p.question(RAW, "x"), p.ocr(RAW), p.multilook(RAW, ["x"])):
        with pytest.raises(ValueError, match="blurred"):
            await call
    with pytest.raises(ValueError):
        await p.compare(FRAME, RAW, "x")
    assert comps.requests == []


async def test_no_key_answers_unavailable_without_building_a_client():
    built = []
    p = PerceptronVisionProvider(api_key="")
    p._client_factory = None
    assert p.unavailable_reason() == "PERCEPTRON_API_KEY not set" and not p.available
    ans = await p.detect(FRAME, ["x"])
    assert ans.call.error_code == "unavailable" and ans.boxes == [] and built == []
    assert [a.call.error_code for a in await p.multilook(FRAME, ["a", "b"])] == ["unavailable"] * 2


async def test_timeouts_are_bounded_by_the_callers_budget():
    p, _ = provider(5.0, timeout_s=8.0)
    t0 = asyncio.get_running_loop().time()
    ans = await p.detect(FRAME, ["x"], timeout_s=0.3)
    assert ans.call.error_code == "timeout" and asyncio.get_running_loop().time() - t0 < 1.0
    tiny = await p.ocr(FRAME, timeout_s=0.05)
    assert tiny.call.error_code == "deadline"


async def test_rate_limit_retry_after_is_honoured():
    p, comps = provider(RateLimitError(retry_after=0.05), Completion("Yes."), timeout_s=2.0)
    ans = await p.question(FRAME, "empty?", "bool")
    assert ans.yes is True and len(comps.requests) == 2  # one in-budget retry after the wait
    p2, comps2 = provider(RateLimitError(retry_after=30.0), Completion("Yes."), timeout_s=2.0)
    first = await p2.question(FRAME, "empty?", "bool")
    assert first.call.error_code == "rate_limit" and len(comps2.requests) == 1
    second = await p2.question(FRAME, "empty?", "bool")  # still inside retry_after: fails fast, no request
    assert second.call.error_code == "rate_limit" and len(comps2.requests) == 1


async def test_quota_and_sdk_errors_map_to_codes():
    p, comps = provider(QuotaExceededError(), TimeoutError_(), RuntimeError("boom"))
    assert (await p.detect(FRAME, ["x"])).call.error_code == "insufficient_quota"
    assert len(comps.requests) == 1  # quota is never retried
    assert (await p.detect(FRAME, ["x"])).call.error_code == "timeout"
    assert (await p.detect(FRAME, ["x"])).call.error_code == "RuntimeError"
    assert error_code_of(TimeoutError_()) == "timeout"


def test_one_client_per_event_loop():
    built = []

    def factory():
        built.append(1)
        return SimpleNamespace(chat=SimpleNamespace(completions=MockCompletions([])))

    p = PerceptronVisionProvider(client_factory=factory)
    asyncio.run(p.ocr(FRAME))
    asyncio.run(p.ocr(FRAME))
    assert len(built) == 2  # a new loop gets its own client (an AsyncClient's pool belongs to its loop)

    async def twice():
        await p.ocr(FRAME)
        await p.ocr(FRAME)

    asyncio.run(twice())
    assert len(built) == 3
