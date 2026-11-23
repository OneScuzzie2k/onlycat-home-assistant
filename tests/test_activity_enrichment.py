"""Tests for late journal enrichment and official classification semantics."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from custom_components.onlycat.data.activity import OnlyCatActivity
from custom_components.onlycat.data.activity_journal import (
    ActivityJournal,
    ActivityJournalEntry,
)
from custom_components.onlycat.data.event import Event, EventClassification
from custom_components.onlycat.data.event_summary import EventSummary, SubEvent

NOW = datetime.now(UTC)


def _activity(
    *,
    rfid: str = "123",
    pet: str | None = None,
    action: str = "TRANSIT",
    classification: str | None = None,
    automatic: str | None = None,
    manual: str | None = None,
    trigger: str | None = None,
    image: bool = False,
    video: bool = False,
) -> OnlyCatActivity:
    return OnlyCatActivity(
        device_id="OC-1",
        event_id=42,
        timestamp=NOW,
        rfid=rfid,
        pet=pet,
        action=action,
        direction="INWARD",
        activity="Entered" if action == "TRANSIT" else "Peeked inside",
        classification=classification,
        trigger_source=trigger,
        image_available=image,
        video_available=video,
        automatic_classification=automatic,
        manual_classification=manual,
    )


def _entry(activity: OnlyCatActivity) -> ActivityJournalEntry:
    return ActivityJournalEntry.from_activity(
        activity,
        received_at=NOW + timedelta(seconds=1),
        source="initial",
        location_before="not_home",
        location_after="home",
        location_changed=True,
        location_source="onlycat_activity",
    )


def test_late_metadata_enriches_one_row_without_mutating_semantics() -> None:
    """Late metadata fills the existing row while preserving movement/location."""
    entry = _entry(_activity())
    original = (
        entry.device_id,
        entry.event_id,
        entry.rfid,
        entry.action,
        entry.direction,
        entry.timestamp,
        entry.location_before,
        entry.location_after,
        entry.location_changed,
    )

    entry.merge_enrichment(
        _activity(
            pet="Nelson",
            classification="CONTRABAND",
            automatic="CONTRABAND",
            trigger="OUTDOOR_MOTION",
            image=True,
            video=True,
        ),
        source="get_event",
        received_at=NOW + timedelta(seconds=9),
    )

    assert entry.pet == "Nelson"
    assert entry.classification == "CONTRABAND"
    assert entry.automatic_classification == "CONTRABAND"
    assert entry.trigger_source == "OUTDOOR_MOTION"
    assert entry.image_available is True
    assert entry.video_available is True
    assert entry.duplicate_delivery_count == 1
    assert entry.delivery_sources == ["get_event", "initial"]
    assert entry.last_received_at == NOW + timedelta(seconds=9)
    assert original == (
        entry.device_id,
        entry.event_id,
        entry.rfid,
        entry.action,
        entry.direction,
        entry.timestamp,
        entry.location_before,
        entry.location_after,
        entry.location_changed,
    )


@pytest.mark.parametrize(
    ("automatic", "expected"),
    [
        (EventClassification.CLEAR, "CLEAR"),
        (EventClassification.CONTRABAND, "CONTRABAND"),
        (EventClassification.SUSPICIOUS, "SUSPICIOUS"),
    ],
)
def test_automatic_classification_is_effective(
    automatic: EventClassification, expected: str
) -> None:
    """Automatic classifications remain effective without a manual override."""
    event = Event(
        device_id="OC-1",
        event_id=42,
        timestamp=NOW,
        event_classification=automatic,
    )
    summary = EventSummary(device_id="OC-1", event_id=42, timestamp=NOW)
    subevent = SubEvent.from_api_response(
        {
            "startFrameIndex": 1,
            "endFrameIndex": 10,
            "rfidCode": "123",
            "action": "TRANSIT",
            "direction": "INWARD",
        }
    )
    activity = OnlyCatActivity.from_parts(
        event=event, summary=summary, subevent=subevent, pet_name="Nelson"
    )

    assert activity is not None
    assert activity.automatic_classification == expected
    assert activity.manual_classification is None
    assert activity.classification == expected


def test_manual_classification_overrides_automatic() -> None:
    """The official manual-over-automatic rule drives effective classification."""
    event = Event.from_api_response(
        {
            "deviceId": "OC-1",
            "eventId": 42,
            "eventClassification": 1,
            "eventManualClassification": 3,
            "eventManualClassificationUserId": 7,
        }
    )

    assert event is not None
    assert event.event_classification is EventClassification.CLEAR
    assert event.event_manual_classification is EventClassification.CONTRABAND
    assert event.event_manual_classification_user_id == 7
    assert event.effective_classification is EventClassification.CONTRABAND


def test_event_level_classification_is_propagated_across_groups() -> None:
    """Every RFID/action row for one physical event receives one classification."""
    journal = ActivityJournal()
    journal.append(_entry(_activity(rfid="123", action="PEEK")))
    journal.append(_entry(_activity(rfid="456", action="TRANSIT")))
    enriched = _activity(
        rfid="456",
        action="TRANSIT",
        classification="CONTRABAND",
        automatic="CLEAR",
        manual="CONTRABAND",
        video=True,
    )
    journal.get(enriched.dedup_key).merge_enrichment(
        enriched, source="get_event", received_at=NOW + timedelta(seconds=10)
    )
    journal.propagate_event_enrichment(enriched)
    journal.harmonize_event_enrichment("OC-1", 42)

    records = journal.records()
    assert len(records) == 2
    assert {item["classification"] for item in records} == {"CONTRABAND"}
    assert {item["automatic_classification"] for item in records} == {"CLEAR"}
    assert {item["manual_classification"] for item in records} == {"CONTRABAND"}
    assert all(item["video_available"] for item in records)
