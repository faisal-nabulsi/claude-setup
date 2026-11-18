# PollEv notifier (watch-only)

Many lecture courses take attendance with live polls on Poll Everywhere (PollEv). Polls open for a minute or two, so you can miss them while your phone is in your bag. This tool pushes your phone the moment a poll opens.

**What it does:** `notifier.py` asks PollEv's public "is a poll open?" feed for each presenter during your lecture window. When a new poll appears, it sends one phone push through `../send-push.sh` (ntfy).

**What it never does:** it never logs in, never answers a poll and never sends your identity anywhere. Answering for you would fake attendance. You answer in the room.

## Set up
1. Find the presenter handle: it is the part after `pollev.com/` on the lecture slides.
2. Add a `"pollev"` block to `../config.json` (see the top of `config.py` for the exact shape).
3. Copy `{{LABEL_PREFIX}}.pollev-notifier.plist` to `~/Library/LaunchAgents/`, edit the `StartCalendarInterval` times so it starts about 5 minutes before each lecture, then:
   `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{{LABEL_PREFIX}}.pollev-notifier.plist`
4. Test: `python3 notifier.py --check` (one request per presenter, no push) and `python3 notifier.py --test-push`.

The Mac must be awake during lecture. Log: `~/Library/Logs/pollev-notifier.log`.
