"""Tracker platform for OnlyCat."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from homeassistant.components.device_tracker import (
    SourceType,
    TrackerEntity,
    TrackerEntityDescription,
)
from homeassistant.const import STATE_HOME, STATE_NOT_HOME
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.restore_state import RestoreEntity

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .data import OnlyCatConfigEntry
    from .data.event_store import EventStore
    from .data.pet import Pet


ENTITY_DESCRIPTION = TrackerEntityDescription(
    key="OnlyCat",
    name="Pet Tracker",
    icon="mdi:cat",
    translation_key="onlycat_pet_tracker",
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: OnlyCatConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the tracker platform."""
    event_store = entry.runtime_data.event_store
    tracked_rfids = {pet.rfid_code for pet in event_store.get_pets()}
    async_add_entities(
        OnlyCatPetTracker(pet=pet, event_store=event_store)
        for pet in event_store.get_pets()
    )

    @callback
    def add_named_pet(pet: Pet) -> None:
        """Dynamically add a newly resolved named profile exactly once."""
        if pet.rfid_code in tracked_rfids:
            return
        tracked_rfids.add(pet.rfid_code)
        async_add_entities([OnlyCatPetTracker(pet=pet, event_store=event_store)])

    entry.async_on_unload(event_store.add_pet_discovery_listener(add_named_pet))


class OnlyCatPetTracker(TrackerEntity, RestoreEntity):
    """OnlyCat Tracker class."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_source_type = SourceType.ROUTER

    @property
    def device_info(self) -> DeviceInfo | None:
        """Return device info to map the pet tracker to its flap device when known."""
        if self.pet.device_id is None:
            return None
        from .const import DOMAIN

        return DeviceInfo(
            identifiers={(DOMAIN, self.pet.device_id)},
            serial_number=self.pet.device_id,
        )

    def __init__(
        self,
        pet: Pet,
        event_store: EventStore,
    ) -> None:
        """Initialize the sensor class."""
        self.entity_description = ENTITY_DESCRIPTION
        self._attr_raw_data = None
        self.pet: Pet = pet
        self._event_store = event_store
        self.pet_name = pet.label if pet.label is not None else pet.rfid_code
        self._attr_translation_placeholders = {
            "pet_name": self.pet_name,
        }
        legacy_prefix = (
            pet.device_id.replace("-", "_").lower() + "_"
            if pet.device_id is not None
            else ""
        )
        self._attr_unique_id = legacy_prefix + pet.rfid_code + "_tracker"
        self.entity_id = "device_tracker." + self._attr_unique_id
        self._attr_available = True
        self._attr_location_name = (
            pet.location
            if pet.location in (STATE_HOME, STATE_NOT_HOME)
            else STATE_NOT_HOME
        )
        self._attr_in_zones = (
            ["zone.home"] if self._attr_location_name == STATE_HOME else []
        )
        self._attr_last_seen = pet.last_seen
        self._refresh_extra_state_attributes()

    def _refresh_extra_state_attributes(self) -> None:
        """Expose stable presence timing separate from entity refresh times."""
        location_changed_at = self.pet.location_changed_at
        activity = self.pet.last_activity
        self._attr_extra_state_attributes = {
            "last_seen": self.pet.last_seen.isoformat()
            if self.pet.last_seen is not None
            else None,
            "location_changed_at": location_changed_at,
            "out_since": location_changed_at
            if self.pet.location == STATE_NOT_HOME
            else None,
            "home_since": location_changed_at
            if self.pet.location == STATE_HOME
            else None,
            "last_flap_activity": activity.activity if activity else None,
            "last_flap_action": activity.action if activity else None,
            "last_flap_direction": activity.direction if activity else None,
            "last_flap_activity_timestamp": (
                activity.timestamp.isoformat()
                if activity and activity.timestamp is not None
                else None
            ),
            "last_event_id": activity.event_id if activity else None,
            "last_classification": activity.classification if activity else None,
            "last_trigger_source": activity.trigger_source if activity else None,
            "location_source": self.pet.location_source,
            "location_source_event_id": self.pet.location_source_event_id,
            "last_image_available": activity.image_available if activity else None,
            "last_video_available": activity.video_available if activity else None,
        }

    async def async_added_to_hass(self) -> None:
        """Restore a newer saved presence state across reloads/restarts."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._event_store.add_pet_listener(self.pet.rfid_code, self.on_pet_update)
        )
        # A dynamically discovered tracker can be queued before the activity that
        # discovered it has finished applying.  Refresh from the shared Pet now so
        # its first written state is current even if that listener update was missed.
        self._attr_location_name = (
            self.pet.location
            if self.pet.location in (STATE_HOME, STATE_NOT_HOME)
            else STATE_NOT_HOME
        )
        self._attr_in_zones = (
            ["zone.home"] if self._attr_location_name == STATE_HOME else []
        )
        self._attr_last_seen = self.pet.last_seen
        self._refresh_extra_state_attributes()
        last_state = await self.async_get_last_state()
        if last_state is None or last_state.state not in (
            STATE_HOME,
            STATE_NOT_HOME,
        ):
            return
        restored_last_seen = None
        last_seen_attr = last_state.attributes.get("last_seen")
        if last_seen_attr is not None:
            try:
                restored_last_seen = datetime.fromisoformat(last_seen_attr)
            except (TypeError, ValueError):
                restored_last_seen = None
        if self.pet.last_seen is not None and (
            restored_last_seen is None or restored_last_seen <= self.pet.last_seen
        ):
            return
        self.pet.location = last_state.state
        self.pet.last_seen = restored_last_seen
        self.pet.last_seen_event = None
        self.pet.last_seen_summary = None
        self._event_store.add_pet(self.pet)
        await self._event_store.run_pet_listeners(self.pet.rfid_code)

    async def on_pet_update(self, pet: Pet) -> None:
        """Handle updates to the pet data."""
        if pet.rfid_code != self.pet.rfid_code:
            return
        previous_state = (
            self._attr_location_name,
            self._attr_last_seen,
            dict(self._attr_extra_state_attributes),
        )
        self.pet = pet
        self._attr_location_name = pet.location
        self._attr_in_zones = ["zone.home"] if pet.location == STATE_HOME else []
        self._attr_last_seen = pet.last_seen
        self._refresh_extra_state_attributes()
        if previous_state != (
            self._attr_location_name,
            self._attr_last_seen,
            self._attr_extra_state_attributes,
        ):
            self.async_write_ha_state()

    async def manual_update_location(self, location: str) -> None:
        """Manually override current state of a pets device tracker."""
        if location not in (STATE_HOME, STATE_NOT_HOME):
            _LOGGER.debug("Manual update of location cannot be set to %s", location)
            return
        if self.pet.location == location:
            return
        self.pet.location = location
        self.pet.last_seen = datetime.now(UTC)
        self.pet.location_changed_at = self.pet.last_seen
        self.pet.location_evidence_at = self.pet.last_seen
        self.pet.location_source = "manual"
        self.pet.location_source_event_id = None
        self._attr_location_name = location
        self._attr_in_zones = ["zone.home"] if location == STATE_HOME else []
        self._attr_last_seen = self.pet.last_seen
        self._refresh_extra_state_attributes()
        self.async_write_ha_state()
