import argparse
import base64
import csv
import fnmatch
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

from cowork_automation import (
    OAUTH_PRESETS,
    build_oauth_authorize_url,
    compute_next_run,
    discover_installed_plugins,
    run_browser_session,
    run_computer_action,
)
from cowork_core import (
    TEXT_EXTENSIONS,
    CoworkStore,
    build_state_payload,
    call_connector,
    find_covering_mount,
    is_safe_path,
    is_within,
    list_directory,
    pick_folder_dialog,
    plan_moves,
    resolve_dir_arg,
    run_in_sandbox,
    scan_folder,
    web_fetch_to_text,
)
from cowork_office import (
    IMAGE_OCR_EXTENSIONS,
    OFFICE_EXTENSIONS,
    create_office_file,
    extract_rich_file_text,
)

APP_URI = "ui://cowork/app.html"
PLUGIN_ROOT = Path(__file__).resolve().parent


def resolve_data_dir() -> Path:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--data_dir", default="")
    known, _ = parser.parse_known_args()
    if known.data_dir and "${" not in known.data_dir:
        return Path(known.data_dir)
    return PLUGIN_ROOT / ".data"


STORE = CoworkStore(resolve_data_dir())

INLINE_UI_META = {"ui": {"resourceUri": APP_URI}}
PANE_UI_META = {
    "ui": {"resourceUri": APP_URI},
    "com.google.antigravity/ui": {
        "entrypoints": [
            {"type": "aux_pane", "title": "Cowork"},
            {"type": "global", "title": "Cowork"},
        ]
    },
}
APP_ONLY_META = {"ui": {"visibility": ["app"]}}


def send_response(response):
    sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def tool_result(text: str, structured: dict | None = None, is_error: bool = False):
    res = {"content": [{"type": "text", "text": text}]}
    if structured is not None:
        res["structuredContent"] = structured
    if is_error:
        res["isError"] = True
    return res


def handle_list_resources():
    return {
        "resources": [
            {
                "uri": APP_URI,
                "name": "Cowork Workspace UI",
                "description": "Antigravity-native Claude Cowork workspace for tasks, mounted folders, artifacts, instructions, and rollback.",
                "mimeType": "text/html;profile=mcp-app",
                "_meta": {"ui": {"prefersBorder": False}},
            }
        ]
    }


def handle_read_resource(uri: str):
    if uri != APP_URI:
        return None
    html_override = os.environ.get("COWORK_APP_HTML", "").strip()
    html_path = Path(html_override) if html_override else (PLUGIN_ROOT / "ui" / "cowork.html")
    if not html_path.exists():
        html = f"<!doctype html><html><body><p>Missing UI bundle: {html_path}</p></body></html>"
    else:
        html = html_path.read_text(encoding="utf-8")
    return {
        "contents": [
            {
                "uri": APP_URI,
                "mimeType": "text/html;profile=mcp-app",
                "text": html,
                "_meta": {"ui": {"prefersBorder": False}},
            }
        ]
    }


def handle_list_tools():
    return {
        "tools": [
            {
                "name": "cowork_open_ui",
                "title": "Cowork",
                "description": "Open the Antigravity-native Cowork workspace (Tasks, Files, Artifacts, Organize, Instructions, and Rollback).",
                "inputSchema": {
                    "type": "object",
                    "properties": {"directory_path": {"type": "string"}},
                },
                "_meta": PANE_UI_META,
            },
            {
                "name": "cowork_mount_folder",
                "description": "Mount a local folder for Cowork with permission_mode (read_only, manual_approve, or auto). System roots are blocked.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string"},
                        "permission_mode": {"type": "string", "enum": ["read_only", "manual_approve", "auto"], "default": "manual_approve"},
                    },
                    "required": ["directory_path"],
                },
                "_meta": INLINE_UI_META,
            },
            {
                "name": "cowork_unmount_folder",
                "description": "Unmount a folder from Cowork.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"directory_path": {"type": "string"}},
                    "required": ["directory_path"],
                },
            },
            {
                "name": "cowork_plan",
                "description": "Create or incrementally update a Cowork task plan (create, update_step, add_step, clear).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["create", "update_step", "add_step", "clear"], "default": "create"},
                        "title": {"type": "string"},
                        "goal": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "string"}},
                        "step_index": {"type": "integer"},
                        "status": {"type": "string", "enum": ["pending", "in_progress", "done", "failed"], "default": "done"},
                        "step_text": {"type": "string"},
                    },
                },
                "_meta": INLINE_UI_META,
            },
            {
                "name": "cowork_write_artifact",
                "description": "Write a polished deliverable artifact (Markdown report, brief, CSV, JSON) inside a mounted folder and register it in Cowork Artifacts.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string"},
                        "filename": {"type": "string", "description": "Relative filename inside the mounted folder (e.g. _COWORK_BRIEF.md)"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["filename", "content"],
                },
                "_meta": INLINE_UI_META,
            },
            {
                "name": "cowork_log_action",
                "description": "Log an action to the Cowork audit trail.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string"},
                        "details": {"type": "string"},
                        "status": {"type": "string", "enum": ["in_progress", "done", "blocked"], "default": "done"},
                    },
                    "required": ["action", "details"],
                },
            },
            {
                "name": "cowork_list_dir",
                "description": "List files and subfolders inside a mounted/safe directory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string"},
                        "subpath": {"type": "string", "default": ""},
                    },
                },
            },
            {
                "name": "cowork_scan_workspace",
                "description": "Deep-scan a folder: categorize files, detect exact SHA-256 duplicates, stale files (>N days), and largest files.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string"},
                        "recursive": {"type": "boolean", "default": False},
                        "stale_days": {"type": "integer", "default": 90},
                    },
                },
                "_meta": INLINE_UI_META,
            },
            {
                "name": "cowork_search_files",
                "description": "Search files by filename glob/substring or text content inside a safe/mounted folder.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string"},
                        "query": {"type": "string"},
                        "content_query": {"type": "string"},
                        "recursive": {"type": "boolean", "default": True},
                        "max_results": {"type": "integer", "default": 40},
                    },
                },
            },
            {
                "name": "cowork_read_file",
                "description": "Safely read lines from a text file inside a mounted/safe folder.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "file_path": {"type": "string"},
                        "start_line": {"type": "integer", "default": 1},
                        "end_line": {"type": "integer", "default": 120},
                    },
                    "required": ["file_path"],
                },
            },
            {
                "name": "cowork_organize_files",
                "description": "Safely organize or normalize files (by_category, by_date, archive_stale, dedupe_review, normalize_names, custom). Respects read_only and manual_approve.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string"},
                        "strategy": {"type": "string", "enum": ["by_category", "by_date", "archive_stale", "dedupe_review", "normalize_names", "custom"], "default": "by_category"},
                        "stale_days": {"type": "integer", "default": 90},
                        "moves": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {"src": {"type": "string"}, "dst": {"type": "string"}},
                                "required": ["src", "dst"],
                            },
                        },
                        "dry_run": {"type": "boolean", "default": True},
                        "approved": {"type": "boolean", "default": False},
                    },
                },
                "_meta": INLINE_UI_META,
            },
            {
                "name": "cowork_rollback",
                "description": "Undo a previous cowork_organize_files batch and remove empty folders created by that batch.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"batch_id": {"type": "string"}},
                },
                "_meta": INLINE_UI_META,
            },
            {
                "name": "cowork_synthesize_index",
                "description": "Generate _COWORK_INDEX.md and _COWORK_SUMMARY.csv inside the target folder and register them as Cowork Artifacts.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string"},
                        "recursive": {"type": "boolean", "default": True},
                    },
                },
            },
            {
                "name": "cowork_sandbox_exec",
                "description": "Execute a shell command or script inside an isolated sandbox (built-in out-of-the-box temp VM, Docker --network=none, or WSL /tmp VM) without risking the host environment.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string"},
                        "directory_path": {"type": "string"},
                        "backend": {"type": "string", "enum": ["auto", "builtin", "wsl", "docker"], "default": "auto"},
                        "copy_workspace": {"type": "boolean", "default": False},
                    },
                    "required": ["command"],
                },
            },
            {
                "name": "cowork_web_fetch",
                "description": "Fetch a public http(s) webpage or JSON API, extract clean text, and optionally save it as a Markdown artifact in the mounted folder.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "directory_path": {"type": "string"},
                        "save_as": {"type": "string", "description": "Optional filename inside the mounted folder (e.g. _COWORK_WEB.md)"},
                    },
                    "required": ["url"],
                },
            },
            {
                "name": "cowork_connector_manage",
                "description": "List, add, or remove external service connectors (Slack webhook, Notion API, custom REST webhook).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "add", "remove"], "default": "list"},
                        "name": {"type": "string"},
                        "service": {"type": "string", "enum": ["slack", "notion", "discord", "webhook", "custom"], "default": "webhook"},
                        "url": {"type": "string"},
                        "token": {"type": "string"},
                    },
                },
            },
            {
                "name": "cowork_connector_send",
                "description": "Send a request or notification through a configured Cowork service connector.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "method": {"type": "string", "enum": ["POST", "GET"], "default": "POST"},
                        "payload": {"type": "object"},
                    },
                    "required": ["name"],
                },
            },
            {
                "name": "cowork_office_create",
                "description": "Create a Word (.docx), Excel (.xlsx), or PowerPoint (.pptx) deliverable inside a mounted folder and register it as a Cowork Artifact.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string"},
                        "filename": {"type": "string"},
                        "kind": {"type": "string", "enum": ["docx", "xlsx", "pptx"]},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "rows": {"type": "array", "items": {"type": "array"}},
                    },
                    "required": ["filename", "kind"],
                },
            },
            {
                "name": "cowork_browser_run",
                "description": "Run interactive browser automation (Edge/Chrome CDP with persistent profile): navigate, click, type, scroll, extract text, and capture screenshots.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                        "steps": {"type": "array", "items": {"type": "object"}},
                        "headless": {"type": "boolean", "default": True},
                        "save_screenshot": {"type": "boolean", "default": True},
                        "directory_path": {"type": "string"},
                    },
                },
            },
            {
                "name": "cowork_computer_use",
                "description": "Control desktop applications via screenshots, window focus, OCR text-finding, mouse clicks, typing, and hotkeys.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["screenshot", "list_windows", "focus_window", "click", "double_click", "move", "type", "hotkey", "scroll", "find_text"]},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "text": {"type": "string"},
                        "keys": {"type": "array", "items": {"type": "string"}},
                        "window_title": {"type": "string"},
                        "scroll_amount": {"type": "integer"},
                        "directory_path": {"type": "string"},
                    },
                    "required": ["action"],
                },
            },
            {
                "name": "cowork_schedule_manage",
                "description": "Manage background scheduled tasks (cron or interval_minutes) for triage, brief, dedupe, index, or web_fetch with optional connector notification.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["list", "add", "toggle", "remove", "run_now"], "default": "list"},
                        "id": {"type": "string"},
                        "name": {"type": "string"},
                        "workflow": {"type": "string", "enum": ["triage", "brief", "dedupe", "index", "web_fetch", "sandbox_exec"], "default": "brief"},
                        "cron": {"type": "string"},
                        "interval_minutes": {"type": "integer", "default": 60},
                        "directory_path": {"type": "string"},
                        "target": {"type": "string"},
                        "notify_connector": {"type": "string"},
                    },
                },
            },
            {
                "name": "cowork_plugins_list",
                "description": "Discover installed Antigravity plugins and their skills/MCP capabilities for Cowork coordination.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "cowork_oauth_connect",
                "description": "Generate/launch 1-click OAuth login flow or configure an authenticated service connector (Google, Slack, Notion, GitHub, Discord).",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string", "enum": ["google", "slack", "notion", "github", "discord", "webhook"]},
                        "name": {"type": "string"},
                        "client_id": {"type": "string"},
                        "token": {"type": "string"},
                        "url": {"type": "string"},
                        "open_browser": {"type": "boolean", "default": False},
                    },
                    "required": ["service"],
                },
            },
            # App-only tools
            {
                "name": "cowork_run_workflow",
                "description": "Execute a built-in Cowork workflow (triage, brief, dedupe, index) directly on the active folder.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "workflow": {"type": "string", "enum": ["triage", "brief", "dedupe", "index"]},
                        "directory_path": {"type": "string"},
                    },
                    "required": ["workflow"],
                },
            },
            {
                "name": "cowork_agent_turn",
                "description": "App-only: process a natural-language user task inside Cowork and append an assistant response with structured steps/actions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "prompt": {"type": "string"},
                        "directory_path": {"type": "string"},
                        "strategy": {"type": "string", "default": "by_category"},
                    },
                    "required": ["prompt"],
                },
                "_meta": APP_ONLY_META,
            },
            {
                "name": "cowork_app_get_state",
                "description": "App-only: retrieve full Cowork state.",
                "inputSchema": {"type": "object", "properties": {}},
                "_meta": APP_ONLY_META,
            },
            {
                "name": "cowork_app_pick_folder",
                "description": "App-only: open native Windows folder picker dialog and mount selected folder.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"permission_mode": {"type": "string", "enum": ["read_only", "manual_approve", "auto"], "default": "manual_approve"}},
                },
                "_meta": APP_ONLY_META,
            },
            {
                "name": "cowork_app_set_permission",
                "description": "App-only: update mount permission mode or active folder.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "directory_path": {"type": "string"},
                        "permission_mode": {"type": "string", "enum": ["read_only", "manual_approve", "auto"]},
                        "set_active": {"type": "boolean"},
                    },
                    "required": ["directory_path"],
                },
                "_meta": APP_ONLY_META,
            },
            {
                "name": "cowork_app_save_instructions",
                "description": "App-only: save Cowork custom instructions.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"instructions": {"type": "string"}},
                    "required": ["instructions"],
                },
                "_meta": APP_ONLY_META,
            },
            {
                "name": "cowork_app_clear_history",
                "description": "App-only: clear audit log, active plan, or chat feed.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"target": {"type": "string", "enum": ["audit", "plan", "chat"]}},
                    "required": ["target"],
                },
                "_meta": APP_ONLY_META,
            },
        ]
    }


def handle_call_tool(name: str, args: dict | None):
    args = args or {}
    sess = STORE.get_session()

    if name == "cowork_open_ui":
        focus = (args.get("directory_path") or "").strip()
        if focus:
            p = Path(focus)
            if p.exists() and p.is_dir() and is_safe_path(p):
                sess["active_path"] = str(p.resolve())
                STORE.save_session(sess)
        state = build_state_payload(STORE, {"view": "tasks"})
        instr = state.get("instructions", "").strip()
        summary = f"Cowork workspace ready. Mounted: {len(state['mounts'])}, Active: {state['active_path'] or 'none'}."
        if instr:
            summary += f"\nUser Cowork Instructions:\n{instr}"
        return tool_result(summary, state)

    if name == "cowork_app_get_state":
        return tool_result("ok", build_state_payload(STORE))

    if name == "cowork_run_workflow":
        wf = args.get("workflow", "triage")
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, build_state_payload(STORE, {"error": err}), is_error=True)

        # Ensure folder is mounted
        if not find_covering_mount(dir_path, sess):
            sess["mounts"].append({
                "path": str(dir_path),
                "permission_mode": "manual_approve",
                "mounted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            sess["active_path"] = str(dir_path)

        if wf == "triage":
            scan = scan_folder(dir_path, False, 90)
            scan["sample_files"] = scan.pop("all_files")[:25]
            STORE.save_json(STORE.last_scan_file, scan)
            planned = plan_moves(dir_path, "by_category", 90, [])
            preview = [
                {"src": str(s.relative_to(dir_path)).replace("\\", "/"), "dst": str(d.relative_to(dir_path)).replace("\\", "/")}
                for s, d in planned[:80]
            ]
            dry = {
                "dry_run": True,
                "directory": str(dir_path),
                "strategy": "by_category",
                "planned_moves": len(planned),
                "preview": preview,
            }
            sess["plan"] = {
                "title": f"Folder Triage: {dir_path.name}",
                "goal": f"Scan {dir_path.name}, prepare category organization ({len(planned)} moves), and await approval.",
                "steps": [
                    {"step": f"Scanned {scan['total_files']} files ({scan['total_mb']} MB)", "status": "done", "completed": True},
                    {"step": f"Prepared dry-run preview ({len(planned)} moves by category)", "status": "done" if planned else "done", "completed": True},
                    {"step": "Review & click Approve & Execute in Inspector", "status": "in_progress" if planned else "done", "completed": not bool(planned)},
                ],
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            STORE.save_session(sess)
            STORE.append_audit("workflow_triage", f"Scanned {scan['total_files']} files, {len(planned)} moves ready")
            return tool_result(
                f"Triage complete: {scan['total_files']} files scanned, {len(planned)} moves ready for approval.",
                build_state_payload(STORE, {"dry_run_preview": dry}),
            )

        if wf == "dedupe":
            scan = scan_folder(dir_path, False, 90)
            scan["sample_files"] = scan.pop("all_files")[:25]
            STORE.save_json(STORE.last_scan_file, scan)
            planned = plan_moves(dir_path, "dedupe_review", 90, [])
            preview = [
                {"src": str(s.relative_to(dir_path)).replace("\\", "/"), "dst": str(d.relative_to(dir_path)).replace("\\", "/")}
                for s, d in planned[:80]
            ]
            dry = {
                "dry_run": True,
                "directory": str(dir_path),
                "strategy": "dedupe_review",
                "planned_moves": len(planned),
                "preview": preview,
            }
            sess["plan"] = {
                "title": f"Duplicate Audit: {dir_path.name}",
                "goal": f"Find exact SHA-256 duplicates ({len(scan['duplicate_groups'])} groups) and isolate extra copies.",
                "steps": [
                    {"step": f"Hashed {scan['total_files']} files (SHA-256)", "status": "done", "completed": True},
                    {"step": f"Found {len(scan['duplicate_groups'])} duplicate groups ({len(planned)} extra copies)", "status": "done", "completed": True},
                    {"step": "Approve moving extra copies to _Duplicates_Review" if planned else "No duplicates found", "status": "in_progress" if planned else "done", "completed": not bool(planned)},
                ],
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            STORE.save_session(sess)
            STORE.append_audit("workflow_dedupe", f"Found {len(scan['duplicate_groups'])} duplicate groups")
            return tool_result(
                f"Duplicate audit complete: {len(scan['duplicate_groups'])} groups, {len(planned)} extra copies.",
                build_state_payload(STORE, {"dry_run_preview": dry}),
            )

        if wf == "brief":
            scan = scan_folder(dir_path, True, 90)
            doc_snippets = []
            for item in scan["all_files"][:40]:
                fp = dir_path / item["rel_path"]
                suf = fp.suffix.lower()
                if suf in OFFICE_EXTENSIONS and fp.stat().st_size <= 8 * 1024 * 1024:
                    try:
                        _, rich_txt = extract_rich_file_text(fp, max_chars=600)
                        excerpt = " ".join(rich_txt.splitlines()[1:6])[:220]
                        doc_snippets.append((item["rel_path"], item["size_kb"], excerpt))
                    except Exception:
                        pass
                elif suf in TEXT_EXTENSIONS and fp.stat().st_size <= 256 * 1024:
                    try:
                        lines = [ln.strip() for ln in fp.read_text(encoding="utf-8", errors="ignore").splitlines() if ln.strip()]
                        excerpt = " ".join(lines[:4])[:220]
                        doc_snippets.append((item["rel_path"], item["size_kb"], excerpt))
                    except Exception:
                        pass
            brief_path = dir_path / "_COWORK_BRIEF.md"
            lines_out = [
                f"# Workspace Brief: `{dir_path.name}`",
                f"- **Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}",
                f"- **Total Files:** {scan['total_files']} ({scan['total_mb']} MB)",
                f"- **Stale (>90 days):** {scan['stale_count']}",
                f"- **Duplicate Groups:** {len(scan['duplicate_groups'])}",
                "",
                "## Category Breakdown",
            ]
            for cat, info in scan["categories"].items():
                lines_out.append(f"- **{cat}**: {info['count']} files ({info['mb']} MB)")
            if doc_snippets:
                lines_out.extend(["", "## Key Text & Document Excerpts"])
                for rel, kb, exc in doc_snippets[:15]:
                    lines_out.append(f"- `{rel}` ({kb} KB): {exc}")
            brief_text = "\n".join(lines_out)
            brief_path.write_text(brief_text, encoding="utf-8")
            STORE.add_artifact(f"{dir_path.name} Brief", str(brief_path), "MD", f"{len(doc_snippets)} docs summarized")
            sess["plan"] = {
                "title": f"Research Brief: {dir_path.name}",
                "goal": "Scan workspace documents and synthesize _COWORK_BRIEF.md",
                "steps": [
                    {"step": f"Scanned {scan['total_files']} files recursively", "status": "done", "completed": True},
                    {"step": f"Extracted excerpts from {len(doc_snippets)} text/code files", "status": "done", "completed": True},
                    {"step": "Created _COWORK_BRIEF.md artifact", "status": "done", "completed": True},
                ],
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            STORE.save_session(sess)
            STORE.append_audit("workflow_brief", f"Generated _COWORK_BRIEF.md ({len(doc_snippets)} excerpts)")
            return tool_result(
                f"Brief created at {brief_path}",
                build_state_payload(STORE, {
                    "preview": {
                        "path": str(brief_path),
                        "name": brief_path.name,
                        "total_lines": len(lines_out),
                        "text": brief_text,
                    }
                }),
            )

        # index
        return handle_call_tool("cowork_synthesize_index", {"directory_path": str(dir_path), "recursive": True})

    if name == "cowork_app_save_instructions":
        text = args.get("instructions", "")
        STORE.save_instructions(text)
        STORE.append_audit("save_instructions", f"Updated Cowork instructions ({len(text)} chars)")
        return tool_result("Instructions saved.", build_state_payload(STORE))

    if name == "cowork_app_pick_folder":
        picked = pick_folder_dialog()
        if not picked:
            return tool_result("Folder selection cancelled.", build_state_payload(STORE))
        return handle_call_tool("cowork_mount_folder", {
            "directory_path": picked,
            "permission_mode": args.get("permission_mode", "manual_approve"),
        })

    if name == "cowork_app_set_permission":
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, build_state_payload(STORE, {"error": err}), is_error=True)
        resolved = str(dir_path)
        updated = False
        for m in sess["mounts"]:
            if m["path"].lower() == resolved.lower():
                if args.get("permission_mode"):
                    m["permission_mode"] = args["permission_mode"]
                updated = True
        if args.get("set_active"):
            sess["active_path"] = resolved
        if not updated:
            return tool_result(f"Folder is not mounted: {resolved}", build_state_payload(STORE), is_error=True)
        STORE.save_session(sess)
        STORE.append_audit("update_mount", f"{resolved} -> {args.get('permission_mode', 'focused')}")
        return tool_result("Updated mount settings.", build_state_payload(STORE))

    if name == "cowork_app_clear_history":
        target = args.get("target")
        if target == "audit":
            STORE.save_json(STORE.history_file, [])
        elif target == "plan":
            sess["plan"] = None
            STORE.save_session(sess)
        elif target == "chat":
            sess["messages"] = []
            sess["plan"] = None
            STORE.save_session(sess)
        return tool_result(f"Cleared {target}.", build_state_payload(STORE))

    if name == "cowork_agent_turn":
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return tool_result("Empty prompt.", is_error=True)
        sess.setdefault("messages", []).append({
            "role": "user",
            "text": prompt,
            "ts": time.strftime("%H:%M"),
        })
        STORE.save_session(sess)
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            sess["messages"].append({
                "role": "assistant",
                "text": "Сначала подключи папку слева (`+ Mount Folder`), чтобы я мог работать с твоими файлами.",
                "ts": time.strftime("%H:%M"),
            })
            STORE.save_session(sess)
            return tool_result("No active folder.", build_state_payload(STORE))

        low = prompt.lower()
        strategy = args.get("strategy", "by_category")
        extra_state = {}

        if any(k in low for k in ["откат", "верни", "undo", "rollback"]):
            batches = STORE.load_json(STORE.rollback_file, [])
            if not batches:
                reply = "Нет сохранённых батчей для отката."
            else:
                res = handle_call_tool("cowork_rollback", {})
                rb = res.get("structuredContent", {}).get("last_rollback", {})
                reply = f"Откатил батч `{rb.get('batch_id', '')}` — восстановлено файлов: **{rb.get('restored', 0)}**, пустые папки удалены."
        elif any(k in low for k in ["дубл", "dup", "копи"]):
            res = handle_call_tool("cowork_run_workflow", {"workflow": "dedupe", "directory_path": str(dir_path)})
            sc = res.get("structuredContent", {})
            dry = sc.get("dry_run_preview", {})
            scan = sc.get("last_scan", {})
            grps = scan.get("duplicate_groups", [])
            lines = [f"Проверил `{dir_path}` по SHA-256.", f"- Найдено групп дубликатов: **{len(grps)}**", f"- Лишних копий к переносу в `_Duplicates_Review`: **{dry.get('planned_moves', 0)}**"]
            for g in grps[:5]:
                lines.append(f"- `{g['size_kb']} KB`: " + ", ".join(f"`{f}`" for f in g["files"][:3]))
            reply = "\n".join(lines)
            extra_state["dry_run_preview"] = dry
        elif any(k in low for k in ["бриф", "отчет", "отчёт", "сводк", "brief", "summar", "проанализ"]):
            res = handle_call_tool("cowork_run_workflow", {"workflow": "brief", "directory_path": str(dir_path)})
            sc = res.get("structuredContent", {})
            prev = sc.get("preview", {})
            reply = f"Готово — проанализировал документы в `{dir_path.name}` и сохранил отчёт `_COWORK_BRIEF.md`.\n\n" + "\n".join(prev.get("text", "").splitlines()[:12])
        elif any(k in low for k in ["индекс", "таблиц", "csv", "index"]):
            res = handle_call_tool("cowork_run_workflow", {"workflow": "index", "directory_path": str(dir_path)})
            sc = res.get("structuredContent", {})
            idx = sc.get("index_result", {})
            reply = f"Создал индекс по `{dir_path.name}` (**{idx.get('total_files', 0)}** файлов):\n- `_COWORK_INDEX.md`\n- `_COWORK_SUMMARY.csv`"
        elif any(k in low for k in ["сорт", "разбер", "порядок", "triage", "clean", "organize", "архив", "переимен"]):
            if "дат" in low or "date" in low:
                strategy = "by_date"
            elif "стар" in low or "архив" in low or "stale" in low:
                strategy = "archive_stale"
            elif "имен" in low or "переимен" in low or "norm" in low:
                strategy = "normalize_names"
            res = handle_call_tool("cowork_organize_files", {"directory_path": str(dir_path), "strategy": strategy, "dry_run": True})
            sc = res.get("structuredContent", {})
            dry = sc.get("dry_run_preview", {})
            moves = dry.get("planned_moves", 0)
            reply = f"Подготовил **dry-run** план для `{dir_path.name}` (стратегия `{strategy}`): **{moves}** перемещений. Проверь список ниже и нажми **Approve & Execute**, если всё ок."
            extra_state["dry_run_preview"] = dry
        elif any(k in low for k in ["посчита", "сумм", "итог", "total", "sum", "чек", "счет", "счёт", "invoice", "receipt", "отправ"]):
            # Compound NL pipeline: search files -> calculate in sandbox -> save artifact -> notify connector
            syn_groups = [
                ("чек", "receipt"),
                ("счет", "счёт", "invoice"),
                ("октябр", "oct"),
                ("ноябр", "nov"),
                ("декабр", "dec"),
                ("январ", "jan"),
            ]
            keywords = []
            for grp in syn_groups:
                if any(g in low for g in grp):
                    keywords.extend(grp)
            matched_files = []
            for p in dir_path.rglob("*"):
                if not p.is_file() or p.name.startswith("_COWORK_"):
                    continue
                suf = p.suffix.lower()
                if suf not in (TEXT_EXTENSIONS | OFFICE_EXTENSIONS | IMAGE_OCR_EXTENSIONS):
                    continue
                rel = str(p.relative_to(dir_path)).replace("\\", "/")
                if not keywords or any(k in rel.lower() for k in keywords):
                    matched_files.append(p)
                elif suf in TEXT_EXTENSIONS and p.stat().st_size <= 256 * 1024:
                    txt_low = p.read_text(encoding="utf-8", errors="ignore").lower()
                    if any(k in txt_low for k in keywords):
                        matched_files.append(p)
            if not matched_files:
                matched_files = [p for p in dir_path.rglob("*") if p.is_file() and not p.name.startswith("_COWORK_") and p.suffix.lower() in (TEXT_EXTENSIONS | OFFICE_EXTENSIONS)][:20]

            extracted_chunks = []
            for fp in matched_files[:20]:
                try:
                    if fp.suffix.lower() in (OFFICE_EXTENSIONS | IMAGE_OCR_EXTENSIONS):
                        _, t = extract_rich_file_text(fp, max_chars=4000)
                    else:
                        t = fp.read_text(encoding="utf-8", errors="ignore")[:4000]
                    extracted_chunks.append((fp.name, t))
                except Exception:
                    pass

            # Calculate numeric totals inside isolated builtin sandbox (base64-safe across shells)
            import re as _re
            nums_per_file = []
            for fname, txt in extracted_chunks:
                found = [float(x.replace(",", ".")) for x in _re.findall(r"(?<!\d)(\d{1,7}(?:[.,]\d{1,2})?)(?!\d)", txt)]
                amounts = [n for n in found if not (2020 <= n <= 2035 and n.is_integer())]
                nums_per_file.append({"file": fname, "numbers": amounts[:20], "subtotal": round(sum(amounts), 2)})

            b64_payload = base64.b64encode(json.dumps(nums_per_file, ensure_ascii=False).encode("utf-8")).decode("ascii")
            sb_cmd = f"python -c \"import base64,json; d=json.loads(base64.b64decode('{b64_payload}')); print(json.dumps({{'files':len(d),'total':round(sum(x['subtotal'] for x in d),2)}}))\""
            sb_res = run_in_sandbox(sb_cmd, backend="builtin", copy_workspace=False)
            try:
                calc = json.loads(sb_res.get("stdout", "{}").strip() or "{}")
            except Exception:
                calc = {"files": len(nums_per_file), "total": round(sum(x["subtotal"] for x in nums_per_file), 2)}

            report_lines = [
                f"# Cowork NL Report",
                f"- **Query:** {prompt}",
                f"- **Matched files:** {len(matched_files)}",
                f"- **Sandbox total ({sb_res.get('backend', 'builtin')}):** **{calc.get('total', 0)}**",
                "",
                "## Breakdown",
            ]
            for item in nums_per_file:
                report_lines.append(f"- `{item['file']}`: subtotal **{item['subtotal']}**")

            # Check if user asked to send to a connector (Slack / Discord / Notion / Webhook)
            conns = STORE.get_connectors(include_secrets=False)
            target_conn = next((c for c in conns if c["service"].lower() in low or c["name"].lower() in low), None)
            if not target_conn and any(w in low for w in ("отправ", "send", "slack", "discord", "notion", "webhook")) and conns:
                target_conn = conns[0]

            conn_note = ""
            if target_conn:
                send_res = handle_call_tool("cowork_connector_send", {"name": target_conn["name"], "payload": {"text": "\n".join(report_lines[:15])}})
                conn_note = f"\n- **Connector `{target_conn['name']}`:** {'Sent!' if not send_res.get('isError') else send_res['content'][0]['text']}"
            elif any(w in low for w in ("slack", "discord", "notion", "отправ", "send")):
                conn_note = "\n- **Connector:** Не настроен. Добавь Slack/Discord/Webhook во вкладке **OAuth & Plugins**."

            report_text = "\n".join(report_lines) + conn_note
            rep_path = dir_path / "_COWORK_NL_REPORT.md"
            rep_path.write_text(report_text, encoding="utf-8")
            STORE.add_artifact("NL Command Report", str(rep_path), "MD", f"Total: {calc.get('total', 0)} ({len(matched_files)} files)")
            STORE.append_audit("nl_pipeline", f"files={len(matched_files)}, total={calc.get('total', 0)}")
            reply = report_text
        elif any(k in low for k in ["мест", "тяж", "больш", "размер", "largest", "heavy", "сколько", "что в", "скан", "scan"]):
            scan = scan_folder(dir_path, True, 90)
            top = scan.get("largest_files", [])[:6]
            scan["sample_files"] = scan.pop("all_files")[:25]
            STORE.save_json(STORE.last_scan_file, scan)
            lines = [f"Просканировал `{dir_path}` (**{scan['total_files']}** файлов, **{scan['total_mb']} MB**, старых >90д: **{scan['stale_count']}**).", "", "### Самые тяжёлые файлы:"]
            for f in top:
                lines.append(f"- `{f['rel_path']}` — **{round(f['size_kb'] / 1024, 2)} MB** ({f['category']})")
            reply = "\n".join(lines)
        else:
            q = prompt.strip()
            res = handle_call_tool("cowork_search_files", {"directory_path": str(dir_path), "query": q if "*" in q else "", "content_query": "" if "*" in q else q, "recursive": True, "max_results": 12})
            sc = res.get("structuredContent", {})
            matches = sc.get("matches", [])
            if not matches and "*" not in q:
                res = handle_call_tool("cowork_search_files", {"directory_path": str(dir_path), "query": q, "recursive": True, "max_results": 12})
                matches = res.get("structuredContent", {}).get("matches", [])
            if matches:
                lines = [f"Нашёл **{len(matches)}** совпадений в `{dir_path.name}` по запросу `{q}`:"]
                for m in matches[:10]:
                    snip = f" — *{m['snippet'][:90]}...*" if m.get("snippet") else ""
                    lines.append(f"- `{m['rel_path']}` ({m['size_kb']} KB){snip}")
                reply = "\n".join(lines)
            else:
                reply = (
                    f"В `{dir_path.name}` по запросу `{q}` совпадений не найдено.\n\n"
                    "Можешь попросить меня:\n"
                    "- *«Что занимает больше всего места?»*\n"
                    "- *«Найди дубликаты»*\n"
                    "- *«Сделай бриф по документам»*\n"
                    "- *«Отсортируй файлы по категориям»*"
                )

        sess = STORE.get_session()
        sess.setdefault("messages", []).append({
            "role": "assistant",
            "text": reply,
            "ts": time.strftime("%H:%M"),
        })
        STORE.save_session(sess)
        return tool_result(reply, build_state_payload(STORE, extra_state))

    if name == "cowork_mount_folder":
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, build_state_payload(STORE, {"error": err}), is_error=True)
        resolved = str(dir_path)
        mode = args.get("permission_mode", "manual_approve")
        mounts = [m for m in sess.get("mounts", []) if m.get("path", "").lower() != resolved.lower()]
        mounts.append({
            "path": resolved,
            "permission_mode": mode,
            "mounted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        sess["mounts"] = mounts
        sess["active_path"] = resolved
        STORE.save_session(sess)
        STORE.append_audit("mount_folder", f"{resolved} ({mode})")
        return tool_result(
            json.dumps({"ok": True, "mounted": resolved, "permission_mode": mode}, ensure_ascii=False),
            build_state_payload(STORE, {"view": "files"}),
        )

    if name == "cowork_unmount_folder":
        raw = (args.get("directory_path") or "").strip()
        if not raw:
            return tool_result("directory_path is required.", is_error=True)
        resolved = str(Path(raw).resolve()).lower()
        sess["mounts"] = [m for m in sess.get("mounts", []) if m.get("path", "").lower() != resolved]
        if sess.get("active_path", "").lower() == resolved:
            sess["active_path"] = sess["mounts"][-1]["path"] if sess["mounts"] else ""
        STORE.save_session(sess)
        STORE.append_audit("unmount_folder", raw)
        return tool_result(f"Unmounted {raw}", build_state_payload(STORE))

    if name == "cowork_list_dir":
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, is_error=True)
        try:
            listing = list_directory(dir_path, args.get("subpath", ""))
        except ValueError as e:
            return tool_result(str(e), is_error=True)
        return tool_result(json.dumps(listing, ensure_ascii=False, indent=2), {"listing": listing})

    if name == "cowork_plan":
        action = args.get("action", "create")
        if action == "clear":
            sess["plan"] = None
            STORE.save_session(sess)
            return tool_result("Cowork plan cleared.", build_state_payload(STORE, {"view": "tasks"}))
        if action == "update_step":
            plan = sess.get("plan")
            if not plan:
                return tool_result("No active plan to update.", is_error=True)
            idx = args.get("step_index")
            status = args.get("status", "done")
            if not isinstance(idx, int) or idx < 0 or idx >= len(plan["steps"]):
                return tool_result(f"Invalid step_index: {idx}", is_error=True)
            plan["steps"][idx]["status"] = status
            plan["steps"][idx]["completed"] = (status == "done")
            plan["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            # Sync with latest task entry
            if sess.get("tasks"):
                all_done = all(s.get("status") == "done" for s in plan["steps"])
                sess["tasks"][-1]["status"] = "completed" if all_done else "in_progress"
                sess["tasks"][-1]["done_steps"] = sum(1 for s in plan["steps"] if s.get("status") == "done")
            STORE.save_session(sess)
            STORE.append_audit("plan_step", f"Step {idx + 1} -> {status}")
            return tool_result(f"Updated step {idx + 1} to {status}.", build_state_payload(STORE, {"view": "tasks"}))
        if action == "add_step":
            plan = sess.get("plan")
            if not plan:
                return tool_result("No active plan.", is_error=True)
            text = (args.get("step_text") or "").strip()
            if not text:
                return tool_result("step_text is required.", is_error=True)
            plan["steps"].append({"step": text, "status": "pending", "completed": False})
            plan["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
            STORE.save_session(sess)
            return tool_result(f"Added step: {text}", build_state_payload(STORE, {"view": "tasks"}))

        steps = [{"step": s, "status": "pending", "completed": False} for s in args.get("steps", [])]
        done_idx = args.get("step_index")
        if isinstance(done_idx, int) and 0 <= done_idx < len(steps):
            steps[done_idx]["status"] = args.get("status", "done")
            steps[done_idx]["completed"] = (steps[done_idx]["status"] == "done")
        plan_obj = {
            "title": args.get("title", "Cowork Task"),
            "goal": args.get("goal", ""),
            "steps": steps,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        sess["plan"] = plan_obj
        sess.setdefault("tasks", []).append({
            "id": f"task_{int(time.time())}",
            "title": plan_obj["title"],
            "goal": plan_obj["goal"],
            "total_steps": len(steps),
            "done_steps": sum(1 for s in steps if s.get("status") == "done"),
            "status": "in_progress",
            "created_at": plan_obj["updated_at"],
        })
        STORE.save_session(sess)
        STORE.append_audit("create_plan", f"{plan_obj['title']} ({len(steps)} steps)")
        return tool_result(
            f"Cowork plan saved: '{plan_obj['title']}' ({len(steps)} steps).",
            build_state_payload(STORE, {"view": "tasks"}),
        )

    if name == "cowork_write_artifact":
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, is_error=True)
        mount = find_covering_mount(dir_path, sess)
        if not mount:
            return tool_result(f"Folder '{dir_path}' is not mounted. Mount it first.", is_error=True)
        if mount.get("permission_mode") == "read_only":
            return tool_result(f"Folder '{mount['path']}' is mounted as read_only.", is_error=True)
        rel_name = (args.get("filename") or "").strip().lstrip("/\\")
        if not rel_name:
            return tool_result("filename is required.", is_error=True)
        target = (dir_path / rel_name).resolve()
        if not is_within(target, dir_path):
            return tool_result("Path traversal blocked.", is_error=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.get("content", ""), encoding="utf-8")
        title = (args.get("title") or target.name).strip()
        kind = target.suffix.lstrip(".").upper() or "FILE"
        STORE.add_artifact(title, str(target), kind, args.get("summary", ""))
        STORE.append_audit("write_artifact", f"Created {target.name} in {dir_path}")
        return tool_result(
            json.dumps({"ok": True, "path": str(target), "title": title}, ensure_ascii=False),
            build_state_payload(STORE, {"view": "artifacts"}),
        )

    if name == "cowork_log_action":
        STORE.append_audit(args.get("action", ""), args.get("details", ""), args.get("status", "done"))
        return tool_result(f"Logged: {args.get('action')}")

    if name == "cowork_scan_workspace":
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, build_state_payload(STORE, {"error": err}), is_error=True)
        res = scan_folder(dir_path, bool(args.get("recursive", False)), int(args.get("stale_days", 90)))
        res["sample_files"] = res.pop("all_files")[:25]
        STORE.save_json(STORE.last_scan_file, res)
        STORE.append_audit("scan_workspace", f"{dir_path} ({res['total_files']} files, {res['total_mb']} MB)")
        return tool_result(json.dumps(res, ensure_ascii=False, indent=2), build_state_payload(STORE, {"view": "organize"}))

    if name == "cowork_search_files":
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, is_error=True)
        query = (args.get("query") or "").strip().lower()
        content_query = (args.get("content_query") or "").strip().lower()
        recursive = bool(args.get("recursive", True))
        max_results = min(int(args.get("max_results", 40)), 100)
        pattern = "**/*" if recursive else "*"
        matches = []
        for p in dir_path.glob(pattern):
            if not p.is_file() or p.name.startswith("_COWORK_"):
                continue
            rel = str(p.relative_to(dir_path)).replace("\\", "/")
            if query:
                is_glob = any(ch in query for ch in "*?[]")
                name_match = fnmatch.fnmatch(p.name.lower(), query) if is_glob else (query in rel.lower())
                if not name_match:
                    continue
            snippet = ""
            if content_query:
                if p.suffix.lower() not in TEXT_EXTENSIONS or p.stat().st_size > 2 * 1024 * 1024:
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                idx = text.lower().find(content_query)
                if idx == -1:
                    continue
                start = max(0, idx - 60)
                end = min(len(text), idx + len(content_query) + 80)
                snippet = text[start:end].replace("\r", " ").replace("\n", " ")
            st = p.stat()
            matches.append({
                "rel_path": rel,
                "full_path": str(p.resolve()),
                "size_kb": round(st.st_size / 1024, 2),
                "mtime": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime)),
                "snippet": snippet,
            })
            if len(matches) >= max_results:
                break
        payload = {"directory": str(dir_path), "count": len(matches), "matches": matches}
        return tool_result(json.dumps(payload, ensure_ascii=False, indent=2), payload)

    if name == "cowork_read_file":
        raw_fp = (args.get("file_path") or "").strip()
        if not raw_fp:
            return tool_result("file_path is required.", is_error=True)
        fp = Path(raw_fp)
        if not fp.exists() or not fp.is_file():
            return tool_result(f"File not found: {fp}", is_error=True)
        if not is_safe_path(fp):
            return tool_result(f"Blocked unsafe system path: {fp}", is_error=True)
        start_line = max(1, int(args.get("start_line", 1)))
        end_line = max(start_line, int(args.get("end_line", start_line + 119)))
        try:
            if fp.suffix.lower() in (OFFICE_EXTENSIONS | IMAGE_OCR_EXTENSIONS):
                _, rich_text = extract_rich_file_text(fp)
                lines = rich_text.splitlines()
            else:
                lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as e:
            return tool_result(f"Failed to read file: {e}", is_error=True)
        sliced = lines[start_line - 1 : end_line]
        numbered = "\n".join(f"{start_line + i}: {line}" for i, line in enumerate(sliced))
        content = f"File: {fp.resolve()} (lines {start_line}-{min(end_line, len(lines))} of {len(lines)})\n\n{numbered}"
        preview_obj = {
            "path": str(fp.resolve()),
            "name": fp.name,
            "total_lines": len(lines),
            "text": "\n".join(sliced),
        }
        ext_low = fp.suffix.lower()
        if ext_low in (IMAGE_OCR_EXTENSIONS | {".gif"}) and fp.stat().st_size <= 2 * 1024 * 1024:
            mime = "jpeg" if ext_low in (".jpg", ".jpeg") else ext_low.lstrip(".")
            preview_obj["image_data_url"] = f"data:image/{mime};base64,{base64.b64encode(fp.read_bytes()).decode('ascii')}"
        return tool_result(content, {"preview": preview_obj})

    if name == "cowork_organize_files":
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, build_state_payload(STORE, {"error": err}), is_error=True)
        strategy = args.get("strategy", "by_category")
        dry_run = bool(args.get("dry_run", True))
        approved = bool(args.get("approved", False))
        stale_days = int(args.get("stale_days", 90))

        mount = find_covering_mount(dir_path, sess)
        if not dry_run:
            if not mount:
                msg = f"Folder '{dir_path}' is not mounted. Call cowork_mount_folder first."
                return tool_result(msg, build_state_payload(STORE, {"error": msg}), is_error=True)
            mode = mount.get("permission_mode", "manual_approve")
            if mode == "read_only":
                msg = f"Folder '{mount['path']}' is mounted as read_only. Switch to manual_approve or auto first."
                return tool_result(msg, build_state_payload(STORE, {"error": msg}), is_error=True)
            if mode == "manual_approve" and not approved:
                msg = f"Permission mode for '{mount['path']}' is 'manual_approve'. Run dry_run=true first and pass approved=true after user confirmation."
                return tool_result(msg, build_state_payload(STORE, {"error": msg}), is_error=True)

        planned = plan_moves(dir_path, strategy, stale_days, args.get("moves", []))
        if dry_run:
            preview = [
                {"src": str(s.relative_to(dir_path)).replace("\\", "/"), "dst": str(d.relative_to(dir_path)).replace("\\", "/")}
                for s, d in planned[:80]
            ]
            payload = {
                "dry_run": True,
                "directory": str(dir_path),
                "strategy": strategy,
                "planned_moves": len(planned),
                "preview": preview,
            }
            return tool_result(json.dumps(payload, ensure_ascii=False, indent=2), build_state_payload(STORE, {"view": "organize", "dry_run_preview": payload}))

        batch_id = time.strftime("batch_%Y%m%d_%H%M%S")
        executed = []
        created_dirs = set()
        for s, d in planned:
            if not d.parent.exists():
                created_dirs.add(str(d.parent))
            d.parent.mkdir(parents=True, exist_ok=True)
            final_dst = d
            if final_dst.exists():
                stem, suf = final_dst.stem, final_dst.suffix
                final_dst = final_dst.with_name(f"{stem}_{int(time.time())}{suf}")
            shutil.move(str(s), str(final_dst))
            executed.append({"src": str(s), "dst": str(final_dst)})

        batches = STORE.load_json(STORE.rollback_file, [])
        batches.append({
            "batch_id": batch_id,
            "directory": str(dir_path),
            "strategy": strategy,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "created_dirs": sorted(created_dirs, key=len, reverse=True),
            "moves": executed,
        })
        STORE.save_json(STORE.rollback_file, batches[-20:])
        STORE.append_audit("organize_files", f"{batch_id} ({strategy}): moved {len(executed)} files")
        res_obj = {"dry_run": False, "batch_id": batch_id, "moved": len(executed), "strategy": strategy}
        return tool_result(json.dumps(res_obj, ensure_ascii=False), build_state_payload(STORE, {"view": "organize", "last_batch": res_obj}))

    if name == "cowork_rollback":
        batches = STORE.load_json(STORE.rollback_file, [])
        if not batches:
            return tool_result("No rollback batches found.", build_state_payload(STORE), is_error=True)
        bid = args.get("batch_id")
        target = next((b for b in reversed(batches) if not bid or b["batch_id"] == bid), None)
        if not target:
            return tool_result(f"Batch not found: {bid}", build_state_payload(STORE), is_error=True)
        restored = 0
        for m in reversed(target["moves"]):
            src, dst = Path(m["src"]), Path(m["dst"])
            if dst.exists() and not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
                restored += 1
        for d_str in target.get("created_dirs", []):
            dp = Path(d_str)
            try:
                if dp.exists() and dp.is_dir() and not any(dp.iterdir()):
                    dp.rmdir()
            except OSError:
                pass
        batches = [b for b in batches if b["batch_id"] != target["batch_id"]]
        STORE.save_json(STORE.rollback_file, batches)
        STORE.append_audit("rollback", f"Restored {restored} files from {target['batch_id']}")
        res_obj = {"ok": True, "batch_id": target["batch_id"], "restored": restored}
        return tool_result(json.dumps(res_obj, ensure_ascii=False), build_state_payload(STORE, {"view": "files", "last_rollback": res_obj}))

    if name == "cowork_synthesize_index":
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, is_error=True)
        data = scan_folder(dir_path, bool(args.get("recursive", True)), 90)
        md_path = dir_path / "_COWORK_INDEX.md"
        csv_path = dir_path / "_COWORK_SUMMARY.csv"
        md_lines = [
            f"# Cowork Index: `{dir_path.name}`",
            f"- **Path:** `{dir_path}`",
            f"- **Total files:** {data['total_files']} ({data['total_mb']} MB)",
            f"- **Stale (>90 days):** {data['stale_count']}",
            "",
            "## Categories",
            "| Category | Files | Size (MB) |",
            "|---|---|---|",
        ]
        for cat, info in data["categories"].items():
            md_lines.append(f"| {cat} | {info['count']} | {info['mb']} |")
        if data["duplicate_groups"]:
            md_lines.extend(["", "## Potential Duplicates"])
            for g in data["duplicate_groups"]:
                md_lines.append(f"- ({g['size_kb']} KB): " + ", ".join(f"`{x}`" for x in g["files"]))
        md_path.write_text("\n".join(md_lines), encoding="utf-8")

        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=["rel_path", "name", "category", "size_kb", "mtime"])
            writer.writeheader()
            writer.writerows(data["all_files"])

        STORE.add_artifact(f"{dir_path.name} Index", str(md_path), "MD", f"{data['total_files']} files indexed")
        STORE.add_artifact(f"{dir_path.name} Summary CSV", str(csv_path), "CSV", f"{data['total_mb']} MB")
        STORE.append_audit("synthesize_index", f"Created {md_path.name} and {csv_path.name}")
        res_obj = {"ok": True, "markdown": str(md_path), "csv": str(csv_path), "total_files": data["total_files"]}
        return tool_result(json.dumps(res_obj, ensure_ascii=False), build_state_payload(STORE, {"view": "artifacts", "index_result": res_obj}))

    if name == "cowork_sandbox_exec":
        cmd = (args.get("command") or "").strip()
        if not cmd:
            return tool_result("command is required.", is_error=True)
        work_dir = None
        if args.get("directory_path") or sess.get("active_path"):
            work_dir, err = resolve_dir_arg(args, sess)
            if err:
                return tool_result(err, is_error=True)
        try:
            res = run_in_sandbox(
                cmd,
                work_dir=work_dir,
                backend=args.get("backend", "auto"),
                copy_workspace=bool(args.get("copy_workspace", False)),
            )
            STORE.append_audit("sandbox_exec", f"[{res['backend']}] rc={res['returncode']}: {cmd[:80]}")
            return tool_result(json.dumps(res, ensure_ascii=False, indent=2), build_state_payload(STORE, {"sandbox_result": res}))
        except Exception as e:
            return tool_result(str(e), is_error=True)

    if name == "cowork_web_fetch":
        url = (args.get("url") or "").strip()
        if not url:
            return tool_result("url is required.", is_error=True)
        try:
            fetched = web_fetch_to_text(url)
        except Exception as e:
            return tool_result(f"Web fetch failed: {e}", is_error=True)

        save_as = (args.get("save_as") or "").strip()
        if save_as:
            dir_path, err = resolve_dir_arg(args, sess)
            if err:
                return tool_result(err, is_error=True)
            mount = find_covering_mount(dir_path, sess)
            if not mount or mount.get("permission_mode") == "read_only":
                return tool_result("Cannot save artifact: folder must be mounted in manual_approve or auto mode.", is_error=True)
            target = (dir_path / save_as.lstrip("/\\")).resolve()
            if not is_within(target, dir_path):
                return tool_result("Path traversal blocked.", is_error=True)
            md_content = f"# {fetched['title']}\n- Source: {fetched['url']}\n\n{fetched['text']}\n"
            target.write_text(md_content, encoding="utf-8")
            STORE.add_artifact(fetched["title"][:60], str(target), "MD", f"Fetched from {fetched['url']}")
            fetched["saved_path"] = str(target)

        STORE.append_audit("web_fetch", f"{fetched['url']} ({len(fetched['text'])} chars)")
        return tool_result(json.dumps(fetched, ensure_ascii=False, indent=2), build_state_payload(STORE, {
            "preview": {
                "path": fetched.get("saved_path", fetched["url"]),
                "name": Path(fetched["saved_path"]).name if fetched.get("saved_path") else "web-fetch.md",
                "total_lines": len(fetched["text"].splitlines()),
                "text": fetched["text"],
            }
        }))

    if name == "cowork_connector_manage":
        action = args.get("action", "list")
        items = STORE.get_connectors(include_secrets=True)
        if action == "add":
            cname = (args.get("name") or "").strip()
            curl = (args.get("url") or "").strip()
            if not cname or not curl:
                return tool_result("name and url are required.", is_error=True)
            items = [c for c in items if c.get("name", "").lower() != cname.lower()]
            items.append({
                "name": cname,
                "service": args.get("service", "webhook"),
                "url": curl,
                "token": (args.get("token") or "").strip(),
            })
            STORE.save_connectors(items)
            STORE.append_audit("connector_add", f"{cname} ({args.get('service', 'webhook')})")
        elif action == "remove":
            cname = (args.get("name") or "").strip()
            items = [c for c in items if c.get("name", "").lower() != cname.lower()]
            STORE.save_connectors(items)
            STORE.append_audit("connector_remove", cname)
        return tool_result(json.dumps(STORE.get_connectors(include_secrets=False), ensure_ascii=False), build_state_payload(STORE))

    if name == "cowork_connector_send":
        cname = (args.get("name") or "").strip()
        items = STORE.get_connectors(include_secrets=True)
        conn = next((c for c in items if c.get("name", "").lower() == cname.lower()), None)
        if not conn:
            return tool_result(f"Connector not found: {cname}", is_error=True)
        try:
            res = call_connector(conn, method=args.get("method", "POST"), payload=args.get("payload", {}))
            STORE.append_audit("connector_send", f"{cname} -> HTTP {res['status']}")
            return tool_result(json.dumps(res, ensure_ascii=False, indent=2), build_state_payload(STORE, {"connector_result": res}))
        except Exception as e:
            return tool_result(f"Connector call failed: {e}", is_error=True)

    if name == "cowork_office_create":
        dir_path, err = resolve_dir_arg(args, sess)
        if err:
            return tool_result(err, is_error=True)
        mount = find_covering_mount(dir_path, sess)
        if not mount or mount.get("permission_mode") == "read_only":
            return tool_result("Folder must be mounted in manual_approve or auto mode.", is_error=True)
        rel_name = (args.get("filename") or "").strip().lstrip("/\\")
        if not rel_name:
            return tool_result("filename is required.", is_error=True)
        target = (dir_path / rel_name).resolve()
        if not is_within(target, dir_path):
            return tool_result("Path traversal blocked.", is_error=True)
        try:
            create_office_file(
                target,
                kind=args.get("kind", "docx"),
                title=args.get("title", target.stem),
                content=args.get("content", ""),
                rows=args.get("rows"),
            )
            STORE.add_artifact(args.get("title") or target.name, str(target), args.get("kind", "docx").upper(), "Office deliverable")
            STORE.append_audit("office_create", f"Created {target.name}")
            return tool_result(json.dumps({"ok": True, "path": str(target)}, ensure_ascii=False), build_state_payload(STORE, {"view": "artifacts"}))
        except Exception as e:
            return tool_result(f"Office creation failed: {e}", is_error=True)

    if name == "cowork_browser_run":
        url = (args.get("url") or "").strip()
        steps = args.get("steps") or []
        headless = bool(args.get("headless", True))
        save_shot = bool(args.get("save_screenshot", True))
        shot_path = None
        if save_shot:
            base_dir = Path(args["directory_path"]).resolve() if args.get("directory_path") else (Path(sess["active_path"]) if sess.get("active_path") else (STORE.data_dir / "screenshots"))
            shot_path = base_dir / "_COWORK_BROWSER.png"
        try:
            res = run_browser_session(
                url=url,
                steps=steps,
                profile_dir=STORE.data_dir / "browser_profile",
                screenshot_path=shot_path,
                headless=headless,
            )
            STORE.append_audit("browser_run", f"{res.get('url', url)} ({len(res.get('steps', []))} steps)")
            return tool_result(json.dumps(res, ensure_ascii=False, indent=2), build_state_payload(STORE, {"browser_result": res}))
        except Exception as e:
            return tool_result(f"Browser run failed: {e}", is_error=True)

    if name == "cowork_computer_use":
        act = args.get("action", "screenshot")
        shot_path = None
        if act == "screenshot":
            base_dir = Path(args["directory_path"]).resolve() if args.get("directory_path") else (Path(sess["active_path"]) if sess.get("active_path") else (STORE.data_dir / "screenshots"))
            shot_path = base_dir / "_COWORK_SCREEN.png"
        try:
            res = run_computer_action(
                action=act,
                x=args.get("x"),
                y=args.get("y"),
                text=args.get("text", ""),
                keys=args.get("keys"),
                window_title=args.get("window_title", ""),
                scroll_amount=int(args.get("scroll_amount") or 0),
                screenshot_path=shot_path,
            )
            STORE.append_audit("computer_use", f"{act}")
            return tool_result(json.dumps(res, ensure_ascii=False, indent=2), build_state_payload(STORE, {"computer_result": res}))
        except Exception as e:
            return tool_result(f"Computer use failed: {e}", is_error=True)

    if name == "cowork_schedule_manage":
        action = args.get("action", "list")
        items = STORE.get_schedules()
        if action == "add":
            sname = (args.get("name") or "").strip() or f"Task {len(items) + 1}"
            wf = args.get("workflow", "brief")
            cron_expr = (args.get("cron") or "").strip()
            interval_m = int(args.get("interval_minutes") or 60)
            nxt = compute_next_run(cron_expr, interval_m)
            sid = f"sched_{int(time.time())}_{len(items)}"
            items.append({
                "id": sid,
                "name": sname,
                "workflow": wf,
                "cron": cron_expr,
                "interval_minutes": interval_m,
                "directory_path": (args.get("directory_path") or sess.get("active_path", "")).strip(),
                "target": (args.get("target") or "").strip(),
                "notify_connector": (args.get("notify_connector") or "").strip(),
                "enabled": True,
                "last_run": "",
                "next_run_ts": nxt,
                "next_run": time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt)),
            })
            STORE.save_schedules(items)
            STORE.append_audit("schedule_add", f"{sname} ({wf})")
        elif action == "toggle":
            sid = args.get("id", "")
            for s in items:
                if s.get("id") == sid:
                    s["enabled"] = not s.get("enabled", True)
            STORE.save_schedules(items)
        elif action == "remove":
            sid = args.get("id", "")
            items = [s for s in items if s.get("id") != sid]
            STORE.save_schedules(items)
            STORE.append_audit("schedule_remove", sid)
        elif action == "run_now":
            sid = args.get("id", "")
            target_s = next((s for s in items if s.get("id") == sid), None)
            if not target_s:
                return tool_result(f"Schedule not found: {sid}", is_error=True)
            out = _execute_schedule_item(target_s)
            STORE.save_schedules(items)
            return tool_result(json.dumps(out, ensure_ascii=False, indent=2), build_state_payload(STORE))
        return tool_result(json.dumps(STORE.get_schedules(), ensure_ascii=False, indent=2), build_state_payload(STORE))

    if name == "cowork_plugins_list":
        plugins = discover_installed_plugins(PLUGIN_ROOT.parent)
        return tool_result(json.dumps(plugins, ensure_ascii=False, indent=2), build_state_payload(STORE))

    if name == "cowork_oauth_connect":
        svc = (args.get("service") or "webhook").lower()
        cname = (args.get("name") or f"{svc}-oauth").strip()
        client_id = (args.get("client_id") or "").strip()
        token = (args.get("token") or "").strip()
        curl = (args.get("url") or "").strip()
        preset = OAUTH_PRESETS.get(svc, {})
        auth_url = build_oauth_authorize_url(svc, client_id) if (client_id and svc in OAUTH_PRESETS) else preset.get("login_url", "")
        if args.get("open_browser") and auth_url:
            run_browser_session(url=auth_url, headless=False)
        if token or curl:
            items = STORE.get_connectors(include_secrets=True)
            items = [c for c in items if c.get("name", "").lower() != cname.lower()]
            items.append({
                "name": cname,
                "service": svc,
                "url": curl or preset.get("auth_url", "https://api.example.com"),
                "token": token,
            })
            STORE.save_connectors(items)
            STORE.append_audit("oauth_connect", f"{cname} ({svc})")
        res = {"service": svc, "name": cname, "authorize_url": auth_url, "hint": preset.get("setup_hint", "")}
        return tool_result(json.dumps(res, ensure_ascii=False, indent=2), build_state_payload(STORE, {"oauth_result": res}))

    return tool_result(f"Unknown tool: {name}", is_error=True)


_SCHED_LOCK = threading.Lock()


def _execute_schedule_item(item: dict) -> dict:
    wf = item.get("workflow", "brief")
    dp = item.get("directory_path", "")
    target = item.get("target", "")
    if wf in ("triage", "brief", "dedupe", "index"):
        res = handle_call_tool("cowork_run_workflow", {"workflow": wf, "directory_path": dp})
    elif wf == "web_fetch":
        res = handle_call_tool("cowork_web_fetch", {"url": target, "directory_path": dp, "save_as": "_COWORK_WEB.md" if dp else ""})
    elif wf == "sandbox_exec":
        res = handle_call_tool("cowork_sandbox_exec", {"command": target, "directory_path": dp})
    else:
        res = tool_result(f"Unknown scheduled workflow: {wf}", is_error=True)
    now = time.time()
    item["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    nxt = compute_next_run(item.get("cron", ""), int(item.get("interval_minutes") or 60))
    item["next_run_ts"] = nxt
    item["next_run"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(nxt))
    summary = res.get("content", [{}])[0].get("text", "")[:500]
    conn_name = (item.get("notify_connector") or "").strip()
    if conn_name:
        try:
            handle_call_tool("cowork_connector_send", {"name": conn_name, "payload": {"text": f"[Cowork Schedule: {item.get('name')}]\n{summary}"}})
        except Exception:
            pass
    return {"id": item.get("id"), "ok": not res.get("isError", False), "summary": summary}


def _scheduler_loop():
    while True:
        time.sleep(15)
        try:
            with _SCHED_LOCK:
                items = STORE.get_schedules()
                now = time.time()
                changed = False
                for item in items:
                    if item.get("enabled", True) and now >= float(item.get("next_run_ts", 0)):
                        _execute_schedule_item(item)
                        changed = True
                if changed:
                    STORE.save_schedules(items)
        except Exception:
            pass


def main():
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        try:
            req = json.loads(line)
        except Exception:
            continue
        method = req.get("method")
        msg_id = req.get("id")
        if method == "initialize":
            send_response({
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}, "resources": {}},
                    "serverInfo": {"name": "gemini-cowork", "version": "2.2.0"},
                },
            })
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send_response({"jsonrpc": "2.0", "id": msg_id, "result": handle_list_tools()})
        elif method == "tools/call":
            params = req.get("params", {})
            res = handle_call_tool(params.get("name"), params.get("arguments", {}))
            send_response({"jsonrpc": "2.0", "id": msg_id, "result": res})
        elif method == "resources/list":
            send_response({"jsonrpc": "2.0", "id": msg_id, "result": handle_list_resources()})
        elif method == "resources/read":
            uri = (req.get("params") or {}).get("uri", "")
            res = handle_read_resource(uri)
            if res is None:
                send_response({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": f"Resource not found: {uri}"}})
            else:
                send_response({"jsonrpc": "2.0", "id": msg_id, "result": res})
        else:
            if msg_id is not None:
                send_response({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": "Method not found"}})


if __name__ == "__main__":
    main()
