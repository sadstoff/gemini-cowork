import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import server  # noqa: E402

HTML_PATH = ROOT / "ui" / "cowork.html"


class CoworkHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            html = HTML_PATH.read_bytes() if HTML_PATH.exists() else b"<h1>Missing ui/cowork.html</h1>"
            return self._send(200, "text/html; charset=utf-8", html)
        return self._send(404, "text/plain", b"Not Found")

    def do_POST(self):
        if self.path != "/api/tool":
            return self._send(404, "application/json", b'{"error":"Not Found"}')
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        try:
            req = json.loads(raw)
            res = server.handle_call_tool(req.get("name", ""), req.get("arguments", {}))
            return self._send(200, "application/json; charset=utf-8", json.dumps(res, ensure_ascii=False).encode("utf-8"))
        except Exception as e:
            return self._send(500, "application/json", json.dumps({"isError": True, "content": [{"type": "text", "text": str(e)}]}).encode("utf-8"))


def main(port: int = 4321, open_browser: bool = True):
    threading.Thread(target=server._scheduler_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", port), CoworkHandler)
    url = f"http://127.0.0.1:{port}"
    print(f"Gemini Cowork Standalone running at {url}")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
