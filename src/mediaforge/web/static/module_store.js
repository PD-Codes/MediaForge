/* Module Store — client half of the Module Manager's store section.
 *
 * Talks to /api/store/* (see routes/extensions.py, web/thirdparties/store.py).
 * Three things worth knowing before reading on:
 *
 * 1. Nothing here runs against a store that isn't configured. The whole
 *    catalog block stays hidden until an admin saves a store URL, and the
 *    server refuses every store route in that state anyway — the hiding is
 *    convenience, not the security boundary.
 * 2. No install is ever live. The server stages downloads into
 *    web/thirdparties/_pending/ and applies them at the next start, so every
 *    successful action here ends in the same place: updating the
 *    "restart required" banner.
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

  // ---- restart banner ------------------------------------------------------
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
    if (m.compat_reason) {
      action = `<span class="integ-subsection-badge badge-incompatible">${esc(t("Inkompatibel", "Incompatible"))}</span>`;
    } else if (m.blocked_by_trust) {
      action = `<span class="settings-row-desc">${esc(t("Unverifizierte Module sind deaktiviert", "Unverified modules are disabled"))}</span>`;
    } else if (m.update_available) {
      action = `<button class="btn btn-primary store-install-btn" data-id="${esc(m.id)}">${esc(t("Aktualisieren", "Update"))} → v${esc(m.version)}</button>`;
    } else if (m.installed) {
      action = `<span class="integ-subsection-badge badge-enabled">${esc(t("Installiert", "Installed"))}</span>`;
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

    return `
      <div class="settings-row">
        <div class="settings-row-left">
          <div class="settings-row-label">
            ${esc(m.name)}
            <span class="integ-subsection-badge badge-version">v${esc(m.version)}</span>
            <span class="integ-subsection-badge ${trust.cls}">${esc(t(trust.de, trust.en))}</span>
          </div>
          ${desc ? `<div class="settings-row-desc">${esc(desc)}</div>` : ""}
          <div class="settings-row-desc" style="opacity:.7;">${meta.join(" · ")}</div>
        </div>
        <div class="settings-row-right">${action}</div>
      </div>`;
  }

  async function loadCatalog(refresh) {
    const list = $("extStoreList");
    const status = $("extStoreStatus");
    if (!list) return;
    status.style.display = "";
    status.textContent = t("Lade Store…", "Loading store…");
    list.innerHTML = "";
    try {
      const resp = await fetch("/api/store/catalog" + (refresh ? "?refresh=1" : ""));
      const data = await resp.json();
      if (!data.ok) {
        status.innerHTML = `<span style="color:var(--error);">${esc(t("Store nicht erreichbar: ", "Store unreachable: ") + (data.error || ""))}</span>`;
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
        return;
      }
      if (brokenHtml) { status.innerHTML = brokenHtml; } else { status.style.display = "none"; }
      list.innerHTML = data.modules.map(moduleRow).join("");
      // Also mark already-installed modules that have a newer version upstream,
      // right on their own card further up the page — an admin scrolling the
      // installed list shouldn't have to reach the store section to find out
      // something is out of date.
      data.modules.filter((m) => m.update_available).forEach((m) => {
        const card = document.getElementById("integCard-ext-" + m.folder);
        if (!card || card.querySelector(".badge-update")) return;
        const badge = document.createElement("span");
        badge.className = "integ-subsection-badge badge-update";
        badge.textContent = t("Update: v", "Update: v") + m.version;
        card.querySelector(".integ-subsection-header").appendChild(badge);
      });
    } catch (e) {
      status.innerHTML = `<span style="color:var(--error);">${esc(String(e))}</span>`;
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
    const installBtn = ev.target.closest(".store-install-btn");
    if (installBtn) {
      installBtn.disabled = true;
      const original = installBtn.textContent;
      installBtn.textContent = t("Lade…", "Downloading…");
      const data = await post("/api/store/install", { id: installBtn.dataset.id });
      if (data.ok) {
        renderPending(data.pending);
        toast(t(`${data.folder} v${data.version} vorgemerkt — beim nächsten Start wird es installiert.`,
                `${data.folder} v${data.version} staged — it will be installed on the next start.`));
        loadCatalog(false);
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
      if (!window.confirm(t(`"${label}" beim nächsten Start entfernen?`,
                            `Remove "${label}" on the next start?`))) return;
      uninstallBtn.disabled = true;
      const data = await post("/api/store/uninstall", { folder: uninstallBtn.dataset.folder });
      if (data.ok) {
        renderPending(data.pending);
        toast(t("Zum Entfernen vorgemerkt — wird beim nächsten Start angewendet.",
                "Staged for removal — applied on the next start."));
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

  const saveBtn = $("extStoreSaveBtn");
  if (saveBtn) {
    saveBtn.addEventListener("click", async () => {
      const url = $("extStoreUrl").value.trim();
      saveBtn.disabled = true;
      const data = await post("/api/store/config", { url: url }, "PUT");
      saveBtn.disabled = false;
      if (data.error) { toast(t("Fehler: ", "Error: ") + data.error); return; }
      // Turning the store on or off changes which parts of this page exist at
      // all (server-rendered), so a reload is the honest way to show it.
      window.location.reload();
    });
  }

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

  // Trusted signing keys. A save here changes what this install considers
  // official, so a reload is the honest response: every trust badge on the page
  // may now say something different.
  const keysSaveBtn = $("extStoreKeysSaveBtn");
  if (keysSaveBtn) {
    keysSaveBtn.addEventListener("click", async () => {
      keysSaveBtn.disabled = true;
      const data = await post("/api/store/config",
        { trusted_keys: $("extStoreTrustedKeys").value }, "PUT");
      keysSaveBtn.disabled = false;
      if (data.error) { toast(t("Fehler: ", "Error: ") + data.error); return; }
      toast(t("Schlüssel gespeichert. Module werden neu bewertet.",
              "Keys saved. Modules will be re-evaluated."));
      setTimeout(() => window.location.reload(), 700);
    });
  }

  const unverified = $("extStoreUnverified");
  if (unverified) {
    unverified.addEventListener("change", async () => {
      const data = await post("/api/store/config",
        { allow_unverified: unverified.checked ? "1" : "0" }, "PUT");
      if (data.error) { toast(t("Fehler: ", "Error: ") + data.error); return; }
      loadCatalog(true);
    });
  }

  const refreshBtn = $("extStoreRefreshBtn");
  if (refreshBtn) refreshBtn.addEventListener("click", () => loadCatalog(true));

  // Only fetch when a store is actually configured — the catalog container is
  // rendered hidden in that case (see extensions.html).
  const catalogBox = $("extStoreCatalog");
  if (catalogBox && catalogBox.style.display !== "none") loadCatalog(false);
})();
