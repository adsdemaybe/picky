"""label_matches judge: template-only / partial OCR reads are uncertain, never a wrong-item fail."""

from arms.perception.judges import CameraAnswers, judge_label, label_content, label_overlap
from contracts.evidence import FrameRef
from contracts.grounding import ProviderCall
from contracts.plan import LabelMatches
from contracts.verify import VisionAnswer


def _judge(text: str, expect: str = "Mild Chunky Salsa 16 oz"):
    ref = FrameRef(frame_id="f1", camera_id="r1.wrist", url="/api/evidence/f1.jpg")
    ans = CameraAnswers(
        kind="wrist", frame=ref, label=VisionAnswer(text=text, call=ProviderCall(provider="perceptron", call="ocr"))
    )
    return judge_label("p", LabelMatches(sku="SALSA-MILD", expect_text=expect, advisory=True), ans, "perceptron")


def test_template_only_read_is_unreadable():
    assert label_content("Brand:  Product Name:  Size:") == ""
    assert _judge("Brand:  Product Name:  Size:").outcome == "uncertain"


def test_partial_read_passes_and_unrelated_read_fails():
    assert label_overlap("Mild Chunky Salsa 16 oz", "MILD CHUNKY SALSA") == 1.0
    assert _judge("Brand: Casa Verde Product Name: MILD CHUNKY SALSA Size:").outcome == "pass"
    assert _judge("Brand: Lakeview Product Name: Black Beans Size: 15 oz").outcome == "fail"
    assert _judge("Salsa").outcome == "uncertain"  # one word of three: not enough either way


def _basket(skus: list[str]) -> CameraAnswers:
    from contracts.grounding import Evidence, NormBox

    ref = FrameRef(frame_id="b1", camera_id="r1.basket", url="/api/evidence/b1.jpg")
    boxes = [
        Evidence(kind="box", role="basket_item", sku=s, box=NormBox(x1=10 * i, y1=10, x2=10 * i + 9, y2=20))
        for i, s in enumerate(skus)
    ]
    call = ProviderCall(provider="perceptron", call="detect")
    return CameraAnswers(kind="basket", frame=ref, items=VisionAnswer(boxes=boxes, call=call))


def test_basket_audit_seeing_a_subset_is_uncertain_not_missing():
    from arms.perception.judges import judge_basket_contains
    from contracts.plan import BasketContains

    want = BasketContains(counts={"SALSA-MILD": 1, "BEANS-BLACK": 2, "BEEF-GROUND": 2}, exact=True)
    assert judge_basket_contains("p", want, _basket(["SALSA-MILD", "BEANS-BLACK"]), "perceptron").outcome == "uncertain"
    full = _basket(["SALSA-MILD", "BEANS-BLACK", "BEANS-BLACK", "BEEF-GROUND", "BEEF-GROUND"])
    assert judge_basket_contains("p", want, full, "perceptron").outcome == "pass"
    foreign = _basket(["SALSA-MILD", "COLA"])
    assert judge_basket_contains("p", want, foreign, "perceptron").outcome == "fail"
