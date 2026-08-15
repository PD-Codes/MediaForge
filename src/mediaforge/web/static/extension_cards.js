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

// ...with one exception this file now covers itself. showToast is defined by
// autosync.js / integrations.js / settings.js / notifications.html, and
// templates/module_settings.html loads none of them -- so every save on that
// page threw ReferenceError on the success line, then threw again in its own
// catch, and the user got no confirmation and no error either. A minimal
// fallback here is better than making a page load a 3000-line settings script
// for one function; a page that HAS the real one keeps it.
// Same behaviour and the same #toast element base.html already renders (see
// feedback.css's .toast/.toast.show), so it looks identical to the real one --
// it is only ever installed where none exists.
if (typeof window.showToast !== "function") {
  window.showToast = function (message, type) {
    const host = document.getElementById("toast");
    if (!host) return;
    host.textContent = String(message == null ? "" : message);
    host.className = "toast" + (type ? " toast-" + type : "");
    host.classList.remove("show");
    void host.offsetWidth;                 // restart the transition
    host.classList.add("show");
    clearTimeout(host._mfHide);
    host._mfHide = setTimeout(function () { host.classList.remove("show"); }, 4000);
  };
}

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
  // [data-exttab] only: the rail also holds the state-filter buttons, which are
  // .settings-tab too. Without the attribute in the selector, switching from
  // Modules to Theme Packs silently cleared the active state filter's highlight
  // while the filter itself stayed applied.
  document.querySelectorAll("#extensionsMenu .settings-tab[data-exttab]").forEach(function (b) {
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
// Iterates the CARDS, not the master toggles. It used to walk
// `.thirdparty-toggle[data-thirdparty-id]` and do all of the below inside that
// loop -- but _settings_card_macro.html renders no master toggle for a module
// card (show_master_toggle), so on the Module Settings page the query matched
// nothing, the GET was never sent, and every field on every card sat at its
// markup default while the stored values were ignored. Keying on the card
// itself makes the load independent of which controls that card happens to
// render.
async function loadThirdpartyToggles() {
  // Card-scoped ids -- the normal case (Integrations / Notifications /
  // Module Settings render one card per third-party id).
  const ids = new Set();
  document.querySelectorAll(".integ-card[data-thirdparty-id]").forEach(function (card) {
    ids.add(card.dataset.thirdpartyId);
  });
  // ...plus toggles that live in a card which is NOT keyed by a single
  // third-party id. The Modulmanager (extensions.html) is exactly that: one
  // .mm-card per INSTALLED MODULE, which may register several settings cards,
  // so the card carries data-module-id instead. Keying the load only on
  // .integ-card[data-thirdparty-id] meant no GET was ever sent for those
  // toggles, so an enabled module still showed its "Enable module" switch in
  // the off position until the user touched it.
  document.querySelectorAll(".thirdparty-toggle[data-thirdparty-id]").forEach(function (el) {
    ids.add(el.dataset.thirdpartyId);
  });
  ids.forEach(async function (id) {
    try {
      const resp = await fetch("/api/settings/thirdparty/" + encodeURIComponent(id));
      const d = await resp.json();
      // Only present when this page renders the master toggle. Queried
      // document-wide (and via forEach) because the same id can legitimately
      // be rendered twice on one page -- e.g. the Modulmanager card and a
      // settings card for the same module.
      document
        .querySelectorAll('.thirdparty-toggle[data-thirdparty-id="' + id + '"]')
        .forEach(function (el) { el.checked = d.enabled === "1"; });
      // Extra per-integration fields for this same id (see registry.py's
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
          // "" is a real answer ("nothing stored"), but overwriting the
          // template-rendered default with it would throw the default away --
          // and an empty numeric stepper is exactly what looked broken. A
          // stored empty value and "never set" are indistinguishable over this
          // API, so the default wins for the blank case.
          if (value !== undefined && value !== "") fieldEl.value = value;
          // number_input.js mirrors the input's value into its stepper display
          // when it enhances the field; a value that arrives afterwards has to
          // tell it to catch up, or the box shows the old one.
          fieldEl.dispatchEvent(new Event("mf-value-set", { bubbles: true }));
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

/** Find the input/select a save call is about.
 *
 *  *el* is whatever the markup passed: the field itself (a <select>'s
 *  onchange) or the Save button next to it. Resolving by data attribute
 *  instead of by DOM position is deliberate -- the markup used to pass
 *  `this.previousElementSibling`, and static/number_input.js wraps a number
 *  input in a stepper <div> and moves the input inside it, so the button's
 *  previous sibling silently became the wrapper. `wrapper.value` is undefined,
 *  JSON.stringify drops an undefined property, and the PUT went out with an
 *  empty `extra` object -- server answers 200, nothing is written, no error
 *  anywhere. Positional DOM lookups and progressive enhancement do not mix.
 */
function _resolveExtraField(id, key, el) {
  if (el && el.classList && el.classList.contains("thirdparty-extra-field")) return el;
  const sel = '.thirdparty-extra-field[data-thirdparty-id="' + id +
    '"][data-extra-key="' + key + '"]';
  // Search the card first: two cards can legitimately declare the same key,
  // and a document-wide query would then save the wrong one's value.
  const card = el && el.closest ? el.closest(".integ-card") : null;
  return (card && card.querySelector(sel)) || document.querySelector(sel);
}

// Non-toggle extra field (text/number/secret/select — registry.py's
// extra_settings "type"). Unlike the toggle, this only ever fires on
// explicit user action (a select's onchange, or the input's Save button),
// never gates a sidebar entry, so no page reload — same reasoning as
// saveThirdpartyExtraSetting() above.
async function saveThirdpartyExtraField(id, key, el) {
  const field = _resolveExtraField(id, key, el);
  if (!field) {
    showToast(t("Feld nicht gefunden", "Field not found"));
    return;
  }
  const value = field.value;
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

// ─── Deep link from the Modulmanager ("Open module" button) ────────────────
// extensions.html links to <page>?open=<item_id>[#<tab>]. Force-expands that
// one card (overriding its collapsed-by-default state) and scrolls it into
// view, so "Open module" lands the admin on the right field rather than the
// right page.
//
// Lives here, not in integrations.js, because this file is the one that every
// card-rendering page loads: Integrations, Notifications and Module Settings.
// While it sat in integrations.js the button worked on exactly one of the
// three. Pages that never render a card simply find no #integCard-<id> and
// this does nothing.
(function openDeepLinkedThirdpartyCard() {
  var openId = "";
  try { openId = new URLSearchParams(window.location.search).get("open") || ""; } catch (e) { /* no URLSearchParams */ }
  if (!openId) return;
  document.addEventListener("DOMContentLoaded", function () {
    // Deferred a tick: restoreIntegCollapse() (which this overrides for this
    // one card) runs synchronously while this file loads, and giving layout a
    // moment to settle makes the scrollIntoView land correctly even on slower
    // first paints.
    setTimeout(function () {
      var card = document.getElementById("integCard-" + openId);
      if (!card) return;
      card.classList.remove("collapsed");
      try { localStorage.setItem("integCollapsed_" + openId, "0"); } catch (e) { /* private mode */ }
      card.scrollIntoView({ behavior: "smooth", block: "center" });
      card.classList.add("integ-card-highlight");
      setTimeout(function () { card.classList.remove("integ-card-highlight"); }, 2200);
    }, 60);
  });
})();
