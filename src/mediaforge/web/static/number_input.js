/**
 * Custom themed [ − ][ value ][ + ] stepper pill for <input type="number">.
 *
 * The native browser spin buttons (the classic tiny up/down arrows) don't
 * match the app's dark/light theme, so style.css hides them and this script
 * builds a themed replacement — the same idea as the custom <select> arrow,
 * just interactive: each input gets wrapped in a .num-input-wrap pill with a
 * minus button before it and a plus button after it, and clicking a button
 * calls the input's native stepUp()/stepDown() (which already respects
 * min/max/step) and fires input/change events so existing onchange="..."
 * handlers still run exactly as if the user had typed a new value.
 *
 * This runs globally (loaded once from templates/base.html) as a progressive
 * enhancement, so none of the templates that use number inputs (settings,
 * integrations, encoding, notifications, advanced_search, syncplay) needed
 * any markup changes. A MutationObserver re-scans for number inputs added
 * later by page-specific JS (e.g. settings modals built dynamically after
 * the initial page load).
 */
(function () {
  "use strict";

  var PLUS_ICON  = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>';
  var MINUS_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M5 12h14"/></svg>';

  function fireChange(input) {
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // Disable a direction's button once the value has hit min/max, same as
  // native spin buttons do.
  function syncDisabled(input, plusBtn, minusBtn) {
    var val = parseFloat(input.value);
    var max = input.max !== "" ? parseFloat(input.max) : null;
    var min = input.min !== "" ? parseFloat(input.min) : null;
    plusBtn.disabled  = input.disabled || (max !== null && !isNaN(val) && val >= max);
    minusBtn.disabled = input.disabled || (min !== null && !isNaN(val) && val <= min);
  }

  function makeButton(cls, icon, label) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "num-step-btn " + cls;
    btn.innerHTML = icon;
    btn.tabIndex = -1;
    btn.setAttribute("aria-label", label);
    return btn;
  }

  function enhance(input) {
    if (input.dataset.numEnhanced) return;
    input.dataset.numEnhanced = "1";

    var wrap = document.createElement("div");
    wrap.className = "num-input-wrap";

    // The pill has its own fixed-ish sizing (button + text + button), so any
    // leftover fixed width from the plain-input days (style="width:70px" on
    // the CRF fields etc.) would just cramp it — drop it and let the pill
    // use its natural/flex sizing instead. min-width/max-width need the same
    // treatment: e.g. the shared thirdparty-extra-field markup sets
    // style="min-width:160px" (sized for its "text"/"secret" siblings), and
    // min-width always wins over the pill's own width:100% !important (CSS
    // clamps used width to at least min-width), so left alone it forces the
    // input past the pill's 120px box and pushes the "+" button out past the
    // wrap's clipped edge — right where the row's Save button sits.
    input.style.width = "";
    input.style.minWidth = "";
    input.style.maxWidth = "";

    input.parentNode.insertBefore(wrap, input);

    var minusBtn = makeButton("num-step-down", MINUS_ICON, "Decrease");
    var plusBtn  = makeButton("num-step-up", PLUS_ICON, "Increase");

    wrap.appendChild(minusBtn);
    wrap.appendChild(input);
    wrap.appendChild(plusBtn);

    plusBtn.addEventListener("click", function () {
      if (input.disabled) return;
      input.stepUp();
      syncDisabled(input, plusBtn, minusBtn);
      fireChange(input);
    });
    minusBtn.addEventListener("click", function () {
      if (input.disabled) return;
      input.stepDown();
      syncDisabled(input, plusBtn, minusBtn);
      fireChange(input);
    });

    syncDisabled(input, plusBtn, minusBtn);
    input.addEventListener("input", function () { syncDisabled(input, plusBtn, minusBtn); });
  }

  function scan(root) {
    root.querySelectorAll('input[type="number"]:not([data-num-enhanced])').forEach(enhance);
  }

  document.addEventListener("DOMContentLoaded", function () { scan(document); });

  var observer = new MutationObserver(function (mutations) {
    for (var i = 0; i < mutations.length; i++) {
      var added = mutations[i].addedNodes;
      for (var j = 0; j < added.length; j++) {
        var node = added[j];
        if (node.nodeType !== 1) continue;
        if (node.matches && node.matches('input[type="number"]')) enhance(node);
        else if (node.querySelectorAll) scan(node);
      }
    }
  });
  document.addEventListener("DOMContentLoaded", function () {
    observer.observe(document.body, { childList: true, subtree: true });
  });
})();
