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


if __name__ == "__main__":
    unittest.main()
