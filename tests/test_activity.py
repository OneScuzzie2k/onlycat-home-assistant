"""Tests for normalized OnlyCat activities and conservative location semantics."""

from datetime import UTC, datetime, timedelta

import pytest
from custom_components.onlycat.data.activity import OnlyCatActivity, friendly_activity
from custom_components.onlycat.data.event import (
    Event,
    EventClassification,
    EventTriggerSource,
)
from custom_components.onlycat.data.event_summary import EventSummary, SubEvent
from custom_components.onlycat.data.pet import Pet
from homeassistant.const import STATE_HOME, STATE_NOT_HOME


@pytest.mark.parametrize(
    ("action", "direction", "expected"),
    [
        ("TRANSIT", "INWARD", "Entered"),
        ("TRANSIT", "OUTWARD", "Exited"),
        ("PEEK", "INWARD", "Peeked inside"),
        ("PEEK", "OUTWARD", "Peeked outside"),
        ("DENY", "INWARD", "Denied entry"),
        ("DENY", "OUTWARD", "Denied exit"),
        ("BREACH", "INWARD", "Bypassed entry"),
        ("BREACH", "OUTWARD", "Bypassed exit"),
    ],
)
def test_friendly_activity_mapping(action: str, direction: str, expected: str) -> None:
    """Test all official action/direction names."""
    assert friendly_activity(action, direction) == expected


def _activity(
    action: str,
    direction: str,
    *,
    event_id: int = 10,
    timestamp: datetime | None = None,
) -> OnlyCatActivity:
    timestamp = timestamp or datetime(2026, 9, 24, 9, 40, tzinfo=UTC)
    event = Event(
        device_id="OC-1",
        event_id=event_id,
        timestamp=timestamp,
        frame_count=20,
        poster_frame_index=10,
        access_token="secret-media-token",
        event_classification=EventClassification.CLEAR,
        event_trigger_source=EventTriggerSource.OUTDOOR_MOTION,
    )
    summary = EventSummary(device_id="OC-1", event_id=event_id, timestamp=timestamp)
    subevent = SubEvent.from_api_response(
        {
            "startFrameIndex": 1,
            "endFrameIndex": 10,
            "rfidCode": "123",
            "action": action,
            "direction": direction,
        }
    )
    assert subevent is not None
    activity = OnlyCatActivity.from_parts(
        event=event,
        summary=summary,
        subevent=subevent,
        pet_name="Nelson",
    )
    assert activity is not None
    return activity


@pytest.mark.parametrize("action", ["TRANSIT", "BREACH"])
@pytest.mark.parametrize(
    ("direction", "expected"),
    [("INWARD", STATE_HOME), ("OUTWARD", STATE_NOT_HOME)],
)
def test_conclusive_activity_changes_location(
    action: str, direction: str, expected: str
) -> None:
    """Transit and breach use the authoritative destination direction."""
    pet = Pet("123", STATE_NOT_HOME if expected == STATE_HOME else STATE_HOME, None)

    changed, location_changed = pet.apply_activity(_activity(action, direction))

    assert changed is True
    assert location_changed is True
    assert pet.location == expected
    assert pet.location_source == "onlycat_activity"
    assert pet.location_source_event_id == 10


@pytest.mark.parametrize("action", ["PEEK", "DENY", "UNKNOWN"])
@pytest.mark.parametrize("direction", ["INWARD", "OUTWARD", "UNKNOWN"])
def test_inconclusive_or_unknown_activity_never_changes_location(
    action: str, direction: str
) -> None:
    """PEEK, DENY and malformed semantics update activity but not presence."""
    original_since = datetime(2026, 9, 1, tzinfo=UTC)
    pet = Pet(
        "123",
        STATE_HOME,
        original_since,
        location_changed_at=original_since,
    )

    changed, location_changed = pet.apply_activity(_activity(action, direction))

    assert changed is True
    assert location_changed is False
    assert pet.location == STATE_HOME
    assert pet.location_changed_at == original_since
    assert pet.last_seen == datetime(2026, 9, 24, 9, 40, tzinfo=UTC)
    assert pet.last_activity is not None


def test_older_activity_cannot_regress_location() -> None:
    """An out-of-order activity cannot overwrite newer location evidence."""
    newer = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    pet = Pet(
        "123",
        STATE_HOME,
        newer,
        location_changed_at=newer,
        location_evidence_at=newer,
    )

    changed, location_changed = pet.apply_activity(
        _activity("TRANSIT", "OUTWARD", timestamp=newer - timedelta(minutes=5))
    )

    assert changed is True
    assert location_changed is False
    assert pet.location == STATE_HOME
    assert pet.last_seen == newer


def test_activity_payload_contains_no_access_token() -> None:
    """Media credentials never enter Home Assistant event data."""
    payload = _activity("TRANSIT", "INWARD").as_event_data()

    assert payload["image_available"] is True
    assert payload["video_available"] is True
    assert "accessToken" not in payload
    assert "access_token" not in payload
    assert "secret-media-token" not in repr(payload)


def test_malformed_event_models_are_ignored_safely() -> None:
    """Test one malformed payload cannot break the activity stream."""
    assert Event.from_api_response(["bad"]) is None
    assert EventSummary.from_api_response(None) is None
    assert SubEvent.from_api_response(None) is None
