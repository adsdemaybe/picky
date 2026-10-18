"""Shopper-visible verifier reasons are words with product names, never Python dicts or raw SKUs (live Isaac trip
trip_10qstaik: "Not sure: camera (camera: basket {'AVOCADO': 6, 'GUAC-KIT': 1}) vs state (robot state: basket
{'AVOCADO': 3, 'COFFEE': 1, 'GUAC-KIT': 1}); asking the shopper")."""

from arms.perception.judges import (
    CameraAnswers,
    basket_words,
    humanize_reason,
    judge_basket_contains,
    unpair_reason,
)
from contracts.evidence import FrameRef
from contracts.grounding import Evidence, NormBox, ProviderCall
from contracts.plan import BasketContains
from contracts.verify import AtomVerdict, VisionAnswer, combine_verdicts

NAMES = {
    "AVOCADO": "Hass Avocado",
    "COFFEE": "Medium Roast Ground Coffee 12 oz",
    "GUAC-KIT": "Fresh Guacamole Kit",
    "BEANS-BLACK": "Black Beans 15 oz",
}
STATE = "robot state says 3 Hass Avocado, 1 Medium Roast Ground Coffee 12 oz, 1 Fresh Guacamole Kit"


def test_basket_words():
    assert basket_words({"GUAC-KIT": 1, "AVOCADO": 6, "COFFEE": 0}, NAMES) == "6 Hass Avocado, 1 Fresh Guacamole Kit"
    assert basket_words({}, NAMES) == "nothing"
    assert basket_words({"MYSTERY": 2}, NAMES) == "2 MYSTERY"


def test_state_and_truth_reasons_become_words():
    raw = "robot state: basket {'AVOCADO': 3, 'COFFEE': 1, 'GUAC-KIT': 1}"
    assert humanize_reason(raw, NAMES) == STATE
    assert humanize_reason("basket {'AVOCADO': 2}", NAMES) == "basket holds 2 Hass Avocado"
    assert humanize_reason("robot state: basket {}", NAMES) == "robot state says nothing"
    assert humanize_reason("robot state: basket -1 AVOCADO", NAMES) == "robot state: basket -1 Hass Avocado"
    assert humanize_reason("robot state: holding BEANS-BLACK", NAMES) == "robot state: holding Black Beans 15 oz"
    assert humanize_reason("robot state: gripper empty", NAMES) == "robot state: gripper empty"


def test_camera_basket_audit_and_disagreement_read_as_one_sentence():
    ref = FrameRef(frame_id="b1", camera_id="r1.basket", url="/api/evidence/b1.jpg")
    skus = ["AVOCADO"] * 6 + ["GUAC-KIT"]
    boxes = [
        Evidence(kind="box", role="basket_item", sku=s, box=NormBox(x1=10 * i, y1=10, x2=10 * i + 9, y2=20))
        for i, s in enumerate(skus)
    ]
    call = ProviderCall(provider="perceptron", call="detect")
    ans = CameraAnswers(kind="basket", frame=ref, items=VisionAnswer(boxes=boxes, call=call), names=NAMES)
    atom = BasketContains(counts={"AVOCADO": 3, "COFFEE": 1, "GUAC-KIT": 1}, exact=True)
    vis = judge_basket_contains("0", atom, ans, "perceptron")
    assert vis is not None and vis.outcome == "fail"
    assert vis.reason == "camera sees 6 Hass Avocado, 1 Fresh Guacamole Kit"
    st = AtomVerdict(path="0", op="basket_contains", outcome="pass", confidence=0.8, reason=STATE)
    both = combine_verdicts(st, vis)
    assert both.outcome == "uncertain"
    assert unpair_reason(both.reason, st.reason) == f"camera sees 6 Hass Avocado, 1 Fresh Guacamole Kit; {STATE}"
    assert unpair_reason("ok", st.reason) == "ok"
