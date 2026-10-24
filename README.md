# Conclave

Claude leads the task, asks Gemini and Codex for independent opinions, then writes the final answer. Pick a different model for each provider whenever you like. The terminal dashboard borrows Branch HQ's midnight-and-violet palette, with a distinct live panel for each model.

Conclave is a command-line interface (CLI): a small program you run from Terminal. It does not replace Claude, Gemini or ChatGPT; it coordinates Claude Code, Gemini CLI and Codex CLI for one task.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer for Gemini CLI
- Claude Code CLI (`claude`), signed in
- Gemini CLI (`gemini`), signed in
- Codex CLI (`codex`), signed in
- Rich and prompt_toolkit (`pip` and `pipx` install them with this project)

The advisers run in parallel. Claude gets their replies, decides how to weigh them, and returns one answer. While they work, the dashboard shows each model's status and a preview of its opinion. The request and adviser replies are sent to the provider CLIs; their account rules and usage limits apply. Run the command from a project directory when you want the CLIs to have that project as context.

## Start a session

Type `conclave` on its own to open an interactive session. It shows the council (which model each provider uses and whether its CLI is installed, with the install command for any that are missing), then waits at the `✦ conclave ❯` prompt. Type a task and press Enter. A phase tracker moves through Brief, Deliberation and Verdict while each model's card shows what it is doing and for how long, and Claude's verdict appears below with the total time. Keep typing to ask follow-ups: each one includes your last three answers from the session as context.

The status bar under the prompt shows each provider's model, how many earlier answers the next follow-up will carry, whether opinions are shown, and the timeout. Type `/` to see the commands with autocomplete. Earlier tasks appear as grey suggestions; press → to accept one. Alt+Enter (or Esc then Enter) starts a new line, and pasting several lines keeps them together as one task.

```sh
conclave
```

| Command | What it does |
| --- | --- |
| `/model` | Show the council's models |
| `/model gemini MODEL_ID` | Use another model for one provider (`claude`, `gemini` or `codex`); `default` resets it |
| `/opinions` | Show or hide Gemini's and Codex's full replies |
| `/timeout 600` | Change the seconds allowed per model call |
| `/save answer.md` | Save the last answer as Markdown |
| `/new` | Forget earlier answers and start fresh |
| `/clear` | Clear the screen |
| `/help` | List the commands |
| `/exit` | Leave (Ctrl+D also works) |

Ctrl+C cancels the task that is running and returns you to the prompt. The up and down arrow keys recall earlier tasks, including ones from previous sessions. If a model fails, Conclave shows the error with the fix it recognizes, such as the command to sign in again or install a missing CLI. Add `--no-animation` (or set `CONCLAVE_NO_ANIMATION=1`) to skip the start-up animation.

You can still run a single task without opening a session, as below.

## Run from source

```sh
python3 conclave.py "Compare these options and recommend one"
```

Override each model for one run:

```sh
python3 conclave.py \
  --claude-model sonnet \
  --gemini-model GEMINI_MODEL_ID \
  --openai-model OPENAI_MODEL_ID \
  "Review this plan"
```

Include a file or other command output with the task by piping it in. The piped text is added to the task as material to review:

```sh
cat plan.md | python3 conclave.py "Review this plan"
git diff | python3 conclave.py "Is this change safe to merge?"
```

The live dashboard is drawn on stderr, so stdout carries only the final answer. Redirect or pipe it like any other command output, or save it with `--output`:

```sh
python3 conclave.py "Compare these options" > answer.md
python3 conclave.py --output answer.md --show-opinions "Compare these options"
```

`--show-opinions` prints Gemini's and Codex's full replies after Claude's answer. Press Ctrl+C at any time to cancel; Conclave stops the running model CLIs before it exits.

## Install as a command

```sh
pipx install .
conclave "Review this plan"
```

For a fresh clone, `cd` into the project first. Install the published project directly with `pipx install git+https://github.com/shrestha-droid/conclave.git`.
Install pipx for your operating system using the [pipx installation guide](https://pipx.pypa.io/latest/how-to/install-pipx.html).

## Save model defaults

Create `~/.config/conclave/config.toml` (or `$XDG_CONFIG_HOME/conclave/config.toml`):

```toml
[models]
claude = "sonnet"
gemini = "GEMINI_MODEL_ID"
openai = "OPENAI_MODEL_ID"
```

You can write `codex` instead of `openai`. An unknown provider name in `[models]` is reported as an error, so a typo does not silently fall back to the default model. Flags override the saved model for that run. Leave out a model to use the provider CLI's default. You can use another config path with `--config path/to/config.toml`.

## Run the tests

The tests use stand-in `claude`, `gemini` and `codex` programs, so they need no accounts or network access:

```sh
pip install -e '.[test]'
pytest
```

## Safety and limits

Claude runs in plan mode, Gemini in plan approval mode, and Codex in a read-only sandbox. Gemini CLI switches plan mode to its default approval mode in a folder you have not trusted; in non-interactive runs it still cannot edit files or run shell commands without approval. Each model call has a 300-second timeout by default; change it with `--timeout`. When a call times out, Conclave stops that CLI and any processes it started. If an adviser fails, Claude still answers with the opinion it did get and says an adviser was missing. Conclave exits with status 0 when Claude writes the final answer, 1 when it cannot, 2 for a usage or config error, and 130 when cancelled. An interactive session sends your last three answers along with each follow-up; a single-task run starts with no history. The terminal inherits the font and background of the app you run it in; its violet accent and panel colors follow Branch HQ.

Install and sign in to Gemini CLI if it is missing:

```sh
npm install -g @google/gemini-cli
gemini
```

Install Codex CLI if needed:

```sh
npm install -g @openai/codex@latest
```

Claude Code installation and sign-in instructions are in the [Claude Code docs](https://code.claude.com/docs/en/quickstart). See the [Gemini CLI project](https://github.com/google-gemini/gemini-cli) and [Codex CLI project](https://github.com/openai/codex) for their current setup instructions.
