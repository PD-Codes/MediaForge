// ============================================================
// SyncPlay page controller — native, server-authoritative rooms.
// Transport: SSE down (/api/syncplay/stream), POST up (/api/syncplay/*).
// Reuses player.js: openPlayer(file,title,pos), playerGetMediaState(),
// playerApplyRemoteState(action,pos,paused).
// ============================================================
(function () {
  'use strict';

  var S = {
    token: null, room: null, you: null, isHost: false,
    es: null, media: null, library: null,
    suppress: false, reportTimer: null, reconnectTimer: null,
    countdownTimer: null, typingTimer: null, typingOn: false, pickerTab: 'series',
    typingNames: {}, away: false,
    serverPaused: null, serverPos: 0, serverAt: 0,
  };
  function $(id) { return document.getElementById(id); }
  function tt(de, en) { return (window.t ? window.t(de, en) : en); }
  function post(url, body) { return fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) }).catch(function(){}); }
  function toast(m) { if (window.showToast) return showToast(m); var e = $('spToast'); if (e) { e.textContent = m; e.classList.add('show'); setTimeout(function(){ e.classList.remove('show'); }, 2600); } }
  var esc = window.mfEscape;  // shared, quote-safe (static/mf_escape.js)

  // Remember the room session across reloads (so a refresh doesn't drop you).
  function _saveSession() { try { localStorage.setItem('sp_session', JSON.stringify({ token: S.token, room: S.room })); } catch (e) {} }
  function _loadSession() { try { return JSON.parse(localStorage.getItem('sp_session') || 'null'); } catch (e) { return null; } }
  function _clearSession() { try { localStorage.removeItem('sp_session'); } catch (e) {} }

  // ── Init ────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    var page = document.querySelector('.sp-page'); if (!page) return;
    var invite = (page.dataset.inviteRoom || '').trim();
    fetch('/api/syncplay/config').then(function (r) { return r.json(); }).then(function (c) {
      if (!c.enabled) { toast(tt('SyncPlay ist deaktiviert', 'SyncPlay is disabled')); return; }
      var urlRoom = new URLSearchParams(location.search).get('room') || '';
      if ($('spRoomInput')) $('spRoomInput').value = (invite || urlRoom || '').trim();
      if ($('spNameInput')) $('spNameInput').value = (c.username || '').trim();
      S.canManage = !!c.can_manage;
      if (invite || urlRoom) { if ($('spLobbyTitle')) $('spLobbyTitle').textContent = tt('Einem Raum beitreten', 'Join a room'); if ($('spPwField')) $('spPwField').hidden = false; }
      var saved = _loadSession();
      if (!(invite || urlRoom) && saved && saved.token) {
        _tryResume(saved.token, function () {});
      }
      _startRoomsPoll();   // self-gates on S.token, so it idles while in a room
    }).catch(function () {});
    document.addEventListener('visibilitychange', _onVisibility);
    document.addEventListener('keydown', _onKey);
    if (typeof window.closePlayer === 'function' && !window._spCloseWrapped) {
      window._spCloseWrapped = true;
      var _origClose = window.closePlayer;
      window.closePlayer = function () { _restorePlayer(); return _origClose.apply(this, arguments); };
    }
  });

  // ── Join / leave ────────────────────────────────────────
  S.join = function () {
    var room = ($('spRoomInput').value || '').trim();
    var name = ($('spNameInput').value || '').trim();
    var pw = ($('spPwInput') && $('spPwInput').value || '').trim();
    if (!room) { toast(tt('Bitte Raumnamen eingeben', 'Please enter a room name')); return; }
    if (!name) { toast(tt('Bitte gib einen Namen ein', 'Please enter a name')); $('spNameInput').focus(); return; }
    post('/api/syncplay/join', { room: room, username: name, password: pw })
      .then(function (r) { return r.json(); }).then(function (d) {
        if (!d) return;
        if (d.error) {
          if (/passwort|password/i.test(d.error) && $('spPwField')) { $('spPwField').hidden = false; $('spPwInput').focus(); }
          toast(d.error); return;
        }
        S.token = d.token; _enterRoom(d.snapshot); _saveSession(); _openStream();
      });
  };

  S.leave = function () {
    if (_isWatching() && window.closePlayer) { try { window.closePlayer(); } catch (e) {} }
    if (S.token) _beacon('/api/syncplay/leave', { token: S.token });
    _clearSession();
    _teardown();
    if ($('spRoom')) $('spRoom').hidden = true;
    if ($('spLobby')) $('spLobby').hidden = false;
  };
  function _beacon(url, body) {
    var blob = new Blob([JSON.stringify(body)], { type: 'application/json' });
    if (navigator.sendBeacon) navigator.sendBeacon(url, blob); else post(url, body);
  }

  function _enterRoom(snap) {
    S.room = snap.room; S.you = snap.you; S.isHost = snap.is_host; S.media = snap.media;
    // Seed the authoritative play-state so a late joiner starts at the right
    // spot and doesn't reset the room (see _startPlayback / _ctrl).
    S.serverPos = snap.position; S.serverPaused = snap.paused; S.serverAt = Date.now();
    S.hostLock = !!snap.host_lock;
    $('spLobby').hidden = true; $('spRoom').hidden = false;
    $('spRoom').classList.toggle('is-host', S.isHost);
    $('spRoomName').textContent = snap.room;
    _renderHost(snap.host);
    _applyRoomFlags(snap);
    _renderMembers(snap.members);
    $('spChatLog').innerHTML = '';
    (snap.chat || []).forEach(_appendChat);
    _renderMedia(snap.media);
    _renderHistory(snap.history || []);
  }

  function _teardown() {
    _restorePlayer();
    if (S.es) { S.es.close(); S.es = null; }
    if (S.reportTimer) { clearInterval(S.reportTimer); S.reportTimer = null; }
    if (S.reconnectTimer) { clearTimeout(S.reconnectTimer); S.reconnectTimer = null; }
    _clearCountdown();
    S.token = null; S.room = null; S.isHost = false; S.media = null; S.typingNames = {};
  }

  // ── SSE stream (auto-reconnect) ─────────────────────────
  function _openStream() {
    if (!S.token) return;
    if (S.es) S.es.close();
    S.es = new EventSource('/api/syncplay/stream?token=' + encodeURIComponent(S.token));
    S.es.onmessage = function (e) { var ev; try { ev = JSON.parse(e.data); } catch (x) { return; } _handle(ev); };
    S.es.onerror = function () {
      if (S.es) { S.es.close(); S.es = null; }
      if (!S.token || S.reconnectTimer) return;
      S.reconnectTimer = setTimeout(function () { S.reconnectTimer = null; _openStream(); }, 1500);
    };
    if (!S.reportTimer) S.reportTimer = setInterval(_reportLoop, 1000);
  }

  // ── Inbound events ──────────────────────────────────────
  function _handle(ev) {
    switch (ev.type) {
      case 'members': _renderMembers(ev.members); _applyRoomFlags(ev); break;
      case 'chat':    _appendChat(ev.message); break;
      case 'left':    _appendChat({ system: true, text: ev.name + ' ' + tt('hat den Raum verlassen', 'left the room') }); break;
      case 'play':    _applyRemote('play', ev.position); break;
      case 'pause':   _applyRemote('pause', ev.position); break;
      case 'seek':    _applyRemote('seek', ev.position); break;
      case 'sync':    _applyRemote('sync', ev.position, ev.paused); break;
      case 'waiting': _applyRemote('pause', ev.position); toast(tt('Warte, bis alle bereit sind…', 'Waiting for everyone…')); break;
      case 'buffering': _appendChat({ system: true, text: ev.name + ' ' + tt('puffert…', 'is buffering…') }); break;
      case 'all_ready': _appendChat({ system: true, text: tt('Alle bereit', 'Everyone ready') }); break;
      case 'media':   _onMedia(ev.media); break;
      case 'history': _renderHistory(null, ev.item); break;
      case 'countdown': _showCountdown(ev); break;
      case 'host':    _renderHost(ev.name); _appendChat({ system: true, text: ev.name + ' ' + tt('ist jetzt Host', 'is now host') }); break;
      case 'host_lock': toast(ev.locked ? tt('Host steuert jetzt', 'Host controls now') : tt('Steuerung für alle frei', 'Everyone can control')); break;
      case 'denied':  toast(tt('Nur der Host darf steuern', 'Only the host can control')); break;
      case 'typing':  _onTyping(ev.name, ev.typing); break;
      case 'reaction': _floatReaction(ev.emoji, ev.name); break;
      case 'track':   _applyTrack(ev.kind, ev.value); break;
      case 'kicked':  _onKicked(ev.reason); break;
      case 'closed':  _onClosed(); break;
      case 'resync':  _resync(); break;
    }
  }

  function _onKicked(reason) {
    toast(reason === 'ban' ? tt('Du wurdest gesperrt', 'You were banned') : tt('Du wurdest entfernt', 'You were removed'));
    _clearSession();
    _teardown(); $('spRoom').hidden = true; $('spLobby').hidden = false;
    _startRoomsPoll();
  }

  function _onClosed() {
    toast(tt('Der Raum wurde geschlossen', 'The room was closed'));
    _clearSession();
    _teardown(); $('spRoom').hidden = true; $('spLobby').hidden = false;
    _startRoomsPoll();
  }

  // Server asked us to re-sync (our event queue overflowed): pull a fresh
  // snapshot and re-apply state, leaving chat input untouched.
  function _resync() {
    if (!S.token) return;
    fetch('/api/syncplay/snapshot?token=' + encodeURIComponent(S.token))
      .then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .then(function (d) {
        var snap = d && d.snapshot; if (!snap) return;
        S.media = snap.media; S.isHost = snap.is_host;
        _renderHost(snap.host); _applyRoomFlags(snap); _renderMembers(snap.members);
        _renderMedia(snap.media);
        _applyRemote('sync', snap.position, snap.paused);
      }).catch(function () {});
  }

  // ── Room flags (host-lock / badges) ─────────────────────
  function _applyRoomFlags(o) {
    if ('host_lock' in o) S.hostLock = !!o.host_lock;
    if ('host_lock' in o && $('spLockBadge')) $('spLockBadge').hidden = !o.host_lock;
    if ($('spHostLock') && 'host_lock' in o) $('spHostLock').checked = !!o.host_lock;
    if ($('spMaxInput') && 'max_members' in o && o.max_members) $('spMaxInput').value = o.max_members;
    _applyLockUI();
  }

  // ── Members ─────────────────────────────────────────────
  function _renderHost(host) { $('spRoomHost').textContent = host ? (tt('Host: ', 'Host: ') + host) : ''; }

  function _deviceIcon(d) { return d === 'Phone' ? '📱' : d === 'Tablet' ? '🖥' : '💻'; }

  function _renderMembers(list) {
    list = list || [];
    var ul = $('spMemberList'); if (!ul) return;
    ul.innerHTML = '';
    list.forEach(function (m) {
      var li = document.createElement('li');
      var cls = 'sp-member';
      if (m.away) cls += ' away'; else if (m.buffering) cls += ' buffering'; else if (!m.ready) cls += ' notready';
      li.className = cls;
      li.dataset.name = m.name;
      var status = m.away ? tt('abwesend', 'away') : (m.buffering ? tt('puffert', 'buffering') : (m.paused ? tt('pausiert', 'paused') : tt('schaut', 'watching')));
      var mod = '';
      if (S.isHost && m.name !== S.you) {
        mod = '<span class="sp-mod">' +
          '<button title="' + tt('Host machen', 'Make host') + '" data-act="host">★</button>' +
          '<button title="' + tt('Kick', 'Kick') + '" data-act="kick">⏏</button>' +
          '<button title="' + tt('Sperren', 'Ban') + '" data-act="ban">⛔</button>' +
          '</span>';
      }
      li.innerHTML =
        '<span class="sp-avatar">' + esc(m.initial || '?') + '</span>' +
        '<span class="sp-dot"></span>' +
        '<span class="sp-name">' + esc(m.name) + (m.name === S.you ? ' ' + tt('(du)', '(you)') : '') + '</span>' +
        (m.is_host ? '<span class="sp-host-badge">Host</span>' : '') +
        (m.different ? '<span class="sp-diff-badge" title="' + tt('Andere Folge', 'Different episode') + '">≠</span>' : '') +
        '<span class="sp-device" title="' + esc(status) + '">' + _deviceIcon(m.device) + '</span>' + mod;
      ul.appendChild(li);
      if (m.name === S.you) { S.isHost = m.is_host; $('spRoom').classList.toggle('is-host', m.is_host); _applyLockUI(); }
    });
    // wire mod buttons
    ul.querySelectorAll('.sp-mod button').forEach(function (b) {
      var li = b.closest('.sp-member'); var name = li.dataset.name || '';
      b.onclick = function () {
        var act = b.dataset.act;
        if (act === 'kick') SP.kick(name); else if (act === 'ban') SP.ban(name); else if (act === 'host') SP.transferHost(name);
      };
    });
    $('spMemberCount').textContent = list.length;
  }

  S.kick = function (name) { if (confirm(tt('„' + name + '" entfernen?', 'Remove "' + name + '"?'))) post('/api/syncplay/kick', { token: S.token, name: name }); };
  S.ban = function (name) { if (confirm(tt('„' + name + '" per IP sperren?', 'Ban "' + name + '" by IP?'))) post('/api/syncplay/ban', { token: S.token, name: name, by_ip: true }); };
  S.transferHost = function (name) { if (confirm(tt('Host an „' + name + '" übergeben?', 'Transfer host to "' + name + '"?'))) post('/api/syncplay/transfer-host', { token: S.token, name: name }); };

  // ── Chat + typing ───────────────────────────────────────
  function _appendChat(msg) {
    var log = $('spChatLog'); if (!log) return;
    var div = document.createElement('div');
    if (msg.system) { div.className = 'sp-chat-msg sp-chat-system'; div.textContent = msg.text; }
    else { div.className = 'sp-chat-msg'; div.innerHTML = '<span class="sp-chat-name">' + esc(msg.name) + '</span>' + esc(msg.text); }
    log.appendChild(div); log.scrollTop = log.scrollHeight;
  }
  S.sendChat = function (e) {
    e.preventDefault();
    var inp = $('spChatInput'); var txt = (inp.value || '').trim();
    if (!txt) return false;
    inp.value = ''; _setTyping(false);
    post('/api/syncplay/chat', { token: S.token, text: txt });
    return false;
  };
  S.onType = function () {
    _setTyping(true);
    if (S.typingTimer) clearTimeout(S.typingTimer);
    S.typingTimer = setTimeout(function () { _setTyping(false); }, 2500);
  };
  function _setTyping(on) {
    if (on === S.typingOn) return;
    S.typingOn = on;
    post('/api/syncplay/typing', { token: S.token, typing: on });
  }
  function _onTyping(name, on) {
    if (on) S.typingNames[name] = Date.now(); else delete S.typingNames[name];
    var names = Object.keys(S.typingNames);
    var el = $('spTyping'); if (!el) return;
    el.textContent = names.length ? (names.join(', ') + ' ' + tt('tippt…', 'typing…')) : '';
  }

  // ── Invite (link + native share, QR if lib present) ─────
  S.openInvite = function () {
    var url = location.origin + '/syncplay?room=' + encodeURIComponent(S.room);
    if ($('spInviteLink')) $('spInviteLink').value = url;
    _renderQr(url);
    $('spInvite').hidden = false;
  };
  S.closeInvite = function () { $('spInvite').hidden = true; };
  S.copyInvite = function () {
    var url = $('spInviteLink').value;
    // Always copy to the clipboard — never open the OS share sheet.
    function viaExec() {
      try { $('spInviteLink').select(); document.execCommand('copy'); toast(tt('Link kopiert', 'Link copied')); }
      catch (e) { toast(tt('Kopieren fehlgeschlagen', 'Copy failed')); }
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).then(function () { toast(tt('Link kopiert', 'Link copied')); }).catch(viaExec);
    } else {
      viaExec();
    }
  };
  function _renderQr(text) {
    var box = $('spQr'); if (!box) return;
    box.innerHTML = '';
    if (typeof window.qrcode === 'function') {
      try { var qr = window.qrcode(0, 'M'); qr.addData(text); qr.make(); box.innerHTML = qr.createImgTag(4); box.hidden = false; return; } catch (e) {}
    }
    box.hidden = true; // no QR lib vendored — link/share is used instead
  }

  // ── Host settings ───────────────────────────────────────
  S.openHostSettings = function () { $('spHostSettings').hidden = false; };
  S.closeHostSettings = function () { $('spHostSettings').hidden = true; };
  S.setHostLock = function (locked) { post('/api/syncplay/host-lock', { token: S.token, locked: !!locked }); };
  S.setMax = function () { post('/api/syncplay/max', { token: S.token, max: parseInt($('spMaxInput').value, 10) || 0 }).then(function(){ toast(tt('Übernommen', 'Applied')); }); };
  S.setRoomPassword = function () { post('/api/syncplay/password', { token: S.token, password: ($('spRoomPwInput').value || '').trim() }).then(function(){ toast(tt('Passwort gesetzt', 'Password set')); }); };
  S.closeRoom = function () { if (confirm(tt('Raum für alle schließen?', 'Close the room for everyone?'))) post('/api/syncplay/close', { token: S.token }); };

  // ── Reactions ───────────────────────────────────────────
  S.react = function (emoji) { post('/api/syncplay/reaction', { token: S.token, emoji: emoji }); };
  function _floatReaction(emoji, name) {
    var host = _isWatching() ? ($('spStage') || document.body) : ($('spNow') || document.body);
    var el = document.createElement('div');
    el.className = 'sp-float-reaction'; el.textContent = emoji;
    el.style.left = (10 + Math.random() * 70) + '%';
    host.appendChild(el);
    setTimeout(function () { el.remove(); }, 2600);
  }

  // ── Media (now-playing) ─────────────────────────────────
  function _renderMedia(media) {
    S.media = media;
    var empty = $('spNowEmpty'), card = $('spNowCard');
    if (!media) { empty.hidden = false; card.hidden = true; return; }
    empty.hidden = true; card.hidden = false;
    $('spNowTitle').textContent = media.title || '';
    $('spNowSub').textContent = media.subtitle || (media.season ? ('S' + media.season + (media.episode ? ' · E' + media.episode : '')) : '');
    var poster = $('spNowPoster');
    if (media.poster) { poster.src = media.poster; poster.style.display = ''; } else { poster.style.display = 'none'; }
  }
  function _onMedia(media) {
    _renderMedia(media);
    if (media) {
      // New media → the server reset the room to 0/paused; mirror that so a
      // late/already-watching member starts the new episode at the start.
      S.serverPos = 0; S.serverPaused = true; S.serverAt = Date.now();
      _appendChat({ system: true, text: tt('Host hat gewählt: ', 'Host picked: ') + (media.title || '') });
      if (_isWatching()) _startPlayback(media);
    }
  }

  // ── History ─────────────────────────────────────────────
  function _renderHistory(list, addOne) {
    var ul = $('spHistoryList'), panel = $('spHistoryPanel'); if (!ul) return;
    if (addOne) { var li = document.createElement('li'); li.textContent = (addOne.title || '') + (addOne.subtitle ? ' — ' + addOne.subtitle : ''); ul.insertBefore(li, ul.firstChild); }
    else { ul.innerHTML = ''; (list || []).slice().reverse().forEach(function (h) { var li = document.createElement('li'); li.textContent = (h.title || '') + (h.subtitle ? ' — ' + h.subtitle : ''); ul.appendChild(li); }); }
    if (panel) panel.hidden = ul.children.length === 0;
  }

  // ── Picker (host) — names + meta (library has no posters) ────
  S.openPicker = function () {
    $('spPicker').hidden = false;
    if (S.library) { _renderPicker(); return; }
    _pickerLoading();
    _loadLibrary();
  };
  S.closePicker = function () { $('spPicker').hidden = true; if (S._libTimer) { clearTimeout(S._libTimer); S._libTimer = null; } };

  function _pickerLoading() {
    $('spPickerEmpty').hidden = true;
    $('spPickerGrid').innerHTML = '<div class="sp-picker-loading">' + tt('Mediathek wird geladen…', 'Loading library…') + '</div>';
  }
  function _loadLibrary() {
    fetch('/api/library').then(function (r) { return r.json(); }).then(function (d) {
      S._scanning = !!d.is_scanning;
      S.library = _flattenLibrary(d);
      _renderPicker();
      // Large libraries scan in the background — keep refreshing while open.
      if (S._scanning && $('spPicker') && !$('spPicker').hidden) {
        S._libTimer = setTimeout(function () { S.library = null; _loadLibrary(); }, 3000);
      }
    }).catch(function () { toast(tt('Mediathek konnte nicht geladen werden', 'Could not load library')); });
  }

  function _flattenLibrary(d) {
    var out = [];
    (d.locations || []).forEach(function (loc) {
      var lists = [];
      if (loc.lang_folders) loc.lang_folders.forEach(function (lf) { lists = lists.concat(lf.titles || []); });
      if (loc.titles) lists = lists.concat(loc.titles);
      lists.forEach(function (it) {
        var eps = []; var seasons = it.seasons || {};
        Object.keys(seasons).sort(function (a, b) {
          var na = parseInt(a, 10), nb = parseInt(b, 10);
          if (isNaN(na) && isNaN(nb)) return a.localeCompare(b);
          if (isNaN(na)) return 1; if (isNaN(nb)) return -1; return na - nb;
        }).forEach(function (sk) {
          (seasons[sk] || []).forEach(function (e) {
            if (e.path && e.is_video !== false) eps.push({ season: parseInt(sk, 10) || 0, episode: e.episode, path: e.path, name: e.file || '' });
          });
        });
        if (eps.length) out.push({ title: it.folder || '', is_movie: !!it.is_movie, episodes: eps });
      });
    });
    out.sort(function (a, b) { return a.title.localeCompare(b.title, 'de', { sensitivity: 'base' }); });
    return out;
  }

  S.pickTab = function (tab) {
    S.pickerTab = tab;
    var tabs = document.querySelectorAll('#spPickerTabs .sp-picker-tab');
    tabs.forEach(function (b) { b.classList.toggle('active', b.dataset.ptab === tab); });
    _renderPicker();
  };

  function _renderPicker() {
    var grid = $('spPickerGrid'); grid.innerHTML = '';
    var q = ($('spPickerSearch').value || '').toLowerCase();
    var wantMovies = S.pickerTab === 'movies';
    var items = (S.library || []).filter(function (it) {
      return (it.is_movie === wantMovies) && (!q || it.title.toLowerCase().indexOf(q) !== -1);
    });
    if (!items.length) {
      $('spPickerEmpty').hidden = false;
      $('spPickerEmpty').textContent = S._scanning ? tt('Mediathek wird gescannt…', 'Scanning library…')
        : (wantMovies ? tt('Keine Filme gefunden.', 'No movies found.') : tt('Keine Serien gefunden.', 'No series found.'));
      return;
    }
    $('spPickerEmpty').hidden = true;
    items.forEach(function (it) {
      var div = document.createElement('div'); div.className = 'sp-pick';
      var meta = it.is_movie ? tt('Film', 'Movie') : (it.episodes.length + ' ' + tt('Folgen', 'episodes'));
      div.innerHTML = '<span class="sp-pick-icon">' + (it.is_movie ? '🎬' : '📺') + '</span>' +
        '<span class="sp-pick-title"></span><span class="sp-pick-meta">' + esc(meta) + '</span>';
      div.querySelector('.sp-pick-title').textContent = it.title;
      div.title = it.title;
      div.onclick = function () {
        if (it.is_movie) _pick(it, it.episodes[0]);
        else _renderEpisodes(it);
      };
      grid.appendChild(div);
    });
  }
  S.filterPicker = _renderPicker;

  // Episode chooser for a series (rendered into the same grid; back returns).
  function _renderEpisodes(item) {
    var grid = $('spPickerGrid'); if (!grid) return;
    grid.innerHTML = '';
    if ($('spPickerEmpty')) $('spPickerEmpty').hidden = true;
    var back = document.createElement('div');
    back.className = 'sp-pick sp-pick-back';
    back.innerHTML = '<span class="sp-pick-icon">\u2190</span><span class="sp-pick-title"></span>' +
      '<span class="sp-pick-meta">' + esc(item.episodes.length + ' ' + tt('Folgen', 'episodes')) + '</span>';
    back.querySelector('.sp-pick-title').textContent = item.title;
    back.title = item.title;
    back.onclick = _renderPicker;
    grid.appendChild(back);
    item.episodes.forEach(function (ep) {
      var div = document.createElement('div'); div.className = 'sp-pick sp-pick-ep';
      var label = 'S' + (ep.season || 0) + ' \u00b7 E' + (ep.episode != null ? ep.episode : '?');
      div.innerHTML = '<span class="sp-pick-icon">\u25b6</span><span class="sp-pick-title"></span>' +
        '<span class="sp-pick-meta">' + esc(label) + '</span>';
      div.querySelector('.sp-pick-title').textContent = ep.name || label;
      div.title = ep.name || label;
      div.onclick = function () { _pick(item, ep); };
      grid.appendChild(div);
    });
  }

  function _pick(item, ep) {
    var media = { title: item.title, is_movie: item.is_movie, season: ep.season, episode: ep.episode, file: ep.path, subtitle: item.is_movie ? '' : ('S' + ep.season + ' · E' + ep.episode) };
    post('/api/syncplay/episode', { token: S.token, media: media }).then(function (r) { return r.json(); }).then(function (d) { if (d && d.error) { toast(d.error); return; } S.closePicker(); });
  }

  // ── Room directory (lobby) ──────────────────────────────
  function _startRoomsPoll() {
    _loadRooms();
    if (S._roomsTimer) clearInterval(S._roomsTimer);
    S._roomsTimer = setInterval(function () { if (!S.token) _loadRooms(); }, 4000);
  }
  function _loadRooms() {
    fetch('/api/syncplay/rooms').then(function (r) { return r.json(); }).then(function (d) { _renderRooms(d.rooms || []); }).catch(function () {});
  }
  function _renderRooms(rooms) {
    var wrap = $('spRooms'), ul = $('spRoomList'); if (!ul) return;
    wrap.hidden = rooms.length === 0;
    ul.innerHTML = '';
    rooms.forEach(function (r) {
      var li = document.createElement('li'); li.className = 'sp-room-item';
      var sub = r.watching ? (tt('schaut: ', 'watching: ') + r.watching + (r.watching_sub ? ' (' + r.watching_sub + ')' : '')) : tt('nichts ausgewählt', 'nothing selected');
      li.innerHTML =
        '<div class="sp-room-item-info"><div class="sp-room-item-name"></div><div class="sp-room-item-sub"></div></div>' +
        '<div class="sp-room-item-meta"><span class="sp-room-item-count">' + r.count + ' 👤</span>' + (r.has_password ? ' 🔒' : '') + '</div>' +
        '<div class="sp-room-item-actions"><button class="sp-btn sp-btn-primary" data-join>' + tt('Beitreten', 'Join') + '</button>' +
        (S.canManage ? '<button class="sp-btn sp-btn-danger" data-close>' + tt('Schließen', 'Close') + '</button>' : '') + '</div>';
      li.querySelector('.sp-room-item-name').textContent = r.name + (r.locked ? ' 🔒' : '');
      li.querySelector('.sp-room-item-sub').textContent = sub;
      li.querySelector('[data-join]').onclick = function () { $('spRoomInput').value = r.name; if (r.has_password && $('spPwField')) $('spPwField').hidden = false; SP.join(); };
      var cb = li.querySelector('[data-close]'); if (cb) cb.onclick = function () { SP.closeRoomByName(r.name); };
      ul.appendChild(li);
    });
  }
  S.closeRoomByName = function (name) {
    if (!confirm(tt('Raum „' + name + '" für alle schließen?', 'Close room "' + name + '" for everyone?'))) return;
    post('/api/syncplay/close-room', { name: name }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.error) { toast(d.error); return; }
      toast(tt('Raum geschlossen', 'Room closed')); _loadRooms();
    });
  };

  // ── Playback (reuse player.js overlay) ──────────────────
  S.openCurrent = function () { if (S.media && S.media.file) _startPlayback(S.media); };
  function _isWatching() {
    var v = document.getElementById('playerVideo'), stage = document.getElementById('spStage');
    return !!(v && stage && stage.contains(v));
  }
  function _startPlayback(media) {
    if (typeof window.openPlayer !== 'function') { toast(tt('Player nicht verfügbar', 'Player unavailable')); return; }
    _clearCountdown();
    if ($('spStage')) $('spStage').hidden = false;
    if ($('spNow')) $('spNow').style.display = 'none';
    // Start at the room's CURRENT position (not 0) and suppress our own startup
    // play/seek events, so a late joiner never yanks everyone back to the start.
    var startPos = (typeof S.serverPos === 'number' && S.serverPos > 0) ? S.serverPos : 0;
    S.suppress = true;
    window.__syncplayToken = S.token;  // share ONE transcode session per room
    window.openPlayer(media.file, media.title + (media.subtitle ? ' — ' + media.subtitle : ''), startPos);
    _embedPlayer();
    setTimeout(function () {
      _embedPlayer(); _attachPlayer(); _applyLockUI();
      // Match the room's pause state, then release suppression.
      var v = $('playerVideo');
      if (v && S.serverPaused === true) { try { v.pause(); } catch (e) {} }
      setTimeout(function () { S.suppress = false; }, 1000);
    }, 400);
  }

  // Move the shared #playerContainer into our inline stage (no fullscreen modal).
  function _embedPlayer() {
    var c = document.getElementById('playerContainer');
    var stage = $('spStage');
    if (c && stage && c.parentNode !== stage) stage.appendChild(c);
    if (c) c.classList.add('sp-embed');
    document.body.classList.add('sp-embed');
  }

  // Host-lock: when it's on and we're not the host, disable the timeline + play
  // button so nobody else can skip (not even locally).
  function _applyLockUI() {
    var locked = !!(S.hostLock && !S.isHost && _isWatching());
    document.body.classList.toggle('sp-locked', locked);
  }
  function _restorePlayer() {
    var c = document.getElementById('playerContainer');
    var ov = document.getElementById('playerOverlay');
    if (c && ov && c.parentNode !== ov) ov.appendChild(c);
    if (c) c.classList.remove('sp-embed');
    document.body.classList.remove('sp-embed');
    document.body.classList.remove('sp-locked');
    window.__syncplayToken = null;
    if ($('spStage')) $('spStage').hidden = true;
    if ($('spNow')) $('spNow').style.display = '';
  }
  function _attachPlayer() {
    var v = $('playerVideo'); if (!v) return;
    [['play', _onPlay], ['pause', _onPause], ['seeked', _onSeeked], ['waiting', _onBuffer], ['playing', _onPlaying], ['ended', _onEnded], ['ratechange', _onRate]]
      .forEach(function (p) { v.removeEventListener(p[0], p[1]); v.addEventListener(p[0], p[1]); });
  }
  function _ctrl(action) {
    if (!S.token || S.suppress) return;
    // Host-only mode: a non-host can't drive playback. Snap back to the room
    // state locally instead of round-tripping a denial (which restarts a live
    // transcode for everyone).
    if (S.hostLock && !S.isHost) {
      var vv = $('playerVideo');
      if (vv) {
        if (S.serverPaused === true && !vv.paused) { try { vv.pause(); } catch (e) {} }
        else if (S.serverPaused === false && vv.paused) { vv.play().catch(function () {}); }
      }
      return;
    }
    var st = window.playerGetMediaState ? window.playerGetMediaState() : null;
    var pos = st ? st.position : 0;
    // De-dupe: don't echo a state the server just pushed us. Applying a remote
    // play/pause/seek re-fires DOM events (and a transcode restart can outlast
    // the 600ms suppress window), which would otherwise bounce control back.
    if ((Date.now() - (S.serverAt || 0)) < 2500) {
      if (action === 'play'  && S.serverPaused === false) return;
      if (action === 'pause' && S.serverPaused === true)  return;
      if (action === 'seek'  && Math.abs(pos - (S.serverPos || 0)) < 1.5) return;
    }
    post('/api/syncplay/control', { token: S.token, action: action, position: pos });
  }
  function _onPlay()  { _ctrl('play'); }
  function _onPause() { _ctrl('pause'); }
  function _onSeeked(){ if (Date.now() - (S.lastUserSeekAt || 0) < 1000) return; _ctrl('seek'); }
  function _onBuffer(){ _report(true); }
  function _onPlaying(){ _report(false); }
  function _onRate()  { if (!S.isHost || S.suppress) return; var v = $('playerVideo'); if (v) post('/api/syncplay/track', { token: S.token, kind: 'rate', value: v.playbackRate }); }

  function _applyRemote(action, position, paused) {
    // Record what the server pushed so _ctrl can suppress the echo.
    S.serverAt = Date.now();
    if (action === 'play') S.serverPaused = false;
    else if (action === 'pause') S.serverPaused = true;
    else if (action === 'sync') S.serverPaused = !!paused;
    if (typeof position === 'number') S.serverPos = position;
    if (!window.playerApplyRemoteState || !_isWatching()) return;
    S.suppress = true;
    if (S._suppressTimer) { clearTimeout(S._suppressTimer); S._suppressTimer = null; }
    try { window.playerApplyRemoteState(action, position, paused); }
    finally {
      if (action === 'seek') {
        // Applying a remote seek restarts the transcode ("loading segments"),
        // which can outlast any fixed timer. Hold suppression until the player
        // actually settles, so the resulting seeked/play DOM events are not
        // echoed back as a new seek (which caused the endless restart loop).
        _holdSuppressUntilSettled();
      } else {
        S._suppressTimer = setTimeout(function () { S.suppress = false; }, 600);
      }
    }
  }

  // Hold suppression until the player settles after a remote-seek restart.
  function _holdSuppressUntilSettled() {
    var v = $('playerVideo');
    if (!v) { S._suppressTimer = setTimeout(function () { S.suppress = false; }, 800); return; }
    _clearSettle();
    S._settleHandler = function () {
      if (S._settleDebounce) clearTimeout(S._settleDebounce);
      S._settleDebounce = setTimeout(_releaseSettle, 1500);
    };
    ['canplay', 'seeked', 'playing'].forEach(function (e) { v.addEventListener(e, S._settleHandler); });
    S._settleCap = setTimeout(_releaseSettle, 20000);  // never suppress forever
  }
  function _releaseSettle() { _clearSettle(); S.suppress = false; }
  function _clearSettle() {
    var v = $('playerVideo');
    if (v && S._settleHandler) ['canplay', 'seeked', 'playing'].forEach(function (e) { v.removeEventListener(e, S._settleHandler); });
    S._settleHandler = null;
    if (S._settleDebounce) { clearTimeout(S._settleDebounce); S._settleDebounce = null; }
    if (S._settleCap) { clearTimeout(S._settleCap); S._settleCap = null; }
  }
  function _applyTrack(kind, value) {
    var v = $('playerVideo'); if (!v) return;
    S.suppress = true;
    try { if (kind === 'rate') v.playbackRate = parseFloat(value) || 1; }
    finally { setTimeout(function () { S.suppress = false; }, 400); }
  }

  function _reportLoop() { _report(false); }
  function _report(buffering) {
    if (!S.token) return;
    var st = window.playerGetMediaState ? window.playerGetMediaState() : null;
    if (!st) return;
    post('/api/syncplay/report', { token: S.token, position: st.position, paused: st.paused, buffering: !!buffering, file: S.media ? S.media.file : null });
  }

  // ── Auto-next episode (host drives, synced countdown) ───
  function _onEnded() {
    if (!S.isHost) return;
    var next = _nextEpisode(); if (!next) return;
    post('/api/syncplay/episode', { token: S.token, media: next, countdown: 10 });
  }
  function _nextEpisode() {
    if (!S.media || S.media.is_movie || !S.library) return null;
    var item = (S.library || []).find(function (it) { return it.title === S.media.title; });
    if (!item) return null;
    var idx = item.episodes.findIndex(function (e) { return e.path === S.media.file; });
    if (idx < 0 || idx + 1 >= item.episodes.length) return null;
    var ep = item.episodes[idx + 1];
    return { title: item.title, is_movie: false, season: ep.season, episode: ep.episode, file: ep.path, subtitle: 'S' + ep.season + ' · E' + ep.episode };
  }

  // ── Countdown overlay (synced) ──────────────────────────
  function _showCountdown(ev) {
    _clearCountdown();
    var media = ev.media, secs = ev.countdown || 10;
    S.serverPos = 0; S.serverPaused = true; S.serverAt = Date.now();
    var box = document.createElement('div'); box.className = 'sp-countdown'; box.id = 'spCountdown';
    box.innerHTML = '<div class="sp-countdown-label">' + tt('Nächste Folge', 'Up next') + '</div>' +
      '<div class="sp-countdown-title"></div><div class="sp-countdown-bar"><div class="sp-countdown-fill"></div></div>' +
      '<div class="sp-countdown-actions"><button class="sp-btn sp-btn-primary" id="spCdNow">' + tt('Jetzt', 'Play now') + '</button>' +
      '<button class="sp-btn sp-btn-ghost" id="spCdCancel">' + tt('Abbrechen', 'Cancel') + '</button></div>';
    document.body.appendChild(box);
    box.querySelector('.sp-countdown-title').textContent = (media.title || '') + (media.subtitle ? ' — ' + media.subtitle : '');
    var fill = box.querySelector('.sp-countdown-fill'); var left = secs; fill.style.width = '100%';
    S.countdownTimer = setInterval(function () { left -= 1; fill.style.width = Math.max(0, (left / secs) * 100) + '%'; if (left <= 0) { _clearCountdown(); _startPlayback(media); } }, 1000);
    $('spCdNow').onclick = function () { _clearCountdown(); _startPlayback(media); };
    $('spCdCancel').onclick = function () { _clearCountdown(); };
  }
  function _clearCountdown() { if (S.countdownTimer) { clearInterval(S.countdownTimer); S.countdownTimer = null; } var b = $('spCountdown'); if (b) b.remove(); }

  // ── Away detection + keyboard ───────────────────────────
  function _onVisibility() { if (!S.token) return; var away = document.hidden; if (away !== S.away) { S.away = away; post('/api/syncplay/away', { token: S.token, away: away }); } }
  function _onKey(e) {
    if (!S.token) return;
    var inField = /input|textarea/i.test((e.target.tagName || ''));
    if (e.key === 'Escape') { ['spPicker', 'spInvite', 'spHostSettings'].forEach(function (id) { if ($(id)) $(id).hidden = true; }); }
    if (e.key === 'c' && !inField) { e.preventDefault(); if ($('spChatInput')) $('spChatInput').focus(); }
  }

  // ── Resume after reload ─────────────────────────────────
  function _tryResume(token, cb) {
    fetch('/api/syncplay/snapshot?token=' + encodeURIComponent(token))
      .then(function (r) { if (!r.ok) throw 0; return r.json(); })
      .then(function (d) { S.token = d.token; _enterRoom(d.snapshot); _saveSession(); _openStream(); cb(true); })
      .catch(function () { _clearSession(); cb(false); });
  }

  // Reliable user-seek hook, called by player.js for BOTH in-buffer seeks and
  // transcode restarts (where the 'seeked' DOM event is unreliable).
  window._spOnUserSeek = function (pos) {
    if (!S.token || S.suppress) return;
    if (S.hostLock && !S.isHost) return;   // host-only mode: can't drive playback
    S.lastUserSeekAt = Date.now();
    post('/api/syncplay/control', { token: S.token, action: 'seek', position: pos });
  };

  window.SP = S;
})();
