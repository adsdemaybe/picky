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
