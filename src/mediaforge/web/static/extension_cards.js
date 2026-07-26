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
// ─── Modulmanager: Modules / Theme Packs tabs ──────────────────────────────
// Same shape as switchMonTab() on the Monitoring page. Deliberately NOT stored
// in the URL hash: module_store.js already owns "#store" for its view swap, and
// two features writing the same hash is how you get a page that opens on the
// wrong thing after an install reload.
function switchExtTab(name) {
  document.querySelectorAll("#extensionsMenu .settings-tab").forEach(function (b) {
    b.classList.toggle("active", b.dataset.exttab === name);
  });
  document.querySelectorAll("#extInstalledView > .settings-tab-panel").forEach(function (p) {
    p.classList.toggle("active", p.id === "exttab-" + name);
  });
  try { localStorage.setItem("extActiveTab", name); } catch (e) { /* private mode */ }
  // Below 861px the menu is an off-canvas drawer (base.html) -- picking an entry
  // there should close it, like every other menu on the site.
  const menu = document.getElementById("extensionsMenu");
  if (menu && menu.classList.contains("mobile-open")) {
    const backdrop = document.querySelector(".floating-menu-backdrop");
    menu.classList.remove("mobile-open");
    if (backdrop) backdrop.classList.remove("show");
    if (window.MFScrollLock) window.MFScrollLock.unlock();
  }
}

(function restoreExtTab() {
  if (!document.getElementById("extensionsMenu")) return;
  let want = "modules";
  try { want = localStorage.getItem("extActiveTab") || "modules"; } catch (e) {}
  // An empty Themes panel is a dead end -- fall back rather than opening it.
  if (want === "themes" && !document.querySelector("#exttab-themes .integ-card")) {
    want = "modules";
  }
  if (want !== "modules") switchExtTab(want);
})();

// ─── Modulmanager: state filter, traceback toggle, copy ────────────────────
// Delegated on document so nothing has to be re-wired when the store view
// swaps the installed list in and out (module_store.js toggles #extInstalledView
// / #extStoreView). All of it is progressive: without JS the cards are simply
// all visible with their errors collapsed.
(function mmModuleManager() {
  function applyFilter(want) {
    document.querySelectorAll(".integ-card.mm-card").forEach(function (card) {
      const state = card.getAttribute("data-mm-state") || "";
      // "trouble" is the union of the two states worth acting on -- one button
      // for "what is wrong", instead of making people check two.
      const show = !want
        || state === want
        || (want === "trouble" && (state === "error" || state === "skipped"));
      card.style.display = show ? "" : "none";
    });
  }

  document.addEventListener("click", function (ev) {
    const filter = ev.target.closest("#mmFilters .mm-filter");
    if (filter) {
      document.querySelectorAll("#mmFilters .mm-filter").forEach(function (b) {
        b.classList.toggle("active", b === filter);
      });
      applyFilter(filter.getAttribute("data-mm-filter") || "");
      return;
    }

    const toggle = ev.target.closest(".mm-error-toggle");
    if (toggle) {
      const pre = document.getElementById(toggle.getAttribute("data-mm-trace"));
      if (!pre) return;
      const open = pre.hidden;
      pre.hidden = !open;
      toggle.setAttribute("aria-expanded", open ? "true" : "false");
      // Keep the label, swap the marker -- the text is translated server-side.
      toggle.textContent = (open ? "▾" : "▸") + toggle.textContent.slice(1);
      return;
    }

    const copy = ev.target.closest(".mm-copy-btn");
    if (copy) {
      const pre = document.getElementById(copy.getAttribute("data-mm-copy"));
      if (!pre) return;
      const text = pre.textContent || "";
      const done = function () {
        const old = copy.textContent;
        copy.textContent = t("Kopiert", "Copied");
        setTimeout(function () { copy.textContent = old; }, 1500);
      };
      // navigator.clipboard needs a secure context; a MediaForge on plain http
      // in a LAN is the normal case, so keep the textarea fallback.
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, function () { fallback(text, done); });
      } else {
        fallback(text, done);
      }
    }
  });

  function fallback(text, done) {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); done(); } catch (e) { /* nothing to offer */ }
    document.body.removeChild(ta);
  }
})();

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
