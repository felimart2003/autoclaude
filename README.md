# autoclaude

Pulls project prompts from the **Claude Projects** database in Notion and makes Claude Code build them autonomously — one project per token window if you want.

- Notion database: [Claude Projects](https://app.notion.com/p/10aefa04a0d14dc4add853782342b841) (under Coding → 🤖 Projects)
- Each **page in the database is one project**; the page *content* is the prompt fed to Claude Code.
- Status lives in the title: no marker = pending, `🔄` = running, `✅` = done, `❌` = failed.
  Remove the emoji from a title to re-queue that project.

## One-time setup

1. **Notion token** — the script talks to Notion directly, so it needs its own key:
   - Go to <https://www.notion.so/profile/integrations> → **New integration** (internal, your workspace).
   - Copy the secret (`ntn_...`).
   - Open the *Claude Projects* database in Notion → `•••` menu → **Connections** → add your integration.
   - `copy .env.example .env` and paste the token into `.env`.
2. **Claude Code CLI** — already handled: the script auto-finds the CLI bundled with your
   Claude desktop app (`%APPDATA%\Claude\claude-code\<version>\claude.exe`). It uses your
   existing Claude login, so no extra auth.

### Notion OAuth Redirect URI (if using OAuth)

- For local development register an exact localhost callback, e.g. `http://localhost:3000/notion/callback`.
- For production register an HTTPS endpoint on your domain, e.g. `https://yourapp.example.com/notion/callback`.
- The Redirect URI you register in Notion must match the `redirect_uri` sent in the OAuth request exactly, including path and any trailing slash.
- Notion requires HTTPS for non-localhost endpoints; localhost callbacks are acceptable for development.
- If you're using an internal integration (the default flow for this project), you do not need OAuth/redirect URIs — just add the integration to your workspace and use the integration secret.

## Usage

```bat
python autoclaude.py --list        :: show pending projects in run order
python autoclaude.py --dry-run     :: show the prompt that would run, change nothing
python autoclaude.py               :: run the next pending project once
python autoclaude.py --watch       :: keep going: run projects back-to-back, sleep through
                                   ::   the 5-hour token resets, poll for new projects
```

Or just double-click `start_autoclaude.bat` (watch mode).

### Choosing model and thinking level

Priority order: command line > Notion page property > default.

- **Per project (in Notion):** set the `Model` (opus/sonnet/haiku) and `Thinking`
  (off/low/medium/high) selects on the database row. `Priority` (lower = sooner) controls run order.
- **Per run (command line):** `--model opus --thinking high`
- **Default:** `AUTOCLAUDE_MODEL` / `AUTOCLAUDE_THINKING` in `.env` (sonnet, Claude Code default thinking).

Thinking maps to the `MAX_THINKING_TOKENS` env var Claude Code reads:
off=0, low=4000, medium=12000, high=31999.

### Token resets

When Claude Code reports the usage limit, the project is put back to *pending*, and watch
mode sleeps until the reset timestamp from the error message (+2 min buffer), then retries.
So you can leave `--watch` running and it will naturally do roughly one batch of work per
5-hour window.

### Where things go

- Code is written to `workspaces\<project-slug>\` (one folder per project).
- Full transcripts go to `logs\`.
- A summary of each run is posted as a **comment on the Notion page**.

## Safety note

By default Claude Code runs with `--permission-mode bypassPermissions` so it can work
unattended (install packages, run commands, write files) inside the workspace folder.
That means it executes shell commands without asking. If you want it more conservative,
set `AUTOCLAUDE_PERMISSION_MODE=acceptEdits` in `.env` — but unattended runs may then
stall or skip steps that need Bash.

## Run it automatically on login (optional)

Watch mode is the simplest: start it once and leave it. To have Windows start it at logon:

```powershell
schtasks /Create /TN "autoclaude" /SC ONLOGON /TR "\"D:\felim\Documents\Coding\autoclaude\start_autoclaude.bat\"" /F
```
