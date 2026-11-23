# Cowork for Antigravity

Autonomous Claude Cowork-style workspace teammate for Google Antigravity, with an Antigravity-native MCP App UI (`ui://cowork/app.html`).

## Features

- **Antigravity-Native Workspace UI**: Opens from the left sidebar (`Plugins -> Cowork`), the side-pane `+` menu, or inline in chat.
- **Scoped Folder Access**: Mount folders via native folder picker or quick-mount chips with granular permission modes (`read_only`, `manual_approve`, `auto`). System roots and drive roots are blocked.
- **1-Click Workflows**:
  - **Folder Triage**: Scan files, preview category organization, and await approval.
  - **Research Brief**: Summarize text/markdown/code files into `_COWORK_BRIEF.md`.
  - **Duplicate Audit**: Exact SHA-256 duplicate detection (with fast head/mid/tail fingerprinting for large media files >16 MB) and safe isolation into `_Duplicates_Review`.
  - **Generate Index**: Export `_COWORK_INDEX.md` and `_COWORK_SUMMARY.csv`.
- **Safe Batch Organization & Clean Rollback**: Supports `by_category`, `by_date`, `archive_stale`, `dedupe_review`, `normalize_names`, and `custom` moves with dry-run previews and 1-click rollback (including empty-directory cleanup).
- **Search, File Explorer & Preview**: Browse subfolders, search by filename/glob or text content, preview text files, or open them in Antigravity's editor pane.
- **Persistent Instructions**: Save custom Cowork rules in `${PLUGIN_DATA}/instructions.md`.

## Requirements

- **Python 3.10+** on `PATH` (zero external Python dependencies; uses standard library only).
- **Node.js 18+** (only required if rebuilding `ui/cowork.html` from `ui/src/`).

## Installation

Copy or clone this directory into `~/.gemini/config/plugins/gemini-cowork`:

```bash
git clone https://github.com/sadstoff/gemini-cowork.git ~/.gemini/config/plugins/gemini-cowork
```

Antigravity discovers the plugin automatically without restarting.

## Development & Building the UI

`ui/cowork.html` is pre-bundled for zero-setup plugin installation. To modify and rebuild the UI:

```bash
cd ui
npm install
npm run build
```

## Testing

Run the full unit and JSON-RPC integration test suite:

```bash
python -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
