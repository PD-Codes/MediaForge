// ===== Seerr requests page =====

const SEERR_PAGE_SIZE = 20;

// NOTE: CineInfo/TMDB enrichment (provider pills, rating badge, FSK badge)
// used to be duplicated here for the old Seerr-only series modal. That modal
// is gone (see openSeriesFromSeerr() in app.js) — the standard modal's own
// enrichModalWithTmdb() in app.js now covers this for the Seerr page too.

let _seerrAutosyncJobsPromise = null;

async function checkAutosyncTitle(searchString) {
    if (!searchString) return false;
    if (!_seerrAutosyncJobsPromise) {
        _seerrAutosyncJobsPromise = (async () => {
            try {
                const response = await fetch("/api/autosync");
                if (!response.ok) {
                    throw new Error(t("HTTP-Fehler! Status: ${response.status}", "HTTP error! Status: ${response.status}"));
                }
                const data = await response.json();
                return data.jobs || [];
            } catch (error) {
                console.error(t("Fehler bei der API-Abfrage:", "Error checking autosync:"), error);
                return [];
            }
        })();
    }
    
    try {
        const jobs = await _seerrAutosyncJobsPromise;
        const lowerSearch = searchString.toLowerCase();
        for (let job of jobs) {
            const title = job.title || "";
            if (title.toLowerCase().includes(lowerSearch)) {
                return true; 
            }
        }
    } catch (error) {
        console.error(t("Fehler beim Prüfen von Autosync:", "Error checking autosync:"), error);
    }

    return false;
}

let _seerrSkip = 0;
let _seerrTotal = null;
let _seerrLoading = false;
let _seerrObserver = null;

// State for the search modal / series modal context. The series modal
// itself now lives in shared_modals.html (see openSeriesFromSeerr() in
// app.js) -- these three fields carry the Seerr request context into it.
let _seerrIsMovie = false;
let _seerrCurrentReqId = null;   // Seerr request id when a series/movie was opened from a request
let _seerrCurrentStatus = null;  // 1=pending, 2=approved

// ---------------------------------------------------------------
// Card list + lazy loading
// ---------------------------------------------------------------

async function seerrLoad() {
  _seerrAutosyncJobsPromise = null;
  _seerrSkip = 0;
  _seerrTotal = null;
  _seerrLoading = false;
  if (_seerrObserver) { _seerrObserver.disconnect(); _seerrObserver = null; }

  const list = document.getElementById("seerrList");
  list.innerHTML = '<div class="queue-empty" style="padding:40px 0">' + t('Lade Anfragen…', 'Loading requests…') + '</div>';
  seerrSetStatus("loading");
  await seerrFetchPage(true);
}

async function seerrFetchPage(isFirst) {
  if (_seerrLoading) return;
  if (_seerrTotal !== null && _seerrSkip >= _seerrTotal) return;
  _seerrLoading = true;

  const list = document.getElementById("seerrList");
  const oldSentinel = document.getElementById("seerrSentinel");
  if (oldSentinel) oldSentinel.remove();

  try {
    const resp = await fetch(`/api/seerr/requests?take=${SEERR_PAGE_SIZE}&skip=${_seerrSkip}`);
    const data = await resp.json();

    if (data.error) {
      if (isFirst) {
        list.innerHTML = data.error.includes("nicht konfiguriert")
          ? t('<div class="queue-empty">Seerr ist noch nicht konfiguriert. Bitte trage URL und API-Key unter <a href="/integrations" style="color:var(--accent)">Integrationen</a> ein.</div>', '<div class="queue-empty">Seerr is not yet configured. Please enter the URL and API key under <a href="/integrations" style="color:var(--accent)">Integrations</a>.</div>')
          : `<div class="queue-empty" style="color:var(--error)">${escS(data.error)}</div>`;
        seerrSetStatus("error");
      }
      _seerrLoading = false;
      return;
    }

    const requests = data.requests || [];
    _seerrTotal = data.total ?? requests.length;
    _seerrSkip += requests.length;

    if (isFirst) {
      if (!requests.length) {
        list.innerHTML = '<div class="queue-empty">' + t('Keine ausstehenden oder angenommenen Serien-Anfragen.', 'No pending or approved series requests.') + '</div>';
        seerrSetStatus("ok", "0 " + t("Anfragen", "requests"));
        _seerrLoading = false;
        return;
      }
      list.innerHTML = '<div class="seerr-grid" id="seerrGrid"></div>';
      seerrSetStatus("ok", `${_seerrTotal} ${t("Anfragen", "requests")}`);
      const badge = document.getElementById("seerrBadge");
      if (badge) { badge.textContent = _seerrTotal; badge.style.display = _seerrTotal > 0 ? "" : "none"; }
    }

    const grid = document.getElementById("seerrGrid");
    requests.forEach(req => grid.insertAdjacentHTML("beforeend", seerrRenderCard(req)));

    if (_seerrSkip < _seerrTotal) {
      const sentinel = document.createElement("div");
      sentinel.id = "seerrSentinel";
      sentinel.className = "seerr-sentinel";
      sentinel.innerHTML = '<span class="lib-scan-spinner" style="display:inline-block"></span>';
      list.appendChild(sentinel);
      if (!_seerrObserver) {
        _seerrObserver = new IntersectionObserver(
          entries => { if (entries[0].isIntersecting) seerrFetchPage(false); },
          { rootMargin: "200px" }
        );
      }
      _seerrObserver.observe(sentinel);
    }
  } catch (e) {
    if (isFirst) {
      list.innerHTML = `<div class="queue-empty" style="color:var(--error)">${t('Fehler', 'Error')}: ${escS(e.message)}</div>`;
      seerrSetStatus("error");
    }
  }
  _seerrLoading = false;
}

function seerrRenderCard(req) {
  const poster = req.posterUrl
    ? `<img class="seerr-card-poster" src="${req.posterUrl}" alt="" loading="lazy">`
    : `<div class="seerr-card-poster seerr-card-poster-placeholder"><img src="/static/placeholder.svg" style="width: 100%;object-fit: cover;object-position: center;height: 120%;min-width: 100%;min-height: 120%;"/></div>`;

  const year = req.firstAirDate || "";

  const release = req.releaseDate || "";

  function _formatDate(dateString) {
  if (!dateString) return "";
  
  const monate = [
    t("Januar", "January"), 
    t("Februar", "February"), 
    t("März", "March"), 
    t("April", "April"), 
    t("Mai", "May"), 
    t("Juni", "June"), 
    t("Juli", "July"), 
    t("August", "August"), 
    t("September", "September"), 
    t("Oktober", "October"), 
    t("November", "November"), 
    t("Dezember", "December")
  ];
  
  const [year, month, day] = dateString.split("-");
  
  const monatsName = monate[parseInt(month, 10) - 1];
  
  return `${parseInt(day, 10)}. ${monatsName} ${year}`;
}

  // Season badges
  let seasonBadges = "";
  if (!req.isMovie) {
    const reqSeasons = Array.isArray(req.requestedSeasons) && req.requestedSeasons.length
      ? req.requestedSeasons : [];
    if (reqSeasons.length) {
      seasonBadges = `<div class="seerr-season-badges">
        <span class="seerr-season-label">${t('Staffel', 'Season')}${window.__LANG === 'de' && reqSeasons.length !== 1 ? 'n' : ''}</span>
        ${reqSeasons.map(n => `<span class="seerr-season-badge">${n}</span>`).join("")}
      </div>`;
    } else if (req.numberOfSeasons) {
      seasonBadges = `<div class="seerr-season-badges"><span class="seerr-season-label">${req.numberOfSeasons} ${t('Staffel', 'Season')}${window.__LANG === 'de' && req.numberOfSeasons !== 1 ? 'n' : ''}</span></div>`;
    }
  } else {
    seasonBadges = `<div class="seerr-season-badges"><span class="seerr-season-label">${t('Film', 'Movie')}</span></div>`;
  }

  const STATUS_LABEL = { 1: t("Ausstehend", "Pending"), 2: t("Angenommen", "Approved") };
  const STATUS_DOWN  = { 3: t("Nicht geladen", "Not loaded"), 4: t("Teilweise", "Partial") };
  const STATUS_CLASS = { 1: "seerr-status-pending", 2: "seerr-status-approved", 3: "seerr-status-notavailable", 4: "seerr-status-partial" };

  const statusLabel     = STATUS_LABEL[req.status] || "";
  const statusClass     = STATUS_CLASS[req.status] || "";
  const statusLabelDown = STATUS_DOWN[req.downloadStatus] || "";
  const statusClassDown = STATUS_CLASS[req.downloadStatus] || "";

  const activePill  = statusLabelDown || statusLabel;
  const activeClass = statusLabelDown ? statusClassDown : statusClass;

  const reqBy = req.requestedBy ? escS(req.requestedBy) : "";
  const date  = req.createdAt ? formatSeerrDate(req.createdAt) : "";
  const overview = req.overview ? escS(req.overview) : "";

  let syncStatus = "";
  if (!req.isMovie) {
    const uniqueId = `autosync-pill-${req.id}`;
    
    syncStatus = `<span id="${uniqueId}" class="seerr-status-pill" style="display: none;"></span>`;

    checkAutosyncTitle(req.title).then(isInSync => {
      setTimeout(() => {
        const pillElement = document.getElementById(uniqueId);
        
        if (!pillElement) {
          console.warn(t("[Autosync] Element mit ID ${uniqueId} wurde noch nicht im DOM gefunden!", "[Autosync] Element with ID ${uniqueId} was not found in the DOM!"));
          return;
        }

        if (isInSync) {
          console.log(t("[Autosync] Match gefunden für: ${req.title} -> Setze 'In Sync'", "[Autosync] Match found for: ${req.title} -> Set to 'In Sync'"));
          pillElement.classList.add('seerr-status-available');
          pillElement.textContent = t("In Sync", "In Sync");
          pillElement.style.display = '';
        } else {
          console.log(t("[Autosync] Kein Match für: ${req.title}", "[Autosync] No match for: ${req.title}"));
          pillElement.textContent = ''; 
          pillElement.style.display = 'none';
        }
      }, 0);
    }).catch(err => console.error(t("Fehler beim Updaten der Pill:", "Error updating pill:"), err));
  }

  const bodyStyle = req.backdropUrl
    ? `style="--seerr-backdrop:url('${req.backdropUrl}')"`
    : `style="--seerr-backdrop:url('/static/placeholder.svg')"`;

  return `<div class="seerr-card" data-has-backdrop="1" data-req-id="${req.id}">

    <button class="seerr-hide-btn" title="${t('Verstecken', 'Hide')}" data-req-id="${req.id}" data-title="${escS(req.title)}" data-poster="${escS(req.posterUrl || '')}" onclick="seerrHideCard(this, event)">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="16" height="16">
        <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
        <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
        <line x1="1" y1="1" x2="23" y2="23"/>
      </svg>
    </button>

    ${poster}

    <div class="seerr-card-body"${bodyStyle}>
      <div class="seerr-card-body-inner">
        ${release ? `<div class="seerr-card-year">${escS(_formatDate(release))}</div>` : ""}
        <div class="seerr-card-title">${escS(req.title)}</div>
        ${seasonBadges}
        ${overview ? `<p class="seerr-card-overview">${overview}</p>` : ""}
      </div>
    </div>

    <div class="seerr-card-right">
      ${activePill ? `<div class="seerr-card-status-row">
        <span class="seerr-card-status-label">Status</span>
        <span class="seerr-status-pill ${activeClass}">${activePill}</span>
        ${syncStatus}
      </div>` : ""}
      ${reqBy || date ? `<div class="seerr-card-req-meta">
        ${t('Angefragt', 'Requested')}${date ? ` ${escS(date)}` : ""}${reqBy ? ` ${t('von', 'by')} <strong>${reqBy}</strong>` : ""}
      </div>` : ""}
      <div class="seerr-card-actions">
        <button class="btn btn-primary btn-sm seerr-search-btn"
          data-id="${req.id}"
          data-status="${req.status}"
          data-title="${escS(req.title)}"
          data-is-movie="${req.isMovie ? '1' : '0'}">${t('Suchen', 'Search')}</button>
         ${(typeof seerrCanDecline !== "undefined" && seerrCanDecline && req.status !== 2) ? `
        <button class="btn btn-sm btn-reject seerr-decline-btn"
          data-id="${req.id}">${t('Ablehnen', 'Decline')}</button>` : ""}
      </div>
    </div>

  </div>`;
}

// ---------------------------------------------------------------
// Search modal
// ---------------------------------------------------------------

function openSeerrSearch(reqId, title, status, isMovie) {
  _seerrCurrentReqId = reqId;
  _seerrCurrentStatus = status;
  _seerrIsMovie = !!isMovie;

  const titleEl = document.getElementById("seerrSearchTitle");
  if (titleEl) titleEl.textContent = isMovie ? "Film suchen" : "Serie suchen";

  document.getElementById("seerrSearchInput").value = title || "";
  document.getElementById("seerrSearchResults").innerHTML = "";
  document.getElementById("seerrSearchOverlay").style.display = "block";
  document.body.style.overflow = "hidden";
  document.getElementById("seerrSearchInput").focus();

  // Show decline button only for admins with a request ID
  const declineBtn = document.getElementById("seerrSearchDeclineBtn");
  if (declineBtn) declineBtn.style.display = (reqId && (typeof seerrCanDecline !== "undefined" && seerrCanDecline)) ? "" : "none";

  // Auto-search with title
  if (title) seerrDoSearch();
}

function closeSeerrSearch() {
  document.getElementById("seerrSearchOverlay").style.display = "none";
  document.body.style.overflow = "";
}

async function seerrDoSearch() {
  const q = document.getElementById("seerrSearchInput").value.trim();
  if (!q) return;

  const container = document.getElementById("seerrSearchResults");
  const isSkeleton = document.body.classList.contains("skeleton-loader");

  if (isSkeleton) {
    container.innerHTML = "";
    for (let i = 0; i < 5; i++) {
      const card = document.createElement("div");
      card.className = "seerr-search-result";
      card.style.pointerEvents = "none";
      card.innerHTML = `
        <div class="seerr-search-poster skeleton"></div>
        <div class="skeleton" style="height:14px; width:45%; border-radius:4px"></div>
      `;
      container.appendChild(card);
    }
  } else {
    container.innerHTML = '<div class="queue-empty" style="padding:20px 0">' + t('Suche läuft…', 'Searching…') + '</div>';
  }

  let combined = [];

  if (_seerrIsMovie) {
    // Movies: search FilmPalast + MegaKino (movies only)
    const [fpRes, mkRes] = await Promise.allSettled([
      seerrFetchSearch(q, "filmpalast"),
      seerrFetchSearch(q, "megakino"),
    ]);
    const fpList = (fpRes.status === "fulfilled" ? fpRes.value : []).map(r => Object.assign({}, r, { _source: "FilmPalast" }));
    const mkList = (mkRes.status === "fulfilled" ? mkRes.value : [])
      .filter(r => !r.is_series)
      .map(r => Object.assign({}, r, { _source: "MegaKino" }));
    combined = fpList.concat(mkList);
  } else {
    // Series: search AniWorld + S.TO + MegaKino (+ hanime if enabled), interleave results
    const [aniRes, stoRes, mkRes, hanRes] = await Promise.allSettled([
      seerrFetchSearch(q, "aniworld"),
      seerrFetchSearch(q, "sto"),
      seerrFetchSearch(q, "megakino"),
      seerrFetchSearch(q, "hanime"),
    ]);
    const aniList = (aniRes.status === "fulfilled" ? aniRes.value : []).map(r => Object.assign({}, r, { _source: "AniWorld" }));
    const stoList = (stoRes.status === "fulfilled" ? stoRes.value : []).map(r => Object.assign({}, r, { _source: "SerienStream" }));
    const mkList = (mkRes.status === "fulfilled" ? mkRes.value : [])
      .filter(r => r.is_series)
      .map(r => Object.assign({}, r, { _source: "MegaKino" }));
    const hanList = (hanRes.status === "fulfilled" ? hanRes.value : []).map(r => Object.assign({}, r, { _source: "hanime 18+" }));
    // Interleave: alternate aniworld/sto so both appear near the top
    const maxLen = Math.max(aniList.length, stoList.length);
    for (let i = 0; i < maxLen; i++) {
      if (i < aniList.length) combined.push(aniList[i]);
      if (i < stoList.length) combined.push(stoList[i]);
    }
    // Append MegaKino series after the interleaved AniWorld/S.TO block
    combined = combined.concat(mkList);
    // hanime (adult) last; empty unless the source is enabled server-side.
    combined = combined.concat(hanList);
  }

  seerrRenderSearchResults(combined);
}

async function seerrFetchSearch(q, site) {
  const resp = await fetch("/api/search", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ keyword: q, site }),
  });
  const data = await resp.json();
  return data.results || [];
}

function seerrRenderSearchResults(results) {
  const container = document.getElementById("seerrSearchResults");
  if (!results || !results.length) {
    container.innerHTML = '<div class="queue-empty" style="padding:20px 0">Keine Ergebnisse.</div>';
    return;
  }
  container.innerHTML = results.map((r, i) =>
    `<div class="seerr-search-result seerr-result-btn" data-url="${escS(r.url)}">
      <div class="seerr-search-poster seerr-card-poster-placeholder" id="seerrPoster-${i}">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" style="opacity:.3">
          <rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/>
        </svg>
      </div>
      <span class="seerr-search-title">${escS(r.title)}</span>
      ${r._source ? `<span class="seerr-source-pill">${escS(r._source)}</span>` : ""}
    </div>`
  ).join("");

  // Fetch posters in background
  results.forEach((r, i) => {
    fetch("/api/series?url=" + encodeURIComponent(r.url))
      .then(res => res.json())
      .then(data => {
        if (!data.poster_url) return;
        const el = document.getElementById("seerrPoster-" + i);
        if (!el) return;
        el.innerHTML = `<img src="${escS(proxyImg(data.poster_url))}" style="width:100%;height:100%;object-fit:cover;border-radius:4px" alt="" loading="lazy">`;
        el.classList.remove("seerr-card-poster-placeholder");
      })
      .catch(() => { });
  });
}

// ---------------------------------------------------------------
// Series modal + download/approve
// ---------------------------------------------------------------
// The old Seerr-only series modal (openSeerrSeries, seerrBuildAccordion,
// seerrUpdateLangDropdown, seerrSetModalActions, seerrStartDownload, the
// VeeV-check helpers, etc.) has been removed. Series/movies opened from a
// Seerr request now use the standard modal from shared_modals.html via
// openSeriesFromSeerr() (app.js) -- see that function and the download hook
// in _submitDownloadGroups() for the approve-then-download flow, and
// closeModal() / _updateSeerrModalActions() for teardown and button labels.

// ---------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------

function seerrSetStatus(state, label) {
  const wrap = document.getElementById("seerrStatus");
  const dot = document.getElementById("seerrStatusDot");
  const lbl = document.getElementById("seerrStatusLabel");
  if (!wrap) return;
  if (state === "loading") {
    wrap.style.display = ""; dot.className = "lib-watcher-dot lib-watcher-starting"; lbl.textContent = t("Lädt…", "Loading…");
  } else if (state === "ok") {
    wrap.style.display = ""; dot.className = "lib-watcher-dot lib-watcher-on"; lbl.textContent = label || t("Verbunden", "Connected");
  } else if (state === "error") {
    wrap.style.display = ""; dot.className = "lib-watcher-dot lib-watcher-off"; lbl.textContent = t("Fehler", "Error");
  } else {
    wrap.style.display = "none";
  }
}

function formatSeerrDate(iso) {
  try { return new Date(iso).toLocaleDateString(window.__LANG === 'de' ? 'de-DE' : 'en-US', { day: "2-digit", month: "2-digit", year: "numeric" }); }
  catch { return ""; }
}

function escS(s) {
  const d = document.createElement("div"); d.textContent = String(s || ""); return d.innerHTML;
}
function seerrEsc(s) { return escS(s); }

// ---------------------------------------------------------------
// Hide / Verstecken
// ---------------------------------------------------------------

async function seerrHideCard(btn, event) {
  event.stopPropagation();
  const reqId = parseInt(btn.dataset.reqId);
  const title = btn.dataset.title || "";
  const posterUrl = btn.dataset.poster || "";
  try {
    await fetch(`/api/seerr/requests/${reqId}/hide`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, posterUrl }),
    });
    // Remove card from DOM
    const card = document.querySelector(`.seerr-card[data-req-id="${reqId}"]`);
    if (card) card.remove();
    // Update total count badge
    if (_seerrTotal !== null) {
      _seerrTotal = Math.max(0, _seerrTotal - 1);
      const badge = document.getElementById("seerrBadge");
      if (badge) { badge.textContent = _seerrTotal; badge.style.display = _seerrTotal > 0 ? "" : "none"; }
      seerrSetStatus("ok", `${_seerrTotal} ${t("Anfragen", "requests")}`);
    }
  } catch (e) {
    showToast(t("Fehler beim Verstecken: ", "Error hiding: ") + e.message);
  }
}

async function seerrOpenHiddenModal() {
  try {
    const resp = await fetch("/api/seerr/hidden");
    const data = await resp.json();
    const items = data.hidden || [];
    const list = document.getElementById("seerrHiddenList");
    if (!items.length) {
      list.innerHTML = `<div class="queue-empty">${t("Keine versteckten Anfragen.", "No hidden requests.")}</div>`;
    } else {
      list.innerHTML = items.map(item => `
        <div class="seerr-hidden-row" data-req-id="${item.seerr_request_id}">
          ${item.poster_url ? `<img class="seerr-hidden-poster" src="${escS(item.poster_url)}" alt="" loading="lazy">` : `<div class="seerr-hidden-poster seerr-hidden-poster-placeholder"></div>`}
          <span class="seerr-hidden-title">${escS(item.title) || `#${item.seerr_request_id}`}</span>
          <button class="btn btn-sm btn-secondary" onclick="seerrUnhide(${item.seerr_request_id})">${t("Einblenden", "Show")}</button>
        </div>
      `).join("");
    }
    document.getElementById("seerrHiddenOverlay").style.display = "block";
    document.body.style.overflow = "hidden";
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message);
  }
}

async function seerrUnhide(reqId) {
  try {
    await fetch(`/api/seerr/requests/${reqId}/unhide`, { method: "POST" });
    const row = document.querySelector(`#seerrHiddenList .seerr-hidden-row[data-req-id="${reqId}"]`);
    if (row) row.remove();
    const list = document.getElementById("seerrHiddenList");
    if (!list.querySelector(".seerr-hidden-row")) {
      list.innerHTML = `<div class="queue-empty">${t("Keine versteckten Anfragen.", "No hidden requests.")}</div>`;
    }
    // Reload main list to show re-enabled card
    seerrLoad();
  } catch (e) {
    showToast(t("Fehler: ", "Error: ") + e.message);
  }
}

function seerrCloseHiddenModal() {
  document.getElementById("seerrHiddenOverlay").style.display = "none";
  document.body.style.overflow = "";
}

// ---------------------------------------------------------------
// Init
// ---------------------------------------------------------------

// ---------------------------------------------------------------
// Decline / Ablehnen
// ---------------------------------------------------------------

async function seerrDeclineRequest(reqId) {
  if (!confirm(t("Anfrage wirklich ablehnen? Diese Aktion kann nicht rückgängig gemacht werden.", "Really decline this request? This action cannot be undone."))) return;
  try {
    const resp = await fetch(`/api/seerr/requests/${reqId}/decline`, { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.status);
    closeSeerrSearch();
    if (typeof closeModal === "function") closeModal();
    seerrLoad();
  } catch (e) {
    alert(t("Fehler beim Ablehnen: ", "Error declining: ") + e.message);
  }
}

function seerrDeclineFromSearch() {
  if (_seerrCurrentReqId) seerrDeclineRequest(_seerrCurrentReqId);
}

// Event delegation for search buttons on the card list
document.getElementById("seerrList").addEventListener("click", function (e) {
  // Decline button
  const decBtn = e.target.closest(".seerr-decline-btn");
  if (decBtn) {
    seerrDeclineRequest(parseInt(decBtn.dataset.id));
    return;
  }
  // Search button
  const btn = e.target.closest(".seerr-search-btn");
  if (!btn) return;
  openSeerrSearch(
    parseInt(btn.dataset.id),
    btn.dataset.title,
    parseInt(btn.dataset.status),
    btn.dataset.isMovie === "1"
  );
});

// Event delegation for search results → open the standard series/movie
// modal (shared_modals.html / app.js), carrying the Seerr request context
// (reqId/pending/isMovie) so it can approve-then-download and offer Decline.
document.getElementById("seerrSearchResults").addEventListener("click", function (e) {
  const row = e.target.closest(".seerr-result-btn");
  if (!row) return;
  closeSeerrSearch();
  if (typeof openSeriesFromSeerr === "function") {
    openSeriesFromSeerr(row.dataset.url, _seerrCurrentReqId, _seerrCurrentStatus === 1, _seerrIsMovie);
  }
});

seerrLoad();
