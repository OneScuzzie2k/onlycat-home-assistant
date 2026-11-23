# OnlyCat Activity Card 1.2.0

Optional Lovelace card for the OnlyCat custom integration 2.0.7.3.3 or newer.
The Python integration works without this card.

The card groups activities belonging to the same physical event, uses friendly
OnlyCat-style wording, and displays historical thumbnails through short-lived,
Home Assistant-signed image URLs. It never connects directly to OnlyCat and it
does not receive OnlyCat API keys, event access tokens, or sharing URLs.

Historical video is fetched on demand through an authenticated, journal-scoped
Home Assistant proxy. Only a short-lived signed local URL reaches the card;
OnlyCat media credentials and upstream URLs remain server-side and are never
persisted.

Copy `onlycat-activity-card.js` to Home Assistant's `config/www` directory and
register `/local/onlycat-activity-card.js?v=1.2.0` as a JavaScript module
dashboard resource. Then add a manual card:

```yaml
type: custom:onlycat-activity-card
config_entry_id: YOUR_ONLYCAT_CONFIG_ENTRY_ID
# device_id: OPTIONAL_ONLYCAT_DEVICE_ID
page_size: 25
title: OnlyCat Activity
```

The card uses the authenticated Home Assistant WebSocket session for journal
pagination. Image and video URLs are scoped to a validated config entry,
device, and journal event, and are signed by Home Assistant for five minutes.
