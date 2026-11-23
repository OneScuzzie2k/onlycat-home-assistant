"""Sensor platform for OnlyCat."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo

from .api import OnlyCatConnectionState
from .const import DOMAIN
from .data.device import DeviceUpdate

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from . import Device
    from .api import OnlyCatApiClient
    from .data.event_store import EventStore

ENTITY_DESCRIPTION = BinarySensorEntityDescription(
    key="OnlyCat",
    name="Connectivity",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="onlycat_connection_sensor",
)


class OnlyCatConnectionSensor(BinarySensorEntity):
    """OnlyCat Sensor class."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info to map to a device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.device_id)},
            name=self.device.description,
            serial_number=self.device.device_id,
        )

    def __init__(
        self,
        device: Device,
        api_client: OnlyCatApiClient,
        event_store: EventStore | None = None,
    ) -> None:
        """Initialize the sensor class."""
        self.entity_description = ENTITY_DESCRIPTION
        self._attr_is_on = (
            device.connectivity.connected if device.connectivity is not None else None
        )
        self._attr_available = True
        self._attr_raw_data = None
        self.device = device
        self._event_store = event_store
        self._socket_state = api_client.connection_state
        self._attr_unique_id = (
            device.device_id.replace("-", "_").lower() + "_connectivity"
        )
        self.entity_id = "binary_sensor." + self._attr_unique_id

        self._update_attributes()
        self._api_client = api_client

    async def async_added_to_hass(self) -> None:
        """Subscribe after HA adds the entity and release listeners on removal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._api_client.add_event_listener("deviceUpdate", self.on_device_update)
        )
        self.async_on_remove(
            self._api_client.add_event_listener(
                "connectionState", self.on_connection_state
            )
        )
        if self._event_store is not None:
            self.async_on_remove(
                self._event_store.add_event_listener(
                    self.device.device_id, self.on_event_update
                )
            )
        self._socket_state = self._api_client.connection_state
        self._update_attributes()

    async def on_event_update(self, event: object) -> None:
        """Refresh informational event-age attributes."""
        self._update_attributes()
        self.async_write_ha_state()

    async def on_device_update(self, data: dict) -> None:
        """Handle device update event."""
        if data["deviceId"] != self.device.device_id:
            return

        device_update = DeviceUpdate.from_api_response(data)

        self._attr_raw_data = str(data)
        if device_update.body.connectivity:
            self.device.connectivity = device_update.body.connectivity
            self._attr_is_on = device_update.body.connectivity.connected
            self._update_attributes()
        self.async_write_ha_state()

    async def on_connection_state(self, state: OnlyCatConnectionState) -> None:
        """Handle a live Socket.IO connection-state update."""
        self._socket_state = state
        self._update_attributes()
        self.async_write_ha_state()

    def _update_attributes(
        self,
        state: OnlyCatConnectionState | None = None,
    ) -> None:
        """Update diagnostic attributes for socket and device connectivity."""
        state = state or self._socket_state
        self._attr_extra_state_attributes = {
            "socket_connected": state.connected,
            "socket_phase": state.phase,
            "socket_disconnect_reason": state.reason,
            "socket_error": state.error,
            "reconnect_attempt": state.reconnect_attempt,
            "reconnect_delay": state.reconnect_delay,
            "account_authorized": state.authorized,
            "flap_connected": (
                self.device.connectivity.connected
                if self.device.connectivity is not None
                else None
            ),
            "flap_disconnect_reason": (
                self.device.connectivity.disconnect_reason
                if self.device.connectivity is not None
                else None
            ),
            "flap_connectivity_timestamp": (
                self.device.connectivity.timestamp
                if self.device.connectivity is not None
                else None
            ),
            "last_event_received": (
                self._event_store.last_event_received_at
                if self._event_store is not None
                else None
            ),
            "last_event_id": (
                self._event_store.last_event_id(self.device.device_id)
                if self._event_store is not None
                else None
            ),
        }
        # Retain the 2.0.7.1 names for existing templates and dashboards.
        self._attr_extra_state_attributes.update(
            {
                "socketConnected": self._attr_extra_state_attributes[
                    "socket_connected"
                ],
                "socketPhase": self._attr_extra_state_attributes["socket_phase"],
                "socketDisconnectReason": self._attr_extra_state_attributes[
                    "socket_disconnect_reason"
                ],
                "socketError": self._attr_extra_state_attributes["socket_error"],
                "reconnectAttempt": self._attr_extra_state_attributes[
                    "reconnect_attempt"
                ],
                "reconnectDelay": self._attr_extra_state_attributes["reconnect_delay"],
                "deviceConnected": self._attr_extra_state_attributes["flap_connected"],
                "deviceDisconnectReason": self._attr_extra_state_attributes[
                    "flap_disconnect_reason"
                ],
                "deviceConnectivityTimestamp": self._attr_extra_state_attributes[
                    "flap_connectivity_timestamp"
                ],
            }
        )
