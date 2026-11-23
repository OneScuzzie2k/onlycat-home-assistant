"""Sensor platform for OnlyCat."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo

from .api import OnlyCatConnectionState
from .const import DOMAIN
from .data.event import Event

if TYPE_CHECKING:
    from .data.device import Device
    from .data.event import Event
    from .data.event_store import EventStore
    from .data.event_summary import EventSummary

ENTITY_DESCRIPTION = SensorEntityDescription(
    key="OnlyCat",
    name="Last RFID-tagged event",
    device_class=SensorDeviceClass.TIMESTAMP,
    entity_category=EntityCategory.DIAGNOSTIC,
    translation_key="onlycat_last_rfid_event_sensor",
)


class OnlyCatLastRfidEventSensor(SensorEntity):
    """OnlyCat last RFID-tagged event timestamp sensor."""

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
        event_store: EventStore,
    ) -> None:
        """Initialize the sensor class."""
        self.entity_description = ENTITY_DESCRIPTION
        self._attr_native_value = None
        self._attr_extra_state_attributes = {}
        self.device: Device = device
        self._attr_unique_id = (
            device.device_id.replace("-", "_").lower() + "_last_rfid_event"
        )
        self._event_store = event_store
        self.entity_id = "sensor." + self._attr_unique_id
        self._attr_available = self._event_store.connection_state.connected

    async def async_added_to_hass(self) -> None:
        """Subscribe after HA adds the entity and release listeners on removal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._event_store.add_event_listener(
                self.device.device_id, self.on_event_update
            )
        )
        self.async_on_remove(
            self._event_store.add_event_summary_listener(
                self.device.device_id, self.on_event_summary_update
            )
        )
        self.async_on_remove(
            self._event_store.add_connection_listener(self.on_connection_state)
        )
        self._attr_available = self._event_store.connection_state.connected

    async def on_event_update(self, event: Event) -> None:
        """Handle merged event updates from EventStore."""
        if not event or event.device_id != self.device.device_id:
            return
        self.determine_new_state(event)
        self.async_write_ha_state()

    async def on_event_summary_update(self, summary: EventSummary) -> None:
        """Use summary RfidCode values when the full event has no RFID list."""
        if not summary or summary.device_id != self.device.device_id:
            return
        rfid_codes = [
            subevent.rfid_code for subevent in summary.subevents if subevent.rfid_code
        ]
        if not rfid_codes:
            return
        if summary.timestamp is not None:
            self._attr_native_value = summary.timestamp
        self._attr_extra_state_attributes = {
            "eventId": summary.event_id,
            "rfidCode": rfid_codes[-1],
            # Compatibility for existing templates; rfidCode is authoritative.
            "rfidCodes": rfid_codes,
        }
        self.async_write_ha_state()

    def determine_new_state(self, event: Event) -> None:
        """Update timestamp from the latest event that includes RFID codes."""
        if not event or not event.timestamp or not event.rfid_codes:
            return

        self._attr_native_value = event.timestamp
        self._attr_extra_state_attributes = {
            "eventId": event.event_id,
            "rfidCodes": event.rfid_codes,
            "eventTriggerSource": event.event_trigger_source.name
            if event.event_trigger_source
            else None,
            "eventClassification": event.event_classification.name
            if event.event_classification
            else None,
        }

    async def on_connection_state(self, state: OnlyCatConnectionState) -> None:
        """Handle Socket.IO connection-state changes."""
        self._attr_available = state.connected
        self.async_write_ha_state()
