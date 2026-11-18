# Phone glance: `GET /todo`

One tap on your iPhone shows your upcoming deadlines. It uses no AI tokens and no Mac, and it works while the laptop is asleep.

The `heartbeat-kicker` Cloudflare Worker (a small program Cloudflare runs on its servers) already fires the heartbeat cron. It now also answers one web address:

```
https://heartbeat-kicker.<your-subdomain>.workers.dev/todo
```

An iOS Shortcut calls that address with a secret key and shows the reply.

## What you see

Plain text, at most 25 lines, labels cut to 80 characters:

```
To-do · Fri 10/9 10:18am  (~ = soft)
OVERDUE
Yest 11:59pm  Hard essay due last night
~ Wed 10/7 7pm  Soft journal entry from Wednesday
NEXT 7 DAYS
Today 6pm  Gradescope make-ups
Tmrw 12pm  Problem set 4
~ Today 8pm  IF TIME: optional make-up entries
+3 more
```

- **OVERDUE**: rows that are not done and were due in the last 3 days.
- **NEXT 7 DAYS**: rows that are not done and are due between now and 7 days out.
- Inside each section, hard deadlines come first, then soft ones. Soft means a self-imposed target rather than a real due date, and soft rows start with `~`.
- If everything does not fit in 25 lines, the glance keeps overdue hard rows first, then upcoming hard, then overdue soft, then upcoming soft. A `+N more` line counts what was cut.
- Rows marked `done: true` never appear. They are permanent tombstones in the file.
- `?format=json` returns the same list as JSON, with each row's `id`. A later "Done" button would need those ids.

## Where the data comes from

The Worker reads `deadlines.json` from the `main` branch of the private repo `YOUR-GITHUB-LOGIN/semester-heartbeat`. It uses the GitHub contents API (GitHub's web interface for reading a file in a repo).

- It keeps each read for 60 seconds, so tapping the Shortcut repeatedly does not hit GitHub every time.
- If GitHub is down, the glance shows the last copy it read, up to 6 hours old. The title line then gets a `[GitHub unreachable, Nm old]` tag.
- New Canvas rows show up as soon as the heartbeat workflow commits them.
- Hand edits to the tracker copy (`semester-tracker/deadlines.json`) show up only after the usual `verify/sync_deadlines.py` run and a push of this repo. That is because the Worker can only see what is on GitHub.
- `todo.json` is deliberately ignored. It has not been edited since Sept 28 and still lists finished items, such as the 9/29 case comp, as open. Showing it would bring finished tasks back.

## Why this is safe

- **The key.** The Shortcut sends a random 64-character key in the header `Authorization: Bearer <key>`. "Bearer" means "whoever holds this key gets in", so the key works like a password. Without it, every request gets `401 unauthorized`.
- **Constant-time check.** The Worker compares keys in constant time. Response timing therefore cannot reveal how many characters of a guess were right.
- **Fails closed.** If the `GLANCE_KEY` secret is missing, nobody gets in.
- **One door only.** Only `GET /todo` exists. Every other path or method gets `404`.
- **No CORS.** CORS headers are what allow a web page from another site to read a reply in your browser. The Worker sends none, so a malicious website cannot read your list through your browser.
- **Read-only everywhere.** The Worker's GitHub token (`GITHUB_READ_TOKEN`) can only read file contents of this one repo. The Worker has no code that writes anything.
- **Nothing in logs.** The key travels in a header, not in the URL, so it never lands in web address logs. Cloudflare's logs blank out any header whose name contains "auth". The Worker never logs the key or the token, and GitHub errors show only a status number such as `HTTP 404`.
- **The key lives inside the Shortcut.** Anyone who gets a copy of the Shortcut can read your deadline list. That risk is acceptable because the key can only read, and one command revokes it (see below). Don't share the Shortcut; if you ever do, rotate the key.

Secrets stored in Cloudflare, shown as names only:

| Secret | What it is |
|---|---|
| `GH_TOKEN` | Existing. Fine-grained GitHub token with Actions read/write, used by the cron to trigger the workflow. It cannot read files. |
| `GLANCE_KEY` | The Shortcut's key. A copy is in your Mac login keychain as **heartbeat-glance-key** (account = your Mac username). |
| `GITHUB_READ_TOKEN` | Fine-grained GitHub token for this repo only, with Contents: Read-only. You create it (step 1 below). |

## Finish setup (one time)

The code is tested and `GLANCE_KEY` is already set. One thing is still missing: a GitHub token that can read the file. The existing `GH_TOKEN` was created with only the Actions permission, so it cannot read contents.

1. **Create a read-only GitHub token.**
   1. On github.com, click your profile photo, then **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
   2. Token name: `heartbeat-glance-read`.
   3. Expiration: a custom date after the semester, for example **2027-01-31**. When it expires, the glance shows `Couldn't load deadlines: GitHub read failed: HTTP 401`.
   4. Resource owner: `YOUR-GITHUB-LOGIN`.
   5. Repository access: **Only select repositories**, then pick `YOUR-GITHUB-LOGIN/semester-heartbeat`.
   6. Permissions: under Repository permissions, set **Contents** to **Read-only**. GitHub adds "Metadata: Read-only" by itself. Add nothing else.
   7. Click **Generate token** and copy it.
2. **Store it in Cloudflare.** In Terminal, run the commands below and paste the token at the hidden prompt.
   ```sh
   cd ~/semester-heartbeat/kicker
   wrangler secret put GITHUB_READ_TOKEN
   ```
   Don't paste the token anywhere else: not in a file, not in chat.
3. **Deploy.** This runs the tests, then a dry run, then the real deploy:
   ```sh
   npm run deploy
   ```
   If wrangler asks you to register a workers.dev subdomain, pick any short name; it becomes part of the URL. The output prints the URL, `https://heartbeat-kicker.<subdomain>.workers.dev`. The cron schedule is unchanged.
4. **Check it.**
   ```sh
   sh scripts/check.sh https://heartbeat-kicker.<subdomain>.workers.dev
   ```
   You want `401`, `401`, `404`, `404`, then `200` followed by the first 3 lines of your list. The script reads the key from the keychain without showing it.

## iOS Shortcut

Building it on the **Mac** Shortcuts app is easiest because you can paste the key straight from the keychain. Shortcuts syncs to your iPhone through iCloud. Menu names can differ slightly between iOS versions.

1. **Copy the key.** Choose one:
   - Open **Keychain Access**, search for `heartbeat-glance-key`, double-click it, tick **Show password** and copy it.
   - Or run `security find-generic-password -a "$USER" -s heartbeat-glance-key -w | pbcopy` in Terminal. This copies the key without displaying it.
2. **Create the shortcut.** Open **Shortcuts**, tap **+**, and rename the new shortcut **Todo** by tapping its name at the top. Siri runs it by this name.
3. **Add "Get Contents of URL".** Search for the action **Get Contents of URL** and add it.
   - Tap the URL placeholder and enter `https://heartbeat-kicker.<subdomain>.workers.dev/todo`.
   - Tap the arrow on the action to show more options.
   - Method: **GET** (the default).
   - Headers: tap **Add new header**. Set Key to `Authorization` and Value to `Bearer ` followed by the pasted key. Use exactly one space after `Bearer` and no quotes.
4. **Add "Show Result".** It displays the text the previous action fetched.
5. Tap **Done**, then tap the shortcut once to test it.

**Home Screen icon.** Open the shortcut, tap its name at the top, choose **Add to Home Screen**, then tap **Add**.

**Widget.** Long-press the Home Screen, tap **Edit → Add Widget → Shortcuts**, pick a size and tap **Add Widget**. While the icons wiggle, tap the widget and choose **Todo**. Tapping the widget then runs it.

**Other ways to run it.** Say "Hey Siri, Todo". On iPhones with an Action button, go to **Settings → Action Button → Shortcut → Todo**.

## Rotate or revoke the key

**Rotate** (make a new key; the old one stops working as soon as the secret updates). The commands generate the key, save it in the keychain and in Cloudflare, and never print it:

```sh
cd ~/semester-heartbeat/kicker
KEY="$(openssl rand -hex 32)" && security add-generic-password -a "$USER" -s heartbeat-glance-key -w "$KEY" -U && printf %s "$KEY" | wrangler secret put GLANCE_KEY; unset KEY
```

Then paste the new key into the Shortcut's `Authorization` header, as in step 1 above.

**Revoke, with no replacement:** `wrangler secret delete GLANCE_KEY`. Every request then gets 401, because the Worker fails closed.

**Cut off GitHub access:** delete the `heartbeat-glance-read` token on GitHub (Settings → Developer settings → Fine-grained tokens). The glance then shows a 502 error, and the cron is not affected.

## Changing it

- Window sizes, the line cap and the label length are constants at the top of `src/glance.js`.
- To run the tests: `npm test`. That is `node --test`, needs nothing to install, and uses fake rows in `test/fixtures/deadlines.json`.
- Local runtime check: `wrangler dev --compatibility-date 2026-07-21 --var GLANCE_KEY:<any 32+ char test value>`. The older date is only needed because the local runtime bundled with wrangler 4.112 stops at 2026-07-21. Locally there is no GitHub token, so an authorized request returns 502.

## Later: a "Done" button (design only, not built)

The goal: tap a row on the phone to mark it done.

**What's needed**

1. **`POST /done`** with the JSON body `{"id": "<row id>"}`. The ids come from `/todo?format=json`. It needs the same Bearer check, constant-time comparison, no CORS, and 404 for anything else.
2. **A separate write-capable GitHub token**, stored as `GITHUB_WRITE_TOKEN`: a fine-grained token for this repo only, with **Contents: Read and write**. Keep `GITHUB_READ_TOKEN` for `/todo`, because then a bug in the write path can never affect the read path, and you can revoke write access without losing the glance.
3. **A separate key, `GLANCE_DONE_KEY`.** The current key is recommended to stay read-only, because a leaked read key only exposes the list, while a leaked write key could mark everything done. "Done" is hard to undo: `done` always wins in the union merge, and the Mac's deadlines guard hook restores un-done flags.

**Merge semantics.** These steps are the whole safety story, so every one is required.

1. Fetch the file: `GET /repos/YOUR-GITHUB-LOGIN/semester-heartbeat/contents/deadlines.json?ref=main`, using the normal JSON form, not the raw form. That returns `content` (base64) plus `sha`, a fingerprint of the exact version read.
2. Parse it and find the row whose `id` matches.
   - No such row: return 404.
   - Row already done: return 200 and change nothing, so a double tap is harmless.
3. On **that row only**, set `"done": true` and `"done_at": "<now, LA local YYYY-MM-DDTHH:MM>"`. That matches the existing `done_at` format. **Never remove, reorder or rewrite any other row.**
4. Check before writing. Re-parse the new text and compare it to the original:
   - same number of rows;
   - same ids in the same order;
   - exactly one row differs, and only in `done` and `done_at`.

   If anything else changed, abort with 500.
5. Serialize in the file's current style, so the git diff is one or two lines.
   - Today the file uses 2-space indent and raw UTF-8. `JSON.stringify(rows, null, 2) + "\n"` reproduces it byte for byte, which was checked on 2026-10-09.
   - heartbeat.py's Canvas scan rewrites it with 1-space indent, so detect the indent from the second line instead of hard-coding it.
6. Write it back: `PUT` the same contents URL with:
   - `message: "glance: done <id>"`;
   - `content` = the new text, base64-encoded from UTF-8;
   - `branch: "main"`;
   - **`sha` = the sha from step 1.**

   The sha is a precondition. If the heartbeat workflow or a Mac session pushed in between, GitHub rejects the write with 409 instead of overwriting their change. On a rejection, start again from step 1, at most 3 times. After that, return 503 "busy, try again".
7. Clear the 60-second read cache so `/todo` shows the change right away.

**Shortcut flow**

1. Get Contents of URL (`…/todo?format=json`).
2. Get Dictionary Value `upcoming` (plus `overdue`).
3. Choose from List, showing each row's `label`.
4. Choose from Menu "Mark done?" with Yes and Cancel. This confirmation exists because a wrong tap is sticky.
5. Get Contents of URL: method POST, JSON body `id`, header `Authorization: Bearer <done key>`.

**Sync.** The Mac's tracker copy picks up the flag on the next `verify/sync_deadlines.py` run, because `done` always wins. The heartbeat workflow's `git pull --rebase` absorbs the new commit; it is a different line, so normally there is no conflict.

**Tests to add**

- Every other row stays byte-identical.
- An unknown id returns 404.
- An already-done row is a no-op.
- A sha conflict retries, then gives up with 503.
- Serializing the real file without changes returns the original bytes.
