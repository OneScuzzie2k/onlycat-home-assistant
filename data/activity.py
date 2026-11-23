"""Normalized OnlyCat RFID activity."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .event import Event
    from .event_summary import EventSummary, SubEvent


_ACTIVITY_NAMES = {
    ("TRANSIT", "INWARD"): "Entered",
    ("TRANSIT", "OUTWARD"): "Exited",
    ("PEEK", "INWARD"): "Peeked inside",
    ("PEEK", "OUTWARD"): "Peeked outside",
    ("DENY", "INWARD"): "Denied entry",
    ("DENY", "OUTWARD"): "Denied exit",
    ("BREACH", "INWARD"): "Bypassed entry",
    ("BREACH", "OUTWARD"): "Bypassed exit",
}


def _enum_name(value: object) -> str | None:
    """Return an enum name without exposing its implementation."""
    if value is None:
        return None
    name = getattr(value, "name", None)
    return str(name) if name is not None else str(value)


def friendly_activity(action: str | None, direction: str | None) -> str:
    """Return a stable user-facing activity label."""
    normalized_action = (action or "UNKNOWN").upper()
    normalized_direction = (direction or "UNKNOWN").upper()
    return _ACTIVITY_NAMES.get(
        (normalized_action, normalized_direction),
        f"{normalized_action.title()} {normalized_direction.lower()}",
    )


@dataclass(frozen=True, slots=True)
class OnlyCatActivity:
    """A safe, normalized activity assembled from an event and subevent."""

    device_id: str
    event_id: int
    timestamp: datetime | None
    rfid: str
    pet: str | None
    action: str
    direction: str
    activity: str
    classification: str | None
    trigger_source: str | None
    image_available: bool
    video_available: bool
    automatic_classification: str | None = None
    manual_classification: str | None = None

    @classmethod
    def from_parts(
        cls,
        *,
        event: Event | None,
        summary: EventSummary,
        subevent: SubEvent,
        pet_name: str | None,
    ) -> OnlyCatActivity | None:
        """Build an activity when the identifiers needed for deduplication exist."""
        if (
            not subevent.rfid_code
            or summary.event_id is None
            or not summary.device_id
            or not subevent.action
            or not subevent.direction
        ):
            return None
        action = subevent.action.upper()
        direction = subevent.direction.upper()
        timestamp = (
            event.timestamp
            if event is not None and event.timestamp is not None
            else summary.timestamp
        )
        frame_count = event.frame_count if event is not None else None
        image_available = bool(
            event is not None
            and (
                event.poster_frame_index is not None
                or (frame_count is not None and frame_count > 0)
            )
        )
        video_available = bool(
            event is not None
            and event.access_token
            and frame_count is not None
            and frame_count > 0
        )
        return cls(
            device_id=summary.device_id,
            event_id=summary.event_id,
            timestamp=timestamp,
            rfid=subevent.rfid_code,
            pet=pet_name,
            action=action,
            direction=direction,
            activity=friendly_activity(action, direction),
            classification=_enum_name(
                event.effective_classification if event is not None else None
            ),
            trigger_source=_enum_name(
                event.event_trigger_source if event is not None else None
            ),
            image_available=image_available,
            video_available=video_available,
            automatic_classification=_enum_name(
                event.event_classification if event is not None else None
            ),
            manual_classification=_enum_name(
                event.event_manual_classification if event is not None else None
            ),
        )

    @property
    def dedup_key(self) -> tuple[str, int, str, str, str]:
        """Return the semantic identity used for delivery deduplication."""
        return (
            self.device_id,
            self.event_id,
            self.rfid,
            self.action,
            self.direction,
        )

    def as_event_data(self) -> dict[str, object]:
        """Return the token-free Home Assistant event payload."""
        return {
            "pet": self.pet,
            "rfid": self.rfid,
            "action": self.action,
            "direction": self.direction,
            "activity": self.activity,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "device_id": self.device_id,
            "event_id": self.event_id,
            "classification": self.classification,
            "trigger_source": self.trigger_source,
            "image_available": self.image_available,
            "video_available": self.video_available,
        }
