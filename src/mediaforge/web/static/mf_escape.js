/* Shared HTML escaping helpers.
 *
 * Loaded before every other script in base.html, so window.mfEscape /
 * window.mfSafeUrl are available everywhere, including inline blocks.
 *
 * Why this file exists: the project used to carry six separate esc()
 * implementations, five of which were built as
 *
 *     var d = document.createElement("div");
 *     d.textContent = value;
 *     return d.innerHTML;
 *
 * That escapes &, < and > -- but NOT " and '. Every one of them was then used
 * to build attributes (data-id="...", title="...", onclick="...('...')"), so a
 * value containing a double quote closed the attribute and the rest of it was
 * parsed as markup: a module name from the store, a Dev-Info tag, an uptime
 * error message coming back from a monitored third-party host, or simply a
 * file name on disk was enough to inject an event handler.
 *
 * mfEscape() escapes all five characters and is therefore safe in both text
 * and attribute context. It is deliberately the ONLY escaper: a second one
 * that "almost" works is how this class of bug came back three times.
 */
(function () {
  "use strict";

  var MAP = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  };

  /* HTML-escape a value for text AND attribute context.
   * null/undefined become "", every other value is stringified first -- note
   * that 0 and false must survive as "0"/"false" (a falsy check here used to
   * silently drop episode number 0 and a size of 0 MB). */
  function mfEscape(value) {
    if (value === null || value === undefined) return "";
    return String(value).replace(/[&<>"']/g, function (ch) { return MAP[ch]; });
  }

  /* Allow only http(s) and same-origin URLs, so a link or an image src can
   * never carry javascript: or data:. Returns "" for anything else. */
  function mfSafeUrl(value) {
    var s = String(value === null || value === undefined ? "" : value).trim();
    if (!s) return "";
    if (/^(https?:)?\/\//i.test(s)) return s;
    if (s.charAt(0) === "/" || s.charAt(0) === "?" || s.charAt(0) === "#") return s;
    return "";
  }

  window.mfEscape = mfEscape;
  window.mfSafeUrl = mfSafeUrl;
})();
