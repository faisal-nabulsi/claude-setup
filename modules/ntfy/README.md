# ntfy: push notifications to your phone from anything

**What it is:** [ntfy](https://ntfy.sh) ("notify") is a free, open-source push-notification
service. You send a plain HTTP POST with some text to `https://ntfy.sh/<topic>`, and every
phone (or browser) subscribed to that topic gets a notification within a second or two.
No account, no API key, no SDK.

**Why this setup uses it:** every watcher (deadline nudges, the Canvas scan, PollEv, the
morning brief, autohub, GitHub Actions jobs) needs one thing: "tell my phone". ntfy makes that
a one-line `curl`, from any machine, in any language.

## The one security rule

**The topic name works like a password.** Anyone who knows it can read your pushes and send you
fake ones. So:
- Make it long and random: `python3 -c "import secrets; print('tk-' + secrets.token_urlsafe(18))"`.
- Keep it in a secrets file (`.ntfy.env`, chmod 600) or a GitHub Actions secret, never in a repo.
- Don't push sensitive content (grades, codes, money): public ntfy.sh topics aren't end-to-end
  encrypted.
- For more: reserve a topic with an ntfy account and use an access token
  (`Authorization: Bearer tk_...`; `send-push.sh` supports `NTFY_TOKEN`), or self-host ntfy
  (`NTFY_SERVER=https://ntfy.yourdomain`).

## Send your first notification (2 minutes)

1. Install the **ntfy** app (App Store or Google Play) and allow notifications.
2. Make a topic name (command above). In the app: **+** → Subscribe to topic → paste it.
3. On your computer:
   ```bash
   curl -d "Hello from my laptop" https://ntfy.sh/YOUR-TOPIC
   ```
   Your phone buzzes. That's the whole API.
4. Extras, all optional headers:
   ```bash
   curl -H "Title: CS 101" -H "Priority: high" -H "Tags: warning" \
        -H "Click: https://canvas.example.edu" -d "Homework 3 due in 1 hour" https://ntfy.sh/YOUR-TOPIC
   ```
5. Save it for this setup: `printf 'NTFY_TOPIC=YOUR-TOPIC\n' > ~/semester-tracker/.ntfy.env && chmod 600 ~/semester-tracker/.ntfy.env`,
   then `bash ~/semester-tracker/send-push.sh "it works"`.

## What you can hook it up to

| From | How |
|---|---|
| Shell scripts, cron, launchd jobs | `curl -d "msg" https://ntfy.sh/$NTFY_TOPIC` (or this repo's `send-push.sh`) |
| Python | `urllib.request.urlopen(urllib.request.Request(f"https://ntfy.sh/{topic}", data=b"msg"))`, or `requests.post(url, data="msg")` |
| GitHub Actions | Store the topic as a repo secret; `curl` in a step. Examples: `examples/github-actions-job-alert/` (new job postings every 2 hours), the semester heartbeat |
| Claude scheduled tasks | Tell the task to run `bash ~/semester-tracker/send-push.sh "<summary>"` at the end |
| Uptime monitors | Uptime Kuma, Healthchecks.io and similar have ntfy built in |
| Home Assistant, Grafana, Node-RED, Zabbix | Built-in ntfy notifiers or a plain webhook to the topic URL |
| Long commands | `make build; curl -d "build finished: $?" ntfy.sh/$NTFY_TOPIC` |
| Email-only tools | ntfy can also receive email-to-topic on self-hosted servers |

## Example: job-alert workflow

`examples/github-actions-job-alert/` checks a public JSON list of internship postings every 2
hours and pushes only the new ones that match your keywords. Copy the folder into its own
private GitHub repo, edit `config.json` (source URL, include/exclude words), and add the
`NTFY_TOPIC` repo secret. The first run only records what exists, so you never get a flood.
