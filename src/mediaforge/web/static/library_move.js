/* Library move: the parts that must keep working on every page.
 *
 * A move runs on a server thread and keeps going after its modal is minimized.
 * But the pill that leads back into it, and the job id needed to reopen it,
 * used to live in library_video.js / library_video.html -- so minimizing a move
 * and then navigating anywhere else made it permanently invisible: still
 * running, still writing files, with no way to see it or learn when it
 * finished.
 *
 * Everything that has to survive that navigation therefore lives here and is
 * loaded from base.html alongside the modal markup: the pill, the polling, the
 * progress view and the job id in sessionStorage. library_video.js keeps only
 * what genuinely needs the library page -- choosing a destination
 * (libOpenMove) and starting the move (libConfirmMove).
 *
 * The functions are global rather than namespaced because the modal calls them
 * from inline onclick handlers, which is this codebase's existing convention.
 */

/* global t */

// Local, so this file does not depend on library_core.js (which is only loaded
// on the library pages -- the exact dependency that made the move page-bound).
function _libMoveEsc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}

// ── Move job state ──────────────────────────────────────────────
//
// The job id is mirrored into sessionStorage because a move outlives the page
// that started it. It runs on a server thread; minimizing the modal used to
// leave it running with no way back — the reopen handler needs an id, the id
// was a module-level variable, and a page navigation threw it away. Anything
// the user did after minimizing (going to Settings and back, opening a title)
// made a running move permanently invisible.
//
// sessionStorage rather than localStorage: the job belongs to this tab's
// session. A second tab that adopted the id would poll the same job and, with
// the old first-poller-wins cleanup, could delete it out from under the tab
// that started it.
var _LIB_MOVE_KEY = "mf-lib-move-job";

var _libMoveJobId    = null;
var _libMovePollTimer = null;
var _libMoveFolder   = "";

function _libMoveRemember(jobId, folder) {
  _libMoveJobId = jobId || null;
  _libMoveFolder = folder || "";
  try {
    if (jobId) {
      sessionStorage.setItem(_LIB_MOVE_KEY, JSON.stringify({ id: jobId, folder: folder || "" }));
    } else {
      sessionStorage.removeItem(_LIB_MOVE_KEY);
    }
  } catch (e) { /* private mode — the move still works, it just won't survive a navigation */ }
}

function _libMoveRestore() {
  try {
    var raw = sessionStorage.getItem(_LIB_MOVE_KEY);
    if (!raw) return null;
    var saved = JSON.parse(raw);
    return saved && saved.id ? saved : null;
  } catch (e) { return null; }
}

// Consecutive transport failures. A move can run for many minutes, during
// which one dropped request — a wifi blip, a proxy hiccup, the server being
// restarted — is entirely normal. Treating the first one as fatal would tear
// down the pill and forget the job id for a move that is still happily
// running: the very failure this file exists to prevent. Only a real answer
// from the server ("status": "error") is fatal immediately.
var _libMoveFailStreak = 0;
var _LIB_MOVE_MAX_FAILS = 8;

// The modal shows a live progress bar and wants a fast poll; the pill only
// shows whole percent. Polling four times faster than the eye can use it, on
// every page in the app, is just load.
var _LIB_MOVE_POLL_OPEN = 400;
var _LIB_MOVE_POLL_BG = 2000;

function _libMoveModalOpen() {
  var modal = document.getElementById("libMoveModal");
  return !!(modal && modal.style.display === "block");
}

function _libMoveStopPolling() {
  if (_libMovePollTimer) { clearInterval(_libMovePollTimer); _libMovePollTimer = null; }
}

/** Poll an in-flight move, whether we started it or picked it up on load. */
function _libMoveStartPolling(folder, interval) {
  _libMoveStopPolling();
  var period = interval || (_libMoveModalOpen() ? _LIB_MOVE_POLL_OPEN : _LIB_MOVE_POLL_BG);
  _libMovePollTimer = setInterval(async function () {
    // Re-tune when the modal is opened or minimized, without restarting the
    // move or losing the fail streak.
    var wanted = _libMoveModalOpen() ? _LIB_MOVE_POLL_OPEN : _LIB_MOVE_POLL_BG;
    if (wanted !== period) { period = wanted; _libMoveStartPolling(folder, wanted); return; }

    try {
      var r = await fetch("/api/library/move_status/" + _libMoveJobId);
      if (r.status === 404) {
        // The job is gone: it finished more than the server's grace period
        // ago, most likely while this tab was closed. Nothing to report and
        // nothing to recover — drop the pill rather than showing an error for
        // a move that probably succeeded.
        _libMoveRemember(null, "");
        _libMoveStopPolling();
        _libHideMovePill();
        return;
      }
      if (!r.ok) { _libMoveTransientFailure(t("Server-Fehler ", "Server error ") + r.status); return; }
      var s = await r.json();
      _libMoveFailStreak = 0;
      if (s.error && s.status !== "done") { _libMoveError(s.error); return; }
      var pct = s.total_bytes > 0 ? Math.round(s.copied_bytes / s.total_bytes * 100) : 0;
      _libMoveSetProgress(pct, s.current_file || "");
      if (s.status === "done") { _libMoveFinish(folder || s.folder || _libMoveFolder); }
      else if (s.status === "error") { _libMoveError(s.error || t("Unbekannter Fehler", "Unknown error")); }
    } catch (e) {
      _libMoveTransientFailure(t("Verbindung unterbrochen", "Connection interrupted"));
    }
  }, period);
}

/** A failed request: keep going unless it keeps happening. */
function _libMoveTransientFailure(msg) {
  _libMoveFailStreak += 1;
  if (_libMoveFailStreak >= _LIB_MOVE_MAX_FAILS) {
    _libMoveFailStreak = 0;
    _libMoveError(msg);
  }
}

/** On every page load: if a move is still running, show the pill again. */
function libResumeMove() {
  var saved = _libMoveRestore();
  if (!saved) return;
  _libMoveJobId = saved.id;
  _libMoveFolder = saved.folder || "";
  var pt = document.getElementById("libMoveProgressTitle");
  if (pt) pt.textContent = _libMoveFolder;
  _libShowMovePill(0);
  _libMoveStartPolling(_libMoveFolder);
}

document.addEventListener("DOMContentLoaded", libResumeMove);


function libCloseMoveModal() {
  var modal = document.getElementById("libMoveModal");
  if (modal) modal.style.display = "none";
}

// Called when clicking the background overlay — only close if no active job
function libMoveModalBgClick() {
  if (_libMoveJobId) { libMinimizeMove(); return; }
  libCloseMoveModal();
}

// Minimize to pill — keep job running in background
function libMinimizeMove() {
  libCloseMoveModal();
}

// Reopen progress modal from pill
function libOpenMoveProgress() {
  if (!_libMoveJobId) return;
  var modal = document.getElementById("libMoveModal");
  var sv    = document.getElementById("libMoveSelectView");
  var pv    = document.getElementById("libMoveProgressView");
  if (!modal) return;
  if (sv) sv.style.display = "none";
  if (pv) pv.style.display = "";
  modal.style.display = "block";
}

function _libShowMovePill(pct) {
  var pill = document.getElementById("libMovePill");
  var pctEl = document.getElementById("libMovePillPct");
  if (pill) pill.style.display = "";
  if (pctEl) pctEl.textContent = pct + "%";
}

function _libHideMovePill() {
  var pill = document.getElementById("libMovePill");
  if (pill) pill.style.display = "none";
}

function _libMoveSetProgress(pct, file) {
  var fill  = document.getElementById("libMoveProgressBarFill");
  var pctEl = document.getElementById("libMoveProgressPct");
  var fileEl = document.getElementById("libMoveProgressFile");
  if (fill)  fill.style.width  = pct + "%";
  if (pctEl) pctEl.textContent = pct + "%";
  if (fileEl) fileEl.textContent = file || "";
  _libShowMovePill(pct);
}

function _libMoveFinish(folder) {
  _libMoveRemember(null, "");
  _libMoveStopPolling();
  _libMoveFailStreak = 0;
  _libHideMovePill();
  libCloseMoveModal();
  if (window.showToast) showToast('"' + folder + t("wurde verschoben", "was moved") + '"');
  // Only the library page has a list to refresh -- the move can now finish
  // while the user is on any other page, where libLoad() does not exist.
  if (typeof libLoad === "function" && document.getElementById("libGrid")) { libLoad(false); }
}

function _libMoveError(msg) {
  _libMoveRemember(null, "");
  _libMoveStopPolling();
  _libMoveFailStreak = 0;
  _libHideMovePill();

  // Show error in modal
  var pv  = document.getElementById("libMoveProgressView");
  var err = document.getElementById("libMoveProgressError");
  var act = document.getElementById("libMoveProgressActions");
  var modal = document.getElementById("libMoveModal");
  if (modal) modal.style.display = "block";
  if (pv) pv.style.display = "";
  var sv = document.getElementById("libMoveSelectView");
  if (sv) sv.style.display = "none";
  if (err) { err.style.display = ""; err.textContent = t("Fehler: ", "Error: ") + msg; }
  if (act) act.innerHTML = '<button class="btn btn-secondary btn-sm" onclick="libCloseMoveModal()">' + _libMoveEsc(t("Schließen", "Close")) + '</button>';
}
