"""FakeMessaging: Protocol conformance + the demo scenario (approvals, follow-up feedback)."""

import pytest

from arms.messaging import create, create_fake
from arms.messaging.fake import DEFAULT_FEEDBACK_TEXT, FakeMessaging, scripted_reply
from arms.messaging.format import format_approval, mask_handle, parse_numbers, parse_yes_no
from arms.messaging.service import MessagingNotConfigured, PhotonMessaging
from contracts.events import AgentMessage
from contracts.messaging import InboundMessage, OutboundMessage
from contracts.services import MessagingService, Services
from contracts.trust import ApprovalKind, ApprovalOption, ApprovalRequest
from utils.config import reset_settings, set_settings

from .conftest import MAYA_PHONE


def _rec(approval_id: str, interest: float | None) -> ApprovalRequest:
    payload = {} if interest is None else {"interest": interest}
    return ApprovalRequest(
        approval_id=approval_id,
        trip_id="trip_1",
        user_id="maya",
        kind=ApprovalKind.RECOMMENDATION,
        title="Add Cholula hot sauce? You liked spicy salsa",
        amount_cents=499,
        sku="hot-sauce-cholula",
        payload=payload,
    )


def _checkout() -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="ap_checkout",
        trip_id="trip_1",
        user_id="maya",
        kind=ApprovalKind.CHECKOUT,
        title="Pay $38.12 for 14 items?",
        amount_cents=3812,
        cart_hash="abc123",
    )


def _pick() -> ApprovalRequest:
    return ApprovalRequest(
        approval_id="ap_pick",
        trip_id="trip_1",
        user_id="maya",
        kind=ApprovalKind.PICK_CHOICE,
        title="Which avocado?",
        options=[ApprovalOption(option_id=f"inst_{i}", label=f"Avocado #{i}") for i in (7, 3, 9)],
    )


def test_conformance() -> None:
    assert isinstance(create_fake(Services()), MessagingService)
    assert isinstance(PhotonMessaging(Services()), MessagingService)  # real constructs without the extra / keys


def test_create_preflight_raises_when_not_configured() -> None:
    """create() refuses without the extra or keys, so the registry falls back to the fake (and /health says why)."""
    set_settings(photon_api_key="", photon_project_id="")
    try:
        with pytest.raises(MessagingNotConfigured):
            create(Services())
    finally:
        reset_settings()


def test_scripted_policy() -> None:
    assert scripted_reply(_rec("a", 0.8)) == "YES"
    assert scripted_reply(_rec("a", 0.6)) == "YES"
    assert scripted_reply(_rec("a", 0.4)) == "NO"
    assert scripted_reply(_rec("a", None)) == "YES"
    assert scripted_reply(_checkout()) == "YES"
    over = _rec("a", 0.9).model_copy(update={"payload": {"interest": 0.9, "over_budget": True}})
    assert scripted_reply(over) == "NO", "she sticks to her budget"
    above = _checkout().model_copy(update={"kind": ApprovalKind.OVER_THRESHOLD, "cart_hash": None})
    assert scripted_reply(above.model_copy(update={"payload": {"reasons": ["above_ask_threshold"]}})) == "YES"
    assert scripted_reply(above.model_copy(update={"payload": {"reasons": ["over_budget"]}})) == "NO"
    assert scripted_reply(_pick()) == "1"


def test_format_and_parse() -> None:
    text = format_approval(_rec("a", 0.8))
    assert "Reply YES or NO." in text and "$4.99" in text
    pick = format_approval(_pick())
    assert "1. Avocado #7" in pick and "Reply 1, 2 or 3." in pick
    assert parse_yes_no("Yes please") is True and parse_yes_no("nah") is False and parse_yes_no("maybe") is None
    assert parse_numbers("1 and 3") == [1, 3]
    assert mask_handle(MAYA_PHONE) == "***0100"


async def test_send_publishes_agent_message(services: Services) -> None:
    m = FakeMessaging(services, reply_delay_s=0)
    res = await m.send(OutboundMessage(channel="photon", user_id="maya", text="List ready: 14 items", trip_id="t"))
    assert res.ok and res.message_id and res.channel == "photon"
    assert m.transcript[-1].handle == MAYA_PHONE
    ev = services.events.events[-1]  # type: ignore[union-attr]
    assert isinstance(ev, AgentMessage) and ev.channel == "photon" and ev.text == "List ready: 14 items"


async def test_approvals_delivered_as_inbound_texts(services: Services) -> None:
    m = FakeMessaging(services, reply_delay_s=0)
    got: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        got.append(msg)

    m.on_inbound(handler)
    await m.start()
    sent = [await m.send_approval_request(r) for r in (_rec("ap_hot", 0.8), _rec("ap_kale", 0.3), _checkout(), _pick())]
    await m.wait_idle()
    await m.stop()
    by_reply = {g.in_reply_to: g for g in got}
    assert [by_reply[s.message_id].text for s in sent] == ["YES", "NO", "YES", "1"]
    assert all(g.user_id == "maya" and g.handle == MAYA_PHONE and g.channel == "photon" for g in got)
    assert services.approvals.responses == []  # type: ignore[union-attr]  # the agent resolves, not us


async def test_resolve_mode_without_handler(services: Services) -> None:
    m = FakeMessaging(services, reply_delay_s=0)
    for r in (_rec("ap_hot", 0.9), _rec("ap_kale", 0.1), _checkout(), _pick()):
        await m.send_approval_request(r)
    await m.wait_idle()
    resp = {r.approval_id: r for r in services.approvals.responses}  # type: ignore[union-attr]
    assert resp["ap_hot"].decision == "approve" and resp["ap_hot"].channel == "photon"
    assert resp["ap_kale"].decision == "reject"
    assert resp["ap_checkout"].approved and resp["ap_checkout"].cart_hash == "abc123"
    assert resp["ap_pick"].decision == "choice" and resp["ap_pick"].choice == "inst_7"


async def test_followup_gets_scripted_feedback_once(services: Services) -> None:
    m = FakeMessaging(services, reply_delay_s=0)
    got: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        got.append(msg)

    m.on_inbound(handler)
    follow = OutboundMessage(
        channel="photon", user_id="maya", trip_id="trip_1", text="How was the salsa? Reply 1-5.", about_skus=["salsa"]
    )
    res = await m.send(follow)
    await m.send(follow)  # second follow-up for the same trip: no second scripted reply
    await m.wait_idle()
    assert len(got) == 1
    assert got[0].text == DEFAULT_FEEDBACK_TEXT and got[0].in_reply_to == res.message_id and got[0].user_id == "maya"


async def test_reply_mode_off_and_inject(services: Services) -> None:
    m = FakeMessaging(services, reply_delay_s=0, reply_mode="off")
    got: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        got.append(msg)

    m.on_inbound(handler)
    await m.send_approval_request(_checkout())
    await m.wait_idle()
    assert got == []
    msg = await m.inject("tacos for 6, under $40, one vegetarian", handle="(555) 555-0100")
    assert msg.user_id == "maya" and got == [msg]


async def test_fixture_fallback_resolves_demo_user() -> None:
    from utils.data import DataError, load_demo_fixtures

    try:
        users = load_demo_fixtures().users
    except DataError:
        pytest.skip("data/fixtures/users.yaml not written yet (data agent)")
    u = next((u for u in users if u.profile.phone), None)
    if u is None:
        pytest.skip("no demo user with a phone in fixtures")
    m = FakeMessaging(Services(), reply_delay_s=0)  # no recommender -> fixtures fallback
    msg = await m.inject("ping", handle=u.profile.phone)
    assert msg.user_id == u.user_id
