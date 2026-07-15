// ===== Seerr requests page =====

const SEERR_PAGE_SIZE = 20;

// CineInfo settings (loaded once on page init)
let _seerrCineinfo = null;
async function _loadSeerrCineinfo() {
  try {
    const r = await fetch("/api/settings");
    const data = await r.json();
    _seerrCineinfo = data.cineinfo || {};
  } catch (e) { _seerrCineinfo = {}; }
}

// Provider brand colours (mirrors app.js _providerColors)
const _seerrProvColors = {
  'Netflix': '#E50914', 'Netflix basic with Ads': '#E50914', 'Netflix Standard with Ads': '#E50914',
  'Amazon Prime Video': '#00A8E0', 'Amazon Video': '#00A8E0',
  'Disney+': '#113CCF', 'Apple TV+': '#555', 'Apple TV Store': '#555',
  'Crunchyroll': '#F47521', 'WOW': '#00B4D8', 'RTL+': '#FF6900', 'Joyn': '#00C896',
  'Paramount+': '#0064FF', 'Max': '#5822B7', 'HBO Max': '#5822B7',
  'MUBI': '#C2410C', 'Hulu': '#1CE783', 'MagentaTV': '#E20074',
  'ARD Mediathek': '#003D5B', 'ZDFmediathek': '#008CD2',
};

function _seerrMakeProviderPill(name) {
  const color = _seerrProvColors[name];
  const pill = document.createElement('span');
  pill.style.cssText = [
    'display:inline-flex', 'align-items:center', 'gap:6px',
    'font-size:0.75rem', 'font-weight:600', 'padding:4px 12px 4px 8px',
    'border-radius:99px',
    'border:1.5px solid ' + (color ? color + '60' : 'rgba(148,163,184,.35)'),
    'background:var(--bg-elevated,#1a1a28)',
    'color:' + (color || 'var(--text-secondary,#9191b0)'),
    'white-space:nowrap', 'line-height:1.4', 'cursor:default',
  ].join(';');
  if (color) {
    const dot = document.createElement('span');
    dot.style.cssText = 'width:7px;height:7px;border-radius:50%;background:' + color + ';flex-shrink:0;display:inline-block';
    pill.appendChild(dot);
  }
  const lbl = document.createElement('span');
  lbl.textContent = name;
  pill.appendChild(lbl);
  return pill;
}

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


async function _enrichSeerrModal(title, imdbId) {
  if (!_seerrCineinfo || !_seerrCineinfo.tmdb_api_key) return;
  try {
    let url = '/api/tmdb/info?title=' + encodeURIComponent(title).replace(/'/g, "%27");
    if (imdbId) url += '&imdb_id=' + encodeURIComponent(imdbId).replace(/'/g, "%27");
    const d = await (await fetch(url)).json();
    if (!d.found) return;

    // ── Providers ────────────────────────────────────────────────
    if (_seerrCineinfo.show_providers !== '0' && d.providers && d.providers.length) {
      const provEl = document.getElementById('seerrTmdbProviders');
      if (provEl) {
        provEl.innerHTML = '';
        provEl.style.cssText = 'display:flex;flex-wrap:wrap;gap:5px;margin:4px 0 16px;max-height:74px;overflow:hidden;position:relative';
        const MAX = 6, visible = d.providers.slice(0, MAX), rest = d.providers.length - MAX;
        visible.forEach(p => provEl.appendChild(_seerrMakeProviderPill(p)));
        if (rest > 0) {
          const more = document.createElement('span');
          more.textContent = '+' + rest + ' mehr';
          more.style.cssText = 'display:inline-flex;align-items:center;font-size:0.72rem;font-weight:600;padding:4px 10px;border-radius:99px;border:1.5px solid rgba(148,163,184,.3);background:var(--bg-elevated,#1a1a28);color:var(--text-muted,#55556a);white-space:nowrap;cursor:default';
          provEl.appendChild(more);
        }
      }
    }

    // ── TMDB Genres (replace site genres if enabled) ─────────────
    if (_seerrCineinfo.show_genres === '1' && d.genres && d.genres.length) {
      const genresEl = document.getElementById('seerrModalGenres');
      if (genresEl) {
        genresEl.innerHTML = '';
        d.genres.forEach(g => { const sp = document.createElement('span'); sp.textContent = g; genresEl.appendChild(sp); });
      }
    }

    // ── Rating badge next to title ────────────────────────────────
    if (_seerrCineinfo.show_rating === '1' && d.vote_average) {
      const titleEl = document.getElementById('seerrModalTitle');
      if (titleEl) {
        titleEl.style.cssText = 'display:flex;align-items:center;flex-wrap:wrap;gap:8px;margin:0 0 4px';
        const old = titleEl.querySelector('#seerrTmdbRating');
        if (old) old.remove();
        const score = d.vote_average.toFixed(1);
        const col = d.vote_average >= 7 ? '#4ade80' : d.vote_average >= 5 ? '#fbbf24' : '#f87171';
        const brd = d.vote_average >= 7 ? 'rgba(74,222,128,.4)' : d.vote_average >= 5 ? 'rgba(251,191,36,.4)' : 'rgba(248,113,113,.4)';
        const badge = document.createElement('span');
        badge.id = 'seerrTmdbRating';
        badge.style.cssText = 'display:inline-flex;align-items:center;gap:4px;font-size:0.72rem;font-weight:700;padding:2px 8px 2px 6px;border-radius:99px;border:1px solid ' + brd + ';background:rgba(0,0,0,.22);color:' + col + ';white-space:nowrap;cursor:default;letter-spacing:.01em;flex-shrink:0';
        badge.innerHTML = '<svg width="10" height="10" viewBox="0 0 24 24" fill="' + col + '" style="flex-shrink:0"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>' + score;
        titleEl.appendChild(badge);
      }
    }

    // ── FSK under poster ─────────────────────────────────────────
    if (_seerrCineinfo.show_fsk !== '0' && d.fsk) {
      const fskEl = document.getElementById('seerrTmdbFsk');
      if (fskEl) {
        const n = parseInt(d.fsk, 10);
        const fp = ({ 0: { bg: 'rgba(255,255,255,.07)', bc: 'rgba(255,255,255,.3)', c: '#d1d5db' }, 6: { bg: 'rgba(234,179,8,.12)', bc: 'rgba(234,179,8,.55)', c: '#fbbf24' }, 12: { bg: 'rgba(34,197,94,.12)', bc: 'rgba(34,197,94,.5)', c: '#4ade80' }, 16: { bg: 'rgba(59,130,246,.12)', bc: 'rgba(59,130,246,.5)', c: '#60a5fa' }, 18: { bg: 'rgba(239,68,68,.12)', bc: 'rgba(239,68,68,.5)', c: '#f87171' } })[n] || { bg: 'rgba(148,163,184,.1)', bc: 'rgba(148,163,184,.35)', c: '#94a3b8' };
        fskEl.textContent = 'FSK ' + d.fsk;
        fskEl.style.cssText = 'display:block;font-size:0.75rem;font-weight:700;padding:3px 10px;border-radius:99px;border:1px solid ' + fp.bc + ';background:' + fp.bg + ';color:' + fp.c + ';text-align:center;white-space:nowrap;letter-spacing:.02em;width:100%;box-sizing:border-box';
      }
    }
  } catch (e) { /* best-effort */ }
}

let _seerrSkip = 0;
let _seerrTotal = null;
let _seerrLoading = false;
let _seerrObserver = null;

// State for the search / series modals
let _seerrIsMovie = false;
let _seerrCurrentReqId = null;   // Seerr request id when series modal is open
let _seerrCurrentStatus = null;  // 1=pending, 2=approved
let _seerrSeriesUrl = null;
let _seerrSeriesTitle = "";
let _seerrCustomPaths = [];
// { "German Dub": ["VOE", "Vidoza", ...], ... } — the real hoster list per
// language for the open title, from /api/providers. null until fetched via
// seerrFetchSeriesProviders() (used for both series and movies).
let _seerrAvailableProviders = null;

// ---------------------------------------------------------------
// Card list + lazy loading
// ---------------------------------------------------------------

// Load CineInfo settings when page initialises
_loadSeerrCineinfo();

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
// Series modal
// ---------------------------------------------------------------

async function openSeerrSeries(url) {
  closeSeerrSearch();
  _seerrSeriesUrl = url;

  const modal = document.getElementById("seerrSeriesModal");
  const isSkeleton = document.body.classList.contains("skeleton-loader");

  document.getElementById("seerrModalPoster").src = "";

  if (isSkeleton) {
    modal.classList.add("skeleton");
    document.getElementById("seerrModalPoster").style.opacity = "0";
    document.getElementById("seerrModalTitle").innerHTML = '<div style="height:28px; width:60%; background:rgba(255,255,255,0.03); border-radius:6px; margin-bottom:8px"></div>';
    document.getElementById("seerrModalGenres").innerHTML = '<div style="height:14px; width:40%; background:rgba(255,255,255,0.03); border-radius:4px"></div>';
    document.getElementById("seerrModalYear").textContent = "";
    document.getElementById("seerrModalDesc").innerHTML = '<div style="height:14px; width:100%; background:rgba(255,255,255,0.03); border-radius:4px; margin-bottom:6px"></div><div style="height:14px; width:80%; background:rgba(255,255,255,0.03); border-radius:4px"></div>';
  } else {
    modal.classList.remove("skeleton");
    document.getElementById("seerrModalPoster").style.opacity = "";
    document.getElementById("seerrModalTitle").textContent = t("Lädt…", "Loading…");
    document.getElementById("seerrModalTitle").style.cssText = "";
    document.getElementById("seerrModalGenres").textContent = "";
    document.getElementById("seerrModalYear").textContent = "";
    document.getElementById("seerrModalDesc").textContent = "";
  }

  document.getElementById("seerrAccordion").innerHTML = "";
  document.getElementById("seerrEpSpinner").style.display = "block";

  const banner = document.getElementById("seerrLangBanner");
  if (banner) {
    if (isSkeleton) {
      banner.style.display = "block";
      banner.innerHTML = "";
      banner.className = "lang-avail-banner skeleton";
    } else {
      banner.style.display = "none";
      banner.innerHTML = "";
      banner.className = "lang-avail-banner";
    }
  }

  document.getElementById("seerrSelectAll").checked = false;
  _seerrAvailableProviders = null;  // fresh series → don't reuse the last one's hosters
  // Reset TMDB elements
  const _rp = document.getElementById("seerrTmdbProviders");
  if (_rp) { _rp.innerHTML = ""; _rp.style.display = "none"; }
  const _rf = document.getElementById("seerrTmdbFsk");
  if (_rf) { _rf.textContent = ""; _rf.style.display = "none"; }
  seerrSetModalActions();
  await seerrLoadCustomPaths();

  document.getElementById("seerrSeriesOverlay").style.display = "block";
  document.body.style.overflow = "hidden";

  // Update lang options based on site
  const isSto = url.includes("s.to") || url.includes("serienstream.to");
  const isFp = url.includes("filmpalast.to");
  const isMk = url.includes("megakino");
  const isHan = url.includes("hanime.tv");
  seerrUpdateLangDropdown(isSto, isFp || isMk, null, isHan);

  try {
    const [seriesResp, seasonsResp] = await Promise.all([
      fetch("/api/series?url=" + encodeURIComponent(url)),
      fetch("/api/seasons?url=" + encodeURIComponent(url)),
    ]);
    const seriesData = await seriesResp.json();
    const seasonsData = await seasonsResp.json();

    _seerrSeriesTitle = seriesData.title || "Unbekannt";
    document.getElementById("seerrModalTitle").textContent = _seerrSeriesTitle;
    // Genres as pills (matching main modal style)
    const _ge = document.getElementById("seerrModalGenres");
    _ge.innerHTML = "";
    (seriesData.genres || []).forEach(g => { const sp = document.createElement("span"); sp.textContent = g; _ge.appendChild(sp); });
    document.getElementById("seerrModalYear").textContent = seriesData.release_year || "";
    document.getElementById("seerrModalDesc").textContent = seriesData.description || "";
    if (seriesData.poster_url) document.getElementById("seerrModalPoster").src = proxyImg(seriesData.poster_url);

    // TMDB enrichment (providers, FSK, rating, TMDB genres)
    _enrichSeerrModal(_seerrSeriesTitle, seriesData.imdb_id || null);

    seerrBuildAccordion(seasonsData.seasons || []);

    // Show/hide "Episoden" heading
    const seerrEpHeading = document.getElementById("seerrEpisodesHeading");
    if (seerrEpHeading) seerrEpHeading.style.display = seriesData.is_movie ? "none" : "";

    // FilmPalast movie: populate provider dropdown from metadata
    if (seriesData.is_movie) {
      const provSel = document.getElementById("seerrProvSelect");
      provSel.innerHTML = "";
      if (seriesData.available_providers && seriesData.available_providers.length) {
        seriesData.available_providers.forEach(p => {
          const opt = document.createElement("option");
          opt.value = opt.textContent = p;
          provSel.appendChild(opt);
        });
        // Prefer VOE if available
        const voeOpt = [...provSel.options].find(o => o.value === "VOE");
        if (voeOpt) provSel.value = "VOE";
      }
      // The static available_providers list above is only an initial
      // placeholder. The real live-availability check runs in
      // seerrBuildAccordion() via seerrFetchSeriesProviders() on the actual
      // episode URL — same unified path the main modal uses for movies.
    }
  } catch (e) {
    modal.classList.remove("skeleton");
    document.getElementById("seerrModalPoster").style.opacity = "";
    if (banner) banner.classList.remove("skeleton");
    document.getElementById("seerrModalTitle").textContent = t("Fehler beim Laden", "Error loading");
    document.getElementById("seerrEpSpinner").style.display = "none";
  }
}

function closeSeerrSeries() {
  document.getElementById("seerrSeriesOverlay").style.display = "none";
  document.body.style.overflow = "";
}

function seerrUpdateLangDropdown(isSto, isFp, foundLangs = null, isHanime = false) {
  const sel = document.getElementById("seerrLangSelect");
  sel.innerHTML = "";
  if (isHanime) {
    const opt = document.createElement("option");
    opt.value = "Japanese Dub";
    opt.textContent = t("Japanisch (Sub)", "Japanese (Sub)");
    sel.appendChild(opt);
    return;
  }
  if (isFp) {
    const opt = document.createElement("option");
    opt.value = opt.textContent = "German Dub";
    sel.appendChild(opt);
    return;
  }
  const langs = isSto
    ? (window.SEERR_STO_LANGS || {})
    : (window.SEERR_ANIWORLD_LANGS || {});
  Object.values(langs).forEach(label => {
    if (foundLangs && !foundLangs.has(label)) {
      return;
    }
    const opt = document.createElement("option");
    opt.value = opt.textContent = label;
    sel.appendChild(opt);
  });
}

async function seerrLoadCustomPaths() {
  try {
    const resp = await fetch("/api/custom-paths");
    const data = await resp.json();
    _seerrCustomPaths = data.paths || [];
    const sel = document.getElementById("seerrPathSelect");
    sel.innerHTML = '<option value="">Standard</option>';
    _seerrCustomPaths.forEach(cp => {
      const opt = document.createElement("option");
      opt.value = cp.id;
      opt.textContent = cp.name;
      sel.appendChild(opt);
    });
    sel.style.display = _seerrCustomPaths.length ? "" : "none";
  } catch (e) { /* ignore */ }
}

async function seerrFetchSeriesProviders(episodeUrl) {
  // Fetch the real hoster list for a representative episode and filter the
  // provider dropdown by live availability + the selected language, mirroring
  // the main modal's fetchProviders(). Used for every site (series and movies
  // alike); without this the Seerr modal offered the full static provider list
  // with no availability check at all.
  try {
    const resp = await fetch("/api/providers?url=" + encodeURIComponent(episodeUrl));
    const data = await resp.json();
    if (data.providers) {
      _seerrAvailableProviders = data.providers;
      seerrUpdateProviderDropdown();
    }
  } catch (e) {
    console.warn("Failed to check series provider availability:", e);
  }
}

function seerrUpdateProviderDropdown() {
  // Rebuild seerrProvSelect from the fetched availability for the currently
  // selected language. No-op until _seerrAvailableProviders is populated, so
  // the static list stays until real data arrives (series and movies alike).
  if (!_seerrAvailableProviders) return;
  const provSel = document.getElementById("seerrProvSelect");
  const langSel = document.getElementById("seerrLangSelect");
  if (!provSel) return;
  const lang = langSel ? langSel.value : "";
  const providers = _seerrAvailableProviders[lang];
  provSel.innerHTML = "";
  if (providers && providers.length) {
    providers.forEach(p => {
      const opt = document.createElement("option");
      opt.value = opt.textContent = p;
      provSel.appendChild(opt);
    });
    const voeOpt = [...provSel.options].find(o => o.value === "VOE");
    if (voeOpt) provSel.value = "VOE";
  } else {
    // Backend checked and found no working hoster for this language — don't
    // fall back to the static list (that would offer dead sources).
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = t("Keine Quelle verfügbar", "No source available");
    opt.disabled = true;
    provSel.appendChild(opt);
  }
}

function seerrSetModalActions() {
  const div = document.getElementById("seerrModalActions");
  const isPending = _seerrCurrentStatus === 1;
  const mainLabel = isPending ? t("Annehmen &amp; Herunterladen", "Approve &amp; Download") : t("Herunterladen", "Download");
  const declineBtn = (_seerrCurrentReqId && (typeof seerrCanDecline !== "undefined" && seerrCanDecline))
    ? `<button class="btn-reject" onclick="seerrDeclineRequest(${_seerrCurrentReqId})">${t('Ablehnen', 'Decline')}</button>`
    : "";
  // "Download all" makes no sense for a single movie
  const allBtn = _seerrIsMovie ? "" :
    `<button class="btn-download-all" onclick="seerrStartDownload(true)">${isPending ? t("Annehmen &amp; alle herunterladen", "Approve &amp; download all") : t("Alle herunterladen", "Download all")}</button>`;
  div.innerHTML = `
    <div class="modal-action-downloads">
      <button class="btn-download-selected" onclick="seerrStartDownload(false)">${mainLabel}</button>
      ${allBtn}
    </div>
    <div class="modal-action-controls">
      <button class="btn btn-secondary" onclick="closeSeerrSeries()">${t('Abbrechen', 'Cancel')}</button>
      ${declineBtn}
    </div>
  `;
}

// ---------------------------------------------------------------
// Episode accordion (mirrors app.js buildAccordion)
// ---------------------------------------------------------------

const SEERR_LANG_FLAGS = {
  "German Dub": "/static/flags/german.svg",
  "English Dub": "/static/flags/english.svg",
  "German Sub": "/static/flags/japanese-germanSub.svg",
  "English Sub": "/static/flags/japanese-englishSub.svg",
  "English Dub (German Sub)": "/static/flags/english-germanSub.svg",
};

function seerrBuildAccordion(seasons) {
  const accordion = document.getElementById("seerrAccordion");
  const spinner = document.getElementById("seerrEpSpinner");
  accordion.innerHTML = "";
  spinner.style.display = "block";

  const fetches = seasons.map((s, i) =>
    fetch("/api/episodes?url=" + encodeURIComponent(s.url))
      .then(r => r.json())
      .then(d => ({ index: i, episodes: d.episodes || [] }))
      .catch(() => ({ index: i, episodes: [] }))
  );

  Promise.all(fetches).then(results => {
    spinner.style.display = "none";
    results.sort((a, b) => a.index - b.index);

    // Find all languages actually present in the episodes
    const foundLangs = new Set();
    results.forEach(({ episodes }) => {
      episodes.forEach(ep => {
        if (ep.languages) {
          ep.languages.forEach(l => foundLangs.add(l));
        }
      });
    });

    if (foundLangs.size > 0) {
      const sel = document.getElementById("seerrLangSelect");
      const prevVal = sel ? sel.value : "";
      const isSto = (_seerrSeriesUrl || "").includes("s.to") || (_seerrSeriesUrl || "").includes("serienstream.to");
      const isFp = (_seerrSeriesUrl || "").includes("filmpalast.to");
      const isMk = (_seerrSeriesUrl || "").includes("megakino");
      const isHan = (_seerrSeriesUrl || "").includes("hanime.tv");
      seerrUpdateLangDropdown(isSto, isFp || isMk, foundLangs, isHan);
      if (sel && Array.from(sel.options).some(o => o.value === prevVal)) {
        sel.value = prevVal;
      }
    }

    // Provider availability: fetch the real hoster list for a representative
    // episode and filter the provider dropdown by live availability + language,
    // mirroring the main modal's fetchProviders(). Runs for EVERY site,
    // including FilmPalast & MegaKino movies. Previously movies were excluded
    // here and relied on a separate helper that kept the unchecked static list,
    // so the Seerr movie modal never dropped dead hosters like the rest of the
    // system does. Hanime is skipped: it has a single fixed source, no check.
    {
      const _u = _seerrSeriesUrl || "";
      const _providerSite = !_u.includes("hanime.tv");
      if (_providerSite) {
        let _repUrl = "";
        for (const _r of results) { if (_r.episodes.length) { _repUrl = _r.episodes[0].url; break; } }
        if (_repUrl) seerrFetchSeriesProviders(_repUrl);
      }
    }

    results.forEach(({ index, episodes }) => {
      const season = seasons[index];
      const section = document.createElement("div");
      section.className = "season-section";
      section.dataset.seasonIndex = index;

      const isSingleMovie = !!season.is_single_movie;

      const label = isSingleMovie
        ? t("Film", "Movie")
        : season.are_movies
          ? `${t("Filme", "Movies")} (${episodes.length} ${t("Episoden", "Episodes")})`
          : `${t("Staffel", "Season")} ${season.season_number} (${episodes.length} ${t("Episoden", "Episodes")})`;

      const header = document.createElement("div");
      if (isSingleMovie) {
        header.className = "season-header season-header-movie expanded";
        header.style.display = "none";
      } else {
        header.className = "season-header";
        header.innerHTML =
          `<div class="season-label"><span class="season-arrow">&#9654;</span> ${seerrEsc(label)}</div>` +
          `<label class="season-all-label" onclick="event.stopPropagation()">` +
          `<input type="checkbox" class="chb-main" onchange="seerrToggleSeasonAll(this,${index})">`+ t('Alle','All')+`</label>`;
        header.addEventListener("click", () => seerrToggleSeason(index));
      }

      const body = document.createElement("div");
      body.className = "season-body" + (isSingleMovie ? " expanded" : "");
      body.id = "seerrSeasonBody-" + index;

      episodes.forEach(ep => {
        const div = document.createElement("div");
        div.className = "episode-item";
        const title = ep.title_en || ep.title_de || "";
        const dlIcon = ep.downloaded ? '<span class="ep-downloaded" title="Downloaded">&#10003;</span>' : "";
        let langsHtml = "";
        if (ep.languages && ep.languages.length) {
          langsHtml = `<span class="ep-langs">${ep.languages.map(l => {
            const src = SEERR_LANG_FLAGS[l];
            return src ? `<img class="ep-lang-flag" src="${src}" title="${seerrEsc(l)}" alt="${seerrEsc(l)}">` : "";
          }).join("")}</span>`;
        }
        const epNumHtml = isSingleMovie ? "" : `<span class="ep-num">E${ep.episode_number}</span>`;
        const cb = `<input type="checkbox" class="chb-main" value="${seerrEsc(ep.url)}" data-season="${index}"${isSingleMovie ? " checked" : ""}>`;
        div.innerHTML = `${cb}${epNumHtml}${dlIcon}<span class="ep-title">${seerrEsc(title)}</span>${langsHtml}`;
        body.appendChild(div);
      });

      section.appendChild(header);
      section.appendChild(body);
      accordion.appendChild(section);
    });

    // Language availability banner
    seerrRenderLangBanner(results);

    // Remove skeleton classes when all is complete
    const modal = document.getElementById("seerrSeriesModal");
    if (modal) {
      modal.classList.remove("skeleton");
    }
    const poster = document.getElementById("seerrModalPoster");
    if (poster) {
      poster.style.opacity = "";
    }
  });
}

function seerrToggleSeason(index) {
  const section = document.querySelector(`#seerrAccordion [data-season-index="${index}"]`);
  if (!section) return;
  section.querySelector(".season-header").classList.toggle("expanded");
  section.querySelector(".season-body").classList.toggle("expanded");
}

function seerrToggleSeasonAll(cb, index) {
  const body = document.getElementById("seerrSeasonBody-" + index);
  if (body) body.querySelectorAll(".episode-item input[type=checkbox]").forEach(c => c.checked = cb.checked);
  seerrSyncSelectAll();
}

function seerrToggleSelectAll() {
  const checked = document.getElementById("seerrSelectAll").checked;
  document.querySelectorAll("#seerrAccordion .episode-item input[type=checkbox]").forEach(c => c.checked = checked);
  document.querySelectorAll("#seerrAccordion .season-all-label input[type=checkbox]").forEach(c => c.checked = checked);
}

function seerrSyncSelectAll() {
  const all = [...document.querySelectorAll("#seerrAccordion .episode-item input[type=checkbox]")];
  document.getElementById("seerrSelectAll").checked = all.length > 0 && all.every(c => c.checked);
}

function seerrGetSelected() {
  return [...document.querySelectorAll("#seerrAccordion .episode-item input[type=checkbox]:checked")].map(c => c.value);
}

function seerrGetAll() {
  return [...document.querySelectorAll("#seerrAccordion .episode-item input[type=checkbox]")].map(c => c.value);
}

function seerrRenderLangBanner(results) {
  const banner = document.getElementById("seerrLangBanner");
  if (!banner) return;
  banner.classList.remove("skeleton");
  if ((_seerrSeriesUrl || "").includes("filmpalast.to") || (_seerrSeriesUrl || "").includes("megakino") || (_seerrSeriesUrl || "").includes("hanime.tv")) { banner.style.display = "none"; return; }
  const isSto = (_seerrSeriesUrl || "").includes("s.to") || (_seerrSeriesUrl || "").includes("serienstream.to");
  const LANG_ORDER = ["German Dub", "English Sub", "German Sub", "English Dub"];
  if (isSto) {
    LANG_ORDER.push("English Dub (German Sub)");
  }
  const LANG_SHORT = { "German Dub": "Ger. Dub", "English Sub": "Eng. Sub", "German Sub": "Ger. Sub", "English Dub": "Eng. Dub", "English Dub (German Sub)": "Eng. Dub (Ger. Sub)" };
  const counts = {};
  let total = 0;
  results.forEach(({ episodes }) => {
    episodes.forEach(ep => {
      total++;
      (ep.languages || []).forEach(l => counts[l] = (counts[l] || 0) + 1);
    });
  });
  if (!total) { banner.style.display = "none"; return; }
  banner.innerHTML = LANG_ORDER.map(lang => {
    const n = counts[lang] || 0;
    const cls = n === total ? "lang-avail-full" : n === 0 ? "lang-avail-none" : "lang-avail-partial";
    return `<span class="lang-avail-pill ${cls}">${LANG_SHORT[lang]}: ${n}&thinsp;/&thinsp;${total}</span>`;
  }).join("");
  banner.style.display = "flex";
}

// ---------------------------------------------------------------
// Download + approve
// ---------------------------------------------------------------

// ── VeeV Availability Check (mirrors app.js — seerr.html loads only seerr.js) ─

function showVeevCheck() {
  const overlay = document.getElementById("veevCheckOverlay");
  if (!overlay) return;
  if (overlay.parentNode !== document.body) document.body.appendChild(overlay);
  overlay.style.cssText = "display:flex;position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:99999;align-items:center;justify-content:center;backdrop-filter:blur(4px)";
  const spinnerWrap = document.getElementById("veevCheckSpinnerWrap");
  if (spinnerWrap) spinnerWrap.style.display = "flex";
  const textEl = document.getElementById("veevCheckText");
  if (textEl) { textEl.style.display = ""; textEl.textContent = t("Es wird überprüft ob der ausgewählte Inhalt auf Veev verfügbar ist", "Checking if the selected content is available on Veev"); }
  const errEl = document.getElementById("veevCheckError");
  if (errEl) { errEl.style.display = "none"; errEl.textContent = ""; }
  const closeBtn = document.getElementById("veevCheckCloseBtn");
  if (closeBtn) closeBtn.style.display = "none";
}

function closeVeevCheck() {
  const overlay = document.getElementById("veevCheckOverlay");
  if (overlay) overlay.style.display = "none";
}

async function veevCheckAvailability(episodeUrl) {
  showVeevCheck();
  try {
    const resp = await fetch("/api/veev/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ episode_url: episodeUrl }),
    });
    const data = await resp.json();
    if (data.available) {
      closeVeevCheck();
      return true;
    }
    const spinnerWrap = document.getElementById("veevCheckSpinnerWrap");
    if (spinnerWrap) spinnerWrap.style.display = "none";
    const textEl = document.getElementById("veevCheckText");
    if (textEl) textEl.style.display = "none";
    const errEl = document.getElementById("veevCheckError");
    if (errEl) { errEl.textContent = data.error || t("Dieser Film ist auf Veev momentan nicht verfügbar.", "This movie is currently not available on Veev."); errEl.style.display = "block"; }
    const closeBtn = document.getElementById("veevCheckCloseBtn");
    if (closeBtn) closeBtn.style.display = "inline-block";
    return false;
  } catch (e) {
    const spinnerWrap = document.getElementById("veevCheckSpinnerWrap");
    if (spinnerWrap) spinnerWrap.style.display = "none";
    const textEl = document.getElementById("veevCheckText");
    if (textEl) textEl.style.display = "none";
    const errEl = document.getElementById("veevCheckError");
    if (errEl) { errEl.textContent = t("Fehler bei der Verfügbarkeitsprüfung: ", "Availability check error: ") + e.message; errEl.style.display = "block"; }
    const closeBtn = document.getElementById("veevCheckCloseBtn");
    if (closeBtn) closeBtn.style.display = "inline-block";
    return false;
  }
}

async function seerrStartDownload(all) {
  const episodes = all ? seerrGetAll() : seerrGetSelected();
  if (!episodes.length) return;

  const language = document.getElementById("seerrLangSelect").value;
  const provider = document.getElementById("seerrProvSelect").value;
  const pathSel = document.getElementById("seerrPathSelect");
  const customPathId = pathSel && pathSel.value ? parseInt(pathSel.value) : null;

  // VeeV availability check before starting download
  if (provider && provider.toLowerCase().replace(/\s+(hd|hq)$/i, "") === "veev") {
    const ok = await veevCheckAvailability(episodes[0]);
    if (!ok) return; // Overlay bleibt mit Fehlermeldung offen
  }

  // If pending → approve on Seerr first
  if (parseInt(_seerrCurrentStatus) === 1 && _seerrCurrentReqId) {
    try {
      const approveResp = await fetch(`/api/seerr/requests/${_seerrCurrentReqId}/approve`, { method: "POST" });
      if (!approveResp.ok) {
        const err = await approveResp.json().catch(() => ({}));
        console.warn("Seerr approve failed:", approveResp.status, err);
        // Show warning but still proceed with download
        if (typeof showToast === "function") showToast("⚠ " + t("Seerr-Genehmigung fehlgeschlagen: ", "Seerr approval failed: ") + (err.error || approveResp.status));
      }
    } catch (e) {
      console.warn("Seerr approve error:", e);
    }
  }

  // Start download
  const body = { episodes, language, provider, title: _seerrSeriesTitle, series_url: _seerrSeriesUrl };
  if (customPathId) body.custom_path_id = customPathId;

  try {
    const resp = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (data.error) { alert(data.error); return; }
  } catch (e) { alert(t("Download fehlgeschlagen: ", "Download failed: ") + e.message); return; }

  closeSeerrSeries();
  // Reload to reflect new status
  seerrLoad();
}

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
    closeSeerrSeries();
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

// Event delegation for search results → open series modal
document.getElementById("seerrSearchResults").addEventListener("click", function (e) {
  const row = e.target.closest(".seerr-result-btn");
  if (!row) return;
  openSeerrSeries(row.dataset.url);
});

// Re-filter the provider dropdown by availability whenever the language
// changes (series and movies alike).
// seerrUpdateProviderDropdown no-ops until availability has been fetched.
const _seerrLangSel = document.getElementById("seerrLangSelect");
if (_seerrLangSel) _seerrLangSel.addEventListener("change", seerrUpdateProviderDropdown);

seerrLoad();
