(function () {
  const overlay = document.getElementById("gapFillOverlay");
  if (!overlay) return;
  const api = window.MFGapDownloads;
  const dialog = overlay.querySelector('[role="dialog"]');
  const title = document.getElementById("gapFillTitle");
  const slots = document.getElementById("gapFillSlots");
  const status = document.getElementById("gapFillStatus");
  const source = document.getElementById("gapFillSource");
  const language = document.getElementById("gapFillLanguage");
  const provider = document.getElementById("gapFillProvider");
  const list = document.getElementById("gapFillEpisodes");
  const selectAll = document.getElementById("gapFillSelectAll");
  const submit = document.getElementById("gapFillSubmit");
  const submitAll = document.getElementById("gapFillAllSubmit");
  const close = document.getElementById("gapFillClose");
  let current = null;
  let previousFocus;
  let previousOverflow;
  function text(de, en) { return window.t(de, en); }
  function message(value) { status.textContent = value; }
  function options(select, values, preferred) {
    select.replaceChildren();
    values.forEach(function (value) { select.add(new Option(value, value)); });
    if (values.includes(preferred)) select.value = preferred;
    select.disabled = !values.length;
  }
  function reset() {
    options(language, []);
    options(provider, []);
    list.replaceChildren();
    selectAll.checked = selectAll.indeterminate = false;
    selectAll.disabled = submit.disabled = submitAll.disabled = true;
    submit.textContent = text("Auswahl zur Warteschlange", "Queue selected");
    submitAll.textContent = text("Alle fehlenden zur Warteschlange", "Queue all missing");
    document.getElementById("gapFillSelectAllLabel").textContent = text("Alle fehlenden auswählen", "Select all missing");
  }
  function report(error, ctx) {
    if (current === ctx && !ctx.cancel.signal.aborted) {
      message(text("Anfrage fehlgeschlagen: ", "Request failed: ") + error.message);
    }
  }
  function available(ctx) { return api.available(ctx.episodes, language.value); }
  function selected(ctx) { return available(ctx).filter(function (ep) { return ctx.selected.has(ep.url); }); }
  function updateSelection(ctx) {
    const episodes = available(ctx), count = selected(ctx).length;
    selectAll.checked = episodes.length > 0 && count === episodes.length;
    selectAll.indeterminate = count > 0 && count < episodes.length;
    selectAll.disabled = ctx.submitting || !episodes.length;
    submit.disabled = ctx.submitting || !ctx.ready || !count;
    submitAll.disabled = ctx.submitting || !ctx.ready || !episodes.length;
    if (ctx.ready && !ctx.submitting) message(count + " / " + episodes.length + " " +
      text("verfügbare fehlende Episoden ausgewählt.", "available missing episodes selected."));
  }
  function renderEpisodes(ctx) {
    list.replaceChildren();
    available(ctx).forEach(function (ep) {
      const li = document.createElement("li"), label = document.createElement("label");
      const box = document.createElement("input");
      box.type = "checkbox";
      box.className = "chb-main";
      box.checked = ctx.selected.has(ep.url);
      box.disabled = !!ctx.submitting;
      box.addEventListener("change", function () {
        if (box.checked) ctx.selected.add(ep.url); else ctx.selected.delete(ep.url);
        updateSelection(ctx);
      });
      label.append(box, document.createTextNode(ep.slot +
        (ep.title_de || ep.title_en ? " — " + (ep.title_de || ep.title_en) : "")));
      li.appendChild(label);
      list.appendChild(li);
    });
    updateSelection(ctx);
  }
  window.openGapFill = async function (item) {
    if (current && current.submitting) return;
    if (current) current.cancel.abort();
    else { previousFocus = document.activeElement; previousOverflow = document.body.style.overflow; }
    const ctx = { item: item, cancel: new AbortController(), episodes: [], selected: new Set(), queued: new Set() };
    current = ctx;
    reset();
    options(source, []);
    title.textContent = text("Lücken füllen: ", "Fill gaps: ") + item.title;
    slots.textContent = (item.missing_slots || item.missing || []).join(", ");
    message(text("Suche und ordne die Serie zu …", "Searching and matching the series …"));
    overlay.style.display = "block";
    document.body.style.overflow = "hidden";
    dialog.focus();
    try {
      const catalogue = await api.sources(ctx);
      const result = await api.find(item, ctx, catalogue);
      if (current !== ctx) return;
      ctx.hits = result.hits;
      source.add(new Option(text("Serie auswählen", "Choose series"), ""));
      ctx.hits.forEach(function (hit, i) { source.add(new Option(hit.sourceLabel + " — " + hit.title, String(i))); });
      source.disabled = !ctx.hits.length;
      message(ctx.hits.length
        ? text("Wähle die passende Serie und Quelle.", "Choose the matching series and source.")
        : result.failed ? text("Die Suche ist teilweise fehlgeschlagen.", "Some searches failed.")
          : text("Keine Serie in den aktivierten Quellen gefunden.", "No series found in the enabled sources."));
      if (result.match !== -1) { source.value = String(result.match); await loadEpisodes(ctx); }
    } catch (error) { report(error, ctx); }
  };
  async function loadEpisodes(ctx) {
    if (ctx.lookup) ctx.lookup.cancel.abort();
    const lookup = { cancel: new AbortController() };
    ctx.lookup = lookup;
    ctx.cancel.signal.addEventListener("abort", function () { lookup.cancel.abort(); }, { once: true });
    reset();
    ctx.episodes = [];
    ctx.ready = false;
    ctx.hit = source.value === "" ? null : ctx.hits[Number(source.value)];
    if (!ctx.hit) { message(text("Wähle eine Serie.", "Choose a series.")); return; }
    message(text("Prüfe fehlende Episoden …", "Checking missing episodes …"));
    try {
      const episodes = await api.episodes(ctx.item, ctx.hit, lookup);
      if (current !== ctx || ctx.lookup !== lookup) return;
      ctx.episodes = episodes.filter(function (ep) { return !ctx.queued.has(ep.slot); });
      ctx.selected = new Set(ctx.episodes.map(function (ep) { return ep.url; }));
      if (!ctx.episodes.length) { message(text("Keine nachladbaren Lücken gefunden.", "No downloadable gaps found.")); return; }
      const langs = [...new Set(ctx.episodes.flatMap(function (ep) { return ep.languages || []; }))];
      if (!langs.length) langs.push(...Object.keys(await api.providers(ctx.episodes[0], lookup)));
      if (current !== ctx || ctx.lookup !== lookup) return;
      const existing = document.getElementById("languageSelect");
      options(language, langs, (existing && existing.value) || "German Dub");
      await loadProviders(ctx);
    } catch (error) { if (ctx.lookup === lookup && !lookup.cancel.signal.aborted) report(error, ctx); }
  }
  async function loadProviders(ctx) {
    const lookup = ctx.lookup, chosenLanguage = language.value;
    const seq = ctx.providerSeq = (ctx.providerSeq || 0) + 1;
    ctx.ready = false;
    submit.textContent = text("Auswahl zur Warteschlange", "Queue selected");
    options(provider, []);
    renderEpisodes(ctx);
    const episodes = available(ctx);
    if (!chosenLanguage || !episodes.length) { message(text("Keine Episoden in dieser Sprache verfügbar.", "No episodes available in this language.")); return; }
    message(text("Prüfe Downloadquellen …", "Checking download providers …"));
    try {
      const providers = await api.providers(episodes[0], lookup);
      if (current !== ctx || ctx.lookup !== lookup || ctx.providerSeq !== seq) return;
      const existing = document.getElementById("providerSelect");
      options(provider, providers[chosenLanguage] || [], (existing && existing.value) || "VOE");
      ctx.ready = !!provider.value;
      updateSelection(ctx);
      if (!ctx.ready) message(text("Keine Downloadquelle für diese Sprache verfügbar.", "No download provider available for this language."));
    } catch (error) { if (ctx.lookup === lookup && ctx.providerSeq === seq) report(error, ctx); }
  }
  source.addEventListener("change", function () { if (current) loadEpisodes(current); });
  language.addEventListener("change", function () { if (current) loadProviders(current); });
  selectAll.addEventListener("change", function () {
    if (!current || current.submitting) return;
    available(current).forEach(function (ep) {
      if (selectAll.checked) current.selected.add(ep.url); else current.selected.delete(ep.url);
    });
    renderEpisodes(current);
  });
  async function enqueue(all) {
    const ctx = current;
    if (!ctx || ctx.submitting || (all ? submitAll.disabled : submit.disabled)) return;
    const episodes = all ? available(ctx) : selected(ctx);
    ctx.submitting = true;
    source.disabled = language.disabled = provider.disabled = close.disabled = true;
    renderEpisodes(ctx);
    message(text("Füge Episoden zur Warteschlange hinzu …", "Adding episodes to the queue …"));
    try {
      await api.enqueue(ctx, episodes, language.value, provider.value, ctx);
      episodes.forEach(function (ep) { ctx.queued.add(ep.slot); ctx.selected.delete(ep.url); });
      ctx.episodes = ctx.episodes.filter(function (ep) { return !ctx.queued.has(ep.slot); });
      ctx.submitting = false;
      if (ctx.episodes.length) {
        source.disabled = language.disabled = provider.disabled = false;
        await loadProviders(ctx);
      } else {
        ctx.ready = false;
        renderEpisodes(ctx);
        submit.textContent = submitAll.textContent = text("Hinzugefügt", "Added");
      }
      if (!ctx.ready) submit.textContent = text("Hinzugefügt", "Added");
      message(text("Zur Download-Warteschlange hinzugefügt.", "Added to download queue."));
    } catch (error) { ctx.ready = false; report(error, ctx); }
    finally { ctx.submitting = false; close.disabled = false; }
  }
  submit.addEventListener("click", function () { enqueue(false); });
  submitAll.addEventListener("click", function () { enqueue(true); });
  function dismiss() {
    if (!current || current.submitting) return;
    current.cancel.abort(); current = null;
    overlay.style.display = "none";
    document.body.style.overflow = previousOverflow;
    if (previousFocus && previousFocus.isConnected) previousFocus.focus();
  }
  close.addEventListener("click", dismiss);
  overlay.addEventListener("click", function (event) { if (event.target === overlay) dismiss(); });
  overlay.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); dismiss(); }
    if (event.key !== "Tab") return;
    const controls = [...dialog.querySelectorAll("button, select, input")].filter(function (el) { return !el.disabled; });
    const first = controls[0], last = controls[controls.length - 1];
    if (!first) { event.preventDefault(); return; }
    if (event.shiftKey && (document.activeElement === first || document.activeElement === dialog)) {
      event.preventDefault(); last.focus();
    } else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialog)) {
      event.preventDefault(); first.focus();
    }
  });
})();
