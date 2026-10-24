/* Visibility-aware polling.
 *
 * Loaded on every page (base.html, right after mf_escape.js).
 *
 * Why: badge and status timers are started with plain setInterval, and
 * base.html loads queue.js, devinfos.js, upscale_queue.js and
 * encoding_queue.js globally -- so their timers run on EVERY page. In an idle
 * browser that was roughly 30 requests a minute per open tab, and a tab kept
 * polling at full rate while sitting in the background for hours. Four tabs
 * meant a request every half second, each one going through the whole
 * before-request chain.
 *
 * mfPoll(fn, ms) behaves like setInterval, except:
 *   - while the tab is hidden, the timer does not fire at all;
 *   - when the tab becomes visible again it fires right away if its last run
 *     is at least one interval old, so the user never looks at a stale badge.
 *     A poll that ran a moment ago waits for the rest of its interval instead:
 *     firing every poll on every focus turned quick tab switching into bursts
 *     of requests, and made the 60 s pollers run on every switch.
 *
 * Returns a handle with stop(); mfPollStop(handle) works too.
 */
(function () {
  "use strict";

  var polls = [];
  var hidden = function () { return document.visibilityState === "hidden"; };

  function mfPoll(fn, intervalMs, opts) {
    var options = opts || {};
    var entry = {
      fn: fn,
      interval: intervalMs,
      timer: null,
      resume: null,  // pending one-shot timeout after the tab came back
      // Callers run fn themselves right before starting the poll, so the
      // first interval counts from here.
      last: Date.now(),
      // Some pollers must keep running in the background (e.g. one that has to
      // notice a finished job to fire a notification). They opt out explicitly.
      always: !!options.always,
      stopped: false
    };

    entry.tick = function () {
      if (entry.stopped) return;
      entry.last = Date.now();
      try { entry.fn(); } catch (e) { /* a failing poll must not kill the timer */ }
    };

    entry.arm = function () {
      if (entry.timer !== null || entry.stopped) return;
      entry.timer = setInterval(entry.tick, entry.interval);
    };

    entry.disarm = function () {
      if (entry.resume !== null) {
        clearTimeout(entry.resume);
        entry.resume = null;
      }
      if (entry.timer === null) return;
      clearInterval(entry.timer);
      entry.timer = null;
    };

    // Tab came back: run now if the last run is at least one interval old,
    // otherwise once the rest of the interval has passed. Either way the
    // regular timer is (re)armed from that run.
    entry.wake = function () {
      if (entry.stopped || entry.timer !== null || entry.resume !== null) return;
      var due = entry.interval - (Date.now() - entry.last);
      if (due <= 0) {
        entry.tick();
        entry.arm();
        return;
      }
      entry.resume = setTimeout(function () {
        entry.resume = null;
        if (entry.stopped || hidden()) return;
        entry.tick();
        entry.arm();
      }, due);
    };

    entry.stop = function () {
      entry.stopped = true;
      entry.disarm();
      var i = polls.indexOf(entry);
      if (i >= 0) polls.splice(i, 1);
    };

    polls.push(entry);
    if (entry.always || !hidden()) entry.arm();
    return entry;
  }

  document.addEventListener("visibilitychange", function () {
    var isHidden = hidden();
    for (var i = 0; i < polls.length; i++) {
      var entry = polls[i];
      if (entry.always || entry.stopped) continue;
      if (isHidden) {
        entry.disarm();
      } else {
        entry.wake();
      }
    }
  });

  window.mfPoll = mfPoll;
  window.mfPollStop = function (handle) { if (handle && handle.stop) handle.stop(); };
})();
