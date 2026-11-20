/* MediaForge brag series: deterministic timeline helpers (no clocks, no randomness). */
window.MF = {
  // Breathe an element with the music: per-frame energy from assets/audio-env.js,
  // baked into fromTo tweens so every frame is a pure function of timeline time.
  audio(tl, target, opts) {
    const a = window.MF_AUDIO;
    if (!a) return;
    const o = Object.assign({ from: 0, to: a.rms.length / a.fps, base: 0.7, amp: 0.3, scale: 0.04, step: 2 }, opts);
    const val = (i) => {
      const k = Math.min(a.rms.length - 1, Math.max(0, i));
      return 0.55 * a.rms[k] + 0.45 * a.bass[k];
    };
    const f0 = Math.round(o.from * a.fps);
    const f1 = Math.round(o.to * a.fps);
    for (let f = f0; f < f1; f += o.step) {
      const v0 = val(f);
      const v1 = val(f + o.step);
      tl.fromTo(
        target,
        { opacity: o.base + o.amp * v0, scale: 1 + o.scale * v0 },
        { opacity: o.base + o.amp * v1, scale: 1 + o.scale * v1, duration: o.step / a.fps, ease: "none", immediateRender: false },
        f / a.fps,
      );
    }
  },

  // Move the cursor to (x, y) so it arrives at time t, then press and release.
  click(tl, cursor, ripple, x, y, t, travel) {
    const d = travel || 0.55;
    tl.to(cursor, { x: x, y: y, duration: d, ease: "power3.inOut" }, t - d);
    tl.to(cursor, { scale: 0.82, duration: 0.08, ease: "power2.in" }, t);
    tl.to(cursor, { scale: 1, duration: 0.18, ease: "power3.out" }, t + 0.08);
    if (ripple) {
      tl.set(ripple, { x: x + 10, y: y + 8 }, t);
      tl.fromTo(ripple, { scale: 0.3, opacity: 0.9 }, { scale: 1.3, opacity: 0, duration: 0.45, ease: "power2.out", immediateRender: false }, t);
    }
  },

  // Type text into an element one character at a time.
  type(tl, el, text, t, cps) {
    const step = 1 / (cps || 28);
    for (let i = 1; i <= text.length; i++) tl.set(el, { textContent: text.slice(0, i) }, t + (i - 1) * step);
    return t + text.length * step;
  },

  // Blur-in of a whole line.
  blurIn(tl, el, t, d) {
    tl.fromTo(el, { opacity: 0, filter: "blur(14px)" }, { opacity: 1, filter: "blur(0px)", duration: d || 0.22, ease: "power3.out" }, t);
  },

  // Per-word blur-in for a line made of <span class="w"> children.
  words(tl, sel, t, gap) {
    tl.fromTo(sel, { opacity: 0, filter: "blur(10px)", y: 18 },
      { opacity: 1, filter: "blur(0px)", y: 0, duration: 0.26, ease: "power3.out", stagger: gap || 0.08 }, t);
  },

  // Blur-out exit.
  out(tl, el, t, d) {
    tl.to(el, { opacity: 0, filter: "blur(12px)", duration: d || 0.18, ease: "power2.in" }, t);
  },
};
