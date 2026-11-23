# OnlyCat custom integration for Home Assistant

Personal version of [OnlyCatAI's Home Assistant integration](https://github.com/OnlyCatAI/onlycat-home-assistant), with additional activity history, media playback and connection recovery features. Based on stable upstream v2.0.10, with custom version **2.0.10.1**.

## Custom features

- Persistent activity journal: up to 250 entries, retained for seven days.
- Optional activity card with grouped events, filters, historical thumbnails and video playback.
- Historical media served through Home Assistant with short-lived signed local URLs; OnlyCat media credentials stay on the server.
- Recent Activity and Last RFID Event sensors, richer pet tracking and named-pet discovery.
- Connection recovery, event reconciliation after reconnects, and diagnostic options.
- Existing entity identities and custom configuration migrations retained.

## Installation and updates

Add **https://github.com/OneScuzzie2k/onlycat-home-assistant** to HACS under **Custom repositories**, with category **Integration**. Install OnlyCat and restart Home Assistant. Configure the integration through Settings → Devices & services using an OnlyCat API key.

This version uses the same `onlycat` integration domain as the official version. Install one version at a time. When switching an existing installation, take a Home Assistant backup, replace the integration files and restart; existing configuration and entity identities are retained by the code.

For manual installation, copy `custom_components/onlycat` into Home Assistant's `config/custom_components/onlycat`, then restart. The optional card has separate [installation instructions](timeline-card/README.md).

## Development

The source of truth is `custom_components/onlycat`; tests are in `tests`, and the optional dashboard card is in `timeline-card`. Local Home Assistant configuration, diagnostic logs, environments, backups and old deployment bundles are excluded from Git.

Use Python 3.14 in a virtual environment:

```console
python -m pip install -r requirements-test.txt
python -m pytest
node --test timeline-card/test-onlycat-activity-card.js
python scripts/build_release.py
```

The Python tests are pinned to the existing local Home Assistant test baseline (2026.7.2). CI runs that baseline on Linux. Compatibility with a running Home Assistant instance still needs a deployment check.

## Maintaining this version

Keep the official repository as the Git remote named `upstream`, and your own repository as `origin`. Review official releases and compare their changes before merging: this branch has substantial custom changes to event processing, reconnects and tracking.

For each release, update `custom_components/onlycat/manifest.json` and `CHANGELOG.md`, run the checks above, commit and push, then create a matching tag such as `v2.0.10.1`. The release workflow checks the tag against the manifest and publishes the integration and optional card ZIP files. HACS can then offer that tagged version from your repository.

## Attribution

Original integration by OnlyCatAI and contributors. This derivative preserves the upstream GPL-3.0 license; see [LICENSE](LICENSE). The original maintainers are not responsible for these custom changes.
