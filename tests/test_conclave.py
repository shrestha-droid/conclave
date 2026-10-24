"""Tests run Conclave against fake claude, gemini and codex executables placed on PATH."""

import codecs
import fcntl
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import conclave  # noqa: E402

FAKE_CLAUDE = """
import sys
prompt = sys.stdin.read()
if "Prepare a concise review brief" in prompt:
    print("BRIEF: weigh cost against speed")
else:
    notes = []
    if "<input>" in prompt:
        notes.append("saw piped input")
    if "GEMINI SAYS" in prompt:
        notes.append("saw gemini")
    if "CODEX SAYS" in prompt:
        notes.append("saw codex")
    if "did not respond" in prompt:
        notes.append("told about missing adviser")
    if "<previous_exchange>" in prompt:
        notes.append("saw history")
    print("FINAL ANSWER (" + ", ".join(notes) + ")")
"""

FAKE_ADVISER = """
import sys
prompt = sys.stdin.read()
assert "BRIEF: weigh cost against speed" in prompt
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
    install(bin_dir, "gemini", FAKE_ADVISER.replace("{name}", "GEMINI"))
    install(bin_dir, "codex", FAKE_ADVISER.replace("{name}", "CODEX"))
    return bin_dir


def run_cli(bin_dir: Path, *args: str, stdin: str = "", tmp_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}", XDG_CONFIG_HOME=str(tmp_path / "config"))
    return subprocess.run(
        [sys.executable, str(ROOT / "conclave.py"), *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_answer_goes_to_stdout_when_piped(fake_bin, tmp_path):
    result = run_cli(fake_bin, "Pick a database", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "FINAL ANSWER (saw gemini, saw codex)\n"


def test_piped_input_is_added_to_the_task(fake_bin, tmp_path):
    result = run_cli(fake_bin, "Review this plan", stdin="step one\nstep two", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "saw piped input" in result.stdout


def test_task_can_come_only_from_stdin(fake_bin, tmp_path):
    result = run_cli(fake_bin, stdin="Pick a database", tmp_path=tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("FINAL ANSWER")


def test_missing_task_is_a_usage_error(fake_bin, tmp_path):
    result = run_cli(fake_bin, tmp_path=tmp_path)
    assert result.returncode == 2
    assert "give a task" in result.stderr


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


def test_gemini_prompt_is_sent_on_stdin():
    command, stdin = conclave.command_for("gemini", "a very long prompt", "gemini-x")
    assert stdin == "a very long prompt"
    assert "a very long prompt" not in command
    assert command[-2:] == ["--model", "gemini-x"]


def test_config_accepts_codex_alias(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[models]\nclaude = "sonnet"\ncodex = "gpt-x"\ngemini = ""\n')
    assert conclave.load_models(config) == {"claude": "sonnet", "openai": "gpt-x"}


def test_config_rejects_unknown_provider(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[models]\nclaud = "sonnet"\n')
    with pytest.raises(ValueError, match="Unknown provider 'claud'"):
        conclave.load_models(config)


ANSI = re.compile(r"\x1b(\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*\x07|[()][0-9A-Za-z]|[=>78])")


class Terminal:
    """Drive conclave in a pseudo-terminal, the way a person would."""

    def __init__(self, bin_dir: Path, tmp_path: Path, *args: str):
        env = dict(
            os.environ,
            PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            XDG_CONFIG_HOME=str(tmp_path / "config"),
            XDG_STATE_HOME=str(tmp_path / "state"),
            COLUMNS="120",
            LINES="50",
            TERM="xterm-256color",
            CONCLAVE_NO_ANIMATION="1",
        )
        self.fd, child = pty.openpty()
        fcntl.ioctl(child, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 120, 0, 0))
        self.proc = subprocess.Popen(
            [sys.executable, str(ROOT / "conclave.py"), *args], stdin=child, stdout=child, stderr=child, env=env, cwd=tmp_path
        )
        os.close(child)
        self.output = ""
        # Multi-byte characters such as ❯ can be split across reads.
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def expect(self, text: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while text not in ANSI.sub("", self.output):
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"never saw {text!r} in:\n{self.output[-3000:]}"
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


@pytest.mark.skipif(not hasattr(pty, "openpty"), reason="needs a POSIX terminal")
def test_interactive_session(fake_bin, tmp_path):
    term = Terminal(fake_bin, tmp_path)
    term.expect("THE COUNCIL")
    term.expect("memory 0/3")  # the status bar under the prompt
    term.send("Pick a database")
    term.expect("FINAL ANSWER (saw gemini, saw codex)")
    term.expect("❯")
    term.send("And for caching?")
    term.expect("saw history")
    term.expect("memory 2/3")
    term.expect("❯")
    term.send("/model codex gpt-x")
    term.expect("now uses gpt-x")
    term.expect("❯")
    term.send("/save answer.md")
    term.expect("Saved to answer.md")
    term.expect("❯")
    term.send("/bogus")
    term.expect("Unknown command")
    term.expect("❯")
    term.send("/exit")
    term.expect("adjourned")
    assert term.proc.wait(timeout=10) == 0
    assert "saw history" in (tmp_path / "answer.md").read_text()
