const ONLYCAT_ACTIVITY_CARD_VERSION = "1.2.0";

const FRIENDLY_ACTIVITY = Object.freeze({
  "TRANSIT:INWARD": "Came in",
  "TRANSIT:OUTWARD": "Went out",
  "PEEK:INWARD": "Looked in",
  "PEEK:OUTWARD": "Looked out",
  "DENY:INWARD": "Denied entry",
  "DENY:OUTWARD": "Denied exit",
  "BREACH:INWARD": "Bypassed coming in",
  "BREACH:OUTWARD": "Bypassed going out",
});

const HEADLINE_PRIORITY = Object.freeze({ PEEK: 1, DENY: 2, TRANSIT: 3, BREACH: 4 });
const SEQUENCE_PRIORITY = Object.freeze({ PEEK: 1, DENY: 2, TRANSIT: 3, BREACH: 4 });
const ACTIVITY_ICONS = Object.freeze({
  TRANSIT_INWARD: "mdi:home-import-outline",
  TRANSIT_OUTWARD: "mdi:home-export-outline",
  PEEK: "mdi:eye-outline",
  DENY: "mdi:cancel",
  BREACH: "mdi:alert-octagon-outline",
  UNKNOWN: "mdi:cat",
});

class OnlyCatActivityCard extends HTMLElement {
  setConfig(config) {
    if (!config.config_entry_id) throw new Error("config_entry_id is required");
    this.config = { title: "OnlyCat Activity", page_size: 25, ...config };
    this.entries = [];
    this.nextCursor = null;
    this.loadedFor = null;
    this.activeFilter = "all";
    this.failedImages = new Set();
    this.videoStates = new Map();
    this.videoQueue = [];
    this.videoActive = 0;
    this.videoObserver = null;
    this.activeVideo = null;
    this.videoErrorKey = null;
  }

  set hass(hass) {
    this._hass = hass;
    const key = `${this.config.config_entry_id}:${this.config.device_id || "all"}`;
    if (this.loadedFor !== key && !this.loading) {
      this.loadedFor = key;
      this.entries = [];
      this.nextCursor = null;
      this.failedImages.clear();
      this.videoStates.clear();
      this.videoQueue = [];
      this.activeVideo = null;
      this._load(false);
    }
  }

  connectedCallback() { this._render(); }

  disconnectedCallback() {
    this.videoObserver?.disconnect();
    const video = this.querySelector("video");
    if (video) { video.pause(); video.removeAttribute("src"); video.load(); }
  }

  getCardSize() { return Math.max(3, Math.min(12, this._groups().length + 1)); }

  async _load(more) {
    if (!this._hass || this.loading) return;
    this.loading = true;
    this._render();
    const request = {
      type: "onlycat/activity_journal",
      config_entry_id: this.config.config_entry_id,
      limit: Math.max(1, Math.min(100, Number(this.config.page_size) || 25)),
    };
    if (this.config.device_id) request.device_id = this.config.device_id;
    if (more && this.nextCursor) request.before = this.nextCursor;
    try {
      const response = await this._hass.callWS(request);
      this.entries = more ? [...this.entries, ...response.entries] : response.entries;
      this.nextCursor = response.next_cursor;
      this.error = null;
    } catch (error) {
      this.error = String(error?.message || error);
    } finally {
      this.loading = false;
      this._render();
    }
  }

  _escape(value) {
    return String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
  }

  _friendly(entry) {
    const key = `${String(entry.action || "").toUpperCase()}:${String(entry.direction || "").toUpperCase()}`;
    return FRIENDLY_ACTIVITY[key] || entry.activity || "Activity detected";
  }

  _icon(entry, unknownPet = false) {
    if (unknownPet) return ACTIVITY_ICONS.UNKNOWN;
    const action = String(entry.action || "").toUpperCase();
    const direction = String(entry.direction || "").toUpperCase();
    if (action === "TRANSIT") return ACTIVITY_ICONS[`TRANSIT_${direction}`] || ACTIVITY_ICONS.UNKNOWN;
    return ACTIVITY_ICONS[action] || ACTIVITY_ICONS.UNKNOWN;
  }

  _groups() {
    const groups = new Map();
    const eventMetadata = new Map();
    for (const entry of this.entries || []) {
      const key = JSON.stringify([entry.device_id, entry.event_id, entry.rfid]);
      if (!groups.has(key)) groups.set(key, { key, entries: [] });
      groups.get(key).entries.push(entry);
      const eventKey = JSON.stringify([entry.device_id, entry.event_id]);
      if (!eventMetadata.has(eventKey)) eventMetadata.set(eventKey, []);
      eventMetadata.get(eventKey).push(entry);
    }
    return [...groups.values()].map((group) => {
      const sequence = [...group.entries].sort((a, b) => {
        const difference = (SEQUENCE_PRIORITY[String(a.action).toUpperCase()] || 99)
          - (SEQUENCE_PRIORITY[String(b.action).toUpperCase()] || 99);
        return difference || String(a.direction).localeCompare(String(b.direction));
      });
      const headline = [...group.entries].sort((a, b) =>
        (HEADLINE_PRIORITY[String(b.action).toUpperCase()] || 0)
        - (HEADLINE_PRIORITY[String(a.action).toUpperCase()] || 0))[0];
      const eventEntries = eventMetadata.get(JSON.stringify([headline.device_id, headline.event_id])) || group.entries;
      const automaticClassification = this._unique(eventEntries.map((entry) => entry.automatic_classification)).at(-1) || null;
      const manualClassification = this._unique(eventEntries.map((entry) => entry.manual_classification)).at(-1) || null;
      const effectiveClassification = manualClassification
        || this._unique(eventEntries.map((entry) => entry.classification)).at(-1)
        || automaticClassification;
      return {
        ...group,
        sequence,
        headline,
        automaticClassification,
        manualClassification,
        effectiveClassification,
      };
    });
  }

  _filterGroups(groups) {
    if (this.activeFilter === "all") return groups;
    if (this.activeFilter === "unknown") return groups.filter((group) => !group.entries.some((entry) => entry.pet));
    if (this.activeFilter.startsWith("pet:")) {
      const name = decodeURIComponent(this.activeFilter.slice(4));
      return groups.filter((group) => group.entries.some((entry) => entry.pet === name));
    }
    if (this.activeFilter === "contraband" || this.activeFilter === "suspicious") {
      const wantedClassification = this.activeFilter.toUpperCase();
      return groups.filter((group) => String(group.effectiveClassification).toUpperCase() === wantedClassification);
    }
    const wanted = { looked: "PEEK", denied: "DENY", bypassed: "BREACH" }[this.activeFilter];
    return groups.filter((group) => group.entries.some((entry) => String(entry.action).toUpperCase() === wanted));
  }

  _filterOptions() {
    const names = [...new Set((this.entries || []).map((entry) => entry.pet).filter(Boolean))]
      .sort((a, b) => a.localeCompare(b));
    const options = [["all", "All"], ...names.map((name) => [`pet:${encodeURIComponent(name)}`, name])];
    options.push(
      ["looked", "Looked"], ["denied", "Denied"], ["bypassed", "Bypassed"],
      ["contraband", "Contraband"], ["suspicious", "Suspicious"], ["unknown", "Unknown"],
    );
    return options;
  }

  _relativeTime(value) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    const seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return "just now";
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"} ago`;
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
    const days = Math.floor(hours / 24);
    if (days < 7) return `${days} day${days === 1 ? "" : "s"} ago`;
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
  }

  _exactTime(value) {
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value || ""
      : new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(date);
  }

  _unique(values) {
    return [...new Set(values.filter((value) => value !== null && value !== undefined && value !== ""))];
  }

  _details(group) {
    const entries = group.entries;
    const headline = group.headline;
    const sequence = group.sequence.map((entry) => this._friendly(entry));
    const firstReceived = entries.map((entry) => entry.first_received_at).filter(Boolean).sort()[0];
    const lastReceived = entries.map((entry) => entry.last_received_at).filter(Boolean).sort().at(-1);
    const fields = [
      ["Pet", entries.find((entry) => entry.pet)?.pet || "Unknown RFID"],
      ["RFID", headline.rfid],
      ["Friendly headline", this._friendly(headline)],
      ["Activity sequence", sequence.join(" → ")],
      ["Raw actions", this._unique(group.sequence.map((entry) => entry.action)).join(" → ")],
      ["Directions", this._unique(group.sequence.map((entry) => entry.direction)).join(", ")],
      ["Automatic classification", group.automaticClassification],
      ["Manual classification", group.manualClassification],
      ["Effective classification", group.effectiveClassification],
      ["Trigger source", this._unique(entries.map((entry) => entry.trigger_source)).join(", ")],
      ["OnlyCat timestamp", this._exactTime(headline.timestamp)],
      ["HA first received", this._exactTime(firstReceived)],
      ["HA last received", this._exactTime(lastReceived)],
      ["Processing latency (seconds)", headline.processing_latency_seconds],
      ["Event ID", headline.event_id],
      ["Image available", entries.some((entry) => entry.image_available)],
      ["Video status", this.videoStates.get(group.key)?.status || "Not checked"],
      ["Location before", headline.location_before],
      ["Location after", headline.location_after],
      ["Location changed", headline.location_changed],
      ["Location source", headline.location_source],
      ["Duplicate deliveries", entries.reduce((sum, entry) => sum + (Number(entry.duplicate_delivery_count) || 0), 0)],
      ["Delivery sources", this._unique(entries.flatMap((entry) => entry.delivery_sources || [])).join(", ")],
    ];
    return fields.filter(([, value]) => value !== null && value !== undefined && value !== "")
      .map(([label, value]) => `<dt>${this._escape(label)}</dt><dd>${this._escape(value)}</dd>`).join("");
  }

  _thumbnail(group) {
    const imageEntry = group.entries.find((entry) => entry.image_available && entry.image_url);
    const image = imageEntry && !this.failedImages.has(group.key)
      ? `<img loading="lazy" src="${this._escape(imageEntry.image_url)}" alt="Activity thumbnail" data-image-key="${this._escape(group.key)}">` : "";
    const icon = this._icon(group.headline, !group.entries.some((entry) => entry.pet));
    const video = this.videoStates.get(group.key);
    const contents = `${image}<ha-icon icon="${icon}"></ha-icon>${video?.status === "available"
      ? '<span class="play-overlay" aria-hidden="true"><ha-icon icon="mdi:play"></ha-icon></span>' : ""}`;
    if (video?.status === "available") {
      return `<button type="button" class="thumbnail media-button${image ? " has-image" : ""}" data-action="open-video" data-video-key="${this._escape(group.key)}" aria-label="Play historical video">${contents}</button>`;
    }
    return `<span class="thumbnail${image ? " has-image" : ""}" data-video-candidate="true" data-video-key="${this._escape(group.key)}" data-device-id="${this._escape(group.headline.device_id)}" data-event-id="${this._escape(group.headline.event_id)}">${contents}</span>`;
  }

  _classificationPresentation(group) {
    const classification = String(group.effectiveClassification || "").toUpperCase();
    if (classification === "CONTRABAND") return { label: "Contraband detected", css: "contraband", icon: "mdi:alert-circle" };
    if (classification === "SUSPICIOUS") return { label: "Suspicious activity", css: "suspicious", icon: "mdi:alert-outline" };
    return null;
  }

  _renderRow(group) {
    const headline = group.headline;
    const pet = group.entries.find((entry) => entry.pet)?.pet || "Unknown visitor";
    const icon = this._icon(headline, !group.entries.some((entry) => entry.pet));
    const classification = this._classificationPresentation(group);
    const friendly = `${this._friendly(headline)}${classification ? ` · ${classification.label}` : ""}`;
    const videoError = this.videoErrorKey === group.key ? '<span class="video-unavailable">Video no longer available</span>' : "";
    return `<details class="activity${pet === "Unknown visitor" ? " unknown" : ""}${classification ? ` ${classification.css}` : ""}">
      <summary>${this._thumbnail(group)}
        <span class="summary-copy"><strong>${this._escape(pet)}</strong>
          <span class="friendly">${this._escape(friendly)}</span>${videoError}
          <time title="${this._escape(this._exactTime(headline.timestamp))}">${this._escape(this._relativeTime(headline.timestamp))}</time>
        </span>
        <span class="row-icon"><ha-icon icon="${classification?.icon || icon}"></ha-icon></span>
        <ha-icon class="chevron" icon="mdi:chevron-down"></ha-icon>
      </summary><div class="diagnostics"><dl>${this._details(group)}</dl></div>
    </details>`;
  }

  _queueVideoStatus(group) {
    if (!this._hass || this.videoStates.has(group.key) || this.videoQueue.some((item) => item.key === group.key)) return;
    this.videoStates.set(group.key, { status: "loading" });
    this.videoQueue.push({ key: group.key, device_id: group.headline.device_id, event_id: group.headline.event_id });
    this._drainVideoQueue();
  }

  _drainVideoQueue() {
    while (this.videoActive < 2 && this.videoQueue.length) {
      const item = this.videoQueue.shift();
      this.videoActive += 1;
      this._hass.callWS({
        type: "onlycat/activity_video_status",
        config_entry_id: this.config.config_entry_id,
        device_id: item.device_id,
        event_id: item.event_id,
      }).then((response) => {
        const localUrl = typeof response?.video_url === "string"
          && response.video_url.startsWith("/api/onlycat/activity_video/") ? response.video_url : null;
        this.videoStates.set(item.key, {
          status: response?.available && localUrl ? "available" : "unavailable",
          url: localUrl,
        });
      }).catch(() => this.videoStates.set(item.key, { status: "unavailable", url: null }))
        .finally(() => { this.videoActive -= 1; this._render(); this._drainVideoQueue(); });
    }
  }

  _observeVideoCandidates(groups) {
    this.videoObserver?.disconnect();
    const candidates = [...this.querySelectorAll("[data-video-candidate]")];
    if (typeof IntersectionObserver === "function") {
      this.videoObserver = new IntersectionObserver((items) => {
        for (const item of items) {
          if (!item.isIntersecting) continue;
          const group = groups.find((candidate) => candidate.key === item.target.dataset.videoKey);
          if (group) this._queueVideoStatus(group);
          this.videoObserver?.unobserve(item.target);
        }
      }, { rootMargin: "160px" });
      candidates.forEach((candidate) => this.videoObserver.observe(candidate));
      return;
    }
    groups.filter((group) => !this.videoStates.has(group.key)).slice(0, 6)
      .forEach((group) => this._queueVideoStatus(group));
  }

  _openVideo(key) {
    const state = this.videoStates.get(key);
    if (state?.status !== "available" || !state.url) return;
    this.videoErrorKey = null;
    this.activeVideo = { key, url: state.url };
    this._render();
  }

  _closeVideo() {
    const video = this.querySelector("video");
    if (video) { video.pause(); video.removeAttribute("src"); video.load(); }
    this.activeVideo = null;
    this._render();
  }

  _videoFailed() {
    if (this.activeVideo) {
      this.videoStates.set(this.activeVideo.key, { status: "unavailable", url: null });
      this.videoErrorKey = this.activeVideo.key;
    }
    this._closeVideo();
  }

  _videoDialog() {
    if (!this.activeVideo) return "";
    return `<div class="video-backdrop" role="dialog" aria-modal="true" aria-label="Historical OnlyCat video">
      <div class="video-dialog"><div class="video-header"><strong>Activity video</strong><button type="button" class="close-video" data-action="close-video" aria-label="Close video"><ha-icon icon="mdi:close"></ha-icon></button></div>
      <div class="video-loading">Loading video…</div>
      <video controls playsinline preload="metadata" src="${this._escape(this.activeVideo.url)}"></video></div>
    </div>`;
  }

  _render() {
    if (!this.config) return;
    const groups = this._filterGroups(this._groups());
    const rows = groups.map((group) => this._renderRow(group)).join("");
    const options = this._filterOptions().map(([value, label]) =>
      `<option value="${this._escape(value)}"${value === this.activeFilter ? " selected" : ""}>${this._escape(label)}</option>`).join("");
    this.innerHTML = `<ha-card><style>
      .header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:16px 16px 10px}.title{color:var(--primary-text-color);font-size:var(--ha-card-header-font-size,24px);font-weight:500}
      select{max-width:44%;border:1px solid var(--divider-color);border-radius:16px;padding:5px 24px 5px 10px;color:var(--primary-text-color);background:var(--card-background-color)}.content{padding:0 12px 14px}.activity{border-top:1px solid var(--divider-color)}.activity.unknown{border-left:3px solid var(--warning-color,#f0a000)}.activity.contraband{border-left:4px solid var(--error-color,#db4437)}.activity.suspicious{border-left:4px solid var(--warning-color,#f0a000)}
      summary{min-height:76px;cursor:pointer;display:grid;grid-template-columns:88px minmax(0,1fr) 32px 22px;align-items:center;gap:10px;padding:10px 4px;list-style:none}summary::-webkit-details-marker{display:none}.thumbnail{position:relative;display:grid;place-items:center;width:88px;height:66px;overflow:hidden;border:0;border-radius:10px;padding:0;margin:0;color:var(--secondary-text-color);background:var(--secondary-background-color)}.thumbnail img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;z-index:1}.thumbnail>ha-icon{--mdc-icon-size:30px}.media-button{cursor:pointer}.play-overlay{position:absolute;z-index:2;display:grid;place-items:center;width:38px;height:38px;border-radius:50%;color:white;background:rgba(0,0,0,.68);box-shadow:0 1px 5px rgba(0,0,0,.4)}.play-overlay ha-icon{--mdc-icon-size:27px}
      .summary-copy{min-width:0;display:flex;flex-direction:column;gap:2px}.summary-copy strong{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:1rem;color:var(--primary-text-color)}.friendly{color:var(--primary-text-color)}.video-unavailable{color:var(--error-color);font-size:.82rem}time{color:var(--secondary-text-color);font-size:.84rem}.row-icon{display:grid;place-items:center;color:var(--primary-color)}.contraband .row-icon{color:var(--error-color,#db4437)}.suspicious .row-icon{color:var(--warning-color,#f0a000)}.chevron{color:var(--secondary-text-color);transition:transform .16s ease}details[open] .chevron{transform:rotate(180deg)}
      .diagnostics{margin:0 4px 12px 102px;padding:12px;border-radius:10px;background:var(--secondary-background-color)}dl{display:grid;grid-template-columns:minmax(9em,auto) 1fr;gap:5px 12px;margin:0;overflow-wrap:anywhere;font-size:.86rem}dt{color:var(--secondary-text-color)}dd{margin:0;color:var(--primary-text-color)}.load-more{margin-top:12px;width:100%;padding:9px;border:1px solid var(--divider-color);border-radius:8px;color:var(--primary-text-color);background:var(--secondary-background-color);cursor:pointer}.message{padding:16px 4px;color:var(--secondary-text-color);text-align:center}.error{color:var(--error-color)}.version{padding-top:8px;color:var(--secondary-text-color);text-align:right;font-size:.68rem;opacity:.7}
      .video-backdrop{position:fixed;inset:0;z-index:9999;display:grid;place-items:center;padding:18px;background:rgba(0,0,0,.72)}.video-dialog{width:min(760px,96vw);max-height:92vh;overflow:auto;border-radius:14px;padding:14px;background:var(--card-background-color);box-shadow:0 12px 48px rgba(0,0,0,.55)}.video-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;color:var(--primary-text-color)}.close-video{display:grid;place-items:center;width:40px;height:40px;border:0;border-radius:50%;padding:0;color:var(--primary-text-color);background:var(--secondary-background-color);cursor:pointer}.video-dialog video{display:block;width:100%;max-height:75vh;border-radius:10px;background:#000}.video-loading{padding:8px;color:var(--secondary-text-color);text-align:center}
      @media(max-width:480px){.header{padding:14px 12px 8px}.title{font-size:20px}.content{padding:0 8px 12px}summary{grid-template-columns:76px minmax(0,1fr) 27px 20px;gap:8px}.thumbnail{width:76px;height:60px}.diagnostics{margin-left:4px}dl{grid-template-columns:1fr;gap:2px}dd{margin-bottom:7px}.video-backdrop{padding:8px}.video-dialog{width:calc(100vw - 16px);padding:10px}}
      </style><div class="header"><span class="title">${this._escape(this.config.title)}</span><select aria-label="Activity filter">${options}</select></div>
      <div class="content">${this.error ? `<div class="message error">${this._escape(this.error)}</div>` : ""}${rows || (!this.loading ? '<div class="message">No matching activity recorded yet.</div>' : "")}${this.loading ? '<div class="message">Loading…</div>' : ""}${this.nextCursor && !this.loading ? '<button type="button" class="load-more" data-action="load-more">Load more</button>' : ""}<div class="version">Activity Card ${ONLYCAT_ACTIVITY_CARD_VERSION}</div></div></ha-card>${this._videoDialog()}`;
    this.querySelector("select")?.addEventListener("change", (event) => { this.activeFilter = event.target.value; this._render(); });
    this.querySelector('[data-action="load-more"]')?.addEventListener("click", () => this._load(true));
    for (const button of this.querySelectorAll('[data-action="open-video"]')) {
      button.addEventListener("click", (event) => { event.preventDefault(); event.stopPropagation(); this._openVideo(button.dataset.videoKey); });
    }
    this.querySelector('[data-action="close-video"]')?.addEventListener("click", () => this._closeVideo());
    this.querySelector(".video-backdrop")?.addEventListener("click", (event) => { if (event.target === event.currentTarget) this._closeVideo(); });
    const video = this.querySelector("video");
    video?.addEventListener("loadeddata", () => this.querySelector(".video-loading")?.remove(), { once: true });
    video?.addEventListener("error", () => this._videoFailed(), { once: true });
    for (const image of this.querySelectorAll("img[data-image-key]")) {
      image.addEventListener("error", () => { this.failedImages.add(image.dataset.imageKey); image.remove(); }, { once: true });
    }
    this._observeVideoCandidates(groups);
  }
}

customElements.define("onlycat-activity-card", OnlyCatActivityCard);
window.customCards = window.customCards || [];
window.customCards.push({ type: "onlycat-activity-card", name: "OnlyCat Activity", description: "A secure, image-rich OnlyCat activity feed backed by Home Assistant.", preview: false });
