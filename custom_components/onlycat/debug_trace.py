"""File-based debug trace logging for OnlyCat."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)
_TRACE_PATH: Path | None = None
_TRACE_ENABLED = False


def configure_debug_trace(path: str | Path, enabled: bool) -> None:
    """Configure JSONL trace logging destination."""
    global _TRACE_PATH, _TRACE_ENABLED
    _TRACE_PATH = Path(path)
    _TRACE_ENABLED = enabled


def _sanitize(value: Any) -> Any:
    """Convert arbitrary values into JSON-safe data."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _sanitize(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in value]
    if hasattr(value, "__dict__"):
        return _sanitize(vars(value))
    return repr(value)


def trace_event(kind: str, **payload: Any) -> None:
    """Append a structured debug line when trace logging is enabled."""
    if not _TRACE_ENABLED or _TRACE_PATH is None:
        return
    try:
        _TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now().isoformat(),
            "kind": kind,
            "payload": _sanitize(payload),
        }
        with _TRACE_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=True) + "`n")
    except Exception:
        _LOGGER.exception("Failed writing OnlyCat debug trace")
