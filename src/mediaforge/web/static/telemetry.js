// Privacy & Telemetry settings tab (see templates/settings.html's
// #tab-privacy). Builds the stage-tree UI from the server-side registry
// (mediaforge/telemetry/registry.py's DATA_REGISTRY, fetched via
// GET /api/settings/telemetry) so the labels/explain texts are never
// hand-duplicated here, and implements the save flow required by
// TELEMETRY_IMPLEMENTATION_PLAN.md §4.2: local diff -> Save button ->
// confirmation dialog (grouped "will be enabled" / "will be disabled",
// each with its own explain text) -> explicit confirm -> PUT request.
//
// Reuses the project's existing generic confirm modal (showConfirm(), see
// base.html) for that confirmation dialog rather than inventing a second
// modal component from scratch -- showConfirm() already accepts an HTML
// message, a custom OK label/class and has no accent-biased button pair
// issue (unlike the first-run consent dialog, which -- per
// TELEMETRY_PLAN.md §4.0 -- genuinely needs its own bespoke, no-dismiss,
// equal-weight-button overlay; see base.html for that one).

var _telemetryRegistry = null;      // {stages: {...}, data_points: {...}}
var _telemetrySavedKeys = new Set();   // last known-good state from the server
var _telemetryPendingKeys = new Set(); // local, unsaved form state

async function loadTelemetrySettings() {
  try {
    const resp = await fetch("/api/settings/telemetry");
    if (!resp.ok) return; // non-admin or auth disabled differences -- tab simply stays empty
    const data = await resp.json();

    _telemetryRegistry = data.registry;
    _telemetrySavedKeys = new Set(data.enabled_keys || []);
    _telemetryPendingKeys = new Set(_telemetrySavedKeys);

    const idEl = document.getElementById("telemetryInstallId");
    if (idEl) idEl.value = data.install_id || "";

    const masterEl = document.getElementById("telemetryMasterConsent");
    if (masterEl) masterEl.checked = data.consent_given === true;

    const descEl = document.getElementById("telemetryConsentDesc");
    if (descEl) {
      if (data.consent_given === true) {
        descEl.textContent = t(
          "Aktiv seit " + (data.consent_at || "?") + ". Ausschalten löscht auch alle unten aktivierten Datenpunkte.",
          "Active since " + (data.consent_at || "?") + ". Turning this off also clears every data point enabled below."
        );
      } else if (data.consent_given === false) {
        descEl.textContent = t(
          "Aus -- am " + (data.consent_at || "?") + " abgelehnt.",
          "Off -- declined on " + (data.consent_at || "?") + "."
        );
      } else {
        descEl.textContent = t("Noch keine Entscheidung getroffen.", "No decision made yet.");
      }
    }

    _renderTelemetryStageTree(data.consent_given === true);
    telemetryLoadRequestStatus();
  } catch (e) {
    console.error("[Telemetry] loadTelemetrySettings failed", e);
  }
}

function _renderTelemetryStageTree(consentGiven) {
  const container = document.getElementById("telemetryStageTree");
  if (!container || !_telemetryRegistry) return;

  const stages = _telemetryRegistry.stages;
  const points = _telemetryRegistry.data_points;

  // Group data_points by stage (skip always_on entries like install_id --
  // those have no toggle of their own, see registry.py).
  const byStage = {};
  Object.keys(points).forEach(function (key) {
    const p = points[key];
    if (p.always_on) return;
    (byStage[p.stage] = byStage[p.stage] || []).push(Object.assign({ key: key }, p));
  });

  let html = "";
  [1, 2, 3, 4, 5, 6].forEach(function (stage) {
    const meta = stages[stage] || { title: "Stage " + stage, description: "" };
    const keys = (byStage[stage] || []).sort(function (a, b) { return a.label.localeCompare(b.label); });
    const cardId = "telstage-" + stage;
    const isSensitive = stage === 6;

    html += '<div class="settings-section integ-card collapsed telemetry-stage-card' +
      (isSensitive ? ' telemetry-stage-sensitive' : '') + '" id="integCard-' + cardId + '">';
    html += '  <div class="integ-subsection-header integ-collapsible-header" style="padding:unset !important;" onclick="toggleIntegCollapse(\'' + cardId + '\')">';
    html += '    <span class="integ-collapsible-chevron">▸</span>';
    html += '    <span class="integ-subsection-title">' + t("Stufe", "Stage") + ' ' + stage + ' — ' + esc(meta.title) + '</span>';
    if (isSensitive) {
      html += '    <span class="integ-subsection-badge telemetry-badge-sensitive">' + t("Sensibel", "Sensitive") + '</span>';
    }
    html += '  </div>';
    html += '  <div class="integ-collapsible-body">';
    html += '    <div class="integ-subsection-hint">' + esc(meta.description) + '</div>';
    html += '    <div class="settings-row">';
    html += '      <div class="settings-row-left"><div class="settings-row-label">' + t("Stufe " + stage + " aktivieren", "Enable stage " + stage) + '</div></div>';
    html += '      <div class="settings-row-right"><label class="toggle"><input type="checkbox" class="telemetry-stage-toggle" data-stage="' + stage + '" onchange="telemetryStageToggle(' + stage + ', this)" /><span class="toggle-slider"></span></label></div>';
    html += '    </div>';
    keys.forEach(function (p) {
      html += '    <div class="settings-row" data-key-row="' + esc(p.key) + '">';
      html += '      <div class="settings-row-left"><div class="settings-row-label">' + esc(p.label) + '</div><div class="settings-row-desc">' + esc(p.explain) + '</div></div>';
      html += '      <div class="settings-row-right"><label class="toggle"><input type="checkbox" class="telemetry-key-toggle" data-key="' + esc(p.key) + '" data-stage="' + stage + '" onchange="telemetryKeyToggle(this)" /><span class="toggle-slider"></span></label></div>';
      html += '    </div>';
    });
    html += '  </div></div>';
  });

  container.innerHTML = html;

  // Apply current pending state to the freshly-rendered checkboxes.
  container.querySelectorAll(".telemetry-key-toggle").forEach(function (cb) {
    cb.checked = _telemetryPendingKeys.has(cb.dataset.key);
  });
  [1, 2, 3, 4, 5, 6].forEach(_recomputeStageCheckbox);

  // Disable all interaction until consent has actually been granted --
  // matches api_settings_telemetry_put()'s server-side refusal, and avoids
  // a confusing "I toggled things but Save always fails" experience.
  if (!consentGiven) {
    container.querySelectorAll("input[type=checkbox]").forEach(function (cb) { cb.disabled = true; });
  }
}

function _stageKeys(stage) {
  const points = (_telemetryRegistry && _telemetryRegistry.data_points) || {};
  return Object.keys(points).filter(function (k) {
    return points[k].stage === stage && !points[k].always_on;
  });
}

function _recomputeStageCheckbox(stage) {
  const stageCb = document.querySelector('.telemetry-stage-toggle[data-stage="' + stage + '"]');
  if (!stageCb) return;
  const keys = _stageKeys(stage);
  const enabledCount = keys.filter(function (k) { return _telemetryPendingKeys.has(k); }).length;
  stageCb.checked = keys.length > 0 && enabledCount === keys.length;
  stageCb.indeterminate = enabledCount > 0 && enabledCount < keys.length;
}

function telemetryKeyToggle(el) {
  const key = el.dataset.key;
  if (el.checked) _telemetryPendingKeys.add(key);
  else _telemetryPendingKeys.delete(key);
  _recomputeStageCheckbox(parseInt(el.dataset.stage, 10));
}

function telemetryStageToggle(stage, el) {
  // Bulk action only -- each individual data_key still remembers its own
  // state afterwards (TELEMETRY_PLAN.md §2: "reine Bulk-Aktion").
  el.indeterminate = false;
  _stageKeys(stage).forEach(function (key) {
    if (el.checked) _telemetryPendingKeys.add(key);
    else _telemetryPendingKeys.delete(key);
  });
  document.querySelectorAll('.telemetry-key-toggle[data-stage="' + stage + '"]').forEach(function (cb) {
    cb.checked = el.checked;
  });
}

function _telemetryLabel(key) {
  const p = _telemetryRegistry.data_points[key];
  return p ? p.label : key;
}

function _telemetryBuildConfirmMessage(added, removed) {
  const points = _telemetryRegistry.data_points;
  let html = "";
  const stage6Touched = added.concat(removed).some(function (k) { return points[k] && points[k].stage === 6; });
  if (stage6Touched) {
    html += '<div class="telemetry-confirm-warning">' +
      t(
        "⚠️ Achtung: Diese Änderung betrifft Stufe 6 (Sehverhalten/Watchtime) — zusammen mit Titel-Daten ein echtes Nutzungsprofil, deutlich näher an Streaming-Analytics als an Crash-Reporting.",
        "⚠️ Note: this change touches Stage 6 (watch behaviour) — combined with title data, a real usage profile, much closer to streaming analytics than crash reporting."
      ) + '</div>';
  }
  if (added.length) {
    html += '<div class="telemetry-confirm-group"><strong>' + t("Wird aktiviert:", "Will be enabled:") + '</strong><ul>';
    added.forEach(function (k) {
      html += '<li><strong>' + esc(_telemetryLabel(k)) + '</strong><br><span>' + esc(points[k].explain) + '</span></li>';
    });
    html += '</ul></div>';
  }
  if (removed.length) {
    html += '<div class="telemetry-confirm-group"><strong>' + t("Wird deaktiviert:", "Will be disabled:") + '</strong><ul>';
    removed.forEach(function (k) {
      html += '<li><strong>' + esc(_telemetryLabel(k)) + '</strong><br><span>' + esc(points[k].explain) + '</span></li>';
    });
    html += '</ul></div>';
  }
  return html;
}

async function telemetrySave() {
  if (!_telemetryRegistry) return;
  const added = [...(_telemetryPendingKeys)].filter(function (k) { return !_telemetrySavedKeys.has(k); });
  const removed = [...(_telemetrySavedKeys)].filter(function (k) { return !_telemetryPendingKeys.has(k) && k !== "install_id"; });
  if (!added.length && !removed.length) {
    showToast(t("Keine Änderungen", "No changes"), "info");
    return;
  }
  const message = _telemetryBuildConfirmMessage(added, removed);
  const ok = await showConfirm(message, t("Bestätigen", "Confirm"),
    t("Änderungen an Privatsphäre & Telemetrie", "Privacy & Telemetry changes"), "btn-primary");
  if (!ok) return;

  try {
    const resp = await fetch("/api/settings/telemetry", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled_keys: [..._telemetryPendingKeys] })
    });
    const data = await resp.json();
    if (data.ok) {
      _telemetrySavedKeys = new Set(data.enabled_keys || []);
      _telemetryPendingKeys = new Set(_telemetrySavedKeys);
      showToast(t("Gespeichert", "Saved"), "success");
    } else {
      showToast(t("Fehler: " + (data.error || "unbekannt"), "Error: " + (data.error || "unknown")), "error");
    }
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message), "error");
  }
}

async function telemetryMasterConsentToggle(el) {
  const granting = el.checked;
  const message = granting
    ? t(
        "MediaForge sendet dann Absturzberichte und Basis-Systeminfo (App-Version, Betriebssystem, Python-Version) an den Entwickler, bis du das hier wieder abschaltest. Keine Titel, keine Zugangsdaten.",
        "MediaForge will then send crash reports and basic system info (app version, OS, Python version) to the developer, until you turn this off again here. No titles, no credentials."
      )
    : t(
        "Schaltet Telemetrie komplett aus und löscht alle unten aktivierten Datenpunkte (lokal). Bereits gesendete Daten bleiben davon unberührt -- nutze „Meine Daten verwalten“ oben, um sie löschen/exportieren zu lassen.",
        "Turns telemetry off entirely and clears every data point enabled below (locally). Data already sent is unaffected -- use \"Manage my data\" above to request its deletion/export."
      );
  const ok = await showConfirm(message, granting ? t("Aktivieren", "Enable") : t("Deaktivieren", "Disable"),
    t("Telemetrie", "Telemetry"), granting ? "btn-primary" : "btn-danger");
  if (!ok) {
    el.checked = !granting;
    return;
  }
  try {
    const resp = await fetch("/api/settings/telemetry/consent", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ granted: granting })
    });
    const data = await resp.json();
    if (data.ok) {
      showToast(t("Gespeichert", "Saved"), "success");
      loadTelemetrySettings();
    }
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message), "error");
  }
}

async function telemetryRegenerateInstallId() {
  const ok = await showConfirm(
    t("Neue Installations-ID erzeugen? Die alte ID wird nicht mehr mit dieser Installation verknüpft (kein Umzug alter Daten auf die neue ID).",
      "Generate a new installation ID? The old ID will no longer be linked to this installation (no migration of old data to the new ID)."),
    t("Zurücksetzen", "Reset"));
  if (!ok) return;
  try {
    const resp = await fetch("/api/settings/telemetry/regenerate-id", { method: "POST" });
    const data = await resp.json();
    if (data.ok) {
      const idEl = document.getElementById("telemetryInstallId");
      if (idEl) idEl.value = data.install_id;
      showToast(t("Neue ID erzeugt", "New ID generated"), "success");
      telemetryLoadRequestStatus();
    }
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message), "error");
  }
}

async function telemetrySubmitRequest(requestType) {
  const username = (document.getElementById("telemetryReqUsername") || {}).value || "";
  const email = (document.getElementById("telemetryReqEmail") || {}).value || "";
  if (!username.trim() || !email.trim()) {
    showToast(t("Bitte Name und E-Mail angeben", "Please provide a name and email"), "error");
    return;
  }
  const label = requestType === "delete"
    ? t("Wirklich die Löschung aller unter dieser Installation gespeicherten Daten beantragen?",
        "Really request deletion of all data stored under this installation?")
    : t("Export aller unter dieser Installation gespeicherten Daten beantragen?",
        "Request an export of all data stored under this installation?");
  const ok = await showConfirm(label, t("Beantragen", "Request"), t("Datenanfrage", "Data request"),
    requestType === "delete" ? "btn-danger" : "btn-primary");
  if (!ok) return;
  try {
    const resp = await fetch("/api/settings/telemetry/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_type: requestType, username: username.trim(), email: email.trim() })
    });
    const data = await resp.json();
    if (data.ok) {
      showToast(t("Anfrage gesendet", "Request submitted"), "success");
      telemetryLoadRequestStatus();
    } else {
      showToast(t("Fehler: " + (data.error || "unbekannt"), "Error: " + (data.error || "unknown")), "error");
    }
  } catch (e) {
    showToast(t("Fehler: " + e.message, "Error: " + e.message), "error");
  }
}

async function telemetryLoadRequestStatus() {
  const el = document.getElementById("telemetryRequestStatus");
  if (!el) return;
  try {
    const resp = await fetch("/api/settings/telemetry/request-status");
    if (!resp.ok) { el.textContent = ""; return; }
    const data = await resp.json();
    if (!Array.isArray(data) || !data.length) {
      el.textContent = "";
      return;
    }
    let html = "";
    data.forEach(function (req) {
      const kind = req.request_type === "delete" ? t("Löschung", "Deletion") : t("Export", "Export");
      if (req.status === "completed" && req.download_url) {
        html += '<div>' + esc(kind) + ': <a href="' + esc(req.download_url) + '" target="_blank" rel="noopener">' + t("Herunterladen", "Download") + '</a>' +
          (req.expires_at ? ' <span style="color:var(--text-muted)">(' + t("gültig bis", "valid until") + ' ' + esc(req.expires_at) + ')</span>' : '') + '</div>';
      } else if (req.status === "completed") {
        html += '<div>' + esc(kind) + ': ' + t("abgeschlossen", "completed") + '</div>';
      } else {
        html += '<div>' + esc(kind) + ': ' + t("in Bearbeitung", "in progress") + '</div>';
      }
    });
    el.innerHTML = html;
  } catch (e) {
    el.textContent = "";
  }
}

// Called directly (not via a DOMContentLoaded listener): like settings.js's
// own loadSettings() kick-off at the bottom of that file, this script tag
// is placed at the end of the body, so the DOM is already parsed by the
// time this runs -- a DOMContentLoaded listener registered here would very
// likely never fire (the event already happened).
loadTelemetrySettings();
