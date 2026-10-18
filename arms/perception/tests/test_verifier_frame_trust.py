"""Frame trust for label / identity checks.

Live failure on vhagar (goal_bu7gy5gm, rover-ave-2, Isaac, decide mode): the post-grasp wrist frame was 97 % black,
Perceptron's OCR "read" 'Brand: Nike / Product Name: Air Max 270 React / Size: 10' off it, the label check FAILED
('vision had no fresh frame; fell back to robot state') and the runner tried to put the correctly grasped Habanero
salsa back. A missing / stale / foreign / reused / dark frame or an off-catalog read must make the check UNCERTAIN
(ground truth / robot state decide), never WRONG; a fresh frame showing another product sold here is still WRONG."""

import base64
import io
from datetime import timedelta

import pytest

import arms.perception.evidence as evidence_mod
from arms.perception.evidence import EvidenceRing, FakeEvidenceCameras
from arms.perception.frame_trust import DARK_FRAME_FRACTION, FrameReuse, dark_fraction, frame_mismatch
from arms.perception.judges import CameraAnswers, catalog_match, judge_label
from arms.perception.tests.loop_world import ROBOT, STORE, ScriptedProvider, answer, world
from arms.perception.verifier import PerceptionVerifier
from contracts.common import utcnow
from contracts.evidence import FrameRef
from contracts.grounding import ProviderCall
from contracts.plan import AllOf, ExecContext, GripperHolding, LabelMatches
from contracts.sim import CameraFrame
from contracts.verify import VerifyRequest, VisionAnswer

HOT = "Fuego Ridge Habanero Fire Salsa 16 oz"
EXPECT = "Habanero Fire Salsa 16 oz"
NIKE = "Brand: Nike\nProduct Name: Air Max 270 React\nSize: 10"  # the live read, verbatim
HOT_LABEL = "Brand: Fuego Ridge\nProduct Name: Habanero Fire Salsa\nSize: 16 oz"
MILD_LABEL = "Brand: Sol de Casa\nProduct Name: Mild Chunky Salsa\nSize: 16 oz"
LIME_LABEL = "Brand: Picky Farms\nProduct Name: Lime"  # another product sold here, no word in common with the salsa
IN_JAWS = (380, 420, 620, 800)
LABEL = LabelMatches(sku="SALSA-HOT", expect_text=EXPECT, min_confidence=0.6, advisory=True, note="right label")
COND = AllOf(of=[GripperHolding(sku="SALSA-HOT", note="holding the hot salsa"), LABEL])
CATALOG = {"SALSA-MILD": "Sol de Casa Mild Chunky Salsa 16 oz", "SALSA-HOT": HOT, "LIMES": "Picky Farms Lime"}


def _jpeg_b64(dark_rows: float, *, size: tuple[int, int] = (640, 480), base: int = 0) -> str:
    """A frame whose top ``1 - dark_rows`` share is light grey and the rest ``base`` (0 = black)."""
    from PIL import Image, ImageDraw

    img = Image.new("RGB", size, (base, base, base))
    light = int(size[1] * (1 - dark_rows))
    if light:
        ImageDraw.Draw(img).rectangle([0, 0, size[0], light - 1], fill=(180, 180, 180))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def _frame(jpeg_b64: str, **kw) -> CameraFrame:
    fields = {"frame_id": "f", "camera_id": f"{ROBOT}.wrist", "robot_id": ROBOT, "store_id": STORE, "w": 640, "h": 480}
    return CameraFrame(jpeg_b64=jpeg_b64, blurred=True, **{**fields, **kw})


def _provider(ocr: str, *, camera_sees_jar: bool = True) -> ScriptedProvider:
    if camera_sees_jar:
        return ScriptedProvider(
            {
                "detect": answer(boxes=[(HOT, IN_JAWS)]),
                "question": answer(text="No, a jar is held.", yes=False),
                "ocr": answer(text=ocr),
            }
        )
    return ScriptedProvider(  # what Perceptron said about the live black frame
        {
            "detect": answer(boxes=[]),
            "question": answer(text="The gripper jaws are open and no object is visible.", yes=True),
            "ocr": answer(text=ocr),
        }
    )


def _setup(provider: ScriptedProvider, *, backend: str = "isaac"):
    services, robot = world(backend=backend)
    services.evidence = FakeEvidenceCameras(services)  # type: ignore[assignment]
    robot.face("SALSA-HOT")
    robot.pick("SALSA-HOT")
    return services, robot, PerceptionVerifier(services, provider, mode="decide")


def _req(vid: str = "v", **kw) -> VerifyRequest:
    return VerifyRequest(verify_id=vid, robot_id=ROBOT, store_id=STORE, condition=kw.pop("cond", COND), **kw)


def _label_atom(verdict):
    return next(a for a in verdict.atoms if a.op == "label_matches")


def _kinds(provider: ScriptedProvider) -> list[str]:
    return [k for k, _ in provider.calls]


# ---- judge: an off-catalog read is unreliable --------------------------------------------------------------------
def _judge(text: str, catalog: dict[str, str] | None = None):
    ref = FrameRef(frame_id="f1", camera_id=f"{ROBOT}.wrist")
    ans = CameraAnswers(
        kind="wrist",
        frame=ref,
        label=VisionAnswer(text=text, call=ProviderCall(provider="perceptron", call="ocr")),
        catalog=dict(CATALOG if catalog is None else catalog),
    )
    return judge_label("0.1", LABEL, ans, "perceptron")


def test_judge_label_right_wrong_and_off_catalog_reads():
    assert _judge(HOT_LABEL).outcome == "pass"
    wrong = _judge(LIME_LABEL)  # another product sold in this store: a real wrong item
    assert wrong.outcome == "fail" and wrong.observed["read_sku"] == "LIMES"
    assert _judge(MILD_LABEL).outcome == "uncertain"  # shares 'salsa' with the expected name: partial read, as before
    nike = _judge(NIKE)  # resembles nothing sold here: the OCR made it up
    assert nike.outcome == "uncertain" and nike.observed["off_catalog"] is True
    assert "matches nothing sold in this store" in nike.reason and nike.confidence < LABEL.min_confidence
    assert _judge(NIKE, catalog={}).outcome == "fail"  # catalog unknown: a substantial mismatch still fails


def test_catalog_match_scores():
    assert catalog_match("Sol de Casa MILD CHUNKY SALSA", CATALOG)[0] == "SALSA-MILD"
    sku, score = catalog_match("Nike Air Max 270 React 10", CATALOG)
    assert score == 0.0
    assert catalog_match("", CATALOG) == (None, 0.0)


# ---- frame_trust helpers -----------------------------------------------------------------------------------------
def test_dark_fraction_separates_a_blocked_lens_from_a_dark_close_up():
    assert dark_fraction(_frame(_jpeg_b64(0.97))) >= DARK_FRAME_FRACTION  # the live frame: 97 % black
    assert dark_fraction(_frame(_jpeg_b64(0.89))) < DARK_FRAME_FRACTION  # black-label can close-up (measured 0.895)
    assert dark_fraction(_frame(evidence_mod.TINY_JPEG_B64)) == 0.0  # synthetic evidence frames are never dark
    assert dark_fraction(_frame("not-a-jpeg")) is None


def test_frame_mismatch_and_reuse_rules():
    ref = FrameRef(frame_id="f", camera_id=f"{ROBOT}.wrist", robot_id=ROBOT)
    ok = _frame(evidence_mod.TINY_JPEG_B64)
    assert frame_mismatch(ref, ok, ROBOT, STORE) is None
    assert "store store_hill" in (frame_mismatch(ref, ok.model_copy(update={"store_id": "store_hill"}), ROBOT, STORE))
    assert "camera" in frame_mismatch(ref, ok.model_copy(update={"camera_id": f"{ROBOT}.front"}), ROBOT, STORE)
    assert "robot" in frame_mismatch(ref, ok.model_copy(update={"robot_id": "rover-hill-1"}), ROBOT, STORE)
    reuse = FrameReuse()
    s1, s2 = ExecContext(plan_id="p", step_id="s1"), ExecContext(plan_id="p", step_id="s2")
    reuse.note([ref], s1, "pre")
    assert reuse.check(ref, s1, "pre") is None
    assert "pre-check" in reuse.check(ref, s1, "post")
    assert "reused" in reuse.check(ref, s2, "post")
    assert reuse.check(ref, None, "post") is None  # no step context: nothing to compare
    later = ref.model_copy(update={"ts": ref.ts + timedelta(seconds=5)})  # same id, new capture (worker restart)
    assert reuse.check(later, s2, "post") is None


# ---- verifier: fresh frames ----------------------------------------------------------------------------------------
async def test_fresh_frame_with_the_right_label_passes():
    provider = _provider(HOT_LABEL)
    _, _, v = _setup(provider)
    verdict = await v.verify(_req())
    label = _label_atom(verdict)
    assert verdict.outcome == "pass" and label.outcome == "pass" and label.decided_by == "provider"
    assert "ocr" in _kinds(provider)


async def test_fresh_frame_with_another_store_product_is_wrong():
    provider = _provider(LIME_LABEL)
    _, _, v = _setup(provider)
    verdict = await v.verify(_req())
    label = _label_atom(verdict)
    assert verdict.outcome == "fail" and label.outcome == "fail" and label.observed["read_sku"] == "LIMES"
    assert f"expected {EXPECT!r}" in verdict.reason


async def test_off_catalog_read_on_a_fresh_frame_is_uncertain_not_wrong():
    provider = _provider(NIKE)
    _, _, v = _setup(provider)
    verdict = await v.verify(_req())
    label = _label_atom(verdict)
    assert label.outcome == "uncertain" and "matches nothing sold" in label.reason
    assert verdict.outcome == "pass" and "Nike" not in verdict.reason  # advisory + uncertain: ignored


# ---- verifier: frames vision must not judge ------------------------------------------------------------------------
async def test_stale_frame_is_uncertain_never_wrong():
    provider = _provider(LIME_LABEL)  # the OCR would call it the wrong product if it were asked
    services, _, v = _setup(provider)
    [old] = await services.evidence.capture_refs(ROBOT, ["wrist"])  # type: ignore[union-attr]
    later = utcnow() + timedelta(seconds=1)  # re-captures are older than not_before too
    verdict = await v.verify(_req(frames=[old], not_before=later))
    assert "ocr" not in _kinds(provider)
    assert _label_atom(verdict).outcome == "uncertain" and verdict.outcome == "uncertain"
    assert verdict.reason.startswith("stale frame")


async def test_dark_frame_is_skipped_and_ground_truth_decides(monkeypatch: pytest.MonkeyPatch):
    """The live replay: black wrist frame, camera 'sees' an empty gripper, OCR reads Nike -> no put-back."""
    monkeypatch.setattr(evidence_mod, "TINY_JPEG_B64", _jpeg_b64(0.97))
    provider = _provider(NIKE, camera_sees_jar=False)
    _, _, v = _setup(provider)
    verdict = await v.verify(_req(context=ExecContext(plan_id="plan-goal_bu7gy5gm", step_id="s06")))
    assert provider.calls == []  # nothing sent to the vision model for a black frame
    assert verdict.outcome == "pass" and verdict.decided_by == "ground_truth" and verdict.degraded
    assert "vision skipped the wrist frame: it is dark" in verdict.reason
    assert "Nike" not in verdict.reason and "no fresh frame" not in verdict.reason
    assert _label_atom(verdict).outcome == "pass"  # sim truth: the held unit's label is the hot salsa


async def test_dark_frame_without_ground_truth_lets_robot_state_decide(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("arms.perception.tests.loop_world.TINY_JPEG_B64", _jpeg_b64(0.97))
    provider = _provider(NIKE, camera_sees_jar=False)
    services, _, v = _setup(provider)
    services.evidence = EvidenceRing(services)  # type: ignore[assignment]  # real frames: MiniRobot pixels

    class NoTruth:
        async def truth(self, robot_id, slot_ids=()):
            raise RuntimeError("isaac truth unavailable")

    services.truth = NoTruth()  # type: ignore[assignment]
    verdict = await v.verify(_req())
    assert provider.calls == []
    assert verdict.outcome == "pass" and verdict.decided_by == "state"
    assert _label_atom(verdict).outcome == "uncertain"  # camera-only: could not check (advisory: ignored)
    assert "vision skipped the wrist frame: it is dark" in verdict.reason
    assert "fell back to robot state" in verdict.reason


async def test_frame_from_another_store_is_not_judged():
    provider = _provider(LIME_LABEL)
    services, _, v = _setup(provider)
    ev = services.evidence
    foreign = _frame(evidence_mod.TINY_JPEG_B64, frame_id="hill-wrist-1", store_id="store_hill")
    ev._retain(foreign, None)  # type: ignore[union-attr]
    ref = FrameRef(frame_id="hill-wrist-1", camera_id=f"{ROBOT}.wrist", robot_id=ROBOT, ts=foreign.ts)
    verdict = await v.verify(_req(frames=[ref]))
    assert "ocr" not in _kinds(provider)
    assert _label_atom(verdict).outcome == "uncertain" and verdict.outcome != "fail"
    assert "it is from store store_hill, not store_ave" in verdict.reason


async def test_frame_from_another_camera_is_not_judged():
    provider = _provider(LIME_LABEL)
    services, _, v = _setup(provider)
    other = _frame(evidence_mod.TINY_JPEG_B64, frame_id="front-1", camera_id=f"{ROBOT}.front")
    services.evidence._retain(other, None)  # type: ignore[union-attr]
    ref = FrameRef(frame_id="front-1", camera_id=f"{ROBOT}.wrist", robot_id=ROBOT, ts=other.ts)
    verdict = await v.verify(_req(frames=[ref]))
    assert "ocr" not in _kinds(provider) and _label_atom(verdict).outcome == "uncertain"
    assert verdict.outcome != "fail" and f"it is from camera {ROBOT}.front" in verdict.reason


async def test_frame_reused_from_another_step_is_not_judged():
    provider = _provider(LIME_LABEL)
    services, _, v = _setup(provider)
    [ref] = await services.evidence.capture_refs(ROBOT, ["wrist"])  # type: ignore[union-attr]
    first = await v.verify(_req("a", frames=[ref], context=ExecContext(plan_id="p", step_id="s05")))
    assert _label_atom(first).outcome == "fail"  # judged once, on its own step
    n = len(provider.calls)
    again = await v.verify(_req("b", frames=[ref], context=ExecContext(plan_id="p", step_id="s06")))
    assert len(provider.calls) == n  # not sent to the vision model again
    assert _label_atom(again).outcome == "uncertain" and again.outcome != "fail"
    assert "reused frame" in again.reason


async def test_isaac_state_override_reason_says_so():
    """Camera says 'empty', Isaac state says holding (state wins): the reason no longer claims 'no fresh frame'."""
    provider = _provider(HOT_LABEL, camera_sees_jar=False)
    _, _, v = _setup(provider)
    verdict = await v.verify(_req())
    assert verdict.outcome == "pass"
    assert "camera overruled by the simulator's grasp state" in verdict.reason
    assert "no fresh frame" not in verdict.reason
