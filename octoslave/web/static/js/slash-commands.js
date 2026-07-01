/**
 * OctoSlave Web UI - Slash Command Handling
 */

import { sendMsg } from './websocket.js?v=20260630c';
import { scrollToBottom } from './utils.js?v=20260429';

/**
 * Handle slash commands - returns true if command was handled
 */
export function handleSlashCommand(text) {
  if (!text.startsWith('/')) return false;
  
  const parts = text.split(/\s+/);
  const cmd = parts[0].toLowerCase();
  const arg = parts.slice(1).join(' ');
  
  switch (cmd) {
    case '/help':
    case '/?':
      appendChatInfo('📚 Available commands:\n' +
        '  /help                    Show this help\n' +
        '  /clear                   Clear chat and reset conversation\n' +
        '  /model [name]            List or switch model\n' +
        '  /dir [path]              Show or change working directory\n' +
        '  /profile [name]          Show or set prompt profile (base/coder/analyst)\n' +
        '  /permission [mode]       Show or set permission mode\n' +
        '  /compact                 Summarize conversation history to save tokens\n' +
        '  /share                   Create a shareable read-only link to this chat\n' +
        '  /parallel <N> <task>     Run N agents on the same task; judge picks best\n' +
        '  /local [model]           Switch to Ollama (local mode)\n' +
        '  /einfra                  Switch to e-INFRA CZ backend\n' +
        '  /nim [model]             Switch to NVIDIA NIM backend\n' +
        '  /pull <model>            Pull a model from Ollama\n' +
        '  /exit, /quit             Close browser tab\n' +
        '\n[dim]Tip: type [bold]@[/bold] in the composer to attach a file from the working directory.[/dim]');
      return true;

    case '/share':
      {
        const messages = window.appState?.messages || [];
        if (messages.length < 2) {
          appendChatInfo('ℹ️ No conversation to share yet.');
          return true;
        }
        appendChatInfo('🔗 Creating shareable link…');
        const model = document.getElementById('chat-model-select')?.value || '';
        fetch('/api/share', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            messages,
            model,
            title: messages.find(m => m.role === 'user')?.content?.slice(0, 80) || '',
          }),
        })
          .then(r => r.json())
          .then(data => {
            if (data.error) {
              appendChatError('Could not create share: ' + data.error);
              return;
            }
            const url = window.location.origin + data.url;
            navigator.clipboard?.writeText(url).catch(() => {});
            appendChatInfo(
              `✅ Shared: [bold]${url}[/bold]\n[dim]Copied to clipboard. ` +
              `Anyone with this link can read the conversation.[/dim]`
            );
          })
          .catch(err => appendChatError('Share failed: ' + err));
      }
      return true;

    case '/parallel':
      {
        const tokens = arg.split(/\s+/).filter(Boolean);
        let n = 3;
        let strategy = 'best';
        let models = null;
        let profiles = null;
        let judge = null;
        const taskTokens = [];
        for (let i = 0; i < tokens.length; i++) {
          const t = tokens[i];
          const low = t.toLowerCase();
          if (low.startsWith('models=')) {
            models = t.slice(7).split(',').map(s => s.trim()).filter(Boolean);
          } else if (low.startsWith('profiles=')) {
            profiles = t.slice(9).split(',').map(s => s.trim()).filter(Boolean);
          } else if (low.startsWith('judge=')) {
            judge = t.slice(6).trim() || null;
          } else if (/^\d+$/.test(t) && taskTokens.length === 0) {
            n = Math.max(1, Math.min(8, parseInt(t)));
          } else if (['best', 'vote', 'merge'].includes(low) && taskTokens.length === 0) {
            strategy = low;
          } else {
            taskTokens.push(t);
          }
        }
        const task = taskTokens.join(' ').trim();
        if (!task) {
          appendChatError(
            'Usage: /parallel [N] [best|vote|merge] [models=A,B,C] ' +
            '[profiles=coder,analyst,base] [judge=MODEL] <task>'
          );
          return true;
        }
        const summary = models
          ? `models=[${models.join(', ')}]`
          : `model=${document.getElementById('chat-model-select')?.value || '(default)'}`;
        appendChatInfo(`🐙×${n} Spawning ${n} agents · strategy=[bold]${strategy}[/bold] · ${summary}`);
        sendMsg({
          type: 'chat_parallel',
          message: task,
          n,
          strategy,
          models,
          profiles,
          judge_model: judge,
          model: document.getElementById('chat-model-select')?.value || '',
          working_dir: window.getWorkingDir(),
          permission_mode: document.getElementById('chat-permission-select')?.value || 'autonomous',
        });
        if (window.setChatRunningExternal) window.setChatRunningExternal(true);
      }
      return true;
      
    case '/clear':
      sendMsg({ type: 'chat_clear' });
      if (window.clearChatMessages) window.clearChatMessages();
      appendChatInfo('🗑️ Chat cleared.');
      return true;
      
    case '/model':
      if (!arg) {
        sendMsg({ type: 'list_models' });
        appendChatInfo('📡 Use UI dropdown to select a model, or wait for the list to load.');
      } else {
        const modelSel = document.getElementById('chat-model-select');
        if (modelSel && modelSel.querySelector(`option[value="${arg}"]`)) {
          modelSel.value = arg;
          appendChatInfo(`✅ Model switched to [bold]${arg}[/bold].`);
        } else {
          appendChatError(`❌ Model '${arg}' not found in available models.`);
        }
      }
      return true;
      
    case '/dir':
      if (!arg) {
        appendChatInfo(`📂 Current working directory: [bold]${window.getWorkingDir()}[/bold]`);
      } else {
        sendMsg({ type: 'set_working_dir', working_dir: arg });
        window.setWorkingDir(arg);
        appendChatInfo(`📂 Working directory set to: [bold]${arg}[/bold]`);
      }
      return true;
      
    case '/profile':
      {
        const profileSelect = document.getElementById('chat-profile-select');
        const availableProfiles = profileSelect
          ? Array.from(profileSelect.options).map(o => o.value)
          : ['base', 'coder', 'analyst', 'local'];
        const profileDisplay = p => p.charAt(0).toUpperCase() + p.slice(1);

        if (!arg) {
          const currentProfile = profileSelect?.value || 'base';
          appendChatInfo(`📝 Current prompt profile: [bold]${profileDisplay(currentProfile)}[/bold]\n` +
            `Available: ${availableProfiles.map(profileDisplay).join(', ')}\n` +
            'Usage: /profile <name>  e.g., /profile coder');
        } else {
          const profileArg = arg.toLowerCase();
          if (availableProfiles.includes(profileArg)) {
            if (profileSelect) profileSelect.value = profileArg;
            appendChatInfo(`✅ Prompt profile set to [bold]${profileDisplay(profileArg)}[/bold].\n` +
              '[dim]Note: Profile will be used for the next task (new conversation).[/dim]');
          } else {
            appendChatError(`❌ Invalid profile '${arg}'. Use: ${availableProfiles.map(profileDisplay).join(', ')}.`);
          }
        }
      }
      return true;
      
    case '/permission':
      if (!arg) {
        const currentMode = document.getElementById('chat-permission-select')?.value;
        const modeNames = { autonomous: 'Autonomous', controlled: 'Controlled', supervised: 'Supervised' };
        appendChatInfo(`🛡️ Current permission mode: [bold]${modeNames[currentMode]}[/bold]\n` +
          'Available: autonomous, controlled, supervised\n' +
          'Usage: /permission <mode>  e.g., /permission controlled\n' +
          '  autonomous — work without asking (default)\n' +
          '  controlled — ask before file edits or commands\n' +
          '  supervised — ask before file edits, auto-allow commands');
      } else {
        const modeArg = arg.toLowerCase();
        if (['autonomous', 'controlled', 'supervised'].includes(modeArg)) {
          const permSelect = document.getElementById('chat-permission-select');
          if (permSelect) permSelect.value = modeArg;
          const modeNames = { autonomous: 'Autonomous', controlled: 'Controlled', supervised: 'Supervised' };
          appendChatInfo(`✅ Permission mode set to [bold]${modeNames[modeArg]}[/bold].\n` +
            '[dim]Note: Mode will apply to the next tool execution.[/dim]');
        } else {
          appendChatError(`❌ Invalid mode '${arg}'. Use: autonomous, controlled, or supervised.`);
        }
      }
      return true;
      
    case '/compact':
      const messages = window.appState?.messages || [];
      if (messages.length <= 1) {
        appendChatInfo('ℹ️ No conversation to compact.');
        return true;
      }
      appendChatInfo('📦 Compacting conversation history...');
      sendMsg({ 
        type: 'chat_continue', 
        message: '/compact',
        model: document.getElementById('chat-model-select')?.value || '',
        working_dir: window.getWorkingDir(),
        prompt_profile: document.getElementById('chat-profile-select')?.value || 'base',
        permission_mode: document.getElementById('chat-permission-select')?.value || 'autonomous'
      });
      return true;
      
    case '/local':
      appendChatInfo('🔄 Switching to local Ollama mode...\n' +
        '[dim]Use the UI or run `/pull <model>` first if you haven\'t pulled any models.[/dim]');
      sendMsg({ type: 'switch_backend', backend: 'ollama', model: arg || undefined });
      { const s = document.getElementById('backend-select'); if (s) { s.value = 'ollama'; s.dataset.backend = 'ollama'; } }
      return true;

    case '/einfra':
      appendChatInfo('🔄 Switching to e-INFRA CZ backend...');
      sendMsg({ type: 'switch_backend', backend: 'einfra' });
      { const s = document.getElementById('backend-select'); if (s) { s.value = 'einfra'; s.dataset.backend = 'einfra'; } }
      return true;

    case '/nim':
      appendChatInfo('🔄 Switching to NVIDIA NIM backend...');
      sendMsg({ type: 'switch_backend', backend: 'nim', model: arg || undefined });
      { const s = document.getElementById('backend-select'); if (s) { s.value = 'nim'; s.dataset.backend = 'nim'; } }
      setTimeout(() => sendMsg({ type: 'list_models' }), 600);
      return true;
      
    case '/pull':
      if (!arg) {
        appendChatError('❌ Usage: /pull <model-name>  e.g., /pull llama3.2');
        return true;
      }
      appendChatInfo(`📥 Pulling model [bold]${arg}[/bold] from Ollama...`);
      sendMsg({ type: 'pull_model', model: arg });
      return true;
      
    case '/exit':
    case '/quit':
    case '/q':
      appendChatInfo('👋 Goodbye! You can close this tab now.');
      return true;
      
    case '/lab':
    case '/research':
    case '/long-research':
      appendChatInfo('🔬 Opening the [bold]Lab[/bold] — autonomous research team…');
      window.location.href = '/lab';
      return true;
      
    default:
      return false;  // Not a recognized command, let it pass as normal message
  }
}

/**
 * Append info message to chat
 */
function appendChatInfo(text) {
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
function appendChatError(text) {
  const container = document.getElementById('chat-messages');
  if (!container) return;
  
  const div = document.createElement('div');
  div.className = 'msg msg-error';
  div.innerHTML = `<div class="msg-bubble">⚠ ${text}</div>`;
  container.appendChild(div);
  scrollToBottom(container);
}

