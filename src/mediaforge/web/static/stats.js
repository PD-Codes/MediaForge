// Stats page logic

async function loadStats(showSkeleton) {
  const container = document.getElementById("statsContent");
  // Show skeletons on the first load only. Background re-polls (e.g. while the
  // media library is still scanning) refresh silently so the whole page doesn't
  // keep flashing back to skeletons.
  if (showSkeleton !== false) renderSkeletons(container);

  try {
    const resp = await fetch("/api/stats");
    const data = await resp.json();
    renderStats(data, container);
  } catch (e) {
    container.innerHTML = '<div class="stats-loading">' + t('Fehler beim Laden der Statistiken.', 'Error loading statistics.') + '</div>';
    console.log(e);
  }
}

function renderSkeletons(container) {
  let html = '<div class="stats-kpi-row stats-kpi-main">';
  for (let i = 0; i < 8; i++) {
    html += '<div class="stat-card skeleton"></div>';
  }
  html += '</div>';

  html += '<div class="stats-section"><div class="stats-section-title skeleton" style="width:150px;height:24px;margin-bottom:12px"></div><div class="stats-kpi-row">';
  for (let i = 0; i < 5; i++) {
    html += '<div class="stat-card skeleton"></div>';
  }
  html += '</div></div>';

  container.innerHTML = html;
}

function fmtDuration(seconds) {
  if (!seconds) return "—";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function statCard(label, value, sub, color, onClick) {
  const style = color ? `--kpi-color:${color}` : "";
  const cursor = onClick ? "cursor:pointer" : "";
  return `<div class="stat-card" style="${style};${cursor}" ${onClick ? `onclick="${onClick}"` : ""}>
    <div class="stat-value">${value}</div>
    <div class="stat-label">${label}</div>
    ${sub ? `<div class="stat-sub">${sub}</div>` : ""}
  </div>`;
}

function openSpeedModal() {
  document.getElementById("speedModal").style.display = "flex";
}

function closeSpeedModal() {
  document.getElementById("speedModal").style.display = "none";
}

function openIncompleteModal() {
  const modal = document.getElementById("incompleteModal");
  if (modal) modal.style.display = "flex";
}

function closeIncompleteModal() {
  const modal = document.getElementById("incompleteModal");
  if (modal) modal.style.display = "none";
}

function openDuplicatesModal() {
  const modal = document.getElementById("duplicatesModal");
  if (modal) modal.style.display = "flex";
}

function closeDuplicatesModal() {
  const modal = document.getElementById("duplicatesModal");
  if (modal) modal.style.display = "none";
}

function escHtml(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}

// Keep the latest media stats around so the ignore/restore handlers can read
// folders, titles and slots without re-fetching.
window._mediaStats = null;

function _incompleteTableHtml(incomplete) {
  if (!incomplete || !incomplete.length) {
    return '<p class="stat-sub">' +
      t('Alle Serien sind vollständig. 🎉', 'All series are complete. 🎉') + '</p>';
  }
  let mh = '<div class="user-table-wrapper"><table class="user-table"><thead><tr>' +
    '<th style="width:34px"></th>' +
    '<th style="width:42%">' + t('Serie', 'Series') + '</th>' +
    '<th style="width:16%">' + t('Speicherort', 'Location') + '</th>' +
    '<th style="width:auto">' + t('Fehlende Episoden', 'Missing episodes') + '</th>' +
    '</tr></thead><tbody>';
  incomplete.forEach((item, idx) => {
    const miss = item.missing || [];
    const slotChips = miss.map((s) =>
      `<label class="ignore-slot-chip"><input type="checkbox" class="ign-slot chb-main" data-idx="${idx}" data-slot="${escHtml(s)}"> ${escHtml(s)}</label>`
    ).join(" ");
    mh += `<tr>
      <td style="text-align:center"><input type="checkbox" class="ign-series chb-main" data-idx="${idx}" title="${escHtml(t('Ganze Serie ignorieren', 'Ignore whole series'))}"></td>
      <td class="speed-modal-title" title="${escHtml(item.title)}">${escHtml(item.title)}</td>
      <td>${escHtml(item.location)}</td>
      <td style="color:var(--warning,#f59e0b)"><div class="ignore-slot-wrap">${slotChips}</div></td>
    </tr>`;
  });
  mh += '</tbody></table></div>';
  mh += '<div class="ignore-actions"><button class="btn-download-selected" onclick="mediaIgnoreSelected()">' +
    t('Auswahl ignorieren', 'Ignore selected') + '</button></div>';
  return mh;
}

function _ignoredTableHtml(ignored) {
  if (!ignored || !ignored.length) {
    return '<p class="stat-sub">' +
      t('Keine ignorierten Einträge.', 'No ignored entries.') + '</p>';
  }
  let mh = '<div class="user-table-wrapper"><table class="user-table"><thead><tr>' +
    '<th style="width:42%">' + t('Serie', 'Series') + '</th>' +
    '<th style="width:auto">' + t('Ignoriert', 'Ignored') + '</th>' +
    '<th style="width:90px"></th></tr></thead><tbody>';
  for (const item of ignored) {
    const slots = item.slots || [];
    const isAll = slots.includes("__all__");
    const folderEnc = encodeURIComponent(item.folder);
    let slotHtml;
    if (isAll) {
      slotHtml = `<span class="ignore-slot-chip ignore-all-chip">${escHtml(t('Ganze Serie', 'Whole series'))}</span>`;
    } else {
      slotHtml = slots.map((s) =>
        `<span class="ignore-slot-chip">${escHtml(s)} <a href="#" class="ignore-remove-x" onclick="mediaUnignore('${folderEnc}','${encodeURIComponent(s)}');return false;" title="${escHtml(t('Wiederherstellen', 'Restore'))}">×</a></span>`
      ).join(" ");
    }
    mh += `<tr>
      <td class="speed-modal-title" title="${escHtml(item.title)}">${escHtml(item.title)}</td>
      <td><div class="ignore-slot-wrap">${slotHtml}</div></td>
      <td><button class="btn btn-ghost ignore-restore-btn" onclick="mediaUnignore('${folderEnc}',null)">${escHtml(t('Alle', 'All'))}</button></td>
    </tr>`;
  }
  mh += '</tbody></table></div>';
  return mh;
}

function _renderIncompleteModal() {
  const m = window._mediaStats || {};
  const modalContent = document.getElementById("incompleteModalContent");
  if (!modalContent) return;
  const view = window._incompleteView || "incomplete";
  const ignoredCount = (m.ignored || []).length;
  let html = '<div class="ignore-tabs">';
  html += `<button class="ignore-tab${view === 'incomplete' ? ' active' : ''}" onclick="switchIncompleteView('incomplete')">${escHtml(t('Unvollständig', 'Incomplete'))}</button>`;
  html += `<button class="ignore-tab${view === 'ignored' ? ' active' : ''}" onclick="switchIncompleteView('ignored')">${escHtml(t('Ignoriert', 'Ignored'))} (${ignoredCount})</button>`;
  html += '</div>';
  html += view === "ignored" ? _ignoredTableHtml(m.ignored) : _incompleteTableHtml(m.incomplete);
  modalContent.innerHTML = html;
}

function switchIncompleteView(view) {
  window._incompleteView = view;
  _renderIncompleteModal();
}

function _dupSlotLabel(item) {
  // Series episodes carry an "SxEy" slot; movies use the sentinel "movie".
  if (item.kind === "movie" || item.slot === "movie") return t("Film", "Movie");
  return item.slot;
}

function _duplicatesTableHtml(duplicates) {
  if (!duplicates || !duplicates.length) {
    return '<p class="stat-sub">' +
      t('Keine Duplikate gefunden. 🎉', 'No duplicates found. 🎉') + '</p>';
  }
  let mh = '<div class="user-table-wrapper"><table class="user-table"><thead><tr>' +
    '<th style="width:34%">' + t('Serie / Film', 'Series / Movie') + '</th>' +
    '<th style="width:12%">' + t('Episode', 'Episode') + '</th>' +
    '<th style="width:14%">' + t('Speicherort', 'Location') + '</th>' +
    '<th style="width:auto">' + t('Vorhandene Versionen', 'Existing versions') + '</th>' +
    '</tr></thead><tbody>';
  duplicates.forEach((item) => {
    const files = item.files || [];
    const langBadge = item.language
      ? ` <span class="ignore-slot-chip">${escHtml(item.language)}</span>` : "";
    const versionChips = files.map((f) => {
      const res = f.resolution || t('unbekannt', 'unknown');
      const codec = f.video_codec ? ` · ${escHtml(f.video_codec)}` : "";
      return `<span class="ignore-slot-chip" title="${escHtml(f.path || f.file || "")}">${escHtml(res)}${codec}</span>`;
    }).join(" ");
    mh += `<tr>
      <td class="speed-modal-title" title="${escHtml(item.title)}">${escHtml(item.title)}${langBadge}</td>
      <td>${escHtml(_dupSlotLabel(item))}</td>
      <td>${escHtml(item.location)}</td>
      <td style="color:var(--warning,#f59e0b)"><div class="ignore-slot-wrap">${versionChips}</div></td>
    </tr>`;
  });
  mh += '</tbody></table></div>';
  return mh;
}

function _renderDuplicatesModal() {
  const m = window._mediaStats || {};
  const modalContent = document.getElementById("duplicatesModalContent");
  if (!modalContent) return;
  modalContent.innerHTML = _duplicatesTableHtml(m.duplicates);
}

function mediaIgnoreSelected() {
  const data = window._mediaStats && window._mediaStats.incomplete || [];
  const items = {};
  document.querySelectorAll("#incompleteModalContent .ign-series:checked").forEach((cb) => {
    const s = data[cb.dataset.idx];
    if (s) items[s.folder] = { folder: s.folder, title: s.title, all: true };
  });
  document.querySelectorAll("#incompleteModalContent .ign-slot:checked").forEach((cb) => {
    const s = data[cb.dataset.idx];
    if (!s) return;
    if (items[s.folder] && items[s.folder].all) return; // whole series already covers it
    const e = items[s.folder] || (items[s.folder] = { folder: s.folder, title: s.title, slots: [] });
    if (e.slots) e.slots.push(cb.dataset.slot);
  });
  const payload = Object.values(items).filter((x) => x.all || (x.slots && x.slots.length));
  if (!payload.length) {
    if (typeof showToast === "function") showToast(t("Nichts ausgewählt.", "Nothing selected."));
    return;
  }
  fetch("/api/media/ignore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ items: payload }),
  }).then((r) => r.json()).then(() => loadStats(false))
    .catch(() => { if (typeof showToast === "function") showToast(t("Fehler.", "Error.")); });
}

function mediaUnignore(folderEnc, slotEnc) {
  const folder = decodeURIComponent(folderEnc);
  const body = { folder };
  if (slotEnc == null) body.all = true;
  else body.slot = decodeURIComponent(slotEnc);
  fetch("/api/media/unignore", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((r) => r.json()).then(() => loadStats(false))
    .catch(() => { if (typeof showToast === "function") showToast(t("Fehler.", "Error.")); });
}

function renderMediaSection(m) {
  // Stash for the ignore/restore handlers and (re)build the modal content.
  window._mediaStats = m;
  _renderIncompleteModal();
  _renderDuplicatesModal();

  let html = '<div class="stats-section"><h2 class="stats-section-title">' + t('Media', 'Media') + '</h2>';
  if (m.scanning) {
    html += '<div class="stat-sub" style="margin-bottom:10px">' +
      t('Mediathek wird gescannt… Werte aktualisieren sich gleich.', 'Media library is being scanned… values will update shortly.') + '</div>';
  }
  html += '<div class="stats-kpi-row">';
  html += statCard(t("Filme (Gesamt)", "Movies (Total)"), m.movies_total ?? 0, "", "#e8914a");
  html += statCard(t("Serien (Gesamt)", "Series (Total)"), m.series_total ?? 0, "", "#a78bfa");
  html += statCard(t("Serien (Vollständig)", "Series (Complete)"), m.series_complete ?? 0, "", "#22c55e");
  html += statCard(
    t("Serien (Unvollständig)", "Series (Incomplete)"),
    m.series_incomplete ?? 0,
    t("Klicken für Details", "Click for details"),
    "#f59e0b",
    "openIncompleteModal()"
  );
  html += statCard(t("Episodenzahl", "Episode count"), m.episodes_total ?? 0, "", "#6ea8fe");
  html += statCard(
    t("Duplikate", "Duplicates"),
    (m.duplicates || []).length,
    t("Klicken für Details", "Click for details"),
    "#f472b6",
    "openDuplicatesModal()"
  );
  html += '</div></div>';
  return html;
}

function renderStats(data, container) {
  const g = data.general || {};
  const q = data.queue || {};
  const s = data.sync || {};
  const m = data.media || null;

  const byStatus = q.by_status || {};
  const successRate = g.total_downloads > 0
    ? Math.round((g.completed / g.total_downloads) * 100)
    : 0;

  // --- Speed Modal ---
  if (g.last_speeds && g.last_speeds.length) {
    let modalHtml = '<div class="user-table-wrapper"><table class="user-table speed-modal-table"><thead><tr><th>' + t('Titel','Title') + '</th><th>' + t('Größe','Size') + '</th><th>' + t('Geschwindigkeit','Speed') + '</th></tr></thead><tbody>';
    let seperator = t(",",".");
    
    for (const item of g.last_speeds) {
      if(seperator == ",")
      {modalHtml += `<tr>
        <td class="speed-modal-title" title="${item.title}">${item.title}</td>
        <td>${item.size.toFixed(2).replace(".", ",")} MB</td>
        <td style="color:var(--accent);font-weight:600">${item.speed.toFixed(3).replace(".", ",")} MB/s</td>
      </tr>`;}
      else{
        modalHtml += `<tr>
        <td class="speed-modal-title" title="${item.title}">${item.title}</td>
        <td>${item.size.toFixed(2).replace(",", ".")} MB</td>
        <td style="color:var(--accent);font-weight:600">${item.speed.toFixed(3).replace(",", ".")} MB/s</td>
      </tr>`;
      }
    }
    modalHtml += '</tbody></table></div>';
    document.getElementById("speedModalContent").innerHTML = modalHtml;
  }

  // --- KPI row ---
  let html = '<div class="stats-kpi-row stats-kpi-main">';
  html += statCard(
    t("Abgeschlossene Downloads", "Completed Downloads"),
    g.completed ?? "—",
    t("Erfolgreich heruntergeladene Queue-Einträge", "Successfully downloaded queue entries"),
    "#22c55e"
  );
  html += statCard(
    t("Heruntergeladene Episoden", "Downloaded Episodes"),
    g.total_episodes ?? "—",
    t("Einzelne Episoden über alle Downloads", "Individual episodes across all downloads"),
    "#6ea8fe"
  );
  html += statCard(
    t("Heruntergeladene Filme", "Downloaded Movies"),
    g.movie_files ?? "—",
    `${g.movie_downloads ?? 0} FilmPalast-Downloads`,
    "#e8914a"
  );
  html += statCard(
    t("Fehlgeschlagen", "Failed"),
    g.failed ?? "—",
    t("Downloads mit Fehlern oder ohne Stream", "Downloads with errors or without stream"),
    "#f87171"
  );
  html += statCard(
    t("Letzte 24 Stunden", "Last 24 hours"),
    g.last_24h_completed ?? "—",
    t("Downloads abgeschlossen in den letzten 24h", "Downloads completed in the last 24h"),
    "#a78bfa"
  );
  html += statCard(
    t("Ø Download-Dauer", "Avg. Download Duration"),
    fmtDuration(g.average_duration_seconds),
    t("Durchschnittliche Zeit pro Queue-Eintrag", "Average time per queue entry"),
    "#fb923c"
  );
  let seperator = t(",",".");
  if(seperator == ","){
    html += statCard(
    t("Ø Geschwindigkeit", "Avg. Speed"),
    g.average_speed_mbps ? `${String(g.average_speed_mbps).replace(".", ",")} MB/s` : "—",
    (g.total_size_mb ? `${t('Gesamt', 'Total')}: ${String(Number(g.total_size_mb).toFixed(2)).replace(".", ",")} MB ${t('geladen', 'loaded')}` : t("Keine Daten", "No data") + "<br>" + t("Klicken für Details", "Click for details")),
    "#06b6d4",
    "openSpeedModal()"
  );
  } else {
    html += statCard(
    t("Ø Geschwindigkeit", "Avg. Speed"),
    g.average_speed_mbps ? `${String(g.average_speed_mbps).replace(",", ".")} MB/s` : "—",
    (g.total_size_mb ? `${t('Gesamt', 'Total')}: ${String(Number(g.total_size_mb).toFixed(2)).replace(",", ".")} MB ${t('geladen', 'loaded')}` : t("Keine Daten", "No data") + "<br>" + t("Klicken für Details", "Click for details")),
    "#06b6d4",
    "openSpeedModal()"
  );
  }
  
  html += statCard(
    t("Auto-Sync Jobs", "Auto-Sync Jobs"),
    `${s.enabled ?? 0} ${t('aktiv', 'active')}`,
    `${s.total_jobs ?? 0} ${t('Jobs gesamt konfiguriert', 'jobs configured total')}`,
    "#34d399"
  );
  html += '</div>';

  // --- Media category (toggleable in settings) ---
  if (m) {
    html += renderMediaSection(m);
  }

  // Weekday Activity removed as requested.

  // --- Queue status breakdown ---
  html += '<div class="stats-section"><h2 class="stats-section-title">' + t('Queue-Status', 'Queue Status') + '</h2><div class="stats-kpi-row">';
  const statusLabels = { completed: t("Abgeschlossen","Completed"), failed: t("Fehlgeschlagen","Failed"), cancelled: t("Abgebrochen","Cancelled"), running: t("Läuft","Running"), queued: t("Wartend","Queued") };
  for (const [st, label] of Object.entries(statusLabels)) {
    html += statCard(label, byStatus[st] ?? 0);
  }
  html += '</div></div>';

  // --- Source breakdown ---
  html += '<div class="stats-section"><h2 class="stats-section-title">' + t('Quelle', 'Source') + '</h2><div class="stats-kpi-row">';
  html += statCard("Anime (AniWorld)", g.anime_downloads ?? 0, `${g.anime_episodes ?? 0} ${t('Episoden heruntergeladen', 'episodes downloaded')}`, "#6ea8fe");
  html += statCard("Serien (SerienStream)", g.series_downloads ?? 0, `${g.series_episodes ?? 0} ${t('Episoden heruntergeladen', 'episodes downloaded')}`, "#a78bfa");
  html += statCard(t("Filme (FilmPalast)", "Movies (FilmPalast)"), g.movie_downloads ?? 0, `${g.movie_files ?? 0} ${t('Filme heruntergeladen', 'movies downloaded')}`, "#e8914a");
  html += '</div></div>';

  // --- Language breakdown table ---
  if (g.by_language && g.by_language.length) {
    html += '<div class="stats-section"><h2 class="stats-section-title">' + t('Nach Sprache', 'By Language') + '</h2>';
    html += '<div class="user-table-wrapper"><table class="user-table"><thead><tr><th>' + t('Sprache','Language') + '</th><th>Downloads</th><th>' + t('Episoden','Episodes') + '</th></tr></thead><tbody>';
    for (const row of g.by_language) {
      html += `<tr><td>${row.language || "—"}</td><td>${row.downloads}</td><td>${row.episodes}</td></tr>`;
    }
    html += '</tbody></table></div></div>';
  }

  container.innerHTML = html;

  // While the library is still scanning, silently reload (no skeleton flash)
  // so the Media counts fill in once it finishes. Cap the retries so a stuck
  // scan can't keep the page polling forever.
  if (m && m.scanning) {
    window._mediaRescanTries = (window._mediaRescanTries || 0) + 1;
    if (window._mediaRescanTries <= 15) {
      clearTimeout(window._mediaRescanTimer);
      window._mediaRescanTimer = setTimeout(function () { loadStats(false); }, 4000);
    }
  } else {
    window._mediaRescanTries = 0;
  }
}

loadStats();
