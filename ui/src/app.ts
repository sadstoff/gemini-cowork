import {
  App,
  applyDocumentTheme,
  applyHostStyleVariables,
} from '@modelcontextprotocol/ext-apps';
import { ICONS } from './icons';
import { esc, renderRichPreview } from './preview';

type Tab = 'files' | 'artifacts' | 'rules';

interface MountEntry {
  path: string;
  permission_mode: 'read_only' | 'manual_approve' | 'auto';
}

interface DirItem {
  name: string;
  rel_path: string;
  full_path: string;
  is_dir: boolean;
  size_kb: number;
  mtime: string;
  previewable: boolean;
}

interface DirListing {
  root: string;
  rel_path: string;
  entries: DirItem[];
}

interface ArtifactItem {
  title: string;
  path: string;
  kind: string;
  summary: string;
}

interface RollbackBatch {
  batch_id: string;
  count: number;
  strategy: string;
}

interface ScanResult {
  total_files: number;
  total_mb: number;
  stale_count: number;
  duplicate_groups: Array<{ size_kb: number; files: string[] }>;
}

interface DryRunPreview {
  strategy: string;
  planned_moves: number;
  preview: Array<{ src: string; dst: string }>;
}

interface FilePreview {
  path: string;
  name: string;
  total_lines: number;
  text: string;
}

interface SearchMatch {
  rel_path: string;
  full_path: string;
  size_kb: number;
  mtime: string;
}

interface CoworkState {
  mounts: MountEntry[];
  active_path: string;
  instructions: string;
  artifacts: ArtifactItem[];
  quick_folders: Array<{ label: string; path: string }>;
  active_listing: DirListing | null;
  rollback_batches: RollbackBatch[];
  last_scan: ScanResult | null;
  dry_run_preview?: DryRunPreview | null;
}

const app = new App({ name: 'Cowork', version: '5.0.0' }, {});

let state: CoworkState = {
  mounts: [],
  active_path: '',
  instructions: '',
  artifacts: [],
  quick_folders: [],
  active_listing: null,
  rollback_batches: [],
  last_scan: null,
  dry_run_preview: null,
};

let tab: Tab = 'files';
let busy = false;
let liveWatch = false;
let liveTimer: number | null = null;
let toastMsg = '';
let toastErr = false;
let preview: FilePreview | null = null;
let searchHits: SearchMatch[] | null = null;
let queryText = '';
let strategy = 'by_category';
let rulesDraft = '';

const root = document.getElementById('app')!;

function notify(msg: string, isErr = false) {
  toastMsg = msg;
  toastErr = isErr;
  render();
  setTimeout(() => {
    if (toastMsg === msg) {
      toastMsg = '';
      render();
    }
  }, 4000);
}

function applyStructured(sc: Record<string, unknown> | undefined) {
  if (!sc) return;
  if (Array.isArray(sc.mounts)) {
    state = {
      ...state,
      ...(sc as unknown as CoworkState),
      dry_run_preview: (sc.dry_run_preview as DryRunPreview | undefined) ?? state.dry_run_preview ?? null,
    };
    if (typeof sc.instructions === 'string') rulesDraft = sc.instructions;
  }
  if (sc.listing) state.active_listing = sc.listing as DirListing;
  if (sc.preview) preview = sc.preview as FilePreview;
}

async function rpc(name: string, args: Record<string, unknown> = {}, silent = false) {
  if (!silent) {
    busy = true;
    render();
  }
  try {
    const res = await app.callServerTool({ name, arguments: args });
    if (res.structuredContent) applyStructured(res.structuredContent as Record<string, unknown>);
    const txt = res.content?.find((c) => c.type === 'text')?.text;
    if (res.isError && !silent) notify(txt || `Error in ${name}`, true);
    else if (name === 'cowork_organize_files' && args.dry_run === false) {
      state.dry_run_preview = null;
      notify('Organized! Rollback saved.');
    } else if (name === 'cowork_rollback') notify('Rollback completed.');
    else if (name === 'cowork_app_save_instructions') notify('Instructions saved.');
    return res;
  } catch (err) {
    if (!silent) notify(err instanceof Error ? err.message : String(err), true);
    return null;
  } finally {
    busy = false;
    render();
  }
}

function toggleLive() {
  liveWatch = !liveWatch;
  if (liveTimer !== null) {
    window.clearInterval(liveTimer);
    liveTimer = null;
  }
  if (liveWatch) {
    liveTimer = window.setInterval(() => {
      if (state.active_path && tab === 'files' && !busy) {
        rpc('cowork_list_dir', { directory_path: state.active_path, subpath: state.active_listing?.rel_path || '' }, true);
      }
    }, 2500);
  }
  render();
}

async function openIDE(fullPath: string) {
  const uri = 'file:///' + fullPath.replace(/\\/g, '/').replace(/^\/+/, '');
  try {
    await app.request(
      { method: 'com.google.antigravity/toggle-aux-pane', params: { tabType: 'fileView', uri } },
      { parse: (x: unknown) => x as { isError?: boolean } },
    );
  } catch {
    notify('Editor pane unavailable here.', true);
  }
}

function renderSidebar(): string {
  const mounts = state.mounts
    .map((m) => {
      const act = m.path.toLowerCase() === state.active_path.toLowerCase();
      const name = m.path.split('\\').pop() || m.path;
      return `<button class="sb-item ${tab === 'files' && act ? 'active' : ''}" data-action="focus" data-path="${esc(m.path)}" title="${esc(m.path)}">
        <span class="sb-item-l"><span class="ic-folder">${ICONS.folder}</span><span class="truncate">${esc(name)}</span></span>
      </button>`;
    })
    .join('');

  const quick = state.quick_folders
    .map(
      (q) => `<button class="sb-item" data-action="quick" data-path="${esc(q.path)}">
        <span class="sb-item-l"><span class="muted">${ICONS.plus}</span><span class="truncate">${esc(q.label)}</span></span>
      </button>`
    )
    .join('');

  return `
    <aside class="sidebar">
      <button class="sb-btn-mount" data-action="browse">${ICONS.plus} Mount Folder</button>
      <div class="sb-scroll">
        <button class="sb-item ${tab === 'files' ? 'active' : ''}" data-tab="files">
          <span class="sb-item-l"><span class="muted">${ICONS.folder}</span><span>Files</span></span>
        </button>
        <button class="sb-item ${tab === 'artifacts' ? 'active' : ''}" data-tab="artifacts">
          <span class="sb-item-l"><span class="muted">${ICONS.file}</span><span>Artifacts</span></span>
          <span class="small muted num">${state.artifacts.length}</span>
        </button>
        <button class="sb-item ${tab === 'rules' ? 'active' : ''}" data-tab="rules">
          <span class="sb-item-l"><span class="muted">${ICONS.sliders}</span><span>Instructions</span></span>
        </button>
        <div class="sb-head">Mounted</div>
        ${mounts || '<div class="small muted" style="padding:4px 10px;">None</div>'}
        <div class="sb-head">Quick Mount</div>
        ${quick}
      </div>
    </aside>
  `;
}

function renderDrawer(): string {
  const parts: string[] = [];
  if (preview) {
    parts.push(`<div class="row-between">
      <span class="mono small" style="font-weight:600;">${esc(preview.name)}</span>
      <div class="row">
        <button class="btn btn-sm" data-action="ide" data-path="${esc(preview.path)}">Open IDE</button>
        <button class="btn btn-sm" data-action="close-preview">${ICONS.close}</button>
      </div>
    </div>`);
    parts.push(renderRichPreview(preview.name, preview.text));
  }
  if (state.dry_run_preview) {
    const dry = state.dry_run_preview;
    parts.push(`<div class="card stack">
      <div class="row-between">
        <strong>${dry.planned_moves} files to move</strong>
        ${dry.planned_moves > 0 ? `<button class="btn btn-sm btn-primary" data-action="exec">Approve &amp; Run</button>` : ''}
      </div>
      <div class="stack" style="gap:3px;max-height:240px;overflow:auto;">
        ${dry.preview.slice(0, 20).map((m) => `<div class="small mono muted truncate">${esc(m.src)} → ${esc(m.dst)}</div>`).join('')}
      </div>
    </div>`);
  }
  return `<aside class="drawer">${parts.join('')}</aside>`;
}

function renderFilesBody(): string {
  if (!state.active_path) {
    return `<div class="content-scroll"><div class="card muted" style="text-align:center;padding:36px;">Click <strong>+ Mount Folder</strong> on the left to open a workspace.</div></div>`;
  }

  const kpi = state.last_scan
    ? `<div class="kpi-row">
        <div class="kpi-card"><div class="kpi-val num">${state.last_scan.total_files}</div><div class="kpi-lbl">Files</div></div>
        <div class="kpi-card"><div class="kpi-val num">${state.last_scan.total_mb} MB</div><div class="kpi-lbl">Total Size</div></div>
        <div class="kpi-card"><div class="kpi-val num">${state.last_scan.stale_count}</div><div class="kpi-lbl">Stale (&gt;90d)</div></div>
        <div class="kpi-card"><div class="kpi-val num">${state.last_scan.duplicate_groups.length}</div><div class="kpi-lbl">Duplicate Groups</div></div>
      </div>`
    : '';

  const undo = state.rollback_batches.length
    ? `<div class="card row-between">
        <span class="small">Last batch: <span class="mono">${esc(state.rollback_batches[0].batch_id)}</span> (${state.rollback_batches[0].count} files)</span>
        <button class="btn btn-sm btn-danger" data-action="undo" data-batch="${esc(state.rollback_batches[0].batch_id)}">${ICONS.undo} Undo</button>
      </div>`
    : '';

  let listHtml = '';
  if (searchHits !== null) {
    listHtml = `<div class="row-between"><span class="small muted">${searchHits.length} results</span><button class="btn btn-sm" data-action="clear-search">Back</button></div>
      <div class="file-list">${
        searchHits.length
          ? searchHits
              .map(
                (r) => `<button class="file-row ${preview?.path === r.full_path ? 'selected' : ''}" data-action="open" data-path="${esc(r.full_path)}" data-prev="1">
                  <span class="ic-file">${ICONS.file}</span>
                  <span class="truncate mono">${esc(r.rel_path)}</span>
                  <span class="muted small num">${r.size_kb} KB</span>
                  <span class="muted small num">${esc(r.mtime)}</span>
                </button>`
              )
              .join('')
          : `<div class="muted" style="padding:18px;text-align:center;">No matches found.</div>`
      }</div>`;
  } else if (state.active_listing) {
    const ls = state.active_listing;
    const subParts = ls.rel_path ? ls.rel_path.split('/').filter(Boolean) : [];
    let acc = '';
    const crumbs = [
      `<button class="crumb ${subParts.length === 0 ? 'current' : ''}" data-action="nav" data-sub="">${esc(ls.root.split('\\').pop() || ls.root)}</button>`,
      ...subParts.map((part, idx) => {
        acc = acc ? `${acc}/${part}` : part;
        return `<span class="muted">/</span><button class="crumb ${idx === subParts.length - 1 ? 'current' : ''}" data-action="nav" data-sub="${esc(acc)}">${esc(part)}</button>`;
      }),
    ].join('');

    const q = queryText.trim().toLowerCase();
    const entries = q ? ls.entries.filter((e) => e.name.toLowerCase().includes(q)) : ls.entries;

    listHtml = `<div class="breadcrumbs">${crumbs}</div>
      <div class="file-list">${
        entries.length
          ? entries
              .map(
                (it) => `<button class="file-row ${preview?.path === it.full_path ? 'selected' : ''}" data-action="${it.is_dir ? 'nav' : 'open'}" data-sub="${esc(it.rel_path)}" data-path="${esc(it.full_path)}" data-prev="${it.previewable ? '1' : '0'}">
                  <span class="${it.is_dir ? 'ic-folder' : 'ic-file'}">${it.is_dir ? ICONS.folder : ICONS.file}</span>
                  <span class="truncate">${esc(it.name)}</span>
                  <span class="muted small num">${it.is_dir ? 'Folder' : `${it.size_kb} KB`}</span>
                  <span class="muted small num">${esc(it.mtime)}</span>
                </button>`
              )
              .join('')
          : `<div class="muted" style="padding:18px;text-align:center;">Empty folder</div>`
      }</div>`;
  }

  const showDrawer = Boolean(preview || state.dry_run_preview);
  return `<div class="body-grid ${showDrawer ? 'with-drawer' : ''}">
    <div class="content-scroll">${kpi}${undo}${listHtml}</div>
    ${showDrawer ? renderDrawer() : ''}
  </div>`;
}

function renderArtifactsBody(): string {
  const cards = state.artifacts.length
    ? state.artifacts
        .map(
          (a) => `<div class="card row-between">
            <div style="min-width:0;flex:1;">
              <div style="font-weight:600;">${esc(a.title)}</div>
              <div class="small muted truncate">${esc(a.summary || a.path)}</div>
            </div>
            <div class="row">
              <button class="btn btn-sm" data-action="open" data-path="${esc(a.path)}" data-prev="1">Preview</button>
              <button class="btn btn-sm" data-action="ide" data-path="${esc(a.path)}">Open IDE</button>
            </div>
          </div>`
        )
        .join('')
    : `<div class="card muted" style="text-align:center;padding:28px;">No artifacts generated yet.</div>`;

  return `<div class="body-grid ${preview ? 'with-drawer' : ''}">
    <div class="content-scroll">${cards}</div>
    ${preview ? renderDrawer() : ''}
  </div>`;
}

function render() {
  const activeMount = state.mounts.find((m) => m.path.toLowerCase() === state.active_path.toLowerCase());
  const nextMode = activeMount?.permission_mode === 'read_only' ? 'manual_approve' : activeMount?.permission_mode === 'manual_approve' ? 'auto' : 'read_only';
  const modeTxt = activeMount?.permission_mode === 'read_only' ? 'Read Only' : activeMount?.permission_mode === 'auto' ? 'Auto' : 'Manual';

  let body = '';
  if (tab === 'artifacts') body = renderArtifactsBody();
  else if (tab === 'rules') {
    body = `<div class="content-scroll" style="max-width:760px;">
      <div class="row-between"><strong>Cowork Instructions</strong><button class="btn btn-primary btn-sm" data-action="save-rules">Save</button></div>
      <textarea id="rules-box" class="textarea" placeholder="Rules applied to Cowork operations...">${esc(rulesDraft)}</textarea>
    </div>`;
  } else body = renderFilesBody();

  root.innerHTML = `
    <div class="app-shell">
      ${renderSidebar()}
      <main class="workspace">
        <header class="header">
          <div class="search-box">
            <span class="muted">${ICONS.search}</span>
            <input id="q-input" type="text" placeholder="Filter folder or press Enter to search..." value="${esc(queryText)}" ${!state.active_path ? 'disabled' : ''} />
          </div>
          <div class="row">
            ${busy ? '<span class="small muted">Working…</span>' : ''}
            ${
              activeMount
                ? `<select id="strat-sel" class="select">
                    <option value="by_category" ${strategy === 'by_category' ? 'selected' : ''}>By Category</option>
                    <option value="by_date" ${strategy === 'by_date' ? 'selected' : ''}>By Date</option>
                    <option value="archive_stale" ${strategy === 'archive_stale' ? 'selected' : ''}>Archive Stale</option>
                    <option value="dedupe_review" ${strategy === 'dedupe_review' ? 'selected' : ''}>Dedupe</option>
                    <option value="normalize_names" ${strategy === 'normalize_names' ? 'selected' : ''}>Normalize Names</option>
                  </select>
                  <button class="btn btn-primary" data-action="organize">${ICONS.sparkles} Organize</button>
                  <button class="btn" data-action="scan">${ICONS.refresh} Scan</button>
                  <button class="btn ${liveWatch ? 'btn-live' : ''}" data-action="live">${liveWatch ? '● Live' : '○ Live'}</button>
                  <button class="btn" data-action="mode" data-path="${esc(activeMount.path)}" data-mode="${nextMode}">${modeTxt}</button>
                  <button class="btn btn-danger" data-action="unmount" data-path="${esc(activeMount.path)}">${ICONS.close}</button>`
                : ''
            }
          </div>
        </header>
        ${toastMsg ? `<div class="toast ${toastErr ? 'toast-err' : 'toast-ok'}">${esc(toastMsg)}</div>` : ''}
        ${body}
      </main>
    </div>
  `;
}

root.addEventListener('input', (e) => {
  const t = e.target as HTMLElement;
  if (t.id === 'q-input') {
    queryText = (t as HTMLInputElement).value;
    const pos = (t as HTMLInputElement).selectionStart ?? queryText.length;
    render();
    const inp = document.getElementById('q-input') as HTMLInputElement | null;
    if (inp) { inp.focus(); inp.setSelectionRange(pos, pos); }
  } else if (t.id === 'strat-sel') strategy = (t as HTMLSelectElement).value;
  else if (t.id === 'rules-box') rulesDraft = (t as HTMLTextAreaElement).value;
});

root.addEventListener('keydown', async (e) => {
  const t = e.target as HTMLElement;
  if (t.id === 'q-input' && e.key === 'Enter' && queryText.trim() && state.active_path) {
    const res = await rpc('cowork_search_files', { directory_path: state.active_path, query: queryText.trim(), recursive: true });
    searchHits = (res?.structuredContent as { matches?: SearchMatch[] } | undefined)?.matches ?? [];
    render();
  }
});

root.addEventListener('click', async (e) => {
  const el = (e.target as HTMLElement).closest<HTMLElement>('[data-tab], [data-action]');
  if (!el) return;

  if (el.dataset.tab) {
    tab = el.dataset.tab as Tab;
    render();
    return;
  }

  const act = el.dataset.action;
  if (!act) return;

  if (act === 'live') toggleLive();
  else if (act === 'browse') await rpc('cowork_app_pick_folder', { permission_mode: 'manual_approve' });
  else if (act === 'quick') await rpc('cowork_mount_folder', { directory_path: el.dataset.path, permission_mode: 'manual_approve' });
  else if (act === 'focus') {
    tab = 'files';
    preview = null;
    searchHits = null;
    await rpc('cowork_app_set_permission', { directory_path: el.dataset.path, set_active: true });
  } else if (act === 'mode') await rpc('cowork_app_set_permission', { directory_path: el.dataset.path, permission_mode: el.dataset.mode });
  else if (act === 'unmount') await rpc('cowork_unmount_folder', { directory_path: el.dataset.path });
  else if (act === 'nav') {
    preview = null;
    searchHits = null;
    await rpc('cowork_list_dir', { directory_path: state.active_path, subpath: el.dataset.sub || '' });
  } else if (act === 'open') {
    if (el.dataset.prev === '1') await rpc('cowork_read_file', { file_path: el.dataset.path, start_line: 1, end_line: 150 });
    else if (el.dataset.path) await openIDE(el.dataset.path);
  } else if (act === 'ide' && el.dataset.path) await openIDE(el.dataset.path);
  else if (act === 'close-preview') { preview = null; render(); }
  else if (act === 'clear-search') { searchHits = null; render(); }
  else if (act === 'scan') await rpc('cowork_scan_workspace', { directory_path: state.active_path, recursive: false });
  else if (act === 'organize') await rpc('cowork_organize_files', { directory_path: state.active_path, strategy, dry_run: true });
  else if (act === 'exec') await rpc('cowork_organize_files', { directory_path: state.active_path, strategy, dry_run: false, approved: true });
  else if (act === 'undo') await rpc('cowork_rollback', { batch_id: el.dataset.batch });
  else if (act === 'save-rules') await rpc('cowork_app_save_instructions', { instructions: rulesDraft });
});

app.ontoolinput = () => { busy = true; render(); };
app.ontoolresult = (r) => { busy = false; if (r.structuredContent) applyStructured(r.structuredContent as Record<string, unknown>); render(); };
app.onhostcontextchanged = (c) => {
  if (c.theme) applyDocumentTheme(c.theme);
  if (c.styles?.variables) applyHostStyleVariables(c.styles.variables);
};

async function init() {
  render();
  await app.connect();
  const c = app.getHostContext();
  if (c?.theme) applyDocumentTheme(c.theme);
  if (c?.styles?.variables) applyHostStyleVariables(c.styles.variables);
  if (!state.mounts.length && !state.quick_folders.length) await rpc('cowork_app_get_state');
  else render();
}

init();
