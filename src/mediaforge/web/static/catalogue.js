// Catalogue page — the full A-Z list of a source, for bulk selection.
//
// Two things shape everything in here:
//
//   1. SIZE. SerienStream lists ~10.8k series in one response. Rendering that
//      as DOM freezes the browser for seconds on every keystroke, so the list
//      is virtualised: one absolutely-positioned slice of rows inside a spacer
//      that carries the full height. Only what fits on screen exists.
//
//   2. The SELECTION has to survive everything — filtering, switching source,
//      opening a details modal, reloading the page. It therefore lives in a
//      Set keyed by url (never by row index, which changes with every filter)
//      and is mirrored into localStorage.
//
// The download itself is not done here: the server expands the selection into
// episodes in a background job (web/catalogue_worker.py), because doing it in
// the browser would mean hundreds of requests and lose everything on a reload.

(function () {
  "use strict";

  const T = window.CAT_I18N || {};
  const t = (key, fallback) => T[key] || fallback || key;

  const ROW_HEIGHT_DESKTOP = 44;
  const ROW_HEIGHT_MOBILE = 54;
  const OVERSCAN = 8;             // rows rendered above/below the viewport
  const WARN_THRESHOLD = 25;      // selection size that earns the warning
  const STORE_KEY = "mf-catalogue-selection";

  const el = (id) => document.getElementById(id);
  const viewport = el("catViewport");
  if (!viewport) return;          // not this page

  const spacer = el("catSpacer");
  const rowsHost = el("catRows");
  const searchInput = el("catSearch");

  let sources = [];               // [{id, label, enabled, color}]
  // ONE merged list across every catalogue, sorted by title. A title that
  // both sites carry stays TWO entries on purpose -- they are two different
  // pages with different languages and different episode counts, and folding
  // them together would silently pick one for the user. The source label on
  // each row is what tells them apart.
  let entries = [];               // [{title, url, alt, source}]
  let filtered = [];
  let selection = new Set();      // urls
  let queuedUrls = new Set();
  let syncUrls = new Set();
  let offSources = new Set();     // source ids the chips have narrowed away
  let jobPoll = null;
  let loadedCount = 0;            // catalogues that answered

  // Stable per-source colour for the row label and the chip dot. Taken from
  // the same palette the home feed uses for its source chips, so a source
  // looks the same wherever it appears.
  const SOURCE_COLORS = {
    aniworld: "#6aa9ff",
    sto: "#8b7dff",
    filmpalast: "#ffb454",
    megakino: "#4ade80",
    filmo: "#e8914a",
    nineanime: "#f0a020",
    aniwaves: "#38bdf8",
  };
  const colorOf = (id) => SOURCE_COLORS[id] || "#8b8bff";
  const labelOf = (id) => (sources.find((s) => s.id === id) || {}).label || id;

  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));

  const rowHeight = () => (window.matchMedia("(max-width: 720px)").matches
    ? ROW_HEIGHT_MOBILE : ROW_HEIGHT_DESKTOP);

  // ── Selection persistence ────────────────────────────────────────────────
  function loadSelection() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      if (raw) selection = new Set(JSON.parse(raw) || []);
    } catch (e) { selection = new Set(); }
  }

  function saveSelection() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(Array.from(selection)));
    } catch (e) { /* private mode / quota — the in-memory Set still works */ }
  }

  // ── Data ─────────────────────────────────────────────────────────────────
  async function loadSources() {
    const resp = await fetch("/api/catalogue/sources");
    const data = await resp.json();
    sources = (data.sources || []).map((s) => Object.assign({}, s, { color: colorOf(s.id) }));
    renderChips();
    await loadAll(false);
  }

  async function loadState() {
    try {
      const resp = await fetch("/api/catalogue/state");
      const data = await resp.json();
      queuedUrls = new Set(data.queued || []);
      syncUrls = new Set(data.autosync || []);
    } catch (e) { /* badges are a nicety, never a blocker */ }
  }

  /** Fetch every enabled catalogue and merge them into one sorted list. */
  async function loadAll(force) {
    entries = [];
    loadedCount = 0;
    applyFilter();
    showMessage(t("loading", "Loading…"));

    const enabled = sources.filter((s) => s.enabled);
    if (!enabled.length) { showMessage(t("disabled", "No source is enabled.")); return; }

    const results = await Promise.all(enabled.map((s) =>
      fetch("/api/catalogue/" + encodeURIComponent(s.id) + (force ? "?refresh=1" : ""))
        .then((r) => r.json().then((d) => ({ ok: r.ok, id: s.id, data: d })))
        .catch(() => ({ ok: false, id: s.id, data: {} }))));

    const merged = [];
    results.forEach((res) => {
      if (!res.ok || !res.data || !res.data.entries) return;
      loadedCount += 1;
      res.data.entries.forEach((e) => {
        merged.push({ title: e.title, url: e.url, alt: e.alt || "", source: res.id });
      });
    });

    if (!merged.length) { showRetry(); return; }

    // Sorted by title, then by source, so the two rows of a title both sites
    // carry sit next to each other instead of ~6000 apart.
    merged.sort((a, b) => {
      const cmp = a.title.localeCompare(b.title, undefined, { sensitivity: "base" });
      return cmp !== 0 ? cmp : a.source.localeCompare(b.source);
    });
    entries = merged;
    applyFilter();
  }

  function showMessage(text) {
    rowsHost.innerHTML = '<div class="cat-empty">' + esc(text) + "</div>";
    spacer.style.height = "auto";
    rowsHost.style.transform = "";
  }

  function showRetry() {
    rowsHost.innerHTML = '<div class="cat-empty">' + esc(t("failed", "Failed.")) +
      ' <button type="button" class="cat-tool-btn" id="catRetry">' +
      esc(t("retry", "Try again")) + "</button></div>";
    spacer.style.height = "auto";
    const btn = el("catRetry");
    if (btn) btn.addEventListener("click", () => loadAll(true));
  }

  // ── Source chips ─────────────────────────────────────────────────────────
  // These FILTER the merged list, they do not switch between lists. A chip is
  // on by default; turning one off hides that source's rows without touching
  // the selection, so a row that is marked and then hidden stays marked.
  function renderChips() {
    const host = el("catSourceChips");
    host.innerHTML = "";
    sources.forEach((s) => {
      const btn = document.createElement("button");
      btn.type = "button";
      const on = s.enabled && !offSources.has(s.id);
      btn.className = "cat-chip" + (on ? " is-on" : "") + (s.enabled ? "" : " is-off");
      btn.innerHTML = '<span class="cat-chip-dot" style="background:' + s.color + '"></span>';
      btn.appendChild(document.createTextNode(s.label));
      if (!s.enabled) {
        btn.title = t("disabled", "Switched off in Settings.");
      } else {
        btn.addEventListener("click", () => {
          if (offSources.has(s.id)) offSources.delete(s.id); else offSources.add(s.id);
          renderChips();
          viewport.scrollTop = 0;
          applyFilter();
        });
      }
      host.appendChild(btn);
    });
  }

  // ── Filter ───────────────────────────────────────────────────────────────
  // Matches the visible title AND the site's own alternate titles, which the
  // catalogue carries for exactly this reason: "Shingeki no Kyojin" should
  // find "Attack on Titan".
  function applyFilter() {
    const term = (searchInput.value || "").trim().toLowerCase();
    filtered = entries.filter((e) => {
      if (offSources.has(e.source)) return false;
      if (!term) return true;
      return e.title.toLowerCase().indexOf(term) !== -1 ||
        (e.alt && e.alt.indexOf(term) !== -1);
    });
    updateCount();
    render();
  }

  function updateCount() {
    el("catCount").textContent = entries.length
      ? filtered.length + " " + t("of", "of") + " " + entries.length + " " + t("entries", "entries")
      : "";
    const n = selection.size;
    el("catSelCount").textContent = n + " " + t("selected", "selected");
    updateWarning(n);
  }

  // The warning the user asked for: informative, never blocking. There is no
  // upper limit on a selection -- marking the whole catalogue is a legitimate
  // thing to want, so this says what it will cost instead of refusing it.
  // The count is prefixed rather than interpolated into the translated string,
  // see the note in catalogue.html.
  function updateWarning(n) {
    const warn = el("catWarn");
    if (n < WARN_THRESHOLD) { warn.style.display = "none"; return; }
    warn.textContent = n + " " + t("warn_many", "series selected; this will take a while.");
    warn.style.display = "";
  }

  // ── Virtualised rendering ────────────────────────────────────────────────
  function render() {
    const h = rowHeight();
    if (!filtered.length) {
      showMessage(entries.length ? t("nothing", "Nothing matches.") : t("loading", "Loading…"));
      return;
    }
    spacer.style.height = (filtered.length * h) + "px";

    const start = Math.max(0, Math.floor(viewport.scrollTop / h) - OVERSCAN);
    const visible = Math.ceil(viewport.clientHeight / h) + OVERSCAN * 2;
    const end = Math.min(filtered.length, start + visible);

    let html = "";
    for (let i = start; i < end; i++) {
      const e = filtered[i];
      const url = e.url;
      const isSel = selection.has(url);
      let badges = "";
      // "In library" is decided client-side against the downloaded-folder list
      // the app already holds — see isDownloaded() in app.js. Asking the server
      // per row would be eleven thousand requests.
      if (typeof window.isDownloaded === "function" && window.isDownloaded(e.title)) {
        badges += '<span class="cat-badge cat-badge--library">' + esc(t("in_library", "In library")) + "</span>";
      }
      if (queuedUrls.has(url)) {
        badges += '<span class="cat-badge cat-badge--queued">' + esc(t("queued", "Queued")) + "</span>";
      }
      if (syncUrls.has(url)) {
        badges += '<span class="cat-badge cat-badge--sync">' + esc(t("syncing", "Auto-Sync")) + "</span>";
      }
      html += '<div class="cat-row' + (isSel ? " is-selected" : "") + '" data-url="' + esc(url) + '">' +
        '<input type="checkbox" class="chb-main" ' + (isSel ? "checked" : "") + ' tabindex="-1">' +
        '<span class="cat-row-title">' + esc(e.title) + "</span>" +
        '<span class="cat-badges">' + badges + "</span>" +
        '<button type="button" class="cat-details-btn" data-details="' + esc(url) + '">' +
        esc(t("details", "Details")) + "</button></div>";
    }
    rowsHost.innerHTML = html;
    rowsHost.style.transform = "translateY(" + (start * h) + "px)";
  }

  // ── Interaction ──────────────────────────────────────────────────────────
  viewport.addEventListener("scroll", () => {
    // rAF rather than a debounce: the list has to keep up WITH the scroll, and
    // a trailing debounce shows blank rows while the finger is still moving.
    if (viewport._raf) return;
    viewport._raf = requestAnimationFrame(() => { viewport._raf = null; render(); });
  });

  window.addEventListener("resize", () => render());

  rowsHost.addEventListener("click", (ev) => {
    const detailsBtn = ev.target.closest("[data-details]");
    if (detailsBtn) {
      ev.stopPropagation();
      openDetails(detailsBtn.getAttribute("data-details"));
      return;
    }
    const row = ev.target.closest(".cat-row");
    if (!row) return;
    toggle(row.getAttribute("data-url"));
  });

  function toggle(url) {
    if (!url) return;
    if (selection.has(url)) selection.delete(url); else selection.add(url);
    saveSelection();
    updateCount();
    render();
  }

  searchInput.addEventListener("input", () => {
    viewport.scrollTop = 0;
    applyFilter();
  });

  el("catSelectAll").addEventListener("click", () => {
    // "Shown", not "all": with a filter active this is the useful action, and
    // without one it is still bounded by what the source has.
    filtered.forEach((e) => selection.add(e.url));
    saveSelection();
    updateCount();
    render();
  });

  el("catClear").addEventListener("click", () => {
    selection.clear();
    saveSelection();
    updateCount();
    render();
  });

  el("catRefresh").addEventListener("click", () => loadAll(true));

  // ── Details modal ────────────────────────────────────────────────────────
  // Deliberately its own modal rather than the start page's download modal:
  // that one owns a language/provider selection of its own and would fight
  // with the selection bar. Opening this one never changes the selection.
  let modalUrl = "";

  window.catCloseModal = function () {
    el("catModal").style.display = "none";
    modalUrl = "";
  };

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && el("catModal").style.display !== "none") window.catCloseModal();
  });

  el("catModalToggle").addEventListener("click", () => {
    toggle(modalUrl);
    syncModalToggle();
  });

  function syncModalToggle() {
    const btn = el("catModalToggle");
    btn.textContent = selection.has(modalUrl)
      ? t("remove_sel", "Remove from selection")
      : t("add_sel", "Add to selection");
  }

  async function openDetails(url) {
    modalUrl = url;
    const modal = el("catModal");
    const body = el("catModalBody");
    const entry = entries.find((e) => e.url === url);
    el("catModalTitle").textContent = entry ? entry.title : "…";
    el("catModalSub").textContent = "";
    el("catModalOpen").href = url;
    body.innerHTML = '<div class="cat-empty">' + esc(t("loading", "Loading…")) + "</div>";
    syncModalToggle();
    modal.style.display = "flex";

    try {
      const [seriesResp, seasonsResp] = await Promise.all([
        fetch("/api/series?url=" + encodeURIComponent(url)),
        fetch("/api/seasons?url=" + encodeURIComponent(url)),
      ]);
      if (modalUrl !== url) return;      // a different one was opened meanwhile
      const series = await seriesResp.json();
      const seasons = (await seasonsResp.json()).seasons || [];
      renderDetails(series, seasons, url);
    } catch (e) {
      body.innerHTML = '<div class="cat-empty">' + esc(t("failed", "Failed.")) + "</div>";
    }
  }

  function renderDetails(series, seasons, url) {
    const body = el("catModalBody");
    if (series && series.title) el("catModalTitle").textContent = series.title;
    el("catModalSub").textContent = series && series.release_year ? series.release_year : "";

    const poster = series && series.poster_url
      ? '<img class="cat-modal-poster" src="' + esc(series.poster_url) + '" alt="" loading="lazy">'
      : '<div class="cat-modal-poster" style="aspect-ratio:2/3"></div>';
    const genres = ((series && series.genres) || [])
      .map((g) => "<span>" + esc(g) + "</span>").join("");

    body.innerHTML =
      '<div class="cat-modal-body">' + poster +
      '<div class="cat-modal-info">' +
      (genres ? '<div class="cat-modal-genres">' + genres + "</div>" : "") +
      "<p>" + esc((series && series.description) || "") + "</p>" +
      "</div></div>" +
      '<div class="cat-eplist" id="catEpList"></div>';

    loadEpisodes(seasons, url);
  }

  async function loadEpisodes(seasons, url) {
    const host = el("catEpList");
    if (!host) return;
    if (!seasons.length) {
      host.innerHTML = '<div class="cat-empty">' + esc(t("no_episodes", "No episodes.")) + "</div>";
      return;
    }
    host.innerHTML = '<div class="cat-empty">' + esc(t("loading", "Loading…")) + "</div>";
    try {
      const lists = await Promise.all(seasons.map((s) =>
        fetch("/api/episodes?url=" + encodeURIComponent(s.url))
          .then((r) => r.json()).catch(() => ({ episodes: [] }))));
      if (modalUrl !== url) return;
      let html = "";
      lists.forEach((data, idx) => {
        const eps = data.episodes || [];
        if (!eps.length) return;
        const season = seasons[idx] || {};
        html += '<div class="cat-season-head">' + esc(t("season", "Season")) + " " +
          esc(season.season_number != null ? season.season_number : idx + 1) +
          " · " + eps.length + " " + esc(t("episodes_n", "episodes")) + "</div>";
        eps.forEach((ep) => {
          html += '<div class="cat-ep"><span class="cat-ep-num">E' +
            esc(ep.episode_number != null ? ep.episode_number : "?") + "</span><span>" +
            esc(ep.title_de || ep.title_en || "") + "</span></div>";
        });
      });
      host.innerHTML = html || '<div class="cat-empty">' + esc(t("no_episodes", "No episodes.")) + "</div>";
    } catch (e) {
      host.innerHTML = '<div class="cat-empty">' + esc(t("failed", "Failed.")) + "</div>";
    }
  }

  // ── Language groups ──────────────────────────────────────────────────────
  // The plain languages and the hosters are rendered by the template (see
  // catalogue.html). Only the GROUPS are fetched, because they are user data
  // that changes without a page reload -- and for a bulk action a fallback
  // chain is not a nicety: every series has different languages, and one fixed
  // language fails on a good share of a large selection.
  async function loadLanguageGroups() {
    try {
      const resp = await fetch("/api/language-groups");
      if (!resp.ok) return;
      const data = await resp.json();
      const groups = (data && (data.groups || data)) || [];
      if (!Array.isArray(groups) || !groups.length) return;
      const sel = el("catLanguage");
      const optgroup = document.createElement("optgroup");
      optgroup.label = t("groups", "Language groups");
      groups.forEach((g) => {
        if (!g || g.id == null) return;
        const opt = new Option(g.name || ("#" + g.id), "group:" + g.id);
        opt.title = (g.languages || []).join(" → ");
        optgroup.appendChild(opt);
      });
      if (optgroup.children.length) sel.appendChild(optgroup);
    } catch (e) { /* groups are optional; the plain languages still work */ }
  }

  // ── Bulk submit ──────────────────────────────────────────────────────────
  async function submit(mode) {
    if (!selection.size) { toast(t("pick_one", "Mark something first.")); return; }

    // Only the entries of the ACTIVE source travel: the server validates the
    // urls against that source's catalogue, so a selection spanning two
    // sources has to be submitted per source.
    const known = new Set(entries.map((e) => e.url));
    const urls = Array.from(selection).filter((u) => known.has(u));
    if (!urls.length) { toast(t("pick_one", "Mark something first.")); return; }

    if (mode === "autosync" && !confirm(t("warn_sync", "Auto-Sync keeps downloading."))) return;

    // No `source`: the list is merged, so a selection legitimately spans
    // several sites and the server resolves each url against whichever
    // catalogue holds it.
    const body = {
      urls: urls,
      mode: mode,
      language: el("catLanguage").value || "German Dub",
      provider: el("catProvider").value || "VOE",
      missing_only: el("catMissingOnly").checked,
    };
    try {
      const resp = await fetch("/api/catalogue/bulk", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await resp.json();
      if (!resp.ok) {
        toast(data.code === "busy" ? t("busy", "Still running.")
          : (data.error || t("failed", "Failed.")));
        return;
      }
      // Only what was actually submitted is cleared: entries of another source
      // stay marked, which is the whole reason the selection is global.
      urls.forEach((u) => selection.delete(u));
      saveSelection();
      updateCount();
      render();
      watchJob(data.id, mode);
    } catch (e) {
      toast(t("failed", "Failed."));
    }
  }

  // ── Options panel (phones) ───────────────────────────────────────────────
  // The three option controls are collapsed on a phone (see catalogue.css).
  // The toggle carries their current values, so the panel stays closed
  // without hiding what is set -- which is the only reason collapsing them is
  // acceptable at all.
  function updateOptionsSummary() {
    const host = el("catOptionsSummary");
    if (!host) return;
    const lang = el("catLanguage");
    const prov = el("catProvider");
    const bits = [
      (lang.options[lang.selectedIndex] || {}).text || "",
      (prov.options[prov.selectedIndex] || {}).text || "",
      el("catMissingOnly").checked
        ? t("only_missing_short", "only missing")
        : t("all_episodes_short", "all episodes"),
    ].filter(Boolean);
    host.textContent = bits.join(" · ");
  }

  const optionsToggle = el("catOptionsToggle");
  if (optionsToggle) {
    optionsToggle.addEventListener("click", () => {
      const panel = el("catOptions");
      const open = panel.classList.toggle("is-open");
      optionsToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  ["catLanguage", "catProvider", "catMissingOnly"].forEach((id) => {
    const node = el(id);
    if (node) node.addEventListener("change", updateOptionsSummary);
  });

  el("catQueueBtn").addEventListener("click", () => submit("queue"));
  el("catSyncBtn").addEventListener("click", () => submit("autosync"));

  // ── Job progress ─────────────────────────────────────────────────────────
  function watchJob(jobId, mode) {
    const card = el("catJob");
    card.style.display = "";
    el("catJobTitle").textContent = mode === "autosync"
      ? t("job_sync", "Creating Auto-Sync jobs") : t("job_queue", "Adding to the queue");
    el("catJobCancel").style.display = "";
    el("catJobCancel").onclick = () => {
      fetch("/api/catalogue/bulk/" + encodeURIComponent(jobId) + "/cancel", { method: "POST" });
    };

    if (jobPoll) clearInterval(jobPoll);
    const tick = async () => {
      try {
        const resp = await fetch("/api/catalogue/bulk/" + encodeURIComponent(jobId));
        if (!resp.ok) { clearInterval(jobPoll); jobPoll = null; return; }
        const job = await resp.json();
        renderJob(job);
        if (job.status !== "running") {
          clearInterval(jobPoll);
          jobPoll = null;
          el("catJobCancel").style.display = "none";
          loadState().then(render);   // badges are stale now
        }
      } catch (e) { /* transient — the next tick tries again */ }
    };
    jobPoll = setInterval(tick, 1500);
    tick();
  }

  function renderJob(job) {
    const pct = job.total ? Math.round((job.done / job.total) * 100) : 0;
    el("catJobFill").style.width = pct + "%";
    const bits = [
      job.done + "/" + job.total,
      job.queued + " " + t("created", "created"),
    ];
    if (job.episodes) bits.push(job.episodes + " " + t("episodes_n", "episodes"));
    if (job.skipped) bits.push(job.skipped + " " + t("skipped", "skipped"));
    if (job.failed) bits.push(job.failed + " " + t("failed_n", "failed"));
    if (job.status === "finished") bits.push("✓ " + t("done", "Done"));
    if (job.status === "cancelled") bits.push(t("cancelled", "Stopped"));
    el("catJobStats").innerHTML = bits.map((b) => "<span>" + esc(b) + "</span>").join("");
  }

  function toast(message) {
    if (typeof window.showToast === "function") window.showToast(message);
    else alert(message);
  }

  // ── Boot ─────────────────────────────────────────────────────────────────
  // A bulk job keeps running on the server while you are somewhere else in the
  // app, so coming back to this page has to pick the progress card back up --
  // otherwise a job that is halfway through a hundred series looks like it
  // never started, and the obvious reaction is to submit it a second time.
  async function resumeJob() {
    try {
      const resp = await fetch("/api/catalogue/bulk");
      if (!resp.ok) return;
      const jobs = (await resp.json()).jobs || [];
      if (!jobs.length) return;
      const running = jobs.filter((j) => j.status === "running").pop();
      if (running) { watchJob(running.id, running.mode); return; }
      // Nothing running: still show the most recent result, so a job that
      // finished while you were away is not silently swallowed. Only briefly
      // relevant, so anything older than an hour is left alone.
      const last = jobs[jobs.length - 1];
      if (last && last.finished_at && (Date.now() / 1000 - last.finished_at) < 3600) {
        el("catJob").style.display = "";
        el("catJobTitle").textContent = last.mode === "autosync"
          ? t("job_sync", "Creating Auto-Sync jobs") : t("job_queue", "Adding to the queue");
        el("catJobCancel").style.display = "none";
        renderJob(last);
      }
    } catch (e) { /* the page works fine without it */ }
  }

  loadSelection();
  updateOptionsSummary();
  loadLanguageGroups().then(updateOptionsSummary);
  loadState().then(loadSources);
  resumeJob();
})();
