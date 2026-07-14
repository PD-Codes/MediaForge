// Auto-Sync page logic

const autosyncList = document.getElementById("autosyncList");
const autosyncEmpty = document.getElementById("autosyncEmpty");

// Schedule map for computing next check time
const SCHEDULE_INTERVALS = {
  "1min": 60,
  "30min": 1800,
  "1h": 3600,
  "2h": 7200,
  "4h": 14400,
  "8h": 28800,
  "12h": 43200,
  "16h": 57600,
  "24h": 86400,
};
const SCHEDULE_LABELS = {
  "1min": "1 min",
  "30min": "30 min",
  "1h": "1h",
  "2h": "2h",
  "4h": "4h",
  "8h": "8h",
  "12h": "12h",
  "16h": "16h",
  "24h": "24h",
};

// Seconds per unit for the adaptive "retry after" interval (mirrors SYNC_ADAPTIVE_UNIT_MAP in app.py)
const ADAPTIVE_UNIT_SECONDS = {
  "days": 86400,
  "weeks": 7 * 86400,
  "months": 30 * 86400,
};

let currentSyncSchedule = "0";
let currentSyncMode = "interval";
let currentSyncDays = "0,1,2,3,4,5,6";
let currentSyncTimes = "06:00";
let adaptiveEnabled = false;
let adaptiveRetryValue = 2;
let adaptiveRetryUnit = "days";
let customPathsCache = [];
let langSepEnabled = false;
let _runningJobs = new Set();
let _pollTimer = null;

// Grouping / view state
let currentView = "__all__";        // "__all__" | "__adaptive__" | "g:<name>"
let currentAutoGroup = "off";       // off | path | language | provider | user
let currentSearch = "";
let _visibleJobIds = new Set();      // ids currently rendered (select-all + toolbar)
let _collapsedGroups = {};           // auto-group collapse state (persisted)
let _lastAutoKeys = [];              // index -> full collapse key for the last render
let _renameOldName = "";

async function pollRunningJobs() {
  try {
    const res = await fetch("/api/autosync/running");
    const data = await res.json();
    const nowRunning = new Set(data.running || []);
    const changed = nowRunning.size !== _runningJobs.size ||
      [...nowRunning].some(id => !_runningJobs.has(id)) ||
      [..._runningJobs].some(id => !nowRunning.has(id));
    _runningJobs = nowRunning;
    if (changed) loadAutosyncJobs();
    if (_runningJobs.size > 0) {
      _pollTimer = setTimeout(pollRunningJobs, 3000);
    } else {
      _pollTimer = null;
    }
  } catch (_) {}
}

function startPollingIfNeeded() {
  if (_runningJobs.size > 0 && !_pollTimer) {
    _pollTimer = setTimeout(pollRunningJobs, 3000);
  }
}

async function loadSyncSchedule() {
  try {
    const resp = await fetch("/api/settings");
    const data = await resp.json();
    currentSyncSchedule = data.sync_schedule || "0";
    currentSyncMode = data.sync_mode === "weekly" ? "weekly" : "interval";
    currentSyncDays = String(data.sync_days || "0,1,2,3,4,5,6");
    currentSyncTimes = String(data.sync_times || "06:00");
    adaptiveEnabled = data.sync_adaptive_enabled === "1";
    adaptiveRetryValue = parseInt(data.sync_adaptive_retry_value, 10) || 2;
    adaptiveRetryUnit = data.sync_adaptive_retry_unit || "days";
    langSepEnabled = data.lang_separation === "1";
  } catch (e) {
    /* ignore */
  }
}

async function loadCustomPathsForEdit() {
  try {
    const resp = await fetch("/api/custom-paths");
    const data = await resp.json();
    customPathsCache = data.paths || [];
  } catch (e) {
    customPathsCache = [];
  }
}

async function loadAutosyncJobs() {
  try {
    // Lade Custom Paths IMMER vor dem Rendern
    await loadCustomPathsForEdit();
    const [jobsRes, runningRes] = await Promise.all([
      fetch("/api/autosync"),
      fetch("/api/autosync/running"),
    ]);
    const jobsData = await jobsRes.json();
    const runningData = await runningRes.json();
    _runningJobs = new Set(runningData.running || []);
    renderJobs(jobsData.jobs || []);
    startPollingIfNeeded();
  } catch (e) {
    let msg = t('Sync-Jobs konnten nicht geladen werden.', 'Sync jobs could not be loaded.');
    if (e && e.stack) {
      msg += '<br><pre style="color:#f87171;font-size:.9em">' + esc(e.stack) + '</pre>';
    } else if (e && e.message) {
      msg += '<br><pre style="color:#f87171;font-size:.9em">' + esc(e.message) + '</pre>';
    }
    autosyncList.innerHTML = '<div class="queue-empty">' + msg + '</div>';
    console.error(t('Fehler beim Laden der Sync-Jobs:',"Sync jobs could not be loaded."), e);
  }
}

function _nextWeeklyCheck() {
  const days = new Set(
    currentSyncDays.split(",").map((x) => parseInt(x.trim(), 10)).filter((x) => !isNaN(x)),
  );
  const times = currentSyncTimes.split(",").map((s) => s.trim()).filter(Boolean)
    .map((s) => s.split(":").map((n) => parseInt(n, 10)))
    .filter((a) => a.length === 2 && !isNaN(a[0]) && !isNaN(a[1]));
  if (!days.size || !times.length) return "—";
  const now = new Date();
  for (let d = 0; d < 8; d++) {
    const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() + d);
    const pyWeekday = (day.getDay() + 6) % 7; // JS Sun=0 -> Python Mon=0
    if (!days.has(pyWeekday)) continue;
    for (const [hh, mm] of times.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1])) {
      const slot = new Date(day.getFullYear(), day.getMonth(), day.getDate(), hh, mm);
      if (slot > now) {
        const pad = (n) => String(n).padStart(2, "0");
        return pad(slot.getDate()) + "." + pad(slot.getMonth() + 1) + "." + slot.getFullYear() +
               " " + pad(slot.getHours()) + ":" + pad(slot.getMinutes());
      }
    }
  }
  return "—";
}

function computeNextCheck(lastCheck, job) {
  if (currentSyncMode === "weekly") return _nextWeeklyCheck();
  if (!lastCheck || currentSyncSchedule === "0") return "—";
  let interval = SCHEDULE_INTERVALS[currentSyncSchedule];
  if (!interval) return "—";
  // While a job is in Adaptive Auto-Sync pause mode it is re-checked on the
  // wider "retry after" interval, not the regular schedule (mirrors the worker).
  if (job && job.adaptive_paused && adaptiveEnabled) {
    const unitSecs = ADAPTIVE_UNIT_SECONDS[adaptiveRetryUnit] || 86400;
    interval = adaptiveRetryValue * unitSecs;
  }
  const lastMs = new Date(lastCheck + "Z").getTime();
  const nextMs = lastMs + interval * 1000;
  const now = Date.now();
  if (!nextMs || nextMs <= now) return t("Bald", "Soon");
  return formatDate(
    new Date(nextMs)
      .toISOString()
      .replace("Z", "")
      .replace("T", " ")
      .slice(0, 19),
  );
}

function renderJobs(jobs) {
  // Keep currentJobs in sync for batch ops
  currentJobs = jobs;
  // Drop selections for jobs that no longer exist
  const existing = new Set(jobs.map((j) => j.id));
  [...selectedJobIds].forEach((id) => { if (!existing.has(id)) selectedJobIds.delete(id); });

  // Populate batch path dropdown
  const batchPath = document.getElementById("batchPathSelect");
  if (batchPath) {
    while (batchPath.options.length > 1) batchPath.remove(1);
    customPathsCache.forEach((p) => {
      const o = document.createElement("option");
      o.value = p.id; o.textContent = p.name + " (" + p.path + ")";
      batchPath.appendChild(o);
    });
  }

  rebuildViewDropdown(jobs);
  rebuildGroupDatalist(jobs);

  const viewSel = document.getElementById("viewSelect");
  const autoOn = currentAutoGroup && currentAutoGroup !== "off";
  if (viewSel) viewSel.disabled = autoOn;
  _updateRenameBtn();

  if (!jobs.length) {
    _visibleJobIds = new Set();
    autosyncList.innerHTML =
      '<div class="queue-empty">' +
      t("Noch keine Sync-Jobs. Füge eine Serie über die Suchseite hinzu.", "No sync jobs yet. Add a series via the search page.") +
      "</div>";
    _updateBatchToolbar();
    return;
  }

  const filtered = jobs.filter(_matchSearch);
  const result = autoOn ? renderAutoGroups(filtered) : renderManualView(filtered);
  _visibleJobIds = result.ids;
  autosyncList.innerHTML =
    result.html ||
    '<div class="queue-empty">' +
      (currentSearch
        ? t("Keine Treffer für die Suche.", "No matches for the search.")
        : t("Keine Jobs in dieser Ansicht.", "No jobs in this view.")) +
    "</div>";
  _updateBatchToolbar();
}

function _matchSearch(job) {
  if (!currentSearch) return true;
  return (job.title || "").toLowerCase().includes(currentSearch);
}

function groupNamesFromJobs(jobs) {
  const set = new Set();
  jobs.forEach((j) => {
    const g = (j.group_name || "").trim();
    if (g) set.add(g);
  });
  return [...set].sort((a, b) => a.localeCompare(b));
}

function rebuildViewDropdown(jobs) {
  const sel = document.getElementById("viewSelect");
  if (!sel) return;
  const names = groupNamesFromJobs(jobs);
  // If the selected group disappeared, fall back to "Alle"
  if (currentView.startsWith("g:") && !names.includes(currentView.slice(2))) {
    currentView = "__all__";
    _savePrefs();
  }
  const counts = {};
  jobs.forEach((j) => {
    const g = (j.group_name || "").trim();
    if (g) counts[g] = (counts[g] || 0) + 1;
  });
  const adaptiveCount = jobs.filter((j) => j.adaptive_paused).length;
  const enabledCount = jobs.filter((j) => j.enabled && !j.adaptive_paused).length;
  const disabledCount = jobs.filter((j) => !j.enabled).length;
  sel.innerHTML = "";
  const addOpt = (val, label) => {
    const o = document.createElement("option");
    o.value = val; o.textContent = label;
    sel.appendChild(o);
  };
  addOpt("__all__", t("Alle", "All"));
  addOpt("__enabled__", t("Aktiviert", "Enabled") + (enabledCount ? " (" + enabledCount + ")" : ""));
  addOpt("__disabled__", t("Deaktiviert", "Disabled") + (disabledCount ? " (" + disabledCount + ")" : ""));
  addOpt("__adaptive__", t("Adaptiver Auto-Sync", "Adaptive Auto-Sync") + (adaptiveCount ? " (" + adaptiveCount + ")" : ""));
  names.forEach((n) => addOpt("g:" + n, n + " (" + (counts[n] || 0) + ")"));
  sel.value = currentView;
  if (sel.value !== currentView) { currentView = "__all__"; sel.value = "__all__"; }
}

function rebuildGroupDatalist(jobs) {
  const dl = document.getElementById("groupDatalist");
  if (!dl) return;
  dl.innerHTML = "";
  groupNamesFromJobs(jobs).forEach((n) => {
    const o = document.createElement("option");
    o.value = n;
    dl.appendChild(o);
  });
}

function _updateRenameBtn() {
  const btn = document.getElementById("renameGroupBtn");
  if (!btn) return;
  const show = autosyncCanManage && currentAutoGroup === "off" && currentView.startsWith("g:");
  btn.style.display = show ? "" : "none";
}

function onViewChange() {
  const sel = document.getElementById("viewSelect");
  currentView = sel ? sel.value : "__all__";
  _savePrefs();
  clearSelection();
  renderJobs(currentJobs);
}

function onAutoGroupChange() {
  const sel = document.getElementById("autoGroupSelect");
  currentAutoGroup = sel ? sel.value : "off";
  _savePrefs();
  clearSelection();
  renderJobs(currentJobs);
}

function onSearchInput(val) {
  currentSearch = (val || "").trim().toLowerCase();
  renderJobs(currentJobs);
}

function _autoGroupValue(job, attr) {
  if (attr === "path") {
    if (job.custom_path_id != null && job.custom_path_id !== "") {
      const cp = customPathsCache.find((p) => String(p.id) === String(job.custom_path_id));
      return cp ? cp.name : "Custom #" + job.custom_path_id;
    }
    return t("Standard", "Default");
  }
  if (attr === "language") return job.language || "—";
  if (attr === "provider") return job.provider || "—";
  if (attr === "user") return job.added_by || "—";
  return "—";
}

function renderManualView(filtered) {
  let list, grayedFn;
  if (currentView === "__adaptive__") {
    list = filtered.filter((j) => j.adaptive_paused);
    grayedFn = () => false;
  } else if (currentView === "__enabled__") {
    // Adaptive-paused jobs have their own view, so exclude them here.
    list = filtered.filter((j) => j.enabled && !j.adaptive_paused);
    grayedFn = () => false;
  } else if (currentView === "__disabled__") {
    list = filtered.filter((j) => !j.enabled);
    grayedFn = () => false;
  } else if (currentView.startsWith("g:")) {
    const name = currentView.slice(2);
    list = filtered.filter((j) => (j.group_name || "").trim() === name);
    grayedFn = (j) => !!j.adaptive_paused;
  } else {
    // "Alle": show every job, including those in Adaptive Auto-Sync pause mode
    // (shown greyed out, like in the grouped/auto views).
    list = filtered;
    grayedFn = (j) => !!j.adaptive_paused;
  }
  const ids = new Set(list.map((j) => j.id));
  if (!list.length) return { html: "", ids };
  let html = '<div class="sync-card-grid">';
  list.forEach((j) => { html += _buildJobCard(j, grayedFn(j)); });
  html += "</div>";
  return { html, ids };
}

function renderAutoGroups(filtered) {
  const attr = currentAutoGroup;
  const groups = {};
  filtered.forEach((j) => {
    const key = _autoGroupValue(j, attr);
    (groups[key] = groups[key] || []).push(j);
  });
  const keys = Object.keys(groups).sort((a, b) => a.localeCompare(b));
  const ids = new Set(filtered.map((j) => j.id));
  _lastAutoKeys = keys.map((k) => attr + ":" + k);
  if (!keys.length) return { html: "", ids };
  let html = "";
  keys.forEach((k, idx) => {
    const arr = groups[k];
    const collapsed = !!_collapsedGroups[attr + ":" + k];
    html +=
      '<div class="autosync-group' + (collapsed ? " collapsed" : "") + '">' +
        '<div class="autosync-group-header" onclick="toggleAutoGroupCollapse(' + idx + ')">' +
          '<span class="autosync-group-chevron">▸</span>' +
          '<span class="autosync-group-name">' + esc(k) + "</span>" +
          '<span class="autosync-group-count">' + arr.length + "</span>" +
        "</div>" +
        '<div class="autosync-group-body"><div class="sync-card-grid">';
    arr.forEach((j) => { html += _buildJobCard(j, !!j.adaptive_paused); });
    html += "</div></div></div>";
  });
  return { html, ids };
}

function toggleAutoGroupCollapse(idx) {
  const key = _lastAutoKeys[idx];
  if (key == null) return;
  _collapsedGroups[key] = !_collapsedGroups[key];
  _savePrefs();
  renderJobs(currentJobs);
}

function _buildJobCard(job, grayed) {
  const isRunning = _runningJobs.has(job.id);
  const isOnHold = !isRunning && job.on_hold;
  const isAdaptivePaused = !isRunning && !isOnHold && job.adaptive_paused;
  const statusClass = isRunning
    ? "queue-status-running"
    : isOnHold
    ? "queue-status-hold"
    : isAdaptivePaused
    ? "queue-status-adaptive"
    : job.enabled
    ? "queue-status-completed"
    : "queue-status-queued";
  const statusLabel = isRunning
    ? '<span class="sync-spinner"></span>' + t("Läuft…", "Running…")
    : isOnHold
    ? "⏸ Hold"
    : isAdaptivePaused
    ? t("Im Adaptiven Auto-Sync", "In Adaptive Auto-Sync")
    : job.enabled ? t("Aktiviert", "Enabled") : t("Deaktiviert", "Disabled");

  const lastCheck = job.last_check ? formatDate(job.last_check) : "—";
  const nextCheck = job.enabled ? computeNextCheck(job.last_check, job) : "—";
  const lastNew = job.last_new_found ? formatDate(job.last_new_found) : "—";

  let dlPath = t("Standard", "Default");
  if (job.custom_path_id != null && job.custom_path_id !== "") {
    const cp = customPathsCache.find((p) => String(p.id) === String(job.custom_path_id));
    dlPath = cp ? cp.name : "Custom #" + job.custom_path_id;
  }

  const localCount = job.local_episodes_found != null ? job.local_episodes_found : "?";
  const newCount = job.last_new_count || 0;

  let filterPill = "";
  if (job.episode_filter && window.AutosyncFilter) {
    const sum = window.AutosyncFilter.summarize(job.episode_filter);
    if (sum) {
      filterPill =
        '<span class="queue-meta-pill" title="' +
        t("Episoden-Filter aktiv", "Episode filter active") +
        '">⛃ ' + esc(sum) + "</span>";
    }
  }
  const groupPill = (job.group_name && String(job.group_name).trim())
    ? '<span class="queue-meta-pill queue-group-pill" title="' + t("Gruppe", "Group") + '">🗂 ' + esc(job.group_name) + "</span>"
    : "";

  let lastResultHtml;
  if (!job.last_check) {
    lastResultHtml = '<span class="sync-stat-neutral">—</span>';
  } else if (newCount > 0) {
    lastResultHtml = '<span class="sync-stat-new">✓ ' + newCount + " " + t("neu", "new") + "</span>";
  } else {
    lastResultHtml = '<span class="sync-stat-neutral">' + t("Aktuell", "Up to date") + "</span>";
  }

  const syncBtnDisabled = (isRunning || grayed) ? ' disabled style="opacity:.4;cursor:not-allowed"' : "";

  return (
    '<div class="sync-card' + (grayed ? " sync-card-grayed" : "") + '">' +
      '<div class="sync-card-header">' +
        (autosyncCanManage ? '<label class="sync-card-cb-wrap" onclick="event.stopPropagation()" title="Auswählen"><input type="checkbox" class="sync-card-checkbox chb-main" ' + (selectedJobIds.has(job.id) ? "checked" : "") + ' onchange="toggleJobSelection(' + job.id + ', this.checked)" /></label>' : "") +
        '<div class="sync-card-title" title="' + esc(job.series_url) + '">' + esc(job.title) + "</div>" +
        '<span class="queue-status ' + statusClass + '">' + statusLabel + "</span>" +
      "</div>" +

      '<div class="sync-card-pills">' +
        '<span class="queue-meta-pill">' + esc(job.language) + "</span>" +
        '<span class="queue-meta-pill">' + esc(job.provider) + "</span>" +
        '<span class="queue-meta-pill">' + esc(dlPath) + "</span>" +
        filterPill + groupPill +
        (job.added_by ? '<span class="queue-meta-pill queue-user">' + esc(job.added_by) + "</span>" : "") +
      "</div>" +

      '<div class="sync-card-stats">' +
        '<div class="sync-stat-row">' +
          '<span class="sync-stat-label">' + t("Episoden", "Episodes") + "</span>" +
          '<span class="sync-stat-value" title="' + localCount + " lokal / " + job.episodes_found + ' online">' +
            localCount + " / " + job.episodes_found +
          "</span>" +
        "</div>" +
        '<div class="sync-stat-row">' +
          '<span class="sync-stat-label">' + t("Letztes Ergebnis", "Last result") + "</span>" +
          '<span class="sync-stat-value">' + lastResultHtml + "</span>" +
        "</div>" +
        '<div class="sync-stat-row">' +
          '<span class="sync-stat-label">' + t("Zuletzt geprüft", "Last checked") + "</span>" +
          '<span class="sync-stat-value">' + lastCheck + "</span>" +
        "</div>" +
        '<div class="sync-stat-row">' +
          '<span class="sync-stat-label">' + t("Nächste Prüfung", "Next check") + "</span>" +
          '<span class="sync-stat-value">' + nextCheck + "</span>" +
        "</div>" +
        (lastNew !== "—" ? '<div class="sync-stat-row">' +
          '<span class="sync-stat-label">' + t("Zuletzt neu", "Last new") + "</span>" +
          '<span class="sync-stat-value">' + lastNew + "</span>" +
        "</div>" : "") +
      "</div>" +

      (job.last_error
        ? '<div class="sync-error-row"><strong>' + t("Fehler:", "Error:") + "</strong>" + esc(job.last_error) + "</div>"
        : "") +

      '<div class="sync-card-actions">' +
        (autosyncCanManage ? '<button class="queue-move sync-card-btn" onclick="openEditModal(' + job.id + ')" title="' + t("Bearbeiten", "Edit") + '">✎ ' + t("Bearbeiten", "Edit") + "</button>" : "") +
        '<button class="queue-move sync-card-btn sync-card-btn-sync" onclick="syncNow(' + job.id + ')" title="' + t("Jetzt synchronisieren", "Sync now") + '"' + syncBtnDisabled + ">⟳ Sync</button>" +
        (autosyncCanManage ? '<button class="queue-remove sync-card-btn" onclick="removeJob(' + job.id + ')" title="' + t("Entfernen", "Remove") + '">✕</button>' : "") +
      "</div>" +
    "</div>"
  );
}

// ===== Group view preferences (persisted) =====
function _loadPrefs() {
  try {
    const v = localStorage.getItem("autosync_view"); if (v) currentView = v;
    const a = localStorage.getItem("autosync_autogroup"); if (a) currentAutoGroup = a;
    const c = localStorage.getItem("autosync_collapsed"); if (c) _collapsedGroups = JSON.parse(c) || {};
  } catch (_) {}
}
function _savePrefs() {
  try {
    localStorage.setItem("autosync_view", currentView);
    localStorage.setItem("autosync_autogroup", currentAutoGroup);
    localStorage.setItem("autosync_collapsed", JSON.stringify(_collapsedGroups));
  } catch (_) {}
}
function _applyPrefsToControls() {
  const ag = document.getElementById("autoGroupSelect"); if (ag) ag.value = currentAutoGroup;
  const ss = document.getElementById("syncSearch"); if (ss && currentSearch) ss.value = currentSearch;
}

// ===== Rename group modal =====
function promptRenameGroup() {
  if (!currentView.startsWith("g:")) return;
  _renameOldName = currentView.slice(2);
  const o = document.getElementById("renameOldName");
  const inp = document.getElementById("renameNewInput");
  if (o) o.textContent = _renameOldName;
  if (inp) inp.value = _renameOldName;
  const ov = document.getElementById("renameOverlay");
  if (ov) ov.style.display = "block";
  if (inp) { inp.focus(); inp.select(); }
}
function closeRenameModal() {
  const ov = document.getElementById("renameOverlay");
  if (ov) ov.style.display = "none";
}
async function confirmRenameGroup() {
  const inp = document.getElementById("renameNewInput");
  const newName = inp ? inp.value.trim() : "";
  if (!newName) { showToast(t("Bitte einen Namen eingeben", "Please enter a name")); return; }
  if (newName === _renameOldName) { closeRenameModal(); return; }
  try {
    const r = await fetch("/api/autosync/group/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ old: _renameOldName, new: newName }),
    });
    const d = await r.json();
    if (d.ok) {
      showToast((d.updated || 0) + " " + t("Job(s) verschoben", "job(s) moved"));
      currentView = "g:" + newName;
      _savePrefs();
      closeRenameModal();
      loadAutosyncJobs();
    } else {
      showToast(d.error || t("Umbenennen fehlgeschlagen", "Rename failed"));
    }
  } catch (e) {
    showToast(t("Umbenennen fehlgeschlagen: ", "Rename failed: ") + e.message);
  }
}

function formatDate(isoStr) {
  if (!isoStr) return "—";
  const d = new Date(isoStr + "Z");
  if (isNaN(d.getTime())) {
    // Try without adding Z (already formatted)
    const d2 = new Date(isoStr);
    if (isNaN(d2.getTime())) return "—";
    const pad = (n) => String(n).padStart(2, "0");
    return (
      pad(d2.getDate()) +
      "." +
      pad(d2.getMonth() + 1) +
      "." +
      d2.getFullYear() +
      " " +
      pad(d2.getHours()) +
      ":" +
      pad(d2.getMinutes())
    );
  }
  const pad = (n) => String(n).padStart(2, "0");
  return (
    pad(d.getDate()) +
    "." +
    pad(d.getMonth() + 1) +
    "." +
    d.getFullYear() +
    " " +
    pad(d.getHours()) +
    ":" +
    pad(d.getMinutes())
  );
}

async function syncNow(id) {
  try {
    const res = await fetch("/api/autosync/" + id + "/sync", {
      method: "POST",
    });
    const data = await res.json();
    if (data.ok) {
      showToast(t("Sync gestartet", "Sync started"));
      _runningJobs.add(id);
      startPollingIfNeeded();
      setTimeout(loadAutosyncJobs, 500);
    } else {
      showToast(data.error || t("Sync konnte nicht gestartet werden", "Sync could not be started"));
    }
  } catch (e) {
    showToast(t("Sync konnte nicht gestartet werden", "Sync could not be started"));
  }
}

async function syncAll() {
  const btn = document.getElementById("syncAllBtn");
  if (btn) { btn.disabled = true; btn.textContent = t("Synchronisiere…", "Syncing…"); }
  try {
    const res = await fetch("/api/autosync/sync-all", { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      const msg = data.started > 0
        ? `${data.started} ${t('Job(s) gestartet', 'job(s) started')}${data.skipped > 0 ? `, ${data.skipped} ${t('bereits aktiv', 'already active')}` : ""}`
        : t("Keine Jobs zum Starten (alle bereits aktiv oder deaktiviert)", "No jobs to start (all already active or disabled)");
      showToast(msg);
      setTimeout(loadAutosyncJobs, 2000);
    } else {
      showToast(data.error || t("Fehler beim Starten", "Error starting"));
    }
  } catch (e) {
    showToast(t("Fehler beim Starten aller Syncs", "Error starting all syncs"));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t("Alle synchronisieren", "Sync all"); }
  }
}

async function removeJob(id) {
  // Pass an explicit title/okLabel — without them showConfirm() falls back
  // to its library-delete defaults ("Delete title?" / "Delete"), which is
  // confusing here since this isn't deleting a library title.
  if (!await showConfirm(
    t("Diesen Sync-Job entfernen?", "Remove this sync job?"),
    t("Entfernen", "Remove"),
    t("Sync-Job entfernen?", "Remove sync job?")
  )) return;
  try {
    const res = await fetch("/api/autosync/" + id, { method: "DELETE" });
    const data = await res.json();
    if (data.ok) {
      showToast(t("Sync-Job entfernt", "Sync job removed"));
      loadAutosyncJobs();
    } else {
      showToast(data.error || t("Entfernen fehlgeschlagen", "Remove failed"));
    }
  } catch (e) {
    showToast(t("Sync-Job konnte nicht entfernt werden", "Sync job could not be removed"));
  }
}

// Edit modal
let currentJobs = [];
let selectedJobIds = new Set();
let _editPicker = null;

function _updateBatchToolbar() {
  const toolbar   = document.getElementById("batchToolbar");
  const label     = document.getElementById("batchSelectionLabel");
  const selectAll = document.getElementById("selectAllCheckbox");
  if (!toolbar) return;
  const count = selectedJobIds.size;
  toolbar.style.display = count > 0 ? "flex" : "none";
  if (label) label.textContent = count + " " + t("ausgewählt", "selected");
  // sync selectAll state
  if (selectAll) {
    const total = _visibleJobIds.size;
    selectAll.checked       = count > 0 && count === total;
    selectAll.indeterminate = count > 0 && count < total;
  }
}

function toggleJobSelection(id, checked) {
  if (checked) selectedJobIds.add(id);
  else         selectedJobIds.delete(id);
  _updateBatchToolbar();
}

function toggleSelectAll(checked) {
  selectedJobIds.clear();
  if (checked) _visibleJobIds.forEach(id => selectedJobIds.add(id));
  // re-render checkboxes without full reload
  document.querySelectorAll(".sync-card-checkbox").forEach(cb => {
    cb.checked = checked;
  });
  _updateBatchToolbar();
}

function clearSelection() {
  selectedJobIds.clear();
  document.querySelectorAll(".sync-card-checkbox").forEach(cb => { cb.checked = false; });
  _updateBatchToolbar();
}

async function openEditModal(id) {
  try {
    const res = await fetch("/api/autosync");
    const data = await res.json();
    currentJobs = data.jobs || [];
    const job = currentJobs.find((j) => j.id === id);
    if (!job) {
      showToast(t("Job nicht gefunden", "Job not found"));
      return;
    }

    document.getElementById("editJobId").value = id;
    document.getElementById("editJobTitle").textContent =
      job.title || t("Unbekannt", "Unknown");

    // Rebuild language dropdown based on lang separation setting
    const langSelect = document.getElementById("editLanguage");
    langSelect.innerHTML = "";
    if (langSepEnabled) {
      const opt = document.createElement("option");
      opt.value = "All Languages";
      opt.textContent = t("Alle Sprachen", "All Languages");
      langSelect.appendChild(opt);
    }
    ["German Dub", "English Sub", "German Sub", "English Dub", "English Dub (German Sub)"].forEach((l) => {
      const opt = document.createElement("option");
      opt.value = l;
      opt.textContent = l;
      langSelect.appendChild(opt);
    });
    langSelect.value = job.language || "German Dub";

    document.getElementById("editProvider").value = job.provider || "VOE";
    document.getElementById("editEnabled").value = job.enabled ? "1" : "0";

    // Populate path dropdown
    const pathSelect = document.getElementById("editPath");
    while (pathSelect.options.length > 1) pathSelect.remove(1);
    await loadCustomPathsForEdit();
    customPathsCache.forEach(function (p) {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name + " (" + p.path + ")";
      pathSelect.appendChild(opt);
    });
    pathSelect.value = job.custom_path_id ? String(job.custom_path_id) : "";

    const pathActionSelect = document.getElementById("editPathUnavailableAction");
    if (pathActionSelect) {
      pathActionSelect.value = job.path_unavailable_action || "skip";
    }

    const groupInput = document.getElementById("editGroup");
    if (groupInput) groupInput.value = job.group_name || "";

    // Season/episode filter picker
    _editPicker = null;
    const pickerHost = document.getElementById("editFilterPicker");
    if (pickerHost && window.AutosyncFilter) {
      let existingFilter = null;
      if (job.episode_filter) {
        try {
          existingFilter =
            typeof job.episode_filter === "string"
              ? JSON.parse(job.episode_filter)
              : job.episode_filter;
        } catch (e) {
          existingFilter = null;
        }
      }
      if (existingFilter)
        existingFilter.movie_custom_path_id = job.movie_custom_path_id;
      else if (job.movie_custom_path_id != null)
        existingFilter = { mode: "all", seasons: {}, include_movies: false, movie_custom_path_id: job.movie_custom_path_id };
      _editPicker = window.AutosyncFilter.renderPicker(pickerHost, {
        seriesUrl: job.series_url,
        existingFilter: existingFilter,
        customPaths: customPathsCache,
      });
    }

    document.getElementById("editOverlay").style.display = "block";
  } catch (e) {
    showToast(t("Job konnte nicht geladen werden", "Job could not be loaded"));
  }
}

function closeEditModal() {
  document.getElementById("editOverlay").style.display = "none";
  const ph = document.getElementById("editFilterPicker");
  if (ph) ph.innerHTML = "";
  _editPicker = null;
}

async function saveEdit() {
  const id = document.getElementById("editJobId").value;
  const pathVal = document.getElementById("editPath").value;
  const pathActionEl = document.getElementById("editPathUnavailableAction");
  if (_editPicker && !_editPicker.validate()) {
    showToast(t("Ungültiger Episodenbereich (z. B. „1-12“).", "Invalid episode range (e.g. \"1-12\")."));
    return;
  }
  const body = {
    language: document.getElementById("editLanguage").value,
    provider: document.getElementById("editProvider").value,
    enabled: parseInt(document.getElementById("editEnabled").value),
    custom_path_id: pathVal ? parseInt(pathVal) : null,
    path_unavailable_action: pathActionEl ? pathActionEl.value : "skip",
    group_name: (document.getElementById("editGroup") ? document.getElementById("editGroup").value.trim() : ""),
  };
  if (_editPicker) {
    body.episode_filter = _editPicker.getFilter();
    body.movie_custom_path_id = _editPicker.getMoviePathId();
  }
  try {
    const res = await fetch("/api/autosync/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (data.ok) {
      showToast(t("Job aktualisiert", "Job updated"));
      closeEditModal();
      loadAutosyncJobs();
    } else {
      showToast(data.error || t("Aktualisierung fehlgeschlagen", "Update failed"));
    }
  } catch (e) {
    showToast(t("Job konnte nicht aktualisiert werden", "Job could not be updated"));
  }
}

function showToast(msg) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.style.display = "block";
  clearTimeout(t._timer);
  t._timer = setTimeout(() => {
    t.style.display = "none";
  }, 3000);
}

// Same contract as app.js's esc(): safe for attribute interpolation, which is
// what half the callers below do (title="..."). The old textContent/innerHTML
// trick escaped & and < but left quotes alone, so a title or an error message
// containing a double quote could break out of the attribute it was rendered
// into.
function esc(s) {
  return (s == null ? "" : String(s)).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c];
  });
}

// Init
_loadPrefs();
_applyPrefsToControls();
Promise.all([loadSyncSchedule()]).then(loadAutosyncJobs);
setInterval(loadAutosyncJobs, 30000);

// ===== Export =====
async function exportAutosync() {
  try {
    const r = await fetch("/api/autosync/export");
    if (!r.ok) { showToast(t("Export fehlgeschlagen", "Export failed")); return; }
    const blob = await r.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "autosync_backup.json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    showToast(t("Export erfolgreich", "Export successful"));
  } catch(e) {
    showToast(t("Export fehlgeschlagen: ", "Export failed: ") + e.message);
  }
}

// ===== Import =====
async function importAutosync(input) {
  const file = input.files[0];
  if (!file) return;
  input.value = "";  // reset so same file can be re-selected
  try {
    const text = await file.text();
    const r = await fetch("/api/autosync/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: text,
    });
    const d = await r.json();
    if (!r.ok || d.error) { showToast(d.error || t("Import fehlgeschlagen", "Import failed")); return; }
    let msg = d.imported + " " + t("importiert", "imported");
    if (d.skipped)  msg += ", " + d.skipped + " " + t("übersprungen", "skipped");
    if (d.errors && d.errors.length) msg += ", " + d.errors.length + " " + t("Fehler", "errors");
    showToast(msg);
    loadAutosyncJobs();
  } catch(e) {
    showToast(t("Import fehlgeschlagen: ", "Import failed: ") + e.message);
  }
}

// ===== Batch =====
async function batchAction(action) {
  const ids = [...selectedJobIds];
  if (!ids.length) return;

  let body = { ids, action };
  if (action === "set_path") {
    const sel = document.getElementById("batchPathSelect");
    const val = sel ? sel.value : "";
    body.custom_path_id = val ? parseInt(val) : null;
  }
  if (action === "set_group") {
    const inp = document.getElementById("batchGroupInput");
    const name = inp ? inp.value.trim() : "";
    if (!name) { showToast(t("Bitte einen Gruppennamen eingeben", "Please enter a group name")); return; }
    body.group_name = name;
  }

  if (action === "delete") {
    const ok = await showConfirm(`${ids.length} ` + t('Sync-Job(s) wirklich löschen?', 'sync job(s) really delete?'));
    if (!ok) return;
  }

  try {
    const r = await fetch("/api/autosync/batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok) {
      const label = {
        enable:   t("aktiviert", "enabled"),
        disable:  t("deaktiviert", "disabled"),
        set_path: t("Pfad gesetzt", "path set"),
        set_group: t("zur Gruppe hinzugefügt", "added to group"),
        remove_group: t("aus Gruppe entfernt", "removed from group"),
        delete:   t("gelöscht", "deleted"),
      }[action] || t("aktualisiert", "updated");
      showToast(d.updated + " Job(s) " + label);
      const gi = document.getElementById("batchGroupInput");
      if (gi) gi.value = "";
      clearSelection();
      loadAutosyncJobs();
    } else {
      showToast(d.error || t("Batch-Aktion fehlgeschlagen", "Batch action failed"));
    }
  } catch(e) {
    showToast(t("Batch-Aktion fehlgeschlagen: ", "Batch action failed: ") + e.message);
  }
}
