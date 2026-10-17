"""Text formatting + parsing shared by the fake console channel and the real Photon service (base deps only).

Approval texts are plain SMS-style messages: a short question, an optional "why" line, then either
"Reply YES or NO" or numbered options ("Reply 1, 2 or 3"). Text YES/NO always works on every channel, so we never
depend on iMessage polls/tapbacks (docs/research.md §9.9).
"""

import re

from contracts.messaging import normalize_handle
from contracts.trust import ApprovalKind, ApprovalRequest
from utils.money import fmt_cents

YES_RE = re.compile(r"^\s*(y|yes|yeah|yep|yup|sure|ok|okay|approve|approved|do it|go)\b", re.IGNORECASE)
NO_RE = re.compile(r"^\s*(n|no|nope|nah|skip|reject|cancel|stop)\b", re.IGNORECASE)
NUM_RE = re.compile(r"\b([1-9])\b")


def mask_handle(handle: str | None) -> str:
    """Never log full phone numbers (docs/research.md §9.9): '+15555550100' -> '***0100'."""
    if not handle:
        return "<none>"
    h = normalize_handle(handle)
    if "@" in h:
        name, _, domain = h.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"***{h[-4:]}" if len(h) > 4 else "***"


def approval_quick_replies(request: ApprovalRequest) -> list[str]:
    if request.options:
        return [str(i) for i in range(1, len(request.options) + 1)]
    return ["YES", "NO"]


def format_approval(request: ApprovalRequest) -> str:
    """Render an ApprovalRequest as one text message."""
    lines = [request.title.strip()]
    if request.body.strip():
        lines.append(request.body.strip())
    if request.amount_cents is not None and request.kind != ApprovalKind.PICK_CHOICE:
        lines.append(f"Amount: {fmt_cents(request.amount_cents)}")
    if request.options:
        for i, opt in enumerate(request.options, start=1):
            detail = f" ({opt.detail})" if opt.detail else ""
            lines.append(f"{i}. {opt.label}{detail}")
        n = len(request.options)
        nums = ", ".join(str(i) for i in range(1, n)) + f" or {n}" if n > 1 else "1"
        if request.multi:
            lines.append(f"Reply with numbers (e.g. 1 and 3), or NO for none. Options: {nums}.")
        else:
            lines.append(f"Reply {nums}.")
    else:
        lines.append("Reply YES or NO.")
    return "\n".join(lines)


def option_id_for_reply(request: ApprovalRequest, number: int) -> str | None:
    """'2' -> the option_id of the 2nd option (1-based), or None if out of range."""
    if 1 <= number <= len(request.options):
        return request.options[number - 1].option_id
    return None


def parse_yes_no(text: str) -> bool | None:
    """True for yes-ish, False for no-ish, None if neither (the agent's interpret_reply handles the rest)."""
    if YES_RE.match(text):
        return True
    if NO_RE.match(text):
        return False
    return None


def parse_numbers(text: str) -> list[int]:
    """'1 and 3' -> [1, 3] (order kept, deduped)."""
    out: list[int] = []
    for m in NUM_RE.finditer(text):
        n = int(m.group(1))
        if n not in out:
            out.append(n)
    return out
