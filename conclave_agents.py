"""The agents Conclave can seat at the council: how to run each one read-only, what it is good at, and its sprite.

Claude always leads. Any other agent here can advise, and you can add your own command-line AI in the config:

    [agents.mistral]
    command = "ollama run mistral"      # the prompt arrives on stdin
    label = "MISTRAL"
    strengths = "small local model, private"
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field


@dataclass
class Agent:
    id: str
    label: str  # shown on cards, e.g. "✧ GEMINI"
    name: str  # the tool's own name, e.g. "Gemini CLI"
    binary: str
    reader: str  # how its output is read: "claude", "gemini", "codex" or "plain"
    strengths: str  # what Claude is told when it hands out subtasks
    install: str  # a sentence on how to install it
    install_command: str = ""  # run by /setup
    sign_in: str = ""
    sign_in_command: str = ""
    sprite: str = "ghost"
    resume: str = ""  # continues a saved session, e.g. "claude --resume {id}"; empty when sessions aren't supported
    command: list[str] = field(default_factory=list)  # for agents defined in the config
    noise: tuple[str, ...] = ()  # prefixes of output lines that aren't part of the answer
    builtin: bool = True


AGENTS: dict[str, Agent] = {}


def _add(agent: Agent) -> None:
    AGENTS[agent.id] = agent


_add(Agent(
    "claude", "✦ CLAUDE", "Claude Code", "claude", "claude",
    "leads the council; careful reasoning, code review and clear writing",
    "Install Claude Code: https://code.claude.com/docs/en/quickstart",
    "npm install -g @anthropic-ai/claude-code",
    "Sign in again: run `claude` in your terminal, type /login, then retry.", "claude auth login",
    sprite="claude", resume="claude --resume {id}",
))
_add(Agent(
    "gemini", "✧ GEMINI", "Gemini CLI", "gemini", "gemini",
    "a very large context window, so it reads whole codebases and long logs well; can search the web",
    "Install Gemini CLI: npm install -g @google/gemini-cli",
    "npm install -g @google/gemini-cli",
    "Sign in again: run `gemini` in your terminal, choose a sign-in method, then retry.", "gemini",
    sprite="gemini", resume="gemini --resume {id}",
))
_add(Agent(
    # ChatGPT has no command-line tool of its own; Codex is OpenAI's, signed in with your ChatGPT account and
    # running ChatGPT's GPT models, so ChatGPT joins the council through it.
    "openai", "✳ CHATGPT", "ChatGPT", "codex", "codex",
    "strong at code; can run read-only commands such as tests and searches in a sandbox to check claims",
    "Install the Codex CLI, ChatGPT's coding agent: npm install -g @openai/codex@latest, then codex login",
    "npm install -g @openai/codex@latest",
    "Sign in again: run `codex login` in your terminal and choose Sign in with ChatGPT, then retry.", "codex login",
    sprite="codex", resume="codex resume {id}",
))
_add(Agent(
    "qwen", "◆ QWEN", "Qwen Code", "qwen", "claude",
    "Qwen's coding model; quick at reading and explaining code",
    "Install Qwen Code: npm install -g @qwen-code/qwen-code",
    "npm install -g @qwen-code/qwen-code",
    "Sign in: run `qwen` in your terminal and choose a sign-in method, then retry.", "qwen",
    sprite="qwen",
))
_add(Agent(
    "copilot", "◈ COPILOT", "GitHub Copilot CLI", "copilot", "plain",
    "knows GitHub well: repositories, pull requests, issues and Actions",
    "Install GitHub Copilot CLI: npm install -g @github/copilot",
    "npm install -g @github/copilot",
    "Sign in: run `copilot` in your terminal and type /login, then retry.", "copilot",
    sprite="copilot",
))
_add(Agent(
    "opencode", "◇ OPENCODE", "OpenCode", "opencode", "plain",
    "uses whichever model provider you have configured in OpenCode",
    "Install OpenCode: npm install -g opencode-ai",
    "npm install -g opencode-ai",
    "Sign in: run `opencode auth login` in your terminal, then retry.", "opencode auth login",
    sprite="opencode", noise=("> ",),
))
_add(Agent(
    "ollama", "▲ OLLAMA", "Ollama", "ollama", "plain",
    "a local model running on this computer: private and offline, but smaller than the cloud models",
    "Install Ollama from https://ollama.com/download, then run `ollama pull llama3.2`",
    "", "Start Ollama: open the Ollama app (or run `ollama serve`), then retry.", "",
    sprite="ollama",
))

DEFAULT_ADVISERS = ("gemini", "openai")


def custom(agent_id: str, spec: dict) -> Agent:
    """An agent defined in the config's [agents.<id>] table."""
    command = spec.get("command")
    if isinstance(command, str):
        command = shlex.split(command)
    if not isinstance(command, list) or not command or not all(isinstance(part, str) for part in command):
        raise ValueError(f"[agents.{agent_id}] needs command = \"…\" (the prompt is sent on stdin)")
    label = str(spec.get("label") or agent_id).upper()
    return Agent(
        agent_id, f"● {label}", str(spec.get("name") or label.title()), command[0], "plain",
        str(spec.get("strengths") or "a general-purpose assistant"),
        str(spec.get("install") or f"Install {command[0]} and make sure it is on your PATH"),
        sign_in=str(spec.get("sign_in") or ""), sprite=str(spec.get("sprite") or "ghost"),
        command=command, builtin=False,
    )


# ---------------------------------------------------------------- sprites
#
# 7×6 pixels. b: the agent's colour, d: darker, a: lighter, e: eyes, w: white, .: transparent.
# Eyes close for a blink; the renderer bobs the sprite while the agent works.

SPRITES = {
    "claude": (
        "...a...",
        "..bbb..",
        ".bbbbb.",
        ".bebeb.",
        ".bbbbb.",
        "..d.d..",
    ),
    "gemini": (
        ".b...b.",
        "bbb.bbb",
        "beb.beb",
        "bbb.bbb",
        ".d...d.",
        "a.....a",
    ),
    "codex": (
        "...a...",
        "...d...",
        ".bbbbb.",
        ".bebeb.",
        ".bdddb.",
        ".b...b.",
    ),
    "qwen": (
        ".b...b.",
        ".bb.bb.",
        ".bbbbb.",
        ".bebeb.",
        ".bbabb.",
        "..bbb..",
    ),
    "copilot": (
        "..ddd..",
        ".bbbbb.",
        ".wewew.",
        ".bbbbb.",
        "..b.b..",
        ".d...d.",
    ),
    "opencode": (
        ".ddddd.",
        ".bbbbb.",
        ".bebeb.",
        ".bbbbb.",
        ".bdddb.",
        "...d...",
    ),
    "ollama": (
        ".b.b...",
        ".bbb...",
        ".beb...",
        ".bbbbbb",
        ".bbbbbb",
        ".b.b.b.",
    ),
    "ghost": (
        "..bbb..",
        ".bbbbb.",
        ".bebeb.",
        ".bbbbb.",
        ".bbbbb.",
        ".b.b.b.",
    ),
}


def sprite_frame(name: str, mood: str = "idle") -> tuple[str, ...]:
    """The sprite's rows for a mood: idle (eyes open), blink or sleep (eyes closed), done (bright eyes), error (x eyes)."""
    rows = SPRITES.get(name, SPRITES["ghost"])
    eye = {"blink": "d", "sleep": "d", "done": "a", "error": "x"}.get(mood, "e")
    return tuple(row.replace("e", eye) for row in rows)
