# Changelog

## 2.0.9.1 — 2026-11-23

Merged applicable fixes from stable upstream v2.0.9 (commit `340218709794e617855242be22edcb2aa0ab984e`). Its changelog groups these fixes under v2.0.8.

- Guard malformed or missing RFID last-seen timestamps during startup and reconnect snapshots.
- Scope cached camera previews and images to their event ID. A failed fetch for a new event no longer returns the previous event's thumbnail.
- Await camera stream shutdown when switching events.
- Subscribe entity listeners only after Home Assistant adds an entity, and release them on removal. Applied to custom RFID, connection and button entities as well as upstream entities.
- Return removable API, event, summary, connection and policy subscriptions. Dispatch over snapshots so removing a callback does not skip other listeners.
- Preserve the custom journal, media proxies, connection recovery, pet tracking and entity IDs.
- Adopt the standard HACS repository layout, retain the upstream license, and add test and release workflows.
- Make journal and media tests use current test timestamps so the seven-day retention policy does not expire their fixtures.

Runtime dependency pins match stable upstream. The 2.0.10 telemetry prerelease is intentionally pending stable release and review.

## Previous custom version: 2.0.7.3.3

Existing work includes activity normalization and deduplication, persisted activity history, safe historical image/video access, the optional activity card, richer pet tracking and RFID handling, and reconnect recovery. The main upstream 2.0.7 fixes were already selectively merged and have dedicated regression coverage.
