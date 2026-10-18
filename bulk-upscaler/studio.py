"""Upscale Studio: the designed dashboard front end for Bulk Upscaler.

Runs a small local server (127.0.0.1 only) that serves ui/index.html and
exposes the upscaler as a JSON API, then opens it in its own app window.

Run:  python studio.py
"""

import json
import mimetypes
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image

from upscaler import core

APP_DIR = Path(__file__).resolve().parent
UI_DIR = APP_DIR / "ui"
TOKEN = secrets.token_urlsafe(16)
UPLOAD_DIR = Path(tempfile.mkdtemp(prefix="upscale-studio-"))
MAX_UPLOAD = 200 * 1024 * 1024

state = {
    "images": {},  # id -> {"name", "path", "w", "h", "status", "output", "error"}
    "running": False,
    "installing": False,
    "out_dir": str(Path.home() / "Pictures" / "Upscaled"),
}
lock = threading.Lock()


def public_image(img_id, img):
    return {"id": img_id, "name": img["name"], "w": img["w"], "h": img["h"],
            "status": img["status"], "error": img["error"],
            "output": Path(img["output"]).name if img["output"] else None}


def snapshot():
    with lock:
        return {
            "engine": core.engine_binary() is not None,
            "installing": state["installing"],
            "running": state["running"],
            "out_dir": state["out_dir"],
            "max_batch": core.MAX_BATCH,
            "models": [{"key": k, "label": m.label} for k, m in core.MODELS.items()],
            "images": [public_image(k, v) for k, v in state["images"].items()],
        }


def run_batch(ids, opts):
    with lock:
        items = [(i, state["images"][i]) for i in ids if i in state["images"]]
        out_dir = state["out_dir"]
    binary = core.engine_binary()
    for img_id, img in items:
        with lock:
            img["status"], img["error"], img["output"] = "working", None, None
        try:
            dest = core.upscale_one(img["path"], out_dir, opts["scale"], opts["model"],
                                    opts["fmt"], opts["dpi"], binary, name=img["name"])
            with lock:
                img["status"], img["output"] = "done", str(dest)
        except Exception as e:
            with lock:
                img["status"], img["error"] = "error", str(e)
    with lock:
        state["running"] = False


def run_python(args):
    """Run a helper Python script and capture its output, with no console flashing up.

    When launched via pythonw.exe there is no stdout, so use the python.exe beside it.
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        exe = exe.parent / "python.exe"
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    return subprocess.run([str(exe), *args], capture_output=True, text=True, **kwargs)


def install_engine():
    r = run_python([str(APP_DIR / "setup_engine.py")])
    with lock:
        state["installing"] = False
        state["install_error"] = None if r.returncode == 0 else (r.stderr or r.stdout)[-300:]


def pick_folder(initial):
    """Native folder picker, run in a child process so Tk stays off the server threads."""
    code = ("import tkinter as tk, sys; from tkinter import filedialog; r = tk.Tk(); r.withdraw(); "
            "r.attributes('-topmost', True); "
            "print(filedialog.askdirectory(initialdir=sys.argv[1], title='Choose where to save'))")
    return run_python(["-c", code, initial]).stdout.strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, cache=False):
        path = Path(path)
        if not path.is_file():
            return self.send_error(404)
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        if self.headers.get("X-Studio-Token") == TOKEN:
            return True
        self.send_json({"error": "unauthorized"}, 403)
        return False

    def read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > MAX_UPLOAD:
            raise ValueError("File too large")
        return self.rfile.read(n)

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/":
            html = (UI_DIR / "index.html").read_text(encoding="utf-8").replace("__TOKEN__", TOKEN)
            body = html.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        # Image routes are loaded by <img> tags, so they carry the token in the query string.
        q = parse_qs(url.query)
        if url.path.startswith("/img/") and q.get("t", [""])[0] == TOKEN:
            kind, _, img_id = url.path[len("/img/"):].partition("/")
            with lock:
                img = state["images"].get(img_id)
            if not img:
                return self.send_error(404)
            path = img["path"] if kind == "original" else img["output"]
            return self.send_file(path) if path else self.send_error(404)
        if url.path == "/api/status":
            if self.authorized():
                self.send_json(snapshot())
            return
        self.send_error(404)

    def do_POST(self):
        if not self.authorized():
            return
        url = urlparse(self.path)
        try:
            if url.path == "/api/upload":
                name = Path(unquote(parse_qs(url.query).get("name", ["image.png"])[0])).name
                with lock:
                    if len(state["images"]) >= core.MAX_BATCH:
                        return self.send_json({"error": f"Batch is full ({core.MAX_BATCH} max)."}, 400)
                if not core.is_supported(name):
                    return self.send_json({"error": f"{name}: use PNG, JPG or WEBP."}, 400)
                img_id = uuid.uuid4().hex[:10]
                path = UPLOAD_DIR / f"{img_id}{Path(name).suffix.lower()}"
                path.write_bytes(self.read_body())
                try:
                    with Image.open(path) as im:
                        w, h = im.size
                except Exception:
                    path.unlink(missing_ok=True)
                    return self.send_json({"error": f"{name} isn't a readable image."}, 400)
                with lock:
                    state["images"][img_id] = {"name": name, "path": str(path), "w": w, "h": h,
                                               "status": "ready", "output": None, "error": None}
                return self.send_json(snapshot())

            data = json.loads(self.read_body() or b"{}")
            if url.path == "/api/remove":
                with lock:
                    if not state["running"]:
                        for i in data.get("ids") or list(state["images"]):
                            img = state["images"].pop(i, None)
                            if img:
                                Path(img["path"]).unlink(missing_ok=True)
                return self.send_json(snapshot())

            if url.path == "/api/upscale":
                opts = {
                    "scale": int(data.get("scale", 4)),
                    "model": data.get("model", "photo"),
                    "fmt": "jpg" if data.get("fmt") == "jpg" else "png",
                    "dpi": 300 if data.get("dpi", True) else None,
                }
                if opts["scale"] not in (2, 3, 4) or opts["model"] not in core.MODELS:
                    return self.send_json({"error": "Bad settings"}, 400)
                with lock:
                    if state["running"]:
                        return self.send_json({"error": "Already running"}, 409)
                    ids = data.get("ids") or list(state["images"])
                    for i in ids:
                        if i in state["images"]:
                            state["images"][i]["status"] = "queued"
                    state["running"] = True
                threading.Thread(target=run_batch, args=(ids, opts), daemon=True).start()
                return self.send_json(snapshot())

            if url.path == "/api/pick-folder":
                chosen = pick_folder(state["out_dir"])
                if chosen:
                    with lock:
                        state["out_dir"] = chosen
                return self.send_json(snapshot())

            if url.path == "/api/open-folder":
                d = Path(state["out_dir"])
                d.mkdir(parents=True, exist_ok=True)
                core.open_folder(d)
                return self.send_json({"ok": True})

            if url.path == "/api/install-engine":
                with lock:
                    if not state["installing"]:
                        state["installing"] = True
                        threading.Thread(target=install_engine, daemon=True).start()
                return self.send_json(snapshot())
        except Exception as e:
            return self.send_json({"error": str(e)}, 500)
        self.send_error(404)


def find_app_browser():
    """Find Edge or Chrome so the UI can open as a standalone app window."""
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"),
                     os.environ.get("LOCALAPPDATA")):
            if base:
                candidates += [Path(base) / "Microsoft/Edge/Application/msedge.exe",
                               Path(base) / "Google/Chrome/Application/chrome.exe"]
    elif sys.platform == "darwin":
        candidates += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                       Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
    else:
        for name in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
            if shutil.which(name):
                candidates.append(Path(shutil.which(name)))
    return next((c for c in candidates if c.is_file()), None)


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Upscale Studio running at {url}  (close this window to quit)")

    browser = find_app_browser()
    if browser and "--browser" not in sys.argv:
        profile = Path(tempfile.gettempdir()) / "upscale-studio-profile"
        window = subprocess.Popen([str(browser), f"--app={url}", "--window-size=1440,900",
                                   f"--user-data-dir={profile}", "--no-first-run"])

        def quit_with_window():
            started = time.monotonic()
            window.wait()
            # If the browser handed off to an already-running copy it exits right away;
            # in that case keep serving instead of quitting.
            if time.monotonic() - started > 5:
                server.shutdown()

        threading.Thread(target=quit_with_window, daemon=True).start()
    else:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)


if __name__ == "__main__":
    main()
