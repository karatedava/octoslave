/**
 * OctoSlave Web UI - Component Helpers
 */

console.log('[components.js] Module loaded');

import { esc, renderMarkdown, scrollToBottom, autoResizeTextarea } from './utils.js?v=20260429';
import { sendMsg } from './websocket.js?v=20260429';

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
  try {
    const response = await fetch('/api/pick-dir');
    const data = await response.json();
    if (data.path) {
      const input = document.getElementById(inputId);
      if (input) input.value = data.path;
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
  const workingDir = document.getElementById('chat-dir-input')?.value || '.';
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
 * Append info message to chat
 */
export function appendChatInfo(text) {
  const container = document.getElementById('chat-messages');
  if (!container) return;
  
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
  const statusBadge = document.getElementById('chat-status');
  const sendBtn = document.getElementById('chat-send-btn');

  if (statusBadge) {
    statusBadge.textContent = running ? 'running' : 'idle';
    statusBadge.className = running ? 'badge badge-running' : 'badge badge-idle';
  }

  if (sendBtn) sendBtn.disabled = running;
}
