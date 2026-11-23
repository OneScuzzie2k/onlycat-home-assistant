"""Tests for 2.0.7.2 tracker and connectivity presentation."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from custom_components.onlycat.api import OnlyCatConnectionState
from custom_components.onlycat.binary_sensor_connectivity import OnlyCatConnectionSensor
from custom_components.onlycat.binary_sensor_event import OnlyCatEventSensor
from custom_components.onlycat.data.activity import OnlyCatActivity
from custom_components.onlycat.data.device import Device, DeviceConnectivity
from custom_components.onlycat.data.event_store import EventStore
from custom_components.onlycat.data.pet import Pet
from custom_components.onlycat.device_tracker import OnlyCatPetTracker
from homeassistant.const import STATE_HOME


def test_tracker_exposes_normalized_activity_without_tokens() -> None:
    """Test rich activity is exposed on the existing tracker entity."""
    timestamp = datetime(2026, 9, 24, 9, 40, 32, tzinfo=UTC)
    activity = OnlyCatActivity(
        device_id="OC-1",
        event_id=20619,
        timestamp=timestamp,
        rfid="123",
        pet="Nelson",
        action="TRANSIT",
        direction="INWARD",
        activity="Entered",
        classification="CLEAR",
        trigger_source="OUTDOOR_MOTION",
        image_available=True,
        video_available=True,
    )
    pet = Pet(
        "123",
        STATE_HOME,
        timestamp,
        device_id="OC-1",
        label="Nelson",
        location_changed_at=timestamp,
        last_activity=activity,
        location_source="onlycat_activity",
        location_source_event_id=20619,
    )
    client = AsyncMock()
    client.connection_state = OnlyCatConnectionState(False, "disconnect")
    tracker = OnlyCatPetTracker(pet, EventStore(client))

    assert tracker.entity_id == "device_tracker.oc_1_123_tracker"
    assert tracker.available is True
    assert tracker.extra_state_attributes["last_flap_activity"] == "Entered"
    assert tracker.extra_state_attributes["last_event_id"] == 20619
    assert tracker.extra_state_attributes["last_trigger_source"] == "OUTDOOR_MOTION"
    assert "token" not in repr(tracker.extra_state_attributes).lower()


def test_flap_event_preserves_availability_during_transport_recovery() -> None:
    """Test a transient socket disconnect does not erase last flap state."""
    client = AsyncMock()
    client.connection_state = OnlyCatConnectionState(False, "disconnect")
    store = EventStore(client)
    device = Device(device_id="OC-1", description="OnlyCat")

    entity = OnlyCatEventSensor(device, store)

    assert entity.available is True
    assert entity.entity_id == "binary_sensor.oc_1_event"


@pytest.mark.asyncio
async def test_connectivity_main_state_is_physical_flap_not_socket() -> None:
    """Test upstream physical connectivity remains distinct from transport."""
    timestamp = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
    device = Device(
        device_id="OC-1",
        description="OnlyCat",
        connectivity=DeviceConnectivity(False, "OFFLINE", timestamp),
    )
    client = SimpleNamespace(
        is_connected=True,
        connection_state=OnlyCatConnectionState(True, "connect", authorized=True),
        add_event_listener=lambda *args: None,
    )

    entity = OnlyCatConnectionSensor(device, client)

    assert entity.is_on is False
    assert entity.extra_state_attributes["socket_connected"] is True
    assert entity.extra_state_attributes["flap_connected"] is False
    assert entity.extra_state_attributes["account_authorized"] is True

    entity.async_write_ha_state = Mock()
    await entity.on_connection_state(OnlyCatConnectionState(False, "disconnect"))

    assert entity.is_on is False
    assert entity.extra_state_attributes["socket_connected"] is False
