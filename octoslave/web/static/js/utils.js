/**
 * OctoSlave Web UI - Utility Functions
 */


// Escape HTML to prevent XSS
export function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// Markdown → HTML for agent messages.
//
// Safe by construction: all source text is HTML-escaped before any markup is
// added. Code (fenced and inline) is set aside FIRST, so emphasis rules can never
// touch it — `**kwargs`, `*.py` and snake_case identifiers stay intact — and a
// fence that is still open mid-stream renders as code rather than garbage.
export function renderMarkdown(text) {
  const stash = [];
  const keep = (html) => `\u0000${stash.push(html) - 1}\u0000`;
  const restore = (s) => s.replace(/\u0000(\d+)\u0000/g, (_, i) => stash[+i]);

  let src = String(text ?? '').replace(/\r\n?/g, '\n');
  src = src.replace(/(^|\n)[ \t]*(```|~~~)([\w+#.-]*)[^\n]*\n([\s\S]*?)(?:\n[ \t]*\2[ \t]*(?=\n|$)|$)/g,
    (_, lead, _f, lang, code) =>
      `${lead}${keep(`<pre><code${lang ? ` class="lang-${esc(lang)}"` : ''}>${esc(code)}</code></pre>`)}`);

  const inline = (t) => {
    let s = t.replace(/`([^`\n]+)`/g, (_, c) => keep(`<code>${esc(c)}</code>`));
    s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, (m, label, url) =>
      /^(https?:|mailto:|\/|#)/i.test(url)
        ? keep(`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`)
        : m);
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g, (_, pre, url) =>
      pre + keep(`<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(url)}</a>`));
    s = esc(s);
    s = s.replace(/\*\*(?=\S)([\s\S]*?\S)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[^\w])__(?=\S)([\s\S]*?\S)__(?!\w)/g, '$1<strong>$2</strong>');
    s = s.replace(/(^|[^*\w])\*(?=[^\s*])([^*\n]*?[^\s*])\*(?![*\w])/g, '$1<em>$2</em>');
    s = s.replace(/(^|[^\w])_(?=[^\s_])([^_\n]*?[^\s_])_(?!\w)/g, '$1<em>$2</em>');
    s = s.replace(/~~(?=\S)([^~\n]*?\S)~~/g, '<del>$1</del>');
    return s;
  };

  const lines = src.split('\n');
  const out = [];
  const LIST = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;
  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const cells = (l) => l.trim().replace(/^\||\|$/g, '').split('|').map(c => c.trim());
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^\u0000\d+\u0000$/.test(line.trim())) { out.push(line.trim()); i++; continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/))) {
      const lvl = Math.min(6, m[1].length + 1);   // an h1 in a chat bubble is too loud
      out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`); i++; continue;
    }
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
    if (/^\s*>/.test(line)) {
      const q = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*>\s?/, ''));
      out.push(`<blockquote>${renderMarkdown(restore(q.join('\n')))}</blockquote>`);
      continue;
    }
    if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(cells(lines[i++]));
      out.push('<div class="md-table-wrap"><table><thead><tr>'
        + head.map(h => `<th>${inline(h)}</th>`).join('') + '</tr></thead><tbody>'
        + rows.map(r => '<tr>' + head.map((_, j) => `<td>${inline(r[j] || '')}</td>`).join('') + '</tr>').join('')
        + '</tbody></table></div>');
      continue;
    }
    if (LIST.test(line)) {
      // Nested lists from indentation; ordered vs bullet decided per level.
      const stack = [];
      const html = [];
      while (i < lines.length) {
        const lm = lines[i].match(LIST);
        if (!lm) {
          if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && html.length) {
            html[html.length - 1] += ' ' + inline(lines[i].trim());   // wrapped item text
            i++; continue;
          }
          break;
        }
        const depth = Math.floor(lm[1].replace(/\t/g, '  ').length / 2);
        const tag = /\d/.test(lm[2]) ? 'ol' : 'ul';
        while (stack.length > depth + 1) html.push(`</li></${stack.pop()}>`);
        if (stack.length === depth + 1) {
          html.push('</li>');
          // Same level, other kind (bullets right after a numbered list): new list.
          if (stack[depth] !== tag) { html.push(`</${stack.pop()}>`); }
        }
        while (stack.length < depth + 1) { stack.push(tag); html.push(`<${tag}>`); }
        let body = lm[3];
        const task = body.match(/^\[([ xX])\]\s+(.*)$/);
        if (task) body = `<input type="checkbox" disabled${task[1] !== ' ' ? ' checked' : ''}> ` + inline(task[2]);
        else body = inline(body);
        html.push(`<li>${body}`);
        i++;
      }
      while (stack.length) html.push(`</li></${stack.pop()}>`);
      out.push(html.join(''));
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !LIST.test(lines[i])
           && !/^(#{1,6}\s|\s*>)/.test(lines[i]) && !/^\u0000\d+\u0000$/.test(lines[i].trim())
           && !(lines[i].includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1]))) {
      para.push(inline(lines[i++]));
    }
    if (para.length) out.push(`<p>${para.join('<br>')}</p>`);
    else out.push(`<p>${inline(lines[i++])}</p>`);
  }
  return restore(out.join('\n'));
}

// Scroll element to bottom
export function scrollToBottom(element) {
  element.scrollTop = element.scrollHeight;
}

// Auto-resize textarea
export function autoResizeTextarea(textarea) {
  textarea.style.height = 'auto';
  textarea.style.height = Math.min(textarea.scrollHeight, 160) + 'px';
}

// Format file size
export function formatFileSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// Format timestamp
export function formatTimestamp(isoString) {
  const date = new Date(isoString);
  return date.toLocaleDateString() + ' ' + date.toLocaleTimeString();
}

// Debounce function
export function debounce(func, wait) {
  let timeout;
  return function executedFunction(...args) {
    const later = () => {
      clearTimeout(timeout);
      func(...args);
    };
    clearTimeout(timeout);
    timeout = setTimeout(later, wait);
  };
}

// Throttle function
export function throttle(func, limit) {
  let inThrottle;
  return function(...args) {
    if (!inThrottle) {
      func.apply(this, args);
      inThrottle = true;
      setTimeout(() => inThrottle = false, limit);
    }
  };
}
