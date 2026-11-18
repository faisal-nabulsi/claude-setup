# autohub: your own Zapier, on your Mac

autohub watches your apps and pushes your phone when something new shows up. It has no task
meter and no subscription.

**How it works:**
- Every 5 minutes, macOS's built-in scheduler (**launchd**, the thing that runs background
  jobs on a Mac) starts `autohub.py tick`.
- The runner checks whichever automations are due and pushes anything new through **ntfy**
  (see `../../ntfy/README.md`). It reads your ntfy topic from `~/semester-tracker/.ntfy.env`
  (or the folder in `SEMESTER_TRACKER`).
- Then it exits, so it uses no memory between checks. Claude doesn't need to be open.

Each automation is a **recipe**: a small JSON file in `recipes/` that says what to watch, how
often, and what title to push under.

## The example recipes

| Recipe | Watches | How often | Example push |
|---|---|---|---|
| `canvas-posted` | New announcements, assignments, module items, pages, and new links on watched pages in the Canvas courses you list | every 10 min | `CS 101: 'Lecture 12 slides' added to Week 6` |
| `grade-posted` | New grades and regrades on Gradescope and Canvas (pushed once if both show the same exam) | every 30 min | `BIO 101: 'Midterm 1' graded` |

Source kinds you can use: `canvas_posted`, `grades`, `ls_sales` and `ls_weekly` (a Lemon
Squeezy store; set `AUTOHUB_STORE_LABEL` for the push title), and `mcp_call`, which calls any
allowed connector tool and pushes rows it hasn't seen.

**Rules every recipe follows:**
- **First run:** a recipe's first run only notes what already exists, so you never get a flood of old items.
- **Bundling:** if several things arrive at once, they come as one push. After 5 lines it says "+N more".
- **Quiet hours:** from 1am to 7am nothing pushes. Anything held comes in one bundle at the first check after 7am.
- **Failures:** if a source fails 3 checks in a row (for example an expired key), you get one push saying so.

## One-time setup (you do these, never Claude)

1. **Canvas key:** autohub has its own key, separate from the Canvas connector's, so you can revoke it on its own.
   - In Canvas, go to Account → Settings → **+ New Access Token** (purpose: autohub) and copy the token.
   - Run `sh ~/code/autohub/setup-secrets.sh` and paste the token twice when asked. It isn't shown on screen.
2. **Lemon Squeezy key** (only for store recipes): run `sh ~/code/lemonsqueezy-mcp/set-key.sh`.
3. **Install:** the installer copies this folder to `~/code/autohub`, builds `.venv` (`python3 -m venv .venv && .venv/bin/pip install "mcp>=1.2,<2"`), loads `launchd/autohub.plist` and registers the connector: `claude mcp add -s user autohub -- ~/code/autohub/.venv/bin/python ~/code/autohub/mcp_server.py`.

## Dry-run vs live

Every recipe starts in **dry-run**: it does everything except push, and writes "WOULD PUSH ..." to the log instead. A recipe goes live only when you say so in chat. Claude then runs:

```
python3 ~/code/autohub/autohub.py set-mode grade-posted live
```

To go back, use `set-mode <id> dry_run`.

## Add a recipe

**In chat (easiest):** say something like "autohub: push me when X happens". Claude uses the `autohub` connector's tools:
- `add_recipe` saves it, always in dry-run.
- `run_now` previews it.
- `set_mode` switches it to live, after you say yes.

**By hand:** copy a file in `recipes/` and change it. Recipes can use these kinds of source:
- `canvas_posted`, `grades`, `ls_sales`, `ls_weekly`
- `mcp_call`, which calls any connector tool on the allowed list and pushes rows it hasn't seen. You set `server`, `tool`, `args`, `list_path`, `id_field` and `text_template`.

**The allowed list:** `allowed_sources.json` lists the connector tools autohub may call. They are all read tools; for Google mail, only search and read are allowed. Adding anything to it needs your OK. The only thing autohub can ever do is push your own phone. It cannot send, delete or change anything in your accounts, and nothing in it can move money.

## See what it's doing

```
python3 ~/code/autohub/autohub.py list          # each recipe: on/off, mode, schedule, last run, failures
python3 ~/code/autohub/autohub.py runs -n 20    # recent runs (found / new / pushed / error)
python3 ~/code/autohub/autohub.py run grade-posted --preview   # what it would push right now; changes nothing
tail -50 ~/Library/Logs/autohub.log             # the job's log, including WOULD PUSH lines
```

State lives in `~/.local/state/autohub/`: what each recipe has seen, the run history, and pushes held during quiet hours. `init <id>` makes a recipe forget what it has seen and quietly re-learn it, with no pushes.

## Pause everything

```
python3 ~/code/autohub/autohub.py pause     # the job keeps waking up but does nothing
python3 ~/code/autohub/autohub.py resume
```

To stop the job itself: `launchctl bootout gui/$UID/{{LABEL_PREFIX}}.autohub`. To start it again: `launchctl bootstrap gui/$UID ~/Library/LaunchAgents/{{LABEL_PREFIX}}.autohub.plist`.

## Uninstall

```
launchctl bootout gui/$UID/{{LABEL_PREFIX}}.autohub
rm ~/Library/LaunchAgents/{{LABEL_PREFIX}}.autohub.plist
claude mcp remove -s user autohub
rm -rf ~/.local/state/autohub
```

Then delete the Keychain item `autohub:canvas` in Keychain Access. Also delete the token in Canvas (Account → Settings). Finally, delete `~/code/autohub`.

The runner itself uses only Python's standard library. The `.venv` folder exists only for the `autohub` connector, which needs the `mcp` package.
