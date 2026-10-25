"""Tests run Conclave against fake claude, gemini and codex executables placed on PATH.

The fakes speak each CLI's streaming JSON format (Claude's stream-json, Gemini's stream-json, Codex's exec --json).
"""

import codecs
import fcntl
import json
import os
import plistlib
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import textwrap
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import conclave  # noqa: E402
import conclave_agents  # noqa: E402
import conclave_app  # noqa: E402
import conclave_context  # noqa: E402



@pytest.fixture(autouse=True)
def gemini_cli_seat():
    """Tests in this process use Gemini CLI for the Gemini seat (loading a config re-picks it); Antigravity CLI has
    its own tests."""
    conclave.seat_gemini("gemini-cli")
    yield
    conclave.seat_gemini("gemini-cli")

FAKE_CLAUDE = """
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
if args[:2] == ["auth", "status"]:
    print(json.dumps({"loggedIn": True}))
    sys.exit(0)
prompt = sys.stdin.read()
if os.environ.get("ARGV_LOG"):
    open(os.environ["ARGV_LOG"], "a").write(json.dumps(["claude", *args]) + "\\n")
# Like the real CLI, a resumed session remembers what was said in it before.
store = Path(__file__).parent / "claude-sessions"
store.mkdir(exist_ok=True)
memory = ""
if "--resume" in args:
    session = store / args[args.index("--resume") + 1]
    memory = session.read_text()
    session.write_text(memory + prompt)
elif "--session-id" in args:
    (store / args[args.index("--session-id") + 1]).write_text(prompt)
seen = memory + prompt

def emit(event):
    print(json.dumps(event), flush=True)

def say(text):
    emit({"type": "system", "subtype": "init", "model": "claude-test-model"})
    emit({"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_start", "message": {}}})
    emit({"type": "stream_event", "parent_tool_use_id": None,
          "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "name": "Read"}}})
    for i in range(0, len(text), 7):
        emit({"type": "stream_event", "parent_tool_use_id": None,
              "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[i:i + 7]}}})
    emit({"type": "result", "subtype": "success", "is_error": False, "result": text, "total_cost_usd": 0.01,
          "usage": {"input_tokens": 100, "cache_read_input_tokens": 900, "output_tokens": 50}})

if os.environ.get("CWD_LOG"):
    open(os.environ["CWD_LOG"], "a").write("claude " + os.getcwd() + "\\n")
if "Carry out this plan" in prompt:
    say("APPLIED THE PLAN")
elif "Your advisers:" in prompt and os.environ.get("FAKE_PLAN"):
    say(json.dumps({"plan": [{"agent": "gemini", "title": "Map the schema", "instructions": "List the tables"},
                             {"agent": "codex", "title": "Run the tests", "instructions": "Run pytest"},
                             {"agent": "nobody", "title": "Check the docs", "instructions": "Read the README"}]}))
elif "Prepare a concise review brief" in prompt:
    say("BRIEF: weigh cost against speed")
else:
    notes = []
    for marker, note in (("<input>\\n", "saw piped input"), ("GEMINI SAYS", "saw gemini"), ("CODEX SAYS", "saw codex"),
                         ("did not respond", "told about missing adviser"), ("<previous_exchange>\\n", "saw history"),
                         ("<claude_code_conversation>\\n", "saw conversation"), ("<project_state>\\n", "saw project"),
                         ("<rebuttal agent=", "saw rebuttals"), ("<subtask number=", "saw subtasks"),
                         ("ECHO SAYS", "saw echo")):
        if marker in seen:
            notes.append(note)
    say("FINAL ANSWER (" + ", ".join(notes) + ")\\n\\nCOUNCIL: agreement=strong; confidence=high")
"""

FAKE_GEMINI = """
import json, os, sys
if sys.argv[1:2] == ["--version"]:
    print("9.9.9")
    sys.exit(0)
prompt = sys.stdin.read()
if os.environ.get("ARGV_LOG"):
    open(os.environ["ARGV_LOG"], "a").write(json.dumps(["gemini", *sys.argv[1:]]) + "\\n")
assert "BRIEF: weigh cost against speed" in prompt or "YOUR SUBTASK:" in prompt or "user's request below" in prompt
if os.environ.get("CWD_LOG"):
    open(os.environ["CWD_LOG"], "a").write("gemini " + os.getcwd() + "\\n")
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "init", "session_id": "x", "model": "gemini-test"})
emit({"type": "message", "role": "user", "content": prompt})
emit({"type": "tool_use", "tool_name": "read_file", "tool_id": "1", "parameters": {}})
for chunk in ("GEMINI ", "SAYS ", "go"):
    emit({"type": "message", "role": "assistant", "content": chunk, "delta": True})
emit({"type": "result", "status": "success", "stats": {"total_tokens": 30, "input_tokens": 20, "output_tokens": 10}})
"""

FAKE_CODEX = """
import json, os, sys
if sys.argv[1:2] == ["resume"]:  # `codex resume <id>` from /continue
    print("RESUMED " + sys.argv[2])
    sys.exit(0)
if sys.argv[1:2] == ["--version"]:
    print("codex-cli 9.9.9")
    sys.exit(0)
if sys.argv[1:3] == ["login", "status"]:
    print("Logged in using ChatGPT")
    sys.exit(0)
prompt = sys.stdin.read()
if os.environ.get("ARGV_LOG"):
    open(os.environ["ARGV_LOG"], "a").write(json.dumps(["codex", *sys.argv[1:]]) + "\\n")
assert "BRIEF: weigh cost against speed" in prompt or "YOUR SUBTASK:" in prompt or "user's request below" in prompt
if os.environ.get("CWD_LOG"):
    open(os.environ["CWD_LOG"], "a").write("codex " + os.getcwd() + "\\n")
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "thread.started", "thread_id": "codex-thread-1"})
emit({"type": "turn.started"})
emit({"type": "item.completed", "item": {"id": "0", "type": "reasoning", "text": "**Checking the brief**"}})
emit({"type": "item.started", "item": {"id": "1", "type": "command_execution", "command": "ls", "status": "in_progress"}})
emit({"type": "item.completed", "item": {"id": "2", "type": "agent_message", "text": "CODEX SAYS go"}})
emit({"type": "turn.completed", "usage": {"input_tokens": 40, "cached_input_tokens": 0, "output_tokens": 12}})
"""

FAKE_PLAIN = """
import sys
prompt = sys.stdin.read()
assert "BRIEF: weigh cost against speed" in prompt or "YOUR SUBTASK:" in prompt
print("{name} SAYS go")
"""

FAKE_FAILURE = """
import sys
sys.stdin.read()
print("not signed in", file=sys.stderr)
sys.exit(1)
"""

# Starts a helper that would outlive the CLI unless the whole process group is stopped.
FAKE_HANG = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open({pidfile!r}, "w").write(str(child.pid))
time.sleep(60)
"""


def running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # An orphan that was killed can linger as a zombie until init reaps it.
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().rsplit(")", 1)[1].split()[0] == "Z")


def install(bin_dir: Path, name: str, source: str) -> None:
    path = bin_dir / name
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(source))
    path.chmod(0o755)


@pytest.fixture
def fake_bin(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    install(bin_dir, "claude", FAKE_CLAUDE)
    install(bin_dir, "gemini", FAKE_GEMINI)
    install(bin_dir, "codex", FAKE_CODEX)
    return bin_dir


def write_conversation(claude_home: Path, project: Path, session: str = "abc12345-0000", *, sidechain_error=False) -> Path:
    """A small Claude Code transcript: a request, a failing test run, and Claude's reply."""
    store = claude_home / "projects" / conclave_context.store_name(project)
    store.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "user", "cwd": str(project), "gitBranch": "main", "message": {"role": "user", "content": "Make the tests pass"}},
        {"type": "assistant", "cwd": str(project), "message": {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "secret"},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "pytest -q"}}]}},
        {"type": "user", "cwd": str(project), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "is_error": True, "content": "FAILED test_app.py::test_login - KeyError: 'token'"}]}},
        {"type": "assistant", "cwd": str(project), "message": {"role": "assistant", "content": [
            {"type": "text", "text": "The login test fails because the token is missing."}]}},
        {"type": "assistant", "isSidechain": True, "cwd": str(project), "message": {"role": "assistant", "content": [
            {"type": "text", "text": "SUBAGENT CHATTER"}]}},
    ]
    path = store / f"{session}.jsonl"
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
    return path


def env_for(bin_dir: Path, tmp_path: Path, **extra) -> dict:
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_STATE_HOME=str(tmp_path / "state"), CLAUDE_CONFIG_DIR=str(tmp_path / "claude-home"))
    env.update(extra)
    return env


def run_cli(bin_dir: Path, *args: str, stdin: str = "", tmp_path: Path, **extra) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "conclave.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env_for(bin_dir, tmp_path, **extra),
        timeout=60,
        cwd=tmp_path,
    )


# ---------------------------------------------------------------- one-shot runs

def test_answer_goes_to_stdout_when_piped(fake_bin, tmp_path):
    result = run_cli(fake_bin, "Pick a database", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FINAL ANSWER (saw gemini, saw codex, saw rebuttals)\n"  # the COUNCIL line is not part of the answer
    assert "agreement strong" in result.stderr
    assert "confidence high" in result.stderr


def test_piped_input_is_added_to_the_task(fake_bin, tmp_path):
    result = run_cli(fake_bin, "Review this plan", stdin="step one\nstep two", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "saw piped input" in result.stdout


def test_task_can_come_only_from_stdin(fake_bin, tmp_path):
    result = run_cli(fake_bin, stdin="Pick a database", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("FINAL ANSWER")


def test_silent_stdin_does_not_block(fake_bin, tmp_path):
    # stdin is a pipe that stays open and silent, as some task runners and schedulers provide.
    read_end, write_end = os.pipe()
    try:
        result = subprocess.run([sys.executable, str(ROOT / "conclave.py"), "Pick a database"], stdin=read_end,
                                capture_output=True, text=True, timeout=60, env=env_for(fake_bin, tmp_path), cwd=tmp_path)
    finally:
        os.close(read_end)
        os.close(write_end)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("FINAL ANSWER")
    assert "nothing arrived on stdin" in result.stderr


def test_missing_task_is_a_usage_error(fake_bin, tmp_path):
    result = run_cli(fake_bin, tmp_path=tmp_path)
    assert result.returncode == 2
    assert "give a task" in result.stderr


def test_plain_text_cli_output_still_works(fake_bin, tmp_path):
    install(fake_bin, "codex", FAKE_PLAIN.replace("{name}", "CODEX"))
    result = run_cli(fake_bin, "Pick a database", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "saw codex" in result.stdout


# A Codex whose JSON events Conclave doesn't recognise, but which answers in plain text without --json.
FAKE_NEW_FORMAT = """
import json, sys
sys.stdin.read()
if "--json" in sys.argv:
    print(json.dumps({"type": "session.begin"}))
    print(json.dumps({"type": "answer.delta", "payload": "CODEX SAYS go"}))
else:
    print("CODEX SAYS go")
"""


def test_unreadable_json_falls_back_to_plain_text(fake_bin, tmp_path):
    install(fake_bin, "codex", FAKE_NEW_FORMAT)
    result = run_cli(fake_bin, "Pick a database", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "saw codex" in result.stdout


def test_failed_adviser_is_reported_to_the_lead(fake_bin, tmp_path):
    install(fake_bin, "gemini", FAKE_FAILURE)
    result = run_cli(fake_bin, "Pick a database", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "saw codex" in result.stdout
    assert "told about missing adviser" in result.stdout
    assert "DID NOT RESPOND" in result.stderr
    assert "Sign in again: run `gemini`" in result.stderr


def test_missing_lead_explains_how_to_install(fake_bin, tmp_path):
    (fake_bin / "claude").unlink()
    env_path = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "claude").exists())
    result = subprocess.run(
        [sys.executable, str(ROOT / "conclave.py"), "Pick a database"],
        input="", capture_output=True, text=True, timeout=60,
        env=dict(os.environ, PATH=f"{fake_bin}{os.pathsep}{env_path}", XDG_CONFIG_HOME=str(tmp_path / "config")),
    )
    assert result.returncode == 1
    assert "CLAUDE COULDN'T START THE REVIEW" in result.stderr
    assert "Install Claude Code" in result.stderr


def test_output_file_and_opinions(fake_bin, tmp_path):
    out = tmp_path / "answer.md"
    result = run_cli(fake_bin, "--show-opinions", "-o", str(out), "Pick a database", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    saved = out.read_text()
    assert "FINAL ANSWER" in saved
    assert "GEMINI SAYS go" in saved
    assert "CODEX SAYS go" in saved
    assert "COUNCIL:" not in saved
    assert saved == result.stdout


def test_timeout_stops_the_whole_process_group(fake_bin, tmp_path):
    pidfile = tmp_path / "child.pid"
    install(fake_bin, "codex", FAKE_HANG.replace("{pidfile!r}", repr(str(pidfile))))
    started = time.monotonic()
    result = run_cli(fake_bin, "--timeout", "3", "Pick a database", tmp_path=tmp_path)
    assert time.monotonic() - started < 30
    assert result.returncode == 0, result.stderr
    assert "timed out after 3 seconds" in result.stderr
    child = int(pidfile.read_text())
    time.sleep(0.5)
    assert not running(child)


def argv_log(path: Path) -> dict[str, list[list[str]]]:
    calls: dict[str, list[list[str]]] = {}
    for line in path.read_text().splitlines():
        command = json.loads(line)
        calls.setdefault(command[0], []).append(command[1:])
    return calls


def test_rounds_are_saved_as_sessions_in_each_tool(fake_bin, tmp_path):
    log = tmp_path / "argv.log"
    result = run_cli(fake_bin, "Pick a database", stdin="the schema", tmp_path=tmp_path, ARGV_LOG=str(log))
    assert result.returncode == 0, result.stderr
    # The verdict comes from a resumed session that already holds the task, and still sees everything.
    assert result.stdout == "FINAL ANSWER (saw piped input, saw gemini, saw codex, saw rebuttals)\n"
    calls = argv_log(log)
    brief, verdict = calls["claude"]
    lead = brief[brief.index("--session-id") + 1]
    assert brief[brief.index("--name") + 1] == "Conclave · Pick a database"
    assert verdict[verdict.index("--resume") + 1] == lead and "--session-id" not in verdict
    assert "--no-session-persistence" not in brief + verdict
    gemini = calls["gemini"][0][calls["gemini"][0].index("--session-id") + 1]
    assert "--ephemeral" not in calls["codex"][0]
    stderr = re.sub(r"\s+", " ", result.stderr)
    assert f"continue in Claude Code: cd {tmp_path} && claude --resume {lead}" in stderr
    assert f"continue in Gemini CLI: cd {tmp_path} && gemini --resume {gemini}" in stderr
    assert f"continue in ChatGPT: cd {tmp_path} && codex resume codex-thread-1" in stderr


def test_private_runs_leave_no_sessions(fake_bin, tmp_path):
    for args, config in ((["--private"], ""), ([], "[sessions]\nsave = false\n")):
        log = tmp_path / "argv.log"
        log.unlink(missing_ok=True)
        (tmp_path / "config" / "conclave").mkdir(parents=True, exist_ok=True)
        (tmp_path / "config" / "conclave" / "config.toml").write_text(config)
        result = run_cli(fake_bin, *args, "Pick a database", tmp_path=tmp_path, ARGV_LOG=str(log))
        assert result.returncode == 0, result.stderr
        assert result.stdout == "FINAL ANSWER (saw gemini, saw codex, saw rebuttals)\n"
        calls = argv_log(log)
        assert all("--no-session-persistence" in call for call in calls["claude"])
        assert "--session-id" not in calls["gemini"][0] and "--ephemeral" in calls["codex"][0]
        assert "continue in" not in result.stderr


def test_advisers_cross_examine_each_other(fake_bin, tmp_path):
    log = tmp_path / "argv.log"
    result = run_cli(fake_bin, "--show-opinions", "Pick a database", tmp_path=tmp_path, ARGV_LOG=str(log))
    assert result.returncode == 0, result.stderr
    calls = argv_log(log)
    assert len(calls["gemini"]) == 2 and len(calls["codex"]) == 2  # an opinion, then a rebuttal
    assert "After reading the other adviser" in result.stdout
    quick = run_cli(fake_bin, "--quick", "Pick a database", tmp_path=tmp_path, ARGV_LOG=str(log))
    assert quick.stdout == "FINAL ANSWER (saw gemini, saw codex)\n"


def test_no_cross_examination_when_an_adviser_fails(fake_bin, tmp_path):
    install(fake_bin, "gemini", FAKE_FAILURE)
    log = tmp_path / "argv.log"
    result = run_cli(fake_bin, "Pick a database", tmp_path=tmp_path, ARGV_LOG=str(log))
    assert "saw rebuttals" not in result.stdout and len(argv_log(log)["codex"]) == 1


def test_setup_reports_each_tool(fake_bin, tmp_path):
    result = run_cli(fake_bin, "setup", tmp_path=tmp_path, GEMINI_API_KEY="x")  # `conclave setup` works like --setup
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("ready") == 3 and "9.9.9 (Claude Code)" in result.stderr
    (fake_bin / "gemini").unlink()
    env_path = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "gemini").exists())
    missing = subprocess.run([sys.executable, str(ROOT / "conclave.py"), "--setup"], capture_output=True, text=True,
                             timeout=60, env=dict(env_for(fake_bin, tmp_path), PATH=f"{fake_bin}{os.pathsep}{env_path}"))
    assert missing.returncode == 1
    # With neither Gemini tool installed, Gemini is seated through Antigravity CLI, which personal accounts can use.
    assert "not installed" in missing.stderr and "antigravity.google/cli/install.sh" in re.sub(r"\s+", "", missing.stderr)


FAKE_AGY = """
import json, os, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("1.4.4")
    sys.exit(0)
prompt = next(a for a in args if a.startswith("--prompt="))[len("--prompt="):]
if os.environ.get("ARGV_LOG"):
    open(os.environ["ARGV_LOG"], "a").write(json.dumps(["agy", *[a if not a.startswith("--prompt=") else "--prompt=…" for a in args]]) + "\\n")
assert "BRIEF: weigh cost against speed" in prompt or "user's request below" in prompt
def emit(event):
    print(json.dumps(event), flush=True)
emit({"type": "init", "cwd": os.getcwd(), "model": "gemini-4-flash", "permission_mode": "plan"})
emit({"type": "step_update", "conversation_id": "agy-conv-1", "step_index": 0, "step_type": "tool", "state": "ACTIVE", "tool_name": "read_file"})
emit({"type": "step_update", "conversation_id": "agy-conv-1", "step_index": 0, "step_type": "tool", "state": "DONE", "tool_name": "read_file"})
for chunk in ("GEMINI ", "SAYS ", "go"):
    emit({"type": "step_update", "conversation_id": "agy-conv-1", "step_index": 1, "step_type": "agent_response", "state": "ACTIVE", "text_delta": chunk})
emit({"type": "result", "conversation_id": "agy-conv-1", "status": "SUCCESS", "response": "GEMINI SAYS go",
      "usage": {"input_tokens": 20, "output_tokens": 10, "thinking_tokens": 5, "cache_read_tokens": 0, "total_tokens": 35}})
"""


def test_gemini_through_antigravity_cli(fake_bin, tmp_path):
    (fake_bin / "gemini").unlink()
    install(fake_bin, "agy", FAKE_AGY)
    log = tmp_path / "argv.log"
    env_path = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep) if not (Path(p) / "gemini").exists())
    result = run_cli(fake_bin, "Pick a database", tmp_path=tmp_path, ARGV_LOG=str(log), PATH=f"{fake_bin}{os.pathsep}{env_path}")
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FINAL ANSWER (saw gemini, saw codex, saw rebuttals)\n"
    first = argv_log(log)["agy"][0]
    assert first[first.index("--mode") + 1] == "plan" and "stream-json" in first and "--dangerously-skip-permissions" not in first
    assert f"continue in Antigravity CLI: cd {tmp_path} && agy --conversation agy-conv-1" in re.sub(r"\s+", " ", result.stderr)

    result = run_cli(fake_bin, "--agent", "gemini", "--access", "full", "Fix it", tmp_path=tmp_path, ARGV_LOG=str(log),
                     PATH=f"{fake_bin}{os.pathsep}{env_path}")
    assert result.stdout == "GEMINI SAYS go\n", result.stderr
    full = argv_log(log)["agy"][-1]
    assert "--dangerously-skip-permissions" in full and full[full.index("--mode") + 1] == "accept-edits"


def test_cli_errors_are_tidied():
    trace = ("V6U.js:312319:11)\n    at _doSetupUser (file:///opt/homebrew/lib/node_modules/x.js:1:5)\n"
             "  ineligibleTiers: [ { reasonCode: 'UNSUPPORTED_CLIENT',\n"
             "      reasonMessage: 'This client is no longer supported for Gemini Code Assist for individuals.',\n")
    assert conclave.tidy_error(trace) == "This client is no longer supported for Gemini Code Assist for individuals."
    assert conclave.tidy_error("Error: boom\n    at foo (x.js:1:2)\n    at node:internal/y") == "Error: boom"


def test_update_needs_pipx(fake_bin, tmp_path):
    result = run_cli(fake_bin, "update", tmp_path=tmp_path, PATH=str(fake_bin))
    assert result.returncode == 1 and "pipx" in result.stderr


def test_choosing_the_gemini_tool(tmp_path):
    try:
        conclave.seat_gemini("antigravity")
        resume = conclave.SessionPlan("c-1", resume=True)
        argv, stdin = conclave.command_for("gemini", "-starts with a dash", "gemini-4-pro", session=resume, access="edit")
        assert argv[:2] == ["agy", "--prompt=-starts with a dash"] and stdin is None
        assert argv[-4:] == ["--conversation", "c-1", "--model", "gemini-4-pro"]
        assert conclave.SESSION_NAMES["gemini"] == "Antigravity CLI"
        stream = feed("gemini", [{"type": "result", "status": "ERROR", "error": {"message": "authentication required"}}])
        assert "authentication required" in stream.error
        assert "agy" in conclave.fix_for("gemini", "Failed to sign in. Message: This client is no longer supported for "
                                         "Gemini Code Assist for individuals.")
        config = tmp_path / "config.toml"
        config.write_text('[gemini]\ncli = "gemini-cli"\n')
        conclave.load_config(config)
        assert conclave.PROVIDERS["gemini"] == "gemini"
        config.write_text('[gemini]\ncli = "bard"\n')
        with pytest.raises(ValueError):
            conclave.load_config(config)
    finally:
        conclave.seat_gemini("gemini-cli")


FAKE_ECHO = """
import sys
prompt = sys.stdin.read()
print("ECHO SAYS go")
"""


def test_custom_agent_joins_the_council(fake_bin, tmp_path):
    install(fake_bin, "echoagent", FAKE_ECHO)
    log = tmp_path / "argv.log"
    (tmp_path / "config" / "conclave").mkdir(parents=True)
    (tmp_path / "config" / "conclave" / "config.toml").write_text(
        f'[agents.echo]\ncommand = "{fake_bin / "echoagent"}"\nlabel = "Echo"\nstrengths = "repeats things"\n\n'
        '[council]\nadvisers = ["gemini", "codex", "echo"]\n')
    result = run_cli(fake_bin, "--show-opinions", "Pick a database", tmp_path=tmp_path, ARGV_LOG=str(log))
    assert result.returncode == 0, result.stderr
    assert "saw gemini, saw codex, saw rebuttals" in result.stdout and "saw echo" in result.stdout
    assert "● ECHO · OPINION" in result.stdout
    assert len(argv_log(log)["gemini"]) == 2  # three advisers still cross-examine each other
    assert "3/3 advisers" in result.stderr


def test_claude_splits_a_task_into_subtasks(fake_bin, tmp_path):
    log = tmp_path / "argv.log"
    result = run_cli(fake_bin, "--split", "always", "--show-opinions", "Audit the whole app", tmp_path=tmp_path,
                     ARGV_LOG=str(log), FAKE_PLAN="1")
    assert result.returncode == 0, result.stderr
    assert "saw subtasks" in result.stdout and "saw gemini" in result.stdout and "saw codex" in result.stdout
    assert "saw rebuttals" not in result.stdout  # no cross-examination in a split round
    calls = argv_log(log)
    # "nobody" isn't an adviser, so its subtask goes round-robin: gemini gets two subtasks, codex one.
    assert len(calls["gemini"]) == 2 and len(calls["codex"]) == 1
    assert "3/3 subtasks" in result.stderr
    assert "GEMINI · SUBTASKS" in result.stdout and "### Map the schema" in result.stdout


def test_plan_parsing():
    advisers = ("gemini", "openai")
    plan = conclave.parse_plan('Sure: {"plan": [{"agent": "Codex", "title": "Run tests", "instructions": "pytest"}]}', advisers)
    assert plan == [{"agent": "openai", "title": "Run tests", "instructions": "pytest"}]
    assert conclave.parse_plan("BRIEF: no plan here", advisers) is None
    assert conclave.parse_plan('{"plan": "not a list"}', advisers) is None


def test_qwen_style_stream_and_sprites():
    stream = feed("qwen", [{"type": "assistant", "parent_tool_use_id": None,
                            "message": {"content": [{"type": "text", "text": "Qwen answer"}]}},
                           {"type": "result", "subtype": "success", "result": "Qwen answer"}])
    assert stream.reply("qwen", 0, "").text == "Qwen answer"
    failed = feed("qwen", [{"type": "result", "subtype": "error_during_execution", "is_error": True,
                            "error": {"message": "No auth type is selected"}}])
    with pytest.raises(RuntimeError, match="No auth type"):
        failed.reply("qwen", 1, "")
    for agent in conclave_agents.AGENTS.values():
        rows = conclave_agents.sprite_frame(agent.sprite, "blink")
        assert len(rows) == 6 and {len(r) for r in rows} == {7} and "e" not in "".join(rows)
    lines = conclave.sprite("gemini", "working")
    assert len(lines) == 7 and {line.cell_len for line in lines} == {14}


def test_shell_command_detection():
    assert conclave.looks_like_shell("npm install -g @google/gemini-cli")
    assert conclave.looks_like_shell("git status")
    assert not conclave.looks_like_shell("git rebase or merge, which is safer here?")
    assert not conclave.looks_like_shell("Why does the build keep failing")


def test_commands_stream_and_send_prompts_on_stdin():
    command, stdin = conclave.command_for("gemini", "a very long prompt", "gemini-x")
    assert stdin == "a very long prompt"
    assert "a very long prompt" not in command
    assert command[-2:] == ["--model", "gemini-x"]
    assert "stream-json" in command
    assert "stream-json" in conclave.command_for("claude", "p", None)[0]
    assert "--json" in conclave.command_for("openai", "p", None)[0]


# ---------------------------------------------------------------- config

def test_config_accepts_codex_alias(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[models]\nclaude = "sonnet"\ncodex = "gpt-x"\ngemini = ""\n')
    assert conclave.load_models(config) == {"claude": "sonnet", "openai": "gpt-x"}


def test_config_rejects_unknown_provider(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[models]\nclaud = "sonnet"\n')
    with pytest.raises(ValueError, match="Unknown provider 'claud'"):
        conclave.load_models(config)


def test_config_theme(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[ui]\ntheme = "ember"\n')
    assert conclave.load_config(config) == conclave.Settings({}, "ember", True)
    config.write_text('[sessions]\nsave = false\n')
    assert conclave.load_config(config).save_sessions is False
    config.write_text('[ui]\ntheme = "plaid"\n')
    config.write_text('[ui]\ntheme = "plaid"\n')
    with pytest.raises(ValueError, match="Unknown theme 'plaid'"):
        conclave.load_config(config)


# ---------------------------------------------------------------- stream parsing

def feed(provider: str, events: list) -> conclave.Stream:
    stream = conclave.Stream(provider)
    for event in events:
        stream.feed(json.dumps(event) + "\n" if isinstance(event, dict) else event)
    return stream


def test_claude_stream():
    delta = lambda text: {"type": "stream_event", "parent_tool_use_id": None,  # noqa: E731
                          "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}}}
    stream = feed("claude", [
        {"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_start"}},
        delta("Let me look"),
        {"type": "stream_event", "parent_tool_use_id": None, "event": {"type": "message_start"}},  # a new message after a tool
        delta("The answer"),
        {"type": "stream_event", "parent_tool_use_id": "sub", "event": {"type": "content_block_delta",
                                                                         "delta": {"type": "text_delta", "text": "subagent"}}},
    ])
    assert stream.live == "The answer"
    stream.feed(json.dumps({"type": "result", "subtype": "success", "result": "The answer.", "total_cost_usd": 0.5,
                            "usage": {"input_tokens": 1, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3,
                                      "output_tokens": 4}}))
    reply = stream.reply("claude", 0, "")
    assert (reply.text, reply.tokens_in, reply.tokens_out, reply.cost) == ("The answer.", 6, 4, 0.5)


def test_claude_stream_error_mentions_sign_in():
    stream = feed("claude", [{"type": "result", "subtype": "success", "is_error": True,
                              "result": "Failed to authenticate: OAuth session expired"}])
    with pytest.raises(RuntimeError, match="OAuth session expired") as error:
        stream.reply("claude", 1, "")
    assert conclave.fix_for("claude", str(error.value)).startswith("Sign in again")


def test_gemini_stream():
    stream = feed("gemini", [{"type": "tool_use", "tool_name": "glob"}])
    assert stream.activity == "using glob"
    stream = feed("gemini", [{"type": "message", "role": "assistant", "content": "a", "delta": True},
                             {"type": "message", "role": "assistant", "content": "b", "delta": True},
                             {"type": "result", "status": "success", "stats": {"input_tokens": 5, "output_tokens": 2}}])
    assert stream.reply("gemini", 0, "").text == "ab"
    stream = feed("gemini", [{"type": "result", "status": "error", "error": {"message": "quota exceeded"}}])
    with pytest.raises(RuntimeError, match="quota exceeded"):
        stream.reply("gemini", 1, "")


def test_codex_stream():
    stream = feed("openai", [{"type": "error", "message": "Reconnecting... 2/5"},
                             {"type": "item.completed", "item": {"type": "reasoning", "text": "**Reading the diff**\nmore"}}])
    assert stream.activity == "reasoning · Reading the diff"
    stream.feed(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "Done."}}))
    stream.feed(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}))
    assert stream.reply("codex", 0, "").text == "Done."
    failed = feed("openai", [{"type": "turn.failed", "error": {"message": "401 Unauthorized"}}])
    with pytest.raises(RuntimeError, match="401"):
        failed.reply("codex", 1, "")


def test_agreement_line_is_split_from_the_verdict():
    body, agreement, confidence = conclave.split_verdict("Use Postgres.\n\n**COUNCIL: agreement=Partial; confidence=low**")
    assert (body, agreement, confidence) == ("Use Postgres.", "partial", "low")
    assert conclave.split_verdict("No line here.") == ("No line here.", None, None)
    assert conclave.drafting("Use Postgres.\nCOUN") == "Use Postgres."


# ---------------------------------------------------------------- Claude Code conversations

def test_conversations_are_listed_and_digested(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    project = tmp_path / "shop"
    project.mkdir()
    write_conversation(tmp_path / "claude-home", project)
    (listed,) = conclave_context.conversations(project)
    assert (listed.title, listed.messages, listed.errors, listed.branch) == ("Make the tests pass", 3, 1, "main")
    assert conclave_context.projects()[0][0] == str(project)
    for key in ("latest", "1", "abc1"):
        assert conclave_context.find(project, key).id == "abc12345-0000"
    assert conclave_context.find(project, "zzz") is None
    digest = conclave_context.digest(listed.path)
    assert "What the user first asked:** Make the tests pass" in digest
    assert "Errors Claude hit (1" in digest and "KeyError: 'token'" in digest
    assert "Bash: pytest -q" in digest
    assert "SUBAGENT CHATTER" not in digest and "secret" not in digest


def test_conversation_and_project_reach_the_council(fake_bin, tmp_path):
    project = tmp_path / "shop"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    write_conversation(tmp_path / "claude-home", project)
    log = tmp_path / "cwd.log"
    result = run_cli(fake_bin, "--project", str(project), "--conversation", "latest", "Why do the tests fail?",
                     tmp_path=tmp_path, CWD_LOG=str(log))
    assert result.returncode == 0, result.stderr
    assert "saw conversation" in result.stdout and "saw project" in result.stdout
    ran_in = {line.split(" ", 1)[1] for line in log.read_text().splitlines()}
    assert ran_in == {str(project)}  # every model worked inside the project


def test_list_conversations_and_handoff(fake_bin, tmp_path):
    project = tmp_path / "shop"
    project.mkdir()
    write_conversation(tmp_path / "claude-home", project)
    listed = run_cli(fake_bin, "--project", str(project), "--list-conversations", tmp_path=tmp_path)
    assert listed.returncode == 0 and "Make the tests pass" in listed.stdout and "1 error " in listed.stdout
    result = run_cli(fake_bin, "-p", str(project), "-c", "1", "--handoff", "Fix it", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    (saved,) = (project / ".conclave").glob("verdict-*.md")
    assert saved.read_text().startswith("FINAL ANSWER")
    assert (project / ".conclave" / ".gitignore").read_text() == "*\n"
    assert "claude --resume abc12345-0000" in re.sub(r"\s+", " ", result.stderr)


def test_unknown_conversation_is_a_usage_error(fake_bin, tmp_path):
    result = run_cli(fake_bin, "-c", "nope", "Fix it", tmp_path=tmp_path)
    assert result.returncode == 2 and "no Claude Code conversation" in result.stderr


def test_hand_off_to_one_agent(fake_bin, tmp_path):
    log = tmp_path / "argv.log"
    result = run_cli(fake_bin, "--agent", "gemini", "--access", "edit", "Fix the login bug", tmp_path=tmp_path,
                     ARGV_LOG=str(log))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "GEMINI SAYS go\n"
    calls = argv_log(log)
    assert list(calls) == ["gemini"]  # no council: Claude isn't asked for a brief or a verdict
    argv = calls["gemini"][0]
    assert argv[argv.index("--approval-mode") + 1] == "auto_edit"
    assert "Gemini CLI · edit access" in re.sub(r"\s+", " ", result.stderr)
    assert "continue in Gemini CLI" in result.stderr

    result = run_cli(fake_bin, "--to", "chatgpt", "Explain the schema", tmp_path=tmp_path, ARGV_LOG=str(log))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "CODEX SAYS go\n"
    assert "read-only" in argv_log(log)["codex"][0]


def test_access_needs_an_agent(fake_bin, tmp_path):
    result = run_cli(fake_bin, "--access", "full", "Do everything", tmp_path=tmp_path)
    assert result.returncode == 2 and "--agent" in result.stderr
    result = run_cli(fake_bin, "--agent", "nobody", "Hi", tmp_path=tmp_path)
    assert result.returncode == 2 and "chatgpt" in result.stderr


def test_access_levels_and_resumed_sessions():
    def argv(provider, access="read", session=None):
        return conclave.command_for(provider, "hi", None, session=session, access=access)[0]

    assert argv("claude")[argv("claude").index("--permission-mode") + 1] == "plan"
    assert argv("claude", "edit")[argv("claude", "edit").index("--permission-mode") + 1] == "acceptEdits"
    assert "--dangerously-skip-permissions" in argv("claude", "full")
    assert [argv("gemini", level)[2] for level in conclave.ACCESS_LEVELS] == ["plan", "auto_edit", "yolo"]
    assert "workspace-write" in argv("openai", "edit")
    assert "--dangerously-bypass-approvals-and-sandbox" in argv("openai", "full")
    assert "--allow-all-tools" in argv("copilot", "full") and "--deny-tool=shell" in argv("copilot", "edit")
    assert argv("opencode", "edit")[3] == "build"
    resume = conclave.SessionPlan("s-1", resume=True)
    assert argv("claude", "edit", resume)[-2:] == ["--resume", "s-1"]
    assert argv("gemini", "edit", resume)[-2:] == ["--resume", "s-1"]
    codex = argv("openai", "edit", resume)
    assert codex[:3] == ["codex", "exec", "resume"] and codex[-2:] == ["s-1", "-"]
    assert 'sandbox_mode="workspace-write"' in codex and "--sandbox" not in codex


def test_model_choices_come_from_each_tool(fake_bin, tmp_path, monkeypatch):
    install(fake_bin, "ollama", 'print("NAME            ID      SIZE\\nllama3.2:latest  a80c   2.0 GB\\nqwen2.5-coder:7b  b1    4.7 GB")')
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    assert conclave.model_choices("ollama")[:2] == ["llama3.2:latest", "qwen2.5-coder:7b"]


def test_models_are_shown_from_settings_and_streams(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(conclave, "SEEN_MODELS", {})
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "config.toml").write_text('model = "gpt-test"\n')
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "settings.json").write_text('{"model": {"name": "gemini-test-pro"}}')
    assert conclave.model_for("openai", {}) == "gpt-test"
    assert conclave.model_for("gemini", {}) == "gemini-test-pro"
    assert conclave.model_for("claude", {}) == "default model"
    assert conclave.model_for("claude", {"claude": "opus"}) == "opus"
    stream = feed("claude", [{"type": "system", "subtype": "init", "model": "claude-reported"}])
    assert stream.model == "claude-reported" and conclave.model_for("claude", {}) == "claude-reported"
    feed("gemini", [{"type": "init", "model": "gemini-reported"}])
    assert conclave.model_for("gemini", {}) == "gemini-reported"


# ---------------------------------------------------------------- interactive session

ANSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|[()][0-9A-Za-z]|[=>78])")


class Terminal:
    """Drive conclave in a pseudo-terminal, the way a person would."""

    def __init__(self, bin_dir: Path, tmp_path: Path, *args: str, cwd: Path | None = None, **extra: str):
        env = env_for(bin_dir, tmp_path, COLUMNS="120", LINES="50", TERM="xterm-256color", CONCLAVE_NO_ANIMATION="1",
                      **extra)
        self.fd, child = pty.openpty()
        fcntl.ioctl(child, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 120, 0, 0))
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "conclave.py"), *args], stdin=child, stdout=child, stderr=child, env=env,
            cwd=cwd or tmp_path,
        )
        os.close(child)
        self.output = ""
        # Multi-byte characters such as ❯ can be split across reads.
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def expect(self, text: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while text not in ANSI.sub("", self.output):
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"never saw {text!r} in:\n{ANSI.sub('', self.output)[-3000:]}"
            if select.select([self.fd], [], [], remaining)[0]:
                try:
                    chunk = self.decoder.decode(os.read(self.fd, 65536))
                except OSError:
                    break
                # Answer cursor-position requests the way a real terminal does.
                for _ in range(chunk.count("\x1b[6n")):
                    os.write(self.fd, b"\x1b[20;1R")
                self.output += chunk

    def send(self, line: str) -> None:
        self.output = ""
        os.write(self.fd, (line + "\r").encode())  # Enter sends a carriage return in raw mode

    def command(self, line: str, *expected: str) -> None:
        self.send(line)
        for text in expected:
            self.expect(text)
        self.expect("❯")


@pytest.mark.skipif(not hasattr(pty, "openpty"), reason="needs a POSIX terminal")
def test_interactive_session(fake_bin, tmp_path):
    term = Terminal(fake_bin, tmp_path)
    term.expect("THE COUNCIL")
    term.expect("memory 0/3")  # the status bar under the prompt
    term.command("Pick a database", "FINAL ANSWER (saw gemini, saw codex, saw rebuttals)", "AGREEMENT")
    term.command("And for caching?", "saw history", "memory 2/3")
    term.command("/show gemini", "GEMINI SAYS go")
    term.command("/continue codex", "Opening ChatGPT", "RESUMED codex-thread-1")
    term.command("/private", "Rounds are private")
    term.command("/agents", "AGENTS", "Qwen Code", "Ollama")
    term.command("/agents add qwen", "Qwen Code now advises", "isn't installed yet")
    term.command("/agents remove qwen", "left the council")
    term.command("/split always", "splits every task")
    term.command("/model codex gpt-x", "now uses gpt-x")
    term.command("/theme ember", "THEME ] kakeya")
    term.command("/stats", "SESSION", "2 strong")
    term.command("/save answer.md", "Saved to answer.md")
    term.command("/export session.md", "Saved to session.md")
    term.command("/bogus", "Unknown command")
    term.send("/exit")
    term.expect("adjourned")
    assert term.proc.wait(timeout=10) == 0
    assert "saw history" in (tmp_path / "answer.md").read_text()
    transcript = (tmp_path / "session.md").read_text()
    assert "## 1. Pick a database" in transcript and "## 2. And for caching?" in transcript


@pytest.mark.skipif(not hasattr(pty, "openpty"), reason="needs a POSIX terminal")
def test_interactive_hand_offs(fake_bin, tmp_path):
    log = tmp_path / "argv.log"
    term = Terminal(fake_bin, tmp_path, ARGV_LOG=str(log))
    term.expect("memory 0/3")
    term.command("@nobody hello", "No agent called nobody")
    term.command("@gemini Read the schema", "GEMINI · ANSWER", "GEMINI SAYS go")
    term.command("/access edit", "edit access")
    term.command("/to claude", "Talking to Claude Code only")
    term.expect("→ CLAUDE")  # the prompt says where messages go
    term.command("Fix the login", "FINAL ANSWER")
    term.command("And the tests?", "FINAL ANSWER", "claude-test-model")
    term.command("@council Pick a database", "AGREEMENT")
    term.command("/apply chatgpt", "applies the verdict", "CODEX SAYS go")
    term.command("/to council", "whole council again")
    term.send("/exit")
    term.expect("adjourned")
    calls = argv_log(log)
    first, second = [argv for argv in calls["claude"] if "acceptEdits" in argv]
    session = first[first.index("--session-id") + 1]
    assert second[second.index("--resume") + 1] == session  # the follow-up carried on Claude's own session
    assert any(argv[:2] == ["exec", "resume"] or "workspace-write" in argv for argv in calls["codex"])
    assert calls["gemini"][0][calls["gemini"][0].index("--approval-mode") + 1] == "plan"


@pytest.mark.skipif(not hasattr(pty, "openpty"), reason="needs a POSIX terminal")
def test_interactive_shell_commands(fake_bin, tmp_path):
    (tmp_path / "sub").mkdir()
    term = Terminal(fake_bin, tmp_path)
    term.expect("memory 0/3")
    term.command("!echo hello-from-the-shell", "hello-from-the-shell")
    term.send("echo detected-command")
    term.expect("looks like a shell command")
    os.write(term.fd, b"y")
    term.expect("detected-command")
    term.expect("❯")
    term.command("!cd sub", "Now in")
    term.command("!pwd", str(tmp_path / "sub"))
    term.send("/exit")
    term.expect("adjourned")


@pytest.mark.skipif(not hasattr(pty, "openpty"), reason="needs a POSIX terminal")
def test_interactive_conversation_and_handoff(fake_bin, tmp_path):
    project = tmp_path / "shop"
    project.mkdir()
    write_conversation(tmp_path / "claude-home", project)
    term = Terminal(fake_bin, tmp_path, cwd=project)
    term.expect("This folder has 1 Claude Code conversation")
    term.expect("❯")
    term.command("/convos", "Make the tests pass", "1 error")
    term.command("/attach latest", "Attached")
    term.command("Why do the tests fail?", "saw conversation")
    term.command("/handoff", "HAND BACK TO CLAUDE CODE", "claude --resume abc12345-0000")
    term.command("/retry", "Asking again", "saw conversation")
    term.command("/detach", "no longer sent")
    term.send("/exit")
    term.expect("adjourned")
    assert list((project / ".conclave").glob("verdict-*.md"))


# ---------------------------------------------------------------- the app

@pytest.fixture
def app_server(fake_bin, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    app = conclave_app.App({name: None for name in conclave.PROVIDERS}, 30)
    server = conclave_app.serve(app, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def call(base: str, path: str, token: str | None, body=None, host: str | None = None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Conclave-Token"] = token
    if host:
        headers["Host"] = host
    request = Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def events(base: str, run_id: str, token: str, until=("done", "cancelled", "failed")):
    seen = []
    with urlopen(f"{base}/api/runs/{run_id}/events?token={token}", timeout=30) as response:
        for raw in response:
            if raw.startswith(b"data: "):
                seen.append(json.loads(raw[6:]))
                if seen[-1]["type"] in until:
                    return seen
    return seen


def test_app_requires_its_token_and_localhost(app_server):
    app, base = app_server
    with urlopen(base + "/", timeout=10) as page:
        assert app.token in page.read().decode()
    for token, host in ((None, None), ("wrong", None), (app.token, "evil.example:80")):
        with pytest.raises(HTTPError) as error:
            call(base, "/api/info", token, host=host)
        assert error.value.code == 403
    info = call(base, "/api/info", app.token)
    assert info["version"] == conclave.__version__
    seated = {a["id"]: a for a in info["agents"] if a["id"] in ("claude", *info["advisers"])}
    assert set(seated) == {"claude", "gemini", "openai"} and all(a["installed"] for a in seated.values())
    assert {"qwen", "copilot", "opencode", "ollama"} <= {a["id"] for a in info["agents"]}
    assert len(seated["claude"]["sprites"]["idle"]) == 6
    models = call(base, "/api/models", app.token)
    assert models["claude"][:3] == ["opus", "sonnet", "haiku"] and set(models) == set(conclave.AGENTS)


def test_app_runs_a_round_with_a_conversation(app_server, tmp_path):
    app, base = app_server
    project = tmp_path / "shop"
    project.mkdir()
    write_conversation(tmp_path / "claude-home", project)
    assert call(base, "/api/projects", app.token)[0]["path"] == str(project)
    listed = call(base, f"/api/conversations?project={project}", app.token)
    assert listed["conversations"][0]["errors"] == 1
    run = call(base, "/api/runs", app.token, {"question": "Why do the tests fail?", "project": str(project),
                                               "conversation": "abc12345-0000", "history": [["Earlier?", "Yes."]]})
    seen = events(base, run["id"], app.token)
    kinds = [event["type"] for event in seen]
    assert kinds[-1] == "done" and "stream" in kinds and "tokens" in kinds
    assert [e["phase"] for e in seen if e["type"] == "phase"] == ["advise", "cross", "verdict", "done"]
    result = seen[-1]["result"]
    assert "saw conversation" in result["final"] and "saw history" in result["final"]
    assert (result["agreement"], result["confidence"]) == ("strong", "high")
    assert result["opinions"]["gemini"].startswith("GEMINI SAYS go") and "After reading the other adviser" in result["opinions"]["gemini"]
    assert set(result["sessions"]) == {"claude", "gemini", "openai"}
    resumed = call(base, f"/api/runs/{run['id']}/continue", app.token, {"provider": "codex"})
    assert resumed["command"].endswith("codex resume codex-thread-1")
    handed = call(base, f"/api/runs/{run['id']}/handoff", app.token, {})
    assert Path(handed["path"]).parent == project / ".conclave"
    assert "--resume abc12345-0000" in handed["command"]


def test_app_runs_a_split_round(app_server, monkeypatch):
    app, base = app_server
    monkeypatch.setenv("FAKE_PLAN", "1")
    run = call(base, "/api/runs", app.token, {"question": "Audit it", "split": "always", "advisers": ["gemini", "openai"]})
    seen = events(base, run["id"], app.token)
    kinds = [event["type"] for event in seen]
    assert "mode" in kinds and "plan" in kinds and kinds[-1] == "done"
    statuses = [e["status"] for e in seen if e["type"] == "subtask"]
    assert statuses.count("done") == 3
    result = seen[-1]["result"]
    assert result["mode"] == "split" and len(result["plan"]) == 3 and "saw subtasks" in result["final"]


def test_app_hands_off_carries_on_and_applies(app_server, tmp_path, monkeypatch):
    app, base = app_server
    log = tmp_path / "argv.log"
    monkeypatch.setenv("ARGV_LOG", str(log))
    info = call(base, "/api/info", app.token)
    chatgpt = next(a for a in info["agents"] if a["id"] == "openai")
    assert chatgpt["name"] == "ChatGPT" and chatgpt["handle"] == "chatgpt" and chatgpt["style"]["c"] == "#10A37F"
    assert set(info["access"]) == {"read", "edit", "full"}

    run = call(base, "/api/runs", app.token, {"question": "Fix the login", "agent": "claude", "access": "edit"})
    seen = events(base, run["id"], app.token)
    assert seen[-1]["type"] == "done", seen[-1]
    result = seen[-1]["result"]
    assert (result["mode"], result["speaker"], result["access"]) == ("solo", "claude", "edit") and result["thread"]
    assert any(e.get("model") == "claude-test-model" for e in seen if e["type"] == "stream")
    follow = call(base, "/api/runs", app.token, {"question": "And the tests?", "agent": "claude", "access": "edit",
                                                  "thread": result["thread"]})
    assert events(base, follow["id"], app.token)[-1]["result"]["thread"] == result["thread"]

    council = call(base, "/api/runs", app.token, {"question": "Pick a database"})
    assert events(base, council["id"], app.token)[-1]["type"] == "done"
    applied = call(base, "/api/runs", app.token, {"question": "Apply: Pick a database", "agent": "claude", "apply": council["id"]})
    result = events(base, applied["id"], app.token)[-1]["result"]
    assert result["final"] == "APPLIED THE PLAN" and result["access"] == "edit"  # applying never runs read-only

    claude = argv_log(log)["claude"]
    assert "acceptEdits" in claude[0] and claude[1][claude[1].index("--resume") + 1] == result_thread(claude[0])
    for body in ({"question": "x", "agent": "nobody"}, {"question": "x", "agent": "claude", "access": "root"},
                 {"question": "x", "apply": "missing"}):
        with pytest.raises(HTTPError) as error:
            call(base, "/api/runs", app.token, body)
        assert error.value.code == 400


def result_thread(argv: list[str]) -> str:
    return argv[argv.index("--session-id") + 1]


def test_app_rejects_bad_requests(app_server):
    app, base = app_server
    with pytest.raises(HTTPError) as error:
        call(base, "/api/runs", app.token, {"question": "  "})
    assert error.value.code == 400
    with pytest.raises(HTTPError) as error:
        call(base, "/api/conversations?project=/no/such/folder", app.token)
    assert error.value.code == 400


def test_app_cancel_stops_the_round(app_server, fake_bin, tmp_path):
    app, base = app_server
    pidfile = tmp_path / "child.pid"
    install(fake_bin, "claude", FAKE_HANG.replace("{pidfile!r}", repr(str(pidfile))))
    run = call(base, "/api/runs", app.token, {"question": "Hang please"})
    deadline = time.monotonic() + 10
    while not pidfile.exists():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    call(base, f"/api/runs/{run['id']}/cancel", app.token, {})
    seen = events(base, run["id"], app.token)
    assert seen[-1]["type"] == "cancelled"
    time.sleep(0.5)
    assert not running(int(pidfile.read_text()))


def test_mac_app_bundle(tmp_path):
    agents = tmp_path / "LaunchAgents"
    assert conclave_app.install_mac_app(tmp_path, agents, start=False) == 0
    agent = plistlib.loads((agents / "io.github.shrestha-droid.conclave.plist").read_bytes())
    assert agent["ProgramArguments"][-2:] == ["--port", "8765"] and agent["RunAtLoad"] is True
    assert agent["EnvironmentVariables"]["PATH"] == os.environ["PATH"]
    assert agent["KeepAlive"] == {"SuccessfulExit": False}
    contents = tmp_path / "Conclave.app" / "Contents"
    launcher = contents / "MacOS" / "Conclave"
    assert os.access(launcher, os.X_OK)
    script = launcher.read_text()
    assert "export PATH=" in script and "conclave_app.py" in script
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    assert info["CFBundleExecutable"] == "Conclave" and info["CFBundleShortVersionString"] == conclave.__version__
    png = (contents / "Resources" / "Conclave.png").read_bytes()
    assert png.startswith(b"\x89PNG\r\n\x1a\n") and struct.unpack(">II", png[16:24]) == (512, 512)
    assert conclave_app.uninstall_mac_app(tmp_path, agents, stop_now=False) == 0
    assert not (tmp_path / "Conclave.app").exists() and not list(agents.iterdir())
