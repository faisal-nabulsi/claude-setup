# How Claude remembers you: CLAUDE.md, memory, and the life vault

Three layers, each with a different job.

| Layer | Holds | Who writes it | Loaded when |
|---|---|---|---|
| `~/.claude/CLAUDE.md` | Standing rules: how to work, what never to do | You (Claude can propose edits) | Every session, in full |
| Auto-memory (`MEMORY.md` + one file per memory) | How Claude should work for you, and your active projects | Claude, as it learns | The index every session; files on demand |
| Life vault (`~/life/`) | Facts about your life: people, orgs, events, places, history | Claude via `lv add` / `/remember`, plus the nightly capture loop | Searched when a question touches your life |

**Why three:** rules have to be read every time, so they must stay short. Working preferences
change often and need a cheap index. Life facts are numerous and only matter sometimes, so they
belong in a searchable store, not in context.

## The capture loop (how the vault reads your session history)

```
session ends ──► lv-capture (Stop hook): writes ~/life/_queue/pending/<session-id>
                  (one pointer file; never starts a process)
hourly ────────► lv-harvest: reads those sessions' transcripts in ~/.claude/projects/,
                  keeps only what YOU typed, redacts secrets, banks it in the evidence spool
nightly ───────► lv-sweep: one headless `claude -p` call per batch proposes facts;
                  mechanical gates check every name and number against your own words;
                  passes are filed, doubtful ones go to _review/INBOX.md
opt-in nightly ► lv-imessage (messages you sent), lv-mail (incoming mail via google-workspace)
always ────────► lv-forget re-applies _review/deletions.md so suppressed facts stay gone
```

`python3 ~/life/tools/bin/lv health` tells you whether each stage is actually running.

## Rules that keep the vault trustworthy

- **Append-only.** New entries supersede old ones; nothing is silently rewritten.
- **Stated beats derived.** Something Claude inferred (`conf/derived`) never overrides something
  you said (`conf/stated`).
- **Contradictions surface.** If Claude is about to say something that conflicts with the vault,
  it asks you which is right before changing anything.
- **Suppression is permanent.** `_review/deletions.md` patterns are applied on every run.
- **Open questions are queued**, not asked mid-task: `_review/questions.md`.
- **No secrets in the vault.** File that an account exists, never its password or number.
