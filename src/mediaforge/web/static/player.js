// ============================================================
// MediaForge Player — HLS Transcoding Player (custom controls)
// ============================================================
(function () {
  'use strict';

  // ── State ──────────────────────────────────────────────────
  var _token       = null;
  var _filePath    = null;
  var _duration    = 0;      // total file duration from ffprobe
  var _startPos    = 0;      // resume position in file (seconds)
  var _streamStart = 0;      // ffmpeg transcode start offset
  var _hls         = null;
  var _saveTimer   = null;
  var _badgePoll   = null;
  var _uiRaf       = null;
  var _uiLastTick  = 0;
  var _seeking     = false;

  // Stream-from-source (play directly from provider without downloading)
  var _sourceMode  = false;
  var _proxyMode   = false;   // playing the provider's native HLS via proxy (no ffmpeg)
  var _proxyToken  = null;
  var _srcEpisodeUrl = null;
  var _srcProvider   = null;
  var _srcLanguage   = null;
  var _srcTitle      = null;

  var SAVE_INTERVAL = 5000;

  // ── DOM helpers ────────────────────────────────────────────
  function $id(id) { return document.getElementById(id); }

  function _fmt(s) {
    s = Math.max(0, Math.floor(s || 0));
    var h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + ':' : '') +
           (h ? String(m).padStart(2, '0') : m) + ':' +
           String(sec).padStart(2, '0');
  }

  // ── Public API ─────────────────────────────────────────────
  function _setSourceBarVisible(v) {
    var bar = $id('playerSourceBar');
    if (bar) bar.style.display = v ? 'flex' : 'none';
  }

  function _fillSelect(sel, options, current) {
    if (!sel) return;
    sel.innerHTML = '';
    (options || []).forEach(function (o) {
      var opt = document.createElement('option');
      opt.value = o; opt.textContent = o;
      if (o === current) opt.selected = true;
      sel.appendChild(opt);
    });
  }

  window.openPlayer = function (filePath, title, startPos) {
    _sourceMode = false;
    _proxyMode = false;
    _proxyToken = null;
    _setSourceBarVisible(false);
    _filePath = filePath;
    _startPos = 0;
    _playerSetTitle(title || filePath.split(/[\\/]/).pop());
    _playerShow();
    var resume = Math.floor(startPos || 0);
    // Offer "resume vs. start over" only when there's a meaningful position.
    if (resume > 5) {
      _showResumeChoice(resume);
    } else {
      _beginPlayback();
    }
  };

  // Stream an episode directly from its provider (no prior download).
  // Watch progress is keyed by the episode URL so resume works per user too.
  window.openStreamSource = function (episodeUrl, title, provider, language, startPos, langOptions, providerOptions) {
    _sourceMode = true;
    _proxyMode = false;
    _proxyToken = null;
    _srcEpisodeUrl = episodeUrl;
    _srcProvider   = provider || 'VOE';
    _srcLanguage   = language || 'German Dub';
    _srcTitle      = title || 'Stream';
    _filePath = episodeUrl;
    _startPos = 0;
    _playerSetTitle(_srcTitle);
    // Populate the language / provider selectors (fall back to current value).
    _fillSelect($id('playerLangSelect'),
                (langOptions && langOptions.length) ? langOptions : [_srcLanguage], _srcLanguage);
    _fillSelect($id('playerProviderSelect'),
                (providerOptions && providerOptions.length) ? providerOptions : [_srcProvider], _srcProvider);
    _setSourceBarVisible(true);
    _playerShow();
    var resume = Math.floor(startPos || 0);
    if (resume > 5) {
      _showResumeChoice(resume);
    } else {
      _beginPlayback();
    }
  };

  // Re-resolve and restart the source stream when language/provider changes,
  // keeping the current playback position.
  window._playerChangeSource = function () {
    if (!_sourceMode) return;
    var ls = $id('playerLangSelect'), ps = $id('playerProviderSelect');
    if (ls) _srcLanguage = ls.value;
    if (ps) _srcProvider  = ps.value;
    var v = $id('playerVideo');
    var pos = v ? (v.currentTime || 0) + _streamStart : 0;
    // Stop the current transcode session, then restart at the same position.
    _playerStop();
    _startPos = pos;
    _beginPlayback();
  };

  // ── Resume / start-over choice ─────────────────────────────
  function _showResumeChoice(resumeSec) {
    var sp = $id('playerSpinner'); if (sp) sp.style.display = 'none';
    var er = $id('playerError');   if (er) er.style.display = 'none';
    var box = $id('playerResumeChoice');
    var at  = $id('playerResumeAt');
    var lbl = $id('playerResumeLabel');
    window._playerPendingResume = resumeSec;
    if (at)  at.textContent  = (window.t ? t('Du warst bei ', 'You were at ') : 'You were at ') + _fmt(resumeSec);
    if (lbl) lbl.textContent = (window.t ? t('Bei ' + _fmt(resumeSec) + ' fortsetzen', 'Resume at ' + _fmt(resumeSec))
                                         : 'Resume at ' + _fmt(resumeSec));
    if (box) box.style.display = 'flex';
  }
  function _hideResumeChoice() {
    var box = $id('playerResumeChoice'); if (box) box.style.display = 'none';
  }
  function _beginPlayback() {
    _hideResumeChoice();
    _playerSetState('loading');
    _playerStart();
  }
  window._playerResume = function () {
    _startPos = window._playerPendingResume || 0;
    _beginPlayback();
  };
  window._playerStartOver = function () {
    _startPos = 0;
    _beginPlayback();
  };

  window.closePlayer = function () {
    _playerStop();
    _playerHide();
  };

  // ── Internal: global functions for inline HTML ─────────────
  window._playerTogglePlay  = _togglePlay;
  window._playerToggleMute  = _toggleMute;
  window._playerSetVolume   = _setVolume;
  window._playerFullscreen  = _toggleFullscreen;

  // ── Hooks for the Syncplay controller (syncplay.js) ────────
  // Absolute media position = transcode start offset + element currentTime.
  window.playerGetMediaState = function () {
    var v = $id('playerVideo');
    if (!v) return null;
    return { position: (v.currentTime || 0) + _streamStart, paused: !!v.paused };
  };
  // Apply an authoritative remote state without re-broadcasting it.
  window.playerApplyRemoteState = function (action, position, paused) {
    var v = $id('playerVideo');
    if (!v) return;
    if (action === 'play') {
      v.play().catch(function(){});
    } else if (action === 'pause') {
      v.pause();
    } else if (action === 'seek' && typeof position === 'number') {
      if (_proxyMode) { v.currentTime = position; }   // native VOD seek
      else _restartFromPosition(position);
    } else if (action === 'sync' && typeof position === 'number') {
      var cur = (v.currentTime || 0) + _streamStart;
      if (Math.abs(cur - position) > 2.5) {
        if (_proxyMode) {
          v.currentTime = position;
        } else {
          var streamTarget = position - _streamStart;
          var maxBuf = v.buffered.length ? v.buffered.end(v.buffered.length - 1) : 0;
          if (streamTarget >= 0 && streamTarget <= maxBuf) {
            v.currentTime = streamTarget;
          } else {
            _restartFromPosition(position);
          }
        }
      }
      // Reconcile play/pause too (e.g. when first joining a paused room).
      if (typeof paused === 'boolean') {
        if (paused && !v.paused) v.pause();
        else if (!paused && v.paused) v.play().catch(function(){});
      }
    }
  };

  // ── UI state ───────────────────────────────────────────────
  function _playerShow() {
    var o=$id('playerOverlay'); if(o) o.style.display='flex';
    // Pause background animations / glow effects while the player is open so
    // they don't compete with video decoding/compositing (see style.css).
    try { document.body.classList.add('player-open'); } catch(e) {}
  }
  function _playerHide() {
    var o=$id('playerOverlay'); if(o) o.style.display='none';
    try { document.body.classList.remove('player-open'); } catch(e) {}
    _stopUI();
    _cleanupHls();
    var v=$id('playerVideo');
    if(v){ v.pause(); v.src=''; v.load(); }
  }

  function _playerSetTitle(t) { var e=$id('playerTitle'); if(e) e.textContent=t; }

  function _playerSetState(state) {
    var spinner = $id('playerSpinner');
    var errBox  = $id('playerError');
    var controls= $id('playerControls');
    if (spinner)  spinner.style.display  = state==='loading'  ? 'flex' : 'none';
    if (errBox)   errBox.style.display   = state==='error'    ? 'flex' : 'none';
    if (controls) controls.style.opacity = state==='playing'  ? '1'   : '0.4';
  }

  function _playerSetError(msg) {
    _playerSetState('error');
    var e=$id('playerErrorMsg'); if(e) e.textContent=msg;
  }

  function _playerSetEncoderInfo(enc, isHw) {
    var e=$id('playerEncoderInfo'); if(!e) return;
    e.textContent = (enc||'–') + (isHw ? ' ⚡' : ' 🖥️');
    e.title = isHw ? 'Hardware-Encoder' : 'Software-Encoder (CPU)';
  }

  function _setSpinnerMsg(msg) {
    var e=$id('playerSpinnerMsg'); if(e) e.textContent=msg;
  }

  // ── Stream start/stop ──────────────────────────────────────
  async function _playerStart() {
    // For direct streams, first try the passthrough proxy: it plays the
    // provider's native HLS without ffmpeg (smooth, no CPU, instant seeking).
    // Falls back to the transcoder only if that isn't possible.
    if (_sourceMode) {
      var proxied = await _tryProxy();
      if (proxied) return;
    }
    return _startTranscode();
  }

  // Play the provider's native HLS through the server-side passthrough proxy.
  async function _tryProxy() {
    try {
      _setSpinnerMsg(t('Stream wird vorbereitet…', 'Preparing stream…'));
      var resp = await fetch('/api/stream/start-proxy', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          episode_url: _srcEpisodeUrl,
          provider:    _srcProvider,
          language:    _srcLanguage,
        }),
      });
      var data = await resp.json();
      if (!resp.ok || data.error || !data.hls || !data.playlist_url) return false;
      _proxyMode   = true;
      _proxyToken  = data.token || null;
      _token       = null;     // no transcode session
      _streamStart = 0;        // native VOD — no transcode offset
      _duration    = 0;        // hls.js reports duration from the playlist
      _playerSetEncoderInfo('direct', false);
      _loadHls(data.playlist_url, 0);   // _startPos handled in MANIFEST_PARSED
      _startSaveTimer();
      return true;
    } catch (e) {
      return false;
    }
  }

  async function _startTranscode() {
    _proxyMode = false;
    try {
      // 1. Encoder check
      var chk = await fetch('/api/stream/check');
      var chkD = await chk.json();
      if (!chkD.available) {
        _playerSetError(t('Kein Encoder: ' + (chkD.reason || 'ffmpeg fehlt'), 'No encoder: ' + (chkD.reason || 'ffmpeg missing')));
        return;
      }
      _playerSetEncoderInfo(chkD.encoder, chkD.is_hardware);

      // 2. Start transcode
      var resp;
      if (_sourceMode) {
        _setSpinnerMsg(t('Stream wird aufgelöst…', 'Resolving stream…'));
        resp = await fetch('/api/stream/start-source', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            episode_url: _srcEpisodeUrl,
            provider:    _srcProvider,
            language:    _srcLanguage,
            start_pos:   _startPos,
          }),
        });
      } else {
        _setSpinnerMsg(t('Transcoding wird gestartet…', 'Transcoding is starting…'));
        resp = await fetch('/api/stream/start', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({path: _filePath, start_pos: _startPos, syncplay_token: window.__syncplayToken || undefined}),
        });
      }
      var data = await resp.json();
      if (!resp.ok || data.error) { _playerSetError(data.error || t('Transcoding fehlgeschlagen','Transcoding failed')); return; }
      _token       = data.token;
      _duration    = data.duration || 0;
      _streamStart = data.start_pos || 0;
      _playerSetEncoderInfo(data.encoder, data.is_hardware);

      // 3. Wait for first segment
      _setSpinnerMsg(t('Erste Segmente werden generiert…', 'First segments are being generated…'));
      var ready = await _waitForStream(_token, 90);
      if (!ready) return;

      // 4. Load HLS
      var url = '/api/stream/' + _token + '/index.m3u8';
      _loadHls(url, _streamStart);
      _startSaveTimer();
      _startBadgePoll();
    } catch(err) {
      _playerSetError(t('Netzwerkfehler: ' + err.message, 'Network error: ' + err.message));
    }
  }

  function _playerStop() {
    _stopSaveTimer();
    _stopBadgePoll();
    _stopUI();
    // Final progress save before stopping
    _saveProgress().catch(function(){});
    _cleanupHls();
    if (_token) {
      fetch('/api/stream/stop', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({token: _token}),
      }).catch(function(){});
      _token = null;
    }
    if (_proxyToken) {
      fetch('/api/stream/close-proxy', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({token: _proxyToken}),
      }).catch(function(){});
      _proxyToken = null;
    }
    _proxyMode = false;
    _updateStreamBadge(0);
  }

  // ── Status polling ─────────────────────────────────────────
  async function _waitForStream(token, timeoutSec) {
    var deadline = Date.now() + timeoutSec * 1000;
    while (Date.now() < deadline) {
      try {
        var r  = await fetch('/api/stream/' + token + '/status');
        var st = await r.json();
        if (st.ready) return true;
        if (!st.alive) {
          _playerSetError(t('Encoder-Fehler: ' + (st.error || 'ffmpeg beendet'), 'Encoder error: ' + (st.error || 'ffmpeg exited')));
          return false;
        }
        if (st.stderr_tail) console.debug('[Player] ffmpeg:', st.stderr_tail);
      } catch(e) {}
      await new Promise(function(res){ setTimeout(res, 800); });
    }
    _playerSetError(t('Timeout: Stream startet nicht nach 90s', 'Timeout: Stream not starting after 90s'));
    return false;
  }

  // Build a buffer cushion before starting playback. For source streams this
  // prevents the most common cause of stutter: starting on the very first
  // segment and then outrunning ffmpeg's segment production.
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
    // Library playback keeps the old behaviour (start immediately).
    if (!_sourceMode) { video.play().catch(function(){}); return; }

    var TARGET = 12;            // seconds of cushion before we start
    var deadline = Date.now() + 12000;  // …but never wait longer than 12 s
    _setSpinnerMsg(t('Puffer wird aufgebaut…', 'Buffering…'));
    _playerSetState('loading');
    (function _wait() {
      var ahead = _bufferedAhead(video, video.currentTime || seekTarget || 0);
      if (ahead >= TARGET || Date.now() > deadline) {
        _playerSetState('playing');
        video.play().catch(function(){});
        return;
      }
      setTimeout(_wait, 400);
    })();
  }

  // ── HLS.js ─────────────────────────────────────────────────
  function _loadHls(url, streamStartPos) {
    var video = $id('playerVideo');
    if (!video) return;
    _cleanupHls();

    // Click on video = play/pause
    video.onclick = function() { _togglePlay(); };

    if (typeof Hls !== 'undefined' && Hls.isSupported()) {
      _hls = new Hls({
        lowLatencyMode:              false,
        maxBufferLength:             60,      // build a larger forward cushion
        maxMaxBufferLength:          240,
        backBufferLength:            30,
        maxBufferHole:               0.5,     // tolerate small gaps without stalling
        highBufferWatchdogPeriod:    1,
        nudgeMaxRetry:               10,      // recover from minor stalls instead of freezing
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
        _playerSetState('playing');
        // Set MediaSource duration to known total
        if (_duration > 0) _trySetMsDuration(_hls, _duration, 15);
        // Seek to resume position within the stream
        var seekTarget = Math.max(0, _startPos - streamStartPos);
        var _startPlay = function () { _prebufferThenPlay(video, seekTarget); };
        if (seekTarget > 2) {
          video.currentTime = seekTarget;
          video.addEventListener('seeked', function onS() {
            video.removeEventListener('seeked', onS);
            _startPlay();
          });
        } else {
          _startPlay();
        }
        _startUI();
      });

      _hls.on(Hls.Events.LEVEL_UPDATED, function(ev, d) {
        if (d && d.details && d.details.totalduration > _duration) {
          _duration = d.details.totalduration;
        }
      });

      var _hlsRetries = 0;
      _hls.on(Hls.Events.ERROR, function (ev, data) {
        if (data.fatal) {
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR && _hlsRetries < 4) {
            _hlsRetries++;
            setTimeout(function(){ _hls && _hls.startLoad(); }, 1200);
          } else {
            _playerSetError((window.t ? t('Stream-Fehler: ', 'Stream error: ') : 'Stream error: ')
                            + (data.details || data.type));
          }
        }
      });

    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari native HLS
      video.src = url;
      video.addEventListener('loadedmetadata', function() {
        _playerSetState('playing');
        video.play().catch(function(){});
        _startUI();
      }, {once: true});
      video.addEventListener('error', function() {
        _playerSetError(t('Video konnte nicht geladen werden.','Video could not be loaded.'));
      }, {once: true});
    } else {
      _playerSetError(t('Browser unterstützt kein HLS. Bitte Chrome/Firefox/Safari.','Browser does not support HLS. Please use Chrome/Firefox/Safari.'));
    }

    // Buffering spinner (re-show during stalls)
    video.addEventListener('waiting', function() {
      var sp = $id('playerSpinner');
      if (sp) { _setSpinnerMsg('Puffert…'); sp.style.display='flex'; }
    });
    video.addEventListener('playing', function() {
      var sp = $id('playerSpinner');
      if (sp) sp.style.display='none';
    });
  }

  function _cleanupHls() {
    if (_hls) { try { _hls.destroy(); } catch(e) {} _hls = null; }
  }

  /**
   * Stop the current ffmpeg session and start a new one from filePos.
   * Used when seeking beyond the buffered range.
   */
  async function _restartFromPosition(filePos) {
    if (!_filePath || !_token) return;

    _playerSetState('loading');
    _setSpinnerMsg('Springe zu ' + _fmt(filePos) + '…');
    _stopSaveTimer();
    _stopBadgePoll();
    _stopUI();
    _cleanupHls();

    // Stop old session
    var oldToken = _token;
    _token = null;
    fetch('/api/stream/stop', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({token: oldToken}),
    }).catch(function(){});

    // Start new session from requested position
    try {
      var resp = await fetch('/api/stream/start', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: _filePath, start_pos: filePos, syncplay_token: window.__syncplayToken || undefined}),
      });
      var data = await resp.json();
      if (!resp.ok || data.error) { _playerSetError(data.error || t('Neustart fehlgeschlagen', 'Restart failed')); return; }
      _token       = data.token;
      _streamStart = data.start_pos || 0;
      if (data.duration > 0) _duration = data.duration;
      _startPos    = filePos;  // update so progress saving is correct

      _setSpinnerMsg(t('Segmente ab ' + _fmt(filePos) + ' werden generiert…', 'Segments starting from ' + _fmt(filePos) + ' are being generated…'));
      var ready = await _waitForStream(_token, 90);
      if (!ready) return;

      var url = '/api/stream/' + _token + '/index.m3u8';
      _loadHls(url, _streamStart);
      _startSaveTimer();
      _startBadgePoll();
    } catch(err) {
      _playerSetError(t('Netzwerkfehler beim Neustart: ' + err.message, 'Network error: ' + err.message));
    }
  }

  // ── MediaSource duration injection ─────────────────────────
  function _trySetMsDuration(hlsInstance, dur, attempts) {
    if (!hlsInstance || attempts <= 0) return;
    try {
      var ms = (hlsInstance.streamController && hlsInstance.streamController.mediaSource)
             || (hlsInstance.bufferController && hlsInstance.bufferController.mediaSource);
      if (ms && ms.readyState === 'open' && isFinite(dur) && dur > 0) {
        if (Math.abs((ms.duration||0) - dur) > 2) ms.duration = dur;
        return;
      }
    } catch(e) {}
    setTimeout(function(){ _trySetMsDuration(hlsInstance, dur, attempts-1); }, 300);
  }

  // ── Custom Controls UI ─────────────────────────────────────
  function _startUI() {
    _stopUI();
    _uiRaf = requestAnimationFrame(_uiTick);
    _bindSeekbar();
    _bindVolumeSlider();
    _bindPlayButton();
  }

  function _stopUI() {
    if (_uiRaf) { cancelAnimationFrame(_uiRaf); _uiRaf = null; }
  }

  function _uiTick(_now) {
    // Throttle UI updates to ~5/s instead of every frame — updating the seekbar
    // each frame forces continuous layout/paint that competes with the video.
    if (_now && _uiLastTick && (_now - _uiLastTick) < 200) {
      _uiRaf = requestAnimationFrame(_uiTick);
      return;
    }
    _uiLastTick = _now || 0;
    var video = $id('playerVideo');
    if (video) {
      // File position = stream position + stream start offset
      var filePos = (video.currentTime || 0) + _streamStart;
      var total   = _duration || 0;

      // Time text
      var tt = $id('playerTimeText');
      if (tt) tt.textContent = _fmt(filePos) + ' / ' + (total > 0 ? _fmt(total) : '--:--');

      // Seekbar fill + thumb
      var pct = total > 0 ? Math.min(100, (filePos / total) * 100) : 0;
      var fill  = $id('playerSeekFill');
      var thumb = $id('playerSeekThumb');
      if (fill)  fill.style.width = pct + '%';
      if (thumb) thumb.style.left = pct + '%';

      // Buffer bar
      var buf = $id('playerSeekBuf');
      if (buf && video.buffered.length > 0) {
        var bufEnd = video.buffered.end(video.buffered.length - 1) + _streamStart;
        buf.style.width = (total > 0 ? Math.min(100, (bufEnd/total)*100) : 0) + '%';
      }

      // Play/pause icons
      var pi = $id('playerPlayIcon'), pau = $id('playerPauseIcon');
      if (pi && pau) {
        pi.style.display  = video.paused ? '' : 'none';
        pau.style.display = video.paused ? 'none' : '';
      }

      // Volume icons
      var vi = $id('playerVolIcon'), mi = $id('playerMuteIcon');
      var vs = $id('playerVolSlider');
      if (vi && mi) {
        var muted = video.muted || video.volume === 0;
        vi.style.display = muted ? 'none' : '';
        mi.style.display = muted ? '' : 'none';
      }
      if (vs && !_seeking) vs.value = video.muted ? 0 : video.volume;
    }
    _uiRaf = requestAnimationFrame(_uiTick);
  }

  // ── Seekbar interaction ────────────────────────────────────
  function _bindSeekbar() {
    var wrap  = $id('playerSeekWrap');
    var thumb = $id('playerSeekThumb');
    var htime = $id('playerHoverTime');
    if (!wrap) return;

    function _posFromEvent(e) {
      var rect = wrap.getBoundingClientRect();
      return Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    }
    function _seekTo(frac) {
      var video = $id('playerVideo');
      if (!video || !_duration) return;
      var fileTarget   = frac * _duration;
      // SyncPlay: report user seeks reliably here — the 'seeked' DOM event is
      // flaky across a transcode restart. No-op outside a SyncPlay room.
      if (window._spOnUserSeek) { try { window._spOnUserSeek(fileTarget); } catch (e) {} }
      // Proxy mode = native VOD HLS: hls.js fetches whatever segment is needed,
      // so just set the time directly (no ffmpeg restart).
      if (_proxyMode) { video.currentTime = fileTarget; return; }
      var streamTarget = fileTarget - _streamStart;  // can be negative!

      // How far has ffmpeg encoded? (buffered end in stream-time)
      var maxBuffered = 0;
      for (var i = 0; i < video.buffered.length; i++) {
        maxBuffered = Math.max(maxBuffered, video.buffered.end(i));
      }

      var withinStream   = streamTarget >= 0;
      var withinBuffered = streamTarget <= maxBuffered + 12;  // 12s lookahead margin

      if (withinStream && withinBuffered) {
        // Normal seek inside current stream
        video.currentTime = Math.min(streamTarget, maxBuffered);
      } else {
        // Before stream start OR beyond encoded range → restart ffmpeg
        _restartFromPosition(fileTarget);
      }
    }

    // Show thumb on hover
    wrap.addEventListener('mouseenter', function() {
      if (thumb) thumb.style.opacity = '1';
    });
    wrap.addEventListener('mouseleave', function() {
      if (thumb) thumb.style.opacity = '0';
      if (htime) htime.style.display = 'none';
    });

    // Hover time preview
    wrap.addEventListener('mousemove', function(e) {
      var frac = _posFromEvent(e);
      var rect = wrap.getBoundingClientRect();
      if (htime) {
        htime.style.display = 'block';
        htime.style.left    = (e.clientX - rect.left) + 'px';
        htime.textContent   = _fmt(frac * _duration);
      }
    });

    // Click to seek
    wrap.addEventListener('click', function(e) {
      _seekTo(_posFromEvent(e));
    });

    // Drag to seek
    wrap.addEventListener('mousedown', function(e) {
      _seeking = true;
      document.addEventListener('mousemove', _onDrag);
      document.addEventListener('mouseup', function onUp() {
        _seeking = false;
        _seekTo(_posFromEvent(e));
        document.removeEventListener('mousemove', _onDrag);
        document.removeEventListener('mouseup', onUp);
      });
    });
    function _onDrag(e) { if (_seeking) _seekTo(_posFromEvent(e)); }
  }

  // ── Volume / Play controls ─────────────────────────────────
  function _bindVolumeSlider() {
    var s = $id('playerVolSlider');
    if (!s) return;
    s.addEventListener('input', function() { _setVolume(s.value); });
  }

  function _bindPlayButton() {
    var btn = $id('playerPlayBtn');
    if (btn) btn.addEventListener('click', _togglePlay);
  }

  function _togglePlay() {
    var v = $id('playerVideo'); if (!v) return;
    if (v.paused) v.play().catch(function(){}); else v.pause();
  }

  function _toggleMute() {
    var v = $id('playerVideo'); if (!v) return;
    v.muted = !v.muted;
    var s = $id('playerVolSlider');
    if (s) s.value = v.muted ? 0 : v.volume;
  }

  function _setVolume(val) {
    var v = $id('playerVideo'); if (!v) return;
    v.volume = Math.max(0, Math.min(1, parseFloat(val)));
    v.muted  = v.volume === 0;
  }

  function _toggleFullscreen() {
    var c = $id('playerContainer'); if (!c) return;
    if (!document.fullscreenElement) c.requestFullscreen && c.requestFullscreen();
    else document.exitFullscreen && document.exitFullscreen();
  }

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
    var filePos  = (v.currentTime || 0) + _streamStart;
    // Always use ffprobe duration — never add _streamStart to video.duration
    var duration = _duration > 0 ? _duration : filePos;
    if (filePos < 1) return;  // don't save if barely started
    try {
      await fetch('/api/progress/save', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({path: _filePath, position: filePos, duration: duration}),
      });
    } catch(e) {}
  }

  // ── Stream badge ───────────────────────────────────────────
  function _startBadgePoll() {
    _stopBadgePoll();
    _updateStreamBadge(1);
    _badgePoll = setInterval(async function() {
      try {
        var r = await fetch('/api/stream/active');
        var d = await r.json();
        _updateStreamBadge(d.count || 0);
      } catch(e) {}
    }, 5000);
  }
  function _stopBadgePoll() {
    if (_badgePoll) { clearInterval(_badgePoll); _badgePoll = null; }
  }
  function _updateStreamBadge(count) {
    ['streamBadge', 'mobileStreamBadge'].forEach(function(id) {
      var el = document.getElementById(id);
      if (!el) return;
      if (count > 0) { el.textContent = count; el.style.display = ''; }
      else el.style.display = 'none';
    });
  }

  // ── Keyboard shortcuts ─────────────────────────────────────
  document.addEventListener('keydown', function(e) {
    var overlay = $id('playerOverlay');
    if (!overlay || overlay.style.display === 'none') return;
    var v = $id('playerVideo'); if (!v) return;
    if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
    switch(e.key) {
      case 'Escape':     closePlayer(); break;
      case ' ': case 'k': e.preventDefault(); _togglePlay(); break;
      case 'ArrowRight': v.currentTime = Math.min((v.duration||1e9), v.currentTime+10); break;
      case 'ArrowLeft':  v.currentTime = Math.max(0, v.currentTime-10); break;
      case 'ArrowUp':    e.preventDefault(); _setVolume(v.volume+0.1); break;
      case 'ArrowDown':  e.preventDefault(); _setVolume(v.volume-0.1); break;
      case 'f': case 'F': _toggleFullscreen(); break;
      case 'm': case 'M': _toggleMute(); break;
    }
  });

})();
