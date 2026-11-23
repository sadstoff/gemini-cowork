export function esc(v: unknown): string {
  return String(v ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function inlineMd(text: string): string {
  return esc(text)
    .replace(/`([^`]+)`/g, '<code class="md-code">$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
}

export function renderRichPreview(name: string, text: string): string {
  const lower = name.toLowerCase();
  if (lower.endsWith('.csv')) {
    const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0).slice(0, 80);
    if (!lines.length) return '<div class="muted">Empty CSV</div>';
    const sep = lines[0].includes(';') && !lines[0].includes(',') ? ';' : ',';
    const rows = lines.map((l) => l.split(sep).map((c) => c.replace(/^"|"$/g, '')));
    return `<div class="table-wrap"><table class="data-table">
      <thead><tr>${rows[0].map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>
      <tbody>${rows.slice(1).map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody>
    </table></div>`;
  }

  if (lower.endsWith('.md')) {
    const out: string[] = [];
    let inList = false;
    for (const raw of text.split(/\r?\n/)) {
      const ln = raw.trim();
      if (!ln) {
        if (inList) { out.push('</ul>'); inList = false; }
        continue;
      }
      if (ln.startsWith('### ')) {
        if (inList) { out.push('</ul>'); inList = false; }
        out.push(`<h4 class="md-h3">${inlineMd(ln.slice(4))}</h4>`);
      } else if (ln.startsWith('## ')) {
        if (inList) { out.push('</ul>'); inList = false; }
        out.push(`<h3 class="md-h2">${inlineMd(ln.slice(3))}</h3>`);
      } else if (ln.startsWith('# ')) {
        if (inList) { out.push('</ul>'); inList = false; }
        out.push(`<h2 class="md-h1">${inlineMd(ln.slice(2))}</h2>`);
      } else if (ln.startsWith('- ') || ln.startsWith('* ')) {
        if (!inList) { out.push('<ul class="md-ul">'); inList = true; }
        out.push(`<li>${inlineMd(ln.slice(2))}</li>`);
      } else {
        if (inList) { out.push('</ul>'); inList = false; }
        out.push(`<p class="md-p">${inlineMd(ln)}</p>`);
      }
    }
    if (inList) out.push('</ul>');
    return `<div class="md-view">${out.join('')}</div>`;
  }

  return `<pre class="code-box">${esc(text)}</pre>`;
}
