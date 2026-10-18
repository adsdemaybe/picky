"""Deterministic judges: provider answers (``VisionAnswer``) -> ``AtomVerdict`` per camera atom.

The vision model only reports what it sees (boxes, points, yes/no, text); CODE decides pass / fail / uncertain here
(CLAUDE.md section 7: the model proposes, code decides). Rules (``verify.PERCEPTRON_CALLS``):

- gripper_holding / gripper_empty: two cues on the wrist frame. (1) a detected box over the jaws
  (``GRIPPER_ROI_WRIST``: IoU > ``HELD_ROI_MIN_IOU``, or at least half of the box inside the ROI, so a small lime held
  in the jaws counts) and (2) the answer to "is the gripper empty?". Both agree -> confidence 0.9 (may override robot
  state), one cue -> 0.7 (never overrides state), cues disagree -> uncertain. The held box's ``mention`` names the
  product (``match_mention_to_sku``; ambiguous = unknown, never a match).
- label_matches: OCR text contains ``expect_text`` (casefold) -> pass; a substantial read that names ANOTHER product
  sold in this store (``catalog_match`` over the store's catalog) -> fail (evidence = the text); a read that resembles
  nothing sold here ('Nike Air Max 270' in a supermarket: a hallucination off a dark / blocked frame) or nothing read
  -> uncertain, never a wrong-item fail.
- basket_delta / basket_contains: per-SKU box counts on the basket frame (before-count from the baseline's basket
  frame, else the baseline's state counts at lower confidence). Occlusion-aware: fewer units than expected in a basket
  that already holds others (delta), or a subset of the expected units with nothing foreign (contains), is uncertain
  (the robot state then decides), never a 'missing item' fail; an empty basket or a foreign / extra unit still fails.
- item_on_shelf: visible count >= min_count -> pass; fewer -> uncertain, never fail (back rows are hidden; the scan
  step is the out-of-stock authority). slot_visible: the slot's product in view -> pass, else uncertain.
- shelf_tag_reads: OCR -> ``parse_price_cents`` == expected cents.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from contracts.common import Sku
from contracts.evidence import FrameRef
from contracts.grounding import GRIPPER_ROI_WRIST, HELD_ROI_MIN_IOU, Evidence, VerifyMethod, box_iou
from contracts.plan import (
    Atom,
    BasketContains,
    BasketDelta,
    GripperEmpty,
    GripperHolding,
    ItemOnShelf,
    LabelMatches,
    ShelfTagReads,
    SlotVisible,
)
from contracts.verify import (
    AtomVerdict,
    VerifyBaseline,
    VisionAnswer,
    count_boxes_by_sku,
    match_mention_to_sku,
    parse_price_cents,
)

from .vision.common import answer_ok, overlap_fraction, parse_yes_no, yes_no_evidence

TWO_CUES_CONFIDENCE = 0.9
ONE_CUE_CONFIDENCE = 0.7
UNKNOWN_ITEM_CONFIDENCE = 0.65
"""Holding SOMETHING the model could not name: passes only together with robot state (below override)."""
HELD_IN_ROI_FRACTION = 0.5
COUNT_PASS_CONFIDENCE = 0.8
COUNT_FAIL_CONFIDENCE = 0.7
STATE_BEFORE_CONFIDENCE = 0.6
TEXT_MATCH_CONFIDENCE = 0.85
TEXT_MISMATCH_CONFIDENCE = 0.75
MIN_TEXT_CHARS = 3
LABEL_PASS_OVERLAP = 0.5
"""label_matches passes when at least this share of the expected name's significant words is read."""
LABEL_FAIL_MIN_TOKENS = 2
"""label_matches fails only on a read with at least this many significant words and none of the expected ones."""
CATALOG_MATCH_MIN = 0.5
"""A read resembles a catalog product when at least this share of the product's significant words is read, or this
share of the read's words is in the product's name."""
OFF_CATALOG_CONFIDENCE = 0.3
MIN_EMPTY_CONFIDENCE = 0.5
GENERIC_HELD_CLASS = "other grocery item"


@dataclass
class CameraAnswers:
    """Provider answers about ONE camera frame (None = not asked; ``call.error_code`` set = asked and failed)."""

    kind: str
    frame: FrameRef
    held: VisionAnswer | None = None
    empty: VisionAnswer | None = None
    label: VisionAnswer | None = None
    items: VisionAnswer | None = None
    items_before: VisionAnswer | None = None
    before_frame: FrameRef | None = None
    tag: VisionAnswer | None = None
    names: dict[Sku, str] = field(default_factory=dict)
    catalog: dict[Sku, str] = field(default_factory=dict)
    """SKU -> label words of every product sold in this store (label_matches: is a mismatching read a real product?).
    Empty = unknown: ``names`` stand in, and with neither a mismatching read fails as before."""


def _av(
    path: str,
    atom: Atom,
    outcome: str,
    reason: str,
    conf: float,
    method: VerifyMethod,
    evidence: list[Evidence] | None = None,
    observed: dict | None = None,
) -> AtomVerdict:
    return AtomVerdict(
        path=path,
        op=atom.op,
        outcome=outcome,  # type: ignore[arg-type]
        confidence=conf,
        reason=reason,
        decided_by="provider",
        method=method,
        evidence=evidence or [],
        observed=observed or {},
    )


def usable_count(ans: VisionAnswer | None) -> bool:
    """A detect answer good enough to COUNT from: it succeeded, and it either found boxes or confidently found none
    (an empty answer at low confidence = a blank / blocked view, not an empty basket)."""
    return answer_ok(ans) and ans is not None and (bool(ans.boxes) or ans.confidence >= MIN_EMPTY_CONFIDENCE)


def _with_frame(boxes: list[Evidence], frame: FrameRef, **upd: object) -> list[Evidence]:
    return [b.model_copy(update={"frame": frame, **upd}) for b in boxes]


def _match(mention: str, names: Mapping[Sku, str]) -> Sku | None:
    return match_mention_to_sku(mention, names)


# ---- gripper ---------------------------------------------------------------------------------------------------------
def held_boxes(ans: CameraAnswers) -> list[Evidence]:
    """Boxes over the gripper jaws (wrist: the ROI rule; other cameras: every box)."""
    if not answer_ok(ans.held) or ans.held is None:
        return []
    out = []
    for b in ans.held.boxes:
        if b.box is None:
            continue
        if ans.kind != "wrist":
            out.append(b)
        elif box_iou(b.box, GRIPPER_ROI_WRIST) > HELD_ROI_MIN_IOU or (
            overlap_fraction(b.box, GRIPPER_ROI_WRIST) >= HELD_IN_ROI_FRACTION
        ):
            out.append(b)
    return out


def gripper_reading(ans: CameraAnswers) -> tuple[str | None, float, Sku | None, list[Evidence], str]:
    """-> (state 'holding' | 'empty' | 'conflict' | None, confidence, held SKU, evidence, why)."""
    boxes = held_boxes(ans)
    cues: list[tuple[str, str]] = []
    evidence: list[Evidence] = []
    if answer_ok(ans.held):
        cues.append(("holding" if boxes else "empty", "a box over the jaws" if boxes else "no box over the jaws"))
    said: bool | None = None
    if answer_ok(ans.empty) and ans.empty is not None:
        said = ans.empty.yes if ans.empty.yes is not None else parse_yes_no(ans.empty.text)
        if said is not None:
            cues.append(("empty" if said else "holding", f"'gripper empty?' -> {'yes' if said else 'no'}"))
            evidence.append(
                Evidence(
                    kind="text",
                    role="held",
                    frame=ans.frame,
                    mention="gripper",
                    text=yes_no_evidence(ans.empty.text) or ("empty" if said else "holding"),
                    confidence=ans.empty.confidence,
                )
            )
    if not cues:
        return None, 0.0, None, evidence, "no usable camera answer"
    held_sku: Sku | None = None
    if boxes:
        best = max(boxes, key=lambda b: box_iou(b.box, GRIPPER_ROI_WRIST) if b.box else 0.0)
        held_sku = _match(best.mention, ans.names)
        evidence[:0] = _with_frame([best], ans.frame, role="held", sku=held_sku)
    if ans.kind == "wrist":
        evidence.append(
            Evidence(kind="box", role="gripper_roi", frame=ans.frame, box=GRIPPER_ROI_WRIST, mention="jaws")
        )
    states = {s for s, _ in cues}
    why = "; ".join(w for _, w in cues)
    if len(states) > 1:
        return "conflict", 0.5, held_sku, evidence, f"camera cues disagree ({why})"
    conf = TWO_CUES_CONFIDENCE if len(cues) >= 2 else ONE_CUE_CONFIDENCE
    return next(iter(states)), conf, held_sku, evidence, why


def judge_gripper(
    path: str, atom: GripperHolding | GripperEmpty, ans: CameraAnswers, method: VerifyMethod
) -> AtomVerdict | None:
    """Reason = one UI line ('Wrist camera: gripper is empty (expected Sol de Casa Mild Chunky Salsa)'); the cues
    behind it go to ``observed['cues']``."""
    state, conf, held_sku, evidence, why = gripper_reading(ans)
    if state is None:
        return None
    cam = f"{ans.kind.capitalize()} camera"
    label = ans.names.get(held_sku, held_sku) if held_sku else None
    observed = {"held_sku": held_sku if state == "holding" else None, "cues": why}

    def v(outcome: str, reason: str, c: float = conf) -> AtomVerdict:
        return _av(path, atom, outcome, reason, c, method, evidence, observed)

    if state == "conflict":
        return v("uncertain", f"{cam}: {why}")
    if isinstance(atom, GripperEmpty):
        if state == "empty":
            return v("pass", f"{cam}: gripper empty")
        return v("fail", f"{cam}: still holding {label or 'something'}")
    want = ans.names.get(atom.sku, atom.sku) if atom.sku else None
    if state == "empty":
        return v("fail", f"{cam}: gripper is empty" + (f" (expected {want})" if want else ""))
    if atom.sku is None or held_sku == atom.sku:
        return v("pass", f"{cam}: holding {label or want or 'the item'}")
    if held_sku is not None:
        return v("fail", f"{cam}: holding {label}, not {want}")
    return v("pass", f"{cam}: holding an item it could not name", min(conf, UNKNOWN_ITEM_CONFIDENCE))


# ---- label -----------------------------------------------------------------------------------------------------------
_LABEL_FIELD_RE = re.compile(
    r"\b(brand|product(?:\s+name)?|name|size|price|n/?a|none|unknown|not\s+visible)\b\s*:?", re.I
)
_UNIT_TOKENS = frozenset(
    {"oz", "fl", "lb", "lbs", "ml", "ct", "pk", "pack", "each", "g", "kg", "l", "in", "inch", "sq", "ft"}
)


def _label_tokens(text: str) -> set[str]:
    """Significant lowercase tokens of a label / product name (>= 3 chars, no units / pure numbers)."""
    toks = re.findall(r"[a-z0-9%]+", text.casefold())
    return {t for t in toks if len(t) >= 3 and t not in _UNIT_TOKENS and not t.isdigit()}


def label_content(text: str) -> str:
    """The OCR text without template field names: 'Brand:  Product Name:  Size:' (nothing read) -> ''."""
    return re.sub(r"[\s:;,.|/-]+", " ", _LABEL_FIELD_RE.sub(" ", text)).strip()


def label_overlap(expect: str, text: str) -> float:
    """Share of the expected name's significant tokens found in the text (1.0 = all of them)."""
    want = _label_tokens(expect)
    if not want:
        return 1.0 if expect.casefold() in text.casefold() else 0.0
    return len(want & _label_tokens(text)) / len(want)


def catalog_match(text: str, catalog: Mapping[Sku, str]) -> tuple[Sku | None, float]:
    """The store product an OCR read most resembles -> (sku, score); score = the larger of the share of the product's
    significant words found in the read and the share of the read's words found in the product's name."""
    read = _label_tokens(text)
    best: tuple[Sku | None, float] = (None, 0.0)
    if not read:
        return best
    for sku, name in catalog.items():
        want = _label_tokens(name)
        if not want:
            continue
        common = len(want & read)
        score = max(common / len(want), common / len(read))
        if score > best[1]:
            best = (sku, score)
    return best


def judge_label(path: str, atom: LabelMatches, ans: CameraAnswers, method: VerifyMethod) -> AtomVerdict | None:
    """Pass when the expected name is in the text, or at least half of its significant words are; FAIL only on a
    substantial read that shares none of them AND names a product sold in this store; anything in between (or a
    template-only / empty read, or a read that resembles nothing in the store's catalog) is uncertain. A partial read
    ('MILD CHUNKY SALSA' without the size), a cut-off label or a hallucinated one must not trigger a put-back."""
    if not answer_ok(ans.label) or ans.label is None:
        return None
    text = (ans.label.text or "").strip()
    content = label_content(text)
    if len(re.sub(r"[^0-9A-Za-z]", "", content)) < MIN_TEXT_CHARS:
        return _av(path, atom, "uncertain", "label unreadable", 0.3, method)
    overlap = label_overlap(atom.expect_text, content)
    ok = atom.expect_text.casefold() in text.casefold() or overlap >= LABEL_PASS_OVERLAP
    if not ok and (overlap > 0 or len(_label_tokens(content)) < LABEL_FAIL_MIN_TOKENS):
        return _av(path, atom, "uncertain", f"label partly read {text!r} (expected {atom.expect_text!r})", 0.4, method)
    ev = Evidence(
        kind="text", role="label", frame=ans.frame, text=text, sku=atom.sku if ok else None, matches_expectation=ok
    )
    if ok:
        return _av(path, atom, "pass", f"label reads {text!r}", TEXT_MATCH_CONFIDENCE, method, [ev], {"label": text})
    catalog = {**ans.names, **ans.catalog}
    read_sku, score = catalog_match(content, catalog) if catalog else (None, 1.0)
    if catalog and (read_sku is None or score < CATALOG_MATCH_MIN):
        # Nothing sold here looks like this read: the OCR saw no real label (dark / blocked frame, wrong camera) and
        # made one up. Uncertain -> robot state / ground truth decide; never a put-back on a hallucination.
        return _av(
            path,
            atom,
            "uncertain",
            f"label read {text!r} matches nothing sold in this store (unreliable read; expected {atom.expect_text!r})",
            OFF_CATALOG_CONFIDENCE,
            method,
            [ev.model_copy(update={"matches_expectation": None})],
            {"label": text, "off_catalog": True},
        )
    observed: dict[str, Any] = {"label": text}
    if read_sku is not None:
        observed["read_sku"] = read_sku
    return _av(
        path,
        atom,
        "fail",
        f"label reads {text!r}, expected {atom.expect_text!r}",
        TEXT_MISMATCH_CONFIDENCE,
        method,
        [ev],
        observed,
    )


# ---- shopper-visible words -------------------------------------------------------------------------------------------
def basket_words(counts: Mapping[Sku, int], names: Mapping[Sku, str]) -> str:
    """{'AVOCADO': 6, 'GUAC-KIT': 1} -> '6 Picky Farms Hass Avocado, 1 Fresh Guacamole Kit' (words, never a dict)."""
    parts = [f"{n} {names.get(s, s)}" for s, n in sorted(counts.items()) if n]
    return ", ".join(parts) if parts else "nothing"


_BASKET_DICT = r"(robot state: )?basket (\{[^{}]*\})"


def humanize_reason(text: str, names: Mapping[Sku, str]) -> str:
    """The contracts' state / truth evaluators write 'robot state: basket {'AVOCADO': 3}' and raw SKUs ('robot state:
    holding BEANS-BLACK'); the reason is shown to the shopper ('Not sure: ...'), so -> 'robot state says 3 Picky Farms
    Hass Avocado' / 'basket holds ...' and product names (one pass: inserted names are never rewritten)."""
    skus = "|".join(re.escape(s) for s in sorted((s for s in names if s), key=len, reverse=True))
    pattern = _BASKET_DICT + (rf"|(?<![\w-])({skus})(?![\w-])" if skus else "")

    def sub(m: re.Match[str]) -> str:
        if m.group(2) is None:
            return names[m.group(3)]
        try:
            counts = ast.literal_eval(m.group(2))
        except (ValueError, SyntaxError):
            return m.group(0)
        if not isinstance(counts, dict):
            return m.group(0)
        words = basket_words(counts, names)
        return f"robot state says {words}" if m.group(1) else f"basket holds {words}"

    return re.sub(pattern, sub, text)


def humanize_atoms(atoms: list[AtomVerdict], names: Mapping[Sku, str]) -> list[AtomVerdict]:
    return [a.model_copy(update={"reason": humanize_reason(a.reason, names)}) for a in atoms]


def unpair_reason(reason: str, state_reason: str) -> str:
    """``combine_verdicts``' disagreement line 'camera (A) vs state (B)' -> 'A; B' (one plain sentence)."""
    head, tail = "camera (", f") vs state ({state_reason})"
    if reason.startswith(head) and reason.endswith(tail):
        return f"{reason[len(head) : -len(tail)]}; {state_reason}"
    return reason


# ---- basket ----------------------------------------------------------------------------------------------------------
def _basket_counts(ans: CameraAnswers) -> tuple[dict[Sku, int] | None, list[Evidence]]:
    if not usable_count(ans.items) or ans.items is None:
        return None, []
    counts = count_boxes_by_sku(ans.items.boxes, ans.names)
    ev = []
    for b in ans.items.boxes:
        sku = b.sku or _match(b.mention, ans.names)
        ev.append(b.model_copy(update={"frame": ans.frame, "role": "basket_item", "sku": sku}))
    return counts, ev


def judge_basket_delta(
    path: str, atom: BasketDelta, ans: CameraAnswers, baseline: VerifyBaseline | None, method: VerifyMethod
) -> AtomVerdict | None:
    after, ev = _basket_counts(ans)
    if after is None:
        return None
    if usable_count(ans.items_before) and ans.items_before is not None:
        before = count_boxes_by_sku(ans.items_before.boxes, ans.names)
        src, pass_c, fail_c = "camera before/after", COUNT_PASS_CONFIDENCE, COUNT_FAIL_CONFIDENCE
    elif baseline is not None:
        before = dict(baseline.basket_counts)
        src, pass_c, fail_c = "camera after vs state before", STATE_BEFORE_CONFIDENCE, STATE_BEFORE_CONFIDENCE
    else:
        return _av(path, atom, "uncertain", "no baseline for a relative check", 0.0, method, ev)
    d = after.get(atom.sku, 0) - before.get(atom.sku, 0)
    ok = d == atom.delta
    name = ans.names.get(atom.sku, atom.sku)
    reason = f"camera: basket {d:+d} {name} (want {atom.delta:+d}; {src})"
    if not ok and 0 <= d < atom.delta and sum(before.values()) > 0:
        # A unit dropped into a basket that already holds others can land under / behind them: the top-down camera
        # seeing fewer is not evidence it is missing (measured on Isaac: '+0, want +1' on most places once the
        # basket had 2+ units). Uncertain -> the robot state decides (combine_verdicts), never a false 'missing'.
        return _av(path, atom, "uncertain", f"{reason}; may be hidden under the other units", 0.5, method, ev[:6])
    return _av(path, atom, "pass" if ok else "fail", reason, pass_c if ok else fail_c, method, ev[:6], {"delta": d})


def judge_basket_contains(
    path: str, atom: BasketContains, ans: CameraAnswers, method: VerifyMethod
) -> AtomVerdict | None:
    have, ev = _basket_counts(ans)
    if have is None:
        return None
    ok = all(have.get(s, 0) >= n for s, n in atom.counts.items())
    if atom.exact:
        ok = ok and {s: n for s, n in have.items() if n} == {s: n for s, n in atom.counts.items() if n}
    reason = f"camera sees {basket_words(have, ans.names)}"
    seen, want = sum(have.values()), sum(atom.counts.values())
    extra = any(n > atom.counts.get(s, 0) for s, n in have.items() if n) if atom.exact else False
    if not ok and not extra and 0 < seen < want and all(have.get(s, 0) <= n for s, n in atom.counts.items()):
        # Occlusion, not absence: a top-down camera sees the top layer of a full basket (Isaac taco trip: 2 of 11
        # units visible at the audit). Seeing a subset with nothing foreign is uncertain -> the robot state decides.
        why = f"{reason} ({seen} of {want} units visible; the rest are under / behind them)"
        return _av(path, atom, "uncertain", why, 0.5, method, ev[:6], {"basket": have})
    conf = COUNT_PASS_CONFIDENCE if ok else COUNT_FAIL_CONFIDENCE
    return _av(path, atom, "pass" if ok else "fail", reason, conf, method, ev[:6], {"basket": have})


# ---- shelf -----------------------------------------------------------------------------------------------------------
def judge_item_on_shelf(path: str, atom: ItemOnShelf, ans: CameraAnswers, method: VerifyMethod) -> AtomVerdict | None:
    if not usable_count(ans.items) or ans.items is None:
        return None
    counts = count_boxes_by_sku(ans.items.boxes, ans.names)
    n = counts.get(atom.sku, 0)
    ev = [
        b.model_copy(update={"frame": ans.frame, "role": "target", "sku": atom.sku})
        for b in ans.items.boxes
        if (b.sku or _match(b.mention, ans.names)) == atom.sku
    ][:6]
    name = ans.names.get(atom.sku, atom.sku)
    if n >= atom.min_count:
        return _av(path, atom, "pass", f"camera: {n} x {name} visible", COUNT_PASS_CONFIDENCE, method, ev, {"count": n})
    return _av(
        path, atom, "uncertain", f"camera: only {n} x {name} visible (back rows hidden)", 0.5, method, ev, {"count": n}
    )


def judge_item_on_counter(path: str, atom: Any, ans: CameraAnswers, method: VerifyMethod) -> AtomVerdict | None:
    """0.2.4 checkout unload: units of the SKU visible on the counter (front camera). Fewer -> uncertain (the arm or
    another unit may hide it): robot state decides then (combine_verdicts)."""
    if not usable_count(ans.items) or ans.items is None:
        return None
    counts = count_boxes_by_sku(ans.items.boxes, ans.names)
    n = counts.get(atom.sku, 0)
    ev = [
        b.model_copy(update={"frame": ans.frame, "role": "target", "sku": atom.sku})
        for b in ans.items.boxes
        if (b.sku or _match(b.mention, ans.names)) == atom.sku
    ][:6]
    name = ans.names.get(atom.sku, atom.sku)
    if n >= atom.min_count:
        return _av(
            path, atom, "pass", f"camera: {n} x {name} on the counter", COUNT_PASS_CONFIDENCE, method, ev, {"count": n}
        )
    return _av(path, atom, "uncertain", f"camera: {n} x {name} visible on the counter", 0.5, method, ev, {"count": n})


def judge_slot_visible(
    path: str, atom: SlotVisible, ans: CameraAnswers, slot_sku: Sku | None, method: VerifyMethod
) -> AtomVerdict | None:
    if not usable_count(ans.items) or ans.items is None:
        return None
    hits = [b for b in ans.items.boxes if slot_sku is None or (b.sku or _match(b.mention, ans.names)) == slot_sku]
    if hits:
        ev = _with_frame(hits[:4], ans.frame, role="slot")
        return _av(path, atom, "pass", "camera: the slot's product is in view", COUNT_PASS_CONFIDENCE, method, ev)
    return _av(path, atom, "uncertain", "camera: the slot's product is not visible", 0.4, method)


def judge_shelf_tag(path: str, atom: ShelfTagReads, ans: CameraAnswers, method: VerifyMethod) -> AtomVerdict | None:
    if not answer_ok(ans.tag) or ans.tag is None:
        return None
    text = (ans.tag.text or "").strip()
    cents = parse_price_cents(text)
    ev = [
        Evidence(kind="text", role="label", frame=ans.frame, text=text or None, matches_expectation=cents == atom.cents)
    ]
    if cents is None:
        return _av(path, atom, "uncertain", "shelf tag unreadable", 0.3, method, ev)
    ok = cents == atom.cents
    conf = TEXT_MATCH_CONFIDENCE if ok else TEXT_MISMATCH_CONFIDENCE
    return _av(path, atom, "pass" if ok else "fail", f"camera: tag reads {cents}c", conf, method, ev, {"cents": cents})


__all__ = [
    "GENERIC_HELD_CLASS",
    "CameraAnswers",
    "catalog_match",
    "gripper_reading",
    "held_boxes",
    "judge_basket_contains",
    "judge_basket_delta",
    "judge_gripper",
    "judge_item_on_shelf",
    "judge_label",
    "judge_shelf_tag",
    "judge_slot_visible",
]
