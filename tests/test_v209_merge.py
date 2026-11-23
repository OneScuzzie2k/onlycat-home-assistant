"""Regression coverage for stable upstream fixes on the custom branch."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from custom_components.onlycat import _refresh_pet_snapshots
from custom_components.onlycat.api import OnlyCatApiClient, OnlyCatConnectionState
from custom_components.onlycat.binary_sensor_event import OnlyCatEventSensor
from custom_components.onlycat.camera import OnlyCatLastVideo
from custom_components.onlycat.data.device import Device
from custom_components.onlycat.data.event import Event
from custom_components.onlycat.data.event_store import EventStore
from custom_components.onlycat.image import OnlyCatLastImage
from custom_components.onlycat.sensor_last_rfid_event import OnlyCatLastRfidEventSensor


def _store():
    client = SimpleNamespace(
        connection_state=OnlyCatConnectionState(True, "connect"),
        send_message=AsyncMock(),
    )
    return EventStore(client), client


@pytest.mark.parametrize(
    "entity_type", [OnlyCatEventSensor, OnlyCatLastRfidEventSensor]
)
async def test_disabled_entities_do_not_subscribe_and_removed_entities_stop_updates(
    entity_type,
):
    store, _ = _store()
    entity = entity_type(Device("OC-1", "OnlyCat"), store)
    assert not store._event_update_listeners
    assert not store._event_summary_update_listeners
    removers = []
    entity.async_on_remove = removers.append
    entity.async_write_ha_state = Mock()
    await entity.async_added_to_hass()
    store._current_events["OC-1"] = Event(device_id="OC-1", event_id=42)
    await store.run_event_listeners("OC-1")
    entity.async_write_ha_state.assert_called()
    for remove in removers:
        remove()
        remove()
    entity.async_write_ha_state.reset_mock()
    await store.run_event_listeners("OC-1")
    await store.on_connection_state(OnlyCatConnectionState(False, "disconnect"))
    entity.async_write_ha_state.assert_not_called()


async def test_listener_can_remove_itself_without_skipping_next_callback():
    store, _ = _store()

    async def first(event):
        remove()

    second = AsyncMock()
    remove = store.add_event_listener("OC-1", first)
    store.add_event_listener("OC-1", second)
    store._current_events["OC-1"] = Event(device_id="OC-1", event_id=42)
    await store.run_event_listeners("OC-1")
    second.assert_awaited_once()


async def test_api_listener_can_be_removed_during_dispatch():
    from collections import defaultdict

    client = object.__new__(OnlyCatApiClient)
    client._listeners = defaultdict(list)

    async def first(*args):
        remove()

    second = AsyncMock()
    remove = client.add_event_listener("test", first)
    client.add_event_listener("test", second)
    await client.handle_event("test", {})
    second.assert_awaited_once()
    remove()


@pytest.mark.parametrize("value", [None, "invalid", 123, ""])
@pytest.mark.parametrize("path", ["startup", "reconnect"])
async def test_invalid_last_seen_timestamp_does_not_abort_pet_sync(value, path):
    store, client = _store()
    replies = {
        "getLastSeenRfidCodesByDevice": [{"rfidCode": "123"}],
        "getRfidLastSeenByDevice": [{"rfidCode": "123", "eventTimestamp": value}],
        "getRfidProfile": {"label": "Nelson"},
    }

    async def send(topic, data):
        return replies[topic]

    client.send_message.side_effect = send
    if path == "startup":
        entry = SimpleNamespace(
            runtime_data=SimpleNamespace(
                devices=[Device("OC-1", "OnlyCat")],
                client=client,
                event_store=store,
            )
        )
        await _refresh_pet_snapshots(entry)
    else:
        await store.reconcile_pet_snapshots_for_device("OC-1")
    assert store.find_pet_by_rfid("123").last_seen == datetime.min.replace(tzinfo=UTC)


@pytest.mark.parametrize(
    "entity_type,method,module",
    [
        (OnlyCatLastVideo, "async_camera_image", "camera"),
        (OnlyCatLastImage, "async_image", "image"),
    ],
)
@pytest.mark.parametrize("status", [200, 404])
async def test_thumbnail_cache_belongs_to_event_and_retries_failed_fetch(
    entity_type, method, module, status
):
    store, _ = _store()
    store.set_current_image("OC-1", b"old", 41)
    entity = entity_type(Mock(), Device("OC-1", "OnlyCat"), store)
    entity._current_event = Event(device_id="OC-1", event_id=42, poster_frame_index=1)
    response = SimpleNamespace(status=status, read=AsyncMock(return_value=b"new"))
    context = AsyncMock()
    context.__aenter__.return_value = response
    session = SimpleNamespace(get=Mock(return_value=context))
    with patch(
        f"custom_components.onlycat.{module}.async_get_clientsession",
        return_value=session,
    ):
        assert await getattr(entity, method)() == (b"new" if status == 200 else None)
        assert await getattr(entity, method)() == (b"new" if status == 200 else None)
    assert session.get.call_count == (1 if status == 200 else 2)


async def test_new_camera_event_awaits_stream_stop():
    store, _ = _store()
    camera = OnlyCatLastVideo(Mock(), Device("OC-1", "OnlyCat"), store)
    camera._current_event = Event(device_id="OC-1", event_id=41)
    stream = SimpleNamespace(stop=AsyncMock())
    camera.stream = stream
    camera.async_write_ha_state = Mock()
    await camera.on_event_update(Event(device_id="OC-1", event_id=42))
    stream.stop.assert_awaited_once()
    assert camera.stream is None
