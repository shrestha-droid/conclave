#!/usr/bin/env python3
"""The Conclave app: a local web page for bringing Claude Code projects and conversations to the council.

`conclave --app` (or `conclave-app`) starts a small server on 127.0.0.1 and opens it in your browser. Pick a project
and one of its Claude Code conversations, ask a question, and watch Claude, Gemini and ChatGPT work live. The server only
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
import conclave_agents as agents
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
            self.run.emit({"type": "stream", "name": name, "text": stream.live, "activity": stream.activity,
                           "model": stream.model})
        return update

    def finish(self, name: str, status: str, stage: str, preview: str = "") -> None:
        self.run.emit({"type": "finish", "name": name, "status": status, "stage": stage, "preview": preview})

    def tokens(self, name: str, tokens_in: int, tokens_out: int) -> None:
        self.run.emit({"type": "tokens", "name": name, "in": tokens_in, "out": tokens_out})

    def set_mode(self, mode: str) -> None:
        self.run.emit({"type": "mode", "mode": mode})

    def plan(self, subtasks: list[dict]) -> None:
        self.run.emit({"type": "plan", "subtasks": [{"agent": t["agent"], "title": t["title"]} for t in subtasks]})

    def subtask(self, index: int, status: str, preview: str = "") -> None:
        self.run.emit({"type": "subtask", "index": index, "status": status, "preview": preview})

    def done(self) -> None:
        pass


def outcome(result: core.Round) -> dict:
    return {
        "final": result.final,
        "agreement": result.agreement,
        "confidence": result.confidence,
        "brief": result.brief,
        "opinions": {name: result.opinion(name) for name in result.opinions},
        "failed": result.failed,
        "errors": [{"provider": p, "headline": h, "message": m, "fix": core.fix_for(p, m)} for p, h, m in result.errors],
        "meta": core.round_meta(result),
        "sessions": result.sessions,
        "mode": result.mode,
        "speaker": result.speaker,
        "access": result.access,
        "thread": result.thread,
        "advisers": list(result.advisers),
        "plan": [{"agent": t["agent"], "title": t["title"], "ok": "result" in t} for t in result.plan],
        "tokens": [result.tokens_in, result.tokens_out],
        "cost": result.cost,
        "seconds": result.seconds,
    }


# ---------------------------------------------------------------- server

class App:
    def __init__(self, selected: dict[str, str | None], timeout: int, save_sessions: bool = True,
                 cross_examine: bool = True, advisers: tuple[str, ...] = agents.DEFAULT_ADVISERS, split: str = "auto"):
        self.selected = selected
        self.timeout = timeout
        self.save_sessions = save_sessions
        self.cross_examine = cross_examine
        self.advisers = tuple(advisers)
        self.split = split
        self.token = secrets.token_urlsafe(24)
        self.runs: dict[str, Run] = {}
        self.server: ThreadingHTTPServer | None = None

    def info(self) -> dict:
        return {
            "version": core.__version__,
            "cwd": str(Path.cwd()),
            "theme": core.THEME,
            "themes": {name: {**theme, "colors": core.theme_colors(name)} for name, theme in core.THEMES.items()},
            "advisers": list(self.advisers),
            "split": self.split,
            "timeout": self.timeout,
            "saveSessions": self.save_sessions,
            "crossExamine": self.cross_examine,
            "access": core.ACCESS_NOTES,
            "agents": [
                {"id": agent.id, "label": agent.label, "name": agent.name, "cli": agent.binary, "handle": core.handle(agent.id),
                 "role": "LEAD" if agent.id == "claude" else "ADVISER", "strengths": agent.strengths,
                 "installed": bool(shutil.which(agent.binary)), "model": core.model_for(agent.id, self.selected),
                 "style": dict(zip(("c", "c2", "ground"), core.agent_style(agent.id))),
                 "install": agent.install, "installCommand": agent.install_command, "sessions": bool(agent.resume),
                 "sprites": {mood: agents.sprite_frame(agent.sprite, mood) for mood in ("idle", "blink", "done", "error", "sleep")}}
                for agent in core.AGENTS.values()
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
        advisers = tuple(a for a in (body.get("advisers") or self.advisers) if a in core.AGENTS and a != "claude")
        if not advisers:
            raise ValueError("Seat at least one adviser")
        split = body.get("split") if body.get("split") in ("auto", "always", "never") else self.split
        cross_examine = bool(body.get("crossExamine", self.cross_examine))
        # A hand-off goes to one agent at the chosen access level; the council itself always only reads.
        agent = core.ALIASES.get(str(body.get("agent") or ""), str(body.get("agent") or "")) or None
        if agent and agent not in core.AGENTS:
            raise ValueError(f"Unknown agent {agent}")
        access = str(body.get("access") or "read")
        if access not in core.ACCESS_LEVELS:
            raise ValueError("access must be read, edit or full")
        resume = str(body.get("thread") or "") if agent else ""
        task = ""
        if body.get("apply"):
            source = self.runs.get(str(body["apply"]))
            if not source or not source.result or not source.result.final:
                raise ValueError("That verdict is no longer available")
            agent, access = agent or "claude", access if access != "read" else "edit"
            task = core.apply_task(source.result.request, source.result.final)
            project, conversation = project or source.project, None
        run = Run(project, conversation)
        self.runs[run.id] = run

        def work() -> None:
            try:
                if agent:
                    work_task = task or (question if resume else core.compose(question, project, conversation, history))
                    result = core.solo(work_task, agent, selected, timeout, WebView(run), cwd=project, control=run.control,
                                       title=question, save_sessions=save_sessions, access=access, resume=resume)
                else:
                    result = core.deliberate(core.compose(question, project, conversation, history), selected, timeout,
                                             WebView(run), cwd=project, control=run.control, title=question,
                                             save_sessions=save_sessions, cross_examine=cross_examine, advisers=advisers,
                                             split=split)
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
            if method == "GET" and parts == ["health"]:
                return self.send(200, [{"id": h.provider, "installed": h.installed, "signedIn": h.signed_in,
                                        "version": h.version, "detail": h.detail}
                                       for h in (core.check(name) for name in core.AGENTS)])
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
    core.apply_theme(settings.theme or core.DEFAULT_THEME)
    app = App({name: settings.models.get(name) for name in core.AGENTS}, args.timeout, settings.save_sessions,
              settings.cross_examine, settings.advisers, settings.split)
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
    """Conclave's icon, drawn pixel by pixel in SP_SYS style: warm-ink corner brackets around a signal-red full stop,
    on a near-black rounded square."""
    def mix(a, b, t):
        t = min(max(t, 0.0), 1.0)
        return tuple(round(x + (y - x) * t) for x, y in zip(a, b))

    ink, warm, red = (0x07, 0x07, 0x0A), (0xEE, 0xEB, 0xE4), (0xE5, 0x09, 0x14)
    rows = []
    for y in range(size):
        row = bytearray(b"\x00")
        v = (y + 0.5) / size * 2 - 1
        for x in range(size):
            u = (x + 0.5) / size * 2 - 1
            edge = (abs(u) ** 5 + abs(v) ** 5) ** 0.2  # squircle background
            alpha = min(max((0.92 - edge) * size / 2, 0.0), 1.0)
            color = mix((0x16, 0x14, 0x16), ink, (v + 1) / 2)
            color = mix(color, red, max(0.0, 0.5 - ((u - 0.02) ** 2 + (v - 0.02) ** 2) ** 0.5) * 0.4)  # a red glow
            # Two corner brackets: top left and bottom right, each an L of thick strokes.
            stroke, reach, at = 0.085, 0.42, 0.58
            for sx in (-1, 1):
                cu, cv = u * sx, v * sx
                if (at - stroke <= -cu <= at and at - reach <= -cv <= at) or (at - stroke <= -cv <= at and at - reach <= -cu <= at):
                    color = warm
            dot = max(abs(u - 0.02), abs(v - 0.02))  # the full stop: a square of signal red
            color = mix(color, red, (0.17 - dot) * size * 0.5)
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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;700&display=swap" rel="stylesheet">
<style>
:root{--ink:#07070A;--panel:#0D0D11;--panel2:#121217;--edge:#1E1E24;--edge2:#2A2A30;--accent:#E50914;--accent2:#FF6B73;--muted:#85837D;
--text:#EEEBE4;--soft:#B9B6AE;--good:#4ADE80;--warn:#FFB547;--bad:#FF9F43;
--claude:#D97757;--gemini:#4F8FF7;--openai:#10A37F;--qwen:#7C6CF2;--copilot:#A371F7;--opencode:#FAB283;--ollama:#E5E5E5;
--mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--sans:Inter,-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;--serif:"Instrument Serif",Georgia,"Times New Roman",serif}
*{box-sizing:border-box}
html,body{margin:0;height:100%;background:var(--ink);color:var(--text);font:14px/1.55 var(--sans)}
body{background:
 linear-gradient(color-mix(in srgb,var(--edge) 55%,transparent) 1px,transparent 1px) 0 0/40px 40px,
 linear-gradient(90deg,color-mix(in srgb,var(--edge) 55%,transparent) 1px,transparent 1px) 0 0/40px 40px,
 radial-gradient(900px 500px at 85% -10%,color-mix(in srgb,var(--accent) 14%,transparent),transparent 70%),var(--ink)}
button,input,textarea,select{font:inherit;color:inherit}
a{color:var(--accent2)}
::selection{background:color-mix(in srgb,var(--accent) 45%,transparent)}
.app{display:grid;grid-template-columns:320px 1fr;grid-template-rows:auto 1fr;height:100vh}

/* HUD: corner brackets on any .hud box */
.hud{position:relative}
.hud::before,.hud::after{content:"";position:absolute;width:12px;height:12px;pointer-events:none;border:1.5px solid var(--hc,var(--accent))}
.hud::before{top:-1px;left:-1px;border-right:0;border-bottom:0}
.hud::after{bottom:-1px;right:-1px;border-left:0;border-top:0}
.mod{font:500 11px var(--mono);letter-spacing:.18em;color:var(--muted);text-transform:uppercase}
.mod b{color:var(--accent);font-weight:700;margin-right:8px}

header{grid-column:1/-1;display:flex;align-items:center;gap:18px;padding:12px 22px;border-bottom:1px solid var(--edge);
background:color-mix(in srgb,var(--ink) 82%,transparent);backdrop-filter:blur(10px)}
.logo{display:flex;align-items:center;gap:12px}
.mark{font:700 13px var(--mono);color:var(--text);letter-spacing:.04em}
.mark i{color:var(--accent);font-style:normal}
.wordmark{font:400 30px/1 var(--serif);letter-spacing:-.01em;color:var(--text)}
.wordmark i{color:var(--accent);font-style:normal}
.tagline{font:500 11px var(--mono);letter-spacing:.16em;color:var(--muted)}
.tagline .live{color:var(--good)}
.tagline .live::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--good);box-shadow:0 0 8px var(--good);margin-right:6px;vertical-align:1px;animation:pulse 2s infinite}
@keyframes pulse{50%{opacity:.35}}
.spacer{flex:1}
.pills{display:flex;gap:6px;flex-wrap:wrap}
.pill{display:flex;align-items:center;gap:7px;padding:4px 10px;border:1px solid var(--edge2);font:11px var(--mono);color:var(--soft);background:var(--panel)}
.pill i{width:6px;height:6px;border-radius:50%;background:var(--good);box-shadow:0 0 8px var(--good)}
.pill.missing i{background:var(--bad);box-shadow:0 0 8px var(--bad)}
.pill.unsigned i{background:var(--warn);box-shadow:0 0 8px var(--warn)}
.pill b{color:var(--c);font-weight:700}
.iconbtn{border:1px solid var(--edge2);background:var(--panel);padding:6px 11px;cursor:pointer;color:var(--soft);font:12px var(--mono)}
.iconbtn:hover{border-color:var(--accent);color:var(--text)}

aside{border-right:1px solid var(--edge);overflow:auto;padding:20px 16px;display:flex;flex-direction:column;gap:26px;background:color-mix(in srgb,var(--ink) 75%,transparent)}
aside h3{margin:0 0 10px 2px}
.list{display:flex;flex-direction:column;gap:2px}
.item{padding:9px 11px;cursor:pointer;border-left:2px solid transparent;transition:.12s}
.item:hover{background:var(--panel2)}
.item.on{background:color-mix(in srgb,var(--accent) 9%,var(--panel));border-left-color:var(--accent)}
.item .t{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.item .s{color:var(--muted);font:11px var(--mono);display:flex;gap:10px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.err{color:var(--bad)}
.field{width:100%;padding:9px 11px;border:1px solid var(--edge2);background:var(--panel);outline:none;border-radius:0}
.field:focus{border-color:var(--accent)}
.empty{color:var(--muted);font-size:12px;padding:6px 11px}

main{overflow:auto;padding:26px 30px 70px}
.wrap{max-width:1180px;margin:0 auto;display:flex;flex-direction:column;gap:26px}
.composer{border:1px solid var(--edge2);background:color-mix(in srgb,var(--panel) 92%,transparent);padding:18px 18px 16px}
.composer .mod{margin-bottom:12px;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px}
.chip{display:inline-flex;align-items:center;gap:7px;padding:3px 10px;background:var(--panel2);border:1px solid var(--edge2);font:11.5px var(--mono);color:var(--soft);max-width:100%}
.chip span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chip.live{border-color:color-mix(in srgb,var(--accent) 55%,transparent);color:var(--text)}
.chip button{border:0;background:none;color:var(--muted);cursor:pointer;padding:0 0 0 2px}

/* Send to: Council or any one agent, each in its own colours */
.sendto{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.sendto .lbl{font:11px var(--mono);letter-spacing:.14em;color:var(--muted);margin-right:4px}
.dest{display:inline-flex;align-items:center;gap:7px;padding:4px 11px 4px 5px;border:1px solid var(--edge2);background:var(--panel);cursor:pointer;color:var(--muted);font:600 12px var(--sans)}
.dest .sprite{width:22px;height:19px;filter:none}
.dest:hover{color:var(--text)}
.dest.on{color:var(--text);border-color:var(--c);background:color-mix(in srgb,var(--c) 14%,var(--ground,var(--panel)));box-shadow:inset 0 -2px 0 var(--c)}
.dest.absent{opacity:.45}
.dest.council{padding-left:11px}
.dest.council.on{--c:var(--accent)}
.lineup{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:0 0 10px;font:11px var(--mono);color:var(--muted)}
.seat{display:inline-flex;align-items:center;gap:6px;padding:2px 9px 2px 3px;border:1px solid var(--edge);background:var(--panel);cursor:pointer;color:var(--muted)}
.seat .sprite{width:18px;height:16px;filter:none}
.seat.on{border-color:color-mix(in srgb,var(--c) 65%,transparent);color:var(--text)}
.seat.absent{opacity:.45}
.seat.lead{cursor:default}
textarea{width:100%;min-height:96px;resize:vertical;border:0;outline:none;background:transparent;font-size:16px;line-height:1.55}
textarea::placeholder{color:var(--muted)}
.row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px}
.quick{border:1px dashed var(--edge2);background:none;padding:4px 11px;font-size:12px;color:var(--soft);cursor:pointer}
.quick:hover{border-color:var(--accent);color:var(--text)}
.access{display:inline-flex;border:1px solid var(--edge2)}
.access button{border:0;background:var(--panel);padding:5px 11px;cursor:pointer;color:var(--muted);font:600 11px var(--mono);letter-spacing:.1em}
.access button+button{border-left:1px solid var(--edge2)}
.access button.on[data-level=read]{background:var(--panel2);color:var(--text)}
.access button.on[data-level=edit]{background:color-mix(in srgb,var(--warn) 22%,var(--panel));color:var(--warn)}
.access button.on[data-level=full]{background:color-mix(in srgb,var(--accent) 28%,var(--panel));color:#fff}
.access.off{opacity:.4;pointer-events:none}
.go{margin-left:auto;border:0;padding:10px 20px;font:700 13px var(--sans);letter-spacing:.04em;cursor:pointer;color:#fff;background:var(--accent);box-shadow:0 10px 30px -10px var(--accent)}
.go:hover{filter:brightness(1.1)}
.go:disabled{opacity:.5;cursor:default}
.go kbd{font:11px var(--mono);opacity:.75;margin-left:6px}
details.models{font:12px var(--mono);color:var(--muted)}
details.models summary{cursor:pointer;list-style:none}
.modelgrid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:8px}
.modelgrid label{display:flex;flex-direction:column;gap:4px}
select.field{width:auto;padding:5px 9px}
.toggle{display:flex;align-items:center;gap:6px;font:12px var(--mono);color:var(--muted);cursor:pointer}
.toggle input{accent-color:var(--accent)}

.run{border:1px solid var(--edge2);background:color-mix(in srgb,var(--panel) 94%,transparent);overflow:hidden;animation:rise .35s ease}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1}}
.runhead{padding:16px 20px 12px;display:flex;gap:16px;align-items:flex-start}
.runhead h2{margin:4px 0 0;font:400 26px/1.15 var(--serif);flex:1;white-space:pre-wrap}
.clock{font:600 13px var(--mono);color:var(--accent);white-space:nowrap;margin-top:8px}
.tracker{display:flex;align-items:center;gap:12px;padding:10px 20px 14px;font:600 11px var(--mono);letter-spacing:.14em;color:var(--edge2);flex-wrap:wrap}
.tracker .bar{flex:1;min-width:20px;height:1px;background:var(--edge2)}
.tracker .bar.on{background:var(--accent)}
.tracker .ph .n{margin-right:6px}
.tracker .ph.on{color:var(--soft)}
.tracker .ph.on::after{content:" ✓";color:var(--good)}
.tracker .ph.now{color:var(--text)}
.tracker .ph.now .n{color:var(--accent)}
.tracker .ph.now::before{content:"▸ ";color:var(--accent);animation:pulse 1.2s infinite}

/* Agent boxes: each wears its maker's look */
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;padding:0 16px 16px}
.card{--c2:var(--c);--ground:var(--panel2);position:relative;border:1px solid color-mix(in srgb,var(--c) 30%,var(--edge));background:var(--ground);padding:14px;min-height:150px;display:flex;flex-direction:column;gap:8px;overflow:hidden;transition:border-color .3s}
.card.working{border-color:var(--c);animation:breathe 1.8s ease-in-out infinite}
@keyframes breathe{50%{border-color:var(--c2);box-shadow:0 0 0 1px color-mix(in srgb,var(--c2) 35%,transparent),0 0 34px -8px var(--c)}}
.card.done{border-color:color-mix(in srgb,var(--c) 70%,transparent)}
.card.error{border-color:var(--bad)}
.card .top{display:flex;align-items:center;gap:10px}
.card .who{display:flex;flex-direction:column;line-height:1.2;min-width:0}
.card .name{font-weight:800;letter-spacing:.06em;color:var(--c);font-size:13px;white-space:nowrap}
.card .role{color:var(--muted);font:10.5px var(--mono);letter-spacing:.14em}
.badge{margin-left:auto;font:700 10px var(--mono);letter-spacing:.12em;padding:3px 7px;background:var(--edge);color:var(--muted);white-space:nowrap}
.working .badge{background:var(--c);color:var(--ground)}
.done .badge{background:color-mix(in srgb,var(--c) 85%,transparent);color:var(--ground)}
.error .badge{background:var(--bad);color:#000}
.track{height:2px;background:var(--edge);overflow:hidden}
.working .track{background:linear-gradient(90deg,transparent,var(--c),var(--c2),transparent) var(--edge);background-size:40% 100%;background-repeat:no-repeat;animation:comet 1.4s linear infinite}
.done .track{background:linear-gradient(90deg,var(--c),var(--c2))}
.error .track{background:var(--bad)}
@keyframes comet{from{background-position:-40% 0}to{background-position:140% 0}}
.stage{color:var(--soft);font-size:12px}
.activity{color:var(--muted);font:11.5px var(--mono);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-height:18px}
.body{font-size:13px;color:var(--soft);max-height:220px;overflow:hidden;mask-image:linear-gradient(transparent,#000 22px);display:flex;flex-direction:column-reverse}
.body .inner{padding-top:6px}
.foot{margin-top:auto;display:flex;gap:10px;color:var(--muted);font:11px var(--mono)}
.foot .model{margin-right:auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:color-mix(in srgb,var(--c) 70%,var(--muted))}
.foot .model::before{content:"◆ ";color:var(--c)}
.cursor{display:inline-block;width:7px;height:14px;background:var(--c);vertical-align:-2px;margin-left:2px;animation:caret 1s steps(2) infinite}
@keyframes caret{50%{opacity:0}}
/* Claude: warm clay on dark, soft corners, a serif voice */
.card.b-claude{border-radius:16px}
.card.b-claude .name{font:400 21px/1 var(--serif);letter-spacing:0;text-transform:none}
/* Gemini: its blue-to-purple gradient as the frame, rounder */
.card.b-gemini{border:1px solid transparent;border-radius:22px;background:linear-gradient(var(--ground),var(--ground)) padding-box,linear-gradient(120deg,var(--c),var(--c2)) border-box}
.card.b-gemini.waiting{background:linear-gradient(var(--ground),var(--ground)) padding-box,linear-gradient(120deg,color-mix(in srgb,var(--c) 35%,var(--edge)),color-mix(in srgb,var(--c2) 35%,var(--edge))) border-box}
.card.b-gemini .name{background:linear-gradient(90deg,var(--c),var(--c2));-webkit-background-clip:text;background-clip:text;color:transparent}
/* ChatGPT: black, square, green and white, monospace */
.card.b-openai{border-radius:4px}
.card.b-openai .name{font:700 13px var(--mono);color:#fff;letter-spacing:.04em}
.card.b-openai .name::first-letter{color:var(--c)}
/* Qwen: heavy purple rule; Copilot: GitHub dark with a gradient top; OpenCode and Ollama: terminal mono */
.card.b-qwen{border-width:2px;border-radius:10px}
.card.b-copilot{border-radius:12px;box-shadow:inset 0 3px 0 -1px var(--c)}
.card.b-copilot .name{background:linear-gradient(90deg,var(--c),var(--c2));-webkit-background-clip:text;background-clip:text;color:transparent}
.card.b-opencode,.card.b-ollama{border-radius:0}
.card.b-opencode .name,.card.b-ollama .name{font:700 13px var(--mono)}
.cards.solo{grid-template-columns:1fr}
.cards.solo .card{min-height:0}
.cards.solo .card .body{display:none}

.verdict{margin:0 16px 16px;padding:20px 24px;border:1px solid var(--edge2);background:var(--ink);display:none}
.verdict.show{display:block}
.verdict h3{margin:0 0 12px;font:400 30px/1 var(--serif);letter-spacing:-.01em}
.verdict h3 i{color:var(--accent);font-style:normal}
.verdict .drafting{font:500 11px var(--mono);letter-spacing:.16em;color:var(--muted);margin-left:12px;vertical-align:middle}
.verdict .text{font-size:15px;line-height:1.68}
.verdict.answer{--hc:var(--c);border-color:color-mix(in srgb,var(--c) 55%,transparent);background:var(--ground)}
.verdict.answer h3 i{color:var(--c)}
.verdict.answer.b-claude{border-radius:16px}
.verdict.answer.b-gemini{border:1px solid transparent;border-radius:22px;background:linear-gradient(var(--ground),var(--ground)) padding-box,linear-gradient(120deg,var(--c),var(--c2)) border-box}
.verdict.answer.b-openai{border-radius:4px}
.meter{display:flex;gap:28px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px solid var(--edge);font:600 11px var(--mono);letter-spacing:.14em;color:var(--muted);align-items:center}
.meter .gauge{display:inline-flex;gap:3px;margin:0 10px;vertical-align:middle}
.meter .gauge i{width:14px;height:5px;background:var(--edge2)}
.meter b{letter-spacing:.06em;font-size:12px}
.meta{margin-top:10px;color:var(--muted);font:11.5px var(--mono)}
.actions{display:flex;gap:8px;flex-wrap:wrap;padding:0 16px 16px;align-items:center}
.btn{border:1px solid var(--edge2);background:var(--panel2);padding:7px 13px;cursor:pointer;font-size:13px;color:var(--soft)}
.btn:hover{border-color:var(--accent);color:var(--text)}
.btn.primary{border-color:var(--accent);color:var(--text);background:color-mix(in srgb,var(--accent) 14%,var(--panel2))}
.applybox{display:inline-flex;align-items:center;gap:0}
.applybox .btn{border-right:0}
.applybox select{border-color:var(--edge2);padding:7px 8px}
.handoff,.problem{margin:0 16px 16px;padding:14px 16px;border:1px solid var(--edge2);background:var(--panel2);font-size:13px}
.problem{border-color:color-mix(in srgb,var(--bad) 60%,transparent)}
.problem b{color:var(--bad)}
.fix{color:var(--accent2);font-weight:700;margin-top:6px}
.fix code{color:var(--text)}
code,pre{font-family:var(--mono);font-size:12.5px}
:not(pre)>code{background:var(--panel2);border:1px solid var(--edge2);padding:1px 5px}
pre{background:#050507;border:1px solid var(--edge2);padding:12px;overflow:auto;white-space:pre-wrap}
.text h3,.text h4,.text h5,.text h6,.body h3,.body h4{margin:14px 0 6px;font-size:15px}
.body h3,.body h4,.body h5,.body h6{font-size:13.5px;margin:10px 0 4px}
.text p{margin:8px 0}
table{border-collapse:collapse;margin:8px 0;font-size:13px}
td,th{border:1px solid var(--edge2);padding:5px 9px;text-align:left}
th{background:var(--panel2)}
blockquote{margin:8px 0;padding-left:12px;border-left:2px solid var(--accent);color:var(--soft)}
details.opinion{margin:0 16px 10px;border:1px solid color-mix(in srgb,var(--c) 30%,var(--edge2));background:var(--ground,var(--panel2))}
details.opinion summary{cursor:pointer;padding:10px 14px;font:700 12px var(--mono);letter-spacing:.08em;color:var(--c)}
details.opinion .text{padding:0 18px 14px;font-size:14px}
.continue{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:0 16px 16px;font:11.5px var(--mono);color:var(--muted)}
.continue .btn{border-color:color-mix(in srgb,var(--c) 45%,transparent);font-size:12px}
.continue .btn b{color:var(--c)}
.plan{margin:0 16px 12px;border:1px solid var(--edge2);background:var(--panel2);padding:10px 14px}
.plan h4{margin:0 0 6px}
.plan .row{display:grid;grid-template-columns:18px 22px 110px 1fr;gap:8px;align-items:center;padding:4px 0;margin:0;font-size:13px;color:var(--muted)}
.plan .row b{color:var(--c);font:700 11px var(--mono);letter-spacing:.06em}
.plan .row.working{color:var(--text)}
.plan .row.working .st{animation:spin 1s linear infinite;display:inline-block}
.plan .row.done .st{color:var(--good)}
.plan .row.error .st{color:var(--bad)}
@keyframes spin{to{transform:rotate(360deg)}}

.hello{padding:46px 24px 30px;text-align:left}
.hello h1{font:400 clamp(40px,6vw,64px)/1 var(--serif);margin:16px 0 12px;letter-spacing:-.02em}
.hello h1 i{color:var(--accent);font-style:normal}
.hello p{max-width:620px;margin:0;color:var(--soft);font-size:15px}
.crowd{display:flex;gap:16px}
.crowd .sprite{width:52px;height:45px;animation:float 2.6s ease-in-out infinite}
.crowd .sprite:nth-child(2n){animation-delay:-1.3s}
@keyframes float{50%{transform:translateY(-6px)}}
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:26px}
.step{border:1px solid var(--edge2);padding:12px 14px;background:var(--panel);font-size:13px;color:var(--soft)}
.step b{display:block;font:700 11px var(--mono);color:var(--accent);letter-spacing:.14em;margin-bottom:4px}
.sprite{width:42px;height:36px;flex:none;image-rendering:pixelated;filter:drop-shadow(0 0 8px color-mix(in srgb,var(--c) 45%,transparent))}
.sprite svg{width:100%;height:100%;display:block;shape-rendering:crispEdges}
.sprite .blink{opacity:0}
.working .sprite{animation:bob .64s steps(2,jump-none) infinite}
.working .sprite .blink{animation:wink 3.4s steps(1) infinite}
@keyframes bob{50%{transform:translateY(-4px)}}
@keyframes wink{0%,94%{opacity:0}95%,100%{opacity:1}}
.card.waiting .sprite{opacity:.55;filter:grayscale(.4)}
.toast{position:fixed;bottom:22px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--accent);padding:10px 16px;opacity:0;transition:.25s;pointer-events:none;font-size:13px;max-width:90vw}
.toast.show{opacity:1}

/* The SP_SYS boot: a second of status lines the first time the page opens */
.boot{position:fixed;inset:0;z-index:50;background:var(--ink);display:flex;align-items:center;justify-content:center;transition:opacity .45s}
.boot pre{background:none;border:0;font:500 13px/1.9 var(--mono);color:var(--accent);margin:0;min-width:min(520px,90vw)}
.boot pre .ok{color:var(--good)}
.boot pre .w{color:var(--text);font-weight:700}
.boot.gone{opacity:0;pointer-events:none}

main,.wrap,.run,.card,.composer{min-width:0}
.body,.verdict .text,.handoff,.problem,.opinion .text{overflow-wrap:anywhere}
@media (max-width:1050px){.cards{grid-template-columns:1fr}.app{grid-template-columns:1fr}aside{display:none}.app.sidebar aside{display:flex;max-height:55vh;border-right:0;border-bottom:1px solid var(--edge)}.modelgrid{grid-template-columns:1fr 1fr}.steps{grid-template-columns:1fr 1fr}}
@media (max-width:720px){.tagline,.pills{display:none}header{gap:10px;padding:12px 14px}main{padding:14px}.runhead{flex-wrap:wrap}
.runhead h2{flex-basis:100%}.modelgrid{grid-template-columns:1fr}.verdict{padding:16px}.go{margin-left:0;width:100%}}
</style>
</head>
<body>
<div class="boot" id="boot"><pre id="bootlog"></pre></div>
<div class="app" id="app">
<header>
  <div class="logo"><span class="mark">[SP<i>_</i>]</span><span class="wordmark">Conclave<i>.</i></span></div>
  <span class="tagline">SP_SYS // COUNCIL OS · v__VERSION__ · <span class="live">LIVE</span></span>
  <div class="spacer"></div>
  <div class="pills" id="pills"></div>
  <select class="field" id="theme" title="Theme"></select>
  <button class="iconbtn" id="menu" title="Projects">☰</button>
  <button class="iconbtn" id="quit" title="Stop Conclave">QUIT</button>
</header>
<aside>
  <section>
    <h3 class="mod"><b>01</b>Projects</h3>
    <input class="field" id="folder" placeholder="Paste any folder path and press Enter">
    <div class="list" id="projects" style="margin-top:8px"></div>
  </section>
  <section>
    <h3 class="mod"><b>02</b>Claude Code conversations</h3>
    <div class="list" id="convos"><div class="empty">Pick a project to see its conversations.</div></div>
  </section>
</aside>
<main>
  <div class="wrap">
    <div class="composer hud">
      <div class="mod"><span><b>03</b><span id="brieftitle">Brief the council</span></span><span id="destnote"></span></div>
      <div class="chips" id="context"></div>
      <div class="sendto" id="sendto"></div>
      <div class="lineup" id="lineup"></div>
      <textarea id="question" placeholder="What should the council look at? For example: why does the build keep failing, and what's the fix?"></textarea>
      <div class="row">
        <button class="quick">Why does this keep failing, and what's the fix?</button>
        <button class="quick">Review Claude's last change for bugs.</button>
        <button class="quick">Is this the right approach? What would you do instead?</button>
        <button class="quick">What should we try next?</button>
      </div>
      <div class="row">
        <details class="models"><summary>⚙ MODELS · TIMEOUT</summary>
          <div class="modelgrid" id="modelgrid"></div>
        </details>
        <span class="access" id="access" title="What a hand-off may do. The council itself always only reads.">
          <button data-level="read">READ</button><button data-level="edit">EDIT</button><button data-level="full">FULL</button></span>
        <label class="toggle council-only" title="Whether Claude may split a big task into subtasks for the agents best suited">SPLIT <select class="field" id="split"><option value="auto">auto</option><option value="always">always</option><option value="never">never</option></select></label>
        <label class="toggle council-only" title="Advisers critique each other's opinions before Claude decides (slower, sharper)"><input type="checkbox" id="cross"> CROSS-EXAM</label>
        <label class="toggle" title="Keep rounds as sessions you can continue in each agent's own tool"><input type="checkbox" id="save"> SAVE SESSIONS</label>
        <button class="go" id="ask">Ask the council <kbd>⌘↵</kbd></button>
      </div>
    </div>
    <div id="runs"></div>
    <div class="hello" id="hello">
      <div class="crowd" id="crowd"></div>
      <h1>Many minds, one answer<i>.</i></h1>
      <p>Claude leads. The advisers read your project and your Claude Code conversation, argue it out, and Claude writes the verdict. Or send a task straight to one agent, let it edit the project, and keep the conversation going.</p>
      <div class="steps"><div class="step"><b>01</b>Pick a project</div><div class="step"><b>02</b>Attach a conversation</div><div class="step"><b>03</b>Ask the council or one agent</div><div class="step"><b>04</b>Apply it, or hand it back</div></div>
    </div>
  </div>
</main>
</div>
<div class="toast" id="toast"></div>
<script>
const TOKEN = "__TOKEN__";
const state = {info: null, project: null, conversation: null, convos: [], history: [], agents: {}, advisers: [], target: null,
               access: "read", threads: {}};
const AG = id => state.agents[id] || {id, label: id.toUpperCase(), name: id, role: "ADVISER", sprites: {}, style: {}};
const short = id => AG(id).label.replace(/^\S+\s+/, "");
const vars = id => { const s = AG(id).style || {}; return `--c:var(--${id});--c2:${s.c2 || `var(--${id})`};--ground:${s.ground || "var(--panel2)"}`; };

// A pixel sprite as SVG. Pixels: b body, d darker, a lighter, e eyes, w white, x error eyes.
function spriteSVG(id, mood) {
  const frames = AG(id).sprites, body = mood === "error" ? "var(--bad)" : `var(--${id})`;
  const fill = {b: body, d: `color-mix(in srgb, ${body} 55%, var(--ink))`, a: `color-mix(in srgb, ${body} 45%, white)`,
                e: "var(--ink)", w: "#fff", x: "#fff"};
  const layer = (rows, cls) => `<g class="${cls}">` + (rows || []).map((row, y) => [...row].map((p, x) =>
    fill[p] ? `<rect x="${x}" y="${y}" width="1.02" height="1.02" style="fill:${fill[p]}"/>` : "").join("")).join("") + "</g>";
  const base = {working: "idle", done: "done", error: "error"}[mood] || "sleep";
  return `<svg viewBox="0 0 7 6">${layer(frames[base], "base")}${mood === "working" ? layer(frames.blink, "blink") : ""}</svg>`;
}
const sprite = (id, mood) => `<div class="sprite" style="--c:var(--${id})">${spriteSVG(id, mood)}</div>`;
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
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.add("show"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 3200); }
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
  const t = state.info.themes[name] || state.info.themes.sys, r = document.documentElement.style;
  const mix = (a, b, p) => `color-mix(in srgb, ${a} ${p}%, ${b})`;
  r.setProperty("--ink", t.ink); r.setProperty("--edge2", t.edge); r.setProperty("--edge", mix(t.edge, t.ink, 65));
  r.setProperty("--panel", t.panel); r.setProperty("--panel2", mix(t.edge, t.panel, 28));
  ["accent", "accent2", "muted", "soft", "good", "warn", "bad"].forEach(k => r.setProperty("--" + k, t[k]));
  r.setProperty("--text", t.fg);
  Object.entries(t.colors).forEach(([k, v]) => r.setProperty("--" + k, v));
  store.set("theme", name);
}

function bootSequence() {
  const box = $("#boot");
  let seen = false; try { seen = sessionStorage.getItem("booted"); sessionStorage.setItem("booted", "1"); } catch {}
  if (seen || matchMedia("(prefers-reduced-motion: reduce)").matches) { box.remove(); return; }
  const members = ["claude", ...state.advisers].map(id => AG(id).name.toUpperCase()).join(" · ");
  const online = ["claude", ...state.advisers].filter(id => AG(id).installed).length;
  const lines = [["CONCLAVE // COUNCIL OS ", "OK"], ["MOUNTING CLAUDE CODE INDEX ... ", `${(state.projects || []).length} PROJECTS`],
                 ["SEATING COUNCIL ... ", members], ["LINKING PROVIDERS ... ", `${online}/${state.advisers.length + 1} ONLINE`],
                 ["OPERATOR ... ", "AUTHENTICATED"]];
  let html = "", i = 0;
  const step = () => {
    if (i < lines.length) { html += `${esc(lines[i][0])}<span class="ok">${esc(lines[i][1])}</span>\n`; i++; $("#bootlog").innerHTML = html; setTimeout(step, 150); }
    else { $("#bootlog").innerHTML = html + `<span class="w">&gt; WELCOME.</span>`; setTimeout(() => { box.classList.add("gone"); setTimeout(() => box.remove(), 500); }, 380); }
  };
  step();
}

async function boot() {
  state.info = await api("/api/info");
  const themes = $("#theme");
  themes.innerHTML = Object.keys(state.info.themes).map(n => `<option>${n}</option>`).join("");
  const savedTheme = store.get("theme", state.info.theme);
  themes.value = state.info.themes[savedTheme] ? savedTheme : state.info.theme; applyTheme(themes.value);
  themes.onchange = () => applyTheme(themes.value);
  state.info.agents.forEach(a => state.agents[a.id] = a);
  state.advisers = store.get("advisers", state.info.advisers).filter(a => state.agents[a] && a !== "claude");
  if (!state.advisers.length) state.advisers = state.info.advisers;
  state.target = state.agents[store.get("target", null)] ? store.get("target", null) : null;
  state.access = store.get("access", "read"); if (state.access === "full") state.access = "read";  // full is asked for each visit
  $("#split").value = store.get("split", state.info.split);
  $("#split").onchange = () => store.set("split", $("#split").value);
  renderCouncil();
  const saved = store.get("models", {});
  $("#modelgrid").innerHTML = state.info.agents.map(p =>
    `<label style="${vars(p.id)}"><span style="color:var(--c)">${esc(short(p.id))}</span><input class="field" data-model="${p.id}" placeholder="${esc(p.model || "default model")}" value="${esc(saved[p.id] || "")}"></label>`).join("")
    + `<label>TIMEOUT (s)<input class="field" id="timeout" type="number" min="10" value="${store.get("timeout", state.info.timeout)}"></label>`;
  $("#modelgrid").oninput = () => { const m = {}; document.querySelectorAll("[data-model]").forEach(e => m[e.dataset.model] = e.value.trim());
    store.set("models", m); store.set("timeout", +$("#timeout").value || state.info.timeout); renderCouncil(); };
  $("#save").checked = store.get("saveSessions", state.info.saveSessions);
  $("#cross").checked = store.get("crossExamine", state.info.crossExamine);
  $("#cross").onchange = () => store.set("crossExamine", $("#cross").checked);
  $("#save").onchange = () => store.set("saveSessions", $("#save").checked);
  document.querySelectorAll("#access button").forEach(b => b.onclick = () => setAccess(b.dataset.level));
  api("/api/health").then(report => { state.health = Object.fromEntries(report.map(h => [h.id, h])); renderCouncil(); }).catch(() => {});
  await loadProjects();
  bootSequence();
  const last = store.get("project", null);
  const pick = last && state.projects.find(p => p.path === last) ? last : (state.projects.find(p => p.path === state.info.cwd) ? state.info.cwd : null);
  if (pick) selectProject(pick);
  renderContext();
}

function setAccess(level) {
  if (level === "full" && state.access !== "full" && !confirm(
    "Full access lets the agent run any command on this computer without asking: install things, delete files, use your signed-in tools.\n\n" +
    "It is told to ask before anything irreversible, such as sending a message, but nothing enforces that. Use it in a project you've committed.\n\nGive hand-offs full access?")) return;
  state.access = level; store.set("access", level); renderCouncil();
}

const modelOf = id => store.get("models", {})[id] || AG(id).model || "default model";

// The header pills, the Send-to row, the council seats, the access switch and the button label.
function renderCouncil() {
  const members = ["claude", ...state.advisers], health = state.health || {};
  $("#pills").innerHTML = (state.target ? [state.target] : members).map(id => { const a = AG(id), h = health[id] || {installed: a.installed};
    const cls = !h.installed ? "missing" : h.signedIn === false ? "unsigned" : "";
    const tip = !h.installed ? (a.installCommand || a.install) : h.signedIn === false ? h.detail : `${h.version || a.cli} · ${a.strengths}`;
    return `<div class="pill ${cls}" style="${vars(id)}" title="${esc(tip)}"><i></i><b>${esc(short(id))}</b>${esc(modelOf(id))}</div>`; }).join("");
  $("#sendto").innerHTML = `<span class="lbl">SEND TO</span><span class="dest council ${state.target ? "" : "on"}" data-dest="">■ COUNCIL</span>`
    + state.info.agents.map(a => `<span class="dest ${state.target === a.id ? "on" : ""} ${a.installed ? "" : "absent"}" data-dest="${a.id}" style="${vars(a.id)}"
      title="${esc(a.name + " · " + modelOf(a.id) + (a.installed ? "" : " · not installed: " + (a.installCommand || a.install)))}">${sprite(a.id, state.target === a.id ? "done" : "sleep")}${esc(short(a.id))}</span>`).join("");
  $("#sendto").querySelectorAll("[data-dest]").forEach(el => el.onclick = () => {
    state.target = el.dataset.dest || null; store.set("target", state.target);
    if (state.target && !AG(state.target).installed) toast(`${AG(state.target).name} isn't installed. In a terminal, run: ${AG(state.target).installCommand || AG(state.target).install}`);
    renderCouncil(); renderContext(); });
  $("#lineup").style.display = state.target ? "none" : "";
  document.querySelectorAll(".council-only").forEach(el => el.style.display = state.target ? "none" : "");
  $("#lineup").innerHTML = `<span>COUNCIL:</span>` + state.info.agents.map(a => {
    const seated = a.id === "claude" || state.advisers.includes(a.id);
    return `<span class="seat ${seated ? "on" : ""} ${a.installed ? "" : "absent"} ${a.id === "claude" ? "lead" : ""}" data-seat="${a.id}" style="${vars(a.id)}"
      title="${esc(a.name + ": " + a.strengths + (a.installed ? "" : " · not installed: " + (a.installCommand || a.install)))}">${sprite(a.id, seated ? "done" : "sleep")}${esc(short(a.id))}</span>`; }).join("");
  $("#lineup").querySelectorAll("[data-seat]").forEach(el => el.onclick = () => {
    const id = el.dataset.seat; if (id === "claude") return;
    if (state.advisers.includes(id)) { if (state.advisers.length === 1) return toast("The council needs at least one adviser");
      state.advisers = state.advisers.filter(a => a !== id); }
    else { state.advisers = [...state.advisers, id]; if (!AG(id).installed) toast(`${AG(id).name} isn't installed. In a terminal, run: ${AG(id).installCommand || AG(id).install}`); }
    store.set("advisers", state.advisers); renderCouncil(); });
  const access = $("#access"); access.classList.toggle("off", !state.target);
  access.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.level === (state.target ? state.access : "read")));
  access.title = state.target ? state.info.access[state.access] : "The council always only reads. Pick one agent under Send to for edit or full access.";
  $("#ask").innerHTML = (state.target ? `Send to ${esc(AG(state.target).name)}` : "Ask the council") + " <kbd>⌘↵</kbd>";
  $("#brieftitle").textContent = state.target ? `Brief ${AG(state.target).name}` : "Brief the council";
  $("#destnote").textContent = state.target ? `${AG(state.target).name.toUpperCase()} · ${state.access.toUpperCase()} ACCESS · ${modelOf(state.target)}`
    : `${members.length} MINDS · ONE ANSWER`;
  const crowd = $("#crowd"); if (crowd) crowd.innerHTML = members.map(id => sprite(id, "done")).join("");
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
    if (data.project !== state.project) state.threads = {};  // agents' conversations belong to the project they were in
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
  if (state.target && state.threads[state.target]) chips.push(`<span class="chip live" style="${vars(state.target)};border-color:var(--c)">↺ <span>carrying on ${esc(AG(state.target).name)}'s conversation</span><button data-x="thread" title="Start a new conversation with it">✕</button></span>`);
  else if (state.history.length) chips.push(`<span class="chip">↺ follow-up remembers ${Math.min(state.history.length, 3)} answer${state.history.length > 1 ? "s" : ""}<button data-x="history" title="Start fresh">✕</button></span>`);
  $("#context").innerHTML = chips.join("");
  $("#context").querySelectorAll("[data-x]").forEach(b => b.onclick = () => {
    if (b.dataset.x === "project") { state.project = null; state.conversation = null; state.convos = []; state.threads = {}; store.set("project", null); renderConvos(); loadProjects(); }
    if (b.dataset.x === "conversation") { state.conversation = null; renderConvos(); }
    if (b.dataset.x === "history") state.history = [];
    if (b.dataset.x === "thread") delete state.threads[state.target];
    renderContext(); });
}

function card(name, role) {
  return `<div class="card waiting b-${name}" data-name="${name}" style="${vars(name)}">
    <div class="top">${sprite(name, "sleep")}<span class="who"><span class="name">${esc(name === "claude" ? "Claude" : short(name))}</span><span class="role">${esc(role)}</span></span><span class="badge">STANDBY</span></div>
    <div class="track"></div><div class="stage">standing by</div><div class="activity"></div>
    <div class="body"><div class="inner"></div></div>
    <div class="foot"><span class="model">${esc(modelOf(name))}</span><span class="tok"></span><span class="time"></span></div></div>`;
}

// Send a question: to the council, or straight to one agent. `apply` carries out an earlier verdict.
async function ask(opts = {}) {
  const question = opts.question || $("#question").value.trim();
  if (!question) { $("#question").focus(); return; }
  const models = store.get("models", {}), timeout = store.get("timeout", state.info.timeout);
  const agent = opts.agent !== undefined ? opts.agent : state.target;
  const access = agent ? (opts.access || state.access) : "read";
  const advisers = [...state.advisers], split = $("#split").value, cross = !agent && $("#cross").checked && advisers.length > 1;
  let id;
  try { id = (await api("/api/runs", {question, project: state.project, conversation: state.conversation, models, timeout, history: state.history.slice(-3),
    saveSessions: $("#save").checked, crossExamine: $("#cross").checked, advisers, split, agent, access,
    thread: agent ? state.threads[agent] || "" : "", apply: opts.apply || ""})).id; }
  catch (e) { toast(e.message); return; }
  $("#hello")?.remove(); if (!opts.question) $("#question").value = "";
  const conv = state.convos.find(c => c.id === state.conversation);
  const el = document.createElement("section"); el.className = "run hud";
  const phases = agent ? [["solo", `${AG(agent).name.toUpperCase()} · HAND-OFF · ${access.toUpperCase()}`]] : phaseList(cross);
  const members = agent ? [agent] : ["claude", ...advisers];
  el.innerHTML = `<div class="runhead"><div style="flex:1;min-width:0"><div class="mod"><b>${agent ? "→" : "■"}</b>${agent ? esc(AG(agent).name) + " · " + access + " access" : "Council round"}</div><h2>${esc(question)}</h2></div><span class="clock">0:00</span><button class="btn" data-act="cancel">Cancel</button></div>
    <div class="chips" style="padding:0 20px">${state.project ? `<span class="chip">⌂ <span>${esc(state.project.split("/").pop())}</span></span>` : ""}${conv && !opts.apply ? `<span class="chip">❝ <span>${esc(conv.title)}</span></span>` : ""}</div>
    <div class="tracker">${trackerHTML(phases)}</div>
    <div class="planbox"></div>
    <div class="cards ${agent ? "solo" : ""}" style="${agent ? "" : `grid-template-columns:repeat(${Math.min(members.length, 4)},1fr)`}">${members.map(n => card(n, agent ? "HAND-OFF" : n === "claude" ? "LEAD" : "ADVISER")).join("")}</div>
    <div class="problems"></div>
    <div class="verdict ${agent ? `answer b-${agent}` : "hud"}" style="${agent ? vars(agent) : ""}"><h3>${agent ? esc(AG(agent).name) : "Verdict"}<i>.</i><span class="drafting">${agent ? "WORKING…" : "DRAFTING…"}</span></h3><div class="text"></div><div class="meterbox"></div></div>
    <div class="actions"></div><div class="continue"></div><div class="opinions"></div>`;
  $("#runs").prepend(el);
  follow(id, el, question, phases, agent);
}

const phaseList = cross => [["brief", "BRIEF"], ["advise", "DELIBERATION"], ...(cross ? [["cross", "CROSS-EXAM"]] : []), ["verdict", "VERDICT"]];
const SPLIT_PHASES = [["brief", "PLAN"], ["work", "SUBTASKS"], ["verdict", "VERDICT"]];
const trackerHTML = list => list.map(([k, label], i) => `${i ? '<span class="bar"></span>' : ""}<span class="ph" data-ph="${k}"><span class="n">${String(i + 1).padStart(2, "0")}</span>${label}</span>`).join("");

function follow(id, el, question, phaseDefs, agent) {
  const run = {phase: phaseDefs[0][0], started: Date.now(), cards: {}, done: false, tokens: {}};
  const speaker = agent || "claude";
  const cardEl = n => el.querySelector(`.card[data-name="${n}"]`);
  const tick = setInterval(() => {
    if (!run.done) el.querySelector(".clock").textContent = clock((Date.now() - run.started) / 1000);
    for (const [n, c] of Object.entries(run.cards)) if (c.start && !c.end) cardEl(n).querySelector(".time").textContent = clock((Date.now() - c.start) / 1000);
  }, 250);
  let phases = [...phaseDefs.map(p => p[0]), "done"];
  const mood = (n, m) => { const box = cardEl(n)?.querySelector(".sprite"); if (box) box.innerHTML = spriteSVG(n, m); };
  const setPhase = p => { run.phase = p; const at = phases.indexOf(p);
    el.querySelectorAll(".ph").forEach((ph, i) => ph.className = "ph" + (i < at ? " on" : i === at ? " now" : ""));
    el.querySelectorAll(".tracker .bar").forEach((b, i) => b.classList.toggle("on", i < at));
    if (p === "verdict" || p === "solo") el.querySelector(".verdict").classList.add("show"); };
  setPhase(run.phase);
  const events = new EventSource(`/api/runs/${id}/events?token=${encodeURIComponent(TOKEN)}`);
  events.onmessage = msg => {
    const e = JSON.parse(msg.data), c = e.name && cardEl(e.name);
    if (e.type === "phase") setPhase(e.phase);
    if (e.type === "mode" && e.mode === "split") { phases = [...SPLIT_PHASES.map(p => p[0]), "done"];
      el.querySelector(".tracker").innerHTML = trackerHTML(SPLIT_PHASES); setPhase(run.phase); }
    if (e.type === "plan") el.querySelector(".planbox").innerHTML = `<div class="plan"><h4 class="mod"><b>■</b>Claude's plan</h4>` + e.subtasks.map((t, i) =>
      `<div class="row" data-sub="${i}" style="${vars(t.agent)}"><span>${i + 1}</span><span class="st">○</span><b>${esc(short(t.agent))}</b><span>${esc(t.title)}</span></div>`).join("") + "</div>";
    if (e.type === "subtask") { const row = el.querySelector(`[data-sub="${e.index}"]`);
      if (row) { row.className = "row " + e.status; row.querySelector(".st").textContent = {working: "◌", done: "✓", error: "✗"}[e.status] || "○"; row.title = e.preview || ""; } }
    if (!c) { if (["done", "cancelled", "failed"].includes(e.type)) end(e); return; }
    if (e.type === "start") { run.cards[e.name] = {start: Date.now()}; c.className = `card working b-${e.name}`; c.querySelector(".badge").textContent = "▸ ACTIVE"; mood(e.name, "working");
      c.querySelector(".stage").textContent = e.stage; c.querySelector(".activity").textContent = e.activity || "";
      c.querySelector(".inner").innerHTML = e.preview ? md(e.preview) : ""; }
    if (e.type === "stream") {
      if (e.activity) c.querySelector(".activity").textContent = "› " + e.activity;
      if (e.model) c.querySelector(".model").textContent = e.model;
      if (e.name === speaker && (agent || run.phase === "verdict")) el.querySelector(".verdict .text").innerHTML = md(agent ? e.text : drafting(e.text)) + `<span class="cursor" style="${agent ? "" : "--c:var(--accent)"}"></span>`;
      else { c.querySelector(".inner").innerHTML = md(e.name === "claude" ? drafting(e.text) : e.text) + '<span class="cursor"></span>';
        if (!run.tokens[e.name] && e.text) c.querySelector(".tok").textContent = "~" + count(Math.round(e.text.length / 4)) + " tok"; } }
    if (e.type === "finish") { if (run.cards[e.name]) run.cards[e.name].end = Date.now(); c.className = `card ${e.status} b-${e.name}`; mood(e.name, e.status);
      c.querySelector(".badge").textContent = {done: "✓ DONE", error: "✗ FAULT"}[e.status] || "STANDBY"; c.querySelector(".stage").textContent = e.stage;
      c.querySelector(".activity").textContent = ""; c.querySelector(".inner").innerHTML = e.status === "error" ? `<span class="err">${esc(e.preview)}</span>` : md(e.preview); }
    if (e.type === "tokens") { const t = run.tokens[e.name] || [0, 0]; run.tokens[e.name] = [t[0] + e.in, t[1] + e.out];
      c.querySelector(".tok").textContent = `↑${count(run.tokens[e.name][0])} ↓${count(run.tokens[e.name][1])}`; }
    if (["done", "cancelled", "failed"].includes(e.type)) end(e);
  };
  const end = e => { events.close(); clearInterval(tick); run.done = true; setPhase("done");
    el.querySelector('[data-act="cancel"]')?.remove(); finish(el, e, question); };
  events.onerror = () => { if (run.done) events.close(); };
  el.querySelector('[data-act="cancel"]').onclick = () => api(`/api/runs/${id}/cancel`, {}).catch(e => toast(e.message));
  el.dataset.run = id;
}

function finish(el, e, question) {
  const actions = el.querySelector(".actions"), problems = el.querySelector(".problems");
  if (e.type === "cancelled") { problems.innerHTML = `<div class="problem"><b>Cancelled.</b> The model calls were stopped.</div>`; return; }
  if (e.type === "failed") { problems.innerHTML = `<div class="problem"><b>Something went wrong:</b> ${esc(e.message)}</div>`; return; }
  const r = e.result, solo = r.mode === "solo";
  problems.innerHTML = r.errors.map(x => `<div class="problem"><b>✗ ${esc(x.headline)}</b><div>${esc(x.message)}</div>${x.fix ? `<div class="fix">→ ${inline(x.fix)}</div>` : ""}</div>`).join("");
  if (!r.final) { el.querySelector(".verdict").classList.remove("show"); return; }
  if (solo && r.thread) { state.threads[r.speaker] = r.thread; renderContext(); }
  const v = el.querySelector(".verdict"); v.classList.add("show"); v.querySelector(".drafting").remove();
  v.querySelector(".text").innerHTML = md(r.final);
  const levels = {strong: [11, "var(--good)"], partial: [7, "var(--warn)"], split: [3, "var(--bad)"]}, dots = {high: 3, medium: 2, low: 1};
  let meter = "";
  if (r.agreement) { const [n, col] = levels[r.agreement];
    meter += `<span>AGREEMENT<span class="gauge">${Array.from({length: 12}, (_, i) => `<i style="${i < n ? `background:${col};box-shadow:0 0 6px ${col}` : ""}"></i>`).join("")}</span><b style="color:${col}">${r.agreement}</b></span>`; }
  if (r.confidence) { const n = dots[r.confidence];
    meter += `<span>CONFIDENCE<span class="gauge">${[0, 1, 2].map(i => `<i style="width:9px;height:9px;${i < n ? "background:var(--accent)" : ""}"></i>`).join("")}</span><b style="color:var(--accent)">${r.confidence}</b></span>`; }
  v.querySelector(".meterbox").innerHTML = (meter ? `<div class="meter">${meter}</div>` : "") + `<div class="meta">${esc(r.meta)}</div>`;
  const applyChoices = state.info.agents.map(a => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
  actions.innerHTML = (solo ? "" : `<span class="applybox"><button class="btn primary" data-act="apply" title="Have an agent make these changes in the project, at edit access (or full, if you chose it)">⚙ Apply with</button><select class="field" data-act="applyto">${applyChoices}</select></span>`)
    + `<button class="btn ${solo ? "primary" : ""}" data-act="handoff">↩ Hand back to Claude Code</button><button class="btn" data-act="copy">Copy</button>
    <button class="btn" data-act="follow">${solo ? `Carry on with ${esc(AG(r.speaker).name)}` : "Ask a follow-up"}</button>`;
  actions.querySelector('[data-act="copy"]').onclick = () => navigator.clipboard.writeText(r.final).then(() => toast("Copied"));
  actions.querySelector('[data-act="follow"]').onclick = () => {
    if (solo) { state.target = r.speaker; store.set("target", state.target); renderCouncil(); renderContext(); }
    $("#question").focus(); window.scrollTo({top: 0, behavior: "smooth"}); document.querySelector("main").scrollTo({top: 0, behavior: "smooth"}); };
  const apply = actions.querySelector('[data-act="apply"]');
  if (apply) apply.onclick = () => { const to = actions.querySelector('[data-act="applyto"]').value;
    const access = state.access === "full" ? "full" : "edit";
    if (!confirm(`${AG(to).name} will make the verdict's changes in ${state.project || state.info.cwd} at ${access} access. Go ahead?`)) return;
    ask({question: `Apply: ${question}`, agent: to, access, apply: el.dataset.run}); };
  actions.querySelector('[data-act="handoff"]').onclick = async () => {
    try { const h = await api(`/api/runs/${el.dataset.run}/handoff`, {});
      let box = el.querySelector(".handoff"); if (!box) { box = document.createElement("div"); box.className = "handoff"; actions.after(box); }
      box.innerHTML = `Saved <code>${esc(h.path)}</code>. Run this in Terminal to continue the Claude Code conversation with it:<pre>${esc(h.command)}</pre>`;
      navigator.clipboard.writeText(h.command).then(() => toast("Command copied — paste it in Terminal"), () => {});
    } catch (err) { toast(err.message); } };
  const saved = Object.keys(r.sessions || {});
  const cont = el.querySelector(".continue");
  if (saved.length) {
    cont.innerHTML = `<span>↪ CONTINUE IN</span>` + saved.map(n => `<button class="btn" style="${vars(n)}" data-tool="${n}"><b>${esc(AG(n).label.split(" ")[0])}</b> ${esc(AG(n).name)}</button>`).join("");
    cont.querySelectorAll("[data-tool]").forEach(b => b.onclick = async () => {
      try { const res = await api(`/api/runs/${el.dataset.run}/continue`, {provider: b.dataset.tool});
        if (res.opened) toast(`Opened in Terminal: ${AG(b.dataset.tool).name}`);
        else navigator.clipboard.writeText(res.command).then(() => toast("Command copied: paste it in a terminal"), () => toast(res.command));
      } catch (err) { toast(err.message); } });
  }
  const split = r.mode === "split";
  el.querySelector(".opinions").innerHTML = solo ? "" : [["brief", "claude", split ? "■ CLAUDE · PLAN" : "■ CLAUDE · BRIEF", r.brief],
    ...Object.entries(r.opinions).map(([n, t]) => [n, n, `${AG(n).label} · ${split ? "SUBTASKS" : "OPINION"}`, t])]
    .filter(x => x[3]).map(([, n, title, t]) => `<details class="opinion" style="${vars(n)}"><summary>${esc(title)}</summary><div class="text">${md(t)}</div></details>`).join("");
  if (!solo) { state.history.push([question, r.final]); renderContext(); }
}

$("#ask").onclick = () => ask();
$("#question").addEventListener("keydown", e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); ask(); } });
document.querySelectorAll(".quick").forEach(b => b.onclick = () => { $("#question").value = b.textContent; $("#question").focus(); });
$("#folder").addEventListener("keydown", e => { if (e.key === "Enter" && e.target.value.trim()) selectProject(e.target.value.trim()); });
$("#menu").onclick = () => $("#app").classList.toggle("sidebar");
$("#quit").onclick = async () => { if (!confirm("Stop Conclave? Running rounds will be cancelled.")) return;
  await api("/api/shutdown", {}).catch(() => {}); document.body.innerHTML = '<div class="hello"><h1>The council is adjourned<i>.</i></h1><p>You can close this tab.</p></div>'; };
boot().catch(e => { $("#boot")?.remove(); toast(e.message); });
</script>
</body>
</html>
"""


if __name__ == "__main__":
    raise SystemExit(main())
