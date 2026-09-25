/**
 * index.js — CodeGloFix Gate Display
 *
 * This page is the member-facing gate display (outside the door).
 * It has ONE job: show the camera feed and tell the member whether
 * they were granted or denied access.
 *
 * APIs used:
 *   GET  /api/gate_status       — CV pipeline message ("Move closer" etc.)
 *   GET  /api/last_detection    — drives the access result popup
 *   GET  /api/health            — watchdog: auto-reload if camera dies
 *
 * APIs intentionally NOT used here (belong to admin panel only):
 *   /api/revenue/summary  — business stats (member count, active count)
 *   /api/access/today     — today's entry list (other members' data)
 *   /api/access/last      — not needed (last_detection covers this)
 */

'use strict';

// ── Boot ──────────────────────────────────────────────────────────────────────
// Detection starts with the backend service. The public 7-inch gate display
// must not call admin-only pause/resume APIs on every page load.
document.getElementById('foot-year').textContent = new Date().getFullYear();
dg_startClock('clock', null);
dg_startFeedReconnect('feed');
dg_startHealthWatchdog();

// ── Gate status bar ───────────────────────────────────────────────────────────
// Polls the CV pipeline message every 800ms.
// Shows "Waiting…", "Move closer", "Centre your face", member name, etc.
// This is the only text info the member needs while standing at the door.
async function pollGateStatus() {
  try {
    const r = await fetch('/api/gate_status', { cache: 'no-store' });
    const d = await r.json();
    const el = document.getElementById('gate-msg');
    if (el && d.status) el.textContent = d.status;
  } catch(e) {}
}
setInterval(pollGateStatus, 400);
pollGateStatus();

// ── Health dot in gate bar ────────────────────────────────────────────────────
// Mirrors the health watchdog state into the small dot in the gate bar.
// Overrides the default watchdog which targets #s-status (removed from HTML).
const _origOnline   = window._dgOnline;
const _origSetCrit  = window._dgSetCrit;

// Patch the status dot in gate-bar to reflect camera health
const _gateStatusDot = document.getElementById('gate-status-dot');
const _gateStatusTxt = document.getElementById('gate-system-status');

function _setGateDotOnline() {
  if (_gateStatusDot) {
    _gateStatusDot.style.background = 'var(--green)';
    _gateStatusDot.style.boxShadow  = '0 0 5px rgba(34,197,94,0.5)';
  }
  if (_gateStatusTxt) _gateStatusTxt.textContent = 'System OK';
}
function _setGateDotWarn() {
  if (_gateStatusDot) {
    _gateStatusDot.style.background = 'var(--amber)';
    _gateStatusDot.style.boxShadow  = 'none';
  }
  if (_gateStatusTxt) _gateStatusTxt.textContent = 'Recovering…';
}
function _setGateDotCrit() {
  if (_gateStatusDot) {
    _gateStatusDot.style.background = 'var(--red)';
    _gateStatusDot.style.boxShadow  = 'none';
  }
  if (_gateStatusTxt) _gateStatusTxt.textContent = 'Camera offline';
}

// Health check — updates gate-bar dot state independently from the
// alert banner (which common.js already handles).
async function _checkGateHealth() {
  try {
    const r = await fetch('/api/health', { cache: 'no-store' });
    if (!r.ok) { _setGateDotCrit(); return; }
    const h = await r.json();
    const age = h.camera_fresh_seconds_ago;
    if (!h.ok || age === null || age > 30) _setGateDotCrit();
    else if (age > 10) _setGateDotWarn();
    else _setGateDotOnline();
  } catch { _setGateDotCrit(); }
}
setInterval(_checkGateHealth, 5000);
_checkGateHealth();

// ── Detection popup ───────────────────────────────────────────────────────────
// Polls /api/last_detection every 400ms.
// When a new detection arrives, shows the popup for 2.5 seconds.
// This is the ONLY member-facing feedback beyond the camera feed.
let _lastDetectTime = '';
let _popupTimer     = null;

async function pollDetection() {
  try {
    const r = await fetch('/api/last_detection', { cache: 'no-store' });
    const d = await r.json();
    if (d.name && d.time && d.time !== _lastDetectTime) {
      _lastDetectTime = d.time;
      _showPopup(d);
    }
  } catch(e) {}
}

function _showPopup(d) {
  const popup  = document.getElementById('detect-popup');
  const typeEl = document.getElementById('popup-type');
  const nameEl = document.getElementById('popup-name');
  const timeEl = document.getElementById('popup-time');
  if (!popup) return;

  const allowed    = d.allowed || (d.decision && d.decision.startsWith('access_granted'));
  const isPriv     = d.reason === 'granted_owner' || d.reason === 'granted_staff';
  const isExpiring = d.reason === 'granted_expiring_soon';

  if (allowed) {
    if (isPriv) {
      typeEl.textContent  = '✓ ' + (d.reason === 'granted_owner' ? 'Owner' : 'Staff') + ' Access';
      typeEl.style.color  = 'var(--amber)';
    } else if (isExpiring) {
      typeEl.textContent  = '⚠ Granted — Expiring Soon';
      typeEl.style.color  = 'var(--amber)';
    } else {
      typeEl.textContent  = '✓ Access Granted';
      typeEl.style.color  = 'var(--green)';
    }
    popup.style.borderColor = 'rgba(34,197,94,0.3)';
    popup.style.boxShadow   = '0 8px 32px rgba(34,197,94,0.12)';
  } else {
    typeEl.textContent      = '✕ Access Denied';
    typeEl.style.color      = 'var(--red)';
    popup.style.borderColor = 'rgba(239,68,68,0.3)';
    popup.style.boxShadow   = '0 8px 32px rgba(239,68,68,0.10)';
  }

  nameEl.textContent = d.name || '—';

  // Meta line: time + membership days remaining (helpful for member)
  // Does NOT show business counts or other members' data.
  let meta = d.time || '';
  if (allowed && d.days_left != null) {
    meta += '  ·  ' + d.days_left + ' days remaining';
  } else if (!allowed && d.reason) {
    meta += '  ·  ' + d.reason.replace(/_/g, ' ');
  }
  timeEl.textContent = meta;

  popup.style.display   = 'block';
  popup.style.opacity   = '1';
  popup.style.transform = 'translateX(-50%) translateY(0)';

  if (_popupTimer) clearTimeout(_popupTimer);
  _popupTimer = setTimeout(() => {
    popup.style.display = 'none';
  }, 2500);
}

setInterval(pollDetection, 400);

// ── Long-running memory hygiene ───────────────────────────────────────────────
// The kiosk holds a single MJPEG <img> open 24/7. Chromium's decoded-image
// memory can slowly grow over days. A full page reload flushes it. We reload
// once every ~6 hours, but only when nobody is at the door (no popup visible)
// and the tab is in the foreground, so a member never sees a mid-scan reload.
const _KIOSK_RELOAD_MS = 6 * 60 * 60 * 1000;
const _kioskStart = Date.now();
setInterval(() => {
  if (Date.now() - _kioskStart < _KIOSK_RELOAD_MS) return;
  const popup = document.getElementById('detect-popup');
  const popupVisible = popup && popup.style.display === 'block';
  if (!popupVisible && document.visibilityState === 'visible') {
    location.reload();
  }
}, 60 * 1000);
