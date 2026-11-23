import csv
import fnmatch
import hashlib
import html.parser
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path

CATEGORIES = {
    "Documents": {".pdf", ".doc", ".docx", ".txt", ".md", ".rtf", ".odt", ".xls", ".xlsx", ".csv", ".ppt", ".pptx"},
    "Images": {".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".bmp", ".ico", ".heic"},
    "Video": {".mp4", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv"},
    "Audio": {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"},
    "Archives": {".zip", ".rar", ".7z", ".tar", ".gz"},
    "Code": {".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".json", ".yaml", ".yml", ".ps1", ".bat", ".sh", ".sql"},
    "Executables": {".exe", ".msi", ".msix", ".cmd"},
}

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".xml", ".html", ".css",
    ".js", ".ts", ".jsx", ".tsx", ".py", ".ps1", ".bat", ".sh", ".sql", ".ini",
    ".toml", ".log", ".env", ".gitignore", ".cfg", ".conf",
}

FORBIDDEN_ROOTS = {
    "c:\\windows",
    "c:\\program files",
    "c:\\program files (x86)",
    "c:\\programdata",
    "/system",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/var",
}

TASK_PRESETS = [
    {
        "id": "triage",
        "title": "Folder Triage & Cleanup",
        "description": "Scan folder, organize files by category, archive stale items (>90d), and generate an index.",
        "prompt": "Mount and scan my active Cowork folder, create a step-by-step plan, preview dry-run organization by category, ask for my approval, and generate _COWORK_INDEX.md.",
    },
    {
        "id": "brief",
        "title": "Synthesize Research Brief",
        "description": "Read text/markdown/CSV files in the workspace and produce a structured executive brief artifact.",
        "prompt": "Search and read the key documents in my active Cowork folder, then create a structured summary brief artifact using cowork_write_artifact.",
    },
    {
        "id": "dedupe",
        "title": "Exact Duplicate Audit",
        "description": "Find byte-identical SHA-256 duplicates and safely isolate extra copies into _Duplicates_Review.",
        "prompt": "Scan my active Cowork folder for exact SHA-256 duplicates, preview moving extra copies to _Duplicates_Review, and wait for my approval.",
    },
    {
        "id": "routine",
        "title": "Daily Workspace Routine",
        "description": "Set up a recurring cleanup & summary briefing workflow via Antigravity Automations.",
        "prompt": "Help me set up a recurring Cowork routine for my active folder (use /schedule or Automations).",
    },
]


class CoworkStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.session_file = self.data_dir / "current_session.json"
        self.history_file = self.data_dir / "action_history.json"
        self.rollback_file = self.data_dir / "rollback_batches.json"
        self.last_scan_file = self.data_dir / "last_scan.json"
        self.artifacts_file = self.data_dir / "artifacts.json"
        self.instructions_file = self.data_dir / "instructions.md"
        self.connectors_file = self.data_dir / "connectors.json"

    def load_json(self, path: Path, default):
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return default

    def save_json(self, path: Path, data):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def append_audit(self, action: str, details: str, status: str = "done"):
        entries = self.load_json(self.history_file, [])
        entries.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": status,
            "action": action,
            "details": details,
        })
        self.save_json(self.history_file, entries[-100:])

    def get_session(self) -> dict:
        sess = self.load_json(self.session_file, {"mounts": [], "active_path": "", "plan": None, "tasks": [], "messages": []})
        sess.setdefault("mounts", [])
        sess.setdefault("tasks", [])
        sess.setdefault("messages", [])
        if "active_path" not in sess:
            sess["active_path"] = sess["mounts"][-1]["path"] if sess["mounts"] else ""
        return sess

    def save_session(self, sess: dict):
        self.save_json(self.session_file, sess)

    def get_instructions(self) -> str:
        if self.instructions_file.exists():
            try:
                return self.instructions_file.read_text(encoding="utf-8")
            except Exception:
                pass
        return ""

    def save_instructions(self, text: str):
        self.instructions_file.write_text(text, encoding="utf-8")

    def add_artifact(self, title: str, path: str, kind: str, summary: str = ""):
        items = self.load_json(self.artifacts_file, [])
        items = [a for a in items if a.get("path", "").lower() != path.lower()]
        items.append({
            "id": f"art_{int(time.time())}",
            "title": title,
            "path": path,
            "kind": kind,
            "summary": summary,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        self.save_json(self.artifacts_file, items[-40:])

    def get_connectors(self, include_secrets: bool = False) -> list[dict]:
        items = self.load_json(self.connectors_file, [])
        if include_secrets:
            return items
        return [
            {
                "name": c.get("name", ""),
                "service": c.get("service", "webhook"),
                "url": c.get("url", ""),
                "has_token": bool(c.get("token")),
            }
            for c in items
        ]

    def save_connectors(self, items: list[dict]):
        self.save_json(self.connectors_file, items)


def is_safe_path(target: Path) -> bool:
    try:
        resolved = target.resolve()
        norm = str(resolved).lower().rstrip("\\/")
    except Exception:
        return False
    if not norm or (os.name == "nt" and len(norm) <= 3) or norm == "/":
        return False
    for bad in FORBIDDEN_ROOTS:
        if norm == bad or norm.startswith(bad + "\\") or norm.startswith(bad + "/"):
            return False
    return True


def is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def categorize(ext: str) -> str:
    ext = ext.lower()
    for cat, exts in CATEGORIES.items():
        if ext in exts:
            return cat
    return "Other"


def sha256_file(path: Path, chunk_size: int = 131072) -> str:
    size = path.stat().st_size
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if size <= 16 * 1024 * 1024:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        else:
            # Fast fingerprint for large media/archives: size + head + middle + tail
            h.update(str(size).encode())
            h.update(f.read(65536))
            f.seek(size // 2)
            h.update(f.read(65536))
            f.seek(max(0, size - 65536))
            h.update(f.read(65536))
    return h.hexdigest()


def get_quick_folders() -> list[dict]:
    home = Path.home()
    candidates = [
        ("Downloads", home / "Downloads"),
        ("Desktop", home / "Desktop"),
        ("Documents", home / "Documents"),
        ("Pictures", home / "Pictures"),
    ]
    return [
        {"label": label, "path": str(p.resolve())}
        for label, p in candidates
        if p.exists() and p.is_dir()
    ]


def pick_folder_dialog() -> str:
    if os.name != "nt":
        return ""
    ps_script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = 'Select a folder to mount in Cowork'; "
        "$f.UseDescriptionForTitle = $true; "
        "if ($f.ShowDialog() -eq 'OK') { Write-Output $f.SelectedPath }"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return res.stdout.strip()
    except Exception:
        return ""


def find_covering_mount(target: Path, sess: dict) -> dict | None:
    for m in sess.get("mounts", []):
        if is_within(target, Path(m["path"])):
            return m
    return None


def resolve_dir_arg(args: dict, sess: dict) -> tuple[Path | None, str | None]:
    raw = (args.get("directory_path") or "").strip() or sess.get("active_path", "")
    if not raw:
        return None, "No directory specified and no active mounted folder."
    p = Path(raw)
    if not p.exists() or not p.is_dir():
        return None, f"Directory not found: {p}"
    if not is_safe_path(p):
        return None, f"Blocked unsafe system path: {p}"
    return p.resolve(), None


def list_directory(dir_path: Path, rel_subpath: str = "", limit: int = 200) -> dict:
    target = (dir_path / rel_subpath).resolve() if rel_subpath else dir_path.resolve()
    if not is_within(target, dir_path) or not target.exists() or not target.is_dir():
        raise ValueError(f"Invalid subfolder: {rel_subpath}")

    items = list(target.iterdir())
    items.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
    entries = []
    for p in items[:limit]:
        try:
            st = p.stat()
        except OSError:
            continue
        is_dir = p.is_dir()
        rel = str(p.relative_to(dir_path)).replace("\\", "/")
        entries.append({
            "name": p.name,
            "rel_path": rel,
            "full_path": str(p.resolve()),
            "is_dir": is_dir,
            "category": "Folder" if is_dir else categorize(p.suffix),
            "size_kb": 0 if is_dir else round(st.st_size / 1024, 2),
            "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "previewable": (not is_dir) and (p.suffix.lower() in TEXT_EXTENSIONS) and st.st_size <= 512 * 1024,
        })

    rel_curr = "" if target == dir_path.resolve() else str(target.relative_to(dir_path)).replace("\\", "/")
    return {
        "root": str(dir_path.resolve()),
        "rel_path": rel_curr,
        "total_entries": len(items),
        "entries": entries,
    }


def _iter_files(dir_path: Path, recursive: bool, max_files: int = 15000):
    count = 0
    truncated = False
    if not recursive:
        with os.scandir(dir_path) as it:
            for entry in it:
                if entry.is_file(follow_symlinks=False) and not entry.name.startswith("_COWORK_"):
                    yield Path(entry.path), entry.stat(follow_symlinks=False)
                    count += 1
                    if count >= max_files:
                        truncated = True
                        break
        return truncated

    stack = [dir_path]
    while stack:
        curr = stack.pop()
        try:
            with os.scandir(curr) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False) and not entry.name.startswith("_COWORK_"):
                        yield Path(entry.path), entry.stat(follow_symlinks=False)
                        count += 1
                        if count >= max_files:
                            return True
        except OSError:
            continue
    return truncated


def scan_folder(dir_path: Path, recursive: bool, stale_days: int) -> dict:
    now = time.time()
    stale_cutoff = now - stale_days * 86400
    cat_counts = defaultdict(int)
    cat_bytes = defaultdict(int)
    size_groups = defaultdict(list)
    files_info = []
    stale_files = []

    gen = _iter_files(dir_path, recursive)
    truncated = False
    try:
        while True:
            p, st = next(gen)
            sz = st.st_size
            cat = categorize(p.suffix)
            cat_counts[cat] += 1
            cat_bytes[cat] += sz
            size_groups[sz].append((p, st.st_mtime))
            rel = str(p.relative_to(dir_path)).replace("\\", "/")
            files_info.append({
                "rel_path": rel,
                "name": p.name,
                "category": cat,
                "size_kb": round(sz / 1024, 2),
                "mtime": time.strftime("%Y-%m-%d", time.localtime(st.st_mtime)),
            })
            if st.st_mtime < stale_cutoff:
                stale_files.append(rel)
    except StopIteration as stop:
        truncated = bool(stop.value)

    duplicates = []
    for sz, items in size_groups.items():
        if sz > 0 and len(items) > 1:
            by_hash = defaultdict(list)
            for p, mtime in items[:40]:
                try:
                    by_hash[sha256_file(p)].append({
                        "rel_path": str(p.relative_to(dir_path)).replace("\\", "/"),
                        "mtime": mtime,
                    })
                except Exception:
                    pass
            for _, grp in by_hash.items():
                if len(grp) > 1:
                    grp_sorted = sorted(grp, key=lambda x: x["mtime"], reverse=True)
                    duplicates.append({
                        "size_kb": round(sz / 1024, 2),
                        "files": [x["rel_path"] for x in grp_sorted],
                    })

    top_largest = sorted(files_info, key=lambda x: x["size_kb"], reverse=True)[:10]
    return {
        "directory": str(dir_path.resolve()),
        "recursive": recursive,
        "stale_days": stale_days,
        "scanned_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_files": len(files_info),
        "total_mb": round(sum(cat_bytes.values()) / (1024 * 1024), 2),
        "categories": {
            k: {"count": cat_counts[k], "mb": round(cat_bytes[k] / (1024 * 1024), 2)}
            for k in sorted(cat_counts)
        },
        "stale_count": len(stale_files),
        "stale_sample": stale_files[:15],
        "duplicate_groups": duplicates[:15],
        "largest_files": top_largest,
        "all_files": files_info,
    }


def normalize_filename(name: str) -> str:
    stem, ext = os.path.splitext(name)
    cleaned = re.sub(r"\s+", "_", stem.strip())
    cleaned = re.sub(r"[()]+", "", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return (cleaned or stem) + ext.lower()


def plan_moves(dir_path: Path, strategy: str, stale_days: int, custom_moves: list) -> list[tuple[Path, Path]]:
    planned = []
    if strategy == "custom":
        for m in custom_moves or []:
            src = (dir_path / m["src"]).resolve()
            dst = (dir_path / m["dst"]).resolve()
            if src.exists() and src.is_file() and is_within(src, dir_path) and is_within(dst, dir_path):
                planned.append((src, dst))
    elif strategy == "archive_stale":
        cutoff = time.time() - stale_days * 86400
        for p in dir_path.iterdir():
            if p.is_file() and not p.name.startswith("_COWORK_") and p.stat().st_mtime < cutoff:
                ym = time.strftime("%Y-%m", time.localtime(p.stat().st_mtime))
                planned.append((p, dir_path / "_Archive" / ym / p.name))
    elif strategy == "by_date":
        for p in dir_path.iterdir():
            if p.is_file() and not p.name.startswith("_COWORK_"):
                st = p.stat()
                yr = time.strftime("%Y", time.localtime(st.st_mtime))
                ym = time.strftime("%Y-%m", time.localtime(st.st_mtime))
                planned.append((p, dir_path / yr / ym / p.name))
    elif strategy == "dedupe_review":
        scan = scan_folder(dir_path, recursive=False, stale_days=stale_days)
        for grp in scan["duplicate_groups"]:
            for dup_rel in grp["files"][1:]:
                src = dir_path / dup_rel
                if src.exists() and src.is_file():
                    planned.append((src, dir_path / "_Duplicates_Review" / src.name))
    elif strategy == "normalize_names":
        for p in dir_path.iterdir():
            if p.is_file() and not p.name.startswith("_COWORK_"):
                new_name = normalize_filename(p.name)
                if new_name != p.name:
                    planned.append((p, dir_path / new_name))
    else:  # by_category
        for p in dir_path.iterdir():
            if p.is_file() and not p.name.startswith("_COWORK_"):
                cat = categorize(p.suffix)
                planned.append((p, dir_path / cat / p.name))
    return planned


def detect_sandbox_backends() -> dict:
    has_docker = bool(shutil.which("docker"))
    has_wsl = bool(shutil.which("wsl") or (os.name == "nt" and Path(r"C:\Windows\System32\wsl.exe").exists()))
    return {"docker": has_docker, "wsl": has_wsl}


def _to_wsl_path(win_path: Path) -> str:
    s = str(win_path.resolve()).replace("\\", "/")
    if len(s) >= 2 and s[1] == ":":
        drive = s[0].lower()
        return f"/mnt/{drive}{s[2:]}"
    return s


def run_in_sandbox(command: str, work_dir: Path | None = None, backend: str = "auto", copy_workspace: bool = True) -> dict:
    cmd = (command or "").strip()
    if not cmd:
        raise ValueError("command is required.")
    backends = detect_sandbox_backends()
    chosen = backend
    if chosen == "auto":
        chosen = "docker" if backends["docker"] else ("wsl" if backends["wsl"] else "none")
    if chosen == "none" or not backends.get(chosen):
        raise RuntimeError("No local sandbox backend available (neither docker nor wsl is installed).")

    if chosen == "docker":
        docker_cmd = ["docker", "run", "--rm", "--network=none", "--memory=512m", "--cpus=1"]
        if work_dir:
            docker_cmd.extend(["-v", f"{work_dir.resolve()}:/workspace:ro", "-w", "/workspace"])
        docker_cmd.extend(["python:3.12-slim", "sh", "-lc", cmd])
        res = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=45, encoding="utf-8", errors="replace")
        return {
            "backend": "docker",
            "isolated": True,
            "network": "none",
            "returncode": res.returncode,
            "stdout": res.stdout[-8000:],
            "stderr": res.stderr[-4000:],
        }

    # WSL disposable /tmp/cowork-vm-XXXXXX sandbox
    wsl_src = _to_wsl_path(work_dir) if (work_dir and copy_workspace) else ""
    copy_step = (
        f'if [ -d "{wsl_src}" ]; then cp -r "{wsl_src}/." "$SB/" 2>/dev/null || true; fi; '
        if wsl_src
        else ""
    )
    script = (
        'set -e; '
        'SB=$(mktemp -d /tmp/cowork-vm-XXXXXX); '
        'trap "rm -rf \\"$SB\\"" EXIT; '
        'cd "$SB"; '
        f'{copy_step}'
        f'{cmd}'
    )
    res = subprocess.run(["wsl.exe", "bash", "-lc", script], capture_output=True, text=True, timeout=45, encoding="utf-8", errors="replace")
    return {
        "backend": "wsl",
        "isolated": True,
        "network": "host-shared",
        "returncode": res.returncode,
        "stdout": res.stdout[-8000:],
        "stderr": res.stderr[-4000:],
    }


class _TextExtractor(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self._skip = 0
        self.title = ""
        self._in_title = False
        self.chunks: list[str] = []

    def handle_starttag(self, tag, attrs):
        t = tag.lower()
        if t in ("script", "style", "noscript", "svg"):
            self._skip += 1
        elif t == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        t = tag.lower()
        if t in ("script", "style", "noscript", "svg") and self._skip > 0:
            self._skip -= 1
        elif t == "title":
            self._in_title = False
        elif t in ("p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"):
            self.chunks.append("\n")

    def handle_data(self, data):
        if self._skip > 0:
            return
        if self._in_title:
            self.title += data
        txt = " ".join(data.split())
        if txt:
            self.chunks.append(txt + " ")


def _validate_public_http_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Only http:// or https:// URLs are allowed.")
    host = parsed.hostname
    if host.lower() in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("Localhost/private addresses are blocked.")
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(host))
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"Private/internal IP blocked: {ip}")
    except socket.gaierror:
        pass
    return parsed.geturl()


def web_fetch_to_text(url: str, max_chars: int = 20000) -> dict:
    clean_url = _validate_public_http_url(url)
    req = urllib.request.Request(clean_url, headers={"User-Agent": "Cowork-Antigravity/1.0.2"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read(1024 * 1024)
        charset = resp.headers.get_content_charset() or "utf-8"
        text_raw = raw.decode(charset, errors="replace")

    if "html" in ctype.lower() or "<html" in text_raw[:500].lower():
        parser = _TextExtractor()
        parser.feed(text_raw)
        body = re.sub(r"\n{3,}", "\n\n", "".join(parser.chunks)).strip()
        title = parser.title.strip() or clean_url
    else:
        body = text_raw.strip()
        title = clean_url

    return {
        "url": clean_url,
        "title": title,
        "content_type": ctype,
        "text": body[:max_chars],
    }


def call_connector(connector: dict, method: str = "POST", payload: dict | None = None) -> dict:
    url = _validate_public_http_url(connector.get("url", ""))
    headers = {"User-Agent": "Cowork-Antigravity/1.0.2", "Content-Type": "application/json"}
    token = (connector.get("token") or "").strip()
    service = connector.get("service", "webhook")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if service == "notion":
        headers.setdefault("Notion-Version", "2022-06-28")
    data = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if method.upper() != "GET" else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read(256 * 1024).decode("utf-8", errors="replace")
        return {"status": resp.status, "body": body[:4000]}


def build_state_payload(store: CoworkStore, extra: dict | None = None) -> dict:
    sess = store.get_session()
    batches = store.load_json(store.rollback_file, [])
    history = store.load_json(store.history_file, [])
    last_scan = store.load_json(store.last_scan_file, None)
    artifacts = store.load_json(store.artifacts_file, [])

    active_listing = None
    active_raw = sess.get("active_path", "")
    if active_raw:
        ap = Path(active_raw)
        if ap.exists() and ap.is_dir() and is_safe_path(ap):
            try:
                active_listing = list_directory(ap)
            except Exception:
                active_listing = None

    state = {
        "mounts": sess.get("mounts", []),
        "active_path": active_raw,
        "plan": sess.get("plan"),
        "messages": sess.get("messages", [])[-30:],
        "tasks": list(reversed(sess.get("tasks", [])[-15:])),
        "task_presets": TASK_PRESETS,
        "instructions": store.get_instructions(),
        "connectors": store.get_connectors(include_secrets=False),
        "sandbox": detect_sandbox_backends(),
        "artifacts": list(reversed(artifacts)),
        "quick_folders": get_quick_folders(),
        "active_listing": active_listing,
        "rollback_batches": [
            {
                "batch_id": b["batch_id"],
                "directory": b["directory"],
                "count": len(b.get("moves", [])),
                "strategy": b.get("strategy", "organize"),
                "created_at": b.get("created_at", ""),
            }
            for b in reversed(batches)
        ],
        "audit_log": list(reversed(history[-30:])),
        "last_scan": last_scan,
    }
    if extra:
        state.update(extra)
    return state
