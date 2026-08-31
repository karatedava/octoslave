/**
 * OctoSlave Web UI — in-app updater.
 *
 * Asks /api/update/check once on load. If a newer release exists, a pill drops
 * into the sidebar; clicking it opens a dialog with the release notes and a
 * single Update button. The backend knows how this copy was installed (pip,
 * pipx, Homebrew, AppImage, .app, Windows installer) and picks the right
 * upgrade path — see octoslave/updater.py.
 *
 * Self-contained on purpose: it renders its own DOM and is imported for side
 * effects only, so nothing else in the UI has to know it exists.
 */

import { renderMarkdown, esc } from './utils.js?v=20260429';

let info = null;          // last /api/update/check payload
let pollTimer = null;

// ──────────────────────────────────────────────────────────────
// Sidebar pill
// ──────────────────────────────────────────────────────────────

function renderPill() {
  const host = document.getElementById('update-slot');
  if (!host) return;
  if (!info || !info.available || info.skipped) {
    host.innerHTML = '';
    return;
  }
  host.innerHTML = `
    <button class="update-pill" id="update-pill" title="OctoSlave ${esc(info.latest)} is available">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 12a9 9 0 1 1-3.5-7.1"/><polyline points="21 3 21 9 15 9"/>
      </svg>
      <span>Update to ${esc(info.latest)}</span>
    </button>`;
  document.getElementById('update-pill').addEventListener('click', openDialog);
}

// ──────────────────────────────────────────────────────────────
// Dialog
// ──────────────────────────────────────────────────────────────

function ensureDialog() {
  let el = document.getElementById('update-modal');
  if (el) return el;
  el = document.createElement('div');
  el.id = 'update-modal';
  el.className = 'update-modal';
  el.innerHTML = `
    <div class="update-backdrop" data-close="1"></div>
    <div class="update-card" role="dialog" aria-modal="true" aria-labelledby="update-title">
      <div class="update-head">
        <h3 id="update-title">Update available</h3>
        <button class="update-close" data-close="1" aria-label="Close">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2.5" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
      </div>
      <div class="update-body" id="update-body"></div>
      <div class="update-foot" id="update-foot"></div>
    </div>`;
  document.body.appendChild(el);
  el.addEventListener('click', (e) => {
    if (e.target.closest('[data-close]')) closeDialog();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && el.classList.contains('open')) closeDialog();
  });
  return el;
}

function openDialog() {
  ensureDialog().classList.add('open');
  renderIdle();
}

function closeDialog() {
  const el = document.getElementById('update-modal');
  if (el) el.classList.remove('open');
  // A running update keeps polling in the background — closing the dialog only
  // hides it, it never abandons an upgrade half-applied.
}

function renderIdle() {
  const body = document.getElementById('update-body');
  const foot = document.getElementById('update-foot');
  if (!body || !foot || !info) return;

  const notes = (info.notes || '').trim();
  body.innerHTML = `
    <div class="update-versions">
      <span class="update-ver-old">${esc(info.current)}</span>
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></svg>
      <span class="update-ver-new">${esc(info.latest)}</span>
      <span class="update-method">via ${esc(info.method_label)}</span>
    </div>
    ${notes ? `<div class="update-notes">${renderMarkdown(notes)}</div>` : ''}
    ${info.self_update ? '' : `<div class="update-warn">${esc(info.hint || 'This install must be updated manually.')}</div>`}`;

  const quitNote = info.quits_app
    ? '<div class="update-hint">OctoSlave will close and reopen once the update is installed.</div>'
    : '<div class="update-hint">Restart OctoSlave afterwards to run the new version.</div>';

  foot.innerHTML = info.self_update
    ? `${quitNote}
       <div class="update-actions">
         <button class="btn-secondary btn-sm" id="update-skip">Skip ${esc(info.latest)}</button>
         <a class="btn-secondary btn-sm" href="${esc(info.url)}" target="_blank" rel="noopener">Release page</a>
         <button class="btn-primary btn-sm" id="update-go">Update now</button>
       </div>`
    : `<div class="update-actions">
         <a class="btn-primary btn-sm" href="${esc(info.url)}" target="_blank" rel="noopener">Open release page</a>
       </div>`;

  document.getElementById('update-go')?.addEventListener('click', startUpdate);
  document.getElementById('update-skip')?.addEventListener('click', async () => {
    await fetch('/api/update/dismiss', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ skip: info.latest }),
    });
    info.skipped = true;
    renderPill();
    closeDialog();
  });
}

// ──────────────────────────────────────────────────────────────
// Running the update
// ──────────────────────────────────────────────────────────────

async function startUpdate() {
  const foot = document.getElementById('update-foot');
  if (foot) foot.innerHTML = '<div class="update-hint">Starting…</div>';
  try {
    const r = await fetch('/api/update/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ version: info.latest }),
    });
    renderProgress(await r.json());
  } catch (e) {
    renderProgress({ state: 'error', error: String(e), log: [] });
    return;
  }
  poll();
}

function poll() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    try {
      const st = await (await fetch('/api/update/status')).json();
      renderProgress(st);
      if (st.state === 'running') poll();
      else if (st.state === 'done') onFinished(st);
    } catch {
      // The server going away mid-update is expected on the paths that restart
      // the app — keep polling; the browser reconnects when it comes back.
      poll();
    }
  }, 900);
}

function renderProgress(st) {
  const body = document.getElementById('update-body');
  const foot = document.getElementById('update-foot');
  if (!body || !foot) return;

  const pct = st.pct == null ? null : Math.max(0, Math.min(100, st.pct));
  const log = (st.log || []).slice(-200).join('\n');
  body.innerHTML = `
    <div class="update-progress">
      <div class="update-bar"><div class="update-bar-fill" style="width:${pct == null ? 0 : pct}%"></div></div>
      <div class="update-stage">${esc(st.stage || st.state || '')}${pct == null ? '' : ` · ${Math.round(pct)}%`}</div>
    </div>
    <pre class="update-log" id="update-log">${esc(log)}</pre>
    ${st.error ? `<div class="update-warn">${esc(st.error)}</div>` : ''}`;
  const pre = document.getElementById('update-log');
  if (pre) pre.scrollTop = pre.scrollHeight;

  if (st.state === 'running') {
    foot.innerHTML = '<div class="update-hint">Do not close OctoSlave while the update runs.</div>';
  } else if (st.state === 'error') {
    foot.innerHTML = `
      <div class="update-actions">
        <a class="btn-secondary btn-sm" href="${esc(info?.url || '#')}" target="_blank" rel="noopener">Download manually</a>
        <button class="btn-primary btn-sm" id="update-retry">Try again</button>
      </div>`;
    document.getElementById('update-retry')?.addEventListener('click', startUpdate);
  }
}

function onFinished(st) {
  const foot = document.getElementById('update-foot');
  if (!foot) return;
  if (st.will_quit) {
    foot.innerHTML = `
      <div class="update-hint">The new version is staged. OctoSlave has to close so the installer can replace it.</div>
      <div class="update-actions">
        <button class="btn-primary btn-sm" id="update-quit">Close &amp; finish install</button>
      </div>`;
    document.getElementById('update-quit')?.addEventListener('click', async () => {
      foot.innerHTML = '<div class="update-hint">Closing… OctoSlave will reopen on its own in a moment.</div>';
      await fetch('/api/update/quit', { method: 'POST' }).catch(() => {});
    });
  } else {
    foot.innerHTML = `
      <div class="update-hint">Installed. Restart OctoSlave to run ${esc(st.target || info?.latest || 'the new version')}.</div>
      <div class="update-actions">
        <button class="btn-primary btn-sm" data-close="1">Done</button>
      </div>`;
  }
  const pill = document.getElementById('update-slot');
  if (pill) pill.innerHTML = '';
}

// ──────────────────────────────────────────────────────────────
// Boot
// ──────────────────────────────────────────────────────────────

export async function initUpdater() {
  try {
    info = await (await fetch('/api/update/check')).json();
  } catch {
    return;                       // offline: stay completely silent
  }
  renderPill();

  // An update left mid-flight by a page reload should reattach, not restart.
  try {
    const st = await (await fetch('/api/update/status')).json();
    if (st.state === 'running') { openDialog(); renderProgress(st); poll(); }
  } catch { /* ignore */ }
}
