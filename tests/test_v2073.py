"""Tests for the OnlyCat 2.0.7.3 activity journal and RFID cleanup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol
from custom_components.onlycat.api import OnlyCatConnectionState
from custom_components.onlycat.data.activity import OnlyCatActivity
from custom_components.onlycat.data.activity_journal import (
    ActivityJournal,
    ActivityJournalEntry,
    ActivityStore,
)
from custom_components.onlycat.data.device import Device
from custom_components.onlycat.data.event import (
    Event,
    EventClassification,
    EventTriggerSource,
)
from custom_components.onlycat.data.event_store import EventStore
from custom_components.onlycat.device_tracker import async_setup_entry as setup_trackers
from custom_components.onlycat.sensor_recent_activity import (
    OnlyCatRecentActivitySensor,
)
from custom_components.onlycat.websocket_api import websocket_activity_journal
from homeassistant.const import STATE_HOME, STATE_NOT_HOME

NOW = datetime.now(UTC)


def _activity(
    event_id: int,
    *,
    pet: str | None = "Nelson",
    rfid: str = "123",
    action: str = "TRANSIT",
    direction: str = "INWARD",
    timestamp: datetime = NOW,
) -> OnlyCatActivity:
    return OnlyCatActivity(
        device_id="OC-1",
        event_id=event_id,
        timestamp=timestamp,
        rfid=rfid,
        pet=pet,
        action=action,
        direction=direction,
        activity="Entered",
        classification="CLEAR",
        trigger_source="OUTDOOR_MOTION",
        image_available=True,
        video_available=True,
    )


def _entry(
    event_id: int,
    *,
    timestamp: datetime = NOW,
    pet: str | None = "Nelson",
    rfid: str = "123",
) -> ActivityJournalEntry:
    return ActivityJournalEntry.from_activity(
        _activity(event_id, pet=pet, rfid=rfid, timestamp=timestamp),
        received_at=timestamp + timedelta(seconds=2),
        source="event_summary_update",
        location_before="not_home",
        location_after="home",
        location_changed=True,
        location_source="onlycat_activity",
    )


def _live_store(*, profile: object = None) -> tuple[EventStore, Mock, AsyncMock]:
    client = AsyncMock()
    client.connection_state = OnlyCatConnectionState(True, "connect")
    client.send_message.return_value = profile
    async_fire = Mock()
    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=async_fire))
    store = EventStore(client, hass=hass)
    store.enable_activity_emission()
    return store, async_fire, client


def _set_event(store: EventStore, event_id: int = 20619) -> None:
    store._current_events["OC-1"] = Event(
        device_id="OC-1",
        event_id=event_id,
        timestamp=NOW,
        frame_count=20,
        poster_frame_index=10,
        access_token="secret-media-token",
        event_classification=EventClassification.CLEAR,
        event_trigger_source=EventTriggerSource.OUTDOOR_MOTION,
    )


def _set_event_without_frame_count(
    store: EventStore,
    event_id: int,
    *,
    poster_frame_index: int | None = None,
    access_token: str | None = None,
) -> None:
    """Set a semantically valid event whose optional media data is incomplete."""
    store._current_events["OC-1"] = Event(
        device_id="OC-1",
        event_id=event_id,
        timestamp=NOW,
        frame_count=None,
        poster_frame_index=poster_frame_index,
        access_token=access_token,
        event_classification=EventClassification.CLEAR,
        event_trigger_source=EventTriggerSource.OUTDOOR_MOTION,
    )


def _summary(event_id: int = 20619, rfid: str = "123") -> dict:
    return {
        "deviceId": "OC-1",
        "eventId": event_id,
        "timestamp": NOW.isoformat(),
        "subevents": [
            {
                "startFrameIndex": 1,
                "endFrameIndex": 10,
                "rfidCode": rfid,
                "action": "TRANSIT",
                "direction": "INWARD",
            }
        ],
    }


def _semantic_summary(
    event_id: int,
    *,
    rfid: str = "123",
    action: str = "TRANSIT",
    direction: str = "INWARD",
) -> dict:
    """Build a complete semantic activity summary independent of media metadata."""
    summary = _summary(event_id=event_id, rfid=rfid)
    summary["subevents"][0]["action"] = action
    summary["subevents"][0]["direction"] = direction
    return summary


@pytest.mark.asyncio
async def test_transit_without_frame_count_advances_watermark_and_emits() -> None:
    """A new semantic activity emits even if its event has no frame count."""
    store, async_fire, _ = _live_store()
    rfid = "978101082885370"
    pet = store.get_pet_by_rfid(rfid, "OC-1")
    pet.label = "Ron"
    pet.location = STATE_NOT_HOME
    store._bootstrap_watermarks["OC-1"] = 20624
    store._last_event_ids["OC-1"] = 20625
    sensor = OnlyCatRecentActivitySensor(Device("OC-1", "OnlyCat"), store)
    sensor.async_write_ha_state = Mock()
    store.add_activity_listener("OC-1", sensor._handle_activity)
    _set_event_without_frame_count(
        store,
        20625,
        poster_frame_index=10,
        access_token="optional-media-token",
    )

    await store.on_get_event_summary(
        _semantic_summary(20625, rfid=rfid), source="event_summary_update"
    )

    records = store.activity_journal.records()
    assert pet.location == STATE_HOME
    assert len(records) == 1
    assert records[0]["event_id"] == 20625
    assert records[0]["image_available"] is True
    assert records[0]["video_available"] is False
    assert store.last_event_id("OC-1") == 20625
    assert len(store._emitted_activity_keys) == 1
    async_fire.assert_called_once()
    assert sensor.native_value == "Ron - Entered"
    assert sensor.extra_state_attributes["event_id"] == 20625
    sensor.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "direction", "initial_location", "expected_activity"),
    [
        ("PEEK", "OUTWARD", STATE_HOME, "Peeked outside"),
        ("DENY", "INWARD", STATE_NOT_HOME, "Denied entry"),
    ],
)
async def test_non_movement_activity_without_frame_count_emits_without_location_change(
    action: str,
    direction: str,
    initial_location: str,
    expected_activity: str,
) -> None:
    """PEEK and DENY remain activity-only when optional media data is missing."""
    store, async_fire, _ = _live_store()
    pet = store.get_pet_by_rfid("123", "OC-1")
    pet.label = "Nelson"
    pet.location = initial_location
    _set_event_without_frame_count(store, 20626)

    await store.on_get_event_summary(
        _semantic_summary(20626, action=action, direction=direction)
    )

    records = store.activity_journal.records()
    assert pet.location == initial_location
    assert len(records) == 1
    assert records[0]["activity"] == expected_activity
    assert records[0]["location_changed"] is False
    async_fire.assert_called_once()


@pytest.mark.asyncio
async def test_breach_without_frame_count_emits_and_changes_location() -> None:
    """BREACH keeps its confirmed direction semantics without media metadata."""
    store, async_fire, _ = _live_store()
    pet = store.get_pet_by_rfid("123", "OC-1")
    pet.label = "Nelson"
    pet.location = STATE_HOME
    _set_event_without_frame_count(store, 20627)

    await store.on_get_event_summary(
        _semantic_summary(20627, action="BREACH", direction="OUTWARD")
    )

    records = store.activity_journal.records()
    assert pet.location == STATE_NOT_HOME
    assert len(records) == 1
    assert records[0]["activity"] == "Bypassed exit"
    assert records[0]["location_changed"] is True
    async_fire.assert_called_once()


@pytest.mark.asyncio
async def test_duplicate_without_frame_count_is_accounted_once() -> None:
    """Repeated summary paths update one journal row and fire one HA event."""
    store, async_fire, _ = _live_store()
    store.get_pet_by_rfid("123", "OC-1").label = "Nelson"
    _set_event_without_frame_count(store, 20628)
    summary = _semantic_summary(20628)

    await store.on_get_event_summary(summary, source="event_summary_update")
    await store.on_get_event_summary(summary, source="get_event_summary_retry")

    records = store.activity_journal.records()
    assert len(records) == 1
    assert records[0]["duplicate_delivery_count"] == 1
    assert records[0]["delivery_sources"] == [
        "event_summary_update",
        "get_event_summary_retry",
    ]
    assert len(store._emitted_activity_keys) == 1
    async_fire.assert_called_once()


@pytest.mark.asyncio
async def test_bootstrap_without_frame_count_remains_silent() -> None:
    """Startup replay suppression remains independent of optional media data."""
    client = AsyncMock()
    client.connection_state = OnlyCatConnectionState(True, "connect")
    async_fire = Mock()
    store = EventStore(client, hass=SimpleNamespace(bus=SimpleNamespace(async_fire=async_fire)))
    store.get_pet_by_rfid("123", "OC-1").label = "Nelson"
    _set_event_without_frame_count(store, 20629)

    await store.on_get_event_summary(_semantic_summary(20629))
    store.enable_activity_emission()
    await store.on_get_event_summary(_semantic_summary(20629))

    assert store.activity_journal.records() == []
    assert store._emitted_activity_keys == {}
    async_fire.assert_not_called()


@pytest.mark.asyncio
async def test_out_of_order_without_frame_count_cannot_regress_or_emit() -> None:
    """An older summary remains unable to regress location or add a journal row."""
    store, async_fire, _ = _live_store()
    pet = store.get_pet_by_rfid("123", "OC-1")
    pet.label = "Nelson"
    pet.location = STATE_NOT_HOME
    _set_event_without_frame_count(store, 20631)
    await store.on_get_event_summary(_semantic_summary(20631))
    assert pet.location == STATE_HOME

    await store.on_get_event_summary(
        _semantic_summary(20630, action="TRANSIT", direction="OUTWARD")
    )

    assert pet.location == STATE_HOME
    assert len(store.activity_journal.records()) == 1
    async_fire.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rfid", "action", "direction"),
    [
        ("", "TRANSIT", "INWARD"),
        ("123", "", "INWARD"),
        ("123", "TRANSIT", ""),
    ],
)
async def test_missing_semantic_identity_without_frame_count_is_rejected(
    rfid: str, action: str, direction: str
) -> None:
    """Removing the media gate does not weaken semantic identity validation."""
    store, async_fire, _ = _live_store()
    store.get_pet_by_rfid("123", "OC-1").label = "Nelson"
    _set_event_without_frame_count(store, 20632)

    await store.on_get_event_summary(
        _semantic_summary(20632, rfid=rfid, action=action, direction=direction)
    )

    assert store.activity_journal.records() == []
    assert store._emitted_activity_keys == {}
    async_fire.assert_not_called()


def test_journal_prunes_to_250_and_seven_days() -> None:
    journal = ActivityJournal()
    journal.append(_entry(1, timestamp=NOW - timedelta(days=8)))
    for event_id in range(2, 262):
        journal.append(_entry(event_id, timestamp=NOW + timedelta(seconds=event_id)))

    journal.prune(NOW + timedelta(seconds=262))

    assert len(journal.records()) == 250
    assert journal.records()[0]["event_id"] == 12
    assert all(
        datetime.fromisoformat(item["timestamp"]) >= NOW - timedelta(days=7)
        for item in journal.records()
    )


def test_journal_restore_ignores_corrupt_and_secret_fields() -> None:
    record = _entry(10, pet=None, rfid="unknown-rfid").as_storage_dict()
    record["accessToken"] = "must-not-survive"
    record["authenticated_media_url"] = "https://example.invalid/?token=secret"
    journal = ActivityJournal()

    journal.load([record, {"event_id": "bad"}], NOW)

    assert len(journal.records()) == 1
    serialized = repr(journal.records())
    assert "must-not-survive" not in serialized
    assert "token=secret" not in serialized
    assert journal.latest().pet is None


@pytest.mark.asyncio
async def test_schema_one_migration_preserves_watermarks_only() -> None:
    store = object.__new__(ActivityStore)
    old = {
        "last_event_ids": {"OC-1": 99},
        "emitted": {"key": NOW.isoformat()},
        "api_key": "must-not-migrate",
    }

    migrated = await store._async_migrate_func(1, 1, old)

    assert migrated == {
        "last_event_ids": {"OC-1": 99},
        "emitted": {"key": NOW.isoformat()},
        "journal": [],
    }


@pytest.mark.asyncio
async def test_duplicate_updates_one_journal_row_and_merges_sources() -> None:
    store, async_fire, _ = _live_store(profile={"label": "Nelson"})
    _set_event(store)

    await store.on_get_event_summary(_summary(), source="event_summary_update")
    await store.on_get_event_summary(_summary(), source="get_event_summary_retry")

    records = store.activity_journal.records()
    assert len(records) == 1
    assert records[0]["duplicate_delivery_count"] == 1
    assert records[0]["delivery_sources"] == [
        "event_summary_update",
        "get_event_summary_retry",
    ]
    async_fire.assert_called_once()
    assert "secret-media-token" not in repr(records)


@pytest.mark.asyncio
async def test_unknown_rfid_is_journaled_without_creating_pet() -> None:
    store, async_fire, _ = _live_store(profile={"label": "unknown-rfid"})
    _set_event(store)

    await store.on_get_event_summary(_summary(rfid="unknown-rfid"))

    latest = store.activity_journal.latest()
    assert latest is not None
    assert latest.rfid == "unknown-rfid"
    assert latest.pet is None
    assert store.find_pet_by_rfid("unknown-rfid") is None
    assert store.get_pets() == []
    assert async_fire.call_args.args[1]["pet"] is None


@pytest.mark.asyncio
async def test_named_profile_is_discovered_exactly_once() -> None:
    store, _, _ = _live_store(profile={"label": "Nelson"})
    discovered = Mock()
    store.add_pet_discovery_listener(discovered)
    _set_event(store)

    await store.on_get_event_summary(_summary())
    await store.on_get_event_summary(_summary())

    pet = store.find_pet_by_rfid("123")
    assert pet is not None
    assert pet.label == "Nelson"
    assert pet.location == STATE_HOME
    discovered.assert_called_once_with(pet)


@pytest.mark.asyncio
async def test_tracker_platform_adds_later_named_pet_once() -> None:
    store, _, _ = _live_store(profile={"label": "Nelson"})
    add_entities = Mock()
    unloaders: list[object] = []
    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(event_store=store),
        async_on_unload=unloaders.append,
    )
    await setup_trackers(SimpleNamespace(), entry, add_entities)
    _set_event(store)

    await store.on_get_event_summary(_summary())
    await store.on_get_event_summary(_summary())

    assert add_entities.call_count == 2
    dynamic_entities = add_entities.call_args_list[1].args[0]
    assert len(dynamic_entities) == 1
    assert dynamic_entities[0].pet.label == "Nelson"


def test_recent_activity_sensor_exposes_latest_only_without_secrets() -> None:
    store, _, _ = _live_store()
    store.activity_journal.append(_entry(10))
    store.activity_journal.append(_entry(11, pet=None, rfid="unknown-rfid"))
    sensor = OnlyCatRecentActivitySensor(Device("OC-1", "OnlyCat"), store)

    assert sensor.native_value == "Unknown RFID - Entered"
    assert sensor.extra_state_attributes["event_id"] == 11
    assert "journal" not in sensor.extra_state_attributes
    assert "secret" not in repr(sensor.extra_state_attributes).lower()
    assert len(sensor.extra_state_attributes) < 30


def test_websocket_is_read_only_paginated_and_device_scoped() -> None:
    store, _, _ = _live_store()
    for event_id in range(1, 4):
        store.activity_journal.append(_entry(event_id))
    runtime = SimpleNamespace(
        devices=[Device("OC-1", "OnlyCat")], event_store=store
    )
    config_entry = SimpleNamespace(domain="onlycat", runtime_data=runtime)
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_entry=lambda entry_id: config_entry if entry_id == "entry-1" else None
        )
    )
    connection = SimpleNamespace(send_result=Mock(), send_error=Mock())

    websocket_activity_journal(
        hass,
        connection,
        {
            "id": 1,
            "type": "onlycat/activity_journal",
            "config_entry_id": "entry-1",
            "device_id": "OC-1",
            "limit": 2,
        },
    )
    result = connection.send_result.call_args.args[1]
    assert [entry["event_id"] for entry in result["entries"]] == [3, 2]
    assert result["next_cursor"] is not None
    assert not hasattr(store.activity_journal, "delete")

    websocket_activity_journal(
        hass,
        connection,
        {
            "id": 2,
            "type": "onlycat/activity_journal",
            "config_entry_id": "entry-1",
            "device_id": "NOT-OURS",
            "limit": 2,
        },
    )
    connection.send_error.assert_called_with(2, "not_found", "OnlyCat device not found")


def test_websocket_schema_rejects_malformed_and_excessive_limits() -> None:
    schema = websocket_activity_journal._ws_schema
    with pytest.raises(vol.Invalid):
        schema({"id": 1, "type": "onlycat/activity_journal"})
    with pytest.raises(vol.Invalid):
        schema(
            {
                "id": 1,
                "type": "onlycat/activity_journal",
                "config_entry_id": "entry-1",
                "limit": 101,
            }
        )


def test_websocket_payload_contains_no_credentials_or_media_urls() -> None:
    payload = _entry(20).as_dict()
    serialized = repr(payload).lower()
    assert "access_token" not in serialized
    assert "accesstoken" not in serialized
    assert "api_key" not in serialized
    assert "media_url" not in serialized
