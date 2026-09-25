/**
 * common.js — CodeGloFix shared utilities
 * Clock, toast, health watchdog, initials helper
 */

'use strict';

// ── Clock ────────────────────────────────────────────────────────────────────
function dg_startClock(clockId, dateId) {
  function tick() {
    const now = new Date();
    const clockEl = document.getElementById(clockId);
    if (clockEl) clockEl.textContent = now.toLocaleTimeString('en-GB');
    const dateEl = document.getElementById(dateId);
    if (dateEl) dateEl.textContent = now.toLocaleDateString('en-GB', { day:'2-digit', month:'short', year:'numeric' });
  }
  setInterval(tick, 1000);
  tick();
}

// ── Toast ────────────────────────────────────────────────────────────────────
let _toastTimer = null;
function toast(msg, type = 'ok') {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = (type === 'ok' ? '✓  ' : '✕  ') + msg;
  el.className = 'show ' + type;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = ''; }, 3500);
}

// ── Initials helper ──────────────────────────────────────────────────────────
function dg_initials(name) {
  if (!name) return '?';
  return name.split(' ').slice(0, 2).map(w => w[0] || '').join('').toUpperCase();
}

// ── Health watchdog (used by index.html) ─────────────────────────────────────
function dg_startHealthWatchdog() {
  const statusEl       = document.getElementById('s-status');
  const alertBanner    = document.getElementById('system-alert');
  const alertMsg       = document.getElementById('alert-msg');
  const alertIcon      = document.getElementById('alert-icon');
  const alertCountdown = document.getElementById('alert-countdown');
  const statusDot      = document.querySelector('.status-dot');
  const liveDot        = document.querySelector('.live-dot');

  let reloadTimer = null, reloadCD = 0, cdInterval = null;

  function clearReload() {
    if (reloadTimer)   { clearTimeout(reloadTimer);   reloadTimer = null; }
    if (cdInterval)    { clearInterval(cdInterval);   cdInterval = null;  }
    if (alertCountdown) alertCountdown.textContent = '';
    reloadCD = 0;
  }

  function scheduleReload(sec) {
    clearReload();
    reloadCD = sec;
    if (alertCountdown) alertCountdown.textContent = 'Reloading in ' + sec + 's…';
    cdInterval = setInterval(() => {
      reloadCD--;
      if (alertCountdown) alertCountdown.textContent = reloadCD > 0 ? 'Reloading in ' + reloadCD + 's…' : 'Reloading…';
      if (reloadCD <= 0) clearInterval(cdInterval);
    }, 1000);
    reloadTimer = setTimeout(() => location.reload(), sec * 1000);
  }

  function setOnline() {
    if (statusEl) { statusEl.textContent = '● Online'; statusEl.style.color = 'var(--green)'; }
    if (statusDot){ statusDot.style.background = 'var(--green)'; statusDot.style.boxShadow = '0 0 5px rgba(34,197,94,0.5)'; }
    if (liveDot)  { liveDot.style.background = 'var(--green)'; }
    if (alertBanner) { alertBanner.className = ''; alertBanner.style.display = 'none'; }
    clearReload();
  }

  function setWarn(age) {
    if (statusEl) { statusEl.textContent = '● Recovering'; statusEl.style.color = 'var(--amber)'; }
    if (statusDot){ statusDot.style.background = 'var(--amber)'; statusDot.style.boxShadow = 'none'; }
    if (liveDot)  { liveDot.style.background = 'var(--amber)'; }
    if (alertIcon) alertIcon.textContent = '⚠';
    if (alertMsg)  alertMsg.textContent = 'Camera signal weak — last frame ' + Math.round(age) + 's ago';
    if (alertBanner){ alertBanner.className = 'warn'; alertBanner.style.display = ''; }
    clearReload();
  }

  function setCrit(label, msg) {
    if (statusEl) { statusEl.textContent = '● ' + label; statusEl.style.color = 'var(--red)'; }
    if (statusDot){ statusDot.style.background = 'var(--red)'; statusDot.style.boxShadow = 'none'; }
    if (liveDot)  { liveDot.style.background = 'var(--red)'; }
    if (alertIcon) alertIcon.textContent = '✕';
    if (alertMsg)  alertMsg.textContent = msg;
    if (alertBanner){ alertBanner.className = 'crit'; alertBanner.style.display = ''; }
    if (!reloadTimer) scheduleReload(10);
  }

  async function check() {
    try {
      const r = await fetch('/api/health', { cache: 'no-store' });
      if (!r.ok) { setCrit('Backend Error', 'Server error. Reloading…'); return; }
      const h = await r.json();
      const age = h.camera_fresh_seconds_ago;
      if (!h.ok || age === null || age > 30) setCrit('Camera Offline', 'No fresh frame for ' + (age !== null ? Math.round(age) + 's' : 'unknown') + '. Reloading…');
      else if (age > 10) setWarn(age);
      else setOnline();
    } catch { setCrit('Backend Lost', 'Cannot reach server. Reloading…'); }
  }

  setInterval(check, 5000);
  check();
}

// ── MJPEG feed auto-reconnect ────────────────────────────────────────────────
function dg_startFeedReconnect(imgId) {
  const feed = document.getElementById(imgId);
  if (!feed) return;

  // Preserve existing feed parameters, for example /video_feed?clean=1
  // used by the enrolment page. The old reconnect always reset back to
  // /video_feed, which could accidentally show annotated detection labels.
  const baseSrc = feed.getAttribute('src') || '/video_feed';
  let timer = null;

  feed.onerror = () => {
    if (timer) return;
    timer = setTimeout(() => {
      timer = null;
      const sep = baseSrc.includes('?') ? '&' : '?';
      feed.src = baseSrc + sep + 't=' + Date.now();
    }, 3000);
  };
}

// ── Auth helpers ─────────────────────────────────────────────────────────────
async function dg_checkAuth(onAuthenticated) {
  try {
    const r = await fetch('/api/auth/check');
    const d = await r.json();
    if (d.authenticated) onAuthenticated();
  } catch(e) {}
}

async function dg_login(password, onSuccess, onFail) {
  const fd = new FormData();
  fd.append('password', password);
  try {
    const r = await fetch('/api/auth/login', { method: 'POST', body: fd });
    const d = await r.json();
    if (d.ok) onSuccess();
    else onFail('Incorrect password');
  } catch(e) { onFail('Server error — try again'); }
}

async function dg_logout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  location.reload();
}
