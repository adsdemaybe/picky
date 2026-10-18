"""Perceptron Mk1.5 as a step-level reward model: yes / no questions about the overhead camera frame.

Used between phases of a pick to catch a missed grasp or a dropped object while the arm can still recover, and as
the reward signal for each decision the Q-learning makes (instead of only one human label at the end).

    python -m arms.robot.so101.judge holding gum last_lift.jpg     # try a question on a saved frame
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

import cv2
import numpy as np

QUESTIONS = {
    "holding": (
        "Look at the robot arm's gripper (the white two-finger claw hanging from above). Is the {target} held "
        "between the gripper's fingers and lifted off the table? Answer no if you can see the {target} resting on the "
        "table or plate instead. If the gripper or the {target} is hidden or unclear, answer unsure."
    ),
    "placed": (
        "Is the {target} resting on top of (inside the rim of) the {dest}? Answer no if it is on the table, still in "
        "the robot's gripper, hanging over the edge, or not visible."
    ),
}
ANSWER_FORMAT = (
    'Reply with JSON only: {"answer": "yes" | "no" | "unsure", "confidence": number from 0 to 1, '
    '"reason": "a short reason"}'
)


@dataclass
class Verdict:
    answer: bool | None  # None = unsure / no answer
    confidence: float
    reason: str

    def __str__(self) -> str:
        a = {True: "yes", False: "no", None: "unsure"}[self.answer]
        return f"{a} ({self.confidence:.2f}): {self.reason}"


def ask(
    frame_bgr: np.ndarray,
    check: str,
    target: str,
    dest: str = "",
    model: str = "perceptron-mk1.5",
    effort: str = "low",
) -> Verdict:
    """One of QUESTIONS about a frame. Never raises: an API problem comes back as unsure."""
    import tempfile

    from perceptron import Client, image

    question = QUESTIONS[check].format(target=target, dest=dest)
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        path = f.name
    cv2.imwrite(path, frame_bgr)
    try:
        client = Client(provider="perceptron", api_key=os.environ["PERCEPTRON_API_KEY"])
        r = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "user", "content": [image(path), {"type": "text", "text": f"{question}\n{ANSWER_FORMAT}"}]}
            ],
            reasoning_effort=effort,
            max_completion_tokens=1024,
        )
        return parse(r.text or "")
    except Exception as e:  # network / API: don't let the judge stop the arm
        return Verdict(None, 0.0, f"judge error: {e}")
    finally:
        os.unlink(path)


def parse(text: str) -> Verdict:
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            a = str(d.get("answer", "")).strip().lower()
            return Verdict(
                {"yes": True, "no": False}.get(a),
                float(d.get("confidence", 0.0) or 0.0),
                str(d.get("reason", "")).strip(),
            )
        except (ValueError, TypeError):
            pass
    low = text.strip().lower()
    if low.startswith("yes"):
        return Verdict(True, 0.5, text.strip()[:120])
    if low.startswith("no"):
        return Verdict(False, 0.5, text.strip()[:120])
    return Verdict(None, 0.0, text.strip()[:120] or "empty reply")


def main() -> None:
    from .config import RigConfig

    cfg = RigConfig()  # loads the API key from ~/.config/picky/env
    check, target, path = sys.argv[1:4]
    dest = sys.argv[4] if len(sys.argv) > 4 else "plate"
    print(ask(cv2.imread(path), check, target, dest, cfg.perceptron_model))


if __name__ == "__main__":
    main()
