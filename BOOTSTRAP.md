# BOOTSTRAP: paste this into Claude Code

Open Claude Code in this repo's folder (`cd ~/code/claude-setup && claude`, or the desktop
app's Code tab pointed at it), then paste everything in the box below as your first message.

---

```text
You are setting up my Claude Code environment from this repo (claude-setup). Work module by
module, verify each one before moving on, and keep a checklist in your task list.

GROUND RULES
- Never overwrite an existing file of mine. Back up ~/.claude/settings.json before any change.
  Prefer ./install.sh, which already follows these rules.
- Never type, read or print my passwords, tokens or API keys. Where a secret is needed, tell me
  which setup script to run in my own terminal (they hide what I type), then wait.
- Ask before anything that leaves this Mac: creating a GitHub repo (private only), pushing,
  sending email, creating accounts, accepting terms.
- Explain each module in two or three plain sentences before installing it, and say why it helps.

STEP 1 — Interview me (one short message, numbered questions):
  1. My first name, and every email address I want Claude to know about (school + personal).
  2. OS (this setup assumes macOS; on Linux skip launchd/Keychain parts and tell me the equivalents).
  3. My timezone, and my school's Canvas address if I use Canvas (e.g. canvas.myschool.edu).
  4. Which modules I want. List them from README.md with one line each and recommend:
     core + hooks + life-vault + skills for everyone; semester-tracker + ntfy if I'm a student;
     mods if I like interface extras; mcp servers only for accounts I actually use.
  5. Which accounts I use: Google (how many accounts?), Canvas, Gradescope, Ed, PollEv, Slack,
     Notion, ESPN fantasy, a Lemon Squeezy store.

STEP 2 — Run the installer:
  - First: ./install.sh --dry-run --modules <my picks>   and summarize what it will write.
  - After I say go: CS_NAME=... CS_EMAILS=... CS_TIMEZONE=... CS_CANVAS_HOST=... \
      ./install.sh --yes --modules <my picks>
  - If I already had a ~/.claude/CLAUDE.md, show me a merge of mine with CLAUDE.md.claude-setup
    and apply it only after I approve.

STEP 3 — Finish each module I chose, verifying as you go:
  core:      read ~/.claude/CLAUDE.md back to me in 5 bullets. Create my memory index
             (MEMORY.md) in my memory folder if it doesn't exist; don't copy the example memory.
  hooks:     start a fresh session check: the connectors-first hook should refuse my first
             browser call once. Edit ~/.claude/connectors-first.txt to match my accounts.
  life-vault: ~/life exists with the empty skeleton. File 3 real facts I tell you with `lv add`
             (ask me for them), run `lv reindex`, then `lv health`. Create ~/life/.venv with
             pytest, then run `python3 ~/life/tools/bin/lv-install --hook-only` only if the Stop
             hook isn't already registered. Ask before loading the harvest + sweep launchd jobs.
             Ask separately (default NO) about the iMessage and mail feeds, after reading me the
             Privacy section of modules/life-vault/README.md.
  ntfy:      walk me through installing the ntfy app and subscribing to the topic in
             ~/semester-tracker/.ntfy.env (I'll read the topic myself). Send a test push with
             bash ~/semester-tracker/send-push.sh "hello" and ask me if it arrived.
  semester-tracker: fill config.json, schedule.md and tags.json with my real classes (ask me,
             or read them from Canvas via the canvas MCP once it works). Replace the example
             deadlines. Run verify/check_*.py. Then help me make a PRIVATE GitHub repo for
             ~/semester-heartbeat (ask first), add the NTFY_TOPIC secret (I paste it in GitHub's
             settings page myself), and trigger one workflow run.
  mcp:       one server at a time, only the ones I use. For each: explain it, install it, have
             me run its secret setup script, register it with `claude mcp add -s user ...`, and
             prove it works with one read call through ~/code/scripts/mcp_bridge.py.
             Google: walk me through creating the Google Cloud OAuth client (modules/mcp/README.md).
  mods:      restart instructions, then check /todo opens and the band shows.
  skills:    show me /hw and /verify-date on a fake example.

STEP 4 — Wrap up: print a table of every module (installed / skipped / needs me), every
launchd job (loaded or not), every MCP server (registered and tested or not), and anything
waiting on me. Save a memory file describing my setup.
```

---

If something goes wrong halfway, paste the same prompt again: the installer is safe to re-run
and skips anything that already exists.
