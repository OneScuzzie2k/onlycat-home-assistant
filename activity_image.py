"""Authenticated, journal-scoped historical OnlyCat activity images."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import quote

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import ACTIVITY_IMAGE_URL, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_IMAGE_BASE_URL = "https://gateway.onlycat.com/events/"
_DEFAULT_ACTIVITY_FRAME = 1
_FETCH_TIMEOUT_SECONDS = 10
_MAX_IMAGE_BYTES = 5 * 1024 * 1024
_REGISTERED = f"{DOMAIN}_activity_image_view_registered"


def activity_image_path(config_entry_id: str, device_id: str, event_id: int) -> str:
    """Return a safely encoded local path for one journal event image."""
    return ACTIVITY_IMAGE_URL.format(
        config_entry_id=quote(config_entry_id, safe=""),
        device_id=quote(device_id, safe=""),
        event_id=int(event_id),
    )


class OnlyCatActivityImageView(HomeAssistantView):
    """Serve an OnlyCat event frame through Home Assistant authentication."""

    url = ACTIVITY_IMAGE_URL
    name = "api:onlycat:activity_image"
    requires_auth = True

    async def get(
        self,
        request: web.Request,
        config_entry_id: str,
        device_id: str,
        event_id: str,
    ) -> web.Response:
        """Validate journal scope and return one fixed upstream event frame."""
        hass: HomeAssistant = request.app["hass"]
        try:
            parsed_event_id = int(event_id)
        except (TypeError, ValueError) as err:
            raise web.HTTPNotFound from err
        if parsed_event_id < 0 or str(parsed_event_id) != event_id:
            raise web.HTTPNotFound

        entry = hass.config_entries.async_get_entry(config_entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise web.HTTPNotFound
        runtime_data = getattr(entry, "runtime_data", None)
        if runtime_data is None:
            raise web.HTTPNotFound

        allowed_device_ids = {device.device_id for device in runtime_data.devices}
        if device_id not in allowed_device_ids:
            raise web.HTTPNotFound
        if not runtime_data.event_store.activity_journal.contains_event(
            device_id, parsed_event_id
        ):
            raise web.HTTPNotFound
        if not runtime_data.event_store.activity_journal.event_image_available(
            device_id, parsed_event_id
        ):
            raise web.HTTPNotFound

        # The journal deliberately contains no access token or frame metadata.
        # Frame 1 is the existing entity fallback and is still scoped to the exact,
        # validated device/event pair, so it cannot select another event's media.
        upstream_url = (
            f"{_IMAGE_BASE_URL}{quote(device_id, safe='')}/"
            f"{parsed_event_id}/{_DEFAULT_ACTIVITY_FRAME}"
        )
        session = async_get_clientsession(hass)
        try:
            async with session.get(
                upstream_url,
                allow_redirects=False,
                timeout=aiohttp.ClientTimeout(total=_FETCH_TIMEOUT_SECONDS),
            ) as response:
                if response.status == HTTPStatus.NOT_FOUND:
                    raise web.HTTPNotFound
                if response.status != HTTPStatus.OK:
                    raise web.HTTPBadGateway
                if response.content_type.lower() != "image/jpeg":
                    raise web.HTTPBadGateway
                content_length = response.content_length
                if content_length is not None and content_length > _MAX_IMAGE_BYTES:
                    raise web.HTTPBadGateway
                image = await response.read()
        except web.HTTPException:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug(
                "Unable to retrieve OnlyCat image for event %s", parsed_event_id
            )
            raise web.HTTPBadGateway from err

        if not image or len(image) > _MAX_IMAGE_BYTES:
            raise web.HTTPBadGateway
        return web.Response(
            body=image,
            content_type="image/jpeg",
            headers={
                "Cache-Control": "private, max-age=240",
                "X-Content-Type-Options": "nosniff",
            },
        )


def async_register_activity_image_view(hass: HomeAssistant) -> None:
    """Register the authenticated image view once per HA runtime."""
    if hass.data.get(_REGISTERED):
        return
    hass.http.register_view(OnlyCatActivityImageView())
    hass.data[_REGISTERED] = True
