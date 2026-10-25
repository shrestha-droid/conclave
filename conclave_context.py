"""Bring a project and its conversations, from Claude Code, Gemini CLI or Codex, to the council.

Where each tool keeps a conversation:

- Claude Code: JSON lines in ~/.claude/projects/<project path with / replaced by ->/<session id>.jsonl.
- Gemini CLI: ~/.gemini/tmp/<project slug>/chats/session-*.jsonl (older versions: one .json file), where the slug
  folder holds a .project_root file naming the project (older versions named the folder by the path's sha256).
- Codex: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl, whose first line (session_meta) names the folder it ran in.

This module lists those conversations, condenses one into a transcript the advisers can read (errors first), and
snapshots the project's git state.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def gemini_home() -> Path:
    return Path.home() / ".gemini"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


TOOLS = ("claude", "gemini", "openai")
TOOL_NAMES = {"claude": "Claude Code", "gemini": "Gemini CLI", "openai": "Codex"}
SPEAKERS = {"claude": "Claude", "gemini": "Gemini", "openai": "ChatGPT"}


def store_name(project: Path) -> str:
    """The folder name Claude Code uses for a project: every character that is not a letter or digit becomes '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(project))


@dataclass
class Conversation:
    id: str
    path: Path
    project: str  # the folder Claude Code was working in
    title: str
    updated: float
    messages: int
    errors: int
    branch: str = ""
    tool: str = "claude"  # whose conversation: claude, gemini or openai (Codex)

    @property
    def when(self) -> str:
        return ago(self.updated)

    @property
    def tool_name(self) -> str:
        return TOOL_NAMES[self.tool]


def ago(timestamp: float) -> str:
    seconds = max(0, datetime.now().timestamp() - timestamp)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


def entries(path: Path):
    """Yield each JSON object in a transcript, skipping lines that don't parse."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return


def text_of(content) -> str:
    """Plain text from a message's content, which is either a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(block.get("text", "") for block in content if isinstance(block, dict) and block.get("type") == "text")
    return ""


def is_prompt(entry: dict) -> bool:
    """A message the person typed, as opposed to tool results, slash-command output or injected context."""
    if entry.get("type") != "user" or entry.get("isSidechain") or entry.get("isMeta"):
        return False
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
        return False
    text = text_of(content).strip()
    return bool(text) and not text.startswith(("<command-", "<local-command", "<system-reminder", "Caveat:"))


def summarize(path: Path) -> Conversation | None:
    title = summary = project = branch = ""
    messages = errors = 0
    for entry in entries(path):
        kind = entry.get("type")
        if kind == "summary" and entry.get("summary"):
            summary = str(entry["summary"])
        elif kind == "custom-title" and entry.get("customTitle"):
            summary = str(entry["customTitle"])
        if kind in ("user", "assistant") and not entry.get("isSidechain"):
            project = entry.get("cwd") or project
            branch = entry.get("gitBranch") or branch
            if is_prompt(entry) or kind == "assistant":
                messages += 1
            if is_prompt(entry) and not title:
                title = text_of((entry.get("message") or {}).get("content")).strip()
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, list):
                errors += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error"))
    if not messages:
        return None
    title = " ".join((summary or title or "(untitled)").split())
    return Conversation(path.stem, path, project, title[:120], path.stat().st_mtime, messages, errors, branch)


def projects() -> list[tuple[str, float, int]]:
    """Every project any of the tools has a conversation for: (folder, last activity, number of conversations), newest
    first."""
    found: dict[str, tuple[float, int]] = {}

    def add(folder: str, updated: float, count: int) -> None:
        before, total = found.get(folder, (0.0, 0))
        found[folder] = (max(before, updated), total + count)

    for folder, files in gemini_stores():
        if files:
            add(folder, max(f.stat().st_mtime for f in files), len(files))
    for path, meta in codex_sessions():
        if meta.get("cwd"):
            add(str(meta["cwd"]), path.stat().st_mtime, 1)
    root = claude_home() / "projects"
    for store in root.iterdir() if root.is_dir() else []:
        files = sorted(store.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True) if store.is_dir() else []
        if not files:
            continue
        folder = ""
        for entry in entries(files[0]):
            if entry.get("cwd"):
                folder = entry["cwd"]
                break
        if not folder:
            continue
        add(folder, files[0].stat().st_mtime, len(files))
    return sorted(((folder, updated, total) for folder, (updated, total) in found.items()), key=lambda item: -item[1])


def conversations(project: Path | str | None = None, limit: int = 30,
                  tools: tuple[str, ...] = TOOLS) -> list[Conversation]:
    """Conversations for one project (or all of them) from each tool in `tools`, newest first."""
    found: list[Conversation] = []
    if "claude" in tools:
        found += claude_conversations(project, limit)
    if "gemini" in tools:
        found += gemini_conversations(project, limit)
    if "openai" in tools:
        found += codex_conversations(project, limit)
    return sorted(found, key=lambda c: -c.updated)[:limit]


def claude_conversations(project: Path | str | None = None, limit: int = 30) -> list[Conversation]:
    root = claude_home() / "projects"
    if not root.is_dir():
        return []
    if project is not None:
        project = Path(project).expanduser().resolve()
        stores = [root / store_name(project)]
        if not stores[0].is_dir():  # older Claude Code versions encoded paths slightly differently
            stores = [s for s in root.iterdir() if s.is_dir()]
    else:
        stores = [s for s in root.iterdir() if s.is_dir()]
    files = sorted((f for s in stores for f in s.glob("*.jsonl")), key=lambda p: p.stat().st_mtime, reverse=True)
    found = []
    for path in files:
        summary = summarize(path)
        if summary is None:
            continue
        if project is not None and summary.project and Path(summary.project).resolve() != project:
            continue
        found.append(summary)
        if len(found) >= limit:
            break
    return found


def find(project: Path | str | None, key: str, tools: tuple[str, ...] = TOOLS) -> Conversation | None:
    """Pick a conversation by 'latest', its number in the list (1 = newest) or the start of its session id."""
    listed = conversations(project, limit=200, tools=tools)
    if not listed:
        return None
    if key in ("", "latest", "last"):
        return listed[0]
    if key.isdigit() and 1 <= int(key) <= len(listed):
        return listed[int(key) - 1]
    matches = [c for c in listed if c.id.startswith(key)]
    return matches[0] if len(matches) == 1 else None


def clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit // 2] + "\n… [trimmed] …\n" + text[-limit // 2:]


def describe_tool(name: str, data) -> str:
    """One line for a tool call, e.g. `Bash: npm test` or `Edit: src/app.ts`."""
    if not isinstance(data, dict):
        return name
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        if data.get(key):
            return f"{name}: {' '.join(str(data[key]).split())[:240]}"
    return name


def digest(path: Path, budget: int = 24000) -> str:
    """A condensed transcript: the goal, the errors Claude hit, then as much of the recent conversation as fits."""
    lines: list[str] = []
    failures: list[str] = []
    calls: dict[str, str] = {}
    goal = project = branch = ""
    for entry in entries(path):
        if entry.get("type") not in ("user", "assistant") or entry.get("isSidechain"):
            continue
        project = entry.get("cwd") or project
        branch = entry.get("gitBranch") or branch
        content = (entry.get("message") or {}).get("content")
        if entry["type"] == "user":
            if is_prompt(entry):
                text = text_of(content).strip()
                goal = goal or text
                lines.append(f"**User:** {clip(text, 2500)}")
                continue
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    result = block.get("content")
                    result = text_of(result) if not isinstance(result, str) else result
                    call = calls.get(block.get("tool_use_id", ""), "a tool")
                    failures.append(f"- {call}\n\n  ```\n  {clip(result, 700)}\n  ```")
                    lines.append(f"✗ **{call} failed:**\n```\n{clip(result, 1500)}\n```")
        else:
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text", "").strip():
                    lines.append(f"**Claude:** {clip(block['text'], 1800)}")
                elif block.get("type") == "tool_use":
                    call = describe_tool(str(block.get("name") or "tool"), block.get("input"))
                    calls[block.get("id", "")] = call
                    lines.append(f"→ Claude ran {call}")

    title = (f"Claude Code conversation {path.stem} in {project or 'an unknown folder'}"
             + (f" (branch {branch})" if branch else "") + ".")
    return assemble(title, goal, failures, lines, budget, "Claude")


def transcript(conversation: Conversation, budget: int = 24000) -> str:
    """The condensed transcript of a conversation from whichever tool it came from."""
    if conversation.tool == "gemini":
        return gemini_digest(conversation.path, budget)
    if conversation.tool == "openai":
        return codex_digest(conversation.path, budget)
    return digest(conversation.path, budget)


def assemble(title: str, goal: str, failures: list[str], lines: list[str], budget: int, speaker: str) -> str:
    """Header (goal and errors), then the newest part of the conversation that fits in the budget."""
    header = [title]
    if goal:
        header += ["", f"**What the user first asked:** {clip(goal, 1500)}"]
    if failures:
        header += ["", f"**Errors {speaker} hit ({len(failures)}, most recent last):**", "", *failures[-6:]]
    head = "\n".join(header)
    room = max(budget - len(head), 2000)
    kept: list[str] = []
    used = 0
    for line in reversed(lines):
        if used + len(line) > room:
            break
        kept.append(line)
        used += len(line) + 2
    kept.reverse()
    omitted = len(lines) - len(kept)
    body = ([f"*[{omitted} earlier messages omitted]*", ""] if omitted else []) + kept
    return head + "\n\n**Recent conversation:**\n\n" + "\n\n".join(body)


# ---------------------------------------------------------------- Gemini CLI

def gemini_stores() -> list[tuple[str, list[Path]]]:
    """(project folder, its chat files) for every project Gemini CLI has chats for."""
    root = gemini_home() / "tmp"
    if not root.is_dir():
        return []
    named: dict[str, str] = {}  # slug or path hash -> project folder
    try:
        registry = json.loads((gemini_home() / "projects.json").read_text(encoding="utf-8")).get("projects", {})
        named.update({slug: folder for folder, slug in registry.items() if isinstance(slug, str)})
    except (OSError, ValueError, AttributeError):
        pass
    found = []
    for store in root.iterdir():
        chats = store / "chats"
        if not chats.is_dir():
            continue
        folder = named.get(store.name, "")
        try:
            folder = (store / ".project_root").read_text(encoding="utf-8").strip() or folder
        except OSError:
            pass
        files = [f for f in chats.glob("session-*") if f.suffix in (".json", ".jsonl")]
        if folder and files:
            found.append((folder, files))
    return found


def gemini_files(project: Path | None) -> list[tuple[str, Path]]:
    """(project folder, chat file) for one project, or for all of them."""
    if project is None:
        return [(folder, f) for folder, files in gemini_stores() for f in files]
    found = [(folder, f) for folder, files in gemini_stores() for f in files if same_folder(folder, project)]
    legacy = gemini_home() / "tmp" / hashlib.sha256(str(project).encode()).hexdigest() / "chats"
    if legacy.is_dir():  # Gemini CLI before project slugs named the folder by the path's sha256
        found += [(str(project), f) for f in legacy.glob("session-*") if f.suffix in (".json", ".jsonl")]
    return found


def same_folder(folder: str, project: Path) -> bool:
    try:
        return Path(folder).expanduser().resolve() == project
    except OSError:
        return False


def gemini_text(content) -> str:
    """Text from a Gemini message's content: a string, a part ({"text": …}) or a list of parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or "")
    if isinstance(content, list):
        return "\n".join(gemini_text(part) for part in content)
    return ""


def gemini_record(path: Path) -> tuple[dict, list[dict]]:
    """A Gemini CLI chat as (metadata, messages). JSON-lines files hold metadata lines, messages, later patches to a
    message (same id), {"$set": …} metadata updates and {"$rewindTo": id} rewinds; older files are one JSON object."""
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return {}, []
        messages = data.get("messages") if isinstance(data, dict) else None
        return (data if isinstance(data, dict) else {}), [m for m in messages or [] if isinstance(m, dict)]
    meta: dict = {}
    order: list[str] = []
    messages: dict[str, dict] = {}
    for entry in entries(path):
        if "$rewindTo" in entry:
            target = entry["$rewindTo"]
            cut = order.index(target) if target in order else 0
            for gone in order[cut:]:
                messages.pop(gone, None)
            del order[cut:]
        elif isinstance(entry.get("$set"), dict):
            meta.update(entry["$set"])
        elif entry.get("type") in ("user", "gemini", "info", "error", "warning") and entry.get("id"):
            if entry["id"] not in messages:
                order.append(entry["id"])
            messages[entry["id"]] = entry
        elif entry.get("id") in messages:  # a patch: new content or tool results for a message
            message = messages[entry["id"]]
            if "content" in entry:
                message["content"] = entry["content"]
            for patch in entry.get("toolCalls") or []:
                for call in message.get("toolCalls") or []:
                    if isinstance(patch, dict) and call.get("id") == patch.get("id"):
                        call.update(patch)
        elif "sessionId" in entry:
            meta.update(entry)
    return meta, [messages[i] for i in order if i in messages]


def gemini_conversations(project: Path | str | None = None, limit: int = 30) -> list[Conversation]:
    project = Path(project).expanduser().resolve() if project is not None else None
    files = sorted(gemini_files(project), key=lambda item: item[1].stat().st_mtime, reverse=True)
    found = []
    for folder, path in files[:limit]:
        meta, messages = gemini_record(path)
        spoken = [m for m in messages if m.get("type") in ("user", "gemini")]
        if not spoken:
            continue
        first = next((gemini_text(m.get("content")).strip() for m in spoken if m.get("type") == "user"), "")
        errors = sum(m.get("type") == "error" for m in messages) + sum(
            1 for m in messages for call in m.get("toolCalls") or [] if isinstance(call, dict) and call.get("status") == "error")
        title = " ".join((str(meta.get("summary") or "") or first or "(untitled)").split())
        found.append(Conversation(str(meta.get("sessionId") or path.stem), path, folder, title[:120],
                                  path.stat().st_mtime, len(spoken), errors, tool="gemini"))
    return found


def gemini_digest(path: Path, budget: int = 24000) -> str:
    meta, messages = gemini_record(path)
    lines: list[str] = []
    failures: list[str] = []
    goal = ""
    for message in messages:
        text = gemini_text(message.get("displayContent") or message.get("content")).strip()
        if message.get("type") == "user" and text:
            goal = goal or text
            lines.append(f"**User:** {clip(text, 2500)}")
        elif message.get("type") == "gemini":
            if text:
                lines.append(f"**Gemini:** {clip(text, 1800)}")
            for call in message.get("toolCalls") or []:
                if not isinstance(call, dict):
                    continue
                name = describe_tool(str(call.get("name") or "tool"), call.get("args"))
                if call.get("status") == "error":
                    result = clip(json.dumps(call.get("result"))[:4000] if call.get("result") else "", 1500)
                    failures.append(f"- {name}\n\n  ```\n  {clip(result, 700)}\n  ```")
                    lines.append(f"✗ **{name} failed:**\n```\n{result}\n```")
                else:
                    lines.append(f"→ Gemini ran {name}")
        elif message.get("type") == "error" and text:
            failures.append(f"- {clip(text, 700)}")
            lines.append(f"✗ **Error:** {clip(text, 1500)}")
    title = f"Gemini CLI conversation {meta.get('sessionId') or path.stem}."
    return assemble(title, goal, failures, lines, budget, "Gemini")


# ---------------------------------------------------------------- Codex

def codex_sessions(limit: int = 2000) -> list[tuple[Path, dict]]:
    """(rollout file, its session_meta payload) for the newest Codex sessions."""
    root = codex_home() / "sessions"
    if not root.is_dir():
        return []
    files = sorted(root.rglob("rollout-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    found = []
    for path in files:
        first = next(entries(path), None)
        if first and first.get("type") == "session_meta" and isinstance(first.get("payload"), dict):
            found.append((path, first["payload"]))
    return found


def codex_titles() -> dict[str, str]:
    """Names given to Codex threads, from its session index when there is one."""
    titles = {}
    for entry in entries(codex_home() / "session_index.jsonl"):
        name = entry.get("thread_name") or entry.get("name")
        key = entry.get("id") or entry.get("thread_id")
        if name and key:
            titles[str(key)] = str(name)
    return titles


def codex_prompt(text: str) -> bool:
    """Whether a Codex user message is something the person typed, not injected instructions or context."""
    text = text.strip()
    return bool(text) and not text.startswith(("<", "# AGENTS.md"))


def codex_exit(output) -> int:
    """The exit code in a Codex command's output, or 0 when it doesn't say."""
    if isinstance(output, str):
        try:
            data = json.loads(output)
        except ValueError:
            match = re.search(r"(?i)exit(?:ed with)? code:?\s*(-?\d+)", output)
            return int(match.group(1)) if match else 0
        output = data
    if isinstance(output, dict):
        metadata = output.get("metadata") if isinstance(output.get("metadata"), dict) else output
        try:
            return int(metadata.get("exit_code") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def codex_events(path: Path):
    """Yield ("user"|"agent"|"call"|"fail", text) for a Codex rollout, in order."""
    calls: dict[str, str] = {}
    typed = any(e.get("type") == "event_msg" and (e.get("payload") or {}).get("type") == "user_message" for e in entries(path))
    for entry in entries(path):
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        kind, sort = entry.get("type"), payload.get("type")
        if kind == "event_msg" and sort == "user_message" and codex_prompt(str(payload.get("message") or "")):
            yield "user", str(payload["message"])
        elif kind == "event_msg" and sort == "agent_message" and payload.get("message"):
            yield "agent", str(payload["message"])
        elif kind == "response_item" and sort == "message" and not typed:
            text = "\n".join(str(b.get("text") or "") for b in payload.get("content") or [] if isinstance(b, dict))
            if payload.get("role") == "user" and codex_prompt(text):
                yield "user", text
            elif payload.get("role") == "assistant" and text.strip():
                yield "agent", text
        elif kind == "response_item" and sort in ("function_call", "custom_tool_call", "local_shell_call"):
            raw = payload.get("arguments") or payload.get("input") or payload.get("action") or {}
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                args = {"command": raw}
            if isinstance(args, dict) and isinstance(args.get("command"), list):
                args = {**args, "command": " ".join(map(str, args["command"]))}
            call = describe_tool(str(payload.get("name") or "shell"), args)
            calls[str(payload.get("call_id") or "")] = call
            yield "call", call
        elif kind == "response_item" and sort in ("function_call_output", "custom_tool_call_output"):
            output = payload.get("output")
            if codex_exit(output):
                text = output if isinstance(output, str) else json.dumps(output)
                yield "fail", f"{calls.get(str(payload.get('call_id') or ''), 'a command')}\n{text}"


def codex_conversations(project: Path | str | None = None, limit: int = 30) -> list[Conversation]:
    project = Path(project).expanduser().resolve() if project is not None else None
    titles = codex_titles()
    found = []
    for path, meta in codex_sessions():
        folder = str(meta.get("cwd") or "")
        if project is not None and not (folder and same_folder(folder, project)):
            continue
        events = list(codex_events(path))
        spoken = [text for kind, text in events if kind in ("user", "agent")]
        if not spoken:
            continue
        session = str(meta.get("id") or path.stem)
        first = next((text for kind, text in events if kind == "user"), "")
        title = " ".join((titles.get(session) or first or "(untitled)").split())
        branch = str((meta.get("git") or {}).get("branch") or "") if isinstance(meta.get("git"), dict) else ""
        found.append(Conversation(session, path, folder, title[:120], path.stat().st_mtime, len(spoken),
                                  sum(kind == "fail" for kind, _ in events), branch, tool="openai"))
        if len(found) >= limit:
            break
    return found


def codex_digest(path: Path, budget: int = 24000) -> str:
    lines: list[str] = []
    failures: list[str] = []
    goal = ""
    for kind, text in codex_events(path):
        if kind == "user":
            goal = goal or text
            lines.append(f"**User:** {clip(text, 2500)}")
        elif kind == "agent":
            lines.append(f"**ChatGPT:** {clip(text, 1800)}")
        elif kind == "call":
            lines.append(f"→ ChatGPT ran {text}")
        else:
            call, _, output = text.partition("\n")
            failures.append(f"- {call}\n\n  ```\n  {clip(output, 700)}\n  ```")
            lines.append(f"✗ **{call} failed:**\n```\n{clip(output, 1500)}\n```")
    meta = next(entries(path), {}).get("payload") or {}
    title = f"Codex (ChatGPT) conversation {meta.get('id') or path.stem} in {meta.get('cwd') or 'an unknown folder'}."
    return assemble(title, goal, failures, lines, budget, "ChatGPT")


def git(project: Path, *args: str, limit: int = 4000) -> str:
    try:
        result = subprocess.run(["git", "-C", str(project), *args], capture_output=True, text=True, timeout=10,
                                encoding="utf-8", errors="replace")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return clip(result.stdout, limit) if result.returncode == 0 else ""


def snapshot(project: Path, diff_budget: int = 12000) -> str:
    """Branch, recent commits, uncommitted changes and their diff, so advisers see what changed lately."""
    project = Path(project).expanduser()
    if not git(project, "rev-parse", "--is-inside-work-tree"):
        try:
            names = sorted(p.name + ("/" if p.is_dir() else "") for p in project.iterdir() if not p.name.startswith("."))
        except OSError:
            return ""
        return f"Folder {project} (not a git repository). Top-level entries: {', '.join(names[:60])}"
    parts = [f"Git repository at {project}, branch {git(project, 'branch', '--show-current').strip() or '(detached)'}."]
    log = git(project, "log", "--oneline", "-8")
    if log:
        parts += ["", "Recent commits:", log]
    status = git(project, "status", "--short", limit=3000)
    parts += ["", "Uncommitted changes:", status or "(none)"]
    diff = git(project, "diff", "HEAD", limit=diff_budget) if status else ""
    if diff:
        parts += ["", "Diff of uncommitted changes:", "```diff", diff, "```"]
    return "\n".join(parts)


def context_block(project: Path | None, conversation: Conversation | None, continuing: bool = False) -> str:
    """Everything the council should know about the project and conversation, ready to put before the request."""
    parts = []
    if project:
        parts.append(f"The user is working in the project at {project}. It is your current directory, so you can read its files "
                     "to check claims and find causes. You are reviewing only: do not change anything.")
        state = snapshot(project)
        if state:
            parts.append(f"<project_state>\n{state}\n</project_state>")
    if conversation:
        tag = {"claude": "claude_code", "gemini": "gemini_cli", "openai": "codex"}[conversation.tool]
        if continuing:
            parts.append(f"This is a conversation the user had with {conversation.tool_name}. They want you to pick it up "
                         "and carry on from where it left off, as if you had been part of it.")
        else:
            parts.append(f"The user has been working on this with {conversation.tool_name} and wants a second opinion on "
                         "that conversation, especially on errors that a fresh look might resolve.")
        parts.append(f"<{tag}_conversation>\n{transcript(conversation)}\n</{tag}_conversation>")
    return "\n\n".join(parts)


RESUME = {"claude": "claude --resume {id}", "gemini": "gemini --resume {id}", "openai": "codex resume {id}"}


def resume_command(conversation: Conversation) -> str:
    """The command that reopens a conversation in its own tool, in the folder it was in."""
    folder = conversation.project or str(Path.cwd())
    return f"cd {quote(folder)} && {RESUME[conversation.tool].format(id=quote(conversation.id))}"


def handoff(project: Path, conversation: Conversation | None, verdict: str) -> tuple[Path, str]:
    """Save the verdict inside the project (git-ignored) and return the command that hands it back to the tool the
    conversation came from (Claude Code when there is none)."""
    folder = Path(project) / ".conclave"
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    path = folder / f"verdict-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(verdict.strip() + "\n", encoding="utf-8")
    message = (f"The Conclave council (Gemini, ChatGPT and other agents) reviewed this conversation. Read .conclave/{path.name}, the verdict "
               "of the council, say where you agree or disagree, then apply the fixes you agree with.")
    tool = conversation.tool if conversation else "claude"
    if tool == "gemini":
        command = f"gemini --resume {quote(conversation.id)} --prompt-interactive {quote(message)}"
    elif tool == "openai":
        command = f"codex resume {quote(conversation.id)} {quote(message)}"
    else:
        command = f"claude {'--resume ' + quote(conversation.id) + ' ' if conversation else ''}{quote(message)}"
    return path, f"cd {quote(str(project))} && {command}"


def quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
