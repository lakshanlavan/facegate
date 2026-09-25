/**
 * enrol.js — CodeGloFix Live Camera Enrolment
 * Corrected version:
 * - Shows successful registration notification properly
 * - Does NOT call /api/enrol/cancel after successful confirm
 * - Shows "Registered ✓" on the button after successful registration
 * - Resets capture slots correctly after registration
 * - Keeps Register button disabled after reset until 3 new captures are taken
 */

'use strict';

dg_startClock('clock', null);
dg_startFeedReconnect('feed');

// ── Gate status poll ─────────────────────────────────────────────────────────
async function pollGate() {
  try {
    const r = await fetch('/api/gate_status', { cache: 'no-store' });
    const d = await r.json();
    const el = document.getElementById('gate-msg');
    if (el) el.textContent = d.status || '—';
  } catch (e) {
    // Silent fail for UI polling
  }
}
setInterval(pollGate, 800);

// ── Capture state ─────────────────────────────────────────────────────────────
let _captured = {};

// ── Utility helpers ───────────────────────────────────────────────────────────
function _getName() {
  return (document.getElementById('enrol-name')?.value || '').trim();
}

function _getPhone() {
  return (document.getElementById('enrol-phone')?.value || '').trim();
}

function _clearNameError() {
  const nameErr = document.getElementById('name-err');
  if (nameErr) nameErr.textContent = '';
}

function _setNameError(msg) {
  const nameErr = document.getElementById('name-err');
  if (nameErr) nameErr.textContent = msg || '';
}

function _clearEnrolMessage() {
  const err = document.getElementById('enrol-err');
  if (!err) return;

  err.textContent = '';
  err.style.display = '';
  err.style.color = '';
  err.style.background = '';
  err.style.border = '';
  err.style.borderRadius = '';
  err.style.padding = '';
  err.style.marginBottom = '';
  err.style.fontWeight = '';
}

function _showError(msg) {
  const err = document.getElementById('enrol-err');
  if (!err) return;

  err.textContent = msg || 'Something went wrong.';
  err.style.display = 'block';
  err.style.color = 'var(--red)';
  err.style.background = 'var(--red-dim)';
  err.style.border = '1px solid rgba(239,68,68,0.25)';
  err.style.borderRadius = 'var(--rs)';
  err.style.padding = '8px 12px';
  err.style.marginBottom = '8px';
  err.style.fontWeight = '600';
}

function _showSuccess(name) {
  const el = document.getElementById('enrol-err');
  if (!el) return;

  el.textContent = '✓ ' + name + ' registered successfully! Now go to Admin → Payments to add membership payment.';
  el.style.display = 'block';
  el.style.color = 'var(--green)';
  el.style.background = 'var(--green-dim)';
  el.style.border = '1px solid rgba(34,197,94,0.35)';
  el.style.borderRadius = 'var(--rs)';
  el.style.padding = '10px 12px';
  el.style.marginBottom = '8px';
  el.style.fontWeight = '700';

  setTimeout(() => {
    _clearEnrolMessage();
  }, 10000);
}

function _resetButtonToDefault(disabled = true) {
  const btn = document.getElementById('confirm-btn');
  if (!btn) return;

  btn.textContent = 'Register Member';
  btn.disabled = disabled;
  btn.style.opacity = disabled ? '0.45' : '1';
  btn.style.background = '';
  btn.style.color = '';
}

function _setButtonRegistering() {
  const btn = document.getElementById('confirm-btn');
  if (!btn) return;

  btn.textContent = 'Registering…';
  btn.disabled = true;
  btn.style.opacity = '0.65';
  btn.style.background = '';
  btn.style.color = '';
}

function _setButtonRegistered() {
  const btn = document.getElementById('confirm-btn');
  if (!btn) return;

  btn.textContent = 'Registered ✓';
  btn.disabled = true;
  btn.style.opacity = '1';
  btn.style.background = 'var(--green)';
  btn.style.color = '#fff';

  setTimeout(() => {
    _resetButtonToDefault(true);
  }, 2500);
}

// ── Capture slot ──────────────────────────────────────────────────────────────
async function captureSlot(slot) {
  if (_captured[slot]) {
    toast('Remove this capture first, then take again.', 'err');
    return;
  }

  const name = _getName();

  _clearEnrolMessage();

  if (!name) {
    _setNameError('Enter member name first');
    return;
  }

  _clearNameError();

  const slotEl = document.getElementById('slot-' + slot);
  const iconEl = document.getElementById('slot-icon-' + slot);

  if (iconEl) iconEl.textContent = '⏳';
  if (slotEl) slotEl.style.pointerEvents = 'none';

  const fd = new FormData();
  fd.append('name', name);
  fd.append('slot', slot);

  try {
    const res = await fetch('/api/enrol/capture', {
      method: 'POST',
      body: fd,
      cache: 'no-store'
    });

    if (res.ok) {
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);

      if (slotEl) {
        slotEl.style.backgroundImage = `url(${url})`;
        slotEl.style.backgroundSize = 'cover';
        slotEl.style.backgroundPosition = 'center';
        slotEl.classList.add('captured');
        slotEl.style.pointerEvents = 'auto';
      }

      if (iconEl) iconEl.textContent = '✓';

      _captured[slot] = true;
      _updateProgress();
      return;
    }

    const d = await res.json().catch(() => ({}));
    _showError(d.detail || 'Capture failed');

    if (iconEl) {
      iconEl.textContent = slot === 1 ? '👤' : slot === 2 ? '↔' : '↕';
    }

    if (slotEl) slotEl.style.pointerEvents = 'auto';

  } catch (e) {
    _showError('Network error — try again');

    if (iconEl) iconEl.textContent = '!';
    if (slotEl) slotEl.style.pointerEvents = 'auto';
  }
}

// ── Update progress ───────────────────────────────────────────────────────────
function _updateProgress(skipButton = false) {
  const count = Object.keys(_captured).length;

  const bar = document.getElementById('capture-bar');
  const label = document.getElementById('capture-count');

  if (bar) bar.style.width = (count / 3 * 100) + '%';
  if (label) label.textContent = count + ' of 3 captured';

  // Skip button changes if caller is handling button state (e.g. after registration)
  if (skipButton) return;

  const btn = document.getElementById('confirm-btn');

  if (btn) {
    btn.disabled = count < 3;
    btn.style.opacity = count < 3 ? '0.45' : '1';

    if (count === 3) {
      btn.textContent = 'Register Member';
      btn.style.background = '';
      btn.style.color = '';
    }
  }
}

// ── Remove one captured slot ──────────────────────────────────────────────────
async function removeSlot(event, slot) {
  if (event) {
    event.preventDefault();
    event.stopPropagation();
  }

  const name = _getName();

  _clearEnrolMessage();

  const slotEl = document.getElementById('slot-' + slot);
  const iconEl = document.getElementById('slot-icon-' + slot);

  // Clear UI immediately so operator can retake even if backend slot remove fails.
  delete _captured[slot];
  _resetSlotUI(slot);
  _updateProgress();

  if (!name) return;

  const fd = new FormData();
  fd.append('name', name);
  fd.append('slot', slot);

  try {
    await fetch('/api/enrol/remove_slot', {
      method: 'POST',
      body: fd,
      cache: 'no-store'
    });
  } catch (e) {
    // Non-blocking
  } finally {
    if (slotEl) slotEl.style.pointerEvents = 'auto';
    if (iconEl && !_captured[slot]) {
      iconEl.textContent = slot === 1 ? '👤' : slot === 2 ? '↔' : '↕';
    }
  }
}

// ── Reset one slot UI ─────────────────────────────────────────────────────────
function _resetSlotUI(slot) {
  const el = document.getElementById('slot-' + slot);
  const ic = document.getElementById('slot-icon-' + slot);

  if (el) {
    el.classList.remove('captured');
    el.style.backgroundImage = '';
    el.style.backgroundSize = '';
    el.style.backgroundPosition = '';
    el.style.pointerEvents = 'auto';
  }

  if (ic) {
    ic.textContent = slot === 1 ? '👤' : slot === 2 ? '↔' : '↕';
  }
}

// ── Confirm enrolment ─────────────────────────────────────────────────────────
async function confirmEnrol() {
  const name = _getName();
  const phone = _getPhone();

  _clearEnrolMessage();

  if (!name) {
    _showError('Enter member name first.');
    return;
  }

  if (Object.keys(_captured).length < 3) {
    _showError('Capture all 3 poses first.');
    return;
  }

  _setButtonRegistering();

  // Member ID (member_code) is optional. Trim; blank is fine (auto-generated).
  const memberCodeInput = document.getElementById('enrol-member-code');
  const memberCode = memberCodeInput ? (memberCodeInput.value || '').trim() : '';

  const fd = new FormData();
  fd.append('name', name);
  fd.append('phone', phone);
  if (memberCodeInput) fd.append('member_code', memberCode);

  try {
    const res = await fetch('/api/enrol/confirm', {
      method: 'POST',
      body: fd,
      cache: 'no-store'
    });

    const d = await res.json().catch(() => ({}));

    if (!res.ok || d.ok === false) {
      // Duplicate Member ID → clear guidance, and DO NOT clear captures below
      // (the reset only runs on the success path), so admin can fix the ID and
      // confirm again without re-capturing faces.
      if (res.status === 409) {
        throw new Error('Member ID already in use. Please use a different ID.');
      }
      throw new Error(d.detail || d.message || 'Confirmation failed.');
    }

    const savedName = d.name || name;

    // ── Success path: manually reset everything WITHOUT calling resetEnrol() ──
    // resetEnrol() is intentionally NOT called here to guarantee /api/enrol/cancel
    // is never fired after a successful confirm. We do it all inline instead.

    // 1. Clear name field
    const nameInput = document.getElementById('enrol-name');
    if (nameInput) nameInput.value = '';

    const phoneInput = document.getElementById('enrol-phone');
    if (phoneInput) phoneInput.value = '';

    if (memberCodeInput) memberCodeInput.value = '';

    // 2. Clear captured state
    _captured = {};

    // 3. Reset slot UIs
    [1, 2, 3].forEach(slot => _resetSlotUI(slot));

    // 4. Reset progress bar and count label only (NOT the button)
    const bar   = document.getElementById('capture-bar');
    const label = document.getElementById('capture-count');
    if (bar)   bar.style.width = '0%';
    if (label) label.textContent = '0 of 3 captured';

    // 5. Show green "Registered ✓" button — after 2.5s it resets to disabled grey
    _setButtonRegistered();

    // 6. Show success banner
    _showSuccess(savedName);

    // 7. Toast
    if (typeof toast === 'function') {
      toast(savedName + ' registered successfully', 'ok');
    }

    // 8. Refresh member list
    await loadEnrolled();

  } catch (e) {
    _showError(e.message || 'Network error.');
    _resetButtonToDefault(false);
  }
}

// ── Full reset ────────────────────────────────────────────────────────────────
function resetEnrol(sendCancel = true, clearMessage = true) {
  const name = _getName();

  const phoneInput = document.getElementById('enrol-phone');
  if (phoneInput) phoneInput.value = '';

  if (sendCancel && name) {
    const fd = new FormData();
    fd.append('name', name);

    fetch('/api/enrol/cancel', {
      method: 'POST',
      body: fd,
      cache: 'no-store'
    }).catch(() => {});
  }

  _captured = {};

  // Check BEFORE _updateProgress so we can skip button stomping
  const btn = document.getElementById('confirm-btn');
  const alreadyRegistered = btn && btn.textContent.startsWith('Registered');

  [1, 2, 3].forEach(slot => _resetSlotUI(slot));
  _updateProgress(alreadyRegistered); // pass true to skip button when Registered ✓ is showing

  if (!alreadyRegistered) {
    _resetButtonToDefault(true);
  }

  if (clearMessage) {
    _clearEnrolMessage();
  }
}

// ── Remove member from enrol page ─────────────────────────────────────────────
async function removeMember(name) {
  if (!confirm(`Remove "${name}" from the system?\n\nFace data will be deleted.`)) return;

  const fd = new FormData();
  fd.append('name', name);

  try {
    const r = await fetch('/api/remove', {
      method: 'POST',
      body: fd,
      cache: 'no-store'
    });

    const d = await r.json().catch(() => ({}));

    if (r.ok && d.ok) {
      toast(`✓ "${name}" removed`, 'ok');
      await loadEnrolled();
    } else {
      toast(d.detail || 'Remove failed', 'err');
    }

  } catch (e) {
    toast('Network error', 'err');
  }
}

// ── Enrolled list ─────────────────────────────────────────────────────────────
async function loadEnrolled() {
  try {
    const r = await fetch('/api/members', { cache: 'no-store' });
    const data = await r.json();

    const countEl = document.getElementById('enrolled-count');
    if (countEl) countEl.textContent = data.length;

    const el = document.getElementById('enrolled-list');
    if (!el) return;

    if (!Array.isArray(data) || !data.length) {
      el.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--t3);padding:20px;font-size:12px">No members registered yet.</td></tr>';
      return;
    }

    el.innerHTML = data.map((m, i) => {
      const dt = m.joined_at ? m.joined_at.replace('T', ' ').substring(0, 16) : '—';

      const role = m.role || 'member';
      const isPriv = role === 'owner' || role === 'staff';

      const st = m.payment_status || 'no_payment';

      const pillMap = {
        active: 'pill-active',
        expiring_soon: 'pill-expiring',
        expired: 'pill-expired',
        no_payment: 'pill-inactive'
      };

      const pillCls = isPriv ? 'pill-active' : (pillMap[st] || 'pill-inactive');

      const pillLbl = isPriv
        ? role.charAt(0).toUpperCase() + role.slice(1)
        : ({
            active: 'Active',
            expiring_soon: 'Expiring',
            expired: 'Expired',
            no_payment: 'No Payment'
          }[st] || st);

      const safeName = String(m.name || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');

      return `<tr>
        <td style="color:var(--t3);font-family:var(--mono)">${i + 1}</td>
        <td class="td-name">${safeName}</td>
        <td style="font-size:11px;color:var(--t3);font-family:var(--mono)">${dt}</td>
        <td><span class="pill ${pillCls}">${pillLbl}</span></td>
        <td>
          <button class="btn btn-danger btn-sm" onclick="removeMember(this.closest('tr').querySelector('.td-name').textContent)">
            Remove
          </button>
        </td>
      </tr>`;
    }).join('');

  } catch (e) {
    const el = document.getElementById('enrolled-list');
    if (el) {
      el.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--red);padding:20px;font-size:12px">Could not load members.</td></tr>';
    }
  }
}

// ── Init ──────────────────────────────────────────────────────────────────────
const footYear = document.getElementById('foot-year');
if (footYear) footYear.textContent = new Date().getFullYear();

loadEnrolled();
setInterval(loadEnrolled, 20000);
