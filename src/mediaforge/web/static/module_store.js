/* Module Store — client half of the Module Manager's store section.
 *
 * Talks to /api/store/* (see routes/extensions.py, web/thirdparties/store.py).
 * Three things worth knowing before reading on:
 *
 * 1. The official store's URL is compiled into the build and is not editable
 *    here — this page only displays it, and the server rejects a PUT that
 *    tries to change it. Same for the trusted signing keys. An admin can add
 *    extra repositories; that is all.
 * 2. Installs and uninstalls are LIVE — no app restart. The server still stages
 *    a download into web/thirdparties/_pending/ first (that's where the
 *    signature is checked), but then moves it into place and registers it on
 *    the running app. The page is reloaded afterwards purely so the
 *    server-rendered parts (sidebar link, settings card) catch up — the module
 *    itself is already running. The one exception is an UPGRADE of a module
 *    that's already loaded: Flask cannot replace a live blueprint, so that one
 *    stays staged and the "restart required" banner appears for it. The server
 *    says which happened via `live` / `restart_required`.
 * 3. Trust tiers (official / verified / unverified) are shown, not enforced,
 *    on this side. The server re-checks them — an unverified module still
 *    needs the explicit opt-in there.
 */

(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);

  function toast(msg) {
    if (window.showToast) { window.showToast(msg); return; }
    console.log("[ModuleStore]", msg);
  }

  // Shared escaper (static/mf_escape.js): the local one built the string via
  // div.textContent, which leaves " and ' untouched -- and every use here is
  // an attribute (data-id, title, href), so a module name from the store
  // index could break out of it.
  const esc = window.mfEscape;
  const safeUrl = window.mfSafeUrl;

  // ---- badges: update count + restart banner --------------------------------
  // The whole reason the store view is worth opening, shown from the installed
  // view. Set on every catalog load, including the silent one on page load —
  // otherwise "3 updates waiting" would only be discoverable by going to look.
  function renderUpdateCount(n) {
    // Also the health block in the installed view's rail: the count is only
    // knowable after the store answered, so the server could not render it.
    const healthRow = $("extHealthUpdates");
    if (healthRow) {
      healthRow.style.display = n ? "" : "none";
      const healthN = $("extHealthUpdatesN");
      if (healthN) healthN.textContent = String(n);
    }
    const badge = $("extStoreUpdateBadge");
    if (!badge) return;
    if (!n) { badge.style.display = "none"; return; }
    badge.textContent = String(n);
    badge.title = t(n + " Update(s) verfügbar", n + " update(s) available");
    badge.style.display = "";
  }

  // Single source of truth for "is a restart pending": every action that stages
  // something gets {pending: {...}} back and pipes it through here, so the
  // banner can never drift from what's actually sitting in _pending/.
  function renderPending(pending) {
    const banner = $("extPendingBanner");
    if (!banner) return;
    const install = (pending && pending.install) || [];
    const remove = (pending && pending.remove) || [];
    if (!install.length && !remove.length) {
      banner.style.display = "none";
      return;
    }
    const parts = [];
    if (install.length) parts.push(t("Zu installieren/aktualisieren: ", "To be installed/updated: ") + install.join(", "));
    if (remove.length) parts.push(t("Zu entfernen: ", "To be removed: ") + remove.join(", "));
    $("extPendingText").textContent = parts.join(" · ");
    banner.style.display = "";
  }

  // ---- theme install hint ---------------------------------------------------
  // A freshly installed theme pack does nothing until somebody selects it, so
  // the install has to say where that happens. The install is followed by a
  // page reload, which kills a toast, hence the sessionStorage hand-off:
  // written right before the reload, read once on the next page load.
  const THEME_HINT_KEY = "mf-theme-installed-hint";

  function rememberThemeHint(folder, version) {
    try {
      sessionStorage.setItem(THEME_HINT_KEY, JSON.stringify({
        folder: String(folder || ""), version: String(version || ""),
      }));
    } catch (e) { /* private mode — the toast above still fired */ }
  }

  function renderThemeHint() {
    const banner = $("extThemeHintBanner");
    if (!banner) return;
    let hint = null;
    try {
      const raw = sessionStorage.getItem(THEME_HINT_KEY);
      if (raw) hint = JSON.parse(raw);
      sessionStorage.removeItem(THEME_HINT_KEY);   // show it once, not forever
    } catch (e) { return; }
    if (!hint || !hint.folder) return;
    const label = hint.version ? `${hint.folder} v${hint.version}` : hint.folder;
    // textContent, not innerHTML: the folder name comes from the store index.
    $("extThemeHintText").textContent =
      t(`"${label}" wurde installiert und ist sofort einsatzbereit — es muss nur noch ausgewählt werden.`,
        `"${label}" is installed and ready to use — it only has to be selected.`);
    banner.style.display = "";
    const dismiss = $("extThemeHintDismiss");
    if (dismiss) dismiss.addEventListener("click", () => { banner.style.display = "none"; });
  }

  // ---- catalog -------------------------------------------------------------
  const TRUST_META = {
    official: { cls: "badge-loaded", de: "Offiziell", en: "Official" },
    verified: { cls: "badge-depends", de: "Verifiziert", en: "Verified" },
    unverified: { cls: "badge-skipped", de: "Unverifiziert", en: "Unverified" },
  };

  // Description in the UI language, falling back to English — the index carries
  // {"de": ..., "en": ...}.
  function descOf(m) {
    return (m.description && (m.description[window.__LANG] || m.description.en)) || "";
  }

  // The three pills every entry answers with: what kind of thing is this, do I
  // already have it, and is what I have current. They are one function because a
  // list row, the detail pane and (for "update") the installed card all have to
  // say the same thing the same way — three renderers drifting apart is how a
  // module ends up looking installed in one place and available in another.
  function pillType(m) {
    return (m.type === "theme")
      ? `<span class="ext-pill ext-pill-theme">${esc(t("Theme", "Theme"))}</span>`
      : `<span class="ext-pill ext-pill-module">${esc(t("Modul", "Module"))}</span>`;
  }

  function pillStatus(m) {
    const out = [];
    if (m.installed) {
      out.push(`<span class="ext-pill ext-pill-installed">${
        esc(t("Installiert", "Installed"))}${m.installed_version ? " v" + esc(m.installed_version) : ""}</span>`);
    }
    if (m.update_available) {
      out.push(`<span class="ext-pill ext-pill-update">${esc(t("Update", "Update"))} v${esc(m.version)}</span>`);
    }
    if (m.compat_reason) {
      out.push(`<span class="ext-pill ext-pill-err" title="${esc(m.compat_reason)}">${
        esc(t("Inkompatibel", "Incompatible"))}</span>`);
    }
    return out.join("");
  }

  // Worst state wins, and it is drawn as a stripe on the row's left edge — so the
  // column can be scanned without reading a single pill.
  function edgeClass(m) {
    if (m.compat_reason || (m.missing_requirements && m.missing_requirements.length)) return "is-blocked";
    if (m.update_available) return "has-update";
    if (m.installed) return "is-installed";
    return "";
  }

  // The action for an entry. Exactly one of these applies, in this order of
  // precedence: incompatible (can't run here at all) > missing pip package >
  // blocked by trust (admin hasn't opted in) > update available > installed >
  // installable. `full` adds the explanatory second half that only fits in the
  // detail pane.
  function actionFor(m, full) {
    if (m.missing_requirements && m.missing_requirements.length) {
      // "Incompatible" was true and unhelpful. A missing pip package and an unsupported
      // MediaForge version are both "won't install", but one of them is a button away and
      // the other is a wait — and in Docker, "go and pip install it yourself" is an errand
      // whose obvious answer (install into the container) is undone by the next image pull.
      const pkgs = m.missing_requirements.join(", ");
      return `<button class="btn btn-primary store-deps-btn" data-id="${esc(m.id)}"
                 title="${esc(t("Installiert " + pkgs + " nach ~/.mediaforge/thirdparty-deps",
                                "Installs " + pkgs + " into ~/.mediaforge/thirdparty-deps"))}">
           ${esc(t("Abhängigkeiten installieren", "Install dependencies"))}
         </button>`;
    }
    if (m.compat_reason) {
      return `<span class="settings-row-desc">${esc(full ? m.compat_reason : t("Inkompatibel", "Incompatible"))}</span>`;
    }
    if (m.blocked_by_trust) {
      return `<span class="settings-row-desc">${
        esc(t("Unverifizierte Module sind deaktiviert", "Unverified modules are disabled"))}</span>`;
    }
    if (m.update_available) {
      return `<button class="btn btn-primary store-install-btn" data-id="${esc(m.id)}">${
        esc(t("Aktualisieren", "Update"))} → v${esc(m.version)}</button>`;
    }
    if (m.installed) {
      // Installed and up to date — but "reinstall" still has to exist. A module folder
      // gets edited by hand, half-deleted, or corrupted by a failed unzip, and the fix is
      // to fetch the same version again.
      return `<button class="btn btn-secondary store-install-btn" data-id="${esc(m.id)}"
                        title="${esc(t("Dieselbe Version erneut herunterladen und überschreiben",
                                       "Download this same version again and overwrite the installed copy"))}">
                  ${esc(t("Neu installieren", "Reinstall"))}
                </button>`;
    }
    if (m.installable) {
      return `<button class="btn btn-primary store-install-btn" data-id="${esc(m.id)}">${
        esc(t("Installieren", "Install"))}</button>`;
    }
    return `<span class="settings-row-desc">${esc(t("Nicht installierbar", "Not installable"))}</span>`;
  }

  // A row, not a card. The catalog is read as a column — name, kind, and whether
  // this install already has it — and a grid of tiles makes that a zig-zag. The
  // action buttons keep their classes and data-ids: the single delegated click
  // handler further down matches on those, never on the layout.
  // Catalog entries carry no icon of their own, so the glyph says which KIND of
  // thing this is — a half-filled disc for a skin, a chip for code.
  function iconOf(m) {
    return m.type === "theme"
      ? `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/>
           <path d="M12 3a9 9 0 0 0 0 18"/></svg>`
      : `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
              stroke-linecap="round" stroke-linejoin="round"><rect x="7" y="7" width="10" height="10" rx="2"/>
           <path d="M9 3v4M15 3v4M9 17v4M15 17v4M3 9h4M3 15h4M17 9h4M17 15h4"/></svg>`;
  }

  function storeRow(m, selected) {
    const icon = iconOf(m);
    const trust = TRUST_META[m.trust] || TRUST_META.unverified;
    const desc = descOf(m);
    return `
      <article class="ext-row ${edgeClass(m)}${selected ? " active" : ""}"
               data-store-id="${esc(m.id)}" tabindex="0" role="button"
               aria-pressed="${selected ? "true" : "false"}">
        <span class="ext-row-icon ${m.type === "theme" ? "is-theme" : ""}" aria-hidden="true">${icon}</span>
        <span class="ext-row-ident">
          <span class="ext-row-name">${esc(m.name)}${pillType(m)}</span>
          <span class="ext-row-pills">${pillStatus(m)}
            <span class="integ-subsection-badge ${trust.cls}">${esc(t(trust.de, trust.en))}</span>
            ${m.unreviewed ? `<span class="integ-subsection-badge badge-incompatible" title="${
              esc(t("Von niemandem geprüft — liegt im Store noch in der Review-Warteschlange",
                    "Reviewed by nobody — still sitting in the store's review queue"))}">${
              esc(t("Ungeprüft", "Unreviewed"))}</span>` : ""}
          </span>
          ${desc ? `<span class="ext-row-desc">${esc(desc)}</span>` : ""}
        </span>
        <span class="ext-row-action">${actionFor(m, false)}</span>
      </article>`;
  }

  // The detail pane. Everything that does not fit on a row and that an admin has
  // to know BEFORE running somebody else's Python in this process: who signed it,
  // which repository it came from, what it needs, where the source is.
  function storeDetail(m) {
    if (!m) {
      return `<div class="mf-empty ext-detail-empty"><p>${
        esc(t("Wähle einen Eintrag aus der Liste.", "Pick an entry from the list."))}</p></div>`;
    }
    const trust = TRUST_META[m.trust] || TRUST_META.unverified;
    const desc = descOf(m);
    const rows = [];
    const kv = (label, value) => rows.push(
      `<div class="ext-kv"><span>${esc(label)}</span><span>${value}</span></div>`);
    if (!m.author) kv(t("Neueste Version", "Latest version"), "v" + esc(m.version));
    if (m.installed && m.installed_version) {
      kv(t("Deine Version", "Your version"), "v" + esc(m.installed_version));
    }
    if (m.category) kv(t("Kategorie", "Category"), esc(m.category));
    // Which repository this came from. Only interesting with more than one
    // configured — but then it is the whole story: "official" from a repo somebody
    // added themselves would be a claim, not a fact.
    if (m.store) kv(t("Repository", "Repository"), esc(m.store));
    if (m.license) kv(t("Lizenz", "License"), esc(m.license));
    if (m.min_app_version) kv(t("Benötigt", "Requires"), "MediaForge ≥ " + esc(m.min_app_version));
    if (m.requirements && m.requirements.length) {
      kv(t("Python-Pakete", "Python packages"), esc(m.requirements.join(", ")));
    }
    // safeUrl() drops anything that is not http(s) or same-origin, so a catalog
    // entry cannot smuggle a javascript: URL into this link.
    const src = m.source_url ? safeUrl(m.source_url) : "";
    return `
      <div class="ext-detail-head">
        <span class="ext-detail-icon ${m.type === "theme" ? "is-theme" : ""}"
              aria-hidden="true">${iconOf(m)}</span>
        <span class="ext-detail-ident">
          <h3 class="ext-detail-name">${esc(m.name)}</h3>
          ${m.author ? `<span class="ext-detail-by">${esc(m.author)} · v${esc(m.version)}</span>` : ""}
        </span>
        <button type="button" class="ext-detail-close" id="extStoreDetailClose"
                aria-label="${esc(t("Schließen", "Close"))}">&times;</button>
      </div>
      <div class="ext-row-pills">${pillType(m)}${pillStatus(m)}
        <span class="integ-subsection-badge ${trust.cls}">${esc(t(trust.de, trust.en))}</span>
      </div>
      ${desc ? `<p class="ext-detail-desc">${esc(desc)}</p>` : ""}
      ${rows.join("")}
      ${m.compat_reason ? `<div class="ext-detail-warn">${esc(m.compat_reason)}</div>` : ""}
      <div class="ext-detail-actions">${actionFor(m, true)}</div>
      ${src ? `<a class="ext-detail-src" href="${esc(src)}" target="_blank"
                  rel="noopener noreferrer">${esc(t("Quelle ansehen", "View source"))} ↗</a>` : ""}`;
  }

  // "Loading store…" is a promise the client has to keep. A fetch with no timeout has no
  // failure state — a request that never comes back leaves that text on screen forever,
  // which is indistinguishable from a hang and tells an admin nothing. The server now
  // bounds its own repo fetches (store.py), but the wire between here and there can also
  // simply go quiet, so this side gets a deadline of its own and always ends in either a
  // catalog or a reason.
  const CATALOG_TIMEOUT_MS = 20000;

  // The last successfully fetched catalog and the active type filter
  // (""=all, "module", "theme") — filtering happens client-side so switching
  // never refetches.
  let _catalogModules = [];
  let _typeFilter = "";
  // Facets beyond type: status ("", "installed", "available", "update") and trust
  // ("", "official", "verified", "unverified"). Both narrow the same in-memory
  // catalog, so no facet ever costs a request.
  let _statusFilter = "";
  let _trustFilter = "";
  // Category is not a fixed set like status/trust — it comes from whatever the
  // catalog's entries declare, plus a synthetic "unknown" bucket for entries
  // that declare none (grouping them under a real, filterable option instead
  // of silently dropping them from every facet count).
  let _categoryFilter = "";
  const CATEGORY_UNKNOWN = "__unknown__";
  // The entry shown in the detail pane. An id, not the object: the catalog is
  // replaced wholesale on every refresh, and holding a stale object would keep a
  // pane open on a version that no longer exists.
  let _selectedId = "";
  // Free-text filter over the catalog. Client-side on purpose: the whole
  // catalog is already in memory, so typing must not cost a request.
  let _query = "";

  function _matchesQuery(m) {
    if (!_query) return true;
    const desc = (m.description && (m.description[window.__LANG] || m.description.en)) || "";
    return [m.name, m.author, m.category, m.id, desc].some(function (v) {
      return String(v || "").toLowerCase().indexOf(_query) !== -1;
    });
  }

  // Everything except the named facet, so a facet's own count is what picking it
  // would yield rather than what is already on screen — a count that drops to 0 on
  // the option you just clicked is how faceted filters get a reputation for lying.
  function _matches(m, except) {
    if (except !== "type" && _typeFilter && (m.type || "module") !== _typeFilter) return false;
    if (except !== "trust" && _trustFilter && (m.trust || "unverified") !== _trustFilter) return false;
    if (except !== "category" && _categoryFilter &&
        (m.category || CATEGORY_UNKNOWN) !== _categoryFilter) return false;
    if (except !== "status" && _statusFilter) {
      if (_statusFilter === "installed" && !m.installed) return false;
      if (_statusFilter === "available" && m.installed) return false;
      if (_statusFilter === "update" && !m.update_available) return false;
    }
    return _matchesQuery(m);
  }

  function _count(except, key, val) {
    return _catalogModules.filter(function (m) {
      if (!_matches(m, except)) return false;
      if (!val) return true;
      if (key === "type") return (m.type || "module") === val;
      if (key === "trust") return (m.trust || "unverified") === val;
      if (key === "category") return (m.category || CATEGORY_UNKNOWN) === val;
      if (val === "installed") return !!m.installed;
      if (val === "available") return !m.installed;
      if (val === "update") return !!m.update_available;
      return true;
    }).length;
  }

  function renderFacets() {
    // Type counts live in the server-rendered rail (data-count-type) so the three
    // buttons exist before the catalog answers and do not jump into place.
    document.querySelectorAll("#extTypeFilter [data-count-type]").forEach(function (el) {
      el.textContent = _count("type", "type", el.getAttribute("data-count-type"));
    });
    const box = $("extStoreFacets");
    if (!box) return;
    const group = (label, key, active, options) => `
      <div class="ext-rail-group" data-facet="${key}" role="group" aria-label="${esc(label)}">
        <span class="ext-rail-label">${esc(label)}</span>
        ${options.map(([val, text]) => {
          const n = _count(key, key, val);
          // An option that would yield nothing is not a choice, it is a line of
          // list to read past -- and on a phone, where the groups stack, three of
          // them are a screenful. "All" and whatever is currently picked always
          // stay, so the group can never render as an empty box you cannot leave.
          if (!n && val && active !== val) return "";
          return `
          <button type="button" class="ext-facet${active === val ? " active" : ""}"
                  data-facet-key="${key}" data-facet-val="${esc(val)}">${esc(text)}
            <span class="ext-facet-n">${n}</span></button>`;
        }).join("")}
      </div>`;
    box.innerHTML =
      group(t("Status", "Status"), "status", _statusFilter, [
        ["", t("Alle", "All")],
        ["installed", t("Installiert", "Installed")],
        ["available", t("Nicht installiert", "Not installed")],
        ["update", t("Update verfügbar", "Update available")],
      ]) +
      group(t("Vertrauen", "Trust"), "trust", _trustFilter, [
        ["", t("Alle", "All")],
        ["official", t("Offiziell", "Official")],
        ["verified", t("Verifiziert", "Verified")],
        ["unverified", t("Unverifiziert", "Unverified")],
      ]) +
      group(t("Kategorie", "Category"), "category", _categoryFilter, [
        ["", t("Alle", "All")],
        // Sorted so the list order does not depend on catalog fetch order;
        // "unknown" always last since it is the fallback bucket, not a real one.
        ...Array.from(new Set(_catalogModules.map((m) => m.category).filter(Boolean))).sort()
          .map((c) => [c, c]),
        [CATEGORY_UNKNOWN, t("Unbekannt", "Unknown")],
      ]);
  }

  function renderDetail() {
    const pane = $("extStoreDetail");
    if (!pane) return;
    const m = _catalogModules.find((x) => x.id === _selectedId);
    pane.innerHTML = storeDetail(m);
    // Below the split's breakpoint the pane is a sheet over the list, so it must
    // not be in the layout at all when nothing is selected (see extensions.css).
    pane.classList.toggle("open", !!m);
  }

  function renderCatalogList() {
    const list = $("extStoreList");
    if (!list) return;
    const mods = _catalogModules.filter((m) => _matches(m, null));
    // If the selection was filtered away, fall to the first row rather than
    // leaving a pane open on an entry that is no longer in the list.
    if (!mods.some((m) => m.id === _selectedId)) _selectedId = mods.length ? mods[0].id : "";
    // Say when a filter is the reason nothing is here -- an empty list on its own
    // reads as a broken store.
    const filtering = _query || _typeFilter || _statusFilter || _trustFilter || _categoryFilter;
    list.innerHTML = mods.length
      ? '<div class="ext-row-list">' + mods.map((m) => storeRow(m, m.id === _selectedId)).join("") + "</div>"
      : '<div class="mf-empty mod-card-empty">' +
          "<p>" + esc(filtering
            ? t("Nichts passt zu diesem Filter", "Nothing matches this filter")
            : t("Der Store ist leer", "The store is empty")) + "</p></div>";
    const count = $("extStoreCount");
    if (count) {
      count.textContent = mods.length + " " + t("Einträge", "entries");
      count.style.display = "";
    }
    renderFacets();
    renderDetail();
  }

  function fetchWithTimeout(url, ms) {
    if (!window.AbortController) return fetch(url);   // old browser: no timeout, but no crash
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms);
    return fetch(url, { signal: ctrl.signal }).finally(() => clearTimeout(timer));
  }

  async function loadCatalog(refresh) {
    const list = $("extStoreList");
    const status = $("extStoreStatus");
    if (!list) return;
    status.style.display = "";
    status.textContent = t("Lade Store…", "Loading store…");
    list.innerHTML = "";
    try {
      const resp = await fetchWithTimeout(
        "/api/store/catalog" + (refresh ? "?refresh=1" : ""), CATALOG_TIMEOUT_MS);
      const data = await resp.json();
      if (!data.ok) {
        status.innerHTML = `<span style="color:var(--error);">${esc(t("Store nicht erreichbar: ", "Store unreachable: ") + (data.error || ""))}</span>`;
        renderUpdateCount(0);
        return;
      }
      renderPending(data.pending);
      const nameBadge = $("extStoreName");
      if (nameBadge && data.name) nameBadge.textContent = data.name;

      // One repo being unreachable must not hide the ones that answered.
      const broken = (data.repos || []).filter((r) => !r.ok);
      const brokenHtml = broken.length
        ? `<div style="color:var(--error);">${broken.map((r) =>
            esc(t("Nicht erreichbar: ", "Unreachable: ") + r.url + " — " + (r.error || ""))).join("<br>")}</div>`
        : "";

      if (!data.modules.length) {
        status.innerHTML = brokenHtml + esc(t("Keine Module in den konfigurierten Repositories.",
                                              "No modules in the configured repositories."));
        renderUpdateCount(0);
        return;
      }
      if (brokenHtml) { status.innerHTML = brokenHtml; } else { status.style.display = "none"; }
      _catalogModules = data.modules;
      renderCatalogList();
      // Also mark already-installed modules that have a newer version upstream, on
      // their own card in the installed view, and count them onto the store button.
      // The store view is a click away, so out-of-date has to be visible from the
      // other side of that click.
      //
      // Match a store entry to its installed card by MODULE_ID, not by folder name.
      // The store computes update_available on MODULE_ID (see store.py's catalog(),
      // installed_by_id keyed on module_id), and the card exposes that same id via
      // data-module-id. Matching on the store's declared folder instead — as this
      // used to — silently attached the badge to nothing whenever the on-disk folder
      // differed from it (a hand-installed or locally renamed module), which is the
      // whole reason installed modules often showed no update at all. The folder id
      // (integCard-ext-<folder>) is kept only as a last-ditch fallback.
      const cardsById = {};
      document.querySelectorAll(".integ-card[data-module-id]").forEach((el) => {
        cardsById[el.getAttribute("data-module-id")] = el;
      });
      const updates = data.modules.filter((m) => m.update_available);
      renderUpdateCount(updates.length);
      updates.forEach((m) => {
        const card = cardsById[m.id] || document.getElementById("integCard-ext-" + m.folder);
        if (!card || card.querySelector(".ext-pill-update")) return;
        // Next to the name, in the same pill vocabulary the store rows use -- an
        // "available update" is the same fact in both views and reading as two
        // different things is how one of them gets ignored. Theme cards have no
        // .mm-title-row, so the header is the fallback.
        const host = card.querySelector(".mm-title-row") || card.querySelector(".integ-subsection-header");
        if (!host) return;
        const badge = document.createElement("span");
        badge.className = "ext-pill ext-pill-update";
        badge.textContent = t("Update", "Update") + " v" + m.version;
        badge.title = t("Neuere Version im Store verfügbar", "A newer version is available in the store");
        host.appendChild(badge);
      });
    } catch (e) {
      const aborted = e && e.name === "AbortError";
      const msg = aborted
        ? t("Der Store hat nicht geantwortet. Erneut versuchen?",
            "The store did not answer. Try again?")
        : String(e);
      status.innerHTML =
        `<span style="color:var(--error);">${esc(msg)}</span> ` +
        `<button class="btn btn-secondary" id="extStoreRetryBtn">${esc(t("Erneut laden", "Retry"))}</button>`;
      status.style.display = "";
      const retry = $("extStoreRetryBtn");
      if (retry) retry.addEventListener("click", () => loadCatalog(true));
      renderUpdateCount(0);
    }
  }

  // ---- actions -------------------------------------------------------------
  // Installs/deps downloads are the slowest, most timing-sensitive requests on this
  // page (a package fetch, pip, signature verification, live registration — all
  // synchronous server-side before the response comes back), yet until now they were
  // the only ones with no client-side deadline and no error handling. A network
  // hiccup, a slow line, or a proxy in front of MediaForge that closes the connection
  // before the server answers left the fetch's promise unresolved with nothing to
  // catch it — the button sat on "Lade…"/"Downloading…" forever, no reload, no error,
  // indistinguishable from a hang. That is exactly the "install doesn't reload, just
  // keeps showing a loading message until I click refresh" report: the install had
  // very likely already succeeded server-side, but this side never found out. Every
  // action below now has a deadline and a catch, so it always ends in either the
  // normal success path or a visible error with the button restored — and on a
  // timeout/error it re-checks the catalog itself instead of leaving that to a manual
  // "Aktualisieren" click, since the action may well have gone through anyway.
  const ACTION_TIMEOUT_MS = 90000;

  function fetchActionWithTimeout(url, opts, ms) {
    if (!window.AbortController) return fetch(url, opts);
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), ms);
    return fetch(url, { ...opts, signal: ctrl.signal }).finally(() => clearTimeout(timer));
  }

  async function post(url, body, method, timeoutMs) {
    const opts = {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    };
    const resp = timeoutMs
      ? await fetchActionWithTimeout(url, opts, timeoutMs)
      : await fetch(url, opts);
    return resp.json();
  }

  function actionErrorMessage(e) {
    if (e && e.name === "AbortError") {
      return t("Zeitüberschreitung — die Anfrage hat zu lange gedauert.",
                "Timed out — the request took too long.");
    }
    return String((e && e.message) || e);
  }

  document.addEventListener("click", async (ev) => {
    const depsBtn = ev.target.closest(".store-deps-btn");
    if (depsBtn) {
      // pip is slow, hence the generous deadline: a cold install of something like
      // discord.py pulls half a dozen wheels over whatever line the NAS has.
      // The button says what is happening rather than pretending it is instant.
      depsBtn.disabled = true;
      const original = depsBtn.textContent;
      depsBtn.textContent = t("Installiere… (kann dauern)", "Installing… (can take a while)");

      try {
        const data = await post("/api/store/requirements", { id: depsBtn.dataset.id },
          "POST", ACTION_TIMEOUT_MS);
        if (data.ok) {
          toast(t("Abhängigkeiten installiert. Das Modul kann jetzt installiert werden.",
                  "Dependencies installed. The module can be installed now."));
          loadCatalog(true);      // the module is no longer "incompatible" — re-render it
        } else {
          // pip's own output, not a summary of it. "Could not install" tells an admin nothing;
          // "No matching distribution found for discord.py>=2.0" tells them everything.
          toast(t("Fehlgeschlagen: ", "Failed: ") + (data.error || ""));
          if (data.output) {
            console.error("[ModuleStore] pip output:\n" + data.output);
          }
          depsBtn.disabled = false;
          depsBtn.textContent = original;
        }
      } catch (e) {
        toast(t("Fehler: ", "Error: ") + actionErrorMessage(e));
        depsBtn.disabled = false;
        depsBtn.textContent = original;
        loadCatalog(true);   // it may have installed anyway before the response was lost
      }
      return;
    }

    const installBtn = ev.target.closest(".store-install-btn");
    if (installBtn) {
      installBtn.disabled = true;
      const original = installBtn.textContent;
      installBtn.textContent = t("Lade…", "Downloading…");
      try {
        const data = await post("/api/store/install", { id: installBtn.dataset.id },
          "POST", ACTION_TIMEOUT_MS);
        if (data.ok) {
          renderPending(data.pending);
          if (data.type === "theme") {
            // Themes apply live, always — the reload is only so the new card,
            // the picker options and the badges show up server-rendered.
            // The toast would not survive that reload, and a theme that is
            // installed but not selected looks like nothing happened — so the
            // "where do I turn this on" hint is handed to the banner instead
            // (see rememberThemeHint / renderThemeHint below).
            rememberThemeHint(data.folder, data.version);
            toast(t(`Theme "${data.folder}" v${data.version} installiert.`,
                    `Theme "${data.folder}" v${data.version} installed.`));
            setTimeout(() => window.location.reload(), 900);
          } else if (data.warning) {
            // Installed, verified — but it refused to load here (unmet DEPENDS_ON,
            // incompatible version, broken code). Its Modulmanager card has the reason.
            toast(t(`${data.folder} installiert, startet aber nicht: ${data.warning}`,
                    `${data.folder} installed, but it won't load: ${data.warning}`));
            setTimeout(() => window.location.reload(), 1200);
          } else if (data.live) {
            // Already running — the reload is only so the server-rendered sidebar
            // link and settings card show up.
            toast(t(`${data.folder} v${data.version} installiert und aktiv.`,
                    `${data.folder} v${data.version} installed and running.`));
            setTimeout(() => window.location.reload(), 800);
          } else {
            // Still staged. Since upgrade_module_live() exists this is the
            // exception, not the rule: either another loaded module DEPENDS_ON
            // this one, or the new version would not register and was rolled
            // back. The server says which in reasons[folder] — worth showing,
            // because "needs a restart" and "your update is broken" are very
            // different messages.
            const why = (data.reasons || {})[data.folder];
            toast(why
              ? t(`${data.folder} v${data.version} vorgemerkt — ${why}`,
                  `${data.folder} v${data.version} staged — ${why}`)
              : t(`${data.folder} v${data.version} vorgemerkt — das Update wird beim nächsten Start aktiv.`,
                  `${data.folder} v${data.version} staged — the update goes live on the next start.`));
            loadCatalog(false);
          }
        } else {
          toast(t("Fehler: ", "Error: ") + (data.error || ""));
          installBtn.disabled = false;
          installBtn.textContent = original;
        }
      } catch (e) {
        // The request never came back (timeout, dropped connection, a proxy that gave
        // up) — but the install may well have gone through on the server regardless.
        // Re-checking the catalog here is what used to require a manual "Aktualisieren"
        // click; do it automatically instead of leaving the button stuck.
        toast(t("Fehler: ", "Error: ") + actionErrorMessage(e) + " " +
              t("Store wird neu geladen…", "Reloading the store…"));
        installBtn.disabled = false;
        installBtn.textContent = original;
        loadCatalog(true);
      }
      return;
    }

    // "Set as default" / "Revert to default look" on an installed theme card
    // (templates/extensions.html). data-folder="" means built-in look.
    const themeDefaultBtn = ev.target.closest(".theme-default-btn");
    if (themeDefaultBtn) {
      themeDefaultBtn.disabled = true;
      try {
        const data = await post("/api/themes/active",
          { folder: themeDefaultBtn.dataset.folder || "" }, "PUT", ACTION_TIMEOUT_MS);
        if (data.ok) {
          toast(t("Standard-Theme gespeichert.", "Default theme saved."));
          setTimeout(() => window.location.reload(), 700);
        } else {
          toast(t("Fehler: ", "Error: ") + (data.error || ""));
          themeDefaultBtn.disabled = false;
        }
      } catch (e) {
        toast(t("Fehler: ", "Error: ") + actionErrorMessage(e));
        themeDefaultBtn.disabled = false;
      }
      return;
    }

    const uninstallBtn = ev.target.closest(".ext-uninstall-btn");
    if (uninstallBtn) {
      const label = uninstallBtn.dataset.label || uninstallBtn.dataset.folder;
      if (!window.confirm(t(`"${label}" jetzt abschalten und entfernen?`,
                            `Switch "${label}" off and remove it now?`))) return;
      uninstallBtn.disabled = true;
      try {
        const data = await post("/api/store/uninstall", {
          folder: uninstallBtn.dataset.folder,
          // "theme" on theme-pack cards (templates/extensions.html) — routes the
          // uninstall to web/themes.py (live delete) instead of the module path.
          kind: uninstallBtn.dataset.kind || "",
        }, "POST", ACTION_TIMEOUT_MS);
        if (data.ok) {
          renderPending(data.pending);
          toast(data.restart_required
            ? t("Abgeschaltet und entfernt — die Dateien werden beim nächsten Start gelöscht.",
                "Switched off and removed — its files are deleted on the next start.")
            : t("Abgeschaltet und entfernt.", "Switched off and removed."));
          setTimeout(() => window.location.reload(), 800);
        } else {
          toast(t("Fehler: ", "Error: ") + (data.error || ""));
          uninstallBtn.disabled = false;
        }
      } catch (e) {
        toast(t("Fehler: ", "Error: ") + actionErrorMessage(e));
        uninstallBtn.disabled = false;
        loadCatalog(true);   // it may have gone through before the response was lost
      }
      return;
    }
  });

  const cancelBtn = $("extPendingCancelBtn");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", async () => {
      try {
        const data = await post("/api/store/pending", null, "DELETE", ACTION_TIMEOUT_MS);
        renderPending(data.pending);
        // Buttons that were disabled after staging are re-enabled by the reload
        // of the catalog; the installed cards' uninstall buttons need the page.
        if (data.ok) {
          toast(t("Vorgemerkte Änderungen verworfen.", "Staged changes discarded."));
          setTimeout(() => window.location.reload(), 600);
        } else {
          toast(t("Fehler: ", "Error: ") + (data.error || ""));
        }
      } catch (e) {
        toast(t("Fehler: ", "Error: ") + actionErrorMessage(e));
      }
    });
  }

  // Note what is NOT here: no handler for the official store URL and none for the
  // trusted signing keys. Both are compiled into the build (thirdparties/store.py's
  // DEFAULT_STORE_URL and trusted_keys.py's BUILTIN_KEYS), the API refuses to write
  // them, and the page only displays them. A trust root — or the address the trusted
  // modules come from — that a user can edit is one an attacker can talk them into
  // editing.

  const extraSaveBtn = $("extStoreExtraSaveBtn");
  if (extraSaveBtn) {
    extraSaveBtn.addEventListener("click", async () => {
      extraSaveBtn.disabled = true;
      const data = await post("/api/store/config",
        { extra_urls: $("extStoreExtraUrls").value }, "PUT");
      extraSaveBtn.disabled = false;
      if (data.error) { toast(t("Fehler: ", "Error: ") + data.error); return; }
      $("extStoreExtraUrls").value = (data.extra_urls || []).join("\n");
      toast(t("Repositories gespeichert.", "Repositories saved."));
      loadCatalog(true);
    });
  }

  const unverified = $("extStoreUnverified");
  if (unverified) {
    unverified.addEventListener("change", async () => {
      const data = await post("/api/store/config",
        { allow_unverified: unverified.checked ? "1" : "0" }, "PUT");
      if (data.error) { toast(t("Fehler: ", "Error: ") + data.error); return; }
      // force=true, not a plain reload: this switch changes *which catalog file* is
      // fetched (index.json vs index-all.json), so a cached answer from before the flip is
      // the wrong answer. Getting the same list back after toggling is exactly how a
      // setting earns a reputation for not working.
      loadCatalog(true);
    });
  }

  // Free-text search over the catalog. Two listeners, no config object --
  // .mf-search ships markup, not behaviour.
  const searchBox = $("extStoreSearch");
  const searchClear = $("extStoreSearchClear");
  if (searchBox) {
    searchBox.addEventListener("input", () => {
      _query = searchBox.value.trim().toLowerCase();
      if (searchClear) searchClear.hidden = !searchBox.value;
      renderCatalogList();
    });
  }
  if (searchClear) {
    searchClear.addEventListener("click", () => {
      if (!searchBox) return;
      searchBox.value = "";
      _query = "";
      searchClear.hidden = true;
      searchBox.focus();
      renderCatalogList();
    });
  }

  // Facets — client-side, no refetch. One delegated handler on the rail rather
  // than one per group: the status/trust groups are re-rendered on every filter
  // change (their counts move), so a listener bound to their buttons would be
  // thrown away with them.
  const storeRail = $("extStoreRail");
  if (storeRail) {
    storeRail.addEventListener("click", (ev) => {
      const typeBtn = ev.target.closest("#extTypeFilter [data-type]");
      if (typeBtn) {
        _typeFilter = typeBtn.dataset.type || "";
        storeRail.querySelectorAll("#extTypeFilter [data-type]").forEach((b) =>
          b.classList.toggle("active", b === typeBtn));
        renderCatalogList();
        return;
      }
      const facet = ev.target.closest("[data-facet-key]");
      if (!facet) return;
      const val = facet.dataset.facetVal || "";
      if (facet.dataset.facetKey === "status") _statusFilter = val;
      if (facet.dataset.facetKey === "trust") _trustFilter = val;
      if (facet.dataset.facetKey === "category") _categoryFilter = val;
      renderCatalogList();
    });
  }

  // Picking a row fills the detail pane. Delegated on the list for the same
  // reason: the rows are replaced on every render.
  const storeList = $("extStoreList");
  if (storeList) {
    storeList.addEventListener("click", (ev) => {
      // Not when the click was the row's own action button — installing is not
      // "tell me more about this".
      if (ev.target.closest("button")) return;
      const row = ev.target.closest("[data-store-id]");
      if (!row) return;
      _selectedId = row.dataset.storeId;
      renderCatalogList();
    });
    // Rows are role="button" and focusable, so they answer to the keyboard too.
    storeList.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      const row = ev.target.closest("[data-store-id]");
      if (!row) return;
      ev.preventDefault();
      _selectedId = row.dataset.storeId;
      renderCatalogList();
    });
  }

  // The detail pane's close button only matters on phones, where the pane is a
  // sheet over the list rather than a third column.
  const detailPane = $("extStoreDetail");
  if (detailPane) {
    detailPane.addEventListener("click", (ev) => {
      if (!ev.target.closest("#extStoreDetailClose")) return;
      _selectedId = "";
      detailPane.classList.remove("open");
      detailPane.innerHTML = storeDetail(null);
    });
  }

  const refreshBtn = $("extStoreRefreshBtn");
  if (refreshBtn) refreshBtn.addEventListener("click", () => loadCatalog(true));

  // ---- restart ------------------------------------------------------------
  // The other half of an upgrade. The server answers, *then* replaces itself, so the
  // page has to survive a window where there is no server at all: poll /api/health
  // until the new process answers, and only then reload. Reloading straight away lands
  // on a connection error and looks exactly like a crash we caused.
  async function waitForServer(deadlineMs) {
    const until = Date.now() + deadlineMs;
    // Give the old process time to actually close its socket first — otherwise the very
    // first poll succeeds against the process that is on its way out, and we reload into
    // a server that then vanishes.
    await new Promise((r) => setTimeout(r, 2500));
    while (Date.now() < until) {
      try {
        const resp = await fetch("/api/health", { cache: "no-store" });
        if (resp.ok) return true;
      } catch (e) { /* expected: the server is not there yet */ }
      await new Promise((r) => setTimeout(r, 1000));
    }
    return false;
  }

  const restartBtn = $("extRestartBtn");
  if (restartBtn) {
    restartBtn.addEventListener("click", async () => {
      const data = await post("/api/store/restart", {});
      if (!data.ok) { toast(t("Fehler: ", "Error: ") + (data.error || "")); return; }

      // Say what it cost, honestly: a restart cancels running downloads/upscales, because
      // their ffmpeg and Chromium children have to die with the process rather than be
      // orphaned onto the new one.
      if (data.active_jobs) {
        toast(t(`${data.active_jobs} laufende(r) Job(s) wurden abgebrochen.`,
                `${data.active_jobs} running job(s) were cancelled.`));
      }

      restartBtn.disabled = true;
      restartBtn.textContent = t("Startet neu…", "Restarting…");
      const cancelBtn2 = $("extPendingCancelBtn");
      if (cancelBtn2) cancelBtn2.disabled = true;

      const back = await waitForServer(90000);
      if (back) {
        window.location.reload();
      } else {
        restartBtn.disabled = false;
        restartBtn.textContent = t("Jetzt neu starten", "Restart now");
        toast(t("MediaForge ist nach 90s nicht zurück — bitte Logs prüfen.",
                "MediaForge did not come back within 90s — check the logs."));
      }
    });
  }

  // ---- view switching ------------------------------------------------------
  // Three destinations, not one long scroll: the modules installed here, the
  // catalog to get more from, and where they come from. An admin arrived to do
  // one of the three; the segmented control in the header swaps between them and
  // the choice survives a reload through the URL hash (an install reloads the
  // page, and landing back on the installed view after installing from the store
  // looks like the click did nothing).
  const VIEWS = {
    installed: $("extInstalledView"),
    store: $("extStoreView"),
    settings: $("extSettingsView"),
  };
  const seg = $("extViewSeg");
  const rescanBtn = $("extRescanBtn");

  function setView(name) {
    if (!VIEWS[name]) name = "installed";
    Object.keys(VIEWS).forEach((key) => {
      if (VIEWS[key]) VIEWS[key].style.display = key === name ? "" : "none";
    });
    if (seg) {
      seg.querySelectorAll("[data-extview]").forEach((b) => {
        const on = b.dataset.extview === name;
        b.classList.toggle("active", on);
        b.setAttribute("aria-selected", on ? "true" : "false");
      });
    }
    // "Refresh" rescans web/thirdparties/ on disk. Anywhere but the installed
    // view it would be a button that looks like it refreshes what you are looking
    // at and does not — the store has its own.
    if (rescanBtn) rescanBtn.style.display = name === "installed" ? "" : "none";
    try {
      history.replaceState(null, "", name === "installed" ? window.location.pathname : "#" + name);
    } catch (e) { /* file:// and the like; the view still switched */ }
    window.scrollTo(0, 0);
  }

  if (seg) {
    seg.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-extview]");
      if (btn) setView(btn.dataset.extview);
    });
  }

  // Restoring the view from the hash on a fresh load (e.g. exactly the reload that
  // follows an install) has to wait for DOMContentLoaded: this <script> tag runs
  // BEFORE base.html's later inline block defines the global t(), and setView's
  // callees used to reach for it. Calling it directly here once threw
  // "t is not defined" the instant a page loaded with a hash, and because nothing
  // caught it the exception killed the rest of this IIFE — including the
  // loadCatalog()/renderThemeHint() deferrals below, which then never got
  // registered at all. That was the actual reason an install's reload sat on
  // "Lade Store…" forever with no request in flight.
  function restoreViewFromHash() {
    const want = (window.location.hash || "").replace("#", "");
    if (VIEWS[want]) setView(want);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", restoreViewFromHash);
  } else {
    restoreViewFromHash();
  }

  // Note this checks the catalog box's own display, not the store view's: the view
  // is hidden at rest, the box is only hidden when this build ships no store at all.
  //
  // Deferred to DOMContentLoaded rather than called directly: this <script> tag sits
  // inside {% block content %} in extensions.html, which runs BEFORE base.html's own
  // later inline script block that defines the global t() -- and loadCatalog()'s very
  // first line calls t(). Calling it directly here used to throw "t is not defined"
  // immediately, silently killing this initial background load every single time (the
  // error is uncaught -- this call has no .catch()) -- the catalog then only populated
  // once an admin clicked "Store aktualisieren" themselves, by which point every script
  // on the page (t() included) had long since finished running. The event handlers
  // below that also call t() (refreshBtn, extraSaveBtn, ...) never had this problem --
  // they only run later, in response to a click, well after the whole page has loaded.
  const catalogBox = $("extStoreCatalog");
  if (catalogBox && catalogBox.style.display !== "none") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", () => loadCatalog(false));
    } else {
      loadCatalog(false);
    }
  }

  // Same t()-availability reasoning as the catalog load above.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", renderThemeHint);
  } else {
    renderThemeHint();
  }
})();
