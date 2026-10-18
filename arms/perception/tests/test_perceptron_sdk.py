"""PerceptronVisionProvider through the REAL perceptron 0.4.x SDK with an in-process httpx transport (no network):
the SDK builds the wire request from our messages and parses the annotation markup; 429 + Retry-After maps to
``rate_limit`` with the wait honoured. Skipped unless the perception extra is installed."""

import json

import pytest

perceptron = pytest.importorskip("perceptron")
httpx = pytest.importorskip("httpx")

from arms.perception.tests.loop_world import TINY_JPEG_B64  # noqa: E402
from arms.perception.vision.perceptron import PerceptronVisionProvider  # noqa: E402
from contracts.sim import CameraFrame  # noqa: E402

FRAME = CameraFrame(
    frame_id="f1", camera="wrist", camera_id="rover-ave-1.wrist", jpeg_b64=TINY_JPEG_B64, w=640, h=480, blurred=True
)


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "perceptron-mk1.5",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def _provider(handler, **kw) -> PerceptronVisionProvider:
    def factory():
        return perceptron.AsyncClient(
            api_key="pk-test",
            base_url="https://api.perceptron.inc/v1",
            provider="perceptron",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    return PerceptronVisionProvider(api_key="pk-test", client_factory=factory, **kw)


async def test_detect_through_the_real_sdk():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization")
        seen.append({"url": str(request.url), "auth": auth, **json.loads(request.content)})
        body = '<point_box mention="Sol de Casa Mild Salsa"> (380,420) (620,800) </point_box>'
        return httpx.Response(200, json=_completion(body), headers={"x-trace-id": "trace-1"})

    ans = await _provider(handler).detect(FRAME, ["Sol de Casa Mild Salsa"], timeout_s=3.0)
    assert ans.call.error_code is None, ans.call
    [box] = ans.boxes
    assert (box.box.x1, box.box.y1, box.box.x2, box.box.y2) == (380, 420, 620, 800)
    assert box.mention == "Sol de Casa Mild Salsa" and ans.call.request_id == "trace-1"
    [req] = seen
    assert req["url"] == "https://api.perceptron.inc/v1/chat/completions" and req["auth"] == "Bearer pk-test"
    assert req["model"] == "perceptron-mk1.5" and req["vision_config"] == {"annotation_format": "box"}
    assert req["messages"][0]["role"] == "system" and "segment out" in req["messages"][0]["content"]
    assert req["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")


async def test_point_question_and_multilook_through_the_real_sdk():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if request.url.path.endswith("/multilook"):
            return httpx.Response(
                200,
                json={
                    "id": "ml-1",
                    "object": "chat.completion.multilook",
                    "model": "perceptron-mk1.5",
                    "results": [
                        {
                            "prompt_index": i,
                            "completions": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": "No, a jar is held."},
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                        for i, _ in enumerate(body["prompts"])
                    ],
                },
            )
        if body.get("vision_config", {}).get("annotation_format") == "point":
            return httpx.Response(200, json=_completion('<point mention="jar"> (512,488) </point>'))
        return httpx.Response(200, json=_completion("Yes, the gripper is empty."))

    p = _provider(handler)
    point = await p.point(FRAME, "Point to the jar")
    assert point.call.error_code is None and (point.points[0].point.x, point.points[0].point.y) == (512, 488)
    yes = await p.question(FRAME, "Is the gripper empty?", "bool")
    assert yes.yes is True
    looks = await p.multilook(FRAME, ["Is the gripper empty?", "Is the label readable?"])
    assert [a.yes for a in looks] == [False, False] and all(a.call.error_code is None for a in looks)


async def test_rate_limit_retry_after_through_the_real_sdk():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(
                429,
                json={"error": {"message": "slow down", "type": "rate_limit_error", "code": "rate_limit"}},
                headers={"retry-after": "0"},
            )
        return httpx.Response(200, json=_completion("Yes."))

    ans = await _provider(handler, timeout_s=3.0).question(FRAME, "Empty?", "bool")
    assert ans.yes is True and len(calls) == 2


async def test_auth_error_degrades_to_a_code():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key", "type": "authentication_error"}})

    ans = await _provider(handler).ocr(FRAME)
    assert ans.call.error_code is not None and ans.text == ""
