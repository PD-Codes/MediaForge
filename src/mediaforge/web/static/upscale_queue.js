// =====================================================================
// upscale_queue.js — the upscaling queue's data source for the queue hub
// ---------------------------------------------------------------------
// The window, the merged model and the renderer all live in queue.js. This
// file only fetches /api/upscale/queue (+ progress), keeps the two badges
// fresh and hands the result to QHub.put("upscaling", …).
// openUpscaleModal() / closeUpscaleModal() are defined in queue.js and open
// the hub on this segment. library.js calls _updateUpscaleBadges() by name.
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
function _startUpscaleBadgePoll() {
  // mfPoll: paused while the tab is hidden (static/mf_poll.js).
  window.mfPoll(async () => {
    if (_upscalePaneActive()) return;   // the hub's own 2s poll is running
    try {
      const d = await (await fetch("/api/upscale/badge")).json();
      if (d.ok) _updateUpscaleBadges(d.count || 0);
    } catch (e) { /* ignore */ }
  }, 8000);
}

document.addEventListener("DOMContentLoaded", () => {
  _startUpscaleBadgePoll();
  fetch("/api/upscale/badge").then(r => r.json()).then(d => {
    if (d.ok) _updateUpscaleBadges(d.count || 0);
  }).catch(() => { });
});
