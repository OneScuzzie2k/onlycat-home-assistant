"""Authenticated, journal-scoped historical OnlyCat video proxy."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import aiohttp
import voluptuous as vol
from aiohttp import web
from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OnlyCatApiClientError
from .const import (
    ACTIVITY_VIDEO_SIGNED_URL_TTL_SECONDS,
    ACTIVITY_VIDEO_STATUS_WS_COMMAND,
    ACTIVITY_VIDEO_URL,
    DOMAIN,
)

if TYPE_CHECKING:
    from .data import OnlyCatData

_LOGGER = logging.getLogger(__name__)

_VIDEO_BASE_URL = "https://gateway.onlycat.com/sharing/video/"
_VIDEO_CONTENT_TYPE = "video/mp4"
_MAX_VIDEO_BYTES = 25 * 1024 * 1024
_VIDEO_TIMEOUT = aiohttp.ClientTimeout(total=30, connect=5, sock_read=20)
_MAX_CONCURRENT_FETCHES = 2
_AVAILABLE_CACHE_SECONDS = 60
_UNAVAILABLE_CACHE_SECONDS = 20
_RUNTIME_KEY = f"{DOMAIN}_activity_video_runtime"
_REGISTERED = f"{DOMAIN}_activity_video_registered"


@dataclass(slots=True)
class _Availability:
    """One credential-free video availability result."""

    available: bool
    expires_at: float


class ActivityVideoRuntime:
    """Bound video work and retain only short-lived availability booleans."""

    def __init__(self) -> None:
        self.semaphore = asyncio.Semaphore(_MAX_CONCURRENT_FETCHES)
        self.availability: dict[tuple[str, str, int], _Availability] = {}

    def cached(
        self, key: tuple[str, str, int], *, now: float | None = None
    ) -> bool | None:
        """Return a live cached result without retaining media credentials."""
        current = time.monotonic() if now is None else now
        item = self.availability.get(key)
        if item is None:
            return None
        if item.expires_at <= current:
            self.availability.pop(key, None)
            return None
        return item.available

    def store(
        self,
        key: tuple[str, str, int],
        available: bool,
        *,
        now: float | None = None,
    ) -> None:
        """Cache only a boolean for a short bounded period."""
        current = time.monotonic() if now is None else now
        ttl = _AVAILABLE_CACHE_SECONDS if available else _UNAVAILABLE_CACHE_SECONDS
        self.availability[key] = _Availability(available, current + ttl)


def activity_video_path(config_entry_id: str, device_id: str, event_id: int) -> str:
    """Return a safely encoded local path for one journal event video."""
    return ACTIVITY_VIDEO_URL.format(
        config_entry_id=quote(config_entry_id, safe=""),
        device_id=quote(device_id, safe=""),
        event_id=int(event_id),
    )


def _runtime(hass: HomeAssistant) -> ActivityVideoRuntime:
    """Return the per-HA runtime controlling cache and concurrency."""
    runtime = hass.data.get(_RUNTIME_KEY)
    if runtime is None:
        runtime = ActivityVideoRuntime()
        hass.data[_RUNTIME_KEY] = runtime
    return runtime


def _validated_runtime_data(
    hass: HomeAssistant,
    config_entry_id: str,
    device_id: str,
    event_id: int,
) -> OnlyCatData:
    """Validate config-entry, device ownership, and journal membership."""
    entry = hass.config_entries.async_get_entry(config_entry_id)
    if entry is None or entry.domain != DOMAIN:
        raise web.HTTPNotFound
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        raise web.HTTPNotFound
    if device_id not in {device.device_id for device in runtime_data.devices}:
        raise web.HTTPNotFound
    if not runtime_data.event_store.activity_journal.contains_event(
        device_id, event_id
    ):
        raise web.HTTPNotFound
    return runtime_data


async def _event_media_credential(
    runtime_data: OnlyCatData, device_id: str, event_id: int
) -> str | None:
    """Retrieve a historical credential without dispatching it to entities."""
    reply: Any = None
    try:
        reply = await runtime_data.client.send_message(
            "getEvent",
            {"deviceId": device_id, "eventId": event_id, "subscribe": False},
            notify_listeners=False,
        )
        if not isinstance(reply, dict):
            return None
        if reply.get("deviceId") != device_id or reply.get("eventId") != event_id:
            return None
        if not isinstance(reply.get("frameCount"), int) or reply["frameCount"] <= 0:
            return None
        value = reply.get("accessToken")
        return value if isinstance(value, str) and value else None
    finally:
        if isinstance(reply, dict):
            reply.pop("accessToken", None)


def _upstream_url(device_id: str, event_id: int, credential: str) -> str:
    """Construct the one fixed HTTPS OnlyCat sharing endpoint."""
    return (
        f"{_VIDEO_BASE_URL}{quote(device_id, safe='')}/{event_id}"
        f"?t={quote(credential, safe='')}"
    )


async def _confirm_available(
    hass: HomeAssistant,
    runtime_data: OnlyCatData,
    device_id: str,
    event_id: int,
) -> bool:
    """Confirm availability without caching or exposing the credential."""
    credential: str | None = None
    upstream_url: str | None = None
    try:
        credential = await _event_media_credential(runtime_data, device_id, event_id)
        if credential is None:
            return False
        upstream_url = _upstream_url(device_id, event_id, credential)
        session = async_get_clientsession(hass)
        async with session.head(
            upstream_url,
            allow_redirects=False,
            timeout=_VIDEO_TIMEOUT,
        ) as response:
            return (
                response.status == HTTPStatus.OK
                and response.content_type.lower() == _VIDEO_CONTENT_TYPE
            )
    except (OnlyCatApiClientError, aiohttp.ClientError, TimeoutError):
        _LOGGER.debug("OnlyCat video status check failed for event %s", event_id)
        return False
    finally:
        credential = None
        upstream_url = None


async def async_video_available(
    hass: HomeAssistant,
    config_entry_id: str,
    device_id: str,
    event_id: int,
) -> bool:
    """Return server-confirmed video availability through a boolean-only cache."""
    runtime_data = _validated_runtime_data(
        hass, config_entry_id, device_id, event_id
    )
    video_runtime = _runtime(hass)
    key = (config_entry_id, device_id, event_id)
    cached = video_runtime.cached(key)
    if cached is not None:
        return cached
    async with video_runtime.semaphore:
        cached = video_runtime.cached(key)
        if cached is not None:
            return cached
        available = await _confirm_available(
            hass, runtime_data, device_id, event_id
        )
        video_runtime.store(key, available)
        return available


def _parse_event_id(value: str) -> int:
    """Parse a canonical non-negative decimal event ID."""
    try:
        event_id = int(value)
    except (TypeError, ValueError) as err:
        raise web.HTTPNotFound from err
    if event_id < 0 or str(event_id) != value:
        raise web.HTTPNotFound
    return event_id


def _range_response(data: bytes, requested: str | None) -> web.Response:
    """Return a complete or standards-compliant single-range MP4 response."""
    headers = {
        "Accept-Ranges": "bytes",
        "Cache-Control": "private, no-store",
        "Content-Disposition": "inline",
        "X-Content-Type-Options": "nosniff",
    }
    if not requested:
        return web.Response(body=data, content_type=_VIDEO_CONTENT_TYPE, headers=headers)
    if not requested.startswith("bytes=") or "," in requested:
        raise web.HTTPRequestRangeNotSatisfiable(
            headers={"Content-Range": f"bytes */{len(data)}"}
        )
    spec = requested[6:]
    try:
        start_text, end_text = spec.split("-", 1)
        if not start_text:
            length = int(end_text)
            if length <= 0:
                raise ValueError
            start = max(0, len(data) - length)
            end = len(data) - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else len(data) - 1
    except (TypeError, ValueError) as err:
        raise web.HTTPRequestRangeNotSatisfiable(
            headers={"Content-Range": f"bytes */{len(data)}"}
        ) from err
    if start < 0 or start >= len(data) or end < start:
        raise web.HTTPRequestRangeNotSatisfiable(
            headers={"Content-Range": f"bytes */{len(data)}"}
        )
    end = min(end, len(data) - 1)
    headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
    return web.Response(
        status=HTTPStatus.PARTIAL_CONTENT,
        body=data[start : end + 1],
        content_type=_VIDEO_CONTENT_TYPE,
        headers=headers,
    )


class OnlyCatActivityVideoView(HomeAssistantView):
    """Serve a historical event clip without exposing its OnlyCat credential."""

    url = ACTIVITY_VIDEO_URL
    name = "api:onlycat:activity_video"
    requires_auth = True

    async def get(
        self,
        request: web.Request,
        config_entry_id: str,
        device_id: str,
        event_id: str,
    ) -> web.Response:
        """Fetch a bounded MP4 in memory and serve an optional local byte range."""
        hass: HomeAssistant = request.app["hass"]
        parsed_event_id = _parse_event_id(event_id)
        runtime_data = _validated_runtime_data(
            hass, config_entry_id, device_id, parsed_event_id
        )
        video_runtime = _runtime(hass)
        credential: str | None = None
        upstream_url: str | None = None
        reply_body: bytes | None = None
        try:
            async with video_runtime.semaphore:
                credential = await _event_media_credential(
                    runtime_data, device_id, parsed_event_id
                )
                if credential is None:
                    raise web.HTTPGone(text="Video no longer available")
                upstream_url = _upstream_url(
                    device_id, parsed_event_id, credential
                )
                session = async_get_clientsession(hass)
                async with session.get(
                    upstream_url,
                    allow_redirects=False,
                    timeout=_VIDEO_TIMEOUT,
                ) as response:
                    if response.status in {
                        HTTPStatus.UNAUTHORIZED,
                        HTTPStatus.FORBIDDEN,
                        HTTPStatus.NOT_FOUND,
                        HTTPStatus.GONE,
                    }:
                        raise web.HTTPGone(text="Video no longer available")
                    if 300 <= response.status < 400:
                        raise web.HTTPBadGateway(text="Video unavailable")
                    if response.status != HTTPStatus.OK:
                        raise web.HTTPBadGateway(text="Video unavailable")
                    if response.content_type.lower() != _VIDEO_CONTENT_TYPE:
                        raise web.HTTPBadGateway(text="Video unavailable")
                    if (
                        response.content_length is not None
                        and response.content_length > _MAX_VIDEO_BYTES
                    ):
                        raise web.HTTPRequestEntityTooLarge(
                            max_size=_MAX_VIDEO_BYTES,
                            actual_size=response.content_length,
                        )
                    data = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        data.extend(chunk)
                        if len(data) > _MAX_VIDEO_BYTES:
                            raise web.HTTPRequestEntityTooLarge(
                                max_size=_MAX_VIDEO_BYTES,
                                actual_size=len(data),
                            )
                    if not data:
                        raise web.HTTPGone(text="Video no longer available")
                    reply_body = bytes(data)
                    data.clear()
            video_runtime.store(
                (config_entry_id, device_id, parsed_event_id), True
            )
            return _range_response(reply_body, request.headers.get("Range"))
        except web.HTTPException:
            raise
        except OnlyCatApiClientError as err:
            _LOGGER.debug(
                "OnlyCat event lookup failed for event %s", parsed_event_id
            )
            raise web.HTTPBadGateway(text="Video unavailable") from err
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug(
                "OnlyCat video retrieval failed for event %s", parsed_event_id
            )
            raise web.HTTPGatewayTimeout(text="Video unavailable") from err
        finally:
            credential = None
            upstream_url = None
            reply_body = None


@websocket_api.websocket_command(
    {
        vol.Required("type"): ACTIVITY_VIDEO_STATUS_WS_COMMAND,
        vol.Required("config_entry_id"): str,
        vol.Required("device_id"): str,
        vol.Required("event_id"): vol.All(vol.Coerce(int), vol.Range(min=0)),
    }
)
@websocket_api.async_response
async def websocket_activity_video_status(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict[str, Any],
) -> None:
    """Return a signed local video path only after server confirmation."""
    try:
        available = await async_video_available(
            hass,
            msg["config_entry_id"],
            msg["device_id"],
            msg["event_id"],
        )
    except web.HTTPNotFound:
        connection.send_error(msg["id"], "not_found", "Video not found")
        return
    result: dict[str, object] = {"available": available}
    refresh_token_id = getattr(connection, "refresh_token_id", None)
    if available and refresh_token_id:
        path = activity_video_path(
            msg["config_entry_id"], msg["device_id"], msg["event_id"]
        )
        result["video_url"] = async_sign_path(
            hass,
            path,
            timedelta(seconds=ACTIVITY_VIDEO_SIGNED_URL_TTL_SECONDS),
            refresh_token_id=refresh_token_id,
        )
    connection.send_result(msg["id"], result)


def async_register_activity_video(hass: HomeAssistant) -> None:
    """Register the authenticated view and status command once per runtime."""
    if hass.data.get(_REGISTERED):
        return
    hass.http.register_view(OnlyCatActivityVideoView())
    websocket_api.async_register_command(hass, websocket_activity_video_status)
    _runtime(hass)
    hass.data[_REGISTERED] = True
