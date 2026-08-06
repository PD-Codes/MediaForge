// =====================================================================
// queue.js — the queue hub
// ---------------------------------------------------------------------
// ONE window (#queueOverlay in base.html) for all three queues. This file
// owns the whole thing: the shell, the merged data model and the single
// renderer. encoding_queue.js and upscale_queue.js only fetch their queue
// and hand the result to QHub.put() — they render nothing themselves.
//
// Layout of the window:
//   segment row   Everything | Downloads | Encoding | Upscaling
//   hero          the one job running right now, large
//   list          "Needs you" / "Up next" / "Finished today"
//
// Deliberately kept: every function name an inline onclick= refers to still
// exists (openQueueModal, setQueueFilter, moveQueueItem, …), and the three
// sidebar badges stay separate — window.updateTotalQueueBadge() reads their
// textContent, so merging them would silently produce 0.
// =====================================================================

let queueModalOpen = false;
let queuePollTimer = null;
let badgePollTimer = null;
let queueCustomPaths = [];
let _queueIsPaused = false;
let _queueFilter = "all";   // legacy status filter, see setQueueFilter()

(async function loadQueueCustomPaths() {
  try {
    const resp = await fetch("/api/custom-paths");
    const data = await resp.json();
    queueCustomPaths = data.paths || [];
  } catch (e) {
    /* ignore */
  }
})();

// =====================================================================
// Shell
// =====================================================================

let queueHubOpen = false;
window.qhubActivePane = "all";   // "all" | "downloads" | "encoding" | "upscaling"

const _QHUB_PANES = ["all", "downloads", "encoding", "upscaling"];

/** The merged model. Each queue writes its own slice; the renderer reads all. */
const _qhubModel = {
  downloads: { items: [], progress: {}, paused: false },
  encoding: { items: [], progress: {} },
  upscaling: { items: [], progress: {} },
};

function _qhubEl(id) { return document.getElementById(id); }

/** True while the hub is open and the given queue is part of the view. */
function qhubPaneActive(pane) {
  return queueHubOpen && (window.qhubActivePane === "all" || window.qhubActivePane === pane);
}

function openQueueHub(pane) {
  const overlay = _qhubEl("queueOverlay");
  if (!overlay) return;
  const wasOpen = queueHubOpen;
  queueHubOpen = true;
  // modals.css reveals overlays via [style*="block"] — never use flex here.
  overlay.style.display = "block";
  if (!wasOpen && window.MFScrollLock) window.MFScrollLock.lock();
  setQueuePane(pane || window.qhubActivePane);
}

function closeQueueHub() {
  const overlay = _qhubEl("queueOverlay");
  if (overlay) overlay.style.display = "none";
  if (queueHubOpen && window.MFScrollLock) window.MFScrollLock.unlock();
  queueHubOpen = false;
  queueModalOpen = false;
  _qhubStopTimers();
}

function _qhubStopTimers() {
  // mfPoll() returns a handle, not a numeric id — stop it through mfPollStop.
  window.mfPollStop(queuePollTimer); queuePollTimer = null;
  window.mfPollStop(window._qhubEncodingTimer); window._qhubEncodingTimer = null;
  window.mfPollStop(window._qhubUpscaleTimer); window._qhubUpscaleTimer = null;
}

/**
 * Switch the visible queue. Only what is on screen polls at 2s — on
 * "Everything" that is all three, on a single segment just that one. The
 * other queues keep their slower background badge poll.
 */
function setQueuePane(pane) {
  if (_QHUB_PANES.indexOf(pane) === -1) pane = "all";
  window.qhubActivePane = pane;

  _QHUB_PANES.forEach(function (p) {
    const tab = _qhubEl("qhubTab-" + p);
    if (!tab) return;
    const on = p === pane;
    tab.classList.toggle("active", on);
    tab.setAttribute("aria-selected", on ? "true" : "false");
  });

  // The two "this queue is switched off" badges only apply to their own view.
  ["encodingDisabledBadge", "upscaleDisabledBadge"].forEach(function (id) {
    const el = _qhubEl(id);
    if (el) el.style.display = "none";
  });

  _qhubStopTimers();
  queueModalOpen = pane === "all" || pane === "downloads";

  if (queueModalOpen) {
    loadQueue();
    queuePollTimer = window.mfPoll(loadQueue, 2000);
  }
  if (pane === "all" || pane === "encoding") {
    if (typeof _checkEncodingDisabled === "function") _checkEncodingDisabled();
    if (typeof loadEncodingQueue === "function") {
      loadEncodingQueue();
      window._qhubEncodingTimer = window.mfPoll(loadEncodingQueue, 2000);
    }
  }
  if (pane === "all" || pane === "upscaling") {
    if (typeof _checkUpscaleDisabled === "function") _checkUpscaleDisabled();
    if (typeof loadUpscaleQueue === "function") {
      loadUpscaleQueue();
      window._qhubUpscaleTimer = window.mfPoll(loadUpscaleQueue, 2000);
    }
  }
  renderQueueHub();
}

/** "Clear finished" acts on whatever the current view shows. */
function qhubClear() {
  const pane = window.qhubActivePane;
  if (pane === "all" || pane === "downloads") clearOldQueueItems();
  if ((pane === "all" || pane === "encoding") && typeof clearEncodingQueue === "function") clearEncodingQueue();
  if ((pane === "all" || pane === "upscaling") && typeof clearUpscaleQueue === "function") clearUpscaleQueue();
}

function qhubSetFacet(pane, n) {
  const el = _qhubEl("qhubFacet-" + pane);
  if (!el) return;
  if (n > 0) { el.textContent = n; el.hidden = false; }
  else { el.hidden = true; }
}
window.qhubSetFacet = qhubSetFacet;

// Legacy entry points — the sidebar, the mobile top bar and app.js's home
// strip call these by name.
function openQueueModal() { openQueueHub("downloads"); }
function closeQueueModal() { closeQueueHub(); }
function openUpscaleModal() { openQueueHub("upscaling"); }
function closeUpscaleModal() { closeQueueHub(); }
function openEncodingQueueModal() { openQueueHub("encoding"); }
function closeEncodingQueueModal() { closeQueueHub(); }

// Per-queue badge counts, summed into the single sidebar badge by
// updateTotalQueueBadge() further down. Seeded here because updateBadge() can
// fire before that assignment is reached.
window._qBadgeCounts = window._qBadgeCounts || { downloads: 0, encoding: 0, upscaling: 0 };

let lastFfmpegProgress = {};
// Last active ffmpeg snapshot, kept PER queue item. /api/queue carries one
// global ffmpeg_progress, but the hub normalises every running (and cancelling)
// download in the same pass — with a single shared snapshot whichever item came
// last overwrote it, and the hero's episode bar fell back to 0% on the next poll.
let _stickyProgressById = {};    // id -> { progress, url }

function formatBandwidth(bwStr) {
  if (!bwStr) return "";
  const trimmed = String(bwStr).trim();
  if (/B\/s$/i.test(trimmed)) return trimmed;
  const m = trimmed.match(/^\s*([\d.]+)\s*([kmg])?bits\/s\s*$/i);
  if (!m) return bwStr;
  const value = parseFloat(m[1]);
  if (Number.isNaN(value)) return bwStr;
  const unit = (m[2] || "").toLowerCase();
  let mbps = value;
  if (unit === "k") mbps = value / 1000;
  else if (unit === "g") mbps = value * 1000;
  const mbytes = (mbps / 8).toFixed(1);
  return (window.__LANG === "de" ? mbytes.replace(".", ",") : mbytes) + " MB/s";
}

// =====================================================================
// Shared helpers (window.QHub) — also the door the other two queues use
// =====================================================================

window.QHub = (function () {

  const esc = window.mfEscape;  // shared, quote-safe (static/mf_escape.js)

  function num(n) {
    // German writes 8,4 — the queue shows a lot of these
    return (window.__LANG === "de") ? String(n).replace(".", ",") : String(n);
  }

  function pct(p) {
    const v = Math.max(0, Math.min(100, Math.round(p || 0)));
    return window.__LANG === "de" ? v + " %" : v + "%";
  }

  function fmtEta(sec) {
    sec = Math.round(sec || 0);
    if (sec <= 0) return "";
    if (sec >= 3600) return Math.floor(sec / 3600) + "h " + Math.floor((sec % 3600) / 60) + "m";
    if (sec >= 60) return Math.floor(sec / 60) + " min";
    return sec + "s";
  }

  function fmtMb(mb) {
    mb = mb || 0;
    if (mb >= 1024) return num((mb / 1024).toFixed(1)) + " GB";
    return num(Math.round(mb)) + " MB";
  }

  /** completed_at comes from SQLite as UTC "YYYY-MM-DD HH:MM:SS". */
  function ts(sqlTs) {
    if (!sqlTs) return null;
    const d = new Date(String(sqlTs).replace(" ", "T") + "Z");
    return isNaN(d.getTime()) ? null : d;
  }

  function isToday(sqlTs) {
    const d = ts(sqlTs);
    if (!d) return false;
    const now = new Date();
    return d.getFullYear() === now.getFullYear()
      && d.getMonth() === now.getMonth()
      && d.getDate() === now.getDate();
  }

  function hhmm(sqlTs) {
    const d = ts(sqlTs);
    if (!d) return "";
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  /** S02E11 out of a source URL, or "" when it isn't an episode URL. */
  function episodeCode(url) {
    if (!url) return "";
    const m = url.match(/staffel-(\d+)\/episode-(\d+)/i);
    if (m) return "S" + String(m[1]).padStart(2, "0") + "E" + String(m[2]).padStart(2, "0");
    const f = url.match(/filme\/film-(\d+)/i);
    if (f) return t("Film", "Movie");
    return "";
  }

  /** S01E19 out of a file name — that is all the other two queues have. */
  function episodeFromFile(path) {
    const name = String(path || "").replace(/\\/g, "/").split("/").pop() || "";
    const m = name.match(/S(\d{1,3})[ ._-]?E(\d{1,4})/i);
    if (m) return "S" + String(m[1]).padStart(2, "0") + "E" + String(m[2]).padStart(2, "0");
    return "";
  }

  /** "AniWorld · VOE" — where it comes from and who hosts it. */
  function sourceLabel(seriesUrl) {
    const u = String(seriesUrl || "").toLowerCase();
    if (u.includes("aniworld.to")) return "AniWorld";
    if (u.includes("filmpalast.to")) return "FilmPalast";
    if (u.includes("megakino")) return "MegaKino";
    if (u.includes("hanime")) return "hanime";
    if (u.includes("s.to") || u.includes("serienstream")) return "SerienStream";
    return "";
  }

  /** The station rail under the hero bar. */
  function stations(steps) {
    let html = '<div class="mf-progress qhub-stations">';
    steps.forEach(function (s) {
      const cls = s.state === "done" ? " is-done" : (s.state === "active" ? " is-active" : "");
      html += '<div class="mf-progress-step' + cls + '">'
        + '<span class="mf-progress-bar"></span>'
        + '<span class="mf-progress-label">' + esc(s.label) + '</span>'
        + '</div>';
    });
    return html + '</div>';
  }

  /** Called by encoding_queue.js / upscale_queue.js after each fetch. */
  function put(queue, slice) {
    if (!_qhubModel[queue]) return;
    Object.assign(_qhubModel[queue], slice);
    renderQueueHub();
  }

  return {
    esc: esc, num: num, pct: pct, fmtEta: fmtEta, fmtMb: fmtMb,
    isToday: isToday, hhmm: hhmm, episodeCode: episodeCode,
    episodeFromFile: episodeFromFile, sourceLabel: sourceLabel,
    stations: stations, put: put, model: _qhubModel,
  };
})();

// =====================================================================
// Downloads: fetching
// =====================================================================

async function loadQueue() {
  try {
    const resp = await fetch("/api/queue");
    const data = await resp.json();
    const items = data.items || [];
    lastFfmpegProgress = data.ffmpeg_progress || {};
    _queueIsPaused = !!data.paused;
    _qhubModel.downloads.items = items;
    _qhubModel.downloads.progress = lastFfmpegProgress;
    _qhubModel.downloads.paused = _queueIsPaused;
    _qPruneSticky(items);
    updateBadge(items);
    qhubSetFacet("downloads", items.filter(i => i.status === "running" || i.status === "queued").length);
    renderQueueHub();
    // The home page shows one strip about the running download; it has no
    // poller of its own on purpose (see renderHomeRunStrip in app.js).
    if (typeof window.renderHomeRunStrip === "function") {
      window.renderHomeRunStrip(items, data.ffmpeg_progress, _queueIsPaused);
    }
  } catch (e) {
    /* ignore */
  }
}

/**
 * Legacy status filter. The hub groups by what an entry needs from you
 * instead ("needs you" / "up next" / "finished today"), so there is no
 * filter row any more — the name stays because inline handlers and older
 * third-party pages call it.
 */
function setQueueFilter(filter) {
  _queueFilter = filter;
  loadQueue();
}
function setEncodingFilter(f) { if (typeof loadEncodingQueue === "function") loadEncodingQueue(); }
function setUpscaleFilter(f) { if (typeof loadUpscaleQueue === "function") loadUpscaleQueue(); }

async function toggleQueuePause() {
  const wasPaused = _queueIsPaused;
  try {
    await fetch(wasPaused ? "/api/queue/resume" : "/api/queue/pause", { method: "POST" });
    await loadQueue();
    if (window.showToast) {
      showToast(wasPaused
        ? t("Downloads werden fortgesetzt.", "Downloads are being resumed")
        : t("Downloads pausiert.", "Downloads paused"));
    }
  } catch (e) {
    if (window.showToast) showToast(t("Fehler: " + e.message, "Error: " + e.message));
  }
}

async function clearOldQueueItems() {
  try {
    await fetch("/api/queue/completed", { method: "DELETE" });
    await loadQueue();
    if (window.showToast) showToast(t("Alte Einträge gelöscht", "Old entries deleted"));
  } catch (e) {
    if (window.showToast) showToast(t("Fehler: " + e.message, "Error: " + e.message));
  }
}

function updateBadge(items) {
  const activeItems = items.filter(i => i.status === "queued" || i.status === "running");
  applyDownloadBadge(activeItems.length, activeItems.map(i => i.series_url).filter(Boolean));
}

// Split out of updateBadge() so the lightweight badge poll can feed the same
// UI without fetching the whole queue payload.
function applyDownloadBadge(active, urls) {
  // Running status on browse cards — download items only, never the other
  // two queues.
  if (window.updateRunningCards) {
    window.updateRunningCards(urls || []);
  }

  window._qBadgeCounts.downloads = active;
  // #queueBadge only exists on pages/modules that still render their own
  // download badge; the sidebar shows the summed one.
  ["queueBadge", "mobileQueueBadge"].forEach(function (id) {
    const badge = document.getElementById(id);
    if (!badge) return;
    badge.textContent = active;
    badge.style.display = active > 0 ? "inline-block" : "none";
  });
  if (window.updateTotalQueueBadge) window.updateTotalQueueBadge();
}

// =====================================================================
// One normalised entry per queue row, so the renderer stays queue-agnostic
// =====================================================================

/**
 * Resolve the per-episode progress of the running download. Between phases
 * and between episodes the last active snapshot is held, so the bar never
 * collapses to 0% and causes a layout jump.
 */
function _qEpisodeProgress(item) {
  const currentUrl = item.current_url || "";
  const slot = _stickyProgressById[item.id] || { progress: {}, url: "" };
  if (lastFfmpegProgress.active && lastFfmpegProgress.percent > 0) {
    slot.progress = Object.assign({}, lastFfmpegProgress);
    slot.url = currentUrl;
  } else if (_queueIsPaused && !lastFfmpegProgress.active) {
    slot.progress = {};
    slot.url = "";
  } else if (currentUrl && currentUrl !== slot.url) {
    // The item moved on to the next episode — the old snapshot is stale.
    slot.progress = {};
    slot.url = currentUrl;
  }
  _stickyProgressById[item.id] = slot;
  return (lastFfmpegProgress.active && lastFfmpegProgress.percent > 0)
    ? lastFfmpegProgress
    : (slot.progress.active !== undefined ? slot.progress : lastFfmpegProgress);
}

/** Forget snapshots of items that are no longer in the queue. */
function _qPruneSticky(items) {
  const alive = {};
  items.forEach(function (i) { alive[i.id] = true; });
  Object.keys(_stickyProgressById).forEach(function (id) {
    if (!alive[id]) delete _stickyProgressById[id];
  });
}

const _QHUB_PHASES = ["download", "ffmpeg", "upscaling", "move"];

/** Normalise a download queue row. */
function _qNormDownload(item) {
  const Q = window.QHub;
  const running = item.status === "running";
  const cancelling = item.status === "cancelled" && !!item.current_url;
  const attn = running && !!item.captcha_url;
  const fp = (running || cancelling) ? _qEpisodeProgress(item) : {};
  const phase = fp.phase || "download";
  // A single-file download -- a movie, or one hand-picked episode -- has
  // nothing to aggregate: "episode 1 of 1" and "this file" are the same
  // number, so the hero card drew the identical percentage twice. Below,
  // such an item gets ONE bar (the phase that is actually moving) and the
  // station rail keeps naming the phase.
  const singleEpisode = (item.total_episodes || 0) <= 1;

  let state = item.status;
  if (cancelling) state = "cancelling";

  const epPct = item.total_episodes > 0 ? (item.current_episode / item.total_episodes) * 100 : 0;
  let ffPct = 0;
  if (running && lastFfmpegProgress.active && item.total_episodes > 0) {
    // During ffmpeg/upscale/move the episode's download is already done, so
    // its full weight counts — otherwise the overall bar jumps backwards.
    const inEp = (phase === "ffmpeg" || phase === "move" || phase === "upscaling")
      ? 100 : (lastFfmpegProgress.percent || 0);
    ffPct = inEp / item.total_episodes;
  }

  let statusText;
  if (attn) statusText = "CAPTCHA";
  else if (running) statusText = t("LÄUFT", "RUNNING");
  else if (item.status === "queued") statusText = _queueIsPaused ? t("PAUSIERT", "PAUSED") : t("WARTET", "QUEUED");
  else if (cancelling) statusText = t("BRICHT AB", "CANCELLING");
  else if (item.status === "completed") statusText = t("FERTIG", "DONE");
  else if (item.status === "partial") statusText = t("TEILWEISE", "PARTIAL");
  else if (item.status === "failed") statusText = t("FEHLER", "FAILED");
  else statusText = t("ABGEBROCHEN", "CANCELLED");

  // The SECOND bar in the hero: how far the CURRENT episode is through its
  // current phase. That is a different number from the overall bar — during
  // the ffmpeg/upscale/move phase the episode's download already counts as
  // finished in the overall figure while this one starts over at 0.
  const epLabel = phase === "move" ? t("📦 Verschieben", "📦 Moving")
    : phase === "upscaling" ? t("✨ Upscaling", "✨ Upscaling")
      : phase === "ffmpeg" ? t("⚙ FFmpeg", "⚙ FFmpeg")
        : t("⬇ Download", "⬇ Download");

  const src = Q.sourceLabel(item.series_url);
  return {
    queue: "downloads",
    epPct: (!singleEpisode && (running || cancelling)) ? (fp.percent || 0) : null,
    epLabel: epLabel,
    id: item.id,
    raw: item,
    title: item.title,
    episode: Q.episodeCode(item.current_url) || (item.total_episodes > 1 ? item.total_episodes + " Ep." : ""),
    chip: [src, item.provider].filter(Boolean).join(" · "),
    language: item.language_label || item.language || "",
    poster: item.poster || "",
    station: attn ? "attn" : (running || cancelling ? _QHUB_PHASES.indexOf(phase) : 0),
    state: state,
    attn: attn,
    running: running || cancelling,
    pct: singleEpisode
      ? ((running || cancelling) ? (fp.percent || 0) : epPct)
      : Math.min(epPct + ffPct, 100),
    statusText: statusText,
    completed_at: item.completed_at,
    sync: (item.source || "").startsWith("sync"),
    fp: fp,
    phase: phase,
  };
}

/**
 * Which row of `queue` is actually being worked on right now.
 *
 * Normally the answer is "the one whose status is running", and the DB says so.
 * But the status column and the live ffmpeg progress are written by different
 * code at different moments, and between claiming a job and committing its
 * status there is a window where the encoder/upscaler is demonstrably busy
 * (progress.active) while every row still reads "queued".
 *
 * That window is what produced the reported bug: on the "Upscaling" pane the
 * job became the hero card (the pane's only runner, so the hero picked it) and
 * looked active, while on "Everything" the hero slot was taken by a running
 * download and the same job fell through to "Up next" — the same job described
 * two different ways depending on which tab you were looking at.
 *
 * So when the queue reports active work and no row admits to running, the first
 * queued row is treated as the running one. Returns null when the DB already
 * has an answer, or when nothing is active.
 */
function _qInferredRunningId(queue) {
  const model = _qhubModel[queue];
  if (!model) { return null; }
  const progress = model.progress || {};
  if (!progress.active) { return null; }
  const items = model.items || [];
  if (items.some(i => i.status === "running")) { return null; }
  const first = items.filter(i => i.status === "queued")[0];
  return first ? first.id : null;
}

/** Normalise an encoding / upscaling queue row (they have the same shape). */
function _qNormJob(item, queue) {
  const Q = window.QHub;
  const progress = _qhubModel[queue].progress || {};
  const running = item.status === "running" || item.id === _qInferredRunningId(queue);
  const totalFiles = item.total_files || 1;
  const curIdx = item.current_file_idx || 0;
  const filePct = running && progress.active ? (progress.percent || 0) : 0;
  const pct = item.status === "completed" ? 100
    : running ? Math.min(curIdx / totalFiles * 100 + filePct / totalFiles, 99)
      : (item.progress_pct || 0);

  const isEnc = queue === "encoding";
  const word = isEnc ? t("ENCODING", "ENCODING") : t("UPSCALING", "UPSCALING");
  let statusText;
  if (running) statusText = filePct > 0 ? word + " " + Q.pct(filePct) : word;
  else if (item.status === "queued") statusText = t("WARTET", "QUEUED");
  else if (item.status === "completed") statusText = t("FERTIG", "DONE");
  else if (item.status === "failed") statusText = t("FEHLER", "FAILED");
  else statusText = t("ABGEBROCHEN", "CANCELLED");

  // "Series – file.mkv" is what both queues store as the title
  const dash = String(item.title || "").indexOf(" – ");
  const seriesTitle = dash > 0 ? item.title.substring(0, dash) : (item.title || "");
  const fileName = running && progress.file ? progress.file : item.file_path;

  const epLabel = totalFiles > 1
    ? (isEnc ? t("🎞 Datei ", "🎞 File ") : t("✨ Datei ", "✨ File ")) + (curIdx + 1) + "/" + totalFiles
    : (isEnc ? t("🎞 Diese Datei", "🎞 This file") : t("✨ Diese Datei", "✨ This file"));

  return {
    queue: queue,
    id: item.id,
    raw: item,
    // Same reasoning as _qNormDownload(): with one file in the job the
    // overall bar and the per-file bar are the same number.
    epPct: (running && totalFiles > 1) ? filePct : null,
    epLabel: epLabel,
    title: seriesTitle || String(fileName || "").split("/").pop(),
    episode: Q.episodeFromFile(item.title) || Q.episodeFromFile(fileName)
      || (totalFiles > 1 ? totalFiles + " " + t("Dateien", "files") : ""),
    chip: "",
    language: "",
    poster: "",
    station: isEnc ? 1 : 2,
    state: item.status,
    attn: false,
    running: running,
    pct: pct,
    statusText: statusText,
    completed_at: item.completed_at,
    sync: false,
    fileName: fileName,
    filePct: filePct,
    totalFiles: totalFiles,
    curIdx: curIdx,
  };
}

/** Everything the current segment should show, already normalised. */
function _qhubEntries() {
  const pane = window.qhubActivePane;
  let out = [];
  if (pane === "all" || pane === "downloads") {
    out = out.concat(_qhubModel.downloads.items.map(_qNormDownload));
  }
  if (pane === "all" || pane === "encoding") {
    out = out.concat(_qhubModel.encoding.items.map(i => _qNormJob(i, "encoding")));
  }
  if (pane === "all" || pane === "upscaling") {
    out = out.concat(_qhubModel.upscaling.items.map(i => _qNormJob(i, "upscaling")));
  }
  return out;
}

// =====================================================================
// Rendering
// =====================================================================

/** Action buttons of a row — same handler names as before. */
function _qhubActions(e) {
  const Q = window.QHub;
  const q = e.queue;
  const mv = q === "downloads" ? "moveQueueItem" : (q === "encoding" ? "moveEncodingItem" : "moveUpscaleItem");
  const rm = q === "downloads" ? "removeQueueItem" : (q === "encoding" ? "removeEncodingItem" : "removeUpscaleItem");
  const cn = q === "downloads" ? "cancelQueueItem" : (q === "encoding" ? "cancelEncodingItem" : "cancelUpscaleItem");

  if (e.state === "queued") {
    return '<button class="queue-move" onclick="' + mv + '(' + e.id + ',\'up\')" title="' + t("Nach oben", "Up") + '">&#9650;</button>'
      + '<button class="queue-move" onclick="' + mv + '(' + e.id + ',\'down\')" title="' + t("Nach unten", "Down") + '">&#9660;</button>'
      + '<button class="queue-remove" onclick="' + rm + '(' + e.id + ')" title="' + t("Entfernen", "Remove") + '">&times;</button>';
  }
  if (e.running) {
    const captcha = e.attn
      ? '<button class="queue-captcha-btn" onclick="openCaptchaModal(' + e.id + ')" title="' + t("Captcha lösen", "Solve captcha") + '">&#128274;</button>'
      : '';
    // An item that is already tearing down cannot be cancelled twice — same as
    // the hero card, the button says so instead of inviting another click.
    if (e.state === "cancelling") {
      return '<button class="queue-cancel" disabled title="' + t("Bricht ab…", "Cancelling…") + '">'
        + t("Bricht ab…", "Cancelling…") + '</button>';
    }
    return captcha + '<button class="queue-cancel" onclick="' + cn + '(' + e.id + ')" title="' + t("Abbrechen", "Cancel") + '">' + t("Abbrechen", "Cancel") + '</button>';
  }
  if (q === "downloads" && (e.state === "failed" || e.state === "cancelled")) {
    let errCount = 0;
    try {
      const errs = typeof e.raw.errors === "string" ? JSON.parse(e.raw.errors || "[]") : (e.raw.errors || []);
      errCount = errs.length;
    } catch (err) { }
    return '<button class="queue-restart" onclick="restartQueueItem(' + e.id + ')" title="'
      + (errCount > 0 ? errCount + t(" fehlerhafte Episoden neu starten", " failed episodes will be restarted")
        : t("Alle Episoden neu starten", "Restart all episodes"))
      + '">&#8635;' + (errCount > 0 ? " " + errCount : "") + '</button>'
      + '<button class="queue-remove" onclick="' + rm + '(' + e.id + ')" title="' + t("Entfernen", "Remove") + '">&times;</button>';
  }
  return '<button class="queue-remove" onclick="' + rm + '(' + e.id + ')" title="' + t("Entfernen", "Remove") + '">&times;</button>';
}

// ---------------------------------------------------------------------
// Failed rows: click to reveal the error
// ---------------------------------------------------------------------
// The pre-hub modals showed this. Downloads had a click-to-expand list,
// encoding/upscaling printed a permanent one-liner. Both are restored here as
// one disclosure so a long ffmpeg message cannot push the list apart.

// Which panels the user opened, as "<queue>:<id>". renderQueueHub() rebuilds
// the whole list every 2s, so without this the panel would snap shut mid-read.
const _qhubOpenErrors = new Set();

/** Normalise both payload shapes into [{label, text}]. */
function _qhubErrorList(e) {
  const raw = e.raw || {};
  const out = [];

  // Downloads: `errors` is a JSON array of {url, error} -- one entry per
  // episode that failed, so a 12-episode season can carry 12 of them.
  if (e.queue === "downloads") {
    let errs = [];
    try {
      errs = typeof raw.errors === "string" ? JSON.parse(raw.errors || "[]") : (raw.errors || []);
    } catch (err) { errs = []; }
    if (Array.isArray(errs)) {
      errs.forEach(function (err) {
        if (!err) return;
        const text = typeof err === "string" ? err : (err.error || "");
        if (!text) return;
        const ep = (err && err.url) ? parseSeasonEpisode(err.url) : "";
        out.push({ label: ep || "", text: text });
      });
    }
    return out;
  }

  // Encoding / upscaling: a single plain string.
  if (raw.error) out.push({ label: "", text: String(raw.error) });
  return out;
}

/**
 * Toggle a row's error panel.
 * Bails on clicks that land on an action button, so Cancel/Remove/Restart keep
 * working -- cheaper and less brittle than adding stopPropagation() to each of
 * the eight buttons _qhubActions() can emit.
 */
window.qhubToggleError = function (ev, rowEl) {
  if (ev && ev.target && ev.target.closest && ev.target.closest(".qhub-row-actions")) return;
  const key = rowEl.getAttribute("data-errkey");
  if (!key) return;
  const panel = document.getElementById("qhuberr-" + key);
  if (!panel) return;
  const open = panel.classList.toggle("is-open");
  rowEl.classList.toggle("is-err-open", open);
  rowEl.setAttribute("aria-expanded", open ? "true" : "false");
  if (open) _qhubOpenErrors.add(key); else _qhubOpenErrors.delete(key);
};

// The row carries role="button"/tabindex="0", so it has to answer the keyboard
// too. Delegated once instead of an inline onkeydown per row -- the list is
// rebuilt every 2s and inline handlers would be re-parsed each time.
document.addEventListener("keydown", function (ev) {
  if (ev.key !== "Enter" && ev.key !== " " && ev.key !== "Spacebar") return;
  const row = ev.target && ev.target.closest ? ev.target.closest(".qhub-row--haserr") : null;
  if (!row) return;
  ev.preventDefault();
  window.qhubToggleError(ev, row);
});

// Cause id -> [German, English] label and advice. The server (web/error_explain.py)
// classifies the raw error and sends back a cause id; the wording lives here
// because that is where the t(de, en) helper this file already uses lives.
//
// The point of the whole thing: a queue entry has to answer "is this my fault
// and what do I do now?", and a traceback answers neither. The raw text is
// still shown underneath -- it is what a bug report needs.
const _QHUB_CAUSES = {
  disk_full:        [["Kein Speicherplatz mehr", "No space left"], ["Platz auf dem Ziellaufwerk schaffen und neu starten.", "Free space on the target drive, then retry."]],
  path_missing:     [["Zielordner nicht gefunden", "Target folder missing"], ["Den Custom Path in den Einstellungen prüfen — er wurde verschoben oder gelöscht.", "Check the custom path in Settings — it was moved or deleted."]],
  permission:       [["Keine Schreibrechte", "Permission denied"], ["MediaForge darf nicht in den Zielordner schreiben. Rechte prüfen (bei Docker: Volume-Mapping und Benutzer-ID).", "MediaForge cannot write to the target folder. Check the permissions (with Docker: the volume mapping and user id)."]],
  file_in_use:      [["Datei ist in Benutzung", "File is in use"], ["Ein anderes Programm hält die Datei offen. Player oder Virenscanner schließen und erneut versuchen.", "Another program is holding the file open. Close the player or scanner and retry."]],
  captcha:          [["Captcha nicht gelöst", "Captcha not solved"], ["Die Seite hat eine Bot-Prüfung gezeigt. Erneut versuchen — bei wiederholtem Auftreten das manuelle Lösen in den Einstellungen aktivieren.", "The site showed a bot check. Retry — if it keeps happening, enable manual solving in the settings."]],
  rate_limited:     [["Zu viele Anfragen", "Rate limited"], ["Die Quelle drosselt. Etwas warten und dann erneut versuchen, oder weniger parallele Downloads einstellen.", "The source is throttling. Wait a while and retry, or lower the number of parallel downloads."]],
  blocked:          [["Zugriff verweigert", "Access denied"], ["Die Quelle blockt diese IP oder verlangt eine Anmeldung. Ein anderer Anbieter oder ein VPN hilft oft.", "The source is blocking this IP or wants a login. Another provider or a VPN often helps."]],
  hoster_dead:      [["Hoster liefert kein Video", "Hoster has no video"], ["Dieser Hoster ist tot oder die Datei wurde gelöscht. Erneut versuchen greift auf den nächsten Hoster zurück.", "This hoster is dead or the file was removed. Retrying falls back to the next hoster."]],
  episode_missing:  [["Episode nicht gefunden", "Episode not found"], ["Die Episode gibt es auf der Quellseite (noch) nicht.", "The episode does not exist on the source site (yet)."]],
  language_missing: [["Sprache nicht verfügbar", "Language unavailable"], ["Diese Episode gibt es in der gewählten Sprache nicht. Eine Sprachkette unter Regeln & Sprachen fängt das ab.", "This episode is not available in the chosen language. A language chain under Rules & Languages covers this."]],
  provider_layout:  [["Quellseite hat sich geändert", "Source site changed"], ["Die Seite antwortet anders als erwartet — das ist ein Fall für ein Update oder einen Fehlerbericht.", "The site responded differently than expected — this is a case for an update or a bug report."]],
  dns:              [["Adresse nicht auflösbar", "Name resolution failed"], ["DNS antwortet nicht. Unter Netzwerk & Zugriff einen anderen DNS-Server wählen.", "DNS is not answering. Pick a different DNS server under Network & Access."]],
  tls:              [["TLS-Fehler", "TLS error"], ["Das Zertifikat wurde abgelehnt. Häufig eine falsche Systemzeit oder ein aufbrechender Proxy.", "The certificate was rejected. Usually a wrong system clock or a TLS-inspecting proxy."]],
  timeout:          [["Zeitüberschreitung", "Timed out"], ["Die Quelle war zu langsam oder nicht erreichbar. Erneut versuchen.", "The source was too slow or unreachable. Retry."]],
  connection:       [["Verbindung abgebrochen", "Connection lost"], ["Netzwerkproblem zwischen dir und der Quelle. Erneut versuchen.", "A network problem between you and the source. Retry."]],
  watchdog_hang:    [["Download hing fest", "Download hung"], ["Der Watchdog hat einen hängenden Download beendet. Erneut versuchen.", "The watchdog stopped a hung download. Retry."]],
  stalled:          [["Kein Fortschritt mehr", "Stalled"], ["Der Download stand zu lange still und wurde beendet. Erneut versuchen.", "The download stood still too long and was stopped. Retry."]],
  cancelled:        [["Abgebrochen", "Cancelled"], ["Von Hand oder durch eine Regel abgebrochen.", "Cancelled by hand or by a rule."]],
  ffmpeg_missing:   [["ffmpeg-Problem", "ffmpeg problem"], ["ffmpeg fehlt oder ist kaputt. Unter Einstellungen neu herunterladen lassen.", "ffmpeg is missing or broken. Let the settings page download it again."]],
  server_error:     [["Serverfehler bei der Quelle", "Source server error"], ["Das Problem liegt bei der Quelle. Später erneut versuchen.", "The problem is on the source's side. Try again later."]],
  not_found:        [["Nicht gefunden (404)", "Not found (404)"], ["Die Adresse gibt es nicht mehr.", "The address no longer exists."]],
  unknown:          [["Unbekannter Fehler", "Unknown error"], ["Kein bekanntes Muster erkannt — der Originaltext steht unten und gehört in einen Fehlerbericht.", "No known pattern matched — the original text is below and belongs in a bug report."]]
};

function _qhubCauseHeader(summary) {
  try {
    return _qhubCauseHeaderInner(summary);
  } catch (e) {
    // The explanation is a nicety bolted onto the error panel. It must never
    // be the reason the queue window fails to render -- that would turn "one
    // download failed" into "the queue is broken".
    return "";
  }
}

function _qhubCauseHeaderInner(summary) {
  if (!summary || !summary.causes || !summary.causes.length) return "";
  const Q = window.QHub;
  return summary.causes.map(function (c) {
    // Strip the "err_"/"fix_" prefixes the server sends: the cause id is the
    // key here, the i18n keys are the server's business.
    const entry = _QHUB_CAUSES[c.cause] || _QHUB_CAUSES.unknown;
    return '<div class="qhub-err-cause qhub-err-sev-' + Q.esc(c.severity) + '">'
      + '<div class="qhub-err-cause-head">'
      + '<strong>' + Q.esc(t(entry[0][0], entry[0][1])) + '</strong>'
      + (c.count > 1 ? '<span class="qhub-err-count">' + Q.esc(c.count) + '&times;</span>' : '')
      + '</div>'
      + '<div class="qhub-err-cause-fix">' + Q.esc(t(entry[1][0], entry[1][1])) + '</div>'
      + '</div>';
  }).join("");
}

/** The sibling panel. Sibling, not child: .qhub-row is a horizontal flex box. */
function _qhubErrorPanel(e, errors, key) {
  const Q = window.QHub;
  let body = _qhubCauseHeader((e.raw || {}).error_summary);
  body += errors.length > 1
    ? '<div class="qhub-err-head">' + errors.length + " " + t("Fehler", "errors") + '</div>'
    : '';
  errors.forEach(function (err) {
    body += '<div class="qhub-err-line">'
      + (err.label ? '<span class="qhub-err-ep">' + Q.esc(err.label) + '</span>' : '')
      + '<span class="qhub-err-text">' + Q.esc(err.text) + '</span>'
      + '</div>';
  });
  const open = _qhubOpenErrors.has(key) ? " is-open" : "";
  return '<div class="qhub-err-panel' + open + '" id="qhuberr-' + Q.esc(key) + '">' + body + '</div>';
}

/** One slim list row. `index` renders the position number when given. */
function _qhubRow(e, index) {
  const Q = window.QHub;
  const kind = e.attn ? "attn"
    : e.state === "completed" ? "done"
      : (e.state === "failed" || e.state === "cancelled") ? "failed"
        : e.state === "partial" ? "partial"
          : e.queue === "encoding" ? "encoding"
            : e.queue === "upscaling" ? "upscaling"
              : "download";

  const right = e.state === "completed" && e.completed_at
    ? '<span class="qhub-row-time">' + Q.esc(Q.hhmm(e.completed_at)) + '</span>'
    : '<span class="qhub-row-state">' + Q.esc(e.statusText) + '</span>';

  // Gate on the error actually being there, not on state === "failed": a
  // partially failed encode/upscale finishes as "completed" but still carries
  // a message, and those were invisible in the old UI too.
  const errors = e.running ? [] : _qhubErrorList(e);
  const hasErr = errors.length > 0;
  const errKey = e.queue + ":" + e.id;
  const isOpen = hasErr && _qhubOpenErrors.has(errKey);

  // The chevron takes the index slot rather than sitting in front of the
  // title: the slot is a fixed 14px that failed rows leave empty anyway, so
  // the titles stay on one vertical line instead of each error row shunting
  // its own title to the right. The count lives in the tooltip and in the
  // panel header.
  const errArrow = '<svg class="qhub-err-arrow" viewBox="0 0 24 24"><path d="M9 18l6-6-6-6"/></svg>';
  const errTitle = errors.length > 1
    ? errors.length + " " + t("Fehler — zum Anzeigen klicken", "errors — click to show")
    : t("Fehler — zum Anzeigen klicken", "Error — click to show");

  const rowCls = 'queue-item qhub-row qhub-row--' + kind
    + (hasErr ? ' qhub-row--haserr' : '')
    + (isOpen ? ' is-err-open' : '');
  const errAttrs = hasErr
    ? ' data-errkey="' + Q.esc(errKey) + '" onclick="qhubToggleError(event,this)"'
      + ' role="button" tabindex="0" aria-expanded="' + (isOpen ? 'true' : 'false') + '"'
      + ' title="' + Q.esc(errTitle) + '"'
    : '';

  const row = '<div class="' + rowCls + '"' + errAttrs
    + ' data-id="' + Q.esc(e.id) + '">'
    + '<span class="qhub-row-idx">' + (index ? index : (hasErr ? errArrow : '')) + '</span>'
    + '<span class="qhub-row-title">'
    + (e.sync ? '<span class="queue-sync-badge">' + t("Auto&#8209;Sync", "Auto-Sync") + '</span> ' : '')
    + Q.esc(e.title) + '</span>'
    + '<span class="qhub-row-right">'
    + (e.episode ? '<span class="qhub-row-ep">' + Q.esc(e.episode) + '</span>' : '')
    + right
    + '<span class="qhub-row-actions queue-item-right">' + _qhubActions(e) + '</span>'
    + '</span>'
    + '</div>';

  return hasErr ? row + _qhubErrorPanel(e, errors, errKey) : row;
}

function _qhubGroup(label, count) {
  return '<div class="qhub-group">' + window.QHub.esc(label)
    + '<span class="qhub-group-count">' + count + '</span></div>';
}

/** The meta line under the hero title: what it is doing, how fast, how long. */
function _qhubHeroMeta(e) {
  const Q = window.QHub;
  const parts = [];
  if (e.queue === "downloads") {
    const fp = e.fp || {};
    const verb = e.phase === "ffmpeg" ? t("Kodiert", "Encoding")
      : e.phase === "upscaling" ? t("Skaliert hoch", "Upscaling")
        : e.phase === "move" ? t("Verschiebt", "Moving")
          : t("Lädt", "Downloading");
    parts.push(verb);
    const speed = e.phase === "download" || e.phase === "move"
      ? formatBandwidth(fp.bandwidth || "")
      : (fp.speed || (fp.fps ? fp.fps + " fps" : ""));
    if (speed) parts.push(speed);
    const eta = Q.fmtEta(fp.eta_sec);
    if (eta) parts.push(t("noch ", "") + eta + t("", " left"));
    if (fp.downloaded_mb > 0) {
      parts.push(fp.total_mb > 0
        ? Q.fmtMb(fp.downloaded_mb) + t(" von ", " of ") + Q.fmtMb(fp.total_mb)
        : Q.fmtMb(fp.downloaded_mb));
    }
    if (e.language) parts.push(e.language);
  } else {
    const pr = _qhubModel[e.queue].progress || {};
    parts.push(e.queue === "encoding" ? t("Kodiert", "Encoding") : t("Skaliert hoch", "Upscaling"));
    if (pr.speed) parts.push(pr.speed);
    const eta = Q.fmtEta(pr.eta_sec);
    if (eta) parts.push(t("noch ", "") + eta + t("", " left"));
    if (e.totalFiles > 1) parts.push((e.curIdx + 1) + "/" + e.totalFiles + " " + t("Dateien", "files"));
    else if (e.fileName) parts.push(String(e.fileName).replace(/\\/g, "/").split("/").pop());
  }
  return parts.filter(Boolean).join(" · ");
}

function _qhubHero(e) {
  const Q = window.QHub;
  const labels = [t("Download", "Download"), t("Encoding", "Encoding"),
  t("Upscaling", "Upscaling"), t("Bibliothek", "Library")];
  const active = e.station === "attn" ? 0 : e.station;
  const rail = Q.stations(labels.map(function (label, i) {
    return { label: label, state: i < active ? "done" : (i === active ? "active" : "todo") };
  }));

  const pauseBtn = (e.queue === "downloads" && !e.attn)
    ? '<button class="btn btn-secondary btn-sm qhub-hero-btn" onclick="toggleQueuePause()">'
    + (_queueIsPaused ? t("Fortsetzen", "Resume") : t("Pause", "Pause")) + '</button>'
    : '';
  const cancelFn = e.queue === "downloads" ? "cancelQueueItem"
    : (e.queue === "encoding" ? "cancelEncodingItem" : "cancelUpscaleItem");
  const captchaBtn = e.attn
    ? '<button class="btn btn-sm qhub-hero-btn qhub-hero-btn--attn" onclick="openCaptchaModal(' + e.id + ')">'
    + t("Captcha lösen", "Solve captcha") + '</button>'
    : '';

  // Second track: the episode (or file) that is being worked on right now.
  // Rendered for the whole time the job runs — including at 0% — so the card
  // keeps its height instead of jumping every time a phase changes.
  const epBar = (e.epPct === null || e.epPct === undefined) ? "" :
    '<div class="qhub-hero-subhead">'
    + '<span class="qhub-hero-sublabel">' + Q.esc(e.epLabel) + '</span>'
    + '<span class="qhub-hero-subpct">' + Q.pct(e.epPct) + '</span>'
    + '</div>'
    + '<div class="qhub-hero-bar qhub-hero-bar--ep">'
    + '<div class="qhub-hero-fill qhub-hero-fill--ep" style="width:' + Math.round(e.epPct) + '%"></div>'
    + '</div>';

  // is-paused freezes the stripe animation in the bars (see queue.css) —
  // a paused queue should not look like it is still moving.
  const paused = e.queue === "downloads" && _queueIsPaused;
  return '<div class="qhub-hero-card qhub-hero-card--' + (e.attn ? "attn" : e.queue)
    + (paused ? " is-paused" : "") + '">'
    + '<div class="qhub-hero-poster"'
    + (e.poster ? ' style="background-image:url(\'' + Q.esc(e.poster) + '\')"' : '') + '></div>'
    + '<div class="qhub-hero-body">'
    + '<div class="qhub-hero-head">'
    + '<div class="qhub-hero-titles">'
    + '<div class="qhub-hero-title">' + Q.esc(e.title)
    + (e.episode ? ' <span class="qhub-hero-ep">' + Q.esc(e.episode) + '</span>' : '')
    + (e.chip ? ' <span class="qhub-hero-chip">' + Q.esc(e.chip) + '</span>' : '')
    + '</div>'
    + '<div class="qhub-hero-meta">' + Q.esc(_qhubHeroMeta(e)) + '</div>'
    + '</div>'
    + '<div class="qhub-hero-side">'
    + '<div class="qhub-hero-pct">' + Q.pct(e.pct) + '</div>'
    + captchaBtn + pauseBtn
    + (e.state === "cancelling"
      ? '<button class="btn btn-sm qhub-hero-btn qhub-hero-btn--cancel" disabled>'
      + t("Bricht ab…", "Cancelling…") + '</button>'
      : '<button class="btn btn-sm qhub-hero-btn qhub-hero-btn--cancel" onclick="'
      + cancelFn + '(' + e.id + ')">' + t("Abbrechen", "Cancel") + '</button>')
    + '</div>'
    + '</div>'
    + '<div class="qhub-hero-bar"><div class="qhub-hero-fill" style="width:' + Math.round(e.pct) + '%"></div></div>'
    + epBar
    + rail
    + '</div></div>';
}

/** The whole window body: hero + the three groups. */
function renderQueueHub() {
  const list = document.getElementById("queueList");
  const heroBox = document.getElementById("qhubHero");
  if (!list || !heroBox) return;
  const Q = window.QHub;

  const entries = _qhubEntries();

  // Facets: what is still moving per queue
  const openTotal = ["downloads", "encoding", "upscaling"].reduce(function (n, q) {
    return n + _qhubModel[q].items.filter(i => i.status === "running" || i.status === "queued").length;
  }, 0);
  qhubSetFacet("all", openTotal);

  // ---- Hero: the one thing running right now ------------------------
  // Deliberately the FIRST runner in queue order, not "whatever needs you":
  // /api/queue carries a single global ffmpeg_progress, so any other choice
  // would show one item's speed and percentage on another item's card. An
  // entry waiting for a captcha is surfaced by the "Needs you" group.
  const heroItem = entries.filter(e => e.running)[0] || null;
  if (heroItem) {
    heroBox.innerHTML = _qhubHero(heroItem);
    heroBox.style.display = "";
  } else {
    heroBox.innerHTML = "";
    heroBox.style.display = "none";
  }

  // ---- Groups -------------------------------------------------------
  const rest = entries.filter(e => e !== heroItem);
  const needsYou = rest.filter(e => e.attn || e.state === "failed" || e.state === "partial"
    || (e.state === "cancelled" && !e.running));
  // A second running job (an encode next to a download, on "Everything") used
  // to land under "Up next" although it is working right now. The hero can only
  // ever show one job, so everything else that runs gets its own group.
  const alsoRunning = rest.filter(e => e.running && needsYou.indexOf(e) === -1);
  const upNext = rest.filter(e => e.state === "queued" && needsYou.indexOf(e) === -1
    && alsoRunning.indexOf(e) === -1);
  const doneToday = rest.filter(e => e.state === "completed" && Q.isToday(e.completed_at))
    .sort((a, b) => String(b.completed_at || "").localeCompare(String(a.completed_at || "")));

  let html = "";
  if (needsYou.length) {
    html += _qhubGroup(t("Braucht dich", "Needs you"), needsYou.length)
      + needsYou.map(e => _qhubRow(e, 0)).join("");
  }
  if (alsoRunning.length) {
    html += _qhubGroup(t("Läuft außerdem", "Also running"), alsoRunning.length)
      + alsoRunning.map(e => _qhubRow(e, 0)).join("");
  }
  if (upNext.length) {
    html += _qhubGroup(t("Als nächstes", "Up next"), upNext.length)
      + upNext.map((e, i) => _qhubRow(e, i + 1)).join("");
  }
  if (doneToday.length) {
    html += _qhubGroup(t("Heute fertig", "Finished today"), doneToday.length)
      + doneToday.map(e => _qhubRow(e, 0)).join("");
  }

  if (!html && !heroItem) {
    const pane = window.qhubActivePane;
    const msg = pane === "encoding" ? t("Encoding-Warteschlange ist leer", "Encoding queue is empty")
      : pane === "upscaling" ? t("Upscaling-Warteschlange ist leer", "Upscaling queue is empty")
        : pane === "downloads" ? t("Warteschlange ist leer", "Queue is empty")
          : t("Nichts zu tun. Alle Warteschlangen sind leer.", "Nothing to do. All queues are empty.");
    html = '<div class="queue-empty">' + Q.esc(msg) + '</div>';
  }
  list.innerHTML = html;

  // ---- The global pause button in the bar ---------------------------
  // Hidden while the hero already offers a contextual Pause, so the same
  // action never appears twice.
  const pauseBtn = document.getElementById("queuePauseBtn");
  const pauseLabel = document.getElementById("queuePauseLabel");
  if (pauseBtn) {
    const heroHasPause = heroItem && heroItem.queue === "downloads" && !heroItem.attn;
    const hasDownloads = _qhubModel.downloads.items.some(i => i.status === "running" || i.status === "queued");
    const pane = window.qhubActivePane;
    const relevant = (pane === "all" || pane === "downloads") && hasDownloads && !heroHasPause;
    pauseBtn.style.display = relevant ? "" : "none";
    pauseBtn.classList.toggle("queue-pause-btn--paused", _queueIsPaused);
    if (pauseLabel) {
      pauseLabel.textContent = _queueIsPaused
        ? t("Alle fortsetzen", "Resume all") : t("Alles pausieren", "Pause all");
    }
  }
}

// Kept so older callers (and third-party pages) don't break.
function renderQueue() { renderQueueHub(); }
function updateFilterCounts() { /* the hub has no filter row */ }
function updatePauseButton() { /* handled inside renderQueueHub */ }

function parseSeasonEpisode(url) {
  const m = url.match(/staffel-(\d+)\/episode-(\d+)/i);
  if (m) return "S" + m[1] + "E" + m[2];
  const f = url.match(/filme\/film-(\d+)/i);
  if (f) return "Film " + f[1];
  return "";
}

// =====================================================================
// Download queue actions
// =====================================================================

async function cancelQueueItem(id) {
  try {
    const resp = await fetch("/api/queue/" + id + "/cancel", { method: "POST" });
    const data = await resp.json();
    if (data.error) {
      if (typeof showToast === "function") showToast(data.error);
    } else if (typeof showToast === "function") {
      showToast(t("Download wird abgebrochen – Teildateien werden aufgeräumt.",
        "Cancelling the download – partial files are being cleaned up."));
    }
    loadQueue();
  } catch (e) {
    /* ignore */
  }
}

async function moveQueueItem(id, direction) {
  try {
    const resp = await fetch("/api/queue/" + id + "/move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction }),
    });
    const data = await resp.json();
    if (data.error && typeof showToast === "function") showToast(data.error);
    loadQueue();
  } catch (e) {
    /* ignore */
  }
}

async function restartQueueItem(id) {
  try {
    const resp = await fetch("/api/queue/" + id + "/restart", { method: "POST" });
    let data;
    try {
      data = await resp.json();
    } catch (e) {
      if (typeof showToast === "function")
        showToast(t("Neustart fehlgeschlagen (Server-Fehler).", "Restart failed (server error)."));
      loadQueue();
      return;
    }
    if (data.error) {
      if (typeof showToast === "function") showToast(data.error);
    } else if (typeof showToast === "function") {
      const epCount = data.episodes || 0;
      showToast(epCount > 0
        ? epCount + t(" Episode(n) wurden erneut in die Warteschlange gestellt.",
          " episode(s) were added to the queue again.")
        : t("Neu gestartet.", "Restarted."));
    }
    loadQueue();
  } catch (e) {
    if (typeof showToast === "function") showToast(t("Neustart fehlgeschlagen: ", "Restart failed: ") + e.message);
    loadQueue();
  }
}

async function removeQueueItem(id) {
  try {
    const resp = await fetch("/api/queue/" + id, { method: "DELETE" });
    const data = await resp.json();
    if (data.error && typeof showToast === "function") showToast(data.error);
    loadQueue();
  } catch (e) {
    /* ignore */
  }
}

const escQ = window.mfEscape;  // shared, quote-safe (static/mf_escape.js)

// ESC closes the hub / the captcha window
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && queueHubOpen) closeQueueHub();
  if (e.key === "Escape" && captchaModalOpen) closeCaptchaModal();
});

// =====================================================================
// Captcha window
// =====================================================================

let captchaModalOpen = false;
let captchaQueueId = null;
let captchaRefreshTimer = null;
let captchaStatusTimer = null;

function openCaptchaModal(queueId) {
  captchaQueueId = queueId;
  captchaModalOpen = true;
  const overlay = document.getElementById("captchaOverlay");
  const img = document.getElementById("captchaScreenshot");
  const hint = document.getElementById("captchaHint");
  if (!overlay || !img) return;

  img.src = "";
  if (hint) hint.textContent = t("Lade Browser-Screenshot...", "Loading browser screenshot...");
  overlay.style.display = "block";

  captchaRefreshTimer = setInterval(function () {
    img.src = "/api/captcha/" + queueId + "/screenshot?t=" + Date.now();
    img.onload = function () {
      if (hint) hint.textContent = t("Klicke irgendwo im Screenshot um mit dem Captcha zu interagieren.",
        "Click anywhere in the screenshot to interact with the captcha.");
    };
    img.onerror = function () {
      if (hint) hint.textContent = t("Warte auf Captcha-Browser...", "Waiting for captcha browser...");
    };
  }, 800);

  captchaStatusTimer = setInterval(async function () {
    try {
      const resp = await fetch("/api/captcha/" + queueId + "/status");
      const data = await resp.json();
      if (!data.active || data.done) {
        closeCaptchaModal();
        if (typeof showToast === "function")
          showToast(t("Captcha gelöst! Download wird fortgesetzt...", "Captcha solved! Download will continue..."));
        loadQueue();
      }
    } catch (e) {
      /* ignore */
    }
  }, 1500);
}

function closeCaptchaModal() {
  captchaModalOpen = false;
  captchaQueueId = null;
  const overlay = document.getElementById("captchaOverlay");
  if (overlay) overlay.style.display = "none";
  if (captchaRefreshTimer) { clearInterval(captchaRefreshTimer); captchaRefreshTimer = null; }
  if (captchaStatusTimer) { clearInterval(captchaStatusTimer); captchaStatusTimer = null; }
}

(function attachCaptchaClickHandler() {
  document.addEventListener("click", function (e) {
    const img = document.getElementById("captchaScreenshot");
    if (!img || e.target !== img || !captchaQueueId) return;
    const rect = img.getBoundingClientRect();
    const scaleX = img.naturalWidth / img.clientWidth;
    const scaleY = img.naturalHeight / img.clientHeight;
    fetch("/api/captcha/" + captchaQueueId + "/click", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        x: Math.round((e.clientX - rect.left) * scaleX),
        y: Math.round((e.clientY - rect.top) * scaleY),
      }),
    }).catch(function () { });
  });
})();

// Background badge poll every 5s.
// Hits /api/queue/badge, not /api/queue: the latter is a SELECT * including
// every job's episodes JSON, i.e. a few hundred KB every 5s per open tab for
// a single number. Paused while the tab is hidden (mf_poll.js).
(function startBadgePoll() {
  // The home page's run strip is fed from here. It used to be updated only by
  // loadQueue(), which is alive exclusively while the queue hub is open -- so
  // on the start page the strip kept whatever percentage it was rendered with
  // and only looked right again after a reload. /api/queue/badge now carries
  // the running job, its progress and the pause flag, so this one poll covers
  // both the badge and the strip.
  function hasRunStrip() { return !!document.getElementById("homeRunStrip"); }

  function paintRunStrip(data) {
    if (typeof window.renderHomeRunStrip !== "function") return;
    const items = [];
    if (data && data.running) items.push(data.running);
    // renderHomeRunStrip counts waiting jobs by status, so hand it stubs
    // rather than a second, divergent code path.
    for (let i = 0; i < (data && data.queued || 0); i++) items.push({ status: "queued" });
    window.renderHomeRunStrip(items, (data && data.ffmpeg_progress) || {}, !!(data && data.paused));
  }

  async function refreshQueueBadge() {
    try {
      const resp = await fetch("/api/queue/badge");
      if (!resp.ok) return;
      const data = await resp.json();
      applyDownloadBadge(data.badge || 0, data.urls || []);
      paintRunStrip(data);
    } catch (e) { /* ignore */ }
  }
  // A progress bar at 5s reads as broken, so the page that shows one polls at
  // 2s -- every other page keeps the cheap 5s badge cadence. Both intervals
  // pause while the tab is hidden (mf_poll.js). Armed after DOMContentLoaded
  // because hasRunStrip() has to see the finished document.
  function start() {
    refreshQueueBadge();
    badgePollTimer = window.mfPoll(function () {
      if (!queueModalOpen) refreshQueueBadge();
    }, hasRunStrip() ? 2000 : 5000);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();

// Seerr badge — fetch count on every page and keep it fresh
(function startSeerrBadgePoll() {
  async function updateSeerrBadge() {
    const badge = document.getElementById("seerrBadge");
    if (!badge) return;
    try {
      const resp = await fetch("/api/seerr/requests?take=1&skip=0");
      if (!resp.ok) return;
      const data = await resp.json();
      if (data.error) { badge.style.display = "none"; return; }
      const n = data.total || 0;
      badge.textContent = n;
      badge.style.display = n > 0 ? "" : "none";
    } catch (e) { /* ignore — Seerr may not be configured */ }
  }
  updateSeerrBadge();
  window.mfPoll(updateSeerrBadge, 60000);
})();

// The sidebar has ONE Queues entry, so it carries the sum of all three
// queues. The per-queue numbers are kept here as numbers instead of being read
// back out of three badge elements: those elements only existed inside the old
// Queues sub-menu, and a DOM-based sum would silently report 0 without them.
// Each loader writes its own slot (see updateBadge, _updateEncodingBadges,
// _updateUpscaleBadges) and then calls this.
window._qBadgeCounts = window._qBadgeCounts || { downloads: 0, encoding: 0, upscaling: 0 };

window.updateTotalQueueBadge = function () {
  const totalBadge = document.getElementById("totalQueueBadge");
  if (!totalBadge) return;
  const c = window._qBadgeCounts;
  const total = (c.downloads || 0) + (c.encoding || 0) + (c.upscaling || 0);
  totalBadge.textContent = total;
  totalBadge.style.display = total > 0 ? "inline-block" : "none";
};
