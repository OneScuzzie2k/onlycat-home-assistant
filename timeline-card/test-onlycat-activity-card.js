const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const registered = new Map();
const source = fs.readFileSync(`${__dirname}/onlycat-activity-card.js`, "utf8");
const context = {
  HTMLElement: class {},
  customElements: { define: (name, value) => registered.set(name, value) },
  window: {},
  Intl,
  Date,
  Set,
  Map,
};
vm.runInNewContext(source, context);
const Card = registered.get("onlycat-activity-card");

function entry(action, direction, extra = {}) {
  return {
    device_id: "OC-1",
    event_id: 20793,
    rfid: "123",
    pet: "Binx",
    action,
    direction,
    activity: "raw friendly text",
    timestamp: "2026-09-29T06:24:43+00:00",
    image_available: true,
    image_url: "/api/onlycat/activity_image/entry-1/OC-1/20793?authSig=signed",
    delivery_sources: [],
    ...extra,
  };
}

function cardWith(entries) {
  const card = new Card();
  card.setConfig({ config_entry_id: "entry-1" });
  card.entries = entries;
  return card;
}

test("groups a split PEEK and TRANSIT event with the conclusive headline", () => {
  const card = cardWith([entry("TRANSIT", "OUTWARD"), entry("PEEK", "OUTWARD")]);
  const groups = card._groups();
  assert.equal(groups.length, 1);
  assert.equal(card._friendly(groups[0].headline), "Went out");
  assert.deepEqual(Array.from(groups[0].sequence, (item) => card._friendly(item)), ["Looked out", "Went out"]);
});

test("groups across appended pagination boundaries", () => {
  const card = cardWith([entry("TRANSIT", "OUTWARD")]);
  card.entries.push(entry("PEEK", "OUTWARD"));
  assert.equal(card._groups().length, 1);
});

test("implements friendly terms and priority", () => {
  const card = cardWith([
    entry("PEEK", "INWARD"),
    entry("DENY", "INWARD"),
    entry("TRANSIT", "INWARD"),
    entry("BREACH", "INWARD"),
  ]);
  assert.equal(card._friendly(card._groups()[0].headline), "Bypassed coming in");
  assert.equal(card._friendly(entry("DENY", "OUTWARD")), "Denied exit");
});

test("discovers pet/category/unknown filters dynamically", () => {
  const card = cardWith([
    entry("PEEK", "OUTWARD"),
    entry("DENY", "INWARD", { event_id: 2, pet: "Nelson" }),
    entry("BREACH", "INWARD", { event_id: 3, pet: null, rfid: "unknown" }),
  ]);
  const labels = Array.from(card._filterOptions(), (option) => option[1]);
  assert.deepEqual(labels, ["All", "Binx", "Nelson", "Looked", "Denied", "Bypassed", "Contraband", "Suspicious", "Unknown"]);
  card.activeFilter = "unknown";
  assert.equal(card._filterGroups(card._groups()).length, 1);
});

test("filters effective event-level Contraband and Suspicious classifications", () => {
  const card = cardWith([
    entry("PEEK", "INWARD", { rfid: "123", classification: null }),
    entry("TRANSIT", "INWARD", { rfid: "456", pet: "Nelson", manual_classification: "CONTRABAND", classification: "CONTRABAND" }),
    entry("DENY", "INWARD", { event_id: 99, classification: "SUSPICIOUS", automatic_classification: "SUSPICIOUS" }),
  ]);
  const groups = card._groups();
  const sameEvent = groups.filter((group) => group.headline.event_id === 20793);
  assert.equal(sameEvent.length, 2);
  assert.deepEqual(Array.from(sameEvent, (group) => group.effectiveClassification), ["CONTRABAND", "CONTRABAND"]);
  card.activeFilter = "contraband";
  assert.equal(card._filterGroups(groups).length, 2);
  card.activeFilter = "suspicious";
  assert.equal(card._filterGroups(groups).length, 1);
});

test("preserves movement while making Contraband presentation obvious", () => {
  const card = cardWith([entry("TRANSIT", "INWARD", {
    classification: "CONTRABAND",
    automatic_classification: "CLEAR",
    manual_classification: "CONTRABAND",
  })]);
  const html = card._renderRow(card._groups()[0]);
  assert.match(html, /Came in · Contraband detected/);
  assert.match(html, /class="activity contraband"/);
  assert.match(html, /Automatic classification<\/dt><dd>CLEAR/);
  assert.match(html, /Manual classification<\/dt><dd>CONTRABAND/);
  assert.match(html, /Effective classification<\/dt><dd>CONTRABAND/);
});

test("renders play overlay only for backend-confirmed local video", () => {
  const card = cardWith([entry("TRANSIT", "OUTWARD")]);
  const group = card._groups()[0];
  assert.doesNotMatch(card._thumbnail(group), /open-video|play-overlay/);
  card.videoStates.set(group.key, {
    status: "available",
    url: "/api/onlycat/activity_video/entry-1/OC-1/20793?authSig=signed",
  });
  assert.match(card._thumbnail(group), /data-action="open-video"/);
  assert.match(card._thumbnail(group), /play-overlay/);
});

test("video modal uses native non-autoplay inline controls and closes cleanly", () => {
  const card = cardWith([entry("TRANSIT", "OUTWARD")]);
  const group = card._groups()[0];
  card.videoStates.set(group.key, {
    status: "available",
    url: "/api/onlycat/activity_video/entry-1/OC-1/20793?authSig=signed",
  });
  card._render = () => {};
  card._openVideo(group.key);
  const dialog = card._videoDialog();
  assert.match(dialog, /<video controls playsinline preload="metadata"/);
  assert.doesNotMatch(dialog, /autoplay/);
  assert.match(dialog, /data-action="close-video"/);
  const fakeVideo = { pauseCalled: false, removed: null, loaded: false,
    pause() { this.pauseCalled = true; }, removeAttribute(value) { this.removed = value; }, load() { this.loaded = true; } };
  card.querySelector = () => fakeVideo;
  card._closeVideo();
  assert.equal(fakeVideo.pauseCalled, true);
  assert.equal(fakeVideo.removed, "src");
  assert.equal(fakeVideo.loaded, true);
  assert.equal(card.activeVideo, null);
});

test("video failure removes play state and retains an unavailable message", () => {
  const card = cardWith([entry("TRANSIT", "OUTWARD")]);
  const group = card._groups()[0];
  card.videoStates.set(group.key, { status: "available", url: "/api/onlycat/activity_video/x" });
  card.activeVideo = { key: group.key, url: "/api/onlycat/activity_video/x" };
  card._closeVideo = function close() { this.activeVideo = null; };
  card._videoFailed();
  assert.equal(card.videoStates.get(group.key).status, "unavailable");
  assert.match(card._renderRow(group), /Video no longer available/);
  assert.match(card._renderRow(group), /img loading="lazy"/);
});

test("card uses only HA image paths and contains no OnlyCat credential logic", () => {
  assert.doesNotMatch(source, /gateway\.onlycat\.com|sharing\/video|accessToken|access_token|api_key/i);
  assert.match(source, /image_url/);
  assert.match(source, /Unknown visitor/);
  assert.match(source, /@media\(max-width:480px\)/);
  assert.match(source, /var\(--primary-text-color\)/);
  assert.match(source, /onlycat\/activity_video_status/);
  assert.match(source, /startsWith\("\/api\/onlycat\/activity_video\/"\)/);
  assert.match(source, /videoActive < 2/);
});
