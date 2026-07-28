// ============================================================
// MediaForge Player
//
// Custom HLS player with overlay controls. Everything is drawn on top of
// the picture (see static/player.css); the container never reserves a
// strip below the video.
//
// Playback paths, in the order they are tried:
//   1. Direct Play + passthrough proxy — the provider's own HLS, no ffmpeg
//   2. Direct Play + transcode         — ffmpeg re-encodes the provider URL
//   3. Library file + transcode/remux  — ffmpeg reads a local file
//
// Positions are handled in TWO time bases and mixing them up is the classic
// bug here:
//   file time   = position inside the media          (what the user sees)
//   stream time = position inside the current ffmpeg output
//   file = stream + _streamStart
// In proxy mode _streamStart is always 0 because the provider serves the
// whole VOD playlist.
// ============================================================
(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────
  var _token       = null;
  var _filePath    = null;
  var _duration    = 0;      // total media duration (file time)
  var _startPos    = 0;      // requested resume position (file time)
  var _streamStart = 0;      // ffmpeg -ss offset of the running session
  var _hls         = null;
  var _saveTimer   = null;
  var _badgePoll   = null;
  var _uiRaf       = null;
  var _uiLastTick  = 0;
  var _seeking     = false;
  var _closed      = true;

  // Direct Play (stream straight from a provider, nothing downloaded)
  var _sourceMode  = false;
  var _proxyMode   = false;
  var _proxyToken  = null;
  var _srcEpisodeUrl = null;
  var _srcProvider   = null;
  var _srcLanguage   = null;
  var _srcTitle      = null;
  var _srcMatrix     = null;   // {language: [provider, ...]}
  var _srcRows       = [];     // flattened + probed source list
  var _srcProbing    = false;
  var _srcFailover   = null;   // pending auto-switch timer

  // Media capabilities of the running session
  var _audioTracks = [];       // [{index, label, language, codec, channels}]
  var _subTracks   = [];       // [{index, label, language, codec, burn}]
  var _qualities   = [];       // [{id, label, height}]
  var _audioSel    = 0;        // index into _audioTracks (server-side stream idx)
  var _subSel      = -1;       // -1 = off
  var _qualitySel  = 'auto';
  var _speed       = 1;
  var _burnedSub   = -1;       // subtitle currently burned into the video

  // Subtitles rendered by us
  var _subSource   = null;     // 'file' | 'hls' | null
  var _cues        = [];       // [{start, end, text}] in FILE time
  var _cueIdx      = -1;
  var _hlsSubTrack = null;

  // Chapters / markers / preview thumbnails
  var _chapters    = [];       // [{start, end, title}]
  var _markers     = [];       // [{start, end, kind, label}] — intro/outro
  var _activeMarker = null;
  var _thumbs      = null;     // {url, interval, cols, rows, w, h, count}
  var _thumbTimer  = null;
  var _thumbTries  = 0;

  // Up next
  var _next        = null;     // {url, title, poster, language, provider, path}
  var _nextTimer   = null;
  var _nextLeft    = 0;
  var _nextCancelled = false;
  var _nextDone      = false;   // countdown already ran; do not restart it

  // UI helpers
  var _skipAcc     = 0;
  var _skipTimer   = null;
  var _skipDir     = 0;
  var _idleTimer   = null;
  var _menuView    = null;     // null | 'settings' | 'quality' | 'audio' | 'subs' | 'speed' | 'source' | 'capstyle'
  var _hudTimer    = null;
  var _dim         = 0;        // 0..0.85 brightness veil

  var SAVE_INTERVAL = 5000;
  var IDLE_MS       = 3000;
  var NEXT_LEAD     = 25;      // show "up next" this many seconds before the end
  var PREFS_KEY     = 'mfPlayerPrefs';

  var _prefs = {
    volume: 1, muted: false, autoplayNext: true,
    capSize: 1, capBox: false, subLang: '', audioLang: '',
  };

  // ── DOM helpers ────────────────────────────────────────────
  function $id(id) { return document.getElementById(id); }
  function _tr(de, en) { return (typeof window.t === 'function') ? window.t(de, en) : en; }
  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }
  function _show(el, on) { if (el) el.classList.toggle('is-on', !!on); }

  function _fmt(s) {
    s = Math.max(0, Math.floor(s || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + ':' : '') +
           (h ? String(m).padStart(2, '0') : m) + ':' +
           String(sec).padStart(2, '0');
  }

  function _loadPrefs() {
    try {
      var raw = localStorage.getItem(PREFS_KEY);
      if (raw) { var o = JSON.parse(raw); for (var k in o) if (k in _prefs) _prefs[k] = o[k]; }
    } catch (e) { /* private mode / disabled storage — defaults are fine */ }
  }
  function _savePrefs() {
    try { localStorage.setItem(PREFS_KEY, JSON.stringify(_prefs)); } catch (e) {}
  }
  _loadPrefs();

  // ── Public API ─────────────────────────────────────────────

  /**
   * Play a file from the library.
   * opts: {subtitle, next, poster, chapters}
   */
  window.openPlayer = function (filePath, title, startPos, opts) {
    opts = opts || {};
    _resetSession();
    _sourceMode = false;
    _filePath   = filePath;
    _srcTitle   = title || String(filePath).split(/[\\/]/).pop();
    _setTitle(_srcTitle, opts.subtitle || '');
    _setNextUp(opts.next || _resolveNext({ path: filePath }));
    _playerShow();
    var resume = Math.floor(startPos || 0);
    if (resume > 5) _showResumeChoice(resume); else _beginPlayback();
  };

  /**
   * Direct Play: stream an episode straight from its provider.
   * ``matrix`` is the full {language: [provider,...]} map (same shape as
   * /api/providers) and powers the source picker. The older positional
   * langOptions/providerOptions are still accepted so existing callers and
   * third-party modules keep working.
   */
  window.openStreamSource = function (episodeUrl, title, provider, language, startPos,
                                      langOptions, providerOptions, matrix) {
    _resetSession();
    _sourceMode    = true;
    _srcEpisodeUrl = episodeUrl;
    _srcProvider   = provider || 'VOE';
    _srcLanguage   = language || 'German Dub';
    _srcTitle      = title || 'Stream';
    _filePath      = episodeUrl;

    _srcMatrix = matrix || null;
    if (!_srcMatrix && langOptions && langOptions.length) {
      // Degraded shape: we only know the hosters of the selected language.
      _srcMatrix = {};
      langOptions.forEach(function (l) {
        _srcMatrix[l] = (l === _srcLanguage && providerOptions && providerOptions.length)
          ? providerOptions.slice() : [];
      });
    }
    _mirrorLegacySelects(langOptions, providerOptions);
    _buildSourceRows();
    _setTitle(_srcTitle, '');
    _setNextUp(_resolveNext({ url: episodeUrl, language: _srcLanguage, provider: _srcProvider }));
    _updateSourceBadge();
    _playerShow();
    var resume = Math.floor(startPos || 0);
    if (resume > 5) _showResumeChoice(resume); else _beginPlayback();

    // The matrix may still be missing (older callers) — fetch it so the
    // picker is complete either way.
    if (!matrix) _fetchSourceMatrix();
    _fetchMarkers(episodeUrl);
  };

  window.closePlayer = function () {
    _playerStop();
    _playerHide();
  };

  /**
   * Re-resolve the Direct Play stream. Called with explicit values by the
   * source picker, and without arguments by the legacy <select>s.
   */
  window._playerChangeSource = function (language, provider) {
    if (!_sourceMode) return;
    var ls = $id('playerLangSelect'), ps = $id('playerProviderSelect');
    var nextLang = language || (ls && ls.value) || _srcLanguage;
    var nextProv = provider || (ps && ps.value) || _srcProvider;
    if (nextLang === _srcLanguage && nextProv === _srcProvider && _hls) return;

    var pos = _filePos();
    // Track indices belong to the stream that is being left behind. In proxy
    // mode they are hls.js rendition/track numbers ("hls:3"), which the
    // transcoder would either reject or misread as an ffmpeg map index.
    _qualitySel = 'auto'; _audioSel = 0; _subSel = -1; _burnedSub = -1;
    _qualities = []; _audioTracks = []; _subTracks = [];
    _clearCaptions();
    _srcLanguage = nextLang;
    _srcProvider = nextProv;
    _mirrorLegacySelects();
    _updateSourceBadge();
    _closeMenu();

    var sw = $id('playerSwitching');
    _setText('playerSwitchTitle', _tr('Wechsel zu ', 'Switching to ') + nextProv);
    _setText('playerSwitchSub', nextLang);
    _setText('playerSwitchNote',
             _tr('Position ', 'Position ') + _fmt(pos) + _tr(' wird übernommen', ' is kept'));
    _show(sw, true);

    _playerStop();
    _startPos = pos;
    _beginPlayback();
  };

  window._playerResume    = function () { _startPos = window._playerPendingResume || 0; _beginPlayback(); };
  window._playerStartOver = function () { _startPos = 0; _beginPlayback(); };
  window._playerTogglePlay = _togglePlay;
  window._playerToggleMute = _toggleMute;
  window._playerSetVolume  = _setVolume;
  window._playerFullscreen = _toggleFullscreen;
  window._playerSkip       = _skip;
  window._playerToggleMenu = _toggleMenu;
  window._playerTogglePip  = _togglePip;
  window._playerSkipMarker = _skipMarker;
  window._playerPlayNext   = _playNext;
  window._playerCancelNext = _cancelNext;

  // Namespaced surface for third-party modules (see .examples/thirdparties).
  window.MFPlayer = {
    open:        window.openPlayer,
    openSource:  window.openStreamSource,
    close:       window.closePlayer,
    skip:        _skip,
    setNextUp:   _setNextUp,
    setChapters: function (list) { _chapters = _normChapters(list); _renderChapterMask(); },
    setMarkers:  function (list) { _markers = _normMarkers(list); },
    getState:    function () {
      return {
        open: !_closed, position: _filePos(), duration: _duration,
        paused: !$id('playerVideo') || $id('playerVideo').paused,
        sourceMode: _sourceMode, provider: _srcProvider, language: _srcLanguage,
        audio: _audioSel, subtitle: _subSel, quality: _qualitySel, speed: _speed,
      };
    },
  };

  // ── SyncPlay hooks ─────────────────────────────────────────
  window.playerGetMediaState = function () {
    var v = $id('playerVideo');
    if (!v) return null;
    return { position: _filePos(), paused: !!v.paused };
  };
  window.playerApplyRemoteState = function (action, position, paused) {
    var v = $id('playerVideo');
    if (!v) return;
    if (action === 'play')       { v.play().catch(function () {}); }
    else if (action === 'pause') { v.pause(); }
    else if (action === 'seek' && typeof position === 'number') {
      _seekToFile(position, true);
    } else if (action === 'sync' && typeof position === 'number') {
      if (Math.abs(_filePos() - position) > 2.5) _seekToFile(position, true);
      if (typeof paused === 'boolean') {
        if (paused && !v.paused) v.pause();
        else if (!paused && v.paused) v.play().catch(function () {});
      }
    }
  };

  // ── Session lifecycle ──────────────────────────────────────
  function _resetSession() {
    _proxyMode = false; _proxyToken = null; _token = null;
    _streamStart = 0; _duration = 0; _startPos = 0;
    _audioTracks = []; _subTracks = []; _qualities = [];
    _audioSel = 0; _subSel = -1; _qualitySel = 'auto'; _burnedSub = -1;
    _chapters = []; _markers = []; _activeMarker = null;
    _thumbs = null; _thumbTries = 0;
    _stopThumbPoll();
    _cues = []; _cueIdx = -1; _subSource = null; _hlsSubTrack = null;
    _srcRows = []; _srcProbing = false;
    // Direct Play identity too: without this, opening a library file after a
    // stream left the source badge pointing at the previous episode -- and
    // clicking it fired real probe requests for it.
    _srcEpisodeUrl = null; _srcProvider = null; _srcLanguage = null; _srcMatrix = null;
    _next = null; _nextCancelled = false; _nextDone = false;
    _speed = 1;
    _cancelFailover();
    _stopNextTimer();
  }

  function _playerShow() {
    _closed = false;
    var o = $id('playerOverlay'); if (o) o.style.display = 'flex';
    try { document.body.classList.add('player-open'); } catch (e) {}
    _applyPrefsToUi();
    _bindOnce();
    _updateSourceBadge();
    _showControls();
    _renderRailLabels();
  }

  function _playerHide() {
    _closed = true;
    var o = $id('playerOverlay'); if (o) o.style.display = 'none';
    try { document.body.classList.remove('player-open'); } catch (e) {}
    _closeMenu();
    _stopUI();
    _cleanupHls();
    _clearCaptions();
    var v = $id('playerVideo');
    if (v) { v.pause(); v.removeAttribute('src'); v.load(); v.playbackRate = 1; }
    var c = $id('playerContainer'); if (c) c.classList.remove('is-idle');
    _clearVideoAspect();
    _setDim(0);
  }

  function _beginPlayback() {
    _show($id('playerResumeChoice'), false);
    _show($id('playerError'), false);
    _setState('loading');
    _playerStart();
  }

  function _showResumeChoice(resumeSec) {
    _show($id('playerSpinner'), false);
    _show($id('playerError'), false);
    window._playerPendingResume = resumeSec;
    _setText('playerResumeAt', _tr('Du warst bei ', 'You were at ') + _fmt(resumeSec));
    _setText('playerResumeLabel',
             _tr('Bei ' + _fmt(resumeSec) + ' fortsetzen', 'Resume at ' + _fmt(resumeSec)));
    _show($id('playerResumeChoice'), true);
  }

  // ── State / messages ───────────────────────────────────────
  function _setText(id, txt) { var e = $id(id); if (e) e.textContent = txt; }

  function _setTitle(title, sub) {
    _setText('playerTitle', title || '');
    var s = $id('playerSubtitle');
    if (s) { s.textContent = sub || ''; s.style.display = sub ? '' : 'none'; }
  }

  function _setState(state) {
    _show($id('playerSpinner'), state === 'loading');
    if (state !== 'error') _show($id('playerError'), false);
    if (state === 'playing') _show($id('playerSwitching'), false);
  }

  function _setSpinnerMsg(msg) { _setText('playerSpinnerMsg', msg); }

  function _setEncoderInfo(enc, isHw) {
    var e = $id('playerEncoderInfo'); if (!e) return;
    e.textContent = (enc || '–') + (isHw ? ' ⚡' : '');
    e.title = isHw ? _tr('Hardware-Encoder', 'Hardware encoder')
                   : _tr('Software-Encoder (CPU)', 'Software encoder (CPU)');
  }

  /**
   * Errors always offer a way on. In Direct Play the way on is the next
   * best source, which is the whole reason the picker exists.
   */
  function _setError(msg, opts) {
    opts = opts || {};
    _setState('error');
    _show($id('playerSpinner'), false);
    _show($id('playerSwitching'), false);
    _setText('playerErrorTitle', opts.title || _tr('Wiedergabe fehlgeschlagen', 'Playback failed'));
    _setText('playerErrorMsg', msg || '');
    var box = $id('playerErrorActions');
    if (box) {
      var html = '';
      var alt = opts.offerSource !== false ? _nextBestSource() : null;
      if (alt) {
        html += '<button class="mfp-btn" id="playerErrSwitch">' +
                _esc(_tr('Zu ', 'Switch to ') + alt.provider) +
                ' <small id="playerErrCount"></small></button>';
      }
      if (_sourceMode) {
        html += '<button class="mfp-btn is-ghost" onclick="_playerToggleMenu(\'source\')">' +
                _esc(_tr('Quellenliste', 'Source list')) + '</button>';
      }
      html += '<button class="mfp-btn is-ghost" onclick="closePlayer()">' +
              _esc(_tr('Schließen', 'Close')) + '</button>';
      box.innerHTML = html;
      var sw = $id('playerErrSwitch');
      if (sw && alt) {
        sw.addEventListener('click', function () {
          _cancelFailover();
          window._playerChangeSource(alt.language, alt.provider);
        });
        _startFailover(alt);
      }
    }
    _show($id('playerError'), true);
  }

  // ── Start / stop ───────────────────────────────────────────
  async function _playerStart() {
    // The passthrough proxy hands the provider's own HLS to the browser --
    // no ffmpeg, no CPU. It can therefore not downscale or re-map audio, so
    // a non-default choice has to take the transcoder route instead.
    var needsFfmpeg = (_qualitySel !== 'auto') || (_audioSel > 0);
    if (_sourceMode && !needsFfmpeg) {
      var proxied = await _tryProxy();
      if (proxied) return;
    }
    return _startTranscode();
  }

  async function _tryProxy() {
    try {
      _setSpinnerMsg(_tr('Stream wird vorbereitet…', 'Preparing stream…'));
      var resp = await fetch('/api/stream/start-proxy', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          episode_url: _srcEpisodeUrl, provider: _srcProvider, language: _srcLanguage,
        }),
      });
      var data = await resp.json();
      if (!resp.ok || data.error || !data.hls || !data.playlist_url) return false;
      _proxyMode   = true;
      _proxyToken  = data.token || null;
      _token       = null;
      _streamStart = 0;
      _duration    = 0;
      _setEncoderInfo('direct', false);
      _applyServerMedia(data);
      _loadHls(data.playlist_url, 0);
      _startSaveTimer();
      return true;
    } catch (e) {
      return false;
    }
  }

  async function _startTranscode() {
    _proxyMode = false;
    try {
      var chk  = await fetch('/api/stream/check');
      var chkD = await chk.json();
      if (!chkD.available) {
        _setError(_tr('Kein Encoder: ', 'No encoder: ') + (chkD.reason || 'ffmpeg'),
                  { offerSource: false });
        return;
      }
      _setEncoderInfo(chkD.encoder, chkD.is_hardware);

      var body, url;
      if (_sourceMode) {
        _setSpinnerMsg(_tr('Stream wird aufgelöst…', 'Resolving stream…'));
        url  = '/api/stream/start-source';
        body = {
          episode_url: _srcEpisodeUrl, provider: _srcProvider,
          language: _srcLanguage, start_pos: _startPos,
        };
      } else {
        _setSpinnerMsg(_tr('Transcoding wird gestartet…', 'Transcoding is starting…'));
        url  = '/api/stream/start';
        body = { path: _filePath, start_pos: _startPos,
                 syncplay_token: window.__syncplayToken || undefined };
      }
      body.audio_index = _audioSel || 0;
      body.quality     = _qualitySel;
      if (_burnedSub >= 0) body.burn_subtitle = _burnedSub;

      var resp = await fetch(url, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      var data = await resp.json();
      if (!resp.ok || data.error) {
        _setError(data.error || _tr('Transcoding fehlgeschlagen', 'Transcoding failed'));
        return;
      }
      _token       = data.token;
      _duration    = data.duration || 0;
      _streamStart = data.start_pos || 0;
      _setEncoderInfo(data.encoder, data.is_hardware);
      _applyServerMedia(data);

      _setSpinnerMsg(_tr('Erste Segmente werden generiert…', 'Generating first segments…'));
      var ready = await _waitForStream(_token, 90);
      if (!ready) return;

      _loadHls('/api/stream/' + _token + '/index.m3u8', _streamStart);
      _startSaveTimer();
      _startBadgePoll();
    } catch (err) {
      _setError(_tr('Netzwerkfehler: ', 'Network error: ') + err.message);
    }
  }

  /** Take over the track/chapter/marker info a start response carries. */
  function _applyServerMedia(data) {
    if (Array.isArray(data.audio_tracks) && data.audio_tracks.length) {
      _audioTracks = data.audio_tracks;
      if (!_audioTracks.some(function (a) { return a.index === _audioSel; })) {
        _audioSel = _pickPreferred(_audioTracks, _prefs.audioLang);
      }
    }
    if (Array.isArray(data.subtitle_tracks)) _subTracks = data.subtitle_tracks;
    if (Array.isArray(data.qualities))       _qualities = data.qualities;
    if (Array.isArray(data.chapters) && data.chapters.length) {
      _chapters = _normChapters(data.chapters);
    }
    if (Array.isArray(data.markers) && data.markers.length) {
      _markers = _normMarkers(data.markers);
    }
    if (data.duration && data.duration > _duration) _duration = data.duration;
    _renderChapterMask();
    _renderRailLabels();
    _maybeAutoSubtitle();
    _loadThumbs();
  }

  function _pickPreferred(list, wantLang) {
    if (!list.length) return 0;
    if (wantLang) {
      for (var i = 0; i < list.length; i++) {
        if ((list[i].language || '').toLowerCase() === wantLang.toLowerCase()) return list[i].index;
      }
    }
    for (var j = 0; j < list.length; j++) if (list[j].default) return list[j].index;
    return list[0].index;
  }

  /** Turn the subtitles on automatically when the user had them on before. */
  function _maybeAutoSubtitle() {
    if (_subSel >= 0 || !_prefs.subLang || !_subTracks.length) return;
    for (var i = 0; i < _subTracks.length; i++) {
      // Never auto-select a burn-in track: that would restart ffmpeg on its
      // own right after the stream came up.
      if (_subTracks[i].burn) continue;
      if ((_subTracks[i].language || '').toLowerCase() === _prefs.subLang.toLowerCase()) {
        _selectSubtitle(_subTracks[i].index, true);
        return;
      }
    }
  }

  function _playerStop() {
    // Both of these can fire AFTER the player is gone and start a fresh
    // session against a hidden overlay: the failover timer calls
    // _playerChangeSource, the skip timer calls _seekToFile.
    _cancelFailover();
    if (_skipTimer) { clearTimeout(_skipTimer); _skipTimer = null; _skipAcc = 0; _skipDir = 0; }
    _stopThumbPoll();
    _stopSaveTimer();
    _stopBadgePoll();
    _stopUI();
    _stopNextTimer();
    _saveProgress().catch(function () {});
    _cleanupHls();
    _clearCaptions();
    if (_token) {
      var tok = _token; _token = null;
      fetch('/api/stream/stop', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: tok }),
      }).catch(function () {});
    }
    if (_proxyToken) {
      var ptok = _proxyToken; _proxyToken = null;
      fetch('/api/stream/close-proxy', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: ptok }),
      }).catch(function () {});
    }
    _proxyMode = false;
    _updateStreamBadge(0);
  }

  async function _waitForStream(token, timeoutSec) {
    var deadline = Date.now() + timeoutSec * 1000;
    while (Date.now() < deadline) {
      if (_closed || _token !== token) return false;
      try {
        var r  = await fetch('/api/stream/' + token + '/status');
        var st = await r.json();
        if (st.ready) return true;
        if (!st.alive) {
          _setError(_tr('Encoder-Fehler: ', 'Encoder error: ') + (st.error || 'ffmpeg'));
          return false;
        }
        if (st.stderr_tail) console.debug('[Player] ffmpeg:', st.stderr_tail);
      } catch (e) {}
      await new Promise(function (res) { setTimeout(res, 800); });
    }
    _setError(_tr('Zeitüberschreitung: Stream startet nicht.',
                  'Timeout: stream is not starting.'));
    return false;
  }

  // ── hls.js ─────────────────────────────────────────────────
  function _bufferedAhead(video, fromPos) {
    if (!video || !video.buffered) return 0;
    for (var i = 0; i < video.buffered.length; i++) {
      if (video.buffered.start(i) <= fromPos + 0.25 && video.buffered.end(i) >= fromPos) {
        return video.buffered.end(i) - fromPos;
      }
    }
    return 0;
  }

  function _prebufferThenPlay(video, seekTarget) {
    if (!_sourceMode) { _setState('playing'); video.play().catch(function () {}); return; }
    var TARGET = 12, deadline = Date.now() + 12000;
    _setSpinnerMsg(_tr('Puffer wird aufgebaut…', 'Buffering…'));
    _setState('loading');
    (function _wait() {
      if (_closed) return;
      var ahead = _bufferedAhead(video, video.currentTime || seekTarget || 0);
      if (ahead >= TARGET || Date.now() > deadline) {
        _setState('playing');
        video.play().catch(function () {});
        return;
      }
      setTimeout(_wait, 400);
    })();
  }

  function _loadHls(url, streamStartPos) {
    var video = $id('playerVideo');
    if (!video) return;
    _cleanupHls();

    if (typeof Hls !== 'undefined' && Hls.isSupported()) {
      _hls = new Hls({
        lowLatencyMode:              false,
        maxBufferLength:             60,
        maxMaxBufferLength:          240,
        backBufferLength:            30,
        maxBufferHole:               0.5,
        highBufferWatchdogPeriod:    1,
        nudgeMaxRetry:               10,
        maxFragLookUpTolerance:      0.5,
        enableWorker:                true,
        startFragPrefetch:           true,
        progressive:                 true,
        liveSyncDurationCount:       9999,
        liveMaxLatencyDurationCount: 99999,
        liveDurationInfinity:        true,
        startPosition:               0,
        debug:                       false,
      });

      _hls.loadSource(url);
      _hls.attachMedia(video);

      _hls.on(Hls.Events.MANIFEST_PARSED, function () {
        _setState('playing');
        if (_duration > 0) _trySetMsDuration(_hls, _duration, 15);
        _collectHlsTracks();
        var seekTarget = Math.max(0, _startPos - streamStartPos);
        var start = function () { _prebufferThenPlay(video, seekTarget); };
        if (seekTarget > 2) {
          video.currentTime = seekTarget;
          video.addEventListener('seeked', function onS() {
            video.removeEventListener('seeked', onS);
            start();
          });
        } else { start(); }
        _applyVideoAspect();
        _startUI();
      });

      _hls.on(Hls.Events.LEVEL_UPDATED, function (ev, d) {
        if (d && d.details && d.details.totalduration > _duration) {
          _duration = d.details.totalduration;
        }
      });
      _hls.on(Hls.Events.LEVEL_SWITCHED, function () {
        _renderRailLabels();
        _applyVideoAspect();
      });

      var retries = 0;
      _hls.on(Hls.Events.ERROR, function (ev, data) {
        if (!data.fatal) return;
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR && retries < 4) {
          retries++;
          setTimeout(function () { if (_hls) _hls.startLoad(); }, 1200);
        } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR && retries < 6) {
          retries++;
          try { _hls.recoverMediaError(); } catch (e) {
            _setError(_tr('Stream-Fehler: ', 'Stream error: ') + (data.details || data.type));
          }
        } else {
          _setError(_tr('Stream-Fehler: ', 'Stream error: ') + (data.details || data.type));
        }
      });

    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url;
      video.addEventListener('loadedmetadata', function () {
        _setState('playing');
        video.play().catch(function () {});
        _startUI();
      }, { once: true });
      video.addEventListener('error', function () {
        _setError(_tr('Video konnte nicht geladen werden.', 'Video could not be loaded.'));
      }, { once: true });
    } else {
      _setError(_tr('Browser unterstützt kein HLS.', 'Browser does not support HLS.'),
                { offerSource: false });
    }

    _applyPrefsToVideo();
  }

  function _cleanupHls() {
    if (_hls) { try { _hls.destroy(); } catch (e) {} _hls = null; }
  }

  /** In proxy mode the provider's own renditions/tracks are the real ones. */
  function _collectHlsTracks() {
    if (!_hls) return;
    if (_proxyMode) {
      var lv = _hls.levels || [];
      if (lv.length > 1) {
        _qualities = [{ id: 'auto', label: _tr('Automatisch', 'Automatic'), height: 0 }];
        lv.forEach(function (l, i) {
          _qualities.push({ id: 'hls:' + i, label: (l.height ? l.height + 'p' : (Math.round((l.bitrate || 0) / 1000) + ' kbit/s')), height: l.height || 0 });
        });
      }
      var at = _hls.audioTracks || [];
      if (at.length > 1) {
        _audioTracks = at.map(function (a, i) {
          return { index: i, label: a.name || a.lang || ('Audio ' + (i + 1)), language: a.lang || '', hls: true };
        });
        _audioSel = _hls.audioTrack >= 0 ? _hls.audioTrack : 0;
      }
      var st = _hls.subtitleTracks || [];
      if (st.length) {
        _subTracks = st.map(function (s, i) {
          return { index: i, label: s.name || s.lang || ('Sub ' + (i + 1)), language: s.lang || '', hls: true };
        });
      }
    }
    _renderRailLabels();
    _maybeAutoSubtitle();
  }

  function _trySetMsDuration(hlsInstance, dur, attempts) {
    if (!hlsInstance || attempts <= 0) return;
    try {
      var ms = (hlsInstance.streamController && hlsInstance.streamController.mediaSource)
            || (hlsInstance.bufferController && hlsInstance.bufferController.mediaSource);
      if (ms && ms.readyState === 'open' && isFinite(dur) && dur > 0) {
        if (Math.abs((ms.duration || 0) - dur) > 2) ms.duration = dur;
        return;
      }
    } catch (e) {}
    setTimeout(function () { _trySetMsDuration(hlsInstance, dur, attempts - 1); }, 300);
  }

  /**
   * Stop the ffmpeg session and start a new one at ``filePos``.
   * ``extra`` overrides go into the start request (audio track, quality,
   * burned-in subtitle), which is how those settings are actually applied:
   * ffmpeg cannot switch them inside a running output.
   */
  async function _restartFromPosition(filePos, extra) {
    if (_sourceMode) {
      // Direct Play re-resolves the provider URL instead of touching a file.
      _startPos = filePos;
      _playerStop();
      _beginPlayback();
      return;
    }
    if (!_filePath) return;
    extra = extra || {};

    _setState('loading');
    _setSpinnerMsg(_tr('Springe zu ', 'Seeking to ') + _fmt(filePos) + '…');
    _stopSaveTimer(); _stopBadgePoll(); _stopUI(); _cleanupHls();

    var oldToken = _token;
    _token = null;
    if (oldToken) {
      fetch('/api/stream/stop', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ token: oldToken }),
      }).catch(function () {});
    }

    try {
      var body = {
        path: _filePath, start_pos: filePos,
        syncplay_token: window.__syncplayToken || undefined,
        audio_index: ('audio_index' in extra) ? extra.audio_index : (_audioSel || 0),
        quality:     ('quality' in extra)     ? extra.quality     : _qualitySel,
      };
      var burn = ('burn_subtitle' in extra) ? extra.burn_subtitle : _burnedSub;
      if (burn >= 0) body.burn_subtitle = burn;

      var resp = await fetch('/api/stream/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      var data = await resp.json();
      if (!resp.ok || data.error) {
        _setError(data.error || _tr('Neustart fehlgeschlagen', 'Restart failed'));
        return;
      }
      _token       = data.token;
      _streamStart = data.start_pos || 0;
      if (data.duration > 0) _duration = data.duration;
      _startPos = filePos;
      _applyServerMedia(data);

      var ready = await _waitForStream(_token, 90);
      if (!ready) return;
      _loadHls('/api/stream/' + _token + '/index.m3u8', _streamStart);
      _startSaveTimer();
      _startBadgePoll();
    } catch (err) {
      _setError(_tr('Netzwerkfehler beim Neustart: ', 'Network error on restart: ') + err.message);
    }
  }

  // ── Position helpers ───────────────────────────────────────
  function _filePos() {
    var v = $id('playerVideo');
    return v ? (v.currentTime || 0) + _streamStart : 0;
  }

  /**
   * Seek in FILE time. Inside the encoded range this is a plain
   * currentTime assignment; outside it ffmpeg has to be restarted, which
   * is why seeking backwards past the session start is expensive.
   */
  function _seekToFile(fileTarget, silent) {
    var video = $id('playerVideo');
    if (!video) return;
    fileTarget = Math.max(0, _duration ? Math.min(fileTarget, _duration - 0.5) : fileTarget);
    if (!silent && window._spOnUserSeek) {
      try { window._spOnUserSeek(fileTarget); } catch (e) {}
    }
    if (_proxyMode) { video.currentTime = fileTarget; return; }

    var streamTarget = fileTarget - _streamStart;
    var maxBuffered  = 0;
    for (var i = 0; i < video.buffered.length; i++) {
      maxBuffered = Math.max(maxBuffered, video.buffered.end(i));
    }
    if (streamTarget >= 0 && streamTarget <= maxBuffered + 12) {
      video.currentTime = Math.min(streamTarget, Math.max(0, maxBuffered));
    } else {
      _restartFromPosition(fileTarget);
    }
  }

  /**
   * Relative jump. Repeated taps within the accumulate window add up and
   * only the SUM is applied — four taps must not trigger four seeks, each
   * of which could restart ffmpeg.
   */
  function _skip(delta) {
    _showControls();
    _skipAcc = (_skipDir === Math.sign(delta)) ? _skipAcc + delta : delta;
    _skipDir = Math.sign(delta);
    _flashRipple(delta < 0 ? 'L' : 'R', Math.abs(_skipAcc));
    if (_skipTimer) clearTimeout(_skipTimer);
    _skipTimer = setTimeout(function () {
      var target = _filePos() + _skipAcc;
      _skipAcc = 0; _skipDir = 0; _skipTimer = null;
      _seekToFile(target);
    }, 320);
  }

  function _flashRipple(side, seconds) {
    var el = $id('playerRipple' + side);
    var tx = $id('playerRipple' + side + 'Text');
    if (tx) tx.textContent = seconds + ' s';
    if (!el) return;
    el.classList.remove('is-on');
    void el.offsetWidth;               // restart the animation
    el.classList.add('is-on');
    setTimeout(function () { el.classList.remove('is-on'); }, 560);
  }

  // ── UI loop ────────────────────────────────────────────────
  function _startUI() {
    _stopUI();
    _uiRaf = requestAnimationFrame(_uiTick);
  }
  function _stopUI() {
    if (_uiRaf) { cancelAnimationFrame(_uiRaf); _uiRaf = null; }
  }

  function _uiTick(now) {
    // ~5 updates/s. Touching the seek bar every frame forces continuous
    // layout and paint, which competes with video decoding.
    if (now && _uiLastTick && (now - _uiLastTick) < 200) {
      _uiRaf = requestAnimationFrame(_uiTick);
      return;
    }
    _uiLastTick = now || 0;
    var video = $id('playerVideo');
    if (video) {
      var filePos = _filePos();
      var total   = _duration || 0;

      var tt = $id('playerTimeText');
      if (tt) tt.innerHTML = '<b>' + _esc(_fmt(filePos)) + '</b> / ' +
                             _esc(total > 0 ? _fmt(total) : '--:--');

      var pct = total > 0 ? Math.min(100, (filePos / total) * 100) : 0;
      if (!_seeking) {
        var fill = $id('playerSeekFill'), thumb = $id('playerSeekThumb');
        if (fill)  fill.style.width = pct + '%';
        if (thumb) thumb.style.left = pct + '%';
        var wrap = $id('playerSeekWrap');
        if (wrap) wrap.setAttribute('aria-valuenow', Math.round(pct));
      }

      var buf = $id('playerSeekBuf');
      if (buf && video.buffered.length > 0) {
        var bufEnd = video.buffered.end(video.buffered.length - 1) + _streamStart;
        buf.style.width = (total > 0 ? Math.min(100, (bufEnd / total) * 100) : 0) + '%';
      }

      _syncPlayIcons(video.paused);
      _syncVolumeIcons(video);
      if (_subSource === 'file') _renderCueAt(filePos);
      _updateMarker(filePos);
      _updateNextUp(filePos, total);
    }
    _uiRaf = requestAnimationFrame(_uiTick);
  }

  function _syncPlayIcons(paused) {
    [['playerPlayIcon', 'playerPauseIcon'], ['playerCenterPlayIcon', 'playerCenterPauseIcon']]
      .forEach(function (pair) {
        var p = $id(pair[0]), q = $id(pair[1]);
        if (p && q) { p.style.display = paused ? '' : 'none'; q.style.display = paused ? 'none' : ''; }
      });
  }

  function _syncVolumeIcons(video) {
    var vi = $id('playerVolIcon'), mi = $id('playerMuteIcon'), vs = $id('playerVolSlider');
    var muted = video.muted || video.volume === 0;
    if (vi && mi) { vi.style.display = muted ? 'none' : ''; mi.style.display = muted ? '' : 'none'; }
    if (!vs) return;
    var level = muted ? 0 : video.volume;
    if (document.activeElement !== vs) vs.value = level;
    // WebKit cannot paint the filled part of a range on its own, so the
    // track reads this variable (see player.css).
    vs.style.setProperty('--mfp-vol', Math.round(level * 100) + '%');
  }

  // ── Controls visibility ────────────────────────────────────
  function _showControls() {
    var c = $id('playerContainer');
    if (c) c.classList.remove('is-idle');
    if (_idleTimer) clearTimeout(_idleTimer);
    _idleTimer = setTimeout(function () {
      var v = $id('playerVideo');
      if (!v || v.paused || _menuView || _closed) return;
      var el = $id('playerContainer');
      if (el) el.classList.add('is-idle');
    }, IDLE_MS);
  }

  function _toggleControls() {
    var c = $id('playerContainer');
    if (!c) return;
    if (c.classList.contains('is-idle')) _showControls();
    else c.classList.add('is-idle');
  }

  // ── Menus ──────────────────────────────────────────────────
  function _toggleMenu(which) {
    if (_menuView === which) { _closeMenu(); return; }
    _menuView = which;
    _renderMenu();
    var m = $id('playerMenu');
    if (m) m.classList.add('is-open');
    _showControls();
    if (which === 'source') _probeSources();
  }

  function _closeMenu() {
    _menuView = null;
    var m = $id('playerMenu');
    if (m) { m.classList.remove('is-open'); m.innerHTML = ''; }
    var b = $id('playerSettingsBtn');
    if (b) b.setAttribute('aria-expanded', 'false');
    var sb = $id('playerSourceBadge');
    if (sb) sb.setAttribute('aria-expanded', 'false');
  }

  function _menuHead(title, note, back) {
    return '<div class="mfp-menu-head">' +
      (back ? '<button class="mfp-menu-back" data-act="menu" data-arg="settings" aria-label="' +
              _esc(_tr('Zurück', 'Back')) + '">' +
              '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M15 18l-6-6 6-6"/></svg></button>' : '') +
      _esc(title) +
      (note ? '<span class="mfp-menu-note">' + _esc(note) + '</span>' : '') +
      '</div>';
  }

  function _row(label, value, act, arg, selected) {
    return '<button class="mfp-menu-item' + (selected ? ' is-sel' : '') + '"' +
           ' data-act="' + _esc(act) + '" data-arg="' + _esc(arg) + '" role="menuitem">' +
           '<span class="mfp-menu-tick">' + (selected ? '✓' : '') + '</span>' +
           '<span>' + _esc(label) + '</span>' +
           (value ? '<span class="mfp-menu-val">' + _esc(value) + '</span>' : '') +
           '</button>';
  }

  function _renderMenu() {
    var m = $id('playerMenu');
    if (!m) return;
    m.classList.toggle('is-wide', _menuView === 'source');
    var h = '';

    if (_menuView === 'settings') {
      h += _menuHead(_tr('Wiedergabe', 'Playback'));
      if (_qualities.length) h += _row(_tr('Qualität', 'Quality'), _qualityLabel(), 'menu', 'quality');
      if (_audioTracks.length > 1) h += _row(_tr('Tonspur', 'Audio track'), _audioLabel(), 'menu', 'audio');
      h += _row(_tr('Untertitel', 'Subtitles'), _subLabel(), 'menu', 'subs');
      h += _row(_tr('Geschwindigkeit', 'Speed'), _speed + '×', 'menu', 'speed');
      if (_subSel >= 0) h += _row(_tr('Untertitel-Darstellung', 'Caption style'), '', 'menu', 'capstyle');
      h += '<div class="mfp-menu-sep"></div>';
      h += '<label class="mfp-menu-item" style="cursor:pointer">' +
           '<input type="checkbox" class="chb-main" data-act="autoplay"' +
           (_prefs.autoplayNext ? ' checked' : '') + '>' +
           '<span>' + _esc(_tr('Nächste Folge automatisch', 'Autoplay next episode')) + '</span></label>';

    } else if (_menuView === 'quality') {
      h += _menuHead(_tr('Qualität', 'Quality'), '', true);
      _qualities.forEach(function (q) {
        h += _row(q.label, q.note || '', 'quality', q.id, q.id === _qualitySel);
      });
      if (!_proxyMode) {
        h += '<div class="mfp-src-foot">' +
             _esc(_tr('Eine andere Qualität startet die Umwandlung neu.',
                      'Changing quality restarts the transcode.')) + '</div>';
      }

    } else if (_menuView === 'audio') {
      h += _menuHead(_tr('Tonspur', 'Audio track'), '', true);
      _audioTracks.forEach(function (a) {
        h += _row(a.label, a.channels ? a.channels : '', 'audio', String(a.index), a.index === _audioSel);
      });

    } else if (_menuView === 'subs') {
      h += _menuHead(_tr('Untertitel', 'Subtitles'), '', true);
      h += _row(_tr('Aus', 'Off'), '', 'sub', '-1', _subSel < 0);
      _subTracks.forEach(function (s) {
        h += _row(s.label, s.burn ? _tr('eingebrannt', 'burned in') : '',
                  'sub', String(s.index), s.index === _subSel);
      });
      if (!_subTracks.length) {
        h += '<div class="mfp-src-foot">' +
             _esc(_tr('Diese Quelle liefert keine Untertitel.',
                      'This source carries no subtitles.')) + '</div>';
      }

    } else if (_menuView === 'speed') {
      h += _menuHead(_tr('Geschwindigkeit', 'Speed'), '', true);
      h += '<div class="mfp-speedrow">';
      [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2].forEach(function (s) {
        h += '<button data-act="speed" data-arg="' + s + '"' +
             (s === _speed ? ' class="is-on"' : '') + '>' + String(s).replace('.', ',') + '×</button>';
      });
      h += '</div>';

    } else if (_menuView === 'capstyle') {
      h += _menuHead(_tr('Untertitel-Darstellung', 'Caption style'), '', true);
      h += '<div class="mfp-speedrow">';
      [['0.8', 'S'], ['1', 'M'], ['1.25', 'L'], ['1.6', 'XL']].forEach(function (p) {
        h += '<button data-act="capsize" data-arg="' + p[0] + '"' +
             (parseFloat(p[0]) === _prefs.capSize ? ' class="is-on"' : '') + '>' + p[1] + '</button>';
      });
      h += '</div>';
      h += '<label class="mfp-menu-item" style="cursor:pointer">' +
           '<input type="checkbox" class="chb-main" data-act="capbox"' +
           (_prefs.capBox ? ' checked' : '') + '>' +
           '<span>' + _esc(_tr('Hintergrundbalken', 'Background box')) + '</span></label>';

    } else if (_menuView === 'source') {
      h += _renderSourceMenu();
    }

    m.innerHTML = h;
    m.querySelectorAll('[data-act]').forEach(function (el) {
      var ev = (el.type === 'checkbox') ? 'change' : 'click';
      el.addEventListener(ev, function (e) {
        e.stopPropagation();
        _menuAction(el.getAttribute('data-act'), el.getAttribute('data-arg'), el);
      });
    });
  }

  function _menuAction(act, arg, el) {
    switch (act) {
      case 'menu':     _menuView = arg; _renderMenu(); break;
      case 'quality':  _selectQuality(arg); break;
      case 'audio':    _selectAudio(parseInt(arg, 10)); break;
      case 'sub':      _selectSubtitle(parseInt(arg, 10)); break;
      case 'speed':    _setSpeed(parseFloat(arg)); break;
      case 'autoplay': _prefs.autoplayNext = !!el.checked; _savePrefs(); break;
      case 'capsize':  _prefs.capSize = parseFloat(arg); _savePrefs(); _applyCaptionStyle(); _renderMenu(); break;
      case 'capbox':   _prefs.capBox = !!el.checked; _savePrefs(); _applyCaptionStyle(); break;
      case 'source':   _closeMenu(); _selectSourceRow(parseInt(arg, 10)); break;
      case 'rescan':   _buildSourceRows(true); _renderMenu(); _probeSources(true); break;
    }
    _showControls();
  }

  // ── Track selection ────────────────────────────────────────
  function _qualityLabel() {
    for (var i = 0; i < _qualities.length; i++) {
      if (_qualities[i].id === _qualitySel) return _qualities[i].label;
    }
    return _tr('Automatisch', 'Automatic');
  }
  function _audioLabel() {
    for (var i = 0; i < _audioTracks.length; i++) {
      if (_audioTracks[i].index === _audioSel) return _audioTracks[i].label;
    }
    return '';
  }
  function _subLabel() {
    if (_subSel < 0) return _tr('Aus', 'Off');
    for (var i = 0; i < _subTracks.length; i++) {
      if (_subTracks[i].index === _subSel) return _subTracks[i].label;
    }
    return _tr('Aus', 'Off');
  }

  function _selectQuality(id) {
    _qualitySel = id;
    _closeMenu();
    _renderRailLabels();
    if (_proxyMode && _hls) {
      // Real renditions from the provider — hls.js switches them in place.
      _hls.currentLevel = (id === 'auto') ? -1 : parseInt(String(id).split(':')[1], 10);
      return;
    }
    // Transcoded: the only way to change the output size is a new ffmpeg run.
    _restartFromPosition(_filePos(), { quality: id });
  }

  function _selectAudio(index) {
    if (isNaN(index)) return;
    _audioSel = index;
    var t = _audioTracks.filter(function (a) { return a.index === index; })[0];
    if (t && t.language) { _prefs.audioLang = t.language; _savePrefs(); }
    _closeMenu();
    _renderRailLabels();
    if (_proxyMode && _hls) { _hls.audioTrack = index; return; }
    _restartFromPosition(_filePos(), { audio_index: index });
  }

  function _selectSubtitle(index, quiet) {
    var track = _subTracks.filter(function (s) { return s.index === index; })[0];
    _subSel = (index < 0 || !track) ? -1 : index;
    if (!quiet) _closeMenu();
    _prefs.subLang = (_subSel >= 0 && track) ? (track.language || '') : '';
    _savePrefs();
    _renderRailLabels();

    // Image-based subtitles (PGS / VobSub) cannot be turned into text, so
    // ffmpeg has to draw them into the picture — which means a restart.
    var wantBurn = (_subSel >= 0 && track && track.burn) ? _subSel : -1;
    if (wantBurn !== _burnedSub) {
      _burnedSub = wantBurn;
      _clearCaptions();
      _restartFromPosition(_filePos(), { burn_subtitle: _burnedSub });
      return;
    }
    if (_subSel < 0) { _clearCaptions(); return; }
    if (track.hls) _useHlsSubtitle(index);
    else _loadFileSubtitle(track);
  }

  function _setSpeed(rate) {
    _speed = rate;
    var v = $id('playerVideo');
    if (v) v.playbackRate = rate;
    _closeMenu();
    _renderRailLabels();
  }

  function _renderRailLabels() {
    var q = $id('playerQualityLbl');
    if (q) {
      q.style.display = _qualities.length ? '' : 'none';
      var lbl = _qualityLabel();
      if (_qualitySel === 'auto' && _proxyMode && _hls && _hls.levels && _hls.levels[_hls.currentLevel]) {
        lbl = _tr('Auto', 'Auto') + ' ' + (_hls.levels[_hls.currentLevel].height || '') + 'p';
      }
      q.textContent = lbl;
      q.classList.toggle('is-on', _qualitySel !== 'auto');
    }
    var a = $id('playerAudioLbl');
    if (a) {
      a.style.display = _audioTracks.length > 1 ? '' : 'none';
      a.textContent = _audioLabel();
      a.classList.add('is-on');
    }
    var s = $id('playerSubLbl');
    if (s) {
      s.innerHTML = _esc(_tr('UT', 'CC')) + ' <small>' + _esc(_subLabel()) + '</small>';
      s.classList.toggle('is-on', _subSel >= 0);
    }
    var sp = $id('playerSpeedLbl');
    if (sp) {
      sp.textContent = String(_speed).replace('.', ',') + '×';
      sp.classList.toggle('is-on', _speed !== 1);
    }
    var n = $id('playerNextBtn');
    if (n) n.style.display = _next ? '' : 'none';
  }

  // ── Subtitles ──────────────────────────────────────────────
  function _clearCaptions() {
    _cues = []; _cueIdx = -1; _subSource = null;
    if (_hlsSubTrack) {
      try { _hlsSubTrack.removeEventListener('cuechange', _onHlsCue); } catch (e) {}
      _hlsSubTrack = null;
    }
    if (_hls) { try { _hls.subtitleTrack = -1; } catch (e) {} }
    var box = $id('playerCaptions');
    if (box) { box.classList.remove('is-on'); box.innerHTML = ''; }
    // _paintCue skips repaints of identical text; without this, toggling
    // subtitles off and on again during one cue left the box empty.
    _lastCueText = null;
  }

  function _applyCaptionStyle() {
    var box = $id('playerCaptions');
    if (!box) return;
    box.classList.toggle('has-box', !!_prefs.capBox);
    box.style.setProperty('--mfp-cue-size', (1.15 * _prefs.capSize) + 'rem');
    box.style.setProperty('--mfp-cue-size-sm', (0.95 * _prefs.capSize) + 'rem');
  }

  async function _loadFileSubtitle(track) {
    _clearCaptions();
    _subSource = 'file';
    _applyCaptionStyle();
    try {
      var qs = '?path=' + encodeURIComponent(_filePath) + '&track=' + encodeURIComponent(track.index);
      var r = await fetch('/api/stream/subtitle' + qs);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      _cues = _parseVtt(await r.text());
      _cueIdx = -1;
    } catch (e) {
      _subSource = null;
      if (window.showToast) {
        window.showToast(_tr('Untertitel konnten nicht geladen werden.',
                             'Subtitles could not be loaded.'), 'error');
      }
    }
  }

  function _useHlsSubtitle(index) {
    _clearCaptions();
    _subSource = 'hls';
    _applyCaptionStyle();
    if (!_hls) return;
    try { _hls.subtitleTrack = index; _hls.subtitleDisplay = false; } catch (e) {}
    var v = $id('playerVideo');
    if (!v) return;
    // hls.js creates the TextTracks lazily; grab the active one and render
    // its cues ourselves so the look matches the file path.
    setTimeout(function () {
      for (var i = 0; i < v.textTracks.length; i++) {
        var tt = v.textTracks[i];
        if (tt.mode !== 'disabled' || i === index) {
          tt.mode = 'hidden';
          _hlsSubTrack = tt;
          tt.addEventListener('cuechange', _onHlsCue);
          break;
        }
      }
    }, 400);
  }

  function _onHlsCue() {
    if (!_hlsSubTrack) return;
    var act = _hlsSubTrack.activeCues;
    var txt = '';
    for (var i = 0; act && i < act.length; i++) {
      txt += (txt ? '\n' : '') + (act[i].text || '');
    }
    _paintCue(txt);
  }

  /** Minimal WebVTT reader: cue timings + text, tags stripped. */
  function _parseVtt(text) {
    var out = [];
    var blocks = String(text).replace(/\r/g, '').split(/\n\n+/);
    for (var i = 0; i < blocks.length; i++) {
      var lines = blocks[i].split('\n').filter(function (l) { return l.trim() !== ''; });
      if (!lines.length) continue;
      var ti = -1;
      for (var j = 0; j < lines.length; j++) {
        if (lines[j].indexOf('-->') !== -1) { ti = j; break; }
      }
      if (ti < 0) continue;
      var parts = lines[ti].split('-->');
      var start = _vttTime(parts[0]);
      var end   = _vttTime((parts[1] || '').split(/\s+/)[0] || parts[1]);
      if (start == null || end == null) continue;
      var body = lines.slice(ti + 1).join('\n')
        .replace(/<[^>]+>/g, '')
        .replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
      if (body) out.push({ start: start, end: end, text: body });
    }
    out.sort(function (a, b) { return a.start - b.start; });
    return out;
  }

  function _vttTime(s) {
    if (!s) return null;
    var m = String(s).trim().match(/(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})/);
    if (!m) return null;
    return (parseInt(m[1] || 0, 10) * 3600) + (parseInt(m[2], 10) * 60) +
           parseInt(m[3], 10) + (parseInt(m[4], 10) / (m[4].length === 2 ? 100 : 1000));
  }

  function _renderCueAt(filePos) {
    if (!_cues.length) return;
    // Cheap scan from the last index — cues are sorted and playback is
    // mostly linear, so this is O(1) except right after a seek.
    var i = _cueIdx;
    if (i < 0 || i >= _cues.length || _cues[i].start > filePos) i = 0;
    while (i < _cues.length - 1 && _cues[i].end < filePos) i++;
    _cueIdx = i;
    var c = _cues[i];
    _paintCue((c && filePos >= c.start && filePos <= c.end) ? c.text : '');
  }

  var _lastCueText = null;
  function _paintCue(text) {
    if (text === _lastCueText) return;
    _lastCueText = text;
    var box = $id('playerCaptions');
    if (!box) return;
    if (!text) { box.classList.remove('is-on'); box.innerHTML = ''; return; }
    box.innerHTML = text.split('\n').map(function (line) {
      return '<div class="mfp-cue">' + _esc(line) + '</div>';
    }).join('');
    box.classList.add('is-on');
  }

  // ── Chapters, markers, thumbnails ──────────────────────────
  function _normChapters(list) {
    return (list || []).map(function (c) {
      return { start: +c.start || 0, end: +c.end || 0, title: c.title || '' };
    }).filter(function (c) { return c.end > c.start; });
  }

  function _normMarkers(list) {
    return (list || []).map(function (m) {
      return {
        start: +m.start || 0, end: +m.end || 0, kind: m.kind || 'intro',
        label: m.label || (m.kind === 'outro' ? _tr('Abspann überspringen', 'Skip outro')
                                             : _tr('Intro überspringen', 'Skip intro')),
      };
    }).filter(function (m) { return m.end > m.start; });
  }

  /** Cut chapter notches out of all three seek layers with one mask. */
  function _renderChapterMask() {
    var wrap = $id('playerSeekWrap');
    if (!wrap) return;
    if (!_chapters.length || !_duration) {
      wrap.classList.remove('mfp-seek-masked');
      wrap.style.removeProperty('--mfp-chapter-mask');
      return;
    }
    var stops = ['#000 0'];
    _chapters.forEach(function (c, i) {
      if (i === 0) return;
      var p = Math.max(0, Math.min(100, (c.start / _duration) * 100));
      stops.push('#000 ' + p + '%', 'transparent ' + p + '%',
                 'transparent ' + (p + 0.35) + '%', '#000 ' + (p + 0.35) + '%');
    });
    stops.push('#000 100%');
    wrap.style.setProperty('--mfp-chapter-mask', 'linear-gradient(90deg,' + stops.join(',') + ')');
    wrap.classList.add('mfp-seek-masked');
  }

  function _chapterAt(pos) {
    for (var i = 0; i < _chapters.length; i++) {
      if (pos >= _chapters[i].start && pos < _chapters[i].end) return _chapters[i];
    }
    return null;
  }

  function _updateMarker(filePos) {
    var found = null;
    for (var i = 0; i < _markers.length; i++) {
      if (filePos >= _markers[i].start && filePos < _markers[i].end) { found = _markers[i]; break; }
    }
    if (found === _activeMarker) return;
    _activeMarker = found;
    var float = $id('playerSkipFloat');
    if (found) _setText('playerSkipMarkerBtn', found.label);
    _show(float, !!found);
  }

  function _skipMarker() {
    if (!_activeMarker) return;
    var to = _activeMarker.end;
    _activeMarker = null;
    _show($id('playerSkipFloat'), false);
    _seekToFile(to);
  }

  /**
   * Ask the server for a seek-preview sprite. Generating it decodes the
   * whole file, so the server does it in the background and answers
   * {ready:false} until it is done — the preview simply falls back to the
   * plain time bubble in the meantime.
   */
  async function _loadThumbs() {
    if (_sourceMode || !_filePath || _thumbs || _closed) return;
    try {
      var r = await fetch('/api/stream/thumbs?path=' + encodeURIComponent(_filePath));
      var d = await r.json();
      if (d && d.ready) { _thumbs = d; _stopThumbPoll(); return; }
      // Bounded and cancellable on purpose: each poll costs the server an
      // ffprobe, and an unbounded chain kept running after the player was
      // closed -- with one chain per session restart.
      if (d && d.pending && _thumbTries < 8) {
        _thumbTries++;
        _stopThumbPoll();
        _thumbTimer = setTimeout(_loadThumbs, 15000);
      }
    } catch (e) {}
  }

  function _stopThumbPoll() {
    if (_thumbTimer) { clearTimeout(_thumbTimer); _thumbTimer = null; }
  }

  function _paintPreview(frac, clientX) {
    var wrap = $id('playerSeekWrap'), prev = $id('playerPreview'), bar = $id('playerControls');
    if (!wrap || !prev || !bar || !_duration) return;
    // The bubble is a child of the rail, not of the seek bar, so it has to
    // be placed against the rail's box -- measuring the seek bar here put
    // it off by the rail's padding.
    var rect = bar.getBoundingClientRect();
    var pos  = frac * _duration;
    prev.style.left = Math.max(88, Math.min(rect.width - 88, clientX - rect.left)) + 'px';
    _setText('playerHoverTime', _fmt(pos));

    var ch = _chapterAt(pos), chEl = $id('playerPreviewChapter');
    if (chEl) {
      chEl.textContent = ch ? ch.title : '';
      chEl.style.display = (ch && ch.title) ? '' : 'none';
    }

    var img = $id('playerPreviewImg');
    if (_thumbs && img) {
      var idx  = Math.min(_thumbs.count - 1, Math.floor(pos / _thumbs.interval));
      var page = Math.floor(idx / (_thumbs.cols * _thumbs.rows));
      var cell = idx % (_thumbs.cols * _thumbs.rows);
      img.style.backgroundImage = 'url("' + _thumbs.url.replace('{n}', page) + '")';
      img.style.backgroundPosition = '-' + ((cell % _thumbs.cols) * _thumbs.w) + 'px -' +
                                     (Math.floor(cell / _thumbs.cols) * _thumbs.h) + 'px';
      img.style.width = _thumbs.w + 'px';
      img.style.height = _thumbs.h + 'px';
      prev.classList.add('has-img');
    } else {
      prev.classList.remove('has-img');
    }
    prev.classList.add('is-open');
  }

  function _hidePreview() {
    var p = $id('playerPreview');
    if (p) p.classList.remove('is-open');
  }

  // ── Up next ────────────────────────────────────────────────

  /**
   * Ask the page what comes after the current item.
   *
   * The player has no idea what a "next episode" is -- the library page and
   * the series page each know their own list. They register a resolver
   * (window.mfPlayerResolveNext) instead of the player importing either of
   * them, which also lets a third-party module supply one.
   */
  function _resolveNext(current) {
    try {
      if (typeof window.mfPlayerResolveNext === 'function') {
        return window.mfPlayerResolveNext(current) || null;
      }
    } catch (e) {}
    return null;
  }

  function _setNextUp(next) {
    _next = next || null;
    _nextCancelled = false;
    _renderRailLabels();
  }

  function _updateNextUp(filePos, total) {
    if (!_next || !total || _nextCancelled || _nextDone || _nextTimer) return;
    if (total - filePos > NEXT_LEAD) return;
    _nextLeft = Math.max(3, Math.round(total - filePos));
    _setText('playerNextName', _next.title || '');
    var art = $id('playerNextArt');
    if (art) art.style.backgroundImage = _next.poster ? 'url("' + _next.poster + '")' : '';
    _show($id('playerNextFloat'), true);
    _nextTimer = setInterval(function () {
      _nextLeft--;
      _setText('playerNextCount', _prefs.autoplayNext ? (_tr('in ', 'in ') + _nextLeft + ' s') : '');
      if (_nextLeft <= 0) {
        // Mark it done either way: without autoplay the card would otherwise
        // reappear on the next UI tick and blink every three seconds for the
        // rest of the episode.
        _nextDone = true;
        _stopNextTimer();
        if (_prefs.autoplayNext) _playNext();
      }
    }, 1000);
  }

  function _stopNextTimer() {
    if (_nextTimer) { clearInterval(_nextTimer); _nextTimer = null; }
    _show($id('playerNextFloat'), false);
  }

  function _cancelNext() {
    _nextCancelled = true;
    _stopNextTimer();
  }

  function _playNext() {
    if (!_next) return;
    var n = _next;
    _stopNextTimer();
    _playerStop();
    if (n.path) {
      window.openPlayer(n.path, n.title, 0, { next: n.next || null, poster: n.poster });
    } else if (n.url) {
      window.openStreamSource(n.url, n.title, n.provider || _srcProvider,
                              n.language || _srcLanguage, 0, null, null, _srcMatrix);
    }
  }

  // ── Direct Play source picker ──────────────────────────────
  /** Intro/outro markers live behind their own request: see the route. */
  async function _fetchMarkers(episodeUrl) {
    try {
      var r = await fetch('/api/stream/markers?url=' + encodeURIComponent(episodeUrl));
      var d = await r.json();
      if (d && d.markers && d.markers.length) _markers = _normMarkers(d.markers);
    } catch (e) {}
  }

  async function _fetchSourceMatrix() {
    if (!_srcEpisodeUrl) return;
    try {
      var r = await fetch('/api/providers?url=' + encodeURIComponent(_srcEpisodeUrl));
      var d = await r.json();
      if (d && d.providers && Object.keys(d.providers).length) {
        _srcMatrix = d.providers;
        _buildSourceRows();
        if (_menuView === 'source') { _renderMenu(); _probeSources(); }
      }
    } catch (e) {}
  }

  /** Flatten {language: [provider]} into the rows the picker draws. */
  function _buildSourceRows(keepHealth) {
    var old = {};
    if (keepHealth) _srcRows.forEach(function (r) { old[r.key] = r; });
    var rows = [];
    var langs = _srcMatrix ? Object.keys(_srcMatrix) : [];
    if (!langs.length && _srcLanguage) langs = [_srcLanguage];
    langs.forEach(function (lang) {
      var provs = (_srcMatrix && _srcMatrix[lang]) || [];
      if (!provs.length && lang === _srcLanguage && _srcProvider) provs = [_srcProvider];
      provs.forEach(function (p) {
        var key = lang + '|' + p;
        rows.push(keepHealth && old[key] ? old[key]
          : { key: key, language: lang, provider: p, state: 'unknown', ms: 0, height: 0 });
      });
    });
    // The current pick first, then everything measured, then the rest.
    rows.sort(function (a, b) {
      var ca = (a.language === _srcLanguage && a.provider === _srcProvider) ? 0 : 1;
      var cb = (b.language === _srcLanguage && b.provider === _srcProvider) ? 0 : 1;
      if (ca !== cb) return ca - cb;
      var sa = a.state === 'ok' ? 0 : (a.state === 'unknown' ? 1 : 2);
      var sb = b.state === 'ok' ? 0 : (b.state === 'unknown' ? 1 : 2);
      if (sa !== sb) return sa - sb;
      return (a.ms || 9999) - (b.ms || 9999);
    });
    _srcRows = rows;
  }

  function _renderSourceMenu() {
    var pending = _srcRows.filter(function (r) { return r.state === 'unknown'; }).length;
    var h = _menuHead(_tr('Quelle wählen', 'Choose source'),
                      _srcRows.length + ' ' + _tr('gefunden', 'found'));
    if (!_srcRows.length) {
      h += '<div class="mfp-src-foot">' +
           _esc(_tr('Keine weiteren Quellen bekannt.', 'No other sources known.')) + '</div>';
    }
    _srcRows.forEach(function (r, i) {
      var cur = (r.language === _srcLanguage && r.provider === _srcProvider);
      var cls = 'mfp-src' + (cur ? ' is-sel' : '') + (r.state === 'dead' ? ' is-dead' : '');
      h += '<button class="' + cls + '" data-act="source" data-arg="' + i + '">' +
           '<span class="mfp-menu-tick">' + (cur ? '✓' : '') + '</span>' +
           '<span class="mfp-src-body">' +
             '<span class="mfp-src-name">' + _esc(r.provider) +
               (r.state === 'dead' ? '<span class="mfp-tag is-dead">' +
                 _esc(_tr('nicht erreichbar', 'unreachable')) + '</span>' : '') +
             '</span>' +
             '<span class="mfp-src-tags">' +
               (r.height ? '<span class="mfp-tag is-hd">' + r.height + 'p</span>' : '') +
               '<span class="mfp-tag is-lang">' + _esc(r.language) + '</span>' +
               (/sub/i.test(r.language) ? '<span class="mfp-tag is-sub">' + _esc(_tr('UT', 'Sub')) + '</span>'
                                        : '<span class="mfp-tag is-dub">' + _esc(_tr('Dub', 'Dub')) + '</span>') +
             '</span>' +
           '</span>' +
           _healthHtml(r) +
           '</button>';
    });
    h += '<div class="mfp-src-foot">' +
         (pending && _srcProbing ? '<span class="mfp-mini-spin"></span>' +
            _esc(pending + ' ' + _tr('werden geprüft', 'being checked')) : '') +
         '<button data-act="rescan">' + _esc(_tr('Neu prüfen', 'Re-check')) + '</button></div>';
    return h;
  }

  function _healthHtml(r) {
    var cls = 'is-unknown', txt = '';
    if (r.state === 'ok') {
      cls = r.ms < 1500 ? 'is-good' : (r.ms < 3500 ? 'is-ok' : 'is-slow');
      txt = (r.ms / 1000).toFixed(1).replace('.', ',') + ' s';
    } else if (r.state === 'dead') { cls = 'is-bad'; txt = '—'; }
    return '<span class="mfp-health ' + cls + '">' +
           '<span class="mfp-health-bars"><i></i><i></i><i></i><i></i></span>' +
           '<span>' + _esc(txt) + '</span></span>';
  }

  /**
   * Measure the sources. Each probe resolves the provider link server-side
   * and times the first response, so the list shows what is actually
   * reachable instead of what the site claims to offer. Kept to a small
   * concurrency because every probe is an outbound request.
   */
  async function _probeSources(force) {
    if (_srcProbing || !_srcEpisodeUrl) return;
    var todo = _srcRows.filter(function (r) { return force || r.state === 'unknown'; });
    if (!todo.length) return;
    _srcProbing = true;
    var CONC = 3, at = 0;

    async function worker() {
      while (at < todo.length && !_closed) {
        var row = todo[at++];
        try {
          var r = await fetch('/api/stream/probe-source', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              episode_url: _srcEpisodeUrl, provider: row.provider, language: row.language,
            }),
          });
          var d = await r.json();
          if (d && d.busy) {
            // The server's probe slots were full. Saying "unreachable" here
            // would strike out a perfectly good hoster and exclude it from
            // the automatic failover, so leave it unmeasured instead.
            row.state = 'unknown';
          } else {
            row.state  = (r.ok && d.ok) ? 'ok' : 'dead';
            row.ms     = d.ms || 0;
            row.height = d.height || 0;
          }
        } catch (e) {
          row.state = 'dead';
        }
        if (_menuView === 'source') _renderMenu();
      }
    }
    var workers = [];
    for (var w = 0; w < CONC; w++) workers.push(worker());
    await Promise.all(workers);
    _srcProbing = false;
    _buildSourceRows(true);
    if (_menuView === 'source') _renderMenu();
  }

  function _selectSourceRow(i) {
    var r = _srcRows[i];
    if (!r) return;
    window._playerChangeSource(r.language, r.provider);
  }

  function _nextBestSource() {
    if (!_sourceMode) return null;
    var cand = _srcRows.filter(function (r) {
      return r.state !== 'dead' && !(r.language === _srcLanguage && r.provider === _srcProvider);
    });
    // Same language first — switching the dub behind the user's back is worse
    // than waiting a second longer.
    var same = cand.filter(function (r) { return r.language === _srcLanguage; });
    return (same[0] || cand[0]) || null;
  }

  function _startFailover(alt) {
    _cancelFailover();
    var left = 6;
    _setText('playerErrCount', '(' + left + ' s)');
    _srcFailover = setInterval(function () {
      left--;
      _setText('playerErrCount', '(' + left + ' s)');
      if (left <= 0) {
        _cancelFailover();
        window._playerChangeSource(alt.language, alt.provider);
      }
    }, 1000);
  }
  function _cancelFailover() {
    if (_srcFailover) { clearInterval(_srcFailover); _srcFailover = null; }
  }

  function _updateSourceBadge() {
    var b = $id('playerSourceBadge');
    if (!b) return;
    if (!_sourceMode) { b.style.display = 'none'; return; }
    b.style.display = '';
    b.classList.remove('is-warn');
    var row = _srcRows.filter(function (r) {
      return r.language === _srcLanguage && r.provider === _srcProvider;
    })[0];
    _setText('playerSourceBadgeText',
             _srcProvider + ' · ' + _srcLanguage + (row && row.height ? ' · ' + row.height + 'p' : ''));
  }

  function _mirrorLegacySelects(langOptions, providerOptions) {
    var ls = $id('playerLangSelect'), ps = $id('playerProviderSelect');
    function fill(sel, options, current) {
      if (!sel) return;
      var opts = (options && options.length) ? options : [current];
      sel.innerHTML = '';
      opts.forEach(function (o) {
        var op = document.createElement('option');
        op.value = o; op.textContent = o;
        if (o === current) op.selected = true;
        sel.appendChild(op);
      });
    }
    fill(ls, langOptions || (_srcMatrix ? Object.keys(_srcMatrix) : null), _srcLanguage);
    fill(ps, providerOptions || (_srcMatrix && _srcMatrix[_srcLanguage]) || null, _srcProvider);
  }

  // ── Playback controls ──────────────────────────────────────
  function _togglePlay() {
    var v = $id('playerVideo'); if (!v) return;
    if (v.paused) { v.play().catch(function () {}); _showControls(); }
    else { v.pause(); _showControls(); }
  }

  function _toggleMute() {
    var v = $id('playerVideo'); if (!v) return;
    v.muted = !v.muted;
    _prefs.muted = v.muted; _savePrefs();
    _syncVolumeIcons(v);
  }

  function _setVolume(val) {
    var v = $id('playerVideo'); if (!v) return;
    v.volume = Math.max(0, Math.min(1, parseFloat(val)));
    v.muted  = v.volume === 0;
    _prefs.volume = v.volume; _prefs.muted = v.muted; _savePrefs();
    _syncVolumeIcons(v);
  }

  function _toggleFullscreen() {
    var c = $id('playerContainer'); if (!c) return;
    if (!document.fullscreenElement) {
      if (c.requestFullscreen) c.requestFullscreen().catch(function () {});
      else if (c.webkitRequestFullscreen) c.webkitRequestFullscreen();
      // Phones only ever show a player in landscape; ask politely and
      // ignore the rejection every desktop browser answers with.
      try {
        if (screen.orientation && screen.orientation.lock) {
          screen.orientation.lock('landscape').catch(function () {});
        }
      } catch (e) {}
    } else if (document.exitFullscreen) {
      document.exitFullscreen().catch(function () {});
    }
  }

  async function _togglePip() {
    var v = $id('playerVideo'); if (!v) return;
    try {
      if (document.pictureInPictureElement) await document.exitPictureInPicture();
      else if (v.requestPictureInPicture) await v.requestPictureInPicture();
    } catch (e) {
      if (window.showToast) {
        window.showToast(_tr('Bild-im-Bild ist hier nicht verfügbar.',
                             'Picture in picture is not available here.'), 'warning');
      }
    }
  }

  function _applyPrefsToUi() {
    var vs = $id('playerVolSlider');
    if (vs) vs.value = _prefs.muted ? 0 : _prefs.volume;
    _applyCaptionStyle();
  }

  function _applyPrefsToVideo() {
    var v = $id('playerVideo');
    if (!v) return;
    v.volume = _prefs.volume;
    v.muted  = _prefs.muted;
    v.playbackRate = _speed;
  }

  /**
   * Shape the modal like the video that is actually playing.
   *
   * Without this the box stays 16:9 and anything wider -- a 2.39:1 film is
   * the common case -- gets black bars top and bottom, with the title row
   * and the rail sitting ON those bars. The controls are supposed to float
   * over the picture; letterboxing pushes them off it.
   *
   * Clamped, because a stray 1x1 or a phone-shot vertical clip should not
   * be allowed to turn the modal into a sliver or a tower.
   */
  function _applyVideoAspect() {
    var v = $id('playerVideo'), c = $id('playerContainer');
    if (!v || !c || !v.videoWidth || !v.videoHeight) return;
    var ar = v.videoWidth / v.videoHeight;
    if (!isFinite(ar) || ar <= 0) return;
    ar = Math.max(1.0, Math.min(3.0, ar));
    c.style.setProperty('--mfp-ar', v.videoWidth + ' / ' + v.videoHeight);
    if (ar !== v.videoWidth / v.videoHeight) {
      // Clamped: fall back to the limit rather than the raw ratio.
      c.style.setProperty('--mfp-ar', String(ar));
    }
  }

  function _clearVideoAspect() {
    var c = $id('playerContainer');
    if (c) c.style.removeProperty('--mfp-ar');
  }

  function _setDim(value) {
    _dim = Math.max(0, Math.min(0.85, value));
    var d = $id('playerDim');
    if (d) d.style.opacity = _dim;
  }

  // ── Input bindings (bound once, the modal lives in base.html) ──
  var _bound = false;
  function _bindOnce() {
    if (_bound) return;
    _bound = true;

    var wrap  = $id('playerSeekWrap');
    var video = $id('playerVideo');
    var stage = $id('playerVideoWrap');
    var play  = $id('playerPlayBtn');
    var vol   = $id('playerVolSlider');

    if (play) play.addEventListener('click', _togglePlay);
    if (vol)  vol.addEventListener('input', function () { _setVolume(vol.value); });

    // ── Seek bar: pointer events, so a finger works exactly like a mouse.
    // The old build listened for mousedown/mousemove only, which is why
    // dragging the scrubber did nothing at all on a phone.
    if (wrap) {
      var dragging = false;

      function frac(e) {
        var r = wrap.getBoundingClientRect();
        return Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
      }
      function paintAt(f) {
        var fill = $id('playerSeekFill'), thumb = $id('playerSeekThumb');
        if (fill)  fill.style.width = (f * 100) + '%';
        if (thumb) thumb.style.left = (f * 100) + '%';
      }

      wrap.addEventListener('pointerdown', function (e) {
        if (!_duration) return;
        dragging = true; _seeking = true;
        wrap.classList.add('is-dragging');
        try { wrap.setPointerCapture(e.pointerId); } catch (err) {}
        var f = frac(e);
        paintAt(f); _paintPreview(f, e.clientX);
        e.preventDefault();
      });
      wrap.addEventListener('pointermove', function (e) {
        if (!_duration) return;
        var f = frac(e);
        if (dragging) paintAt(f);
        _paintPreview(f, e.clientX);
        _showControls();
      });
      wrap.addEventListener('pointerup', function (e) {
        if (!dragging) return;
        dragging = false; _seeking = false;
        wrap.classList.remove('is-dragging');
        try { wrap.releasePointerCapture(e.pointerId); } catch (err) {}
        _seekToFile(frac(e) * _duration);
        if (e.pointerType !== 'mouse') _hidePreview();
      });
      wrap.addEventListener('pointercancel', function () {
        dragging = false; _seeking = false;
        wrap.classList.remove('is-dragging');
        _hidePreview();
      });
      wrap.addEventListener('pointerleave', function () { if (!dragging) _hidePreview(); });

      // Keyboard access for the slider role.
      wrap.addEventListener('keydown', function (e) {
        if (!_duration) return;
        if (e.key === 'ArrowRight') { _skip(10);  e.preventDefault(); }
        if (e.key === 'ArrowLeft')  { _skip(-10); e.preventDefault(); }
        if (e.key === 'Home')       { _seekToFile(0); e.preventDefault(); }
        if (e.key === 'End')        { _seekToFile(_duration - 5); e.preventDefault(); }
      });
    }

    if (video) {
      // Fires for the first frame and again after every ffmpeg restart or
      // quality change, which is exactly when the shape can change.
      video.addEventListener('loadedmetadata', _applyVideoAspect);
      video.addEventListener('resize', _applyVideoAspect);
      video.addEventListener('waiting', function () {
        _setSpinnerMsg(_tr('Puffert…', 'Buffering…'));
        _show($id('playerSpinner'), true);
      });
      video.addEventListener('playing', function () {
        _show($id('playerSpinner'), false);
        _showControls();
      });
      video.addEventListener('pause', _showControls);
      video.addEventListener('ended', function () {
        if (_next && _prefs.autoplayNext && !_nextCancelled) _playNext();
      });
      video.addEventListener('ratechange', function () {
        if (!_holdingSpeed) { _speed = video.playbackRate; _renderRailLabels(); }
      });
    }

    if (stage) _bindStage(stage);

    document.addEventListener('fullscreenchange', function () {
      var on = !!document.fullscreenElement;
      var a = $id('playerFsIcon'), b = $id('playerFsExitIcon');
      if (a && b) { a.style.display = on ? 'none' : ''; b.style.display = on ? '' : 'none'; }
    });

    // A click anywhere else closes an open menu.
    document.addEventListener('click', function (e) {
      if (!_menuView) return;
      if (e.target.closest && (e.target.closest('#playerMenu') ||
          e.target.closest('.mfp-lbl') || e.target.closest('#playerSettingsBtn') ||
          e.target.closest('#playerSourceBadge'))) return;
      _closeMenu();
    });
  }

  /** Pointer + gesture handling on the picture itself. */
  var _holdingSpeed = false;
  function _bindStage(stage) {
    var downAt = 0, downX = 0, downY = 0, downPos = 0, moved = false;
    var lastTap = 0, lastTapX = 0, holdTimer = null;
    // Which kind of pointer produced the last press. A browser synthesises a
    // dblclick from two quick taps as well, so the fullscreen shortcut below
    // has to be able to tell a real mouse double-click from a double tap.
    var lastPointerType = 'mouse';
    var swipeMode = null;   // 'vol' | 'dim' | null
    var startVol = 1, startDim = 0;

    function interactive(e) {
      return e.target.closest &&
             e.target.closest('button, a, input, select, #playerMenu, #playerSeekWrap, .mfp-nextup');
    }

    stage.addEventListener('pointermove', function (e) {
      if (e.pointerType === 'mouse') _showControls();
    });

    stage.addEventListener('pointerdown', function (e) {
      // Recorded before the early return, so a press on a button still
      // updates it.
      lastPointerType = e.pointerType || 'mouse';
      if (interactive(e)) return;
      downAt = Date.now(); downX = e.clientX; downY = e.clientY;
      downPos = _filePos(); moved = false; swipeMode = null;
      var v = $id('playerVideo');
      startVol = v ? v.volume : 1;
      startDim = _dim;
      if (e.pointerType !== 'mouse') {
        holdTimer = setTimeout(function () {
          holdTimer = null;
          _holdingSpeed = true;
          if (v) v.playbackRate = 2;
          _show($id('playerSpeedHud'), true);
        }, 480);
      }
    });

    stage.addEventListener('pointermove', function (e) {
      if (!downAt || interactive(e)) return;
      var dx = e.clientX - downX, dy = e.clientY - downY;
      if (!moved && Math.abs(dx) + Math.abs(dy) > 12) {
        moved = true;
        if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
      }
      if (!moved || _holdingSpeed || e.pointerType === 'mouse') return;

      if (!swipeMode && Math.abs(dy) > Math.abs(dx) + 8) {
        var r = stage.getBoundingClientRect();
        swipeMode = (downX - r.left) < r.width / 2 ? 'dim' : 'vol';
      }
      if (swipeMode === 'vol') {
        var vv = Math.max(0, Math.min(1, startVol - dy / 220));
        _setVolume(vv);
        _showHud('vol', Math.round(vv * 100) + '%', vv);
        e.preventDefault();
      } else if (swipeMode === 'dim') {
        var dd = Math.max(0, Math.min(0.85, startDim + dy / 300));
        _setDim(dd);
        _showHud('dim', Math.round((1 - dd / 0.85) * 100) + '%', 1 - dd / 0.85);
        e.preventDefault();
      }
    }, { passive: false });

    stage.addEventListener('pointerup', function (e) {
      if (!downAt || interactive(e)) { downAt = 0; return; }
      var dt = Date.now() - downAt;
      var dx = e.clientX - downX, dy = e.clientY - downY;
      downAt = 0;
      if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }

      if (_holdingSpeed) {
        _holdingSpeed = false;
        var v = $id('playerVideo');
        if (v) v.playbackRate = _speed;
        _show($id('playerSpeedHud'), false);
        return;
      }
      _hideHud();
      if (swipeMode) { swipeMode = null; return; }

      // Swipe down = close, the same gesture every phone app uses.
      if (e.pointerType !== 'mouse' && dy > 110 && Math.abs(dx) < 80 && dt < 600) {
        closePlayer();
        return;
      }
      if (moved) return;

      if (e.pointerType === 'mouse') { _togglePlay(); return; }

      // Touch: single tap toggles the controls, double tap seeks.
      var now = Date.now();
      var r = stage.getBoundingClientRect();
      var rel = (e.clientX - r.left) / r.width;
      if (now - lastTap < 320 && Math.abs(e.clientX - lastTapX) < 80) {
        lastTap = 0;
        if (rel < 0.36)      _skip(-10);
        else if (rel > 0.64) _skip(10);
        else                 _togglePlay();
        return;
      }
      lastTap = now; lastTapX = e.clientX;
      setTimeout(function () {
        if (lastTap === now) { lastTap = 0; _toggleControls(); }
      }, 320);
    });

    stage.addEventListener('pointercancel', function () {
      downAt = 0;
      if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
      if (_holdingSpeed) {
        _holdingSpeed = false;
        var v = $id('playerVideo');
        if (v) v.playbackRate = _speed;
        _show($id('playerSpeedHud'), false);
      }
      _hideHud();
    });

    // A double click on the picture is the DESKTOP fullscreen shortcut.
    //
    // Touch screens must be excluded explicitly: a double tap fires our own
    // gesture handler (jump 10 s) AND a synthesised dblclick, so on a phone
    // in fullscreen every jump also dropped straight back out of fullscreen.
    stage.addEventListener('dblclick', function (e) {
      if (lastPointerType !== 'mouse') return;
      if (interactive(e)) return;
      _toggleFullscreen();
    });
  }

  function _showHud(kind, text, frac) {
    var hud = $id('playerSwipeHud');
    if (!hud) return;
    _setText('playerSwipeVal', text);
    var fill = $id('playerSwipeFill');
    if (fill) fill.style.width = Math.round(Math.max(0, Math.min(1, frac)) * 100) + '%';
    var ic = $id('playerSwipeIcon');
    if (ic) {
      ic.innerHTML = (kind === 'vol')
        ? '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/>'
        : '<circle cx="12" cy="12" r="4"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.2 4.2l2.1 2.1M17.7 17.7l2.1 2.1M4.2 19.8l2.1-2.1M17.7 6.3l2.1-2.1"/>';
    }
    _show(hud, true);
    if (_hudTimer) clearTimeout(_hudTimer);
    _hudTimer = setTimeout(_hideHud, 900);
  }
  function _hideHud() { _show($id('playerSwipeHud'), false); }

  // ── Progress saving ────────────────────────────────────────
  function _startSaveTimer() {
    _stopSaveTimer();
    _saveTimer = setInterval(_saveProgress, SAVE_INTERVAL);
  }
  function _stopSaveTimer() {
    if (_saveTimer) { clearInterval(_saveTimer); _saveTimer = null; }
  }

  async function _saveProgress() {
    var v = $id('playerVideo');
    if (!v || !_filePath) return;
    var filePos  = _filePos();
    var duration = _duration > 0 ? _duration : filePos;
    if (filePos < 1) return;
    try {
      await fetch('/api/progress/save', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: _filePath, position: filePos, duration: duration }),
      });
    } catch (e) {}
  }

  // ── Stream badge ───────────────────────────────────────────
  function _startBadgePoll() {
    _stopBadgePoll();
    _updateStreamBadge(1);
    _badgePoll = setInterval(async function () {
      try {
        var r = await fetch('/api/stream/active');
        var d = await r.json();
        _updateStreamBadge(d.count || 0);
      } catch (e) {}
    }, 5000);
  }
  function _stopBadgePoll() {
    if (_badgePoll) { clearInterval(_badgePoll); _badgePoll = null; }
  }
  function _updateStreamBadge(count) {
    ['streamBadge', 'mobileStreamBadge'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      if (count > 0) { el.textContent = count; el.style.display = ''; }
      else el.style.display = 'none';
    });
  }

  // ── Keyboard ───────────────────────────────────────────────
  document.addEventListener('keydown', function (e) {
    var overlay = $id('playerOverlay');
    var embedded = document.body.classList.contains('sp-embed');
    if (!overlay || (overlay.style.display === 'none' && !embedded)) return;
    var v = $id('playerVideo'); if (!v) return;
    if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' ||
                     e.target.isContentEditable)) return;

    var big = e.shiftKey ? 30 : 10;
    switch (e.key) {
      case 'Escape':
        if (_menuView) _closeMenu(); else closePlayer();
        break;
      case ' ': case 'k': case 'K':
        e.preventDefault(); _togglePlay(); break;
      case 'ArrowRight': e.preventDefault(); _skip(big); break;
      case 'ArrowLeft':  e.preventDefault(); _skip(-big); break;
      case 'l': case 'L': _skip(10); break;
      case 'j': case 'J': _skip(-10); break;
      case 'ArrowUp':    e.preventDefault(); _setVolume(v.volume + 0.1); _showControls(); break;
      case 'ArrowDown':  e.preventDefault(); _setVolume(v.volume - 0.1); _showControls(); break;
      case 'f': case 'F': _toggleFullscreen(); break;
      case 'm': case 'M': _toggleMute(); break;
      case 'c': case 'C':
        // Toggle between off and the first (or last used) subtitle track.
        if (_subSel >= 0) _selectSubtitle(-1);
        else if (_subTracks.length) _selectSubtitle(_subTracks[0].index);
        break;
      case 'p': case 'P': _togglePip(); break;
      case '<': _setSpeed(Math.max(0.5, Math.round((_speed - 0.25) * 100) / 100)); break;
      case '>': _setSpeed(Math.min(2, Math.round((_speed + 0.25) * 100) / 100)); break;
      case 'n': case 'N': if (_next) _playNext(); break;
      default:
        if (e.key >= '0' && e.key <= '9' && _duration) {
          _seekToFile(_duration * (parseInt(e.key, 10) / 10));
        }
    }
    _showControls();
  });

  // Media keys / lock screen.
  if ('mediaSession' in navigator) {
    try {
      navigator.mediaSession.setActionHandler('play',  function () { _togglePlay(); });
      navigator.mediaSession.setActionHandler('pause', function () { _togglePlay(); });
      navigator.mediaSession.setActionHandler('seekbackward',  function () { _skip(-10); });
      navigator.mediaSession.setActionHandler('seekforward',   function () { _skip(10); });
      navigator.mediaSession.setActionHandler('nexttrack',     function () { if (_next) _playNext(); });
    } catch (e) {}
  }

})();
