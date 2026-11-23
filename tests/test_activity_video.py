"""Security, availability, and range tests for historical activity video."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from custom_components.onlycat.activity_video import (
    _MAX_CONCURRENT_FETCHES,
    ActivityVideoRuntime,
    OnlyCatActivityVideoView,
    activity_video_path,
    async_video_available,
    websocket_activity_video_status,
)
from custom_components.onlycat.api import OnlyCatApiClientError
from custom_components.onlycat.data.activity_journal import (
    ActivityJournal,
    ActivityJournalEntry,
)
from homeassistant.helpers.http import request_handler_factory


def _journal(*event_ids: int) -> ActivityJournal:
    journal = ActivityJournal()
    timestamp = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
    for event_id in event_ids or (42,):
        journal.append(
            ActivityJournalEntry(
                device_id="OC-1",
                event_id=event_id,
                timestamp=timestamp,
                rfid="123",
                pet="Nelson",
                activity="Entered",
                action="TRANSIT",
                direction="INWARD",
                classification="CLEAR",
                trigger_source="OUTDOOR_MOTION",
                first_received_at=timestamp,
                last_received_at=timestamp,
                image_available=True,
                video_available=False,
                location_before="not_home",
                location_after="home",
                location_changed=True,
                location_source="onlycat_activity",
                duplicate_delivery_count=0,
                delivery_sources=["event_summary_update"],
            )
        )
    return journal


def _entry(entry_id: str = "entry-1", device_id: str = "OC-1", *events: int):
    client = AsyncMock()
    client.send_message.return_value = {
        "deviceId": device_id,
        "eventId": events[0] if events else 42,
        "frameCount": 100,
        "accessToken": "media-secret",
    }
    return SimpleNamespace(
        entry_id=entry_id,
        domain="onlycat",
        runtime_data=SimpleNamespace(
            devices=[SimpleNamespace(device_id=device_id)],
            event_store=SimpleNamespace(activity_journal=_journal(*(events or (42,)))),
            client=client,
        ),
    )


def _hass(entries: dict[str, object]):
    return SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(async_get_entry=entries.get),
    )


def _request(hass: object, *, range_header: str | None = None):
    headers = {} if range_header is None else {"Range": range_header}
    return SimpleNamespace(app={"hass": hass}, headers=headers)


class _Content:
    def __init__(self, body: bytes):
        self.body = body

    async def iter_chunked(self, _size: int):
        midpoint = max(1, len(self.body) // 2)
        yield self.body[:midpoint]
        yield self.body[midpoint:]


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        content_type: str = "video/mp4",
        body: bytes = b"0123456789",
        content_length: int | None = None,
    ):
        self.status = status
        self.content_type = content_type
        self.content_length = len(body) if content_length is None else content_length
        self.content = _Content(body)


class _Context:
    def __init__(self, response: _Response | None = None, error: Exception | None = None):
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error:
            raise self.error
        return self.response

    async def __aexit__(self, *_args):
        return None


class _Session:
    def __init__(self, response: _Response | None = None, error: Exception | None = None):
        self.response = response or _Response()
        self.error = error
        self.calls: list[tuple[str, str, dict]] = []

    def get(self, url: str, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return _Context(self.response, self.error)

    def head(self, url: str, **kwargs):
        self.calls.append(("HEAD", url, kwargs))
        return _Context(self.response, self.error)


def test_view_requires_authentication_and_has_no_arbitrary_url() -> None:
    assert OnlyCatActivityVideoView.requires_auth is True
    assert "url" not in inspect.signature(OnlyCatActivityVideoView.get).parameters
    assert activity_video_path("entry/one", "OC/../../two", 42) == (
        "/api/onlycat/activity_video/entry%2Fone/OC%2F..%2F..%2Ftwo/42"
    )


@pytest.mark.asyncio
async def test_unauthenticated_video_request_is_rejected() -> None:
    view = OnlyCatActivityVideoView()
    handler = request_handler_factory(
        SimpleNamespace(is_stopping=False), view, view.get
    )
    with pytest.raises(web.HTTPUnauthorized):
        await handler({})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_id", "device_id", "event_id"),
    [
        ("missing", "OC-1", "42"),
        ("entry-1", "OC-WRONG", "42"),
        ("entry-1", "OC-1", "404"),
        ("entry-1", "OC-1", "bad"),
        ("entry-1", "OC-1", "042"),
        ("entry-2", "OC-1", "42"),
        ("other-domain", "OC-1", "42"),
    ],
)
async def test_video_scope_rejects_invalid_and_cross_entry_access(
    entry_id: str, device_id: str, event_id: str
) -> None:
    entries = {
        "entry-1": _entry("entry-1", "OC-1", 42),
        "entry-2": _entry("entry-2", "OC-2", 42),
        "other-domain": SimpleNamespace(domain="other", runtime_data=None),
    }
    with pytest.raises(web.HTTPNotFound):
        await OnlyCatActivityVideoView().get(
            _request(_hass(entries)), entry_id, device_id, event_id
        )


@pytest.mark.asyncio
async def test_valid_video_uses_internal_credential_and_serves_local_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry("entry-1", "OC-1", 42)
    hass = _hass({"entry-1": entry})
    session = _Session(_Response(body=b"0123456789"))
    monkeypatch.setattr(
        "custom_components.onlycat.activity_video.async_get_clientsession",
        lambda _hass: session,
    )

    response = await OnlyCatActivityVideoView().get(
        _request(hass, range_header="bytes=2-5"), "entry-1", "OC-1", "42"
    )

    assert response.status == 206
    assert response.body == b"2345"
    assert response.headers["Content-Range"] == "bytes 2-5/10"
    entry.runtime_data.client.send_message.assert_awaited_once_with(
        "getEvent",
        {"deviceId": "OC-1", "eventId": 42, "subscribe": False},
        notify_listeners=False,
    )
    method, upstream_url, kwargs = session.calls[0]
    assert method == "GET"
    assert upstream_url.startswith(
        "https://gateway.onlycat.com/sharing/video/OC-1/42?t="
    )
    assert kwargs["allow_redirects"] is False
    client_visible = repr((response.headers, response.body)).lower()
    assert "media-secret" not in client_visible
    assert "gateway.onlycat.com" not in client_visible
    assert "access" not in client_visible
    assert "media-secret" not in repr(entry.runtime_data.event_store.activity_journal.records())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error", "expected"),
    [
        (_Response(status=302), None, web.HTTPBadGateway),
        (_Response(content_type="text/html"), None, web.HTTPBadGateway),
        (_Response(content_length=30 * 1024 * 1024), None, web.HTTPRequestEntityTooLarge),
        (_Response(status=404), None, web.HTTPGone),
        (None, TimeoutError(), web.HTTPGatewayTimeout),
    ],
)
async def test_video_rejects_unsafe_or_unavailable_upstream(
    monkeypatch: pytest.MonkeyPatch,
    response: _Response | None,
    error: Exception | None,
    expected: type[web.HTTPException],
) -> None:
    entry = _entry("entry-1", "OC-1", 42)
    hass = _hass({"entry-1": entry})
    session = _Session(response, error)
    monkeypatch.setattr(
        "custom_components.onlycat.activity_video.async_get_clientsession",
        lambda _hass: session,
    )
    with pytest.raises(expected):
        await OnlyCatActivityVideoView().get(
            _request(hass), "entry-1", "OC-1", "42"
        )


@pytest.mark.asyncio
async def test_missing_or_mismatched_event_credential_is_unavailable() -> None:
    entry = _entry("entry-1", "OC-1", 42)
    entry.runtime_data.client.send_message.return_value = {
        "deviceId": "OC-OTHER",
        "eventId": 42,
        "frameCount": 10,
        "accessToken": "must-not-escape",
    }
    with pytest.raises(web.HTTPGone):
        await OnlyCatActivityVideoView().get(
            _request(_hass({"entry-1": entry})), "entry-1", "OC-1", "42"
        )


@pytest.mark.asyncio
async def test_event_lookup_failure_is_a_clean_bad_gateway() -> None:
    entry = _entry("entry-1", "OC-1", 42)
    entry.runtime_data.client.send_message.side_effect = OnlyCatApiClientError

    with pytest.raises(web.HTTPBadGateway) as error:
        await OnlyCatActivityVideoView().get(
            _request(_hass({"entry-1": entry})), "entry-1", "OC-1", "42"
        )
    assert error.value.text == "Video unavailable"


def test_availability_cache_is_boolean_only_and_expires() -> None:
    runtime = ActivityVideoRuntime()
    key = ("entry-1", "OC-1", 42)
    runtime.store(key, True, now=10)
    assert runtime.cached(key, now=11) is True
    assert runtime.cached(key, now=1000) is None
    assert "secret" not in repr(runtime.availability).lower()
    assert "token" not in repr(runtime.availability).lower()
    assert runtime.semaphore._value == _MAX_CONCURRENT_FETCHES


@pytest.mark.asyncio
async def test_availability_checks_are_bounded_and_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry("entry-1", "OC-1", 40, 41, 42)
    hass = _hass({"entry-1": entry})
    active = 0
    peak = 0
    calls = 0

    async def fake_confirm(_hass, _runtime_data, _device_id, _event_id):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return True

    monkeypatch.setattr(
        "custom_components.onlycat.activity_video._confirm_available", fake_confirm
    )
    results = await asyncio.gather(
        *(async_video_available(hass, "entry-1", "OC-1", event_id) for event_id in (40, 41, 42))
    )
    assert results == [True, True, True]
    assert peak <= _MAX_CONCURRENT_FETCHES
    assert await async_video_available(hass, "entry-1", "OC-1", 42) is True
    assert calls == 3


@pytest.mark.asyncio
async def test_status_returns_only_a_signed_local_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry("entry-1", "OC-1", 42)
    hass = _hass({"entry-1": entry})
    monkeypatch.setattr(
        "custom_components.onlycat.activity_video.async_video_available",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "custom_components.onlycat.activity_video.async_sign_path",
        lambda _hass, path, _ttl, **_kwargs: f"{path}?authSig=signed",
    )
    connection = SimpleNamespace(
        refresh_token_id="refresh-id", send_result=Mock(), send_error=Mock()
    )

    await inspect.unwrap(websocket_activity_video_status)(
        hass,
        connection,
        {
            "id": 1,
            "type": "onlycat/activity_video_status",
            "config_entry_id": "entry-1",
            "device_id": "OC-1",
            "event_id": 42,
        },
    )

    result = connection.send_result.call_args.args[1]
    assert result == {
        "available": True,
        "video_url": "/api/onlycat/activity_video/entry-1/OC-1/42?authSig=signed",
    }
    serialized = repr(result).lower()
    assert "gateway.onlycat.com" not in serialized
    assert "media-secret" not in serialized
    assert "accesstoken" not in serialized
