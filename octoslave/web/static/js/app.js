/**
 * OctoSlave Web UI - Main Application
 */

console.log('[app.js] Module loaded');

import {
  WS_URL, connectWebSocket, sendMsg, applyConfig, populateModelSelects, onConfigUpdated,
  populateBackendSelects, getProviderName
} from './websocket.js?v=20260630c';
import { handleSlashCommand } from './slash-commands.js?v=20260630c';
import {
  toggleHistory, browseDir, refreshHistory,
  uploadFile, removeAttachment, clearChatMessages, appendChatInfo, appendChatError,
  dismissChatEmptyState
} from './components.js?v=20260630c';
import { scrollToBottom, autoResizeTextarea, renderMarkdown, esc } from './utils.js?v=20260429';

// Export functions to global scope for inline handlers
window.sendWs = sendMsg;   // used by the exec (local/remote) toggle in components.js
window.toggleHistory = toggleHistory;
window.browseDir = browseDir;
window.refreshHistory = refreshHistory;
window.uploadFile = uploadFile;
window.removeAttachment = removeAttachment;
window.appendChatInfo = appendChatInfo;
window.appendChatError = appendChatError;
window.clearChatMessages = clearChatMessages;
window.loadChat = (id) => { window.loadChatImpl && window.loadChatImpl(id); };
window.deleteChat = (id) => { window.deleteChatImpl && window.deleteChatImpl(id); };

// ──────────────────────────────────────────────────────────────
// Server message handler
// ──────────────────────────────────────────────────────────────
function handleServerMessage(msg) {
  console.log('[app] Received message:', msg.type, msg);

  // Parallel-mode routing: events tagged with a candidate_index belong to a
  // running multi-agent run and should update the live panel rather than the
  // single-conversation chat bubble.
  if (msg.type === 'parallel_start')   { onParallelStart(msg);   return; }
  if (msg.type === 'parallel_progress'){ onParallelProgress(msg);return; }
  if (msg.candidate_index !== undefined && parallelLive.active) {
    onParallelEvent(msg);
    return;
  }
  if (msg.type === 'providers') { onProviders(msg); return; }
  if (msg.type === 'provider_test') { onProviderTest(msg); return; }
  if (msg.type === 'mcp_servers') { onMcpServers(msg); return; }
  if (msg.type === 'mcp_registry') { onMcpRegistry(msg); return; }

  switch (msg.type) {
    case 'config':        applyConfig(msg.data); break;
    case 'config_updated': onConfigUpdated(msg); break;
    case 'models':        populateModelSelects(msg.list || []); break;
    case 'stream_start':  onStreamStart(); break;
    case 'token':         onToken(msg.text); break;
    case 'stream_end':    onStreamEnd(); break;
    case 'tool_call':     onToolCall(msg); break;
    case 'tool_result':   onToolResult(msg.name, msg.ok, msg.preview); break;
    case 'plan':          onPlan(msg.text); break;
    case 'done':          onDone(msg.iterations, msg.stopped); break;
    case 'info':          appendChatInfo(msg.text); break;
    case 'error':         onServerError(msg.text); break;
    case 'cleared':       break;
    case 'chat_saved':
      // When this save was the "flush" half of starting a new chat, the
      // conversation has already been cleared (currentChatId reset to null).
      // Adopting the saved id here would make the fresh chat overwrite the
      // one we just archived on its next save — so skip it in that case.
      if (msg.id && !window.appState.suppressNextChatSavedId) {
        window.appState.currentChatId = msg.id;
      }
      window.appState.suppressNextChatSavedId = false;
      refreshHistory();
      break;
    case 'chat_loaded': onChatLoaded(msg); break;
    case 'permission_request':  onPermissionRequest(msg); break;
    case 'user_question':       onUserQuestion(msg); break;
    case 'todos':               onTodos(msg); break;
    case 'parallel_result':     onParallelResult(msg); break;
    case 'ok':
      // set_remote reply carries the resolved remote working dir (its home).
      if (msg.set_remote && typeof msg.working_dir === 'string') {
        window.setWorkingDir(msg.working_dir);
        window.renderExecToggle?.();
      }
      break;
    default: break;
  }
}

// ──────────────────────────────────────────────────────────────
// Parallel run — live progress panel
// ──────────────────────────────────────────────────────────────

const parallelLive = {
  active: false,
  panel: null,        // root <div> in the chat stream
  cards: {},          // { [index]: { card, log, status, action, model, iters } }
  candidates: [],     // metadata pushed in parallel_start
};

function onParallelStart(msg) {
  // Tear down any leftover panel from a previous run
  if (parallelLive.panel && parallelLive.panel.parentElement) {
    parallelLive.panel.remove();
  }
  parallelLive.active = true;
  parallelLive.cards = {};
  parallelLive.candidates = msg.candidates || [];

  const container = document.getElementById('chat-messages');
  const root = document.createElement('div');
  root.className = 'msg msg-assistant parallel-live-msg';

  const cardsHtml = (msg.candidates || []).map(c => {
    const color = c.color || '#fab283';
    return `
      <div class="plive-card plive-running" data-idx="${c.index}" style="--c: ${color}">
        <div class="plive-head">
          <span class="plive-idx">#${c.index}</span>
          <span class="plive-model">${esc(c.model || '')}</span>
          <span class="plive-status">running…</span>
        </div>
        <div class="plive-meta">
          <span class="plive-profile">${esc(c.profile || '')}</span>
          <span class="plive-iter">0 iter</span>
        </div>
        <div class="plive-action plive-action-empty">waiting for first action…</div>
        <div class="plive-log"></div>
      </div>`;
  }).join('');

  root.innerHTML = `
    <div class="msg-bubble plive-bubble">
      <div class="plive-head-row">
        🐙×${(msg.candidates || []).length}
        <span class="plive-strategy">strategy=<strong>${esc(msg.strategy)}</strong></span>
        <span class="plive-overall">running…</span>
      </div>
      <div class="plive-grid">${cardsHtml}</div>
    </div>`;
  container.appendChild(root);

  parallelLive.panel = root;
  Array.from(root.querySelectorAll('.plive-card')).forEach(card => {
    const idx = parseInt(card.dataset.idx);
    parallelLive.cards[idx] = {
      card,
      log: card.querySelector('.plive-log'),
      status: card.querySelector('.plive-status'),
      action: card.querySelector('.plive-action'),
      iter: card.querySelector('.plive-iter'),
    };
  });
  scrollToBottom(container);
}

function onParallelProgress(msg) {
  if (!parallelLive.active) return;
  const slot = parallelLive.cards[msg.index];
  if (!slot) return;

  if (typeof msg.iter === 'number') {
    slot.iter.textContent = msg.iter + ' iter';
  }
  if (msg.action) {
    slot.action.textContent = msg.action;
    slot.action.classList.remove('plive-action-empty');
  }
  if (msg.status === 'done') {
    slot.card.classList.remove('plive-running');
    slot.card.classList.add('plive-done');
    slot.status.textContent = 'done';
  } else if (msg.status === 'error') {
    slot.card.classList.remove('plive-running');
    slot.card.classList.add('plive-failed');
    slot.status.textContent = 'failed';
  } else {
    slot.status.textContent = 'running…';
  }
}

function onParallelEvent(msg) {
  // Append a single line to the candidate's log feed.
  const slot = parallelLive.cards[msg.candidate_index];
  if (!slot) return;
  const line = document.createElement('div');
  line.className = 'plive-line';

  const t = msg.type;
  if (t === 'tool_call') {
    line.innerHTML = `<span class="plive-line-tool">${esc(msg.name || '')}</span> ` +
                     `<span class="plive-line-args">${esc(msg.summary || '')}</span>`;
  } else if (t === 'tool_result') {
    if (!msg.preview) return;  // silent results add no signal
    const cls = msg.ok ? 'ok' : 'fail';
    line.classList.add('plive-line-result', cls);
    line.textContent = (msg.ok ? '✓ ' : '✗ ') + (msg.preview || '').replace(/\n+/g, ' ').slice(0, 140);
  } else if (t === 'info') {
    if (!msg.text) return;
    line.classList.add('plive-line-info');
    line.textContent = 'ℹ ' + msg.text.slice(0, 200);
  } else if (t === 'error') {
    line.classList.add('plive-line-error');
    line.textContent = '✗ ' + (msg.text || '').slice(0, 200);
  } else {
    return;  // skip token / plan / stream noise
  }

  slot.log.appendChild(line);
  // Cap log growth so DOM doesn't balloon during long runs.
  while (slot.log.childElementCount > 60) slot.log.firstChild.remove();
  slot.log.scrollTop = slot.log.scrollHeight;
}

// ──────────────────────────────────────────────────────────────
// Parallel run result panel (final summary, after live run finishes)
// ──────────────────────────────────────────────────────────────

function onParallelResult(msg) {
  setChatRunning(false);
  // Mark the live panel as complete (don't tear it down — it's the run's log).
  if (parallelLive.panel) {
    const overall = parallelLive.panel.querySelector('.plive-overall');
    if (overall) {
      const winner = msg.winner;
      overall.textContent = winner !== null && winner !== undefined
        ? `winner: #${winner}` : 'complete';
      overall.classList.add('plive-overall-done');
    }
    // Highlight the winning card
    if (msg.winner !== null && msg.winner !== undefined) {
      const winSlot = parallelLive.cards[msg.winner];
      if (winSlot) winSlot.card.classList.add('plive-winner');
    }
  }
  parallelLive.active = false;

  const container = document.getElementById('chat-messages');
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-assistant';

  const cards = (msg.candidates || []).map(c => {
    const isWinner = c.index === msg.winner;
    const head = `Candidate ${c.index} · ${esc(c.profile)} · ${esc(c.model || '')}`;
    const status = c.succeeded ? (isWinner ? '🏆 winner' : '✓') : '✗ failed';
    const body = c.succeeded ? esc(c.summary || '') : esc(c.error || 'failed');
    return `
      <div class="parallel-card${isWinner ? ' parallel-winner' : ''}${c.succeeded ? '' : ' parallel-fail'}">
        <div class="parallel-card-head">
          <span class="parallel-card-title">${head}</span>
          <span class="parallel-card-status">${status}</span>
        </div>
        <pre class="parallel-card-body">${body}</pre>
        <div class="parallel-card-foot"><code>${esc(c.workdir || '')}</code></div>
      </div>`;
  }).join('');

  let mergeBlock = '';
  if (msg.strategy === 'merge' && msg.merged_text) {
    mergeBlock = `
      <div class="parallel-merge">
        <div class="parallel-merge-head">📝 Synthesised result</div>
        <div class="parallel-merge-body">${renderMarkdown(msg.merged_text)}</div>
      </div>`;
  }

  wrap.innerHTML = `
    <div class="msg-bubble parallel-bubble">
      <div class="parallel-head">
        🐙×${(msg.candidates || []).length} parallel run · strategy=<strong>${esc(msg.strategy)}</strong>
        ${msg.winner !== null && msg.winner !== undefined ? `· winner=<strong>#${msg.winner}</strong>` : ''}
      </div>
      <div class="parallel-reason">${esc(msg.reason || '')}</div>
      <div class="parallel-grid">${cards}</div>
      ${mergeBlock}
    </div>`;
  container.appendChild(wrap);
  scrollToBottom(container);
}

// ──────────────────────────────────────────────────────────────
// Chat functions
// ──────────────────────────────────────────────────────────────

let currentAssistantBubble = null;
let currentToolCallsDiv = null;
let streamBuffer = '';

// Interrupt the running agent. The backend stops at the next stream chunk /
// turn boundary and emits a `done` event, which clears the running state. The
// partial conversation is kept, so the user can refine and send a follow-up.
function stopChat() {
  if (!window.appState.running || window.appState.stopping) return;
  window.appState.stopping = true;
  sendMsg({ type: 'stop' });
  const btn = document.getElementById('chat-send-btn');
  if (btn) btn.title = 'Stopping…';
}

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
  // New user turn → start a fresh task checklist card if one appears.
  window._lastTodoCard = null;
  setChatRunning(true);

  const model = document.getElementById('chat-model-select').value.trim();
  const dir   = window.getWorkingDir();
  const profile = document.getElementById('chat-profile-select').value;
  const permMode = document.getElementById('chat-permission-select').value;
  // Agent mode — standard | improved | ultra (default improved). Improved/Ultra
  // run the council; backend auto-falls back to the single agent on local/Ollama.
  const mode = document.getElementById('mode-seg')?.dataset.mode || 'improved';
  const council = mode === 'improved' || mode === 'ultra';
  const ultra = mode === 'ultra';
  // Per-role council model overrides (empty = auto-resolve on the backend).
  let councilModels;
  if (council && window.councilConfig) {
    const picked = Object.fromEntries(
      Object.entries(window.councilConfig).filter(([, v]) => v)
    );
    if (Object.keys(picked).length) councilModels = picked;
  }

  // Parallel mode short-circuit: when the popover toggle is on, route to the
  // multi-agent handler with per-candidate model/profile selections.
  if (window.parallelConfig?.enabled) {
    const cfg = window.parallelConfig;
    const summary = cfg.models?.length
      ? `models=[${cfg.models.join(', ')}]`
      : `model=${model || '(default)'}`;
    appendChatInfo(`🐙×${cfg.n} Spawning ${cfg.n} agents · strategy=${cfg.strategy} · ${summary}`);
    sendMsg({
      type: 'chat_parallel',
      message: fullText,
      n: cfg.n,
      strategy: cfg.strategy,
      models: cfg.models?.length ? cfg.models : undefined,
      profiles: cfg.profiles?.length ? cfg.profiles : undefined,
      judge_model: cfg.judge || undefined,
      model,
      working_dir: dir,
      permission_mode: permMode,
    });
    return;
  }

  const type = window.appState.chatIsFirst ? 'chat' : 'chat_continue';
  window.appState.chatIsFirst = false;

  sendMsg({ type, message: fullText, model, working_dir: dir, prompt_profile: profile, permission_mode: permMode, council, ultra, mode, council_models: councilModels, remote_id: window.getRemoteId ? window.getRemoteId() : null });
}

function appendUserMessage(text) {
  dismissChatEmptyState();
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

function onStreamStart() {
  ensureAssistantBubble();
  if (currentAssistantBubble) {
    currentAssistantBubble.classList.remove('streaming-cursor');
    currentAssistantBubble.classList.add('waiting-for-response');
  }
}

function onToken(text) {
  // Transition from waiting indicator to live streaming on first token
  if (currentAssistantBubble && currentAssistantBubble.classList.contains('waiting-for-response')) {
    currentAssistantBubble.classList.remove('waiting-for-response');
    currentAssistantBubble.classList.add('streaming-cursor');
  }
  ensureAssistantBubble();
  streamBuffer += text;
  currentAssistantBubble.textContent = streamBuffer;
  scrollToBottom(document.getElementById('chat-messages'));
}

function onStreamEnd() {
  if (currentAssistantBubble) {
    currentAssistantBubble.classList.remove('streaming-cursor');
    currentAssistantBubble.classList.remove('waiting-for-response');
    currentAssistantBubble.innerHTML = renderMarkdown(streamBuffer);
  }
  currentAssistantBubble = null;
  currentToolCallsDiv    = null;
}

function onToolCall(msg) {
  // Backwards-compat: accept the legacy (name, summary) form too
  if (typeof msg === 'string') msg = { name: msg, summary: arguments[1] || '' };
  ensureAssistantBubble();

  const name = msg.name || '';
  const summary = msg.summary || '';
  const preview = msg.args_preview || null;
  const icon = globalThis.TOOL_ICONS?.[name] || '🔧';

  const toolBlock = document.createElement('details');
  toolBlock.className = 'tool-block tool-block-' + name;

  // File-mutating + structurally-interesting tools open expanded by default,
  // so the user sees the actual change without clicking the disclosure.
  const expandByDefault = (name === 'edit_file' || name === 'write_file' || name === 'bash');
  if (expandByDefault) toolBlock.open = true;

  const previewHtml = preview ? renderToolPreview(name, preview) : '';

  toolBlock.innerHTML = `
    <summary>
      <span class="tool-icon">${icon}</span>
      <span class="tool-name">${esc(name)}</span>
      <span class="tool-summary">${esc(summary)}</span>
    </summary>
    ${previewHtml}
    <div class="tool-detail pending">running…</div>
  `;

  currentToolCallsDiv.appendChild(toolBlock);
  scrollToBottom(document.getElementById('chat-messages'));

  window.appState.pendingToolCall = { element: toolBlock, name };
}

// ──────────────────────────────────────────────────────────────
// Tool-call inline previews — diff for edits, code for writes, etc.
// ──────────────────────────────────────────────────────────────

const _MAX_PREVIEW_LINES = 18;

function clampLines(text, n) {
  const lines = String(text).split('\n');
  if (lines.length <= n) return { text: lines.join('\n'), truncated: false, total: lines.length };
  return {
    text: lines.slice(0, n).join('\n'),
    truncated: true,
    total: lines.length,
  };
}

function renderToolPreview(name, p) {
  if (name === 'edit_file') return renderEditPreview(p);
  if (name === 'write_file') return renderWritePreview(p);
  if (name === 'bash') return renderBashPreview(p);
  if (name === 'read_file' || name === 'list_dir') return renderPathPreview(p);
  if (name === 'glob' || name === 'grep') return renderQueryPreview(name, p);
  if (name === 'web_search') return renderWebSearchPreview(p);
  if (name === 'web_fetch') return renderWebFetchPreview(p);
  return '';
}

function renderEditPreview(p) {
  // Split each side into lines and present as a unified diff: red "-" rows
  // for removed lines, green "+" rows for inserted ones. Long blocks clamp
  // with a "… X more lines" footer to keep the chat from exploding.
  const oldClamp = clampLines(p.old_string || '', _MAX_PREVIEW_LINES);
  const newClamp = clampLines(p.new_string || '', _MAX_PREVIEW_LINES);

  const oldRows = oldClamp.text
    ? oldClamp.text.split('\n').map(l => `<div class="diff-line diff-old">${esc(l)}</div>`).join('')
    : '';
  const newRows = newClamp.text
    ? newClamp.text.split('\n').map(l => `<div class="diff-line diff-new">${esc(l)}</div>`).join('')
    : '';

  const moreOld = oldClamp.truncated
    ? `<div class="diff-more">… ${oldClamp.total - _MAX_PREVIEW_LINES} more lines removed</div>` : '';
  const moreNew = newClamp.truncated
    ? `<div class="diff-more">… ${newClamp.total - _MAX_PREVIEW_LINES} more lines inserted</div>` : '';

  const replaceAll = p.replace_all ? '<span class="diff-flag">replace_all</span>' : '';

  return `
    <div class="tool-preview tool-diff">
      <div class="tool-preview-head">
        <span class="tool-preview-path">${esc(p.path || '')}</span>
        ${replaceAll}
      </div>
      <div class="diff-body">
        ${oldRows ? `<div class="diff-side diff-side-old"><div class="diff-side-label">− removing</div>${oldRows}${moreOld}</div>` : ''}
        ${newRows ? `<div class="diff-side diff-side-new"><div class="diff-side-label">+ inserting</div>${newRows}${moreNew}</div>` : ''}
      </div>
    </div>`;
}

function renderWritePreview(p) {
  const c = clampLines(p.content || '', _MAX_PREVIEW_LINES);
  const body = c.text
    ? c.text.split('\n').map(l => `<div class="code-line">${esc(l)}</div>`).join('')
    : '<div class="code-line code-empty">(empty file)</div>';
  const more = c.truncated
    ? `<div class="diff-more">… ${(p.lines || c.total) - _MAX_PREVIEW_LINES} more lines</div>` : '';
  return `
    <div class="tool-preview tool-write">
      <div class="tool-preview-head">
        <span class="tool-preview-path">${esc(p.path || '')}</span>
        <span class="tool-preview-meta">${p.lines || c.total} line${(p.lines || c.total) === 1 ? '' : 's'}</span>
      </div>
      <div class="code-body">${body}${more}</div>
    </div>`;
}

function renderBashPreview(p) {
  return `
    <div class="tool-preview tool-bash">
      <div class="tool-preview-head"><span class="tool-preview-meta">$</span></div>
      <pre class="code-body bash-body">${esc(p.command || '')}</pre>
    </div>`;
}

function renderPathPreview(p) {
  if (!p.path) return '';
  return `
    <div class="tool-preview tool-path">
      <span class="tool-preview-path">${esc(p.path)}</span>
    </div>`;
}

function renderQueryPreview(name, p) {
  const where = p.path ? ` <span class="tool-preview-meta">in ${esc(p.path)}</span>` : '';
  return `
    <div class="tool-preview tool-path">
      <code class="tool-preview-query">${esc(p.pattern || '')}</code>${where}
    </div>`;
}

function renderWebSearchPreview(p) {
  return `
    <div class="tool-preview tool-path">
      <span class="tool-preview-meta">🔎</span>
      <code class="tool-preview-query">${esc(p.query || '')}</code>
    </div>`;
}

function renderWebFetchPreview(p) {
  return `
    <div class="tool-preview tool-path">
      <span class="tool-preview-meta">↗</span>
      <code class="tool-preview-query">${esc(p.url || '')}</code>
    </div>`;
}

function onToolResult(name, ok, preview) {
  if (!window.appState.pendingToolCall) return;
  const { element } = window.appState.pendingToolCall;
  const detail = element.querySelector('.tool-detail');
  if (detail) {
    detail.className = `tool-detail ${ok ? 'ok' : 'fail'}`;
    detail.textContent = preview || (ok ? '✓ done' : '✗ failed');
  }
  // Mark the whole block so the styling reflects success/failure at a glance.
  element.classList.toggle('tool-block-fail', !ok);
  element.classList.toggle('tool-block-ok', !!ok);
  window.appState.pendingToolCall = null;
}

function onPlan(text) {
  const container = document.getElementById('chat-messages');
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-plan';
  wrap.innerHTML = `
    <div class="plan-card">
      <div class="plan-header">
        <span class="plan-icon">◆</span>
        <span>Plan</span>
      </div>
      <div class="plan-body">${esc(text)}</div>
    </div>`;
  container.appendChild(wrap);
  scrollToBottom(container);
}

function onDone(iterations, stopped) {
  setChatRunning(false);
  appendChatInfo(stopped
    ? `⏹ Stopped (${iterations} iteration${iterations !== 1 ? 's' : ''}) — refine and send to continue.`
    : `✓ Done (${iterations} iteration${iterations !== 1 ? 's' : ''})`);
  // Auto-persist after every completed turn so the conversation shows up in
  // history (and survives a reload) without requiring a "New Chat" click.
  // Reuses currentChatId when set, so the same chat is updated in place.
  if (window.appState.messages.length > 0) {
    sendMsg({ type: 'save_chat', chat_id: window.appState.currentChatId || '' });
  }
}

function onServerError(text) {
  appendChatError(text);
  setChatRunning(false);
}

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

// ──────────────────────────────────────────────────────────────
// Chat history
// ──────────────────────────────────────────────────────────────

function onChatLoaded(msg) {
  window.appState.messages = msg.messages || [];
  window.appState.model = msg.model || '';
  window.appState.currentChatId = msg.id || null;
  window.appState.chatIsFirst = false;

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
// Permission request UI
// ──────────────────────────────────────────────────────────────

function onPermissionRequest(msg) {
  const container = document.getElementById('chat-messages');
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-permission';

  const modeLabel = msg.mode === 'supervised' ? 'Supervised' : 'Controlled';
  wrap.innerHTML = `
    <div class="perm-card">
      <div class="perm-header">
        <span class="perm-icon">⚠</span>
        <span>Permission Required</span>
        <span class="perm-mode-badge">${modeLabel} Mode</span>
      </div>
      <div class="perm-body">
        <span class="perm-tool">${esc(msg.tool)}</span>
        wants to: <strong>${esc(msg.desc)}</strong>
      </div>
      <div class="perm-dir">${esc(msg.working_dir)}</div>
      <div class="perm-actions">
        <button class="perm-btn perm-allow" onclick="window.resolvePermission(this, true)">✓ Allow</button>
        <button class="perm-btn perm-deny"  onclick="window.resolvePermission(this, false)">✗ Deny</button>
      </div>
    </div>`;

  container.appendChild(wrap);
  scrollToBottom(container);
}

window.resolvePermission = function(btn, allow) {
  sendMsg({ type: 'permission_response', allow });
  const actions = btn.closest('.perm-actions');
  if (actions) {
    actions.innerHTML = allow
      ? '<span class="perm-resolved perm-resolved-allow">✓ Allowed</span>'
      : '<span class="perm-resolved perm-resolved-deny">✗ Denied</span>';
  }
};

// ──────────────────────────────────────────────────────────────
// Task checklist (todo_write) — a single live card, updated in place
// ──────────────────────────────────────────────────────────────
function onTodos(msg) {
  const todos = msg.todos || [];
  const container = document.getElementById('chat-messages');
  if (!container) return;
  const done = todos.filter(t => t.status === 'completed').length;
  const glyph = (s) => s === 'completed' ? '✓' : (s === 'in_progress' ? '▸' : '○');
  const rows = todos.map(t =>
    `<li class="todo-item todo-${esc(t.status)}"><span class="todo-glyph">${glyph(t.status)}</span>${esc(t.content)}</li>`
  ).join('');
  const inner = `
    <div class="todo-header">
      <span class="todo-title">Tasks</span>
      <span class="todo-count">${done}/${todos.length}</span>
    </div>
    <ul class="todo-list">${rows}</ul>`;

  // Reuse the most recent todo card if it's still the last meaningful block,
  // otherwise append a fresh one so progress reads top-to-bottom.
  let card = window._lastTodoCard;
  if (!card || !card.parentElement) {
    card = document.createElement('div');
    card.className = 'msg msg-todos';
    container.appendChild(card);
    window._lastTodoCard = card;
  }
  card.innerHTML = `<div class="todo-card">${inner}</div>`;
  scrollToBottom(container);
}

// ──────────────────────────────────────────────────────────────
// ask_user — question card with optional quick-pick options
// ──────────────────────────────────────────────────────────────
function onUserQuestion(msg) {
  const container = document.getElementById('chat-messages');
  if (!container) return;
  const wrap = document.createElement('div');
  wrap.className = 'msg msg-question';
  const opts = (msg.options || []).map(o =>
    `<button class="uq-opt" onclick="window.resolveUserQuestion(this, ${JSON.stringify(esc(o)).replace(/"/g, '&quot;')})">${esc(o)}</button>`
  ).join('');
  wrap.innerHTML = `
    <div class="uq-card">
      <div class="uq-header"><span class="uq-icon">?</span><span>The agent needs your input</span></div>
      <div class="uq-body">${esc(msg.question)}</div>
      ${opts ? `<div class="uq-options">${opts}</div>` : ''}
      <div class="uq-input-row">
        <input type="text" class="uq-input" placeholder="Type your answer…"
               onkeydown="if(event.key==='Enter'){window.resolveUserQuestionInput(this);}">
        <button class="uq-send" onclick="window.resolveUserQuestionInput(this.previousElementSibling)">Send</button>
      </div>
    </div>`;
  container.appendChild(wrap);
  scrollToBottom(container);
  const inp = wrap.querySelector('.uq-input');
  if (inp) inp.focus();
}

function _finishQuestion(node, answer) {
  const card = node.closest('.uq-card');
  if (card) {
    card.innerHTML = `<div class="uq-header"><span class="uq-icon">✓</span><span>Answered</span></div>
      <div class="uq-answered">${esc(answer)}</div>`;
  }
}
window.resolveUserQuestion = function(btn, answer) {
  sendMsg({ type: 'user_response', answer });
  _finishQuestion(btn, answer);
};
window.resolveUserQuestionInput = function(input) {
  const answer = (input.value || '').trim();
  if (!answer) return;
  sendMsg({ type: 'user_response', answer });
  _finishQuestion(input, answer);
};

// ──────────────────────────────────────────────────────────────
// Initialization
// ──────────────────────────────────────────────────────────────

function fetchPromptProfiles() {
  console.log('[app] Fetching prompt profiles...');
  fetch('/api/profiles')
    .then(r => {
      if (!r.ok) {
        console.error('[app] Failed to fetch profiles:', r.status, r.statusText);
        return Promise.reject(new Error(`HTTP ${r.status}`));
      }
      return r.json();
    })
    .then(data => {
      console.log('[app] Profiles received:', data);
      populatePromptProfiles(data.profiles || []);
    })
    .catch(err => {
      console.error('[app] Profile fetch error:', err);
      populatePromptProfiles([]);
    });
}

function populatePromptProfiles(profiles) {
  const sel = document.getElementById('chat-profile-select');
  if (!sel) {
    console.error('[app] chat-profile-select element not found!');
    return;
  }
  const prev = sel.value;
  sel.innerHTML = '';

  if (!profiles || !profiles.length) {
    // No profiles from server, use fallback list
    const fallback = ['base', 'coder', 'analyst', 'local'];
    fallback.forEach(p => {
      const o = document.createElement('option');
      o.value = p;
      o.textContent = p.charAt(0).toUpperCase() + p.slice(1);
      sel.appendChild(o);
    });
    console.log('[app] Using fallback profiles:', fallback);
  } else {
    profiles.sort();
    profiles.forEach(p => {
      const o = document.createElement('option');
      o.value = p;
      o.textContent = p.charAt(0).toUpperCase() + p.slice(1);
      sel.appendChild(o);
    });
    console.log('[app] Populated profiles from server:', profiles);
  }

  // Restore previous selection if still valid, otherwise fall back to config,
  // then to the 'base' default profile, then to the first available item.
  const pref = prev || window.appState?.config?.prompt_profile || '';
  const allValues = Array.from(sel.options).map(o => o.value);
  if (pref && allValues.includes(pref)) {
    sel.value = pref;
    console.log('[app] Restored profile selection:', pref);
  } else if (allValues.includes('base')) {
    sel.value = 'base';
    console.log('[app] Defaulted to base profile');
  } else if (allValues.length > 0) {
    sel.value = allValues[0];
    console.log('[app] Defaulted to first profile:', allValues[0]);
  }
}

function initApp() {
  // Tab switching
  document.querySelectorAll('.nav-btn').forEach(btn => {
    // Links (e.g. the Lab nav item) navigate natively — skip tab handling.
    if (!btn.dataset.tab) return;
    btn.addEventListener('click', () => {
      document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const tab = btn.dataset.tab;
      document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
      document.getElementById('tab-' + tab).classList.add('active');
    });
  });

  // Chat input
  const textarea = document.getElementById('chat-textarea');
  if (textarea) {
    textarea.addEventListener('keydown', (e) => {
      if (filePickerVisible() && handlePickerKey(e)) return;
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChat();
      }
    });

    textarea.addEventListener('input', () => {
      autoResizeTextarea(textarea);
      maybeShowFilePicker(textarea);
    });

    // Close picker when caret leaves the @-token
    textarea.addEventListener('blur', () => setTimeout(hideFilePicker, 150));
  }

  // Expose for slash-commands.js
  window.setChatRunningExternal = setChatRunning;

  // Parallel-agents popover
  initParallelPopover();

  // Council-models popover (Improved / Ultra role → model dropdowns)
  initCouncilPopover();

  // Send button doubles as a stop button while the agent is working.
  document.getElementById('chat-send-btn')?.addEventListener('click', () => {
    if (window.appState.running) stopChat();
    else sendChat();
  });

  // ── Working directory: the hero empty-state picker is the only UI for it.
  //    It writes straight into appState (window.setWorkingDir). The picker is
  //    rebuilt on New Chat, so delegate its events from the document.
  //    browseDir() dispatches a synthetic 'change' after writing the path.
  document.addEventListener('input',  (e) => { if (e.target?.id === 'empty-dir-input') window.setWorkingDir(e.target.value); });
  document.addEventListener('change', (e) => { if (e.target?.id === 'empty-dir-input') window.setWorkingDir(e.target.value); });

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
    if (window.appState.messages.length > 0) {
      // Archive the current conversation, but don't let the async chat_saved
      // reply re-adopt its id onto the now-empty chat (see chat_saved handler).
      window.appState.suppressNextChatSavedId = true;
      sendMsg({ type: 'save_chat', chat_id: window.appState.currentChatId || '' });
    }
    sendMsg({ type: 'chat_clear' });
    clearChatMessages();
    refreshHistory();
  });

  // Mode selector (Standard · Improved · Ultra) — set active button + data-mode.
  function _setMode(mode) {
    const seg = document.getElementById('mode-seg');
    if (!seg) return;
    seg.dataset.mode = mode;
    seg.querySelectorAll('.mode-seg-btn').forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle('active', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    });
    // The council-models gear only makes sense when a council will actually run.
    const gear = document.getElementById('council-config-btn');
    if (gear) gear.style.display = (mode === 'standard' || seg.classList.contains('disabled')) ? 'none' : 'inline-flex';
    if (mode === 'standard') window.hideCouncilPopover?.();
  }
  document.getElementById('mode-seg')?.querySelectorAll('.mode-seg-btn').forEach((btn) => {
    btn.addEventListener('click', () => { if (!btn.disabled) _setMode(btn.dataset.mode); });
  });

  // Backend select change handler — send switch_backend and refresh model list.
  // Improved/Ultra need a cloud pool — disable them on Ollama and snap back to
  // Standard (the backend also falls back gracefully).
  function _updateCouncilAvailability(backend) {
    const seg = document.getElementById('mode-seg');
    if (!seg) return;
    const ok = backend !== 'ollama';
    seg.classList.toggle('disabled', !ok);
    seg.querySelectorAll('.mode-seg-btn').forEach((b) => {
      if (b.dataset.mode !== 'standard') b.disabled = !ok;
    });
    if (!ok && seg.dataset.mode !== 'standard') _setMode('standard');
    // Re-sync the gear with the (possibly unchanged) current mode.
    _setMode(seg.dataset.mode);
    seg.title = ok
      ? 'Agent mode — Standard: one model. Improved: a council (Thinker · Worker · Verifier). Ultra: council + multi-model debate on the plan and completion (strongest, more tokens).'
      : 'Improved / Ultra need a cloud backend (e-INFRA / NIM). Not available on local Ollama.';
  }
  window._updateCouncilAvailability = _updateCouncilAvailability;

  function _onBackendChange(e) {
    const backend = e.target.value;
    const chatSel = document.getElementById('backend-select');
    if (chatSel) { chatSel.value = backend; chatSel.dataset.backend = backend; }
    _updateCouncilAvailability(backend);
    appendChatInfo(`🔄 Switching to [bold]${getProviderName(backend)}[/bold] backend…`);
    sendMsg({ type: 'switch_backend', backend });
    setTimeout(() => sendMsg({ type: 'list_models' }), 600);
  }
  document.getElementById('backend-select')?.addEventListener('change', _onBackendChange);
  _updateCouncilAvailability(document.getElementById('backend-select')?.value || 'einfra');

  // Model select change handler - update the badge in the sidebar
  document.getElementById('chat-model-select')?.addEventListener('change', (e) => {
    const badge = document.getElementById('model-badge');
    if (badge) badge.textContent = e.target.value || '—';
  });

  // Profile and permission select change handlers
  document.getElementById('chat-profile-select')?.addEventListener('change', (e) => {
    const label = e.target.value ? (e.target.options[e.target.selectedIndex]?.textContent || e.target.value) : 'Default';
    appendChatInfo(`📝 Profile set to [bold]${label}[/bold]. Will apply to next task.`);
  });

  document.getElementById('chat-permission-select')?.addEventListener('change', (e) => {
    const modeNames = { autonomous: 'Autonomous', controlled: 'Controlled', supervised: 'Supervised' };
    appendChatInfo(`🛡️ Permission mode set to [bold]${modeNames[e.target.value]}[/bold]. Will apply to next tool execution.`);
  });

  // Settings refresh button
  document.getElementById('settings-refresh-btn')?.addEventListener('click', () => {
    sendMsg({ type: 'get_config' });
    sendMsg({ type: 'list_providers' });
    sendMsg({ type: 'list_mcp' });
    sendMsg({ type: 'mcp_registry' });
  });

  // Custom-provider management
  initProviderForm();

  // Remote (SSH) hosts management
  initRemotesForm();
  window.renderExecToggle?.();

  // MCP server management
  initMcpPanel();

  // Fetch available prompt profiles dynamically
  fetchPromptProfiles();

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
      // On open - request config, providers, and models; reset stuck state
      sendMsg({ type: 'get_config' });
      sendMsg({ type: 'list_providers' });
      sendMsg({ type: 'list_models' });
      sendMsg({ type: 'list_mcp' });
      sendMsg({ type: 'mcp_registry' });
      if (window.appState.running) {
        window.appState.running = false;
        setChatRunning(false);
      }
    },
    () => {
      // On close - show error
      appendChatError('Disconnected from server. Reconnecting...');
    },
    handleServerMessage
  );

  console.log('OctoSlave Web UI initialized');
}

// ──────────────────────────────────────────────────────────────
// Custom providers (Settings tab)
// ──────────────────────────────────────────────────────────────

function onProviders(msg) {
  populateBackendSelects(msg.providers || [], msg.active);
  renderProvidersList(msg.providers || [], msg.active);
}

function renderProvidersList(providers, active) {
  const host = document.getElementById('providers-list');
  if (!host) return;
  host.innerHTML = providers.map(p => {
    const tag = p.kind === 'custom' ? 'custom' : 'built-in';
    const isActive = p.id === active;
    const activeTag = isActive ? '<span class="provider-tag tag-active">active</span>' : '';
    const url = p.base_url ? `<span class="provider-url">${esc(p.base_url)}</span>` : '<span class="provider-url"></span>';
    const actions = p.kind === 'custom'
      ? `<div class="provider-actions">
           <button class="btn-link" data-act="use" data-id="${esc(p.id)}">use</button>
           <button class="btn-link btn-link-danger" data-act="remove" data-id="${esc(p.id)}">remove</button>
         </div>`
      : `<div class="provider-actions">
           <button class="btn-link" data-act="use" data-id="${esc(p.id)}">use</button>
         </div>`;
    return `
      <div class="provider-row${p.kind === 'builtin' ? ' provider-builtin' : ''}${isActive ? ' provider-active' : ''}">
        <span class="provider-name">${esc(p.name)}</span>
        <span class="provider-id">${esc(p.id)}</span>
        ${url}
        <span class="provider-tag">${tag}</span>
        ${activeTag}
        ${actions}
      </div>`;
  }).join('');

  // Wire row buttons
  host.querySelectorAll('button[data-act]').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.id;
      const act = btn.dataset.act;
      if (act === 'use') {
        sendMsg({ type: 'switch_backend', backend: id });
        setTimeout(() => sendMsg({ type: 'list_models' }), 600);
      } else if (act === 'remove') {
        if (!confirm(`Remove provider "${id}"?`)) return;
        sendMsg({ type: 'remove_provider', id });
      }
    });
  });
}

function _provFormValues() {
  return {
    id:            document.getElementById('prov-id')?.value || '',
    name:          document.getElementById('prov-name')?.value || '',
    base_url:      document.getElementById('prov-base-url')?.value || '',
    api_key:       document.getElementById('prov-api-key')?.value || '',
    default_model: document.getElementById('prov-default-model')?.value || '',
    models:        document.getElementById('prov-models')?.value || '',
  };
}

function _setProvFormStatus(text, kind) {
  const el = document.getElementById('prov-form-status');
  if (!el) return;
  el.textContent = text || '';
  el.classList.remove('status-ok', 'status-fail');
  if (kind === 'ok') el.classList.add('status-ok');
  if (kind === 'fail') el.classList.add('status-fail');
}

function _resetProvForm() {
  ['prov-id', 'prov-name', 'prov-base-url', 'prov-api-key',
   'prov-default-model', 'prov-models'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  _setProvFormStatus('');
}

function onProviderTest(msg) {
  if (msg.ok) {
    const n = msg.count || (msg.models || []).length;
    _setProvFormStatus(`✓ Connected — ${n} model${n === 1 ? '' : 's'} returned`, 'ok');
  } else {
    _setProvFormStatus(`✗ ${msg.error || 'connection failed'}`, 'fail');
  }
}

function initProviderForm() {
  const idInp = document.getElementById('prov-id');
  if (idInp) {
    idInp.addEventListener('input', () => {
      // Lowercase + slugify in real time
      const v = idInp.value.toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
      if (v !== idInp.value) idInp.value = v;
      // Default the display name to the id if name is still empty
      const nameInp = document.getElementById('prov-name');
      if (nameInp && !nameInp.dataset.touched) {
        nameInp.value = idInp.value;
      }
    });
  }
  const nameInp = document.getElementById('prov-name');
  nameInp?.addEventListener('input', () => { nameInp.dataset.touched = '1'; });

  document.getElementById('prov-test-btn')?.addEventListener('click', () => {
    const v = _provFormValues();
    if (!v.base_url) {
      _setProvFormStatus('Base URL is required to test.', 'fail');
      return;
    }
    _setProvFormStatus('Testing connection…');
    sendMsg({ type: 'test_provider', provider: v });
  });

  document.getElementById('prov-save-btn')?.addEventListener('click', () => {
    const v = _provFormValues();
    if (!v.id) { _setProvFormStatus('ID is required.', 'fail'); return; }
    if (!v.base_url) { _setProvFormStatus('Base URL is required.', 'fail'); return; }
    if (!v.default_model) { _setProvFormStatus('Default model is required.', 'fail'); return; }
    _setProvFormStatus('Saving…');
    sendMsg({ type: 'add_provider', provider: v });
    // The server reply (providers + info) will reset the form via onProviders
    // when the new id appears in the list — also clear here optimistically.
    setTimeout(() => {
      _resetProvForm();
      const form = document.getElementById('provider-add-form');
      if (form) form.open = false;
    }, 300);
  });
}

// ──────────────────────────────────────────────────────────────
// Remote hosts (SSH)
// ──────────────────────────────────────────────────────────────

async function refreshRemotes() {
  try {
    const res = await fetch('/api/remotes');
    const data = await res.json();
    window.appState.remotes = data.remotes || [];
  } catch (err) {
    console.error('Failed to load remotes:', err);
  }
  window.renderRemotesCard?.();
  window.renderExecToggle?.();
}

window.renderRemotesCard = function () {
  const host = document.getElementById('remotes-list');
  if (!host) return;
  const remotes = window.appState.remotes || [];
  const active = window.appState.remoteId;
  if (!remotes.length) {
    host.innerHTML = '<div class="remotes-empty">No remote hosts yet. Add one below.</div>';
    return;
  }
  host.innerHTML = remotes.map(r => {
    const isActive = r.id === active;
    const target = `${r.user ? esc(r.user) + '@' : ''}${esc(r.host)}${r.port && r.port !== 22 ? ':' + r.port : ''}`;
    return `
      <div class="remote-row${isActive ? ' remote-active' : ''}">
        <span class="remote-name">${esc(r.name || r.id)}</span>
        <span class="remote-target">${target}<span class="remote-dir">:${esc(r.remote_dir || '.')}</span></span>
        ${isActive ? '<span class="provider-tag tag-active">active</span>' : ''}
        <div class="remote-actions">
          <button class="btn-link" data-ract="use" data-id="${esc(r.id)}">use</button>
          <button class="btn-link btn-link-danger" data-ract="remove" data-id="${esc(r.id)}">remove</button>
        </div>
      </div>`;
  }).join('');
  host.querySelectorAll('button[data-ract]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.id;
      if (btn.dataset.ract === 'use') {
        window.setRemoteMode('remote', id);
      } else {
        if (!confirm(`Remove remote "${id}"?`)) return;
        await fetch(`/api/remotes/${encodeURIComponent(id)}`, { method: 'DELETE' });
        if (window.appState.remoteId === id) window.setRemoteMode('local');
        refreshRemotes();
      }
    });
  });
};

window.openRemotesConfig = function () {
  // Switch to the Settings tab and scroll the Remotes card into view.
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));
  document.querySelector('.nav-btn[data-tab="settings"]')?.classList.add('active');
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.remove('active'));
  document.getElementById('tab-settings')?.classList.add('active');
  const card = document.getElementById('remotes-card');
  if (card) {
    card.scrollIntoView({ behavior: 'smooth', block: 'center' });
    card.classList.add('card-flash');
    setTimeout(() => card.classList.remove('card-flash'), 1200);
    document.getElementById('remote-host')?.focus();
  }
};

function _remoteFormValues() {
  return {
    id:            (document.getElementById('remote-id')?.value || '').trim(),
    name:          (document.getElementById('remote-name')?.value || '').trim(),
    host:          (document.getElementById('remote-host')?.value || '').trim(),
    user:          (document.getElementById('remote-user')?.value || '').trim(),
    port:          parseInt(document.getElementById('remote-port')?.value || '22', 10) || 22,
    identity_file: (document.getElementById('remote-identity')?.value || '').trim(),
  };
}

function _setRemoteStatus(text, kind) {
  const el = document.getElementById('remote-test-status');
  if (!el) return;
  el.textContent = text || '';
  el.classList.remove('status-ok', 'status-fail');
  if (kind === 'ok') el.classList.add('status-ok');
  if (kind === 'fail') el.classList.add('status-fail');
}

function initRemotesForm() {
  const idInp = document.getElementById('remote-id');
  idInp?.addEventListener('input', () => {
    const v = idInp.value.toLowerCase().replace(/[^a-z0-9_-]+/g, '-');
    if (v !== idInp.value) idInp.value = v;
  });

  document.getElementById('remote-test-btn')?.addEventListener('click', async () => {
    const v = _remoteFormValues();
    if (!v.host) { _setRemoteStatus('Host is required to test.', 'fail'); return; }
    _setRemoteStatus('Testing connection…');
    try {
      const res = await fetch('/api/remotes/test', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(v),
      });
      const data = await res.json();
      _setRemoteStatus((data.ok ? '✓ ' : '✗ ') + (data.message || ''), data.ok ? 'ok' : 'fail');
    } catch (err) {
      _setRemoteStatus('✗ ' + err, 'fail');
    }
  });

  // (remote directory picker is defined below as window.openRemoteDirPicker)

  document.getElementById('remote-add-btn')?.addEventListener('click', async () => {
    const v = _remoteFormValues();
    if (!v.host) { _setRemoteStatus('Host is required.', 'fail'); return; }
    _setRemoteStatus('Saving…');
    try {
      const res = await fetch('/api/remotes', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(v),
      });
      const data = await res.json();
      if (!data.ok) { _setRemoteStatus('✗ ' + (data.error || 'failed'), 'fail'); return; }
      _setRemoteStatus('✓ added', 'ok');
      ['remote-id', 'remote-name', 'remote-host', 'remote-user', 'remote-identity']
        .forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
      const portEl = document.getElementById('remote-port'); if (portEl) portEl.value = '22';
      await refreshRemotes();
      // Activate the freshly added remote.
      if (data.remote?.id) window.setRemoteMode('remote', data.remote.id);
    } catch (err) {
      _setRemoteStatus('✗ ' + err, 'fail');
    }
  });
}

// Remote directory picker — a small modal that navigates folders on the active
// remote host over SSH and writes the chosen path into the working-dir field.
window.openRemoteDirPicker = function (inputId) {
  const rid = window.getRemoteId?.();
  if (!rid) return;
  let curPath = '';   // '' → server resolves the remote home

  const overlay = document.createElement('div');
  overlay.className = 'remote-picker-overlay';
  overlay.innerHTML = `
    <div class="remote-picker" role="dialog" aria-label="Choose a remote folder">
      <div class="remote-picker-head">
        <span class="remote-picker-title">🌐 Remote folder</span>
        <button class="remote-picker-close" title="Close">✕</button>
      </div>
      <div class="remote-picker-path" id="rp-path">…</div>
      <div class="remote-picker-list" id="rp-list"><div class="remote-picker-loading">Loading…</div></div>
      <div class="remote-picker-foot">
        <button class="btn-secondary btn-sm" id="rp-cancel">Cancel</button>
        <button class="btn-primary btn-sm" id="rp-use">Use this folder</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  const close = () => overlay.remove();
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.querySelector('.remote-picker-close').addEventListener('click', close);
  overlay.querySelector('#rp-cancel').addEventListener('click', close);
  overlay.querySelector('#rp-use').addEventListener('click', () => {
    if (curPath) {
      window.setWorkingDir(curPath);
      const input = document.getElementById(inputId);
      if (input) { input.value = curPath; input.dispatchEvent(new Event('change', { bubbles: true })); }
    }
    close();
  });

  async function load(path) {
    const listEl = overlay.querySelector('#rp-list');
    listEl.innerHTML = '<div class="remote-picker-loading">Loading…</div>';
    try {
      const res = await fetch(`/api/remote-dirs?remote_id=${encodeURIComponent(rid)}&path=${encodeURIComponent(path)}`);
      const data = await res.json();
      if (!data.ok) { listEl.innerHTML = `<div class="remote-picker-error">${esc(data.error || 'failed')}</div>`; return; }
      curPath = data.path;
      overlay.querySelector('#rp-path').textContent = curPath;
      const rows = [`<button class="remote-picker-row remote-picker-up" data-path="${esc(curPath + '/..')}">↩ ..</button>`]
        .concat((data.dirs || []).map(d =>
          `<button class="remote-picker-row" data-path="${esc((curPath.endsWith('/') ? curPath : curPath + '/') + d)}">📁 ${esc(d)}</button>`));
      listEl.innerHTML = rows.join('') || '<div class="remote-picker-empty">(no sub-folders)</div>';
      listEl.querySelectorAll('.remote-picker-row').forEach(btn =>
        btn.addEventListener('click', () => load(btn.dataset.path)));
    } catch (err) {
      listEl.innerHTML = `<div class="remote-picker-error">${esc(String(err))}</div>`;
    }
  }
  load(curPath);
};

// ──────────────────────────────────────────────────────────────
// MCP servers (Settings tab) — wire in external tools
// ──────────────────────────────────────────────────────────────

let _mcpRegistry = [];

function onMcpRegistry(msg) {
  _mcpRegistry = msg.entries || [];
  renderMcpRegistry();
}

function renderMcpRegistry() {
  const host = document.getElementById('mcp-registry-list');
  if (!host) return;
  if (!_mcpRegistry.length) { host.innerHTML = ''; return; }
  // Group by category
  const cats = {};
  _mcpRegistry.forEach(e => { (cats[e.category] = cats[e.category] || []).push(e); });
  host.innerHTML = Object.entries(cats).map(([cat, items]) => `
    <div class="mcp-cat">${esc(cat)}</div>
    ${items.map(e => {
      const key = e.inputs.some(i => i.secret) ? ' <span class="mcp-key" title="Needs an API key/token">🔑</span>' : '';
      let badge;
      if (e.installed) badge = '<span class="mcp-badge mcp-badge-on">installed</span>';
      else if (e.runtime === 'http') badge = '<span class="mcp-badge">remote</span>';
      else if (e.runtime_available) badge = `<span class="mcp-badge">${esc(e.runtime)}</span>`;
      else badge = `<span class="mcp-badge mcp-badge-warn">needs ${esc(e.runtime)}</span>`;
      const btn = e.installed
        ? `<button class="btn-link" disabled>installed</button>`
        : (e.runtime_available
            ? `<button class="btn-link" data-mcp-install="${esc(e.id)}">install</button>`
            : `<button class="btn-link" disabled title="${esc(e.runtime_hint)}">unavailable</button>`);
      return `
        <div class="mcp-reg-row">
          <div class="mcp-reg-main">
            <span class="mcp-reg-name">${esc(e.name)}</span>
            <span class="mcp-reg-id">${esc(e.id)}</span>${key} ${badge}
          </div>
          <div class="mcp-reg-summary">${esc(e.summary)}</div>
          <div class="mcp-reg-actions">${btn}</div>
        </div>`;
    }).join('')}
  `).join('');

  host.querySelectorAll('button[data-mcp-install]').forEach(btn => {
    btn.addEventListener('click', () => mcpInstallFlow(btn.dataset.mcpInstall));
  });
}

function mcpInstallFlow(id) {
  const entry = _mcpRegistry.find(e => e.id === id);
  if (!entry) return;
  const values = {};
  for (const inp of entry.inputs) {
    let def = '';
    if (inp.default_wd) {
      def = document.getElementById('settings-working-dir')?.value || '.';
    }
    const label = inp.secret ? `${inp.prompt} (kept private)` : inp.prompt;
    const v = window.prompt(`${entry.name}: ${label}`, def);
    if (v === null) return;  // cancelled
    values[inp.key] = v.trim();
  }
  sendMsg({ type: 'install_mcp', id, values });
}

function onMcpServers(msg) {
  const host = document.getElementById('mcp-servers-list');
  if (!host) return;
  const servers = msg.servers || [];
  if (!servers.length) {
    host.innerHTML = '<div class="mcp-empty">No MCP servers configured yet. Install one from the catalog below, or add a custom server.</div>';
    return;
  }
  host.innerHTML = servers.map(s => {
    let dot;
    if (!s.enabled) dot = '<span class="mcp-dot mcp-dot-off"></span>disabled';
    else if (s.connected) dot = `<span class="mcp-dot mcp-dot-on"></span>connected · ${s.tool_count} tools`;
    else if (s.error) dot = `<span class="mcp-dot mcp-dot-err"></span>error`;
    else dot = '<span class="mcp-dot"></span>not connected';
    const toolList = (s.tools && s.tools.length)
      ? `<div class="mcp-tools" title="${esc(s.tools.join(', '))}">${esc(s.tools.slice(0, 8).join(', '))}${s.tools.length > 8 ? ` +${s.tools.length - 8}` : ''}</div>`
      : '';
    const errLine = (!s.connected && s.enabled && s.error) ? `<div class="mcp-err">${esc(s.error)}</div>` : '';
    return `
      <div class="mcp-row">
        <div class="mcp-row-head">
          <span class="mcp-name">${esc(s.name)}</span>
          <span class="mcp-transport">${esc(s.transport)}</span>
          <span class="mcp-status">${dot}</span>
          <div class="mcp-actions">
            <button class="btn-link" data-mcp-act="${s.enabled ? 'disable' : 'enable'}" data-name="${esc(s.name)}">${s.enabled ? 'disable' : 'enable'}</button>
            <button class="btn-link btn-link-danger" data-mcp-act="remove" data-name="${esc(s.name)}">remove</button>
          </div>
        </div>
        <div class="mcp-target">${esc(s.target)}</div>
        ${errLine}
        ${toolList}
      </div>`;
  }).join('');

  host.querySelectorAll('button[data-mcp-act]').forEach(btn => {
    btn.addEventListener('click', () => {
      const name = btn.dataset.name;
      const act = btn.dataset.mcpAct;
      if (act === 'remove') {
        if (!confirm(`Remove MCP server "${name}"?`)) return;
        sendMsg({ type: 'remove_mcp', name });
      } else {
        sendMsg({ type: 'toggle_mcp', name, enabled: act === 'enable' });
      }
    });
  });
}

function initMcpPanel() {
  document.getElementById('mcp-reconnect-btn')?.addEventListener('click', () => {
    sendMsg({ type: 'reconnect_mcp' });
  });

  // Custom MCP add form
  document.getElementById('mcp-add-btn')?.addEventListener('click', () => {
    const name = (document.getElementById('mcp-name')?.value || '').trim();
    const transport = document.getElementById('mcp-transport')?.value || 'stdio';
    const statusEl = document.getElementById('mcp-form-status');
    const setStatus = (t, k) => {
      if (!statusEl) return;
      statusEl.textContent = t || '';
      statusEl.classList.remove('status-ok', 'status-fail');
      if (k) statusEl.classList.add(k === 'ok' ? 'status-ok' : 'status-fail');
    };
    if (!name) { setStatus('Name is required.', 'fail'); return; }
    let server;
    if (transport === 'http') {
      const url = (document.getElementById('mcp-url')?.value || '').trim();
      if (!url) { setStatus('URL is required for http.', 'fail'); return; }
      const headersRaw = (document.getElementById('mcp-headers')?.value || '').trim();
      const headers = {};
      headersRaw.split(',').forEach(p => {
        const i = p.indexOf('=');
        if (i > 0) headers[p.slice(0, i).trim()] = p.slice(i + 1).trim();
      });
      server = { name, url, headers, enabled: true };
    } else {
      const command = (document.getElementById('mcp-command')?.value || '').trim();
      if (!command) { setStatus('Command is required for stdio.', 'fail'); return; }
      const argsRaw = (document.getElementById('mcp-args')?.value || '').trim();
      const args = argsRaw ? argsRaw.split(/\s+/) : [];
      const envRaw = (document.getElementById('mcp-env')?.value || '').trim();
      const env = {};
      envRaw.split(',').forEach(p => {
        const i = p.indexOf('=');
        if (i > 0) env[p.slice(0, i).trim()] = p.slice(i + 1).trim();
      });
      server = { name, command, args, env, enabled: true };
    }
    setStatus('Adding…');
    sendMsg({ type: 'add_mcp', server });
    setTimeout(() => {
      ['mcp-name', 'mcp-url', 'mcp-headers', 'mcp-command', 'mcp-args', 'mcp-env'].forEach(id => {
        const el = document.getElementById(id); if (el) el.value = '';
      });
      setStatus('');
      const form = document.getElementById('mcp-add-form');
      if (form) form.open = false;
    }, 300);
  });

  // Toggle stdio/http fields
  document.getElementById('mcp-transport')?.addEventListener('change', (e) => {
    const isHttp = e.target.value === 'http';
    const stdioFields = document.getElementById('mcp-stdio-fields');
    const httpFields = document.getElementById('mcp-http-fields');
    if (stdioFields) stdioFields.style.display = isHttp ? 'none' : 'block';
    if (httpFields) httpFields.style.display = isHttp ? 'block' : 'none';
  });
}

// ──────────────────────────────────────────────────────────────
// Parallel-agents popover
// ──────────────────────────────────────────────────────────────

window.parallelConfig = {
  enabled: false,
  n: 3,
  strategy: 'best',
  judge: '',
  models: [],
  profiles: [],
};

const PARALLEL_PROFILES = ['(inherit)', 'base', 'coder', 'analyst', 'local'];

function modelOptionsHtml(includeBlank = true) {
  const sel = document.getElementById('chat-model-select');
  const opts = sel ? Array.from(sel.options).map(o => o.value).filter(Boolean) : [];
  let html = '';
  if (includeBlank) html += '<option value="">(inherit)</option>';
  opts.forEach(m => {
    html += `<option value="${esc(m)}">${esc(m)}</option>`;
  });
  return html;
}

function refreshParallelRows() {
  const host = document.getElementById('parallel-rows');
  if (!host) return;
  const n = window.parallelConfig.n;
  const cur = window.parallelConfig.models || [];
  const curP = window.parallelConfig.profiles || [];
  let html = '';
  for (let i = 0; i < n; i++) {
    const profOpts = PARALLEL_PROFILES.map(p =>
      `<option value="${p === '(inherit)' ? '' : p}"${(curP[i] || '') === (p === '(inherit)' ? '' : p) ? ' selected' : ''}>${p}</option>`
    ).join('');
    const modelOpts = modelOptionsHtml(true).replace(
      `value="${esc(cur[i] || '')}"`,
      `value="${esc(cur[i] || '')}" selected`
    );
    html += `
      <div class="parallel-cand-row">
        <span class="parallel-cand-idx">#${i}</span>
        <select class="parallel-cand-model" data-idx="${i}">${modelOpts}</select>
        <select class="parallel-cand-profile" data-idx="${i}">${profOpts}</select>
      </div>`;
  }
  host.innerHTML = html;
  // Re-attach listeners
  host.querySelectorAll('.parallel-cand-model').forEach(sel => {
    sel.addEventListener('change', e => {
      const i = parseInt(e.target.dataset.idx);
      window.parallelConfig.models[i] = e.target.value || '';
      // Trim trailing empties so backend gets a tidy array
      while (window.parallelConfig.models.length && !window.parallelConfig.models.at(-1)) {
        window.parallelConfig.models.pop();
      }
    });
  });
  host.querySelectorAll('.parallel-cand-profile').forEach(sel => {
    sel.addEventListener('change', e => {
      const i = parseInt(e.target.dataset.idx);
      window.parallelConfig.profiles[i] = e.target.value || '';
      while (window.parallelConfig.profiles.length && !window.parallelConfig.profiles.at(-1)) {
        window.parallelConfig.profiles.pop();
      }
    });
  });
}

function refreshParallelBadge() {
  const badge = document.getElementById('parallel-count-badge');
  const btn = document.getElementById('chat-parallel-btn');
  if (!badge || !btn) return;
  if (window.parallelConfig.enabled) {
    badge.textContent = String(window.parallelConfig.n);
    badge.style.display = 'inline-flex';
    btn.classList.add('active');
  } else {
    badge.style.display = 'none';
    btn.classList.remove('active');
  }
}

function refreshParallelJudge() {
  const sel = document.getElementById('parallel-judge');
  if (!sel) return;
  sel.innerHTML = modelOptionsHtml(true);
  if (window.parallelConfig.judge) sel.value = window.parallelConfig.judge;
}

function positionParallelPopover() {
  const btn = document.getElementById('chat-parallel-btn');
  const popover = document.getElementById('parallel-popover');
  if (!btn || !popover) return;

  const rect = btn.getBoundingClientRect();
  const margin = 16;
  const vw = window.innerWidth;
  const vh = window.innerHeight;

  // Decide width — already styled, but read it back to clamp left coord
  const width = Math.min(popover.offsetWidth || 480, vw - margin * 2);
  popover.style.width = width + 'px';

  // Available space above and below the button. Prefer above (composer
  // sits at bottom), fall back to below if there's no room.
  const spaceAbove = rect.top - margin;
  const spaceBelow = vh - rect.bottom - margin;
  const naturalHeight = popover.scrollHeight || 480;

  let placeAbove = spaceAbove >= 280 || spaceAbove >= spaceBelow;
  let maxH;
  if (placeAbove) {
    maxH = Math.min(naturalHeight, spaceAbove);
    popover.style.top = Math.max(margin, rect.top - 8 - maxH) + 'px';
    popover.style.bottom = '';
    popover.style.maxHeight = maxH + 'px';
  } else {
    maxH = Math.min(naturalHeight, spaceBelow);
    popover.style.top = (rect.bottom + 8) + 'px';
    popover.style.bottom = '';
    popover.style.maxHeight = maxH + 'px';
  }

  // Horizontal: align left edge with the button, clamp to viewport
  let left = rect.left;
  if (left + width > vw - margin) left = vw - margin - width;
  if (left < margin) left = margin;
  popover.style.left = left + 'px';
  popover.style.right = '';
}

function showParallelPopover() {
  const popover = document.getElementById('parallel-popover');
  if (!popover) return;
  // Backdrop — created on demand so closing one click outside is easy
  let bd = document.getElementById('parallel-popover-backdrop');
  if (!bd) {
    bd = document.createElement('div');
    bd.id = 'parallel-popover-backdrop';
    bd.className = 'parallel-popover-backdrop';
    bd.addEventListener('click', hideParallelPopover);
    document.body.appendChild(bd);
  }
  bd.style.display = 'block';
  popover.style.display = 'flex';
  refreshParallelJudge();
  refreshParallelRows();
  // Two rAFs: first lets the layout settle so scrollHeight is right.
  requestAnimationFrame(() => requestAnimationFrame(positionParallelPopover));
}

function hideParallelPopover() {
  const popover = document.getElementById('parallel-popover');
  const bd = document.getElementById('parallel-popover-backdrop');
  if (popover) popover.style.display = 'none';
  if (bd) bd.remove();
}

function isPopoverVisible() {
  const popover = document.getElementById('parallel-popover');
  return !!popover && popover.style.display !== 'none';
}

function initParallelPopover() {
  const btn = document.getElementById('chat-parallel-btn');
  const popover = document.getElementById('parallel-popover');
  const close = document.getElementById('parallel-popover-close');
  const enable = document.getElementById('parallel-enable');
  const nInp = document.getElementById('parallel-n');
  const strat = document.getElementById('parallel-strategy');
  const judge = document.getElementById('parallel-judge');
  if (!btn || !popover) return;

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (isPopoverVisible()) hideParallelPopover();
    else showParallelPopover();
  });
  close?.addEventListener('click', hideParallelPopover);

  enable?.addEventListener('change', () => {
    window.parallelConfig.enabled = enable.checked;
    refreshParallelBadge();
  });

  nInp?.addEventListener('change', () => {
    let v = parseInt(nInp.value);
    if (isNaN(v) || v < 2) v = 2;
    if (v > 8) v = 8;
    nInp.value = v;
    window.parallelConfig.n = v;
    window.parallelConfig.models = window.parallelConfig.models.slice(0, v);
    window.parallelConfig.profiles = window.parallelConfig.profiles.slice(0, v);
    refreshParallelRows();
    refreshParallelBadge();
    if (isPopoverVisible()) requestAnimationFrame(positionParallelPopover);
  });

  strat?.addEventListener('change', () => {
    window.parallelConfig.strategy = strat.value;
  });

  judge?.addEventListener('change', () => {
    window.parallelConfig.judge = judge.value;
  });

  // Reposition on viewport changes
  window.addEventListener('resize', () => {
    if (isPopoverVisible()) positionParallelPopover();
  });
  window.addEventListener('scroll', () => {
    if (isPopoverVisible()) positionParallelPopover();
  }, true);

  // Close on Escape
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isPopoverVisible()) hideParallelPopover();
  });

  // Refresh content when the model dropdown changes (keeps per-candidate
  // dropdowns in sync with whatever the user can actually pick).
  document.getElementById('chat-model-select')?.addEventListener('change', () => {
    if (isPopoverVisible()) {
      refreshParallelRows();
      refreshParallelJudge();
    }
  });
}

// ──────────────────────────────────────────────────────────────
// Council-models popover (Improved / Ultra modes)
// ──────────────────────────────────────────────────────────────

const COUNCIL_ROLES_UI = [
  ['worker',     'council-worker'],
  ['thinker',    'council-thinker'],
  ['verifier',   'council-verifier'],
  ['worker_alt', 'council-worker-alt'],
];
const COUNCIL_LS_KEY = 'octoslave-council-models';

// Per-role overrides; '' = Auto (backend resolves from its preference chains).
window.councilConfig = (() => {
  try {
    const saved = JSON.parse(localStorage.getItem(COUNCIL_LS_KEY) || '{}');
    return {
      worker: saved.worker || '', thinker: saved.thinker || '',
      verifier: saved.verifier || '', worker_alt: saved.worker_alt || '',
    };
  } catch { return { worker: '', thinker: '', verifier: '', worker_alt: '' }; }
})();

function saveCouncilConfig() {
  try { localStorage.setItem(COUNCIL_LS_KEY, JSON.stringify(window.councilConfig)); } catch {}
}

// Blue dot on the gear when any role is pinned (so a stale pick is visible).
function refreshCouncilDot() {
  const dot = document.getElementById('council-config-dot');
  if (!dot) return;
  const any = Object.values(window.councilConfig).some(Boolean);
  dot.style.display = any ? 'block' : 'none';
}

function refreshCouncilSelects() {
  const models = Array.from(document.getElementById('chat-model-select')?.options || [])
    .map(o => o.value).filter(Boolean);
  COUNCIL_ROLES_UI.forEach(([role, id]) => {
    const sel = document.getElementById(id);
    if (!sel) return;
    const cur = window.councilConfig[role] || '';
    let html = '<option value="">Auto</option>';
    // Keep a pinned model visible even if it's not in the current catalog.
    const pool = cur && !models.includes(cur) ? [cur, ...models] : models;
    pool.forEach(m => {
      html += `<option value="${esc(m)}"${m === cur ? ' selected' : ''}>${esc(m)}</option>`;
    });
    sel.innerHTML = html;
    sel.value = cur;
  });
}

function positionCouncilPopover() {
  const btn = document.getElementById('council-config-btn');
  const popover = document.getElementById('council-popover');
  if (!btn || !popover) return;
  const rect = btn.getBoundingClientRect();
  const margin = 16;
  const vw = window.innerWidth, vh = window.innerHeight;
  const width = Math.min(popover.offsetWidth || 420, vw - margin * 2);
  popover.style.width = width + 'px';
  // The gear lives in the top config bar, so open downward.
  const maxH = Math.min(popover.scrollHeight || 400, vh - rect.bottom - 8 - margin);
  popover.style.top = (rect.bottom + 8) + 'px';
  popover.style.bottom = '';
  popover.style.maxHeight = maxH + 'px';
  let left = rect.right - width;   // right-align to the gear
  if (left + width > vw - margin) left = vw - margin - width;
  if (left < margin) left = margin;
  popover.style.left = left + 'px';
  popover.style.right = '';
}

function showCouncilPopover() {
  const popover = document.getElementById('council-popover');
  if (!popover) return;
  let bd = document.getElementById('council-popover-backdrop');
  if (!bd) {
    bd = document.createElement('div');
    bd.id = 'council-popover-backdrop';
    bd.className = 'parallel-popover-backdrop';
    bd.addEventListener('click', hideCouncilPopover);
    document.body.appendChild(bd);
  }
  bd.style.display = 'block';
  popover.style.display = 'flex';
  refreshCouncilSelects();
  requestAnimationFrame(() => requestAnimationFrame(positionCouncilPopover));
}

function hideCouncilPopover() {
  const popover = document.getElementById('council-popover');
  const bd = document.getElementById('council-popover-backdrop');
  if (popover) popover.style.display = 'none';
  if (bd) bd.remove();
}
window.hideCouncilPopover = hideCouncilPopover;

function isCouncilPopoverVisible() {
  const popover = document.getElementById('council-popover');
  return !!popover && popover.style.display !== 'none';
}

function initCouncilPopover() {
  const btn = document.getElementById('council-config-btn');
  const popover = document.getElementById('council-popover');
  if (!btn || !popover) return;

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (isCouncilPopoverVisible()) hideCouncilPopover();
    else showCouncilPopover();
  });
  document.getElementById('council-popover-close')?.addEventListener('click', hideCouncilPopover);

  COUNCIL_ROLES_UI.forEach(([role, id]) => {
    document.getElementById(id)?.addEventListener('change', (e) => {
      window.councilConfig[role] = e.target.value || '';
      saveCouncilConfig();
      refreshCouncilDot();
    });
  });

  document.getElementById('council-reset-btn')?.addEventListener('click', () => {
    window.councilConfig = { worker: '', thinker: '', verifier: '', worker_alt: '' };
    saveCouncilConfig();
    refreshCouncilSelects();
    refreshCouncilDot();
  });

  window.addEventListener('resize', () => {
    if (isCouncilPopoverVisible()) positionCouncilPopover();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && isCouncilPopoverVisible()) hideCouncilPopover();
  });

  refreshCouncilDot();
}

// ──────────────────────────────────────────────────────────────
// @-file picker
// ──────────────────────────────────────────────────────────────

let pickerEl = null;
let pickerItems = [];
let pickerSelected = 0;
let pickerToken = null;       // {start, end} of the @… token in textarea

function ensurePickerEl() {
  if (pickerEl) return pickerEl;
  pickerEl = document.createElement('div');
  pickerEl.className = 'file-picker';
  pickerEl.style.display = 'none';
  document.body.appendChild(pickerEl);
  return pickerEl;
}

function filePickerVisible() {
  return pickerEl && pickerEl.style.display !== 'none';
}

function hideFilePicker() {
  if (pickerEl) pickerEl.style.display = 'none';
  pickerToken = null;
  pickerItems = [];
}

function handlePickerKey(e) {
  if (!filePickerVisible()) return false;
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    pickerSelected = Math.min(pickerItems.length - 1, pickerSelected + 1);
    renderPicker();
    return true;
  }
  if (e.key === 'ArrowUp') {
    e.preventDefault();
    pickerSelected = Math.max(0, pickerSelected - 1);
    renderPicker();
    return true;
  }
  if (e.key === 'Enter' || e.key === 'Tab') {
    if (pickerItems.length) {
      e.preventDefault();
      acceptPicker(pickerItems[pickerSelected]);
      return true;
    }
  }
  if (e.key === 'Escape') {
    e.preventDefault();
    hideFilePicker();
    return true;
  }
  return false;
}

function renderPicker() {
  if (!pickerEl) return;
  if (!pickerItems.length) {
    pickerEl.innerHTML = '<div class="file-picker-empty">no matches</div>';
    return;
  }
  pickerEl.innerHTML = pickerItems.map((p, i) => {
    const cls = i === pickerSelected ? 'file-picker-item active' : 'file-picker-item';
    return `<div class="${cls}" data-idx="${i}">${esc(p)}</div>`;
  }).join('');
  Array.from(pickerEl.querySelectorAll('.file-picker-item')).forEach(node => {
    node.addEventListener('mousedown', (ev) => {
      ev.preventDefault();
      acceptPicker(pickerItems[parseInt(node.dataset.idx)]);
    });
  });
}

function acceptPicker(path) {
  const ta = document.getElementById('chat-textarea');
  if (!ta || !pickerToken) {
    hideFilePicker();
    return;
  }
  const value = ta.value;
  const before = value.slice(0, pickerToken.start);
  const after = value.slice(pickerToken.end);
  const insert = '@' + path + ' ';
  ta.value = before + insert + after;
  const caret = before.length + insert.length;
  ta.setSelectionRange(caret, caret);
  hideFilePicker();
  ta.focus();
}

function maybeShowFilePicker(ta) {
  const value = ta.value;
  const caret = ta.selectionStart || 0;
  // Walk back from caret to last @ or whitespace
  let i = caret - 1;
  while (i >= 0 && !/\s/.test(value[i]) && value[i] !== '@') i--;
  if (i < 0 || value[i] !== '@') {
    hideFilePicker();
    return;
  }
  // Make sure @ is preceded by whitespace or start-of-input (so emails don't trigger)
  if (i > 0 && !/\s/.test(value[i - 1])) {
    hideFilePicker();
    return;
  }
  pickerToken = { start: i, end: caret };
  const query = value.slice(i + 1, caret);
  const wd = window.getWorkingDir();
  fetch(`/api/picker?working_dir=${encodeURIComponent(wd)}&q=${encodeURIComponent(query)}`)
    .then(r => r.json())
    .then(data => {
      pickerItems = data.items || [];
      pickerSelected = 0;
      const pe = ensurePickerEl();
      const rect = ta.getBoundingClientRect();
      pe.style.left = rect.left + 'px';
      pe.style.bottom = (window.innerHeight - rect.top + 4) + 'px';
      pe.style.minWidth = Math.min(420, rect.width) + 'px';
      pe.style.display = 'block';
      renderPicker();
    })
    .catch(() => hideFilePicker());
}

// Wait for DOM to be ready before initializing
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}
