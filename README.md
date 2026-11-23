# Gemini Cowork (v2.0.0)

An Antigravity-native desktop & folder coworker plugin inspired by **Claude Cowork**, built with Python MCP + TypeScript MCP App UI.

## What's New in v2.0.0
- **Out-of-the-Box Sandbox VM (`cowork_sandbox_exec`)**: Includes a built-in temporary isolated workspace sandbox (`builtin`) that works immediately on Windows without Docker or WSL, plus optional `docker` (`--network=none --memory=512m`) and `wsl` backends.
- **Built-in Browser Use (`cowork_browser_run`)**: Interactive Edge/Chrome CDP automation with persistent profile (`browser_profile`), multi-step click/type/scroll/eval, DOM text extraction, and screenshot capture.
- **Computer Use (`cowork_computer_use`)**: Desktop screen capture, window listing & focus, screen OCR text-finding (`find_text`), mouse clicks, typing, and hotkeys.
- **Deep Office Formats & OCR (`cowork_read_file`, `cowork_office_create`)**: Read multi-page `.pdf` (including image OCR fallback), `.docx`, `.xlsx`, `.pptx`, and images (`.png`, `.jpg`, `.webp`), plus create `.docx`, `.xlsx`, and `.pptx` deliverables.
- **Scheduled Tasks (`cowork_schedule_manage`)**: Background cron/interval scheduler for `triage`, `brief`, `dedupe`, `index`, `web_fetch`, and `sandbox_exec` with optional connector notifications.
- **OAuth & Plugins Hub (`cowork_oauth_connect`, `cowork_plugins_list`)**: 1-click OAuth setup & connector management (Google, Slack, Notion, GitHub, Discord, Webhook) and discovery of installed Antigravity plugins.
- **Redesigned 6-Tab UI**: `Files & Office`, `Tasks & Cron`, `Browser & Screen`, `Sandbox VM`, `OAuth & Plugins`, and `Artifacts & Rules`.

## Quick Start
```powershell
python -m unittest discover -s tests -v
cd ui && npm install && npm run build
```
