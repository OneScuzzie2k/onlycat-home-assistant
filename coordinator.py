"""Coordinator for OnlyCat integration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data.__init__ import OnlyCatConfigEntry
    from .data.device import Device
import logging
from datetime import timedelta

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OnlyCatApiClientCommunicationError, OnlyCatConnectionState
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _normalise_reboot_logs(logs: object) -> list[dict]:
    """Return reboot records with the v2.0.7 shape and legacy aliases."""
    if not isinstance(logs, list):
        return []
    normalised = []
    for log in logs:
        if not isinstance(log, dict):
            continue
        record = dict(log)
        # Keep old automations working while timestamp/new reboot fields become
        # authoritative. deviceId and message already exist in both formats.
        if "timestamp" in record:
            record.setdefault("time", record["timestamp"])
        record.setdefault("measureName", "message")
        normalised.append(record)
    return normalised


class OnlyCatDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching polling OnlyCat sensor data."""

    def __init__(self, hass: HomeAssistant, config_entry: OnlyCatConfigEntry) -> None:
        """Initialize global OnlyCat data updater."""
        interval = timedelta(
            hours=config_entry.data["settings"].get("poll_interval_hours", 1)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=interval,
        )
        self._socket_state = OnlyCatConnectionState(connected=False, phase="initial")

    @property
    def socket_connected(self) -> bool:
        """Return whether the OnlyCat Socket.IO client is live."""
        return self._socket_state.connected

    @property
    def socket_state(self) -> OnlyCatConnectionState:
        """Return the latest known socket state."""
        return self._socket_state

    async def on_connection_state(self, state: OnlyCatConnectionState) -> None:
        """Record transport state without discarding last-known coordinator data."""
        self._socket_state = state
        self.async_update_listeners()

    async def fetch_device_transit_policies(self, device: Device) -> None:
        """Fetch transit policies for a device and update the device object."""
        if not self.config_entry.runtime_data.client:
            return
        transit_policies = await self.config_entry.runtime_data.client.send_message(
            "getDeviceTransitPolicies", {"deviceId": device.device_id}
        )
        if transit_policies is None:
            return
        for policy in transit_policies:
            await self.config_entry.runtime_data.client.send_message(
                "getDeviceTransitPolicy",
                {"deviceTransitPolicyId": policy["deviceTransitPolicyId"]},
            )

    async def _async_update_data(self) -> dict:
        """Fetch data."""
        _LOGGER.debug("Updating OnlyCat coordinator data")
        if not self.config_entry.runtime_data.client.is_connected:
            raise UpdateFailed("OnlyCat socket is not connected")
        data = {}
        try:
            for device in self.config_entry.runtime_data.devices:
                await self.fetch_device_transit_policies(device)
                data[device.device_id] = {}
                try:
                    reboot_logs = (
                        await self.config_entry.runtime_data.client.send_message(
                            "getDeviceRebootLogs",
                            {
                                "deviceId": device.device_id,
                                "limit": 100,
                                "hours": self.config_entry.data["settings"].get(
                                    "poll_interval_hours", 1
                                ),
                            },
                        )
                    )
                    data[device.device_id]["errors"] = _normalise_reboot_logs(
                        reboot_logs
                    )
                except TimeoutError:
                    _LOGGER.exception("Error fetching OnlyCat errors: %s")
                    data[device.device_id]["errors"] = []
                if self.config_entry.data["settings"].get(
                    "enable_detailed_metrics", False
                ):
                    try:
                        data[device.device_id][
                            "metrics"
                        ] = await self.config_entry.runtime_data.client.send_message(
                            "getDeviceTelemetryMetrics",
                            {
                                "deviceId": device.device_id,
                            },
                        )
                    except TimeoutError:
                        _LOGGER.exception("Error fetching OnlyCat metrics: %s")
        except OnlyCatApiClientCommunicationError as exception:
            raise UpdateFailed("OnlyCat socket is not connected") from exception
        return data
