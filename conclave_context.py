"""Bring a Claude Code project and its conversations to the council.

Claude Code keeps each conversation as JSON lines in ~/.claude/projects/<project path with / replaced by ->/<session id>.jsonl.
This module lists those conversations, condenses one into a transcript the advisers can read (errors first), and
snapshots the project's git state.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


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

    @property
    def when(self) -> str:
        return ago(self.updated)


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
    """Every project Claude Code has a conversation for: (folder, last activity, number of conversations), newest first."""
    found: dict[str, tuple[float, int]] = {}
    root = claude_home() / "projects"
    if not root.is_dir():
        return []
    for store in root.iterdir():
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
        updated, total = found.get(folder, (0.0, 0))
        found[folder] = (max(updated, files[0].stat().st_mtime), total + len(files))
    return sorted(((folder, updated, total) for folder, (updated, total) in found.items()), key=lambda item: -item[1])


def conversations(project: Path | str | None = None, limit: int = 30) -> list[Conversation]:
    """Conversations for one project (or all of them), newest first."""
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


def find(project: Path | str | None, key: str) -> Conversation | None:
    """Pick a conversation by 'latest', its number in the list (1 = newest) or the start of its session id."""
    listed = conversations(project, limit=200)
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

    header = [f"Claude Code conversation {path.stem} in {project or 'an unknown folder'}"
              + (f" (branch {branch})" if branch else "") + "."]
    if goal:
        header += ["", f"**What the user first asked:** {clip(goal, 1500)}"]
    if failures:
        header += ["", f"**Errors Claude hit ({len(failures)}, most recent last):**", "", *failures[-6:]]
    head = "\n".join(header)

    # Keep the newest part of the conversation that fits in what is left of the budget.
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


def context_block(project: Path | None, conversation: Conversation | None) -> str:
    """Everything the council should know about the project and conversation, ready to put before the request."""
    parts = []
    if project:
        parts.append(f"The user is working in the project at {project}. It is your current directory, so you can read its files "
                     "to check claims and find causes. You are reviewing only: do not change anything.")
        state = snapshot(project)
        if state:
            parts.append(f"<project_state>\n{state}\n</project_state>")
    if conversation:
        parts.append("The user has been working on this with Claude Code and wants a second opinion on that conversation, "
                     "especially on errors that a fresh look might resolve.")
        parts.append(f"<claude_code_conversation>\n{digest(conversation.path)}\n</claude_code_conversation>")
    return "\n\n".join(parts)


def handoff(project: Path, conversation: Conversation | None, verdict: str) -> tuple[Path, str]:
    """Save the verdict inside the project (git-ignored) and return the command that hands it back to Claude Code."""
    folder = Path(project) / ".conclave"
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")
    path = folder / f"verdict-{datetime.now():%Y%m%d-%H%M%S}.md"
    path.write_text(verdict.strip() + "\n", encoding="utf-8")
    message = (f"Gemini and Codex reviewed this conversation in Conclave. Read .conclave/{path.name}, the verdict "
               "of the council, say where you agree or disagree, then apply the fixes you agree with.")
    resume = f"--resume {conversation.id} " if conversation else ""
    command = f"cd {quote(str(project))} && claude {resume}{quote(message)}"
    return path, command


def quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
