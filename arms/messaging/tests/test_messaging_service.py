"""PhotonMessaging: parsing, dedupe, space learning (no network, no photonhq-api needed except where skipped)."""

import pytest

from arms.messaging.service import PhotonMessaging, parse_photon_message
from contracts.messaging import InboundMessage, OutboundMessage
from contracts.services import Services

from .conftest import MAYA_PHONE


def _raw(msg_id: str, text: str = "YES", ctype: str = "text") -> dict:
    return {
        "id": msg_id,
        "status": "received",
        "platform": "imessage",
        "createdAt": "2026-10-17T20:00:00.000Z",
        "content": {"type": ctype, "format": "plain", "text": text},
        "sentBy": {"id": "pho_usr_maya", "handle": "+1 (555) 555-0100"},
        "receivedBy": {"id": "pho_usr_agent"},
        "space": {"id": "pho_spc_1", "kind": "direct", "participantIds": ["pho_usr_maya", "pho_usr_agent"]},
    }


def test_parse_photon_message() -> None:
    p = parse_photon_message(_raw("m1", " tacos for 6 "))
    assert p is not None and p["text"] == "tacos for 6" and p["space_id"] == "pho_spc_1"
    assert p["agent_id"] == "pho_usr_agent"
    assert parse_photon_message(_raw("m2", ctype="reaction")) is None
    no_recv = _raw("m3")
    del no_recv["receivedBy"]
    assert parse_photon_message(no_recv)["agent_id"] == "pho_usr_agent"  # type: ignore[index]


async def test_handle_raw_dedupes_and_learns_space(services: Services) -> None:
    svc = PhotonMessaging(services, api_key="k", project_id="pho_prj_x")
    got: list[InboundMessage] = []

    async def handler(msg: InboundMessage) -> None:
        got.append(msg)

    svc.on_inbound(handler)
    first = await svc.handle_raw(_raw("m1"))
    assert await svc.handle_raw(_raw("m1")) is None  # at-least-once delivery -> dedupe
    assert first is not None and first.user_id == "maya" and first.handle == MAYA_PHONE
    assert got == [first]
    assert svc._space_by_handle[MAYA_PHONE] == "pho_spc_1" and svc._agent_id == "pho_usr_agent"


async def test_send_before_user_texts_first_degrades(services: Services) -> None:
    svc = PhotonMessaging(services, api_key="k", project_id="pho_prj_x")
    res = await svc.send(OutboundMessage(channel="photon", user_id="maya", text="hi"))
    assert not res.ok and res.error == "no_space_user_must_text_first"


async def test_send_uses_idempotency_key(services: Services) -> None:
    pytest.importorskip("photon_api")

    class FakeProjects:
        def __init__(self) -> None:
            self.calls: list = []

        async def send_message(self, req):  # type: ignore[no-untyped-def]
            self.calls.append(req)
            return {"id": "pho_msg_sent"}

    class FakeClient:
        projects = FakeProjects()

    svc = PhotonMessaging(services, api_key="k", project_id="pho_prj_x", client=FakeClient())
    await svc.handle_raw(_raw("m1"))
    res = await svc.send(OutboundMessage(channel="photon", user_id="maya", text="Add salsa?", approval_id="ap_1"))
    assert res.ok and res.provider_ref == "pho_msg_sent"
    req = FakeClient.projects.calls[0]
    assert req.headers.idempotency_key == "ap_1"
