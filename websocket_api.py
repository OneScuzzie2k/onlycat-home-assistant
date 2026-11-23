"""Read-only WebSocket access to the sanitized OnlyCat activity journal."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback

from .activity_image import activity_image_path
from .const import (
    ACTIVITY_IMAGE_SIGNED_URL_TTL_SECONDS,
    ACTIVITY_JOURNAL_DEFAULT_PAGE_SIZE,
    ACTIVITY_JOURNAL_MAX_PAGE_SIZE,
    ACTIVITY_JOURNAL_WS_COMMAND,
    DOMAIN,
)

_REGISTERED = f"{DOMAIN}_activity_journal_websocket_registered"


@callback
@websocket_api.websocket_command(
    {
        vol.Required("type"): ACTIVITY_JOURNAL_WS_COMMAND,
        vol.Required("config_entry_id"): str,
        vol.Optional("device_id"): str,
        vol.Optional("limit", default=ACTIVITY_JOURNAL_DEFAULT_PAGE_SIZE): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=ACTIVITY_JOURNAL_MAX_PAGE_SIZE)
        ),
        vol.Optional("before"): str,
    }
)
def websocket_activity_journal(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return one read-only page scoped to a loaded OnlyCat config entry."""
    config_entry = hass.config_entries.async_get_entry(msg["config_entry_id"])
    if config_entry is None or config_entry.domain != DOMAIN:
        connection.send_error(msg["id"], "not_found", "OnlyCat entry not found")
        return
    runtime_data = getattr(config_entry, "runtime_data", None)
    if runtime_data is None:
        connection.send_error(msg["id"], "not_loaded", "OnlyCat entry is not loaded")
        return
    device_id = msg.get("device_id")
    allowed_device_ids = {device.device_id for device in runtime_data.devices}
    if device_id is not None and device_id not in allowed_device_ids:
        connection.send_error(msg["id"], "not_found", "OnlyCat device not found")
        return
    entries, next_cursor = runtime_data.event_store.activity_journal.page(
        device_id=device_id,
        limit=msg["limit"],
        before=msg.get("before"),
    )
    refresh_token_id = getattr(connection, "refresh_token_id", None)
    for journal_entry in entries:
        if journal_entry.get("image_available") and refresh_token_id:
            path = activity_image_path(
                msg["config_entry_id"],
                journal_entry["device_id"],
                journal_entry["event_id"],
            )
            journal_entry["image_url"] = async_sign_path(
                hass,
                path,
                timedelta(seconds=ACTIVITY_IMAGE_SIGNED_URL_TTL_SECONDS),
                refresh_token_id=refresh_token_id,
            )
    connection.send_result(
        msg["id"],
        {
            "entries": entries,
            "next_cursor": next_cursor,
            "count": len(entries),
        },
    )


@callback
def async_register_activity_journal_websocket(hass: HomeAssistant) -> None:
    """Register the authenticated command once per Home Assistant runtime."""
    if hass.data.get(_REGISTERED):
        return
    websocket_api.async_register_command(hass, websocket_activity_journal)
    hass.data[_REGISTERED] = True
