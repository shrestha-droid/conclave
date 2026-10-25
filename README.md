# Conclave

When a project gets stuck on an error that a fresh pair of eyes would spot, bring it to the council. Conclave reads the conversation you were having, in Claude Code, Gemini CLI or Codex (including the errors it hit), looks at the project's git state, and asks Gemini and ChatGPT for independent opinions while they read the code themselves. Claude, as lead, weighs everything and writes one verdict. Then have an agent apply it to the project, or hand it straight back to the same conversation.

You can also skip the council and work with one agent, the way you would in Claude Code: `@claude fix the login test` (or `@gemini`, `@chatgpt`…) hands the task to that agent, lets it edit the project if you allow it, and keeps one conversation going with it.

It works on any question too, and you can pick the model for each agent. You can seat more advisers (Qwen Code, GitHub Copilot CLI, OpenCode, Ollama, or any command-line AI you add), and for a big task Claude can split the work into subtasks and hand each to the agent best suited to it.

Conclave coordinates the Claude Code, Antigravity (or Gemini) and Codex command-line tools you already have and are signed in to. It doesn't replace them, and their account rules and usage limits apply.

**About ChatGPT:** ChatGPT has no command-line tool of its own, so Conclave reaches it through Codex, OpenAI's coding agent. You sign in to Codex with your ChatGPT account and it runs ChatGPT's GPT models on your ChatGPT plan, which is why Conclave calls it ChatGPT. It is the coding agent, not the chat app: rounds don't appear in your chatgpt.com history, and your ChatGPT memory and custom GPTs aren't used. It can read the project and run commands, which the chat app can't.

## Requirements

- Python 3.11 or newer
- Claude Code CLI (`claude`), signed in
- Antigravity CLI (`agy`) for Gemini, signed in with your Google account
- Codex CLI (`codex`), signed in with your ChatGPT account (this is ChatGPT in Conclave)

Run each of these lines on its own in Terminal. Copy only the command, not any words before it:

```sh
curl -fsSL https://antigravity.google/cli/install.sh | bash
agy
npm install -g @openai/codex@latest
codex login
```

`agy` opens Antigravity CLI so you can sign in with Google; quit it afterwards. For `codex login`, choose **Sign in with ChatGPT**.

**Why Antigravity and not Gemini CLI:** on 18 June 2026 Google stopped serving Gemini CLI to personal accounts (free, Google AI Pro and Ultra), which now get "This client is no longer supported for Gemini Code Assist for individuals". Antigravity CLI is Google's replacement, and Conclave uses it for the Gemini seat whenever `agy` is installed. Gemini CLI still works with a paid Gemini API key or a Code Assist Standard or Enterprise licence; to keep using it, add this to the config:

```toml
[gemini]
cli = "gemini-cli"     # or "antigravity"; the default "auto" prefers Antigravity
``` If `npm` isn't found, install Node.js first (`brew install node`, or from nodejs.org). Or skip all of this: run `conclave`, type `/setup`, and Conclave installs and signs in to each tool for you.

Claude Code installation and sign-in steps are in the [Claude Code docs](https://code.claude.com/docs/en/quickstart).

## Install

```sh
pipx install git+https://github.com/shrestha-droid/conclave.git
```

Install pipx with the [pipx installation guide](https://pipx.pypa.io/latest/how-to/install-pipx.html). From a clone, `pipx install .` works too.

To update, run `conclave update`. It reinstalls the latest version from where you installed it, and restarts the always-on app so the page at localhost:8765 shows the new version too. `conclave app`, `conclave setup` and `conclave install-app` work as well as the `--app`, `--setup` and `--install-app` flags.

## The app

```sh
conclave --app
```

This opens Conclave in your browser: a monospace page in the same purple as the terminal, where each agent works in a card edged in its maker's colour. The sidebar lists every project you've worked on with Claude Code, Gemini CLI or Codex. Click one and it unfolds into its conversations from all three, each marked with its tool and how many errors it hit; the **claude · gemini · chatgpt** tabs above them switch between tools. Pick a conversation, ask a question (or use one of the quick prompts such as "Why does this keep failing, and what's the fix?"), and watch the models work live. Each agent works in its own box, outlined in its maker's colour: Claude clay, Gemini blue, ChatGPT green. Each box shows the model it's running, what it is doing (reading a file, running a test) and its answer as it writes.

**Models** has a field for each agent in play: type any model, or pick one from its list. The list holds Claude's `opus`, `sonnet` and `haiku`, the models Antigravity and Ollama say they have, and any model an agent has reported or is set to in its own settings. Leave a field empty for that tool's default. **Send to** picks who gets the question: the whole council, or one agent. With one agent you also choose its access (**Read**, **Edit** or **Full**, explained below), and follow-ups carry on that agent's conversation.

When the verdict is in, the app shows:

- how strongly the council agreed, and Claude's confidence;
- the time taken, token use and Claude's cost estimate;
- the full brief and opinions, behind expanders.

**Apply with** has an agent (Claude by default) make the verdict's changes in the project, at edit access. **Hand back to Claude Code** saves the verdict into the project (in `.conclave/`, which is git-ignored) and copies a command that resumes your conversation with it:

```sh
cd ~/code/shop-api && claude --resume <session-id> 'The Conclave council (Gemini, ChatGPT and other agents) reviewed this conversation. Read .conclave/verdict-….md …'
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

### Sharing it: everyone runs their own

Conclave is meant to be installed on each laptop that uses it, not hosted once for everyone. Each person installs it with the pipx command above (the repository has to be public, or they need access to it) and signs in to the tools with their own accounts:

- **With a subscription:** Claude Code with a Claude plan, Antigravity CLI with a Google account, Codex with a ChatGPT plan.
- **With API keys instead:** Claude Code reads `ANTHROPIC_API_KEY`; Codex signs in with `codex login --with-api-key`; Gemini CLI reads `GEMINI_API_KEY` (set `[gemini] cli = "gemini-cli"`). Each is billed per use to that person's key.
- **With anything else:** any command-line AI they have can join through `[agents.<name>]` in the config, Ollama included for a free local model.

Each person picks their own access level: you might give hand-offs full access on your laptop, while someone else keeps theirs read-only. Don't share your own sign-ins with other people; Claude, ChatGPT and Google accounts are for one person each.

### Why it isn't hosted on Vercel

Conclave runs on your computer on purpose. It uses the `claude`, `gemini` and `codex` tools you're already signed in to, so it costs nothing beyond your subscriptions. It also reads your conversations and project files from your disk. A hosted site can do neither: it would need separately billed API keys, and you'd have to upload conversations and code to it.

The app only listens on 127.0.0.1, and it only answers requests that carry a secret token from the page it served. That stops other websites from starting rounds. The page loads its typeface (JetBrains Mono) from Google Fonts; offline it falls back to your system's monospace font.

## In the terminal

Run `conclave` on its own for an interactive session. While it runs, Conclave asks your terminal to paint its whole background in the theme's colour, and puts your own back when you leave. iTerm2, Ghostty, kitty, WezTerm and most modern terminals do this; ones that don't simply ignore it, and in macOS Terminal you can set the profile's background to `#05060B` instead. `[ui] background = false` turns it off. Inside a project folder it tells you how many conversations it found there, from Claude Code, Gemini CLI and Codex.

```sh
cd ~/code/shop-api
conclave
```

```text
/convos              list this project's conversations (/convos gemini shows only Gemini CLI's)
/attach latest       bring the newest one to the council (or /attach 2, or a session id)
Why does test_login keep failing after the session refactor?
/apply               have Claude make the verdict's changes in the project
/handoff             or save the verdict and get the command that gives it back to Claude Code
```

| Command | What it does |
| --- | --- |
| `@<agent> <task>` | Hand a task straight to one agent: `@claude`, `@gemini`, `@chatgpt`, `@qwen`… `@council <question>` asks everyone once |
| `/to <agent\|council>` | Send every message to one agent (`@claude` on its own does the same) or back to the council |
| `/access <read\|edit\|full>` | What hand-offs may do (see below). Full asks you to confirm |
| `/apply [agent]` | Have an agent (Claude by default) carry out the last verdict in the project, at edit access or more |
| `/convos [claude\|gemini\|chatgpt\|all]` | List the project's conversations from Claude Code, Gemini CLI and Codex, with message and error counts, or only one tool's |
| `/attach <n\|latest\|id>` | Send a conversation along with each question |
| `/detach` | Stop sending it |
| `/project [path\|none]` | Show or set the folder the models read and review |
| `/handoff` | Save the last verdict in the project and show the `claude --resume` command (copied to the clipboard on macOS) |
| `/continue <agent>` | Open the last round in that agent's own tool and keep talking; exit it to come back |
| `/private` | Toggle saving rounds as sessions in the three tools |
| `/show <agent\|brief\|verdict>` | Print the full text from the last round |
| `/retry` | Ask the last question again |
| `/opinions` | Toggle printing both opinions after each verdict |
| `/agents [add\|remove] [agent]` | List every agent Conclave knows, or seat and unseat advisers |
| `/split [auto\|always\|never]` | Whether Claude may split a big task into subtasks |
| `/quick` | Turn the cross-examination round off or on |
| `/setup` | Check each seated agent is installed and signed in, and fix any that aren't |
| `!<command>` | Run a shell command without leaving Conclave |
| `/model [agent] [name]` | Show the models, or set one (`claude`, `gemini`, `chatgpt`…; `default` resets it) |
| `/theme [name]` | List the themes (sys, branchhq, ring0, mend, spherex, kakeya), or switch |
| `/stats` | Time, tokens, cost and agreement for the session |
| `/timeout <seconds>` | Change the time allowed per model call |
| `/save <file>` / `/export [file]` | Save the last answer, or the whole session, as Markdown |
| `/new`, `/clear`, `/help`, `/exit` | Forget earlier answers, clear the screen, list commands, leave |

While a round runs, the dashboard streams each model's answer and current activity. Claude, for example, shows `using Read`; Codex shows `running pytest -q` or `reasoning · Comparing session keys`. The verdict writes itself into a panel below the cards. The status bar under the prompt shows the model each agent is running (the one you set, else the one its tool reports, else its own settings), the attached project and conversation, follow-up memory, the access level and session token use. When you're talking to one agent, the prompt shows it: `■ CONCLAVE → CLAUDE EDIT ❯`. Type `/` for autocomplete, press → to accept a suggestion from history, and use Alt+Enter for a new line. Ctrl+C cancels a running round.

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
- `--agent claude` (or `--to chatgpt`, and so on) hands the task to one agent instead of the council, and `--access edit|full` lets it change things: `conclave --agent claude --access edit "Fix the failing login test"`.

## Coding with it every day: hand-offs and access

To use Conclave in place of Claude Code, talk to one agent and give it edit access:

```text
/to claude
/access edit
Fix the failing login test, then run the suite.
And add a regression test for it.
@council Is this the right fix, or are we papering over something?
/apply chatgpt
```

`/to claude` sends everything to Claude, and each message carries on the same Claude Code session, so it remembers the work so far, just as in Claude Code (it shows up in `claude --resume` too). `@council` brings in the whole council for one question when you want a second opinion. `/apply` has an agent carry out the verdict. `/new` starts fresh conversations.

| Access | What the agent may do | Claude Code | Gemini (Antigravity CLI) | Gemini CLI | ChatGPT (Codex) |
| --- | --- | --- | --- | --- | --- |
| `read` (default) | read the project; change nothing | plan mode | plan mode | plan mode | read-only sandbox |
| `edit` | edit files in the project | accept edits | accept-edits | auto-edit | workspace-write sandbox (may run commands inside the project) |
| `full` | edit files and run any command on this computer, without asking | skip permissions | skip permissions | yolo | no sandbox |

Antigravity CLI never stops to ask in a headless run: a step the mode doesn't allow is skipped. Rules you've added to its own `permissions.allow` setting still apply at every level.

Qwen Code, Copilot CLI and OpenCode get their own equivalents. The council itself always runs read-only; access applies to hand-offs and `/apply`.

**Full access, email and LinkedIn.** At full access an agent can use anything you've set up in its own tool. For example, if Claude Code has a Gmail or Google Calendar connector, `@claude draft replies to today's unread email` can do it through that connector. Agents are told to ask before anything irreversible, such as sending a message, deleting data or spending money, but full access doesn't enforce that, so use it in a project you've committed and for tasks you'd trust the agent with. Conclave doesn't automate LinkedIn: LinkedIn's terms ban bots and scripted activity, and accounts get restricted for it. Agents can draft posts and messages for you to send yourself.

## More agents, and splitting big tasks

Claude always leads. Gemini and ChatGPT advise by default, and these can join them:

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

## Your rounds in Claude Code, Antigravity and Codex

Each round is also saved in each model's own tool, so you can carry on with any of them there:

- **Claude Code:** the brief and the verdict are one session named "Conclave · your question". It appears in `claude --resume`, and Conclave's conversation list shows it too.
- **Antigravity CLI:** the opinion is a conversation you can open with `agy --conversation <id>` (with Gemini CLI, `gemini --resume <id>`).
- **Codex:** the opinion is kept, and `codex resume <id>` opens it. Codex uses your ChatGPT account.

After each verdict Conclave shows the exact command for each. In the app, **Continue in** buttons open a Terminal window already in that session (on a Mac; elsewhere they copy the command). In the terminal, use `/continue codex` and the like.

claude.ai, gemini.google.com and chatgpt.com don't let any app add conversations to your history, so rounds can't appear there.

Use `--private` for one run, `/private` in a session, the app's **Save sessions** switch, or `[sessions] save = false` in the config, and Claude Code and Codex keep no session. Antigravity and Gemini CLI always keep their own history.

## Settings

Create `~/.config/conclave/config.toml` (or `$XDG_CONFIG_HOME/conclave/config.toml`):

```toml
[models]
claude = "sonnet"
gemini = "GEMINI_MODEL_ID"
chatgpt = "OPENAI_MODEL_ID" # "codex" and "openai" work too

[ui]
theme = "branchhq"          # branchhq (default, purple), sys (signal red), ring0, mend, spherex or kakeya
background = true           # paint the terminal's background in the theme's colour during a session

[sessions]
save = true                 # false: don't keep rounds in Claude Code, Gemini CLI and Codex

[council]
advisers = ["gemini", "codex"]   # add "qwen", "copilot", "opencode", "ollama" or your own
cross_examine = true
split = "auto"                   # "always" or "never"
```

Flags such as `--claude-model`, `--chatgpt-model`, `--theme` and `--timeout` override these for one run. An unknown provider or theme name is reported as an error, so a typo doesn't silently fall back to a default. Use another file with `--config path/to/config.toml`.

## What gets sent where

Your question, and anything you attach, goes to Anthropic, Google and OpenAI through their CLIs (Claude Code, Antigravity CLI or Gemini CLI, and Codex): the condensed conversation, the project's git state and diff, and earlier answers in the session. The models can also read files in the project folder. Don't attach a conversation or project you wouldn't share with all three.

In council rounds all three run read-only: Claude in plan mode, Gemini in plan approval mode and Codex in a read-only sandbox. Hand-offs and `/apply` run at the access you choose (see above). Gemini CLI uses its default approval mode in folders you haven't trusted. In non-interactive runs it still can't edit files or run shell commands without approval. Conclave itself writes only what you ask for (`.conclave/verdict-*.md` on hand-back, and files you name with `/save`, `/export` or `--output`), plus its prompt history and app state in `~/.local/state/conclave`. Each round is also saved as sessions in the three tools unless you make it private (see above).

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
