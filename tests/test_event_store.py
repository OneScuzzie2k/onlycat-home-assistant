"""Tests for EventStore."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import pytest
from custom_components.onlycat.api import OnlyCatConnectionState
from custom_components.onlycat.data import event_store as event_store_module
from custom_components.onlycat.data.event import (
    Event,
    EventClassification,
    EventTriggerSource,
)
from custom_components.onlycat.data.event_store import EventStore
from homeassistant.const import STATE_HOME, STATE_NOT_HOME


@pytest.mark.asyncio
async def test_run_listeners_no_current_event() -> None:
    """Test run_listeners when listeners are registered but no event data exists."""
    api_client = AsyncMock()
    store = EventStore(api_client)

    device_id = "OC-00000000001"

    # Register 7 listeners, matching the entity types that call add_event_listener
    listener_lock = AsyncMock(name="lock_on_event_update")
    listener_contraband = AsyncMock(name="contraband_on_event_update")
    listener_event = AsyncMock(name="event_on_event_update")
    listener_human = AsyncMock(name="human_on_event_update")
    listener_camera = AsyncMock(name="camera_on_event_update")
    listener_image = AsyncMock(name="image_on_event_update")
    listener_device_tracker = AsyncMock(name="device_tracker_on_event_update")

    store.add_event_listener(device_id, listener_lock)
    store.add_event_listener(device_id, listener_contraband)
    store.add_event_listener(device_id, listener_event)
    store.add_event_listener(device_id, listener_human)
    store.add_event_listener(device_id, listener_camera)
    store.add_event_listener(device_id, listener_image)
    store.add_event_listener(device_id, listener_device_tracker)

    # _current_events and _current_images are empty — no event data yet
    assert store._current_events == {}
    assert store._current_images == {}

    await store.run_event_listeners(device_id)

    # No callback should have been called since there is no event for this device
    listener_lock.assert_not_called()
    listener_contraband.assert_not_called()
    listener_event.assert_not_called()
    listener_human.assert_not_called()
    listener_camera.assert_not_called()
    listener_image.assert_not_called()
    listener_device_tracker.assert_not_called()


@pytest.mark.asyncio
async def test_run_listeners_never_calls_with_none() -> None:
    """
    Test that listeners are never called with None as the event parameter.

    Covers two scenarios:
    1. _current_events has an explicit None value for the device.
    2. _current_events has a real Event — callbacks should receive it, not None.
    """
    api_client = AsyncMock()
    store = EventStore(api_client)

    device_id = "OC-00000000001"

    listener_lock = AsyncMock(name="lock_on_event_update")
    listener_contraband = AsyncMock(name="contraband_on_event_update")
    listener_event = AsyncMock(name="event_on_event_update")
    listener_human = AsyncMock(name="human_on_event_update")
    listener_camera = AsyncMock(name="camera_on_event_update")
    listener_image = AsyncMock(name="image_on_event_update")
    listener_device_tracker = AsyncMock(name="device_tracker_on_event_update")

    all_listeners = [
        listener_lock,
        listener_contraband,
        listener_event,
        listener_human,
        listener_camera,
        listener_image,
        listener_device_tracker,
    ]

    store.add_event_listener(device_id, listener_lock)
    store.add_event_listener(device_id, listener_contraband)
    store.add_event_listener(device_id, listener_event)
    store.add_event_listener(device_id, listener_human)
    store.add_event_listener(device_id, listener_camera)
    store.add_event_listener(device_id, listener_image)
    store.add_event_listener(device_id, listener_device_tracker)

    # Scenario 1: _current_events entry is explicitly None
    store._current_events[device_id] = None

    await store.run_event_listeners(device_id)

    for listener in all_listeners:
        listener.assert_not_called()

    # Scenario 2: _current_events has a real Event — ensure it's passed, not None
    real_event = Event(device_id=device_id, event_id=42)
    store._current_events[device_id] = real_event

    await store.run_event_listeners(device_id)

    for listener in all_listeners:
        listener.assert_called_once_with(real_event)
        # Verify the argument was never None in any call
        for c in listener.call_args_list:
            assert c != call(None), "Listener must never be called with None"


@pytest.mark.asyncio
async def test_connection_state_listener_receives_updates() -> None:
    """Test connection listeners receive live socket-state changes."""
    api_client = AsyncMock()
    api_client.connection_state = OnlyCatConnectionState(
        connected=False,
        phase="initial",
    )
    store = EventStore(api_client)
    listener = AsyncMock()
    store.add_connection_listener(listener)
    new_state = OnlyCatConnectionState(
        connected=False,
        phase="disconnect",
        reason="server disconnect",
    )

    await store.on_connection_state(new_state)

    assert store.connection_state == new_state
    listener.assert_awaited_once_with(new_state)


@pytest.mark.asyncio
async def test_retry_get_event_summary_until_rfid_retries_until_summary_complete() -> (
    None
):
    """Test summary retry continues until an RFID-tagged subevent is returned."""
    api_client = AsyncMock()
    api_client.connection_state = OnlyCatConnectionState(
        connected=True,
        phase="connect",
    )
    store = EventStore(api_client)
    store.send_get_event_summary = AsyncMock(
        side_effect=[
            {"body": {"deviceId": "OC-1", "eventId": 1, "subevents": []}},
            {
                "body": {
                    "deviceId": "OC-1",
                    "eventId": 1,
                    "subevents": [{"rfidCode": "123"}],
                }
            },
        ]
    )

    await store._retry_get_event_summary_until_rfid(
        "OC-1",
        1,
        "token",
        (0, 0),
    )

    assert store.send_get_event_summary.await_count == 2


def test_mark_event_received_updates_last_event_timestamp() -> None:
    """Test EventStore records when live event data is received."""
    api_client = AsyncMock()
    api_client.connection_state = OnlyCatConnectionState(
        connected=True,
        phase="connect",
    )
    store = EventStore(api_client)

    assert store.last_event_received_at is None

    store.mark_event_received()

    assert store.last_event_received_at is not None


def test_apply_pet_snapshot_reports_location_changes() -> None:
    """Test authoritative pet snapshots update EventStore state."""
    api_client = AsyncMock()
    api_client.connection_state = OnlyCatConnectionState(
        connected=True,
        phase="connect",
    )
    store = EventStore(api_client)
    last_seen = datetime(2026, 6, 16, 12, 0, tzinfo=UTC)
    outward_subevent = type(
        "SubEventStub",
        (),
        {"direction": "outward", "action": "transit"},
    )()
    inward_subevent = type(
        "SubEventStub",
        (),
        {"direction": "inward", "action": "transit"},
    )()

    changed = store.apply_pet_snapshot(
        device_id="OC-1",
        rfid_code="123",
        label="Milo",
        last_seen=last_seen,
        location=None,
        last_subevent=outward_subevent,
    )

    pet = store.get_pet_by_rfid("123")
    assert changed is True
    assert pet.location == STATE_NOT_HOME
    assert pet.last_seen == last_seen

    changed = store.apply_pet_snapshot(
        device_id="OC-1",
        rfid_code="123",
        label="Milo",
        last_seen=last_seen,
        location=None,
        last_subevent=inward_subevent,
    )

    assert changed is True
    assert pet.location == STATE_HOME


def _live_store() -> tuple[EventStore, Mock]:
    """Return an emission-enabled store with a fake HA event bus."""
    client = AsyncMock()
    client.connection_state = OnlyCatConnectionState(True, "connect")
    bus = SimpleNamespace(async_fire=Mock())
    hass = SimpleNamespace(bus=bus)
    store = EventStore(client, hass=hass)
    store.enable_activity_emission()
    return store, bus.async_fire


def _set_event(store: EventStore, event_id: int, timestamp: datetime) -> None:
    """Set complete event metadata without invoking an API retry."""
    store._current_events["OC-1"] = Event(
        device_id="OC-1",
        event_id=event_id,
        timestamp=timestamp,
        frame_count=20,
        poster_frame_index=10,
        access_token="never-expose-this",
        event_classification=EventClassification.CLEAR,
        event_trigger_source=EventTriggerSource.OUTDOOR_MOTION,
    )


def _summary(
    event_id: int,
    timestamp: datetime,
    subevents: list[dict] | None = None,
) -> dict:
    """Build an EventSummary API payload."""
    return {
        "deviceId": "OC-1",
        "eventId": event_id,
        "timestamp": timestamp.isoformat(),
        "subevents": subevents
        or [
            {
                "startFrameIndex": 1,
                "endFrameIndex": 10,
                "rfidCode": "123",
                "action": "TRANSIT",
                "direction": "INWARD",
            }
        ],
    }


@pytest.mark.asyncio
async def test_activity_event_fires_once_with_safe_payload() -> None:
    """Test duplicate update paths produce one token-free HA event."""
    store, async_fire = _live_store()
    timestamp = datetime(2026, 9, 24, 9, 40, 32, tzinfo=UTC)
    pet = store.get_pet_by_rfid("123", "OC-1")
    pet.label = "Nelson"
    _set_event(store, 20619, timestamp)

    await store.on_get_event_summary(_summary(20619, timestamp))
    await store.on_get_event_summary(_summary(20619, timestamp))

    async_fire.assert_called_once()
    event_type, payload = async_fire.call_args.args
    assert event_type == "onlycat_activity"
    assert payload == {
        "pet": "Nelson",
        "rfid": "123",
        "action": "TRANSIT",
        "direction": "INWARD",
        "activity": "Entered",
        "timestamp": timestamp.isoformat(),
        "device_id": "OC-1",
        "event_id": 20619,
        "classification": "CLEAR",
        "trigger_source": "OUTDOOR_MOTION",
        "image_available": True,
        "video_available": True,
    }
    assert "never-expose-this" not in repr(payload)
    assert pet.location == STATE_HOME
    assert pet.last_activity is not None


@pytest.mark.asyncio
async def test_bootstrap_suppresses_replay_then_allows_unseen_event() -> None:
    """Test startup seeding is silent while a later event fires once."""
    client = AsyncMock()
    client.connection_state = OnlyCatConnectionState(True, "connect")
    async_fire = Mock()
    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=async_fire))
    store = EventStore(client, hass=hass)
    timestamp = datetime(2026, 9, 24, 9, 40, tzinfo=UTC)
    store.get_pet_by_rfid("123", "OC-1").label = "Nelson"
    _set_event(store, 10, timestamp)

    await store.on_get_event_summary(_summary(10, timestamp))
    store.enable_activity_emission()
    await store.on_get_event_summary(_summary(10, timestamp))
    assert async_fire.call_count == 0

    _set_event(store, 11, timestamp + timedelta(minutes=1))
    await store.on_get_event_summary(_summary(11, timestamp + timedelta(minutes=1)))
    assert async_fire.call_count == 1

    await store.on_get_event_summary(_summary(11, timestamp + timedelta(minutes=1)))
    assert async_fire.call_count == 1


@pytest.mark.asyncio
async def test_multiple_distinct_subevents_are_preserved() -> None:
    """Test distinct activities under one event ID are each emitted once."""
    store, async_fire = _live_store()
    timestamp = datetime(2026, 9, 24, 9, 40, tzinfo=UTC)
    store.get_pet_by_rfid("123", "OC-1").label = "Nelson"
    _set_event(store, 12, timestamp)
    subevents = [
        {
            "startFrameIndex": 1,
            "endFrameIndex": 5,
            "rfidCode": "123",
            "action": "PEEK",
            "direction": "OUTWARD",
        },
        {
            "startFrameIndex": 6,
            "endFrameIndex": 10,
            "rfidCode": "123",
            "action": "TRANSIT",
            "direction": "OUTWARD",
        },
    ]

    await store.on_get_event_summary(_summary(12, timestamp, subevents))
    await store.on_get_event_summary(_summary(12, timestamp, subevents))

    assert async_fire.call_count == 2
    assert {call_.args[1]["activity"] for call_ in async_fire.call_args_list} == {
        "Peeked outside",
        "Exited",
    }


@pytest.mark.asyncio
async def test_out_of_order_event_cannot_regress_pet_or_notify() -> None:
    """Test a delayed older summary cannot change location or fire."""
    store, async_fire = _live_store()
    timestamp = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    pet = store.get_pet_by_rfid("123", "OC-1")
    _set_event(store, 20, timestamp)
    await store.on_get_event_summary(_summary(20, timestamp))
    assert pet.location == STATE_HOME
    assert async_fire.call_count == 1

    older = timestamp - timedelta(minutes=5)
    await store.on_get_event_summary(
        _summary(
            19,
            older,
            [
                {
                    "startFrameIndex": 1,
                    "endFrameIndex": 10,
                    "rfidCode": "123",
                    "action": "TRANSIT",
                    "direction": "OUTWARD",
                }
            ],
        )
    )

    assert pet.location == STATE_HOME
    assert async_fire.call_count == 1


@pytest.mark.asyncio
async def test_persisted_dedup_survives_integration_reload(monkeypatch) -> None:
    """Test a reconnect/reload replay is suppressed by persisted semantic keys."""
    timestamp = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
    persisted_key = '["OC-1",20,"123","TRANSIT","INWARD"]'

    class FakeStorage:
        async def async_load(self):
            return {
                "last_event_ids": {"OC-1": 19},
                "emitted": {persisted_key: datetime.now(UTC).isoformat()},
            }

        def async_delay_save(self, data_func, delay):
            assert "emitted" in data_func()
            assert delay == 5

    storage = FakeStorage()
    monkeypatch.setattr(
        event_store_module,
        "Store",
        lambda hass, version, key: storage,
    )
    client = AsyncMock()
    client.connection_state = OnlyCatConnectionState(True, "reconnect")
    async_fire = Mock()
    hass = SimpleNamespace(bus=SimpleNamespace(async_fire=async_fire))
    store = EventStore(client, hass=hass, storage_key="onlycat.activity.test")
    await store.async_initialize()
    store.enable_activity_emission()
    store.get_pet_by_rfid("123", "OC-1").label = "Nelson"
    _set_event(store, 20, timestamp)

    await store.on_get_event_summary(_summary(20, timestamp))

    async_fire.assert_not_called()
