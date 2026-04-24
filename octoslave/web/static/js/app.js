/**
 * OctoSlave Web UI - Main Application
 */

console.log('[app.js] Module loaded');

import { 
  WS_URL, connectWebSocket, sendMsg, applyConfig, populateModelSelects, onConfigUpdated 
} from './websocket.js';
import { handleSlashCommand } from './slash-commands.js';
import {
  toggleHistory, browseDir, refreshHistory, refreshFileTree, viewFile,
  uploadFile, removeAttachment, clearChatMessages, appendChatInfo, appendChatError
} from './components.js';
import { scrollToBottom, autoResizeTextarea, renderMarkdown, esc } from './utils.js';

// Export functions to global scope for inline handlers
window.toggleHistory = toggleHistory;
window.browseDir = browseDir;
window.refreshHistory = refreshHistory;
window.viewFile = viewFile;
window.removeAttachment = removeAttachment;
window.loadChat = (id) => { window.loadChatImpl && window.loadChatImpl(id); };
window.deleteChat = (id) => { window.deleteChatImpl && window.deleteChatImpl(id); };

// ──────────────────────────────────────────────────────────────
// Server message handler
// ──────────────────────────────────────────────────────────────
function handleServerMessage(msg) {
  console.log('[app] Received message:', msg.type, msg);
  switch (msg.type) {
    case 'config':        applyConfig(msg.data); break;
    case 'config_updated': onConfigUpdated(msg); break;
    case 'models':        populateModelSelects(msg.list || []); break;
    case 'token':         onToken(msg.text); break;
    case 'stream_end':    onStreamEnd(); break;
    case 'tool_call':     onToolCall(msg.name, msg.summary); break;
    case 'tool_result':   onToolResult(msg.name, msg.ok, msg.preview); break;
    case 'done':          onDone(msg.iterations); break;
    case 'info':          appendChatInfo(msg.text); break;
    case 'error':         onServerError(msg.text); break;
    case 'cleared':       break;
    case 'chat_saved':
      if (msg.id) window.appState.currentChatId = msg.id;
      refreshHistory();
      break;
    case 'chat_loaded': onChatLoaded(msg); break;
    case 'research_start':    onResearchStart(msg); break;
    case 'round_start':       onRoundStart(msg); break;
    case 'round_done':        onRoundDone(msg); break;
    case 'agent_start':       onAgentStart(msg); break;
    case 'agent_done':        onAgentDone(msg); break;
    case 'research_complete': onResearchComplete(msg); break;
    default: break;
  }
}

// ──────────────────────────────────────────────────────────────
// Chat functions
// ──────────────────────────────────────────────────────────────

let currentAssistantBubble = null;
let currentToolCallsDiv = null;
let streamBuffer = '';

function sendChat() {
  const textarea = document.getElementById('chat-textarea');
  const text = textarea.value.trim();
  const hasFiles = window.appState.attachedFiles.length > 0;
  if ((!text && !hasFiles) || window.appState.running) return;

  // Check for slash commands first
  if (text.startsWith('/')) {
    const handled = handleSlashCommand(text);
    if (handled) {
      textarea.value = '';
      autoResizeTextarea(textarea);
      return;  // Don't send as regular message
    }
  }

  let fullText = text;
  if (hasFiles) {
    const paths = window.appState.attachedFiles.map(f => `- ${f.path}`).join('\n');
    fullText += (text ? '\n\n' : '') + `Attached files:\n${paths}`;
  }

  appendUserMessage(fullText);
  textarea.value = '';
  autoResizeTextarea(textarea);
  document.getElementById('chat-attachments').innerHTML = '';
  window.appState.attachedFiles = [];
  setChatRunning(true);

  const model = document.getElementById('chat-model-select').value.trim();
  const dir   = document.getElementById('chat-dir-input').value.trim();
  const profile = document.getElementById('chat-profile-select').value;
  const permMode = document.getElementById('chat-permission-select').value;

  const type = window.appState.chatIsFirst ? 'chat' : 'chat_continue';
  window.appState.chatIsFirst = false;

  sendMsg({ type, message: fullText, model, working_dir: dir, prompt_profile: profile, permission_mode: permMode });
}

function appendUserMessage(text) {
  window.appState.messages.push({ role: 'user', content: text });
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = 'msg msg-user';
  div.innerHTML = `<div class="msg-bubble">${esc(text)}</div>`;
  container.appendChild(div);
  scrollToBottom(container);
}

function ensureAssistantBubble() {
  if (currentAssistantBubble) return;

  const container = document.getElementById('chat-messages');
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-assistant';

  const bubble = document.createElement('div');
  bubble.className = 'msg-bubble';

  const textDiv = document.createElement('div');
  textDiv.className = 'md-content streaming-cursor';
  textDiv.dataset.raw = '';

  const toolsDiv = document.createElement('div');
  toolsDiv.className = 'tool-calls';

  bubble.appendChild(textDiv);
  bubble.appendChild(toolsDiv);
  wrap.appendChild(bubble);
  container.appendChild(wrap);

  currentAssistantBubble = textDiv;
  currentToolCallsDiv    = toolsDiv;
  streamBuffer           = '';
  scrollToBottom(container);
}

function onToken(text) {
  ensureAssistantBubble();
  streamBuffer += text;
  currentAssistantBubble.textContent = streamBuffer;
  scrollToBottom(document.getElementById('chat-messages'));
}

function onStreamEnd() {
  if (currentAssistantBubble) {
    currentAssistantBubble.classList.remove('streaming-cursor');
    currentAssistantBubble.innerHTML = renderMarkdown(streamBuffer);
  }
  currentAssistantBubble = null;
  currentToolCallsDiv    = null;
}

function onToolCall(name, summary) {
  ensureAssistantBubble();
  
  const icon = globalThis.TOOL_ICONS?.[name] || '🔧';
  const toolBlock = document.createElement('details');
  toolBlock.className = 'tool-block';
  toolBlock.innerHTML = `
    <summary>
      <span class="tool-icon">${icon}</span>
      <span class="tool-name">${name}</span>
      <span class="tool-summary">${esc(summary)}</span>
    </summary>
    <div class="tool-detail pending">Loading...</div>
  `;
  
  currentToolCallsDiv.appendChild(toolBlock);
  scrollToBottom(document.getElementById('chat-messages'));
  
  // Store reference for updating
  window.appState.pendingToolCall = { element: toolBlock, name };
}

function onToolResult(name, ok, preview) {
  if (!window.appState.pendingToolCall) return;
  
  const { element } = window.appState.pendingToolCall;
  const detail = element.querySelector('.tool-detail');
  if (detail) {
    detail.className = `tool-detail ${ok ? 'ok' : 'fail'}`;
    detail.textContent = preview || (ok ? 'Success' : 'Failed');
  }
  
  window.appState.pendingToolCall = null;
}

function onDone(iterations) {
  setChatRunning(false);
}

function onServerError(text) {
  appendChatError(text);
  window.appState.researchRunning = false;
  setChatRunning(false);
}

function setChatRunning(running) {
  window.appState.running = running;
  const statusBadge = document.getElementById('chat-status');
  const sendBtn = document.getElementById('chat-send-btn');
  const startBtn = document.getElementById('research-start-btn');
  
  if (statusBadge) {
    statusBadge.textContent = running ? 'running' : 'idle';
    statusBadge.className = running ? 'badge badge-running' : 'badge badge-idle';
  }
  
  if (sendBtn) sendBtn.disabled = running;
  if (startBtn) startBtn.disabled = running;
}

// ──────────────────────────────────────────────────────────────
// Chat history
// ──────────────────────────────────────────────────────────────

function onChatLoaded(msg) {
  window.appState.messages = msg.messages || [];
  window.appState.model = msg.model || '';
  
  // Clear and rebuild chat UI
  const container = document.getElementById('chat-messages');
  container.innerHTML = '';
  
  msg.messages.forEach(m => {
    if (m.role === 'user') {
      const div = document.createElement('div');
      div.className = 'msg msg-user';
      div.innerHTML = `<div class="msg-bubble">${esc(m.content)}</div>`;
      container.appendChild(div);
    } else if (m.role === 'assistant') {
      const div = document.createElement('div');
      div.className = 'msg msg-assistant';
      div.innerHTML = `<div class="msg-bubble">${renderMarkdown(m.content)}</div>`;
      container.appendChild(div);
    }
  });
  
  scrollToBottom(container);
}

// ──────────────────────────────────────────────────────────────
// Research functions
// ──────────────────────────────────────────────────────────────

function onResearchStart(msg) {
  window.appState.researchRunning = true;
  setChatRunning(true);
  document.getElementById('pipeline-section').classList.add('show');
  document.getElementById('research-console').innerHTML = '';
  document.getElementById('completion-card').classList.remove('show');
  // Reset all pipeline boxes to pending state for the new run
  document.querySelectorAll('.pipeline-box').forEach(box => {
    box.className = 'pipeline-box pending';
    const m = box.querySelector('.p-model'); if (m) m.textContent = '';
    const e = box.querySelector('.p-elapsed'); if (e) e.textContent = '';
  });
}

function onRoundStart(msg) {
  const label = document.getElementById('round-progress-label');
  const fill = document.getElementById('round-progress-fill');
  if (label) label.textContent = `Round ${msg.round}/${window.appState.researchMaxRounds}`;
  if (fill) fill.style.width = '10%';
  
  appendToConsole(`<span class="console-round">═══ ROUND ${msg.round} ═══</span>`);
}

function onRoundDone(msg) {
  const fill = document.getElementById('round-progress-fill');
  if (fill) fill.style.width = `${Math.min(90, ((msg.round / window.appState.researchMaxRounds) * 100))}%`;
}

function onAgentStart(msg) {
  const box = document.querySelector(`.pipeline-box[data-role="${msg.role}"]`);
  if (box) {
    box.classList.remove('pending');
    box.classList.add('active');
    box.querySelector('.p-model').textContent = msg.model || '';
  }
  
  appendToConsole(`<span class="console-agent">▶ ${msg.role}</span> starting...`);
}

function onAgentDone(msg) {
  const box = document.querySelector(`.pipeline-box[data-role="${msg.role}"]`);
  if (box) {
    box.classList.remove('active');
    box.classList.add('done');
    box.querySelector('.p-elapsed').textContent = msg.elapsed || '';
  }
  
  appendToConsole(`<span class="console-agent">✓ ${msg.role}</span> done in ${msg.elapsed || '?'}`);
}

function onResearchComplete(msg) {
  window.appState.researchRunning = false;
  setChatRunning(false);
  document.getElementById('pipeline-section').classList.remove('show');
  document.getElementById('completion-card').classList.add('show');
  
  const reportPath = msg.report_path || 'research/final_report.html';
  const reportBtn = document.getElementById('comp-report-btn');
  if (reportBtn) reportBtn.href = `/api/files/view/${encodeURIComponent(reportPath)}`;
  
  appendToConsole('<span class="console-success">═════ RESEARCH COMPLETE ═════</span>');
}

function appendToConsole(text) {
  const consoleEl = document.getElementById('research-console');
  if (!consoleEl) return;
  
  const line = document.createElement('div');
  line.innerHTML = text;
  consoleEl.appendChild(line);
  consoleEl.scrollTop = consoleEl.scrollHeight;
}

// ──────────────────────────────────────────────────────────────
// Initialization
// ──────────────────────────────────────────────────────────────

function initApp() {
  // Tab switching
  document.querySelectorAll('.nav-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const tab = btn.dataset.tab;
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      document.getElementById('tab-' + tab).classList.add('active');
      if (tab === 'files') refreshFileTree();
    });
  });

  // Chat input
  const textarea = document.getElementById('chat-textarea');
  if (textarea) {
    textarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });
    
    textarea.addEventListener('input', () => autoResizeTextarea(textarea));
  }

  document.getElementById('chat-send-btn')?.addEventListener('click', sendChat);

  document.getElementById('chat-attach-btn')?.addEventListener('click', () => {
    document.getElementById('chat-file-input')?.click();
  });

  document.getElementById('chat-file-input')?.addEventListener('change', async (e) => {
    const files = Array.from(e.target.files);
    for (const file of files) {
      await uploadFile(file);
    }
    e.target.value = '';
  });

  document.getElementById('chat-new-btn')?.addEventListener('click', () => {
    if (window.appState.messages.length > 0 && !window.appState.currentChatId) {
      sendMsg({ type: 'save_chat', chat_id: '' });
    }
    sendMsg({ type: 'chat_clear' });
    clearChatMessages();
    refreshHistory();
  });

  // Profile and permission select change handlers
  document.getElementById('chat-profile-select')?.addEventListener('change', (e) => {
    const profileNames = { base: 'Base', simple: 'Simple', strict: 'Strict' };
    appendChatInfo(`📝 Profile set to [bold]${profileNames[e.target.value]}[/bold]. Will apply to next task.`);
  });

  document.getElementById('chat-permission-select')?.addEventListener('change', (e) => {
    const modeNames = { autonomous: 'Autonomous', controlled: 'Controlled', supervised: 'Supervised' };
    appendChatInfo(`🛡️ Permission mode set to [bold]${modeNames[e.target.value]}[/bold]. Will apply to next tool execution.`);
  });

  // File refresh button
  document.getElementById('files-refresh-btn')?.addEventListener('click', refreshFileTree);

  // Settings refresh button
  document.getElementById('settings-refresh-btn')?.addEventListener('click', () => {
    sendMsg({ type: 'get_config' });
  });

  // Research start button
  document.getElementById('research-start-btn')?.addEventListener('click', () => {
    if (window.appState.running) return;
    const topic = document.getElementById('research-topic').value.trim();
    if (!topic) {
      appendChatError('⚠ Research topic is required.');
      return;
    }
    
    const rounds = parseInt(document.getElementById('research-rounds').value) || 3;
    const modelAll = document.getElementById('research-model-select').value || undefined;
    const resume = document.getElementById('research-resume').checked;
    const workingDir = document.getElementById('research-dir-input').value || '.';
    
    window.appState.researchMaxRounds = rounds;
    window.appState.researchDir = workingDir;
    
    sendMsg({ 
      type: 'research', 
      topic, 
      rounds, 
      model_all: modelAll, 
      resume,
      working_dir: workingDir
    });
  });

  // Completion card buttons
  document.getElementById('comp-files-btn')?.addEventListener('click', () => {
    document.querySelector('[data-tab="files"]').click();
  });

  // History close button
  document.getElementById('history-close')?.addEventListener('click', toggleHistory);

  // Expose load/delete chat functions globally
  window.loadChatImpl = (id) => {
    sendMsg({ type: 'load_chat', chat_id: id });
    toggleHistory();
  };

  window.deleteChatImpl = async (id) => {
    if (!confirm('Delete this chat?')) return;
    try {
      await fetch(`/api/chats/${id}`, { method: 'DELETE' });
      refreshHistory();
    } catch (err) {
      console.error('Failed to delete chat:', err);
    }
  };

  // Initialize WebSocket connection
  connectWebSocket(
    () => {
      // On open - request config and models
      sendMsg({ type: 'get_config' });
      sendMsg({ type: 'list_models' });
    },
    () => {
      // On close - show error
      appendChatError('Disconnected from server. Reconnecting...');
    },
    handleServerMessage
  );

  console.log('OctoSlave Web UI initialized');
}

// Wait for DOM to be ready before initializing
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}
