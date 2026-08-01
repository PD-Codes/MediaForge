/* Self-update overlay — global. Shows a full-screen install/restart view and
   polls /api/update/status until the update completes or fails. Also restores
   the overlay automatically after the app restarts. */
(function () {
  "use strict";

  var POLL_MS = 2000;
  var pollTimer = null;

  function $(id) { return document.getElementById(id); }
  function tt(de, en) { return (typeof t === "function") ? t(de, en) : en; }
  function overlay() { return $("updateOverlay"); }

  function show() { var o = overlay(); if (o) o.style.display = "flex"; }
  function hide() { var o = overlay(); if (o) o.style.display = "none"; }

  function render(st) {
    if (!overlay()) return;
    var state = (st && st.state) ? st.state : "idle";
    var spinner = $("updateOverlaySpinner");
    var icon    = $("updateOverlayIcon");
    var title   = $("updateOverlayTitle");
    var text    = $("updateOverlayText");
    var ver     = $("updateOverlayVersion");
    var logWrap = $("updateOverlayLogWrap");
    var logEl   = $("updateOverlayLog");
    var actions = $("updateOverlayActions");
    var btn     = $("updateOverlayBtn");
    var hint    = $("updateOverlayHint");

    if (logEl && st && st.log) logEl.textContent = st.log;

    if (state === "installing" || state === "restarting") {
      show();
      spinner.style.display = "";
      icon.style.display = "none";
      actions.style.display = "none";
      title.textContent = state === "restarting"
        ? tt("Neustart …", "Restarting …")
        : tt("Update wird installiert …", "Installing update …");
      text.textContent = tt("Bitte dieses Fenster nicht schließen.",
                            "Please don't close this window.");
      ver.style.display = "none";
      logWrap.style.display = (st && st.log) ? "" : "none";
      hint.textContent = tt("Die Verbindung kann kurz abbrechen, während die App neu startet.",
                            "The connection may drop briefly while the app restarts.");
    } else if (state === "success") {
      show();
      spinner.style.display = "none";
      icon.style.display = "";
      icon.textContent = "✓";
      icon.className = "update-overlay-icon success";
      title.textContent = tt("Update abgeschlossen", "Update complete");
      text.textContent = tt("Die App läuft jetzt in der neuen Version.",
                            "The app is now running the new version.");
      if (st && st.restart_only) {
        title.textContent = tt("Neustart abgeschlossen", "Restart complete");
        text.textContent = tt("Die App wurde neu gestartet.", "The app has restarted.");
      }
      if (st && st.to_version) { ver.style.display = ""; ver.textContent = "v" + st.to_version; }
      else ver.style.display = "none";
      logWrap.style.display = (st && st.log) ? "" : "none";
      actions.style.display = "";
      btn.textContent = tt("Seite neu laden", "Reload page");
      btn.className = "update-overlay-btn primary";
      btn.onclick = function () { ack(function () { location.reload(); }); };
      hint.textContent = "";
      stopPoll();
    } else if (state === "failed") {
      show();
      spinner.style.display = "none";
      icon.style.display = "";
      icon.textContent = "!";
      icon.className = "update-overlay-icon error";
      title.textContent = tt("Update fehlgeschlagen", "Update failed");
      text.textContent = (st && st.error) ? st.error
        : tt("Beim Update ist ein Fehler aufgetreten. Die vorherige Version läuft weiter.",
             "Something went wrong during the update. The previous version keeps running.");
      ver.style.display = "none";
      logWrap.style.display = (st && st.log) ? "" : "none";
      actions.style.display = "";
      btn.textContent = tt("Schließen", "Close");
      btn.className = "update-overlay-btn";
      btn.onclick = function () { ack(function () { hide(); }); };
      hint.textContent = "";
      stopPoll();
    } else {
      hide();
      stopPoll();
    }
  }

  function fetchStatus() {
    return fetch("/api/update/status", { cache: "no-store" }).then(function (r) { return r.json(); });
  }

  function poll() {
    fetchStatus().then(render).catch(function () {
      /* During the restart the server is unreachable — keep the overlay up and
         keep polling; it will reconnect on its own. */
    });
  }

  function startPoll() { if (!pollTimer) pollTimer = setInterval(poll, POLL_MS); }
  function stopPoll() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

  function ack(cb) {
    fetch("/api/update/status/ack", { method: "POST" })
      .then(function () { if (cb) cb(); })
      .catch(function () { if (cb) cb(); });
  }

  function startInstall(channel) {
    render({ state: "installing" });
    show();
    var body = channel ? JSON.stringify({ channel: channel }) : "{}";
    fetch("/api/update/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (e) { throw new Error(e.error || ("HTTP " + r.status)); });
      return r.json();
    }).then(function () {
      startPoll();           // server will exit & restart shortly
    }).catch(function (err) {
      render({ state: "failed", error: (err && err.message) || String(err) });
    });
  }

  function startRestart() {
    render({ state: "restarting", restart_only: true });
    show();
    fetch("/api/restart", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}"
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (e) { throw new Error(e.error || ("HTTP " + r.status)); });
      return r.json();
    }).then(function () {
      startPoll();           // server will exit & relaunch shortly
    }).catch(function (err) {
      render({ state: "failed", error: (err && err.message) || String(err) });
    });
  }

  window.AniUpdate = { startInstall: startInstall, startRestart: startRestart, show: show, hide: hide };

  document.addEventListener("DOMContentLoaded", function () {
    if (!overlay()) return;
    fetchStatus().then(function (st) {
      if (st && st.state && st.state !== "idle") {
        render(st);
        if (st.state === "installing" || st.state === "restarting") startPoll();
      }
    }).catch(function () { });
  });
})();
