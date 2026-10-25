#!/usr/bin/env python3
"""The Conclave app: a local web page for bringing Claude Code projects and conversations to the council.

`conclave --app` (or `conclave-app`) starts a small server on 127.0.0.1 and opens it in your browser. Pick a project
and one of its Claude Code conversations, ask a question, and watch Claude, Gemini and Codex work live. The server only
answers requests that carry the token embedded in the page it serves, and only for a localhost Host header.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import secrets
import shlex
import shutil
import struct
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import conclave as core
import conclave_context as context

DEFAULT_PORT = 8765
AGENT_LABEL = "io.github.shrestha-droid.conclave"
STATE_FILE = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "conclave" / "app.json"


# ---------------------------------------------------------------- runs

class Run:
    """One council round started from the page, with the events the page has been sent so far."""

    def __init__(self, project: Path | None, conversation):
        self.id = uuid.uuid4().hex[:12]
        self.project = project
        self.conversation = conversation
        self.events: list[dict] = []
        self.changed = threading.Condition()
        self.control = core.Control()
        self.result: core.Round | None = None
        self.finished = False

    def emit(self, event: dict) -> None:
        with self.changed:
            self.events.append(event)
            self.changed.notify_all()

    def close(self) -> None:
        with self.changed:
            self.finished = True
            self.changed.notify_all()


class WebView:
    """Receives a round's progress from conclave.deliberate() and turns it into events for the page."""

    def __init__(self, run: Run):
        self.run = run
        self.sent: dict[str, float] = {}

    def set_phase(self, phase: str) -> None:
        self.run.emit({"type": "phase", "phase": phase})

    def start(self, name: str, stage: str, activity: str = "", preview: str = "") -> None:
        self.run.emit({"type": "start", "name": name, "stage": stage, "activity": activity, "preview": preview})

    def watch(self, name: str):
        def update(stream: core.Stream) -> None:
            now = time.monotonic()
            if now - self.sent.get(name, 0) < 0.08:  # about 12 updates a second is plenty for reading along
                return
            self.sent[name] = now
            self.run.emit({"type": "stream", "name": name, "text": stream.live, "activity": stream.activity})
        return update

    def finish(self, name: str, status: str, stage: str, preview: str = "") -> None:
        self.run.emit({"type": "finish", "name": name, "status": status, "stage": stage, "preview": preview})

    def tokens(self, name: str, tokens_in: int, tokens_out: int) -> None:
        self.run.emit({"type": "tokens", "name": name, "in": tokens_in, "out": tokens_out})

    def done(self) -> None:
        pass


def outcome(result: core.Round) -> dict:
    return {
        "final": result.final,
        "agreement": result.agreement,
        "confidence": result.confidence,
        "brief": result.brief,
        "opinions": result.opinions,
        "failed": result.failed,
        "errors": [{"provider": p, "headline": h, "message": m, "fix": core.fix_for(p, m)} for p, h, m in result.errors],
        "meta": core.round_meta(result),
        "sessions": result.sessions,
        "tokens": [result.tokens_in, result.tokens_out],
        "cost": result.cost,
        "seconds": result.seconds,
    }


# ---------------------------------------------------------------- server

class App:
    def __init__(self, selected: dict[str, str | None], timeout: int, save_sessions: bool = True):
        self.selected = selected
        self.timeout = timeout
        self.save_sessions = save_sessions
        self.token = secrets.token_urlsafe(24)
        self.runs: dict[str, Run] = {}
        self.server: ThreadingHTTPServer | None = None

    def info(self) -> dict:
        return {
            "version": core.__version__,
            "cwd": str(Path.cwd()),
            "theme": core.THEME,
            "themes": core.THEMES,
            "timeout": self.timeout,
            "saveSessions": self.save_sessions,
            "providers": [
                {"id": name, "label": core.LABELS[name], "role": core.ROLES[name], "cli": core.PROVIDERS[name],
                 "installed": bool(shutil.which(core.PROVIDERS[name])), "model": self.selected[name],
                 "install": core.INSTALL[name]}
                for name in core.PROVIDERS
            ],
        }

    def projects(self) -> list[dict]:
        listed = [{"path": folder, "name": Path(folder).name or folder, "when": context.ago(updated), "count": total,
                   "exists": Path(folder).is_dir()}
                  for folder, updated, total in context.projects()]
        return [item for item in listed if item["exists"]]

    def conversations(self, folder: str) -> dict:
        project = Path(folder).expanduser().resolve()
        if not project.is_dir():
            raise ValueError(f"{folder} is not a folder")
        return {
            "project": str(project),
            "conversations": [{"id": c.id, "title": c.title, "when": c.when, "messages": c.messages,
                               "errors": c.errors, "branch": c.branch}
                              for c in context.conversations(project)],
        }

    def start_run(self, body: dict) -> Run:
        question = str(body.get("question") or "").strip()
        if not question:
            raise ValueError("Ask a question first")
        project = None
        if body.get("project"):
            project = Path(str(body["project"])).expanduser().resolve()
            if not project.is_dir():
                raise ValueError(f"{project} is not a folder")
        conversation = None
        if body.get("conversation"):
            conversation = context.find(project, str(body["conversation"]))
            if not conversation:
                raise ValueError("That conversation is no longer available")
        selected = dict(self.selected)
        for name, model in (body.get("models") or {}).items():
            if name in selected and isinstance(model, str):
                selected[name] = model.strip() or self.selected[name]
        timeout = int(body.get("timeout") or self.timeout)
        history = [(str(q), str(a)) for q, a in (body.get("history") or [])][-core.MEMORY:]
        save_sessions = bool(body.get("saveSessions", self.save_sessions))
        run = Run(project, conversation)
        self.runs[run.id] = run

        def work() -> None:
            try:
                task = core.compose(question, project, conversation, history)
                result = core.deliberate(task, selected, timeout, WebView(run), cwd=project, control=run.control,
                                         title=question, save_sessions=save_sessions)
                result.request = question
                run.result = result
                run.emit({"type": "done", "result": outcome(result)})
            except core.Cancelled:
                run.emit({"type": "cancelled"})
            except Exception as exc:  # anything unexpected still reaches the page instead of hanging it
                run.emit({"type": "failed", "message": f"{type(exc).__name__}: {exc}"})
            finally:
                run.close()

        threading.Thread(target=work, daemon=True).start()
        return run

    def resume(self, run: Run, provider: str) -> dict:
        """Open a model's saved session from this round in a Terminal window (macOS), or return the command."""
        provider = core.ALIASES.get(provider, provider)
        command = (run.result.sessions if run.result else {}).get(provider)
        if not command:
            raise ValueError("That session wasn't saved")
        opened = False
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = command.replace("\\", "\\\\").replace('"', '\\"')
            try:
                subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "{script}"',
                                "-e", 'tell application "Terminal" to activate'], check=True, capture_output=True, timeout=15)
                opened = True
            except (OSError, subprocess.SubprocessError):
                pass
        return {"command": command, "opened": opened}

    def handoff(self, run: Run) -> dict:
        if not run.result or not run.result.final:
            raise ValueError("There is no verdict to hand back yet")
        path, command = context.handoff(run.project or Path.cwd(), run.conversation, run.result.final)
        return {"path": str(path), "command": command}


class Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "Conclave"

    def log_message(self, *_args) -> None:
        pass

    # -- plumbing

    def allowed_host(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")

    def authorized(self, query: dict) -> bool:
        token = self.headers.get("X-Conclave-Token") or (query.get("token") or [""])[0]
        return secrets.compare_digest(token, self.app.token)

    def send(self, status: int, payload, kind: str = "application/json") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def route(self, method: str) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self.allowed_host():
            return self.send(403, {"error": "Conclave only answers on localhost"})
        if method == "GET" and url.path == "/":
            page = PAGE.replace("__TOKEN__", self.app.token).replace("__VERSION__", core.__version__)
            return self.send(200, page.encode(), "text/html; charset=utf-8")
        if method == "GET" and url.path == "/icon.png":
            return self.send(200, ICON_64, "image/png")
        if not url.path.startswith("/api/") or not self.authorized(query):
            return self.send(403 if url.path.startswith("/api/") else 404, {"error": "not found"})
        parts = url.path.strip("/").split("/")[1:]
        try:
            if method == "GET" and parts == ["ping"]:
                return self.send(200, {"ok": True})
            if method == "GET" and parts == ["info"]:
                return self.send(200, self.app.info())
            if method == "GET" and parts == ["projects"]:
                return self.send(200, self.app.projects())
            if method == "GET" and parts == ["conversations"]:
                return self.send(200, self.app.conversations((query.get("project") or [str(Path.cwd())])[0]))
            if method == "POST" and parts == ["runs"]:
                return self.send(200, {"id": self.app.start_run(self.body()).id})
            if len(parts) >= 2 and parts[0] == "runs":
                run = self.app.runs.get(parts[1])
                if not run:
                    return self.send(404, {"error": "unknown run"})
                if method == "GET" and parts[2:] == ["events"]:
                    return self.events(run)
                if method == "POST" and parts[2:] == ["cancel"]:
                    run.control.cancel()
                    return self.send(200, {"ok": True})
                if method == "POST" and parts[2:] == ["handoff"]:
                    return self.send(200, self.app.handoff(run))
                if method == "POST" and parts[2:] == ["continue"]:
                    return self.send(200, self.app.resume(run, str(self.body().get("provider") or "")))
            if method == "POST" and parts == ["shutdown"]:
                self.send(200, {"ok": True})
                core.stop_all()
                threading.Thread(target=self.app.server.shutdown, daemon=True).start()
                return
        except ValueError as exc:
            return self.send(400, {"error": str(exc)})
        except OSError as exc:
            return self.send(500, {"error": str(exc)})
        self.send(404, {"error": "not found"})

    def events(self, run: Run) -> None:
        """Server-Sent Events: replay what the page missed, then stream new events until the round ends."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        sent = int(self.headers.get("Last-Event-ID") or 0)
        try:
            while True:
                with run.changed:
                    if sent >= len(run.events) and not run.finished:
                        run.changed.wait(timeout=15)
                    batch = run.events[sent:]
                    over = run.finished and sent + len(batch) >= len(run.events)
                if not batch:
                    self.wfile.write(b": still here\n\n")
                for event in batch:
                    sent += 1
                    self.wfile.write(f"id: {sent}\ndata: {json.dumps(event)}\n\n".encode())
                self.wfile.flush()
                if over:
                    return
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:
        self.route("GET")

    def do_POST(self) -> None:
        self.route("POST")


def serve(app: App, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"app": app})
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)  # the usual port is taken: use any free one
    server.daemon_threads = True
    app.server = server
    return server


def running_instance() -> str | None:
    """The address of a Conclave app that is already running, if there is one."""
    try:
        saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        url = f"http://127.0.0.1:{int(saved['port'])}/"
        request = Request(url + "api/ping", headers={"X-Conclave-Token": saved["token"]})
        with urlopen(request, timeout=1) as response:
            return url if response.status == 200 else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="conclave-app", description="Open the Conclave app in your browser.")
    parser.add_argument("--port", type=int, default=0, help=f"port to listen on (default: {DEFAULT_PORT}, or any free port)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    parser.add_argument("--timeout", type=int, default=300, help="seconds allowed per model call (default: 300)")
    args = parser.parse_args(argv)

    existing = running_instance()
    if existing:
        print(f"Conclave is already running at {existing}")
        if not args.no_browser:
            webbrowser.open(existing)
        return 0
    try:
        settings = core.load_config(core.CONFIG_PATH)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    core.apply_theme(settings.theme or "violet")
    app = App({name: settings.models.get(name) for name in core.PROVIDERS}, args.timeout, settings.save_sessions)
    server = serve(app, args.port or DEFAULT_PORT)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"port": port, "token": app.token, "pid": os.getpid()}), encoding="utf-8")
        STATE_FILE.chmod(0o600)
    except OSError:
        pass
    print(f"✦ Conclave is running at {url}  (Ctrl+C or the Quit button stops it)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        core.stop_all()
        server.server_close()
        try:
            if json.loads(STATE_FILE.read_text(encoding="utf-8")).get("pid") == os.getpid():
                STATE_FILE.unlink()
        except (OSError, ValueError):
            pass
    print("✦ The council is adjourned.")
    return 0


# ---------------------------------------------------------------- macOS app bundle and icon

def icon_png(size: int) -> bytes:
    """Conclave's icon, drawn pixel by pixel: a violet four-point sparkle on a dark rounded square."""
    def mix(a, b, t):
        t = min(max(t, 0.0), 1.0)
        return tuple(round(x + (y - x) * t) for x, y in zip(a, b))

    radius = 0.64 ** (2 / 3)
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        v = (y + 0.5) / size * 2 - 1
        for x in range(size):
            u = (x + 0.5) / size * 2 - 1
            edge = (abs(u) ** 5 + abs(v) ** 5) ** 0.2  # squircle background
            alpha = min(max((0.92 - edge) * size / 2, 0.0), 1.0)
            color = mix((0x1C, 0x1E, 0x33), (0x08, 0x09, 0x10), (v + 1) / 2)
            star = abs(u) ** (2 / 3) + abs(v) ** (2 / 3)  # astroid: a sparkle with curved sides
            color = mix(color, (0xA5, 0xA3, 0xFF), max(0.0, 1 - star / (radius * 1.9)) ** 2 * 0.45)
            inside = (radius - star) * size * 0.6
            color = mix(color, mix((0xF2, 0xF1, 0xFF), (0x6F, 0x6C, 0xE0), (u + v + 2) / 4), inside)
            su, sv = (u - 0.52) / 0.2, (v + 0.52) / 0.2  # a small companion sparkle, top right
            small = abs(su) ** (2 / 3) + abs(sv) ** (2 / 3)
            color = mix(color, (0xE4, 0xE3, 0xFF), (1 - small) * size * 0.12)
            row += bytes((*color, round(alpha * 255)))
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b"")


ICON_64 = icon_png(64)


def launch_agent() -> dict:
    """A login item that keeps the Conclave app running in the background at http://localhost:8765."""
    log = Path.home() / "Library" / "Logs" / "Conclave.log"
    return {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "--no-browser", "--port", str(DEFAULT_PORT)],
        # launchd starts programs with a minimal PATH; keep the one that finds claude, gemini and codex right now.
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", "")},
        "WorkingDirectory": str(Path.home()),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after a crash, not after the Quit button
        "ProcessType": "Interactive",
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }


def launchctl(*args: str) -> bool:
    try:
        return subprocess.run(["launchctl", *args], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def install_mac_app(destination: Path | None = None, agents: Path | None = None, start: bool = True) -> int:
    """Create Conclave.app and a login item that keeps Conclave running at http://localhost:8765."""
    if destination is None and sys.platform != "darwin":
        print("Conclave.app is for macOS. On this system, run `conclave --app` to open the app in your browser.")
        return 1
    destination = destination or Path.home() / "Applications"
    bundle = destination / "Conclave.app"
    contents = bundle / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)

    launcher = contents / "MacOS" / "Conclave"
    # Apps opened from Finder get a minimal PATH, so keep the one that finds claude, gemini and codex right now.
    launcher.write_text(
        "#!/bin/bash\n"
        f"export PATH={shlex.quote(os.environ.get('PATH', ''))}\n"
        f"cd {shlex.quote(str(Path.home()))}\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} \"$@\"\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    (contents / "Info.plist").write_bytes(plistlib.dumps({
        "CFBundleName": "Conclave",
        "CFBundleDisplayName": "Conclave",
        "CFBundleIdentifier": "io.github.shrestha-droid.conclave",
        "CFBundleExecutable": "Conclave",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": core.__version__,
        "CFBundleVersion": core.__version__,
        "CFBundleIconFile": "Conclave",
        "LSUIElement": True,  # no Dock icon; the browser tab is the window and it has a Quit button
        "LSMinimumSystemVersion": "11.0",
    }))

    print("✦ Drawing the icon…")
    (contents / "Resources" / "Conclave.png").write_bytes(icon_png(512))
    if shutil.which("iconutil"):
        iconset = contents / "Resources" / "Conclave.iconset"
        iconset.mkdir(exist_ok=True)
        for size in (16, 32, 128, 256, 512):
            (iconset / f"icon_{size}x{size}.png").write_bytes(icon_png(size))
            (iconset / f"icon_{size}x{size}@2x.png").write_bytes(icon_png(size * 2))
        try:
            subprocess.run(["iconutil", "-c", "icns", "-o", str(contents / "Resources" / "Conclave.icns"), str(iconset)],
                           check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            pass
        shutil.rmtree(iconset, ignore_errors=True)
    print(f"✦ Installed {bundle}")

    agents = agents or Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / f"{AGENT_LABEL}.plist"
    plist.write_bytes(plistlib.dumps(launch_agent()))
    if start:
        (Path.home() / "Library" / "Logs").mkdir(parents=True, exist_ok=True)
        domain = f"gui/{os.getuid()}"
        launchctl("bootout", domain, str(plist))  # replace an earlier version if one is loaded
        if launchctl("bootstrap", domain, str(plist)):
            for _ in range(40):
                if running_instance():
                    break
                time.sleep(0.25)
    print(f"✦ Conclave now starts when you log in and stays at http://localhost:{DEFAULT_PORT}. Bookmark it.")
    print("  Open Conclave.app from Spotlight, Launchpad or the Dock to bring it up in your browser.")
    print("  `conclave --uninstall-app` removes both.")
    return 0


def uninstall_mac_app(destination: Path | None = None, agents: Path | None = None, stop_now: bool = True) -> int:
    """Remove Conclave.app and the login item."""
    plist = (agents or Path.home() / "Library" / "LaunchAgents") / f"{AGENT_LABEL}.plist"
    if stop_now and plist.exists() and hasattr(os, "getuid"):
        launchctl("bootout", f"gui/{os.getuid()}", str(plist))
    plist.unlink(missing_ok=True)
    shutil.rmtree((destination or Path.home() / "Applications") / "Conclave.app", ignore_errors=True)
    print("✦ Removed Conclave.app and its login item. The `conclave` command is still installed.")
    return 0


# ---------------------------------------------------------------- the page

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Conclave</title>
<link rel="icon" href="/icon.png">
<style>
:root{--ink:#0A0C13;--panel:#0F1119;--panel2:#141725;--edge:#262938;--edge2:#343746;--accent:#A5A3FF;--muted:#6B6E85;
--text:#E8E8F5;--soft:#C9C8F5;--claude:#A5A3FF;--gemini:#8B88F0;--openai:#C2C0FF;--g1:#E4E3FF;--g2:#A5A3FF;--g3:#6F6CE0;
--good:#8BE9A8;--warn:#F0C878;--bad:#FF6B7A;--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,system-ui,sans-serif}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--ink);color:var(--text);font:14px/1.55 var(--sans)}
body{background:radial-gradient(1200px 600px at 75% -10%,color-mix(in srgb,var(--accent) 12%,transparent),transparent),var(--ink)}
button,input,textarea,select{font:inherit;color:inherit}
a{color:var(--accent)}
::selection{background:color-mix(in srgb,var(--accent) 40%,transparent)}
.app{display:grid;grid-template-columns:320px 1fr;grid-template-rows:auto 1fr;height:100vh}
header{grid-column:1/-1;display:flex;align-items:center;gap:18px;padding:14px 22px;border-bottom:1px solid var(--edge);
background:color-mix(in srgb,var(--ink) 80%,transparent);backdrop-filter:blur(8px)}
.logo{display:flex;align-items:center;gap:10px;font-weight:800;letter-spacing:.32em;font-size:15px}
.logo svg{width:26px;height:26px;filter:drop-shadow(0 0 10px color-mix(in srgb,var(--accent) 60%,transparent))}
.wordmark{background:linear-gradient(90deg,var(--g1),var(--g2),var(--g3));-webkit-background-clip:text;background-clip:text;color:transparent}
.tagline{color:var(--muted);font-size:12px;letter-spacing:.08em}
.spacer{flex:1}
.pills{display:flex;gap:8px}
.pill{display:flex;align-items:center;gap:7px;padding:4px 11px;border:1px solid var(--edge);border-radius:999px;font-size:12px;color:var(--soft);background:var(--panel)}
.pill i{width:7px;height:7px;border-radius:50%;background:var(--good);box-shadow:0 0 8px var(--good)}
.pill.missing i{background:var(--bad);box-shadow:0 0 8px var(--bad)}
.pill b{font-weight:700}
.iconbtn{border:1px solid var(--edge);background:var(--panel);border-radius:9px;padding:6px 11px;cursor:pointer;color:var(--soft)}
.iconbtn:hover{border-color:var(--accent);color:var(--text)}
aside{border-right:1px solid var(--edge);overflow:auto;padding:18px 14px;display:flex;flex-direction:column;gap:22px;background:color-mix(in srgb,var(--panel) 60%,transparent)}
aside h3{margin:0 0 8px 6px;font-size:11px;letter-spacing:.18em;color:var(--muted);font-weight:700;display:flex;justify-content:space-between}
.list{display:flex;flex-direction:column;gap:4px}
.item{padding:9px 11px;border-radius:10px;cursor:pointer;border:1px solid transparent;transition:.15s}
.item:hover{background:var(--panel2);border-color:var(--edge)}
.item.on{background:color-mix(in srgb,var(--accent) 12%,var(--panel2));border-color:color-mix(in srgb,var(--accent) 55%,transparent)}
.item .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .s{color:var(--muted);font-size:12px;display:flex;gap:8px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.err{color:var(--bad)}
.field{width:100%;padding:9px 11px;border-radius:10px;border:1px solid var(--edge);background:var(--panel);outline:none}
.field:focus{border-color:var(--accent)}
.empty{color:var(--muted);font-size:12px;padding:6px 11px}
main{overflow:auto;padding:22px 28px 60px}
.wrap{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:22px}
.composer{border:1px solid var(--edge);border-radius:18px;background:linear-gradient(180deg,var(--panel2),var(--panel));padding:16px;
box-shadow:0 20px 60px -30px color-mix(in srgb,var(--accent) 45%,transparent)}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:4px 10px;border-radius:999px;background:var(--panel);border:1px solid var(--edge);font-size:12px;color:var(--soft);max-width:100%}
.chip span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip.live{border-color:color-mix(in srgb,var(--accent) 60%,transparent);color:var(--text)}
.chip button{border:0;background:none;color:var(--muted);cursor:pointer;padding:0 0 0 2px}
textarea{width:100%;min-height:92px;resize:vertical;border:0;outline:none;background:transparent;font-size:16px;line-height:1.5}
textarea::placeholder{color:var(--muted)}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px}
.quick{border:1px dashed var(--edge2);background:none;border-radius:999px;padding:4px 11px;font-size:12px;color:var(--soft);cursor:pointer}
.quick:hover{border-color:var(--accent);color:var(--text)}
.go{margin-left:auto;border:0;border-radius:12px;padding:10px 18px;font-weight:700;cursor:pointer;color:var(--ink);
background:linear-gradient(90deg,var(--g1),var(--g2),var(--g3));box-shadow:0 8px 30px -8px var(--accent)}
.go:disabled{opacity:.5;cursor:default}
.go kbd{font:11px var(--mono);opacity:.7;margin-left:6px}
details.models{font-size:12px;color:var(--muted)}
details.models summary{cursor:pointer;list-style:none}
.modelgrid{display:grid;grid-template-columns:repeat(3,1fr) 110px;gap:8px;margin-top:8px}
.modelgrid label{display:flex;flex-direction:column;gap:4px}
.run{border:1px solid var(--edge);border-radius:20px;background:var(--panel);overflow:hidden;animation:rise .35s ease}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1}}
.runhead{padding:16px 20px;border-bottom:1px solid var(--edge);display:flex;gap:16px;align-items:flex-start}
.runhead h2{margin:0;font-size:16px;font-weight:650;flex:1;white-space:pre-wrap}
.clock{font:600 13px var(--mono);color:var(--accent);white-space:nowrap}
.tracker{display:flex;align-items:center;gap:12px;padding:12px 20px;font:700 11px var(--sans);letter-spacing:.16em;color:var(--muted)}
.tracker .bar{flex:1;height:2px;background:var(--edge)}
.tracker .bar.on{background:var(--accent)}
.tracker .ph.on{color:var(--text)}
.tracker .ph.now{color:var(--accent);text-shadow:0 0 12px var(--accent)}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:0 16px 16px}
.card{position:relative;border:1px solid var(--edge);border-radius:14px;background:var(--panel2);padding:14px;min-height:150px;display:flex;flex-direction:column;gap:8px;overflow:hidden}
.card.working{border-color:color-mix(in srgb,var(--c) 70%,transparent);animation:breathe 1.6s ease-in-out infinite}
@keyframes breathe{50%{box-shadow:0 0 0 1px color-mix(in srgb,var(--c) 40%,transparent),0 0 30px -6px var(--c)}}
.card.done{border-color:color-mix(in srgb,var(--c) 55%,transparent)}
.card.error{border-color:var(--bad)}
.card .top{display:flex;align-items:center;gap:8px}
.card .name{font-weight:800;letter-spacing:.08em;color:var(--c);font-size:13px}
.card .role{color:var(--muted);font-size:11px;letter-spacing:.14em}
.badge{margin-left:auto;font:700 10px var(--sans);letter-spacing:.12em;padding:3px 8px;border-radius:6px;background:var(--edge);color:var(--muted)}
.working .badge{background:var(--c);color:var(--ink)}
.done .badge{background:color-mix(in srgb,var(--c) 85%,transparent);color:var(--ink)}
.error .badge{background:#B33A48;color:#fff}
.track{height:2px;background:var(--edge);border-radius:2px;overflow:hidden}
.working .track{background:linear-gradient(90deg,transparent,var(--c),transparent) var(--edge);background-size:40% 100%;background-repeat:no-repeat;animation:comet 1.4s linear infinite}
.done .track{background:var(--c)}
.error .track{background:var(--bad)}
@keyframes comet{from{background-position:-40% 0}to{background-position:140% 0}}
.stage{color:var(--soft);font-size:12px}
.activity{color:var(--muted);font:12px var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:18px}
.body{font-size:13px;color:var(--soft);max-height:220px;overflow:hidden;mask-image:linear-gradient(transparent,#000 22px);display:flex;flex-direction:column-reverse}
.body .inner{padding-top:6px}
.foot{margin-top:auto;display:flex;gap:10px;color:var(--muted);font:11px var(--mono)}
.foot .model{margin-right:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cursor{display:inline-block;width:7px;height:14px;background:var(--c);vertical-align:-2px;margin-left:2px;animation:blink 1s steps(2) infinite}
@keyframes blink{50%{opacity:0}}
.verdict{margin:0 16px 16px;border-radius:16px;padding:2px;background:linear-gradient(135deg,var(--g1),var(--g2),var(--g3));display:none}
.verdict.show{display:block}
.verdict .in{background:var(--panel);border-radius:14px;padding:20px 24px}
.verdict h3{margin:0 0 10px;font-size:12px;letter-spacing:.28em;font-weight:800}
.verdict h3 .wordmark{font-size:13px}
.verdict .drafting{color:var(--muted);font-weight:500;letter-spacing:.1em;margin-left:8px}
.verdict .text{font-size:15px;line-height:1.65}
.meter{display:flex;gap:28px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px solid var(--edge);font:700 11px var(--sans);letter-spacing:.14em;color:var(--muted);align-items:center}
.meter .gauge{display:inline-flex;gap:3px;margin:0 10px;vertical-align:middle}
.meter .gauge i{width:14px;height:5px;border-radius:2px;background:var(--edge)}
.meter b{letter-spacing:.06em;font-size:12px}
.meta{margin-top:10px;color:var(--muted);font:12px var(--mono)}
.actions{display:flex;gap:8px;flex-wrap:wrap;padding:0 16px 16px}
.btn{border:1px solid var(--edge2);background:var(--panel2);border-radius:10px;padding:7px 13px;cursor:pointer;font-size:13px;color:var(--soft)}
.btn:hover{border-color:var(--accent);color:var(--text)}
.btn.primary{border-color:color-mix(in srgb,var(--accent) 60%,transparent);color:var(--text)}
.handoff,.problem{margin:0 16px 16px;padding:14px 16px;border-radius:12px;border:1px solid var(--edge);background:var(--panel2);font-size:13px}
.problem{border-color:color-mix(in srgb,var(--bad) 60%,transparent)}
.problem b{color:var(--bad)}
.fix{color:var(--accent);font-weight:700;margin-top:6px}
code,pre{font-family:var(--mono);font-size:12.5px}
:not(pre)>code{background:var(--panel2);border:1px solid var(--edge);padding:1px 5px;border-radius:5px}
pre{background:#07080D;border:1px solid var(--edge);border-radius:10px;padding:12px;overflow:auto;white-space:pre-wrap}
.text h3,.text h4,.text h5,.text h6,.body h3,.body h4{margin:14px 0 6px;letter-spacing:normal;font-size:15px}
.body h3,.body h4,.body h5,.body h6{font-size:13.5px;margin:10px 0 4px}
.text p{margin:8px 0}
table{border-collapse:collapse;margin:8px 0;font-size:13px}
td,th{border:1px solid var(--edge);padding:5px 9px;text-align:left}
th{background:var(--panel2)}
blockquote{margin:8px 0;padding-left:12px;border-left:3px solid var(--edge2);color:var(--soft)}
details.opinion{margin:0 16px 12px;border:1px solid var(--edge);border-radius:12px;background:var(--panel2)}
details.opinion summary{cursor:pointer;padding:10px 14px;font-weight:700;color:var(--c)}
details.opinion .text{padding:0 18px 14px;font-size:14px}
.hello{text-align:center;padding:40px 20px;color:var(--muted)}
.hello svg{width:64px;height:64px;filter:drop-shadow(0 0 24px var(--accent));animation:float 4s ease-in-out infinite}
@keyframes float{50%{transform:translateY(-6px) rotate(8deg)}}
.hello h1{font-size:28px;margin:14px 0 6px;color:var(--text);font-weight:750}
.hello p{max-width:560px;margin:0 auto}
.steps{display:flex;justify-content:center;gap:12px;margin-top:22px;flex-wrap:wrap}
.step{border:1px solid var(--edge);border-radius:12px;padding:10px 14px;background:var(--panel);font-size:13px;color:var(--soft)}
.step b{color:var(--accent);margin-right:6px}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--accent);padding:10px 16px;border-radius:12px;opacity:0;transition:.25s;pointer-events:none}
.toast.show{opacity:1}
select.field{width:auto;padding:5px 9px}
.toggle{margin-left:auto;display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);cursor:pointer}
.toggle input{accent-color:var(--accent)}
.toggle + .go{margin-left:0}
.continue{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:0 16px 16px;font-size:12px;color:var(--muted)}
.continue .btn{border-color:color-mix(in srgb,var(--c) 45%,transparent)}
.continue .btn b{color:var(--c)}
main,.wrap,.run,.card,.composer{min-width:0}
.body,.verdict .text,.handoff,.problem,.opinion .text{overflow-wrap:anywhere}
@media (max-width:1050px){.cards{grid-template-columns:1fr}.app{grid-template-columns:1fr}aside{display:none}.app.sidebar aside{display:flex}.modelgrid{grid-template-columns:1fr 1fr}.app.sidebar aside{max-height:55vh;border-right:0;border-bottom:1px solid var(--edge)}}
@media (max-width:720px){.tagline,.pills{display:none}header{gap:10px;padding:12px 14px}main{padding:14px}.runhead{flex-wrap:wrap}
.runhead h2{flex-basis:100%}.modelgrid{grid-template-columns:1fr}.verdict .in{padding:16px}.go{margin-left:0;width:100%}}
</style>
</head>
<body>
<div class="app" id="app">
<header>
  <div class="logo"><svg viewBox="-1 -1 2 2"><defs><linearGradient id="lg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="var(--g1)"/><stop offset=".5" stop-color="var(--g2)"/><stop offset="1" stop-color="var(--g3)"/></linearGradient></defs><path d="M0,-1 Q0.08,-0.08 1,0 Q0.08,0.08 0,1 Q-0.08,0.08 -1,0 Q-0.08,-0.08 0,-1Z" fill="url(#lg)"/></svg><span class="wordmark">CONCLAVE</span></div>
  <span class="tagline">three minds · one answer</span>
  <div class="spacer"></div>
  <div class="pills" id="pills"></div>
  <select class="field" id="theme" title="Theme"></select>
  <button class="iconbtn" id="menu" title="Projects">☰</button>
  <button class="iconbtn" id="quit" title="Stop Conclave">Quit</button>
</header>
<aside>
  <section>
    <h3>PROJECTS</h3>
    <input class="field" id="folder" placeholder="Paste any folder path and press Enter">
    <div class="list" id="projects" style="margin-top:8px"></div>
  </section>
  <section>
    <h3><span>CLAUDE CODE CONVERSATIONS</span></h3>
    <div class="list" id="convos"><div class="empty">Pick a project to see its conversations.</div></div>
  </section>
</aside>
<main>
  <div class="wrap">
    <div class="composer">
      <div class="chips" id="context"></div>
      <textarea id="question" placeholder="What should the council look at? For example: why does the build keep failing, and what's the fix?"></textarea>
      <div class="row">
        <button class="quick">Why does this keep failing, and what's the fix?</button>
        <button class="quick">Review Claude's last change for bugs.</button>
        <button class="quick">Is this the right approach? What would you do instead?</button>
        <button class="quick">What should we try next?</button>
      </div>
      <div class="row">
        <details class="models"><summary>⚙ Models and timeout</summary>
          <div class="modelgrid" id="modelgrid"></div>
        </details>
        <label class="toggle" title="Keep this round as sessions you can continue in Claude Code, Gemini CLI and Codex"><input type="checkbox" id="save"> Save sessions</label>
        <button class="go" id="ask">Ask the council <kbd>⌘↵</kbd></button>
      </div>
    </div>
    <div id="runs"></div>
    <div class="hello" id="hello">
      <svg viewBox="-1 -1 2 2"><path d="M0,-1 Q0.08,-0.08 1,0 Q0.08,0.08 0,1 Q-0.08,0.08 -1,0 Q-0.08,-0.08 0,-1Z" fill="url(#lg)"/></svg>
      <h1>Bring a stuck conversation to the council</h1>
      <p>Claude leads. Gemini and Codex read your project and your Claude Code conversation, give independent opinions, and Claude writes the verdict. Then hand it straight back to Claude Code.</p>
      <div class="steps"><div class="step"><b>1</b>Pick a project</div><div class="step"><b>2</b>Attach a conversation</div><div class="step"><b>3</b>Ask</div><div class="step"><b>4</b>Hand the verdict back</div></div>
    </div>
  </div>
</main>
</div>
<div class="toast" id="toast"></div>
<script>
const TOKEN = "__TOKEN__";
const NAMES = {claude: "CLAUDE", gemini: "GEMINI", openai: "CODEX"};
const ROLES = {claude: "LEAD", gemini: "ADVISER", openai: "ADVISER"};
const state = {info: null, project: null, conversation: null, convos: [], history: []};
const $ = (s, el = document) => el.querySelector(s);
const store = {get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d } catch { return d } },
               set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)) } catch {} }};

async function api(path, body) {
  const r = await fetch(path, {method: body ? "POST" : "GET", headers: {"X-Conclave-Token": TOKEN, "Content-Type": "application/json"},
                               body: body ? JSON.stringify(body) : undefined});
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2600); }
function esc(s) { return String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c])); }
function inline(s) {
  return s.split(/(`[^`]+`)/).map(part => {
    if (/^`[^`]+`$/.test(part)) return `<code>${esc(part.slice(1, -1))}</code>`;
    return esc(part).replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*\w])\*([^*\n]+)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }).join("");
}
function md(src) {
  const lines = String(src || "").replace(/\r/g, "").split("\n"); let html = "", i = 0;
  const isList = l => /^\s*([-*+]|\d+[.)])\s+/.test(l);
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*```/.test(l)) { const code = []; i++; while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]); i++;
      html += `<pre><code>${esc(code.join("\n"))}</code></pre>`; continue; }
    const h = l.match(/^(#{1,6})\s+(.*)/);
    if (h) { const n = Math.min(h[1].length + 2, 6); html += `<h${n}>${inline(h[2])}</h${n}>`; i++; continue; }
    if (/^\s*\|/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(c.trim()));
      let t = `<table><tr>${cells(l).map(c => `<th>${c}</th>`).join("")}</tr>`; i += 2;
      while (i < lines.length && /^\s*\|/.test(lines[i])) t += `<tr>${cells(lines[i++]).map(c => `<td>${c}</td>`).join("")}</tr>`;
      html += t + "</table>"; continue; }
    if (isList(l)) { const ordered = /^\s*\d/.test(l), items = [];
      while (i < lines.length && isList(lines[i])) { items.push(lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, ""));
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !isList(lines[i])) items[items.length - 1] += " " + lines[i++].trim(); }
      const tag = ordered ? "ol" : "ul"; html += `<${tag}>${items.map(x => `<li>${inline(x)}</li>`).join("")}</${tag}>`; continue; }
    if (/^>\s?/.test(l)) { const q = []; while (i < lines.length && /^>\s?/.test(lines[i])) q.push(lines[i++].replace(/^>\s?/, ""));
      html += `<blockquote>${md(q.join("\n"))}</blockquote>`; continue; }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(l)) { html += "<hr>"; i++; continue; }
    if (!l.trim()) { i++; continue; }
    const p = []; while (i < lines.length && lines[i].trim() && !/^(\s*```|#{1,6}\s|>|\s*\|)/.test(lines[i]) && !isList(lines[i])) p.push(lines[i++]);
    html += `<p>${inline(p.join(" "))}</p>`;
  }
  return html;
}
const COUNCIL = /^[ \t]*\**COUNCIL:?\**:?[ \t]*agreement[\s\S]*$/im;
const drafting = t => t.replace(COUNCIL, "").replace(/\n[^\n]*$/, m => "COUNCIL:".startsWith(m.trim().replace(/^\*+/, "").toUpperCase().slice(0, 8)) && m.trim() ? "" : m);
const clock = s => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const count = n => n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n);

function applyTheme(name) {
  const t = state.info.themes[name] || state.info.themes.violet, r = document.documentElement.style;
  const mix = (a, b, p) => `color-mix(in srgb, ${a} ${p}%, ${b})`;
  r.setProperty("--ink", t.ink); r.setProperty("--edge2", t.edge); r.setProperty("--edge", mix(t.edge, t.ink, 70));
  r.setProperty("--panel", mix(t.edge, t.ink, 18)); r.setProperty("--panel2", mix(t.edge, t.ink, 30));
  r.setProperty("--accent", t.accent); r.setProperty("--muted", t.muted);
  ["g1", "g2", "g3"].forEach((k, i) => r.setProperty("--" + k, t.gradient[i]));
  Object.entries(t.colors).forEach(([k, v]) => r.setProperty("--" + k, v));
  store.set("theme", name);
}

async function boot() {
  state.info = await api("/api/info");
  const themes = $("#theme");
  themes.innerHTML = Object.keys(state.info.themes).map(n => `<option>${n}</option>`).join("");
  themes.value = store.get("theme", state.info.theme); applyTheme(themes.value);
  themes.onchange = () => applyTheme(themes.value);
  $("#pills").innerHTML = state.info.providers.map(p =>
    `<div class="pill ${p.installed ? "" : "missing"}" title="${esc(p.installed ? p.cli + " is installed" : p.install)}"><i></i><b>${esc(NAMES[p.id])}</b>${esc(p.model || "default")}</div>`).join("");
  const saved = store.get("models", {});
  $("#modelgrid").innerHTML = state.info.providers.map(p =>
    `<label>${esc(NAMES[p.id])}<input class="field" data-model="${p.id}" placeholder="${esc(p.model || "provider default")}" value="${esc(saved[p.id] || "")}"></label>`).join("")
    + `<label>TIMEOUT (s)<input class="field" id="timeout" type="number" min="10" value="${store.get("timeout", state.info.timeout)}"></label>`;
  $("#modelgrid").oninput = () => { const m = {}; document.querySelectorAll("[data-model]").forEach(e => m[e.dataset.model] = e.value.trim());
    store.set("models", m); store.set("timeout", +$("#timeout").value || state.info.timeout); };
  $("#save").checked = store.get("saveSessions", state.info.saveSessions);
  $("#save").onchange = () => store.set("saveSessions", $("#save").checked);
  await loadProjects();
  const last = store.get("project", null);
  const pick = last && state.projects.find(p => p.path === last) ? last : (state.projects.find(p => p.path === state.info.cwd) ? state.info.cwd : null);
  if (pick) selectProject(pick);
  renderContext();
}

async function loadProjects() {
  state.projects = await api("/api/projects");
  $("#projects").innerHTML = state.projects.length ? state.projects.map(p =>
    `<div class="item ${p.path === state.project ? "on" : ""}" data-path="${esc(p.path)}"><div class="t">${esc(p.name)}</div>
     <div class="s"><span>${p.count} conversation${p.count === 1 ? "" : "s"}</span><span>${esc(p.when)}</span></div></div>`).join("")
    : `<div class="empty">No Claude Code projects found in ~/.claude/projects yet. Paste a folder path above.</div>`;
  document.querySelectorAll("#projects .item").forEach(el => el.onclick = () => selectProject(el.dataset.path));
}

async function selectProject(path) {
  try {
    const data = await api("/api/conversations?project=" + encodeURIComponent(path));
    state.project = data.project; state.convos = data.conversations; state.conversation = null; store.set("project", state.project);
  } catch (e) { toast(e.message); return; }
  document.querySelectorAll("#projects .item").forEach(el => el.classList.toggle("on", el.dataset.path === state.project));
  renderConvos(); renderContext();
}

function renderConvos() {
  const box = $("#convos");
  box.innerHTML = state.convos.length ? state.convos.map(c =>
    `<div class="item ${state.conversation === c.id ? "on" : ""}" data-id="${c.id}"><div class="t">${esc(c.title)}</div>
     <div class="s"><span>${esc(c.when)}</span><span>${c.messages} msgs</span>${c.errors ? `<span class="err">${c.errors} error${c.errors === 1 ? "" : "s"}</span>` : ""}${c.branch ? `<span>⎇ ${esc(c.branch)}</span>` : ""}</div></div>`).join("")
    : `<div class="empty">No Claude Code conversations for this folder. You can still ask about the project.</div>`;
  box.querySelectorAll(".item").forEach(el => el.onclick = () => {
    state.conversation = state.conversation === el.dataset.id ? null : el.dataset.id; renderConvos(); renderContext(); $("#question").focus(); });
}

function renderContext() {
  const chips = [];
  if (state.project) chips.push(`<span class="chip live">⌂ <span>${esc(state.project)}</span><button data-x="project" title="Stop reviewing this project">✕</button></span>`);
  else chips.push(`<span class="chip">No project: the models run in ${esc(state.info.cwd)}</span>`);
  const c = state.convos.find(c => c.id === state.conversation);
  if (c) chips.push(`<span class="chip live">❝ <span>${esc(c.title)}</span>${c.errors ? `<b class="err">${c.errors} error${c.errors === 1 ? "" : "s"}</b>` : ""}<button data-x="conversation" title="Detach">✕</button></span>`);
  else if (state.project && state.convos.length) chips.push(`<span class="chip">Attach a Claude Code conversation from the sidebar for context</span>`);
  if (state.history.length) chips.push(`<span class="chip">↺ follow-up remembers ${Math.min(state.history.length, 3)} answer${state.history.length > 1 ? "s" : ""}<button data-x="history" title="Start fresh">✕</button></span>`);
  $("#context").innerHTML = chips.join("");
  $("#context").querySelectorAll("[data-x]").forEach(b => b.onclick = () => {
    if (b.dataset.x === "project") { state.project = null; state.conversation = null; state.convos = []; store.set("project", null); renderConvos(); loadProjects(); }
    if (b.dataset.x === "conversation") { state.conversation = null; renderConvos(); }
    if (b.dataset.x === "history") state.history = [];
    renderContext(); });
}

function card(name, model) {
  return `<div class="card" data-name="${name}" style="--c:var(--${name})">
    <div class="top"><span class="name">${name === "claude" ? "✦" : name === "gemini" ? "✧" : "✳"} ${NAMES[name]}</span><span class="role">${ROLES[name]}</span><span class="badge">STANDBY</span></div>
    <div class="track"></div><div class="stage">standing by</div><div class="activity"></div>
    <div class="body"><div class="inner"></div></div>
    <div class="foot"><span class="model">${esc(model || "provider default")}</span><span class="tok"></span><span class="time"></span></div></div>`;
}

async function ask() {
  const question = $("#question").value.trim();
  if (!question) { $("#question").focus(); return; }
  const models = store.get("models", {}), timeout = store.get("timeout", state.info.timeout);
  let id;
  try { id = (await api("/api/runs", {question, project: state.project, conversation: state.conversation, models, timeout, history: state.history.slice(-3),
    saveSessions: $("#save").checked})).id; }
  catch (e) { toast(e.message); return; }
  $("#hello")?.remove(); $("#question").value = "";
  const conv = state.convos.find(c => c.id === state.conversation);
  const el = document.createElement("section"); el.className = "run";
  const providers = Object.fromEntries(state.info.providers.map(p => [p.id, models[p.id] || p.model]));
  el.innerHTML = `<div class="runhead"><h2>${esc(question)}</h2><span class="clock">0:00</span><button class="btn" data-act="cancel">Cancel</button></div>
    <div class="chips" style="padding:10px 20px 0">${state.project ? `<span class="chip">⌂ <span>${esc(state.project.split("/").pop())}</span></span>` : ""}${conv ? `<span class="chip">❝ <span>${esc(conv.title)}</span></span>` : ""}</div>
    <div class="tracker"><span class="ph" data-ph="brief">◉ BRIEF</span><span class="bar"></span><span class="ph" data-ph="advise">○ DELIBERATION</span><span class="bar"></span><span class="ph" data-ph="verdict">○ VERDICT</span></div>
    <div class="cards">${["claude", "gemini", "openai"].map(n => card(n, providers[n])).join("")}</div>
    <div class="problems"></div>
    <div class="verdict"><div class="in"><h3><span class="wordmark">✦ VERDICT</span><span class="drafting">DRAFTING…</span></h3><div class="text"></div><div class="meterbox"></div></div></div>
    <div class="actions"></div><div class="continue"></div><div class="opinions"></div>`;
  $("#runs").prepend(el);
  follow(id, el, question);
}

function follow(id, el, question) {
  const run = {phase: "brief", started: Date.now(), cards: {}, done: false, tokens: {}};
  const cardEl = n => el.querySelector(`.card[data-name="${n}"]`);
  const tick = setInterval(() => {
    if (!run.done) el.querySelector(".clock").textContent = clock((Date.now() - run.started) / 1000);
    for (const [n, c] of Object.entries(run.cards)) if (c.start && !c.end) cardEl(n).querySelector(".time").textContent = clock((Date.now() - c.start) / 1000);
  }, 250);
  const phases = ["brief", "advise", "verdict", "done"];
  const setPhase = p => { run.phase = p; const at = phases.indexOf(p);
    el.querySelectorAll(".ph").forEach((ph, i) => { ph.className = "ph" + (i < at ? " on" : i === at ? " now" : "");
      ph.textContent = (i < at ? "✓ " : i === at ? "◉ " : "○ ") + ph.textContent.slice(2); });
    el.querySelectorAll(".tracker .bar").forEach((b, i) => b.classList.toggle("on", i < at));
    if (p === "verdict") el.querySelector(".verdict").classList.add("show"); };
  const events = new EventSource(`/api/runs/${id}/events?token=${encodeURIComponent(TOKEN)}`);
  events.onmessage = msg => {
    const e = JSON.parse(msg.data), c = e.name && cardEl(e.name);
    if (e.type === "phase") setPhase(e.phase);
    if (e.type === "start") { run.cards[e.name] = {start: Date.now()}; c.className = "card working"; c.querySelector(".badge").textContent = "● THINKING";
      c.querySelector(".stage").textContent = e.stage; c.querySelector(".activity").textContent = e.activity || "";
      c.querySelector(".inner").innerHTML = e.preview ? md(e.preview) : ""; }
    if (e.type === "stream") {
      if (e.activity) c.querySelector(".activity").textContent = "› " + e.activity;
      if (e.name === "claude" && run.phase === "verdict") el.querySelector(".verdict .text").innerHTML = md(drafting(e.text)) + '<span class="cursor" style="--c:var(--accent)"></span>';
      else { c.querySelector(".inner").innerHTML = md(e.name === "claude" ? drafting(e.text) : e.text) + '<span class="cursor"></span>';
        if (!run.tokens[e.name] && e.text) c.querySelector(".tok").textContent = "~" + count(Math.round(e.text.length / 4)) + " tok"; } }
    if (e.type === "finish") { run.cards[e.name].end = Date.now(); c.className = "card " + e.status;
      c.querySelector(".badge").textContent = e.status === "done" ? "✓ DONE" : "✗ FAILED"; c.querySelector(".stage").textContent = e.stage;
      c.querySelector(".activity").textContent = ""; c.querySelector(".inner").innerHTML = e.status === "error" ? `<span class="err">${esc(e.preview)}</span>` : md(e.preview); }
    if (e.type === "tokens") { const t = run.tokens[e.name] || [0, 0]; run.tokens[e.name] = [t[0] + e.in, t[1] + e.out];
      c.querySelector(".tok").textContent = `↑${count(run.tokens[e.name][0])} ↓${count(run.tokens[e.name][1])}`; }
    if (["done", "cancelled", "failed"].includes(e.type)) { events.close(); clearInterval(tick); run.done = true;
      el.querySelector('[data-act="cancel"]').remove(); finish(el, e, question); }
  };
  events.onerror = () => { if (run.done) events.close(); };
  el.querySelector('[data-act="cancel"]').onclick = () => api(`/api/runs/${id}/cancel`, {}).catch(e => toast(e.message));
  el.dataset.run = id;
}

function finish(el, e, question) {
  const actions = el.querySelector(".actions"), problems = el.querySelector(".problems");
  if (e.type === "cancelled") { problems.innerHTML = `<div class="problem"><b>Cancelled.</b> The model calls were stopped.</div>`; return; }
  if (e.type === "failed") { problems.innerHTML = `<div class="problem"><b>Something went wrong:</b> ${esc(e.message)}</div>`; return; }
  const r = e.result;
  problems.innerHTML = r.errors.map(x => `<div class="problem"><b>✗ ${esc(x.headline)}</b><div>${esc(x.message)}</div>${x.fix ? `<div class="fix">→ ${esc(x.fix)}</div>` : ""}</div>`).join("");
  if (!r.final) { el.querySelector(".verdict").classList.remove("show"); return; }
  const v = el.querySelector(".verdict"); v.classList.add("show"); v.querySelector(".drafting").remove();
  v.querySelector(".text").innerHTML = md(r.final);
  const levels = {strong: [11, "var(--good)"], partial: [7, "var(--warn)"], split: [3, "var(--bad)"]}, dots = {high: 3, medium: 2, low: 1};
  let meter = "";
  if (r.agreement) { const [n, col] = levels[r.agreement];
    meter += `<span>AGREEMENT<span class="gauge">${Array.from({length: 12}, (_, i) => `<i style="${i < n ? `background:${col};box-shadow:0 0 6px ${col}` : ""}"></i>`).join("")}</span><b style="color:${col}">${r.agreement}</b></span>`; }
  if (r.confidence) { const n = dots[r.confidence];
    meter += `<span>CONFIDENCE<span class="gauge">${[0, 1, 2].map(i => `<i style="width:9px;height:9px;border-radius:50%;${i < n ? "background:var(--accent)" : ""}"></i>`).join("")}</span><b style="color:var(--accent)">${r.confidence}</b></span>`; }
  v.querySelector(".meterbox").innerHTML = (meter ? `<div class="meter">${meter}</div>` : "") + `<div class="meta">${esc(r.meta)}</div>`;
  actions.innerHTML = `<button class="btn primary" data-act="handoff">↩ Hand back to Claude Code</button><button class="btn" data-act="copy">Copy verdict</button>
    <button class="btn" data-act="follow">Ask a follow-up</button>`;
  actions.querySelector('[data-act="copy"]').onclick = () => navigator.clipboard.writeText(r.final).then(() => toast("Verdict copied"));
  actions.querySelector('[data-act="follow"]').onclick = () => { $("#question").focus(); window.scrollTo({top: 0, behavior: "smooth"}); };
  actions.querySelector('[data-act="handoff"]').onclick = async () => {
    try { const h = await api(`/api/runs/${el.dataset.run}/handoff`, {});
      let box = el.querySelector(".handoff"); if (!box) { box = document.createElement("div"); box.className = "handoff"; actions.after(box); }
      box.innerHTML = `Saved <code>${esc(h.path)}</code>. Run this in Terminal to continue the Claude Code conversation with the verdict:<pre>${esc(h.command)}</pre>`;
      navigator.clipboard.writeText(h.command).then(() => toast("Command copied — paste it in Terminal"), () => {});
    } catch (err) { toast(err.message); } };
  const tools = {claude: ["✦", "Claude Code"], gemini: ["✧", "Gemini CLI"], openai: ["✳", "Codex"]};
  const saved = Object.keys(tools).filter(n => r.sessions && r.sessions[n]);
  const cont = el.querySelector(".continue");
  if (saved.length) {
    cont.innerHTML = `<span>↪ Continue in:</span>` + saved.map(n => `<button class="btn" style="--c:var(--${n})" data-tool="${n}"><b>${tools[n][0]}</b> ${tools[n][1]}</button>`).join("");
    cont.querySelectorAll("[data-tool]").forEach(b => b.onclick = async () => {
      try { const res = await api(`/api/runs/${el.dataset.run}/continue`, {provider: b.dataset.tool});
        if (res.opened) toast(`Opened in Terminal: ${tools[b.dataset.tool][1]}`);
        else navigator.clipboard.writeText(res.command).then(() => toast("Command copied: paste it in a terminal"), () => toast(res.command));
      } catch (err) { toast(err.message); } });
  }
  el.querySelector(".opinions").innerHTML = [["brief", "claude", "✦ CLAUDE · BRIEF", r.brief], ...Object.entries(r.opinions).map(([n, t]) => [n, n, `${n === "gemini" ? "✧" : "✳"} ${NAMES[n]} · OPINION`, t])]
    .filter(x => x[3]).map(([, n, title, t]) => `<details class="opinion" style="--c:var(--${n})"><summary>${esc(title)}</summary><div class="text">${md(t)}</div></details>`).join("");
  state.history.push([question, r.final]); renderContext();
}

$("#ask").onclick = ask;
$("#question").addEventListener("keydown", e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); ask(); } });
document.querySelectorAll(".quick").forEach(b => b.onclick = () => { $("#question").value = b.textContent; $("#question").focus(); });
$("#folder").addEventListener("keydown", e => { if (e.key === "Enter" && e.target.value.trim()) selectProject(e.target.value.trim()); });
$("#menu").onclick = () => $("#app").classList.toggle("sidebar");
$("#quit").onclick = async () => { if (!confirm("Stop Conclave? Running rounds will be cancelled.")) return;
  await api("/api/shutdown", {}).catch(() => {}); document.body.innerHTML = '<div class="hello"><h1>The council is adjourned.</h1><p>You can close this tab.</p></div>'; };
boot().catch(e => toast(e.message));
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
