"""0.2.5 Cart Whisperer contracts: cards, responses, decisions, the whisper.cards event, Trip.whisper."""

import json
from datetime import date

import pytest
from pydantic import ValidationError

import contracts
from contracts.events import EVENT_PRODUCERS, WhisperCardsEvent, event_json_schema, make_event, parse_event
from contracts.services import RecommenderService
from contracts.shopping import Cart
from contracts.sim import versions_compatible
from contracts.trip import Trip
from contracts.whisper import (
    WHISPER_DEFAULT_ACTION,
    WHISPER_KINDS,
    WHISPER_MAX_CARDS,
    WHISPER_REASON_CODES,
    ShopperContext,
    WhisperCard,
    WhisperDecisionResult,
    WhisperReminder,
    WhisperResponse,
    WhisperSignal,
    whisper_card_id,
    whisper_transparency_entry,
)


def _card(**kw: object) -> WhisperCard:
    base: dict[str, object] = {
        "card_id": whisper_card_id("skip", "CHG-20W"),
        "kind": "skip",
        "action": "skip",
        "user_id": "maya",
        "store_id": "store_tmobile",
        "sku": "CHG-20W",
        "name": "Samsung 25W Power Adapter",
        "title": "Skip the charger",
        "qty": 0,
        "price_cents": 1999,
        "avoided_cents": 1999,
        "reason": "You already own a USB-C charger (bought Mar 14) and it works with a Pixel 9.",
        "reason_codes": ["ALREADY_OWNED"],
        "signals": [
            WhisperSignal(name="owned_since", value="2026-03-14", source="customer", label="You bought one on Mar 14")
        ],
        "score": 0.8,
    }
    base.update(kw)
    return WhisperCard.model_validate(base)


def test_patch_bump_keeps_deployed_isaac_workers_connecting() -> None:
    assert contracts.CONTRACTS_VERSION == "0.2.5"
    for worker in ("0.2.0", "0.2.4"):
        assert versions_compatible(worker, contracts.CONTRACTS_VERSION)


def test_kinds_actions_and_codes() -> None:
    assert set(WHISPER_KINDS) == set(WHISPER_DEFAULT_ACTION)
    assert WHISPER_DEFAULT_ACTION["skip"] == "skip" and WHISPER_DEFAULT_ACTION["wait"] == "remind"
    assert {"ALREADY_OWNED", "PROMO_SOON", "INCOMPATIBLE", "ALLERGEN"} <= set(WHISPER_REASON_CODES)
    assert WHISPER_MAX_CARDS == 3


def test_card_ids_are_stable() -> None:
    assert whisper_card_id("skip", "CHG-20W") == "cw-skip-CHG-20W"
    assert whisper_card_id("swap_save", "SOYRIZO", "BEEF-GROUND") == "cw-swap_save-SOYRIZO-for-BEEF-GROUND"
    assert whisper_card_id("qty", "TORT-CORN", "TORT-CORN") == "cw-qty-TORT-CORN"


def test_card_validation() -> None:
    card = _card()
    assert card.sponsored is False and card.phrased_by == "template" and card.product_sku == "CHG-20W"
    with pytest.raises(ValidationError):
        _card(reason_codes=[])  # every card carries at least one deterministic reason code
    with pytest.raises(ValidationError):
        _card(reason_codes=["MADE_UP"])
    with pytest.raises(ValidationError):
        _card(reason="")
    with pytest.raises(ValidationError):
        _card(sku=None, line_sku=None)
    with pytest.raises(ValidationError):
        _card(kind="wait", action="remind", card_id="cw-wait-BUDS-RUN")  # WAIT needs remind_on
    wait = _card(
        kind="wait", action="remind", card_id="cw-wait-BUDS-RUN", remind_on=date(2026, 10, 23), promo_price_cents=13999
    )
    assert wait.remind_on == date(2026, 10, 23)
    with pytest.raises(ValidationError):
        _card(score=1.5)


def test_response_caps_cards_and_allows_zero() -> None:
    empty = WhisperResponse(user_id="maya", store_id="store_ave", phase="checkout", empty_reason="Nothing today.")
    assert empty.cards == [] and empty.empty_reason
    three = WhisperResponse(user_id="maya", store_id="store_ave", phase="planning", cards=[_card()] * 3)
    assert len(three.cards) == 3
    with pytest.raises(ValidationError):
        WhisperResponse(user_id="maya", store_id="store_ave", phase="planning", cards=[_card()] * 4)


def test_event_round_trip_and_producer() -> None:
    ev = make_event("whisper.cards", trip_id="t1", user_id="maya", phase="checkout", cards=[_card()], source="gateway")
    assert isinstance(ev, WhisperCardsEvent)
    back = parse_event(ev.model_dump_json())
    assert isinstance(back, WhisperCardsEvent) and back.cards[0].card_id == "cw-skip-CHG-20W"
    empty = parse_event({"type": "whisper.cards", "phase": "in_trip", "cards": [], "empty_reason": "Nothing"})
    assert isinstance(empty, WhisperCardsEvent) and empty.cards == []
    assert EVENT_PRODUCERS["whisper.cards"] == "gateway"
    assert "whisper.cards" in json.dumps(event_json_schema())


def test_trip_snapshot_carries_the_latest_pass() -> None:
    trip = Trip(trip_id="t1", user_id="maya", store_id="store_ave", cart=Cart(cart_id="c1"))
    assert trip.whisper is None
    trip.whisper = WhisperResponse(user_id="maya", store_id="store_ave", phase="planning", cards=[_card()])
    back = Trip.model_validate_json(trip.model_dump_json())
    assert back.whisper is not None and back.whisper.cards[0].kind == "skip"


def test_decision_result_and_reminder() -> None:
    reminder = WhisperReminder(
        reminder_id="rem1",
        user_id="maya",
        card_id="cw-wait-BUDS-RUN",
        sku="BUDS-RUN",
        name="Galaxy Buds4",
        store_id="store_tmobile",
        remind_on=date(2026, 10, 23),
        price_cents=13999,
        was_cents=17999,
        message="I'll text you Fri Oct 23 when it drops to $139.99.",
    )
    res = WhisperDecisionResult(
        card_id="cw-wait-BUDS-RUN", decision="accept", outcome="reminder_scheduled", reminder=reminder
    )
    assert WhisperDecisionResult.model_validate_json(res.model_dump_json()).reminder == reminder
    with pytest.raises(ValidationError):
        WhisperDecisionResult(card_id="x", decision="accept", outcome="maybe")  # type: ignore[arg-type]


def test_transparency_entry_for_a_card() -> None:
    entry = whisper_transparency_entry(_card(trip_id="t1"), outcome="declined")
    assert entry.kind == "whisper" and entry.rec_id == "cw-skip-CHG-20W@t1" and entry.outcome == "declined"
    assert entry.signals["reason_codes"] == "ALREADY_OWNED" and entry.signals["owned_since"] == "2026-03-14"
    assert entry.sponsored is False


def test_shopper_context_is_strict_and_server_side() -> None:
    ctx = ShopperContext(user_id="maya", store_id="store_ave", phase="in_trip", last_picked_sku="TORT-CORN")
    assert ctx.answered_card_ids == [] and ctx.remaining_stops == []
    with pytest.raises(ValidationError):
        ShopperContext.model_validate({"user_id": "maya", "store_id": "store_ave", "card_number": "4242"})


def test_recommender_protocol_has_whisper() -> None:
    assert hasattr(RecommenderService, "whisper") and hasattr(RecommenderService, "whisper_feedback")
