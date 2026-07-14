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

  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  // ---- badges: update count + restart banner --------------------------------
  // The whole reason the store view is worth opening, shown from the installed
  // view. Set on every catalog load, including the silent one on page load —
  // otherwise "3 updates waiting" would only be discoverable by going to look.
  function renderUpdateCount(n) {
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

  // ---- catalog -------------------------------------------------------------
  const TRUST_META = {
    official: { cls: "badge-loaded", de: "Offiziell", en: "Official" },
    verified: { cls: "badge-depends", de: "Verifiziert", en: "Verified" },
    unverified: { cls: "badge-skipped", de: "Unverifiziert", en: "Unverified" },
  };

  function moduleRow(m) {
    const trust = TRUST_META[m.trust] || TRUST_META.unverified;
    const desc = (m.description && (m.description[window.__LANG] || m.description.en)) || "";

    // Exactly one of these four states applies, in this order of precedence:
    // incompatible (can't run here at all) > blocked by trust (admin hasn't
    // opted in) > update available > installed/installable.
    let action = "";
    if (m.missing_requirements && m.missing_requirements.length) {
      // "Incompatible" was true and unhelpful. A missing pip package and an unsupported
      // MediaForge version are both "won't install", but one of them is a button away and the
      // other is a wait — and in Docker, "go and pip install it yourself" is an errand whose
      // obvious answer (install into the container) is undone by the next image pull.
      action =
        `<button class="btn btn-primary store-deps-btn" data-id="${esc(m.id)}"
                 title="${esc(t("Installiert " + m.missing_requirements.join(", ") + " nach ~/.mediaforge/thirdparty-deps",
                                "Installs " + m.missing_requirements.join(", ") + " into ~/.mediaforge/thirdparty-deps"))}">
           ${esc(t("Abhängigkeiten installieren", "Install dependencies"))}
         </button>`;
    } else if (m.compat_reason) {
      action = `<span class="integ-subsection-badge badge-incompatible">${esc(t("Inkompatibel", "Incompatible"))}</span>`;
    } else if (m.blocked_by_trust) {
      action = `<span class="settings-row-desc">${esc(t("Unverifizierte Module sind deaktiviert", "Unverified modules are disabled"))}</span>`;
    } else if (m.update_available) {
      action = `<button class="btn btn-primary store-install-btn" data-id="${esc(m.id)}">${esc(t("Aktualisieren", "Update"))} → v${esc(m.version)}</button>`;
    } else if (m.installed) {
      // Installed, and up to date — but "reinstall" still has to exist. A module folder
      // gets edited by hand, half-deleted, or corrupted by a failed unzip, and the fix
      // is to fetch the same version again. Without this the only way back to a clean
      // copy is uninstall, restart, install, restart.
      action = `<span class="integ-subsection-badge badge-enabled">${esc(t("Installiert", "Installed"))}</span>
                <button class="btn btn-secondary store-install-btn" data-id="${esc(m.id)}"
                        title="${esc(t("Dieselbe Version erneut herunterladen und überschreiben", "Download this same version again and overwrite the installed copy"))}">
                  ${esc(t("Neu installieren", "Reinstall"))}
                </button>`;
    } else if (m.installable) {
      action = `<button class="btn btn-primary store-install-btn" data-id="${esc(m.id)}">${esc(t("Installieren", "Install"))}</button>`;
    } else {
      action = `<span class="settings-row-desc">${esc(t("Nicht installierbar", "Not installable"))}</span>`;
    }

    const meta = [];
    if (m.author) meta.push(esc(m.author));
    // Which repository this came from — only worth saying when there's more than
    // one, but always worth saying then: "official" from a repo you added
    // yourself would be a claim, not a fact, and the badge already reflects that.
    if (m.store) meta.push(esc(m.store));
    if (m.license) meta.push(esc(m.license));
    if (m.min_app_version) meta.push("MediaForge ≥ " + esc(m.min_app_version));
    if (m.installed && m.installed_version) meta.push(t("installiert: v", "installed: v") + esc(m.installed_version));
    if (m.compat_reason) meta.push(`<span style="color:var(--error);">${esc(m.compat_reason)}</span>`);
    if (m.trust === "unverified" && m.source_url) {
      meta.push(`<a href="${esc(m.source_url)}" target="_blank" rel="noopener noreferrer">${esc(m.source_url)}</a>`);
    }

    // Two different warnings, two badges. "Unverified" = nobody signed this. "Unreviewed" =
    // nobody read it — it is a submission still sitting in the store's queue, visible only
    // because this install asked to see the queue. Folding them into one word would throw
    // away the more alarming half.
    const unreviewed = m.unreviewed
      ? `<span class="integ-subsection-badge badge-incompatible"
               title="${esc(t("Von niemandem geprüft — liegt im Store noch in der Review-Warteschlange",
                              "Reviewed by nobody — still sitting in the store's review queue"))}">${
          esc(t("Ungeprüft", "Unreviewed"))}</span>`
      : "";

    return `
      <div class="settings-row">
        <div class="settings-row-left">
          <div class="settings-row-label">
            ${esc(m.name)}
            <span class="integ-subsection-badge badge-version">v${esc(m.version)}</span>
            <span class="integ-subsection-badge ${trust.cls}">${esc(t(trust.de, trust.en))}</span>
            ${unreviewed}
          </div>
          ${desc ? `<div class="settings-row-desc">${esc(desc)}</div>` : ""}
          <div class="settings-row-desc" style="opacity:.7;">${meta.join(" · ")}</div>
        </div>
        <div class="settings-row-right" style="display:flex;gap:8px;align-items:center;">${action}</div>
      </div>`;
  }

  // "Loading store…" is a promise the client has to keep. A fetch with no timeout has no
  // failure state — a request that never comes back leaves that text on screen forever,
  // which is indistinguishable from a hang and tells an admin nothing. The server now
  // bounds its own repo fetches (store.py), but the wire between here and there can also
  // simply go quiet, so this side gets a deadline of its own and always ends in either a
  // catalog or a reason.
  const CATALOG_TIMEOUT_MS = 20000;

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
      list.innerHTML = data.modules.map(moduleRow).join("");
      const count = $("extStoreCount");
      if (count) {
        count.textContent = data.modules.length + " " + t("Module", "modules");
        count.style.display = "";
      }
      // Also mark already-installed modules that have a newer version upstream, on
      // their own card in the installed view, and count them onto the store button.
      // The store view is a click away, so out-of-date has to be visible from the
      // other side of that click.
      const updates = data.modules.filter((m) => m.update_available);
      renderUpdateCount(updates.length);
      updates.forEach((m) => {
        const card = document.getElementById("integCard-ext-" + m.folder);
        if (!card || card.querySelector(".badge-update")) return;
        const badge = document.createElement("span");
        badge.className = "integ-subsection-badge badge-update";
        badge.textContent = t("Update: v", "Update: v") + m.version;
        card.querySelector(".integ-subsection-header").appendChild(badge);
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
  async function post(url, body, method) {
    const resp = await fetch(url, {
      method: method || "POST",
      headers: { "Content-Type": "application/json" },
      body: body ? JSON.stringify(body) : undefined,
    });
    return resp.json();
  }

  document.addEventListener("click", async (ev) => {
    const depsBtn = ev.target.closest(".store-deps-btn");
    if (depsBtn) {
      // pip is slow and this is a fetch with no timeout on purpose: a cold install of
      // something like discord.py pulls half a dozen wheels over whatever line the NAS has.
      // The button says what is happening rather than pretending it is instant.
      depsBtn.disabled = true;
      const original = depsBtn.textContent;
      depsBtn.textContent = t("Installiere… (kann dauern)", "Installing… (can take a while)");

      const data = await post("/api/store/requirements", { id: depsBtn.dataset.id });
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
      return;
    }

    const installBtn = ev.target.closest(".store-install-btn");
    if (installBtn) {
      installBtn.disabled = true;
      const original = installBtn.textContent;
      installBtn.textContent = t("Lade…", "Downloading…");
      const data = await post("/api/store/install", { id: installBtn.dataset.id });
      if (data.ok) {
        renderPending(data.pending);
        if (data.warning) {
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
          // An upgrade of an already-loaded module: unavoidable restart.
          toast(t(`${data.folder} v${data.version} vorgemerkt — das Update wird beim nächsten Start aktiv.`,
                  `${data.folder} v${data.version} staged — the update goes live on the next start.`));
          loadCatalog(false);
        }
      } else {
        toast(t("Fehler: ", "Error: ") + (data.error || ""));
        installBtn.disabled = false;
        installBtn.textContent = original;
      }
      return;
    }

    const uninstallBtn = ev.target.closest(".ext-uninstall-btn");
    if (uninstallBtn) {
      const label = uninstallBtn.dataset.label || uninstallBtn.dataset.folder;
      if (!window.confirm(t(`"${label}" jetzt abschalten und entfernen?`,
                            `Switch "${label}" off and remove it now?`))) return;
      uninstallBtn.disabled = true;
      const data = await post("/api/store/uninstall", { folder: uninstallBtn.dataset.folder });
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
      return;
    }
  });

  const cancelBtn = $("extPendingCancelBtn");
  if (cancelBtn) {
    cancelBtn.addEventListener("click", async () => {
      const data = await post("/api/store/pending", null, "DELETE");
      renderPending(data.pending);
      // Buttons that were disabled after staging are re-enabled by the reload
      // of the catalog; the installed cards' uninstall buttons need the page.
      if (data.ok) {
        toast(t("Vorgemerkte Änderungen verworfen.", "Staged changes discarded."));
        setTimeout(() => window.location.reload(), 600);
      } else {
        toast(t("Fehler: ", "Error: ") + (data.error || ""));
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
  // Installed modules and the store are two destinations, not one long scroll: an
  // admin arrived to do one or the other. The header button swaps between them.
  //
  // The catalog is still fetched on page load even though the store starts hidden —
  // it is what puts the update count on the button and the "Update: v…" badges on
  // the installed cards. Loading it lazily would mean an admin only learns about
  // updates by going to look for them, which is the wrong way round.
  const installedView = $("extInstalledView");
  const storeView = $("extStoreView");
  const toggleBtn = $("extStoreToggleBtn");
  const toggleLabel = $("extStoreToggleLabel");
  const rescanBtn = $("extRescanBtn");

  function setView(showStore) {
    if (!installedView || !storeView) return;
    installedView.style.display = showStore ? "none" : "";
    storeView.style.display = showStore ? "" : "none";
    // "Refresh" rescans web/thirdparties/ on disk. With the catalog on screen it
    // would be a button that looks like it refreshes what you're looking at and
    // doesn't — the store has its own.
    if (rescanBtn) rescanBtn.style.display = showStore ? "none" : "";
    if (toggleBtn) toggleBtn.className = showStore ? "btn btn-secondary" : "btn btn-primary";
    if (toggleLabel) {
      toggleLabel.textContent = showStore
        ? t("← Installierte Module", "← Installed modules")
        : t("Modulstore", "Module Store");
    }
    // Survives a reload — and the store spends its time telling you to restart.
    try {
      history.replaceState(null, "", showStore ? "#store" : window.location.pathname);
    } catch (e) { /* file:// and the like; the view still switched */ }
    window.scrollTo(0, 0);
  }

  if (toggleBtn && storeView) {
    toggleBtn.addEventListener("click", () => setView(storeView.style.display === "none"));
    if (window.location.hash === "#store") setView(true);
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
})();
