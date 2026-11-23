"""Persistent, token-free OnlyCat activity journal models."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..const import (
    ACTIVITY_JOURNAL_MAX_AGE_DAYS,
    ACTIVITY_JOURNAL_MAX_ENTRIES,
    ACTIVITY_STORE_VERSION,
)
from .activity import OnlyCatActivity


def _as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: object) -> datetime | None:
    """Parse an ISO timestamp without allowing malformed storage to fail setup."""
    if not isinstance(value, str):
        return None
    try:
        return _as_utc(datetime.fromisoformat(value))
    except ValueError:
        return None


@dataclass(slots=True)
class ActivityJournalEntry:
    """One safe normalized activity retained by Home Assistant."""

    device_id: str
    event_id: int
    timestamp: datetime
    rfid: str
    pet: str | None
    activity: str
    action: str
    direction: str
    classification: str | None
    trigger_source: str | None
    first_received_at: datetime
    last_received_at: datetime
    image_available: bool
    video_available: bool
    location_before: str | None
    location_after: str | None
    location_changed: bool
    location_source: str | None
    duplicate_delivery_count: int
    delivery_sources: list[str]
    automatic_classification: str | None = None
    manual_classification: str | None = None

    @property
    def semantic_key(self) -> tuple[str, int, str, str, str]:
        """Return the semantic delivery identity."""
        return (self.device_id, self.event_id, self.rfid, self.action, self.direction)

    @property
    def cursor(self) -> str:
        """Return an opaque-enough stable pagination cursor."""
        return "|".join(str(item).replace("|", "_") for item in self.semantic_key)

    @classmethod
    def from_activity(
        cls,
        activity: OnlyCatActivity,
        *,
        received_at: datetime,
        source: str,
        location_before: str | None,
        location_after: str | None,
        location_changed: bool,
        location_source: str | None,
    ) -> ActivityJournalEntry:
        """Build a journal entry from the already-sanitized activity model."""
        if activity.timestamp is None:
            raise ValueError("A finalized activity requires a timestamp")
        received_at = _as_utc(received_at)
        return cls(
            device_id=activity.device_id,
            event_id=activity.event_id,
            timestamp=_as_utc(activity.timestamp),
            rfid=activity.rfid,
            pet=activity.pet,
            activity=activity.activity,
            action=activity.action,
            direction=activity.direction,
            classification=activity.classification,
            trigger_source=activity.trigger_source,
            first_received_at=received_at,
            last_received_at=received_at,
            image_available=activity.image_available,
            video_available=activity.video_available,
            location_before=location_before,
            location_after=location_after,
            location_changed=location_changed,
            location_source=location_source,
            duplicate_delivery_count=0,
            delivery_sources=[source],
            automatic_classification=activity.automatic_classification,
            manual_classification=activity.manual_classification,
        )

    @classmethod
    def from_dict(cls, data: object) -> ActivityJournalEntry | None:
        """Load one entry, ignoring corrupt or unsafe records."""
        if not isinstance(data, dict):
            return None
        timestamp = _parse_datetime(data.get("timestamp"))
        first_received = _parse_datetime(data.get("first_received_at"))
        last_received = _parse_datetime(data.get("last_received_at"))
        required_strings = ("device_id", "rfid", "activity", "action", "direction")
        if (
            timestamp is None
            or first_received is None
            or last_received is None
            or any(not isinstance(data.get(key), str) or not data[key] for key in required_strings)
        ):
            return None
        try:
            event_id = int(data["event_id"])
            duplicate_count = max(0, int(data.get("duplicate_delivery_count", 0)))
        except (KeyError, TypeError, ValueError):
            return None
        sources = data.get("delivery_sources", [])
        if not isinstance(sources, list):
            sources = []
        clean_sources = sorted({str(source) for source in sources if source})
        pet = data.get("pet")
        return cls(
            device_id=data["device_id"],
            event_id=event_id,
            timestamp=timestamp,
            rfid=data["rfid"],
            pet=pet if isinstance(pet, str) and pet else None,
            activity=data["activity"],
            action=data["action"],
            direction=data["direction"],
            classification=data.get("classification") if isinstance(data.get("classification"), str) else None,
            trigger_source=data.get("trigger_source") if isinstance(data.get("trigger_source"), str) else None,
            first_received_at=first_received,
            last_received_at=last_received,
            image_available=bool(data.get("image_available", False)),
            video_available=bool(data.get("video_available", False)),
            location_before=data.get("location_before") if isinstance(data.get("location_before"), str) else None,
            location_after=data.get("location_after") if isinstance(data.get("location_after"), str) else None,
            location_changed=bool(data.get("location_changed", False)),
            location_source=data.get("location_source") if isinstance(data.get("location_source"), str) else None,
            duplicate_delivery_count=duplicate_count,
            delivery_sources=clean_sources,
            automatic_classification=(
                data.get("automatic_classification")
                if isinstance(data.get("automatic_classification"), str)
                else data.get("classification")
                if isinstance(data.get("classification"), str)
                else None
            ),
            manual_classification=(
                data.get("manual_classification")
                if isinstance(data.get("manual_classification"), str)
                else None
            ),
        )

    def note_delivery(self, source: str, received_at: datetime) -> None:
        """Record a duplicate delivery without creating another journal row."""
        self.duplicate_delivery_count += 1
        self.last_received_at = _as_utc(received_at)
        if source and source not in self.delivery_sources:
            self.delivery_sources.append(source)
            self.delivery_sources.sort()

    @staticmethod
    def _is_meaningful(value: str | None) -> bool:
        """Return whether an enum label carries information beyond UNKNOWN."""
        return bool(value and value.upper() != "UNKNOWN")

    def merge_enrichment(
        self,
        activity: OnlyCatActivity,
        *,
        source: str,
        received_at: datetime,
    ) -> None:
        """Merge later authoritative metadata without changing activity semantics."""
        if self._is_meaningful(activity.automatic_classification):
            self.automatic_classification = activity.automatic_classification
        if self._is_meaningful(activity.manual_classification):
            self.manual_classification = activity.manual_classification
        if self._is_meaningful(activity.classification) or self.classification is None and activity.classification:
            self.classification = activity.classification
        if self._is_meaningful(activity.trigger_source) or self.trigger_source is None and activity.trigger_source:
            self.trigger_source = activity.trigger_source
        if self.pet is None and activity.pet:
            self.pet = activity.pet
        self.image_available = self.image_available or activity.image_available
        self.video_available = self.video_available or activity.video_available
        self.note_delivery(source, received_at)

    def merge_event_enrichment(self, activity: OnlyCatActivity) -> None:
        """Apply event-level metadata to another row from the same physical event."""
        if self._is_meaningful(activity.automatic_classification):
            self.automatic_classification = activity.automatic_classification
        if self._is_meaningful(activity.manual_classification):
            self.manual_classification = activity.manual_classification
        if self._is_meaningful(activity.classification):
            self.classification = activity.classification
        if self._is_meaningful(activity.trigger_source):
            self.trigger_source = activity.trigger_source
        self.image_available = self.image_available or activity.image_available
        self.video_available = self.video_available or activity.video_available

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-safe representation containing no media credentials."""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        data["first_received_at"] = self.first_received_at.isoformat()
        data["last_received_at"] = self.last_received_at.isoformat()
        data["cursor"] = self.cursor
        data["processing_latency_seconds"] = max(
            0.0, (self.first_received_at - self.timestamp).total_seconds()
        )
        return data

    def as_storage_dict(self) -> dict[str, Any]:
        """Return the persistent form without computed presentation fields."""
        data = self.as_dict()
        data.pop("cursor", None)
        data.pop("processing_latency_seconds", None)
        return data


class ActivityStore(Store[dict[str, Any]]):
    """Versioned Home Assistant Store with a safe schema-1 migration."""

    def __init__(
        self,
        hass: HomeAssistant,
        version_or_key: int | str,
        key: str | None = None,
    ) -> None:
        storage_key = version_or_key if key is None else key
        super().__init__(hass, ACTIVITY_STORE_VERSION, str(storage_key), private=True)

    async def _async_migrate_func(
        self,
        old_major_version: int,
        _old_minor_version: int,
        old_data: dict[str, Any],
    ) -> dict[str, Any]:
        """Carry forward schema-1 deduplication state and add an empty journal."""
        if old_major_version == 1 and isinstance(old_data, dict):
            return {
                "last_event_ids": old_data.get("last_event_ids", {}),
                "emitted": old_data.get("emitted", {}),
                "journal": [],
            }
        return {"last_event_ids": {}, "emitted": {}, "journal": []}


class ActivityJournal:
    """In-memory bounded journal persisted by the EventStore."""

    def __init__(self) -> None:
        self._entries: list[ActivityJournalEntry] = []
        self._by_key: dict[tuple[str, int, str, str, str], ActivityJournalEntry] = {}

    def load(self, records: object, now: datetime | None = None) -> None:
        """Restore valid records and enforce retention."""
        self._entries = []
        self._by_key = {}
        if isinstance(records, list):
            for record in records:
                entry = ActivityJournalEntry.from_dict(record)
                if entry is not None:
                    self._entries.append(entry)
                    self._by_key[entry.semantic_key] = entry
        for device_id, event_id in {
            (entry.device_id, entry.event_id) for entry in self._entries
        }:
            self.harmonize_event_enrichment(device_id, event_id)
        self.prune(now)

    def prune(self, now: datetime | None = None) -> None:
        """Enforce maximum age and row count."""
        now = _as_utc(now or datetime.now(UTC))
        oldest = now - timedelta(days=ACTIVITY_JOURNAL_MAX_AGE_DAYS)
        self._entries = [entry for entry in self._entries if entry.timestamp >= oldest]
        self._entries.sort(key=lambda entry: (entry.timestamp, entry.event_id))
        if len(self._entries) > ACTIVITY_JOURNAL_MAX_ENTRIES:
            self._entries = self._entries[-ACTIVITY_JOURNAL_MAX_ENTRIES:]
        self._by_key = {entry.semantic_key: entry for entry in self._entries}

    def append(self, entry: ActivityJournalEntry) -> None:
        """Append one new semantic activity."""
        self._entries.append(entry)
        self._by_key[entry.semantic_key] = entry
        self.prune()

    def get(self, key: tuple[str, int, str, str, str]) -> ActivityJournalEntry | None:
        """Return an existing semantic activity."""
        return self._by_key.get(key)

    def propagate_event_enrichment(self, activity: OnlyCatActivity) -> None:
        """Keep event-level metadata consistent across every RFID/subevent row."""
        for entry in self._entries:
            if (
                entry.device_id == activity.device_id
                and entry.event_id == activity.event_id
            ):
                entry.merge_event_enrichment(activity)

    def harmonize_event_enrichment(self, device_id: str, event_id: int) -> None:
        """Project the best event-level metadata onto every row for an event."""
        matching = [
            entry
            for entry in self._entries
            if entry.device_id == device_id and entry.event_id == event_id
        ]
        if not matching:
            return
        manual = next(
            (
                entry.manual_classification
                for entry in reversed(matching)
                if entry._is_meaningful(entry.manual_classification)
            ),
            None,
        )
        automatic = next(
            (
                entry.automatic_classification
                for entry in reversed(matching)
                if entry._is_meaningful(entry.automatic_classification)
            ),
            None,
        )
        effective = manual or next(
            (
                entry.classification
                for entry in reversed(matching)
                if entry._is_meaningful(entry.classification)
            ),
            automatic,
        )
        trigger_source = next(
            (
                entry.trigger_source
                for entry in reversed(matching)
                if entry._is_meaningful(entry.trigger_source)
            ),
            None,
        )
        image_available = any(entry.image_available for entry in matching)
        video_available = any(entry.video_available for entry in matching)
        for entry in matching:
            if automatic:
                entry.automatic_classification = automatic
            if manual:
                entry.manual_classification = manual
            if effective:
                entry.classification = effective
            if trigger_source:
                entry.trigger_source = trigger_source
            entry.image_available = image_available
            entry.video_available = video_available

    def latest(self, device_id: str | None = None) -> ActivityJournalEntry | None:
        """Return the newest entry, optionally for one device."""
        for entry in reversed(self._entries):
            if device_id is None or entry.device_id == device_id:
                return entry
        return None

    def contains_event(self, device_id: str, event_id: int) -> bool:
        """Return whether the journal permits access to a device event."""
        return any(
            entry.device_id == device_id and entry.event_id == event_id
            for entry in self._entries
        )

    def event_image_available(self, device_id: str, event_id: int) -> bool:
        """Return whether a permitted journal event advertises an image."""
        return any(
            entry.device_id == device_id
            and entry.event_id == event_id
            and entry.image_available
            for entry in self._entries
        )

    def records(self) -> list[dict[str, Any]]:
        """Return the persistent representation."""
        return [entry.as_storage_dict() for entry in self._entries]

    def page(
        self,
        *,
        device_id: str | None,
        limit: int,
        before: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Return a newest-first cursor page."""
        entries = [
            entry
            for entry in reversed(self._entries)
            if device_id is None or entry.device_id == device_id
        ]
        if before is not None:
            try:
                cursor_index = next(
                    index for index, entry in enumerate(entries) if entry.cursor == before
                )
            except StopIteration:
                return [], None
            entries = entries[cursor_index + 1 :]
        selected = entries[:limit]
        next_cursor = selected[-1].cursor if len(entries) > limit and selected else None
        return [entry.as_dict() for entry in selected], next_cursor
