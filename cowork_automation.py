import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from cronsim import CronSim


def _find_browser_exe() -> str:
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for c in candidates:
        if Path(c).exists():
            return c
    for name in ("msedge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    return ""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _cdp_execute(ws_url: str, steps: list[dict], screenshot_path: Path | None = None) -> dict:
    import websockets

    msg_id = 0
    results = []

    async with websockets.connect(ws_url, max_size=20 * 1024 * 1024) as ws:
        async def cdp(method: str, params: dict | None = None):
            nonlocal msg_id
            msg_id += 1
            cid = msg_id
            await ws.send(json.dumps({"id": cid, "method": method, "params": params or {}}))
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=25)
                data = json.loads(raw)
                if data.get("id") == cid:
                    if "error" in data:
                        raise RuntimeError(f"CDP {method} error: {data['error']}")
                    return data.get("result", {})

        await cdp("Page.enable")
        await cdp("Runtime.enable")

        for st in steps:
            act = (st.get("action") or "").lower()
            if act in ("goto", "open", "navigate"):
                url = st.get("url", "")
                await cdp("Page.navigate", {"url": url})
                await asyncio.sleep(float(st.get("wait_sec", 2.0)))
                results.append({"action": act, "url": url, "ok": True})
            elif act == "wait":
                sec = float(st.get("seconds", 1.0))
                await asyncio.sleep(sec)
                results.append({"action": "wait", "seconds": sec})
            elif act == "click":
                sel = st.get("selector", "")
                txt = st.get("text", "")
                js = (
                    f"(() => {{ const el = document.querySelector({json.dumps(sel)}); "
                    "if (!el) return 'NOT_FOUND'; el.scrollIntoView({block:'center'}); el.click(); return 'OK'; })()"
                    if sel
                    else f"(() => {{ const t = {json.dumps(txt)}.toLowerCase(); "
                    "const el = [...document.querySelectorAll('a,button,input[type=submit],[role=button]')]"
                    ".find(e => (e.innerText||e.value||'').toLowerCase().includes(t)); "
                    "if (!el) return 'NOT_FOUND'; el.scrollIntoView({block:'center'}); el.click(); return 'OK'; })()"
                )
                r = await cdp("Runtime.evaluate", {"expression": js, "returnByValue": True})
                val = r.get("result", {}).get("value")
                results.append({"action": "click", "target": sel or txt, "result": val})
            elif act == "type":
                sel = st.get("selector", "")
                val = st.get("text", "")
                js = (
                    f"(() => {{ const el = document.querySelector({json.dumps(sel)}); "
                    "if (!el) return 'NOT_FOUND'; el.focus(); el.value = "
                    f"{json.dumps(val)}; el.dispatchEvent(new Event('input', {{bubbles:true}})); "
                    "el.dispatchEvent(new Event('change', {{bubbles:true}})); return 'OK'; })()"
                )
                r = await cdp("Runtime.evaluate", {"expression": js, "returnByValue": True})
                results.append({"action": "type", "selector": sel, "result": r.get("result", {}).get("value")})
            elif act == "scroll":
                dy = int(st.get("y", 600))
                await cdp("Runtime.evaluate", {"expression": f"window.scrollBy(0, {dy})"})
                results.append({"action": "scroll", "y": dy})
            elif act == "eval":
                expr = st.get("script", "document.title")
                r = await cdp("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                results.append({"action": "eval", "value": r.get("result", {}).get("value")})

        info_res = await cdp(
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify({title: document.title, url: location.href, text: (document.body ? document.body.innerText : '').slice(0, 12000)})",
                "returnByValue": True,
            },
        )
        page_info = json.loads(info_res.get("result", {}).get("value") or "{}")

        saved_shot = ""
        if screenshot_path:
            shot = await cdp("Page.captureScreenshot", {"format": "png"})
            b64 = shot.get("data", "")
            if b64:
                screenshot_path.parent.mkdir(parents=True, exist_ok=True)
                screenshot_path.write_bytes(base64.b64decode(b64))
                saved_shot = str(screenshot_path)

    return {
        "title": page_info.get("title", ""),
        "url": page_info.get("url", ""),
        "text": page_info.get("text", ""),
        "steps": results,
        "screenshot": saved_shot,
    }


def run_browser_session(
    url: str = "",
    steps: list[dict] | None = None,
    profile_dir: Path | None = None,
    screenshot_path: Path | None = None,
    headless: bool = True,
) -> dict:
    exe = _find_browser_exe()
    if not exe:
        raise RuntimeError("No Chromium/Edge browser executable found on this system.")

    all_steps = list(steps or [])
    if url and (not all_steps or all_steps[0].get("action") not in ("goto", "open", "navigate")):
        all_steps.insert(0, {"action": "goto", "url": url, "wait_sec": 2.0})
    if not all_steps:
        raise ValueError("Provide a url or at least one browser step.")

    port = _free_port()
    prof = profile_dir or (Path.home() / ".cowork_browser_profile")
    prof.mkdir(parents=True, exist_ok=True)

    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={prof}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
    ]
    if headless:
        cmd.append("--headless=new")
    cmd.append("about:blank")

    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ws_url = ""
        for _ in range(30):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1) as resp:
                    targets = json.loads(resp.read().decode("utf-8"))
                    page = next((t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")), None)
                    if page:
                        ws_url = page["webSocketDebuggerUrl"]
                        break
            except Exception:
                time.sleep(0.25)
        if not ws_url:
            raise RuntimeError("Browser CDP debugger did not start in time.")
        return asyncio.run(_cdp_execute(ws_url, all_steps, screenshot_path=screenshot_path))
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def run_computer_action(
    action: str,
    x: int | None = None,
    y: int | None = None,
    text: str = "",
    keys: list[str] | None = None,
    window_title: str = "",
    clicks: int = 1,
    scroll_amount: int = 0,
    screenshot_path: Path | None = None,
) -> dict:
    import pyautogui
    import pygetwindow as gw
    from PIL import ImageGrab

    pyautogui.FAILSAFE = True
    act = (action or "screenshot").lower()

    if act == "list_windows":
        titles = [t for t in gw.getAllTitles() if t and t.strip()]
        return {"action": "list_windows", "windows": titles[:60]}

    if act == "focus_window":
        matches = gw.getWindowsWithTitle(window_title)
        if not matches:
            return {"action": "focus_window", "ok": False, "error": f"Window not found: {window_title}"}
        win = matches[0]
        try:
            if win.isMinimized:
                win.restore()
            win.activate()
        except Exception as e:
            return {"action": "focus_window", "ok": False, "error": str(e)}
        return {"action": "focus_window", "ok": True, "title": win.title}

    if act == "screenshot":
        from PIL import Image
        try:
            img = ImageGrab.grab()
        except Exception as e1:
            try:
                import dxcam
                cam = dxcam.create()
                frame = cam.grab()
                if frame is None:
                    raise RuntimeError("dxcam returned empty frame")
                img = Image.fromarray(frame)
            except Exception as e2:
                raise RuntimeError(f"Desktop screen capture unavailable in current session ({e1}; {e2})")
        out_path = screenshot_path or (Path.cwd() / "_COWORK_SCREEN.png")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path))
        return {"action": "screenshot", "path": str(out_path), "width": img.width, "height": img.height}

    if act == "find_text":
        import numpy as np
        from cowork_office import _get_ocr
        img = ImageGrab.grab().convert("RGB")
        ocr = _get_ocr()
        res, _ = ocr(np.array(img))
        needle = (text or "").strip().lower()
        hits = []
        for item in res or []:
            box, label, conf = item[0], str(item[1]), float(item[2])
            if not needle or needle in label.lower():
                cx = int(sum(p[0] for p in box) / 4)
                cy = int(sum(p[1] for p in box) / 4)
                hits.append({"text": label, "x": cx, "y": cy, "confidence": round(conf, 3)})
        return {"action": "find_text", "query": text, "matches": hits[:30]}

    if act in ("click", "double_click"):
        if x is None or y is None:
            raise ValueError("x and y coordinates are required for click.")
        pyautogui.click(x=int(x), y=int(y), clicks=2 if act == "double_click" else int(clicks))
        return {"action": act, "x": x, "y": y}

    if act == "move":
        if x is None or y is None:
            raise ValueError("x and y coordinates are required for move.")
        pyautogui.moveTo(int(x), int(y), duration=0.2)
        return {"action": "move", "x": x, "y": y}

    if act == "type":
        pyautogui.write(text, interval=0.02)
        return {"action": "type", "chars": len(text)}

    if act == "hotkey":
        if not keys:
            raise ValueError("keys list is required for hotkey.")
        pyautogui.hotkey(*keys)
        return {"action": "hotkey", "keys": keys}

    if act == "scroll":
        pyautogui.scroll(int(scroll_amount))
        return {"action": "scroll", "amount": scroll_amount}

    raise ValueError(f"Unknown computer action: {action}")


def compute_next_run(cron_expr: str = "", interval_minutes: int = 0) -> float:
    now = time.time()
    if cron_expr and cron_expr.strip():
        it = CronSim(cron_expr.strip(), datetime.fromtimestamp(now))
        return next(it).timestamp()
    mins = max(1, int(interval_minutes or 60))
    return now + mins * 60


def discover_installed_plugins(plugins_root: Path) -> list[dict]:
    if not plugins_root.exists():
        return []
    found = []
    for pdir in sorted(plugins_root.iterdir()):
        if not pdir.is_dir():
            continue
        pjson = pdir / "plugin.json"
        meta = {}
        if pjson.exists():
            try:
                meta = json.loads(pjson.read_text(encoding="utf-8"))
            except Exception:
                pass
        skills_dir = pdir / "skills"
        skills = [s.name for s in skills_dir.iterdir() if s.is_dir()] if skills_dir.exists() else []
        found.append({
            "id": pdir.name,
            "name": meta.get("name", pdir.name),
            "version": meta.get("version", ""),
            "description": meta.get("description", "")[:140],
            "skills": skills[:10],
            "has_mcp": (pdir / "mcp_config.json").exists(),
        })
    return found


OAUTH_PRESETS = {
    "google": {
        "auth_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "scopes": "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/drive.readonly https://www.googleapis.com/auth/calendar.readonly",
        "setup_hint": "Sign in with Google or connect Antigravity Google Workspace plugins (gdocs, gsheets, gslides).",
    },
    "slack": {
        "auth_url": "https://slack.com/oauth/v2/authorize",
        "scopes": "chat:write,channels:read",
        "setup_hint": "Use Slack Incoming Webhook URL or OAuth Bot Token (xoxb-...).",
    },
    "notion": {
        "auth_url": "https://api.notion.com/v1/oauth/authorize",
        "scopes": "",
        "setup_hint": "Use Notion Internal Integration Token (ntn_...) with https://api.notion.com/v1/...",
    },
    "github": {
        "auth_url": "https://github.com/login/oauth/authorize",
        "scopes": "repo,read:user",
        "setup_hint": "Connect GitHub OAuth or Personal Access Token.",
    },
}


def build_oauth_authorize_url(service: str, client_id: str, redirect_uri: str = "http://127.0.0.1:8765/callback") -> str:
    svc = service.lower()
    preset = OAUTH_PRESETS.get(svc)
    if not preset:
        raise ValueError(f"No OAuth preset for service: {service}")
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
    }
    if preset.get("scopes"):
        params["scope"] = preset["scopes"]
    if svc == "google":
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    return f"{preset['auth_url']}?{urllib.parse.urlencode(params)}"
