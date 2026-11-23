import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PLUGIN_ROOT / "server.py"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
import server
from cowork_core import CoworkStore


def run_rpc(requests: list[dict], data_dir: str) -> list[dict]:
    proc = subprocess.Popen(
        [sys.executable, str(SERVER_PATH), f"--data_dir={data_dir}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    payload = "\n".join(json.dumps(r, ensure_ascii=False) for r in requests) + "\n"
    out, err = proc.communicate(payload, timeout=20)
    if err:
        raise AssertionError(f"Server stderr not empty: {err}")
    return [json.loads(line) for line in out.strip().splitlines() if line.strip()]


class TestCoworkServer(unittest.TestCase):
    def test_initialize_tools_and_ui_resource(self):
        with tempfile.TemporaryDirectory() as td:
            res = run_rpc(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": "ui://cowork/app.html"}},
                ],
                td,
            )
            self.assertEqual(res[0]["result"]["capabilities"], {"tools": {}, "resources": {}})
            tools = {t["name"]: t for t in res[1]["result"]["tools"]}
            self.assertIn("cowork_open_ui", tools)
            self.assertIn("cowork_run_workflow", tools)
            self.assertIn("com.google.antigravity/ui", tools["cowork_open_ui"]["_meta"])
            self.assertEqual(res[2]["result"]["resources"][0]["mimeType"], "text/html;profile=mcp-app")
            html = res[3]["result"]["contents"][0]["text"]
            self.assertIn('<div id="app"></div>', html)

    def test_security_blocks_system_and_drive_roots(self):
        with tempfile.TemporaryDirectory() as td:
            bad_root = "C:\\" if os.name == "nt" else "/"
            res = run_rpc(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "cowork_mount_folder", "arguments": {"directory_path": bad_root}}},
                ],
                td,
            )
            self.assertTrue(res[0]["result"].get("isError"))

    def test_permission_modes_read_only_and_manual_approve(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as ws:
            (Path(ws) / "sample.txt").write_text("hello", encoding="utf-8")
            res = run_rpc(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "cowork_mount_folder", "arguments": {"directory_path": ws, "permission_mode": "read_only"}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cowork_write_artifact", "arguments": {"directory_path": ws, "filename": "OUT.md", "content": "x"}}},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cowork_app_set_permission", "arguments": {"directory_path": ws, "permission_mode": "manual_approve"}}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "cowork_organize_files", "arguments": {"directory_path": ws, "strategy": "by_category", "dry_run": False, "approved": False}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "cowork_organize_files", "arguments": {"directory_path": ws, "strategy": "by_category", "dry_run": False, "approved": True}}},
                    {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "cowork_rollback", "arguments": {}}},
                ],
                td,
            )
            self.assertTrue(res[1]["result"].get("isError"))
            self.assertTrue(res[3]["result"].get("isError"))
            self.assertEqual(res[4]["result"]["structuredContent"]["last_batch"]["moved"], 1)
            self.assertEqual(res[5]["result"]["structuredContent"]["last_rollback"]["restored"], 1)
            self.assertTrue((Path(ws) / "sample.txt").exists())
            self.assertFalse((Path(ws) / "Documents").exists())

    def test_all_strategies_workflows_and_cyrillic_search(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as ws:
            ws_p = Path(ws)
            (ws_p / "Отчёт (1).txt").write_text("дубликат контент", encoding="utf-8")
            (ws_p / "Копия (2).txt").write_text("дубликат контент", encoding="utf-8")
            (ws_p / "My  Note.md").write_text("# Заголовок\nТест", encoding="utf-8")
            stale_f = ws_p / "old.log"
            stale_f.write_text("stale", encoding="utf-8")
            old_ts = time.time() - 120 * 86400
            os.utime(stale_f, (old_ts, old_ts))

            reqs = [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "cowork_mount_folder", "arguments": {"directory_path": ws, "permission_mode": "auto"}}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cowork_search_files", "arguments": {"directory_path": ws, "query": "*Отчёт*", "content_query": "дубликат"}}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "cowork_organize_files", "arguments": {"directory_path": ws, "strategy": "normalize_names", "dry_run": False}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "cowork_rollback", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "cowork_organize_files", "arguments": {"directory_path": ws, "strategy": "dedupe_review", "dry_run": False}}},
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "cowork_rollback", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "cowork_run_workflow", "arguments": {"workflow": "brief", "directory_path": ws}}},
                {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "cowork_run_workflow", "arguments": {"workflow": "index", "directory_path": ws}}},
                {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "cowork_app_save_instructions", "arguments": {"instructions": "Keep reports concise."}}},
                {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "cowork_agent_turn", "arguments": {"prompt": "Что занимает больше всего места?", "directory_path": ws}}},
                {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "cowork_connector_manage", "arguments": {"action": "add", "name": "test-hook", "service": "slack", "url": "https://hooks.slack.com/services/T0/B0/XX"}}},
                {"jsonrpc": "2.0", "id": 12, "method": "tools/call", "params": {"name": "cowork_connector_manage", "arguments": {"action": "remove", "name": "test-hook"}}},
            ]
            res = run_rpc(reqs, td)
            for item in res:
                self.assertFalse(item["result"].get("isError"), f"Unexpected error: {item}")
            self.assertEqual(res[1]["result"]["structuredContent"]["count"], 1)
            self.assertTrue((ws_p / "_COWORK_BRIEF.md").exists())
            self.assertTrue((ws_p / "_COWORK_INDEX.md").exists())
            self.assertTrue((ws_p / "_COWORK_SUMMARY.csv").exists())
            self.assertEqual(res[8]["result"]["structuredContent"]["instructions"], "Keep reports concise.")
            self.assertEqual(len(res[9]["result"]["structuredContent"]["messages"]), 2)
            self.assertEqual(len(res[10]["result"]["structuredContent"]["connectors"]), 1)
            self.assertEqual(len(res[11]["result"]["structuredContent"]["connectors"]), 0)

    def test_web_fetch_blocks_localhost_and_sandbox_exec(self):
        with tempfile.TemporaryDirectory() as td:
            res = run_rpc(
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "cowork_web_fetch", "arguments": {"url": "http://127.0.0.1:8080/secret"}}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "cowork_web_fetch", "arguments": {"url": "file:///C:/Windows/win.ini"}}},
                ],
                td,
            )
            self.assertTrue(res[0]["result"].get("isError"))
            self.assertTrue(res[1]["result"].get("isError"))

    def test_v2_office_ocr_sandbox_cron_browser_computer_oauth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ws"
            root.mkdir()
            old_store = server.STORE
            server.STORE = CoworkStore(Path(tmp) / "data")
            self.addCleanup(setattr, server, "STORE", old_store)
            server.handle_call_tool("cowork_mount_folder", {"directory_path": str(root), "permission_mode": "auto"})

            # 1. Office creation & reading (.docx, .xlsx, .pptx)
            r_doc = server.handle_call_tool("cowork_office_create", {
                "directory_path": str(root),
                "filename": "contract.docx",
                "kind": "docx",
                "title": "Service Agreement",
                "content": "## Terms\n- Payment within 14 days",
            })
            self.assertFalse(r_doc.get("isError", False))
            r_read_doc = server.handle_call_tool("cowork_read_file", {"file_path": str(root / "contract.docx")})
            self.assertIn("Payment within 14 days", r_read_doc["content"][0]["text"])

            r_xls = server.handle_call_tool("cowork_office_create", {
                "directory_path": str(root),
                "filename": "budget.xlsx",
                "kind": "xlsx",
                "title": "Budget",
                "rows": [["Item", "Cost"], ["Server", 120]],
            })
            self.assertFalse(r_xls.get("isError", False))
            r_read_xls = server.handle_call_tool("cowork_read_file", {"file_path": str(root / "budget.xlsx")})
            self.assertIn("Server", r_read_xls["content"][0]["text"])

            r_ppt = server.handle_call_tool("cowork_office_create", {
                "directory_path": str(root),
                "filename": "deck.pptx",
                "kind": "pptx",
                "title": "Q4 Deck",
                "content": "# Agenda\nRevenue growth",
            })
            self.assertFalse(r_ppt.get("isError", False))
            r_read_ppt = server.handle_call_tool("cowork_read_file", {"file_path": str(root / "deck.pptx")})
            self.assertIn("Revenue growth", r_read_ppt["content"][0]["text"])

            # 2. Out-of-the-box builtin sandbox
            r_sb = server.handle_call_tool("cowork_sandbox_exec", {
                "command": "echo COWORK_BUILTIN_OK",
                "backend": "builtin",
            })
            self.assertFalse(r_sb.get("isError", False))
            sb_data = json.loads(r_sb["content"][0]["text"])
            self.assertEqual(sb_data["backend"], "builtin")
            self.assertIn("COWORK_BUILTIN_OK", sb_data["stdout"])

            # 3. Scheduled tasks (add, run_now, toggle, remove)
            r_sched = server.handle_call_tool("cowork_schedule_manage", {
                "action": "add",
                "name": "Test Brief",
                "workflow": "brief",
                "cron": "0 20 * * *",
                "directory_path": str(root),
            })
            scheds = json.loads(r_sched["content"][0]["text"])
            self.assertEqual(len(scheds), 1)
            sid = scheds[0]["id"]
            r_run = server.handle_call_tool("cowork_schedule_manage", {"action": "run_now", "id": sid})
            self.assertFalse(r_run.get("isError", False))
            self.assertTrue((root / "_COWORK_BRIEF.md").exists())
            server.handle_call_tool("cowork_schedule_manage", {"action": "remove", "id": sid})

            # 4. Browser Use (CDP on data: URL with self-correcting click fallback & DOM extract)
            r_br = server.handle_call_tool("cowork_browser_run", {
                "url": "data:text/html,<title>CoworkTest</title><button onclick=\"document.getElementById('out').innerText='CLICKED_OK'\">Go</button><div id='out'>INIT</div>",
                "steps": [{"action": "click", "selector": "#missing-btn", "text": "Go"}, {"action": "wait", "seconds": 0.3}],
                "headless": True,
                "directory_path": str(root),
            })
            self.assertFalse(r_br.get("isError", False), r_br["content"][0]["text"])
            br_data = json.loads(r_br["content"][0]["text"])
            self.assertIn("CLICKED_OK", br_data["text"])
            self.assertEqual(br_data["steps"][1]["strategy"], "self_corrected_text")
            self.assertTrue((root / "_COWORK_BROWSER.png").exists())

            # 5. Image OCR (generate PNG with text and read via cowork_read_file)
            from PIL import Image, ImageDraw
            img_p = root / "note.png"
            im = Image.new("RGB", (320, 90), color=(255, 255, 255))
            ImageDraw.Draw(im).text((20, 30), "COWORK 2026", fill=(0, 0, 0))
            im.save(str(img_p))
            r_ocr = server.handle_call_tool("cowork_read_file", {"file_path": str(img_p)})
            self.assertFalse(r_ocr.get("isError", False))
            self.assertIn("Image OCR", r_ocr["content"][0]["text"])
            self.assertTrue(r_ocr["structuredContent"]["preview"].get("image_data_url", "").startswith("data:image/png;base64,"))

            # 5b. Compound NL Command Bar pipeline (search + sandbox math + artifact)
            (root / "receipt_oct_1.txt").write_text("Coffee 150.50\nTaxi 300", encoding="utf-8")
            (root / "receipt_oct_2.txt").write_text("Lunch 249.50", encoding="utf-8")
            r_nl = server.handle_call_tool("cowork_agent_turn", {
                "prompt": "Найди все чеки за октябрь, посчитай сумму и отправь в Slack",
                "directory_path": str(root),
            })
            self.assertFalse(r_nl.get("isError", False))
            self.assertIn("700.0", r_nl["content"][0]["text"])
            self.assertTrue((root / "_COWORK_NL_REPORT.md").exists())

            # 6. Computer Use (list_windows & screenshot)
            r_win = server.handle_call_tool("cowork_computer_use", {"action": "list_windows"})
            self.assertFalse(r_win.get("isError", False))
            r_shot = server.handle_call_tool("cowork_computer_use", {"action": "screenshot", "directory_path": str(root)})
            if r_shot.get("isError"):
                self.assertIn("screen capture unavailable", r_shot["content"][0]["text"].lower())
            else:
                self.assertTrue((root / "_COWORK_SCREEN.png").exists())

            # 6. Plugins Hub & OAuth URL builder
            r_pl = server.handle_call_tool("cowork_plugins_list", {})
            self.assertFalse(r_pl.get("isError", False))
            r_oa = server.handle_call_tool("cowork_oauth_connect", {"service": "google", "client_id": "test-cid"})
            self.assertIn("accounts.google.com", r_oa["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
