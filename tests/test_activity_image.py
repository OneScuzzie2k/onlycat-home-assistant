"""Security and behavior tests for historical activity images."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web
from custom_components.onlycat.activity_image import (
    OnlyCatActivityImageView,
    activity_image_path,
)
from custom_components.onlycat.data.activity_journal import (
    ActivityJournal,
    ActivityJournalEntry,
)
from custom_components.onlycat.websocket_api import websocket_activity_journal
from homeassistant.helpers.http import request_handler_factory


def _journal(
    device_id: str = "OC-1", event_id: int = 42, *, image_available: bool = True
) -> ActivityJournal:
    timestamp = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
    journal = ActivityJournal()
    journal.append(
        ActivityJournalEntry(
            device_id=device_id,
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
            image_available=image_available,
            video_available=False,
            location_before="not_home",
            location_after="home",
            location_changed=True,
            location_source="onlycat_activity",
            duplicate_delivery_count=0,
            delivery_sources=["get_event_summary"],
        )
    )
    return journal


def _entry(entry_id: str, device_id: str, event_id: int = 42) -> SimpleNamespace:
    return SimpleNamespace(
        entry_id=entry_id,
        domain="onlycat",
        runtime_data=SimpleNamespace(
            devices=[SimpleNamespace(device_id=device_id)],
            event_store=SimpleNamespace(activity_journal=_journal(device_id, event_id)),
        ),
    )


def _request(entries: dict[str, object]) -> SimpleNamespace:
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_get_entry=entries.get),
    )
    return SimpleNamespace(app={"hass": hass})


class _ImageResponse:
    status = 200
    content_type = "image/jpeg"
    content_length = 8

    async def read(self) -> bytes:
        return b"jpegdata"


class _ResponseContext:
    async def __aenter__(self) -> _ImageResponse:
        return _ImageResponse()

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Session:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _ResponseContext:
        self.calls.append((url, kwargs))
        return _ResponseContext()


def test_view_requires_home_assistant_authentication() -> None:
    """The HA HTTP layer rejects unsigned/unauthenticated requests."""
    assert OnlyCatActivityImageView.requires_auth is True


@pytest.mark.asyncio
async def test_unauthenticated_request_is_rejected() -> None:
    """Exercise Home Assistant's view wrapper, not just the declaration."""
    view = OnlyCatActivityImageView()
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
        ("entry-1", "OC-1", "not-a-number"),
        ("entry-1", "OC-1", "042"),
        ("entry-2", "OC-1", "42"),
        ("other-domain", "OC-1", "42"),
    ],
)
async def test_view_rejects_unpermitted_scope(
    entry_id: str, device_id: str, event_id: str
) -> None:
    """Reject wrong entry/device/event, malformed IDs, and cross-entry access."""
    entries = {
        "entry-1": _entry("entry-1", "OC-1"),
        "entry-2": _entry("entry-2", "OC-2"),
        "other-domain": SimpleNamespace(domain="not_onlycat", runtime_data=None),
    }
    with pytest.raises(web.HTTPNotFound):
        await OnlyCatActivityImageView().get(
            _request(entries), entry_id, device_id, event_id
        )


@pytest.mark.asyncio
async def test_view_rejects_event_without_advertised_image() -> None:
    """A real journal event is insufficient when it has no image metadata."""
    entry = _entry("entry-1", "OC-1")
    entry.runtime_data.event_store.activity_journal = _journal(image_available=False)
    with pytest.raises(web.HTTPNotFound):
        await OnlyCatActivityImageView().get(
            _request({"entry-1": entry}), "entry-1", "OC-1", "42"
        )


@pytest.mark.asyncio
async def test_valid_image_is_fixed_scope_and_contains_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxy only the fixed frame URL and return clean JPEG bytes/headers."""
    session = _Session()
    monkeypatch.setattr(
        "custom_components.onlycat.activity_image.async_get_clientsession",
        lambda _hass: session,
    )
    response = await OnlyCatActivityImageView().get(
        _request({"entry-1": _entry("entry-1", "OC-1")}),
        "entry-1",
        "OC-1",
        "42",
    )

    assert response.status == 200
    assert response.body == b"jpegdata"
    assert response.content_type == "image/jpeg"
    assert session.calls[0][0] == "https://gateway.onlycat.com/events/OC-1/42/1"
    assert session.calls[0][1]["allow_redirects"] is False
    serialized = repr((response.headers, response.body)).lower()
    assert "api_key" not in serialized
    assert "accesstoken" not in serialized
    assert "access_token" not in serialized
    assert "gateway.onlycat.com" not in serialized
    assert "sharing/video" not in serialized


def test_path_and_handler_offer_no_arbitrary_proxy_parameter() -> None:
    """Only encoded identifiers, never an upstream URL, enter the proxy path."""
    path = activity_image_path("entry/other", "OC/../../other", 42)
    assert path == "/api/onlycat/activity_image/entry%2Fother/OC%2F..%2F..%2Fother/42"
    assert "url" not in inspect.signature(OnlyCatActivityImageView.get).parameters
    assert "http" not in path


def test_proxy_has_no_private_media_file_output() -> None:
    """Historical media remains in memory and is never placed under /local."""
    source = inspect.getsource(OnlyCatActivityImageView)
    assert "/local" not in source
    assert "config/www" not in source
    assert "write_bytes" not in source
    assert "write_text" not in source


def test_authenticated_journal_returns_only_a_signed_ha_image_path() -> None:
    """The card receives an expiring HA path, never an OnlyCat URL or secret."""
    store = SimpleNamespace(activity_journal=_journal())
    config_entry = SimpleNamespace(
        domain="onlycat",
        runtime_data=SimpleNamespace(
            devices=[SimpleNamespace(device_id="OC-1")], event_store=store
        ),
    )
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_get_entry=lambda entry_id: (
                config_entry if entry_id == "entry-1" else None
            )
        ),
    )
    connection = SimpleNamespace(
        refresh_token_id="ha-refresh-token-id",
        send_result=Mock(),
        send_error=Mock(),
    )

    websocket_activity_journal(
        hass,
        connection,
        {
            "id": 1,
            "type": "onlycat/activity_journal",
            "config_entry_id": "entry-1",
            "limit": 25,
        },
    )

    result = connection.send_result.call_args.args[1]
    image_url = result["entries"][0]["image_url"]
    assert image_url.startswith("/api/onlycat/activity_image/entry-1/OC-1/42?authSig=")
    serialized = repr(result).lower()
    assert "gateway.onlycat.com" not in serialized
    assert "api_key" not in serialized
    assert "accesstoken" not in serialized
    assert "access_token" not in serialized
