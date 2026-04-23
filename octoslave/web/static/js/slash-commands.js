/**
 * OctoSlave Web UI - Slash Command Handling
 */

import { sendMsg } from './websocket.js';

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
        '  /help              Show this help\n' +
        '  /clear             Clear chat and reset conversation\n' +
        '  /model [name]      List or switch model\n' +
        '  /dir [path]        Show or change working directory\n' +
        '  /profile [name]    Show or set prompt profile (base/simple/strict)\n' +
        '  /permission [mode] Show or set permission mode (autonomous/controlled/supervised)\n' +
        '  /compact           Summarize conversation history to save tokens\n' +
        '  /local [model]     Switch to Ollama (local mode)\n' +
        '  /einfra            Switch to e-INFRA CZ backend\n' +
        '  /pull <model>      Pull a model from Ollama\n' +
        '  /exit, /quit       Close browser tab');
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
        const currentDir = document.getElementById('chat-dir-input')?.value;
        appendChatInfo(`📂 Current working directory: [bold]${currentDir}[/bold]`);
      } else {
        sendMsg({ type: 'set_working_dir', working_dir: arg });
        const dirInput = document.getElementById('chat-dir-input');
        if (dirInput) dirInput.value = arg;
        appendChatInfo(`📂 Working directory set to: [bold]${arg}[/bold]`);
      }
      return true;
      
    case '/profile':
      if (!arg) {
        const currentProfile = document.getElementById('chat-profile-select')?.value;
        const profileNames = { base: 'Base', simple: 'Simple', strict: 'Strict' };
        appendChatInfo(`📝 Current prompt profile: [bold]${profileNames[currentProfile]}[/bold]\n` +
          'Available: base, simple, strict\n' +
          'Usage: /profile <name>  e.g., /profile simple');
      } else {
        const profileArg = arg.toLowerCase();
        if (['base', 'simple', 'strict'].includes(profileArg)) {
          const profileSelect = document.getElementById('chat-profile-select');
          if (profileSelect) profileSelect.value = profileArg;
          const profileNames = { base: 'Base', simple: 'Simple', strict: 'Strict' };
          appendChatInfo(`✅ Prompt profile set to [bold]${profileNames[profileArg]}[/bold].\n` +
            '[dim]Note: Profile will be used for the next task (new conversation).[/dim]');
        } else {
          appendChatError(`❌ Invalid profile '${arg}'. Use: base, simple, or strict.`);
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
        working_dir: document.getElementById('chat-dir-input')?.value || '.',
        prompt_profile: document.getElementById('chat-profile-select')?.value || 'base',
        permission_mode: document.getElementById('chat-permission-select')?.value || 'autonomous'
      });
      return true;
      
    case '/local':
      appendChatInfo('🔄 Switching to local Ollama mode...\n' +
        '[dim]Use the UI or run `/pull <model>` first if you haven\'t pulled any models.[/dim]');
      sendMsg({ type: 'switch_backend', backend: 'ollama', model: arg || undefined });
      return true;
      
    case '/einfra':
      appendChatInfo('🔄 Switching to e-INFRA CZ backend...');
      sendMsg({ type: 'switch_backend', backend: 'einfra' });
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
      
    case '/long-research':
      appendChatInfo('🔬 Use the [bold]Research[/bold] tab to start a long-research pipeline.');
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

/**
 * Scroll chat to bottom
 */
function scrollToBottom(element) {
  element.scrollTop = element.scrollHeight;
}
