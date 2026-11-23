"""Provides services for OnlyCat."""

from __future__ import annotations

import json
import logging

import voluptuous as vol
from homeassistant.const import STATE_HOME, STATE_NOT_HOME
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from custom_components.onlycat.api import OnlyCatApiClientCommunicationError
from custom_components.onlycat.data import OnlyCatConfigEntry

from .const import DOMAIN
from .device_tracker import OnlyCatPetTracker

_LOGGER = logging.getLogger(__name__)


def _get_pet_tracker_entity(call: ServiceCall) -> OnlyCatPetTracker:
    """Get the pet tracker entity from the service call."""
    device_tracker_id: str = call.data["device_tracker"]
    entity_component = call.hass.data.get("entity_components", {}).get("device_tracker")
    if not entity_component:
        error = "Device tracker component not found"
        raise ServiceValidationError(error)
    entity_obj = entity_component.get_entity(device_tracker_id)
    if not entity_obj:
        error = f"Entity {device_tracker_id} not found"
        raise ServiceValidationError(error)
    if not isinstance(entity_obj, OnlyCatPetTracker):
        error = f"Entity {device_tracker_id} is not an OnlyCatPetTracker entity"
        raise ServiceValidationError(error)
    return entity_obj


def _get_loaded_entry(hass: HomeAssistant) -> OnlyCatConfigEntry:
    """Return the first loaded OnlyCat config entry."""
    return _get_loaded_entries(hass)[0]


def _get_loaded_entries(hass: HomeAssistant) -> list[OnlyCatConfigEntry]:
    """Return loaded OnlyCat config entries."""
    entries = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.runtime_data is not None
    ]
    if entries:
        return entries
    error = "OnlyCat integration is not loaded"
    raise ServiceValidationError(error)


async def async_setup_services(hass: HomeAssistant) -> None:
    """Create services for OnlyCat."""
    if not hass.services.has_service(DOMAIN, "set_pet_location"):
        hass.services.async_register(
            DOMAIN,
            "set_pet_location",
            async_handle_set_pet_presence,
            schema=vol.Schema(
                {
                    vol.Required("device_tracker"): cv.entity_id,
                    vol.Required("location"): cv.string,
                }
            ),
        )
    if not hass.services.has_service(DOMAIN, "toggle_pet_location"):
        hass.services.async_register(
            DOMAIN,
            "toggle_pet_location",
            async_handle_toggle_pet_presence,
            schema=vol.Schema(
                {
                    vol.Required("device_tracker"): cv.entity_id,
                }
            ),
        )
    if not hass.services.has_service(DOMAIN, "update_device_policy"):
        hass.services.async_register(
            DOMAIN,
            "update_device_policy",
            async_handle_update_device_policy,
            schema=vol.Schema(
                {
                    vol.Required("policy_data"): cv.string,
                }
            ),
        )
    if not hass.services.has_service(DOMAIN, "force_sync"):
        hass.services.async_register(
            DOMAIN,
            "force_sync",
            async_handle_force_sync,
        )


async def async_handle_set_pet_presence(call: ServiceCall) -> ServiceResponse:
    """Handle the set presence service call."""
    location: str = call.data["location"]
    entity_obj = _get_pet_tracker_entity(call)
    new_state = STATE_HOME if location.lower() == "home" else STATE_NOT_HOME
    await entity_obj.manual_update_location(new_state)
    _LOGGER.info("Set %s presence to: %s", entity_obj.entity_id, location)


async def async_handle_toggle_pet_presence(call: ServiceCall) -> ServiceResponse:
    """Handle the toggle presence service call."""
    entity_obj = _get_pet_tracker_entity(call)
    current_state = entity_obj.state
    new_state = STATE_NOT_HOME if current_state == STATE_HOME else STATE_HOME
    await entity_obj.manual_update_location(new_state)
    _LOGGER.info("Toggled %s presence to: %s", entity_obj.entity_id, new_state)


async def async_handle_update_device_policy(
    call: ServiceCall,
) -> ServiceResponse:
    """Handle the set device policy service call."""
    entry = _get_loaded_entry(call.hass)
    policy_data: str = call.data["policy_data"]
    policy_dict = json.loads(policy_data)
    try:
        response = await entry.runtime_data.client.send_message(
            "updateDeviceTransitPolicy", policy_dict
        )
    except OnlyCatApiClientCommunicationError as exception:
        raise ServiceValidationError(
            "OnlyCat socket is not connected; try again after the integration reconnects"
        ) from exception
    await entry.runtime_data.coordinator.async_refresh()
    _LOGGER.info("Updated device policy %s: %s", policy_data, response)


async def async_handle_force_sync(call: ServiceCall) -> ServiceResponse:
    """Handle a force-sync service call."""
    from . import async_force_sync_entry

    entries = _get_loaded_entries(call.hass)
    for entry in entries:
        await async_force_sync_entry(entry, reason="manual service force_sync")
    _LOGGER.info("Completed OnlyCat force_sync for %s loaded entries", len(entries))
