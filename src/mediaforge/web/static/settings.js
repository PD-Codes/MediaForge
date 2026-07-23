// ─── Tab navigation ────────────────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll(".settings-tab-btn, .settings-tab").forEach(function (btn) {
    btn.classList.toggle("active", btn.dataset.tab === name);
  });
  document.querySelectorAll(".settings-tab-panel").forEach(function (panel) {
    panel.classList.toggle("active", panel.id === "tab-" + name);
  });
  // Sync sidebar sub-links and keep container open
  var subContainer = document.getElementById("settingsSidebarSub");
  if (subContainer) subContainer.classList.add("open");
  var toggleBtn = document.getElementById("settingsSidebarToggle");
  if (toggleBtn) toggleBtn.classList.remove("collapsed");
  document.querySelectorAll(".sidebar-sub-link[data-settings-tab]").forEach(function (a) {
    a.classList.toggle("active", a.dataset.settingsTab === name);
  });
  // Update URL hash without scrolling
  try {
    history.replaceState(null, "", "#" + name);
    localStorage.setItem("settingsActiveTab", name);
  } catch (e) { }
}

(function restoreTab() {
  var hash = "";
  try { hash = (window.location.hash || "").replace("#", "").trim(); } catch (e) { }
  // Read valid tab ids off the DOM instead of a hardcoded list, so a tab a
  // thirdparty registers dynamically (settings_host="settings", see
  // registry.py's resolve_dynamic_tabs()) is restorable via #hash exactly
  // like the built-in ones -- same pattern as integrations.js's
  // restoreIntegTab().
  var validTabs = Array.prototype.map.call(
    document.querySelectorAll("#settingsTabs .settings-tab"),
    function (btn) { return btn.dataset.tab; }
  );
  var tab = (hash && validTabs.indexOf(hash) !== -1) ? hash : "overview";
  if (validTabs.indexOf(tab) === -1) tab = "overview";
  switchTab(tab);
})();

// ─── Deep link from the Modulmanager ("Open module" button) ───────────────
// Mirrors integrations.js's openDeepLinkedThirdpartyCard(): extensions.html
// links here as .../settings?open=<item_id>#<tab> for any thirdparty
// registered with settings_host="settings".
(function openDeepLinkedThirdpartyCard() {
  var openId = "";
  try { openId = new URLSearchParams(window.location.search).get("open") || ""; } catch (e) {}
  if (!openId) return;
  document.addEventListener("DOMContentLoaded", function () {
    setTimeout(function () {
      var card = document.getElementById("integCard-" + openId);
      if (!card) return;
      card.classList.remove("collapsed");
      try { localStorage.setItem("integCollapsed_" + openId, "0"); } catch (e) {}
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("integ-card-highlight");
      setTimeout(function () { card.classList.remove("integ-card-highlight"); }, 2200);
    }, 60);
  });
})();

// ─── Element references ─────────────────────────────────────────────────────

const downloadPathInput = document.getElementById("downloadPath");
const langSeparationCb = document.getElementById("langSeparation");
const disableEnglishSubCb = document.getElementById("disableEnglishSub");
const filmpalastSubfolderCb = document.getElementById("filmpalastSubfolder");
const syncScheduleSelect         = document.getElementById("syncSchedule");
const syncLanguageSelect         = document.getElementById("syncLanguage");
const syncProviderSelect         = document.getElementById("syncProvider");
const syncPathUnavailableSelect  = document.getElementById("syncPathUnavailableAction");
const historyRetentionSelect     = document.getElementById("historyRetention");
const syncModeSelect             = document.getElementById("syncMode");
const syncIntervalField          = document.getElementById("syncIntervalField");
const syncWeeklyBlock            = document.getElementById("syncWeeklyBlock");
const syncDaysToggles            = document.getElementById("syncDaysToggles");
const syncTimesList              = document.getElementById("syncTimesList");

// ─── Load all settings ──────────────────────────────────────────────────────

async function loadSettings() {
  try {
    const resp = await fetch("/api/settings?_=" + Date.now()); //Prohibit Browser Caching because of "new URL"(Yes there was in issue during development)
    const data = await resp.json();

    // Downloads tab
    if (downloadPathInput) downloadPathInput.value = data.download_path || "";
    if (langSeparationCb) langSeparationCb.checked = data.lang_separation === "1";
    if (disableEnglishSubCb) disableEnglishSubCb.checked = data.disable_english_sub === "1";
    if (filmpalastSubfolderCb) filmpalastSubfolderCb.checked = data.movie_subfolder === "1" || data.filmpalast_movie_subfolder === "1";

    const dlLangEl = document.getElementById("downloadLanguage");
    if (dlLangEl && data.download_language) dlLangEl.value = data.download_language;

    const dlProvEl = document.getElementById("downloadProvider");
    if (dlProvEl && data.download_provider) dlProvEl.value = data.download_provider;

    const nmTplEl = document.getElementById("namingTemplate");
    if (nmTplEl) nmTplEl.value = data.naming_template || "{title} - S{season:02d}E{episode:02d}";

    const rateLimitEl = document.getElementById("downloadRateLimit");
    if (rateLimitEl && data.download_rate_limit !== undefined) rateLimitEl.value = data.download_rate_limit;
    const winEnabledEl = document.getElementById("downloadWindowEnabled");
    if (winEnabledEl) winEnabledEl.checked = data.download_window_enabled === "1";
    const winStartEl = document.getElementById("downloadWindowStart");
    if (winStartEl) {
      if (data.download_window_start) winStartEl.value = data.download_window_start;
      createCustomTimePicker(winStartEl);
      if (winStartEl.syncCustomPicker) winStartEl.syncCustomPicker();
    }
    const winEndEl = document.getElementById("downloadWindowEnd");
    if (winEndEl) {
      if (data.download_window_end) winEndEl.value = data.download_window_end;
      createCustomTimePicker(winEndEl);
      if (winEndEl.syncCustomPicker) winEndEl.syncCustomPicker();
    }
    const timeFormatEl = document.getElementById("timeFormatSetting");
    if (timeFormatEl) {
      timeFormatEl.value = localStorage.getItem("timeFormatSetting") || "24h";
    }
    updateDownloadWindowDisabledState();

    // Auto-Sync tab
    if (syncScheduleSelect && data.sync_schedule) syncScheduleSelect.value = data.sync_schedule;
    if (historyRetentionSelect && data.history_retention_days != null) historyRetentionSelect.value = String(data.history_retention_days);
    if (syncModeSelect) syncModeSelect.value = data.sync_mode === "weekly" ? "weekly" : "interval";
    _renderSyncDays(data.sync_days || "0,1,2,3,4,5,6");
    _renderSyncTimes(data.sync_times || "06:00");
    _applySyncModeUI();

    // Updates tab — automatic update schedule
    _renderAutoUpdateDays(data.auto_update_days || "0,1,2,3,4,5,6");
    const _auEnabled = document.getElementById("autoUpdateEnabled");
    if (_auEnabled) _auEnabled.checked = data.auto_update_enabled === "1";
    const _auTime = document.getElementById("autoUpdateTime");
    if (_auTime && data.auto_update_time) _auTime.value = data.auto_update_time;
    const _auBlock = document.getElementById("autoUpdateBlock");
    if (_auBlock) _auBlock.style.display = (data.auto_update_enabled === "1") ? "" : "none";

    const isLangSep = data.lang_separation === "1";
    let currentSyncLang = data.sync_language;
    if (currentSyncLang === "All Languages" && !isLangSep) currentSyncLang = "German Dub";
    updateSyncLanguageDropdown(isLangSep, currentSyncLang);
    if (syncProviderSelect && data.sync_provider) syncProviderSelect.value = data.sync_provider;
    if (syncPathUnavailableSelect && data.sync_path_unavailable_action) syncPathUnavailableSelect.value = data.sync_path_unavailable_action;
    const syncErrorRetriesEl = document.getElementById("syncErrorRetries");
    if (syncErrorRetriesEl && data.sync_error_retries !== undefined) syncErrorRetriesEl.value = data.sync_error_retries;
    const syncErrorRetryTimeEl = document.getElementById("syncErrorRetryTime");
    if (syncErrorRetryTimeEl && data.sync_error_retry_time) syncErrorRetryTimeEl.value = data.sync_error_retry_time;
    const syncAdaptiveEnabledEl = document.getElementById("syncAdaptiveEnabled");
    if (syncAdaptiveEnabledEl) syncAdaptiveEnabledEl.checked = data.sync_adaptive_enabled === "1";
    const syncAdaptivePauseEl = document.getElementById("syncAdaptivePauseAfter");
    if (syncAdaptivePauseEl && data.sync_adaptive_pause_after) syncAdaptivePauseEl.value = data.sync_adaptive_pause_after;
    const syncAdaptiveRetryValueEl = document.getElementById("syncAdaptiveRetryValue");
    if (syncAdaptiveRetryValueEl && data.sync_adaptive_retry_value !== undefined) syncAdaptiveRetryValueEl.value = data.sync_adaptive_retry_value;
    const syncAdaptiveRetryUnitEl = document.getElementById("syncAdaptiveRetryUnit");
    if (syncAdaptiveRetryUnitEl && data.sync_adaptive_retry_unit) syncAdaptiveRetryUnitEl.value = data.sync_adaptive_retry_unit;
    updateAdaptiveSyncDisabledState();

    // Netzwerk tab
    const dnsModeEl = document.getElementById("dnsMode");
    const dnsServerEl = document.getElementById("dnsServer");
    if (dnsModeEl) { dnsModeEl.value = data.dns_mode || "system"; onDnsModeChange(); }
    if (dnsServerEl) dnsServerEl.value = data.dns_server || "";

    const _setCb = (id, on) => { const el = document.getElementById(id); if (el) el.checked = !!on; };
    _setCb("browserPersistentProfile", data.browser_persistent_profile === "1");
    _setCb("captchaAdblock",        (data.captcha_adblock ?? "1") === "1");
    _setCb("captchaAdtabGuard",     (data.captcha_adtab_guard ?? "1") === "1");
    _setCb("captchaOverlayRemoval", (data.captcha_overlay_removal ?? "1") === "1");
    _setCb("captchaUaSync",         (data.captcha_ua_sync ?? "1") === "1");
    _setCb("captchaWebglSpoof",     (data.captcha_webgl_spoof ?? "0") === "1");
    _setCb("captchaManual",         (data.captcha_manual ?? "0") === "1");
    _setCb("captchaVisible",        (data.captcha_visible ?? "0") === "1");
    const captchaTimeoutEl = document.getElementById("captchaTimeout");
    if (captchaTimeoutEl) captchaTimeoutEl.value = data.captcha_timeout || "";

    const webBaseUrlEl = document.getElementById("webBaseUrl");
    if (webBaseUrlEl) webBaseUrlEl.value = data.web_base_url || "";

    const debugModeEl = document.getElementById("debugMode");
    if (debugModeEl) {
      const forced = data.debug_forced === "1";
      debugModeEl.checked = data.debug_mode === "1" || forced;
      // When started with --debug the toggle is locked on (greyed out).
      debugModeEl.disabled = forced;
      const forcedHint = document.getElementById("debugForcedHint");
      if (forcedHint) forcedHint.style.display = forced ? "block" : "none";
      const row = debugModeEl.closest(".settings-checkbox-row");
      if (row) row.style.opacity = forced ? "0.6" : "";
    }
    const mediaStatsEl = document.getElementById("mediaStatsEnabled");
    if (mediaStatsEl) mediaStatsEl.checked = data.media_stats_enabled === "1";

    const webConsoleEl = document.getElementById("webConsole");
    if (webConsoleEl) {
      const on = data.web_console === "1";
      webConsoleEl.checked = on;
      // If the Web Console was already enabled (e.g. during startup), show it immediately.
      setWebConsoleVisible(on);
      if (on) startWebConsole();
    }


    // Startup & Tray
    const trayModeEl = document.getElementById("trayMode");
    if (trayModeEl) trayModeEl.checked = data.tray_mode === "1";
    const autostartEl = document.getElementById("autostartEnabled");
    if (autostartEl) autostartEl.checked = data.autostart_enabled === "1";
    const openBrowserEl = document.getElementById("openBrowserOnStartup");
    if (openBrowserEl) openBrowserEl.checked = data.open_browser_on_startup !== "0"; // Default true
    
    if (data.is_docker) {
      if (trayModeEl) trayModeEl.disabled = true;
      if (autostartEl) autostartEl.disabled = true;
      if (openBrowserEl) openBrowserEl.disabled = true;
      const dhint = document.getElementById("dockerStartupHint");
      if (dhint) dhint.style.display = "block";
      const trayOpts = document.getElementById("startupTrayOptions");
      if (trayOpts) trayOpts.style.opacity = "0.6";
    }

    // Design tab - Extended Settings
    _loadDesignCheckboxes();

    // Sources: order, section order, enabled state, search scope
    _loadSourceSettings(data.sources || {});

    // Hoster order + automatic provider fallback
    _loadProviderSettings(data.providers || {});

    // Per-site domain fallback (mirrors)
    _loadMirrorSettings(data.mirrors || {});

  } catch (e) {
    showToast("Einstellungen konnten nicht geladen werden: " + e.message);
  }
  loadApiKey();
  loadSsoSettings();
}

// ─── Downloads tab ──────────────────────────────────────────────────────────

async function saveLangSeparation() {
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        download_path: downloadPathInput ? downloadPathInput.value.trim() : undefined,
        lang_separation: langSeparationCb ? langSeparationCb.checked : false,
      }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast("Sprachentrennung " + (langSeparationCb && langSeparationCb.checked ? t("aktiviert","activated") : t("deaktiviert","deactivated")));
    const isLangSep = langSeparationCb ? langSeparationCb.checked : false;
    let currentSyncLang = syncLanguageSelect ? syncLanguageSelect.value : null;
    if (!isLangSep && currentSyncLang === "All Languages") {
      currentSyncLang = "German Dub";
      updateSyncLanguageDropdown(false, currentSyncLang);
      saveSyncDefaults();
    } else {
      updateSyncLanguageDropdown(isLangSep, currentSyncLang);
    }
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

function updateSyncLanguageDropdown(isLangSep, currentValue) {
  if (!syncLanguageSelect) return;
  syncLanguageSelect.innerHTML = "";
  if (isLangSep) {
    const opt = document.createElement("option");
    opt.value = "All Languages";
    opt.textContent = t("Alle Sprachen","All Languages");
    syncLanguageSelect.appendChild(opt);
  }
  ["German Dub", "English Sub", "German Sub", "English Dub", "English Dub (German Sub)"].forEach(function (l) {
    const opt = document.createElement("option");
    opt.value = l;
    opt.textContent = l;
    syncLanguageSelect.appendChild(opt);
  });
  if (currentValue) syncLanguageSelect.value = currentValue;
}

async function saveDisableEnglishSub() {
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ disable_english_sub: disableEnglishSubCb ? disableEnglishSubCb.checked : false }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Englische Untertitel-Downloads ", "English subtitle downloads ") +
      (disableEnglishSubCb && disableEnglishSubCb.checked ? t("deaktiviert","deactivated") : t("aktiviert","activated")));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

async function saveMovieSubfolder() {
  try {
    const checked = filmpalastSubfolderCb ? filmpalastSubfolderCb.checked : false;
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filmpalast_movie_subfolder: checked,
        movie_subfolder: checked
      }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Film-Unterordner ", "Movie subfolder ") + (checked ? t("aktiviert","activated") : t("deaktiviert","deactivated")));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}
window.saveFilmpalastSubfolder = saveMovieSubfolder;

async function saveDownloadPath() {
  const download_path = downloadPathInput ? downloadPathInput.value.trim() : "";
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ download_path }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Download-Pfad gespeichert","Download path saved"));
  } catch (e) {
    showToast(t("Einstellungen konnten nicht gespeichert werden: ", "Settings could not be saved: ") + e.message);
  }
}

async function saveDownloadLanguage() {
  const el = document.getElementById("downloadLanguage");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ download_language: el.value }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Standardsprache gespeichert","Default language saved"));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

async function saveDownloadProvider() {
  const el = document.getElementById("downloadProvider");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ download_provider: el.value }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Standardanbieter gespeichert","Default provider saved"));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

async function saveNamingTemplate() {
  const el = document.getElementById("namingTemplate");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ naming_template: el.value.trim() }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Namens-Template gespeichert","Naming template saved"));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

function resetNamingTemplate() {
  const el = document.getElementById("namingTemplate");
  if (el) el.value = "{title} - S{season:02d}E{episode:02d}";
  saveNamingTemplate();
}

async function saveDownloadRateLimit() {
  const el = document.getElementById("downloadRateLimit");
  if (!el) return;
  let val = parseInt(el.value, 10);
  if (isNaN(val) || val < 0) val = 0;
  el.value = val;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ download_rate_limit: val }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Bandbreiten-Limit gespeichert","Bandwidth limit saved"));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

function updateDownloadWindowDisabledState() {
  const enabledEl = document.getElementById("downloadWindowEnabled");
  if (!enabledEl) return;
  const off = !enabledEl.checked;
  ["downloadWindowStart", "downloadWindowEnd"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) {
      el.disabled = off;
      if (el.customWrapper) {
        el.customWrapper.querySelectorAll("select").forEach((sel) => {
          sel.disabled = off;
        });
      }
    }
  });
}

async function saveDownloadWindow() {
  const enabledEl = document.getElementById("downloadWindowEnabled");
  const startEl = document.getElementById("downloadWindowStart");
  const endEl = document.getElementById("downloadWindowEnd");
  if (!enabledEl || !startEl || !endEl) return;
  updateDownloadWindowDisabledState();
  const payload = { download_window_enabled: enabledEl.checked };
  if (startEl.value) payload.download_window_start = startEl.value;
  if (endEl.value) payload.download_window_end = endEl.value;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Download-Zeitfenster gespeichert","Download time window saved"));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

// ─── Auto-Sync tab ──────────────────────────────────────────────────────────

async function saveSyncSchedule() {
  if (!syncScheduleSelect) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sync_schedule: syncScheduleSelect.value }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Auto-Sync-Zeitplan gespeichert","Auto-sync schedule saved"));
    else showToast(t("Zeitplan konnte nicht gespeichert werden","Could not save schedule"));
  } catch (e) {
    showToast(t("Zeitplan konnte nicht gespeichert werden: ", "Could not save schedule: ") + e.message);
  }
}

async function saveHistoryRetention() {
  if (!historyRetentionSelect) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ history_retention_days: parseInt(historyRetentionSelect.value, 10) }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Aufbewahrung gespeichert", "Retention saved"));
    else showToast(data.error || t("Konnte nicht gespeichert werden", "Could not save"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden", "Could not save"));
  }
}

// ─── Auto-Sync weekly schedule ───────────────────────────────────────────────
function _weekdayLabels() {
  return [
    t("Mo", "Mon"), t("Di", "Tue"), t("Mi", "Wed"), t("Do", "Thu"),
    t("Fr", "Fri"), t("Sa", "Sat"), t("So", "Sun"),
  ];
}

function _renderSyncDays(csv) {
  if (!syncDaysToggles) return;
  const active = new Set(
    String(csv || "").split(",").map((x) => parseInt(x.trim(), 10)).filter((x) => !isNaN(x)),
  );
  const labels = _weekdayLabels();
  syncDaysToggles.innerHTML = "";
  labels.forEach((lbl, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "weekday-toggle" + (active.has(i) ? " active" : "");
    b.dataset.day = i;
    b.textContent = lbl;
    b.addEventListener("click", () => {
      b.classList.toggle("active");
      saveSyncWeekly();
    });
    syncDaysToggles.appendChild(b);
  });
}

function _getSelectedDays() {
  if (!syncDaysToggles) return "";
  return Array.from(syncDaysToggles.querySelectorAll(".weekday-toggle.active"))
    .map((b) => b.dataset.day)
    .join(",");
}

function _addTimeRow(value) {
  const row = document.createElement("div");
  row.className = "sync-time-row";
  const input = document.createElement("input");
  input.type = "time";
  input.value = value || "06:00";
  input.addEventListener("change", saveSyncWeekly);
  const rm = document.createElement("button");
  rm.type = "button";
  rm.className = "sync-time-remove";
  rm.textContent = "✕";
  rm.title = t("Entfernen", "Remove");
  rm.addEventListener("click", () => {
    row.remove();
    if (syncTimesList && !syncTimesList.children.length) _addTimeRow("06:00");
    saveSyncWeekly();
  });
  row.appendChild(input);
  row.appendChild(rm);
  syncTimesList.appendChild(row);
  if (typeof createCustomTimePicker === "function") {
    createCustomTimePicker(input);
  }
}

function _renderSyncTimes(csv) {
  if (!syncTimesList) return;
  syncTimesList.innerHTML = "";
  const times = String(csv || "").split(",").map((x) => x.trim()).filter(Boolean);
  if (!times.length) times.push("06:00");
  times.forEach((tm) => _addTimeRow(tm));
}

function addSyncTime() {
  _addTimeRow("12:00");
  saveSyncWeekly();
}

function _getTimes() {
  if (!syncTimesList) return "";
  return Array.from(syncTimesList.querySelectorAll("input[type=time]"))
    .map((i) => i.value)
    .filter(Boolean)
    .join(",");
}

function _applySyncModeUI() {
  const weekly = syncModeSelect && syncModeSelect.value === "weekly";
  if (syncIntervalField) syncIntervalField.style.display = weekly ? "none" : "";
  if (syncWeeklyBlock) syncWeeklyBlock.style.display = weekly ? "" : "none";
}

async function onSyncModeChange() {
  _applySyncModeUI();
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sync_mode: syncModeSelect.value }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Modus gespeichert", "Mode saved"));
    else showToast(data.error || t("Konnte nicht gespeichert werden", "Could not save"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden", "Could not save"));
  }
}

async function saveSyncWeekly() {
  const days = _getSelectedDays();
  const times = _getTimes();
  if (!days) { showToast(t("Mindestens einen Wochentag wählen", "Select at least one weekday")); return; }
  if (!times) { showToast(t("Mindestens eine Uhrzeit angeben", "Provide at least one time")); return; }
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sync_days: days, sync_times: times }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Wochenplan gespeichert", "Weekly schedule saved"));
    else showToast(data.error || t("Konnte nicht gespeichert werden", "Could not save"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden", "Could not save"));
  }
}

async function saveSyncDefaults() {
  const body = {};
  if (syncLanguageSelect) body.sync_language = syncLanguageSelect.value;
  if (syncProviderSelect) body.sync_provider = syncProviderSelect.value;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Auto-Sync-Standards gespeichert","Auto-sync defaults saved"));
    else showToast(t("Standards konnten nicht gespeichert werden","Could not save defaults"));
  } catch (e) {
    showToast(t("Standards konnten nicht gespeichert werden: ", "Could not save defaults: ") + e.message);
  }
}

async function saveSyncPathAction() {
  if (!syncPathUnavailableSelect) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sync_path_unavailable_action: syncPathUnavailableSelect.value }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Pfad-offline-Verhalten gespeichert","Path-offline-behavior saved"));
    else showToast(data.error || t("Konnte nicht gespeichert werden","Could not save"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden: ", "Could not save: ") + e.message);
  }
}

async function saveSyncErrorRetries() {
  const el = document.getElementById("syncErrorRetries");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sync_error_retries: parseInt(el.value, 10) }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Fehler-Wiederholungen gespeichert","Error retries saved"));
    else showToast(data.error || t("Konnte nicht gespeichert werden","Could not save"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden: ", "Could not save: ") + e.message);
  }
}

async function saveSyncErrorRetryTime() {
  const el = document.getElementById("syncErrorRetryTime");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sync_error_retry_time: el.value }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Wiederholungs-Zeit gespeichert","Retry time saved"));
    else showToast(data.error || t("Konnte nicht gespeichert werden","Could not save"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden: ", "Could not save: ") + e.message);
  }
}

function updateAdaptiveSyncDisabledState() {
  const enabledEl = document.getElementById("syncAdaptiveEnabled");
  if (!enabledEl) return;
  const off = !enabledEl.checked;
  ["syncAdaptivePauseAfter", "syncAdaptiveRetryValue", "syncAdaptiveRetryUnit"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.disabled = off;
  });
}

async function saveSyncAdaptiveEnabled() {
  const el = document.getElementById("syncAdaptiveEnabled");
  if (!el) return;
  updateAdaptiveSyncDisabledState();
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sync_adaptive_enabled: el.checked }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Adaptive Auto-Sync gespeichert","Adaptive Auto-Sync saved"));
    else showToast(data.error || t("Konnte nicht gespeichert werden","Could not save"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden: ", "Could not save: ") + e.message);
  }
}

async function saveSyncAdaptivePauseAfter() {
  const el = document.getElementById("syncAdaptivePauseAfter");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sync_adaptive_pause_after: el.value }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Pause-Intervall gespeichert","Pause interval saved"));
    else showToast(data.error || t("Konnte nicht gespeichert werden","Could not save"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden: ", "Could not save: ") + e.message);
  }
}

async function saveSyncAdaptiveRetry() {
  const valEl = document.getElementById("syncAdaptiveRetryValue");
  const unitEl = document.getElementById("syncAdaptiveRetryUnit");
  if (!valEl || !unitEl) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sync_adaptive_retry_value: parseInt(valEl.value, 10),
        sync_adaptive_retry_unit: unitEl.value,
      }),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Wiederholungs-Intervall gespeichert","Retry interval saved"));
    else showToast(data.error || t("Konnte nicht gespeichert werden","Could not save"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden: ", "Could not save: ") + e.message);
  }
}

// ─── Netzwerk tab ───────────────────────────────────────────────────────────

async function saveWebBaseUrl() {
  const el = document.getElementById("webBaseUrl");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ web_base_url: el.value.trim() }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Basis-URL gespeichert — Neustart erforderlich","Base URL saved — restart required"));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

async function saveMediaStatsEnabled() {
  const el = document.getElementById("mediaStatsEnabled");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ media_stats_enabled: el.checked }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Medien-Statistik " + (el.checked ? "aktiviert" : "deaktiviert"), "Media statistics " + (el.checked ? "enabled" : "disabled")));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

async function saveDebugMode() {
  const el = document.getElementById("debugMode");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ debug_mode: el.checked }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Debug-Modus " + (el.checked ? "aktiviert" : "deaktiviert"), "Debug mode " + (el.checked ? "enabled" : "disabled")));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

async function saveWebConsole() {
  const el = document.getElementById("webConsole");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ web_console: el.checked }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    setWebConsoleVisible(el.checked);
    if (el.checked) startWebConsole();
    else stopWebConsole();
    showToast(t("Web-Konsole " + (el.checked ? "aktiviert" : "deaktiviert"), "Web-Console " + (el.checked ? "enabled" : "disabled")));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}

async function saveStartupSettings() {
  const trayModeEl = document.getElementById("trayMode");
  const autostartEl = document.getElementById("autostartEnabled");
  const openBrowserEl = document.getElementById("openBrowserOnStartup");
  
  const payload = {};
  if (trayModeEl) payload.tray_mode = trayModeEl.checked;
  if (autostartEl) payload.autostart_enabled = autostartEl.checked;
  if (openBrowserEl) payload.open_browser_on_startup = openBrowserEl.checked;
  
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Startup-Einstellungen gespeichert", "Startup settings saved"));
  } catch (e) {
    showToast(t("Einstellung konnte nicht gespeichert werden: ", "Setting could not be saved: ") + e.message);
  }
}


// ─── Web Console (read-only live console mirror) ──────────────────────────────

let _webConsoleTimer = null;
let _webConsoleSeq = 0;
let _webConsolePolling = false;

function setWebConsoleVisible(visible) {
  const wrap = document.getElementById("webConsoleWrap");
  if (wrap) wrap.style.display = visible ? "block" : "none";
}

// Escape HTML so console text can never inject markup.
function _wcEscape(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// Convert a single line containing ANSI SGR escape codes to safe HTML.
function _wcAnsiToHtml(line) {
  // Strip non-color escape sequences (cursor moves etc.), keep SGR (m).
  let out = "";
  let open = 0;
  const classes = [];
  const re = /\x1b\[([0-9;]*)m/g;
  let last = 0;
  let m;
  const applyText = (txt) => {
    if (!txt) return;
    out += _wcEscape(txt);
  };
  const closeSpans = () => { while (open > 0) { out += "</span>"; open--; } };
  while ((m = re.exec(line)) !== null) {
    applyText(line.slice(last, m.index));
    last = re.lastIndex;
    const codes = m[1].split(";").filter((x) => x !== "");
    if (codes.length === 0 || codes.includes("0")) {
      closeSpans();
      continue;
    }
    const cls = [];
    for (const code of codes) {
      if (code === "1") cls.push("ansi-bold");
      else if (code === "41") cls.push("ansi-bg-41");
      else if (/^(3[0-7]|9[0-7])$/.test(code)) cls.push("ansi-" + code);
    }
    if (cls.length) { out += '<span class="' + cls.join(" ") + '">'; open++; }
  }
  applyText(line.slice(last));
  closeSpans();
  // Remove any remaining/unsupported escape sequences.
  return out.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "");
}

function _wcRenderLine(text) {
  const div = document.createElement("div");
  div.className = "web-console-line";
  div.innerHTML = _wcAnsiToHtml(text) || "&nbsp;";
  return div;
}

function _wcAtBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < 40;
}

async function _webConsolePoll() {
  const out = document.getElementById("webConsoleOutput");
  const statusEl = document.getElementById("webConsoleStatus");
  if (!out) return;
  try {
    const resp = await fetch("/api/console?after=" + _webConsoleSeq);
    const data = await resp.json();
    if (data.enabled === false) { stopWebConsole(); setWebConsoleVisible(false); return; }

    // Detect a server-side buffer reset/restart (our cursor is ahead of buffer).
    if (typeof data.first_seq === "number" && _webConsoleSeq > data.seq) {
      out.innerHTML = "";
      _webConsoleSeq = 0;
    }

    const autoEl = document.getElementById("webConsoleAutoscroll");
    const autoscroll = !autoEl || autoEl.checked;
    const stick = autoscroll && _wcAtBottom(out);

    // Remove any previously rendered partial (transient) line.
    const oldPartial = out.querySelector(".web-console-partial");
    if (oldPartial) oldPartial.remove();

    if (Array.isArray(data.lines) && data.lines.length) {
      const frag = document.createDocumentFragment();
      for (const ln of data.lines) {
        frag.appendChild(_wcRenderLine(ln.text));
        if (typeof ln.seq === "number") _webConsoleSeq = ln.seq;
      }
      out.appendChild(frag);
    }
    if (data.partial) {
      const p = _wcRenderLine(data.partial);
      p.classList.add("web-console-partial");
      out.appendChild(p);
    }
    if (stick || autoscroll) out.scrollTop = out.scrollHeight;
    if (statusEl) statusEl.textContent = "";
  } catch (e) {
    if (statusEl) statusEl.textContent = t("Verbindung verloren …", "Connection lost …");
  }
}

function startWebConsole() {
  if (_webConsolePolling) return;
  _webConsolePolling = true;
  const out = document.getElementById("webConsoleOutput");
  if (out) { out.innerHTML = ""; }
  _webConsoleSeq = 0;
  _webConsolePoll();
  _webConsoleTimer = setInterval(_webConsolePoll, 1500);
}

function stopWebConsole() {
  _webConsolePolling = false;
  if (_webConsoleTimer) { clearInterval(_webConsoleTimer); _webConsoleTimer = null; }
}

// ─── DNS ────────────────────────────────────────────────────────────────────

function dnsTestAppendErrorBtn(container, errorText) {
  const btn = document.createElement("button");
  btn.className = "dns-test-err-btn";
  btn.textContent = t("Fehler anzeigen","Show error");
  btn.onclick = function () {
    const existing = container.querySelector(".dns-test-err-detail");
    if (existing) {
      existing.remove();
      btn.textContent = t("Fehler anzeigen","Show error");
    } else {
      const detail = document.createElement("div");
      detail.className = "dns-test-err-detail";
      detail.textContent = errorText;
      container.appendChild(detail);
      btn.textContent = t("Fehler ausblenden","Hide error");
    }
  };
  container.appendChild(btn);
}

function toggleDnsTestPanel() {
  const panel = document.getElementById("dnsTestPanel");
  if (!panel) return;
  const opening = panel.style.display === "none";
  panel.style.display = opening ? "block" : "none";
  if (opening) runDnsTest();
}

async function runDnsTest() {
  const btn = document.getElementById("dnsTestRunBtn");
  const statusEl = document.getElementById("dnsTestStatus");
  if (btn) { btn.disabled = true; btn.textContent = t("⏳ Teste…","⏳ Testing..."); }
  if (statusEl) statusEl.innerHTML = t('<span class="dns-test-loading">⏳ Lädt…</span>','<span class="dns-test-loading">⏳ Loading...</span>');
  for (const id of ["dnsTestRowAniWorld", "dnsTestRowSTO", "dnsTestRowFilmpalast", "dnsTestRowMegaKino", "dnsTestRowHanime"]) {
    const row = document.getElementById(id);
    if (row) {
      const res = row.querySelector(".dns-test-site-result");
      if (res) { res.className = "dns-test-site-result dns-test-loading"; res.textContent = t("⏳ Teste…","⏳ Testing..."); }
    }
  }
  try {
    const resp = await fetch("/api/settings/dns/test");
    const data = await resp.json();
    if (statusEl) {
      const mode = data.dns_mode || "system";
      const active = data.dns_active_server;
      const modeLabel = { system: "System-DNS", cloudflare: "Cloudflare (1.1.1.1)", google: "Google (8.8.8.8)", quad9: "Quad9 (9.9.9.9)", custom: t("Benutzerdefiniert","Custom") }[mode] || mode;
      if (mode === "system") {
        statusEl.innerHTML = t('<span class="dns-test-ok">✓ System-DNS aktiv (kein eigener DNS konfiguriert)</span>','<span class="dns-test-ok">✓ System DNS active (no own DNS configured)</span>');
      } else if (active) {
        statusEl.innerHTML = t('<span class="dns-test-ok">✓ ' + modeLabel + ' aktiv — Server: ' + active + '</span>','<span class="dns-test-ok">✓ ' + modeLabel + ' active — Server: ' + active + '</span>');
      } else {
        statusEl.innerHTML = t('<span class="dns-test-warn">⚠ Gespeicherter Modus: ' + modeLabel + ' — aber kein Server aktiv. Einstellungen erneut speichern?</span>','<span class="dns-test-warn">⚠ Saved mode: ' + modeLabel + ' — but no server active. Save settings again?</span>');
      }
    }
    const siteMap = { AniWorld: "dnsTestRowAniWorld", SerienStream: "dnsTestRowSTO", FilmPalast: "dnsTestRowFilmpalast", MegaKino: "dnsTestRowMegaKino", hanime: "dnsTestRowHanime" };
    for (const [label, rowId] of Object.entries(siteMap)) {
      const row = document.getElementById(rowId);
      if (!row) continue;
      const resEl = row.querySelector(".dns-test-site-result");
      if (!resEl) continue;
      const site = data.sites?.[label];
      if (!site) { resEl.className = "dns-test-site-result dns-test-warn"; resEl.textContent = t("— Keine Daten","— No data"); continue; }
      const ip = site.ip ? " (" + site.ip + (site.ip_provider ? " · " + site.ip_provider : "") + ")" : "";
      resEl.innerHTML = "";
      const txt = document.createElement("span");
      resEl.appendChild(txt);
      if (site.http_ok && site.site_verified) {
        resEl.className = "dns-test-site-result dns-test-ok";
        txt.textContent = t("✓ Erreichbar & verifiziert","✓ Reachable & verified") + ip;
      } else if (site.http_ok && site.blocked) {
        resEl.className = "dns-test-site-result dns-test-fail";
        txt.textContent = t("✗ Sperr-/Blockseite erkannt — nicht die echte Seite","✗ Block/ISP page detected — not the real site") + ip;
      } else if (site.http_ok && !site.site_verified) {
        resEl.className = "dns-test-site-result dns-test-warn";
        txt.textContent = t("⚠ Erreichbar","⚠ Reachable") + ip + t(", aber echte Seite nicht bestätigt (evtl. Schutz-/Challenge-Seite)"," , but real site not confirmed (possibly protection/challenge page)");
      } else if (site.socket_ok) {
        resEl.className = "dns-test-site-result dns-test-warn";
        txt.textContent = t("⚠ DNS aufgelöst","⚠ DNS resolved") + ip + t(", aber HTTP fehlgeschlagen"," , but HTTP failed");
        if (site.http_error) dnsTestAppendErrorBtn(resEl, site.http_error);
      } else {
        resEl.className = "dns-test-site-result dns-test-fail";
        txt.textContent = t("✗ Nicht erreichbar","✗ Not reachable");
        const errMsg = site.socket_error || site.http_error;
        if (errMsg) dnsTestAppendErrorBtn(resEl, errMsg);
      }
    }
  } catch (e) {
    if (statusEl) statusEl.innerHTML = t('<span class="dns-test-fail">✗ Fehler: ' + e.message + '</span>','<span class="dns-test-fail">✗ Error: ' + e.message + '</span>');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t("↺ Erneut testen","↺ Test again"); }
  }
}

function onDnsModeChange() {
  const mode = document.getElementById("dnsMode")?.value;
  const field = document.getElementById("dnsCustomField");
  const statusEl = document.getElementById("dnsStatus");
  if (field) field.style.display = mode === "custom" ? "" : "none";
  if (statusEl) statusEl.style.display = "none";
}

async function saveDnsSettings() {
  const mode = document.getElementById("dnsMode")?.value || "system";
  const server = (document.getElementById("dnsServer")?.value || "").trim();
  if (mode === "custom" && !server) { showToast(t("Bitte einen DNS-Server eingeben", "Please enter a DNS server"), "warning"); return; }
  try {
    const resp = await fetch("/api/settings/dns", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ dns_mode: mode, dns_server: server }),
    });
    const data = await resp.json();
    if (data.ok) {
      const modeLabel = { system: "System-DNS", cloudflare: "Cloudflare (1.1.1.1)", google: "Google (8.8.8.8)", quad9: "Quad9 (9.9.9.9)", custom: "Benutzerdefiniert" }[mode] || mode;
      showToast(mode === "system" ? t("DNS zurückgesetzt — System-DNS aktiv","DNS reset — System DNS active") : t("DNS gespeichert — ", "DNS saved — ") + modeLabel + " aktiv", "success");
    } else {
      showToast(data.error || t("Fehler beim Speichern","Error saving"), "error");
    }
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message, "error");
  }
}

// ─── Captcha / Browser tab ──────────────────────────────────────────────────

async function saveCaptchaSettings() {
  const g = (id) => document.getElementById(id);
  const cb = (id) => !!(g(id) && g(id).checked);
  const payload = {
    persistent_profile: cb("browserPersistentProfile"),
    manual:             cb("captchaManual"),
    visible:            cb("captchaVisible"),
    adblock:            cb("captchaAdblock"),
    adtab_guard:        cb("captchaAdtabGuard"),
    overlay_removal:    cb("captchaOverlayRemoval"),
    ua_sync:            cb("captchaUaSync"),
    webgl_spoof:        cb("captchaWebglSpoof"),
    timeout:            ((g("captchaTimeout") && g("captchaTimeout").value) || "").trim(),
  };
  try {
    const resp = await fetch("/api/settings/browser", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.ok) {
      showToast(t("Captcha-Einstellungen gespeichert — greifen beim nächsten Captcha", "Captcha settings saved — apply on the next captcha"), "success");
    } else {
      showToast(data.error || t("Fehler beim Speichern", "Error saving"), "error");
    }
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message, "error");
  }
}

async function clearBrowserProfile() {
  const ok = (typeof showConfirm === "function")
    ? await showConfirm(
        t("Das gespeicherte Browser-Profil (Cookies, cf_clearance, Fingerprint) wird gelöscht. Beim nächsten Captcha wird es neu angelegt.",
          "The saved browser profile (cookies, cf_clearance, fingerprint) will be deleted. It is recreated on the next captcha."),
        t("Profil löschen", "Delete profile"),
        t("Browser-Profil löschen?", "Delete browser profile?"),
        "btn-danger")
    : window.confirm(t("Browser-Profil löschen?", "Delete browser profile?"));
  if (!ok) return;
  try {
    const resp = await fetch("/api/browser/profile/clear", { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      showToast(t("Browser-Profil gelöscht", "Browser profile deleted"), "success");
    } else if (data.error === "captcha_running") {
      showToast(t("Gerade läuft ein Captcha — bitte später erneut versuchen", "A captcha is running — please try again later"), "warning");
    } else {
      showToast(data.error || t("Fehler", "Error"), "error");
    }
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message, "error");
  }
}

async function restartApp() {
  const ok = (typeof showConfirm === "function")
    ? await showConfirm(
        t("Die App wird mit denselben Startargumenten neu gestartet (ohne Update). Laufende Downloads werden pausiert und danach fortgesetzt.",
          "The app restarts with the same startup arguments (no update). Running downloads pause and resume afterwards."),
        t("Neu starten", "Restart"),
        t("App neu starten?", "Restart app?"),
        "btn-primary")
    : window.confirm(t("App neu starten?", "Restart app?"));
  if (!ok) return;
  if (window.AniUpdate && window.AniUpdate.startRestart) {
    window.AniUpdate.startRestart();
  } else {
    try {
      await fetch("/api/restart", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      showToast(t("Neustart gestartet…", "Restart started…"), "success");
    } catch (e) {
      showToast(t("Fehler: ", "Error: ") + e.message, "error");
    }
  }
}

// ─── SSO / Authentifizierung tab ────────────────────────────────────────────

async function loadSsoSettings() {
  try {
    const resp = await fetch("/api/settings/sso");
    if (!resp.ok) return;
    const data = await resp.json();

    const ssoEl = document.getElementById("ssoEnabled");
    const forceSsoEl = document.getElementById("forceSso");
    const issuerEl = document.getElementById("oidcIssuerUrl");
    const clientIdEl = document.getElementById("oidcClientId");
    const secretEl = document.getElementById("oidcClientSecret");
    const displayEl = document.getElementById("oidcDisplayName");
    const adminUsEl = document.getElementById("oidcAdminUser");
    const adminSubEl = document.getElementById("oidcAdminSubject");

    if (ssoEl) ssoEl.checked = data.sso_enabled === true || data.sso_enabled === "1";
    if (forceSsoEl) forceSsoEl.checked = data.force_sso === true || data.force_sso === "1";
    if (issuerEl) issuerEl.value = data.oidc_issuer_url || "";
    if (clientIdEl) clientIdEl.value = data.oidc_client_id || "";
    if (secretEl) secretEl.value = data.oidc_client_secret || "";
    if (displayEl) displayEl.value = data.oidc_display_name || "";
    if (adminUsEl) adminUsEl.value = data.oidc_admin_user || "";
    if (adminSubEl) adminSubEl.value = data.oidc_admin_subject || "";

    onSsoToggle();
  } catch (e) {
    // non-critical, SSO may not be configured
  }
}

function onSsoToggle() {
  const ssoEl = document.getElementById("ssoEnabled");
  const ssoFields = document.getElementById("ssoFields");
  if (!ssoFields) return;
  ssoFields.style.display = (ssoEl && ssoEl.checked) ? "" : "none";
}

async function saveSsoSettings() {
  const ssoEl = document.getElementById("ssoEnabled");
  const forceSsoEl = document.getElementById("forceSso");
  const issuerEl = document.getElementById("oidcIssuerUrl");
  const clientIdEl = document.getElementById("oidcClientId");
  const secretEl = document.getElementById("oidcClientSecret");
  const displayEl = document.getElementById("oidcDisplayName");
  const adminUsEl = document.getElementById("oidcAdminUser");
  const adminSubEl = document.getElementById("oidcAdminSubject");

  const body = {
    sso_enabled: ssoEl ? ssoEl.checked : false,
    force_sso: forceSsoEl ? forceSsoEl.checked : false,
    oidc_issuer_url: issuerEl ? issuerEl.value.trim() : "",
    oidc_client_id: clientIdEl ? clientIdEl.value.trim() : "",
    oidc_display_name: displayEl ? displayEl.value.trim() : "",
    oidc_admin_user: adminUsEl ? adminUsEl.value.trim() : "",
    oidc_admin_subject: adminSubEl ? adminSubEl.value.trim() : "",
  };
  // Only include secret if user actually typed something (not the masked placeholder)
  if (secretEl && secretEl.value && secretEl.value !== "***") {
    body.oidc_client_secret = secretEl.value;
  }

  try {
    const resp = await fetch("/api/settings/sso", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error, "error"); return; }
    showToast(t("SSO-Einstellungen gespeichert — Neustart erforderlich", "SSO settings saved — restart required"), "success");
  } catch (e) {
    showToast(t("SSO-Einstellungen konnten nicht gespeichert werden: " + e.message, "SSO settings could not be saved: " + e.message), "error");
  }
}

// ─── Custom paths ────────────────────────────────────────────────────────────

const customPathsBody = document.getElementById("customPathsBody");
const customPathsTable = document.getElementById("customPathsTable");
let customPathsCache = [];
let customPathSiteOptions = [];

if (customPathsBody) loadCustomPaths();

async function loadCustomPaths() {
  if (!customPathsBody) return;
  try {
    const resp = await fetch("/api/custom-paths");
    const data = await resp.json();
    customPathsCache = data.paths || [];
    customPathSiteOptions = data.site_options || [];
    renderCustomPaths(customPathsCache);
  } catch (e) {
    showToast(t("Benutzerdefinierte Pfade konnten nicht geladen werden: " + e.message, "Custom paths could not be loaded: " + e.message));
  }
}

function renderCustomPaths(paths) {
  customPathsBody.innerHTML = "";
  if (!paths.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = t('<td colspan="4" style="color:#6b7280;text-align:center">Keine benutzerdefinierten Pfade</td>','<td colspan="4" style="color:#6b7280;text-align:center">No custom paths</td>');
    customPathsBody.appendChild(tr);
    return;
  }
  paths.forEach(function (p) {
    const active = (p.default_sites || "").split(",").map((site) => site.trim()).filter(Boolean);
    const siteChips = customPathSiteOptions.map(function ({ key, label }) {
      const checked = active.includes(key) ? "checked" : "";
      const disabled = (typeof settingsCanEdit !== "undefined" && !settingsCanEdit) ? "disabled" : "";
      return '<label class="path-site-chip"><input data-custom-path-id="' + p.id + '" type="checkbox" ' + checked + " " + disabled +
        " onchange=\"togglePathSite(" + p.id + ",'" + key + "',this.checked)\"> " + esc(label) + "</label>";
    }).join("");
    const tr = document.createElement("tr");
    const delCell = (typeof settingsCanEdit !== "undefined" && settingsCanEdit)
      ? '<td><button class="btn-del" onclick="deleteCustomPath(' + p.id + ')">'+t("Löschen","Delete")+'</button></td>'
      : '<td></td>';
    tr.innerHTML =
      "<td>" + esc(p.name) + "</td>" +
      "<td style=\"font-family:'SF Mono','Fira Code',monospace;font-size:.82rem\">" + esc(p.path) + "</td>" +
      '<td><div class="path-site-chips">' + siteChips + "</div></td>" +
      delCell;
    customPathsBody.appendChild(tr);
  });
}

async function togglePathSite(pathId, siteKey, enabled) {
  const path = customPathsCache.find((item) => item.id === pathId);
  if (!path) return;

  const previousDefaultSites = path.default_sites || "";
  const active = new Set(previousDefaultSites.split(",")
    .map((site) => site.trim()).filter(Boolean));
  if (enabled) active.add(siteKey);
  else active.delete(siteKey);
  path.default_sites = Array.from(active).join(",");
  setPathSiteInputsDisabled(pathId, true);

  try {
    const save = await fetch("/api/custom-paths/" + pathId, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ default_sites: Array.from(active) }),
    });
    const result = await save.json();
    if (result.error) {
      path.default_sites = previousDefaultSites;
      renderCustomPaths(customPathsCache);
      showToast(result.error);
      return;
    }
    setPathSiteInputsDisabled(pathId, false);
  } catch (e) {
    path.default_sites = previousDefaultSites;
    renderCustomPaths(customPathsCache);
    showToast(t("Standardseiten konnten nicht aktualisiert werden: " + e.message, "Default sites could not be updated: " + e.message));
  }
}

function setPathSiteInputsDisabled(pathId, disabled) {
  const canEdit = typeof settingsCanEdit === "undefined" || settingsCanEdit;
  document.querySelectorAll('[data-custom-path-id="' + pathId + '"]').forEach((input) => {
    input.disabled = disabled || !canEdit;
  });
}

async function addCustomPath() {
  const name = document.getElementById("newPathName").value.trim();
  const path = document.getElementById("newPathValue").value.trim();
  if (!name || !path) { showToast(t("Name und Pfad sind erforderlich", "Name and path required")); return; }
  try {
    const resp = await fetch("/api/custom-paths", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, path }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    document.getElementById("newPathName").value = "";
    document.getElementById("newPathValue").value = "";
    showToast(t("Benutzerdefinierter Pfad hinzugefügt","Custom path added"));
    loadCustomPaths();
  } catch (e) {
    showToast(t("Benutzerdefinierter Pfad konnte nicht hinzugefügt werden: " + e.message, "Custom path could not be added: " + e.message));
  }
}

async function deleteCustomPath(id) {
  if (!await showConfirm(t("Diesen benutzerdefinierten Pfad löschen?","Delete this custom path?"))) return;
  try {
    const resp = await fetch("/api/custom-paths/" + id, { method: "DELETE" });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Benutzerdefinierter Pfad gelöscht","Custom path deleted"));
    loadCustomPaths();
  } catch (e) {
    showToast(t("Benutzerdefinierter Pfad konnte nicht gelöscht werden: " + e.message, "Custom path could not be deleted: " + e.message));
  }
}

// ─── User management ─────────────────────────────────────────────────────────

const userTableBody = document.getElementById("userTableBody");
if (userTableBody) loadUsers();

async function loadUsers() {
  if (!userTableBody) return;
  try {
    const resp = await fetch("/admin/api/users");
    const data = await resp.json();
    renderUsers(data.users || []);
  } catch (e) {
    showToast(t("Benutzer konnten nicht geladen werden: " + e.message, "Users could not be loaded: " + e.message));
  }
}

function renderUsers(users) {
  const adminCount = users.filter(function (u) { return u.role === "admin"; }).length;
  userTableBody.innerHTML = "";
  users.forEach(function (u) {
    const isLastAdmin = u.role === "admin" && adminCount <= 1;
    const tr = document.createElement("tr");
    const authMethod = u.auth_method || "local";
    const authBadge = authMethod === "oidc"
      ? '<span class="auth-badge auth-sso">SSO</span>'
      : '<span class="auth-badge auth-local">'+t("Lokal","Local")+'</span>';
    tr.innerHTML =
      "<td>" + u.id + "</td>" +
      "<td>" + esc(u.username) + "</td>" +
      "<td><select onchange=\"changeRole(" + u.id + ", this.value)\" " + (isLastAdmin ? "disabled" : "") + ">" +
      "<option value=\"user\" " + (u.role === "user" ? "selected" : "") + ">" + t("Benutzer","User") + "</option>" +
      "<option value=\"admin\" " + (u.role === "admin" ? "selected" : "") + ">" + t("Administrator","Administrator") + "</option>" +
      "</select></td>" +
      "<td>" + authBadge + "</td>" +
      "<td>" + esc(u.created_at) + "</td>" +
      "<td>" + (isLastAdmin
        ? '<span style="color:#555">'+t("geschützt","protected")+'</span>'
        : '<button class="btn-del" onclick="deleteUser(' + u.id + ')">'+t("Löschen","Delete")+'</button>') + "</td>";
    userTableBody.appendChild(tr);
  });
}

async function addUser() {
  const username = document.getElementById("newUsername").value.trim();
  const password = document.getElementById("newPassword").value;
  const role = document.getElementById("newRole").value;
  if (!username || !password) { showToast(t("Benutzername und Passwort sind erforderlich", "Username and password required")); return; }
  try {
    const resp = await fetch("/admin/api/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password, role }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    document.getElementById("newUsername").value = "";
    document.getElementById("newPassword").value = "";
    showToast(t("Benutzer erstellt","User created"));
    loadUsers();
  } catch (e) {
    showToast(t("Benutzer konnte nicht erstellt werden: " + e.message, "User could not be created: " + e.message));
  }
}

async function deleteUser(id) {
  if (!await showConfirm(t("Diesen Benutzer löschen?", "Delete this user?"))) return;
  try {
    const resp = await fetch("/admin/api/users/" + id, { method: "DELETE" });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    showToast(t("Benutzer gelöscht","User deleted"));
    loadUsers();
  } catch (e) {
    showToast(t("Benutzer konnte nicht gelöscht werden: " + e.message, "User could not be deleted: " + e.message));
  }
}

async function changeRole(id, newRole) {
  try {
    const resp = await fetch("/admin/api/users/" + id + "/role", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ role: newRole }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); loadUsers(); return; }
    showToast(t("Rolle aktualisiert", "Role updated"));
    loadUsers();
  } catch (e) {
    showToast(t("Rolle konnte nicht aktualisiert werden: " + e.message, "Role could not be updated: " + e.message));
  }
}

// ─── External API key ─────────────────────────────────────────────────────────

async function loadApiKey() {
  try {
    const resp = await fetch("/api/settings/api-key");
    const data = await resp.json();
    const el = document.getElementById("externalApiKeyDisplay");
    if (el) el.value = data.key || "";
    const baseUrl = window.location.origin;
    const baseEl = document.getElementById("apiBaseUrlDisplay");
    const exEl = document.getElementById("apiExampleUrl");
    if (baseEl) baseEl.textContent = baseUrl;
    if (exEl) exEl.textContent = baseUrl + "/api/v1/status?apikey=****";
  } catch (e) { /* non-critical */ }
}

function toggleApiKeyVisibility() {
  const el = document.getElementById("externalApiKeyDisplay");
  const btn = document.getElementById("apiKeyToggleBtn");
  if (!el) return;
  const visible = el.type === "text";
  el.type = visible ? "password" : "text";
  btn.textContent = visible ? t("Anzeigen","Show") : t("Verbergen","Hide");
}

async function copyApiKey() {
  const el = document.getElementById("externalApiKeyDisplay");
  if (!el || !el.value) return;
  try {
    await navigator.clipboard.writeText(el.value);
    showToast(t("API-Key kopiert","API key copied"), "success");
  } catch (e) {
    showToast(t("Kopieren fehlgeschlagen: " + e.message, "Copying failed: " + e.message), "error");
  }
}

async function regenerateApiKey() {
  if (!await showConfirm(t("API-Key neu generieren? Der alte Key wird sofort ungültig.","Regenerate API key? The old key will become invalid immediately."))) return;
  try {
    const resp = await fetch("/api/settings/api-key/regenerate", { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      const el = document.getElementById("externalApiKeyDisplay");
      if (el) el.value = data.key;
      const exEl = document.getElementById("apiExampleUrl");
      if (exEl) exEl.textContent = window.location.origin + "/api/v1/status?apikey=****";
      showToast(t("Neuer API-Key generiert","New API key generated"), "success");
    } else {
      showToast(t("Fehler beim Generieren","Error generating"), "error");
    }
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message), "error");
  }
}

// ─── Update checker ───────────────────────────────────────────────────────────

function _applyUpdateData(data) {
  const latestEl        = document.getElementById("latestVersion");
  const banner          = document.getElementById("updateBanner");
  const bannerText      = document.getElementById("updateBannerText");
  const changelog       = document.getElementById("updateChangelog");
  const releaseLink     = document.getElementById("updateReleaseLink");
  const status          = document.getElementById("updateStatus");
  const pipCmd          = document.getElementById("updatePipCmd");
  const devHint         = document.getElementById("devChannelHint");

  const isDevInstall = !!data.is_dev_install;
  const canSelfUpdate = !!data.can_self_update;
  _selfUpdateCanUpdate = canSelfUpdate;
  if (data.channel) _selfUpdateChannel = data.channel;

  // "Verfügbar" field
  if (latestEl) latestEl.textContent = data.latest_version || "—";

  // Channel switch (pip/pipx only)
  const channelRow = document.getElementById("updateChannelRow");
  if (channelRow) channelRow.style.display = canSelfUpdate ? "" : "none";
  _renderChannelSwitch(_selfUpdateChannel);

  // Auto-update section only makes sense when we can self-update
  const autoSec = document.getElementById("autoUpdateSection");
  if (autoSec) autoSec.style.display = canSelfUpdate ? "" : "none";

  // Manual pip command + dev copy-hint only when we cannot self-update
  const pipEl = document.getElementById("updatePipCmd");
  const pipBlock = (pipEl && pipEl.closest("div")) ? pipEl.closest("div").parentElement : null;
  if (pipBlock) pipBlock.style.display = canSelfUpdate ? "none" : "";
  if (devHint) devHint.style.display = (!canSelfUpdate && !isDevInstall) ? "" : "none";

  if (data.error) {
    if (status) status.textContent = "⚠️ " + data.error;
    if (banner) banner.style.display = "none";
  } else if (data.update_available) {
    if (status) status.textContent = "";
    if (banner) banner.style.display = "";

    if (isDevInstall) {
      // Dev install: neuere Commits auf models branch
      if (bannerText) bannerText.textContent =
        t("Neuere Commits verfügbar (aktueller Commit: " + data.latest_version + ")","Newer commits available (current commit: " + data.latest_version + ")");
      if (releaseLink && data.release_url) {
        releaseLink.href = data.release_url;
        releaseLink.textContent = t("Commits ansehen","View commits");
      }
      if (changelog) changelog.style.display = "none";
      if (pipCmd) pipCmd.textContent =
        'pip install --upgrade "git+https://github.com/PD-Codes/MediaForge.git@main"';
    } else {
      // Release install: neue Version verfügbar
      if (bannerText) bannerText.textContent =t("Version " + data.latest_version + " verfügbar (installiert: " + data.local_version + ")","Version " + data.latest_version + " available (installed: " + data.local_version + ")");
      if (releaseLink && data.release_url) {
        releaseLink.href = data.release_url;
        releaseLink.textContent = t("Release öffnen","Open Release");
      }
      if (changelog) {
        changelog.style.display = "";
        changelog.textContent = data.release_notes || "";
      }
      if (pipCmd) pipCmd.textContent =
        'pip install --upgrade mediaforge';
    }
  } else {
    if (status) status.textContent = t("✓ Bereits aktuell.","✓ Already up to date.");
    if (banner) banner.style.display = "none";
  }

  const installRow = document.getElementById("updateInstallRow");
  if (installRow) installRow.style.display = (canSelfUpdate && data.update_available) ? "" : "none";
}

let _selfUpdateChannel = null;
let _selfUpdateCanUpdate = false;

function _renderChannelSwitch(channel) {
  const sw = document.getElementById("updateChannelSwitch");
  if (!sw) return;
  sw.querySelectorAll(".update-channel-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.channel === channel);
  });
}

function installUpdateNow() {
  if (window.AniUpdate) window.AniUpdate.startInstall();
}

async function switchChannel(target) {
  if (!target || target === _selfUpdateChannel) return;
  const msg = target === "dev"
    ? t("Die neueste (evtl. instabile) Version aus dem models-Branch wird jetzt installiert.",
        "The latest (possibly unstable) version from the models branch will be installed now.")
    : t("Die stabile Version wird jetzt installiert.",
        "The stable release will be installed now.");
  const title = target === "dev"
    ? t("Zum Dev-Channel wechseln?", "Switch to the dev channel?")
    : t("Zur stabilen Version wechseln?", "Switch to stable?");
  const okLabel = t("Wechseln & installieren", "Switch & install");
  let confirmed = false;
  if (typeof showConfirm === "function") {
    confirmed = await showConfirm(msg, okLabel, title, "btn-primary");
  } else {
    confirmed = window.confirm(msg);
  }
  if (!confirmed) return;
  if (window.AniUpdate) window.AniUpdate.startInstall(target);
}

function _renderAutoUpdateDays(csv) {
  const cont = document.getElementById("autoUpdateDays");
  if (!cont) return;
  const active = new Set(
    String(csv || "").split(",").map((x) => parseInt(x.trim(), 10)).filter((x) => !isNaN(x)),
  );
  const labels = (typeof _weekdayLabels === "function")
    ? _weekdayLabels()
    : ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
  cont.innerHTML = "";
  labels.forEach((lbl, i) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "weekday-toggle" + (active.has(i) ? " active" : "");
    b.dataset.day = i;
    b.textContent = lbl;
    b.addEventListener("click", () => { b.classList.toggle("active"); saveAutoUpdate(); });
    cont.appendChild(b);
  });
}

function _getAutoUpdateDays() {
  const cont = document.getElementById("autoUpdateDays");
  if (!cont) return "";
  return Array.from(cont.querySelectorAll(".weekday-toggle.active"))
    .map((b) => b.dataset.day).join(",");
}

async function saveAutoUpdate() {
  const enabled = document.getElementById("autoUpdateEnabled");
  const timeEl = document.getElementById("autoUpdateTime");
  const block = document.getElementById("autoUpdateBlock");
  const isOn = !!(enabled && enabled.checked);
  if (block) block.style.display = isOn ? "" : "none";
  const days = _getAutoUpdateDays() || "0,1,2,3,4,5,6";
  const payload = {
    auto_update_enabled: isOn,
    auto_update_days: days,
    auto_update_time: (timeEl && timeEl.value) ? timeEl.value : "03:00",
  };
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await resp.json();
    if (data.ok) showToast(t("Gespeichert", "Saved"), "success");
    else showToast(data.error || t("Konnte nicht gespeichert werden", "Could not save"), "error");
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden", "Could not save"), "error");
  }
}

async function copyUpdateCmd() {
  const el = document.getElementById("updatePipCmd");
  if (!el) return;
  try {
    await navigator.clipboard.writeText(el.textContent.trim());
    showToast("Befehl kopiert", "success");
  } catch (e) {
    showToast(t("Kopieren fehlgeschlagen","failed to copy"), "error");
  }
}

async function copyDevChannelCmd() {
  const el = document.getElementById("devChannelCmd");
  if (!el) return;
  try {
    await navigator.clipboard.writeText(el.textContent.trim());
    showToast(t("Befehl kopiert", "Command Copied"), "success");
  } catch (e) {
    showToast(t("Kopieren fehlgeschlagen", "failed to copy"), "error");
  }
}

async function checkForUpdates(force = false) {
  const btn = document.getElementById("updateCheckBtn");
  const status = document.getElementById("updateStatus");
  if (btn) { btn.disabled = true; btn.textContent = "Prüfe…"; }
  if (status) status.textContent = "";
  try {
    const resp = await fetch("/api/update-check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force }),
    });
    _applyUpdateData(await resp.json());
  } catch (e) {
    if (status) status.textContent = t("⚠️ Fehler: ", "⚠️ Error:") + e.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t("Jetzt prüfen", "check now"); }
  }
}

(function initUpdateSection() {
  fetch("/api/update-check", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ force: false }),
  }).then(function (r) { return r.json(); }).then(_applyUpdateData).catch(function () { });
})();

// ─── Toast / helpers ──────────────────────────────────────────────────────────

function showToast(msg, type) {
  const t = document.getElementById("toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast" + (type ? " toast-" + type : "");
  t.style.display = "";
  t.classList.remove("show");
  void t.offsetWidth;
  t.classList.add("show");
  clearTimeout(t._hideTimer);
  t._hideTimer = setTimeout(function () { t.classList.remove("show"); }, 4000);
}

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s || "";
  return d.innerHTML;
}

// ─── Kick off ─────────────────────────────────────────────────────────────────
loadSettings();

// ─── .env migration banner ───────────────────────────────────────────────────

(function checkEnvFileBanner() {
  var banner = document.getElementById("envFileBanner");
  if (!banner) return;
  fetch("/api/settings/env-file")
    .then(function (r) { return r.json(); })
    .then(function (data) {
      if (data.exists && data.migrated) {
        banner.style.display = "flex";
      }
    })
    .catch(function () { });
})();

async function deleteEnvFile() {
  const banner = document.getElementById("envFileBanner");
  if (!await showConfirm(t("~/.mediaforge/.env jetzt löschen? Diese Aktion kann nicht rückgängig gemacht werden.","Delete ~/.mediaforge/.env now? This action cannot be undone."))) return;
  try {
    const resp = await fetch("/api/settings/env-file", { method: "DELETE" });
    const data = await resp.json();
    if (data.ok) {
      if (banner) banner.style.display = "none";
      showToast(t(".env erfolgreich gelöscht", "env deleted successfully"), "success");
    } else {
      showToast(data.error || t("Fehler beim Löschen", "Error deleting env"), "error");
    }
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message, "error");
  }
}

// ─── Design / Appearance ───────────────────────────────────────────────────

function _loadDesignCheckboxes() {
  const settings = [
    { id: 'glowEffect', key: 'aw-glow-effect' },
    { id: 'headerColor', key: 'aw-header-color' },
    { id: 'skeletonLoader', key: 'aw-skeleton-loader' },
    { id: 'chooseBorder', key: 'aw-choose-border' },
    { id: 'activeDownloadGlow', key: 'aw-active-download-glow' },
    { id: 'clickEffect', key: 'aw-click-effect' },
    { id: 'iconMove', key: 'aw-icon-move' }
  ];
  settings.forEach(s => {
    const el = document.getElementById(s.id);
    if (el) el.checked = localStorage.getItem(s.key) === 'true';
  });
  _loadThemePackSelect();
}

// ─── Theme packs (web/themes.py) ────────────────────────────────────────────
// The personal choice is localStorage 'aw-themepack' (same pattern as the
// dark/light 'aw-theme' key); applyThemePack() in base.html swaps the
// stylesheet live. The instance default is a server setting (admin only).

function _loadThemePackSelect() {
  const el = document.getElementById('themePackSelect');
  if (!el) return;
  let choice = '';
  try { choice = localStorage.getItem('aw-themepack') || ''; } catch (e) { }
  // A stale override (theme uninstalled since) falls back to instance default.
  if (choice && ![...el.options].some(o => o.value === choice)) choice = '';
  el.value = choice;
}

function saveThemePackChoice() {
  const el = document.getElementById('themePackSelect');
  if (!el) return;
  applyThemePack(el.value);
  showToast(t('Theme übernommen', 'Theme applied'));
}

function saveThemePackDefault() {
  const el = document.getElementById('themePackDefaultSelect');
  if (!el) return;
  const folder = el.value === 'default' ? '' : el.value;
  fetch('/api/themes/active', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ folder })
  }).then(r => r.json()).then(d => {
    if (d && d.ok) {
      showToast(t('Standard-Theme gespeichert', 'Default theme saved'));
      // If this user follows the instance default, apply it right away.
      let choice = '';
      try { choice = localStorage.getItem('aw-themepack') || ''; } catch (e) { }
      if (!choice) {
        window._THEME_DEFAULT = folder;
        applyThemePack('');
      }
    } else {
      showToast((d && d.error) || t('Speichern fehlgeschlagen', 'Save failed'));
    }
  }).catch(() => showToast(t('Speichern fehlgeschlagen', 'Save failed')));
}

function _toggleDesignSetting(key, id, className, label) {
  const el = document.getElementById(id);
  if (!el) return;
  const active = el.checked;
  localStorage.setItem(key, active);
  document.body.classList.toggle(className, active);
  showToast(label + (active ? t(" aktiviert", " enabled") : t(" deaktiviert", " disabled")));
}

function saveGlowEffect() {
  _toggleDesignSetting('aw-glow-effect', 'glowEffect', 'glow-effect', t('Glow-Effekt','Glow Effect'));
}

function saveHeaderColor() {
  _toggleDesignSetting('aw-header-color', 'headerColor', 'header-color', t  ('Header-Farbe','Header Color'));
}

function saveHeaderColorHelp() {
  _toggleDesignSetting('aw-header-color-help', 'headerColorHelp', 'header-color-help', t('Header-Farbe umgestellt','Header Color Changed'));
}

function saveSkeletonLoader() {
  _toggleDesignSetting('aw-skeleton-loader', 'skeletonLoader', 'skeleton-loader', t('Skeleton Loader','Skeleton Loader'));
}

function saveBorder() {
  _toggleDesignSetting('aw-choose-border', 'chooseBorder', 'choose-border', t('Farbliche Markierung','Color marking'));
}

function saveActiveDownloadGlow() {
  _toggleDesignSetting('aw-active-download-glow', 'activeDownloadGlow', 'active-download-glow', t('Download-Markierung','Download marking'));
}

function saveClickEffect() {
  _toggleDesignSetting('aw-click-effect', 'clickEffect', 'click-effect', t('Klick-Effekt','Click Effect'));
}

function saveIconMove() {
  _toggleDesignSetting('aw-icon-move', 'iconMove', 'icon-move', t('Icon Movement','Icon Movement'));
}

// ─── Drag to Scroll for Settings Tabs ─────────────────────────────────────────

document.addEventListener("DOMContentLoaded", () => {
  const tabs = document.getElementById("settingsTabs");
  if (!tabs) return;

  let isDown = false;
  let startX;
  let scrollLeft;
  let dragged = false;

  tabs.addEventListener("mousedown", (e) => {
    isDown = true;
    dragged = false;
    tabs.style.cursor = "grabbing";
    startX = e.pageX - tabs.offsetLeft;
    scrollLeft = tabs.scrollLeft;
  });

  tabs.addEventListener("mouseleave", () => {
    isDown = false;
    tabs.style.cursor = "grab";
  });

  tabs.addEventListener("mouseup", () => {
    isDown = false;
    tabs.style.cursor = "grab";
  });

  tabs.addEventListener("mousemove", (e) => {
    if (!isDown) return;
    const x = e.pageX - tabs.offsetLeft;
    const walk = (x - startX) * 1.5; // scroll speed multiplier
    if (Math.abs(walk) > 5) {
      dragged = true;
    }
    tabs.scrollLeft = scrollLeft - walk;
  });

  // Intercept click event in capture phase to prevent switching tab when dragging
  tabs.addEventListener("click", (e) => {
    if (dragged) {
      e.stopPropagation();
      e.preventDefault();
    }
  }, true);
});

// ─── Custom Time Picker ──────────────────────────────────────────────────────

function createCustomTimePicker(inputEl) {
  if (inputEl.customPickerCreated) return;
  inputEl.customPickerCreated = true;

  inputEl.style.display = "none";

  const wrapper = document.createElement("div");
  wrapper.className = "asf-custom-time-picker";
  wrapper.style.display = "inline-flex";
  wrapper.style.alignItems = "center";
  wrapper.style.gap = "4px";

  const selectHour = document.createElement("select");
  selectHour.style.minWidth = "55px";
  const selectHourWrap = document.createElement("div");
  selectHourWrap.className = "asf-select-wrap";
  selectHourWrap.appendChild(selectHour);
  
  const separator = document.createElement("span");
  separator.textContent = ":";
  separator.style.fontWeight = "bold";
  separator.style.color = "var(--text-secondary)";

  const selectMinute = document.createElement("select");
  selectMinute.style.minWidth = "55px";
  const selectMinuteWrap = document.createElement("div");
  selectMinuteWrap.className = "asf-select-wrap";
  selectMinuteWrap.appendChild(selectMinute);

  const selectAmpm = document.createElement("select");
  selectAmpm.style.minWidth = "60px";
  const optAm = document.createElement("option");
  optAm.value = "AM"; optAm.textContent = "AM";
  const optPm = document.createElement("option");
  optPm.value = "PM"; optPm.textContent = "PM";
  selectAmpm.appendChild(optAm);
  selectAmpm.appendChild(optPm);
  const selectAmpmWrap = document.createElement("div");
  selectAmpmWrap.className = "asf-select-wrap";
  selectAmpmWrap.appendChild(selectAmpm);

  inputEl.parentNode.insertBefore(wrapper, inputEl.nextSibling);
  inputEl.customWrapper = wrapper;

  function populateHours(is12h) {
    const prevVal = selectHour.value;
    selectHour.innerHTML = "";
    if (is12h) {
      for (let h = 1; h <= 12; h++) {
        const opt = document.createElement("option");
        opt.value = String(h).padStart(2, "0");
        opt.textContent = String(h).padStart(2, "0");
        selectHour.appendChild(opt);
      }
    } else {
      for (let h = 0; h < 24; h++) {
        const opt = document.createElement("option");
        opt.value = String(h).padStart(2, "0");
        opt.textContent = String(h).padStart(2, "0");
        selectHour.appendChild(opt);
      }
    }
    if (prevVal) {
      selectHour.value = prevVal;
    }
  }

  for (let m = 0; m < 60; m++) {
    const opt = document.createElement("option");
    opt.value = String(m).padStart(2, "0");
    opt.textContent = String(m).padStart(2, "0");
    selectMinute.appendChild(opt);
  }

  function syncPickerUI() {
    const format = localStorage.getItem("timeFormatSetting") || "24h";
    const is12h = format === "12h";

    populateHours(is12h);

    let [hh, mm] = (inputEl.value || "06:00").split(":");
    let hVal = parseInt(hh, 10);
    let mVal = parseInt(mm, 10);
    if (isNaN(hVal)) hVal = 6;
    if (isNaN(mVal)) mVal = 0;

    if (is12h) {
      let ampm = "AM";
      if (hVal >= 12) {
        ampm = "PM";
        if (hVal > 12) hVal -= 12;
      } else if (hVal === 0) {
        hVal = 12;
      }
      selectHour.value = String(hVal).padStart(2, "0");
      selectAmpm.value = ampm;
      if (!selectAmpmWrap.parentNode) {
        wrapper.appendChild(selectAmpmWrap);
      }
    } else {
      selectHour.value = String(hVal).padStart(2, "0");
      if (selectAmpmWrap.parentNode) {
        wrapper.removeChild(selectAmpmWrap);
      }
    }
    selectMinute.value = String(mVal).padStart(2, "0");
  }

  wrapper.appendChild(selectHourWrap);
  wrapper.appendChild(separator);
  wrapper.appendChild(selectMinuteWrap);

  function updateInputValue() {
    const format = localStorage.getItem("timeFormatSetting") || "24h";
    const is12h = format === "12h";

    let hVal = parseInt(selectHour.value, 10);
    let mVal = parseInt(selectMinute.value, 10);

    if (is12h) {
      const ampm = selectAmpm.value;
      if (ampm === "PM" && hVal < 12) hVal += 12;
      else if (ampm === "AM" && hVal === 12) hVal = 0;
    }

    const hh = String(hVal).padStart(2, "0");
    const mm = String(mVal).padStart(2, "0");
    const newVal = hh + ":" + mm;

    if (inputEl.value !== newVal) {
      inputEl.value = newVal;
      inputEl.dispatchEvent(new Event("change"));
    }
  }

  selectHour.addEventListener("change", updateInputValue);
  selectMinute.addEventListener("change", updateInputValue);
  selectAmpm.addEventListener("change", updateInputValue);

  inputEl.syncCustomPicker = syncPickerUI;
  syncPickerUI();
}

function changeTimeFormatSetting() {
  const select = document.getElementById("timeFormatSetting");
  if (!select) return;
  const val = select.value;
  localStorage.setItem("timeFormatSetting", val);
  
  document.querySelectorAll("#downloadWindowStart, #downloadWindowEnd, .sync-time-row input").forEach((input) => {
    if (input.syncCustomPicker) {
      input.syncCustomPicker();
    }
  });
}


// ─── Quellen / Sources: Reihenfolge & Aktivierung ───────────────────────────

const SOURCE_META = {
  aniworld:   { label: "AniWorld",   cls: "browse-provider-aniworld",   hasSections: true },
  sto:        { label: "SerienStream", cls: "browse-provider-sto",       hasSections: true },
  filmpalast: { label: "FilmPalast", cls: "browse-provider-filmpalast", hasSections: false },
  megakino:   { label: "MegaKino",   cls: "browse-provider-megakino",   hasSections: false, multiSections: [
                  { key: "new_movies",     de: "Neue Filme",     en: "New Movies" },
                  { key: "popular_movies", de: "Beliebte Filme", en: "Popular Movies" },
                  { key: "new_series",     de: "Neue Serien",    en: "New Series" },
                  { key: "popular_series", de: "Beliebte Serien", en: "Popular Series" },
              ] },
  hanime:     { label: "hanime 18+",  cls: "browse-provider-hanime",     hasSections: false, multiSections: [
                  { key: "new",        de: "Neu",         en: "New" },
                  { key: "trending",   de: "Trending",    en: "Trending" },
                  // Content-type filters — applied per item within the New/
                  // Trending lists above (not separate sections themselves).
                  { key: "censored",   de: "Zensiert",    en: "Censored" },
                  { key: "uncensored", de: "Unzensiert",  en: "Uncensored" },
              ] }
};

let _sourceState = {
  order: ["aniworld", "sto", "filmpalast", "megakino", "hanime"],
  section_order: { aniworld: ["new", "popular"], sto: ["new", "popular"], megakino: ["new_movies", "popular_movies", "new_series", "popular_series"], hanime: ["new", "trending"] },
  sections_visible: { aniworld: { new: true, popular: true }, sto: { new: true, popular: true }, megakino: { new_movies: true, popular_movies: true, new_series: true, popular_series: true }, hanime: { new: true, trending: true, censored: true, uncensored: true } },
  enabled: { aniworld: true, sto: true, filmpalast: true, megakino: true, hanime: false },
  hide_in_search: false
};

function _splitOrder(str, fallback) {
  const parts = String(str || "").split(",").map(p => p.trim().toLowerCase()).filter(Boolean);
  return parts.length ? parts : fallback.slice();
}

function _loadSourceSettings(sources) {
  sources = sources || {};
  const validProv = ["aniworld", "sto", "filmpalast", "megakino", "hanime"];
  let order = _splitOrder(sources.order, ["aniworld", "sto", "filmpalast", "megakino", "hanime"]).filter(p => validProv.indexOf(p) !== -1);
  // ensure every provider is present exactly once
  validProv.forEach(p => { if (order.indexOf(p) === -1) order.push(p); });
  _sourceState.order = order;

  const so = sources.section_order || {};
  _sourceState.section_order.aniworld = _splitOrder(so.aniworld, ["new", "popular"]);
  _sourceState.section_order.sto      = _splitOrder(so.sto,      ["new", "popular"]);

  const secVis = sources.sections || {};
  ["aniworld", "sto"].forEach(p => {
    const sp = secVis[p] || {};
    _sourceState.sections_visible[p] = { new: sp.new !== "0", popular: sp.popular !== "0" };
  });
  // MegaKino: four independently toggleable sections
  {
    const mp = secVis.megakino || {};
    _sourceState.sections_visible.megakino = {
      new_movies:     mp.new_movies     !== "0",
      popular_movies: mp.popular_movies !== "0",
      new_series:     mp.new_series     !== "0",
      popular_series: mp.popular_series !== "0",
    };
  }
  // hanime: new + trending (sections) plus censored/uncensored (item filters)
  {
    const hn = secVis.hanime || {};
    _sourceState.sections_visible.hanime = {
      new: hn.new !== "0",
      trending: hn.trending !== "0",
      censored: hn.censored !== "0",
      uncensored: hn.uncensored !== "0",
    };
  }

  const en = sources.enabled || {};
  _sourceState.enabled.aniworld   = en.aniworld   !== "0";
  _sourceState.enabled.sto        = en.sto        !== "0";
  _sourceState.enabled.filmpalast = en.filmpalast !== "0";
  _sourceState.enabled.megakino   = en.megakino   !== "0";
  _sourceState.enabled.hanime     = en.hanime     === "1";  // adult source: default OFF
  _sourceState.hide_in_search = sources.hide_disabled_in_search === "1";

  // Reflect enabled toggles (Quellen tab)
  const cbSto = document.getElementById("sourceEnabledSto");
  const cbAni = document.getElementById("sourceEnabledAniworld");
  const cbFp  = document.getElementById("sourceEnabledFilmpalast");
  const cbMk  = document.getElementById("sourceEnabledMegakino");
  const cbHan = document.getElementById("sourceEnabledHanime");
  if (cbSto) cbSto.checked = _sourceState.enabled.sto;
  if (cbAni) cbAni.checked = _sourceState.enabled.aniworld;
  if (cbFp)  cbFp.checked  = _sourceState.enabled.filmpalast;
  if (cbMk)  cbMk.checked  = _sourceState.enabled.megakino;
  if (cbHan) cbHan.checked = _sourceState.enabled.hanime;
  const cbHide = document.getElementById("sourcesHideInSearch");
  if (cbHide) cbHide.checked = _sourceState.hide_in_search;

  _renderSourceOrder();
}

function _sectionLabel(prov) {
  const first = (_sourceState.section_order[prov] || ["new", "popular"])[0];
  return first === "new" ? t("Neu zuerst", "New first") : t("Beliebt zuerst", "Popular first");
}

function _renderSourceOrder() {
  const list = document.getElementById("sourceOrderList");
  if (!list) return;
  list.innerHTML = "";
  _sourceState.order.forEach((prov, idx) => {
    const meta = SOURCE_META[prov];
    if (!meta) return;
    const row = document.createElement("div");
    row.className = "source-order-row";
    row.setAttribute("draggable", "true");
    row.dataset.provider = prov;

    let sectionCtrls = "";
    if (meta.hasSections) {
      const vis = _sourceState.sections_visible[prov] || { new: true, popular: true };
      const bothVisible = vis.new && vis.popular;
      sectionCtrls =
        '<label class="source-sec-check"><input type="checkbox" class="chb-main" ' + (vis.new ? "checked" : "") +
          ' onchange="toggleSourceSectionVisible(\'' + prov + '\',\'new\')"> ' + t("Neu", "New") + '</label>' +
        '<label class="source-sec-check"><input type="checkbox" class="chb-main" ' + (vis.popular ? "checked" : "") +
          ' onchange="toggleSourceSectionVisible(\'' + prov + '\',\'popular\')"> ' + t("Beliebt", "Popular") + '</label>' +
        '<button type="button" class="source-section-toggle" ' + (bothVisible ? "" : "disabled") +
          ' title="' + t("Reihenfolge der Bereiche", "Order of the sections") + '"' +
          ' onclick="toggleSourceSection(\'' + prov + '\')">' + _sectionLabel(prov) + '</button>';
    } else if (meta.multiSections) {
      const vis = _sourceState.sections_visible[prov] || {};
      sectionCtrls = meta.multiSections.map(function (sec) {
        return '<label class="source-sec-check"><input type="checkbox" class="chb-main" ' +
          (vis[sec.key] !== false ? "checked" : "") +
          ' onchange="toggleSourceSectionVisible(\'' + prov + '\',\'' + sec.key + '\')"> ' +
          t(sec.de, sec.en) + '</label>';
      }).join("");
    }

    row.innerHTML =
      '<span class="source-drag-handle" title="' + t("Ziehen zum Sortieren", "Drag to reorder") + '" aria-hidden="true">' +
        '<svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor"><circle cx="7" cy="5" r="1.5"/><circle cx="13" cy="5" r="1.5"/><circle cx="7" cy="10" r="1.5"/><circle cx="13" cy="10" r="1.5"/><circle cx="7" cy="15" r="1.5"/><circle cx="13" cy="15" r="1.5"/></svg>' +
      '</span>' +
      '<span class="source-badge ' + meta.cls + '">' + meta.label + '</span>' +
      '<div class="source-order-actions">' +
        sectionCtrls +
        '<button type="button" class="source-move-btn" title="' + t("Nach oben", "Move up") + '" ' + (idx === 0 ? "disabled" : "") + ' onclick="moveSource(\'' + prov + '\',-1)">' +
          '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg>' +
        '</button>' +
        '<button type="button" class="source-move-btn" title="' + t("Nach unten", "Move down") + '" ' + (idx === _sourceState.order.length - 1 ? "disabled" : "") + ' onclick="moveSource(\'' + prov + '\',1)">' +
          '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>' +
        '</button>' +
      '</div>';

    _attachSourceDnd(row);
    list.appendChild(row);
  });
}
let _dragProv = null;
function _attachSourceDnd(row) {
  row.addEventListener("dragstart", (e) => {
    _dragProv = row.dataset.provider;
    row.classList.add("dragging");
    try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", _dragProv); } catch (err) {}
  });
  row.addEventListener("dragend", () => {
    _dragProv = null;
    row.classList.remove("dragging");
    document.querySelectorAll(".source-order-row.drag-over").forEach(r => r.classList.remove("drag-over"));
  });
  row.addEventListener("dragover", (e) => {
    e.preventDefault();
    try { e.dataTransfer.dropEffect = "move"; } catch (err) {}
    if (row.dataset.provider !== _dragProv) row.classList.add("drag-over");
  });
  row.addEventListener("dragleave", () => row.classList.remove("drag-over"));
  row.addEventListener("drop", (e) => {
    e.preventDefault();
    row.classList.remove("drag-over");
    const target = row.dataset.provider;
    if (!_dragProv || _dragProv === target) return;
    const order = _sourceState.order;
    const from = order.indexOf(_dragProv);
    const to = order.indexOf(target);
    if (from === -1 || to === -1) return;
    order.splice(from, 1);
    order.splice(to, 0, _dragProv);
    _renderSourceOrder();
    _saveSourceOrder();
  });
}

function moveSource(prov, dir) {
  const order = _sourceState.order;
  const i = order.indexOf(prov);
  const j = i + dir;
  if (i === -1 || j < 0 || j >= order.length) return;
  const tmp = order[i]; order[i] = order[j]; order[j] = tmp;
  _renderSourceOrder();
  _saveSourceOrder();
}

function toggleSourceSection(prov) {
  const cur = _sourceState.section_order[prov] || ["new", "popular"];
  _sourceState.section_order[prov] = [cur[1], cur[0]];
  _renderSourceOrder();
  _saveSectionOrder(prov);
}

function toggleSourceSectionVisible(prov, sec) {
  const vis = _sourceState.sections_visible[prov] || {};
  vis[sec] = !vis[sec];
  _sourceState.sections_visible[prov] = vis;
  _renderSourceOrder();
  const payload = {};
  payload["source_show_" + sec + "_" + prov] = vis[sec];
  let secLabel;
  const meta = SOURCE_META[prov];
  if (meta && meta.multiSections) {
    const ms = meta.multiSections.find(function (m) { return m.key === sec; });
    secLabel = ms ? t(ms.de, ms.en) : sec;
  } else {
    secLabel = sec === "new" ? t("Neu", "New") : t("Beliebt", "Popular");
  }
  _putSettings(payload, secLabel + (vis[sec] ? t(" eingeblendet", " shown") : t(" ausgeblendet", " hidden")));
}

async function _putSettings(payload, okMsg) {
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return false; }
    if (okMsg) showToast(okMsg);
    return true;
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden: ", "Could not be saved: ") + e.message);
    return false;
  }
}

function _saveSourceOrder() {
  _putSettings({ home_source_order: _sourceState.order.join(",") }, t("Reihenfolge gespeichert", "Order saved"));
}

function _saveSectionOrder(prov) {
  const payload = {};
  payload["home_section_order_" + prov] = (_sourceState.section_order[prov] || ["new", "popular"]).join(",");
  _putSettings(payload, t("Reihenfolge gespeichert", "Order saved"));
}

function _commitSourceEnabled(prov, enabled) {
  _sourceState.enabled[prov] = enabled;
  const payload = {};
  payload["source_enabled_" + prov] = enabled;
  const label = SOURCE_META[prov] ? SOURCE_META[prov].label : prov;
  _putSettings(payload, label + (enabled ? t(" aktiviert", " enabled") : t(" deaktiviert", " disabled")));
}

function saveSourceEnabled(prov) {
  const map = { sto: "sourceEnabledSto", aniworld: "sourceEnabledAniworld", filmpalast: "sourceEnabledFilmpalast", megakino: "sourceEnabledMegakino", hanime: "sourceEnabledHanime" };
  const el = document.getElementById(map[prov]);
  if (!el) return;
  // hanime is an adult source: turning it ON requires an explicit 18+ confirmation.
  if (prov === "hanime" && el.checked) {
    el.checked = false;              // stays off until the user confirms
    _openHanimeAgeModal();
    return;
  }
  _commitSourceEnabled(prov, el.checked);
}

// --- hanime 18+ age gate --------------------------------------------------
// The "No" button is the highlighted one and "Yes" is de-emphasised on purpose,
// so the user has to actually read the question instead of clicking the
// prominent button by reflex.
// True once the user clicked "Yes, I am under 18" and bailed out at least once.
// The NEXT activation attempt then shows the sharper "should we really believe
// you?" variant instead of the normal question.
let _hanimeBailedOnce = false;

function _openHanimeAgeModal() {
  const s1 = document.getElementById("hanimeAgeStep1"); // variant A (first try)
  const s2 = document.getElementById("hanimeAgeStep2"); // variant B (retry after bailing)
  if (s1) s1.style.display = _hanimeBailedOnce ? "none" : "";
  if (s2) s2.style.display = _hanimeBailedOnce ? "" : "none";
  const ov = document.getElementById("hanimeAgeOverlay");
  if (ov) ov.classList.add("open");
}
function _closeHanimeAgeModal() {
  const ov = document.getElementById("hanimeAgeOverlay");
  if (ov) ov.classList.remove("open");
}
function _hanimeAgeClose() {
  _closeHanimeAgeModal();
  const el = document.getElementById("sourceEnabledHanime");
  if (el) el.checked = false;
}
// Variant A, highlighted button "Yes, I am under 18": do NOT enable, and
// remember it so the next attempt shows variant B.
function hanimeAgeUnder18() {
  _hanimeBailedOnce = true;
  _hanimeAgeClose();
}
// Variant B, "No": just close (stays not enabled, stays "bailed").
function hanimeAgeDecline() { _hanimeAgeClose(); }
// Variant A "No, I am 18 or older"  AND  variant B "Yes": actually enable.
function hanimeAgeConfirm() {
  _closeHanimeAgeModal();
  const el = document.getElementById("sourceEnabledHanime");
  if (el) el.checked = true;
  _commitSourceEnabled("hanime", true);
}

function saveSourcesHideInSearch() {
  const el = document.getElementById("sourcesHideInSearch");
  if (!el) return;
  _sourceState.hide_in_search = el.checked;
  _putSettings({ sources_hide_in_search: el.checked },
    el.checked ? t("Deaktivierte Quellen aus Suche ausgeblendet", "Disabled sources hidden from search")
               : t("Deaktivierte Quellen in Suche sichtbar", "Disabled sources shown in search"));
}

// ─── Provider order & fallback (Sources tab) ─────────────────────────────
// The order the download queue walks when a hoster fails: the hoster picked
// for the download is tried first (with its normal retries), then every other
// hoster in this list, top to bottom — see web/queue_worker.py's
// _build_attempt_plan() and runtime_state.get_provider_fallback_chain().
// The list itself is whatever WORKING_PROVIDERS reports, so a newly enabled
// hoster shows up here on its own.
let _providerState = { available: [], order: [], fallback: true };

function _loadProviderSettings(providers) {
  _providerState.available = providers.available || [];
  _providerState.order = (providers.order || []).slice();
  _providerState.fallback = providers.fallback_enabled !== "0";

  // Guard against a stale saved order: drop unknown names, append new hosters.
  _providerState.order = _providerState.order.filter(p => _providerState.available.indexOf(p) !== -1);
  _providerState.available.forEach(p => {
    if (_providerState.order.indexOf(p) === -1) _providerState.order.push(p);
  });

  const cb = document.getElementById("providerFallbackEnabled");
  if (cb) cb.checked = _providerState.fallback;
  _renderProviderOrder();
}

function _renderProviderOrder() {
  const list = document.getElementById("providerOrderList");
  if (!list) return;
  list.innerHTML = "";
  _providerState.order.forEach((prov, idx) => {
    const row = document.createElement("div");
    row.className = "source-order-row";
    row.setAttribute("draggable", "true");
    row.dataset.provider = prov;
    row.innerHTML =
      '<span class="source-drag-handle" title="' + t("Ziehen zum Sortieren", "Drag to reorder") + '" aria-hidden="true">' +
        '<svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor"><circle cx="7" cy="5" r="1.5"/><circle cx="13" cy="5" r="1.5"/><circle cx="7" cy="10" r="1.5"/><circle cx="13" cy="10" r="1.5"/><circle cx="7" cy="15" r="1.5"/><circle cx="13" cy="15" r="1.5"/></svg>' +
      '</span>' +
      '<span class="source-badge">' + (idx + 1) + '. ' + prov + '</span>' +
      '<div class="source-order-actions">' +
        '<button type="button" class="source-move-btn" title="' + t("Nach oben", "Move up") + '" ' + (idx === 0 ? "disabled" : "") + ' onclick="moveProvider(\'' + prov + '\',-1)">' +
          '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg>' +
        '</button>' +
        '<button type="button" class="source-move-btn" title="' + t("Nach unten", "Move down") + '" ' + (idx === _providerState.order.length - 1 ? "disabled" : "") + ' onclick="moveProvider(\'' + prov + '\',1)">' +
          '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>' +
        '</button>' +
      '</div>';
    _attachProviderDnd(row);
    list.appendChild(row);
  });
}

let _dragProvider = null;
function _attachProviderDnd(row) {
  row.addEventListener("dragstart", (e) => {
    _dragProvider = row.dataset.provider;
    row.classList.add("dragging");
    try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", _dragProvider); } catch (err) {}
  });
  row.addEventListener("dragend", () => {
    _dragProvider = null;
    row.classList.remove("dragging");
    document.querySelectorAll("#providerOrderList .source-order-row.drag-over").forEach(r => r.classList.remove("drag-over"));
  });
  row.addEventListener("dragover", (e) => {
    e.preventDefault();
    try { e.dataTransfer.dropEffect = "move"; } catch (err) {}
    if (row.dataset.provider !== _dragProvider) row.classList.add("drag-over");
  });
  row.addEventListener("dragleave", () => row.classList.remove("drag-over"));
  row.addEventListener("drop", (e) => {
    e.preventDefault();
    row.classList.remove("drag-over");
    const target = row.dataset.provider;
    if (!_dragProvider || _dragProvider === target) return;
    const order = _providerState.order;
    const from = order.indexOf(_dragProvider);
    const to = order.indexOf(target);
    if (from === -1 || to === -1) return;
    order.splice(from, 1);
    order.splice(to, 0, _dragProvider);
    _renderProviderOrder();
    _saveProviderOrder();
  });
}

function moveProvider(prov, dir) {
  const order = _providerState.order;
  const i = order.indexOf(prov);
  const j = i + dir;
  if (i === -1 || j < 0 || j >= order.length) return;
  const tmp = order[i]; order[i] = order[j]; order[j] = tmp;
  _renderProviderOrder();
  _saveProviderOrder();
}

function _saveProviderOrder() {
  _putSettings({ provider_order: _providerState.order.join(",") },
    t("Provider-Reihenfolge gespeichert", "Provider order saved"));
}

function saveProviderFallbackEnabled() {
  const el = document.getElementById("providerFallbackEnabled");
  if (!el) return;
  _providerState.fallback = el.checked;
  _putSettings({ provider_fallback_enabled: el.checked },
    el.checked ? t("Provider-Fallback aktiv", "Provider fallback enabled")
               : t("Provider-Fallback deaktiviert", "Provider fallback disabled"));
}

// ─── Domain fallback / mirrors (Sources tab) ─────────────────────────────
// One textarea per site: the ordered list of interchangeable domains for it
// (see mediaforge/mirrors.py). The first line is the primary domain and is
// rendered read-only — every URL inside MediaForge is written with it, only
// the actual HTTP request is redirected to a healthy mirror.
let _mirrorSites = [];

function _loadMirrorSettings(mirrors) {
  _mirrorSites = mirrors.sites || [];
  _renderMirrorSites();
}

function _renderMirrorSites() {
  const box = document.getElementById("mirrorSitesList");
  if (!box) return;
  box.innerHTML = "";
  _mirrorSites.forEach(site => {
    const wrap = document.createElement("div");
    wrap.className = "mirror-site";
    const fallbacks = (site.hosts || []).slice(1);
    const activeNote = site.active && site.active !== site.canonical
      ? '<span class="mirror-active-badge">' + t("aktiv: ", "active: ") + site.active + '</span>'
      : '';
    wrap.innerHTML =
      '<div class="mirror-site-head">' +
        '<span class="mirror-site-label">' + site.label + '</span>' +
        '<span class="mirror-site-primary">' + site.canonical + '</span>' +
        activeNote +
      '</div>' +
      '<textarea class="mirror-hosts" id="mirrorHosts-' + site.id + '" rows="' + Math.max(2, fallbacks.length + 1) + '" ' +
        'placeholder="' + t("z. B. serienstream.to", "e.g. serienstream.to") + '">' + fallbacks.join("\n") + '</textarea>' +
      '<div class="mirror-site-actions">' +
        '<button type="button" class="btn btn-secondary btn-sm" onclick="resetMirrorHosts(\'' + site.id + '\')">' + t("Standard", "Default") + '</button>' +
        '<button type="button" class="btn btn-primary btn-sm" onclick="saveMirrorHosts(\'' + site.id + '\')">' + t("Speichern", "Save") + '</button>' +
      '</div>';
    box.appendChild(wrap);
  });
}

function _mirrorPayload(siteId, fallbackLines) {
  const site = _mirrorSites.find(s => s.id === siteId);
  const hosts = [site.canonical].concat(
    fallbackLines.map(l => l.trim()).filter(Boolean)
  );
  const payload = {};
  payload["site_mirrors_" + siteId] = hosts.join(",");
  return { payload: payload, hosts: hosts };
}

async function saveMirrorHosts(siteId) {
  const ta = document.getElementById("mirrorHosts-" + siteId);
  if (!ta) return;
  const { payload, hosts } = _mirrorPayload(siteId, ta.value.split("\n"));
  const ok = await _putSettings(payload, t("Mirrors gespeichert", "Mirrors saved"));
  if (ok) {
    const site = _mirrorSites.find(s => s.id === siteId);
    if (site) { site.hosts = hosts; site.active = site.canonical; }
    _renderMirrorSites();
  }
}

async function resetMirrorHosts(siteId) {
  const site = _mirrorSites.find(s => s.id === siteId);
  if (!site) return;
  const { payload, hosts } = _mirrorPayload(siteId, (site.default || []).slice(1));
  const ok = await _putSettings(payload, t("Standard wiederhergestellt", "Defaults restored"));
  if (ok) {
    site.hosts = hosts;
    site.active = site.canonical;
    _renderMirrorSites();
  }
}

// ─── Full & Selective Backup (admin only) ─────────────────────────────────
// Bilingual UI strings via the global t(de, en) helper from base.html.

const BACKUP_CAT_LABELS = {
  settings:       function () { return t("Einstellungen", "Settings"); },
  favourites:     function () { return t("Favoriten & Bibliothek", "Favorites & library"); },
  history:        function () { return t("Download-Verlauf", "Download history"); },
  watch_progress: function () { return t("Wiedergabe-Fortschritt", "Watch progress"); },
  custom_paths:   function () { return t("Eigene Pfade", "Custom paths"); },
  users:          function () { return t("Benutzerkonten", "User accounts"); },
  queues:         function () { return t("Warteschlangen & Jobs", "Queues & jobs"); },
  calendar:       function () { return t("Kalender", "Calendar"); },
  push:           function () { return t("Push-Abos", "Push subscriptions"); },
};

function _backupCatLabel(id) {
  return BACKUP_CAT_LABELS[id] ? BACKUP_CAT_LABELS[id]() : id;
}

function _backupRenderCats(container, cats) {
  container.innerHTML = "";
  cats.forEach(function (c) {
    const wrap = document.createElement("label");
    wrap.className = "settings-checkbox-row backup-cat";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = c.id;
    cb.className = "chb-main backup-cat-cb";
    cb.checked = !!c.default;
    const span = document.createElement("span");
    let label = _backupCatLabel(c.id);
    if (typeof c.count === "number") label += " (" + c.count + ")";
    span.textContent = label;
    wrap.appendChild(cb);
    wrap.appendChild(span);
    container.appendChild(wrap);
  });
}

function _backupSelectedCats(container) {
  return Array.prototype.map.call(
    container.querySelectorAll(".backup-cat-cb:checked"),
    function (cb) { return cb.value; }
  );
}

async function backupLoadCats() {
  const box = document.getElementById("backupExportCats");
  if (!box) return;
  try {
    const res = await fetch("/api/backup/categories");
    const data = await res.json();
    if (data.error) { box.innerHTML = ""; showToast(data.error); return; }
    _backupRenderCats(box, data.categories || []);
  } catch (e) {
    box.innerHTML = "";
    showToast(t("Backup-Kategorien konnten nicht geladen werden", "Could not load backup categories"));
  }
}

function backupToggleNoPassword() {
  // Disable/clear the password fields when "save without password" is on.
  const on = (document.getElementById("backupNoPassword") || {}).checked;
  const row = document.getElementById("backupPwRow");
  if (row) row.style.display = on ? "none" : "";
  ["backupExportPw", "backupExportPw2"].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) { el.disabled = !!on; if (on) el.value = ""; }
  });
}

async function backupExport() {
  const noPw = (document.getElementById("backupNoPassword") || {}).checked;
  const pw = (document.getElementById("backupExportPw") || {}).value || "";
  const pw2 = (document.getElementById("backupExportPw2") || {}).value || "";
  const msg = document.getElementById("backupExportMsg");
  const cats = _backupSelectedCats(document.getElementById("backupExportCats"));
  if (!cats.length) { showToast(t("Keine Kategorie ausgewählt", "No category selected")); return; }

  if (!noPw) {
    if (!pw) { showToast(t("Bitte ein Backup-Passwort setzen", "Please set a backup password")); return; }
    if (pw !== pw2) { showToast(t("Passwörter stimmen nicht überein", "Passwords do not match")); return; }
  } else {
    // Danger confirmation: an unencrypted backup exposes all secrets.
    const warn = t(
      "Ohne Passwort wird das Backup UNVERSCHLÜSSELT gespeichert. Alle sensiblen Daten — API-Schlüssel, Zugangsdaten, Tokens und Passwörter — liegen dann im Klartext in der Datei. Jeder mit Zugriff auf die Datei kann sie lesen. Bewahre sie niemals ungeschützt (Cloud, E-Mail, geteilte Ordner) auf. Wirklich ohne Passwort fortfahren?",
      "Without a password the backup is stored UNENCRYPTED. All sensitive data — API keys, credentials, tokens and passwords — will be in plain text in the file. Anyone with access to the file can read it. Never store it unprotected (cloud, email, shared folders). Really continue without a password?"
    );
    const ok = (typeof showConfirm === "function")
      ? await showConfirm(warn, t("Ohne Passwort sichern", "Save without password"), t("Achtung", "Warning"), "btn-danger")
      : confirm(warn);
    if (!ok) return;
  }

  const btn = document.getElementById("backupExportBtn");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/backup/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ categories: cats, password: noPw ? "" : pw, no_password: !!noPw }),
    });
    if (!res.ok) {
      let err = t("Export fehlgeschlagen", "Export failed");
      try { const j = await res.json(); if (j.error) err = j.error; } catch (e) { }
      showToast(err);
      return;
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    let fname = "mediaforge-backup.mfbackup";
    const m = /filename="?([^"]+)"?/.exec(cd);
    if (m) fname = m[1];
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    if (msg) msg.textContent = t("Backup erstellt: ", "Backup created: ") + fname;
  } catch (e) {
    showToast(t("Export fehlgeschlagen: ", "Export failed: ") + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

function backupOnFileChange() {
  // Reflect the chosen filename in the styled picker and hide stale preview.
  const input = document.getElementById("backupImportFile");
  const label = document.getElementById("backupFileLabel");
  if (label) {
    const f = input && input.files && input.files[0];
    label.textContent = f ? f.name : t("Datei wählen…", "Choose file…");
  }
  const box = document.getElementById("backupPreviewBox");
  if (box) box.style.display = "none";
}

async function backupPreview() {
  const fileInput = document.getElementById("backupImportFile");
  const pw = (document.getElementById("backupImportPw") || {}).value || "";
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!file) { showToast(t("Bitte eine Backup-Datei wählen", "Please choose a backup file")); return; }
  const btn = document.getElementById("backupPreviewBtn");
  if (btn) btn.disabled = true;
  try {
    // Upload as a JSON body (the .mfbackup file is JSON text) — every /api/
    // POST must be application/json; see backup.py route docstring.
    const text = await file.text();
    const res = await fetch("/api/backup/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: text, password: pw }),
    });
    const data = await res.json();
    if (data.error) { showToast(data.error); return; }
    const info = document.getElementById("backupPreviewInfo");
    const created = data.created_utc || "?";
    const ver = data.app_version || "?";
    let pwNote = "";
    if (data.encrypted === false) pwNote = " — " + t("⚠ Unverschlüsseltes Backup (kein Passwort nötig)", "⚠ unencrypted backup (no password needed)");
    else if (data.password_ok === false) pwNote = " — " + t("⚠ Passwort falsch oder fehlt", "⚠ wrong or missing password");
    else if (data.password_ok === true) pwNote = " — " + t("✓ Passwort ok", "✓ password ok");
    info.textContent = t("Erstellt: ", "Created: ") + created + " · " + t("Version: ", "Version: ") + ver + pwNote;
    const cats = (data.categories || []).map(function (id) {
      return { id: id, count: (data.counts || {})[id], default: true };
    });
    _backupRenderCats(document.getElementById("backupImportCats"), cats);
    document.getElementById("backupPreviewBox").style.display = "";
  } catch (e) {
    showToast(t("Backup konnte nicht gelesen werden: ", "Could not read backup: ") + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function backupImport() {
  const fileInput = document.getElementById("backupImportFile");
  const pw = (document.getElementById("backupImportPw") || {}).value || "";
  const file = fileInput && fileInput.files && fileInput.files[0];
  if (!file) { showToast(t("Bitte eine Backup-Datei wählen", "Please choose a backup file")); return; }
  if (!pw) { showToast(t("Bitte das Backup-Passwort eingeben", "Please enter the backup password")); return; }
  const cats = _backupSelectedCats(document.getElementById("backupImportCats"));
  if (!cats.length) { showToast(t("Keine Kategorie ausgewählt", "No category selected")); return; }
  const mode = (document.querySelector('input[name="backupMode"]:checked') || {}).value || "merge";
  if (mode === "replace" &&
      !confirm(t("„Ersetzen“ löscht die ausgewählten Daten vor dem Import unwiderruflich. Fortfahren?",
                 "'Replace' will irreversibly clear the selected data before import. Continue?"))) {
    return;
  }
  const btn = document.getElementById("backupImportBtn");
  if (btn) btn.disabled = true;
  try {
    const text = await file.text();
    const res = await fetch("/api/backup/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: text, password: pw, mode: mode, categories: cats }),
    });
    const data = await res.json();
    if (data.error) { showToast(data.error); return; }
    const msg = document.getElementById("backupImportMsg");
    let total = 0;
    Object.keys(data.imported || {}).forEach(function (k) { total += data.imported[k]; });
    if (msg) msg.textContent = t("Wiederhergestellt: ", "Restored: ") + total + t(" Einträge", " entries");
    showToast(t("Backup wiederhergestellt", "Backup restored"), "success");
  } catch (e) {
    showToast(t("Import fehlgeschlagen: ", "Import failed: ") + e.message);
  } finally {
    if (btn) btn.disabled = false;
  }
}

document.addEventListener("DOMContentLoaded", function () {
  if (document.getElementById("tab-backup")) backupLoadCats();
});
