"""Recent normalized OnlyCat activity sensor."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN
from .data.activity_journal import ActivityJournalEntry
from .data.device import Device
from .data.event_store import EventStore


class OnlyCatRecentActivitySensor(SensorEntity):
    """Expose only the latest safe journal record for one flap."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "onlycat_recent_activity_sensor"
    _attr_icon = "mdi:timeline-clock"

    def __init__(self, device: Device, event_store: EventStore) -> None:
        self.device = device
        self._event_store = event_store
        self._attr_unique_id = f"{device.device_id.replace('-', '_').lower()}_recent_activity"
        self.entity_id = f"sensor.{self._attr_unique_id}"
        self._apply_entry(event_store.activity_journal.latest(device.device_id))

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the sensor to the existing flap device."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.device.device_id)},
            name=self.device.description,
            serial_number=self.device.device_id,
        )

    def _apply_entry(self, entry: ActivityJournalEntry | None) -> None:
        """Update the bounded state and latest-only attributes."""
        if entry is None:
            self._attr_native_value = None
            self._attr_extra_state_attributes = {}
            return
        subject = entry.pet or "Unknown RFID"
        self._attr_native_value = f"{subject} - {entry.activity}"
        self._attr_extra_state_attributes = entry.as_dict()

    @callback
    def _handle_activity(self, entry: ActivityJournalEntry) -> None:
        """Receive a journal update."""
        self._apply_entry(entry)
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Subscribe when added to Home Assistant."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._event_store.add_activity_listener(
                self.device.device_id, self._handle_activity
            )
        )
