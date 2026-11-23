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
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>');
}

function renderMarkdown(text: string): string {
  const out: string[] = [];
  let inList = false;
  let inCode = false;
  let tableRows: string[][] = [];

  const flushList = () => {
    if (inList) { out.push('</ul>'); inList = false; }
  };
  const flushTable = () => {
    if (!tableRows.length) return;
    const head = tableRows[0];
    const body = tableRows.slice(1).filter((r) => !r.every((c) => /^[-:]+$/.test(c.trim())));
    out.push(`<div class="table-wrap"><table class="data-table"><thead><tr>${head.map((h) => `<th>${inlineMd(h)}</th>`).join('')}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${inlineMd(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
    tableRows = [];
  };

  for (const raw of text.split(/\r?\n/)) {
    const ln = raw.trim();
    if (ln.startsWith('```')) {
      flushList();
      flushTable();
      if (!inCode) { out.push('<pre class="code-box">'); inCode = true; }
      else { out.push('</pre>'); inCode = false; }
      continue;
    }
    if (inCode) {
      out.push(esc(raw) + '\n');
      continue;
    }
    if (ln.startsWith('|') && ln.endsWith('|')) {
      flushList();
      const cells = ln.slice(1, -1).split('|').map((c) => c.trim());
      tableRows.push(cells);
      continue;
    }
    flushTable();
    if (!ln) {
      flushList();
      continue;
    }
    if (ln.startsWith('### ')) {
      flushList();
      out.push(`<h4 class="md-h3">${inlineMd(ln.slice(4))}</h4>`);
    } else if (ln.startsWith('## ')) {
      flushList();
      out.push(`<h3 class="md-h2">${inlineMd(ln.slice(3))}</h3>`);
    } else if (ln.startsWith('# ')) {
      flushList();
      out.push(`<h2 class="md-h1">${inlineMd(ln.slice(2))}</h2>`);
    } else if (ln.startsWith('- ') || ln.startsWith('* ')) {
      if (!inList) { out.push('<ul class="md-ul">'); inList = true; }
      out.push(`<li>${inlineMd(ln.slice(2))}</li>`);
    } else {
      flushList();
      out.push(`<p class="md-p">${inlineMd(ln)}</p>`);
    }
  }
  flushList();
  flushTable();
  if (inCode) out.push('</pre>');
  return `<div class="md-view">${out.join('')}</div>`;
}

export function renderRichPreview(name: string, text: string, imageDataUrl?: string): string {
  const lower = name.toLowerCase();
  const imgHtml = imageDataUrl
    ? `<div style="margin-bottom:8px;"><img src="${esc(imageDataUrl)}" style="max-width:100%;max-height:260px;border-radius:8px;border:1px solid var(--bd);" /></div>`
    : '';

  if (lower.endsWith('.csv')) {
    const lines = text.split(/\r?\n/).filter((l) => l.trim().length > 0).slice(0, 80);
    if (!lines.length) return '<div class="muted">Empty CSV</div>';
    const sep = lines[0].includes(';') && !lines[0].includes(',') ? ';' : ',';
    const rows = lines.map((l) => l.split(sep).map((c) => c.replace(/^"|"$/g, '')));
    return `${imgHtml}<div class="table-wrap"><table class="data-table">
      <thead><tr>${rows[0].map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>
      <tbody>${rows.slice(1).map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join('')}</tr>`).join('')}</tbody>
    </table></div>`;
  }

  if (lower.endsWith('.md') || lower.endsWith('.xlsx') || lower.endsWith('.docx') || lower.endsWith('.pdf') || lower.endsWith('.pptx') || imageDataUrl) {
    return `${imgHtml}${renderMarkdown(text)}`;
  }

  return `<pre class="code-box">${esc(text)}</pre>`;
}
