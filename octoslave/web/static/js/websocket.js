/**
 * OctoSlave Web UI - WebSocket Management
 */

console.log('[websocket.js] Module loaded');

import { esc, renderMarkdown, scrollToBottom } from './utils.js?v=20260429';

// WebSocket URL
export const WS_URL = `ws://${location.host}/ws`;

// Maximum reconnection attempts
export const MAX_RETRIES = 5;

// Reconnection delay in ms
export const RETRY_DELAY = 3000;

// Tool icons mapping
export const TOOL_ICONS = {
  read_file:    '📄',
  write_file:   '✏️',
  edit_file:    '🔧',
  bash:         '⚡',
  glob:         '🔍',
  grep:         '🔎',
  list_dir:     '📁',
  web_search:   '🌐',
  web_fetch:    '🌍',
};

/**
 * WebSocket connection state
 */
export const wsState = {
  ws: null,
  ready: false,
  retries: 0,
  retryTimer: null,
  onMessage: null,  // preserved across reconnects
};

/**
 * Safe CSS escape function with fallback
 */
function safeCssEscape(str) {
  if (typeof CSS !== 'undefined' && typeof CSS.escape === 'function') {
    try {
      return CSS.escape(str);
    } catch (e) {
      // Fallback: manually escape special characters
    }
  }
  // Manual fallback for CSS.escape
  return str.replace(/[!"#$%&'()*+,./:;<=>?@[\\\]^`{|}~]/g, '\\$&');
}

/**
 * Connect to WebSocket server
 */
export function connectWebSocket(onOpen, onClose, onMessage) {
  if (wsState.ws) {
    try { wsState.ws.close(); } catch(_) {}
  }

  // Preserve onMessage handler across reconnects
  if (onMessage) wsState.onMessage = onMessage;

  const ws = new WebSocket(WS_URL);
  wsState.ws = ws;

  ws.onopen = () => {
    wsState.ready  = true;
    wsState.retries  = 0;
    setConnected(true);
    if (onOpen) onOpen();
  };

  ws.onclose = () => {
    wsState.ready = false;
    setConnected(false);
    scheduleReconnect(onClose);
  };

  ws.onerror = () => {
    wsState.ready = false;
    setConnected(false);
  };

  ws.onmessage = (ev) => {
    let msg;
    try { msg = JSON.parse(ev.data); } catch(_) { return; }
    const handler = wsState.onMessage;
    if (handler) handler(msg);
  };
}

// Export TOOL_ICONS for other modules
globalThis.TOOL_ICONS = TOOL_ICONS;

/**
 * Schedule reconnection attempt
 */
function scheduleReconnect(onClose) {
  if (wsState.retries >= MAX_RETRIES) {
    if (onClose) onClose();
    return;
  }
  wsState.retries++;
  clearTimeout(wsState.retryTimer);
  wsState.retryTimer = setTimeout(() => connectWebSocket(null, onClose, null), RETRY_DELAY);
  // onMessage is already preserved in wsState.onMessage; passing null is intentional here
}

/**
 * Send message through WebSocket
 */
export function sendMsg(obj) {
  if (wsState.ws && wsState.ready) {
    wsState.ws.send(JSON.stringify(obj));
    return true;
  }
  return false;
}

/**
 * Set connection status indicator
 */
function setConnected(ok) {
  const dot = document.getElementById('conn-dot');
  if (dot) {
    dot.classList.toggle('connected', ok);
    dot.title = ok ? 'Connected' : 'Disconnected';
  }
}

/**
 * Cached provider catalog (filled by populateBackendSelects).
 * Used to resolve backend-id → display name when no full catalog is present.
 */
const providerLookup = new Map();
// Seed with built-ins so labels work before list_providers returns.
[
  { id: 'einfra', name: 'e-INFRA CZ' },
  { id: 'ollama', name: 'Local (Ollama)' },
  { id: 'nim',    name: 'NVIDIA NIM' },
].forEach(p => providerLookup.set(p.id, p));

export function getProviderName(backendId) {
  const p = providerLookup.get(backendId);
  return (p && p.name) || backendId;
}

/**
 * Populate both backend-select dropdowns from a {providers, active} payload.
 */
export function populateBackendSelects(providers, active) {
  if (!Array.isArray(providers)) return;
  // Update the lookup
  providerLookup.clear();
  providers.forEach(p => providerLookup.set(p.id, p));

  const fillSelect = (sel) => {
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = '';
    providers.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.id;
      opt.textContent = p.name + (p.kind === 'custom' ? ' ✦' : '');
      if (!p.configured) opt.title = `${p.name} is not configured yet`;
      sel.appendChild(opt);
    });
    const target = active || prev;
    if (target && providers.some(p => p.id === target)) {
      sel.value = target;
      sel.dataset.backend = target;
    }
  };
  fillSelect(document.getElementById('backend-select'));
  fillSelect(document.getElementById('research-backend-select'));
  applyBackend(active || providerLookup.keys().next().value || 'einfra');
}

/**
 * Update the backend select dropdown and sidebar pill to reflect the active backend.
 */
function applyBackend(backend) {
  const chatSel = document.getElementById('backend-select');
  if (chatSel) {
    chatSel.value = backend;
    chatSel.dataset.backend = backend;
  }
  const researchSel = document.getElementById('research-backend-select');
  if (researchSel) {
    researchSel.value = backend;
    researchSel.dataset.backend = backend;
  }
  const pill = document.getElementById('backend-pill');
  if (pill) {
    // Built-in pills get short labels; custom providers show the id verbatim
    // so users can tell their providers apart at a glance.
    const builtinLabels = { einfra: 'einfra', ollama: 'local', nim: 'nim' };
    pill.textContent = builtinLabels[backend] || backend;
    pill.dataset.backend = backend;
    pill.title = getProviderName(backend);
  }
}

/**
 * Handle server configuration message
 */
export function applyConfig(data) {
  const state = window.appState || {};
  state.config = data || {};
  const model   = data?.model || '';
  const dir     = data?.working_dir || '.';
  const url     = data?.base_url || '';
  const backend = data?.backend || 'einfra';

  document.getElementById('model-badge').textContent = model || '—';
  state.config._pendingModel = model;
  const sel = document.getElementById('chat-model-select');
  if (model && sel.querySelector(`option[value="${safeCssEscape(model)}"]`)) {
    sel.value = model;
  }
  document.getElementById('chat-dir-input').value     = dir;
  document.getElementById('research-dir-input').value = dir;

  document.getElementById('settings-api-key').value     = data?.has_api_key ? '••••••••' : '';
  document.getElementById('settings-nim-api-key').value = data?.has_nim_key  ? '••••••••' : '';
  document.getElementById('settings-base-url').value    = url;
  document.getElementById('settings-model').value       = model;
  document.getElementById('settings-working-dir').value = dir;
  document.getElementById('settings-backend').value     = backend;

  applyBackend(backend);

  window.appState = state;
}

/**
 * Populate model selects with available models
 */
export function populateModelSelects(models) {
  console.log('[websocket] populateModelSelects called with', models.length, 'models');
  const chatSel     = document.getElementById('chat-model-select');
  const researchSel = document.getElementById('research-model-select');
  const prevChat    = chatSel?.value;

  console.log('[websocket] chatSel:', chatSel ? 'found' : 'NOT FOUND');
  console.log('[websocket] researchSel:', researchSel ? 'found' : 'NOT FOUND');

  if (chatSel) {
    chatSel.innerHTML = '';
    models.forEach(m => {
      const o = document.createElement('option');
      o.value = o.textContent = m;
      chatSel.appendChild(o);
    });
    console.log('[websocket] Added', models.length, 'options to chat select');
    
    const target = prevChat || window.appState?.config?._pendingModel || window.appState?.config?.model || '';
    if (target && chatSel.querySelector(`option[value="${safeCssEscape(target)}"]`)) {
      chatSel.value = target;
      console.log('[websocket] Set chat select to:', target);
    } else if (models.length) {
      chatSel.value = models[0];
      console.log('[websocket] Set chat select to first model:', models[0]);
    }
  } else {
    console.error('[websocket] ERROR: chat-model-select element not found!');
  }

  if (researchSel) {
    const prevResearch = researchSel.value;
    researchSel.innerHTML = '<option value="">(use default)</option>';
    models.forEach(m => {
      const o = document.createElement('option');
      o.value = o.textContent = m;
      researchSel.appendChild(o);
    });
    console.log('[websocket] Added', models.length, 'options to research select');
    if (prevResearch) researchSel.value = prevResearch;
  } else {
    console.error('[websocket] ERROR: research-model-select element not found!');
  }
}

/**
 * Handle config update (backend switch)
 */
export function onConfigUpdated(msg) {
  const modelBadge = document.getElementById('model-badge');
  if (modelBadge) modelBadge.textContent = msg.model || '—';

  if (msg.backend) applyBackend(msg.backend);

  const infoFn = window.appendChatInfo;
  if (infoFn) {
    const name = msg.name || getProviderName(msg.backend);
    let icon = '🟣';
    if (msg.backend === 'nim') icon = '🟦';
    else if (msg.backend === 'ollama') icon = '🟢';
    else if (msg.backend && msg.backend !== 'einfra') icon = '🟡';
    const modelTail = msg.model ? ` with ${msg.model}` : '';
    infoFn(`${icon} Switched to [bold]${name}[/bold]${modelTail}`);
  }
}
