"""
Custom integration to integrate OnlyCat with Home Assistant.

For more details about this integration, please refer to
https://github.com/OnlyCatAI/onlycat-home-assistant
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .activity_image import async_register_activity_image_view
from .activity_video import async_register_activity_video
from .api import OnlyCatApiClient, OnlyCatApiClientError
from .const import (
    CONF_ENABLE_DEBUG_LOGGING,
    RECONNECT_EVENT_RECONCILE_LIMIT,
    SYNC_MONITOR_INTERVAL_SECONDS,
)
from .coordinator import OnlyCatDataUpdateCoordinator
from .data.__init__ import OnlyCatConfigEntry, OnlyCatData
from .data.device import Device
from .data.event_store import EventStore
from .data.event_summary import SubEvent
from .debug_trace import configure_debug_trace
from .services import async_setup_services
from .websocket_api import async_register_activity_journal_websocket

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.DEVICE_TRACKER,
    Platform.BUTTON,
    Platform.SENSOR,
    Platform.IMAGE,
    Platform.CAMERA,
]
_LOGGER = logging.getLogger(__name__)
CONNECT_RETRY_DELAY_SECONDS = 30


def _apply_runtime_logging(settings: dict[str, object]) -> None:
    """Apply runtime logging level toggle for this integration namespace."""
    debug_enabled = bool(settings.get(CONF_ENABLE_DEBUG_LOGGING, False))
    level = logging.DEBUG if debug_enabled else logging.NOTSET
    logging.getLogger("custom_components.onlycat").setLevel(level)


# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(
    hass: HomeAssistant,
    entry: OnlyCatConfigEntry,
) -> bool:
    """Set up this integration using UI."""
    default_settings = {
        "ignore_flap_motion_rules": False,
        "ignore_motion_sensor_rules": False,
        "poll_interval_hours": 1,
        "enable_detailed_metrics": False,
        "enable_debug_logging": False,
        "event_retry_delays_seconds": "1,3,6",
        "final_event_retry_delays_seconds": "0,1,3",
    }
    settings = default_settings | entry.data.get("settings", {})
    _apply_runtime_logging(settings)
    configure_debug_trace(
        hass.config.path("custom_components", "onlycat", "onlycat_activity.jsonl"),
        bool(settings.get(CONF_ENABLE_DEBUG_LOGGING, False)),
    )
    client = OnlyCatApiClient(
        api_key=entry.data["api_key"], session=async_get_clientsession(hass)
    )
    entry.runtime_data = OnlyCatData(
        client=client,
        devices=[],
        pets=[],
        event_store=EventStore(
            api_client=client,
            settings=settings,
            hass=hass,
            storage_key=f"onlycat.activity.{entry.entry_id}",
        ),
        settings=settings,
        coordinator=OnlyCatDataUpdateCoordinator(hass=hass, config_entry=entry),
    )
    await entry.runtime_data.event_store.async_initialize()
    async_register_activity_image_view(hass)
    async_register_activity_video(hass)
    async_register_activity_journal_websocket(hass)
    await async_setup_services(hass)
    _register_connection_state_listeners(entry)

    initial_connect_succeeded = await _async_bootstrap_entry(entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if initial_connect_succeeded:
        await _run_initial_event_listeners(entry)
        entry.runtime_data.event_store.enable_activity_emission()
        _start_health_monitor(hass, entry)
    else:
        _schedule_connect_retry(hass, entry)

    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    entry.async_on_unload(lambda: _cancel_connect_retry(entry))
    entry.async_on_unload(lambda: _cancel_health_monitor(entry))
    return True


async def _async_bootstrap_entry(entry: OnlyCatConfigEntry) -> bool:
    """Connect and initialize runtime data for an entry."""
    try:
        await entry.runtime_data.client.connect()
    except OnlyCatApiClientError:
        _LOGGER.warning(
            "OnlyCat initial socket connection failed for entry %s; "
            "integration setup will continue and retry in the background",
            entry.entry_id,
        )
        return False

    await _initialize_devices(entry)
    await _initialize_pets(entry)
    await entry.runtime_data.coordinator.async_config_entry_first_refresh()
    _register_runtime_event_listeners(entry)
    await _refresh_subscriptions(entry, None)

    async def refresh_subscriptions_listener(*args: object) -> None:
        await _refresh_subscriptions(entry, args[0] if args else None)

    entry.runtime_data.client.add_event_listener(
        "connect", refresh_subscriptions_listener
    )
    entry.runtime_data.client.add_event_listener(
        "userUpdate", refresh_subscriptions_listener
    )
    return True


def _register_runtime_event_listeners(entry: OnlyCatConfigEntry) -> None:
    """Register runtime listeners that depend on an active socket connection."""
    entry.runtime_data.client.add_event_listener(
        "deviceEventUpdate", entry.runtime_data.event_store.on_device_event_update
    )
    entry.runtime_data.client.add_event_listener(
        "eventUpdate", entry.runtime_data.event_store.on_event_update
    )
    entry.runtime_data.client.add_event_listener(
        "getEvent", entry.runtime_data.event_store.on_get_event
    )
    entry.runtime_data.client.add_event_listener(
        "getEventSummary", entry.runtime_data.event_store.on_get_event_summary
    )
    entry.runtime_data.client.add_event_listener(
        "eventSummaryUpdate", entry.runtime_data.event_store.on_event_summary_update
    )


def _register_connection_state_listeners(entry: OnlyCatConfigEntry) -> None:
    """Register listeners that mirror socket state into Home Assistant."""
    entry.runtime_data.client.add_event_listener(
        "connectionState", entry.runtime_data.event_store.on_connection_state
    )
    entry.runtime_data.client.add_event_listener(
        "connectionState", entry.runtime_data.coordinator.on_connection_state
    )


async def _refresh_subscriptions(entry: OnlyCatConfigEntry, args: dict | None) -> None:
    """Refresh device subscriptions after a successful connection."""
    _LOGGER.debug("Refreshing subscriptions, caused by event: %s", args)
    if not await entry.runtime_data.client.wait_until_ready():
        _LOGGER.warning(
            "OnlyCat socket never became ready for subscription refresh on entry %s",
            entry.entry_id,
        )
        return
    for device in entry.runtime_data.devices:
        await entry.runtime_data.client.send_message(
            "getDevice", {"deviceId": device.device_id, "subscribe": True}
        )
        events = await entry.runtime_data.client.send_message(
            "getDeviceEvents", {"deviceId": device.device_id, "subscribe": True}
        )
        if events:
            events.sort(
                key=lambda e: datetime.fromisoformat(
                    e.get(
                        "timestamp",
                        None,
                    )
                    or datetime.min.replace(tzinfo=UTC).isoformat()
                ),
                reverse=True,
            )
        if not events:
            continue
        if not entry.runtime_data.event_store.activity_emission_enabled:
            events_to_fetch = events[:1]
        else:
            last_event_id = entry.runtime_data.event_store.last_event_id(
                device.device_id
            )
            events_to_fetch = [
                event
                for event in reversed(events)
                if "eventId" in event
                and (last_event_id is None or event["eventId"] >= last_event_id)
            ][-RECONNECT_EVENT_RECONCILE_LIMIT:]
        for event in events_to_fetch:
            if "eventId" in event:
                await entry.runtime_data.event_store.send_get_event_message(
                    device.device_id, event["eventId"], subscribe=False
                )
    await _refresh_pet_snapshots(entry)
    for pet in entry.runtime_data.event_store.get_pets():
        await entry.runtime_data.event_store.run_pet_listeners(pet.rfid_code)


async def _run_initial_event_listeners(entry: OnlyCatConfigEntry) -> None:
    """Run deferred event listeners once startup data has been loaded."""
    for device in entry.runtime_data.devices:
        await entry.runtime_data.event_store.run_event_listeners(device.device_id)
    for pet in entry.runtime_data.event_store.get_pets():
        await entry.runtime_data.event_store.run_pet_listeners(pet.rfid_code)


def _schedule_connect_retry(hass: HomeAssistant, entry: OnlyCatConfigEntry) -> None:
    """Schedule background reconnect attempts for a failed initial setup."""
    if (
        entry.runtime_data.connect_retry_task
        and not entry.runtime_data.connect_retry_task.done()
    ):
        return
    entry.runtime_data.connect_retry_task = hass.async_create_background_task(
        _async_retry_connect(hass, entry),
        f"onlycat_retry_connect_{entry.entry_id}",
    )


def _cancel_connect_retry(entry: OnlyCatConfigEntry) -> None:
    """Cancel any pending background reconnect task."""
    if (
        entry.runtime_data.connect_retry_task
        and not entry.runtime_data.connect_retry_task.done()
    ):
        entry.runtime_data.connect_retry_task.cancel()
    entry.runtime_data.connect_retry_task = None


async def _async_retry_connect(hass: HomeAssistant, entry: OnlyCatConfigEntry) -> None:
    """Retry an initial socket connection in the background."""
    while True:
        await asyncio.sleep(CONNECT_RETRY_DELAY_SECONDS)
        try:
            await entry.runtime_data.client.connect()
        except OnlyCatApiClientError:
            _LOGGER.debug(
                "OnlyCat background reconnect attempt failed for entry %s",
                entry.entry_id,
            )
            continue
        _LOGGER.info(
            "OnlyCat socket reconnected for entry %s; scheduling integration reload",
            entry.entry_id,
        )
        hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
        return


async def _initialize_devices(entry: OnlyCatConfigEntry) -> None:
    device_ids = (
        device["deviceId"]
        for device in await entry.runtime_data.client.send_message(
            "getDevices", {"subscribe": True}
        )
    )
    for device_id in device_ids:
        device = Device.from_api_response(
            await entry.runtime_data.client.send_message(
                "getDevice", {"deviceId": device_id, "subscribe": True}
            ),
            entry,
        )
        entry.runtime_data.devices.append(device)

    for device in entry.runtime_data.devices:
        device.settings = entry.runtime_data.settings
        entry.runtime_data.client.add_event_listener(
            "deviceUpdate", device.handle_device_update
        )
        entry.runtime_data.client.add_event_listener(
            "getDevice", device.update_device_from_api
        )
        entry.runtime_data.client.add_event_listener(
            "getDeviceTransitPolicy", device.update_device_transit_policy_from_api
        )
        if device.device_transit_policy_id is not None:
            await entry.runtime_data.client.send_message(
                "getDeviceTransitPolicy",
                {"deviceTransitPolicyId": device.device_transit_policy_id},
            )


async def _initialize_pets(entry: OnlyCatConfigEntry) -> None:
    await _refresh_pet_snapshots(entry)


async def _refresh_pet_snapshots(entry: OnlyCatConfigEntry) -> None:
    """Refresh latest RFID-derived pet state from the API."""
    await _refresh_pet_snapshots_with_notify(entry, notify=False, reason="startup")


async def _refresh_pet_snapshots_with_notify(
    entry: OnlyCatConfigEntry,
    *,
    notify: bool,
    reason: str,
) -> int:
    """Refresh latest RFID-derived pet state from the API."""
    changed_pets = 0
    for device in entry.runtime_data.devices:
        rfids = (
            await entry.runtime_data.client.send_message(
                "getLastSeenRfidCodesByDevice", {"deviceId": device.device_id}
            )
            or []
        )
        last_seens = (
            await entry.runtime_data.client.send_message(
                "getRfidLastSeenByDevice", {"deviceId": device.device_id}
            )
            or []
        )
        last_seen_rfids = {last_seen["rfidCode"]: last_seen for last_seen in last_seens}
        for rfid in rfids:
            rfid_code = rfid["rfidCode"]
            pet = entry.runtime_data.event_store.find_pet_by_rfid(rfid_code)
            label = pet.label if pet is not None else None
            if not entry.runtime_data.event_store.is_valid_profile_label(
                label, rfid_code
            ):
                rfid_profile = await entry.runtime_data.client.send_message(
                    "getRfidProfile",
                    {"deviceId": device.device_id, "rfidCode": rfid_code},
                )
                label = (
                    rfid_profile.get("label")
                    if isinstance(rfid_profile, dict)
                    else None
                )
            if not entry.runtime_data.event_store.is_valid_profile_label(
                label, rfid_code
            ):
                continue
            last_seen_at = None
            last_seen_location = None
            last_subevent = None
            if rfid_code in last_seen_rfids:
                last_seen = last_seen_rfids[rfid_code]
                try:
                    last_seen_at = datetime.fromisoformat(
                        last_seen.get(
                            "eventTimestamp",
                            datetime.min.replace(tzinfo=UTC).isoformat(),
                        )
                    )
                except (TypeError, ValueError):
                    last_seen_at = datetime.min.replace(tzinfo=UTC)
                last_seen_location = last_seen.get("location")
                last_subevent = SubEvent.from_api_response(
                    last_seen.get("lastSubevent", None) or {}
                )
            changed = entry.runtime_data.event_store.apply_pet_snapshot(
                device_id=device.device_id,
                rfid_code=rfid_code,
                label=label,
                last_seen=last_seen_at,
                location=last_seen_location,
                last_subevent=last_subevent,
            )
            if changed:
                changed_pets += 1
                _LOGGER.info(
                    "OnlyCat pet snapshot resynchronised for RFID %s on device %s (%s)",
                    rfid_code,
                    device.device_id,
                    reason,
                )
                if notify:
                    await entry.runtime_data.event_store.run_pet_listeners(rfid_code)
    return changed_pets


async def _refresh_device_snapshots(entry: OnlyCatConfigEntry) -> None:
    """Refresh latest device state from the API."""
    for device in entry.runtime_data.devices:
        await entry.runtime_data.client.send_message(
            "getDevice", {"deviceId": device.device_id, "subscribe": True}
        )


async def async_force_sync_entry(entry: OnlyCatConfigEntry, reason: str) -> int:
    """Force an authoritative device and pet snapshot refresh."""
    async with entry.runtime_data.sync_lock:
        if not entry.runtime_data.client.is_connected:
            _LOGGER.warning(
                "OnlyCat force sync requested while disconnected for entry %s (%s)",
                entry.entry_id,
                reason,
            )
            reconnected = await entry.runtime_data.client.restart_connection(
                f"force sync requested: {reason}"
            )
            if not reconnected:
                raise OnlyCatApiClientError(
                    "OnlyCat socket reconnect failed during force sync"
                )
        _LOGGER.info(
            "Refreshing OnlyCat snapshot state for entry %s (%s)",
            entry.entry_id,
            reason,
        )
        await _refresh_device_snapshots(entry)
        changed_pets = await _refresh_pet_snapshots_with_notify(
            entry,
            notify=True,
            reason=reason,
        )
        await entry.runtime_data.coordinator.async_refresh()
        if changed_pets:
            _LOGGER.info(
                "OnlyCat snapshot refresh resynchronised %s pet state(s) for entry %s",
                changed_pets,
                entry.entry_id,
            )
        else:
            _LOGGER.info(
                "OnlyCat snapshot refresh completed with no pet-state changes for entry %s",
                entry.entry_id,
            )
        return changed_pets


def _start_health_monitor(hass: HomeAssistant, entry: OnlyCatConfigEntry) -> None:
    """Start the background health monitor for an entry."""
    if (
        entry.runtime_data.health_monitor_task
        and not entry.runtime_data.health_monitor_task.done()
    ):
        return
    entry.runtime_data.health_monitor_task = hass.async_create_background_task(
        _async_health_monitor(entry),
        f"onlycat_health_monitor_{entry.entry_id}",
    )


def _cancel_health_monitor(entry: OnlyCatConfigEntry) -> None:
    """Cancel the background health monitor."""
    if (
        entry.runtime_data.health_monitor_task
        and not entry.runtime_data.health_monitor_task.done()
    ):
        entry.runtime_data.health_monitor_task.cancel()
    entry.runtime_data.health_monitor_task = None


async def _async_health_monitor(entry: OnlyCatConfigEntry) -> None:
    """Periodically verify actual Socket.IO transport liveness."""
    interval = timedelta(seconds=SYNC_MONITOR_INTERVAL_SECONDS)
    while True:
        await asyncio.sleep(interval.total_seconds())
        try:
            await _run_health_check(entry)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "OnlyCat health monitor failed for entry %s",
                entry.entry_id,
            )


async def _run_health_check(entry: OnlyCatConfigEntry) -> None:
    """Recover only when there is positive evidence of transport failure."""
    client = entry.runtime_data.client
    if not client.is_connected:
        _LOGGER.warning(
            "OnlyCat health monitor found disconnected socket for entry %s; restarting",
            entry.entry_id,
        )
        await client.publish_connection_state(
            connected=False,
            phase="offline",
            reason="Socket disconnected during health check",
        )
        await client.restart_connection("socket disconnected during health check")
        return
    _LOGGER.debug(
        "OnlyCat transport is connected for entry %s; event age is informational",
        entry.entry_id,
    )


async def async_unload_entry(
    hass: HomeAssistant,
    entry: OnlyCatConfigEntry,
) -> bool:
    """Handle removal of an entry."""
    _cancel_connect_retry(entry)
    _cancel_health_monitor(entry)
    await entry.runtime_data.client.disconnect()
    entry.runtime_data.client.clear_event_listeners()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: OnlyCatConfigEntry,
) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: OnlyCatConfigEntry
) -> bool:
    """Migrate old entry."""
    _LOGGER.info(
        "Migrating configuration from version %s.%s",
        config_entry.version,
        config_entry.minor_version,
    )
    default_settings = {
        "ignore_flap_motion_rules": False,
        "ignore_motion_sensor_rules": False,
        "poll_interval_hours": 1,
        "enable_detailed_metrics": False,
        "enable_debug_logging": False,
        "event_retry_delays_seconds": "1,3,6",
        "final_event_retry_delays_seconds": "0,1,3",
    }
    new_data = {**config_entry.data}
    new_data["settings"] = default_settings | config_entry.data.get("settings", {})
    if "api_key" not in new_data and "token" in new_data:
        new_data["api_key"] = new_data.pop("token")
    hass.config_entries.async_update_entry(
        config_entry, data=new_data, minor_version=1, version=4
    )
    return True
