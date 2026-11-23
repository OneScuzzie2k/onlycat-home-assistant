"""Custom types for onlycat representing a pet."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import STATE_HOME, STATE_NOT_HOME

from .activity import OnlyCatActivity
from .const import (
    ONLYCAT_DIRECTION_INWARD,
    ONLYCAT_DIRECTION_OUTWARD,
    ONLYCAT_SUBEVENT_ACTION_TRANSIT,
)

if TYPE_CHECKING:
    from datetime import datetime

    from .event import Event
    from .event_summary import EventSummary, SubEvent

_LOGGER = logging.getLogger(__name__)


@dataclass
class Pet:
    """Data representing a pet."""

    rfid_code: str
    location: str
    last_seen: datetime | None
    device_id: str | None = None
    last_seen_event: Event | None = None
    last_seen_summary: EventSummary | None = None
    label: str | None = None
    location_changed_at: datetime | None = None
    last_activity: OnlyCatActivity | None = None
    location_source: str | None = None
    location_source_event_id: int | None = None
    location_evidence_at: datetime | None = None

    def update_from_subevent(self, subevent: SubEvent | None) -> None:
        """Update pet data from a conclusive subevent."""
        if subevent is None:
            return
        direction = (subevent.direction or "").upper()
        action = (subevent.action or "").upper()
        new_location = self.location
        if action not in {ONLYCAT_SUBEVENT_ACTION_TRANSIT, "BREACH"}:
            _LOGGER.debug(
                "Ignoring inconclusive subevent for pet %s: direction=%s action=%s",
                self.rfid_code,
                direction,
                action,
            )
            return
        if direction == ONLYCAT_DIRECTION_INWARD:
            new_location = STATE_HOME
        elif direction == ONLYCAT_DIRECTION_OUTWARD:
            new_location = STATE_NOT_HOME
        if new_location != self.location:
            self.location = new_location
            if self.last_seen is not None:
                self.location_changed_at = self.last_seen
        _LOGGER.debug(
            "Updated pet %s location to %s based on subevent at %s",
            self.rfid_code,
            self.location,
            self.last_seen,
        )

    def apply_activity(self, activity: OnlyCatActivity) -> tuple[bool, bool]:
        """Apply a newer normalized activity and return data/location changes."""
        if activity.rfid != self.rfid_code:
            return False, False
        if (
            self.last_activity is not None
            and activity.timestamp is not None
            and self.last_activity.timestamp is not None
            and activity.timestamp < self.last_activity.timestamp
        ):
            return False, False

        previous = (
            self.last_seen,
            self.last_activity,
            self.location,
            self.location_changed_at,
            self.location_source,
            self.location_source_event_id,
        )
        self.last_activity = activity
        if activity.timestamp is not None and (
            self.last_seen is None or activity.timestamp >= self.last_seen
        ):
            self.last_seen = activity.timestamp

        location_changed = False
        if (
            activity.action in {ONLYCAT_SUBEVENT_ACTION_TRANSIT, "BREACH"}
            and activity.direction
            in {ONLYCAT_DIRECTION_INWARD, ONLYCAT_DIRECTION_OUTWARD}
            and activity.timestamp is not None
            and (
                self.location_evidence_at is None
                or activity.timestamp >= self.location_evidence_at
            )
        ):
            new_location = (
                STATE_HOME
                if activity.direction == ONLYCAT_DIRECTION_INWARD
                else STATE_NOT_HOME
            )
            location_changed = new_location != self.location
            self.location = new_location
            self.location_source = "onlycat_activity"
            self.location_source_event_id = activity.event_id
            if activity.timestamp is not None:
                self.location_evidence_at = activity.timestamp
                if location_changed:
                    self.location_changed_at = activity.timestamp

        current = (
            self.last_seen,
            self.last_activity,
            self.location,
            self.location_changed_at,
            self.location_source,
            self.location_source_event_id,
        )
        return current != previous, location_changed
