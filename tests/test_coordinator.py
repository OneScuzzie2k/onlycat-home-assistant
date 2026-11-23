"""Tests for OnlyCat coordinator connection state handling."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from custom_components.onlycat.api import OnlyCatConnectionState
from custom_components.onlycat.coordinator import (
    OnlyCatDataUpdateCoordinator,
    _normalise_reboot_logs,
)


def test_reboot_logs_keep_new_shape_and_legacy_aliases() -> None:
    """Test new reboot fields remain authoritative without breaking old templates."""
    logs = [
        {
            "deviceId": "device-1",
            "timestamp": "2026-09-09T06:00:00+00:00",
            "build": "123",
            "cause": "watchdog",
            "isError": True,
            "summary": "Restarted",
            "detail": "Watchdog restart",
            "message": "Restarted by watchdog",
        }
    ]

    result = _normalise_reboot_logs(logs)

    assert result[0]["cause"] == "watchdog"
    assert result[0]["time"] == result[0]["timestamp"]
    assert result[0]["measureName"] == "message"


@pytest.mark.asyncio
async def test_coordinator_tracks_socket_without_discarding_cached_data() -> None:
    """Test a transient socket disconnect preserves coordinator availability."""
    hass = Mock()
    config_entry = SimpleNamespace(
        data={"settings": {"poll_interval_hours": 1}},
        async_on_unload=Mock(),
    )
    coordinator = OnlyCatDataUpdateCoordinator(hass, config_entry)
    coordinator.async_update_listeners = Mock()

    disconnected = OnlyCatConnectionState(
        connected=False,
        phase="disconnect",
        reason="server disconnect",
    )
    connected = OnlyCatConnectionState(
        connected=True,
        phase="reconnect",
    )

    await coordinator.on_connection_state(disconnected)
    assert coordinator.socket_state == disconnected
    assert coordinator.socket_connected is False
    assert coordinator.last_update_success is True

    await coordinator.on_connection_state(connected)
    assert coordinator.socket_state == connected
    assert coordinator.socket_connected is True
    assert coordinator.last_update_success is True
    assert coordinator.async_update_listeners.call_count == 2
