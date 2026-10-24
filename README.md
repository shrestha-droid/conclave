# AI Council CLI

Claude leads the task, asks Gemini and ChatGPT for independent opinions, then writes the final answer. Pick a different model for each provider whenever you like. The terminal dashboard borrows Branch HQ's midnight-and-violet palette, with a distinct live panel for each model.

AI Council is a command-line interface (CLI): a small program you run from Terminal. It does not replace Claude, Gemini or ChatGPT; it coordinates their installed command-line tools for one task.

## Requirements

- Python 3.11 or newer
- Node.js 20 or newer for Gemini CLI
- Claude Code CLI (`claude`), signed in
- Gemini CLI (`gemini`), signed in
- Codex CLI (`codex`), signed in
- Rich (`pip` installs it with this project)

The advisers run in parallel. Claude gets their replies, decides how to weigh them, and returns one answer. While they work, the dashboard shows each model's status and a preview of its opinion. The request and adviser replies are sent to the provider CLIs; their account rules and usage limits apply. Run the command from a project directory when you want the CLIs to have that project as context.

## Run from source

```sh
python3 aicouncil.py "Compare these options and recommend one"
```

Override each model for one run:

```sh
python3 aicouncil.py \
  --claude-model sonnet \
  --gemini-model GEMINI_MODEL_ID \
  --openai-model OPENAI_MODEL_ID \
  "Review this plan"
```

## Install as a command

```sh
pipx install .
aicouncil "Review this plan"
```

For a fresh clone, `cd` into the project first. Once its public GitHub repository is published, others can install it directly with `pipx install git+https://github.com/shrestha-droid/ai-council-cli.git`.
Install pipx for your operating system using the [pipx installation guide](https://pipx.pypa.io/latest/how-to/install-pipx.html).

## Save model defaults

Create `~/.config/ai-council/config.toml` (or `$XDG_CONFIG_HOME/ai-council/config.toml`):

```toml
[models]
claude = "sonnet"
gemini = "GEMINI_MODEL_ID"
openai = "OPENAI_MODEL_ID"
```

Flags override the saved model for that run. Leave out a model to use the provider CLI's default. You can use another config path with `--config path/to/config.toml`.

## Safety and limits

Claude runs in plan mode, Gemini in plan approval mode, and Codex in a read-only sandbox. Each model call has a 300-second timeout by default; change it with `--timeout`. This version handles one task per invocation and does not keep shared chat history between runs. The terminal inherits the font and background of the app you run it in; its violet accent and panel colors follow Branch HQ.

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
