// =====================================================================
// encoding_queue.js — H.264/H.265 Encoding Queue Modal + Badge polling
// Design: identical to queue.js / upscale_queue.js / queue.css
// =====================================================================

let encodingModalOpen = false;
let encodingPollTimer = null;
let _encodingFilter   = "all";
let _lastEncodingProgress = {};

// ── Open / Close ────────────────────────────────────────────────────
function openEncodingQueueModal() {
  encodingModalOpen = true;
  document.getElementById("encodingOverlay").style.display = "block";
  _checkEncodingDisabled();
  loadEncodingQueue();
  if (encodingPollTimer) clearInterval(encodingPollTimer);
  encodingPollTimer = setInterval(loadEncodingQueue, 2000);
}

async function _checkEncodingDisabled() {
  try {
    const r = await fetch("/api/encoding/timing");
    const d = await r.json();
    const disabled = !d.ok || (d.settings && d.settings.timing !== "after_download");
    const badge = document.getElementById("encodingDisabledBadge");
    if (badge) badge.style.display = disabled ? "" : "none";
  } catch(e) {}
}

function closeEncodingQueueModal() {
  encodingModalOpen = false;
  document.getElementById("encodingOverlay").style.display = "none";
  if (encodingPollTimer) { clearInterval(encodingPollTimer); encodingPollTimer = null; }
}

// ── Filter ──────────────────────────────────────────────────────────
function setEncodingFilter(f) {
  _encodingFilter = f;
  document.querySelectorAll('[id^="eqfTab-"]').forEach(b => b.classList.remove("active"));
  const tab = document.getElementById("eqfTab-" + f);
  if (tab) tab.classList.add("active");
  loadEncodingQueue();
}

// ── Load & Render ───────────────────────────────────────────────────
async function loadEncodingQueue() {
  try {
    const [qr, pr] = await Promise.all([
      fetch("/api/encoding/queue"),
      fetch("/api/encoding/queue/progress"),
    ]);
    const qd = await qr.json();
    const pd = await pr.json();
    if (!qd.ok) return;

    const items    = qd.items || [];
    const progress = pd.ok ? (pd.progress || {}) : {};
    _lastEncodingProgress = progress;

    _updateEncodingBadges(qd.badge || 0);
    _updateEqFilterCounts(items);

    if (!encodingModalOpen) return;
    _renderEncodingQueue(items, progress);
  } catch(e) { /* ignore */ }
}

function _updateEqFilterCounts(items) {
  const counts = {
    all:       items.length,
    active:    items.filter(i => i.status === "running").length,
    queued:    items.filter(i => i.status === "queued").length,
    completed: items.filter(i => i.status === "completed").length,
    failed:    items.filter(i => ["failed","cancelled"].includes(i.status)).length,
  };
  Object.keys(counts).forEach(k => {
    const el = document.getElementById("eqfCount-" + k);
    if (!el) return;
    el.textContent = counts[k] ? counts[k] : "";
    el.style.display = counts[k] ? "" : "none";
  });
}

function _updateEncodingBadges(count) {
  ["encodingBadge","mobileEncodingBadge"].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = count > 0 ? "" : "none";
    if (count > 0) el.textContent = count;
  });
  if (window.updateTotalQueueBadge) window.updateTotalQueueBadge();
}

// ── Helpers ─────────────────────────────────────────────────────────
function _eEsc(s) {
  return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
                      .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function _eFilename(p) {
  return (p||"").replace(/\\/g,"/").split("/").pop() || p;
}
// Series title: everything before " – " (or full string)
function _eSeriesTitle(title) {
  if (!title) return "";
  const idx = title.indexOf(" – ");
  return idx > 0 ? title.substring(0, idx) : title;
}
// Subtitle: everything after " – " (filename part)
function _eSubtitle(title) {
  if (!title) return "";
  const idx = title.indexOf(" – ");
  return idx > 0 ? title.substring(idx + 3) : "";
}

// ── Render ──────────────────────────────────────────────────────────
function _renderEncodingQueue(items, progress) {
  const list = document.getElementById("encodingQueueList");
  if (!list) return;

  let visible = items;
  if      (_encodingFilter === "active")    visible = items.filter(i => i.status === "running");
  else if (_encodingFilter === "queued")    visible = items.filter(i => i.status === "queued");
  else if (_encodingFilter === "completed") visible = items.filter(i => i.status === "completed").reverse();
  else if (_encodingFilter === "failed")    visible = items.filter(i => ["failed","cancelled"].includes(i.status)).reverse();
  else {
    // "all": active on top, then last 3 finished (newest first)
    const running = items.filter(i => i.status === "running");
    const queued  = items.filter(i => i.status === "queued");
    const done    = items
      .filter(i => ["completed","failed","cancelled"].includes(i.status))
      .slice(-3).reverse();
    visible = running.concat(queued, done);
  }

  if (!visible.length) {
    const emptyMsgs = {
      active:    t("Kein Encoding läuft gerade.", "No encoding is currently running."),
      queued:    t("Keine wartenden Encoding-Jobs.", "No pending encoding jobs."),
      completed: t("Keine abgeschlossenen Encoding-Jobs.", "No completed encoding jobs."),
      failed:    t("Keine fehlgeschlagenen oder abgebrochenen Encoding-Jobs.", "No failed or cancelled encoding jobs."),
      all:       t("Encoding-Warteschlange ist leer", "Encoding queue is empty"),
    };
    list.innerHTML = '<div class="queue-empty">' + (_encodingFilter in emptyMsgs ? emptyMsgs[_encodingFilter] : emptyMsgs.all) + '</div>';
    return;
  }

  const queued = visible.filter(i => i.status === "queued");

  let html = "";
  visible.forEach(item => {
    const isRunning   = item.status === "running";
    const isQueued    = item.status === "queued";
    const isDone      = item.status === "completed";
    const isFailed    = item.status === "failed";
    const isCancelled = item.status === "cancelled";
    const isStopped   = isDone || isFailed || isCancelled;

    const cls = isRunning ? "queue-item queue-item-active" : "queue-item";

    let statusBadge = "";
    if (isRunning)
      statusBadge = '<span class="queue-status queue-status-running">' + t("Läuft", "Running") + '</span>';
    else if (isQueued)
      statusBadge = '<span class="queue-status queue-status-queued">' + t("Wartend", "Queued") + '</span>';
    else if (isDone)
      statusBadge = '<span class="queue-status queue-status-completed">' + t("Abgeschlossen", "Completed") + '</span>';
    else if (isFailed)
      statusBadge = '<span class="queue-status queue-status-failed">' + t("Fehlgeschlagen", "Failed") + '</span>';
    else if (isCancelled)
      statusBadge = '<span class="queue-status queue-status-cancelled">' + t("Abgebrochen", "Cancelled") + '</span>';

    const seriesTitle = _eSeriesTitle(item.title) || _eFilename(item.file_path);
    const subtitlePart = _eSubtitle(item.title);

    const totalFiles = item.total_files || 1;
    const curFileIdx = isRunning ? (item.current_file_idx || 0)
                     : (isStopped ? totalFiles : (item.current_file_idx || 0));

    const curFilePct = isRunning && progress.active ? (progress.percent || 0) : 0;
    const overallPct = isDone ? 100
      : isRunning ? Math.min(Math.round(curFileIdx / totalFiles * 100 + curFilePct / totalFiles), 99)
      : Math.round(item.progress_pct || 0);
    const filePct    = isRunning && progress.active ? Math.round(progress.percent || 0) : (isDone ? 100 : 0);

    let progressHtml = "";
    if (isRunning || isQueued || isDone) {
      let fileLabel;
      if (totalFiles > 1) {
        fileLabel = (curFileIdx + (isRunning ? 1 : 0)) + "/" + totalFiles + " " + t("Dateien", "Files");
      } else {
        fileLabel = _eFilename(isRunning && progress.file ? progress.file : item.file_path);
      }

      let pillsHtml = "";
      if (isRunning && progress.active) {
        if (progress.speed)
          pillsHtml += '<span class="queue-meta-pill queue-progress-pill">⚡ ' + _eEsc(progress.speed) + '</span>';
        if (progress.time)
          pillsHtml += '<span class="queue-meta-pill queue-progress-pill">' + _eEsc(progress.time) + '</span>';
      }

      progressHtml =
        '<div class="queue-progress">' +
        '<div class="queue-progress-bar"><div class="queue-progress-fill queue-progress-fill--encoding" style="width:' + overallPct + '%"></div></div>' +
        '<div class="queue-progress-footer">' +
        '<span>' + _eEsc(fileLabel) + '</span>' +
        '<span class="queue-progress-pct">' + overallPct + '%</span>' +
        '</div>';

      if (totalFiles > 1 && isRunning) {
        progressHtml +=
          '<div class="queue-progress-bar queue-progress-bar--episode">' +
          '<div class="queue-progress-fill queue-progress-fill--encoding" style="width:' + filePct + '%"></div>' +
          '</div>' +
          '<div class="queue-progress-footer">' +
          '<span class="queue-progress-phase">' + t("🎞 Datei", "🎞 File") + '</span>' +
          '<span class="queue-progress-pct">' + filePct + '%</span>' +
          '</div>';
      } else if (isRunning && totalFiles === 1) {
        progressHtml +=
          '<div class="queue-progress-bar queue-progress-bar--episode">' +
          '<div class="queue-progress-fill queue-progress-fill--encoding" style="width:' + filePct + '%"></div>' +
          '</div>' +
          '<div class="queue-progress-footer">' +
          '<span class="queue-progress-phase">🎞 Encoding</span>' +
          '<span class="queue-progress-pct">' + filePct + '%</span>' +
          '</div>';
      }

      if (pillsHtml)
        progressHtml += '<div class="queue-progress-pills">' + pillsHtml + '</div>';

      if (isFailed && item.error)
        progressHtml += '<div class="queue-errors">' + _eEsc(item.error) + '</div>';

      progressHtml += '</div>';
    }

    const queuedIdx = queued.indexOf(item);
    let actionBtn = "";
    if (isQueued) {
      const isFirst = queuedIdx === 0;
      const isLast  = queuedIdx === queued.length - 1;
      actionBtn =
        '<button class="queue-move" onclick="moveEncodingItem(' + item.id + ',\'up\')"   title="'+ t("Nach oben", "Up") +'"  ' + (isFirst ? ' disabled' : '') + '>&#9650;</button>' +
        '<button class="queue-move" onclick="moveEncodingItem(' + item.id + ',\'down\')" title="'+ t("Nach unten", "Down") +'" ' + (isLast  ? ' disabled' : '') + '>&#9660;</button>' +
        '<button class="queue-remove" onclick="removeEncodingItem(' + item.id + ')" title="'+ t("Entfernen", "Remove") +'">&times;</button>';
    } else if (isRunning) {
      actionBtn = '<button class="queue-cancel" onclick="cancelEncodingItem(' + item.id + ')" title="'+ t("Abbrechen", "Cancel") +'">'+ t("Abbrechen", "Cancel") +'</button>';
    } else if (isStopped) {
      actionBtn = '<button class="queue-remove" onclick="removeEncodingItem(' + item.id + ')" title="'+ t("Entfernen", "Remove") +'">&times;</button>';
    }

    let metaHtml = statusBadge;
    if (totalFiles > 1)
      metaHtml += '<span class="queue-meta-pill">' + totalFiles + ' '+ t("Dateien", "Files") +'</span>';
    if (subtitlePart)
      metaHtml += '<span class="queue-meta-pill">' + _eEsc(subtitlePart) + '</span>';

    html +=
      '<div class="' + cls + '" data-id="' + item.id + '">' +
      '<div class="queue-item-header">' +
      '<div class="queue-item-title">' + _eEsc(seriesTitle) + '</div>' +
      '<div class="queue-item-right">' + actionBtn + '</div>' +
      '</div>' +
      '<div class="queue-item-meta">' + metaHtml + '</div>' +
      progressHtml +
      '</div>';
  });

  list.innerHTML = html;
}

// ── Actions ─────────────────────────────────────────────────────────
async function cancelEncodingItem(id) {
  try { await fetch("/api/encoding/queue/" + id + "/cancel", {method:"POST"}); loadEncodingQueue(); }
  catch(e) {}
}
async function removeEncodingItem(id) {
  try { await fetch("/api/encoding/queue/" + id, {method:"DELETE"}); loadEncodingQueue(); }
  catch(e) {}
}
async function moveEncodingItem(id, direction) {
  try {
    await fetch("/api/encoding/queue/" + id + "/move", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({direction: direction}),
    });
    loadEncodingQueue();
  } catch(e) {}
}
async function clearEncodingQueue() {
  try { await fetch("/api/encoding/queue/clear", {method:"POST"}); loadEncodingQueue(); }
  catch(e) {}
}

// ── Background badge poll ────────────────────────────────────────────
function _startEncodingBadgePoll() {
  setInterval(async () => {
    if (encodingModalOpen) return;
    try {
      const d = await (await fetch("/api/encoding/queue/badge")).json();
      if (d.ok) _updateEncodingBadges(d.count || 0);
    } catch(e) {}
  }, 8000);
}

document.addEventListener("DOMContentLoaded", () => {
  _startEncodingBadgePoll();
  fetch("/api/encoding/queue/badge").then(r=>r.json()).then(d=>{
    if (d.ok) _updateEncodingBadges(d.count||0);
  }).catch(()=>{});
});
