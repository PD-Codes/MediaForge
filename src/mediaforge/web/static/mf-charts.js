/* ===================================================================
   MediaForge - MFCharts
   Dependency-free inline-SVG chart primitives (no CDN, no bundler).

   Why hand-rolled instead of Chart.js: MediaForge ships offline-capable
   and CSP-friendly, themes are pure CSS custom properties (incl. user
   theme packs), and the whole set of charts we need is small. Everything
   here paints with var(--...) tokens so a theme pack restyles the charts
   for free.

   Public API (also intended for third-party modules, see
   .examples/thirdparties/example_ui_components):
     MFCharts.render(target, spec)     -> mount a chart, auto re-renders on resize
     MFCharts.renderAll(root)          -> mount every [data-mfc-id] placeholder
     MFCharts.place(id, spec)          -> queue a spec + return placeholder HTML
     MFCharts.sparkline(values, opts)  -> tiny standalone SVG string
     MFCharts.destroy(target)          -> detach observers
     MFCharts.palette                  -> default categorical colors

   Supported spec.type values:
     "area"    - time series, one or many series, optional smoothing
     "bars"    - vertical or horizontal bars, optional stacking
     "donut"   - donut / ring with center label
     "heatmap" - matrix (e.g. weekday x hour activity)
     "gauge"   - single percentage ring

   All specs accept: { height, valueFmt(fn), labelFmt(fn), empty(text) }
   =================================================================== */

(function (global) {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";

  // Categorical palette. Kept deliberately colorblind-safe-ish and in the
  // same hue family the stat cards already use, so charts and KPI cards read
  // as one system.
  var PALETTE = [
    "#7c3aed", "#22c55e", "#6ea8fe", "#f59e0b", "#f472b6",
    "#06b6d4", "#e8914a", "#a78bfa", "#34d399", "#f87171",
  ];

  // ---------------------------------------------------------------
  // Small helpers
  // ---------------------------------------------------------------

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function num(v) {
    var n = Number(v);
    return isFinite(n) ? n : 0;
  }

  function nice(max) {
    // Round an axis maximum up to a readable step (1/2/5 x 10^n).
    if (max <= 0) return 1;
    var exp = Math.floor(Math.log10(max));
    var base = Math.pow(10, exp);
    var f = max / base;
    var mult = f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10;
    return mult * base;
  }

  function defaultFmt(v) {
    if (v == null) return "-";
    if (Math.abs(v) >= 1000000) return (v / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
    if (Math.abs(v) >= 1000) return (v / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return String(Math.round(v * 100) / 100);
  }

  // Catmull-Rom -> cubic bezier, so multi-point series read as a smooth
  // trend instead of a jagged polyline. Falls back to straight segments
  // for 2 points or when smoothing is disabled.
  function smoothPath(pts) {
    if (pts.length < 3) {
      return pts.map(function (p, i) { return (i ? "L" : "M") + p[0] + " " + p[1]; }).join(" ");
    }
    var d = "M" + pts[0][0] + " " + pts[0][1];
    for (var i = 0; i < pts.length - 1; i++) {
      var p0 = pts[i - 1] || pts[i];
      var p1 = pts[i];
      var p2 = pts[i + 1];
      var p3 = pts[i + 2] || p2;
      var c1x = p1[0] + (p2[0] - p0[0]) / 6;
      var c1y = p1[1] + (p2[1] - p0[1]) / 6;
      var c2x = p2[0] - (p3[0] - p1[0]) / 6;
      var c2y = p2[1] - (p3[1] - p1[1]) / 6;
      d += " C" + c1x + " " + c1y + "," + c2x + " " + c2y + "," + p2[0] + " " + p2[1];
    }
    return d;
  }

  function linePath(pts) {
    return pts.map(function (p, i) { return (i ? "L" : "M") + p[0] + " " + p[1]; }).join(" ");
  }

  // ---------------------------------------------------------------
  // Shared tooltip (one node for the whole page)
  // ---------------------------------------------------------------

  var tipEl = null;

  function tip() {
    if (!tipEl) {
      tipEl = document.createElement("div");
      tipEl.className = "mfc-tooltip";
      tipEl.setAttribute("role", "tooltip");
      document.body.appendChild(tipEl);
    }
    return tipEl;
  }

  function showTip(evt, html) {
    var el = tip();
    el.innerHTML = html;
    el.classList.add("visible");
    moveTip(evt);
  }

  function moveTip(evt) {
    if (!tipEl) return;
    var pad = 14;
    var r = tipEl.getBoundingClientRect();
    var x = evt.clientX + pad;
    var y = evt.clientY - r.height - 10;
    if (x + r.width > window.innerWidth - 8) x = evt.clientX - r.width - pad;
    if (y < 8) y = evt.clientY + pad;
    tipEl.style.transform = "translate(" + Math.round(x) + "px," + Math.round(y) + "px)";
  }

  function hideTip() {
    if (tipEl) tipEl.classList.remove("visible");
  }

  // Delegated once, globally: every chart element that carries a data-tip
  // attribute participates. Avoids one listener per bar/point.
  document.addEventListener("mouseover", function (e) {
    var t = e.target.closest && e.target.closest("[data-mfc-tip]");
    if (t) showTip(e, t.getAttribute("data-mfc-tip"));
  });
  document.addEventListener("mousemove", function (e) {
    if (tipEl && tipEl.classList.contains("visible")) moveTip(e);
  });
  document.addEventListener("mouseout", function (e) {
    var t = e.target.closest && e.target.closest("[data-mfc-tip]");
    if (t) hideTip();
  });
  window.addEventListener("scroll", hideTip, true);

  // ---------------------------------------------------------------
  // Chart builders. Each returns an SVG markup string for a given
  // pixel width, so the caller can re-render on resize.
  // ---------------------------------------------------------------

  function emptyState(w, h, text) {
    return '<div class="mfc-empty" style="height:' + h + 'px">' + esc(text || "") + "</div>";
  }

  function svgOpen(w, h, label) {
    return '<svg class="mfc-svg" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h +
      '" role="img" aria-label="' + esc(label || "chart") + '">';
  }

  function gridAndAxis(x0, y0, plotW, plotH, maxV, fmt, steps) {
    steps = steps || 4;
    var out = "";
    for (var i = 0; i <= steps; i++) {
      var v = (maxV / steps) * i;
      var y = y0 + plotH - (plotH * i) / steps;
      out += '<line class="mfc-grid" x1="' + x0 + '" y1="' + y + '" x2="' + (x0 + plotW) + '" y2="' + y + '"/>';
      out += '<text class="mfc-axis-label" x="' + (x0 - 8) + '" y="' + (y + 4) + '" text-anchor="end">' +
        esc(fmt(v)) + "</text>";
    }
    return out;
  }

  // ----- area / line -----------------------------------------------

  function buildArea(spec, w) {
    var h = spec.height || 220;
    var series = spec.series || [{ name: spec.name || "", values: spec.values || [], color: spec.color }];
    var labels = spec.labels || [];
    var n = labels.length || (series[0] && series[0].values.length) || 0;
    if (!n) return emptyState(w, h, spec.empty);

    var fmt = spec.valueFmt || defaultFmt;
    var padL = spec.padL == null ? 46 : spec.padL;
    var padR = 10, padT = 12, padB = 28;
    var plotW = Math.max(10, w - padL - padR);
    var plotH = Math.max(10, h - padT - padB);

    var maxV = 0;
    series.forEach(function (s) {
      (s.values || []).forEach(function (v) { if (num(v) > maxV) maxV = num(v); });
    });
    maxV = nice(maxV || 1);

    var xAt = function (i) { return padL + (n === 1 ? plotW / 2 : (plotW * i) / (n - 1)); };
    var yAt = function (v) { return padT + plotH - (plotH * num(v)) / maxV; };

    var out = svgOpen(w, h, spec.aria || "area chart");
    out += "<defs>";
    series.forEach(function (s, si) {
      var c = s.color || PALETTE[si % PALETTE.length];
      out += '<linearGradient id="mfcg' + spec._uid + "_" + si + '" x1="0" y1="0" x2="0" y2="1">' +
        '<stop offset="0%" stop-color="' + esc(c) + '" stop-opacity="0.38"/>' +
        '<stop offset="100%" stop-color="' + esc(c) + '" stop-opacity="0"/></linearGradient>';
    });
    out += "</defs>";
    out += gridAndAxis(padL, padT, plotW, plotH, maxV, fmt);

    series.forEach(function (s, si) {
      var c = s.color || PALETTE[si % PALETTE.length];
      var pts = (s.values || []).map(function (v, i) { return [xAt(i), yAt(v)]; });
      if (!pts.length) return;
      var d = spec.smooth === false ? linePath(pts) : smoothPath(pts);
      if (spec.fill !== false) {
        out += '<path class="mfc-area" d="' + d + " L" + pts[pts.length - 1][0] + " " + (padT + plotH) +
          " L" + pts[0][0] + " " + (padT + plotH) + ' Z" fill="url(#mfcg' + spec._uid + "_" + si + ')"/>';
      }
      out += '<path class="mfc-line" d="' + d + '" stroke="' + esc(c) + '"/>';
    });

    // X labels: thin them out so they never overlap on narrow screens.
    var every = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 64))));
    for (var i = 0; i < n; i++) {
      if (i % every !== 0 && i !== n - 1) continue;
      out += '<text class="mfc-axis-label" x="' + xAt(i) + '" y="' + (h - 8) + '" text-anchor="middle">' +
        esc(labels[i] == null ? i : labels[i]) + "</text>";
    }

    // One transparent hit-column per index -> hovering anywhere in the column
    // shows every series' value for that x. Cheaper and far easier to hit on
    // touch than per-point circles.
    var colW = n > 1 ? plotW / (n - 1) : plotW;
    for (var j = 0; j < n; j++) {
      var cx = xAt(j);
      var rows = series.map(function (s, si) {
        var c = s.color || PALETTE[si % PALETTE.length];
        return '<span class="mfc-tip-row"><i style="background:' + esc(c) + '"></i>' +
          esc(s.name || "") + " <b>" + esc(fmt(num((s.values || [])[j]))) + "</b></span>";
      }).join("");
      var tipHtml = '<span class="mfc-tip-title">' + esc(labels[j] == null ? j : labels[j]) + "</span>" + rows;
      out += '<rect class="mfc-hit" x="' + (cx - colW / 2) + '" y="' + padT + '" width="' + colW +
        '" height="' + plotH + '" data-mfc-tip="' + esc(tipHtml) + '"/>';
      series.forEach(function (s, si) {
        var c = s.color || PALETTE[si % PALETTE.length];
        out += '<circle class="mfc-dot" cx="' + cx + '" cy="' + yAt((s.values || [])[j]) + '" r="3" fill="' + esc(c) + '"/>';
      });
    }

    out += "</svg>";
    if (series.length > 1 || spec.legend) out += legendHtml(series);
    return out;
  }

  function legendHtml(series) {
    return '<div class="mfc-legend">' + series.map(function (s, si) {
      var c = s.color || PALETTE[si % PALETTE.length];
      return '<span class="mfc-legend-item"><i style="background:' + esc(c) + '"></i>' + esc(s.name || "") + "</span>";
    }).join("") + "</div>";
  }

  // ----- bars -------------------------------------------------------

  function buildBars(spec, w) {
    var data = spec.data || [];
    if (!data.length) return emptyState(w, spec.height || 200, spec.empty);
    var fmt = spec.valueFmt || defaultFmt;

    if (spec.horizontal) {
      // Horizontal bars are laid out with plain HTML, not SVG: labels then
      // wrap and truncate with normal CSS, which SVG text cannot do.
      var max = Math.max.apply(null, data.map(function (d) { return num(d.value); })) || 1;
      return '<div class="mfc-hbars">' + data.map(function (d, i) {
        var c = d.color || spec.color || PALETTE[i % PALETTE.length];
        var pct = (num(d.value) / max) * 100;
        return '<div class="mfc-hbar-row" data-mfc-tip="' +
          esc('<span class="mfc-tip-title">' + esc(d.label) + '</span><span class="mfc-tip-row"><b>' + esc(fmt(d.value)) + "</b></span>") + '">' +
          '<span class="mfc-hbar-label" title="' + esc(d.label) + '">' + esc(d.label) + "</span>" +
          '<span class="mfc-hbar-track"><span class="mfc-hbar-fill" style="width:' + pct.toFixed(2) +
          "%;background:" + esc(c) + '"></span></span>' +
          '<span class="mfc-hbar-value">' + esc(fmt(d.value)) + "</span></div>";
      }).join("") + "</div>";
    }

    var h = spec.height || 200;
    var padL = spec.padL == null ? 46 : spec.padL, padR = 10, padT = 12, padB = 30;
    var plotW = Math.max(10, w - padL - padR);
    var plotH = Math.max(10, h - padT - padB);
    var maxV = nice(Math.max.apply(null, data.map(function (d) { return num(d.value); })) || 1);
    var step = plotW / data.length;
    var bw = Math.max(3, Math.min(spec.maxBarWidth || 42, step * 0.62));

    var out = svgOpen(w, h, spec.aria || "bar chart");
    out += gridAndAxis(padL, padT, plotW, plotH, maxV, fmt);
    data.forEach(function (d, i) {
      var c = d.color || spec.color || PALETTE[i % PALETTE.length];
      var bh = (plotH * num(d.value)) / maxV;
      var x = padL + step * i + (step - bw) / 2;
      var y = padT + plotH - bh;
      var tipHtml = '<span class="mfc-tip-title">' + esc(d.label) + '</span><span class="mfc-tip-row"><i style="background:' +
        esc(c) + '"></i><b>' + esc(fmt(d.value)) + "</b></span>";
      out += '<rect class="mfc-bar" x="' + x + '" y="' + y + '" width="' + bw + '" height="' + Math.max(0, bh) +
        '" rx="4" fill="' + esc(c) + '" data-mfc-tip="' + esc(tipHtml) + '"/>';
    });
    var every = Math.max(1, Math.ceil(data.length / Math.max(2, Math.floor(plotW / 52))));
    data.forEach(function (d, i) {
      if (i % every !== 0) return;
      out += '<text class="mfc-axis-label" x="' + (padL + step * i + step / 2) + '" y="' + (h - 10) +
        '" text-anchor="middle">' + esc(d.label) + "</text>";
    });
    out += "</svg>";
    return out;
  }

  // ----- donut ------------------------------------------------------

  function buildDonut(spec, w) {
    var data = (spec.data || []).filter(function (d) { return num(d.value) > 0; });
    var h = spec.height || 220;
    if (!data.length) return emptyState(w, h, spec.empty);
    var fmt = spec.valueFmt || defaultFmt;
    var total = data.reduce(function (a, d) { return a + num(d.value); }, 0) || 1;

    var size = Math.min(w, h);
    var cx = w / 2, cy = h / 2;
    var r = size / 2 - 12;
    var thickness = spec.thickness || Math.max(14, r * 0.34);
    var ir = r - thickness;

    var out = svgOpen(w, h, spec.aria || "donut chart");
    var angle = -Math.PI / 2;
    data.forEach(function (d, i) {
      var c = d.color || PALETTE[i % PALETTE.length];
      var frac = num(d.value) / total;
      var a2 = angle + frac * Math.PI * 2;
      // A single full-circle slice cannot be drawn as an arc (start == end),
      // so render it as a ring instead.
      var seg;
      if (frac >= 0.9999) {
        seg = '<circle cx="' + cx + '" cy="' + cy + '" r="' + ((r + ir) / 2) + '" fill="none" stroke="' +
          esc(c) + '" stroke-width="' + thickness + '"/>';
      } else {
        var large = frac > 0.5 ? 1 : 0;
        var p = function (rr, a) { return [cx + rr * Math.cos(a), cy + rr * Math.sin(a)]; };
        var o1 = p(r, angle), o2 = p(r, a2), i2 = p(ir, a2), i1 = p(ir, angle);
        seg = '<path d="M' + o1 + " A" + r + " " + r + " 0 " + large + " 1 " + o2 +
          " L" + i2 + " A" + ir + " " + ir + " 0 " + large + " 0 " + i1 + ' Z" fill="' + esc(c) + '"/>';
      }
      var tipHtml = '<span class="mfc-tip-title">' + esc(d.label) + '</span><span class="mfc-tip-row"><i style="background:' +
        esc(c) + '"></i><b>' + esc(fmt(d.value)) + "</b> &middot; " + (frac * 100).toFixed(1) + "%</span>";
      out += '<g class="mfc-slice" data-mfc-tip="' + esc(tipHtml) + '">' + seg + "</g>";
      angle = a2;
    });
    if (spec.centerValue != null) {
      out += '<text class="mfc-donut-value" x="' + cx + '" y="' + (cy + 2) + '" text-anchor="middle">' +
        esc(spec.centerValue) + "</text>";
    }
    if (spec.centerLabel) {
      out += '<text class="mfc-donut-label" x="' + cx + '" y="' + (cy + 22) + '" text-anchor="middle">' +
        esc(spec.centerLabel) + "</text>";
    }
    out += "</svg>";
    out += legendHtml(data.map(function (d, i) {
      return { name: d.label + " (" + fmt(d.value) + ")", color: d.color || PALETTE[i % PALETTE.length] };
    }));
    return out;
  }

  // ----- gauge ------------------------------------------------------

  function buildGauge(spec, w) {
    var h = spec.height || 150;
    var pct = Math.max(0, Math.min(100, num(spec.percent)));
    var size = Math.min(w, h);
    var cx = w / 2, cy = h / 2;
    var r = size / 2 - 12;
    var th = spec.thickness || Math.max(10, r * 0.26);
    var rr = r - th / 2;
    var circ = 2 * Math.PI * rr;
    var c = spec.color || "#22c55e";
    var out = svgOpen(w, h, spec.aria || "gauge");
    out += '<circle class="mfc-gauge-track" cx="' + cx + '" cy="' + cy + '" r="' + rr + '" stroke-width="' + th + '"/>';
    out += '<circle class="mfc-gauge-fill" cx="' + cx + '" cy="' + cy + '" r="' + rr + '" stroke-width="' + th +
      '" stroke="' + esc(c) + '" stroke-dasharray="' + (circ * pct / 100) + " " + circ +
      '" transform="rotate(-90 ' + cx + " " + cy + ')"/>';
    out += '<text class="mfc-donut-value" x="' + cx + '" y="' + (cy + 4) + '" text-anchor="middle">' +
      esc(spec.label != null ? spec.label : Math.round(pct) + "%") + "</text>";
    if (spec.sub) {
      out += '<text class="mfc-donut-label" x="' + cx + '" y="' + (cy + 24) + '" text-anchor="middle">' + esc(spec.sub) + "</text>";
    }
    out += "</svg>";
    return out;
  }

  // ----- heatmap ----------------------------------------------------

  function buildHeatmap(spec, w) {
    var rows = spec.rows || [];      // [{label, values:[..]}]
    var cols = spec.cols || [];      // column labels
    if (!rows.length || !cols.length) return emptyState(w, spec.height || 200, spec.empty);
    var fmt = spec.valueFmt || defaultFmt;
    var color = spec.color || "#7c3aed";
    var max = 0;
    rows.forEach(function (r) { (r.values || []).forEach(function (v) { if (num(v) > max) max = num(v); }); });
    max = max || 1;

    var labelW = spec.labelW || 34;
    var gap = 2;
    var cell = Math.max(6, (w - labelW - gap) / cols.length - gap);
    var h = rows.length * (cell + gap) + 22;

    var out = svgOpen(w, h, spec.aria || "heatmap");
    rows.forEach(function (r, ri) {
      var y = ri * (cell + gap);
      out += '<text class="mfc-axis-label" x="' + (labelW - 8) + '" y="' + (y + cell / 2 + 4) + '" text-anchor="end">' +
        esc(r.label) + "</text>";
      (r.values || []).forEach(function (v, ci) {
        var val = num(v);
        // Keep a faint floor opacity so empty cells still read as a grid.
        var op = val <= 0 ? 0.06 : 0.18 + 0.82 * Math.sqrt(val / max);
        var tipHtml = '<span class="mfc-tip-title">' + esc(r.label + " " + cols[ci]) +
          '</span><span class="mfc-tip-row"><b>' + esc(fmt(val)) + "</b></span>";
        out += '<rect class="mfc-cell" x="' + (labelW + ci * (cell + gap)) + '" y="' + y + '" width="' + cell +
          '" height="' + cell + '" rx="3" fill="' + esc(color) + '" fill-opacity="' + op.toFixed(3) +
          '" data-mfc-tip="' + esc(tipHtml) + '"/>';
      });
    });
    var every = Math.max(1, Math.ceil(cols.length / Math.max(2, Math.floor((w - labelW) / 34))));
    cols.forEach(function (cl, ci) {
      if (ci % every !== 0) return;
      out += '<text class="mfc-axis-label" x="' + (labelW + ci * (cell + gap) + cell / 2) + '" y="' + (h - 6) +
        '" text-anchor="middle">' + esc(cl) + "</text>";
    });
    out += "</svg>";
    return out;
  }

  // ----- sparkline (standalone string, no mounting) -------------------

  function sparkline(values, opts) {
    opts = opts || {};
    var vals = (values || []).map(num);
    var w = opts.width || 92, h = opts.height || 28;
    if (vals.length < 2) return '<span class="mfc-spark-empty" style="width:' + w + "px;height:" + h + 'px"></span>';
    var max = Math.max.apply(null, vals);
    var min = Math.min.apply(null, vals);
    var span = max - min || 1;
    var c = opts.color || "#7c3aed";
    var pts = vals.map(function (v, i) {
      return [(w * i) / (vals.length - 1), h - 2 - ((h - 4) * (v - min)) / span];
    });
    var d = smoothPath(pts);
    var uid = "sp" + Math.random().toString(36).slice(2, 8);
    return '<svg class="mfc-spark" width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + " " + h +
      '" aria-hidden="true"><defs><linearGradient id="' + uid + '" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0%" stop-color="' + esc(c) + '" stop-opacity="0.35"/>' +
      '<stop offset="100%" stop-color="' + esc(c) + '" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + d + " L" + w + " " + h + " L0 " + h + ' Z" fill="url(#' + uid + ')"/>' +
      '<path class="mfc-spark-line" d="' + d + '" stroke="' + esc(c) + '"/>' +
      '<circle cx="' + pts[pts.length - 1][0] + '" cy="' + pts[pts.length - 1][1] + '" r="2.4" fill="' + esc(c) + '"/></svg>';
  }

  // ---------------------------------------------------------------
  // Mounting / resize handling
  // ---------------------------------------------------------------

  var BUILDERS = {
    area: buildArea,
    line: function (s, w) { s.fill = false; return buildArea(s, w); },
    bars: buildBars,
    donut: buildDonut,
    gauge: buildGauge,
    heatmap: buildHeatmap,
  };

  var uidSeq = 0;
  var pending = {};   // id -> spec, queued by place()
  var mounted = new WeakMap();

  function resolve(target) {
    if (!target) return null;
    return typeof target === "string" ? document.getElementById(target) : target;
  }

  function paint(el, spec) {
    var w = Math.max(80, Math.floor(el.clientWidth || el.getBoundingClientRect().width || 320));
    var build = BUILDERS[spec.type];
    if (!build) { el.innerHTML = ""; return; }
    el.innerHTML = build(spec, w);
  }

  function render(target, spec) {
    var el = resolve(target);
    if (!el || !spec) return null;
    spec._uid = spec._uid || ++uidSeq;
    destroy(el);
    paint(el, spec);
    // Re-render at the new pixel width instead of scaling the SVG, so stroke
    // widths and label sizes stay constant across breakpoints.
    if (typeof ResizeObserver !== "undefined") {
      var lastW = el.clientWidth;
      var raf = 0;
      var ro = new ResizeObserver(function () {
        if (Math.abs(el.clientWidth - lastW) < 8) return;
        lastW = el.clientWidth;
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(function () { paint(el, spec); });
      });
      ro.observe(el);
      mounted.set(el, ro);
    }
    return el;
  }

  function destroy(target) {
    var el = resolve(target);
    if (!el) return;
    var ro = mounted.get(el);
    if (ro) { ro.disconnect(); mounted.delete(el); }
  }

  // place() lets callers that build their page as one big HTML string still
  // use charts: it returns a placeholder <div> and remembers the spec, then a
  // single renderAll() call after innerHTML mounts every chart at once.
  function place(id, spec, className) {
    pending[id] = spec;
    return '<div class="mfc-chart ' + (className || "") + '" id="' + esc(id) + '" data-mfc-id="' + esc(id) + '"></div>';
  }

  function renderAll(root) {
    (root || document).querySelectorAll("[data-mfc-id]").forEach(function (el) {
      var spec = pending[el.getAttribute("data-mfc-id")];
      if (spec) render(el, spec);
    });
  }

  global.MFCharts = {
    render: render,
    renderAll: renderAll,
    place: place,
    destroy: destroy,
    sparkline: sparkline,
    palette: PALETTE,
    esc: esc,
    formatNumber: defaultFmt,
  };
})(window);
