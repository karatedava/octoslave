/**
 * OctoSlave Web UI - Component Helpers
 */

console.log('[components.js] Module loaded');

import { esc, renderMarkdown, scrollToBottom, autoResizeTextarea } from './utils.js?v=20260429';
import { sendMsg } from './websocket.js?v=20260630c';

/**
 * Application state (shared across modules)
 */
window.appState = {
  ws: null,
  wsReady: false,
  retries: 0,
  retryTimer: null,
  running: false,
  config: {},
  currentAssistantBubble: null,
  currentToolCallsDiv: null,
  chatIsFirst: true,
  pendingToolCall: null,
  streamBuffer: '',
  messages: [],
  currentChatId: null,
  suppressNextChatSavedId: false,
  attachedFiles: [],
  workingDir: '.',          // single source of truth for the working directory
  remoteId: null,           // active remote (SSH) target id, or null = local
  remotes: [],              // configured remote targets (from /api/remotes)
};

/**
 * Working directory — the canonical value lives in appState; the hero empty-state
 * picker is just one view of it. '.' (or empty) means "not chosen yet".
 */
window.getWorkingDir = function () {
  const v = (window.appState.workingDir || '').trim();
  return v || '.';
};
window.setWorkingDir = function (value) {
  window.appState.workingDir = value ?? '';
  const v = (window.appState.workingDir || '').trim();
  const real = v && v !== '.';
  // Reflect into the hero picker (when the empty state is mounted). Show the
  // placeholder until a real folder is chosen; guard against cursor resets.
  const hero = document.getElementById('empty-dir-input');
  if (hero) { const shown = real ? v : ''; if (hero.value !== shown) hero.value = shown; }
  // Pulse the hero until a directory is chosen.
  document.getElementById('empty-dir')?.classList.toggle('needs-dir', !real);
  // Mirror into the read-only Settings display.
  const sett = document.getElementById('settings-working-dir');
  if (sett) sett.value = real ? v : '.';
};

/**
 * Execution mode — local (default) or a remote SSH target. The toggle next to
 * the working-directory picker drives this. When switching to Remote with no
 * remotes configured, the caller redirects to the Remotes settings card.
 */
window.getRemoteId = function () { return window.appState.remoteId || null; };

window.setRemoteMode = function (mode, remoteId) {
  // mode: 'local' | 'remote'
  if (mode === 'local') {
    window.appState.remoteId = null;
    if (window.sendWs) window.sendWs({ type: 'set_remote', remote_id: null });
  } else {
    const remotes = window.appState.remotes || [];
    const id = remoteId || window.appState.remoteId || (remotes[0] && remotes[0].id);
    if (!remotes.length || !id) {
      // Nothing configured → send the user to configure a remote.
      if (window.openRemotesConfig) window.openRemotesConfig();
      window.renderExecToggle?.();
      return;
    }
    window.appState.remoteId = id;
    // The server resolves the starting directory (remote home) and returns it in
    // the set_remote 'ok' reply, which updates the working-dir field.
    if (window.sendWs) window.sendWs({ type: 'set_remote', remote_id: id });
  }
  window.renderExecToggle?.();
};

/**
 * Render the Local | Remote segmented toggle + remote picker next to the dir
 * field. Rebuilt whenever the remotes list or selection changes.
 */
window.renderExecToggle = function () {
  const host = document.getElementById('exec-toggle');
  if (!host) return;
  const remotes = window.appState.remotes || [];
  const rid = window.appState.remoteId;
  const isRemote = !!rid;
  const opts = remotes.map((r) =>
    `<option value="${r.id}" ${r.id === rid ? 'selected' : ''}>${r.name || r.id}</option>`
  ).join('');
  host.innerHTML = `
    <div class="exec-seg" role="radiogroup" aria-label="Execution location">
      <button type="button" class="exec-seg-btn ${isRemote ? '' : 'active'}" data-exec="local">💻 Local</button>
      <button type="button" class="exec-seg-btn ${isRemote ? 'active' : ''}" data-exec="remote">🌐 Remote</button>
    </div>
    ${isRemote ? `<select id="exec-remote-select" class="exec-remote-select" title="Remote host">${opts}</select>` : ''}
    <button type="button" class="exec-config-btn" title="Manage remotes">⚙</button>`;
  host.querySelector('[data-exec="local"]')?.addEventListener('click', () => window.setRemoteMode('local'));
  host.querySelector('[data-exec="remote"]')?.addEventListener('click', () => window.setRemoteMode('remote'));
  host.querySelector('.exec-config-btn')?.addEventListener('click', () => window.openRemotesConfig?.());
  host.querySelector('#exec-remote-select')?.addEventListener('change', (e) => window.setRemoteMode('remote', e.target.value));
};

/**
 * Toggle history sidebar
 */
export function toggleHistory() {
  const overlay = document.getElementById('history-overlay');
  const sidebar = document.getElementById('history-sidebar');
  const isOpen = sidebar?.classList.contains('open');
  
  if (overlay && sidebar) {
    overlay.classList.toggle('open', !isOpen);
    sidebar.classList.toggle('open', !isOpen);
    if (!isOpen) refreshHistory();
  }
}

/**
 * Browse directory using native dialog
 */
export async function browseDir(inputId) {
  // In remote mode, browse folders on the remote host instead of the local OS.
  if (window.getRemoteId && window.getRemoteId() && window.openRemoteDirPicker) {
    window.openRemoteDirPicker(inputId);
    return;
  }
  try {
    const response = await fetch('/api/pick-dir');
    const data = await response.json();
    if (data.path) {
      const input = document.getElementById(inputId);
      if (input) {
        input.value = data.path;
        // Notify listeners (dir sync + hero highlight) of the programmatic change.
        input.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
  } catch (err) {
    console.error('Failed to open directory picker:', err);
  }
}

/**
 * Refresh chat history list
 */
export async function refreshHistory() {
  const listContainer = document.getElementById('history-list');
  if (!listContainer) return;
  
  try {
    const response = await fetch('/api/chats');
    const data = await response.json();
    
    if (!data.chats || data.chats.length === 0) {
      listContainer.innerHTML = '<div class="history-empty">No saved chats yet.</div>';
      return;
    }
    
    listContainer.innerHTML = data.chats.map(chat => `
      <div class="history-item" onclick="loadChat('${chat.id}')">
        <div class="history-item-body">
          <div class="history-item-title">${esc(chat.title)}</div>
          <div class="history-item-meta">${chat.model || 'Unknown'} • ${formatTimestamp(chat.updated_at)}</div>
        </div>
        <button class="history-item-del" onclick="event.stopPropagation(); deleteChat('${chat.id}')">🗑️</button>
      </div>
    `).join('');
  } catch (err) {
    console.error('Failed to load chat history:', err);
    listContainer.innerHTML = '<div class="history-empty">Failed to load chats.</div>';
  }
}

/**
 * Load a chat by ID
 */
export async function loadChat(chatId) {
  try {
    sendMsg({ type: 'load_chat', chat_id: chatId });
    toggleHistory();
  } catch (err) {
    console.error('Failed to load chat:', err);
  }
}

/**
 * Delete a chat by ID
 */
export async function deleteChat(chatId) {
  if (!confirm('Delete this chat?')) return;
  
  try {
    await fetch(`/api/chats/${chatId}`, { method: 'DELETE' });
    refreshHistory();
  } catch (err) {
    console.error('Failed to delete chat:', err);
  }
}

/**
 * Format timestamp for display
 */
function formatTimestamp(isoString) {
  const date = new Date(isoString);
  return date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
}

/**
 * Handle file upload
 */
export async function uploadFile(file) {
  const workingDir = window.getWorkingDir();
  const formData = new FormData();
  formData.append('file', file);
  formData.append('working_dir', workingDir);
  
  try {
    const response = await fetch('/api/upload', {
      method: 'POST',
      body: formData,
    });
    
    if (!response.ok) throw new Error('Upload failed');
    
    const data = await response.json();
    window.appState.attachedFiles.push(data);
    
    updateAttachmentChips();
    return data;
  } catch (err) {
    console.error('Failed to upload file:', err);
    appendChatError(`Failed to upload ${file.name}`);
    return null;
  }
}

/**
 * Update attachment chips display
 */
function updateAttachmentChips() {
  const container = document.getElementById('chat-attachments');
  if (!container) return;
  
  container.innerHTML = window.appState.attachedFiles.map(file => `
    <div class="attach-chip">
      <span class="chip-name">${esc(file.name)}</span>
      <button class="chip-remove" onclick="removeAttachment('${file.path}')">×</button>
    </div>
  `).join('');
}

/**
 * Remove an attachment
 */
export function removeAttachment(path) {
  window.appState.attachedFiles = window.appState.attachedFiles.filter(f => f.path !== path);
  updateAttachmentChips();
}

/**
 * Empty-state markup (logo + hero working-directory picker). Kept in JS so it
 * can be rebuilt after a chat is cleared — see renderChatEmptyState().
 */
const EMPTY_STATE_HTML = `
  <div class="chat-empty-state" id="chat-empty-state">
    <img src="/static/logo.png?v=3" alt="OctoSlave" class="empty-logo">
    <p class="empty-title">OctoSlave</p>

    <div class="empty-dir needs-dir" id="empty-dir">
      <div class="empty-dir-label">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
        </svg>
        <span>Pick a working directory to get started</span>
      </div>
      <div class="dir-field dir-field-hero" id="empty-dir-field">
        <svg class="dir-field-icon" width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
        </svg>
        <input type="text" id="empty-dir-input" placeholder="No folder selected yet…" title="Working directory">
        <button class="btn-browse-hero" onclick="browseDir('empty-dir-input')" title="Browse for folder">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
          </svg>
          <span>Browse…</span>
        </button>
      </div>
      <div class="empty-dir-hint">OctoSlave reads &amp; writes files here. Change it later with <code>/dir &lt;path&gt;</code>.</div>
    </div>

    <p class="empty-sub">
      Then ask anything. Run code. Research topics.<br>
      Type <code>@</code> to attach a file, <code>/help</code> for commands,
      or use the <code>•••</code> button to spin up a multi-agent committee.
    </p>
  </div>`;

/**
 * Render the friendly empty state (logo + hero dir picker) into an empty chat.
 * Re-wires the directory inputs so the hero picker stays in sync.
 */
export function renderChatEmptyState() {
  const container = document.getElementById('chat-messages');
  if (!container) return;
  container.innerHTML = EMPTY_STATE_HTML;
  // Restore the current working dir into the freshly-built hero input.
  window.setWorkingDir(window.appState.workingDir);
  // The empty state was just rebuilt from a template, so its (now empty)
  // #exec-toggle host needs the Local/Remote control re-rendered into it.
  window.renderExecToggle?.();
}

/**
 * Remove the empty state as soon as real chat content appears.
 */
export function dismissChatEmptyState() {
  document.getElementById('chat-empty-state')?.remove();
}

/**
 * Append info message to chat
 */
export function appendChatInfo(text) {
  const container = document.getElementById('chat-messages');
  if (!container) return;
  // NOTE: do NOT dismiss the empty-state hero here. Config notices (profile /
  // permission / remote changes) are emitted before any conversation starts, and
  // removing the hero would take the working-directory + Local/Remote controls
  // with it. The hero is dismissed when a real message is sent (appendUserMessage).

  const div = document.createElement('div');
  div.className = 'msg msg-info';
  
  // Process markdown-like formatting
  let formattedText = text
    .replace(/\[bold\](.*?)\[\/bold\]/g, '<strong>$1</strong>')
    .replace(/\[dim\](.*?)\[\/dim\]/g, '<em>$1</em>');
  
  div.innerHTML = `<div class="msg-bubble">${formattedText}</div>`;
  container.appendChild(div);
  scrollToBottom(container);
}

/**
 * Append error message to chat
 */
export function appendChatError(text) {
  const container = document.getElementById('chat-messages');
  if (!container) return;
  // Keep the empty-state hero (see appendChatInfo) — the working-dir + Local/Remote
  // controls live there; a real message dismisses it via appendUserMessage.

  const div = document.createElement('div');
  div.className = 'msg msg-error';
  div.innerHTML = `<div class="msg-bubble">⚠ ${text}</div>`;
  container.appendChild(div);
  scrollToBottom(container);
}

/**
 * Clear chat messages
 */
export function clearChatMessages() {
  const container = document.getElementById('chat-messages');
  if (container) container.innerHTML = '';
  // Bring back the friendly logo + working-directory picker.
  renderChatEmptyState();

  const attachments = document.getElementById('chat-attachments');
  if (attachments) attachments.innerHTML = '';
  
  window.appState.currentAssistantBubble = null;
  window.appState.currentToolCallsDiv    = null;
  window.appState.pendingToolCall        = null;
  window.appState.streamBuffer           = '';
  window.appState.chatIsFirst            = true;
  window.appState.messages               = [];
  window.appState.currentChatId          = null;
  window.appState.attachedFiles          = [];
  setChatRunning(false);
}

/**
 * Set chat running state
 */
function setChatRunning(running) {
  window.appState.running = running;
  if (!running) window.appState.stopping = false;
  const statusBadge = document.getElementById('chat-status');
  const sendBtn = document.getElementById('chat-send-btn');

  if (statusBadge) {
    statusBadge.textContent = running ? 'running' : 'idle';
    statusBadge.className = running ? 'badge badge-running' : 'badge badge-idle';
  }

  // The send button doubles as a stop button while a task runs — keep it
  // enabled and swap its icon/colour via the `.running` class.
  if (sendBtn) {
    sendBtn.disabled = false;
    sendBtn.classList.toggle('running', running);
    sendBtn.title = running ? 'Stop the agent' : 'Send message';
  }
}
