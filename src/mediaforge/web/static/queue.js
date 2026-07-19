let queueModalOpen = false;
let queuePollTimer = null;
let badgePollTimer = null;
let queueCustomPaths = [];
let _queueIsPaused = false;
let _queueFilter = "all";  // "all" | "active" | "completed" | "failed"

(async function loadQueueCustomPaths() {
  try {
    const resp = await fetch("/api/custom-paths");
    const data = await resp.json();
    queueCustomPaths = data.paths || [];
  } catch (e) {
    /* ignore */
  }
})();

function openQueueModal() {
  queueModalOpen = true;
  document.getElementById("queueOverlay").style.display = "block";
  loadQueue();
  if (queuePollTimer) clearInterval(queuePollTimer);
  queuePollTimer = setInterval(loadQueue, 2000);
}

function closeQueueModal() {
  queueModalOpen = false;
  document.getElementById("queueOverlay").style.display = "none";
  if (queuePollTimer) {
    clearInterval(queuePollTimer);
    queuePollTimer = null;
  }
}

let lastFfmpegProgress = {};
let _stickyFfmpegProgress = {};  // last active snapshot — held across phase/episode gaps
let _stickyUrl = "";             // current_url when snapshot was taken

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
  const mbytes = mbps / 8;
  return mbytes.toFixed(1) + " MB/s";
}

async function loadQueue() {
  try {
    const resp = await fetch("/api/queue");
    const data = await resp.json();
    const items = data.items || [];
    lastFfmpegProgress = data.ffmpeg_progress || {};
    _queueIsPaused = !!data.paused;
    updateFilterCounts(items);
    updatePauseButton(items, _queueIsPaused);
    renderQueue(items);
    updateBadge(items);

  } catch (e) {
    /* ignore */
  }
}

function updateFilterCounts(items) {
  const counts = {
    all: items.length,
    active: items.filter(i => i.status === "running").length,
    queued: items.filter(i => i.status === "queued").length,
    completed: items.filter(i => i.status === "completed").length,
    partial: items.filter(i => i.status === "partial").length,
    failed: items.filter(i => i.status === "failed" || i.status === "cancelled").length,
  };
  Object.entries(counts).forEach(([key, n]) => {
    const el = document.getElementById("qfCount-" + key);
    if (!el) return;
    el.textContent = n > 0 ? n : "";
    el.style.display = n > 0 ? "" : "none";
  });
}

function updatePauseButton(items, paused) {
  const btn = document.getElementById("queuePauseBtn");
  const icon = document.getElementById("queuePauseIcon");
  const label = document.getElementById("queuePauseLabel");
  if (!btn) return;

  // Only show the button when there are active (queued/running) items
  const hasActive = items.some(i => i.status === "running" || i.status === "queued");
  btn.style.display = hasActive ? "" : "none";

  if (paused) {
    btn.title = t("Downloads fortsetzen", "Resume downloads");
    btn.classList.add("queue-pause-btn--paused");
    if (label) label.textContent = t("Fortsetzen", "Resume");
    if (icon) icon.innerHTML =
      '<polygon points="5 3 19 12 5 21 5 3"/>';
  } else {
    btn.title = t("Downloads pausieren","Pause downloads");
    btn.classList.remove("queue-pause-btn--paused");
    if (label) label.textContent = t("Pause","Pause");
    if (icon) icon.innerHTML =
      '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>';
  }
}

function setQueueFilter(filter) {
  _queueFilter = filter;
  // Update active tab
  ["all", "active", "queued", "completed", "partial", "failed"].forEach(f => {
    const tab = document.getElementById("qfTab-" + f);
    if (tab) tab.classList.toggle("active", f === filter);
  });
  loadQueue();
}

async function toggleQueuePause() {
  const wasPaused = _queueIsPaused;
  try {
    const endpoint = wasPaused ? "/api/queue/resume" : "/api/queue/pause";
    await fetch(endpoint, { method: "POST" });
    await loadQueue();
    if (window.showToast) {
      showToast(wasPaused
        ? t("Downloads werden fortgesetzt.","Downloads are being resumed")
        : t("Downloads pausiert.","Downloads paused"));
    }
  } catch (e) {
    if (window.showToast) showToast(t("Fehler: " + e.message, "Error: " + e.message));
  }
}

async function clearOldQueueItems() {
  try {
    await fetch("/api/queue/completed", { method: "DELETE" });
    await loadQueue();
    if (window.showToast) showToast(t("Alte Einträge gelöscht","Old entries deleted"));
  } catch (e) {
    if (window.showToast) showToast(t("Fehler: " + e.message, "Error: " + e.message));
  }
}

function updateBadge(items) {
  const activeItems = items.filter(
    (i) => i.status === "queued" || i.status === "running",
  );
  const active = activeItems.length;
  
  // Update running status on browse cards if app.js is present
  if (window.updateRunningCards) {
    const runningUrls = activeItems.map(i => i.series_url).filter(Boolean);
    window.updateRunningCards(runningUrls);
  }

  const badge = document.getElementById("queueBadge");
  if (active > 0) {
    badge.textContent = active;
    badge.style.display = "inline-block";
  } else {
    badge.style.display = "none";
  }
  if (window.updateTotalQueueBadge) window.updateTotalQueueBadge();
}

function renderQueue(items, paused) {
  const list = document.getElementById("queueList");
  paused = (paused !== undefined) ? paused : _queueIsPaused;

  let visible;
  if (_queueFilter === "active") {
    visible = items.filter(i => i.status === "running");
  } else if (_queueFilter === "queued") {
    visible = items.filter(i => i.status === "queued");
  } else if (_queueFilter === "completed") {
    visible = items.filter(i => i.status === "completed").reverse();
  } else if (_queueFilter === "partial") {
    visible = items.filter(i => i.status === "partial").reverse();
  } else if (_queueFilter === "failed") {
    visible = items.filter(i => i.status === "failed" || i.status === "cancelled").reverse();
  } else {
    // "all" — default: active on top, then last 3 finished (newest first)
    const running = items.filter(i => i.status === "running");
    const queued = items.filter(i => i.status === "queued");
    const done = items
      .filter(i => i.status === "completed" || i.status === "partial" || i.status === "failed" || i.status === "cancelled")
      .slice(-3)
      .reverse();
    visible = running.concat(queued, done);
  }

  if (!visible.length) {
    const emptyMsgs = {
      active:    t("Kein Download läuft gerade.", "No download is currently running."),
      queued:    t("Keine wartenden Downloads.", "No pending downloads."),
      completed: t("Keine abgeschlossenen Downloads.", "No completed downloads."),
      partial:   t("Keine teilweise abgeschlossenen Downloads.", "No partially completed downloads."),
      failed:    t("Keine fehlgeschlagenen oder abgebrochenen Downloads.", "No failed or cancelled downloads."),
      all:       t("Warteschlange ist leer", "Queue is empty"),
    };
    list.innerHTML = '<div class="queue-empty">' + (emptyMsgs[_queueFilter] || emptyMsgs.all) + '</div>';
    return;
  }

  // For non-"all" filters, rebuild queued sub-list from visible only
  const queued = visible.filter(i => i.status === "queued");

  // Remember which error panels are expanded before re-render
  const expandedErrors = new Set();
  list.querySelectorAll(".queue-error-details.expanded").forEach((el) => {
    expandedErrors.add(el.id);
  });

  // Remember old widths for smooth transitions
  const oldWidths = {};
  list.querySelectorAll(".queue-item").forEach((el) => {
    const id = el.dataset.id;
    if (id) {
      const fills = el.querySelectorAll(".queue-progress-fill");
      oldWidths[id] = {
        ep: fills[0] ? fills[0].style.width : null,
        phase: fills[1] ? fills[1].style.width : null
      };
    }
  });

  let html = "";
  visible.forEach((item) => {
    // Determine position within the queued sub-list so we can disable boundary arrows
    const queuedIdx = queued.indexOf(item);  // -1 when item is not queued
    const isRunning = item.status === "running";
    const isActive =
      isRunning || (item.status === "cancelled" && item.current_url);
    const cls = isActive ? "queue-item queue-item-active" : "queue-item";

    const isCancelling = item.status === "cancelled" && item.current_url;

    let statusBadge = "";
    if (item.status === "running")
      statusBadge =
        '<span class="queue-status queue-status-running">' + t("Läuft", "Running") + '</span>';
    else if (item.status === "queued")
      statusBadge = paused
        ? '<span class="queue-status queue-status-paused">' + t("Pausiert", "Paused") + '</span>'
        : '<span class="queue-status queue-status-queued">' + t("Wartend", "Queued") + '</span>';
    else if (item.status === "completed")
      statusBadge =
        '<span class="queue-status queue-status-completed">' + t("Abgeschlossen", "Completed") + '</span>';
    else if (item.status === "partial")
      statusBadge =
        '<span class="queue-status queue-status-partial">' + t("Teilweise erfolgreich", "Partially completed") + '</span>';
    else if (item.status === "failed")
      statusBadge =
        '<span class="queue-status queue-status-failed">' + t("Fehlgeschlagen", "Failed") + '</span>';
    else if (isCancelling)
      statusBadge =
        '<span class="queue-status queue-status-cancelling">' + t("Wird abgebrochen...", "Cancelling...") + '</span>';
    else if (item.status === "cancelled")
      statusBadge =
        '<span class="queue-status queue-status-cancelled">' + t("Abgebrochen", "Cancelled") + '</span>';
    // Captcha badge shown on top of the running badge when captcha_url is set
    const captchaBadge = (isRunning && item.captcha_url)
      ? ' <span class="queue-status queue-status-captcha">CAPTCHA</span>'
      : '';

    let progressHtml = "";
    if (isRunning || isCancelling || item.status === "cancelled") {
      const epPct =
        item.total_episodes > 0
          ? (item.current_episode / item.total_episodes) * 100
          : 0;
      const seInfo = item.current_url
        ? parseSeasonEpisode(item.current_url)
        : "";

      // Combine episode progress with in-episode ffmpeg progress.
      // During the ffmpeg phase, percent resets to 0 — but the download is
      // already done, so we treat the full episode weight as earned to prevent
      // the overall bar from jumping backwards.
      let ffPct = 0;
      if (isRunning && lastFfmpegProgress.active && item.total_episodes > 0) {
        const inEpPct = (lastFfmpegProgress.phase === "ffmpeg" || lastFfmpegProgress.phase === "move" || lastFfmpegProgress.phase === "upscaling")
          ? 100                                 // download done — hold full weight
          : (lastFfmpegProgress.percent || 0);  // still downloading
        ffPct = inEpPct / item.total_episodes;
      }
      const combinedPct = Math.min(Math.round(epPct + ffPct), 100);

      const isFilmPalast = (item.series_url || "").includes("filmpalast.to");
      let epLabel;
      if (isFilmPalast) {
        epLabel = isCancelling ? t("Film – beendet...","Movie - finished...") : item.status === "cancelled" ? t("Film (gestoppt)","Movie (stopped)") : t("Film","Movie");
      } else if (isCancelling) {
        epLabel = item.current_episode + "/" + item.total_episodes + t(" Ep. – beendet aktuelle Episode..."," Ep. - finished current episode...");
      } else if (item.status === "cancelled") {
        epLabel = item.current_episode + "/" + item.total_episodes + t(" Ep. (gestoppt)", " Ep. (stopped)");
      } else {
        epLabel = item.current_episode + "/" + item.total_episodes + t(" Ep.", " Ep.");
      }

      // Second bar: per-episode phase progress (download or ffmpeg)
      // Always rendered while running/cancelling so the modal height stays stable.
      // Between phases/episodes the last active snapshot is held so the bar never
      // collapses to 0% and causes a layout jump.
      let episodeBarHtml = "";
      if (isRunning || isCancelling) {
        const currentUrl = item.current_url || "";

        if (lastFfmpegProgress.active && lastFfmpegProgress.percent > 0) {
          // Fresh data — update sticky snapshot
          _stickyFfmpegProgress = Object.assign({}, lastFfmpegProgress);
          _stickyUrl = currentUrl;
        } else if (_queueIsPaused && !lastFfmpegProgress.active) {
          // Queue is paused and episode finished — clear sticky so the bar
          // resets to 0% while waiting for resume instead of freezing on the
          // last percentage of the completed episode
          _stickyFfmpegProgress = {};
          _stickyUrl = "";
        } else if (currentUrl && currentUrl !== _stickyUrl) {
          // New episode URL with no active progress yet — reset sticky so the
          // bar starts fresh at 0% rather than showing stale data from prev ep
          _stickyFfmpegProgress = {};
          _stickyUrl = currentUrl;
        }
        // If neither condition matched (same URL, just between phases) keep sticky as-is

        const fp = (lastFfmpegProgress.active && lastFfmpegProgress.percent > 0)
          ? lastFfmpegProgress
          : (_stickyFfmpegProgress.active !== undefined ? _stickyFfmpegProgress : lastFfmpegProgress);

        const phase = fp.phase || "";
        const epPct = fp.percent || 0;
        const bw = formatBandwidth(fp.bandwidth || "");
        const dlMb = fp.downloaded_mb || 0;
        const totalMb = fp.total_mb || 0;
        const etaSec = fp.eta_sec || 0;
        const fpsFps = fp.fps || "";
        const fpTime = fp.time || "";
        const fpEncoder = fp.encoder || "";

        let phaseLabel, fillClass;
        const episodeTag = seInfo ? " · " + seInfo : "";
        if (phase === "move") {
          phaseLabel = t("📦 Verschieben", "📦 Moving") + episodeTag;
          fillClass = "queue-progress-fill--move";
        } else if (phase === "upscaling") {
          phaseLabel = t("✨ Upscaling", "✨ Upscaling") + episodeTag;
          fillClass = "queue-progress-fill--upscaling";
        } else if (phase === "ffmpeg") {
          phaseLabel = t("⚙ FFmpeg", "⚙ FFmpeg") + episodeTag;
          fillClass = "queue-progress-fill--ffmpeg";
        } else {
          phaseLabel = t("⬇ Download", "⬇ Download") + episodeTag;
          fillClass = "queue-progress-fill--download";
        }

        // Build info pills
        let pillsHtml = "";
        if (epPct > 0 && phase === "move") {
          // Move phase: show size + speed + ETA
          const fmtMbM = mb => mb >= 1024 ? (mb / 1024).toFixed(2) + " GB" : mb.toFixed(1) + " MB";
          const dlMbM = fp.downloaded_mb || 0;
          const totalMbM = fp.total_mb || 0;
          if (dlMbM > 0) {
            const sizeLabel = totalMbM > 0
              ? escQ(fmtMbM(dlMbM) + " / " + fmtMbM(totalMbM))
              : escQ(fmtMbM(dlMbM));
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill">' + sizeLabel + '</span>';
          }
          if (bw) pillsHtml += '<span class="queue-meta-pill queue-progress-pill">' + escQ(bw) + '</span>';
          if (etaSec > 0) {
            const etaStr = etaSec >= 60
              ? Math.floor(etaSec / 60) + "m " + (etaSec % 60) + "s"
              : etaSec + "s";
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill queue-progress-pill--eta">ETA ' + escQ(etaStr) + '</span>';
          }
        } else if (epPct > 0 && phase === "upscaling") {
          // Upscaling phase: show speed + ETA
          if (fp.speed) {
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill">⚡ ' + escQ(fp.speed) + '</span>';
          }
          if (etaSec > 0) {
            const etaStr = etaSec >= 60
              ? Math.floor(etaSec / 60) + "m " + (etaSec % 60) + "s"
              : etaSec + "s";
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill queue-progress-pill--eta">ETA ' + escQ(etaStr) + '</span>';
          }
        } else if (epPct > 0 && phase === "ffmpeg") {
          // FFmpeg phase: show encoder + fps + ETA
          if (fpEncoder) {
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill">⚙ ' + escQ(fpEncoder) + '</span>';
          }
          if (fpsFps) {
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill">⚡ ' + escQ(fpsFps) + ' fps</span>';
          }
          if (etaSec > 0) {
            const etaStr = etaSec >= 60
              ? Math.floor(etaSec / 60) + "m " + (etaSec % 60) + "s"
              : etaSec + "s";
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill queue-progress-pill--eta">ETA ' + escQ(etaStr) + '</span>';
          }
        } else if (epPct > 0) {
          // Download phase: show size + speed + eta
          const fmtMb = mb => mb >= 1024
            ? (mb / 1024).toFixed(2) + " GB"
            : mb.toFixed(1) + " MB";
          if (dlMb > 0) {
            const sizeLabel = totalMb > 0
              ? escQ(fmtMb(dlMb) + " / " + fmtMb(totalMb))
              : escQ(fmtMb(dlMb));
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill">' + sizeLabel + '</span>';
          }
          if (bw) {
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill">' + escQ(bw) + '</span>';
          }
          if (etaSec > 0) {
            const etaStr = etaSec >= 60
              ? Math.floor(etaSec / 60) + "m " + (etaSec % 60) + "s"
              : etaSec + "s";
            pillsHtml += '<span class="queue-meta-pill queue-progress-pill queue-progress-pill--eta">ETA ' + escQ(etaStr) + '</span>';
          }

        }

        const prevPhaseWidth = (oldWidths[item.id] && oldWidths[item.id].phase) ? oldWidths[item.id].phase : epPct + "%";
        episodeBarHtml =
          '<div class="queue-progress-bar queue-progress-bar--episode">' +
          '<div class="queue-progress-fill ' + fillClass + '" style="width:' + prevPhaseWidth + '" data-target-width="' + epPct + '%"></div>' +
          '</div>' +
          '<div class="queue-progress-footer">' +
          '<span class="queue-progress-phase">' + escQ(phaseLabel) + '</span>' +
          '<span class="queue-progress-pct">' + epPct + '%</span>' +
          '</div>' +
          '<div class="queue-progress-pills">' + pillsHtml + '</div>';
      }

      const prevEpWidth = (oldWidths[item.id] && oldWidths[item.id].ep) ? oldWidths[item.id].ep : combinedPct + "%";
      progressHtml =
        '<div class="queue-progress">' +
        '<div class="queue-progress-bar"><div class="queue-progress-fill" style="width:' + prevEpWidth + '" data-target-width="' + combinedPct + '%"></div></div>' +
        '<div class="queue-progress-footer">' +
        '<span>' + epLabel + '</span>' +
        '<span class="queue-progress-pct">' + combinedPct + '%</span>' +
        '</div>' +
        episodeBarHtml +
        '</div>';
    }

    let errorsHtml = "";
    if (item.errors) {
      let errors = [];
      try {
        errors =
          typeof item.errors === "string"
            ? JSON.parse(item.errors)
            : item.errors;
      } catch (e) { }
      if (errors.length) {
        const errId = "qerr-" + item.id;
        let details = "";
        errors.forEach(function (err) {
          var ep = err.url ? parseSeasonEpisode(err.url) : "";
          var label = ep ? ep + ": " : "";
          details +=
            '<div class="queue-error-detail">' +
            escQ(label + (err.error || "")) +
            "</div>";
        });
        errorsHtml =
          "<div class=\"queue-errors queue-errors-expandable\" onclick=\"this.classList.toggle('expanded');document.getElementById('" +
          errId +
          "').classList.toggle('expanded')\">" +
          errors.length +
          ' Fehler <span class="queue-errors-toggle">&#9654;</span>' +
          "</div>" +
          '<div class="queue-error-details" id="' +
          errId +
          '">' +
          details +
          "</div>";
      }
    }

    let actionBtn = "";
    if (item.status === "queued") {
      const isFirst = queuedIdx === 0;
      const isLast = queuedIdx === queued.length - 1;
      actionBtn =
        '<button class="queue-move" onclick="moveQueueItem(' + item.id + ',\'up\')" title="'+t("Nach oben","Up")+'"' + (isFirst ? ' disabled' : '') + '>&#9650;</button>' +
        '<button class="queue-move" onclick="moveQueueItem(' + item.id + ',\'down\')" title="'+t("Nach unten","Down")+'"' + (isLast ? ' disabled' : '') + '>&#9660;</button>' +
        '<button class="queue-remove" onclick="removeQueueItem(' + item.id + ')" title="'+t("Entfernen","Remove")+'">&times;</button>';
    } else if (item.status === "running") {
      const captchaBtn = item.captcha_url
        ? '<button class="queue-captcha-btn" onclick="openCaptchaModal(' + item.id + ')" title="'+t("Captcha lösen","Solve captcha")+'">&#128274; Lösen</button>'
        : '';
      actionBtn =
        captchaBtn +
        '<button class="queue-cancel" onclick="cancelQueueItem(' + item.id + ')" title="'+t("Nach aktueller Episode abbrechen","Cancel after current episode")+'">Abbrechen</button>';
    } else if (isCancelling) {
      actionBtn = '';
    } else if (item.status === "failed" || item.status === "cancelled") {
      // Show how many episodes would be restarted
      let errCount = 0;
      try {
        const errs = typeof item.errors === "string" ? JSON.parse(item.errors || "[]") : (item.errors || []);
        errCount = errs.length;
      } catch (e) { }
      const restartLabel = errCount > 0
        ? '&#8635; ' + errCount + ' neu'
        : '&#8635; Neu starten';
      actionBtn =
        '<button class="queue-restart" onclick="restartQueueItem(' + item.id + ')" title="' +
        (errCount > 0 ? errCount + t(' fehlerhafte Episoden neu starten', ' fehlerhafte Episoden neu starten') : t('Alle Episoden neu starten', 'Alle Episoden neu starten')) +
        '">' + restartLabel + '</button>' +
        '<button class="queue-remove" onclick="removeQueueItem(' + item.id + ')" title="'+t("Entfernen","Remove")+'">&times;</button>';
    } else if (item.status === "completed" || item.status === "partial") {
      actionBtn =
        '<button class="queue-remove" onclick="removeQueueItem(' + item.id + ')" title="'+t("Entfernen","Remove")+'">&times;</button>';
    }

    const isSync = (item.source || "").startsWith("sync");
    const syncBadge = isSync
      ? '<span class="queue-sync-badge">'+t("Auto&#8209;Sync","Auto-Sync")+'</span> '
      : "";

    const userHtml = item.username
      ? '<span class="queue-meta-pill queue-user">' + escQ(item.username) + "</span>"
      : "";

    let pathHtml = "";
    if (item.custom_path_id) {
      const cp = queueCustomPaths.find((p) => p.id === item.custom_path_id);
      const pathName = cp ? cp.name : "Custom #" + item.custom_path_id;
      pathHtml = '<span class="queue-meta-pill queue-path">' + escQ(pathName) + "</span>";
    }

    html +=
      '<div class="' + cls + '" data-id="' + item.id + '">' +
      '<div class="queue-item-header">' +
      '<div class="queue-item-title">' + syncBadge + escQ(item.title) + '</div>' +
      '<div class="queue-item-right">' + actionBtn + '</div>' +
      '</div>' +
      '<div class="queue-item-meta">' +
      statusBadge + captchaBadge +
      '<span class="queue-meta-pill">' + ((item.series_url || "").includes("filmpalast.to") ? "Film" : item.total_episodes + " Ep.") + '</span>' +
      '<span class="queue-meta-pill">' + escQ(item.language) + '</span>' +
      '<span class="queue-meta-pill">' + escQ(item.provider) + '</span>' +
      pathHtml + userHtml +
      '</div>' +
      progressHtml + errorsHtml +
      '</div>';
  });

  list.innerHTML = html;

  // Trigger progress bar transitions by setting target widths in the next frame
  setTimeout(() => {
    list.querySelectorAll('.queue-progress-fill[data-target-width]').forEach((el) => {
      el.style.width = el.getAttribute('data-target-width');
    });
  }, 50);

  // Restore expanded state (both the details panel and its sibling header)
  expandedErrors.forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.classList.add("expanded");
      const header = el.previousElementSibling;
      if (header) header.classList.add("expanded");
    }
  });
}

function parseSeasonEpisode(url) {
  const m = url.match(/staffel-(\d+)\/episode-(\d+)/i);
  if (m) return "S" + m[1] + "E" + m[2];
  const f = url.match(/filme\/film-(\d+)/i);
  if (f) return "Film " + f[1];
  return "";
}

async function cancelQueueItem(id) {
  try {
    const resp = await fetch("/api/queue/" + id + "/cancel", {
      method: "POST",
    });
    const data = await resp.json();
    if (data.error) {
      if (typeof showToast === "function") showToast(data.error);
    } else {
      if (typeof showToast === "function")
        showToast(t("Nach aktueller Episode wird abgebrochen...","After current episode is cancelled..."));
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
    const resp = await fetch("/api/queue/" + id + "/restart", {
      method: "POST",
    });
    let data;
    try {
      data = await resp.json();
    } catch (e) {
      if (typeof showToast === "function") showToast("Neustart fehlgeschlagen (Server-Fehler).");
      loadQueue();
      return;
    }
    if (data.error) {
      if (typeof showToast === "function") showToast(data.error);
    } else {
      const epCount = data.episodes || 0;
      if (typeof showToast === "function")
        showToast(
          epCount > 0
            ? epCount + t(" Episode(n) wurden erneut in die Warteschlange gestellt.", "Episode(s) were added to the queue again.")
            : t("Neu gestartet.", "Restarted."),
        );
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
    if (data.error) {
      if (typeof showToast === "function") showToast(data.error);
    }
    loadQueue();
  } catch (e) {
    /* ignore */
  }
}

function escQ(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

// ESC key closes queue modal
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape" && queueModalOpen) closeQueueModal();
  if (e.key === "Escape" && captchaModalOpen) closeCaptchaModal();
});

// ===== Captcha Modal =====

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

  // Start screenshot polling
  captchaRefreshTimer = setInterval(function () {
    img.src = "/api/captcha/" + queueId + "/screenshot?t=" + Date.now();
    img.onload = function () {
      if (hint) hint.textContent = t("Klicke irgendwo im Screenshot um mit dem Captcha zu interagieren.", "Click anywhere in the screenshot to interact with the captcha.");
    };
    img.onerror = function () {
      if (hint) hint.textContent = t("Warte auf Captcha-Browser...", "Waiting for captcha browser...");
    };
  }, 800);

  // Poll for solved status
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
  if (captchaRefreshTimer) {
    clearInterval(captchaRefreshTimer);
    captchaRefreshTimer = null;
  }
  if (captchaStatusTimer) {
    clearInterval(captchaStatusTimer);
    captchaStatusTimer = null;
  }
}

(function attachCaptchaClickHandler() {
  document.addEventListener("click", function (e) {
    const img = document.getElementById("captchaScreenshot");
    if (!img || e.target !== img || !captchaQueueId) return;
    const rect = img.getBoundingClientRect();
    const scaleX = img.naturalWidth / img.clientWidth;
    const scaleY = img.naturalHeight / img.clientHeight;
    const x = Math.round((e.clientX - rect.left) * scaleX);
    const y = Math.round((e.clientY - rect.top) * scaleY);
    fetch("/api/captcha/" + captchaQueueId + "/click", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ x, y }),
    }).catch(function () { });
  });
})();

// Background badge poll every 5s
(function startBadgePoll() {
  // Wait for all scripts (like app.js) to be ready before first update
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", loadQueue);
  } else {
    loadQueue();
  }
  badgePollTimer = setInterval(function () {
    if (!queueModalOpen) loadQueue();
  }, 5000);
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
  setInterval(updateSeerrBadge, 60000); // refresh every 60s
})();

window.updateTotalQueueBadge = function() {
  const dBadge = document.getElementById("queueBadge");
  const eBadge = document.getElementById("encodingBadge");
  const uBadge = document.getElementById("upscaleBadge");
  const totalBadge = document.getElementById("totalQueueBadge");
  if (!totalBadge) return;

  const dCount = (dBadge && dBadge.style.display !== "none") ? parseInt(dBadge.textContent) || 0 : 0;
  const eCount = (eBadge && eBadge.style.display !== "none") ? parseInt(eBadge.textContent) || 0 : 0;
  const uCount = (uBadge && uBadge.style.display !== "none") ? parseInt(uBadge.textContent) || 0 : 0;
  
  const total = dCount + eCount + uCount;
  
  totalBadge.textContent = total;
  totalBadge.style.display = total > 0 ? "inline-block" : "none";
};
