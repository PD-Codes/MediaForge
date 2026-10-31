(function () {
  const overlay = document.getElementById("gapBatchOverlay");
  if (!overlay) return;
  const api = window.MFGapDownloads;
  const dialog = overlay.querySelector('[role="dialog"]');
  const language = document.getElementById("gapBatchLanguage");
  const provider = document.getElementById("gapBatchProvider");
  const status = document.getElementById("gapBatchStatus");
  const list = document.getElementById("gapBatchEntries");
  const submit = document.getElementById("gapBatchSubmit");
  const close = document.getElementById("gapBatchClose");
  let current = null, previousFocus, previousOverflow;
  function text(de, en) { return window.t(de, en); }
  function options(select, values, preferred) {
    select.replaceChildren();
    values.forEach(function (value) { select.add(new Option(value, value)); });
    if (values.includes(preferred)) select.value = preferred;
    select.disabled = !values.length;
  }
  function selected(ctx) { return ctx.entries.filter(function (entry) { return entry.included; }); }
  function render(ctx) {
    list.replaceChildren();
    ctx.entries.forEach(function (entry) {
      const li = document.createElement("li"), label = document.createElement("label");
      const box = document.createElement("input"), note = document.createElement("small");
      box.type = "checkbox"; box.className = "chb-main";
      box.checked = entry.included; box.disabled = !!ctx.submitting || !!entry.done || !!ctx.finished;
      box.addEventListener("change", function () { entry.included = box.checked; refresh(ctx); });
      label.append(box, document.createTextNode(entry.item.title));
      note.textContent = entry.note || entry.error || text("Wird geprüft …", "Checking …");
      li.append(label, note); list.appendChild(li);
    });
  }
  window.openGapFillBatch = async function (items, onQueued) {
    if (!items.length || (current && current.submitting)) return;
    if (current) current.cancel.abort();
    else { previousFocus = document.activeElement; previousOverflow = document.body.style.overflow; }
    const ctx = { cancel: new AbortController(), entries: items.map(function (item) {
      return { item: item, included: true };
    }), onQueued: onQueued, seq: 0, loading: true };
    current = ctx;
    document.getElementById("gapBatchTitle").textContent = text("Ausgewählte Lücken herunterladen", "Download selected gaps");
    submit.textContent = text("Auswahl zur Warteschlange", "Queue selected");
    submit.disabled = true;
    options(language, []); options(provider, []);
    overlay.style.display = "block"; document.body.style.overflow = "hidden"; dialog.focus();
    status.textContent = text("Prüfe Serien und fehlende Episoden …", "Checking series and missing episodes …");
    render(ctx);
    try {
      const catalogue = await api.sources(ctx);
      for (let i = 0; i < ctx.entries.length; i += 3) {
        await Promise.all(ctx.entries.slice(i, i + 3).map(async function (entry) {
          try {
            Object.assign(entry, await api.prepare(entry.item, ctx, catalogue));
            if (entry.unavailable.length) throw new Error(text("Ausgewählte Folgen nicht verfügbar: ",
              "Selected episodes unavailable: ") + entry.unavailable.join(", "));
            entry.note = entry.episodes.length + " " + text("fehlende Folgen gefunden", "missing episodes found");
            if (!entry.episodes.some(function (ep) { return (ep.languages || []).length; })) {
              entry.initialProviders = await api.providers(entry.episodes[0], ctx);
            }
          } catch (error) { entry.error = error.message; }
        }));
        if (current !== ctx) return;
        render(ctx);
      }
      const langs = [...new Set(ctx.entries.flatMap(function (entry) {
        return (entry.episodes || []).flatMap(function (ep) { return ep.languages || []; })
          .concat(Object.keys(entry.initialProviders || {}));
      }))];
      options(language, langs, "German Dub");
      ctx.loading = false;
      await refresh(ctx);
    } catch (error) {
      if (current === ctx && !ctx.cancel.signal.aborted) status.textContent = error.message;
    }
  };
  async function refresh(ctx) {
    if (current !== ctx || ctx.loading || ctx.submitting || ctx.finished) return;
    const seq = ++ctx.seq, chosenLanguage = language.value;
    const entries = selected(ctx);
    submit.disabled = true;
    options(provider, []);
    status.textContent = text("Prüfe gemeinsame Downloadquelle …", "Checking for a shared download provider …");
    let shared = null, blocked = 0;
    for (let i = 0; i < entries.length; i += 3) {
      await Promise.all(entries.slice(i, i + 3).map(async function (entry) {
        entry.available = api.available(entry.episodes || [], chosenLanguage);
        entry.hosters = [];
        if (entry.error) { entry.note = entry.error; return; }
        if (!entry.available.length || entry.available.length !== entry.episodes.length) {
          entry.note = entry.available.length + " / " + entry.episodes.length + " " +
            text("Folgen in dieser Sprache verfügbar. Wähle eine andere Sprache oder nimm diese Serie aus der Auswahl.",
              "episodes available in this language. Choose another language or deselect this series.");
          return;
        }
        try {
          const map = await api.providers(entry.available[0], ctx);
          if (current !== ctx || ctx.seq !== seq) return;
          entry.hosters = map[chosenLanguage] || [];
          entry.note = entry.available.length + " " + text("Folgen bereit", "episodes ready");
        } catch (error) { if (current === ctx && ctx.seq === seq) entry.note = error.message; }
      }));
      if (current !== ctx || ctx.seq !== seq) return;
    }
    entries.forEach(function (entry) {
      if (!entry.hosters.length) blocked++;
      shared = shared === null ? entry.hosters.slice() : shared.filter(function (h) { return entry.hosters.includes(h); });
    });
    options(provider, shared || [], "VOE");
    submit.disabled = !entries.length || blocked > 0 || !provider.value;
    status.textContent = !entries.length ? text("Nichts ausgewählt.", "Nothing selected.")
      : blocked ? text("Nicht alle ausgewählten Serien sind mit dieser Sprache verfügbar. Prüfe die Hinweise in der Liste.",
        "Not all selected series are available in this language. Check the notes below.")
      : !provider.value ? text("Kein gemeinsamer Anbieter verfügbar. Ändere die Auswahl oder lade die Serien einzeln.",
        "No shared provider available. Change the selection or download the series individually.")
      : entries.length + " " + text("Serien bereit. Die ausgewählten Folgen werden gemeinsam eingereiht.",
        "series ready. The selected episodes will be queued together.");
    render(ctx);
  }
  language.addEventListener("change", function () { if (current) refresh(current); });
  submit.addEventListener("click", async function () {
    const ctx = current;
    if (!ctx || ctx.submitting || submit.disabled) return;
    const entries = selected(ctx), chosenLanguage = language.value, hoster = provider.value;
    ctx.submitting = true;
    submit.disabled = language.disabled = provider.disabled = close.disabled = true;
    render(ctx);
    let success = 0;
    for (const entry of entries) {
      status.textContent = text("Füge zur Warteschlange hinzu: ", "Adding to queue: ") + entry.item.title;
      try {
        await api.enqueue(entry, entry.available, chosenLanguage, hoster, ctx);
        entry.done = true; entry.included = false; success++;
        entry.note = entry.available.length + " " + text("Folgen zur Warteschlange hinzugefügt", "episodes added to queue");
        if (ctx.onQueued) ctx.onQueued(entry.item);
      } catch (error) { entry.note = text("Fehlgeschlagen: ", "Failed: ") + error.message; }
      render(ctx);
    }
    ctx.submitting = false; ctx.finished = true; close.disabled = false;
    status.textContent = success + " / " + entries.length + " " + text("Serien zur Warteschlange hinzugefügt.", "series added to queue.");
    submit.textContent = text("Abgeschlossen", "Finished");
    render(ctx);
  });
  function dismiss() {
    if (!current || current.submitting) return;
    current.cancel.abort(); current = null; overlay.style.display = "none";
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
