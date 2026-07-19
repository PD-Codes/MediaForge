// =====================================================================
// upscale_queue.js — Upscaling Queue Modal + Badge polling
// Design: identical to queue.js / queue.css
// =====================================================================

let upscaleModalOpen = false;
let upscalePollTimer = null;
let _upscaleFilter   = "all";
let _lastUpscaleProgress = {};

// ── Open / Close ────────────────────────────────────────────────────
function openUpscaleModal() {
  upscaleModalOpen = true;
  document.getElementById("upscaleOverlay").style.display = "block";
  _checkUpscaleDisabled();
  loadUpscaleQueue();
  if (upscalePollTimer) clearInterval(upscalePollTimer);
  upscalePollTimer = setInterval(loadUpscaleQueue, 2000);
}

async function _checkUpscaleDisabled() {
  try {
    const r = await fetch("/api/upscale/settings");
    const d = await r.json();
    const disabled = !d.ok || (d.settings && d.settings.upscaling_mode === "disabled");
    const badge = document.getElementById("upscaleDisabledBadge");
    if (badge) badge.style.display = disabled ? "" : "none";
  } catch(e) {}
}

function closeUpscaleModal() {
  upscaleModalOpen = false;
  document.getElementById("upscaleOverlay").style.display = "none";
  if (upscalePollTimer) { clearInterval(upscalePollTimer); upscalePollTimer = null; }
}

// ── Filter ──────────────────────────────────────────────────────────
function setUpscaleFilter(f) {
  _upscaleFilter = f;
  document.querySelectorAll('[id^="uqfTab-"]').forEach(b => b.classList.remove("active"));
  const tab = document.getElementById("uqfTab-" + f);
  if (tab) tab.classList.add("active");
  loadUpscaleQueue();
}

// ── Load & Render ───────────────────────────────────────────────────
async function loadUpscaleQueue() {
  try {
    const [qr, pr] = await Promise.all([
      fetch("/api/upscale/queue"),
      fetch("/api/upscale/progress"),
    ]);
    const qd = await qr.json();
    const pd = await pr.json();
    if (!qd.ok) return;

    const items    = qd.items || [];
    const progress = pd.ok ? (pd.progress || {}) : {};
    _lastUpscaleProgress = progress;

    _updateUpscaleBadges(qd.badge || 0);
    _updateUqFilterCounts(items);

    if (!upscaleModalOpen) return;
    _renderUpscaleQueue(items, progress);
  } catch(e) { /* ignore */ }
}

function _updateUqFilterCounts(items) {
  const counts = {
    all:       items.length,
    active:    items.filter(i => i.status === "running").length,
    queued:    items.filter(i => i.status === "queued").length,
    completed: items.filter(i => i.status === "completed").length,
    failed:    items.filter(i => ["failed","cancelled"].includes(i.status)).length,
  };
  Object.keys(counts).forEach(k => {
    const el = document.getElementById("uqfCount-" + k);
    if (!el) return;
    el.textContent = counts[k] ? counts[k] : "";
    el.style.display = counts[k] ? "" : "none";
  });
}

function _updateUpscaleBadges(count) {
  ["upscaleBadge","mobileUpscaleBadge"].forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = count > 0 ? "" : "none";
    if (count > 0) el.textContent = count;
  });
  if (window.updateTotalQueueBadge) window.updateTotalQueueBadge();
}

// ── Helpers ─────────────────────────────────────────────────────────
function _uEsc(s) {
  return String(s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;")
                      .replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function _uFmtEta(sec) {
  if (!sec || sec <= 0) return "";
  sec = Math.round(sec);
  if (sec >= 3600) return Math.floor(sec/3600) + "h " + Math.floor((sec%3600)/60) + "m";
  if (sec >= 60)   return Math.floor(sec/60) + "m " + (sec%60) + "s";
  return sec + "s";
}
function _uFilename(p) {
  return (p||"").replace(/\\/g,"/").split("/").pop() || p;
}
// Series title: everything before " – " (or full string)
function _uSeriesTitle(title) {
  if (!title) return "";
  const idx = title.indexOf(" – ");
  return idx > 0 ? title.substring(0, idx) : title;
}
// Subtitle: everything after " – " (filename part)
function _uSubtitle(title) {
  if (!title) return "";
  const idx = title.indexOf(" – ");
  return idx > 0 ? title.substring(idx + 3) : "";
}

// ── Render ──────────────────────────────────────────────────────────
function _renderUpscaleQueue(items, progress) {
  const list = document.getElementById("upscaleQueueList");
  if (!list) return;

  let visible = items;
  if      (_upscaleFilter === "active")    visible = items.filter(i => i.status === "running");
  else if (_upscaleFilter === "queued")    visible = items.filter(i => i.status === "queued");
  else if (_upscaleFilter === "completed") visible = items.filter(i => i.status === "completed").reverse();
  else if (_upscaleFilter === "failed")    visible = items.filter(i => ["failed","cancelled"].includes(i.status)).reverse();
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
      active:    t("Kein Upscaling läuft gerade.", "No upscaling is currently running."),
      queued:    t("Keine wartenden Upscaling-Jobs.", "No pending upscaling jobs."),
      completed: t("Keine abgeschlossenen Upscaling-Jobs.", "No completed upscaling jobs."),
      failed:    t("Keine fehlgeschlagenen oder abgebrochenen Upscaling-Jobs.", "No failed or cancelled upscaling jobs."),
      all:       t("Upscaling-Warteschlange ist leer", "Upscaling queue is empty"),
    };
    list.innerHTML = '<div class="queue-empty">' + (_upscaleFilter in emptyMsgs ? emptyMsgs[_upscaleFilter] : emptyMsgs.all) + '</div>';
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

    // Status badge — same classes as normal queue
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

    // Title display: series name as main title, file as subtitle pill
    const seriesTitle = _uSeriesTitle(item.title) || _uFilename(item.file_path);
    const subtitlePart = _uSubtitle(item.title);

    // Progress
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
      // File label
      let fileLabel;
      if (totalFiles > 1) {
        fileLabel = (curFileIdx + (isRunning ? 1 : 0)) + "/" + totalFiles + " Dateien";
      } else {
        fileLabel = _uFilename(isRunning && progress.file ? progress.file : item.file_path);
      }

      // Pills for the active file bar
      let pillsHtml = "";
      if (isRunning && progress.active) {
        if (progress.speed)
          pillsHtml += '<span class="queue-meta-pill queue-progress-pill">⚡ ' + _uEsc(progress.speed) + '</span>';
        const etaStr = _uFmtEta(progress.eta_sec);
        if (etaStr)
          pillsHtml += '<span class="queue-meta-pill queue-progress-pill queue-progress-pill--eta">ETA ' + etaStr + '</span>';
        if (progress.time)
          pillsHtml += '<span class="queue-meta-pill queue-progress-pill">' + _uEsc(progress.time) + '</span>';
      }

      // Overall bar (always shown)
      progressHtml =
        '<div class="queue-progress">' +
        '<div class="queue-progress-bar"><div class="queue-progress-fill queue-progress-fill--upscaling" style="width:' + overallPct + '%"></div></div>' +
        '<div class="queue-progress-footer">' +
        '<span>' + _uEsc(fileLabel) + '</span>' +
        '<span class="queue-progress-pct">' + overallPct + '%</span>' +
        '</div>';

      // Per-file bar only when there are multiple files (or when running single)
      if (totalFiles > 1 && isRunning) {
        progressHtml +=
          '<div class="queue-progress-bar queue-progress-bar--episode">' +
          '<div class="queue-progress-fill queue-progress-fill--upscaling" style="width:' + filePct + '%"></div>' +
          '</div>' +
          '<div class="queue-progress-footer">' +
          '<span class="queue-progress-phase">' + t("✨ Datei", "✨ File") + '</span>' +
          '<span class="queue-progress-pct">' + filePct + '%</span>' +
          '</div>';
      } else if (isRunning && totalFiles === 1) {
        // Single file running: show second bar for file detail
        progressHtml +=
          '<div class="queue-progress-bar queue-progress-bar--episode">' +
          '<div class="queue-progress-fill queue-progress-fill--upscaling" style="width:' + filePct + '%"></div>' +
          '</div>' +
          '<div class="queue-progress-footer">' +
          '<span class="queue-progress-phase">✨ Upscaling</span>' +
          '<span class="queue-progress-pct">' + filePct + '%</span>' +
          '</div>';
      }

      if (pillsHtml)
        progressHtml += '<div class="queue-progress-pills">' + pillsHtml + '</div>';

      if (isFailed && item.error)
        progressHtml += '<div class="queue-errors">' + _uEsc(item.error) + '</div>';

      progressHtml += '</div>';
    }

    // Action buttons — same style as normal queue
    const queuedIdx = queued.indexOf(item);
    let actionBtn = "";
    if (isQueued) {
      const isFirst = queuedIdx === 0;
      const isLast  = queuedIdx === queued.length - 1;
      actionBtn =
        '<button class="queue-move" onclick="moveUpscaleItem(' + item.id + ',\'up\')"   title="'+ t("Nach oben", "Up") +'"  ' + (isFirst ? ' disabled' : '') + '>&#9650;</button>' +
        '<button class="queue-move" onclick="moveUpscaleItem(' + item.id + ',\'down\')" title="'+ t("Nach unten", "Down") +'" ' + (isLast  ? ' disabled' : '') + '>&#9660;</button>' +
        '<button class="queue-remove" onclick="removeUpscaleItem(' + item.id + ')" title="'+ t("Entfernen", "Remove") +'">&times;</button>';
    } else if (isRunning) {
      actionBtn = '<button class="queue-cancel" onclick="cancelUpscaleItem(' + item.id + ')" title="'+ t("Abbrechen", "Cancel") +'">'+ t("Abbrechen", "Cancel") +'</button>';
    } else if (isStopped) {
      actionBtn = '<button class="queue-remove" onclick="removeUpscaleItem(' + item.id + ')" title="Entfernen">&times;</button>';
    }

    // Meta pills
    let metaHtml = statusBadge;
    if (totalFiles > 1)
      metaHtml += '<span class="queue-meta-pill">' + totalFiles + ' '+ t("Dateien", "Files") +'</span>';
    if (subtitlePart)
      metaHtml += '<span class="queue-meta-pill">' + _uEsc(subtitlePart) + '</span>';

    html +=
      '<div class="' + cls + '" data-id="' + item.id + '">' +
      '<div class="queue-item-header">' +
      '<div class="queue-item-title">' + _uEsc(seriesTitle) + '</div>' +
      '<div class="queue-item-right">' + actionBtn + '</div>' +
      '</div>' +
      '<div class="queue-item-meta">' + metaHtml + '</div>' +
      progressHtml +
      '</div>';
  });

  list.innerHTML = html;
}

// ── Actions ─────────────────────────────────────────────────────────
async function cancelUpscaleItem(id) {
  try { await fetch("/api/upscale/queue/" + id + "/cancel", {method:"POST"}); loadUpscaleQueue(); }
  catch(e) {}
}
async function removeUpscaleItem(id) {
  try { await fetch("/api/upscale/queue/" + id, {method:"DELETE"}); loadUpscaleQueue(); }
  catch(e) {}
}
async function moveUpscaleItem(id, direction) {
  try {
    await fetch("/api/upscale/queue/" + id + "/move", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({direction: direction}),
    });
    loadUpscaleQueue();
  } catch(e) {}
}
async function clearUpscaleQueue() {
  try { await fetch("/api/upscale/queue/clear", {method:"POST"}); loadUpscaleQueue(); }
  catch(e) {}
}

// ── Background badge poll ────────────────────────────────────────────
function _startUpscaleBadgePoll() {
  setInterval(async () => {
    if (upscaleModalOpen) return;
    try {
      const d = await (await fetch("/api/upscale/badge")).json();
      if (d.ok) _updateUpscaleBadges(d.count || 0);
    } catch(e) {}
  }, 8000);
}

document.addEventListener("DOMContentLoaded", () => {
  _startUpscaleBadgePoll();
  fetch("/api/upscale/badge").then(r=>r.json()).then(d=>{
    if (d.ok) _updateUpscaleBadges(d.count||0);
  }).catch(()=>{});
});
