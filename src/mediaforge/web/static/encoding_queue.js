// =====================================================================
// encoding_queue.js — the encoding queue's data source for the queue hub
// ---------------------------------------------------------------------
// The window, the merged model and the renderer all live in queue.js. This
// file only fetches /api/encoding/queue (+ progress), keeps the two badges
// fresh and hands the result to QHub.put("encoding", …).
// openEncodingQueueModal() / closeEncodingQueueModal() are defined in
// queue.js and open the hub on this segment.
// =====================================================================

let _lastEncodingProgress = {};

/** True while the hub shows this queue (on its own segment or in "Everything"). */
function _encodingPaneActive() {
  return typeof qhubPaneActive === "function" && qhubPaneActive("encoding");
}

async function _checkEncodingDisabled() {
  try {
    const r = await fetch("/api/encoding/timing");
    const d = await r.json();
    const disabled = !d.ok || (d.settings && d.settings.timing !== "after_download");
    const badge = document.getElementById("encodingDisabledBadge");
    // Only meaningful on this queue's own segment — in the merged view it
    // would sit next to two other queues it says nothing about.
    const show = disabled && window.qhubActivePane === "encoding";
    if (badge) badge.style.display = show ? "" : "none";
  } catch (e) { /* ignore */ }
}

async function loadEncodingQueue() {
  try {
    const [qr, pr] = await Promise.all([
      fetch("/api/encoding/queue"),
      fetch("/api/encoding/queue/progress"),
    ]);
    const qd = await qr.json();
    const pd = await pr.json();
    if (!qd.ok) return;

    const items = qd.items || [];
    const progress = pd.ok ? (pd.progress || {}) : {};
    _lastEncodingProgress = progress;

    _updateEncodingBadges(qd.badge || 0);
    if (window.qhubSetFacet) {
      window.qhubSetFacet("encoding",
        items.filter(i => i.status === "running" || i.status === "queued").length);
    }
    if (window.QHub) window.QHub.put("encoding", { items: items, progress: progress });
  } catch (e) { /* ignore */ }
}

function _updateEncodingBadges(count) {
  if (window._qBadgeCounts) window._qBadgeCounts.encoding = count || 0;
  ["encodingBadge", "mobileEncodingBadge"].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = count > 0 ? "" : "none";
    if (count > 0) el.textContent = count;
  });
  if (window.updateTotalQueueBadge) window.updateTotalQueueBadge();
}

// ── Actions (names referenced by generated rows) ─────────────────────
async function cancelEncodingItem(id) {
  try { await fetch("/api/encoding/queue/" + id + "/cancel", { method: "POST" }); loadEncodingQueue(); }
  catch (e) { /* ignore */ }
}
async function removeEncodingItem(id) {
  try { await fetch("/api/encoding/queue/" + id, { method: "DELETE" }); loadEncodingQueue(); }
  catch (e) { /* ignore */ }
}
async function moveEncodingItem(id, direction) {
  try {
    await fetch("/api/encoding/queue/" + id + "/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction: direction }),
    });
    loadEncodingQueue();
  } catch (e) { /* ignore */ }
}
async function clearEncodingQueue() {
  try { await fetch("/api/encoding/queue/clear", { method: "POST" }); loadEncodingQueue(); }
  catch (e) { /* ignore */ }
}

// ── Background badge poll ────────────────────────────────────────────
function _startEncodingBadgePoll() {
  // mfPoll: paused while the tab is hidden (static/mf_poll.js).
  window.mfPoll(async () => {
    if (_encodingPaneActive()) return;   // the hub's own 2s poll is running
    try {
      const d = await (await fetch("/api/encoding/queue/badge")).json();
      if (d.ok) _updateEncodingBadges(d.count || 0);
    } catch (e) { /* ignore */ }
  }, 8000);
}

document.addEventListener("DOMContentLoaded", () => {
  _startEncodingBadgePoll();
  fetch("/api/encoding/queue/badge").then(r => r.json()).then(d => {
    if (d.ok) _updateEncodingBadges(d.count || 0);
  }).catch(() => { });
});
