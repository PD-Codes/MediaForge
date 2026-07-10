// ─── Shared behaviour for auto-registered extension cards ──────────────────
// Rendered by _settings_card_macro.html wherever a card from
// web/thirdparties/registry.py's resolve_settings_cards() shows up — the
// classic Integrations "Third Party" tab, any other existing tab/pill an
// extension attaches to, or a brand-new one it creates
// (resolve_dynamic_tabs()). Everything here works off data-* attributes and
// DOM queries rather than page-specific ids, so it behaves identically no
// matter which page/tab a given card ends up rendered on — including pages
// that don't load integrations.js (e.g. notifications.html).
//
// Depends on window.showToast and the global t(de, en) helper (see
// base.html) both being defined by the time a user actually interacts with
// a toggle — true on every page this script is included from, since both
// are defined synchronously during page load, well before any onchange
// fires.

// Each card (Crunchyroll, Fernsehserien.de, any auto-registered one) can be
// expanded/collapsed; the state is remembered per-card in localStorage,
// mirroring the AutoSync group-collapse pattern (see autosync.js /
// .autosync-group).
function toggleIntegCollapse(name) {
  const card = document.getElementById("integCard-" + name);
  if (!card) return;
  const collapsed = card.classList.toggle("collapsed");
  try { localStorage.setItem("integCollapsed_" + name, collapsed ? "1" : "0"); } catch (e) {}
}

(function restoreIntegCollapse() {
  // Default is collapsed (see the "collapsed" class already on the cards in
  // the macro) — only expand a card if the user explicitly opened it
  // before. This also avoids a flash of expanded content before JS runs.
  // Scans the DOM instead of a hardcoded name list so auto-registered
  // extension cards (see web/thirdparties/) are covered for free too,
  // wherever they're rendered.
  document.querySelectorAll('.integ-card[id^="integCard-"]').forEach(function (card) {
    const name = card.id.slice("integCard-".length);
    try {
      if (localStorage.getItem("integCollapsed_" + name) === "0") {
        card.classList.remove("collapsed");
      }
    } catch (e) {}
  });
})();

// Every card shares one generic enable/disable toggle backed by
// /api/settings/thirdparty/<id> — no per-integration JS needed for the
// simple "just a toggle" case, and the same fetch also populates every
// other field type the card declared (text/number/secret/select — see
// registry.py's extra_settings "type").
async function loadThirdpartyToggles() {
  document.querySelectorAll(".thirdparty-toggle[data-thirdparty-id]").forEach(async function (el) {
    const id = el.dataset.thirdpartyId;
    try {
      const resp = await fetch("/api/settings/thirdparty/" + encodeURIComponent(id));
      const d = await resp.json();
      el.checked = d.enabled === "1";
      // Extra per-integration fields for this same card (see registry.py's
      // extra_settings) -- one fetch already has everything needed, so
      // populate them here instead of a second request per field.
      const extra = d.extra || {};
      document
        .querySelectorAll('.thirdparty-extra-toggle[data-thirdparty-id="' + id + '"][data-extra-key]')
        .forEach(function (extraEl) {
          extraEl.checked = extra[extraEl.dataset.extraKey] === "1";
        });
      // Non-toggle fields (text/number/secret/select) share one CSS hook —
      // .thirdparty-extra-field — regardless of the underlying <input>/
      // <select> type, so this one query covers all of them.
      document
        .querySelectorAll('.thirdparty-extra-field[data-thirdparty-id="' + id + '"][data-extra-key]')
        .forEach(function (fieldEl) {
          const value = extra[fieldEl.dataset.extraKey];
          if (value !== undefined) fieldEl.value = value;
        });
    } catch (e) { /* best-effort */ }
  });
}

// Reload so a sidebar entry (if any) and any other-tab placement appear/
// disappear immediately (same pattern as saveUptimeSettings(reload)).
async function saveThirdpartyToggle(id, el) {
  const enabled = el && el.checked ? "1" : "0";
  try {
    await fetch("/api/settings/thirdparty/" + encodeURIComponent(id), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled }),
    });
    setTimeout(function () { location.reload(); }, 250);
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message);
  }
}

// Extra per-integration toggle (registry.py's extra_settings) -- unlike the
// master toggle above, this never gates a sidebar entry or tab, so no page
// reload is needed; the next time the integration's own pages/API calls
// read this setting they'll see the new value (each reads it fresh via
// get_setting(), nothing caches it in-process).
async function saveThirdpartyExtraSetting(id, key, el) {
  const value = el && el.checked ? "1" : "0";
  try {
    await fetch("/api/settings/thirdparty/" + encodeURIComponent(id), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ extra: { [key]: value } }),
    });
    showToast(t("Gespeichert", "Saved"));
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message);
  }
}

// Non-toggle extra field (text/number/secret/select — registry.py's
// extra_settings "type"). Unlike the toggle, this only ever fires on
// explicit user action (a select's onchange, or the input's Save button),
// never gates a sidebar entry, so no page reload — same reasoning as
// saveThirdpartyExtraSetting() above.
async function saveThirdpartyExtraField(id, key, el) {
  const value = el ? el.value : "";
  try {
    const resp = await fetch("/api/settings/thirdparty/" + encodeURIComponent(id), {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ extra: { [key]: value } }),
    });
    const d = await resp.json();
    if (!resp.ok || d.error) throw new Error(d.error || ("HTTP " + resp.status));
    showToast(t("Gespeichert", "Saved"));
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message);
  }
}

document.addEventListener("DOMContentLoaded", loadThirdpartyToggles);
