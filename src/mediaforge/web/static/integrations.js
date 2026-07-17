// ─── Tab Navigation ────────────────────────────────────────────────────────

function switchIntegTab(name) {
  document.querySelectorAll("#integTabs .settings-tab").forEach(function (btn) {
    btn.classList.toggle("active", btn.dataset.tab === name);
  });
  document.querySelectorAll(".settings-tab-panel").forEach(function (panel) {
    panel.classList.toggle("active", panel.id === "tab-" + name);
  });
  var subContainer = document.getElementById("integrationsSidebarSub");
  if (subContainer) subContainer.classList.add("open");
  var toggleBtn = document.getElementById("integrationsSidebarToggle");
  if (toggleBtn) toggleBtn.classList.remove("collapsed");
  document.querySelectorAll(".sidebar-sub-link[data-integ-tab]").forEach(function (a) {
    a.classList.toggle("active", a.dataset.integTab === name);
  });
  try {
    history.replaceState(null, "", "#" + name);
    localStorage.setItem("integActiveTab", name);
  } catch (e) {}
}

(function restoreIntegTab() {
  var hash = "";
  try { hash = (window.location.hash || "").replace("#", "").trim(); } catch (e) {}
  // Read from the DOM instead of a hardcoded list, so tabs an extension
  // registers dynamically (see registry.py's resolve_dynamic_tabs(), which
  // adds extra buttons to #integTabs at render time) are restorable via
  // #hash exactly like the built-in ones.
  var valid = Array.prototype.map.call(
    document.querySelectorAll("#integTabs .settings-tab"),
    function (btn) { return btn.dataset.tab; }
  );
  // Honor a valid #hash deep-link (e.g. from a sidebar sub-link), otherwise
  // always start on the overview — matching settings.js's restoreTab(). We
  // deliberately do NOT restore the last tab from localStorage here, so that
  // opening the main "Integrations" entry shows the overview rather than the
  // previously viewed sub-tab.
  var tab = (hash && valid.indexOf(hash) !== -1) ? hash : "overview";
  if (valid.indexOf(tab) === -1) tab = "overview";
  switchIntegTab(tab);
})();

// ─── Deep link from the Modulmanager ("Open module" button) ───────────────
// extensions.html links here as .../integrations?open=<item_id>#<tab> — the
// #tab half is already handled by restoreIntegTab() above (tab ids are read
// generically off the DOM, so this works for dynamic tabs too); this part
// additionally force-expands that one item's settings card (overriding its
// collapsed-by-default state) and scrolls it into view, so "Open module"
// actually lands the admin on the right field instead of just the right tab.
(function openDeepLinkedThirdpartyCard() {
  var openId = "";
  try { openId = new URLSearchParams(window.location.search).get("open") || ""; } catch (e) {}
  if (!openId) return;
  document.addEventListener("DOMContentLoaded", function () {
    // Deferred a tick: extension_cards.js's restoreIntegCollapse() (which
    // this overrides for this one card) runs synchronously as its own
    // script loads, but giving layout a moment to settle first makes the
    // scrollIntoView land correctly even on slower first paints.
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

// ─── Third Party tab: collapsible integration cards ───────────────────────
// toggleIntegCollapse/restoreIntegCollapse and the thirdparty-toggle load/
// save helpers used to live here; they moved to static/extension_cards.js
// (included from integrations.html and notifications.html) so extension
// cards behave identically wherever they're rendered, not just here. See
// that file for the implementation.

// ===== Settings Caching Helper =====
let _combinedSettingsPromise = null;
function _getSettings() {
  if (!_combinedSettingsPromise) {
    _combinedSettingsPromise = (async () => {
      try {
        const resp = await fetch("/api/settings");
        return await resp.json();
      } catch (e) {
        console.error("Failed to load settings:", e);
        return {};
      }
    })();
  }
  return _combinedSettingsPromise;
}

// ===== Seerr =====
async function loadIntegrations() {
  try {
    const data = await _getSettings();
    const el1 = document.getElementById("seerrUrl");
    const el2 = document.getElementById("seerrApiKey");
    if (el1) el1.value = data.seerr_url || "";
    if (el2) el2.value = data.seerr_api_key || "";
  } catch (e) {
    showToast(t("Einstellungen konnten nicht geladen werden: ", "Settings could not be loaded: ") + e.message);
  }
}

function _seerrIsConfigured() {
  const url = (document.getElementById("seerrUrl")?.value || "").trim();
  const key = (document.getElementById("seerrApiKey")?.value || "").trim();
  return !!(url && key);
}

async function saveSeerrSettings() {
  const url = (document.getElementById("seerrUrl")?.value || "").trim();
  const key = (document.getElementById("seerrApiKey")?.value || "").trim();
  try {
    const resp = await fetch("/api/settings/seerr", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ seerr_url: url, seerr_api_key: key }),
    });
    const data = await resp.json();
    if (data.ok) {
      showToast(t("Seerr-Einstellungen gespeichert", "Seerr settings saved"));
      // Configuration may have changed — re-evaluate the calendar Seerr sub-option
      window._seerrConfigured = _seerrIsConfigured();
      if (typeof _applyCalendarSeerrState === "function") _applyCalendarSeerrState();
    } else {
      showToast(data.error || "Fehler beim Speichern");
    }
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message));
  }
}

// ===== CineInfo =====
async function loadCineinfoSettings() {
  try {
    const data = await _getSettings();
    const d = data.cineinfo || {};

    const keyEl = document.getElementById("cineinfoApiKey");
    const countryEl = document.getElementById("cineinfoCountry");
    const provEl = document.getElementById("cineinfoShowProviders");
    const fskEl = document.getElementById("cineinfoShowFsk");
    const genresEl = document.getElementById("cineinfoShowGenres");
    const ratingEl = document.getElementById("cineinfoShowRating");
    const recEl = document.getElementById("cineinfoShowRecommendations");
    const trailerEl = document.getElementById("cineinfoShowTrailer");
    const hRatingEl = document.getElementById("cineinfoShowHoverRating");
    const hGenresEl = document.getElementById("cineinfoShowHoverGenres");
    const hFskEl = document.getElementById("cineinfoShowHoverFsk");
    const advancedSearchEl = document.getElementById("cineinfoAdvancedSearch");
    const calendarEl = document.getElementById("cineinfoCalendar");

    if (keyEl) keyEl.value = d.tmdb_api_key || "";
    if (countryEl) countryEl.value = d.country || "DE";
    if (provEl) provEl.checked = d.show_providers !== "0";
    if (fskEl) fskEl.checked = d.show_fsk !== "0";
    if (genresEl) genresEl.checked = d.show_genres === "1";
    if (ratingEl) ratingEl.checked = d.show_rating === "1";
    if (recEl) recEl.checked = d.show_recommendations !== "0";
    if (trailerEl) trailerEl.checked = d.show_trailer !== "0";
    if (hRatingEl) hRatingEl.checked = d.show_hover_rating === "1";
    if (hGenresEl) hGenresEl.checked = d.show_hover_genres === "1";
    if (hFskEl) hFskEl.checked = d.show_hover_fsk === "1";
    _loadPillOrder(d.provider_order || "");
    if (advancedSearchEl) {
      advancedSearchEl.checked = d.advanced_search === "1";
      const sidebarAdvancedSearch = document.getElementById("sidebarAdvancedSearch");
      if (sidebarAdvancedSearch) {
        sidebarAdvancedSearch.style.display = d.advanced_search === "1" ? "flex" : "none";
      }
    }
    if (calendarEl) {
      calendarEl.checked = d.calendar === "1";
      const sidebarCalendar = document.getElementById("sidebarCalendar");
      if (sidebarCalendar) {
        sidebarCalendar.style.display = d.calendar === "1" ? "flex" : "none";
      }
    }
    const calSeerrEl = document.getElementById("cineinfoCalendarSeerr");
    if (calSeerrEl) calSeerrEl.checked = d.calendar_seerr === "1";
    const calMediathekEl = document.getElementById("cineinfoCalendarMediathek");
    if (calMediathekEl) calMediathekEl.checked = d.calendar_mediathek === "1";
    const calIntervalEl = document.getElementById("cineinfoCalendarRefreshInterval");
    if (calIntervalEl) calIntervalEl.value = d.calendar_refresh_interval || "24";

    // Remember whether Seerr is configured (server-evaluated) for the gating check
    window._seerrConfigured = !!data.seerr_configured;
    _applyCalendarSeerrState();
  } catch (e) {
    showToast(t("CineInfo-Einstellungen konnten nicht geladen werden: ", "CineInfo-Settings could not be loaded: ") + e.message);
  }
}

// The "Show Seerr requests in calendar" sub-option is only changeable when the
// calendar is enabled AND a Seerr integration is configured.
function _applyCalendarSeerrState() {
  const calEl = document.getElementById("cineinfoCalendar");
  const subEl = document.getElementById("cineinfoCalendarSeerr");
  const hint = document.getElementById("cineinfoCalendarSeerrHint");
  const mediathekEl = document.getElementById("cineinfoCalendarMediathek");
  const intervalEl = document.getElementById("cineinfoCalendarRefreshInterval");

  const calendarOn = !!(calEl && calEl.checked);
  if (mediathekEl) mediathekEl.disabled = !calendarOn;
  if (intervalEl) intervalEl.disabled = !calendarOn;

  if (!subEl) return;
  const seerrOk = (typeof _seerrIsConfigured === "function" && _seerrIsConfigured()) || !!window._seerrConfigured;
  const enabled = calendarOn && seerrOk;
  subEl.disabled = !enabled;
  if (hint) hint.style.display = enabled ? "none" : "block";
}

async function saveCineinfoSettings() {
  const key = (document.getElementById("cineinfoApiKey")?.value || "").trim();
  const country = (document.getElementById("cineinfoCountry")?.value || "DE");
  try {
    const resp = await fetch("/api/settings/cineinfo", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tmdb_api_key: key, country }),
    });
    const data = await resp.json();
    showToast(data.ok ? t("CineInfo gespeichert","CineInfo saved") : (data.error || t("Fehler", "Error")));
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message));
  }
}

async function saveCineinfoDisplayOptions() {
  const show_providers = document.getElementById("cineinfoShowProviders")?.checked ? "1" : "0";
  const show_fsk = document.getElementById("cineinfoShowFsk")?.checked ? "1" : "0";
  const show_genres = document.getElementById("cineinfoShowGenres")?.checked ? "1" : "0";
  const show_rating = document.getElementById("cineinfoShowRating")?.checked ? "1" : "0";
  const show_recommendations = document.getElementById("cineinfoShowRecommendations")?.checked ? "1" : "0";
  const show_trailer = document.getElementById("cineinfoShowTrailer")?.checked ? "1" : "0";
  const show_hover_rating = document.getElementById("cineinfoShowHoverRating")?.checked ? "1" : "0";
  const show_hover_genres = document.getElementById("cineinfoShowHoverGenres")?.checked ? "1" : "0";
  const show_hover_fsk = document.getElementById("cineinfoShowHoverFsk")?.checked ? "1" : "0";
  const advanced_search = document.getElementById("cineinfoAdvancedSearch")?.checked ? "1" : "0";
  const calendar = document.getElementById("cineinfoCalendar")?.checked ? "1" : "0";
  // Sub-option: only meaningful when the calendar is on and Seerr is configured
  const calSeerrEl = document.getElementById("cineinfoCalendarSeerr");
  const calendar_seerr = (calendar === "1" && calSeerrEl && calSeerrEl.checked && !calSeerrEl.disabled) ? "1" : "0";
  
  const calMediathekEl = document.getElementById("cineinfoCalendarMediathek");
  const calendar_mediathek = (calendar === "1" && calMediathekEl && calMediathekEl.checked) ? "1" : "0";
  const calIntervalEl = document.getElementById("cineinfoCalendarRefreshInterval");
  const calendar_refresh_interval = (calIntervalEl && calIntervalEl.value) || "24";

  // Instantly toggle sidebar menu link visibility
  const sidebarAdvancedSearch = document.getElementById("sidebarAdvancedSearch");
  if (sidebarAdvancedSearch) {
    sidebarAdvancedSearch.style.display = advanced_search === "1" ? "flex" : "none";
  }
  const sidebarCalendar = document.getElementById("sidebarCalendar");
  if (sidebarCalendar) {
    sidebarCalendar.style.display = calendar === "1" ? "flex" : "none";
  }
  // Re-evaluate the sub-option's enabled state (e.g. calendar was just toggled)
  _applyCalendarSeerrState();

  try {
    await fetch("/api/settings/cineinfo", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        show_providers, show_fsk, show_genres, show_rating, show_recommendations, show_trailer,
        show_hover_rating, show_hover_genres, show_hover_fsk, advanced_search, calendar,
        calendar_seerr, calendar_mediathek, calendar_refresh_interval
      }),
    });
  } catch (e) { /* silent */ }
}

// ===== CineInfo Cache =====
async function clearCineinfoCache(btn) {
  if (btn) {
    btn.disabled = true;
    btn.textContent = t("Wird geleert…", "Clearing…");
  }
  try {
    const resp = await fetch("/api/tmdb/cache/clear", { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      showToast(t("Cache geleert — TMDB-Daten werden im Hintergrund neu geladen", "Cache cleared — TMDB data will be reloaded in the background"));
    } else {
      showToast(t("Fehler beim Leeren des Caches", "Error clearing cache"), "error");
    }
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message), "error");
  } finally {
    if (btn) {
      // Re-enable after a short delay so the user sees feedback
      setTimeout(() => {
        btn.disabled = false;
        btn.textContent = t("Cache leeren", "Clear cache");
      }, 2000);
    }
  }
}

// ===== Crunchyroll =====
async function loadCrunchyrollSettings() {
  try {
    const data = await _getSettings();
    const d = data.crunchyroll || {};
    window.__tmdbKeySet = !!(data.cineinfo && data.cineinfo.tmdb_api_key);
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.value = v; };
    const chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = v; };

    chk("crEnabled", d.enabled === "1");
    set("crEmail", d.email || "");
    set("crLocale", d.locale || "de-DE");
    chk("crAnon", d.anon === "1");
    chk("crShowProviders", d.show_providers !== "0");
    chk("crCalSimulcast", d.calendar_simulcast === "1");
    chk("crCalWatchlist", d.calendar_watchlist === "1");
    chk("crCalLists", d.calendar_lists === "1");
    window.__crProfileId = d.profile_id || "";
    // Real (non-anon) account configured? Lets the watchlist/list calendar
    // toggles stay usable even when the master display toggle is off.
    window.__crHasAccount = !!((d.email || "") && d.has_password);

    // Password is never returned; only show whether one is stored.
    const pw = document.getElementById("crPassword");
    if (pw) {
      pw.value = "";
      pw.placeholder = d.has_password
        ? t("•••••••• (gespeichert)", "•••••••• (saved)")
        : t("Wird verschlüsselt gespeichert", "Stored encrypted");
    }
    _applyCrunchyrollState();
    // Populate the profile selector if we have an account login configured.
    if (d.enabled === "1" && d.anon !== "1" && (d.email || "")) {
      _loadCrProfiles(window.__crProfileId);
    }
  } catch (e) {
    showToast(t("Crunchyroll-Einstellungen konnten nicht geladen werden: ", "Crunchyroll settings could not be loaded: ") + e.message);
  }
}

// Fill the profile dropdown from the account; called on load and after a test.
async function _loadCrProfiles(selectedId) {
  try {
    const resp = await fetch("/api/settings/crunchyroll/profiles");
    const d = await resp.json();
    _populateCrProfiles(d.profiles || [], selectedId);
  } catch (e) { /* silent */ }
}

function _populateCrProfiles(profiles, selectedId) {
  const sel = document.getElementById("crProfile");
  const row = document.getElementById("crProfileRow");
  if (!sel || !row) return;
  if (!profiles.length) { row.style.display = "none"; return; }
  const want = selectedId || window.__crProfileId || "";
  sel.innerHTML = "";
  profiles.forEach(function (p) {
    const opt = document.createElement("option");
    opt.value = p.id;
    opt.textContent = p.name + (p.is_primary ? " " + t("(Haupt)", "(primary)") : "");
    if (p.id === want) opt.selected = true;
    sel.appendChild(opt);
  });
  window.__crProfileId = sel.value;
  const anon = !!document.getElementById("crAnon")?.checked;
  const enabled = !!document.getElementById("crEnabled")?.checked;
  row.style.display = (enabled && !anon) ? "flex" : "none";
}

// Enable/disable Crunchyroll inputs depending on the master + anonymous toggles.
function _applyCrunchyrollState() {
  const enabled = !!document.getElementById("crEnabled")?.checked;
  const anon = !!document.getElementById("crAnon")?.checked;
  ["crEmail", "crPassword", "crLocale", "crAnon", "crShowProviders",
   "crCalSimulcast", "crTestBtn"].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.disabled = !enabled;
  });
  // Account-only inputs are pointless in anonymous mode.
  ["crEmail", "crPassword", "crProfile"].forEach(function (id) {
    const el = document.getElementById(id);
    if (el && enabled) el.disabled = anon;
  });
  const profileRow = document.getElementById("crProfileRow");
  if (profileRow && (!enabled || anon)) profileRow.style.display = "none";

  // Calendar sync needs TMDB (CineInfo) for the episode dates.
  const tmdbOk = !!window.__tmdbKeySet;
  // Simulcast sync is tied to the master Crunchyroll display toggle.
  const simEl = document.getElementById("crCalSimulcast");
  if (simEl) simEl.disabled = !enabled || !tmdbOk;
  // Watchlist & custom-list sync are personal-account features: they work even
  // with the master display toggle off, as long as a real (non-anonymous)
  // account is configured and TMDB is set.
  const hasAccount = !!window.__crHasAccount;
  ["crCalWatchlist", "crCalLists"].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.disabled = !tmdbOk || anon || (!enabled && !hasAccount);
  });
  const hint = document.getElementById("crCalTmdbHint");
  if (hint) hint.style.display = ((enabled || hasAccount) && !tmdbOk) ? "block" : "none";
}

async function saveCrunchyrollSettings() {
  const email = (document.getElementById("crEmail")?.value || "").trim();
  const locale = (document.getElementById("crLocale")?.value || "de-DE");
  const pwEl = document.getElementById("crPassword");
  const password = (pwEl?.value || "").trim();
  const body = { email, locale };
  if (password) body.password = password;     // only send when actually typed
  try {
    const resp = await fetch("/api/settings/crunchyroll", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.ok) {
      showToast(t("Crunchyroll gespeichert", "Crunchyroll saved"));
      if (pwEl && password) {
        pwEl.value = "";
        pwEl.placeholder = t("•••••••• (gespeichert)", "•••••••• (saved)");
      }
    } else {
      showToast(data.error || t("Fehler", "Error"));
    }
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message));
  }
}

// Persist the toggle-style options (and the locale) immediately on change.
async function saveCrunchyrollOptions() {
  _applyCrunchyrollState();
  const body = {
    enabled:            document.getElementById("crEnabled")?.checked ? "1" : "0",
    anon:               document.getElementById("crAnon")?.checked ? "1" : "0",
    show_providers:     document.getElementById("crShowProviders")?.checked ? "1" : "0",
    calendar_simulcast: document.getElementById("crCalSimulcast")?.checked ? "1" : "0",
    calendar_watchlist: document.getElementById("crCalWatchlist")?.checked ? "1" : "0",
    calendar_lists: document.getElementById("crCalLists")?.checked ? "1" : "0",
    locale:             document.getElementById("crLocale")?.value || "de-DE",
    profile_id:         document.getElementById("crProfile")?.value || "",
  };
  window.__crProfileId = body.profile_id;
  try {
    await fetch("/api/settings/crunchyroll", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) { /* silent */ }
}

async function testCrunchyroll(btn) {
  const result = document.getElementById("crTestResult");
  if (btn) { btn.disabled = true; btn.textContent = t("Teste…", "Testing…"); }
  const body = {
    email:    (document.getElementById("crEmail")?.value || "").trim(),
    locale:   document.getElementById("crLocale")?.value || "de-DE",
    anon:     document.getElementById("crAnon")?.checked ? "1" : "0",
  };
  body.profile_id = document.getElementById("crProfile")?.value || window.__crProfileId || "";
  const pw = (document.getElementById("crPassword")?.value || "").trim();
  if (pw) body.password = pw;
  try {
    const resp = await fetch("/api/settings/crunchyroll/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await resp.json();
    if (result) {
      result.style.display = "block";
      if (d.ok) {
        let msg = d.mode === "anonymous"
          ? t("✓ Anonyme Verbindung erfolgreich", "✓ Anonymous connection successful")
          : t("✓ Angemeldet", "✓ Logged in");
        if (d.profile) msg += " · " + d.profile;
        if (d.mode === "account") msg += d.premium
          ? " · " + t("Premium aktiv", "Premium active")
          : " · " + t("kein Premium", "no Premium");
        result.textContent = msg;
        result.style.color = "var(--success, #2ecc71)";
        // Bottom toast, like other settings actions.
        showToast(msg, "success");
        // Populate the profile selector with the freshly returned profiles.
        if (Array.isArray(d.profiles) && d.profiles.length) {
          _populateCrProfiles(d.profiles, d.profile_id || window.__crProfileId);
        }
      } else {
        const map = {
          login_failed: t("✗ Login fehlgeschlagen — Zugangsdaten prüfen", "✗ Login failed — check credentials"),
          missing_credentials: t("✗ Bitte E-Mail und Passwort angeben", "✗ Please enter email and password"),
          cloudflare: t("✗ Von Cloudflare blockiert — später erneut versuchen", "✗ Blocked by Cloudflare — try again later"),
          library_unavailable: t("✗ Crunchyroll-Bibliothek nicht verfügbar", "✗ Crunchyroll library unavailable"),
        };
        const errMsg = map[d.error] || t("✗ Verbindung fehlgeschlagen", "✗ Connection failed");
        result.textContent = errMsg;
        result.style.color = "var(--danger, #e74c3c)";
        showToast(errMsg, "error");
      }
    }
  } catch (e) {
    if (result) {
      result.style.display = "block";
      result.textContent = t("✗ Fehler: " + e.message, "✗ Error: " + e.message);
      result.style.color = "var(--danger, #e74c3c)";
    }
  } finally {
    if (btn) {
      setTimeout(() => { btn.disabled = false; btn.textContent = t("Verbindung testen", "Test connection"); _applyCrunchyrollState(); }, 300);
    }
  }
}

// ===== Toast =====
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
  t._hideTimer = setTimeout(() => t.classList.remove("show"), 4000);
}

// ===== SyncPlay =====
async function loadSyncplaySettings() {
  try {
    const data = await _getSettings();
    const en = document.getElementById("spEnabled");
    if (en) en.checked = data.syncplay_enabled === "1";
    _applySyncplayState();
  } catch (e) {}
}

function _applySyncplayState() {
  const on = !!document.getElementById("spEnabled")?.checked;
  const row = document.getElementById("spOpenRow");
  if (row) row.style.display = on ? "" : "none";
}

async function saveSyncplaySettings() {
  const on = !!document.getElementById("spEnabled")?.checked;
  try {
    const resp = await fetch("/api/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ syncplay_enabled: on }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    _applySyncplayState();
    // Reload so the sidebar SyncPlay entry appears/disappears immediately.
    setTimeout(function () { location.reload(); }, 250);
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message);
  }
}


// ===== UpTime =====
const _UPTIME_SOURCES = ["aniworld", "sto", "filmpalast", "megakino", "hanime"];

async function loadUptimeSettings() {
  try {
    const resp = await fetch("/api/uptime/status");
    const data = await resp.json();
    const en = document.getElementById("uptimeEnabled");
    if (en) en.checked = !!data.enabled;
    const iv = document.getElementById("uptimeInterval");
    if (iv) iv.value = Math.max(1, Math.round((data.interval || 300) / 60));
    const rt = document.getElementById("uptimeRetention");
    if (rt) rt.value = data.retention_days || 7;
    const to = document.getElementById("uptimeTimeout");
    if (to) to.value = data.timeout || 15;
    const ft = document.getElementById("uptimeFailureThreshold");
    if (ft) ft.value = data.failure_threshold || 2;
    const ug = document.getElementById("uptimeUseGet");
    if (ug) ug.checked = !!data.use_get;
    const trackedMap = {};
    (data.sources || []).forEach(function (s) { trackedMap[s.id] = !!s.tracked; });
    _UPTIME_SOURCES.forEach(function (sid) {
      const cb = document.getElementById("uptimeTrack_" + sid);
      if (cb) cb.checked = trackedMap[sid] !== undefined ? trackedMap[sid] : (sid !== "hanime");
    });
    _applyUptimeState();
  } catch (e) {}
}

function _applyUptimeState() {
  const on = !!document.getElementById("uptimeEnabled")?.checked;
  const cfg = document.getElementById("uptimeConfig");
  if (cfg) cfg.style.display = on ? "" : "none";
}

async function saveUptimeSettings(reload) {
  const on = !!document.getElementById("uptimeEnabled")?.checked;
  const intervalMin = Math.max(1, parseInt(document.getElementById("uptimeInterval")?.value || "5", 10) || 5);
  const retention = Math.min(7, Math.max(1, parseInt(document.getElementById("uptimeRetention")?.value || "7", 10) || 7));
  const timeout = Math.min(120, Math.max(5, parseInt(document.getElementById("uptimeTimeout")?.value || "15", 10) || 15));
  const failureThreshold = Math.min(10, Math.max(1, parseInt(document.getElementById("uptimeFailureThreshold")?.value || "2", 10) || 2));
  const useGet = !!document.getElementById("uptimeUseGet")?.checked;
  const tracked = {};
  _UPTIME_SOURCES.forEach(function (sid) {
    const cb = document.getElementById("uptimeTrack_" + sid);
    tracked[sid] = !!(cb && cb.checked);
  });
  _applyUptimeState();
  try {
    const resp = await fetch("/api/settings/uptime", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: on,
        interval: intervalMin * 60,
        retention_days: retention,
        timeout: timeout,
        failure_threshold: failureThreshold,
        use_get: useGet,
        tracked: tracked,
      }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error); return; }
    if (reload) {
      // Reload so the sidebar UpTime entry appears/disappears immediately.
      setTimeout(function () { location.reload(); }, 250);
    } else {
      showToast(t("UpTime gespeichert", "UpTime saved"));
    }
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message);
  }
}


document.addEventListener("DOMContentLoaded", () => {

  loadIntegrations();
  loadSyncplaySettings();
  loadUptimeSettings();
  loadCineinfoSettings();
  loadCrunchyrollSettings();
  loadFernsehserienSettings();
  loadMediaplayerSettings();
  loadMediascanSettings();
  // loadThirdpartyToggles() and restoreIntegCollapse() now run on their own
  // from static/extension_cards.js — see the <script> tag in
  // integrations.html.
});

// ===== Fernsehserien.de =====
async function loadFernsehserienSettings() {
  try {
    const data = await _getSettings();
    const d = data.fernsehserien || {};
    const chk = (id, v) => { const el = document.getElementById(id); if (el) el.checked = v; };

    chk("fsEnabled", d.enabled === "1");
    chk("fsShowProviders", d.show_providers !== "0");
    _applyFernsehserienState();
  } catch (e) {
    showToast(t("Fernsehserien-Einstellungen konnten nicht geladen werden: ", "Fernsehserien settings could not be loaded: ") + e.message);
  }
}

function _applyFernsehserienState() {
  const enabled = !!document.getElementById("fsEnabled")?.checked;
  ["fsShowProviders", "fsTestBtn"].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.disabled = !enabled;
  });
}

// Persist the toggle-style options immediately on change.
async function saveFernsehserienOptions() {
  _applyFernsehserienState();
  const body = {
    enabled:        document.getElementById("fsEnabled")?.checked ? "1" : "0",
    show_providers: document.getElementById("fsShowProviders")?.checked ? "1" : "0",
  };
  try {
    await fetch("/api/settings/fernsehserien", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (e) { /* silent */ }
}

async function testFernsehserien(btn) {
  const result = document.getElementById("fsTestResult");
  if (btn) { btn.disabled = true; btn.textContent = t("Teste…", "Testing…"); }
  try {
    const resp = await fetch("/api/settings/fernsehserien/test", { method: "POST" });
    const d = await resp.json();
    if (result) {
      result.style.display = "block";
      if (d.ok) {
        const msg = t("✓ Scraper funktioniert", "✓ Scraper working") + (d.title ? " · " + d.title : "");
        result.textContent = msg;
        result.style.color = "var(--success, #2ecc71)";
        showToast(msg, "success");
      } else {
        const map = {
          library_unavailable: t("✗ Fernsehserien-Bibliothek nicht verfügbar (beautifulsoup4 fehlt?)", "✗ Fernsehserien library unavailable (beautifulsoup4 missing?)"),
          request_failed: t("✗ Seite konnte nicht geladen werden", "✗ Could not load page"),
        };
        const errMsg = map[d.error] || t("✗ Test fehlgeschlagen", "✗ Test failed");
        result.textContent = errMsg;
        result.style.color = "var(--danger, #e74c3c)";
        showToast(errMsg, "error");
      }
    }
  } catch (e) {
    if (result) {
      result.style.display = "block";
      result.textContent = t("✗ Fehler: " + e.message, "✗ Error: " + e.message);
      result.style.color = "var(--danger, #e74c3c)";
    }
  } finally {
    if (btn) {
      setTimeout(() => { btn.disabled = false; btn.textContent = t("Verbindung testen", "Test connection"); _applyFernsehserienState(); }, 300);
    }
  }
}


// ===== Mediaplayer (Jellyfin / Plex) =====
async function loadMediaplayerSettings() {
  try {
    const r = await fetch("/api/settings/mediaplayer");
    const d = await r.json();
    const typeEl = document.getElementById("mediaplayerType");
    if (typeEl) typeEl.value = d.type || "";

    // Jellyfin fields
    const jfUrl = document.getElementById("mediaplayerUrl");
    const jfKey = document.getElementById("mediaplayerApikey");
    const jfSsl = document.getElementById("mediaplayerSsl");
    if (jfUrl) jfUrl.value = _stripScheme(d.url || "");
    if (jfKey) jfKey.value = d.apikey || "";
    if (jfSsl) jfSsl.checked = (d.url || "").startsWith("https://");

    // Plex fields
    const plexUrl = document.getElementById("mediaplayerPlexUrl");
    const plexSect = document.getElementById("mediaplayerPlexSectionId");
    const plexSsl = document.getElementById("mediaplayerPlexSsl");
    if (plexUrl) plexUrl.value = _stripScheme(d.plex_url || "");
    if (plexSect) plexSect.value = d.plex_section || "";
    if (plexSsl) plexSsl.checked = (d.plex_url || "").startsWith("https://");

    // Plex token badge
    _updatePlexTokenBadge(d.has_token, d.apikey);

    onMediaplayerTypeChange();

    // If Plex is configured with a token, pre-load libraries and restore selection
    if (d.type === "plex" && d.has_token) {
      // Store saved ID on select element so loadPlexLibraries can restore it
      const libSel = document.getElementById("mediaplayerPlexSectionId");
      if (libSel) libSel.dataset.saved = d.plex_section || "";
      loadPlexLibraries(d.plex_section || "");
    }
  } catch (e) { /* ignore */ }
}

function _updatePlexTokenBadge(hasToken, token) {
  const badge = document.getElementById("plexTokenBadge");
  if (!badge) return;
  if (hasToken && token) {
    const masked = token.slice(0, 4) + "••••" + token.slice(-4);
    badge.textContent = t("✓ Token gespeichert ","✓ Token saved ") + "(" + masked + ")";
    badge.style.background = "rgba(34,197,94,.12)";
    badge.style.color = "#4ade80";
    badge.style.border = "1px solid rgba(34,197,94,.3)";
  } else {
    badge.textContent = t("Kein Token gespeichert", "No token saved");
    badge.style.background = "rgba(148,163,184,.12)";
    badge.style.color = "var(--text-secondary)";
    badge.style.border = "1px solid var(--border)";
  }
}

function onMediaplayerTypeChange() {
  const svc = (document.getElementById("mediaplayerType")?.value || "");
  const jfFields = document.getElementById("mediaplayerJellyfinFields");
  const plexFields = document.getElementById("mediaplayerPlexFields");
  if (jfFields) jfFields.style.display = svc === "jellyfin" ? "block" : "none";
  if (plexFields) plexFields.style.display = svc === "plex" ? "block" : "none";
}


function _normalizeUrl(raw, useSSL) {
  raw = (raw || "").trim().replace(/\/+$/, "");
  // Strip any existing scheme first
  raw = raw.replace(/^https?:\/\//i, "");
  if (!raw) return "";
  return (useSSL ? "https://" : "http://") + raw;
}

// Strip scheme for display in the input field
function _stripScheme(url) {
  return (url || "").replace(/^https?:\/\//i, "");
}

async function saveMediaplayerSettings() {
  const svc = document.getElementById("mediaplayerType")?.value || "";
  const body = { type: svc };

  if (svc === "jellyfin") {
    const jfSslOn = document.getElementById("mediaplayerSsl")?.checked || false;
    body.url = _normalizeUrl(document.getElementById("mediaplayerUrl")?.value || "", jfSslOn);
    body.apikey = document.getElementById("mediaplayerApikey")?.value || "";
  } else if (svc === "plex") {
    const plexSslOn = document.getElementById("mediaplayerPlexSsl")?.checked || false;
    body.plex_url = _normalizeUrl(document.getElementById("mediaplayerPlexUrl")?.value || "", plexSslOn);
    const libSel = document.getElementById("mediaplayerPlexSectionId");
    body.plex_section = libSel ? libSel.value : "";
    // token is already saved by the OAuth poll – don't overwrite with empty
    const hiddenKey = document.getElementById("mediaplayerPlexApikey");
    if (hiddenKey && hiddenKey.value) body.apikey = hiddenKey.value;
  }

  try {
    const r = await fetch("/api/settings/mediaplayer", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok) showToast(t("Mediaplayer-Einstellungen gespeichert", "Mediaplayer settings saved"));
    else showToast(d.error || t("Fehler beim Speichern", "Error saving"));
  } catch (e) { showToast(t("Fehler: " + e.message, "Error: " + e.message)); }
}

// ── Plex OAuth popup ────────────────────────────────────────────────────────
let _plexPollInterval = null;

async function startPlexOAuth() {
  const btn = document.getElementById("plexLoginBtn");
  const status = document.getElementById("plexOAuthStatus");
  if (btn) btn.disabled = true;
  if (status) { status.style.display = "inline"; status.textContent = t("Verbinde mit Plex…", "Connecting to Plex…"); }

  try {
    // Step 1: create pin via backend proxy
    const r = await fetch("/api/settings/mediaplayer/plex-pin", { method: "POST" });
    const d = await r.json();
    if (!d.ok) { _plexOAuthError(d.error || t("Pin-Erstellung fehlgeschlagen", "Pin creation failed")); return; }

    // Step 2: open auth popup
    const popup = window.open(d.auth_url, "plex_auth",
      "width=800,height=700,scrollbars=yes,resizable=yes");
    if (!popup) { _plexOAuthError(t("Popup wurde blockiert – bitte Popup-Blocker deaktivieren", "Popup blocked - please disable popup blocker")); return; }

    if (status) status.textContent = t("Warte auf Plex-Login…", "Waiting for Plex login…");

    // Step 3: poll backend for token
    let attempts = 0;
    _plexPollInterval = setInterval(async () => {
      attempts++;
      if (attempts > 60) {   // 2 min timeout
        clearInterval(_plexPollInterval);
        _plexOAuthError(t("Timeout – bitte erneut versuchen", "Timeout - please try again"));
        if (!popup.closed) popup.close();
        if (btn) btn.disabled = false;
        return;
      }
      try {
        const pr = await fetch("/api/settings/mediaplayer/plex-pin/" + d.id);
        const pd = await pr.json();
        if (pd.ok && pd.authorized && pd.token) {
          clearInterval(_plexPollInterval);
          if (!popup.closed) popup.close();
          // Store token in hidden field + update badge
          const hiddenKey = document.getElementById("mediaplayerPlexApikey");
          if (hiddenKey) hiddenKey.value = pd.token;
          _updatePlexTokenBadge(true, pd.token);
          if (status) { status.style.display = "none"; }
          if (btn) btn.disabled = false;
          showToast(t("\u2713 Plex-Anmeldung erfolgreich!", "\u2713 Plex login successful!"));
          // Auto-save then load libraries
          await saveMediaplayerSettings();
          await loadPlexLibraries();
        }
      } catch (_) { }
    }, 2000);

  } catch (e) {
    _plexOAuthError(e.message);
  }
}

function _plexOAuthError(msg) {
  if (_plexPollInterval) { clearInterval(_plexPollInterval); _plexPollInterval = null; }
  const btn = document.getElementById("plexLoginBtn");
  const status = document.getElementById("plexOAuthStatus");
  if (btn) btn.disabled = false;
  if (status) { status.style.display = "inline"; status.style.color = "#f87171"; status.textContent = "\u2717 " + msg; }
}


// ── SSL toggle ↔ URL input sync ─────────────────────────────────────────────
function onJfSslToggle() {
  // Just visual feedback – actual scheme applied on save
  const url = document.getElementById("mediaplayerUrl");
  if (url) url.placeholder = document.getElementById("mediaplayerSsl")?.checked
    ? "192.168.1.100:8096  (https)"
    : "192.168.1.100:8096";
}
function onPlexSslToggle() {
  const url = document.getElementById("mediaplayerPlexUrl");
  if (url) url.placeholder = document.getElementById("mediaplayerPlexSsl")?.checked
    ? "192.168.1.100:32400  (https)"
    : "192.168.1.100:32400";
}
// Auto-detect scheme if user pastes a full URL with http(s)://
function onJfUrlInput() {
  const url = document.getElementById("mediaplayerUrl");
  const ssl = document.getElementById("mediaplayerSsl");
  if (!url || !ssl) return;
  if (/^https:\/\//i.test(url.value)) { ssl.checked = true; url.value = url.value.replace(/^https:\/\//i, ""); }
  else if (/^http:\/\//i.test(url.value)) { ssl.checked = false; url.value = url.value.replace(/^http:\/\//i, ""); }
}
function onPlexUrlInput() {
  const url = document.getElementById("mediaplayerPlexUrl");
  const ssl = document.getElementById("mediaplayerPlexSsl");
  if (!url || !ssl) return;
  if (/^https:\/\//i.test(url.value)) { ssl.checked = true; url.value = url.value.replace(/^https:\/\//i, ""); }
  else if (/^http:\/\//i.test(url.value)) { ssl.checked = false; url.value = url.value.replace(/^http:\/\//i, ""); }
}
// ── Plex library loader ────────────────────────────────────────────────────
async function loadPlexLibraries(selectedId) {
  const sel = document.getElementById("mediaplayerPlexSectionId");
  const btn = document.getElementById("plexLibLoadBtn");
  if (!sel) return;

  if (btn) { btn.disabled = true; btn.textContent = "…"; }

  try {
    const r = await fetch("/api/settings/mediaplayer/plex-libraries");
    const d = await r.json();

    // Keep "Alle scannen" as first option
    while (sel.options.length > 1) sel.remove(1);

    if (d.ok && d.libraries && d.libraries.length) {
      d.libraries.forEach(lib => {
        const o = document.createElement("option");
        o.value = lib.id;
        o.textContent = lib.title + (lib.type ? "  (" + lib.type + ")" : "");
        sel.appendChild(o);
      });
      // Restore saved selection
      const saved = selectedId !== undefined ? selectedId : sel.dataset.saved || "";
      if (saved) sel.value = saved;
    } else if (!d.ok) {
      showToast(t("Bibliotheken: " + (d.error || "Fehler"), "Libraries: " + (d.error || "Error")));
    }
  } catch (e) {
    showToast(t("Bibliotheken konnten nicht geladen werden", "Libraries could not be loaded"));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "↻ Laden"; }
  }
}

// ── Connection test ─────────────────────────────────────────────────────────
async function triggerMediaScan() {
  const btns = ["mediaplayerScanBtn", "mediaplayerScanBtn2"]
    .map(id => document.getElementById(id)).filter(Boolean);
  const toast = msg => { if (typeof showToast === "function") showToast(msg); };
  const setBtns = label => btns.forEach(b => { b.textContent = label; });
  const TIMEOUT_MS = 10 * 60 * 1000; // 10 min max
  const POLL_MS = 2000;
  const NO_SCAN_MAX = 5 * 60 * 1000; // if never seen scanning=true after 5 min, warn

  btns.forEach(b => b.disabled = true);
  setBtns("Wird ausgelöst…");

  // 1. Trigger scan
  try {
    const r = await fetch("/api/settings/mediaplayer/scan", { method: "POST" });
    const d = await r.json();
    if (!d.ok) {
      toast("✗ " + (d.error || t("Fehler beim Auslösen", "Error triggering scan")));
      btns.forEach(b => b.disabled = false);
      setBtns(t("Mediascan testen", "Test media scan"));
      return;
    }
    console.debug(t("[MediaScan] Scan ausgelöst:","[MediaScan] Scan triggered:") + d.message);
  } catch (e) {
    toast("✗ " + e.message);
    btns.forEach(b => b.disabled = false);
    setBtns(t("Mediascan testen", "Test media scan"));
    return;
  }

  // 2. Poll until server reports scanning=true (started), then scanning=false (done)
  setBtns(t("Scan läuft…", "Scan running…"));
  const deadline = Date.now() + TIMEOUT_MS;
  const noScanLimit = Date.now() + NO_SCAN_MAX;
  let seenScanning = false;

  const poll = async () => {
    if (Date.now() > deadline) {
      console.warn(t("[MediaScan] Timeout nach 10 min", "[MediaScan] Timeout after 10 min"));
      toast(t("⚠ Scan-Timeout – möglicherweise läuft er noch im Hintergrund", "⚠ Scan timeout – it may still be running in the background"));
      btns.forEach(b => b.disabled = false);
      setBtns(t("Mediascan testen", "Test media scan"));
      return;
    }
    try {
      const r = await fetch("/api/settings/mediaplayer/scan-status");
      const d = await r.json();
      console.debug(t("[MediaScan] Status:","[MediaScan] Status:"), d);
      if (d.scanning) {
        seenScanning = true;
        setTimeout(poll, POLL_MS);
      } else if (seenScanning) {
        console.debug(t("[MediaScan] Abgeschlossen", "[MediaScan] Completed"));
        toast(t("✓ Mediascan abgeschlossen", "✓ Media scan completed"));
        btns.forEach(b => b.disabled = false);
        setBtns(t("Mediascan testen", "Test media scan"));
      } else if (Date.now() > noScanLimit) {
        // Never saw scanning=true after 20s — something may be wrong
        console.warn(t("[MediaScan] Kein Scan erkannt nach 20s – prüfe Plex/Jellyfin manuell", "[MediaScan] No scan detected after 20s - check Plex/Jellyfin manually"));
        toast(t("⚠ Kein aktiver Scan erkannt – prüfe ob Plex/Jellyfin scant", "⚠ No active scan detected - check if Plex/Jellyfin is scanning"));
        btns.forEach(b => b.disabled = false);
        setBtns(t("Mediascan testen", "Test media scan"));
      } else {
        // Not scanning yet — give it more time to start
        setTimeout(poll, POLL_MS);
      }
    } catch (e) {
      console.warn(t("[MediaScan] Poll-Fehler:","[MediaScan] Poll-Error:"), e.message);
      setTimeout(poll, POLL_MS);
    }
  };

  setTimeout(poll, 1500); // short delay to let server start the scan
}


async function testMediaplayerConnection() {
  const svc = document.getElementById("mediaplayerType")?.value || "";
  const resultId = svc === "plex" ? "mediaplayerTestResult2" : "mediaplayerTestResult";
  const btnId = svc === "plex" ? "mediaplayerTestBtn2" : "mediaplayerTestBtn";
  const btn = document.getElementById(btnId);
  const result = document.getElementById(resultId);
  if (btn) btn.disabled = true;
  if (result) { result.style.display = "none"; result.textContent = ""; }
  try {
    await saveMediaplayerSettings();
    const r = await fetch("/api/settings/mediaplayer/test", { method: "POST" });
    const d = await r.json();
    if (result) {
      result.style.display = "block";
      result.style.background = d.ok ? "rgba(34,197,94,.12)" : "rgba(239,68,68,.12)";
      result.style.color = d.ok ? "#4ade80" : "#f87171";
      result.style.border = "1px solid " + (d.ok ? "rgba(34,197,94,.3)" : "rgba(239,68,68,.3)");
      result.textContent = d.ok
        ? t("\u2713 Verbunden mit: " + (d.name || "Server"), "\u2713 Connected to: " + (d.name || "Server"))
        : t("\u2717 " + (d.error || "Verbindung fehlgeschlagen"), "\u2717 " + (d.error || "Connection failed"));
    }
  } catch (e) {
    if (result) {
      result.style.display = "block";
      result.style.color = "#f87171";
      result.textContent = "\u2717 " + e.message;
    }
  } finally {
    if (btn) btn.disabled = false;
  }
}


// ===== MediaScan =====

let _mediascanPollTimer  = null;
async function loadMediascanSettings() {
  try {
    const resp = await fetch("/api/settings/mediascan");
    const d    = await resp.json();

    const enabledEl = document.getElementById("mediascanEnabled");
    const sourceEl  = document.getElementById("mediascanSource");
    if (enabledEl) enabledEl.checked = !!d.enabled;
    if (sourceEl && d.source) sourceEl.value = d.source;

    // Jellyfin fields
    const jfUrl    = document.getElementById("mediascanJfUrl");
    const jfKey    = document.getElementById("mediascanJfApikey");
    const jfSsl    = document.getElementById("mediascanJfSsl");
    if (jfUrl) jfUrl.value     = d.jf_url    || "";
    if (jfKey) jfKey.value     = d.jf_apikey || "";
    if (jfSsl) jfSsl.checked   = !!d.jf_ssl;

    // Plex fields
    const plexUrl  = document.getElementById("mediascanPlexUrl");
    const plexSsl  = document.getElementById("mediascanPlexSsl");
    const plexSect = document.getElementById("mediascanPlexSection");
    if (plexUrl)  plexUrl.value  = d.plex_url     || "";
    if (plexSsl)  plexSsl.checked = !!d.plex_ssl;
    if (plexSect) plexSect.dataset.saved = d.plex_section || "";

    _updateMsPlexTokenBadge(d.has_plex_token, d.plex_token_masked);
    _applyMediascanUI(d);
    _updateMediascanStatusUI(d);

    // Restore Plex library list if token present
    if (d.source === "plex" && d.has_plex_token) loadMsPlexLibraries(d.plex_section || "");

    // If a scan is running (started before user navigated away) keep polling
    if (d.scan_running) _startMediascanPoll();
  } catch (e) { /* best-effort */ }
}

function _updateMsPlexTokenBadge(hasToken, masked) {
  const badge = document.getElementById("msPlexTokenBadge");
  if (!badge) return;
  if (hasToken) {
    badge.textContent = t("✓ Token gespeichert (" + (masked || "••••") + ")", "✓ Token saved (" + (masked || "••••") + ")");
    badge.style.background = "rgba(34,197,94,.12)";
    badge.style.color = "#4ade80";
    badge.style.border = "1px solid rgba(34,197,94,.3)";
  } else {
    badge.textContent = t("Kein Token gespeichert", "No token saved");
    badge.style.background = "rgba(148,163,184,.12)";
    badge.style.color = "var(--text-secondary)";
    badge.style.border = "1px solid var(--border)";
  }
}

function onMediascanToggle() {
  const en = document.getElementById("mediascanEnabled")?.checked;
  const fields = document.getElementById("mediascanFields");
  if (fields) fields.style.display = en ? "block" : "none";
  saveMediascanSettings();
}

function onMediascanSourceChange() {
  const source = document.getElementById("mediascanSource")?.value || "";
  document.getElementById("mediascanJellyfinFields").style.display = source === "jellyfin" ? "block" : "none";
  document.getElementById("mediascanPlexFields").style.display     = source === "plex"     ? "block" : "none";
  saveMediascanSettings();
}

function _applyMediascanUI(d) {
  const en     = !!d.enabled;
  const source = d.source || "";
  const fields = document.getElementById("mediascanFields");
  if (fields) fields.style.display = en ? "block" : "none";

  // Show/hide source-specific fields
  const jfF   = document.getElementById("mediascanJellyfinFields");
  const plexF = document.getElementById("mediascanPlexFields");
  if (jfF)   jfF.style.display   = source === "jellyfin" ? "block" : "none";
  if (plexF) plexF.style.display = source === "plex"     ? "block" : "none";

  // TMDB warning
  const warn = document.getElementById("mediascanNoTmdbWarn");
  if (warn) warn.style.display = en && source && source !== "folders" && !d.has_tmdb ? "block" : "none";

  // Status card
  const card = document.getElementById("mediascanStatusCard");
  if (card) card.style.display = en && source && source !== "folders" ? "block" : "none";
}

function _formatRelTime(ts) {
  if (!ts) return "—";
  const diff = Math.round((Date.now() / 1000) - ts);
  if (diff < 60)  return "Gerade eben";
  if (diff < 3600) return `vor ${Math.round(diff/60)} Min.`;
  if (diff < 86400) return `vor ${Math.round(diff/3600)} Std.`;
  return `vor ${Math.round(diff/86400)} Tag(en)`;
}

function _updateMediascanStatusUI(d) {
  const lastEl   = document.getElementById("mediascanLastUpdated");
  const countEl  = document.getElementById("mediascanCount");
  const statusEl = document.getElementById("mediascanScanStatus");
  const progWrap = document.getElementById("mediascanProgressWrap");
  const progBar  = document.getElementById("mediascanProgressBar");
  const progText = document.getElementById("mediascanProgressText");
  const progPct  = document.getElementById("mediascanProgressPct");
  const refreshBtn = document.getElementById("mediascanRefreshBtn");

  if (lastEl)  lastEl.textContent  = _formatRelTime(d.last_updated || d.scan_finished);
  if (countEl) countEl.textContent = d.cached_count !== undefined ? `${d.cached_count} ${t("Einträge", "Entries")}` : (d.count ? `${d.count} ${t("Einträge", "Entries")}` : "—");

  if (d.scan_running || d.running) {
    // Show progress
    if (progWrap) progWrap.style.display = "block";
    if (statusEl) statusEl.textContent = t("Scan läuft…", "Scan running…");
    if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.textContent = t("Scan läuft…", "Scan running…"); }

    const total = d.scan_total || d.total || 0;
    const done  = d.scan_count || d.count || 0;
    if (total > 0) {
      const pct = Math.min(100, Math.round((done / total) * 100));
      if (progBar) { progBar.classList.remove("indeterminate"); progBar.style.width = pct + "%"; }
      if (progText) progText.textContent = `${done} / ${total} ${t("Einträge", "Entries")}`;
      if (progPct)  progPct.textContent  = pct + "%";
    } else {
      if (progBar) { progBar.classList.add("indeterminate"); progBar.style.width = "40%"; }
      if (progText) progText.textContent = t("Scan läuft…", "Scan running…");
      if (progPct)  progPct.textContent  = "";
    }
  } else {
    // Scan idle
    if (progWrap) progWrap.style.display = "none";
    if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.textContent = t("↻ Mediathek jetzt aktualisieren", "↻ Update library now"); }

    if (d.scan_error || d.error) {
      if (statusEl) {
        statusEl.textContent = t("Fehler", "Error");
        statusEl.style.color = "#f87171";
      }
    } else if (d.scan_finished || d.finished_at) {
      if (statusEl) {
        statusEl.textContent = t("Bereit", "Ready");
        statusEl.style.color = "#4ade80";
      }
    } else {
      if (statusEl) { statusEl.textContent = t("Noch kein Scan", "No scan yet"); statusEl.style.color = ""; }
    }
  }
}

// ── URL input helpers ─────────────────────────────────────────────────────
function onMsJfUrlInput() {
  const url = document.getElementById("mediascanJfUrl");
  const ssl = document.getElementById("mediascanJfSsl");
  if (!url || !ssl) return;
  if (/^https:\/\//i.test(url.value)) { ssl.checked = true;  url.value = url.value.replace(/^https:\/\//i, ""); }
  else if (/^http:\/\//i.test(url.value)) { ssl.checked = false; url.value = url.value.replace(/^http:\/\//i, ""); }
}
function onMsPlexUrlInput() {
  const url = document.getElementById("mediascanPlexUrl");
  const ssl = document.getElementById("mediascanPlexSsl");
  if (!url || !ssl) return;
  if (/^https:\/\//i.test(url.value)) { ssl.checked = true;  url.value = url.value.replace(/^https:\/\//i, ""); }
  else if (/^http:\/\//i.test(url.value)) { ssl.checked = false; url.value = url.value.replace(/^http:\/\//i, ""); }
}

async function saveMediascanSettings() {
  const enabled = document.getElementById("mediascanEnabled")?.checked || false;
  const source  = document.getElementById("mediascanSource")?.value    || "";
  const body = { enabled, source };

  if (source === "jellyfin") {
    body.jf_url    = document.getElementById("mediascanJfUrl")?.value    || "";
    body.jf_apikey = document.getElementById("mediascanJfApikey")?.value || "";
    body.jf_ssl    = document.getElementById("mediascanJfSsl")?.checked  || false;
  } else if (source === "plex") {
    body.plex_url     = document.getElementById("mediascanPlexUrl")?.value || "";
    body.plex_ssl     = document.getElementById("mediascanPlexSsl")?.checked || false;
    const sect = document.getElementById("mediascanPlexSection");
    body.plex_section = sect ? sect.value : "";
  }

  try {
    const r = await fetch("/api/settings/mediascan", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const d = await r.json();
    if (d.ok) showToast(t("MediaScan gespeichert", "MediaScan saved"));
    else showToast(d.error || t("Fehler beim Speichern", "Error saving"));
    await loadMediascanSettings();
  } catch (e) {
    showToast("Fehler: " + e.message);
  }
}

// ── Plex OAuth (reuses the same backend pin endpoints as Mediaplayer) ──────
let _msPollInterval = null;

async function startMsPlexOAuth() {
  const btn    = document.getElementById("msPlexLoginBtn");
  const status = document.getElementById("msPlexOAuthStatus");
  if (btn) btn.disabled = true;
  if (status) { status.style.display = "inline"; status.textContent = t("Verbinde mit Plex…", "Connecting to Plex…"); }
  try {
    const r = await fetch("/api/settings/mediaplayer/plex-pin", { method: "POST" });
    const d = await r.json();
    if (!d.ok) { _msPlexOAuthError(d.error || t("Pin-Erstellung fehlgeschlagen", "Pin creation failed")); return; }
    const popup = window.open(d.auth_url, "plex_auth_ms", "width=800,height=700,scrollbars=yes,resizable=yes");
    if (!popup) { _msPlexOAuthError(t("Popup blockiert — bitte Popup-Blocker deaktivieren", "Popup blocked - please disable popup blocker")); return; }
    if (status) status.textContent = t("Warte auf Plex-Login…", "Waiting for Plex login…");
    let attempts = 0;
    _msPollInterval = setInterval(async () => {
      attempts++;
      if (attempts > 60) {
        clearInterval(_msPollInterval);
        _msPlexOAuthError("Timeout");
        if (!popup.closed) popup.close();
        if (btn) btn.disabled = false;
        return;
      }
      try {
        const pr = await fetch("/api/settings/mediaplayer/plex-pin/" + d.id);
        const pd = await pr.json();
        if (pd.ok && pd.authorized && pd.token) {
          clearInterval(_msPollInterval);
          if (!popup.closed) popup.close();
          _updateMsPlexTokenBadge(true, pd.token.slice(0,4) + "••••" + pd.token.slice(-4));
          if (status) status.style.display = "none";
          if (btn) btn.disabled = false;
          showToast(t("✓ Plex-Anmeldung erfolgreich!", "✓ Plex login successful!"));
          await saveMediascanSettings();
          await loadMsPlexLibraries();
        }
      } catch (_) {}
    }, 2000);
  } catch (e) { _msPlexOAuthError(e.message); }
}

function _msPlexOAuthError(msg) {
  if (_msPollInterval) { clearInterval(_msPollInterval); _msPollInterval = null; }
  const btn    = document.getElementById("msPlexLoginBtn");
  const status = document.getElementById("msPlexOAuthStatus");
  if (btn) btn.disabled = false;
  if (status) { status.style.display = "inline"; status.style.color = "#f87171"; status.textContent = "✗ " + msg; }
}

async function loadMsPlexLibraries(selectedId) {
  const sel = document.getElementById("mediascanPlexSection");
  const btn = document.getElementById("msPlexLibLoadBtn");
  if (!sel) return;
  if (btn) { btn.disabled = true; btn.textContent = "…"; }
  try {
    const r = await fetch("/api/settings/mediascan/plex-libraries");
    const d = await r.json();
    while (sel.options.length > 1) sel.remove(1);
    if (d.ok && d.libraries && d.libraries.length) {
      d.libraries.forEach(lib => {
        const o = document.createElement("option");
        o.value = lib.id;
        o.textContent = lib.title + (lib.type ? "  (" + lib.type + ")" : "");
        sel.appendChild(o);
      });
      const saved = selectedId !== undefined ? selectedId : sel.dataset.saved || "";
      if (saved) sel.value = saved;
    } else if (!d.ok) {
      showToast(t("Bibliotheken: " + (d.error || "Fehler"), "Libraries: " + (d.error || "Error")));
    }
  } catch (e) {
    showToast(t("Bibliotheken konnten nicht geladen werden", "Could not load libraries"));
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = t("↻ Laden", "↻ Load"); }
  }
}

async function triggerMediascanRefresh() {
  const btn = document.getElementById("mediascanRefreshBtn");
  if (btn) { btn.disabled = true; btn.textContent = t("Wird gestartet…", "Starting…"); }

  try {
    const r = await fetch("/api/settings/mediascan/refresh", { method: "POST" });
    const d = await r.json();
    if (!d.ok) {
      showToast("✗ " + (d.error || t("Fehler beim Starten", "Error while starting")), "error");
      if (btn) { btn.disabled = false; btn.textContent = t("↻ Mediathek jetzt aktualisieren", "↻ Update library now"); }
      return;
    }
    showToast(t("Scan gestartet…", "Scan started…"));
    _startMediascanPoll();
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message), "error");
    if (btn) { btn.disabled = false; btn.textContent = t("↻ Mediathek jetzt aktualisieren", "↻ Update library now"); }
  }
}

function _startMediascanPoll() {
  if (_mediascanPollTimer) clearInterval(_mediascanPollTimer);
  _mediascanPollTimer = setInterval(async () => {
    try {
      const r = await fetch("/api/settings/mediascan/status");
      const d = await r.json();
      _updateMediascanStatusUI(d);
      if (!d.running) {
        clearInterval(_mediascanPollTimer);
        _mediascanPollTimer = null;
        if (d.error) showToast("✗ " + (d.error || t("Scan fehlgeschlagen", "Scan failed")), "error");
        else showToast("✓ " + t("Mediathek aktualisiert — " + (d.cached_count || d.count || 0) + " Einträge", "Media library updated — " + (d.cached_count || d.count || 0) + " entries"));
        // Update last-updated display
        const lastEl = document.getElementById("mediascanLastUpdated");
        if (lastEl) lastEl.textContent = _formatRelTime(d.last_updated || d.finished_at);
        const countEl = document.getElementById("mediascanCount");
        if (countEl) countEl.textContent = (d.cached_count || d.count || 0) + t(" Einträge", " Entries");
      }
    } catch (_) { /* network hiccup — keep polling */ }
  }, 1500);
}

// ─── CineInfo provider order (pill chain) ────────────────────────────────
// Which source may show its provider pill on a card / in the detail modal
// first: TMDB, Crunchyroll, Fernsehserien.de — plus every module that
// registered its own pill through registerProviderPill() (see
// web/thirdparties/registry.py's provider_pill_script and static/app.js).
// Those module resolvers are discovered live from window._providerPillResolvers,
// so a newly installed module shows up in this list without any code change
// here. The saved order is a preference, not a whitelist: a source the saved
// order doesn't mention is still used, just after the ones that are listed
// (see app.js's _pillSources()).
const _PILL_BUILTIN_LABELS = {
  tmdb: "TMDB",
  crunchyroll: "Crunchyroll",
  fernsehserien: "Fernsehserien.de",
};

let _pillOrder = [];

function _pillLabel(id) {
  if (_PILL_BUILTIN_LABELS[id]) return _PILL_BUILTIN_LABELS[id];
  return id.startsWith("ext:") ? id.slice(4) : id;
}

function _knownPillIds() {
  const ext = (window._providerPillResolvers || []).map(e => "ext:" + e.name);
  return ["tmdb", "crunchyroll", "fernsehserien"].concat(ext);
}

function _loadPillOrder(raw) {
  const known = _knownPillIds();
  const configured = String(raw || "")
    .split(",")
    .map(s => s.trim())
    .filter(s => s && known.indexOf(s) !== -1);
  _pillOrder = configured.concat(known.filter(id => configured.indexOf(id) === -1));
  _renderPillOrder();
}

function _renderPillOrder() {
  const list = document.getElementById("cineinfoProviderOrderList");
  if (!list) return;
  list.innerHTML = "";
  _pillOrder.forEach((id, idx) => {
    const row = document.createElement("div");
    row.className = "source-order-row";
    row.setAttribute("draggable", "true");
    row.dataset.pill = id;
    row.innerHTML =
      '<span class="source-drag-handle" title="' + t("Ziehen zum Sortieren", "Drag to reorder") + '" aria-hidden="true">' +
        '<svg viewBox="0 0 20 20" width="16" height="16" fill="currentColor"><circle cx="7" cy="5" r="1.5"/><circle cx="13" cy="5" r="1.5"/><circle cx="7" cy="10" r="1.5"/><circle cx="13" cy="10" r="1.5"/><circle cx="7" cy="15" r="1.5"/><circle cx="13" cy="15" r="1.5"/></svg>' +
      '</span>' +
      '<span class="source-badge">' + (idx + 1) + '. ' + _pillLabel(id) +
        (id.startsWith("ext:") ? ' <span class="mirror-active-badge">' + t("Modul", "Module") + '</span>' : '') +
      '</span>' +
      '<div class="source-order-actions">' +
        '<button type="button" class="source-move-btn" title="' + t("Nach oben", "Move up") + '" ' + (idx === 0 ? "disabled" : "") + ' onclick="movePillSource(\'' + id + '\',-1)">' +
          '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="18 15 12 9 6 15"/></svg>' +
        '</button>' +
        '<button type="button" class="source-move-btn" title="' + t("Nach unten", "Move down") + '" ' + (idx === _pillOrder.length - 1 ? "disabled" : "") + ' onclick="movePillSource(\'' + id + '\',1)">' +
          '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>' +
        '</button>' +
      '</div>';
    _attachPillDnd(row);
    list.appendChild(row);
  });
}

let _dragPill = null;
function _attachPillDnd(row) {
  row.addEventListener("dragstart", (e) => {
    _dragPill = row.dataset.pill;
    row.classList.add("dragging");
    try { e.dataTransfer.effectAllowed = "move"; e.dataTransfer.setData("text/plain", _dragPill); } catch (err) {}
  });
  row.addEventListener("dragend", () => {
    _dragPill = null;
    row.classList.remove("dragging");
    document.querySelectorAll("#cineinfoProviderOrderList .source-order-row.drag-over")
      .forEach(r => r.classList.remove("drag-over"));
  });
  row.addEventListener("dragover", (e) => {
    e.preventDefault();
    try { e.dataTransfer.dropEffect = "move"; } catch (err) {}
    if (row.dataset.pill !== _dragPill) row.classList.add("drag-over");
  });
  row.addEventListener("dragleave", () => row.classList.remove("drag-over"));
  row.addEventListener("drop", (e) => {
    e.preventDefault();
    row.classList.remove("drag-over");
    const target = row.dataset.pill;
    if (!_dragPill || _dragPill === target) return;
    const from = _pillOrder.indexOf(_dragPill);
    const to = _pillOrder.indexOf(target);
    if (from === -1 || to === -1) return;
    _pillOrder.splice(from, 1);
    _pillOrder.splice(to, 0, _dragPill);
    _renderPillOrder();
    _savePillOrder();
  });
}

function movePillSource(id, dir) {
  const i = _pillOrder.indexOf(id);
  const j = i + dir;
  if (i === -1 || j < 0 || j >= _pillOrder.length) return;
  const tmp = _pillOrder[i]; _pillOrder[i] = _pillOrder[j]; _pillOrder[j] = tmp;
  _renderPillOrder();
  _savePillOrder();
}

async function _savePillOrder() {
  try {
    const resp = await fetch("/api/settings/cineinfo", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider_order: _pillOrder.join(",") }),
    });
    const data = await resp.json();
    if (data.error) { showToast(data.error, "error"); return; }
    showToast("✓ " + t("Provider-Reihenfolge gespeichert", "Provider order saved"));
  } catch (e) {
    showToast(t("Konnte nicht gespeichert werden: ", "Could not be saved: ") + e.message, "error");
  }
}


// == Third Party Plugins ==

// -- Crunchyroll --


// -- Fernsehserien.de --
