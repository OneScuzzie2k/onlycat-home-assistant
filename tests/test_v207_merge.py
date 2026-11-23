"""Regression tests for the selectively merged upstream v2.0.7 changes."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.onlycat import (
    PLATFORMS,
    async_migrate_entry,
    async_reload_entry,
    async_unload_entry,
)
from custom_components.onlycat.api import OnlyCatConnectionState
from custom_components.onlycat.binary_sensor_event import OnlyCatEventSensor
from custom_components.onlycat.coordinator import OnlyCatDataUpdateCoordinator
from custom_components.onlycat.data.event import Event, EventTriggerSource
from custom_components.onlycat.data.event_store import EventStore
from custom_components.onlycat.data.event_summary import EventSummary, SubEvent
from custom_components.onlycat.sensor_last_rfid_event import (
    OnlyCatLastRfidEventSensor,
)


@pytest.mark.asyncio
async def test_custom_v3_entry_migrates_token_to_api_key() -> None:
    """Ensure deployed custom v3 entries migrate without losing settings."""
    config_entries = SimpleNamespace(async_update_entry=Mock())
    hass = SimpleNamespace(config_entries=config_entries)
    entry = SimpleNamespace(
        version=3,
        minor_version=1,
        data={
            "user_id": "user-1",
            "token": "secret",
            "settings": {"enable_detailed_metrics": True},
        },
    )

    assert await async_migrate_entry(hass, entry) is True

    update = config_entries.async_update_entry.call_args
    migrated = update.kwargs["data"]
    assert migrated["api_key"] == "secret"
    assert "token" not in migrated
    assert migrated["settings"]["enable_detailed_metrics"] is True
    assert update.kwargs["version"] == 4
    assert update.kwargs["minor_version"] == 1


@pytest.mark.asyncio
async def test_unload_clears_listeners_and_reload_uses_ha_lifecycle() -> None:
    """Ensure reload cannot retain callbacks to unloaded HA entities."""
    client = SimpleNamespace(
        disconnect=AsyncMock(),
        clear_event_listeners=Mock(),
    )
    runtime_data = SimpleNamespace(
        client=client,
        connect_retry_task=None,
        health_monitor_task=None,
    )
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=runtime_data)
    config_entries = SimpleNamespace(
        async_unload_platforms=AsyncMock(return_value=True),
        async_reload=AsyncMock(),
    )
    hass = SimpleNamespace(config_entries=config_entries)

    assert await async_unload_entry(hass, entry) is True
    client.disconnect.assert_awaited_once()
    client.clear_event_listeners.assert_called_once()
    config_entries.async_unload_platforms.assert_awaited_once_with(entry, PLATFORMS)

    await async_reload_entry(hass, entry)
    config_entries.async_reload.assert_awaited_once_with("entry-1")


@pytest.mark.asyncio
async def test_coordinator_uses_reboot_log_api_and_new_response() -> None:
    """Ensure polling uses getDeviceRebootLogs and preserves its new fields."""
    client = SimpleNamespace(is_connected=True, send_message=AsyncMock())
    client.send_message.side_effect = [
        None,
        [
            {
                "deviceId": "OC-1",
                "timestamp": "2026-09-09T06:00:00+00:00",
                "build": "123",
                "cause": "watchdog",
                "isError": True,
                "summary": "Restarted",
                "detail": "Watchdog restart",
                "message": "Restarted by watchdog",
            }
        ],
    ]
    entry = SimpleNamespace(
        data={
            "settings": {
                "poll_interval_hours": 1,
                "enable_detailed_metrics": False,
            }
        },
        runtime_data=SimpleNamespace(
            client=client,
            devices=[SimpleNamespace(device_id="OC-1")],
        ),
    )
    coordinator = object.__new__(OnlyCatDataUpdateCoordinator)
    coordinator.config_entry = entry

    data = await coordinator._async_update_data()

    reboot_call = client.send_message.await_args_list[1]
    assert reboot_call.args[0] == "getDeviceRebootLogs"
    assert "measureName" not in reboot_call.args[1]
    assert data["OC-1"]["errors"][0]["cause"] == "watchdog"
    assert data["OC-1"]["errors"][0]["time"] == data["OC-1"]["errors"][0][
        "timestamp"
    ]


@pytest.mark.asyncio
async def test_event_summary_rfid_code_is_authoritative_and_ids_stay_legacy() -> None:
    """Ensure singular summary RFID wins without changing entity identity."""
    api_client = SimpleNamespace(
        connection_state=OnlyCatConnectionState(connected=True, phase="connect")
    )
    store = EventStore(api_client)
    device = SimpleNamespace(device_id="OC-ABC-123", description="OnlyCat")
    sensor = OnlyCatEventSensor(device, store)
    sensor.async_write_ha_state = Mock()

    await sensor.on_event_update(
        Event(
            device_id=device.device_id,
            event_id=42,
            event_trigger_source=EventTriggerSource.INDOOR_MOTION,
            rfid_codes=["legacy-code"],
        )
    )
    subevent = SubEvent.from_api_response(
        {
            "startFrameIndex": 1,
            "endFrameIndex": 2,
            "rfidCode": "summary-code",
            "direction": "inward",
            "action": "transit",
        }
    )
    await sensor.on_event_summary_update(
        EventSummary(device_id=device.device_id, event_id=42, subevents=[subevent])
    )

    assert sensor.unique_id == "oc_abc_123_event"
    assert sensor.entity_id == "binary_sensor.oc_abc_123_event"
    assert sensor.extra_state_attributes["rfidCode"] == "summary-code"
    assert sensor.extra_state_attributes["rfidCodes"] == ["legacy-code"]


@pytest.mark.asyncio
async def test_summary_only_event_is_not_missed() -> None:
    """Ensure a summary can create the event-sensor update without a full event."""
    api_client = SimpleNamespace(
        connection_state=OnlyCatConnectionState(connected=True, phase="connect")
    )
    store = EventStore(api_client)
    device = SimpleNamespace(device_id="OC-1", description="OnlyCat")
    sensor = OnlyCatEventSensor(device, store)
    sensor.async_write_ha_state = Mock()
    subevent = SubEvent.from_api_response(
        {
            "startFrameIndex": 1,
            "endFrameIndex": 2,
            "rfidCode": "rfid-1",
            "direction": "outward",
            "action": "transit",
        }
    )

    await sensor.on_event_summary_update(
        EventSummary(device_id=device.device_id, event_id=99, subevents=[subevent])
    )

    assert sensor.is_on is True
    assert sensor.extra_state_attributes["eventId"] == 99
    assert sensor.extra_state_attributes["rfidCode"] == "rfid-1"


@pytest.mark.asyncio
async def test_custom_last_rfid_sensor_accepts_summary_rfid_code() -> None:
    """Ensure the custom RFID sensor survives removal of full-event RFID lists."""
    api_client = SimpleNamespace(
        connection_state=OnlyCatConnectionState(connected=True, phase="connect")
    )
    store = EventStore(api_client)
    device = SimpleNamespace(device_id="OC-1", description="OnlyCat")
    sensor = OnlyCatLastRfidEventSensor(device, store)
    sensor.async_write_ha_state = Mock()
    subevent = SubEvent.from_api_response(
        {
            "startFrameIndex": 1,
            "endFrameIndex": 2,
            "rfidCode": "rfid-2",
            "direction": "inward",
            "action": "transit",
        }
    )

    await sensor.on_event_summary_update(
        EventSummary(device_id=device.device_id, event_id=100, subevents=[subevent])
    )

    assert sensor.unique_id == "oc_1_last_rfid_event"
    assert sensor.entity_id == "sensor.oc_1_last_rfid_event"
    assert sensor.extra_state_attributes["rfidCode"] == "rfid-2"
    assert sensor.extra_state_attributes["rfidCodes"] == ["rfid-2"]
