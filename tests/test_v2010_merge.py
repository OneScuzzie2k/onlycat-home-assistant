"""Cover stable v2.0.10 telemetry behavior while preserving custom recovery."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.onlycat.binary_sensor_device_errors import OnlyCatErrorSensor
from custom_components.onlycat.coordinator import OnlyCatDataUpdateCoordinator
from custom_components.onlycat.data.device import Device


@pytest.mark.parametrize("hours,window", [(0.25, 1), (0.75, 1), (1, 1), (1.25, 2)])
async def test_latest_telemetry_request_and_whole_hour_reboot_window(hours, window):
    sample = [{"measures": {"tcpu": 41.5}}]
    client = SimpleNamespace(is_connected=True, send_message=AsyncMock())
    client.send_message.side_effect = [None, [], sample]
    entry = SimpleNamespace(
        data={
            "settings": {"poll_interval_hours": hours, "enable_detailed_metrics": True}
        },
        async_on_unload=Mock(),
        runtime_data=SimpleNamespace(
            client=client, devices=[Device("OC-1", "OnlyCat")]
        ),
    )
    coordinator = OnlyCatDataUpdateCoordinator(Mock(), entry)
    assert coordinator.update_interval == timedelta(hours=hours)
    data = await coordinator._async_update_data()
    client.send_message.assert_any_await(
        "getDeviceRebootLogs", {"deviceId": "OC-1", "limit": 100, "hours": window}
    )
    client.send_message.assert_any_await(
        "getDeviceTelemetry", {"deviceId": "OC-1", "limit": 1}
    )
    assert data["OC-1"]["metrics"] == sample


def test_missing_measures_are_removed_without_losing_reboot_aliases():
    errors = [{"timestamp": "2026-11-23T08:00:00Z", "time": "2026-11-23T08:00:00Z"}]
    coordinator = Mock()
    coordinator.data = {
        "OC-1": {
            "errors": errors,
            "metrics": [{"measures": {"tcpu": 40, "wifi_rssi": -58}}],
        }
    }
    sensor = OnlyCatErrorSensor(coordinator, Device("OC-1", "OnlyCat"), Mock())
    sensor.async_write_ha_state = Mock()
    sensor._handle_coordinator_update()
    assert sensor.extra_state_attributes["wifi_rssi"] == -58
    coordinator.data["OC-1"]["metrics"] = [{"measures": {"tcpu": 42}}]
    sensor._handle_coordinator_update()
    assert sensor.extra_state_attributes == {"errors": errors, "tcpu": 42}
    assert sensor.is_on is True
