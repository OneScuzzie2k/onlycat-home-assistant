"""Custom types for onlycat."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry

    from custom_components.onlycat.api import OnlyCatApiClient
    from custom_components.onlycat.coordinator import OnlyCatDataUpdateCoordinator

    from .device import Device
    from .event_store import EventStore
    from .pet import Pet

_LOGGER = logging.getLogger(__name__)


type OnlyCatConfigEntry = ConfigEntry[OnlyCatData]


@dataclass
class OnlyCatData:
    """Data for the OnlyCat integration."""

    client: OnlyCatApiClient
    devices: list[Device]
    pets: list[Pet]
    event_store: EventStore
    settings: dict[str, object]
    coordinator: OnlyCatDataUpdateCoordinator
    connect_retry_task: asyncio.Task | None = None
    health_monitor_task: asyncio.Task | None = None
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
