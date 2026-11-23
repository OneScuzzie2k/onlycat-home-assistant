"""Manage global store of events per device."""

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from custom_components.onlycat.api import (
    OnlyCatApiClient,
    OnlyCatApiClientError,
    OnlyCatConnectionState,
)
from custom_components.onlycat.data.pet import Pet
from homeassistant.const import STATE_HOME, STATE_NOT_HOME, STATE_UNKNOWN
from homeassistant.core import HomeAssistant

from ..const import (
    ACTIVITY_DEDUP_MAX_ENTRIES,
    ACTIVITY_STORE_VERSION,
    CONF_EVENT_RETRY_DELAYS_SECONDS,
    CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS,
    ONLYCAT_ACTIVITY_EVENT,
)
from ..debug_trace import trace_event
from .activity import OnlyCatActivity
from .activity_journal import ActivityJournal, ActivityJournalEntry, ActivityStore
from .event import Event, EventUpdate
from .event_summary import EventSummary, SubEvent

_LOGGER = logging.getLogger(__name__)

_DEDUP_TTL = timedelta(days=7)
Store = ActivityStore


class EventStore:
    """Store events for each device and manage callbacks."""

    def __init__(
        self,
        api_client: OnlyCatApiClient,
        settings: dict[str, object] | None = None,
        hass: HomeAssistant | None = None,
        storage_key: str | None = None,
    ) -> None:
        """Initialize EventStore with given api client."""
        self._event_update_listeners: dict[str, list[Callable]] = {}
        self._event_summary_update_listeners: dict[str, list[Callable]] = {}
        self._pet_update_listeners: dict[str, list[Callable]] = {}
        self._pet_discovery_listeners: list[Callable] = []
        self._activity_update_listeners: dict[str, list[Callable]] = {}
        self._connection_state_listeners: list[Callable] = []
        self._current_events: dict[str, Event] = {}
        self._current_summaries: dict[str, EventSummary] = {}
        self._current_images: dict[str, bytes] = {}
        self._pets: dict[str, Pet] = {}
        self._api_client: OnlyCatApiClient = api_client
        self._connection_state = api_client.connection_state
        self._last_event_received_at: datetime | None = None
        self._last_event_ids: dict[str, int] = {}
        self._bootstrap_watermarks: dict[str, int] = {}
        self._emitted_activity_keys: OrderedDict[str, datetime] = OrderedDict()
        self._journal = ActivityJournal()
        self._activity_emission_enabled = False
        self._hass = hass
        self._storage = (
            Store(hass, ACTIVITY_STORE_VERSION, storage_key)
            if hass is not None and storage_key is not None
            else None
        )
        self._settings = settings or {}
        self._event_retry_delays = self._parse_delays(
            self._settings.get(CONF_EVENT_RETRY_DELAYS_SECONDS),
            (1, 3, 6),
        )
        self._final_event_retry_delays = self._parse_delays(
            self._settings.get(CONF_FINAL_EVENT_RETRY_DELAYS_SECONDS),
            (0, 1, 3),
        )

    async def async_initialize(self) -> None:
        """Load the bounded activity watermark used across integration reloads."""
        if self._storage is None:
            return
        stored = await self._storage.async_load() or {}
        self._last_event_ids = {
            str(device_id): int(event_id)
            for device_id, event_id in stored.get("last_event_ids", {}).items()
        }
        now = datetime.now(UTC)
        for key, timestamp in stored.get("emitted", {}).items():
            try:
                seen_at = datetime.fromisoformat(timestamp)
            except (TypeError, ValueError):
                continue
            if seen_at.tzinfo is None:
                seen_at = seen_at.replace(tzinfo=UTC)
            if now - seen_at <= _DEDUP_TTL:
                self._emitted_activity_keys[str(key)] = seen_at
        self._prune_emitted(now)
        self._journal.load(stored.get("journal", []), now)

    def enable_activity_emission(self) -> None:
        """Finish bootstrap and allow genuinely new activity onto the HA bus."""
        self._bootstrap_watermarks = dict(self._last_event_ids)
        self._activity_emission_enabled = True
        self._schedule_save()

    @property
    def activity_emission_enabled(self) -> bool:
        """Return whether startup replay suppression has finished."""
        return self._activity_emission_enabled

    @staticmethod
    def _serialize_key(key: tuple[str, int, str, str, str]) -> str:
        """Serialize a semantic activity key for persistent storage."""
        return json.dumps(key, separators=(",", ":"))

    def _prune_emitted(self, now: datetime | None = None) -> None:
        """Bound the dedup cache by both age and number of entries."""
        now = now or datetime.now(UTC)
        expired = [
            key
            for key, seen_at in self._emitted_activity_keys.items()
            if now - seen_at > _DEDUP_TTL
        ]
        for key in expired:
            self._emitted_activity_keys.pop(key, None)
        while len(self._emitted_activity_keys) > ACTIVITY_DEDUP_MAX_ENTRIES:
            self._emitted_activity_keys.popitem(last=False)

    def _schedule_save(self) -> None:
        """Persist only token-free dedup keys and event watermarks."""
        if self._storage is None:
            return
        self._storage.async_delay_save(
            lambda: {
                "last_event_ids": dict(self._last_event_ids),
                "emitted": {
                    key: timestamp.isoformat()
                    for key, timestamp in self._emitted_activity_keys.items()
                },
                "journal": self._journal.records(),
            },
            5,
        )

    def _remember_event_id(self, device_id: str, event_id: int) -> None:
        """Advance a device watermark monotonically."""
        if event_id > self._last_event_ids.get(device_id, -1):
            self._last_event_ids[device_id] = event_id
            self._schedule_save()

    async def _process_summary_activities(
        self, summary: EventSummary, *, source: str = "event_summary"
    ) -> None:
        """Normalize, apply and emit complete RFID activities exactly once."""
        event = self._current_events.get(summary.device_id)
        if event is None or event.event_id != summary.event_id:
            return
        newest_before = self._last_event_ids.get(summary.device_id, -1)
        out_of_order = summary.event_id < newest_before
        self._remember_event_id(summary.device_id, summary.event_id)
        changed_pets: set[str] = set()
        for subevent in summary.subevents:
            if not subevent.rfid_code:
                continue
            pet = await self._resolve_named_pet(subevent.rfid_code, summary.device_id)
            activity = OnlyCatActivity.from_parts(
                event=event,
                summary=summary,
                subevent=subevent,
                pet_name=pet.label if pet is not None else None,
            )
            if activity is None:
                continue
            location_before = pet.location if pet is not None else None
            location_changed = False
            if not out_of_order and pet is not None:
                changed, location_changed = pet.apply_activity(activity)
                pet.last_seen_event = event
                pet.last_seen_summary = summary
                if changed:
                    changed_pets.add(pet.rfid_code)

            serialized_key = self._serialize_key(activity.dedup_key)
            bootstrap_watermark = self._bootstrap_watermarks.get(activity.device_id, -1)
            existing_journal_entry = self._journal.get(activity.dedup_key)
            should_emit = (
                self._activity_emission_enabled
                and not out_of_order
                and activity.timestamp is not None
                and activity.event_id > bootstrap_watermark
                and serialized_key not in self._emitted_activity_keys
            )
            if should_emit:
                received_at = datetime.now(UTC)
                location_after = pet.location if pet is not None else None
                journal_entry = ActivityJournalEntry.from_activity(
                    activity,
                    received_at=received_at,
                    source=source,
                    location_before=location_before,
                    location_after=location_after,
                    location_changed=location_changed,
                    location_source=pet.location_source if pet is not None else None,
                )
                self._journal.append(journal_entry)
                self._journal.harmonize_event_enrichment(
                    activity.device_id, activity.event_id
                )
                self._emitted_activity_keys[serialized_key] = received_at
                self._prune_emitted()
                self._schedule_save()
                if self._hass is not None:
                    self._hass.bus.async_fire(
                        ONLYCAT_ACTIVITY_EVENT,
                        activity.as_event_data(),
                    )
                await self.run_activity_listeners(activity.device_id, journal_entry)
            elif existing_journal_entry is not None:
                existing_journal_entry.merge_enrichment(
                    activity,
                    source=source,
                    received_at=datetime.now(UTC),
                )
                self._journal.propagate_event_enrichment(activity)
                self._journal.harmonize_event_enrichment(
                    activity.device_id, activity.event_id
                )
                self._schedule_save()
                await self.run_activity_listeners(
                    activity.device_id, existing_journal_entry
                )
        for rfid_code in changed_pets:
            await self.run_pet_listeners(rfid_code)

    @staticmethod
    def _summary_has_rfid_subevent(summary: dict[str, Any] | None) -> bool:
        """Return whether an event summary payload includes RFID-tagged subevents."""
        if not isinstance(summary, dict):
            return False
        body = summary.get("body", summary)
        subevents = body.get("subevents") or []
        return any(
            isinstance(subevent, dict) and subevent.get("rfidCode")
            for subevent in subevents
        )

    @staticmethod
    def _parse_delays(value: object, default: tuple[int, ...]) -> tuple[int, ...]:
        """Parse retry delays from config values."""
        if isinstance(value, str):
            try:
                parsed = tuple(int(part.strip()) for part in value.split(",") if part)
            except ValueError:
                return default
            return tuple(delay for delay in parsed if delay >= 0) or default
        if isinstance(value, list):
            try:
                parsed = tuple(int(part) for part in value)
            except (TypeError, ValueError):
                return default
            return tuple(delay for delay in parsed if delay >= 0) or default
        return default

    async def _retry_get_event_until_rfid(
        self,
        device_id: str,
        event_id: int,
        delays: tuple[int, ...],
    ) -> None:
        """Retry getEvent because RFID/classification can arrive after the event."""
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                event = await self._api_client.send_message(
                    "getEvent",
                    {
                        "deviceId": device_id,
                        "eventId": event_id,
                        "subscribe": False,
                    },
                )
            except Exception:
                _LOGGER.exception(
                    "Failed getEvent retry for device %s event %s",
                    device_id,
                    event_id,
                )
                return
            if isinstance(event, dict) and event.get("rfidCodes"):
                return

    async def _retry_get_event_summary_until_rfid(
        self,
        device_id: str,
        event_id: int,
        access_token: str,
        delays: tuple[int, ...],
    ) -> None:
        """Retry getEventSummary because RFID direction/action can arrive later."""
        for delay in delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                summary = await self.send_get_event_summary(
                    device_id,
                    event_id,
                    access_token,
                    subscribe=False,
                )
            except Exception:
                _LOGGER.exception(
                    "Failed getEventSummary retry for device %s event %s",
                    device_id,
                    event_id,
                )
                return
            if self._summary_has_rfid_subevent(summary):
                return

    async def send_get_event_message(
        self,
        device_id: str,
        event_id: int,
        subscribe: bool = True,
    ) -> None:
        """Send getEvent message to get latest full event."""
        await self._api_client.send_message(
            "getEvent",
            {
                "deviceId": device_id,
                "eventId": event_id,
                "subscribe": subscribe,
            },
        )

    async def send_get_event_summary(
        self,
        device_id: str,
        event_id: int,
        access_token: str,
        subscribe: bool = True,
    ) -> Any:
        """Send getEventSummary message and subscribe."""
        return await self._api_client.send_message(
            "getEventSummary",
            {
                "deviceId": device_id,
                "eventId": event_id,
                "accessToken": access_token,
                "subscribe": subscribe,
            },
        )

    def mark_event_received(self) -> None:
        """Record when live event data is observed."""
        self._last_event_received_at = datetime.now(UTC)

    @property
    def last_event_received_at(self) -> datetime | None:
        """Return the timestamp of the last live event payload."""
        return self._last_event_received_at

    def last_event_id(self, device_id: str) -> int | None:
        """Return the greatest observed event ID for a device."""
        return self._last_event_ids.get(device_id)

    async def reconcile_pet_snapshots_for_device(self, device_id: str) -> set[str]:
        """Reconcile pet state for one device from authoritative API snapshots."""
        changed_rfids: set[str] = set()
        rfids = (
            await self._api_client.send_message(
                "getLastSeenRfidCodesByDevice", {"deviceId": device_id}
            )
            or []
        )
        last_seens = (
            await self._api_client.send_message(
                "getRfidLastSeenByDevice", {"deviceId": device_id}
            )
            or []
        )
        last_seen_rfids = {last_seen["rfidCode"]: last_seen for last_seen in last_seens}
        for rfid in rfids:
            rfid_code = rfid["rfidCode"]
            pet = self.find_pet_by_rfid(rfid_code)
            label = pet.label if pet is not None else None
            if not self.is_valid_profile_label(label, rfid_code):
                rfid_profile = (
                    await self._api_client.send_message(
                        "getRfidProfile",
                        {"deviceId": device_id, "rfidCode": rfid_code},
                    )
                    or {}
                )
                label = rfid_profile.get("label")
            if not self.is_valid_profile_label(label, rfid_code):
                continue
            last_seen_at = None
            location = None
            last_subevent = None
            if rfid_code in last_seen_rfids:
                last_seen = last_seen_rfids[rfid_code]
                last_seen_at = datetime.fromisoformat(
                    last_seen.get(
                        "eventTimestamp", datetime.min.replace(tzinfo=UTC).isoformat()
                    )
                )
                location = last_seen.get("location")
                last_subevent = SubEvent.from_api_response(
                    last_seen.get("lastSubevent", None) or {}
                )
            changed = self.apply_pet_snapshot(
                device_id=device_id,
                rfid_code=rfid_code,
                label=label,
                last_seen=last_seen_at,
                location=location,
                last_subevent=last_subevent,
            )
            if changed:
                changed_rfids.add(rfid_code)
        for rfid_code in changed_rfids:
            await self.run_pet_listeners(rfid_code)
        return changed_rfids

    def apply_pet_snapshot(
        self,
        *,
        device_id: str,
        rfid_code: str,
        label: str,
        last_seen: datetime | None,
        location: str | None,
        last_subevent: Any,
    ) -> bool:
        """Apply an authoritative pet snapshot and return whether it changed."""
        if not self.is_valid_profile_label(label, rfid_code):
            return False
        pet = self.find_pet_by_rfid(rfid_code)
        pet_was_missing = pet is None
        became_named = pet is None or not self.is_valid_profile_label(
            pet.label, rfid_code
        )
        if pet is None:
            pet = Pet(
                rfid_code=rfid_code,
                location=STATE_UNKNOWN,
                last_seen=None,
                device_id=device_id,
                label=label.strip(),
            )
            self.add_pet(pet)
        previous_state = (
            pet.device_id,
            pet.label,
            pet.last_seen,
            pet.location,
            pet.location_changed_at,
            pet.location_source,
            pet.location_source_event_id,
        )
        pet.device_id = device_id
        pet.label = label.strip()
        if became_named and not pet_was_missing:
            for callback in list(self._pet_discovery_listeners):
                callback(pet)
        if last_seen is not None and (
            pet.last_seen is None or last_seen >= pet.last_seen
        ):
            pet.last_seen = last_seen
        snapshot_can_set_location = (
            last_seen is None
            or pet.location_evidence_at is None
            or last_seen >= pet.location_evidence_at
        )
        if snapshot_can_set_location and location == "OUTSIDE":
            trace_event(
                "pet_snapshot_location",
                device_id=device_id,
                rfid_code=rfid_code,
                source="snapshot_location",
                location=location,
                last_seen=last_seen,
            )
            location_changed = pet.location != STATE_NOT_HOME
            pet.location = STATE_NOT_HOME
            if last_seen is not None and location_changed:
                pet.location_changed_at = last_seen
            if last_seen is not None:
                pet.location_evidence_at = last_seen
            pet.location_source = "onlycat_snapshot"
            pet.location_source_event_id = None
        elif snapshot_can_set_location and location in {"INSIDE", "HOME"}:
            trace_event(
                "pet_snapshot_location",
                device_id=device_id,
                rfid_code=rfid_code,
                source="snapshot_location",
                location=location,
                last_seen=last_seen,
            )
            location_changed = pet.location != STATE_HOME
            pet.location = STATE_HOME
            if last_seen is not None and location_changed:
                pet.location_changed_at = last_seen
            if last_seen is not None:
                pet.location_evidence_at = last_seen
            pet.location_source = "onlycat_snapshot"
            pet.location_source_event_id = None
        elif snapshot_can_set_location and last_subevent is not None:
            trace_event(
                "pet_snapshot_location",
                device_id=device_id,
                rfid_code=rfid_code,
                source="snapshot_subevent",
                subevent=last_subevent,
                last_seen=last_seen,
            )
            pet.update_from_subevent(last_subevent)
            if last_seen is not None and pet.location in {STATE_HOME, STATE_NOT_HOME}:
                pet.location_evidence_at = last_seen
                pet.location_source = "onlycat_snapshot_subevent"
                pet.location_source_event_id = None
        return previous_state != (
            pet.device_id,
            pet.label,
            pet.last_seen,
            pet.location,
            pet.location_changed_at,
            pet.location_source,
            pet.location_source_event_id,
        )

    async def on_device_event_update(self, data: dict) -> None:
        """Handle deviceEventUpdate messages."""
        self.mark_event_received()
        if "deviceId" not in data or "eventId" not in data:
            return
        await self.send_get_event_message(data["deviceId"], data["eventId"])
        if "body" in data and "accessToken" in data["body"]:
            await self.send_get_event_summary(
                data["deviceId"], data["eventId"], data["body"]["accessToken"]
            )
            asyncio.create_task(
                self._retry_get_event_summary_until_rfid(
                    data["deviceId"],
                    data["eventId"],
                    data["body"]["accessToken"],
                    self._final_event_retry_delays,
                )
            )
        asyncio.create_task(
            self._retry_get_event_until_rfid(
                data["deviceId"], data["eventId"], self._event_retry_delays
            )
        )

    async def on_event_update(self, data: dict) -> None:
        """Handle eventUpdate messages."""
        self.mark_event_received()
        update = EventUpdate.from_api_response(data)
        if not update:
            return
        await self.on_get_event(update.event, source="event_update")
        if update.event.access_token and update.event.event_id is not None:
            await self.send_get_event_summary(
                update.device_id,
                update.event_id,
                update.event.access_token,
                subscribe=False,
            )
            asyncio.create_task(
                self._retry_get_event_summary_until_rfid(
                    update.device_id,
                    update.event_id,
                    update.event.access_token,
                    self._final_event_retry_delays,
                )
            )
        if update.event.frame_count is not None and update.event.event_id is not None:
            asyncio.create_task(
                self._retry_get_event_until_rfid(
                    update.device_id,
                    update.event_id,
                    self._final_event_retry_delays,
                )
            )

    async def on_get_event(
        self, data: dict | Event, *, source: str = "get_event"
    ) -> None:
        """Handle replies from getEvent messages."""
        self.mark_event_received()
        event = Event.from_api_response(data) if isinstance(data, dict) else data
        if not event or event.device_id is None or event.event_id is None:
            return
        current_event = self._current_events.get(event.device_id)
        if (
            current_event is not None
            and current_event.event_id is not None
            and event.event_id < current_event.event_id
        ):
            _LOGGER.debug(
                "Ignoring out-of-order event %s for %s; current event is %s",
                event.event_id,
                event.device_id,
                current_event.event_id,
            )
            return
        self._remember_event_id(event.device_id, event.event_id)
        if event.access_token:
            current_summary = self._current_summaries.get(event.device_id)
            if current_summary is None or current_summary.event_id != event.event_id:
                await self.send_get_event_summary(
                    event.device_id,
                    event.event_id,
                    event.access_token,
                    subscribe=False,
                )
                asyncio.create_task(
                    self._retry_get_event_summary_until_rfid(
                        event.device_id,
                        event.event_id,
                        event.access_token,
                        self._final_event_retry_delays,
                    )
                )
        if event.device_id not in self._current_events:
            self._current_events[event.device_id] = event
        if self._current_events[event.device_id].event_id != event.event_id:
            self._current_events[event.device_id] = event
        else:
            self._current_events[event.device_id].update_from(event)
        event = self._current_events[event.device_id]
        for rfid_code in event.rfid_codes or []:
            pet = self.find_pet_by_rfid(rfid_code)
            if (
                pet
                and event.timestamp is not None
                and (pet.last_seen is None or pet.last_seen < event.timestamp)
            ):
                pet.last_seen = event.timestamp
                pet.last_seen_event = event
                await self.run_pet_listeners(pet.rfid_code)
        await self.run_event_listeners(event.device_id)
        current_summary = self._current_summaries.get(event.device_id)
        if current_summary and current_summary.event_id == event.event_id:
            await self._process_summary_activities(current_summary, source=source)

    async def on_get_event_summary(
        self, data: dict, *, source: str = "get_event_summary"
    ) -> None:
        """Handle replies from getEventSummary messages."""
        self.mark_event_received()
        summary = EventSummary.from_api_response(data)
        if not summary:
            return
        current_summary = self._current_summaries.get(summary.device_id)
        if current_summary is not None and summary.event_id < current_summary.event_id:
            _LOGGER.debug(
                "Ignoring out-of-order summary %s for %s; current summary is %s",
                summary.event_id,
                summary.device_id,
                current_summary.event_id,
            )
            return
        if (
            summary.device_id not in self._current_summaries
            or self._current_summaries[summary.device_id].event_id != summary.event_id
        ):
            self._current_summaries[summary.device_id] = summary
        else:
            self._current_summaries[summary.device_id].update_from(summary)
        summary = self._current_summaries[summary.device_id]
        had_rfid_subevent = any(item.rfid_code for item in summary.subevents)
        for subevent in summary.subevents:
            if subevent.rfid_code:
                trace_event(
                    "pet_live_subevent",
                    device_id=summary.device_id,
                    event_id=summary.event_id,
                    rfid_code=subevent.rfid_code,
                    direction=subevent.direction,
                    action=subevent.action,
                )
        await self._process_summary_activities(summary, source=source)
        if not had_rfid_subevent:
            try:
                snapshot_changes = await self.reconcile_pet_snapshots_for_device(
                    summary.device_id
                )
                if snapshot_changes:
                    _LOGGER.info(
                        "Recovered pet presence for device %s from snapshot after summary %s arrived without RFID subevents",
                        summary.device_id,
                        summary.event_id,
                    )
                    trace_event(
                        "pet_snapshot_recovery",
                        device_id=summary.device_id,
                        event_id=summary.event_id,
                        changed_rfids=sorted(snapshot_changes),
                    )
            except Exception:
                _LOGGER.exception(
                    "Failed snapshot reconciliation for device %s after summary %s without RFID subevents",
                    summary.device_id,
                    summary.event_id,
                )
                trace_event(
                    "pet_snapshot_recovery_failed",
                    device_id=summary.device_id,
                    event_id=summary.event_id,
                )
        await self.run_summary_listeners(summary.device_id)

    async def on_event_summary_update(self, data: dict) -> None:
        """Handle eventSummaryUpdate messages."""
        self.mark_event_received()
        if "body" not in data:
            _LOGGER.warning("Received event summary update with no body: %s", data)
            return
        await self.on_get_event_summary(data["body"], source="event_summary_update")

    async def run_event_listeners(self, device_id: str) -> None:
        """Call all listeners for a given device."""
        if device_id not in self._event_update_listeners:
            return
        event = self._current_events.get(device_id, None)
        if event is not None:
            for callback in self._event_update_listeners[device_id]:
                await callback(event)

    async def run_summary_listeners(self, device_id: str) -> None:
        """Call all event summary listeners for a given device."""
        if device_id not in self._event_summary_update_listeners:
            return
        summary = self._current_summaries.get(device_id, None)
        if summary is not None:
            for callback in self._event_summary_update_listeners[device_id]:
                await callback(summary)

    async def run_pet_listeners(self, rfid_code: str) -> None:
        """Call all pet listeners for a given RFID code."""
        if rfid_code not in self._pet_update_listeners:
            return
        pet = self.find_pet_by_rfid(rfid_code)
        if pet is not None:
            for callback in self._pet_update_listeners[rfid_code]:
                await callback(pet)

    async def on_connection_state(self, state: OnlyCatConnectionState) -> None:
        """Handle a Socket.IO connection-state update."""
        self._connection_state = state
        for callback in self._connection_state_listeners:
            await callback(state)

    def add_event_listener(self, device_id: str, callback: Callable) -> None:
        """Add function to a devices listener list."""
        if device_id not in self._event_update_listeners:
            self._event_update_listeners[device_id] = []
        self._event_update_listeners[device_id].append(callback)

    def add_event_summary_listener(self, device_id: str, callback: Callable) -> None:
        """Add function to a devices event summary listener list."""
        if device_id not in self._event_summary_update_listeners:
            self._event_summary_update_listeners[device_id] = []
        self._event_summary_update_listeners[device_id].append(callback)

    def add_pet_listener(self, rfid_code: str, callback: Callable) -> Callable:
        """Add function to a pets listener list."""
        if rfid_code not in self._pet_update_listeners:
            self._pet_update_listeners[rfid_code] = []
        self._pet_update_listeners[rfid_code].append(callback)

        def remove_listener() -> None:
            listeners = self._pet_update_listeners.get(rfid_code, [])
            if callback in listeners:
                listeners.remove(callback)

        return remove_listener

    def add_pet_discovery_listener(self, callback: Callable) -> Callable:
        """Add a callback for newly resolved named pets."""
        self._pet_discovery_listeners.append(callback)

        def remove_listener() -> None:
            if callback in self._pet_discovery_listeners:
                self._pet_discovery_listeners.remove(callback)

        return remove_listener

    def add_activity_listener(self, device_id: str, callback: Callable) -> Callable:
        """Add a listener for journal updates on one flap."""
        self._activity_update_listeners.setdefault(device_id, []).append(callback)

        def remove_listener() -> None:
            listeners = self._activity_update_listeners.get(device_id, [])
            if callback in listeners:
                listeners.remove(callback)

        return remove_listener

    async def run_activity_listeners(
        self, device_id: str, entry: ActivityJournalEntry
    ) -> None:
        """Notify Recent Activity entities of a journal change."""
        for callback in self._activity_update_listeners.get(device_id, []):
            result = callback(entry)
            if asyncio.iscoroutine(result):
                await result

    def add_connection_listener(self, callback: Callable) -> None:
        """Add function to be called on connection-state changes."""
        self._connection_state_listeners.append(callback)

    @property
    def connection_state(self) -> OnlyCatConnectionState:
        """Return the latest known Socket.IO connection state."""
        return self._connection_state

    def get_current_image(self, device_id: str) -> bytes | None:
        """Return cached image for given device."""
        return self._current_images.get(device_id, None)

    def set_current_image(self, device_id: str, image: bytes) -> None:
        """Cache image for given device."""
        self._current_images[device_id] = image

    @staticmethod
    def is_valid_profile_label(label: object, rfid_code: str) -> bool:
        """Return whether a profile label is useful and human-friendly."""
        return (
            isinstance(label, str)
            and bool(label.strip())
            and label.strip().casefold() != rfid_code.strip().casefold()
        )

    def find_pet_by_rfid(self, rfid_code: str) -> Pet | None:
        """Return an already resolved named pet without creating an entity source."""
        return self._pets.get(rfid_code)

    async def _resolve_named_pet(self, rfid_code: str, device_id: str) -> Pet | None:
        """Resolve an RFID profile and create a Pet only for a useful name."""
        pet = self.find_pet_by_rfid(rfid_code)
        if pet is not None:
            if pet.device_id is None:
                pet.device_id = device_id
            return pet
        try:
            profile = await self._api_client.send_message(
                "getRfidProfile", {"deviceId": device_id, "rfidCode": rfid_code}
            )
        except (OnlyCatApiClientError, TimeoutError, TypeError, ValueError):
            _LOGGER.debug("Unable to resolve OnlyCat profile for RFID %s", rfid_code)
            return None
        label = profile.get("label") if isinstance(profile, dict) else None
        if not self.is_valid_profile_label(label, rfid_code):
            return None
        if pet is None:
            pet = Pet(
                rfid_code=rfid_code,
                location=STATE_UNKNOWN,
                last_seen=None,
                device_id=device_id,
                label=label.strip(),
            )
            self.add_pet(pet)
        else:
            pet.label = label.strip()
            pet.device_id = device_id
        return pet

    def get_pet_by_rfid(self, rfid_code: str, device_id: str | None = None) -> Pet:
        """Legacy explicit pet creation helper retained for compatibility/tests."""
        pet = self._pets.get(rfid_code, None)
        if pet is None:
            self.add_pet(
                Pet(
                    rfid_code=rfid_code,
                    location=STATE_UNKNOWN,
                    last_seen=None,
                    device_id=device_id,
                )
            )
            pet = self._pets[rfid_code]
        elif device_id is not None and pet.device_id is None:
            pet.device_id = device_id
        return pet

    def add_pet(self, pet: Pet) -> None:
        """Add pet to store."""
        is_new = pet.rfid_code not in self._pets
        self._pets[pet.rfid_code] = pet
        if is_new and self.is_valid_profile_label(pet.label, pet.rfid_code):
            for callback in list(self._pet_discovery_listeners):
                callback(pet)

    def get_pets(self) -> list[Pet]:
        """Return list of all pets in store."""
        return [
            pet
            for pet in self._pets.values()
            if self.is_valid_profile_label(pet.label, pet.rfid_code)
        ]

    @property
    def activity_journal(self) -> ActivityJournal:
        """Return the sanitized activity journal."""
        return self._journal
