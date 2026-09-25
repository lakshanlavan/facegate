/**
 * admin.js — CodeGloFix Admin Panel
 * All dashboard, member, payment, logs, door, config logic
 */

'use strict';

// ── Init ──────────────────────────────────────────────────────────────────────
document.getElementById('foot-year').textContent = new Date().getFullYear();
dg_checkAuth(showMainUI);

document.getElementById('login-pw').addEventListener('keydown', e => {
  if (e.key === 'Enter') doLogin();
});

function doLogin() {
  const pw = document.getElementById('login-pw').value;
  dg_login(pw, showMainUI, msg => {
    document.getElementById('login-err').textContent = msg;
  });
}

function showMainUI() {
  document.getElementById('login-overlay').style.display = 'none';
  document.getElementById('main-ui').style.display = 'flex';
  loadAll();
}

function loadAll() {
  loadDashboard();
  loadMemberNames(false);
  loadPlans();
  setDefaultLogDates();
}

// ── Tab routing ───────────────────────────────────────────────────────────────
const TAB_LOADERS = {
  dashboard:   loadDashboard,
  members:     loadMembers,
  payments:    loadPayments,
  plans:       loadPlansManagement,
  access_logs: loadLogs,
  door:        loadDoorStatus,
  config:      loadGateConfig,
};

function showTab(id) {
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('.side-tab').forEach(t => t.classList.remove('active'));
  const sec = document.getElementById('tab-' + id);
  if (sec) sec.classList.add('active');
  document.querySelectorAll('.side-tab').forEach(t => {
    if (t.dataset.tab === id) t.classList.add('active');
  });
  if (TAB_LOADERS[id]) TAB_LOADERS[id]();
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
async function loadDashboard() {
  let _expiryDays = 7;   // fallback until revenue summary returns the configured value

  try {
    const r = await fetch('/api/revenue/summary');
    const d = await r.json();

    _expiryDays = d.expiry_warning_days || 7;

    _set('k-total',    d.total_members   ?? '—');
    _set('k-active',   d.active_members  ?? '—');
    _set('k-expired',  d.expired_members ?? '—');
    _set('k-expiring', d.expiring_soon   ?? '—');
    _set('k-today',    d.today_entries   ?? '—');

    _set(
      'k-rev-today',
      d.today_revenue != null ? 'Rs.' + Number(d.today_revenue).toLocaleString() : '—'
    );

    _set(
      'k-rev-month',
      d.month_revenue != null ? 'Rs.' + Number(d.month_revenue).toLocaleString() : '—'
    );
  } catch (e) {}

  try {
    const r = await fetch('/api/door/status');
    const d = await r.json();
    _set('k-door', d.emergency_lock ? '🔴 LOCKED' : (d.door_open ? '🟢 Open' : '⚫ Locked'));
  } catch (e) {}

  try {
    const r = await fetch('/api/members/expiring');   // backend uses configured default
    const rows = await r.json();
    const tbody = document.getElementById('expiring-list');
    if (!tbody) return;

    // Keep the table title in sync with the configured warning window
    const titleEl = document.getElementById('expiring-table-title');
    if (titleEl) titleEl.textContent = `⚠ Expiring within ${_expiryDays} days`;

    if (!rows.length) {
      tbody.innerHTML = `<tr class="empty-row"><td colspan="5">No memberships expiring within ${_expiryDays} days</td></tr>`;
    } else {
      tbody.innerHTML = rows.map(m => `<tr>
        <td class="td-name">${escapeHtml(m.name)}</td>
        <td>${escapeHtml(m.phone || '')}</td>
        <td>${escapeHtml(m.plan_name || '')}</td>
        <td><span class="pill pill-expiring">${escapeHtml(m.valid_until)}</span></td>
        <td><span class="pill ${m.days_left <= 2 ? 'pill-expired' : 'pill-expiring'}">${m.days_left}d</span></td>
      </tr>`).join('');
    }
  } catch (e) {}

  if (typeof window.renderDashboardVisualsNow === 'function') {
    await window.renderDashboardVisualsNow();
  }
}

// ── Members ───────────────────────────────────────────────────────────────────
let _allMembers = [];
let _memberCache = null;
let _memberCacheAt = 0;
let _memberCacheFetching = null;
const MEMBER_CACHE_MS = 10000;

async function getMembers(force = false) {
  const now = Date.now();
  if (!force && _memberCache && (now - _memberCacheAt) < MEMBER_CACHE_MS) {
    return _memberCache;
  }
  if (_memberCacheFetching) return _memberCacheFetching;

  _memberCacheFetching = fetch('/api/members', { cache: 'no-store' })
    .then(r => {
      if (!r.ok) throw new Error('members api failed');
      return r.json();
    })
    .then(data => {
      _memberCache = Array.isArray(data) ? data : [];
      _memberCacheAt = Date.now();
      _allMembers = _memberCache;
      return _memberCache;
    })
    .finally(() => { _memberCacheFetching = null; });

  return _memberCacheFetching;
}

function invalidateMembersCache() {
  _memberCache = null;
  _memberCacheAt = 0;
}

async function loadMembers(force = false) {
  try {
    _allMembers = await getMembers(force);
    renderMembers(_allMembers);
    populateDatalist('remove-member-list');
    populateDatalist('rename-member-list');
    populateDatalist('role-member-list');
    populateDatalist('pay-member-list');
    populateDatalist('edit-member-list');
  } catch (e) {}
}

function renderMembers(data) {
  const tbody = document.getElementById('member-list');
  if (!tbody) return;

  if (!data.length) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="9">No members registered</td></tr>';
    return;
  }

  tbody.innerHTML = data.map(m => {
    const role = String(m.role || 'member').toLowerCase();
    const isPriv = role === 'owner' || role === 'staff';
    const st = String(m.payment_status || 'no_payment').toLowerCase();

    const statusMap = {
      active: 'pill-active',
      expiring_soon: 'pill-expiring',
      expired: 'pill-expired',
      no_payment: 'pill-inactive',
      expires_today: 'pill-expiring',
      owner: 'pill-active',
      staff: 'pill-active',
    };

    const statusLbl = {
      active: 'Active',
      expiring_soon: 'Expiring',
      expired: 'Expired',
      no_payment: 'No Payment',
      expires_today: 'Today',
      owner: 'Owner Pass',
      staff: 'Staff Pass',
    };

    // Owner and staff are privileged access roles. They do not require
    // payment records, expiry dates, or days-left calculations.
    const pillCls = isPriv ? 'pill-active' : (statusMap[st] || 'pill-inactive');
    const pillLbl = isPriv ? (role === 'owner' ? 'Owner Pass' : 'Staff Pass') : (statusLbl[st] || st);

    const rolePill = `<span class="pill pill-${escapeAttr(role)}">${escapeHtml(role)}</span>`;
    const lastTs = m.last_access ? (m.last_access.ts || '').slice(0, 16).replace('T', ' ') : '';
    const days = isPriv ? 'Bypass' : (m.days_left != null ? m.days_left + 'd' : '');
    const until = isPriv ? 'No payment required' : (m.valid_until || '');

    const realIdx = _allMembers.findIndex(x => x.name === m.name);

    return `<tr>
      <td class="td-name">${escapeHtml(m.name)}</td>
      <td class="td-mono">${escapeHtml(m.member_code || '')}</td>
      <td>${escapeHtml(m.phone || '')}</td>
      <td>${rolePill}</td>
      <td><span class="pill ${pillCls}">${escapeHtml(pillLbl)}</span></td>
      <td class="td-mono">${escapeHtml(until)}</td>
      <td class="td-mono">${escapeHtml(days)}</td>
      <td class="td-mono">${escapeHtml(lastTs)}</td>
      <td>
        <button class="btn btn-outline btn-sm" data-idx="${realIdx}" onclick="prefillEditIdx(this)" title="Edit details">✎</button>
        <button class="btn btn-outline btn-sm" data-idx="${realIdx}" onclick="prefillRenameIdx(this)" title="Rename">✏</button>
        <button class="btn btn-outline btn-sm" data-idx="${realIdx}" data-role="${escapeAttr(role)}" onclick="prefillRoleIdx(this)" title="Set role">⚙</button>
        <button class="btn btn-danger btn-sm" data-idx="${realIdx}" onclick="removeMemberIdx(this)" title="Remove">✕</button>
      </td>
    </tr>`;
  }).join('');
}

function filterMembers() {
  const q = (document.getElementById('member-search')?.value || '').toLowerCase();
  renderMembers(_allMembers.filter(m => m.name.toLowerCase().includes(q)));
}

// ── Member name helpers ───────────────────────────────────────────────────────
async function loadMemberNames(force = false) {
  try {
    _allMembers = await getMembers(force);
    [
      'pay-member-list',
      'rename-member-list',
      'role-member-list',
      'remove-member-list',
    ].forEach(id => populateDatalist(id));
  } catch (e) {}
}

function populateDatalist(id) {
  const dl = document.getElementById(id);
  if (!dl) return;

  // Payment suggestions show normal members only. Owner/staff do not need
  // membership payments, although historical payments remain visible.
  const source = id === 'pay-member-list'
    ? _allMembers.filter(m => !['owner', 'staff'].includes(String(m.role || 'member').toLowerCase()))
    : _allMembers;

  dl.innerHTML = source.map(m => `<option value="${escapeAttr(m.name)}">`).join('');
}

// ── Rename / Role / Remove ────────────────────────────────────────────────────
function prefillRenameIdx(btn) {
  const m = _allMembers[parseInt(btn.dataset.idx)];
  if (!m) return;

  _set_input('rename-old', m.name);
  _set_input('rename-new', '');
  _flashCard('rename-card', 'rgba(8,145,178,0.35)');
  document.getElementById('rename-new')?.focus();
  toast('✏ Rename form filled — scroll down and enter new name', 'ok');
}

function prefillRoleIdx(btn) {
  const m = _allMembers[parseInt(btn.dataset.idx)];
  if (!m) return;

  _set_input('role-name', m.name);

  const sel = document.getElementById('role-select');
  if (sel) sel.value = btn.dataset.role || 'member';

  _flashCard('role-card', 'rgba(245,158,11,0.25)');
  toast('⚙ Role form filled — scroll down and select role', 'ok');
}

function removeMemberIdx(btn) {
  const m = _allMembers[parseInt(btn.dataset.idx)];
  if (!m) return;

  _set_input('remove-name', m.name);
  _flashCard('remove-card', 'rgba(239,68,68,0.3)');
  document.getElementById('remove-card')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  toast('✕ Remove form filled — confirm below', 'ok');
}

function prefillEditIdx(btn) {
  const m = _allMembers[parseInt(btn.dataset.idx)];
  if (!m) return;

  _set_input('edit-name', m.name);
  _set_input('edit-member-code', m.member_code || '');
  _set_input('edit-phone', m.phone || '');
  _flashCard('edit-details-card', 'rgba(34,197,94,0.25)');
  document.getElementById('edit-details-card')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  document.getElementById('edit-member-code')?.focus();
  toast('✎ Edit form filled — update Member ID / phone below', 'ok');
}

async function saveMemberDetails() {
  const name = _val('edit-name');
  const memberCode = _val('edit-member-code');
  const phone = _val('edit-phone');

  if (!name) {
    toast('Select a member first', 'err');
    return;
  }
  if (!memberCode) {
    toast('Member ID cannot be blank', 'err');
    return;
  }

  try {
    const r = await fetch('/api/members/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, member_code: memberCode, phone: phone }),
    });

    const d = await r.json().catch(() => ({}));

    if (r.ok && d.ok) {
      toast(`✓ Updated details for "${name}"`);
      _set_input('edit-name', '');
      _set_input('edit-member-code', '');
      _set_input('edit-phone', '');
      invalidateMembersCache();
      loadMembers(true);
      loadMemberNames(false);
    } else if (r.status === 409) {
      toast('Member ID already in use. Please use a different ID.', 'err');
    } else {
      toast(d.detail || 'Update failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

async function renameMember() {
  const oldName = _val('rename-old');
  const newName = _val('rename-new');

  if (!oldName || !newName) {
    toast('Both name fields are required', 'err');
    return;
  }

  if (oldName === newName) {
    toast('New name must differ from current name', 'err');
    return;
  }

  try {
    const r = await fetch('/api/members/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ old_name: oldName, new_name: newName }),
    });

    const d = await r.json();

    if (d.ok) {
      toast(`✓ Renamed "${oldName}" → "${newName}"`);
      _set_input('rename-old', '');
      _set_input('rename-new', '');
      invalidateMembersCache();
      loadMembers(true);
      loadMemberNames(false);
    } else {
      toast(d.detail || 'Rename failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

async function setMemberRole() {
  const name = _val('role-name');
  const role = document.getElementById('role-select')?.value;

  if (!name) {
    toast('Enter a member name', 'err');
    return;
  }

  try {
    const r = await fetch('/api/members/role', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, role }),
    });

    const d = await r.json();

    if (d.ok) {
      toast(`✓ ${name} — role set to "${role}"`);
      _set_input('role-name', '');
      invalidateMembersCache();
      loadMembers(true);
    } else {
      toast(d.detail || 'Role update failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

async function removeMemberByForm() {
  const name = _val('remove-name');

  if (!name) {
    toast('Enter a member name to remove', 'err');
    return;
  }

  await removeMember(name);
}

async function removeMember(name) {
  if (!confirm(`Remove "${name}" from the system?\n\nThis deletes the face scan.\nAccess history is preserved.`)) return;

  const fd = new FormData();
  fd.append('name', name);

  try {
    const r = await fetch('/api/members/remove', {
      method: 'POST',
      body: fd,
    });

    if (r.status === 401) {
      toast('Session expired — please log in again', 'err');
      setTimeout(() => location.reload(), 1500);
      return;
    }

    const d = await r.json();

    if (r.ok && d.ok) {
      toast(`✓ "${name}" removed`);

      if (_val('remove-name') === name) {
        _set_input('remove-name', '');
      }

      invalidateMembersCache();
      loadMembers(true);
      loadMemberNames(false);
      loadDashboard();
    } else {
      toast(d.detail || 'Remove failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

// ── Payments ──────────────────────────────────────────────────────────────────
let _plans = [];

async function loadPlans() {
  try {
    const r = await fetch('/api/plans');
    _plans = await r.json();

    const sel = document.getElementById('pay-plan');
    if (!sel) return;

    sel.innerHTML = '<option value="">— Select plan —</option>' +
      _plans.map(p => `<option value="${p.id}" data-days="${p.duration_days}" data-price="${p.price}">${escapeHtml(p.plan_name)} (${p.duration_days}d — Rs.${Number(p.price).toLocaleString()})</option>`).join('');
  } catch (e) {}
}

function autoFillDates() {
  const sel = document.getElementById('pay-plan');
  const opt = sel?.options[sel.selectedIndex];

  if (!opt || !opt.dataset.days) {
    toast('Select a plan first', 'err');
    return;
  }

  const days = parseInt(opt.dataset.days);
  const from = new Date();
  const until = new Date();

  until.setDate(from.getDate() + days - 1);

  _set_input('pay-from', from.toISOString().slice(0, 10));
  _set_input('pay-until', until.toISOString().slice(0, 10));
  _set_input('pay-amount', opt.dataset.price || '');
}

async function loadPayments() {
  try {
    const r = await fetch('/api/payments');
    const rows = await r.json();
    const tbody = document.getElementById('payments-list');

    if (!tbody) return;

    if (!rows.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="7">No payments recorded</td></tr>';
      return;
    }

    tbody.innerHTML = rows.map(p => `<tr>
      <td class="td-name">${escapeHtml(p.member_name || '')}</td>
      <td>${escapeHtml(p.plan_name || 'Custom')}</td>
      <td style="color:var(--accent);font-weight:600">Rs.${Number(p.amount || 0).toLocaleString()}</td>
      <td>${escapeHtml(p.payment_method || '')}</td>
      <td class="td-mono">${escapeHtml(p.valid_from || '')}</td>
      <td class="td-mono">${escapeHtml(p.valid_until || '')}</td>
      <td class="td-mono">${escapeHtml((p.payment_date || '').slice(0, 10))}</td>
    </tr>`).join('');
  } catch (e) {}
}

async function savePayment() {
  const name = _val('pay-member');
  const amount = parseFloat(_val('pay-amount') || '0');
  const from = _val('pay-from');
  const until = _val('pay-until');
  const method = _val('pay-method') || 'cash';
  const note = _val('pay-note');
  const planId = (() => {
    const v = document.getElementById('pay-plan')?.value;
    return v ? parseInt(v) : null;
  })();

  if (!name || !amount || !from || !until) {
    toast('Fill all required fields', 'err');
    return;
  }

  const selectedMember = (_allMembers || []).find(
    m => String(m.name || '').toLowerCase() === name.toLowerCase()
  );
  const selectedRole = String(selectedMember?.role || 'member').toLowerCase();

  if (selectedRole === 'owner' || selectedRole === 'staff') {
    toast('Owner/Staff access does not require membership payment.', 'err');
    return;
  }

  try {
    const r = await fetch('/api/payments/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        member_name: name,
        amount,
        valid_from: from,
        valid_until: until,
        payment_method: method,
        note,
        plan_id: planId,
      }),
    });

    const d = await r.json();

    if (d.ok) {
      toast(`Payment saved for ${name}`);
      _set_input('pay-member', '');
      _set_input('pay-amount', '');
      _set_input('pay-note', '');
      loadPayments();
      loadDashboard();
    } else {
      toast(d.detail || 'Save failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

// ── Plans management ────────────────────────────────────────────────────────
// Edits membership_plans (the single source of truth). Price/name changes here
// affect NEW payments only — existing payments.amount is a stored snapshot and
// is never touched. Plans are deactivated, never hard-deleted.
async function loadPlansManagement() {
  const tbody = document.getElementById('plans-list');
  if (!tbody) return;
  try {
    const r = await fetch('/api/plans/manage', { cache: 'no-store' });

    if (!r.ok) {
      const msg = r.status === 401
        ? 'Session expired — please log in again'
        : `Could not load plans (HTTP ${r.status})`;
      tbody.innerHTML = `<tr class="empty-row"><td colspan="6">${msg}</td></tr>`;
      toast(msg, 'err');
      return;
    }

    const rows = await r.json();

    if (!Array.isArray(rows) || !rows.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No plans defined</td></tr>';
      return;
    }

    tbody.innerHTML = rows.map(p => {
      const active = Number(p.is_active) === 1;
      return `<tr>
        <td><input class="form-input" type="text" id="plan-name-${p.id}" value="${escapeAttr(p.plan_name)}"></td>
        <td><input class="form-input" type="number" id="plan-days-${p.id}" value="${Number(p.duration_days)}" style="max-width:110px"></td>
        <td><input class="form-input" type="number" id="plan-price-${p.id}" value="${Number(p.price)}" style="max-width:130px"></td>
        <td><input class="form-input" type="text" id="plan-desc-${p.id}" value="${escapeAttr(p.description || '')}"></td>
        <td><span style="color:${active ? 'var(--green)' : 'var(--t3)'};font-weight:600">${active ? 'Active' : 'Inactive'}</span></td>
        <td style="white-space:nowrap">
          <button class="btn btn-primary" onclick="savePlanRow(${p.id})">Save</button>
          <button class="btn btn-outline" onclick="togglePlanActive(${p.id}, ${active ? 'false' : 'true'})">${active ? 'Deactivate' : 'Activate'}</button>
        </td>
      </tr>`;
    }).join('');
  } catch (e) {
    tbody.innerHTML = '<tr class="empty-row"><td colspan="6">Could not load plans — check the connection and reload.</td></tr>';
    toast('Could not load plans', 'err');
  }
}

async function createPlan() {
  const name = _val('plan-new-name');
  const days = parseInt(_val('plan-new-days') || '0');
  const price = parseFloat(_val('plan-new-price') || '-1');
  const desc = _val('plan-new-desc');

  if (!name || !days || days <= 0 || !(price >= 0)) {
    toast('Enter a name, duration > 0 and price ≥ 0', 'err');
    return;
  }

  try {
    const r = await fetch('/api/plans/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ plan_name: name, duration_days: days, price, description: desc }),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      toast(`Plan "${name}" added`);
      _set_input('plan-new-name', '');
      _set_input('plan-new-days', '');
      _set_input('plan-new-price', '');
      _set_input('plan-new-desc', '');
      loadPlansManagement();
      loadPlans();   // refresh Payments dropdown (active plans)
    } else {
      toast(d.detail || 'Create failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

async function savePlanRow(id) {
  const name = _val(`plan-name-${id}`);
  const days = parseInt(_val(`plan-days-${id}`) || '0');
  const price = parseFloat(_val(`plan-price-${id}`) || '-1');
  const desc = _val(`plan-desc-${id}`);

  if (!name || !days || days <= 0 || !(price >= 0)) {
    toast('Enter a name, duration > 0 and price ≥ 0', 'err');
    return;
  }

  try {
    const r = await fetch('/api/plans/update', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, plan_name: name, duration_days: days, price, description: desc }),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      toast(`Plan "${name}" updated`);
      loadPlansManagement();
      loadPlans();   // refresh Payments dropdown (active plans)
    } else {
      toast(d.detail || 'Update failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

async function togglePlanActive(id, makeActive) {
  try {
    const r = await fetch('/api/plans/toggle-active', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, is_active: makeActive }),
    });
    const d = await r.json();
    if (r.ok && d.ok) {
      toast(makeActive ? 'Plan activated' : 'Plan deactivated');
      loadPlansManagement();
      loadPlans();   // refresh Payments dropdown (active plans)
    } else {
      toast(d.detail || 'Update failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

// ── Access Logs ───────────────────────────────────────────────────────────────
function setDefaultLogDates() {
  const today = new Date().toISOString().slice(0, 10);
  const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);
  _set_input('log-from', weekAgo);
  _set_input('log-to', today);
}

function setLogToday() {
  const today = new Date().toISOString().slice(0, 10);
  _set_input('log-from', today);
  _set_input('log-to', today);
  loadLogs();
}

function setLogWeek() {
  const today = new Date().toISOString().slice(0, 10);
  const weekAgo = new Date(Date.now() - 7 * 86400000).toISOString().slice(0, 10);
  _set_input('log-from', weekAgo);
  _set_input('log-to', today);
  loadLogs();
}

async function loadLogs() {
  const from = _val('log-from');
  const to   = _val('log-to');
  const name = _val('log-name');

  let url = '/api/access_logs?limit=500&';
  if (from) url += `date_from=${from}&`;
  if (to)   url += `date_to=${to}&`;
  if (name) url += `name=${encodeURIComponent(name)}&`;

  try {
    const r = await fetch(url);
    const rows = await r.json();
    const tbody = document.getElementById('log-list');
    if (!tbody) return;

    // ── Compute stats from returned rows ──────────────────────────────────────
    let granted = 0, denied = 0, doorOpened = 0;
    rows.forEach(e => {
      const isGranted = e.decision && (
        e.decision.startsWith('access_granted') || e.decision.startsWith('granted')
      );
      const isManual = e.decision?.includes('manual') || e.reason?.includes('manual');
      if (isGranted) granted++;
      else if (!isManual) denied++;
      if (e.door_opened) doorOpened++;
    });

    const statsRow = document.getElementById('log-stats-row');
    if (statsRow) statsRow.style.display = '';
    _set('ls-total',   rows.length);
    _set('ls-granted', granted);
    _set('ls-denied',  denied);
    _set('ls-door',    doorOpened);

    if (!rows.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="5">No records found for the selected filters</td></tr>';
      return;
    }

    const today = new Date().toISOString().slice(0, 10);

    tbody.innerHTML = rows.map(e => {
      const isGranted = e.decision && (
        e.decision.startsWith('access_granted') || e.decision.startsWith('granted')
      );
      const isManual = e.decision?.includes('manual') || e.reason?.includes('manual');

      const ts       = e.ts || '';
      const datePart = ts.slice(0, 10);
      const timePart = ts.slice(11, 19) || ts.slice(0, 19);
      const isToday  = datePart === today;

      const initials   = dg_initials(e.member_name || '?');
      const decision   = isGranted ? 'Granted' : (isManual ? 'Manual' : 'Denied');
      const pillCls    = isGranted ? 'pill-log-granted' : (isManual ? 'pill-log-manual' : 'pill-log-denied');
      const decIcon    = isGranted ? '✓' : (isManual ? '⊙' : '✕');
      const reasonText = _formatReason(e.reason || '');

      const doorCell = e.door_opened
        ? '<span class="log-door-icon" title="Door opened">⊙</span>'
        : '<span style="color:var(--border-2);font-size:14px;display:block;text-align:center">—</span>';

      return `<tr>
        <td>
          <div class="log-time-wrap">
            <div class="log-time-main">${escapeHtml(timePart)}</div>
            ${!isToday ? `<div class="log-time-date">${escapeHtml(datePart)}</div>` : ''}
          </div>
        </td>
        <td>
          <div class="log-name-cell">
            <span class="log-avatar">${escapeHtml(initials)}</span>
            <span style="color:var(--t1);font-weight:600">${escapeHtml(e.member_name || '')}</span>
          </div>
        </td>
        <td><span class="pill ${pillCls}">${escapeHtml(decIcon + ' ' + decision)}</span></td>
        <td style="font-size:11px;color:var(--t2)">${escapeHtml(reasonText)}</td>
        <td style="text-align:center">${doorCell}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    const tbody = document.getElementById('log-list');
    if (tbody) tbody.innerHTML = '<tr class="empty-row"><td colspan="5">Could not load access logs</td></tr>';
  }
}

// ── Human-readable reason codes ───────────────────────────────────────────────
function _formatReason(reason) {
  const map = {
    'granted_valid':           'Valid membership',
    'granted_expiring_soon':   'Expiring soon',
    'granted_owner':           'Owner access',
    'granted_staff':           'Staff access',
    'denied_expired':          'Membership expired',
    'denied_no_payment':       'No payment record',
    'denied_inactive':         'Inactive account',
    'denied_unknown':          'Not registered',
    'denied_emergency_lock':   'Emergency lock active',
    'manual_open':             'Admin — manual open',
    'manual_lock':             'Admin — force lock',
    'relay_test':              'Relay test pulse',
    'emergency_lock_on':       'Emergency lock enabled',
    'emergency_lock_off':      'Emergency lock disabled',
    'system_start':            'System start',
  };
  if (!reason) return '—';
  return map[reason] || reason.replace(/_/g, ' ');
}

function exportLogs() {
  const from = _val('log-from');
  const to   = _val('log-to');
  const name = _val('log-name');

  let url = '/api/export_csv?';
  if (from) url += `date_from=${from}&`;
  if (to)   url += `date_to=${to}&`;
  if (name) url += `name=${encodeURIComponent(name)}&`;

  window.location.href = url;
}

// ── Door Control ──────────────────────────────────────────────────────────────
async function loadDoorStatus() {
  try {
    const r = await fetch('/api/door/status');
    if (!r.ok) throw new Error('door status request failed');

    const d = await r.json();

    const isEmergency = !!d.emergency_lock;
    const isOpen = !!d.door_open;
    const isConnected = !!d.connected;
    const modeText = isConnected ? 'Controller Connected' : 'Controller Offline';

    const card = document.getElementById('door-state-card');
    const ring = document.getElementById('door-state-ring');
    const dot = document.getElementById('door-dot');
    const txt = document.getElementById('door-status-text');
    const sub = document.getElementById('door-status-sub');
    const mode = document.getElementById('door-mode-badge');
    const emergencyState = document.getElementById('door-emergency-state');
    const liveNote = document.getElementById('door-live-note');

    if (card) {
      card.classList.remove('open', 'emergency');
      if (isEmergency) card.classList.add('emergency');
      else if (isOpen) card.classList.add('open');
    }

    if (ring) {
      ring.classList.remove('open', 'locked', 'emergency');
      ring.classList.add(isEmergency ? 'emergency' : (isOpen ? 'open' : 'locked'));
    }

    if (dot) {
      dot.className = 'door-dot ' + ((isOpen && !isEmergency) ? 'open' : 'locked');
    }

    if (txt) {
      txt.textContent = isEmergency ? 'Emergency Locked' : (isOpen ? 'Door Open' : 'Locked');
      txt.style.color = isEmergency ? 'var(--red)' : (isOpen ? 'var(--green)' : 'var(--t1)');
    }

    if (sub) {
      sub.textContent = isEmergency
        ? 'All automatic member access is blocked. Only admin override/manual actions should be used.'
        : isOpen
          ? 'Door signal sent to relay controller. The relay hold time is configured in the controller firmware.'
          : 'Relay is ready. Face recognition access will open the door only for authorised members.';
    }

    if (mode) {
      mode.textContent = modeText;
      mode.className = 'door-pro-pill ' + (isConnected ? 'good' : 'warn');
    }

    if (emergencyState) {
      emergencyState.textContent = isEmergency ? 'Emergency: Active' : 'Emergency: Off';
      emergencyState.className = 'door-pro-pill ' + (isEmergency ? 'danger' : 'good');
    }

    if (liveNote) {
      liveNote.textContent = isEmergency ? 'Access Blocked' : (isOpen ? 'Relay Active' : 'System Ready');
      liveNote.className = 'door-pro-pill ' + (isEmergency ? 'danger' : 'good');
    }

    const set = (id, value) => {
      const el = document.getElementById(id);
      if (el) el.textContent = value ?? '—';
    };

    set('door-port-val', d.port || '—');
    set('door-baud-val', d.baudrate != null ? d.baudrate + ' baud' : '—');
    set('door-last-open-val', d.last_open_at ? d.last_open_at.replace('T', ' ').slice(0, 19) : '—');
    set('door-last-action-val', d.last_action || 'system');

    const eCard = document.getElementById('emergency-card');
    const title = document.getElementById('emergency-title');
    const desc = document.getElementById('emergency-desc');
    const icon = document.getElementById('emergency-icon');
    const badge = document.getElementById('emergency-badge');
    const btnOn = document.getElementById('btn-emg-lock');
    const btnOff = document.getElementById('btn-emg-unlock');

    if (eCard) eCard.classList.toggle('active', isEmergency);

    if (isEmergency) {
      if (icon) icon.textContent = '🚨';
      if (title) {
        title.textContent = 'EMERGENCY LOCK ACTIVE';
        title.style.color = 'var(--red)';
      }
      if (badge) badge.style.display = '';
      if (desc) desc.textContent = 'All face recognition access is blocked. No member can enter automatically. Disable only after the issue is resolved.';
      if (btnOn) btnOn.style.display = 'none';
      if (btnOff) btnOff.style.display = '';
    } else {
      if (icon) icon.textContent = '🔒';
      if (title) {
        title.textContent = 'Emergency Lock';
        title.style.color = 'var(--t1)';
      }
      if (badge) badge.style.display = 'none';
      if (desc) desc.textContent = 'When enabled, face recognition cannot open the door. All access attempts are denied and logged.';
      if (btnOn) btnOn.style.display = '';
      if (btnOff) btnOff.style.display = 'none';
    }
  } catch (e) {
    toast('Door status unavailable', 'err');
  }
}

async function manualOpen() {
  try {
    const r = await fetch('/api/door/open', { method: 'POST' });
    const d = await r.json();
    toast(d.message || 'Door open signal sent to relay controller');
    setTimeout(loadDoorStatus, 600);
  } catch (e) {
    toast('Door open failed', 'err');
  }
}

async function setEmergency(enable) {
  const url = enable ? '/api/door/emergency_lock' : '/api/door/emergency_unlock';

  try {
    await fetch(url, { method: 'POST' });
    toast(enable ? '⚠ Emergency lock ENABLED' : '✓ Emergency lock disabled');
    loadDoorStatus();
  } catch (e) {
    toast('Error', 'err');
  }
}

// ── Gate Config ───────────────────────────────────────────────────────────────
async function loadGateConfig() {
  try {
    const r = await fetch('/api/gate_config');
    const d = await r.json();

    _set_input('cfg-area', d.min_face_area ?? '');
    _set_input('cfg-roi', d.center_roi ?? '');
    _set_input('cfg-cooldown', d.cooldown_secs ?? '');
    _set_input('cfg-warn-days', d.expiry_warning_days ?? '');
    _set_input('cfg-baudrate', d.relay_baudrate ?? '');
    _set_input('cfg-relay-port', d.relay_port ?? '');
  } catch (e) {}
}

async function saveGateConfig() {
  const body = {
    min_face_area: parseInt(_val('cfg-area') || '0'),
    center_roi: parseFloat(_val('cfg-roi') || '0'),
    cooldown_secs: parseFloat(_val('cfg-cooldown') || '0'),
    expiry_warning_days: parseInt(_val('cfg-warn-days') || '0'),
    relay_baudrate: parseInt(_val('cfg-baudrate') || '115200'),
    relay_port: _val('cfg-relay-port') || 'auto',
  };

  try {
    const r = await fetch('/api/gate_config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    const d = await r.json();

    if (d.ok) {
      toast('Configuration saved');
    } else {
      toast(d.detail || 'Save failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

// ── Password ──────────────────────────────────────────────────────────────────
async function changePassword() {
  const cur = _val('pw-current');
  const nw = _val('pw-new');
  const conf = _val('pw-confirm');

  if (nw !== conf) {
    toast('Passwords do not match', 'err');
    return;
  }

  if (nw.length < 6) {
    toast('Password must be at least 6 characters', 'err');
    return;
  }

  const fd = new FormData();
  fd.append('current_password', cur);
  fd.append('new_password', nw);

  try {
    const r = await fetch('/api/settings/password', {
      method: 'POST',
      body: fd,
    });

    const d = await r.json();

    if (d.ok) {
      toast('Password updated');
      _set_input('pw-current', '');
      _set_input('pw-new', '');
      _set_input('pw-confirm', '');
    } else {
      toast(d.detail || 'Update failed', 'err');
    }
  } catch (e) {
    toast('Network error', 'err');
  }
}

// ── Auto-refresh ──────────────────────────────────────────────────────────────
setInterval(loadDashboard, 30000);

// ── Micro helpers ─────────────────────────────────────────────────────────────
function _set(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

function _val(id) {
  return (document.getElementById(id)?.value || '').trim();
}

function _set_input(id, v) {
  const el = document.getElementById(id);
  if (el) el.value = v;
}

function _flashCard(cardId, color) {
  const card = document.getElementById(cardId);
  if (!card) return;

  card.style.borderColor = color;
  card.style.boxShadow = '0 0 0 2px ' + color.replace(')', ',0.15)').replace('rgb', 'rgba');
  card.scrollIntoView({ behavior: 'smooth', block: 'center' });

  setTimeout(() => {
    card.style.borderColor = '';
    card.style.boxShadow = '';
  }, 2000);
}

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function escapeAttr(value) {
  return escapeHtml(value);
}


// === DASHBOARD VISUALS START ===
(function () {
  let visualTimerStarted = false;

  async function renderDashboardVisualsNow() {
    const donut = document.getElementById('membership-donut');
    const legend = document.getElementById('membership-legend');
    const bars = document.getElementById('expiry-risk-bars');

    // Dashboard visual HTML not currently visible/loaded
    if (!donut || !legend || !bars) return;

    try {
      const members = await getMembers(false);
      const list = Array.isArray(members) ? members : [];

      const counts = {
        active: 0,
        expiring: 0,
        expired: 0,
        noPayment: 0
      };

      const risk = {
        expired: 0,
        today: 0,
        oneThree: 0,
        fourSeven: 0,
        eightThirty: 0
      };

      list.forEach(m => {
        const role = String(m.role || 'member').toLowerCase();
        const status = String(m.payment_status || 'no_payment').toLowerCase();
        const privileged = role === 'owner' || role === 'staff';
        const daysLeft = getDaysLeft(m);

        // Membership health count
        if (privileged || status === 'active') {
          counts.active++;
        } else if (status === 'expiring_soon' || status === 'expires_today') {
          counts.expiring++;
        } else if (status === 'expired') {
          counts.expired++;
        } else {
          counts.noPayment++;
        }

        // Expiry risk buckets
        if (!privileged) {
          if (status === 'expired' || (daysLeft !== null && daysLeft < 0)) {
            risk.expired++;
          } else if (status === 'expires_today' || daysLeft === 0) {
            risk.today++;
          } else if (daysLeft !== null && daysLeft >= 1 && daysLeft <= 3) {
            risk.oneThree++;
          } else if (daysLeft !== null && daysLeft >= 4 && daysLeft <= 7) {
            risk.fourSeven++;
          } else if (daysLeft !== null && daysLeft >= 8 && daysLeft <= 30) {
            risk.eightThirty++;
          }
        }
      });

      const total = counts.active + counts.expiring + counts.expired + counts.noPayment;
      const activePercent = total ? Math.round((counts.active / total) * 100) : 0;

      setText('membership-total-pill', 'Total ' + total.toLocaleString());
      setText('donut-active-percent', activePercent + '%');

      const activeDeg = total ? (counts.active / total) * 360 : 0;
      const expiringDeg = total ? (counts.expiring / total) * 360 : 0;
      const expiredDeg = total ? (counts.expired / total) * 360 : 0;

      const a1 = activeDeg;
      const a2 = a1 + expiringDeg;
      const a3 = a2 + expiredDeg;

      donut.style.background = `conic-gradient(
        var(--green) 0deg ${a1}deg,
        var(--amber) ${a1}deg ${a2}deg,
        var(--red) ${a2}deg ${a3}deg,
        rgba(85,96,121,0.75) ${a3}deg 360deg
      )`;

      legend.innerHTML = `
        <div class="legend-row">
          <span class="legend-dot2" style="background:var(--green)"></span>
          <span class="legend-name">Active</span>
          <span class="legend-value">${counts.active.toLocaleString()}</span>
        </div>
        <div class="legend-row">
          <span class="legend-dot2" style="background:var(--amber)"></span>
          <span class="legend-name">Expiring Soon</span>
          <span class="legend-value">${counts.expiring.toLocaleString()}</span>
        </div>
        <div class="legend-row">
          <span class="legend-dot2" style="background:var(--red)"></span>
          <span class="legend-name">Expired</span>
          <span class="legend-value">${counts.expired.toLocaleString()}</span>
        </div>
        <div class="legend-row">
          <span class="legend-dot2" style="background:rgba(85,96,121,0.95)"></span>
          <span class="legend-name">No Payment</span>
          <span class="legend-value">${counts.noPayment.toLocaleString()}</span>
        </div>
      `;

      const riskTotal = risk.expired + risk.today + risk.oneThree + risk.fourSeven + risk.eightThirty;
      setText('risk-total-pill', 'Risk ' + riskTotal.toLocaleString());

      const maxRisk = Math.max(1, risk.expired, risk.today, risk.oneThree, risk.fourSeven, risk.eightThirty);

      const riskRows = [
        ['Expired', risk.expired, 'danger'],
        ['Today', risk.today, 'danger'],
        ['1–3 days', risk.oneThree, 'warn'],
        ['4–7 days', risk.fourSeven, 'warn'],
        ['8–30 days', risk.eightThirty, 'good']
      ];

      bars.innerHTML = riskRows.map(([label, value, cls]) => {
        const width = value > 0 ? Math.max(4, Math.round((value / maxRisk) * 100)) : 0;
        return `
          <div class="risk-row">
            <div class="risk-label">${label}</div>
            <div class="risk-track">
              <div class="risk-fill ${cls}" style="width:${width}%"></div>
            </div>
            <div class="risk-count">${Number(value).toLocaleString()}</div>
          </div>
        `;
      }).join('');

    } catch (err) {
      legend.innerHTML = '<div class="analytics-loading">Unable to load membership health.</div>';
      bars.innerHTML = '<div class="analytics-loading">Unable to load expiry risk.</div>';
      setText('membership-total-pill', 'Total —');
      setText('risk-total-pill', 'Risk —');
    }
  }

  function getDaysLeft(m) {
    if (m.days_left !== undefined && m.days_left !== null && m.days_left !== '') {
      const n = Number(m.days_left);
      return Number.isFinite(n) ? n : null;
    }

    if (m.valid_until) {
      const today = new Date();
      const valid = new Date(String(m.valid_until) + 'T00:00:00');

      if (!Number.isNaN(valid.getTime())) {
        today.setHours(0, 0, 0, 0);
        return Math.round((valid - today) / 86400000);
      }
    }

    return null;
  }

  function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value;
  }

  // Make callable from browser console if needed
  window.renderDashboardVisualsNow = renderDashboardVisualsNow;

  // No separate visual refresh timer. loadDashboard() already refreshes visuals,
  // and setInterval(loadDashboard, 30000) is the single dashboard timer.
  if (!visualTimerStarted) {
    visualTimerStarted = true;
  }
})();
// === DASHBOARD VISUALS END ===

