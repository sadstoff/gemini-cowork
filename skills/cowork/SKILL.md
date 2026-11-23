---
name: cowork
description: Autonomous desktop & folder coworker with Antigravity-native GUI, Office/OCR support, Browser Use, Computer Use, Scheduled Tasks, Sandbox VM, and OAuth/Plugin Hub.
---

# Cowork Skill (v2.0.0)

1. Mount scoped folders with `cowork_mount_folder` (`read_only`, `manual_approve`, `auto`).
2. Inspect and read text, code, `.pdf`, `.docx`, `.xlsx`, `.pptx`, and image OCR via `cowork_read_file` / `cowork_scan_workspace`.
3. Create `.docx`, `.xlsx`, `.pptx` deliverables with `cowork_office_create` and Markdown/CSV artifacts with `cowork_write_artifact` / `cowork_synthesize_index`.
4. Run isolated commands via `cowork_sandbox_exec` (`auto` -> `docker` / `wsl` / out-of-the-box `builtin`).
5. Automate web workflows with `cowork_browser_run` (Edge/Chrome CDP) or fetch public pages with `cowork_web_fetch`.
6. Control desktop apps with `cowork_computer_use` (`screenshot`, `list_windows`, `focus_window`, `find_text`, `click`, `type`, `hotkey`).
7. Schedule recurring background workflows with `cowork_schedule_manage` (cron or interval).
8. Connect services with `cowork_oauth_connect` / `cowork_connector_manage` / `cowork_connector_send` and discover installed Antigravity plugins with `cowork_plugins_list`.
