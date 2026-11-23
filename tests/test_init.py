"""Tests for OnlyCat/__init.py."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, call

import pytest
from custom_components.onlycat import (
    _async_bootstrap_entry,
    _initialize_devices,
    _refresh_pet_snapshots_with_notify,
    _refresh_subscriptions,
    _run_health_check,
    async_force_sync_entry,
)
from custom_components.onlycat.api import OnlyCatApiClientError, OnlyCatConnectionState
from custom_components.onlycat.data.event_store import EventStore
from homeassistant.const import STATE_HOME

get_devices = [
    # "Normal device"
    {
        "deviceId": "OC-00000000001",
        "description": "Device Name",
        "timeZone": "Europe/Zurich",
        "deviceTransitPolicyId": 0000,
        "cursorId": 0000,
    },
    # Fresh device with no transit policy
    {
        "deviceId": "OC-00000000002",
        "description": "Device Name",
        "timeZone": "Europe/Zurich",
        "cursorId": 0000,
    },
]
get_device = {
    "OC-00000000001": {
        "deviceId": "OC-00000000001",
        "description": "Device Name",
        "timeZone": "Europe/Zurich",
        "deviceTransitPolicyId": 0000,
        "connectivity": {
            "connected": True,
            "disconnectReason": None,
            "timestamp": 1743841488269,
        },
    },
    "OC-00000000002": {
        "deviceId": "OC-00000000002",
        "description": "Device Name",
        "timeZone": "Europe/Zurich",
        "connectivity": {
            "connected": True,
            "disconnectReason": None,
            "timestamp": 1743841488269,
        },
    },
}
get_device_transit_policy = {
    0: {
        "deviceTransitPolicyId": 0000,
        "deviceId": "OC-00000000001",
        "name": "Nachts",
        "transitPolicy": {
            "rules": [
                {
                    "action": {"lock": True},
                    "criteria": {
                        "eventTriggerSource": 3,
                        "eventClassification": [2, 3],
                    },
                    "description": "Contraband Rule",
                },
                {
                    "action": {"lock": False},
                    "enabled": True,
                    "criteria": {
                        "rfidCode": [
                            "000000000000003",
                            "000000000000001",
                            "000000000000002",
                        ],
                        "eventTriggerSource": 3,
                    },
                    "description": "Entry Rule",
                },
            ],
            "idleLock": True,
            "idleLockBattery": True,
        },
    }
}
device_update = [
    {
        "type": "update",
        "deviceId": "OC-00000000002",
        "body": {
            "connectivity": {
                "connected": False,
                "disconnectReason": "SERVER_INITIATED_DISCONNECT",
                "timestamp": 1754114553075,
            }
        },
    },
    {
        "deviceId": "OC-00000000002",
        "type": "update",
        "body": {"deviceId": "OC-00000000002", "deviceTransitPolicyId": 0000},
    },
]


async def mock_send_message(topic: str, data: dict) -> Any | None:
    """Mock of OnlyCatApiClient.send_message to return test api responses."""
    if topic == "getDevices":
        return get_devices
    if topic == "getDevice":
        return get_device[data["deviceId"]]
    if topic == "getDeviceTransitPolicy":
        return get_device_transit_policy[data["deviceTransitPolicyId"]]
    return None


@pytest.mark.asyncio
async def test_initialize_devices() -> None:
    """Test _initialize_devices."""
    mock_entry = AsyncMock()
    mock_entry.runtime_data.devices = []
    mock_entry.runtime_data.client = AsyncMock()
    mock_entry.runtime_data.client.add_event_listener = Mock()
    mock_entry.runtime_data.client.send_message.side_effect = mock_send_message
    await _initialize_devices(mock_entry)

    assert len(mock_entry.runtime_data.devices) == len(get_devices)
    mock_entry.runtime_data.client.send_message.assert_any_call(
        "getDevices", {"subscribe": True}
    )
    for device_id in get_device:
        mock_entry.runtime_data.client.send_message.assert_any_call(
            "getDevice", {"deviceId": device_id, "subscribe": True}
        )
    assert mock_entry.runtime_data.devices[1].device_transit_policy_id is None


@pytest.mark.asyncio
async def test_bootstrap_entry_returns_false_on_connect_failure() -> None:
    """Test setup bootstrap does not raise on initial socket failure."""
    mock_entry = Mock()
    mock_entry.entry_id = "test-entry"
    mock_entry.runtime_data = Mock()
    mock_entry.runtime_data.client = AsyncMock()
    mock_entry.runtime_data.client.connect.side_effect = OnlyCatApiClientError

    result = await _async_bootstrap_entry(mock_entry)

    assert result is False


@pytest.mark.asyncio
async def test_refresh_pet_snapshots_notifies_on_resync() -> None:
    """Test authoritative pet snapshots notify listeners when state changes."""
    api_client = AsyncMock()
    api_client.connection_state = OnlyCatConnectionState(
        connected=True,
        phase="connect",
    )
    store = EventStore(api_client)
    store.run_pet_listeners = AsyncMock()

    async def send_message(topic: str, data: dict) -> Any | None:
        if topic == "getLastSeenRfidCodesByDevice":
            return [{"rfidCode": "123"}]
        if topic == "getRfidLastSeenByDevice":
            return [
                {
                    "rfidCode": "123",
                    "eventTimestamp": "2026-06-16T12:00:00+00:00",
                    "lastSubevent": {
                        "startFrameIndex": 1,
                        "endFrameIndex": 2,
                        "rfidCode": "123",
                        "direction": "outward",
                        "action": "transit",
                    },
                }
            ]
        if topic == "getRfidProfile":
            return {"label": "Milo"}
        return None

    runtime_data = SimpleNamespace(
        client=AsyncMock(side_effect=None),
        devices=[SimpleNamespace(device_id="OC-1")],
        event_store=store,
    )
    runtime_data.client.send_message.side_effect = send_message
    entry = SimpleNamespace(runtime_data=runtime_data)

    changed = await _refresh_pet_snapshots_with_notify(
        entry,
        notify=True,
        reason="test resync",
    )

    assert changed == 1
    store.run_pet_listeners.assert_awaited_once_with("123")


@pytest.mark.asyncio
async def test_force_sync_reconnects_before_refresh() -> None:
    """Test manual force sync reconnects first when the socket is offline."""
    client = AsyncMock()
    client.is_connected = False
    client.restart_connection.return_value = True
    client.connection_state = OnlyCatConnectionState(
        connected=False,
        phase="disconnect",
    )

    async def send_message(topic: str, data: dict) -> Any | None:
        if topic == "getDevice":
            return {"deviceId": "OC-1"}
        if topic == "getLastSeenRfidCodesByDevice":
            return []
        if topic == "getRfidLastSeenByDevice":
            return []
        return None

    client.send_message.side_effect = send_message
    runtime_data = SimpleNamespace(
        client=client,
        devices=[SimpleNamespace(device_id="OC-1")],
        event_store=EventStore(client),
        coordinator=SimpleNamespace(async_refresh=AsyncMock()),
        sync_lock=asyncio.Lock(),
    )
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=runtime_data)

    changed = await async_force_sync_entry(entry, reason="manual test")

    assert changed == 0
    client.restart_connection.assert_awaited_once()
    runtime_data.coordinator.async_refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_health_check_ignores_quiet_event_stream() -> None:
    """Test event inactivity does not resync, disconnect, or restart transport."""
    client = AsyncMock()
    client.is_connected = True
    client.publish_connection_state = AsyncMock()
    client.restart_connection = AsyncMock()
    client.connection_state = OnlyCatConnectionState(
        connected=True,
        phase="connect",
    )

    store = EventStore(client)
    store.run_pet_listeners = AsyncMock()
    pet = store.get_pet_by_rfid("123", "OC-1")
    pet.label = "Nelson"
    pet.location = STATE_HOME
    store._last_event_received_at = datetime.now(UTC) - timedelta(hours=1)

    runtime_data = SimpleNamespace(
        client=client,
        devices=[SimpleNamespace(device_id="OC-1")],
        event_store=store,
        coordinator=SimpleNamespace(async_refresh=AsyncMock()),
        sync_lock=asyncio.Lock(),
    )
    entry = SimpleNamespace(entry_id="entry-1", runtime_data=runtime_data)

    await _run_health_check(entry)

    assert pet.location == STATE_HOME
    store.run_pet_listeners.assert_not_awaited()
    client.publish_connection_state.assert_not_awaited()
    client.restart_connection.assert_not_awaited()


@pytest.mark.asyncio
async def test_health_check_recovers_genuine_disconnect() -> None:
    """Test positive transport failure still triggers recovery."""
    client = AsyncMock()
    client.is_connected = False
    client.connection_state = OnlyCatConnectionState(
        connected=False,
        phase="disconnect",
    )
    entry = SimpleNamespace(
        entry_id="entry-1",
        runtime_data=SimpleNamespace(
            client=client,
            event_store=EventStore(client),
        ),
    )

    await _run_health_check(entry)

    client.restart_connection.assert_awaited_once_with(
        "socket disconnected during health check"
    )


@pytest.mark.asyncio
async def test_bootstrap_subscription_seeds_only_latest_event() -> None:
    """Test initial history establishes a watermark without replaying history."""
    client = AsyncMock()
    client.connection_state = OnlyCatConnectionState(True, "connect")
    client.wait_until_ready.return_value = True
    events = [
        {"eventId": 9, "timestamp": "2026-09-24T09:00:00+00:00"},
        {"eventId": 10, "timestamp": "2026-09-24T10:00:00+00:00"},
    ]

    async def send_message(topic: str, data: dict) -> Any | None:
        if topic == "getDeviceEvents":
            return list(events)
        if topic in {"getLastSeenRfidCodesByDevice", "getRfidLastSeenByDevice"}:
            return []
        return {}

    client.send_message.side_effect = send_message
    store = EventStore(client)
    store.send_get_event_message = AsyncMock()
    entry = SimpleNamespace(
        entry_id="entry-1",
        runtime_data=SimpleNamespace(
            client=client,
            devices=[SimpleNamespace(device_id="OC-1")],
            event_store=store,
        ),
    )

    await _refresh_subscriptions(entry, None)

    store.send_get_event_message.assert_awaited_once_with("OC-1", 10, subscribe=False)


@pytest.mark.asyncio
async def test_reconnect_reconciles_unseen_events_in_order() -> None:
    """Test reconnect fetches the watermark and newer IDs for safe dedup/recovery."""
    client = AsyncMock()
    client.connection_state = OnlyCatConnectionState(True, "reconnect")
    client.wait_until_ready.return_value = True
    events = [
        {"eventId": event_id, "timestamp": f"2026-09-24T{event_id:02d}:00:00+00:00"}
        for event_id in (9, 10, 11, 12)
    ]

    async def send_message(topic: str, data: dict) -> Any | None:
        if topic == "getDeviceEvents":
            return list(events)
        if topic in {"getLastSeenRfidCodesByDevice", "getRfidLastSeenByDevice"}:
            return []
        return {}

    client.send_message.side_effect = send_message
    store = EventStore(client)
    store._last_event_ids["OC-1"] = 10
    store.enable_activity_emission()
    store.send_get_event_message = AsyncMock()
    entry = SimpleNamespace(
        entry_id="entry-1",
        runtime_data=SimpleNamespace(
            client=client,
            devices=[SimpleNamespace(device_id="OC-1")],
            event_store=store,
        ),
    )

    await _refresh_subscriptions(entry, {"phase": "reconnect"})

    assert store.send_get_event_message.await_args_list == [
        call("OC-1", 10, subscribe=False),
        call("OC-1", 11, subscribe=False),
        call("OC-1", 12, subscribe=False),
    ]
