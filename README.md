# Conclave

When a Claude Code project gets stuck on an error that a fresh pair of eyes would spot, bring it to the council. Conclave reads the Claude Code conversation you were having (including the errors Claude hit), looks at the project's git state, and asks Gemini and Codex for independent opinions while they read the code themselves. Claude, as lead, weighs everything and writes one verdict, and you can hand that verdict straight back to the same Claude Code conversation.

It works on any question too: Claude leads, Gemini and Codex advise, and you can pick the model for each. You can seat more advisers (Qwen Code, GitHub Copilot CLI, OpenCode, Ollama, or any command-line AI you add), and for a big task Claude can split the work into subtasks and hand each to the agent best suited to it.

Conclave coordinates the Claude Code, Gemini and Codex command-line tools you already have and are signed in to. It doesn't replace them, and their account rules and usage limits apply.

## Requirements

- Python 3.11 or newer
- Claude Code CLI (`claude`), signed in
- Gemini CLI (`gemini`), signed in (needs Node.js 20 or newer)
- Codex CLI (`codex`), signed in

```sh
npm install -g @google/gemini-cli && gemini        # then choose a sign-in method
npm install -g @openai/codex@latest && codex login
```

Claude Code installation and sign-in steps are in the [Claude Code docs](https://code.claude.com/docs/en/quickstart).

## Install

```sh
pipx install git+https://github.com/shrestha-droid/conclave.git
```

Install pipx with the [pipx installation guide](https://pipx.pypa.io/latest/how-to/install-pipx.html). To update later, run the same command with `--force`. From a clone, `pipx install .` works too.

## The app

```sh
conclave --app
```

This opens Conclave in your browser. The sidebar lists every project you have used Claude Code in, and each project's conversations show how many errors they hit. Pick a conversation, ask a question (or use one of the quick prompts such as "Why does this keep failing, and what's the fix?"), and watch the three models work live. Each one's card shows what it is doing, such as reading a file or running a test, and streams its answer as it writes.

When the verdict is in, the app shows:

- how strongly the council agreed, and Claude's confidence;
- the time taken, token use and Claude's cost estimate;
- the full brief and opinions, behind expanders.

**Hand back to Claude Code** saves the verdict into the project (in `.conclave/`, which is git-ignored) and copies a command that resumes your conversation with it:

```sh
cd ~/code/shop-api && claude --resume <session-id> 'Gemini and Codex reviewed this conversation in Conclave. Read .conclave/verdict-….md …'
```

### Always on, for every project (macOS)

```sh
conclave --install-app
```

This keeps Conclave running in the background from the moment you log in, always at **http://localhost:8765**, so you can bookmark it. It also adds Conclave.app to Applications, so you can open it from Spotlight, Launchpad or the Dock. The sidebar shows every project you've used Claude Code in, so one bookmark covers all of them.

- The **Quit** button in the page stops Conclave until you next log in or open Conclave.app.
- Logs go to `~/Library/Logs/Conclave.log`.
- It remembers the `PATH` from when you installed it, so it can find `claude`, `gemini` and `codex`. Run `--install-app` again if you move them.
- `conclave --uninstall-app` removes the app and the login item.

On Linux and Windows, run `conclave --app` when you want it.

### Why it isn't hosted on Vercel

Conclave runs on your computer on purpose. It uses the `claude`, `gemini` and `codex` tools you're already signed in to, so it costs nothing beyond your subscriptions. It also reads your Claude Code conversations and project files from your disk. A hosted site can do neither: it would need separately billed API keys, and you'd have to upload conversations and code to it.

The app only listens on 127.0.0.1, and it only answers requests that carry a secret token from the page it served. That stops other websites from starting rounds.

## In the terminal

Run `conclave` on its own for an interactive session. Inside a project folder it tells you how many Claude Code conversations it found.

```sh
cd ~/code/shop-api
conclave
```

```text
/convos              list this project's Claude Code conversations
/attach latest       bring the newest one to the council (or /attach 2, or a session id)
Why does test_login keep failing after the session refactor?
/handoff             save the verdict and get the command that gives it back to Claude Code
```

| Command | What it does |
| --- | --- |
| `/convos` | List the project's Claude Code conversations, with message and error counts |
| `/attach <n\|latest\|id>` | Send a conversation along with each question |
| `/detach` | Stop sending it |
| `/project [path\|none]` | Show or set the folder the models read and review |
| `/handoff` | Save the last verdict in the project and show the `claude --resume` command (copied to the clipboard on macOS) |
| `/continue <claude\|gemini\|codex>` | Open the last round in that model's own tool and keep talking; exit it to come back |
| `/private` | Toggle saving rounds as sessions in the three tools |
| `/show <gemini\|codex\|brief\|verdict>` | Print the full text from the last round |
| `/retry` | Ask the last question again |
| `/opinions` | Toggle printing both opinions after each verdict |
| `/agents [add\|remove] [agent]` | List every agent Conclave knows, or seat and unseat advisers |
| `/split [auto\|always\|never]` | Whether Claude may split a big task into subtasks |
| `/quick` | Turn the cross-examination round off or on |
| `/setup` | Check each seated agent is installed and signed in, and fix any that aren't |
| `!<command>` | Run a shell command without leaving Conclave |
| `/model [provider] [name]` | Show the models, or set one (`claude`, `gemini` or `codex`; `default` resets it) |
| `/theme [name]` | List the themes (violet, ember, aurora, matrix, mono), or switch |
| `/stats` | Time, tokens, cost and agreement for the session |
| `/timeout <seconds>` | Change the time allowed per model call |
| `/save <file>` / `/export [file]` | Save the last answer, or the whole session, as Markdown |
| `/new`, `/clear`, `/help`, `/exit` | Forget earlier answers, clear the screen, list commands, leave |

While a round runs, the dashboard streams each model's answer and current activity. Claude, for example, shows `using Read`; Codex shows `running pytest -q` or `reasoning · Comparing session keys`. The verdict writes itself into a panel below the cards. The status bar under the prompt shows the models, the attached project and conversation, follow-up memory and session token use. Type `/` for autocomplete, press → to accept a suggestion from history, and use Alt+Enter for a new line. Ctrl+C cancels a running round.

### One-off questions

```sh
conclave --project ~/code/shop-api --conversation latest "Why do the tests fail?"
conclave -p ~/code/shop-api --list-conversations
conclave -p ~/code/shop-api -c 2 --handoff "Review Claude's last change for bugs"
git diff | conclave "Is this change safe to merge?"
conclave "Compare these options" > answer.md
```

- `--project` (`-p`) runs every model inside that folder and includes its branch, recent commits, uncommitted changes and diff.
- `--conversation` (`-c`) adds a condensed Claude Code transcript: what you first asked, every error Claude hit, then as much of the recent conversation as fits. Without `--project`, Conclave uses the folder the conversation was in.
- Piped text is added to the task as material to review.
- The dashboard is drawn on stderr, so stdout carries only the verdict; a one-line summary with agreement and token use follows on stderr.
- `--output FILE` saves the verdict, and `--show-opinions` adds both opinions.

## More agents, and splitting big tasks

Claude always leads. Gemini and Codex advise by default, and these can join them:

| Agent | Install | Runs as |
| --- | --- | --- |
| Qwen Code (`qwen`) | `npm install -g @qwen-code/qwen-code` | plan mode: analyses only |
| GitHub Copilot CLI (`copilot`) | `npm install -g @github/copilot` | write and shell tools denied |
| OpenCode (`opencode`) | `npm install -g opencode-ai` | its read-only `plan` agent |
| Ollama (`ollama`) | [ollama.com/download](https://ollama.com/download) | a local model, private and offline |

Seat them with `/agents add qwen`, the app's Council row, `--advisers gemini,codex,qwen`, or `[council] advisers` in the config. Any other command-line AI that reads a prompt on stdin can join too:

```toml
[agents.mistral]
command = "ollama run mistral"
label = "Mistral"
strengths = "small local model, private"
```

**Cross-examination.** After the advisers answer, each one reads the others' opinions and says where they're wrong before Claude writes the verdict. It's on by default; `--quick` or `/quick` skips it.

**Splitting.** With `split = "auto"` (the default), Claude decides as it reads the task. A big task that splits into independent parts becomes a plan of 2 to 6 subtasks, each given to the adviser whose strengths fit it. Each adviser works through its own subtasks, all of them at the same time, and Claude combines the results. Use `--split always|never` or `/split` to choose.

## Your rounds in Claude Code, Gemini CLI and Codex

Each round is also saved in each model's own tool, so you can carry on with any of them there:

- **Claude Code:** the brief and the verdict are one session named "Conclave · your question". It appears in `claude --resume`, and Conclave's conversation list shows it too.
- **Gemini CLI:** the opinion is a session you can open with `gemini --resume <id>`.
- **Codex:** the opinion is kept, and `codex resume <id>` opens it. Codex uses your ChatGPT account.

After each verdict Conclave shows the exact command for each. In the app, **Continue in** buttons open a Terminal window already in that session (on a Mac; elsewhere they copy the command). In the terminal, use `/continue codex` and the like.

claude.ai, gemini.google.com and chatgpt.com don't let any app add conversations to your history, so rounds can't appear there.

Use `--private` for one run, `/private` in a session, the app's **Save sessions** switch, or `[sessions] save = false` in the config, and Claude Code and Codex keep no session. Gemini CLI always keeps its own session history.

## Settings

Create `~/.config/conclave/config.toml` (or `$XDG_CONFIG_HOME/conclave/config.toml`):

```toml
[models]
claude = "sonnet"
gemini = "GEMINI_MODEL_ID"
codex = "OPENAI_MODEL_ID"   # "openai" works too

[ui]
theme = "aurora"            # violet, ember, aurora, matrix or mono

[sessions]
save = true                 # false: don't keep rounds in Claude Code, Gemini CLI and Codex

[council]
advisers = ["gemini", "codex"]   # add "qwen", "copilot", "opencode", "ollama" or your own
cross_examine = true
split = "auto"                   # "always" or "never"
```

Flags such as `--claude-model`, `--theme` and `--timeout` override these for one run. An unknown provider or theme name is reported as an error, so a typo doesn't silently fall back to a default. Use another file with `--config path/to/config.toml`.

## What gets sent where

Your question, and anything you attach, goes to Anthropic, Google and OpenAI through their CLIs: the condensed conversation, the project's git state and diff, and earlier answers in the session. The models can also read files in the project folder. Don't attach a conversation or project you wouldn't share with all three.

All three run read-only: Claude in plan mode, Gemini in plan approval mode and Codex in a read-only sandbox. Gemini CLI uses its default approval mode in folders you haven't trusted. In non-interactive runs it still can't edit files or run shell commands without approval. Conclave itself writes only what you ask for (`.conclave/verdict-*.md` on hand-back, and files you name with `/save`, `/export` or `--output`), plus its prompt history and app state in `~/.local/state/conclave`. Each round is also saved as sessions in the three tools unless you make it private (see above).

## Limits and behaviour

- **Timeouts:** each model call has a 300-second timeout (`--timeout`). On a timeout or Ctrl+C, Conclave stops the CLI and every process it started.
- **Missing advisers:** if one adviser fails, Claude answers with the opinion it has and says one was missing. Failures are shown with the fix Conclave recognizes, such as the sign-in or install command.
- **Changed output formats:** Conclave reads each CLI's streaming JSON output. If a CLI's format changes so that no answer can be read, Conclave retries that model once with plain-text output.
- **Silent stdin:** if a task is given as words and nothing arrives on a piped stdin within 2 seconds (some task runners leave an empty pipe open), Conclave goes ahead without it.
- **Exit status:** 0 when Claude writes the verdict, 1 when it can't, 2 for a usage or config error, and 130 when cancelled.

## Run the tests

The tests use stand-in `claude`, `gemini` and `codex` programs that speak each CLI's streaming format, so they need no accounts or network access:

```sh
pip install -e '.[test]'
pytest
```
