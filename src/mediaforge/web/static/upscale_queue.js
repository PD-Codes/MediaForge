// =====================================================================
// upscale_queue.js — the upscaling queue's data source for the queue hub
// ---------------------------------------------------------------------
// The window, the merged model and the renderer all live in queue.js. This
// file only fetches /api/upscale/queue (+ progress), keeps the two badges
// fresh and hands the result to QHub.put("upscaling", …).
// openUpscaleModal() / closeUpscaleModal() are defined in queue.js and open
// the hub on this segment. library_video.js calls _updateUpscaleBadges() by name.
// =====================================================================

let _lastUpscaleProgress = {};

/** True while the hub shows this queue (on its own segment or in "Everything"). */
function _upscalePaneActive() {
  return typeof qhubPaneActive === "function" && qhubPaneActive("upscaling");
}

async function _checkUpscaleDisabled() {
  try {
    const r = await fetch("/api/upscale/settings");
    const d = await r.json();
    const disabled = !d.ok || (d.settings && d.settings.upscaling_mode === "disabled");
    const badge = document.getElementById("upscaleDisabledBadge");
    // Only meaningful on this queue's own segment — see encoding_queue.js.
    const show = disabled && window.qhubActivePane === "upscaling";
    if (badge) badge.style.display = show ? "" : "none";
  } catch (e) { /* ignore */ }
}

async function loadUpscaleQueue() {
  try {
    const [qr, pr] = await Promise.all([
      fetch("/api/upscale/queue"),
      fetch("/api/upscale/progress"),
    ]);
    const qd = await qr.json();
    const pd = await pr.json();
    if (!qd.ok) return;

    const items = qd.items || [];
    const progress = pd.ok ? (pd.progress || {}) : {};
    _lastUpscaleProgress = progress;

    _updateUpscaleBadges(qd.badge || 0);
    if (window.qhubSetFacet) {
      window.qhubSetFacet("upscaling",
        items.filter(i => i.status === "running" || i.status === "queued").length);
    }
    if (window.QHub) window.QHub.put("upscaling", { items: items, progress: progress });
  } catch (e) { /* ignore */ }
}

function _updateUpscaleBadges(count) {
  if (window._qBadgeCounts) window._qBadgeCounts.upscaling = count || 0;
  ["upscaleBadge", "mobileUpscaleBadge"].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = count > 0 ? "" : "none";
    if (count > 0) el.textContent = count;
  });
  if (window.updateTotalQueueBadge) window.updateTotalQueueBadge();
  _upscaleBadgeCount = count || 0;
  _syncUpscaleBadgePoll();
}

// ── Actions (names referenced by generated rows) ─────────────────────
async function cancelUpscaleItem(id) {
  try { await fetch("/api/upscale/queue/" + id + "/cancel", { method: "POST" }); loadUpscaleQueue(); }
  catch (e) { /* ignore */ }
}
async function removeUpscaleItem(id) {
  try { await fetch("/api/upscale/queue/" + id, { method: "DELETE" }); loadUpscaleQueue(); }
  catch (e) { /* ignore */ }
}
async function moveUpscaleItem(id, direction) {
  try {
    await fetch("/api/upscale/queue/" + id + "/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction: direction }),
    });
    loadUpscaleQueue();
  } catch (e) { /* ignore */ }
}
async function clearUpscaleQueue() {
  try { await fetch("/api/upscale/queue/clear", { method: "POST" }); loadUpscaleQueue(); }
  catch (e) { /* ignore */ }
}

// ── Background badge poll ────────────────────────────────────────────
// Runs only while the count can change by itself: while upscaling is switched
// on (downloads queue jobs on their own), or while jobs are left -- the worker
// processes library jobs whatever the mode. With the mode at its default
// "disabled" and an empty queue, this asked every 8 s, on every page and for
// every account, for the same 0. Jobs queued from this page restart it through
// _updateUpscaleBadges().
let _upscaleBadgePoll = null;
let _upscaleBadgeCount = 0;
let _upscaleModeOff = false;   // true once the server said the mode is "disabled"

function _startUpscaleBadgePoll() {
  if (_upscaleBadgePoll) return;
  // mfPoll: paused while the tab is hidden (static/mf_poll.js).
  _upscaleBadgePoll = window.mfPoll(async () => {
    if (_upscalePaneActive()) return;   // the hub's own 2s poll is running
    // queue.js's badge poll brought the count within the last few seconds.
    if (window._mfUpscaleBadgeAt && Date.now() - window._mfUpscaleBadgeAt < 7000) return;
    try {
      const d = await (await fetch("/api/upscale/badge")).json();
      if (d.ok) _updateUpscaleBadges(d.count || 0);
    } catch (e) { /* ignore */ }
  }, 8000);
}

function _syncUpscaleBadgePoll() {
  if (!_upscaleModeOff || _upscaleBadgeCount > 0) {
    _startUpscaleBadgePoll();
  } else if (_upscaleBadgePoll) {
    window.mfPollStop(_upscaleBadgePoll);
    _upscaleBadgePoll = null;
  }
}

/** The upscaling mode, or "" when it could not be read (keeps polling). */
function _readUpscaleMode() {
  // Pages with app.js have already asked for it at startup (_loadUpscaleMode
  // memoizes the request); everywhere else this is the one extra request.
  if (typeof _loadUpscaleMode === "function") return _loadUpscaleMode();
  return fetch("/api/upscale/settings")
    .then(r => (r.ok ? r.json() : null))
    .then(d => (d && d.ok && d.settings && d.settings.mode) || "")
    .catch(() => "");
}

document.addEventListener("DOMContentLoaded", () => {
  _startUpscaleBadgePoll();
  fetch("/api/upscale/badge").then(r => r.json()).then(d => {
    if (d.ok) _updateUpscaleBadges(d.count || 0);
  }).catch(() => { });
  _readUpscaleMode().then(mode => {
    _upscaleModeOff = mode === "disabled";
    _syncUpscaleBadgePoll();
  }).catch(() => { });
});
